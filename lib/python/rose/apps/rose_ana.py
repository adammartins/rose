# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# (C) British Crown Copyright 2012-6 Met Office.
#
# This file is part of Rose, a framework for meteorological suites.
#
# Rose is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rose is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rose. If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------
"""Builtin application: "rose ana", a comparison engine for Rose."""


# Standard Python modules
import glob
import inspect
import os
import re
import sys
import sqlite3

# Rose modules
import rose.config
from rose.env import env_var_process
from rose.opt_parse import RoseOptionParser
from rose.popen import RosePopener, RosePopenError
from rose.reporter import Reporter, Event
from rose.resource import ResourceLocator
from rose.app_run import BuiltinApp

WARN = -1
PASS = 0
FAIL = 1

MAX_KGO_FILES = 100

OPTIONS = ["method_path"]

USRCOMPARISON_DIRNAME = "comparisons"
USRCOMPARISON_EXT = ".py"


class KGODatabase(object):
    """
    KGO Database object, stores comparison information for rose_ana apps.

    """
    # Stores retries of database creation and the maximum allowed number
    # of retries before an exception is raised
    RETRIES = 0
    MAX_RETRIES = 5

    def __init__(self):
        "Initialise the object."
        self.file_name = os.path.join(
            os.getenv("ROSE_SUITE_DIR"), "log", "rose-ana-comparisons.db")
        self.conn = None
        self.create()

    def get_conn(self):
        "Return a connection to the database if it exists."
        if self.conn is None:
            self.conn = sqlite3.connect(self.file_name, timeout=60.0)
        return self.conn

    def create(self):
        "If the databaste doesn't exist, create it."
        conn = self.get_conn()
        # This table stores the individual comparisons
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comparisons (
            app_task TEXT,
            kgo_file TEXT,
            suite_file TEXT,
            status TEXT,
            comparison TEXT,
            PRIMARY KEY(app_task))
            """)
        # And this one stores the completion status of the task
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
            task_name TEXT,
            completed INT,
            PRIMARY KEY(task_name))
            """)
        conn.commit()

    def enter_comparison(
            self, app_task, kgo_file, suite_file, status, comparison):
        "Insert a new comparison entry to the database."
        conn = self.get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO comparisons VALUES (?, ?, ?, ?, ?)",
            [app_task, kgo_file, suite_file, status, comparison])
        conn.commit()

    def enter_task(self, app_task, status):
        "Insert a new task entry to the database."
        conn = self.get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO tasks VALUES (?, ?)",
            [app_task, status])
        conn.commit()


class RoseAnaApp(BuiltinApp):

    """Run rosa ana as an application."""

    SCHEME = "rose_ana"

    def run(self, app_runner, conf_tree, opts, args, uuid, work_files):
        """Implement the "rose ana" command"""

        # Get config file option for user-specified method paths
        method_paths = [os.path.join(os.path.dirname(__file__),
                        USRCOMPARISON_DIRNAME)]
        conf = ResourceLocator.default().get_conf()

        my_conf = conf.get_value(["rose-ana", "method-path"])
        if my_conf:
            for item in my_conf.split():
                method_paths.append(item)

        # Initialise the analysis engine
        engine = Analyse(conf_tree.node, opts, args, method_paths,
                         reporter=app_runner.event_handler,
                         popen=app_runner.popen)

        # Initialise a database session
        rose_ana_task_name = os.getenv("ROSE_TASK_NAME")
        kgo_db = KGODatabase()

        # Add an entry for this task, with a non-zero status code
        kgo_db.enter_task(rose_ana_task_name, 1)

        # Run the analysis
        num_failed, tasks = engine.analyse()

        # Update the database to indicate that the task succeeded
        kgo_db.enter_task(rose_ana_task_name, 0)

        # Update the database
        for task in tasks:
            # The primary key in the database is composed from both the
            # rose_ana app name and the task index (to make it unique)
            app_task = "{0} ({1})".format(rose_ana_task_name, task.name)
            # Include an indication of what extract/comparison was performed
            comparison = "{0} : {1} : {2}".format(
                task.comparison, task.extract, getattr(task, "subextract", ""))
            kgo_db.enter_comparison(app_task, task.kgo1file, task.resultfile,
                                    task.userstatus, comparison)

        if num_failed != 0:
            raise TestsFailedException(num_failed)


class DataLengthError(Exception):

    """An exception if KGO and result data are of different lengths."""

    def __init__(self, task):
        self.resultlen = len(task.resultdata)
        self.kgolen = len(task.kgo1data)
        self.taskname = task.name

    def __repr__(self):
        return "Mismatch in data lengths in %s (%s and %s)" % (
            self.taskname, self.resultlen, self.kgolen)

    __str__ = __repr__


class TaskCompletionEvent(Event):

    """Event for completing a comparison from AnalysisTask."""

    def __init__(self, task):
        self.message = task.message
        self.userstatus = task.userstatus
        self.level = Event.DEFAULT
        self.kind = Event.KIND_OUT

    def __repr__(self):
        return " %s" % (self.message)

    __str__ = __repr__


class TestsFailedException(Exception):

    """Exception raised if any rose-ana comparisons fail."""

    def __init__(self, num_failed):
        self.rc = num_failed

    def __repr__(self):
        return "%s tests did not pass" % (self.rc)

    __str__ = __repr__


class Analyse(object):

    """A comparison engine for Rose."""

    def __init__(self, config, opts, args, method_paths, reporter=None,
                 popen=None):
        if reporter is None:
            self.reporter = Reporter(opts.verbosity - opts.quietness)
        else:
            self.reporter = reporter
        if popen is None:
            self.popen = RosePopener(event_handler=self.reporter)
        else:
            self.popen = popen
        self.opts = opts
        self.args = args
        self.config = config
        self.load_tasks()
        modules = []
        for path in method_paths:
            for filename in glob.glob(path + "/*.py"):
                modules.append(filename)
        self.load_user_comparison_modules(modules)

    def analyse(self):
        """Perform comparisons given a list of tasks."""
        rc = 0
        for task in self.tasks:

            if self.check_extract(task):
                # Internal AnalysisEngine extract+comparison test

                # Extract data from results and from kgoX
                task = self.do_extract(task, "result")

                for i in range(task.numkgofiles):
                    var = "kgo" + str(i + 1)
                    task = self.do_extract(task, var)

                task = self.do_comparison(task)
            else:
                # External program(s) doing test

                # Command to run
                command = task.extract

                result = re.search(r"\$file", command)
                # If the command contains $file, it is run separately for both
                # the KGO and the result files
                if result:
                    # Replace $file token with resultfile or kgofile
                    resultcommand = self._expand_tokens(command, task,
                                                        "result")
                    kgocommand = self._expand_tokens(command, task, "kgo1")

                    # Run the command on the resultfile
                    task.resultdata = self._run_command(resultcommand)

                    # Run the command on the KGO file
                    task.kgo1data = self._run_command(kgocommand)
                else:
                    # The command works on both files at the same time
                    # Replace tokens $kgofile and $resultfile for actual values
                    command = self._expand_tokens(command, task)

                    # Run the command
                    task.resultdata = self._run_command(command)

                # Run the comparison
                task = self.do_comparison(task)

            self.reporter(TaskCompletionEvent(task),
                          prefix="[%s]" % (task.userstatus))
            if task.numericstatus != PASS:
                rc += 1
        return rc, self.tasks

    def check_extract(self, task):
        """Check if an extract name is present in a user method."""
        for module_name, class_name, method, help in self.user_methods:
            if task.extract == class_name:
                return True
        return False

    def do_comparison(self, task):
        """Run the comparison."""
        for module_name, class_name, method, help in self.user_methods:
            comparison_name = ".".join([module_name, class_name])
            if task.comparison == class_name:
                for module in self.modules:
                    if module.__name__ == module_name:
                        comparison_inst = getattr(module, class_name)()
                        comparison_meth = getattr(comparison_inst, "run")(task)
        return task

    def do_extract(self, task, var):
        """Extract the specified data."""
        for module_name, class_name, method, help in self.user_methods:
            extract_name = ".".join([module_name, class_name])
            if task.extract == class_name:
                for module in self.modules:
                    if module.__name__ == module_name:
                        extract_inst = getattr(module, class_name)()
                        extract_meth = getattr(extract_inst, "run")(task, var)
        return task

    def _run_command(self, command):
        """Run an external command using rose.popen."""
        output, stderr = self.popen.run_ok(command, shell=True)
        output = "".join(output).splitlines()
        return output

    def _expand_tokens(self, inputstring, task, var=None):
        """Expands tokens $resultfile, $file and $kgoXfile."""
        filename = ''
        if var:
            filename = getattr(task, var + "file")
        expansions = {'resultfile': task.resultfile, 'file': filename}
        for i in range(1, task.numkgofiles + 1):
            key = "kgo" + str(i) + "file"
            value = getattr(task, key)
            expansions[key] = value
        inputstring = inputstring.format(**expansions)
        return inputstring

    def _find_file(self, var, task):
        """Finds a file given a variable name containing the filename.

        Given a variable name and task object, this returns the filename it
        points to, including expanding any * characters with glob.
        """

        filevar = var + "file"
        if hasattr(task, filevar):
            configvar = var + "fileconfig"
            setattr(task, configvar, getattr(task, filevar))
            filenames = glob.glob(env_var_process(getattr(task, filevar)))
            if len(filenames) > 0:
                setattr(task, filevar, os.path.abspath(filenames[0]))
        return task

    def load_tasks(self):
        """Loads AnalysisTasks from files.

        Given a list of files, return AnalysisTasks generated from those files.
        This also expands environment variables in filenames, but saves the
        original contents for use when writing out config files
        """

        tasks = []
        for task in self.config.value.keys():
            if task is "env":
                continue
            if task.startswith("file:"):
                continue
            newtask = AnalysisTask()
            newtask.name = task
            value = self.config.get_value([task, "resultfile"])

            # If the task is ignored, this will be None, so continue
            # on to the next task
            if value is None:
                continue

            if "{}" in value:
                newtask.resultfile = value.replace("{}", self.args[0])
            else:
                newtask.resultfile = value
            newtask = self._find_file("result", newtask)
            newtask.extract = self.config.get_value([task, "extract"])
            result = re.search(r":", newtask.extract)
            if result:
                newtask.subextract = ":".join(newtask.extract.split(":")[1:])
                newtask.extract = newtask.extract.split(":")[0]
            newtask.comparison = self.config.get_value([task, "comparison"])
            newtask.tolerance = self.config.get_value([task, "tolerance"])
            newtask.warnonfail = (
                self.config.get_value([task, "warnonfail"]) in ["yes", "true"])

            # Allow for multiple KGO, e.g. kgo1file, kgo2file, for
            # statistical comparisons of results
            newtask.numkgofiles = 0
            for i in range(1, MAX_KGO_FILES):
                kgovar = "kgo" + str(i)
                kgofilevar = kgovar + "file"
                if self.config.get([task, kgofilevar]):
                    value = self.config.get([task, kgofilevar])[:]
                    if "{}" in value:
                        setattr(
                            newtask, kgofilevar,
                            value.replace("{}", self.args[0]))
                    else:
                        setattr(newtask, kgofilevar, value)
                    newtask.numkgofiles += 1
                    newtask = self._find_file(kgovar, newtask)
                else:
                    break
            tasks.append(newtask)
        self.tasks = tasks
        return tasks

    def load_user_comparison_modules(self, files):
        """Import comparison modules and store them."""
        modules = []
        for filename in files:
            directory = os.path.dirname(filename)
            if (not directory.endswith(USRCOMPARISON_DIRNAME) or
                    not filename.endswith(USRCOMPARISON_EXT)):
                continue
            comparison_name = os.path.basename(filename).rpartition(
                USRCOMPARISON_EXT)[0]
            sys.path.insert(0, os.path.abspath(directory))
            try:
                modules.append(__import__(comparison_name))
            except ImportError as e:
                self.reporter(e)
            sys.path.pop(0)
        modules.sort()
        self.modules = modules

        user_methods = []
        for module in modules:
            comparison_name = module.__name__
            contents = inspect.getmembers(module, inspect.isclass)
            for obj_name, obj in contents:
                att_name = "run"
                if hasattr(obj, att_name) and callable(getattr(obj, att_name)):
                    doc_string = obj.__doc__
                    user_methods.append((comparison_name, obj_name, att_name,
                                        doc_string))
        self.user_methods = user_methods
        return user_methods

    def write_config(self, filename, tasks):
        """Write an analysis config file based on a list of tasks provided"""
        config = rose.config.ConfigNode()

        for task in tasks:
            sectionname = task.name
            if task.resultfileconfig:
                config.set([sectionname, "resultfile"], task.resultfileconfig)
            for i in range(1, task.numkgofiles + 1):
                origvar = "kgo" + str(i) + "fileconfig"
                valvar = "kgo" + str(i) + "file"
                if hasattr(task, origvar):
                    config.set([sectionname, valvar], getattr(task, origvar))
            if task.extract:
                config.set([sectionname, "extract"], task.extract)
            if task.subextract:
                config.set([sectionname, "extract"],
                           task.extract + ":" + task.subextract)
            if task.comparison:
                config.set([sectionname, "comparison"], task.comparison)
            if task.tolerance:
                config.set([sectionname, "tolerance"], task.tolerance)
            if task.warnonfail:
                config.set([sectionname, "warnonfail"], "true")
        rose.config.dump(config, filename)


class AnalysisTask(object):

    """Class to completely describe an analysis task.

    Attributes:
        name                    # Short description of test for the user
        resultfile              # File generated by suite
        kgo1file                # Known Good output file
        comparison              # Comparison type
        extract                 # Extract method
        subextract              # Extract sub-type (if any)
        tolerance               # Tolerance (optional in file)
        warnonfail              # True if failure is just a warning
        numkgofiles             # Number of KGO files
        resultdata              # Data from result file
        kgo1data                # Data from KGO file
        ok                      # True if test didn"t fail
        message                 # User message
        userstatus              # User status
        numericstatus           # Numeric status
    """

    def __init__(self):

        # Variables defined in config file
        self.name = None
        self.resultfile = None
        self.kgo1file = None
        self.comparison = None
        self.extract = None
        self.tolerance = None
        self.warnonfail = False
        self.numkgofiles = 0

# Variables to save settings before environment variable expansion (for
# writing back to config file, rerunning, etc)
        self.resultfileconfig = self.resultfile
        self.kgofileconfig = self.kgo1file

# Data variables filled by extract methods
        self.resultdata = []
        self.kgo1data = []

# Variables set by comparison methods
        self.ok = False

        self.message = None
        self.userstatus = "UNTESTED"
        self.numericstatus = WARN

# Methods for setting pass/fail/warn; all take an object of one of the
# success/ failure/warning classes as an argument, which all have a sensible
# user message as the string representation of them.
    def set_failure(self, message):
        """Sets the status of the task to "FAIL"."""

        if self.warnonfail:
            self.set_warning(message)
        else:
            self.ok = False
            self.message = message
            self.userstatus = "FAIL"
            self.numericstatus = FAIL

    def set_pass(self, message):
        """Sets the status of the task to " OK "."""

        self.ok = True
        self.message = message
        self.userstatus = " OK "
        self.numericstatus = PASS

    def set_warning(self, message):
        """Sets the status of the task to "WARN"."""

        self.ok = True
        self.message = message
        self.userstatus = "WARN"
        self.numericstatus = WARN

    def __repr__(self):
        return "%s: %s" % (self.name, self.userstatus)

    __str__ = __repr__


def data_from_regexp(regexp, filename):
    """Returns a list of text matching a regexp from a given file."""
    numbers = []
    for line in open(filename):
        result = re.search(regexp, line)
        if result:
            numbers.append(result.group(1))
    return numbers

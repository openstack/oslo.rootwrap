# Copyright (c) 2011 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import configparser
import logging
import logging.handlers
import os
import signal
import sys
from typing import Any, cast

from oslo_rootwrap import filters
from oslo_rootwrap import subprocess

if sys.platform != 'win32':
    import pwd

LOG = logging.getLogger(__name__)


class NoFilterMatched(Exception):
    """This exception is raised when no filter matched."""

    pass


class FilterMatchNotExecutable(Exception):
    """Raised when a filter matched but no executable was found."""

    def __init__(
        self, match: filters.CommandFilter | None = None, **kwargs: Any
    ) -> None:
        self.match = match


class RootwrapConfig:
    filters_path: list[str]
    exec_dirs: list[str]
    syslog_log_facility: int
    syslog_log_level: int
    use_syslog: bool
    daemon_timeout: int
    rlimit_nofile: int

    def __init__(self, config: configparser.RawConfigParser) -> None:
        # filters_path
        self.filters_path = config.get("DEFAULT", "filters_path").split(",")

        # exec_dirs
        if config.has_option("DEFAULT", "exec_dirs"):
            self.exec_dirs = config.get("DEFAULT", "exec_dirs").split(",")
        else:
            self.exec_dirs = []
            # Use system PATH if exec_dirs is not specified
            if "PATH" in os.environ:
                self.exec_dirs = os.environ['PATH'].split(':')

        # syslog_log_facility
        if config.has_option("DEFAULT", "syslog_log_facility"):
            v = config.get("DEFAULT", "syslog_log_facility")
            facility_names = logging.handlers.SysLogHandler.facility_names
            facility: int | None = getattr(
                logging.handlers.SysLogHandler, v, None
            )
            if facility is None and v in facility_names:
                facility = facility_names.get(v)
            if facility is None:
                raise ValueError(f'Unexpected syslog_log_facility: {v}')
            self.syslog_log_facility = facility
        else:
            self.syslog_log_facility = (
                logging.handlers.SysLogHandler.LOG_SYSLOG
            )

        # syslog_log_level
        if config.has_option("DEFAULT", "syslog_log_level"):
            v = config.get("DEFAULT", "syslog_log_level")
            level_name = v.upper()
            level: int | str = logging.getLevelName(level_name)
            if isinstance(level, str):
                raise ValueError(f'Unexpected syslog_log_level: {v!r}')
            self.syslog_log_level = level
        else:
            self.syslog_log_level = logging.ERROR

        # use_syslog
        if config.has_option("DEFAULT", "use_syslog"):
            self.use_syslog = config.getboolean("DEFAULT", "use_syslog")
        else:
            self.use_syslog = False

        # daemon_timeout
        if config.has_option("DEFAULT", "daemon_timeout"):
            self.daemon_timeout = int(config.get("DEFAULT", "daemon_timeout"))
        else:
            self.daemon_timeout = 600

        # fd ulimit
        if config.has_option("DEFAULT", "rlimit_nofile"):
            self.rlimit_nofile = int(config.get("DEFAULT", "rlimit_nofile"))
        else:
            self.rlimit_nofile = 1024


def setup_syslog(execname: str, facility: int, level: int) -> None:
    try:
        handler = logging.handlers.SysLogHandler(
            address='/dev/log', facility=facility
        )
    except OSError:
        LOG.warning(
            "Unable to setup syslog, maybe /dev/log socket needs "
            "to be restarted. Ignoring syslog configuration "
            "options."
        )
        return

    rootwrap_logger = logging.getLogger()
    rootwrap_logger.setLevel(level)
    handler.setFormatter(
        logging.Formatter(os.path.basename(execname) + ': %(message)s')
    )
    rootwrap_logger.addHandler(handler)


def build_filter(class_name: str, *args: Any) -> filters.CommandFilter | None:
    """Returns a filter object of class class_name."""
    if not hasattr(filters, class_name):
        LOG.warning(
            "Skipping unknown filter class (%s) specified "
            "in filter definitions",
            class_name,
        )
        return None
    filterclass = getattr(filters, class_name)
    return cast(filters.CommandFilter, filterclass(*args))


def load_filters(filters_path: list[str]) -> list[filters.CommandFilter]:
    """Load filters from a list of directories."""
    filterlist = []
    for filterdir in filters_path:
        if not os.path.isdir(filterdir):
            continue
        for filterfile in filter(
            lambda f: not f.startswith('.'), os.listdir(filterdir)
        ):
            filterfilepath = os.path.join(filterdir, filterfile)
            if not os.path.isfile(filterfilepath):
                continue
            filterconfig = configparser.RawConfigParser(strict=False)
            filterconfig.read(filterfilepath)
            for name, value in filterconfig.items("Filters"):
                filterdefinition = [s.strip() for s in value.split(',')]
                newfilter = build_filter(*filterdefinition)
                if newfilter is None:
                    continue
                newfilter.name = name
                filterlist.append(newfilter)
    # And always include privsep-helper
    privsep = build_filter("CommandFilter", "privsep-helper", "root")
    assert privsep is not None  # narrow type
    privsep.name = "privsep-helper"
    filterlist.append(privsep)
    return filterlist


def match_filter(
    filter_list: list[filters.CommandFilter],
    userargs: list[str],
    exec_dirs: list[str] | None = None,
) -> filters.CommandFilter:
    """Checks user command and arguments through command filters.

    Returns the first matching filter.

    Raises NoFilterMatched if no filter matched.
    Raises FilterMatchNotExecutable if no executable was found for the
    best filter match.
    """
    first_not_executable_filter = None
    exec_dirs = exec_dirs or []

    for f in filter_list:
        if f.match(userargs):
            if isinstance(f, filters.ChainingFilter):
                # This command calls exec verify that remaining args
                # matches another filter.
                def non_chain_filter(fltr: filters.CommandFilter) -> bool:
                    return fltr.run_as == f.run_as and not isinstance(
                        fltr, filters.ChainingFilter
                    )

                leaf_filters = [
                    fltr for fltr in filter_list if non_chain_filter(fltr)
                ]
                args = f.exec_args(userargs)
                if not args:
                    continue
                try:
                    match_filter(leaf_filters, args, exec_dirs=exec_dirs)
                except (NoFilterMatched, FilterMatchNotExecutable):
                    continue

            # Try other filters if executable is absent
            if not f.get_exec(exec_dirs=exec_dirs):
                if not first_not_executable_filter:
                    first_not_executable_filter = f
                continue
            # Otherwise return matching filter for execution
            return f

    if first_not_executable_filter:
        # A filter matched, but no executable was found for it
        raise FilterMatchNotExecutable(match=first_not_executable_filter)

    # No filter matched
    raise NoFilterMatched()


def _getlogin() -> str | None:
    try:
        return os.getlogin()
    except OSError:
        return (
            os.getenv('USER') or os.getenv('USERNAME') or os.getenv('LOGNAME')
        )


def start_subprocess(
    filter_list: list[filters.CommandFilter],
    userargs: list[str],
    exec_dirs: list[str] | None = None,
    log: bool = False,
    **kwargs: Any,
) -> subprocess.Popen[bytes]:
    filtermatch = match_filter(filter_list, userargs, exec_dirs or [])

    command = filtermatch.get_command(userargs, exec_dirs or [])
    if log:
        LOG.info(
            "(%s > %s) Executing %s (filter match = %s)",
            _getlogin(),
            pwd.getpwuid(os.getuid())[0],
            command,
            filtermatch.name,
        )

    def preexec() -> None:
        # Python installs a SIGPIPE handler by default. This is
        # usually not what non-Python subprocesses expect.
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        filtermatch.preexec()

    obj = subprocess.Popen(
        command,
        preexec_fn=preexec,
        env=filtermatch.get_environment(userargs),
        **kwargs,
    )
    return obj

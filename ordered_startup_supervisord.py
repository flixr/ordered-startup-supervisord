#!/usr/bin/env python

from __future__ import print_function
import sys
import os
import glob
import logging
from supervisor.options import UnhosedConfigParser
from supervisor import childutils


def get_all_configs(root_path):
    """Get all the configuration files to be parsed.  This is kinda weird because in order to do it we must
    parse the configs.
    """
    retval = [root_path]
    parser = UnhosedConfigParser()
    parser.read(root_path)
    if 'include' in parser.sections() and parser.has_option('include', 'files'):
        files = parser.get('include', 'files').split()
        base_dir = os.path.dirname(os.path.abspath(root_path))
        for pattern in files:
            if pattern.startswith('/'):
                for config in glob.glob(pattern):
                    retval.extend(get_all_configs(config))
            else:
                for config in glob.glob(os.path.join(base_dir, pattern)):
                    retval.extend(get_all_configs(config))
    return retval


def get_default_config_file():
    here = os.path.dirname(os.path.dirname(sys.argv[0]))
    search_paths = [os.path.join(here, 'etc', 'supervisord.conf'),
     os.path.join(here, 'supervisord.conf'),
     'supervisord.conf',
     'etc/supervisord.conf',
     '/etc/supervisord.conf',
     '/etc/supervisor/supervisord.conf',
     ]
    for possible in search_paths:
        if os.path.exists(possible):
            return possible
    return None


ProcessStates = [
    'STOPPED',
    'STARTING',
    'RUNNING',
    'BACKOFF',
    'STOPPING',
    'EXITED',
    'FATAL',
    'UNKNOWN'
]


class OrderedStartupOption(object):

    def __init__(self, parser, section_name):
        """

        :param parser: the config parser object
        :type parser: UnhosedConfigParser
        :param section_name: The name of the section to get the options from
        :type section_name: str
        """
        self.autostart = False
        if parser.has_option(section_name, 'autostart'):
            self.autostart = parser.getboolean(section_name, 'autostart')
        self.startinorder = False
        if parser.has_option(section_name, 'startinorder'):
            self.startinorder = parser.getboolean(section_name, 'startinorder')
        self.startnext = self.startinorder
        if parser.has_option(section_name, 'startnext'):
            self.startnext = parser.getboolean(section_name, 'startnext')
        self.startnextafter = 'RUNNING'
        if parser.has_option(section_name, 'startnextafter'):
            self.startnextafter = parser.get(section_name, 'startnextafter').upper()
            if self.startnextafter not in ProcessStates:
                self.startnextafter = 'RUNNING'


class Program(object):

    def __init__(self):
        self.name = ""
        self.group = ""
        self.procname = ""
        self.priority = 1000
        self.options = None
        """:type : OrderedStartupOption"""


class StartupPlan(object):

    def __init__(self, parser):
        """Create a new StartupPlan, which is a fancy term for an index of program names
        and OrderedStartupOptions.

        :param parser: The config parser object.
        :type parser: UnhosedConfigParser
        """

        self.programs = []
        """:type : list[Program]"""

        for section_name in parser.sections():
            if section_name.startswith('program:'):
                option = OrderedStartupOption(parser, section_name)
                program = Program()
                program.name = section_name[8:]
                program.procname = program.name
                program.options = option
                if parser.has_option(section_name, 'priority'):
                    program.priority = parser.getint(section_name, 'priority')
                self.programs.append(program)

        self.programs.sort(key=lambda x: x.priority)


def main():
    logging.basicConfig(filename='ordered_startup.log', level=logging.DEBUG)
    log = logging.getLogger('ordered_startup_supervisord.main')
    try:
        config_file = None
        if len(sys.argv) > 1:
            config_file = sys.argv[1]
        if config_file is None:
            config_file = get_default_config_file()
        if config_file is None:
            print("Unable to find a config file!", file=sys.stderr)
            sys.exit(1)
        if not os.path.exists(config_file):
            print("Config path {} does not exist!".format(config_file), file=sys.stderr)
            sys.exit(1)

        parser = UnhosedConfigParser()
        parser.read(get_all_configs(config_file))
        startup_plan = StartupPlan(parser)

        rpcinterface = childutils.getRPCInterface(os.environ)

        # get group for all processes
        processes = rpcinterface.supervisor.getAllProcessInfo()
        for proc in processes:
            # if group and name differ, process is in a group and startProcess needs "group:name" as arg
            if proc['name'] != proc['group']:
                for p in startup_plan.programs:
                    if p.name == proc['name']:
                        p.group = proc['group']
                        p.procname = "{}:{}".format(proc['group'], proc['name'])
                        break

        log.info("programs in order: ")
        for prog in startup_plan.programs:
            log.info(prog.name)
        if not startup_plan.programs[0].options.autostart:
            rpcinterface.supervisor.startProcess(startup_plan.programs[0].name, False)
        initial_start = 'STARTED'
        while 1:
            headers, payload = childutils.listener.wait()
            if headers['eventname'].startswith('PROCESS_STATE') and initial_start != 'FINISHED':
                pheaders = childutils.get_headers(payload)
                log.debug("headers = {}".format(repr(headers)))
                log.debug("payload = {}".format(repr(pheaders)))
                state = headers['eventname'][len('PROCESS_STATE_'):]
                start_next = False
                for program in startup_plan.programs:
                    if start_next:
                        log.info("Starting process: {}".format(program.procname))
                        rpcinterface.supervisor.startProcess(program.procname)
                        start_next = False
                        break
                    if program.options.startinorder and program.name == pheaders['processname'] and program.options.startnextafter == state:
                        if program.options.startnext:
                            log.info("Recieved process state of {} from {}, starting next process.".format(state, program.name))
                            start_next = True
                        else:
                            log.info("Recieved process state of {} from {}, with startnext set to false.".format(state, program.name))
                            log.info("No more processes to start for initial startup, ignoring all future events.")
                            initial_start = 'FINISHED'
                            break
                else:
                    if start_next:
                        log.info("No more processes to start for initial startup, ignoring all future events.")
                        initial_start = 'FINISHED'
                #log.debug("data = {}".format(repr(pdata)))
            childutils.listener.ok()
    except:
        log.error("ERROR: ", exc_info=sys.exc_info())


if __name__ == "__main__":
    main()

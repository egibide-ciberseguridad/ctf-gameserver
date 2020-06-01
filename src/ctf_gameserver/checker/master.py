import base64
import datetime
import logging
import math
import os
import signal
import time

import psycopg2
from psycopg2 import errorcodes as postgres_errors

from ctf_gameserver.lib.args import get_arg_parser_with_db
from ctf_gameserver.lib import daemon
from ctf_gameserver.lib.database import transaction_cursor
from ctf_gameserver.lib.checkresult import CheckResult
from ctf_gameserver.lib.exceptions import DBDataError
import ctf_gameserver.lib.flag as flag_lib

from . import database
from .supervisor import RunnerSupervisor
from .supervisor import ACTION_FLAG, ACTION_LOAD, ACTION_STORE, ACTION_RESULT


def main():

    arg_parser = get_arg_parser_with_db('CTF Gameserver Checker Master')
    arg_parser.add_argument('--ippattern', type=str, required=True,
                            help='(Old-style) Python formatstring for building the IP to connect to')
    arg_parser.add_argument('--flagsecret', type=str, required=True,
                            help='Base64 string used as secret in flag generation')

    group = arg_parser.add_argument_group('statedb', 'Checker state database')
    group.add_argument('--statedbhost', type=str, help='Hostname of the database. If unspecified, the '
                       'default Unix socket will be used.')
    group.add_argument('--statedbname', type=str, required=True,
                       help='Name of the used database')
    group.add_argument('--statedbuser', type=str, required=True,
                       help='User name for database access')
    group.add_argument('--statedbpassword', type=str,
                       help='Password for database access if needed')

    group = arg_parser.add_argument_group('check', 'Check parameters')
    group.add_argument('--service', type=str, required=True,
                       help='Slug of the service')
    group.add_argument('--checkerscript', type=str, required=True,
                       help='Path of the Checker Script')
    group.add_argument('--sudouser', type=str, help=' User to excute the Checker Scripts as, will be passed '
                       'to `sudo -u`')
    group.add_argument('--maxcheckduration', type=float, required=True,
                       help='Maximum duration of a Checker Script run in seconds')
    group.add_argument('--checkercount', type=int, required=True,
                       help='Number of Checker Masters running for this service')
    group.add_argument('--interval', type=float, required=True,
                       help='Time between launching batches of Checker Scripts in seconds')

    group = arg_parser.add_argument_group('logging', 'Checker Script logging')
    group.add_argument('--journald', action='store_true', help='Log Checker Script messages to journald')
    group.add_argument('--gelf-server', help='Log Checker Script messages to the specified GELF (Graylog) '
                       'server ("<host>:<port>")')

    args = arg_parser.parse_args()

    logging.basicConfig(format='[%(levelname)s] %(message)s [%(name)s]')
    numeric_loglevel = getattr(logging, args.loglevel.upper())
    logging.getLogger().setLevel(numeric_loglevel)

    if args.interval < 3:
        logging.error('`--interval` must be at least 3 seconds')
        return os.EX_USAGE

    logging_params = {}

    if args.journald:
        try:
            # pylint: disable=import-outside-toplevel,unused-import,import-error
            from systemd.journal import JournalHandler
        except ImportError:
            logging.error('systemd module is required for journald logging')
            return os.EX_USAGE
        logging_params['journald'] = True

    if args.gelf_server is not None:
        try:
            # pylint: disable=import-outside-toplevel,unused-import,import-error
            import graypy
        except ImportError:
            logging.error('graypy module is required for GELF logging')
            return os.EX_USAGE
        try:
            gelf_host, gelf_port = args.gelf_server.rsplit(':', 1)
            gelf_port = int(gelf_port)
        except ValueError:
            logging.error('GELF server needs to be specified as "<host>:<port>"')
            return os.EX_USAGE
        logging_params['gelf'] = {'host': gelf_host, 'port': gelf_port}

    try:
        game_db_conn = psycopg2.connect(host=args.dbhost, database=args.dbname, user=args.dbuser,
                                        password=args.dbpassword)
    except psycopg2.OperationalError as e:
        logging.error('Could not establish connection to game database: %s', e)
        return os.EX_UNAVAILABLE
    logging.info('Established connection to game database')

    try:
        state_db_conn = psycopg2.connect(host=args.statedbhost, database=args.statedbname,
                                         user=args.statedbuser, password=args.statedbpassword)
    except psycopg2.OperationalError as e:
        logging.error('Could not establish connection to state database: %s', e)
        return os.EX_UNAVAILABLE
    logging.info('Established connection to state database')

    # Keep our mental model easy by always using (timezone-aware) UTC for dates and times
    with transaction_cursor(game_db_conn) as cursor:
        cursor.execute('SET TIME ZONE "UTC"')
    with transaction_cursor(state_db_conn) as cursor:
        cursor.execute('SET TIME ZONE "UTC"')

    # Check database grants
    try:
        try:
            database.get_control_info(game_db_conn, prohibit_changes=True)
        except DBDataError as e:
            logging.warning('Invalid database state: %s', e)

        service_id = database.get_service_attributes(game_db_conn, args.service, prohibit_changes=True)['id']
        database.get_current_tick(game_db_conn, prohibit_changes=True)
        database.get_task_count(game_db_conn, service_id, prohibit_changes=True)
        database.get_new_tasks(game_db_conn, service_id, 1, prohibit_changes=True)
        database.commit_result(game_db_conn, service_id, 1, -1, 0, prohibit_changes=True)
        database.load_state(state_db_conn, service_id, 1, 'identifier', prohibit_changes=True)
        database.store_state(state_db_conn, service_id, 1, 'identifier', 'data', prohibit_changes=True)
    except psycopg2.ProgrammingError as e:
        if e.pgcode == postgres_errors.INSUFFICIENT_PRIVILEGE:
            # Log full exception because only the backtrace will tell which kind of permission is missing
            logging.exception('Missing database permissions:')
            return os.EX_NOPERM
        else:
            raise

    daemon.notify('READY=1')

    while True:
        try:
            master_loop = MasterLoop(game_db_conn, state_db_conn, args.service, args.checkerscript,
                                     args.sudouser, args.maxcheckduration, args.checkercount, args.interval,
                                     args.ippattern, args.flagsecret, logging_params)
            break
        except DBDataError as e:
            logging.warning('Waiting for valid database state: %s', e)
            time.sleep(60)

    # Graceful shutdown to prevent loss of check results
    def sigterm_handler(_, __):
        logging.info('Shutting down, waiting for %d Checker Scripts to finish',
                     master_loop.get_running_script_count())
        master_loop.shutting_down = True
    signal.signal(signal.SIGTERM, sigterm_handler)

    while True:
        try:
            master_loop.step()
            if master_loop.shutting_down and master_loop.get_running_script_count() == 0:
                break
        except:    # noqa, pylint: disable=bare-except
            logging.exception('Error in main loop:')

    return os.EX_OK


class MasterLoop:

    def __init__(self, game_db_conn, state_db_conn, service_slug, checker_script, sudo_user,
                 max_check_duration, checker_count, interval, ip_pattern, flag_secret, logging_params):
        self.game_db_conn = game_db_conn
        self.state_db_conn = state_db_conn
        self.checker_script = checker_script
        self.sudo_user = sudo_user
        self.max_check_duration = max_check_duration
        self.checker_count = checker_count
        self.interval = interval
        self.ip_pattern = ip_pattern
        self.flag_secret = flag_secret
        self.logging_params = logging_params

        control_info = database.get_control_info(self.game_db_conn)
        self.tick_duration = datetime.timedelta(seconds=control_info['tick_duration'])
        self.flag_valid_ticks = control_info['valid_ticks']
        self.service = database.get_service_attributes(self.game_db_conn, service_slug)
        self.service['slug'] = service_slug

        self.supervisor = RunnerSupervisor()
        self.known_tick = -1
        # Trigger launch of tasks in first step()
        self.last_launch = get_monotonic_time() - self.interval
        self.tasks_per_launch = None
        self.shutting_down = False

    def step(self):
        """
        Handles a request from the supervisor, kills overdue tasks and launches new ones.
        Only processes one request at a time to make sure that launch_tasks() gets called regularly and
        long-running tasks get killed, at the cost of accumulating a backlog of messages.

        Returns:
            A boolean indicating whether a request was handled.
        """
        req = self.supervisor.get_request()
        if req is not None:
            resp = None
            send_resp = True

            try:
                if req['action'] == ACTION_FLAG:
                    resp = self.handle_flag_request(req['info'], req['param'])
                elif req['action'] == ACTION_LOAD:
                    resp = self.handle_load_request(req['info'], req['param'])
                elif req['action'] == ACTION_STORE:
                    self.handle_store_request(req['info'], req['param'])
                elif req['action'] == ACTION_RESULT:
                    self.handle_result_request(req['info'], req['param'])
                else:
                    logging.error('Unknown action received from Checker Script for team %d in tick %d: %s',
                                  req['info']['team'], req['info']['tick'], req['action'])
                    # We can't signal an error to the Checker Script (which might be waiting for a response),
                    # so our only option is to kill it
                    self.supervisor.terminate_runner(req['runner_id'])
                    send_resp = False
            except:    # noqa, pylint: disable=bare-except
                logging.exception('Checker Script communication error for team %d in tick %d:',
                                  req['info']['team'], req['info']['tick'])
                self.supervisor.terminate_runner(req['runner_id'])
            else:
                if send_resp:
                    req['send'].send(resp)

        if not self.shutting_down:
            # Launch new tasks and catch up missed intervals
            while get_monotonic_time() - self.last_launch >= self.interval:
                self.last_launch += self.interval
                self.launch_tasks()

        return req is not None

    def handle_flag_request(self, task_info, params):
        try:
            payload = base64.b64decode(params['payload'])
        except KeyError:
            payload = None

        if payload == b'':
            payload = None
        expiration = datetime.datetime.utcnow() + (self.tick_duration * self.flag_valid_ticks)

        return flag_lib.generate(task_info['team'], self.service['id'], self.flag_secret, payload,
                                 expiration.timestamp())

    def handle_load_request(self, task_info, param):
        return database.load_state(self.state_db_conn, self.service['id'], task_info['team'], param)

    def handle_store_request(self, task_info, params):
        database.store_state(self.state_db_conn, self.service['id'], task_info['team'], params['key'],
                             params['data'])

    def handle_result_request(self, task_info, param):
        try:
            result = int(param)
        except ValueError:
            logging.error('Invalid result from Checker Script for team %d in tick %d: %s',
                          task_info['team'], task_info['tick'], param)
            return

        try:
            check_result = CheckResult(result)
        except ValueError:
            logging.error('Invalid result from Checker Script for team %d in tick %d: %d',
                          task_info['team'], task_info['tick'], result)
            return

        logging.info('Result from Checker Script for team %d in tick %d: %d', task_info['team'],
                     task_info['tick'], check_result.value)
        database.commit_result(self.game_db_conn, self.service['id'], task_info['team'], task_info['tick'],
                               result)

    def launch_tasks(self):
        current_tick = database.get_current_tick(self.game_db_conn)

        if current_tick < 0:
            # Competition not running yet
            return

        if current_tick != self.known_tick:
            self.supervisor.terminate_runners()
            self.update_launch_params()
            self.known_tick = current_tick

        tasks = database.get_new_tasks(self.game_db_conn, self.service['id'], self.tasks_per_launch)
        for task in tasks:
            ip = self.ip_pattern % task['team_id']
            runner_args = [self.checker_script, ip, str(task['team_id']), str(task['tick'])]
            if self.sudo_user is not None:
                runner_args = ['sudo', '--user='+self.sudo_user, '--preserve-env=PATH,CTF_CHECKERSCRIPT',
                               '--close-from=5', '--'] + runner_args

            # Information in task_info should be somewhat human-readable, because it also ends up in Checker
            # Script logs
            task_info = {'service': self.service['name'],
                         'team': task['team_id'],
                         'tick': current_tick}
            logging.info('Starting Checker Script for team %d in tick %d', task['team_id'], current_tick)
            self.supervisor.start_runner(runner_args, task_info, self.logging_params)

    def update_launch_params(self):
        """
        Determines the number of Checker tasks to start per launch.
        Our goal here is to balance the load over a tick with some smearing (to make Checker fingerprinting
        more difficult), while also ensuring that all teams get checked in every tick.
        This simple implementation distributes the start of tasks evenly across the available time with some
        safety margin at the end.
        """
        total_tasks = database.get_task_count(self.game_db_conn, self.service['id'])
        local_tasks = math.ceil(total_tasks / self.checker_count)

        margin_seconds = self.tick_duration.total_seconds() / 6
        launch_timeframe = self.tick_duration.total_seconds() - self.max_check_duration - margin_seconds
        if launch_timeframe <= 0:
            raise ValueError('Maximum Checker Script duration too long for tick')

        intervals_per_timeframe = math.floor(launch_timeframe / self.interval)
        self.tasks_per_launch = math.ceil(local_tasks / intervals_per_timeframe)

    def get_running_script_count(self):
        return len(self.supervisor.processes)


def get_monotonic_time():
    """
    Wrapper around time.monotonic() to enables mocking in test cases. Globally mocking time.monotonic()
    breaks library code (e.g. multiprocessing in RunnerSupervisor).
    """

    return time.monotonic()
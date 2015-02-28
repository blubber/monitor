'''
    Monitor
    ~~~~~~~
    Monitor power and door related events.
'''

import argparse
import atexit
import datetime
import json
import logging, logging.config, logging.handlers
import os, os.path
import Queue
import sys
import threading
import time
import urllib2

import serial

# Number of push attempts before giving up.
NATTEMPTS = 5

# Events
EVENT_CODES = {
     0: 'NOOP',
    10: 'DOOR_CLOSING',
    11: 'DOOR_OPENING',
    20: 'ENERGY_PULSE',
}

logging.basicConfig(level='ERROR',
                    format='%(asctime)s [%(levelname)8s] :: %(message)s')
logger = logging.getLogger('monitor')
door_logger = logging.getLogger('door')
queue = Queue.Queue()
stopping = threading.Event()


class SMTPHandler (logging.handlers.SMTPHandler):

    def __init__(self, mailhost, fromaddr, toaddrs, subject,
                 credentials=None, secure=None):
        if secure is True:
            secure = tuple()
        super(SMTPHandler, self).__init__(mailhost, fromaddr, toaddrs,
                                          subject, credentials, secure)
        self.subject = subject

    def getSubject(self, record):
        now = datetime.datetime.now()
        return now.strftime(self.subject)

logging.handlers.CustomSMTPHandler = SMTPHandler


def put_pulse(pulses=None):
    ''' Add one or pulse to the global queue. '''
    if pulses is None:
        pulses = [int(10 * time.time())]
    elif not isinstance(pulses, (list, tuple)):
        pulses = [pulses]

    logger.info('Add %d pulses queue', len(pulses))

    for pulse in pulses:
        queue.put(pulse)


def purge_queue():
    ''' Remove all pulses from the queue and return them. '''
    pulses = []
    try:
        while True:
            pulses.append(queue.get_nowait())
    except Queue.Empty:
        pass
    return pulses


def flush_pulses(url, pulses):
    ''' Push pulses to the API.
    '''
    logger.info('Pushing %d pulses to %s.', len(pulses), url)
    if len(pulses) == 0:
        return

    payload = json.dumps(pulses)
    headers = {'Content-Type': 'application/json'}
    req = urllib2.Request(url, payload, headers=headers)
    resp = urllib2.urlopen(req)

    return resp


def push_thread(url):
    attempt = 0
    sleep = 0

    while not stopping.is_set():
        if sleep > 0:
            time.sleep(0.5)
            sleep -= 0.5
            continue

        if queue.qsize() == 0:
            time.sleep(0.5)
            continue

        pulses = purge_queue()

        try:
            flush_pulses(url, pulses)
            attempt = 0
        except (urllib2.URLError, urllib2.HTTPError) as ex:
            put_pulse(pulses)

            if attempt == NATTEMPTS - 1:
                logger.exception('Exception during push, giving up.')
                stopping.set()
            else:
                attempt += 1
                sleep = 2**(attempt + 2)
                logger.info('Exception %s during push, sleeping for %ds',
                            str(ex), sleep)
        except Exception:
            logger.exception('Fatal exception during push')
            put_pulse(pulses)
            stopping.set()


def format_door_message(events):
    ''' Format a door logging message. '''
    fmt = '{:8s}  {:12s}  {}'
    lines = []

    for i, (event, at) in enumerate(events):
        if i == 0:
            lasted = ''
        else:
            elapsed = at - events[i-1][1]
            lasted = '{:4.1f}s'.format(elapsed.total_seconds())

        if event == 'DOOR_OPENING':
            event = 'open'
        else:
            event = 'dicht'

        lines.append(fmt.format(at.strftime('%H:%M:%S'), event, lasted))

    return '\n\n{}'.format('\n'.join(lines))


def loop(fp, door_timeout):
    ''' Loop until an error occurs or `stopping` is set. '''
    door_events = []

    while not stopping.is_set():
        if len(door_events) > 0:
            now = datetime.datetime.now()
            delta = now - door_events[-1][1]

            if delta > door_timeout:
                logger.info('Door event timeout occured with %d events.',
                            len(door_events))

                message = format_door_message(door_events)
                door_logger.log(100, message)
                logger.info(message)
                door_events = []

        data = fp.read(1)

        if data == '':
            time.sleep(0.01)
            continue

        try:
            event_code = ord(data)
        except Exception as ex:
            logger.exception('Received `%s`', data)

        event = EVENT_CODES.get(event_code)

        if event is None:
            logger.error('Unknown event code received: `%d`', event_code)
            continue

        logger.debug('Received event `%s`', event)

        if event == 'ENERGY_PULSE':
            put_pulse()

        elif event.startswith('DOOR'):
            door_events.append((event, datetime.datetime.now()))


def install_auth_opener(url, username, password):
    ''' Installs a basic auth opener for the pusher if required. '''
    logger.info('Installing basic auth opener.')

    password_mgr = urllib2.HTTPPasswordMgrWithDefaultRealm()
    password_mgr.add_password(None, url, username, password)

    handler = urllib2.HTTPBasicAuthHandler(password_mgr)
    opener = urllib2.build_opener(handler)
    urllib2.install_opener(opener)


def slurp(fp):
    ''' Read bytes from `fp` until no more are available within ms. '''
    last = time.time()
    count = 0
    while True:
        data = fp.read()
        if data == '':
            break

        now = time.time()
        if now - last > 0.01:
            break
        last = now
        count += 1

    logger.info('Slurped %d bytes from fp', count)


def watch():
    filename = os.path.abspath(__file__)
    mtime = os.stat(filename).st_mtime

    while not stopping.is_set():
        time.sleep(5)
        new_mtime = os.stat(filename).st_mtime
        if not new_mtime == mtime:
            logger.info('Reload by stat.')
            stopping.set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-P', '--push',
        help='Speciy a URL to push pulses to.')
    parser.add_argument('-B', '--backlog', default='backlog.txt',
        help='Backlog filename.')
    parser.add_argument('-L', '--logging-config',
        help='Logging configuration file.')
    parser.add_argument('-F', '--faux-serial', default=False,
        action='store_true', help='Open serial port as file.')
    parser.add_argument('-W', '--watch', default=False, action='store_true',
        help='Watch monitor script for modifications and reload.')
    parser.add_argument('-D', type=int, default=180, help='Door timeout.')
    parser.add_argument('-A', '--auth', help='Basic auth username:password')
    parser.add_argument('serial_port', nargs='+',
        help='One or more serial ports.')

    args = parser.parse_args()

    logging.addLevelName(100, 'DOOR')

    if args.logging_config:
        logging.config.fileConfig(args.logging_config)

    logger.warning('Monitor starting up.')

    fp = None
    for path in args.serial_port:
        if not os.path.exists(path):
            continue

        if args.faux_serial:
            logger.info('Opening %s as fake serial port.', path)
            fp = open(path, 'r')
            break
        else:
            logger.info('Opening %s as serial port', path)
            fp = serial.Serial(path, 9600, timeout=0.5)
            slurp(fp)
            break

    if fp is None:
        logger.critical('No serial port found to open.')
        return -3

    errno = -2

    if args.push and args.auth:
        username, password = args.auth.split(':')
        install_auth_opener(args.push, username, password)

    if args.push:
        logger.info('Starting push thread for %s', args.push)
        thread = threading.Thread(target=push_thread, args=(args.push,))
        thread.daemon = True
        thread.start()
    else:
        logger.warning('No push URL specified.')
        thread = None

    if args.watch:
        logger.info('Starting modification monitor.')
        watch_thread = threading.Thread(target=watch)
        watch_thread.daemon = True
        watch_thread.start()

    if args.backlog and os.path.isfile(args.backlog):
        logger.info('Reading pulses from backlog %s',
                    args.backlog)
        with open(args.backlog) as bfp:
            pulses = [int(l.strip()) for l in bfp if l.strip() != '']
            logger.debug('Pushing %d backlog pulses.', len(pulses))
            if len(pulses) > 0:
                put_pulse(pulses)

    try:
        loop(fp, datetime.timedelta(seconds=args.D))
        logger.info('Loop() returned')
    except KeyboardInterrupt:
        logger.info('Gracefully shutting down.')
    except:
        logger.exception('An exception occured in the main loop.')
    else:
        errno = 0
    finally:
        stopping.set()

    try:
        fp.close()
    except:
        pass

    if thread:
        logger.debug('Join pusher thread.')
        thread.join()

    if args.backlog:
        # Truncating the current backlog on purpose.
        pulses = purge_queue()
        logger.info('Writing %d pulses to backlog.', len(pulses))
        with open(args.backlog, 'w') as fp:
            fp.write('\n'.join(map(str, pulses)))
            fp.write('\n')

    return errno

if __name__ == '__main__':
    sys.exit(main())

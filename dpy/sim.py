import os
import abc
import sys
import time
import queue
import signal
import random
import logging
import threading
import traceback
import multiprocessing

from . import pattern
from .common import Null

class DistProcess(multiprocessing.Process):
    """Abstract base class for DistAlgo processes.

    Each instance of this class enbodies the runtime activities of a DistAlgo
    process in a distributed system. Each process is uniquely identified by a
    two-ary tuple (address, port), where 'address' is the name or IP of the
    host machine and 'port' is an integer corresponding to the port number on
    which this process listens for incoming messages from other DistAlgo
    processes. Messages exchanged between DistAlgo processes are instances of
    `DistMessage`.

    DistAlgo processes can spawn more processes by calling `createprocs()`.
    The domain of `DistProcess` instances are flat, in the sense that all
    processes are created "equal" -- no parent-child relationship is
    maintained. Any DistProcess can send messages to any other DistProcess,
    given that it knows the unique id of the target process. However, the
    terminal is shared between all processes spawned from that terminal. This
    includes the stdout, stdin, and stderr streams. In addition, each
    DistProcess also maintains a TCP connection to the master control node
    (the first node started in a distributed system) where DistAlgo commands
    are passed (see `distalgo.runtime.proto`).

    Concrete subclasses of `DistProcess` must define the functions:

    - `setup`: A function that initializes the process-local variables.

    - `main`: The entry point of the process. This function defines the
      activities of the process.

    Users should not instantiate this class directly, process instances should
    be created by calling `createprocs()`.

    """

    class Comm(threading.Thread):
        """The background communications thread.

        Creates an event object for each incoming message, and appends the
        event object to the main process' event queue.
        """

        def __init__(self, parent):
            threading.Thread.__init__(self)
            self._parent = parent

        def run(self):
            try:
                for msg in self._parent._recvmesgs():
                    (src, clock, data) = msg
                    e = pattern.ReceivedEvent(
                            message=data,
                            timestamp=clock,
                            source=src,
                            destination=None)
                    self._parent._eventq.put(e)
            except KeyboardInterrupt:
                pass

    def __init__(self, parent, initpipe, channel, name=None):
        multiprocessing.Process.__init__(self)

        self._running = False
        self._channel = channel

        self._logical_clock = 0

        self._events = []
        self._received_q = []
        self._jobqueue = []
        self._timer = None
        self._timer_expired = False
        self._failures = {'send': 0,
                          'receive': 0,
                          'crash': 0}
        self._evtimeout = None

        # Performance counters:
        self._usrtime_st = 0
        self._systime_st = 0
        self._waltime_st = 0
        self._usrtime = 0
        self._systime = 0
        self._waltime = 0
        self._is_timer_running = False

        self._dp_name = name
        self._log = None

        self._parent = parent
        self._initpipe = initpipe
        self._child_procs = []

    def _wait_for_go(self):
        self._initpipe.send(self._id)
        while True:
            act = self._initpipe.recv()

            if act == "start":
                self._running = True
                del self._initpipe
                return
            else:
                inst, args = act
                if inst == "setup":
                    self.setup(*args)
                else:
                    m = getattr(self, "set_" + inst)
                    m(*args)

    def _start_comm_thread(self):
        self._eventq = queue.Queue()
        self._comm = DistProcess.Comm(self)
        self._comm.daemon =True
        self._comm.start()

    def _sighandler(self, signum, frame):
        for cpid, _ in self._child_procs:
            os.kill(cpid, signal.SIGTERM)
        sys.exit(0)

    def run(self):
        try:
            signal.signal(signal.SIGTERM, self._sighandler)

            self._id = self._channel(self._dp_name)
            self._log = logging.getLogger(str(self))
            self._start_comm_thread()

            self._wait_for_go()

            result = self.main()

            self.report_times()

        except Exception as e:
            sys.stderr.write("Unexpected error at process %s:%r"% (str(self), e))
            traceback.print_tb(e.__traceback__)

        except KeyboardInterrupt as e:
            self._log.debug("Received KeyboardInterrupt, exiting")
            pass

    def start_timers(self):
        if not self._is_timer_running:
            self._usrtime_st, self._systime_st, _, _, _ = os.times()
            self._waltime_st = time.clock()
            self._is_timer_running = True

    def stop_timers(self):
        if self._is_timer_running:
            usrtime, systime, _, _, _ = os.times()
            self._usrtime += usrtime - self._usrtime_st
            self._systime += systime - self._systime_st
            self._waltime += time.clock() - self._waltime_st
            self._is_timer_running = False

    def report_times(self):
        self._parent.send(('totalusrtime', self._usrtime), self._id)
        self._parent.send(('totalsystime', self._systime), self._id)
        self._parent.send(('totaltime', self._waltime), self._id)

    def report_mem(self):
        import pympler.asizeof
        memusage = pympler.asizeof.asizeof(self) / 1024
        self._parent.send(('mem', memusage), self._id)

    def exit(self, code):
        raise SystemExit(10)

    def output(self, message, level=logging.INFO):
        self._log.log(level, message)

    def purge_received(self):
        for attr in dir(self):
            if attr.startswith("_receive_messages_"):
                setattr(self, attr, [])

    def purge_sent(self):
        for attr in dir(self):
            if attr.startswith("_sent_messages_"):
                setattr(self, attr, [])

    def spawn(self, pcls, args):
        """Spawns a child process"""

        childp, ownp = multiprocessing.Pipe()
        p = pcls(self._id, childp, self._channel)
        p.start()

        childp.close()
        cid = ownp.recv()
        ownp.send(("setup", args))
        ownp.send("start")

        #self._child_procs.append((p.pid, cid))

        return cid

    # Wrapper functions for message passing:
    def _send(self, data, to):
        self.incr_logical_clock()
        if (self._fails('send')):
            return False

        result = True
        if (hasattr(to, '__iter__')):
            for t in to:
                r = t.send(data, self._id, self._logical_clock)
                if not r: result = False
        else:
            result = to.send(data, self._id, self._logical_clock)

        self._log.debug("Sent %s -> %r"%(str(data), to))
        self._trigger_event(pattern.Event(pattern.SentEvent, self._id,
                                          self._logical_clock,data))
        self._parent.send(('sent', 1), self._id)
        return result

    def _recvmesgs(self):
        for mesg in self._id.recvmesgs():
            if not (self._fails('receive')):
                yield mesg

    def _timer_start(self):
        self._timer = time.time()
        self._timer_expired = False

    def _timer_end(self):
        self._timer = None

    def _fails(self, failtype):
        if not failtype in self._failures.keys():
            return False
        if (random.randint(0, 100) < self._failures[failtype]):
            return True
        return False

    def _label(self, name, block=False, timeout=None):
        """This simulates the controlled "label" mechanism.

        Currently we simply handle one event on one label call.

        """
        # Handle performance timers first:
        if name == "start":
            self.start_timers()
        elif name == "end":
            self.stop_timers()
        if (self._fails('crash')):
            self.output("Stuck in label: %s" % name)
            self.exit(10)

        if timeout is not None:
            if self._timer is None:
                self._timer_start()
            timeleft = timeout - (time.time() - self._timer)
            if timeleft <= 0:
                self._timer_end()
                self._timer_expired = True
                return
        else:
            timeleft = None
        self._process_event(block, timeleft)
        self._process_jobqueue(name)

    def _process_jobqueue(self, label=None):
        newq = []
        for handler, args in self._jobqueue:
            if ((handler._labels is None or label in handler._labels) and
                (handler._notlabels is None or label not in handler._notlabels)):
                try:
                    handler(**args)
                except TypeError as e:
                    self._log.warn("Insufficient bindings to call handler:", e)
            else:
                newq.append((handler, args))
        self._jobqueue = newq

    def _process_event(self, block, timeout=None):
        """Retrieves one message, then process the backlog event queue.

        Parameter 'block' indicates whether to block waiting for next message
        to come in if the queue is currently empty. 'timeout' is the maximum
        time to wait for an event.

        """
        if timeout is not None and timeout < 0:
            timeout = 0
        try:
            event = self._eventq.get(block, timeout)
            self._logical_clock = max(self._logical_clock, event.timestamp) + 1
            self._trigger_event(event)
        except queue.Empty:
            return
        except Exception as e:
            self._log.error("Caught exception while waiting for events: %r", e)
            return

    def _trigger_event(self, event):
        """Immediately triggers 'event', skipping the event queue.

        """

        self._log.debug("triggering event %s%r%r%r%r" %
                        (type(event).__name__,
                         event.message, event.timestamp,
                         event.destination, event.source))
        for p in self._events:
            bindings = dict()
            if (p.match(event, bindings=bindings,
                        ignore_bound_vars=True, **self.__dict__)):
                if p.record_history is True:
                    getattr(self, p.name).append(event.to_tuple())
                elif p.record_history is not None:
                    # Call the update stub:
                    p.record_history(getattr(self, p.name), event.to_tuple())
                for h in p.handlers:
                    self._jobqueue.append((h, bindings))

    def _forever_message_loop(self):
        while (True):
            self._process_event(self._events, True)

    def _has_received(self, mess):
        try:
            self._received_q.remove(mess)
            return True
        except ValueError:
            return False

    def __str__(self):
        s = self.__class__.__name__
        if self._dp_name is not None:
            s += "(" + self._dp_name + ")"
        else:
            s += "(" + str(self._id) + ")"
        return s

    def work(self):
        """Waste some random amount of time."""
        time.sleep(random.randint(0, 200) / 100)
        pass

    def logical_clock(self):
        """Returns the current value of Lamport clock."""
        return self._logical_clock

    def incr_logical_clock(self):
        """Increment Lamport clock by 1."""
        self._logical_clock += 1


    ### Various attribute setters:
    def set_send_fail_rate(self, rate):
        self._failures['send'] = rate

    def set_receive_fail_rate(self, rate):
        self._failures['receive'] = rate

    def set_crash_rate(self, rate):
        self._failures['crash'] = rate

    def set_event_timeout(self, time):
        self._evtimeout = time

    def set_name(self, name):
        self._dp_name = name

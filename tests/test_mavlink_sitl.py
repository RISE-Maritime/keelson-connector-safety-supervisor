"""The SITL drone's key switch and stop over the supervisor's own MAVLink link (crowsnest-dev#268), in
process: the RC input source, the force-disarm actuator and the link's message handling, with fake
messages -- no socket, no autopilot."""

import threading
import time

from safety_supervisor.actuation import MavlinkDisarmActuator, ThreadedActuator
from safety_supervisor.inputs import MavlinkRcInputs
from safety_supervisor.mavlink_link import FORCE_DISARM_MAGIC, MavlinkLink


class Msg:
    def __init__(self, kind, src=(1, 1), **fields):
        self._kind, self._src = kind, src
        self.__dict__.update(fields)

    def get_type(self):
        return self._kind

    def get_srcSystem(self):
        return self._src[0]

    def get_srcComponent(self):
        return self._src[1]


def heartbeat(armed, autopilot=3):
    return Msg("HEARTBEAT", autopilot=autopilot, base_mode=128 if armed else 0)


def rc(ch9, chancount=16):
    return Msg("RC_CHANNELS", chancount=chancount, chan9_raw=ch9)


# ---------------------------------------------------------------------------- the key switch


def test_the_key_is_rc9_at_or_above_the_threshold_and_labelled_simulated():
    k = MavlinkRcInputs(channel=9, high_us=1700)
    assert k.read(0).lockout is False and k.read(0).simulated is True  # nothing heard: no claim of a lockout
    k.on_rc(1699, 0)
    assert k.read(0).lockout is False
    k.on_rc(1700, 0)
    r = k.read(0)
    assert r.lockout is True and r.simulated is True and r.physical_stop is False


def test_silence_never_releases_the_key():
    k = MavlinkRcInputs(ttl_ms=1000)
    k.on_rc(2000, 0)
    assert k.read(5000).lockout is True and k.stale(5000)
    assert k.read(60_000).lockout is True


def test_the_link_reports_rc9_and_never_a_no_rc_value():
    got = []
    link = MavlinkLink("tcp:nowhere", on_rc=got.append, on_armed=lambda a: None)
    link.handle(rc(2000))
    link.handle(rc(0))  # "unused"
    link.handle(rc(65535))  # "unused"
    link.handle(rc(1000, chancount=8))  # RC9 not present before any RC input: no reading
    link.handle(rc(1000))
    assert got == [2000, 1000]


def test_the_link_reports_the_vehicles_armed_flag_and_ignores_a_gcs():
    armed = []
    link = MavlinkLink("tcp:nowhere", on_rc=lambda p: None, on_armed=armed.append)
    link.handle(heartbeat(True, autopilot=8))  # MAV_AUTOPILOT_INVALID: a GCS, not her
    link.handle(heartbeat(True))
    link.handle(heartbeat(False))
    assert armed == [True, False]


# ---------------------------------------------------------------------------- the stop


class FakeConn:
    def __init__(self, link, result=0, answer=True):
        self.sent, self._link, self._result, self._answer = [], link, result, answer
        self.mav = self

    def command_long_send(self, sysid, compid, command, confirmation, p1, p2, *rest):
        self.sent.append((command, p1, p2))
        if self._answer and command == 400:
            threading.Thread(target=lambda: self._link.handle(Msg("COMMAND_ACK", command=400, result=self._result)), daemon=True).start()


def _linked(result=0, answer=True):
    link = MavlinkLink("tcp:nowhere", on_rc=lambda p: None, on_armed=lambda a: None)
    link.handle(heartbeat(True))  # learns her system and component
    link._conn = FakeConn(link, result, answer)  # noqa: SLF001 - the test stands in for the socket
    return link


def test_force_disarm_is_a_forced_disarm_and_waits_for_her_ack():
    link = _linked(result=0)
    assert link.force_disarm(1.0) == (True, "force-disarm MAV_RESULT 0")
    assert link._conn.sent == [(400, 0, FORCE_DISARM_MAGIC)]  # noqa: SLF001


def test_a_refused_or_unanswered_disarm_is_not_a_stop():
    assert link_result(_linked(result=4)) is False
    t0 = time.time()
    accepted, detail = _linked(answer=False).force_disarm(0.3)
    assert accepted is False and "no COMMAND_ACK" in detail and time.time() - t0 < 1.0
    assert MavlinkLink("tcp:nowhere", on_rc=lambda p: None, on_armed=lambda a: None).force_disarm(0.1)[0] is False  # no link


def link_result(link):
    return link.force_disarm(1.0)[0]


def test_the_mavlink_actuator_is_for_the_test_profile_with_its_own_link_only():
    assert MavlinkDisarmActuator.guard("test", True) is None
    assert "test profile only" in MavlinkDisarmActuator.guard("operational", True)
    assert "test profile only" in MavlinkDisarmActuator.guard(None, True)
    assert "--sim-mavlink" in MavlinkDisarmActuator.guard("test", False)


def test_the_mavlink_actuator_disarms_her_until_accepted_and_a_release_calls_nothing():
    answers = [(False, "no ack"), (True, "MAV_RESULT 0")]
    a = MavlinkDisarmActuator(lambda: answers.pop(0))
    a.apply(True, "lockout")
    a.apply(True, "lockout")  # retried: the first did not land
    a.apply(True, "lockout")  # landed: not called again
    assert a.calls == 2
    a.apply(False, "")
    assert a.calls == 2  # a release never arms her


def test_armed_over_the_own_link_while_locked_out_she_is_disarmed_again_and_it_alarms():
    calls, alarms = [], []
    act = ThreadedActuator(MavlinkDisarmActuator(lambda: calls.append(1) or (True, "ok")), on_rearmed=alarms.append)
    act.apply(True, "lockout")
    act.run_once()
    assert len(calls) == 1
    act.vessel_latch(True)  # disarmed: the stop held
    act.run_once()
    assert len(calls) == 1
    act.vessel_latch(False)  # armed again while the supervisor inhibits
    act.run_once()
    assert len(calls) == 2 and alarms == ["lockout"]

"""The SITL drone's key switch and stop over the supervisor's OWN MAVLink link (crowsnest-dev#268), end to
end: the CLI as a real process, on the isolated loopback Zenoh pair of test_e2e.py, against a FAKE
autopilot that pymavlink serves on loopback (`tcpin`). It streams HEARTBEAT (her armed flag) and
RC_CHANNELS (RC9: the key), and answers a forced disarm. Nothing here reaches a network or a SITL; the
real SITL is the HIL suite's (keelson-platforms/hil, the `sitl` marker)."""

import threading
import time

import pytest
from keelson.payloads.SafetyState_pb2 import EmergencyCommand, SafetyCause, SafetyRequestOutcome, SafetyState

from tests.test_e2e import BASE, Harness, free_port

mavutil = pytest.importorskip("pymavlink.mavutil")

pytestmark = pytest.mark.e2e


class FakeAutopilot:
    """One loopback MAVLink peer standing in for ArduPilot: sysid 1, a rover."""

    def __init__(self):
        self.port = free_port()
        self.armed, self.key_us = True, 1000
        self.disarms = self.arms = 0
        self._stop = threading.Event()
        self._conn = mavutil.mavlink_connection(f"tcpin:127.0.0.1:{self.port}", source_system=1, source_component=1)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        m = self._conn.mav
        last = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last >= 0.1:
                last = now
                try:
                    m.heartbeat_send(10, 3, 128 if self.armed else 0, 0, 0)  # GROUND_ROVER, ARDUPILOTMEGA
                    m.rc_channels_send(int(now * 1000) & 0xFFFFFFFF, 16, *([1500] * 8), self.key_us, *([1500] * 9), 255)
                except OSError:
                    pass  # no client yet
            msg = self._conn.recv_match(type="COMMAND_LONG", blocking=True, timeout=0.05)
            if msg is not None and msg.command == 400:
                if int(msg.param1) == 0:
                    self.disarms += 1
                    self.armed = False
                else:
                    self.arms += 1
                    self.armed = True
                m.command_ack_send(400, 0)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)
        self._conn.close()


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


@pytest.fixture
def ap():
    a = FakeAutopilot()
    yield a
    a.close()


def own_link(ap):
    return ("--accept-policy", "--sim-mavlink", f"tcp:127.0.0.1:{ap.port}", "--actuator", "mavlink",
            "--recover-locally", "engineer-on-board")


def test_her_key_switch_locks_her_out_labelled_simulated_and_disarms_her(h, ap):
    h.start(*own_link(ap))
    h.wait(lambda: ap.disarms >= 1, what="the recovery's STOPPED to disarm her over the own link")
    ap.armed = True  # somebody armed her; the key turns
    ap.key_us = 2000
    h.wait(lambda: h.states and h.states[-1].state == SafetyState.SAFETY_STATE_LOCKED_OUT, what="LOCKED_OUT")
    st = h.states[-1]
    assert st.local_lockout and st.lockout_simulated
    h.wait(lambda: not ap.armed, what="her disarmed while locked out")


def test_armed_while_locked_out_she_is_disarmed_again_and_it_alarms(h, ap):
    alarms = []
    sub = h.session.declare_subscriber(f"{BASE}/alarm/**", lambda s: alarms.append(1))
    try:
        ap.key_us = 2000
        h.start(*own_link(ap))
        h.wait(lambda: h.states and h.states[-1].state == SafetyState.SAFETY_STATE_LOCKED_OUT, what="LOCKED_OUT")
        h.wait(lambda: not ap.armed, what="the first disarm")
        before = ap.disarms
        ap.armed = True  # armed directly while the lockout stands
        h.wait(lambda: ap.disarms > before and not ap.armed, what="her second disarm")
        h.wait(lambda: alarms, what="the alarm")
    finally:
        sub.undeclare()


def test_a_stop_during_the_lockout_is_accepted_and_clearing_the_key_rearms_nothing(h, ap):
    ap.key_us = 2000
    h.start(*own_link(ap))
    h.wait(lambda: h.states and h.states[-1].state == SafetyState.SAFETY_STATE_LOCKED_OUT, what="LOCKED_OUT")
    r = h.request(EmergencyCommand.EMERGENCY_COMMAND_STOP)
    assert r.outcome == SafetyRequestOutcome.SAFETY_REQUEST_OUTCOME_ACCEPTED
    ap.key_us = 1000  # the key turned back, locally
    h.wait(lambda: not h.states[-1].local_lockout, what="the lockout cleared")
    st = h.states[-1]
    assert SafetyCause.Type.CAUSE_TYPE_LOCKOUT not in [c.type for c in st.active_causes]
    assert st.state != SafetyState.SAFETY_STATE_CLEAR  # clearing the key re-arms nothing: still stopped
    time.sleep(1.0)
    assert ap.arms == 0 and not ap.armed


def test_the_mavlink_actuator_without_its_own_link_is_refused(h, tmp_path):
    import json
    import subprocess
    import sys

    from tests.test_e2e import REALM, ROOT, V

    policy = tmp_path / "test.json"
    policy.write_text(json.dumps({"config_revision": 1, "command_profile": "test"}))
    p = subprocess.run([sys.executable, str(ROOT / "bin/safety-supervisor2keelson.py"), "-r", REALM, "-e", V,
                        "--state-dir", str(tmp_path / "s"), "--policy", str(policy), "--accept-policy", "--actuator", "mavlink",
                        "--mode", "peer", "--connect", f"tcp/127.0.0.1:{h.port}"],
                       capture_output=True, text=True, timeout=30, check=False)
    assert p.returncode == 2 and "--sim-mavlink" in (p.stdout + p.stderr)

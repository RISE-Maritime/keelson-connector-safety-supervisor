"""The card's bench steps in software (crowsnest-dev#277): the CLI as a real process, on the isolated
loopback Zenoh pair of test_e2e.py, reading a FAKE Modbus device on loopback."""

import json
import subprocess
import sys
import time

import pytest
from keelson.payloads.SafetyState_pb2 import SafetyCause, SafetyState

from tests.test_e2e import REALM, ROOT, V, Harness, _reset_by_the_conn
from tests.test_hw_inputs import FakeModbus

pytestmark = pytest.mark.e2e

STOP, LOCKOUT = (2, 0), (2, 1)
RECOVER = ("--accept-policy", "--recover-locally", "engineer-on-board")
PHYSICAL = SafetyCause.CAUSE_TYPE_PHYSICAL_STOP
STOPPED, CLEAR, LOCKED = SafetyState.SAFETY_STATE_STOPPED, SafetyState.SAFETY_STATE_CLEAR, SafetyState.SAFETY_STATE_LOCKED_OUT


@pytest.fixture
def rig(tmp_path):
    h = Harness(tmp_path)
    dev = FakeModbus()
    dev.values.update({STOP: 1, LOCKOUT: 1})
    cfg = tmp_path / "hw.json"
    cfg.write_text(json.dumps({
        "host": "127.0.0.1", "port": dev.port, "poll_ms": 20, "timeout_ms": 50, "fault_ms": 200, "release_ms": 40, "boot_wait_ms": 3000,
        "physical_stop": {"table": "discrete", "address": 0, "released": True},
        "lockout": {"table": "discrete", "address": 1, "released": True},
    }))
    yield h, dev, cfg
    h.close()
    dev.close()


def state_is(h, state):
    h.wait(lambda: h.states and h.states[-1].state == state, what=f"state {SafetyState.State.Name(state)}")
    return h.states[-1]


def clear(h):
    state_is(h, STOPPED)
    _reset_by_the_conn(h)
    return state_is(h, CLEAR)


def physical(st):
    return [c for c in st.active_causes if c.type == PHYSICAL]


def settled(h):
    """A heartbeat later: what the state is once nothing else is on its way."""
    n = len(h.states)
    h.wait(lambda: len(h.states) > n, what="a heartbeat")
    return h.states[-1]


def test_pressing_latches_and_releasing_does_not_clear_it_without_a_reset(rig):
    h, dev, cfg = rig
    h.start("--hw-inputs", str(cfg), *RECOVER)
    clear(h)

    dev.values[STOP] = 0
    st = state_is(h, STOPPED)
    assert physical(st) and not physical(st)[0].clearable_remotely

    _reset_by_the_conn(h)
    assert settled(h).state == STOPPED  # no RESET clears it while pressed

    dev.values[STOP] = 1
    h.wait(lambda: physical(h.states[-1])[0].clearable_remotely, what="the cause to become clearable")
    assert settled(h).state == STOPPED  # released, and still stopped

    _reset_by_the_conn(h)
    assert state_is(h, CLEAR).active_causes == []


def test_a_pulled_wire_stops_her(rig):
    """Normally closed: the loop going open is the same reading as a press."""
    h, dev, cfg = rig
    h.start("--hw-inputs", str(cfg), *RECOVER)
    clear(h)
    dev.values[STOP] = 0
    assert physical(state_is(h, STOPPED))[0].source == "physical-stop"


def test_a_lost_device_stops_her_and_a_reset_waits_for_it(rig):
    h, dev, cfg = rig
    h.start("--hw-inputs", str(cfg), *RECOVER)
    clear(h)

    dev.mode = "silent"
    st = state_is(h, STOPPED)
    assert physical(st)[0].source == "physical-stop-input-fault"
    assert not st.local_lockout  # nobody turned a key

    _reset_by_the_conn(h)
    assert settled(h).state == STOPPED

    dev.mode = "ok"
    h.wait(lambda: physical(h.states[-1])[0].clearable_remotely, what="the input to read again")
    assert h.states[-1].state == STOPPED  # back, and still stopped
    _reset_by_the_conn(h)
    state_is(h, CLEAR)


def test_a_device_that_never_answers_boots_stopped_and_locked_out(rig):
    h, dev, cfg = rig
    dev.mode = "silent"
    c = json.loads(cfg.read_text())
    cfg.write_text(json.dumps({**c, "boot_wait_ms": 100}))
    h.start("--hw-inputs", str(cfg), *RECOVER)
    st = state_is(h, LOCKED)
    assert st.local_lockout and physical(st)[0].source == "physical-stop-input-fault"


def test_a_normal_start_latches_nothing_from_the_input(rig):
    h, _dev, cfg = rig
    h.start("--hw-inputs", str(cfg), *RECOVER)
    st = state_is(h, STOPPED)  # local recovery's own STOPPED
    time.sleep(0.2)
    assert physical(h.states[-1]) == [] and physical(st) == []


def test_the_lockout_input_sets_local_lockout_and_it_is_not_simulated(rig):
    h, dev, cfg = rig
    h.policy_extra = {"command_profile": "operational", "manager": {"issuer_id": "authority-manager/0", "source_id": "authority-manager"}}
    h.start("--hw-inputs", str(cfg), *RECOVER)
    dev.values[LOCKOUT] = 0
    st = state_is(h, LOCKED)
    assert st.local_lockout and not st.lockout_simulated

    dev.values[LOCKOUT] = 1
    h.wait(lambda: not h.states[-1].local_lockout, what="the key turned back")
    assert h.states[-1].state != CLEAR  # clearing it re-arms nothing


def test_the_operational_profile_with_no_real_inputs_says_so_and_still_runs(rig):
    h, _dev, _cfg = rig
    h.policy_extra = {"command_profile": "operational", "manager": {"issuer_id": "authority-manager/0", "source_id": "authority-manager"}}
    h.start(*RECOVER)
    state_is(h, STOPPED)
    assert "no physical stop or lockout is being read" in h.stop_proc()


def run_cli(tmp_path, port, *extra):
    return subprocess.run([sys.executable, str(ROOT / "bin/safety-supervisor2keelson.py"), "-r", REALM, "-e", V,
                           "--state-dir", str(tmp_path / "s"), "--mode", "peer", "--connect", f"tcp/127.0.0.1:{port}", *extra],
                          capture_output=True, text=True, timeout=30, check=False)


def test_a_simulated_input_cannot_be_combined_with_the_real_one(rig, tmp_path):
    h, _dev, cfg = rig
    p = run_cli(tmp_path, h.port, "--hw-inputs", str(cfg), "--sim-truth")
    assert p.returncode == 2 and "not allowed with" in (p.stdout + p.stderr)


def test_a_bad_config_refuses_to_start_and_says_why(rig, tmp_path):
    h, _dev, _cfg = rig
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"host": "h"}))
    p = run_cli(tmp_path, h.port, "--hw-inputs", str(bad))
    assert p.returncode == 2 and "physical_stop is required" in (p.stdout + p.stderr)

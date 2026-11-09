"""The on-board start warning end to end (crowsnest-dev#377): the CLI as a real process, on the isolated
loopback Zenoh pair of test_e2e.py."""

import pytest
from keelson.payloads.SafetyState_pb2 import EmergencyCommand, SafetyRequestOutcome as O, SafetyState, StartWarning

from tests.test_e2e import Harness, _reset_by_the_conn

pytestmark = pytest.mark.e2e

W = StartWarning.State
RECOVER = ("--accept-policy", "--recover-locally", "engineer-on-board")
SIM = {"start_warning": {"device": "simulated", "hold_seconds": 1, "valid_seconds": 2}}
ASK = EmergencyCommand.EMERGENCY_COMMAND_START_WARNING


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def warning_is(h, state):
    h.wait(lambda: h.states and h.states[-1].start_warning.state == state, what=W.Name(state))
    return h.states[-1].start_warning


def clear(h):
    h.wait(lambda: h.states and h.states[-1].state == SafetyState.SAFETY_STATE_STOPPED, what="STOPPED")
    _reset_by_the_conn(h)
    h.wait(lambda: h.states[-1].state == SafetyState.SAFETY_STATE_CLEAR, what="CLEAR")


def test_it_sounds_then_is_satisfied_then_lapses(h):
    h.policy_extra = SIM
    h.start(*RECOVER)
    clear(h)
    assert warning_is(h, W.START_WARNING_STATE_IDLE).simulated

    assert h.request(ASK).outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED
    w = warning_is(h, W.START_WARNING_STATE_SOUNDING)
    assert (w.requested_by, w.hold_seconds, w.valid_seconds) == ("ted@ROC-1", 1, 2) and w.HasField("started_at")
    warning_is(h, W.START_WARNING_STATE_SATISFIED)
    warning_is(h, W.START_WARNING_STATE_IDLE)


def test_a_stop_ends_it(h):
    h.policy_extra = {"start_warning": {"device": "simulated", "hold_seconds": 30, "valid_seconds": 30}}
    h.start(*RECOVER)
    clear(h)
    h.request(ASK)
    warning_is(h, W.START_WARNING_STATE_SOUNDING)
    h.request(EmergencyCommand.EMERGENCY_COMMAND_STOP)
    warning_is(h, W.START_WARNING_STATE_IDLE)


def test_it_is_refused_while_stopped(h):
    h.policy_extra = SIM
    h.start(*RECOVER)
    h.wait(lambda: h.states and h.states[-1].state == SafetyState.SAFETY_STATE_STOPPED, what="STOPPED")
    assert h.request(ASK).outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_NOT_CLEAR


def test_a_test_vessel_with_no_device_says_so(h):
    h.start(*RECOVER)
    clear(h)
    assert warning_is(h, W.START_WARNING_STATE_NOT_FITTED).state == W.START_WARNING_STATE_NOT_FITTED
    assert h.request(ASK).outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_NO_WARNING_DEVICE


def test_the_operational_profile_with_no_device_is_a_fault(h):
    h.policy_extra = {"command_profile": "operational", "manager": {"issuer_id": "authority-manager/0", "source_id": "authority-manager"}}
    h.start(*RECOVER)
    warning_is(h, W.START_WARNING_STATE_FAULT)

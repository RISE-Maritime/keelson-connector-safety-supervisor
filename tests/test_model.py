"""The state machine, against the protocol's invariants (keelson protocols/emergency_request.yaml)."""

import pytest

from safety_supervisor import model
from safety_supervisor.model import Authorisation, Cause, Fence, Request, State

T, S, O, C = model.T, model.S, model.O, model.C  # noqa: E741
V = "sf18"
OK = Authorisation(may_trip=True, reset_refusal=None)


def clear():
    return State(causes=(), fence=Fence("a" * 32, 1))


def stop(rid="r1", who="ted@ROC-1"):
    return Request(V, rid, C.EMERGENCY_COMMAND_STOP, who, reason="test")


def reset(state, rid="r2", who="ted@ROC-1"):
    return Request(V, rid, C.EMERGENCY_COMMAND_RESET, who, expected_stopped_fence=state.fence)


def cause(t, clearable=True):
    return Cause(t, "x", 0, clearable)


@pytest.mark.parametrize(
    "types,expected",
    [
        ([], S.SAFETY_STATE_CLEAR),
        ([T.CAUSE_TYPE_SAFE_STATE], S.SAFETY_STATE_SAFE_STATE),
        ([T.CAUSE_TYPE_SAFETY_TRIP, T.CAUSE_TYPE_SAFE_STATE], S.SAFETY_STATE_STOPPED),
        ([T.CAUSE_TYPE_PHYSICAL_STOP], S.SAFETY_STATE_STOPPED),
        ([T.CAUSE_TYPE_EXTERNAL_LEGACY_ESTOP], S.SAFETY_STATE_STOPPED),
        ([T.CAUSE_TYPE_ENVELOPE], S.SAFETY_STATE_STOPPED),
        ([T.CAUSE_TYPE_LOCKOUT, T.CAUSE_TYPE_SAFETY_TRIP], S.SAFETY_STATE_LOCKED_OUT),
        ([T.CAUSE_TYPE_SAFETY_STATE_LOST, T.CAUSE_TYPE_LOCKOUT], S.SAFETY_STATE_RECOVERY_REQUIRED),
    ],
)
def test_state_is_derived_in_the_protocols_precedence(types, expected):
    """inv. 6 — and the derivation is total: never UNSPECIFIED."""
    assert model.derive_state([cause(t) for t in types]) == expected


def test_a_stop_latches_and_bumps_the_generation():
    st, r = model.apply_request(clear(), stop(), OK, 1000, V)
    assert st.state == S.SAFETY_STATE_STOPPED
    assert st.fence == Fence("a" * 32, 2)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED and r.processed_fence == st.fence
    assert st.stopped_at_ms == 1000


def test_two_stops_latch_once_each():
    """inv. 2: unconditional on state — each valid STOP adds its cause and bumps."""
    st, _ = model.apply_request(clear(), stop("r1"), OK, 1000, V)
    st, r = model.apply_request(st, stop("r2"), OK, 1001, V)
    assert [c.request_id for c in st.causes] == ["r1", "r2"]
    assert st.fence.generation == 3 and r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED
    assert st.stopped_at_ms == 1000  # stopped since the first


def test_a_stop_lands_even_in_recovery_required():
    st = model.recovery_required(0, "wiped")
    st2, r = model.apply_request(st, stop(), OK, 1, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED
    assert st2.state == S.SAFETY_STATE_RECOVERY_REQUIRED  # still the stronger state


def test_a_stop_from_a_non_trip_principal_is_refused():
    _, r = model.apply_request(clear(), stop(), Authorisation(False, None, "process"), 0, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED


def test_a_reset_against_the_current_fence_clears_and_bumps():
    st, _ = model.apply_request(clear(), stop(), OK, 0, V)
    st2, r = model.apply_request(st, reset(st), OK, 1, V)
    assert st2.state == S.SAFETY_STATE_CLEAR and st2.causes == ()
    assert st2.fence.generation == st.fence.generation + 1
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED and st2.stopped_at_ms is None


def test_a_reset_against_a_stale_fence_is_refused():
    st, _ = model.apply_request(clear(), stop("r1"), OK, 0, V)
    stale = reset(st)
    st, _ = model.apply_request(st, stop("r3"), OK, 1, V)  # another STOP moved the fence
    st2, r = model.apply_request(st, stale, OK, 2, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_STALE_FENCE and st2 is st


def test_a_reset_with_a_non_resettable_cause_clears_nothing_and_answers_partial():
    """#82: only when no non-resettable one remains; flow partial_reset: the generation does not move."""
    st, _ = model.apply_request(clear(), stop(), OK, 0, V)
    st = model.apply_lockout(st, True, 1, simulated=True)
    st2, r = model.apply_request(st, reset(st), OK, 2, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_PARTIAL
    assert [c.type for c in r.remaining_causes] == [T.CAUSE_TYPE_LOCKOUT]
    assert st2 is st and st2.fence == st.fence


@pytest.mark.parametrize(
    "state,outcome",
    [
        (clear(), O.SAFETY_REQUEST_OUTCOME_REFUSED_NOT_STOPPED),
        (model.recovery_required(0, "wiped"), O.SAFETY_REQUEST_OUTCOME_REFUSED_RECOVERY_REQUIRED),
    ],
)
def test_a_reset_while_clear_or_in_recovery_is_refused(state, outcome):
    _, r = model.apply_request(state, reset(state), OK, 0, V)
    assert r.outcome == outcome


def test_a_reset_refused_by_the_profile_carries_that_refusal():
    st, _ = model.apply_request(clear(), stop(), OK, 0, V)
    refused = Authorisation(True, O.SAFETY_REQUEST_OUTCOME_REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED, "feed not live")
    st2, r = model.apply_request(st, reset(st), refused, 1, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED and st2 is st


@pytest.mark.parametrize(
    "req",
    [
        Request(V, "a/b", C.EMERGENCY_COMMAND_STOP, "ted@ROC-1"),
        Request("other", "r", C.EMERGENCY_COMMAND_STOP, "ted@ROC-1"),
        Request(V, "r", C.EMERGENCY_COMMAND_UNSPECIFIED, "ted@ROC-1"),
        Request(V, "r", C.EMERGENCY_COMMAND_STOP, ""),
    ],
)
def test_malformed_requests_are_refused_and_change_nothing(req):
    st, r = model.apply_request(clear(), req, OK, 0, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_MALFORMED and st.state == S.SAFETY_STATE_CLEAR


def test_the_physical_stop_is_not_clearable_until_released_then_a_reset_clears_it():
    st = model.apply_physical_stop(clear(), True, 0)
    assert st.state == S.SAFETY_STATE_STOPPED
    _, r = model.apply_request(st, reset(st), OK, 1, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_PARTIAL  # pressed: no RESET clears it
    st = model.apply_physical_stop(st, False, 2)
    assert st.state == S.SAFETY_STATE_STOPPED  # released: still stopped, now clearable
    st2, r = model.apply_request(st, reset(st), OK, 3, V)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED and st2.state == S.SAFETY_STATE_CLEAR


def test_the_lockout_is_above_everything_and_clearing_it_rearms_nothing():
    """I14: no remote role clears it; clearing it locally removes only its own cause."""
    st, _ = model.apply_request(clear(), stop(), OK, 0, V)
    st = model.apply_lockout(st, True, 1, simulated=True)
    assert st.state == S.SAFETY_STATE_LOCKED_OUT and st.local_lockout and st.lockout_simulated
    st = model.apply_lockout(st, False, 2, simulated=True)
    assert st.state == S.SAFETY_STATE_STOPPED  # the STOP is still there: nothing re-armed


def test_recovery_is_a_fresh_term_and_a_local_recovery_is_stopped_never_clear():
    """I17, inv. 9."""
    a, b = model.recovery_required(0, "x"), model.recovery_required(0, "x")
    assert a.fence.term != b.fence.term and a.fence.generation == 1
    st = model.recover_locally(a, 1, "engineer-on-board")
    assert st.state == S.SAFETY_STATE_STOPPED and all(c.clearable_remotely for c in st.causes)

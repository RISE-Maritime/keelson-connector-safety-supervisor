"""The envelope watchdog's STOP (crowsnest-dev#291): CAUSE_TYPE_ENVELOPE for a policy-named watchdog, and
SAFETY_TRIP for anyone else. Not clearable while the watchdog keeps re-sending it -- she is still outside a
hard limit -- and clearable by a RESET once it has been quiet for the hold (Ted, 2026-11-06). No network."""

import pytest

from safety_supervisor import model
from safety_supervisor.model import Request
from safety_supervisor.policy import parse
from test_store_and_service import NOTHING, V, conn, make

T, S, O, C = model.T, model.S, model.O, model.C  # noqa: E741
WATCHDOG = "envelope-watchdog/0@sf18"
POLICY = parse({"config_revision": 1, "command_profile": "test",
                "envelope": {"trippers": ["envelope-watchdog/*@*"], "hold_ms": 3000, "watchdog_source_id": "envelope-watchdog"}})


class Now:
    def __init__(self, t=1000):
        self.t = t

    def __call__(self):
        return self.t


def stop(rid, who=WATCHDOG, reason="roll 31.0 deg > 25 deg for 2.0 s"):
    return Request(V, rid, C.EMERGENCY_COMMAND_STOP, who, reason=reason)


def reset(sup, rid):
    return sup.handle_request(Request(V, rid, C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))


@pytest.fixture
def ship(tmp_path):
    now = Now()
    sup, store, bus, proj = make(tmp_path, policy=POLICY, recover="eng", now=now)
    conn(proj)
    assert reset(sup, "r0").outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED  # CLEAR to start from
    return sup, now, store


def envelope(sup):
    return [c for c in sup.state.causes if c.type == T.CAUSE_TYPE_ENVELOPE]


def test_the_watchdogs_stop_is_an_envelope_cause_and_anyone_elses_a_safety_trip(ship):
    sup, now, _ = ship
    assert sup.handle_request(stop("w1")).outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED
    (c,) = envelope(sup)
    assert (c.source, c.clearable_remotely, c.reason) == (WATCHDOG, False, "roll 31.0 deg > 25 deg for 2.0 s")
    assert sup.state.state == S.SAFETY_STATE_STOPPED
    sup.handle_request(stop("h1", who="ted@ROC-1", reason="human"))
    assert [c.type for c in sup.state.causes if c.source == "ted@ROC-1"] == [T.CAUSE_TYPE_SAFETY_TRIP]


def test_another_process_is_never_an_envelope_tripper(ship):
    # The default allow `*@*` matches a process principal too (a pre-existing gap, filed separately), so it
    # may STOP -- but its STOP is a SAFETY_TRIP, clearable at once, never ENVELOPE.
    sup, _, _ = ship
    sup.handle_request(stop("x1", who="navigation/0@sf18"))
    assert [c.type for c in sup.state.causes] == [T.CAUSE_TYPE_SAFETY_TRIP] and not envelope(sup)


def test_while_it_keeps_re_sending_a_reset_is_partial_and_nothing_moves(ship):
    sup, now, _ = ship
    sup.handle_request(stop("w1"))
    fence = sup.state.fence
    now.t += 1000
    sup.handle_request(stop("w2", reason="roll 33.0 deg > 25 deg for 3.0 s"))
    (c,) = envelope(sup)  # one cause per watchdog, refreshed in place
    assert c.heard_ms == now.t and c.reason.startswith("roll 33.0") and sup.state.fence == fence  # no new generation
    now.t += 1000
    sup.on_inputs(NOTHING)  # within the hold: still not clearable
    r = reset(sup, "r1")
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_PARTIAL
    assert T.CAUSE_TYPE_ENVELOPE in [x.type for x in r.remaining_causes] and sup.state.state == S.SAFETY_STATE_STOPPED


def test_quiet_for_the_hold_it_becomes_clearable_and_a_reset_clears_it(ship):
    sup, now, _ = ship
    sup.handle_request(stop("w1"))
    now.t += 3001  # the watchdog has stopped re-sending: she is back inside
    sup.on_inputs(NOTHING)
    assert envelope(sup)[0].clearable_remotely is True
    assert sup.state.state == S.SAFETY_STATE_STOPPED  # clearable is not cleared: a RESET does that
    assert reset(sup, "r1").outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED and sup.state.state == S.SAFETY_STATE_CLEAR


def test_a_re_send_after_it_became_clearable_holds_it_again(ship):
    sup, now, _ = ship
    sup.handle_request(stop("w1"))
    now.t += 3001
    sup.on_inputs(NOTHING)
    sup.handle_request(stop("w2"))  # out again before anyone reset it
    assert envelope(sup)[0].clearable_remotely is False
    assert reset(sup, "r1").outcome == O.SAFETY_REQUEST_OUTCOME_PARTIAL


def test_the_cause_and_its_last_re_send_survive_a_restart(tmp_path):
    now = Now()
    sup, store, *_ = make(tmp_path, policy=POLICY, recover="eng", now=now)
    sup.handle_request(stop("w1"))
    want = envelope(sup)
    store.close()
    sup2, *_ = make(tmp_path, policy=POLICY, now=now)
    assert envelope(sup2) == want


@pytest.mark.parametrize("bad", [{"hold_ms": 0}, {"hold_ms": -5}])
def test_a_hold_that_is_no_hold_is_refused(bad):
    with pytest.raises(ValueError, match="hold_ms"):
        parse({"config_revision": 1, "command_profile": "test", "envelope": bad})


def test_without_an_envelope_section_nothing_is_an_envelope_tripper():
    p = parse({"config_revision": 1, "command_profile": "test"})
    assert not p.is_envelope_tripper(WATCHDOG)


def test_a_denied_watchdog_may_not_trip_even_when_named():
    p = parse({"config_revision": 1, "command_profile": "test", "safety_trip": {"allow": ["*@*"], "deny": [WATCHDOG]},
               "envelope": {"trippers": ["envelope-watchdog/*@*"]}})
    assert not p.may_trip(WATCHDOG)

"""The on-board start warning (crowsnest-dev#377, keelson #428): the state machine, the policy and the ledger."""

import pytest

from safety_supervisor import codec, model
from safety_supervisor.model import O, W, Request, WarningConfig, idle_warning, request_warning, tick_warning
from safety_supervisor.policy import parse
from tests.test_store_and_service import NOTHING, OP_POLICY, TEST_POLICY, V, make

FITTED = WarningConfig(fitted=True, simulated=True, profile="test", hold_ms=10_000, valid_ms=60_000)
REQ = Request(V, "w1", model.C.EMERGENCY_COMMAND_START_WARNING, "ted@ROC-1")


def clear_state():
    return model.replace(model.recovery_required(0, "x"), causes=())


def stopped_state():
    return model.replace(clear_state(), causes=(model.Cause(model.T.CAUSE_TYPE_SAFETY_TRIP, "ted", 0, True),))


CLEAR = clear_state()
IDLE = idle_warning(FITTED)


def ask(w=IDLE, state=CLEAR, may=True, cfg=FITTED, req=REQ, mono=5000):
    return request_warning(w, state, req, may, cfg, 1000, mono, V)


@pytest.mark.parametrize(
    "cfg,expected",
    [
        (FITTED, W.START_WARNING_STATE_IDLE),
        (WarningConfig(profile="test"), W.START_WARNING_STATE_NOT_FITTED),
        (WarningConfig(profile="legacy_single_operator"), W.START_WARNING_STATE_NOT_FITTED),
        (WarningConfig(profile="operational"), W.START_WARNING_STATE_FAULT),
        (WarningConfig(profile=None), W.START_WARNING_STATE_FAULT),
    ],
)
def test_a_warning_at_rest_reads_by_device_and_profile(cfg, expected):
    assert idle_warning(cfg).state == expected


def test_a_request_starts_it_sounding_and_changes_nothing_else():
    w, r = ask()
    assert w.state == W.START_WARNING_STATE_SOUNDING and r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED
    assert (w.requested_by, w.started_at_ms, w.since_mono_ms) == ("ted@ROC-1", 1000, 5000)
    assert r.processed_fence == CLEAR.fence


def test_who_may_not_ask_is_refused():
    w, r = ask(may=False)
    assert w is IDLE and r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED


def test_it_is_refused_unless_clear():
    w, r = ask(state=stopped_state())
    assert w is IDLE and r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_NOT_CLEAR


def test_it_is_refused_under_a_lockout_even_if_the_state_reads_clear():
    _, r = ask(state=model.replace(CLEAR, local_lockout=True))
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_NOT_CLEAR


@pytest.mark.parametrize("cfg", [WarningConfig(profile="test"), WarningConfig(profile="operational")])
def test_it_is_refused_with_no_working_device(cfg):
    w = idle_warning(cfg)
    got, r = ask(w=w, cfg=cfg)
    assert got is w and r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_NO_WARNING_DEVICE


def test_a_second_request_while_sounding_does_not_restart_it():
    w, _ = ask()
    again, r = ask(w=w, mono=9000)
    assert again is w and r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED


def test_a_request_after_it_was_satisfied_sounds_it_again():
    w, _ = ask()
    done = tick_warning(w, CLEAR, FITTED, 15_000)
    again, _ = ask(w=done, mono=20_000)
    assert again.state == W.START_WARNING_STATE_SOUNDING and again.since_mono_ms == 20_000


def test_a_malformed_request_is_refused():
    _, r = ask(req=Request("other", "w1", model.C.EMERGENCY_COMMAND_START_WARNING, "ted@ROC-1"))
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_MALFORMED


def test_it_sounds_for_hold_then_is_satisfied():
    w, _ = ask(mono=5000)
    assert tick_warning(w, CLEAR, FITTED, 14_999) is w
    done = tick_warning(w, CLEAR, FITTED, 15_000)
    assert done.state == W.START_WARNING_STATE_SATISFIED and done.since_mono_ms == 15_000


def test_a_satisfied_warning_cannot_be_banked():
    w, _ = ask(mono=5000)
    done = tick_warning(w, CLEAR, FITTED, 15_000)
    assert tick_warning(done, CLEAR, FITTED, 74_999) is done
    assert tick_warning(done, CLEAR, FITTED, 75_000).state == W.START_WARNING_STATE_IDLE


@pytest.mark.parametrize("state", [stopped_state(), model.replace(CLEAR, local_lockout=True)])
def test_any_departure_from_clear_ends_it(state):
    sounding, _ = ask()
    done = tick_warning(sounding, CLEAR, FITTED, 15_000)
    assert tick_warning(sounding, state, FITTED, 6000).state == W.START_WARNING_STATE_IDLE
    assert tick_warning(done, state, FITTED, 16_000).state == W.START_WARNING_STATE_IDLE


def test_nothing_ticks_a_warning_at_rest():
    assert tick_warning(IDLE, stopped_state(), FITTED, 10**9) is IDLE


def policy(**warning):
    return parse({"config_revision": 1, "command_profile": "test", "start_warning": warning})


def test_people_may_ask_by_default_and_a_process_only_if_named():
    p = policy()
    assert p.may_warn("ted@ROC-1") and not p.may_warn("scheduler/0")
    named = policy(allow=["*@*", "scheduler/*"])
    assert named.may_warn("scheduler/0")


def test_a_denied_person_may_not_ask():
    assert not policy(deny=["ted@*"]).may_warn("ted@ROC-1")


def test_a_simulated_horn_is_honoured_only_in_the_test_profile():
    assert policy(device="simulated").warning_config().fitted
    for profile in ("operational", "legacy_single_operator"):
        p = parse({"config_revision": 1, "command_profile": profile, "manager": {"issuer_id": "m"}, "start_warning": {"device": "simulated"}})
        assert not p.warning_config().fitted and not p.warning_config().simulated


def test_the_times_are_configured():
    cfg = policy(device="simulated", hold_seconds=3, valid_seconds=7).warning_config()
    assert (cfg.hold_ms, cfg.valid_ms) == (3000, 7000)


@pytest.mark.parametrize("bad", [{"device": "klaxon"}, {"hold_seconds": 0}, {"valid_seconds": -1}, {"hold_seconds": "3"}])
def test_a_bad_warning_policy_refuses_to_load(bad):
    with pytest.raises(ValueError):
        policy(**bad)


# ---------------------------------------------------------------------------- the service


class Mono:
    t = 0

    def __call__(self):
        return Mono.t


@pytest.fixture
def sim(tmp_path):
    Mono.t = 0
    p = policy(device="simulated", hold_seconds=10, valid_seconds=60)
    sup, store, bus, proj = make(tmp_path, policy=p, recover="engineer")
    sup.state = model.replace(sup.state, causes=())
    sup.mono_ms = Mono()
    yield sup, store, bus, proj
    store.close()


def test_the_ledger_answers_a_repeated_request_once(sim):
    sup, _store, bus, _ = sim
    first = sup.handle_request(REQ)
    Mono.t = 20_000
    sup.on_inputs(NOTHING)
    assert sup.warning.state == W.START_WARNING_STATE_SATISFIED
    again = sup.handle_request(REQ)
    assert again == first and sup.warning.state == W.START_WARNING_STATE_SATISFIED
    assert [r.request_id for r in bus.results] == ["w1", "w1"]


def test_it_mints_no_generation_and_adds_no_cause(sim):
    sup, *_ = sim
    before = sup.state
    sup.handle_request(REQ)
    assert sup.state is before


def test_the_loop_moves_it_on_time_and_a_stop_ends_it(sim):
    sup, *_ = sim
    sup.handle_request(REQ)
    Mono.t = 9999
    sup.on_inputs(NOTHING)
    assert sup.warning.state == W.START_WARNING_STATE_SOUNDING
    Mono.t = 10_000
    sup.on_inputs(NOTHING)
    assert sup.warning.state == W.START_WARNING_STATE_SATISFIED
    sup.handle_request(Request(V, "s9", model.C.EMERGENCY_COMMAND_STOP, "ted@ROC-1"))
    assert sup.warning.state == W.START_WARNING_STATE_IDLE


def test_the_operational_profile_with_no_device_reads_fault(tmp_path):
    sup, store, *_ = make(tmp_path, policy=OP_POLICY, recover="engineer")
    assert sup.warning.state == W.START_WARNING_STATE_FAULT
    store.close()


def test_no_adopted_policy_reads_fault_and_refuses_the_request(tmp_path):
    sup, store, *_ = make(tmp_path, policy=None, recover="engineer")
    assert sup.warning.state == W.START_WARNING_STATE_FAULT
    assert sup.handle_request(REQ).outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED
    store.close()


def test_a_test_vessel_with_no_device_says_not_fitted(tmp_path):
    sup, store, *_ = make(tmp_path, policy=TEST_POLICY, recover="engineer")
    assert sup.warning.state == W.START_WARNING_STATE_NOT_FITTED
    store.close()


def test_the_wire_form():
    w, _ = ask()
    pb = codec.state_to_pb(CLEAR, vessel_id=V, supervisor_id="s", now_ms=1, warning=w, warning_cfg=FITTED).start_warning
    assert (pb.state, pb.hold_seconds, pb.valid_seconds, pb.requested_by, pb.simulated) == (W.START_WARNING_STATE_SOUNDING, 10, 60, "ted@ROC-1", True)
    assert pb.HasField("started_at")
    rest = codec.state_to_pb(CLEAR, vessel_id=V, supervisor_id="s", now_ms=1, warning=IDLE, warning_cfg=FITTED).start_warning
    assert not rest.HasField("started_at")

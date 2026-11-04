"""The store's transaction, the ledger, boot and recovery — with fake publishers, no network."""

import json
import sqlite3

import pytest
from keelson.payloads.CommandAuthority_pb2 import Assignment, CommandAssignment, CommandAuthority, Role

from safety_supervisor import model
from safety_supervisor.actuation import LoggingActuator
from safety_supervisor.inputs import Reading
from safety_supervisor.model import Fence, Request
from safety_supervisor.policy import parse
from safety_supervisor.projection import Projections
from safety_supervisor.service import Supervisor
from safety_supervisor.store import Store

T, S, O, C = model.T, model.S, model.O, model.C
V = "sf18"
NOTHING = Reading(False, False, simulated=True)
TEST_POLICY = parse({"config_revision": 1, "command_profile": "test"})
OP_POLICY = parse({"config_revision": 1, "command_profile": "operational", "manager": {"issuer_id": "authority-manager/0", "source_id": "authority-manager"}})


class Bus:
    def __init__(self, fail_results=False):
        self.states, self.results, self.fail_results = [], [], fail_results

    def state(self, st):
        self.states.append(st)

    def result(self, r):
        if self.fail_results:
            raise RuntimeError("crash before the result was published")
        self.results.append(r)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(tmp_path, bus=None, policy=TEST_POLICY, clock=None, recover=None, reading=NOTHING):
    store = Store(tmp_path / "safety.sqlite")
    bus = bus or Bus()
    proj = Projections(store, clock=clock or Clock())
    sup = Supervisor(store=store, vessel_id=V, policy=policy, projections=proj, publish_state=bus.state,
                     publish_result=bus.result, actuator=LoggingActuator(), now_ms=lambda: 1000)
    sup.boot(reading, recover_operator=recover)
    return sup, store, bus, proj


def conn(proj, who="ted@ROC-1"):
    proj.on_conn_lease(CommandAuthority(vessel_id=V, controller_id=who, role=Role.ROLE_CONN))


def stop(rid="s1"):
    return Request(V, rid, C.EMERGENCY_COMMAND_STOP, "ted@ROC-1")


# ---------------------------------------------------------------------------- I17: a lost store


def test_a_new_or_wiped_store_is_recovery_required_never_clear(tmp_path):
    sup, *_ = make(tmp_path)
    assert sup.state.state == S.SAFETY_STATE_RECOVERY_REQUIRED
    assert [c.type for c in sup.state.causes] == [T.CAUSE_TYPE_SAFETY_STATE_LOST]
    assert not sup.state.causes[0].clearable_remotely


def test_a_tampered_state_is_recovery_required(tmp_path):
    sup, store, *_ = make(tmp_path, recover="eng")
    sup.handle_request(Request(V, "r", C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))
    store.close()
    db = sqlite3.connect(tmp_path / "safety.sqlite")
    row = json.loads(db.execute("SELECT v FROM meta WHERE k='state'").fetchone()[0])
    row["causes"] = []  # someone "clears" it behind the supervisor's back
    db.execute("UPDATE meta SET v=? WHERE k='state'", (json.dumps(row),))
    db.commit()
    db.close()
    sup2, *_ = make(tmp_path)
    assert sup2.state.state == S.SAFETY_STATE_RECOVERY_REQUIRED


def test_local_recovery_is_stopped_and_then_a_reset_by_the_conn_clears(tmp_path):
    sup, _, bus, proj = make(tmp_path, recover="engineer-on-board")
    assert sup.state.state == S.SAFETY_STATE_STOPPED
    conn(proj)
    r = sup.handle_request(Request(V, "r1", C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED and sup.state.state == S.SAFETY_STATE_CLEAR
    assert bus.results[-1].request_id == "r1"


# ---------------------------------------------------------------------------- I8: the ledger


def test_a_request_executes_once_and_a_repeat_returns_the_recorded_result(tmp_path):
    sup, _store, _bus, _ = make(tmp_path, recover="eng")
    first = sup.handle_request(stop("s1"))
    gen = sup.state.fence.generation
    again = sup.handle_request(stop("s1"))
    assert again == first and sup.state.fence.generation == gen
    assert len([c for c in sup.state.causes if c.request_id == "s1"]) == 1


def test_a_crash_between_commit_and_publish_is_republished_at_boot(tmp_path):
    bus = Bus(fail_results=True)
    sup, store, *_ = make(tmp_path, bus=bus, recover="eng")
    with pytest.raises(RuntimeError):
        sup.handle_request(stop("s1"))
    assert store.result_for("s1") is not None  # committed with the state
    store.close()
    bus2 = Bus()
    sup2, *_ = make(tmp_path, bus=bus2)
    assert [r.request_id for r in bus2.results] == ["s1"]  # republished from the ledger
    assert sup2.state.state == S.SAFETY_STATE_STOPPED
    # ...and seeing the request again does not execute it twice.
    gen = sup2.state.fence.generation
    sup2.handle_request(stop("s1"))
    assert sup2.state.fence.generation == gen


def test_multi_crash_recovery_processes_every_retained_request_once(tmp_path):
    """I8: retained requests at boot, in any order, some already answered."""
    sup, store, *_ = make(tmp_path, recover="eng")
    sup.handle_request(stop("s1"))
    store.close()
    sup2, *_ = make(tmp_path)
    sup2.handle_requests([stop("s2"), stop("s1"), stop("s3")])
    trips = [c.request_id for c in sup2.state.causes if c.type == T.CAUSE_TYPE_SAFETY_TRIP and c.request_id]
    assert sorted(trips) == ["s1", "s2", "s3"]


def test_gc_never_removes_an_unpublished_result(tmp_path):
    bus = Bus(fail_results=True)
    sup, store, *_ = make(tmp_path, bus=bus, recover="eng")
    with pytest.raises(RuntimeError):
        sup.handle_request(stop("s1"))
    assert store.gc(now_ms=10**12, retention_ms=0) == 0
    assert store.result_for("s1") is not None


def test_the_term_persists_across_a_restart_with_a_valid_store(tmp_path):
    sup, store, *_ = make(tmp_path, recover="eng")
    fence = sup.state.fence
    store.close()
    sup2, *_ = make(tmp_path)
    assert sup2.state.fence == fence


# ---------------------------------------------------------------------------- inputs at boot


def test_boot_adds_the_causes_the_inputs_prove(tmp_path):
    sup, *_ = make(tmp_path, recover="eng", reading=Reading(physical_stop=True, lockout=True, simulated=True))
    types = {c.type for c in sup.state.causes}
    assert {T.CAUSE_TYPE_PHYSICAL_STOP, T.CAUSE_TYPE_LOCKOUT} <= types
    assert sup.state.state == S.SAFETY_STATE_LOCKED_OUT and sup.state.lockout_simulated


# ---------------------------------------------------------------------------- who may RESET


def test_test_profile_only_the_conn_may_reset(tmp_path):
    sup, _, _, proj = make(tmp_path, recover="eng")
    conn(proj, "alice@ROC-2")
    r = sup.handle_request(Request(V, "r1", C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED


def _assignment(proj, *, rev=4, issuer="authority-manager/0", ttl=10, assignee="ted@ROC-1"):
    proj.on_command_assignment(
        CommandAssignment(vessel_id=V, issuer_id=issuer, feed_ttl_seconds=ttl,
                          overall=Assignment(assignee=assignee, assignment_id="as-1", revision=rev)),
        OP_POLICY,
    )


def _op_reset(sup, rev=4, who="ted@ROC-1", rid="r1"):
    return sup.handle_request(Request(V, rid, C.EMERGENCY_COMMAND_RESET, who, expected_stopped_fence=sup.state.fence,
                                      overall_assignment_id="as-1", overall_assignment_revision=rev))


def test_operational_reset_by_the_current_master_with_a_live_feed(tmp_path):
    clock = Clock()
    sup, _, _, proj = make(tmp_path, policy=OP_POLICY, clock=clock, recover="eng")
    _assignment(proj)
    proj.on_manager_liveliness(True)
    assert _op_reset(sup).outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED


@pytest.mark.parametrize(
    "case",
    ["feed_stale", "no_manager_token", "wrong_issuer", "old_revision", "never_heard"],
)
def test_operational_reset_fails_closed_unless_the_projection_is_current(tmp_path, case):
    """I7′: every way the projection can be unverified refuses the RESET; a STOP still lands."""
    clock = Clock()
    sup, _, _, proj = make(tmp_path, policy=OP_POLICY, clock=clock, recover="eng")
    if case != "never_heard":
        _assignment(proj, issuer="someone-else" if case == "wrong_issuer" else "authority-manager/0")
    proj.on_manager_liveliness(case != "no_manager_token")
    if case == "feed_stale":
        clock.t = 11
    r = _op_reset(sup, rev=3 if case == "old_revision" else 4)
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED
    assert sup.handle_request(stop("s9")).outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED


def test_operational_reset_by_someone_other_than_the_master_is_unauthorised(tmp_path):
    sup, _, _, proj = make(tmp_path, policy=OP_POLICY, recover="eng")
    _assignment(proj)
    proj.on_manager_liveliness(True)
    assert _op_reset(sup, who="alice@ROC-2").outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED


def test_after_a_restart_the_projection_is_not_live_until_heard_again(tmp_path):
    _sup, store, _, proj = make(tmp_path, policy=OP_POLICY, recover="eng")
    _assignment(proj)
    proj.on_manager_liveliness(True)
    store.close()
    sup2, _, _, proj2 = make(tmp_path, policy=OP_POLICY)
    proj2.on_manager_liveliness(True)
    assert _op_reset(sup2).outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED


def test_with_no_adopted_policy_a_stop_lands_and_a_reset_is_refused(tmp_path):
    sup, *_ = make(tmp_path, policy=None, recover="eng")
    assert sup.handle_request(stop("s1")).outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED
    r = sup.handle_request(Request(V, "r1", C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED


def test_states_published_carry_the_current_fence(tmp_path):
    sup, _, bus, _ = make(tmp_path, recover="eng")
    sup.handle_request(stop("s1"))
    assert bus.states[-1].fence == sup.state.fence and bus.states[-1].state == S.SAFETY_STATE_STOPPED
    assert isinstance(sup.state.fence, Fence)


# ---------------------------------------------------------------------------- the legacy stop (edge, RESET clears)


def _clear(tmp_path, **kw):
    """A supervisor at CLEAR: recovered locally, then reset by the conn."""
    sup, store, bus, proj = make(tmp_path, recover="eng", **kw)
    conn(proj)
    sup.handle_request(Request(V, "r0", C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))
    assert sup.state.state == S.SAFETY_STATE_CLEAR
    return sup, store, bus, proj


def test_an_observed_edge_into_stopped_latches_and_a_reset_clears_it(tmp_path):
    sup, _, _, _ = _clear(tmp_path)
    sup.on_vehicle_stopped("slipway", False)
    sup.on_vehicle_stopped("slipway", True)
    assert sup.state.state == S.SAFETY_STATE_STOPPED
    cause = next(c for c in sup.state.causes if c.type == T.CAUSE_TYPE_EXTERNAL_LEGACY_ESTOP)
    assert cause.clearable_remotely and cause.source == "slipway"
    r = sup.handle_request(Request(V, "r1", C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))
    assert r.outcome == O.SAFETY_REQUEST_OUTCOME_ACCEPTED and sup.state.state == S.SAFETY_STATE_CLEAR


def test_a_vessel_that_is_simply_stopped_is_not_an_emergency_stop(tmp_path):
    sup, _, _, _ = _clear(tmp_path)
    sup.on_vehicle_stopped("mavlink:mavlink", True)  # first value ever seen: disarmed at the berth
    sup.on_vehicle_stopped("mavlink:mavlink", True)
    assert sup.state.state == S.SAFETY_STATE_CLEAR


def test_staying_stopped_after_a_reset_does_not_latch_again(tmp_path):
    sup, _, _, _ = _clear(tmp_path)
    sup.on_vehicle_stopped("slipway", False)
    sup.on_vehicle_stopped("slipway", True)
    sup.handle_request(Request(V, "r1", C.EMERGENCY_COMMAND_RESET, "ted@ROC-1", expected_stopped_fence=sup.state.fence))
    sup.on_vehicle_stopped("slipway", True)  # her latch is still on until someone arms her: no new edge
    assert sup.state.state == S.SAFETY_STATE_CLEAR


def test_an_edge_while_the_supervisor_was_down_is_seen_at_boot(tmp_path):
    sup, store, _, _ = _clear(tmp_path)
    sup.on_vehicle_stopped("slipway", False)
    store.close()
    sup2, *_ = make(tmp_path)
    sup2.on_vehicle_stopped("slipway", True)  # the first value after the restart, against the persisted memory
    assert sup2.state.state == S.SAFETY_STATE_STOPPED


def test_the_stop_the_supervisor_actuated_comes_back_and_adds_nothing(tmp_path):
    sup, _, _, _ = _clear(tmp_path)
    sup.on_vehicle_stopped("slipway", False)
    sup.handle_request(stop("s1"))
    gen = sup.state.fence.generation
    sup.on_vehicle_stopped("slipway", True)  # the echo of our own STOP
    assert sup.state.fence.generation == gen
    assert [c.type for c in sup.state.causes] == [T.CAUSE_TYPE_SAFETY_TRIP]


# ---------------------------------------------------------------------------- Slipway's truth record


def test_the_truth_records_lockout_is_a_simulated_lockout_and_silence_never_releases_it():
    from safety_supervisor.inputs import TruthInputs

    t = TruthInputs(ttl_ms=1000)
    assert t.read(0) == Reading(False, False, simulated=True) and t.estop() is None
    t.on_truth({"estop": False, "lockout": True}, 0)
    assert t.read(10).lockout and t.estop() is False
    assert t.read(60_000).lockout and t.stale(60_000)  # silent for a minute: still locked out
    t.on_truth({"estop": True, "lockout": False}, 61_000)
    assert not t.read(61_000).lockout and t.estop() is True


def test_a_lockout_from_the_truth_record_is_locked_out_and_labelled(tmp_path):
    from safety_supervisor.inputs import TruthInputs

    t = TruthInputs()
    sup, *_ = _clear(tmp_path)
    t.on_truth({"lockout": True, "estop": False}, 0)
    sup.on_inputs(t.read(0))
    assert sup.state.state == S.SAFETY_STATE_LOCKED_OUT and sup.state.lockout_simulated


# ---------------------------------------------------------------------------- the Slipway actuator


def test_the_slipway_actuator_stops_her_retries_until_accepted_and_never_rearms():
    from safety_supervisor.actuation import SlipwayLifecycleActuator

    answers = [(False, "no answer"), (True, "engaged")]
    a = SlipwayLifecycleActuator(lambda: answers.pop(0))
    a.apply(True, "STOP")
    assert a.calls == 1
    a.apply(True, "STOP")  # the heartbeat retries
    assert a.calls == 2
    a.apply(True, "STOP")  # landed: not again
    assert a.calls == 2
    a.apply(False, "RESET")  # release calls nothing
    assert a.calls == 2


def test_the_slipway_actuator_is_for_the_test_profile_with_the_truth_record_only():
    from safety_supervisor.actuation import SlipwayLifecycleActuator

    assert SlipwayLifecycleActuator.guard("operational", True)
    assert SlipwayLifecycleActuator.guard(None, True)
    assert SlipwayLifecycleActuator.guard("test", False)
    assert SlipwayLifecycleActuator.guard("test", True) is None


def test_the_heartbeat_retries_an_actuation_that_did_not_land(tmp_path):
    from safety_supervisor.actuation import SlipwayLifecycleActuator

    calls = []
    store = Store(tmp_path / "safety.sqlite")
    bus = Bus()
    act = SlipwayLifecycleActuator(lambda: calls.append(1) or (len(calls) > 1, "x"))
    sup = Supervisor(store=store, vessel_id=V, policy=TEST_POLICY, projections=Projections(store, clock=Clock()),
                     publish_state=bus.state, publish_result=bus.result, actuator=act, now_ms=lambda: 1000)
    sup.boot(NOTHING, recover_operator="eng")  # STOPPED: inhibiting, first call fails
    assert len(calls) == 1
    sup.heartbeat()
    assert len(calls) == 2
    sup.heartbeat()
    assert len(calls) == 2


# ---------------------------------------------------------------------------- review 2026-11-04: A and C


class _Inner:
    def __init__(self, answers=None):
        from safety_supervisor.actuation import SlipwayLifecycleActuator

        self.calls = 0
        answers = list(answers or [])

        def call():
            self.calls += 1
            return answers.pop(0) if answers else (True, "ok")

        self.a = SlipwayLifecycleActuator(call)

    def apply(self, inhibit, why):
        self.a.apply(inhibit, why)

    def relatch(self):
        self.a.relatch()


def test_the_decision_loop_never_calls_the_vessel_itself():
    from safety_supervisor.actuation import ThreadedActuator

    inner = _Inner()
    t = ThreadedActuator(inner)
    t.apply(True, "STOP")  # what the loop does: record, return
    assert inner.calls == 0
    t.run_once()  # the worker's body
    assert inner.calls == 1


def test_a_stop_that_did_not_land_is_retried_by_the_worker_until_it_does():
    from safety_supervisor.actuation import ThreadedActuator

    inner = _Inner([(False, "no answer"), (False, "no answer"), (True, "ok")])
    t = ThreadedActuator(inner)
    t.apply(True, "STOP")
    for _ in range(5):
        t.run_once()
    assert inner.calls == 3  # and not again once it landed


def test_armed_on_the_legacy_path_while_inhibiting_she_is_stopped_again_and_it_alarms():
    from safety_supervisor.actuation import ThreadedActuator

    alarms = []
    inner = _Inner()
    t = ThreadedActuator(inner, on_rearmed=alarms.append)
    t.apply(True, "STOP")
    t.run_once()
    t.vessel_latch(True)  # her latch engaged: as it should be
    t.run_once()
    assert inner.calls == 1 and alarms == []
    t.vessel_latch(False)  # somebody armed her directly
    t.run_once()
    assert inner.calls == 2 and alarms == ["STOP"]


def test_a_released_latch_after_a_reset_is_no_alarm():
    from safety_supervisor.actuation import ThreadedActuator

    alarms = []
    inner = _Inner()
    t = ThreadedActuator(inner, on_rearmed=alarms.append)
    t.apply(True, "STOP")
    t.run_once()
    t.apply(False, "RESET")
    t.run_once()
    t.vessel_latch(False)  # armed after the RESET: allowed
    t.run_once()
    assert inner.calls == 1 and alarms == []

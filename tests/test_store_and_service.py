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

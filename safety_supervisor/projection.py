"""Who may RESET: the supervisor's own persisted projections, judged on its own monotonic clock.

OPERATIONAL — the master. The OVERALL_COMMAND assignee from `command_assignment/{vessel_id}`,
persisted so a RESET after ASSUME_COMMAND is checked against the CURRENT master (I7, I7'). It FAILS
CLOSED — REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED — unless the feed was heard within
`feed_ttl_seconds` on this process's monotonic clock, the manager's liveliness token is present, the
issuer is the manager the policy names, and the RESET's assignment id + revision are the ones the
feed advertises. A restart therefore starts NOT live (a monotonic time does not survive it) until the
feed is heard again. A STOP never reads any of this (inv. 11).

TEST / LEGACY_SINGLE_OPERATOR — the conn. The holder of the conn lease `command_authority/{vessel_id}`
(role CONN or unspecified), not released, heard within `conn_ttl_s`. This is the one lease the
supervisor reads; it is flagged on crowsnest-dev #82 for review.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from keelson.payloads.CommandAuthority_pb2 import Role
from keelson.payloads.SafetyState_pb2 import SafetyRequestOutcome as O

from .model import C, Authorisation, Request
from .policy import Policy

# The protocol's default SAFETY_TRIP policy (humans at a site yes, processes no), used for a STOP
# when no policy has been adopted yet.
DEFAULT_POLICY = Policy(config_revision=0, command_profile="test")


@dataclass
class _Overall:
    assignee: str
    assignment_id: str
    revision: int
    issuer_id: str
    feed_ttl_s: float
    heard_mono: float | None  # None after a restart: not live until heard again


@dataclass
class _Conn:
    controller_id: str
    released: bool
    heard_mono: float


class Projections:
    def __init__(self, store, clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.clock = clock
        self.manager_token_present = False
        self.overall: _Overall | None = None
        self.conn: _Conn | None = None
        saved = store.get_json("projection")
        if saved:
            self.overall = _Overall(**{**saved, "heard_mono": None})

    # ------------------------------------------------------------------ feeds

    def on_command_assignment(self, msg, policy: Policy | None) -> None:
        """A CommandAssignment from the bus. Only the authorised manager's is projected."""
        if policy is None or not policy.manager_issuer_id or msg.issuer_id != policy.manager_issuer_id:
            return
        if not msg.HasField("overall"):
            self.overall = None
            self.store.put_json("projection", {})
            return
        a = msg.overall
        if self.overall and self.overall.assignment_id == a.assignment_id and a.revision < self.overall.revision:
            return  # an older revision of the same assignment is ignored (CommandAuthority.proto)
        self.overall = _Overall(a.assignee, a.assignment_id, a.revision, msg.issuer_id, float(msg.feed_ttl_seconds or 0), self.clock())
        saved = {k: v for k, v in self.overall.__dict__.items() if k != "heard_mono"}
        self.store.put_json("projection", saved)

    def on_conn_lease(self, msg) -> None:
        if msg.role not in (Role.ROLE_UNSPECIFIED, Role.ROLE_CONN):
            return
        self.conn = _Conn(msg.controller_id, bool(msg.released), self.clock())

    def on_manager_liveliness(self, present: bool) -> None:
        self.manager_token_present = present

    # ------------------------------------------------------------------ decisions

    def assignment_live(self) -> bool:
        o = self.overall
        return bool(o and o.heard_mono is not None and o.feed_ttl_s > 0
                    and self.clock() - o.heard_mono <= o.feed_ttl_s and self.manager_token_present)

    def authorise(self, req: Request, policy: Policy | None) -> Authorisation:
        if policy is None:
            # No adopted policy. A RESET fails closed; a STOP must still land, so it takes the
            # protocol's default policy -- a human at a site -- rather than nothing (more inhibited is
            # the safe side, never less).
            return Authorisation(DEFAULT_POLICY.may_trip(req.controller_id), O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED,
                                 "no adopted safety policy")
        may_trip = policy.may_trip(req.controller_id)
        if req.command == C.EMERGENCY_COMMAND_STOP and policy.is_envelope_tripper(req.controller_id):
            return Authorisation(may_trip, None, envelope=True)
        if policy.command_profile == "operational":
            o = self.overall
            if not self.assignment_live() or o.issuer_id != policy.manager_issuer_id:
                return Authorisation(may_trip, O.SAFETY_REQUEST_OUTCOME_REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED,
                                     "the command_assignment feed is not live from the authorised manager")
            if (req.overall_assignment_id, req.overall_assignment_revision) != (o.assignment_id, o.revision):
                return Authorisation(may_trip, O.SAFETY_REQUEST_OUTCOME_REFUSED_ASSIGNMENT_PROJECTION_UNVERIFIED,
                                     "the RESET names another assignment or revision than the feed advertises")
            if req.controller_id != o.assignee:
                return Authorisation(may_trip, O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED, "only the OVERALL_COMMAND assignee may RESET")
            return Authorisation(may_trip, None)
        # test / legacy_single_operator: the conn.
        c = self.conn
        live = c is not None and not c.released and self.clock() - c.heard_mono <= policy.conn_ttl_s
        if not live or req.controller_id != c.controller_id:
            return Authorisation(may_trip, O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED, "only the conn holder may RESET in this profile")
        return Authorisation(may_trip, None)

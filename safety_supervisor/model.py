"""The safety state machine, and nothing else. Pure: no clock, no store, no Zenoh.

Normative source: keelson `protocols/emergency_request.yaml` (#332) and `SafetyState.proto`. The
invariants it implements are cited by number ("inv. N") and by crowsnest-dev
docs/COMMAND-ARCHITECTURE.md §8 ("I8", "I17", ...).

Every function takes the current `State` and returns a new one; nothing is mutated. The caller
(service.py) commits the new state and the ledger entry in ONE store transaction before anything is
published (inv. 4).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace

from keelson.payloads.SafetyState_pb2 import (
    EmergencyCommand,
    SafetyCause,
    SafetyRequestOutcome,
    SafetyState,
)

T = SafetyCause.Type
S = SafetyState.State
O = SafetyRequestOutcome  # noqa: E741 - the enum's alias, like T, S and C
C = EmergencyCommand

# Causes that latch STOPPED (SafetyState.proto).
STOP_CLASS = frozenset(
    {T.CAUSE_TYPE_SAFETY_TRIP, T.CAUSE_TYPE_PHYSICAL_STOP, T.CAUSE_TYPE_EXTERNAL_LEGACY_ESTOP, T.CAUSE_TYPE_ENVELOPE}
)

PROFILES = ("operational", "test", "legacy_single_operator")


@dataclass(frozen=True)
class Fence:
    """`(term, generation)`. The term is the supervisor's incarnation; only it mints either half."""

    term: str
    generation: int

    @staticmethod
    def fresh() -> Fence:
        # A new incarnation: 16 random bytes, hex. Never "last + 1" (Fence.proto).
        return Fence(term=secrets.token_hex(16), generation=1)

    def bumped(self) -> Fence:
        return Fence(self.term, self.generation + 1)


@dataclass(frozen=True)
class Cause:
    type: int
    source: str
    since_ms: int
    clearable_remotely: bool
    request_id: str | None = None
    safe_state_name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class State:
    causes: tuple[Cause, ...]
    fence: Fence
    local_lockout: bool = False
    lockout_simulated: bool = False
    stopped_at_ms: int | None = None
    who: str = ""
    why: str = ""

    @property
    def state(self) -> int:
        return derive_state(self.causes)


@dataclass(frozen=True)
class Request:
    """The fields of an EmergencyRequest the decision reads (codec.request_from_proto)."""

    vessel_id: str
    request_id: str
    command: int
    controller_id: str
    controller_site: str = ""
    principal: str = ""
    reason: str = ""
    expected_stopped_fence: Fence | None = None
    overall_assignment_id: str = ""
    overall_assignment_revision: int = 0


@dataclass(frozen=True)
class Result:
    """A ledger entry: what the supervisor answered, forever (inv. 4, 5)."""

    request_id: str
    command: int
    outcome: int
    processed_fence: Fence
    processed_at_ms: int
    remaining_causes: tuple[Cause, ...] = field(default_factory=tuple)
    detail: str = ""


@dataclass(frozen=True)
class Authorisation:
    """What the caller (service.py) established about the request, from the supervisor's OWN policy
    and projection. The model never asks anyone (inv. 10)."""

    may_trip: bool
    # For a RESET: None = authorised; otherwise the refusal outcome (UNAUTHORISED or
    # ASSIGNMENT_PROJECTION_UNVERIFIED) and why.
    reset_refusal: int | None = None
    detail: str = ""


def derive_state(causes) -> int:
    """Total, in the protocol's precedence (inv. 6). Never UNSPECIFIED."""
    types = {c.type for c in causes}
    if T.CAUSE_TYPE_SAFETY_STATE_LOST in types:
        return S.SAFETY_STATE_RECOVERY_REQUIRED
    if T.CAUSE_TYPE_LOCKOUT in types:
        return S.SAFETY_STATE_LOCKED_OUT
    if types & STOP_CLASS:
        return S.SAFETY_STATE_STOPPED
    if T.CAUSE_TYPE_SAFE_STATE in types:
        return S.SAFETY_STATE_SAFE_STATE
    return S.SAFETY_STATE_CLEAR


def inhibiting(state: State) -> bool:
    """Anything but CLEAR inhibits actuation."""
    return state.state != S.SAFETY_STATE_CLEAR or state.local_lockout


def _with_causes(state: State, causes, now_ms: int, *, who: str, why: str) -> State:
    """New causes; the generation bumps on every change of the cause set (every STOP that latches,
    every RESET that clears, every input that changes the state), so a RESET prepared against the
    old fence is stale."""
    causes = tuple(causes)
    was_stopped = state.stopped_at_ms is not None
    now_stopped = derive_state(causes) != S.SAFETY_STATE_CLEAR
    return replace(
        state,
        causes=causes,
        fence=state.fence.bumped(),
        stopped_at_ms=(state.stopped_at_ms if was_stopped else now_ms) if now_stopped else None,
        who=who,
        why=why,
    )


def malformed(req: Request, vessel_id: str) -> str | None:
    if not req.request_id or "/" in req.request_id or "*" in req.request_id:
        return "request_id must be one key chunk"
    if req.vessel_id != vessel_id:
        return f"request names vessel {req.vessel_id!r}, this supervisor is {vessel_id!r}"
    if req.command not in (C.EMERGENCY_COMMAND_STOP, C.EMERGENCY_COMMAND_RESET):
        return "command must be STOP or RESET"
    if not req.controller_id:
        return "controller_id is required"
    return None


def apply_request(state: State, req: Request, auth: Authorisation, now_ms: int, vessel_id: str) -> tuple[State, Result]:
    """Decide one request. The ledger (service.py) guarantees this runs at most once per request_id."""

    def answer(st: State, outcome: int, detail: str = "", remaining=()) -> tuple[State, Result]:
        return st, Result(req.request_id, req.command, outcome, st.fence, now_ms, tuple(remaining), detail)

    bad = malformed(req, vessel_id)
    if bad:
        return answer(state, O.SAFETY_REQUEST_OUTCOME_REFUSED_MALFORMED, bad)

    if req.command == C.EMERGENCY_COMMAND_STOP:
        # Unconditional on state; idempotence is the ledger's (inv. 2). Refused only when the caller
        # may not trip at all under the supervisor's own policy (inv. 10).
        if not auth.may_trip:
            return answer(state, O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED, auth.detail or "not a SAFETY_TRIP principal")
        cause = Cause(T.CAUSE_TYPE_SAFETY_TRIP, req.controller_id, now_ms, True, req.request_id, reason=req.reason)
        st = _with_causes(state, (*state.causes, cause), now_ms, who=req.controller_id, why=req.reason or "STOP")
        return answer(st, O.SAFETY_REQUEST_OUTCOME_ACCEPTED, remaining=st.causes)

    # RESET (inv. 3).
    current = state.state
    if current == S.SAFETY_STATE_RECOVERY_REQUIRED:
        return answer(state, O.SAFETY_REQUEST_OUTCOME_REFUSED_RECOVERY_REQUIRED, "safety state was lost; local recovery first", state.causes)
    if current == S.SAFETY_STATE_CLEAR:
        return answer(state, O.SAFETY_REQUEST_OUTCOME_REFUSED_NOT_STOPPED)
    if auth.reset_refusal is not None:
        return answer(state, auth.reset_refusal, auth.detail, state.causes)
    if req.expected_stopped_fence != state.fence:
        return answer(state, O.SAFETY_REQUEST_OUTCOME_REFUSED_STALE_FENCE, "expected_stopped_fence is not the current fence", state.causes)
    # Clears only the clearable causes, and only when no non-resettable one remains; otherwise
    # nothing is cleared and the generation does not move (#82; flow partial_reset).
    stuck = tuple(c for c in state.causes if not c.clearable_remotely)
    if stuck:
        return answer(state, O.SAFETY_REQUEST_OUTCOME_PARTIAL, "causes remain that a RESET cannot clear", stuck)
    st = _with_causes(state, (), now_ms, who=req.controller_id, why=req.reason or "RESET")
    # A RESET re-arms nothing: CLEAR only makes arming possible again; nothing here arms.
    return answer(st, O.SAFETY_REQUEST_OUTCOME_ACCEPTED)


# ---------------------------------------------------------------------------- inputs


def apply_physical_stop(state: State, pressed: bool, now_ms: int, source: str = "physical-stop") -> State:
    """Pressed: a PHYSICAL_STOP cause that no RESET can clear. Released: the cause stays, now
    clearable — "it clears when the button is released, and then a RESET" (SafetyState.proto)."""
    present = [c for c in state.causes if c.type == T.CAUSE_TYPE_PHYSICAL_STOP]
    others = [c for c in state.causes if c.type != T.CAUSE_TYPE_PHYSICAL_STOP]
    if pressed:
        if present and not any(c.clearable_remotely for c in present):
            return state
        return _with_causes(state, (*others, Cause(T.CAUSE_TYPE_PHYSICAL_STOP, source, now_ms, False)), now_ms, who=source, why="physical stop pressed")
    if not present or all(c.clearable_remotely for c in present):
        return state
    released = [replace(c, clearable_remotely=True) for c in present]
    return _with_causes(state, (*others, *released), now_ms, who=source, why="physical stop released")


def apply_legacy_stop(state: State, now_ms: int, source: str) -> State:
    """A stop taken on the legacy path -- a direct `vehicle_lifecycle/v1 emergency_stop`, seen in the
    vehicle's own telemetry -- converges here as EXTERNAL_LEGACY_ESTOP (protocol flow
    legacy_stop_converges). The caller passes only an EDGE into stopped (service.on_vehicle_stopped).

    CLEARABLE BY A RESET (Ted, 2026-11-04). A cause that could not clear while the vehicle reports
    itself stopped would deadlock: the gate refuses every arm unless CLEAR, so she could never be
    armed again. A RESET by the profile's holder clears it like a STOP's own cause; she stays stopped
    until somebody arms her, and a RESET re-arms nothing.

    NOTHING WHEN SHE IS ALREADY STOPPED BY A STOP-CLASS CAUSE: the stop the supervisor itself just
    actuated comes back as exactly this edge, and must not become a second cause."""
    if any(c.type in STOP_CLASS for c in state.causes):
        return state
    cause = Cause(T.CAUSE_TYPE_EXTERNAL_LEGACY_ESTOP, source, now_ms, True, reason="stop observed on the legacy path")
    return _with_causes(state, (*state.causes, cause), now_ms, who=source, why="legacy emergency stop observed")


def apply_lockout(state: State, engaged: bool, now_ms: int, *, simulated: bool, source: str = "local-lockout") -> State:
    """The local maintenance lockout (I14). Above every lease; no remote role clears it; clearing it
    removes its own cause and re-arms nothing."""
    others = [c for c in state.causes if c.type != T.CAUSE_TYPE_LOCKOUT]
    if engaged:
        if state.local_lockout:
            return state
        st = _with_causes(state, (*others, Cause(T.CAUSE_TYPE_LOCKOUT, source, now_ms, False)), now_ms, who=source, why="local lockout engaged")
        return replace(st, local_lockout=True, lockout_simulated=simulated)
    if not state.local_lockout:
        return state
    st = _with_causes(state, others, now_ms, who=source, why="local lockout cleared locally")
    return replace(st, local_lockout=False, lockout_simulated=simulated)


# ---------------------------------------------------------------------------- recovery


def recovery_required(now_ms: int, reason: str) -> State:
    """A missing, corrupt or inconsistent store (I17, inv. 9): MORE inhibited, never less. A fresh
    term, because nothing from the lost life may be replayed into this one."""
    lost = Cause(T.CAUSE_TYPE_SAFETY_STATE_LOST, "safety-supervisor", now_ms, False, reason=reason)
    return State(causes=(lost,), fence=Fence.fresh(), stopped_at_ms=now_ms, who="safety-supervisor", why=reason)


def recover_locally(state: State, now_ms: int, operator: str) -> State:
    """The local / maintenance recovery procedure: STOPPED with a clearable trip, never CLEAR
    (inv. 9). The causes the inputs prove are added by the caller afterwards."""
    if state.state != S.SAFETY_STATE_RECOVERY_REQUIRED:
        return state
    trip = Cause(T.CAUSE_TYPE_SAFETY_TRIP, operator, now_ms, True, reason="local recovery")
    kept = [c for c in state.causes if c.type != T.CAUSE_TYPE_SAFETY_STATE_LOST]
    return _with_causes(state, (*kept, trip), now_ms, who=operator, why="recovered locally")

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
    StartWarning,
)

T = SafetyCause.Type
S = SafetyState.State
O = SafetyRequestOutcome  # noqa: E741 - the enum's alias, like T, S and C
C = EmergencyCommand
W = StartWarning.State

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
    # ENVELOPE only: when the watchdog last re-sent its STOP (#291). While it keeps re-sending she is
    # still out of her limits, and the cause stays non-clearable (`age_envelope`). Not on the wire.
    heard_ms: int | None = None


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
    # The caller is a policy-named envelope watchdog: its STOP is CAUSE_TYPE_ENVELOPE (#291). LAST, so the
    # positional Authorisation(may_trip, refusal, detail) calls keep their meaning.
    envelope: bool = False


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


def _envelope_stop(state: State, req: Request, now_ms: int) -> tuple[State, int, str, tuple]:
    """A STOP from a policy-named envelope watchdog (#291): CAUSE_TYPE_ENVELOPE, NOT clearable while the
    watchdog keeps re-sending it -- she is still outside a hard limit, so a RESET is PARTIAL (Ted,
    2026-11-06). One cause per watchdog: a re-send refreshes it in place (`heard_ms`, the reason naming
    the latest breach) and moves no generation, because the cause set has not changed."""
    held = next((c for c in state.causes if c.type == T.CAUSE_TYPE_ENVELOPE and c.source == req.controller_id), None)
    if held is None:
        cause = Cause(T.CAUSE_TYPE_ENVELOPE, req.controller_id, now_ms, False, req.request_id, reason=req.reason,
                      heard_ms=now_ms)
        st = _with_causes(state, (*state.causes, cause), now_ms, who=req.controller_id, why=req.reason or "envelope breached")
    else:
        fresh = replace(held, clearable_remotely=False, heard_ms=now_ms, reason=req.reason or held.reason)
        st = replace(state, causes=tuple(fresh if c is held else c for c in state.causes))
    return st, O.SAFETY_REQUEST_OUTCOME_ACCEPTED, "", st.causes


def age_envelope(state: State, now_ms: int, hold_ms: int) -> State:
    """An ENVELOPE cause whose watchdog has not re-sent its STOP for `hold_ms` becomes clearable: she is
    back inside (or the watchdog is dead, which the supervisor alarms on separately). It is not cleared
    here -- a RESET by the profile's holder does that, as for a released physical stop."""
    aged = tuple(replace(c, clearable_remotely=True)
                 if c.type == T.CAUSE_TYPE_ENVELOPE and not c.clearable_remotely and c.heard_ms is not None
                 and now_ms - c.heard_ms > hold_ms else c
                 for c in state.causes)
    return state if aged == state.causes else replace(state, causes=aged)


def malformed(req: Request, vessel_id: str) -> str | None:
    if not req.request_id or "/" in req.request_id or "*" in req.request_id:
        return "request_id must be one key chunk"
    if req.vessel_id != vessel_id:
        return f"request names vessel {req.vessel_id!r}, this supervisor is {vessel_id!r}"
    if req.command not in (C.EMERGENCY_COMMAND_STOP, C.EMERGENCY_COMMAND_RESET, C.EMERGENCY_COMMAND_START_WARNING):
        return "command must be STOP, RESET or START_WARNING"
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
        if auth.envelope:
            return answer(*_envelope_stop(state, req, now_ms))
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


# ---------------------------------------------------------------------------- the start warning


@dataclass(frozen=True)
class WarningConfig:
    """The supervisor's own configuration of the on-board warning (policy.warning_config)."""

    fitted: bool = False  # a working device exists
    simulated: bool = False
    profile: str | None = None  # None: no adopted policy
    hold_ms: int = 10_000
    valid_ms: int = 60_000


@dataclass(frozen=True)
class WarningState:
    """The on-board warning of impending remote operation (SARUMS A3.24; keelson #428).

    BESIDE THE STATE, NOT IN IT: it changes no `state`, adds no cause and mints no generation. Not
    persisted either: a restart comes back IDLE, which holds the next start."""

    state: int
    requested_by: str = ""
    started_at_ms: int | None = None  # wall clock, for reporting
    since_mono_ms: int = 0  # when the current SOUNDING or SATISFIED began: durations are measured on this


def idle_warning(cfg: WarningConfig) -> WarningState:
    """What a warning at rest reads. No device is NOT_FITTED only where a vessel may go without one: in the
    operational profile, or with no adopted policy, it is FAULT, which holds every start."""
    if cfg.fitted:
        return WarningState(W.START_WARNING_STATE_IDLE)
    if cfg.profile in ("test", "legacy_single_operator"):
        return WarningState(W.START_WARNING_STATE_NOT_FITTED)
    return WarningState(W.START_WARNING_STATE_FAULT)


def tick_warning(w: WarningState, state: State, cfg: WarningConfig, mono_ms: int) -> WarningState:
    """The warning, as time and the safety state have moved it. Any departure from CLEAR ends it, and
    SATISFIED lasts `valid_ms`, so a warning cannot be banked."""
    if w.state not in (W.START_WARNING_STATE_SOUNDING, W.START_WARNING_STATE_SATISFIED):
        return w
    if state.state != S.SAFETY_STATE_CLEAR or state.local_lockout:
        return idle_warning(cfg)
    held = mono_ms - w.since_mono_ms
    if w.state == W.START_WARNING_STATE_SOUNDING:
        return replace(w, state=W.START_WARNING_STATE_SATISFIED, since_mono_ms=mono_ms) if held >= cfg.hold_ms else w
    return idle_warning(cfg) if held >= cfg.valid_ms else w


def request_warning(w: WarningState, state: State, req: Request, may_warn: bool, cfg: WarningConfig, now_ms: int, mono_ms: int,
                    vessel_id: str) -> tuple[WarningState, Result]:
    """Decide one START_WARNING. The ledger (service.py) guarantees it runs at most once per request_id. A
    request is not a warning: only `safety_state.start_warning` says one was given."""

    def answer(warning: WarningState, outcome: int, detail: str = "") -> tuple[WarningState, Result]:
        return warning, Result(req.request_id, req.command, outcome, state.fence, now_ms, (), detail)

    bad = malformed(req, vessel_id)
    if bad:
        return answer(w, O.SAFETY_REQUEST_OUTCOME_REFUSED_MALFORMED, bad)
    if not may_warn:
        return answer(w, O.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED, "not a START_WARNING principal")
    if state.state != S.SAFETY_STATE_CLEAR or state.local_lockout:
        return answer(w, O.SAFETY_REQUEST_OUTCOME_REFUSED_NOT_CLEAR, "the vessel is not CLEAR")
    if w.state in (W.START_WARNING_STATE_NOT_FITTED, W.START_WARNING_STATE_FAULT):
        return answer(w, O.SAFETY_REQUEST_OUTCOME_REFUSED_NO_WARNING_DEVICE, "this vessel has no working warning device")
    if w.state == W.START_WARNING_STATE_SOUNDING:
        return answer(w, O.SAFETY_REQUEST_OUTCOME_ACCEPTED, "already sounding")  # one warning, however many ask
    return answer(WarningState(W.START_WARNING_STATE_SOUNDING, req.controller_id, now_ms, mono_ms), O.SAFETY_REQUEST_OUTCOME_ACCEPTED)


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

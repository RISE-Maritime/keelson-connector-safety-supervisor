"""Model ↔ keelson protobuf, and model ↔ the JSON the store keeps. No decisions here."""

from __future__ import annotations

from dataclasses import asdict

from google.protobuf.timestamp_pb2 import Timestamp
from keelson.payloads.Fence_pb2 import Fence as FencePb
from keelson.payloads.SafetyState_pb2 import EmergencyRequest, SafetyCause, SafetyRequestResult, SafetyState

from .model import Cause, Fence, Request, Result, State


def _ts(ms: int | None) -> Timestamp | None:
    if ms is None:
        return None
    t = Timestamp()
    t.FromMilliseconds(int(ms))
    return t


def fence_to_pb(f: Fence) -> FencePb:
    return FencePb(term=f.term, generation=f.generation)


def fence_from_pb(pb) -> Fence | None:
    if pb is None or not pb.term or pb.generation == 0:
        return None  # empty term / generation 0 reads as absent (Fence.proto)
    return Fence(pb.term, pb.generation)


def cause_to_pb(c: Cause) -> SafetyCause:
    pb = SafetyCause(type=c.type, source=c.source, clearable_remotely=c.clearable_remotely,
                     safe_state_name=c.safe_state_name, reason=c.reason)
    pb.since.CopyFrom(_ts(c.since_ms))
    if c.request_id:
        pb.request_id = c.request_id
    return pb


def state_to_pb(state: State, *, vessel_id: str, supervisor_id: str, now_ms: int) -> SafetyState:
    pb = SafetyState(vessel_id=vessel_id, supervisor_id=supervisor_id, state=state.state,
                     local_lockout=state.local_lockout, lockout_simulated=state.lockout_simulated,
                     who=state.who, why=state.why)
    pb.timestamp.CopyFrom(_ts(now_ms))
    pb.safety_generation.CopyFrom(fence_to_pb(state.fence))
    pb.active_causes.extend(cause_to_pb(c) for c in state.causes)
    if state.stopped_at_ms is not None:
        pb.stopped_at.CopyFrom(_ts(state.stopped_at_ms))
    safe = [c.safe_state_name for c in state.causes if c.safe_state_name]
    if safe:
        pb.safe_state_name = safe[0]
    return pb


def result_to_pb(r: Result, *, vessel_id: str, supervisor_id: str) -> SafetyRequestResult:
    pb = SafetyRequestResult(vessel_id=vessel_id, request_id=r.request_id, supervisor_id=supervisor_id,
                             command=r.command, outcome=r.outcome, detail=r.detail)
    pb.processed_fence.CopyFrom(fence_to_pb(r.processed_fence))
    pb.processed_at.CopyFrom(_ts(r.processed_at_ms))
    pb.remaining_causes.extend(cause_to_pb(c) for c in r.remaining_causes)
    return pb


def request_from_pb(pb: EmergencyRequest) -> Request:
    expected = fence_from_pb(pb.expected_stopped_fence) if pb.HasField("expected_stopped_fence") else None
    return Request(
        vessel_id=pb.vessel_id, request_id=pb.request_id, command=pb.command,
        controller_id=pb.controller_id, controller_site=pb.controller_site, principal=pb.principal,
        reason=pb.reason, expected_stopped_fence=expected,
        overall_assignment_id=pb.overall_assignment_id,
        overall_assignment_revision=pb.overall_assignment_revision,
    )


# ---------------------------------------------------------------------------- store JSON


def state_to_json(s: State) -> dict:
    return asdict(s)


def state_from_json(d: dict) -> State:
    return State(
        causes=tuple(Cause(**c) for c in d["causes"]),
        fence=Fence(**d["fence"]),
        local_lockout=bool(d["local_lockout"]),
        lockout_simulated=bool(d["lockout_simulated"]),
        stopped_at_ms=d["stopped_at_ms"],
        who=d["who"],
        why=d["why"],
    )


def result_to_json(r: Result) -> dict:
    return asdict(r)


def result_from_json(d: dict) -> Result:
    return Result(
        request_id=d["request_id"], command=d["command"], outcome=d["outcome"],
        processed_fence=Fence(**d["processed_fence"]), processed_at_ms=d["processed_at_ms"],
        remaining_causes=tuple(Cause(**c) for c in d["remaining_causes"]), detail=d["detail"],
    )


def truth_record(pb_bytes: bytes) -> dict:
    """Slipway's truth record: a keelson.TimestampedString whose value is JSON."""
    import json

    from keelson.payloads.Primitives_pb2 import TimestampedString

    msg = TimestampedString()
    msg.ParseFromString(pb_bytes)
    return json.loads(msg.value)


def vehicle_armed(pb_bytes: bytes) -> bool:
    from keelson.payloads.Primitives_pb2 import TimestampedBool

    msg = TimestampedBool()
    msg.ParseFromString(pb_bytes)
    return bool(msg.value)


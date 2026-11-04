#!/usr/bin/env python3
"""keelson-connector-safety-supervisor — the onboard safety supervisor (COMMAND-ARCHITECTURE.md §3).

Its OWN Zenoh session and its own store; it needs nothing else alive for a STOP to land. It reads
`emergency_request`, `command_assignment` (the master, operational profile), the conn lease
(test / legacy profiles) and the authority manager's liveliness; it writes `safety_state` and
`safety_processed_request`. It never reads the gate, the navigator or mavlink2keelson.

Boot (protocols/emergency_request.yaml, action `restore`): restore the state or RECOVERY_REQUIRED →
add what the inputs prove → republish unpublished results → declare the subscribers (they only
enqueue) → GET the retained requests (enqueue) → declare liveliness → one loop decides, in order.
"""

import argparse
import logging
import pathlib
import queue
import sys
import time

import keelson
import zenoh
from keelson.payloads.CommandAuthority_pb2 import CommandAssignment, CommandAuthority
from keelson.payloads.SafetyState_pb2 import EmergencyRequest
from keelson.scaffolding import add_common_arguments, create_zenoh_config, declare_publisher, setup_logging
from keelson.scaffolding.liveliness import declare_liveliness
from keelson.scaffolding.signals import GracefulShutdown

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from safety_supervisor import codec
from safety_supervisor.actuation import LoggingActuator, SlipwayLifecycleActuator, ThreadedActuator
from safety_supervisor.inputs import NoInputs, SimulatedInputs, TruthInputs
from safety_supervisor.policy import resolve
from safety_supervisor.projection import Projections
from safety_supervisor.service import Supervisor
from safety_supervisor.store import Store

log = logging.getLogger("safety-supervisor")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safety-supervisor2keelson", description=__doc__.split("\n")[0])
    add_common_arguments(p)
    p.add_argument("-r", "--realm", required=True, help="Keelson realm, e.g. rise")
    p.add_argument("-e", "--entity-id", required=True, help="The vessel this supervisor protects (the vessel_id)")
    p.add_argument("-s", "--source-id", default="safety-supervisor", help="Source id (default: safety-supervisor)")
    p.add_argument("--supervisor-id", default="safety-supervisor/0", help="SafetyState.supervisor_id, the slot's writer")
    p.add_argument("--state-dir", type=pathlib.Path, required=True, help="Directory of the supervisor's own store (a persistent volume)")
    p.add_argument("--policy", type=pathlib.Path, help="SafetyPolicySnapshot JSON file")
    p.add_argument("--accept-policy", action="store_true", help="MAINTENANCE: adopt --policy into the store")
    p.add_argument("--recover-locally", metavar="OPERATOR", help="MAINTENANCE: leave RECOVERY_REQUIRED for STOPPED (never CLEAR)")
    sim = p.add_mutually_exclusive_group()
    sim.add_argument("--sim-inputs", type=pathlib.Path, help="test profile: JSON {physical_stop, lockout} simulating the onboard inputs")
    sim.add_argument("--sim-truth", action="store_true",
                     help="test profile: read the lockout (and, with --legacy-observe slipway, her stop latch) from Slipway's truth record")
    p.add_argument("--truth-ttl-s", type=float, default=5.0, help="log when the truth record is silent this long (it never releases)")
    p.add_argument("--legacy-observe", metavar="slipway|mavlink:<source_id>",
                   help="observe legacy stops: Slipway's truth-record latch, or a MAVLink vehicle_armed source")
    p.add_argument("--actuator", choices=["logging", "slipway"], default="logging", help="safety actuation (default: logging only)")
    p.add_argument("--slipway-responder-id", default="sim", help="the Slipway vessel's vehicle_lifecycle/v1 responder (default sim)")
    p.add_argument("--actuator-timeout-s", type=float, default=2.0, help="timeout of one actuator call")
    p.add_argument("--heartbeat-s", type=float, default=5.0, help="safety_state heartbeat period (default 5 s)")
    p.add_argument("--retention-days", type=float, default=7.0, help="ledger anti-replay retention (default 7 days)")
    p.add_argument("--boot-get-timeout-s", type=float, default=5.0, help="timeout of the boot GET of retained requests")
    p.add_argument("--retained-poll-s", type=float, default=5.0,
                   help="re-read the retained requests this often: a STOP published while partitioned still lands (default 5 s)")
    return p


def _decode(sample, cls):
    _, _, inner = keelson.uncover(sample.payload.to_bytes())
    msg = cls()
    msg.ParseFromString(inner)
    return msg


def main() -> int:
    args = build_parser().parse_args()
    setup_logging(level=args.log_level)
    zenoh.init_log_from_env_or(logging.getLevelName(args.log_level))

    realm, vessel = args.realm, args.entity_id
    base = f"{realm}/@v0/{vessel}/pubsub"
    keys = {
        "state": f"{base}/safety_state/{vessel}",
        "result": f"{base}/safety_processed_request/{vessel}",
        "requests": f"{base}/emergency_request/{vessel}/*",
        "assignment": f"{base}/command_assignment/{vessel}",
        "conn": f"{base}/command_authority/{vessel}",
    }

    store = Store(args.state_dir / "safety.sqlite")
    policy = resolve(store, args.policy, args.accept_policy)
    if policy is None:
        log.error("no adopted SafetyPolicySnapshot: every STOP lands on the default policy, every RESET is refused")
    else:
        log.info("policy revision %s, profile %s", policy.config_revision, policy.command_profile)
    truth = TruthInputs(ttl_ms=int(args.truth_ttl_s * 1000)) if args.sim_truth else None
    inputs = truth or (SimulatedInputs(args.sim_inputs) if args.sim_inputs else NoInputs())
    legacy = args.legacy_observe
    if legacy and legacy != "slipway" and not legacy.startswith("mavlink:"):
        log.error("--legacy-observe must be 'slipway' or 'mavlink:<source_id>'")
        return 2
    if legacy == "slipway" and truth is None:
        log.error("--legacy-observe slipway reads the truth record: it needs --sim-truth")
        return 2
    # Review B (2026-11-04): a simulated input is a remotely writable key or a local file standing in
    # for hardware -- a forged truth record can clear a lockout. Never outside the test profile.
    if (args.sim_truth or args.sim_inputs) and (policy is None or policy.command_profile != "test"):
        log.error("refusing to start: --sim-truth / --sim-inputs are for the test profile only (profile %r)",
                  policy.command_profile if policy else None)
        return 2
    if args.actuator == "slipway":
        refusal = SlipwayLifecycleActuator.guard(policy.command_profile if policy else None, args.sim_truth)
        if refusal:
            log.error("refusing to start: %s", refusal)
            return 2
    log.warning("independence level L1: own process and Zenoh session; shares the router and the box (§3.4)")

    conf = create_zenoh_config(args.mode, args.connect, args.listen, zenoh_config=args.zenoh_config)
    events: queue.Queue[tuple] = queue.Queue()

    with zenoh.open(conf) as session:
        state_pub = declare_publisher(session, keys["state"])

        def publish_state(st):
            state_pub.put(keelson.enclose(codec.state_to_pb(st, vessel_id=vessel, supervisor_id=args.supervisor_id,
                                                            now_ms=int(time.time() * 1000)).SerializeToString()))

        def publish_result(r):
            session.put(f"{keys['result']}/{r.request_id}",
                        keelson.enclose(codec.result_to_pb(r, vessel_id=vessel, supervisor_id=args.supervisor_id).SerializeToString()))

        if args.actuator == "slipway":
            from keelson.interfaces.VehicleCommon_pb2 import CommandResult
            from keelson.interfaces.VehicleLifecycle_pb2 import EmergencyStopRequest, EmergencyStopResponse

            stop_key = keelson.construct_rpc_key(realm, vessel, "vehicle_lifecycle", "v1", "emergency_stop", args.slipway_responder_id)

            def call_emergency_stop():
                for reply in session.get(stop_key, payload=EmergencyStopRequest().SerializeToString(), timeout=args.actuator_timeout_s):
                    if reply.ok is None:
                        return False, "error reply"
                    resp = EmergencyStopResponse()
                    resp.ParseFromString(reply.ok.payload.to_bytes())
                    return resp.result == CommandResult.COMMAND_RESULT_ACCEPTED, resp.detail or CommandResult.Name(resp.result)
                return False, f"no answer from {args.slipway_responder_id} within {args.actuator_timeout_s} s"

            from keelson.payloads.Alarm_pb2 import Alarm

            def on_rearmed(why):
                session.put(f"{base}/alarm/{args.source_id}", keelson.enclose(Alarm(
                    identifier=f"{args.source_id}/rearmed-while-stopped",
                    description=f"{vessel} was armed on the legacy path while safety_state inhibits ({why}): stopped again",
                    category=Alarm.Category.CATEGORY_SAFETY, priority=Alarm.Priority.PRIORITY_WARNING).SerializeToString()))

            actuator = ThreadedActuator(SlipwayLifecycleActuator(call_emergency_stop), on_rearmed=on_rearmed).start()
            log.warning("SIL actuator: an accepted STOP calls %s's vehicle_lifecycle/v1 emergency_stop (off the decision loop)", vessel)
        else:
            actuator = LoggingActuator()

        projections = Projections(store)
        sup = Supervisor(store=store, vessel_id=vessel, policy=policy, projections=projections,
                         publish_state=publish_state, publish_result=publish_result, actuator=actuator)
        sup.boot(inputs.read(), recover_operator=args.recover_locally)
        log.info("booted: %s, fence %s/%s", keelson.payloads.SafetyState_pb2.SafetyState.State.Name(sup.state.state),
                 sup.state.fence.term[:8], sup.state.fence.generation)

        # Subscribers first; they only enqueue. One loop below decides.
        subs = [
            session.declare_subscriber(keys["requests"], lambda s: events.put(("request", s))),
            session.declare_subscriber(keys["assignment"], lambda s: events.put(("assignment", s))),
            session.declare_subscriber(keys["conn"], lambda s: events.put(("conn", s))),
        ]
        if truth is not None:
            subs.append(session.declare_subscriber(f"{base}/raw_json/truth", lambda s: events.put(("truth", s))))
        if legacy and legacy.startswith("mavlink:"):
            subs.append(session.declare_subscriber(f"{base}/vehicle_armed/{legacy.split(':', 1)[1]}", lambda s: events.put(("armed", s))))
        if policy and policy.manager_source_id:
            mkey = keelson.construct_source_liveliness_key(realm, vessel, policy.manager_source_id)
            subs.append(session.liveliness().declare_subscriber(mkey, history=True, handler=lambda s: events.put(("manager", s))))

        # Retained requests: a STOP published while this process was down must still land.
        retained = 0
        for reply in session.get(keys["requests"], timeout=args.boot_get_timeout_s):
            if reply.ok is not None:
                events.put(("request", reply.ok))
                retained += 1
        log.info("boot: %d retained request(s) queued", retained)

        with declare_liveliness(session, realm, vessel, args.source_id, pubsub_subjects=["safety_state", "safety_processed_request"]), \
                GracefulShutdown() as shutdown:
            last_hb = last_gc = last_poll = time.monotonic()
            while not shutdown.is_requested():
                try:
                    kind, sample = events.get(timeout=0.25)
                except queue.Empty:
                    kind = None
                try:
                    if kind == "request":
                        sup.handle_request(codec.request_from_pb(_decode(sample, EmergencyRequest)))
                    elif kind == "retained":
                        sup.handle_retained(codec.request_from_pb(_decode(sample, EmergencyRequest)))
                    elif kind == "assignment":
                        projections.on_command_assignment(_decode(sample, CommandAssignment), policy)
                    elif kind == "conn":
                        projections.on_conn_lease(_decode(sample, CommandAuthority))
                    elif kind == "manager":
                        projections.on_manager_liveliness(sample.kind == zenoh.SampleKind.PUT)
                    elif kind == "truth":
                        truth.on_truth(codec.truth_record(keelson.uncover(sample.payload.to_bytes())[2]), int(time.monotonic() * 1000))
                        if legacy == "slipway" and truth.estop() is not None:
                            sup.on_vehicle_stopped("slipway", truth.estop())
                        if isinstance(actuator, ThreadedActuator) and truth.estop() is not None:
                            actuator.vessel_latch(truth.estop())
                    elif kind == "armed":
                        armed = codec.vehicle_armed(keelson.uncover(sample.payload.to_bytes())[2])
                        sup.on_vehicle_stopped(legacy, not armed)
                except Exception:  # pylint: disable=broad-except
                    log.exception("could not handle a %s sample", kind)
                sup.on_inputs(truth.read(int(time.monotonic() * 1000)) if truth is not None else inputs.read())
                now = time.monotonic()
                if now - last_hb >= args.heartbeat_s:
                    sup.heartbeat()
                    last_hb = now
                # HIL #86 found it: the boot GET alone misses a STOP published while this process was
                # partitioned but alive -- its live publish was never heard. Storage keeps it; read it back.
                if now - last_poll >= args.retained_poll_s:
                    last_poll = now
                    try:
                        for reply in session.get(keys["requests"], timeout=1.0):
                            if reply.ok is not None:
                                events.put(("retained", reply.ok))
                    except Exception:  # pylint: disable=broad-except
                        log.exception("the retained-request poll failed")
                if now - last_gc >= 3600:
                    sup.gc(int(args.retention_days * 86_400_000))
                    last_gc = now
        if isinstance(actuator, ThreadedActuator):
            actuator.close()  # before the session it calls through is closed
        for s in subs:
            s.undeclare()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""keelson-connector-safety-supervisor — the onboard safety supervisor (COMMAND-ARCHITECTURE.md §3).

Its OWN Zenoh session and its own store; it needs nothing else alive for a STOP to land. It reads
`emergency_request`, `command_assignment` (the master, operational profile), the conn lease
(test / legacy profiles) and the authority manager's liveliness; it writes `safety_state` and
`safety_processed_request`. It never reads the gate, the ANS or mavlink2keelson. In the test
profile it may hold its OWN MAVLink link to a SITL autopilot (`--sim-mavlink`): the drone's key switch
and its stop, standing in for a key and an output wired into the supervisor (crowsnest-dev#268).

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
from safety_supervisor.actuation import LoggingActuator, MavlinkDisarmActuator, SlipwayLifecycleActuator, ThreadedActuator
from safety_supervisor.inputs import HardwareConfig, HardwareInputs, HardwarePoller, MavlinkRcInputs, NoInputs, SimulatedInputs, TruthInputs
from safety_supervisor.model import WarningConfig
from safety_supervisor.policy import resolve
from safety_supervisor.projection import Projections
from safety_supervisor.retained import RetainedPoll
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
    sim.add_argument("--sim-mavlink", metavar="URL",
                     help="test profile: the SITL drone's key switch (an RC channel) over the supervisor's OWN MAVLink link, "
                          "e.g. tcp:127.0.0.1:5763 (the `sitl` extra)")
    sim.add_argument("--hw-inputs", type=pathlib.Path, metavar="FILE",
                     help="the real physical stop and lockout over Modbus TCP, any profile (config/hw-inputs.example.json)")
    p.add_argument("--truth-ttl-s", type=float, default=5.0, help="log when the truth record is silent this long (it never releases)")
    p.add_argument("--sim-mavlink-rc-channel", type=int, default=9, help="the drone's lockout key: this RC channel (default 9)")
    p.add_argument("--sim-mavlink-rc-high-us", type=int, default=1700, help="a pulse at or above this many µs is the key turned (default 1700)")
    p.add_argument("--legacy-observe", metavar="slipway|mavlink:<source_id>",
                   help="observe legacy stops: Slipway's truth-record latch, or a MAVLink vehicle_armed source")
    p.add_argument("--actuator", choices=["logging", "slipway", "mavlink"], default="logging",
                   help="safety actuation (default: logging only; mavlink = force-disarm over the --sim-mavlink link)")
    p.add_argument("--slipway-responder-id", default="slipway", help="the Slipway vessel's vehicle_lifecycle/v1 responder (default slipway)")
    p.add_argument("--actuator-timeout-s", type=float, default=2.0, help="timeout of one actuator call")
    p.add_argument("--heartbeat-s", type=float, default=5.0, help="safety_state heartbeat period (default 5 s)")
    p.add_argument("--retention-days", type=float, default=0.0,
                   help="ledger anti-replay retention, counted from the answered request's delete from storage; "
                        "0 (the default) never collects (README)")
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
    rc = (MavlinkRcInputs(args.sim_mavlink_rc_channel, args.sim_mavlink_rc_high_us, ttl_ms=int(args.truth_ttl_s * 1000))
          if args.sim_mavlink else None)
    hardware, poller = None, None
    if args.hw_inputs:
        try:
            hardware = HardwareInputs(HardwareConfig.load(args.hw_inputs))
        except (OSError, ValueError) as e:
            log.error("refusing to start: --hw-inputs %s: %s", args.hw_inputs, e)
            return 2
        poller = HardwarePoller(hardware)
    clocked = truth or rc or hardware  # the inputs whose `read` takes the monotonic now
    inputs = clocked or (SimulatedInputs(args.sim_inputs) if args.sim_inputs else NoInputs())
    legacy = args.legacy_observe
    if legacy and legacy != "slipway" and not legacy.startswith("mavlink:"):
        log.error("--legacy-observe must be 'slipway' or 'mavlink:<source_id>'")
        return 2
    if legacy == "slipway" and truth is None:
        log.error("--legacy-observe slipway reads the truth record: it needs --sim-truth")
        return 2
    # Review B (2026-11-04): a simulated input is a remotely writable key or a local file standing in
    # for hardware -- a forged truth record can clear a lockout. Never outside the test profile.
    if (args.sim_truth or args.sim_inputs or args.sim_mavlink) and (policy is None or policy.command_profile != "test"):
        log.error("refusing to start: --sim-truth / --sim-inputs / --sim-mavlink are for the test profile only (profile %r)",
                  policy.command_profile if policy else None)
        return 2
    if args.actuator == "slipway":
        refusal = SlipwayLifecycleActuator.guard(policy.command_profile if policy else None, args.sim_truth)
        if refusal:
            log.error("refusing to start: %s", refusal)
            return 2
    if args.actuator == "mavlink":
        refusal = MavlinkDisarmActuator.guard(policy.command_profile if policy else None, bool(args.sim_mavlink))
        if refusal:
            log.error("refusing to start: %s", refusal)
            return 2
    if policy is not None and policy.command_profile == "operational" and isinstance(inputs, NoInputs):
        # An error and not a refusal: a supervisor that does not start is no STOP path at all.
        log.error("the operational profile with no --hw-inputs: no physical stop or lockout is being read")
    log.warning("independence level L1: own process and Zenoh session; shares the router and the box (§3.4)")

    conf = create_zenoh_config(args.mode, args.connect, args.listen, zenoh_config=args.zenoh_config)
    events: queue.Queue[tuple] = queue.Queue()

    with zenoh.open(conf) as session:
        state_pub = declare_publisher(session, keys["state"])

        warning_cfg = policy.warning_config() if policy is not None else WarningConfig()

        def publish_state(st, warning):
            state_pub.put(keelson.enclose(codec.state_to_pb(st, vessel_id=vessel, supervisor_id=args.supervisor_id, warning=warning, warning_cfg=warning_cfg,
                                                            now_ms=int(time.time() * 1000)).SerializeToString()))

        def publish_result(r):
            session.put(f"{keys['result']}/{r.request_id}",
                        keelson.enclose(codec.result_to_pb(r, vessel_id=vessel, supervisor_id=args.supervisor_id).SerializeToString()))

        from keelson.payloads.Alarm_pb2 import Alarm

        def raise_alarm(identifier, description):
            session.put(f"{base}/alarm/{args.source_id}", keelson.enclose(Alarm(
                identifier=f"{args.source_id}/{identifier}", description=description,
                category=Alarm.Category.CATEGORY_SAFETY, priority=Alarm.Priority.PRIORITY_WARNING).SerializeToString()))

        def on_rearmed(why):
            session.put(f"{base}/alarm/{args.source_id}", keelson.enclose(Alarm(
                identifier=f"{args.source_id}/rearmed-while-stopped",
                description=f"{vessel} was armed on the legacy path while safety_state inhibits ({why}): stopped again",
                category=Alarm.Category.CATEGORY_SAFETY, priority=Alarm.Priority.PRIORITY_WARNING).SerializeToString()))

        link = None
        if args.sim_mavlink:
            from safety_supervisor.mavlink_link import MavlinkLink  # the `sitl` extra (pymavlink)

            # Only reports; the decision loop decides (the subscribers' rule).
            link = MavlinkLink(args.sim_mavlink, rc_channel=args.sim_mavlink_rc_channel,
                               on_rc=lambda pwm: events.put(("rc", pwm)), on_armed=lambda armed: events.put(("link_armed", armed))).start()

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

            actuator = ThreadedActuator(SlipwayLifecycleActuator(call_emergency_stop), on_rearmed=on_rearmed).start()
            log.warning("SIL actuator: an accepted STOP calls %s's vehicle_lifecycle/v1 emergency_stop (off the decision loop)", vessel)
        elif args.actuator == "mavlink":
            actuator = ThreadedActuator(MavlinkDisarmActuator(lambda: link.force_disarm(args.actuator_timeout_s)),
                                        on_rearmed=on_rearmed).start()
            log.warning("SIL actuator: an inhibit force-disarms %s over the own MAVLink link %s (off the decision loop)",
                        vessel, args.sim_mavlink)
        else:
            actuator = LoggingActuator()

        projections = Projections(store)
        def delete_request(request_id):
            # The protocol's `gc`: an answered request leaves storage (a tombstone stays), so the ledger can
            # be collected after the retention without a stored request coming back as new (#279).
            session.delete(f"{base}/emergency_request/{vessel}/{request_id}")

        sup = Supervisor(store=store, vessel_id=vessel, policy=policy, projections=projections,
                         publish_state=publish_state, publish_result=publish_result, actuator=actuator,
                         delete_request=delete_request)
        def read_inputs():
            return clocked.read(int(time.monotonic() * 1000)) if clocked is not None else inputs.read()

        if poller is not None:
            poller.start()
            # Not a stop just because the first read has not finished. A device still silent is one.
            poller.wait_first()
        sup.boot(read_inputs(), recover_operator=args.recover_locally)
        log.info("booted: %s, fence %s/%s", keelson.payloads.SafetyState_pb2.SafetyState.State.Name(sup.state.state),
                 sup.state.fence.term[:8], sup.state.fence.generation)

        # Subscribers first; they only enqueue. One loop below decides.
        subs = [
            # A DELETE here is a tombstone -- this supervisor's own, for an answered request (#279) -- never a
            # request: decoded, it is an empty EmergencyRequest that logs as malformed on every answer.
            session.declare_subscriber(keys["requests"], lambda s: events.put(("request", s)) if s.kind != zenoh.SampleKind.DELETE else None),
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
        if policy is not None and policy.envelope_watchdog_source_id:
            # A dead envelope watchdog also stops re-sending, which would make its cause clearable while
            # she may still be outside a limit (#291). So its going away is an alarm, never silent.
            wkey = keelson.construct_source_liveliness_key(realm, vessel, policy.envelope_watchdog_source_id)
            subs.append(session.liveliness().declare_subscriber(wkey, history=True, handler=lambda s: events.put(("watchdog", s))))

        # Retained requests: a STOP published while this process was down must still land.
        retained = 0
        for reply in session.get(keys["requests"], timeout=args.boot_get_timeout_s):
            if reply.ok is not None:
                events.put(("request", reply.ok))
                retained += 1
        log.info("boot: %d retained request(s) queued", retained)

        # HIL #86 found it: the boot GET alone misses a STOP published while this process was partitioned
        # but alive. Storage keeps it; this reads it back -- on its own thread, enqueuing only (review A2).
        def retained_get():
            return [r.ok for r in session.get(keys["requests"], timeout=1.0) if r.ok is not None]

        poll = RetainedPoll(retained_get, lambda s: events.put(("retained", s)),
                            lambda s: _decode(s, EmergencyRequest).request_id, period_s=args.retained_poll_s,
                            seen=lambda rid: events.put(("seen", rid))).start()

        with declare_liveliness(session, realm, vessel, args.source_id, pubsub_subjects=["safety_state", "safety_processed_request"]), \
                GracefulShutdown() as shutdown:
            last_hb = last_gc = time.monotonic()
            # A real input is read as often as it is polled, not at the heartbeat of the simulated ones.
            loop_wait_s = min(0.25, hardware.config.poll_ms / 1000) if hardware is not None else 0.25
            while not shutdown.is_requested():
                try:
                    kind, sample = events.get(timeout=loop_wait_s)
                except queue.Empty:
                    kind = None
                try:
                    if kind == "request":
                        sup.handle_request(codec.request_from_pb(_decode(sample, EmergencyRequest)))
                    elif kind == "retained":
                        req = codec.request_from_pb(_decode(sample, EmergencyRequest))
                        sup.handle_retained(req)
                        if req.request_id and store.is_published(req.request_id):
                            poll.mark_published(req.request_id)
                    elif kind == "seen":
                        sup.request_still_stored(sample)  # answered, yet still in storage: delete it again
                    elif kind == "assignment":
                        projections.on_command_assignment(_decode(sample, CommandAssignment), policy)
                    elif kind == "conn":
                        projections.on_conn_lease(_decode(sample, CommandAuthority))
                    elif kind == "manager":
                        projections.on_manager_liveliness(sample.kind == zenoh.SampleKind.PUT)
                    elif kind == "watchdog":
                        if sample.kind == zenoh.SampleKind.DELETE:
                            log.error("the envelope watchdog %s is gone", policy.envelope_watchdog_source_id)
                            raise_alarm("envelope-watchdog-lost",
                                        f"the envelope watchdog {policy.envelope_watchdog_source_id} is gone: no hard limit is "
                                        "watched, and an ENVELOPE cause it raised becomes clearable after its hold")
                        else:
                            log.info("the envelope watchdog %s is up", policy.envelope_watchdog_source_id)
                    elif kind == "truth":
                        truth.on_truth(codec.truth_record(keelson.uncover(sample.payload.to_bytes())[2]), int(time.monotonic() * 1000))
                        if legacy == "slipway" and truth.estop() is not None:
                            sup.on_vehicle_stopped("slipway", truth.estop())
                        if isinstance(actuator, ThreadedActuator) and truth.estop() is not None:
                            actuator.vessel_latch(truth.estop())
                    elif kind == "armed":
                        armed = codec.vehicle_armed(keelson.uncover(sample.payload.to_bytes())[2])
                        sup.on_vehicle_stopped(legacy, not armed)
                    elif kind == "rc":
                        rc.on_rc(sample, int(time.monotonic() * 1000))
                    elif kind == "link_armed":
                        # Her armed flag over the own link is her latch: armed while inhibiting, stop her again.
                        if isinstance(actuator, ThreadedActuator):
                            actuator.vessel_latch(not sample)
                except Exception:  # pylint: disable=broad-except
                    log.exception("could not handle a %s sample", kind)
                sup.on_inputs(read_inputs())
                now = time.monotonic()
                if now - last_hb >= args.heartbeat_s:
                    sup.heartbeat()
                    last_hb = now
                if now - last_gc >= 3600:
                    sup.gc(int(args.retention_days * 86_400_000))
                    last_gc = now
        poll.close()
        if poller is not None:
            poller.close()
        if isinstance(actuator, ThreadedActuator):
            actuator.close()  # before the session it calls through is closed
        if link is not None:
            link.close()
        for s in subs:
            s.undeclare()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

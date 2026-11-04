"""The CLI as a real process, on an ISOLATED loopback Zenoh pair: multicast scouting off on both
sides, so nothing here can reach a shared network or a vessel."""

import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import keelson
import pytest
import zenoh
from keelson.payloads.CommandAuthority_pb2 import CommandAuthority, Role
from keelson.payloads.Fence_pb2 import Fence
from keelson.payloads.SafetyState_pb2 import EmergencyCommand, EmergencyRequest, SafetyRequestOutcome, SafetyRequestResult, SafetyState

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
REALM, V = "e2e", "testvessel"
BASE = f"{REALM}/@v0/{V}/pubsub"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def isolated(conf: zenoh.Config, **kv):
    conf.insert_json5("scouting/multicast/enabled", "false")
    conf.insert_json5("scouting/gossip/enabled", "false")
    for k, v in kv.items():
        conf.insert_json5(k, json.dumps(v))
    return conf


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.port = free_port()
        self.proc = None
        self.session = zenoh.open(isolated(zenoh.Config(), mode="peer", **{"listen/endpoints": [f"tcp/127.0.0.1:{self.port}"], "connect/endpoints": []}))
        self.results, self.states = {}, []
        self._subs = [
            self.session.declare_subscriber(f"{BASE}/safety_processed_request/{V}/*", self._on_result),
            self.session.declare_subscriber(f"{BASE}/safety_state/{V}", self._on_state),
        ]

    def _decode(self, sample, cls):
        msg = cls()
        msg.ParseFromString(keelson.uncover(sample.payload.to_bytes())[2])
        return msg

    def _on_result(self, s):
        r = self._decode(s, SafetyRequestResult)
        self.results[r.request_id] = r

    def _on_state(self, s):
        self.states.append(self._decode(s, SafetyState))

    def start(self, *extra):
        zcfg = self.tmp / "zenoh.json5"
        zcfg.write_text(json.dumps({"mode": "peer", "scouting": {"multicast": {"enabled": False}, "gossip": {"enabled": False}},
                                    "connect": {"endpoints": [f"tcp/127.0.0.1:{self.port}"]}, "listen": {"endpoints": []}}))
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"config_revision": 1, "command_profile": "test"}))
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bin/safety-supervisor2keelson.py"), "-r", REALM, "-e", V, "--zenoh-config", str(zcfg),
             "--state-dir", str(self.tmp / "state"), "--policy", str(policy), "--heartbeat-s", "0.5", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        token = keelson.construct_source_liveliness_key(REALM, V, "safety-supervisor")
        self.wait(lambda: any(True for _ in self.session.liveliness().get(token, timeout=0.5)), what="the supervisor's liveliness token")

    def stop_proc(self):
        if self.proc:
            self.proc.terminate()
            out, _ = self.proc.communicate(timeout=10)
            self.proc = None
            return out
        return ""

    def wait(self, pred, timeout=15.0, what="condition"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc and self.proc.poll() is not None:
                raise AssertionError(f"supervisor exited:\n{self.proc.stdout.read()}")
            if pred():
                return
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}")

    def request(self, command, fence=None, who="ted@ROC-1"):
        rid = uuid.uuid4().hex
        req = EmergencyRequest(vessel_id=V, request_id=rid, command=command, controller_id=who, reason="e2e")
        if fence is not None:
            req.expected_stopped_fence.CopyFrom(fence)
        self.session.put(f"{BASE}/emergency_request/{V}/{rid}", keelson.enclose(req.SerializeToString()))
        self.wait(lambda: rid in self.results, what=f"the result of {rid}")
        return self.results[rid]

    def latest_state(self):
        self.wait(lambda: self.states, what="a safety_state")
        return self.states[-1]

    def close(self):
        out = self.stop_proc()
        for s in self._subs:
            s.undeclare()
        self.session.close()
        return out


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_a_wiped_store_boots_recovery_required(h):
    h.start("--accept-policy")
    assert h.latest_state().state == SafetyState.SAFETY_STATE_RECOVERY_REQUIRED


def test_stop_then_a_stale_reset_then_the_conns_reset(h):
    h.start("--accept-policy", "--recover-locally", "engineer-on-board")
    r = h.request(EmergencyCommand.EMERGENCY_COMMAND_STOP)
    assert r.outcome == SafetyRequestOutcome.SAFETY_REQUEST_OUTCOME_ACCEPTED
    h.wait(lambda: h.states[-1].safety_generation.generation == r.processed_fence.generation, what="the STOP's state")
    assert h.states[-1].state == SafetyState.SAFETY_STATE_STOPPED
    # Before the conn is known, a RESET is unauthorised (the test profile's RESET is the conn's).
    assert h.request(EmergencyCommand.EMERGENCY_COMMAND_RESET, r.processed_fence).outcome == SafetyRequestOutcome.SAFETY_REQUEST_OUTCOME_REFUSED_UNAUTHORISED
    lease = CommandAuthority(vessel_id=V, controller_id="ted@ROC-1", role=Role.ROLE_CONN)
    h.session.put(f"{BASE}/command_authority/{V}", keelson.enclose(lease.SerializeToString()))
    time.sleep(0.3)
    stale = Fence(term=r.processed_fence.term, generation=r.processed_fence.generation - 1)
    assert h.request(EmergencyCommand.EMERGENCY_COMMAND_RESET, stale).outcome == SafetyRequestOutcome.SAFETY_REQUEST_OUTCOME_REFUSED_STALE_FENCE
    ok = h.request(EmergencyCommand.EMERGENCY_COMMAND_RESET, r.processed_fence)
    assert ok.outcome == SafetyRequestOutcome.SAFETY_REQUEST_OUTCOME_ACCEPTED
    assert ok.processed_fence.generation == r.processed_fence.generation + 1
    h.wait(lambda: h.states[-1].state == SafetyState.SAFETY_STATE_CLEAR, what="CLEAR")


def test_a_request_retained_while_the_supervisor_was_down_lands_at_boot(h):
    rid = uuid.uuid4().hex
    req = EmergencyRequest(vessel_id=V, request_id=rid, command=EmergencyCommand.EMERGENCY_COMMAND_STOP, controller_id="ted@ROC-1")
    payload = keelson.enclose(req.SerializeToString())
    # This session stands in for the router's storage of the request.
    q = h.session.declare_queryable(f"{BASE}/emergency_request/{V}/*", lambda query: query.reply(f"{BASE}/emergency_request/{V}/{rid}", payload))
    try:
        h.start("--accept-policy", "--recover-locally", "engineer-on-board")
        h.wait(lambda: rid in h.results, what="the retained STOP's result")
        assert h.results[rid].outcome == SafetyRequestOutcome.SAFETY_REQUEST_OUTCOME_ACCEPTED
    finally:
        q.undeclare()


def test_the_ledger_survives_a_restart(h):
    h.start("--accept-policy", "--recover-locally", "engineer-on-board")
    r = h.request(EmergencyCommand.EMERGENCY_COMMAND_STOP)
    h.stop_proc()
    h.states.clear()
    h.start()  # the adopted policy and the state persist; no maintenance flags
    st = h.latest_state()
    assert st.state == SafetyState.SAFETY_STATE_STOPPED
    assert (st.safety_generation.term, st.safety_generation.generation) == (r.processed_fence.term, r.processed_fence.generation)

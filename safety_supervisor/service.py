"""The supervisor's sequence: boot, requests, inputs, heartbeat. No Zenoh here — publishing is
injected, so the whole sequence runs in tests without a network.

ONE THREAD DECIDES. Every request, input change and heartbeat is handled in order by the caller's
single loop; Zenoh callbacks only enqueue (bin/safety-supervisor2keelson.py).

THE ORDER OF ONE REQUEST (inv. 4): the ledger first (a seen request_id returns its recorded result
and never executes twice), then the decision, then ONE store transaction with the new state and the
ledger entry, then publish, then mark published. A crash between commit and publish is healed at
boot by republishing every unpublished result.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

from . import model
from .inputs import Reading
from .model import Request, Result, State

log = logging.getLogger(__name__)


def wall_ms() -> int:
    return int(time.time() * 1000)


class Supervisor:
    def __init__(self, *, store, vessel_id: str, policy, projections, publish_state: Callable[[State], None],
                 publish_result: Callable[[Result], None], actuator, now_ms: Callable[[], int] = wall_ms,
                 delete_request: Callable[[str], None] | None = None):
        self.store = store
        self.vessel_id = vessel_id
        self.policy = policy
        self.projections = projections
        self._publish_state = publish_state
        self._publish_result = publish_result
        self.actuator = actuator
        self.now_ms = now_ms
        # Removes an answered request's key from storage (protocol action `gc`, crowsnest-dev#279); raises on
        # failure. None deletes nothing, so nothing is ever marked deleted and no ledger row is collected.
        self._delete_request = delete_request
        self.state: State | None = None

    # ------------------------------------------------------------------ boot

    def boot(self, reading: Reading, recover_operator: str | None = None) -> State:
        """Restore a state that can be trusted, or RECOVERY_REQUIRED (I17); add what the inputs prove;
        republish every unpublished result. Requests are read only after this (action `restore`)."""
        now = self.now_ms()
        state, why = self.store.load_state()
        if state is None:
            log.error("SAFETY STATE LOST (%s): RECOVERY_REQUIRED until a local recovery", why)
            state = model.recovery_required(now, why)
            self.store.commit(state)
        if recover_operator:
            recovered = model.recover_locally(state, now, recover_operator)
            if recovered is not state:
                log.warning("local recovery by %s: STOPPED, never CLEAR", recover_operator)
                state = recovered
                self.store.commit(state)
        self.state = state
        self.on_inputs(reading, publish=False)
        for result in self.store.unpublished():
            self._send_result(result)
        self._actuate()
        self._publish_state(self.state)
        return self.state

    # ------------------------------------------------------------------ requests

    def handle_request(self, req: Request) -> Result:
        seen = self.store.result_for(req.request_id) if req.request_id else None
        if seen is not None:
            # The second sighting of a request IS the first answer (no DUPLICATE outcome).
            self._send_result(seen)
            return seen
        auth = self.projections.authorise(req, self.policy)
        new_state, result = model.apply_request(self.state, req, auth, self.now_ms(), self.vessel_id)
        if result.outcome == model.O.SAFETY_REQUEST_OUTCOME_REFUSED_MALFORMED and not req.request_id:
            # Nothing to key a ledger entry by: answer nothing, log it.
            log.error("malformed request with no request_id ignored: %s", result.detail)
            return result
        self.store.commit(new_state, result)  # state + ledger: one transaction
        changed = new_state is not self.state
        self.state = new_state
        if changed:
            self._actuate()
            self._publish_state(self.state)
        self._send_result(result)
        log.info("%s %s from %s → %s", req.request_id, model.C.Name(req.command) if req.command in (1, 2) else req.command,
                 req.controller_id, model.O.Name(result.outcome))
        return result

    def handle_retained(self, req: Request) -> Result | None:
        """A request read back from storage by the periodic poll. One already answered AND published
        is not republished -- the poll must not republish every old result every period -- but it is
        still IN STORAGE, so it is a sighting (`request_still_stored`). Anything else goes through
        `handle_request`, so the ledger still decides (an unpublished result is republished; an
        unknown request is processed once)."""
        if req.request_id and self.store.is_published(req.request_id):
            self.request_still_stored(req.request_id)
            return None
        return self.handle_request(req)

    def request_still_stored(self, request_id: str) -> None:
        """An answered request seen in storage after (or without) its delete. It was not deleted after
        all: its retention clock is void, and it is deleted again. So a key that lingers can never age
        its ledger row out -- the row that keeps a week-old STOP from executing again (#279)."""
        if not self.store.is_published(request_id):
            return
        if self.store.deleted_ms(request_id) is not None:
            log.warning("answered request %s is still in storage: deleting it again, its retention restarts", request_id)
        self.store.stored_again(request_id)
        self._delete(request_id)

    def sweep_deletes(self) -> int:
        """Retry every answered request whose key is not known deleted (a delete that raised, or a row
        from before deletes existed). Returns how many are still not deleted."""
        for rid in self.store.undeleted():
            self._delete(rid)
        return len(self.store.undeleted())

    def handle_requests(self, requests: Iterable[Request]) -> None:
        """Retained requests at boot, in any order: a STOP always lands; a RESET is judged against
        the current fence like any other (flow boot_and_multi_crash_recovery)."""
        for req in requests:
            self.handle_request(req)

    # ------------------------------------------------------------------ inputs / heartbeat

    def on_inputs(self, reading: Reading, publish: bool = True) -> None:
        now = self.now_ms()
        st = model.apply_physical_stop(self.state, reading.physical_stop, now)
        st = model.apply_lockout(st, reading.lockout, now, simulated=reading.simulated)
        # An envelope watchdog that has stopped re-sending its STOP: its cause becomes clearable (#291).
        st = model.age_envelope(st, now, self.policy.envelope_hold_ms if self.policy is not None else 3000)
        if st is self.state:
            return
        self.store.commit(st)
        self.state = st
        if publish:
            self._actuate()
            self._publish_state(self.state)

    def on_vehicle_stopped(self, source: str, stopped: bool) -> None:
        """The legacy-stop observer. Only an EDGE into stopped latches: a vessel that is simply
        disarmed -- at the berth, at boot -- is not an emergency stop.

        The last value per source is persisted (`legacy:<source>`), so an edge that happened while
        this process was down is seen at the next boot. The first value ever seen sets the memory and
        latches nothing. The state commits BEFORE the memory: a crash between the two re-detects the
        same edge, which apply_legacy_stop then ignores (a STOP-class cause is already there)."""
        key = f"legacy:{source}"
        seen = self.store.get_json(key)
        was = None if seen is None else bool(seen.get("stopped"))
        if was is False and stopped:
            st = model.apply_legacy_stop(self.state, self.now_ms(), source)
            if st is not self.state:
                log.warning("legacy emergency stop observed on %s: STOPPED (EXTERNAL_LEGACY_ESTOP)", source)
                self.store.commit(st)
                self.state = st
                self._actuate()
                self._publish_state(self.state)
        if was != stopped:
            self.store.put_json(key, {"stopped": stopped})

    def heartbeat(self) -> None:
        self._actuate()  # an actuator that did not land retries here
        self._publish_state(self.state)
        self.sweep_deletes()  # a request key whose delete failed retries here

    def gc(self, retention_ms: int) -> int:
        """Collect ledger rows whose request key was deleted more than `retention_ms` ago.
        `retention_ms <= 0` NEVER collects, and it is the default (joint review of 130fd57, A1). A row
        collected while its request is still in storage makes that request look new: a STOP from last
        week would execute again. So only a deleted key starts the clock, and any later sighting of
        it in storage restarts it (`request_still_stored`, #279)."""
        if retention_ms <= 0:
            return 0
        return self.store.gc(self.now_ms(), retention_ms)

    # ------------------------------------------------------------------ internals

    def _send_result(self, result: Result) -> None:
        """Publish, mark published, THEN delete the request key: never before the result is out (protocol
        action `gc`). A repeat of the request republishes and deletes it again."""
        self._publish_result(result)
        self.store.mark_published(result.request_id)
        self._delete(result.request_id)

    def _delete(self, request_id: str) -> None:
        """Delete the request key; mark it deleted only when the call returned. A failure is retried at
        the heartbeat and leaves the row uncollectable."""
        if self._delete_request is None or not self.store.is_published(request_id):
            return
        try:
            self._delete_request(request_id)
        except Exception as e:  # noqa: BLE001 - any failure is a key still in storage
            log.warning("could not delete answered request %s from storage (%s): retrying at the heartbeat", request_id, e)
            return
        self.store.mark_deleted(request_id, self.now_ms())

    def _actuate(self) -> None:
        self.actuator.apply(model.inhibiting(self.state), self.state.why)

"""The independent safety actuation interface, chosen per vessel (COMMAND-ARCHITECTURE.md §3):
propulsion inhibit · predefined safe state · local hold · shutdown · a physical output — MAVLink
force-disarm on a second link is the drone case only.

`LoggingActuator` records what an actuator would be told and moves nothing. `SlipwayLifecycleActuator`
is the SIL one for Slipway: it stops a SIMULATED vessel through her own `vehicle_lifecycle/v1
emergency_stop`. `MavlinkDisarmActuator` is the SIL one for the SITL drone: a forced disarm over the
supervisor's own MAVLink link. A real actuator for a real vessel is its own change, with its own
safety review.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class LoggingActuator:
    def __init__(self):
        self.inhibited = None  # unknown until the first apply

    def apply(self, inhibit: bool, why: str) -> None:
        if inhibit == self.inhibited:
            return
        self.inhibited = inhibit
        log.warning("SAFETY ACTUATION (logging only, nothing moved): %s — %s", "INHIBIT" if inhibit else "inhibit released", why)


class _StopUntilAccepted:
    """What every SIL actuator shares: an inhibit edge calls `call()` until a reply is ACCEPTED.

    - Retried on every `apply` while inhibiting, until ACCEPTED: a stop that did not land is not a stop.
    - Release calls NOTHING. A RESET re-arms nothing: she stays stopped until somebody arms her.
    - `call()` is injected (`() -> (accepted: bool, detail: str)`), so this module stays free of Zenoh
      and of pymavlink; the bin builds it."""

    WHAT = "stop"

    def __init__(self, call):
        self._call = call
        self._landed = False
        self.calls = 0

    def relatch(self) -> None:
        """Her own latch was seen RELEASED while the supervisor inhibits -- somebody armed her on the
        legacy path. The stop has not held, so the next `apply` stops her again (review A,
        2026-11-04: never 'moving while safety_state says STOPPED')."""
        self._landed = False

    def apply(self, inhibit: bool, why: str) -> None:
        if not inhibit:
            self._landed = False  # the next inhibit stops her again; nothing is called on release
            return
        if self._landed:
            return
        self.calls += 1
        try:
            accepted, detail = self._call()
        except Exception as e:  # noqa: BLE001 - a stop that could not be sent is retried, never dropped
            accepted, detail = False, str(e)
        if accepted:
            self._landed = True
            log.warning("SAFETY ACTUATION: %s ACCEPTED (%s) — %s", self.WHAT, detail, why)
        else:
            log.error("SAFETY ACTUATION: %s NOT accepted (%s) — retrying", self.WHAT, detail)


class SlipwayLifecycleActuator(_StopUntilAccepted):
    """The SIL actuator for a Slipway vessel: an inhibit edge calls her `vehicle_lifecycle/v1
    emergency_stop` (Slipway ADR-0126: latched, from anyone, ACCEPTED even when already latched or
    locked out). Refused outside the `test` profile at startup (`guard`): it is a simulator's actuator.

    Independence level L1: the supervisor's own session, sharing the router with what it protects."""

    WHAT = "Slipway emergency_stop"

    @staticmethod
    def guard(command_profile: str | None, sim_truth: bool) -> str | None:
        if command_profile != "test":
            return f"the Slipway actuator is for the test profile only, not {command_profile!r}"
        if not sim_truth:
            return "the Slipway actuator needs --sim-truth: it stops a simulated vessel and reads her back"
        return None


class MavlinkDisarmActuator(_StopUntilAccepted):
    """The SIL actuator for the SITL drone (crowsnest-dev#268): an inhibit edge FORCE-DISARMS her over the
    supervisor's own MAVLink link (mavlink_link.py: MAV_CMD_COMPONENT_ARM_DISARM, param2 21196) -- the
    "MAVLink force-disarm on a second link" this module's header names as the drone case. Her armed
    flag, read back over the same link, is her latch: armed while inhibiting, she is stopped again.

    Refused outside the `test` profile and without `--sim-mavlink` (`guard`): it disarms a simulator.
    Independence: its own link to the autopilot, never mavlink2keelson; still L1 (one box)."""

    WHAT = "MAVLink force-disarm (own link)"

    @staticmethod
    def guard(command_profile: str | None, sim_mavlink: bool) -> str | None:
        if command_profile != "test":
            return f"the MAVLink actuator is for the test profile only, not {command_profile!r}"
        if not sim_mavlink:
            return "the MAVLink actuator needs --sim-mavlink: it disarms a SITL autopilot over its own link and reads her back"
        return None


class ThreadedActuator:
    """Runs an actuator OFF the decision loop (review C, 2026-11-04). The loop's `apply` only records
    the latest wanted (inhibit, why) and returns, so `safety_state` and the results are published
    without waiting on a vessel that does not answer. The worker applies the latest wish, and
    re-applies it every `retry_s` while it inhibits, which is how a stop that did not land is retried.

    `vessel_latch(latched)` reports her own stop latch as last heard. If it is released while the
    supervisor inhibits, the worker re-stops her (`inner.relatch()`) and calls `on_rearmed(why)`.

    All of the inner actuator's state is touched by the worker thread only; `run_once` is the
    worker's body, and the tests call it directly."""

    def __init__(self, inner, retry_s: float = 1.0, on_rearmed=None):
        import threading

        self.inner = inner
        self.retry_s = retry_s
        self.on_rearmed = on_rearmed or (lambda why: None)
        self._cv = threading.Condition()
        self._wanted: tuple[bool, str] | None = None
        self._dirty = False
        self._rearmed = False
        self._stop = False
        self._thread = None

    # ---- the decision loop's side: never blocks on the vessel

    def apply(self, inhibit: bool, why: str) -> None:
        with self._cv:
            if self._wanted != (inhibit, why):
                self._wanted, self._dirty = (inhibit, why), True
                self._cv.notify()

    def vessel_latch(self, latched: bool) -> None:
        with self._cv:
            if not latched and self._wanted is not None and self._wanted[0]:
                self._rearmed, self._dirty = True, True
                self._cv.notify()

    # ---- the worker

    def run_once(self, wait: bool = False) -> None:
        with self._cv:
            if wait:
                self._cv.wait_for(lambda: self._dirty or self._stop, timeout=self.retry_s)
            wanted, rearmed = self._wanted, self._rearmed
            self._dirty = self._rearmed = False
        if wanted is None:
            return
        inhibit, why = wanted
        if rearmed and inhibit:
            log.error("her stop latch was RELEASED while the supervisor inhibits (%s): stopping her again", why)
            if hasattr(self.inner, "relatch"):
                self.inner.relatch()
            self.on_rearmed(why)
        self.inner.apply(inhibit, why)

    def start(self) -> ThreadedActuator:
        import threading

        def loop():
            while not self._stop:
                try:
                    self.run_once(wait=True)
                except Exception:
                    log.exception("actuator worker")

        self._thread = threading.Thread(target=loop, name="safety-actuator", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        if self._thread is not None:
            self._thread.join(timeout=5)


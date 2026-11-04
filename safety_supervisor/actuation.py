"""The independent safety actuation interface, chosen per vessel (COMMAND-ARCHITECTURE.md §3):
propulsion inhibit · predefined safe state · local hold · shutdown · a physical output — MAVLink
force-disarm on a second link is the drone case only.

`LoggingActuator` records what an actuator would be told and moves nothing. `SlipwayLifecycleActuator`
is the SIL one: it stops a SIMULATED vessel through her own `vehicle_lifecycle/v1 emergency_stop`.
A real actuator for a real vessel is its own change, with its own safety review.
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


class SlipwayLifecycleActuator:
    """The SIL actuator: an inhibit edge calls the Slipway vessel's `vehicle_lifecycle/v1
    emergency_stop` (Slipway ADR-0126: latched, from anyone, ACCEPTED even when already latched or
    locked out).

    - Retried on every `apply` while inhibiting, until a reply is ACCEPTED: a stop that did not
      land is not a stop.
    - Release calls NOTHING. A RESET re-arms nothing: she stays stopped until somebody arms her.
    - `call()` is injected (`() -> (accepted: bool, detail: str)`), so this module stays free of
      Zenoh; the bin builds it from the RPC key.
    - Refused outside the `test` profile at startup (`guard`): it is a simulator's actuator.

    Independence level L1: the supervisor's own session, sharing the router with what it protects."""

    def __init__(self, call):
        self._call = call
        self._landed = False
        self.calls = 0

    @staticmethod
    def guard(command_profile: str | None, sim_truth: bool) -> str | None:
        if command_profile != "test":
            return f"the Slipway actuator is for the test profile only, not {command_profile!r}"
        if not sim_truth:
            return "the Slipway actuator needs --sim-truth: it stops a simulated vessel and reads her back"
        return None

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
            log.warning("SAFETY ACTUATION: Slipway emergency_stop ACCEPTED (%s) — %s", detail, why)
        else:
            log.error("SAFETY ACTUATION: Slipway emergency_stop NOT accepted (%s) — retrying", detail)


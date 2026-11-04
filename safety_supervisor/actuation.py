"""The independent safety actuation interface, chosen per vessel (COMMAND-ARCHITECTURE.md §3):
propulsion inhibit · predefined safe state · local hold · shutdown · a physical output — MAVLink
force-disarm on a second link is the drone case only.

THIS CUT HAS NONE. `LoggingActuator` records what an actuator would be told and moves nothing. A
real one is its own change, with its own safety review.
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

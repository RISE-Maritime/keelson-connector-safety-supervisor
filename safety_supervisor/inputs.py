"""The onboard inputs the supervisor reads directly: the physical stop and the local maintenance
lockout.

In this cut both are SIMULATED (the `test` profile): a local JSON file `{"physical_stop": bool,
"lockout": bool}` that a person on the vessel (or a test) edits. The state says so —
`lockout_simulated` — on every screen that shows it (COMMAND-ARCHITECTURE.md §3.3). In the
`operational` profile the lockout is a HARDWARE inhibit the supervisor only observes and reports;
that input does not exist yet, and nothing here pretends it does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reading:
    physical_stop: bool
    lockout: bool
    simulated: bool


class NoInputs:
    """No physical inputs wired: proves nothing, so adds no cause."""

    def read(self) -> Reading:
        return Reading(False, False, simulated=False)


class SimulatedInputs:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._last: Reading | None = None

    def read(self) -> Reading:
        try:
            d = json.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, ValueError) as e:
            # An unreadable input file is not "released": keep the last reading.
            log.error("simulated inputs %s unreadable (%s); keeping the last reading", self.path, e)
            return self._last or Reading(False, False, simulated=True)
        self._last = Reading(bool(d.get("physical_stop", False)), bool(d.get("lockout", False)), simulated=True)
        return self._last


class TruthInputs:
    """The SIL stand-in for the onboard inputs: Slipway's truth record (`raw_json/truth`), which
    carries her local lockout switch (Slipway ADR-0140, the `set_lockout` scenario event) and her
    ADR-0126 emergency-stop latch. In SIL the simulator IS the hardware, so this is the lockout input
    the `operational` profile's hardware inhibit stands in for -- and it is labelled simulated.

    SILENCE NEVER RELEASES. Between records -- or after the kernel stops publishing -- the last reading
    stands; a record older than `ttl_ms` is logged, never read as "released". Fed by the bin's
    subscriber (`on_truth`); the decision loop calls `read`."""

    def __init__(self, ttl_ms: int = 5000):
        self.ttl_ms = ttl_ms
        self._lockout = False
        self._estop: bool | None = None
        self._heard_ms: int | None = None
        self._warned = False

    def on_truth(self, record: dict, now_ms: int) -> None:
        self._lockout = bool(record.get("lockout", self._lockout))
        if "estop" in record:
            self._estop = bool(record["estop"])
        self._heard_ms = now_ms
        self._warned = False

    def estop(self) -> bool | None:
        """Her own stop latch, as last heard; None before the first record."""
        return self._estop

    def stale(self, now_ms: int) -> bool:
        return self._heard_ms is None or now_ms - self._heard_ms > self.ttl_ms

    def read(self, now_ms: int | None = None) -> Reading:
        if now_ms is not None and self._heard_ms is not None and self.stale(now_ms) and not self._warned:
            log.error("Slipway's truth record is silent (> %d ms): keeping the last lockout reading", self.ttl_ms)
            self._warned = True
        return Reading(False, self._lockout, simulated=True)


class MavlinkRcInputs:
    """The SITL drone's key switch (crowsnest-dev#268): her RC channel `channel` (RC9 by default), read
    over the supervisor's OWN MAVLink link (mavlink_link.py). A pulse at or above `high_us` is the key
    turned: locked out. Labelled simulated, like every SIL input.

    SILENCE NEVER RELEASES, as for the truth record: between readings, or after the link is lost, the
    last reading stands; "no RC" values never arrive here (the link drops 0 / 65535). Fed by the bin's
    queue (`on_rc`); the decision loop calls `read`."""

    def __init__(self, channel: int = 9, high_us: int = 1700, ttl_ms: int = 5000):
        self.channel, self.high_us, self.ttl_ms = channel, high_us, ttl_ms
        self._lockout = False
        self._heard_ms: int | None = None
        self._warned = False

    def on_rc(self, pwm_us: int, now_ms: int) -> None:
        self._lockout = int(pwm_us) >= self.high_us
        self._heard_ms = now_ms
        self._warned = False

    def stale(self, now_ms: int) -> bool:
        return self._heard_ms is None or now_ms - self._heard_ms > self.ttl_ms

    def read(self, now_ms: int | None = None) -> Reading:
        if now_ms is not None and self._heard_ms is not None and self.stale(now_ms) and not self._warned:
            log.error("the drone's RC%d is silent (> %d ms): keeping the last lockout reading", self.channel, self.ttl_ms)
            self._warned = True
        return Reading(False, self._lockout, simulated=True)


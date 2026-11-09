"""The onboard inputs the supervisor reads directly: the physical stop and the local maintenance
lockout.

Real ones come over Modbus TCP (`HardwareInputs`, crowsnest-dev#277). In the `operational` profile
the lockout is a HARDWARE inhibit the supervisor only observes and reports (COMMAND-ARCHITECTURE.md
§3.3). The rest are SIMULATED, for the `test` profile: a local JSON file `{"physical_stop": bool,
"lockout": bool}` that a person on the vessel (or a test) edits, Slipway's truth record, or the SITL
drone's RC channel. The state says so -- `lockout_simulated` -- on every screen that shows it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from safety_supervisor.modbus import TABLES, ModbusClient, ModbusError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reading:
    physical_stop: bool
    lockout: bool
    simulated: bool
    fault: str | None = None  # the input could not be read: why, and physical_stop is True because of it


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



@dataclass(frozen=True)
class Channel:
    table: str
    address: int
    released: bool  # the raw value when healthy and released; a broken normally-closed loop reads the other


@dataclass(frozen=True)
class HardwareConfig:
    host: str
    physical_stop: Channel
    lockout: Channel
    port: int = 502
    unit: int = 1
    poll_ms: int = 50
    timeout_ms: int = 100
    fault_ms: int = 500
    release_ms: int = 200
    boot_wait_ms: int = 3000

    @staticmethod
    def _channel(d: dict, name: str) -> Channel:
        c = d.get(name)
        if not isinstance(c, dict):
            raise ValueError(f"{name} is required")
        if c.get("table") not in TABLES:
            raise ValueError(f"{name}.table must be one of {sorted(TABLES)}")
        address = c.get("address")
        if type(address) is not int or not 0 <= address <= 65535:
            raise ValueError(f"{name}.address must be 0-65535")
        # Required, never defaulted: a guessed polarity is a stop that reads released when pressed.
        if type(c.get("released")) is not bool:
            raise ValueError(f"{name}.released must be true or false: the raw value when healthy and released")
        return Channel(c["table"], address, c["released"])

    @classmethod
    def load(cls, path: Path) -> HardwareConfig:
        d = json.loads(Path(path).read_text())
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        if not isinstance(d.get("host"), str) or not d["host"]:
            raise ValueError("host is required")
        numbers = {k: d[k] for k in ("port", "unit", "poll_ms", "timeout_ms", "fault_ms", "release_ms", "boot_wait_ms") if k in d}
        for k, v in numbers.items():
            if type(v) is not int or v < 0:
                raise ValueError(f"{k} must be a whole number, not negative")
        cfg = cls(d["host"], cls._channel(d, "physical_stop"), cls._channel(d, "lockout"), **numbers)
        if not 1 <= cfg.port <= 65535 or cfg.unit > 255 or cfg.poll_ms == 0 or cfg.timeout_ms == 0:
            raise ValueError("port must be 1-65535, unit at most 255, poll_ms and timeout_ms above 0")
        # One poll can take a poll period and two read timeouts. Any tighter and a slow poll is a stop.
        if cfg.fault_ms < cfg.poll_ms + 2 * cfg.timeout_ms:
            raise ValueError("fault_ms must be at least poll_ms + 2 * timeout_ms")
        return cfg


def _debounced(held: bool, released_now: bool, since_ms: int | None, now_ms: int, release_ms: int) -> tuple[bool, int | None]:
    """Toward inhibited at once; toward released only after `release_ms` of unbroken released samples."""
    if not released_now:
        return True, None
    if not held:
        return False, None
    since_ms = now_ms if since_ms is None else since_ms
    return (False, None) if now_ms - since_ms >= release_ms else (True, since_ms)


class HardwareInputs:
    """The real stop and lockout, as the poller last read them. Pure: the clock is passed in.

    UNLIKE THE SIMULATED INPUTS, SILENCE STOPS HER. No good sample for `fault_ms`, or none yet, reads
    as a pressed stop. The lockout holds its last reading through that (nobody turned a key because a
    cable fell out); with no reading since boot it counts as engaged."""

    def __init__(self, config: HardwareConfig):
        self.config = config
        self._lock = threading.Lock()
        self._stop = True
        self._lockout: bool | None = None
        self._stop_since: int | None = None
        self._lockout_since: int | None = None
        self._heard_ms: int | None = None
        self._why = "no reading yet"
        self._reported: str | None = None

    def on_sample(self, stop_raw: bool, lockout_raw: bool, now_ms: int) -> None:
        cfg = self.config
        stop_released, lockout_released = stop_raw == cfg.physical_stop.released, lockout_raw == cfg.lockout.released
        with self._lock:
            if self._heard_ms is None:
                # The first reading there has ever been: nothing to debounce against.
                self._stop, self._lockout = not stop_released, not lockout_released
            else:
                if now_ms - self._heard_ms > cfg.fault_ms:
                    # It read as stopped through the gap, so it has to come back clean.
                    self._stop, self._stop_since = True, None
                self._stop, self._stop_since = _debounced(self._stop, stop_released, self._stop_since, now_ms, cfg.release_ms)
                self._lockout, self._lockout_since = _debounced(self._lockout, lockout_released, self._lockout_since, now_ms, cfg.release_ms)
            self._heard_ms = now_ms

    def on_failure(self, why: str, _now_ms: int) -> None:
        with self._lock:
            self._why = why
            self._stop_since = self._lockout_since = None  # released has to be unbroken

    def read(self, now_ms: int) -> Reading:
        with self._lock:
            silent = self._heard_ms is None or now_ms - self._heard_ms > self.config.fault_ms
            if silent:
                if self._reported != self._why:
                    log.error("the hardware inputs are unreadable (%s): reading as a pressed stop", self._why)
                    self._reported = self._why
                return Reading(True, True if self._lockout is None else self._lockout, simulated=False, fault=self._why)
            if self._reported is not None:
                log.warning("the hardware inputs are readable again")
                self._reported = None
            return Reading(self._stop, self._lockout, simulated=False)


class HardwarePoller:
    """Owns the Modbus connection, on its own thread: the decision loop never waits on the device."""

    def __init__(self, inputs: HardwareInputs, client: ModbusClient | None = None):
        cfg = inputs.config
        self.inputs = inputs
        self.client = client or ModbusClient(cfg.host, cfg.port, cfg.unit, cfg.timeout_ms / 1000)
        self._stop = threading.Event()
        self._first = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hw-inputs", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait_first(self) -> bool:
        return self._first.wait(self.inputs.config.boot_wait_ms / 1000)

    def _poll(self) -> None:
        cfg = self.inputs.config
        try:
            stop_raw = self.client.read_bit(cfg.physical_stop.table, cfg.physical_stop.address)
            lockout_raw = self.client.read_bit(cfg.lockout.table, cfg.lockout.address)
        except ModbusError as e:
            self.inputs.on_failure(str(e), int(time.monotonic() * 1000))
            return
        self.inputs.on_sample(stop_raw, lockout_raw, int(time.monotonic() * 1000))
        self._first.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._poll()
            self._stop.wait(self.inputs.config.poll_ms / 1000)

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.client.close()

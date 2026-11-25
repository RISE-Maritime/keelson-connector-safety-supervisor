"""The supervisor's OWN MAVLink link to a SITL autopilot (test profile only; crowsnest-dev#268).

On a real vessel the local lockout is a key wired INTO the supervisor (or the safety PLC), and the
stop is an output the supervisor drives itself. On the SITL drone there is neither, so this link
stands in for both: the drone's RC9 is the key (high = locked out), and a forced disarm over this
same link is the stop. It is a second, independent link to the autopilot -- its own serial port on
the SITL (5763), its own system id -- and it never goes through mavlink2keelson, which is why it does
not break "never reads the gate, the ANS or mavlink2keelson".

One thread owns the connection and only REPORTS: `on_rc(pwm_us)` and `on_armed(armed)` are the
bin's enqueue callbacks, and the decision loop decides. `force_disarm` is called from the actuator's
worker thread and waits for the COMMAND_ACK the reader hands it. pymavlink is imported here only, and
the bin imports this module only when `--sim-mavlink` is given (the optional `sitl` extra).
"""

from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger(__name__)

MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_SET_MESSAGE_INTERVAL = 511
MSG_RC_CHANNELS = 65
FORCE_DISARM_MAGIC = 21196  # param2: disarm even if the vehicle thinks it is moving (what mavlink2keelson's emergency_stop sends a boat)
MAV_RESULT_ACCEPTED = 0
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_AUTOPILOT_INVALID = 8
NO_RC = (0, 65535)  # RC_CHANNELS' "unused" values: no reading, never a released key


class MavlinkLink:
    def __init__(self, url: str, *, on_rc, on_armed, rc_channel: int = 9, rc_rate_hz: float = 5.0,
                 source_system: int = 250, source_component: int = 191, connect=None):
        self.url = url
        self.rc_channel = rc_channel
        self.rc_rate_hz = rc_rate_hz
        self.source_system, self.source_component = source_system, source_component
        self._on_rc, self._on_armed = on_rc, on_armed
        self._connect = connect or self._pymavlink_connect
        self._conn = None
        self._target: tuple[int, int] | None = None
        self._send_lock = threading.Lock()
        self._acks: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _pymavlink_connect(self):
        from pymavlink import mavutil  # the `sitl` extra

        return mavutil.mavlink_connection(self.url, source_system=self.source_system, source_component=self.source_component)

    # ---- the reader thread

    def handle(self, msg) -> None:
        """One received message. Public so the tests can drive it without a socket."""
        kind = msg.get_type()
        if kind == "HEARTBEAT":
            if msg.autopilot == MAV_AUTOPILOT_INVALID:
                return  # a GCS or companion, not the vehicle
            if self._target is None:
                self._target = (msg.get_srcSystem(), msg.get_srcComponent())
            self._on_armed(bool(msg.base_mode & MAV_MODE_FLAG_SAFETY_ARMED))
        elif kind == "RC_CHANNELS":
            if msg.chancount and self.rc_channel > msg.chancount:
                return  # the channel is not present: no reading
            pwm = getattr(msg, f"chan{self.rc_channel}_raw", None)
            if pwm is not None and pwm not in NO_RC:
                self._on_rc(int(pwm))
        elif kind == "COMMAND_ACK" and msg.command == MAV_CMD_COMPONENT_ARM_DISARM:
            self._acks.put(msg.result)

    def _request_rc_stream(self) -> None:
        if self._target is None:
            return
        with self._send_lock:
            self._conn.mav.command_long_send(self._target[0], self._target[1], MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                                             MSG_RC_CHANNELS, int(1_000_000 / self.rc_rate_hz), 0, 0, 0, 0, 0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._conn = self._connect()
                self._target = None
                log.warning("own MAVLink link up on %s (sysid %d): reading RC%d as the lockout key", self.url, self.source_system, self.rc_channel)
                asked = False
                while not self._stop.is_set():
                    msg = self._conn.recv_match(blocking=True, timeout=0.5)
                    if msg is None:
                        continue
                    self.handle(msg)
                    if not asked and self._target is not None:
                        self._request_rc_stream()
                        asked = True
            except Exception as e:  # noqa: BLE001 - a lost link is retried; silence never releases the lockout
                log.error("own MAVLink link on %s lost (%s): reconnecting", self.url, e)
            finally:
                conn, self._conn = self._conn, None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._stop.wait(1.0)

    def start(self) -> MavlinkLink:
        self._thread = threading.Thread(target=self._run, name="own-mavlink-link", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---- the actuator's side

    def force_disarm(self, timeout_s: float = 2.0) -> tuple[bool, str]:
        """Forced disarm over this link; `(accepted, detail)`, never raises."""
        conn, target = self._conn, self._target
        if conn is None or target is None:
            return False, f"no MAVLink link to the autopilot on {self.url}"
        while not self._acks.empty():
            self._acks.get_nowait()
        with self._send_lock:
            conn.mav.command_long_send(target[0], target[1], MAV_CMD_COMPONENT_ARM_DISARM, 0, 0, FORCE_DISARM_MAGIC, 0, 0, 0, 0, 0)
        deadline = time.monotonic() + timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False, f"no COMMAND_ACK from the autopilot within {timeout_s} s"
            try:
                result = self._acks.get(timeout=left)
            except queue.Empty:
                continue
            return result == MAV_RESULT_ACCEPTED, f"force-disarm MAV_RESULT {result}"

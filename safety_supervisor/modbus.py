"""A minimal Modbus TCP client: read one bit from a coil, a discrete input or a holding register.

Standard library only, and deliberately small: this is how a real stop and lockout reach the
supervisor (crowsnest-dev#277), and it must stay simpler than what it protects. Every way a reply can
be wrong is a ModbusError, which the caller treats as "unreadable"; a value is never guessed.

Modbus has no authentication. Anyone who can reach the device can answer for it, so it belongs on
its own network segment (README).
"""

from __future__ import annotations

import socket
import struct
import time

TABLES = {"coil": 1, "discrete": 2, "holding": 3}

_HEADER = struct.Struct(">HHHB")


class ModbusError(Exception):
    """The device could not be read, or what it answered cannot be trusted."""


class ModbusClient:
    def __init__(self, host: str, port: int, unit: int, timeout_s: float):
        self.host, self.port, self.unit, self.timeout_s = host, port, unit, timeout_s
        self._sock: socket.socket | None = None
        self._tid = 0

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _recv(self, n: int, deadline: float) -> bytes:
        data = b""
        while len(data) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ModbusError("timed out")
            self._sock.settimeout(left)
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ModbusError("the device closed the connection")
            data += chunk
        return data

    def _transact(self, table: str, address: int) -> bytes:
        fc = TABLES[table]
        deadline = time.monotonic() + self.timeout_s
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._tid = (self._tid + 1) & 0xFFFF
        self._sock.settimeout(self.timeout_s)
        self._sock.sendall(_HEADER.pack(self._tid, 0, 6, self.unit) + struct.pack(">BHH", fc, address, 1))

        tid, proto, length, unit = _HEADER.unpack(self._recv(_HEADER.size, deadline))
        if tid != self._tid or proto != 0 or unit != self.unit or not 2 <= length <= 254:
            raise ModbusError(f"a malformed reply header (tid {tid}, protocol {proto}, length {length}, unit {unit})")
        body = self._recv(length - 1, deadline)
        if body[0] == fc | 0x80:
            raise ModbusError(f"the device refused the read: exception {body[1]}")
        expected = 2 if fc == 3 else 1
        if body[0] != fc or body[1] != expected or len(body) != 2 + expected:
            raise ModbusError("a malformed reply")
        return body[2:]

    def read_bit(self, table: str, address: int) -> bool:
        """One bit. Reconnects on the next call after any failure, because a stream that has gone
        wrong cannot be trusted to line up with the next reply."""
        try:
            data = self._transact(table, address)
        except (OSError, ModbusError, IndexError) as e:
            self.close()
            raise ModbusError(str(e) or type(e).__name__) from e
        return any(data) if table == "holding" else bool(data[0] & 1)

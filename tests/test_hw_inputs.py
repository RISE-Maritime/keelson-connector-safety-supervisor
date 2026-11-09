"""The real stop and lockout (crowsnest-dev#277): the Modbus client against a fake server on loopback,
and the driver's decisions with the clock passed in. Nothing here reaches a device or a network."""

import json
import socket
import struct
import threading
import time

import pytest

from safety_supervisor.inputs import Channel, HardwareConfig, HardwareInputs, HardwarePoller
from safety_supervisor.modbus import ModbusClient, ModbusError


class FakeModbus:
    """A loopback Modbus TCP server. `mode` makes it misbehave in one specific way."""

    def __init__(self):
        self.values = {}  # (fc, address) -> int
        self.mode = "ok"
        self.requests = 0
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self._conns = []
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._closed:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            while not self._closed:
                req = conn.recv(12)
                if len(req) < 12:
                    return
                self.requests += 1
                tid, _, _, unit, fc, addr, _ = struct.unpack(">HHHBBHH", req)
                if not self._answer(conn, tid, unit, fc, addr):
                    return
        except OSError:
            pass

    def _answer(self, conn, tid, unit, fc, addr):
        mode, proto = self.mode, 0
        if mode == "silent":
            return True
        if mode == "close":
            conn.close()
            return False
        if mode == "exception":
            pdu = bytes([fc | 0x80, 2])
        else:
            value = self.values.get((fc, addr), 0)
            data = struct.pack(">H", value) if fc == 3 else bytes([value & 0xFF])
            pdu = bytes([fc, len(data)]) + data
        tid = tid + 1 if mode == "bad_tid" else tid
        unit = unit + 1 if mode == "bad_unit" else unit
        proto = 1 if mode == "bad_proto" else proto
        if mode == "bad_fc":
            pdu = bytes([(fc % 3) + 1]) + pdu[1:]
        if mode == "bad_count":
            pdu = bytes([fc, pdu[1] + 1]) + pdu[2:]
        if mode == "one_byte":
            pdu = bytes([fc])
        frame = struct.pack(">HHHB", tid, proto, len(pdu) + 1, unit) + pdu
        if mode == "drip":
            for b in frame:
                conn.sendall(bytes([b]))
                time.sleep(0.05)
        elif mode == "short":
            conn.sendall(frame[:-1])
            time.sleep(1)
        else:
            conn.sendall(frame)
        return True

    def close(self):
        self._closed = True
        try:
            self._srv.close()
        except OSError:
            pass
        for c in self._conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass


@pytest.fixture
def fake():
    f = FakeModbus()
    yield f
    f.close()


def client(fake, timeout_s=0.2):
    return ModbusClient("127.0.0.1", fake.port, 1, timeout_s)


@pytest.mark.parametrize("table,fc", [("coil", 1), ("discrete", 2)])
def test_a_bit_is_read_from_a_coil_or_a_discrete_input(fake, table, fc):
    c = client(fake)
    fake.values[(fc, 7)] = 1
    assert c.read_bit(table, 7) is True
    assert c.read_bit(table, 8) is False
    c.close()


def test_a_holding_register_is_set_when_it_is_not_zero(fake):
    c = client(fake)
    fake.values[(3, 2000)] = 0x0200  # any bit, not only the lowest
    assert c.read_bit("holding", 2000) is True
    fake.values[(3, 2000)] = 0
    assert c.read_bit("holding", 2000) is False
    c.close()


@pytest.mark.parametrize("mode", ["exception", "bad_tid", "bad_unit", "bad_proto", "bad_fc", "bad_count", "one_byte", "short", "silent", "close", "drip"])
def test_a_reply_that_cannot_be_trusted_is_a_failure_never_a_value(fake, mode):
    c = client(fake)
    fake.values[(2, 0)] = 1
    fake.mode = mode
    with pytest.raises(ModbusError):
        c.read_bit("discrete", 0)
    c.close()


def test_a_refusal_says_it_was_a_refusal(fake):
    c = client(fake)
    fake.mode = "exception"
    with pytest.raises(ModbusError, match="refused the read: exception 2"):
        c.read_bit("discrete", 0)
    c.close()


def test_only_the_first_bit_of_a_reply_byte_counts(fake):
    c = client(fake)
    fake.values[(2, 0)] = 0xFE
    assert c.read_bit("discrete", 0) is False
    c.close()


def test_a_device_that_is_not_there_is_a_failure(fake):
    port = fake.port
    fake.close()
    c = ModbusClient("127.0.0.1", port, 1, 0.2)
    with pytest.raises(ModbusError):
        c.read_bit("discrete", 0)


def test_after_a_failure_it_reconnects_rather_than_reading_a_stream_out_of_step(fake):
    c = client(fake, timeout_s=0.1)
    fake.values[(2, 0)] = 1
    fake.mode = "silent"
    with pytest.raises(ModbusError):
        c.read_bit("discrete", 0)
    fake.mode = "ok"
    assert c.read_bit("discrete", 0) is True
    assert len(fake._conns) == 2  # pylint: disable=protected-access
    c.close()


CHANNEL = Channel("discrete", 0, True)
CFG = HardwareConfig("127.0.0.1", CHANNEL, Channel("discrete", 1, True), fault_ms=500, release_ms=200, poll_ms=50, timeout_ms=100)
OK, OPEN = True, False  # raw values, with `released: true`


def primed(stop=OK, lockout=OK, cfg=CFG):
    i = HardwareInputs(cfg)
    i.on_sample(stop, lockout, 1000)
    return i


def test_before_any_reading_she_reads_stopped_and_locked_out():
    r = HardwareInputs(CFG).read(1000)
    assert r.physical_stop and r.lockout and r.fault == "no reading yet" and not r.simulated


def test_the_first_reading_is_taken_as_it_is():
    r = primed().read(1000)
    assert (r.physical_stop, r.lockout, r.fault, r.simulated) == (False, False, None, False)
    r = primed(OPEN, OPEN).read(1000)
    assert (r.physical_stop, r.lockout) == (True, True)


def test_a_press_reads_at_once():
    i = primed()
    i.on_sample(OPEN, OK, 1010)
    assert i.read(1010).physical_stop


def test_a_release_reads_only_after_release_ms_unbroken():
    i = primed(OPEN)
    i.on_sample(OK, OK, 1050)
    assert i.read(1050).physical_stop
    i.on_sample(OK, OK, 1249)
    assert i.read(1249).physical_stop
    i.on_sample(OK, OK, 1250)
    assert not i.read(1250).physical_stop


def test_a_bounce_restarts_the_release():
    i = primed(OPEN)
    i.on_sample(OK, OK, 1050)
    i.on_sample(OPEN, OK, 1150)
    i.on_sample(OK, OK, 1200)
    i.on_sample(OK, OK, 1399)
    assert i.read(1399).physical_stop
    i.on_sample(OK, OK, 1400)
    assert not i.read(1400).physical_stop


def test_a_failed_read_restarts_the_release():
    i = primed(OPEN)
    i.on_sample(OK, OK, 1050)
    i.on_failure("timed out", 1100)
    i.on_sample(OK, OK, 1200)
    i.on_sample(OK, OK, 1300)
    assert i.read(1300).physical_stop


def test_the_lockout_debounces_the_same_way():
    i = primed()
    i.on_sample(OK, OPEN, 1010)
    assert i.read(1010).lockout
    i.on_sample(OK, OK, 1020)
    i.on_sample(OK, OK, 1219)
    assert i.read(1219).lockout
    i.on_sample(OK, OK, 1220)
    assert not i.read(1220).lockout


def test_polarity_is_the_configured_one():
    cfg = HardwareConfig("h", Channel("coil", 0, False), Channel("coil", 1, False), fault_ms=500, poll_ms=50, timeout_ms=100)
    i = primed(False, False, cfg)
    assert (i.read(1000).physical_stop, i.read(1000).lockout) == (False, False)
    assert primed(True, True, cfg).read(1000).physical_stop


def test_a_short_silence_holds_and_a_longer_one_stops_her():
    i = primed()
    i.on_failure("timed out", 1400)
    assert not i.read(1500).physical_stop
    r = i.read(1501)
    assert r.physical_stop and r.fault == "timed out"


@pytest.mark.parametrize("held", [OK, OPEN])
def test_the_lockout_holds_its_last_reading_through_a_fault(held):
    assert primed(OK, held).read(5000).lockout is (held == OPEN)


def test_coming_back_from_a_fault_needs_clean_samples():
    i = primed()
    assert i.read(5000).physical_stop
    i.on_sample(OK, OK, 5000)
    assert i.read(5000).physical_stop
    i.on_sample(OK, OK, 5100)
    assert i.read(5100).physical_stop
    i.on_sample(OK, OK, 5200)
    assert not i.read(5200).physical_stop


def test_a_gap_that_nothing_read_through_still_has_to_come_back_clean():
    """The stop is judged on the sample's own clock, not on whether the loop happened to read the fault."""
    i = primed()
    i.on_sample(OK, OK, 1501)
    assert i.read(1501).physical_stop


def config_file(tmp_path, **over):
    d = {"host": "h", "physical_stop": {"table": "discrete", "address": 0, "released": True},
         "lockout": {"table": "holding", "address": 2000, "released": False}, **over}
    p = tmp_path / "hw.json"
    p.write_text(json.dumps(d))
    return p


def test_a_good_config_loads_with_its_defaults(tmp_path):
    cfg = HardwareConfig.load(config_file(tmp_path))
    assert (cfg.port, cfg.unit, cfg.lockout) == (502, 1, Channel("holding", 2000, False))


@pytest.mark.parametrize("over", [
    {"host": ""},
    {"physical_stop": {"table": "discrete", "address": 0}},  # polarity is never guessed
    {"physical_stop": {"table": "discrete", "address": 0, "released": 1}},
    {"lockout": {"table": "input", "address": 0, "released": True}},
    {"lockout": {"table": "coil", "address": 70000, "released": True}},
    {"lockout": None},
    {"port": 0},
    {"port": "502"},
    {"unit": 300},
    {"poll_ms": 0},
    {"timeout_ms": 0},
    {"release_ms": -1},
    {"fault_ms": 200, "poll_ms": 50, "timeout_ms": 100},  # a slow poll would be a stop
])
def test_a_config_that_could_be_wrong_refuses_to_load(tmp_path, over):
    with pytest.raises(ValueError):
        HardwareConfig.load(config_file(tmp_path, **over))


def test_the_example_config_loads():
    from pathlib import Path

    assert HardwareConfig.load(Path(__file__).resolve().parent.parent / "config/hw-inputs.example.json").host


def test_the_poller_feeds_the_inputs_and_a_lost_device_stops_her(fake):
    cfg = HardwareConfig("127.0.0.1", CHANNEL, Channel("discrete", 1, True), port=fake.port, poll_ms=10, timeout_ms=50, fault_ms=150, release_ms=0, boot_wait_ms=2000)
    fake.values.update({(2, 0): 1, (2, 1): 1})
    inputs = HardwareInputs(cfg)
    p = HardwarePoller(inputs)
    p.start()
    try:
        assert p.wait_first()
        assert not inputs.read(int(time.monotonic() * 1000)).physical_stop
        fake.close()
        time.sleep(0.4)
        r = inputs.read(int(time.monotonic() * 1000))
        assert r.physical_stop and r.fault
    finally:
        p.close()


def test_the_poller_gives_up_waiting_after_boot_wait(fake):
    port = fake.port
    fake.close()
    cfg = HardwareConfig("127.0.0.1", CHANNEL, Channel("discrete", 1, True), port=port, poll_ms=10, timeout_ms=50, fault_ms=150, boot_wait_ms=100)
    p = HardwarePoller(HardwareInputs(cfg))
    p.start()
    try:
        assert not p.wait_first()
    finally:
        p.close()

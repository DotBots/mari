"""Tests for the host side of the UART hop.

The gateway receives continuously into rotating DMA buffers, so nothing on the
wire depends on host timing any more. These pin that down: one write per frame,
and no sleeping inside the serial lock.
"""

import threading
import time

import pytest

from marilib.communication_adapter import SerialAdapter, SerialAdapterStats
from marilib.serial_hdlc import hdlc_encode
from marilib.serial_uart import SerialInterface


class FakeSerial:
    """Stands in for pyserial, recording each write as its own call."""

    def __init__(self):
        self.writes: list[bytes] = []
        self.flushes = 0

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        self.flushes += 1

    def close(self):
        pass


def make_interface(fake) -> SerialInterface:
    """A SerialInterface around a fake port, without opening one or starting
    the reader thread."""
    interface = SerialInterface.__new__(SerialInterface)
    interface.lock = threading.Lock()
    interface.serial = fake
    return interface


@pytest.mark.parametrize("payload_len", [1, 63, 64, 65, 145, 255])
def test_write_is_a_single_call_whatever_the_size(payload_len):
    fake = FakeSerial()
    interface = make_interface(fake)
    frame = hdlc_encode(bytes(range(256))[:payload_len])

    interface.write(frame)

    assert fake.writes == [bytes(frame)]
    assert fake.flushes == 1


def test_write_does_not_sleep():
    """A frame must leave in the time a write takes, not on a timer.

    The pacing this replaces held the serial lock for about 9 ms per frame.
    """
    fake = FakeSerial()
    interface = make_interface(fake)
    frame = hdlc_encode(b"\xa5" * 200)

    start = time.monotonic()
    for _ in range(20):
        interface.write(frame)
    elapsed = time.monotonic() - start

    assert elapsed < 0.05, f"20 writes took {elapsed:.3f}s, so something is pacing them"
    assert len(fake.writes) == 20


def test_writer_has_no_chunking_knobs():
    """The chunk size and inter-chunk delay were half of a timing handshake the
    receiver no longer has. Reintroducing either brings back the loss without
    the benefit."""
    import marilib.serial_uart as serial_uart

    for name in (
        "SERIAL_PAYLOAD_CHUNK_SIZE",
        "SERIAL_PAYLOAD_CHUNK_SIZE_WITH_TRIGGER_BYTE",
        "SERIAL_PAYLOAD_CHUNK_DELAY",
    ):
        assert not hasattr(serial_uart, name), name
    for name in ("write_chunked", "write_trigger_byte", "write_chunked_with_trigger_byte"):
        assert not hasattr(SerialInterface, name), name


def test_send_data_writes_one_encoded_frame_and_counts_it():
    fake = FakeSerial()
    adapter = SerialAdapter.__new__(SerialAdapter)
    adapter.stats = SerialAdapterStats()
    adapter.serial = make_interface(fake)

    payload = b"\x03" + b"\x11" * 40
    adapter.send_data(payload)

    assert len(fake.writes) == 1
    assert fake.writes[0] == bytes(hdlc_encode(payload))
    assert adapter.stats.write_calls == 1
    assert adapter.stats.write_bytes == len(fake.writes[0])
    assert adapter.stats.write_errors == 0

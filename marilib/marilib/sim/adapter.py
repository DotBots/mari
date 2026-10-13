"""Seam A: a CommunicationAdapterBase that puts a simulated network where the gateway UART was.

MarilibEdge takes this in place of a SerialAdapter and sees the same EdgeEvent
bytes. With `realtime=True` the network runs on its own thread, paced to the
wall clock; otherwise the caller drives it with `network.run(...)` and every
UART event is delivered synchronously from that call.
"""

from __future__ import annotations

import threading
import time

from marilib.communication_adapter import CommunicationAdapterBase, SerialAdapterStats
from marilib.serial_uart import SERIAL_DEFAULT_BAUDRATE
from marilib.sim.network import Network


class SimulatedGatewayAdapter(CommunicationAdapterBase):
    def __init__(self, network: Network, *, realtime: bool = True):
        self.network = network
        self.realtime = realtime
        self.port = f"simulator:{network.schedule.name}"
        self.baudrate = SERIAL_DEFAULT_BAUDRATE
        self.stats = SerialAdapterStats()
        self.on_data_received = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def init(self, on_data_received: callable):
        self.on_data_received = on_data_received
        self.network.on_uart = self._deliver
        if self.realtime:
            self._thread = threading.Thread(
                target=self.network.run,
                kwargs={"realtime": True, "stop": self._stop},
                name="mari-sim",
                daemon=True,
            )
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def send_data(self, data, late_stamp_offset: int | None = None) -> int | None:
        """Same contract as SerialAdapter.send_data, including the late TX stamp."""
        stamp_us = None
        if late_stamp_offset is not None:
            data = bytearray(data)
            stamp_us = time.monotonic_ns() // 1000
            data[late_stamp_offset : late_stamp_offset + 8] = stamp_us.to_bytes(8, "little")
        self.network.edge_tx(bytes(data))
        self.stats.write_calls += 1
        self.stats.write_bytes += len(data)
        return stamp_us

    def _deliver(self, payload: bytes) -> None:
        self.stats.rx_frames_ok += 1
        if self.on_data_received is not None:
            self.on_data_received(payload, time.monotonic_ns() // 1000)

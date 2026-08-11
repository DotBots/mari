# SPDX-FileCopyrightText: 2022-present Inria
# SPDX-FileCopyrightText: 2022-present Alexandre Abadie <alexandre.abadie@inria.fr>
# SPDX-FileCopyrightText: 2025-present Geovane Fedrecheski <geovane.fedrecheski@inria.fr>
#
# SPDX-License-Identifier: BSD-3-Clause

"""Serial interface."""

import logging
import sys
import threading
from typing import Callable

import serial
from serial.tools import list_ports

SERIAL_DEFAULT_PORT = "/dev/ttyACM0"
SERIAL_DEFAULT_BAUDRATE = 1_000_000
# SERIAL_DEFAULT_BAUDRATE = 460_800


def get_default_port():
    """Return default serial port."""
    ports = [port for port in list_ports.comports()]
    if sys.platform != "win32":
        ports = sorted([port for port in ports if "J-Link" == port.product])
    if not ports:
        return SERIAL_DEFAULT_PORT
    # return first JLink port available
    return ports[0].device


class SerialInterfaceException(Exception):
    """Exception raised when serial port is disconnected."""


class SerialInterface(threading.Thread):
    """Bidirectional serial interface."""

    def __init__(self, port: str, baudrate: int, callback: Callable):
        self.lock = threading.Lock()
        self.callback = callback
        self.serial = serial.Serial(port, baudrate)
        super().__init__(daemon=True)
        self._logger = logging.getLogger(__name__)
        self.start()
        self._logger.info("Serial port thread started")

    def run(self):
        """Listen continuously at each byte received on serial."""
        self.serial.flush()
        try:
            while 1:
                try:
                    byte = self.serial.read(1)
                except (TypeError, serial.serialutil.SerialException):
                    byte = None
                if byte is None:
                    self._logger.info("Serial port disconnected")
                    break
                self.callback(byte)
        except serial.serialutil.PortNotOpenError as exc:
            self._logger.error(f"{exc}")
            raise SerialInterfaceException(f"{exc}") from exc
        except serial.serialutil.SerialException as exc:
            self._logger.error(f"{exc}")
            raise SerialInterfaceException(f"{exc}") from exc

    def stop(self):
        self.serial.close()
        self.join()

    def write(self, bytes_):
        """Write one HDLC frame on serial, in a single call.

        Delimiting is the framing's job: the flags mark the boundaries and the
        FCS rejects a torn frame. The gateway receives continuously into
        rotating DMA buffers, so it has no state that host timing has to match
        and nothing here paces the bytes. One write, one flush, no sleeps.
        """
        self.serial.write(bytes_)
        self.serial.flush()

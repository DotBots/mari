import time

from marilib.serial_hdlc import (
    HDLCDecodeException,
    HDLCHandler,
    HDLCState,
    hdlc_encode,
)
from marilib.serial_uart import SerialInterface

BAUDRATE = 1000000

hdlc_handler = HDLCHandler()


def on_byte_received(byte):
    hdlc_handler.handle_byte(byte)
    if hdlc_handler.state == HDLCState.READY:
        try:
            print(".", end="", flush=True)
        except HDLCDecodeException as e:
            print(f"Error decoding payload: {e}")


serial_interface = SerialInterface("/dev/ttyACM0", BAUDRATE, on_byte_received)


while True:

    def send_payload(payload):
        print(f"\nSending {len(payload)} bytes: {payload.hex(' ')}")
        encoded = hdlc_encode(payload)
        print(f"Sending encoded {len(encoded)} bytes: {encoded.hex(' ')}")
        serial_interface.write(encoded)

    sleep_time = 0.01  # 10 ms

    for i in range(4):
        # send_payload(b"ABCD" * 8) # 32 bytes
        send_payload(b"A" * 80)
        time.sleep(sleep_time)

    print("Sleeping for 100 ms\n\n")
    time.sleep(0.1)

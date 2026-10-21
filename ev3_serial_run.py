"""Run a Pybricks program on an EV3 over its USB serial port, from the terminal.

Why this exists:
    pybricksdev 2.3.2 (``pybricksdev run usb``) only finds hubs that expose the
    raw-USB Pybricks interface (class 0xFF / subclass 0xC5, product name ending
    in "Pybricks"). Pybricks EV3 firmware 4.1.0 instead shows up as a plain
    USB CDC-ACM serial port ("<name> (MINDSTORMS EV3)"), which the live
    code.pybricks.com talks to over Web Serial.

    This script speaks that same serial "Pybricks link" protocol (framing and
    message types taken from the live code.pybricks.com bundle) and reuses
    pybricksdev's own command layer to compile, download, start the program
    and stream its print() output.

Usage (from the pybricksdev venv's Python):
    python ev3_serial_run.py hello.py            # auto-detects the EV3 COM port
    python ev3_serial_run.py hello.py --port COM6
"""

import argparse
import asyncio
import sys
import threading

import serial
from serial.tools import list_ports
from packaging.version import Version

from pybricksdev.ble.pybricks import (
    DEVICE_NAME_UUID,
    FW_REV_UUID,
    PYBRICKS_COMMAND_EVENT_UUID,
    PYBRICKS_HUB_CAPABILITIES_UUID,
    SW_REV_UUID,
    short_uuid,
    unpack_hub_capabilities,
)
from pybricksdev.connections.pybricks import PybricksHub

LEGO_VID = 0x0694
EV3_PID = 0x0005
BAUD_RATE = 115200

# --- Frame encoding (COBS variant used by Pybricks serial link) ---------------
# Bytes 0..2 are escaped, runs are at most 84 bytes, every byte is XORed with
# 0x03 and each frame ends with a 0x02 delimiter.

DELIM = 0x02
MAX_RUN = 84
NO_DELIM = 0xFF
XOR = 0x03


def encode_frame(data: bytes) -> bytes:
    out = []
    code_idx = 0
    n = 0

    def start_block():
        nonlocal code_idx, n
        code_idx = len(out)
        out.append(NO_DELIM)
        n = 1

    start_block()
    for b in data:
        if b > DELIM:
            out.append(b)
            n += 1
        if b <= DELIM or n > MAX_RUN:
            if b <= DELIM:
                out[code_idx] = b * MAX_RUN + n + DELIM
            start_block()
    out[code_idx] = n + DELIM

    return bytes(x ^ XOR for x in out) + bytes([DELIM])


def decode_frame(frame: bytes) -> bytes:
    """Decode one frame (without its trailing delimiter)."""
    if frame and frame[0] == 0x01:
        frame = frame[1:]

    d = bytes(x ^ XOR for x in frame)
    if not d:
        return b""

    def split_code(c):
        if c == NO_DELIM:
            return None, MAX_RUN + 1
        delim, run = divmod(c - DELIM, MAX_RUN)
        if run == 0:
            run = MAX_RUN
            delim -= 1
        return delim, run

    out = bytearray()
    delim, remaining = split_code(d[0])
    for c in d[1:]:
        remaining -= 1
        if remaining > 0:
            out.append(c)
        else:
            if delim is not None:
                out.append(delim)
            delim, remaining = split_code(c)

    return bytes(out)


# --- Link message types ------------------------------------------------------

OUT_SUBSCRIBE = 0x01  # [type, 1=on / 0=off]
OUT_COMMAND = 0x02  # [type, seq, pybricks command...]
OUT_READ = 0x03  # [type, service, char id LE16]

IN_RESPONSE = 0x01  # [type, seq, status LE32]
IN_EVENT = 0x02  # [type, pybricks event...]
IN_READ_REPLY = 0x03  # [type, service, char id LE16, value...]

SERVICE_GATT = 0x01
SERVICE_PYBRICKS = 0x02

REPLY_TIMEOUT = 2.0


class PybricksHubSerial(PybricksHub):
    """Pybricks hub connected through a USB (CDC-ACM) or Bluetooth serial port."""

    def __init__(self, port: str):
        super().__init__()
        self._port_name = port
        self._notify_callbacks = {}
        self._seq = 0
        self._subscribed = False

    # -- transport --

    def _read_loop(self):
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(self._ser.in_waiting or 1)
            except serial.SerialException:
                self._loop.call_soon_threadsafe(self._on_port_lost)
                return
            for b in chunk:
                if b != DELIM:
                    buf.append(b)
                elif buf:
                    msg = decode_frame(bytes(buf))
                    buf.clear()
                    if msg:
                        self._loop.call_soon_threadsafe(self._dispatch, msg)

    def _on_port_lost(self):
        if self._subscribed:
            self._handle_disconnect()

    def _dispatch(self, msg: bytes):
        kind = msg[0]
        if kind == IN_RESPONSE and len(msg) >= 6:
            self._responses.put_nowait((msg[1], int.from_bytes(msg[2:6], "little")))
        elif kind == IN_EVENT:
            callback = self._notify_callbacks.get(PYBRICKS_COMMAND_EVENT_UUID)
            if callback:
                callback(None, msg[1:])
        elif kind == IN_READ_REPLY and len(msg) >= 4:
            self._read_replies.put_nowait(
                (msg[1], int.from_bytes(msg[2:4], "little"), msg[4:])
            )

    def _write(self, msg: bytes):
        self._ser.write(encode_frame(msg))

    async def _read_char(self, service: int, char_id: int) -> bytes:
        self._write(bytes([OUT_READ, service]) + char_id.to_bytes(2, "little"))
        deadline = self._loop.time() + REPLY_TIMEOUT
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no reply reading characteristic 0x{char_id:04X}")
            svc, cid, value = await asyncio.wait_for(self._read_replies.get(), remaining)
            if svc == service and cid == char_id:
                if not value:
                    raise RuntimeError(f"hub does not know characteristic 0x{char_id:04X}")
                return value

    async def _send_command(self, payload: bytes, wait_reply: bool = True):
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        self._write(bytes([OUT_COMMAND, seq]) + payload)
        if not wait_reply:
            return
        deadline = self._loop.time() + REPLY_TIMEOUT
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no response to command (seq {seq})")
            tag, status = await asyncio.wait_for(self._responses.get(), remaining)
            if tag == seq:
                if status != 0:
                    raise RuntimeError(f"command failed with status {status}")
                return

    # -- PybricksHub interface --

    async def _client_connect(self) -> bool:
        self._loop = asyncio.get_running_loop()
        self._responses = asyncio.Queue()
        self._read_replies = asyncio.Queue()

        self._ser = serial.Serial(self._port_name, BAUD_RATE, timeout=0.05)
        self._ser.reset_input_buffer()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # Flush any partial frame on the hub side, like Pybricks Code does.
        self._ser.write(bytes([DELIM]))

        self.fw_version = Version(
            (await self._read_char(SERVICE_GATT, short_uuid(FW_REV_UUID))).decode()
        )
        self._hub_name = (
            await self._read_char(SERVICE_GATT, short_uuid(DEVICE_NAME_UUID))
        ).decode()
        self._protocol_version = (
            await self._read_char(SERVICE_GATT, short_uuid(SW_REV_UUID))
        ).decode()

        caps = await self._read_char(
            SERVICE_PYBRICKS, short_uuid(PYBRICKS_HUB_CAPABILITIES_UUID)
        )
        (
            self._max_write_size,
            self._capability_flags,
            self._max_user_program_size,
            self._num_of_slots,
        ) = unpack_hub_capabilities(caps)

        return True

    async def _client_disconnect(self) -> bool:
        if self._subscribed:
            self._write(bytes([OUT_SUBSCRIBE, 0]))
            self._subscribed = False
        self._stop.set()
        self._reader.join(timeout=1)
        self._ser.close()
        self._handle_disconnect()

    async def read_gatt_char(self, uuid: str) -> bytearray:
        raise NotImplementedError

    async def write_gatt_char(self, uuid: str, data, response: bool) -> None:
        if uuid.lower() != PYBRICKS_COMMAND_EVENT_UUID:
            raise ValueError("Only Pybricks command event UUID is supported")
        await self._send_command(bytes(data), wait_reply=response)

    async def start_notify(self, uuid: str, callback) -> None:
        self._notify_callbacks[uuid] = callback
        if uuid == PYBRICKS_COMMAND_EVENT_UUID and not self._subscribed:
            # Pybricks Code does not wait for a reply to subscribe.
            self._write(bytes([OUT_SUBSCRIBE, 1]))
            self._subscribed = True


def find_ev3_port() -> str | None:
    for p in list_ports.comports():
        if p.vid == LEGO_VID and p.pid == EV3_PID:
            return p.device
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", help="MicroPython program to run on the EV3")
    parser.add_argument("--port", help="serial port (default: auto-detect EV3)")
    args = parser.parse_args()

    # Hub output can contain characters the Windows console code page can't
    # show; print them escaped instead of crashing mid-run.
    sys.stdout.reconfigure(errors="backslashreplace")

    port = args.port or find_ev3_port()
    if port is None:
        print("EV3 serial port not found (is the brick on and plugged in?)", file=sys.stderr)
        return 1

    hub = PybricksHubSerial(port)
    try:
        await hub.connect()
    except (serial.SerialException, TimeoutError, RuntimeError) as e:
        print(f"Could not connect on {port}: {e}", file=sys.stderr)
        return 1

    print(
        f"[bridge] connected to {hub._hub_name} on {port} "
        f"(firmware {hub.fw_version}, protocol {hub._protocol_version})",
        file=sys.stderr,
    )
    try:
        await hub.run(args.file, wait=True)
    finally:
        await hub.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

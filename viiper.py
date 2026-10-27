"""VIIPER client: virtual DualSense (DualSense 5) as a pad sink.

VIIPER is a local USB/IP device broker.  Its API is a plain TCP service on port
3242 where each request is one line terminated by a null byte and the reply is
JSON; connections from localhost need no authentication.  A device is created on
a "bus", then a second connection to the same port carries the device's binary
stream: we write input-state frames to it and read feedback frames back.

This module provides:

* :class:`ViiperServer` -- the small API client (create/remove buses and devices);
* :class:`DeviceStream` -- the framed binary stream (writer + reader thread);
* :class:`DualSensePad` -- a drop-in replacement for :class:`vigem.DS4Controller`
  that implements the same ``set_*``/``submit``/``plug_in``/``unplug`` methods, so
  the existing mappers drive it without knowing it is a DualSense.

The stream framing was established from the build bundled with DS4Windows (it
rejects bare packets with "invalid DualSense framed stream magic"): a 16-byte
header ("VPCM", version 5, frame type, u16 payload length, u32 sequence, u32
CRC32 over header bytes 4..11 plus the payload) followed by the payload.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import zlib

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3242

#: Device type to create.  This build names its devices with the v5 suffixes;
#: "combined audio duplex" is the one with both the gamepad and the audio
#: interface, which is what a game needs in order to send audio haptics.
DEVICE_TYPE = "dualsensecombinedaudioduplexv5"

FRAME_MAGIC = b"VPCM"
FRAME_VERSION = 0x05
FRAME_HEADER = 16
FRAME_INPUT_STATE = 0x01
FRAME_MIC_PCM = 0x02
FRAME_OUTPUT_STATE = 0x81
FRAME_SPEAKER_PCM = 0x82
FRAME_ATOMIC_AUDIO_HAPTICS = 0x83
FRAME_REALTIME_HAPTICS = 0x84
FRAME_MIC_INTERFACE_STATE = 0x85
FRAME_NAMES = {
    FRAME_INPUT_STATE: "input",
    FRAME_MIC_PCM: "mic-pcm",
    FRAME_OUTPUT_STATE: "output-state",
    FRAME_SPEAKER_PCM: "speaker-pcm",
    FRAME_ATOMIC_AUDIO_HAPTICS: "atomic-audio-haptics",
    FRAME_REALTIME_HAPTICS: "realtime-haptics",
    FRAME_MIC_INTERFACE_STATE: "mic-iface",
}

#: DualSense report units (VIIPER's own constants).
DS5_GYRO_COUNTS_PER_DPS = 16.384
DS5_ACCEL_COUNTS_PER_MS2 = 835.07
#: Our mappers produce DualShock 4 counts: 16 per deg/s and 8192 per g.
DS4_GYRO_COUNTS_PER_DPS = 16.0
DS4_ACCEL_COUNTS_PER_G = 8192.0
GRAVITY = 9.80665
_gyro_scale = DS5_GYRO_COUNTS_PER_DPS / DS4_GYRO_COUNTS_PER_DPS
_accel_scale = (DS5_ACCEL_COUNTS_PER_MS2 * GRAVITY) / DS4_ACCEL_COUNTS_PER_G

#: DualSense button bits (identical low bits to a DualShock 4 for the shared ones).
DS5_PS = 0x00010000
DS5_TOUCHPAD = 0x00020000
DPAD_NEUTRAL = 0x00
_DPAD_BITS = {                       # DualShock 4 hat value -> DualSense bitfield
    0: 0x01,                         # up
    1: 0x01 | 0x08,                  # up-right
    2: 0x08,                         # right
    3: 0x02 | 0x08,                  # down-right
    4: 0x02,                         # down
    5: 0x02 | 0x04,                  # down-left
    6: 0x04,                         # left
    7: 0x01 | 0x04,                  # up-left
    8: DPAD_NEUTRAL,                 # centred
}


class ViiperError(RuntimeError):
    """Raised when VIIPER cannot be reached or refuses a request."""


# --------------------------------------------------------------------------- #
# Frame codec
# --------------------------------------------------------------------------- #

def build_frame(frame_type: int, payload: bytes, sequence: int,
                version: int = FRAME_VERSION) -> bytes:
    """16-byte header + payload, with the CRC over [4..12) and the payload."""
    header = bytearray(FRAME_HEADER)
    header[0:4] = FRAME_MAGIC
    header[4] = version
    header[5] = frame_type
    header[6] = len(payload) & 0xFF
    header[7] = (len(payload) >> 8) & 0xFF
    header[8:12] = (sequence & 0xFFFFFFFF).to_bytes(4, "little")
    crc = zlib.crc32(bytes(header[4:12]) + payload) & 0xFFFFFFFF
    header[12:16] = crc.to_bytes(4, "little")
    return bytes(header) + payload


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #

class ViiperServer:
    """Minimal client for VIIPER's TCP API (localhost, no authentication)."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, path: str, payload=None) -> dict | str:
        """Send one request line; returns the parsed JSON reply (or raw text)."""
        line = path if payload is None else f"{path} {json.dumps(payload)}"
        try:
            with socket.create_connection((self.host, self.port),
                                          timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(line.encode("utf-8") + b"\0")
                chunks = []
                while True:
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError as exc:
            raise ViiperError(
                f"VIIPER 服务未响应（{self.host}:{self.port}）：{exc}. "
                "请先启动 VIIPER（VIIPER-*.exe server）") from exc
        text = b"".join(chunks).decode("utf-8", "replace").strip()
        if not text:
            return {}
        try:
            reply = json.loads(text)
        except ValueError:
            return text
        if isinstance(reply, dict) and "status" in reply and reply.get("status", 200) >= 400:
            raise ViiperError(f"{path} 失败：{reply.get('title')}: {reply.get('detail')}")
        return reply

    def create_bus(self) -> int:
        reply = self.request("bus/create")
        bus_id = reply.get("busId") if isinstance(reply, dict) else None
        if bus_id is None:
            raise ViiperError(f"bus/create 返回异常：{reply}")
        return int(bus_id)

    def add_device(self, bus_id: int, device_type: str = DEVICE_TYPE,
                   device_specific: dict | None = None) -> dict:
        payload = {"type": device_type}
        if device_specific:
            payload["deviceSpecific"] = device_specific
        reply = self.request(f"bus/{bus_id}/add", payload)
        if not isinstance(reply, dict) or "devId" not in reply:
            raise ViiperError(f"bus/{bus_id}/add 返回异常：{reply}")
        return reply

    def remove_device(self, bus_id: int, device_id: str) -> None:
        try:
            self.request(f"bus/{bus_id}/remove {device_id}")
        except ViiperError:
            pass

    def remove_bus(self, bus_id: int) -> None:
        try:
            self.request(f"bus/{bus_id}/remove")
        except ViiperError:
            pass


# --------------------------------------------------------------------------- #
# Framed device stream
# --------------------------------------------------------------------------- #

class DeviceStream:
    """One device's binary stream: framing on the way out, parsing on the way in."""

    def __init__(self, server: ViiperServer, bus_id: int, device_id: str,
                 on_frame=None, log=print):
        self.server = server
        self.bus_id = bus_id
        self.device_id = device_id
        self.on_frame = on_frame
        self.log = log
        self._sock: socket.socket | None = None
        self._sequence = 0
        self._send_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.frame_counts: dict[int, int] = {}
        self.closed = False

    # -- lifecycle -------------------------------------------------------- #

    def open(self, timeout: float = 5.0) -> "DeviceStream":
        """Attach to the device.  Must happen within a few seconds of the add."""
        self._sock = socket.create_connection((self.server.host, self.server.port),
                                              timeout=timeout)
        self._sock.sendall(f"bus/{self.bus_id}/{self.device_id}".encode() + b"\0")
        self._sock.settimeout(0.2)
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="viiper-stream-reader")
        self._reader.start()
        return self

    def close(self) -> None:
        self.closed = True
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- writing ---------------------------------------------------------- #

    def send(self, frame_type: int, payload: bytes) -> None:
        if self.closed or self._sock is None:
            return
        frame = build_frame(frame_type, payload, self._sequence)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        with self._send_lock:
            try:
                self._sock.sendall(frame)
            except OSError as exc:
                self.log(f"VIIPER 流写入失败：{exc}")
                self.closed = True

    # -- reading ---------------------------------------------------------- #

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                header = self._read_exactly(FRAME_HEADER)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if not header:
                break
            if header[0:4] != FRAME_MAGIC:
                self.log(f"VIIPER 流出现未知帧头 {header[0:4]!r}，停止读取")
                break
            frame_type = header[5]
            length = header[6] | (header[7] << 8)
            sequence = int.from_bytes(header[8:12], "little")
            payload = b""
            if length:
                try:
                    payload = self._read_exactly(length) or b""
                except (socket.timeout, TimeoutError):
                    break
                except OSError:
                    break
            self.frame_counts[frame_type] = self.frame_counts.get(frame_type, 0) + 1
            if self.on_frame is not None:
                try:
                    self.on_frame(frame_type, payload, sequence)
                except Exception as exc:          # noqa: BLE001 - never kill the reader
                    self.log(f"帧回调出错：{exc}")
        self.closed = True

    def _read_exactly(self, count: int) -> bytes | None:
        if self._sock is None:
            return None
        chunks = []
        remaining = count
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def describe_frames(self) -> str:
        if not self.frame_counts:
            return "尚未收到任何帧"
        parts = [f"{FRAME_NAMES.get(kind, hex(kind))}={count}"
                 for kind, count in sorted(self.frame_counts.items())]
        return "已收到帧：" + ", ".join(parts)


# --------------------------------------------------------------------------- #
# DualSense pad sink
# --------------------------------------------------------------------------- #

class _ReportShim:
    """The ``pad.report.wTimestamp`` slot our mappers write to.

    A DualSense input state carries no timestamp -- the host timestamps arrivals
    -- so the value is accepted and ignored.
    """

    __slots__ = ("wTimestamp",)

    def __init__(self):
        self.wTimestamp = 0


class DualSensePad:
    """A virtual DualSense, driven through the same API as the ViGEm DS4 pad.

    ``submit()`` marks the state dirty; a pacing thread pushes the latest state to
    VIIPER at ``rate_hz`` (default 250 Hz, i.e. the wired DualSense report rate)
    so the game sees a steady, timestamp-free stream.
    """

    def __init__(self, server: ViiperServer | None = None, log=print,
                 rate_hz: float = 250.0, device_type: str = DEVICE_TYPE,
                 device_specific: dict | None = None):
        self.server = server or ViiperServer()
        self.log = log
        self.rate_hz = rate_hz
        self.device_type = device_type
        self.device_specific = device_specific
        self.report = _ReportShim()
        self.bus_id: int | None = None
        self.device_id: str | None = None
        self.stream: DeviceStream | None = None
        self.plugged = False

        # Pad state, in our mappers' DualShock 4 terms.
        self._sticks = (128, 128, 128, 128)
        self._buttons = 0x0008            # hat centred
        self._special = 0
        self._triggers = (0, 0)
        self._gyro = (0, 0, 0)
        self._accel = (0, 0, 0)
        self._dirty = True
        self._sender: threading.Thread | None = None
        self._stop = threading.Event()
        #: Latest rumble/LED the host asked for (filled in by output-state frames).
        self.output_state = {"small": 0, "large": 0, "led": (0, 0, 0), "player": 0}
        self.haptics_frames = 0

    # -- lifecycle -------------------------------------------------------- #

    def plug_in(self) -> None:
        """Create the virtual DualSense and attach its stream."""
        if self.plugged:
            return
        self.bus_id = self.server.create_bus()
        added = self.server.add_device(self.bus_id, self.device_type,
                                      self.device_specific)
        self.device_id = str(added["devId"])
        self.stream = DeviceStream(self.server, self.bus_id, self.device_id,
                                   on_frame=self._on_frame, log=self.log)
        self.stream.open()                # must happen within seconds of the add
        self.plugged = True
        self._stop.clear()
        self._sender = threading.Thread(target=self._send_loop, daemon=True,
                                       name="dualsense-sender")
        self._sender.start()
        self.log(f"virtual DualSense 已创建（bus={self.bus_id} dev={self.device_id}，"
                 f"USB/IP 端口 {added.get('usbipPort')}）")

    def unplug(self) -> None:
        if not self.plugged:
            return
        self._stop.set()
        if self._sender is not None:
            self._sender.join(timeout=1.0)
            self._sender = None
        if self.stream is not None:
            self.stream.close()
            self.frame_summary = self.stream.describe_frames()
            self.stream = None
        if self.bus_id is not None and self.device_id is not None:
            self.server.remove_device(self.bus_id, self.device_id)
        if self.bus_id is not None:
            self.server.remove_bus(self.bus_id)
        self.plugged = False
        self.bus_id = None
        self.device_id = None
        self.log("virtual DualSense 已移除")

    # -- feedback --------------------------------------------------------- #

    def _on_frame(self, frame_type: int, payload: bytes, sequence: int) -> None:
        if frame_type == FRAME_OUTPUT_STATE and len(payload) >= 6:
            small, large, red, green, blue, player = payload[:6]
            self.output_state = {"small": small, "large": large,
                                 "led": (red, green, blue), "player": player}
        elif frame_type in (FRAME_ATOMIC_AUDIO_HAPTICS, FRAME_REALTIME_HAPTICS):
            self.haptics_frames += 1

    # -- the pad API our mappers use -------------------------------------- #

    def set_sticks(self, lx: int, ly: int, rx: int, ry: int) -> None:
        self._sticks = (lx, ly, rx, ry)
        self._dirty = True

    def set_triggers(self, left: int, right: int) -> None:
        self._triggers = (left, right)
        self._dirty = True

    def set_buttons(self, buttons: int, special: int = 0) -> None:
        self._buttons = buttons
        self._special = special
        self._dirty = True

    def set_motion(self, gyro, accel) -> None:
        self._gyro = tuple(gyro)
        self._accel = tuple(accel)
        self._dirty = True

    def set_battery(self, level: int, cable_connected: bool = False) -> None:
        # Battery lives in the device metadata, not in the input state.
        pass

    def set_battery_byte(self, value: int) -> None:
        pass

    def submit(self) -> None:
        self._dirty = True

    def reset(self) -> None:
        self._sticks = (128, 128, 128, 128)
        self._buttons = 0x0008
        self._special = 0
        self._triggers = (0, 0)
        self._gyro = (0, 0, 0)
        self._accel = (0, 0, 0)
        self._dirty = True

    # -- state packing ---------------------------------------------------- #

    def pack_state(self) -> bytes:
        """33-byte DualSense input state, converted from our DS4-space values."""
        lx, ly, rx, ry = (max(-128, min(127, value - 128)) for value in self._sticks)
        hat = self._buttons & 0x0F
        buttons = self._buttons & ~0x0F          # DualSense encodes the d-pad apart
        if self._special & 0x01:
            buttons |= DS5_PS
        if self._special & 0x02:
            buttons |= DS5_TOUCHPAD
        dpad = _DPAD_BITS.get(hat, DPAD_NEUTRAL)
        trigger_l, trigger_r = (max(0, min(255, value)) for value in self._triggers)
        gyro = tuple(max(-32768, min(32767, int(round(value * _gyro_scale))))
                     for value in self._gyro)
        accel = tuple(max(-32768, min(32767, int(round(value * _accel_scale))))
                      for value in self._accel)
        return struct.pack(
            "<4bIBBBHHBHHB3h3h",
            lx, ly, rx, ry,
            buttons & 0xFFFFFFFF,
            dpad, trigger_l, trigger_r,
            0, 0, 0,                              # touch 1: x, y, active
            0, 0, 0,                              # touch 2: x, y, active
            gyro[0], gyro[1], gyro[2],
            accel[0], accel[1], accel[2],
        )

    def _send_loop(self) -> None:
        interval = 1.0 / max(1.0, self.rate_hz)
        next_send = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            if now < next_send:
                time.sleep(min(next_send - now, 0.002))
                continue
            next_send += interval
            if self.stream is None or self.stream.closed:
                time.sleep(0.05)
                continue
            self._dirty = False
            self.stream.send(FRAME_INPUT_STATE, self.pack_state())

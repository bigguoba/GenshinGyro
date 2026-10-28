"""VIIPER smoke test: create a bus, add a device, attach its stream, poke it.

This is the "does the wire actually behave as documented" check before any of it
goes into the product code.

    python tools\\viiper_smoke.py --type dualsense
    python tools\\viiper_smoke.py --type dualsense --device-specific '{\"variant\":\"dualsensecombinedaudioduplexv5\"}'
    python tools\\viiper_smoke.py --type dualsense --feedback-seconds 3 --hex
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import zlib
import time

HOST, PORT = "127.0.0.1", 3242

#: V5 framed stream, as used by the build bundled with DS4Windows: 16-byte header
#: "VPCM" + version + frameType + u16 payload length + u32 sequence + u32 CRC32.
#: The CRC covers header bytes 4..11 plus the payload (magic and CRC excluded).
FRAME_MAGIC = b"VPCM"
FRAME_VERSION = 0x05
FRAME_HEADER = 16
FRAME_INPUT_STATE = 0x01
FRAME_OUTPUT_STATE = 0x81
FRAME_SPEAKER_PCM = 0x82
FRAME_ATOMIC_AUDIO_HAPTICS = 0x83
FRAME_REALTIME_HAPTICS = 0x84
FRAME_MIC_INTERFACE_STATE = 0x85
FRAME_NAMES = {
    0x01: "input", 0x02: "mic-pcm", 0x81: "output-state", 0x82: "speaker-pcm",
    0x83: "atomic-audio-haptics", 0x84: "realtime-haptics", 0x85: "mic-iface",
}


def frame(frame_type: int, payload: bytes, sequence: int,
          version: int = FRAME_VERSION) -> bytes:
    header = bytearray(FRAME_HEADER)
    header[0:4] = FRAME_MAGIC
    header[4] = version
    header[5] = frame_type
    header[6] = len(payload) & 0xFF
    header[7] = (len(payload) >> 8) & 0xFF
    header[8:12] = sequence.to_bytes(4, "little")
    crc = zlib.crc32(bytes(header[4:12]) + payload) & 0xFFFFFFFF
    header[12:16] = crc.to_bytes(4, "little")
    return bytes(header) + payload


def read_frames(sock, timeout: float) -> list[tuple[int, bytes, int]]:
    """Read whatever whole frames are available.  Returns [(type, payload, seq)]."""
    out = []
    sock.settimeout(timeout)
    try:
        header = recv_exactly(sock, FRAME_HEADER, timeout)
    except (socket.timeout, ConnectionError):
        return out
    if header is None:
        return out
    while header:
        if header[0:4] != FRAME_MAGIC:
            print(f"   !! bad frame magic {header[0:4]!r}")
            return out
        frame_type = header[5]
        length = header[6] | (header[7] << 8)
        sequence = int.from_bytes(header[8:12], "little")
        payload = recv_exactly(sock, length, timeout) if length else b""
        if payload is None:
            return out
        out.append((frame_type, payload, sequence))
        try:
            header = recv_exactly(sock, FRAME_HEADER, timeout)
        except (socket.timeout, ConnectionError):
            break
    return out


def recv_exactly(sock, count: int, timeout: float) -> bytes | None:
    """Read exactly ``count`` bytes, or None on EOF.  Raises socket.timeout."""
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def api(payload: str, timeout: float = 5.0) -> dict | str:
    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.sendall(payload.encode() + b"\0")
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


def neutral_state() -> bytes:
    """33-byte DualSense input state: sticks centred, gyro/accel zero."""
    return struct.pack(
        "<4bIBB?HH?HH3h3h",
        0, 0, 0, 0,          # sticks (-128..127, 0 = centre)
        0,                   # buttons bitfield
        0,                   # dpad bitfield
        0, 0,                # triggers
        False, 0, 0,         # touch1 (x, y, active)  -- see note below
        False, 0, 0,         # touch2
        0, 0, 0,             # gyro
        0, 0, 0,             # accel
    )


def build_state(lx=0, ly=0, rx=0, ry=0, buttons=0, dpad=0, l2=0, r2=0,
                gyro=(0, 0, 0), accel=(0, 0, 0), t1=(0, 0, False), t2=(0, 0, False)) -> bytes:
    """33-byte little-endian DualSense input state, per docs/devices/dualsense.md.

    Layout: 4 sticks + uint32 buttons + uint8 dpad + 2 triggers + 5 touch1 +
    5 touch2 + 6 gyro + 6 accel = 33 bytes, no padding.
    """
    packed = struct.pack(
        # 4 sticks | uint32 buttons | dpad + L2 + R2 | touch1 | touch2 | gyro | accel
        "<4bIBBBHHBHHB3h3h",
        lx, ly, rx, ry,
        buttons,
        dpad, l2, r2,
        int(t1[0]), int(t1[1]), 1 if t1[2] else 0,
        int(t2[0]), int(t2[1]), 1 if t2[2] else 0,
        gyro[0], gyro[1], gyro[2],
        accel[0], accel[1], accel[2],
    )
    assert len(packed) == 33, f"DualSense state must be 33 bytes, got {len(packed)}"
    return packed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", default="dualsense")
    parser.add_argument("--device-specific", default="", help="JSON object passed as deviceSpecific")
    parser.add_argument("--feedback-seconds", type=float, default=2.0)
    parser.add_argument("--hex", action="store_true", help="hex-dump every feedback packet")
    parser.add_argument("--keep", action="store_true", help="do not remove the device/bus afterwards")
    args = parser.parse_args(argv)

    print(f"1) bus/create -> {api('bus/create')}")
    buses = api("bus/list")
    # bus/list answers {"buses": [1, 2, ...]} -- plain ids, not objects.
    raw_ids = buses.get("buses") if isinstance(buses, dict) else None
    bus_ids = [item.get("id", item.get("busId")) if isinstance(item, dict) else item
               for item in (raw_ids or [])]
    print(f"   bus/list -> {buses} -> ids={bus_ids}")
    if not bus_ids:
        print("   no bus available")
        return 1
    bus_id = bus_ids[-1]

    payload = {"type": args.type}
    if args.device_specific:
        payload["deviceSpecific"] = json.loads(args.device_specific)
    request = f'bus/{bus_id}/add ' + json.dumps(payload)
    print(f"2) {request}")
    added = api(request)
    print(f"   -> {added}")
    if not isinstance(added, dict) or "devId" not in added:
        print("   device not created")
        return 1
    dev_id = added["devId"]
    print(f"   vid/pid = {added.get('vid')}/{added.get('pid')}  deviceSpecific={added.get('deviceSpecific')}")

    # The stream must be attached within 5 s of the add, or the device is dropped.
    stream = socket.create_connection((HOST, PORT), timeout=5.0)
    stream.sendall(f"bus/{bus_id}/{dev_id}".encode() + b"\0")
    stream.settimeout(0.05)
    print(f"3) stream attached: bus/{bus_id}/{dev_id}")

    try:
        sequence = 0
        stream.sendall(frame(FRAME_INPUT_STATE, build_state(), sequence))
        sequence += 1
        print(f"4) sent neutral state as framed input (seq 0)")

        deadline = time.perf_counter() + args.feedback_seconds
        sent = 1
        seen: dict[int, int] = {}
        samples: list[tuple[int, bytes]] = []
        next_send = time.perf_counter()
        while time.perf_counter() < deadline:
            now = time.perf_counter()
            if now >= next_send:
                phase = int((now * 1000) % 1000)
                stream.sendall(frame(FRAME_INPUT_STATE,
                                     build_state(gyro=(phase, -phase, phase // 2)),
                                     sequence))
                sequence = (sequence + 1) & 0xFFFFFFFF
                sent += 1
                next_send += 0.004
            for frame_type, payload, seq in read_frames(stream, 0.005):
                seen[frame_type] = seen.get(frame_type, 0) + 1
                if len(samples) < 6 and frame_type not in (0x01,):
                    samples.append((frame_type, payload))
                if args.hex and seen[frame_type] <= 3:
                    name = FRAME_NAMES.get(frame_type, f"0x{frame_type:02x}")
                    print(f"   rx {name} seq={seq} {len(payload)}B {payload[:40].hex(' ')}")
        print(f"5) sent {sent} input frames")
        if seen:
            for frame_type, count in sorted(seen.items()):
                print(f"   rx {FRAME_NAMES.get(frame_type, hex(frame_type)):24} {count:5} 帧")
        else:
            print("   rx 没有任何帧（没有反馈/触觉数据）")
        for frame_type, payload in samples:
            name = FRAME_NAMES.get(frame_type, f"0x{frame_type:02x}")
            print(f"   样本 {name}: {len(payload)}B {payload[:48].hex(' ')}")
    finally:
        stream.close()
        if not args.keep:
            print(f"6) bus/{bus_id}/remove {dev_id} -> {api(f'bus/{bus_id}/remove {dev_id}')}")
            print(f"   bus/{bus_id}/remove -> {api(f'bus/{bus_id}/remove')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

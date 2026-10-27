"""Self-test for the output path (needs no physical controller).

1. connects to ViGEmBus and plugs in a virtual DualShock 4,
2. waits for the device to appear in the HID tree,
3. fills the DS4 report buffer with a known byte pattern and submits it,
4. reads the virtual pad's own HID input report back and compares it byte by
   byte, then checks the named sticks/buttons/gyro/accel fields land on the
   offsets a real DualShock 4 uses on the wire.

Step 4 is what proves the ``DS4_REPORT_EX`` offsets in :mod:`vigem` are the DS4
wire offsets -- which is exactly what the gyroscope and accelerometer fields
depend on.

Run:  python main.py --self-test
"""

from __future__ import annotations

import time

import hidwin
import vigem

DS4_VID = 0x054C
DS4_PIDS = (0x05C4, 0x09CC)  # DS4 v1 / v2


def ds4_paths() -> set[str]:
    return {
        i["path"]
        for i in hidwin.enumerate_hid()
        if i.get("vid") == DS4_VID and i.get("pid") in DS4_PIDS
    }


def wait_for_new_ds4(before: set[str], timeout_s: float = 8.0) -> dict | None:
    """Wait for the DS4 interface ViGEmBus just created.

    A physical DS4 may already be connected, so only a newly appeared 64-byte
    interface counts as ours (ViGEmBus creates the wired target, PID 0x05C4).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for info in hidwin.enumerate_hid():
            if info["path"] in before:
                continue
            if info.get("vid") == DS4_VID and (info.get("input_len") or 0) >= 64:
                return info
        time.sleep(0.25)
    return None


def read_report(info: dict, timeout_s: float = 3.0) -> bytes | None:
    with hidwin.HidDevice(info["path"], info).open() as device:
        device.drain(max_reports=16, timeout_ms=0)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            data = device.read(timeout_ms=500)
            if data:
                return data
    return None


def run(verbose: bool = True) -> int:
    def say(message: str) -> None:
        if verbose:
            print(message, flush=True)

    say(f"ViGEmClient: {vigem.load_client_dll()._name}")

    with vigem.ViGEmClient() as client:
        say("connected to ViGEmBus")
        before = ds4_paths()
        say(f"DS4 HID interfaces already present: {len(before)}")
        pad = client.create_ds4()
        pad.plug_in()
        say("virtual DualShock 4 plugged in")
        try:
            info = wait_for_new_ds4(before)
            if info is None:
                say("FAIL: the virtual DS4 did not appear as a HID device")
                return 1
            say(f"found: {hidwin.describe(info)}")

            # 1. The whole 63-byte buffer must pass through unchanged.
            pattern = bytes(range(63))
            for index, value in enumerate(pattern):
                pad.report.ReportBuffer[index] = value
            pad.submit()
            time.sleep(0.3)
            got = read_report(info)
            if got is None:
                say("FAIL: no input report read back from the virtual pad")
                return 1
            say(f"read back {len(got)} bytes: {got.hex(' ')}")
            payload = got[1:] if got[0] == 0x01 else got
            if payload[:63] != pattern:
                say("MISMATCH between the submitted buffer and the HID report:")
                for index in range(min(len(payload), 63)):
                    if payload[index] != pattern[index]:
                        say(f"  byte {index}: submitted 0x{pattern[index]:02X}, "
                            f"read 0x{payload[index]:02X}")
                return 1
            say("PASS: report buffer passes through unchanged at offset 0")

            # 2. Named fields must land on the real DS4 wire offsets.
            pad.reset()
            pad.set_sticks(10, 20, 30, 40)
            pad.set_buttons(vigem.DS4_BUTTONS["cross"] | vigem.DS4_BUTTONS["triangle"] | 4)
            pad.set_triggers(50, 60)
            pad.set_motion(gyro=(1000, -2000, 3000), accel=(-4000, 5000, -6000))
            pad.submit()
            time.sleep(0.3)
            got = read_report(info)
            if got is None:
                say("FAIL: no second report read back")
                return 1
            payload = got[1:] if got[0] == 0x01 else got
            checks = [
                ("sticks", payload[0:4], bytes((10, 20, 30, 40))),
                ("buttons", payload[4:6], bytes((0x04 | 0x80 | 0x20, 0x00))),
                ("special", payload[6:7], bytes((0x00,))),
                ("triggers", payload[7:9], bytes((50, 60))),
                ("gyro", payload[12:18], b"".join(
                    v.to_bytes(2, "little", signed=True) for v in (1000, -2000, 3000))),
                ("accel", payload[18:24], b"".join(
                    v.to_bytes(2, "little", signed=True) for v in (-4000, 5000, -6000))),
            ]
            failures = 0
            for name, actual, expected in checks:
                if actual == expected:
                    say(f"  {name:9s} ok   {actual.hex(' ')}")
                else:
                    failures += 1
                    say(f"  {name:9s} BAD  got {actual.hex(' ')}  want {expected.hex(' ')}")
            if failures:
                return 1
            say("PASS: DS4 report layout verified, gyro at bytes 12..17 and "
                "accel at bytes 18..23")
            return 0
        finally:
            pad.unplug()
            say("virtual DualShock 4 removed")


if __name__ == "__main__":
    raise SystemExit(run())

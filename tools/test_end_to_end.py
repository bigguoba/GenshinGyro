"""End-to-end test of the forwarding chain without any physical controller.

The Switch side is fed synthetic HID reports; the DualShock 4 side is real: a
virtual pad is plugged into ViGEmBus and its own HID input report is read back
and checked.  That covers parsing, calibration, button/stick/motion mapping and
the DS4 wire format in one go -- everything except the physical HID transport,
which `tools\\dump_switch.py` covers once a controller is attached.

Run:  python tools\\test_end_to_end.py
"""

from __future__ import annotations

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hidwin  # noqa: E402
import mapping  # noqa: E402
import switchpro  # noqa: E402
import vigem  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {actual!r}, want {expected!r}")


def check_close(name: str, actual: float, expected: float, tolerance: float = 1.0) -> None:
    global PASS, FAIL
    if abs(actual - expected) <= tolerance:
        PASS += 1
        print(f"  ok   {name} ({actual:.2f} ~= {expected:.2f})")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {actual:.2f}, want {expected:.2f}")


# --------------------------------------------------------------------------- #
# Synthetic Switch reports
# --------------------------------------------------------------------------- #

def pack_stick(x: int, y: int) -> bytes:
    return bytes((
        x & 0xFF,
        ((x >> 8) & 0x0F) | ((y & 0x0F) << 4),
        (y >> 4) & 0xFF,
    ))


def pack_12bit_groups(values) -> bytes:
    """Inverse of switchpro._pack12: 6 twelve-bit values -> 9 calibration bytes."""
    out = bytearray()
    for index in range(0, 6, 2):
        first, second = values[index], values[index + 1]
        out += bytes((
            first & 0xFF,
            ((first >> 8) & 0x0F) | ((second & 0x0F) << 4),
            (second >> 4) & 0xFF,
        ))
    return bytes(out)


def build_report(buttons=(0, 0, 0), left=(2048, 2048), right=(2048, 2048),
                 imu=(), battery=4, charging=False, wired=False, timer=0,
                 report_id=0x30) -> bytes:
    report = bytearray(49)
    report[0] = report_id
    report[1] = timer
    # bit0 wired, bits1-2 controller kind, bit4 charging, bits5-7 battery level
    report[2] = (((battery & 0x07) << 5) | (0x10 if charging else 0x00)
                 | (0x01 if wired else 0x00))
    report[3:6] = bytes(buttons)
    report[6:9] = pack_stick(*left)
    report[9:12] = pack_stick(*right)
    report[12] = 0
    for index, sample in enumerate(imu[:3]):
        struct.pack_into("<6h", report, 13 + index * 12, *sample)
    return bytes(report)


def state_with(buttons=(0, 0, 0), left=(2048, 2048), right=(2048, 2048),
               imu_sample=None, battery=4, charging=False,
               wired=False) -> switchpro.ControllerState:
    return switchpro.ControllerState(
        report_id=0x30,
        timer=0,
        battery_level=battery,
        charging=charging,
        connection_info=0,
        buttons=buttons,
        left_stick=left,
        right_stick=right,
        vibration_code=0,
        imu=[switchpro.ImuSample(accel=imu_sample[:3], gyro=imu_sample[3:])] if imu_sample else [],
        wired=wired,
    )


# --------------------------------------------------------------------------- #
# Section A: report parsing
# --------------------------------------------------------------------------- #

def test_parsing() -> None:
    print("\n[A] 0x30 report parsing")
    report = build_report(
        buttons=(switchpro.BUTTON_A | switchpro.BUTTON_ZR,
                 switchpro.BUTTON_PLUS | switchpro.BUTTON_CAPTURE,
                 switchpro.BUTTON_LEFT | switchpro.BUTTON_ZL),
        left=(100, 4000),
        right=(3999, 12),
        imu=[(100, -200, 300, 1000, -2000, 3000)],
        battery=3,
        charging=True,
        timer=42,
    )
    state = switchpro.parse_state(report)
    assert state is not None
    check("report id", state.report_id, 0x30)
    check("timer", state.timer, 42)
    check("battery level (0..4)", state.battery_level, 3)
    check("battery level (0..8 scale)", state.battery_level_8, 6)
    check("charging flag", state.charging, True)
    check("wired flag", state.wired, False)
    check("buttons bytes", state.buttons, (0x88, 0x22, 0x88))
    check("a pressed", state.a, True)
    check("b not pressed", state.b, False)
    check("left stick", state.left_stick, (100, 4000))
    check("right stick", state.right_stick, (3999, 12))
    check("imu sample count", len(state.imu), 3)
    check("imu sample 0", (state.imu[0].accel, state.imu[0].gyro),
          ((100, -200, 300), (1000, -2000, 3000)))
    check("imu sample 2 (empty filler)", (state.imu[2].accel, state.imu[2].gyro),
          ((0, 0, 0), (0, 0, 0)))

    reply = bytes([0x21, 0, 0x80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                   0x90, 0x10, 0x20, 0x60, 0, 0, 0x18, 1, 2, 3])
    parsed = switchpro.subcommand_reply(reply)
    assert parsed is not None
    check("subcommand ack", parsed[0], 0x90)
    check("subcommand id", parsed[1], 0x10)
    check("subcommand payload head", parsed[2][:8], bytes([0x20, 0x60, 0, 0, 0x18, 1, 2, 3]))


# --------------------------------------------------------------------------- #
# Section B: calibration maths
# --------------------------------------------------------------------------- #

def test_calibration() -> None:
    print("\n[B] calibration conversions")
    # Values from the reverse-engineering notes: accel origin/sens and gyro
    # origin/sens as int16 LE.
    block = struct.pack(
        "<12h",
        -80, -327, 224,        # accel origin (X, Y, Z)
        16384, 16384, 16384,   # accel sensitivity coefficient
        14, -33, -48,          # gyro origin (X, Y, Z)
        13371, 13371, 13371,   # gyro sensitivity coefficient
    )
    calibration = switchpro.ImuCalibration.parse(block)
    check("calibration source", calibration.source, "spi")
    check("accel origin", calibration.accel_origin, (-80.0, -327.0, 224.0))
    check("accel scale X", round(calibration.accel_scale[0], 8), round(4.0 / (16384 + 80), 8))
    check("gyro origin", calibration.gyro_origin, (14, -33, -48))
    check("gyro scale Z", round(calibration.gyro_scale[2], 8), round(936.0 / (13371 + 48), 8))

    # 1 g of raw acceleration lands on ~4096 counts, 1 dps on ~14.28 counts.
    check_close("4096 counts in g", calibration.to_g((4096, 4096, 4096))[0], 1.0, 0.01)
    check_close("one gyro count in dps", calibration.to_dps((15, -32, -47))[0],
                936.0 / (13371 - 14), 0.001)

    default = switchpro.ImuCalibration.default()
    check_close("default accel", default.to_g((4096, 0, 0))[0], 1.0, 0.001)
    check_close("default gyro", default.to_dps((143, 0, 0))[0], 10.0, 0.05)

    # Stick calibration: rebuild the documented sample block and read it back.
    # Left stick sample from the reverse-engineering notes:
    #   X max above centre 0x4F7, Y max above centre 0x424,
    #   X centre 0x79F, Y centre 0x8A0,
    #   X min below centre 0x510, Y min below centre 0x479
    documented = (0x4F7, 0x424, 0x79F, 0x8A0, 0x510, 0x479)
    stick_block = pack_12bit_groups(documented) + bytes(9)
    left = switchpro.StickCalibration.parse(stick_block, 0, left=True)
    check("left stick centre X", left.x.center, 0x79F)
    check("left stick centre Y", left.y.center, 0x8A0)
    check("left stick max X", left.x.maximum, 0x79F + 0x4F7)
    check("left stick min X", left.x.minimum, 0x79F - 0x510)
    check("left stick max Y", left.y.maximum, 0x8A0 + 0x424)
    check("left stick min Y", left.y.minimum, 0x8A0 - 0x479)


# --------------------------------------------------------------------------- #
# Section C: mapping through a real virtual DS4
# --------------------------------------------------------------------------- #

DS4_VID = 0x054C
DS4_PIDS = (0x05C4, 0x09CC)


def ds4_paths() -> set[str]:
    return {
        i["path"]
        for i in hidwin.enumerate_hid()
        if i.get("vid") == DS4_VID and i.get("pid") in DS4_PIDS
    }


def wait_for_new_ds4(before: set[str], timeout_s: float = 8.0) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for info in hidwin.enumerate_hid():
            if info["path"] not in before and info.get("vid") == DS4_VID \
                    and (info.get("input_len") or 0) == 64:
                return info
        time.sleep(0.2)
    return None


class DS4Reader:
    """Reads the newest report the virtual pad is emitting."""

    def __init__(self, info: dict):
        self.info = info

    def latest(self, timeout_s: float = 2.0):
        with hidwin.HidDevice(self.info["path"], self.info).open() as device:
            device.drain(max_reports=64, timeout_ms=0)
            deadline = time.time() + timeout_s
            last = None
            while time.time() < deadline:
                report = device.read(timeout_ms=200)
                if report:
                    last = report
            if last is None:
                return None
            payload = last[1:] if last[0] == 0x01 else last
            return {
                "sticks": tuple(payload[0:4]),
                "buttons": struct.unpack_from("<H", payload, 4)[0],
                "special": payload[6],
                "triggers": (payload[7], payload[8]),
                "gyro": struct.unpack_from("<3h", payload, 12),
                "accel": struct.unpack_from("<3h", payload, 18),
                # Real DualShock 4 hardware carries the battery byte at payload
                # offset 29 (offset 11 stays 0); verified against a physical pad.
                "battery": payload[29],
                "raw": last,
            }


def test_mapping() -> int:
    print("\n[C] mapping into a virtual DualShock 4")

    with vigem.ViGEmClient() as client:
        before = ds4_paths()
        pad = client.create_ds4()
        pad.plug_in()
        try:
            info = wait_for_new_ds4(before)
            if info is None:
                print("  FAIL virtual DS4 never appeared")
                return 1
            reader = DS4Reader(info)

            controller = switchpro.SwitchProController(log=lambda *_: None)
            controller.left_stick_calibration = switchpro.StickCalibration.default()
            controller.right_stick_calibration = switchpro.StickCalibration.default()
            mapper = mapping.SwitchToDS4Mapper(controller, pad)
            mapper.reset()

            # -- neutral ---------------------------------------------------- #
            mapper.push_state(state_with())
            out = reader.latest(0.6)
            check("neutral sticks", out["sticks"], (128, 128, 128, 128))
            check("neutral dpad", out["buttons"] & 0x0F, 8)
            check("neutral buttons", out["buttons"] & 0xFFF0, 0)
            check("neutral triggers", out["triggers"], (0, 0))
            check("neutral special", out["special"], 0)

            # -- face buttons (positional); exact word, so cross-talk fails --- #
            b = vigem.DS4_BUTTONS
            cases = [
                ("B -> Cross", (switchpro.BUTTON_B, 0, 0), 8 | b["cross"]),
                ("A -> Circle", (switchpro.BUTTON_A, 0, 0), 8 | b["circle"]),
                ("Y -> Square", (switchpro.BUTTON_Y, 0, 0), 8 | b["square"]),
                ("X -> Triangle", (switchpro.BUTTON_X, 0, 0), 8 | b["triangle"]),
                ("R -> R1", (switchpro.BUTTON_R, 0, 0), 8 | b["r1"]),
                ("ZR -> R2", (switchpro.BUTTON_ZR, 0, 0), 8 | b["r2"]),
                ("L -> L1", (0, 0, switchpro.BUTTON_L), 8 | b["l1"]),
                ("ZL -> L2", (0, 0, switchpro.BUTTON_ZL), 8 | b["l2"]),
                ("LStick -> L3", (0, switchpro.BUTTON_L_STICK, 0), 8 | b["l3"]),
                ("RStick -> R3", (0, switchpro.BUTTON_R_STICK, 0), 8 | b["r3"]),
                ("Plus -> Options", (0, switchpro.BUTTON_PLUS, 0), 8 | b["options"]),
                ("Minus -> Share", (0, switchpro.BUTTON_MINUS, 0), 8 | b["share"]),
                ("all four shoulders",
                 (switchpro.BUTTON_R | switchpro.BUTTON_ZR, 0,
                  switchpro.BUTTON_L | switchpro.BUTTON_ZL),
                 8 | b["r1"] | b["r2"] | b["l1"] | b["l2"]),
                ("all four face buttons",
                 (switchpro.BUTTON_A | switchpro.BUTTON_B | switchpro.BUTTON_X | switchpro.BUTTON_Y,
                  0, 0),
                 8 | b["cross"] | b["circle"] | b["square"] | b["triangle"]),
            ]
            for name, buttons, expected_word in cases:
                mapper.push_state(state_with(buttons=buttons))
                out = reader.latest(0.5)
                check(name, out["buttons"], expected_word)

            # -- special buttons ------------------------------------------- #
            mapper.push_state(state_with(buttons=(0, switchpro.BUTTON_HOME, 0)))
            out = reader.latest(0.5)
            check("Home -> PS", out["special"] & vigem.DS4_SPECIAL_PS, vigem.DS4_SPECIAL_PS)

            mapper.push_state(state_with(buttons=(0, switchpro.BUTTON_CAPTURE, 0)))
            out = reader.latest(0.5)
            check("Capture -> touchpad click",
                  out["special"] & vigem.DS4_SPECIAL_TOUCHPAD, vigem.DS4_SPECIAL_TOUCHPAD)

            # -- "-" as the touchpad click (Share mapping switch) ---------- #
            # The Switch Pro has no Share button; "-" plays that role, and with the
            # switch on it must become the touchpad click (moved, not duplicated).
            mapper.push_state(state_with(buttons=(0, switchpro.BUTTON_MINUS, 0)))
            out = reader.latest(0.5)
            check("开关关闭时 - 仍是 Share", out["buttons"] & b["share"], b["share"])
            check("开关关闭时不碰触控板", out["special"] & vigem.DS4_SPECIAL_TOUCHPAD, 0)

            mapper.config.share_as_touchpad = True
            mapper.push_state(state_with(buttons=(0, switchpro.BUTTON_MINUS, 0)))
            out = reader.latest(0.5)
            check("开关打开后 - -> 触控板按下",
                  out["special"] & vigem.DS4_SPECIAL_TOUCHPAD, vigem.DS4_SPECIAL_TOUCHPAD)
            check("开关打开后 - 的 Share 位被移走", out["buttons"] & b["share"], 0)
            check("开关打开后其它键不受影响", out["buttons"] & 0xFFF0, 8 & 0xFFF0)

            mapper.push_state(state_with(buttons=(switchpro.BUTTON_A, switchpro.BUTTON_MINUS, 0)))
            out = reader.latest(0.5)
            check("- 和别的键同时按：其它键照旧",
                  out["buttons"] & ~b["share"], 8 | b["circle"])
            check("- 和别的键同时按：触控板仍在",
                  out["special"] & vigem.DS4_SPECIAL_TOUCHPAD, vigem.DS4_SPECIAL_TOUCHPAD)

            mapper.push_state(state_with(buttons=(0, 0, 0)))
            out = reader.latest(0.5)
            check("松开 - 后触控板位归零",
                  out["special"] & vigem.DS4_SPECIAL_TOUCHPAD, 0)
            check("松开后 Share 位也归零", out["buttons"] & b["share"], 0)
            mapper.config.share_as_touchpad = False

            # -- triggers are digital, so they saturate --------------------- #
            # ZR lives in button byte 0 bit 0x80, ZL in byte 2 bit 0x80.
            mapper.push_state(state_with(
                buttons=(switchpro.BUTTON_ZR, 0, switchpro.BUTTON_ZL)))
            out = reader.latest(0.5)
            check("ZL+ZR analog values", out["triggers"], (255, 255))
            mapper.push_state(state_with(buttons=(0, 0, switchpro.BUTTON_ZL)))
            out = reader.latest(0.5)
            check("ZL alone: no crosstalk to R2", out["triggers"], (255, 0))
            mapper.push_state(state_with(buttons=(switchpro.BUTTON_ZR, 0, 0)))
            out = reader.latest(0.5)
            check("ZR alone: no crosstalk to L2", out["triggers"], (0, 255))

            # -- d-pad ------------------------------------------------------ #
            dpad_cases = [
                ("dpad up", switchpro.BUTTON_UP, 0),
                ("dpad up+right", switchpro.BUTTON_UP | switchpro.BUTTON_RIGHT, 1),
                ("dpad right", switchpro.BUTTON_RIGHT, 2),
                ("dpad down+right", switchpro.BUTTON_RIGHT | switchpro.BUTTON_DOWN, 3),
                ("dpad down", switchpro.BUTTON_DOWN, 4),
                ("dpad down+left", switchpro.BUTTON_DOWN | switchpro.BUTTON_LEFT, 5),
                ("dpad left", switchpro.BUTTON_LEFT, 6),
                ("dpad up+left", switchpro.BUTTON_UP | switchpro.BUTTON_LEFT, 7),
            ]
            for name, bits, expected_hat in dpad_cases:
                mapper.push_state(state_with(buttons=(0, 0, bits)))
                out = reader.latest(0.5)
                check(name, out["buttons"] & 0x0F, expected_hat)

            # -- sticks ------------------------------------------------------ #
            stick_cases = [
                ("left stick full left", (248, 2048), (0, 128, 128, 128)),
                ("left stick full right", (3848, 2048), (255, 128, 128, 128)),
                ("left stick full up", (2048, 3848), (128, 0, 128, 128)),
                ("left stick full down", (2048, 248), (128, 255, 128, 128)),
                ("right stick full left", (2048, 2048, 248, 2048), (128, 128, 0, 128)),
                ("right stick full down", (2048, 2048, 2048, 248), (128, 128, 128, 255)),
            ]
            for case in stick_cases:
                name, sticks = case[0], case[1]
                expected = case[2]
                left = sticks[0:2]
                right = sticks[2:4] if len(sticks) == 4 else (2048, 2048)
                mapper.push_state(state_with(left=left, right=right))
                out = reader.latest(0.5)
                check(name, out["sticks"], expected)

            # -- motion, with the SDL PlayStation frame mapping ------------- #
            # Switch (accel X,Y,Z / gyro X,Y,Z) -> DS4 X=-Y, Y=+Z, Z=-X
            # Default calibration: 4096 counts/g, 14.2842 counts per deg/s.
            # With the real calibration parsed from SPI in production these
            # numbers shift by a fraction of a percent.
            raw_accel = (4096, 0, 0)   # +1 g on Switch X
            raw_gyro = (143, 0, 0)     # ~10 deg/s on Switch X
            mapper.push_state(state_with(imu_sample=raw_accel + raw_gyro))
            out = reader.latest(0.6)
            # accel: DS4 X = -Y = 0, Y = +Z = 0, Z = -X = -8192
            check("accel axis mapping", out["accel"], (0, 0, -8192))
            # gyro: DS4 X = -Y = 0, Y = +Z = 0, Z = -X = -10 dps * 16 = -160
            check("gyro axis mapping", out["gyro"], (0, 0, -160))

            raw_accel = (0, 4096, 0)   # +1 g on Switch Y
            raw_gyro = (0, 143, 0)     # ~10 deg/s on Switch Y
            mapper.push_state(state_with(imu_sample=raw_accel + raw_gyro))
            out = reader.latest(0.6)
            check("accel Y axis", out["accel"], (-8192, 0, 0))
            check("gyro Y axis", out["gyro"], (-160, 0, 0))

            raw_accel = (0, 0, 4096)
            raw_gyro = (0, 0, 143)
            mapper.push_state(state_with(imu_sample=raw_accel + raw_gyro))
            out = reader.latest(0.6)
            check("accel Z axis", out["accel"], (0, 8192, 0))
            check("gyro Z axis", out["gyro"], (0, 160, 0))

            # -- battery ---------------------------------------------------- #
            mapper.push_state(state_with(battery=4, charging=True))
            out = reader.latest(0.5)
            check("battery full + charging", (out["battery"] & 0x0F, bool(out["battery"] & 0x10)),
                  (10, True))
            mapper.push_state(state_with(battery=0, charging=False))
            out = reader.latest(0.5)
            check("battery empty", (out["battery"] & 0x0F, bool(out["battery"] & 0x10)), (0, False))
            mapper.push_state(state_with(battery=2, wired=True))
            out = reader.latest(0.5)
            check("battery half + wired", (out["battery"] & 0x0F, bool(out["battery"] & 0x10)),
                  (5, True))

            # -- one DS4 report per IMU sample ------------------------------ #
            state = state_with()
            state.imu = [
                switchpro.ImuSample(accel=(0, 0, 0), gyro=(0, 0, 0)),
                switchpro.ImuSample(accel=(0, 0, 0), gyro=(0, 0, 0)),
                switchpro.ImuSample(accel=(0, 0, 0), gyro=(143, 0, 0)),
            ]
            submitted = mapper.push_state(state)
            check("three reports per controller report", submitted, 3)

            # -- report timestamp must look like real hardware's ------------- #
            # A real DualShock 4 advances its 16-bit timestamp (5.33 us units) by
            # exactly 768 per report at 250 Hz, i.e. it encodes real elapsed time.
            # Feeding a bare counter instead breaks any dt-based filter or
            # integration in the game, which is what broke yaw in Genshin.
            print("\n  [timestamp behaves like real hardware]")
            check_close("unit constant reproduces the measured 768 counts / 4.1 ms",
                        0.004096 * mapping.DS4_TIMESTAMP_UNITS_PER_SECOND, 768, tolerance=1.0)

            def timestamp_of(payload) -> int:
                return struct.unpack_from("<H", payload, 9)[0]

            # simulate a 60 Hz controller stream: three IMU samples ~5 ms apart
            mapper.reset()
            mapper._last_report_time = None
            stamps = []
            for _ in range(12):
                state = state_with()
                state.imu = [switchpro.ImuSample(accel=(0, 0, 4096), gyro=(0, 100, 0))
                             for _ in range(3)]
                mapper.push_state(state)
                out = reader.latest(0.4)
                stamps.append(timestamp_of(out["raw"][1:]))
                time.sleep(0.016)                    # ~60 reports/s from the controller

            steps = [(b - a) % 65536 for a, b in zip(stamps, stamps[1:])]
            # one push = three reports = one controller report's worth of time
            per_push = [step for step in steps if step]
            check("each 60 Hz report advances ~16 ms of timestamp",
                  all(2200 <= step <= 3400 for step in per_push) and len(per_push) >= 10, True)
            span_ms = sum(per_push) * (16.0 / 3.0) / 1000.0
            wall_ms = len(per_push) * 16.0
            check_close("timestamped span matches wall clock", span_ms, wall_ms,
                        tolerance=0.15 * wall_ms)

            mapper.reset()
            pad.unplug()
        finally:
            pad.unplug()
    return 1 if FAIL else 0


def main() -> int:
    print("Switch Pro Controller -> virtual DualShock 4 : end-to-end test")
    test_parsing()
    test_calibration()
    result = test_mapping()

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if (FAIL or result) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rumble probe: does a physical pad actually vibrate on each transport?

Four independent cases, run one at a time because a pad can only be in one mode
at a time:

    ds4-usb    DualShock 4 in DS4 mode, cable
    ds4-bt     DualShock 4 in DS4 mode, Bluetooth
    nspro-usb  Switch Pro in NS Pro mode, cable
    nspro-bt   Switch Pro in NS Pro mode, Bluetooth

The output-report layouts are **not invented here**; each one is the layout used
by an existing implementation:

  * DS4 USB  0x05, motors at bytes 4/5
        - libds4   src/ds4/ds4_output.c  (ds4_create_oreport / ds4_set_vibration)
        - SDL      SDL_hidapi_ps4.c      (offset 4)
        - JSL      JoyShock.cpp:1100-1127
  * DS4 BT   0x11, motors at bytes 6/7, CRC32 seeded 0xA2 over the first 74 bytes
        - SDL      SDL_hidapi_ps4.c      (offset 6)
        - daidr/dualsense-tester  ds4.util.ts + outputStruct.ts
        - JSL      JoyShock.cpp:1129-1190
        Measured on hardware: the report must be **exactly 78 bytes** and written
        to the interrupt pipe.  Padding it to the node's declared size (Windows
        reports 547 for a Bluetooth DS4) and/or using the control channel gives
        no vibration at all -- see Session._ds4_write().
  * NS Pro   0x10, 8 payload bytes: left low/high band, right low/high band
        - SDL      SDL_hidapi_switch.c   (EncodeRumble + 101-row table)
        - JSL      JoyShock.cpp:1324-1346 + encode*() in JoyShockLibrary.cpp
        - nsctrld  nsinput/src/lib.rs    (RumbleData)

Software cannot feel a vibration, so each case ends with a question for the
operator: felt / not felt / weak.  A case counts as passing only when the
operator says the pad moved.

Run:
    python tools\\rumble_probe.py --list
    python tools\\rumble_probe.py --case ds4-usb
    python tools\\rumble_probe.py --case all
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not sys.stdout.isatty():
    # When the output is redirected, write UTF-8 so a pasted log stays readable.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

import dualshock4  # noqa: E402
import hidwin  # noqa: E402
import switchpro  # noqa: E402

CASES = ("ds4-usb", "ds4-bt", "nspro-usb", "nspro-bt")
KIND_LABEL = {"ds4": "DS4", "nspro": "NS Pro"}
TRANSPORT_LABEL = {"usb": "有线", "bluetooth": "蓝牙"}

#: One burst on, then silence.  Long enough for a rotor motor to spin up.
BURST_ON_S = 0.35
BURST_GAP_S = 0.40

#: How many times the "this really is silence" payload is repeated at the end.
#: HANDOVER 9.6: some pads latch the last strength and need the all-zero packet
#: sent again before they actually stop.
STOP_REPEATS = 5
STOP_GAP_S = 0.05

#: Strength test: one continuous ramp, then three plateaus to compare.
RAMP_SECONDS = 4.0
RAMP_STEP_S = 0.05
RAMP_LEVELS = (("最弱", 0.20), ("中等", 0.60), ("最强", 1.00))
HOLD_S = 1.10
LEVEL_GAP_S = 0.55

#: Band comparison: the two actuators inside a Switch rumble are driven by a
#: low band (default 160 Hz) and a high band (default 320 Hz).  Each sweep keeps
#: the amplitude at full and only changes the frequency; the last item drives
#: both bands together.
LOW_SWEEP_HZ = (80.0, 160.0, 320.0)
HIGH_SWEEP_HZ = (160.0, 320.0, 640.0)
BAND_HOLD_S = 1.00
BAND_GAP_S = 0.55

#: Left/right test: drive one actuator at a time, then both.
LR_HOLD_S = 1.20
LR_GAP_S = 0.60


# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #

def title(text: str) -> None:
    print()
    print("=" * 62)
    print(text)
    print("=" * 62)


def step(text: str) -> None:
    print(f"  {text}")


def _pad(text: str, width: int) -> str:
    """Left-align text in `width` console columns (CJK glyphs are two wide)."""
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - used)


def quiet_log(_message: str) -> None:
    """Swallow the library chatter; the probe prints its own progress."""


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def kind_of(info: dict) -> str | None:
    vid, pid = info.get("vid"), info.get("pid")
    if vid == 0x054C and pid in (0x05C4, 0x09CC):
        return "ds4"
    if vid == 0x057E and pid == 0x2009:
        return "nspro"
    return None


def split_case(case: str) -> tuple[str, str]:
    """'nspro-bt' -> ('nspro', 'bluetooth'); 'ds4-usb' -> ('ds4', 'usb')."""
    mode, wire = case.split("-")
    return mode, ("bluetooth" if wire == "bt" else "usb")


def candidates(kind: str, transport: str) -> list[dict]:
    """Physical interfaces of `kind` currently sitting on `transport`."""
    if kind == "ds4":
        found = dualshock4.find_ds4_interfaces()
    else:
        found = switchpro.find_controller_interfaces()
    return [info for info in found
            if hidwin.transport_of(info) == transport]


def short_path(path: str) -> str:
    head = path.split("#")[0]
    tail = path[-18:] if len(path) > 18 else path
    return f"{head}#…{tail}"


def cmd_list() -> int:
    title("HID 设备一览")
    pads = 0
    for info in hidwin.enumerate_hid():
        kind = kind_of(info)
        if kind is None:
            continue
        pads += 1
        transport = hidwin.transport_of(info)
        print(f"  [{KIND_LABEL[kind]:7s}] {TRANSPORT_LABEL.get(transport, transport):4s} "
              f"VID_{info.get('vid', 0):04X}&PID_{info.get('pid', 0):04X}  "
              f"in={info.get('input_len')} out={info.get('output_len')}  "
              f"{(info.get('product') or '')!r}")
        print(f"            {short_path(info.get('path', ''))}")
    if pads == 0:
        print("  没有发现 DS4 或 Switch Pro。")
        print("  · DS4 模式：VID 054C，PID 05C4 或 09CC")
        print("  · NS Pro 模式：VID 057E，PID 2009")
    else:
        print(f"  共 {pads} 个接口。注意一只手柄可能暴露多个 HID 集合。")
    return 0


# --------------------------------------------------------------------------- #
# Report builders (existing layouts, see module docstring)
# --------------------------------------------------------------------------- #

def ds4_report(small: int, big: int, transport: str,
               hwctl: int | None = None, valid: int | None = None) -> bytes:
    """One output report with both motors set; 0..255 goes in untouched.

    `hwctl` / `valid` override the Bluetooth hardware-control and valid_flag0
    bytes so the probe can try the known variants one at a time.
    """
    small = max(0, min(255, int(round(small))))
    big = max(0, min(255, int(round(big))))

    if transport == "usb":
        report = bytearray(dualshock4.OUTPUT_REPORT_USB_SIZE)   # 32
        report[0] = dualshock4.OUTPUT_REPORT_USB                # 0x05
        report[1] = 0x07                                        # flags
        report[4] = small                                       # right / small
        report[5] = big                                         # left  / big
        # Keep the lightbar at its usual player-1 blue instead of turning it off.
        report[6], report[7], report[8] = dualshock4.DEFAULT_LIGHTBAR
        return bytes(report)

    report = bytearray(dualshock4.OUTPUT_REPORT_BT_SIZE)        # 78
    report[0] = dualshock4.OUTPUT_REPORT_BT                     # 0x11
    # 0xC4 = HID + CRC32 + 4 ms poll, the value SDL and dualsense-tester use.
    report[1] = (dualshock4.HWCTL_HID | dualshock4.HWCTL_CRC32
                 | dualshock4.DEFAULT_POLL_INTERVAL_MS) if hwctl is None else hwctl
    report[2] = 0                                                # audio control
    # bit0 = motors, bit1 = lightbar.  The lightbar is sent too, at the usual
    # player-1 blue, so a rumble test does not silently switch it off.
    report[3] = (dualshock4.VALID_FLAG0_MOTOR | dualshock4.VALID_FLAG0_LED
                 if valid is None else valid)
    report[4] = 0                                                # valid_flag1
    report[6] = small                                            # right / small
    report[7] = big                                              # left  / big
    report[8], report[9], report[10] = dualshock4.DEFAULT_LIGHTBAR
    struct.pack_into("<I", report, len(report) - 4,
                     dualshock4.output_crc32(bytes(report)))
    return bytes(report)


#: The Bluetooth DS4 is the one transport that failed on real hardware, so the
#: probe walks the known variants instead of betting on a single one.  Every
#: variant keeps the documented 78-byte length and a valid CRC; only the write
#: channel and the two flag bytes change.
DS4_BT_VARIANTS = (
    ("控制通道 · 78 字节 · 0xC4 / 0x03（SDL 写法）", "control", 0xC4, 0x03),
    ("中断写 · 78 字节 · 0xC4 / 0x03", "file", 0xC4, 0x03),
    ("控制通道 · 78 字节 · 0xC0 / 0x03（不带节流位）", "control", 0xC0, 0x03),
    ("控制通道 · 78 字节 · 0xC4 / 0x01（只马达，不碰灯）", "control", 0xC4, 0x01),
)


def _log_index(level: float) -> int:
    """Amplitude table row for 0..1, using JoyShockLibrary's encode*Ampli.

    JSL (and SDL's 101-row table) spread 0..65535 over rows 0..100 with a
    piecewise log curve, so the whole byte range 0x00..0xC8 is used.  Our
    switchpro.rumble_payload() instead stops at row 33, i.e. 0x42 -- about a
    third of the range -- which is why every strength felt weak and similar.
    """
    if level <= 0.0:
        return 0
    if level < 0.012:
        row = 1.0
    elif level < 0.112:
        row = round(4 * math.log2(level * 110))
    elif level < 0.225:
        row = round(16 * math.log2(level * 17))
    else:
        row = round(32 * math.log2(level * 8.7))
    return max(0, min(100, int(row)))


def nspro_payload_log(level: float,
                      low_hz: float = switchpro.RUMBLE_LOW_HZ,
                      high_hz: float = switchpro.RUMBLE_HIGH_HZ) -> bytes:
    """Eight-byte payload with the full-range amplitude table (SDL/JSL rows)."""
    row = _log_index(level)
    high_byte = 2 * row
    low_word = (0x8000 if row % 2 else 0) | (0x40 + row // 2)
    high_frequency = (switchpro._rumble_frequency_code(high_hz, "high") - 0x60) * 4
    low_code = switchpro._rumble_frequency_code(low_hz, "low")
    side = bytes((
        high_frequency & 0xFF,
        # Byte 1 holds an 8-bit amplitude and, in bit 0, the high-band
        # frequency's 9th bit.  Every table amplitude is even (2*row), so bit 0
        # is free -- masking the amplitude to 7 bits would silently turn 0xC8
        # into 0x48 and cap the top half of the range.
        (high_byte & 0xFE) | ((high_frequency >> 8) & 0x01),
        ((low_code - 0x40) & 0x7F) | ((low_word >> 8) & 0x80),
        low_word & 0xFF,
    ))
    return side + side


def nspro_payload_bands(low_level: float, low_hz: float,
                        high_level: float, high_hz: float) -> bytes:
    """Eight-byte payload with each band's amplitude and frequency chosen here.

    `low_level` / `high_level` are 0..1; a level of 0 leaves that band silent
    (its amplitude row is 0, which encodes as the neutral `40` bias).
    """
    low_row = _log_index(low_level)
    high_row = _log_index(high_level)
    high_byte = 2 * high_row
    low_word = (0x8000 if low_row % 2 else 0) | (0x40 + low_row // 2)
    high_frequency = (switchpro._rumble_frequency_code(high_hz, "high") - 0x60) * 4
    low_code = switchpro._rumble_frequency_code(low_hz, "low")
    side = bytes((
        high_frequency & 0xFF,
        (high_byte & 0xFE) | ((high_frequency >> 8) & 0x01),
        ((low_code - 0x40) & 0x7F) | ((low_word >> 8) & 0x80),
        low_word & 0xFF,
    ))
    return side + side


def nspro_payload_lr(left_on: bool, right_on: bool,
                     high_hz: float = 320.0) -> bytes:
    """High-band-only payload with each actuator switched on or off.

    Measured on this pad: the three frequency steps inside a band felt the same,
    and the high band feels better than the low one, so the left/right test
    drives the high band only.
    """
    side_on = nspro_payload_bands(0.0, switchpro.RUMBLE_LOW_HZ, 1.0, high_hz)[:4]
    side_off = nspro_payload_bands(0.0, switchpro.RUMBLE_LOW_HZ, 0.0, high_hz)[:4]
    return (side_on if left_on else side_off) + (side_on if right_on else side_off)


def _preview(data: bytes, limit: int = 12) -> str:
    head = " ".join(f"{byte:02X}" for byte in data[:limit])
    more = " …" if len(data) > limit else ""
    return f"{head}{more}  ({len(data)} 字节)"


# --------------------------------------------------------------------------- #
# Per-case sessions
# --------------------------------------------------------------------------- #

class Session:
    """One opened pad and the two operations the pattern needs: on and off."""

    def __init__(self, case: str, info: dict, strength: float, raw: bool,
                 legacy_framing: bool = False, encoding: str = "log"):
        self.case = case
        self.info = info
        self.strength = strength
        self.raw = raw
        self.legacy_framing = legacy_framing
        self.encoding = encoding
        self.mode, self.transport = split_case(case)
        self.pad = None
        self.ctl = None
        self._counter = 0
        #: hex of the last packet sent, for the diagnostic line in run_case()
        self.last_preview = ""

    def _next_counter(self) -> int:
        value = self._counter
        self._counter = (self._counter + 1) & 0x0F
        return value

    # -- lifecycle -------------------------------------------------------- #

    def open(self) -> None:
        if self.mode == "ds4":
            self.pad = dualshock4.DualShock4(
                path=self.info["path"], info=self.info, log=quiet_log).open()
            step(f"已打开：{self.pad.device_name} / {self.pad.transport}")
            if self.transport == "bluetooth":
                # A Bluetooth DS4 starts in the 10-byte 0x01 input mode; the
                # 0x11 mode request must be written at its exact 78-byte length.
                # DualShock4.request_extended_mode() pads to output_len, and a
                # Bluetooth node often declares a large aggregate (547 here), so
                # go straight to the control channel with the 78 bytes built
                # above instead.
                request = dualshock4.build_extended_mode_report()
                try:
                    sent = bool(self.pad._device.set_output_report(request))
                except OSError:
                    sent = self.pad.request_extended_mode()
                step(f"请求扩展模式（78 字节）：{'已发送' if sent else '发送失败'}")
                time.sleep(0.35)
            return

        self.ctl = switchpro.SwitchProController(
            path=self.info["path"], info=self.info, log=quiet_log).open()
        step(f"已打开：{short_path(self.info['path'])}")
        encoding_label = ("SDL/JSL 满量程对数表（100 档）"
                          if self.encoding == "log" else "仓库原表（33 档上限）")
        step(f"振幅编码：{encoding_label}")
        try:
            self.ctl.initialize()
            step(f"初始化完成：{self.ctl.device_type_name} / {self.ctl.transport}")
            if self.ctl.transport not in (self.transport, "unknown"):
                step(f"注意：手柄实际报告的传输是 {self.ctl.transport}，与用例不符；"
                     f"报文长度按实际传输走。")
        except Exception as exc:                      # noqa: BLE001
            # Rumble does not need the 0x30 stream; report and carry on.
            step(f"初始化告警（继续测震动）：{exc}")

    def close(self) -> None:
        if self.pad is not None:
            try:
                self.pad.close()
            except Exception:                          # noqa: BLE001
                pass
            self.pad = None
        if self.ctl is not None:
            try:
                self.ctl.close()
            except Exception:                          # noqa: BLE001
                pass
            self.ctl = None

    # -- actions ---------------------------------------------------------- #

    def _nspro_packet(self, payload: bytes) -> bytes:
        """0x10 report: byte 1 is the rolling 4-bit packet counter.

        ``switchpro.set_rumble_sides()`` omits that counter byte (it writes the
        payload straight after 0x10), which shifts every rumble byte by one --
        the pad then decodes the high-band frequency as the counter and gets
        nonsense.  Every reference implementation (SDL, JoyShockLibrary,
        nsctrld) puts the counter at byte 1, so this probe does too.
        Pass --legacy-framing to reproduce the old 9-byte packet and compare.
        """
        payload = bytes(payload[:8])
        header = bytes((switchpro.OUTPUT_REPORT_RUMBLE,))
        if not self.legacy_framing:
            header += bytes((self._next_counter(),))
        return header + payload

    def _nspro_send(self, payload: bytes) -> None:
        packet = self._nspro_packet(payload)
        self.last_preview = _preview(packet)
        assert self.ctl is not None
        self.ctl._write(packet)

    def _ds4_write(self, data: bytes) -> None:
        """Send one DS4 output report over the channel that transport accepts.

        Measured on hardware (DS4 v2, Bluetooth, Windows 11): the pad accepts the
        **exact 78-byte** 0x11 report written to the interrupt pipe.  Padding the
        report to the node's declared size (Windows reports 547 for this
        Bluetooth node) and/or sending it over the control channel produces no
        vibration at all.  Wired pads use the plain 32-byte report.
        """
        assert self.pad is not None
        if self.transport == "bluetooth":
            assert self.pad._device is not None
            self.pad._device.write(data)
            return
        self.pad.write_output(data)

    def on(self) -> None:
        value = int(round(max(0.0, min(1.0, self.strength)) * 255))
        if self.pad is not None:
            data = ds4_report(value, value, self.transport)
            self.last_preview = _preview(data)
            self._ds4_write(data)
            return
        assert self.ctl is not None
        if self.raw:
            payload = switchpro.rumble_payload_level(self.strength, self.strength)
        elif self.encoding == "repo":
            payload = switchpro.rumble_payload(self.strength, self.strength)
        else:
            payload = nspro_payload_log(self.strength)
        self._nspro_send(payload)

    def off(self) -> None:
        if self.pad is None and self.ctl is None:
            return
        if self.pad is not None:
            self._ds4_write(ds4_report(0, 0, self.transport))
            return
        # All-zero, not the documented neutral: clones latch 0x40 as "a quarter
        # on" (HANDOVER 9.6 #3).
        self._nspro_send(switchpro.RUMBLE_SILENCE)

    def ds4_bt_send(self, data: bytes, channel: str) -> bool:
        """Write an exact-length 0x11 report over the chosen channel.

        `DualShock4.write_output()` pads to the size Windows declares for the
        node; for a Bluetooth pad that can be a large aggregate length, and a
        padded 0x11 report is not the 78-byte report the pad expects.  The sweep
        therefore writes here directly, with the bytes exactly as built.
        """
        assert self.pad is not None and self.pad._device is not None
        if channel == "file":
            self.pad._device.write(data)
            return True
        return bool(self.pad._device.set_output_report(data))

    def set_levels(self, left: float, right: float) -> None:
        """Drive the two actuators with independent 0..1 levels.

        NS Pro goes through the high band only (this pad responds to it better);
        DS4 maps left to the big motor and right to the small one.
        """
        left = max(0.0, min(1.0, float(left)))
        right = max(0.0, min(1.0, float(right)))
        if self.pad is not None:
            data = ds4_report(round(right * 255), round(left * 255), self.transport)
            self.last_preview = _preview(data)
            self._ds4_write(data)
            return
        side_left = nspro_payload_bands(0.0, switchpro.RUMBLE_LOW_HZ, left, 320.0)[:4]
        side_right = nspro_payload_bands(0.0, switchpro.RUMBLE_LOW_HZ, right, 320.0)[:4]
        self._nspro_send(side_left + side_right)

    def silence(self) -> None:
        for _ in range(STOP_REPEATS):
            try:
                self.off()
            except Exception:                          # noqa: BLE001
                return
            time.sleep(STOP_GAP_S)


# --------------------------------------------------------------------------- #
# The operator question
# --------------------------------------------------------------------------- #

def ask_felt() -> str:
    while True:
        try:
            answer = input("  感觉到了吗？ 1=感觉到了  2=没感觉  3=很弱  [回车=没感觉] ").strip()
        except EOFError:
            return "no"
        if answer in ("", "2"):
            return "no"
        if answer == "1":
            return "yes"
        if answer == "3":
            return "weak"


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def run_ds4_bt_sweep(session: Session, strength: float) -> dict:
    """Walk the known Bluetooth variants and ask which one moved the pad."""
    value = int(round(max(0.0, min(1.0, strength)) * 255))
    step(f"Windows 声明的输出报文长度：output_len={session.info.get('output_len')}")
    step("蓝牙 DS4 有几种写法，一种一种试；每次震 2 下。")

    for index, (label, channel, hwctl, valid) in enumerate(DS4_BT_VARIANTS, 1):
        print(f"  [{index}/{len(DS4_BT_VARIANTS)}] {label}")
        accepted = True
        for _ in range(2):
            data = ds4_report(value, value, "bluetooth", hwctl=hwctl, valid=valid)
            try:
                accepted = session.ds4_bt_send(data, channel) and accepted
            except OSError as exc:
                step(f"  写入失败：{exc}")
                accepted = False
                break
            time.sleep(BURST_ON_S)
            session.ds4_bt_send(
                ds4_report(0, 0, "bluetooth", hwctl=hwctl, valid=valid), channel)
            time.sleep(BURST_GAP_S)
        if not accepted:
            step("  这一种写入被拒绝。")
        time.sleep(0.4)

    # Stop with the exact-length report as well, in case the pad latched.
    for _ in range(STOP_REPEATS):
        try:
            session.ds4_bt_send(ds4_report(0, 0, "bluetooth"), "control")
        except OSError:
            break
        time.sleep(STOP_GAP_S)

    try:
        answer = input("  刚才这几种，哪一种震了？（输入 1-4；都没震输入 0） ").strip()
    except EOFError:
        answer = "0"

    if answer in ("1", "2", "3", "4"):
        label = DS4_BT_VARIANTS[int(answer) - 1][0]
        return {"felt": "yes", "note": f"第 {answer} 种有效：{label}"}
    return {"felt": "no", "note": "4 种写法都没有震动"}


def ask_choice(prompt: str, allowed: set[str]) -> str:
    """Read one of `allowed`; empty string when the console has no input."""
    while True:
        try:
            answer = input(prompt).strip()
        except EOFError:
            return ""
        if answer in allowed:
            return answer


def run_strength_test(session: Session) -> dict:
    """Ramp 0 -> 100 %, then hold three levels; ask the operator to compare."""
    print(f"  ⤴ 渐强：0% → 100%（约 {RAMP_SECONDS:.0f} 秒，留意从哪一刻开始有震感）")
    steps = max(1, int(RAMP_SECONDS / RAMP_STEP_S))
    next_mark = 25
    try:
        for index in range(steps + 1):
            percent = index * 100 // steps
            if percent >= next_mark:
                print(f"     … 已到 {next_mark}%")
                next_mark += 25
            session.strength = index / steps
            session.on()
            time.sleep(RAMP_STEP_S)
        session.off()
        time.sleep(0.6)

        for label, level in RAMP_LEVELS:
            session.strength = level
            session.on()
            print(f"  ▸ {label}：{int(level * 100)}%   载荷 {session.last_preview}")
            time.sleep(HOLD_S)
            session.off()
            time.sleep(LEVEL_GAP_S)
    finally:
        session.strength = 1.0

    ramp_answer = ask_choice(
        "  ① 渐强过程中，震动是从小到大吗？\n"
        "     1=是，越来越强   2=没变化   3=跳动/断续   4=完全没震\n"
        "     输入：", {"1", "2", "3", "4"})
    level_answer = ask_choice(
        "  ② 三个档位能分清吗？\n"
        "     1=能分清   2=弱/中分不清   3=中/强分不清   4=都分不清   5=最弱没震\n"
        "     输入：", {"1", "2", "3", "4", "5"})

    ramp_note = {"1": "渐强正常", "2": "渐强没变化", "3": "渐强跳动",
                 "4": "完全没有震动"}.get(ramp_answer, "渐强未回答")
    level_note = {"1": "三档可分", "2": "弱/中分不清", "3": "中/强分不清",
                  "4": "都分不清", "5": "最弱没震"}.get(level_answer, "三档未回答")

    if ramp_answer == "4":
        felt = "no"
    elif ramp_answer == "1" and level_answer == "1":
        felt = "yes"
    elif not ramp_answer and not level_answer:
        felt = "-"
    else:
        felt = "weak"
    return {"felt": felt, "note": f"{ramp_note}；{level_note}"}


def run_band_test(session: Session) -> dict:
    """Play low band, high band and both, then ask the operator to compare."""
    assert session.ctl is not None

    def play(payload: bytes, label: str) -> None:
        session._nspro_send(payload)
        print(f"  ▸ {label}   {payload.hex()}")
        time.sleep(BAND_HOLD_S)
        session.off()
        time.sleep(BAND_GAP_S)

    print("  低频段单独（高频振幅 0）")
    for hz in LOW_SWEEP_HZ:
        play(nspro_payload_bands(1.0, hz, 0.0, switchpro.RUMBLE_HIGH_HZ),
             f"低频 {hz:.0f} Hz")

    print("  高频段单独（低频振幅 0）")
    for hz in HIGH_SWEEP_HZ:
        play(nspro_payload_bands(0.0, switchpro.RUMBLE_LOW_HZ, 1.0, hz),
             f"高频 {hz:.0f} Hz")

    print("  两段一起（默认 160 + 320 Hz）")
    play(nspro_payload_bands(1.0, 160.0, 1.0, 320.0), "低频 160 + 高频 320")

    low_answer = ask_choice(
        "  ① 低频段三个频率，哪个手感最好？\n"
        "     1=80Hz   2=160Hz   3=320Hz   4=都差不多\n"
        "     输入：", {"1", "2", "3", "4"})
    high_answer = ask_choice(
        "  ② 高频段三个频率，哪个手感最好？\n"
        "     1=160Hz   2=320Hz   3=640Hz   4=都差不多\n"
        "     输入：", {"1", "2", "3", "4"})
    which_answer = ask_choice(
        "  ③ 低频段和高频段，哪个整体更明显？\n"
        "     1=低频   2=高频   3=差不多\n"
        "     输入：", {"1", "2", "3"})
    pick_answer = ask_choice(
        "  ④ 你希望游戏震动用哪种？\n"
        "     1=只用低频   2=只用高频   3=两段一起\n"
        "     输入：", {"1", "2", "3"})

    low_note = {"1": "低频选80Hz", "2": "低频选160Hz", "3": "低频选320Hz",
                "4": "低频都差不多"}.get(low_answer, "低频未答")
    high_note = {"1": "高频选160Hz", "2": "高频选320Hz", "3": "高频选640Hz",
                 "4": "高频都差不多"}.get(high_answer, "高频未答")
    which_note = {"1": "低频更明显", "2": "高频更明显",
                  "3": "两段差不多"}.get(which_answer, "整体未答")
    pick_note = {"1": "想用低频", "2": "想用高频",
                 "3": "想用两段"}.get(pick_answer, "选择未答")

    answered = any((low_answer, high_answer, which_answer, pick_answer))
    return {"felt": "yes" if answered else "-",
            "note": f"{low_note}；{high_note}；{which_note}；{pick_note}"}


def run_lr_test(session: Session) -> dict:
    """Left only, right only, both; ask whether each side really moves alone."""

    def send(left_on: bool, right_on: bool, label: str) -> None:
        if session.pad is not None:
            report = ds4_report(255 if right_on else 0,
                                255 if left_on else 0, session.transport)
            session._ds4_write(report)
            print(f"  ▸ {label}   {_preview(report)}")
        else:
            payload = nspro_payload_lr(left_on, right_on)
            session._nspro_send(payload)
            print(f"  ▸ {label}   {payload.hex()}")
        time.sleep(LR_HOLD_S)
        session.off()
        time.sleep(LR_GAP_S)

    send(True, False, "只震左")
    send(False, True, "只震右")
    send(True, True, "两边一起")

    left_answer = ask_choice(
        "  ① 只震左边时，左手握把有震感吗？ 1=有  2=没有\n     输入：",
        {"1", "2"})
    right_answer = ask_choice(
        "  ② 只震右边时，右手握把有震感吗？ 1=有  2=没有\n     输入：",
        {"1", "2"})
    bleed_answer = ask_choice(
        "  ③ 单边震动时，另一边是否也在动？\n"
        "     1=完全不动   2=轻微   3=明显\n     输入：", {"1", "2", "3"})
    power_answer = ask_choice(
        "  ④ 左右强度对比？ 1=左更强  2=右更强  3=差不多\n     输入：",
        {"1", "2", "3"})

    left_note = {"1": "左有", "2": "左无"}.get(left_answer, "左未答")
    right_note = {"1": "右有", "2": "右无"}.get(right_answer, "右未答")
    bleed_note = {"1": "互不串扰", "2": "轻微串扰", "3": "明显串扰"}.get(
        bleed_answer, "串扰未答")
    power_note = {"1": "左更强", "2": "右更强", "3": "左右相当"}.get(
        power_answer, "强弱未答")

    if left_answer == "2" and right_answer == "2":
        felt = "no"
    elif left_answer == "1" and right_answer == "1" and bleed_answer in ("1", "2"):
        felt = "yes"
    elif not any((left_answer, right_answer)):
        felt = "-"
    else:
        felt = "weak"
    return {"felt": felt,
            "note": f"{left_note}；{right_note}；{bleed_note}；{power_note}"}


def run_case(case: str, strength: float, raw: bool, bursts: int,
             legacy_framing: bool = False, sweep: bool = False,
             ramp: bool = False, encoding: str = "log",
             bands: bool = False, lr: bool = False) -> dict:
    mode, transport = split_case(case)
    result = {"case": case, "found": False, "sent": False,
              "felt": "-", "note": ""}

    title(f"用例：{KIND_LABEL[mode]} · {TRANSPORT_LABEL[transport]}  ({case})")

    infos = candidates(mode, transport)
    if not infos:
        step(f"没找到处于「{TRANSPORT_LABEL[transport]}」的 {KIND_LABEL[mode]} 设备。")
        step("确认手柄当前是哪种模式、以及是否真的连在这个传输上；")
        step("可先跑 --list 看系统里有什么。")
        result["note"] = "未找到设备"
        return result

    info = infos[0]
    result["found"] = True
    step(f"选中接口：in={info.get('input_len')} out={info.get('output_len')} "
         f"{(info.get('product') or '')!r}")
    step(short_path(info["path"]))

    session = Session(case, info, strength, raw, legacy_framing, encoding)
    try:
        session.open()
        if lr:
            result["sent"] = True
            result.update(run_lr_test(session))
            return result
        if bands:
            if mode != "nspro":
                step("DS4 没有频段概念（只有大/小两个马达），这一项跳过。")
                result["note"] = "DS4 无频段"
                return result
            result["sent"] = True
            result.update(run_band_test(session))
            return result
        if ramp:
            result["sent"] = True
            result.update(run_strength_test(session))
            return result
        if case == "ds4-bt" and sweep:
            result["sent"] = True
            result.update(run_ds4_bt_sweep(session, strength))
            return result
        session.on()                       # make sure a first packet lands
        step(f"首包：{session.last_preview}")
        time.sleep(0.12)
        session.off()
        time.sleep(0.25)

        for index in range(bursts):
            print(f"  [{index + 1}/{bursts}] 震动 {int(BURST_ON_S * 1000)} ms ...")
            session.on()
            time.sleep(BURST_ON_S)
            session.off()
            time.sleep(BURST_GAP_S)
        result["sent"] = True
    except Exception as exc:                # noqa: BLE001
        step(f"发送失败：{exc}")
        result["note"] = f"发送失败：{type(exc).__name__}"
        return result
    finally:
        session.silence()
        session.close()
        print("  已停止（静音包 ×%d）" % STOP_REPEATS)

    result["felt"] = ask_felt()
    return result


def print_summary(results: list[dict]) -> None:
    title("汇总")
    verdict = {"yes": "通过", "weak": "很弱", "no": "未通过", "-": "—"}
    header = (f"  {_pad('用例', 12)}{_pad('找到设备', 10)}"
              f"{_pad('发送成功', 10)}{_pad('你感觉到', 10)}备注")
    print(header)
    print("  " + "-" * 58)
    for item in results:
        print(f"  {_pad(item['case'], 12)}"
              f"{_pad('是' if item['found'] else '否', 10)}"
              f"{_pad('是' if item['sent'] else '否', 10)}"
              f"{_pad(verdict.get(item['felt'], item['felt']), 10)}"
              f"{item['note']}")
    passed = [item["case"] for item in results if item["felt"] in ("yes", "weak")]
    print()
    if passed:
        print(f"  能震的用例：{', '.join(passed)}")
    else:
        print("  没有用例被确认震感。")


MENU_ITEMS = (
    ("1", "全部四种（每个用例之间会暂停，方便切模式 / 插拔线）", "all"),
    ("2", "只测 DS4 有线", "ds4-usb"),
    ("3", "只测 DS4 蓝牙", "ds4-bt"),
    ("4", "只测 NS Pro 有线", "nspro-usb"),
    ("5", "只测 NS Pro 蓝牙", "nspro-bt"),
    ("6", "强度测试：全部四种（渐强 + 最弱/中等/最强）", "all-ramp"),
    ("7", "强度测试：只测 NS Pro（有线 + 蓝牙）", "nspro-ramp"),
    ("8", "频段对比：NS Pro（低频 / 高频 / 两段）", "nspro-bands"),
    ("9", "左右马达：全部四种（左 / 右 / 两边）", "all-lr"),
)


def interactive_menu() -> str | None:
    """Print the device list plus a numbered menu; return a --case value."""
    cmd_list()
    print()
    print("  " + "-" * 52)
    for key, label, _ in MENU_ITEMS:
        print(f"   {key}  {label}")
    print("   0  只看列表，不震动（退出）")
    print("  " + "-" * 52)
    try:
        answer = input("  输入数字后按回车：").strip()
    except EOFError:
        return None
    for key, _label, case in MENU_ITEMS:
        if answer == key:
            return case
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="测试实体手柄在四种情况下的震动通路。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="用例：ds4-usb / ds4-bt / nspro-usb / nspro-bt / all",
    )
    parser.add_argument("--list", action="store_true", help="只列出手柄接口，不发任何报文")
    parser.add_argument("--menu", action="store_true",
                        help="先列设备，再用数字菜单选用例（给双击的 .bat 用）")
    parser.add_argument("--case", default=None, choices=(*CASES, "all", "nspro"),
                        help="要测的用例；nspro = NS Pro 的有线+蓝牙")
    parser.add_argument("--strength", type=float, default=1.0,
                        help="震动强度 0..1，默认 1.0")
    parser.add_argument("--bursts", type=int, default=3,
                        help="每个用例震几下，默认 3")
    parser.add_argument("--raw", action="store_true",
                        help="NS Pro 用 raw-drive 字节（文档编码无效时的备选）")
    parser.add_argument("--legacy-framing", action="store_true",
                        help="NS Pro 省略 0x10 的计数器字节（复现旧实现，做 A/B 对照）")
    parser.add_argument("--sweep", action="store_true",
                        help="DS4 蓝牙：把 4 种已知写法逐一试一遍（排障用）")
    parser.add_argument("--ramp", action="store_true",
                        help="强度测试：渐强 0→100 %%，再保持 最弱/中等/最强 三档")
    parser.add_argument("--encoding", choices=("log", "repo"), default="log",
                        help="NS Pro 振幅编码：log=SDL/JSL 满量程（默认），repo=仓库原表")
    parser.add_argument("--bands", action="store_true",
                        help="频段对比：低频 / 高频 / 两段一起（仅 NS Pro）")
    parser.add_argument("--lr", action="store_true",
                        help="左右马达：只左 / 只右 / 两边一起")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.menu:
        chosen = interactive_menu()
        if chosen is None:
            return 0
        if chosen == "all-ramp":
            args.case, args.ramp = "all", True
        elif chosen == "nspro-ramp":
            args.case, args.ramp = "nspro", True
        elif chosen == "nspro-bands":
            args.case, args.bands = "nspro", True
        elif chosen == "all-lr":
            args.case, args.lr = "all", True
        else:
            args.case = chosen
    if args.case is None:
        parser.print_help()
        return 2

    if args.case == "all":
        cases = CASES
    elif args.case == "nspro":
        cases = ("nspro-usb", "nspro-bt")
    else:
        cases = (args.case,)
    print()
    if args.lr:
        print("说明：每种先只震左边 1.2 秒，再只震右边 1.2 秒，最后两边一起。")
        print("      NS Pro 用高频段单边驱动；DS4 用大马达=左、小马达=右。")
        print("      结束后问你四个问题：左右各自有没有、会不会串、谁更强。")
    elif args.bands:
        print("说明：每个用例先扫低频段 80/160/320 Hz，再扫高频段 160/320/640 Hz，")
        print("      最后 160+320 两段一起。每下 1 秒，中间停一下。")
        print("      结束后问你四个问题：两段各自的偏好、谁更明显、想要哪种。")
    elif args.ramp:
        levels = " / ".join(f"{label}{int(level * 100)}%"
                            for label, level in RAMP_LEVELS)
        print(f"说明：每个用例先渐强 {RAMP_SECONDS:.0f} 秒（0→100%），"
              f"再分别保持：{levels}。")
        print("      结束后会问你两个问题：渐强是否明显、三档能不能分清。")
    else:
        print("说明：每个用例震 %d 下（每下 %.0f ms），结束后会强制静音。"
              % (args.bursts, BURST_ON_S * 1000))
        print("      软件无法感知震动，最后会问你有没有感觉到。")

    results = []
    for case in cases:
        results.append(run_case(case, args.strength, args.raw, args.bursts,
                                args.legacy_framing, args.sweep, args.ramp,
                                args.encoding, args.bands, args.lr))
        if case != cases[-1]:
            try:
                input("\n  按回车进入下一个用例（可先把手柄切到对应模式）...")
            except EOFError:
                # Not an interactive console: just move on to the next case.
                continue

    print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())

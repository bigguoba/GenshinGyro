"""Maps Switch Pro Controller state onto a DualShock 4 report.

Button/stick mapping is positional (Nintendo A sits where DS4 Circle sits, etc.)
and there is no remapping UI: that is intentional, this tool only forwards.

Motion mapping
--------------
Both sensor frames are normalised through the same reference SDL2 uses:

    DS4 X = -Switch Y
    DS4 Y = +Switch Z
    DS4 Z = -Switch X

(SDL_hidapi_switch.c ``SendSensorUpdate``: "the order of components has been
shuffled to match PlayStation controllers ... users will want consistent axis
mappings across devices", while SDL_hidapi_ps4.c passes the DS4 axes straight
through.)

Units
-----
Switch: ``dps = (raw - gyro_origin) * 936 / (gyro_coeff - gyro_origin)`` and
``g = raw * 4 / (accel_coeff - accel_origin)``; defaults 14.2842 counts per
deg/s and 4096 counts per g.

DS4: the raw report is 16 counts per deg/s and 8192 counts per g
(SDL: gyro 1/16 deg/s per count, accel 1/8192 g per count; Linux uses the same
2048 deg/s and 4 g full scales).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import paths
import switchpro
import vigem


def set_timer_resolution(milliseconds: int) -> bool:
    """Raise/lower this process's timer granularity (Windows only).

    Without this, ``Event.wait`` and ``time.sleep`` round up to the default
    ~15.6 ms tick, so a 2 ms render cadence silently becomes ~64 Hz -- slower
    than the 187.5 Hz at which the host polls the virtual pad, which is exactly
    the staircase the smoothing is meant to remove.  Passing 0 restores the
    default.
    """
    if sys.platform != "win32":
        return False
    try:
        return ctypes.WinDLL("winmm").timeBeginPeriod(milliseconds) == 0
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        return False


#: Timing for motion work must use the performance counter: on Windows
#: ``time.monotonic()`` has ~15.6 ms granularity (GetTickCount64), which made the
#: render clock advance only once per tick -- several renders sharing one value
#: and then a jump, i.e. exactly the staircase this code exists to remove.
_clock = time.perf_counter

# --------------------------------------------------------------------------- #
# What a *real wired* DualShock 4 actually does
#
# Measured on hardware: it sends a report every 4.096 ms (so tools show "250 Hz"),
# its 16-bit timestamp advances by exactly 768 counts per report, and its
# gyroscope/accelerometer values only change every *second* report -- the IMU
# updates at ~125 Hz while the report rate stays 250 Hz, so consecutive reports
# carry identical motion, and the timestamp advances anyway.
#
# That pattern matters: a game's yaw aiming integrates rotation over reports, so
# feeding it several times more reports than real hardware (or fresh values on
# every report) makes the integrated yaw come out far too large -- the picture
# jumps.  ``hardware`` pacing below reproduces the real pattern on purpose.
# --------------------------------------------------------------------------- #

#: 768 counts / 187500 counts-per-second = the real report interval.
DS4_NATIVE_REPORT_INTERVAL_S = 768.0 / 187500.0          # 4.096 ms, ~244 Hz
DS4_NATIVE_TIMESTAMP_STEP = 768

#: How the relayed motion is paced onto the virtual pad.
#:   "hardware" - 4.096 ms reports, timestamp +768, sample held until the next
#:                one arrives: what a real wired pad does (default).
#:   "smooth"   - ~400 Hz reports with interpolated values: a pretty waveform,
#:                but games that count reports integrate far too much rotation.
#:   "raw"      - one report per input report, no pacing thread at all.
MOTION_PACING_DEFAULT = "hardware"
MOTION_PACING_CHOICES = ("hardware", "smooth", "raw")

# --------------------------------------------------------------------------- #
# DS4 raw report units
# --------------------------------------------------------------------------- #

DS4_GYRO_COUNTS_PER_DPS = 16.0
DS4_ACCEL_COUNTS_PER_G = 8192.0

#: The DS4 report timestamp counts 5.33 us units (SDL: timestamp_us = value * 16 / 3).
#: Real hardware advances it by 768 per report at its 250 Hz USB rate, i.e. it
#: encodes how much time passed since the previous report.  A game that derives
#: its time step from this field (integration or a dt-based filter) gets nonsense
#: if the value is just a counter, so it must be advanced by real elapsed time.
DS4_TIMESTAMP_UNITS_PER_SECOND = 1_000_000.0 / (16.0 / 3.0)   # 187500

#: Fallback spacing between two forwarded IMU samples (the controller packs three
#: samples ~5 ms apart into each report); replaced by the measured value.
DEFAULT_IMU_SAMPLE_INTERVAL_S = 0.005
MIN_IMU_SAMPLE_INTERVAL_S = 0.001
MAX_IMU_SAMPLE_INTERVAL_S = 0.020

DS4_TIMESTAMP_MODULUS = 1 << 16

#: DS4 d-pad hat values, indexed by (up, right, down, left) as a bit mask.
#: order: 0=none 1=up 2=right 3=up+right 4=down 5=up+down 6=right+down ...
#: It is simpler to look up explicit combinations below.
DPAD_HAT = {
    (False, False, False, False): 8,
    (True, False, False, False): 0,
    (True, True, False, False): 1,
    (False, True, False, False): 2,
    (False, True, True, False): 3,
    (False, False, True, False): 4,
    (False, False, True, True): 5,
    (False, False, False, True): 6,
    (True, False, False, True): 7,
}

#: Logical control -> (button byte index, Switch bit).
#:
#: The protocol reuses bit values across the three button bytes (R and L are
#: both 0x40, ZR and ZL are both 0x80), so the byte index is what actually
#: selects the button.  This is the mapping the GUI can verify and correct;
#: a captured correction is stored in ``captured_mapping.json``.
DEFAULT_BUTTON_MAP = {
    "B": (0, switchpro.BUTTON_B),              # bottom
    "A": (0, switchpro.BUTTON_A),              # right
    "Y": (0, switchpro.BUTTON_Y),              # left
    "X": (0, switchpro.BUTTON_X),              # top
    "R": (0, switchpro.BUTTON_R),
    "ZR": (0, switchpro.BUTTON_ZR),
    "L": (2, switchpro.BUTTON_L),
    "ZL": (2, switchpro.BUTTON_ZL),
    "L3": (1, switchpro.BUTTON_L_STICK),
    "R3": (1, switchpro.BUTTON_R_STICK),
    "Plus": (1, switchpro.BUTTON_PLUS),
    "Minus": (1, switchpro.BUTTON_MINUS),
}

#: Logical control -> DS4 button bit.  Fixed: these are DS4 hardware facts.
BUTTON_TARGETS = {
    "B": vigem.DS4_BUTTONS["cross"],
    "A": vigem.DS4_BUTTONS["circle"],
    "Y": vigem.DS4_BUTTONS["square"],
    "X": vigem.DS4_BUTTONS["triangle"],
    "R": vigem.DS4_BUTTONS["r1"],
    "ZR": vigem.DS4_BUTTONS["r2"],
    "L": vigem.DS4_BUTTONS["l1"],
    "ZL": vigem.DS4_BUTTONS["l2"],
    "L3": vigem.DS4_BUTTONS["l3"],
    "R3": vigem.DS4_BUTTONS["r3"],
    "Plus": vigem.DS4_BUTTONS["options"],
    "Minus": vigem.DS4_BUTTONS["share"],
}

#: Logical control -> (button byte index, Switch bit) for the d-pad.
DPAD_MAP = {
    "Up": (2, switchpro.BUTTON_UP),
    "Right": (2, switchpro.BUTTON_RIGHT),
    "Down": (2, switchpro.BUTTON_DOWN),
    "Left": (2, switchpro.BUTTON_LEFT),
}

#: Logical control -> (button byte index, Switch bit, DS4 special bit).
SPECIAL_MAP = {
    "Home": (1, switchpro.BUTTON_HOME, vigem.DS4_SPECIAL_PS),
    "Capture": (1, switchpro.BUTTON_CAPTURE, vigem.DS4_SPECIAL_TOUCHPAD),
}

#: Where a GUI-captured button correction is stored.
CAPTURED_MAPPING_FILENAME = "captured_mapping.json"


def captured_mapping_path() -> str:
    return paths.app_path(CAPTURED_MAPPING_FILENAME)


def load_captured_buttons(path: str | None = None) -> dict:
    """Read ``{"buttons": {"B": [byte, bit], "Up": [...]}}``; empty if absent.

    Names may be ordinary buttons or d-pad directions; use
    :func:`apply_captured` to route them into a config.
    """
    path = path or captured_mapping_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    buttons = data.get("buttons") or {}
    known = set(DEFAULT_BUTTON_MAP) | set(DPAD_MAP)
    result = {}
    for name, value in buttons.items():
        if name in known and isinstance(value, (list, tuple)) and len(value) == 2:
            result[name] = (int(value[0]), int(value[1]))
    return result


def apply_captured(config: "MappingConfig", captured: dict) -> "MappingConfig":
    """Route captured corrections into ``button_map`` / ``dpad_map``."""
    for name, value in captured.items():
        if name in DEFAULT_BUTTON_MAP:
            config.button_map[name] = value
        elif name in DPAD_MAP:
            config.dpad_map[name] = value
    return config


def save_captured_buttons(buttons: dict, path: str | None = None) -> str:
    """Persist corrected button bindings; returns the file path."""
    path = path or captured_mapping_path()
    payload = {
        "comment": "Captured by the verification GUI: logical control -> "
                   "[button byte index, bit mask]. Delete this file to go back "
                   "to the default mapping.",
        "buttons": {name: [int(byte), int(bit)] for name, (byte, bit) in sorted(buttons.items())},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


@dataclass
class MappingConfig:
    """Everything about the mapping that a user may reasonably want to adjust."""

    #: logical control -> (button byte index, bit); defaults to the documented map
    button_map: dict = field(default_factory=lambda: dict(DEFAULT_BUTTON_MAP))

    #: logical d-pad direction -> (button byte index, bit)
    dpad_map: dict = field(default_factory=lambda: dict(DPAD_MAP))

    #: per-DS4-axis (source axis index, sign) for gyroscope and accelerometer
    gyro_axis_map: tuple = ((1, -1.0), (2, 1.0), (0, -1.0))
    accel_axis_map: tuple = ((1, -1.0), (2, 1.0), (0, -1.0))

    #: multipliers applied on top of the unit conversion (1.0 = physically 1:1)
    gyro_gain: float = 1.0
    accel_gain: float = 1.0

    #: rotation rates below this many deg/s are forced to zero (sensor noise)
    gyro_deadzone_dps: float = 0.0

    #: Anti-deadzone compensation per axis, in degrees/second as the game sees it:
    #: (pitch, yaw, roll).  Games (Genshin among them) ignore rotation slower than
    #: their own deadzone, so a small constant is added *in the direction the pad is
    #: already turning* to lift slow movements over that threshold.  0 disables the
    #: axis.  Compensation is added after ``gyro_gain``, so the numbers are in the
    #: units the game receives.
    motion_deadzone_dps: tuple[float, float, float] = (0.0, 0.0, 0.0)

    #: Below this rotation rate (degrees/second, after gain) no compensation is
    #: added, so a pad lying still cannot make the view drift on sensor noise.
    motion_deadzone_gate_dps: float = 0.5

    #: fraction of the stick travel ignored around the centre
    stick_deadzone: float = 0.0

    #: invert the stick Y axis (DS4 reports 0 = up)
    invert_stick_y: bool = True

    #: send one DS4 report per IMU sample instead of one per controller report
    interpolate_imu: bool = True

    #: IMU samples the controller packs into one report (sizes the timestamp step
    #: when IMU forwarding is disabled)
    imu_samples_per_report: int = 3

    #: forward motion only; buttons, sticks and triggers stay neutral.  Meant for
    #: setups where a separate physical pad supplies the buttons.
    motion_only: bool = False

    #: make Share act as a touchpad click, in whichever forwarding mode is active.
    #: Useful when the pad's own touchpad button is unreliable over Bluetooth.
    #: Share is remapped (moved), not duplicated.  Off by default; the front-ends
    #: switch it on explicitly (the GUI's checkbox, or --shared-touchpad /
    #: --motion-only on the command line).
    share_as_touchpad: bool = False

    #: exchange the two pairs of face buttons on the virtual pad: ✕↔○ and □↔△.
    #: Games disagree about which physical button means "confirm", so this lets one
    #: controller match either layout without re-learning the controls.  Only those
    #: four bits move -- the d-pad hat, shoulders, triggers and Share/Options are
    #: untouched.  Off by default.
    swap_face_buttons: bool = False

    #: How motion is paced onto the virtual pad: "hardware" (reproduce a real
    #: wired pad: 4.096 ms reports, timestamp +768, sample held over two reports),
    #: "smooth" (interpolated ~400 Hz, pretty waveform) or "raw" (one report per
    #: input report).  See the notes above MOTION_PACING_CHOICES.
    motion_pacing: str = MOTION_PACING_DEFAULT

    #: Also guess across a burst gap by continuing the last sample pair's slope.
    #: Costs less latency than waiting for the next burst, but the guess has to be
    #: retracted when real data arrives, which shows up as small spikes on a
    #: waveform.  Off by default.  Only used by "smooth" pacing.
    motion_extrapolate: bool = False

    #: Output rate used while smoothing.  Must comfortably exceed the 187.5 Hz
    #: the host polls the emulated DualShock 4 at.
    motion_output_hz: float = 400.0

    #: Report interval used by "hardware" pacing, and the timestamp step that
    #: goes with it (kept consistent: step = interval * 187500 counts).  The
    #: default is the real pad's 4.096 ms / 768 counts.
    ds4_report_interval_s: float = DS4_NATIVE_REPORT_INTERVAL_S

    #: swap the IMU sample order within a report (the controller sends newest first)
    reverse_imu_samples: bool = True

    #: digital trigger travel sent when ZL/ZR are pressed
    trigger_max: int = 255


def _apply_axis_map(vector, axis_map):
    return tuple(sign * vector[index] for index, sign in axis_map)


def _clamp_i16(value: float) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return int(round(value))


def _stick_axis_to_ds4(normalized: float, deadzone: float) -> int:
    """-1.0 .. 1.0 -> 0 .. 255 with 128 at centre."""
    if deadzone > 0.0:
        magnitude = abs(normalized)
        if magnitude <= deadzone:
            normalized = 0.0
        else:
            normalized = (magnitude - deadzone) / (1.0 - deadzone) * (1.0 if normalized > 0 else -1.0)
    value = int(round((normalized + 1.0) * 127.5))
    return max(0, min(255, value))


class SwitchToDS4Mapper:
    """Converts controller states into DS4 reports on a virtual pad."""

    def __init__(self, controller: switchpro.SwitchProController,
                 pad: vigem.DS4Controller,
                 config: MappingConfig | None = None):
        self.controller = controller
        self.pad = pad
        self.config = config or MappingConfig()
        self._timestamp = 0
        self._sample_interval_s = DEFAULT_IMU_SAMPLE_INTERVAL_S
        self._last_report_time: float | None = None
        self.last_timestamp_step = 0

    # -- helpers ---------------------------------------------------------- #

    def _advance_timestamp(self, step_s: float | None = None) -> int:
        """Advance the report timestamp by the elapsed time, like real hardware."""
        interval = self._sample_interval_s if step_s is None else step_s
        step = int(round(interval * DS4_TIMESTAMP_UNITS_PER_SECOND))
        step = max(1, min(step, DS4_TIMESTAMP_MODULUS - 1))
        self._timestamp = (self._timestamp + step) % DS4_TIMESTAMP_MODULUS
        self.pad.report.wTimestamp = self._timestamp
        self.last_timestamp_step = step
        return self._timestamp

    def _measure_interval(self, sample_count: int) -> None:
        """Track the real spacing between forwarded IMU samples."""
        now = _clock()
        if self._last_report_time is not None and sample_count:
            interval = (now - self._last_report_time) / sample_count
            if MIN_IMU_SAMPLE_INTERVAL_S <= interval <= MAX_IMU_SAMPLE_INTERVAL_S:
                self._sample_interval_s = interval
        self._last_report_time = now

    def _pressed(self, state: switchpro.ControllerState, control: str) -> bool:
        """Whether a logical control is pressed, honouring any captured map."""
        entry = self.config.button_map.get(control) or self.config.dpad_map.get(control)
        if entry is None:
            return False
        byte_index, switch_bit = entry[0], entry[1]
        return bool(state.buttons[byte_index] & switch_bit)

    def _dpad_hat(self, state: switchpro.ControllerState) -> int:
        up = self._pressed(state, "Up")
        right = self._pressed(state, "Right")
        down = self._pressed(state, "Down")
        left = self._pressed(state, "Left")
        # A DS4 cannot report opposing directions at once; drop the axis entirely.
        if up and down:
            up = down = False
        if left and right:
            left = right = False
        return DPAD_HAT.get((up, right, down, left), 8)

    def _buttons(self, state: switchpro.ControllerState) -> tuple[int, int]:
        buttons = self._dpad_hat(state)
        for name in BUTTON_TARGETS:
            if self._pressed(state, name):
                buttons |= BUTTON_TARGETS[name]

        special = 0
        for _name, (byte_index, switch_bit, ds4_bit) in SPECIAL_MAP.items():
            if state.buttons[byte_index] & switch_bit:
                special |= ds4_bit
        return buttons, special

    def _triggers(self, state: switchpro.ControllerState) -> tuple[int, int]:
        left = self.config.trigger_max if self._pressed(state, "ZL") else 0
        right = self.config.trigger_max if self._pressed(state, "ZR") else 0
        return left, right

    def _sticks(self, state: switchpro.ControllerState) -> tuple[int, int, int, int]:
        left = self.controller.left_stick_calibration
        right = self.controller.right_stick_calibration
        lx = left.x.normalize(state.left_stick[0])
        ly = left.y.normalize(state.left_stick[1])
        rx = right.x.normalize(state.right_stick[0])
        ry = right.y.normalize(state.right_stick[1])
        if self.config.invert_stick_y:
            ly = -ly
            ry = -ry
        return (
            _stick_axis_to_ds4(lx, self.config.stick_deadzone),
            _stick_axis_to_ds4(ly, self.config.stick_deadzone),
            _stick_axis_to_ds4(rx, self.config.stick_deadzone),
            _stick_axis_to_ds4(ry, self.config.stick_deadzone),
        )

    def _motion(self, sample: switchpro.ImuSample) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        calibration = self.controller.imu_calibration
        dps = _apply_axis_map(calibration.to_dps(sample.gyro), self.config.gyro_axis_map)
        g = _apply_axis_map(calibration.to_g(sample.accel), self.config.accel_axis_map)

        if self.config.gyro_deadzone_dps > 0.0:
            dps = tuple(0.0 if abs(v) < self.config.gyro_deadzone_dps else v for v in dps)

        gyro = tuple(
            _clamp_i16(v * DS4_GYRO_COUNTS_PER_DPS * self.config.gyro_gain) for v in dps
        )
        gyro = apply_motion_deadzone(gyro, self.config)
        accel = tuple(
            _clamp_i16(v * DS4_ACCEL_COUNTS_PER_G * self.config.accel_gain) for v in g
        )
        return gyro, accel

    def _battery(self, state: switchpro.ControllerState) -> tuple[int, bool]:
        # Switch reports level 0..4 (bits 5-7); the DS4 accepts 0..10.
        level = max(0, min(4, state.battery_level))
        ds4_level = min(10, int(round(level * 10 / 4)))
        return ds4_level, state.charging or state.wired

    # -- main entry points ------------------------------------------------ #

    def apply_static(self, state: switchpro.ControllerState) -> None:
        """Buttons, d-pad, sticks, triggers and battery (no motion)."""
        buttons, special = self._buttons(state)
        # "-" on the Switch Pro becomes the Share bit, so this turns it into the
        # touchpad click when the user asked for that.
        buttons, special = apply_share_as_touchpad(buttons, special, self.config)
        self.pad.set_buttons(swap_face_buttons(buttons, self.config), special)
        self.pad.set_sticks(*self._sticks(state))
        self.pad.set_triggers(*self._triggers(state))
        level, charging = self._battery(state)
        self.pad.set_battery(level, charging)

    def set_motion(self, sample: switchpro.ImuSample) -> None:
        """Update gyro/accel values without touching the timestamp."""
        gyro, accel = self._motion(sample)
        self.pad.set_motion(gyro, accel)

    def apply_motion(self, sample: switchpro.ImuSample, step_s: float | None = None) -> None:
        self.set_motion(sample)
        self._advance_timestamp(step_s)

    def submit(self) -> None:
        self.pad.submit()

    def push_state(self, state: switchpro.ControllerState) -> int:
        """Push one controller report; returns the number of DS4 reports submitted."""
        self.apply_static(state)
        samples = state.imu
        if not samples:
            self._advance_timestamp(
                self._sample_interval_s * max(1, self.config.imu_samples_per_report))
            self.submit()
            return 1

        self._measure_interval(len(samples))

        if self.config.interpolate_imu:
            ordered = list(reversed(samples)) if self.config.reverse_imu_samples else list(samples)
            for sample in ordered:
                self.apply_motion(sample)
                self.submit()
            # Leave the newest sample in the report for the next static update
            # (values only: the timestamp must not advance twice for one report).
            self.set_motion(ordered[-1])
            return len(ordered)

        newest = samples[0] if self.config.reverse_imu_samples else samples[-1]
        self.apply_motion(newest, step_s=self._sample_interval_s * len(samples))
        self.submit()
        return 1

    def reset(self) -> None:
        """Return the virtual pad to a neutral, still state."""
        self.pad.reset()


#: Share sits in the second button byte, bit 4; the touchpad click is bit 1 of the
#: "special" byte (the same place real hardware reports it).
SHARE_MASK = 1 << 12
TOUCHPAD_MASK = 0x02

#: The pairs "swap face buttons" exchanges, as DS4 button bits.  The low nibble of
#: the button word is the d-pad hat, so these bits never collide with it.
FACE_BUTTON_PAIRS = (
    (vigem.DS4_BUTTONS["cross"], vigem.DS4_BUTTONS["circle"]),      # ✕ <-> ○
    (vigem.DS4_BUTTONS["square"], vigem.DS4_BUTTONS["triangle"]),   # □ <-> △
)


def swap_face_buttons(buttons: int, config: "MappingConfig") -> int:
    """Exchange ✕↔○ and □↔△ in a DS4 button word.

    A pair is exchanged by flipping both of its bits, which is only right when
    exactly one of the two is down.  When both are down (or neither is), each
    state maps onto itself, so the word is returned unchanged instead.
    """
    if not config.swap_face_buttons:
        return buttons
    for first, second in FACE_BUTTON_PAIRS:
        if bool(buttons & first) != bool(buttons & second):
            buttons ^= first | second
    return buttons


def apply_motion_deadzone(gyro, config: "MappingConfig"):
    """Add the anti-deadzone bias to a gyro triple already in DS4 counts.

    Order of the triple is the DS4's own: (pitch X, yaw Y, roll Z) -- i.e. exactly
    what the game receives, so the three sliders mean what they say.

    A tiny rotation is left alone: only rates above ``motion_deadzone_gate_dps``
    get the bias, so a pad that is lying still (sensor noise only) never drifts.
    The bias keeps the sign of the measurement, so the view always turns the same
    way the pad does.
    """
    comp = tuple(config.motion_deadzone_dps)
    if not any(comp):
        return tuple(gyro)
    gate = config.motion_deadzone_gate_dps * DS4_GYRO_COUNTS_PER_DPS
    out = []
    for value, amount in zip(gyro, comp):
        if not amount or abs(value) <= gate:
            out.append(value)
            continue
        bias = amount * DS4_GYRO_COUNTS_PER_DPS
        out.append(_clamp_i16(value + bias if value > 0 else value - bias))
    return tuple(out)


def apply_share_as_touchpad(buttons: int, special: int, config: "MappingConfig",
                            share_pressed: bool | None = None) -> tuple[int, int]:
    """Move the Share bit onto the touchpad click when that switch is on.

    Works for both input paths by looking at the *output* bits: a DualShock 4
    reports Share as this bit, and the Switch Pro's Minus button is mapped onto
    the same bit -- so "Share 或 -" both end up here.  It is a move, not a copy,
    so a game that tells Share and the touchpad apart never sees both at once.

    ``share_pressed`` overrides where the press is detected from; motion-only mode
    zeroes the output buttons on purpose, so there the caller passes the state.
    """
    if not config.share_as_touchpad:
        return buttons, special
    pressed = bool(buttons & SHARE_MASK) if share_pressed is None else share_pressed
    if not pressed:
        return buttons, special
    return buttons & ~SHARE_MASK, special | TOUCHPAD_MASK


class MotionResampler:
    """Turn an uneven, bursty stream of real IMU samples into a smooth one.

    Why this exists: the host polls a DualShock 4's HID interrupt endpoint every
    5.33 ms (187.5 Hz, straight from the descriptor) and *every poll returns the
    most recent report*, while a Bluetooth pad only produces ~125 samples/s.  A
    plain one-report-per-sample relay therefore hands back a duplicate on about a
    third of the polls -- two identical samples in a row, which shows up as a
    staircase in waveform tools and as jitter while aiming.

    Worse, the Bluetooth link delivers those samples in *bursts*: two samples
    about 8 ms apart arrive together every ~16 ms (measured: arrival gap median
    15 ms, minimum 0 ms).  Interpolating by arrival time would therefore see 0 ms
    and 16 ms gaps and still produce plateaus, so samples are placed and
    interpolated in the **pad's own timestamp domain** instead, which is exactly
    the 5.33 us counter the pad reports.

    Rendering then lags by one measured burst period (about 16 ms on Bluetooth,
    much less on USB) so the two real samples always bracket the time being
    drawn.  Interpolating rather than extrapolating means no invented motion.
    """

    def __init__(self, capacity: int = 48, extrapolate: bool = False):
        self._lock = threading.Lock()
        self._samples: deque = deque(maxlen=capacity)    # (stamp, gyro, accel)
        self._arrivals: deque = deque(maxlen=capacity)   # wall clock per sample
        self._last_raw: int | None = None
        self._newest_at = 0.0
        #: Burst period of the link: ~16 ms on Bluetooth, ~4 ms on USB.
        self.burst_s = 0.016
        #: Pad-time step between two samples: ~1500 counts (8 ms) on Bluetooth.
        self.step_units = DS4_TIMESTAMP_UNITS_PER_SECOND * 0.008
        #: Continue the last pair's slope across a burst gap.  Saves latency but
        #: guesses at the motion, and the guess has to be taken back when the next
        #: burst arrives -- that correction is the little spike seen on a
        #: waveform.  Off by default: a smooth, truthful curve matters more here
        #: than the extra ~12 ms.
        self.extrapolate = extrapolate

    # -- input ------------------------------------------------------------ #

    def add(self, at: float, raw_stamp: int, gyro, accel) -> None:
        """Queue one real sample; ``raw_stamp`` is the pad's 16-bit counter."""
        with self._lock:
            if self._samples and self._last_raw is not None:
                step = (raw_stamp - self._last_raw) % DS4_TIMESTAMP_MODULUS
                stamp = self._samples[-1][0] + step
            else:
                stamp = float(raw_stamp)
            self._last_raw = raw_stamp
            self._samples.append((stamp, tuple(gyro), tuple(accel)))
            self._arrivals.append(at)
            self._newest_at = at
            self._update_timing_locked()

    def _update_timing_locked(self) -> None:
        """Track the burst period and the per-sample step (both in pad time).

        The 90th-percentile gap, not the maximum: one early hiccup would pin a
        maximum at the clamp forever, and an over-estimated lag makes the render
        time fall before the oldest buffered sample, where interpolation clamps
        and the output goes back to being steppy.
        """
        if len(self._arrivals) >= 6:
            arrivals = list(self._arrivals)
            gaps = sorted(b - a for a, b in zip(arrivals, arrivals[1:]))
            period = gaps[min(len(gaps) - 1, int(len(gaps) * 0.9))]
            self.burst_s = min(0.024, max(0.004, period))
        if len(self._samples) >= 4:
            steps = sorted(b[0] - a[0] for a, b in zip(self._samples, list(self._samples)[1:])
                           if b[0] > a[0])
            if steps:
                self.step_units = float(steps[len(steps) // 2])

    def ready(self) -> bool:
        with self._lock:
            return len(self._samples) >= 2

    @property
    def latency_units(self) -> float:
        """How far behind the pad's clock the rendered instant sits.

        Extrapolating needs only one sample step, because the rest of a burst gap
        is guessed.  Pure interpolation has to sit a whole burst period back, so
        that the two buffered samples always bracket the instant being drawn --
        that is the price of never inventing motion (and of never having to take
        a guess back, which is what the little spikes were).
        """
        if self.extrapolate:
            return self.step_units
        return (self.burst_s * DS4_TIMESTAMP_UNITS_PER_SECOND
                + self.step_units * 0.5)

    def clock_at(self, now: float) -> float:
        """Where the pad's own timestamp counter is *now*, extrapolated."""
        with self._lock:
            if not self._samples:
                return 0.0
            return (self._samples[-1][0]
                    + (now - self._newest_at) * DS4_TIMESTAMP_UNITS_PER_SECOND)

    # -- output ----------------------------------------------------------- #

    def sample_at(self, when: float):
        """Interpolate at pad time ``when``, extrapolating a bounded amount.

        Within the buffered range this is plain linear interpolation.  Past the
        newest sample -- which happens for most of a Bluetooth burst gap -- the
        slope of the last sample pair is continued, but never for more than one
        sample step, so a sudden stop cannot produce a long invented swing.
        """
        with self._lock:
            if not self._samples:
                return None
            if len(self._samples) == 1 or when <= self._samples[0][0]:
                return self._samples[0][1:]
            newest = self._samples[-1]
            if when >= newest[0]:
                if not self.extrapolate:
                    # Past the newest real sample: hold it.  With the lag set to a
                    # whole burst period this only happens when a burst is late,
                    # and holding is the honest answer -- the alternative is the
                    # invented-and-then-retracted motion that shows up as spikes.
                    return newest[1:]
                # Continue the last sample pair's slope.  The cap is one burst
                # period -- enough to cover a whole gap so the output never
                # freezes -- while still bounding how much motion can be invented
                # if the pad goes quiet.
                allowed = max(self.step_units,
                              self.burst_s * DS4_TIMESTAMP_UNITS_PER_SECOND)
                offset = min(when - newest[0], allowed)
                older = self._samples[-2]
                span = newest[0] - older[0]
                if span <= 0:
                    return newest[1:]
                gain = offset / span
                gyro = tuple(b + (b - a) * gain
                             for a, b in zip(older[1], newest[1]))
                accel = tuple(b + (b - a) * gain
                              for a, b in zip(older[2], newest[2]))
                return gyro, accel
            samples = list(self._samples)
        for older, newer in zip(samples, samples[1:]):
            if older[0] <= when <= newer[0]:
                span = newer[0] - older[0]
                fraction = 0.0 if span <= 0 else (when - older[0]) / span
                gyro = tuple(a + (b - a) * fraction
                             for a, b in zip(older[1], newer[1]))
                accel = tuple(a + (b - a) * fraction
                              for a, b in zip(older[2], newer[2]))
                return gyro, accel
        return samples[-1][1:]


class DS4ToDS4Relay:
    """Relay a physical DualShock 4 onto the virtual one, field for field.

    No axis shuffling and no unit conversion: the input already speaks the
    DualShock 4 frame and units, so the only thing that changes is the *report
    format*.  That is the whole point -- a Bluetooth pad sends report 0x11 with a
    two-byte header, which games such as Genshin Impact do not read, while the
    virtual pad always emits the wired 0x01 report.

    The timestamp is forwarded as-is when it behaves like real hardware's (it
    counts 5.33 us units); if a pad ever reports a frozen timestamp, one is
    synthesised from elapsed time instead so a game's dt-based filtering keeps
    working.

    When smoothing is enabled (the default) the motion is not submitted once per
    input report: the real samples are collected and a background thread renders
    the virtual pad on a steady 500 Hz cadence with interpolated values, because
    the host polls the emulated device at a fixed 187.5 Hz and repeats the last
    report on every poll.  See :class:`MotionResampler`.
    """

    def __init__(self, controller, pad: vigem.DS4Controller,
                 config: MappingConfig | None = None):
        self.controller = controller
        self.pad = pad
        self.config = config or MappingConfig()
        self._timestamp = 0
        self._last_input_timestamp: int | None = None
        self._unchanged = 0
        self._synthetic = False
        self._last_submit: float | None = None
        self._resampler = MotionResampler(extrapolate=self.config.motion_extrapolate)
        self._lock = threading.Lock()
        self._latest = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.submitted = 0
        self.duplicates = 0
        self._last_rendered = None
        self._previous_rendered = None

    # -- field writing ---------------------------------------------------- #

    def _write_inputs(self, state) -> None:
        """Buttons, sticks, triggers, special and battery from a real state."""
        report = self.pad.report
        if self.config.motion_only:
            # "Genshin mode": hand the game motion only, and leave buttons,
            # sticks and triggers neutral so a physically connected pad can keep
            # supplying those without the two devices fighting over them.
            report.bThumbLX = report.bThumbLY = report.bThumbRX = report.bThumbRY = 0x80
            report.wButtons = 0x0008          # hat centred, nothing pressed
            report.bTriggerL = report.bTriggerR = 0
            report.bSpecial = 0
        else:
            report.bThumbLX, report.bThumbLY, report.bThumbRX, report.bThumbRY = state.sticks
            report.wButtons = state.buttons
            report.bSpecial = state.special
            report.bTriggerL, report.bTriggerR = state.triggers
        self._apply_share_mapping(state, report)
        # Last thing before the report is submitted: the face buttons the game sees.
        report.wButtons = swap_face_buttons(report.wButtons, self.config)
        self.pad.set_battery_byte(state.battery)

    def _apply_share_mapping(self, state, report) -> None:
        """Make Share act as the touchpad click (opt-in, any forwarding mode).

        Some pads report an unreliable touchpad button over Bluetooth, so Share is
        remapped instead: it is *moved*, not duplicated, so a game that treats
        Share and the touchpad separately can never see both at once.
        """
        buttons, special = apply_share_as_touchpad(
            report.wButtons, report.bSpecial, self.config,
            share_pressed=bool(state.buttons & SHARE_MASK))
        report.wButtons = buttons
        report.bSpecial = special

    def _write_motion(self, gyro, accel) -> None:
        report = self.pad.report
        # Sensitivity: scale the rotation rate the game sees.  Acceleration is
        # left alone by default -- scaling gravity would break the tilt reference
        # that games use alongside the gyro.
        if self.config.gyro_gain != 1.0:
            gyro = tuple(value * self.config.gyro_gain for value in gyro)
        if self.config.accel_gain != 1.0:
            accel = tuple(value * self.config.accel_gain for value in accel)
        # Anti-deadzone: lift slow rotations over the game's own deadzone.
        gyro = apply_motion_deadzone(gyro, self.config)
        values = (int(round(gyro[0])), int(round(gyro[1])), int(round(gyro[2])),
                  int(round(accel[0])), int(round(accel[1])), int(round(accel[2])))
        gyro_out = tuple(max(-32768, min(32767, value)) for value in values[:3])
        accel_out = tuple(max(-32768, min(32767, value)) for value in values[3:])
        report.wGyroX, report.wGyroY, report.wGyroZ = gyro_out
        report.wAccelX, report.wAccelY, report.wAccelZ = accel_out
        self._last_rendered = (gyro_out, accel_out)

    # -- paced streaming --------------------------------------------------- #

    @property
    def pacing(self) -> str:
        return self.config.motion_pacing

    @property
    def smoothing(self) -> bool:
        """True when a background thread paces reports (either paced mode)."""
        return self.pacing in ("hardware", "smooth")

    def start_streaming(self) -> None:
        """Begin writing the virtual pad on a fixed cadence."""
        if not self.smoothing or self._thread is not None:
            return
        self._stop.clear()
        self._timer_raised = set_timer_resolution(1)
        self._thread = threading.Thread(target=self._render_loop, daemon=True,
                                        name="ds4-motion-render")
        self._thread.start()

    def stop_streaming(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        if getattr(self, "_timer_raised", False):
            set_timer_resolution(0)
            self._timer_raised = False

    def feed(self, state) -> None:
        """Accept a real sample; the render thread does the submitting."""
        with self._lock:
            self._latest = state
        if getattr(state, "motion_valid", True):
            self._resampler.add(_clock(), state.timestamp, state.gyro,
                                state.accel)

    def _render_loop(self) -> None:
        if self.pacing == "hardware":
            # Exactly what a real wired DualShock 4 does: a report every 4.096 ms
            # (configurable) whose timestamp advances consistently with it,
            # carrying the newest IMU sample -- so the motion values legitimately
            # repeat while the sample rate stays at the pad's own ~125-167 Hz.
            # Games integrate yaw per report, so reproducing the real cadence (and
            # a timestamp that agrees with it) is what keeps their yaw correct.
            interval = max(1e-4, self.config.ds4_report_interval_s)
        else:
            interval = 1.0 / max(60.0, self.config.motion_output_hz)
        spin = min(0.0004, interval / 3.0)      # final stretch, busy-waited
        next_at = time.perf_counter() + interval
        while not self._stop.is_set():
            remaining = next_at - time.perf_counter()
            if remaining > 0:
                # Sleep most of the way, then spin the last fraction of a
                # millisecond.  Windows rounds sleeps up to its timer tick: with
                # a plain sleep this loop woke in bursts at ~210 calls/s with two
                # thirds of them carrying the same value, which is not enough to
                # stay ahead of the 187.5 Hz the virtual pad is polled at.
                if remaining > spin * 2.0:
                    time.sleep(remaining - spin)
                while not self._stop.is_set() and time.perf_counter() < next_at:
                    pass
            if self._stop.is_set():
                break
            now = _clock()
            next_at += interval
            if next_at < time.perf_counter() - interval:   # fell behind: resync
                next_at = time.perf_counter() + interval
            try:
                self.render(now)
            except vigem.ViGEmError:
                break
            except Exception as exc:  # pragma: no cover - defensive
                # A dead render thread would silently stop all motion, so make it
                # loud instead of leaving a frozen pad behind.
                print(f"motion rendering stopped: {exc!r}", flush=True)
                break

    def render(self, now: float | None = None) -> bool:
        """Write and submit one report; False when not ready."""
        now = _clock() if now is None else now
        with self._lock:
            state = self._latest
        if state is None:
            return False
        hardware = self.pacing == "hardware"
        if not hardware and not self._resampler.ready():
            return False
        self._write_inputs(state)
        if not getattr(state, "motion_valid", True):
            # No motion in the input at all (minimal Bluetooth report): hold the
            # sensor still rather than inventing values.
            self._write_motion((0, 0, 0), (0, 0, 0))
            self.pad.report.wTimestamp = self._synthetic_timestamp(now)
        elif hardware:
            # Real-wired-pad behaviour: whatever the newest real sample says, and
            # a timestamp step that matches the configured report interval
            # (768 counts = 4.096 ms by default, the pad's own measured step).
            # The gyro/accel values therefore repeat between IMU updates, which is
            # what actual hardware does and what the game's yaw integration
            # expects.  No interpolation, no extrapolation, no added latency.
            self._write_motion(state.gyro, state.accel)
            step = max(1, int(round(self.config.ds4_report_interval_s
                                    * DS4_TIMESTAMP_UNITS_PER_SECOND)))
            self._timestamp = (self._timestamp + step) % DS4_TIMESTAMP_MODULUS
            self.pad.report.wTimestamp = self._timestamp
        else:
            # Smoothed: interpolate in the pad's own timestamp domain, one burst
            # period behind its clock so the two real samples always bracket the
            # moment being drawn.
            when = self._resampler.clock_at(now) - self._resampler.latency_units
            motion = self._resampler.sample_at(when)
            if motion is None:
                return False
            self._write_motion(*motion)
            self.pad.report.wTimestamp = int(when) % DS4_TIMESTAMP_MODULUS
        self.pad.submit()
        self.submitted += 1
        if self._last_rendered and self._last_rendered == self._previous_rendered:
            self.duplicates += 1
        self._previous_rendered = self._last_rendered
        return True

    def _synthetic_timestamp(self, now: float) -> int:
        """A steady 5.33 us timestamp; the pad's own is only per-report coarse."""
        if self._last_submit is not None:
            delta = DS4_TIMESTAMP_UNITS_PER_SECOND * (now - self._last_submit)
        else:
            delta = DS4_TIMESTAMP_UNITS_PER_SECOND * self._resampler.interval_s
        self._last_submit = now
        self._timestamp = (self._timestamp + int(round(delta))) % DS4_TIMESTAMP_MODULUS
        return self._timestamp

    # -- timestamp -------------------------------------------------------- #

    def _resolve_timestamp(self, state) -> int:
        now = _clock()
        if not self._synthetic:
            if self._last_input_timestamp is None:
                self._last_input_timestamp = state.timestamp
            elif state.timestamp == self._last_input_timestamp:
                self._unchanged += 1
                if self._unchanged > 5:
                    self._synthetic = True
                    print("note: the pad's report timestamp is not advancing; "
                          "synthesising one from elapsed time", flush=True)
            else:
                self._unchanged = 0
                self._last_input_timestamp = state.timestamp
            if not self._synthetic:
                self._timestamp = state.timestamp
                self._last_submit = now
                return self._timestamp
        return self._synthetic_timestamp(now)

    # -- main entry point ------------------------------------------------- #

    def push_state(self, state) -> int:
        if self.smoothing:
            # Only collect: the render thread owns every write to the pad, so
            # there is never a half-updated report being submitted.
            self.feed(state)
            self.start_streaming()
            return 0
        report = self.pad.report
        self._write_inputs(state)
        if getattr(state, "motion_valid", True):
            self._write_motion(state.gyro, state.accel)
        else:
            # Minimal Bluetooth report: the pad sends no motion at all, so relay
            # a still sensor rather than whatever bytes happen to sit there.
            self._write_motion((0, 0, 0), (0, 0, 0))
        report.wTimestamp = self._resolve_timestamp(state)
        self.pad.submit()
        return 1

    # -- feed the physical pad's own outputs back (rumble passthrough) ---- #

    def reset(self) -> None:
        self.stop_streaming()
        self.pad.reset()
    # -- feed the physical pad's own outputs back (rumble passthrough) ---- #

    def reset(self) -> None:
        self.stop_streaming()
        self.pad.reset()

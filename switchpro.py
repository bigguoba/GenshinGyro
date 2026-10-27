"""Nintendo Switch Pro Controller (and Joy-Con charging grip) HID protocol.

Implements the transport, handshake, subcommand, input-report and calibration
handling needed to read a Pro Controller over **USB** and **Bluetooth**.

Sources for every wire format here:
  * dekuNukem, Nintendo_Switch_Reverse_Engineering
      bluetooth_hid_notes.md, bluetooth_hid_subcommands_notes.md,
      USB-HID-Notes.md, imu_sensor_notes.md, spi_flash_notes.md
  * SDL2 src/joystick/hidapi/SDL_hidapi_switch.c (handshake order, packet sizes,
    calibration parsing) -- the same logic that ships in SDL/Steam.

Report layout used throughout (byte 0 is the HID report id):

    0x30 / 0x21 input report
      0        report id (0x30 = full state, 0x21 = subcommand reply)
      1        timer
      2        high nibble: battery level (0,2,4,6,8; bit0 = charging)
               low nibble:  connection info
      3..5     buttons (right, shared, left)
      6..8     left stick, 12-bit packed
      9..11    right stick, 12-bit packed
      12       vibrator
      13..48   3 x [accel x,y,z, gyro x,y,z] as int16 LE (0x30 only)
      13       (0x21) subcommand ACK, MSB set = ACK
      14       (0x21) echoed subcommand id
      15..     (0x21) reply payload
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field

import hidwin

# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #

SWITCH_PRO_VID = 0x057E
SWITCH_PRO_PID = 0x2009  # Pro Controller (and charging grip) on USB and BT

INPUT_REPORT_FULL = 0x30
INPUT_REPORT_SUBCMD_REPLY = 0x21
INPUT_REPORT_SIMPLE = 0x3F
INPUT_REPORT_PROPRIETARY_ACK = 0x81

OUTPUT_REPORT_RUMBLE_AND_SUBCMD = 0x01
OUTPUT_REPORT_RUMBLE = 0x10
OUTPUT_REPORT_PROPRIETARY = 0x80

SUB_REQUEST_DEVICE_INFO = 0x02
SUB_SET_INPUT_REPORT_MODE = 0x03
SUB_SPI_FLASH_READ = 0x10
SUB_SET_PLAYER_LIGHTS = 0x30
SUB_ENABLE_IMU = 0x40
SUB_SET_IMU_SENSITIVITY = 0x41
SUB_ENABLE_VIBRATION = 0x48

PROP_STATUS = 0x01
PROP_HANDSHAKE = 0x02
PROP_HIGH_SPEED = 0x03
PROP_FORCE_USB = 0x04
PROP_CLEAR_USB = 0x05

#: Neutral (silent) rumble payload; needed because every output report carries one.
NEUTRAL_RUMBLE = bytes((0x00, 0x01, 0x40, 0x40, 0x00, 0x01, 0x40, 0x40))

#: Rumble encoding, from dekuNukem/Nintendo_Switch_Reverse_Engineering
#: (rumble_data_table.md): each side is four bytes holding two bands --
#:   byte0 = high-band frequency code (low byte), byte1 = high-band amplitude
#:           with the frequency's 9th bit on top,
#:   byte2 = low-band frequency code in the low 7 bits, low-band amplitude's
#:           high bits above, byte3 = low-band amplitude's low byte.
#: Frequency code = round(log2(hz / 10) * 32); the high band is offset by 0x60 and
#: the low band by 0x40.
#:
#: The amplitude table is a long list of (high, low16) rows whose entries follow a
#: simple pattern: row i has high = 2*i and low16 = (i odd ? 0x8000 : 0) |
#: (0x40 + i // 2).  Its non-linear "amp" column reaches 404 at row 33, which is
#: where a real Pro Controller tops out, so a level of 1.0 means row 33.
RUMBLE_INDEX_MAX = 33
#: Default carrier frequencies for a whole-body rumble.
RUMBLE_LOW_HZ = 160.0
RUMBLE_HIGH_HZ = 320.0


def _rumble_frequency_code(hz: float, band: str) -> int:
    """Documented frequency code: round(log2(hz / 10) * 32), clamped per band."""
    if band == "high":
        hz = min(1252.0, max(82.0, hz))
        return int(round(math.log2(hz / 10.0) * 32))
    hz = min(626.0, max(41.0, hz))
    return int(round(math.log2(hz / 10.0) * 32))


def _rumble_amplitude(level: float) -> tuple[int, int]:
    """(high byte, low 16-bit value) for a level in 0..1, from the table pattern."""
    index = int(round(max(0.0, min(1.0, level)) * RUMBLE_INDEX_MAX))
    high = 2 * index
    low = (0x8000 if index % 2 else 0x0000) | (0x40 + index // 2)
    return high, low


def rumble_side_bands(low_amplitude: float, low_hz: float,
                      high_amplitude: float, high_hz: float) -> bytes:
    """Drive one actuator's two bands with independent amplitude and frequency.

    A rotor-motor pad has no use for the band split, but this is what makes it
    possible to hold the frequency constant and vary only the strength (or the
    other way round) while working out what a particular pad responds to.
    """
    high_byte, _ = _rumble_amplitude(high_amplitude)
    _, low_word = _rumble_amplitude(low_amplitude)
    high_frequency = (_rumble_frequency_code(high_hz, "high") - 0x60) * 4
    low_code = _rumble_frequency_code(low_hz, "low")
    return bytes((
        high_frequency & 0xFF,
        (high_byte & 0x7F) | ((high_frequency >> 8) & 0x01),
        ((low_code - 0x40) & 0x7F) | ((low_word >> 8) & 0x80),
        low_word & 0xFF,
    ))


def rumble_side(level: float, low_hz: float = RUMBLE_LOW_HZ,
                high_hz: float = RUMBLE_HIGH_HZ) -> bytes:
    """The four bytes that drive one actuator, for a level in 0..1 (both bands)."""
    return rumble_side_bands(level, low_hz, level, high_hz)


def rumble_payload(left: float, right: float, low_hz: float = RUMBLE_LOW_HZ,
                   high_hz: float = RUMBLE_HIGH_HZ) -> bytes:
    """The eight-byte payload of a rumble-only (0x10) output report."""
    return (rumble_side(left, low_hz, high_hz)
            + rumble_side(right, low_hz, high_hz))


#: Payload that actually stops a pad which reads the raw bytes as drive values.
#: The documented neutral (00 01 40 40 per side) is *not* silence on such clones:
#: they treat 0x40 as a quarter of the scale and keep buzzing.  An all-zero payload
#: stops them; verified against a third-party pad in NS mode.
RUMBLE_SILENCE = bytes(8)


def rumble_side_raw(low_byte: int, high_byte: int = 0x01, frequency_byte: int = 0x40,
                    high_frequency_byte: int = 0x00) -> bytes:
    """One side built from raw bytes, for probing pads with a non-standard mapping."""
    return bytes((high_frequency_byte & 0xFF, high_byte & 0xFF,
                  frequency_byte & 0xFF, low_byte & 0xFF))


#: Third-party pads in NS mode often ignore the documented band encoding and treat
#: one byte of each side as a raw 0..255 drive.  Measured on such a pad: only the
#: second byte of the side group changes the strength, monotonically, and an
#: all-zero group stops it.  Building payloads this way uses the whole range
#: instead of the narrow slice the documented table covers.
RUMBLE_RAW_DRIVE_BYTE = 2


def rumble_side_level(level: float) -> bytes:
    """One side at ``level`` 0..1 using the raw-drive byte layout."""
    drive = int(round(max(0.0, min(1.0, level)) * 255.0))
    side = bytearray(4)
    side[RUMBLE_RAW_DRIVE_BYTE] = drive & 0xFF
    return bytes(side)


def rumble_payload_level(left: float, right: float) -> bytes:
    """Eight-byte payload for the raw-drive layout; all-zero means silence."""
    return rumble_side_level(left) + rumble_side_level(right)

USB_PACKET_LEN = 64
BLUETOOTH_PACKET_LEN = 49

DEVICE_TYPE_JOYCON_LEFT = 1
DEVICE_TYPE_JOYCON_RIGHT = 2
DEVICE_TYPE_PRO_CONTROLLER = 3

DEVICE_TYPE_NAMES = {
    0: "unknown",
    DEVICE_TYPE_JOYCON_LEFT: "Joy-Con (L)",
    DEVICE_TYPE_JOYCON_RIGHT: "Joy-Con (R)",
    DEVICE_TYPE_PRO_CONTROLLER: "Pro Controller",
}

#: SPI flash addresses of factory calibration blocks.
SPI_IMU_CALIBRATION = 0x6020
SPI_IMU_CALIBRATION_LEN = 0x18
SPI_STICK_CALIBRATION = 0x603D
SPI_STICK_CALIBRATION_LEN = 0x12

#: User calibration (written when the controller is calibrated on a Switch).
#: When the magic bytes are present the controller itself uses these values, so
#: we prefer them too.  Only the origins are taken from the user block; the
#: sensitivity coefficients stay factory.
SPI_STICK_USER_CALIBRATION = 0x8010
SPI_STICK_USER_CALIBRATION_LEN = 0x16
SPI_IMU_USER_CALIBRATION = 0x8026
SPI_IMU_USER_CALIBRATION_LEN = 0x1A
USER_CALIBRATION_MAGIC = b"\xB2\xA1"

SPI_READ_CHUNK = 0x1D  # hardware maximum per subcommand 0x10

#: The controller resets or drops the link if subcommands arrive too fast.
#: (Linux hid-nintendo.c: JC_SUBCMD_RATE_LIMITER_USB_MS 20 / _BT_MS 60.)
SUBCOMMAND_INTERVAL_USB_S = 0.020
SUBCOMMAND_INTERVAL_BLUETOOTH_S = 0.060

#: Fallback sensors: the SPI coefficients are always the +-8 g / +-2000 dps
#: sensitivity ones, so these literals reproduce 4/16384 g and 936/13371 dps
#: per count.  (Linux DFLT_ACCEL_SCALE / DFLT_GYRO_SCALE.)
DEFAULT_ACCEL_COEFF = 16384
DEFAULT_GYRO_COEFF = 13371


class SwitchProError(RuntimeError):
    pass


class DeviceGoneError(SwitchProError):
    """The controller was unplugged / left the Bluetooth range mid-session."""


# --------------------------------------------------------------------------- #
# Bit definitions
# --------------------------------------------------------------------------- #

#: buttons[0] -- right side of the controller (byte 3 of the report)
BUTTON_Y = 0x01
BUTTON_X = 0x02
BUTTON_B = 0x04
BUTTON_A = 0x08
BUTTON_SR_R = 0x10
BUTTON_SL_R = 0x20
BUTTON_R = 0x40
BUTTON_ZR = 0x80

#: buttons[1] -- shared (byte 4)
BUTTON_MINUS = 0x01
BUTTON_PLUS = 0x02
BUTTON_R_STICK = 0x04
BUTTON_L_STICK = 0x08
BUTTON_HOME = 0x10
BUTTON_CAPTURE = 0x20
BUTTON_CHARGING_GRIP = 0x80

#: buttons[2] -- left side (byte 5)
BUTTON_DOWN = 0x01
BUTTON_UP = 0x02
BUTTON_RIGHT = 0x04
BUTTON_LEFT = 0x08
BUTTON_SR_L = 0x10
BUTTON_SL_L = 0x20
BUTTON_L = 0x40
BUTTON_ZL = 0x80


# --------------------------------------------------------------------------- #
# Parsed state
# --------------------------------------------------------------------------- #

@dataclass
class ImuSample:
    """One 6-axis sample: raw int16 accelerometer and gyroscope values."""

    accel: tuple[int, int, int]
    gyro: tuple[int, int, int]


@dataclass
class ControllerState:
    report_id: int
    timer: int
    battery_level: int
    charging: bool
    connection_info: int
    buttons: tuple[int, int, int]
    left_stick: tuple[int, int]
    right_stick: tuple[int, int]
    vibration_code: int
    imu: list[ImuSample] = field(default_factory=list)
    wired: bool = False

    @property
    def battery_level_8(self) -> int:
        """Battery level on the 0/2/4/6/8 scale the protocol notes use."""
        return self.battery_level * 2

    @property
    def is_joycon(self) -> bool:
        return ((self.connection_info >> 1) & 0x03) == 0x03

    # -- convenience button accessors ------------------------------------- #

    @property
    def a(self) -> bool:
        return bool(self.buttons[0] & BUTTON_A)

    @property
    def b(self) -> bool:
        return bool(self.buttons[0] & BUTTON_B)

    @property
    def x(self) -> bool:
        return bool(self.buttons[0] & BUTTON_X)

    @property
    def y(self) -> bool:
        return bool(self.buttons[0] & BUTTON_Y)

    @property
    def pressed_names(self) -> list[str]:
        names = []
        b0, b1, b2 = self.buttons
        for mask, name in (
            (BUTTON_Y, "Y"), (BUTTON_X, "X"), (BUTTON_B, "B"), (BUTTON_A, "A"),
            (BUTTON_SR_R, "SR"), (BUTTON_SL_R, "SL"), (BUTTON_R, "R"), (BUTTON_ZR, "ZR"),
        ):
            if b0 & mask:
                names.append(name)
        for mask, name in (
            (BUTTON_MINUS, "Minus"), (BUTTON_PLUS, "Plus"), (BUTTON_R_STICK, "RStick"),
            (BUTTON_L_STICK, "LStick"), (BUTTON_HOME, "Home"), (BUTTON_CAPTURE, "Capture"),
            (BUTTON_CHARGING_GRIP, "ChargingGrip"),
        ):
            if b1 & mask:
                names.append(name)
        for mask, name in (
            (BUTTON_DOWN, "Down"), (BUTTON_UP, "Up"), (BUTTON_RIGHT, "Right"),
            (BUTTON_LEFT, "Left"), (BUTTON_SR_L, "SR(L)"), (BUTTON_SL_L, "SL(L)"),
            (BUTTON_L, "L"), (BUTTON_ZL, "ZL"),
        ):
            if b2 & mask:
                names.append(name)
        return names


def parse_state(report: bytes) -> ControllerState | None:
    """Parse a 0x30 (or 0x21) input report.  Returns ``None`` for other reports."""
    if not report:
        return None
    report_id = report[0]
    if report_id not in (INPUT_REPORT_FULL, INPUT_REPORT_SUBCMD_REPLY):
        return None
    if len(report) < 49:
        raise SwitchProError(f"short input report: {len(report)} bytes")

    battery_byte = report[2]
    # Battery byte: bits 5-7 level (0 empty .. 4 full), bit 4 charging,
    # bits 1-2 controller kind, bit 0 powered over the rail/USB.
    state = ControllerState(
        report_id=report_id,
        timer=report[1],
        battery_level=(battery_byte >> 5) & 0x07,
        charging=bool(battery_byte & 0x10),
        connection_info=battery_byte & 0x0F,
        buttons=(report[3], report[4], report[5]),
        left_stick=decode_stick(report, 6),
        right_stick=decode_stick(report, 9),
        vibration_code=report[12],
        wired=bool(battery_byte & 0x01),
    )
    if report_id == INPUT_REPORT_FULL:
        for index in range(3):
            base = 13 + index * 12
            accel = struct.unpack_from("<3h", report, base)
            gyro = struct.unpack_from("<3h", report, base + 6)
            state.imu.append(ImuSample(accel=accel, gyro=gyro))
    return state


def decode_stick(report: bytes, offset: int) -> tuple[int, int]:
    """Decode the 12-bit packed stick pair at ``offset``."""
    low = report[offset]
    middle = report[offset + 1]
    high = report[offset + 2]
    horizontal = low | ((middle & 0x0F) << 8)
    vertical = (middle >> 4) | (high << 4)
    return horizontal, vertical


def subcommand_reply(report: bytes) -> tuple[int, int, bytes] | None:
    """Return ``(ack, subcommand_id, payload)`` for a 0x21 reply report."""
    if not report or report[0] != INPUT_REPORT_SUBCMD_REPLY:
        return None
    if len(report) < 15:
        return None
    return report[13], report[14], bytes(report[15:])


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #

def _pack12(data: bytes, offset: int) -> list[int]:
    """Unpack the 3-byte groups of the stick calibration format."""
    values = []
    for group in range(3):
        base = offset + group * 3
        b0, b1, b2 = data[base], data[base + 1], data[base + 2]
        values.append(((b1 << 8) & 0xF00) | b0)
        values.append((b2 << 4) | (b1 >> 4))
    return values


@dataclass
class AxisCalibration:
    center: int
    minimum: int
    maximum: int

    def normalize(self, raw: int) -> float:
        """Map a raw axis reading to -1.0 .. 1.0 around the calibrated centre."""
        if raw >= self.center:
            span = self.maximum - self.center
            if span <= 0:
                return 0.0
            value = (raw - self.center) / span
        else:
            span = self.center - self.minimum
            if span <= 0:
                return 0.0
            value = (raw - self.center) / span
        if value > 1.0:
            return 1.0
        if value < -1.0:
            return -1.0
        return value


@dataclass
class StickCalibration:
    x: AxisCalibration
    y: AxisCalibration

    @staticmethod
    def default() -> "StickCalibration":
        # 12-bit axes, nominal centre.
        return StickCalibration(
            x=AxisCalibration(2048, 2048 - 1800, 2048 + 1800),
            y=AxisCalibration(2048, 2048 - 1800, 2048 + 1800),
        )

    @staticmethod
    def parse(data: bytes, offset: int, left: bool) -> "StickCalibration":
        values = _pack12(data, offset)
        if left:
            x_max_above, y_max_above, x_center, y_center, x_min_below, y_min_below = values
        else:
            x_center, y_center, x_min_below, y_min_below, x_max_above, y_max_above = values
        return StickCalibration(
            x=AxisCalibration(x_center, x_center - x_min_below, x_center + x_max_above),
            y=AxisCalibration(y_center, y_center - y_min_below, y_center + y_max_above),
        )


@dataclass
class ImuCalibration:
    """Factory IMU calibration: converts raw int16 values to g and degrees/s.

    The SPI block holds, per axis, a raw origin and a sensitivity coefficient:

        acceleration_g = raw * 4 / (accel_coeff - accel_origin)
        rotation_dps   = (raw - gyro_origin) * 936 / (gyro_coeff - gyro_origin)

    with the fallback 1 g = 4096 counts and 1 deg/s = 14.2842 counts.  The
    coefficients always describe the +-8 g / +-2000 dps sensitivity, whatever
    the configured range is.
    """

    accel_origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    accel_coeff: tuple[float, float, float] = (
        float(DEFAULT_ACCEL_COEFF),) * 3
    gyro_origin: tuple[int, int, int] = (0, 0, 0)
    gyro_coeff: tuple[int, int, int] = (DEFAULT_GYRO_COEFF,) * 3
    source: str = "default"

    def __post_init__(self) -> None:
        self._recompute()

    def _recompute(self) -> None:
        accel_scale = []
        for origin, coeff in zip(self.accel_origin, self.accel_coeff):
            denominator = coeff - origin
            accel_scale.append(4.0 / denominator if denominator else 1.0 / 4096)
        gyro_scale = []
        for origin, coeff in zip(self.gyro_origin, self.gyro_coeff):
            denominator = coeff - origin
            gyro_scale.append(936.0 / denominator if denominator else 1.0 / 14.2842)
        self.accel_scale = tuple(accel_scale)
        self.gyro_scale = tuple(gyro_scale)

    @staticmethod
    def default() -> "ImuCalibration":
        return ImuCalibration()

    @staticmethod
    def parse(data: bytes) -> "ImuCalibration":
        if len(data) < SPI_IMU_CALIBRATION_LEN:
            raise SwitchProError("short IMU calibration block")
        return ImuCalibration(
            accel_origin=tuple(float(v) for v in struct.unpack_from("<3h", data, 0)),
            accel_coeff=tuple(float(v) for v in struct.unpack_from("<3h", data, 6)),
            gyro_origin=struct.unpack_from("<3h", data, 12),
            gyro_coeff=struct.unpack_from("<3h", data, 18),
            source="spi",
        )

    def with_user_origins(self, accel_origin, gyro_origin) -> "ImuCalibration":
        """Apply user-calibration origins, keeping the factory coefficients.

        This mirrors what the controller itself (and SDL) does: user calibration
        re-zeroes the sensors, it does not change their sensitivity.
        """
        return ImuCalibration(
            accel_origin=tuple(float(v) for v in accel_origin),
            accel_coeff=self.accel_coeff,
            gyro_origin=tuple(int(v) for v in gyro_origin),
            gyro_coeff=self.gyro_coeff,
            source=f"{self.source}+user",
        )

    def to_g(self, raw: tuple[int, int, int]) -> tuple[float, float, float]:
        return tuple(raw[i] * self.accel_scale[i] for i in range(3))

    def to_dps(self, raw: tuple[int, int, int]) -> tuple[float, float, float]:
        return tuple(
            (raw[i] - self.gyro_origin[i]) * self.gyro_scale[i] for i in range(3)
        )


# --------------------------------------------------------------------------- #
# Device discovery
# --------------------------------------------------------------------------- #

def find_controller_interfaces() -> list[dict]:
    """All present HID interfaces that could be a Switch Pro Controller."""
    candidates = []
    for info in hidwin.enumerate_hid():
        if info.get("vid") != SWITCH_PRO_VID or info.get("pid") != SWITCH_PRO_PID:
            continue
        if (info.get("input_len") or 0) < 49:
            continue
        if not info.get("output_len"):
            continue
        candidates.append(info)

    def sort_key(info: dict):
        usage_page = info.get("usage_page")
        usage = info.get("usage")
        # The Pro Controller exposes several HID collections; only the one with
        # a 64-byte output report accepts the 0x80 init commands and the 0x01
        # subcommand reports over USB, so prefer it strongly.
        usb_command_collection = 0 if info.get("output_len") == 64 else 1
        gamepad = 0 if (usage_page == 0x01 and usage in (0x04, 0x05)) else 1
        bluetooth_like = 0 if (info.get("input_len") == 49) else 1
        return (usb_command_collection, gamepad, bluetooth_like)

    candidates.sort(key=sort_key)
    return candidates


def is_bluetooth_path(path: str) -> bool:
    lowered = path.lower()
    return "bthenum" in lowered or "bthledevice" in lowered or "{00001124" in lowered


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #

class SwitchProController:
    """Reads and initialises a Switch Pro Controller over USB or Bluetooth."""

    def __init__(self, path: str | None = None, info: dict | None = None, log=print):
        self._log = log
        self._path = path
        self._info = info
        self._candidates: list[dict] = []
        self._device: hidwin.HidDevice | None = None
        self.transport = "unknown"
        self.device_type = 0
        self.mac_address = ""
        self.firmware = (0, 0)
        self.imu_calibration = ImuCalibration.default()
        self.left_stick_calibration = StickCalibration.default()
        self.right_stick_calibration = StickCalibration.default()
        self._command_number = 0
        self._last_write = 0.0
        self._saw_state_report = False

    # -- properties ------------------------------------------------------- #

    @property
    def device_type_name(self) -> str:
        return DEVICE_TYPE_NAMES.get(self.device_type, f"type {self.device_type}")

    @property
    def is_open(self) -> bool:
        return self._device is not None and self._device.is_open

    # -- lifecycle -------------------------------------------------------- #

    def open(self) -> "SwitchProController":
        if self._path is None:
            candidates = find_controller_interfaces()
            if not candidates:
                raise SwitchProError(
                    "no Nintendo Switch Pro Controller found; connect it over USB or "
                    "pair it over Bluetooth, then try again"
                )
            self._candidates = candidates[1:]
            self._info = candidates[0]
            self._path = self._info["path"]
            if len(candidates) > 1:
                self._log(
                    f"note: {len(candidates)} matching HID interfaces, starting with "
                    f"in={self._info.get('input_len')} out={self._info.get('output_len')}"
                )
        device = hidwin.HidDevice(self._path, self._info)
        self._device = device.open()
        return self

    def _reopen(self, info: dict) -> None:
        """Switch to another HID collection of the same controller."""
        if self._device is not None:
            self._device.close()
        self._info = info
        self._path = info["path"]
        device = hidwin.HidDevice(self._path, self._info)
        self._device = device.open()
        self._saw_state_report = False
        self._last_write = 0.0

    def _probe_usb_interfaces(self) -> bytes | None:
        """Try the other HID collections for the one that answers 0x80 0x01."""
        for info in list(self._candidates):
            try:
                self._reopen(info)
            except OSError:
                continue
            acknowledged, reply = self._proprietary(PROP_STATUS, timeout_s=0.2)
            if acknowledged:
                self._log(f"using HID interface in={info.get('input_len')} "
                          f"out={info.get('output_len')}")
                self._candidates = [c for c in self._candidates if c["path"] != info["path"]]
                return reply
        return None

    def close(self) -> None:
        if self._device is None:
            return
        # Leave the controller the way we found it: hand the USB link back to
        # Bluetooth, or restore simple input mode so other software sees it.
        try:
            if self.transport == "usb":
                self._proprietary(PROP_CLEAR_USB, wait_for_reply=False)
            elif self.transport == "bluetooth":
                self._subcommand(SUB_SET_INPUT_REPORT_MODE,
                                 bytes((INPUT_REPORT_SIMPLE,)),
                                 timeout_s=0.15, attempts=1)
        except (SwitchProError, OSError):
            pass
        self._device.close()
        self._device = None

    def __enter__(self) -> "SwitchProController":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low level I/O ---------------------------------------------------- #

    def _write(self, packet: bytes) -> None:
        """Write one output report, padded to the transport's packet size."""
        if self._device is None:
            raise SwitchProError("controller is not open")
        size = BLUETOOTH_PACKET_LEN if self.transport == "bluetooth" else USB_PACKET_LEN
        caps_size = self._device.output_len
        if caps_size and caps_size >= len(packet):
            size = caps_size
        if len(packet) < size:
            packet = packet + bytes(size - len(packet))
        try:
            self._device.write(packet)
        except OSError:
            # Some USB stacks only accept the control-transfer path.
            if not self._device.set_output_report(packet):
                raise
        self._last_write = time.monotonic()

    def _next_command_number(self) -> int:
        number = self._command_number
        self._command_number = (self._command_number + 1) & 0x0F
        return number

    def _pace(self) -> None:
        """Keep subcommands apart: the controller drops the link if they are not.

        Linux hid-nintendo.c limits subcommands to one per 20 ms over USB and one
        per 60 ms over Bluetooth, and waits for an input report in between.
        """
        interval = (SUBCOMMAND_INTERVAL_BLUETOOTH_S if self.transport == "bluetooth"
                    else SUBCOMMAND_INTERVAL_USB_S)
        elapsed = time.monotonic() - self._last_write
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def _prepare_write(self) -> None:
        self._pace()
        if self._saw_state_report:
            # Cheap liveness check: state reports stream continuously once the
            # controller is in 0x30 mode.
            self._wait_for(
                lambda r: True if r[0] == INPUT_REPORT_FULL else None, 0.25
            )

    def _read_raw(self, timeout_ms: int) -> bytes | None:
        if self._device is None:
            raise SwitchProError("controller is not open")
        try:
            report = self._device.read(timeout_ms=timeout_ms)
        except OSError as exc:
            raise DeviceGoneError(str(exc)) from exc
        if report and report[0] == INPUT_REPORT_FULL:
            self._saw_state_report = True
        return report

    def _wait_for(self, predicate, timeout_s: float = 0.2):
        """Read reports until ``predicate(report)`` is truthy or time runs out."""
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            report = self._read_raw(max(1, int(remaining * 1000)))
            if report:
                result = predicate(report)
                if result is not None and result is not False:
                    return result

    # -- proprietary (USB) commands --------------------------------------- #

    def _proprietary(self, command: int, wait_for_reply: bool = True,
                     timeout_s: float = 0.25) -> tuple[bool, bytes | None]:
        """Send a USB-only proprietary command; return ``(acknowledged, reply)``."""
        packet = bytes((OUTPUT_REPORT_PROPRIETARY, command))
        last_reply = None
        for _ in range(3):
            self._prepare_write()
            self._write(packet)
            if not wait_for_reply:
                return True, None
            reply = self._wait_for(
                lambda r: r if (r[0] == INPUT_REPORT_PROPRIETARY_ACK and len(r) > 1 and r[1] == command) else None,
                timeout_s,
            )
            if reply:
                return True, reply
            last_reply = reply
        return False, last_reply

    # -- rumble ----------------------------------------------------------- #

    def set_rumble_sides(self, left: float, right: float) -> None:
        """Drive the two actuators independently; levels are 0..1.

        Uses the rumble-only output report (0x10), so no subcommand handshake and
        no pacing is involved: it can be sent at whatever cadence the caller wants.
        """
        self._write(bytes((OUTPUT_REPORT_RUMBLE,))
                    + rumble_payload(float(left), float(right)))

    def set_rumble_payload(self, left: bytes, right: bytes) -> None:
        """Send explicit four-byte-per-side payloads (band-level control)."""
        self._write(bytes((OUTPUT_REPORT_RUMBLE,)) + bytes(left[:4]) + bytes(right[:4]))

    def send_subcommand(self, subcommand: int, data: bytes = b"",
                        timeout_s: float = 0.3) -> bytes | None:
        """Public wrapper: run one subcommand and return its reply payload.

        Handy for clearing state (vibration enable, input mode) outside the
        calibration path, and for diagnostics.
        """
        return self._subcommand(subcommand, data, timeout_s=timeout_s)

    def set_rumble(self, level: float) -> None:
        self.set_rumble_sides(level, level)

    def stop_rumble(self) -> None:
        """Send the payload that really is silence on every pad seen so far.

        The documented neutral keeps some clones buzzing (they read the bytes as raw
        drive, where 0x40 means "a quarter on"), so this sends all zeros.
        """
        self._write(bytes((OUTPUT_REPORT_RUMBLE,)) + RUMBLE_SILENCE)

    # -- subcommands ------------------------------------------------------ #

    def _build_subcommand(self, subcommand: int, data: bytes = b"") -> bytes:
        # Both transports use the same packet layout; only the padding differs
        # (USB pads to 64 bytes, Bluetooth to 49) and both carry the rolling
        # 4-bit packet counter.
        header = bytes((OUTPUT_REPORT_RUMBLE_AND_SUBCMD, self._next_command_number()))
        return header + NEUTRAL_RUMBLE + bytes((subcommand,)) + data

    def _subcommand(self, subcommand: int, data: bytes = b"", timeout_s: float = 0.3,
                    attempts: int = 4) -> bytes | None:
        """Send a subcommand and return its reply payload (without the 5-byte header)."""
        packet = self._build_subcommand(subcommand, data)
        for _ in range(attempts):
            self._prepare_write()
            self._write(packet)
            reply = self._wait_for(
                lambda report: self._match_subcommand_reply(report, subcommand, data),
                timeout_s,
            )
            if reply is not None:
                return reply
        return None

    @staticmethod
    def _match_subcommand_reply(report: bytes, subcommand: int, request: bytes):
        parsed = subcommand_reply(report)
        if parsed is None:
            return None
        ack, reply_id, payload = parsed
        if reply_id != subcommand or not (ack & 0x80):
            return None
        # For SPI reads make sure the reply belongs to this request.
        if subcommand == SUB_SPI_FLASH_READ and not payload.startswith(request[:5]):
            return None
        return payload

    # -- calibration / info ----------------------------------------------- #

    def read_spi(self, address: int, length: int) -> bytes:
        """Read ``length`` bytes of SPI flash starting at ``address``."""
        out = bytearray()
        while length > 0:
            chunk = min(length, SPI_READ_CHUNK)
            request = struct.pack("<IB", address + len(out), chunk)
            payload = self._subcommand(SUB_SPI_FLASH_READ, request)
            if payload is None:
                raise SwitchProError(
                    f"SPI read at 0x{address + len(out):06X} failed (no reply)"
                )
            # payload = 4-byte address + 1-byte length + data
            out += payload[5:5 + chunk]
            if len(out) < chunk:
                raise SwitchProError("short SPI read reply")
            length -= chunk
        return bytes(out)

    def request_device_info(self) -> bool:
        payload = self._subcommand(SUB_REQUEST_DEVICE_INFO)
        if payload is None or len(payload) < 12:
            return False
        self.firmware = (payload[0], payload[1])
        self.device_type = payload[2]
        # MAC is stored big-endian in the reply.
        self.mac_address = ":".join(f"{b:02X}" for b in payload[4:10])
        return True

    def _try_spi(self, address: int, length: int) -> bytes | None:
        try:
            return self.read_spi(address, length)
        except SwitchProError:
            return None

    def read_calibration(self) -> None:
        self._read_imu_calibration()
        self._read_stick_calibration()

    def _read_imu_calibration(self) -> None:
        factory = self._try_spi(SPI_IMU_CALIBRATION, SPI_IMU_CALIBRATION_LEN)
        if factory is None:
            self._log("warning: IMU calibration unavailable; using default sensitivity")
            self.imu_calibration = ImuCalibration.default()
            return
        try:
            calibration = ImuCalibration.parse(factory)
        except SwitchProError as exc:
            self._log(f"warning: bad IMU calibration ({exc}); using defaults")
            self.imu_calibration = ImuCalibration.default()
            return

        # User calibration (present once the controller has been calibrated on a
        # Switch) overrides the origins while keeping factory sensitivity.
        user = self._try_spi(SPI_IMU_USER_CALIBRATION, SPI_IMU_USER_CALIBRATION_LEN)
        if user is not None and user[:2] == USER_CALIBRATION_MAGIC and len(user) >= 20:
            accel_origin = struct.unpack_from("<3h", user, 2)
            gyro_origin = struct.unpack_from("<3h", user, 14)
            calibration = calibration.with_user_origins(accel_origin, gyro_origin)
        self.imu_calibration = calibration

    def _read_stick_calibration(self) -> None:
        left = right = None
        user = self._try_spi(SPI_STICK_USER_CALIBRATION, SPI_STICK_USER_CALIBRATION_LEN)
        if user is not None and len(user) >= SPI_STICK_USER_CALIBRATION_LEN:
            if user[0:2] == USER_CALIBRATION_MAGIC:
                left = user[2:11]
            if user[11:13] == USER_CALIBRATION_MAGIC:
                right = user[13:22]

        if left is None or right is None:
            factory = self._try_spi(SPI_STICK_CALIBRATION, SPI_STICK_CALIBRATION_LEN)
            if factory is not None and len(factory) >= SPI_STICK_CALIBRATION_LEN:
                if left is None:
                    left = factory[0:9]
                if right is None:
                    right = factory[9:18]

        if left is None or right is None:
            self._log("warning: stick calibration unavailable; using nominal centres")
            self.left_stick_calibration = StickCalibration.default()
            self.right_stick_calibration = StickCalibration.default()
            return
        self.left_stick_calibration = StickCalibration.parse(left, 0, left=True)
        self.right_stick_calibration = StickCalibration.parse(right, 0, left=False)

    # -- initialisation --------------------------------------------------- #

    def initialize(self) -> None:
        """Run the full handshake so the controller streams 0x30 reports."""
        if self._info is None and self._device is not None:
            self._info = self._device.info

        path_hint = is_bluetooth_path(self._path or "")
        status_reply = None
        if not path_hint:
            # Assume USB and run the proprietary handshake; it does not exist over
            # Bluetooth, so a missing reply means we are on Bluetooth after all.
            ok, status_reply = self._proprietary(PROP_STATUS)
            if not ok and self._candidates:
                status_reply = self._probe_usb_interfaces()
                ok = status_reply is not None
            self.transport = "usb" if ok else "bluetooth"
        else:
            self.transport = "bluetooth"

        if self.transport == "usb":
            # Order matters: handshake, probe higher speed, handshake again, then
            # keep the controller from dropping back to Bluetooth.
            self._proprietary(PROP_HANDSHAKE)
            self._proprietary(PROP_HIGH_SPEED)
            self._proprietary(PROP_HANDSHAKE)
            self._proprietary(PROP_FORCE_USB, wait_for_reply=False)
            self.device_type = DEVICE_TYPE_PRO_CONTROLLER
            self._parse_proprietary_status(status_reply)
        else:
            if not self.request_device_info():
                self._log("warning: controller did not answer the device-info request")

        if not self._subcommand(SUB_SET_INPUT_REPORT_MODE, bytes((INPUT_REPORT_FULL,))):
            self._log("warning: controller did not acknowledge the input-report mode change")

        self._subcommand(SUB_ENABLE_IMU, b"\x01")
        self._subcommand(SUB_ENABLE_VIBRATION, b"\x01")
        # The firmware default IMU configuration is already the one the
        # calibration block describes (+-2000 dps, +-8 g), so no 0x41 needed.
        # Light player LED 1 so it is obvious the controller is in use.
        self._subcommand(SUB_SET_PLAYER_LIGHTS, b"\x01")

        self.read_calibration()
        # Drop any reports queued while we were still handshaking.
        self.drain()

    def _parse_proprietary_status(self, report: bytes | None) -> None:
        """0x81 0x01 0x00 <device type> <MAC, reversed> from the USB status command."""
        if not report or len(report) < 10:
            return
        self.device_type = report[3]
        self.mac_address = ":".join(f"{b:02X}" for b in reversed(report[4:10]))

    # -- reading ---------------------------------------------------------- #

    def read_state(self, timeout_ms: int = 500) -> ControllerState | None:
        """Read the next 0x30 state report, or ``None`` on timeout."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            report = self._read_raw(max(1, int(remaining * 1000)))
            if not report:
                continue
            if report[0] == INPUT_REPORT_FULL:
                return parse_state(report)
        # unreachable

    def drain(self, max_reports: int = 32) -> None:
        """Discard reports already queued on the device."""
        if self._device is not None:
            self._device.drain(max_reports=max_reports, timeout_ms=0)

    # -- convenience ------------------------------------------------------ #

    def describe(self) -> str:
        parts = [
            f"{self.device_type_name} over {self.transport}",
            f"firmware {self.firmware[0]}.{self.firmware[1]}" if self.firmware != (0, 0) else "firmware ?",
        ]
        if self.mac_address:
            parts.append(f"MAC {self.mac_address}")
        parts.append(f"IMU calibration: {self.imu_calibration.source}")
        return ", ".join(parts)

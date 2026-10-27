"""Read a physical DualShock 4 over USB or Bluetooth.

Why this exists: the DualShock 4 sends a different input report depending on the
transport.  Wired it uses report id ``0x01`` with the 63-byte state payload
right after the id; over Bluetooth it uses report id ``0x11``, which prepends a
two-byte header before (almost) the same payload.  Games that only understand the
wired format -- Genshin Impact among them -- therefore read no motion data from a
Bluetooth pad, even though the gyroscope is reporting fine.

Relaying the pad onto a virtual *wired* DualShock 4 fixes that, because the
virtual pad always emits the wired ``0x01`` report.

Payload layout used here (same as a wired DS4, offsets relative to the state):

    0..3    left stick X/Y, right stick X/Y
    4..5    buttons (16 bit: hat in bits 0-3, then square/cross/circle/triangle,
            L1, R1, L2, R2, share, options, L3, R3)
    6       special (bit0 PS/home, bit1 touchpad click)
    7       left trigger
    8       right trigger
    9..10   timestamp, 5.33 us units
    11      reserved
    12..17  gyro X/Y/Z (int16 little endian)
    18..23  accelerometer X/Y/Z (int16 little endian)
    24..28  reserved
    29      battery level (low nibble) + charging bit 4
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass

import hidwin

DS4_VID = 0x054C
DS4_PIDS = (0x05C4, 0x09CC)     # DS4 v1 / v2

REPORT_WIRED = 0x01
REPORT_BLUETOOTH = 0x11

#: Bytes of state payload we need (through the accelerometer + battery).
STATE_BYTES_USED = 30

#: Candidate state offsets.  Wired: right after the report id.  Bluetooth: two
#: header bytes follow the id.  ``initialize()`` confirms the choice against
#: live data instead of trusting a constant.
CANDIDATE_OFFSETS = {
    REPORT_WIRED: (1, 3),
    REPORT_BLUETOOTH: (3, 2),
}

DEFAULT_OFFSET = {REPORT_WIRED: 1, REPORT_BLUETOOTH: 3}

#: |acceleration| is ~1 g whatever the orientation, so it identifies the layout.
ACCEL_MIN, ACCEL_MAX = 4000, 14000

# --------------------------------------------------------------------------- #
# Bluetooth "extended report" request
#
# Over Bluetooth a DualShock 4 starts out sending a *minimal* report: 0x01, ten
# bytes, sticks and buttons only -- no gyroscope, no accelerometer, a frozen
# timestamp.  Games that ask for motion therefore see none, which is what
# "Bluetooth gyro is not recognised" actually is.  The pad switches to the full
# 0x11 report with motion once the host sends the Bluetooth output report with
# the hardware-control byte asking for HID data plus a CRC (Linux
# hid-playstation.c does exactly this, and DS4Windows/SDL do the equivalent).
# --------------------------------------------------------------------------- #

OUTPUT_REPORT_BT = 0x11
OUTPUT_REPORT_BT_SIZE = 78
OUTPUT_REPORT_USB = 0x05
OUTPUT_REPORT_USB_SIZE = 32

HWCTL_POLL_MASK = 0x3F
HWCTL_CRC32 = 0x40
HWCTL_HID = 0x80
OUTPUT_CRC32_SEED = 0xA2
DEFAULT_POLL_INTERVAL_MS = 4

VALID_FLAG0_MOTOR = 0x01
VALID_FLAG0_LED = 0x02

#: Default lightbar colour (blue), same as the Linux driver's first player colour.
DEFAULT_LIGHTBAR = (0x00, 0x00, 0x40)


def output_crc32(report: bytes) -> int:
    """CRC-32 over the report body, seeded like real PlayStation hardware."""
    body = bytes((OUTPUT_CRC32_SEED,)) + report[:-4]
    return zlib.crc32(body) & 0xFFFFFFFF


def build_extended_mode_report(poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
                               lightbar: tuple[int, int, int] = DEFAULT_LIGHTBAR) -> bytes:
    """Build the Bluetooth output report that requests full 0x11 reports.

    Layout from the Linux driver's ``dualshock4_output_report_bt``: report id,
    hardware control, audio control, then a ten-byte common section, 61 reserved
    bytes and a CRC32 in the last four.
    """
    report = bytearray(OUTPUT_REPORT_BT_SIZE)
    report[0] = OUTPUT_REPORT_BT
    report[1] = HWCTL_HID | HWCTL_CRC32 | (poll_interval_ms & HWCTL_POLL_MASK)
    report[2] = 0                                   # audio control
    report[3] = VALID_FLAG0_LED                     # valid_flag0: lightbar update
    report[4] = 0                                   # valid_flag1
    report[8], report[9], report[10] = lightbar
    struct.pack_into("<I", report, OUTPUT_REPORT_BT_SIZE - 4, output_crc32(bytes(report)))
    return bytes(report)


class DS4Error(RuntimeError):
    pass


class DS4GoneError(DS4Error):
    """The pad was unplugged / left Bluetooth range mid-session."""


@dataclass
class DS4State:
    report_id: int
    sticks: tuple[int, int, int, int]      # LX, LY, RX, RY
    buttons: int
    special: int
    triggers: tuple[int, int]              # L2, R2
    timestamp: int
    gyro: tuple[int, int, int]
    accel: tuple[int, int, int]
    battery: int
    wired: bool
    #: False for the minimal Bluetooth report (0x01): sticks/buttons are real but
    #: the pad sends no motion data at all in that mode.
    motion_valid: bool = True
    raw: bytes = b""

    # -- convenience ------------------------------------------------------ #

    @property
    def battery_level(self) -> int:
        return self.battery & 0x0F

    @property
    def charging(self) -> bool:
        return bool(self.battery & 0x10)

    @property
    def dpad(self) -> int:
        return self.buttons & 0x0F

    @property
    def pressed_names(self) -> list[str]:
        names = []
        if self.dpad != 8:
            names.append(f"dpad:{self.dpad}")
        for mask, name in (
            (1 << 4, "Square"), (1 << 5, "Cross"), (1 << 6, "Circle"),
            (1 << 7, "Triangle"), (1 << 8, "L1"), (1 << 9, "R1"),
            (1 << 10, "L2"), (1 << 11, "R2"), (1 << 12, "Share"),
            (1 << 13, "Options"), (1 << 14, "L3"), (1 << 15, "R3"),
        ):
            if self.buttons & mask:
                names.append(name)
        if self.special & 0x01:
            names.append("PS")
        if self.special & 0x02:
            names.append("Touchpad")
        return names


def state_offset_for(report_id: int) -> int:
    return DEFAULT_OFFSET.get(report_id, 1)


def parse_report(report: bytes, offset: int | None = None,
                 bluetooth_minimal: bool = False) -> DS4State | None:
    """Parse one input report; ``None`` when it is not a DS4 state report."""
    if not report:
        return None
    report_id = report[0]
    if report_id not in (REPORT_WIRED, REPORT_BLUETOOTH):
        return None
    if bluetooth_minimal and report_id == REPORT_WIRED:
        # Minimal Bluetooth report: the first nine bytes of the full state, i.e.
        # sticks and buttons only.  Anything past that is padding, not motion.
        if len(report) < 1 + 9:
            return None
        state = report[1:10]
        return DS4State(
            report_id=report_id,
            sticks=(state[0], state[1], state[2], state[3]),
            buttons=struct.unpack_from("<H", state, 4)[0],
            special=state[6],
            triggers=(state[7], state[8]),
            timestamp=0,
            gyro=(0, 0, 0),
            accel=(0, 0, 0),
            battery=0,
            wired=False,
            motion_valid=False,
            raw=bytes(state),
        )
    if offset is None:
        offset = state_offset_for(report_id)
    if len(report) < offset + STATE_BYTES_USED:
        return None

    state = report[offset:]
    gyro = struct.unpack_from("<3h", state, 12)
    accel = struct.unpack_from("<3h", state, 18)
    return DS4State(
        report_id=report_id,
        sticks=(state[0], state[1], state[2], state[3]),
        buttons=struct.unpack_from("<H", state, 4)[0],
        special=state[6],
        triggers=(state[7], state[8]),
        timestamp=struct.unpack_from("<H", state, 9)[0],
        gyro=gyro,
        accel=accel,
        battery=state[29],
        wired=(report_id == REPORT_WIRED),
        raw=bytes(state),
    )


def _plausibility(state: bytes) -> int:
    """Score how much a candidate payload looks like a real DS4 state."""
    score = 0
    magnitude = sum(value * value for value in struct.unpack_from("<3h", state, 18)) ** 0.5
    if ACCEL_MIN <= magnitude <= ACCEL_MAX:
        score += 4                                    # gravity, any orientation
    if max(abs(value) for value in struct.unpack_from("<3h", state, 12)) < 25000:
        score += 1                                    # gyro in a sane range
    if all(0x30 <= value <= 0xD0 for value in state[0:4]):
        score += 1                                    # sticks near centre
    if state[7] < 0x40 and state[8] < 0x40:
        score += 1                                    # triggers released
    if (state[4] & 0x0F) <= 8:
        score += 1                                    # valid hat value
    return score


def detect_offsets(reports: list[bytes]) -> tuple[dict[int, int], dict[str, float]]:
    """Per report id, pick the state offset that best explains the data.

    Detection is deliberately per report id: a pad that is asleep at start-up
    answers with a cached ``0x01`` report, and that must not decide how a later
    live ``0x11`` report is parsed.
    """
    offsets: dict[int, int] = {}
    scores: dict[str, float] = {}
    for report_id in (REPORT_WIRED, REPORT_BLUETOOTH):
        best_offset = None
        best_score = None
        for offset in CANDIDATE_OFFSETS[report_id]:
            total = 0
            seen = 0
            for report in reports:
                if report[0] != report_id or len(report) < offset + STATE_BYTES_USED:
                    continue
                total += _plausibility(report[offset:])
                seen += 1
            if not seen:
                continue
            score = total / seen
            scores[f"0x{report_id:02X}@{offset}"] = round(score, 1)
            if best_score is None or score > best_score:
                best_score, best_offset = score, offset
        if best_offset is not None:
            offsets[report_id] = best_offset
    return offsets, scores


# --------------------------------------------------------------------------- #
# Device
# --------------------------------------------------------------------------- #

def find_ds4_interfaces(include_emulated: bool = False) -> list[dict]:
    """HID interfaces that might be a physical DualShock 4."""
    candidates = []
    for info in hidwin.enumerate_hid():
        if info.get("vid") != DS4_VID or info.get("pid") not in DS4_PIDS:
            continue
        if not include_emulated and hidwin.is_emulated(info):
            continue                                  # a ViGEm target, not hardware
        candidates.append(info)

    def sort_key(info: dict):
        transport = hidwin.transport_of(info)
        # A wired pad reports 64 bytes; a Bluetooth node can advertise a large
        # aggregate size, so rank by transport first and size second.
        return (0 if transport == "usb" else 1,
                0 if (info.get("input_len") or 0) == 64 else 1)

    candidates.sort(key=sort_key)
    return candidates


class DualShock4:
    """Reads a physical DualShock 4 over USB or Bluetooth."""

    def __init__(self, path: str | None = None, info: dict | None = None, log=print):
        self._log = log
        self._path = path
        self._info = info
        self._device: hidwin.HidDevice | None = None
        #: report id -> state offset; starts at the documented defaults and is
        #: refined per report id from live data.
        self._offsets: dict[int, int] = dict(DEFAULT_OFFSET)
        self.transport = "unknown"
        self.offset_scores: dict[str, float] = {}
        self.report_rate = 0.0
        self._rate_samples: list[float] = []
        self.offset_confirmed = False
        self._last_report_bytes: bytes | None = None
        self.static_count = 0
        self._static_warned = False
        #: "extended" (0x11, has motion), "minimal" (Bluetooth 0x01, no motion)
        #: or "unknown" until reports arrive.
        self.motion_mode = "unknown"
        self._mode_requests = 0
        self._last_mode_request = 0.0

    # -- properties ------------------------------------------------------- #

    @property
    def is_open(self) -> bool:
        return self._device is not None and self._device.is_open

    @property
    def device_name(self) -> str:
        pid = (self._info or {}).get("pid")
        return f"DualShock 4 {'v1' if pid == 0x05C4 else 'v2'}" if pid else "DualShock 4"

    def offset_for(self, report_id: int) -> int:
        return self._offsets.get(report_id, state_offset_for(report_id))

    def describe(self) -> str:
        offsets = ", ".join(f"0x{report_id:02X}->{offset}"
                            for report_id, offset in sorted(self._offsets.items()))
        return (f"{self.device_name} over {self.transport}, state offset {offsets}, "
                f"motion {self.motion_mode}, ~{self.report_rate:.0f} reports/s")

    # -- lifecycle -------------------------------------------------------- #

    def open(self) -> "DualShock4":
        if self._path is not None:
            device = hidwin.HidDevice(self._path, self._info) if self._info \
                else hidwin.HidDevice(self._path)
            self._device = device.open()
            self._info = device.info
            self.transport = hidwin.transport_of(self._info)
            return self

        candidates = find_ds4_interfaces()
        if not candidates:
            raise DS4Error(
                "no physical DualShock 4 found; connect it with the USB cable or "
                "pair it over Bluetooth"
            )
        errors = []
        for info in candidates:
            try:
                device = hidwin.HidDevice(info["path"], info).open()
            except OSError as exc:
                errors.append(f"{info['path'][:60]}...: {exc}")
                continue
            device.drain(max_reports=16, timeout_ms=0)
            if self._probe(device):
                self._device = device
                self._info = info
                self.transport = hidwin.transport_of(info)
                self._log(f"using HID interface in={info.get('input_len')} "
                          f"out={info.get('output_len')} ({self.transport})")
                return self
            device.close()
        detail = ("; ".join(errors) if errors
                  else "found the pad but no state reports arrived")
        raise DS4Error(f"could not read the DualShock 4 ({detail})")

    def _probe(self, device: hidwin.HidDevice, attempts: int = 8) -> bool:
        """Does this interface actually deliver DS4 state reports?"""
        for _ in range(attempts):
            report = device.read(timeout_ms=150)
            if report and report[0] in (REPORT_WIRED, REPORT_BLUETOOTH) \
                    and parse_report(report, state_offset_for(report[0])) is not None:
                return True
        return False

    def close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None

    def __enter__(self) -> "DualShock4":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- Bluetooth report mode -------------------------------------------- #

    def write_output(self, data: bytes) -> int:
        """Send an output report the way this transport actually accepts it.

        Bluetooth needs the write to go over the **control channel**
        (``HidD_SetOutputReport``) and padded to the report size Windows declares
        for the node.  ``WriteFile`` on the interrupt OUT endpoint is accepted and
        even reports the full number of bytes written, but the pad never sees the
        report: measured on real hardware, a 0x11 mode request sent that way left
        the pad in the motionless 0x01 report forever, while the same bytes via
        ``HidD_SetOutputReport`` switched it on the spot.

        Wired pads take the plain interrupt-OUT write, as before.
        """
        if self._device is None:
            raise DS4Error("pad is not open")
        expected = (self._info or {}).get("output_len") or len(data)
        if len(data) < expected:
            data = data + bytes(expected - len(data))
        if self.transport == "bluetooth":
            if self._device.set_output_report(data):
                return len(data)
            self._log("warning: HidD_SetOutputReport was refused; falling back to "
                      "WriteFile, which a Bluetooth pad usually ignores")
        return self._device.write(data)

    def request_extended_mode(self) -> bool:
        """Ask a Bluetooth pad to switch to the full 0x11 report (with motion).

        Returns True when the request was written.  Whether the pad obeys is
        checked by :meth:`initialize` / :meth:`read_state`.
        """
        if self.transport != "bluetooth" or self._device is None:
            return False
        report = build_extended_mode_report()
        try:
            written = self.write_output(report)
        except OSError as exc:
            self._log(f"warning: could not send the Bluetooth mode request: {exc}")
            return False
        self._mode_requests += 1
        self._last_mode_request = time.monotonic()
        self._log(f"asked the pad for full (0x11) reports: wrote {written} bytes, "
                  f"hw_control=0x{report[1]:02X}, crc=0x{struct.unpack_from('<I', report, 74)[0]:08X}")
        return True

    def ensure_motion(self, force: bool = False) -> str | None:
        """Re-request the extended report if the pad fell back to minimal.

        A Bluetooth pad can drop back to the motionless minimal report after a
        reconnect, a sleep/wake cycle, or a filter driver re-opening the device.
        The symptom is confusing: buttons and sticks keep working, motion turns
        to zeros -- because the pad genuinely stops sending any.
        """
        if self.transport != "bluetooth":
            return None
        stalled = self.motion_mode == "minimal" or self.static_count >= 60
        if not force and not stalled:
            return None
        if time.monotonic() - self._last_mode_request < 2.0:
            return None
        if not self.request_extended_mode():
            return None
        return ("the pad had fallen back to the motionless minimal report; asked it "
                "again for the full (0x11) report")

    # -- initialisation --------------------------------------------------- #

    def initialize(self, samples: int = 12) -> None:
        """Confirm the state offset and the report mode, using live reports."""
        if self.transport == "bluetooth":
            # Without this the pad stays in its minimal report and never sends
            # motion at all -- the usual reason Bluetooth gyro "does not work".
            self.request_extended_mode()
            time.sleep(0.30)              # the pad needs a moment to switch

        collected: list[bytes] = []
        deadline = time.monotonic() + 1.5
        while len(collected) < samples and time.monotonic() < deadline:
            report = self._read_raw(200)
            if report and report[0] in (REPORT_WIRED, REPORT_BLUETOOTH):
                collected.append(report)
        if collected:
            found, scores = detect_offsets(collected)
            self.offset_scores = scores
            self.offset_confirmed = True
            for report_id, offset in found.items():
                self._offsets[report_id] = offset
            self._note_mode(collected)
            summary = ", ".join(f"0x{report_id:02X}->{offset}"
                                for report_id, offset in sorted(self._offsets.items()))
            self._log(f"state offsets: {summary} (scores {scores})")
        else:
            self._log("warning: no input reports while initialising; using the "
                      "default state offsets")

        if self.transport == "bluetooth" and self.motion_mode != "extended":
            # One more try with a longer look, then report the situation clearly.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and self.motion_mode != "extended":
                report = self._read_raw(200)
                if report and report[0] in (REPORT_WIRED, REPORT_BLUETOOTH):
                    self._note_mode([report])
            if self.motion_mode == "minimal":
                self._log("WARNING: the pad is in the minimal Bluetooth report mode: "
                          "it sends sticks and buttons but NO gyroscope/accelerometer "
                          "data. Games therefore see no Bluetooth motion. It was asked "
                          "to switch modes; if it refuses, connect it by USB or use a "
                          "different pad.")
            elif self.motion_mode == "unknown":
                self._log("warning: no input reports arrived, so the report mode is "
                          "unknown")

    def _note_mode(self, reports: list[bytes]) -> None:
        """Track whether the pad is sending motion or the minimal report."""
        for report in reports:
            if report[0] == REPORT_BLUETOOTH:
                self.motion_mode = "extended"
                return
        if self.transport == "bluetooth" and any(r[0] == REPORT_WIRED for r in reports):
            self.motion_mode = "minimal"
        elif self.transport != "bluetooth":
            self.motion_mode = "extended"

    # -- reading ---------------------------------------------------------- #

    def _read_raw(self, timeout_ms: int) -> bytes | None:
        if self._device is None:
            raise DS4Error("pad is not open")
        try:
            report = self._device.read(timeout_ms=timeout_ms)
        except OSError as exc:
            raise DS4GoneError(str(exc)) from exc
        if report:
            self._rate_samples.append(time.monotonic())
            if len(self._rate_samples) > 200:
                self._rate_samples.pop(0)
                span = self._rate_samples[-1] - self._rate_samples[0]
                if span > 0:
                    self.report_rate = (len(self._rate_samples) - 1) / span
            if self._last_report_bytes is not None and report == self._last_report_bytes:
                self.static_count += 1
            else:
                self.static_count = 0
            self._last_report_bytes = report
        return report

    def warn_if_static(self) -> str | None:
        """Explain a pad whose reports never change; returns the message once.

        A paired but idle Bluetooth pad keeps enumerating and answering reads
        with the same cached report, which otherwise looks like a working pad
        that simply never moves.
        """
        if self.static_count >= 60 and not self._static_warned:
            self._static_warned = True
            return ("warning: this pad keeps sending an identical report, so it is "
                    "not actually moving; press the PS button to wake it")
        return None

    def read_state(self, timeout_ms: int = 500) -> DS4State | None:
        """Read the next state report, or ``None`` on timeout."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            report = self._read_raw(max(1, int(remaining * 1000)))
            if not report:
                continue
            self._note_mode([report])
            state = parse_report(report, self.offset_for(report[0]),
                                 bluetooth_minimal=(self.transport == "bluetooth"))
            if state is not None:
                return state

    def drain(self, max_reports: int = 32) -> None:
        if self._device is not None:
            self._device.drain(max_reports=max_reports, timeout_ms=0)

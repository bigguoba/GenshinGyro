"""Visual verification and setup screen for the Switch Pro Controller.

Shows a live picture of the controller (buttons light up, sticks move, motion is
metered) and walks through every control one at a time, comparing what the
controller actually sent with what was expected.  A wrong binding is reported
immediately and can be saved as a correction that the bridge then uses.

    python gui.py                 # window (also: python main.py --gui)
    python gui.py --forward       # start with the virtual DS4 enabled

The window is deliberately plain Tk: everything it needs ships with Python.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import dualshock4
import haptics
import hidwin  # noqa: F401  (kept so the module can report device details if needed)
import mapping
import paths
import settings
import sources
import ui_theme
import switchpro
import verify_plan
import vigem
import viiper

BG = "#1e1f26"
PANEL = "#282a36"
TEXT = "#e8e8ef"
DIM = "#8b8fa3"
ACCENT = "#4ea1ff"
OK = ui_theme.ACCENT
BAD = "#ff6b6b"
WARN = ui_theme.DANGER
BODY = "#3a3d4d"
BODY_EDGE = "#565b73"
IDLE = "#4a4e63"

BUTTON_LABELS = {
    "A": "A", "B": "B", "X": "X", "Y": "Y",
    "L": "L", "R": "R", "ZL": "ZL", "ZR": "ZR",
    "L3": "L3", "R3": "R3", "Plus": "+", "Minus": "−",
    "Home": "HOME", "Capture": "▣",
}

DPAD_LABELS = {"Up": "▲", "Right": "▶", "Down": "▼", "Left": "◀"}

#: Confirm key for "go to the next step": the documented A button bit
#: (button byte 0, 0x08).  Deliberately the raw documented bit rather than the
#: (possibly corrected) mapping, so it always works even while a binding is
#: being investigated.  Space / Enter / the on-screen button work too.
CONFIRM_BYTE = 0
CONFIRM_BIT = 0x08
CONFIRM_NAME = "A"

#: Gyro sensitivity control: the virtual pad scales the rotation rate by this
#: factor (1.0 = exactly what the physical pad reports, 0 = motion off).
GAIN_MIN, GAIN_MAX, GAIN_STEP = 0.0, 5.0, 0.1

#: Rumble slider steps, keyed like ``settings.HAPTICS_KEYS``.  The ranges live in
#: ``settings.HAPTICS_RANGES`` so the window and the file agree on them.
HAPTICS_STEPS = {
    "gain": 0.05,
    "sensitivity": 1.0,
    "attack": 0.01,
    "release": 10.0,
    "dead_zone": 0.005,
    "gamma": 0.05,
    "split": 10.0,
}

#: One row per pair of sliders: (left key, left label, right key, right label).
#: ``None`` on the right means "this one gets the whole row".
HAPTICS_ROWS = (
    ("sensitivity", "灵敏度", "attack", "起振"),
    ("release", "拖尾", "dead_zone", "轻音过滤"),
    ("gamma", "响应曲线", "split", "低频/高频分界"),
)

#: One short line under each label.  Kept to a handful of characters so it fits
#: the label column; the full reasoning stays in the module docstrings.
HAPTICS_HINTS = {
    "gain": "整体力度",
    "sensitivity": "越大越容易震",
    "attack": "越大起振越快",
    "release": "掉强度有多慢",
    "dead_zone": "太轻就不震",
    "gamma": "前段迟钝还是猛",
    "split": "手感偏沉或偏脆",
    "dual": "两个马达分工",
}


def format_haptics(key: str, value: float) -> str:
    """How one rumble parameter reads in its value box."""
    if key == "gain":
        return f"{value * 100:.0f}%"
    if key == "sensitivity":
        return f"{value:.0f}"
    if key == "release":
        return f"{value:.0f}ms"                # already milliseconds
    if key == "dead_zone":
        return f"{value * 100:.1f}%"
    if key == "gamma" or key == "attack":
        return f"{value:.2f}"
    return f"{value:.0f}Hz"                    # split


def parse_haptics(key: str, text: str) -> float | None:
    """Read a value box back into the parameter; ``None`` when it is not a number."""
    cleaned = (text.strip().lower()
               .replace("ms", "").replace("hz", "")
               .replace("%", "").replace("％", ""))
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if key in ("gain", "dead_zone"):
        number /= 100.0                        # shown as a percentage
    return number

#: Names shown in the input-source box.  Short forms: the long descriptions did
#: not fit the narrower window and carried no information.
SOURCE_LABELS = {"auto": "自动", "switch": "Switch Pro", "ds4": "DualShock4"}
#: How the Share mapping reads in the note under the checkboxes.
SHARE_NOTE = "Share（DualShock 4）/ -（Switch Pro）"

#: The forwarding modes are mutually exclusive.  "genshin" is still reachable
#: from the command line (--mode genshin) but no longer has an on-screen button;
#: "ds5" forwards the same buttons/sticks/motion as "full", only onto a virtual
#: DualSense (through VIIPER) instead of a DualShock 4.
MODE_OFF, MODE_FULL, MODE_MOTION, MODE_GENSHIN, MODE_DS5 = (
    "off", "full", "motion", "genshin", "ds5")
#: Colour of an inactive flat button (also used as the slider grip colour).
BUTTON_OFF = "#3b3f52"

#: Product name.  The window title shows both forms; the English one is what the
#: repository and the exe will be called.
APP_NAME = "原神陀螺仪助手"
APP_NAME_EN = "GenshinGyro"


def make_dpi_aware() -> None:
    """Paint at real pixels instead of letting Windows stretch the window.

    A DPI-unaware process is given a 96-DPI device context and Windows then
    bitmap-scales the finished window on a 125%/150% display.  For a window with
    hand-drawn shapes that is doubly bad: every glyph goes soft and every
    anti-aliased edge is resampled again.  Declaring awareness lets Tk ask for
    the real DPI, so ``scale`` in :func:`main` can size the window and the
    controls to match and the pixels land 1:1 on the screen.

    System-DPI aware (not per-monitor): if the window is dragged to a monitor
    with a different scale, Windows resamples it once -- acceptable, and far
    safer for a fixed-layout window than re-laying-out on the fly.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:                                    # Windows 7 / 8 fallback
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def claim_taskbar_identity() -> None:
    """Give the process its own task-bar identity, so the icon is ours.

    Windows groups windows by AppUserModelID and takes the task-bar icon from
    that group.  Without an explicit one the window is grouped with whatever
    launched it -- python.exe when run from source, cmd.exe when started from
    one of the 启动-*.bat files -- and the task bar shows *that* icon.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "GenshinGyro.App")
    except Exception:
        pass


def apply_window_icon(root: tk.Tk) -> None:
    """Use the bundled .ico for the title bar and the task bar.

    Failure is not fatal and deliberately silent: a source checkout without
    ``assets/`` should still start, just with the default Tk icon.
    """
    icon = paths.resource_path(os.path.join("assets", "genshingyro.ico"))
    if not os.path.exists(icon):
        return
    try:
        root.iconbitmap(default=icon)
    except tk.TclError:
        pass


# --------------------------------------------------------------------------- #
# Controller worker: owns the HID device and hands states to the UI
# --------------------------------------------------------------------------- #

class ControllerWorker(threading.Thread):
    """Reads the controller and optionally mirrors it onto a virtual DS4."""

    def __init__(self, config: mapping.MappingConfig, source: str = "auto",
                 backend: str = "vigem"):
        super().__init__(daemon=True)
        self.config = config
        self.source = source
        #: Which virtual pad the forwarding mode creates: "vigem" (DualShock 4) or
        #: "dualsense" (a real DualSense through VIIPER).
        self.backend = backend if backend in ("vigem", "dualsense") else "vigem"
        self.states: queue.Queue = queue.Queue(maxsize=2)
        self.controller = None
        self.kind = "switch"
        self.status = "正在寻找手柄…"
        self.connected = False
        self.forwarding = False
        self.error: str | None = None
        self._client: vigem.ViGEmClient | None = None
        self._pad = None
        self._mapper = None
        self._halt = threading.Event()
        self._want_forward = False
        self._backend_changed = False
        self._lock = threading.Lock()

    # -- commands from the UI --------------------------------------------- #

    def set_forwarding(self, enabled: bool) -> None:
        with self._lock:
            self._want_forward = enabled

    def set_backend(self, backend: str) -> None:
        """Switch between the DualShock 4 and DualSense sinks.

        The pump thread does the actual teardown/rebuild: touching the pad from the
        UI thread while a report is being submitted is how you get a half-written
        report.
        """
        if backend not in ("vigem", "dualsense"):
            return
        with self._lock:
            if backend == self.backend:
                return
            self.backend = backend
            self._backend_changed = True

    def stop(self) -> None:
        """Ask the worker to finish; it tears the virtual pad down on the way out."""
        self._halt.set()

    # -- forwarding plumbing ---------------------------------------------- #

    def _start_forwarding(self) -> None:
        if self._pad is not None or self.controller is None:
            return
        try:
            if self.backend == "dualsense":
                self.status = "正在准备虚拟 DualSense（VIIPER）…"
                self._ensure_viiper()
                pad = viiper.DualSensePad(log=lambda message: None)
            else:
                self._client = vigem.ViGEmClient()
                self._client.connect()
                pad = self._client.create_ds4()
            self._pad = pad
            pad.plug_in()
            if self.kind == "ds4":
                self._mapper = mapping.DS4ToDS4Relay(self.controller, self._pad, self.config)
            else:
                self._mapper = mapping.SwitchToDS4Mapper(self.controller, self._pad,
                                                         self.config)
            if hasattr(self._mapper, "start_streaming"):
                self._mapper.start_streaming()
            self.forwarding = True
            self.status = ("已开启转发：虚拟 DualSense 已插入（VIIPER）"
                           if self.backend == "dualsense"
                           else "已开启转发：虚拟 DualShock 4 已插入")
        except Exception as exc:  # pragma: no cover - depends on the host
            self._teardown_forwarding()
            self.status = f"转发失败：{exc}"

    def _ensure_viiper(self) -> None:
        """Make sure the VIIPER service is up before creating the DualSense.

        Same helper the rumble endpoint uses, so a user who never started VIIPER
        by hand still gets a working pad (and if it cannot start, the failure lands
        in ``status`` instead of a traceback).
        """
        if haptics.viiper_port_open():
            return
        self.status = "正在启动 VIIPER 服务…"
        exe = haptics.find_server_exe()
        if exe is None:
            raise RuntimeError("没找到 VIIPER-*.exe，无法启动虚拟 DualSense 服务")
        haptics.launch_viiper_server(exe)
        if not haptics.wait_for_viiper(20.0):
            raise RuntimeError("VIIPER 服务没有起来（端口 3242 无响应）")

    def _teardown_forwarding(self) -> None:
        self.forwarding = False
        if self._mapper is not None and hasattr(self._mapper, "stop_streaming"):
            try:
                self._mapper.stop_streaming()
            except Exception:
                pass
        if self._pad is not None:
            try:
                self._pad.unplug()
            except Exception:
                pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._pad = None
        self._mapper = None
        self._client = None

    # -- main loop -------------------------------------------------------- #

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self._connect()
                self._pump()
            except switchpro.DeviceGoneError as exc:
                self.status = f"手柄已断开（{exc}）；等待重新连接…"
                self.connected = False
            except dualshock4.DS4GoneError as exc:
                self.status = f"手柄已断开（{exc}）；等待重新连接…"
                self.connected = False
            except (switchpro.SwitchProError, dualshock4.DS4Error,
                    sources.NoControllerError) as exc:
                self.status = f"手柄错误：{exc}"
                self.connected = False
            except OSError as exc:
                self.status = f"设备错误：{exc}"
                self.connected = False
            except Exception as exc:  # pragma: no cover - defensive
                self.status = f"未预期的错误：{exc}"
                self.connected = False
            finally:
                self._teardown_forwarding()
                if self.controller is not None:
                    try:
                        self.controller.close()
                    except Exception:
                        pass
                    self.controller = None
            if self._halt.is_set():
                break
            time.sleep(1.5)

    def _connect(self) -> None:
        self.status = "正在寻找手柄…"
        kind, controller = sources.open_source(self.source, log=lambda m: None)
        self.kind = kind
        self.controller = controller
        self.connected = True
        if kind == "ds4":
            self.status = f"已连接：{controller.describe()}"
        else:
            self.status = (f"已连接：{controller.device_type_name} / {controller.transport} / "
                           f"固件 {controller.firmware[0]}.{controller.firmware[1]} / "
                           f"体感标定 {controller.imu_calibration.source}")

    def _pump(self) -> None:
        while not self._halt.is_set():
            state = self.controller.read_state(timeout_ms=200)
            if state is None:
                continue
            with self._lock:
                want_forward = self._want_forward
                backend_changed = self._backend_changed
                self._backend_changed = False
            if backend_changed and self._pad is not None:
                # The sink changed while a pad was up: rebuild it in this thread.
                self._teardown_forwarding()
            if want_forward and self._pad is None:
                self._start_forwarding()
            elif not want_forward and self._pad is not None:
                self._teardown_forwarding()
            if self._mapper is not None:
                try:
                    self._mapper.push_state(state)
                except vigem.ViGEmError as exc:
                    self.status = f"转发中断：{exc}"
                    self._teardown_forwarding()
            if self.kind == "ds4":
                if self.controller.warn_if_static():
                    self.status = "手柄没有在动：请按 PS 键唤醒（休眠的蓝牙手柄会一直返回同一条报文）"
                elif self.controller.ensure_motion():
                    self.status = "手柄退回了不带体感的最小报文，已自动重新请求完整报文"
            try:
                self.states.put_nowait(state)
            except queue.Full:
                try:
                    self.states.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.states.put_nowait(state)
                except queue.Full:
                    pass


# --------------------------------------------------------------------------- #
# Live controller drawing
# --------------------------------------------------------------------------- #

class DemoWorker(threading.Thread):
    """Generates synthetic controller states so the window can be previewed."""

    BUTTON_CYCLE = [
        ("B", (0x04, 0, 0)), ("A", (0x08, 0, 0)), ("Y", (0x01, 0, 0)),
        ("X", (0x02, 0, 0)), ("R", (0x40, 0, 0)), ("L", (0, 0, 0x40)),
        ("ZR", (0x80, 0, 0)), ("ZL", (0, 0, 0x80)), ("L3", (0, 0x08, 0)),
        ("R3", (0, 0x04, 0)), ("Plus", (0, 0x02, 0)), ("Minus", (0, 0x01, 0)),
        ("Home", (0, 0x10, 0)), ("Capture", (0, 0x20, 0)),
        ("Up", (0, 0, 0x02)), ("Right", (0, 0, 0x04)),
        ("Down", (0, 0, 0x01)), ("Left", (0, 0, 0x08)),
    ]

    def __init__(self, config: mapping.MappingConfig, source: str = "auto",
                 backend: str = "vigem"):
        super().__init__(daemon=True)
        self.config = config
        self.source = source          # accepted so the UI can swap workers freely
        self.backend = backend
        self.states: queue.Queue = queue.Queue(maxsize=2)
        self.controller = DemoController()
        self.status = "演示模式：没有读取真实手柄"
        self.connected = True
        self.forwarding = False
        self._halt = threading.Event()
        self._start = time.monotonic()

    def set_forwarding(self, enabled: bool) -> None:
        self.status = "演示模式不支持转发"

    def set_backend(self, backend: str) -> None:
        self.backend = backend if backend in ("vigem", "dualsense") else "vigem"

    def stop(self) -> None:
        """Ask the demo worker to finish (the App calls this on every worker)."""
        self._halt.set()

    def run(self) -> None:
        index = 0
        while not self._halt.is_set():
            elapsed = time.monotonic() - self._start
            name, buttons = self.BUTTON_CYCLE[index % len(self.BUTTON_CYCLE)]
            index = int(elapsed / 0.6)
            phase = (elapsed % 6.0) / 6.0
            angle = phase * 2 * math.pi
            radius = 1800
            left = (int(2048 + radius * 0.9 * math.cos(angle)),
                    int(2048 + radius * 0.9 * math.sin(angle)))
            right = (int(2048 - radius * 0.6 * math.cos(angle)),
                     int(2048 - radius * 0.6 * math.sin(angle)))
            gyro = (int(200 * math.sin(angle)), int(150 * math.cos(angle)), 40)
            accel = (int(1200 * math.sin(angle)), 0, int(4000 * math.cos(angle)))
            state = switchpro.ControllerState(
                report_id=0x30, timer=int(elapsed * 60) & 0xFF, battery_level=4,
                charging=False, connection_info=0, buttons=buttons,
                left_stick=left, right_stick=right, vibration_code=0,
                imu=[switchpro.ImuSample(accel=accel, gyro=gyro)],
            )
            try:
                self.states.put_nowait(state)
            except queue.Full:
                try:
                    self.states.get_nowait()
                except queue.Empty:
                    pass
            time.sleep(1 / 60)



class App:
    def __init__(self, root: tk.Tk, forward: bool = False, demo: bool = False,
                 topmost: bool = False, scale: float = 1.0, source: str = "auto"):
        self.root = root
        self.scale = scale
        if topmost:
            root.attributes("-topmost", True)
        self.config = mapping.MappingConfig()
        mapping.apply_captured(self.config, mapping.load_captured_buttons())
        # Remembered sensitivity and anti-deadzone from the last run
        # (settings.json beside the exe).
        self.config.gyro_gain = settings.gyro_gain()
        # Remembered like the sensitivity: the window shows exactly what the
        # virtual pad was doing when the program last closed.
        self.config.swap_face_buttons = settings.swap_face_buttons()
        self.deadzone = settings.motion_deadzone()
        self.config.motion_deadzone_dps = tuple(self.deadzone)
        self.source = source
        self.controller = None
        self.last_state = None
        self._last_live_update = 0.0
        #: Phase of the status-dot breathing animation (0..1).
        self._pulse_phase = 0.0
        self._ds4_mode = False
        #: id of the scheduled UI tick, so it can be cancelled on close.
        self._tick_id: str | None = None
        #: Current forwarding mode; see MODE_* above.
        self.mode = MODE_OFF
        #: Input source to restore when the Genshin mode (command line only) is off.
        self._source_before_genshin: str | None = None

        # Audio haptics: one worker thread owns the slow part (creating the audio
        # endpoint can take a few seconds, and a Bluetooth pad write can take 20 ms,
        # so neither may ever happen on the UI thread).
        self.haptics_runner = haptics.HapticsRunner()
        #: Identity of the controller the current rumble request points at; a
        #: reconnect produces a new object, which is what makes the runner restart.
        self._haptics_key: tuple | None = None
        self._haptics_error: str | None = None

        self._build_layout()
        # Hand the remembered sliders to the runner before anything can start, so
        # the first engine it creates already has the user's values.
        try:
            self.haptics_runner.set_tuning(self.haptics_values)
        except Exception:
            pass
        self.worker = (DemoWorker(self.config) if demo
                       else ControllerWorker(self.config, source=self.source))
        self.worker.start()
        # Restore the checkbox state from the last run (settings.json).
        remembered = settings.mode()
        if forward and not demo:
            remembered = MODE_FULL
        if remembered != MODE_OFF:
            self.apply_mode(remembered)
        self.root.after(16, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout ----------------------------------------------------------- #

    def _px(self, value: float) -> int:
        """A design pixel -> a screen pixel at the current display scale."""
        return int(round(value * self.scale))

    def _build_layout(self) -> None:
        """Two section cards: one per group of settings, rows inside.

        Every padding goes through :meth:`_px` so the layout keeps its
        proportions at 125%/150% display scaling; the fonts are point-sized and
        already scale on their own.
        """
        page = ui_theme.BG
        px = self._px
        label_font = ui_theme.font(ui_theme.SIZE_BODY)
        small_font = ui_theme.font(ui_theme.SIZE_SMALL)
        self.root.configure(bg=page)

        header = tk.Frame(self.root, bg=page)
        header.pack(fill="x", padx=px(ui_theme.SPACE_MD),
                    pady=(px(ui_theme.SPACE_SM + 2), px(2)))
        # The status dot.  A glyph rather than a drawn circle: Tk renders the
        # font anti-aliased, and colouring it per frame is what gives the
        # "it is alive" pulse without any custom painting.
        self.status_dot = tk.Label(header, text="●", bg=page, fg=ui_theme.ACCENT_DIM,
                                   font=ui_theme.font(ui_theme.SIZE_SMALL))
        self.status_dot.pack(side="left", padx=(0, px(5)))
        self.status_label = tk.Label(header, text="", bg=page, fg=ui_theme.TEXT_MUTED,
                                     font=small_font, anchor="w")
        self.status_label.pack(side="left")
        self.forward_state = tk.Label(header, text="转发：关闭", bg=page,
                                      fg=ui_theme.TEXT_MUTED, font=small_font)
        self.forward_state.pack(side="right")

        body = tk.Frame(self.root, bg=page)
        body.pack(fill="x", padx=px(ui_theme.SPACE_MD), pady=(0, px(4)))
        self.body = body

        # -- readout card ----------------------------------------------------- #
        self.info_panel = ui_theme.RoundedFrame(body, padding=(px(14), px(8)))
        self.info_panel.pack(fill="x")
        info = self.info_panel.content
        self.live_label = tk.Label(info, text="", bg=ui_theme.SURFACE, fg=ui_theme.TEXT,
                                   font=ui_theme.mono(ui_theme.SIZE_READOUT),
                                   justify="left", anchor="w")
        self.live_label.pack(fill="x")
        self.motion_label = tk.Label(info, text="", bg=ui_theme.SURFACE,
                                     fg=ui_theme.TEXT,
                                     font=ui_theme.mono(ui_theme.SIZE_READOUT),
                                     justify="left", anchor="w")
        self.motion_label.pack(fill="x", pady=(px(2), 0))
        self.worker_label = tk.Label(info, text="", bg=ui_theme.SURFACE,
                                     fg=ui_theme.TEXT_MUTED, font=small_font,
                                     justify="left", anchor="w",
                                     wraplength=px(660))
        self.worker_label.pack(fill="x", pady=(px(4), 0))

        footer = tk.Frame(self.root, bg=page)
        footer.pack(fill="x", padx=px(ui_theme.SPACE_MD),
                    pady=(0, px(ui_theme.SPACE_MD)))
        self.footer = footer

        label_column = int(max(label_font.measure(text) for text in (
            "虚拟手柄只转发体感（按键留给实体手柄）",
            "完整转发到虚拟 DualShock 4", "输入手柄", "体感灵敏度",
            "× 与 ○ 互换，□ 与 △ 互换", "死区补偿 · 俯仰",
            "开启震动", "震动强度", "低频/高频分界")) + px(20))

        def section(title: str, top: int = ui_theme.SPACE_MD) -> tk.Frame:
            """A titled card; rows go inside the returned frame."""
            tk.Label(footer, text=title, bg=page, fg=ui_theme.TEXT_MUTED,
                     font=ui_theme.font(ui_theme.SIZE_SECTION), anchor="w").pack(
                         fill="x", padx=px(2), pady=(px(top), px(ui_theme.SPACE_XS)))
            card = ui_theme.RoundedFrame(footer, padding=(px(14), px(4)), radius=px(12))
            card.pack(fill="x")
            return card.content

        def label_cell(parent: tk.Frame, text: str, hint: str | None = None,
                       padx: int = 0) -> None:
            """A label, optionally with a muted one-liner underneath.

            Without a hint the label fills the row so Tk keeps the text vertically
            centred -- the look every other card has.  With a hint the two lines
            stack from the top, which is what the rumble card needs.
            """
            if not hint:
                tk.Label(parent, text=text, bg=ui_theme.SURFACE, fg=ui_theme.TEXT,
                         font=label_font, anchor="w").pack(
                             side="left", fill="y", padx=(padx, 0))
                return
            column = tk.Frame(parent, bg=ui_theme.SURFACE)
            column.pack(side="left", fill="y", padx=(padx, 0), pady=(px(3), 0))
            tk.Label(column, text=text, bg=ui_theme.SURFACE, fg=ui_theme.TEXT,
                     font=label_font, anchor="w").pack(side="top", anchor="w")
            tk.Label(column, text=hint, bg=ui_theme.SURFACE,
                     fg=ui_theme.TEXT_MUTED, font=small_font, anchor="w").pack(
                         side="top", anchor="w")

        def row(parent: tk.Frame, label: str | None, first: bool = False,
                hint: str | None = None) -> tk.Frame:
            """One row inside a section card, with a hairline above it.

            ``hint`` adds a second, muted line under the label: the parameters in
            the rumble card are not self-explanatory, and a one-line note beats a
            tooltip nobody hovers over.
            """
            if not first:
                tk.Frame(parent, bg=ui_theme.BORDER, height=max(1, px(1))).pack(fill="x")
            # A row with a hint needs two text lines: give it six logical pixels
            # more, otherwise Tk squeezes the muted second line to a sliver.
            row_height = 40 if hint else 34
            inside = tk.Frame(parent, bg=ui_theme.SURFACE,
                              height=int(row_height * self.scale))
            inside.pack(fill="x")
            inside.pack_propagate(False)
            if label is not None:
                holder = tk.Frame(inside, bg=ui_theme.SURFACE, width=label_column)
                holder.pack(side="left", fill="y")
                holder.pack_propagate(False)
                label_cell(holder, label, hint)
            return inside

        # -- output mode ------------------------------------------------------ #
        mode_card = section("输出模式")
        source_row = row(mode_card, "输入手柄", first=True)
        self.source_var = tk.StringVar(
            value=SOURCE_LABELS.get(self.source, SOURCE_LABELS["auto"]))
        self.source_box = ui_theme.ChoiceBox(
            source_row, self.source_var, list(SOURCE_LABELS.values()),
            scale=self.scale)
        self.source_box.pack(side="left", pady=px(4))
        self.source_box.bind("<<ComboboxSelected>>", self._on_source_changed)

        self.forward_var = tk.BooleanVar(value=False)
        forward_row = row(mode_card, "完整转发到虚拟 DualShock 4")
        self.forward_check = ui_theme.ToggleSwitch(
            forward_row, self.forward_var, command=self.toggle_forward, scale=self.scale)
        self.forward_check.pack(side="right")

        self.motion_only_var = tk.BooleanVar(value=False)
        motion_row = row(mode_card, "虚拟手柄只转发体感（按键留给实体手柄）")
        self.motion_check = ui_theme.ToggleSwitch(
            motion_row, self.motion_only_var, command=self.toggle_motion_only,
            scale=self.scale)
        self.motion_check.pack(side="right")

        # Same payload as "full", but the virtual pad is a real DualSense (VIIPER)
        # instead of a DualShock 4 -- that is what games reading DualSense-only
        # features (native haptics, adaptive triggers) need.
        self.ds5_var = tk.BooleanVar(value=False)
        ds5_row = row(mode_card, "转发到虚拟 DualSense 手柄")
        self.ds5_check = ui_theme.ToggleSwitch(
            ds5_row, self.ds5_var, command=self.toggle_ds5, scale=self.scale)
        self.ds5_check.pack(side="right")

        # The virtual pad's face buttons can be exchanged without touching the
        # controller, so a pad laid out one way still fits a game that expects the
        # other.  Independent of the forwarding mode above.
        self.swap_face_var = tk.BooleanVar(value=self.config.swap_face_buttons)
        swap_row = row(mode_card, "× 与 ○ 互换，□ 与 △ 互换")
        self.swap_face_check = ui_theme.ToggleSwitch(
            swap_row, self.swap_face_var, command=self.toggle_swap_face_buttons,
            scale=self.scale)
        self.swap_face_check.pack(side="right")

        self.share_note = tk.Label(
            footer, text=f"三种模式都会把 {SHARE_NOTE} 用作触控板按下",
            bg=page, fg=ui_theme.TEXT_MUTED, font=small_font, anchor="w")
        self.share_note.pack(fill="x", padx=px(2), pady=(px(ui_theme.SPACE_XS), 0))

        # -- motion tuning ---------------------------------------------------- #
        tune_card = section("体感调节")
        self.gain_var = tk.DoubleVar(value=self.config.gyro_gain)
        gain_row = row(tune_card, "体感灵敏度", first=True)
        self.gain_entry = ui_theme.ValueBox(gain_row, scale=self.scale)
        self.gain_entry.pack(side="left", pady=px(4))
        self.gain_entry.bind("<Return>", self._on_gain_entry)
        self.gain_entry.bind("<FocusOut>", self._on_gain_entry)
        self.gain_scale = ui_theme.DiamondSlider(
            gain_row, self.gain_var, from_=GAIN_MIN, to=GAIN_MAX, step=GAIN_STEP,
            command=self._on_gain_scale, scale=self.scale)
        self.gain_scale.pack(side="left", fill="x", expand=True, padx=(px(12), 0))
        self.gain_scale.bind("<ButtonRelease-1>", self._remember_gain)
        self._set_gain_entry(self.config.gyro_gain)

        self.deadzone_vars: dict[str, tk.DoubleVar] = {}
        self.deadzone_scales: dict[str, ui_theme.DiamondSlider] = {}
        self.deadzone_entries: dict[str, ui_theme.ValueBox] = {}
        for index, (axis, title) in enumerate(zip(settings.DEADZONE_AXES,
                                                  ("俯仰", "偏航", "滚转"))):
            var = tk.DoubleVar(value=float(self.deadzone[index]))
            dead_row = row(tune_card, f"死区补偿 · {title}")
            entry = ui_theme.ValueBox(dead_row, scale=self.scale)
            entry.pack(side="left", pady=px(4))
            entry.bind("<Return>", lambda _event, key=axis: self._on_deadzone_entry(key))
            entry.bind("<FocusOut>", lambda _event, key=axis: self._on_deadzone_entry(key))
            scale = ui_theme.DiamondSlider(
                dead_row, var, from_=settings.DEADZONE_MIN, to=settings.DEADZONE_MAX,
                step=GAIN_STEP, scale=self.scale,
                command=lambda _value, key=axis: self._on_deadzone_scale(key))
            scale.pack(side="left", fill="x", expand=True, padx=(px(12), 0))
            scale.bind("<ButtonRelease-1>", lambda _event: self._remember_deadzone())
            self.deadzone_vars[axis] = var
            self.deadzone_scales[axis] = scale
            self.deadzone_entries[axis] = entry
            self._set_deadzone_entry(axis, float(self.deadzone[index]))

        # -- audio haptics ---------------------------------------------------- #
        # 开关 + 七个滑块（强度 / 灵敏度 / 起振 / 拖尾 / 轻音过滤 / 响应曲线 /
        # 频段分界）。原来的预设下拉去掉了：那些预设本质上就是这几组数的组合，
        # 直接让人拖更直观。每个参数下面一行小字说明它是干什么的。
        haptics_card = section("PS5手柄原生震动")
        self.haptics_values = settings.haptics_tuning()
        self.haptics_vars: dict[str, tk.DoubleVar] = {}
        self.haptics_entries: dict[str, ui_theme.ValueBox] = {}
        self.haptics_scales: dict[str, ui_theme.DiamondSlider] = {}

        self.haptics_var = tk.BooleanVar(value=settings.haptics_enabled())
        master_row = row(haptics_card, "开启震动", first=True)
        self.haptics_check = ui_theme.ToggleSwitch(
            master_row, self.haptics_var, command=self.toggle_haptics,
            scale=self.scale)
        self.haptics_check.pack(side="right")

        # 强度那一行右边留给"左右分工"（它是开关，不是滑块）。
        gain_row = row(haptics_card, "震动强度", hint=HAPTICS_HINTS["gain"])
        self._add_haptics_slider(gain_row, "gain")
        self.haptics_dual_var = tk.BooleanVar(value=self.haptics_values["dual"])
        self.haptics_dual_check = ui_theme.ToggleSwitch(
            gain_row, self.haptics_dual_var, command=self.toggle_haptics_dual,
            scale=self.scale)
        self.haptics_dual_check.pack(side="right", padx=(0, px(2)))
        dual_note = tk.Frame(gain_row, bg=ui_theme.SURFACE)
        dual_note.pack(side="right", padx=(px(14), px(10)))
        tk.Label(dual_note, text="左右分工", bg=ui_theme.SURFACE, fg=ui_theme.TEXT,
                 font=label_font, anchor="e").pack(anchor="e")
        tk.Label(dual_note, text=HAPTICS_HINTS["dual"], bg=ui_theme.SURFACE,
                 fg=ui_theme.TEXT_MUTED, font=small_font, anchor="e").pack(anchor="e")

        for left_key, left_label, right_key, right_label in HAPTICS_ROWS:
            inside = row(haptics_card, left_label, hint=HAPTICS_HINTS[left_key])
            self._add_haptics_slider(inside, left_key)
            if right_key is not None:
                # 右边这个参数同样要一行说明：它挤在左侧标签栏之外，所以自己
                # 带一个标签列（标签 + 小字），和左边保持同样的两行结构。
                right_holder = tk.Frame(inside, bg=ui_theme.SURFACE)
                right_holder.pack(side="left", fill="y", padx=(px(14), px(6)))
                label_cell(right_holder, right_label, HAPTICS_HINTS[right_key])
                self._add_haptics_slider(inside, right_key)

        # 状态提示 + 一键恢复默认（默认就是原来「低频沉浸」那一档）。
        note_row = tk.Frame(footer, bg=page)
        note_row.pack(fill="x", padx=px(2), pady=(px(ui_theme.SPACE_XS), 0))
        self.haptics_note = tk.Label(
            note_row, text="", bg=page, fg=ui_theme.TEXT_MUTED, font=small_font,
            anchor="w", justify="left", wraplength=px(800))
        self.haptics_note.pack(side="left", fill="x", expand=True)
        self.haptics_reset = tk.Label(
            note_row, text="恢复默认", bg=page, fg=ui_theme.ACCENT_DIM,
            font=small_font, cursor="hand2")
        self.haptics_reset.pack(side="right", padx=(px(10), 0))
        self.haptics_reset.bind("<Button-1>", self.reset_haptics_defaults)
        self._update_haptics_note()

    # -- controls --------------------------------------------------------- #

    def _set_deadzone_entry(self, axis: str, value: float) -> None:
        entry = self.deadzone_entries[axis]
        entry.delete(0, "end")
        entry.insert(0, f"{value:.1f}")

    def _apply_deadzone(self, axis: str, value: float) -> None:
        """Store one axis of the anti-deadzone; the mappers read it per report."""
        value = round(max(settings.DEADZONE_MIN, min(settings.DEADZONE_MAX, float(value))), 1)
        index = settings.DEADZONE_AXES.index(axis)
        current = list(self.config.motion_deadzone_dps)
        current[index] = value
        self.config.motion_deadzone_dps = tuple(current)
        self.deadzone = tuple(current)
        if abs(self.deadzone_vars[axis].get() - value) >= GAIN_STEP / 2:
            self.deadzone_vars[axis].set(value)
        self._set_deadzone_entry(axis, value)

    def _on_deadzone_scale(self, axis: str) -> None:
        self._apply_deadzone(axis, self.deadzone_scales[axis].get())

    def _on_deadzone_entry(self, axis: str) -> None:
        text = self.deadzone_entries[axis].get().strip()
        try:
            value = float(text)
        except ValueError:
            value = self.config.motion_deadzone_dps[settings.DEADZONE_AXES.index(axis)]
        self._apply_deadzone(axis, value)
        self._remember_deadzone()

    def _remember_deadzone(self, _event=None) -> None:
        """Store the three compensations so the next start shows them again."""
        settings.set_motion_deadzone(self.config.motion_deadzone_dps)

    def _set_gain_entry(self, value: float) -> None:
        self.gain_entry.delete(0, "end")
        self.gain_entry.insert(0, f"{value:.1f}")

    def _apply_gain(self, value: float) -> None:
        """Set the gyro scale; the mappers read it on every report."""
        value = max(GAIN_MIN, min(GAIN_MAX, value))
        self.config.gyro_gain = round(value, 1)
        if abs(self.gain_var.get() - self.config.gyro_gain) >= GAIN_STEP / 2:
            self.gain_var.set(self.config.gyro_gain)
        self._set_gain_entry(self.config.gyro_gain)

    def _remember_gain(self, _event=None) -> None:
        """Store the sensitivity so the next start shows it again."""
        settings.set_gyro_gain(self.config.gyro_gain)

    def _on_gain_scale(self, _value=None) -> None:
        self._apply_gain(float(self.gain_scale.get()))

    def _on_gain_entry(self, _event=None) -> None:
        text = self.gain_entry.get().strip().replace("×", "").replace("x", "")
        try:
            value = float(text)
        except ValueError:
            value = self.config.gyro_gain          # put the old value back
        self._apply_gain(value)
        self._remember_gain()

    # -- audio haptics ----------------------------------------------------- #

    def toggle_haptics(self) -> None:
        """主开关：开 → 先确保音频端点、有手柄再起引擎；关 → 停引擎并拆自己建的设备。"""
        enabled = settings.set_haptics_enabled(self.haptics_var.get())
        self._haptics_error = None
        if not enabled:
            self._haptics_key = None
            self.haptics_runner.request_off()
        else:
            self._sync_haptics()
        self._update_haptics_note()

    def _sync_haptics(self) -> None:
        """Tell the runner what the switch and the current controller call for.

        Never blocks: the runner thread does the slow part.  Three states, and
        only the endpoint's lifetime changes between them (it belongs to the
        runner, so a disconnect never removes the virtual sound card):

        * switch off                     -> ``request_off`` (stop + release);
        * switch on, no usable pad       -> ``prepare`` (endpoint only, wait);
        * switch on, pad usable          -> ``request`` (endpoint + engine).

        A reconnect hands the worker a brand-new controller object, so the
        identity kept in ``_haptics_key`` is also what makes the engine come back
        on the new handle instead of writing into a dead one.
        """
        controller = getattr(self.worker, "controller", None)
        transport = getattr(controller, "transport", None)
        kind = getattr(self.worker, "kind", None)
        usable = (self.haptics_var.get() and controller is not None
                  and kind is not None and transport in ("usb", "bluetooth")
                  # A closed handle means the worker is tearing this controller
                  # down; wait for the reconnect instead of writing into it.
                  and bool(getattr(controller, "is_open", True)))
        if not self.haptics_var.get():
            key = None
        elif usable:
            # Keep the controller *object* (not its id): it pins the identity, so a
            # reconnect always produces a different key even if CPython reuses an
            # id, and the engine is restarted on the new session.
            key = (controller, transport)
        else:
            key = ("prepare",)
        if key == self._haptics_key:
            return
        self._haptics_key = key
        if key is None:
            self.haptics_runner.request_off()
            return
        if key == ("prepare",):
            self.haptics_runner.prepare()
            return
        try:
            pad_kind = haptics.pad_kind(kind)
        except haptics.HapticsError:
            self._haptics_key = None
            self.haptics_runner.prepare()
            return
        # 不把手柄句柄交给引擎：震动自己开一个句柄，手柄拔掉时工作线程关它自己
        # 那个句柄，就不会和震动写入撞在同一个 handle 上（那会直接崩）。
        self.haptics_runner.request(pad_kind, transport)

    # -- 六个滑块 ---------------------------------------------------------- #

    def _add_haptics_slider(self, parent: tk.Frame, key: str) -> None:
        """One value box + slider, wired to ``haptics_values[key]``."""
        px = self._px
        low, high = settings.HAPTICS_RANGES[key]
        var = tk.DoubleVar(value=float(self.haptics_values[key]))
        entry = ui_theme.ValueBox(parent, scale=self.scale)
        entry.pack(side="left", pady=px(4), padx=(px(2), 0))
        entry.bind("<Return>", lambda _event, name=key: self._on_haptics_entry(name))
        entry.bind("<FocusOut>",
                   lambda _event, name=key: self._on_haptics_entry(name))
        slider = ui_theme.DiamondSlider(
            parent, var, from_=low, to=high, step=HAPTICS_STEPS[key],
            command=lambda _value, name=key: self._on_haptics_scale(name),
            scale=self.scale)
        # A canvas defaults to a wide request; two per row would then squeeze the
        # right-hand one to nothing.  A small request plus expand shares evenly.
        slider.configure(width=px(110))
        slider.pack(side="left", fill="x", expand=True, padx=(px(10), px(8)))
        slider.bind("<ButtonRelease-1>", lambda _event: self._remember_haptics())
        self.haptics_vars[key] = var
        self.haptics_entries[key] = entry
        self.haptics_scales[key] = slider
        self._set_haptics_entry(key, float(self.haptics_values[key]))

    def _set_haptics_entry(self, key: str, value: float) -> None:
        entry = self.haptics_entries[key]
        entry.delete(0, "end")
        entry.insert(0, format_haptics(key, value))

    def _apply_haptics_value(self, key: str, value: float) -> None:
        """Clamp one parameter, show it, hand it to the engine."""
        clamped = settings.clamp_haptics(key, value)
        self.haptics_values[key] = clamped
        var = self.haptics_vars[key]
        if abs(float(var.get()) - clamped) > 1e-9:
            var.set(clamped)                    # redraws the knob, no callback
        self._set_haptics_entry(key, clamped)
        self.haptics_runner.set_value(key, clamped)

    def _on_haptics_scale(self, key: str) -> None:
        self._apply_haptics_value(key, float(self.haptics_scales[key].get()))

    def _on_haptics_entry(self, key: str) -> None:
        parsed = parse_haptics(key, self.haptics_entries[key].get())
        if parsed is None:                      # garbage: put the old value back
            self._set_haptics_entry(key, float(self.haptics_values[key]))
            return
        self._apply_haptics_value(key, parsed)
        self._remember_haptics()

    def toggle_haptics_dual(self) -> None:
        dual = bool(self.haptics_dual_var.get())
        self.haptics_values["dual"] = dual
        self.haptics_runner.set_value("dual", dual)
        self._remember_haptics()

    def reset_haptics_defaults(self, _event=None) -> None:
        """Restore the six sliders (and the dual switch) to the shipped defaults."""
        defaults = dict(haptics.DEFAULT_TUNING)
        for key in settings.HAPTICS_KEYS:
            self._apply_haptics_value(key, defaults[key])
        self.haptics_dual_var.set(defaults["dual"])
        self.haptics_values["dual"] = defaults["dual"]
        self.haptics_runner.set_value("dual", defaults["dual"])
        self._remember_haptics()
        self._update_haptics_note()

    def _remember_haptics(self, _event=None) -> None:
        """Remember the six numbers, the two switches and the master switch."""
        settings.save_haptics_tuning(self.haptics_values)
        settings.set_haptics_enabled(self.haptics_var.get())

    def _update_haptics_note(self) -> None:
        """One line: endpoint + live levels, or why the switch went back off."""
        runner = self.haptics_runner
        if not self.haptics_var.get():
            if self._haptics_error:
                text = f"PS5手柄原生震动没起来：{self._haptics_error}"
            elif runner.running:
                text = "PS5手柄原生震动：正在停止…"
            else:
                text = "PS5手柄原生震动：关闭（打开后会自动准备虚拟音频端点）"
        else:
            text = runner.status
            if runner.running:
                left, right = runner.levels()
                text = f"{text}  ·  L {left:.2f} / R {right:.2f}"
        if str(self.haptics_note.cget("text")) != text:
            self.haptics_note.configure(text=text)


    def apply_mode(self, mode: str) -> None:
        """The single place that changes the forwarding mode.

        Three mutually exclusive modes (DualShock 4 full, motion-only, DualSense
        full), all of them including the Share mapping (Share on a DualShock 4,
        ``-`` on a Switch Pro becomes the touchpad click), because the pad's own
        touchpad button is unreliable over Bluetooth.
        """
        if mode not in (MODE_OFF, MODE_FULL, MODE_MOTION, MODE_GENSHIN, MODE_DS5):
            mode = MODE_OFF
        genshin = mode == MODE_GENSHIN
        previous = self.mode
        self.mode = mode
        # "ds5" forwards everything, just onto a DualSense instead of a DualShock 4.
        self.config.motion_only = mode in (MODE_MOTION, MODE_GENSHIN)
        # Share → touchpad is part of every forwarding mode; off only when nothing
        # is forwarded at all.
        self.config.share_as_touchpad = mode != MODE_OFF

        # Keep the widgets in step; set() does not re-trigger the commands.
        self.forward_var.set(mode == MODE_FULL)
        self.motion_only_var.set(mode == MODE_MOTION)
        self.ds5_var.set(mode == MODE_DS5)

        if genshin:
            # Only reachable from the command line now, but it still pins the input
            # source to the DualShock 4 like the old on-screen button did.
            if self._source_before_genshin is None:
                self._source_before_genshin = getattr(self.worker, "source", "auto")
            self.source_var.set(SOURCE_LABELS["ds4"])
            self._switch_source("ds4")
        elif self._source_before_genshin is not None:
            restore = self._source_before_genshin
            self._source_before_genshin = None
            self.source_var.set(SOURCE_LABELS.get(restore, SOURCE_LABELS["auto"]))
            self._switch_source(restore)

        # The virtual DualSense brings its own audio endpoint.  Windows names a
        # *second* same-named device "2- DualSense …" and the game looks its output
        # up by name, so the rumble side must let go of its own audio-only card
        # *before* the pad is created, then adopt the pad's endpoint.
        if mode == MODE_DS5:
            self.haptics_runner.prefer_combined(True)
        elif previous == MODE_DS5:
            self.haptics_runner.prefer_combined(False)
        self._set_backend("dualsense" if mode == MODE_DS5 else "vigem")
        self._set_forwarding(mode != MODE_OFF)
        self._update_mode_hint()
        self._remember_mode()

    def _set_backend(self, backend: str) -> None:
        """Which virtual pad the worker should create (it rebuilds it itself)."""
        try:
            self.worker.set_backend(backend)
        except Exception:                            # pragma: no cover - defensive
            pass

    def _remember_mode(self) -> None:
        """Remember which checkbox was ticked, so the next start restores it."""
        settings.set_mode(self.mode)

    def _set_forwarding(self, enabled: bool) -> None:
        try:
            self.worker.set_forwarding(enabled)
        except Exception:
            pass

    def _update_mode_hint(self) -> None:
        share_note = "；Share / - 当作触控板按下" if self.config.share_as_touchpad else ""
        texts = {
            MODE_OFF: "当前不转发，只显示实时数值。",
            MODE_FULL: "完整转发：按键、摇杆、体感都会转发到虚拟 DualShock 4"
                       + share_note + "。",
            MODE_MOTION: "仅体感：虚拟手柄只发体感，按键摇杆由实体手柄负责"
                         + share_note + "。",
            MODE_GENSHIN: "仅体感 + 输入固定为 DualShock 4（命令行 --mode genshin 专用）"
                          + share_note + "。",
            MODE_DS5: "转发到虚拟 DualSense：按键、摇杆、体感都会转发到虚拟 "
                      "DualSense（VIIPER）" + share_note + "。",
        }
        self.worker_label.configure(text=texts.get(self.mode, ""))

    # -- input source ----------------------------------------------------- #

    def _on_source_changed(self, _event=None) -> None:
        """Restart the reader when the user picks a different input device."""
        label = self.source_var.get()
        chosen = next((key for key, text in SOURCE_LABELS.items() if text == label), "auto")
        if chosen != "auto":
            self._source_before_genshin = None      # a manual pick wins
        if self._switch_source(chosen):
            self.worker_label.configure(text=f"输入已切换为：{label}")

    def _switch_source(self, chosen: str) -> bool:
        """Swap the reader, waiting for the old one to unplug its virtual pad.

        Without the join the old worker's teardown could still be in flight while
        the new one plugs in, and for a moment two virtual DualShock 4 pads exist
        -- which is how a game ends up seeing a duplicated controller.
        """
        if chosen == getattr(self.worker, "source", None):
            return False
        previous = self.worker
        backend = getattr(previous, "backend", "vigem")
        try:
            previous.stop()
            previous.join(timeout=2.0)
        except Exception:
            pass
        self.worker = (DemoWorker(self.config, source=chosen, backend=backend)
                       if isinstance(previous, DemoWorker)
                       else ControllerWorker(self.config, source=chosen,
                                             backend=backend))
        self.worker.start()
        if self.mode != MODE_OFF:
            self._set_backend("dualsense" if self.mode == MODE_DS5 else "vigem")
            self._set_forwarding(True)
        return True




    def toggle_motion_only(self) -> None:
        """Checkbox 2: motion only, so the physical pad keeps buttons and sticks."""
        self.apply_mode(MODE_MOTION if self.motion_only_var.get() else MODE_OFF)

    def toggle_ds5(self) -> None:
        """Checkbox 3: forward everything, but onto a virtual DualSense."""
        self.apply_mode(MODE_DS5 if self.ds5_var.get() else MODE_OFF)

    def toggle_swap_face_buttons(self) -> None:
        """Exchange ✕↔○ and □↔△ on the virtual pad, and remember the choice.

        Independent of the forwarding mode: it only rewrites the four face-button
        bits on their way into the virtual pad, so the physical controller needs no
        settings of its own.  In "motion only" mode the report carries no buttons at
        all, which makes the switch a no-op there rather than a contradiction.
        """
        self.config.swap_face_buttons = settings.set_swap_face_buttons(
            self.swap_face_var.get())





    def _controller_description(self) -> str:
        controller = self.worker.controller
        if controller is None:
            return "未连接"
        calibration = controller.imu_calibration
        return (f"{controller.device_type_name} / {controller.transport} / "
                f"固件 {controller.firmware[0]}.{controller.firmware[1]} / "
                f"MAC {controller.mac_address} / 标定 {calibration.source} / "
                f"accel_scale={tuple(round(v, 8) for v in calibration.accel_scale)} / "
                f"gyro_scale={tuple(round(v, 6) for v in calibration.gyro_scale)} / "
                f"gyro_origin={calibration.gyro_origin}")

    # -- forwarding toggle ------------------------------------------------ #

    def toggle_forward(self) -> None:
        """Checkbox 1: forward buttons, sticks and motion to the virtual pad."""
        self.apply_mode(MODE_FULL if self.forward_var.get() else MODE_OFF)


    # -- periodic UI update ----------------------------------------------- #

    def _update_pulse(self) -> None:
        """Breathe the status dot: bright green while forwarding is live.

        Steady dim green when the pad is there but nothing is forwarded, red
        when there is no pad at all.  The only place the accent colour is used
        in the header, so "is it working?" is answerable from across a room --
        which is also the shot a screen recording wants.
        """
        self._pulse_phase = (self._pulse_phase + 0.012) % 1.0
        worker = self.worker
        if not getattr(worker, "connected", False):
            self.status_dot.configure(fg=ui_theme.DANGER)
            return
        if getattr(worker, "forwarding", False):
            span = len(ui_theme.PULSE) - 1
            step = int(round((0.5 + 0.5 * math.sin(self._pulse_phase * 2 * math.pi))
                             * span))
            self.status_dot.configure(fg=ui_theme.PULSE[max(0, min(span, step))])
            return
        self.status_dot.configure(fg=ui_theme.ACCENT_DIM)

    def _tick(self) -> None:
        # A tick can still be delivered while the window is being torn down; bail
        # out quietly instead of raising inside a Tk callback.
        if not self._alive():
            return
        state = None
        while True:
            try:
                state = self.worker.states.get_nowait()
            except queue.Empty:
                break
        if state is not None:
            self.last_state = state

        controller = self.worker.controller
        if state is not None and controller is not None:
            self._update_live_text(controller, state)

        self.status_label.configure(text=self.worker.status,
                                    fg=(OK if self.worker.connected else WARN))
        self.forward_state.configure(
            text="转发：开启" if self.worker.forwarding else "转发：关闭",
            fg=(OK if self.worker.forwarding else DIM))
        self._update_pulse()
        if self.haptics_runner.failed:
            # The endpoint could not be created (VIIPER down, driver missing...).
            # Never leave the switch sitting in "on" over a feature that is not
            # running: pop it back and say why in the note under the card.
            self.haptics_runner.failed = False
            self.haptics_var.set(False)
            settings.set_haptics_enabled(False)
            self._haptics_key = None
            self._haptics_error = self.haptics_runner.error or "音频通道创建失败"
        # The virtual sound card is owned by the rumble switch alone: turning the
        # virtual DualSense (or any forwarding mode) on and off must not touch it.
        self._sync_haptics()
        self._update_haptics_note()
        self._tick_id = self.root.after(16, self._tick)

    def _update_live_text(self, controller, state) -> None:
        now = time.monotonic()
        if now - self._last_live_update < 0.05:
            return
        self._last_live_update = now
        if isinstance(state, dualshock4.DS4State):
            names = "+".join(state.pressed_names) or "无"
            transport = getattr(controller, "transport", "?")
            fmt = "有线格式" if state.wired else "蓝牙格式"
            self.live_label.configure(
                text=f"按键: {names}    （链路 {transport}，报文 id=0x{state.report_id:02X} "
                     f"{fmt}）\n"
                     f"按键位 0x{state.buttons:04X}  特殊 0x{state.special:02X}   "
                     f"扳机 L2={state.triggers[0]} R2={state.triggers[1]}   "
                     f"电量 {state.battery_level}/10")
        else:
            names = "+".join(state.pressed_names) or "无"
            self.live_label.configure(
                text=f"按键: {names}\n"
                     f"字节: b0=0x{state.buttons[0]:02X} b1=0x{state.buttons[1]:02X} "
                     f"b2=0x{state.buttons[2]:02X}    电量 {state.battery_level_8}/8")
        if isinstance(state, dualshock4.DS4State):
            self.motion_label.configure(
                text=f"陀螺仪 °/s: X={state.gyro[0] / 16.0:+7.1f} "
                     f"Y={state.gyro[1] / 16.0:+7.1f} Z={state.gyro[2] / 16.0:+7.1f}\n"
                     f"重力 g   : X={state.accel[0] / 8192.0:+7.3f} "
                     f"Y={state.accel[1] / 8192.0:+7.3f} Z={state.accel[2] / 8192.0:+7.3f}")
        elif state.imu:
            dps = controller.imu_calibration.to_dps(state.imu[-1].gyro)
            g = controller.imu_calibration.to_g(state.imu[-1].accel)
            self.motion_label.configure(
                text=f"陀螺仪 °/s: X={dps[0]:+7.1f} Y={dps[1]:+7.1f} Z={dps[2]:+7.1f}\n"
                     f"重力 g   : X={g[0]:+7.3f} Y={g[1]:+7.3f} Z={g[2]:+7.3f}")




    def _alive(self) -> bool:
        """Is the window still there?  False while/after it is being destroyed."""
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _on_close(self) -> None:
        """Close the window, remember the sensitivity and stop the worker.

        Everything is guarded and the destroy is registered first: an exception in
        a close handler would otherwise leave a window that cannot be closed at
        all -- and in the windowed build there is no console to show why.
        """
        try:
            if self._tick_id is not None:
                self.root.after_cancel(self._tick_id)
                self._tick_id = None
        except Exception:
            pass
        self.root.after(60, self.root.destroy)
        try:
            self._remember_gain()
        except Exception:
            pass
        try:
            self._remember_deadzone()
        except Exception:
            pass
        try:
            self._remember_haptics()
        except Exception:
            pass
        # Stop the rumble engine before the worker closes the pad handle it borrows,
        # so the last packet the pad ever gets is a silence packet.
        try:
            self.haptics_runner.stop()
        except Exception:
            pass
        try:
            self.worker.stop()
        except Exception:
            pass


class _NullController:
    """Fallback with nominal calibration so the UI still works while disconnected."""

    def __init__(self):
        self.imu_calibration = switchpro.ImuCalibration.default()
        self.left_stick_calibration = switchpro.StickCalibration.default()
        self.right_stick_calibration = switchpro.StickCalibration.default()
        self.device_type_name = "未连接"
        self.transport = "-"
        self.firmware = (0, 0)
        self.mac_address = ""


class DemoController(_NullController):
    """Stand-in used by --demo: enough surface for the UI and the step runner."""

    def __init__(self):
        super().__init__()
        self.device_type_name = "演示模式"
        self.transport = "demo"
        self.firmware = (3, 72)
        self.mac_address = "DE:MO:00:00:00:01"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switch Pro 手柄按键核对界面")
    parser.add_argument("--forward", action="store_true",
                        help="启动时即开启转发到虚拟 DualShock 4（等同 --mode full）")
    parser.add_argument("--demo", action="store_true",
                        help="演示模式：用合成数据预览界面，不读取真实手柄")
    parser.add_argument("--topmost", action="store_true",
                        help="窗口保持最前（边看提示边按手柄时更方便）")
    parser.add_argument("--source", choices=sources.KINDS, default="auto",
                        help="输入手柄：自动 / Switch Pro / DualShock 4（有线或蓝牙）")
    parser.add_argument("--mode", choices=(MODE_OFF, MODE_FULL, MODE_MOTION,
                                           MODE_GENSHIN, MODE_DS5),
                        default=MODE_OFF,
                        help="启动时即进入某个转发模式：full 整机转发 / motion 仅体感 / "
                             "ds5 整机转发到虚拟 DualSense / genshin 原神推荐"
                             "（DS4 仅体感 + Share 当触控板）/ off 只显示数值")
    parser.add_argument("--debug-layout", action="store_true",
                        help="打印各面板尺寸（默认 1.2 秒后打印并退出）")
    parser.add_argument("--metrics-delay", type=float, default=1.2,
                        help="--debug-layout 的延迟秒数；设为负数则打印后不退出")
    args = parser.parse_args(argv)

    make_dpi_aware()
    claim_taskbar_identity()
    root = tk.Tk()
    # The ASCII product name comes first so tooling (and the task bar) can find
    # this window unambiguously.
    root.title(APP_NAME)
    apply_window_icon(root)
    root.configure(bg=BG)
    # One fixed coordinate space, laid out in 96-DPI logical pixels and then
    # multiplied by the real DPI.  A 125% display therefore gets a 125% window
    # drawn natively, rather than a 100% window bit-stretched by Windows (which
    # softens every glyph and re-resamples every anti-aliased edge).  The
    # --debug-layout report below prints req/alloc widths so clipping is
    # verifiable without eyeballing pixels.  The height covers the four cards
    # (the audio-haptics one grew when the preset dropdown became six sliders)
    # plus a couple of lines of slack for the note under the last card, which
    # wraps when it has to explain a failure.
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    width, height = int(800 * scale), int(880 * scale)
    if args.debug_layout:
        print(f"screen = {root.winfo_screenwidth()}x{root.winfo_screenheight()}  "
              f"dpi scale = {root.winfo_fpixels('1i') / 96.0:.3f}  "
              f"window = {width}x{height}", flush=True)
        print(f"app dir = {paths.app_dir()}", flush=True)
        print(f"report  = {paths.app_path('verification_report.json')}", flush=True)
        print(f"fixes   = {mapping.captured_mapping_path()}", flush=True)
    root.geometry(f"{width}x{height}+40+40")
    # Never smaller than the layout needs: below this the sliders and the note line
    # under the last card start to clip (the cards are a fixed height).
    root.minsize(int(768 * scale), int(880 * scale))

    app = App(root, forward=args.forward, demo=args.demo, topmost=args.topmost,
              scale=scale, source=args.source)
    wanted_mode = MODE_FULL if (args.forward and args.mode == MODE_OFF) else args.mode
    if wanted_mode != MODE_OFF:
        app.apply_mode(wanted_mode)
    if args.debug_layout:
        keep_running = args.metrics_delay < 0
        delay_ms = int(abs(args.metrics_delay) * 1000)

        def report() -> None:
            root.update_idletasks()
            print(f"root   {root.winfo_width()}x{root.winfo_height()}", flush=True)
            for label, widget in (("body", app.body), ("info", app.info_panel),
                                  ("footer", app.footer)):
                print(f"{label:7s}{widget.winfo_width():5d}x{widget.winfo_height():<5d} "
                      f"at ({widget.winfo_rootx()},{widget.winfo_rooty()})", flush=True)
            print("labels (clipped if required > allocated):", flush=True)
            for label, widget in (("live", app.live_label), ("motion", app.motion_label),
                                  ("worker", app.worker_label), ("note", app.share_note)):
                required = widget.winfo_reqwidth()
                allocated = widget.winfo_width()
                flag = "  <-- CLIPPED" if required > allocated + 1 else ""
                print(f"  {label:12s} req={required:4d} alloc={allocated:4d}{flag}",
                      flush=True)
            # Each control row packs several widgets side by side; report whether
            # they still fit, since a long label can push one off the edge.
            for row in app.footer.winfo_children():
                required = row.winfo_reqwidth()
                allocated = row.winfo_width()
                flag = "  <-- TOO WIDE" if required > allocated + 1 else ""
                print(f"  {row.winfo_name():12s} req={required:4d} "
                      f"alloc={allocated:4d}{flag}", flush=True)
            if not keep_running:
                root.destroy()

        root.after(delay_ms, report)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

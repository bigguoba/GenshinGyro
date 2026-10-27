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
import hidwin  # noqa: F401  (kept so the module can report device details if needed)
import mapping
import paths
import settings
import sources
import ui_theme
import switchpro
import verify_plan
import vigem

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

#: Names shown in the input-source box.  Short forms: the long descriptions did
#: not fit the narrower window and carried no information.
SOURCE_LABELS = {"auto": "自动", "switch": "Switch Pro", "ds4": "DualShock4"}
#: How the Share mapping reads in the note under the checkboxes.
SHARE_NOTE = "Share（DualShock 4）/ -（Switch Pro）"

#: The two forwarding modes are mutually exclusive.  "genshin" is still reachable
#: from the command line (--mode genshin) but no longer has an on-screen button.
MODE_OFF, MODE_FULL, MODE_MOTION, MODE_GENSHIN = "off", "full", "motion", "genshin"
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

    def __init__(self, config: mapping.MappingConfig, source: str = "auto"):
        super().__init__(daemon=True)
        self.config = config
        self.source = source
        self.states: queue.Queue = queue.Queue(maxsize=2)
        self.controller = None
        self.kind = "switch"
        self.status = "正在寻找手柄…"
        self.connected = False
        self.forwarding = False
        self.error: str | None = None
        self._client: vigem.ViGEmClient | None = None
        self._pad: vigem.DS4Controller | None = None
        self._mapper = None
        self._stop = threading.Event()
        self._want_forward = False
        self._lock = threading.Lock()

    # -- commands from the UI --------------------------------------------- #

    def set_forwarding(self, enabled: bool) -> None:
        with self._lock:
            self._want_forward = enabled

    def stop(self) -> None:
        """Ask the worker to finish; it tears the virtual pad down on the way out."""
        self._stop.set()

    # -- forwarding plumbing ---------------------------------------------- #

    def _start_forwarding(self) -> None:
        if self._pad is not None or self.controller is None:
            return
        try:
            self._client = vigem.ViGEmClient()
            self._client.connect()
            self._pad = self._client.create_ds4()
            self._pad.plug_in()
            if self.kind == "ds4":
                self._mapper = mapping.DS4ToDS4Relay(self.controller, self._pad, self.config)
            else:
                self._mapper = mapping.SwitchToDS4Mapper(self.controller, self._pad,
                                                         self.config)
            if hasattr(self._mapper, "start_streaming"):
                self._mapper.start_streaming()
            self.forwarding = True
            self.status = "已开启转发：虚拟 DualShock 4 已插入"
        except Exception as exc:  # pragma: no cover - depends on the host
            self._teardown_forwarding()
            self.status = f"转发失败：{exc}"

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
        while not self._stop.is_set():
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
            if self._stop.is_set():
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
        while not self._stop.is_set():
            state = self.controller.read_state(timeout_ms=200)
            if state is None:
                continue
            with self._lock:
                want_forward = self._want_forward
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

    def __init__(self, config: mapping.MappingConfig, source: str = "auto"):
        super().__init__(daemon=True)
        self.config = config
        self.source = source          # accepted so the UI can swap workers freely
        self.states: queue.Queue = queue.Queue(maxsize=2)
        self.controller = DemoController()
        self.status = "演示模式：没有读取真实手柄"
        self.connected = True
        self.forwarding = False
        self._stop = threading.Event()
        self._start = time.monotonic()

    def set_forwarding(self, enabled: bool) -> None:
        self.status = "演示模式不支持转发"

    def stop(self) -> None:
        """Ask the demo worker to finish (the App calls this on every worker)."""
        self._stop.set()

    def run(self) -> None:
        index = 0
        while not self._stop.is_set():
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

        self._build_layout()
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
            "死区补偿 · 俯仰")) + px(20))

        def section(title: str, top: int = ui_theme.SPACE_MD) -> tk.Frame:
            """A titled card; rows go inside the returned frame."""
            tk.Label(footer, text=title, bg=page, fg=ui_theme.TEXT_MUTED,
                     font=ui_theme.font(ui_theme.SIZE_SECTION), anchor="w").pack(
                         fill="x", padx=px(2), pady=(px(top), px(ui_theme.SPACE_XS)))
            card = ui_theme.RoundedFrame(footer, padding=(px(14), px(4)), radius=px(12))
            card.pack(fill="x")
            return card.content

        def row(parent: tk.Frame, label: str | None, first: bool = False) -> tk.Frame:
            """One row inside a section card, with a hairline above it."""
            if not first:
                tk.Frame(parent, bg=ui_theme.BORDER, height=max(1, px(1))).pack(fill="x")
            inside = tk.Frame(parent, bg=ui_theme.SURFACE, height=int(34 * self.scale))
            inside.pack(fill="x")
            inside.pack_propagate(False)
            if label is not None:
                holder = tk.Frame(inside, bg=ui_theme.SURFACE, width=label_column)
                holder.pack(side="left", fill="y")
                holder.pack_propagate(False)
                tk.Label(holder, text=label, bg=ui_theme.SURFACE, fg=ui_theme.TEXT,
                         font=label_font, anchor="w").pack(side="left", fill="y")
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

        self.share_note = tk.Label(
            footer, text=f"两种模式都会把 {SHARE_NOTE} 用作触控板按下",
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


    def apply_mode(self, mode: str) -> None:
        """The single place that changes the forwarding mode.

        Two mutually exclusive modes, and both of them include the Share mapping
        (Share on a DualShock 4, ``-`` on a Switch Pro becomes the touchpad click),
        because the pad's own touchpad button is unreliable over Bluetooth.
        """
        if mode not in (MODE_OFF, MODE_FULL, MODE_MOTION, MODE_GENSHIN):
            mode = MODE_OFF
        genshin = mode == MODE_GENSHIN
        self.mode = mode
        self.config.motion_only = mode in (MODE_MOTION, MODE_GENSHIN)
        # Share → touchpad is part of both modes now; off only when nothing is
        # forwarded at all.
        self.config.share_as_touchpad = mode != MODE_OFF

        # Keep the widgets in step; set() does not re-trigger the commands.
        self.forward_var.set(mode == MODE_FULL)
        self.motion_only_var.set(mode == MODE_MOTION)

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

        self._set_forwarding(mode != MODE_OFF)
        self._update_mode_hint()
        self._remember_mode()

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
            MODE_FULL: "完整转发：按键、摇杆、体感都会转发到虚拟手柄" + share_note + "。",
            MODE_MOTION: "仅体感：虚拟手柄只发体感，按键摇杆由实体手柄负责" + share_note + "。",
            MODE_GENSHIN: "仅体感 + 输入固定为 DualShock 4（命令行 --mode genshin 专用）"
                          + share_note + "。",
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
        try:
            previous.stop()
            previous.join(timeout=2.0)
        except Exception:
            pass
        self.worker = (DemoWorker(self.config, source=chosen)
                       if isinstance(previous, DemoWorker)
                       else ControllerWorker(self.config, source=chosen))
        self.worker.start()
        if self.mode != MODE_OFF:
            self._set_forwarding(True)
        return True




    def toggle_motion_only(self) -> None:
        """Checkbox 2: motion only, so the physical pad keeps buttons and sticks."""
        self.apply_mode(MODE_MOTION if self.motion_only_var.get() else MODE_OFF)





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
    parser.add_argument("--mode", choices=(MODE_OFF, MODE_FULL, MODE_MOTION, MODE_GENSHIN),
                        default=MODE_OFF,
                        help="启动时即进入某个转发模式：full 整机转发 / motion 仅体感 / "
                             "genshin 原神推荐（DS4 仅体感 + Share 当触控板）/ off 只显示数值")
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
    # verifiable without eyeballing pixels.
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    width, height = int(800 * scale), int(556 * scale)
    if args.debug_layout:
        print(f"screen = {root.winfo_screenwidth()}x{root.winfo_screenheight()}  "
              f"dpi scale = {root.winfo_fpixels('1i') / 96.0:.3f}  "
              f"window = {width}x{height}", flush=True)
        print(f"app dir = {paths.app_dir()}", flush=True)
        print(f"report  = {paths.app_path('verification_report.json')}", flush=True)
        print(f"fixes   = {mapping.captured_mapping_path()}", flush=True)
    root.geometry(f"{width}x{height}+40+40")
    root.minsize(int(760 * scale), int(520 * scale))

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

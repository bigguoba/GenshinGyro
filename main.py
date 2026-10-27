"""Switch Pro Controller -> virtual DualShock 4 bridge.

Reads a Nintendo Switch Pro Controller (USB or Bluetooth) and forwards its
buttons, sticks and 6-axis motion to a virtual DualShock 4 created through
ViGEmBus.  There is deliberately no button-remapping or macro layer.

Usage:
    python main.py                 # run the bridge
    python main.py --list          # list HID devices and exit
    python main.py --verbose       # log every controller report
    python main.py --self-test     # verify the ViGEm output path and exit
    python main.py --device PATH   # use a specific HID interface by path
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

import dualshock4
import hidwin
import mapping
import selftest
import sources
import switchpro
import version
import vigem
import viiper

POLL_INTERVAL_S = 0.02
RECONNECT_INTERVAL_S = 2.0
READ_TIMEOUT_MS = 250


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _prepare_console() -> None:
    """Handle the windowed build, where there is no console at all.

    The shipped exe is built without a console so that double-clicking it opens
    only the graphical window.  The console modes (``--cli`` and friends) still
    need somewhere to print, so they attach to the console that launched them --
    the .bat window -- or create one, and then rebind the streams to it.  Anything
    else gets a null sink so a stray ``print`` can never crash the program.
    """
    # Only touch anything when the build has no usable streams (the windowed
    # build sets them to None).  A normal console run keeps its streams, so
    # "exe --list > file" keeps redirecting the way the shell expects.
    if sys.platform == "win32" and (sys.stdout is None or sys.stderr is None):
        wants_console = "--cli" in sys.argv[1:] or "-h" in sys.argv[1:] \
            or "--help" in sys.argv[1:]
        has_console = False
        try:
            kernel32 = ctypes.WinDLL("kernel32")
            # A console may already be inherited from the .bat that launched us;
            # in that case reuse it instead of popping up a second window.
            has_console = bool(kernel32.GetConsoleWindow())
            if not has_console:
                has_console = bool(kernel32.AttachConsole(-1))   # parent process
            if not has_console and wants_console:
                has_console = bool(kernel32.AllocConsole())
        except (OSError, AttributeError):
            pass
        if has_console:
            try:
                sys.stdout = open("CONOUT$", "w", buffering=1, errors="replace")
                sys.stderr = open("CONOUT$", "w", buffering=1, errors="replace")
                sys.stdin = open("CONIN$", "r", errors="replace")
            except OSError:
                pass
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def log_banner() -> None:
    log(f"{version.version_string()} 已启动")


# --------------------------------------------------------------------------- #
# Device listing
# --------------------------------------------------------------------------- #

def list_devices() -> int:
    print(f"{'VID:PID':>10}  {'usage':>10}  {'in':>4} {'out':>4}  {'manufacturer':<28} product")
    for info in hidwin.enumerate_hid():
        vid = info.get("vid")
        pid = info.get("pid")
        vidpid = f"{vid:04X}:{pid:04X}" if vid is not None else "  ?  :  ? "
        usage = f"0x{info.get('usage_page', 0):04X}:{info.get('usage', 0):04X}"
        flag = ""
        if vid == switchpro.SWITCH_PRO_VID and pid == switchpro.SWITCH_PRO_PID:
            flag = "  <-- Switch Pro Controller"
        print(
            f"{vidpid:>10}  {usage:>10}  {str(info.get('input_len')):>4} "
            f"{str(info.get('output_len')):>4}  {info.get('manufacturer', '')[:28]:<28} "
            f"{info.get('product', '')}{flag}"
        )
    return 0


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #

class Bridge:
    """Owns the virtual pad and one controller session.

    Two input sources are supported, both ending in the same virtual pad:

    * a Switch Pro Controller (buttons/sticks/gyro are mapped across device
      families, see :mod:`mapping`);
    * a physical DualShock 4, wired or Bluetooth.  Here nothing is mapped or
      rescaled -- the pad is relayed field for field, and the only thing that
      changes is the *report format*, from the Bluetooth ``0x11`` report (which
      games such as Genshin Impact do not read motion from) to the wired ``0x01``
      report the virtual pad always emits.
    """

    def __init__(self, config: mapping.MappingConfig, verbose: bool = False,
                 device_path: str | None = None, source: str = "auto",
                 no_virtual_pad: bool = False, backend: str = "vigem"):
        self.config = config
        self.verbose = verbose
        self.device_path = device_path
        self.source = source
        self.no_virtual_pad = no_virtual_pad
        self.backend = backend
        self._last_verbose = 0.0
        self._reports = 0

    # -- choosing and opening the input ----------------------------------- #

    def _open_source(self):
        """Return ``("switch"|"ds4", controller)`` for the first usable source."""
        return sources.open_source(self.source, self.device_path, log=log)

    def _make_mapper(self, kind: str, controller, pad: vigem.DS4Controller):
        if kind == "ds4":
            return mapping.DS4ToDS4Relay(controller, pad, self.config)
        return mapping.SwitchToDS4Mapper(controller, pad, self.config)

    # -- one controller session ------------------------------------------ #

    def run_session(self, client: vigem.ViGEmClient) -> None:
        kind, controller = self._open_source()
        try:
            log(f"controller: {controller.describe()}")

            if self.no_virtual_pad:
                # Diagnostic mode: no virtual pad at all.  Useful to find out
                # whether a game reads the physical pad's own motion once it has
                # been switched to its full report -- in that case no relay and
                # no HidHide are needed at all.
                log("no virtual DualShock 4 will be created (--no-virtual-pad); "
                    "only keeping the pad awake and in its full-report mode")
                self.pump(kind, controller, None)
                return

            if self.backend == "dualsense":
                if kind != "switch":
                    log("--backend dualsense 目前只支持 Switch Pro 输入（实体 DS4 的 1:1 "
                        "中继仍走 --backend vigem）")
                    return
                pad = viiper.DualSensePad(log=log)
            else:
                pad = client.create_ds4()
            pad.plug_in()
            if self.backend == "dualsense":
                log("virtual DualSense plugged in (VID_054C/PID_0CE6 via VIIPER)")
            else:
                log("virtual DualShock 4 plugged in (VID_054C/PID_05C4)")
            if self.config.motion_only:
                log("motion-only mode: the virtual pad reports gyro/accelerometer "
                    "and leaves every button, stick and trigger neutral")
            mapper = self._make_mapper(kind, controller, pad)
            try:
                # A DS4 relay renders the motion itself on a steady high-rate
                # cadence, so start that before pumping input into it.
                if hasattr(mapper, "start_streaming"):
                    mapper.start_streaming()
                self.pump(kind, controller, mapper)
            finally:
                if hasattr(mapper, "stop_streaming"):
                    mapper.stop_streaming()
                if isinstance(pad, viiper.DualSensePad):
                    pad.unplug()
                    log(f"DualSense 反馈统计：{getattr(pad, 'frame_summary', '')}"
                        f"；触觉帧 {pad.haptics_frames} 个")
                else:
                    # Leaving an unmapped pad plugged in would look like a stuck
                    # controller to games, so remove it with the session.
                    pad.unplug()
                    log("virtual DualShock 4 removed")
        finally:
            controller.close()

    def pump(self, kind: str, controller, mapper) -> None:
        if mapper is None:
            log("watching the pad only (no virtual pad); Ctrl+C to stop")
        elif kind == "ds4":
            log("relaying buttons, sticks and gyro/accelerometer 1:1 as a wired "
                "DS4 report; Ctrl+C to stop")
        else:
            log("forwarding buttons, sticks and gyro/accelerometer; Ctrl+C to stop")
        last_battery_log = 0.0
        while True:
            state = controller.read_state(timeout_ms=READ_TIMEOUT_MS)
            if state is None:
                # No reports: the controller went to sleep, was unplugged, or is
                # still switching input mode.  Keep waiting; the caller decides
                # when to give up based on errors.
                continue
            if mapper is not None:
                mapper.push_state(state)
            self._reports += 1

            if kind == "ds4":
                warning = controller.warn_if_static()
                if warning:
                    log(warning)
                retry = controller.ensure_motion()
                if retry:
                    log(retry)

            now = time.monotonic()
            if self.verbose and now - self._last_verbose > 0.25:
                self._last_verbose = now
                print("  " + self._describe_state(kind, controller, state), flush=True)
            elif now - last_battery_log > 60.0:
                last_battery_log = now
                log(f"running: {self._reports} reports forwarded, "
                    + self._battery_text(kind, state))

    @staticmethod
    def _battery_text(kind: str, state) -> str:
        if kind == "ds4":
            return f"battery {state.battery_level}/10"
        return f"battery {state.battery_level_8}/8"

    @staticmethod
    def _describe_state(kind: str, controller, state) -> str:
        buttons = "+".join(state.pressed_names) or "-"
        if kind == "ds4":
            gyro = tuple(value / 16.0 for value in state.gyro)
            accel = tuple(value / 8192.0 for value in state.accel)
            return (f"report 0x{state.report_id:02X} sticks={state.sticks} "
                    f"gyro=({gyro[0]:7.1f},{gyro[1]:7.1f},{gyro[2]:7.1f}) dps "
                    f"accel=({accel[0]:6.3f},{accel[1]:6.3f},{accel[2]:6.3f}) g "
                    f"batt={state.battery_level}/10 [{buttons}]")
        sample = state.imu[-1] if state.imu else None
        motion = ""
        if sample is not None:
            dps = controller.imu_calibration.to_dps(sample.gyro)
            motion = f" gyro=({dps[0]:7.1f},{dps[1]:7.1f},{dps[2]:7.1f}) dps"
        sticks = state.left_stick + state.right_stick
        return (f"timer={state.timer:3d} batt={state.battery_level_8}/8 "
                f"sticks={sticks} buttons={buttons}{motion}")

    # -- top level -------------------------------------------------------- #

    def run(self) -> int:
        log_banner()
        if self.backend == "dualsense":
            # A virtual DualSense comes from VIIPER, which is a separate local
            # service -- ViGEmBus is not involved at all on this path.
            log("using the VIIPER backend: a virtual DualSense will be created")
            return self._run_loop(None)
        log("connecting to ViGEmBus...")
        with vigem.ViGEmClient() as client:
            log("connected to ViGEmBus")
            return self._run_loop(client)

    def _run_loop(self, client) -> int:
        last_message = None
        last_logged = 0.0
        while True:
            message = None
            try:
                self.run_session(client)
            except switchpro.DeviceGoneError as exc:
                message = f"controller disconnected ({exc}); waiting for it to come back"
            except dualshock4.DS4GoneError as exc:
                message = f"controller disconnected ({exc}); waiting for it to come back"
            except (switchpro.SwitchProError, dualshock4.DS4Error,
                    sources.NoControllerError) as exc:
                message = f"controller error: {exc}"
            except viiper.ViiperError as exc:
                message = f"VIIPER error: {exc}"
            except OSError as exc:
                message = f"device error: {exc}"
            # Repeat a stable message only occasionally, so the console stays
            # readable while a controller is unplugged.
            now = time.monotonic()
            if message != last_message or now - last_logged > 30.0:
                log(message)
                last_message = message
                last_logged = now
            time.sleep(RECONNECT_INTERVAL_S)


def self_test() -> int:
    return selftest.run()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Forward a Nintendo Switch Pro Controller to a virtual DualShock 4 "
                    "through ViGEmBus (buttons, sticks and 6-axis motion).",
    )
    parser.add_argument("--list", action="store_true", help="list HID devices and exit")
    parser.add_argument("--version", action="version",
                        version=f"{version.version_string()}\n{version.__summary__}",
                        help="print the version and exit")
    parser.add_argument("--self-test", action="store_true",
                        help="plug in a virtual DS4, verify the report layout, then exit")
    parser.add_argument("--gui", action="store_true",
                        help="open the graphical window (this is also what a plain "
                             "double-click does)")
    parser.add_argument("--cli", action="store_true",
                        help="run the console bridge instead of opening the window; "
                             "needed when passing console options")
    parser.add_argument("--demo", action="store_true",
                        help="with --gui: preview the window with synthetic data")
    parser.add_argument("--topmost", action="store_true",
                        help="with --gui: keep the window above others")
    parser.add_argument("--device", metavar="PATH",
                        help="HID interface path of the controller (default: auto-detect)")
    parser.add_argument("--source", choices=("auto", "switch", "ds4"), default="auto",
                        help="which controller to read: a Switch Pro Controller "
                             "(switch), a physical DualShock 4 wired or Bluetooth "
                             "(ds4), or whichever is connected (auto, default). "
                             "The DS4 is relayed 1:1 as a wired DS4 report, which is "
                             "what makes Bluetooth motion data usable in games that "
                             "only read the wired format.")
    parser.add_argument("--verbose", action="store_true", help="log every controller report")
    parser.add_argument("--gyro-gain", type=float, default=1.0,
                        help="multiply forwarded rotation rates (default 1.0)")
    parser.add_argument("--accel-gain", type=float, default=1.0,
                        help="multiply forwarded acceleration (default 1.0)")
    parser.add_argument("--gyro-deadzone", type=float, default=0.0, metavar="DPS",
                        help="treat rotation slower than this as zero (default 0)")
    parser.add_argument("--stick-deadzone", type=float, default=0.0, metavar="FRACTION",
                        help="ignore this fraction of stick travel around the centre")
    parser.add_argument("--no-imu-interpolation", action="store_true",
                        help="send one DS4 report per controller report instead of "
                             "one per IMU sample")
    parser.add_argument("--motion-only", action="store_true",
                        help="the virtual pad reports motion only; buttons, sticks "
                             "and triggers stay neutral (lets a physically connected "
                             "pad keep supplying them). Share is mapped to the "
                             "touchpad click unless --no-share-touchpad is given")
    parser.add_argument("--no-share-touchpad", action="store_true",
                        help="do not turn Share into a touchpad click "
                             "(--motion-only switches it on by default)")
    parser.add_argument("--share-touchpad", action="store_true",
                        help="turn Share into a touchpad click as well, in any mode "
                             "(remapped, not duplicated: Share itself is not forwarded)")
    parser.add_argument("--no-motion-smoothing", action="store_true",
                        help="same as --motion-pacing raw")
    parser.add_argument("--motion-pacing", choices=mapping.MOTION_PACING_CHOICES,
                        default=mapping.MOTION_PACING_DEFAULT,
                        help="how relayed motion is paced: 'hardware' reproduces a "
                             "real wired pad (a report every 4.096 ms, timestamp "
                             "+768, the newest IMU sample held over two reports) -- "
                             "games integrate yaw per report, so this is the one "
                             "that keeps aiming correct; 'smooth' interpolates at a "
                             "higher rate for a pretty waveform, but yaw then "
                             "integrates far too much rotation and the picture "
                             "jumps; 'raw' sends one report per input report")
    parser.add_argument("--motion-output-hz", type=float, default=400.0,
                        help="report rate used by --motion-pacing smooth (default 400)")
    parser.add_argument("--motion-extrapolate", action="store_true",
                        help="with --motion-pacing smooth: guess across a Bluetooth "
                             "burst gap instead of waiting for the next one (about "
                             "12 ms less latency, small spikes on a waveform)")
    parser.add_argument("--report-interval-ms", type=float,
                        default=mapping.DS4_NATIVE_REPORT_INTERVAL_S * 1000.0,
                        help="report interval used by --motion-pacing hardware "
                             "(default 4.096 ms, the real pad's step; the timestamp "
                             "step follows it automatically).  Note this changes how "
                             "often the state is refreshed, not the rate a reader "
                             "sees: the emulated device answers every read with its "
                             "latest state, whereas real hardware is capped at "
                             "250 Hz by its USB endpoint")
    parser.add_argument("--no-virtual-pad", action="store_true",
                        help="do not create a virtual pad at all: just keep the "
                             "controller awake and in its full-report mode "
                             "(diagnostic: checks whether a game reads the physical "
                             "pad's own motion)")
    parser.add_argument("--backend", choices=("vigem", "dualsense"), default="vigem",
                        help="which virtual pad to create: vigem (DualShock 4, default) "
                             "or dualsense (a real DualSense through the VIIPER "
                             "service; needed for games that only send audio haptics "
                             "to a DualSense).  dualsense needs a Switch Pro input")
    return parser


#: Options only the window understands.  Passing one of these means the user wants
#: the GUI even without --gui, so "exe --mode genshin" opens the window instead of
#: being rejected by the console parser.
GUI_ONLY_OPTIONS = frozenset({
    "--gui", "--demo", "--topmost", "--debug-layout", "--metrics-delay",
    "--mode", "--forward",
})


def gui_wanted(raw: list[str]) -> bool:
    """Does this command line ask for the window?"""
    if not raw:
        return True                     # plain double-click
    return any(item.split("=", 1)[0] in GUI_ONLY_OPTIONS for item in raw)


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    raw = list(sys.argv[1:] if argv is None else argv)

    # Double-clicking the exe (no arguments at all) opens the window: that is what
    # an ordinary user expects, and every feature is reachable from there.  The
    # console bridge stays available via --cli, or by passing console options such
    # as --source/--motion-only without any window-only option.
    if gui_wanted(raw):
        import gui  # imported lazily so the console modes need no display

        return gui.main([item for item in raw if item != "--gui"])

    args = build_parser().parse_args(raw)

    if args.list:
        return list_devices()
    if args.self_test:
        return self_test()

    config = mapping.MappingConfig(
        gyro_gain=args.gyro_gain,
        accel_gain=args.accel_gain,
        gyro_deadzone_dps=args.gyro_deadzone,
        stick_deadzone=args.stick_deadzone,
        interpolate_imu=not args.no_imu_interpolation,
        motion_only=args.motion_only,
        # Motion-only keeps its documented behaviour (Share becomes the touchpad
        # click), while full relay does not unless --share-touchpad asks for it.
        share_as_touchpad=((args.motion_only or args.share_touchpad)
                           and not args.no_share_touchpad),
        motion_pacing=("raw" if args.no_motion_smoothing else args.motion_pacing),
        motion_output_hz=args.motion_output_hz,
        motion_extrapolate=args.motion_extrapolate,
        ds4_report_interval_s=max(1e-4, args.report_interval_ms / 1000.0),
    )
    bridge = Bridge(config, verbose=args.verbose, device_path=args.device,
                    source=args.source, no_virtual_pad=args.no_virtual_pad,
                    backend=args.backend)
    try:
        return bridge.run()
    except KeyboardInterrupt:
        log("stopped")
        return 0
    except vigem.ViGEmError as exc:
        log(f"ViGEmBus error: {exc}")
        print(
            "\nThe virtual gamepad could not be created. Check that:\n"
            "  * the ViGEmBus driver is installed (https://github.com/nefarius/ViGEmBus/releases),\n"
            "  * vendor\\ViGEmClient.dll exists next to this program.",
            file=sys.stderr,
        )
        return 1
    except sources.NoControllerError as exc:
        log(f"no controller: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

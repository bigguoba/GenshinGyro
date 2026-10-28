"""Regression tests for the reworked GUI (no step-by-step panel any more).

Drives the real Tk app object with the window withdrawn, in demo mode so no
hardware is needed: the controls must drive the mapping config exactly, the
one-click Genshin button must set up the whole configuration, and the gyro
sensitivity slider and its text box must stay in sync and clamped.

Run:  python tools\\test_gui_flow.py
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui  # noqa: E402
import settings  # noqa: E402
import sources  # noqa: E402

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


def gui_routing(argv: list[str]):
    """Which arguments does ``main`` hand to the GUI (if any)?"""
    import types

    import main as main_module

    seen: dict = {}
    fake = types.ModuleType("gui")
    fake.main = lambda args: (seen.setdefault("args", args), 0)[1]
    saved = sys.modules.get("gui")
    sys.modules["gui"] = fake
    try:
        code = main_module.main(argv)
    finally:
        if saved is not None:
            sys.modules["gui"] = saved
        else:
            sys.modules.pop("gui", None)
    return seen.get("args"), code


def set_gain_entry(app, text: str) -> None:
    app.gain_entry.delete(0, "end")
    app.gain_entry.insert(0, text)
    app._on_gain_entry()


def pump(root, seconds: float = 1.0) -> None:
    """Run the Tk event loop until the window is destroyed (or time runs out)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            root.update()
        except tk.TclError:
            return                          # interpreter gone: window closed
        time.sleep(0.02)


def run_tests(app) -> None:
    print("\n[1] 默认状态")
    check("默认不勾完整转发", app.forward_var.get(), False)
    check("默认不勾仅体感", app.motion_only_var.get(), False)
    check("默认不转发", app.mode, gui.MODE_OFF)
    check("默认不开启 Share 映射", app.config.share_as_touchpad, False)
    check("灵敏度默认 1.0", app.config.gyro_gain, 1.0)
    check("灵敏度输入框显示 1.0", app.gain_entry.get(), "1.0")
    check("灵敏度上限 5.0", float(app.gain_scale.cget("to")), 5.0)
    check("灵敏度下限 0.0", float(app.gain_scale.cget("from")), 0.0)
    check("步进 0.1", float(app.gain_scale.cget("resolution")), gui.GAIN_STEP)

    print("\n[2] 输入手柄下拉框：自动 / Switch Pro / DualShock4")
    check("三个选项", list(app.source_box.cget("values")),
          ["自动", "Switch Pro", "DualShock4"])
    check("默认显示自动", app.source_var.get(), "自动")

    print("\n[2b] 完整转发：勾上后 Share 也会被映射")
    app.forward_var.set(True)
    app.toggle_forward()
    check("模式 = 完整转发", app.mode, gui.MODE_FULL)
    check("不是仅体感", app.config.motion_only, False)
    check("自动开启 Share 映射", app.config.share_as_touchpad, True)
    check("仅体感勾选框被取消", app.motion_only_var.get(), False)
    check("状态条提示里写了 Share", "Share" in app.worker_label.cget("text"), True)

    print("\n[2c] 仅体感：同样带 Share 映射，且与完整转发互斥")
    app.motion_only_var.set(True)
    app.toggle_motion_only()
    check("模式 = 仅体感", app.mode, gui.MODE_MOTION)
    check("仅体感开启", app.config.motion_only, True)
    check("同样开启 Share 映射", app.config.share_as_touchpad, True)
    check("完整转发勾选框被取消", app.forward_var.get(), False)
    app.forward_var.set(True)
    app.toggle_forward()
    check("再勾完整转发 -> 仅体感被取消", app.motion_only_var.get(), False)
    check("模式回到完整转发", app.mode, gui.MODE_FULL)
    check("Share 依旧映射", app.config.share_as_touchpad, True)

    print("\n[2d] 两个都取消 -> 完全不转发")
    app.forward_var.set(False)
    app.toggle_forward()
    check("模式 = 关闭", app.mode, gui.MODE_OFF)
    check("Share 映射随之关闭", app.config.share_as_touchpad, False)
    check("仅体感也是关的", app.motion_only_var.get(), False)

    print("\n[2e] 已移除的控件")
    check("没有 Share 勾选框", hasattr(app, "share_check"), False)
    check("没有 share_var", hasattr(app, "share_var"), False)
    check("没有「原神推荐」按钮", hasattr(app, "recommended_button"), False)
    check("没有按钮提示文字", hasattr(app, "recommended_hint"), False)
    check("没有手柄示意图", hasattr(app, "view"), False)
    check("没有旧标题控件", hasattr(app, "title_label"), False)

    print("\n[2f] 说明文字")
    note = app.share_note.cget("text")
    check("说明里提到 Share", "Share" in note, True)
    check("说明里提到 -", "-" in note, True)
    check("说明里提到触控板", "触控板" in note, True)

    print("\n[2g] 转发到虚拟 DualSense（与另外两个转发模式互斥）")
    check("有 DS5 开关", hasattr(app, "ds5_var"), True)
    check("默认关", app.ds5_var.get(), False)
    forwarded = []
    app.worker.set_forwarding = lambda enabled: forwarded.append(enabled)

    app.ds5_var.set(True)
    app.toggle_ds5()
    check("模式 = ds5", app.mode, gui.MODE_DS5)
    check("完整转发被取消", app.forward_var.get(), False)
    check("仅体感被取消", app.motion_only_var.get(), False)
    check("按键照转（不是仅体感）", app.config.motion_only, False)
    check("Share 映射照旧开着", app.config.share_as_touchpad, True)
    check("worker 换成 DualSense 后端", app.worker.backend, "dualsense")
    check("要求开启转发", forwarded[-1], True)
    check("提示里写了虚拟 DualSense",
          "DualSense" in app.worker_label.cget("text"), True)
    check("模式被记住", settings.mode(), "ds5")

    app.forward_var.set(True)
    app.toggle_forward()
    check("再点完整转发 -> DS5 被取消", app.ds5_var.get(), False)
    check("后端换回 vigem", app.worker.backend, "vigem")
    check("模式 = full", app.mode, gui.MODE_FULL)

    app.ds5_var.set(True)
    app.toggle_ds5()
    app.motion_only_var.set(True)
    app.toggle_motion_only()
    check("再点仅体感 -> DS5 被取消", app.ds5_var.get(), False)
    check("模式 = motion", app.mode, gui.MODE_MOTION)
    app.motion_only_var.set(False)
    app.toggle_motion_only()
    check("全部取消 -> off", app.mode, gui.MODE_OFF)
    check("off 时后端回到 vigem", app.worker.backend, "vigem")

    print("\n[3] gyro sensitivity: slider and entry stay in sync")
    app.gain_scale.set(2.5)
    app._on_gain_scale()
    check("slider 2.5 -> config 2.5", app.config.gyro_gain, 2.5)
    check("entry shows 2.5", app.gain_entry.get(), "2.5")

    set_gain_entry(app, "0.3")
    check("entry 0.3 -> config 0.3", app.config.gyro_gain, 0.3)
    check("slider follows to 0.3", round(float(app.gain_scale.get()), 1), 0.3)

    set_gain_entry(app, "99")
    check("99 is clamped to 5.0", app.config.gyro_gain, 5.0)
    set_gain_entry(app, "0")
    check("0 stays 0 (motion off)", app.config.gyro_gain, 0.0)
    set_gain_entry(app, "-3")
    check("negative clamps to 0.0", app.config.gyro_gain, 0.0)
    set_gain_entry(app, "abc")
    check("garbage keeps the previous value", app.config.gyro_gain, gui.GAIN_MIN)
    set_gain_entry(app, "1.06")
    check("rounded to one decimal (1.06 -> 1.1)", app.config.gyro_gain, 1.1)
    check("entry echoes one decimal", app.gain_entry.get(), "1.1")
    set_gain_entry(app, "5")
    check("upper bound itself works", app.config.gyro_gain, 5.0)
    check("shows 5.0", app.gain_entry.get(), "5.0")

    print("\n[4] the step-by-step panel is gone")
    check("no start button", hasattr(app, "start_button"), False)
    check("no step list", hasattr(app, "list_text"), False)
    check("no instruction label", hasattr(app, "instruction"), False)
    check("no progress label", hasattr(app, "progress_label"), False)
    check("no right-hand panel", hasattr(app, "right_panel"), False)
    check("no auto-advance checkbox", hasattr(app, "auto_var"), False)

    print("\n[5] kept controls")
    check("input source combo", hasattr(app, "source_box"), True)
    check("forwarding checkbox", hasattr(app, "forward_var"), True)
    check("motion-only checkbox", hasattr(app, "motion_only_var"), True)
    check("sensitivity slider", hasattr(app, "gain_scale"), True)
    check("sensitivity entry", hasattr(app, "gain_entry"), True)
    check("live readout", hasattr(app, "live_label"), True)
    check("gyro readout", hasattr(app, "motion_label"), True)
    check("status hint", hasattr(app, "worker_label"), True)
    check("share note", hasattr(app, "share_note"), True)

    print("\n[6] 双击 exe（无参数）打开界面；--cli 仍走控制台")
    args, code = gui_routing([])
    check("无参数 -> 交给 GUI", args, [])
    check("无参数 -> 退出码 0", code, 0)
    args, _ = gui_routing(["--gui", "--demo"])
    check("--gui 会把其余参数转给 GUI", args, ["--demo"])
    import main as main_module
    parsed = main_module.build_parser().parse_args(["--cli"])
    check("--cli 是合法参数", parsed.cli, True)
    check("无参数 -> 要窗口", main_module.gui_wanted([]), True)
    check("--mode genshin -> 要窗口（不是报错）",
          main_module.gui_wanted(["--mode", "genshin"]), True)
    check("--mode=genshin 这种写法也算",
          main_module.gui_wanted(["--mode=genshin"]), True)
    check("--topmost -> 要窗口", main_module.gui_wanted(["--topmost"]), True)
    check("--cli -> 不要窗口", main_module.gui_wanted(["--cli"]), False)
    check("--source ds4 --motion-only -> 不要窗口（走控制台）",
          main_module.gui_wanted(["--source", "ds4", "--motion-only"]), False)
    args, _ = gui_routing(["--mode", "genshin"])
    check("--mode 会原样转给 GUI", args, ["--mode", "genshin"])

    print("\n[7] 关窗（点了叉必须能关掉）")
    for worker_cls in (gui.ControllerWorker, gui.DemoWorker):
        check(f"{worker_cls.__name__} 提供 stop()",
              hasattr(worker_cls, "stop"), True)
        check(f"{worker_cls.__name__} 提供 set_forwarding()",
              hasattr(worker_cls, "set_forwarding"), True)
        check(f"{worker_cls.__name__} 提供 join()（切换输入源时要等它结束）",
              hasattr(worker_cls, "join"), True)
    check("窗口已绑定关闭处理程序",
          bool(app.root.protocol("WM_DELETE_WINDOW")), True)
    started = app.worker.is_alive()
    app._on_close()
    pump(app.root)
    try:
        still_open = app.root.winfo_exists() != 0
    except tk.TclError:
        still_open = False                 # destroyed entirely
    check("点了叉之后窗口确实关闭（且不再抛异常）", still_open, False)
    check("关闭前 worker 是在运行的（说明确实验证过）", started, True)

    print("\n[8] 灵敏度记忆（关掉再打开要记得）")
    check("关窗时已写入设置文件", os.path.isfile(settings.settings_path()), True)
    check("关窗前是 5.0，文件里也记成 5.0", settings.gyro_gain(), 5.0)

    reopened = tk.Tk()
    reopened.withdraw()
    app2 = gui.App(reopened, demo=True)
    try:
        check("重新打开后配置里是 5.0", app2.config.gyro_gain, 5.0)
        check("重新打开后输入框显示 5.0", app2.gain_entry.get(), "5.0")
        check("重新打开后滑块也在 5.0", round(float(app2.gain_scale.get()), 1), 5.0)

        set_gain_entry(app2, "0.8")
        check("改成 0.8", app2.config.gyro_gain, 0.8)
        check("松开滑块/回车就会记住", settings.gyro_gain(), 0.8)

        # Change without committing, then close: the close handler itself must save.
        app2.config.gyro_gain = 3.3
        settings.set_gyro_gain(1.0)            # make the file stale again
        app2._on_close()
        pump(reopened)
        check("关窗会保存当前灵敏度（无需手动）", settings.gyro_gain(), 3.3)
    finally:
        try:
            app2.worker.stop()
        except Exception:
            pass
        try:
            reopened.destroy()
        except tk.TclError:
            pass


    print("\n[9] 死区补偿：三个独立滑块（俯仰 / 偏航 / 滚转）")
    # A fresh window: section [7] closed the first one on purpose.
    fourth = tk.Tk()
    fourth.withdraw()
    app4 = gui.App(fourth, demo=True)
    try:
        for axis in settings.DEADZONE_AXES:
            check(f"有 {axis} 滑块", axis in app4.deadzone_scales, True)
            check(f"有 {axis} 输入框", axis in app4.deadzone_entries, True)
            check(f"{axis} 默认 0.0", float(app4.deadzone_vars[axis].get()), 0.0)
            check(f"{axis} 默认不补偿",
                  app4.config.motion_deadzone_dps[settings.DEADZONE_AXES.index(axis)], 0.0)
        check("滑块范围 0 – 20",
              (float(app4.deadzone_scales["pitch"].cget("from")),
               float(app4.deadzone_scales["pitch"].cget("to"))),
              (settings.DEADZONE_MIN, settings.DEADZONE_MAX))
        check("三个滑块配色与灵敏度滑块一致",
              [str(app4.deadzone_scales[a].cget("troughcolor"))
               for a in settings.DEADZONE_AXES],
              [str(app4.gain_scale.cget("troughcolor"))] * 3)

        app4.deadzone_vars["pitch"].set(2.5)
        app4._on_deadzone_scale("pitch")
        check("俯仰 2.5 只改俯仰", app4.config.motion_deadzone_dps, (2.5, 0.0, 0.0))
        check("输入框跟着显示 2.5", app4.deadzone_entries["pitch"].get(), "2.5")
        app4.deadzone_vars["yaw"].set(1.0)
        app4._on_deadzone_scale("yaw")
        app4.deadzone_vars["roll"].set(0.5)
        app4._on_deadzone_scale("roll")
        check("三轴各自独立", app4.config.motion_deadzone_dps, (2.5, 1.0, 0.5))

        app4.deadzone_entries["pitch"].delete(0, "end")
        app4.deadzone_entries["pitch"].insert(0, "99")
        app4._on_deadzone_entry("pitch")
        check("超上限夹到 5.0", app4.config.motion_deadzone_dps[0], 5.0)
        app4.deadzone_entries["pitch"].delete(0, "end")
        app4.deadzone_entries["pitch"].insert(0, "-3")
        app4._on_deadzone_entry("pitch")
        check("负数夹到 0.0", app4.config.motion_deadzone_dps[0], 0.0)
        app4.deadzone_entries["pitch"].delete(0, "end")
        app4.deadzone_entries["pitch"].insert(0, "abc")
        app4._on_deadzone_entry("pitch")
        check("非法输入保留原值", app4.config.motion_deadzone_dps[0], 0.0)
        app4.deadzone_entries["pitch"].delete(0, "end")
        app4.deadzone_entries["pitch"].insert(0, "1.06")
        app4._on_deadzone_entry("pitch")
        check("精度到小数点后一位（1.06 -> 1.1）", app4.config.motion_deadzone_dps[0], 1.1)

        print("\n[9b] 死区补偿也会被记住")
        settings.set_motion_deadzone((0.0, 0.0, 0.0))
        app4._remember_deadzone()
        check("保存当前三轴（1.1 / 1.0 / 0.5）",
              settings.motion_deadzone(), (1.1, 1.0, 0.5))
        third = tk.Tk()
        third.withdraw()
        app3 = gui.App(third, demo=True)
        try:
            check("重新打开后三轴都恢复",
                  app3.config.motion_deadzone_dps, (1.1, 1.0, 0.5))
            check("重新打开后滑块也在原位",
                  [round(float(app3.deadzone_vars[a].get()), 1)
                   for a in settings.DEADZONE_AXES], [1.1, 1.0, 0.5])
            check("重新打开后输入框也一致",
                  [app3.deadzone_entries[a].get() for a in settings.DEADZONE_AXES],
                  ["1.1", "1.0", "0.5"])
        finally:
            try:
                app3.worker.stop()
            except Exception:
                pass
            try:
                third.destroy()
            except tk.TclError:
                pass
    finally:
        try:
            app4.worker.stop()
        except Exception:
            pass
        try:
            fourth.destroy()
        except tk.TclError:
            pass


def main() -> int:
    # Never touch the real settings.json: the sensitivity is remembered next to
    # the program, so a test must not read (or overwrite) the developer's value.
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scratch_dir = os.path.join(project, "build", "gui_test")
    os.makedirs(scratch_dir, exist_ok=True)
    settings_file = os.path.join(scratch_dir, "settings.json")
    try:
        os.remove(settings_file)
    except OSError:
        pass
    settings.settings_path = lambda: settings_file

    root = tk.Tk()
    root.withdraw()                     # no window while testing
    app = gui.App(root, demo=True)
    try:
        run_tests(app)
    finally:
        try:
            app.worker.stop()
        except Exception:
            pass
        try:
            root.destroy()
        except tk.TclError:
            pass                        # already closed by the test itself
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

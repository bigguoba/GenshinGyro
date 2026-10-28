"""Tests for the remembered user settings (the sensitivity slider's memory).

Run:  python tools\\test_settings.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings  # noqa: E402

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


def ensure_writable(path: str, attempts: int = 40) -> bool:
    """Wait until a path can be written, creating its folder if needed.

    A freshly created directory under the workspace can refuse the very first
    write while the sandbox finishes granting access, so this retries briefly
    instead of failing the test on a timing artefact.
    """
    directory = os.path.dirname(path)
    for _ in range(attempts):
        try:
            os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("")
            return True
        except OSError:
            time.sleep(0.05)
    return False


def main() -> int:
    # Inside the project, not the system temp area: writes there are sandboxed and
    # a denied write is exactly what the "cannot save" case below must not hit by
    # accident.  A fixed path (rather than a random one) avoids the provisioning
    # race described in ensure_writable.
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    directory = os.path.join(project, "build", "settings_test")
    path = os.path.join(directory, "settings.json")
    if not ensure_writable(path):
        print(f"  !! cannot write {path}; skipping the settings tests")
        return 0
    try:
        print("\n[1] 默认值与往返")
        check("文件不存在时用默认 1.0", settings.gyro_gain(path), 1.0)
        check("写入 3.5 返回 3.5", settings.set_gyro_gain(3.5, path), 3.5)
        check("文件已生成", os.path.isfile(path), True)
        check("读回 3.5", settings.gyro_gain(path), 3.5)
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
        check("文件里有全部记忆项（含三个死区、勾选框与键位互换）",
              sorted(stored), ["deadzone_pitch", "deadzone_roll", "deadzone_yaw",
                              "gyro_gain", "mode", "swap_face_buttons"])
        check("文件里存的就是 3.5", stored["gyro_gain"], 3.5)

        print("\n[2] 精度与范围")
        settings.set_gyro_gain(1.06, path)
        check("1.06 存为 1.1（一位小数）", settings.gyro_gain(path), 1.1)
        settings.set_gyro_gain(99, path)
        check("99 夹到 5.0", settings.gyro_gain(path), 5.0)
        settings.set_gyro_gain(0, path)
        check("0 就是 0（体感关闭）", settings.gyro_gain(path), 0.0)
        settings.set_gyro_gain(-5, path)
        check("负数夹到 0.0", settings.gyro_gain(path), 0.0)

        print("\n[3] 坏文件不能导致启动失败")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json at all")
        check("损坏的 JSON -> 默认 1.0", settings.gyro_gain(path), 1.0)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(["wrong", "shape"]))
        check("类型不对（数组）-> 默认 1.0", settings.gyro_gain(path), 1.0)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"gyro_gain": "abc"}))
        check("值不是数字 -> 默认 1.0", settings.gyro_gain(path), 1.0)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"something_else": 5}))
        check("缺少该键 -> 默认 1.0", settings.gyro_gain(path), 1.0)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"gyro_gain": None}))
        check("键为 null -> 默认 1.0", settings.gyro_gain(path), 1.0)

        print("\n[3b] 编辑器加了 BOM 也要认（记事本 / PowerShell Set-Content）")
        with open(path, "w", encoding="utf-8-sig") as handle:
            handle.write(json.dumps({"gyro_gain": 2.5}))
        with open(path, "rb") as handle:
            check("文件确实带 BOM", handle.read(3), b"\xef\xbb\xbf")
        check("带 BOM 仍读出 2.5", settings.gyro_gain(path), 2.5)
        settings.set_gyro_gain(4.5, path)
        with open(path, "rb") as handle:
            check("自己写出的文件不带 BOM", handle.read(1), b"{")
        check("写入值正确", settings.gyro_gain(path), 4.5)

        print("\n[3c] 死区补偿（三个轴）也是记忆项，且互不覆盖")
        os.remove(path)
        check("默认三轴都是 0", settings.motion_deadzone(path), (0.0, 0.0, 0.0))
        check("写入 (2.5, 1.0, 0.0)",
              settings.set_motion_deadzone((2.5, 1.0, 0.0), path), (2.5, 1.0, 0.0))
        check("读回 (2.5, 1.0, 0.0)", settings.motion_deadzone(path), (2.5, 1.0, 0.0))
        settings.set_gyro_gain(3.0, path)
        check("只存灵敏度不会把死区清零", settings.motion_deadzone(path), (2.5, 1.0, 0.0))
        check("灵敏度也确实存进去了", settings.gyro_gain(path), 3.0)
        settings.set_motion_deadzone((1.0, 2.0, 3.0), path)
        check("只存死区不会把灵敏度清零", settings.gyro_gain(path), 3.0)
        settings.set_motion_deadzone((99, -1, 5.04), path)
        check("死区范围 0–5 且保留一位小数",
              settings.motion_deadzone(path), (5.0, 0.0, 5.0))
        settings.set_motion_deadzone(("abc", None, 1.06), path)
        check("非法输入回退 0（1.06 -> 1.1）",
              settings.motion_deadzone(path), (0.0, 0.0, 1.1))
        with open(path, encoding="utf-8") as handle:
            keys = sorted(json.load(handle))
        check("六个记忆项都在同一个文件里",
              keys, ["deadzone_pitch", "deadzone_roll", "deadzone_yaw", "gyro_gain",
                     "mode", "swap_face_buttons"])

        print("\n[3d] 勾选框状态（mode）")
        check("默认 off", settings.mode(path), "off")
        check("写入 motion 返回 motion", settings.set_mode("motion", path), "motion")
        check("读回 motion", settings.mode(path), "motion")
        settings.set_gyro_gain(2.0, path)
        check("存灵敏度不会清掉 mode", settings.mode(path), "motion")
        check("非法 mode 回退 off", settings.set_mode("nonsense", path), "off")
        settings.set_mode("full", path)
        check("full 可以存", settings.mode(path), "full")

        print("\n[3e] 键位互换（swap_face_buttons）")
        check("默认关", settings.swap_face_buttons(path), False)
        check("写入 True 返回 True", settings.set_swap_face_buttons(True, path), True)
        check("读回 True", settings.swap_face_buttons(path), True)
        check("存互换不会清掉勾选框", settings.mode(path), "full")
        check("存互换不会清掉灵敏度", settings.gyro_gain(path), 2.0)
        check("字符串 false 当关闭", settings.clamp_flag("false"), False)
        check("数字 1 当打开", settings.clamp_flag(1), True)
        check("乱写回退关闭", settings.clamp_flag("乱写"), False)
        check("写回 False", settings.set_swap_face_buttons(False, path), False)
        check("读回 False", settings.swap_face_buttons(path), False)

        print("\n[4] 目录不可写时不抛异常")
        nested = os.path.join(directory, "no_such_dir", "deep", "settings.json")
        check("自动创建父目录后仍能保存",
              settings.set_gyro_gain(2.0, nested), 2.0)
        check("确实写进去了", settings.gyro_gain(nested), 2.0)
        check("对目录路径写入 -> 返回 None 而不是崩溃",
              settings.save({"gyro_gain": 2.0}, directory), None)
        check("load 对目录路径也安全", settings.load(directory)["gyro_gain"], 1.0)
    finally:
        print("\ncleanup:")
        for root, dirs, files in os.walk(directory, topdown=False):
            for name in files:
                target = os.path.join(root, name)
                try:
                    os.remove(target)
                except OSError as exc:
                    print(f"  could not remove {target}: {exc}")
            for name in dirs:
                target = os.path.join(root, name)
                try:
                    os.rmdir(target)
                except OSError as exc:
                    print(f"  could not remove dir {target}: {exc}")

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

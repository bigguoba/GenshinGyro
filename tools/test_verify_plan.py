"""Tests for the guided verification logic (verify_plan), no hardware needed.

Drives each kind of step with synthetic controller states and checks that a
correct input passes while a wrong button/axis/sign is reported as a mismatch
with the right capture.

Run:  python tools\\test_verify_plan.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mapping  # noqa: E402
import switchpro  # noqa: E402
import verify_plan  # noqa: E402

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


class FakeController:
    """Minimal stand-in providing the calibration objects the runner uses."""

    def __init__(self, imu=None, left=None, right=None):
        self.imu_calibration = imu or switchpro.ImuCalibration.default()
        self.left_stick_calibration = left or switchpro.StickCalibration.default()
        self.right_stick_calibration = right or switchpro.StickCalibration.default()


def state(buttons=(0, 0, 0), left=(2048, 2048), right=(2048, 2048),
          accel=(0, 0, 0), gyro=(0, 0, 0)) -> switchpro.ControllerState:
    return switchpro.ControllerState(
        report_id=0x30, timer=0, battery_level=4, charging=False, connection_info=0,
        buttons=buttons, left_stick=left, right_stick=right, vibration_code=0,
        imu=[switchpro.ImuSample(accel=accel, gyro=gyro)],
    )


def find_step(steps, key):
    for step in steps:
        if step.key == key:
            return step
    raise KeyError(key)


# --------------------------------------------------------------------------- #

def test_button_steps() -> None:
    print("\n[1] 按键步骤：正确 / 按错 / 多按")
    config = mapping.MappingConfig()
    steps = verify_plan.build_steps(config)
    fake = FakeController()

    # B is byte 0 bit 0x04
    runner = verify_plan.StepRunner(find_step(steps, "button:B"), fake, config)
    check("等待（未按）", runner.feed(state()).status, "waiting")
    check("正确按下 B", runner.feed(state(buttons=(0x04, 0, 0))).status, "pass")

    # pressing Y (byte 0 bit 0x01) instead must be caught, with a capture
    runner = verify_plan.StepRunner(find_step(steps, "button:B"), fake, config)
    runner.feed(state())
    outcome = runner.feed(state(buttons=(0x01, 0, 0)))
    check("按错时报 mismatch", outcome.status, "mismatch")
    check("捕捉到实际位", outcome.captured, (0, 0x01))
    check("错误信息指出实际按键", "Y" in outcome.message, True)

    # two buttons at once -> retry, not a silent pass
    runner = verify_plan.StepRunner(find_step(steps, "button:B"), fake, config)
    runner.feed(state())
    check("同时按两个键 -> retry",
          runner.feed(state(buttons=(0x05, 0, 0))).status, "retry")

    # R lives in byte 0 bit 0x40, ZR in byte 0 bit 0x80, L/ZL in byte 2
    for key, expect in (("button:R", (0x40, 0, 0)), ("button:ZR", (0x80, 0, 0)),
                        ("button:L", (0, 0, 0x40)), ("button:ZL", (0, 0, 0x80)),
                        ("button:L3", (0, 0x08, 0)), ("button:R3", (0, 0x04, 0)),
                        ("button:Plus", (0, 0x02, 0)), ("button:Minus", (0, 0x01, 0)),
                        ("button:Home", (0, 0x10, 0)), ("button:Capture", (0, 0x20, 0))):
        runner = verify_plan.StepRunner(find_step(steps, key), fake, config)
        runner.feed(state())
        check(f"{key} 正确", runner.feed(state(buttons=expect)).status, "pass")

    # the classic byte-confusion: L and ZL share bits with R and ZR
    runner = verify_plan.StepRunner(find_step(steps, "button:ZR"), fake, config)
    runner.feed(state())
    outcome = runner.feed(state(buttons=(0, 0, 0x80)))   # pressed ZL instead
    check("把 ZL 当成 ZR 会被抓住", outcome.status, "mismatch")
    check("并指出实际是 ZL", "ZL" in outcome.message, True)


def test_dpad_steps() -> None:
    print("\n[2] 十字键步骤")
    config = mapping.MappingConfig()
    steps = verify_plan.build_steps(config)
    fake = FakeController()
    for key, expect in (("dpad:Up", 0x02), ("dpad:Right", 0x04),
                        ("dpad:Down", 0x01), ("dpad:Left", 0x08)):
        runner = verify_plan.StepRunner(find_step(steps, key), fake, config)
        runner.feed(state())
        check(f"{key} 正确", runner.feed(state(buttons=(0, 0, expect))).status, "pass")

    runner = verify_plan.StepRunner(find_step(steps, "dpad:Up"), fake, config)
    runner.feed(state())
    outcome = runner.feed(state(buttons=(0, 0, 0x01)))  # Down instead of Up
    check("按反方向会被抓住", outcome.status, "mismatch")
    check("指出实际是 Down", "Down" in outcome.message, True)


def test_stick_steps() -> None:
    print("\n[3] 摇杆步骤")
    config = mapping.MappingConfig()
    steps = verify_plan.build_steps(config)
    fake = FakeController()

    runner = verify_plan.StepRunner(find_step(steps, "stick:left:x:-1"), fake, config)
    check("左摇杆最左", runner.feed(state(left=(248, 2048))).status, "pass")

    runner = verify_plan.StepRunner(find_step(steps, "stick:left:x:-1"), fake, config)
    check("推反了（最右）", runner.feed(state(left=(3848, 2048))).status, "mismatch")

    runner = verify_plan.StepRunner(find_step(steps, "stick:left:x:-1"), fake, config)
    outcome = runner.feed(state(left=(2048, 3848)))
    check("推错轴（最上）", outcome.status, "mismatch")
    check("提示动的是另一根轴", "Y" in outcome.message, True)

    runner = verify_plan.StepRunner(find_step(steps, "stick:left:x:-1"), fake, config)
    check("同时动了右摇杆 -> retry",
          runner.feed(state(left=(248, 2048), right=(3848, 2048))).status, "retry")

    runner = verify_plan.StepRunner(find_step(steps, "stick:right:y:-1"), fake, config)
    check("右摇杆最下", runner.feed(state(right=(2048, 248))).status, "pass")

    runner = verify_plan.StepRunner(find_step(steps, "stick:center"), fake, config) \
        if False else None
    # centre must be inside the deadband for the pass branch
    runner = verify_plan.StepRunner(find_step(steps, "stick:left:x:-1"), fake, config)
    check("只推到一半不算通过",
          runner.feed(state(left=(1400, 2048))).status, "waiting")


def test_gyro_steps() -> None:
    print("\n[4] 体感步骤（轴向与符号）")
    config = mapping.MappingConfig()
    steps = verify_plan.build_steps(config)
    calibration = switchpro.ImuCalibration.default()
    fake = FakeController(imu=calibration)

    # 50 dps on one axis: with the default calibration ~14.2842 counts per dps
    counts = int(round(50 * 14.2842))
    runner = verify_plan.StepRunner(find_step(steps, "gyro:2:1"), fake, config)
    check("偏航向左（+Z）", runner.feed(state(gyro=(0, 0, counts))).status, "pass")

    runner = verify_plan.StepRunner(find_step(steps, "gyro:2:1"), fake, config)
    check("偏航方向相反（-Z）",
          runner.feed(state(gyro=(0, 0, -counts))).status, "mismatch")

    runner = verify_plan.StepRunner(find_step(steps, "gyro:2:1"), fake, config)
    outcome = runner.feed(state(gyro=(counts, 0, 0)))
    check("偏航跑到 X 轴", outcome.status, "mismatch")
    check("提示主要出现在 X 轴", "X 轴" in outcome.message, True)

    runner = verify_plan.StepRunner(find_step(steps, "gyro:1:1"), fake, config)
    check("俯仰（+Y）", runner.feed(state(gyro=(0, counts, 0))).status, "pass")

    runner = verify_plan.StepRunner(find_step(steps, "gyro:0:-1"), fake, config)
    check("侧倾向左（-X）", runner.feed(state(gyro=(-counts, 0, 0))).status, "pass")

    runner = verify_plan.StepRunner(find_step(steps, "gyro:0:-1"), fake, config)
    check("侧倾方向相反（+X）",
          runner.feed(state(gyro=(counts, 0, 0))).status, "mismatch")


def test_accel_and_rest_steps() -> None:
    print("\n[5] 重力姿态与静止步骤")
    config = mapping.MappingConfig()
    steps = verify_plan.build_steps(config)
    fake = FakeController()

    runner = verify_plan.StepRunner(find_step(steps, "imu:flat"), fake, config)
    check("平放 +Z = 1 g", runner.feed(state(accel=(0, 0, 4096))).status, "pass")

    runner = verify_plan.StepRunner(find_step(steps, "imu:flat"), fake, config)
    outcome = runner.feed(state(accel=(4096, 0, 0)))
    check("重力跑到 X 轴", outcome.status, "mismatch")
    check("提示主要落在 X 轴", "X 轴" in outcome.message, True)

    runner = verify_plan.StepRunner(find_step(steps, "imu:flat"), fake, config)
    check("重力方向相反（-Z）",
          runner.feed(state(accel=(0, 0, -4096))).status, "mismatch")

    runner = verify_plan.StepRunner(find_step(steps, "imu:stand"), fake, config)
    check("立起 90°（±X 都接受）",
          runner.feed(state(accel=(-4096, 0, 0))).status, "pass")
    runner = verify_plan.StepRunner(find_step(steps, "imu:stand"), fake, config)
    check("立起 90°（+X）", runner.feed(state(accel=(4096, 0, 0))).status, "pass")

    # rest needs REST_SECONDS of quiet; drive a fake clock
    now = [1000.0]
    runner = verify_plan.StepRunner(find_step(steps, "imu:rest"), fake, config,
                                    clock=lambda: now[0])
    check("刚静止时还在等待", runner.feed(state(accel=(0, 0, 4096), gyro=(0, 0, 0))).status,
          "waiting")
    now[0] += 0.9
    check("静止满 0.8 秒后通过",
          runner.feed(state(accel=(0, 0, 4096), gyro=(0, 0, 0))).status, "pass")

    now = [2000.0]
    runner = verify_plan.StepRunner(find_step(steps, "imu:rest"), fake, config,
                                    clock=lambda: now[0])
    runner.feed(state(accel=(0, 0, 4096), gyro=(0, 0, 0)))
    now[0] += 0.9
    moving = int(round(20 * 14.2842))
    check("中途动了一下不算通过",
          runner.feed(state(accel=(0, 0, 4096), gyro=(moving, 0, 0))).status, "waiting")


def test_step_coverage() -> None:
    print("\n[6] 步骤清单覆盖全部控件")
    config = mapping.MappingConfig()
    steps = verify_plan.build_steps(config)
    keys = {step.key for step in steps}
    for name in mapping.DEFAULT_BUTTON_MAP:
        check(f"包含按键 {name}", f"button:{name}" in keys, True)
    for name in mapping.DPAD_MAP:
        check(f"包含十字键 {name}", f"dpad:{name}" in keys, True)
    for stick in ("left", "right"):
        for axis in ("x", "y"):
            for sign in (-1, 1):
                check(f"包含摇杆 {stick}:{axis}:{sign}",
                      f"stick:{stick}:{axis}:{sign}" in keys, True)
    check("包含体感三轴", sum(1 for key in keys if key.startswith("gyro:")) >= 3, True)
    print(f"  （共 {len(steps)} 个步骤）")


def test_correction_round_trip(tmpdir: str) -> None:
    print("\n[7] 捕捉到的修正可以保存并重新加载")
    path = os.path.join(tmpdir, "captured_mapping.json")
    mapping.save_captured_buttons({"B": (0, 0x01), "Up": (2, 0x01)}, path)
    loaded = mapping.load_captured_buttons(path)
    check("修正已写入并可读回", loaded, {"B": (0, 0x01), "Up": (2, 0x01)})

    config = mapping.MappingConfig()
    config.button_map.update({k: v for k, v in loaded.items() if k in mapping.DEFAULT_BUTTON_MAP})
    runner = verify_plan.StepRunner(
        verify_plan.build_steps(config)[0], FakeController(), config)
    runner.feed(state())
    check("修正后的 B 位按下即通过",
          runner.feed(state(buttons=(0x01, 0, 0))).status, "pass")
    os.remove(path)


def main() -> int:
    print("verify_plan 逻辑测试")
    test_button_steps()
    test_dpad_steps()
    test_stick_steps()
    test_gyro_steps()
    test_accel_and_rest_steps()
    test_step_coverage()
    test_correction_round_trip(os.path.dirname(os.path.abspath(__file__)))
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

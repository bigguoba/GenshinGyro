"""Guided one-control-at-a-time verification of the Switch -> DS4 mapping.

Each step asks for exactly one control.  Instead of just watching a log, the
runner *captures* what the controller actually sent and compares it with what
was expected, so a wrong binding is identified immediately and can be turned
into a correction for :mod:`mapping`.

The module is deliberately UI-free: :mod:`gui` drives it with a window and
``tools/test_verify_plan.py`` drives it with synthetic states.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import mapping
import switchpro

#: Thresholds used to decide that a deliberate motion happened.
GYRO_MIN_DPS = 15.0
GYRO_DOMINANCE = 1.5
STICK_MIN_TRAVEL = 0.7
STICK_OTHER_MAX = 0.5
STICK_OTHER_STICK_MAX = 0.35
REST_MAX_DPS = 3.0
REST_SECONDS = 0.8
ACCEL_MIN_G = 0.75
ACCEL_MAX_OTHER_G = 0.5

AXIS_NAMES = ("X", "Y", "Z")

#: human readable description of what each measured axis means physically
AXIS_MEANING = {
    "X": "前后轴（朝向扳机）",
    "Y": "左右轴（指向左手侧）",
    "Z": "竖直轴（朝上）",
}


@dataclass
class Step:
    key: str
    title: str
    instruction: str
    kind: str                      # rest | button | dpad | stick | gyro | accel
    control: str = ""              # logical control name (button/dpad)
    expect: dict = field(default_factory=dict)
    hint: str = ""


@dataclass
class Outcome:
    status: str                    # waiting | pass | mismatch | retry
    message: str = ""
    captured: tuple | None = None  # (byte, bit) for buttons / d-pad
    measured: str = ""


def build_steps(config: mapping.MappingConfig | None = None) -> list[Step]:
    """The full guided sequence, in the order the user should perform it."""
    config = config or mapping.MappingConfig()

    def button(name: str, title: str, instruction: str, hint: str = "") -> Step:
        byte_index, bit = config.button_map[name]
        return Step(key=f"button:{name}", title=title, instruction=instruction,
                    kind="button", control=name,
                    expect={"byte": byte_index, "bit": bit}, hint=hint)

    def dpad(name: str, title: str, instruction: str) -> Step:
        byte_index, bit = config.dpad_map[name]
        return Step(key=f"dpad:{name}", title=title, instruction=instruction,
                    kind="dpad", control=name,
                    expect={"byte": byte_index, "bit": bit})

    def special(name: str, title: str, instruction: str) -> Step:
        # Home / Capture live in the shared byte and are not remappable.
        byte_index, bit, _ds4 = mapping.SPECIAL_MAP[name]
        return Step(key=f"button:{name}", title=title, instruction=instruction,
                    kind="button", control=name,
                    expect={"byte": byte_index, "bit": bit})

    def stick(stick_name: str, axis: str, sign: int, title: str, instruction: str) -> Step:
        return Step(key=f"stick:{stick_name}:{axis}:{sign}", title=title,
                    instruction=instruction, kind="stick",
                    expect={"stick": stick_name, "axis": axis, "sign": sign})

    def gyro(axis: int, sign: int, title: str, instruction: str, hint: str = "") -> Step:
        return Step(key=f"gyro:{axis}:{sign}", title=title, instruction=instruction,
                    kind="gyro", expect={"axis": axis, "sign": sign}, hint=hint)

    steps: list[Step] = [
        # -- face buttons (physical positions) --------------------------- #
        button("B", "B → ✕ (Cross)", "请按下 B 键（下方那个）", "DS4 的 ✕"),
        button("A", "A → ○ (Circle)", "请按下 A 键（右侧那个）", "DS4 的 ○"),
        button("Y", "Y → □ (Square)", "请按下 Y 键（左侧那个）", "DS4 的 □"),
        button("X", "X → △ (Triangle)", "请按下 X 键（上方那个）", "DS4 的 △"),
        # -- shoulders and triggers -------------------------------------- #
        button("L", "L → L1", "请按下 L 键（左肩键）"),
        button("R", "R → R1", "请按下 R 键（右肩键）"),
        button("ZL", "ZL → L2", "请按下 ZL 键（左扳机，按住到底）"),
        button("ZR", "ZR → R2", "请按下 ZR 键（右扳机，按住到底）"),
        # -- stick clicks ------------------------------------------------ #
        button("L3", "L3（左摇杆按下）", "请垂直按下左摇杆"),
        button("R3", "R3（右摇杆按下）", "请垂直按下右摇杆"),
        # -- centre buttons ---------------------------------------------- #
        button("Plus", "Plus → Options", "请按下 + 键（右上）"),
        button("Minus", "Minus → Share", "请按下 − 键（左上）"),
        special("Home", "Home → PS", "请轻按一下 Home 键（右下，别长按）"),
        special("Capture", "Capture → 触摸板按下", "请按一下截图键（左下）"),
        # -- d-pad ------------------------------------------------------- #
        dpad("Up", "十字键 ↑", "请按十字键的上"),
        dpad("Right", "十字键 →", "请按十字键的右"),
        dpad("Down", "十字键 ↓", "请按十字键的下"),
        dpad("Left", "十字键 ←", "请按十字键的左"),
        # -- sticks ------------------------------------------------------ #
        stick("left", "x", -1, "左摇杆 最左", "请把左摇杆推到最左边并停住"),
        stick("left", "x", +1, "左摇杆 最右", "请把左摇杆推到最右边并停住"),
        stick("left", "y", +1, "左摇杆 最上", "请把左摇杆推到最上面并停住"),
        stick("left", "y", -1, "左摇杆 最下", "请把左摇杆推到最下面并停住"),
        stick("right", "x", -1, "右摇杆 最左", "请把右摇杆推到最左边并停住"),
        stick("right", "x", +1, "右摇杆 最右", "请把右摇杆推到最右边并停住"),
        stick("right", "y", +1, "右摇杆 最上", "请把右摇杆推到最上面并停住"),
        stick("right", "y", -1, "右摇杆 最下", "请把右摇杆推到最下面并停住"),
        # -- motion ------------------------------------------------------ #
        Step(key="imu:rest", title="静止（陀螺仪零点）",
             instruction="请把手柄平放在桌面上、正面朝上，完全静止",
             kind="rest", hint="应接近 0 °/s"),
        Step(key="imu:flat", title="重力方向（平放 = +Z）",
             instruction="保持平放不动，等重力读数稳定",
             kind="accel", expect={"axis": 2, "sign": 1, "min_g": ACCEL_MIN_G,
                                   "max_other_g": ACCEL_MAX_OTHER_G},
             hint="平放时重力应落在 Z 轴 +1 g"),
        gyro(2, +1, "偏航向左（绕竖直轴）",
             "像转方向盘那样绕竖直轴向左转约 90°（俯视逆时针）",
             hint="应主要落在 Z 轴，且为正值"),
        gyro(1, +1, "俯仰（前端向下）",
             "请做点头动作：前端（扳机那一侧）朝下",
             hint="应主要落在 Y 轴，且为正值"),
        gyro(0, -1, "侧倾向左（左侧朝下）",
             "请像倒水一样把左侧压下去",
             hint="应主要落在 X 轴，且为负值"),
        Step(key="imu:stand", title="立起 90°（重力转到 X 轴）",
             instruction="请从平放慢慢把柄立起来、侧立在桌面上（约 90°），停住",
             kind="accel", expect={"axis": 0, "sign": 0, "min_g": ACCEL_MIN_G,
                                   "max_other_g": ACCEL_MAX_OTHER_G},
             hint="立起来后重力应落在 X 轴（正负都可）"),
        Step(key="imu:flat_again", title="放回平放",
             instruction="请把手柄放回平放、正面朝上并停住",
             kind="accel", expect={"axis": 2, "sign": 1, "min_g": ACCEL_MIN_G,
                                   "max_other_g": ACCEL_MAX_OTHER_G},
             hint="确认能回到 +Z = 1 g"),
    ]
    return steps


def describe_bit(byte_index: int, bit: int,
                 config: mapping.MappingConfig | None = None) -> str:
    """Which logical control a (byte, bit) belongs to, for error messages."""
    config = config or mapping.MappingConfig()
    for table in (config.button_map, config.dpad_map):
        for name, (byte, value) in table.items():
            if byte == byte_index and value == bit:
                return name
    for name, (byte, value, _special) in mapping.SPECIAL_MAP.items():
        if byte == byte_index and value == bit:
            return name
    return f"未映射的位 (字节 {byte_index}, 0x{bit:02X})"


def set_bits(button_bytes) -> set[tuple[int, int]]:
    found = set()
    for byte_index, value in enumerate(button_bytes):
        for bit_index in range(8):
            if value & (1 << bit_index):
                found.add((byte_index, 1 << bit_index))
    return found


class StepRunner:
    """Feeds controller states into one step and reports the outcome."""

    def __init__(self, step: Step, controller: switchpro.SwitchProController,
                 config: mapping.MappingConfig | None = None, clock=time.monotonic):
        self.step = step
        self.controller = controller
        self.config = config or mapping.MappingConfig()
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._primed = False
        self._rest_since: float | None = None
        self._best_measured = ""
        self._peak = 0.0
        self._peak_axis = None
        self._peak_sign = 0

    # -- helpers ---------------------------------------------------------- #

    def _calibration(self):
        return self.controller.imu_calibration

    def _stick_normalized(self, state: switchpro.ControllerState):
        left = self.controller.left_stick_calibration
        right = self.controller.right_stick_calibration
        return {
            ("left", "x"): left.x.normalize(state.left_stick[0]),
            ("left", "y"): left.y.normalize(state.left_stick[1]),
            ("right", "x"): right.x.normalize(state.right_stick[0]),
            ("right", "y"): right.y.normalize(state.right_stick[1]),
        }

    def live_text(self, state: switchpro.ControllerState) -> str:
        """Short live read-out for the UI while this step is active."""
        sample = state.imu[-1] if state.imu else None
        if self.step.kind == "gyro" and sample is not None:
            dps = self._calibration().to_dps(sample.gyro)
            return "陀螺仪 °/s:  " + "  ".join(
                f"{AXIS_NAMES[i]}={dps[i]:+7.1f}" for i in range(3))
        if self.step.kind in ("accel", "rest") and sample is not None:
            g = self._calibration().to_g(sample.accel)
            return "重力 g:  " + "  ".join(f"{AXIS_NAMES[i]}={g[i]:+6.3f}" for i in range(3))
        if self.step.kind == "stick":
            values = self._stick_normalized(state)
            return ("左摇杆 ({:+.2f}, {:+.2f})   右摇杆 ({:+.2f}, {:+.2f})").format(
                values[("left", "x")], values[("left", "y")],
                values[("right", "x")], values[("right", "y")])
        if self.step.kind in ("button", "dpad"):
            return "按键字节:  " + "  ".join(
                f"b{i}=0x{value:02X}" for i, value in enumerate(state.buttons))
        return ""

    # -- evaluation ------------------------------------------------------- #

    def feed(self, state: switchpro.ControllerState) -> Outcome:
        kind = self.step.kind
        if kind in ("button", "dpad"):
            return self._feed_button(state)
        if kind == "stick":
            return self._feed_stick(state)
        if kind == "gyro":
            return self._feed_gyro(state)
        if kind == "accel":
            return self._feed_accel(state)
        if kind == "rest":
            return self._feed_rest(state)
        return Outcome("waiting")

    def _feed_button(self, state: switchpro.ControllerState) -> Outcome:
        if not self._primed:
            if not any(state.buttons):
                self._primed = True
            return Outcome("waiting", "等待按键…")
        pressed = set_bits(state.buttons)
        if not pressed:
            return Outcome("waiting", "等待按键…")
        expected = (self.step.expect["byte"], self.step.expect["bit"])
        if pressed == {expected}:
            return Outcome("pass", f"读到 {describe_bit(*expected, config=self.config)}")
        if len(pressed) == 1:
            byte_index, bit = next(iter(pressed))
            actual = describe_bit(byte_index, bit, config=self.config)
            return Outcome(
                "mismatch",
                f"按下的是「{self.step.control}」，但手柄报的是 {actual}"
                f"（字节 {byte_index}, 0x{bit:02X}）；预期 字节 {expected[0]}, "
                f"0x{expected[1]:02X}",
                captured=(byte_index, bit),
            )
        return Outcome("retry", "检测到多个按键同时按下，请只按这一个键")

    def _feed_stick(self, state: switchpro.ControllerState) -> Outcome:
        values = self._stick_normalized(state)
        stick_name = self.step.expect["stick"]
        axis = self.step.expect["axis"]
        sign = self.step.expect["sign"]
        target = values[(stick_name, axis)] * sign
        other_axis = "y" if axis == "x" else "x"
        other = values[(stick_name, other_axis)]
        other_stick = "right" if stick_name == "left" else "left"
        cross = max(abs(values[(other_stick, "x")]), abs(values[(other_stick, "y")]))

        if target >= STICK_MIN_TRAVEL and abs(other) <= STICK_OTHER_MAX and cross <= STICK_OTHER_STICK_MAX:
            return Outcome("pass", f"行程 {target * 100:.0f}%")
        if (abs(other) >= STICK_MIN_TRAVEL and target < STICK_MIN_TRAVEL
                and cross <= STICK_OTHER_STICK_MAX):
            return Outcome("mismatch",
                           f"动的是另一个方向（{other_axis.upper()} 轴 "
                           f"{other:+.2f}），说明轴或方向不对")
        if target <= -STICK_MIN_TRAVEL and abs(other) <= STICK_OTHER_MAX:
            return Outcome("mismatch", f"方向相反（{axis.upper()} 轴 {target:+.2f}）")
        if cross > STICK_OTHER_STICK_MAX:
            return Outcome("retry", "请只动这一根摇杆，另一根保持在中间")
        return Outcome("waiting", "等待摇杆推到底…")

    def _feed_gyro(self, state: switchpro.ControllerState) -> Outcome:
        if not state.imu:
            return Outcome("waiting")
        dps = self._calibration().to_dps(state.imu[-1].gyro)
        axis = self.step.expect["axis"]
        sign = self.step.expect["sign"]
        target = dps[axis] * sign
        others = [abs(dps[i]) for i in range(3) if i != axis]
        worst_other = max(others)

        if target >= GYRO_MIN_DPS and target >= GYRO_DOMINANCE * worst_other:
            return Outcome("pass", f"{AXIS_NAMES[axis]} 轴 {target * sign:+.1f} °/s")
        if target <= -GYRO_MIN_DPS and abs(target) >= GYRO_DOMINANCE * worst_other:
            return Outcome("mismatch", f"方向相反（{AXIS_NAMES[axis]} 轴 {target * sign:+.1f} °/s）")
        if worst_other >= GYRO_MIN_DPS and abs(target) < GYRO_MIN_DPS:
            dominant = max(range(3), key=lambda i: abs(dps[i]))
            return Outcome(
                "mismatch",
                f"转动主要出现在 {AXIS_NAMES[dominant]} 轴 "
                f"（{dps[dominant]:+.1f} °/s），预期是 {AXIS_NAMES[axis]} 轴")
        return Outcome("waiting", "等待转动…")

    def _feed_accel(self, state: switchpro.ControllerState) -> Outcome:
        if not state.imu:
            return Outcome("waiting")
        g = self._calibration().to_g(state.imu[-1].accel)
        axis = self.step.expect["axis"]
        sign = self.step.expect["sign"]
        min_g = self.step.expect.get("min_g", ACCEL_MIN_G)
        max_other = self.step.expect.get("max_other_g", ACCEL_MAX_OTHER_G)
        others = [abs(g[i]) for i in range(3) if i != axis]
        worst_other = max(others)

        if abs(g[axis]) >= min_g and worst_other <= max_other:
            if sign and (g[axis] > 0) != (sign > 0):
                return Outcome("mismatch",
                               f"{AXIS_NAMES[axis]} 轴符号相反（{g[axis]:+.3f} g）")
            return Outcome("pass", f"{AXIS_NAMES[axis]} 轴 {g[axis]:+.3f} g")
        if worst_other >= min_g and abs(g[axis]) < min_g:
            dominant = max(range(3), key=lambda i: abs(g[i]))
            return Outcome("mismatch",
                           f"重力主要落在 {AXIS_NAMES[dominant]} 轴（{g[dominant]:+.3f} g），"
                           f"预期是 {AXIS_NAMES[axis]} 轴")
        return Outcome("waiting", "等待姿态稳定…")

    def _feed_rest(self, state: switchpro.ControllerState) -> Outcome:
        if not state.imu:
            return Outcome("waiting")
        dps = self._calibration().to_dps(state.imu[-1].gyro)
        g = self._calibration().to_g(state.imu[-1].accel)
        magnitude = sum(value * value for value in g) ** 0.5
        quiet = max(abs(value) for value in dps) <= REST_MAX_DPS
        sane = 0.85 <= magnitude <= 1.15
        now = self._clock()
        if quiet and sane:
            if self._rest_since is None:
                self._rest_since = now
            elif now - self._rest_since >= REST_SECONDS:
                return Outcome("pass", f"零偏 ≤ {max(abs(v) for v in dps):.1f} °/s，"
                                       f"|g| = {magnitude:.3f}")
        else:
            self._rest_since = None
        if not sane:
            return Outcome("waiting", f"|g| = {magnitude:.3f} g，请让手柄静止")
        return Outcome("waiting",
                       f"请保持静止…（当前最大 {max(abs(v) for v in dps):.1f} °/s）")

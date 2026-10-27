"""Menu for the physical pad's rumble tests.

A plain numbered menu, because "drag a letter onto the .bat" turned out to be far
too obscure.  Each entry runs one mode from :mod:`rumble_test`, which opens and
closes the pad on its own -- and always leaves it silent.

    python tools\\rumble_menu.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rumble_test  # noqa: E402

MENU = """
============================================================
 手柄震动测试（当前 NS 模式，蓝牙）
============================================================
  1  强度扫频（文档编码，11 档）        ← 请先跑这个
  2  频率扫频（文档编码，诊断用）
  3  四段：同时 / 只左 / 只右 / 交替     ← 再跑这个，确认左右能分开
  4  交互模式：键盘手动控制
  5  逐字节扫描（找出原始强度字节，诊断用）
  s  立即停止震动（全零载荷）
  q  退出

 已知：
  * 停止只认**全零**载荷（文档"中立" 0x40 对它是 25% 驱动）
  * 文档编码：能震 ✓ 左右能分开 ✓ 但强度曲线可能不理想
  * raw 编码（只动 byte2）：力度单调 ✓ 但左右分不开、且混合后不单调 ✗
============================================================
"""


def ask() -> str:
    try:
        return input("请输入 1/2/3/4/s/q 后回车：").strip().lower()
    except EOFError:
        return "q"


def main() -> int:
    while True:
        print(MENU)
        choice = ask()
        if choice in ("q", "quit", "退出"):
            print("退出。")
            return 0
        if choice in ("s", "stop", "停止"):
            rumble_test.main(["--stop"])
        elif choice == "1":
            rumble_test.main(["--sweep-amplitude"])
        elif choice == "2":
            rumble_test.main(["--sweep-frequency"])
        elif choice == "3":
            rumble_test.main(["--sweep"])
        elif choice == "4":
            rumble_test.main(["--interactive"])
        elif choice == "5":
            rumble_test.main(["--sweep-raw"])
        else:
            print("  （没有这个选项，请输入 1/2/3/4/5/s/q）")
            continue
        try:
            input("\n按回车返回菜单…")
        except EOFError:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())

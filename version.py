"""Version of the GenshinGyro tool (原神陀螺仪助手)."""

from __future__ import annotations

__version__ = "1.3.2"

#: Short description of what this build is, used by --version and the snapshot.
__summary__ = ("原神陀螺仪助手 / GenshinGyro —— Switch Pro 手柄 与 DualShock 4（有线·蓝牙）"
               "→ 虚拟 DualShock 4"
               "（按键 / 摇杆 / 体感；灵敏度、三轴死区补偿、×○□△ 键位互换可调、都会被记住；"
               "Share 或 Switch 的 - 可映射为触控板按下）"
               "；双击即开图形界面；蓝牙 DS4 会被改写成有线报文，"
               "解决游戏读不到蓝牙体感的问题")


def version_string() -> str:
    return f"GenshinGyro {__version__}"

# 开发约定（给未来的自己 / AI 助手）

本文件只讲工作方式，不讲功能。用户明确要求：**开发期不要每次都打包、写报告**。

## 开发期间（默认状态）

只做用户当下提出的改动，做完汇报一句话即可：

- **不**运行 PyInstaller，**不**创建 `GenshinGyro-vX.Y.Z` 目录或 zip，**不**算 SHA256
- **不**更新 `CHANGELOG.md` / `README.md` / `快速开始.txt` / `version.py`
- **不**截图、**不**跑 `tools\list_process_windows.py` / `close_window.py` 之类的验证仪式
- 例外（这些是"实现"的一部分，成本低且能防回归）：
  - `python -m py_compile` 改动过的文件
  - 跑测试套件（可只跑相关的，一条命令跑完五个更好）
- 开发时想试运行，直接跑源码：`python main.py` 或 `GenshinGyro.bat`

## 封版时（用户说"封版 / 打包 / 出个新版"才做）

1. `version.py` 升版本号 + 更新 `__summary__`
2. `CHANGELOG.md` 写这一版（含验证结论）
3. 同步文档里可能过时的具体数值：`README.md`、`快速开始.txt`
4. `build_exe.bat`（内部就是 `--onedir --windowed --name GenshinGyro`
   加 `--icon`/`--add-data` 图标与 `--add-binary` ViGEmClient；**必须 --windowed**，无控制台）
5. 组装 `GenshinGyro-vX.Y.Z\`（exe + `_internal` + 启动 `.bat` + 文档 + `source\`）、
   打 zip、报 SHA256；`快速开始.txt` 写回 UTF-8 **带 BOM**
   （发行包不放 `.lnk`：快捷方式里是绝对路径，换台机器就失效，`.bat` 才是可携带的入口）
6. 冒烟：无参数启动只有窗口（无 `ConsoleWindowClass`）、点叉能退出、无残留进程

## 长期红线（与上面无关，永远适用）

- 已发布的版本目录（v1.0.0 …）只增不改
- 不批量 kill python 进程、不改 HidHide 配置
- 体感节奏（4.096 ms / 时间戳 +768 / 样本保持）是原神可用性的根，不要为了"波形好看"改它

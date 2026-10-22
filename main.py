#!/usr/bin/env python3
"""
NetOpsTerminal - 网络运维终端
主程序入口

两种跑法（同一份产物，别拆成两个 exe）：
  · 双击 / 无参数                     → 起界面（正常使用）
  · `<产物>.exe --script <脚本.py> [参数…]` → 跑一个**随包脚本**（打包态的脚本通道）

为什么需要后面这个通道：PyInstaller onefile 打包后 `sys.executable` 就是**应用本体**，
`[sys.executable, "probe_tier.py"]` 会被当成"再开一个应用"而不是跑脚本 —— 这是改造前的
硬伤。唯一实现见 `core/app_paths.script_cmd()`，测试见 `tests/test_frozen_scripts.py`。
"""
import os
import runpy
import sys
import time
import traceback

# 添加项目根目录到路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _script_log(text):
    """把脚本的失败写进 `<基准目录>\\aiterminal-script.log`，并返回非 0。

    为什么必须写文件：打包态的 exe 是 windowed（无控制台），`print` 出去的报错
    **没人看得见**。日志就落在 exe 旁边（`core/app_paths.base_dir()`），交付机上
    打开就能看，不用教用户开命令行。
    """
    path = os.path.join(BASE_DIR, "aiterminal-script.log")
    try:
        from core import app_paths                    # 打包态基准目录 = exe 所在目录
        path = os.path.join(app_paths.base_dir(), "aiterminal-script.log")
    except Exception:  # noqa: BLE001
        pass
    text = f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} --script 失败 =====\n{text}\n"
    try:
        sys.stderr.write(text)                        # 有控制台时同时给人看
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
    return 1


def _resolve_script(path):
    """把 `--script` 给的名字解析成**真实文件路径**；解析不到返回 ""。

    按序试三个位置（第一个命中即用）：
      1) 原样（绝对路径，或相对当前工作目录 —— 交付机上脚本摆在手边时的写法）
      2) `<基准目录>/名字`（基准目录 = 源码态仓库根 / 打包态 exe 所在目录，见 app_paths）
      3) `<随包解压目录>/名字`（onefile 的 `_MEIPASS`：`datas` 里的脚本解压在那儿，
         路径每次启动都变，所以 .bat 不能硬编码它，只能靠这里兜住）
    """
    cands = [path]
    try:
        from core import app_paths
        cands.append(os.path.join(app_paths.base_dir(), path))
    except Exception:  # noqa: BLE001
        cands.append(os.path.join(BASE_DIR, path))
    bundle = getattr(sys, "_MEIPASS", "") or ""
    if bundle:
        cands.append(os.path.join(bundle, path))
    for cand in cands:
        if cand and os.path.isfile(cand):
            return os.path.abspath(cand)
    return ""


def _run_script(argv):
    """`--script <脚本> [参数…]`：跑完把脚本的退出码原样返回（**不建界面**）。"""
    if not argv:
        return _script_log("--script 后面没有给脚本路径")
    script = _resolve_script(argv[0])
    if not script:
        return _script_log(f"脚本不存在: {argv[0]}")
    rest = list(argv[1:])
    # 脚本自己的目录要进 sys.path：probe_tier.py / run_server.py 都会 `import paths`，
    # 而 runpy.run_path **不会**替你加（不加就是 ModuleNotFoundError）。
    sdir = os.path.dirname(os.path.abspath(script))
    if sdir not in sys.path:
        sys.path.insert(0, sdir)

    saved_argv = sys.argv
    sys.argv = [script] + rest        # 脚本按"自己被直接运行"来解析参数
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:           # 脚本自己 sys.exit(code) → 透传退出码
        if e.code is None:
            return 0
        return e.code if isinstance(e.code, int) else 1
    except BaseException:             # noqa: BLE001 —— 含语法错误 / KeyboardInterrupt
        return _script_log(traceback.format_exc())
    finally:
        sys.argv = saved_argv
    return 0


def _excepthook(exc_type, exc_value, exc_tb):
    """把未捕获异常显示为对话框，避免界面直接退出"""
    from PyQt6.QtWidgets import QMessageBox
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(text, file=sys.stderr)
    try:
        QMessageBox.critical(None, "程序异常", text[-1500:])
    except Exception:
        pass


def main():
    """主函数（起界面）"""
    # Qt 与界面模块的导入放在这里：`--script` 跑随包脚本时**不该**依赖 Qt/显示器，
    # 而模块级 import 会在任何分支之前就把 PyQt6 拉起来（打包态慢，且脚本用不上）。
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt

    from ui.main_window import MainWindow

    # 启用高DPI支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("NetOpsTerminal")
    app.setApplicationVersion("0.7.0")
    app.setOrganizationName("NetOps Terminal Team")

    # 进程级图标：窗口、各对话框、任务栏都继承它。
    # 换图标就替换 resources\icons\app.ico（多尺寸 16/32/48/64/128/256，exe 图标同源）。
    from PyQt6.QtGui import QIcon
    from core import app_paths

    # Windows 任务栏图标：**必须**设置 AppUserModelID。
    # 不设置时任务栏显示的是宿主进程的图标（脚本方式启动就是 python.exe 的白图标），
    # 而不是本应用自己的图标 —— 用户反馈"启动后任务栏这里也要改"就是这个。
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "NetOpsTerminal.Terminal.0.7"
            )
        except Exception:
            pass

    # 图标要兜两处路径：打包态 base_dir() 返回 exe 所在目录，
    # 而 PyInstaller 的 datas 实际解压在 sys._MEIPASS ——
    # 只认第一处会出现"资源已经打进包了却静默没图标"（if os.path.exists 直接跳过）。
    # Linux 下 .ico 支持不稳（部分 Qt 构建只画第一帧），优先 app.png。
    icon_files = ("app.ico", "app.png") if sys.platform == "win32" else ("app.png", "app.ico")
    icon_dirs = [
        os.path.join(app_paths.base_dir(), "resources", "icons"),
        os.path.join(getattr(sys, "_MEIPASS", "") or "", "resources", "icons"),
    ]
    for _dir in icon_dirs:
        picked = None
        for _name in icon_files:
            _p = os.path.join(_dir, _name) if _dir else ""
            if _p and os.path.exists(_p):
                picked = _p
                break
        if picked:
            app.setWindowIcon(QIcon(picked))
            break

    sys.excepthook = _excepthook

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    # `--script` 必须在**任何 Qt/GUI 之前**处理：打包态下这是跑随包脚本的唯一通道
    if "--script" in sys.argv[1:]:
        _i = sys.argv.index("--script", 1)
        sys.exit(_run_script(sys.argv[_i + 1:]))
    main()

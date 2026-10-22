"""生成 README 用的演示截图。

原则：**不含任何真实设备信息**。
- 设备库只放本机 127.0.0.1 的模拟设备（`tools/fake_device.py` 起的假交换机）；
- 运行前把 `USERPROFILE` 指到临时目录，`~/.aiterminal/settings.json` 是空的，
  不会读到用户真实的设备库、日志、资料库。

跑法（Windows，先在另一个终端起模拟设备）：
    python tools/fake_device.py --port 2323 --no-login --paging
    python main.py --script tools/make_readme_shots.py

产物：`docs/screenshots/01-main.png`（已连接会话）、`02-ai-panel.png`（AI 面板展开）。
"""
import os
import sys
import time

# 默认用真实平台：offscreen 平台在这台机器上找不到字体目录，截图里全是方框（已实测）。
# 只有显式设置 AIT_SHOT_OFFSCREEN=1 才走离屏。
if os.environ.get("AIT_SHOT_OFFSCREEN") == "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402

OUT = os.path.join(ROOT, "docs", "screenshots")

DEMO_DEVICES = [
    {"name": "实验室-模拟交换机", "protocol": "Telnet", "host": "127.0.0.1",
     "port": 2323, "username": "admin"},
    {"name": "实验室-模拟路由器", "protocol": "Telnet", "host": "127.0.0.1",
     "port": 2324, "username": "admin"},
]

DEMO_SESSION = {
    "name": "实验室-模拟交换机",
    "protocol": "Telnet",
    "host": "127.0.0.1",
    "port": 2323,
    "username": "admin",
    "password": "",
}


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.03)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("NetOpsTerminal")

    win = MainWindow()
    win.resize(1600, 900)
    win.show()
    pump(app, 0.5)

    win.settings.set("vault_devices", DEMO_DEVICES)
    win.settings.save()
    win.vault_panel.refresh()
    pump(app, 0.5)

    win.create_session(dict(DEMO_SESSION))
    deadline = time.time() + 25
    session_id = None
    while time.time() < deadline and session_id is None:
        app.processEvents()
        time.sleep(0.05)
        ids = list(getattr(win, "tabs", {}).keys())
        session_id = ids[0] if ids else None
    print("session_id =", session_id, flush=True)
    if session_id is None:
        print("连接失败：先起 python tools/fake_device.py --port 2323 --no-login --paging",
              flush=True)
        return 1

    # 等假设备把 banner / 提示符吐完，画面有内容再截
    pump(app, 4.0)

    # 敲两条命令，让画面像真实会话（假设备对 display 类命令有 H3C 风格回显）
    for cmd in ("display version", "display interface brief"):
        win.session_manager.send_command(session_id, cmd)
        pump(app, 1.8)

    os.makedirs(OUT, exist_ok=True)
    shot1 = os.path.join(OUT, "01-main.png")
    win.grab().save(shot1)
    print("saved", shot1, flush=True)

    win.set_ai_panel_visible(True)
    pump(app, 1.5)
    shot2 = os.path.join(OUT, "02-ai-panel.png")
    win.grab().save(shot2)
    print("saved", shot2, flush=True)

    # 顺带把窗口尺寸记下来，README 里写实际分辨率不是猜的
    print("window =", win.width(), "x", win.height(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

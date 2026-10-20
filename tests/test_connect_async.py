"""连接不能冻结界面（用户 2026-10-17 反馈：「点击连接后软件卡死，过一会才加载进去」）。

跑法：python tests\test_connect_async.py
判据：① 慢连接下 create_session 立刻返回；② 连接期间界面线程事件循环照常转；
      ③ 连接结束后状态清空、耗时被记进 aiterminal-slow.log。
不碰真硬件：把 session_manager.create_session 换成"睡 2 秒"的假实现。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


from ui.main_window import MainWindow                        # noqa: E402

print("=== 连接异步化（界面不冻结）===")
window = MainWindow()
SLOW = 2.0
seen = {}


def fake_create_session(**kwargs):
    seen['started'] = time.monotonic()
    seen['kwargs'] = kwargs
    time.sleep(SLOW)                    # 模拟 SSH/Telnet/串口握手
    return 12345


window.session_manager.create_session = fake_create_session

t0 = time.monotonic()
window.create_session({'name': '测试连接', 'protocol': 'SSH', 'host': '10.0.0.1'})
cost = time.monotonic() - t0
check(f"create_session 立刻返回（{cost * 1000:.0f} ms < 300 ms）", cost < 0.3,
      f"实耗 {cost * 1000:.0f} ms")
task = window._connect_task
check("连接跑在工作线程里（QThread.isRunning）",
      task is not None and task.isRunning())

ticks = 0
t_end = time.monotonic() + SLOW * 0.4
while time.monotonic() < t_end:          # 连接期间界面线程应当照常处理事件
    app.processEvents()
    ticks += 1
    time.sleep(0.01)
check(f"连接期间事件循环照常转（{ticks} 次）", ticks > 3)
check("连接参数原样传给了连接器（含串口项）",
      seen.get('kwargs', {}).get('host') == '10.0.0.1'
      and 'local_echo' in seen.get('kwargs', {}))

while window._connect_task is not None and time.monotonic() < t0 + SLOW + 8:
    app.processEvents()
    time.sleep(0.02)
check("连接结束后 _connect_task 已清空", window._connect_task is None)

log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "aiterminal-slow.log")
logged = ""
if os.path.exists(log_path):
    with open(log_path, "r", encoding="utf-8") as fh:
        logged = fh.read()
check("连接耗时被记进 aiterminal-slow.log", "连接耗时" in logged,
      logged.strip().splitlines()[-1] if logged.strip() else "(日志为空)")

print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
sys.exit(1 if FAIL else 0)

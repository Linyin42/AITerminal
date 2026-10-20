"""聚焦探针：只跑 GUI 快照路径，把 worker 发到 **AI 面板** 的消息读出来。

背景：G18 报"快照 0 → 0；界面提示=[]"，但 gui_drive_test.py 是从**终端控件**里
grep 提示的，而 _snapshot_worker 的提示走 session_manager.ai_output（AI 面板）——
读错控件 ⇒ 真实失败原因一直没被读出来。
这里直接连 ai_output 信号，并调用**真实的 worker 函数**（不是复刻）。
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                        # noqa: E402
from ui.main_window import MainWindow                           # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

HOST = os.environ.get("ENSP_HOST", "192.168.31.11")
SB = int(os.environ.get("ENSP_SB", "2"))
NODE = int(os.environ.get("ENSP_NODE", "22"))
PWD = os.environ.get("ENSP_PWD", "Huawei@123")
CMD = os.environ.get("ENSP_CMD", "display current-configuration")

print(f"=== GUI 快照路径聚焦探针：{HOST} 沙箱{SB} 设备{NODE} ===", flush=True)
win = MainWindow()
print("MainWindow 已创建", flush=True)

msgs = []
win.session_manager.ai_output.connect(
    lambda sid, text: (msgs.append(text), print(f"[AI面板] {text}", flush=True)))

sid = win.session_manager.create_session(
    "快照GUI", "eNSP", host=HOST, username=os.environ.get("ENSP_USER", "admin"),
    password=PWD, sandbox_id=SB, node_id=NODE)
print(f"create_session → {sid}", flush=True)
sess = win.session_manager.get_session(sid)
if not sess:
    print("拿不到会话，退出", flush=True)
    sys.exit(1)

t = threading.Thread(target=win._snapshot_worker,
                     args=(sid, CMD), daemon=True)
t.start()

deadline = time.time() + 70
while time.time() < deadline and t.is_alive():
    app.processEvents()
    time.sleep(0.1)
t.join(timeout=5)

print(f"worker 结束（存活={t.is_alive()}）", flush=True)
print(f"snapshots = {len(getattr(sess, 'snapshots', []) or [])} 份", flush=True)
print(f"AI 面板消息 {len(msgs)} 条：{msgs}", flush=True)
print(f"状态栏 = {win.status_bar.currentMessage()!r}", flush=True)

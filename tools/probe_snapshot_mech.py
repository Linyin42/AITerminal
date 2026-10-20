"""最小探针：直接测快照的采集机制（start_capture / capture_size / stop_capture）。

为什么需要它：GUI 测试的 G18 报"快照 0 → 0"，但它的"界面提示"是从**终端控件**里
grep 的，而 _snapshot_worker 的提示是发到 **AI 面板**（ai_output.emit）——
读错控件 ⇒ 那条提示是假阴性，真正的失败原因被吞掉了。
这里绕开界面，直接复刻 worker 的核心，把采集链每一步都打出来。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                        # noqa: E402
from core.session import SessionManager                          # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv)

HOST = os.environ.get("ENSP_HOST", "192.168.31.11")
SB = int(os.environ.get("ENSP_SB", "2"))
NODE = int(os.environ.get("ENSP_NODE", "22"))
PWD = os.environ.get("ENSP_PWD", "Huawei@123")
CMD = os.environ.get("ENSP_CMD", "display current-configuration")
SETTLE = float(os.environ.get("ENSP_SETTLE", "1.5"))
TIMEOUT = float(os.environ.get("ENSP_TIMEOUT", "25"))

print(f"=== 快照采集机制探针：{HOST} 沙箱{SB} 设备{NODE} ===", flush=True)
mgr = SessionManager()
t0 = time.monotonic()
sid = mgr.create_session("快照机制", "eNSP", host=HOST,
                         username=os.environ.get("ENSP_USER", "admin"),
                         password=PWD, sandbox_id=SB, node_id=NODE)
print(f"create_session → {sid}（{time.monotonic() - t0:.1f}s）", flush=True)
sess = mgr.get_session(sid)
if not sess:
    print("拿不到会话，退出", flush=True)
    sys.exit(1)

sess.start_capture()
print("start_capture() 完成", flush=True)
ok = mgr.send_command(sid, CMD)
print(f"send_command → {ok}", flush=True)

start = time.time()
last_len = 0
last_change = time.time()
while time.time() - start < TIMEOUT:
    cur = sess.capture_size()
    if cur != last_len:
        print(f"  +{cur - last_len} 字节（累计 {cur}，t={time.time() - start:.1f}s）", flush=True)
        last_len = cur
        last_change = time.time()
    elif time.time() - last_change >= SETTLE:
        break
    time.sleep(0.2)

text = sess.stop_capture()
print(f"stop_capture() → {len(text)} 字符（判定稳定用时 {time.time() - start:.1f}s）", flush=True)
print("--- 前 300 字符 ---", flush=True)
print(text[:300] if text.strip() else "（空！）", flush=True)
print("--- 末尾 200 字符 ---", flush=True)
print(repr(text[-200:]), flush=True)

sess.add_snapshot(f"{CMD} @ probe", text)
print(f"add_snapshot 后 snapshots={len(sess.snapshots)} 份", flush=True)

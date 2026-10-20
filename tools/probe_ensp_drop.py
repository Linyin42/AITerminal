r"""eNSP 连接是不是"发过重命令之后自己掉线"（GUI 测试里 G18 快照 0→0 的根因）。

跑法：python -u tools\probe_ensp_drop.py
判据：每发一条命令后打印 send 结果 / is_connected() / 连接器 last_error。
     只要有一条命令之后 is_connected() 变 False，就复现了"静默掉线"。

背景：core/ensp_connector.py:413-418 —— 任何一次发送异常都会把 _connected 置 False，
之后 is_connected() 恒为 False 且不会自愈，快照/巡检/终端全都会无声失败。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication                  # noqa: E402
from core.session import SessionManager                        # noqa: E402

# SessionManager 内部要创建 Qt 对象并靠信号回传结果；
# 没有 QApplication 实例时这条路径会卡到内部超时、create_session 返回 -1
# （现象：固定 30.0s、-1，而直接调连接器 connect() 却成功）。
# offscreen 平台下建一个即可，不需要窗口。
_APP = QApplication.instance() or QApplication(sys.argv)

HOST = os.environ.get("ENSP_HOST", "192.168.31.11")
SB = int(os.environ.get("ENSP_SB", "2"))
NODE = int(os.environ.get("ENSP_NODE", "22"))
PWD = os.environ.get("ENSP_PWD", "Huawei@123")

COMMANDS = [
    "display version",
    "display device",
    "display current-configuration",
    "display device",
]

print(f"=== eNSP 掉线探针：{HOST} 沙箱{SB} 设备{NODE} ===")
mgr = SessionManager()
t0 = time.monotonic()
# username 必须给：连接器内部 login(username, password) 走平台免密登录。
# port 默认不传，用来验证 core/session.py 里"eNSP 分支把 22 当未指定、落到 8443"的修复；
# 需要覆盖时用 ENSP_PORT 显式指定。
_extra = {}
if os.environ.get("ENSP_PORT"):
    _extra["port"] = int(os.environ["ENSP_PORT"])
sid = mgr.create_session("掉线探针", "eNSP", host=HOST,
                         username=os.environ.get("ENSP_USER", "admin"),
                         password=PWD,
                         sandbox_id=SB, node_id=NODE, **_extra)
print(f"create_session → {sid}（用时 {time.monotonic() - t0:.1f}s）")

sess = mgr.get_session(sid)
conn = sess.connector if sess else None
if conn is None:
    print("没拿到连接器，探针无法继续")
    raise SystemExit(2)

# 连接建立后先看一次
for _ in range(60):
    if conn.is_connected():
        break
    time.sleep(0.5)
print(f"连接建立: is_connected={conn.is_connected()} "
      f"last_error={getattr(conn, '_last_error', '')!r}")

for index, command in enumerate(COMMANDS, start=1):
    t0 = time.monotonic()
    try:
        ok = mgr.send_command(sid, command)
    except Exception as e:                                     # noqa: BLE001
        ok = f"异常 {type(e).__name__}: {e}"
    time.sleep(3.0)                                            # 等回显落定
    print(f"[{index}] {command:32s} send={ok} 用时={time.monotonic() - t0:5.1f}s "
          f"is_connected={conn.is_connected()} "
          f"last_error={getattr(conn, '_last_error', '')!r} "
          f"buffer={len(getattr(conn, '_buffer', '') or '')} 字节")

print("=== 结束 ===")
try:
    conn.disconnect()
except Exception:                                              # noqa: BLE001
    pass

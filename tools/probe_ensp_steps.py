"""逐步诊断：完全复刻连接器的登录时序，但**每一步都打印缓冲区**。

背景：probe_ensp_why.py 只说"设备未接受密码"，看不到设备到底答了什么。
这里把时序拆开：连上 → 回车 → 等 Password → 发密码 → dump → 补回车 → dump → 发命令 → dump。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ensp_connector import EnspProConnector  # noqa: E402

HOST = os.environ.get("ENSP_HOST", "192.168.31.11")
SB = int(os.environ.get("ENSP_SB", "5"))
NODE = int(os.environ.get("ENSP_NODE", "29"))
USER = os.environ.get("ENSP_USER", "admin")
PWD = os.environ.get("ENSP_TRY_PWD", "huawei@123")


def dump(c, label):
    with c._lock:
        buf = c._buffer
    tail = buf[-260:].replace("\r", "")
    print(f"[{label}] 缓冲={len(buf)} 字符，末尾={tail!r}", flush=True)


print(f"=== 逐步登录诊断：{HOST} 沙箱{SB} 设备{NODE} ===", flush=True)
c = EnspProConnector()
ok = c.connect(host=HOST, port=8443, username=USER, password="",
               sandbox_id=SB, node_id=NODE, device_password="")
print(f"connect -> {ok}", flush=True)
if not ok:
    print(f"last_error={c.last_error!r}", flush=True)
    sys.exit(1)

dump(c, "连上后")
print(">>> 发回车（前端要求第一下必须是 \\r）", flush=True)
c.send_raw("\r")
time.sleep(6)
dump(c, "回车后")

print(f">>> 发密码 {PWD!r}", flush=True)
c.send_raw(PWD + "\r")
time.sleep(10)
dump(c, "发密码后")

print(">>> 再补一个回车（Press ENTER 场景）", flush=True)
c.send_raw("\r")
time.sleep(6)
dump(c, "补回车后")

print(">>> screen-length 0 temporary", flush=True)
c.send_raw("screen-length 0 temporary\r")
time.sleep(6)
dump(c, "关分页后")

print(">>> display version", flush=True)
c.send_raw("display version\r")
time.sleep(8)
dump(c, "display version 后")

print(">>> get_output() 原样：", flush=True)
print((c.get_output() or "（空）")[:1200], flush=True)
c.disconnect()

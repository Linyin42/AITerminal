# -*- coding: utf-8 -*-
"""端到端测 eNSP Pro 设备连接 —— 用应用自己的连接器（core/ensp_connector.py）。

沙箱/设备来源（实测）:
    POST /service/ensp/sandbox/getByPage
         body={"projectId":1,"currentPage":1,"pageSize":20}
    返回 data[].id = sandbox_id，data[].devices[].id = node_id

用法:
    python -u tools\\probe_ensp_console.py [host] [port] [sandbox_id] [node_id] [device_password]
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.ensp_connector import EnspProConnector              # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.11"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
sandbox = int(sys.argv[3]) if len(sys.argv) > 3 else 2
node = int(sys.argv[4]) if len(sys.argv) > 4 else 22
dev_pwd = sys.argv[5] if len(sys.argv) > 5 else "Huawei@123"

conn = EnspProConnector()
t0 = time.time()
ok = conn.connect(host=host, port=port, sandbox_id=sandbox, node_id=node,
                  device_password=dev_pwd)
print(f"connect -> {ok}  用时 {time.time() - t0:.1f}s", flush=True)
print(f"last_error = {getattr(conn, '_last_error', '')}", flush=True)
print(f"connected = {getattr(conn, 'connected', None)}  "
      f"auto_login = {getattr(conn, '_auto_login', None)}", flush=True)

time.sleep(1.5)
raw = conn.get_output()
print("--- 连接后的原始回显（尾部 2000 字）---", flush=True)
print(raw[-2000:] if raw else "(空)", flush=True)

for cmd in ("\r", "display version\r", "display device\r"):
    conn.send_raw(cmd)
    time.sleep(4.0)
    out = conn.get_output()
    print(f"--- 发送 {cmd!r} 之后 ---", flush=True)
    print(out[-2000:] if out else "(空)", flush=True)

for name in ("disconnect", "close", "stop"):
    fn = getattr(conn, name, None)
    if callable(fn):
        try:
            fn()
            print(f"已断开（{name}）", flush=True)
        except Exception as exc:
            print(f"断开出错（{name}）: {exc}", flush=True)
        break

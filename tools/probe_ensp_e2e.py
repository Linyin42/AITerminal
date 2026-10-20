# -*- coding: utf-8 -*-
"""端到端：经应用的接管通道（control_api）连 eNSP Pro 设备。

链路与界面上点「连接」完全一致：
    POST /session/open  {protocol: ensp, host, port, sandbox_id, node_id, password}
      → core/session.py → core/ensp_connector.py（HTTP + SockJS/STOMP）→ eNSP Pro 平台
    POST /session/send  发命令并取回显
    POST /session/close 收尾

用法: python -u tools\\probe_ensp_e2e.py [host] [port] [sandbox_id] [node_id] [device_password]
"""
import json
import os
import sys

import requests

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.11"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8443
sandbox = int(sys.argv[3]) if len(sys.argv) > 3 else 2
node = int(sys.argv[4]) if len(sys.argv) > 4 else 22
dev_pwd = sys.argv[5] if len(sys.argv) > 5 else "Huawei@123"

FAIL = []


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (f"   {detail}" if detail else ""), flush=True)
    if not cond:
        FAIL.append(name)


ctl = os.path.join(os.path.expanduser("~"), ".aiterminal", "control.json")
with open(ctl, encoding="utf-8") as fh:
    conf = json.load(fh)
url, token = conf["url"], conf["token"]
headers = {"Authorization": f"Bearer {token}"}
print(f"接管通道 {url}（应用需已启动且开启接管）", flush=True)

print("== ① 打开会话：协议 ensp + 沙箱/设备编号 ==", flush=True)
body = {"protocol": "ensp", "host": host, "port": port,
        "sandbox_id": sandbox, "node_id": node,
        "password": dev_pwd, "key": "ensp-e2e", "name": "eNSP E2E"}
resp = requests.post(url + "/session/open", json=body, headers=headers, timeout=180)
data = resp.json()
check("open 未报错", not data.get("error"), str(data)[:220])
check("state = connected", data.get("state") == "connected", str(data.get("state")))
if data.get("detail"):
    print(f"    detail = {data['detail']}", flush=True)

print("== ② 发命令 display version ==", flush=True)
resp = requests.post(url + "/session/send",
                     json={"key": "ensp-e2e", "command": "display version",
                           "timeout_ms": 40000}, headers=headers, timeout=90)
data = resp.json()
output = data.get("output") or ""
check("命令已发送", bool(data.get("sent")), str(data.get("state")))
check("回显里有版本信息",
      any(k in output for k in ("Version", "VRP", "YunShan", "uptime")),
      output[-160:].replace("\n", " | "))
print("----- 设备回显（尾部 800 字）-----", flush=True)
print(output[-800:], flush=True)

print("== ③ 关闭会话 ==", flush=True)
resp = requests.post(url + "/session/close", json={"key": "ensp-e2e"},
                     headers=headers, timeout=30)
check("close 成功", resp.status_code == 200, str(resp.status_code))

print(f"\n结果: {'全绿' if not FAIL else '失败 ' + str(FAIL)}", flush=True)
sys.exit(1 if FAIL else 0)

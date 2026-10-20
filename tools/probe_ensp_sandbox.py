# -*- coding: utf-8 -*-
"""列 eNSP Pro 的沙箱与设备，拿到连接器需要的 sandboxId / nodeId。

前端 bundle 里挖到的接口族（/service/ensp/...）：
    GET  /service/ensp/project/getUserProjects
    GET  /service/ensp/sandbox/getLatestUsedSandbox
    POST /service/ensp/sandbox/getByPage            （body 是分页参数，形状未知，试两种）
    GET  /service/ensp/sandbox/getOneSandbox?sandboxId=N
    GET  /service/ensp/sandbox/getAvailableDevice?sandboxId=N    ← 设备 + nodeId
    GET  /service/ensp/device

用法: python -u tools\\probe_ensp_sandbox.py [host] [port]
"""
import json
import re
import sys

import requests
import urllib3

urllib3.disable_warnings()

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.11"
port = sys.argv[2] if len(sys.argv) > 2 else "8443"
base = f"https://{host}:{port}"

s = requests.Session()
s.verify = False


def call(method, path, **kw):
    try:
        r = s.request(method, base + path, timeout=25, **kw)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, (r.text or "")[:200]
    except Exception as exc:
        return None, f"<{exc}>"


def brief(data, n=700):
    if isinstance(data, str):
        return data[:n]
    return json.dumps(data, ensure_ascii=False)[:n]


def scan_ids(obj, key_re=r"(sandboxId|^id$)", out=None):
    """从任意 JSON 里把所有像沙箱 id 的值捞出来"""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, str)) and re.search(key_re, str(k)):
                out.add(v)
            else:
                scan_ids(v, key_re, out)
    elif isinstance(obj, list):
        for it in obj:
            scan_ids(it, key_re, out)
    return out


print(f"目标 {base}", flush=True)
code, _ = call("GET", f"/api/login?service={base}")
print(f"登录 GET /api/login -> {code}", flush=True)
code, info = call("GET", "/api/getUserInfo")
print(f"用户 -> {code} {brief(info, 260)}", flush=True)

print("\n== 工程 / 沙箱列表 ==", flush=True)
ids = set()
for m, p, kw in (
    ("GET", "/service/ensp/project/getUserProjects", {}),
    ("GET", "/service/ensp/sandbox/getLatestUsedSandbox", {}),
    ("POST", "/service/ensp/sandbox/getByPage", {"json": {"page": 0, "size": 20}}),
    ("POST", "/service/ensp/sandbox/getByPage", {"json": {"pageNum": 1, "pageSize": 20}}),
    ("POST", "/service/ensp/sandbox/getByPage", {"json": {}}),
    ("GET", "/service/ensp/sandbox/getByPage", {}),
    ("GET", "/service/ensp/device", {}),
):
    code, data = call(m, p, **kw)
    extra = f" body={kw['json']}" if "json" in kw else ""
    print(f"  {m} {p}{extra} -> {code} {brief(data)}", flush=True)
    if code == 200:
        ids |= scan_ids(data)

print(f"\n捞到的候选沙箱 id: {sorted(map(str, ids))}", flush=True)

print("\n== 逐个沙箱看设备 ==", flush=True)
for sid in sorted(map(str, ids))[:4]:
    for p in (f"/service/ensp/sandbox/getOneSandbox?sandboxId={sid}",
              f"/service/ensp/sandbox/getAvailableDevice?sandboxId={sid}",
              f"/service/ensp/device/getBySandboxId?sandboxId={sid}"):
        code, data = call("GET", p)
        print(f"  GET {p} -> {code} {brief(data, 1200)}", flush=True)

print("\n== 更多候选接口（含分页体形状猜测）==", flush=True)
for m, p, kw in (
    ("POST", "/service/ensp/sandbox/getByPage",
     {"json": {"currentPage": 1, "pageSize": 20}}),
    ("POST", "/service/ensp/sandbox/getLatestUsedSandbox",
     {"json": {"currentPage": 1, "pageSize": 10}}),
    ("POST", "/service/ensp/sandbox/demo/getByPage",
     {"json": {"currentPage": 1, "pageSize": 10}}),
    ("GET", "/service/ensp/sandbox/getAvailableDevices", {}),
    ("GET", "/service/ensp/sandbox/cloudSandboxList", {}),
    ("GET", "/service/ensp/sandbox/demo/getByPage", {}),
    ("POST", "/service/ensp/sandbox/demo/getByPage",
     {"json": {"pageNum": 1, "pageSize": 20}}),
    ("POST", "/service/ensp/sandbox/getLatestUsedSandbox", {"json": {}}),
    ("POST", "/service/ensp/sandbox/getByPage",
     {"json": {"projectId": 1, "pageNum": 1, "pageSize": 20}}),
    ("POST", "/service/ensp/sandbox/getByPage",
     {"json": {"projectId": 1, "currentPage": 1, "pageSize": 20}}),
    ("POST", "/service/ensp/sandbox/getByPage",
     {"json": {"sandboxName": "", "projectId": 1, "pageNum": 1, "pageSize": 20}}),
    ("POST", "/service/ensp/sandbox/getByPage",
     {"json": {"page": {"pageNum": 1, "pageSize": 20}, "projectId": 1}}),
):
    code, data = call(m, p, **kw)
    print(f"  {m} {p} {kw.get('json', '')} -> {code} {brief(data, 900)}", flush=True)

print("\n== 连接器需要的参数 ==", flush=True)
print("  host = " + host, flush=True)
print("  port = " + str(port), flush=True)
print("  sandbox_id / node_id = 从上面 getAvailableDevice 的返回里取 "
      "（nodeId 字段就是连接器的 node_id）", flush=True)

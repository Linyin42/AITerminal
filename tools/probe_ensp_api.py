# -*- coding: utf-8 -*-
"""探 eNSP Pro 的 REST 接口：登录 → 看会话 → 列工程/沙箱 → 找设备的 sandboxId/nodeId。

背景：连接器（core/ensp_connector.py）需要 host + sandbox_id + node_id 三个参数，
但界面拿不到后两个。前端 bundle 里只挖到 /api/getAllProject 这类通用接口，
沙箱/节点接口要么是模板字符串拼的、要么在别的路径下 —— 本探针把候选逐个试一遍，
并把 bundle 里所有跟 sandbox/node/web.socket 有关的字面量打出来。

用法: python -u tools\\probe_ensp_api.py [host] [port] [bundle_path]
"""
import json
import os
import re
import sys

import requests
import urllib3

urllib3.disable_warnings()

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.31.11"
port = sys.argv[2] if len(sys.argv) > 2 else "8443"
bundle = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
    os.environ.get("TEMP", "."), "ensp_js_1.js")
base = f"https://{host}:{port}"

s = requests.Session()
s.verify = False


def get(path, **kw):
    try:
        r = s.get(base + path, timeout=25, **kw)
        return r.status_code, r.text
    except Exception as exc:
        return None, f"<{exc}>"


print(f"目标 {base}", flush=True)

print("\n== 1) 登录 ==", flush=True)
code, text = get(f"/api/login?service={base}")
print(f"  GET /api/login?service=... -> {code} {(text or '')[:160]}", flush=True)
code, text = get("/api/getUserInfo")
print(f"  GET /api/getUserInfo -> {code} {(text or '')[:300]}", flush=True)
print(f"  cookies = {dict(s.cookies)}", flush=True)

print("\n== 2) 工程/沙箱 ==", flush=True)
for path in ("/api/getAllProject", "/api/getAllSandbox", "/api/sandbox",
             "/api/sandbox/list", "/api/getSandboxList", "/api/project",
             "/api/getProject", "/api/topology", "/api/node", "/api/device"):
    code, text = get(path)
    body = (text or "").replace("\n", " ")[:400]
    print(f"  GET {path} -> {code} {body}", flush=True)

if os.path.exists(bundle):
    raw = open(bundle, encoding="utf-8", errors="replace").read()
    print(f"\n== 3) bundle（{len(raw)} 字符）里的相关字面量 ==", flush=True)
    pats = [
        r'["\'`][^"\'`]{0,70}sandbox[^"\'`]{0,70}["\'`]',
        r'["\'`]/[A-Za-z0-9_/\-{}.:$]*node[A-Za-z0-9_/\-{}.:$]*["\'`]',
        r'["\'`][^"\'`]{0,60}web\.socket[^"\'`]{0,60}["\'`]',
        r'["\'`]/api/[A-Za-z0-9_/\-{}.:$]+["\'`]',
        r'["\'`]/service/[A-Za-z0-9_/\-{}.:$]+["\'`]',
    ]
    seen = set()
    for pat in pats:
        for m in re.findall(pat, raw, re.I):
            if m not in seen:
                seen.add(m)
                print("  ", m, flush=True)
else:
    print(f"\n== 3) 跳过：没找到 bundle {bundle} ==", flush=True)

print("\n== 4) 已知的 STOMP 地址族（连接器里用的）==", flush=True)
print(f"  wss://{host}:{port}/web.socket/000/<random>/websocket", flush=True)
print(f"  订阅 /user/topic/console/message/<sandboxId>/<nodeId>", flush=True)
print(f"  发送 /app/console/connect/<sandboxId>/<nodeId>", flush=True)

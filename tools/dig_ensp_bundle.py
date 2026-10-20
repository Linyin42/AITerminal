# -*- coding: utf-8 -*-
"""从 eNSP Pro 前端 bundle 里挖接口调用点（含请求体形状）。

minified 代码里对象字面量的方法名还留着，配合 ±N 字符的上下文就能看出
body 里有哪些键。用法:
    python -u tools\\dig_ensp_bundle.py [bundle_path] [关键词1 关键词2 ...]
"""
import os
import re
import sys

bundle = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("TEMP", "."), "ensp_js_1.js")
keys = sys.argv[2:] or [
    "getSandboxByPage", "createSandbox", "getAvailableDevice",
    "startDevice", "nodeId", "getLatestUsedSandbox",
]
WINDOW = 380

raw = open(bundle, encoding="utf-8", errors="replace").read()
print(f"bundle {bundle}（{len(raw)} 字符）\n", flush=True)

for key in keys:
    hits = [m.start() for m in re.finditer(re.escape(key), raw)]
    print(f"=== {key}：{len(hits)} 处 ===", flush=True)
    seen = set()
    for pos in hits[:8]:
        seg = raw[max(0, pos - WINDOW): pos + WINDOW]
        # 压掉多余空白，便于阅读
        seg = re.sub(r"\s+", " ", seg)
        if seg[:120] in seen:
            continue
        seen.add(seg[:120])
        print(f"  …{seg}…\n", flush=True)

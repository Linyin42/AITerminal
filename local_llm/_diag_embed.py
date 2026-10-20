# -*- coding: utf-8 -*-
r"""诊断：为什么某些片段嵌入会 500（拿服务端原始响应体）。

python 的 urllib 在 HTTPError 里把响应体藏起来，看不到服务端到底抱怨什么，
所以这里直接读 HTTPResponse / HTTPError 的 body 原文。
"""
import json
import os
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:8081/v1/embeddings"
KEY = "local-llm"
import paths  # noqa: E402  （同目录；路径的唯一来源见 paths.py）
CHUNKS = os.path.join(paths.INDEX, "h3c_chunks.jsonl")


def post(texts, timeout=180):
    data = json.dumps({"input": texts, "model": "bge-m3"},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(URL, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def main():
    texts = []
    with open(CHUNKS, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= 5:
                break
            texts.append(json.loads(line)["text"])

    print("=== 前 5 个片段的字符数 ===")
    for i, t in enumerate(texts):
        print(f"  [{i}] {len(t)} 字符")

    print("\n=== 逐个单独嵌入 ===")
    for i, t in enumerate(texts):
        code, body = post([t])
        print(f"  [{i}] len={len(t)}  HTTP {code}  {body[:200]}")

    print("\n=== 短文本对照（应 200）===")
    code, body = post(["测试"])
    print(f"  HTTP {code}  {body[:120]}")

    print("\n=== 二分：把最长的片段截断到不同长度 ===")
    longest = max(texts, key=len)
    for cut in (500, 1000, 2000, 3000, 4000):
        code, body = post([longest[:cut]])
        flag = "OK" if code == 200 else "FAIL"
        print(f"  截到 {cut} 字符 -> HTTP {code} {flag}  {body[:120]}")


if __name__ == "__main__":
    main()

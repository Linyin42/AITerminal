# -*- coding: utf-8 -*-
"""NetOpsTerminal MCP 桥的端到端自测（脚本式用例，不用 pytest）。

守四件事 —— 每一条都"真起进程、真过 HTTP"，不 mock 掉要验证的东西：
  ① 真起进程：把 `data_tools/mcp/aiterminal_mcp.py` 当子进程，JSON-RPC 逐行走 stdin/stdout；
  ② 真过 HTTP：本地 `http.server` 假后端实现契约里的全部端点，并记录请求头/请求体；
  ③ 真验协议：initialize 握手、tools/list 10 个工具、每个工具调一次看返回文本；
  ④ 真验边界：Bearer 令牌、空参数不外送、回显原文无包装、-32601、EOF 退出码 0、
     无 traceback、control.json 缺失时的中文报错。

跑法：python -u tests\\test_mcp_bridge.py
"""
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(REPO, "data_tools", "mcp", "aiterminal_mcp.py")
TOKEN = "test-token-123"

PASS, FAIL = [], []

EXPECTED_TOOLS = ["ait_status", "ait_sessions", "ait_open", "ait_send", "ait_read",
                  "ait_raw", "ait_interrupt", "ait_close",
                  "ait_knowledge_search", "ait_knowledge_stats"]

SESSION_H3C = {"key": "h3c-1", "sid": 3, "name": "核心交换机", "protocol": "ssh",
               "host": "10.0.0.1", "port": 22, "connected": True}
SEND_OUTPUT = ("<H3C>display version\r\n"
               "H3C Comware Software, Version 7.1.070, Release 3506P02\r\n"
               "[H3C]")
READ_TEXT = "新增输出：GigabitEthernet1/0/1  up\n"
LONG_SNIPPET = "VLAN 配置示例：" + "接口下 VLAN 的说明文字，" * 30      # 远超 300 字


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("   " + detail) if detail else ""),
          flush=True)


# --------------------------------------------------------------------------- #
# ① 假后端：实现契约里的全部端点，记录每个请求的头与体
# --------------------------------------------------------------------------- #
def start_fake_backend():
    state = {"requests": [], "sessions": [dict(SESSION_H3C)], "lock": threading.Lock()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):                       # 别往测试输出里灌日志
            pass

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def _send(self, obj, status=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            with state["lock"]:
                state["requests"].append({
                    "method": self.command, "path": u.path, "query": q, "body": body,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "raw_body": raw.decode("utf-8", "replace")})
            m, p = self.command, u.path
            if m == "GET" and p == "/ping":
                return self._send({"ok": True, "app": "NetOps Terminal", "version": "0.7.0"})
            if m == "GET" and p == "/status":
                return self._send({"app": "NetOps Terminal", "version": "0.7.0", "pid": 1234,
                                   "sessions": list(state["sessions"])})
            if m == "GET" and p == "/session/list":
                return self._send({"sessions": list(state["sessions"])})
            if m == "POST" and p == "/session/open":
                key = body.get("key") or body.get("name") or "auto-1"
                if not any(s["key"] == key for s in state["sessions"]):
                    state["sessions"].append({
                        "key": key, "sid": 9, "name": body.get("name") or key,
                        "protocol": body.get("protocol") or "ssh",
                        "host": body.get("host") or "", "port": body.get("port"),
                        "connected": True})
                return self._send({"key": key, "sid": 3, "state": "connected", "detail": ""})
            if m == "POST" and p == "/session/send":
                return self._send({"key": body.get("key"), "sent": True, "output": SEND_OUTPUT,
                                   "truncated": False, "elapsed_ms": 1523, "state": "idle"})
            if m == "POST" and p == "/session/raw":
                return self._send({"key": body.get("key"), "sent": True})
            if m == "POST" and p == "/session/interrupt":
                return self._send({"key": body.get("key"), "sent": True})
            if m == "POST" and p == "/session/close":
                return self._send({"key": body.get("key"), "closed": True})
            if m == "GET" and p == "/session/read":
                return self._send({"key": q.get("key"), "text": READ_TEXT, "cursor": 42, "eof": False})
            if m == "GET" and p == "/knowledge/stats":
                return self._send({"documents": 27297, "chunks": 518512, "size_mb": 782.1})
            if m == "GET" and p == "/knowledge/search":
                return self._send({"hits": [{"path": "docs/vlan.md", "title": "VLAN 配置",
                                             "snippet": LONG_SNIPPET, "score": 0.83}]})
            return self._send({"error": "no such endpoint: %s %s" % (m, p)}, status=404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return state, httpd


# --------------------------------------------------------------------------- #
# ② 桥进程的读写封装
# --------------------------------------------------------------------------- #
class Bridge:
    """把桥当子进程跑，逐行发 JSON-RPC、收响应；stderr 单独收集供断言。"""

    def __init__(self, home):
        env = dict(os.environ)
        env["USERPROFILE"] = home                      # Windows 的 ~ 看这个
        env["HOME"] = home
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["NO_PROXY"] = "127.0.0.1,localhost"        # 回环绝不走代理
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
        self.proc = subprocess.Popen([sys.executable, "-u", BRIDGE], cwd=REPO, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.stdout_lines, self.stderr_lines = [], []
        self._out, self._err = queue.Queue(), queue.Queue()
        self._id = 0
        threading.Thread(target=self._pump,
                         args=(self.proc.stdout, self._out, self.stdout_lines), daemon=True).start()
        threading.Thread(target=self._pump,
                         args=(self.proc.stderr, self._err, self.stderr_lines), daemon=True).start()

    @staticmethod
    def _pump(pipe, q, keep):
        try:
            for line in iter(pipe.readline, b""):
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                keep.append(text)
                q.put(text)
        finally:
            q.put(None)

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read_line(self, timeout):
        end = timeout
        while True:
            try:
                line = self._out.get(timeout=end)
            except queue.Empty:
                return None
            if line is None:                            # 进程结束了
                return None
            if line.strip():
                return line
            end = 1.0                                   # 空行不算响应，继续等

    def call(self, method, params=None, timeout=20):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        line = self._read_line(timeout)
        if line is None:
            return {"__no_response__": True, "id": self._id}
        try:
            return json.loads(line)
        except ValueError:
            return {"__bad_json__": line, "id": self._id}

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def stderr_text(self):
        return "\n".join(self.stderr_lines)

    def finish(self, timeout=10):
        """关 stdin，等进程退出，返回退出码（超时返回 'timeout'）。"""
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return "timeout"
        finally:
            try:
                self.proc.stdout.close()
                self.proc.stderr.close()
            except Exception:
                pass
        return rc


def text_of(resp):
    try:
        return resp["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return ""


def is_error(resp):
    return bool((resp.get("result") or {}).get("isError"))


def call_tool(bridge, name, args):
    resp = bridge.call("tools/call", {"name": name, "arguments": args})
    return text_of(resp), is_error(resp), resp


def make_home(control=None):
    """造一个假 HOME，可选写入 ~/.aiterminal/control.json。"""
    h = tempfile.mkdtemp(prefix="ait_mcp_home_")
    if control is not None:
        os.makedirs(os.path.join(h, ".aiterminal"), exist_ok=True)
        with open(os.path.join(h, ".aiterminal", "control.json"), "w", encoding="utf-8") as fh:
            json.dump(control, fh)
    return h


def reqs_of(state, path, method=None):
    return [r for r in state["requests"]
            if r["path"] == path and (method is None or r["method"] == method)]


def is_json_line(line):
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# ③ 用例
# --------------------------------------------------------------------------- #
def main():
    print("=== ① 起假后端 + 假 HOME（control.json 指向它） ===")
    state, httpd = start_fake_backend()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]
    home = make_home({"url": url, "token": TOKEN, "pid": 4321, "version": "0.7.0"})
    check("假后端在 127.0.0.1:%d 起来了" % httpd.server_address[1], True, url)

    bridge = Bridge(home)
    print("=== ② MCP 握手与工具清单 ===")
    r = bridge.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}})
    res = r.get("result") or {}
    check("initialize 回同 id、回显客户端 protocolVersion",
          r.get("id") == 1 and res.get("protocolVersion") == "2024-11-05",
          json.dumps(r, ensure_ascii=False)[:120])
    check("initialize capabilities.tools 存在", res.get("capabilities", {}).get("tools") == {},
          str(res.get("capabilities")))
    check("serverInfo = aiterminal/0.1.0",
          res.get("serverInfo") == {"name": "aiterminal", "version": "0.1.0"},
          str(res.get("serverInfo")))

    bridge.notify("notifications/initialized")
    r = bridge.call("ping")
    check("notifications/initialized 不回包（下一个响应就是 ping 的，id=2）",
          r.get("id") == 2 and r.get("result") == {}, json.dumps(r, ensure_ascii=False)[:120])

    r = bridge.call("tools/list", {})
    tools = (r.get("result") or {}).get("tools") or []
    names = [t.get("name") for t in tools]
    check("tools/list 返回 10 个工具", len(tools) == 10, str(len(tools)))
    check("工具名与约定清单一致（含顺序）", names == EXPECTED_TOOLS, str(names))
    check("每个工具都有非空 description 与 object 型 inputSchema",
          all(isinstance(t.get("description"), str) and t["description"].strip()
              and (t.get("inputSchema") or {}).get("type") == "object" for t in tools))
    check("ait_open 的 inputSchema 把 protocol 列为必填且给了 enum",
          next((t["inputSchema"] for t in tools if t["name"] == "ait_open"), {})
          .get("required") == ["protocol"]
          and next((t["inputSchema"] for t in tools if t["name"] == "ait_open"), {})
          .get("properties", {}).get("protocol", {}).get("enum")
          == ["ssh", "telnet", "serial", "ensp"])
    send_desc = next((t["description"] for t in tools if t["name"] == "ait_send"), "")
    check("ait_send description 写明“回显安静（默认 400ms 无新输出）或超时后一次性返回”",
          "400ms" in send_desc and "超时" in send_desc and "一次性返回" in send_desc,
          send_desc[:80])

    print("=== ③ 逐个工具真调一次 ===")
    t, e, _ = call_tool(bridge, "ait_status", {})
    check("ait_status 是中文摘要：app/版本/pid/会话数 + 每个会话一行",
          (not e) and all(k in t for k in ("NetOps Terminal", "0.7.0", "1234", "h3c-1",
                                           "核心交换机", "ssh", "10.0.0.1:22")),
          t.replace("\n", " | ")[:140])

    t, e, _ = call_tool(bridge, "ait_sessions", {})
    check("ait_sessions 列出会话（key/名称/协议/host:port/状态）",
          (not e) and all(k in t for k in ("h3c-1", "核心交换机", "ssh", "10.0.0.1:22")),
          t[:120])

    t, e, _ = call_tool(bridge, "ait_open", {"protocol": "ssh", "host": "10.0.0.1", "port": 22,
                                             "name": "核心交换机", "key": "h3c-1"})
    check("ait_open 返回中文摘要 + key + state（connected 不算错）",
          (not e) and "h3c-1" in t and "connected" in t, t[:140])

    t, e, _ = call_tool(bridge, "ait_send", {"key": "h3c-1", "command": "display version",
                                             "timeout_ms": 30000, "quiet_ms": 400})
    check("ait_send 的 text 就是回显原文（无“命令已发送”之类包装）",
          (not e) and t == SEND_OUTPUT, repr(t[:80]))

    t, e, _ = call_tool(bridge, "ait_read", {"key": "h3c-1", "cursor": 5})
    check("ait_read 返回新增输出原文 + 末尾 [cursor=42]",
          (not e) and t.startswith("新增输出") and t.rstrip().endswith("[cursor=42]"),
          repr(t[:80]))

    t, e, _ = call_tool(bridge, "ait_raw", {"key": "h3c-1", "data": "?"})
    check("ait_raw 简述已发送字节数（'?' = 1 字节）",
          (not e) and "已发送" in t and "1" in t, t[:80])

    t, e, _ = call_tool(bridge, "ait_interrupt", {"key": "h3c-1"})
    check("ait_interrupt 说明已发 Ctrl-C", (not e) and "Ctrl-C" in t, t[:80])

    t, e, _ = call_tool(bridge, "ait_close", {"key": "h3c-1"})
    check("ait_close 说明关闭了哪个会话", (not e) and "h3c-1" in t and "关闭" in t, t[:80])

    t, e, _ = call_tool(bridge, "ait_knowledge_search", {"query": "display vlan", "top_k": 3})
    check("ait_knowledge_search 列出 title/score/snippet",
          (not e) and "VLAN 配置" in t and "score=0.83" in t, t.splitlines()[0][:100])
    _snip = [l for l in t.splitlines() if l.startswith("   ")]
    check("检索片段截断到 300 字以内", bool(_snip) and len(_snip[0]) - 3 <= 301,
          "len=%d" % (len(_snip[0]) - 3 if _snip else -1))

    t, e, _ = call_tool(bridge, "ait_knowledge_stats", {})
    check("ait_knowledge_stats 给篇数/片段数/占用 MB",
          (not e) and all(k in t for k in ("27297", "518512", "782.1")), t[:100])

    print("=== ④ 参数与请求体细节 ===")
    op = reqs_of(state, "/session/open", "POST")
    first = op[0]["body"] if op else {}
    check("ait_open 只送非空参数（没传 username/password/serial_port/baudrate 就一个都不发）",
          not ({"username", "password", "serial_port", "baudrate"} & set(first)),
          json.dumps(first, ensure_ascii=False))
    check("ait_open 送出的 protocol/host/port/name/key 正确（port 是整数不是字符串）",
          first.get("protocol") == "ssh" and first.get("host") == "10.0.0.1"
          and first.get("port") == 22 and first.get("name") == "核心交换机"
          and first.get("key") == "h3c-1", json.dumps(first, ensure_ascii=False))
    for k, v in first.items():
        check("ait_open 没有把空串/None 送出去（%s）" % k, v not in ("", None), repr(v))

    call_tool(bridge, "ait_open", {"protocol": "telnet", "host": "10.0.0.2"})
    call_tool(bridge, "ait_open", {"protocol": "telnet", "host": "10.0.0.3"})
    auto_keys = [r["body"].get("key") for r in reqs_of(state, "/session/open", "POST")]
    check("ait_open 未给 key/name 时自动生成 key（dev-1）", "dev-1" in auto_keys, str(auto_keys))
    check("自动 key 不与已打开会话撞名：已有 dev-1 时下一台是 dev-2",
          auto_keys.count("dev-1") == 1 and "dev-2" in auto_keys, str(auto_keys))

    send_body = (reqs_of(state, "/session/send", "POST") or [{}])[0].get("body", {})
    check("ait_send 把 key/command/timeout_ms/quiet_ms 都送出",
          send_body.get("key") == "h3c-1" and send_body.get("command") == "display version"
          and send_body.get("timeout_ms") == 30000 and send_body.get("quiet_ms") == 400,
          json.dumps(send_body, ensure_ascii=False))
    read_q = (reqs_of(state, "/session/read", "GET") or [{}])[0].get("query", {})
    check("ait_read 把 key/cursor 放进 query 参数", read_q.get("key") == "h3c-1"
          and read_q.get("cursor") == "5", json.dumps(read_q, ensure_ascii=False))
    raw_body = (reqs_of(state, "/session/raw", "POST") or [{}])[0].get("body", {})
    check("ait_raw 原样送 data（不追加换行）",
          raw_body.get("data") == "?" and raw_body.get("key") == "h3c-1", str(raw_body))
    kn_q = (reqs_of(state, "/knowledge/search", "GET") or [{}])[0].get("query", {})
    check("ait_knowledge_search 送 q/top_k 两个 query 参数",
          kn_q.get("q") == "display vlan" and kn_q.get("top_k") == "3",
          json.dumps(kn_q, ensure_ascii=False))

    bad_auth = [r["path"] for r in state["requests"]
                if r["headers"].get("authorization") != "Bearer " + TOKEN]
    check("每个后端请求都带 Authorization: Bearer test-token-123",
          bool(state["requests"]) and not bad_auth, "缺: %s" % sorted(set(bad_auth))[:3])
    bad_ct = [r["path"] for r in state["requests"] if r["method"] == "POST"
              and "application/json" not in (r["headers"].get("content-type") or "")]
    check("POST 请求都带 Content-Type: application/json", not bad_ct, str(sorted(set(bad_ct))[:3]))

    print("=== ⑤ 错误路径 ===")
    t, e, _ = call_tool(bridge, "ait_send", {"key": "h3c-1"})
    check("缺必填参数 → isError:true 并说明缺什么", e and "command" in t, t[:100])
    t, e, _ = call_tool(bridge, "ait_open", {"protocol": "rdp"})
    check("protocol 不在 enum 内 → isError:true", e and "protocol" in t, t[:100])
    t, e, _ = call_tool(bridge, "ait_nonexistent", {})
    check("未知工具名 → isError:true", e and "未知工具" in t, t[:100])
    r = bridge.call("resources/list", {})
    check("未知方法 → -32601 Method not found",
          (r.get("error") or {}).get("code") == -32601, json.dumps(r, ensure_ascii=False)[:120])
    r = bridge.call("tools/call", {"arguments": {}})
    check("tools/call 缺 name → -32602", (r.get("error") or {}).get("code") == -32602,
          json.dumps(r, ensure_ascii=False)[:120])

    print("=== ⑥ 退出与 stderr 卫生 ===")
    joined = "\n".join(bridge.stdout_lines)
    check("stdout 上是原样 UTF-8 中文（不是 \\uXXXX 转义）",
          "核心交换机" in joined and "\\u" not in joined, "含 核心交换机=%s" % ("核心交换机" in joined))
    rc = bridge.finish(10)
    check("stdin 关掉后 10 秒内退出且退出码 0", rc == 0, "rc=%s" % rc)
    check("stderr 里没有 traceback", "Traceback" not in bridge.stderr_text(),
          bridge.stderr_text()[-200:])
    bad_lines = [l for l in bridge.stdout_lines if l.strip() and not is_json_line(l)]
    check("stdout 每一行都是合法 JSON-RPC 报文（无 stray print）",
          not bad_lines, str(bad_lines[:2]))

    print("=== ⑦ control.json 缺失（另一个空 HOME 起第二个桥） ===")
    home2 = make_home()
    bridge2 = Bridge(home2)
    r = bridge2.call("initialize", {})
    check("initialize 缺 protocolVersion 时兜底 2024-11-05",
          (r.get("result") or {}).get("protocolVersion") == "2024-11-05",
          json.dumps(r, ensure_ascii=False)[:120])
    t, e, _ = call_tool(bridge2, "ait_status", {})
    check("没有 control.json → ait_status 是 isError:true", e, t[:120])
    check("错误文本说明没找到控制文件", "没找到控制文件" in t, t[:140])
    rc2 = bridge2.finish(10)
    check("第二个桥进程同样干净退出（rc=0）", rc2 == 0, "rc=%s" % rc2)
    check("第二个桥 stderr 无 traceback", "Traceback" not in bridge2.stderr_text(),
          bridge2.stderr_text()[-200:])

    print("=== ⑧ 后端不在 / HTTP 报错（契约要求原文进 text） ===")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    home3 = make_home({"url": "http://127.0.0.1:%d" % dead_port, "token": TOKEN, "pid": 1,
                       "version": "0.7.0"})
    bridge3 = Bridge(home3)
    t, e, _ = call_tool(bridge3, "ait_status", {})
    check("后端没在监听（连接被拒）→ isError:true 且提示连不上", e and "连不上" in t, t[:140])
    rc3 = bridge3.finish(10)
    check("第三个桥进程干净退出（rc=0）", rc3 == 0, "rc=%s" % rc3)

    home4 = make_home({"url": url + "/sub", "token": TOKEN, "pid": 4321, "version": "0.7.0"})
    bridge4 = Bridge(home4)                    # 多一层路径 ⇒ 后端回 404
    t, e, _ = call_tool(bridge4, "ait_status", {})
    check("HTTP 报错时把状态码与后端 error 原文写进 text",
          e and "HTTP 404" in t and "no such endpoint" in t, t[:170])
    rc4 = bridge4.finish(10)
    check("第四个桥进程干净退出（rc=0）", rc4 == 0, "rc=%s" % rc4)

    httpd.shutdown()
    httpd.server_close()
    for h in (home, home2, home3, home4):
        shutil.rmtree(h, ignore_errors=True)

    print("\n结果: %d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项: " + "；".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

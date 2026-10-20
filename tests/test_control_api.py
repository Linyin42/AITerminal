# -*- coding: utf-8 -*-
"""控制通道端到端：外部智能体（MCP 桥）→ 回环 HTTP → app 会话 → 假设备。

跑法：python -u tests\\test_control_api.py

链路（这是 DSH「连终端一起接管」的完整链路，只差 DSH 自己）：
    MCP stdio 桥（data_tools/mcp/aiterminal_mcp.py，随包发布）
        → http://127.0.0.1:<port> + Bearer 令牌（core/control_api.py，本文件服务端）
            → core/session.py 的 SessionManager
                → core/telnet_connector.py
                    → tests/fake_vendor_telnet.py 起的假设备

隔离：USERPROFILE / control_api.CONTROL_FILE / AUDIT_FILE 全指到临时目录，
      不碰用户真实的 ~/.aiterminal；测试结束把假设备与桥都杀掉。
"""
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HOME_TMP = tempfile.mkdtemp(prefix="ait-control-home-")
os.environ["USERPROFILE"] = HOME_TMP        # 桥子进程按它算 ~/.aiterminal/control.json
os.environ["HOME"] = HOME_TMP

FAIL = []
TOTAL = [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAIL.append(name)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------- 起假设备
FAKE_PORT = free_port()
fake = subprocess.Popen([sys.executable, "-u", os.path.join(BASE, "tests", "fake_vendor_telnet.py"),
                         "--vendor", "h3c", "--port", str(FAKE_PORT), "--quiet"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace")
ready = ""
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    line = fake.stdout.readline()
    if not line:
        break
    ready += line
    if "127.0.0.1" in line:
        break
print(f"假设备: {ready.strip() or '（没等到就绪输出）'}", flush=True)

# ---------------------------------------------------------------- 起控制服务
import core.control_api as control_api                                 # noqa: E402

control_api.CONTROL_FILE = os.path.join(HOME_TMP, ".aiterminal", "control.json")
control_api.AUDIT_FILE = os.path.join(HOME_TMP, ".aiterminal", "control-audit.log")

from PyQt6.QtWidgets import QApplication                              # noqa: E402

app = QApplication.instance() or QApplication([])

from core.session import SessionManager                               # noqa: E402
from config.settings import Settings                                  # noqa: E402

manager = SessionManager()
manager.settings = Settings(config_file=os.path.join(HOME_TMP, "settings.json"))

server = control_api.ControlServer(manager, app_version="0.7.0-test")
url = server.start()
TOKEN = server.token
print(f"控制服务: {url}  令牌 {TOKEN[:8]}…（发现文件 {control_api.CONTROL_FILE}）", flush=True)


def api(method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕开本机代理
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


print("\n=== ① 鉴权与发现 ===")
check("没令牌 → 401", api("GET", "/ping", token=None)[0] == 401)
check("错令牌 → 401", api("GET", "/ping", token="deadbeef")[0] == 401)
code, body = api("GET", "/ping")
check("对令牌 → ok", code == 200 and body.get("ok") is True, str(body))
with open(control_api.CONTROL_FILE, encoding="utf-8") as fh:
    disc = json.load(fh)
check("发现文件写了 url/token/pid", disc.get("url") == url and disc.get("token") == TOKEN
      and disc.get("pid") == os.getpid(), json.dumps(disc, ensure_ascii=False)[:120])

print("\n=== ② 开会话（真的连上假设备）===")
code, body = api("POST", "/session/open", {
    "protocol": "telnet", "host": "127.0.0.1", "port": FAKE_PORT,
    "name": "假设备", "key": "fake-1", "timeout_ms": 20000})
check("open 返回 connected", code == 200 and body.get("state") == "connected", str(body))
check("key 用传入的 fake-1", body.get("key") == "fake-1", str(body.get("key")))
SID = body.get("sid")
code, body = api("GET", "/session/list")
sessions = body.get("sessions", [])
check("列表里有这个会话且已连接",
      any(s["key"] == "fake-1" and s["connected"] for s in sessions), str(sessions))

print("\n=== ③ 发命令 → 拿回显（游标语义）===")
code, body = api("POST", "/session/send", {
    "key": "fake-1", "command": "display version", "timeout_ms": 25000, "quiet_ms": 400})
first = body.get("output", "")
check("send 成功且带回显", code == 200 and body.get("sent") and len(first) > 0,
      f"state={body.get('state')} 回显 {len(first)} 字 / {body.get('elapsed_ms')}ms")
check("回显是假设备的应答（Comware 版本）", "Comware" in first and "Version 7.1" in first,
      repr(first[:120]))
check("状态是 idle（等到安静）", body.get("state") == "idle", str(body.get("state")))

code, body = api("POST", "/session/send", {
    "key": "fake-1", "command": "display interface brief", "timeout_ms": 25000})
second = body.get("output", "")
check("第二条命令只拿增量（不重复上一次应答）",
      "Comware" not in second and "<H3C>" in second, repr(second[:160]))
check("不认识的命令如实回显设备的报错",
      "Unrecognized" in api("POST", "/session/send", {
          "key": "fake-1", "command": "显示 版本", "timeout_ms": 15000})[1].get("output", ""),
      "假设备对未知命令的应答")

code, body = api("GET", f"/session/read?key=fake-1&cursor=0&wait_ms=800")
check("按游标从头读能拿到历史", code == 200 and len(body.get("text", "")) > 0
      and body.get("cursor", 0) > 0, f"cursor={body.get('cursor')} eof={body.get('eof')}")

print("\n=== ④ 交互输入与关闭 ===")
code, body = api("POST", "/session/raw", {"key": "fake-1", "data": "\r"})
check("raw 逐字符发送", code == 200 and body.get("sent") is True, str(body))
time.sleep(1.0)
code, body = api("GET", f"/session/read?key=fake-1&cursor=0&wait_ms=300")
check("raw 敲回车后设备重新出提示符", "<H3C>" in body.get("text", ""),
      repr(body.get("text", "")[-160:]))
code, body = api("POST", "/session/interrupt", {"key": "fake-1"})
check("中断（Ctrl-C）能发", code == 200 and body.get("sent") is True, str(body))
code, body = api("POST", "/session/close", {"key": "fake-1"})
check("close 生效", code == 200 and body.get("closed") is True, str(body))
code, body = api("GET", "/session/list")
check("关闭后列表里没有了", all(s["key"] != "fake-1" for s in body.get("sessions", [])),
      str(body.get("sessions")))

print("\n=== ⑤ 审计与错误分支 ===")
with open(control_api.AUDIT_FILE, encoding="utf-8") as fh:
    audit_text = fh.read()
check("审计日志记到了命令原文",
      "display version" in audit_text and "display interface brief" in audit_text,
      f"{len(audit_text)} 字")
check("审计日志记了 open/close", "open key=fake-1" in audit_text and "close key=fake-1" in audit_text)
code, body = api("POST", "/session/send", {"key": "不存在", "command": "x"})
check("给不存在的会话发命令 → 404", code == 404 and "没有这个会话" in body.get("error", ""), str(body))
code, body = api("POST", "/session/open", {"protocol": "ssh", "host": "127.0.0.1",
                                           "port": 1, "name": "连不上的", "key": "bad-1",
                                           "timeout_ms": 3000})
check("连不上时返回 state=error 且有原因",
      code == 200 and body.get("state") == "error" and body.get("detail"), str(body))
code, body = api("GET", "/no-such-endpoint")
check("未知接口 → 404", code == 404, str(body))
code, body = api("GET", "/status")
check("status 汇总会话数", code == 200 and isinstance(body.get("sessions"), list), str(body))

print("\n=== ⑥ 走 MCP 桥（DSH 那一侧的真实入口）===")
BRIDGE = os.path.join(BASE, "data_tools", "mcp", "aiterminal_mcp.py")
env = dict(os.environ)
env["USERPROFILE"] = HOME_TMP
env["HOME"] = HOME_TMP
bridge = subprocess.Popen([sys.executable, "-u", BRIDGE], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=BASE)
lines = queue.Queue()


def pump():
    for line in bridge.stdout:
        lines.put(line)


threading.Thread(target=pump, daemon=True).start()


def mcp(method, params=None, rid=1, timeout=60):
    bridge.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                   "params": params or {}}, ensure_ascii=False) + "\n")
    bridge.stdin.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            continue
        msg = json.loads(line)
        if msg.get("id") == rid:
            return msg
    return {}


hello = mcp("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "0"}})
check("MCP 握手成功", "result" in hello and hello["result"].get("serverInfo", {}).get("name") == "aiterminal",
      json.dumps(hello, ensure_ascii=False)[:140])
tools = mcp("tools/list", {}, rid=2).get("result", {}).get("tools", [])
check("工具清单 10 个", len(tools) == 10, str([t["name"] for t in tools])[:160])
check("有 ait_send / ait_open / ait_knowledge_search",
      {"ait_send", "ait_open", "ait_knowledge_search"} <= {t["name"] for t in tools})

opened = mcp("tools/call", {"name": "ait_open", "arguments": {
    "protocol": "telnet", "host": "127.0.0.1", "port": FAKE_PORT, "key": "via-mcp"}}, rid=3)
text = "".join(c.get("text", "") for c in opened.get("result", {}).get("content", []))
check("经 MCP 打开会话成功", opened.get("result", {}).get("isError") is not True, text[:160])

sent = mcp("tools/call", {"name": "ait_send", "arguments": {
    "key": "via-mcp", "command": "display version", "timeout_ms": 25000}}, rid=4)
sent_text = "".join(c.get("text", "") for c in sent.get("result", {}).get("content", []))
check("经 MCP 发命令拿回真设备应答",
      sent.get("result", {}).get("isError") is not True
      and "Comware" in sent_text and len(sent_text) > 20, repr(sent_text[:160]))

closed = mcp("tools/call", {"name": "ait_close", "arguments": {"key": "via-mcp"}}, rid=5)
check("经 MCP 关闭会话", "已关闭" in json.dumps(closed, ensure_ascii=False), str(closed)[:140])

bridge.stdin.close()
try:
    bridge.wait(timeout=10)
    rc = bridge.returncode
except subprocess.TimeoutExpired:
    bridge.kill()
    rc = "timeout"
check("桥收到 EOF 后干净退出", rc == 0, str(rc))
err = bridge.stderr.read()
check("桥的 stderr 没有 traceback", "Traceback" not in err, err.strip().splitlines()[-1] if err.strip() else "")

# ---------------------------------------------------------------- 收尾
server.stop()
try:
    fake.kill()
except Exception:
    pass
exists = os.path.exists(control_api.CONTROL_FILE)
check("停止后删掉了发现文件", not exists, f"path={control_api.CONTROL_FILE} exists={exists}")

print("\n" + "-" * 60)
print(f"结果: {TOTAL[0] - len(FAIL)} 通过 / {len(FAIL)} 失败（共 {TOTAL[0]} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)

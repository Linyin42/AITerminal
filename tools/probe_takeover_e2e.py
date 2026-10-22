# -*- coding: utf-8 -*-
"""真机接管探针：拿**真实的 MainWindow** 起控制服务，让 DSH 用的那个 MCP 桥连过来。

和 tests/test_control_api.py 的分工：
    那个测的是"服务 + 桥 + 设备"这条链（假设备、直连 SessionManager）；
    这个测的是"**真界面**起服务、真 MCP 子进程连进来"——也就是 DSH 实际要走的那条路。

跑法：python -u tools\\probe_takeover_e2e.py
隔离：USERPROFILE/HOME 指到临时目录（桥子进程按它找 control.json），
      不动用户真实的 ~/.aiterminal/control.json。
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

HOME_TMP = tempfile.mkdtemp(prefix="ait-takeover-home-")
os.environ["USERPROFILE"] = HOME_TMP          # 桥子进程按它算 ~/.aiterminal/control.json
os.environ["HOME"] = HOME_TMP
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import control_api                                        # noqa: E402

control_api.CONTROL_FILE = os.path.join(HOME_TMP, ".aiterminal", "control.json")
control_api.AUDIT_FILE = os.path.join(HOME_TMP, ".aiterminal", "control-audit.log")

from PyQt6.QtWidgets import QApplication                            # noqa: E402

app = QApplication.instance() or QApplication([])
app.setApplicationVersion("0.7.0-probe")

STATS = []
T0 = time.monotonic()


def step(n, text, detail=""):
    dt = time.monotonic() - T0
    STATS.append(text)
    print(f"[{n}] {text}   {detail}   ({dt:.1f}s)", flush=True)


class Bridge:
    """把随包的 MCP 桥当外部客户端拉起来（DSH 就是这么起它的）"""

    def __init__(self, script):
        self.env = dict(os.environ)
        self.proc = subprocess.Popen(
            [sys.executable, "-u", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
            text=True, encoding="utf-8", bufsize=1)
        self.q = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc.stdout,), daemon=True).start()
        self.log = []
        threading.Thread(target=self._pump_err, daemon=True).start()
        self.n = 0

    def _pump(self, stream):
        for line in stream:
            self.q.put(line.strip())

    def _pump_err(self):
        for line in self.proc.stderr:
            self.log.append(line.rstrip())

    def call(self, method, params=None, timeout=90):
        self.n += 1
        req = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.q.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == self.n:
                return msg
        raise TimeoutError(f"{method} 超时（{timeout}s）")

    def stop(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def text_of(resp):
    """取 MCP tools/call 返回的第一段文本"""
    try:
        return resp["result"]["content"][0]["text"]
    except Exception:
        return json.dumps(resp, ensure_ascii=False)[:200]


def main():
    step(1, "构造真实 MainWindow（界面代码原样）", "…")
    from ui.main_window import MainWindow

    win = MainWindow()
    step(2, "MainWindow 构造完成", f"{type(win).__name__} title={win.windowTitle()!r}")

    win.set_control_enabled(True, remember=False)
    state = win.control_state()
    assert state["enabled"], "控制服务没起来"
    step(3, "外部接管开关打开（真的起了服务）", f"{state['url']} token={state['token'][:8]}…")

    # 发现文件：DSH 侧就是靠它找到地址和令牌的
    assert os.path.exists(control_api.CONTROL_FILE), "没写发现文件"
    disc = json.load(open(control_api.CONTROL_FILE, encoding="utf-8"))
    step(4, "发现文件已写", f"{control_api.CONTROL_FILE} → url={disc['url']} "
                            f"pid={disc['pid']} app={disc['app']} v={disc['version']}")

    bridge = Bridge(os.path.join(BASE, "data_tools", "mcp", "aiterminal_mcp.py"))
    try:
        hs = bridge.call("initialize", {"protocolVersion": "2024-11-05",
                                        "capabilities": {},
                                        "clientInfo": {"name": "probe", "version": "1"}})
        name = hs["result"]["serverInfo"]["name"]
        step(5, "MCP 握手成功", f"server={name}")

        tools = [t["name"] for t in bridge.call("tools/list")["result"]["tools"]]
        assert len(tools) == 10, tools
        step(6, "工具清单 10 个", ", ".join(tools[:4]) + " …")

        # 工具返回的是给人看的文本（不是 JSON），所以按关键字断言
        st = text_of(bridge.call("tools/call", {"name": "ait_status", "arguments": {}}))
        assert "NetOpsTerminal" in st and str(os.getpid()) in st, st[:200]
        step(7, "经 MCP 读到**这个界面**的状态", st.replace("\n", " / ")[:80])

        ss = text_of(bridge.call("tools/call", {"name": "ait_sessions", "arguments": {}}))
        assert ss.strip(), "会话列表返回空"
        step(8, "会话列表拿到（当前没有终端会话）", ss.replace("\n", " / ")[:60])

        ks = text_of(bridge.call("tools/call", {"name": "ait_knowledge_stats",
                                                "arguments": {}}))
        assert ks.strip(), "资料库统计返回空"
        step(9, "经 MCP 查本地资料库（临时 home ⇒ 0 篇，只验通路）",
             ks.replace("\n", " / ")[:80])

        # 令牌错了必须挡住
        import urllib.request
        bad = urllib.request.Request(state["url"] + "/status",
                                     headers={"Authorization": "Bearer wrong"})
        try:
            urllib.request.urlopen(bad, timeout=10)
            raise AssertionError("错令牌居然通过了")
        except urllib.error.HTTPError as e:
            assert e.code == 401, e.code
        step(10, "错令牌被挡（401）", "ok")

        # 审计只记"动了设备"的动作（open/send/interrupt/close），只读操作不写审计；
        # 那部分的断言在 tests/test_control_api.py 里（下发命令后查命令原文）。
    finally:
        bridge.stop()
        assert bridge.proc.returncode == 0, bridge.proc.returncode
        err = "\n".join(bridge.log)
        assert "Traceback" not in err, err
        step(11, "桥干净退出、stderr 无 traceback", f"rc={bridge.proc.returncode}")

    win.set_control_enabled(False, remember=False)
    assert not os.path.exists(control_api.CONTROL_FILE), "关掉后发现文件没删"
    step(12, "关开关后服务停了、发现文件删了", "ok")
    win.close()

    print("\n" + "-" * 62)
    print(f"真机接管链路全通：{len(STATS)} 步，耗时 {time.monotonic() - T0:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n探针失败：{type(e).__name__}: {e}")
        sys.exit(1)

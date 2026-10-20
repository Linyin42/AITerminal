#!/usr/bin/env python3
"""引擎端到端冒烟测试：拉起 engine/main.py，走完整 JSON-RPC 链路开一个会话。

它验证的是 C++ 界面依赖的那条真实链路：
    engine.hello → hosts.list → session.open → session.write → session.output

用法：
    # 终端 A：起模拟设备
    python tools/fake_device.py --port 2323 --vendor h3c --no-login

    # 终端 B：跑本测试
    python tests/engine_smoke.py --host 127.0.0.1 --port 2323 --protocol Telnet

参数：
    --seconds N   观察窗时长（默认 30）
    --command C   连上后下发的一条命令（默认 "display version"）
    --config PATH 用另一份配置文件（隔离测试，不碰真实设备库）

eNSP Pro 设备（没有 TCP 端点，走 HTTP+STOMP）：
    python tests/engine_smoke.py --protocol eNSP --host 192.168.31.157 --port 8443 \
        --sandbox-id 2 --node-id 18 --login-password Huawei@123
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine", "main.py")


class EngineDriver:
    """极简的 JSON-RPC over stdio 客户端。"""

    def __init__(self, config_file=None, verbose=True):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if config_file:
            env["AIT_CONFIG_FILE"] = config_file

        self.verbose = verbose
        self.events = []                      # (时刻, 方法, 参数)
        self.output_chunks = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            [sys.executable, "-u", ENGINE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=ROOT, env=env,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._t0 = time.time()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # ---------------------------------------------------------------- 内部
    def _stamp(self):
        return time.time() - self._t0

    def _read_stdout(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                print("  !! stdout 出现非 JSON: %r" % line[:200])
                continue
            self._handle(msg)

    def _read_stderr(self):
        for line in self._proc.stderr:
            line = line.rstrip()
            if line and self.verbose:
                print("  [engine] %s" % line)

    def _handle(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        if method:
            self.events.append((self._stamp(), method, params))
            if method == "session.output":
                self.output_chunks.append(params.get("text", ""))
                if self.verbose:
                    for piece in params.get("text", "").splitlines():
                        print("  [%6.2fs] OUT  %s" % (self._stamp(), piece))
            elif self.verbose:
                brief = {k: v for k, v in params.items()
                         if k in ("key", "state", "detail", "command", "ok", "sid")}
                print("  [%6.2fs] %-14s %s" % (self._stamp(), method, brief))
        else:
            self.events.append((self._stamp(), "reply#%s" % msg.get("id"), msg))
            if self.verbose and "error" in msg:
                print("  [%6.2fs] ERROR %s" % (self._stamp(), msg["error"]))

    # ---------------------------------------------------------------- 对外
    def call(self, method, params=None):
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            payload = json.dumps({"id": req_id, "method": method,
                                  "params": params or {}}, ensure_ascii=False)
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        return req_id

    def wait_for(self, method, timeout, predicate=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for _, name, params in list(self.events):
                if name == method and (predicate is None or predicate(params)):
                    return params
            time.sleep(0.02)
        return None

    def state_of(self, key):
        state = None
        for _, name, params in self.events:
            if name == "session.state" and params.get("key") == key:
                state = params
        return state

    def stop(self):
        try:
            self.call("engine.exit")
            self._proc.wait(timeout=3)
        except Exception:
            self._proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2323)
    ap.add_argument("--protocol", default="Telnet")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--command", default="display version")
    ap.add_argument("--config", default=None)
    ap.add_argument("--key", default="smoke#1")
    # eNSP Pro 专用：设备没有 TCP 端点，走 HTTP+STOMP，需要沙箱/设备 ID
    ap.add_argument("--sandbox-id", type=int, default=0, dest="sandbox_id")
    ap.add_argument("--node-id", type=int, default=0, dest="node_id")
    # 设备已设过登录密码时用它：等出现 Password 提示后自动输入
    ap.add_argument("--login-password", default="", dest="login_password")
    args = ap.parse_args()

    is_ensp = args.protocol.lower() in ("ensp", "ensp-pro", "ensppro")
    # 华为设备命令行认 CR；其余协议沿用 CRLF
    ending = "\r" if is_ensp else "\r\n"

    print("=" * 68)
    print("引擎端到端冒烟：%s %s:%s" % (args.protocol, args.host, args.port))
    print("=" * 68)

    driver = EngineDriver(config_file=args.config)
    key = args.key

    print("\n[1] engine.hello")
    driver.call("engine.hello")
    hello = driver.wait_for("reply#1", 15)
    print("  → %s" % json.dumps(hello.get("result", {}), ensure_ascii=False)[:220])

    print("\n[2] session.open  （观察连接耗时）")
    open_started = time.time()
    host_desc = {"name": "冒烟设备", "protocol": args.protocol,
                 "host": args.host, "port": args.port,
                 "username": args.username}
    if is_ensp:
        host_desc["sandbox_id"] = args.sandbox_id
        host_desc["node_id"] = args.node_id
    driver.call("session.open", {
        "key": key,
        "host": host_desc,
        "password": "",
    })

    connected = driver.wait_for("session.state", 25,
                               lambda p: p.get("key") == key
                               and p.get("state") in ("connected", "error"))
    elapsed = time.time() - open_started
    if not connected:
        print("  !! 25s 内既没连上也没报错")
    elif connected.get("state") == "error":
        print("  !! 连接失败：%s" % connected.get("detail"))
    else:
        print("  ✓ 连接成功，耗时 %.2fs" % elapsed)

    if connected and connected.get("state") == "connected":
        # 很多网络设备/Console 要一个回车才给提示符
        print("\n[3] 先发一个回车（设备横幅要求 Press ENTER）")
        driver.call("session.write", {"key": key, "data": ending})
        time.sleep(2.0)

        # 设备已设登录密码时，等服务端吐出 Password 提示再输入
        if args.login_password:
            deadline = time.time() + 20
            saw_prompt = False
            while time.time() < deadline:
                if "assword" in "".join(driver.output_chunks):
                    saw_prompt = True
                    break
                time.sleep(0.4)
            if saw_prompt:
                print("\n[3b] 出现 Password 提示，发送登录密码")
                driver.call("session.write",
                            {"key": key, "data": args.login_password + ending})
                time.sleep(6)
            else:
                print("\n[3b] 20s 内没等到 Password 提示，跳过登录")

        print("\n[4] session.write: %s" % args.command)
        driver.call("session.write", {"key": key, "data": args.command + ending})

        print("\n[5] 观察输出 %ss" % args.seconds)
        time.sleep(args.seconds)

        print("\n[6] session.close")
        driver.call("session.close", {"key": key})
        time.sleep(1.0)

    print("\n" + "=" * 68)
    print("汇总")
    print("=" * 68)
    final = driver.state_of(key)
    print("  最终状态      : %s" % (final.get("state") if final else "无"))
    print("  连接耗时      : %.2fs" % elapsed)
    print("  输出片段数    : %d" % len(driver.output_chunks))
    total = "".join(driver.output_chunks)
    print("  输出总字符数  : %d" % len(total))
    kinds = {}
    for _, name, _p in driver.events:
        kinds[name] = kinds.get(name, 0) + 1
    print("  事件统计      : %s" % kinds)
    if total.strip():
        print("\n  ---- 终端实际收到的前 500 字符 ----")
        print(total[:500])
    else:
        print("\n  !! 终端没有收到任何输出")

    driver.stop()
    ok = bool(total.strip()) and final and final.get("state") in ("connected", "closed")
    print("\n结果: %s" % ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

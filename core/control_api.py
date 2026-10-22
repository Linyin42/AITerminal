# -*- coding: utf-8 -*-
"""本地控制通道：让外部智能体（DSH / MCP）驱动**正在运行的** NetOpsTerminal。

为什么是自己实现的回环 HTTP，而不是复用 engine/：
    engine/main.py 的 stdio JSON-RPC 是"C++ 版界面的接入面"：它另起一个进程、有
    自己的 SessionManager，附着不到用户正在用的这个界面；而且它没有端口、没有鉴权、
    也没有"读一次输出"的请求-响应接口（2026-10-17 侦察结论）。要满足"连终端一起
    接管"，只能由 app 自己开一条回环通道。

形态（2026-10-17 定的契约，随包的 MCP 桥 data_tools/mcp/aiterminal_mcp.py 按它调用）：
    · 只监听 127.0.0.1，端口由系统分配（0），令牌是 32 位随机十六进制
    · 令牌与端口写在 ~/.aiterminal/control.json，外部进程读它来发现服务
    · 每个会话维护"输出游标"：用 connector.add_output_listener()（非 Qt、读线程内
      同步回调）把新输出追加进环形缓冲，读取按游标取增量。
      **绝不能调 get_output()/get_output_block()** —— 两者都是"读后清空"，
      会把 GUI 终端里的回显抢走（GUI 就是靠这个缓冲刷终端的）。
    · 每条下发命令追加一行到 ~/.aiterminal/control-audit.log（命令级审计）

安全：默认关闭，要用户在「AI 高级设置 → 外部接管」里显式打开；打开后也只回环 +
令牌，令牌可随时在界面上重置（重置即让旧令牌失效）。
"""
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

#: 发现文件：外部进程（MCP 桥）读它拿 url/token
CONTROL_FILE = os.path.join(os.path.expanduser("~"), ".aiterminal", "control.json")
#: 命令级审计日志（谁在什么时候往哪台设备发了什么）
AUDIT_FILE = os.path.join(os.path.expanduser("~"), ".aiterminal", "control-audit.log")
APP_NAME = "NetOps Terminal"

#: 每个会话保留的输出上限（字符）；超出丢最旧的，够模型读回上文
FEED_LIMIT = 512 * 1024
#: 一次返回给外部的输出上限，避免把模型上下文撑爆
READ_LIMIT_DEFAULT = 200_000


def audit(line: str):
    """记一行命令级审计（失败不影响主流程）"""
    try:
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except Exception:
        pass


class SessionFeed:
    """一个会话的输出环形缓冲 + 游标。

    游标是"已发出字符数"的单调计数：外部读 `cursor=N` 拿到 N 之后的新内容。
    缓冲满了从头丢，游标不回退（旧内容读不到了，但不会串位）。
    """

    def __init__(self, limit: int = FEED_LIMIT):
        self._lock = threading.Lock()
        self._text = ""
        self._dropped = 0          # 已丢弃的字符数（游标起点）
        self._limit = limit

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._dropped + len(self._text)

    def append(self, text: str):
        if not text:
            return
        with self._lock:
            self._text += text
            if len(self._text) > self._limit:
                cut = len(self._text) - self._limit
                self._text = self._text[cut:]
                self._dropped += cut

    def read(self, cursor: int = 0, limit: int = READ_LIMIT_DEFAULT):
        """取 [cursor, now) 的增量；返回 (text, 新游标, 是否被截断)"""
        with self._lock:
            start = max(0, cursor - self._dropped)
            if cursor < self._dropped:
                start = 0                      # 要的内容已被丢，尽力给现有的
            text = self._text[start:]
            end = self._dropped + len(self._text)
        truncated = len(text) > limit
        if truncated:
            text = text[-limit:]               # 留最新的：模型更关心命令尾部
        return text, end, truncated

    def wait_quiet(self, from_cursor: int = 0, quiet_ms: int = 400,
                   timeout_ms: int = 30_000, limit: int = READ_LIMIT_DEFAULT,
                   first_ms: int = 1500, fresh_from: int = None):
        """等到"有回显且安静了 quiet_ms"；返回 (text, state, truncated)。

        from_cursor 只决定**从哪开始返回**（给"上次读到的位置"，好让调用方连登录
        横幅一起看到）；"有没有回显"看的是进入等待之后游标有没有动。
        设备完全不吭声（比如 screen-length 这类静默命令）时，最多空等 first_ms
        就返回 timeout —— 不要拿 30 秒超时去填一个本来就没输出的命令。
        """
        started = time.monotonic()
        deadline = started + max(0.2, timeout_ms / 1000.0)
        quiet = max(0.05, quiet_ms / 1000.0)
        first = max(0.1, first_ms / 1000.0)
        # "之后动的才算回显"：设备可能在 send 的 0.3s 里就把应答送来了，
        # 所以这个基准点由调用方在**发送之前**取好传进来（fresh_from）。
        base = self.cursor if fresh_from is None else fresh_from
        seen = base
        last_change = started
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            if self.cursor != seen:
                seen = self.cursor
                last_change = now
            if self.cursor > base and now - last_change >= quiet:
                text, _, trunc = self.read(from_cursor, limit)
                return text, "idle", trunc
            if self.cursor <= base and now - started >= first:
                text, _, trunc = self.read(from_cursor, limit)
                return text, "timeout", trunc
            if now >= deadline:
                text, _, trunc = self.read(from_cursor, limit)
                return text, "timeout", trunc

    def wait_new(self, cursor: int = 0, wait_ms: int = 1000,
                 limit: int = READ_LIMIT_DEFAULT):
        """最多等 wait_ms 等新内容；拿到就立刻返回（读接口用）"""
        deadline = time.monotonic() + max(0.0, wait_ms / 1000.0)
        while self.cursor <= cursor and time.monotonic() < deadline:
            time.sleep(0.05)
        text, end, trunc = self.read(cursor, limit)
        return text, end, trunc


class ControlServer:
    """回环 HTTP 控制服务；由 MainWindow 持有（用户在那个开关里启停）"""

    def __init__(self, session_manager, app_version: str = "",
                 knowledge=None, host: str = "127.0.0.1", port: int = 0,
                 token: str = None):
        self.manager = session_manager
        self.version = app_version
        self.knowledge = knowledge
        self.host = host
        self.port = port
        self.token = token or secrets.token_hex(16)
        self._httpd = None
        self._thread = None
        self._keys = {}            # key -> sid（外部用 key，内部用 sid）
        self._feeds = {}           # sid -> SessionFeed
        self._locks = {}           # sid -> 该会话的"读游标"（send 用）
        self._error_msg = {}       # sid -> 最近一次连接失败原因
        self._last_error = (None, 0.0)
        self._watch_stop = threading.Event()
        self._watcher = None
        self.started_at = None

    # ------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        """起服务并写发现文件；返回 url"""
        if self._httpd:
            return self.url
        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.daemon_threads = True
        server.control = self            # handler 靠它拿控制服务与令牌（换令牌即时生效）
        # 连接失败原因：只连一次；create_session 只返回 -1，原因走这个信号
        try:
            self.manager.session_error.connect(self._remember_error)
        except Exception:
            pass
        self._httpd = server
        self.port = server.server_address[1]
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="ait-control", daemon=True)
        self._thread.start()
        # 会话输出监听：已开的会话补挂上，之后新开的在 open 时挂（见 attach）
        for sid in self._live():
            self.attach(sid)
        self._watcher = threading.Thread(target=self._watch_sessions,
                                         name="ait-control-watch", daemon=True)
        self._watcher.start()
        self._write_discovery()
        return self.url

    def stop(self):
        """停服务并删发现文件（只删自己写的那份）"""
        self._watch_stop.set()
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        for _ in range(5):                          # Windows 上文件可能被短暂占用
            try:
                with open(CONTROL_FILE, encoding="utf-8") as fh:
                    if int(json.load(fh).get("pid", -1)) != os.getpid():
                        break                       # 不是自己写的，别删
                os.remove(CONTROL_FILE)
                break
            except FileNotFoundError:
                break
            except Exception:
                time.sleep(0.2)

    def reset_token(self) -> str:
        """换令牌（旧令牌立刻失效）；发现文件同步更新"""
        self.token = secrets.token_hex(16)
        if self._httpd:
            self._write_discovery()
        return self.token

    def _write_discovery(self):
        os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
        payload = {
            "url": self.url, "token": self.token, "pid": os.getpid(),
            "app": APP_NAME, "version": self.version, "started": self.started_at,
        }
        tmp = CONTROL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONTROL_FILE)

    # ------------------------------------------------------------ 会话与输出
    def _live(self) -> dict:
        """当前会话表 {sid: SessionInfo}（走公开接口，拿不到再退回内部表）"""
        try:
            return self.manager.get_all_sessions()
        except Exception:
            return dict(getattr(self.manager, "_sessions", {}) or {})

    def _remember_error(self, sid, msg):
        self._error_msg[sid] = str(msg)
        self._last_error = (str(msg), time.monotonic())

    def attach(self, sid: int):
        """给会话挂输出监听器（幂等）"""
        if sid in self._feeds:
            return
        connector = self.manager.get_connector(sid)
        if connector is None:
            return
        feed = SessionFeed()
        self._feeds[sid] = feed
        self._locks[sid] = 0
        try:
            connector.add_output_listener(
                lambda text, f=feed: f.append(text))
        except Exception:
            pass

    def _watch_sessions(self):
        """后台看会话增减：新开的挂监听，关掉的清掉缓冲（不碰输出缓冲）"""
        while not self._watch_stop.wait(1.0):
            try:
                live = set(self._live())
            except Exception:
                continue
            for sid in live - set(self._feeds):
                self.attach(sid)
            for sid in set(self._feeds) - live:
                self._feeds.pop(sid, None)
                self._locks.pop(sid, None)

    def key_of(self, sid: int) -> str:
        for key, value in self._keys.items():
            if value == sid:
                return key
        return f"sid-{sid}"

    def sid_of(self, key) -> int:
        if key in self._keys:
            return self._keys[key]
        try:
            return int(key)                        # 也允许直接给 sid
        except (TypeError, ValueError):
            return -1

    def auto_key(self, name: str = "", protocol: str = "") -> str:
        base = (name or protocol or "dev").strip().lower().replace(" ", "-") or "dev"
        if base not in self._keys:
            return base
        n = 2
        while f"{base}-{n}" in self._keys:
            n += 1
        return f"{base}-{n}"

    # ------------------------------------------------------------ 端点实现
    def sessions(self):
        out = []
        for sid, session in sorted(self._live().items()):
            connector = getattr(session, "connector", None)
            config = getattr(session, "config", {}) or {}
            connected = False
            try:
                connected = bool(connector and connector.is_connected())
            except Exception:
                pass
            out.append({
                "key": self.key_of(sid), "sid": sid,
                "name": getattr(session, "name", "") or config.get("name", ""),
                "protocol": getattr(session, "protocol", "") or config.get("protocol", ""),
                "host": config.get("host", ""), "port": config.get("port", 0),
                "connected": connected,
            })
        out.sort(key=lambda item: item["sid"])
        return out

    def open_session(self, body: dict):
        protocol = str(body.get("protocol") or "").strip()
        name = str(body.get("name") or "").strip()
        key = str(body.get("key") or "").strip()
        if not key:
            key = self.auto_key(name, protocol)
        if protocol.lower() in ("ssh",):
            protocol = "SSH"
        elif protocol.lower() in ("telnet",):
            protocol = "Telnet"
        elif protocol.lower() in ("serial", "console", "com"):
            protocol = "Serial"
        elif protocol.lower() in ("ensp", "en"):
            protocol = "eNSP"
        if protocol not in ("SSH", "Telnet", "Serial", "eNSP"):
            return {"key": key, "state": "error",
                    "detail": f"不支持的协议: {protocol}"}, 400

        kwargs = {}
        for src, dst in (("username", "username"), ("password", "password"),
                         ("serial_port", "serial_port"), ("baudrate", "baudrate"),
                         ("line_ending", "line_ending"), ("bytesize", "bytesize"),
                         ("parity", "parity"), ("stopbits", "stopbits"),
                         ("flow", "flow"), ("sandbox_id", "sandbox_id"),
                         ("node_id", "node_id"), ("cookie", "cookie")):
            if body.get(src) not in (None, ""):
                kwargs[dst] = body[src]
        if body.get("port") not in (None, ""):
            kwargs["port"] = int(body["port"])
        elif protocol == "SSH":
            kwargs["port"] = 22
        elif protocol == "Telnet":
            kwargs["port"] = 23

        timeout = body.get("timeout_ms") or 20_000
        try:
            kwargs["timeout"] = max(1, int(timeout) // 1000)
        except (TypeError, ValueError):
            kwargs["timeout"] = 20

        sid = self.manager.create_session(
            name=name or key, protocol=protocol,
            host=str(body.get("host") or ""), **kwargs)
        if sid is None or sid < 0:
            msg, stamp = self._last_error
            fresh = msg and (time.monotonic() - stamp) < 5
            return {"key": key, "state": "error",
                    "detail": msg if fresh else "连接失败"}, 200
        self._keys[key] = sid
        self.attach(sid)
        audit(f"open key={key} sid={sid} {protocol} {body.get('host', '')}"
              f":{body.get('port', '')}")
        return {"key": key, "sid": sid, "state": "connected", "detail": ""}, 200

    def send_command(self, body: dict):
        key = str(body.get("key") or "")
        sid = self.sid_of(key)
        if sid < 0 or sid not in self._feeds:
            return {"error": f"没有这个会话: {key}"}, 404
        command = body.get("command")
        if command is None:
            return {"error": "缺少 command"}, 400
        try:
            timeout_ms = int(body.get("timeout_ms") or 30_000)
            quiet_ms = int(body.get("quiet_ms") or 400)
            limit = int(body.get("max_chars") or READ_LIMIT_DEFAULT)
        except (TypeError, ValueError):
            return {"error": "timeout_ms/quiet_ms/max_chars 要是整数"}, 400

        feed = self._feeds[sid]
        started = time.monotonic()
        from_cursor = self._locks.get(sid, 0)     # 返回内容从这里起（含未读的历史）
        fresh_base = feed.cursor                  # 发送前的基准：之后动的才算应答
        ok = bool(self.manager.send_command(sid, command))
        audit(f"send key={key} sid={sid} cmd={command!r}")
        text, state, trunc = feed.wait_quiet(from_cursor, quiet_ms, timeout_ms, limit,
                                             fresh_from=fresh_base)
        self._locks[sid] = feed.cursor
        return {
            "key": key, "sent": ok, "output": text, "truncated": trunc,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "state": state,
        }, 200

    def send_raw(self, body: dict):
        key = str(body.get("key") or "")
        sid = self.sid_of(key)
        connector = self.manager.get_connector(sid) if sid >= 0 else None
        if connector is None:
            return {"error": f"没有这个会话: {key}"}, 404
        data = body.get("data")
        if data is None:
            return {"error": "缺少 data"}, 400
        connector.send_raw(str(data))
        if str(data) == "\x03":
            audit(f"interrupt key={key} sid={sid}")
        return {"key": key, "sent": True}, 200

    def read_output(self, query: dict):
        key = (query.get("key") or [""])[0]
        sid = self.sid_of(key)
        if sid < 0 or sid not in self._feeds:
            return {"error": f"没有这个会话: {key}"}, 404
        try:
            cursor = int((query.get("cursor") or ["0"])[0])
            wait_ms = int((query.get("wait_ms") or ["0"])[0])
            limit = int((query.get("max_chars") or [READ_LIMIT_DEFAULT])[0])
        except ValueError:
            return {"error": "cursor/wait_ms/max_chars 要是整数"}, 400
        feed = self._feeds[sid]
        if wait_ms > 0:
            text, end, trunc = feed.wait_new(cursor, wait_ms, limit)
        else:
            text, end, trunc = feed.read(cursor, limit)
        self._locks[sid] = end
        session = self.manager.get_session(sid)
        return {"key": key, "text": text, "cursor": end, "truncated": trunc,
                "eof": session is None}, 200

    def close_session(self, body: dict):
        key = str(body.get("key") or "")
        sid = self.sid_of(key)
        if sid < 0:
            return {"error": f"没有这个会话: {key}"}, 404
        self.manager.close_session(sid)
        self._feeds.pop(sid, None)
        self._locks.pop(sid, None)
        self._keys = {k: v for k, v in self._keys.items() if v != sid}
        audit(f"close key={key} sid={sid}")
        return {"key": key, "closed": True}, 200

    def knowledge_base(self):
        if self.knowledge is None:
            kb = getattr(self.manager, "_knowledge", None)
            if kb is None and hasattr(self.manager, "_ensure_knowledge"):
                try:
                    kb = self.manager._ensure_knowledge()   # 惰性建库（首次要几秒）
                except Exception:
                    kb = None
            self.knowledge = kb or getattr(self.manager, "knowledge_base", None)
        return self.knowledge

    def knowledge_stats(self):
        kb = self.knowledge_base()
        if kb is None:
            return {"error": "知识库未就绪"}, 503
        stats = kb.stats()
        # KB 的字段是 size（字节）；以前这里读 size_mb 取不到，一直返回 0
        size_mb = stats.get("size_mb")
        if not size_mb:
            size_mb = round((stats.get("size") or 0) / (1024 * 1024), 1)
        return {"documents": stats.get("documents", 0),
                "chunks": stats.get("chunks", 0),
                "size_mb": size_mb, "root": stats.get("root", "")}, 200

    def knowledge_search(self, query: dict):
        kb = self.knowledge_base()
        if kb is None:
            return {"error": "知识库未就绪"}, 503
        q = (query.get("q") or query.get("query") or [""])[0]
        if not q.strip():
            return {"error": "缺少 q 参数"}, 400
        try:
            top_k = int((query.get("top_k") or ["5"])[0])
        except ValueError:
            return {"error": "top_k 要是整数"}, 400
        hits = []
        for hit in kb.search(q, top_k=top_k) or []:
            hits.append({
                "path": hit.get("path", ""), "title": hit.get("title", ""),
                "snippet": (hit.get("snippet") or hit.get("text") or "")[:400],
                "score": hit.get("score", 0),
            })
        return {"hits": hits}, 200

    def status(self):
        return {"app": APP_NAME, "version": self.version, "pid": os.getpid(),
                "url": self.url, "sessions": self.sessions()}, 200


class _Handler(BaseHTTPRequestHandler):
    server_version = "NetOpsTerminalControl/1.0"

    def _reply(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = self.server.control.token
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer "):
            got = got[7:].strip()
        return bool(token) and secrets.compare_digest(got, str(token))

    def _dispatch(self, method: str):
        control = self.server.control
        if not self._authorized():
            self._reply({"error": "令牌不对（读 ~/.aiterminal/control.json 里的 token）"}, 401)
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        body = {}
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:
                self._reply({"error": f"请求体不是 JSON: {e}"}, 400)
                return
        try:
            if method == "GET":
                routes = {
                    "/ping": lambda: ({"ok": True, "app": APP_NAME,
                                       "version": control.version}, 200),
                    "/status": lambda: control.status(),
                    "/session/list": lambda: ({"sessions": control.sessions()}, 200),
                    "/session/read": lambda: control.read_output(query),
                    "/knowledge/stats": lambda: control.knowledge_stats(),
                    "/knowledge/search": lambda: control.knowledge_search(query),
                    "/audit": lambda: ({"file": AUDIT_FILE}, 200),
                }
            else:
                routes = {
                    "/session/open": lambda: control.open_session(body),
                    "/session/send": lambda: control.send_command(body),
                    "/session/raw": lambda: control.send_raw(body),
                    "/session/interrupt": lambda: control.send_raw(
                        {"key": body.get("key"), "data": "\x03"}),
                    "/session/close": lambda: control.close_session(body),
                }
            handler = routes.get(path)
            if handler is None:
                self._reply({"error": f"没有这个接口: {method} {path}"}, 404)
                return
            payload, code = handler()
            self._reply(payload, code)
        except Exception as e:                      # 服务端绝不因单次调用崩掉
            self._reply({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):              # 走 stderr，别污染 stdout
        print(f"[control] {self.address_string()} {fmt % args}", file=sys.stderr)

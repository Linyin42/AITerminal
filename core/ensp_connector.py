"""
eNSP Pro 连接器 —— 直接对接华为 eNSP Pro 仿真平台里的设备命令行。

为什么需要它
------------
eNSP Pro 的设备**没有任何 TCP 端点**：实测扫描服务器 788 个候选端口，
除 8443 外全部关闭；设备的 hostIp 是 127.0.0.1、manageIp 为空。
设备 Console 全部经 HTTP + STOMP 中转，浏览器里用 xterm.js 呈现。
所以 SSH/Telnet/Serial 三种连接器都够不着它，必须另做一条协议。

协议（逆向自前端 bundle，并已实测跑通 display version / display device）
------------------------------------------------------------------------
    SockJS    wss://{host}:{port}/web.socket/000/{random}/websocket
    STOMP     CONNECT 必须带 host 头，否则收不到 CONNECTED
    订阅      /user/topic/console/message/{sandboxId}/{nodeId}
              /user/topic/console/heartbeat/{sandboxId}/{nodeId}
    建连      SEND /app/console/connect/{sandboxId}/{nodeId}
    输入      SEND /app/console/message/{sandboxId}/{nodeId}    body=按键原文
    心跳      SEND /app/console/heartbeat/{sandboxId}/{nodeId}  每 30s
    断开      SEND /app/console/close/{sandboxId}/{nodeId}
    全局心跳  SEND /app/user/topic/ping  body="ping"            每 3s

    注意前缀不对称：**订阅端要加 /user/topic，发送端要加 /app**。
    这是最容易搞错的一点 —— 少一个前缀就表现为"连上了但收不到任何输出"。

两个坑
------
1. 首次输入必须是 "\\r"。前端有 isFirstEnterKey 逻辑：第一下只转发回车，
   之后才转发普通按键。这也正是控制台那句 "Please Press ENTER." 的由来。
2. 首次上电的设备会要求设置登录密码（8-16 位），设置完才进命令行。
   连接器把它交给上层：把提示原样交给界面，由用户输入。

依赖：websocket-client（读 STOMP over SockJS），requests（REST + 登录）。
"""

from __future__ import annotations

import json
import random
import ssl
import string
import sys
import threading
import time

import requests

try:  # 连接环境里可能没有，缺了就让 connect() 明确报错
    import websocket
except Exception:  # pragma: no cover
    websocket = None

from .connector import BaseConnector


def log(msg: str) -> None:
    """诊断日志走 stderr。

    引擎用 stdout 跑 JSON-RPC（JSON Lines），往 stdout 写任何东西都会
    被当成协议报文，所以日志一律走 stderr。
    """
    print(f"[ensp] {msg}", file=sys.stderr, flush=True)

NUL = "\x00"

#: 订阅前缀（前端常量 Kb）
SUB_PREFIX = "/user/topic"
#: 发送前缀（前端常量 tO）
SEND_PREFIX = "/app"


def _rand(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class EnspProConnector(BaseConnector):
    """eNSP Pro 设备 Console 连接器。

    用法（与其它连接器一致）::

        conn = EnspProConnector()
        conn.connect(host="192.168.31.157", port=8443,
                     sandbox_id=2, node_id=18,
                     username="admin", password="")
        conn.send_raw("display version\\r")
        print(conn.get_output())
    """

    #: 控制台按键不需要额外追加换行（由调用方自己带 \r）
    line_ending = ""

    def __init__(self):
        super().__init__()
        self._host = ""
        self._port = 8443
        self._sandbox = 0
        self._node = 0
        self._session = requests.Session()
        self._session.verify = False
        self._ws = None
        self._alive = False
        self._connected = False
        self._lock = threading.Lock()
        self._buffer = ""
        self._first_enter_sent = False
        self._sub_id = 0
        self._device_password = ""
        self._auto_login = False
        self._pump_thread = None
        self._ping_thread = None
        self._hb_thread = None
        self._last_error = ""
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass

    # ---------------------------------------------------------------- 登录
    @property
    def base_url(self) -> str:
        return f"https://{self._host}:{self._port}"

    def login(self, username: str = "", password: str = "") -> bool:
        """取得会话 Cookie。

        eNSP Pro 在 standalone 部署下通常是免登录/自动登录，
        这里按优先级尝试：
          1) 显式 POST /api/login（有用户名密码时）
          2) GET  /api/login?service={origin}（standalone 自动登录）
          3) 复用已有 Cookie（调用方自己塞进 self._session）
        """
        for path, kwargs in (
            (f"/api/login?service={self.base_url}", {}),
            ("/api/login", {"json": {"username": username, "password": password}}
             if username else None),
        ):
            if kwargs is None:
                continue
            try:
                self._session.request("GET" if kwargs == {} else "POST",
                                      self.base_url + path, timeout=15,
                                      **kwargs)
            except Exception as exc:
                self._last_error = f"登录请求失败: {exc}"
                continue
            if self._probe_logged_in():
                return True
        return self._probe_logged_in()

    def _probe_logged_in(self) -> bool:
        try:
            r = self._session.get(self.base_url + "/api/getUserInfo", timeout=15)
            data = r.json()
            return data.get("code") == 0 and data.get("data") is not None
        except Exception:
            return False

    # ---------------------------------------------------------------- 连接
    def connect(self, **kwargs) -> bool:
        if websocket is None:
            self._last_error = "缺少依赖 websocket-client，请先 pip install websocket-client"
            return False

        # 记住这次握手用的参数：掉线后 ensure_connected() 要用同一套参数重连
        self._connect_kwargs = dict(kwargs)

        self._host = kwargs.get("host") or kwargs.get("address") or ""
        self._port = int(kwargs.get("port") or 8443)
        self._sandbox = int(kwargs.get("sandbox_id") or kwargs.get("sandboxId") or 0)
        self._node = int(kwargs.get("node_id") or kwargs.get("nodeId") or 0)
        username = kwargs.get("username") or ""
        password = kwargs.get("password") or ""
        # 设备 console 的登录密码。默认复用调用方给的 password；
        # 单独用 device_password 可以区分「平台登录密码」与「设备登录密码」。
        self._device_password = kwargs.get("device_password") or password

        if not self._host or not self._sandbox or not self._node:
            self._last_error = "需要 host、sandbox_id、node_id 三个参数"
            return False

        # 允许上层直接注入已登录的 Cookie（例如从浏览器导出）
        cookie = kwargs.get("cookie")
        if cookie:
            self._session.headers["Cookie"] = cookie
        elif not self.login(username, password):
            self._last_error = "登录 eNSP Pro 失败（可用 cookie= 参数直接传入会话 Cookie）"
            return False

        cookies = "; ".join(f"{c.name}={c.value}" for c in self._session.cookies)
        url = f"wss://{self._host}:{self._port}/web.socket/000/{_rand()}/websocket"
        try:
            self._ws = websocket.create_connection(
                url,
                header=[f"Cookie: {cookies}", f"Origin: {self.base_url}"],
                sslopt={"cert_reqs": ssl.CERT_NONE},
                timeout=15,
            )
        except Exception as exc:
            self._last_error = f"WebSocket 连接失败: {exc}"
            return False

        self._ws.settimeout(1.0)
        if not self._wait_sockjs_open(12):
            self._last_error = "SockJS 未返回打开帧"
            return False

        # CONNECT 必须带 host 头，否则服务端不回 CONNECTED
        self._frame("CONNECT", {"accept-version": "1.2,1.1,1.0",
                                "host": self._host, "heart-beat": "0,0"})
        self._alive = True
        self._pump_thread = threading.Thread(target=self._pump, daemon=True,
                                             name="ensp-pump")
        self._pump_thread.start()
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True,
                                            name="ensp-ping")
        self._ping_thread.start()
        time.sleep(1.2)

        # 订阅输出 + 建连
        self._subscribe(f"{SUB_PREFIX}/console/message/{self._sandbox}/{self._node}")
        self._subscribe(f"{SUB_PREFIX}/console/heartbeat/{self._sandbox}/{self._node}")
        time.sleep(0.4)
        self._send_sockjs(f"{SEND_PREFIX}/console/connect/{self._sandbox}/{self._node}")

        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True,
                                           name="ensp-hb")
        self._hb_thread.start()
        self._connected = True
        time.sleep(1.5)

        # 首次回车：前端逻辑要求第一下必须是 \r 才会开始转发按键
        self.send_raw("\r")
        self._first_enter_sent = True

        # 设备设过登录密码时会停在 "Password:"。
        # 这里直接用调用方给的密码应答，而不是把密码丢给界面手输 ——
        # 界面里敲密码要过一次安全闸门，会被当成配置命令弹确认框，
        # 既打扰用户、语义也不对（密码不是命令）。
        #
        # 必须判「缓冲区**末尾**」而不是"包含"：eNSP 首次上电的横幅里就出现过
        # Password 字样，用包含判断会在设备还没给出提示时就把密码发出去，
        # 设备侧的表现是 "Error: The password times out."（超时，不是错误）。
        if self._device_password:
            for attempt in range(3):
                if not self._wait_for_prompt("password:", 12.0):
                    break
                time.sleep(0.4)          # 等提示符渲染完再发，别抢
                self.send_raw(self._device_password + "\r")
                self._auto_login = True
                # 密码对了之后设备**不一定立刻给提示符**：
                # 华为 VRP 会先清屏并显示 "User interface con0 is available"
                # + "Please Press ENTER."，此时缓冲区末尾没有 > ，
                # 必须补一个回车才会出现 <HUAWEI> 之类的提示符。
                # 少了这一步，**正确密码也会被误判成"未接受密码"**并重试三次，
                # 表现为 connect 返回 True 但后续命令零回显。
                if self._wait_for_prompt("press enter", 8.0):
                    self.send_raw("\r")
                # 成功的话会出现提示符；失败设备会重新问一次密码
                if self._wait_for_prompt(">", 8.0):
                    break
                log(f"设备未接受密码，重试 {attempt + 1}/3")

        # 关闭分页 —— 所有自动化都必须做这一步。
        # 华为 VRP 对长输出（display interface brief 有 900+ 字符）会停在
        # ---- More ----，此时设备处于翻页状态，只接受空格/回车/q，
        # 下一条命令会被当成翻页输入而报 "Unrecognized command found at '^' position"。
        # 这一步漏掉时的表现极具误导性：命令明明是好的，单独跑也对，
        # 只是"紧跟在一个长输出命令之后"才失败。
        try:
            self.send_command("screen-length 0 temporary", timeout=8)
        except Exception as exc:  # noqa: BLE001
            log(f"关闭分页失败（不致命，继续）: {exc}")
        return True

    def send_command(self, command: str, timeout: float = 20.0,
                     idle: float = 0.7) -> str:
        """发一条命令，**等提示符出现**再返回输出。

        为什么必须有这个方法：靠 sleep 猜等待时间，在设备输出较长时会把
        下一条命令插进上一条的输出流里，设备收到的是残缺命令 ——
        实测表现是 `Error: Unrecognized command found at '^' position.`，
        而这条命令单独跑明明是好的。终端自动化的标准做法就是等提示符。

        判定"命令结束"用两个条件同时成立：
          1) 输出已静默（idle 秒内没有新数据）
          2) 缓冲区末尾是一个提示符字符（> 用户视图 / ] 系统视图）
        只看静默会在设备"思考"的间隙提前返回；只看提示符会被输出正文里
        的 > 误判。两个一起才可靠。
        """
        self.get_output()                      # 丢掉上一次的残留
        self.send_raw(command + "\r")
        end = time.time() + timeout
        idle_since = time.time()
        buf = ""
        while time.time() < end:
            time.sleep(0.25)
            chunk = self.get_output()
            if chunk:
                buf += chunk
                idle_since = time.time()
                continue
            tail = buf.rstrip()
            # 末尾可能是提示符后面跟了空格；取最后一个非空白字符判断
            if tail and tail[-1] in ">]" and (time.time() - idle_since) >= idle:
                break
        return buf

    def _wait_for_prompt(self, needle: str, timeout: float,
                         tail: int = 120) -> bool:
        """等缓冲区**末尾**出现某段文字。

        只在末尾找，是因为"任意位置包含"会命中历史上出现过的同名字串
        （横幅里的 Password、上一条命令回显里的 > 等），
        结果是**在设备还没准备好的时候就把输入发出去了**。
        """
        needle = needle.lower()
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if needle in self._buffer[-tail:].lower():
                    return True
            time.sleep(0.3)
        return False

    def _wait_sockjs_open(self, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                data = self._ws.recv()
            except Exception:
                continue
            if data and str(data).startswith("o"):
                return True
        return False

    # ---------------------------------------------------------------- STOMP
    def _frame(self, command: str, headers: dict, body: str = "") -> None:
        lines = [command] + [f"{k}:{v}" for k, v in headers.items()]
        if body:
            lines.append(f"content-length:{len(body.encode('utf-8'))}")
            lines.append("content-type:text/plain;charset=UTF-8")
        payload = "\n".join(lines) + "\n\n" + body + NUL
        self._ws.send(json.dumps([payload]))

    def _subscribe(self, destination: str) -> None:
        self._sub_id += 1
        self._frame("SUBSCRIBE", {"id": f"sub-{self._sub_id}",
                                  "destination": destination})

    def _send_sockjs(self, destination: str, body: str = "") -> None:
        self._frame("SEND", {"destination": destination}, body)

    def _ping_loop(self) -> None:
        """全局心跳，掉线会被服务端清理会话。"""
        while self._alive:
            time.sleep(3)
            try:
                self._send_sockjs(f"{SEND_PREFIX}{SUB_PREFIX}/ping", "ping")
            except Exception:
                return

    def _heartbeat_loop(self) -> None:
        while self._alive:
            time.sleep(30)
            try:
                self._send_sockjs(
                    f"{SEND_PREFIX}/console/heartbeat/{self._sandbox}/{self._node}")
            except Exception:
                return

    def _pump(self) -> None:
        while self._alive:
            try:
                data = self._ws.recv()
            except Exception:
                # 含超时；真正的断开会由 is_connected 反映
                if self._ws is None:
                    return
                continue
            if not data:
                continue
            text = data if isinstance(data, str) else data.decode("utf-8", "replace")
            if not text.startswith("a"):
                continue
            try:
                frames = json.loads(text[1:])
            except Exception:
                continue
            for frame in frames:
                self._on_stomp(frame)

    def _on_stomp(self, frame: str) -> None:
        head, _, body = frame.partition("\n\n")
        lines = head.split("\n")
        command = lines[0].strip()
        if command != "MESSAGE":
            return
        body = body.rstrip(NUL)
        # 服务端会在心跳订阅上回 "true"，那是协议噪声，不是终端内容
        if body.strip() in ("true", "false"):
            return
        with self._lock:
            self._buffer += body

        # 喂给输出监听器 —— 批量下发与巡检靠它判断"命令是否执行完"。
        # ssh / telnet / serial 三个连接器都有这一步，eNSP 之前漏了。
        # 漏掉时的表现很隐蔽：终端路径（轮询 get_output）完全正常，
        # 但巡检/批量会一直等不到输出，直到超时才返回空结果。
        self._notify_output(body)

    # ---------------------------------------------------------------- 接口
    def send(self, command: str):
        """发送一条命令（自动补回车）。"""
        self.send_raw((command or "") + "\r")

    def send_raw(self, data: str):
        """发送原始按键，不追加任何字符 —— 交互式输入走这条。"""
        if not self._connected:
            # 掉线自愈：先尝试重连一次，连不上就把原因写进 _last_error 再返回，
            # 不再像以前那样静默丢掉这条命令（用户会以为"点了没反应"）。
            if not self.ensure_connected():
                return
        try:
            self._send_sockjs(
                f"{SEND_PREFIX}/console/message/{self._sandbox}/{self._node}", data)
        except Exception as exc:
            self._last_error = f"发送失败: {exc}"
            self._connected = False

    def is_connected(self) -> bool:
        return bool(self._connected and self._ws is not None)

    def ensure_connected(self, retries: int = 1) -> bool:
        """确认连接还在；掉线就自动重连。

        为什么需要（2026-10-17 实测）：任何一次发送异常都会把 _connected 置 False
        （见 send_raw），而它**不会自愈** —— 之后采集快照/并发巡检/终端命令全部
        无声失败，用户只能自己重连。这里让它在下一次发送之前先自愈一次。
        """
        if self.is_connected():
            return True
        kwargs = getattr(self, "_connect_kwargs", None)
        if not kwargs:
            self._last_error = self._last_error or "连接已断开，且没有可复用的连接参数"
            return False
        for _attempt in range(max(1, retries)):
            try:
                self.disconnect()
            except Exception:                    # noqa: BLE001
                pass
            if self.connect(**kwargs):
                self._last_error = ""
                self.reconnect_count = getattr(self, "reconnect_count", 0) + 1
                return True
        self._last_error = self._last_error or "连接已断开，自动重连失败"
        return False

    def get_output(self) -> str:
        """取走并清空缓冲区（读后清空，与其它连接器语义一致）。"""
        with self._lock:
            data, self._buffer = self._buffer, ""
        return data

    def get_output_block(self, timeout: float = 2.0) -> str:
        """等待输出稳定后返回（无新数据达到 timeout 即认为这一段结束）。"""
        collected = self.get_output()
        last_len = len(collected)
        deadline = time.time() + timeout * 3
        while time.time() < deadline:
            time.sleep(0.25)
            chunk = self.get_output()
            if chunk:
                collected += chunk
                last_len = len(collected)
            elif collected:
                break
        return collected

    def disconnect(self):
        self._connected = False
        try:
            if self._ws is not None:
                self._send_sockjs(
                    f"{SEND_PREFIX}/console/close/{self._sandbox}/{self._node}")
                time.sleep(0.3)
        except Exception:
            pass
        self._alive = False
        time.sleep(0.2)
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None

    def resize_terminal(self, width: int, height: int):
        """把 xterm 尺寸同步给服务端（对应 /service/ensp/cmd/setting/resize）。"""
        try:
            self._session.post(
                self.base_url + "/service/ensp/cmd/setting/resize",
                json={"deviceId": self._node, "weight": int(width),
                      "height": int(height)}, timeout=8)
        except Exception:
            pass

    def info(self) -> str:
        extra = "（已自动完成设备登录）" if self._auto_login else ""
        return (f"eNSP Pro {self._host}:{self._port} "
                f"sandbox={self._sandbox} device={self._node}{extra}")

    @property
    def last_error(self) -> str:
        return self._last_error

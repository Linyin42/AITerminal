"""
批量命令下发 - 按顺序执行一串命令

两种等待方式：
    delay   —— 固定间隔（简单，但慢设备/长输出容易出错）
    prompt  —— 等设备提示符回来（默认，像人在终端里一条条敲）

prompt 模式靠 core.connector 的输出监听器观察回显，判断依据：
    1. 输出末尾出现提示符（> ] # $ % 结尾）且静默了 quiet_time
    2. 出现分页标记（---- More ----）时自动补一个空格翻页
    3. 出现 [Y/N] 之类的确认提示则停下，交给人工判断（不自动应答，避免误操作）
    4. 兜底：有输出但静默超过 silence_timeout，或总耗时超过 timeout

为什么要用输出监听器而不是读缓冲区：
    界面每 100ms 会把连接器缓冲区取走显示，批处理再去读就拿不到数据了；
    监听器是在读线程里同步回调的旁路通道，不消耗缓冲区，两者互不干扰。
"""
import time
import threading
import re
from typing import List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .ai_agent import AIAgent
from .terminal import TerminalBuffer


class CommandWaiter:
    """
    判断一条命令是否执行完成

    通过连接器的输出监听器收集回显，不读取（不消耗）连接器缓冲区，
    因此不会和界面的刷新抢数据。
    """
    #: 输出末尾是这些字符之一，认为回到了设备提示符
    PROMPT_CHARS = '>]#$%'

    #: 分页标记
    PAGING_RE = re.compile(
        r'(-{2,}\s*More\s*-{2,}|<---\s*More\s*--->|--More--|More:)',
        re.IGNORECASE
    )
    #: 需要人工确认的提示
    CONFIRM_RE = re.compile(
        r'(\[\s*[YyNn](\s*/\s*[YyNn])?\s*\]'
        r'|\(\s*[YyNn](\s*/\s*[YyNn])?\s*\)'
        r'|Are you sure|Continue\?|confirm\?)',
        re.IGNORECASE
    )

    def __init__(self, connector, quiet_time: float = 0.3, timeout: float = 30.0,
                 silence_timeout: float = 2.0, auto_page: bool = True,
                 max_pages: int = 200, tail_limit: int = 40000):
        self.connector = connector
        self.quiet_time = max(0.1, float(quiet_time))
        self.timeout = max(1.0, float(timeout))
        self.silence_timeout = max(0.5, float(silence_timeout))
        self.auto_page = bool(auto_page)
        self.max_pages = max(1, int(max_pages))
        self.tail_limit = tail_limit

        self._lock = threading.Lock()
        self._tail = ""                    # 近期输出（判定用，翻页时清空）
        self._collected: List[str] = []    # 本次命令的完整回显
        self._last_change = time.time()
        self._pages = 0
        self._registered = False

    # ------------------------------------------------------------ 生命周期
    def start(self):
        """注册监听并清空状态"""
        if not self._registered:
            self.connector.add_output_listener(self._on_output)
            self._registered = True
        self.reset()

    def stop(self):
        """注销监听"""
        if self._registered:
            self.connector.remove_output_listener(self._on_output)
            self._registered = False

    def reset(self):
        """清空本次命令的收集状态（发送命令前调用）"""
        with self._lock:
            self._tail = ""
            self._collected = []
            self._last_change = time.time()
            self._pages = 0

    def _on_output(self, data: str):
        with self._lock:
            self._tail += data
            if len(self._tail) > self.tail_limit:
                self._tail = self._tail[-self.tail_limit:]
            self._collected.append(data)
            self._last_change = time.time()

    # ------------------------------------------------------------ 观察
    def text(self) -> str:
        """本次命令收集到的完整输出（已剥离 ANSI）"""
        with self._lock:
            raw = "".join(self._collected)
        return TerminalBuffer.strip_ansi(raw)

    def _recent(self, length: int = 240) -> str:
        """近期输出的尾部（剥离 ANSI，用于判定）"""
        with self._lock:
            tail = self._tail[-length:]
        return TerminalBuffer.strip_ansi(tail)

    def _clear_tail(self):
        with self._lock:
            self._tail = ""

    @property
    def pages(self) -> int:
        return self._pages

    # ------------------------------------------------------------ 等待
    def wait(self) -> tuple:
        """
        等待命令执行完成

        返回: (状态, 耗时秒数)
            状态: prompt / silence / confirm / timeout / disconnected
        """
        started = time.time()
        deadline = started + self.timeout
        last_length = -1

        while True:
            if not self.connector.is_connected():
                return "disconnected", time.time() - started

            with self._lock:
                length = len(self._tail)
                quiet = time.time() - self._last_change
            if length != last_length:
                last_length = length

            recent = self._recent()
            stripped = recent.rstrip()

            # 1) 分页：自动补空格翻页（先清空判定窗口，再补空格，
            #    否则会把翻页后返回的内容一起清掉）
            if self.auto_page and stripped and self.PAGING_RE.search(stripped[-40:]):
                self._pages += 1
                if self._pages > self.max_pages:
                    return "timeout", time.time() - started
                self._clear_tail()
                last_length = -1
                try:
                    self.connector.send_raw(' ')
                except Exception:
                    pass
                time.sleep(0.2)
                continue

            # 2) 需要人工确认：停下来，不自动应答
            #    注意要排在提示符判定之前 —— "[Y/N]" 本身也以 ] 结尾
            if stripped and quiet >= self.quiet_time \
                    and self.CONFIRM_RE.search(stripped[-60:]):
                return "confirm", time.time() - started

            # 3) 提示符回来了且静默够久 → 认为执行完
            if stripped and stripped[-1] in self.PROMPT_CHARS and quiet >= self.quiet_time:
                return "prompt", time.time() - started

            # 4) 兜底：有输出但长时间没新内容
            if stripped and quiet >= self.silence_timeout:
                return "silence", time.time() - started

            if time.time() > deadline:
                return "timeout", time.time() - started

            time.sleep(0.05)

    # ------------------------------------------------------------ 组合操作
    def run(self, command: str):
        """
        下发一条命令并等待完成

        返回: (状态, 耗时, 输出)
        """
        self.reset()
        self.connector.send(command)
        status, elapsed = self.wait()
        return status, elapsed, self.text()


# ------------------------------------------------------------------ 关闭分屏
#: 关闭分屏的候选命令（按厂商）。
#: 注意：Comware 5 用 screen-length disable，Comware 7 才是 screen-length 0 temporary，
#: 用错了设备会回 % Unrecognized command，分屏照样在，长输出就会卡在 ---- More ----。
PAGING_OFF_COMMANDS = {
    "cisco": ("terminal length 0",),
    "ruijie": ("terminal length 0",),          # 锐捷 RGOS 与思科同款
    "huawei": ("screen-length 0 temporary", "screen-length disable"),
    "h3c": ("screen-length disable", "screen-length 0 temporary"),
    "unknown": ("screen-length disable", "screen-length 0 temporary",
                "terminal length 0"),
}

#: 设备拒绝命令时的典型响应特征
_REJECT_PATTERNS = (
    "unrecognized", "invalid input", "incomplete command", "too many parameters",
    "unknown command", "wrong parameter", "ambiguous", "not supported",
    "error:", "% error",
)


def disable_paging(connector, waiter=None, vendor: str = "unknown",
                   quiet_time: float = 0.3, timeout: float = 15.0):
    """
    关闭设备分屏（会按厂商依次尝试候选命令，并检查是否被设备拒绝）

    参数:
        connector: 已连接的连接器
        waiter: 复用的 CommandWaiter，留空则临时创建一个
        vendor: 厂商，用于决定先试哪条命令
    返回:
        (是否成功, 生效的命令, 设备响应文本)
    """
    candidates = PAGING_OFF_COMMANDS.get((vendor or "unknown").lower(),
                                         PAGING_OFF_COMMANDS["unknown"])
    own_waiter = waiter is None
    if own_waiter:
        waiter = CommandWaiter(connector, quiet_time=quiet_time, timeout=timeout,
                               silence_timeout=2.0, auto_page=True)
        waiter.start()
    try:
        last_output = ""
        for command in candidates:
            try:
                status, _elapsed, output = waiter.run(command)
            except Exception as e:
                last_output = f"{type(e).__name__}: {e}"
                continue
            last_output = output or ""
            if status not in ("prompt", "silence", "delay"):
                continue
            low = last_output.lower()
            if any(pattern in low for pattern in _REJECT_PATTERNS):
                continue          # 这条命令设备不认，试下一条
            return True, command, last_output.strip()
        return False, "", last_output.strip()
    finally:
        if own_waiter:
            waiter.stop()


#: 用户模式提示符：`Ruijie>` / `Switch>` / `Router>` / `S2928G-E>`。
#: **故意不含** `<H3C>` / `<Huawei>` 这种带尖括号的 —— 华为/华三没有 enable 这一层，
#: `<H3C>` 就是它们的普通视图，往里发 `enable` 只会得到 `% Unrecognized command`。
#: 也不含 `#`（已是特权模式）和 `[...]`（配置视图）。
USER_PROMPT_RE = re.compile(r'^[A-Za-z][\w.\-]*>\s*$')


def current_prompt(connector, waiter, timeout: float = 8.0) -> str:
    """敲一个空回车，取当前提示符（回显的最后一行非空行）。

    为什么要发一个回车而不是直接读缓冲：缓冲区可能停在半行（比如上一条命令
    的 `--More--`），空回车能让设备把提示符重新打一遍，是最便宜的状态探测。
    """
    old_timeout = waiter.timeout
    try:
        waiter.timeout = timeout
        waiter.reset()
        connector.send("")            # send("") 只发行结束符 = 敲回车
        waiter.wait()
        text = (waiter.text() or "").strip()
    finally:
        waiter.timeout = old_timeout
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line
    return ""


def in_user_mode(prompt: str) -> bool:
    """提示符是不是"用户模式"（需要 enable 才能做 show running-config 这类事）"""
    return bool(USER_PROMPT_RE.match((prompt or "").strip()))


def ensure_privileged(connector, waiter, password: str = "", timeout: float = 10.0):
    """设备停在用户模式（`Ruijie>`）时自动 `enable`，返回 (ok, 提示符, 说明)。

    **只切模式，不改配置**。ok=True 表示"当前确实在特权模式"——本来就在也算成功，
    所以调用方可以无脑调用，不必先判断。

    为什么需要它：真机测试（2026-10-23）发现设备停在 `Ruijie>` 时
    `terminal length 0` 直接 `% Unknown command`、`show running-config` 只回一行报错，
    巡检/批量拿不到配置，界面上却只显示"关分屏没成功"，看不出根因是模式。
    """
    prompt = current_prompt(connector, waiter, timeout=min(timeout, 8.0))
    if not in_user_mode(prompt):
        return True, prompt, f"已在特权模式（{prompt or '提示符未知'}）"

    old_timeout = waiter.timeout
    status = ""
    out = ""
    try:
        waiter.timeout = timeout
        status, _elapsed, out = waiter.run("enable")
        low = (out or "").lower()
        # 设备要密码时，提示符停在 `Password:` 上：既可能被判成 silence（没有提示符字符），
        # 也可能因为冒号进了 quiet 判定。两种都当成"要密码"。
        if not (waiter.text() or "").rstrip().endswith("#") and (
                "password" in low or status == "silence"):
            if not password:
                return False, prompt, ("设备要求 enable 密码，但没填"
                                       "（连接设置里可以填「enable 密码」）")
            waiter.timeout = timeout
            status, _elapsed, out = waiter.run(password)
    finally:
        waiter.timeout = old_timeout

    prompt2 = current_prompt(connector, waiter, timeout=min(timeout, 8.0))
    if prompt2.endswith("#"):
        return True, prompt2, f"已自动 enable（{prompt} → {prompt2}）"
    if "password" in (out or "").lower() or status == "silence":
        return False, prompt2, "enable 失败：密码不对（或设备没配 enable 密码却要求输入）"
    return False, prompt2, f"enable 没生效（当前提示符 {prompt2 or '未知'}）"


class BatchRunner(QObject):
    """
    批量命令执行器

    信号:
        progress(int, int, str):       当前序号(从1开始), 总数, 命令
        blocked(int, str):             被安全策略拦截的序号与命令
        command_done(int, str, float): 序号, 等待结果, 耗时秒
        finished(bool, str):           是否正常结束, 说明文字
    """

    progress = pyqtSignal(int, int, str)
    blocked = pyqtSignal(int, str)
    command_done = pyqtSignal(int, str, float)
    finished = pyqtSignal(bool, str)

    WAIT_DELAY = 'delay'
    WAIT_PROMPT = 'prompt'

    def __init__(self, connector, commands: List[str], delay_ms: int = 500,
                 block_dangerous: bool = True, logger=None, parent=None,
                 wait_mode: str = WAIT_PROMPT, prompt_timeout: float = 30.0,
                 quiet_time: float = 0.3, silence_timeout: float = 2.0,
                 auto_page: bool = True):
        super().__init__(parent)
        self.connector = connector
        self.commands = [c for c in (commands or []) if c.strip()]
        self.delay = max(0, int(delay_ms)) / 1000.0
        self.block_dangerous = block_dangerous
        self.logger = logger
        self.wait_mode = wait_mode if wait_mode in (self.WAIT_DELAY, self.WAIT_PROMPT) \
            else self.WAIT_PROMPT
        self.quiet_time = quiet_time
        self.silence_timeout = silence_timeout
        self.auto_page = auto_page
        self.prompt_timeout = prompt_timeout

        self._stop_flag = False
        self._thread: Optional[threading.Thread] = None
        self._waiter: Optional[CommandWaiter] = None
        self.results: List[dict] = []

    # ------------------------------------------------------------ 控制
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """在后台线程中开始执行"""
        if self.running:
            return False
        if not self.commands:
            self.finished.emit(True, "没有需要执行的命令")
            return False
        if self.connector is None or not self.connector.is_connected():
            self.finished.emit(False, "连接不可用，批量下发已取消")
            return False

        self._stop_flag = False
        self.results = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """请求中止（当前这条命令的等待会被打断）"""
        self._stop_flag = True

    # ------------------------------------------------------------ 执行
    def _run(self):
        total = len(self.commands)
        sent = 0
        skipped = 0
        mode_text = "等提示符返回" if self.wait_mode == self.WAIT_PROMPT \
            else f"固定间隔 {int(self.delay * 1000)}ms"

        if self.logger:
            self.logger.mark(f"批量下发开始，共 {total} 条命令，等待方式: {mode_text}")

        waiter = None
        if self.wait_mode == self.WAIT_PROMPT:
            waiter = CommandWaiter(
                self.connector, quiet_time=self.quiet_time,
                timeout=self.prompt_timeout, silence_timeout=self.silence_timeout,
                auto_page=self.auto_page,
            )
            waiter.start()
        self._waiter = waiter

        try:
            for index, command in enumerate(self.commands, start=1):
                if self._stop_flag:
                    self.finished.emit(
                        False, f"已中止：共 {total} 条，已下发 {sent} 条，跳过 {skipped} 条")
                    return
                if not self.connector.is_connected():
                    self.finished.emit(False, f"连接已断开：已下发 {sent}/{total} 条")
                    return

                if self.block_dangerous and AIAgent.is_dangerous(command):
                    skipped += 1
                    self.blocked.emit(index, command)
                    self.results.append({"index": index, "command": command,
                                         "status": "blocked", "elapsed": 0.0, "output": ""})
                    if self.logger:
                        self.logger.mark(f"安全拦截，跳过: {command}")
                    continue

                self.progress.emit(index, total, command)
                if self.logger:
                    self.logger.write(f"\n# >>> 下发: {command}\n")

                try:
                    if waiter is not None:
                        status, elapsed, output = waiter.run(command)
                        if output and self.logger:
                            self.logger.write(output)
                        if waiter.pages and self.logger:
                            self.logger.write(f"# （自动翻页 {waiter.pages} 次）\n")
                    else:
                        self.connector.send(command)
                        status, elapsed, output = "delay", self.delay, ""
                        if self.delay and index < total:
                            waited = 0.0
                            while waited < self.delay and not self._stop_flag:
                                time.sleep(0.05)
                                waited += 0.05
                except Exception as e:
                    self.finished.emit(False, f"发送失败（第 {index} 条）: {e}")
                    return

                sent += 1
                self.results.append({"index": index, "command": command,
                                     "status": status, "elapsed": elapsed,
                                     "output": output})
                self.command_done.emit(index, status, elapsed)

                if status == "confirm":
                    self.finished.emit(
                        False,
                        f"第 {index} 条命令需要人工确认（{command}），已暂停。"
                        "请切到终端手动应答后再下发剩余命令。"
                    )
                    return
                if status == "disconnected":
                    self.finished.emit(False, f"连接已断开：已下发 {sent}/{total} 条")
                    return
        finally:
            if waiter is not None:
                waiter.stop()

        summary = f"批量下发完成：共 {total} 条，已下发 {sent} 条"
        if skipped:
            summary += f"，安全拦截 {skipped} 条"
        if waiter is not None:
            slow = [r for r in self.results if r["status"] in ("timeout", "silence")]
            if slow:
                summary += f"，其中 {len(slow)} 条未检测到提示符"
        if self.logger:
            self.logger.mark(summary)
        self.finished.emit(True, summary)

    # ------------------------------------------------------------ 结果
    def get_results(self) -> List[dict]:
        """返回每条命令的执行结果（供导出）"""
        return list(self.results)

    def summary_rows(self):
        """生成结果汇总行（供 Excel/CSV 导出）"""
        status_text = {
            "prompt": "完成（提示符返回）",
            "silence": "完成（输出静默）",
            "delay": "已发送（固定间隔）",
            "confirm": "需人工确认",
            "timeout": "超时未完成",
            "blocked": "安全拦截",
            "disconnected": "连接断开",
        }
        rows = []
        for item in self.results:
            rows.append([
                item["index"], item["command"],
                status_text.get(item["status"], item["status"]),
                round(item.get("elapsed", 0.0), 1),
                (item.get("output") or "").strip(),
            ])
        return rows

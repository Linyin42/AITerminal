"""使用总结：本机记录 + 确定性归类 + 排错链（**零 AI**）。

为什么要放在引擎侧：引擎是**唯一**能同时看到"用户敲的命令"和"设备回显"的地方
（`session.write` 的交互字符、`session.command` 的整行、以及唯一的输出泵），
所以归类、串链、落盘都不需要 C++ 再铺一条数据通道 —— 界面只做"看"和"开关"。

红线（用户 2026-10 定）：
  · **只存本机**，路径固定 `%APPDATA%\\AITerminal\\usage-log\\YYYY-MM-DD.md`，永不上传；
  · **不许悄悄录**：monitor 关着且不在 AI 问答窗口内时，一个字都不写；
  · **无 AI 也要能用**：AI 归纳只是"顶部一句话"，没有 AI 时退化成纯列表，不报错；
  · 归类**全部是确定性规则**（正则），AI 不参与判定。

两种触发：
  ① 开始问 AI → 开一个"问答窗口"（默认 15 分钟），窗口内记录；
  ② 设置里打开"AI 时刻监视并总结" → 全程记录（默认关）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 归类规则（确定性）
#: "命令写错"——设备明确告诉你这句不合法
#:
#: ⚠ 这张表必须**按厂商措辞**覆盖，否则会静默地把"报错"记成「可用示例」——
#:   实测（2026-10-05 Eve-NG）：锐捷回 `% Unknown command.` 不在表里，于是同一台设备
#:   连回 3 次报错却被记成 3 条可用示例（ok=3 / wrong=0），比漏记更糟：
#:   它会让用户把一条**根本不存在的命令**当成范例。
#: 已核对过的措辞：
#:   H3C        `% Unrecognized command found at '^' position.`
#:   华为 VRP   `Error: Unrecognized command found at '^' position.` / `Error: Wrong parameter found …`
#:   Cisco IOS  `% Invalid input detected at '^' marker.` / `% Incomplete command.` / `% Ambiguous command:`
#:   Cisco XR   `% Invalid input detected at '^' marker.`
#:   NX-OS      `% Invalid command at '^' marker.`
#:   锐捷       `% Unknown command.` / `% Incomplete command.`
#:   Junos      `unknown command.` / `syntax error, expecting <command>.`
#:   通用       出错位置用 `^` 单独标一行
#: ⚠ **不要**加"以 % 开头就算错"这种通配：Cisco 的 syslog 行形如
#:   `%LINEPROTO-5-UPDOWN: …`，也是 % 开头，但那是正常日志。
WRONG_PATTERNS = [
    r"%\s*Unrecognized command",
    r"%\s*Wrong parameter",
    r"%\s*Incomplete command",
    r"%\s*Ambiguous",               # Cisco `% Ambiguous command:` / H3C `% Ambiguous`
    r"%\s*Too many parameters",
    r"%\s*Invalid input",           # Cisco IOS/XR `% Invalid input detected at '^' marker.`
    r"%\s*Invalid command",         # NX-OS `% Invalid command at '^' marker.`
    r"%\s*Unknown command",         # 锐捷 `% Unknown command.`
    r"\bUnknown command\b",         # Junos `unknown command.`
    r"\bUnknown action\b",          # Fortinet `Unknown action 0`
    r"\bUnrecognized command\b",
    r"\bIncomplete command\b",      # 不带 % 前缀的写法（锐捷/部分厂商）
    r"\bWrong parameter\b",
    r"\bsyntax error\b",            # Junos `syntax error, expecting <command>.`
    r"Error:\s*\S",
    r"Invalid input",
    r"Unrecognized command found",
    r"%\s*Please input the correct",
    r"^\s*\^\s*$",                  # 设备把出错位置用 ^ 标出来
]

#: "疑似不通"——不是语法错，是"发出去没人应/不通"，与上一类**分开**
DEAD_PATTERNS = [
    r"Request time ?out",
    r"request timeout",
    r"timeout is \d+ second",
    r"Destination host unreachable",
    r"Destination unreachable",
    r"not reachable",
    r"Connection timed ?out",
    r"100(\.0)?% packet loss",
    r"Success rate is 0 percent",   # Cisco ping 全丢
    r"No route to host",
    r"Host is down",
    r"Connection refused",
]

_WRONG_RE = [re.compile(p, re.I) for p in WRONG_PATTERNS]
_DEAD_RE = [re.compile(p, re.I) for p in DEAD_PATTERNS]

#: 提示符形态：`<H3C>` / `[SW1]` / `[H3C-GigabitEthernet1/0/1]` / `Ruijie>` / `Switch#` / `host(config)#`
#: 以及 **IOS XR** 的 `RP/0/RP0/CPU0:ios#`、**Junos** 的 `root@vmx1>` —— 这两家提示符里带
#: `/` `:` `@`，早期版本漏了它们，会让 XR/Junos 的命令**永远结算不了**（2026-10-05 补）。
#: **提示符只是"回到可输入状态"，不是"上一条命令执行成功"的证据** ——
#: 命令被分页器吞掉时，设备就只回一个提示符（实测 HCL）。
#: 但它同时是"设备已经进入可输入状态"的证据 —— `_maybe_success` 拿它当前置条件。
PROMPT_ONLY_RE = re.compile(
    r"^(<[^>]{1,40}>"
    r"|\[[^\]]{1,60}\]"
    r"|[A-Za-z0-9_.\-/:@]{1,48}(#[>]?|>[>]?)"
    r"|[A-Za-z0-9_.\-/:@]{1,48}\([^)]{0,30}\)[#>])$")

#: 心跳/刷屏类噪声：同一行连续重复超过这次数就不再记
HEARTBEAT_LIMIT = 3
#: 回显关键行截断长度（用户定：~200 字符）
ECHO_CHARS = 200
#: 成功命令去重窗口（秒）——同一台设备同一命令在这之内重复不记
OK_DEDUP_SECONDS = 600


def classify(line: str) -> str:
    """一行设备回显 → 'wrong' / 'dead' / ''（不归类就返回空串）。"""
    text = line.strip()
    if not text:
        return ""
    for rx in _WRONG_RE:
        if rx.search(text):
            return "wrong"
    for rx in _DEAD_RE:
        if rx.search(text):
            return "dead"
    return ""


@dataclass
class Entry:
    """一条记录。kind: question / wrong / dead / ok。"""

    kind: str
    ts: float
    device: str
    text: str                       # 命令原文 / 提问原文
    echo: str = ""                  # 回显关键行（截断）
    chain_id: int = 0               # >0 表示属于某条排错链
    resolved: bool = False
    id: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "ts": self.ts,
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "device": self.device, "text": self.text, "echo": self.echo,
            "chain": self.chain_id, "resolved": self.resolved,
        }


@dataclass
class Chain:
    """排错链：报错命令 → 后续命令（…）→ 最后成功的那条。"""

    id: int
    device: str
    started: float
    trigger: str = ""               # 触发它的那条命令
    trigger_echo: str = ""
    steps: List[int] = field(default_factory=list)   # 后续命令 entry id
    resolved_by: int = 0            # 最后成功的那条 entry id
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "device": self.device,
            "time": time.strftime("%H:%M:%S", time.localtime(self.started)),
            "trigger": self.trigger, "trigger_echo": self.trigger_echo,
            "steps": self.steps, "resolved_by": self.resolved_by,
            "resolved": self.resolved,
        }


class UsageLog:
    """一份进程内的使用记录（引擎单例）。落盘是"追加一行 markdown"，不做数据库。"""

    def __init__(self, base_dir: str = "", monitor: bool = False) -> None:
        self.base_dir = base_dir or self._default_dir()
        self._monitor = bool(monitor)
        self._ai_until = 0.0
        self._entries: List[Entry] = []
        self._chains: List[Chain] = []
        self._seq = 0
        self._chain_seq = 0
        self._pending: Dict[str, Dict[str, Any]] = {}    # device → 待判定的命令
        self._last_line: Dict[str, str] = {}             # device → 上一行回显
        self._repeat: Dict[str, int] = {}                # device → 连续重复次数
        self._ok_seen: Dict[str, float] = {}             # "device\x00cmd" → ts
        self._open_chain: Dict[str, int] = {}            # device → 当前排错链 id
        self._last_cmd: Dict[str, str] = {}              # device → 最近一条命令（失败兜底用）
        # ↓ 2026-10-05 新增：应对"设备把一行切成多片送来"（实测 Eve-NG 的串口代理会切成
        #   1~3 字节的碎块）。三件状态各修一个坑，见 note_output / _consume_echo 的注释。
        self._line_buf: Dict[str, str] = {}              # device → 还没收到换行的半行
        self._echo_acc: Dict[str, str] = {}              # device → 已收到的命令回显累积
        self._prompt_seen: Dict[str, bool] = {}          # device → 见过可输入提示符没有

    # ---------------------------------------------------------------- 基本状态
    @staticmethod
    def _default_dir() -> str:
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, "AITerminal", "usage-log")

    def set_monitor(self, on: bool) -> None:
        self._monitor = bool(on)

    @property
    def monitor(self) -> bool:
        return self._monitor

    def begin_ai_window(self, minutes: float = 15.0) -> None:
        """触发①：开始问 AI → 开一个记录窗口（只覆盖问答期间）。"""
        self._ai_until = max(self._ai_until, time.time() + minutes * 60)

    def ai_window_left(self) -> float:
        return max(0.0, self._ai_until - time.time())

    def active(self) -> bool:
        """现在该不该记 —— 关着开关又不在问答窗口内就是"不记"。"""
        return self._monitor or time.time() < self._ai_until

    def _why(self) -> str:
        if self._monitor:
            return "monitor"          # 设置里开着"时刻监视"
        if time.time() < self._ai_until:
            return "ai-window"        # 只在问答窗口内
        return "off"

    # ---------------------------------------------------------------- 记录
    def note_question(self, text: str, device: str = "AI 助手") -> Optional[Entry]:
        """用户问 AI 的原话（**不受开关限制**：问都问了，问题本身要留档）。"""
        text = (text or "").strip()
        if not text:
            return None
        self.begin_ai_window()
        return self._add("question", device, text, "")

    def note_command(self, device: str, command: str) -> None:
        """用户提交了一条命令（先挂起，等回显判成败）。"""
        self._trace("note_command", device=device, command=command,
                    active=self.active(), monitor=self._monitor)
        if not self.active():
            return
        command = (command or "").strip()
        if not command:
            return
        self._pending[device] = {"cmd": command, "ts": time.time(), "failed": False}
        # 新命令作废上一轮的回显累积（否则上一轮残留会让新回显的前缀判断错位）。
        # 行缓冲**不清** —— 那是设备侧还没收完的半个输出行，与命令无关。
        self._echo_acc.pop(device, None)
        # 最近一条命令：失败可能来得比"成功判定"晚（ping 先出一行头、再超时），
        # 那时挂起命令已被消费，得靠这个兜底，否则记录里原文是空的。
        self._last_cmd[device] = command

    def note_output(self, device: str, text: str) -> None:
        """设备回显（逐块）——归类的**唯一入口**。

        ⚠ 传进来的 `text` 是**socket 读到的一块**，不是一行。实测 Eve-NG 的串口代理
        会把一行切成 1~3 字节的碎块（`note_output n=` 序列 `[10,1,7,3,26,2,88,2,538,…]`），
        所以这里必须做两件事，否则会静默漏判/误判：

        1. **先在文本层吃掉"命令回显"**（`_consume_echo`）—— 回显也会被切，
           逐片做 `cmd in text` 的精确匹配必然失败，第二片会被当成命令输出，
           于是凭空记一条「可用示例」，回显关键行还是半截命令。
        2. **再做行缓冲**（`_line_buf`）—— 只有收到换行才算一行完整。
           不缓冲的话 `Request time out` 会被切成 `R` + `equest time out`，
           两条都不匹配 DEAD 模式 ⇒ 真正的"不通"反而漏记；
           首行输出被切碎时，「回显关键行」也会变成 `System d` 这种半截（串口 9600 波特实测）。
        """
        self._trace("note_output", device=device, n=len(text or ""),
                    active=self.active())
        if not self.active():
            return
        rest = self._consume_echo(device, text or "")
        if not rest:
            return
        buf = self._line_buf.get(device, "") + rest
        while True:
            m = re.search(r"[\r\n]", buf)
            if not m:
                break
            line, buf = buf[:m.start()], buf[m.end():]
            self._handle_line(device, line)
        # 尾部没有换行：提示符/分页提示**本身不带换行**，必须单独放行，
        # 否则它们会永远卡在缓冲里 ⇒ 既漏掉"设备已可输入"这个事实，也漏掉噪声过滤。
        tail = buf.strip()
        if tail and (PROMPT_ONLY_RE.match(tail)
                     or re.match(r"^-+\s*More\s*-+$", tail, re.I)):
            buf = ""
            self._handle_line(device, tail)
        self._line_buf[device] = buf

    def _consume_echo(self, device: str, text: str) -> str:
        """吃掉设备回显，返回**真正的输出**部分。

        判定方式：把"提交命令之后收到的片段"累积起来，只要它仍是命令本身的前缀，
        就说明还在回显中，本次不返回任何内容；一旦累积长度够了、且前缀与命令逐字相符，
        就把命令那一段剥掉，剩下的才算输出。

        为什么不能逐片比：设备回显的是**命令原文**，但它会被切成多片
        （实测 `sc` + `reen-length disable`、`di` + `splay version`、`pi` + `ng -c 1 …`），
        单片既不等于命令也不包含命令。
        """
        pending = self._pending.get(device)
        cmd = (pending or {}).get("cmd") or ""
        if not pending or pending.get("failed") or len(cmd) < 2:
            self._echo_acc.pop(device, None)
            return text
        acc = self._echo_acc.get(device, "") + text
        if len(acc) < len(cmd):
            if cmd.startswith(acc):          # 仍是命令前缀 ⇒ 还在回显，等下一片
                self._echo_acc[device] = acc
                return ""
            self._echo_acc.pop(device, None)  # 已经不像回显了，按输出处理
            return acc
        if acc[:len(cmd)] == cmd:            # 回显刚好结束，后面就是输出
            self._echo_acc.pop(device, None)
            return acc[len(cmd):]
        self._echo_acc.pop(device, None)
        return acc

    def _handle_line(self, device: str, line: str) -> None:
        """处理**一行完整**回显。"""
        line = line.rstrip()
        if not line.strip():
            return
        if self._is_noise(device, line):
            return
        kind = classify(line)
        if kind:
            self._record_failure(device, kind, line)
            return
        # 提示符 ⇒ 设备回到了可输入状态。这**不是**"上一条命令成功"的证据（分页器吞命令时
        # 也只有一个提示符），但它是"设备确实在可输入状态"的证据，`_maybe_success` 用它兜底。
        if PROMPT_ONLY_RE.match(line.strip()):
            self._prompt_seen[device] = True
        # 没有报错 → 挂起的那条命令算成功（"最后成功的那条"）
        self._maybe_success(device, line)
        self._last_line[device] = line

    def _is_noise(self, device: str, line: str) -> bool:
        if self._last_line.get(device) == line:
            self._repeat[device] = self._repeat.get(device, 0) + 1
            if self._repeat[device] > HEARTBEAT_LIMIT:
                return True
        else:
            self._repeat[device] = 0
        # 分页提示（---- More ----）不是错误也不是成功，别搅乱判定
        if re.match(r"^\s*-+\s*More\s*-+\s*$", line, re.I):
            return True
        return False

    def _record_failure(self, device: str, kind: str, line: str) -> None:
        # 回显关键行 = **触发分类的那一行**放在最前（之前把上一行放前面，
        # 表格一窄就被省略号截掉，用户看不到 `Request time out` 这种关键信息），
        # 上一行只作为补充短截断附在后面。
        prev = (self._last_line.get(device, "") or "").strip()
        echo = line.strip()[:160]
        if prev:
            echo = "%s　←　%s" % (echo, prev[:60])
        echo = echo[:ECHO_CHARS]
        pending = self._pending.get(device) or {}
        cmd = pending.get("cmd") or self._last_cmd.get(device, "")
        # 同一台设备、同一条命令、同一条报错在 5 秒内只记一次：
        # 设备（和输出泵的分块读取）经常把同一句报错吐两遍，实测 HCL 上
        # `display versionn` 就被记了两条。
        recent = [e for e in reversed(self._entries)
                  if e.device == device and e.kind == kind]
        if recent and recent[0].text == cmd and time.time() - recent[0].ts < 5.0:
            return
        # 触发排错链：报错命令 + 后续命令 + 最后成功的那条
        chain_id = self._open_chain.get(device, 0)
        if not chain_id:
            self._chain_seq += 1
            chain_id = self._chain_seq
            self._chains.append(Chain(id=chain_id, device=device, started=time.time(),
                                      trigger=cmd, trigger_echo=echo[:ECHO_CHARS]))
            self._open_chain[device] = chain_id
        # 同一条命令 20 秒内刚被记成 ok、现在却报错/超时 → **撤回那条 ok**
        # （否则 `ping` 超时那条会同时躺在「可用示例」里 —— 属于"记错"）
        for prev in reversed(self._entries):
            if (prev.device == device and prev.kind == "ok" and prev.text == cmd
                    and time.time() - prev.ts < 20.0):
                # 撤回而不是并存：否则同一次超时会留下两条（一条"降级"一条新记）
                self._entries.remove(prev)
                if prev.chain_id and not chain_id:
                    chain_id = prev.chain_id
                self._trace("downgraded_ok", device=device, text=cmd, to=kind)
                break
        entry = self._add(kind, device, cmd, echo, chain_id=chain_id)
        if pending:
            pending["failed"] = True
            pending["entry"] = entry.id
            self._pending[device] = pending

    def _maybe_success(self, device: str, line: str) -> None:
        pending = self._pending.get(device)
        if not pending or pending.get("failed"):
            return
        cmd = pending.get("cmd") or ""
        if not cmd:
            return
        if len(cmd) < 2:
            return
        # ⚠ 设备**还没到过可输入提示符**时，敲进去的字会被当成噪声吞掉，但设备仍会吐
        #   一大段东西（首启向导 / license 声明）。那种情况下"来了输出"完全不代表命令执行成功。
        #   实测（2026-10-05 Eve-NG）：Cisco XRv 整个 37s 都在打 IOS XRv 的 license banner，
        #   探针敲的 3 条命令全被记成「可用示例」—— 比漏记更糟，等于教用户用错命令。
        #   所以把"这台设备见过提示符"当前置条件（提示符在 `_handle_line` 里置位）。
        if not self._prompt_seen.get(device):
            return
        # 回显**不是**"执行成功"的证据（第一版把它当成功，导致后面的 `% Unrecognized command`
        # 找不到挂起命令，排错链的触发命令变成空串 —— 实测踩到）。
        # 主路径已在 `_consume_echo` 里按**文本累积**剥掉回显，这里留一次逐字兜底，
        # 防止某台设备的回显与命令有细微差异（多空格/大小写）而没被剥干净。
        text = line.strip()
        if text.replace(" ", "") == cmd.replace(" ", "") or (len(cmd) >= 6 and cmd in text):
            return
        # 提示符/分页提示不算"执行成功"（bug1：否则被吞掉的命令会被记成可用示例）
        if PROMPT_ONLY_RE.match(text) or re.match(r"^-+\s*More\s*-+$", text, re.I):
            return
        if len(text) < 4:
            return
        key = device + "\x00" + cmd
        now = time.time()
        if now - self._ok_seen.get(key, 0.0) < OK_DEDUP_SECONDS:
            # bug2：这次成功**因去重没新建记录**，但排错链仍然要闭合 ——
            # 链的最后一步指向"已有的那条" ok 记录（否则链永远停在未解决）。
            chain_id = self._open_chain.pop(device, 0)
            if chain_id:
                chain = self._chain(chain_id)
                prev = next((e for e in reversed(self._entries)
                             if e.kind == "ok" and e.device == device and e.text == cmd), None)
                if chain and prev is not None:
                    if prev.id not in chain.steps:
                        chain.steps.append(prev.id)
                    chain.resolved_by = prev.id
                    chain.resolved = True
            self._pending.pop(device, None)
            return
        self._ok_seen[key] = now
        chain_id = self._open_chain.get(device, 0)
        entry = self._add("ok", device, cmd, line.strip()[:ECHO_CHARS], chain_id=chain_id)
        if chain_id:
            chain = self._chain(chain_id)
            if chain:
                chain.steps.append(entry.id)
                chain.resolved_by = entry.id
                chain.resolved = True          # 串到"最后成功的那条"就闭环
            self._open_chain.pop(device, None)
        self._pending.pop(device, None)

    def _trace(self, event: str, **fields) -> None:
        path = os.environ.get("AIT_USAGE_TRACE") or ""
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                extra = " ".join("%s=%s" % (k, v) for k, v in fields.items())
                fh.write("%s [L3-recorder] %s %s\n"
                         % (time.strftime("%H:%M:%S"), event, extra))
        except OSError:
            pass

    def _add(self, kind: str, device: str, text: str, echo: str,
             chain_id: int = 0) -> Entry:
        self._seq += 1
        entry = Entry(kind=kind, ts=time.time(), device=device, text=text,
                      echo=echo[:ECHO_CHARS], chain_id=chain_id, id=self._seq)
        self._entries.append(entry)
        self._append_md(entry)
        self._trace("recorded", kind=kind, device=device, text=text,
                    file=self.today_file())
        return entry

    def _chain(self, chain_id: int) -> Optional[Chain]:
        for c in self._chains:
            if c.id == chain_id:
                return c
        return None

    def _entry(self, entry_id: int) -> Optional[Entry]:
        for e in self._entries:
            if e.id == entry_id:
                return e
        return None

    # ---------------------------------------------------------------- 人工修正
    def mark_resolved(self, chain_id: int, resolved: bool = True) -> bool:
        chain = self._chain(chain_id)
        if not chain:
            return False
        chain.resolved = bool(resolved)
        return True

    def merge(self, chain_id: int, entry_id: int) -> bool:
        """把一条记录并入某条排错链（人工合并）。"""
        entry = self._entry(entry_id)
        chain = self._chain(chain_id)
        if not entry or not chain:
            return False
        entry.chain_id = chain_id
        if entry.id not in chain.steps:
            chain.steps.append(entry.id)
        return True

    def split(self, entry_id: int) -> bool:
        """把一条记录从链里摘出来（人工拆分）。"""
        entry = self._entry(entry_id)
        if not entry:
            return False
        chain = self._chain(entry.chain_id)
        if chain and entry.id in chain.steps:
            chain.steps.remove(entry.id)
            if chain.resolved_by == entry.id:
                chain.resolved_by = 0
                chain.resolved = False
        entry.chain_id = 0
        return True

    def clear(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        self._chains.clear()
        self._pending.clear()
        self._ok_seen.clear()
        self._open_chain.clear()
        # 分片相关的三份状态也要清，否则"清空本次"之后第一行会被上一次的半行污染
        self._line_buf.clear()
        self._echo_acc.clear()
        self._prompt_seen.clear()
        self._seq = self._chain_seq = 0
        return n

    # ---------------------------------------------------------------- 查询
    def groups(self) -> Dict[str, List[Dict[str, Any]]]:
        def pick(kind: str) -> List[Dict[str, Any]]:
            return [e.to_dict() for e in self._entries if e.kind == kind]
        return {
            "questions": pick("question"),
            "wrong": pick("wrong"),
            "dead": pick("dead"),
            "ok": pick("ok"),
        }

    def chains(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self._chains]

    def search(self, keyword: str) -> List[Dict[str, Any]]:
        """按"现象"搜索：命令原文 + 回显都搜（确定性，不是语义检索）。"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return [e.to_dict() for e in self._entries]
        return [e.to_dict() for e in self._entries
                if kw in e.text.lower() or kw in e.echo.lower()
                or kw in e.device.lower()]

    def state(self) -> Dict[str, Any]:
        counts = {"question": 0, "wrong": 0, "dead": 0, "ok": 0}
        for e in self._entries:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        return {
            "monitor": self._monitor,
            "active": self.active(),
            "reason": self._why(),
            "aiWindowLeft": int(self.ai_window_left()),
            "counts": counts,
            "total": len(self._entries),
            "chains": len(self._chains),
            "logDir": self.base_dir,
            "todayFile": self.today_file(),
        }

    # ---------------------------------------------------------------- 落盘
    def today_file(self) -> str:
        return os.path.join(self.base_dir,
                            time.strftime("%Y-%m-%d") + ".md")

    def _append_md(self, entry: Entry) -> None:
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            new = not os.path.exists(self.today_file())
            with open(self.today_file(), "a", encoding="utf-8") as fh:
                if new:
                    fh.write("# 使用总结 %s\n\n" % time.strftime("%Y-%m-%d"))
                    fh.write("> 仅本机保存（%s）。这里只有设备回显原文与用户输入原文，"
                             "没有 AI 生成的新事实。\n\n" % self.base_dir)
                label = {"question": "我问过的问题", "wrong": "命令写错",
                         "dead": "疑似不通", "ok": "可用示例"}.get(entry.kind, entry.kind)
                stamp = time.strftime("%H:%M:%S", time.localtime(entry.ts))
                fh.write('<a id="e%d"></a>\n' % entry.id)
                fh.write("### [%s] %s · %s\n\n"
                         % (stamp, label, entry.device))
                fh.write("- 原文：`%s`\n" % entry.text.replace("`", "'"))
                if entry.echo:
                    fh.write("- 回显关键行：`%s`\n" % entry.echo.replace("`", "'"))
                if entry.chain_id:
                    fh.write("- 排错链：#%d%s\n"
                             % (entry.chain_id, "（已解决）" if entry.resolved else ""))
                fh.write("\n")
        except OSError:
            # 落盘失败**不能**影响终端使用：记录是附加功能
            pass

    def to_markdown(self) -> str:
        lines = ["# 使用总结（本次会话）", ""]
        groups = self.groups()
        titles = [("questions", "我问过的问题"), ("wrong", "命令写错"),
                  ("dead", "疑似不通"), ("ok", "可用示例")]
        for key, title in titles:
            items = groups.get(key) or []
            lines.append("## %s（%d）" % (title, len(items)))
            for e in items:
                lines.append("- `%s` %s · %s%s" % (
                    e["text"], e["time"], e["device"],
                    ("　回显：" + e["echo"]) if e["echo"] else ""))
            lines.append("")
        chains = self.chains()
        if chains:
            lines.append("## 排错链（%d）" % len(chains))
            for c in chains:
                lines.append("- #%d %s %s → 后续 %d 步 → %s"
                             % (c["id"], c["time"], c["trigger"] or "(未知)",
                                len(c["steps"]),
                                ("已解决" if c["resolved"] else "未解决")))
            lines.append("")
        return "\n".join(lines)

    def write_candidate(self, entry_id: int) -> Dict[str, Any]:
        """「升级进知识库」→ 只生成**待确认候选**文件，人确认后才入库（不自动入库）。"""
        entry = self._entry(entry_id)
        if not entry:
            return {"ok": False, "error": "记录不存在"}
        try:
            cdir = os.path.join(self.base_dir, "candidates")
            os.makedirs(cdir, exist_ok=True)
            path = os.path.join(cdir, "%s-e%d.md" % (time.strftime("%Y-%m-%d"), entry.id))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# 待确认候选（未入库）\n\n")
                fh.write("- 来源：%s · %s\n" % (entry.device, time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(entry.ts))))
                fh.write("- 现象/命令：`%s`\n" % entry.text.replace("`", "'"))
                if entry.echo:
                    fh.write("- 回显：`%s`\n" % entry.echo.replace("`", "'"))
                fh.write("\n> 人工确认后才应写入 knowledge/；本文件不会被自动读取。\n")
            return {"ok": True, "path": path}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

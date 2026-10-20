#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技术解释工具：**让 AI 把厂商文档原文整理成结构化技术卡片**，写回资料层。

它在整条链路里的位置
====================
    _vendor_docs（厂商原始文档）
        │  tools/build_tech_docs.py        纯代码提取，零 AI
        ▼
    knowledge/<vendor>/tech/*.md          厂商原文（可信，不可改写）
        │  tools/gen_tech_summary.py       ← **本工具**，唯一允许调模型的一步
        ▼
    knowledge/<vendor>/tech/ai/*.md       AI 整理稿（物理隔离，带醒目免责声明）
        │  core/tech_knowledge.py         纯代码检索
        ▼
    界面 / RPC：先查厂商原文 → 再查 AI 整理稿 → 都没有才现场上 AI

三条不可让步的规矩
==================
1. **先有原文，才允许 AI 整理。** 原文没提取到的 feature 直接跳过，绝不让模型凭自己
   的知识写 —— 那样产出的东西没有任何可核查的来源，比"没有"更糟。
2. **原文是唯一依据。** 提示词里明确要求"原文未涉及"就写"原文未涉及"，
   不允许引入原文以外的命令、参数、默认值、数字。
3. **产物物理隔离。** 一律写进 `tech/ai/`，文件头带"本文件由 AI 生成"的声明。
   厂商原文文件**一个字都不改** —— 否则以后没人分得清哪句是厂商说的。

为什么用 AI 而不是纯代码
======================
"一句话定义 / 适用场景 / 关键点 / 注意事项"这种**面向人的归纳**，纯代码做不了
（原文是给工程师看的操作手册，不是摘要）。但归纳的**材料**必须是真实原文，
所以这一步的输入严格限定为 `build_tech_docs.py` 提取出的正文，不含任何原始 HTML。

用法
====
    python tools/gen_tech_summary.py --vendor h3c --limit 2 --dry-run   # 只看提示词
    python tools/gen_tech_summary.py --vendor h3c --limit 3             # 先小样试
    python tools/gen_tech_summary.py --vendor h3c                       # 全量（可断点续跑）
    python tools/gen_tech_summary.py --vendor h3c --feature OSPF --force

    --stats      只看"已整理 / 待整理"统计，不调模型
    --limit N    每个厂商最多处理 N 个 feature
    --feature X  只处理叶子名等于 X 的 feature
    --force      已有 AI 条目的也重做（默认跳过，便于续跑）
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tech_knowledge as tk            # noqa: E402

#: `tk.learn()` 是「读 `ai/index.json` → 改 → 写回」，**并发会丢更新**，
#: 所以写盘必须串行。API 调用（十几秒）留在锁外 —— 并行度不受影响。
_WRITE_LOCK = threading.Lock()
#: 并发时 print 会交错，输出没法看。
_PRINT_LOCK = threading.Lock()

VENDORS = ("h3c", "huawei", "ruijie", "tplink")

#: 单次请求塞给模型的原文上限（字符）。原文动辄几百 KB，必须截；
#: 截到开头部分是因为厂商文档的"概述/配置任务"总在前面，最能支撑"一句话定义"。
MAX_INPUT_CHARS = 14000
#: 生成上限。卡片很短，给多了只会让模型开始自由发挥。
#: ⚠ 别压到 1400 以下：推理模型（LongCat-2.x）的"思考"token 和正文**共享**这个预算，
#: 预算不够时会返回**空正文**（表现为"产物不合格：缺少标题 ## 一句话定义"，
#: 看着像模型不听话，其实是预算被思考吃光了）。现在默认关掉思考（见 apply_thinking），
#: 留 2600 是给个别超长卡片（原文里注意事项特别多）的余量。
MAX_OUTPUT_TOKENS = 2600
#: 两次请求之间的间隔（秒）。**别去掉** —— 全量 569 个 feature，
#: 打满会被限流（429），反而更慢。
SLEEP_BETWEEN = 1.0

#: 单次调用失败后的重试次数。**全量七千多个 feature，一次跑好几个钟头**，
#: 中途撞上一次 429 / 连接重置就丢一张卡，事后还得再补跑一轮 —— 不划算。
#: 失败重试的代价远低于重跑。
MAX_RETRY = 3
#: 判定"值得重试"的失败特征（限流 / 超时 / 连接类）。命中就退避重试，
#: 不命中（比如模型返回了不合格的产物）立刻放弃，重试也是白费。
RETRYABLE_HINTS = ("429", "rate limit", "too many requests", "timeout",
                   "timed out", "connection", "reset", "502", "503", "504",
                   "temporarily", "overloaded")

#: ⭐ 产物不合格时的**最后抢救**：改用「开思考」的独立 client 再试一次。
#:
#: 为什么需要（2026-10-07 实测定位）
#: --------------------------------
#: `apply_thinking(off)` 对**绝大多数** feature 是对的（省 token、避免思考吃光预算）。
#: 但对**原文形态不标准的**那几类 —— 术语表、缩略语汇总、附录（多段零散资料拼接）
#: —— 关掉思考后模型不会去琢磨"该怎么处理这种输入"，直接**摆烂拒绝**：
#:     ## 原文未涉及
#:     原文为术语解释汇总，未针对单一命令提供结构化技术卡片所需的场景、配置
#:     关键点、注意事项及关联命令信息。
#: （实测 38~64 字，恒判"返回过短"）。**它不是在判断自己会不会做，而是在判断该不该做。**
#:
#: 同一 prompt 打开思考后能正常产出（实测 术语 257 字 / 附录 536 字 / 缩略语 254 字）。
#: 所以这不是"schema 不兼容"，**不需要给词表类加宽松分支**（那会污染格式）。
#:
#: 触发条件刻意收得很紧：**只有常规 3 次重试全部不合格**才启用。
#: 这样已成功的 5800+ 个 feature 行为完全不变，只兜住边界情况。
#: 设 `AIT_THINKING_RESCUE=0` 可关闭。
THINKING_RESCUE = os.environ.get("AIT_THINKING_RESCUE", "1") != "0"
#: 抢救时给更大的输出预算 —— 思考 token 与正文**共享** max_tokens，
#: 2600 在个别超长卡片上会把正文挤没（实测报过"思考占满预算、正文为空"）。
RESCUE_MAX_TOKENS = 6000

_rescue_cli = None
_rescue_lock = threading.Lock()


def rescue_client():
    """开思考的**独立** client。

    ⚠ 必须独立实例，不能改共享的 `cli.extra_body` ——
    `run_vendor` 是 6 并发，改共享对象会把别的 worker 的请求也带偏。
    """
    global _rescue_cli
    with _rescue_lock:
        if _rescue_cli is None:
            c = client_or_die("")
            if getattr(c, "provider", "") == "longcat":
                c.extra_body = {"thinking": {"type": "enabled"}}
            _rescue_cli = c
    return _rescue_cli


def rescue_call(prompt: str, feat: str, why: str) -> tuple[str, str]:
    """开思考抢救一次。返回 (card, why)：card 非空即成功，why 非空即仍失败。"""
    if not THINKING_RESCUE:
        return "", why
    try:
        c = rescue_client()
    except Exception as exc:                                   # noqa: BLE001
        return "", "%s（抢救 client 不可用：%s）" % (why, str(exc)[:60])
    try:
        with _PRINT_LOCK:
            print("    ⟳ %s 常规重试全不合格（%s）→ 改用「开思考」抢救一次"
                  % (tk._leaf(feat), why[:50]))
        card = c.chat(SYSTEM_PROMPT, [{"role": "user", "content": prompt}],
                      max_tokens=RESCUE_MAX_TOKENS, temperature=0.2)
    except Exception as exc:                                   # noqa: BLE001
        return "", "%s（抢救调用失败：%s）" % (why, str(exc)[:60])
    ok, w = valid_card(card)
    return (card, "") if ok else ("", "%s（开思考后仍不合格）" % w)

#: 训练期间在知识库根下放一把锁（内容 = 本进程 pid）。
#: 为什么必须要有：`tools/rebuild_all_knowledge.py` 重建时会把
#: `knowledge/<vendor>` **整个改名**，训练此刻写进去的卡片会被卷进
#: `.bak_<stamp>/` 里再也不回来。重建脚本读到这把锁就会让路（见该脚本
#: 的 `wait_for_training()`）。锁在正常退出/异常退出时都会清掉。
TRAIN_LOCK_NAME = ".ai_train.lock"

#: 「已确认死亡的孤儿锁」要等多久才接管。锁**只在获取时写一次、之后从不刷新**，
#: 所以不能拿锁龄当"还活着"的替身；但为了防"刚写锁、pid 查询恰好抖动"，
#: 留一个很小的宽限窗口（原来这个值是 1800 秒，会让强杀后的下一轮白等半小时）。
ORPHAN_GRACE = 120.0


def _pid_state(pid: int) -> str:
    """进程状态：`"alive"` / `"dead"` / `"unknown"`（拿不准）。

    `_pid_alive` 的底层实现，额外把**"拿不准"**暴露出来 —— 锁的接管逻辑需要它：
    只有**已确认死亡**才敢立刻接管；拿不准时必须按老规矩排队（见 `_acquire_train_lock`）。
    """
    if pid <= 0:
        return "dead"
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return "alive"
        except PermissionError:
            return "alive"
        except OSError:
            return "dead"
    try:
        import ctypes                                   # noqa: PLC0415
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)       # SYNCHRONIZE
        if not h:
            return "dead"
        try:
            #: 0 = WAIT_OBJECT_0 = 已退出；258 = WAIT_TIMEOUT = 还在跑
            return "alive" if k.WaitForSingleObject(h, 0) != 0 else "dead"
        finally:
            k.CloseHandle(h)
    except Exception:                                    # noqa: BLE001
        return "unknown"


def _pid_alive(pid: int) -> bool:
    """进程还在吗？（纯标准库，Windows 用 ctypes 查内核句柄）

    ⚠ **不要用 `os.kill(pid, 0)` 当存在性检查**：本机实测对一个**活着**的
    训练进程（pid 29684，psutil 与任务管理器都看得到）直接抛
    `OSError: [WinError 87] 参数错误`。它只对"自己的直接子进程"侥幸成立，
    对别的进程完全不可信 —— 拿它判活会把活着的训练误判成死了，
    进而误清锁、让重建把正在写的卡片卷走。

    改用 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(h, 0)`：
    开不到句柄 = 进程没了；句柄已 signal = 进程已退出。准确且无需第三方库
    （训练脚本跑在**没有 psutil** 的托管 Python 下，所以不能依赖 psutil）。

    语义：**"拿不准"算活着**（安全方向）。实现已收敛到 `_pid_state`，
    行为与改造前逐条一致（`pid<=0`→False、非 Windows 的 OSError→False、
    异常→True）。
    """
    return _pid_state(pid) != "dead"


def _read_lock(path: str) -> tuple[int, float, str]:
    """读锁 → `(pid, 年龄秒, 厂商)`。读不出来给 `(0, 1e9, "")`。

    ⚠ 时间戳必须**不带空格**（写成 `2026-10-06T20:03:25`）。踩过的坑：原先写
    `strftime("%Y-%m-%d %H:%M:%S")`，按空格切分后 `parts[2]` 是 `21:42:16`
    而不是厂商名 —— 厂商感知直接失效，重建又变回"傻等"。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            parts = fh.read().split()
    except OSError:
        return 0, 1e9, ""
    pid = 0
    if parts:
        try:
            pid = int(parts[0])
        except ValueError:
            pid = 0
    vendor = _pick_vendor(parts)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = 1e9
    return pid, age, vendor


def _pick_vendor(parts: list[str]) -> str:
    """从锁字段里认出厂商名（认不出给空串）。

    只认白名单里的名字，不盲目取"第 3 个字段"—— 万一有人把时间戳写成带空格的
    形式，`parts[2]` 会是 `21:42:16`，那是个假厂商，会把冲突判断带偏。
    """
    known = set(VENDORS) | {"all"}
    for tok in parts[1:]:
        if tok in known:
            return tok
    return ""


def _acquire_train_lock(vendor: str, wait_min: float = 180.0) -> str:
    """写下 `pid 时间 厂商` 三字段的训练锁；拿不到就返回空串。

    ⚠ **锁里必须带厂商。** 卡片写在 `knowledge/<vendor>/tech/ai/`，各厂商互不相干，
    但 `tk.learn()` 是"读索引→改→写回"，**跨进程不安全** —— 两个进程同时整理
    同一个厂商，索引条目会被后写的盖掉（表现为"训练了却搜不到"）。
    所以同厂商必须排队，不同厂商可以并行。

    三种情形：
      * 锁属于**别的**厂商 → 不接管，也不删，直接跑（各写各的目录，安全）。
      * 锁属于**本**厂商且持锁进程活着（或状态拿不准）→ **排队等**，最多 `wait_min`
        分钟；等不到就放行（宁可多跑一遍，也不能永远卡住）。
      * 锁是**孤儿**（`_pid_state` 明确判定持锁 pid 已死，且锁龄 ≥ `ORPHAN_GRACE`）
        → 接管。进程被强杀时 `finally` 不执行，锁会永远留着；实测本机就有一把
        20:03 的孤儿锁，不接管的话所有后续训练都会以为自己"已经有锁"而放弃写锁。
      * 其余（锁不存在）→ 接管。

    还有一条铁律：**没拿到锁就绝不删锁**。踩过的坑：主训练在跑时随手
    `--limit 1` 试一下，试跑进程退出时把锁删了，于是 rebuild 立刻认为
    "没人在训练"就能趁虚而入 —— 锁形同虚设。
    """
    path = os.path.join(tk.knowledge_dir(), TRAIN_LOCK_NAME)
    deadline = time.time() + wait_min * 60
    announced = False
    while os.path.exists(path):
        old_pid, age, old_vendor = _read_lock(path)
        if old_vendor and old_vendor != vendor:
            print("  ℹ 训练锁属于 %s（本次只训练 %s，各写各的目录）→ 不接管、直接跑"
                  % (old_vendor, vendor))
            return ""
        #: ⚠ 锁**只在获取时写一次、之后从不刷新** —— 所以 `age` 完全不反映持锁者
        #: 是否活着。原判据 `_pid_alive(pid) or age < 1800` 的后果是：训练被强杀
        #: （`finally` 不跑、锁留下）之后，**下一轮必须干等 30 分钟**才肯接管
        #: —— 实测 2026-10-07 17:17 白等过，正好踩在"无人值守不能有干等的分支"上。
        #: 既然 `_pid_state` 能明确区分「已确认死亡」与「拿不准」，就别再拿 age
        #: 当替身：dead → 宽限 ORPHAN_GRACE 秒后接管；活/拿不准 → 照旧排队（安全方向）。
        st = _pid_state(old_pid)
        if st == "dead" and age >= ORPHAN_GRACE:
            print("  ℹ 训练锁是孤儿（pid %s 已确认不在，锁 %.1f 分钟没更新）→ 接管"
                  % (old_pid or "?", age / 60.0))
            break
        if time.time() > deadline:
            print("  ⚠ 等 %s 的训练锁超过 %.0f 分钟 → 放行（索引可能被并发写）"
                  % (vendor, wait_min))
            return ""
        if not announced:
            print("  ⏳ %s 已有训练在跑（pid %s，状态 %s，%.0f 秒前更新）→ 排队等它"
                  % (vendor, old_pid or "?", st, age))
            announced = True
        time.sleep(30)
        continue
    if announced:
        print("  ✓ 排到了 %s 的训练锁" % vendor)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            #: 时间戳用 ISO 的 `T` 分隔，**不带空格** —— 否则按空格切分时厂商
            #: 字段会错位（见 `_read_lock` 的说明）。
            fh.write("%d %s %s\n" % (os.getpid(),
                                     time.strftime("%Y-%m-%dT%H:%M:%S"), vendor))
        return path
    except OSError as exc:
        print("  ⚠ 训练锁写入失败（%s）—— 重建可能卷走本次卡片" % exc)
        return ""


def _release_train_lock(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass

#: 卡片必须出现的标题。缺了说明模型没按格式来，直接判失败（不写盘）。
REQUIRED_HEADINGS = ("## 一句话定义",)

SYSTEM_PROMPT = """你是网络设备厂商技术资料的编辑。你的唯一职责是：把**给定的厂商文档原文**\
整理成一张结构化技术卡片。

硬性要求：
1. 只能依据我提供的原文。原文没写的内容，一律写「原文未涉及」，**不得**用你自己的知识补充。
2. 不得引入原文以外的命令、参数、默认值、数字、版本号。
3. 不要复述原文的目录、页眉页脚、来源标注、页码。
4. 用简体中文；技术缩写保留原文写法（IRF / ACL / OSPF / VLAN 等不翻译）。
5. 只输出要求的结构，不要开场白、不要结束语、不要用代码块包裹整篇。"""

USER_TEMPLATE = """厂商：{vendor}
技术分类：{feature}
该分类下的命令：{cmds}

=== 厂商文档原文（唯一依据，共 {n_chars} 字{truncated_note}）===
{prose}
=== 原文结束 ===

请严格按下面的结构输出 Markdown：

## 一句话定义
（30 字以内，说明这项技术是什么）

## 适用场景
（2-4 条，什么情况下会用到）

## 关键点
（3-6 条，配置或原理上必须知道的）

## 注意事项
（原文提到的限制、默认值、互斥关系、风险；原文没提就写「原文未涉及」）

## 关联命令
（从上面的命令列表与原文里挑，每条写成 `命令` —— 一句话作用）"""


# ---------------------------------------------------------------- 原文读取
def read_feature_prose(vendor: str, feature: str) -> dict:
    """读某个 feature 的**厂商原文正文**（不含命令清单、不含我们写的文件头）。

    返回 {"ok", "prose", "commands", "reason"}。ok=False 时调用方必须跳过，
    **不允许**在这种情况下调模型。
    """
    out = {"ok": False, "prose": "", "commands": [], "reason": ""}
    idx = tk._index(vendor)
    node = (idx.get("features") or {}).get(feature) or {}
    rel = str(node.get("file") or "")
    if not rel:
        out["reason"] = "索引里没有这个 feature"
        return out
    path = os.path.join(tk.knowledge_dir(), vendor, rel)
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        out["reason"] = "读不到 %s（%s）" % (rel, exc)
        return out

    # 去掉文件头（到第一个 `---` 为止）
    body = re.sub(r"\A#[^\n]*\n(?:>[^\n]*\n|\s*\n|- [^\n]*\n)*\s*---\s*\n", "", text, count=1)
    # 去掉末尾"本分类下的命令"清单（命令另走 commands 字段，不占原文配额）
    body = re.split(r"\n---\n+## 本分类下的命令", body)[0]

    body = re.sub(r"<!--\s*source:.*?-->", "", body)
    # ⚠ `### 小节标题` **要保留** —— 它是分节的唯一依据，select_prose() 靠它切片。
    #    早先把它一并删了，导致只能用"取开头"这种蠢办法（见 select_prose 的说明）。
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    if not body or "没有提取到讲解正文" in body:
        out["reason"] = "该 feature 没有提取到厂商原文正文（**不允许让 AI 编**）"
        return out
    out.update(ok=True, prose=body, commands=list(node.get("commands") or []))
    return out


def _split_sections(prose: str) -> list[tuple[str, str]]:
    """把正文按 `### 小节标题` 切成 [(标题, 正文), ...]；没有小节标题时整体作一节。"""
    parts = re.split(r"(?m)^###\s+(.*)$", prose)
    if len(parts) < 3:
        return [("", prose)]
    out = [("", parts[0])] if parts[0].strip() else []
    for i in range(1, len(parts) - 1, 2):
        out.append((parts[i].strip(), parts[i + 1].strip()))
    return out


def select_prose(prose: str, commands: list, max_chars: int = MAX_INPUT_CHARS) -> tuple[str, str]:
    """从整篇正文里挑出**最能支撑摘要**的部分，返回 (选中的正文, 说明)。

    为什么不能简单取开头（**实测踩过的坑**）：`build_tech_docs.py` 是按 source 逐页
    拼接的，一个 feature 的正文可能是几十页。华为 `系统管理` 有 19 万字，而开头 1.4 万字
    恰好全是 NTP/LLDP/RADIUS 的**配置举例**，该分类真正的 `display device` 系列命令在后面
    —— 只取开头会让模型得出"原文未涉及"的**错误结论**（第一版实测就是这个结果，
    卡片里 10 条关联命令全写"原文未涉及"）。

    选法（纯代码、可解释、无依赖）：
      ① **必留第一节**（通常是"概述/配置任务"，最支撑"一句话定义"）；
      ② 其余按"这一节提到了本分类多少条命令"打分，分高的先入选；
      ③ 按预算装，装不下为止。
    说明字符串会写进提示词，让模型知道这是**选摘**而不是全文。
    """
    if len(prose) <= max_chars:
        return prose, ""

    secs = _split_sections(prose)
    # 探针去重：**去掉是别人前缀的短命令**。
    # 为什么（实测）：`display device` 是 `display device all`/`display device board` 的
    # 子串，一条 `display device` 命中会一次性给这一节加 8 分（8 条探针全中），
    # 把评分彻底带偏 —— 于是选出来的是"恰好提过 display device 一句"的 NTP 举例章节，
    # 而不是真正讲设备管理的那一节。只留最长的那批，一节最多为每条命令计一次。
    raw = [c.lower() for c in (commands or []) if len(c) >= 5]
    probes = [c for c in raw if not any(o != c and o.startswith(c) for o in raw)]
    scored = []
    for i, (title, text) in enumerate(secs):
        low = text.lower()
        hit = sum(1 for c in probes if c in low) if probes else 0
        scored.append((i, title, text, hit))

    # ① 第一节必留
    picked = [scored[0]] if scored else []
    used = len(scored[0][2]) if scored else 0
    # ② 其余按命中命令数降序
    for item in sorted(scored[1:], key=lambda x: (-x[3], x[0])):
        if item[3] <= 0:
            break
        if used + len(item[2]) > max_chars:
            continue
        picked.append(item)
        used += len(item[2])
        if used >= max_chars * 0.95:
            break

    if len(picked) <= 1:
        # 一节都没打上分（命令名在正文里的写法与索引不完全一致等）⇒ 退回均匀抽样，
        # 至少让模型看到全文各处的样子，而不是只看开头。
        step = max(1, len(secs) // 8)
        picked = scored[::step]
        note = "按篇幅均匀抽样（命令名未在正文中直接命中）"
    else:
        note = "按相关度选摘（优先取提到本分类命令的小节）"

    picked.sort(key=lambda x: x[0])                 # 还原文档顺序，读起来才连贯
    body = "\n\n".join("### %s\n%s" % (t, x) if t else x for _, t, x, _ in picked)
    if len(body) > max_chars:
        body = body[:max_chars]
    return body, "%s，共 %d/%d 节" % (note, len(picked), len(secs))


# ---------------------------------------------------------------- 提示词
def build_prompt(vendor: str, feature: str, prose: str, commands: list) -> str:
    body, how = select_prose(prose, commands, MAX_INPUT_CHARS)
    cmds = "、".join("`%s`" % c for c in commands[:40]) or "（原文未单列）"
    if len(commands) > 40:
        cmds += " 等 %d 条" % len(commands)
    return USER_TEMPLATE.format(
        vendor=vendor, feature=feature, cmds=cmds,
        n_chars=len(body),
        truncated_note=("，%s；**不是全文**，涉及全文的问题请写「原文未涉及」" % how) if how else "",
        prose=body)


def valid_card(text: str) -> tuple[bool, str]:
    """产物校验：格式不对就不写盘（宁缺勿滥，别把半成品沉淀成"资料"）。"""
    t = (text or "").strip()
    if len(t) < 80:
        return False, "返回过短（%d 字）" % len(t)
    for h in REQUIRED_HEADINGS:
        if h not in t:
            return False, "缺少标题 %s" % h
    if t.startswith("```"):
        t = re.sub(r"\A```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```\s*\Z", "", t)
    return True, ""


# ---------------------------------------------------------------- 主流程
def existing_entries(vendor: str) -> dict:
    """AI 层的索引。注意 `aliases` 会让同一个卡片有多个键 —— 统计时要按 `file` 去重。"""
    path = os.path.join(tk.tech_dir(vendor), tk.AI_SUBDIR, "index.json")
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def n_cards(vendor: str) -> int:
    """已整理的**卡片数**（按文件去重，不是索引键数）。"""
    return len({str(v.get("file") or "") for v in existing_entries(vendor).values()} - {""})


def client_or_die(provider: str = ""):
    from config.settings import Settings
    from core.llm import LLMClient, provider_defaults, build_client
    st = Settings()
    cli = build_client(st, provider=provider or None)
    if not cli.api_key:
        # 配置文件里没密钥时，兜一层环境变量（CI / 手动跑的场景）。
        # ⚠ 必须**按 provider 取对应的变量名**：拿 DEEPSEEK_API_KEY 去调 LongCat
        # 只会得到 401，而报错看起来像"key 写错了"，很难往"取错变量"上想。
        name = (provider or st.get("ai_provider") or "deepseek").lower()
        env_names = {
            "deepseek": ["DEEPSEEK_API_KEY"],
            "openai_compat": ["OPENAI_API_KEY"],
            "longcat": ["LONGCAT_API_KEY", "LONGCAT_KEY"],
            "anthropic": ["ANTHROPIC_API_KEY"],
        }.get(name) or ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LONGCAT_API_KEY"]
        env = ""
        for k in env_names:
            if os.environ.get(k):
                env = os.environ[k]
                break
        if env:
            cli = LLMClient(provider=name, api_key=env,
                            model=cli.model or provider_defaults(name)["model"],
                            base_url=cli.base_url or provider_defaults(name)["base_url"])
    if not cli.configured:
        raise SystemExit("✗ 没找到可用的 API Key（配置 ~/.aiterminal/settings.json，"
                         "或设 DEEPSEEK_API_KEY / LONGCAT_API_KEY / OPENAI_API_KEY）")
    return cli


def apply_thinking(cli, mode: str = "off") -> None:
    """推理模型（LongCat-2.x）默认"先想再答"，思考 token 与正文**共享** max_tokens。

    本任务是"照着给定原文填固定模板"，不需要思考：关掉它既省 token 又避免
    思考把预算吃光导致正文为空。只对 longcat 生效 —— 别的家不认 `thinking`
    字段，塞进去会 400。
    """
    if cli is None or cli.provider != "longcat":
        return
    on = (mode == "on")
    cli.extra_body = {"thinking": {"type": "enabled" if on else "disabled"}}
    print("  思考模式 = %s（LongCat）" % ("开启" if on else "关闭"))


def run_vendor(vendor: str, cli, limit: int = 0, only: str = "", force: bool = False,
               dry_run: bool = False, sleep: float = SLEEP_BETWEEN, top: int = 0,
               workers: int = 1) -> dict:
    idx = tk._index(vendor)
    feats = sorted((idx.get("features") or {}).keys())
    if not feats:
        print("  ✗ %s 没有技术资料层，先跑 tools/build_tech_docs.py" % vendor)
        return {"vendor": vendor, "error": "no-tech-layer"}

    done = existing_entries(vendor)
    todo = []
    for f in feats:
        leaf = tk._leaf(f)
        if only and only.lower() not in (leaf, f.lower()):
            continue
        if not force and (f.lower() in done or leaf in done):
            continue
        todo.append(f)

    if top:
        # 按**命令条数**降序取前 N 个。为什么要这个：全量 569 个 feature，
        # 每个都要一次 API 调用，先做"最常用"的性价比最高 —— 命令多说明这个分类
        # 用户碰到的概率大。这是纯代码可算的重要性排序，不需要人来挑。
        def n_cmds(f):
            return len(((idx.get("features") or {}).get(f) or {}).get("commands") or [])
        todo.sort(key=lambda f: (-n_cmds(f), f))
        todo = todo[:top]
    if limit:
        todo = todo[:limit]

    print("  %s：feature %d 个，已有 AI 条目 %d 个，本次处理 %d 个"
          % (vendor, len(feats), n_cards(vendor), len(todo)))
    if not todo:
        return {"vendor": vendor, "n_features": len(feats), "n_done": n_cards(vendor),
                "processed": 0, "written": 0, "skipped": 0, "failed": 0}

    stats = {"vendor": vendor, "n_features": len(feats), "n_done": len(done),
             "processed": 0, "written": 0, "skipped": 0, "failed": 0, "details": []}

    def one(feat: str) -> dict:
        """处理单个 feature。网络调用在锁外；写盘串行。"""
        src = read_feature_prose(vendor, feat)
        if not src["ok"]:
            return {"feature": feat, "status": "skip", "reason": src["reason"]}

        prompt = build_prompt(vendor, feat, src["prose"], src["commands"])
        if dry_run:
            return {"feature": feat, "status": "dry", "prompt": prompt,
                    "prompt_chars": len(prompt), "prose_chars": len(src["prose"])}

        t0 = time.time()
        last_err = ""
        card = ""
        why = ""
        for attempt in range(1, MAX_RETRY + 1):
            try:
                card = cli.chat(SYSTEM_PROMPT, [{"role": "user", "content": prompt}],
                                max_tokens=MAX_OUTPUT_TOKENS, temperature=0.2)
            except Exception as exc:                   # LLMError 或任何网络异常
                last_err = str(exc)
                #: 只有网络/限流类失败才值得重试；其它（如鉴权失败）重试也是白费。
                if attempt >= MAX_RETRY or not any(h in last_err.lower()
                                                   for h in RETRYABLE_HINTS):
                    return {"feature": feat, "status": "fail",
                            "reason": "%s（第 %d 次）" % (last_err, attempt)}
                #: 指数退避：2s → 4s → 8s，封顶 20s。限流时越等越久。
                back = min(20.0, 2.0 ** attempt)
                with _PRINT_LOCK:
                    print("    ↻ %s 第 %d 次调用失败（%s），%.0fs 后重试"
                          % (tk._leaf(feat), attempt, last_err[:80], back))
                time.sleep(back)
                continue

            #: 调用成功，但产物不合格 —— 最常见的是"返回过短"（模型偶尔只吐个残句）。
            #: 这同样是**瞬时**故障，重试一次多半就正常了；不重试这张卡就白丢，
            #: 事后还得专门再补跑一轮。
            ok, why = valid_card(card)
            if ok:
                break
            if attempt >= MAX_RETRY:
                #: 常规重试全不合格 → 最后一次「开思考」抢救（见 THINKING_RESCUE 注释）。
                #: 不这么做的话，术语表/附录这类"原文形态不标准"的 feature
                #: 会**每轮补扫都失败一次**，永远进不了知识库。
                card, why = rescue_call(prompt, feat, why)
                if card:
                    ok = True
                    break
                return {"feature": feat, "status": "bad", "reason": why}
            back = min(20.0, 2.0 ** attempt)
            with _PRINT_LOCK:
                print("    ↻ %s 第 %d 次产物不合格（%s），%.0fs 后重试"
                      % (tk._leaf(feat), attempt, why[:60], back))
            time.sleep(back)
        else:
            return {"feature": feat, "status": "fail",
                    "reason": last_err or "重试次数耗尽"}

        ok, why = valid_card(card)
        if not ok:
            return {"feature": feat, "status": "bad", "reason": why}

        leaf = tk._leaf(feat)
        with _WRITE_LOCK:
            res = tk.learn(vendor, leaf, card.strip(),
                           question="整理 %s 的技术要点" % feat,
                           model=cli.model, source="ai-summary",
                           aliases=[feat] if feat.lower() != leaf else [])
        if not res["ok"]:
            return {"feature": feat, "status": "write-fail", "reason": res["reason"]}
        return {"feature": feat, "status": "ok", "file": res["file"],
                "secs": round(time.time() - t0, 1)}

    def emit(i: int, d: dict) -> None:
        """按原格式打印（并发下加锁，避免行交错）。"""
        st, feat = d["status"], d["feature"]
        with _PRINT_LOCK:
            if st == "skip":
                print("    [%3d/%3d] 跳过 %s —— %s" % (i, len(todo), feat, d["reason"]))
            elif st == "dry":
                print("    [%3d/%3d] （dry-run）%s —— 原文 %d 字 → 提示词 %d 字"
                      % (i, len(todo), feat, d["prose_chars"], d["prompt_chars"]))
                if i == 1:
                    print("\n" + "-" * 70 + "\n" + d["prompt"][:1600]
                          + "\n" + "-" * 70 + "\n")
            elif st == "ok":
                print("    [%3d/%3d] ✓ %s → %s（%.1fs）"
                      % (i, len(todo), feat, d["file"], d["secs"]))
            elif st == "bad":
                print("    [%3d/%3d] ✗ %s —— 产物不合格：%s" % (i, len(todo), feat, d["reason"]))
            elif st == "write-fail":
                print("    [%3d/%3d] ✗ %s —— 写盘失败：%s" % (i, len(todo), feat, d["reason"]))
            else:
                print("    [%3d/%3d] ✗ %s —— %s" % (i, len(todo), feat, d["reason"]))

    def tally(d: dict) -> None:
        st = d["status"]
        stats["details"].append(d)
        if st == "skip":
            stats["skipped"] += 1
        elif st == "ok":
            stats["written"] += 1
            stats["processed"] += 1
        elif st != "dry":
            stats["failed"] += 1

    if workers > 1 and not dry_run:
        print("    并发 %d（API 调用并行；索引写盘串行）" % workers)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, f): f for f in todo}
            for i, fut in enumerate(futs, 1):
                #: 结果按**完成顺序**取 —— 谁先回来先打印谁，进度更实时。
                d = fut.result()
                emit(i, d)
                tally(d)
    else:
        for i, feat in enumerate(todo, 1):
            d = one(feat)
            emit(i, d)
            tally(d)
            if d["status"] == "fail":
                time.sleep(max(2.0, sleep * 2))        # 失败后多等一会（多半是限流）
            elif i < len(todo):
                time.sleep(sleep)

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="让 AI 基于厂商原文生成结构化技术卡片")
    ap.add_argument("--vendor", default="h3c", choices=list(VENDORS) + ["all"])
    ap.add_argument("--limit", type=int, default=0, help="每个厂商最多处理 N 个 feature")
    ap.add_argument("--top", type=int, default=0,
                    help="按命令条数优先处理前 N 个 feature（控制 API 花费用）")
    ap.add_argument("--feature", default="", help="只处理这个 feature（叶子名或全名）")
    ap.add_argument("--force", action="store_true", help="已有 AI 条目的也重做")
    ap.add_argument("--dry-run", action="store_true", help="只拼提示词，不调模型")
    ap.add_argument("--stats", action="store_true", help="只看统计，不调模型")
    ap.add_argument("--provider", default="", help="覆盖配置里的提供方")
    ap.add_argument("--thinking", choices=["on", "off"], default="off",
                    help="推理模型的思考开关（仅 LongCat 生效）。默认 off")
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN, help="请求间隔秒数")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发数（>1 时 API 调用并行、索引写盘串行）。默认 1")
    a = ap.parse_args()

    vendors = list(VENDORS) if a.vendor == "all" else [a.vendor]

    if a.stats:
        print("知识库目录 =", tk.knowledge_dir())
        for v in vendors:
            idx = tk._index(v)
            done = existing_entries(v)
            print("  %-7s feature %4d，厂商原文已提取 %4d，AI 已整理 %4d"
                  % (v, len(idx.get("features") or {}),
                     idx.get("n_features", 0), n_cards(v)))
        return 0

    cli = None
    if not a.dry_run:
        cli = client_or_die(a.provider)
        apply_thinking(cli, a.thinking)
        print("模型 =", cli.describe())

    grand = {"written": 0, "skipped": 0, "failed": 0}
    #: 锁**按厂商**逐轮拿、逐轮放（锁里带厂商名）。为什么不是整个循环持一把：
    #: `--vendor all` 要跑 4 个多小时，期间 rebuild 想重建"别的"厂商完全是安全的
    #: （各写各的 `knowledge/<vendor>`），却会被一把全局锁挡在门外。逐厂商持锁后，
    #: 只有"重建的正好是正在训练的那个厂商"才会让路。
    for v in vendors:
        lock_path = _acquire_train_lock(v) if not a.dry_run else ""
        try:
            print("\n=== %s ===" % v)
            r = run_vendor(v, cli, limit=a.limit, only=a.feature, force=a.force,
                           dry_run=a.dry_run, sleep=a.sleep, top=a.top,
                           workers=a.workers)
        finally:
            _release_train_lock(lock_path)
        if r.get("error"):
            continue
        for k in grand:
            grand[k] += r.get(k, 0)

    if not a.dry_run:
        print("\n汇总：写入 %d，跳过 %d，失败 %d" % (grand["written"], grand["skipped"],
                                                    grand["failed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

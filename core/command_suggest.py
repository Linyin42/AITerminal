"""命令自动补全 —— 为终端提供三层补全能力。

数据源：``_vendor_cmd/{vendor}/commands.jsonl``，每行一个 JSON 对象：

    {"kind": "command", "command": "display vlan", "syntax": "display vlan [ ... ]",
     "view": "任意视图", "feature": "二层技术-以太网交换/VLAN"}
    {"kind": "example", "title": "1.13.4 IRF典型配置举例", "steps": ["system-view", ...]}

三层补全：

* ① ``token``     —— 正在输入的这个词该补成什么（前缀匹配命令名；带 view/feature）
* ② ``next``      —— 这个词敲完之后，下一个可选词是什么（从 syntax 里取**字面量**）
* ③ ``sequences`` —— 用户已敲的整行命中了哪个配置举例，返回剩余步骤（参数无关匹配）

六条由实测约束决定的写法：

1. **懒加载 + 进程内缓存**：华为 JSONL 21 MB / 5.7 万行，每次查询重读会让界面卡死。
   缓存按 ``(vendor, data_dir)`` 分键 —— 换了数据目录不能复用旧索引。
2. **只加载当前厂商**：三家全读进内存要几十 MB，而一次补全只可能属于一家。
3. **补全在按键路径上**：加载后单次查询必须毫秒级。序列匹配靠
   ``(步序, 命令名键)`` 倒排索引定位候选，绝不全表扫描。
4. **序列匹配的粒度是"命令名"而不是"首词"**（实测缺陷）：只比首词时
   ``display vlan`` 会命中 ``display ip routing-table`` 这类完全无关的例子。
   现在的键取"去掉尾部参数后的命令名"的前 1~2 个词（见 :meth:`_VendorIndex.line_key`），
   参数值仍然无关（``vlan 20`` 命中例子里的 ``vlan 10``），但命令名不同就不再误命中。
5. **半截输入不推序列**：最后一行没敲回车且不是一整条合法命令时（例如
   ``display ``），它不参与序列匹配 —— 否则半截输入会被当成一条命令去比对例子。
4. **字面量 / 占位符必须分得清**：``syntax`` 里 ``vlan-id1``、``<ipv4-address>``
   是占位符，绝不能出现在 next 候选里。判定用**命令名词表**：真实命令词表里
   有的（link-type / mac-address / brief）就是字面量，没有的且形如 ``xxx-id`` /
   含数字 / 全大写 / 单字符的，一律当占位符过滤掉。
"""

from __future__ import annotations

import json
import os
import re
import threading
from array import array
from collections import Counter
from heapq import nlargest

from core import app_paths

#: 生成侧（tools/doc_to_commands.py、tools/build_tech_docs.py 等）沿用的历史约定位置。
_LEGACY_DIR = os.path.join(os.path.expanduser("~"), "deepseek_harness", "_vendor_cmd")

#: 默认数据目录。**不依赖 TEMP / 当前工作目录**：引擎可能被以任意 cwd 拉起。
#: 三级解析（见 core/app_paths.data_dir）：① 环境变量 `AIT_VENDOR_CMD_DIR`
#: ② `<exe 同目录>/_vendor_cmd`（交付包）③ 历史约定 `~/deepseek_harness/_vendor_cmd`。
#: ⚠ 2026-10-17 修正：这里曾经是写死的绝对路径 `C:\Users\linyin\deepseek_harness\_vendor_cmd`，
#: 交付机上该路径不存在 ⇒ 命令补全整块失效（数据既没进包、路径也指不到别处）。
DEFAULT_DIR = app_paths.data_dir("_vendor_cmd", "AIT_VENDOR_CMD_DIR",
                                 legacy=[_LEGACY_DIR])

VENDORS = ("h3c", "huawei", "ruijie")

# 命令名里出现这些字符 = 数据损坏。华为实测有 10 条，形如 "undo ["、
# "ipv6 address <dhcpv6-prefix>"、"hrp auto-check <module-name> enable"。
_BAD_NAME = re.compile(r"[<>\[\]{}|]")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 提示符前缀：<HUAWEI> / [H3C] / [H3C-vlan10] / DeviceA(config)# / SW1#
_PROMPT = re.compile(
    r"^\s*(?:\[[^\]\n]{0,64}\]|<[^>\n]{0,64}>|"
    r"[A-Za-z0-9_.,:\-]{1,40}(?:\([^)\n]{0,40}\))?\s*[#>])\s*")

_SPACE = re.compile(r"\s+")

# syntax 词法：&<1-32> / <占位符> / 括号 / 竖线 / 普通词
_LEX = re.compile(r"&<\d+(?:-\d+)?>|<[^>\s]*>|[\[\]{}()|]|\S+")

_OPEN = {"[": "]", "{": "}", "(": ")"}

# 占位符后缀：vpn-instance-name / slot-id / vlan-id1 / interface-type ...
_PH_SUFFIX = re.compile(
    r"-(?:id|idx|index|num|number|name|type|addr|address|string|str|value|val|len|length|"
    r"level|count|cnt|mask|slot|profile|policy|key|file|rule|list|text|time|times|interval|"
    r"port|ip|mac|vlan|vrf|vpn|wildcard|hex|octet|size|seq|sec|seconds|minutes|days)\d*$")

# 光秃秃的占位名词（不带连字符、不含数字，最容易漏网）
_PH_BARE = frozenset((
    "string", "text", "char", "word", "line", "hex", "octet", "integer", "number",
    "value", "name", "id", "addr", "address", "wildcard", "mask", "seconds",
    "minutes", "hours", "days", "x", "y", "z", "n",
))

# 强占位符词形。**只在 syntax 来源里拦**：华为确实有命令叫 `vlan-id`
# （安全策略视图）和 `if-match vlan-id`，所以词表里查得到它 —— 但它在
# `display vlan [ vlan-id ]` 这类语法里就是占位符，绝不能当补全候选。
# 命令名自身的词（name_hits）不受这条约束，`router-id` 这类真字面量照旧保留。
_PH_STRONG = re.compile(r"-(?:id|idx|index|num|number)\d*$")

#: 单个厂商最多缓存的 syntax 展开结果条数（防止无限增长）
_EXPAND_CACHE_MAX = 20000
#: 单条 syntax 最多展开出的词序列条数
_EXPAND_CAP = 48
#: next 层最多展开多少条命令的 syntax（命令名本身已能覆盖大部分情况）
_EXPAND_COMMANDS_MAX = 200
#: 一条序列最多回传多少步。有的举例有 700+ 步（华为 VXLAN 案例），
#: 整包回传单次响应就 180 KB —— 补全在按键路径上，终端一次也只展示一屏。
#: 所以只回前 N 步，真实剩余步数放在 "remaining" 里。
_MAX_SEQ_STEPS = 40

_CACHE = {}
_CACHE_LOCK = threading.Lock()


# ------------------------------------------------------------------ 小工具
def infer_vendor(model: str) -> str:
    """按型号猜厂商。猜不出返回空串（调用方给空结果，不报错）。"""
    text = (model or "").strip().upper()
    if not text:
        return ""
    if "CR16000" in text or "S12500" in text:
        return "h3c"
    if "RG-" in text:
        return "ruijie"
    for key in ("CE", "USG", "AR", "NE"):
        if key in text:
            return "huawei"
    return ""


def normalize_line(line: str) -> str:
    """归一化一行：去提示符前缀 → 压空白 → 转小写。

    用户从终端复制的配置必然带 ``<HUAWEI>`` / ``[SW1]`` 这类前缀，
    不归一化就一条例子都匹配不上。
    """
    if not line:
        return ""
    text = str(line).replace("\r", " ").replace("\n", " ")
    text = _PROMPT.sub("", text)
    return _SPACE.sub(" ", text).strip().lower()


def is_placeholder(tok: str, known=frozenset()) -> bool:
    """是不是占位符（不能进 next 候选）。

    ``known`` 是当前厂商的**命令词表**（所有命令名的分词）。
    词表里有的就是真字面量（``link-type`` / ``mac-address`` / ``brief``），
    没有的再看形状：``<...>``、中文、``xxx-id`` 后缀、含数字、全大写、单字符。
    """
    if not tok:
        return True
    if _BAD_NAME.search(tok) or _CJK.search(tok):
        return True
    low = tok.lower()
    if low in known:
        return False
    if _PH_SUFFIX.search(low):
        return True
    if any(ch.isdigit() for ch in low):
        return True
    if low in _PH_BARE:
        return True
    if tok.isupper() or len(tok) == 1:
        return True
    return False


def first_token(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    return line.split(" ", 1)[0]


# ------------------------------------------------------------------ syntax 解析
def _parse_alts(toks, i, close):
    """解析竖线分隔的备选，直到 close。返回 ``([[item...], ...], next_i)``。

    item = ("w", word) | ("grp", [[item...], ...], optional)
    """
    alts, cur = [], []
    while i < len(toks):
        tok = toks[i]
        if tok == close:
            i += 1
            break
        if tok == "|":
            alts.append(cur)
            cur = []
            i += 1
            continue
        if tok in _OPEN:
            sub, i = _parse_alts(toks, i + 1, _OPEN[tok])
            # 方括号 = 可选，花括号/圆括号 = 必选
            cur.append(("grp", sub, tok == "["))
            continue
        if tok in "]})":
            i += 1                      # 多余的闭合，跳过：脏数据不该炸掉整条语法
            continue
        cur.append(("w", tok))
        i += 1
    alts.append(cur)
    return alts, i


def _expand_items(items, cap):
    """把 item 列表展开成所有可能的词序列（叉积），上限 cap 条。"""
    seqs = [[]]
    for item in items:
        if item[0] == "w":
            for seq in seqs:
                seq.append(item[1])
            continue
        _, alts, optional = item
        branches = [[]] if optional else []
        for alt in alts:
            branches.extend(_expand_items(alt, cap))
            if len(branches) >= cap:
                break
        new = []
        for seq in seqs:
            for branch in branches:
                new.append(seq + branch)
                if len(new) >= cap:
                    break
            if len(new) >= cap:
                break
        seqs = new
        if not seqs:
            break
    return seqs


# ------------------------------------------------------------------ 索引
class _VendorIndex:
    """单个厂商的只读索引。加载一次，之后只读（多线程共用）。"""

    __slots__ = ("vendor", "commands", "by_first", "known", "word_freq", "examples",
                 "seq_pos", "seq_any", "n_command_rows", "n_bad_rows", "models",
                 "size_order", "_expand_cache", "_key_cache")

    def __init__(self, vendor: str, data_dir: str):
        path = os.path.join(data_dir, vendor, "commands.jsonl")
        if not os.path.isfile(path):
            raise FileNotFoundError("命令数据不存在: %s" % path)
        self.vendor = vendor
        self.commands = {}          # 命令名 -> {syntax, view, feature, model}
        self.by_first = {}          # 首词 -> [命令名]
        self.known = set()          # 命令词表（字面量判定依据）
        self.word_freq = Counter()  # 词 -> 出现在多少条命令名里（next 排序用）
        self.examples = []          # [{'title','steps','feature','model'}]
        self.seq_pos = {}           # (步序, 命令名键) -> array('i') 例子下标
        self.seq_any = {}           # 命令名键 -> array('i') 含这条键的例子下标（片段匹配）
        self.n_command_rows = 0
        self.n_bad_rows = 0
        self.models = Counter()
        self.size_order = []
        self._expand_cache = {}
        # 一行 -> 命令名键。例子步数总量 37 万（华为一家 27.8 万），
        # 逐行算最长命令名前缀太浪费；重复行（system-view / quit ...）极多，缓存一下。
        self._key_cache = {}
        self._load(path)
        # 例子按步数降序排一次：序列补全在候选极多时按此顺序取，避免排序 2 万条
        self.size_order = sorted(range(len(self.examples)),
                                 key=lambda i: -len(self.examples[i]["steps"]))

    # -------------------------------------------------- 加载
    def _load(self, path):
        # 分两遍读：command 行必须先全部吃完，例子才能算出正确的命令名键。
        # 一份 jsonl 里 command / example 是**按特性块交错**写的，边读边算键
        # 会让"在 vlan 这一行之前就遇到 vlan 例子"变成 k=2 的兜底键，
        # 结果 `vlan 20` 一条例子都匹配不上（实测踩过）。
        examples = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue        # 坏行跳过，不让一条脏数据毁掉整个索引
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("kind")
                if kind == "command":
                    self._add_command(obj)
                elif kind == "example":
                    examples.append(obj)
        for obj in examples:
            self._add_example(obj)

    def _add_command(self, obj):
        name = str(obj.get("command") or "").strip()
        if not name:
            return
        if _BAD_NAME.search(name) or _CJK.search(name):
            # 已知损坏数据：华为 10 条。丢了它们，换索引干净。
            self.n_bad_rows += 1
            return
        low = _SPACE.sub(" ", name.lower())
        toks = low.split()
        if not toks or len(toks) > 16:
            return
        self.n_command_rows += 1
        self.known.update(toks)
        for tok in set(toks):
            self.word_freq[tok] += 1
        model = str(obj.get("model") or "")
        self.models[model] += 1
        if low in self.commands:
            return                  # 同名多条：按规格保留第一份
        self.commands[low] = {
            "name": low,
            "syntax": _SPACE.sub(" ", str(obj.get("syntax") or "").strip()),
            "view": str(obj.get("view") or "").strip(),
            "feature": str(obj.get("feature") or "").strip(),
            "model": model,
            # 来源（厂商文档里的具体文件/行）——知识库要能回答"这句话哪来的"，
            # 批次三的 commands 表也用它当 source 列。
            "source": str(obj.get("source") or "").strip(),
        }
        self.by_first.setdefault(toks[0], []).append(low)

    def _add_example(self, obj):
        steps = []
        for step in (obj.get("steps") or []):
            if not isinstance(step, str):
                continue
            line = normalize_line(step)
            if line:
                steps.append(line)
        if not steps:
            return
        idx = len(self.examples)
        self.examples.append({
            "title": str(obj.get("title") or "").strip(),
            "steps": steps,
            "feature": str(obj.get("feature") or "").strip(),
            "model": str(obj.get("model") or ""),
        })
        # seq_pos：前缀匹配用，(步序, 键) -> 例子下标
        # seq_any：片段匹配用，键 -> 含这条键的例子下标（每个例子只记一次）
        seen_keys = set()
        for pos, line in enumerate(steps):
            key = self.cached_line_key(line)
            if not key:
                continue
            self.seq_pos.setdefault((pos, key), array("i")).append(idx)
            if key not in seen_keys:
                seen_keys.add(key)
                self.seq_any.setdefault(key, array("i")).append(idx)

    # -------------------------------------------------- 命令名 / 匹配键
    def command_prefix(self, toks):
        """一行里属于**命令名**的那些 token（最长的已知命令名前缀）。

        ``vlan 20`` -> ``('vlan',)``（``20`` 是参数）；
        ``display vlan`` -> ``('display', 'vlan')``（这就是命令名本身）；
        ``interface GigabitEthernet 1/0/1`` -> ``('interface',)``。
        查不到任何已知命令名时返回 ``()``。
        """
        if not toks:
            return ()
        commands = self.commands
        best = ()
        for k in range(1, len(toks) + 1):
            if " ".join(toks[:k]) in commands:
                best = tuple(toks[:k])
        return best

    def line_key(self, toks):
        """一行的**匹配键**：去掉尾部参数后的命令名里的前 1~2 个词。

        为什么不是首词、也不是整行（实测缺陷的根因）：

        * 只比首词 —— ``display vlan`` 归一化后是 ``display``，与例子里任意
          ``display ip routing-table ...`` 的首词相同，于是推出 BGP 例子里的
          命令链，完全无关。
        * 比整行 —— ``vlan 20`` 就命不中例子里的 ``vlan 10``，
          "参数无关"这个关键特性被破坏。

        规则：先取命令名（尾部参数去掉）。命令名有 2 个词以上时比前 2 个词，
        命令名只有 1 个词（``system-view`` / ``vlan`` / ``interface``）时只比这 1 个词。
        命令名查不到（数据里没有这条命令）时要求前 2 个词相同 —— 宁可少命中，
        也不能退回"首词相同就算命中"。
        """
        if not toks:
            return ()
        name = self.command_prefix(toks)
        if len(name) >= 2:
            k = 2
        elif len(name) == 1:
            k = 1
        else:
            k = 2 if len(toks) >= 2 else 1
        return tuple(toks[:k])

    def cached_line_key(self, line):
        """:meth:`line_key` 的按行缓存版（装载 37 万步时用）。"""
        key = self._key_cache.get(line)
        if key is None:
            key = self.line_key(line.split())
            self._key_cache[line] = key
        return key

    def is_complete_command(self, toks):
        """这一行是不是**一整条合法的完整命令**（不是半截输入）。

        两步判据：

        1. 去掉尾部参数后的命令名能在命令索引里查到 —— 查不到说明用户还在敲
           （``disp`` / ``display vla``），或者这行根本不是命令；
        2. 这行必须把该命令语法里的**字面量**吃完。第 2 条不是多余的：索引里有
           一批 ``display`` 这样的命令名 —— 厂商文档里的 ``display command |
           by-linenum`` 被抽取工具截成了 ``display``，真正的命令是后面的
           ``command``。只按第 1 条判，用户敲半截的 ``display `` 会被当成一条
           完整命令去比例子，于是又推出一堆无关的配置流程。
           占位符（``vlan-id`` 之类）允许缺失 —— 那是参数，用户可以不填。
        """
        name = self.command_prefix(toks)
        if not name:
            return False
        ent = self.commands.get(" ".join(name))
        if not ent:
            return False
        depth = len(name)
        for seq in self._expand(ent.get("syntax") or ""):
            if self._seq_satisfies(seq, toks, depth):
                return True
        return False

    @staticmethod
    def _seq_satisfies(seq, toks, depth):
        """一行是否吃完了这条展开序列（``seq`` 里每项是 (是否字面量, 词)）。"""
        for i in range(min(depth, len(seq))):
            literal, word = seq[i]
            if literal and word != toks[i]:
                return False
        # 参数逐位对：字面量必须一字不差，占位符吃什么都行
        for j in range(depth, len(toks)):
            if j >= len(seq):
                return True                 # 语法比这行还短（脏数据），不再苛求
            literal, word = seq[j]
            if literal and word != toks[j]:
                return False
        # 这行吃完了：语法后面若还欠着**必选字面量**，就说明这行不完整
        for j in range(len(toks), len(seq)):
            if seq[j][0]:
                return False
        return True

    # -------------------------------------------------- 命令匹配
    def match_commands(self, words, partial):
        """找出命令名与已输入前缀相符的命令。

        * words 是已敲完的完整词（逐词**精确**相等）
        * partial 是正在输入、还没敲完的最后一个词（**前缀**相等）
        * words 为空时 = 用户在敲第一个词，按首词前缀匹配
        """
        if not words:
            out = []
            for first, names in self.by_first.items():
                if partial and not first.startswith(partial):
                    continue
                out.extend(names)
            return out
        names = self.by_first.get(words[0])
        if not names:
            return []
        depth = len(words)
        out = []
        for name in names:
            parts = name.split()
            if len(parts) < depth or parts[:depth] != words:
                continue
            if partial:
                if len(parts) <= depth or not parts[depth].startswith(partial):
                    continue
            out.append(name)
        return out

    def next_literals(self, syntax, words):
        """syntax 里紧跟在 words 之后的**字面量**词集合（占位符已剔除）。"""
        k = len(words)
        out = set()
        for seq in self._expand(syntax):
            if len(seq) <= k:
                continue
            ok = True
            for i in range(k):
                literal, word = seq[i]
                if literal and word != words[i]:
                    ok = False
                    break
            if not ok:
                continue
            literal, word = seq[k]
            if literal:
                out.add(word)
        return out

    def _expand(self, syntax):
        """展开 syntax（带缓存）。展开结果里每个词都标好了 literal/占位符。"""
        cached = self._expand_cache.get(syntax)
        if cached is not None:
            return cached
        toks = _LEX.findall(syntax or "")
        alts, _ = _parse_alts(toks, 0, "\x00")
        seqs = []
        for alt in alts:
            seqs.extend(_expand_items(alt, _EXPAND_CAP))
            if len(seqs) >= _EXPAND_CAP:
                break
        known = self.known
        out = tuple(tuple((not is_placeholder(w, known), w) for w in seq)
                    for seq in seqs[:_EXPAND_CAP])
        if len(self._expand_cache) < _EXPAND_CACHE_MAX:
            self._expand_cache[syntax] = out
        return out

    # -------------------------------------------------- 序列匹配
    def match_sequences(self, lines, limit, model=""):
        """用用户已输入的行去匹配配置举例的 steps。

        参数无关：比的是**命令名键**（见 :meth:`line_key`）而不是整行，
        所以用户敲 ``vlan 20`` 能命中例子里写 ``vlan 10`` 的那条；
        而 ``display vlan`` 不会再命中 ``display ip routing-table``（键不同）。

        两条路，按优先级：

        1. **前缀**（第 0 步对齐）—— 用户敲的就是一条流程的开头，返回后面全部步骤；
        2. **片段**（任意位置连续出现）—— 前缀一条都匹配不上、且输入 >= 2 行时，
           把这几行当成流程中间的一段。实测 ``interface GigabitEthernet 1/0/1`` +
           ``port link-type access`` 这种片段在举例里都出现在第 3、4 步
           （前面还有 ``system-view`` / ``vlan 10``），严格对齐第 0 步一条都命中不了。

        任一行取不出键（空行、命令名查不到）就整体不匹配 ——
        序列是"整条配置流程"的建议，宁可不给，也不能给错的。
        """
        n = len(lines)
        if n == 0:
            return []                   # 还没有完整命令 → 不推序列
        keys = []
        for line in lines:
            key = self.cached_line_key(line)
            if not key:
                return []
            keys.append(key)

        top = self._prefix_picks(keys, limit, model)
        if not top and n >= 2:
            top = self._fragment_picks(keys, limit, model)
        return self._sequence_items(top, lines, model)

    def _prefix_picks(self, keys, limit, model):
        """第 0 步对齐的匹配。返回 ``[(命中末步, 型号亲和, 总步数, 下标, 起点)]``。"""
        n = len(keys)
        req = []
        for i, key in enumerate(keys):
            arr = self.seq_pos.get((i, key))
            if not arr:
                return []
            req.append(arr)
        req.sort(key=len)
        base = req[0]
        others = [set(a) for a in req[1:]]
        model = (model or "").strip()

        if len(base) > 4000:
            # 候选太多（华为 system-view 一敲就有 2.5 万条候选）：
            # 直接按"步数降序"走一遍，取够 limit 条就停，不做全量排序。
            base_set = set(base)
            picks = []
            if model:
                for idx in self.size_order:
                    if idx in base_set and self.examples[idx]["model"] == model \
                            and len(self.examples[idx]["steps"]) > n:
                        picks.append(idx)
                        if len(picks) >= limit:
                            break
            chosen = set(picks)
            for idx in self.size_order:
                if len(picks) >= limit:
                    break
                if idx in chosen:
                    continue            # 上一轮按型号优先挑过的别再挑一遍
                if idx in base_set and len(self.examples[idx]["steps"]) > n:
                    picks.append(idx)
                    chosen.add(idx)
            return [(n, 1 if self.examples[i]["model"] == model else 0,
                     len(self.examples[i]["steps"]), i, 0) for i in picks[:limit]]

        heap = []
        for idx in base:
            if others and not all(idx in s for s in others):
                continue
            ex = self.examples[idx]
            total = len(ex["steps"])
            if total <= n:
                continue
            hit = 1 if (model and ex["model"] == model) else 0
            heap.append((n, hit, total, idx, 0))
        return nlargest(limit, heap)

    def _fragment_picks(self, keys, limit, model):
        """退一步：把已输入的行当成流程中间的一段，在例子里找连续出现的位置。

        用"最稀有的那一行"取候选例子（``seq_any``），逐个验证连续出现，
        复杂度由最稀有的键决定 —— 实测两行片段的候选量在几百到几千。
        """
        n = len(keys)
        sizes = []
        for i, key in enumerate(keys):
            arr = self.seq_any.get(key)
            sizes.append((len(arr) if arr else 0, i))
        sizes.sort()
        if not sizes[0][0]:
            return []
        anchor = sizes[0][1]
        anchor_key = keys[anchor]
        model = (model or "").strip()

        picks = []
        seen = set()
        for idx in self.seq_any.get(anchor_key, ()):
            if idx in seen:
                continue
            seen.add(idx)
            steps = self.examples[idx]["steps"]
            offset = None
            for pos in range(len(steps)):
                if self.cached_line_key(steps[pos]) != anchor_key:
                    continue
                start = pos - anchor
                if start < 0 or start + n > len(steps):
                    continue
                if all(self.cached_line_key(steps[start + i]) == keys[i]
                       for i in range(n)):
                    offset = start
                    break
            if offset is None:
                continue
            ex = self.examples[idx]
            hit = 1 if (model and ex["model"] == model) else 0
            picks.append((offset + n, hit, len(ex["steps"]), idx, offset))
        return nlargest(limit, picks)

    def _sequence_items(self, top, lines, model):
        """把排名结果拼成对外的 sequences 条目。"""
        out = []
        for end, hit, total, idx, offset in top:
            ex = self.examples[idx]
            coverage = end / float(total) if total else 0.0
            score = min(0.99, 0.35 + 0.35 * (end / (end + 1.0))
                        + 0.25 * min(total, 60) / 60.0 + (0.05 if hit else 0))
            if offset:                  # 片段匹配：分数略降，前缀匹配优先
                score = max(0.0, score - 0.05)
            rest = ex["steps"][end:]                    # 剩余的步骤
            out.append({
                "title": ex["title"],
                "steps": list(rest[:_MAX_SEQ_STEPS]),
                "remaining": len(rest),                 # 真实剩余步数（steps 可能被截断）
                "truncated": len(rest) > _MAX_SEQ_STEPS,
                "head": list(ex["steps"][offset:end]),  # 命中的那几步在例子里的原文
                "offset": offset,                       # 命中片段在例子里的起始步（0 = 流程开头）
                "score": round(score, 3),
                "matched": list(lines),
                "feature": ex["feature"],
                "model": ex["model"],
                "total": total,
                "coverage": round(coverage, 3),
            })
        return out


# ------------------------------------------------------------------ 缓存
def _index(vendor: str, data_dir: str) -> _VendorIndex:
    key = (vendor, os.path.abspath(data_dir))
    idx = _CACHE.get(key)
    if idx is not None:
        return idx
    with _CACHE_LOCK:
        idx = _CACHE.get(key)
        if idx is None:
            idx = _VendorIndex(vendor, data_dir)
            _CACHE[key] = idx
    return idx


def cached_vendors():
    """已加载缓存的 (vendor, data_dir) 列表，供自测/诊断使用。"""
    return sorted(_CACHE.keys())


def clear_cache():
    """丢掉所有索引缓存（自测用；正常路径不需要）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ------------------------------------------------------------------ 单条命令定位
def lookup(text, vendor, data_dir=None, max_examples=3):
    """把一段文本**定位到一条命令**，并把它旁边的东西一起带回来。

    用途：终端里的 Ctrl+点击（浮层候选 / 输入行里已经敲好的命令）→ 打开知识库。

    匹配顺序（刻意与 suggest() 共用同一份索引和"命令名"概念，不另造一套规则）：
      1. ``exact``  —— 整段就是一条已知命令名
      2. ``prefix`` —— 整段是某条命令名的前缀（``display inte`` → ``display interface``；
                       半截的最后一个词也算，所以**不能**要求词边界），
                       多条命中时取**最短**的那条（最接近用户已经敲出来的东西）
      3. ``name``   —— 整段是"命令名 + 尾部参数"（``display interface brief`` →
                       命令名 ``display interface``）：用索引现成的 ``command_prefix()``
                       取最长的已知命令名前缀。**这一步很关键**：没有它，
                       `display interface brief` 会退化成 `display`（实测踩过）。
      4. ``first``  —— 最后兜底：按**第一个 token** 精确匹配（需求要求的兜底）
      5. 都查不到 → ``found=False``（调用方给"知识库暂无该命令"，不许静默无反应）

    例子同样是**确定性**取回来的：走索引里已有的 ``seq_any``（命令名键 → 例子下标），
    再按"这一步真的以这条命令开头"过滤，按步数升序（越短越聚焦）。没有 AI 参与。
    """
    out = {
        "found": False,
        "query": str(text or ""),
        "vendor": vendor,
        "match": "",
        "command": "",
        "syntax": "",
        "view": "",
        "feature": "",
        "model": "",
        "source": "",
        "tech_note": "",        # 厂商数据里没有"短讲解"这一列 → 留空，不编
        "examples": [],
    }
    norm = _SPACE.sub(" ", str(text or "").strip().lower())
    if not norm or not vendor:
        return out

    try:
        idx = _index(vendor, data_dir or DEFAULT_DIR)
    except Exception as exc:                        # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out

    def _exact(name):
        return name if name in idx.commands else None

    def _prefix(name):
        # 整段（含半截的最后一个词）是命令名的前缀；取最短命中
        # （"display inte" → display interface；"display inter" → huawei 也取 interface 而非 interzone）
        hits = [c for c in idx.commands if c.startswith(name)]
        if not hits:
            return None
        return min(hits, key=len)

    name = norm
    found = _exact(name)
    if found:
        out["match"] = "exact"
    else:
        found = _prefix(name)
        if found:
            out["match"] = "prefix"
    if not found:
        # 命令名 + 尾部参数：取最长的已知命令名前缀（`display interface brief`
        # → `display interface`）。这一步必须在"首个 token"兜底**之前**做，
        # 否则会定位到过短的 `display`。
        toks = norm.split()
        cp = idx.command_prefix(toks)
        if cp:
            found = " ".join(cp)
            out["match"] = "name"
    if not found:
        # 最后兜底：只按第一个 token（用户点在中间某个词上）
        first = norm.split()[0]
        found = _exact(first)
        if found:
            out["match"] = "first"

    if not found:
        return out

    info = idx.commands.get(found) or {}
    out.update({
        "found": True,
        "command": found,
        "syntax": info.get("syntax", ""),
        "view": info.get("view", ""),
        "feature": info.get("feature", ""),
        "model": info.get("model", ""),
        "source": info.get("source", ""),
    })
    # 例子：先按索引里的命令名键取候选（快），再确认这一步真的以该命令开头（准）
    try:
        key = idx.line_key(found.split())
        picked = []
        for i in idx.seq_any.get(key, []):
            ex = idx.examples[i]
            if any(s == found or s.startswith(found + " ") for s in ex["steps"]):
                picked.append(ex)
        picked.sort(key=lambda e: len(e["steps"]))
        out["examples"] = [
            {"title": e.get("title", ""), "feature": e.get("feature", ""),
             "model": e.get("model", ""), "steps": e.get("steps", [])}
            for e in picked[:max(1, int(max_examples or 1))]
        ]
    except Exception:                               # noqa: BLE001
        out["examples"] = []
    return out


# ------------------------------------------------------------------ 视图 / 模式
# 设备提示符就在告诉我们**现在在哪个视图**，而每条命令数据都带 ``view`` 字段 ——
# 两者一对上，就能把"这个视图里根本敲不出来"的命令从候选里去掉。
# 例如 H3C 的 ``interface`` / ``vlan`` 都是**系统视图**，在 ``<H3C>``（用户视图）
# 下敲 `int` 就不该把它们推出来。
#
# 三家的词汇**完全不同**（实测数据得出，别想当然）：
#   * h3c     : 任意视图 / 用户视图 / 系统视图                    —— 用"视图"
#   * huawei  : 所有视图 / 用户视图 / 系统视图                    —— 用"视图"，且**不是**"任意"
#   * ruijie  : （没有通配标签）用户模式 / 特权模式 / 全局配置模式   —— 用"模式"
_WILDCARD_VIEWS = {
    "h3c": ("任意视图",),
    "huawei": ("所有视图",),
    "ruijie": (),          # 锐捷数据里没有"任意模式"这种通配标签
}

# 子视图提示符里总会出现这些词之一。用来区分：
#   [H3C-GigabitEthernet1/0/1] 真子视图  vs  [my-sw-1] 只是主机名带横线
_SUBVIEW_HINTS = (
    "bgp", "ospf", "isis", "rip", "pim", "vlan", "vlanif", "loopback", "tunnel",
    "vsi", "trunk", "aggregation", "ethernet", "serial", "cellular", "acl",
    "radius", "hwtacacs", "user-interface", "line", "qos", "class", "behavior",
    "policy", "nat", "ipsec", "ike", "dhcp", "pool", "mst", "stp", "nqa",
    "ssh", "telnet", "local-user", "probe", "filter",
)


def current_view(vendor, prompt):
    """设备提示符 → 当前视图 / 模式名；**拿不准就返回空串**。

    调用方拿到空串就**跳过过滤**。为什么宁可不判：视图判错会把用户真正能用的
    命令藏起来，比"多给几条"糟得多。所以只认最有把握的几种形态，
    其余（接口 / VLAN / BGP 等子视图，锐捷的 ``(config-if)#``）一律空串。
    """
    p = str(prompt or "").strip()
    ven = str(vendor or "").strip().lower()
    if not p:
        return ""

    if ven in ("h3c", "huawei"):
        # <H3C>          → 用户视图
        # [H3C]          → 系统视图
        # [H3C-GE1/0/1]  → 子视图（不判）
        # [my-sw-1]      → 主机名带横线，仍是系统视图
        if p.startswith("<") and p.endswith(">"):
            return "用户视图"
        if p.startswith("[") and p.endswith("]"):
            inner = p[1:-1]
            if "-" not in inner:
                return "系统视图"
            tail = inner.split("-", 1)[1].lower()
            if "/" in tail or "(" in tail or any(k in tail for k in _SUBVIEW_HINTS):
                return ""                        # 确实是子视图
            return "系统视图"                       # 只是主机名里带横线
        return ""

    if ven == "ruijie":
        if re.search(r"\(config-[^)]*\)#$", p):
            return ""                            # 子模式，不判
        if p.endswith("(config)#"):
            return "全局配置模式"
        if p.endswith("#"):
            return "特权模式"
        if p.endswith(">"):
            return "用户模式"
        return ""
    return ""


def _view_matches(label, vendor, current):
    """命令数据里的一个 view 标签，在当前视图下是否可用。"""
    label = (label or "").strip()
    if not label:
        return True                     # 数据没写视图 → 不拦
    for wildcard in _WILDCARD_VIEWS.get(vendor, ()):
        if label == wildcard:
            return True
        if label.startswith(wildcard + "（"):        # 如 `所有视图（除用户视图）`
            inner = label[label.find("（") + 1: label.rfind("）")]
            inner = inner[1:] if inner.startswith("除") else inner
            return not (current and current in inner)
    return bool(current) and label == current


def _entry_in_view(idx, name, vendor, current):
    ent = idx.commands.get(name) or {}
    raw = str(ent.get("view") or "")
    if not raw:
        return True
    # 华为的 view 字段可能是 `A / B / C`（一个字段挂多个视图）
    return any(_view_matches(part, vendor, current)
               for part in raw.split(" / "))


def _filter_by_view(idx, names, vendor, current):
    """按当前视图过滤候选。**过滤后为空就是空，不退回全量。**

    一开始想的是"过滤后为空就退回全量，免得用户没提示"，但实测立刻否掉了：
    h3c 在用户视图 ``<H3C>`` 下敲 ``int``，过滤后为空（``interface`` 等全是
    系统视图命令），退回全量就把 ``interface`` 又端了出来 —— 正是用户抱怨的
    "提示的命令是错的"。锐捷 ``Ruijie>`` 同样如此。

    取舍：数据缺一条/标错一条，代价只是"这次没提示"；而多给一条，代价是
    "用户照着敲、被设备拒绝"。后者更坑，所以选硬过滤。
    """
    if not current:
        return names
    return [n for n in names if _entry_in_view(idx, n, vendor, current)]


# ------------------------------------------------------------------ 三层查询
def _token_candidates(idx, matched, words, partial, limit):
    """第①层：正在输入的这个词补成什么。"""
    out = []
    if not words:
        # 敲第一个词：按首词去重（否则 display 一家就有 1794 条命令淹掉界面）
        seen = {}
        for name in matched:
            first = name.split(" ", 1)[0]
            info = seen.get(first)
            if info is None:
                ent = idx.commands.get(name) or {}
                seen[first] = {
                    "count": 1,
                    "view": ent.get("view", ""),
                    "feature": ent.get("feature", ""),
                }
            else:
                info["count"] += 1
        ranked = sorted(seen.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        for first, info in ranked[:limit]:
            out.append({"text": first, "kind": "command",
                        "view": info["view"], "feature": info["feature"]})
        return out

    # 已经输入了完整词：补全**整条命令名**（右侧要显示 view / feature）
    names = sorted(set(matched), key=lambda name: (len(name.split()), len(name), name))
    for name in names[:limit]:
        ent = idx.commands.get(name) or {}
        out.append({"text": name, "kind": "command",
                    "view": ent.get("view", ""), "feature": ent.get("feature", "")})
    return out


def _next_candidates(idx, matched, words, partial, limit):
    """第②层：下一个可选词。命令名 + syntax 两条路都取，最后统一滤占位符。

    排序（实测数据决定，纯词频会把 `version` 这种"常用但文档里只出现 4 次"的词挤出去）：
      1. 该词能否把用户已输入的前缀**补成一条完整命令**（`display vlan` 本身就是命令）
      2. 该词在**全厂商命令词表**里的出现次数 + 以此词续写的命令条数
      3. 字母序
    """
    if not words:
        return []                   # 还在敲第一个词，第①层已经在补它了，别重复
    depth = len(words)
    name_hits = Counter()
    for name in matched:
        parts = name.split()
        if len(parts) > depth:
            word = parts[depth]
            if partial and not word.startswith(partial):
                continue
            name_hits[word] += 1
    literals = set()
    expanded = 0
    for name in matched:
        if expanded >= _EXPAND_COMMANDS_MAX:
            break
        ent = idx.commands.get(name)
        if not ent or not ent.get("syntax"):
            continue
        expanded += 1
        literals.update(idx.next_literals(ent["syntax"], words))

    known = idx.known
    commands = idx.commands
    word_freq = idx.word_freq
    scored = []
    for word in set(name_hits) | literals:
        if partial and not word.startswith(partial):
            continue
        if is_placeholder(word, known):
            continue                # 单一出口过滤：占位符绝不进候选
        if word not in name_hits and _PH_STRONG.search(word):
            # 只出现在 syntax 里、又长成 xxx-id 的样子 → 占位符
            continue
        complete = 1 if " ".join(words + [word]) in commands else 0
        weight = word_freq.get(word, 0) + 2 * name_hits.get(word, 0)
        scored.append((complete, weight, word))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [{"text": word, "kind": "next"} for _, _, word in scored[:limit]]


def suggest(typed, vendor, model=None, limit=20, data_dir=None, prompt=None) -> dict:
    """三层补全。

    :param typed: 终端里当前已经输入的全部文本，可能多行，
                  最后一行是正在输入（还没回车）的那一行。
    :param vendor: ``h3c`` / ``huawei`` / ``ruijie``；空则按 model 猜。
    :param model: 设备型号，用于序列补全的排序亲和（可选）。
    :param limit: 每层最多返回多少条。
    :param data_dir: 数据根目录，默认 :data:`DEFAULT_DIR`。
    :param prompt: 设备当前提示符（如 ``<H3C>`` / ``[H3C]`` / ``[H3C-GE1/0/1]``
                   / ``Ruijie#``）。给了就按它算出当前视图，
                   **只在这个视图里能敲的命令**才进候选 —— 这是"按设备真实上下文
                   过滤"，而不是只靠厂商名。拿不准的形态会自动跳过过滤。
    :returns: ``{"token": [...], "next": [...], "sequences": [...], ...}``。

              * ``token`` 每项：``{text, kind:"command", view, feature}``
              * ``next``  每项：``{text, kind:"next"}``
              * ``sequences`` 每项：``{title, steps, remaining, truncated, head,
                score, matched, feature, model, total, coverage}`` ——
                ``steps`` 是剩余步骤（最多 :data:`_MAX_SEQ_STEPS` 步），
                ``remaining`` 是真实剩余步数，``head`` 是命中的那几步在例子里的原文
                （用户敲 ``vlan 20``、例子写 ``vlan 10`` 时能看出来）。

              匹配粒度：比对**命令名**（尾部参数去掉）的前 1~2 个词，不是首词 ——
              所以 ``display vlan`` 不会再命中 ``display ip routing-table`` 的流程。
              半截输入（最后一行没敲回车、且不是完整合法命令，如 ``display ``）
              不参与序列匹配，这时 ``sequences`` 为空。

              **永不抛异常** —— 补全失败最多是"没提示"，不该打断 RPC 请求。
    """
    ven = str(vendor or "").strip().lower()
    mdl = str(model or "").strip()
    result = {"vendor": ven, "model": mdl, "error": "", "words": [], "partial": "",
              "view": "", "token": [], "next": [], "sequences": []}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    if limit <= 0:
        limit = 20

    if ven not in VENDORS:
        ven = infer_vendor(mdl)
        result["vendor"] = ven
    if ven not in VENDORS:
        # 厂商推不出来 → 空结果，而不是报错（界面照常显示，不弹错误）
        return result

    try:
        idx = _index(ven, data_dir or DEFAULT_DIR)
    except Exception as exc:        # noqa: BLE001
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        return result

    try:
        text = typed if isinstance(typed, str) else ("" if typed is None else str(typed))
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        last_raw = lines[-1] if lines else ""
        committed = [normalize_line(x) for x in lines[:-1]]
        committed = [x for x in committed if x]
        last_line = normalize_line(last_raw)
        ends_space = bool(last_raw) and last_raw[-1].isspace()
        toks = last_line.split()

        if ends_space:
            words, partial = toks, ""       # 词已敲完，在等下一个词
        elif toks:
            words, partial = toks[:-1], toks[-1]   # 最后一个词还在输入中
        else:
            words, partial = [], ""
        result["words"] = words
        result["partial"] = partial

        if words or partial:
            matched = idx.match_commands(words, partial)
            # 按设备当前视图过滤（拿不准时 current_view 返回空串 → 不过滤）
            view = current_view(ven, prompt)
            result["view"] = view
            matched = _filter_by_view(idx, matched, ven, view)
            result["token"] = _token_candidates(idx, matched, words, partial, limit)
            result["next"] = _next_candidates(idx, matched, words, partial, limit)

        # 序列补全：已回车的前几行 + 最后一行。
        # 最后一行只在"它是一整条合法命令"时才参与 —— 用户还在敲的时候
        # （`display ` / `display vla`）不推序列；`system-view`、`vlan 20`
        # 这种完整命令照旧参与（别改坏）。
        seq_lines = list(committed)
        if last_line and idx.is_complete_command(toks):
            seq_lines.append(last_line)
        result["sequences"] = idx.match_sequences(seq_lines, limit, mdl)
    except Exception as exc:        # noqa: BLE001
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["token"] = []
        result["next"] = []
        result["sequences"] = []
    return result


def stats(vendor, data_dir=None) -> dict:
    """索引规模，供自测与诊断。"""
    ven = str(vendor or "").strip().lower()
    out = {"vendor": ven, "commands": 0, "examples": 0, "error": ""}
    if ven not in VENDORS:
        out["error"] = "未知厂商: %s" % vendor
        return out
    try:
        idx = _index(ven, data_dir or DEFAULT_DIR)
    except Exception as exc:        # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    out.update({
        "commands": idx.n_command_rows,            # 有效 command 行数
        "command_names": len(idx.commands),        # 去重后的命令名数
        "commands_dropped": idx.n_bad_rows,        # 被过滤的损坏命令名
        "examples": len(idx.examples),
        "words": len(idx.known),
        "seq_keys": len(idx.seq_pos),
        "models": dict(idx.models.most_common(6)),
    })
    return out

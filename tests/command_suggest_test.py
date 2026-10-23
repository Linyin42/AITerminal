"""命令自动补全接口自测 —— 三层补全 + 性能 + 无污染 + 降级。

覆盖验收用的 8 个用例（token / next / sequences），外加：
  * 性能：连续 20 次 suggest（首次含加载），后续每次必须 < 50 ms
  * 无污染：next 候选里不许出现 syntax 的占位符（<...>、xxx-id 之类）
  * 降级：数据目录不存在时返回 error 而不是抛异常

运行：
    python tests/command_suggest_test.py
"""

import json
import os
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import command_suggest as cs          # noqa: E402

PASS, FAIL = [], []
VENDOR = "h3c"          # 三个业务用例里必看的厂商
MODEL = "CR16000"

# next 候选里**不允许**出现的东西：尖括号/方括号/花括号/竖线、中文、xxx-id 形态
POLLUTION = re.compile(r"[<>\[\]{}|]|[\u4e00-\u9fff]|-(?:id|idx|index|num|number)\d*$")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
    if detail:
        print("         %s" % detail)
    return bool(cond)


def dump(res, max_items=3, max_steps=3):
    """原始结果的紧凑打印：字段全在，列表按 max 截断并标注。"""
    return {
        "vendor": res.get("vendor"), "model": res.get("model"),
        "words": res.get("words"), "partial": res.get("partial"),
        "error": res.get("error"),
        "token": [x for x in res.get("token", [])[:max_items]],
        "token总数": len(res.get("token", [])),
        "next": [x["text"] for x in res.get("next", [])[:max_items * 2]],
        "next总数": len(res.get("next", [])),
        "sequences": [{
            "title": s["title"], "matched": s["matched"], "head": s["head"],
            "score": s["score"], "total": s["total"],
            "steps(前%d步)" % max_steps: s["steps"][:max_steps],
            "steps总数": len(s["steps"]),
        } for s in res.get("sequences", [])[:max_items]],
        "sequences总数": len(res.get("sequences", [])),
    }


print("=" * 78)
print("NetOpsTerminal 命令自动补全自测  commands.suggest")
print("=" * 78)

# ---------------------------------------------------------------- 0. 数据规模
print("\n【0】索引规模（懒加载，只加载当前厂商）")
for vendor in cs.VENDORS:
    t0 = time.perf_counter()
    st = cs.stats(vendor)
    ms = (time.perf_counter() - t0) * 1000
    print("   %-7s 加载 %7.1f ms  commands=%d (去重命令名 %d, 过滤损坏 %d)  examples=%d  "
          "词表=%d 序列索引键=%d  型号=%s"
          % (vendor, ms, st["commands"], st["command_names"], st["commands_dropped"],
             st["examples"], st["words"], st["seq_keys"],
             ",".join(list(st["models"])[:3])))
# 2026-10-23：ruijie 从 12387 → 12990 → 15259 —— 补了 `show …` 命令（原来整个 show
# 家族只有 2 条，补全给不出 `show vlan`）。来源：tools/ruijie_fulltext_commands.py
# 抽 312 份「命令参考」，228101 行原始条目按命令名去重到 2867 条，其中 2269 条是新命令。
check("三家索引规模与原始数据一致（h3c 8556/821, ruijie 15259/5721）",
      cs.stats("h3c")["commands"] == 8556 and cs.stats("h3c")["examples"] == 821
      and cs.stats("ruijie")["commands"] == 15259 and cs.stats("ruijie")["examples"] == 5721,
      "huawei commands=%d（原始 29835，被过滤的 66 条是 `undo {`/`ftp { put | get }` 这类语法碎片）"
      % cs.stats("huawei")["commands"])

# ---------------------------------------------------------------- 1
print("\n【1】`disp` —— 第①层 token 前缀匹配首词")
r1 = cs.suggest("disp", VENDOR, model=MODEL)
print("   原始:", dump(r1))
hit1 = [x for x in r1["token"] if x["text"].startswith("display")]
check("token 里有 display 开头的候选", bool(hit1),
      "命中 %d 条，前 3: %s" % (len(hit1), [x["text"] for x in hit1[:3]]))

# ---------------------------------------------------------------- 2
print("\n【2】`display ` —— 第②层 next（命令已完整，取第二个词）")
r2 = cs.suggest("display ", VENDOR, model=MODEL)
print("   原始(前3条):", dump(r2))
words20 = [x["text"] for x in r2["next"]]
full2 = cs.suggest("display ", VENDOR, model=MODEL, limit=500)
words_all = [x["text"] for x in full2["next"]]
print("   limit=20 的 next:", words20)
print("   limit=500 的完整候选 %d 个" % len(words_all))
want = ("vlan", "interface", "version", "device")
for w in want:
    print("      %-10s limit20=%-5s 完整候选里=%-5s 名次=%s"
          % (w, w in words20, w in words_all,
             (words_all.index(w) + 1) if w in words_all else "-"))
check("next 里有 vlan/interface/version 等（去重候选集）",
      all(w in words_all for w in want) and bool(words20),
      "limit=20 只放前 20 个：version 在 %s 名、device 在 %s 名（h3c 里 display version 只有 4 条命令，"
      "所以进不了前 20；完整候选集里都在）"
      % ((words_all.index("version") + 1) if "version" in words_all else "-",
         (words_all.index("device") + 1) if "device" in words_all else "-"))

# ---------------------------------------------------------------- 3
print("\n【3】`display vlan` —— 能补出该命令且带 view/feature")
r3 = cs.suggest("display vlan", VENDOR, model=MODEL)
print("   原始:", dump(r3))
ok3 = [x for x in r3["token"] if x["text"] == "display vlan"]
check("token 补出 display vlan 且带 view/feature", bool(ok3) and ok3[0].get("view") and ok3[0].get("feature"),
      "token=%s" % [x["text"] for x in r3["token"]])

# ---------------------------------------------------------------- 4
print("\n【4】`system-view`（完整一行）—— 第③层 sequences")
r4 = cs.suggest("system-view", VENDOR, model=MODEL)
print("   原始(前2条):", dump(r4, max_items=2))
big = max(r4["sequences"], key=lambda s: s["remaining"])
print("   最长一条: title=%r 总步数=%d 剩余=%d 回传=%d 截断=%s"
      % (big["title"], big["total"], big["remaining"], len(big["steps"]), big["truncated"]))
check("sequences 非空且每条都返回后续步骤",
      bool(r4["sequences"]) and all(s["steps"] for s in r4["sequences"]),
      "前 3 条: %s" % [(s["title"][:22], "剩余%d步" % s["remaining"]) for s in r4["sequences"][:3]])
check("超长举例按窗口截断但真实剩余步数不丢（steps<=40 且 remaining 准确）",
      all(len(s["steps"]) <= 40 and s["remaining"] == s["total"] - len(s["matched"])
          and (not s["truncated"] or len(s["steps"]) == 40)
          for s in r4["sequences"]),
      "最长一条 total=%d remaining=%d steps=%d" % (big["total"], big["remaining"], len(big["steps"])))

# ---------------------------------------------------------------- 5
print("\n【5】`system-view\\nvlan 10`（两行）—— 序列逐行匹配")
r5 = cs.suggest("system-view\nvlan 10", VENDOR, model=MODEL)
print("   原始(前2条):", dump(r5, max_items=2))
ok5 = [s for s in r5["sequences"] if s["matched"] == ["system-view", "vlan 10"]]
check("sequences 命中且 matched 含这两行",
      bool(r5["sequences"]) and bool(ok5),
      "命中 %d 条，前 3: %s" % (len(r5["sequences"]),
                              [(s["title"][:22], s["matched"], s["head"]) for s in r5["sequences"][:3]]))

# ---------------------------------------------------------------- 6
print("\n【6】`vlan 20` —— 参数无关匹配（应命中例子里写 `vlan 10` 的那条）")
r6 = cs.suggest("vlan 20", VENDOR, model=MODEL)
print("   原始:", dump(r6, max_items=2))
agnostic = [s for s in r6["sequences"]
            if s["head"] and s["head"][0].split()[0] == "vlan" and s["head"][0] != "vlan 20"]
check("能匹配到 vlan 10 的例子（例子原文 head 与用户输入不同）",
      bool(agnostic),
      "例子原文 head=%s，用户输入 matched=%s" % (
          agnostic[0]["head"] if agnostic else None,
          agnostic[0]["matched"] if agnostic else None))

# ---------------------------------------------------------------- 7
print("\n【7】不存在的 `zzzzz` —— 三层都空且不报错")
r7 = cs.suggest("zzzzz", VENDOR, model=MODEL)
print("   原始:", dump(r7))
check("三层都为空且 error 为空",
      r7["token"] == [] and r7["next"] == [] and r7["sequences"] == [] and r7["error"] == "")

# ---------------------------------------------------------------- 8
print("\n【8】vendor 传错 / 为空 / 按型号推断")
for params, want_vendor, want_empty in (
        (("display ", "zzz"), "", True),
        (("display ", ""), "", True),
        (("display ", "", "CR16000"), "h3c", False),
        (("display ", "", "S12500"), "h3c", False),
        (("display ", "", "CE16800"), "huawei", False),
        (("display ", "", "USG6000F"), "huawei", False),
        (("show ", "", "RG-N18000"), "ruijie", False),
):
    typed, ven, *mdl = params
    model = mdl[0] if mdl else ""
    res = cs.suggest(typed, ven, model=model)
    empty = not (res["token"] or res["next"] or res["sequences"])
    print("   typed=%-10r vendor=%-4r model=%-10r -> vendor=%r 三层空=%s error=%r"
          % (typed, ven, model, res["vendor"], empty, res["error"]))
    check("vendor%r + model%r 推断为 %r" % (ven, model, want_vendor),
          res["vendor"] == want_vendor and empty == want_empty and res["error"] == "")
# 厂商为空且 model 也推不出时，绝不能抛异常
r8 = cs.suggest("display ", "", model="")
check("推不出厂商时返回空结果而不是报错",
      r8["token"] == [] and r8["next"] == [] and r8["sequences"] == [] and r8["error"] == "")

# ---------------------------------------------------------------- 9. 性能
print("\n【9】性能：连续 20 次 suggest（含首次加载），后续每次应 < 50 ms")
PERF_QUERIES = ["disp", "display ", "display vlan", "system-view\nvlan 10", "display interface "]
for vendor in cs.VENDORS:
    cs.clear_cache()                     # 逼出真实的首次加载
    times = []
    for i in range(20):
        q = PERF_QUERIES[i % len(PERF_QUERIES)]
        t0 = time.perf_counter()
        cs.suggest(q, vendor, limit=20)
        times.append((time.perf_counter() - t0) * 1000.0)
    rest = times[1:]
    sizes = []
    for q in PERF_QUERIES:
        res = cs.suggest(q, vendor, limit=20)
        sizes.append(len(json.dumps(res, ensure_ascii=False).encode("utf-8")))
    print("   %-7s 第1次(含加载)=%8.2f ms | 后19次 max=%6.2f ms 平均=%6.2f ms | 单次响应 max=%5.1f KB | 明细: %s"
          % (vendor, times[0], max(rest), sum(rest) / len(rest), max(sizes) / 1024.0,
             " ".join("%.1f" % x for x in rest)))
    check("%s 首次加载后每次 < 50 ms" % vendor, max(rest) < 50.0,
          "最慢 %.2f ms（限额 50 ms）" % max(rest))
    check("%s 单次响应 < 40 KB（按键路径扛不住大包）" % vendor, max(sizes) < 40 * 1024,
          "最大 %.1f KB" % (max(sizes) / 1024.0))

# ---------------------------------------------------------------- 10. 无污染
print("\n【10】无污染：next 候选里不得出现 syntax 的占位符")
POLLUTION_QUERIES = {
    "h3c": ["display ", "display vlan ", "display interface ", "interface ", "system-view\ninterface ",
            "display ip ", "undo "],
    "huawei": ["display ", "display vlan ", "display interface ", "interface ", "system-view\ninterface "],
    "ruijie": ["show ", "show ip ", "interface ", "configure terminal\nvlan "],
}
leaks, checked = [], 0
for vendor, queries in POLLUTION_QUERIES.items():
    for q in queries:
        res = cs.suggest(q, vendor, limit=200)
        for item in res["next"]:
            checked += 1
            if POLLUTION.search(item["text"]):
                leaks.append((vendor, q, item["text"]))
print("   检查 %d 个 next 候选（limit=200，覆盖 %d 条查询）"
      % (checked, sum(len(v) for v in POLLUTION_QUERIES.values())))
print("   反例数: %d %s" % (len(leaks), leaks[:6]))
sample = cs.suggest("display vlan ", VENDOR, limit=200)
print("   样例 `display vlan ` 的 next:", [x["text"] for x in sample["next"]][:12])
check("占位符反例数 == 0（<...>、xxx-id、中文一律不出现在 next 里）", not leaks)

# ---------------------------------------------------------------- 11. RPC 层
print("\n【11】引擎 RPC：commands.suggest 注册 / 厂商推断 / 目录缺失降级")
from engine.service import EngineService          # noqa: E402


class _Emitter:
    def notify(self, *args, **kwargs):
        pass


service = EngineService(_Emitter())
check("methods 里注册了 commands.suggest", "commands.suggest" in service.methods)

res = service.dispatch("commands.suggest",
                       {"typed": "disp", "vendor": "h3c", "model": "CR16000"})
print("   RPC `disp`: token=%s next=%s seq=%d error=%r"
      % ([x["text"] for x in res["token"][:3]], [x["text"] for x in res["next"][:3]],
         len(res["sequences"]), res["error"]))
check("RPC 返回三层结构", res["token"] and res["error"] == "")

res = service.dispatch("commands.suggest", {"typed": "display ", "vendor": "",
                                            "model": "CE16800"})
print("   RPC 空 vendor + model=CE16800 -> vendor=%r next=%s" % (res["vendor"], [x["text"] for x in res["next"][:5]]))
check("RPC 按 model 推断出 huawei", res["vendor"] == "huawei" and res["error"] == "")

res = service.dispatch("commands.suggest", {"typed": "display ", "vendor": "bogus"})
print("   RPC vendor=bogus -> %s" % dump(res))
check("RPC vendor 传错 → 空结果不崩溃",
      res["token"] == [] and res["next"] == [] and res["sequences"] == [])

bad_dir = os.path.join(os.environ.get("TEMP") or ".", "__ait_no_such_dir__")
os.environ["AIT_VENDOR_CMD_DIR"] = bad_dir
try:
    res = service.dispatch("commands.suggest", {"typed": "disp", "vendor": "h3c"})
finally:
    os.environ.pop("AIT_VENDOR_CMD_DIR", None)
print("   数据目录=%r -> error=%r token=%s next=%s sequences=%s"
      % (bad_dir, res["error"], res["token"], res["next"], res["sequences"]))
check("数据目录不存在 → 返回 error 且不抛异常",
      bool(res["error"]) and res["token"] == [] and res["next"] == [] and res["sequences"] == [],
      "error=%r" % res["error"])
res = service.dispatch("commands.suggest", {"typed": "disp", "vendor": "h3c"})
check("坏目录不会污染缓存（改回正常目录仍然可用）", bool(res["token"]) and res["error"] == "")

# ---------------------------------------------------------------- 12. 大候选集分支
print("\n【12】大候选集分支（华为 `system-view` 有 2.5 万条候选，走『步数降序扫描』快路径）")
t0 = time.perf_counter()
r12 = cs.suggest("system-view", "huawei", model="CE16800", limit=20)
ms12 = (time.perf_counter() - t0) * 1000
keys = [(s["title"], s["total"], s["model"]) for s in r12["sequences"]]
print("   耗时 %.2f ms  命中 %d 条  前 3: %s" % (ms12, len(r12["sequences"]),
      [(s["title"][:20], s["total"], s["model"]) for s in r12["sequences"][:3]]))
check("大候选集下不重复、带型号亲和、且仍在 50 ms 内",
      len(set(keys)) == len(keys) and any(s["model"] == "CE16800" for s in r12["sequences"])
      and ms12 < 50.0,
      "去重后 %d 条 / 共 %d 条，CE16800 命中 %d 条"
      % (len(set(keys)), len(keys), sum(1 for s in r12["sequences"] if s["model"] == "CE16800")))

# ---------------------------------------------------------------- 13. 序列误匹配回归
print("\n【13】序列匹配粒度 + 半截输入（修 `display vlan` 推出 BGP 流程的缺陷）")
idx13 = cs._index(VENDOR, cs.DEFAULT_DIR)

# 匹配键：去掉尾部参数后的命令名里的前 1~2 个词
key_cases = [
    (["display", "vlan"], ("display", "vlan"), "两词命令名比前 2 个词"),
    (["display", "ip", "routing-table", "vpn-instance"], ("display", "ip"),
     "多词命令名也只比前 2 个词"),
    (["vlan", "20"], ("vlan",), "单词命令名 + 参数：只比 1 个词（参数无关）"),
    (["interface", "gigabitethernet", "1/0/1"], ("interface",),
     "interface 的命令名只有 1 个词，接口名是参数"),
    (["system-view"], ("system-view",), "单词命令"),
]
bad_keys = [(t, idx13.line_key(t), want) for t, want, _ in key_cases
            if idx13.line_key(t) != want]
print("   匹配键: %s" % [(idx13.line_key(t), want) for t, want, _ in key_cases])
print("   反例: %s" % bad_keys)
check("line_key 比的是命令名（前 1~2 个词），不是首词、也不是整行", not bad_keys)

# 完整命令判定（半截输入的门槛）
complete_cases = [
    (["system-view"], True), (["vlan", "20"], True), (["display", "vlan"], True),
    (["interface", "gigabitethernet", "1/0/1"], True),
    (["port", "link-type", "access"], True),
    (["display"], False),            # 文档里的 `display command | by-linenum` 被截成了 display
    (["display", "vla"], False), (["disp"], False), (["zzzzz"], False),
]
bad_complete = [(t, idx13.is_complete_command(t)) for t, want in complete_cases
                if idx13.is_complete_command(t) != want]
print("   完整性判定: %s"
      % [(t, idx13.is_complete_command(t)) for t, _ in complete_cases])
check("is_complete_command 认得出半截输入（display / display vla / disp）",
      not bad_complete,
      "反例: %s" % bad_complete)

# 6 个验收用例
r13a = cs.suggest("display ", VENDOR, model=MODEL)
print("   `display `           -> sequences=%d token=%d next=%d"
      % (len(r13a["sequences"]), len(r13a["token"]), len(r13a["next"])))
check("`display ` 的 sequences 必须为空（半截输入不推序列）",
      r13a["sequences"] == [], "实测 %d 条" % len(r13a["sequences"]))

r13b = cs.suggest("display vlan", VENDOR, model=MODEL)
heads13 = [h for s in r13b["sequences"] for h in s["head"]]
print("   `display vlan`       -> sequences=%d head=%s"
      % (len(r13b["sequences"]), heads13[:4]))
check("`display vlan` 只命中真正以 display vlan 开头的例子（不再推 BGP 的 display ip routing-table）",
      all(h.startswith("display vlan") for h in heads13)
      and not any(h.startswith("display ip") for h in heads13),
      "head=%s" % heads13[:4])

r13c = cs.suggest("system-view", VENDOR, model=MODEL)
print("   `system-view`        -> sequences=%d matched=%s"
      % (len(r13c["sequences"]), [s["matched"] for s in r13c["sequences"][:2]]))
check("`system-view` 仍返回序列，且 matched 只含 system-view",
      bool(r13c["sequences"]) and all(s["matched"] == ["system-view"]
                                      for s in r13c["sequences"]))

r13d = cs.suggest("system-view\nvlan 10", VENDOR, model=MODEL)
print("   `system-view\\nvlan 10` -> sequences=%d matched=%s"
      % (len(r13d["sequences"]), [s["matched"] for s in r13d["sequences"][:2]]))
check("`system-view\\nvlan 10` 命中两条的流程",
      any(s["matched"] == ["system-view", "vlan 10"] for s in r13d["sequences"]))

r13e = cs.suggest("vlan 20", VENDOR, model=MODEL)
print("   `vlan 20`            -> sequences=%d head=%s offset=%s"
      % (len(r13e["sequences"]), [s["head"] for s in r13e["sequences"][:2]],
         [s["offset"] for s in r13e["sequences"][:2]]))
check("`vlan 20` 参数无关：仍命中例子原文 `vlan 10` 的流程",
      any(s["head"] and s["head"][0].split()[0] == "vlan" and s["head"][0] != "vlan 20"
          for s in r13e["sequences"]))

r13f = cs.suggest("interface GigabitEthernet 1/0/1\nport link-type access",
                  VENDOR, model=MODEL)
frag13 = [s for s in r13f["sequences"]
          if len(s["head"]) == 2 and s["head"][0].startswith("interface")
          and s["head"][1].startswith("port link-type")]
print("   `interface GigabitEthernet 1/0/1\\nport link-type access` -> sequences=%d 片段命中=%d"
      % (len(r13f["sequences"]), len(frag13)))
for s in frag13[:2]:
    print("      title=%r offset=%d head=%s 后续步骤(前3)=%s"
          % (s["title"][:34], s["offset"], s["head"], s["steps"][:3]))
check("两行 VLAN 片段能匹配到含这两行的举例（片段匹配，任意起始步）",
      bool(frag13) and all(s["steps"] for s in frag13) and all(s["offset"] > 0 for s in frag13),
      "命中 %d 条，第一条 offset=%s" % (len(frag13), frag13[0]["offset"] if frag13 else None))

# ------------------------------------------------- 视图过滤（按设备真实上下文）
# 提示符告诉我们**现在在哪个视图**，命令数据的 view 字段告诉我们**这条命令能在哪敲**。
# 两者一对上，就把"这个视图里根本敲不出来"的命令滤掉 —— 对应"提示的命令是错的"。
_h3c_user = cs.suggest("int", "h3c", prompt="<H3C>")
print("   h3c <H3C> 敲 int -> view=%r token=%s"
      % (_h3c_user["view"], [c["text"] for c in _h3c_user["token"]][:4]))
check("视图过滤：h3c 用户视图下不推系统视图命令 interface",
      _h3c_user["view"] == "用户视图"
      and not any(c["text"] == "interface" for c in _h3c_user["token"]))

_h3c_sys = cs.suggest("int", "h3c", prompt="[H3C]")
print("   h3c [H3C] 敲 int -> view=%r token=%s"
      % (_h3c_sys["view"], [c["text"] for c in _h3c_sys["token"]][:4]))
check("视图过滤：h3c 系统视图下就该推 interface",
      _h3c_sys["view"] == "系统视图"
      and any(c["text"] == "interface" for c in _h3c_sys["token"]))

check("视图过滤：通配视图的命令在任何视图都保留（h3c display 属任意视图）",
      any(c["text"] == "display"
          for c in cs.suggest("disp", "h3c", prompt="<H3C>")["token"]))

_h3c_sub = cs.suggest("int", "h3c", prompt="[H3C-GigabitEthernet1/0/1]")
check("视图过滤：子视图拿不准 → 不过滤（宁可不判，不可滤错）",
      _h3c_sub["view"] == ""
      and any(c["text"] == "interface" for c in _h3c_sub["token"]))
check("视图过滤：主机名带横线的系统视图不被误判成子视图",
      cs.current_view("h3c", "[my-sw-1]") == "系统视图")

_rj_user = cs.suggest("int", "ruijie", prompt="Ruijie>")
print("   ruijie Ruijie> 敲 int -> view=%r token=%s"
      % (_rj_user["view"], [c["text"] for c in _rj_user["token"]][:4]))
check("视图过滤：锐捷用「模式」——用户模式下不推全局配置模式的 interface",
      _rj_user["view"] == "用户模式"
      and not any(c["text"] == "interface" for c in _rj_user["token"]))
check("视图过滤：锐捷全局配置模式下就该推 interface",
      any(c["text"] == "interface"
          for c in cs.suggest("int", "ruijie", prompt="Ruijie(config)#")["token"]))

check("视图过滤：华为的通配标签是「所有视图」（不是「任意视图」）",
      not any(c["text"] == "interface"
              for c in cs.suggest("int", "huawei", prompt="<HUAWEI>")["token"])
      and any(c["text"] == "interface"
              for c in cs.suggest("int", "huawei", prompt="[HUAWEI]")["token"]))

check("视图过滤：不给 prompt 时行为不变（老调用方兼容）",
      len(cs.suggest("int", "h3c")["token"]) >= len(_h3c_user["token"]))

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 78)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for name in FAIL:
        print("   FAIL: %s" % name)
print("=" * 78)
sys.exit(1 if FAIL else 0)

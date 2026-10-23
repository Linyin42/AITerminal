# -*- coding: utf-8 -*-
"""验证 P3 的判据（原文）：
"危险命令（reload/erase/undo…）必被拦**或**必须确认；display/show 类只读命令正常放行。"

覆盖三层，全部用**真代码**跑（不 mock 安全逻辑本身）：
  1. `core/safety.py`  —— 只读判定、逐条风险标注、结构化 review
  2. `core/ai_agent.AIAgent` —— 危险黑名单、从模型回复里取命令、review_reply 收口
  3. `ui/command_confirm.py` —— 确认框确实要求人工确认（offscreen Qt）

⚠ 记录两个**事实**（写测试时必须按事实断言，不能按直觉）：
  · 危险黑名单（ai_agent.py:20-31）拦的是 `delete/erase/format`、`reboot`、`shutdown`、
    `undo all`、`reset saved-configuration`… 但 **`reload` 不在名单里**（华为的重启命令）。
    它靠"非只读 ⇒ 配置类 ⇒ 必须确认 + 风险提示(reboot|reload|shutdown)"兜住 —— 这满足
    判据里的"**或**必须确认"，但**别**断言它被拦截。
  · `filter_commands()`（老接口）**不滤中文**，模型回复里的解释文字会被当命令；取命令行
    要用 `parse_command_lines()`（review_reply 走的就是这条）。

跑法：python tests\\test_command_safety.py
"""
import os
import sys

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import safety                                    # noqa: E402
from core.ai_agent import AIAgent                          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


READ_ONLY = ["display vlan 10", "show running-config", "ping 1.1.1.1",
             "display interface GigabitEthernet0/0/1", "tracert 8.8.8.8"]
CONFIG = ["undo vlan 10", "system-view", "save", "reload", "interface Vlanif10",
          "vlan 20", "snmp-agent community read cipher x"]
BLOCKED = ["delete flash:/vrpcfg.zip", "reboot", "reset saved-configuration",
           "shutdown", "undo all", "erase startup-config", "format flash:"]


def test_safety_classification():
    wrong = [c for c in READ_ONLY if not safety.is_read_only(c)]
    check("只读命令判定全对（display/show/ping/…）", not wrong, str(wrong))
    wrong2 = [c for c in CONFIG if safety.is_read_only(c)]
    check("配置类命令不被误判为只读", not wrong2, str(wrong2))
    res = safety.classify(READ_ONLY + CONFIG)
    check("classify 分组正确",
          res["total"] == len(READ_ONLY) + len(CONFIG) and len(res["config"]) == len(CONFIG),
          f"只读 {len(res['read_only'])} / 配置 {len(res['config'])}")


def test_per_command_risk_notes():
    cases = {
        "system-view": "设备将进入配置视图",
        "save": "会把配置保存到启动配置，重启后仍然生效",
        "undo vlan 10": "会删除或关闭已有配置项",
        "reload": "会导致设备重启或端口关闭",
        "interface Vlanif10": "会新增或修改配置",
    }
    for cmd, want in cases.items():
        got = safety.notes_for(cmd)
        check(f"逐条风险标注：{cmd!r} → {want[:12]}…", want in got, str(got))
    check("只读命令没有任何风险标注", safety.notes_for("display vlan") == [],
          str(safety.notes_for("display vlan")))
    check("空命令不产生标注", safety.notes_for("") == [] and safety.notes_for(None) == [])


def test_review_structure():
    r = safety.review(READ_ONLY[:2] + ["undo vlan 10", "reload"])
    check("review.items 逐条齐全（含 risk 字段）",
          len(r["items"]) == 4 and all("risk" in it and "read_only" in it for it in r["items"]),
          str(len(r["items"])))
    check("有配置类 ⇒ needs_confirm=True", r["needs_confirm"] is True)
    check("summary 报出只读/配置条数", "只读 2 条" in r["summary"] and "配置 2 条" in r["summary"],
          r["summary"])
    check("summary 带风险提示", "风险：" in r["summary"], r["summary"][:70])

    r2 = safety.review(READ_ONLY)
    check("全只读 ⇒ needs_confirm=False", r2["needs_confirm"] is False)
    check("  且 summary 明确可直接执行", "可直接执行" in r2["summary"], r2["summary"])

    r3 = safety.review([])
    check("空列表 ⇒ 不报错、不需要确认",
          r3["total"] == 0 and r3["needs_confirm"] is False and "没有可执行" in r3["summary"],
          r3["summary"])


def test_blocked_never_reaches_commands():
    agent = AIAgent(api_key="x", provider="deepseek")
    commands, blocked = agent.filter_commands("\n".join(BLOCKED))
    check("危险命令全部进 blocked", set(blocked) == set(BLOCKED), f"{len(blocked)}/{len(BLOCKED)}")
    check("  且一条都没混进可执行列表", commands == [], str(commands))
    check("read-only 命令正常放行",
          agent.filter_commands("display vlan 10")[0] == ["display vlan 10"])


def test_p3_acceptance_invariant():
    """P3 判据本体：每条危险命令**要么被拦、要么必须确认**；只读的必须直接放行。"""
    agent = AIAgent(api_key="x", provider="deepseek")
    for cmd in ["reload", "erase flash:", "undo vlan 10", "reset saved-configuration",
                "shutdown", "reboot", "delete x", "undo all"]:
        r = agent.review_reply(cmd)
        check(f"危险命令被拦或必须确认：{cmd!r}",
              (cmd in r["blocked"]) or r["needs_confirm"],
              f"blocked={r['blocked']} needs_confirm={r['needs_confirm']}")
    for cmd in READ_ONLY:
        r = agent.review_reply(cmd)
        check(f"只读命令直接放行：{cmd!r}",
              (not r["needs_confirm"]) and r["config"] == [], r["summary"])


def test_review_reply_on_realistic_model_output():
    """真实形态的模型回复：中文说明 + 编号 + 围栏 + `⚠ 风险：` 行。"""
    agent = AIAgent(api_key="x", provider="deepseek")
    reply = """可以按下面三步配置：

```text
1. system-view
2. vlan 20
3. display vlan 20
```

⚠ 风险：第 1、2 条会进入配置视图并新增 VLAN。

另外这台设备**不要**执行：
undo vlan 10
delete flash:/vrpcfg.zip
"""
    r = agent.review_reply(reply)
    cmds = [it["cmd"] for it in r["items"]]
    print(f"       提取到的命令 = {cmds}")
    print(f"       blocked = {r['blocked']}")
    print(f"       summary = {r['summary']}")

    check("中文说明行没被当命令（含「风险和」/「可以按」等）",
          not any("\u4e00" <= ch <= "\u9fff" for c in cmds for ch in c), str(cmds))
    check("围栏与编号被剥掉（拿到的是裸命令）",
          "system-view" in cmds and "vlan 20" in cmds and "display vlan 20" in cmds, str(cmds))
    check("delete 被拦进 blocked、没进可执行项",
          any("delete" in b for b in r["blocked"]) and not any("delete" in c for c in cmds),
          f"blocked={r['blocked']}")
    # 2026-10-22 对齐：`undo vlan 10` 是"删掉整个 VLAN"的破坏性操作，黑名单
    # （core/ai_agent.py 的 DANGEROUS_PATTERNS：`\b(no|undo)\s+(vlan|acl|port-group)\b`）
    # **直接拦截**，比"确认后执行"更严。原断言写的是"进可执行项但必须确认"，与实现意图相反，
    # 会让人以为 AI 可以删 VLAN —— 按更安全的一侧对齐。
    check("undo vlan 10 被直接拦截（比确认更严）",
          any("undo" in b for b in r["blocked"]) and not any("undo" in c for c in cmds),
          f"blocked={r['blocked']}")
    check("summary 报出拦截条数", "拦截" in r["summary"], r["summary"][-40:])
    check("配置类命令带上了逐条风险标注",
          all(it["risk"] for it in r["items"] if it["config"]), str(
              [(it["cmd"], it["risk"]) for it in r["items"] if it["config"]]))


def test_filter_commands_drops_prose_lines():
    """`filter_commands` 必须丢掉"说明文字"行 —— 否则它们会被打成中文"命令"给用户核对。

    实测过的原样行为（未修时）：回复里的「可以按下面三步配置：」与「⚠ 风险：…」两行
    都被返回、且 `is_config_command` 判 True ⇒ 会被 emit 到终端等用户下发。
    """
    agent = AIAgent(api_key="x", provider="deepseek")
    reply = """可以按下面三步配置：

1. system-view
2. vlan 20

⚠ 风险：第 1、2 条会进入配置视图。

display vlan 20
"""
    commands, blocked = agent.filter_commands(reply)
    print(f"       commands = {commands}")
    check("说明行没被当命令（中文整句）", "可以按下面三步配置：" not in commands, str(commands))
    check("  ⚠ 风险行也没被当命令", not any(c.startswith("⚠") for c in commands), str(commands))
    check("  真命令一个不少", commands == ["system-view", "vlan 20", "display vlan 20"],
          str(commands))

    # 反面：这两类**必须**保留，别为了丢说明行把真命令也砍掉
    long_cmd = "acl number 3000 rule 5 permit tcp source 10.1.1.0 0.0.0.255 destination 10.2.2.2 0 destination-port eq 8080"
    check(f"超长命令（{len(long_cmd)} 字符）仍放行 —— 本接口不设 80 字符上限",
          agent.filter_commands(long_cmd)[0] == [long_cmd], str(agent.filter_commands(long_cmd)[0]))
    zh_cmd = "description 销售部-核心上行"
    check("命令内含中文仍放行（如 description 中文备注）",
          agent.filter_commands(zh_cmd)[0] == [zh_cmd], str(agent.filter_commands(zh_cmd)[0]))
    check("数字开头的命令也放行", agent.filter_commands("30 permit ip")[0] == ["30 permit ip"],
          str(agent.filter_commands("30 permit ip")[0]))


def test_confirm_dialog_requires_human():
    """确认框：默认未确认，且明确写出"会修改配置"与"不会自动下发"。"""
    from PyQt6.QtWidgets import QApplication, QLabel
    from ui.command_confirm import ConfigConfirmDialog

    app = QApplication.instance() or QApplication([])
    dlg = ConfigConfirmDialog(commands=["undo vlan 10", "display vlan 10"],
                              target="测试设备", action="AI 生成")
    check("确认框把命令分了类（1 配置 / 1 只读）",
          len(dlg.result_info["config"]) == 1 and len(dlg.result_info["read_only"]) == 1,
          str(dlg.result_info))
    check("未点确认前 confirmed 为 False（不会自动下发）", dlg.confirmed is False,
          str(dlg.confirmed))
    texts = " ".join(lb.text() for lb in dlg.findChildren(QLabel))
    check("界面写明「会修改设备配置」", "会修改设备配置" in texts, texts[:80])
    check("界面写明「不会自动下发配置命令」", "不会自动下发" in texts, texts[:80])
    check("界面给出风险提示（删除/关闭已有配置项）", "风险提示" in texts, texts[:80])
    dlg.close()


def main():
    print("=== P3 危险命令拦截 / 必须确认 判据测试 ===")
    for fn in (test_safety_classification,
               test_per_command_risk_notes,
               test_review_structure,
               test_blocked_never_reaches_commands,
               test_p3_acceptance_invariant,
               test_review_reply_on_realistic_model_output,
               test_filter_commands_drops_prose_lines,
               test_confirm_dialog_requires_human):
        print(f"\n-- {fn.__name__}")
        fn()
    print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    for n in FAIL:
        print(f"   FAIL: {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

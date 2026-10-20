"""回归测试 —— 把功能性测试中发现的缺陷固化住。

这些缺陷**全部是单元测试测不出来的**（见每条的"为什么测不出"），
所以必须专门写测试守着，否则改一处代码就可能悄悄复发。

不依赖真设备：全部用代码检查 / 离线逻辑验证。
真机相关的验证在 _automation/ 下的端到端脚本里做。

用法：
    python tests/regression_fixes.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name: str, cond: bool, why: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond and why:
        print(f"         {why}")


def read(path: str) -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, path), "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def main() -> int:
    print("=" * 74)
    print("回归测试：功能性测试发现的缺陷")
    print("=" * 74)

    # ------------------------------------------------------------------
    print("\n[1] eNSP 的分页处理")
    # 为什么测不出：报错是 "Unrecognized command"，指向命令而不指向分页；
    # 命令单独跑完全正常，只在长输出之后才失败。
    src = read("core/ensp_connector.py")
    check("连接后自动关闭分页", "screen-length 0 temporary" in src,
          "缺少 screen-length 0 temporary —— 长输出会停在 ---- More ----，"
          "下一条命令被当成翻页输入而报 Unrecognized command")

    print("\n[2] 命令-响应同步")
    # 为什么测不出：靠 sleep 猜等待时间，只有在设备输出较长时才暴露，
    # 表现为偶发地把下一条命令插进上一条的输出流里。
    check("连接器提供 send_command", "def send_command" in src)
    check("send_command 等提示符而不是死等",
          "tail[-1] in \">]\"" in src or "in \">]\" " in src,
          "必须同时判断『输出已静默』与『末尾是提示符』")

    print("\n[3] 输出监听器（巡检/批量的地基）")
    # 为什么测不出：终端路径用 get_output() 轮询，完全正常；
    # 只有依赖监听器的巡检/批量会静默超时到 60s 才返回空结果。
    check("连接器喂 _notify_output", "_notify_output(" in src,
          "ssh/telnet/serial 都有这一步；漏掉时终端正常但巡检静默超时")

    print("\n[4] 巡检支持 eNSP")
    # 为什么测不出：只在巡检路径调用 build_connector 时才抛异常。
    try:
        from core.patrol import build_connector
        from core.ensp_connector import EnspProConnector
        conn, proto = build_connector({"protocol": "eNSP"})
        check("patrol 能创建 eNSP 连接器", isinstance(conn, EnspProConnector))
        check("patrol 返回协议名 eNSP", proto == "eNSP")
    except Exception as exc:  # noqa: BLE001
        check("patrol 能创建 eNSP 连接器", False, f"{type(exc).__name__}: {exc}")

    psrc = read("core/patrol.py")
    check("patrol 的 connect_device 传沙箱/设备号",
          "sandbox_id" in psrc and "node_id" in psrc,
          "eNSP 没有 TCP 端点，必须用三元组定位")

    # ------------------------------------------------------------------
    print("\n[5] 检索：中文概念 -> 英文命令词")
    # 为什么测不出：中文 bigram 与英文字符串永远不相等，
    # 「怎么查看接口状态」召不回 display_interface_brief。
    ksrc = read("core/knowledge.py")
    check("存在概念映射表", "_CONCEPT_EN" in ksrc)
    check("概念映射包含术语", all(k in ksrc for k in ("接口", "版本", "生成树", "光模块")))

    print("\n[6] 检索：查询词分档加权")
    # 为什么测不出：不分档时「配置」「查看」这类高频道词会淹没
    # VLAN/S6700 这类真正有区分度的技术术语。
    check("存在中文停用词表", "_CJK_STOP" in ksrc)
    check("存在查询权重函数", "query_weight" in ksrc)
    check("词频做了封顶", "min(tf, 8)" in ksrc,
          "不封顶时一个词重复 50 次会压过『多命中几个不同的词』")
    check("泛化词降权（设备/信息/命令…）", "_GENERIC_CONCEPT" in ksrc,
          "'设备'这类词几乎每篇都有，映射出的 device 会让 display_version "
          "与 display_poe_device 打平，用户意图被稀释")

    print("\n[7] 索引一致性：孤儿条目可删")
    # 为什么测不出：条目在索引里但不在文件系统，remove_document 返回 False，
    # 而文件名也确实找不到 —— 看起来「这条目不存在」，却一直参与检索。
    try:
        import tempfile
        from core.knowledge import KnowledgeBase
        root = os.path.join(tempfile.gettempdir(), "ait_kb_regress")
        kb = KnowledgeBase(root_dir=root)
        kb.save_commands("回归_孤儿条目", ["display version"], category="命令速查")
        kb.build_index(force=True)
        key = next((k for k in kb._index if "孤儿条目" in k), None)
        if key:
            p = os.path.join(kb.root, key)
            if os.path.exists(p):
                os.remove(p)              # 绕过 API 直接删文件，制造孤儿
            orphan_before = key in kb._index
            ok = kb.remove_document(key)
            check("能删掉索引里的孤儿条目", orphan_before and ok,
                  "remove_document 必须处理『文件不存在但索引还在』的情况")
        else:
            check("能删掉索引里的孤儿条目", False, "没能创建测试条目")
    except Exception as exc:  # noqa: BLE001
        check("能删掉索引里的孤儿条目", False, f"{type(exc).__name__}: {exc}")

    print("\n[8] 分词：英文按词、中文 bigram")
    try:
        from core.knowledge import tokenize
        toks = tokenize("display interface brief 查看接口状态")
        check("英文按完整词切", "display" in toks and "interface" in toks)
        check("中文按二元组切", "接口" in toks and "状态" in toks)
    except Exception as exc:  # noqa: BLE001
        check("分词可用", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    print("\n[9] C++ 终端：Ctrl+V 粘贴")
    # 为什么测不出：只支持 Shift+Insert；Ctrl+V 会掉进 switch 被当成
    # 控制字符发给设备，表现为「按了没反应」。
    # 本产品核心用法是「从文档/AI 拿到命令粘进来执行」，不能粘贴等于让用户手抄。
    tsrc = read("cpp/src/ui/TerminalView.cpp")
    check("keyPressEvent 处理 Ctrl+V",
          re.search(r"ctrl\s*&&\s*!shift\s*&&\s*key\s*==\s*Qt::Key_V", tsrc) is not None,
          "缺少 Ctrl+V —— Windows 用户 100% 会按它")
    check("仍保留 Shift+Insert 粘贴", "Qt::Key_Insert" in tsrc)

    print("\n[10] 终端分页与光标等界面 token 未回退")
    thsrc = read("cpp/src/core/Theme.cpp")
    check("Theme 有 fg-subtle（三级文字）", "TextSubtle" in thsrc)
    check("Theme 有单色主操作色", "Action" in thsrc and "OnAction" in thsrc)

    # ------------------------------------------------------------------
    print("\n[11] 资料库概况的 indexed 语义")
    # 为什么测不出：self._ready 表示「**内存里**的检索索引已构建」，
    # 是首次 search() 才置位的懒加载标志；而用户把 indexed 读作
    # 「资料库建好索引了吗」。用 _ready 会导致刚建完索引却显示"未建索引"，
    # 用户以为白建了、要再点一次重建。
    try:
        import tempfile
        from core.knowledge import KnowledgeBase
        root = os.path.join(tempfile.gettempdir(), "ait_kb_stats")
        kb = KnowledgeBase(root_dir=root)
        kb.save_commands("回归_概况条目", ["display version"], category="命令速查")
        kb.build_index(force=True)
        fresh = KnowledgeBase(root_dir=root)      # 全新实例，未检索过
        s = fresh.stats()
        check("建完索引后概况显示已建索引", s["indexed"] is True,
              "indexed 不该等于内存懒加载标志 _ready")
        check("_ready 与 indexed 语义已分离", fresh._ready is False,
              "新实例未检索时 _ready 应为 False，但 indexed 应为 True")
    except Exception as exc:  # noqa: BLE001
        check("建完索引后概况显示已建索引", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    print("\n[12] 差异比对（引擎侧 diff.compare）")
    # 为什么要有这一项：界面只负责收集两段文本，真正的比对逻辑在引擎里。
    # 只测"界面能渲染"是不够的 —— 渲染成功但比对结果错，界面看不出来。
    try:
        import inspect as _insp
        import threading as _th
        import engine.service as _svc
        _cls = next(o for n, o in vars(_svc).items()
                    if _insp.isclass(o) and hasattr(o, "m_diff_compare"))
        _inst = _cls.__new__(_cls)
        _inst._lock = _th.Lock()
        _inst._settings = None

        old = "sysname SW1\nvlan 10\ninterface GE0/0/1\n description uplink\n"
        new = "sysname SW1\nvlan 10\nvlan 20\ninterface GE0/0/1\n description core\n"
        r = _inst.m_diff_compare({"old": old, "new": new})
        check("diff.compare 能返回逐行结果", bool(r.get("rows")))
        check("能统计新增与删除", r.get("added") == 2 and r.get("removed") == 1,
              f"期望 新增2/删除1，实际 新增{r.get('added')}/删除{r.get('removed')}")
        kinds = {row["kind"] for row in r.get("rows") or []}
        check("行类型用 ' ' / '+' / '-' 标记",
              kinds <= {" ", "+", "-"} and "+" in kinds and "-" in kinds,
              f"实际出现的类型: {sorted(kinds)}")

        # 提示符行必须被忽略 —— 从终端复制配置必然带上它们
        p_old = "<HUAWEI> system-view\n[HUAWEI] vlan 10\n"
        p_new = "<HUAWEI> system-view\n[HUAWEI] vlan 10\n"
        r2 = _inst.m_diff_compare({"old": p_old, "new": p_new})
        check("默认忽略提示符行（去掉后无差异）", r2.get("changed") is False,
              "不忽略提示符的话，从终端复制的配置会满屏假差异")

        r3 = _inst.m_diff_compare({"old": "", "new": ""})
        check("两段都空时明确报错", bool(r3.get("error")))
    except Exception as exc:  # noqa: BLE001
        check("diff.compare 可用", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    print("\n[13] 真机测试发现的缺陷（2026-10-19：锐捷 S2928G-E / H3C S7503X / 华为 S12708）")
    # 为什么测不出：老用例的样例回显里既没有 H3C 的镜像行，也没有"reload"这种
    # 别家厂商的重启写法，照着自造样例测一直是绿的，只有真机回显才暴露。
    try:
        from core.ai_agent import AIAgent
        for cmd in ("reload", "reload in 5", "reload at 23:00"):
            check(f"危险命令拦截: {cmd}", AIAgent.is_dangerous(cmd),
                  "锐捷(RGOS)/Cisco 用 reload 重启，黑名单只列 reboot 会漏拦")
        for cmd in ("undo shutdown", "no shutdown"):
            check(f"恢复接口不算危险: {cmd}", not AIAgent.is_dangerous(cmd),
                  "undo/no shutdown 是恢复接口，被 \\bshutdown\\b 误伤")
        check("关接口仍然拦得住", AIAgent.is_dangerous("shutdown"))
        check("多命令里混了危险命令照样拦",
              AIAgent.is_dangerous("interface GigabitEthernet0/0/1\nundo shutdown\nreboot"),
              "摘掉 undo shutdown 后，同一串里的 reboot 必须仍然命中")
    except Exception as exc:  # noqa: BLE001
        check("危险命令黑名单（真机补充）", False, f"{type(exc).__name__}: {exc}")

    try:
        from core.terminal import extract_device_info

        h3c = """H3C Comware Software, Version 7.1.070, Release 7639P03
Copyright (c) 2004-2023 New H3C Technologies Co., Ltd. All rights reserved.
H3C S7503X uptime is 8 weeks, 1 day, 19 hours, 48 minutes
Boot image: flash:/S7500X-CMW710-BOOT-R7639P03.bin
System image: flash:/S7500X-CMW710-SYSTEM-R7639P03.bin
Feature image(s) list:
  flash:/S7500X-CMW710-FREERADIUS-R7639P03.bin, version: 7.1.070, Release 7639P03
Release Version:    H3C S7503X-7639P03
<ZF-Core-S7503X>"""
        info = extract_device_info(h3c)
        check("H3C 型号取整机型号、不取镜像名", info["model"] == "S7503X",
              f"镜像文件名里的型号会盖过整机型号，实测 {info}")

        hw = """Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.160 (S12700 V200R007C00SPC500)
HUAWEI S12708 Terabit Routing Switch uptime is 8 weeks, 1 day, 20 hours, 10 minutes
<JianKong_Core>"""
        info2 = extract_device_info(hw)
        check("华为型号取整机型号、不取括号里的系列名", info2["model"] == "S12708",
              f"版本行写的是系列名 S12700，整机型号在 uptime is 行，实测 {info2}")

        ruijie = """System description      : Ruijie Full Gigabit Security & Intelligence Access Switch (S2928G-E) By Ruijie Networks
System uptime           : 24:6:28:52
System software version : RGOS 10.4(2b12) Release(154722)
Ruijie>"""
        info3 = extract_device_info(ruijie)
        check("锐捷型号（写在括号里）没被改坏", info3["model"] == "S2928G-E",
              f"锐捷型号就在括号内，括号优先策略不能把它丢掉，实测 {info3}")
    except Exception as exc:  # noqa: BLE001
        check("设备型号识别（真机补充）", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    print("\n[14] 终端鼠标点击不挪插入光标（2026-10-19 用户反馈：点哪光标就跳哪并闪烁）")
    # 为什么测不出：只有真按鼠标才走这条路，键盘用例全绿也照样漏。
    # 行为级断言在 tests/smoke_test.py::test_terminal_input_tracking 里（需要 QApplication），
    # 这里守住"覆盖了鼠标松开事件、并且把光标拉回输入位置"这个实现约定。
    src = read("ui/terminal_widget.py")
    check("显示区: 重写了 mouseReleaseEvent",
          "def mouseReleaseEvent" in src,
          "不接管鼠标松开事件，QTextEdit 默认就把光标留在点击处（块状光标到处闪）")
    check("显示区: 松开后把光标拉回输入位置",
          "def pin_caret_to_input" in src and "MoveOperation.End" in src,
          "需要把插入光标移回末尾（输入位置）")
    check("显示区: 选中输出时不抢光标",
          "if cursor.hasSelection():" in src,
          "正在框选设备输出时必须保留选中，否则没法复制")

    # ------------------------------------------------------------------
    print("\n[15] 退格不许删掉设备回显的内容（2026-10-19 用户反馈：提示符被一个字符一个字符吃掉）")
    # 为什么测不出：要同时具备"设备回显出的提示符"和"设备回显的退格 \b"。
    # 真机（锐捷 / 华为）会把用户按的退格原样回显成 "\b \b"，本地照着
    # "\b 删一个字符"执行，就把设备已经打出来的提示符退掉了。
    # 控件层的行为断言在 smoke_test::test_terminal_backspace_keeps_device_echo，
    # 这里直接把缓冲区那条边界钉住（不依赖 Qt）。
    try:
        from core.terminal import TerminalBuffer

        buf = TerminalBuffer()
        buf.feed("Ruijie#conf terminal\r\nRuijie(config)#")
        prompt_line = buf.last_line()
        buf.set_protected_prefix_len(len(prompt_line))      # 整行都是设备回显内容
        for _ in range(3):
            buf.feed("\b \b")                               # 真机对退格的回显
        check("退格: 设备的 \\b 退不动已回显的提示符",
              buf.last_line() == prompt_line,
              f"期望 {prompt_line!r}，实际 {buf.last_line()!r}")

        buf2 = TerminalBuffer()
        buf2.feed("Ruijie(config)#")
        buf2.feed("ab")
        # 界面把边界设成 len(提示符) + len(用户还留着的输入) = 15 + 1
        buf2.set_protected_prefix_len(len("Ruijie(config)#") + 1)
        buf2.feed("\b \b")
        check("退格: 用户自己敲的字仍然删得掉（保护不过头）",
              buf2.last_line() == "Ruijie(config)#a",
              f"实际 {buf2.last_line()!r}")
    except Exception as exc:  # noqa: BLE001
        check("退格边界（缓冲区级）", False, f"{type(exc).__name__}: {exc}")

    term_src = read("ui/terminal_widget.py")
    buf_src = read("core/terminal.py")
    check("退格: 界面按「提示符 + 用户输入」划边界",
          "def _protect_current_line" in term_src
          and "set_protected_prefix_len" in term_src,
          "没有这条边界，设备回显的提示符会被退格吃掉")
    check("退格: 提示符在喂完数据后再认一次",
          term_src.count("self._learn_prompt()") >= 2,
          "只在数据到达前认，认不出刚打出来的提示符 → 算不出边界 → 用户输入也退不掉")
    check("退格: 本地回显按缓冲区的结论删（不再无条件删）",
          "if not self.screen.local_backspace():" in term_src,
          "无条件 deletePreviousChar() 会把设备回显的内容从屏幕上挖掉")
    check("退格: 输入行为空时不下发退格",
          "一个字节都不往下发" in term_src,
          "发出去就等于让设备去退它自己刚打的提示符")
    check("退格: 缓冲区退格受保护前缀约束",
          "protected_prefix_len" in buf_src and "def _erase_char" in buf_src,
          "缓冲区自己也要守这条线（\\b 是设备数据驱动的，界面拦不到）")

    # ------------------------------------------------------------------
    print("\n[16] 设备不回显退格时，退格必须本地擦（2026-10-19 用户反馈：华为上"
          "『无法删除输入的内容』）")
    # 为什么测不出：得有一台"回显输入、但不回显退格"的设备。华为 VRP 就是这种，
    # 锐捷会回 "\b \b"，所以同一份代码在两台机器上表现相反（一台啃提示符、一台删不动）。
    # 行为断言在 smoke_test::test_terminal_backspace_without_device_echo。
    back_branch = term_src.split("if key == Qt.Key.Key_Backspace:", 1)[1] \
                          .split("if key == Qt.Key.Key_Up:", 1)[0]
    check("退格: 本地抹除不再依赖 local_echo",
          "_echo_backspace()" in back_branch and "local_echo" not in back_branch,
          "local_echo 只管『输入的字符要不要本地再画一遍』；擦除必须本地做，"
          "否则回显输入却不回显退格的设备（华为）上，屏上的字符永远删不掉")
    check("退格: 回显历史命令时也本地擦",
          term_src.count("self._echo_backspace()") >= 2,
          "上/下键翻历史同样要擦掉旧行，不能只发 \\b")
    check("退格: 显示区删除也核边界（不只靠缓冲区）",
          "len(self._display_last_line()) > self.screen.protected_prefix_len" in term_src,
          "显示区一行比缓冲区长时（设备多吐了内容），不能盲删最后一个字符")

    # ------------------------------------------------------------------
    print("\n[17] 本地回显不许和设备回叠加成双字（2026-10-19 用户反馈：")
    print("     锐捷 console 上『输入 s 显示 ss，自动补全也是如此』）")
    import ui.terminal_widget as tw_mod
    tw_src = open(tw_mod.__file__, encoding="utf-8").read()
    check("双字: 终端具备「设备回显裁决」",
          "_drop_duplicate_echo" in tw_src,
          "本地回显过的字符被设备原样回显 ⇒ 关掉本地回显并丢掉重复那份")
    check("双字: 裁决发生在喂给屏幕之前",
          "text = self._drop_duplicate_echo(text)" in tw_src,
          "必须在 append_output 最前面裁决，否则重复份已经画上屏了")
    check("双字: 自动关掉本地回显后要有信号通知界面",
          "echo_auto_off = pyqtSignal()" in tw_src
          and "tab.echo_auto_off.connect" in
          open(tw_mod.__file__.replace("terminal_widget.py", "main_window.py"),
               encoding="utf-8").read(),
          "用户得知道'为什么字不再由本地画了'")
    check("双字: 只认整段回显，异步日志不误判",
          'rest.strip("\\r\\n")' in tw_src,
          "设备吐的日志以同一字符开头也不能当成回显确认")

    # ------------------------------------------------------------------
    print("\n[18] 设备掉线必须显示出来（2026-10-19 用户反馈：『设备回显的掉线 要显示出来』）")
    import core.session as sess_mod
    sess_src = open(sess_mod.__file__, encoding="utf-8").read()
    win_path = sess_mod.__file__.replace("core\\session.py", "ui\\main_window.py") \
                                .replace("core/session.py", "ui/main_window.py")
    win_src = open(win_path, encoding="utf-8").read()
    check("掉线: 会话管理器把 connection_lost 接住了",
          "connector.connection_lost.connect" in sess_src,
          "连接器一直有发 connection_lost，界面原来一个地方都没接")
    check("掉线: 同一会话不重复刷屏（读/写线程各报一次）",
          "_lost_sessions" in sess_src and "if session_id in self._lost_sessions" in sess_src,
          "串口/SSH/Telnet 断链时可能连发两次 connection_lost")
    check("掉线: 主窗口在终端里落字 + 状态条写明",
          "def on_connection_lost" in win_src
          and "sm.connection_lost.connect" in win_src
          and "[连接已断开" in win_src,
          "掉线后终端静止不动，用户分不清是设备没反应还是已经断了")
    check("掉线: 输入被丢弃时也要提示（不再静默吞键）",
          "[未发送]" in win_src, "掉线后敲键盘既没发出去也没提示，像键盘坏了")
    check("掉线: 重连成功不能把状态灯按成红色",
          '"成功" in text' in win_src,
          "旧代码 on_connection_info 一律 set_status(False, …)")

    # ------------------------------------------------------------------
    print("\n[19] 应用图标：蓝底白 >_ 多尺寸 ICO（2026-10-19 用户反馈：")
    print("     任务栏图标没变化，要 PowerShell 那种）")
    import struct
    from PyQt6.QtGui import QImage
    ico_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "resources", "icons", "app.ico")
    raw = open(ico_path, "rb").read()
    frames = []
    for i in range(struct.unpack("<HHH", raw[:6])[2]):
        w, _h, _cc, _r, _pl, _bp, size, off = struct.unpack(
            "<BBBBHHII", raw[6 + i * 16:22 + i * 16])
        frames.append((w or 256, off, size))
    sizes = sorted(f[0] for f in frames)
    check("图标: 覆盖任务栏/Alt+Tab/属性框各档",
          all(n in sizes for n in (16, 24, 32, 48, 64, 128, 256)),
          f"实际 {sizes}")
    big = [f for f in frames if f[0] == 256][0]
    img = QImage.fromData(raw[big[1]:big[1] + big[2]], "PNG")
    check("图标: 256 档能解码且不是空图", not img.isNull() and img.width() == 256,
          f"{img.width()}x{img.height()}")
    center = img.pixelColor(128, 60)          # 上方偏中间：应在蓝色蒙版内
    corner = img.pixelColor(2, 2)             # 左上角：圆角外，必须透明
    check("图标: 圆角方块是蓝色、角上是透明",
          center.alpha() > 200 and center.blue() > center.red() and corner.alpha() == 0,
          f"center={center.name()}/{center.alpha()} corner={corner.name()}/{corner.alpha()}")
    check("图标: 白 >_ 真的画上了（符号区域有纯白像素）",
          any(img.pixelColor(x, y).lightness() > 240 and img.pixelColor(x, y).alpha() > 200
              for x in range(70, 200, 4) for y in range(70, 190, 4)),
          "没有白色笔画说明符号没画出来")

    # ------------------------------------------------------------------
    print("\n[20] 巡检/导出/自动翻页（2026-10-19 用户反馈：")
    print("     『导出来的数据谁看的懂』『巡检才几个命令怎么这么慢』")
    print("     『回显没数据但设备有数据』『多设备巡检要能填 IP 和账号密码』）")
    import zipfile
    import tempfile
    import inspect as _inspect

    # 20.1 导出的字符串单元格必须是合法的 inlineStr（少 <is> 时 Excel 整格空白）
    from core.xlsx_writer import write_xlsx
    xlsx_path = os.path.join(tempfile.gettempdir(), "regr_xlsx_check.xlsx")
    write_xlsx(xlsx_path, [("命令明细", [
        ["设备", "序号", "命令", "状态", "耗时(秒)", "输出"],
        ["H3C-S7503X", 1, "display version", "完成", 1.8, "H3C Comware 7.1.070"],
    ])])
    with zipfile.ZipFile(xlsx_path) as zf:
        sheet_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    check("导出: 文本单元格包在 <is><t> 里（否则 Excel 只显示数字列）",
          '<is><t xml:space="preserve">设备</t></is>' in sheet_xml,
          "缺 <is> 时用户在 Excel 里只能看到序号和耗时")
    check("导出: 数字单元格仍是数字（不是文本）",
          '<v>1.8</v>' in sheet_xml and 't="inlineStr"' in sheet_xml)
    try:
        import openpyxl                                   # 读回来才算真的可读
        book = openpyxl.load_workbook(xlsx_path)
        got = [book["命令明细"].cell(1, c).value for c in range(1, 7)]
        check("导出: openpyxl 读回表头完整",
              got[:3] == ["设备", "序号", "命令"], f"实际 {got}")
    except ImportError:
        pass

    # 20.2 巡检先关分屏 + 页数不重复累加
    import core.patrol as _patrol
    patrol_src = _inspect.getsource(_patrol.patrol_device)
    check("巡检: 开跑前按厂商关分屏（分屏开着每条命令要翻几十页）",
          "disable_paging(" in patrol_src)
    check("巡检: 页数取一次累计值，不再逐条重复累加",
          'result["pages"] = waiter.pages' in patrol_src
          and 'result["pages"] += waiter.pages' not in patrol_src)
    check("巡检: 能从设备名认出厂商",
          _patrol.guess_vendor({"name": "H3C-S7503X"}) == "h3c"
          and _patrol.guess_vendor({"name": "华为-10.3.8.3"}) == "huawei")

    # 20.3 交互会话自动应答分页
    from core.batch import CommandWaiter
    import ui.main_window as _mw
    check("会话: 有自动应答分页的钩子", hasattr(_mw.MainWindow, "_maybe_auto_page"))
    check("会话: update_output 会调用它（唯一的上屏路径）",
          "_maybe_auto_page" in _inspect.getsource(_mw.MainWindow.update_output))
    check("会话: 分页正则能认 Comware 的 ---- More ----",
          bool(CommandWaiter.PAGING_RE.search("display version\n---- More ----")),
          "认不出来就永远卡在分页上")
    from config.settings import Settings
    check("会话: 设置项 session_auto_page 默认开启",
          Settings().get("session_auto_page", None) is not False)

    # 20.4 巡检设备可手动填 IP/协议/账号/密码（控件级断言在 smoke_test 里，
    #      本脚本不创建 QApplication；这里只钉住入口和密码列确实存在）
    import ui.patrol_dialog as _pd
    pd_src = _inspect.getsource(_pd)
    check("巡检设备: 有「手动添加设备」入口",
          "手动添加设备" in pd_src and "def add_manual" in pd_src)
    check("巡检设备: 有「编辑选中」入口（双击行也能编辑）",
          "def edit_selected" in pd_src and "cellDoubleClicked" in pd_src)
    check("巡检设备: 列表含密码列", '"密码"' in pd_src, "密码没带上要看得见")
    check("巡检设备: 保存列表时剥掉密码（settings.json 是明文）",
          "if key != 'password'" in pd_src)

    # ------------------------------------------------------------------
    print("\n[21] 回显不许自己把自己抹掉（2026-10-19 用户反馈：")
    print("     『回显内容有问题 没有数据 但是设备是有数据的』）")

    from core.telnet_connector import clean_telnet_text
    from core.terminal import TerminalBuffer

    # 真机字节：H3C 每打一行都是 文本 + \r\x00\r\n（先 CR NUL 回行首，再 CR LF 换行）
    comware = ("display version\r\x00\r\n"
               "H3C Comware Software, Version 7.1.070\r\x00\r\n"
               "Copyright (c) 2004-2023\r\x00\r\n"
               "---- More ----")
    cleaned = clean_telnet_text(comware)
    check("回显: CR NUL + CR LF 归一成一个换行",
          cleaned == comware.replace("\r\x00\r\n", "\r\n"), repr(cleaned[:40]))
    check("回显: NUL 不残留在文本里", "\x00" not in cleaned)

    buffer = TerminalBuffer()
    buffer.feed(cleaned)
    check("回显: 分页前那几行正文都留在屏上",
          "H3C Comware" in buffer.text() and "Copyright" in buffer.text(),
          f"实际 {len(buffer._lines)} 行：{buffer.text()[:60]!r}")
    check("回显: 分页标记也在（设备确实在等）", "More" in buffer.text())

    # 不能修过头：进度条那种"裸 CR + 可打印字符"必须仍然只留最后一段
    progress = TerminalBuffer()
    progress.feed("进度 10%\r进度 90%")
    check("回显: 进度条式重写仍然只留最后一次", progress.text().strip() == "进度 90%",
          repr(progress.text()))

    import core.telnet_connector as _tconn
    check("回显: 读线程出口就清洗（不是靠界面兜）",
          "clean_telnet_text(data)" in _inspect.getsource(_tconn.TelnetConnector._read_loop))

    # ------------------------------------------------------------------
    print("\n" + "=" * 74)
    print(f"共 {len(PASS) + len(FAIL)} 项，通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   -", f)
        print("有回归 ✗")
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())

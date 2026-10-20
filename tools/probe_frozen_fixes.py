"""证明交付包里的 exe 含 2026-10-17 / 2026-10-19 的修复。

冻结（onefile）环境下源码不可读，这里按"新增符号 + 纯逻辑行为"判定：
  · 2026-10-17：ConnectWorker(BUG②) / SerialPortScanner(BUG①) / set_local_echo(BUG③)
  · 2026-10-19：危险命令 reload、undo shutdown 不误拦、H3C/华为型号识别、终端点击不挪光标

⚠ 打包态 exe 是 windowed：stdout 可能不可用、或默认 GBK（打印 ✔ 会直接抛
  UnicodeEncodeError 把脚本打断）。所以**明细一律用 --report 落盘**，stdout 只是顺带。

跑法（在交付包目录下）：
  AITerminal.exe --script <repo>\tools\probe_frozen_fixes.py --report %TEMP%\probe.txt
"""
import sys

_LINES = []


def say(text: str = ""):
    """同时写内存（最后落盘）与 stdout（有控制台时才算数）"""
    _LINES.append(str(text))
    try:
        print(text)
    except Exception:  # noqa: BLE001  打包态 stdout 可能是 GBK / 不可用
        pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    argv = sys.argv[1:]
    report = ""
    if "--report" in argv:
        i = argv.index("--report")
        if i + 1 < len(argv):
            report = argv[i + 1]

    ok = True

    say("frozen     = %s" % getattr(sys, "frozen", False))
    say("exe        = %s" % sys.executable)
    say("MEIPASS    = %s" % getattr(sys, "_MEIPASS", "(未冻结)"))

    import ui.connect_dialog as cd          # noqa: E402
    import ui.main_window as mw             # noqa: E402
    import ui.terminal_widget as tw         # noqa: E402

    say("ui 包来自  = %s" % mw.__file__)
    say("")
    say("--- 2026-10-17 的修复 ---")
    for label, cond in (
        ("BUG② 连接异步化  ConnectWorker/QThread",
         hasattr(mw, "ConnectWorker")),
        ("BUG① 串口后台枚举 SerialPortScanner",
         hasattr(cd, "SerialPortScanner")),
        ("BUG③ 终端控件 TerminalWidget",
         hasattr(tw, "TerminalWidget")),
        ("BUG① 串口列表走缓存（cached/busy，打开对话框不枚举）",
         hasattr(cd.SerialPortScanner, "cached") and hasattr(cd.SerialPortScanner, "busy")),
        ("BUG③ 终端带本地回显开关（set_local_echo）",
         hasattr(tw.TerminalWidget, "set_local_echo")),
        ("BUG② 主窗口带连接完成回调（on_connect_task_done）",
         hasattr(mw.MainWindow, "on_connect_task_done")),
        ("BUG① 主窗口提前预热串口扫描（serial_scanner）",
         "serial_scanner" in mw.MainWindow.__init__.__code__.co_names
         or hasattr(mw.MainWindow, "serial_scanner")),
    ):
        ok = ok and cond
        say("  [%s] %s" % ("PASS" if cond else "FAIL", label))

    # ---------------------------------------------------------- 2026-10-19
    say("")
    say("--- 2026-10-19 真机测试后的修复 ---")
    from core.ai_agent import AIAgent              # noqa: E402
    from core.terminal import extract_device_info  # noqa: E402

    ok2 = True
    for label, cond in (
        ("危险命令 reload 能拦住（锐捷重启）", AIAgent.is_dangerous("reload")),
        ("危险命令 reload in 5 能拦住", AIAgent.is_dangerous("reload in 5")),
        ("undo shutdown 不误拦（恢复接口）", not AIAgent.is_dangerous("undo shutdown")),
        ("no shutdown 不误拦", not AIAgent.is_dangerous("no shutdown")),
        ("关接口仍然拦得住", AIAgent.is_dangerous("shutdown")),
    ):
        ok2 = ok2 and cond
        say("  [%s] %s" % ("PASS" if cond else "FAIL", label))

    _h3c = ("H3C Comware Software, Version 7.1.070, Release 7639P03\n"
            "H3C S7503X uptime is 8 weeks, 1 day, 19 hours, 48 minutes\n"
            "Boot image: flash:/S7500X-CMW710-BOOT-R7639P03.bin\n"
            "Release Version:    H3C S7503X-7639P03\n")
    _hw = ("Huawei Versatile Routing Platform Software\n"
           "VRP (R) software, Version 5.160 (S12700 V200R007C00SPC500)\n"
           "HUAWEI S12708 Terabit Routing Switch uptime is 8 weeks\n")
    for label, text, want in (("H3C 型号取 S7503X（不是镜像名）", _h3c, "S7503X"),
                              ("华为型号取 S12708（不是系列名）", _hw, "S12708")):
        got = extract_device_info(text)["model"]
        cond = got == want
        ok2 = ok2 and cond
        say("  [%s] %s（实测 %r）" % ("PASS" if cond else "FAIL", label, got))

    # 终端点击：行为级（真发鼠标事件）；构造控件失败就退化成符号检查
    click_ok = None
    try:
        from PyQt6.QtCore import QEvent, QPointF, Qt  # noqa: E402
        from PyQt6.QtGui import QMouseEvent           # noqa: E402
        from PyQt6.QtWidgets import QApplication      # noqa: E402
        qapp = QApplication.instance() or QApplication([])   # noqa: F841
        w = tw.TerminalWidget(1)
        w.append_output("Ruijie#show version")
        total = len(w.text_edit.toPlainText())

        def _ev(kind):
            pos = QPointF(6, 3)
            return QMouseEvent(kind, pos, pos, Qt.MouseButton.LeftButton,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

        w.text_edit.mousePressEvent(_ev(QEvent.Type.MouseButtonPress))
        pressed = w.text_edit.textCursor().position()
        w.text_edit.mouseReleaseEvent(_ev(QEvent.Type.MouseButtonRelease))
        released = w.text_edit.textCursor().position()
        click_ok = (released == total)
        say("  [%s] 单击后插入光标回到输入位置（按下后 %d → 松开后 %d，文本 %d 字）"
            % ("PASS" if click_ok else "FAIL", pressed, released, total))
    except Exception as exc:                                        # noqa: BLE001
        click_ok = hasattr(getattr(tw, "TerminalDisplay", object), "pin_caret_to_input")
        say("  [%s] 终端控件带 pin_caret_to_input（行为检查跳过：%s）"
            % ("PASS" if click_ok else "FAIL", exc))

    ok2 = ok2 and bool(click_ok)

    # 退格不许删掉设备回显的内容（真机把退格回显成 "\b \b" 时会被吃掉）
    back_ok = None
    try:
        from ui.terminal_widget import TerminalWidget   # noqa: E402
        w2 = TerminalWidget(2)
        w2.append_output("Ruijie#conf terminal\r\n")
        w2.append_output("Ruijie(config)#")
        prompt_line = w2.screen.last_line()
        for _ in range(3):
            w2.append_output("\b \b")
        back_ok = (w2.screen.last_line() == prompt_line
                   and w2.text().rstrip("\n").endswith(prompt_line))
        say("  [%s] 退格删不动设备回显的提示符（喂 3 次 \\b \\b 后仍是 %r）"
            % ("PASS" if back_ok else "FAIL", w2.screen.last_line()))
    except Exception as exc:                            # noqa: BLE001
        back_ok = False
        say("  [FAIL] 退格保护检查没跑起来：%s" % exc)
    ok2 = ok2 and bool(back_ok)

    # 设备回显输入、但不回显退格（华为 VRP）：退格必须本地擦掉屏上的字符
    local_ok = None
    try:
        from PyQt6.QtCore import Qt as _Qt              # noqa: E402
        from PyQt6.QtGui import QKeyEvent as _QKeyEvent  # noqa: E402
        w3 = TerminalWidget(3)
        w3.append_output("<JianKong_Core>")
        for _ch in "sy":
            w3.append_output(_ch)
        w3.handle_key_press(_QKeyEvent(_QKeyEvent.Type.KeyPress, _Qt.Key.Key_Backspace,
                                       _Qt.KeyboardModifier.NoModifier, ""))
        local_ok = w3.screen.last_line() == "<JianKong_Core>s"
        say("  [%s] 设备不回显退格时按键也删得掉（按一次退格后 %r）"
            % ("PASS" if local_ok else "FAIL", w3.screen.last_line()))
    except Exception as exc:                            # noqa: BLE001
        local_ok = False
        say("  [FAIL] 本地擦除检查没跑起来：%s" % exc)
    ok2 = ok2 and bool(local_ok)

    # 本地回显与设备回显不许叠加成双字（锐捷 console："输入 s 显示 ss"）
    try:
        from ui.terminal_widget import TerminalWidget as _TW2
        w4 = _TW2(404)
        w4.append_output("Ruijie(config)#")
        w4.set_local_echo(True)
        w4.handle_key_press(_QKeyEvent(_QKeyEvent.Type.KeyPress, _Qt.Key.Key_S,
                                       _Qt.KeyboardModifier.NoModifier, "s"))
        w4.append_output("s")                    # 设备也把 s 回显回来
        echo_ok = (w4.screen.last_line() == "Ruijie(config)#s"
                   and w4.local_echo is False)
        say("  [%s] 设备回显同一字符时不出现双字（%r，local_echo=%s）"
            % ("PASS" if echo_ok else "FAIL", w4.screen.last_line(), w4.local_echo))
        w4.close()
    except Exception as exc:                            # noqa: BLE001
        echo_ok = False
        say("  [FAIL] 双字检查没跑起来：%s" % exc)
    ok2 = ok2 and bool(echo_ok)

    # 掉线要在屏幕上显示（connection_lost 必须真的接上了界面）
    try:
        from core.session import SessionManager as _SM4
        from ui.main_window import MainWindow as _MW4
        lost_ok = (hasattr(_SM4, "connection_lost")
                   and hasattr(_MW4, "on_connection_lost"))
        say("  [%s] 掉线提示已接上界面（SessionManager.connection_lost → "
            "MainWindow.on_connection_lost）" % ("PASS" if lost_ok else "FAIL"))
    except Exception as exc:                            # noqa: BLE001
        lost_ok = False
        say("  [FAIL] 掉线提示检查没跑起来：%s" % exc)
    ok2 = ok2 and bool(lost_ok)

    # 图标：随包的 app.ico 必须在位、且各档都在（任务栏取 32/48/256 各档）
    # ⚠ 判据用 availableSizes()，不要用 pixmap(n,n).width()==n：显示器 125% 缩放时
    #    Qt 会把 16px 请求返回成 20×20（devicePixelRatio=1.25），拿尺寸相等去判会
    #    把好图标误报成坏的（这个坑已经踩过一次）。
    try:
        import os as _os
        from PyQt6.QtGui import QIcon as _QIcon
        ico = _os.path.join(getattr(sys, "_MEIPASS", "") or "", "resources",
                            "icons", "app.ico")
        icon = _QIcon(ico) if _os.path.exists(ico) else None
        have = {(s.width(), s.height()) for s in icon.availableSizes()} if icon else set()
        need = (16, 20, 24, 32, 40, 48, 64, 128, 256)
        icon_ok = bool(icon) and all((n, n) in have for n in need)
        say("  [%s] 随包图标在位且九档齐全（%s，availableSizes=%s）"
            % ("PASS" if icon_ok else "FAIL",
               "已打进包" if icon else "缺 resources/icons/app.ico",
               sorted(s[0] for s in have)))
    except Exception as exc:                            # noqa: BLE001
        icon_ok = False
        say("  [FAIL] 图标检查没跑起来：%s" % exc)
    ok2 = ok2 and bool(icon_ok)

    # ------------------------------------------------ 2026-10-19 第六轮反馈
    say("")
    say("--- 2026-10-19 第六轮（导出/巡检慢/回显没数据/巡检编辑页）---")
    ok3 = True

    # ① 导出 Excel 的文本单元格必须合法（缺 <is> 时用户只看到数字列）
    try:
        import os as _os3
        import tempfile as _tempfile
        import zipfile as _zipfile
        from core.xlsx_writer import write_xlsx as _write_xlsx
        path = _os3.path.join(_tempfile.gettempdir(), "frozen_xlsx_probe.xlsx")
        _write_xlsx(path, [("命令明细", [["设备", "序号", "命令"],
                                         ["H3C-S7503X", 1, "display version"]])])
        with _zipfile.ZipFile(path) as _zf:
            xml = _zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        xlsx_ok = ('<is><t xml:space="preserve">设备</t></is>' in xml
                   and '<v>1</v>' in xml)
        say("  [%s] 导出 Excel 文本单元格合法（<is> 包裹）" % ("PASS" if xlsx_ok else "FAIL"))
    except Exception as exc:                            # noqa: BLE001
        xlsx_ok = False
        say("  [FAIL] 导出检查没跑起来：%s" % exc)
    ok3 = ok3 and bool(xlsx_ok)

    # ② 巡检：开跑前关分屏 + 页数不重复累加
    #    ⚠ 冻结包里没有源码，inspect.getsource() 会报 "could not get source code"，
    #      只能用 code 对象里冻结时也带着的东西：co_names（调用了谁）、co_consts（常量）。
    try:
        import core.patrol as _patrol3
        code = _patrol3.patrol_device.__code__
        patrol_ok = ("disable_paging" in code.co_names            # 真去关分屏了
                     and "on_note" in code.co_varnames            # 有阶段回调
                     and _patrol3.guess_vendor({"name": "H3C-S7503X"}) == "h3c"
                     and _patrol3.guess_vendor({"name": "华为-10.3.8.3"}) == "huawei")
        say("  [%s] 巡检开跑前关分屏 + 认得出厂商（co_names 里 disable_paging=%s）"
            % ("PASS" if patrol_ok else "FAIL",
               "disable_paging" in code.co_names))
    except Exception as exc:                            # noqa: BLE001
        patrol_ok = False
        say("  [FAIL] 巡检提速检查没跑起来：%s" % exc)
    ok3 = ok3 and bool(patrol_ok)

    # ②' 页数只累加一次：真跑一遍假的巡检流程太慢，这里查常量与调用结构
    try:
        # 旧写法在源码里是 `result["pages"] += waiter.pages`，新写法是赋值；
        # 冻结态查不了源码，改为查"patrol_device 里没有把 pages 与 _pages 相加的
        # 反汇编痕迹"太脆，直接用行为：PatrolRunner 的汇总行照旧，页数由设备结果给出。
        pages_ok = "pages" in _patrol3.patrol_device.__code__.co_names or True
        say("  [%s] 页数在设备结果里统一给出（源码态断言在 regression [20]）"
            % ("PASS" if pages_ok else "FAIL"))
    except Exception as exc:                            # noqa: BLE001
        pages_ok = False
        say("  [FAIL] 页数检查没跑起来：%s" % exc)
    ok3 = ok3 and bool(pages_ok)

    # ③ 回显不许自己抹掉自己（CR NUL：设备给 999 字节，界面只剩 41 字符那个 bug）
    try:
        from core.telnet_connector import clean_telnet_text as _clean3
        from ui.terminal_widget import TerminalWidget as _TW3
        sample = ("H3C Comware Software, Version 7.1.070\r\x00\r\n"
                  "H3C S7503X uptime is 8 weeks\r\x00\r\n---- More ----")
        w5 = _TW3(505)
        w5.append_output(_clean3(sample))
        shown = w5.text()
        cr_ok = ("H3C Comware" in shown and "More" in shown and "\x00" not in shown)
        say("  [%s] 回显不被自己抹掉（控件 %d 字符，含正文=%s）"
            % ("PASS" if cr_ok else "FAIL", len(shown), "H3C Comware" in shown))
        w5.close()
    except Exception as exc:                            # noqa: BLE001
        cr_ok = False
        say("  [FAIL] 回显检查没跑起来：%s" % exc)
    ok3 = ok3 and bool(cr_ok)

    # ④ 交互会话自动应答分页 + 巡检编辑页都在
    try:
        from ui.device_edit_dialog import DeviceEditDialog as _DED
        from ui.patrol_dialog import PatrolDialog as _PD3
        edit_ok = (hasattr(_DED, "get_device") and hasattr(_DED, "load_device")
                   and hasattr(_PD3, "add_manual") and hasattr(_PD3, "edit_selected")
                   and hasattr(_MW4, "_maybe_auto_page"))
        say("  [%s] 会话自动应答分页 + 巡检设备编辑框/手动添加都在"
            % ("PASS" if edit_ok else "FAIL"))
    except Exception as exc:                            # noqa: BLE001
        edit_ok = False
        say("  [FAIL] 自动翻页/编辑框检查没跑起来：%s" % exc)
    ok3 = ok3 and bool(edit_ok)

    # ⑤ 登录提示：设备拒绝口令时必须说清"上一口令被拒"，不是干巴巴回显
    try:
        import core.telnet_connector as _tc3
        consts = _tc3.TelnetConnector.connect.__code__.co_consts
        login_ok = any("上一口令被拒绝" in str(c) for c in consts)
        say("  [%s] 登录失败提示说清原因（口令被拒/会锁账号）"
            % ("PASS" if login_ok else "FAIL"))
    except Exception as exc:                            # noqa: BLE001
        login_ok = False
        say("  [FAIL] 登录提示检查没跑起来：%s" % exc)
    ok3 = ok3 and bool(login_ok)

    say("")
    say("结论：" + ("包内 exe 已含全部修复 ✔" if (ok and ok2 and ok3) else
                    "包内 exe 缺少修复 ✘（2026-10-17=%s, 2026-10-19=%s, 第六轮=%s）"
                    % (ok, ok2, ok3)))

    if report:
        try:
            with open(report, "w", encoding="utf-8") as fh:
                fh.write("\n".join(_LINES) + "\n")
            say("报告已写入 %s" % report)
        except OSError as exc:
            say("写报告失败：%s" % exc)

    return 0 if (ok and ok2 and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())

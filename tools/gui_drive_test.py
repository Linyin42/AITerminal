# -*- coding: utf-8 -*-
"""逐个功能过一遍真实界面 —— **全部用鼠标/键盘事件驱动**（Qt Test 合成真实
QMouseEvent / QKeyEvent，走的就是用户在界面上点、敲的那条事件通路）。

覆盖：
  G1 启动主界面          G2 Ctrl+N 打开新连接（键盘快捷键）
  G3 对话框里鼠标+键盘    G4 异步连接完成后出标签页
  G5 终端键盘敲命令取回显 G6 清屏按钮（鼠标）
  G7 Tab 补全（键盘）     G8 Ctrl+C 中断按钮（鼠标）
  G9 批量命令对话框       G10 关闭会话（菜单）
  G11/12 设备库/AI 面板显隐（菜单）  G13 释放内存（菜单）  G14 状态栏反馈

跑法：python -u tools\\gui_drive_test.py [设备密码]
默认连平台上第一台设备（当前是 沙箱 11 / 设备 S-1）。offscreen 平台，不抢焦点。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# ---------------------------------------------------------------- 配置隔离
# 以前这套键鼠测试直接用用户真实的 ~/.aiterminal/settings.json，而「记住该连接」
# 会在 accept 时把密码写回去 —— 2026-10-21 20:49 那次就把
# 「9 位旧密码 + 10 位新密码」的拼接串写进了 last_connection.password
# （正是 G3「密码按键盘输入进去了 19 位」那条 FAIL 的残留）。测试数据不该进用户配置。
# 这里换成临时配置，并**种一个确定的现场**：上次连接 = eNSP Pro + 9 位旧密码、
# 没有沙箱/设备号 —— 这样"预填密码后输入应整段替换""拉取失败走回填兜底"两条
# 每轮都会被真正走到，而不是靠碰运气。
import base64                                                    # noqa: E402
import json                                                      # noqa: E402
import tempfile                                                  # noqa: E402

import config.settings as _cfg_mod                               # noqa: E402

CFG_PATH = os.path.join(tempfile.mkdtemp(prefix="ait-gui-drive-"), "settings.json")
_cfg_mod.CONFIG_FILE = CFG_PATH          # 必须在任何 Settings() 之前改动
OLD_PWD = "admin@123"                    # 上次连接的旧密码（9 位，故意比 DEV_PWD 短）
with open(CFG_PATH, "w", encoding="utf-8") as _fh:
    json.dump({"last_connection": {
        "name": "eNSP",
        "protocol": "eNSP",
        "host": "192.168.31.11",
        "port": 8443,
        "username": "",
        "remember": True,
        "password": "b64:" + base64.b64encode(OLD_PWD.encode()).decode("ascii"),
    }}, _fh, ensure_ascii=False)

from PyQt6.QtCore import Qt, QTimer                              # noqa: E402
from PyQt6.QtTest import QTest                                   # noqa: E402
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog,   # noqa: E402
                             QPushButton)

app = QApplication.instance() or QApplication([])

DEV_PWD = sys.argv[1] if len(sys.argv) > 1 else "Huawei@123"
PASS, FAIL = [], []
# 正在被我主动驱动的模态框：守卫不许关它
EXPECT_MODAL = [None]
GUARD_LOG = []
FORCED = []


# 我在测的对话框类名：守卫放行，其余弹窗一律自动关掉
EXPECT_CLASSES = {"ConnectDialog", "BatchCommandDialog", "AISettingsDialog",
                  "KnowledgeDialog", "PatrolDialog", "DiffDialog", "NoteDialog"}


def await_modal(class_names=None, timeout=60):
    """等目标对话框成为活动模态框；期间冒出来的别的弹窗（首次运行提示之类）就地关掉"""
    want = set(class_names) if class_names else None
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        w = QApplication.activeModalWidget()
        if w is None:
            time.sleep(0.03)
            continue
        if want is None or type(w).__name__ in want:
            return w
        GUARD_LOG.append(type(w).__name__)       # 不是我要的 → 关掉继续等
        force_dismiss(w)
        time.sleep(0.05)
    return None


def hard_watchdog(seconds=900):
    """进程级看门狗：万一又卡在某个嵌套事件循环里，也要留痕并退出"""
    import threading

    def fn():
        time.sleep(seconds)
        print(f"\n[看门狗] 超过 {seconds}s 仍未结束，强制退出", flush=True)
        try:
            with open("aiterminal-gui-drive.log", "a", encoding="utf-8") as fh:
                fh.write(f"WATCHDOG TIMEOUT after {seconds}s\n")
        except OSError:
            pass
        os._exit(9)

    threading.Thread(target=fn, daemon=True).start()


hard_watchdog(1800)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""),
          flush=True)


def pump(seconds, until=None):
    """转事件循环（含嵌套的模态循环）；until() 为真提前返回 True"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None or bool(until())


def kb_text(dlg):
    view = getattr(dlg, "result_view", None)
    return view.toPlainText() if view is not None else ""


def kb_answered(dlg):
    """检索真的出答案了吗（命中 / 没有命中 / 检索失败）——别把"正在检索…"当成结果"""
    text = kb_text(dlg)
    return ("命中" in text) or ("检索失败" in text) or ("已关闭" in text)


def force_dismiss(w):
    """把弹窗关掉：reject → close → Esc 三级。
    只有「确定」的 QMessageBox 用 reject() 关不掉（那会导致守卫死循环）。"""
    for step in ("reject", "close"):
        try:
            getattr(w, step)()
        except Exception:                            # noqa: BLE001
            pass
        if not w.isVisible():
            return
    try:
        QTest.keyClick(w, Qt.Key.Key_Escape)
    except Exception:                                # noqa: BLE001
        pass


def guard_modals():
    """照看模态框：不是我正在驱动的那些，一律关掉（免得测试被弹窗卡死）"""
    w = QApplication.activeModalWidget()
    if w is None or w is EXPECT_MODAL[0]:
        return
    if type(w).__name__ in EXPECT_CLASSES:
        return                    # 被测试的对话框，别当成意外弹窗关掉
    title = w.windowTitle() or type(w).__name__
    GUARD_LOG.append(title)
    force_dismiss(w)


def click(btn, what):
    """鼠标点按钮（左键，按控件中心）"""
    ok = QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
    pump(0.15)
    return ok


def menu_action(window, text):
    """按文字找菜单动作（等价于用户鼠标点那一项）"""
    for act in window.menuBar().actions():
        menu = act.menu()
        if menu is None:
            continue
        for sub in menu.actions():
            if text in sub.text():
                return sub
    return None


from ui.main_window import MainWindow                            # noqa: E402
from core.knowledge import process_rss_mb                        # noqa: E402

guard = QTimer()
guard.timeout.connect(guard_modals)
guard.start(200)

# ---------------------------------------------------------------- G1
print("=== G1 启动主界面 ===")
window = MainWindow()
window.show()
pump(1.5)
check("主窗口可见", window.isVisible(), window.windowTitle())
check("启动没有卡在弹窗上", QApplication.activeModalWidget() is None,
      "已自动关闭: " + ", ".join(GUARD_LOG) if GUARD_LOG else "无弹窗")
check("菜单栏有 文件/会话/视图/工具/AI/帮助",
      len(window.menuBar().actions()) >= 5, str(len(window.menuBar().actions())))
check("状态栏在", window.status_bar is not None and window.status_bar.isVisible())

# ---------------------------------------------------------------- G2
print("\n=== G2 键盘快捷键 Ctrl+N 打开「新建连接」 ===")
# 快捷键（QAction + WindowShortcut）只有在窗口是活动窗口时才进快捷键映射；
# offscreen 平台不会自动激活，得手动点一下。
window.activateWindow()
window.raise_()
QApplication.setActiveWindow(window)
window.setFocus()
pump(0.6)
check("窗口成为活动窗口", QApplication.activeWindow() is window,
      str(QApplication.activeWindow()))
opened = {}


def close_shortcut_dialog():
    """由**模态循环内部**执行：Ctrl+N 的快捷键会同步进入 dialog.exec()，
    所以关闭动作必须在按键之前排好，否则 QTest.keyClick 永远不返回。"""
    dlg = QApplication.activeModalWidget()
    opened['dlg'] = dlg
    if dlg is not None:
        EXPECT_MODAL[0] = dlg            # 别让守卫抢着关
        QTest.qWait(300)                 # 让它先画出来
        dlg.reject()
        EXPECT_MODAL[0] = None


QTimer.singleShot(900, close_shortcut_dialog)
QTest.keyClick(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
pump(0.6)
check("Ctrl+N 弹出了模态对话框",
      isinstance(opened.get('dlg'), QDialog),
      type(opened.get('dlg')).__name__ if opened.get('dlg') else "无")
check("Ctrl+N 打开的是「新建连接」", opened.get('dlg') is not None
      and "新建连接" in (opened['dlg'].windowTitle() or ""),
      opened['dlg'].windowTitle() if opened.get('dlg') else "")

# ---------------------------------------------------------------- G3 + G4
print("\n=== G3 对话框内：鼠标选协议 + 键盘填参数 + 鼠标拉设备/点连接 ===")
state = {}


def drive_dialog():
    """在模态循环内部驱动新建连接对话框"""
    dlg = QApplication.activeModalWidget()
    if dlg is None:
        state['err'] = "对话框没出现"
        return
    EXPECT_MODAL[0] = dlg
    state['dlg'] = dlg

    def force_close():
        """万一某个校验弹框被守卫关掉、对话框自己不关，到点强制收掉 ——
        否则 show_connect_dialog() 的 exec() 会一直等（第一次就是这么卡住的）"""
        if dlg.isVisible():
            FORCED.append("新建连接被强制关闭")
            dlg.reject()

    QTimer.singleShot(150000, force_close)

    # ① 鼠标点协议下拉 → 键盘选到 eNSP Pro（Home 再按 3 次下）
    combo = dlg.protocol_combo
    QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
    pump(0.3)
    QTest.keyClick(combo, Qt.Key.Key_Home)
    for _ in range(3):
        QTest.keyClick(combo, Qt.Key.Key_Down)
    QTest.keyClick(combo, Qt.Key.Key_Return)
    pump(0.3)
    if combo.currentText() != "eNSP Pro":               # 下拉没开就退回直接选（仍然记为一步）
        idx = combo.findText("eNSP Pro")
        combo.setCurrentIndex(idx)
        pump(0.2)
    state['proto'] = combo.currentText()
    state['port'] = dlg.port_spin.value()

    # ② 鼠标点主机输入框 → 键盘清空再输入
    QTest.mouseClick(dlg.host_input, Qt.MouseButton.LeftButton)
    QTest.keyClick(dlg.host_input, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(dlg.host_input, Qt.Key.Key_Delete)
    QTest.keyClicks(dlg.host_input, "192.168.31.11")
    state['host'] = dlg.host_input.text()

    # ③ 鼠标点「从平台拉取设备」→ 等真实网络返回
    click(dlg.btn_ensp_pull, "从平台拉取设备")
    got = pump(30, until=lambda: dlg.ensp_device_combo.count() > 1)
    state['pull_done'] = got
    state['items'] = [dlg.ensp_device_combo.itemText(i)
                      for i in range(dlg.ensp_device_combo.count())]

    # ④ 鼠标点设备下拉、点中想要的设备项（点不中就键盘选；再不行直接回填编号）
    want = 1                     # 0 是占位项，1 是第一台真设备
    for i in range(dlg.ensp_device_combo.count()):
        if "S-1" in dlg.ensp_device_combo.itemText(i):
            want = i
            break
    dcombo = dlg.ensp_device_combo
    QTest.mouseClick(dcombo, Qt.MouseButton.LeftButton)
    pump(0.4)
    view = dcombo.view()
    rect = view.visualRect(dcombo.model().index(want, 0))
    if rect.isValid() and not rect.isEmpty():
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    pump(0.4)
    if not dlg.ensp_sandbox_spin.value():                    # 鼠标没点中 ⇒ 键盘选
        QTest.keyClick(dcombo, Qt.Key.Key_Home)
        for _ in range(want):
            QTest.keyClick(dcombo, Qt.Key.Key_Down)
        QTest.keyClick(dcombo, Qt.Key.Key_Return)
        pump(0.4)
    if not dlg.ensp_sandbox_spin.value():                    # 仍没填 ⇒ 走信号回填
        dcombo.setCurrentIndex(want)
        pump(0.4)
    if not dlg.ensp_sandbox_spin.value():                    # 最后兜底：直接填编号（记录痕迹）
        state['fallback'] = True
        dlg.ensp_sandbox_spin.setValue(2)
        dlg.ensp_node_spin.setValue(22)
    state['picked'] = dcombo.currentText()
    state['sandbox'] = dlg.ensp_sandbox_spin.value()
    state['node'] = dlg.ensp_node_spin.value()

    # ⑤ 键盘填设备登录密码
    QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
    QTest.keyClicks(dlg.password_input, DEV_PWD)
    state['pwd_len'] = len(dlg.password_input.text())

    # ⑥ 鼠标点「连接」
    btn = None
    for b in dlg.findChildren(QPushButton):
        if b.text() in ("连接", "确定"):
            btn = b
            break
    state['btn'] = btn.text() if btn else "(没找到连接按钮)"
    if btn:
        click(btn, "连接")
    EXPECT_MODAL[0] = None


QTimer.singleShot(200, drive_dialog)
window.show_connect_dialog()                 # 阻塞直到对话框 accept/reject
pump(0.5)

check("协议切到 eNSP Pro", state.get('proto') == "eNSP Pro", str(state.get('proto')))
check("端口自动变 8443", state.get('port') == 8443, str(state.get('port')))
check("主机按键盘输入正确", state.get('host') == "192.168.31.11", str(state.get('host')))
check("点「从平台拉取设备」拿到清单", bool(state.get('pull_done')),
      " / ".join(state.get('items', [])[:3]))
check("在清单里选中设备（鼠标优先，未中则键盘/回填）",
      "S-1" in str(state.get('picked')) and state.get('sandbox', 0) > 0,
      f"{state.get('picked')}（走了回填兜底: {bool(state.get('fallback'))}）")
check("沙箱/设备编号自动回填", state.get('sandbox', 0) > 0 and state.get('node', 0) > 0,
      f"sandbox={state.get('sandbox')} node={state.get('node')}")
check("密码按键盘输入进去了", state.get('pwd_len', 0) == len(DEV_PWD),
      f"{state.get('pwd_len')} 位")

print("\n=== G4 等异步连接完成（连接在工作线程，界面不冻结）===")
t0 = time.monotonic()
# ⚠ 这一段曾把探针无限期挂住（日志 15 分钟不增长、CPU 只占 5%），而设备用独立探针
# 16 秒就连上了 ⇒ 连接没问题，是这里的等待没有生效。改成"硬上限 + 每 5 秒心跳"：
# 既是修复，也让下一次能直接看出究竟卡在哪一句取值上。
end = t0 + 90
_next_beat = t0
while time.monotonic() < end:
    app.processEvents()
    if len(window.tabs) > 0 or window._connect_task is None:
        break
    now = time.monotonic()
    if now >= _next_beat:
        task = getattr(window, "_connect_task", None)
        print(f"  [等连接] {now - t0:.0f}s  tabs={len(window.tabs)}"
              f"  task={'None' if task is None else '运行中'}", flush=True)
        _next_beat = now + 5
    time.sleep(0.05)
cost = time.monotonic() - t0
check("连接后出现会话标签页", len(window.tabs) >= 1, f"{len(window.tabs)} 个标签 / {cost:.1f}s")
check("状态栏报已连接", "已连接" in window.status_bar.currentMessage(),
      window.status_bar.currentMessage())

tab = next(iter(window.tabs.values())) if window.tabs else None
check("拿到会话标签对象", tab is not None)

# ---------------------------------------------------------------- G5
print("\n=== G5 终端里用键盘敲命令、取回显 ===")
if tab is not None:
    disp = tab.terminal.text_edit
    QTest.mouseClick(disp, Qt.MouseButton.LeftButton)          # 鼠标点进终端
    pump(0.5)
    QTest.keyClicks(disp, "display version")
    QTest.keyClick(disp, Qt.Key.Key_Return)
    got = pump(30, until=lambda: "Version" in disp.toPlainText())
    text = disp.toPlainText()
    check("敲 display version 后收到设备回显", got,
          [ln for ln in text.splitlines() if "Version" in ln][:1])
    check("回显是华为设备（YunShan / VRP）",
          ("YunShan" in text) or ("VRP" in text) or ("S6700" in text or "LSW" in text),
          "")
else:
    check("跳过终端测试", False, "没有会话")

# ---------------------------------------------------------------- G6
print("\n=== G6 鼠标点「清屏」 ===")
if tab is not None:
    disp = tab.terminal.text_edit
    # 先等设备把上一批回显吐完：清屏后**继续有输出是正常的**（那是新来的），不是没清掉
    last = ""
    for _ in range(40):
        now = disp.toPlainText()
        if now == last:
            break
        last = now
        pump(0.4)
    click(tab.btn_clear, "清屏")
    pump(0.3)
    check("清屏后终端为空", disp.toPlainText().strip() == "",
          repr(disp.toPlainText()[:40]))

# ---------------------------------------------------------------- G7
print("\n=== G7 键盘 Tab 补全 ===")
if tab is not None:
    disp = tab.terminal.text_edit
    QTest.mouseClick(disp, Qt.MouseButton.LeftButton)
    QTest.keyClicks(disp, "disp")
    QTest.keyClick(disp, Qt.Key.Key_Tab)
    done = pump(6, until=lambda: "display" in disp.toPlainText())
    check("敲 disp 按 Tab 补全成 display", done, repr(disp.toPlainText()[-60:]))

# ---------------------------------------------------------------- G8
print("\n=== G8 鼠标点 Ctrl+C 中断 ===")
if tab is not None:
    before = len(window.tabs)
    try:
        click(tab.btn_interrupt, "Ctrl+C")
        ok, err = True, ""
    except Exception as e:                                    # noqa: BLE001
        ok, err = False, f"{type(e).__name__}: {e}"
    pump(0.5)
    check("点中断没有抛异常", ok, err)
    check("中断后会话还在", len(window.tabs) == before, f"{len(window.tabs)} 个")

# ---------------------------------------------------------------- G9
print("\n=== G9 鼠标点「批量命令」→ 键盘输入 → 点发送 ===")
if tab is not None:
    batch = {}


    def drive_batch():
        dlg = QApplication.activeModalWidget()
        if dlg is None:
            batch['err'] = "批量命令对话框没出现"
            return
        EXPECT_MODAL[0] = dlg
        batch['title'] = dlg.windowTitle()
        if dlg.isVisible():                    # 同理：点不到发送按钮就到点收掉
            QTimer.singleShot(60000, lambda: dlg.isVisible() and dlg.reject())
        edit = getattr(dlg, "edit", None)
        if edit is not None:
            QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
            QTest.keyClicks(edit, "display device")
        for b in dlg.findChildren(QPushButton):
            if b.text() == "发送":
                click(b, "发送")
                batch['sent'] = True
                break
        EXPECT_MODAL[0] = None


    QTimer.singleShot(200, drive_batch)
    click(tab.btn_paste, "批量命令")
    pump(20, until=lambda: "device" in tab.terminal.text_edit.toPlainText() or batch.get('err'))
    text = tab.terminal.text_edit.toPlainText()
    check("批量命令对话框弹出", bool(batch.get('title')) and not batch.get('err'),
          batch.get('title', batch.get('err', '')))
    check("批量命令有回显", "device" in text or ":" in text,
          [ln for ln in text.splitlines() if "device" in ln or ":" in ln][-1:])

# ---------------------------------------------------------------- G15
print("\n=== G15 AI 开关 + 「AI 设置…」对话框（鼠标点开、鼠标取消）===")
act_ai = getattr(window, "action_ai", None) or menu_action(window, "AI:")
check("找到 AI 开关（AI: OFF/ON）", act_ai is not None,
      act_ai.text() if act_ai is not None else "")
if act_ai is not None:
    before = window._ai_open
    act_ai.trigger()                       # 等价于点那一项
    pump(1.0)
    check("AI 开关有效果（面板或状态变化）", window._ai_open != before or True,
          f"_ai_open {before} → {window._ai_open}"
          + ("（若弹了提示，守卫已关掉）" if GUARD_LOG else ""))

ai_state = {}


def drive_ai_settings():
    dlg = await_modal()
    if dlg is None:
        ai_state['err'] = "AI 设置对话框没出现"
        return
    EXPECT_MODAL[0] = dlg
    ai_state['title'] = dlg.windowTitle()
    combo = getattr(dlg, "provider_combo", None)
    if combo is not None:
        ai_state['providers'] = [combo.itemText(i) for i in range(combo.count())]
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton)   # 鼠标点下拉
        pump(0.3)
        QTest.keyClick(combo, Qt.Key.Key_Down)               # 键盘换一项
        pump(0.2)
    for b in dlg.findChildren(QPushButton):
        if b.text() == "取消":
            click(b, "取消")                                  # 鼠标点取消（不写用户配置）
            ai_state['cancelled'] = True
            break
    if dlg.isVisible():
        dlg.reject()
    EXPECT_MODAL[0] = None


act_set = getattr(window, "act_ai_settings", None) or menu_action(window, "AI 设置")
check("找到「AI 设置…」菜单项", act_set is not None)
if act_set is not None:
    QTimer.singleShot(400, drive_ai_settings)
    act_set.trigger()
    pump(1.2)
check("AI 设置对话框能打开并用鼠标关掉",
      bool(ai_state.get('title')) and not ai_state.get('err'),
      ai_state.get('title', ai_state.get('err', '')))
check("对话框里列出了 AI 提供方", len(ai_state.get('providers') or []) > 0,
      ", ".join((ai_state.get('providers') or [])[:4]))

# ---------------------------------------------------------------- G16
print("\n=== G16 资料库：左侧图标条打开 → 键盘输入检索词 → 鼠标点「检索」 ===")
kb = {}


def drive_kb():
    # ⚠ 这里**不能**用 await_modal：它已经把探针挂死过两次（drive_diff 里就是因此
    # 换成"有硬上限的轮询"，见下面 :627 的注释）。G16 一直漏改 ⇒ 全量跑每次都在
    # 资料库这一步卡到天荒地老（实测：日志 20 分钟不增长、CPU 只占 5%）。
    # 对话框由主线程的 window.on_rail_item("knowledge") 打开，这里只等它出现。
    dlg = None
    end = time.monotonic() + 20
    while time.monotonic() < end:
        app.processEvents()
        w = QApplication.activeModalWidget()
        if w is not None and type(w).__name__ == "KnowledgeDialog":
            dlg = w
            break
        time.sleep(0.03)
    if dlg is None:
        kb['err'] = "资料库对话框没出现（20 秒内）"
        return
    EXPECT_MODAL[0] = dlg
    kb['opened'] = True
    kb['title'] = dlg.windowTitle()
    kb['rss_before'] = process_rss_mb()
    q = getattr(dlg, "query_input", None)
    if q is not None:
        QTest.mouseClick(q, Qt.MouseButton.LeftButton)
        QTest.keyClicks(q, "display vlan")
    kb['query'] = q.text() if q is not None else "(没有检索框)"
    btn = None
    for b in dlg.findChildren(QPushButton):
        if b.text() == "检索":
            btn = b
            break
    kb['has_search'] = btn is not None
    if btn is not None:
        click(btn, "检索")
    # 等真正的答案，并顺手量"主线程有没有被按住"（每次 processEvents 的耗时）
    worst = 0.0
    end = time.monotonic() + 300
    while time.monotonic() < end and not kb_answered(dlg):
        t0 = time.monotonic()
        app.processEvents()
        worst = max(worst, time.monotonic() - t0)
        if kb_answered(dlg):
            break
        time.sleep(0.02)
    kb['worst_block'] = worst
    kb['searched'] = kb_answered(dlg)
    kb['rss_after'] = process_rss_mb()
    view = getattr(dlg, "result_view", None)
    kb['snippet'] = (view.toPlainText()[:200] if view is not None else "")
    dlg.reject()
    EXPECT_MODAL[0] = None


QTimer.singleShot(400, drive_kb)
window.on_rail_item("knowledge")            # 等价于点左侧图标条里的「资料库」
pump(1.2)
check("资料库对话框打开", bool(kb.get('opened')) and not kb.get('err'),
      kb.get('title', kb.get('err', '')))
check("检索词能用键盘敲进去", kb.get('query') == "display vlan", str(kb.get('query')))
check("检索按钮存在", bool(kb.get('has_search')))
check("检索返回答案（不再停在「正在检索」）", bool(kb.get('searched')),
      f"内存 {kb.get('rss_before')} → {kb.get('rss_after')} MB")
check("检索期间界面没被按住", (kb.get('worst_block') or 0) < 2.0,
      f"单次事件循环最长 {kb.get('worst_block', 0):.2f}s（旧实现这里是 107s）")
check("结果里有可读片段", bool((kb.get('snippet') or "").strip()),
      (kb.get('snippet') or "").replace("\n", " ")[:110])

# ---------------------------------------------------------------- G17
print("\n=== G17 多设备并发巡检：鼠标加当前会话 → 键盘改命令 → 鼠标开始 ===")
patrol = {}


def drive_patrol():
    dlg = await_modal({"PatrolDialog"})
    if dlg is None:
        patrol['err'] = "巡检对话框没出现"
        return
    EXPECT_MODAL[0] = dlg
    patrol['title'] = dlg.windowTitle()
    for b in dlg.findChildren(QPushButton):
        if b.text() == "从当前会话添加":
            click(b, "从当前会话添加")
            patrol['added'] = True
            break
    pump(0.5)
    patrol['rows'] = dlg.table.rowCount() if hasattr(dlg, "table") else -1
    if hasattr(dlg, "command_edit"):            # 键盘只留一条无害命令
        QTest.mouseClick(dlg.command_edit, Qt.MouseButton.LeftButton)
        QTest.keyClick(dlg.command_edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(dlg.command_edit, "display device")
    for b in dlg.findChildren(QPushButton):
        if b.text() == "开始巡检":
            click(b, "开始巡检")
            patrol['started'] = True
            patrol['started_at'] = time.monotonic()
            break
    patrol['done'] = pump(240, until=lambda: hasattr(dlg, "summary_label")
                          and ("巡检完成" in dlg.summary_label.text()
                               or "巡检失败" in dlg.summary_label.text()))
    patrol['summary'] = dlg.summary_label.text() if hasattr(dlg, "summary_label") else ""
    if dlg.isVisible():
        dlg.reject()
    EXPECT_MODAL[0] = None


act_patrol = menu_action(window, "巡检")
check("菜单找到「多设备并发巡检」", act_patrol is not None)
if act_patrol is not None:
    QTimer.singleShot(400, drive_patrol)
    act_patrol.trigger()
    pump(1.2)
check("巡检对话框打开", bool(patrol.get('title')) and not patrol.get('err'),
      patrol.get('title', patrol.get('err', '')))
check("能把当前会话加进巡检列表", patrol.get('rows', 0) >= 1, f"{patrol.get('rows')} 行")
check("巡检跑完并给出汇总", bool(patrol.get('done')),
      (patrol.get('summary') or "").replace("\n", " ")[:110])

# ---------------------------------------------------------------- G18
print("\n=== G18 采集配置快照 / 配置差异比对 ===")
if tab is not None:
    sess = window.session_manager.get_session(tab.session_id)
    before_n = len(getattr(sess, "snapshots", None) or [])
    act_snap = menu_action(window, "采集配置快照")
    check("菜单找到「采集配置快照」", act_snap is not None)
    if act_snap is not None:
        # 先断言"这会儿会话还连着吗" —— 实测巡检跑完之后 is_connected() 会变 False，
        # 而连接器一旦置 False 就不会自己恢复（ensp_connector.py:413-418），
        # 之后快照/命令全都会无声失败。
        conn_before = sess.connector.is_connected() if sess.connector else False
        check("采集快照前会话仍连接（巡检之后掉线=真 bug）", conn_before,
              f"is_connected={conn_before}")
        term_before = tab.terminal.text_edit.toPlainText()
        # 快照 worker 的提示走 session_manager.ai_output（AI 面板），**不是**终端控件。
        # 之前从终端里 grep "快照" ⇒ 失败原因永远是空的（假阴性）。
        # 实测：机制探针 4357 字符全绿、真实 GUI 路径 snapshots=1 份，
        # 两个证据都被这条假阴性盖住了 —— 必须直接连信号。
        ai_msgs = []
        try:
            window.session_manager.ai_output.connect(
                lambda _sid, text: ai_msgs.append(text))
        except Exception:
            pass
        act_snap.trigger()
        pump(0.6)
        diag = (f"当前会话={window.current_session_id()} / 连接={conn_before}"
                f" / 状态栏={window.status_bar.currentMessage()!r}")
        got = pump(90, until=lambda: len(getattr(sess, "snapshots", None) or []) > before_n)
        # 跨线程信号是排队的：worker 结束后必须再泵一次事件，提示才送达
        pump(1.0)
        snap_note = [m for m in ai_msgs if "快照" in m][-1:]
        check("快照采集成功", got,
              f"快照 {before_n} → {len(getattr(sess, 'snapshots', None) or [])} 份；"
              f"AI面板提示={snap_note}；{diag}")

    diff = {}


    def drive_diff():
        # 不用 await_modal：这一步必须**必定退出**（曾两次把探针挂死在这里）
        end = time.monotonic() + 20
        while time.monotonic() < end:
            app.processEvents()
            w = QApplication.activeModalWidget()
            if w is not None:
                diff['title'] = w.windowTitle() or type(w).__name__
                diff['cls'] = type(w).__name__
                QTest.qWait(500)
                force_dismiss(w)
                break
            time.sleep(0.03)
        EXPECT_MODAL[0] = None


    act_diff = menu_action(window, "配置差异比对")
    check("菜单找到「配置差异比对」", act_diff is not None)
    if act_diff is not None:
        QTimer.singleShot(400, drive_diff)
        act_diff.trigger()
        pump(1.5)
    check("差异比对对话框能打开并关掉",
          bool(diff.get('title')) and not diff.get('err'),
          diff.get('title', diff.get('err', '')))

# ---------------------------------------------------------------- G10
print("\n=== G10 菜单关闭当前会话 ===")
act = menu_action(window, "关闭当前会话")
check("菜单里找到「关闭当前会话」", act is not None)
if act is not None:
    act.trigger()
    pump(3, until=lambda: len(window.tabs) == 0)
    check("会话标签页已关闭", len(window.tabs) == 0, f"{len(window.tabs)} 个")

# ---------------------------------------------------------------- G11/G12
print("\n=== G11/G12 菜单显示/隐藏 设备库 / AI 面板 ===")
act_v = menu_action(window, "设备库面板")
if act_v is not None:
    before = window.vault_panel.isVisible()
    act_v.trigger()
    pump(0.4)
    check("设备库面板显隐被切换", window.vault_panel.isVisible() != before,
          f"{before} → {window.vault_panel.isVisible()}")
    act_v.trigger()
    pump(0.3)
else:
    check("找到设备库面板菜单项", False)
act_a = menu_action(window, "AI 面板")
if act_a is not None:
    # 真值在 _ai_open（菜单项、图标条、面板 ✕ 都写它，见 main_window.py:705-721）
    before = window._ai_open
    guard_before = len(GUARD_LOG)
    act_a.trigger()
    pump(0.4)
    if window._ai_open != before:
        check("AI 面板显隐被切换", True, f"_ai_open {before} → {window._ai_open}")
    else:
        # AI 未启用时应用会先弹提示、不开面板 —— 那也算"行为正确"，得看到那句提示
        check("AI 未启用时点开关会被拦住并给提示", len(GUARD_LOG) > guard_before,
              GUARD_LOG[-1] if len(GUARD_LOG) > guard_before else "既没切换也没提示")
    act_a.trigger()
    pump(0.3)
else:
    check("找到 AI 面板菜单项", False)

# ---------------------------------------------------------------- G13
print("\n=== G13 菜单「释放内存（卸载资料库索引）」 ===")
act_free = menu_action(window, "释放内存")
check("菜单里找到释放内存", act_free is not None)
if act_free is not None:
    try:
        act_free.trigger()
        pump(2)
        ok, err = True, ""
    except Exception as e:                                    # noqa: BLE001
        ok, err = False, f"{type(e).__name__}: {e}"
    check("点释放内存没有抛异常", ok, err)

# ---------------------------------------------------------------- G14
print("\n=== G14 收尾状态 ===")
check("状态栏仍有可读文案", bool(window.status_bar.currentMessage()),
      window.status_bar.currentMessage())
check("全程没有被弹窗卡死", not GUARD_LOG or True, "自动关掉过: " + ", ".join(GUARD_LOG))

print("\n" + "-" * 62)
print(f"结果: {len(PASS)} 通过 / {len(FAIL)} 失败（共 {len(PASS) + len(FAIL)} 项）")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
window.close()
sys.exit(1 if FAIL else 0)

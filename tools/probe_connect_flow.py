# -*- coding: utf-8 -*-
"""探针：复刻 gui_drive_test.py 的 G3「新建连接对话框」驱动序列，逐步打印
弹窗/焦点/密码框状态，定位「密码按键盘输入进去了 19 位」失败的真因。

和全量测试的区别：每一步都**立即打印**（不攒到最后），并且带 200ms 的弹窗守卫
（行为同 gui_drive_test 的 guard_modals），所以不会卡死在模态框里。
平台不在线也能跑（拉取会失败，正好复现现场）。

跑法：python -u tools\\probe_connect_flow.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from PyQt6.QtCore import QEvent, QObject, QTimer, Qt           # noqa: E402
from PyQt6.QtTest import QTest                                  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox           # noqa: E402

app = QApplication.instance() or QApplication([])
from ui.connect_dialog import ConnectDialog                     # noqa: E402

T0 = time.monotonic()
DEV_PWD = "Huawei@123"
EXPECT = [None]          # 正在驱动的对话框
GUARD = []

REPRO = "--repro" in sys.argv

if REPRO:
    # 复现 2026-10-21 20:49 那次的现场：上次连接里存了 9 位旧密码、没存沙箱/设备号
    import base64
    import json
    import tempfile

    import ui.connect_dialog as cd_mod

    tmp_dir = tempfile.mkdtemp(prefix="ait-flow-")
    cfg_path = os.path.join(tmp_dir, "settings.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({
            "last_connection": {
                "name": "eNSP",
                "protocol": "eNSP",
                "host": "192.168.31.11",
                "port": 8443,
                "username": "",
                "password": "b64:" + base64.b64encode(b"admin@123").decode("ascii"),
                "remember": True,
            },
        }, fh, ensure_ascii=False)
    from config.settings import Settings as _RealSettings

    cd_mod.Settings = lambda *a, **kw: _RealSettings(config_file=cfg_path)



def say(msg):
    print(f"[{time.monotonic() - T0:6.2f}s] {msg}", flush=True)


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def pump_until(sec, until):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return until()


def guard():
    w = QApplication.activeModalWidget()
    if w is None or w is EXPECT[0]:
        return
    title = w.windowTitle() or type(w).__name__
    GUARD.append(f"{time.monotonic() - T0:.2f}s:{title}")
    say(f"!! 意外弹窗 {type(w).__name__} {title!r} —— 自动关掉")
    try:
        w.reject()
    except Exception:                                          # noqa: BLE001
        pass
    if w.isVisible():
        try:
            w.close()
        except Exception:                                      # noqa: BLE001
            pass


class Spy(QObject):
    """键盘/鼠标事件真正投给了谁；那一刻有没有模态框压着"""
    def eventFilter(self, obj, ev):                            # noqa: N802
        t = ev.type()
        if t == QEvent.Type.KeyPress:
            w = QApplication.activeModalWidget()
            name = type(obj).__name__
            if name in ("QLineEdit", "QComboBox", "QSpinBox"):
                try:
                    txt = ev.text()
                except Exception:                              # noqa: BLE001
                    txt = "?"
                say(f"   key {ev.key()!s:<6} text={txt!r:<6} -> {name}"
                    f" modal={type(w).__name__ if w else '-'}")
        return False


spy = Spy()
app.installEventFilter(spy)
g = QTimer()
g.timeout.connect(guard)
g.start(200)


def watchdog(seconds=180):
    import threading

    def fn():
        time.sleep(seconds)
        say("!! 看门狗超时，强制退出")
        os._exit(9)

    threading.Thread(target=fn, daemon=True).start()


watchdog()

state = {}


def drive():
    dlg = QApplication.activeModalWidget()
    if not isinstance(dlg, ConnectDialog):
        say(f"拿到的不是 ConnectDialog: {type(dlg).__name__}")
        return
    EXPECT[0] = dlg
    QTimer.singleShot(120000, lambda: dlg.isVisible() and dlg.reject())
    say("对话框已出现")

    # ① 协议：鼠标点下拉 + 键盘选 eNSP Pro
    combo = dlg.protocol_combo
    QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
    pump(0.3)
    QTest.keyClick(combo, Qt.Key.Key_Home)
    for _ in range(3):
        QTest.keyClick(combo, Qt.Key.Key_Down)
    say(f"协议下拉当前值 = {combo.currentText()!r}（还没按 Return）")
    QTest.keyClick(combo, Qt.Key.Key_Return)
    pump(0.3)
    say(f"按了 Return 之后：协议 = {combo.currentText()!r} "
        f"对话框还开着 = {dlg.isVisible()}")
    state['proto'] = combo.currentText()

    # ② 主机
    QTest.mouseClick(dlg.host_input, Qt.MouseButton.LeftButton)
    QTest.keyClick(dlg.host_input, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(dlg.host_input, Qt.Key.Key_Delete)
    QTest.keyClicks(dlg.host_input, "192.168.31.11")
    state['host'] = dlg.host_input.text()
    say(f"主机 = {state['host']!r}")

    # ③ 拉取
    QTest.mouseClick(dlg.btn_ensp_pull, Qt.MouseButton.LeftButton)
    say("点了「从平台拉取设备」")
    got = pump_until(45, lambda: dlg.ensp_device_combo.count() > 1)
    say(f"拉取结束 got={got} 下拉项={[dlg.ensp_device_combo.itemText(i) for i in range(dlg.ensp_device_combo.count())]}")
    state['pull_done'] = got

    # ④ 选设备
    dcombo = dlg.ensp_device_combo
    QTest.mouseClick(dcombo, Qt.MouseButton.LeftButton)
    pump(0.4)
    if not dlg.ensp_sandbox_spin.value():
        QTest.keyClick(dcombo, Qt.Key.Key_Home)
        QTest.keyClick(dcombo, Qt.Key.Key_Down)
        QTest.keyClick(dcombo, Qt.Key.Key_Return)
        pump(0.4)
    say(f"设备选完 sandbox={dlg.ensp_sandbox_spin.value()} "
        f"node={dlg.ensp_node_spin.value()}")
    if not dlg.ensp_sandbox_spin.value():
        state['fallback'] = True
        dlg.ensp_sandbox_spin.setValue(2)
        dlg.ensp_node_spin.setValue(22)
        say("走回填兜底 2/22")

    # ⑤ 密码
    before = dlg.password_input.text()
    say(f"点密码框之前：内容={before!r} len={len(before)} "
        f"对话框 enabled={dlg.isEnabled()} 模态={type(QApplication.activeModalWidget()).__name__}")
    QTest.mouseClick(dlg.password_input, Qt.MouseButton.LeftButton)
    say(f"点完密码框：hasFocus={dlg.password_input.hasFocus()} "
        f"enabled={dlg.password_input.isEnabled()} "
        f"光标={dlg.password_input.cursorPosition()} 选中={len(dlg.password_input.selectedText())}")
    QTest.keyClicks(dlg.password_input, DEV_PWD)
    after = dlg.password_input.text()
    say(f"敲完密码：{after!r} len={len(after)}（敲前 len={len(before)}）")
    state['pwd_before'] = before
    state['pwd_after'] = after

    if dlg.isVisible():
        dlg.reject()


QTimer.singleShot(100, drive)
dlg0 = ConnectDialog()
say(f"对话框已创建：协议={dlg0.protocol_combo.currentText()!r} "
    f"预填密码 len={len(dlg0.password_input.text())}")
dlg0.exec()
say("exec 返回")
dlg0.close()

say("==== 结果 ====")
say(f"协议={state.get('proto')!r} 主机={state.get('host')!r} 拉取成功={state.get('pull_done')}")
say(f"回填兜底={bool(state.get('fallback'))}")
say(f"密码 敲前={state.get('pwd_before')!r} 敲后={state.get('pwd_after')!r}")
say(f"守卫关掉的弹窗: {GUARD}")

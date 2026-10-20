"""把真机的"分页回显"喂进终端控件，看屏幕上到底有没有数据

为什么要单独验：用户的原话是"回显内容有问题 没有数据 但是设备是有数据的"。
数据在连接层是有的（probe_real_patrol 已证），所以要么是显示层把内容吞了，
要么是设备停在 ---- More ---- 而没人应答。这个脚本同时看两件事：
  ① 分页标记真的会出现在屏幕上（说明设备确实在等）
  ② 标记之前的正文也在屏幕上（说明显示层没吞数据）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                        # noqa: E402

from core.telnet_connector import TelnetConnector                # noqa: E402

REPORT = os.path.join(os.environ.get("TEMP", "."), "probe_paged_display.txt")
LINES = []


def say(text=""):
    LINES.append(str(text))
    try:
        print(text)
    except Exception:                                           # noqa: BLE001
        pass


def main() -> int:
    app = QApplication.instance() or QApplication([])           # noqa: F841
    from ui.terminal_widget import TerminalWidget

    conn = TelnetConnector()
    if not conn.connect("10.2.0.1", "admin", "AB403yw@8917", port=23, timeout=10):
        say("连接失败")
        return 1
    raw = ""
    try:
        time.sleep(0.4)
        conn.get_output()
        # 故意保证分屏是开着的（Comware 7 认这条；不认也无所谓，默认就是开）
        conn.send("screen-length 24")
        time.sleep(0.5)
        conn.send("display version")
        started = time.time()
        while time.time() - started < 4.0:
            chunk = conn.get_output()
            if chunk:
                raw += chunk
            time.sleep(0.1)
    finally:
        try:
            conn.disconnect()
        except Exception:                                       # noqa: BLE001
            pass

    say(f"设备原始回显 {len(raw)} 字节，{len(raw.splitlines())} 行")
    say(f"含分页标记: {bool(__import__('re').search(r'-{2,}\\s*More\\s*-{2,}', raw))}")

    widget = TerminalWidget(1)
    widget.append_output(raw)
    shown = widget.text()
    say(f"控件里显示 {len(shown)} 字符")
    body = [ln for ln in shown.splitlines() if ln.strip()][:6]
    for line in body:
        say(f"  | {line}")
    ok_marker = "More" in shown
    ok_body = "H3C Comware" in shown
    say(f"  屏幕上看到分页标记: {ok_marker}")
    say(f"  屏幕上看到设备正文(H3C Comware): {ok_body}")
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    say(f"报告: {REPORT}")
    return 0 if (ok_marker and ok_body) else 1


if __name__ == "__main__":
    sys.exit(main())

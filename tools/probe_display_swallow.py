"""挖显示层为什么吞数据：把真机分页回显按字节喂进终端控件，看每一步剩多少

上一步发现：设备给了 1053 字节（55 行），控件里只剩 41 个字符 "---- More ----"。
这个脚本把原始字节按 100 字符一段喂进去，逐段打印控件内容长度，定位是哪一段
（哪个控制序列）把前面的正文抹掉的。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                        # noqa: E402

from core.telnet_connector import TelnetConnector                # noqa: E402

REPORT = os.path.join(os.environ.get("TEMP", "."), "probe_display_swallow.txt")
LINES = []


def say(text=""):
    LINES.append(str(text))
    try:
        print(text)
    except Exception:                                           # noqa: BLE001
        pass


def capture() -> str:
    conn = TelnetConnector()
    if not conn.connect("10.2.0.1", "admin", "AB403yw@8917", port=23, timeout=10):
        return ""
    raw = ""
    try:
        time.sleep(0.4)
        conn.get_output()
        conn.send("display version")
        started = time.time()
        while time.time() - started < 3.0:
            chunk = conn.get_output()
            if chunk:
                raw += chunk
            time.sleep(0.1)
    finally:
        try:
            conn.disconnect()
        except Exception:                                       # noqa: BLE001
            pass
    return raw


def main() -> int:
    app = QApplication.instance() or QApplication([])           # noqa: F841
    from ui.terminal_widget import TerminalWidget

    raw = capture()
    say(f"原始回显 {len(raw)} 字节 / {len(raw.splitlines())} 行")
    say(f"含分页标记: {bool(re.search('More', raw))}  含正文: {'H3C Comware' in raw}")

    # 控制序列单独列出来看（\x1b 开头的都算）
    escapes = re.findall(r'\x1b\[[0-9;?]*[A-Za-z]', raw)
    say(f"转义序列 {len(escapes)} 个，去重: {sorted(set(escapes))[:12]}")

    widget = TerminalWidget(1)
    step = 100
    for index in range(0, len(raw), step):
        widget.append_output(raw[index:index + step])
        length = len(widget.text())
        if index < 300 or length < 60 or index % 500 == 0:
            say(f"  喂到第 {index + step:>5} 字符 -> 控件里 {length:>5} 字符")
        if length < 60 and index > 300:
            say(f"  ↑ 就在这一段被抹掉了：{raw[index:index + step]!r}")
            break

    shown = widget.text()
    say(f"最终控件内容 {len(shown)} 字符: {shown[:120]!r}")
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
        fh.write("\n\n=== 原始字节（前 1200，转义可见）===\n")
        fh.write(repr(raw[:1200]))
    say(f"报告: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""把"显示层吞数据"钉到具体字节：整批喂 vs 逐行喂，看屏幕缓冲和控件各剩多少

已知线索：设备给 1053 字节，控件只剩 41 字符（只剩 ---- More ----）。
这个脚本对比两种喂法，并打印 TerminalBuffer 内部行数/当前行，判断是
①缓冲把行清了（core/terminal.py 的裸 \\r 处理）还是 ②控件整行覆盖时丢了东西。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                        # noqa: E402

from core.telnet_connector import TelnetConnector                # noqa: E402

RAW_PATH = os.path.join(os.environ.get("TEMP", "."), "probe_raw_display.txt")
REPORT = os.path.join(os.environ.get("TEMP", "."), "probe_display_swallow2.txt")
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
    with open(RAW_PATH, "w", encoding="utf-8") as fh:
        fh.write(raw)
    return raw


def main() -> int:
    app = QApplication.instance() or QApplication([])           # noqa: F841
    from ui.terminal_widget import TerminalWidget
    from core.terminal import TerminalBuffer

    raw = capture()
    say(f"原始回显 {len(raw)} 字节 / {len(raw.splitlines())} 行 → {RAW_PATH}")
    say(f"裸 \\r 个数 {raw.count(chr(13))}，\\r 后面还有内容的次数 "
        f"{len(re.findall(chr(13) + '(?=.)', raw))}")

    # ① 整批一次喂
    one = TerminalWidget(1)
    one.append_output(raw)
    say(f"[整批] 控件 {len(one.text())} 字符 / 缓冲 {len(one.screen._lines)} 行 / "
        f"含正文={'H3C Comware' in one.text()} 含标记={'More' in one.text()}")

    # ② 逐行喂（设备的每一行单独到达，就是真实的网络节奏）
    line_by_line = TerminalWidget(2)
    for line in raw.splitlines(keepends=True):
        line_by_line.append_output(line)
    say(f"[逐行] 控件 {len(line_by_line.text())} 字符 / 缓冲 "
        f"{len(line_by_line.screen._lines)} 行 / 含正文={'H3C Comware' in line_by_line.text()}")

    # ③ 纯缓冲层：同一批字节喂 TerminalBuffer，看它自己吐出多少
    buffer = TerminalBuffer()
    clean = buffer.feed(raw)
    say(f"[缓冲层] feed 返回 {len(clean)} 字符，缓冲 {len(buffer._lines)} 行，"
        f"含正文={'H3C Comware' in buffer.text()}")
    say(f"[缓冲层] feed 开头 120: {clean[:120]!r}")
    say(f"[缓冲层] 原数据开头 120: {raw[:120]!r}")
    say(f"[缓冲层] 结尾 120: {raw[-120:]!r}")

    # ④ 找出"被 del 掉"的那一段：逐字符喂缓冲，记录每步行数变化
    fresh = TerminalBuffer()
    drops = []
    for index, ch in enumerate(raw, 1):
        before = list(fresh._lines)
        emitted = fresh.feed(ch)
        after = list(fresh._lines)
        if emitted == "" and ch not in "\r\n" and before and after == before:
            drops.append((index, repr(ch)))
    say(f"[缓冲层] 被吃掉的可打印字符 {len(drops)} 个，前 8 个位置: {drops[:8]}")

    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    say(f"报告: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

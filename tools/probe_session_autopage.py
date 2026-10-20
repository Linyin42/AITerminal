"""真机验证"交互会话自动翻页"：故意打开分屏，看能不能无人值守翻完

模拟的是界面 update_output 的逻辑（同一张 PAGING_RE、同一个"最后 40 字符"判定、
同样的"用户没在打字才发空格"边界），只是把显示换成计数。
判定成功 = ①真的替用户发过空格 ②整段输出收全了 ③设备最后回到提示符。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.batch import CommandWaiter                             # noqa: E402
from core.telnet_connector import TelnetConnector                 # noqa: E402

PAGING_RE = CommandWaiter.PAGING_RE

REPORT = os.path.join(os.environ.get("TEMP", "."), "probe_session_autopage.txt")
LINES = []


def say(text=""):
    LINES.append(str(text))
    try:
        print(text)
    except Exception:                                             # noqa: BLE001
        pass


def run(host, username, password, paging_on, long_command):
    conn = TelnetConnector()
    if not conn.connect(host, username, password, port=23, timeout=10):
        say("连接失败")
        return False
    try:
        time.sleep(0.4)
        conn.get_output()                      # 丢掉登录横幅
        # ① 故意打开分屏（每 24 行停一次），确认后续不靠人也能翻完
        conn.send(paging_on)
        time.sleep(0.6)
        banner = conn.get_output()
        say(f"  打开分屏: {paging_on!r} -> {banner.strip()[:60]!r}")

        # ② 跑一条长输出命令，用界面同一套逻辑应答分页
        conn.send(long_command)
        collected = ""
        tail = ""
        spaces = 0
        prompt_back = False
        started = time.time()
        while time.time() - started < 25:
            chunk = conn.get_output()
            if chunk:
                collected += chunk
                tail = (tail + chunk)[-240:]
                if PAGING_RE.search(tail[-40:]):
                    tail = ""
                    spaces += 1
                    conn.send_raw(" ")
            if spaces and re.search(r'[>\]#]\s*$', collected.rstrip()[-40:]):
                prompt_back = True
                break
            time.sleep(0.1)
        elapsed = time.time() - started
        lines = len(collected.splitlines())
        say(f"  命令 {long_command!r}: 自动翻页 {spaces} 次  用时 {elapsed:.1f}s  "
            f"收到 {len(collected)} 字节 / {lines} 行  回到提示符={prompt_back}")
        head = [ln for ln in collected.splitlines() if ln.strip()][:3]
        say(f"  输出开头: {head}")
        ok = spaces > 0 and lines > 24 and prompt_back
        say(f"  结论: {'PASS' if ok else 'FAIL'}")
        return ok
    finally:
        try:
            conn.disconnect()
        except Exception:                                         # noqa: BLE001
            pass


if __name__ == "__main__":
    say(f"验证时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say("【H3C S7503X】分屏 24 行 + display version")
    ok = run("10.2.0.1", "admin", "AB403yw@8917", "screen-length 24", "display version")
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    say(f"报告: {REPORT}")
    sys.exit(0 if ok else 1)

# -*- coding: utf-8 -*-
"""自动 enable / 用户模式识别 的单元测试（假连接器，不占串口）。

覆盖：
  - 提示符分类：`Ruijie>` 是用户模式；`Ruijie#`、`<H3C>`、`[H3C]` 都不是
  - 已在特权模式：不发 enable
  - 用户模式 + 无密码：发 enable 后到 `Ruijie#` → 成功
  - 用户模式 + 要密码：先 enable 再发密码 → 成功
  - 要密码但没填：失败并给出明确说明
  - 密码错：失败
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
REPO = r"C:\Users\linyin\WorkBuddy AI\2026-09-21-18-54-37\aiterminal"
sys.path.insert(0, REPO)

from core.batch import CommandWaiter, in_user_mode, ensure_privileged, current_prompt

RESULTS = []


def check(name, passed, evidence=""):
    RESULTS.append((name, bool(passed)))
    print("  [%s] %s%s" % ("PASS" if passed else "FAIL", name, ("  | " + evidence) if evidence else ""))


class FakeConnector:
    """最小的连接器替身：send 时按脚本回显，监听器同步回调（和真连接器一样）。"""

    def __init__(self, script):
        self.script = list(script)      # [(收到的命令, 回显文本)]
        self._listeners = []
        self.sent = []

    def add_output_listener(self, cb):
        self._listeners.append(cb)

    def remove_output_listener(self, cb):
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    def is_connected(self):
        return True

    def send(self, command):
        self.sent.append(command)
        reply = ""
        for i, (want, out) in enumerate(self.script):
            if want == command:
                reply = out
                self.script.pop(i)
                break
        for cb in list(self._listeners):
            if reply:
                cb(reply)

    def send_raw(self, data):
        self.send(data)


# ---------------------------------------------------------------- 提示符分类
print("=== 提示符分类 ===")
check("Ruijie> 是用户模式", in_user_mode("Ruijie>"))
check("Switch> 是用户模式", in_user_mode("Switch>"))
check("Ruijie# 不是用户模式", not in_user_mode("Ruijie#"))
check("<H3C> 不是用户模式（华为没有 enable 层）", not in_user_mode("<H3C>"))
check("[H3C-GE1/0/1] 不是用户模式", not in_user_mode("[H3C-GE1/0/1]"))
check("空提示符不算用户模式", not in_user_mode(""))

# ---------------------------------------------------------------- 已在特权模式
print("\n=== 已在特权模式：不该发 enable ===")
fc = FakeConnector([("", "\r\nRuijie#")])
w = CommandWaiter(fc, quiet_time=0.2, timeout=3); w.start()
ok, prompt, note = ensure_privileged(fc, w)
w.stop()
check("直接返回成功", ok and prompt == "Ruijie#", note)
check("没有发 enable", "enable" not in fc.sent, str(fc.sent))

# ---------------------------------------------------------------- 无密码 enable
print("\n=== 用户模式 → enable（无密码） ===")
fc = FakeConnector([("", "\r\nRuijie>"), ("enable", "\r\nRuijie#"), ("", "\r\nRuijie#")])
w = CommandWaiter(fc, quiet_time=0.2, timeout=3); w.start()
ok, prompt, note = ensure_privileged(fc, w)
w.stop()
check("切到特权模式", ok and prompt == "Ruijie#", note)
check("发过 enable", "enable" in fc.sent, str(fc.sent))

# ---------------------------------------------------------------- 要密码
print("\n=== 用户模式 → enable（要密码） ===")
fc = FakeConnector([
    ("", "\r\nRuijie>"),
    ("enable", "\r\nPassword:"),
    ("s3cret", "\r\nRuijie#"),
    ("", "\r\nRuijie#"),
])
w = CommandWaiter(fc, quiet_time=0.2, timeout=3, silence_timeout=0.5); w.start()
ok, prompt, note = ensure_privileged(fc, w, password="s3cret")
w.stop()
check("密码被发出并进入特权模式", ok and "s3cret" in fc.sent, note)

# ---------------------------------------------------------------- 要密码但没填
print("\n=== 要密码但没填 → 明确失败 ===")
fc = FakeConnector([("", "\r\nRuijie>"), ("enable", "\r\nPassword:")])
w = CommandWaiter(fc, quiet_time=0.2, timeout=3, silence_timeout=0.5); w.start()
ok, prompt, note = ensure_privileged(fc, w, password="")
w.stop()
check("失败并提示填密码", (not ok) and "密码" in note, note)

# ---------------------------------------------------------------- 密码错
print("\n=== 密码错 → 失败 ===")
fc = FakeConnector([
    ("", "\r\nRuijie>"),
    ("enable", "\r\nPassword:"),
    ("wrong", "\r\nPassword:"),
    ("", "\r\nRuijie>"),
])
w = CommandWaiter(fc, quiet_time=0.2, timeout=3, silence_timeout=0.5); w.start()
ok, prompt, note = ensure_privileged(fc, w, password="wrong")
w.stop()
check("密码错时返回失败", not ok, note)

ok_n = sum(1 for _n, p in RESULTS if p)
print("\n=== 合计 PASS %d / FAIL %d ===" % (ok_n, len(RESULTS) - ok_n))
sys.exit(0 if ok_n == len(RESULTS) else 1)

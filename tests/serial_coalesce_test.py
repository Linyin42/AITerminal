"""fix-A（串口读「帧内合并」窗口）的离线单测。

为什么需要它：fix-A 修的是**时序**问题 —— 9600 波特下 7 字符的 `Ruijie>`
要 7.3 ms 才发完，而读线程 5 ms 轮询一次，于是提示符被切成 `Ruij` + `ie>`
两块上屏。第一块在行首、不含 `\\w+[>#]`，被判成正文色涂白，且不会回刷 ——
真机症状是"提示符有时候变白"。

这种缺陷**单测很难覆盖**（要真串口），所以这里用一个按时间表放字节的假串口，
把"分块到达"这件事确定性地重放出来，直接验证 `_read_chunk` 有没有把同一帧攒齐。

用法（**必须用系统 Python**，core 依赖 PyQt6）：
    "C:/Users/linyin/AppData/Local/Programs/Python/Python313/python.exe" \
        tests/serial_coalesce_test.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.serial_connector import SerialConnector

PASS, FAIL = [], []


def check(name: str, cond: bool, why: str = ""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
    if not cond and why:
        print("         %s" % why)


class FakeSerial:
    """按时间表放出字节的假串口。

    schedule: [(相对开测时刻的秒数, 字节串), ...]，到点后这些字节才"到达"。
    这样就能确定性地重放"提示符被切成两块"的时序，不需要真设备。
    """

    def __init__(self, schedule):
        self.schedule = list(schedule)
        self.t0 = time.monotonic()
        self.is_open = True
        self._pending = b""

    def _pump(self):
        now = time.monotonic() - self.t0
        while self.schedule and self.schedule[0][0] <= now:
            self._pending += self.schedule.pop(0)[1]

    @property
    def in_waiting(self) -> int:
        self._pump()
        return len(self._pending)

    def read(self, n: int) -> bytes:
        self._pump()
        out = self._pending[:n]
        self._pending = self._pending[n:]
        return out


def make_connector(fake) -> SerialConnector:
    """造一个只带假串口的连接器（不碰真端口、不起读线程）。"""
    conn = SerialConnector.__new__(SerialConnector)
    conn.ser = fake
    return conn


def main() -> int:
    print("=" * 74)
    print("fix-A 单测：串口读「帧内合并」窗口")
    print("=" * 74)
    silence = SerialConnector._COALESCE_SILENCE
    hard_max = SerialConnector._COALESCE_MAX
    print("  合并静默窗口 = %.3fs，硬上限 = %.3fs" % (silence, hard_max))

    # ---- 用例 1：提示符被切成两段（真机就是这个时序）----
    print("\n[1] `Ruijie>` 被切成 `Ruij` + `ie>` 两块（间隔 7ms）")
    fake = FakeSerial([(0.0, b"Ruij"), (0.007, b"ie>")])
    conn = make_connector(fake)
    t0 = time.monotonic()
    data = conn._read_chunk(fake.in_waiting)
    dt = time.monotonic() - t0
    check("两段被合并成一帧", data == b"Ruijie>",
          "期望 b'Ruijie>'，实际 %r（合并窗口没生效，半截提示符会单独上屏）" % data)
    check("合并耗时在静默窗口与硬上限之间", silence <= dt <= hard_max + 0.02,
          "实际 %.3fs（太快=没等，太慢=长输出会被一直攒着）" % dt)

    # ---- 用例 2：正常整帧到达，不该被拖太久 ----
    print("\n[2] 整帧一次到达（`<H3C>`）")
    fake = FakeSerial([(0.0, b"<H3C>")])
    conn = make_connector(fake)
    t0 = time.monotonic()
    data = conn._read_chunk(fake.in_waiting)
    dt = time.monotonic() - t0
    check("整帧原样返回", data == b"<H3C>", "实际 %r" % data)
    check("没有白等超过静默窗口", dt <= silence + 0.03,
          "实际 %.3fs —— 整帧也要等满静默窗口才放行，但不能超太多" % dt)

    # ---- 用例 3：持续输出必须被硬上限截断（否则界面会卡住不出字）----
    print("\n[3] 持续输出 200ms：必须被硬上限截断，不能一直攒")
    sched = [(i * 0.010, b"x" * 40) for i in range(20)]     # 每 10ms 来 40 字节
    fake = FakeSerial(sched)
    conn = make_connector(fake)
    t0 = time.monotonic()
    data = conn._read_chunk(fake.in_waiting)
    dt = time.monotonic() - t0
    check("在硬上限附近返回", dt <= hard_max + 0.05,
          "实际 %.3fs（超过 %.3fs 说明长输出会被一直攒着不上屏）" % (dt, hard_max))
    check("确实收到了数据", len(data) > 0, "实际 %d 字节" % len(data))
    check("没有把 200ms 的输出全攒完", len(data) < 20 * 40,
          "实际 %d 字节 —— 攒完了说明硬上限没生效" % len(data))

    print("\n" + "=" * 74)
    print("共 %d 项，通过 %d 项，失败 %d 项" % (len(PASS) + len(FAIL), len(PASS), len(FAIL)))
    for f in FAIL:
        print("   - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

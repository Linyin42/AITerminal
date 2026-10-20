r"""eNSP 连接器掉线自愈的离线验证（不需要真机）。

跑法：python -u tools\probe_ensp_selfheal.py
判据：① 从未连过时 ensure_connected() 返回 False 且给出原因（不抛异常）；
     ② 掉线后 send_raw() 会尝试重连、失败时把原因写进 _last_error（以前是静默丢弃）；
     ③ 连不上的重连失败不会留下半连接状态（is_connected() 仍为 False）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ensp_connector import EnspProConnector                # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


print("=== ① 从未连接过：ensure_connected 要如实说不 ===")
conn = EnspProConnector()
try:
    ok = conn.ensure_connected()
    check("返回 False（没参数可复用）", ok is False, f"last_error={conn._last_error!r}")
    check("给出了可读原因", bool(conn._last_error))
except Exception as e:                                          # noqa: BLE001
    check("不抛异常", False, f"{type(e).__name__}: {e}")

print("=== ② 掉线后发命令：要尝试重连并把原因留下 ===")
conn2 = EnspProConnector()
conn2._connect_kwargs = {"host": "127.0.0.1", "port": 8443, "sandbox_id": 1, "node_id": 1}
conn2._connected = False
try:
    conn2.send_raw("display version")
    check("send_raw 不抛异常", True)
    check("留下了失败原因（不再静默）", bool(conn2._last_error),
          f"last_error={conn2._last_error!r}")
    check("③ 失败后没留下半连接状态", conn2.is_connected() is False,
          f"is_connected={conn2.is_connected()}")
except Exception as e:                                          # noqa: BLE001
    check("send_raw 不抛异常", False, f"{type(e).__name__}: {e}")

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
raise SystemExit(1 if FAIL else 0)

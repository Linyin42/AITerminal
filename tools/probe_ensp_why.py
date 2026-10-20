"""最小诊断：直接调 eNSP 连接器，把 connect 失败的真实原因打出来。

探针 probe_ensp_drop.py 只印 create_session -> -1，看不到根因；
这里绕过 session_manager，直接拿连接器的 last_error / info。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ensp_connector import EnspProConnector  # noqa: E402

HOST = os.environ.get("ENSP_HOST", "192.168.31.11")
SB = int(os.environ.get("ENSP_SB", "4"))
NODE = int(os.environ.get("ENSP_NODE", "28"))
USER = os.environ.get("ENSP_USER", "admin")
PWD = os.environ.get("ENSP_PWD", "Huawei@123")

print(f"=== 连接器直连诊断：{HOST} 沙箱{SB} 设备{NODE} ===", flush=True)
c = EnspProConnector()
t0 = time.monotonic()
try:
    ok = c.connect(host=HOST, port=8443, username=USER, password=PWD,
                   sandbox_id=SB, node_id=NODE)
    print(f"connect -> {ok}（用时 {time.monotonic() - t0:.1f}s）", flush=True)
except Exception as e:
    print(f"connect 抛异常：{type(e).__name__}: {e}", flush=True)
    ok = None

print(f"last_error: {getattr(c, 'last_error', None)!r}", flush=True)
try:
    print(f"info: {c.info()}", flush=True)
except Exception as e:
    print(f"info 抛异常：{type(e).__name__}: {e}", flush=True)

if ok:
    try:
        # 注意：send_command() **返回**输出并在内部清空缓冲区，
        # 所以必须打印它的返回值；再调 get_output() 只会拿到空串（探针原来就是这么写错的）。
        out = c.send_command("display version", timeout=25) or ""
        print(f"send_command 返回 {len(out)} 字符：", flush=True)
        print(out[:800], flush=True)
        extra = c.get_output() or ""
        if extra:
            print(f"（缓冲区另有 {len(extra)} 字符）", flush=True)
    except Exception as e:
        print(f"发命令失败：{type(e).__name__}: {e}", flush=True)
    finally:
        try:
            c.disconnect()
        except Exception:
            pass

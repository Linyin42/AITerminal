"""按应用自己的路径连真机，插桩记录登录阶段每一次 write —— 定位"提示密码错误"

和 probe_real_patrol.py 的区别：那个直接用 TelnetConnector；这个走
SessionManager.create_session（界面点"连接"走的就是它），所以能看出应用层有没有
多写一次、或者在不同时机写。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication                    # noqa: E402

REPORT = os.path.join(os.environ.get("TEMP", "."), "probe_app_connect.txt")
LINES = []


def say(text=""):
    LINES.append(str(text))
    try:
        print(text)
    except Exception:                                        # noqa: BLE001
        pass


def flush():
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")


def run_case(title, config):
    from core.session import SessionManager

    say("=" * 70)
    say(f"【应用路径】{title}  config={ {k: v for k, v in config.items() if k != 'password'} }"
        f"  密码长度={len(config.get('password') or '')}")

    manager = SessionManager()
    created = []
    errors = []
    manager.session_created.connect(lambda sid, name: created.append(sid))
    manager.session_error.connect(lambda sid, msg: errors.append(msg))
    manager.connection_info.connect(lambda sid, msg: say(f"  [info] {msg}"))

    t0 = time.time()
    session_id = manager.create_session(**config)
    cost = time.time() - t0
    say(f"  create_session -> id={session_id}  耗时={cost:.2f}s")
    if errors:
        say(f"  会话错误: {errors}")
    session = manager.get_session(session_id) if session_id > 0 else None
    if session and session.connector:
        writes = getattr(session.connector, "_probe_writes", None)
        say(f"  登录阶段写入序列: {writes}")
        raw = getattr(session.connector, "_login_buffer", b"") or b""
        say(f"  登录阶段设备回话 {len(raw)} 字节:")
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                say(f"    | {line}")
    try:
        manager.close_all()
    except Exception:                                        # noqa: BLE001
        pass
    flush()
    return cost


if __name__ == "__main__":
    app = QCoreApplication.instance() or QCoreApplication([])

    # 给 TelnetConnector._open_socket 打桩：记录每一次 write
    from core import telnet_connector as tc

    orig_open = tc.TelnetConnector._open_socket
    t_start = time.time()

    def patched_open(host, port, timeout):
        tn = orig_open(host, port, timeout)
        writes = []
        orig_write = tn.write

        def write_wrapped(data):
            writes.append((round(time.time() - t_start, 2), bytes(data)))
            return orig_write(data)

        tn.write = write_wrapped
        tc.TelnetConnector._last_probe_writes = writes
        return tn

    tc.TelnetConnector._open_socket = staticmethod(patched_open)

    # create_session 之后把写入序列挂到实例上（_open_socket 是静态方法，拿不到实例）
    orig_create = tc.TelnetConnector.connect

    def connect_wrapped(self, *args, **kwargs):
        ok = orig_create(self, *args, **kwargs)
        self._probe_writes = getattr(tc.TelnetConnector, "_last_probe_writes", [])
        return ok

    tc.TelnetConnector.connect = connect_wrapped

    say(f"复现时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    run_case("华为 S12708（用户报：提示密码错误）", {
        "name": "华为-10.3.8.3", "protocol": "Telnet", "host": "10.3.8.3",
        "port": 23, "username": "", "password": "admin@123",
    })
    run_case("H3C S7503X（对照，应一次成功）", {
        "name": "H3C-S7503X", "protocol": "Telnet", "host": "10.2.0.1",
        "port": 23, "username": "admin", "password": "AB403yw@8917",
    })
    flush()
    say(f"\n报告已写入 {REPORT}")
    del app

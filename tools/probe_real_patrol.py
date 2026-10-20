"""对着真机复现"巡检慢 / 回显没数据 / 登录提示密码错误"，把证据写进报告

为什么要这么测：用户在界面上看到的是"0% 不动""---- More ---- 后面没数据""登录时说
密码无效"，但界面只是结果。真正要回答的是：
  · 登录阶段到底往设备写了什么、写了几次、时间点在哪（密码错误是不是我们多发了一个回车）
  · 巡检每条命令的耗时、结束状态（prompt/silence/timeout）、翻页次数、拿到多少字节
  · 长输出命令（display version）在分页设备上到底能不能自己翻下去

跑法（工程根）：
    .venv-train\\Scripts\\python.exe tools\\probe_real_patrol.py
报告：%TEMP%\\probe_real_patrol.txt
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORT = os.path.join(os.environ.get("TEMP", "."), "probe_real_patrol.txt")
LINES = []


def say(text=""):
    LINES.append(str(text))
    try:
        print(text)
    except Exception:                                   # noqa: BLE001
        pass


def flush():
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")


# --------------------------------------------------------------- 登录取证
def probe_login(title, host, username, password, port=23):
    """把 TelnetConnector 的每一次 write 都记下来，看它到底给设备发了什么"""
    from core.telnet_connector import TelnetConnector

    say("=" * 70)
    say(f"【登录取证】{title}  {host}:{port}  用户名={username!r}  密码长度={len(password)}")
    conn = TelnetConnector()
    log = []
    orig_write = conn._open_socket

    def wrapped_open(h, p, timeout):
        tn = orig_write(h, p, timeout)
        orig_w = tn.write

        def write_wrapped(data):
            log.append((round(time.time() - t0, 2), bytes(data)))
            return orig_w(data)

        tn.write = write_wrapped
        return tn

    t0 = time.time()
    conn._open_socket = wrapped_open
    try:
        ok = conn.connect(host, username, password, port=port, timeout=10)
        cost = time.time() - t0
        say(f"  结果: connect={ok}  用时={cost:.2f}s")
        say("  写入序列（时刻, 内容）:")
        for ts, data in log:
            say(f"    +{ts:>5.2f}s  {data!r}")
        raw = conn._login_buffer or b""
        say(f"  登录阶段设备回话 {len(raw)} 字节（原文，按 \\r\\n 断行）:")
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                say(f"    | {line}")
        if ok:
            # 登录后跑一条命令，看交互路径通不通
            from core.batch import CommandWaiter
            waiter = CommandWaiter(conn, quiet_time=0.3, timeout=30,
                                   silence_timeout=2.0, auto_page=True)
            waiter.start()
            status, elapsed, output = waiter.run("display version" if "ZF-" in title
                                                 else "display version")
            waiter.stop()
            say(f"  [交互] display version -> 状态={status} 耗时={elapsed:.1f}s "
                f"翻页={waiter.pages} 输出={len(output)} 字节")
            say("  输出开头 300 字:")
            for line in output[:300].splitlines():
                say(f"    | {line}")
        return log
    except Exception as exc:                            # noqa: BLE001
        say(f"  异常: {type(exc).__name__}: {exc}")
        return log
    finally:
        try:
            conn.disconnect()
        except Exception:                               # noqa: BLE001
            pass
        flush()


# --------------------------------------------------------------- 巡检取证
def probe_patrol(title, config, commands):
    from core.patrol import patrol_device

    say("=" * 70)
    say(f"【巡检取证】{title}  命令 {len(commands)} 条")
    marks = []

    def on_command(index, command):
        marks.append((round(time.time() - t0, 2), index, command))
        say(f"    +{marks[-1][0]:>6.2f}s  第 {index} 条: {command}")

    t0 = time.time()
    result = patrol_device(config, commands, timeout=10.0, prompt_timeout=30.0,
                           quiet_time=0.3, silence_timeout=2.0, auto_page=True,
                           block_dangerous=True, on_command=on_command,
                           on_note=lambda text: say(f"    +{time.time() - t0:>6.2f}s  [note] {text}"))
    total = time.time() - t0
    say(f"  总耗时 {total:.1f}s  ok={result['ok']} error={result['error']!r} "
        f"翻页合计={result['pages']}")
    for i, item in enumerate(result["commands"], 1):
        out = item.get("output") or ""
        first = next((ln for ln in out.splitlines() if ln.strip()), "")
        say(f"  第{i}条 {item['command']!r}: 状态={item['status']} "
            f"耗时={item['elapsed']:.1f}s 输出={len(out)}字节 首行={first[:70]!r}")
    return result


if __name__ == "__main__":
    say(f"复现时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # ① 华为 10.3.8.3：只给密码（用户报"连接后提示密码错误，且有点慢"）
    probe_login("华为 S12708", "10.3.8.3", "", "admin@123")

    # ② H3C 10.2.0.1：用户名 + 密码，跑一条长输出命令看分页
    probe_login("H3C S7503X", "10.2.0.1", "admin", "AB403yw@8917")

    # ③ 巡检：同一台 H3C，三条常见命令
    probe_patrol("H3C S7503X", {
        "name": "H3C-S7503X", "protocol": "Telnet", "host": "10.2.0.1",
        "port": 23, "username": "admin", "password": "AB403yw@8917",
    }, ["display mac-address", "display cpu-usage", "display logbuffer"])

    flush()
    say(f"\n报告已写入 {REPORT}")

"""
设备连通性与命令验证 - 在打开图形界面之前，先用命令行把链路跑通

它会依次验证:
    1. 连接是否成功（SSH / Telnet / Serial）
    2. 设备是否有回显、提示符是否可识别
    3. 厂商识别（华为 / H3C / Cisco / 锐捷）
    4. 关闭分屏命令是否生效
    5. 指定命令能否"等提示符返回"地执行完，并打印输出

密码建议用交互输入（不回显、不留在命令历史里）:
    python tools/verify_device.py --protocol ssh --host 10.0.0.1 --user admin
也可以从环境变量读:
    set AITERM_PASSWORD=xxx   (Windows)
    export AITERM_PASSWORD=xxx
    python tools/verify_device.py --protocol ssh --host 10.0.0.1 --user admin --password-env AITERM_PASSWORD

用法示例:
    # SSH
    python tools/verify_device.py --protocol ssh --host 10.0.0.1 --user admin

    # Telnet
    python tools/verify_device.py --protocol telnet --host 10.0.0.1 --user admin

    # Console 串口
    python tools/verify_device.py --protocol serial --serial-port COM3 --baudrate 9600

    # 自定义命令
    python tools/verify_device.py --protocol ssh --host 10.0.0.1 --user admin \
        --commands "display version,display interface brief,display cpu-usage"

    # 连本项目的模拟设备（无需真实设备）
    python tools/fake_device.py --no-login
    python tools/verify_device.py --protocol telnet --host 127.0.0.1 --port 2323
"""
import argparse
import getpass
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.batch import CommandWaiter              # noqa: E402
from core.terminal import detect_vendor           # noqa: E402
from core.ai_agent import AIAgent                 # noqa: E402
from core.ssh_connector import SSHConnector       # noqa: E402
from core.telnet_connector import TelnetConnector  # noqa: E402
from core.serial_connector import SerialConnector  # noqa: E402

DEFAULT_COMMANDS = ["display version", "display interface brief", "display cpu-usage"]

STATUS_TEXT = {
    "prompt": "完成（提示符返回）",
    "silence": "完成（输出静默）",
    "delay": "已发送（固定间隔）",
    "confirm": "需人工确认",
    "timeout": "超时未完成",
    "blocked": "安全拦截",
    "disconnected": "连接断开",
}

OK_STATUSES = ("prompt", "silence", "delay")


def parse_args():
    parser = argparse.ArgumentParser(
        description="验证设备连通性与命令执行（NetOpsTerminal）",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--protocol", default="ssh",
                        choices=["ssh", "telnet", "serial"], help="连接方式")
    parser.add_argument("--host", help="设备地址（SSH/Telnet）")
    parser.add_argument("--port", type=int, help="端口（SSH 默认22，Telnet 默认23）")
    parser.add_argument("--user", default="", help="用户名")
    parser.add_argument("--password", default="", help="密码（不推荐，会留在命令历史里）")
    parser.add_argument("--password-env", default="", help="从该环境变量读取密码")
    parser.add_argument("--key", default="", help="SSH 私钥文件路径")

    parser.add_argument("--serial-port", default="", help="串口号，如 COM3 或 /dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=9600, help="串口波特率")
    parser.add_argument("--parity", default="N", help="校验位 N/E/O")
    parser.add_argument("--bytesize", type=int, default=8, help="数据位")
    parser.add_argument("--stopbits", type=float, default=1, help="停止位")
    parser.add_argument("--flow", default="none", help="流控 none/rtscts/xonxoff")

    parser.add_argument("--commands", default="",
                        help="要执行的命令，逗号分隔；留空则用默认巡检命令")
    parser.add_argument("--timeout", type=float, default=10, help="连接超时秒数")
    parser.add_argument("--prompt-timeout", type=float, default=30,
                        help="单条命令等待提示符的最长秒数")
    parser.add_argument("--delay-mode", action="store_true",
                        help="用固定间隔而不是等提示符")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="固定间隔模式下的等待秒数")
    parser.add_argument("--no-prepare", action="store_true",
                        help="不自动执行关闭分屏的命令")
    parser.add_argument("--allow-dangerous", action="store_true",
                        help="允许下发 reboot/delete 等危险命令")
    parser.add_argument("--show-lines", type=int, default=12,
                        help="每条命令最多显示多少行输出")
    parser.add_argument("--list-ports", action="store_true",
                        help="只列出本机串口然后退出")
    return parser.parse_args()


def resolve_password(args) -> str:
    """按优先级取密码：命令行 > 环境变量 > 交互输入"""
    if args.password:
        print("! 提示：命令行传密码会留在 shell 历史里，建议改用交互输入")
        return args.password
    if args.password_env:
        value = os.environ.get(args.password_env, "")
        if not value:
            print(f"! 环境变量 {args.password_env} 为空")
        return value
    # 串口不需要密码；没给用户名时按"无需登录"处理（避免非交互环境下卡住）
    if args.protocol == "serial" or not args.user:
        return ""
    if not sys.stdin.isatty():
        print("! 当前不是交互终端，且未提供密码，将按空密码尝试连接")
        return ""
    try:
        return getpass.getpass("请输入密码（不回显）: ")
    except (KeyboardInterrupt, EOFError):
        print("\n已取消")
        sys.exit(1)


def print_step(index: int, title: str):
    print()
    print(f"── 步骤 {index}：{title} " + "─" * max(0, 44 - len(title)))


def build_connector(args):
    if args.protocol == "ssh":
        return SSHConnector()
    if args.protocol == "telnet":
        return TelnetConnector()
    return SerialConnector()


def do_connect(connector, args, password: str):
    """建立连接，返回 (成功, 错误信息)"""
    errors = []
    connector.error_occurred.connect(errors.append)
    started = time.time()
    if args.protocol == "ssh":
        ok = connector.connect(
            host=args.host, username=args.user, password=password,
            port=args.port or 22, key_filename=args.key or None,
            timeout=int(args.timeout),
        )
    elif args.protocol == "telnet":
        ok = connector.connect(
            host=args.host, username=args.user, password=password,
            port=args.port or 23, timeout=int(args.timeout),
        )
    else:
        ok = connector.connect(
            port=args.serial_port, baudrate=args.baudrate,
            bytesize=args.bytesize, parity=args.parity,
            stopbits=args.stopbits, flow=args.flow,
            line_ending='\r', auto_reconnect=False,
        )
    elapsed = time.time() - started
    return ok, (errors[0] if errors else ""), elapsed


def probe_vendor(connector, waiter, is_serial: bool):
    """探测厂商：先按 H3C/华为 试，识别到 Cisco 再换 show"""
    if is_serial:
        connector.send_raw('\r')      # Console 先敲个回车唤醒设备
        time.sleep(0.5)

    status, elapsed, output = waiter.run("display version")
    vendor = detect_vendor(output)

    if vendor == "cisco":
        status, elapsed, output = waiter.run("show version")
        vendor = detect_vendor(output) or "cisco"

    return vendor, status, elapsed, output


def disable_paging(connector, waiter, vendor: str):
    """
    按厂商关闭分屏，避免长输出被 ---- More ---- 卡住

    Comware 5 用 screen-length disable，Comware 7 用 screen-length 0 temporary，
    用错设备会回 % Unrecognized command，所以这里逐条尝试并检查是否被拒绝。
    """
    from core.batch import disable_paging as _disable_paging
    return _disable_paging(connector, waiter=waiter, vendor=vendor)


def run_command(waiter, connector, command: str, args, index: int, total: int):
    """执行一条命令并打印结果"""
    print(f"  [{index}/{total}] {command}")
    if not args.allow_dangerous and AIAgent.is_dangerous(command):
        print("        ⊘ 安全拦截（危险命令，加 --allow-dangerous 可强制下发）")
        return {"command": command, "status": "blocked", "elapsed": 0.0, "output": ""}

    if args.delay_mode:
        started = time.time()
        connector.send(command)
        time.sleep(args.delay)
        status, elapsed, output = "delay", time.time() - started, ""
    else:
        status, elapsed, output = waiter.run(command)

    print(f"        → {STATUS_TEXT.get(status, status)}，耗时 {elapsed:.1f}s"
          + (f"，自动翻页 {waiter.pages} 次" if waiter.pages else ""))

    lines = [ln for ln in output.splitlines() if ln.strip()]
    for line in lines[:args.show_lines]:
        print(f"        │ {line[:110]}")
    if len(lines) > args.show_lines:
        print(f"        │ …… 其余 {len(lines) - args.show_lines} 行（共 {len(lines)} 行）")
    if not lines:
        print("        │ （没有捕获到输出）")

    return {"command": command, "status": status, "elapsed": elapsed, "output": output}


def main():
    args = parse_args()

    if args.list_ports:
        ports = SerialConnector.list_ports_detailed()
        print("本机串口:")
        if not ports:
            print("  （未检测到串口，检查驱动是否安装）")
        for port in ports:
            print(f"  {port['device']}  {port['description']}")
        return 0

    # 参数检查
    if args.protocol == "serial":
        if not args.serial_port:
            print("× 串口模式需要 --serial-port COM3")
            return 1
        target = f"{args.serial_port} @ {args.baudrate}-{args.bytesize}" \
                 f"{args.parity}{args.stopbits}"
    else:
        if not args.host:
            print("× 需要 --host 设备地址")
            return 1
        target = f"{args.host}:{args.port or (22 if args.protocol == 'ssh' else 23)}"

    password = resolve_password(args)
    commands = [c.strip() for c in (args.commands or "").split(',') if c.strip()] \
        or list(DEFAULT_COMMANDS)

    print("=" * 62)
    print(f"NetOpsTerminal 设备验证  协议={args.protocol.upper()}  目标={target}")
    print(f"用户名={args.user or '(无)'}  等待方式="
          f"{'固定间隔 %.1fs' % args.delay if args.delay_mode else '等提示符返回'}")
    print(f"将执行 {len(commands)} 条命令: {', '.join(commands)}")
    print("=" * 62)

    # ---------------- 步骤 1：连接
    print_step(1, "建立连接")
    connector = build_connector(args)
    ok, error, elapsed = do_connect(connector, args, password)
    if not ok:
        print(f"× 连接失败（{elapsed:.1f}s）: {error}")
        print()
        print("排查建议：")
        if args.protocol == "ssh":
            print("  · 确认地址端口可达：telnet <IP> 22")
            print("  · 认证失败：检查用户名密码；设备可能只允许特定来源 IP 登录")
            print("  · 设备较老：可能需要启用 ssh server 或改用 Telnet")
        elif args.protocol == "telnet":
            print("  · 确认设备已开启 telnet server，且允许本机 IP 登录")
            print("  · 确认端口（部分设备改成 2323 等非标准端口）")
        else:
            print("  · 确认串口未被 MobaXterm / SecureCRT 等占用")
            print("  · 确认波特率（9600 或 115200）与线序正确")
        return 1
    print(f"√ 连接成功，耗时 {elapsed:.1f}s")

    waiter = CommandWaiter(
        connector,
        quiet_time=0.3,
        timeout=args.prompt_timeout,
        silence_timeout=2.0,
        auto_page=True,
    )
    waiter.start()

    results = []
    try:
        # ---------------- 步骤 2：厂商识别
        print_step(2, "识别设备厂商（发一条 display version）")
        vendor, status, elapsed, output = probe_vendor(
            connector, waiter, args.protocol == "serial")
        if vendor == "unknown":
            print(f"? 未能识别厂商（等待结果：{STATUS_TEXT.get(status, status)}）")
            print("  可能是提示符不标准或设备未回显；后续命令仍会继续尝试")
        else:
            print(f"√ 识别为 {vendor.upper()}（{STATUS_TEXT.get(status, status)}，"
                  f"耗时 {elapsed:.1f}s）")
        if output.strip():
            for line in output.splitlines()[:3]:
                if line.strip():
                    print(f"    │ {line[:110]}")

        # ---------------- 步骤 3：关闭分屏
        if not args.no_prepare and vendor != "unknown":
            print_step(3, "关闭分屏（避免长输出卡在 ---- More ----）")
            ok, command, response = disable_paging(connector, waiter, vendor)
            if ok:
                first_line = next((l.strip() for l in response.splitlines()
                                   if l.strip() and l.strip() != command), "")
                print(f"  √ 已生效: {command}")
                if first_line:
                    print(f"    │ {first_line[:100]}")
            else:
                print("  ! 未能关闭分屏（设备可能不认这些命令），"
                      "长输出会靠自动翻页处理")
                if response:
                    print(f"    │ {response.splitlines()[-1][:100]}")
        else:
            print_step(3, "关闭分屏")
            print("  已跳过（未识别厂商或指定了 --no-prepare）")

        # ---------------- 步骤 4：执行命令
        print_step(4, f"执行 {len(commands)} 条命令")
        for index, command in enumerate(commands, start=1):
            results.append(run_command(waiter, connector, command, args, index, len(commands)))
            if results[-1]["status"] == "disconnected":
                print("  × 连接已断开，停止后续命令")
                break
            if results[-1]["status"] == "confirm":
                print("  ! 设备要求人工确认，已停止后续命令"
                      "（这是有意的安全设计，不会替你按 Y）")
                break
    finally:
        waiter.stop()
        try:
            connector.disconnect()
        except Exception:
            pass

    # ---------------- 汇总
    print()
    print("=" * 62)
    print("验证结果汇总")
    print("=" * 62)
    print(f"{'#':<3}{'命令':<34}{'结果':<20}{'耗时':>7}")
    print("-" * 62)
    for index, item in enumerate(results, start=1):
        status = STATUS_TEXT.get(item["status"], item["status"])
        print(f"{index:<3}{item['command'][:32]:<34}{status:<20}"
              f"{item['elapsed']:>6.1f}s")
    print("-" * 62)

    ok_count = sum(1 for item in results if item["status"] in OK_STATUSES)
    print(f"成功 {ok_count}/{len(results)} 条，"
          f"连接方式 {args.protocol.upper()}，厂商 {vendor.upper()}")
    print()
    if ok_count == len(results) and results:
        print("√ 链路验证通过，可以打开图形界面正式使用了：python main.py")
        print("  界面里填入同样的连接参数，AI 面板填 Anthropic API Key 即可启用 AI 辅助。")
        return 0
    print("! 有命令未正常完成，可以：")
    print("  · 加 --delay-mode 用固定间隔再试一次（提示符不标准时更稳）")
    print("  · 加大 --prompt-timeout（大配置输出很慢时）")
    print("  · 加 --show-lines 40 看更完整的输出，把报错发出来一起看")
    return 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)

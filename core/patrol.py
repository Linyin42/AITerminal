"""
多设备并发巡检 - 同时对多台设备下发同一组命令并汇总结果

特点:
    · 每个设备一个线程，用线程池控制并发数（默认 4）
    · 复用 BatchRunner 的提示符等待逻辑（CommandWaiter），按设备回显判断命令是否执行完
    · 自动处理分页（---- More ----），遇到 [Y/N] 确认提示则停止该设备的后续命令
    · 结果汇总导出 Excel / CSV（Excel 用 core.xlsx_writer，不需要 openpyxl）

与现有会话的关系:
    巡检会自己新建连接（不占用/不影响界面上已打开的会话），跑完即断开。
"""
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence

from PyQt6.QtCore import QObject, pyqtSignal

from .batch import CommandWaiter, disable_paging
from .ssh_connector import SSHConnector
from .telnet_connector import TelnetConnector
from .serial_connector import SerialConnector
from .ai_agent import AIAgent

# 认为命令执行成功的等待结果
OK_STATUSES = ("prompt", "silence", "delay")
# 异常结果
BAD_STATUS_TEXT = {
    "confirm": "需人工确认",
    "timeout": "超时未完成",
    "blocked": "安全拦截",
    "disconnected": "连接断开",
}
STATUS_TEXT = {
    "prompt": "完成",
    "silence": "完成（输出静默）",
    "delay": "已发送",
    "confirm": "需人工确认",
    "timeout": "超时未完成",
    "blocked": "安全拦截",
    "disconnected": "连接断开",
}


from .ensp_connector import EnspProConnector
from .terminal import detect_vendor


def guess_vendor(config: dict, extra_text: str = "") -> str:
    """按设备名/型号/主机/额外文本猜厂商，认不出来就返回 unknown

    只用来决定"先试哪条关分屏命令"，猜错也不致命 —— disable_paging 会按候选
    列表依次试，代价仅是多一次往返（0.3~0.5 秒）。
    """
    parts = [str(config.get(key) or "") for key in
             ("name", "vendor", "model", "host", "serial_port")]
    text = " ".join(parts + [extra_text or ""])
    # 中文名先自己认（detect_vendor 只认英文厂商名）
    if "华为" in text:
        return "huawei"
    if "华三" in text:
        return "h3c"
    vendor = detect_vendor(text)
    return vendor if vendor in ("huawei", "h3c", "cisco", "ruijie") else "unknown"



def build_connector(config: dict):
    """按配置创建连接器（不注册到会话管理器）"""
    protocol = (config.get('protocol') or 'SSH').strip()
    if protocol == 'SSH':
        return SSHConnector(), 'SSH'
    if protocol == 'Telnet':
        return TelnetConnector(), 'Telnet'
    if protocol == 'Serial':
        return SerialConnector(), 'Serial'
    if protocol == 'eNSP':
        # eNSP Pro 是第四种协议。之前只接进了 session.py 与引擎，
        # 巡检这条路漏了 —— 表现是巡检 eNSP 设备直接抛
        # "ValueError: 不支持的协议: eNSP"。
        return EnspProConnector(), 'eNSP'
    raise ValueError(f"不支持的协议: {protocol}")


def connect_device(connector, protocol: str, config: dict, timeout: float = 10.0):
    """按协议连接设备，返回 (成功, 错误信息)"""
    errors: List[str] = []
    connector.error_occurred.connect(errors.append)
    try:
        if protocol == 'Serial':
            ok = connector.connect(
                port=config.get('serial_port') or config.get('host', ''),
                baudrate=int(config.get('baudrate', 9600)),
                bytesize=int(config.get('bytesize', 8)),
                parity=config.get('parity', 'N'),
                stopbits=config.get('stopbits', 1),
                flow=config.get('flow', 'none'),
                line_ending=config.get('line_ending', '\r'),
                auto_reconnect=False,
            )
        elif protocol == 'eNSP':
            # eNSP 的设备没有 TCP 端点，靠「服务器 + 沙箱号 + 设备号」定位，
            # 所以不能走下面那套 host/port/username 的通用参数。
            ok = connector.connect(
                host=config.get('host', ''),
                port=int(config.get('port') or 8443),
                sandbox_id=int(config.get('sandbox_id') or 0),
                node_id=int(config.get('node_id') or 0),
                username=config.get('username', ''),
                password=config.get('password', ''),
            )
        else:
            ok = connector.connect(
                host=config.get('host', ''),
                username=config.get('username', ''),
                password=config.get('password', ''),
                port=int(config.get('port', 22 if protocol == 'SSH' else 23)),
                timeout=int(timeout),
                key_filename=config.get('key_filename'),
            )
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            connector.error_occurred.disconnect(errors.append)
        except (TypeError, RuntimeError):
            pass

    if ok:
        return True, ""
    return False, (errors[0] if errors else "连接失败")


def patrol_device(config: dict, commands: Sequence[str], timeout: float = 10.0,
                  prompt_timeout: float = 30.0, quiet_time: float = 0.3,
                  silence_timeout: float = 2.0, auto_page: bool = True,
                  block_dangerous: bool = True,
                  on_command=None, should_stop=None, on_note=None) -> dict:
    """
    巡检单台设备（在工作线程中执行）

    参数:
        config: 连接参数，同 ConnectDialog.get_config()
        commands: 要执行的命令列表
        on_command: 回调 (序号, 命令)，用于上报进度
        should_stop: 回调，返回 True 表示需要中止
        on_note: 回调 (文本)，上报"连上了/正在关分屏/失败原因"这类阶段性信息
    返回:
        结果字典
    """
    name = config.get('name') or config.get('host') or '未命名设备'
    result = {
        "name": name,
        "protocol": config.get('protocol', 'SSH'),
        "host": config.get('host') or config.get('serial_port', ''),
        "ok": False,
        "error": "",
        "elapsed": 0.0,
        "commands": [],
        "pages": 0,
    }

    started = time.time()
    connector = None
    waiter = None

    def note(text: str):
        """把"正在进行到哪一步"回传给界面（连上/关分屏/失败原因），让进度别停在 0%"""
        if not on_note:
            return
        try:
            on_note(str(text))
        except Exception:                           # noqa: BLE001
            pass

    try:
        connector, protocol = build_connector(config)
        result["protocol"] = protocol
        ok, error = connect_device(connector, protocol, config, timeout)
        if not ok:
            result["error"] = error
            note(f"连接失败：{error}")
            return result
        note(f"已连接（{protocol}），正在关闭设备分屏…")

        waiter = CommandWaiter(connector, quiet_time=quiet_time,
                               timeout=prompt_timeout,
                               silence_timeout=silence_timeout,
                               auto_page=auto_page)
        waiter.start()

        # 先关设备分屏再跑命令。为什么必须做：
        #   分屏开着时，一条 `display mac-address` 会被切成几十屏，每屏都要等
        #   "---- More ----" 再补一个空格（每页还带 0.2s 节流），实测 51 KB 的输出
        #   要 6 秒才收完；命令一多，用户看到的就是"才几个命令怎么这么慢"。
        #   关掉分屏后同一台设备同样命令 1 秒内收完（2026-10-19 真机复测）。
        paging_off = ""
        if auto_page:
            try:
                off_ok, used, _text = disable_paging(
                    connector, waiter, vendor=guess_vendor(config),
                    quiet_time=quiet_time, timeout=min(15.0, max(5.0, prompt_timeout)))
                if off_ok:
                    paging_off = used
                    note(f"已关闭分屏（{used}）")
                else:
                    note("关分屏没成功，将逐页翻页（会慢一些）")
            except Exception:                       # noqa: BLE001
                paging_off = ""
                note("关分屏出错，改为逐页翻页")
        result["paging"] = paging_off

        for index, command in enumerate(commands, start=1):
            if should_stop and should_stop():
                result["error"] = "已中止"
                break
            if not connector.is_connected():
                result["error"] = "连接已断开"
                break
            if on_command:
                try:
                    on_command(index, command)
                except Exception:
                    pass

            if block_dangerous and AIAgent.is_dangerous(command):
                result["commands"].append({
                    "command": command, "status": "blocked",
                    "elapsed": 0.0, "output": "",
                })
                continue

            try:
                status, elapsed, output = waiter.run(command)
            except Exception as e:
                result["commands"].append({
                    "command": command, "status": "disconnected",
                    "elapsed": 0.0, "output": f"发送失败: {e}",
                })
                result["error"] = f"发送失败: {e}"
                break

            # waiter.pages 是本设备的累计翻页数，逐条累加会重复计数
            # （3 条命令会报出 50+50+51 这种越滚越大的数），循环结束后统一取一次
            result["commands"].append({
                "command": command, "status": status,
                "elapsed": elapsed, "output": output,
            })

            if status == "confirm":
                result["error"] = f"第 {index} 条命令需要人工确认，已停止该设备"
                break
            if status == "disconnected":
                result["error"] = "连接已断开"
                break

        result["pages"] = waiter.pages if waiter is not None else 0
        result["ok"] = bool(result["commands"]) and not any(
            item["status"] not in OK_STATUSES for item in result["commands"])
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if waiter is not None:
            waiter.stop()
        if connector is not None:
            try:
                connector.disconnect()
            except Exception:
                pass
        result["elapsed"] = time.time() - started


class PatrolRunner(QObject):
    """
    多设备并发巡检

    信号:
        device_started(int, str):        设备序号, 设备名
        device_command(int, int, str):   设备序号, 命令序号, 命令
        device_done(int, dict):          设备序号, 结果
        log(str):                        过程日志
        finished(bool, dict):            是否正常结束, 汇总信息
    """

    device_started = pyqtSignal(int, str)
    device_command = pyqtSignal(int, int, str)
    device_done = pyqtSignal(int, dict)
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, dict)

    def __init__(self, devices: Sequence[dict], commands: Sequence[str],
                 concurrency: int = 4, timeout: float = 10.0,
                 prompt_timeout: float = 30.0, quiet_time: float = 0.3,
                 silence_timeout: float = 2.0, auto_page: bool = True,
                 block_dangerous: bool = True, task_name: str = "巡检",
                 parent=None):
        super().__init__(parent)
        self.devices = list(devices or [])
        self.commands = [c for c in (commands or []) if str(c).strip()]
        self.concurrency = max(1, min(int(concurrency), 32))
        self.timeout = timeout
        self.prompt_timeout = prompt_timeout
        self.quiet_time = quiet_time
        self.silence_timeout = silence_timeout
        self.auto_page = auto_page
        self.block_dangerous = block_dangerous
        self.task_name = task_name

        self._stop_flag = False
        self._thread: Optional[threading.Thread] = None
        self.results: List[dict] = []
        self.started_at = 0.0
        self.finished_at = 0.0

    # ------------------------------------------------------------ 控制
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        if not self.devices:
            self.finished.emit(False, {"error": "没有选择设备"})
            return False
        if not self.commands:
            self.finished.emit(False, {"error": "没有选择巡检命令"})
            return False

        self._stop_flag = False
        self.results = []
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """请求中止：已发出的命令会跑完，未开始的设备不再启动"""
        self._stop_flag = True

    # ------------------------------------------------------------ 执行
    def _run(self):
        total = len(self.devices)
        self.log.emit(f"开始巡检：{total} 台设备，{len(self.commands)} 条命令，"
                      f"并发 {self.concurrency}")

        results: Dict[int, dict] = {}
        try:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {}
                for index, device in enumerate(self.devices):
                    if self._stop_flag:
                        break
                    self.device_started.emit(index, device.get('name', ''))
                    futures[pool.submit(self._patrol_one, index, device)] = index

                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception as e:
                        results[index] = {
                            "name": self.devices[index].get('name', ''),
                            "protocol": self.devices[index].get('protocol', ''),
                            "host": self.devices[index].get('host', ''),
                            "ok": False, "error": f"{type(e).__name__}: {e}",
                            "elapsed": 0.0, "commands": [], "pages": 0,
                        }
                    self.results.append(results[index])
                    self.device_done.emit(index, results[index])
                    state = "成功" if results[index]["ok"] else "异常"
                    paging = results[index].get("paging") or ""
                    extra = f"（已关分屏:{paging}，翻页 {results[index].get('pages', 0)} 次）" \
                        if paging else ""
                    self.log.emit(f"  [{index + 1}/{total}] "
                                  f"{results[index]['name']} → {state}{extra}")
        finally:
            self.finished_at = time.time()

        ordered = [results[i] for i in sorted(results)]
        self.results = ordered
        ok_count = sum(1 for r in ordered if r["ok"])
        aborted = self._stop_flag
        summary = {
            "task": self.task_name,
            "devices": total,
            "done": len(ordered),
            "ok": ok_count,
            "failed": len(ordered) - ok_count,
            "commands": len(self.commands),
            "concurrency": self.concurrency,
            "elapsed": self.finished_at - self.started_at,
            "aborted": aborted,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        text = (f"巡检{'已中止' if aborted else '完成'}："
                f"{len(ordered)}/{total} 台，成功 {ok_count} 台，异常 {len(ordered) - ok_count} 台，"
                f"耗时 {summary['elapsed']:.1f} 秒")
        self.log.emit(text)
        self.finished.emit(not aborted, summary)

    def _patrol_one(self, index: int, device: dict) -> dict:
        return patrol_device(
            device, self.commands, timeout=self.timeout,
            prompt_timeout=self.prompt_timeout, quiet_time=self.quiet_time,
            silence_timeout=self.silence_timeout, auto_page=self.auto_page,
            block_dangerous=self.block_dangerous,
            on_command=lambda cmd_index, command: self.device_command.emit(
                index, cmd_index, command),
            should_stop=lambda: self._stop_flag,
            on_note=lambda text: self.log.emit(
                f"  {device.get('name') or device.get('host', '')}：{text}"),
        )

    # ------------------------------------------------------------ 导出
    def summary_rows(self):
        """巡检汇总表数据"""
        rows = [["序号", "设备名称", "协议", "地址", "状态", "耗时(秒)",
                 "命令数", "正常数", "异常数", "说明"]]
        for index, result in enumerate(self.results, start=1):
            commands = result.get("commands") or []
            ok_count = sum(1 for c in commands if c["status"] in OK_STATUSES)
            rows.append([
                index, result.get("name", ""), result.get("protocol", ""),
                result.get("host", ""), "成功" if result.get("ok") else "异常",
                round(result.get("elapsed", 0.0), 1), len(commands), ok_count,
                len(commands) - ok_count,
                result.get("error", "") or "",
            ])
        return rows

    def detail_rows(self):
        """命令明细表数据"""
        rows = [["设备", "序号", "命令", "状态", "耗时(秒)", "输出"]]
        for result in self.results:
            for index, item in enumerate(result.get("commands") or [], start=1):
                rows.append([
                    result.get("name", ""), index, item.get("command", ""),
                    STATUS_TEXT.get(item.get("status"), item.get("status", "")),
                    round(item.get("elapsed", 0.0), 1),
                    (item.get("output") or "").strip(),
                ])
        return rows

    def info_rows(self, summary: dict = None):
        """巡检任务信息表数据"""
        summary = summary or {}
        started = self.started_at or 0
        finished = self.finished_at or 0
        rows = [
            ["任务名称", self.task_name],
            ["开始时间", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else ""],
            ["结束时间", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(finished)) if finished else ""],
            ["设备数量", summary.get("done", len(self.results))],
            ["成功 / 异常", f"{summary.get('ok', 0)} / {summary.get('failed', 0)}"],
            ["并发数", self.concurrency],
            ["命令条数", len(self.commands)],
            ["", ""],
            ["巡检命令清单", ""],
        ]
        for index, command in enumerate(self.commands, start=1):
            rows.append([f"  {index}", command])
        return rows


def export_patrol_excel(path: str, runner: PatrolRunner, summary: dict = None) -> bool:
    """
    把巡检结果导出为 Excel（三个工作表：巡检信息 / 巡检汇总 / 命令明细）
    """
    from .xlsx_writer import write_xlsx

    sheets = [
        ("巡检信息", runner.info_rows(summary)),
        ("巡检汇总", runner.summary_rows()),
        ("命令明细", runner.detail_rows()),
    ]
    widths = {
        "巡检信息": [18, 60],
        "巡检汇总": [6, 22, 8, 16, 8, 10, 8, 8, 8, 40],
        "命令明细": [18, 6, 28, 16, 10, 90],
    }
    return write_xlsx(path, sheets, column_widths=widths)


def export_patrol_csv(path: str, runner: PatrolRunner) -> bool:
    """把巡检汇总导出为 CSV（明细另存一个文件）"""
    import csv
    try:
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            for row in runner.summary_rows():
                writer.writerow(row)

        detail_path = os.path.splitext(path)[0] + "_明细.csv"
        with open(detail_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            for row in runner.detail_rows():
                writer.writerow(row)
        return True
    except Exception:
        return False

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

from .batch import CommandWaiter
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


def build_connector(config: dict):
    """按配置创建连接器（不注册到会话管理器）"""
    protocol = (config.get('protocol') or 'SSH').strip()
    if protocol == 'SSH':
        return SSHConnector(), 'SSH'
    if protocol == 'Telnet':
        return TelnetConnector(), 'Telnet'
    if protocol == 'Serial':
        return SerialConnector(), 'Serial'
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
                  on_command=None, should_stop=None) -> dict:
    """
    巡检单台设备（在工作线程中执行）

    参数:
        config: 连接参数，同 ConnectDialog.get_config()
        commands: 要执行的命令列表
        on_command: 回调 (序号, 命令)，用于上报进度
        should_stop: 回调，返回 True 表示需要中止
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
    try:
        connector, protocol = build_connector(config)
        result["protocol"] = protocol
        ok, error = connect_device(connector, protocol, config, timeout)
        if not ok:
            result["error"] = error
            return result

        waiter = CommandWaiter(connector, quiet_time=quiet_time,
                               timeout=prompt_timeout,
                               silence_timeout=silence_timeout,
                               auto_page=auto_page)
        waiter.start()

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

            result["pages"] += waiter.pages
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
                    self.log.emit(f"  [{index + 1}/{total}] "
                                  f"{results[index]['name']} → {state}")
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

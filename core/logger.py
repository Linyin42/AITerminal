"""
会话日志 - 把设备输出自动落盘

目录结构:
    ~/.aiterminal/logs/2026-09-21/核心交换机_10.0.0.1_20260921_143012.log
"""
import os
import re
import sys
import time
from datetime import datetime

DEFAULT_LOG_DIR = os.path.join(os.path.expanduser("~"), ".aiterminal", "logs")


def sanitize(text: str, max_len: int = 40) -> str:
    """把名称/主机名转成安全的文件名片段"""
    text = (text or "").strip() or "session"
    text = re.sub(r'[^\w\-.\u4e00-\u9fff]+', '_', text)
    return text[:max_len].strip('_') or "session"


class SessionLogger:
    """
    单个会话的日志写入器

    参数:
        name: 会话名称
        host: 目标地址
        log_dir: 日志根目录，默认 ~/.aiterminal/logs
        enabled: 是否启用
        day_dir: 是否按日期建子目录
    """

    def __init__(self, name: str, host: str = "", log_dir: str = None,
                 enabled: bool = True, day_dir: bool = True):
        self.name = name
        self.host = host
        self.enabled = enabled
        self.root_dir = log_dir or DEFAULT_LOG_DIR
        self.day_dir = day_dir
        self.path = ""
        self.bytes_written = 0
        self._fh = None
        self._closed = False

    # ------------------------------------------------------------ 生命周期
    def build_path(self) -> str:
        """计算日志文件路径"""
        base = os.path.join(
            self.root_dir,
            datetime.now().strftime("%Y-%m-%d") if self.day_dir else ""
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{sanitize(self.name)}_{sanitize(self.host)}_{stamp}.log"
        return os.path.join(base, filename)

    def open(self) -> bool:
        """创建并打开日志文件"""
        if not self.enabled or self._fh is not None:
            return self._fh is not None
        try:
            self.path = self.build_path()
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fh = open(self.path, 'a', encoding='utf-8', errors='replace')
            self._fh.write(self._header())
            self._fh.flush()
            return True
        except Exception as exc:
            # 这里以前是静默失败：文件建出来了却一个字没写（0 字节），
            # 用户以为在记日志、排查的人也拿不到任何线索。必须留痕。
            print(f"[日志] 打开失败 {self.path!r}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            self._fh = None
            self.enabled = False
            return False

    def _header(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            "=" * 68 + "\n"
            f"AITerminal 会话日志\n"
            f"会话名称: {self.name}\n"
            f"目标设备: {self.host}\n"
            f"开始时间: {now}\n"
            + "=" * 68 + "\n\n"
        )

    def write(self, text: str):
        """
        追加输出

        每次写入后立即 flush：文件默认是块缓冲（不到 8KB 不落盘），
        不刷新的话程序异常退出会丢日志，用户也没法实时 tail 日志。
        """
        if not self._fh or not text:
            return
        try:
            self._fh.write(text)
            self._fh.flush()
            self.bytes_written += len(text)
        except Exception as exc:
            print(f"[日志] 写入失败 {self.path!r}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    def mark(self, note: str):
        """写入一条人工标记（如"批量下发开始"）"""
        if not self._fh:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.write(f"\n--- [{stamp}] {note} ---\n")

    def flush(self):
        if self._fh:
            try:
                self._fh.flush()
            except Exception:
                pass

    def close(self):
        """关闭文件并写入结束时间"""
        if self._fh is None or self._closed:
            return
        try:
            self.write("\n\n" + "-" * 68 + "\n"
                       f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                       f"累计写入: {self.bytes_written} 字符\n")
            self._fh.close()
        except Exception:
            pass
        finally:
            self._fh = None
            self._closed = True

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    def __repr__(self):
        return f"<SessionLogger {self.path or '(未打开)'}>"


def list_logs(log_dir: str = None, limit: int = 50):
    """列出最近的日志文件（按修改时间倒序）"""
    root = log_dir or DEFAULT_LOG_DIR
    result = []
    if not os.path.isdir(root):
        return result
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".log"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            result.append({
                "path": full,
                "name": fn,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            })
    result.sort(key=lambda x: x["mtime"], reverse=True)
    return result[:limit]

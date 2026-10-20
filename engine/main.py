#!/usr/bin/env python3
"""AITerminal Python 引擎入口。

C++/Qt6 界面（cpp/）把这个进程当作后端：
    python -u engine/main.py

stdin 收 JSON-RPC 请求，stdout 回结果与通知，stderr 打日志。
**stdout 上不允许出现任何非 JSON 内容**（别在这里 print 东西）。

为什么引擎必须装 PyQt6：core/connector.py 的 BaseConnector 继承 QObject，
连接器全部硬依赖 PyQt6。但引擎不需要 QApplication，也不需要事件循环 ——
实测在无事件循环的进程里 SessionManager 可构造、真实 Telnet 全链路可用。
"""

import os
import sys


def _bootstrap_path() -> None:
    """把项目根目录塞进 sys.path，这样 `import core.*` 才找得到。

    开发态：<项目根>/engine/main.py  → 根目录就是它的上一级
    发布态：<产物目录>/engine/main.py 且 core/ 也被拷到 <产物目录>/core
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)
    # 也支持把引擎目录自身加进去，便于 engine 内部相对导入
    if here not in sys.path:
        sys.path.insert(0, here)


_bootstrap_path()


def _force_utf8_streams() -> None:
    """把 stdout/stderr 钉死在 UTF-8 上。

    界面按 UTF-8 解析协议流，而 Windows 上 Python 默认可能用 GBK/cp936，
    中文提示会变成乱码。C++ 侧虽然会注入 PYTHONIOENCODING，但不能指望
    别人替我们设好 —— 手动启动引擎时也必须正确。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except Exception:
            pass


_force_utf8_streams()

from engine.rpc import Emitter, JsonRpcServer, log          # noqa: E402
from engine.service import EngineService                    # noqa: E402


def main() -> int:
    emitter = Emitter()
    service = EngineService(emitter)
    # slow_methods：把会阻塞十几秒的方法（ai.ask / patrol.run / …）分流到工作线程，
    # 否则读循环被卡住时，界面刷新用的 usage.list 等请求会排队到超时（见 service.SLOW_METHODS）
    server = JsonRpcServer(emitter, service.dispatch, shutdown=service.shutdown,
                           slow_methods=service.SLOW_METHODS)

    log("info", "AITerminal 引擎启动中，python=%s" % sys.version.split()[0])
    try:
        return server.serve_forever()
    except KeyboardInterrupt:
        log("info", "收到中断信号，引擎退出")
        return 0
    finally:
        service.shutdown()


if __name__ == "__main__":
    sys.exit(main())

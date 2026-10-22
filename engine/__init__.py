"""NetOpsTerminal Python 引擎。

这个包把已有的 core/ 能力包装成 JSON-RPC 服务，供 C++/Qt6 界面调用。
设计约束（来自对 core/ 的实测结论）：

* core/connector.py 的 BaseConnector 继承 QObject，所以引擎进程必须能 import
  PyQt6 —— 但**不需要** QApplication，也不需要事件循环。
* 跨线程的 pyqtSignal 在没有事件循环时会静默丢弃，因此输出/状态一律走
  connector.add_output_listener()（读线程内同步回调）+ 轮询 is_connected()，
  不复用 Qt 信号通道。
* connector.get_output() 是"读后清空"的破坏性读取，引擎绝不调用它，
  以免把界面要显示的内容吃掉。
"""

__version__ = "0.8.0"

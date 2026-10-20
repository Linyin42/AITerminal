"""JSON-RPC 2.0 的最小实现（JSON Lines over stdio）。

协议约定
--------
stdin   : 每行一个 JSON 请求   {"id":1,"method":"hosts.list","params":{}}
stdout  : 每行一个 JSON 消息   {"id":1,"result":{...}}
                               {"id":1,"error":{"code":...,"message":...}}
                               {"method":"session.output","params":{...}}
stderr  : 人类可读日志

**stdout 是协议通道，绝对不允许出现非 JSON 内容。**
任何 print() 都会污染协议流，所以本模块所有诊断信息都写 stderr。
"""

import json
import sys
import threading
import traceback

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: 保护 JsonRpcServer._workers（耗时方法的工作线程登记表）
_WORKERS_LOCK = threading.Lock()


def log(level: str, message: str) -> None:
    """写一条日志到 stderr（线程安全）。"""
    try:
        sys.stderr.write("[engine/%s] %s\n" % (level, message))
        sys.stderr.flush()
    except Exception:
        pass


class Emitter:
    """线程安全的 stdout 写入器。

    读线程会通过它推送 session.output 通知，主线程回响应，
    两边并发写 stdout 必须加锁，否则 JSON 行会交错、界面解析必然失败。
    """

    def __init__(self, stream=None):
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def _write(self, obj: dict) -> None:
        # ensure_ascii=False 让中文以 UTF-8 原样出去（比 \uXXXX 省一半字节）；
        # separators 去掉多余空格，和 C++ 侧的写出口径保持一致。
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except Exception as exc:          # 父进程没了，写不进去很正常
                log("error", "写 stdout 失败: %s" % exc)

    def result(self, req_id, result=None) -> None:
        self._write({"id": req_id, "result": {} if result is None else result})

    def error(self, req_id, code: int, message: str, data=None) -> None:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"id": req_id, "error": err})

    def notify(self, method: str, params=None) -> None:
        self._write({"method": method, "params": {} if params is None else params})


class JsonRpcServer:
    """逐行读取 stdin，按方法分流派发。

    **两种派发路径**（`slow_methods` 决定走哪条）：

    - 默认**同步**：handler 被要求"立刻返回"。绝大多数方法都是这种。
    - `slow_methods` 里的方法**丢到工作线程**：它们会阻塞十几秒（`ai.ask` 走 HTTP 等
      大模型返回、`patrol.run` 串行轮询多台设备）。同步派发会把**读循环**一起卡住 ——
      实测（2026-10-05 Eve-NG）：探针 t=16s 发起 AI 问答，t=26s 请求 `usage.list`，
      引擎直到 t=35s 才处理它，于是界面「使用总结」渲染出构造时的**空快照**，
      表现为"记录丢了"。

    ⚠ 为什么不是"让 handler 自己开线程 + 用通知回推"：那要改 C++ 侧的协议
      （现在 C++ 的 `callAsync` 等的是**带 id 的应答**）。在这里分流**不动协议**，
      C++ 侧拿到的仍然是同一个 id 的应答，只是晚一点到 —— 而"晚一点"是可接受的，
      "卡住其他所有请求"才是缺陷。

    ⚠ **顺序性**：`session.write` / `session.command` 一律**不进** `slow_methods`，
      所以敲命令的时序不会被并发打乱。这也是必须按方法白名单、而不是"全部异步"的原因。
    """

    def __init__(self, emitter: Emitter, dispatch, shutdown=None, slow_methods=()):
        self._emit = emitter
        self._dispatch = dispatch          # callable(method, params) -> dict
        self._shutdown = shutdown          # callable() -> None
        self._slow = frozenset(slow_methods or ())
        self._workers = []                 # 只用于退出前 join，避免半截应答

    def _respond(self, req_id, method: str, params: dict) -> None:
        """执行 handler 并把结果/错误写回 stdout。**可能在工作线程里跑。**"""
        try:
            result = self._dispatch(method, params)
        except KeyError:
            self._emit.error(req_id, METHOD_NOT_FOUND, "未知方法: %s" % method)
            return
        except TypeError as exc:
            self._emit.error(req_id, INVALID_PARAMS, "参数不合法: %s" % exc)
            return
        except Exception as exc:
            detail = traceback.format_exc()
            log("error", "方法 %s 抛异常: %s\n%s" % (method, exc, detail))
            self._emit.error(req_id, INTERNAL_ERROR, "%s: %s" % (type(exc).__name__, exc))
            return
        self._emit.result(req_id, result)

    def _dispatch_async(self, req_id, method: str, params: dict) -> None:
        """把耗时方法放到工作线程，**读循环立刻继续**。

        `Emitter` 自身带锁（读线程推 session.output 与这里回响应会并发写 stdout），
        所以这里直接回写是安全的。
        """
        def work():
            try:
                self._respond(req_id, method, params)
            finally:
                with _WORKERS_LOCK:
                    if t in self._workers:
                        self._workers.remove(t)

        t = threading.Thread(target=work, name="rpc-slow-%s" % method, daemon=True)
        with _WORKERS_LOCK:
            self._workers.append(t)
        t.start()
        log("info", "耗时方法 %s 转入工作线程（读循环不再被阻塞）" % method)

    def _handle_line(self, line: str) -> bool:
        """处理一行请求，返回是否要继续服务。"""
        try:
            message = json.loads(line)
        except Exception as exc:
            self._emit.error(None, PARSE_ERROR, "JSON 解析失败: %s" % exc)
            return True

        if not isinstance(message, dict):
            self._emit.error(None, INVALID_REQUEST, "请求必须是 JSON 对象")
            return True

        req_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if method == "engine.exit":
            self._emit.result(req_id, {"bye": True})
            return False

        if not isinstance(method, str) or not method:
            self._emit.error(req_id, INVALID_REQUEST, "缺少 method 字段")
            return True
        if not isinstance(params, dict):
            self._emit.error(req_id, INVALID_PARAMS, "params 必须是对象")
            return True

        if method in self._slow:
            self._dispatch_async(req_id, method, params)
            return True

        self._respond(req_id, method, params)
        return True

    def serve_forever(self) -> int:
        log("info", "引擎启动，等待 stdin 上的 JSON-RPC 请求")
        stdin = getattr(sys.stdin, "buffer", sys.stdin)
        while True:
            try:
                raw = stdin.readline()
            except Exception as exc:
                log("error", "读 stdin 失败: %s" % exc)
                return 1
            if not raw:                     # 父进程关掉了管道
                log("info", "stdin 已关闭，引擎退出")
                break
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw).strip()
            if not line:
                continue
            if not self._handle_line(line):
                break

        if self._shutdown:
            try:
                self._shutdown()
            except Exception:
                pass
        return 0

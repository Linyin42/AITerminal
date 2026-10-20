"""复现"会话日志 0 字节"：同一进程里连开两个会话日志，看第二个是否真的落盘

2026-10-19 现场：`logs/2026-10-19/` 里
    华为-10.3.8.3_10.3.8.3_20261019_203438.log   887 B  （第一个会话）
    H3C-S7503X_10.2.0.1_20261019_203519.log        0 B  （几秒后的第二个会话）
    Console交换机_COM5_20261019_204028.log         0 B
而同一天 21:46 的同类探针里两个都是 252/257 B（正常）。Console 那个会话屏幕上
明明有设备输出（用户截图有 line con 0 / show run），磁盘上却一个字没有 ——
所以要么 header 写失败被静默吞掉，要么有第二个写者把它截断了。用实验分辨。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import SessionLogger          # noqa: E402

tmp = os.path.join(tempfile.gettempdir(), "ait_log_probe")
os.makedirs(tmp, exist_ok=True)
for name in os.listdir(tmp):
    os.remove(os.path.join(tmp, name))

print(f"日志目录: {tmp}")
loggers = []
for name, host in (("华为-10.3.8.3", "10.3.8.3"), ("H3C-S7503X", "10.2.0.1"),
                   ("Console交换机", "")):
    logger = SessionLogger(name, host, log_dir=tmp)
    opened = logger.open()
    loggers.append(logger)
    print(f"  open({name!r}) -> {opened}  文件 {os.path.basename(logger.path)}")
    logger.write(f"{name} 的输出第一行\n")

print()
for logger in loggers:
    size = os.path.getsize(logger.path) if os.path.exists(logger.path) else -1
    with open(logger.path, encoding="utf-8", errors="replace") as fh:
        head = fh.read(60).replace("\n", "\\n")
    print(f"  {size:6d} B  {os.path.basename(logger.path)}  开头={head!r}")

print()
print("再模拟真实写法：会话记录输出后**不 close**（进程被杀/仍在跑）")
logger = SessionLogger("杀进程测试", "1.2.3.4", log_dir=tmp)
logger.open()
logger.write("display version 的输出\n")
size_now = os.path.getsize(logger.path)
print(f"  写完后立刻读: {size_now} B")
logger.flush()
print(f"  再 flush 后: {os.path.getsize(logger.path)} B")

bad = [f for f in os.listdir(tmp) if os.path.getsize(os.path.join(tmp, f)) == 0]
print()
print("[结论] 0 字节文件: %s" % (bad or "无（没能复现，说明不是「同进程第二个 logger」的问题）"))

"""原始 Console 探针：连上 eNSP 设备后**不自动应答密码**，把设备真实提示符打出来；
再按候选口令逐个试，把设备自己的回答原样打印（用设备的话当证据）。

背景：连接器日志说"设备未接受密码"，但看不到设备给的是什么回答；
      原始探针已确认提示符是 `Password:`（只要密码、不要用户名）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ensp_connector import EnspProConnector  # noqa: E402

HOST = os.environ.get("ENSP_HOST", "192.168.31.11")
SB = int(os.environ.get("ENSP_SB", "4"))
NODE = int(os.environ.get("ENSP_NODE", "28"))
USER = os.environ.get("ENSP_USER", "admin")
WAIT = float(os.environ.get("ENSP_WAIT", "20"))
CANDIDATES = os.environ.get(
    "ENSP_CANDIDATES", "|Huawei@123|Admin@123|huawei@123|Huawei123|admin"
).split("|")

print(f"=== 口令候选扫描：{HOST} 沙箱{SB} 设备{NODE} ===", flush=True)
c = EnspProConnector()
t0 = time.monotonic()
ok = c.connect(host=HOST, port=8443, username=USER, password="",
               sandbox_id=SB, node_id=NODE, device_password="")
print(f"connect -> {ok}（用时 {time.monotonic() - t0:.1f}s）", flush=True)
if not ok:
    print(f"last_error: {getattr(c, 'last_error', None)!r}", flush=True)
    sys.exit(1)

c.send_raw("\r")
time.sleep(WAIT)
buf = c.get_output() or ""
print(f"初始回显 {len(buf)} 字符：{buf[-120:]!r}", flush=True)

if "assword" not in buf[-200:]:
    print("没看到 Password 提示，直接 dump 并退出", flush=True)
    c.disconnect()
    sys.exit(0)


def drain(seconds: float) -> str:
    end = time.time() + seconds
    acc = ""
    while time.time() < end:
        time.sleep(0.4)
        chunk = c.get_output()
        if chunk:
            acc += chunk
    return acc


for pwd in CANDIDATES:
    label = pwd if pwd else "（空口令/直接回车）"
    print(f"\n--- 试口令：{label} ---", flush=True)
    c.send_raw(pwd + "\r")
    out = drain(9)
    tail = out.replace("\r", "").strip()
    print(f"回答（{len(out)} 字符）：{tail[-300:]!r}", flush=True)
    # 成功判据必须包含"Please Press ENTER."：华为 VRP 密码正确后会先清屏并显示
    # "User interface con0 is available" + "Please Press ENTER."，此时**没有** > ；
    # 只认 > 会把成功当成失败、继续试下一个候选 —— 而设备此时正处于
    # "首次登录改密码"流程，后续输入会被当成新密码，**把设备密码改掉**。
    low = tail.lower()
    if (tail.endswith(">") or tail.endswith("]")
            or "press enter" in low or "con0 is available" in low):
        print(f"*** 命中：{label} 登录成功 ***", flush=True)
        c.send_raw("screen-length 0 temporary\r")
        time.sleep(2)
        c.send_raw("display version\r")
        time.sleep(4)
        print((c.get_output() or "")[:800], flush=True)
        break
    # 失败后设备会再问一次密码，等它
    time.sleep(1.5)

c.disconnect()

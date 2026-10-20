# -*- coding: utf-8 -*-
"""eNSP Pro 设备控制：列沙箱/设备、启动、停止、等待就绪。

接口族（从前端 bundle 逆向 + 本机实测跑通）:
    POST /api/login?service=...                         免登录会话（拿 csrfToken/JSESSIONID）
    POST /service/ensp/sandbox/getByPage                body={"projectId":1,"currentPage":1,"pageSize":20}
        返回 data[].id 与 data[].devices[]（device.id 就是连接器要的 node_id）
    POST /service/ensp/device/batchStart                body={"sandboxId":N,"deviceIds":[M]}
    POST /service/ensp/device/batchStop                 body={"sandboxId":N,"deviceIds":[M]}

用法:
    python -u tools\\ensp_device_ctl.py list [host] [port]
    python -u tools\\ensp_device_ctl.py start <sandbox_id> <device_id> [host] [port]
    python -u tools\\ensp_device_ctl.py stop  <sandbox_id> <device_id> [host] [port]
    python -u tools\\ensp_device_ctl.py wait  <sandbox_id> <device_id> [秒] [host] [port]
"""
import json
import sys
import time

import requests
import urllib3

urllib3.disable_warnings()

#: 设备"已启动"的 displayStatus 取值（12 是未启动，实测）
RUNNING_STATES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)


def open_session(host: str, port: str):
    base = f"https://{host}:{port}"
    s = requests.Session()
    s.verify = False
    s.get(f"{base}/api/login?service={base}", timeout=20)
    return s, base


def sandboxes(s, base):
    r = s.post(f"{base}/service/ensp/sandbox/getByPage",
               json={"projectId": 1, "currentPage": 1, "pageSize": 50}, timeout=25)
    return (r.json() or {}).get("data") or []


def show(s, base):
    rows = sandboxes(s, base)
    if not rows:
        print("（没有沙箱）", flush=True)
    for sb in rows:
        print(f"沙箱 id={sb.get('id')} 名称={sb.get('name')} "
              f"已用/上限={sb.get('usedVms')}/{sb.get('maxVms')} 状态={sb.get('status')}",
              flush=True)
        for d in (sb.get("devices") or []):
            dtype = (d.get("deviceType") or {}).get("name")
            print(f"    设备 id={d.get('id')} 名称={d.get('name')} "
                  f"status={d.get('status')} displayStatus={d.get('displayStatus')} "
                  f"类型={dtype} hostIp={d.get('hostIp')}", flush=True)
    return rows


def device_state(s, base, sandbox_id: int, device_id: int):
    for sb in sandboxes(s, base):
        if int(sb.get("id") or 0) != sandbox_id:
            continue
        for d in (sb.get("devices") or []):
            if int(d.get("id") or 0) == device_id:
                return d.get("status"), d.get("displayStatus")
    return None, None


def main():
    args = sys.argv[1:]
    action = (args[0] if args else "list").lower()
    rest = args[1:]

    if action == "list":
        host, port = (rest + ["192.168.31.11", "8443"])[:2]
        s, base = open_session(host, port)
        print(f"== {base} 的沙箱与设备 ==", flush=True)
        show(s, base)
        return 0

    if action in ("start", "stop", "wait"):
        sandbox_id, device_id = int(rest[0]), int(rest[1])
        if action == "wait":
            seconds = int(rest[2]) if len(rest) > 2 else 90
            host, port = (rest[3:] + ["192.168.31.11", "8443"])[:2]
        else:
            host, port = (rest[2:] + ["192.168.31.11", "8443"])[:2]
        s, base = open_session(host, port)

        if action in ("start", "stop"):
            api = "batchStart" if action == "start" else "batchStop"
            body = {"sandboxId": sandbox_id, "deviceIds": [device_id]}
            r = s.post(f"{base}/service/ensp/device/{api}", json=body, timeout=30)
            print(f"POST /service/ensp/device/{api} {body} -> {r.status_code} "
                  f"{r.text[:300]}", flush=True)

        print("== 轮询设备状态 ==", flush=True)
        deadline = time.time() + (seconds if action == "wait" else 60)
        last = None
        while time.time() < deadline:
            st, dst = device_state(s, base, sandbox_id, device_id)
            if (st, dst) != last:
                print(f"  status={st} displayStatus={dst}", flush=True)
                last = (st, dst)
            if st is not None and dst in RUNNING_STATES:
                print(f"  → 设备已就绪（sandbox={sandbox_id} node={device_id}）",
                      flush=True)
                return 0
            time.sleep(3)
        print("  → 超时，仍未就绪；当前 = "
              f"{device_state(s, base, sandbox_id, device_id)}", flush=True)
        return 1

    print(__doc__, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())

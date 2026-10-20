# -*- coding: utf-8 -*-
"""清空（或按名字删除）设备库里的保存记录。

用户 2026-10-17 要求："现在先删除这些存在连接记录"。

用法：
    python tools\\clear_vault_devices.py                 # 清空全部
    python tools\\clear_vault_devices.py --keep Console   # 保留名字含 Console 的
    python tools\\clear_vault_devices.py --list           # 只列出，不动

安全：动手前先把 settings.json 备份成 settings.json.bak-vault-<时间戳>，
      并打印删了什么（名字 + 协议 + 主机），便于回滚。
"""
import argparse
import datetime
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from config.settings import CONFIG_FILE, Settings           # noqa: E402


def describe(device: dict) -> str:
    name = device.get('name') or '(无名)'
    proto = device.get('protocol', '?')
    host = device.get('host') or device.get('serial_port') or '?'
    port = device.get('port')
    tail = f":{port}" if port else ""
    pwd = "（含密码）" if device.get('password') else ""
    return f"{name} [{proto}] {host}{tail}{pwd}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default="", help="保留名字里含这些子串的记录（逗号分隔）")
    ap.add_argument("--list", action="store_true", help="只列出，不删")
    args = ap.parse_args()

    settings = Settings()
    devices = settings.get_vault_devices() or []
    print(f"配置文件：{CONFIG_FILE}")
    print(f"当前记录 {len(devices)} 条：")
    for d in devices:
        print(f"  · {describe(d)}")
    if args.list or not devices:
        return 0

    keep_words = [w.strip() for w in args.keep.split(",") if w.strip()]
    keep, drop = [], []
    for d in devices:
        name = str(d.get('name') or '')
        (keep if any(w in name for w in keep_words) else drop).append(d)

    if not drop:
        print("没有需要删除的记录（都被 --keep 保住了）")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{CONFIG_FILE}.bak-vault-{stamp}"
    shutil.copy2(CONFIG_FILE, backup)
    print(f"\n备份：{backup}")

    settings.set("vault_devices", keep)
    settings.save()
    print(f"已删除 {len(drop)} 条：")
    for d in drop:
        print(f"  - {describe(d)}")
    print(f"保留 {len(keep)} 条")
    # 回读校验：确认真的落盘了
    again = Settings()
    if len(again.get_vault_devices()) != len(keep):
        print("⚠ 回读校验不一致（设置文件可能被别的进程占用）")
        return 1
    print("回读校验：一致 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())

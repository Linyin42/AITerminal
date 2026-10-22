"""交付包归档校验：zip 与解压后目录必须**一一对应**（条目 / 名字 / 字节数），CRC 可选。

为什么要有它：出包后最容易出的错是"压缩包与目录不一致"（漏文件、多文件、同名不同
内容、打包中断留半截），而这类错在交付现场才暴露。用法（2026-10-17 首用）：

    python tools\\verify_delivery_zip.py --zip D:\\ait_delivery\\NetOpsTerminal-便携.zip ^
                                        --dir D:\\ait_delivery\\NetOpsTerminal --crc

退出码 0 = 完全一致；1 = 有差异（差异明细打印在最前面，最多 20 条）。
"""
import argparse
import os
import sys
import zipfile


def disk_manifest(root):
    """{相对路径(用 / ): 字节数}，跳过目录。"""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            rel = os.path.relpath(p, root).replace("\\", "/")
            try:
                out[rel] = os.path.getsize(p)
            except OSError as e:                                  # noqa: PERF203
                print(f"!! 读不到 {p}: {e}")
    return out


def zip_manifest(zp, prefix):
    """{相对路径: (字节数, CRC)}，跳过目录条目；返回 (map, 目录条目数, 非本前缀条目)。"""
    zmap, dirs, foreign = {}, 0, []
    with zipfile.ZipFile(zp) as zf:
        for zi in zf.infolist():
            name = zi.filename
            if not name.startswith(prefix):
                foreign.append(name)
                continue
            rel = name[len(prefix):]
            if not rel or rel.endswith("/"):
                dirs += 1
                continue
            zmap[rel] = (zi.file_size, zi.CRC)
        return zmap, dirs, foreign


def main():
    try:                                                          # 2026-10-17：GBK 控制台印不出 ✔，
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 曾让「校验通过」看起来像崩了
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True, dest="zp")
    ap.add_argument("--dir", required=True, dest="dr")
    ap.add_argument("--prefix", default="NetOpsTerminal/",
                    help="zip 内的顶层前缀（默认 NetOpsTerminal/）")
    ap.add_argument("--crc", action="store_true", help="逐条目 CRC 校验（慢，~1 分钟）")
    a = ap.parse_args()

    for p in (a.zp, a.dr):
        if not os.path.exists(p):
            raise SystemExit(f"!! 不存在: {p}")
    print(f"zip : {a.zp}  ({os.path.getsize(a.zp):,} 字节)")
    print(f"目录: {a.dr}")

    disk = disk_manifest(a.dr)
    zmap, ndirs, foreign = zip_manifest(a.zp, a.prefix)
    print(f"目录文件数 = {len(disk):,}    zip 文件条目 = {len(zmap):,}    "
          f"zip 内目录条目 = {ndirs:,}")
    if foreign:
        print(f"!! zip 里有 {len(foreign)} 个条目不在 {a.prefix} 前缀下，前 5 个：{foreign[:5]}")

    missing = sorted(set(disk) - set(zmap))
    extra = sorted(set(zmap) - set(disk))
    diff = sorted(k for k in set(disk) & set(zmap) if disk[k] != zmap[k][0])
    bad = bool(missing or extra or diff or foreign)

    if missing:
        print(f"\n!! 目录里有、zip 里缺：{len(missing)} 个")
        for k in missing[:20]:
            print(f"   - {k}  ({disk[k]:,} B)")
    if extra:
        print(f"\n!! zip 里有、目录里没有：{len(extra)} 个")
        for k in extra[:20]:
            print(f"   + {k}  ({zmap[k][0]:,} B)")
    if diff:
        print(f"\n!! 同名但字节数不同：{len(diff)} 个")
        for k in diff[:20]:
            print(f"   ~ {k}  目录 {disk[k]:,} B  vs  zip {zmap[k][0]:,} B")

    total_disk = sum(disk.values())
    total_zip = sum(v[0] for v in zmap.values())
    print(f"\n目录总字节 = {total_disk:,}    zip 解压总字节 = {total_zip:,}    "
          f"差 = {total_zip - total_disk:,}")

    if a.crc:
        print("CRC 全量校验中（testzip，可能要 1 分钟）…", flush=True)
        with zipfile.ZipFile(a.zp) as zf:
            bad_crc = zf.testzip()
        if bad_crc:
            print(f"!! CRC 失败的首个条目: {bad_crc}")
            bad = True
        else:
            print("CRC ✔ 无损坏条目")

    print("\n" + ("✘ 归档与目录**不一致**" if bad else "✔ 归档与目录逐条一致"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

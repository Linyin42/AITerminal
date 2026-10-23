# -*- coding: utf-8 -*-
"""下载锐捷 PDF 命令参考 → 文本 → 落到 _vendor_docs/<name>/fulltext.txt。

为什么用这个而不是 fetch_vendor_docs.py：锐捷官网文档是 PDF（www-file.ruijie.com.cn 直链），
fetch_vendor_docs 的无头渲染只能拿到目录壳（见该工具头部表格：锐捷"深页未通"）。
PDF 直链 + PyMuPDF 提取是实测能跑通的路径（S5750 命令参考 2,248 页 → 1.76M 字符）。

用法：
    python ruijie_pdf_docs.py --url <pdf> --name <目录名> [--url ... --name ...]
    python ruijie_pdf_docs.py --list urls.txt      # 每行 "<url>\t<目录名>"
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request

DOCS_ROOT = r"C:\Users\linyin\deepseek_harness\_vendor_docs"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def download(url, dest, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.ruijie.com.cn/"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r, io.open(dest, "wb") as f:
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            f.write(buf)
    return os.path.getsize(dest), time.time() - t0


def pdf_to_text(pdf_path):
    import fitz     # PyMuPDF
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        pages.append(doc[i].get_text())
    return "\n".join(pages), doc.page_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--name", action="append", default=[])
    ap.add_argument("--list", default="")
    a = ap.parse_args()

    pairs = list(zip(a.url, a.name))
    if a.list:
        with io.open(a.list, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip() or line.startswith("#"):
                    continue
                url, name = line.split("\t")
                pairs.append((url.strip(), name.strip()))
    if not pairs:
        print("没有要下载的 URL")
        return 1

    tmp = os.path.join(os.environ["TEMP"], "ruijie_pdf")
    os.makedirs(tmp, exist_ok=True)
    report = []
    for url, name in pairs:
        print("\n=== %s ===" % name)
        pdf = os.path.join(tmp, re.sub(r"[^\w\-]+", "_", name)[:60] + ".pdf")
        try:
            size, sec = download(url, pdf)
            print("  下载 %.1f MB，%.1f s" % (size / 1048576, sec))
            text, pages = pdf_to_text(pdf)
            lines = [ln.strip() for ln in text.splitlines()]
            out_dir = os.path.join(DOCS_ROOT, name)
            os.makedirs(out_dir, exist_ok=True)
            out = os.path.join(out_dir, "fulltext.txt")
            with io.open(out, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            show_lines = sum(1 for ln in lines if re.match(r"^(show|display) [a-z]", ln))
            report.append({"name": name, "pages": pages, "chars": len(text),
                           "show_lines": show_lines, "kb": round(os.path.getsize(out) / 1024)})
            print("  %d 页 / %d 字符 / show-display 起始行 %d → %s"
                  % (pages, len(text), show_lines, out))
        except Exception as exc:                        # noqa: BLE001  下载/解析失败都记下来继续
            print("  ✗ 失败: %s: %s" % (type(exc).__name__, exc))
            report.append({"name": name, "error": str(exc)})
        finally:
            if os.path.isfile(pdf):
                try:
                    os.remove(pdf)
                except OSError:
                    pass
    print("\n=== 汇总 ===")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

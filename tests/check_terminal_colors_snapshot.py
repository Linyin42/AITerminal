"""终端三色语料快照比较（S2 用；逐 run 精确等价；--strict 才非零退出）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(os.environ["TEMP"], "ait_cpp_build", "AITerminal.exe")
EV = r"C:\Users\linyin\deepseek_harness\_shots\round08-impl\evidence"
CORPUS = os.path.join(APP, "tests", "corpus", "terminal_colors.txt")
BASE = os.path.join(APP, "tests", "snapshots", "terminal_colors_baseline.json")


def probe() -> dict:
    shot = os.path.join(EV, "colors-probe.png")
    subprocess.run(["taskkill", "/F", "/IM", "AITerminal.exe"], capture_output=True)
    with open(os.path.join(os.environ["TEMP"], "ait_snap_out.txt"), "wb") as fo:
        p = subprocess.Popen([EXE, "--shot-dialog=complete", "--feed-colors=" + CORPUS,
                              "--screenshot=" + shot],
                             stdout=fo, stderr=subprocess.STDOUT)
        try:
            p.wait(timeout=180)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/IM", "AITerminal.exe"], capture_output=True)
    return json.load(open(shot + ".json", encoding="utf-8"))


def main() -> int:
    strict = "--strict" in sys.argv
    base = json.load(open(BASE, encoding="utf-8"))
    now = probe()
    b, n = base.get("feed_colors") or [], now.get("feed_colors") or []
    print("基线 %d 条 (exe %s / corpus %s)" % (len(b), str(base.get("exe_sha256"))[:16],
                                              str(base.get("corpus_sha256"))[:16]))
    print("现在 %d 条 (corpus %s)" % (len(n), str(now.get("feed_corpus_sha"))[:16]))
    diffs = []
    for i in range(max(len(b), len(n))):
        x = b[i] if i < len(b) else "<缺失>"
        y = n[i] if i < len(n) else "<缺失>"
        if x != y:
            diffs.append("  run[%d] 基线=%r 现在=%r" % (i, x, y))
    print("差异 %d 条" % len(diffs))
    for d in diffs[:20]:
        print(d)
    same = (not diffs) and len(b) == len(n)
    print("\n结论：" + ("逐 run 精确等价 OK（判定未变）" if same else "不等价（见上，禁止合并）"))
    return 0 if (same or not strict) else 2


if __name__ == "__main__":
    raise SystemExit(main())

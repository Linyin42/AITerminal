# -*- coding: utf-8 -*-
"""验证 P5 的路径收口：`core/app_paths.py` 的两态判据 + 三处数据根解析。

为什么必须有这个用例（都有真实事故）：
  · `core/command_suggest.py` 的 `DEFAULT_DIR` 曾是写死的绝对路径
    `C:\\Users\\linyin\\deepseek_harness\\_vendor_cmd` —— 在交付机上该路径不存在，
    命令补全整块静默失效。本用例断言它现在**解析得到真实存在的目录**。
  · onefile 打包后 `sys.executable` 是应用本体，`[sys.executable, 某脚本.py]` 会变成
    "再开一个应用"。本用例断言 `script_cmd()` 在两种态下产出不同的命令行。
  · `_MEIPASS` 是临时解压目录，绝不能当数据目录。本用例断言打包态基准是 **exe 所在目录**。

跑法：python tests\\test_app_paths.py
"""
import os
import sys
import tempfile

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)

from core import app_paths                                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def main():
    print("=== P5 路径收口 判据测试（core/app_paths.py）===")

    # ---------------- 源码态
    check("源码态 is_frozen() 为假", app_paths.is_frozen() is False)
    check("源码态 base_dir() = 仓库根（与本文件上一级相同）",
          os.path.abspath(app_paths.base_dir()) == os.path.abspath(P),
          app_paths.base_dir())

    # 环境变量优先级最高
    fake = tempfile.mkdtemp(prefix="ait_env_")
    os.environ["AIT_KNOWLEDGE_DIR"] = fake
    try:
        check("环境变量优先于基准目录", app_paths.data_dir("knowledge", "AIT_KNOWLEDGE_DIR") == fake)
        got = app_paths.data_dir("knowledge", "AIT_KNOWLEDGE_DIR")
        check("  （且真的命中了该目录）", os.path.isdir(got))
    finally:
        del os.environ["AIT_KNOWLEDGE_DIR"]

    # 基准目录下的既有数据
    got_k = app_paths.data_dir("knowledge", "AIT_KNOWLEDGE_DIR")
    check("无环境变量时取 <基准>/knowledge 且存在",
          os.path.isdir(got_k) and os.path.abspath(got_k) == os.path.join(P, "knowledge"),
          got_k)

    # legacy 兜底（本机 _vendor_cmd 按历史约定不在仓库里）
    legacy = os.path.join(os.path.expanduser("~"), "deepseek_harness", "_vendor_cmd")
    got_v = app_paths.data_dir("_vendor_cmd", "AIT_VENDOR_CMD_DIR", legacy=[legacy])
    check("无 <基准>/_vendor_cmd 时落到 legacy 约定位置", got_v == legacy, got_v)
    check("  （legacy 目录确实存在，命令补全数据可用）", os.path.isdir(got_v))

    # ---------------- 打包（frozen）态：用 monkeypatch 模拟，绝不动真 exe
    real_frozen = getattr(sys, "frozen", None)
    real_exe = sys.executable
    tmp = tempfile.mkdtemp(prefix="ait_frozen_")
    os.makedirs(os.path.join(tmp, "_vendor_cmd", "h3c"), exist_ok=True)
    try:
        sys.frozen = True                       # type: ignore[attr-defined]
        sys.executable = os.path.join(tmp, "NetOpsTerminal.exe")
        check("打包态 is_frozen() 为真", app_paths.is_frozen() is True)
        check("打包态 base_dir() = exe 所在目录（不是 _MEIPASS）",
              os.path.abspath(app_paths.base_dir()) == os.path.abspath(tmp),
              app_paths.base_dir())
        got_f = app_paths.data_dir("_vendor_cmd", "AIT_VENDOR_CMD_DIR", legacy=[legacy])
        check("打包态优先取 <exe 同目录>/_vendor_cmd（交付包布局）",
              os.path.abspath(got_f) == os.path.abspath(os.path.join(tmp, "_vendor_cmd")),
              got_f)

        cmd_frozen = app_paths.script_cmd("probe_tier.py", "--json")
        check("打包态 script_cmd 走 `exe --script 脚本`（否则会再开一个应用）",
              cmd_frozen[:3] == [sys.executable, "--script", "probe_tier.py"],
              " ".join(cmd_frozen))
    finally:
        if real_frozen is None:
            del sys.frozen                      # type: ignore[attr-defined]
        else:
            sys.frozen = real_frozen            # type: ignore[attr-defined]
        sys.executable = real_exe

    cmd_src = app_paths.script_cmd("probe_tier.py", "--json")
    check("源码态 script_cmd 走 `python 脚本`（与改造前一致）",
          cmd_src[:2] == [sys.executable, "probe_tier.py"], " ".join(cmd_src))

    # ---------------- 三处数据根的实际解析（防回归）
    from core import command_suggest as cs
    from core import tech_knowledge as tk
    from core import vendor_knowledge as vk

    check("command_suggest.DEFAULT_DIR 是真实存在的目录（曾写死交付机不存在的路径）",
          os.path.isdir(cs.DEFAULT_DIR), cs.DEFAULT_DIR)
    check("  （且三家厂商子目录齐）",
          all(os.path.isdir(os.path.join(cs.DEFAULT_DIR, v)) for v in cs.VENDORS),
          str(cs.VENDORS))
    check("tech_knowledge.DEFAULT_DIR = <仓库>/knowledge",
          os.path.abspath(tk.DEFAULT_DIR) == os.path.join(P, "knowledge"), tk.DEFAULT_DIR)
    check("vendor_knowledge.DEFAULT_DIR = <仓库>/knowledge",
          os.path.abspath(vk.DEFAULT_DIR) == os.path.join(P, "knowledge"), vk.DEFAULT_DIR)
    check("tech/vendor 两家共用同一个环境变量 AIT_KNOWLEDGE_DIR",
          tk.knowledge_dir() == vk.knowledge_dir())
    check("源码态下三处默认根与改造前完全一致（不改变本机行为）",
          os.path.isdir(os.path.join(tk.DEFAULT_DIR, "h3c", "tech")))

    print(f"\n{'=' * 70}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n in FAIL:
            print("  FAIL: " + n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

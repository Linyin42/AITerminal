# -*- coding: utf-8 -*-
"""只跑 smoke_test.py 里指定的几段（改了一处就只跑相关的那几段，别每次全量）

用法：
    python tools\\run_smoke_subset.py test_terminal_input_tracking test_ai_agent
    python tools\\run_smoke_subset.py --list          # 列出所有用例名
退出码：0 = 全过；1 = 有失败项（失败详情打印在最后）。
"""
import argparse
import importlib.util
import os
import sys

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)

spec = importlib.util.spec_from_file_location("smoke", os.path.join(P, "tests", "smoke_test.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["smoke"] = mod
spec.loader.exec_module(mod)            # 只加载，不跑 main()

_APP = None                             # 引用住 QApplication，别让它被回收


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="要跑的用例函数名")
    ap.add_argument("--list", action="store_true", help="列出所有用例名")
    args = ap.parse_args()

    if args.list or not args.names:
        for name in dir(mod):
            if name.startswith("test_"):
                print(name)
        return 0

    # 控件的用例必须有 QApplication（smoke_test.main() 里也是这么起的）；
    # ⚠ 必须存进变量：临时对象会被回收，之后建 QWidget 就报
    #    "Must construct a QApplication before a QWidget"
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    mod._APP = _APP          # 有的用例用的是 smoke_test 模块自己的 _APP（如 processEvents）

    for name in args.names:
        fn = getattr(mod, name, None)
        if fn is None:
            print(f"没有这个用例：{name}")
            return 1
        print(f"--- {name} ---")
        fn()

    results = getattr(mod, "RESULTS", [])
    fails = [(n, d) for n, ok, d in results if not ok]
    print(f"\n共跑 {len(results)} 项，失败 {len(fails)} 项")
    for name, detail in fails:
        print("  FAIL:", name, detail)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

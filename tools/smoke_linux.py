"""Linux 冒烟脚本：由冻结后的 NetOpsTerminal --script 调用，验证随包资源与 Qt GUI 栈。

用法:
    ./dist/NetOpsTerminal --script tools/smoke_linux.py /tmp/smoke-linux.json
    # 返回码: 0=全部通过  3=GUI 子项失败  4=资源缺失

它**不连任何设备**，只回答三个问题：
    1. 冻结包里 sys.frozen 是否成立、Python/Qt 版本是多少；
    2. resources/icons/app.png、local_llm/retrieve.py 有没有真的进包；
    3. 在 offscreen 平台下 QApplication + QWidget 能否创建并显示（不依赖 X）。
"""
import json
import os
import sys


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/smoke-linux.json"
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    res = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "meipass": getattr(sys, "_MEIPASS", ""),
    }

    try:
        from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
        res["qt"] = QT_VERSION_STR
        res["pyqt"] = PYQT_VERSION_STR
    except Exception as exc:                                   # noqa: BLE001
        res["qt_error"] = repr(exc)

    missing = []
    for rel in ("resources/icons/app.png", "resources/icons/app.ico",
                "local_llm/retrieve.py", "local_llm/probe_tier.py"):
        ok = os.path.exists(os.path.join(base, *rel.split("/")))
        res[f"has:{rel}"] = ok
        if not ok:
            missing.append(rel)

    gui_ok = False
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication, QLabel

        app = QApplication([])
        w = QLabel("NetOpsTerminal smoke")
        w.resize(320, 120)
        w.show()
        app.processEvents()
        res["widget_size"] = [w.width(), w.height()]
        res["platform_name"] = app.platformName()
        gui_ok = w.width() == 320 and w.height() == 120
        w.close()
        app.quit()
    except Exception as exc:                                   # noqa: BLE001
        res["gui_error"] = repr(exc)
    res["gui_ok"] = gui_ok

    res["verdict"] = ("PASS" if gui_ok and not missing
                      else "FAIL_RESOURCES" if missing else "FAIL_GUI")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2, ensure_ascii=False)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if missing:
        return 4
    return 0 if gui_ok else 3


if __name__ == "__main__":
    sys.exit(main())

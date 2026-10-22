"""离屏渲染主窗口并截图：验证 Linux 上真实 UI（不是空壳 QWidget）。

用法（冻结包内）:
    QT_QPA_PLATFORM=offscreen ./dist/NetOpsTerminal --script tools/linux_ui_shot.py /tmp/ait-linux-ui.png

返回码: 0=截到有效画面  5=渲染失败
"""
import json
import os
import sys


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ait-linux-ui.png"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    info = {"out": out}
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication

        from ui.main_window import MainWindow

        app = QApplication([])
        win = MainWindow()
        win.resize(1500, 900)
        win.show()
        for _ in range(12):
            app.processEvents()
        pix = win.grab()
        pix.save(out, "PNG")
        img = pix.toImage()
        # 统计非纯白像素比例，确认不是空白窗口
        w, h = img.width(), img.height()
        colors = set()
        nonwhite = 0
        step = max(1, w // 120)
        for y in range(0, h, step):
            for x in range(0, w, step):
                c = img.pixel(x, y)
                colors.add(c)
                if (c & 0x00FFFFFF) != 0x00FFFFFF:
                    nonwhite += 1
        total = len(range(0, h, step)) * len(range(0, w, step))
        info.update({
            "size": [w, h],
            "file_bytes": os.path.getsize(out) if os.path.exists(out) else 0,
            "distinct_colors_sampled": len(colors),
            "nonwhite_ratio": round(nonwhite / max(1, total), 3),
            "window_title": win.windowTitle(),
        })
        ok = w > 800 and h > 500 and len(colors) > 8
        info["verdict"] = "PASS" if ok else "FAIL_BLANK"
        win.close()
        app.quit()
    except Exception as exc:                                   # noqa: BLE001
        info["error"] = repr(exc)
        info["verdict"] = "FAIL_ERROR"
    print(json.dumps(info, indent=2, ensure_ascii=False))
    with open("/tmp/ait-linux-ui.json", "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=2, ensure_ascii=False)
    return 0 if info.get("verdict") == "PASS" else 5


if __name__ == "__main__":
    sys.exit(main())

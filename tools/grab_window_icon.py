#!/usr/bin/env python
"""抓取某个进程主窗口的图标并算指纹（用于"换了图标到底生效没有"的客观比对）。

为什么不用截图：截屏受桌面锁定、缩放 125%、任务栏合成影响；
这里直接向窗口要 ICON_BIG（WM_GETICON），拿到的是程序自己设的图标本体。

用法：
  python tools\\grab_window_icon.py --pid 23948
  python tools\\grab_window_icon.py --title NetOpsTerminal --save out.png
输出：宽×高、RGBA 指纹（SHA256 前 16 位）；给了 --save 就存 PNG。

⚠ 坑：HICON/HBITMAP 在 64 位上是指针。不给 ctypes 设 argtypes/restype 的话，
   ctypes 按 32 位 c_int 猜，一会 OverflowError: int too long，
   一会把返回的图标句柄截断成负数。所以下面每个函数都显式声明原型。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_GETICON = 0x007F
ICON_SMALL, ICON_BIG = 0, 1
GCLP_HICON = -14
DI_NORMAL = 0x0003
HICON = wintypes.HANDLE
HBITMAP = wintypes.HANDLE


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", HBITMAP),
                ("hbmColor", HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


# ------------------------------------------------------------------ 原型
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_void_p          # 返回 HICON，必须当指针
user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClassLongPtrW.restype = ctypes.c_void_p
user32.GetIconInfo.argtypes = [HICON, ctypes.POINTER(ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL
user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, HICON,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT,
                              wintypes.HBRUSH, wintypes.UINT]
user32.DrawIconEx.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]

gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetObjectW.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL


def find_window(pid: int | None, title: str | None) -> int:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if pid is not None and wpid.value != pid:
            return True
        if title:
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title.lower() not in buf.value.lower():
                return True
        found.append(hwnd)
        return False

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def icon_pixels(hicon: int):
    info = ICONINFO()
    if not user32.GetIconInfo(HICON(hicon), ctypes.byref(info)):
        raise OSError("GetIconInfo 失败")
    bm = BITMAP()
    gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
    w, h = int(bm.bmWidth), int(bm.bmHeight)
    if w <= 0 or h <= 0:
        raise OSError(f"图标尺寸异常 {w}x{h}")

    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h        # 负高度 = 自上而下
    bi.biPlanes, bi.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mdc, hbmp)
    user32.DrawIconEx(mdc, 0, 0, HICON(hicon), w, h, 0, None, DI_NORMAL)
    raw = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, hdc)
    return w, h, raw


def fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16].upper()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int)
    ap.add_argument("--title")
    ap.add_argument("--save")
    a = ap.parse_args()
    if a.pid is None and not a.title:
        print("需要 --pid 或 --title")
        return 2
    hwnd = find_window(a.pid, a.title)
    if not hwnd:
        print("没找到窗口")
        return 1
    print(f"窗口: {hwnd}  标题={window_title(hwnd)!r}")
    out = []
    for which, label in ((ICON_BIG, "BIG"), (ICON_SMALL, "SMALL")):
        h = user32.SendMessageW(hwnd, WM_GETICON, which, 0)
        src = f"WM_GETICON {label}"
        if not h and which == ICON_BIG:
            h = user32.GetClassLongPtrW(hwnd, GCLP_HICON)
            src = "GetClassLongPtr HICON"
        if not h:
            continue
        try:
            w, hh, raw = icon_pixels(h)
        except OSError as exc:
            print(f"  {src}: 读不出像素（{exc}）")
            continue
        print(f"  {src}: {w}x{hh}  指纹={fingerprint(raw)}  handle={h}")
        out.append((label, w, hh, raw))
    if a.save and out:
        try:
            from PIL import Image
            label, w, hh, raw = out[0]
            Image.frombuffer("RGBA", (w, hh), raw, "raw", "BGRA", 0, 1).save(a.save)
            print(f"  已保存 {a.save}（{label} {w}x{hh}）")
        except Exception as exc:                 # noqa: BLE001
            print(f"  存图失败：{exc}")
    return 0 if out else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

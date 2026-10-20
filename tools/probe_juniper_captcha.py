#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Juniper Ask AI：确认**提交被什么挡住**（hCaptcha？还是没触发提交）。

已知（2026-10-18 实测）：点开 Ask AI 后弹窗里有
  form.ai-response-form > textarea#promptInput、#resultContainer、chat-greeting-*
但**没有发送按钮** ⇒ 提交只能靠回车/表单 JS；按回车后出现 hCaptcha 请求、正文不变。
本探针回答三件事：① 回车到底有没有提交（textarea 是否被清空）② hCaptcha 有没有发挑战
③ 有没有可见验证码交互（iframe/widget）。

用法：python tools/probe_juniper_askai.py captcha
"""
from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

URL = "https://networkingdocs.hpe.com/documentation/juniper/"
CLICK = 'button[aria-label="Open Juniper Ask AI"], button.ask-ai-btn, button:has-text("Ask AI")'
Q = "show interfaces terse 命令的作用和输出字段解释"


def snap(page, reqs) -> list[str]:
    lines = []
    for label, js in (
        ("promptInput.value", "() => (document.getElementById('promptInput') || {}).value || ''"),
        ("hcaptcha tokens", "() => [...document.querySelectorAll('textarea[id^=h-captcha-response]')]"
                            ".map(e => (e.value || '').length)"),
        ("hcaptcha iframes", "() => [...document.querySelectorAll('iframe')]"
                             ".map(e => (e.src || '').slice(0, 90)).filter(s => /hcaptcha|hsprotect/i.test(s))"),
        ("hcaptcha widgets", "() => [...document.querySelectorAll('.h-captcha, div[class*=hcaptcha]')]"
                             ".map(e => [e.getAttribute('class') || '', (e.innerText || '').slice(0, 60)])"),
        ("#resultContainer", "() => { const e = document.getElementById('resultContainer');"
                             " return e ? e.innerText.replace(/\\s+/g,' ').slice(0, 300) : '（不存在）'; }"),
        ("chat-history len", "() => { const e = document.querySelector('.chat-history-container');"
                             " return e ? e.innerHTML.length : -1; }"),
    ):
        try:
            lines.append(f"  {label}: {page.evaluate(js)}")
        except Exception as exc:                              # noqa: BLE001
            lines.append(f"  {label}: !! {exc}")
    fresh = [u for u in reqs if "captcha" in u.lower() or "dialogue" in u.lower() or "chat" in u.lower()]
    lines.append(f"  相关请求（{len(fresh)}）: " + " | ".join(u[:100] for u in fresh[-6:]))
    return lines


def main() -> int:
    from playwright.sync_api import sync_playwright

    out = ["=== juniper :: hCaptcha 阻断取证 ==="]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                                              "Chrome/131.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        reqs = []
        page.on("request", lambda r: reqs.append(r.url))
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        page.locator(CLICK).first.click(timeout=8000)
        page.wait_for_timeout(2000)

        out.append("-- 提交前 --")
        out.extend(snap(page, reqs))
        reqs.clear()

        box = page.locator("#promptInput").first
        box.click()
        box.fill(Q)
        page.wait_for_timeout(500)
        out.append("-- 填完问题后（未提交）--")
        out.extend(snap(page, reqs))
        reqs.clear()

        # 1) 表单提交（submit 事件）而不是敲回车 —— 排除"回车没触发提交"
        submitted = False
        try:
            page.evaluate("() => { const f = document.querySelector('form.ai-response-form');"
                          " if (f) { f.requestSubmit ? f.requestSubmit() : f.submit(); return true; } return false; }")
            submitted = True
            out.append("✓ 已 requestSubmit() form.ai-response-form")
        except Exception as exc:                              # noqa: BLE001
            out.append(f"!! requestSubmit 失败：{exc}")
        page.wait_for_timeout(1000)
        box.press("Enter")
        page.wait_for_timeout(600)
        box.press("Enter")

        for i in range(12):
            page.wait_for_timeout(5000)
            out.append(f"-- 轮{i}（提交后 {(i + 1) * 5}s）--")
            out.extend(snap(page, reqs))
            if "（不存在）" not in out[-4]:
                pass
        page.screenshot(path=os.path.join(os.environ.get("TEMP", "."), "probe_juniper_captcha.png"))
        out.append(f"submitted_via_js={submitted}")
        ctx.close()
        browser.close()

    dest = os.path.join(os.environ.get("TEMP", "."), "probe_juniper_captcha.txt")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out))
    print(f"\n✓ 落盘：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

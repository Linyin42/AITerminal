#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aruba Ask AI 入口到底怎么开（HPE 文档站，与 Juniper 同款 ChatBot）。

已知：
- `/documentation/` 索引页有 `button.ask-ai-btn`，点开后 HTML 里只有 `ai-modal-wrap`（1 处）
  + hcaptcha（3 处），**没有 `#promptInput` / `#resultContainer` / `ai-response-form`**
  ⇒ 索引页的 Ask AI 不是 Juniper 那套完整对话弹窗；
- 子代理报告称 `…/documentation/aruba/` 对脚本客户端返回 403（浏览器可能能过）；
- `usePwpTopic-*.js` 里有 Ask AI 作用域菜单 `aruba:{label:"Aruba Networking", websiteId:…}`。

本探针逐条试：① 索引页点 ask-ai-btn ② 索引页点 ai-modal-trigger ③ 直接开 /documentation/aruba/
每条都报 `#promptInput` / `#resultContainer` / hcaptcha iframe 的有无。

用法：python tools/probe_aruba_ai.py
"""
from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CASES = [
    ("A 索引页 + button.ask-ai-btn", "https://networkingdocs.hpe.com/documentation/", "button.ask-ai-btn"),
    ("B 索引页 + div.ai-modal-trigger", "https://networkingdocs.hpe.com/documentation/", "div.ai-modal-trigger"),
    ("C 直接开 /documentation/aruba/", "https://networkingdocs.hpe.com/documentation/aruba/", None),
    ("D 索引页 + ask-ai-btn（等 10s 看弹窗迟到）",
     "https://networkingdocs.hpe.com/documentation/", "button.ask-ai-btn"),
]

CHECK = r"""
() => {
  const c = (s) => document.querySelectorAll(s).length;
  const vis = (s) => { const e = document.querySelector(s); return !!e && (e.offsetParent !== null || e.getClientRects().length); };
  const inp = document.querySelector('#promptInput');
  return {
    promptInput: c('#promptInput'), promptVisible: vis('#promptInput'),
    resultContainer: c('#resultContainer'),
    aiModalWrap: c('.ai-modal-wrap'), aiModalVisible: vis('.ai-modal-wrap'),
    responseForm: c('form.ai-response-form'),
    hcaptchaIframe: c('iframe[src*=hcaptcha]'), hcaptchaDiv: c('div.h-captcha'),
    textareas: [...document.querySelectorAll('textarea')].map(e => [e.id || '', e.getAttribute('class') || '',
                e.getAttribute('placeholder') || '']),
    bodyText: (document.body.innerText || '').replace(/\s+/g, ' ').length,
    askAiBtns: [...document.querySelectorAll('button.ask-ai-btn, .ai-modal-trigger')].length,
    title: document.title,
  };
}
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    out = ["=== Aruba Ask AI 入口探针 ==="]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        for label, url, target in CASES:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                  "Chrome/131.0.0.0 Safari/537.36"))
            page = ctx.new_page()
            out.append("=" * 70)
            out.append(f"[{label}] {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
            except Exception as exc:                          # noqa: BLE001
                out.append(f"  !! 打不开：{exc}")
                ctx.close()
                continue
            out.append("  打开后： " + str(page.evaluate(CHECK)))
            if target:
                try:
                    page.locator(target).first.click(timeout=8000)
                    out.append(f"  ✓ 点了 {target}")
                except Exception as exc:                      # noqa: BLE001
                    out.append(f"  !! 点 {target} 失败：{exc}")
                page.wait_for_timeout(4000 if not label.startswith("D") else 10000)
                out.append("  点开后： " + str(page.evaluate(CHECK)))
                try:
                    inp = page.locator("#promptInput")
                    if inp.count() and inp.first.is_visible():
                        inp.first.fill("show interfaces terse 是什么")
                        page.wait_for_timeout(500)
                        inp.first.press("Enter")
                        out.append("  ✓ 已提交问题，等 45s…")
                        page.wait_for_timeout(45000)
                        out.append("  提交后： " + str(page.evaluate(CHECK)))
                        out.append("  正文： " + str(page.evaluate(
                            "() => { const e = document.getElementById('resultContainer');"
                            " return e ? e.innerText.replace(/\\s+/g,' ').slice(0, 600) : '（无 resultContainer）'; }")))
                except Exception as exc:                      # noqa: BLE001
                    out.append(f"  !! 提问失败：{exc}")
            page.screenshot(path=os.path.join(
                os.environ.get("TEMP", "."), f"probe_aruba_{label[0]}.png"))
            ctx.close()
        browser.close()

    dest = os.path.join(os.environ.get("TEMP", "."), "probe_aruba_ai.txt")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out))
    print(f"\n✓ 落盘：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锐捷（Ruijie）有没有**网页 AI 问答入口** —— 浏览器实测探针。

背景（子代理联网取证 2026-10-18）：
- 官网文档页里 `文档AI助手入口` 整段被 HTML 注释 ⇒ 页面无生效组件；
- 但官方新闻稿说有企业级 AI 助手 **RITA**，SMB 线有 **Reyee livechat**；
- 静态 HTTP 抓取只拿到导航框架 ⇒ 必须真浏览器跑一遍，看请求里有没有 AI 后端。

判定：有 ⇒ 输出接口/入口，交给人写进 assistant_driver.py；没有 ⇒ 锐捷归"本地兜底"。
"""
from __future__ import annotations

import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PAGES = [
    "https://www.ruijie.com.cn/fw/wd-sp-lxznywpts/",          # 文档页（AI 入口被注释掉的那页）
    "https://www.ruijie.com.cn/fw/",                          # 服务与支持
    "https://reyee.ruijie.com/zh-cn/support/livechat-overview/",
    "https://reyee.ruijie.com/id-id/support/livechat-overview/",
]

#: 命中任一即算"疑似 AI/聊天入口"
HINTS = ["rita", "livechat", "live-chat", "chat", "ai", "bot", "kapa", "zendesk",
         "tawk", "客服", "智能", "问答", "机器人"]
#: 后端接口关键词（看网络请求）
API_HINTS = ["chat", "ai", "robot", "bot", "llm", "nlu", "dialog", "kapa", "livechat"]


def main() -> int:
    from playwright.sync_api import sync_playwright

    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        for url in PAGES:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                  "Chrome/131.0.0.0 Safari/537.36"))
            page = ctx.new_page()
            reqs = []
            page.on("request", lambda r: reqs.append(r.url))
            out.append("=" * 78)
            out.append(f"URL: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                # 滚动到底 + 等懒加载的聊天组件
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(3000)
            except Exception as exc:                          # noqa: BLE001
                out.append(f"!! 打开失败：{exc}")
                ctx.close()
                continue

            out.append(f"TITLE: {page.title()}")
            # 1) 有 shadowRoot 的 host（Kapa/livechat 常在 shadow 里）
            hosts = page.evaluate(
                "() => [...document.querySelectorAll('*')].filter(e => e.shadowRoot)"
                ".map(e => [e.tagName, e.getAttribute('id')||'', e.getAttribute('class')||''])"
            )
            out.append(f"SHADOW HOSTS: {hosts if hosts else '（无）'}")
            # 2) iframe
            ifr = page.evaluate("() => [...document.querySelectorAll('iframe')].map(e => e.src)")
            out.append(f"IFRAMES: {ifr if ifr else '（无）'}")
            # 3) 命中 HINTS 的元素（id/class/aria-label/href/text 片段）
            hits = page.evaluate(
                """(hints) => {
                    const res = [];
                    const attrs = ['id','class','aria-label','href','data-tooltip','title','placeholder'];
                    for (const e of document.querySelectorAll('*')) {
                        const blob = attrs.map(a => e.getAttribute(a) || '').join(' ')
                                     + ' ' + (e.children.length === 0 ? (e.innerText || '') : '');
                        const low = blob.toLowerCase();
                        for (const h of hints) {
                            if (low.includes(h)) {
                                res.push([e.tagName, h, blob.replace(/\\s+/g,' ').trim().slice(0,140)]);
                                break;
                            }
                        }
                    }
                    return res.slice(0, 40);
                }""", HINTS)
            out.append(f"-- 命中关键词的元素（{len(hits)}）--")
            for h in hits:
                out.append("   " + " | ".join(str(x) for x in h))
            # 4) 网络请求里的疑似 AI 接口
            api = sorted({u for u in reqs if any(k in u.lower() for k in API_HINTS)})
            out.append(f"-- 疑似 AI/聊天请求（{len(api)} / 共 {len(reqs)} 个请求）--")
            for u in api[:40]:
                out.append("   " + u[:200])
            # 5) 页面里出现的关键词上下文（原文取证）
            try:
                txt = re.sub(r"\s+", " ", page.inner_text("body"))
            except Exception:                                 # noqa: BLE001
                txt = ""
            for kw in ("RITA", "livechat", "在线客服", "智能客服", "机器人"):
                for m in re.finditer(re.escape(kw), txt, re.I):
                    out.append(f"   [正文] …{txt[max(0, m.start()-60):m.start()+90]}…")
                    break
            ctx.close()
        browser.close()

    dest = os.path.join(os.environ.get("TEMP", "."), "probe_ruijie_ai.txt")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out))
    print(f"\n✓ 落盘：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

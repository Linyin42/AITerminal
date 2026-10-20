#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锐捷「阿在(AZAI)」端到端实测 —— 用**中签 key** 绕过灰度抽签。

依据 `ai-entry-gate-v1.0.js`（2026-10-18 取证，47KB）：
  L11  AI_GUEST_HIT_KEY = 'azai-guest-random-gate:allowed'
  L511 AI_GUEST_RANDOM_RATIO = 0.75（未登录访客抽签比例）
  L519 isGuestHit(): localStorage[key] === '1' ⇒ 直接放行
  L527 中签后写 '1'（永久）
  L567 shouldRender = Math.random() < 0.75
  L605 wrapper.id = 'ai-conversation-dynamic-root'（动态根节点）
  L600 已在 DOM 就不重复注入
  · 登录用户另走白名单接口 /application/api/user/ai-auth-white（比例 0.5）
  · Safari 一律不加载（含 iPad 桌面版）
⇒ 应用侧只要**预置这个 key**，就不用碰那 25% 的运气。

用法：python tools/probe_ruijie_ai3.py
"""
from __future__ import annotations

import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

URL = "https://www.ruijie.com.cn/fw/wd-sp-lxznywpts/"
Q = "display vlan 命令的作用和输出字段解释"
SEED = "try{localStorage.setItem('azai-guest-random-gate:allowed','1')}catch(e){}"

PANEL_JS = r"""
() => {
  const q = (s) => [...document.querySelectorAll(s)];
  const vis = (e) => (e.offsetParent !== null || e.getClientRects().length);
  const root = document.getElementById('ai-conversation-dynamic-root');
  return {
    htmlClass: document.documentElement.className,
    bodyClass: document.body ? document.body.className : '',
    root: !!root,
    rootText: root ? (root.innerText || '').replace(/\s+/g, ' ').slice(0, 300) : '',
    avatar: q('.AI-tips__avatar').length,
    hello: q('.AI-tips__hello').length,
    panel: !!document.getElementById('AI-panel'),
    panelText: (document.getElementById('AI-panel') || {}).innerText
      ? document.getElementById('AI-panel').innerText.replace(/\s+/g, ' ').slice(0, 400) : '',
    inputs: q('.ai-chat__input, #AI-panel textarea, #AI-panel [contenteditable=true]')
      .map(e => [e.tagName, e.getAttribute('class') || '', vis(e) ? 'visible' : 'hidden']),
    panelBtns: q('#AI-panel button, #AI-panel [role=button], #AI-panel [data-action]')
      .map(e => [e.tagName, (e.getAttribute('class') || '').slice(0, 60),
                 e.getAttribute('aria-label') || '', e.getAttribute('data-action') || '',
                 (e.innerText || '').trim().slice(0, 24),
                 e.outerHTML.indexOf('send') >= 0 ? 'HTML含send' : '']),
  };
}
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    out = [f"=== 锐捷 阿在(AZAI) :: {URL}", f"QUESTION: {Q}", f"SEED: {SEED}"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_init_script(SEED)          # 每次导航前预置中签 key
        page = ctx.new_page()
        reqs = []
        page.on("request", lambda r: reqs.append(r.url))
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(9000)

        st = page.evaluate(PANEL_JS)
        out.append(f"== 加载后 ==")
        out.append(f"   html.class={st['htmlClass']!r}  body.class={st['bodyClass']!r}")
        out.append(f"   动态根 {st['root']}  头像 {st['avatar']}  问候 {st['hello']}  面板 {st['panel']}")
        out.append(f"   根文本：{st['rootText'][:150]}")

        if not st["root"]:
            out.append("!! 中签 key 没生效 ⇒ 门脚本没跑到/被 Safari 判定拦掉")
            out.append("   localStorage: " + str(page.evaluate(
                "() => { try { return Object.keys(localStorage).slice(0, 20) } catch (e) { return String(e) } }")))
        else:
            # ---- 点头像开面板 ----
            for sel in (".AI-tips__avatar", ".AI-tips__hello", "#ai-conversation-dynamic-root"):
                loc = page.locator(sel)
                try:
                    if loc.count() and loc.first.is_visible():
                        loc.first.click(timeout=5000)
                        out.append(f"✓ 点开入口：{sel}")
                        break
                except Exception as exc:                      # noqa: BLE001
                    out.append(f"   {sel} 点击失败：{exc}")
            page.wait_for_timeout(5000)
            st = page.evaluate(PANEL_JS)
            out.append(f"== 点开后 == panel={st['panel']}")
            out.append(f"   面板文本：{st['panelText'][:250]}")
            out.append("   -- 面板内输入框 --")
            for i in st["inputs"]:
                out.append("      " + " | ".join(str(x) for x in i))
            out.append("   -- 面板内按钮 --")
            for b in st["panelBtns"]:
                out.append("      " + " | ".join(str(x) for x in b))
            page.screenshot(path=os.path.join(os.environ.get("TEMP", "."), "probe_ruijie_ai3_panel.png"))

            # ---- 提问 ----
            n0 = len(reqs)
            box = None
            for sel in (".ai-chat__input", "#AI-panel textarea", "#AI-panel [contenteditable=true]"):
                loc = page.locator(sel)
                try:
                    if loc.count() and loc.first.is_visible():
                        box = loc.first
                        out.append(f"✓ 输入框：{sel}")
                        break
                except Exception:                             # noqa: BLE001
                    continue
            if box is None:
                out.append("!! 面板里没有可见输入框")
            else:
                box.click()
                box.fill(Q)
                page.wait_for_timeout(600)
                sent = False
                for sel in ("#AI-panel button:has(svg)", "#AI-panel [class*=send]",
                            "#AI-panel [data-action*=send]", "#AI-panel button"):
                    loc = page.locator(sel)
                    if loc.count():
                        try:
                            loc.last.click(timeout=5000)
                            out.append(f"✓ 点发送（{sel}，共 {loc.count()} 个）")
                            sent = True
                            break
                        except Exception:                     # noqa: BLE001
                            continue
                if not sent:
                    box.press("Enter")
                    out.append("✓ 回车发送")

                prev, stable = "", 0
                for i in range(22):
                    page.wait_for_timeout(4000)
                    txt = page.evaluate(
                        "() => { const p = document.getElementById('AI-panel');"
                        " return p ? (p.innerText || '') : ''; }")
                    out.append(f"   轮{i}: 面板 {len(txt)} 字")
                    if txt and txt == prev:
                        stable += 1
                        if stable >= 3:
                            break
                    else:
                        stable = 0
                    prev = txt
                out.append("== 面板最终文本（末尾 2500 字）==")
                out.append(prev[-2500:])

            out.append("== azai/dialogue 接口请求 ==")
            for u in reqs[n0:]:
                if re.search(r"azai|dialogue|gw\.ruijie", u, re.I):
                    out.append("   " + u[:180])
            page.screenshot(path=os.path.join(os.environ.get("TEMP", "."), "probe_ruijie_ai3_answer.png"))

        ctx.close()
        browser.close()

    dest = os.path.join(os.environ.get("TEMP", "."), "probe_ruijie_ai3.txt")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out[:70]))
    print(f"\n…（共 {len(out)} 行）✓ 落盘：{dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""厂商 AI 助手直连 driver（应用侧用，不依赖 DSH 的 bsk 工具）。

架构（2026-10 定）
================
- **联网**：默认去问厂商的 AI 助手（华为 WeiKnow / 锐捷阿在 已端到端实测跑通）。
- **本地**：只在无网或厂商验证码挡住时兜底（`local_llm/retrieve.py`）。

为什么用 Playwright + 持久化 profile
==================================
- 应用（AITerminal）不能调用 DSH 的 bsk_* 工具 ⇒ 必须自带浏览器驱动；
- `launch_persistent_context(user_data_dir=...)` 把登录态（cookie）落在**磁盘 profile** 里
  ⇒ **登录一次、长期复用**（华为 SSO 到期才需重登，厂商限制，绕不过）；
- 我们**不复制**用户的密码/cookie ✗，登录动作由用户在自己看得见的窗口里完成 ✓。

用法
====
    python tools/assistant_driver.py ask --vendor huawei --q "display vlan 命令解释"
    python tools/assistant_driver.py login --vendor huawei     # 打开窗口让用户登录一次
    python tools/assistant_driver.py ask --vendor huawei --q "..." --headless
    python tools/assistant_driver.py dump --vendor juniper     # 落盘真实 DOM 取证（含 shadow DOM）
    python tools/assistant_driver.py vendors                   # 列出已配置厂商
    python tools/assistant_driver.py probe                     # 逐家体检（能否拿到答案/卡在哪）

退出码
======
    0 拿到答案 / 3 需登录 / 4 选择器失效 / 5 超时无内容 / 6 **被验证码挡住（需人工过码）**

选择器铁律（都实测踩过，2026-10-18）
=================================
1. **按候选顺序试，不能用逗号串 `.first`**：Playwright 的 `.first` 取**文档序最先**的匹配项，
   逗号串里 `body` 永远排第一 ⇒ 读回来是整页。
2. **逗号串必须是纯 CSS**：混进 `text=Ask AI` 会报 `Unexpected token "="`；要按文案找用 `:has-text()`。
3. **`done` 为空 ⇒ 不能拿 `"" in txt` 判完成**（恒真，会把欢迎语当答案）；
   空 `done` 一律走"文本不再增长"收尾，且**必须先把发问前的基线文本记下来**，否则会停在欢迎语。
4. Playwright 的 CSS **穿透 open shadow root**；但 2026-10-18 实测 HPE 已换掉 Kapa——Juniper/Aruba
   现在是站点自研 `ChatBot-*.js`，**没有 shadow DOM**。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

#: 每个厂商的助手：入口页、输入框候选选择器、答案容器、完成标记。
#: 选择器都给**多个候选**，按顺序试第一个存在的 —— 站点改版时不至于直接死掉。
#: `answer` 是**列表**（不是逗号串），按作者优先级取第一个有内容的。
#: `init_js` 在**每次导航前**执行（灰度抽签类站点的确定性绕过）。
#: `dismiss` 是打开助手后必须依次点掉的遮挡层（隐私弹窗等）。
VENDORS = {
    "huawei": {
        "name": "华为 WeiKnow",
        "url": "https://weiknow.gts.huawei.com/weiknow/#/question?locale=zh_CN&lang=zh&source=Forum",
        "input": ['textarea[placeholder*="请输入"]', "textarea", '[contenteditable="true"]'],
        "answer": ["main", "body"],
        "done": "已生成答案",
        "login_hint": "Uniportal账号登录",
    },
    "ruijie": {
        # ---- 2026-10-18 实测跑通：灰度门 + 隐私弹窗都已定位 ----
        "name": "锐捷 阿在（AZAI 智能问答）",
        "url": "https://www.ruijie.com.cn/fw/",
        # ai-entry-gate-v1.0.js：未登录访客按 AI_GUEST_RANDOM_RATIO=0.75 抽签，
        # 中签写 localStorage['azai-guest-random-gate:allowed']='1'（永久）。
        # 预置这个 key ⇒ 不用碰 25% 的运气（实测有效）。
        "init_js": "try{localStorage.setItem('azai-guest-random-gate:allowed','1')}catch(e){}",
        # 入口是右下角头像/问候（AI-tips__avatar / AI-tips__hello）；
        # ⚠ 不要把 `#ai-conversation-dynamic-root` 放进来：那层容器不可见（见 _click_first_visible）
        "click": [".AI-tips__avatar", ".AI-tips__hello"],
        # 首次进面板弹隐私声明：先勾「同意」再提交
        "dismiss": ['button.ai-privacy__agree[data-action="toggle-agree"]',
                    "button.ai-privacy__submit"],
        "input": ["textarea.AI-panel__input", "#AI-panel textarea"],
        "send": ['button.AI-panel__send[aria-label="发送"]', "button.AI-panel__send"],
        # ai-app.js：流式输出期间给发送按钮加 `is-stop`（图标换成暂停），结束再摘掉
        # ⇒ 这是**真·完成信号**，比"文本不涨"可靠。
        "busy": ["button.AI-panel__send.is-stop"],
        "answer": ["#AI-panel-messages", "#AI-panel"],
        "done": "",                  # 无固定完成文案 ⇒ 走"基线 + 不再增长"
        "login_hint": "",
        "need_login": False,
    },
    "h3c": {
        "name": "H3C AI助手（iClaw）",
        "url": "https://ai.h3c.com/",
        "input": ['textarea[placeholder*="请输入"]', "textarea", '[contenteditable="true"]'],
        "answer": ["body"],          # ⚠ 需真机实测：回答区容器未取到，先用 body 兜底
        "done": "回答结果",          # 完成标记候选：回答结果 / 停止回答 / 生成中
        "login_hint": "退出登录",    # 未登录时页面没有这个文案 ⇒ 可当"已登录"标志用
        "need_login": True,
    },
    "juniper": {
        # ---- 2026-10-18 实测：**不是 Kapa**，是站点自研 ChatBot；且提交被 hCaptcha 挡死 ----
        "name": "Juniper Ask AI（HPE 自研 ChatBot，hCaptcha 门）",
        "url": "https://networkingdocs.hpe.com/documentation/juniper/",
        "click": ['button.ask-ai-btn', 'button[aria-label="Open Juniper Ask AI"]'],
        "input": ["#promptInput", "textarea"],
        "answer": ["#resultContainer"],
        "done": "",
        # 页面上的 div.llm-hcaptcha 是静态"Protected by hCaptcha"字样（提交前就有），
        # 真正被挑战的信号是 **hcaptcha 的 iframe 被插进来**（提交前 0 个、提交后 4 个）。
        "captcha": ["iframe[src*=hcaptcha]", "div.h-captcha"],
        "login_hint": "",
        "need_login": False,
    },
    "aruba": {
        "name": "Aruba Networking Ask AI（HPE 自研 ChatBot，hCaptcha 门）",
        "url": "https://networkingdocs.hpe.com/documentation/",
        "click": ['div.ai-modal-trigger', 'button.ask-ai-btn', 'button[aria-label*="Ask AI"]'],
        "input": ["#promptInput", "textarea"],
        "answer": ["#resultContainer"],
        "done": "",
        "captcha": ["iframe[src*=hcaptcha]", "div.h-captcha"],
        "login_hint": "",
        "need_login": False,
    },
    "cisco": {
        "name": "Cisco 搜索（AI 答案块）",
        # ⚠ 无独立聊天框：走搜索结果页的 AI 答案块；且**AI 答案仅登录用户可见**
        "url": "https://search.cisco.com/search?query={q}",
        "input": [],                 # 无需填输入框：问题直接进 URL
        "answer": ["body"],
        "done": "Results generated by AI",
        "login_hint": "Sign in",
        "need_login": True,
    },
    # 未找到可验证入口（子代理取证 + 本人实测）：
    #   zte 中兴（网页端只有 /robot SPA 外壳，AI 性质未证实，需登录）
    #   fiberhome 烽火 / maipu 迈普（确认没有网页问答入口）
}

PROFILE_ROOT = os.path.join(os.path.expanduser("~"), ".aiterminal", "assistant_profiles")

#: 取证用：把 body 序列化成含 shadow DOM 的 HTML（`page.content()` **不含** shadow 内容）。
DUMP_HTML_JS = r"""
() => {
  const ser = (node) => {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    const tag = node.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript') return '';
    let attrs = '';
    for (const a of node.attributes) attrs += ' ' + a.name + '="' + a.value + '"';
    let inner = '';
    for (const c of node.childNodes) inner += ser(c);
    if (node.shadowRoot) {
      inner += '<!--#shadow-root-->';
      for (const c of node.shadowRoot.childNodes) inner += ser(c);
      inner += '<!--/#shadow-root-->';
    }
    return '<' + tag + attrs + '>' + inner + '</' + tag + '>';
  };
  return ser(document.body);
}
"""


def _ctx(vendor: str, headless: bool):
    from playwright.sync_api import sync_playwright
    cfg = VENDORS[vendor]
    profile = os.path.join(PROFILE_ROOT, vendor)
    os.makedirs(profile, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        profile, headless=headless,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    if cfg.get("init_js"):
        ctx.add_init_script(cfg["init_js"])
    return pw, ctx, cfg


def _click_first_visible(page, sels, timeout: int = 8000) -> str:
    """按**作者给的优先级**点第一个**可见**的候选，返回命中的选择器。

    ⚠ 这里同样不能用逗号串 + `.first`：Playwright 取**文档序最先**的匹配项，
    锐捷的 `#ai-conversation-dynamic-root`（包裹容器，本身不可见）排在头像前面
    ⇒ 点它直接 Timeout 8s（2026-10-18 实测踩过）。
    """
    if isinstance(sels, str):
        sels = [sels]
    for sel in sels:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=timeout)
                    return sel
        except Exception:                                    # noqa: BLE001
            continue
    return ""


def _open_page(ctx, cfg, question=""):
    """打开入口页 → 点开页内助手 → 点掉遮挡层。"""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(cfg["url"].format(q=question), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    if cfg.get("click"):
        hit = _click_first_visible(page, cfg["click"])
        if hit:
            page.wait_for_timeout(3000)
            print(f"✓ 已点开助手：{hit}")
        else:
            print(f"!! 打不开助手（候选都没命中可见元素：{cfg['click']}）")
    for sel in cfg.get("dismiss", []):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=5000)
                print(f"✓ 已点掉遮挡层：{sel}")
                page.wait_for_timeout(1200)
        except Exception as exc:                              # noqa: BLE001
            print(f"   （遮挡层 {sel} 跳过：{exc}）")
    return page


def _find_input(page, cfg):
    for sel in cfg["input"]:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return loc
        except Exception:                                    # noqa: BLE001
            continue
    return None


def _answer_text(page, cfg) -> str:
    """按**候选顺序**读回答区文本（见模块头"选择器铁律"第 1 条）。"""
    for sel in cfg["answer"]:
        try:
            loc = page.locator(sel)
            n = loc.count()
            if not n:
                continue
            target = loc.last if cfg.get("answer_last") else loc.first
            txt = target.inner_text(timeout=5000)
            if txt and txt.strip():
                return txt
        except Exception:                                    # noqa: BLE001
            continue
    return ""


def _any_visible(page, sels) -> bool:
    """是否有任一候选**可见**地存在（用于"还在生成中"这类状态位）。"""
    for sel in sels or []:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return True
        except Exception:                                    # noqa: BLE001
            continue
    return False


def _captcha_up(page, cfg) -> bool:
    """提交后是否被验证码挑战（信号 = 验证码 iframe 被插进 DOM）。"""
    for sel in cfg.get("captcha", []):
        try:
            if page.locator(sel).count():
                return True
        except Exception:                                    # noqa: BLE001
            continue
    return False


def do_login(vendor: str) -> int:
    pw, ctx, cfg = _ctx(vendor, headless=False)
    try:
        page = _open_page(ctx, cfg)
        print(f"已打开 {cfg['name']}：{cfg['url']}")
        print("请在窗口里完成登录；登录成功后本脚本会自动继续（最多等 300 秒）…")
        for _ in range(150):
            time.sleep(2)
            if _find_input(page, cfg):
                print("✓ 检测到输入框 ⇒ 登录成功，profile 已保存，后续无需再登")
                break
        else:
            print("✗ 300 秒内没检测到输入框（可能仍在登录页）")
    finally:
        ctx.close()
        pw.stop()
    return 0


def do_ask(vendor: str, question: str, headless: bool, wait_s: int) -> int:
    pw, ctx, cfg = _ctx(vendor, headless)
    try:
        page = _open_page(ctx, cfg, question)

        if cfg.get("login_hint") and cfg["login_hint"] in (page.content() or "") and not _find_input(page, cfg):
            print(f"!! 需要登录：请在窗口里登录一次后重跑（提示：python {sys.argv[0]} login --vendor {vendor}）")
            return 3

        box = _find_input(page, cfg)
        if cfg["input"]:                                     # cisco 走 URL 传参，无需输入框
            if box is None:
                print("!! 找不到输入框：站点可能改版，请更新 VENDORS 里的选择器（用 dump 取证）")
                return 4
            box.click()
            box.fill(question)
            # ⚠ 有的站点不吃回车（锐捷阿在有自己的发送按钮），有的没有按钮（HPE ChatBot 只能回车）
            if cfg.get("send"):
                hit = _click_first_visible(page, cfg["send"])
                if hit:
                    print(f"✓ 已点发送按钮：{hit}")
                else:
                    print(f"!! 没点到发送按钮（{cfg['send']}）⇒ 退回回车")
                    page.keyboard.press("Enter")
            else:
                page.keyboard.press("Enter")
            print(f"已发送：{question}")

        # ⚠ 记下**发问前的基线文本**：没有 `done` 文案的站点，只能靠"文本变化 + 不再增长"收尾，
        #   否则第一轮就把欢迎语当答案收工（2026-10-18 juniper 实测踩过）。
        page.wait_for_timeout(1200)
        baseline = _answer_text(page, cfg)

        deadline = time.time() + wait_s
        last, prev, stable, blocked = "", baseline, 0, 0
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            txt = _answer_text(page, cfg)
            if not txt:
                continue
            if cfg["done"]:
                if cfg["done"] in txt and len(txt) <= len(prev):
                    last = txt
                    break
            elif txt != baseline:
                # ⚠ 生成中的站点会**停顿**（锐捷阿在检索知识库时正文 3 秒不涨）
                #   ⇒ 光看"文本不再增长"会把「我正在知识库里查询」当成答案收工
                #   （2026-10-18 实测只拿到 25 字）。有 busy 状态位就以它为准。
                if _any_visible(page, cfg.get("busy")):
                    stable = 0
                elif txt == prev:
                    stable += 1
                    if stable >= 3:
                        last = txt
                        break
                else:
                    stable = 0
            if cfg.get("captcha") and _captcha_up(page, cfg) and txt == baseline:
                blocked += 1
                if blocked >= 2:
                    print("!! 被人机验证（hCaptcha）挡住：站点在提交后发了验证码挑战，"
                          "自动化过不去 ⇒ 需要人工在窗口里过一次码，或走本地兜底。")
                    return 6
            else:
                blocked = 0
            prev = txt
        if not last:
            if prev and prev != baseline:
                print(f"!! {wait_s} 秒内没检测到收尾，给出最后读到的内容")
                last = prev
            else:
                print(f"!! {wait_s} 秒内没拿到回答（回答区文本没变化）")
                return 5

        body = re.sub(r"^\s*" + re.escape(question) + r"\s*", "", last)
        # 各自站点会在答案开头贴一句完成状态（华为「已生成答案」/ 锐捷「阿在已完成」）
        for kw in ["已生成答案", "阿在已完成"] + list(cfg.get("strip", [])):
            body = re.sub(r"^\s*" + re.escape(kw) + r"\s*", "", body)
        body = body.strip()
        print("-" * 70)
        print(body)
        print("-" * 70)
        print(f"来源：联网 · {cfg['name']}（{len(body)} 字）")
        return 0
    finally:
        ctx.close()
        pw.stop()


def do_dump(vendor: str, out_path: str) -> int:
    """把助手打开后的**真实 DOM**落盘：站点改版后用它重取选择器。"""
    pw, ctx, cfg = _ctx(vendor, headless=False)
    try:
        page = _open_page(ctx, cfg)
        lines = [f"URL: {page.url}", f"TITLE: {page.title()}", ""]

        lines.append("== BUTTONS（Playwright 选择器，穿透 open shadow root）==")
        btns = page.locator("button, [role=button], a[href]")
        n = min(btns.count(), 300)
        for i in range(n):
            el = btns.nth(i)
            try:
                if not el.is_visible():
                    continue
                meta = el.evaluate(
                    "e => [e.tagName, e.getAttribute('class')||'', e.getAttribute('aria-label')||'',"
                    " (e.innerText||'').trim().slice(0,50), e.getAttribute('id')||'']"
                )
                lines.append(f"  {i:>3} | " + " | ".join(str(m) for m in meta))
            except Exception:                                # noqa: BLE001
                continue

        lines.append("")
        lines.append("== INPUTS ==")
        for sel in ("textarea", "input", "[contenteditable=true]"):
            loc = page.locator(sel)
            for i in range(min(loc.count(), 100)):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    meta = el.evaluate(
                        "e => [e.tagName, e.getAttribute('class')||'', e.getAttribute('placeholder')||'',"
                        " e.getAttribute('aria-label')||'', e.getAttribute('id')||'']"
                    )
                    lines.append(f"  {sel} {i} | " + " | ".join(str(m) for m in meta))
                except Exception:                            # noqa: BLE001
                    continue

        lines.append("")
        lines.append("== SHADOW HOSTS ==")
        try:
            hosts = page.evaluate(
                "() => [...document.querySelectorAll('*')].filter(e => e.shadowRoot)"
                ".map(e => [e.tagName, e.getAttribute('id')||'', e.getAttribute('class')||'',"
                " e.shadowRoot.innerHTML.length])"
            )
            for h in hosts:
                lines.append("  " + " | ".join(str(x) for x in h))
            if not hosts:
                lines.append("  （无 shadow host）")
        except Exception as exc:                             # noqa: BLE001
            lines.append(f"  !! {exc}")

        lines.append("")
        lines.append("== ANSWER 候选 ==")
        for sel in cfg["answer"]:
            try:
                cnt = page.locator(sel).count()
                txt = _answer_text(page, {"answer": [sel]})
                head = re.sub(r"\s+", " ", txt)[:120] if txt else ""
                lines.append(f"  [{cnt} 个] {sel} ⇒ innerText {len(txt)} 字：{head}")
            except Exception as exc:                         # noqa: BLE001
                lines.append(f"  !! {sel}: {exc}")

        lines.append("")
        lines.append("== 验证码信号 ==")
        for sel in cfg.get("captcha", []):
            try:
                lines.append(f"  [{page.locator(sel).count()} 个] {sel}")
            except Exception as exc:                         # noqa: BLE001
                lines.append(f"  !! {sel}: {exc}")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        html_path = out_path + ".html"
        try:
            html = page.evaluate(DUMP_HTML_JS)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html or "")
        except Exception as exc:                             # noqa: BLE001
            html_path = f"（导出失败：{exc}）"
        print(f"✓ 已落盘：{out_path}")
        print(f"✓ 含 shadow DOM 的 HTML：{html_path}")
        print(json.dumps({"url": page.url, "buttons": btns.count()}, ensure_ascii=False))
        return 0
    finally:
        ctx.close()
        pw.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["ask", "login", "vendors", "dump"])
    ap.add_argument("--vendor", default="huawei")
    ap.add_argument("--q", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--headless", action="store_true", help="无头（首次登录必须留窗口）")
    ap.add_argument("--wait", type=int, default=120, help="等答案上限（秒）")
    a = ap.parse_args()

    if a.action == "vendors":
        for k, v in VENDORS.items():
            flags = []
            if v.get("need_login"):
                flags.append("需登录")
            if v.get("captcha"):
                flags.append("有验证码门")
            if v.get("init_js"):
                flags.append("有灰度门(可绕过)")
            print(f"  {k:<9} {v['name']:<44} {'/'.join(flags) or '直连'}")
        return 0
    if a.vendor not in VENDORS:
        print(f"!! 未配置的厂商：{a.vendor}（可用：{', '.join(VENDORS)}）")
        return 2
    if a.action == "login":
        return do_login(a.vendor)
    if a.action == "dump":
        out = a.out or os.path.join(os.environ.get("TEMP", "."), f"assistant_dump_{a.vendor}.txt")
        return do_dump(a.vendor, out)
    if not a.q:
        print("!! ask 需要 --q")
        return 2
    return do_ask(a.vendor, a.q, a.headless, a.wait)


if __name__ == "__main__":
    sys.exit(main())

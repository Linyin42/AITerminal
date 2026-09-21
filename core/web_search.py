"""
联网检索 - 让 AI 能上网查厂商命令、版本特性、报错含义

只依赖标准库 urllib，不需要额外的搜索 API Key。
支持的检索后端:
    baidu       —— 百度（默认，中文技术内容召回最好）
    bing        —— 必应
    duckduckgo  —— DuckDuckGo HTML 版
    searxng     —— 自建/公共 SearXNG 实例（需在设置里填地址）
    auto        —— 按 baidu → bing → duckduckgo → searxng 顺序尝试
"""
import base64
import html as html_mod
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 12
MAX_DOWNLOAD = 2 * 1024 * 1024  # 单页最多下载 2MB

# 已知的"跳转链接"特征：这些 URL 本身不是目标页，需要跟随跳转拿到真实地址
REDIRECT_MARKERS = (
    "baidu.com/link?",
    "bing.com/ck/a",
    "google.com/url?",
    "so.com/link?",
    "sogou.com/link?",
)


class SearchError(Exception):
    """检索失败"""


# ------------------------------------------------------------------ 基础工具
def http_get(url: str, timeout: int = DEFAULT_TIMEOUT, headers: dict = None,
             max_bytes: int = MAX_DOWNLOAD):
    """
    发起 GET 请求

    返回: (文本, 最终URL) —— 最终URL 与传入不同说明发生了跳转
    """
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        req_headers.update(headers)

    request = urllib.request.Request(url, headers=req_headers)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            raw = resp.read(max_bytes)
            charset = resp.headers.get_content_charset()
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        raise SearchError(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise SearchError(f"网络不可达: {e.reason}")
    except socket.timeout:
        raise SearchError("请求超时")
    except Exception as e:
        raise SearchError(f"请求失败: {e}")

    for encoding in (charset, "utf-8", "gb18030", "latin-1"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding, errors="replace"), final_url
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace"), final_url


def http_get_text(url: str, timeout: int = DEFAULT_TIMEOUT, headers: dict = None,
                  max_bytes: int = MAX_DOWNLOAD) -> str:
    """只需要文本时的便捷封装"""
    text, _final = http_get(url, timeout, headers, max_bytes)
    return text


def is_redirect_url(url: str) -> bool:
    """判断是否为搜索引擎的跳转链接"""
    low = (url or "").lower()
    return any(marker in low for marker in REDIRECT_MARKERS)


def resolve_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """
    跟随跳转拿到真实地址

    百度/必应等的链接点击后由浏览器跳转，这里直接请求并读取最终 URL。
    解析失败时原样返回，不影响用户手动打开。
    """
    if not is_redirect_url(url):
        return url
    try:
        _text, final_url = http_get(url, timeout=timeout, max_bytes=64 * 1024)
        if final_url and not is_redirect_url(final_url):
            return final_url
    except SearchError:
        pass
    return url


def strip_tags(text: str) -> str:
    """去掉 HTML 标签并反转义实体"""
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', text or '', flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html_mod.unescape(text).strip()


def html_to_text(raw_html: str, max_chars: int = 8000) -> str:
    """把网页 HTML 转成可读纯文本（供 AI 阅读）"""
    if not raw_html:
        return ""
    text = re.sub(r'<(script|style|noscript|svg|head)[^>]*>.*?</\1>', ' ',
                  raw_html, flags=re.S | re.I)
    text = re.sub(r'<!--.*?-->', ' ', text, flags=re.S)
    # 块级标签转换行
    text = re.sub(r'<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_mod.unescape(text)
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    lines = [ln.strip() for ln in text.split('\n')]
    text = '\n'.join(ln for ln in lines if ln)
    return text[:max_chars]


def decode_bing_url(url: str) -> str:
    """还原必应的跳转链接（/ck/a?...&u=a1<base64>）"""
    if 'bing.com/ck/a' not in url:
        return url
    match = re.search(r'[?&]u=a1([^&]+)', url)
    if not match:
        return url
    payload = match.group(1)
    payload += '=' * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload).decode('utf-8', errors='replace')
    except Exception:
        return url


class SearchResult:
    """一条检索结果"""

    __slots__ = ("title", "url", "snippet", "source", "raw_url")

    def __init__(self, title: str, url: str, snippet: str = "",
                 source: str = "", raw_url: str = ""):
        self.title = title.strip()
        self.url = url.strip()
        self.snippet = re.sub(r'\s+', ' ', snippet or '').strip()
        self.source = source
        self.raw_url = raw_url or self.url  # 搜索引擎原始链接（可能是跳转链接）

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "source": self.source, "raw_url": self.raw_url}

    def __repr__(self):
        return f"<SearchResult {self.title[:40]} | {self.url[:60]}>"


# ------------------------------------------------------------------ 检索后端
class SearchBackend:
    """检索后端基类"""

    name = "base"

    def search(self, query: str, max_results: int = 5):
        raise NotImplementedError


class BaiduBackend(SearchBackend):
    """百度搜索（中文技术内容召回较好）"""

    name = "baidu"
    URL = "https://www.baidu.com/s?wd={q}&rn={n}"

    def __init__(self, resolve_links: bool = True, timeout: int = DEFAULT_TIMEOUT):
        self.resolve_links = resolve_links
        self.timeout = timeout

    def search(self, query: str, max_results: int = 5):
        url = self.URL.format(q=urllib.parse.quote(query), n=max(max_results, 10))
        raw = http_get_text(url, timeout=self.timeout)
        results = self.parse(raw)[:max_results]

        if self.resolve_links:
            for item in results:
                item.url = resolve_url(item.url, self.timeout)
        return results

    @staticmethod
    def parse(raw: str):
        results = []
        blocks = re.split(r'class="result[ "]', raw)[1:]
        for block in blocks:
            match = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                              block, re.S)
            if not match:
                continue
            raw_url = html_mod.unescape(match.group(1))
            title = strip_tags(match.group(2))
            if not title:
                continue

            # 摘要：优先取 abstract 区块，否则取标题之后的正文
            snippet = ""
            for pattern in (r'<div[^>]*class="c-abstract[^"]*"[^>]*>(.*?)</div>',
                            r'<span[^>]*class="content-right[^"]*"[^>]*>(.*?)</span>'):
                found = re.search(pattern, block, re.S)
                if found:
                    snippet = strip_tags(found.group(1))
                    if snippet:
                        break
            if not snippet:
                plain = strip_tags(block)
                snippet = plain[len(title):].strip()[:240] if title in plain else plain[:240]

            results.append(SearchResult(title, raw_url, snippet, "baidu", raw_url))
        return results


class BingBackend(SearchBackend):
    """必应网页搜索"""

    name = "bing"
    URL = "https://cn.bing.com/search?q={q}&count={n}&setlang=zh-CN"

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def search(self, query: str, max_results: int = 5):
        url = self.URL.format(q=urllib.parse.quote(query), n=max(max_results, 5))
        raw = http_get_text(url, timeout=self.timeout)
        return self.parse(raw)[:max_results]

    @staticmethod
    def parse(raw: str):
        results = []
        blocks = raw.split('class="b_algo"')[1:]
        for block in blocks:
            match = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                              block, re.S)
            if not match:
                match = re.search(r'<a[^>]*href="(http[^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not match:
                continue
            # href 里可能含 &amp; 实体，必须先反转义再解码跳转参数
            raw_url = html_mod.unescape(match.group(1))
            url = decode_bing_url(raw_url)
            title = strip_tags(match.group(2))
            if not url.startswith('http') or not title:
                continue

            snippet = ""
            for pattern in (r'<p[^>]*class="b_lineclamp[^"]*"[^>]*>(.*?)</p>',
                            r'<p[^>]*>(.*?)</p>',
                            r'<div[^>]*class="b_caption[^"]*"[^>]*>(.*?)</div>'):
                found = re.search(pattern, block, re.S)
                if found:
                    snippet = strip_tags(found.group(1))
                    if snippet:
                        break
            results.append(SearchResult(title, url, snippet, "bing", raw_url))
        return results


class DuckDuckGoBackend(SearchBackend):
    """DuckDuckGo HTML 版"""

    name = "duckduckgo"
    URL = "https://html.duckduckgo.com/html/?q={q}"

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def search(self, query: str, max_results: int = 5):
        url = self.URL.format(q=urllib.parse.quote(query))
        raw = http_get_text(url, timeout=self.timeout)
        return self.parse(raw)[:max_results]

    @staticmethod
    def parse(raw: str):
        results = []
        blocks = re.split(r'class="result results_links', raw)[1:]
        for block in blocks:
            match = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                              block, re.S)
            if not match:
                continue
            raw_url = html_mod.unescape(match.group(1))
            url = raw_url
            if 'uddg=' in url:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                url = params.get('uddg', [url])[0]
            title = strip_tags(match.group(2))
            snippet = ""
            found = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
            if found:
                snippet = strip_tags(found.group(1))
            if url.startswith('http') and title:
                results.append(SearchResult(title, url, snippet, "duckduckgo", raw_url))
        return results


class SearxngBackend(SearchBackend):
    """SearXNG 实例（优先 JSON 接口，失败回退 HTML 解析）"""

    name = "searxng"

    def __init__(self, base_url: str = "", timeout: int = DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip('/')
        self.timeout = timeout
        if not self.base_url:
            raise SearchError("未配置 SearXNG 实例地址")

    def search(self, query: str, max_results: int = 5):
        results = self._search_json(query, max_results)
        if results:
            return results
        return self._search_html(query, max_results)

    def _search_json(self, query: str, max_results: int):
        url = f"{self.base_url}/search?q={urllib.parse.quote(query)}&format=json"
        try:
            raw = http_get_text(url, timeout=self.timeout,
                                headers={"Accept": "application/json"})
            data = json.loads(raw)
        except Exception:
            return []
        results = []
        for item in (data.get("results") or [])[:max_results]:
            results.append(SearchResult(
                item.get("title", ""), item.get("url", ""),
                item.get("content", ""), "searxng"
            ))
        return results

    def _search_html(self, query: str, max_results: int):
        url = f"{self.base_url}/search?q={urllib.parse.quote(query)}"
        raw = http_get_text(url, timeout=self.timeout)
        results = []
        for block in re.split(r'class="result[ "]', raw)[1:]:
            match = re.search(r'<a[^>]*href="(http[^"]+)"[^>]*class="url[^"]*"', block)
            if not match:
                match = re.search(r'<h3[^>]*>\s*<a[^>]*href="(http[^"]+)"', block)
            if not match:
                continue
            title_match = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
            snippet_match = re.search(r'class="content"[^>]*>(.*?)</p>', block, re.S)
            results.append(SearchResult(
                strip_tags(title_match.group(1)) if title_match else match.group(1),
                html_mod.unescape(match.group(1)),
                strip_tags(snippet_match.group(1)) if snippet_match else "",
                "searxng"
            ))
            if len(results) >= max_results:
                break
        return results


BACKENDS = {
    "baidu": BaiduBackend,
    "bing": BingBackend,
    "duckduckgo": DuckDuckGoBackend,
    "searxng": SearxngBackend,
}

AUTO_ORDER = ("baidu", "bing", "duckduckgo", "searxng")


# ------------------------------------------------------------------ 客户端
class WebSearchClient:
    """
    联网检索客户端

    参数:
        backend: baidu / bing / duckduckgo / searxng / auto
        searxng_url: 使用 searxng 时的实例地址
        timeout: 单次请求超时秒数
        max_results: 默认返回条数
        resolve_links: 是否把搜索引擎跳转链接解析成真实地址
    """

    def __init__(self, backend: str = "auto", searxng_url: str = "",
                 timeout: int = DEFAULT_TIMEOUT, max_results: int = 5,
                 resolve_links: bool = True):
        self.backend = (backend or "auto").lower()
        self.searxng_url = searxng_url or ""
        self.timeout = timeout
        self.max_results = max_results
        self.resolve_links = resolve_links
        self.last_error = ""
        self.last_backend = ""

    # ------------------------------------------------------------ 构造后端
    def _build(self, name: str):
        cls = BACKENDS.get(name)
        if cls is None:
            return None
        if name == "searxng":
            if not self.searxng_url:
                return None
            return cls(self.searxng_url, self.timeout)
        if name == "baidu":
            return cls(resolve_links=self.resolve_links, timeout=self.timeout)
        return cls(self.timeout)

    def _order(self):
        if self.backend == "auto":
            return list(AUTO_ORDER)
        return [self.backend]

    # ------------------------------------------------------------ 检索
    def search(self, query: str, max_results: int = None):
        """执行检索，返回 SearchResult 列表（自动在多个后端间回退）"""
        query = (query or "").strip()
        if not query:
            return []
        limit = max_results or self.max_results
        errors = []

        for name in self._order():
            backend = self._build(name)
            if backend is None:
                continue
            try:
                results = backend.search(query, limit)
                if results:
                    self.last_backend = name
                    self.last_error = ""
                    return results
                errors.append(f"{name}: 无结果")
            except SearchError as e:
                errors.append(f"{name}: {e}")
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__} {e}")

        self.last_error = "；".join(errors) or "没有可用的检索后端"
        return []

    def fetch(self, url: str, max_chars: int = 6000) -> str:
        """抓取网页并转成纯文本"""
        try:
            target = resolve_url(url, self.timeout) if is_redirect_url(url) else url
            text, _final = http_get(target, timeout=self.timeout)
            return html_to_text(text, max_chars)
        except SearchError:
            return ""

    def search_and_read(self, query: str, top_n: int = 2,
                        max_chars: int = 4000, max_results: int = None) -> dict:
        """
        检索并阅读前几条网页

        会跳过抓取失败的站点，直到拿到 top_n 篇正文。
        返回: {"query":…, "results":[SearchResult…], "pages":[(url, title, text)…],
               "error": str}
        """
        results = self.search(query, max_results)
        if not results:
            return {"query": query, "results": [], "pages": [],
                    "error": self.last_error}

        pages = []
        for item in results:
            if len(pages) >= top_n:
                break
            text = self.fetch(item.url, max_chars)
            if text:
                pages.append((item.url, item.title, text))
        return {"query": query, "results": results, "pages": pages, "error": ""}

    # ------------------------------------------------------------ 格式化
    @staticmethod
    def format_results(results, max_items: int = 8) -> str:
        """把检索结果格式化成给 AI/用户看的文本"""
        lines = []
        for index, item in enumerate(results[:max_items], start=1):
            lines.append(f"[{index}] {item.title}")
            lines.append(f"    链接: {item.url}")
            if item.snippet:
                lines.append(f"    摘要: {item.snippet}")
        return "\n".join(lines)

    @staticmethod
    def build_context(data: dict, max_chars: int = 6000) -> str:
        """把 search_and_read 的结果拼成给模型的上下文"""
        parts = [f"联网检索关键词: {data.get('query', '')}"]
        if data.get("error"):
            parts.append(f"检索失败: {data['error']}")
        if data.get("results"):
            parts.append("\n搜索结果列表:\n" + WebSearchClient.format_results(data["results"]))
        for url, title, text in data.get("pages", []):
            parts.append(f"\n网页正文（{title} - {url}）:\n{text}")
        content = "\n".join(parts)
        return content[:max_chars]

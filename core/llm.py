"""
大模型客户端 - 统一 Anthropic 与 OpenAI 兼容接口（DeepSeek / 本地模型 / 中转站）

设计:
    · 上层（AIAgent）只调用 `LLMClient.chat()`，不关心是哪家
    · Anthropic 用官方 SDK
    · OpenAI 兼容协议（DeepSeek、vLLM、Ollama、各类中转站）只用标准库 urllib 实现，
      不引入 openai 包，保持依赖精简
    · 错误统一翻译成可读中文（密钥无效 / 余额不足 / 频率限制 / 模型名不对…）
"""
import json
import ssl
import urllib.error
import urllib.request

try:
    import anthropic
except ImportError:
    anthropic = None

#: 支持的提供方及其默认配置
PROVIDERS = {
    "anthropic": {
        "label": "Anthropic Claude",
        "base_url": "",
        "model": "claude-sonnet-4-20250514",
        "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514",
                   "claude-3-5-haiku-20241022"],
        "key_hint": "sk-ant-...",
    },
    "deepseek": {
        "label": "DeepSeek（深度求索）",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_hint": "sk-...",
    },
    "openai_compat": {
        "label": "OpenAI 兼容接口（自建/中转/本地）",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "qwen-plus", "glm-4-plus"],
        "key_hint": "按服务商要求填写",
    },
}

DEFAULT_PROVIDER = "anthropic"


class LLMError(Exception):
    """调用大模型失败（消息已翻译成可读中文）"""


def provider_defaults(name: str) -> dict:
    """取某提供方的默认配置"""
    return dict(PROVIDERS.get((name or DEFAULT_PROVIDER).lower(),
                             PROVIDERS[DEFAULT_PROVIDER]))


def provider_label(name: str) -> str:
    return provider_defaults(name).get("label", name)


def chat_url(base_url: str) -> str:
    """
    拼出 OpenAI 兼容协议的 chat completions 地址

    DeepSeek 两种写法都支持（带不带 /v1 都行），这里统一成带 /v1 的形式；
    若用户直接填了完整地址则原样使用。
    """
    base = (base_url or "").strip().rstrip('/')
    if not base:
        raise LLMError("未配置接口地址（base_url）")
    if base.endswith('/chat/completions'):
        return base
    if base.endswith('/v1'):
        return base + '/chat/completions'
    return base + '/v1/chat/completions'


class LLMClient:
    """
    统一的大模型客户端

    参数:
        provider: anthropic / deepseek / openai_compat
        api_key: 密钥
        model: 模型名
        base_url: 接口地址（OpenAI 兼容协议用；Anthropic 忽略）
        timeout: 单次请求超时秒数
    """

    def __init__(self, provider: str = DEFAULT_PROVIDER, api_key: str = "",
                 model: str = "", base_url: str = "", timeout: int = 90):
        self.provider = (provider or DEFAULT_PROVIDER).lower()
        defaults = provider_defaults(self.provider)
        self.api_key = (api_key or "").strip()
        self.model = (model or defaults["model"]).strip()
        self.base_url = (base_url or defaults["base_url"]).strip()
        self.timeout = max(5, int(timeout))
        self._anthropic_client = None

    # ------------------------------------------------------------ 信息
    @property
    def is_anthropic(self) -> bool:
        return self.provider == "anthropic"

    @property
    def configured(self) -> bool:
        if not self.api_key:
            return False
        if not self.is_anthropic and not self.base_url:
            return False
        return True

    def describe(self) -> str:
        """一句话描述（用于界面与日志）"""
        if self.is_anthropic:
            return f"{provider_label(self.provider)} / {self.model}"
        return f"{provider_label(self.provider)} / {self.model} @ {self.base_url}"

    # ------------------------------------------------------------ 调用
    def chat(self, system: str, messages, max_tokens: int = 1024,
             temperature: float = 0.3) -> str:
        """
        发起一次对话补全

        参数:
            system: 系统提示词
            messages: [{"role": "user"/"assistant", "content": "..."}]
            max_tokens / temperature: 生成参数
        返回:
            模型回复的纯文本
        """
        if not self.api_key:
            raise LLMError("未配置 API Key")
        if self.is_anthropic:
            return self._chat_anthropic(system, messages, max_tokens, temperature)
        return self._chat_openai_compat(system, messages, max_tokens, temperature)

    # ------------------------------ Anthropic
    def _chat_anthropic(self, system, messages, max_tokens, temperature) -> str:
        if anthropic is None:
            raise LLMError("未安装 anthropic 库，请执行: pip install anthropic")
        if self._anthropic_client is None:
            try:
                self._anthropic_client = anthropic.Anthropic(
                    api_key=self.api_key, timeout=self.timeout)
            except Exception as e:
                raise LLMError(f"初始化 Anthropic 客户端失败: {e}")

        try:
            response = self._anthropic_client.messages.create(
                model=self.model, system=system or "",
                messages=self._clean_messages(messages),
                max_tokens=max_tokens, temperature=temperature,
            )
        except Exception as e:
            raise LLMError(self._translate_error(e, "Anthropic"))

        parts = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    # ------------------------------ OpenAI 兼容（DeepSeek 等）
    def _chat_openai_compat(self, system, messages, max_tokens, temperature) -> str:
        url = chat_url(self.base_url)
        payload = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + self._clean_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "AITerminal/0.7",
            },
        )
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=context) as response:
                raw = response.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode('utf-8', errors='replace')
            except Exception:
                pass
            raise LLMError(self._translate_http_error(e.code, body))
        except urllib.error.URLError as e:
            raise LLMError(f"网络不可达（{self.base_url}）: {e.reason}")
        except Exception as e:
            raise LLMError(f"请求失败: {type(e).__name__}: {e}")

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise LLMError(f"接口返回不是合法 JSON: {raw[:200]}")

        if isinstance(result, dict) and result.get("error"):
            error = result["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise LLMError(f"接口返回错误: {message}")

        choices = (result or {}).get("choices") or []
        if not choices:
            raise LLMError(f"接口没有返回内容: {raw[:200]}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            # 推理类模型可能把内容放在 reasoning_content 里
            content = message.get("reasoning_content") or ""
        return (content or "").strip()

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _clean_messages(messages):
        """只保留合法字段，避免把内部字段发给接口"""
        cleaned = []
        for item in messages or []:
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, str) and content.strip():
                cleaned.append({"role": role, "content": content})
        if not cleaned:
            cleaned = [{"role": "user", "content": "你好"}]
        return cleaned

    def _translate_http_error(self, code: int, body: str) -> str:
        """把 HTTP 状态码翻译成可操作的中文提示"""
        detail = ""
        try:
            parsed = json.loads(body)
            error = parsed.get("error") or {}
            detail = (error.get("message") if isinstance(error, dict)
                      else str(error)) or parsed.get("message", "")
        except Exception:
            detail = (body or "").strip()[:200]

        hints = {
            400: "请求参数有误（常见：模型名不对）",
            401: "API Key 无效或已过期",
            402: "账户余额不足",
            403: "无权访问该模型或接口",
            404: "接口地址或模型名不存在（检查 base_url 与 model）",
            422: "请求格式不被接受",
            429: "请求过于频繁或已达配额上限，请稍后重试",
            500: "服务端内部错误",
            502: "网关错误（中转服务异常）",
            503: "服务暂不可用（可能正在维护或过载）",
            504: "网关超时",
        }
        base = hints.get(code, f"HTTP {code}")
        # 网关类错误里常包着真实的连接失败原因，识别出来给更直接的提示
        low = (detail or "").lower()
        if any(mark in low for mark in (
                "connection refused", "积极拒绝", "connect failed", "econnrefused",
                "no route to host", "timed out", "超时")):
            base = "无法连接到接口地址（连接被拒绝或超时）"
        text = f"{base}"
        if detail:
            text += f"：{detail}"
        if code in (401, 402, 403) and self.provider == "deepseek":
            text += "（请到 platform.deepseek.com 检查密钥与余额）"
        return text

    @staticmethod
    def _translate_error(exc: Exception, provider: str) -> str:
        """Anthropic SDK 异常翻译"""
        name = type(exc).__name__
        text = str(exc)
        if "Authentication" in name or "401" in text:
            return "API Key 无效或已过期"
        if "RateLimit" in name or "429" in text:
            return "请求过于频繁或已达配额上限，请稍后重试"
        if "NotFound" in name or "404" in text:
            return f"模型名不存在: {text}"
        if "Connection" in name or "Timeout" in name:
            return f"网络连接异常: {text}"
        if "BadRequest" in name or "400" in text:
            return f"请求参数有误: {text}"
        return f"{provider} 调用失败（{name}）: {text}"


def build_client(settings, provider: str = None, api_key: str = None,
                 model: str = None) -> LLMClient:
    """
    按配置构建客户端（供 SessionManager / AIAgent 使用）

    参数留空时从设置里取当前提供方的配置。
    """
    name = (provider or (settings.get("ai_provider") if settings else None)
            or DEFAULT_PROVIDER).lower()
    defaults = provider_defaults(name)
    keys = (settings.get("provider_keys") if settings else None) or {}
    return LLMClient(
        provider=name,
        api_key=api_key if api_key is not None else (keys.get(name) or
                                                     (settings.api_key if settings else "")),
        model=model or (settings.get("model") if settings else "") or defaults["model"],
        base_url=(settings.get("api_base") if settings else "") or defaults["base_url"],
    )

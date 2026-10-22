"""
大模型客户端 - 统一 Anthropic 与 OpenAI 兼容接口（DeepSeek / LongCat / 本地模型 / 中转站）

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
    "longcat": {
        # 美团 LongCat（龙猫）。同一套 key 既走 OpenAI 兼容端点也走 Anthropic 端点，
        # 这里只用 OpenAI 兼容的（见 chat_url：/openai + /v1/chat/completions）。
        # ⚠ base_url 必须带 /openai 这一段，不能只填 https://api.longcat.chat ——
        # 少了它拼出来的是 /v1/chat/completions（那是它的"模型列表"端点，会 404）。
        "label": "LongCat（美团龙猫）",
        "base_url": "https://api.longcat.chat/openai",
        "model": "LongCat-2.0",
        "models": ["LongCat-2.0", "LongCat-2.5-Preview"],
        "key_hint": "ak_...",
    },
    "openai_compat": {
        "label": "OpenAI 兼容接口（自建/中转/本地）",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "qwen-plus", "glm-4-plus"],
        "key_hint": "按服务商要求填写",
    },
    # 本机离线模型（llama.cpp）。与 openai_compat 走**同一条**代码路径
    # （core/llm.py:75-89 拼 /v1/chat/completions、:186-259 发请求），
    # 单独列出来只是为了让用户在设置里一眼看到"不用联网、不用密钥"。
    # ⚠ default_key 必须与 local_llm/run_server.py:36 API_KEY 完全一致：
    # 那边启动时传了 --api-key，这里少一个字符就是 401。
    "local": {
        "label": "本地模型（本机离线 · llama.cpp）",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "local-llm",
        "models": ["local-llm"],
        "key_hint": "本地服务占位密钥，默认 local-llm（与 run_server.py 的 --api-key 一致）",
        "no_key": True,
        "default_key": "local-llm",
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
        provider: anthropic / deepseek / longcat / openai_compat
        api_key: 密钥
        model: 模型名
        base_url: 接口地址（OpenAI 兼容协议用；Anthropic 忽略）
        timeout: 单次请求超时秒数
        extra_body: 额外的请求体字段（仅 OpenAI 兼容协议），原样并进 payload。
            给"某家专有开关"留的口子 —— 例如 LongCat 的
            `{"thinking": {"type": "disabled"}}`。**不要**把这类字段写死在
            payload 里：DeepSeek 收到不认识的字段会直接 400。
    """

    def __init__(self, provider: str = DEFAULT_PROVIDER, api_key: str = "",
                 model: str = "", base_url: str = "", timeout: int = 90,
                 extra_body: dict = None):
        self.provider = (provider or DEFAULT_PROVIDER).lower()
        defaults = provider_defaults(self.provider)
        self.api_key = (api_key or "").strip()
        self.model = (model or defaults["model"]).strip()
        self.base_url = (base_url or defaults["base_url"]).strip()
        # 本地模型不要求用户填密钥，但全链路（configured / chat）都要求非空，
        # 否则界面会拦在"请先填写 API Key"、用户以为本地也要买 key。
        # 这里补占位值，且必须与 run_server.py 的 --api-key 一致。
        self.no_key = bool(defaults.get("no_key"))
        if self.no_key and not self.api_key:
            self.api_key = (defaults.get("default_key") or "local-llm").strip()
        self.timeout = max(5, int(timeout))
        self.extra_body = dict(extra_body or {})
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
        # 提供方专有开关（LongCat 的 thinking 等）。空字典时等于没写这一行。
        if self.extra_body:
            payload.update(self.extra_body)
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "NetOpsTerminal/0.7",
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
        finish = choices[0].get("finish_reason") or ""
        content = message.get("content")
        if not content:
            # 推理模型（deepseek-reasoner / LongCat-2.x）把"思考"放 reasoning_content，
            # 正式答复放 content。content 为空**基本只有一个原因**：max_tokens 被思考吃光。
            # 此时若把 reasoning_content 当答案返回，用户看到的是模型的**自言自语**
            # （实测：问"回：聚合"，返回的是"用户只要求回复两个字…我应该直接回复"）。
            # 所以截断时**报错**，宁可失败也不要冒充答案。
            reasoning = message.get("reasoning_content") or ""
            if finish == "length":
                raise LLMError(
                    "输出被 max_tokens 截断：推理模型的思考占满了预算，正文为空。"
                    "请调大 max_tokens，或对支持的服务商关闭思考"
                    "（LongCat 可传 thinking={\"type\":\"disabled\"}）")
            content = reasoning
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
        low = (detail or "").lower()
        # 各家把"没钱了"放在不同状态码上：DeepSeek 用 402，LongCat 用 403 且
        # code=insufficient_quota。只看状态码会把 LongCat 的"额度不足"说成
        # "无权访问该模型"，用户会跑去查权限而不是去充值 —— 所以按 body 纠正。
        if code in (402, 403) and ("insufficient_quota" in low or "quota" in low
                                   or "balance" in low or "额度" in detail):
            base = "账户额度不足"
        # 网关类错误里常包着真实的连接失败原因，识别出来给更直接的提示
        elif any(mark in low for mark in (
                "connection refused", "积极拒绝", "connect failed", "econnrefused",
                "no route to host", "timed out", "超时")):
            base = "无法连接到接口地址（连接被拒绝或超时）"
        text = f"{base}"
        if detail:
            text += f"：{detail}"
        if code in (401, 402, 403) and self.provider == "deepseek":
            text += "（请到 platform.deepseek.com 检查密钥与余额）"
        elif code in (401, 402, 403) and self.provider == "longcat":
            text += "（请到 longcat.chat 开放平台检查密钥与额度）"
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
    按配置构建客户端（供 SessionManager / AIAgent / 离线工具使用）

    参数留空时从设置里取当前提供方的配置。

    ⚠ `provider` 与设置里的 `ai_provider` **不一致**时，模型名与接口地址必须取
    `provider` 自己那份配置（`provider_configs[name]`），**不能**回落到
    `settings.model` / `settings.api_base` —— 那两个字段只描述"当前提供方"。
    踩过的坑：显式 `--provider longcat` 却拿着 deepseek 的 model 与 api_base 去
    请求，得到 404，看上去像"LongCat 没有这个模型"，其实是地址/模型张冠李戴。
    """
    name = (provider or (settings.get("ai_provider") if settings else None)
            or DEFAULT_PROVIDER).lower()
    defaults = provider_defaults(name)

    cfg = {}
    same = True
    if settings is not None:
        try:
            cfg = settings.get_provider_config(name) or {}
            same = (name == (settings.get("ai_provider") or "").lower())
        except Exception:
            cfg, same = {}, True

    if api_key is None:
        api_key = ""
        if settings is not None:
            try:
                api_key = settings.get_api_key_for(name) or ""
            except Exception:
                api_key = ""
            # 兼容旧配置：只有顶层 api_key 字段（按"当前提供方"解读）
            if not api_key and same:
                api_key = settings.get("api_key", "")

    if not model:
        model = (cfg.get("model")
                 or ((settings.get("model") if (same and settings) else "") or "")
                 or defaults["model"])
    base_url = (cfg.get("base_url")
                or ((settings.get("api_base") if (same and settings) else "") or "")
                or defaults["base_url"])
    return LLMClient(provider=name, api_key=api_key, model=model,
                     base_url=base_url)

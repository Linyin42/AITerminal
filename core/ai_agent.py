"""
AI智能体 - 分析设备输出并生成命令
"""
import os
import re
from typing import List, Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .llm import LLMClient, LLMError, provider_defaults, PROVIDERS
from .knowledge import EXPERIENCE_CATEGORY

try:
    import anthropic  # noqa: F401  （保留：旧配置的兼容判断）
except ImportError:  # 允许未安装依赖时导入模块，运行时再报错
    anthropic = None


# 危险命令黑名单：默认拒绝自动执行，需人工确认
DANGEROUS_PATTERNS = [
    r'\b(delete|erase|format)\b',          # 删除文件/配置
    r'\bundo\s+all\b',
    r'\breset\s+saved-configuration\b',
    r'\breboot\b',
    # 2026-10-19 真机测试补：锐捷（RGOS）/ Cisco 的重启命令是 reload，不是 reboot，
    # 只列 reboot 会让"reload"和"reload in 5"直接放行。
    r'\breload\b',
    r'\bshutdown\b',
    r'\bwrite\s+erase\b',
    r'\bfactory-reset\b',
    r'\brestore\s+factory\b',
    r'\b(no)\s+interface\b',
    r'\bundo\s+interface\b',
    # 2026-10-20 AI 联调补：删除"整段对象"同样是破坏性操作。
    # 起因：真机探针喂 `no vlan 100` 判为安全 —— AI 生成这一条就能把整个 VLAN
    # 连同其下所有端口配置一起抹掉。只拦删对象（VLAN / ACL / 路由 / 路由进程），
    # 不拦日常改配置：`no shutdown` 已被 _SAFE_SHUTDOWN_RE 摘掉，
    # `no ip address` / `no description` 这类仍然放行（探针里有对应用例）。
    r'\b(no|undo)\s+(vlan|acl|port-group)\b',
    r'\b(no|undo)\s+ip\s+route\b',
    r'\b(no|undo)\s+router\s+\w+',
    r'\bclear\s+configuration\b',
]
_DANGEROUS_RE = re.compile('|'.join(DANGEROUS_PATTERNS), re.IGNORECASE)

#: 取反写法是"恢复接口"，不是关接口：华为/H3C 写 undo shutdown，锐捷/Cisco 写 no shutdown。
#: 2026-10-19 真机测试补：这两个以前被 \bshutdown\b 误判成危险命令，
#: 导致 AI 建议"恢复某接口"时被安全策略挡住。
_SAFE_SHUTDOWN_RE = re.compile(r'\b(?:undo|no)\s+shutdown\b', re.IGNORECASE)


# 内置系统提示词（可在界面"工具 → 编辑系统提示词"里覆盖）
BUILTIN_SYSTEM_PROMPT = """你是专业的网络工程师AI助手，擅长分析网络设备输出并生成配置命令。

核心能力:
1. 分析设备终端输出（display/show命令结果）
2. 根据当前状态生成下一步命令
3. 识别设备厂商（华为/华三/H3C/Cisco等）并使用正确语法
4. 执行多步骤网络配置和排查任务

行为准则:
1. 只输出命令本身，不要解释或额外文字
2. 如果当前输出已满足任务要求，只输出: DONE
3. 如果需要更多信息，生成查询命令
4. 如果需要配置修改，生成配置命令
5. 安全第一，不生成删除/重启/关闭端口命令除非明确要求
6. 考虑命令的依赖关系，按正确顺序执行
7. 进入配置视图（system-view/configure terminal）后记得退出（return/end）

支持的厂商语法:
- 华为/H3C: display, system-view, interface, quit
- Cisco: show, configure terminal, interface, exit
- 通用: ping, traceroute, telnet, ssh

输出格式:
- 普通命令: 直接输出命令，如: display interface brief
- 多步命令: 每行一条命令
- 任务完成: 输出 DONE
- 无法判断: 输出 ASK（需要用户输入）
"""


# 对话式交互的系统提示词（ChatView 用，允许"说明 + 命令计划"混合输出）
CONVERSATION_SYSTEM_PROMPT = """你是运行在网络终端里的 AI 运维助手（对标 NetCatty 的 Catty Agent）。
用户用自然语言描述需求，你负责：
1. 回答用户的疑问（简洁中文）
2. 给出要在设备上执行的命令计划

输出格式要求：
- 先用一段简短说明（中文，不超过 3 句话），解释你要做什么 / 为什么这么做
- 然后给出命令计划，用代码块包裹，bash 语言标记：
  ```bash
  display version
  display vlan
  ```
- 只读命令（display / show / ping / traceroute 等）正常给出
- 配置类命令（vlan / interface / ip address / system-view 等）也正常给出，系统会要求用户确认后才下发
- 如果只需要回答、不需要命令，就只写说明，不要出现代码块
- 不要使用 DONE / ASK 这类控制词
- 始终结合用户当前设备的厂商（华为/H3C/Cisco 等）和已有输出来给命令，语法要对
- 输出要可以直接被解析：说明是普通文字，命令必须放在 ```bash 代码块里
"""

# 追加到系统提示词后的资料检索协议（配置了本地资料库或联网检索时启用）
SEARCH_INSTRUCTION = """
资料检索能力:
你可以检索两类资料来确认不确定的外部知识:
1. 本地资料库（用户导入的命令手册、配置案例、培训文档、接口对照表等）—— 优先
2. 联网检索（厂商官网、技术博客、社区问答）
当你需要确认厂商命令语法、设备型号特性、版本差异、报错含义等不确定的外部知识时，
不要凭记忆猜测，直接输出一行:
SEARCH: <检索关键词>
系统会先查本地资料库，必要时再联网，然后把查到的资料回填给你，你再据此生成命令。
约束:
1. 每轮最多检索 2 次，能靠设备输出判断的不要检索
2. 检索关键词要具体，包含厂商和型号，例如: H3C S5560 配置端口镜像 命令
3. 拿到资料后仍然只输出命令本身（或 DONE / ASK），不要解释
4. 优先采用本地资料库里的命令，它与用户实际设备型号更匹配
"""


# 经验总结提示词：把一轮交互压成一条"成功点 / 踩坑点"记录，供以后快速匹配
EXPERIENCE_SYSTEM_PROMPT = """你在帮网络工程师归档"运维经验"，输入是刚刚发生的一轮交互。

请输出一段简短的中文 Markdown（不超过 200 字），严格按下面的小标题：
- **需求**：用户想要什么（一句话）
- **有效做法**：这次哪些命令/思路是对的、确实解决了问题
- **踩坑**：哪些命令报错、输出异常、走了弯路（没有就写"无"）
- **下次可复用**：以后遇到同类需求，直接可用的命令或顺序

要求：
1. 只写有信息量的内容，不要复述原文，不要客套话
2. 命令保持原样放在反引号里，方便以后检索
3. 如果这一轮没有任何值得归档的信息（例如只是闲聊、或没有任何设备操作），
   只输出一行: SKIP
"""


# 整理导入的产品文档：出一份技术解释 + 一份命令清单
DOCUMENT_DIGEST_SYSTEM_PROMPT = """你在整理网络设备厂商文档，成果供运维人员查阅、并用于命令自动补全。

输入可能包含两部分：从文档里粗筛出来的命令行候选、以及文档正文摘录。

请严格按下面两个小标题输出：
## 技术解释
用中文讲清这份文档涉及的关键概念、工作机制、适用条件与注意事项，300 字以内。
如果文档主要是命令参考，就说明这些命令按用途怎么分类。

## 命令
用一个 ``` 代码块列出**文档里确实出现**、且可以直接在设备上敲的命令：
1. 每行一条，只写命令本身；去掉参数占位符（如 <ip-address>、interface-name）和示例参数
2. 不要凭记忆补充文档里没有的命令
3. 去掉重复项，最多 80 条
"""


# 匹配 AI 提出的检索请求
_SEARCH_RE = re.compile(r'^\s*SEARCH\s*[:：]\s*(.+?)\s*$', re.IGNORECASE | re.MULTILINE)


class AIAgent(QObject):
    """
    AI智能体

    功能:
        分析设备终端输出，生成下一步执行命令
        支持上下文记忆，可执行多步任务
        支持任务模式（自动执行）和辅助模式（建议命令）
        支持联网检索（查厂商命令、报错含义、版本特性）
    """

    # 信号
    command_generated = pyqtSignal(str)      # 生成命令时发射
    analysis_ready = pyqtSignal(str)         # 分析完成时发射
    task_completed = pyqtSignal()            # 任务完成时发射
    error_occurred = pyqtSignal(str)         # 发生错误时发射
    research_ready = pyqtSignal(str, list)   # 联网检索完成: (回答, 来源列表)

    DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    def __init__(self, api_key: str = "", model: str = None,
                 system_prompt: str = "", search_client=None,
                 knowledge_base=None, provider: str = "anthropic",
                 base_url: str = ""):
        """
        初始化AI智能体

        参数:
            api_key: 大模型 API 密钥
            model: 模型名称，留空用该提供方的默认模型
            system_prompt: 自定义系统提示词，留空则使用内置提示词
            search_client: WebSearchClient 实例，传入后 AI 具备联网检索能力
            knowledge_base: KnowledgeBase 实例，传入后 AI 可检索本地资料库
            provider: anthropic / deepseek / openai_compat
            base_url: 接口地址（OpenAI 兼容协议用）
        """
        super().__init__()
        self.provider = (provider or "anthropic").lower()
        defaults = provider_defaults(self.provider)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or defaults["model"]
        self.base_url = base_url or defaults["base_url"]
        self.custom_system_prompt = (system_prompt or "").strip()
        self.search_client = search_client
        self.knowledge_base = knowledge_base
        # 统一的大模型客户端：Anthropic 与 OpenAI 兼容协议（DeepSeek 等）都走这里
        self.llm = LLMClient(provider=self.provider, api_key=self.api_key,
                             model=self.model, base_url=self.base_url)
        self.context: List[Dict] = []
        self.system_prompt = self._get_system_prompt()
        self.task_mode = False
        self.current_task = ""
        self.vendor = "unknown"
        self.search_count = 0  # 本次会话已发起的检索次数
        self.search_read_pages = 2
        self.search_page_chars = 4000
        self.knowledge_top_k = 4
        self.knowledge_max_chars = 4000
        self.last_local_hits: List[dict] = []
        self.last_lookup_sources: List[dict] = []
        self.last_experience_hits: List[dict] = []   # 本轮参考到的历史经验

    # ------------------------------------------------------------ 客户端
    def set_provider(self, provider: str, api_key: str = None,
                     model: str = None, base_url: str = None):
        """切换大模型提供方（Anthropic / DeepSeek / OpenAI 兼容）"""
        self.provider = (provider or self.provider).lower()
        defaults = provider_defaults(self.provider)
        if api_key is not None:
            self.api_key = api_key
        # 没显式指定模型时用新提供方的默认模型
        # （旧提供方的模型名在新提供方上通常无效，不能沿用）
        self.model = (model or "").strip() or defaults["model"]
        if base_url is not None:
            self.base_url = base_url or defaults["base_url"]
        self.llm = LLMClient(provider=self.provider, api_key=self.api_key,
                             model=self.model, base_url=self.base_url)

    def set_api_key(self, api_key: str):
        """更新API Key并重建客户端"""
        self.api_key = api_key
        self.llm = LLMClient(provider=self.provider, api_key=api_key,
                             model=self.model, base_url=self.base_url)

    def set_model(self, model: str):
        """切换模型"""
        self.model = model
        self.llm = LLMClient(provider=self.provider, api_key=self.api_key,
                             model=model, base_url=self.base_url)

    def describe_model(self) -> str:
        """当前使用的模型描述（用于界面提示）"""
        return self.llm.describe()

    def set_vendor(self, vendor: str):
        """设置设备厂商，让 AI 使用正确的命令语法"""
        self.vendor = vendor or "unknown"

    # ------------------------------------------------------------ 对话式交互
    def converse(self, user_message: str, device_output: str = "",
                 task: str = "", max_tokens: int = 900,
                 experience: str = ""):
        """
        对话式一轮：用户自然语言 → AI 说明 + 命令计划

        参数:
            user_message:   用户这条消息（自然语言）
            device_output:  当前设备终端输出（作为背景上下文）
            task:          当前任务描述（可选）
            experience:     命中的历史经验（可选，只用于本轮，不写进对话历史）
        返回:
            (text, commands)
            text:      AI 的口头说明（已去掉代码块标记）
            commands:  从回复里解析出的命令列表
        """
        content = user_message.strip()
        if device_output and device_output.strip():
            content += f"\n\n当前设备输出（背景）：\n{device_output[-4000:]}"
        if task:
            content += f"\n\n当前任务：{task}"
        elif self.current_task:
            content += f"\n\n当前任务：{self.current_task}"

        self.context.append({"role": "user", "content": content})
        messages = self.context[-20:]
        if experience:
            # 历史经验只在这一轮发给模型、不写进上下文：否则后面每轮都会
            # 把它们重新带一遍，白烧 token
            messages = messages[:-1] + [{
                "role": "user",
                "content": content
                + "\n\n以下是在这台设备上积累的历史经验（成功做法 / 踩过的坑），"
                  "请优先沿用有效的做法、避开踩坑点：\n" + experience,
            }]

        try:
            reply = self.llm.chat(
                system=CONVERSATION_SYSTEM_PROMPT,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.4,
            ).strip()
        except LLMError as e:
            self.error_occurred.emit(f"AI对话错误: {e}")
            return "", []
        except Exception as e:
            self.error_occurred.emit(f"AI对话错误: {self._err_text(e)}")
            return "", []

        self.context.append({"role": "assistant", "content": reply})
        return self._split_reply(reply)

    @staticmethod
    def _split_reply(reply: str):
        """把 AI 回复拆成 (口头说明, 命令列表)。

        规则：```bash / ```text 围栏里的行当作命令（去掉编号/项目符号），
        其余文字当作说明。
        """
        text_parts = []
        commands = []
        in_block = False
        for raw in reply.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_block:
                    in_block = False
                else:
                    in_block = True
                    # 这行是开头围栏，后面是命令
                continue
            if in_block:
                cmd = stripped
                if not cmd:
                    continue
                # 去编号 / 项目符号
                cmd = re.sub(r'^(\d+[.、)]|[-*•])\s*', '', cmd).strip()
                cmd = cmd.strip('`').strip()
                if cmd:
                    commands.append(cmd)
            else:
                if stripped:
                    text_parts.append(stripped)
        text = "\n".join(text_parts).strip()
        return text, commands

    def set_system_prompt(self, text: str):
        """设置自定义系统提示词（留空恢复内置）"""
        self.custom_system_prompt = (text or "").strip()
        self.system_prompt = self._get_system_prompt()

    # ------------------------------------------------------------ 提示词
    @staticmethod
    def default_system_prompt() -> str:
        """返回内置系统提示词（供界面"恢复默认"使用）"""
        return BUILTIN_SYSTEM_PROMPT

    def _get_system_prompt(self) -> str:
        """获取生效的系统提示词（有检索能力时追加检索协议）"""
        base = self.custom_system_prompt or BUILTIN_SYSTEM_PROMPT
        if self.lookup_enabled:
            base = base.rstrip() + "\n" + SEARCH_INSTRUCTION
        return base

    def set_search_client(self, client):
        """注入/替换联网检索客户端"""
        self.search_client = client
        self.system_prompt = self._get_system_prompt()

    def set_knowledge_base(self, knowledge_base):
        """注入/替换本地资料库"""
        self.knowledge_base = knowledge_base
        self.system_prompt = self._get_system_prompt()

    def configure_knowledge(self, top_k: int = 4, max_chars: int = 4000):
        """配置本地资料库检索参数"""
        self.knowledge_top_k = max(1, int(top_k))
        self.knowledge_max_chars = max(500, int(max_chars))

    @property
    def search_enabled(self) -> bool:
        """是否具备联网检索能力"""
        return self.search_client is not None

    @property
    def knowledge_enabled(self) -> bool:
        """是否具备本地资料检索能力"""
        return self.knowledge_base is not None

    @property
    def lookup_enabled(self) -> bool:
        """是否具备任意一种资料检索能力（本地或联网）"""
        return self.search_enabled or self.knowledge_enabled

    # ------------------------------------------------------------ 主流程
    def analyze(self, output: str, task: str = "") -> str:
        """
        分析设备输出，生成下一步命令

        参数:
            output: 设备终端输出内容
            task: 当前任务描述
        返回:
            生成的命令字符串，或"DONE"/"ASK"
        """
        # 构建用户消息
        content = f"设备输出:\n{output}"
        if self.vendor and self.vendor != "unknown":
            content += f"\n\n设备厂商: {self.vendor}"
        if task:
            content += f"\n\n当前任务: {task}"
        elif self.current_task:
            content += f"\n\n当前任务: {self.current_task}"

        # 添加到上下文
        self.context.append({"role": "user", "content": content})

        # 限制上下文长度（保留最近10轮）
        messages = self.context[-20:]

        try:
            reply = self.llm.chat(
                system=self.system_prompt,
                messages=messages,
                max_tokens=512,
                temperature=0.3
            ).strip()

            # 更新上下文
            self.context.append({"role": "assistant", "content": reply})

            # 处理特殊回复
            if reply.upper() == "DONE":
                self.task_completed.emit()
                return ""
            elif reply.upper() == "ASK":
                return ""

            # 发射信号
            self.command_generated.emit(reply)
            return reply

        except LLMError as e:
            self.error_occurred.emit(f"AI分析错误: {e}")
            return ""
        except Exception as e:
            self.error_occurred.emit(f"AI分析错误: {self._err_text(e)}")
            return ""

    def analyze_config(self, config_text: str, requirement: str) -> str:
        """
        分析设备配置，给出修改建议

        参数:
            config_text: 设备当前配置
            requirement: 修改需求
        返回:
            配置建议文本
        """
        prompt = f"""分析以下设备配置，针对需求给出具体的修改建议。

设备配置:
{config_text}

修改需求:
{requirement}

请给出:
1. 需要修改的具体命令
2. 命令执行顺序
3. 注意事项"""

        try:
            result = self.llm.chat(
                system="你是网络配置专家。分析设备配置并给出修改建议。",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.3
            )
            self.analysis_ready.emit(result)
            return result

        except LLMError as e:
            self.error_occurred.emit(f"配置分析错误: {e}")
            return ""
        except Exception as e:
            self.error_occurred.emit(f"配置分析错误: {self._err_text(e)}")
            return ""

    def explain(self, output: str, question: str = "") -> str:
        """
        用自然语言解释设备输出（AI面板"解释"功能）

        参数:
            output: 设备输出
            question: 追加提问，可为空
        """
        prompt = f"请用简洁的中文解释以下网络设备输出的含义和关键信息：\n\n{output}"
        if question:
            prompt += f"\n\n补充问题: {question}"

        try:
            result = self.llm.chat(
                system="你是网络运维专家，用简洁准确的中文解释设备输出。",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.3
            )
            self.analysis_ready.emit(result)
            return result
        except LLMError as e:
            self.error_occurred.emit(f"AI解释错误: {e}")
            return ""
        except Exception as e:
            self.error_occurred.emit(f"AI解释错误: {self._err_text(e)}")
            return ""

    # ------------------------------------------------------------ 联网检索
    @staticmethod
    def parse_search_request(reply: str) -> Optional[str]:
        """
        解析 AI 提出的检索请求

        AI 需要外部资料时会输出 `SEARCH: 关键词`，这里把它取出来。
        """
        if not reply:
            return None
        match = _SEARCH_RE.search(reply)
        if not match:
            return None
        query = match.group(1).strip().strip('`"\'').strip()
        return query or None

    def configure_search(self, read_pages: int = 2, page_chars: int = 4000,
                         max_results: int = None):
        """配置检索行为（由主程序从设置里读取后注入）"""
        self.search_read_pages = max(0, int(read_pages))
        self.search_page_chars = max(500, int(page_chars))
        if max_results and self.search_client is not None:
            try:
                self.search_client.max_results = int(max_results)
            except Exception:
                pass

    def web_context(self, query: str) -> str:
        """
        执行一次联网检索并返回可回填给模型的上下文文本

        检索失败时返回说明文字，让模型知道"查不到"而不是凭空编造。
        """
        if self.search_client is None:
            return "联网检索不可用（未配置检索客户端），请基于已有信息回答或输出 ASK。"

        self.search_count += 1
        data = self.search_client.search_and_read(
            query,
            top_n=getattr(self, 'search_read_pages', 2),
            max_chars=getattr(self, 'search_page_chars', 4000),
        )
        if not data.get("results"):
            return (f"联网检索「{query}」没有返回结果"
                    f"（{data.get('error') or '未知原因'}）。"
                    "请基于已知信息回答，或输出 ASK 请用户补充。")
        return self.search_client.build_context(data, max_chars=6000)

    def local_context(self, query: str) -> str:
        """
        在本地资料库中检索，返回可回填给模型的上下文文本

        本地资料优先于联网：用户导入的手册与自己设备的型号最匹配。
        """
        self.last_local_hits = []
        if self.knowledge_base is None:
            return ""
        if not getattr(self.knowledge_base, "enabled", True):
            return ""
        try:
            hits = self.knowledge_base.search(query, self.knowledge_top_k)
        except Exception as e:
            return f"本地资料库检索失败: {e}"
        if not hits:
            return ""

        self.last_local_hits = hits
        parts = []
        for index, hit in enumerate(hits, start=1):
            parts.append(
                f"[本地资料{index}] {hit['source']}（第 {hit['chunk_index'] + 1} 段）\n"
                f"{hit['text']}"
            )
        return "\n\n".join(parts)[:self.knowledge_max_chars]

    # ------------------------------------------------------------ 历史经验
    def experience_context(self, query: str, top_k: int = 3) -> str:
        """
        检索以前归档的「经验总结」（哪些做法有效、踩过哪些坑）

        命中时返回可回填给模型的文本，同时把命中列表记到
        `last_experience_hits`，供界面提示"已参考 N 条历史经验"。
        纯本地检索，不调用大模型，所以很快。
        """
        self.last_experience_hits = []
        kb = self.knowledge_base
        if kb is None or not getattr(kb, "enabled", True):
            return ""
        try:
            hits = kb.search_category(query, EXPERIENCE_CATEGORY, top_k)
        except Exception:
            return ""
        if not hits:
            return ""

        self.last_experience_hits = hits
        parts = []
        for index, hit in enumerate(hits, start=1):
            parts.append(f"[历史经验{index}] {hit['source']}\n{hit['text']}")
        return "\n\n".join(parts)[:self.knowledge_max_chars]

    def summarize_experience(self, user_message: str, reply_text: str = "",
                             commands=None, device_output: str = "",
                             device: str = "", vendor: str = "") -> str:
        """
        把一轮交互整理成一条「经验总结」（有效做法 / 踩坑点）

        返回 Markdown 正文；没什么可归档或调用失败时返回空字符串，
        调用方静默跳过即可（属于后台整理，不该打扰用户）。
        """
        parts = [f"用户需求: {(user_message or '').strip()}"]
        if device:
            parts.append(f"设备: {device}")
        if vendor and vendor != "unknown":
            parts.append(f"厂商: {vendor}")
        if (reply_text or "").strip():
            parts.append(f"AI 给出的建议:\n{reply_text.strip()}")
        if commands:
            parts.append("AI 生成的命令:\n" + "\n".join(str(c) for c in commands))
        if (device_output or "").strip():
            parts.append(f"设备当时的输出（含之前命令的结果）:\n{device_output[-3000:]}")

        try:
            reply = self.llm.chat(
                system=EXPERIENCE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": "\n\n".join(parts)}],
                max_tokens=500,
                temperature=0.2,
            ).strip()
        except Exception:
            return ""
        # 模型判断没什么值得归档时会输出 SKIP
        if not reply or reply.upper().startswith("SKIP"):
            return ""
        return reply

    @staticmethod
    def parse_command_lines(reply: str, limit: int = 20) -> List[str]:
        """
        从模型回复里挑出命令行

        模型有时仍会带编号、项目符号或代码块围栏，这里一并清掉；
        含中文的行（说明文字）直接丢弃。
        """
        commands: List[str] = []
        for raw in (reply or "").splitlines():
            stripped = raw.strip()
            # 先判围栏再剥反引号：否则 "```text" 会被剥成 "text" 当成命令
            if not stripped or stripped.startswith("```"):
                continue
            line = stripped.strip('`').strip()
            line = re.sub(r'^(\d+[.、)]|[-*•>])\s*', '', line).strip()
            if not line or len(line) > 80:
                continue
            if any('\u4e00' <= ch <= '\u9fff' for ch in line):
                continue
            if line not in commands:
                commands.append(line)
            if len(commands) >= limit:
                break
        return commands

    # ------------------------------------------------------------ 整理文档
    def digest_document(self, name: str, text: str, candidates=None,
                        doc_chars: int = 4000, limit: int = 80):
        """
        把一份导入的产品文档整理成（技术解释, 命令清单）

        参数:
            name:       文档名（给模型当上下文）
            text:       文档正文（只取开头 doc_chars 字符，控制额度）
            candidates: 从正文粗筛出的命令行候选（find_command_candidates 的结果）
        返回:
            (explanation, commands)，整理失败时返回 ("", [])
        """
        parts = [f"文档名: {name}"]
        if candidates:
            parts.append("从文档里粗筛出的命令行候选（需要你甄别）:\n"
                         + "\n".join(list(candidates)[:300]))
        if text and text.strip():
            parts.append("文档正文摘录:\n" + text[:doc_chars])

        try:
            reply = self.llm.chat(
                system=DOCUMENT_DIGEST_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": "\n\n".join(parts)}],
                max_tokens=1200,
                temperature=0.2,
            ).strip()
        except Exception:
            return "", []
        return self.split_digest(reply, limit)

    @staticmethod
    def split_digest(reply: str, limit: int = 80):
        """把整理结果拆成（技术解释, 命令列表）"""
        text = reply or ""
        match = re.search(r'##\s*技术解释\s*(.*?)(?=##\s*命令|```|$)', text, re.S)
        explanation = (match.group(1) if match else "").strip()
        return explanation, AIAgent.parse_command_lines(text, limit)

    def lookup_context(self, query: str, max_chars: int = 6000) -> str:
        """
        资料检索：先本地资料库，再联网检索，返回合并后的上下文

        同时把命中来源记录到 self.last_lookup_sources，供界面展示参考来源。
        """
        query = (query or "").strip()
        self.last_lookup_sources = []
        parts = []

        local = self.local_context(query)
        if local:
            parts.append("【本地资料库】\n" + local)
            for hit in self.last_local_hits:
                self.last_lookup_sources.append({
                    "title": f"{hit['source']}（第 {hit['chunk_index'] + 1} 段）",
                    "url": "",
                    "snippet": " ".join(hit["text"].split())[:160],
                    "source_type": "local",
                    "path": hit.get("path", ""),
                })

        if self.search_client is not None:
            data = self.search_client.search_and_read(
                query,
                top_n=max(1, self.search_read_pages),
                max_chars=self.search_page_chars,
            )
            self.search_count += 1
            if data.get("results"):
                parts.append("【联网检索】\n" + self.search_client.build_context(
                    data, max_chars=max_chars))
                for item in data["results"]:
                    entry = item.to_dict()
                    entry["source_type"] = "web"
                    self.last_lookup_sources.append(entry)
            elif not local:
                parts.append(f"【联网检索】没有返回结果"
                             f"（{data.get('error') or '未知原因'}）")
        elif not local:
            parts.append("本地资料库没有匹配内容，且联网检索未启用。")

        return "\n\n".join(parts)[:max_chars]

    def research(self, question: str, device_context: str = "") -> str:
        """
        检索资料并回答问题（供界面的"资料检索 / 联网查询"使用）

        参数:
            question: 用户的问题，如"如何在 H3C 上配置端口镜像"
            device_context: 设备当前输出，作为背景信息
        返回:
            带引用来源的中文回答
        """
        question = (question or "").strip()
        if not question:
            return ""
        if not self.lookup_enabled:
            msg = "未启用资料检索：本地资料库与联网检索都关闭了。"
            self.error_occurred.emit(msg)
            return ""

        # 1) 先本地资料库，再联网
        context = self.lookup_context(question, max_chars=8000)
        if not context.strip():
            msg = "本地资料库与联网检索都没有查到相关内容"
            self.error_occurred.emit(msg)
            return ""

        # 2) 让模型基于资料作答
        prompt = f"""你是资深网络工程师。请依据下面的资料回答用户问题。

要求:
1. 只依据资料内容作答，资料不足以判断时明确说明"资料中没有提到"，不要编造命令
2. 给出可直接在设备上执行的命令，并标注适用厂商/版本
3. 关键结论后用 [序号] 标注引用的资料编号
4. 本地资料库的内容优先级高于联网资料（更贴合用户实际设备）
5. 用简洁中文，先给结论，再给命令和注意事项
6. 不要输出 DONE / ASK 这类控制词

用户问题: {question}
{("设备当前输出（背景信息）:" + chr(10) + device_context[:2000]) if device_context else ""}

可用资料:
{context}"""

        try:
            answer = self.llm.chat(
                system="你是网络运维专家，回答必须严格基于给定的资料。",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1600,
                temperature=0.2
            ).strip()
        except LLMError as e:
            self.error_occurred.emit(f"资料检索失败: {e}")
            return ""
        except Exception as e:
            self.error_occurred.emit(f"资料检索失败: {self._err_text(e)}")
            return ""

        self.research_ready.emit(answer, list(self.last_lookup_sources))
        return answer

    # ------------------------------------------------------------ 工具方法
    @staticmethod
    def is_dangerous(command: str) -> bool:
        """判断命令是否命中危险操作黑名单

        `undo shutdown` / `no shutdown` 是恢复接口（安全），先把它们从待判文本里摘掉，
        免得被 shutdown 规则误伤；同一条串里其它危险命令仍然照常命中
        （如 "undo shutdown\\nreboot" 里的 reboot）。
        """
        text = _SAFE_SHUTDOWN_RE.sub(" ", command or "")
        return bool(_DANGEROUS_RE.search(text))

    def filter_commands(self, reply: str, block_dangerous: bool = True):
        """
        把 AI 回复拆成可执行命令列表

        返回: (commands, blocked)
            commands: 可安全执行的命令列表
            blocked:  被安全策略拦截的命令列表
        """
        commands, blocked = [], []
        for line in (reply or "").splitlines():
            cmd = line.strip()
            if not cmd or cmd.startswith('#'):
                continue
            # 去掉 AI 可能加上的编号/项目符号/代码块标记
            cmd = re.sub(r'^(\d+[.、)]|[-*•])\s*', '', cmd)
            cmd = cmd.strip('`').strip()
            if not cmd or cmd.upper() in ("DONE", "ASK"):
                continue
            # 只收"看着像命令"的行：必须以 **ASCII 字母/数字**开头。
            # 模型回复里常夹说明文字（"可以按下面三步配置："、"⚠ 风险：…"），不滤的话
            # 它们会被 `is_config_command` 判成配置类命令、一路打到终端，用户得对着一条
            # 中文"命令"去核对下发（实测：这两行原样出现在 commands 里，且都判 True）。
            # ⚠ 不用"含中文就丢"：命令里合法地会有中文（如 `description 销售部`），
            # 那种行都以 ASCII 命令字开头。也**不要**顺手加长度上限 —— 本接口要允许
            # 长命令（ACL 规则、描述超过 80 字符很常见；`parse_command_lines` 才有 80 上限）。
            if not (cmd[:1].isascii() and cmd[:1].isalnum()):
                continue
            if block_dangerous and self.is_dangerous(cmd):
                blocked.append(cmd)
            else:
                commands.append(cmd)
        return commands, blocked

    def review_reply(self, reply: str, block_dangerous: bool = True, limit: int = 20):
        """模型回复 → **结构化**命令审查（逐条风险标注 + 是否必须确认）。

        与 `filter_commands()` 的关系：老接口只做"危险黑名单二分"，返回二元组，且
        **不会**滤掉中文说明行（模型回复里的解释文字会被当成命令）；这里改用
        `parse_command_lines()` 取命令行（它本来就负责丢中文、剥编号/围栏），再套上
        危险黑名单，最后交给 `core.safety.review()` 逐条分级。

        返回: core.safety.review() 的 dict，外加
            blocked: [被危险黑名单拦下的命令]
            summary: 会追加"已拦截 N 条危险命令"
        """
        from core import safety                      # noqa: WPS433（避免循环导入）

        commands, blocked = [], []
        for cmd in AIAgent.parse_command_lines(reply, limit=limit):
            if block_dangerous and self.is_dangerous(cmd):
                blocked.append(cmd)
            else:
                commands.append(cmd)

        result = safety.review(commands)
        result["blocked"] = blocked
        if blocked:
            result["summary"] += f"｜已拦截 {len(blocked)} 条危险命令：{'、'.join(blocked)}"
        return result

    @staticmethod
    def _err_text(e: Exception) -> str:
        """把异常转成更友好的文本"""
        name = type(e).__name__
        if "Authentication" in name or "401" in str(e):
            return "API Key 无效或已过期"
        if "RateLimit" in name or "429" in str(e):
            return "请求过于频繁，请稍后重试"
        if "Connection" in name:
            return f"网络连接异常: {e}"
        return f"{name}: {e}"

    def set_task(self, task: str):
        """设置当前任务"""
        self.current_task = task

    def reset_context(self):
        """清空上下文"""
        self.context = []

    def get_context_length(self) -> int:
        """获取当前上下文长度"""
        return len(self.context)

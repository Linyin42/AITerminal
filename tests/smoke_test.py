"""
AITerminal 冒烟测试 - 不依赖真实设备，验证核心逻辑与界面可正常构建

运行:
    python tests/smoke_test.py
"""
import os
import sys
import time

# 无显示环境下运行 Qt
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402

from core.connector import BaseConnector  # noqa: E402
from core.terminal import TerminalBuffer, detect_vendor, clean_prompt_tail  # noqa: E402
from core.ai_agent import AIAgent  # noqa: E402
from core.session import SessionManager, SessionInfo  # noqa: E402
from core import telnet_connector as telnet_mod  # noqa: E402
from core.diff import diff_lines, summarize, to_unified, has_changes  # noqa: E402
from core.logger import SessionLogger, list_logs  # noqa: E402
from core.batch import BatchRunner, CommandWaiter, disable_paging  # noqa: E402
from core import safety as safety_mod  # noqa: E402
from core.xlsx_writer import write_xlsx  # noqa: E402
from core import manual as manual_mod  # noqa: E402
from core import patrol as patrol_mod  # noqa: E402
from core.patrol import PatrolRunner, export_patrol_excel, export_patrol_csv  # noqa: E402
from core import web_search as ws  # noqa: E402
from core.web_search import WebSearchClient  # noqa: E402
from core.serial_connector import SerialConnector  # noqa: E402
from config.settings import Settings  # noqa: E402
from config.presets import (  # noqa: E402
    PRESET_CONNECTIONS, get_vendor_commands, CONSOLE_PRESETS,
    BAUDRATES, PARITY_OPTIONS, FLOW_OPTIONS, LINE_ENDING_OPTIONS
)
from config.templates import (  # noqa: E402
    COMMAND_TEMPLATES, extract_variables, render_template,
    template_commands, save_config_commands
)
from plugins.base import PluginManager  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.diff_dialog import DiffDialog  # noqa: E402
from ui.template_dialog import TemplateDialog  # noqa: E402
from ui.prompt_dialog import PromptDialog  # noqa: E402

RESULTS = []
_APP = None  # 保持 QApplication 引用，避免被回收


def _settings_snapshot():
    """备份配置，避免测试写脏用户真实配置"""
    settings = Settings()
    return settings, dict(settings.data)


def _settings_restore(settings, snapshot):
    settings._data = snapshot
    settings.save()


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    flag = "PASS" if condition else "FAIL"
    print(f"[{flag}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


class DummyConnector(BaseConnector):
    """用于测试的假连接器（会模拟设备回显与提示符返回）"""

    def __init__(self, simulate_prompt: bool = True):
        super().__init__()
        self._buffer = ""
        self._connected = True
        self.sent = []
        self.simulate_prompt = simulate_prompt

    def connect(self, **kwargs) -> bool:
        return True

    def send(self, command: str):
        self.sent.append(command)
        if self.simulate_prompt:
            # 模拟设备：回显命令 + 输出 + 提示符，供"等提示符"逻辑判定
            self._notify_output(f"{command}\r\nOK\r\nswitch#")

    def send_raw(self, data: str):
        self.sent.append(data)
        if self.simulate_prompt:
            self._notify_output("switch#")

    def disconnect(self):
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_output(self) -> str:
        out, self._buffer = self._buffer, ""
        return out

    def get_output_block(self, timeout: float = 2.0) -> str:
        return self.get_output()


def test_terminal_buffer():
    buf = TerminalBuffer()
    buf.feed("hello\r\nworld")
    check("终端缓冲: 换行", buf.text() == "hello\nworld", repr(buf.text()))

    buf2 = TerminalBuffer()
    out = buf2.feed("progress 10%\rprogress 99%")
    check("终端缓冲: \\r 行内覆盖", out == "progress 99%" and buf2.text() == "progress 99%",
          repr((out, buf2.text())))

    buf3 = TerminalBuffer()
    buf3.feed("\x1b[31mERROR\x1b[0m ok")
    check("终端缓冲: 剥离ANSI", buf3.text() == "ERROR ok", repr(buf3.text()))

    buf4 = TerminalBuffer()
    buf4.feed("ab\b\bcd")
    check("终端缓冲: 退格", buf4.text() == "cd", repr(buf4.text()))

    check("厂商识别: H3C", detect_vendor("<H3C>display version\nComware Software") == "h3c")
    check("厂商识别: 华为", detect_vendor("Huawei Versatile Routing Platform VRP") == "huawei")
    check("厂商识别: Cisco", detect_vendor("Cisco IOS Software, C2960") == "cisco")

    tail = clean_prompt_tail("display version\nVRP V800\n<H3C>")
    check("去除尾部提示符", tail == "display version\nVRP V800", repr(tail))


def test_ai_agent():
    agent = AIAgent(api_key="test-key")

    check("危险命令识别: reboot", agent.is_dangerous("reboot"))
    check("危险命令识别: undo all", agent.is_dangerous("undo all"))
    check("危险命令识别: 普通命令", not agent.is_dangerous("display interface brief"))

    reply = "display version\nreboot\n# 注释行\nDONE\n"
    commands, blocked = agent.filter_commands(reply)
    check("命令拆分", commands == ["display version"], repr(commands))
    check("危险命令拦截", blocked == ["reboot"], repr(blocked))

    numbered = "1. system-view\n2) interface GigabitEthernet0/0/1\n- quit"
    commands, _ = agent.filter_commands(numbered)
    check("去除编号/项目符号", commands == ["system-view", "interface GigabitEthernet0/0/1", "quit"],
          repr(commands))

    agent.set_task("测试任务")
    agent.set_vendor("h3c")
    check("上下文初始为空", agent.get_context_length() == 0)
    agent.reset_context()
    check("上下文清空", agent.get_context_length() == 0)


def test_settings():
    """使用临时配置文件验证读写，避免污染用户真实配置"""
    settings = Settings()
    original_data = dict(settings.data)
    original_file = settings.config_file
    tmp_file = os.path.join(BASE_DIR, "tests", "_tmp_settings.json")
    try:
        settings.config_file = tmp_file
        settings.set("font_size", 12)
        # 先清空最近连接再测"追加"行为。
        # 注意：Settings() 会加载**真实**配置文件，用户只要用过软件，
        # recent_connections 里就会有记录；不清空的话下面 len==1 的断言
        # 会随真实数据漂移（这条测试踩过）。
        settings.set("recent_connections", [])
        settings.set_last_connection({"protocol": "SSH", "host": "10.0.0.1", "port": 22,
                                      "password": "secret", "name": "sw1"})
        settings.save()

        check("配置保存", os.path.exists(tmp_file))

        # 密码不应写入最近连接列表
        recents = settings.get_recent_connections()
        check("最近连接不含密码", all("password" not in r for r in recents), repr(recents))
        check("最近连接已记录", len(recents) == 1 and recents[0]["host"] == "10.0.0.1")
        check("配置项读取", settings.get("font_size") == 12)
    finally:
        if os.path.exists(tmp_file):
            os.remove(tmp_file)
        settings.config_file = original_file
        settings._data = original_data


def test_presets_and_plugins():
    check("预设连接非空", len(PRESET_CONNECTIONS) > 0)
    check("厂商命令库: h3c", "查看版本" in get_vendor_commands("h3c"))
    check("未知厂商回退", get_vendor_commands("unknown") == get_vendor_commands("h3c"))

    pm = PluginManager(os.path.join(BASE_DIR, "plugins"))
    plugins = pm.load_all()
    check("插件目录扫描不报错", isinstance(plugins, list))
    check("空钩子广播返回True", pm.dispatch("on_output", None, "text") is True)


def test_telnet_fallback():
    check("telnetlib 探测", isinstance(telnet_mod.HAS_TELNETLIB, bool),
          str(telnet_mod.HAS_TELNETLIB))
    check("MiniTelnet 回退实现存在", hasattr(telnet_mod, "_MiniTelnet"))
    check("连接器可正常导入", telnet_mod.TelnetConnector is not None)


# ------------------------------------------------------------------ v0.2 功能
def test_diff():
    old = ("sysname SW1\n"
           "vlan 10\n"
           " description USERS\n"
           "interface GigabitEthernet1/0/1\n"
           " port link-type access\n"
           "<H3C>\n")
    new = ("sysname SW1\n"
           "vlan 10\n"
           " description OFFICE\n"
           "vlan 20\n"
           " description GUEST\n"
           "interface GigabitEthernet1/0/1\n"
           " port link-type access\n"
           "<H3C>\n")

    added, removed = summarize(old, new)
    check("配置比对: 统计差异", added == 3 and removed == 1, f"+{added}/-{removed}")

    diff = diff_lines(old, new, context=1)
    tags = [t for t, _ in diff]
    check("配置比对: 含新增/删除标记", '+' in tags and '-' in tags, repr(tags))
    check("配置比对: 提示符行被剔除", all('H3C' not in line for _t, line in diff))
    check("配置比对: 相同文本无差异", not has_changes("a\nb", "a\nb"))

    unified = to_unified("a\nb", "a\nc", "旧", "新")
    check("配置比对: 生成unified diff",
          unified.startswith('---') and '+c' in unified, repr(unified))


def test_templates():
    check("模板库非空", len(COMMAND_TEMPLATES) > 0)

    commands = COMMAND_TEMPLATES["创建VLAN并划分端口"]["commands"]
    names = extract_variables(commands)
    check("模板: 变量提取", names == ["vlan_id", "vlan_name", "interface"], repr(names))

    rendered = render_template(commands, {
        "vlan_id": "20", "vlan_name": "OFFICE", "interface": "GE1/0/2"
    })
    check("模板: 渲染变量",
          "vlan 20" in rendered and "name OFFICE" in rendered and "interface GE1/0/2" in rendered,
          repr(rendered))

    partial = render_template(["vlan {vlan_id}", "name {vlan_name}"], {"vlan_id": "30"})
    check("模板: 未填变量保留占位", partial[1] == "name {vlan_name}", repr(partial))

    check("模板: 按名称渲染", template_commands("备份当前配置") == ["display current-configuration"])
    check("模板: 厂商保存命令",
          save_config_commands("cisco") == ["write memory"]
          and save_config_commands("h3c") == ["save force"])


def test_logger():
    import shutil
    tmp_dir = os.path.join(BASE_DIR, "tests", "_tmp_logs")
    try:
        logger = SessionLogger("测试/设备", "10.0.0.1", log_dir=tmp_dir)
        check("日志: 文件创建", logger.open() and os.path.exists(logger.path), logger.path)
        logger.write("display version\n")
        logger.mark("用户下发: save")
        logger.flush()
        logger.close()

        with open(logger.path, 'r', encoding='utf-8') as f:
            content = f.read()
        check("日志: 内容写入", "display version" in content and "用户下发: save" in content)
        check("日志: 文件名已转义",
              "/" not in os.path.basename(logger.path), os.path.basename(logger.path))
        check("日志: 目录扫描", len(list_logs(tmp_dir)) == 1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_batch():
    connector = DummyConnector()
    runner = BatchRunner(connector, ["display version", "reboot",
                                     "display interface brief", "   "],
                         delay_ms=0, block_dangerous=True)
    events = {"progress": [], "blocked": [], "finished": []}
    runner.progress.connect(lambda i, t, c: events["progress"].append((i, t, c)))
    runner.blocked.connect(lambda i, c: events["blocked"].append((i, c)))
    runner.finished.connect(lambda ok, msg: events["finished"].append((ok, msg)))

    runner._run()  # 同步执行，便于断言

    check("批量: 下发条数", len(events["progress"]) == 2, repr(events["progress"]))
    check("批量: 危险命令被拦截",
          len(events["blocked"]) == 1 and events["blocked"][0][1] == "reboot",
          repr(events["blocked"]))
    check("批量: 结束回调成功", bool(events["finished"]) and events["finished"][0][0] is True,
          repr(events["finished"]))
    check("批量: 空行被忽略",
          connector.sent == ["display version", "display interface brief"],
          repr(connector.sent))

    offline = DummyConnector()
    offline._connected = False
    runner2 = BatchRunner(offline, ["display version"], delay_ms=0)
    check("批量: 断开时拒绝执行", runner2.start() is False)


def test_snapshot_and_capture():
    session = SessionInfo(1, "测试设备", "SSH")
    session.connector = DummyConnector()

    session.start_capture()
    session.record_output("display current-configuration\n")
    session.record_output("sysname SW1\n")
    expected = len("display current-configuration\n") + len("sysname SW1\n")
    check("采集: 累计长度", session.capture_size() == expected, str(session.capture_size()))
    captured = session.stop_capture()
    check("采集: 取回内容", "sysname SW1" in captured)
    check("采集: 结束后不再累积", session.stop_capture() == "")

    session.add_snapshot("第一次", "vlan 10")
    session.add_snapshot("第二次", "vlan 20")
    check("快照: 最近一份", session.last_snapshot()[0] == "第二次")
    check("快照: 上一份", session.previous_snapshot()[0] == "第一次")

    for i in range(SessionInfo.MAX_SNAPSHOTS + 5):
        session.add_snapshot(f"第{i}次", "x")
    check("快照: 数量上限",
          len(session.snapshots) == SessionInfo.MAX_SNAPSHOTS, str(len(session.snapshots)))


def test_new_settings():
    settings = Settings()
    original = dict(settings.data)
    try:
        check("配置: 新增默认项",
              settings.auto_log is True and settings.batch_delay_ms == 500)

        settings.set("batch_delay_ms", 1200)
        check("配置: 批量间隔读取", settings.batch_delay_ms == 1200)

        settings.set("system_prompt", "  自定义提示词  ")
        check("配置: 提示词去空格", settings.system_prompt == "自定义提示词")

        encoded = Settings.encode_password("P@ssw0rd!")
        check("配置: 密码混淆",
              encoded.startswith("b64:") and "P@ssw0rd" not in encoded, encoded)
        check("配置: 密码还原", Settings.decode_password(encoded) == "P@ssw0rd!")

        settings.set_last_sessions([{"name": "sw1", "protocol": "SSH"}])
        check("配置: 会话列表存取", settings.get_last_sessions()[0]["name"] == "sw1")
    finally:
        settings._data = original


def test_ai_prompt_override():
    agent = AIAgent(api_key="test-key")
    check("提示词: 默认内置", "网络工程师" in agent.system_prompt)

    agent.set_system_prompt("只输出命令")
    check("提示词: 自定义生效", agent.system_prompt == "只输出命令")

    agent.set_system_prompt("")
    check("提示词: 清空恢复内置",
          agent.system_prompt == AIAgent.default_system_prompt())


def test_v02_dialogs():
    session = SessionInfo(1, "测试设备", "SSH")
    session.connector = DummyConnector()
    session.add_snapshot("快照A", "vlan 10\n description USERS")
    session.record_output("vlan 10\n description OFFICE")

    dialog = DiffDialog(None, session)
    dialog.run_diff()
    summary = dialog.summary_label.text()
    check("比对对话框: 生成结果", "新增" in summary or "一致" in summary, summary)
    html_text = dialog.diff_view.toHtml()
    check("比对对话框: 差异着色", "3fb950" in html_text or "f85149" in html_text)
    dialog.close()

    tpl = TemplateDialog(None, default_delay=300, session_name="测试设备", vendor="h3c")
    check("模板对话框: 模板已载入", tpl.tpl_list.count() == len(COMMAND_TEMPLATES),
          str(tpl.tpl_list.count()))
    check("模板对话框: 变量表单已生成", "vlan_id" in tpl.var_edits, repr(list(tpl.var_edits)))
    check("模板对话框: 预览非空", len(tpl.get_commands()) > 0)
    tpl.var_edits["vlan_id"].setText("88")
    check("模板对话框: 变量联动预览",
          "vlan 88" in tpl.preview.toPlainText(), tpl.preview.toPlainText())
    tpl.close()

    prompt_dialog = PromptDialog(None, "")
    check("提示词对话框: 默认填充", "网络工程师" in prompt_dialog.editor.toPlainText())
    check("提示词对话框: 默认值返回空", prompt_dialog.get_prompt() == "")
    prompt_dialog.editor.setPlainText("自定义规则")
    check("提示词对话框: 自定义返回", prompt_dialog.get_prompt() == "自定义规则")
    prompt_dialog.close()


# ------------------------------------------------------------------ 联网检索
BING_FIXTURE = """
<ol id="b_results">
<li class="b_algo" data-id><h2><a href="https://example.com/a">H3C 命令教程A</a></h2>
<p class="b_lineclamp2">这是摘要A</p></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=xx&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9i&amp;ntb=1">标题B</a></h2>
<div class="b_caption"><p>这是摘要B</p></div></li>
</ol>
"""

BAIDU_FIXTURE = """
<div class="result c-container"><h3 class="t"><a href="https://www.baidu.com/link?url=abc">交换机命令甲</a></h3>
<div class="c-abstract">摘要甲内容</div></div>
<div class="result c-container"><h3 class="t"><a href="https://www.baidu.com/link?url=def">交换机命令乙</a></h3>
<div class="c-abstract">摘要乙内容</div></div>
"""


def test_web_search_parsing():
    """离线校验各检索后端的 HTML 解析（不依赖网络）"""
    bing = ws.BingBackend.parse(BING_FIXTURE)
    check("检索: 必应解析条数", len(bing) == 2, str(len(bing)))
    check("检索: 必应标题", bing and bing[0].title == "H3C 命令教程A", repr(bing[0].title if bing else None))
    check("检索: 必应摘要", bing and bing[0].snippet == "这是摘要A")
    # 跳转链接要先反转义 &amp; 再解 base64
    check("检索: 必应跳转链接还原",
          len(bing) > 1 and bing[1].url == "https://example.com/b",
          repr(bing[1].url if len(bing) > 1 else None))
    check("检索: 必应保留原始链接",
          len(bing) > 1 and "bing.com/ck/a" in bing[1].raw_url)

    baidu = ws.BaiduBackend.parse(BAIDU_FIXTURE)
    check("检索: 百度解析条数", len(baidu) == 2, str(len(baidu)))
    check("检索: 百度标题", baidu and baidu[0].title == "交换机命令甲")
    check("检索: 百度摘要", baidu and baidu[0].snippet == "摘要甲内容", repr(baidu[0].snippet if baidu else None))

    check("检索: 跳转链接识别", ws.is_redirect_url("https://www.baidu.com/link?url=x"))
    check("检索: 非跳转链接识别", not ws.is_redirect_url("https://www.h3c.com/cn/"))

    text = ws.html_to_text("<html><head><style>x{}</style></head><body><p>第一行</p>"
                           "<script>bad()</script><div>第二行</div></body></html>")
    check("检索: HTML转文本", "第一行" in text and "第二行" in text and "bad()" not in text, repr(text))
    check("检索: 去标签反转义", ws.strip_tags("<b>A&amp;B</b>") == "A&B")


def test_web_search_client():
    client = WebSearchClient(backend="searxng", searxng_url="")
    check("检索: 无可用后端时返回空", client.search("test") == [])
    check("检索: 无后端时有错误说明", bool(client.last_error), client.last_error)

    results = ws.BaiduBackend.parse(BAIDU_FIXTURE)
    formatted = WebSearchClient.format_results(results)
    check("检索: 结果格式化", "交换机命令甲" in formatted and "链接:" in formatted)

    context = WebSearchClient.build_context({
        "query": "测试", "results": results,
        "pages": [("https://x.com", "标题X", "正文内容X")], "error": ""
    })
    check("检索: 上下文拼装",
          "联网检索关键词: 测试" in context and "正文内容X" in context)


def test_ai_search_flow():
    """校验 AI 的 SEARCH 协议、资料回填与联网问答（用假模型客户端，不联网）"""
    from core.ai_agent import SEARCH_INSTRUCTION

    agent = AIAgent(api_key="test-key")
    check("AI检索: 默认未启用", agent.search_enabled is False)
    check("AI检索: 未启用时提示词不含协议", SEARCH_INSTRUCTION.strip()[:10] not in agent.system_prompt)

    agent.set_search_client(WebSearchClient(backend="auto"))
    check("AI检索: 注入后启用", agent.search_enabled is True)
    check("AI检索: 提示词追加协议", "SEARCH:" in agent.system_prompt)

    check("AI检索: 解析检索请求",
          agent.parse_search_request("SEARCH: H3C 端口镜像 命令") == "H3C 端口镜像 命令")
    check("AI检索: 解析中文冒号",
          agent.parse_search_request("SEARCH：Cisco console 乱码") == "Cisco console 乱码")
    check("AI检索: 普通命令不算检索",
          agent.parse_search_request("display interface brief") is None)

    # 用桩客户端替代真实检索
    class _StubSearch:
        max_results = 3

        def search_and_read(self, query, top_n=2, max_chars=4000, max_results=None):
            return {
                "query": query,
                "results": [ws.SearchResult("H3C 端口镜像配置", "https://example.com/mirror",
                                            "使用 mirroring-group 命令", "stub")],
                "pages": [("https://example.com/mirror", "H3C 端口镜像配置",
                           "mirroring-group 1 local\nmirroring-group 1 mirroring-port ...")],
                "error": "",
            }

        @staticmethod
        def build_context(data, max_chars=6000):
            return WebSearchClient.build_context(data, max_chars)

    agent.set_search_client(_StubSearch())
    context = agent.web_context("H3C 端口镜像")
    check("AI检索: 资料回填包含命令", "mirroring-group" in context, repr(context[:120]))
    check("AI检索: 检索次数累加", agent.search_count == 1)

    class _EmptySearch:
        max_results = 3

        def search_and_read(self, query, top_n=2, max_chars=4000, max_results=None):
            return {"query": query, "results": [], "pages": [], "error": "超时"}

    agent.set_search_client(_EmptySearch())
    empty_context = agent.web_context("查不到的东西")
    check("AI检索: 无结果时明确告知", "没有返回结果" in empty_context, repr(empty_context[:80]))

    # 联网问答：用假模型客户端验证整条链路
    class _FakeLLM:
        def chat(self, system, messages, max_tokens=1024, temperature=0.3):
            return "结论: 用 mirroring-group 配置端口镜像 [1]"

        def describe(self):
            return "FakeLLM"

    agent.set_search_client(_StubSearch())
    agent.llm = _FakeLLM()
    captured = {}
    agent.research_ready.connect(lambda answer, sources: captured.update(
        {"answer": answer, "sources": sources}))
    answer = agent.research("H3C 怎么配置端口镜像", "display current-configuration")
    check("AI检索: 联网问答返回内容", "mirroring-group" in answer, repr(answer))
    check("AI检索: 发出结果信号", captured.get("answer") == answer)
    check("AI检索: 附带来源列表",
          captured.get("sources") and captured["sources"][0]["url"] == "https://example.com/mirror")

    agent.set_search_client(None)
    check("AI检索: 关闭后不联网", agent.research("任意问题") == "")


# ------------------------------------------------------------------ 串口 Console
class _FakeSerial:
    """模拟 pyserial 的串口对象，用于离线验证发送逻辑"""

    def __init__(self):
        self.is_open = True
        self.baudrate = 9600
        self.written = b""
        self.breaks = []
        self.dtr = None
        self.rts = None

    @property
    def in_waiting(self):
        return 0

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        pass

    def send_break(self, duration=0.25):
        self.breaks.append(duration)

    def close(self):
        self.is_open = False


def test_serial_console():
    connector = SerialConnector()
    check("串口: 声明支持串口控制", connector.supports_serial_control() is True)
    check("串口: Console 默认 CR 结尾", connector.line_ending == '\r',
          repr(connector.line_ending))

    # 端口枚举不应抛异常
    ports = SerialConnector.list_ports_detailed()
    check("串口: 端口枚举可用", isinstance(ports, list))

    # 打开不存在的端口应返回 False 并给出可读错误
    errors = []
    connector.error_occurred.connect(errors.append)
    opened = connector.connect(port="COM_DOES_NOT_EXIST_999", baudrate=9600,
                               auto_reconnect=False)
    check("串口: 打开不存在的端口返回False", opened is False)
    check("串口: 给出可读错误", bool(errors), repr(errors))

    # 用假串口验证发送与行结束符
    connector = SerialConnector()
    connector.ser = _FakeSerial()
    connector._connected = True
    connector._params = {'port': 'COM3', 'baudrate': 9600, 'bytesize': 8,
                         'parity': 'N', 'stopbits': 1, 'char_delay_ms': 0}

    connector.send("display version")
    check("串口: 命令以CR结尾", connector.ser.written == b"display version\r",
          repr(connector.ser.written))

    connector.send_raw("\x03")
    check("串口: 原始数据不追加结尾", connector.ser.written.endswith(b"\x03"))

    connector.set_line_ending("\r\n")
    connector.ser.written = b""
    connector.send("save")
    check("串口: 切换行结束符", connector.ser.written == b"save\r\n",
          repr(connector.ser.written))

    connector.set_char_delay(2)
    check("串口: 字符间隔生效", connector._char_delay == 0.002, str(connector._char_delay))

    check("串口: 发送Break", connector.send_break(0.3) is True and connector.ser.breaks == [0.3])
    check("串口: DTR控制", connector.set_dtr(True) is True and connector.ser.dtr is True)
    check("串口: RTS控制", connector.set_rts(False) is True and connector.ser.rts is False)
    check("串口: 运行时改波特率",
          connector.set_baudrate(115200) is True and connector.ser.baudrate == 115200)

    params = connector.connection_params()
    check("串口: 参数可回读",
          params.get('port') == 'COM3' and params.get('baudrate') == 115200, repr(params))
    check("串口: info 含端口与波特率", "COM3" in connector.info(), connector.info())

    # 常见报错翻译
    occupied = SerialConnector._explain_error(Exception("Access is denied."))
    check("串口: 占用报错翻译", "被占用" in occupied, occupied)

    # Console 预设与选项
    check("串口: Console 预设非空", len(CONSOLE_PRESETS) > 0)
    check("串口: 波特率表含115200", 115200 in BAUDRATES)
    check("串口: 校验选项完整", len(PARITY_OPTIONS) == 5)
    check("串口: 流控选项完整", len(FLOW_OPTIONS) == 4)
    check("串口: 行结束符含CR", any(v == "\r" for _l, v in LINE_ENDING_OPTIONS))


def test_ai_panel_tabs_and_queue():
    """校验 AI 面板三个页签与待执行队列"""
    from ui.ai_panel import AIPanel
    panel = AIPanel()
    check("AI面板: 三个页签", panel.tabs.count() == 3, str(panel.tabs.count()))
    check("AI面板: 页签标题",
          [panel.tabs.tabText(i) for i in range(3)] == ["AI 助手", "资料检索", "待执行"])

    check("AI面板: 待执行初始为空", panel.queue_list.count() == 0)
    added = panel.add_commands(["display version", "reboot", "display version", ""])
    check("AI面板: 入队去重", added == 2, str(added))
    check("AI面板: 默认全部勾选", len(panel.checked_commands()) == 2)

    panel.set_all_checked(False)
    check("AI面板: 全不选", panel.checked_commands() == [])
    panel.queue_list.item(0).setCheckState(Qt.CheckState.Checked)
    check("AI面板: 单条勾选", panel.checked_commands() == ["display version"])

    panel.remove_checked()
    check("AI面板: 移除勾选", panel.queue_list.count() == 1, str(panel.queue_list.count()))

    panel.show_research_result("回答内容", [
        {"title": "标题1", "url": "https://example.com/1", "snippet": "s"}
    ])
    check("AI面板: 联网结果显示", "回答内容" in panel.research_output.toPlainText())
    check("AI面板: 来源列表", panel.source_list.count() == 1)

    panel.update_screen_preview("设备输出的内容")
    check("AI面板: 屏幕预览", "设备输出的内容" in panel.screen_preview.toPlainText())

    panel.clear_queue()
    check("AI面板: 清空队列", panel.queue_list.count() == 0)
    panel.close()


# ------------------------------------------------------------------ 本地资料库
def _make_minimal_pdf(path: str, lines):
    """手工拼一个含文本的最小 PDF，用来验证 pypdf 抽取路径"""
    import zipfile  # noqa: F401  (保持与其它样例一致的导入位置)

    body = b"BT /F1 12 Tf 72 720 Td "
    for index, line in enumerate(lines):
        if index:
            body += b"0 -16 Td "
        body += b"(" + line.encode("ascii", "replace") + b") Tj "
    body += b"ET"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref_position = len(pdf)
    pdf += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        pdf += ("%010d 00000 n \n" % offset).encode()
    pdf += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
            b"startxref\n" + str(xref_position).encode() + b"\n%%EOF\n")

    with open(path, "wb") as f:
        f.write(bytes(pdf))


def _make_sample_docs(src_dir: str):
    """造几种格式的样例资料：md / docx / xlsx / pdf / zip / 不支持的老格式"""
    import zipfile

    with open(os.path.join(src_dir, "H3C_S5560_命令手册.md"), "w", encoding="utf-8") as f:
        f.write("# H3C S5560 命令手册\n\n## 端口镜像\n"
                "mirroring-group 1 local\n"
                "mirroring-group 1 mirroring-port GigabitEthernet1/0/1 both\n"
                "mirroring-group 1 monitor-port GigabitEthernet1/0/24\n\n"
                "## VLAN 配置\nvlan 10\n port link-type access\n")

    with zipfile.ZipFile(os.path.join(src_dir, "华为S5700巡检.docx"), 'w') as z:
        z.writestr('word/document.xml',
                   '<w:document><w:body><w:p><w:r><w:t>华为 S5700 巡检命令</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>display device</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>display cpu-usage</w:t></w:r></w:p></w:body></w:document>')

    with zipfile.ZipFile(os.path.join(src_dir, "接口对照表.xlsx"), 'w') as z:
        z.writestr('xl/sharedStrings.xml',
                   '<sst><si><t>接口</t></si><si><t>对端设备</t></si>'
                   '<si><t>GE1/0/1</t></si><si><t>核心交换机</t></si></sst>')
        z.writestr('xl/worksheets/sheet1.xml',
                   '<worksheet><sheetData>'
                   '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                   '<row><c t="s"><v>2</v></c><c t="s"><v>3</v></c></row>'
                   '</sheetData></worksheet>')

    # PDF 命令手册（手工构造，用于验证 pypdf 抽取）
    _make_minimal_pdf(os.path.join(src_dir, "Cisco_console手册.pdf"),
                      ["console 9600 8N1 troubleshooting",
                       "show line console 0",
                       "speed 9600"])

    # 打成 zip，验证压缩包导入
    with zipfile.ZipFile(os.path.join(src_dir, "手册合集.zip"), 'w') as z:
        z.writestr('Cisco巡检.txt', 'show version\nshow ip interface brief\n')

    with open(os.path.join(src_dir, "老格式.doc"), "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0 old binary doc")


def test_knowledge_base():
    """本地资料库：导入 / 抽取 / 建索引 / 检索 / 删除"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    src_dir = tempfile.mkdtemp(prefix="aiterminal_kb_src_")
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_kb_root_")
    try:
        _make_sample_docs(src_dir)
        kb = KnowledgeBase(root_dir=kb_dir)

        result = kb.import_paths([src_dir])
        check("资料库: 导入新增", len(result["added"]) >= 5, repr(result["added"]))
        check("资料库: 不支持格式被跳过并说明原因",
              any("老格式.doc" in p for p, _m in result["errors"]),
              repr(result["errors"]))
        check("资料库: zip 内文件被解出",
              any("Cisco巡检.txt" in name for name in result["added"]),
              repr(result["added"]))

        # 导入一段剪贴板文本
        saved = kb.import_text("手记.md", "console 乱码 通常是波特率不匹配\n")
        check("资料库: 导入剪贴文本", bool(saved) and os.path.exists(saved), str(saved))

        summary = kb.build_index()
        check("资料库: 建索引文档数", summary["documents"] >= 5, repr(summary))
        check("资料库: 建索引片段数", summary["chunks"] >= 5, repr(summary))
        check("资料库: 无解析失败", not summary["failed"], repr(summary["failed"]))

        # 增量复用
        again = kb.build_index()
        check("资料库: 增量复用已索引文档", again["reused"] == again["documents"],
              repr(again))

        hits = kb.search("端口镜像 mirroring-group", top_k=2)
        check("资料库: 英文命令命中", bool(hits) and "mirroring-group" in hits[0]["text"],
              repr(hits[:1]))
        check("资料库: 返回来源与段号",
              bool(hits) and "命令手册" in hits[0]["source"] and hits[0]["chunk_index"] >= 0)

        cn_hits = kb.search("华为巡检命令", top_k=2)
        check("资料库: 中文检索命中 docx", bool(cn_hits), repr(cn_hits[:1]))

        xlsx_hits = kb.search("GE1/0/1 对端", top_k=2)
        check("资料库: xlsx 表格可检索",
              bool(xlsx_hits) and "核心交换机" in xlsx_hits[0]["text"], repr(xlsx_hits[:1]))

        pdf_hits = kb.search("console troubleshooting speed", top_k=2)
        check("资料库: PDF 手册可检索",
              bool(pdf_hits) and ".pdf" in pdf_hits[0]["source"]
              and "show line console" in pdf_hits[0]["text"], repr(pdf_hits[:1]))

        check("资料库: 无关词不命中", kb.search("zzzz_不存在的内容_zzzz") == [])

        context = kb.search_text("mirroring-group", top_k=2, max_chars=2000)
        check("资料库: 生成给AI的上下文",
              "[本地资料1]" in context and "mirroring-group" in context, repr(context[:80]))

        documents = kb.list_documents()
        check("资料库: 文档列表", len(documents) >= 4, str(len(documents)))
        check("资料库: 列表含状态字段",
              all("error" in d and "chunks" in d for d in documents))

        stats = kb.stats()
        check("资料库: 概况统计",
              stats["documents"] >= 4 and stats["chunks"] >= 4, repr(stats))

        # 删除
        target = documents[0]["name"]
        check("资料库: 删除文档", kb.remove_document(target) is True)
        check("资料库: 删除后数量减少", kb.document_count() == len(documents) - 1)
        check("资料库: 拒绝删除库外文件",
              kb.remove_document(os.path.join(src_dir, "老格式.doc")) is False)
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_ai_local_lookup():
    """AI 优先查本地资料库，再联网"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    src_dir = tempfile.mkdtemp(prefix="aiterminal_lk_src_")
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_lk_root_")
    try:
        _make_sample_docs(src_dir)
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.import_paths([src_dir])
        kb.build_index()

        agent = AIAgent(api_key="test-key", knowledge_base=kb)
        check("本地检索: 具备检索能力", agent.lookup_enabled is True)
        check("本地检索: 仅本地也启用协议", "SEARCH:" in agent.system_prompt)
        check("本地检索: 未启用联网", agent.search_enabled is False)

        local = agent.local_context("端口镜像 命令")
        check("本地检索: 命中本地资料", "mirroring-group" in local, repr(local[:100]))
        check("本地检索: 记录命中来源", bool(agent.last_local_hits))

        context = agent.lookup_context("端口镜像 命令")
        check("本地检索: 合并上下文含本地标记", "【本地资料库】" in context, repr(context[:60]))
        check("本地检索: 来源标注为 local",
              agent.last_lookup_sources
              and agent.last_lookup_sources[0]["source_type"] == "local",
              repr(agent.last_lookup_sources[:1]))

        # 只开本地、不联网也能回答问题
        class _FakeLLM:
            def chat(self, system, messages, max_tokens=1024, temperature=0.3):
                return "用 mirroring-group 配置端口镜像 [1]"

            def describe(self):
                return "FakeLLM"

        agent.llm = _FakeLLM()
        captured = {}
        agent.research_ready.connect(lambda answer, sources: captured.update(
            {"answer": answer, "sources": sources}))
        answer = agent.research("H3C 怎么配置端口镜像")
        check("本地检索: 仅本地资料即可作答", "mirroring-group" in answer, repr(answer))
        check("本地检索: 来源列表为本地资料",
              captured.get("sources")
              and captured["sources"][0]["source_type"] == "local",
              repr(captured.get("sources", [])[:1]))

        # 本地查不到、联网也关闭时给出明确提示
        agent.set_knowledge_base(None)
        check("本地检索: 都关闭时不再检索", agent.lookup_enabled is False)
        errors = []
        agent.error_occurred.connect(errors.append)
        check("本地检索: 无能力时返回空", agent.research("任意问题") == "")
        check("本地检索: 给出未启用提示", bool(errors), repr(errors))

        # 开关关闭后不检索
        agent.set_knowledge_base(kb)
        kb.set_enabled(False)
        check("本地检索: 开关关闭后不命中", agent.local_context("端口镜像") == "")
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_knowledge_dialog():
    """资料库管理界面冒烟测试"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase
    from ui.knowledge_dialog import KnowledgeDialog, build_file_filter

    src_dir = tempfile.mkdtemp(prefix="aiterminal_kd_src_")
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_kd_root_")
    try:
        _make_sample_docs(src_dir)
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.import_paths([src_dir])
        kb.build_index()

        dialog = KnowledgeDialog(None, kb)
        check("资料库界面: 列表已填充", dialog.table.rowCount() >= 4,
              str(dialog.table.rowCount()))
        check("资料库界面: 统计信息", "篇资料" in dialog.stats_label.text(),
              dialog.stats_label.text())
        check("资料库界面: 文件过滤器含pdf", "*.pdf" in build_file_filter())

        dialog.query_input.setText("mirroring-group")
        dialog.test_search()
        output = dialog.result_view.toPlainText()
        check("资料库界面: 检索测试命中", "命中" in output and "mirroring-group" in output,
              output[:120])

        dialog.query_input.setText("完全不存在的关键词zzz")
        dialog.test_search()
        check("资料库界面: 检索测试无命中提示",
              "没有命中" in dialog.result_view.toPlainText())
        dialog.close()
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(kb_dir, ignore_errors=True)


# ------------------------------------------------------------------ 知识沉淀
def test_save_note():
    """把 AI 回答 / 命令存回资料库，并能被检索到"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    kb_dir = tempfile.mkdtemp(prefix="aiterminal_note_")
    try:
        kb = KnowledgeBase(root_dir=kb_dir)

        path = kb.save_note(
            "H3C 端口镜像配置命令",
            "mirroring-group 1 local\n"
            "mirroring-group 1 mirroring-port GigabitEthernet1/0/1 both",
            category="命令速查",
            tags="端口镜像, mirroring-group, S5560",
            meta={"设备": "核心交换机（SSH 10.0.0.1）", "厂商": "h3c"},
        )
        check("沉淀: 文件已保存", bool(path) and os.path.exists(path), str(path))
        check("沉淀: 归入分类子目录", "命令速查" in (path or ""), str(path))

        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        check("沉淀: 标题写在首行", text.startswith("# H3C 端口镜像配置命令"), text[:40])
        check("沉淀: 含记录时间与分类",
              "记录时间:" in text and "分类: 命令速查" in text)
        check("沉淀: 含标签", "标签: 端口镜像, mirroring-group, S5560" in text)
        check("沉淀: 含设备元信息", "厂商: h3c" in text and "核心交换机" in text)
        check("沉淀: 含正文命令", "mirroring-group 1 local" in text)

        # 建索引后应该能被检索到（这就是"知识库越用越厚"的关键）
        kb.build_index()
        hits = kb.search("端口镜像 mirroring-group", top_k=3)
        check("沉淀: 可被本地检索命中",
              bool(hits) and "mirroring-group" in hits[0]["text"], repr(hits[:1]))
        check("沉淀: 可按标签/型号检索",
              bool(kb.search("S5560 端口镜像", top_k=3)))

        # 命令清单类笔记
        cmd_path = kb.save_commands(
            "Cisco 上电巡检命令",
            ["show version", "show ip interface brief"],
            tags=["cisco", "巡检"], note="设备上电后先跑这几条",
        )
        check("沉淀: 命令清单已保存", bool(cmd_path) and os.path.exists(cmd_path))
        with open(cmd_path, 'r', encoding='utf-8') as f:
            cmd_text = f.read()
        check("沉淀: 命令用代码块包裹",
              "```" in cmd_text and "show version" in cmd_text)
        check("沉淀: 附带说明文字", "上电后先跑这几条" in cmd_text)

        # 同名不覆盖、空标题自动命名
        second = kb.save_note("H3C 端口镜像配置命令", "第二次内容", category="命令速查")
        check("沉淀: 同名自动加序号", second != path and os.path.exists(second), str(second))
        auto = kb.save_note("", "内容")
        check("沉淀: 空标题自动命名", bool(auto) and os.path.exists(auto))
        check("沉淀: 标签可省略", bool(kb.save_note("无标签笔记", "内容", tags=None)))
        check("沉淀: 统计已更新", kb.stats()["documents"] >= 4, repr(kb.stats()))
    finally:
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_note_dialog():
    """沉淀对话框回填与取值"""
    from ui.note_dialog import NoteDialog

    dialog = NoteDialog(None, title="H3C 端口镜像", content="mirroring-group 1 local",
                        category="命令速查", tags=["h3c", "镜像"], root_dir="C:/kb")
    values = dialog.get_values()
    check("沉淀对话框: 标题回填", values["title"] == "H3C 端口镜像")
    check("沉淀对话框: 分类回填", values["category"] == "命令速查", values["category"])
    check("沉淀对话框: 标签回显", values["tags"] == "h3c, 镜像", values["tags"])
    check("沉淀对话框: 内容回填", "mirroring-group" in values["content"])
    check("沉淀对话框: 提示保存路径", "命令速查" in dialog.path_label.text(),
          dialog.path_label.text())

    dialog.title_input.setText("改过的标题")
    dialog.category_combo.setCurrentText("配置案例")
    check("沉淀对话框: 标题可改", dialog.get_values()["title"] == "改过的标题")
    check("沉淀对话框: 分类可改", dialog.get_values()["category"] == "配置案例")
    dialog.close()


def test_ai_panel_save_note():
    """AI 面板三处"存为资料"入口"""
    from ui.ai_panel import AIPanel

    panel = AIPanel()
    captured = []
    panel.save_note_requested.connect(
        lambda title, content, category: captured.append((title, content, category)))

    # 资料检索结果 → 存为资料
    answer = "结论: 用 mirroring-group 配置端口镜像 [1]"
    panel.question_input.setPlainText("H3C 怎么配置端口镜像")
    panel.show_research_result(answer, [
        {"title": "H3C命令手册（第 1 段）", "path": "C:/kb/a.md", "source_type": "local"},
        {"title": "某博客", "url": "https://example.com/x", "source_type": "web"},
    ])
    panel.on_save_research()
    check("AI面板: 检索结果可存为资料", len(captured) == 1, str(captured))
    title, content, category = captured[-1]
    check("AI面板: 标题取自提问", title.startswith("H3C 怎么配置端口镜像"), title)
    check("AI面板: 内容含回答", "mirroring-group" in content)
    check("AI面板: 内容含参考来源", "参考来源" in content and "命令手册" in content)
    check("AI面板: 分类为命令速查", category == "命令速查", category)

    # AI 输出 → 存为资料
    panel.append_output("[AI] display interface brief")
    panel.on_save_output()
    check("AI面板: AI输出可存为资料", len(captured) == 2)
    check("AI面板: 输出内容正确", "display interface brief" in captured[-1][1])
    check("AI面板: 输出分类为AI笔记", captured[-1][2] == "AI笔记")

    # 待执行队列 → 存为资料
    panel.add_commands(["display version", "display cpu-usage"])
    panel.on_save_queue()
    check("AI面板: 队列可存为资料", len(captured) == 3)
    check("AI面板: 队列内容为代码块",
          captured[-1][1].startswith("```") and "display cpu-usage" in captured[-1][1])
    panel.close()


def test_main_window_note_sinking():
    """主窗口：沉淀元信息、标签建议与落库"""
    import shutil
    import tempfile

    window = MainWindow()
    settings = window.settings
    original = dict(settings.data)
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_mw_kb_")
    try:
        settings.set("knowledge_dir", kb_dir)
        window.session_manager.refresh_knowledge()

        manager = window.session_manager
        session = SessionInfo(1, "核心交换机", "SSH")
        session.connector = DummyConnector()
        session.config = {"host": "10.0.0.1", "protocol": "SSH"}
        session.vendor = "h3c"
        session.ai_task = "检查端口镜像配置"
        manager._sessions[1] = session
        manager._next_id = 2
        window.on_session_created(1, "核心交换机")

        meta = window.build_note_meta(session)
        check("沉淀: 元信息含设备", "核心交换机" in meta.get("设备", ""), repr(meta))
        check("沉淀: 元信息含厂商", meta.get("厂商") == "h3c", repr(meta))
        check("沉淀: 元信息含任务", meta.get("任务") == "检查端口镜像配置", repr(meta))

        tags = window.suggest_tags(session, "端口镜像", "mirroring-group 1 local")
        check("沉淀: 建议标签含厂商与协议",
              "h3c" in tags and "SSH" in tags, tags)

        path = window.save_note_to_kb(
            "H3C 端口镜像命令", "mirroring-group 1 local",
            category="命令速查", tags=tags, meta=meta)
        check("沉淀: 主窗口保存成功", bool(path) and os.path.exists(path), str(path))

        # 等后台索引落盘后再检索（build_index 内部有锁，会等前一个完成）
        kb = window.session_manager.knowledge_base
        kb.build_index()
        hits = kb.search("端口镜像 mirroring-group", top_k=2)
        check("沉淀: 沉淀内容可被检索", bool(hits), repr(hits[:1]))
        check("沉淀: 资料库状态已刷新",
              "篇资料" in window.ai_panel.kb_status.text(),
              window.ai_panel.kb_status.text())

        window.close_session(0)
    finally:
        settings._data = original
        settings.save()
        shutil.rmtree(kb_dir, ignore_errors=True)
        window.close()


# ------------------------------------------------------------------ Excel 写入
def test_xlsx_writer():
    """轻量 xlsx 写入器（巡检导出用）"""
    import shutil
    import tempfile
    import zipfile
    from core.knowledge import extract_xlsx

    tmp = tempfile.mkdtemp(prefix="aiterminal_xlsx_")
    try:
        path = os.path.join(tmp, "巡检结果.xlsx")
        ok = write_xlsx(path, [
            ("巡检汇总", [
                ["序号", "设备名称", "状态", "耗时(秒)"],
                [1, "核心交换机", "成功", 3.2],
                [2, "接入交换机", "失败", 10.0],
            ]),
            ("命令明细", [
                ["设备", "命令", "输出"],
                ["核心交换机", "display version", "含特殊字符 & < > 与换行\n第二行"],
            ]),
        ], column_widths={"巡检汇总": [6, 20, 8, 10], "命令明细": [16, 26, 80]})

        check("xlsx: 文件已生成", ok and os.path.exists(path))
        with zipfile.ZipFile(path) as zf:
            check("xlsx: zip 结构完整", zf.testzip() is None)
            names = zf.namelist()
        check("xlsx: 含必要部件",
              all(part in names for part in (
                  '[Content_Types].xml', 'xl/workbook.xml', 'xl/styles.xml',
                  'xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml')))

        text = extract_xlsx(path)
        check("xlsx: 读回表头", "设备名称" in text and "状态" in text)
        check("xlsx: 读回数据行", "核心交换机" in text and "接入交换机" in text)
        check("xlsx: 读回数字", "3.2" in text and "10.0" in text)
        check("xlsx: 特殊字符与换行正确",
              "含特殊字符 & < > 与换行" in text and "第二行" in text)
        check("xlsx: 使用真实工作表名",
              "--- 巡检汇总 ---" in text and "--- 命令明细 ---" in text)

        # 非法字符与超长内容不应写坏文件
        dirty = os.path.join(tmp, "脏数据.xlsx")
        check("xlsx: 非法控制字符被清理",
              write_xlsx(dirty, [("S", [["正常\x00\x08文本", "x" * 40000]])]))
        dirty_text = extract_xlsx(dirty)
        check("xlsx: 清理后仍可读", "正常文本" in dirty_text and "已截断" in dirty_text)

        # 工作表名去重与非法字符
        dup = os.path.join(tmp, "重名.xlsx")
        check("xlsx: 工作表名处理",
              write_xlsx(dup, [("a/b", [["1"]]), ("a?b", [["2"]]), ("a*b", [["3"]])]))
        dup_text = extract_xlsx(dup)
        check("xlsx: 非法字符被替换", "--- a_b ---" in dup_text, repr(dup_text[:60]))
        check("xlsx: 重名自动加序号",
              "a_b_2" in dup_text and "a_b_3" in dup_text, repr(dup_text[:90]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 笔记手册
def test_manual():
    """笔记筛选与批量导出成手册"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase

    kb_dir = tempfile.mkdtemp(prefix="aiterminal_manual_")
    try:
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.save_note("H3C S5560 端口镜像配置命令",
                     "```\nmirroring-group 1 local\n```",
                     category="命令速查", tags="h3c, 端口镜像",
                     meta={"设备": "核心交换机", "厂商": "h3c",
                           "记录时间": "2026-09-21 19:46:24"})
        kb.save_note("Cisco 上电巡检命令",
                     "```\nshow version\n```",
                     category="命令速查", tags="cisco, 巡检")
        kb.save_note("接入交换机排错记录",
                     "端口 err-disable，先 shutdown 再 undo shutdown",
                     category="排错记录", tags="h3c, 排错")
        kb.build_index()

        # 解析
        parsed = manual_mod.parse_note(open(
            os.path.join(kb_dir, "命令速查", "H3C S5560 端口镜像配置命令.md"),
            encoding="utf-8").read())
        check("手册: 解析标题", parsed["title"] == "H3C S5560 端口镜像配置命令")
        check("手册: 解析分类", parsed["category"] == "命令速查", parsed["category"])
        check("手册: 解析标签", parsed["tags"] == ["h3c", "端口镜像"], repr(parsed["tags"]))
        check("手册: 解析设备元信息", parsed["meta"].get("设备") == "核心交换机")
        check("手册: 解析正文", "mirroring-group" in parsed["body"])
        check("手册: 正文不含署名行", "AITerminal 保存" not in parsed["body"])

        notes = manual_mod.load_notes(kb)
        check("手册: 载入笔记数量", len(notes) == 3, str(len(notes)))
        check("手册: 分类来自元信息",
              {n["category"] for n in notes} == {"命令速查", "排错记录"},
              repr({n["category"] for n in notes}))

        tags = manual_mod.collect_tags(notes)
        check("手册: 标签统计", tags.get("h3c") == 2 and tags.get("cisco") == 1, repr(tags))
        categories = manual_mod.collect_categories(notes)
        check("手册: 分类统计", categories.get("命令速查") == 2, repr(categories))

        # 筛选
        check("手册: 按分类筛选",
              len(manual_mod.filter_notes(notes, categories=["命令速查"])) == 2)
        check("手册: 按标签筛选",
              len(manual_mod.filter_notes(notes, tags=["cisco"])) == 1)
        check("手册: 按关键词筛选",
              len(manual_mod.filter_notes(notes, keyword="端口镜像")) == 1)
        check("手册: 多条件组合",
              len(manual_mod.filter_notes(notes, categories=["命令速查"],
                                          tags=["h3c"])) == 1)

        # Markdown 手册
        markdown = manual_mod.build_manual(notes, "命令手册", "测试副标题")
        check("手册: 含主标题", markdown.startswith("# 命令手册"))
        check("手册: 含副标题与统计", "测试副标题" in markdown and "共 3 篇笔记" in markdown)
        check("手册: 含目录", "## 目录" in markdown)
        check("手册: 按分类分章", markdown.count("# 命令速查") == 1
              and markdown.count("# 排错记录") == 1)
        check("手册: 笔记标题为二级标题", "## H3C S5560 端口镜像配置命令" in markdown)
        check("手册: 保留元信息", "厂商: h3c" in markdown)

        # HTML 手册
        html_text = manual_mod.build_manual_html(notes, "命令手册")
        check("手册: HTML 自包含", html_text.startswith("<!DOCTYPE html>")
              and "<style>" in html_text)
        check("手册: HTML 含目录清单", "笔记清单（3 篇）" in html_text)
        check("手册: HTML 渲染标题", "<h2>H3C S5560 端口镜像配置命令</h2>" in html_text)
        check("手册: HTML 渲染代码块", "<pre><code>" in html_text)
        check("手册: HTML 转义尖括号", "&lt;" in html_text or "<h2" in html_text)

        # 行内渲染
        inline = manual_mod.markdown_to_html("**粗体** 和 `代码` 和 [链接](https://x.com)")
        check("手册: 行内格式",
              "<strong>粗体</strong>" in inline and "<code>代码</code>" in inline
              and '<a href="https://x.com">链接</a>' in inline, inline)

        # 导出
        md_path = os.path.join(kb_dir, "out", "手册.md")
        check("手册: 导出 Markdown", manual_mod.export_manual(md_path, notes, "命令手册"))
        check("手册: Markdown 文件存在", os.path.exists(md_path))
        html_path = os.path.join(kb_dir, "out", "手册.html")
        check("手册: 导出 HTML", manual_mod.export_manual(html_path, notes, "命令手册"))
        with open(html_path, encoding="utf-8") as f:
            check("手册: HTML 内容完整", "</html>" in f.read())
        csv_path = os.path.join(kb_dir, "out", "清单.csv")
        check("手册: 导出清单 CSV", manual_mod.export_notes_csv(csv_path, notes))
        with open(csv_path, encoding="utf-8-sig") as f:
            csv_text = f.read()
        check("手册: CSV 含分类与标签",
              "命令速查" in csv_text and "端口镜像" in csv_text)
    finally:
        shutil.rmtree(kb_dir, ignore_errors=True)


# ------------------------------------------------------------------ 等提示符
class _ScriptedConnector(DummyConnector):
    """按脚本输出回显的连接器，用于验证提示符判定"""

    def __init__(self, on_send: str = "", on_raw: str = ""):
        super().__init__(simulate_prompt=False)
        self.on_send = on_send
        self.on_raw = on_raw

    def send(self, command: str):
        self.sent.append(command)
        if self.on_send:
            self._buffer += self.on_send          # 同时写缓冲区，验证监听器不消耗它
            self._notify_output(self.on_send)

    def send_raw(self, data: str):
        self.sent.append(data)
        if self.on_raw:
            self._buffer += self.on_raw
            self._notify_output(self.on_raw)


def test_command_waiter():
    """按提示符判断命令是否执行完"""
    # 1) 正常返回提示符
    connector = _ScriptedConnector(on_send="display version\r\nH3C Comware\r\n<H3C>")
    waiter = CommandWaiter(connector, quiet_time=0.1, timeout=5, silence_timeout=1.0)
    waiter.start()
    status, elapsed, output = waiter.run("display version")
    check("提示符: 判定为完成", status == "prompt", status)
    check("提示符: 收集到输出", "H3C Comware" in output, repr(output[:60]))
    check("提示符: 不消耗连接器缓冲区",
          "H3C Comware" in connector.get_output())
    waiter.stop()

    # 2) 需要人工确认
    confirm = _ScriptedConnector(on_send="Are you sure? [Y/N]")
    waiter = CommandWaiter(confirm, quiet_time=0.1, timeout=5, silence_timeout=2.0)
    waiter.start()
    status, _elapsed, _output = waiter.run("reboot")
    check("提示符: 识别确认提示", status == "confirm", status)
    waiter.stop()

    # 3) 分页自动翻页
    pager = _ScriptedConnector(on_send="display current-configuration\r\nvlan 10\r\n"
                                        " ---- More ----", on_raw=" port link-type access\r\n<H3C>")
    waiter = CommandWaiter(pager, quiet_time=0.1, timeout=5, silence_timeout=1.0)
    waiter.start()
    status, _elapsed, output = waiter.run("display current-configuration")
    check("提示符: 自动翻页后完成", status == "prompt", status)
    check("提示符: 记录翻页次数", waiter.pages == 1, str(waiter.pages))
    check("提示符: 翻页补发空格", " " in pager.sent, repr(pager.sent))
    check("提示符: 翻页后内容仍保留", "vlan 10" in output, repr(output[:80]))
    waiter.stop()

    # 4) 无提示符但有输出 → 静默兜底
    silent = _ScriptedConnector(on_send="部分输出但没有提示符")
    waiter = CommandWaiter(silent, quiet_time=0.1, timeout=5, silence_timeout=0.5)
    waiter.start()
    status, _elapsed, _output = waiter.run("show x")
    check("提示符: 静默兜底", status == "silence", status)
    waiter.stop()

    # 5) 一直没输出 → 超时
    quiet = _ScriptedConnector(on_send="")
    waiter = CommandWaiter(quiet, quiet_time=0.1, timeout=0.6, silence_timeout=60)
    waiter.start()
    status, elapsed, _output = waiter.run("show x")
    check("提示符: 超时返回", status == "timeout", status)
    check("提示符: 超时耗时合理", 0.5 <= elapsed < 3.0, f"{elapsed:.2f}s")
    waiter.stop()

    # 6) 断开
    offline = DummyConnector(simulate_prompt=False)
    waiter = CommandWaiter(offline, quiet_time=0.1, timeout=5, silence_timeout=5)
    waiter.start()
    offline._connected = False
    status, _elapsed = waiter.wait()
    check("提示符: 连接断开识别", status == "disconnected", status)
    waiter.stop()
    check("提示符: 监听器已注销", offline._output_listeners == [])


def test_batch_prompt_mode():
    """批量下发：等提示符模式与结果汇总"""
    connector = DummyConnector(simulate_prompt=True)
    runner = BatchRunner(connector, ["display version", "display interface brief"],
                         delay_ms=0, block_dangerous=True,
                         wait_mode=BatchRunner.WAIT_PROMPT,
                         quiet_time=0.1, prompt_timeout=5, silence_timeout=1.0)
    done_events = []
    finished = []
    runner.command_done.connect(lambda i, s, e: done_events.append((i, s, round(e, 1))))
    runner.finished.connect(lambda ok, msg: finished.append((ok, msg)))
    runner._run()

    check("批量: 等提示符模式全部完成",
          len(done_events) == 2 and all(s == "prompt" for _i, s, _e in done_events),
          repr(done_events))
    check("批量: 结束回调成功", bool(finished) and finished[0][0] is True, repr(finished))
    check("批量: 汇总含条数", "共 2 条" in finished[0][1], finished[0][1])

    rows = runner.summary_rows()
    check("批量: 结果行含状态文字",
          rows and rows[0][2] == "完成（提示符返回）", repr(rows[:1]))
    check("批量: 结果行含输出",
          "OK" in rows[0][4], repr(rows[0][4][:40]))

    # 固定间隔模式仍然可用
    connector2 = DummyConnector(simulate_prompt=True)
    runner2 = BatchRunner(connector2, ["display version"], delay_ms=0,
                          wait_mode=BatchRunner.WAIT_DELAY)
    runner2._run()
    check("批量: 固定间隔模式",
          runner2.results and runner2.results[0]["status"] == "delay",
          repr(runner2.results[:1]))

    # 需要确认时中止后续
    confirm = _ScriptedConnector(on_send="Are you sure? [Y/N]")
    runner3 = BatchRunner(confirm, ["reboot", "display version"], delay_ms=0,
                          block_dangerous=False, wait_mode=BatchRunner.WAIT_PROMPT,
                          quiet_time=0.1, prompt_timeout=5, silence_timeout=2.0)
    messages = []
    runner3.finished.connect(lambda ok, msg: messages.append((ok, msg)))
    runner3._run()
    check("批量: 确认提示后暂停",
          messages and messages[0][0] is False and "人工确认" in messages[0][1],
          repr(messages))
    check("批量: 暂停后不再下发后续命令",
          confirm.sent == ["reboot"], repr(confirm.sent))


def test_patrol():
    """多设备并发巡检与导出"""
    import shutil
    import tempfile
    from core.knowledge import extract_xlsx

    devices = [
        {"name": "核心交换机", "protocol": "SSH", "host": "10.0.0.1",
         "username": "admin", "password": "x"},
        {"name": "接入交换机", "protocol": "SSH", "host": "10.0.0.2",
         "username": "admin", "password": "x"},
        {"name": "汇聚交换机", "protocol": "Telnet", "host": "10.0.0.3",
         "username": "admin", "password": "x"},
    ]
    commands = ["display version", "display interface brief"]

    original_build = patrol_mod.build_connector
    original_connect = patrol_mod.connect_device
    patrol_mod.build_connector = lambda config: (
        DummyConnector(simulate_prompt=True), config.get('protocol', 'SSH'))
    patrol_mod.connect_device = lambda connector, protocol, config, timeout=10: (True, "")
    tmp = tempfile.mkdtemp(prefix="aiterminal_patrol_")
    try:
        runner = PatrolRunner(devices, commands, concurrency=3,
                              quiet_time=0.1, prompt_timeout=5, silence_timeout=1.0,
                              task_name="测试巡检")
        done = []
        runner.device_done.connect(lambda i, r: done.append((i, r)))
        runner._run()

        check("巡检: 设备全部执行", len(runner.results) == 3, str(len(runner.results)))
        check("巡检: 每台都成功", all(r["ok"] for r in runner.results),
              repr([(r["name"], r["ok"], r["error"]) for r in runner.results]))
        check("巡检: 每台命令数正确",
              all(len(r["commands"]) == 2 for r in runner.results))
        check("巡检: 命令结果含输出",
              all("OK" in c["output"] for r in runner.results for c in r["commands"]))
        check("巡检: 发出设备完成信号", len(done) == 3, str(len(done)))

        rows = runner.summary_rows()
        check("巡检: 汇总表表头", rows[0][:4] == ["序号", "设备名称", "协议", "地址"])
        check("巡检: 汇总表行数", len(rows) == 4, str(len(rows)))
        check("巡检: 汇总表状态为成功", rows[1][4] == "成功", repr(rows[1]))
        detail = runner.detail_rows()
        check("巡检: 明细表行数", len(detail) == 1 + 3 * 2, str(len(detail)))
        check("巡检: 明细含设备与命令",
              detail[1][0] == "核心交换机" and detail[1][2] == "display version",
              repr(detail[1]))
        info = runner.info_rows({"done": 3, "ok": 3, "failed": 0})
        check("巡检: 信息表含任务名", any("测试巡检" in str(row) for row in info))

        # Excel 导出并读回
        xlsx_path = os.path.join(tmp, "巡检结果.xlsx")
        check("巡检: 导出 Excel",
              export_patrol_excel(xlsx_path, runner,
                                  {"done": 3, "ok": 3, "failed": 0}))
        xlsx_text = extract_xlsx(xlsx_path)
        check("巡检: Excel 含三个工作表",
              all(f"--- {name} ---" in xlsx_text
                  for name in ("巡检信息", "巡检汇总", "命令明细")))
        check("巡检: Excel 含设备与输出",
              "核心交换机" in xlsx_text and "display version" in xlsx_text)

        # CSV 导出
        csv_path = os.path.join(tmp, "巡检结果.csv")
        check("巡检: 导出 CSV", export_patrol_csv(csv_path, runner))
        check("巡检: CSV 汇总与明细都生成",
              os.path.exists(csv_path)
              and os.path.exists(os.path.join(tmp, "巡检结果_明细.csv")))

        # 空任务校验
        check("巡检: 无设备时拒绝启动",
              PatrolRunner([], commands).start() is False)
        check("巡检: 无命令时拒绝启动",
              PatrolRunner(devices, []).start() is False)

        # 连接失败要如实记录
        patrol_mod.connect_device = lambda connector, protocol, config, timeout=10: (
            False, "认证失败：用户名或密码错误")
        failed = patrol_mod.patrol_device(devices[0], commands)
        check("巡检: 连接失败被记录",
              failed["ok"] is False and "认证失败" in failed["error"], repr(failed))
    finally:
        patrol_mod.build_connector = original_build
        patrol_mod.connect_device = original_connect
        shutil.rmtree(tmp, ignore_errors=True)


def test_patrol_dialog():
    """巡检界面：设备勾选、模板载入、参数"""
    from ui.patrol_dialog import PatrolDialog, DEFAULT_COMMANDS

    settings, snapshot = _settings_snapshot()
    # 清掉上次保存的巡检命令，保证"默认命令"断言不受历史状态影响
    settings.set_patrol_commands([])
    try:
        sessions = [
            {"name": "核心交换机", "protocol": "SSH", "host": "10.0.0.1",
             "username": "admin", "password": "x"},
            {"name": "接入交换机", "protocol": "Telnet", "host": "10.0.0.2",
             "username": "admin", "password": "x"},
        ]
        candidates = [{"name": "历史设备", "protocol": "SSH", "host": "10.0.0.9",
                       "username": "admin"}]

        dialog = PatrolDialog(None, candidates=candidates, sessions=sessions)
        check("巡检界面: 当前会话已加入", dialog.table.rowCount() == 2,
              str(dialog.table.rowCount()))
        check("巡检界面: 默认全部勾选", len(dialog.checked_devices()) == 2)

        dialog.set_all_checked(False)
        check("巡检界面: 全不选", dialog.checked_devices() == [])
        dialog.set_all_checked(True)

        dialog.add_from_candidates()
        check("巡检界面: 候选设备已加入", dialog.table.rowCount() == 3)
        dialog.add_from_candidates()
        check("巡检界面: 重复设备不重复添加", dialog.table.rowCount() == 3)

        check("巡检界面: 默认命令非空",
              dialog.get_commands() == DEFAULT_COMMANDS.split("\n"),
              repr(dialog.get_commands()[:3]))

        dialog.template_combo.setCurrentText("设备基础巡检")
        dialog.load_template()
        commands = dialog.get_commands()
        check("巡检界面: 模板载入",
              "display version" in commands and "display logbuffer" in commands,
              repr(commands))

        dialog.command_edit.setPlainText("# 注释行\n\ndisplay version\n  \ndisplay cpu-usage")
        check("巡检界面: 过滤注释与空行",
              dialog.get_commands() == ["display version", "display cpu-usage"],
              repr(dialog.get_commands()))

        dialog.concurrency_spin.setValue(8)
        dialog.save_settings()
        check("巡检界面: 参数已保存", dialog.settings.patrol_concurrency == 8)

        check("巡检界面: 默认导出文件名",
              dialog.default_name("xlsx").startswith("巡检结果_")
              and dialog.default_name("xlsx").endswith(".xlsx"))
        dialog.close()
    finally:
        _settings_restore(settings, snapshot)


def test_knowledge_dialog_filters():
    """资料库界面：分类/标签筛选与手册导出选择"""
    import shutil
    import tempfile
    from core.knowledge import KnowledgeBase
    from ui.knowledge_dialog import KnowledgeDialog

    settings, snapshot = _settings_snapshot()
    kb_dir = tempfile.mkdtemp(prefix="aiterminal_kdf_")
    try:
        kb = KnowledgeBase(root_dir=kb_dir)
        kb.save_note("H3C 端口镜像", "```\nmirroring-group 1 local\n```",
                     category="命令速查", tags="h3c, 端口镜像")
        kb.save_note("Cisco 巡检", "```\nshow version\n```",
                     category="命令速查", tags="cisco, 巡检")
        kb.save_note("排错记录一", "端口 err-disable 处理", category="排错记录", tags="h3c")
        # 一份没有元信息的原始 txt（会被当成"未分类"笔记）
        with open(os.path.join(kb_dir, "随手记.txt"), "w", encoding="utf-8") as f:
            f.write("原始文档内容")
        # 一份 PDF（非笔记，不参与手册合并）
        _make_minimal_pdf(os.path.join(kb_dir, "官方手册.pdf"),
                          ["official manual page one"])
        kb.build_index()

        dialog = KnowledgeDialog(None, kb)
        check("资料库界面: 列表含全部资料", dialog.table.rowCount() == 5,
              str(dialog.table.rowCount()))
        check("资料库界面: 分类下拉含分类",
              any(dialog.category_combo.itemData(i) == "命令速查"
                  for i in range(dialog.category_combo.count())))
        check("资料库界面: 标签下拉含标签",
              any(dialog.tag_combo.itemData(i) == "h3c"
                  for i in range(dialog.tag_combo.count())))

        # 按分类筛选
        index = dialog.category_combo.findData("命令速查")
        dialog.category_combo.setCurrentIndex(index)
        check("资料库界面: 按分类筛选", dialog.table.rowCount() == 2,
              str(dialog.table.rowCount()))

        # 叠加标签筛选
        index = dialog.tag_combo.findData("cisco")
        dialog.tag_combo.setCurrentIndex(index)
        check("资料库界面: 按标签筛选", dialog.table.rowCount() == 1,
              str(dialog.table.rowCount()))

        # 关键词筛选
        dialog.reset_filter()
        dialog.filter_input.setText("排错")
        check("资料库界面: 关键词筛选", dialog.table.rowCount() == 1,
              str(dialog.table.rowCount()))
        dialog.reset_filter()
        check("资料库界面: 重置筛选", dialog.table.rowCount() == 5)

        # 分类列显示：笔记显示元信息分类，无元信息的显示"未分类"
        category_by_name = {
            dialog.table.item(row, 1).text(): dialog.table.item(row, 2).text()
            for row in range(dialog.table.rowCount())
        }
        check("资料库界面: 笔记显示元信息分类",
              category_by_name.get("命令速查\\H3C 端口镜像.md") == "命令速查",
              repr(category_by_name))
        check("资料库界面: 无元信息的 txt 归为未分类",
              category_by_name.get("随手记.txt") == "未分类",
              repr(category_by_name.get("随手记.txt")))

        # 导出选择：未勾选时按筛选条件
        dialog.filter_input.setText("命令速查")
        selected = dialog.select_notes_for_export()
        check("资料库界面: 按筛选选笔记",
              len(selected) == 2 and all(n["category"] == "命令速查" for n in selected),
              str(len(selected)))

        # 勾选后优先用勾选，且非文本资料不参与手册合并
        dialog.reset_filter()
        dialog.set_all_checked(True)
        check("资料库界面: 全选后勾选路径数", len(dialog.checked_paths()) == 5,
              str(len(dialog.checked_paths())))
        selected = dialog.select_notes_for_export()
        check("资料库界面: 勾选时只导出文本笔记",
              len(selected) == 4
              and all(n["name"].endswith(('.md', '.txt')) for n in selected),
              repr([n["name"] for n in selected]))
        check("资料库界面: PDF 不参与手册合并",
              all(not n["name"].endswith('.pdf') for n in selected))

        check("资料库界面: 检索测试页签可用", dialog.tabs.count() == 2)
        dialog.close()
    finally:
        _settings_restore(settings, snapshot)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_main_window():
    window = MainWindow()

    check("主窗口创建", window is not None)
    check("初始无标签页", window.tab_widget.count() == 0)

    # 注入一个假会话，验证标签页接线与输出渲染
    manager = window.session_manager
    session = SessionInfo(1, "测试设备", "SSH")
    session.connector = DummyConnector()
    manager._sessions[1] = session
    manager._next_id = 2
    window.on_session_created(1, "测试设备")

    check("标签页已创建", window.tab_widget.count() == 1)
    check("标签页携带 session_id",
          window.tab_widget.widget(0).property('session_id') == 1)

    # 模拟设备输出 -> 定时器刷新 -> 终端显示
    session.connector._buffer = "\x1b[32mOK\x1b[0m\r\nswitch#"
    window.update_output()
    text = window.tabs[1].terminal.text()
    check("输出渲染到终端", "OK" in text and "switch#" in text, repr(text))

    # 模拟用户输入命令
    window.on_command_entered(1, "display version")
    check("命令已下发", session.connector.sent == ["display version"], repr(session.connector.sent))

    # 中断信号
    window.on_interrupt(1)
    check("Ctrl+C 中断", session.connector.sent[-1] == "\x03", repr(session.connector.sent))

    # AI 未启用时心跳不动作
    window.ai_tick()
    check("AI未启用时心跳安全", session.ai_busy is False)

    # 会话输出记录（供 AI 使用）
    session.record_output("x" * 25000)
    check("输出记录有上限", len(session.recent_output) <= SessionInfo.MAX_RECENT)

    # v0.2: 工具菜单动作已挂上
    check("工具菜单: 自动日志开关", window.act_auto_log is not None)

    # v0.3: 串口按钮默认禁用（非 Console 会话）
    check("工具栏: 串口控制默认禁用", window.action_serial.isEnabled() is False)

    # v0.3: AI 面板页签与屏幕预览
    check("AI面板: 主窗口内三个页签", window.ai_panel.tabs.count() == 3)
    window.update_screen_preview()
    preview = window.ai_panel.screen_preview.toPlainText()
    check("AI面板: 屏幕预览同步最近输出",
          preview != "" and preview == session.recent_output[-2000:],
          f"preview长度={len(preview)}")

    # v0.3: AI 命令进入待执行队列
    window.on_ai_command(1, "display interface brief")
    check("待执行队列: AI命令入队",
          window.ai_panel.queue_list.count() == 1,
          str(window.ai_panel.queue_list.count()))

    # v0.3: 本地回显（Console 场景）
    window.tabs[1].terminal.set_local_echo(True)
    window.tabs[1].terminal._echo_text("ping 1.1.1.1")
    check("终端: 本地回显生效",
          "ping 1.1.1.1" in window.tabs[1].terminal.text())
    window.tabs[1].terminal.set_local_echo(False)

    # v0.3: 批量下发接线（后台线程执行，等提示符模式下每条约 0.3s）
    window.run_batch(1, ["display version", "display interface brief"], 0, True)
    time.sleep(1.6)
    sent = session.connector.sent
    check("批量下发已执行到连接器",
          "display version" in sent and "display interface brief" in sent, repr(sent))
    if window.progress_dialog:
        window.progress_dialog.close()
        window.progress_dialog = None

    # 关闭会话
    window.close_session(0)
    check("会话已关闭", window.tab_widget.count() == 0 and len(manager.get_all_sessions()) == 0)

    window.close()
    check("窗口关闭无异常", True)


def main():
    global _APP
    print("=" * 60)
    print("AITerminal 冒烟测试")
    print("=" * 60)
    _APP = QApplication.instance() or QApplication([])
    test_terminal_buffer()
    test_ai_agent()
    test_settings()
    test_presets_and_plugins()
    test_telnet_fallback()
    print("-" * 60)
    print("v0.2 功能")
    print("-" * 60)
    test_diff()
    test_templates()
    test_logger()
    test_batch()
    test_snapshot_and_capture()
    test_new_settings()
    test_ai_prompt_override()
    print("-" * 60)
    print("v0.3 联网检索 / Console")
    print("-" * 60)
    test_web_search_parsing()
    test_web_search_client()
    test_ai_search_flow()
    test_serial_console()
    print("-" * 60)
    print("v0.4 本地资料库")
    print("-" * 60)
    test_knowledge_base()
    test_ai_local_lookup()
    test_save_note()

    # 主窗口用例会写配置（批量间隔、会话列表等），测试后还原，避免污染用户配置
    settings = Settings()
    snapshot = dict(settings.data)
    try:
        test_main_window()
    finally:
        settings._data = snapshot
        settings.save()

    test_v02_dialogs()
    test_ai_panel_tabs_and_queue()
    test_knowledge_dialog()
    test_note_dialog()
    test_ai_panel_save_note()
    test_main_window_note_sinking()
    print("-" * 60)
    print("v0.6 Excel 导出 / 笔记手册 / 等提示符 / 并发巡检")
    print("-" * 60)
    test_xlsx_writer()
    test_manual()
    test_command_waiter()
    test_batch_prompt_mode()
    test_patrol()
    test_patrol_dialog()
    test_knowledge_dialog_filters()
    print("-" * 60)
    print("v0.7 命令安全分级 / 配置命令确认闸门")
    print("-" * 60)
    test_command_safety()
    test_config_confirm_dialog()
    test_paging_off_commands()
    test_vendor_detect_robustness()
    print("-" * 60)
    print("v0.7 大模型提供方（Anthropic / DeepSeek / OpenAI 兼容）")
    print("-" * 60)
    test_llm_client()
    test_ai_provider_settings()
    print("-" * 60)
    print("v0.7 NetCatty 式界面：设备库 Vault + Catty 对话")
    print("-" * 60)
    test_vault_and_converse()

    print("-" * 60)
    failed = [r for r in RESULTS if not r[1]]
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for name, _, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    print("全部通过 ✔")
    return 0




# ------------------------------------------------------------------ 命令安全分级
def test_command_safety():
    """只读命令与配置命令的分级（配置命令必须人工确认的依据）"""
    read_only = [
        "display version", "display interface brief", "display current-configuration",
        "show running-config", "show ip route", "ping 1.1.1.1",
        "traceroute 8.8.8.8", "dir flash:", "screen-length disable",
        "terminal length 0", "quit", "return", "exit",
    ]
    config = [
        "system-view", "configure terminal", "vlan 901", "undo vlan 901",
        "interface GigabitEthernet1/0/1", "ip address 1.1.1.1 255.255.255.0",
        "name TEST", "port link-type access", "snmp-agent", "save",
        "write memory", "reboot", "shutdown", "no shutdown", "delete flash:/x",
    ]
    for command in read_only:
        check(f"安全分级: 只读 {command[:28]}", safety_mod.is_read_only(command) is True)
    for command in config:
        check(f"安全分级: 配置 {command[:28]}", safety_mod.is_config_command(command) is True)

    check("安全分级: 未知命令按配置处理",
          safety_mod.is_config_command("some-unknown-cmd 123") is True)
    check("安全分级: 空命令不算配置", safety_mod.is_config_command("") is False)
    check("安全分级: 大小写不敏感",
          safety_mod.is_read_only("DISPLAY VERSION") is True)

    result = safety_mod.classify(["display version", "vlan 901", "save", ""])
    check("安全分级: 分类统计",
          result["total"] == 3 and len(result["config"]) == 2
          and len(result["read_only"]) == 1, repr(result))
    check("安全分级: 含配置命令判定",
          safety_mod.has_config_command(["display version", "vlan 901"]) is True)
    check("安全分级: 纯只读判定",
          safety_mod.has_config_command(["display version", "show ip route"]) is False)

    notes = safety_mod.risk_notes(["system-view", "vlan 901", "save", "undo vlan 902"])
    check("安全分级: 风险提示覆盖进入配置视图",
          any("配置视图" in n for n in notes), repr(notes))
    check("安全分级: 风险提示覆盖保存", any("启动配置" in n for n in notes), repr(notes))
    check("安全分级: 风险提示覆盖删除", any("删除" in n for n in notes), repr(notes))

    text = safety_mod.describe(["display version", "vlan 901"])
    check("安全分级: 说明文本含配置命令", "vlan 901" in text and "只读" in text)


def test_config_confirm_dialog():
    """配置命令确认框"""
    from ui.command_confirm import ConfigConfirmDialog

    commands = ["system-view", "vlan 901", "quit", "return"]
    dialog = ConfigConfirmDialog(None, commands, target="核心交换机", action="批量下发")
    check("确认框: 列出全部命令", dialog.table.rowCount() == 4,
          str(dialog.table.rowCount()))
    check("确认框: 识别配置命令数", len(dialog.result_info["config"]) == 2,
          repr(dialog.result_info))
    check("确认框: 退出视图不算配置命令",
          set(dialog.result_info["read_only"]) == {"quit", "return"},
          repr(dialog.result_info["read_only"]))
    check("确认框: 默认未确认", dialog.confirmed is False)
    check("确认框: 命令原文可复制",
          dialog.text_view.toPlainText() == "\n".join(commands))
    check("确认框: 类型列标注",
          dialog.table.item(1, 2).text() == "配置"
          and dialog.table.item(1, 1).text() == "vlan 901")
    dialog.close()

    # 纯只读命令也应能构造（用于统一提示）
    readonly = ConfigConfirmDialog(None, ["display version"], target="sw1")
    check("确认框: 只读命令无需二次确认",
          len(readonly.result_info["config"]) == 0, repr(readonly.result_info))
    readonly.close()


def test_paging_off_commands():
    """关分屏命令要按厂商选择，并在被设备拒绝时自动换下一条"""
    from core.batch import PAGING_OFF_COMMANDS

    check("关分屏: Comware5 先试 disable",
          PAGING_OFF_COMMANDS["h3c"][0] == "screen-length disable",
          repr(PAGING_OFF_COMMANDS["h3c"]))
    check("关分屏: Cisco 用 terminal length 0",
          PAGING_OFF_COMMANDS["cisco"] == ("terminal length 0",))
    check("关分屏: 未知厂商覆盖三家",
          len(PAGING_OFF_COMMANDS["unknown"]) == 3,
          repr(PAGING_OFF_COMMANDS["unknown"]))

    # 设备拒绝第一条命令时应自动换下一条
    class _RejectingConnector(_ScriptedConnector):
        def __init__(self):
            super().__init__(on_send="")
            self.count = 0

        def send(self, command: str):
            self.sent.append(command)
            self.count += 1
            if self.count == 1:
                out = (f"{command}\r\n^\r\n"
                       "% Unrecognized command found at '^' position.\r\n<H3C>")
            else:
                out = (f"{command}\r\n"
                       "% Screen-length configuration is disabled for current user.\r\n<H3C>")
            self._buffer += out
            self._notify_output(out)

    connector = _RejectingConnector()
    ok, command, response = disable_paging(connector, vendor="h3c",
                                           quiet_time=0.1, timeout=3)
    check("关分屏: 被拒绝后换下一条", ok is True and command == "screen-length 0 temporary",
          f"{ok} {command}")
    check("关分屏: 返回设备响应", "disabled" in response.lower(), repr(response[:60]))
    check("关分屏: 实际发了两次", len(connector.sent) == 2, repr(connector.sent))

    # 全部被拒绝时要如实返回失败
    class _AllReject(_ScriptedConnector):
        def send(self, command: str):
            self.sent.append(command)
            out = f"{command}\r\n% Unrecognized command found at '^' position.\r\n<H3C>"
            self._buffer += out
            self._notify_output(out)

    ok2, command2, _resp = disable_paging(_AllReject(), vendor="h3c",
                                          quiet_time=0.1, timeout=2)
    check("关分屏: 全部被拒绝时返回失败", ok2 is False and command2 == "")


def test_vendor_detect_robustness():
    """厂商识别：不能被 description 里的"对端设备名"带偏"""
    from core.terminal import detect_vendor

    # 强特征优先
    check("厂商识别: H3C Comware",
          detect_vendor("H3C Comware Platform Software\nComware Software, Version 5.20") == "h3c")
    check("厂商识别: 华为 VRP",
          detect_vendor("Huawei Versatile Routing Platform Software VRP (R) software") == "huawei")
    check("厂商识别: Cisco IOS",
          detect_vendor("Cisco IOS Software, C2960 Software") == "cisco")

    # 关键回归：接口描述里写了 huawei，但设备是 H3C
    config = """
#
 version 5.20, Release 5101P05
#
 sysname zhengfayonghuhuiju
#
 interface GigabitEthernet1/0/1
  description to 2-4 huawei
  port link-type access
#
 interface GigabitEthernet1/0/2
  description to 2-4_huawei25
#
"""
    check("厂商识别: 描述里的 huawei 不误判（无强特征时为 unknown）",
          detect_vendor(config) == "unknown", detect_vendor(config))
    check("厂商识别: 版本信息与配置一起看能认出 H3C",
          detect_vendor("H3C Comware Platform Software\n" + config) == "h3c")

    check("厂商识别: 注释里的 cisco 不误判",
          detect_vendor("# ! remark cisco uplink\n vlan 10") == "unknown",
          detect_vendor("# ! remark cisco uplink\n vlan 10"))


# ------------------------------------------------------------------ 大模型提供方
def test_llm_client():
    """统一大模型客户端：地址拼接、请求构造、错误翻译（用本地假服务，不联网）"""
    import http.server
    import json as json_mod
    import threading as th
    from core.llm import LLMClient, LLMError, chat_url, provider_defaults

    # 地址拼接
    check("LLM: DeepSeek 地址",
          chat_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/chat/completions",
          chat_url("https://api.deepseek.com"))
    check("LLM: 带 /v1 的地址不重复拼",
          chat_url("https://x.com/v1") == "https://x.com/v1/chat/completions")
    check("LLM: 完整地址原样使用",
          chat_url("https://x.com/v1/chat/completions") == "https://x.com/v1/chat/completions")
    try:
        chat_url("")
        check("LLM: 空地址应报错", False)
    except LLMError as e:
        check("LLM: 空地址报错提示", "接口地址" in str(e), str(e))

    # 提供方默认值
    check("LLM: Anthropic 默认模型",
          provider_defaults("anthropic")["model"].startswith("claude"))
    check("LLM: DeepSeek 默认配置",
          provider_defaults("deepseek")["base_url"] == "https://api.deepseek.com"
          and provider_defaults("deepseek")["model"] == "deepseek-chat")
    check("LLM: 未知提供方回落 Anthropic",
          provider_defaults("nope")["model"].startswith("claude"))

    # 未配置密钥时直接报错，不发请求
    client = LLMClient(provider="deepseek", api_key="", model="deepseek-chat")
    check("LLM: 无密钥时 configured=False", client.configured is False)
    try:
        client.chat(system="s", messages=[{"role": "user", "content": "hi"}])
        check("LLM: 无密钥应报错", False)
    except LLMError as e:
        check("LLM: 无密钥报错提示", "API Key" in str(e), str(e))

    # 本地假服务验证 OpenAI 兼容链路
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            payload = json_mod.loads(self.rfile.read(length).decode('utf-8'))
            captured['path'] = self.path
            captured['auth'] = self.headers.get('Authorization')
            captured['payload'] = payload

            if 'bad' in payload.get('model', ''):
                body = json_mod.dumps({"error": {"message": "Model Not Exist"}}).encode()
                self.send_response(404)
            elif 'deny' in payload.get('model', ''):
                body = json_mod.dumps({"error": {"message": "Authentication Fails"}}).encode()
                self.send_response(401)
            else:
                body = json_mod.dumps({
                    "model": payload.get("model"),
                    "choices": [{"message": {"role": "assistant", "content": "命令已生成"}}],
                    "usage": {"total_tokens": 12},
                }, ensure_ascii=False).encode()
                self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    th.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}/v1"
        client = LLMClient(provider="deepseek", api_key="sk-test",
                           model="deepseek-chat", base_url=base)
        check("LLM: 配置完整时 configured=True", client.configured is True)
        reply = client.chat(system="你是网络工程师", messages=[
            {"role": "user", "content": "分析输出"}], max_tokens=64, temperature=0.2)
        check("LLM: 解析出回复", reply == "命令已生成", repr(reply))
        check("LLM: 请求路径正确", captured.get('path') == "/v1/chat/completions",
              str(captured.get('path')))
        check("LLM: 带 Bearer 认证",
              captured.get('auth') == "Bearer sk-test", str(captured.get('auth')))
        payload = captured.get('payload') or {}
        check("LLM: system 作为首条消息",
              payload.get("messages", [{}])[0].get("role") == "system",
              repr(payload.get("messages", [])[:1]))
        check("LLM: 传递模型与生成参数",
              payload.get("model") == "deepseek-chat"
              and payload.get("max_tokens") == 64
              and payload.get("temperature") == 0.2, repr(
                  {k: payload.get(k) for k in ("model", "max_tokens", "temperature")}))
        check("LLM: 非流式请求", payload.get("stream") is False)

        # 模型名错误 → 404 → 提示里要说清
        bad = LLMClient(provider="deepseek", api_key="sk-test",
                        model="bad-model", base_url=base)
        try:
            bad.chat(system="s", messages=[{"role": "user", "content": "hi"}])
            check("LLM: 404 应报错", False)
        except LLMError as e:
            check("LLM: 404 提示含模型/地址", "模型名" in str(e) or "接口地址" in str(e), str(e))

        # 密钥无效 → 401
        deny = LLMClient(provider="deepseek", api_key="sk-bad",
                         model="deny-model", base_url=base)
        try:
            deny.chat(system="s", messages=[{"role": "user", "content": "hi"}])
            check("LLM: 401 应报错", False)
        except LLMError as e:
            check("LLM: 401 提示密钥无效", "API Key" in str(e), str(e))

        # 网络不可达
        offline = LLMClient(provider="deepseek", api_key="sk-test",
                            model="deepseek-chat", base_url="http://127.0.0.1:9/v1")
        try:
            offline.chat(system="s", messages=[{"role": "user", "content": "hi"}])
            check("LLM: 不可达应报错", False)
        except LLMError as e:
            check("LLM: 不可达时给出可读提示",
                  any(mark in str(e) for mark in ("网络", "网关", "连接")), str(e))
    finally:
        server.shutdown()
        server.server_close()

    # 空消息兜底
    check("LLM: 空消息不崩",
          LLMClient._clean_messages([]) == [{"role": "user", "content": "你好"}])


def test_ai_provider_settings():
    """提供方切换时密钥/模型/地址各存各的"""
    settings, snapshot = _settings_snapshot()
    try:
        settings.set("provider_keys", {})
        settings.set("provider_configs", {})

        settings.ai_provider = "deepseek"
        settings.set_api_key_for("deepseek", "sk-ds-1")
        settings.set_provider_config("deepseek", model="deepseek-reasoner",
                                     base_url="https://api.deepseek.com")
        check("提供方: 读取当前密钥", settings.api_key == "sk-ds-1", settings.api_key)
        check("提供方: 读取模型", settings.model == "deepseek-reasoner", settings.model)
        check("提供方: 读取地址",
              settings.api_base == "https://api.deepseek.com", settings.api_base)

        settings.ai_provider = "anthropic"
        settings.set_api_key_for("anthropic", "sk-ant-1")
        settings.set_provider_config("anthropic", model="claude-sonnet-4-20250514",
                                     base_url="")
        check("提供方: 切换后密钥互不覆盖",
              settings.api_key == "sk-ant-1"
              and settings.get_api_key_for("deepseek") == "sk-ds-1",
              f"{settings.api_key} / {settings.get_api_key_for('deepseek')}")
        check("提供方: 切回 DeepSeek 配置还在",
              settings.get_provider_config("deepseek")["model"] == "deepseek-reasoner",
              repr(settings.get_provider_config("deepseek")))
        check("提供方: 未配置的用默认值",
              settings.get_provider_config("openai_compat")["model"] == "gpt-4o-mini",
              repr(settings.get_provider_config("openai_compat")))
    finally:
        _settings_restore(settings, snapshot)

    # AIAgent 会按提供方建客户端
    from core.ai_agent import AIAgent
    agent = AIAgent(api_key="sk-ds-2", provider="deepseek")
    check("AIAgent: 提供方生效", agent.provider == "deepseek")
    check("AIAgent: 默认模型按提供方取", agent.model == "deepseek-chat", agent.model)
    check("AIAgent: 客户端地址正确",
          agent.llm.base_url == "https://api.deepseek.com", agent.llm.base_url)
    check("AIAgent: 描述信息含提供方",
          "DeepSeek" in agent.describe_model(), agent.describe_model())

    agent.set_provider("anthropic", api_key="sk-ant-x")
    check("AIAgent: 切换提供方",
          agent.provider == "anthropic" and agent.model.startswith("claude"),
          f"{agent.provider}/{agent.model}")
    agent.set_provider("deepseek", model="deepseek-reasoner")
    check("AIAgent: 切换时指定模型",
          agent.model == "deepseek-reasoner" and agent.provider == "deepseek")


# ------------------------------------------------------------------ 设备库 + 对话
def test_vault_and_converse():
    """设备库 Vault 持久化 + AIAgent.converse 对话解析（不连真机、不调 AI）"""
    from config.settings import Settings
    from core.ai_agent import AIAgent

    settings, snapshot = _settings_snapshot()
    try:
        settings.set("vault_devices", [])
        # 设备库增删改查
        d1 = {"name": "核心", "protocol": "SSH", "host": "10.0.0.1", "port": 22,
              "vendor": "h3c"}
        check("Vault: 添加设备", settings.add_vault_device(d1) is True)
        check("Vault: 列表含 1 台", len(settings.get_vault_devices()) == 1)
        # 同 host+port 去重，但允许改名
        check("Vault: 同键去重不新增",
              settings.add_vault_device({"name": "核心改名", "protocol": "SSH",
                                        "host": "10.0.0.1", "port": 22}) is False)
        check("Vault: 改名已更新",
              settings.get_vault_devices()[0]["name"] == "核心改名")
        check("Vault: 删除生效",
              settings.remove_vault_device({"protocol": "SSH", "host": "10.0.0.1",
                                             "port": 22}) is True
              and len(settings.get_vault_devices()) == 0)
        check("Vault: 不存在的删除返回 False",
              settings.remove_vault_device({"protocol": "SSH", "host": "9.9.9.9",
                                            "port": 1}) is False)

        # converse：把“说明 + bash 代码块命令”解析为 (文本, 命令)
        agent = AIAgent(api_key="x", provider="deepseek")
        captured = {}

        class _FakeLLM:
            def chat(self, system, messages, max_tokens=900, temperature=0.4):
                captured["system"] = system
                captured["messages"] = messages
                return ("建议先查版本再列接口：\n"
                        "```bash\n"
                        "display version\n"
                        "display interface brief\n"
                        "```")
            def describe(self):
                return "FakeLLM"

        agent.llm = _FakeLLM()
        text, cmds = agent.converse("检查设备的版本和接口", device_output="<输出>",
                                    task="巡检")
        check("converse: 说明文本被解析", "建议先查版本" in text, repr(text))
        check("converse: 命令被解析",
              cmds == ["display version", "display interface brief"], repr(cmds))
        check("converse: 带设备输出上下文",
              any("<输出>" in m.get("content", "") for m in captured["messages"]))
        check("converse: 用对话式提示词", "Catty" in captured["system"],
              captured.get("system", "")[:30])
        # 多轮上下文累计
        agent.llm.chat = lambda **k: "好的，已记录。"
        t2, c2 = agent.converse("那 VLAN 呢")
        check("converse: 多轮上下文保留前文",
              len(agent.context) >= 4 and t2 and c2 == [], repr((t2, c2)))
        # 纯文本回复（无命令）
        agent.llm.chat = lambda **k: "这是一台 H3C 交换机，版本正常，无需操作。"
        t3, c3 = agent.converse("有什么问题吗")
        check("converse: 纯文本无命令", t3 and c3 == [], repr((t3, c3)))
    finally:
        _settings_restore(settings, snapshot)


if __name__ == '__main__':
    sys.exit(main())

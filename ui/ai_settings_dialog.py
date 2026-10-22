"""
AI 设置对话框 - 提供方 / 密钥 / 接口地址 / 模型 与连通性测试

原先是侧栏「AI 助手」页里的「AI配置」分组，现在改从「工具 → AI 设置…」打开：
侧栏只留一行"当前：提供方 / 模型"提示，真正要改配置时才弹这个对话框。
"""
import os
import subprocess
import sys
import threading

from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QLineEdit, QPushButton, QComboBox, QWidget, QCheckBox, QMessageBox
)
from PyQt6.QtCore import pyqtSignal

from core.llm import LLMClient, LLMError, PROVIDERS, provider_defaults, provider_label
from core import app_paths, local_model
from config.settings import Settings

#: AI 提供方的展示顺序。**从 PROVIDERS 派生**，不要写死 ——
#: 写死的后果：新加一个提供方（如 longcat）后，设置界面里根本看不到它，
#: 用户会以为"这个 key 不能用"，而代码其实早就支持了。
PROVIDER_ORDER = tuple(PROVIDERS.keys())


class AISettingsDialog(QDialog):
    """
    AI 设置对话框

    信号:
        saved():                      配置已写入并保存
        connection_tested(bool, str): 测试连接结果（是否成功, 说明文字）
    """

    saved = pyqtSignal()
    connection_tested = pyqtSignal(bool, str)
    #: 外部接管开关变了（主窗口据此真起停回环控制服务）
    control_toggled = pyqtSignal(bool)
    #: 本地模型状态探测结果（`core.local_model.decide()` 的返回值）。
    #: 走信号是因为探测要起子进程（秒级），**不能**在 UI 线程里同步等。
    local_checked = pyqtSignal(object)

    def __init__(self, parent=None, control=None):
        """control: 主窗口，提供 control_state / set_control_enabled / reset_control_token"""
        super().__init__(parent)
        self.settings = Settings()
        self.control = control
        # {提供方: {"api_key", "model", "base_url"}} —— 对话框里的改动先放草稿，
        # 点"保存"才落盘，点"取消"不影响已有配置
        self._drafts = {}
        self._current_provider = self.settings.ai_provider
        self.setWindowTitle("AI 设置")
        self.setMinimumWidth(460)
        self.init_ui()
        self.load_settings()

    # ------------------------------------------------------------ 外部接管
    # 让本机智能体（DSH / MCP 客户端）驱动**正在运行的**界面与它的终端会话：
    # 开会话、下发命令读回显、查本地资料库。默认关闭，只绑回环，令牌可重置。
    def control_state(self) -> dict:
        if self.control is None:
            return {"enabled": False, "url": "", "token": ""}
        try:
            return self.control.control_state()
        except Exception:
            return {"enabled": False, "url": "", "token": ""}

    def refresh_control_status(self):
        state = self.control_state()
        if state["enabled"]:
            self.control_status.setText(
                f"已开启 · {state['url']} · 令牌 {state['token'][:8]}…\n"
                "本机的 MCP / DSH 客户端读 ~/.aiterminal/control.json 即可接入；"
                "令牌就是全部凭据，不要外发。")
        else:
            self.control_status.setText("未开启（默认关闭，不影响其它功能）")
        self.btn_copy_token.setEnabled(state["enabled"])
        self.btn_reset_token.setEnabled(state["enabled"])

    def on_control_toggled(self, checked: bool):
        """开关外部接管：落盘 + 真起停服务；起不来就把开关回滚"""
        self.settings.set("control_api_enabled", bool(checked))
        self.settings.save()
        ok = True
        if self.control is not None:
            try:
                ok = self.control.set_control_enabled(checked, remember=False)
            except Exception as e:
                ok = False
                QMessageBox.warning(self, "外部接管", f"{type(e).__name__}: {e}")
        if checked and not ok:
            self.chk_control.blockSignals(True)
            self.chk_control.setChecked(False)
            self.chk_control.blockSignals(False)
            self.settings.set("control_api_enabled", False)
            self.settings.save()
            checked = False
        self.refresh_control_status()
        self.control_toggled.emit(checked)

    def on_copy_token(self):
        state = self.control_state()
        if not state["token"]:
            return
        QApplication.clipboard().setText(f"{state['url']}\n{state['token']}")
        self.control_status.setText("已复制：地址 + 令牌")

    def on_reset_token(self):
        if self.control is None:
            return
        token = self.control.reset_control_token()
        self.control_status.setText(
            f"令牌已重置（旧令牌立刻失效）· 新令牌 {token[:8]}…" if token
            else "服务没在跑，先打开上面的开关")

    # ------------------------------------------------------------ 界面
    def init_ui(self):
        layout = QVBoxLayout(self)

        group = QGroupBox("AI配置")
        api_layout = QVBoxLayout()

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("提供方:"))
        self.provider_combo = QComboBox()
        for name in PROVIDER_ORDER:
            self.provider_combo.addItem(provider_label(name), name)
        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)
        provider_row.addWidget(self.provider_combo, 1)
        api_layout.addLayout(provider_row)

        api_layout.addWidget(QLabel("API Key:"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        api_layout.addWidget(self.api_key_input)

        # 接口地址（仅 OpenAI 兼容协议需要）
        self.base_row = QWidget()
        base_layout = QHBoxLayout(self.base_row)
        base_layout.setContentsMargins(0, 0, 0, 0)
        base_layout.addWidget(QLabel("接口地址:"))
        self.base_url_input = QLineEdit()
        base_layout.addWidget(self.base_url_input, 1)
        api_layout.addWidget(self.base_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("模型:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        model_row.addWidget(self.model_combo, 1)
        api_layout.addLayout(model_row)

        # 测试连接：用当前填写的内容（不必先保存）发一次最小请求
        test_row = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        self.btn_test.setToolTip(
            "用当前填写的内容（不必先保存）发一次最小请求，\n"
            "验证 API Key / 接口地址 / 模型是否可用")
        self.btn_test.clicked.connect(self.on_test_connection)
        test_row.addWidget(self.btn_test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setStyleSheet("color: #93A0B4; font-size: 11px;")
        test_row.addWidget(self.test_result, 1)
        api_layout.addLayout(test_row)
        self.connection_tested.connect(self._on_test_finished)

        self.provider_hint = QLabel("")
        self.provider_hint.setWordWrap(True)
        self.provider_hint.setStyleSheet("color: #93A0B4; font-size: 11px;")
        api_layout.addWidget(self.provider_hint)

        # 本地模型（provider=local）专属：档位标注 + 起停按钮。
        # 只在选中 local 时出现 —— 别的提供方看到这行只会困惑。
        self._local_running = False
        self.local_row = QWidget()
        local_layout = QVBoxLayout(self.local_row)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_btns = QHBoxLayout()
        self.btn_local = QPushButton("启动本地模型")
        self.btn_local.clicked.connect(self.on_local_button)
        local_btns.addWidget(self.btn_local)
        self.btn_local_refresh = QPushButton("重新探测")
        self.btn_local_refresh.clicked.connect(self.refresh_local_status)
        local_btns.addWidget(self.btn_local_refresh)
        local_btns.addStretch()
        local_layout.addLayout(local_btns)
        self.local_status = QLabel("")
        self.local_status.setWordWrap(True)
        self.local_status.setStyleSheet("color: #93A0B4; font-size: 11px;")
        local_layout.addWidget(self.local_status)
        # 档位选择：默认自动（官方 4B）；ait = 2026-10-22 自训模型。
        # 只在"启动本地模型"时通过 --tier 传下去，改完要重启本地服务才生效。
        tier_row = QHBoxLayout()
        tier_row.addWidget(QLabel("档位："))
        self.local_tier_combo = QComboBox()
        self.local_tier_combo.addItem("自动（官方 Qwen3-4B）", "")
        self.local_tier_combo.addItem("ait（自训 Qwen3-4B）", "ait")
        _cur_tier = str(self.settings.get("local_tier") or "")
        _idx = self.local_tier_combo.findData(_cur_tier)
        self.local_tier_combo.setCurrentIndex(_idx if _idx >= 0 else 0)
        self.local_tier_combo.setToolTip(
            "「启动本地模型」时传给 run_server.py 的档位。\n"
            "ait = 自训模型（56 题带检索严格命中 48.2%，官方 32.1%）；\n"
            "英文手册类问题自训模型略差，所以默认仍是自动（官方）。\n"
            "改完需要停一次再启动本地服务才生效。")
        tier_row.addWidget(self.local_tier_combo, 1)
        local_layout.addLayout(tier_row)
        api_layout.addWidget(self.local_row)
        self.local_row.setVisible(False)
        self.local_checked.connect(self._on_local_checked)

        group.setLayout(api_layout)
        layout.addWidget(group)

        # 外部接管（DSH / MCP）：默认关闭；只绑回环，令牌可随时重置
        control_group = QGroupBox("外部接管（DSH / 智能体）")
        control_layout = QVBoxLayout()
        self.chk_control = QCheckBox("允许本机智能体接管（仅回环 127.0.0.1 + 令牌）")
        self.chk_control.setToolTip(
            "打开后，本机上的 DSH / MCP 客户端可以用令牌驱动这个界面：\n"
            "开设备会话（SSH / Telnet / 串口）、下发命令并读回显、查本地资料库。\n"
            "服务只绑 127.0.0.1，外网连不上；下发的每条命令都记进审计日志。")
        self.chk_control.toggled.connect(self.on_control_toggled)
        control_layout.addWidget(self.chk_control)

        control_row = QHBoxLayout()
        self.control_status = QLabel("")
        self.control_status.setWordWrap(True)
        self.control_status.setStyleSheet("color: #93A0B4; font-size: 11px;")
        control_row.addWidget(self.control_status, 1)
        self.btn_copy_token = QPushButton("复制地址与令牌")
        self.btn_copy_token.clicked.connect(self.on_copy_token)
        control_row.addWidget(self.btn_copy_token)
        self.btn_reset_token = QPushButton("重置令牌")
        self.btn_reset_token.setToolTip(
            "重置后旧令牌立刻失效，已连上的外部客户端要重新读控制文件")
        self.btn_reset_token.clicked.connect(self.on_reset_token)
        control_row.addWidget(self.btn_reset_token)
        control_layout.addLayout(control_row)
        control_group.setLayout(control_layout)
        layout.addWidget(control_group)

        # 按真实状态初始化开关（屏蔽信号，别一打开对话框就触发启停）
        self.chk_control.blockSignals(True)
        self.chk_control.setChecked(self.control_state()["enabled"])
        self.chk_control.blockSignals(False)
        self.refresh_control_status()

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_save = QPushButton("保存")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self.on_save)
        btn_row.addWidget(btn_save)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------ 取值
    def get_provider(self) -> str:
        return self._current_provider or "anthropic"

    def get_api_key(self) -> str:
        """获取API Key"""
        return self.api_key_input.text().strip()

    def get_base_url(self) -> str:
        return self.base_url_input.text().strip()

    def get_model(self) -> str:
        """获取模型名（留空则用提供方默认）"""
        return (self.model_combo.currentText().strip()
                or provider_defaults(self.get_provider())["model"])

    # ------------------------------------------------------------ 配置
    def load_settings(self):
        """按配置里的提供方填好各字段"""
        provider = self.settings.ai_provider
        index = self.provider_combo.findData(provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)
        self._current_provider = self.provider_combo.currentData() or "anthropic"
        self.apply_provider_fields(self._current_provider)

    def provider_draft(self, provider: str) -> dict:
        """取某提供方的草稿值（第一次用到时从配置里读）"""
        provider = (provider or "anthropic").lower()
        if provider not in self._drafts:
            config = self.settings.get_provider_config(provider)
            self._drafts[provider] = {
                "api_key": self.settings.get_api_key_for(provider),
                "model": config["model"],
                "base_url": config["base_url"],
            }
        return self._drafts[provider]

    def stash_current(self) -> dict:
        """把界面上正在编辑的值存回草稿，切换提供方时不会丢"""
        provider = self.get_provider()
        self._drafts[provider] = {
            "api_key": self.get_api_key(),
            "model": self.model_combo.currentText().strip(),
            "base_url": self.get_base_url(),
        }
        return self._drafts[provider]

    def apply_provider_fields(self, provider: str):
        """把某提供方的密钥 / 模型 / 接口地址填到界面上"""
        provider = (provider or "anthropic").lower()
        defaults = provider_defaults(provider)
        draft = self.provider_draft(provider)

        self.api_key_input.setText(
            draft["api_key"] or (defaults.get("default_key", "") if defaults.get("no_key") else ""))
        self.api_key_input.setPlaceholderText(defaults.get("key_hint", ""))

        self.base_url_input.setText(draft["base_url"])
        self.base_row.setVisible(provider != "anthropic")

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        models = list(defaults.get("models") or [])
        if draft["model"] and draft["model"] not in models:
            models.insert(0, draft["model"])
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(draft["model"])
        self.model_combo.blockSignals(False)

        hints = {
            "anthropic": "Anthropic 官方接口，需要 sk-ant- 开头的密钥。",
            "deepseek": "DeepSeek 官方接口（OpenAI 兼容）。密钥在 platform.deepseek.com 申请；"
                        "deepseek-chat 通用，deepseek-reasoner 推理更强但更慢。",
            "openai_compat": "任何 OpenAI 兼容服务：vLLM / Ollama / 中转站等，"
                             "填服务商的接口地址（一般以 /v1 结尾）。",
            "local": "本机离线模型（llama.cpp）：**不联网、不用买密钥**，用哪一档由本机"
                     "内存/显存自动判定。第一次用请点下面「启动本地模型」。"
                     "内存/显存不够时会自动降级为「仅本地检索」，那是设计行为，不是故障。",
        }
        self.provider_hint.setText(hints.get(provider, ""))

        # 本地模型是唯一"没有云端可测"的提供方：走自己的档位标注 + 起停按钮
        is_local = (provider == "local")
        self.local_row.setVisible(is_local)
        if is_local:
            self.refresh_local_status()

    def on_provider_changed(self):
        """切换提供方：先把当前填写的内容存进草稿，再载入新一家的"""
        self.stash_current()
        provider = self.provider_combo.currentData() or "anthropic"
        self._current_provider = provider
        self.apply_provider_fields(provider)

    def on_save(self):
        """按原来的键写入配置并落盘，然后通知外面刷新"""
        self.stash_current()
        self.settings.ai_provider = self.get_provider()
        self.settings.set("local_tier", self.local_tier_combo.currentData() or "")
        for name, draft in self._drafts.items():
            self.settings.set_api_key_for(name, draft["api_key"])
            self.settings.set_provider_config(
                name, model=draft["model"], base_url=draft["base_url"])
        self.settings.save()
        self.saved.emit()
        self.accept()

    # ------------------------------------------------------------ 测试连接
    def on_test_connection(self):
        """
        用**当前输入框里的值**（不必先保存）发一次最小请求验证连通性。

        注意：故意不用已保存的值 —— 用户改了 Key 还没保存就点测试，
        测旧值等于白测。也不阻断保存：网络临时不通时照样能存 Key。
        """
        provider = self.get_provider()
        api_key = self.get_api_key()
        model = self.get_model()
        base_url = self.get_base_url()

        if not api_key and not provider_defaults(provider).get("no_key"):
            self._set_test_result("请先填写 API Key", ok=False)
            return
        if provider != "anthropic" and not base_url:
            self._set_test_result("请先填写接口地址", ok=False)
            return

        self.btn_test.setEnabled(False)
        self._set_test_result("测试中…", ok=None)
        threading.Thread(
            target=self._test_worker,
            args=(provider, api_key, model, base_url),
            daemon=True,
        ).start()

    def _test_worker(self, provider: str, api_key: str, model: str, base_url: str):
        """后台线程里跑网络请求，结果通过信号回主线程（不能直接碰控件）"""
        try:
            client = LLMClient(provider=provider, api_key=api_key,
                               model=model, base_url=base_url)
            client.chat(
                "You are a connectivity probe. Reply with exactly: ok",
                [{"role": "user", "content": "ping"}],
                max_tokens=8,      # 最小请求，成本可忽略
            )
            self.connection_tested.emit(True, f"已连通 · {model}")
        except LLMError as e:
            # LLMError 的文案已经是翻译过的人话（401/网络不可达等）
            self.connection_tested.emit(False, str(e))
        except Exception as e:
            self.connection_tested.emit(False, f"{type(e).__name__}: {e}")

    def _on_test_finished(self, ok: bool, message: str):
        self.btn_test.setEnabled(True)
        self._set_test_result(message, ok=ok)

    def _set_test_result(self, text: str, ok):
        """ok: True 成功 / False 失败 / None 进行中"""
        if ok is True:
            color, prefix = "#3FB950", "✓ "
        elif ok is False:
            color, prefix = "#F85149", "✗ "
        else:
            color, prefix = "#93A0B4", ""
        self.test_result.setText(prefix + text)
        self.test_result.setStyleSheet(f"color: {color}; font-size: 11px;")
        self.test_result.setToolTip(text)

    # ------------------------------------------------------------ 本地模型
    def refresh_local_status(self):
        """刷新本地模型标注。

        ⚠ **不要**在这里直接调 `local_model.decide()`：它内部要起
        `llama-server --list-devices` 子进程（秒级），在 UI 线程里同步等就是卡界面。
        所以先立刻显示一句便宜的"探测中"（`server_status()` 只读 pid 文件），
        完整档位判定丢到后台线程、结果走 `local_checked` 信号回来。
        """
        st = local_model.server_status()
        if st.get("running"):
            self.local_status.setText(
                f"探测中…（本地服务在跑：{st.get('model')} @ {st.get('device')}）")
        else:
            self.local_status.setText("探测中…（正在判定本机档位）")
        self.btn_local.setEnabled(False)
        threading.Thread(target=self._local_worker, daemon=True).start()

    def _local_worker(self):
        try:
            self.local_checked.emit(local_model.decide())
        except Exception as e:  # noqa: BLE001
            # 探测失败也必须给界面一个结论，不能留"探测中…"永远不变
            self.local_checked.emit({
                "mode": local_model.MODE_RETRIEVAL,
                "label": f"本地模型探测失败（{type(e).__name__}）", "detail": str(e),
                "base_url": "", "tier": "", "model": "", "device": "",
                "reason": str(e)})

    def _on_local_checked(self, decision):
        """decision = `core.local_model.decide()` 的返回值（或探测失败时的兜底 dict）"""
        self.local_status.setText(local_model.describe(decision))
        self.local_status.setToolTip(self.local_status.text())
        self._local_running = (decision.get("mode") == local_model.MODE_LOCAL)
        self.btn_local.setText("停止本地模型" if self._local_running else "启动本地模型")
        self.btn_local.setToolTip(
            "调用 local_llm\\run_server.py --stop（只停本端口，不会误杀 :8081 嵌入服务）"
            if self._local_running else
            "调用 local_llm\\run_server.py（按本机档位选模型；首次加载可能要几十秒）")
        self.btn_local.setEnabled(True)

    def on_local_button(self):
        """起 / 停本地模型服务（丢后台线程，界面不卡）"""
        running = self._local_running
        self.btn_local.setEnabled(False)
        self.local_status.setText("正在停止本地模型…" if running else
                                  "正在启动本地模型（首次加载模型可能要几十秒）…")
        threading.Thread(target=self._local_toggle_worker, args=(running,),
                         daemon=True).start()

    def _local_toggle_worker(self, running: bool):
        # 起停的**唯一实现**在 core/local_model.py（同一份逻辑也被「启动时自动起」用），
        # 这里只管显示反馈 —— 免得两处各写一遍启动命令（打包态的 --script 通道很容易写错）。
        if running:
            ok, detail = local_model.stop_server()
            text = "已停止本地模型"
        else:
            ok, detail = local_model.start_server()
            text = "本地模型已启动"
        self.connection_tested.emit(ok, text + (f"｜{detail}" if detail else ""))
        # 无论成败都重新探测，把标注拉回真实状态（不能靠"我以为起来了"）
        self.local_checked.emit(local_model.decide())

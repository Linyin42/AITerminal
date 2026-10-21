"""
验证：AI「测试连接」功能

AI 配置已从侧栏面板搬到「工具(T) → AI 设置…」对话框，
本脚本驱动 ui/ai_settings_dialog.py 的 AISettingsDialog。

用本地 mock HTTP 服务器验证成功路径，用关闭端口验证失败路径。
不需要真实 API Key，也不消耗任何额度。
"""
import atexit
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication
from config.settings import Settings

# ⚠ 必须把配置写到临时文件：本脚本最后会驱动 on_credentials_changed()，
#   它内部 `settings.save()` 写的是**真实**配置文件。以前没隔离，跑一次就把用户的
#   AI 配置改成 provider=openai_compat / key=sk-test-123 / base_url=127.0.0.1:9/v1，
#   用户那边的"本地模型"选择也就跟着丢了（2026-10-20 实查 settings.json 发现的）。
_CFG = Settings()
_ORIG_CFG_FILE = _CFG.config_file
_ORIG_CFG_DATA = dict(_CFG.data)
_TMP_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_tmp_ai_connection_settings.json")
_CFG.config_file = _TMP_CFG


@atexit.register
def _restore_real_settings():
    """还原真实配置（异常退出也要还原）"""
    _CFG.config_file = _ORIG_CFG_FILE
    _CFG._data = _ORIG_CFG_DATA
    try:
        os.remove(_TMP_CFG)
    except OSError:
        pass


Settings().set("restore_sessions", False)
from ui.ai_settings_dialog import AISettingsDialog
from ui.main_window import MainWindow

# ---------------------------------------------------------- mock 服务
class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass   # 静音


srv = HTTPServer(("127.0.0.1", 0), MockHandler)
MOCK_PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"mock 服务器: 127.0.0.1:{MOCK_PORT}")

app = QApplication(sys.argv)
w = MainWindow()
app.processEvents(); time.sleep(0.2); app.processEvents()

panel = w.ai_panel
dlg = AISettingsDialog(w)      # AI 配置所在处（工具菜单 → AI 设置…）
# 复刻 AIPanel.open_settings_dialog() 里的两条接线（那边用 exec() 会阻塞，这里手工接）
dlg.saved.connect(panel.on_ai_settings_saved)
dlg.connection_tested.connect(panel.connection_tested.emit)
print("1. 控件存在:", hasattr(dlg, "btn_test"), hasattr(dlg, "test_result"),
      hasattr(w, "status_api"))
print("   状态栏初始:", repr(w.status_api.text()))

results = []
dlg.connection_tested.connect(lambda ok, msg: results.append((ok, msg)))


def wait_result(timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout and not results:
        app.processEvents()
        time.sleep(0.05)
    return results[-1] if results else None


# ---------------- 用例 1：成功（指向 mock 服务器）
print("\n2. 成功路径（指向本地 mock）")
dlg.provider_combo.setCurrentIndex(
    [dlg.provider_combo.itemData(i) for i in range(dlg.provider_combo.count())].index("openai_compat"))
dlg.api_key_input.setText("sk-test-123")
dlg.base_url_input.setText(f"http://127.0.0.1:{MOCK_PORT}/v1")
dlg.model_combo.setCurrentText("mock-model")
dlg.on_test_connection()
r1 = wait_result()
print("   结果:", r1)
print("   面板标签:", repr(dlg.test_result.text()))
print("   状态栏:", repr(w.status_api.text()))
ok1 = r1 and r1[0] is True and "已连通" in dlg.test_result.text() and "✓" in w.status_api.text()
print("   判定:", "✓ 通过" if ok1 else "✗ 失败")

# ---------------- 用例 2：失败（关闭的端口）
results.clear()
print("\n3. 失败路径（指向关闭端口）")
dlg.base_url_input.setText("http://127.0.0.1:9/v1")
dlg.on_test_connection()
r2 = wait_result(timeout=20)
print("   结果:", r2)
print("   面板标签:", repr(dlg.test_result.text()))
print("   状态栏:", repr(w.status_api.text()))
ok2 = r2 and r2[0] is False and "✗" in dlg.test_result.text() and "✗" in w.status_api.text()
print("   判定:", "✓ 通过" if ok2 else "✗ 失败")

# ---------------- 用例 3：空 Key
results.clear()
print("\n4. 空 Key 应直接提示，不发请求")
dlg.api_key_input.setText("")
dlg.on_test_connection()
time.sleep(0.2); app.processEvents()
print("   面板标签:", repr(dlg.test_result.text()))
print("   无信号发出:", len(results) == 0)
ok3 = "API Key" in dlg.test_result.text() and len(results) == 0
print("   判定:", "✓ 通过" if ok3 else "✗ 失败")

# ---------------- 用例 4：改凭据后回到"未验证"
results.clear()
print("\n5. 改凭据后状态栏应回到『未验证』")
dlg.api_key_input.setText("sk-test-123")
panel.on_credentials_changed()
app.processEvents()
print("   状态栏:", repr(w.status_api.text()))
ok4 = "未验证" in w.status_api.text()
print("   判定:", "✓ 通过" if ok4 else "✗ 失败")

srv.shutdown()
print("\n=== 汇总 ===")
for name, r in (("成功路径", ok1), ("失败路径", ok2), ("空Key拦截", ok3), ("改凭据回未验证", ok4)):
    print(f"  {name}: {'✓' if r else '✗'}")
dlg.close()
w.close()
sys.exit(0 if all([ok1, ok2, ok3, ok4]) else 1)
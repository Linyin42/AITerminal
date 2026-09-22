"""
回归测试：连接对话框的"记住该连接"链路。

背景（曾经的两个 bug）：
  1. `load_last()` 从不回填 password_input —— 勾了"记住"也白搭，
     每次连接都要重新输密码。
  2. `set_last_connection()` 把 `remember` 标志剥掉了 —— 导致下次打开
     对话框复选框是未勾选状态，用户直接点连接就把已存的密码又过滤掉了，
     "记住"只能撑一次。
两个 bug 叠在一起：密码存了，但用户永远用不上。

本测试覆盖：
  · 勾"记住" → 密码持久化 + 重开对话框能回填 + 复选框保持勾选
  · 连续重连多次密码不丢
  · 不勾"记住" → 密码不持久化、不回填
  · **两种情况下"最近连接列表"都绝不含密码**（安全边界，不能破）
"""
import os
import sys
import tempfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from config.settings import Settings


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    # 全程用临时配置文件，绝不碰用户真实配置
    tmp = os.path.join(tempfile.gettempdir(), "_tmp_conn_remember_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)

    s = Settings()
    real_file, real_data = s.config_file, dict(s.data)
    s.config_file = tmp
    s.set("recent_connections", [])
    s.set("last_connection", {})
    s.save()

    # 让对话框用我们的临时 settings 实例
    import ui.connect_dialog as cd
    cd.Settings = lambda *a, **k: s
    from ui.connect_dialog import ConnectDialog

    def fill(d, host, pwd, remember):
        d.name_input.setText("核心交换机-A")
        d.protocol_combo.setCurrentText("Telnet")
        d.host_input.setText(host)
        d.port_spin.setValue(23)
        d.password_input.setText(pwd)
        d.chk_remember.setChecked(remember)

    results = []
    try:
        # ---------------- 场景 A：勾"记住"
        d = ConnectDialog()
        fill(d, "10.3.8.2", "AB403yw@8917", True)
        d.get_config()          # 模拟点"连接"

        restored = []
        for _ in range(3):
            d = ConnectDialog()
            restored.append((d.password_input.text(), d.chk_remember.isChecked()))
            d.get_config()      # 模拟"直接再点连接"

        results.append(("勾记住: 3 次重连密码都在",
                        all(p == "AB403yw@8917" for p, _ in restored)))
        results.append(("勾记住: 复选框保持勾选",
                        all(chk for _, chk in restored)))
        results.append(("勾记住: last_connection 含密码",
                        "password" in s.get("last_connection", {})))
        results.append(("勾记住: 最近连接不含密码（安全边界）",
                        not any("password" in r for r in s.get_recent_connections())))

        # ---------------- 场景 B：不勾"记住"
        s.set("recent_connections", [])
        s.set("last_connection", {})
        d = ConnectDialog()
        fill(d, "10.0.0.9", "secret2", False)
        d.get_config()

        d2 = ConnectDialog()
        results.append(("不勾记住: 密码不回填", d2.password_input.text() == ""))
        results.append(("不勾记住: last_connection 不含密码",
                        "password" not in s.get("last_connection", {})))
        results.append(("不勾记住: 最近连接不含密码",
                        not any("password" in r for r in s.get_recent_connections())))
    finally:
        s.config_file = real_file
        s._data = real_data
        if os.path.exists(tmp):
            os.remove(tmp)

    print("=" * 58)
    print("连接对话框「记住该连接」链路")
    print("=" * 58)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    passed = sum(1 for _, ok in results if ok)
    print("-" * 58)
    print(f"共 {len(results)} 项，通过 {passed} 项，失败 {len(results) - passed} 项")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

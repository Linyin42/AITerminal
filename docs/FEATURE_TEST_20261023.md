# 命令界面 / 设备操作 功能逐个测试（2026-10-23）

真机：**COM5 锐捷 S2928G-E**（RGOS 10.4(2b12)，已 `enable` 到 `Ruijie#`）。
全程只用只读命令（`show …` / `terminal length`），未改设备配置。

跑法（工程根，offscreen）：
```
$env:QT_QPA_PLATFORM='offscreen'
.venv-train\Scripts\python.exe -u %TEMP%\feat_matrix.py      # 真机 21 项（G1–G6）
.venv-train\Scripts\python.exe -u tests\smoke_test.py        # 647 项
.venv-train\Scripts\python.exe -u tests\regression_fixes.py  # 83 项
```

## 结果总表（30 PASS / 1 FAIL / 3 N/A）

| # | 功能 | 结论 | 证据 |
|---|---|---|---|
| **连接层** ||||
| 1 | 串口连接 COM5 | PASS | `1.1 Serial COM5 @ 9600` |
| 2 | 提示符识别 | PASS | `1.2 '\r\nRuijie#'` |
| 3 | 断开 | PASS | `1.6 is_connected=False` |
| 4 | 重连后继续下发 | PASS | `1.7 reconnect=True status=prompt` |
| 5 | Telnet 登录验证 | PASS | `test_telnet_login_verify.py 结果: ✓ 通过` |
| 6 | 连接对话框（异步/串口缓存/键盘/记住密码） | PASS | `6/0`、`7/0`、`10/0`、`7/0` |
| 7 | SSH 连接 | N/A | 无目标设备；settings 无 `devices` |
| 8 | eNSP Pro 连接 | N/A | `127.0.0.1:8443` 不可达 |
| **命令输入** ||||
| 9 | 单条命令 + 回显 | PASS | `1.3 status=prompt 1.2s len=719` |
| 10 | 连续 8 条命令压测 | PASS | `1.4 8/8` |
| 11 | 回车行为（补全浮层不吞回车） | PASS | `test_terminal_enter_hint.py 14/0` |
| 12 | 光标编辑（退格/行中插入/擦除回显） | PASS | `test_terminal_caret_edit.py ALL PASS` |
| 13 | Tab 补全 / 建议浮层 | PASS | `command_suggest_test.py 48/0` |
| 14 | 命令历史（上下键） | PASS | smoke「历史已记录」「回车把这一行记进历史」 |
| 15 | 多行粘贴 | PASS | smoke「粘贴被挡掉」+ regression「Ctrl+V / Shift+Insert 粘贴」 |
| 16 | Ctrl+C / Break | PASS | 真机 `1.5 send_break=True`；smoke「Ctrl+C 中断」 |
| **输出处理** ||||
| 17 | 关闭分屏（terminal length 0） | PASS | `2.1 terminal length 0 → Ruijie#` |
| 18 | 大输出全量落地（show running-config） | PASS | `2.2 1364 字符 / 98 行，含 interface/end` |
| 19 | `--More--` 自动翻页 | PASS | `2.3 pages=19 len=2124` |
| 20 | 分页回显进控件（H3C 语料） | PASS | `probe_paged_display` 分页标记 True / 正文 True |
| 21 | 会话自动翻页 | PASS | `probe_session_autopage 结论: PASS` |
| 22 | UTF-8 中文回显 | PASS | `3.3 中文回显测试 / 端口 GigabitEthernet` |
| 23 | ANSI 颜色码清洗 | PASS | `3.4 'Ruijie>'` |
| 24 | 厂商识别 | PASS | `3.2 detect_vendor='ruijie'` |
| **安全 / 批量 / 巡检** ||||
| 25 | 危险命令拦截（AI 生成路径） | PASS | `test_command_safety.py 50/0` |
| 26 | 只读 / 配置命令分类 | PASS | `4.2`、`4.4` |
| 27 | 批量下发（危险命令不下发） | PASS | `probe_patrol_batch_ruijie 11/11`，`reload 被拦截` |
| 28 | 真机巡检 patrol_device | PASS | `5.1 ok=True 2/2 有输出` |
| 29 | 巡检结果导出 xlsx | PASS | `probe_xlsx_export 所有单元格都读得回来` |
| **会话管理** ||||
| 30 | 多标签创建 / 关闭 / 掉线提示 | PASS | smoke「标签页已创建」「会话已关闭」「掉线: 连接已断开 + 时间」 |
| 31 | 外部接管控制 UI | PASS | `test_control_ui.py 17/0` |
| **补全数据** ||||
| 32 | 锐捷 `show vlan` 补全 | **FAIL** | `suggest('show vlan','ruijie')` 0 候选；`_vendor_cmd/ruijie/commands.jsonl` 里只有 example 步骤，没有该命令条目 |
| 33 | 华为/H3C `display vlan ` 补全 | PASS | huawei token 5 / next 5；h3c token 2 / next 5 / seq 4 |
| 34 | 终端三色快照 | N/A | 依赖 `%TEMP%\ait_cpp_build\AITerminal.exe`（C++ 构建产物不存在） |

## 发现的问题

### 1. 设备在用户模式（`Ruijie>`）时，关闭分屏与大输出都会失败，应用不会自动 enable
现象：`terminal length 0` → `% Unknown command.`；`show running-config` 只回 86 字符报错。
执行 `enable` 后同一批测试全过（`Ruijie#`）。
影响：巡检 / 批量下发在用户模式下拿不到 `show running-config` 这类输出，但应用既不提示"设备处于用户模式"，
也不自动 enable。建议二选一：连接后可勾选"自动 enable"；或巡检失败时明确提示模式问题。

### 2. 协议名大小写敏感（已修）
`patrol.build_connector({'protocol': 'serial'})` 抛 `ValueError: 不支持的协议: serial`。
UI 传的是 `Serial`，但手写/导入的配置常是小写。
修复：`core/patrol.py` 加 `_PROTOCOL_ALIASES` 归一（`serial`/`SERIAL`/`ssh`/`ensp` 均可）。

### 3. 锐捷命令补全数据缺口（未修，属数据管线）
`_vendor_cmd/ruijie/commands.jsonl`（5.47 MB）里 `show vlan` 只出现在 **example steps**，
没有作为命令条目建索引 ⇒ `is_complete_command(['show','vlan'])` 为 False，补全浮层给不出任何候选。
华为/H3C 同类命令正常。修它需要重跑锐捷命令抽取（文档 → commands.jsonl），不是改代码能解决的。

### 4. `undo vlan 10` 的测试断言与实现意图相反（已对齐）
实现：`core/ai_agent.py` 的 `DANGEROUS_PATTERNS` 含 `\b(no|undo)\s+(vlan|acl|port-group)\b`，直接**拦截**。
测试原断言：应进可执行项、只需确认。两者相反，测试一直红。
处理：按更安全一侧（拦截）对齐测试，并在断言旁写明原因。

## 未覆盖 / 待办
- SSH：需要一个可达的 SSH 设备（或本机起 sshd）才能测；当前无。
- eNSP Pro：平台未启动（8443 不通）。
- 终端三色快照：需要 C++ 版构建产物。
- 真机 `--More--` 手动翻页（空格/`q`）只测了自动翻页路径。

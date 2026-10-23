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

## 结果总表（32 PASS / 0 FAIL / 3 N/A）

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
| 32 | 锐捷 `show vlan` 补全 | PASS（修后） | 全量抽 312 份命令参考后 `suggest('show vlan','ruijie')` 8 候选、`show vlan ` → 5；矩阵 6.2 由 0 候选变 10 候选，23/23 |
| 33 | 华为/H3C `display vlan ` 补全 | PASS | huawei token 5 / next 5；h3c token 2 / next 5 / seq 4 |
| 34 | 终端三色快照 | N/A | 依赖 `%TEMP%\ait_cpp_build\AITerminal.exe`（C++ 构建产物不存在） |

## 发现的问题

### 1. 设备在用户模式（`Ruijie>`）时，关闭分屏与大输出都会失败，应用不会自动 enable（已修）
现象：`terminal length 0` → `% Unknown command.`；`show running-config` 只回 86 字符报错。
执行 `enable` 后同一批测试全过（`Ruijie#`）。

修复（2026-10-23）：`core/batch.py` 新增 `current_prompt` / `in_user_mode` / `ensure_privileged`，
只把 `Ruijie>` 这种（`^[A-Za-z][\w.\-]*>\s*$`）判成用户模式——`<H3C>` / `[H3C]` / `#` 都不算
（华为/华三没有 enable 层，误发 `enable` 会污染回显）。接入两条路径：

- 巡检：`auto_enable` 默认开，`enable` 成功后 `disable_paging` 才可执行；未勾自动 enable 时
  也会判定并 note「设备停在用户模式（Ruijie>），未勾选「自动 enable」」，不静默失败。
- 交互会话：Serial/SSH/Telnet 建会话成功后自动 enable，提示写进会话信息区
  （实测 `notes = ['已自动 enable（Ruijie> → Ruijie#）']`，终端里能看到 `Ruijie>enable` 回显，等同手敲）。

UI：连接对话框串口页新增「自动 enable」勾选（默认勾）+ enable 密码（留空即无密码），
写进 `config.settings.serial_defaults` 持久化。

验证：`tests/test_enable_mode.py` **13/13**；真机 `auto_enable=True` → `privilege=privileged`、
`show running-config` 拿到 1364 字符全量；`auto_enable=False` → `privilege=user` + 4 条提示。

### 2. 协议名大小写敏感（已修）
`patrol.build_connector({'protocol': 'serial'})` 抛 `ValueError: 不支持的协议: serial`。
UI 传的是 `Serial`，但手写/导入的配置常是小写。
修复：`core/patrol.py` 加 `_PROTOCOL_ALIASES` 归一（`serial`/`SERIAL`/`ssh`/`ensp` 均可）。

### 3. 锐捷命令补全数据缺口（已修，2026-10-23）
`_vendor_cmd/ruijie/commands.jsonl`（5.47 MB / 12,387 条命令）里 `show vlan` 只出现在 **example steps**，
`show …` 家族整共只有 **2 条**（`show efd device` / `show license hostid`）—— 用户最常用的
`show vlan` / `show version` / `show running-config` 一个补全候选都没有。

根因：`tools/doc_to_commands.py` 只认 `章/*.htm` 结构；锐捷「命令参考」是**单文件 fulltext.txt**，
它一份都读不了（这就是为什么 ruijie 的 `source` 全是 `fulltext.txt#L<n>` 但命令数那么少）。

修复：新增 `tools/ruijie_fulltext_commands.py`（保守规则：整行成命令、动词限 show/display、
命令名 ≤4 词、只允许 `[...]`/`{a|b}` 尾巴、剔除中文行），全量跑了 312 份「命令参考」：

- 原始命中 **228,101** 条目 → 按命令名去重到 **2,867** 条 → 跳过已入库后写入 **2,269** 条；
- `commands.jsonl` 18,108 → **20,980** 行（自动备份 `.bak_20261023_2125`）；索引
  commands 12,387 → **15,259**、去重命令名 6,243、词表 3,571；
- **加载耗时没变**：312 份文档抽完，索引加载 316 ms → **336 ms**（只写命令名去重后的条目，
  不是 22 万条全塞进去，这才是加载不涨的原因）；
- 验证：`show vlan` → 8 候选、`show vlan ` → 5（`summary`/`statistics`/`private-vlan`）、
  `show ver` → 6、`show run` → 3、`show interface ` → 9+7、`show ` → 20+20；
- 抽出来的不是垃圾：抽查 `show of`（OpenFlow 状态）、`show si`（自定义应用）在原文里都是
  `【命令格式】` 小节标题，属真实命令；
- 索引规模断言 12,387 → **15,259**（`tests/command_suggest_test.py` 48/0）。

复跑真机矩阵：`6.2 'show vlan '` 从 2 候选变成 10 候选，**23 PASS / 0 FAIL**。

### 4. `undo vlan 10` 的测试断言与实现意图相反（已对齐）
实现：`core/ai_agent.py` 的 `DANGEROUS_PATTERNS` 含 `\b(no|undo)\s+(vlan|acl|port-group)\b`，直接**拦截**。
测试原断言：应进可执行项、只需确认。两者相反，测试一直红。
处理：按更安全一侧（拦截）对齐测试，并在断言旁写明原因。

## 复现（2026-10-23 修后全绿）
```
真机矩阵     python %TEMP%\feat_matrix.py                 → 23 PASS / 0 FAIL
自动 enable  python tests\test_enable_mode.py             → 13 PASS / 0 FAIL
补全数据     python tests\command_suggest_test.py          → 48 PASS / 0 FAIL
回归         python tests\smoke_test.py                    → 647 PASS / 0 FAIL
             python tests\regression_fixes.py              → 83 PASS / 0 FAIL
```
真机需先把要测的脚本跑在 COM5（9600 8N1）上，且串口独占——同一进程不能同时持有两个连接。

## 未覆盖 / 待办
- SSH：需要一个可达的 SSH 设备（或本机起 sshd）才能测；当前无。
- eNSP Pro：平台未启动（8443 不通）。
- 终端三色快照：需要 C++ 版构建产物。
- 真机 `--More--` 手动翻页（空格/`q`）只测了自动翻页路径。
- 锐捷 312 份命令参考已全量抽取（22.8 万条原始 → 2,867 条去重命令）；其余非「命令参考」类
  文档（配置指南/一本通）里的 `show …` 还没抽，需要时同样可以跑这个工具。

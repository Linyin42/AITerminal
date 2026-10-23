# AITerminal 项目速查总结（PROJECT-DIGEST）

> 用途：**下次用这个东西时，问某件事 / 找某条经验，能马上查到。**
> 写法约定：每条结论都带来源（`文件路径:行号`）。**拿不准 / 读不到的写「未确认」，不猜。**
> 生成时间：2026-10-02（本轮只读调查，**未修改 `cpp/` `engine/` `tests/` `design/` 下任何文件**）

---

## 更正记录（2026-10-03）

> 由逐功能验收（`FEATURE-TESTS.md`）核对得出：**原文保留**，此处给"原文说法 → 现状"。

| 原文位置 | 原文说法 | 现状（2026-10-03） |
|---|---|---|
| §1.3 | 「厂商知识库还没建数据」「`knowledge/` 目录当前**不存在**」 | **已建**：`knowledge/{h3c,huawei,ruijie}/` 含 `commands.sqlite` + `chapters/*.md` + `examples/*.md` + `index.json`；h3c 2823 命令 / huawei 12441 命令 / ruijie 3377 命令；建库期"索引→锚点"不变量检查覆盖 **24,865 条候选、0 违规** |
| §2.2 | `--shot-dialog` 「合法值只有 7 个」 | **现在 10+**（另有 `usage-fill` / `usage-summary` / `command-knowledge` / `connect-serial` / `connect-serial-edit` / `diff-data` / `knowledge-hits` / `ai-answer` / `patrol-run` …）；**非法值仍按原文退 2 ✓**（实测 `--shot-dialog=conect` → exit 2） |
| §1.3 / §5.3 | 「设计稿菜单栏 **7 项**，实现是 **5 项**」 | 实测实现 = **5 个菜单 / 14 项**：文件 3 / 会话 4 / 视图 1 / 工具 4 / 帮助 2（明细见 `FEATURE-TESTS.md` 的"菜单清单"） |
| §1.2 / 6 节 | 危险命令「`reboot`/`delete`/`erase`/`undo all`/`factory-reset`」（读作穷举） | **举例，非穷举**：`core/safety.py` 实际规则为 `^\s*(undo|no|delete|erase|reset|clear)\b` 与 `^\s*(reboot|reload|shutdown)\b`（还含 `no`/`clear`/`reset`/`reload`/`shutdown`） |
| §1.3 | 「`---- More ----` 分页**没有界面提示条**」是"计划加" | **仍未实现**：实测真机截图 `evidence/13-autocmd-display-version.png` 里 `---- More ----` 是**设备原文直出**，界面无额外提示条 |
| §1.3（旧文档 `使用说明.txt` 的 AI 菜单） | 「AI 开关（Ctrl+I）/ AI 分析一次 / 清空 AI 上下文」「视图 → 显示/隐藏 AI 面板」 | **现行均不存在**：全仓无 `Ctrl+I` 绑定（仅 `Key_Insert`）；视图菜单只有「显示/隐藏 设备库」；AI 面板是**对话框**（`MainWindow.cpp:959` `new AiPanel(...)`），**无启停状态** → 记 **MISSING-CAPABILITY**（旧版有、现行无，待用户决定是否补） |
| §1.3（本轮修复项） | `_HANDOFF.md:538` 建议"安全闸门点 No 后补发 `0x15` 擦行" | **该建议是错的**：H3C 收到 Ctrl+U(0x15) 是**静默清行**（真机 `AIT_BYTELOG` 字节实测：屏上旧内容不消失、光标列不回退，下一条命令会接在半截文本后）；正确做法 = **退格擦行**，已实现为 `TerminalView::clearDeviceLine()` 并在 `MainWindow::submitLine` 的 `answer != Yes` 分支调用 |

---

## 目录（按"现象/问题"跳转）

- [1. 一句话是什么 + 能用/不能用](#1-一句话是什么--能用不能用)
- [2. 怎么跑起来：构建 / 启动参数 / 跑测试](#2-怎么跑起来构建--启动参数--跑测试)
- [3. 怎么连设备：四个靶机](#3-怎么连设备四个靶机)
- [4. 环境与坑速查表](#4-环境与坑速查表现象--原因--做法)
- [5. 设计与配色的决议](#5-设计与配色的决议最终采用什么为什么)
- [6. AI 的作用范围](#6-ai-的作用范围用户-2026-10-明确定义)
- [7. 知识库结构](#7-知识库结构用户定义)
- [8. 文件地图（某功能在哪改）](#8-文件地图想知道某功能在哪改时看这里)
- [9. 常见问题速查 Q&A](#9-常见问题速查-qa)
- [10. 未完成 / 下一步](#10-未完成--下一步)
- [11. 网络知识速查](#11-网络知识速查)
  - [11.1 三大厂商命令体系对照表](#111-三大厂商命令体系对照表)
  - [11.2 常见运维任务怎么做](#112-常见运维任务怎么做三厂商对照--注意)
  - [11.3 术语速查](#113-术语速查一句话--一条最相关命令)
  - [11.4 分页与屏显管理](#114-分页与屏显管理最省时间的一节)
  - [11.5 会话与连接管理](#115-会话与连接管理telnet--ssh--console-的差别与坑)
  - [11.6 易错点 / 经验](#116-易错点--经验最能省时间)
  - [11.7 排障思路（分层）](#117-排障思路分层先看什么--用什么命令--典型结论)
- **[12. 实战经验库（重点）](#12-实战经验库)** ← 按现象查
  - [12.1 样式表整份失效](#case-qss-整份失效)
  - [12.2 终端上色不生效](#case-上色不生效)
  - [12.3 分页吃输入](#case-分页吃输入)
  - [12.4 无边框窗口判据](#case-无边框判据)
  - [12.5 windres 空格路径](#case-windres-空格路径)
  - [12.6 像素判色被 ClearType 骗](#case-cleartype-假彩边)
  - [12.7 出图前置：控制台登录态](#case-控制台登录态)
  - [12.8 按退格删不掉字](#case-退格删不掉)
  - [12.9 厂商判定被配置压过](#case-厂商判定)
  - [12.10 提示符被切成两块](#case-提示符分块)
  - [12.11 隐藏的 QSS 类缺陷：token 前缀替换](#case-token-前缀)
  - [12.12 添加新 .cpp 报 undefined reference](#case-undefined-reference)
  - [12.13 卡片化改版被回退](#case-卡片化回退)
- [13. 「使用总结」功能规格（**待实现**）](#13-使用总结功能规格待实现)

---

## 1. 一句话是什么 + 能用/不能用

### 1.1 一句话

**AITerminal 是一个面向网络设备运维的桌面终端工具：C++/Qt6 做界面层，Python 做引擎（JSON-RPC over stdio），
支持 SSH / Telnet / Serial(Console) / eNSP Pro 四种连接，带厂商命令自动补全、本地资料库、AI 助手。**

> 来源：`README.md:1-4`（Python/PyQt6 版定位）、`cpp/CMakeLists.txt:3-6`（C++ 界面层 v0.8.0）、
> `engine/main.py:1-13`（引擎把界面当后端，stdin/stdout 走 JSON-RPC）、`engine/service.py:20-32`（协议收口表）。

**⚠️ 重要的历史分层（最容易搞错的一点）：**

| 层 | 在哪 | 现状 |
|---|---|---|
| **旧版 Python/PyQt6 界面** | `main.py` + `ui/` + `core/` + `config/` | 早期完整实现（v0.7），**已被 C++ 界面取代**，但 `core/` 仍是引擎的真源 |
| **现行 C++/Qt6 界面** | `cpp/` | 正在迭代的主力界面（无边框窗口 + 自绘标题栏 + 命令补全浮层） |
| **Python 引擎** | `engine/service.py` + `core/*.py` | 两代界面共用；C++ 通过 `EngineClient` 以子进程方式拉起 |

来源：`README.md:118-133`（旧版目录结构）、`cpp/CMakeLists.txt:23-76`（C++ 源文件清单）、
`engine/service.py:1-12`（引擎的三条硬约束）。

### 1.2 已实现（能用）

| 能力 | 状态 | 来源 |
|---|---|---|
| SSH / Telnet / Serial / eNSP 四种连接 | ✅ | `engine/service.py:20-32`（协议收口）、`core/ssh_connector.py`、`core/telnet_connector.py`、`core/serial_connector.py`、`core/ensp_connector.py` |
| 多标签会话、终端 ANSI 渲染 | ✅ | `cpp/src/ui/SessionTabBar.{h,cpp}`、`cpp/src/core/AnsiParser.{h,cpp}` |
| **命令自动补全（IDE 式浮层）** | ✅ 真机验证过 | `cpp/src/ui/TerminalView.cpp`、`core/command_suggest.py`、RPC `commands.suggest`（`engine/service.py:119`） |
| **按设备当前视图过滤候选** | ✅ 真机验证过（HCL S6850） | `_HANDOFF.md:122-168`；C++ 只送 prompt 原文、Python 定策略 |
| 厂商嗅探（含分块形态、提示符纠正） | ✅ | `tests/cpp_ui_regression.py:283-307`；`_HANDOFF.md:292-312` |
| ANSI 擦除（CSI nD / CSI nJ） | ✅ 三家真机字节验证 | `tests/cpp_ui_regression.py:260-274`；`_HANDOFF.md:250-271` |
| 本地资料库（导入/索引/检索/删除） | ✅ | `core/knowledge.py`、RPC `knowledge.*`（`engine/service.py:96-101`） |
| AI 助手（多提供方、RAG 接地） | ✅ | `core/llm.py`、`core/ai_agent.py`、RPC `ai.ask`/`ai.test`/`ai.getSettings`/`ai.setSettings`（`engine/service.py:104-113`） |
| 多设备并发巡检 | ✅ 真机验证过（eNSP + Telnet 并发 2/2） | `core/patrol.py`、RPC `patrol.run`（`engine/service.py:107`） |
| 配置差异比对 | ✅ | `core/diff.py`、RPC `diff.compare`（`engine/service.py:115`） |
| 安全闸门（危险命令拦截 + 配置类确认） | ✅ | `core/safety.py`、RPC `safety.check`（`engine/service.py:86`） |
| 无边框窗口 + 自绘标题栏 + 应用图标 | ✅ 实测过 | `cpp/src/ui/TitleBar.{h,cpp}`、`_HANDOFF.md:646-676`（画风统一）、`_automation/check_exe_icon.py` |
| 问题包（反馈给 AI，Ctrl+Shift+D） | ✅ | `cpp/src/ui/ProblemReport.{h,cpp}`、`_HANDOFF.md:559-587` |
| round-08 新配色 + 终端蓝绿双色 | ✅ **已落地到应用** | `cpp/src/core/Theme.cpp:14-138`、`cpp/resources/theme/dark.qss` |

### 1.3 未实现 / 已知缺口

| 缺口 | 说明 | 来源 |
|---|---|---|
| **厂商知识库还没建数据** | 读数据的代码层已写好（批次二），**建库脚本属于批次三、尚未开工**；`knowledge/` 目录当前**不存在** | `core/vendor_knowledge.py:1-16`（"批次三会往这里加 `build_*`"）、本轮 `Test-Path knowledge` → 不存在 | <!-- 更正 2026-10-03：知识库已建（h3c 2823/huawei 12441/ruijie 3377 命令），见顶部「更正记录」 -->
| 其余对话框**无交互自动化测试** | 只测"能渲染出图"：配置差异比对/命令模板/提示词/资料库/巡检/笔记/串口高级参数/AI 设置/确认下发/存为资料/编辑系统提示词 | `tests/cpp_ui_regression.py:393-404`（测试自己打印的覆盖缺口） |
| 安全闸门点 No 后**设备行未清** | 本地缓冲清了、设备行还留着半截，下条命令会接在后面；应在 `MainWindow::submitLine` 的 `answer != Yes` 分支补发 `0x15` | `_HANDOFF.md:538-539`（明确标"仍未修"） |
| 界面上**没有厂商字段** | 嗅探失败（自定义 hostname）时补全依旧不弹；建议给 ConnectDialog 加厂商下拉兜底 | `_HANDOFF.md:553-555` |
| `---- More ----` 分页**没有界面提示条** | 实机撞到过，界面目前原样显示；计划加"设备在分页显示 · 继续"提示条 | `_HANDOFF.md:842-843` | <!-- 更正 2026-10-03：仍未有提示条，实测 ---- More ---- 是设备原文直出 -->
| 设计稿菜单栏 **7 项**，实现是 **5 项** | 稿子：文件/会话/视图/工具/AI/资料/帮助 | `_HANDOFF.md:844` | <!-- 更正 2026-10-03：实现为 5 组/14 项，见顶部「更正记录」与 FEATURE-TESTS.md -->
| 设备名在「设备列表 / 标签 / 会话头」出现三次 | 可再收敛 | `_HANDOFF.md:840-841` |
| 终端不是完整模拟器 | 未实现光标定位（CSI H/f）、全屏 TUI；跨包 `\r` 覆盖无法撤回 | `docs/README.md:863-865` |
| 旧 Python 界面已知限制 18 条 | 密码 base64 非加密、本地检索是关键词非语义、扫描件 PDF 不可检索等 | `docs/README.md:861-899` |

---

## 2. 怎么跑起来：构建 / 启动参数 / 跑测试

### 2.1 构建 C++ 界面（**必须项：先杀进程 + PATH 前缀**）

```powershell
# ① 必须先杀进程，否则链接阶段会报 ld error
Get-Process AITerminal -ErrorAction SilentlyContinue | Stop-Process -Force

# ② 必须把 Qt 的 mingw 与 Qt bin 放到 PATH 最前面
$env:PATH = "D:\Qt\Tools\mingw1310_64\bin;D:\Qt\6.8.3\mingw_64\bin;" + $env:PATH

# ③ 用 Qt 自带/随 Python 装的 cmake 构建（构建目录在 TEMP，不在项目里）
$cmake = Join-Path (Split-Path (Get-Command python).Source) "Scripts\cmake.exe"
& $cmake --build (Join-Path $env:TEMP "ait_cpp_build")
```

来源：`_HANDOFF.md:407-417`（构建三行原文）。

**构建目录与工具链（实测核到的值）：**

| 项 | 值 | 来源 |
|---|---|---|
| 构建目录（binary dir） | `%TEMP%\ait_cpp_build\` | `_HANDOFF.md:17` |
| 源码目录 | `…\aiterminal\cpp` | `CMakeCache.txt: CMAKE_HOME_DIRECTORY` |
| 生成器 | Ninja | `CMakeCache.txt: CMAKE_GENERATOR:INTERNAL=Ninja` |
| C++ 编译器 | `D:/Qt/Tools/mingw1310_64/bin/g++.exe` | `CMakeCache.txt: CMAKE_CXX_COMPILER` |
| Qt | `D:/Qt/6.8.3/mingw_64`（`Qt6_DIR=…/lib/cmake/Qt6`） | `CMakeCache.txt: Qt6_DIR` |
| Build type | `Release` | `CMakeCache.txt: CMAKE_BUILD_TYPE` |
| 项目内有没有 `cpp/build` | **没有**（不要在这个路径找 exe） | 本轮实测：`Test-Path cpp\build` → False |

**产物路径：`%TEMP%\ait_cpp_build\AITerminal.exe`**（本轮实测存在，mtime `2026-10-02 21:43:17`）。
> ⚠️ 项目里的 `dist\AITerminal.exe`（39.6 MB，`2026-09-22`）是**旧的 Python 打包版**，不是 C++ 版。
> ⚠️ 且本轮实测 `cpp/src/ui/TerminalView.cpp`（21:50）、`engine/service.py`（21:52）**晚于 exe**，
> 也就是说 **exe 是旧构建**，改了 C++ 代码必须先重新构建再出图。

**POST_BUILD 会自动拷到 exe 同级**：`engine/`、`core/`、`config/`、`vendor-icons/`
（少拷 `core/`+`config/` 会让 `import core.*` 直接失败；少拷 `vendor-icons/` 会让 12 个厂商图标静默回退成手绘）。
来源：`cpp/CMakeLists.txt:119-135`。

**新增 `.cpp` 必须登记**：`CMakeLists.txt` 是**显式列源文件**（不是 `file(GLOB)`），
不登记会以 `undefined reference to XxxDialog::XxxDialog` 暴露（看起来像构造函数写错了）。
来源：`cpp/CMakeLists.txt:56-59`。

### 2.2 启动参数（全是**等号**形式，空格形式会被静默忽略）

| 参数 | 作用 | 关键细节 |
|---|---|---|
| `--screenshot=<png>` | 主窗口 `grab()` 存图，**15 秒后自己退出** | **输出目录必须先建好**，否则 `save()` 静默失败（`_HANDOFF.md:419`）；C++ 侧在 `main.cpp:642-644` 设了 15000ms 定时 |
| `--shot-dialog=<name>` | 渲染某个对话框/面板后退出 | 合法值只有 7 个（见下） | <!-- 更正 2026-10-03：现已 10+ 个合法值（非法值仍退 2），见顶部「更正记录」 -->
| `--autoconnect` | 启动即连"第一台设备" | 需要 `AIT_CONFIG_FILE` 指向设备配置 |
| `--autocmd=<命令>` | 连上后自动下发一条命令（**不需要抢键盘焦点**） | `main.cpp:92-93`；`_automation/ait_shot_now.py:79-99` 用它避开 DSH 霸占前台 |

**`--shot-dialog=` 的合法值（7 个）与各自的等待时间**
来源：`cpp/src/main.cpp:105-115`（白名单）、`main.cpp:129-196`（各分支的定时）：

| 值 | 打开什么 | 截图延时 |
|---|---|---|
| `connect` | 连接对话框（样例用 eNSP Pro，能顺带证明按协议切字段） | 1500 ms |
| `knowledge` | 资料库（要等 `knowledge.stats` + `knowledge.list` 两次 RPC 回来） | 6000 ms |
| `ai` | AI 助手面板 | 2500 ms |
| `patrol` | 多设备并发巡检 | 2500 ms |
| `ai-settings` | AI 设置 | 2500 ms |
| `diff` | 配置差异比对 | 2000 ms |
| `complete` | 命令补全自测（真控件+真引擎+真键盘事件），并额外落一份 `.json` 报告 | 见 `main.cpp:213-320` |

> **未知 `--shot-dialog` 值会报错并非零退出（返回 2）**，不是静默忽略。
> 原因写在 `cpp/src/main.cpp:101-104`：静默忽略会让脚本打开交互窗口一直挂着，拼错一个名字（`conect`）就中招。
> 对应回归断言：`tests/cpp_ui_regression.py:347-357`。

用法示例（等号形式，来自 `tests/cpp_ui_regression.py:113`、`:121`、`:136`）：

```powershell
& "$env:TEMP\ait_cpp_build\AITerminal.exe" --screenshot="$env:TEMP\ait_ui_shots\boot.png"
& "$env:TEMP\ait_cpp_build\AITerminal.exe" --shot-dialog=connect --screenshot="$env:TEMP\ait_ui_shots\connect.png"
```

### 2.3 跑测试（四套 + 为什么每套之间要 kill 进程）

> ⚠ 凡可能触发使用总结记录器的探针，除 `AIT_CONFIG_FILE` 外，**必须把 usage-log 目录也重定向到 `%TEMP%`**。
>    否则探针会写进用户真实目录 `%APPDATA%\AITerminal\usage-log\YYYY-MM-DD.md`
>    （实例：一次 `--autocmd` 探针在其间状态栏为「记录中」，写入了 `display version` ×22 等测试记录；
>      处置=先备份再移出到 `_shots\round08-impl\usage-log-moved\`，不删除）。

| 套 | 命令 | **实跑断言数**（唯一权威口径） | 需要什么 |
|---|---|---|---|
| 冒烟（引擎侧全量） | `python tests/smoke_test.py` | **593** | 不需要设备、不联网 |
| 命令补全 | `python tests/command_suggest_test.py` | **48** | 需要 `_vendor_cmd/` 数据；**PYTHONUTF8=1** |
| 回归修复项 | `python tests/regression_fixes.py` | **27** | 不需要设备（纯代码检查/离线逻辑） |
| C++ 界面回归 | `python tests/cpp_ui_regression.py` | **63** | **需要已构建的 exe**（`%TEMP%\ait_cpp_build\AITerminal.exe`），无 exe 直接返回 2 |
| 引擎端到端 | `python tests/engine_smoke.py --protocol Telnet --host 127.0.0.1 --port 2323` | 无 `check()` 计数（脚本自己打印"结果: 通过/失败"） | 需要 `tools/fake_device.py` 当对端，或真设备 |

**2026-10-02 当场实测（本轮重建后实跑，每套之间 kill AITerminal；日志 `_shots\round08-impl\tests-batch2.txt`）**：

```
check_qss_tokens.py : [OK] 所有 token 都有定义 / 括号平衡 depth=0
command_suggest_test.py : 通过 48 项，失败 0 项            exit=0
regression_fixes.py     : 共 27 项，通过 27 项，失败 0 项   exit=0
cpp_ui_regression.py    : 共 63 项，通过 63 项，失败 0 项   exit=0
smoke_test.py           : 共 593 项，通过 593 项，失败 0 项 exit=0
（exe mtime 2026-10-02 22:05:43，构建退出码 0）
```

**⚠ 上一版的 532/39/33/44 是"源码字面量估计值"，不是实跑值，已作废 —— 见下面的口径说明。**
**任何"全绿"结论都必须以当场跑出来的输出为准，不要引用本文的数字当结论。**

**计数口径（2026-10-02 查清，别再漂移）**：
- 这四套测试**都没有用 pytest**，都是自写 `check(name, cond, detail)` 收集 `PASS/FAIL` 列表，
  末尾打印 `共 N 项，通过 M 项`（`command_suggest_test.py` 打印的是 `通过 N 项，失败 M 项`）。
- **实跑断言数 = 运行时真正执行的 `check()` 次数**，它与"源码里能数出来的 `check(` 字面量"**不相等**，
  因为相当一部分 `check()` 写在**循环/数据表**里（例如命令补全的"视图过滤"三家 × 多个用例、
  smoke 的数据驱动用例），还有 regression 里运行时才决定走不走的分支。
- 当场对照（同一份文件，两种数法）：
  | 套 | 源码 `check(` 出现次数 | 其中字面量标题的 | **实跑** |
  |---|---|---|---|
  | command_suggest_test.py | 39 | 38 | **48** |
  | regression_fixes.py | 34 | 33 | **27** |
  | cpp_ui_regression.py | 45 | 42 | **63** |
  | smoke_test.py | 533 | 532 | **593** |
  （digest 上一版的 39/33/44/532 正是"字面量标题"那一列 —— 口径错了，数字没错。）
- **结论：没有用例丢失、没有文件被替换。** `593 / 48 / 27` 与 `_HANDOFF.md:37-42` 的基线完全一致；
  `cpp_ui` 是 **63**（`_HANDOFF.md` 第 13 节自己也记了"cpp_ui_regression 仍 63/63"，57 是更早的数字）。
  历史输出文件 `_*.out.txt` 里的 39 是**那一版的实跑值**（那时视图过滤用例还没加），不是"丢失的 9 项"。

**为什么每套之间要 kill 进程**（原文与代码依据）：

1. `cpp_ui_regression.py` 每次超时会主动 `taskkill /F /IM AITerminal.exe`
   —— 见 `tests/cpp_ui_regression.py:60-62`。
2. **不 kill 的后果（出图场景）**：残留进程会让后面的图**带上前一张的状态**，
   实测"不 kill 时对话框会挂在旧进程的窗口上" —— `_automation/shoot_round08.py:22-24`。
3. `_automation/ait_shot_now.py:92` 在启动前无条件 kill 一次，也是同一个理由。

**两个跑测试时必踩的坑**（都在代码注释里写明）：

- **不要用 `capture_output=True`（管道）收这个程序的输出**：程序会派生 Python 引擎子进程，
  孙子进程继承管道写端 → `communicate()` 永远等不到 EOF，表现为"进程挂住不退出"，
  而实际 15.5s 就正常退出了（实测踩过，差点误报成缺陷）。
  改用**临时文件**收输出 —— `tests/cpp_ui_regression.py:41-48`。
- **参数必须是 `--screenshot=<路径>` 的等号形式**，空格形式会被静默忽略 →
  程序打开交互窗口一直等，看起来像"挂住" —— `tests/cpp_ui_regression.py:109-112`。

**跑 Python 侧测试的前置**：`$env:PYTHONUTF8=1`（中文与编码，见第 4 节）。

### 2.4 无设备时怎么自证链路（旧版工具，仍可用）

```bash
# 离线自检
python tests/smoke_test.py

# 终端 A：内置模拟设备（模拟 H3C 交换机，开了分页）
python tools/fake_device.py --port 2323 --no-login --paging
# 终端 B：验证整条链路
python tools/verify_device.py --protocol telnet --host 127.0.0.1 --port 2323
```

来源：`README.md:65-84`、`docs/验证指南.md:8-28`。`docs/验证指南.md:175-192` 有一张 **14 项验证清单**。

---

## 3. 怎么连设备：四个靶机

### 3.1 eNSP Pro（华为，HTTP + STOMP，不是 TCP）

| 项 | 值 |
|---|---|
| 地址 | `192.168.31.11:8443`（**旧地址 `192.168.31.157` 已废弃**，机器换过 IP） |
| 沙箱 / 设备 | 沙箱 `2` / 设备 `22`（handoff 正文写 22；`_HANDOFF.md:434-438`） |
| 密码 | `Huawei@123`（无需用户名） |
| 设备 | `S-1`，ENSP-LSW 交换机，Huawei VRP |
| 配置 | `%TEMP%\ait_ensp_settings.json` |
| 协议名 | 引擎里叫 `eNSP`（`ensp` / `ensp-pro` / `ensppro` / `ensp pro` 都收口到它） |

来源：`_HANDOFF.md:433-441`、`engine/service.py:27-31`。

**坑与恢复流程（各步都有脚本）：**

1. **设备关机时会稳定回** `Device <id>-<uuid> is not running.` + `Connection timeout, please press any key to reconnect.`
   ⚠️ **此时 `deviceStartProgress` 仍可能是 100，别被它骗了** —— `_HANDOFF.md:451-453`。
2. **先问服务端要真实清单**，别反复重试一个连不上的 node_id：
   `python _automation\ensp_list_devices.py --host 192.168.31.11 --port 8443`
   （可用只读接口：`/api/getUserInfo`、`/service/ensp/system/summary`、
   `/service/ensp/sandbox/getOneSandbox?sandboxId=N`）—— `_HANDOFF.md:444-450`。
3. **启动设备**：`python _automation\ensp_start_device.py --start`（CDP 驱动界面走「一键启动」）。
   两个坑：① eNSP 是自签证书，必须 `B.launch(..., restart=True, ignore_cert_errors=True)`
   （换开关不 restart 无效）；② 沙箱 2 在界面上叫 **`11`**、沙箱 3 叫 `111`，
   **必须精确文本匹配**，包含匹配会点错 —— `_HANDOFF.md:454-459`。
4. **出厂状态要设密码**：`python _automation\ensp_set_password.py 22 "Huawei@123"`。
   设备在 `Please Press ENTER.` 之后**完全静默**，要主动发回车推进；
   但**一旦看到 `Enter Password:` 就不能再发多余回车**（会被当成密码内容 →
   `Error: The password length must range from 8 to 16.` 然后清屏重来）—— `_HANDOFF.md:460-465`。
5. 单验连接：
   `python tests/engine_smoke.py --protocol eNSP --host 192.168.31.11 --port 8443 --sandbox-id 2 --node-id 22 --seconds 8 --login-password Huawei@123`
   —— `_HANDOFF.md:441`。

### 3.2 HCL（H3C Cloud Lab）—— H3C 真机的替代

| 项 | 值 |
|---|---|
| 安装 | `C:\Program Files (x86)\HCL\H3C Cloud Lab.exe`，版本 **HCL 5.10.3** |
| 引擎 | 自带 Simware（`SimwareClient`/`SimwareWrapper`/`SimwareMultiCC`），引擎控制口 **16600**，打开工程后工程服务口 **18600** |
| 虚拟化 | **不依赖 VirtualBox**（`VBoxSDS` 停着也能跑） |
| **设备控制台** | **`127.0.0.1:<telnet_port>`**，端口从 HCL 日志读：`telnet_port 30001` |
| 本轮实测设备 | **S6850**（H3C Comware **7.1.070, Alpha 7170**），提示符 `<H3C>` |

来源：`_HANDOFF.md:485-493`、`_automation/hcl_console.py:22-24`（`HOST=127.0.0.1`、`PORT=30001`）。

**三个坑（都花了很久定位）：**

1. **出厂设备卡在"自动配置循环"**：
   ```
   Automatic configuration attempt: 5.
   Not ready for automatic configuration: no interface available.
   Automatic configuration is running, press CTRL_C or CTRL_D to break.
   ```
   **必须发 Ctrl+C（0x03）打断**，打断后立刻给出 `<H3C>`；**只发回车没用**。
   —— `_HANDOFF.md:494-501`；实测脚本 `_automation/hcl_ctrlc.log`。
2. **复现前必须先清分页** —— **处于 `---- More ----` 时不回显**（见第 12.3 节与 `_HANDOFF.md`）。
3. **驱动 HCL 界面时点击会打在 DSH 自己的窗口上**：`WindowFromPoint` 返回的是
   `class=Chrome_RenderWidgetHostHWND, pid=DeepSeek Harness.exe`。
   "最小化 DSH"**不生效**（harness 每次工具调用自我抬升）→
   **把 DSH 窗口移到屏幕外**（`SetWindowPos(x=-1900)`），见 `_automation/hcl_uncover_and_click.py`。
   另：**HCL 当前台时会抓鼠标**（`SetCursorPos` 返回 0、光标钉住；键盘不受影响）。
   —— `_HANDOFF.md:507-516`。

**恢复/辅助脚本**：

| 脚本 | 作用 |
|---|---|
| `_automation/hcl_console.py dump\|logout` | 看原始回显 / 发 `quit` 把控制台退回"未登录" |
| `_automation/hcl_wait_console.py --timeout 150` | 等设备启动、自动抓控制台端口并读提示符 |
| `_automation/hcl_make_config.py --scan\|--port N` | 把控制台写成 AITerminal 的设备配置（`vendor=h3c`） |
| `_automation/hcl_sweep.py` | 按钮定位的有界扫描（HCL 是 PyQt 自绘，控件枚举看不到按钮） |

来源：各脚本 docstring；`_HANDOFF.md:503-521`。

### 3.3 物理锐捷设备（串口 console）

| 项 | 值 |
|---|---|
| 接口 | **COM5**（FTDI USB 串口线，`VID_0403&PID_6001`），**9600 8N1** |
| 设备 | 锐捷 RGOS，`Ruijie>`（用户模式）/ `Ruijie#`（特权模式） |
| **App 真正读的配置** | `C:\Users\linyin\.aiterminal\settings.json` |

来源：`_HANDOFF.md:467-472`。

**坑：**
- **锐捷查询用 `show`，没有 `display`**（敲 `display` 回 `% Unknown command.`）；
  进配置是 `configure terminal`，且**必须先到特权模式 `Ruijie#`**，用户视图下直接敲会被拒
  —— `_HANDOFF.md:473-476`。
- 锐捷的擦除序列是 `08 20 08`（BS 空格 BS），与华为 `ESC[1D` 不同 —— "退格"这条在锐捷上本来就对
  —— `_HANDOFF.md:477-478`。
- **一个 phase 一次全新连接**：串口没有"清屏"概念，App 每次是干净连接，
  但**设备侧那一行不会自己消失**，跨阶段复用会让残留文本粘到本轮输入上
  —— `_HANDOFF.md:479-482`。
- 复现脚本：`_automation/ait_repro5_serial.py --phase lat|bs|arrow|accept|seq`。

### 3.4 本地 Linux 沙箱 / 假设备（靶机全下线时的底线）

- 内置模拟设备（H3C）：`python tools/fake_device.py --port 2323 --no-login --paging` —— `docs/验证指南.md:20-22`。
- **本地假华为 VRP**（回显序列**照抄真机抓到的字节**）+ **真 SendInput 按键**：
  `_automation/fake_huawei_telnet.py`、`_automation/ait_kbd_fake.py --phase bs|arrow|tab|lat [--down N]`
  —— `_HANDOFF.md:69-74`。
- 元教训（务必守住）：**真机同时下线时不要退回程序化注入**；写本地假设备当对端，
  链路其余部分（真窗口/真引擎/真 Telnet/真 SendInput）全不动，
  断言看**对端实际收到了什么**而不是截图 —— `_HANDOFF.md:862-865`。
- EVE-NG（备用靶机，**空镜像库**）：`http://192.168.2.134/ admin/eve` v6.2.0-4-Community。
  168 个模板只有 vpcs 有镜像，**无 H3C 模板 → 厂商维度无法在这台验证** —— `_HANDOFF.md:523-531`。

---

## 4. 环境与坑速查表（现象 → 原因 → 做法）

| # | 现象 | 原因 | 做法 | 来源 |
|---|---|---|---|---|
| 1 | 界面**大片发灰**、颜色完全不是设计稿的色 | QSS 里有一个**未定义的 `@token`** 或**多余/缺失的 `}`** → Qt 报 `Could not parse application stylesheet` → **整份样式表被丢弃** → 所有控件退回 `QPalette`（Fusion `#2d2d2d`） | 改完 `dark.qss`（或 `Theme.cpp` 的 token 表）**必须跑** `python _automation\check_qss_tokens.py` | `_automation/check_qss_tokens.py:1-16`、`_HANDOFF.md:735-758` |
| 2 | 看不见 Qt 的样式表报错 | GUI 子系统平时看不见 stderr（Qt 在 Windows 默认走 `OutputDebugString`） | 加 `QT_FORCE_STDERR_LOGGING=1` + `Start-Process -RedirectStandardError`；**它不给行号** | `_HANDOFF.md:752-755` |
| 3 | 有些 token（如 `@fg-strong`）时好时坏、不可复现 | `Theme::substitute()` 原来用 `QHash` 的**随机化遍历顺序**做文本替换，而 token 名互相包含（`@fg` 是 `@fg-strong` 的前缀）→ 先替换短名会把长名破坏 | 已改为**按 key 长度降序**替换 —— 别再改回去 | `_HANDOFF.md:762-766` |
| 4 | 终端数字/IP 上色"代码写了却没生效" | 上色分支的前提是 `format.foreground().style() == Qt::NoBrush`，而**设备回显/初始 prompt 会带一个显式前景色** → 整条分支被跳过 | 用独立标志位区分"设备 ANSI 真指定过颜色"与"框架默认前景色" | `design/reviews/ui-review-02.md:19-29` |
| 5 | 按键"像是没进去"、命令粘在一起 | **`---- More ----` 分页时设备不回显**（分页吃得掉后续输入） | 先发空格/`q` 翻完页，或先关分页：H3C/华为 `screen-length 0 temporary`，Cisco `terminal length 0` | `README.md:147-149`、`docs/README.md:999-1000` |
| 6 | 截图里"没有原生标题栏"就以为去掉边框成功了 | `window.grab()` **只截客户区**，原生标题栏**永远不在截图里** → 截图证明不了任何事 | 判据必须是 **`GetWindowRect == GetClientRect`（非客户区 = 0）**，外加 `GWL_STYLE` 里没有 `WS_CAPTION/WS_THICKFRAME/WS_SYSMENU` | `_automation/ait_frameless_check.py:1-16` |
| 7 | 编译资源时 `cc1.exe: fatal error: AI/2026-09-21-…/aiterminal/cpp/src: No such file or directory` | 项目路径**含空格**（"WorkBuddy AI"），CMake 把 `-I "<含空格路径>"` 交给 windres，windres 转发 cc1 时**不会重新加引号** | `configure_file` 把 `.rc`/`.ico` 复制到**无空格的构建目录**再编，且**不给该 target 任何 `-I`**；用 **OBJECT 库**（静态库会丢无符号引用的资源对象） | `cpp/CMakeLists.txt:88-97` |
| 8 | 像素判色"命中了一堆旧色"，结论反了 | Windows **ClearType** 按通道分别混色，文字边缘产生**孤立的**旧色近似像素（假彩边） | 两个口径都看：① `raw` 精确色值（±2/通道）② `run3` **同色水平连续 ≥3 像素**。判"旧色是否还在用"看 `run3` | `_automation/verify_round08_pixels.py:1-16` |
| 9 | 用"彩色像素占比"判断新旧配色 → 结论与事实相反 | ClearType 假彩边让占比虚高 | 别用占比；用精确值/连续游程 | 同上 |
| 10 | 对话框出图前终端里**没有绿色状态行** | HCL 的 telnet 控制台**保留上一次会话的登录态**，不主动退出就不再打横幅/登录行 | 出图前先 `python _automation\hcl_console.py logout`（发 `quit`） | `_automation/hcl_console.py:1-12`、`_automation/shoot_round08.py:25-27` |
| 11 | 出图带上了桌面/任务栏/DSH 界面 | 整屏截图做不到干净；DSH 铺满全屏且抢前台 | 用**应用自带** `--screenshot=`（走 `window.grab()`，只截窗口）；驱动 GUI 前先 `park_dsh()`/`park_others()` 把别的窗口挪出屏幕 | `_automation/shoot_round08.py:9-13`、`_HANDOFF.md:803-805` |
| 12 | 补全浮层在截图里看不到 | 浮层曾是 `Qt::ToolTip` **顶层窗口**，不在 `window.grab()` 里 | **已根治**：浮层改成主窗口的子控件，现在窗口截图能覆盖到它；历史做法见第 12 节 | `_HANDOFF.md:202-226` |
| 13 | 从外面枚举顶层窗口判断"浮层在不在"→ 永远只数到 1 个 | `uictl` 枚举不到 `Qt::ToolTip` 浮层；`Driver.popup_present()` 在小浮层上返回 False | 让**控件自己报状态**（`completionPopupVisible()` / 问题包 JSON）；关键判断至少两个独立证据源 | `_HANDOFF.md:202-206`、`_HANDOFF.md:375-377` |
| 14 | 固定睡眠的断言在负载下**间歇性失败** | 机器忙时引擎应答晚到，抓到"还没弹" | 改成**轮询等目标状态**（100ms 一次、上限 8300ms），或断言"在 N ms 内出现" | `_HANDOFF.md:238-248` |
| 15 | 抓快照时 `popup_visible=False` 但候选明明有 | `visible` 是**瞬时值**，窗口失焦会被 `ApplicationDeactivate` 收掉 | 用**单调计数** `popup_show_count` 判"弹出过" | `_HANDOFF.md:266-268`、`tests/cpp_ui_regression.py:247-254` |
| 16 | `python -c "…"` 跑带引号/中文/正则的片段报 `SyntaxError` | PowerShell 双引号与 Python 字符串引号**两层打架**，报错行还长得像环境问题 | **超过一行、或含任何引号，就写成 `.py` 文件再跑** | `_HANDOFF.md:876-879` |
| 17 | `Get-Content \| Select-Object -Skip N` 读引擎写的日志**返回空** | 工具/编码问题，不是数据问题 | 换 `read` 工具读 —— **读不出来先换工具，别怀疑数据** | `_HANDOFF.md:880-881` |
| 18 | Python 打印中文乱码 | Windows 上 Python 默认可能用 GBK/cp936 | 跑中文脚本前 `$env:PYTHONUTF8=1`；引擎自己把 `stdout/stderr` 钉死在 UTF-8（`engine/main.py:37-51`）；C++ 侧注入 `PYTHONIOENCODING` | `_automation/*.py` 的"用法"段、`engine/main.py:37-49` |
| 19 | 断言不通过的探针被"反卡死闸/纪律卡"当成方案失败而拦下重跑 | 探针 `sys.exit(1)` 与"命令失败"在守卫看来一样 | 断言**落到项目自己的测试文件**里跑（`tests/*.py`）；失败后**不要原样重跑**，改完再跑一个不同指纹的命令 | `_HANDOFF.md:883-887` |
| 20 | 程序"挂住不退出"（其实是管道问题） | GUI 程序派生的引擎孙进程继承了管道写端，`communicate()` 等不到 EOF | 用**临时文件**收输出，不用 `capture_output=True` | `tests/cpp_ui_regression.py:41-48` |
| 21 | `--screenshot=D:\a b\x.png` 没生效 | 路径有空格？不是 —— **必须是等号形式**；另外**输出目录必须先建好** | `--screenshot=<绝对路径>`，先 `New-Item -ItemType Directory` | `tests/cpp_ui_regression.py:109-112`、`_HANDOFF.md:419` |
| 22 | 对话框尺寸看着别扭/被屏切掉 | 本机屏幕可用逻辑高度 816（1536×816） | 对话框默认宽 **560–960**（表 ≤1100）、高 **≤ 可用高 82%（→669）** + `setMinimumSize`；**量 PNG 的逻辑尺寸 = 像素 / 1.25** | 记忆条目（`~/.dsh/memories/MEMORY.md`），本轮未在源码中逐条复核 → **未验证到具体行** |
| 23 | `netstat`/DLL 锁住导致构建失败 | exe 正在运行 | 构建前 `Get-Process AITerminal \| Stop-Process -Force` | `_HANDOFF.md:409` |

---

## 5. 设计与配色的决议（最终采用什么、为什么）

### 5.1 各设计轮次是什么 / 结论是什么

| 轮次 | 是什么 | 交付物 | 结论 |
|---|---|---|---|
| round-01 / 02 | 早期样张与风格探索（无 README） | `style-explorer.html`、`manifest.json`、`img/` | 只作为过程产物；**未确认**其结论（没有 README） |
| round-03 | **重新评判 + 三个全新方向**（用 `lhg-ui-review` 评判 + `oil-ui` 起方向） | `README.md`、`candidates/d1/d2/d3-*.html`、`shots/` | 判定"不行在**设计语言层**（构图与信息层级），不是执行层"；定方向 **D1「机架轨道」** |
| round-04 | **全 8 屏重画**（DSH 深色外壳 + 终端方案 A） | `screens.html`、`shots/01..08`、`shots/00-all`、`_shots.py` | **只画图，没碰任何 C++/QSS**；8 屏**没有在真机 Qt 上验证过**；旧 `Theme.cpp` 配色与稿子新配色**是两套东西，落地需一次 token 替换** |
| round-05 | **三套深色配色重设**（只换颜色） | `p1-graphite-teal.html`、`p2-mauve-purple.html`、`p3-achromatic-invert.html`、`shots/compare.png` | 推荐 **P1 石墨青**；明确规避历史否决点（无近纯黑、非纯饱和红、无青蓝外壳抢主按钮、数值色非金色） |
| round-06 | **照参考图（Netcatty）实测取色**搬进稿子（只换颜色） | `screens.html`、`shots/`、`_scripts/` | 全部色值来自参考截图**量化取色**，每项记采样位置 |
| round-07 | **不存在**（`design/` 下没有该目录） | — | — |
| round-08 | **配色重设（深色，颜色组合借参考图）** | `screens.html`、`shots/00-all`、`01..08`、`compare.png`、`_scripts/` | **最终定稿**：20 角色表、硬约束 20/20 全过；用户口径「就按这个，其余界面照这版设计」 |

来源：`design/round-03/README.md:1-9`、`design/round-04/README.md:1-8` 与 `:220`、
`design/round-05/README.md:1-21`、`design/round-06/README.md:1-14`、
`design/round-08/README.md:1-22`、`design/round-08/README.md:172-196`；`design/` 目录实测无 `round-07`。

> **⚠️ 事实边界（必须写清楚）**：round-04 ~ round-08 **都是稿子**（`screens.html` / `shots/*.png`），
> `round-04/README.md:220` 原文承认"没有在真机 Qt 上验证过…落地需要一次 token 替换，本轮没做"。
> **只有 round-08 的配色 + 终端"蓝绿双色"是已落地到应用的**（落在 `cpp/src/core/Theme.cpp` + `cpp/resources/theme/dark.qss`）。
> 其它轮次的稿子**不是已实现功能**。

### 5.2 round-08 最终角色表（20 角色）→ 落地 token

来源：`design/round-08/README.md:172-196`（表 B）、`:137-160`（表 C）；
落地映射见 `cpp/src/core/Theme.cpp:24-61`（逐条映射理由）与 `:67-130`（实际值）。

| # | 角色 | 稿子 hex | 落地到 `Theme::Col::*` | QSS token |
|---|---|---|---|---|
| 1 | `bg` | `#282b30` | `Window` | `@bg-window` |
| 2 | `panel` | `#373a3f` | `Panel` | `@bg-panel` |
| 3 | `card` | `#41454a` | `Raised`；**同时**是 `AccentSoft` 的面 | `@bg-raised` / `@accent-soft` |
| 4 | `sunk` | `#151b2b` | `Sidebar`（导航）与 `Surface`（输入/终端/凹面）**同为 sunk** | `@bg-sidebar` / `@bg-surface` |
| 5 | `line` | `#4b4f56` | `Border`；**派生** `Hover`、`TermSel` 也取它 | `@border` / `@bg-hover` / `@term-sel` |
| 6 | `line_strong` | `#636870` | `BorderSoft`；**派生** `Active` 也取它 | `@border-soft` / `@bg-active` |
| 7 | `fg` | `#f3f5fc` | `Text`、`TextStrong`（标题靠 600 字重，**不另开亮度档**）、`TermFg` | `@fg` / `@fg-strong` / `@term-fg` |
| 8 | `fg2` | `#bdc1c9` | `TextMuted` | `@fg-muted` |
| 9 | `fg3` | `#92969f` | `TextSubtle` | `@fg-subtle` |
| 10 | `fgph` | `#828690` | `TextDisabled` | `@fg-disabled` |
| 11 | `accent` | `#1e6fd9` | `Accent`、`Action`、`BorderFocus`、`TermCursor` | `@accent` / `@action` / `@border-focus` |
| 12 | `accent_hover` | `#3373d9` | `AccentHover`、`ActionHover` | `@accent-hover` / `@action-hover` |
| 13 | `accent_text` | `#95a9ee` | `AccentText`、`Info` | `@accent-text` / `@info` |
| 14 | `onaccent` | `#ffffff` | `OnAction` | `@on-action` |
| 15 | `ok` | `#5bbe7d` | `Ok` | `@ok` |
| 16 | `warn` | `#f18a4c` | `Warn` | `@warn` |
| 17 | `danger` | `#fa8377` | `Err` | `@err` |
| 18 | `t_net` | `#52c6cf` | **刻意不引入** | — |
| 19 | `t_num` | `#e1b562` | **刻意不引入** | — |
| 20 | `t_pmt` | `#a8b6d6` | **刻意不引入** | — |

**终端提示双色（用户刚验收过，不动）**：

| token | 值 | 对比度（在终端底 `#151b2b` 上） | 语义 |
|---|---|---|---|
| `TermPrompt` | `#78BAEA`（色相 208°） | **8.18:1**（AA/AAA 均过） | 蓝：提示符 / 命令行 / IP |
| `TermOk` | `#5BBE7D`（色相 143°） | **7.44:1**（AA/AAA 均过） | 绿：成功与状态类提示 |

来源：`cpp/src/core/Theme.cpp:113-130`（含对比度实测与"为什么用这支蓝/这支绿"的完整理由）。

### 5.3 为什么 —— 用户**明确否决过**什么（下次别重犯）

| 被否决的东西 | 用户口径 / 证据 | 来源 |
|---|---|---|
| **近纯黑 + 扁平** | "像早期开发者工具"；round-05 起刻意把最暗面抬到 L\*10.6（现状曾是 4.3） | `design/round-05/README.md:43`；用户档案 |
| **纯饱和红做危险色** | "突兀" → danger 三套分别是 Radix red-11 `#ff9592` / Tailwind rose-300 `#fda4af` / red-300 `#fca5a5`；round-08 定为 `#fa8377`（**同色相降彩度**，且**只当文字与描边、不填充**） | `design/round-05/README.md:43`、`design/round-06/README.md:140`、`design/round-08/README.md:157`、`cpp/src/core/Theme.cpp:99-101` |
| **UI 外壳用青蓝**（抢主按钮） | round-05 三套都"没有青蓝外壳抢主按钮"；round-03 自评 D2 最可能被否的就是"水蓝就是刚被否决的青蓝" | `design/round-05/README.md:43`、`design/round-03/README.md:168` |
| **同一屏 5 种绿**（信号超载） | 走查实测 `02b` 里 `#22C55E` 出现 4 处 + `#A3BE8C` 2 处 + 头像品牌红绿 → 13px 下不可分辨语义。**round-08 收敛成一个绿 `#5bbe7d`**，且 UI 的"成功"与终端的"成功"是**同一个绿**（`Ok` 与 `TermOk` 同值） | `design/reviews/ui-review-02.md:42-60`、`:253-256`；`design/round-08/README.md:583`；`cpp/src/core/Theme.cpp:97`、`:124-126` |
| **sage 绿 `#A3BE8C`** | "偏灰、不够成功" → TermOk 取管理台正绿 `#5BBE7D`；且 `AccentText` 原来是 sage 绿 `#A3BE8C` 且**已无使用点**，正好复用为 `accent_text #95a9ee` | `cpp/src/core/Theme.cpp:48-51`、`:124-126` |
| **金色数值** | round-05 明确"数值色**不是金色**"；`t_num` 是琥珀 `#e1b562`，且 round-08 的 `t_num/t_net/t_pmt` **一律不引入**（用户口径："不要多姿多彩"） | `design/round-05/README.md:43`、`design/round-08/README.md:159`、`cpp/src/core/Theme.cpp:127-128` |
| **"关键数据靠颜色突出"那条绿被误用** | 走查 P0：终端数字/IP 上色**在实机截图里完全没生效**（`#A3BE8C` 精确 0 命中） | `design/reviews/ui-review-02.md:17-29` |
| **状态栏把文字染成和状态点一样的亮绿** | "色点=状态"的约定被自毁 → 文字回 `@fg-muted`，绿只留给圆点 | `design/reviews/ui-review-02.md:62-66` |
| **卡片化 / 自己发明一套视觉** | 用户回"**还是变了**"。真相：项目**本就有完整设计系统**（`design/system/tokens.json`、`DESIGN-SYSTEM.md`、`mockups/`），我的卡片化正好反着来，已逐条精确回退 | `_HANDOFF.md:678-703` |

### 5.4 round-08 的自检数字（可复算）

来源：`design/round-08/README.md:203-230`（表 D，全部由 `_scripts/palette.py check` **现算**，不是手抄）。

| 约束 | 实测 |
|---|---|
| `fg / bg` ≥ 12:1 | **13.04:1** |
| `fg2 / bg` ≥ 6:1 | **7.87:1** |
| `fgph / sunk` ≥ 4.5:1 | **4.71:1** |
| `onaccent / accent` ≥ 4.5:1 | **4.85:1** |
| `accent_text / panel` ≥ 4.5:1 | **4.99:1** |
| `panel` 对 `bg` ΔL\* ≥ 6 | **6.89** |
| 导航(sunk) 比内容(bg) 更深 | **ΔL\* = 7.49** |
| `t_net/t_num/t_pmt` 两两 ΔE76 ≥ 15 | **71.8 / 31.9 / 65.7** |
| 面阶单调 | sunk 9.9 < bg 17.4 < panel 24.3 < card 29.1 < line 33.5 < line_strong 43.8 |
| 硬约束总计 | **20/20 通过** |

---

## 6. AI 的作用范围（用户 2026-10 明确定义）

**三条允许的用途：**
1. **技术问答**
2. **可行性判断**（"我问这个想法可以实现吗"）
3. **整理知识与命令**

**红线（最重要）：**
> **能不用 AI 就不用。** 有确定性办法（正则 / 字段抽取 / 表格导入 / 模板转换）一律走**确定性路径**；
> AI 只**整理已有原文**，**不得产生新事实**（无来源就留空 + 空状态）。

来源：`C:\Users\linyin\.dsh\memories\USER.md` 第 8 条（用户档案原文）。

**代码里怎么体现这条红线（已落地）：**

| 位置 | 怎么体现 |
|---|---|
| `core/vendor_knowledge.py:11-12` | "**这一层只读、只做确定性查找**：没有任何 AI 调用，也不生成内容。数据不在（本机还没建）就返回明确的'没有'，由界面显示空状态 —— **不编**。" |
| `core/vendor_knowledge.py:31-32` | 没有技术讲解时的统一文案 `EMPTY_REASON`："该命令暂无技术解释…" —— 保证"**绝不静默**" |
| `core/command_suggest.py` | 补全三层（token / next / sequences）是**确定性索引 + 打分**，不调 LLM |
| `core/safety.py` + RPC `safety.check` | 危险命令是**代码级黑名单**，不靠提示词 |
| `tests/smoke_test.py:677-728` | "命令表完全改成程序侧来源：内置厂商表 + 资料库整理出的命令"，并断言**不再向设备敲 `?`** 取命令表 |

---

## 7. 知识库结构（用户定义）

### 7.1 目录结构（**批次三的脚本会按这个落数据**）

```
knowledge/<vendor>/                 # vendor: h3c / huawei / ruijie
  commands.sqlite                   # ① 命令表（一条命令一行）
  chapters/<章节>.md                # ② 技术讲解：每个章节一个 md，带 <a id="..."> 锚点
  examples/<...>.md                 # ③ 例子：单独文件
  index.json                        # ④ 索引：命令/章节 → 文件#锚点
```

来源：`core/vendor_knowledge.py:3-9`（docstring 原文）；`COMMANDS_DB = "commands.sqlite"` 见 `:29`。

**键设计要点：**
- 索引按**锚点**切片，不是把整篇塞进去 —— "知识库面板要'跳到对应位置'，
  把整篇塞进去用户还得自己找"：`_slice_anchor()` 从锚点切到**下一个锚点或下一个二级标题**为止
  —— `core/vendor_knowledge.py:61-77`。
- 每个条目带 `source`（必须是**厂商文档**），"用来给'这段话哪来的'一个可追溯的落点"
  —— `core/vendor_knowledge.py:125-126`。
- **数据不在就返回明确的"没有"**，不抛异常（`load_index()` 返回空结构）
  —— `core/vendor_knowledge.py:43-53`；`stats()` 给界面做空状态判断 —— `:131-141`。
- 根目录可用环境变量覆盖（自测指向临时目录）：`AIT_KNOWLEDGE_DIR`
  —— `core/vendor_knowledge.py:35-36`。

### 7.2 现状

| 项 | 状态 |
|---|---|
| 读数据层（批次二） | ✅ 已实现：`core/vendor_knowledge.py` |
| RPC 暴露 | ✅ `knowledge.command`（`engine/service.py:124`）；另有 `knowledge.stats/list/search/index/import/delete` 给**资料库**用（`:96-101`） |
| 建库脚本（批次三） | ❌ **尚未开工**（"批次三会往这里加 `build_*`"） |
| `knowledge/` 数据目录 | ❌ **本轮实测不存在** |

> ⚠️ 别把这一节和"**本地资料库**（`core/knowledge.py`，落在 `~/.aiterminal/knowledge/`，
> 索引文件 `.index.json`，用户自己导手册/存笔记）搞混 —— **那是另一套东西**：
> 来源 `docs/README.md:489-491`、`:738`、`core/knowledge.py:35`。

### 7.3 命令数据的真源（已经有的那份）

| 厂商 | 文件 | 行数 | command | example | 唯一命令名 | 大小 |
|---|---|---|---|---|---|---|
| h3c | `_vendor_cmd/h3c/commands.jsonl` | 9,377 | 8,556 | 821 | 2,823 | 3.48 MB |
| huawei | `_vendor_cmd/huawei/commands.jsonl` | 57,828 | 29,835 | 27,993 | 12,441 | 21.09 MB |
| ruijie | `_vendor_cmd/ruijie/commands.jsonl` | 18,108 | 12,387 | 5,721 | 3,377 | 5.47 MB |
| **合计** | | | | | | **85,313 条 / 30.06 MB** |

来源：`_vendor_cmd/README.md:8-14`；自检输出 `_vendor_cmd/_verify_all.txt`；解析工具 `_vendor_cmd_tools/`。

**已知局限（原文如实说明，别当没有）：**
- **H3C 的 `view` 是推断值**，不是文档原文（该 CHM 没有「视图」小节，来源是页面前言 + 表格状态机）
  —— `_vendor_cmd/README.md:99-101`。
- H3C 命令名依赖 `<b>` 加粗标记，极少数页面只加粗第一个关键字
  （例：`ipv6 dhcp server forbidden-address` 落成 `ipv6`）—— `:102`。
- **锐捷命令名是启发式**（前缀 + 真机实例校验），个别偏长/偏短；`syntax` 字段始终是**原文语法行**
  —— `:103`、`:107-108`。
- 华为 `example` 里含大量「使用实例」（24,953 条，短序列）—— `:104`。
- 未做全量覆盖：HTML 共 96,165 个，只读了与命令/举例相关的**约 7 万个**页面 —— `:109-110`。

**复现命令**（`_vendor_cmd/README.md:114-122`）：

```powershell
$env:PYTHONUTF8=1
cd C:\Users\linyin\deepseek_harness\_vendor_cmd_tools
python extract_h3c.py      # ~1.5 s
python extract_huawei.py   # ~5.5 s
python extract_ruijie.py   # ~14.7 s
python verify.py C:\Users\linyin\deepseek_harness\_vendor_cmd\h3c\commands.jsonl
python coverage.py
```

---

## 8. 文件地图（想知道"某功能在哪改"时看这里）

### 8.1 C++ 界面层（`cpp/src/`）

| 功能 | 文件 / 类 | 一句话作用 |
|---|---|---|
| 程序入口 + 命令行开关 | `cpp/src/main.cpp` | 解析 `--screenshot/--shot-dialog/--autoconnect/--autocmd`；装 Fusion 样式 + 全局手型光标；无边框窗口装配 |
| 设计系统单一真源 | `cpp/src/core/Theme.{h,cpp}`、`namespace Theme::Col/Dim/Font` | 所有颜色/尺寸/字号只在这里定义；`tokenMap()` 把 `@token` 映射成色值 |
| 样式表 | `cpp/resources/theme/dark.qss` | 只写 `@token`，不写死色值；改完必跑 `check_qss_tokens.py` |
| 引擎客户端（RPC） | `cpp/src/core/EngineClient.{h,cpp}` | 拉起 `python -u engine/main.py`，stdin 写请求、stdout 收结果与通知 |
| ANSI 解析 | `cpp/src/core/AnsiParser.{h,cpp}` | 颜色/擦除序列 → 渲染指令；**CSI nJ/nK 吐哨兵字符 `0x0B`**（`kEraseToLineEnd`） |
| 主窗口 | `cpp/src/ui/MainWindow.{h,cpp}` | 菜单、会话标签、安全闸门、`sendToSession()`（**唯一发送入口**，字节日志挂这里）、`submitLine()` |
| 自绘标题栏 | `cpp/src/ui/TitleBar.{h,cpp}` | 无边框窗口的顶行：菜单合并（`setMenuWidget`）+ 拖动 + 最小化/最大化/关闭 |
| 终端视图（最大） | `cpp/src/ui/TerminalView.{h,cpp}`（65 KB） | 渲染、键盘、**命令补全浮层**（含 `repositionCompletionPopup()`）、真机诊断日志 `AIT_KEYLOG`/`AIT_BYTELOG` |
| 补全候选绘制 | `TerminalView.cpp` 匿名命名空间的 `CandidateDelegate` | PyCharm 风格双色排版（命令名正常色 + 说明弱化色，说明限宽 240px） |
| 左侧导航条 | `cpp/src/ui/NavRail.{h,cpp}` | 56px 图标导航（`Dim::NavRailWidth = 56`） |
| 设备库 / Vault | `cpp/src/ui/HostListPanel.{h,cpp}`、`HostDelegate.{h,cpp}` | 设备列表与自绘行；`vendorIconPixmap()` 找 `vendor-icons/`；选中态画 **2px `@accent` 竖条** |
| 会话标签栏 | `cpp/src/ui/SessionTabBar.{h,cpp}` | 标签拖动/关闭；激活态是**底部下划线** |
| 状态栏 | `cpp/src/ui/StatusStrip.{h,cpp}` | 连接数、安全闸门、API 状态三态点 |
| 连接对话框 | `cpp/src/ui/ConnectDialog.{h,cpp}` | 按协议切换字段（eNSP 多出沙箱 ID / 设备 ID） |
| 资料库对话框 | `cpp/src/ui/KnowledgeDialog.{h,cpp}` | `knowledge.stats` + `knowledge.list` 的界面外壳 |
| AI 助手面板 | `cpp/src/ui/AiPanel.{h,cpp}` | 对话 / 任务 / 待执行队列 |
| AI 设置 | `cpp/src/ui/AiSettingsDialog.{h,cpp}` | 提供方 / 密钥 / 模型 / 接口地址；"测试连通"**降为次级按钮**，保存是唯一主按钮 |
| 多设备巡检 | `cpp/src/ui/PatrolPanel.{h,cpp}` | 结果表 stretch 3 是主角、输出限高 150 |
| 差异比对 | `cpp/src/ui/DiffDialog.{h,cpp}` | 输入 stretch 2 / 逐行差异 stretch 3，底栏一行收全部 |
| 问题包 | `cpp/src/ui/ProblemReport.{h,cpp}` | 帮助→反馈给 AI（Ctrl+Shift+D）；落盘 `%APPDATA%\AITerminal\AITerminal\reports\problem-<时间戳>\` |
| 数据模型 | `cpp/src/model/Host.{h,cpp}`、`Session.{h,cpp}` | 设备与会话结构 |
| 资源清单 | `cpp/resources/resources.qrc`、`cpp/resources/app.rc` | Qt 资源 / exe 静态图标（windres） |
| 厂商图标 | `cpp/resources/vendor-icons/*.png` | 12 个（cisco/huawei/h3c/ruijie/juniper/netgear/paloalto/fortinet/mikrotik/redhat/ubuntu/aws） |

来源：`cpp/CMakeLists.txt:23-76`（完整源文件清单，与上表一一对应）。

### 8.2 Python 引擎与核心（`engine/`、`core/`、`config/`）

| 功能 | 文件 | 一句话作用 |
|---|---|---|
| 引擎入口 | `engine/main.py` | 强制 UTF-8 流、`sys.path` bootstrap、起 JSON-RPC 服务 |
| RPC 协议 | `engine/rpc.py` | JSON Lines over stdio；**stdout 只允许 JSON**，所有诊断写 stderr |
| RPC 方法实现 | `engine/service.py` | 方法注册表 + 会话句柄 + 30ms 轮询把输出推成通知 |
| 连接基类 | `core/connector.py` | `BaseConnector`（**继承 QObject → 引擎必须能 import PyQt6**，但不需要 `QApplication`） |
| SSH / Telnet / Serial / eNSP | `core/{ssh,telnet,serial,ensp}_connector.py` | 四种连接实现 |
| 终端模拟 | `core/terminal.py` | 缓冲清洗（ANSI / `\r` 覆盖 / 分页） |
| 命令补全 | `core/command_suggest.py`（43 KB） | 三层 token/next/sequences；序列匹配键 = 去掉尾部参数的命令名前 1~2 词；**装载必须两遍** |
| 厂商知识库访问 | `core/vendor_knowledge.py` | 业务批次二：命令表/章节/索引的**确定性只读查找** |
| 本地资料库 | `core/knowledge.py`（44 KB） | 导入 / 抽取正文 / 分块 / 检索 / `.index.json` 增量索引 |
| AI | `core/ai_agent.py`、`core/llm.py` | 智能体（`converse`/`ai_step`）、多提供方 LLM 客户端 |
| 联网检索 | `core/web_search.py` | 百度/必应/DuckDuckGo/SearXNG 后端 |
| 安全分级 | `core/safety.py` | 危险命令黑名单 + 配置类判定 |
| 批量下发 | `core/batch.py` | 等提示符判定（自动翻页、`[Y/N]` 停下） |
| 并发巡检 | `core/patrol.py` | 多设备线程池 |
| 配置比对 | `core/diff.py` | 纯函数、无外部依赖 |
| Excel 写出 | `core/xlsx_writer.py` | 纯标准库写 xlsx（多工作表 + 样式 + 非法字符清理） |
| 笔记/手册 | `core/manual.py` | 笔记筛选 + 导出 Markdown / HTML / CSV |
| 日志 | `core/logger.py` | 按日期/设备落盘 `~/.aiterminal/logs/` |
| 配置/预设/模板 | `config/settings.py`（23 KB）、`config/presets.py`、`config/templates.py` | 全局配置 / 厂商快捷命令 / 命令模板库 |

来源：`engine/*.py`、`core/` 目录实测清单、`docs/README.md:194-660`（逐模块说明）。

### 8.3 RPC 方法全表（`engine/service.py:80-120`）

```
engine.hello / engine.status        hosts.list / hosts.save / hosts.delete
safety.check                        session.open / write / command / close / resize / list
knowledge.stats / list / search / index / import / delete / command
ai.status / ai.ask / ai.getSettings / ai.setSettings / ai.test
patrol.run                          diff.compare            commands.suggest
```

**推送通知**（非请求）：`session.output`、以及会话状态变化（由 `_poll_loop()` 每 30ms 产生）
—— `engine/rpc.py:5-9`、`engine/service.py:67-69`。

---

## 9. 常见问题速查 Q&A

| # | 问题 | 一句话答案 | 跳转 |
|---|---|---|---|
| 1 | 界面**大片发灰**、颜色全不对，怎么办？ | QSS 里有一个坏 `@token` 或括号不配对 → **整份样式表被丢弃**。跑 `check_qss_tokens.py` | `_automation/check_qss_tokens.py`；第 12.1 节 |
| 2 | 想看 Qt 到底报了什么样式表错？ | `QT_FORCE_STDERR_LOGGING=1` + `Start-Process -RedirectStandardError`；**它不给行号** | `_HANDOFF.md:752-755` |
| 3 | 改了 C++ 代码但界面没变？ | 构建产物在 `%TEMP%\ait_cpp_build\`，不是 `cpp\build`，也不是 `dist\`；先重新构建再出图 | 第 2.1 / 2.2 节 |
| 4 | 构建报 `ld error` / 文件被占用？ | 先 `Get-Process AITerminal \| Stop-Process -Force` | `_HANDOFF.md:409` |
| 5 | 构建报找不到 `Qt6Config.cmake`？ | PATH 前缀没设：`D:\Qt\Tools\mingw1310_64\bin;D:\Qt\6.8.3\mingw_64\bin;` | `_HANDOFF.md:410` |
| 6 | 截图脚本卡住不退出？ | 别用管道收输出（孙子进程继承写端）→ 用临时文件；超时用 `taskkill` | `tests/cpp_ui_regression.py:41-48`、`:60-62` |
| 7 | `--screenshot=` 没生成图？ | ① 必须**等号**形式 ② **输出目录要先建好**（否则 `save()` 静默失败） | `tests/cpp_ui_regression.py:109-112`、`_HANDOFF.md:419` |
| 8 | 想看某个对话框长什么样、但不想手点？ | `--shot-dialog=<7 个合法值之一> --screenshot=<png>`；拼错会**报错退出**不静默 | 第 2.2 节 |
| 9 | 截图里看不到补全浮层？ | 浮层现在是主窗口**子控件**，窗口截图能拍到；历史（顶层窗口时代）要抓物理屏 | `_HANDOFF.md:225-226` |
| 10 | 怎么判断"无边框窗口"真的生效了？ | **非客户区 = 0**（`GetWindowRect == GetClientRect`）；`grab()` 截图证明不了 | `_automation/ait_frameless_check.py`；第 12.4 节 |
| 11 | 像素判色老是被"假彩边"骗？ | ClearType 假彩边；只认**精确值（±2/通道）** 或 **同色水平连续 ≥3px** | `_automation/verify_round08_pixels.py`；第 12.6 节 |
| 12 | 怎么知道某张图属于哪套色板？ | `python _automation\sample_palette.py`：采样像素找最近 token | `_HANDOFF.md:759-760` |
| 13 | Python 脚本打印中文乱码 / 报错？ | 先 `$env:PYTHONUTF8=1`；超一行或含引号的片段**写成 .py 文件再跑** | 第 4 节 #18 / #16 |
| 14 | 设备输出卡在 `---- More ----`？ | 先翻页或先关分页：H3C/华为 `screen-length 0 temporary`，Cisco `terminal length 0` | `docs/README.md:999-1000` |
| 15 | 按退格"删不掉字、只多个空格"？ | 真因在**显示层**：设备用 CSI 序列擦除，解析器丢了 `CSI nD`/`CSI nJ`。看 `AIT_BYTELOG`，别只看 `AIT_KEYLOG` | 第 12.8 节 |
| 16 | 补全一个候选都不弹？ | 厂商推不出来（设备名如 `Console`）→ 靠设备**提示符**兜底认厂商；仍失败就是提示符不规范 | `_HANDOFF.md:292-312` |
| 17 | 补全把**别家厂商**的命令拿来了？ | **设备提示符才是权威**（配置里写的厂商会被推翻）；只在末尾 96 字符找、取最后一个匹配 | `_HANDOFF.md:90`、`:100-107` |
| 18 | `<H3C>` 下敲 `int` 一个候选都没有？ | 正确行为：用户视图里确实没有 `int…` 命令。**硬过滤不退回全量**（宁可漏、不可错） | `_HANDOFF.md:148-157` |
| 19 | 浮层飘在别的应用上、必须点一下才消失？ | 曾是 `Qt::ToolTip` 顶层置顶窗口且无失焦处理；**已根治**为子控件（被主窗口裁剪） | `_HANDOFF.md:170-226` |
| 20 | 真机复现前必须做什么？ | ① 先 `hcl_console.py logout`（登录态跨连接保留）② 先清分页 ③ 出图前 kill 残留进程 | 第 12.3 / 12.7 节 |
| 21 | 驱动 GUI 时点击"打在别的窗口上"？ | DSH 铺满全屏抢前台 → **把 DSH 窗口移到屏幕外**；点击用物理坐标；首次点击只激活 → **点两次** | `_HANDOFF.md:507-516`、`:603-605` |
| 22 | 保存配置为什么"重启后没了"？ | `save`（H3C/华为）/ `write`（锐捷）写的是**下次启动配置**；只改运行配置不保存，重启就丢 | 第 11.6 节 |
| 23 | `undo` 和 `no` 怎么区分？ | H3C/华为用 `undo`，Cisco/锐捷用 `no`（都是取消/删除配置的前缀） | 第 11.6 节 |
| 24 | Python 引擎起来了但"AI 不可用"？ | 所有 `core` 导入是**惰性**的，就是为了让 `engine.hello` 仍能回答并把"引擎在但 core 不可用"显示给你 | `engine/service.py:5-7` |
| 25 | 怎么把一次问题完整交给 AI？ | `帮助 → 反馈给 AI（收集问题包）…`，Ctrl+Shift+D；含屏幕/按键/原始字节/浮层状态 | `_HANDOFF.md:559-573` |

---

## 10. 未完成 / 下一步

### 10.1 当前排期（**批次制**，来自 `core/vendor_knowledge.py` 的分批命名）

| 批次 | 内容 | 状态 | 证据 |
|---|---|---|---|
| **批次一** | 厂商命令数据抽取 + 引擎 `commands.suggest` + C++ 补全浮层 + 真机端到端 | **已交付** | `core/vendor_knowledge.py:1`（"批次二读"）暗示批次一已完；`_HANDOFF.md:28-47` 四步全 ✅ |
| **批次二** | 厂商知识库的**结构化数据访问层**（读：`explanation()` / `stats()` / `load_index()`，无 AI） | **在跑 / 已落地** | `core/vendor_knowledge.py:1-16`；RPC `knowledge.command` 已注册（`engine/service.py:122-124`） |
| **批次三** | 厂商知识库的**建库**（`commands.sqlite` + `chapters/*.md` + `examples/*.md` + `index.json`） | **排队中（未开工）** | `core/vendor_knowledge.py:15`"批次三会往这里加 `build_*`"；本轮实测 `knowledge/` 目录**不存在** |

> ⚠️ 「批次一/二/三」这个划分只在 `core/vendor_knowledge.py` 的注释里有直接文本依据；
> 用户口径的"批次一已交付、批次二在跑、批次三排队"与之一致，但**没有找到一份独立的排期文件**
> —— **未确认**是否存在其它排期文档。

### 10.2 已知缺口（按"该不该现在做"排序）

| 优先级 | 缺口 | 依据 |
|---|---|---|
| 高 | **批次三：建厂商知识库数据**（当前 `knowledge/` 不存在，`knowledge.command` 只能返回空状态） | `core/vendor_knowledge.py:15`、本轮目录探测 |
| 高 | **安全闸门点 No 后设备行未清**（`MainWindow::submitLine` 的 `answer != Yes` 分支补发 `0x15`） | `_HANDOFF.md:538-539` |
| 中 | **ConnectDialog 加厂商下拉**（Huawei/H3C/Ruijie/自动）作为嗅探失败的手动兜底 | `_HANDOFF.md:553-555` |
| 中 | **`---- More ----` 分页界面提示条**（"设备在分页显示 · 继续"，点一下发空格） | `_HANDOFF.md:842-843` |
| 中 | 其余对话框的**交互自动化测试**（现在只测"能渲染"） | `tests/cpp_ui_regression.py:393-404` |
| 低 | **操作设备界面（主窗口）结构重做**（设备名在 3 处出现可收敛） | `_HANDOFF.md:840-841` |
| 低 | 菜单栏 5 项 → 设计稿的 7 项 | `_HANDOFF.md:844` |
| 低 | 旧 Python 界面 v0.8 计划（插件系统、巡检回灌资料库、向量检索…） | `docs/README.md:982-989` |

---

## 11. 网络知识速查

> **本节口径**：
> - 标 **【本地数据】** 的语法/视图，来自项目已抽取的结构化命令库
>   `_vendor_cmd/{h3c,huawei,ruijie}/commands.jsonl`（由 `_vendor_cmd_tools/` 从厂商原始文档抽取，
>   自检输出 `_vendor_cmd/_verify_all.txt`）。括号 `[...]` / `{...}` 是**原文语法占位符**，不是照抄即可用的命令。
> - 标 **【厂商原文】** 的，来自 `_vendor_docs/` 里厂商**原始文档**的正文（锐捷 `fulltext.txt`，行号可回溯）。
> - 标 **【官方站点】** 的，是我本轮联网核到的官方站点链接。
> - **查不到 / 拿不准的写「未确认」**，不编。

### 11.1 三大厂商命令体系对照表

**先记住一句话：H3C 与华为用 `display`，锐捷用 `show`；H3C/华为用 `undo`，锐捷用 `no`。**

来源：`_HANDOFF.md:474-476`（锐捷 `display` 会被拒）、`_vendor_cmd/README.md:12`（锐捷 `display*` 命令数 = 0）。

| 任务 | H3C（Comware V7） | Huawei（VRP） | Ruijie（RGOS） |
|---|---|---|---|
| **查版本** | `<H3C>` 用户视图：<br>`display version` 【本地数据】 | `<HUAWEI>` 用户视图：<br>`display version` 【本地数据】 | `Ruijie#` 特权模式：<br>`show version [ detail ]` 【厂商原文 `ruijie_n18000/fulltext.txt:2170`】 |
| **查接口（简表）** | `display interface brief` —— ⚠️ **未确认**（本地数据 h3c 侧未见该命令名；同类可见 `display interface vlan-interface [ brief [ down ] ]`） | `display interface brief [ ifType ]` 【本地数据，所有视图】 | `show interfaces [ interface-type interface-number ] status` 【厂商原文 `:13300-13301`】 |
| **查接口（详细）** | `display interface [ interface-type [ interface-number \| interface-number.subnumber ] ]` 【本地数据，任意视图】 | `display interface [ interface-type [ interface-number ] \| slot slot-number ]` 【本地数据，所有视图】 | `show interfaces [ interface-type interface-number ]` 【厂商原文 `:13300`】 |
| **查接口 IP** | `display ip interface [ interface-type interface-number ]` 【本地数据】 | `display ip interface brief [ … ]` / `display ip interface [ interface-name \| … ]` 【本地数据】 | `show ip interface brief` —— **未确认**（原始文档里未见该确切命令名）；接口 IP 可看 `show interfaces` 的 IP 段【未确认具体字段】 |
| **查路由表** | `display ip routing-table` 【本地数据】 | `display ip routing-table` / `display ip routing-table protocol { bgp \| direct \| isis \| rip \| static \| ospf }` 【本地数据】 | `show ip route` 【厂商原文 `:18327`】 |
| **查 ARP** | `display arp` 【本地数据】 | `display arp` / `display arp dynamic` / `display arp interface …` 【本地数据】 | `show arp [ interface-type interface-number \| trusted … ]` / `show arp detail …` 【厂商原文 `:35386`、`:35393`】 |
| **查 MAC 表** | `display mac-address` / `display mac-address mac-move` 【本地数据】 | `display mac-address dynamic […]` 【本地数据】 | `show mac-address-table dynamic [ address … ] [ [ vlan … ] … ]` 【厂商原文 `:16655`】 |
| **查 VLAN** | `display vlan [ vlan-id1 [ to vlan-id2 ] \| all \| … ]` / `display vlan brief` 【本地数据】 | `display vlan vlan-id [ verbose \| to vlan-id2 ]` 【本地数据】 | `show vlan [ id vlan-id ]` / `show interface switchport` 【厂商原文 `:18137`、`:17774`】 |
| **查日志** | `display logbuffer [ reverse ] [ level severity \| size buffersize \| slot … ] *` / `display logbuffer summary` 【本地数据】 | `display logbuffer [ security ] [ slot … \| … ]` 【本地数据】 | `show logging` / `show logging reverse [ timestamp …]` / `show logging config` / `show logging count` 【厂商原文 `:5872-5878`】 |
| **保存配置** | 用户视图 `save file-url [ all \| slot … ]`；不带参数时交互确认 【本地数据】 | 用户视图 `save [ configuration-file ]` 【本地数据】 | 特权模式 `write [ memory ]` / `write [ auto-save … \| terminal ]` 【厂商原文 `:1959`】 |
| **重启** | 用户视图 `reboot` 【本地数据】 | 用户视图 `reboot` 【本地数据】 | `reload` → 回显 `Reload system?(Y/N)` 【厂商原文 `:855-856`】 |
| **恢复出厂** | 用户视图 `reset saved-configuration [ backup \| main ]` 【本地数据】 | 用户视图 `reset saved-configuration` 【本地数据】 | ⚠️ **未确认**（锐捷侧未见同名命令；RGOS 常用 `delete flash:config.text` 类做法 —— **本轮无出处，不写**） |
| **进系统视图/全局配置** | 用户视图 `system-view` → `[H3C]` | 用户视图 `system-view` → `[HUAWEI]` | 用户模式 `enable` → `Ruijie#`；再 `configure terminal` → `Ruijie(config)#` 【本地数据：`enable` 视图=用户模式，`configure terminal` 视图=特权模式】 |
| **退出** | `quit`（用户视图即可用；逐级退出）【本地数据】 | `quit`（所有视图）【本地数据】 | `exit`（全局配置模式）【本地数据】 |
| **视图/模式的叫法** | **视图**：`用户视图` / `系统视图` / `任意视图`（还有 156 种） | **视图**：`用户视图` / `系统视图` / **`所有视图`**（注意不是"任意视图"，1211 种） | **模式**：`用户模式` / `特权模式` / `全局配置模式`（147 种） |

**这个"视图词汇三家完全不同"的坑**（round-08 之后的补全过滤直接依赖它）：
H3C 的通配标签是 `任意视图`、华为是 `所有视图`、**锐捷没有通配标签**。
来源：`_HANDOFF.md:130-136`；本轮从命令库统计的视图总数（h3c 156 / huawei 1211 / ruijie 147）与之一致。

### 11.2 常见运维任务怎么做（三厂商对照 + 注意）

#### ① 接口不通排查（顺序很重要）

```text
1) 看接口状态 up/down + 错误计数
   H3C/华为：display interface brief          ← 华为【本地数据】；H3C 见下方注意
   锐捷   ：show interfaces status            【厂商原文 :13301】
2) 看 IP 是否配上、是否同网段
   H3C：display ip interface      华为：display ip interface brief
3) 看 ARP 有没有学到对端
   H3C/华为：display arp            锐捷：show arp
4) 看路由表有没有去程
   display ip routing-table（H3C/华为） / show ip route（锐捷）
5) 看 MAC 表条目是否从预期口学到（二层问题）
   display mac-address / show mac-address-table dynamic
```

> ⚠️ **注意**：H3C 侧的 `display interface brief` **本轮未在本地数据里确认到**（只确认到
> `display interface vlan-interface [ brief [ down ] ]`）。这是 **未确认**项 —— 上机前请以 `?` 补全或官方手册为准。

#### ② 端口 up/down 判断

- **物理 down vs 协议 down 要分开看**：接口详细输出里第一行通常是
  `current state` / `Line protocol state`。**本轮的本地数据只到"命令 + 语法"，没有逐字段说明**，
  所以字段名 **未确认** —— 不要照抄字段名。
- **快速看全部口**：华为/锐捷用 `display interface brief` / `show interfaces status`（一张表，含 up/down）。
- **抖动（flapping）**：看接口详细输出里的 up/down 次数与最近一次时间 —— 字段名 **未确认**。

#### ③ 光模块 / 收发功率

| 厂商 | 命令 | 来源 |
|---|---|---|
| H3C | `display transceiver interface [ interface-type interface-number ]`<br>`display transceiver diagnosis { interface … }` | 【本地数据】 |
| Huawei | `display transceiver [ interface … \| controller … ]` | 【本地数据】；官方说明见 [Displaying Optical Module Information（S12700/S12700E V200R023C00 配置指南 — 设备管理）](https://support.huawei.com/enterprise/en/doc/EDOC1100333876/94ef2802/displaying-optical-module-information) |
| Ruijie | `show interface [ interface-type interface-number ] transceiver`（**实时光模块信息**）<br>`show interfaces transceiver`（周期性采集） | 【厂商原文 `fulltext.txt:12758-12759`】 |

- 光功率单位/字段（`RxPower` / `TxPower` / 温度 / 电压）**字段名未确认**，别背字段名。
- 华为官方另有排障页：[Optical Modules Fail to Communicate（S 系列排障指南 V600）](https://support.huawei.cn/enterprise/en/doc/EDOC1100313480/c5e30425/optical-modules-fail-to-communicate)。

#### ④ 链路聚合

| 厂商 | 命令 | 来源 |
|---|---|---|
| H3C | `display link-aggregation summary`<br>`display link-aggregation verbose [ { bridge-aggregation \| route-aggregation } [ interface … ] ]`<br>`display link-aggregation member-port [ interface-list ]`<br>`display link-aggregation load-sharing mode` | 【本地数据】 |
| Huawei | `display link-aggregation …` —— ⚠️ **未确认**（本轮本地数据 huawei 侧未命中该前缀） | — |
| Ruijie | `show lacp capacity` / `show lacp counters [ key-number ]` / `show lacp information …` | 【厂商原文 `:14647-14649`】 |

#### ⑤ 生成树（STP/MSTP）

| 厂商 | 命令 | 来源 |
|---|---|---|
| H3C | `display stp [ instance instance-list \| vlan vlan-list ] history [ slot … ]`<br>`display stp root` / `display stp region-configuration` / `display stp abnormal-port` / `display stp down-port` / `display stp bpdu-statistics …` | 【本地数据】 |
| Huawei | `display stp` 的命令名存在，但本地数据里**视图记录为 `MSTP进程视图`**（`display stp [ instance instance-id ] [ interface … ]`）—— 注意**和 H3C 的"任意视图"不同** | 【本地数据】 |
| Ruijie | `show spanning-tree [ mst instance-id ] { port-index \| interface … }` | 【厂商原文 `:27321`】 |

#### ⑥ ACL

| 厂商 | 命令 | 来源 |
|---|---|---|
| H3C | `display acl [ ipv6 ] { acl-number \| all \| name acl-name }`；配置 `access-list` 家族 | 【本地数据】 |
| Huawei | `display acl { name acl-name \| all }` | 【本地数据】 |
| Ruijie | `show acl capability` / `show acl res [ dev … [ slot … ] ]`；配置 `access-list acl-number { deny \| permit } { source-ipv4-address source-ipv4-wildcard … }` | 【厂商原文 `:122760-122764`】 + 【本地数据】 |

#### ⑦ VLAN 划分与 Trunk

- 建 VLAN：H3C/华为 `vlan vlan-id`（系统视图）；锐捷 `vlan vlan-id`（全局配置模式）【本地数据】。
- 接口放行/Trunk：**具体命令族本轮未逐条确认**，写进配置前务必用 `?` 或在设备上 `display this` 复核。
- 验证：H3C `display vlan brief`、`display vlan <id>`；锐捷 `show vlan [ id vlan-id ]` + `show interface switchport`
  【本地数据 / 厂商原文 `:18137`、`:17774`】。
- **锐捷文档原文的关键提醒**：配 VLAN 名称**前**先用 `show vlan` 看现有名称，**避免 VLAN 名称重复** —— 【厂商原文 `:17833`】。

#### ⑧ 静态路由 / OSPF / BGP 邻居

| 项 | 命令 | 来源 |
|---|---|---|
| 看路由（含协议过滤） | 华为 `display ip routing-table protocol { bgp \| direct \| isis \| rip \| static \| ospf }` | 【本地数据】 |
| OSPF 邻居 | 华为 `display ospf [ process-id ] peer [ … ]`；锐捷 `show ip ospf neighbor` | 【本地数据】/【厂商原文 `:55671`】 |
| H3C OSPF | `display ospf [ process-id ] [ verbose ] …`（**该命令不带 `peer` 关键字**，本地数据里是 `display ospf`） | 【本地数据】 |
| BGP 邻居 | 华为 `display bgp { peer \| slow-peer }` / `display bgp peer ipv4-address verbose`；H3C `display bgp peer ipv4 [ unicast ] [ vpn-instance … ] [ ip-address … ]` | 【本地数据】 |
| BGP（锐捷） | `show bgp all neighbors [ { neighbor-ipv4-address \| … } ]` | 【厂商原文 `:66022`】 |

#### ⑨ DHCP

| 项 | 命令 | 来源 |
|---|---|---|
| H3C（服务端看租约） | `display dhcp server ip-in-use [ ip ip-address \| pool pool-name ]` | 【本地数据】 |
| H3C（relay） | `display dhcp relay { configuration \| all \| interface … }`、`display dhcp relay statistics …` | 【本地数据】 |
| H3C（snooping） | `display dhcp snooping binding …` / `… trust` / `… packet statistics …` | 【本地数据】 |
| 华为 | `display dhcp snooping [ interface … \| … ]`、`display dhcp relay { configuration \| all \| interface … }` | 【本地数据】 |
| 锐捷 | `show ip dhcp binding [ ipv4-address \| all \| pppoe \| solidify ]`、`show ip dhcp server exclude` | 【厂商原文 `:38151-38153`】 |

#### ⑩ 端口镜像抓包

- ⚠️ **三厂商 `monitor`/`mirror` 类命令的语法本轮未从本地数据确认到**（本地命令库的 model 覆盖偏路由/交换的特定机型，
  镜像命令名未进入 `top` 命中）。**未确认** —— 请以官方手册为准。
- 已知可用的官方入口（H3C）：[H3C 配置文件管理（官方文档）](http://wwwsg.h3c.com/cn/d_202305/1841386_30005_0.htm)。

#### ⑪ 配置文件备份与回滚

| 厂商 | 查看当前/下次启动配置 | 来源 |
|---|---|---|
| H3C | `display current-configuration [ … ]`、`display saved-configuration`、`display startup` | 【本地数据】 |
| Huawei | `display current-configuration [ … ]`、`display saved-configuration [ last \| time ]`、`display startup [ slot … ]`、`display current-configuration interface …` | 【本地数据】 |
| Ruijie | `show running-config [ interface interface-type interface-number \| … ]`；导出到文件：`show running-config > flash:output.txt` | 【厂商原文 `:2151`、`:540`】 |
| Ruijie（保存） | `write [ memory ]` | 【厂商原文 `:1959`】 |

**回滚的注意事项**：
- 锐捷官方原文明确要求：**配置文件缩进格式需和执行 `show running-config` 命令回显的格式保持一致**，
  否则会导致模式判断出错、回滚异常 —— 【厂商原文 `:1919`】。
- 「当前配置」vs「下次启动配置」的区别见 11.2 节第 2、11.6 节；关分页见 11.4 节。

#### ⑫ 批量改配置

- 本项目的做法（`core/batch.py`）：**等提示符返回**再发下一条；遇到 `---- More ----` 自动翻页；
  遇到 `[Y/N]` **停下交人工**。慢设备/长输出用固定间隔容易错位 —— `README.md:145-146`。
- 命令行层面的通用提醒：**进配置视图要先 `system-view`（H3C/华为）/ `enable`→`configure terminal`（锐捷）**，
  批量脚本里必须按视图层级排列命令，否则会被设备拒绝 —— `docs/README.md:1013-1016`。

### 11.3 术语速查（一句话 + 一条最相关命令）

| 术语 | 一句话解释 | 一条最相关命令（含厂商） |
|---|---|---|
| **VLAN** | 把一个物理交换机逻辑切成多个广播域，隔离广播、划分安全边界 | 建：`vlan vlan-id`（三家）；验：H3C `display vlan brief` / Ruijie `show vlan` |
| **Trunk** | 交换机之间的链路，允许多个 VLAN 带标签通过 | 验：Ruijie `show interface switchport`（看 Switchport 是否 enabled）【厂商原文 `:17774`】 |
| **STP** | 生成树协议，在冗余链路里**逻辑阻塞**端口以防环 | 验：H3C `display stp root` / Ruijie `show spanning-tree` |
| **LACP** | 链路聚合的控制协议，把多条物理链路绑成一条逻辑链路 | 验：H3C `display link-aggregation summary` / Ruijie `show lacp counters` |
| **ACL** | 访问控制列表，按五元组/网段匹配报文并 permit/deny | 验：H3C `display acl { acl-number \| all \| name … }` / Huawei `display acl { name … \| all }` |
| **QoS** | 对流量分类、标记、限速、调度，保证关键业务优先级 | ⚠️ 本轮**未确认**具体命令（本地命令库未命中 QoS 查询命令名） |
| **VRRP** | 多台设备虚拟出一个网关 IP，主备切换保证网关高可用 | 配：Ruijie `vrrp group-id ip ipv4-address [ secondary ]`（SVI 接口配置模式）【本地数据】 |
| **OSPF** | 链路状态型内部网关协议，按链路开销算最短路径 | 验：Huawei `display ospf peer` / Ruijie `show ip ospf neighbor` |
| **BGP** | 路径向量型外部网关协议，互联网/跨域路由的事实标准 | 验：Huawei `display bgp peer` / H3C `display bgp peer ipv4` |
| **VXLAN** | 用 UDP 封装二层帧，在三层网络上做大规模二层延伸（数据中心） | ⚠️ 本轮**未确认**查询命令；H3C 数据里存在 VSAN/VSI 类视图但未逐条确认 VXLAN 命令 |
| **DHCP Snooping** | 交换机监听 DHCP 交互，建立"IP-MAC-端口"绑定表以挡私接 DHCP | 验：H3C `display dhcp snooping binding` / Huawei `display dhcp snooping` |
| **ARP 抑制** | 抑制大量 ARP 请求泛洪，防 ARP 攻击与广播风暴 | H3C 侧相关：`display arp source-suppression`、`display arp detection`【本地数据】；华为侧是 `display arp anti-attack …` / `display arp flood statistics`【本地数据】 |

### 11.4 分页与屏显管理（**最省时间的一节**）

| # | 项 | H3C / Huawei（Comware / VRP） | Ruijie（RGOS） |
|---|---|---|---|
| 1 | **关分页（临时）** | `screen-length 0 temporary` —— 只对**当前会话**生效，不写配置、断线即失效。本项目 README 原文推荐登录后先执行 | ⚠️ **未确认**（锐捷侧未见同名命令；RGOS 常见做法是 `terminal length 0` —— **本轮无出处，不写**） |
| 2 | 关分页（永久） | 系统视图下 `screen-length disable` —— ⚠️ **未确认**（本轮未在本地命令数据里命中该命令名，**别当已核实**） | ⚠️ **未确认** |
| 3 | **分页时按什么继续** | **空格**翻一页；回车**往下一行**；`q` 或 `Ctrl+C` 退出分页 | 同（锐捷也是 More 提示）⚠️ **未确认具体按键** |
| 4 | 分页时**会不会回显** | ❌ **不回显** —— 这是第 12.3 节那个"看起来像按键没进去"的真因 | 同 |
| 5 | 长输出被截断/卡住的后果 | 采配置快照会卡住、批量下发会错位 | 同 |

**为什么"关分页"是全项目最高频的前置动作：**
- 旧版 README 就直接写"登录华为/H3C 设备后建议先执行 `screen-length 0 temporary` 关闭分屏，
  避免长输出被分页卡住；Cisco 设备对应 `terminal length 0`" —— `README.md:147-149`。
- `core/ensp_connector.py` 在**连接后自动关闭分页**；`tests/regression_fixes.py` 的 `[1]` 就是守着这条，
  原因注释原文："报错是 `Unrecognized command`，**指向命令而不指向分页**；
  命令单独跑完全正常，只在长输出之后才失败。" —— 这是"**误导性报错**"的典型。
- 采快照前也必须先关分页 —— `docs/README.md:1018-1020`。

**做法（本项目里的确定性路径）**：

```text
① 连上设备后，先发关分页命令：
   H3C / Huawei : screen-length 0 temporary
   Cisco        : terminal length 0
② 再跑任何长输出命令（display current-configuration / display interface 等）
③ 如果已经停在 ---- More ---- ：发空格逐步翻完，或发 q 退出，再继续
```

> ⚠️ **H3C 的 `screen-length disable` 我标了「未确认」**：本地命令数据里没命中该命令名，
> 官方入口见 `http://wwwsg.h3c.com/cn/d_202305/1841386_30005_0.htm`。
> 上机前请用 `?` 补全或官方手册核实 —— **不要照抄我没核到的命令**。

### 11.5 会话与连接管理（Telnet / SSH / Console 的差别与坑）

| 项 | SSH | Telnet | Console（串口） |
|---|---|---|---|
| 传输 | TCP 22 | TCP 23 | 串口 COM（9600 8N1 等） |
| 本项目连接器 | `core/ssh_connector.py` | `core/telnet_connector.py`（Python 3.13 移除 `telnetlib` → 内置 `_MiniTelnet` 回退） | `core/serial_connector.py`（完整帧格式/四流控/DTR-RTS/Break/运行时切波特率/断线重连） |
| 登录提示语 | 固定 | **多样**：`login:` / `Username:` / `login as:` / `Password:` / `Passwd:` —— 本项目已兼容；部分 Telnet 转 Console 需**先敲回车唤醒设备** | 设备侧可能完全不提示 |
| 登录失败行为 | — | **立即返回错误，不会反复发命令当密码重试**（避免触发设备锁定策略） | — |
| 回显 | 通常有 | 通常有 | **设备可能不回显** → 需勾"本地回显"；界面底部输入回显条可兜底 |
| 特别的坑 | — | 分页时**不吃回显**；擦除序列**各家不同** | **没有"清屏"概念**：每次是干净连接，但**设备侧那一行不会自己消失** → 复现脚本要"一个 phase 一次全新连接" |
| 超时/重连 | — | — | Break（进 BootROM）、DTR/RTS 电平、自动重连依赖 USB 转串口芯片驱动（少数廉价芯片不支持 Break/DTR-RTS） |

来源：`README.md:11-13`、`docs/README.md:226-252`、`:879-880`（驱动限制）、`:902-916`（Telnet 两个已修问题）、
`_HANDOFF.md:467-482`（锐捷串口的 phase 隔离）。

**本项目的实测性能改进（可引用）**：Telnet 输出曾是"每 10 秒一批"（`read_very_eager()` 阻塞到 socket 超时）
→ 改临时非阻塞后 **10.1s → 0.1s**；Telnet 登录原来写死匹配 `login:`/`Password:` 各等 5 秒
→ 改为兼容多提示语 + 匹配到立刻输入 + 先唤醒设备后 **11s → 0.5s** —— `docs/验证指南.md:196-207`。

### 11.6 易错点 / 经验（最能省时间）

| # | 易错点 | 正确做法 | 来源 |
|---|---|---|---|
| 1 | **`---- More ----` 分页会吃掉后续输入** —— 并且**分页时不回显**，看起来像"按键没进去" | 先翻页或先关分页：H3C/华为 `screen-length 0 temporary`，Cisco `terminal length 0` | `README.md:147-149`、`docs/README.md:999-1000` |
| 2 | 以为"改完就生效" | 改的是**运行配置**；要写进**下次启动配置**必须显式保存：`save`（H3C/华为）/ `write`（锐捷）。区分 `display current-configuration`（当前运行）与 `display saved-configuration`（下次启动） | `_vendor_cmd` 语法 + `docs/README.md:54-55` |
| 3 | `undo` 与 `no` 混用 | H3C/华为用 **`undo`**；Cisco/锐捷用 **`no`** | 通用约定；本项目锐捷数据里 `no` 未逐一确认 → 记作**经验总结（无出处）** |
| 4 | 危险命令直接下发 | `reboot` / `delete` / `erase` / `undo all` / `factory-reset` 在本项目里是**代码级拦截**；AI 生成的配置类命令**只入队不自动下发** | `README.md:37-39` | <!-- 更正 2026-10-03：该清单是举例非穷举，实际规则见 core/safety.py（含 no/clear/reset/reload/shutdown） -->
| 5 | 分页没关就采配置快照 | 快照会卡住；先 `screen-length 0 temporary` | `docs/README.md:1018-1020` |
| 6 | 批量下发用固定间隔 | 慢设备会错位；用**等提示符**；遇到 `[Y/N]` 停手 | `README.md:145-146` |
| 7 | 并发巡检开太大 | 可能触发设备侧 VTY 线路限制/管理网拥塞；默认 4，5~10 台以内稳妥 | `docs/README.md:893-894` |
| 8 | Telnet vs SSH vs Console 的差别 | 见下 | `README.md:3`、`docs/验证指南.md:42-53` |
| 9 | 日志时间戳不对就开始排障 | **先 `display clock` / `show clock` 校时**，否则时间线对不上 | 经验总结（无出处）；锐捷 `clock set hh:mm:ss [ MM [ DD [ YY ] ] ]` 【厂商原文 `:1469`】 |
| 10 | 改完 QSS 直接看界面 | 必须跑 `check_qss_tokens.py`（坏 token → 整份样式表失效） | `_HANDOFF.md:756-758` |
| 11 | 用 `python -c` 跑含引号/中文的片段 | 写成 `.py` 文件再跑 | `_HANDOFF.md:876-879` |
| 12 | 拿"程序化注入按键"当过产品验证 | **注入验证的是代码路径，真实键盘验证的是产品** | `_HANDOFF.md:383-387` |
| 13 | 用固定睡眠等 UI 状态 | 改**轮询等目标状态**，或断言"在 N ms 内出现" | `_HANDOFF.md:238-248` |
| 14 | 单点证据就下"不存在"的结论 | 关键判断**至少两个独立证据源**；探针说"没有"时先怀疑探针 | `_HANDOFF.md:871-874` |
| 15 | 离线用例喂"完整串"就以为覆盖了真机 | 真机提示符**会被切成两块**；离线用例必须按真实**分块/时序**构造 | `_HANDOFF.md:866-870` |

### 11.7 排障思路（分层：先看什么 → 用什么命令 → 典型结论）

> 这是**方法论**，命令名以 11.1 表为准；**字段名未确认**的地方不写字段名。

| 层 | 先看什么 | 命令（三家） | 典型结论 |
|---|---|---|---|
| **① 物理层** | 接口是不是物理 up？光模块在不在位、收发光功率是否在范围内？ | H3C `display transceiver diagnosis …`<br>Huawei `display transceiver`<br>Ruijie `show interface … transceiver` | 光功率异常 / 模块不在位 → 换模块或换纤 |
| **② 链路层** | 接口 up 吗？MAC 学到了吗？有没有环路（STP 阻塞）？聚合口成员齐吗？ | 接口状态（11.1 表）+ MAC 表 + `display stp …` / `show spanning-tree` + `display link-aggregation summary` / `show lacp counters` | 物理 up、协议 down → 二层协商/封装问题；MAC 学不到 → 二层不通或安全特性挡了 |
| **③ 网络层** | 有没有 IP、同网段吗、ARP 通吗、路由表有没有去程/回程？ | `display ip interface …` / `display arp` / `display ip routing-table`（H3C/华为）；`show ip route` / `show arp`（锐捷） | ARP 学不到 → 二层不通或 ARP 抑制；有去程无回程 → 对端/回程路由问题 |
| **④ 传输层** | 端口通不通、有没有被 ACL 挡？ | ACL：`display acl …` / `show acl …`；连通性测试命令（本项目的 `ping` 走设备） | ACL deny 命中计数增长 → 策略挡了 |
| **⑤ 应用层** | 服务在不在、日志报什么、时间对不对？ | 日志：`display logbuffer` / `display logbuffer summary` / `show logging`；时间：`display clock` / `show clock` | 日志时间戳不对 → **先校时**，否则所有排障时间线都对不上（本项目也踩过时间戳相关的坑，见第 12 节） |

**两条跨层铁律（本项目实测得来的）：**
1. **报错指向的方向和真实原因可以完全无关** —— eNSP 报 `Unrecognized command` 实为**分页未关**
   —— `_HANDOFF.md:855`。
2. **每个诊断工具只能回答它那一层的问题** —— `AIT_KEYLOG` 回答"按键有没有到"，
   `AIT_BYTELOG` 回答"我们发出去什么、设备回什么" —— `_HANDOFF.md:856-861`。

---

## 12. 实战经验库

> **这一节是本文档最重要的一节。** 它记录的是**我们自己踩出来的**事故与判据，
> 不是知识罗列。全部条目都从仓库里的真实记录提取（`_HANDOFF.md`、`design/reviews/*`、
> `_automation/` 脚本注释、各 `README.md`），**没有编造**。
>
> **追加方式（以后每次遇到新问题就照这个做）：**
> 1. 复制下面的模板，**插到 12.2 之前**（最新在最上面，编号顺延；或直接用日期当序号）。
> 2. 在开头**目录**里加一条锚点，**用"现象关键词"当标题**，不要用根因当标题
>    （下次你是先看到现象、还不知道原因）。
> 3. 标题格式固定：`### YYYY-MM-DD · <一句话标题>`，并在标题**紧上一行**放
>    `<a id="case-短名"></a>` 锚点（短名用英文/拼音，别带尖括号）。
> 4. 填完"经验"那一行必须是**可执行的判据**："如果看到 X，就是 Y，直接做 Z"。

**记录模板（复制这段）：**

```markdown
<a id="case-short-name"></a>
### YYYY-MM-DD · <一句话标题>
- **场景**：在做什么（哪台设备 / 哪个界面 / 哪个配置）
- **现象**：看到了什么（**贴关键回显/报错原文**，不要转述）
- **排查**：一步步做了什么、每步得到什么结论（哪一步排除了什么）
- **根因**：真正的原因（区分"现象"与"根因"）
- **解决**：具体命令/改法（可直接复制的命令块）
- **经验**：**如果看到 X，就是 Y，直接做 Z**（一句话判据，最重要）
- **相关**：涉及的文件 / 命令 / 章节链接
```

---

<a id="case-qss-整份失效"></a>
### 12.1 · 2026-10-01 · QSS 里一个多余的 `}` 让**整份样式表**被 Qt 丢弃 → 全应用退回灰色

- **场景**：改完 `cpp/resources/theme/dark.qss`（给 `#NavRail` 加 `border-right`）后启动应用。
- **现象**：界面**大片发灰**、颜色不是设计稿的颜色。用户两次说"还是变了 / 颜色不是图片的颜色"。
  代码里的残留物长这样：
  ```qss
  #NavRail {
      background: @bg-panel;
      border-right: 1px solid @border;
  }
      border-right: 1px solid @border;   ← 编辑时残留的孤行
  }                                      ← 多出来的 } → 语法错误
  ```
- **排查**：
  1. 先跟着"配色分歧"的方向查了好几轮 —— **方向错了**，真因不在配色。
  2. 加 `QT_FORCE_STDERR_LOGGING=1` + `Start-Process -RedirectStandardError` 才看到
     `Could not parse application stylesheet`。**Qt 不给行号。**
  3. 写了静态自查脚本：① QSS 里用到的 `@token` 是否都在 `Theme.cpp::tokenMap()` 里有定义
     ② 花括号是否配对（会跳过 `/* */` 注释，避免误报）。
- **根因**：**不是配色分歧**，是 QSS 语法错误 → Qt `setStyleSheet()` 解析失败 →
  **整份样式表被丢弃** → 所有控件退回 `QPalette`（Fusion 的 `#2d2d2d`）。
- **解决**：
  ```powershell
  $env:PYTHONUTF8=1
  python C:\Users\linyin\deepseek_harness\_automation\check_qss_tokens.py
  ```
  验证：修复后 Qt 无样式表报错；像素采样 终端/会话头 = `#12151A`（正好 `@bg-surface`）、
  设备库 = `@bg-panel`；**连续两次启动像素一致**。
- **经验**：**如果改完 QSS 后界面"大片发灰、全都不对"，就是样式表整份解析失败了
  （不是配色问题）—— 立刻跑 `check_qss_tokens.py`，别去调颜色。**
- **相关**：`_automation/check_qss_tokens.py:1-16`（docstring 原文）、`_HANDOFF.md:735-770`、
  `cpp/resources/theme/dark.qss`、`cpp/src/core/Theme.cpp`

---

<a id="case-上色不生效"></a>
### 12.2 · 2026-10-02 · 终端"数字/IP 上色"看起来做了，实机上一像素都没生效

- **场景**：`02b` 截图里跑 `ping 192.168.1.254`，希望 IP 与数字按"关键数据"上色。
- **现象**：终端区（`x=530..1460, y=200..470`，共 251100 像素）
  **没有任何一个像素**接近 `#A3BE8C`（**精确 0 个、±8/通道 0 个**），
  而同区域有 **3963 个**像素是纯正文色 `#E6E8EB`。
  把 `ping …` 那一行放大 4 倍看：**IP 与命令名同为灰白**。而同一张图里「已连接」事件行是明确的绿。
- **排查**：
  1. 不要靠肉眼 —— 用像素统计（精确命中数）当证据。
  2. 回到代码看**上色分支的前提条件**是否成立。
- **根因**：`TerminalView::writeRun()` 里的上色前提是
  `format.foreground().style() == Qt::NoBrush`（"只有没有显式前景色才上色"）。
  而**设备回显 / 初始 prompt 会带一个显式前景色** → **整条分支被跳过** → 数字高亮全线失效。
- **解决**：把"设备 ANSI 真正指定过颜色"与"框架给的默认前景色"**区分开**，
  用独立标志位（如 `QTextFormat::UserProperty`）或比较 `QColor` 是否等于 `Theme::Col::TermFg` 判断。
  验证：同区域 `#A3BE8C` 像素数应 **> 300**。
- **经验**：**如果"上色/高亮"代码写了但界面上看不到，先去看它的 `if` 前提条件是不是在设备回显路径下不成立
  —— 别先怀疑颜色值本身。**
- **相关**：`design/reviews/ui-review-02.md:17-29`（P0-1 原文，含建议改法）、
  `cpp/src/ui/TerminalView.cpp` 的 `writeRun()`

---

<a id="case-分页吃输入"></a>
### 12.3 · 2026-10-01 · 设备在 `---- More ----` 分页时**不回显**，看起来像"按键没进去"

- **场景**：在 HCL/S6850 上跑长输出（`display version` 之类），或在真机上做补全复现。
- **现象**：敲键盘界面上什么都没变，像"按键丢了/没进终端"。
- **排查**：
  1. 先怀疑按键路径 → 看 `AIT_KEYLOG`：按键事件**都到了**。
  2. 再看 `AIT_BYTELOG`（我们发出去什么、设备回什么）：**设备在分页时不回显**。
  3. 结论：不是按键问题，是**设备侧分页把后续输入吃掉了**。
- **根因**：设备处于分页显示状态（`---- More ----`），此时**不产生回显**；
  本控件在远端回显模式下"设备回显什么就画什么"，于是屏幕上什么都没变。
- **解决**：
  - 手动：翻页（空格/回车）或发 `q` 退出分页。
  - 根治：登录后先关分页 —— H3C/华为 `screen-length 0 temporary`，Cisco `terminal length 0`。
  - **复现脚本开工前必须先完成这一步**（`_HANDOFF.md` 明确列为 HCL 的坑之一）。
- **经验**：**如果敲键盘"界面上什么都不变"，先看设备是不是停在 `---- More ----`；
  分页时不回显 —— 先把分页清掉再判断按键/程序的问题。**
- **相关**：`_HANDOFF.md:484-521`（HCL 坑）、`README.md:147-149`、`docs/README.md:999-1000`、
  `core/ensp_connector.py`（连接后自动关分页）、`tests/regression_fixes.py` 的 `[1] eNSP 的分页处理`

---

<a id="case-无边框判据"></a>
### 12.4 · 2026-10-02 · "截图里没有系统标题栏"**证明不了**无边框生效

- **场景**：把主窗口与所有对话框改成无边框（`FramelessWindowHint` + 自绘 `TitleBar`）后要验收。
- **现象**：如果只看 `window.grab()` 的截图，**永远看不到原生标题栏** ——
  于是"截图里没有标题栏"看起来像通过，其实毫无意义。
- **排查 / 判据**（必须用 User32/内核事实，四个独立证据）：
  1. **样式位**：`GWL_STYLE` 里应**没有** `WS_CAPTION` / `WS_THICKFRAME` / `WS_SYSMENU`；
  2. **非客户区为 0**：`GetWindowRect`（整个窗口）**==** `GetClientRect`（客户区）
     —— 相等就等于"没有标题栏和边框"（原生窗口这里通常差 **30~40px**）；
  3. **拖动**：把光标放到自绘标题行上按下→移动→抬起（真实 `SendInput`），看窗口矩形确实跟着位移；
  4. **关闭**：点最右边自绘 ✕ → **主窗口应让进程退出**；**对话框应让它的窗口消失**
     （`QDialog::close` **不退应用**）。
- **根因**：`window.grab()` **只截客户区**，原生非客户区根本不进截图。
- **解决**：
  ```powershell
  $env:PYTHONUTF8=1
  python C:\Users\linyin\deepseek_harness\_automation\ait_frameless_check.py
  ```
  它还会把窗口区域从**物理屏**抓一张图（含窗口上沿外侧 40px），直观证明"上面没有任何系统标题栏"。
- **经验**：**如果要在截图里"证明"无边框生效 —— 证明不了。
  判据只能是「非客户区 = 0」（`GetWindowRect == GetClientRect`）+ 样式位检查。**
- **相关**：`_automation/ait_frameless_check.py:1-16`、`cpp/src/ui/TitleBar.{h,cpp}`、
  `cpp/CMakeLists.txt:40-43`

---

<a id="case-windres-空格路径"></a>
### 12.5 · 2026-10-02 · windres 转发 cc1 时不重新加引号 → 含空格路径下资源编译失败

- **场景**：给 exe 编静态图标（`resources/app.rc`）时，项目路径里有空格：`WorkBuddy AI`。
- **现象**：
  ```
  cc1.exe: fatal error: AI/2026-09-21-.../aiterminal/cpp/src: No such file or directory
  ```
- **排查**：报错里那个路径明显是被空格**截断**的（只剩 `AI/2026-…`）。
  CMake 会把 `-I "<含空格的绝对路径>"` 一并交给 windres，
  而 **windres 转发给 cc1 时不会重新加引号** → 路径被空格拆成两个参数。
- **根因**：工具链对空格路径的引号在转发环节丢失（不是 CMake 的配置写错）。
- **解决**（`cpp/CMakeLists.txt:88-97` 的三步）：
  1. `configure_file` 把 `.rc` 和 `.ico` **复制到无空格的构建目录**（`${CMAKE_CURRENT_BINARY_DIR}`）再编；
  2. **不给这个 target 任何 `-I`**；
  3. 用 **OBJECT 库** 而不是静态库 —— 资源对象**没有任何符号引用**，静态库里的会被链接器丢掉；
     再 `target_link_libraries(AITerminal PRIVATE ait_app_icon …)`。
- **经验**：**如果在含空格的路径下编 Windows 资源报 `cc1: No such file or directory` 且路径被截断，
  就是 windres 的引号转发问题 —— 把 .rc/.ico 复制到无空格目录、用 OBJECT 库编，不要试着加转义。**
- **相关**：`cpp/CMakeLists.txt:82-104`、`_automation/check_exe_icon.py`（验证 .ico 真的进了 exe，
  走字节证据 + `ExtractIconExW` 系统证据两条独立路径）

---

<a id="case-cleartype-假彩边"></a>
### 12.6 · 2026-10-02 · 像素判色被 ClearType 假彩边骗，结论会**反过来**

- **场景**：核对 round-08 新配色是否真的落到渲染像素上（旧色是否还在用）。
- **现象**：如果看"彩色像素占比"，会**虚高** —— 因为 Windows ClearType 把文字边缘
  按通道分别混色，产生**孤立的**旧色近似像素（假彩边）。
- **排查 / 两个口径**（`_automation/verify_round08_pixels.py:1-16`）：
  - **`raw`**：精确色值命中（**±2/通道**）。缺点：包含孤立假彩边。
  - **`run3`**：只统计"**同色水平连续 ≥3 像素**"的像素。
    真实在用的旧色一定**成片**（背景/边框/填充都有厚度），假彩边不成片。
    → **判"旧色是否还在用"看 `run3`。**
- **根因**：ClearType 亚像素渲染在文字边缘造出不存在于设计的颜色。
- **解决**：用 `run3` 口径；另有一个 `sample_palette.py`（采样 PNG 像素找最近的 token，
  客观判定某张图属于哪套色板，别靠肉眼比图）。
- **经验**：**如果像素统计说"旧色还在"，先排除 ClearType 假彩边 ——
  只认精确值（±2/通道）+ 同色连续 ≥3px；不要用"彩色像素占比"。**
- **相关**：`_automation/verify_round08_pixels.py`、`_automation/sample_palette.py`、`_HANDOFF.md:758-760`；
  同源教训：UI 走查里"绿像素 ±8/通道 近似 0 个"的判定口径（`design/reviews/ui-review-02.md:29`）

---

<a id="case-控制台登录态"></a>
### 12.7 · 2026-10-02 · 出图前置：HCL 控制台**跨连接保留登录态**，不先 `quit` 就没有绿色状态行

- **场景**：给 round-08 配色出全部 8 屏实机图，其中一屏希望终端里有绿色状态行。
- **现象**：终端里**没有**绿色状态行，出的图不满足预期。
- **排查**：需要的那行来自设备**当次登录**时打印的
  `... SHELL_LOGIN: Console logged in from con0`。
  而 **HCL 的 telnet 控制台会保留上一次会话的登录态** —— 不主动退出，就不再打横幅/登录行。
- **根因**：控制台会话状态是**跨连接残留**的，不是每次连接都重新走登录流程。
- **解决**：
  ```powershell
  python C:\Users\linyin\deepseek_harness\_automation\hcl_console.py logout   # 发 quit，退回"未登录"
  python C:\Users\linyin\deepseek_harness\_automation\hcl_console.py dump     # 只读看当前回显
  ```
  另：`shoot_round08.py` 明确要求"每张之间 **kill AITerminal**"，
  因为残留进程会让后面的图带上前一张的状态（实测：不 kill 时对话框会挂在旧进程的窗口上）。
- **经验**：**如果出的图里"少了登录/横幅那几行"，就是控制台还停在上一次的登录态 ——
  先 `hcl_console.py logout`；出图全套之间必须 kill 残留进程。**
- **相关**：`_automation/hcl_console.py:1-12`、`_automation/shoot_round08.py:9-27`

---

<a id="case-退格删不掉"></a>
### 12.8 · 2026-10-01 · 用户报"按退格删不掉、只多一个空格" —— 真因在**显示层**，不在输入层

- **场景**：用户在真机上敲命令，用退格改错。
- **现象**：用户原话"按 Backspace 无法删除，只会输入为空格"；以及"用提示后命令塞在许多空格之后"。
- **排查（这一步是本案最值钱的部分）**：
  1. 上一轮拿 **`AIT_KEYLOG`** 去查"按键有没有漏"，查完就断定"按键没到分支"，**改了输入代码**。
  2. 本轮 `AIT_KEYLOG` 明确打出按键**完全正常**：
     ```
     event    key=0x1000003 (Key_Backspace) popup=1 input=disp
     keyPress key=0x1000003                popup=1 input=disp
     event    key=0x5a 'z'                 input=dis     ← 缓冲确实删掉了 p
     ```
  3. 真证据在 **`AIT_BYTELOG`（原始字节）**：
     ```
     > 7F                                   ← 我们发对了退格
     < 1B 5B 31 44 20 1B 5B 31 44           ← 设备回 ESC[1D SPACE ESC[1D
     ```
     设备的擦除**完全依赖光标左移**把空格写在原字符上；而解析器把 CSI 一律丢弃 →
     空格成了"追加" → 屏幕上 `disp z`。
  4. **三家设备的擦除序列各不相同**（必须每家都抓原始字节）：
     | 厂商 | 擦一个字符发什么 | 谁负责擦 |
     |---|---|---|
     | 华为 VRP | `ESC[1D` + 空格 + `ESC[1D` | **空格覆盖** |
     | 锐捷 RGOS | `08 20 08`（BS 空格 BS） | `\b`（老代码本来就支持） |
     | **H3C Comware** | **`ESC[1D` + `ESC[J`** | **`CSI J` 擦除** |
  5. 附带发现：**H3C 收到 Ctrl+U 是"静默清行"，一个字节都不回** ——
     ```
     > 15                              ← 发 Ctrl+U，期望远端擦行
     > display                         ← 紧接着发新命令
     < display                         ← 设备只回显新命令，没有任何擦除序列
     ```
     于是真机上看到 `dispdisplay verdisplay version`。
- **根因**：**`AnsiParser` 把 CSI 转义一律丢弃**，而各家设备的擦除全靠 CSI →
  光标/覆盖关系全乱。这是**显示层**缺陷，被误判成**输入层**缺陷。
- **解决**：
  1. 实现 `CSI nD`（光标左移）；
  2. **补 `CSI nJ` / `CSI nK`** → 解析器吐一个**哨兵字符 `0x0B`**（`AnsiParser::kEraseToLineEnd`），
     由 `TerminalView::eraseToLineEnd()` 用空格覆盖"当前列→行尾"，
     **且不改 `m_col`**（VT 语义：擦除不移动光标；用 `putRun` 代替会把光标推右，正好是错的）；
  3. `replaceInputWith()` **不再用 Ctrl+U，改成发 `m_input.size()` 个退格** ——
     退格有回显，三家都能正确渲染，光标列自然跟着回退。命令都很短，多发几十个字节无所谓。
- **经验**：**如果用户说"按键删不掉字 / 命令叠在后面"，先看 `AIT_BYTELOG` 而不是 `AIT_KEYLOG`
  —— `AIT_KEYLOG` 只回答"按键有没有到"，要判断"擦除对不对"必须看原始收发字节。
  且记住：华为靠空格覆盖、锐捷靠 `\b`、H3C 靠 `CSI J`，**修了一家不等于修了全部**。**
- **相关**：`_HANDOFF.md:250-333`、`_HANDOFF.md:314-333`（原假设是错的，务必别重走）、
  `cpp/src/core/AnsiParser.cpp`、`cpp/src/ui/TerminalView.cpp`、
  回归断言 `tests/cpp_ui_regression.py:260-274`

---

<a id="case-厂商判定"></a>
### 12.9 · 2026-10-01 · 补全弹出**别家厂商**的命令 —— 配置里写的厂商压过了设备自己的提示符

- **场景**：设备实际上在 `<H3C>` 上，但设备配置里的 `vendor` 字段写的是 `huawei`。
- **现象**：H3C 设备上弹出华为命令（用户原话"提示命令有错、甚至把别家厂家的命令拿来"），
  例如 `displayname  H323属性视图`。
- **排查**：
  1. 先查**数据侧**是否跨厂商污染 —— 实测：锐捷文件里 `display*` 命令数 = **0**；
     h3c∩huawei 810 条属 Comware 同源正常重叠。
     → **不是数据问题。**
  2. 再查厂商判定：嗅探原来**只在厂商为空时生效**，配置给了 `huawei` 就不会被纠正。
- **根因**：**厂商判定问题** —— 配置/设备名给的厂商压过了设备自己的提示符。
- **解决**：`appendOutput()` 里改成"嗅探到的厂商**与当前不一致就纠正**"，并且：
  - **只在末尾 96 字符里找** —— 提示符总是刚打出来的；否则
    `display current-configuration` 之类正文里出现 `[H3C]` 字样就会误判；
  - **取最后一个匹配**（不是第一个）—— 窗口里可能残留上一条提示符，只有最新的代表当前设备；
  - `setCompletionContext()` 会清空嗅探尾巴（换会话时不让上一台的残留参与判断）。
  回归断言：`tests/cpp_ui_regression.py:300-307`。
- **经验**：**如果补全给了"别家厂商的命令"，不要先怀疑命令库 —— 设备提示符才是权威，
  配置里的厂商字段必须能被提示符推翻。判据：取末尾 96 字符里的最后一个提示符匹配。**
- **相关**：`_HANDOFF.md:90`、`:100-107`、`:119-120`、`cpp/src/ui/TerminalView.cpp` 的 `sniffPrompt()`

---

<a id="case-提示符分块"></a>
### 12.10 · 2026-10-01 · 离线用例"全绿"但真机补全不弹 —— 因为提示符被切成**两块**

- **场景**：串口/名称里没有厂商词的设备（设备名就叫 `Console`）靠**提示符**认厂商。
- **现象**：离线用例（喂**完整串** `<HUAWEI>`）**通过**；真机上补全**仍然不弹**
  （`AIT_KEYLOG` 里 16 个按键事件 `popup=0` 全为 0）。
- **排查**：
  1. 第一版修法是"只看**当前这一个数据块**"。
  2. 真机上设备回的提示符**被切成两块**：
     ```
     < 0D 0A 52 75        |..Ru|
     < 69 6A 69 65 3E     |ijie>|
     ```
     两块单独都匹配不上 → 补全不弹。
  3. 离线用例喂的是**完整串**，所以它"通过"了 —— **它只给了虚假的安全感**。
- **根因**：嗅探逻辑的作用域是"单块"而不是"一段滚动缓冲"。
- **解决**：改成**累积一小段滚动缓冲（512 字符）再匹配**；
  并把"分块形态"补成回归用例（`sniff_ruijie_split` / `sniff_huawei_split`）——
  见 `tests/cpp_ui_regression.py:287-290`。
  另：厂商为空时从**设备自己打的提示符**里认厂商（`<HUAWEI>` / `[~HUAWEI]` / `<H3C>` / `Ruijie>` / `Ruijie#`），
  且**必须被提示符包裹**才算命中，否则横幅里的 `Huawei Versatile Routing Platform` 会误判
  （已加反例用例 `sniff_negative_banner`）。
- **经验**：**如果离线测试全绿但真机上功能不生效，先检查离线用例喂的"输入形态"是否与真机一致
  —— 真机的提示符/回显会被 TCP 分块切开；用例必须按真实的「分块/时序」构造。**
- **相关**：`_HANDOFF.md:292-312`、`:866-870`、`tests/cpp_ui_regression.py:283-297`

---

<a id="case-token-前缀"></a>
### 12.11 · 2026-10-01 · `@token` 替换顺序随机 → 外观**时好时坏、不可复现**

- **场景**：`Theme::substitute()` 把 QSS 里的 `@token` 文本替换成色值。
- **现象**：某些 token（如 `@fg-strong`）的样式**有时生效、有时不生效**，每次进程启动不一样。
- **排查**：token 名互相包含 —— `@fg` 是 `@fg-strong`/`@fg-muted` 的前缀，
  `@border` 是 `@border-soft` 的前缀，`@accent` 是 `@accent-soft` 的前缀。
  先替换短名会把长名变成 `<颜色>-strong`，那条声明**非法被丢弃**。
  而替换用的 `QHash` **遍历顺序是随机化的** → 顺序每次进程都不同。
- **根因**：替换算法对"名字互为前缀"的顺序敏感，而顺序不确定。
- **解决**：改为**按 key 长度降序**替换。
  验证：连续两次启动像素一致（`_HANDOFF.md:768-770`）。
- **经验**：**如果样式"偶发不生效、重启一次又好了"，先怀疑替换/遍历顺序
  —— 有前缀包含关系的 token 必须按长度降序替换。**
- **相关**：`_HANDOFF.md:762-766`、`cpp/src/core/Theme.cpp` 的 `substitute()`

---

<a id="case-undefined-reference"></a>
### 12.12 · 2026-10-02 · 新增 `.cpp` 报 `undefined reference to XxxDialog::XxxDialog`

- **场景**：给 C++ 界面加一个新对话框（`KnowledgeDialog` 之类）。
- **现象**：链接错误 `undefined reference to KnowledgeDialog::KnowledgeDialog`
  —— **看起来像构造函数写错了**。
- **排查 / 根因**：`cpp/CMakeLists.txt` 是**显式列源文件**而不是 `file(GLOB)`，
  新 `.cpp` 没登记 → 那个文件**根本没被编译**。
- **解决**：在 `APP_SOURCES` 里登记这个 `.cpp`（`cpp/CMakeLists.txt:56-59` 的注释原文就写了这条）。
- **经验**：**如果报"某个新类的构造函数 undefined reference"，先去 CMakeLists 看这个 .cpp 登记了没有
  —— 别去读构造函数。**
- **相关**：`cpp/CMakeLists.txt:23-76`、`:56-59`

---

<a id="case-卡片化回退"></a>
### 12.13 · 2026-10-02 · 用户说"操作设备界面和其它界面画风不行"，我自己发明了一套视觉 → 被回退

- **场景**：用户反馈"操作设备界面和其它界面画风不行"。我**没盘点就自己发明了一套**
  （卡片化：窗口留白 + 到处加框线/底色）。
- **现象**：用户回"**还是变了**"。
- **排查**：去盘点项目既有资产，发现它**本就有完整设计系统**：
  | 资产 | 说明 |
  |---|---|
  | `design/system/tokens.json` | **token 单一真源**（`Theme.cpp` 注释明写"改配色请改 tokens.json，别把值写进 QSS"） |
  | `design/system/DESIGN-SYSTEM.md`（21KB） | Geist/Linear/Raycast/Warp 拆包 grep 交叉验证，带 🟢🟡🔴 证据等级 |
  | `design/system/COMPONENTS.md` | 组件规范（边框用 `box-shadow: 0 0 0 1px` 不撑布局；active 用 `scale(.97)`…） |
  | `design/mockups/` | **6 屏设计稿**（SVG+PNG） |
  | `design/gen/` | `ds.py`（token + 绘图原语 + 组件，含 minimal/graphite/slate 三套配色）+ `screens.py` + `render.py` |
  纪律原文：层次靠**字号 / 字重 / 面色差 / 留白**，**不是靠颜色和框线**。
  **我的卡片化正好反着来**，所以"还是变了"。
- **根因**：**没有先盘点既有设计系统**就动手改版式。
- **解决**：把改动**逐条精确回退**（窗口留白 / `#NavRail` / `#HostPanel` / `#SessionPage` /
  `#SessionInfoBar` / `#Chip` / 标签栏 / `#StatusStrip` / `#TerminalView` 内边距 / `#PanelTitle`），
  回到设计稿的通栏扁平结构。
  另外做了 `objectName × QSS 规则`的**审计**，找到了"其它界面画风不行"的**真因**：
  `#SectionLabel`、`#DialogHint`、`#FieldHint`、`#Knowledge*`、`#Diff*`、`#Patrol*`、`#Ai*`
  **全都没有样式规则** → 只能吃 Qt 默认样式。**不是风格不对，是实现没跟上设计稿。**
- **经验**：**用户说"画风不对"时，先盘点项目自带的 `design/system/` + `design/mockups/` + `design/gen/`，
  并且跑一次 `objectName × QSS 规则` 审计 —— 大概率是"某批控件根本没有 QSS 规则"，
  不是需要重新发明一套视觉。**
- **相关**：`_HANDOFF.md:678-703`、`design/system/DESIGN-SYSTEM.md`、`design/system/tokens.json`、
  `design/gen/README.md`、`cpp/resources/theme/dark.qss`

---

<a id="case-python-改完不同步"></a>
### 12.14 · 2026-10-02 · 改完 Python 行为没变：**POST_BUILD 只在链接时跑，产物目录还是旧那份**

- **场景**：修 `engine/service.py` 的一个 `IndentationError`（引擎启动即退出），改完重新构建再测 —— **行为一点没变**。
- **现象**：`cmake --build` 输出 `ninja: no work to do`；应用照旧"静默无应答"：
  补全浮层完全不弹、`commands.suggest` 永远没有 reply、`cpp_ui_regression` 的 popup 断言全假。
  更坑的是**没有任何报错** —— 看起来像"引擎慢"或"补全逻辑坏了"。
- **排查**（决定性一步：**去 py_compile 产物目录里的那一份**）：
  ```powershell
  python -c "import py_compile; py_compile.compile(r'%TEMP%\ait_cpp_build\engine\service.py', doraise=True)"
  # → Sorry: IndentationError: unexpected indent (service.py, line 759)   ← 源码已经修好，产物目录里还是坏的
  ```
  再用 `_automation/ait_engine_latency.py` 直接以 JSON-RPC 驱动引擎 → 引擎正常（冷 1380ms / 热 1–3ms），
  说明**代码没问题、是产物目录那份没被替换**。
- **根因**：把 `engine/ core/ config/` 同步到 `%TEMP%\ait_cpp_build` 的是 CMake 的 **POST_BUILD**，
  而 Ninja **只在重新链接时才跑 POST_BUILD** —— 只改 Python（无 C++ 变更）时它根本不执行。
- **解决**：
  1. 新增 `_automation/ait_sync_engine.py`（复制 engine/core/config 到产物目录 + 对关键 .py 做 `py_compile`）；
  2. 或者让 C++ 侧有任何改动以触发重链接；或手动 `robocopy` 三个目录。
- **经验（一句话判据）**：**改了 Python 但行为没变 / 引擎静默不答 RPC →
  先 `py_compile` 产物目录里的那份 `.py`，再手动同步 `_automation/ait_sync_engine.py`。**
  配套：GUI 进程启动**必须用** `_automation/ait_run_app.py`（等退出 + stdout/stderr 落文件），
  不要用 `subprocess.run(capture_output=True)`（引擎孙进程继承管道写端会假死），
  也不要在 pwsh 里 `& exe`（GUI 程序不等它退出，`$LASTEXITCODE` 为空，会误判成"没出图"）。
- **相关**：`cpp/CMakeLists.txt:119-135`（POST_BUILD）、`_automation/ait_sync_engine.py`、
  `_automation/ait_run_app.py`、`_automation/ait_engine_latency.py`、`engine/service.py`

---

## 13. 「使用总结」功能规格（**待实现**）

> ⚠️ **状态：待实现 / 规格草案。** 本节记录的是**需求与约定**，
> **不是已实现功能**。等负责改代码的 agent 交付后，**以它的实际实现为准**更新本节
> （并把下面每一条的"来源"改成真实文件与行号）。
> 本轮实测：全项目 grep `使用总结|小结|usage_summary|usageSummary|digest|回顾`
> **没有找到该功能的任何实现**（只有旧 Python 界面里一个同名的、**不相干**的
> `AIAgent.digest_document()` —— 它做的是"把导入的手册交给 AI 拆成解释 + 命令"，
> 见 `core/ai_agent.py:665-696`、`ui/main_window.py:868-929`，**别和本功能混为一谈**）。

### 13.1 需求原文（用户口径）

用户在软件使用过程中自动收集：
- **"我问过的问题 / 想法"**
- **"没跑通的命令"** 之类

攒成**小结、可回看**。

### 13.2 规格（草案 —— 交付后按实际实现校正）

| 项 | 草案约定 | 备注 |
|---|---|---|
| **入口** | 待定（预期是菜单/导航条里一个"使用总结 / 回顾"入口） | **待实现** |
| **收集哪三类** | ① 我问过的问题/想法 ② 没跑通的命令（命令失败/被拒/超时） ③ 待确认（第三类由实现方定，本节不预设） | **待实现**；第三类的确切定义 **未确认** |
| **存到哪** | 待定。**可参照的两条既有约定**：启动配置与日志在 `%APPDATA%\AITerminal\`（问题包落 `…\AITerminal\reports\problem-<时间戳>\problem.txt`）；业务数据在 `~/.aiterminal/`（`logs/`、`knowledge/`） | 来源：`_HANDOFF.md:573`、`docs/README.md:1010`、`docs/README.md:489-491` |
| **格式** | 待定。**若沿用项目既有做法**：markdown（知识库章节是 `chapters/*.md` + 索引 `index.json`） | 来源：`core/vendor_knowledge.py:3-9` |
| **AI 用在哪一步** | **只用于"整理已有原文"这一步**：把已收集到的记录**归类/归纳成可读小结**。**不得产生新事实** —— 无来源就留空 + 空状态 | 强制约束，来源：`~/.dsh/memories/USER.md` 第 8 条、`core/vendor_knowledge.py:11-12`、`:31-32` |
| **无 AI 时怎么退化** | **必须可用**：收集与展示走**确定性路径**（结构化的原始记录直接列出/按关键词与时间索引），AI 只是可选的"更易读"增强，缺失时退化成"原始记录清单 + 明确空状态"，**绝不静默、绝不编** | 同上；"能不用 AI 就不用" |
| **与现有能力的边界** | 不复用/不替代：**问题包（Ctrl+Shift+D）** 是"一次性把现场交给 AI"；**本地资料库**（`~/.aiterminal/knowledge/`）是"沉淀好用的命令"；**会话日志**（`~/.aiterminal/logs/`）是"原始流水"。本功能是"**按时间回看我用过什么、哪一步没走通**" | 来源：`_HANDOFF.md:559-573`、`docs/README.md:1010` |

### 13.3 实现时应当遵守的项目既有纪律（避免重蹈第 12 节）

1. **不要先发明一套视觉** —— 先盘点 `design/system/` + `design/mockups/`，并跑一次
   `objectName × QSS 规则` 审计（见 12.13）。
2. **改完 QSS 必跑** `_automation/check_qss_tokens.py`（见 12.1）。
3. **AI 不得产生新事实**；无来源就留空 + 空状态文案（照 `core/vendor_knowledge.py:31-32` 的写法）。
4. **记录/展示层别依赖 AI**：确定性路径优先（见第 6 节红线）。
5. 如果新增 `.cpp`，**记得在 `cpp/CMakeLists.txt` 登记**（见 12.12）。
6. 如果新增 RPC 方法，在 `engine/service.py` 的 `self.methods` 里注册（现有 RPC 全表见 8.3）。

---

## 附：本文档的事实边界与已知未确认项

| 项 | 状态 |
|---|---|
| 四套测试的计数 | **已查清（2026-10-02）**：`run` = 运行时断言数，**593 / 48 / 27 / 63**（当场实跑，0 失败）。源码里能数到的 `check(` 字面量是 533 / 39 / 34 / 45 —— **两者本来就不等**（很多 `check()` 在循环/数据表里），**没有用例丢失**。详见 2.3 节 |
| H3C `display interface brief` | **未确认**（本地命令数据里未见该命令名；只见 `display interface vlan-interface [ brief [ down ] ]`） |
| 锐捷 `show ip interface brief` | **未确认** |
| 锐捷"恢复出厂"命令 | **未确认**（不写） |
| 光模块/接口输出的**字段名** | **未确认**（本轮只拿到命令与语法，没有逐字段说明） |
| 端口镜像 / QoS / VXLAN 查询命令 | **未确认**（本地命令库未命中） |
| 对话框默认尺寸 560–960 / 高 ≤82% | 来自跨会话记忆条目，**本轮未在源码里逐行复核** → 未验证 |
| 是否存在独立的"批次排期文档" | **未确认**（只找到 `core/vendor_knowledge.py` 注释里的批次命名） |
| `design/round-01`、`round-02` 的结论 | **未确认**（无 README） |

**官方文档入口（本轮联网核到的）**：
- H3C：[配置文件管理（H3C 官方）](http://wwwsg.h3c.com/cn/d_202305/1841386_30005_0.htm)、
  [新华三产品资料使用宝典](https://zhiliao.h3c.com/EnclosureTechDoc/ziliao/16434421406507.pdf)、
  [H3C KMS 技术问答](https://kms.h3c.com/questions/dispcont/284543?sort=1)
- Huawei：[Displaying Optical Module Information（官方配置指南）](https://support.huawei.com/enterprise/en/doc/EDOC1100333876/94ef2802/displaying-optical-module-information)、
  [Optical Modules Fail to Communicate（官方排障指南）](https://support.huawei.cn/enterprise/en/doc/EDOC1100313480/c5e30425/optical-modules-fail-to-communicate)
- Ruijie：官方命令参考 PDF
  [RG-S6500/S6250-48XS8CQ 等 RGOS 12.5(1) 命令参考](https://www-file.ruijie.com.cn/Fu/Article/985e0dab-ae3d-4929-ac9e-1611cc7c8547/v0/%E5%AE%98%E7%BD%91%E5%8F%91%E5%B8%83/PDF/00-pdf/RG-S6500%2C%20S6250-48XS8CQ%20RGOS%2012.5%281%29B0509P1%20%5B%20S6580%2012.5%281%29B0409P1%20%5D%20%5B%20S6980%2012.5%281%29B0609P1%20%5D%20%5B%20S6920-4C%2012.5%281%29B1209P1%20%5D%20%E5%91%BD%E4%BB%A4%E5%8F%82%E8%80%83%20%28V1.0%29.pdf)、
  [锐捷基础配置（官方 PDF）](https://www-file.ruijie.com.cn/Fu/Article/00b1aa0e-fc53-4449-a03b-d658ce52fc6b/v1/20250110102456/PDF/00-pdf/01-%E5%9F%BA%E7%A1%80%E9%85%8D%E7%BD%AE.pdf)

> 本地离线可用性更好：`_vendor_docs/`（9 份厂商文档，含 h3c_cr16000 / h3c_s12500 /
> huawei_ar_v200r010 / huawei_ce16800 / huawei_netengine_ar / huawei_usg6000f /
> ruijie_n18000 / ruijie_rsr77 / ruijie_s6160，锐捷为可直接 grep 的 `fulltext.txt`）。
> 上述 11 节里标【厂商原文】的锐捷行号，都可以在
> `C:\Users\linyin\deepseek_harness\_vendor_docs\ruijie_n18000\fulltext.txt` 里直接定位。

## 靶机与判定纪律（2026-10-03 真机验收补充）
1. **物理锐捷（真机身份，实测原文）**：`COM5` / 9600 8N1 = **S2928G-E**，`System software version : RGOS 10.4(2b12) Release(154722)`，
   SN **G1G304J002013**；提示符 `Ruijie>`（用户模式）→ `enable`（**无需口令**）→ `Ruijie#`（特权模式）。
2. **分页符两家不同**：**锐捷 RGOS = `--More--`**，**H3C = `---- More ----`**；
   **两者都在 `core/batch.py` 的 `PAGING_RE` 里**（正则含 `-{2,}\s*More\s*-{2,}` 与 `--More--` 两支）⇒ 自动翻页无 MISSING-CAPABILITY。
   锐捷两条关分页命令（`screen-length disable` / `terminal length 0`）均 Unknown ⇒ **靠空格/回车翻页**。
   参考：HCL(H3C) 侧 `screen-length disable` **被接受**（本次实测：接受后该会话不再分页）。
3. **自动翻页只在批量/巡检**（`core/batch.py` 的 `auto_page`，翻页计数写进巡检日志）；
   **交互式终端不自动翻页**，由用户按 **空格 / PageDown**（`TerminalView.cpp` 只有 `Key_PageDown` 分支）—— **别再误以为交互也该自动翻**。
4. **判定纪律：会话前置状态必须声明**。同一控制台会话里的先前配置会**残留**（例：会话内发过 `screen-length disable` 后，后续命令不再分页）
   ⇒ 同一现象在不同前置状态下会得到**两种结论**；每条判定必须写明"新会话 / 已 enable / 已发过哪些命令"。

## 14. 第十二版交付（2026-10-17，补记）
本节按当时的校验单（`D:\ait_delivery\校验单.txt` 2026-10-17 版）补记，**内容全部有据**：
- 主题：**删除「快速预设」+ 对话框瘦身**。
  「新建连接」删掉整块快速预设（含"没选预设就收起输入区"的旧逻辑，表单打开即展开），
  并按协议收起用不上的行：选 eNSP Pro 时收起「用户名」「密钥文件」（平台免密自动登录，密码留着填设备密码），
  选 Telnet 时收起「密钥文件」（SSH 专用），Console 仍整块「连接参数」收起。
  带主题实测高度：SSH 473px / Telnet 439px / Console 442px / eNSP Pro 611px
  （改前 eNSP Pro 那套约 1040+px，底部「记住该连接」与按钮被切出屏幕）。
- 交付：`AITerminal-便携.zip` 6452053235 字节，
  SHA256 `71C93D1CE73F54CB823E6E7050300AA6BBB61EC2C1A29A057C9B1930E35298C1`；
  包内 exe 72402765 字节，SHA256 `377141E657C1259227FA2BCA5F1AAC8E2F1CDA3ACAB20BB2F6AE74F701706848`；
  解压后 36203 个文件 / 11823875078 字节。
- 注意：该版 zip 在第十三版出包时被**同路径覆盖**，旧 exe 亦已被重建覆盖 ⇒ **无法原样复现**。

## 15. 第十三版交付（2026-10-18）
本版三件事（均已验证）：

一、**应用图标修复**（用户反馈：左上角图标要改，其它界面也是，任务栏也要改）
- `main.py` 增加 `ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AITerminal.Terminal.0.7")`
  —— **任务栏图标必须靠它**；不设置时任务栏显示的是宿主进程图标（脚本方式启动即 python.exe 白图标）。
  实测旧 exe 的内嵌图标能提取出 40x40 ⇒ "任务栏白图标"不是缺资源，而是缺这个 ID。
- 图标查找**双路径兜底**：`base_dir()/resources/icons/app.ico` → `sys._MEIPASS/resources/icons/app.ico`。
  打包态 `base_dir()` 返回 exe 所在目录，而 PyInstaller 的 `datas` 解压在 `_MEIPASS`；
  只认前者会出现"资源明明打进包了却静默没图标"（实测 `dist\resources\icons\app.ico` 原本不存在）。
- 另把 `resources\icons\app.ico`（15462 字节，6 尺寸 16/32/48/64/128/256，全 32bpp）
  复制到 `dist\resources\icons\`，供非打包方式启动时也能取到。

二、**eNSP Pro 连接端口修复**（真缺陷）
- `create_session` 不显式传 port 时，eNSP 分支此前会落到 SSH 默认的 22 ⇒ 连接器去连 22 端口、
  一路重试后失败（表现为"点了连接没反应"）。现在 `0 / None / 22` 都视为"未指定"，落到 **8443**。
- 实测：不传 port 也能连上（16.2 秒），随后 4 条命令全部 `send=True`、各约 3 秒、全程 `is_connected=True`。

三、**键鼠 GUI 测试框架修复**（为"用真实键鼠测通"服务）
- G16：把已知会挂死的 `await_modal` 换成 **20 秒有界轮询**
  （资料库这一步曾让全量跑每次卡死：日志 20 分钟不增长、CPU 仅 5%）。
- G4：等待改为 **90 秒硬上限 + 每 5 秒心跳**（修复与诊断一体）。
- G18：诊断源从**终端控件**改读 **AI 面板** —— 快照 worker 的提示走 `session_manager.ai_output`，
  原来从终端里 grep ⇒ "界面提示"永远为空（假阴性），真实失败原因一直被吞掉。
- 新增探针：`tools/probe_snapshot_mech.py`（采集机制直测）、
  `tools/probe_snapshot_gui.py`（真实 MainWindow + 真实 worker + 监听 AI 面板）。

交付与校验（实测值）：
- exe 72406270 字节 / 2026-10-18 00:51:46 / SHA256 `578842D3DEC4E489EE29713A00273D26777CBD20724C3E20275C88CFE6B0B730`；
  冒烟通过（launcher + 主进程 RSS 190MB）。
- zip 6452057427 字节 / 01:02:34 / SHA256 `5B4CA352B8A585573BE1897527B9C0211113D16DE044BD794191FF25ACD1E783`。
- `verify_delivery_zip.py --zip <路径> --dir <路径> --crc` ⇒ **归档与目录逐条一致、CRC ✔ 无损坏条目**（exit=0）；
  文件数 36203 / 36203，总字节 11823878583 / 11823878583，差 0。
- 校验单已换为第十三版（`D:\ait_delivery\校验单.txt`）。

**本版未完成（如实）**：
- 全量键鼠测试最好成绩 42 PASS / 2 FAIL，**未全绿**；当前卡点在 G4（第一次 `processEvents` 内
  某处理器不返回，心跳只打印一次为铁证；已排除四处：连接对话框处理器、守卫 `guard_modals`、
  连接线程、完成回调）。
- 2 个 FAIL：「在清单里选中设备（鼠标优先，未中则键盘/回填）」；「快照采集成功」
  （诊断源已修，真实原因待下一次全量跑打印）。

## 16. 第十四版（2026-10-21 全量键鼠复跑：30 PASS / 11 FAIL 的定性 + 修 3 条真 bug）

一次全量 `%TEMP%\gui_drive_1.log`（20:49，133 行）打出 30 PASS / 11 FAIL。
**先把 11 条拆成"环境的 / 我们的"，再动手**——不拆就会去改本来没坏的代码。

### 16.1 8 条是环境级联：eNSP Pro 平台那台机器不在

| 事实 | 证据 |
|---|---|
| 平台不可达 | `Test-NetConnection 192.168.31.11 -Port 8443` → **False**；`Test-Connection` → `DestinationHostUnreachable`；80/8080/22 同 |
| 本机地址没变 | `Get-NetIPAddress` → 以太网 `192.168.31.13` |
| 因此拉取失败 | 探针实测：点「从平台拉取设备」**20.9s** 后弹 `拉取设备失败`，下拉只剩「（拉取失败）」 |
| 因此连不上 | 无设备清单 ⇒ 走回填兜底 `sandbox=2/node=22` ⇒ 连接 30.0s 超时 ⇒ 无标签页 |

级联失败的 8 条：拉取清单 / 清单里选设备 / 连接后出标签页 / 状态栏报已连接 /
拿到会话标签对象 / 跳过终端测试 / 巡检加会话 0 行 / 巡检跑完给汇总。
⇒ **平台不在线时这 8 条不可能绿**，别去改连接代码。

### 16.2 三条真 bug（已修 + 有回归测试）

**① 下拉框上的回车被当成"点确定"**（`ui/connect_dialog.py:37-58` 新增 `_QuietCombo`）
- 现象：鼠标点协议下拉 → 键盘选 eNSP Pro → 回车 ⇒ 弹「参数不完整」；设备下拉框上回车再弹一次；
  预填参数齐全时则**直接开始连接**。日志里 GUARD 记到两次 `参数不完整`，就是这两次回车。
- 根因：`QComboBox::keyPressEvent` 不认回车（未处理的键走 `showPopup(); return;`，事件既不
  accept 也不 stop）⇒ 冒泡到 `QDialog::keyPressEvent` ⇒ 被当成默认按钮的点击。
- 修法：非可编辑下拉框一律 `event.accept()` 吞掉 Return/Enter；可编辑的（串口号/波特率）放行。
  覆盖 `connect_dialog.py` 里全部 10 个非可编辑下拉框（协议/设备/停止位 + 串口高级对话框 7 个）。

**② 预填密码 + 新密码 = 两段拼接**（`ui/connect_dialog.py:61-101` 新增 `_PrefillPasswordEdit`）
- 现象：日志 `[FAIL] 密码按键盘输入进去了   19 位`。19 = 上次记住的 9 位 + 新敲的 10 位。
  实测落盘证据：`last_connection.password` 解码后正是 19 位拼接串。
- 危害：密码显示成圆点，用户看不出；连上去设备只回 `Error: The password is invalid.`
  —— 与 2026-10-19 用户报的"点了连接就说密码错误"同一根因（`set_config` 注释有记）。
- 修法：`setText()` 预填即进入"待替换"态；第一次按可打印字符或 Ctrl+V ⇒ 先全选再插入；
  先按方向键/Home/End/退格 ⇒ 取消待替换（正常编辑不受影响）。

**③ 「显示/隐藏 AI 面板」只会收起、永远打不开**（`ui/main_window.py:517-524`）
- 现象：日志 `[FAIL] AI 未启用时点开关会被拦住并给提示   既没切换也没提示`。
- 根因：菜单项按 `right_tabs.isVisible()` 判断，但 AI 面板收起用的是**宽度归零**
  (`set_ai_panel_visible(False)`)，`right_tabs` 恒 visible ⇒ 永远只调"收起"，
  第二次起被 `_ai_open` 早退挡掉。
- 修法：判据换成 `self._ai_open`（与图标条同一入口）。

### 16.3 一条是已知代价，不是本轮 bug

`[FAIL] 检索期间界面没被按住   单次事件循环最长 2.79s`：本地资料库 `~/.aiterminal/knowledge`
的 `.index.json` 676 MB / 27,297 篇 / 518,512 段，冷检索要把整份索引读进内存并建 per-chunk
缓存；同一轮日志里这次检索 **96.4s**、RSS **805 → 7885 MB**；修完上面三条后复跑两次，
检索分别 **97.4s（最长单次阻塞 2.28s）** 和 **87.5s（1.05s）**。检索本身已在工作线程
（`ui/knowledge_dialog.py:1065` `kb-dialog-search`），x 秒级停顿来自大 JSON 解析时的 GIL
占用，**不是**把活儿干在了 GUI 线程上。要压下去得换索引结构（sqlite/分片），属于架构改动，
本轮不做（记录在此，避免下次又误判成"有人在主线程里 sleep"）。
注意这条**本身会飘**：同一台机三轮测到 2.79s / 2.28s / **1.05s**，阈值是 2.0s ⇒
它时红时绿，反应的是 GIL 抢占的运气，不是功能回归；拿它当红线判断会误伤。

### 16.4 本轮新增的探针与测试

| 路径 | 用途 |
|---|---|
| `tools/probe_connect_flow.py` | 复刻 G3 驱动序列并逐步打印弹窗/焦点/密码状态；`--repro` 用临时配置复现 20:49 现场（9 位旧密码、无沙箱号），**不碰用户真实 settings.json** |
| `tools/probe_pwd_prefill.py` | 单点验证"预填密码 + 键盘输入"的拼接现象 |
| `tests/test_connect_dialog_keys.py` | 10 项：回车不提交（协议/设备两个下拉框）、密码替换、← 后插入、粘贴替换 |
| `tests/test_ai_panel_toggle.py` | 8 项：AI 面板 打开 → 收起 → 再打开（第三次就是原来挂掉的一步） |

判定纪律（本轮踩到，写下来）：**探针里不要用真 `Settings()`**。`Settings` 是单例
（`config/settings.py:96-108`），第一次实例化就锁死了配置文件；要用临时配置，必须在自己
`ConnectDialog()` 之前把 `ui.connect_dialog.Settings` 换成 `lambda: Settings(config_file=tmp)`。

### 16.5 三轮实跑的数字（同一台机、同一时段）

| 轮次 | 日志 | 结果 | 说明 |
|---|---|---|---|
| 20:49（原始） | `%TEMP%\gui_drive_1.log` | **30 PASS / 11 FAIL** | 问题清单的来源 |
| 21:14（修完后复跑） | `%TEMP%\gui_drive_2.log` | **32 PASS / 9 FAIL** | 密码 19→**10 位 PASS**；AI 面板 `_ai_open False → True` PASS；GUARD 里两次「参数不完整」**消失** |
| 21:22（配置隔离版） | `%TEMP%\gui_drive_3.log` | **33 PASS / 8 FAIL** | 只剩平台不可达的 8 条；检索卡顿 1.05s 这次在阈值内 |

**剩下 8 条全部是"平台不在线"的级联**，平台起回来（或换 `--host`）才有意义复测：
拉取清单 / 清单里选设备 / 出会话标签页 / 状态栏已连接 / 拿到会话标签对象 /
跳过终端测试 / 巡检加会话 0 行 / 巡检跑完给汇总。

### 16.6 测试自身的卫生问题（已修）

`tools/gui_drive_test.py` 原来直接读写**用户真实** `~/.aiterminal/settings.json`：
「记住该连接」在 accept 时把密码写回去，20:49 那次就把拼接串写进了 `last_connection.password`
（用户下次打开对话框会拿这段废密码去连）。现在改成临时配置（`config.settings.CONFIG_FILE`
指向 `%TEMP%\ait-gui-drive-*\settings.json`），并**种一个确定的现场**：上次连接 = eNSP Pro +
9 位旧密码、无沙箱/设备号 —— 于是"预填密码后整段替换""拉取失败走回填兜底"两条每轮都真被走到
（21:22 那轮日志里 `走了回填兜底: True` + `密码 10 位` 就是证据）。
⚠️ 用户现有 `settings.json` 里那条 19 位废密码（20:49 的残留）**未擅自改动**，要清就在界面上
重输一次密码并保存。
> 2026-10-22 复核：`~/.aiterminal/settings.json` 的 `last_connection.password` 解码后是 **10 位**
> 且以 `Huawei@` 开头，不等于 `admin@123 + Huawei@123`（19 位）——那串废值已经不在了（用户重存过
> 或后续 accept 覆盖），**无需再清**。

---

## 17 改名：AITerminal → NetOps Terminal（网络运维终端）

### 17.1 定了什么、为什么改

产品名不再叫 `AITerminal`（"AI" 是个过时的前缀，跟"网络设备运维"这件事对不上）。2026-10-22 定：

| 维度 | 值 |
|---|---|
| 英文名 | `NetOps Terminal`（界面/文档里带空格） |
| 中文名 | `网络运维终端` |
| ASCII 形态 | `NetOpsTerminal`（进 exe / 目录名 / AppUserModelID —— 路径里不能有空格） |
| 仓库 | `github.com/Linyin42/netops-terminal` |
| 版本 | 仍是 `v0.7`（没借改名跳版本；`cpp/CMakeLists.txt` 的 0.8.0 是 C++ 那套自己的号，本轮没动） |

范围：**品牌层 + 仓库 + 产物名**。数据目录与外部契约不动 —— 理由见 17.3。

### 17.2 分界线：大小写决定改不改

`AITerminal`（驼峰）= 品牌，改；`aiterminal`（小写）= 路径/标识，一律保留。
唯一例外是 4 个"小写但是名字"的地方：`setup.py` 的 `name="aiterminal"` → `netops-terminal`、
入口点 `aiterminal=main:main` → `netopsterminal=main:main`、`packaging/aiterminal.desktop` →
`netops-terminal.desktop`、图标 `aiterminal.png` → `netops-terminal.png`。

主要映射：

| 旧 | 新 | 出现处 |
|---|---|---|
| `AITerminal.exe` / `dist/AITerminal` | `NetOpsTerminal.exe` / `dist/NetOpsTerminal` | build.spec、build.bat、make_portable、README |
| `AITerminal-linux.spec` | `NetOpsTerminal-linux.spec` | 文件名 + 各处引用 |
| `AITerminal-便携.zip` | `NetOpsTerminal-便携.zip` | make_portable:352、verify_delivery_zip |
| `AITerminal.Terminal.0.7` | `NetOpsTerminal.Terminal.0.7` | main.py:147（AppUserModelID，任务栏分组靠它） |
| `AITerminal.Team` / `AITerminal Team` | `NetOps Terminal Team` | main.py:133、setup.py:80 |
| `AI智能终端` / `AI 智能终端` | `网络运维终端` | 窗口标题、README、使用说明 |
| `AITerminalControl/1.0` | `NetOpsTerminalControl/1.0` | control_api.py:507（HTTP `Server` 头，没有客户端比较） |
| `APP_NAME = "AITerminal"` | `APP_NAME = "NetOps Terminal"` | control_api.py:35（`/status` 报给外部智能体的名字） |

### 17.3 不动清单（改了会真的丢东西）

| 保留项 | 原因 |
|---|---|
| `~/.aiterminal/`（settings、vault、`knowledge/` 676 MB、`control.json`、`control-audit.log`） | 用户已有配置/资料库/控制令牌，改名等于全部失联 |
| `%APPDATA%\AITerminal\usage-log`（`core/usage_log.py:192`） | 用量日志数据目录，历史记录都在里面 |
| `tests/snapshots/usage_dto_baseline.json` 里那条 `...\AITerminal\usage-log` | 快照存的是上面那个真实路径 |
| `AIT_*` 环境变量、`AITERMINAL_MCP_DEBUG` | 外部脚本/本机资料库约定 |
| `data_tools/mcp/aiterminal_mcp.py` 文件名、`SERVER_NAME="aiterminal"`、`mcp-aiterminal` 标记、`serverName: aiterminal` | DSH 里已注册的 `mcp__aiterminal__*` 10 个工具靠它，改名 = 外部 AI 客户端找不到桥 |
| `aiterminal-script.log` / `aiterminal-slow.log` / `aiterminal-rag.log` / `%TEMP%\aiterminal-engine.log` | 交付机上的既有日志与排障手册引用 |
| Linux 安装前缀 `~/.local/share/aiterminal`、`/opt/aiterminal` | 已装用户升级路径（desktop 的 Exec 靠 sed 换前缀） |
| `cpp/**` + `tests/cpp_ui_regression.py`、`tests/check_terminal_colors_snapshot.py` | C++ 旧界面本轮不改：它的 exe 仍叫 `AITerminal.exe`，改字符串不重建就是未验证状态 |

### 17.4 怎么落地的

工具 `tools/rebrand_netopsterminal.py`：`--apply` 真改、`--check` 查残留，幂等（改过的规则自动
跳过），按文件类型分两套规则（文档 → `NetOps Terminal` 带空格；代码 → `NetOpsTerminal` 无空格），
保护名单写死在 `SKIP_FILES`/`SKIP_EXACT`/`SKIP_PREFIX_DIRS` 里。

自动改名：**114 个文件 / 471 处**（第一轮 92/323 覆盖 .py/.md/.spec/.sh/.bat/.json/.desktop，
第二轮补 .qss/.ps1/.html/.svg/.gitignore 22/148）。

两个踩过的坑（都已修）：
1. `.gitignore` 里 `!AITerminal-linux.spec` 是**白名单**（`*.spec` 全忽略，只放行两份）——spec
   文件改名后这条就不再匹配，新 spec 会变成"未跟踪"。
2. `DISPLAY_FIXES` 的键写成正斜杠 `"ui/main_window.py"`，而 `walk()` 给的是 `os.sep` 拼接的
   相对路径 ⇒ 整组"给人看的带空格文案"静默不生效（界面标题还是 `NetOpsTerminal - …`）。现在
   键统一用 `os.path.join`，并加了"键在仓库里找不到对应文件"的告警。

手工收尾 6 处（脚本不该碰、也不该猜的）：
- README 标题与仓库链接（自动替换会把 URL 变成带空格的 `NetOps Terminal.git`，Git 用不了）；
- `packaging/netops-terminal.desktop` 的 `Name`/`Name[zh_CN]`（`StartupWMClass` 保持 ASCII）；
- `core/manual.py:53,374` 手册署名带空格；
- `tests/smoke_test.py:2555` 署名断言跟着改；
- 使用说明.txt 副标题；
- `tests/cpp_dialog_color_probe.py` 的 exe 名**回退**成 `AITerminal.exe`（它跑的是 C++ 那套）。

### 17.5 验证数字

| 项 | 结果 |
|---|---|
| `python -m py_compile` × 50 个改动过的 .py | 0 语法错 |
| `tests/test_connect_dialog_keys.py` | **10 / 10** |
| `tests/test_ai_panel_toggle.py` | **8 / 8** |
| `tests/test_mcp_bridge.py` | **54 通过 / 0 失败**（桥的 `serverInfo.name` 仍是 `aiterminal`） |
| `tests/test_control_api.py` | **33 / 33** |
| `tests/test_app_paths.py` | **18 / 18** |
| 窗口标题探针（offscreen 起 MainWindow 读 `windowTitle()`） | **7 / 7**，标题 `NetOps Terminal · 网络运维终端 v0.7` |
| `python -m PyInstaller build.spec --clean` | **76 s 成功**，产物 `dist/NetOpsTerminal.exe` 69.2 MB |
| GitHub | `Linyin42/AITerminal` → `Linyin42/netops-terminal`（旧地址自动重定向），本地 `origin` 已切 |

### 17.6 现在还叫 AITerminal 的地方（都是故意的）

`PROJECT-DIGEST.md`（本文，历史记录）、`core/usage_log.py`（数据目录）、
`tests/snapshots/usage_dto_baseline.json`（同上）、`cpp/**` 6 个文件、
`tests/cpp_ui_regression.py`、`tests/check_terminal_colors_snapshot.py`、
`design/**`（设计历史稿，未入 git）、`tools/rebrand_netopsterminal.py`（规则表本身）。

C++ 那套要改的话是另一轮：`cpp/CMakeLists.txt` 的 target、`cpp/src/main.cpp` 的
`setApplicationName`（它还决定 `%APPDATA%\AITerminal` 数据目录，得配迁移），并重建验证。

### 17.7 改名后的真机回归（Console，2026-10-22 18:2x）

用户把锐捷交换机接在 **COM5（9600，`Ruijie>`，S2928G-E，RGOS 10.4(2b12)）**，用它验了两件事：

1. **`ui/terminal_widget.py` 那条未提交改动**（浮层候选与已输入内容相同时回车被吞 ⇒ 用户看到
   "按了回车没反应，得按两次"）。真机探针 `tools/probe_console_enter_real.py`：**13/13 通过**
   —— 敲完整 `show version` 且浮层候选相同 → 回车**立刻发**、设备回显 RGOS 版本；敲 `show vers`
   且浮层有尾巴 → 第 1 次回车只把 `ion` 补发给设备（不发回车）、第 2 次才发。
   不依赖设备的回归：`tests/test_terminal_enter_hint.py` **14/14**（含小键盘 Enter、脏候选）。
2. **改名后的交付物整条链路**：`dist\NetOpsTerminal.exe`（新产物）起来后 `control.json` 里
   `"app": "NetOps Terminal"`，经 MCP 桥 `ait_open(serial COM5)` + `ait_send show version`
   拿到真实设备输出 ⇒ 控制通道 + 串口 + 改名都没问题。

⚠️ 注意：DSH 侧注册的 MCP 桥是**已交付的旧副本**（`ait_status` 的状态行仍打印 `AITerminal 0.7.0`），
协议兼容、不受仓库改名影响 —— 这也正是 17.3 里坚持不改 `serverName` / 桥文件名的原因。

### 17.8 交付目录改名（`D:\ait_delivery`）

旧目录里 6.4 GB 的 `knowledge/`、`llm_data/` 没有重拷，做的是**原地改名 + 换 exe + 改引用**
（脚本 `tools/migrate_delivery_dir.py`，默认试运行、`--apply` 才动手）：

| 项 | 结果 |
|---|---|
| 目录 | `D:\ait_delivery\AITerminal` → `NetOpsTerminal`（顶层只剩新名） |
| exe | 拷入 `NetOpsTerminal.exe`（69.2 MB，本轮 PyInstaller 产物）；旧 `AITerminal.exe` 存为 `AITerminal.exe.bak-20261022-1830`，没删 |
| 包内文案 | **59 处**：启动.bat 1、自检.bat 1、换成正式包并刷新图标.bat 13（GBK）、使用说明.txt 2、连接模拟器操作文档.md 6、测试记录 26、data_tools/给AI的说明.md 10 |
| 包内 MCP 桥 | 用仓库里改名后的版本覆盖（交付机走 `NetOpsTerminal.exe --script data_tools\mcp\aiterminal_mcp.py`） |
| 打包态验证 | **5/5**：`serverInfo.name` 仍是 `aiterminal`、10 个 `ait_*` 工具齐全、应用没开时提示 `... —— NetOps Terminal 桌面程序可能没在运行`、stderr 无 traceback |

**没动的**：`AITerminal-便携.zip`（6.4 GB，2026-10-18 那版，内容仍是旧名，重打包属另一件事）、
`AITerminal_fix.exe`（那是另一个修复版二进制，不是本包的正式 exe）、`校验单.txt`（历史记录）。

顺带澄清一处误判：DSH 的 MCP 注册**并不指向交付目录**，而是 `cwd=仓库` + `python -u
data_tools\mcp\aiterminal_mcp.py`。所以 `ait_status` 打印旧名只是**长驻进程加载的是改名前的代码**，
DSH 下次重启会自动变成新名，配置一行都不用改。



---

## 18 本地模型重训（2026-10-22，按本机 6GB 显存配置）

**一句话**：重跑 QLoRA 管线，同一套 56 题上**严格命中 32.1% → 48.2%**（配对净 +9 题，符号检验 p=0.035），
生产 GGUF 已换新；不带检索仍是 10% 上下 ⇒ **微调替代不了检索，`ait` 档继续 opt-in**。
完整报告：`D:\ait_llm\train\RETRAIN_REPORT_20261022.md`。

### 18.1 本轮配置（都是这台机器逼出来的）

| 阶段 | 步数 | seq | bs/accum | LoRA | 用时 | train_loss |
|---|---|---|---|---|---|---|
| CPT | 3,000 | **256** | 1/1 | r16 α32 | 4,093 s（68 min） | 1.891 |
| SFT | 1,500 | **256** | 1/1 | 接 CPT 续训 | 2,417 s（40 min） | 1.761 |

数据：`build_train_set.py` 重扫 41.2 万条切片 → CPT **43.79 MB ≈ 2870 万 token**、SFT **19,905 条**
（seq≤256 可用 9,217 条）。导出：f16 7.50 GB（合并 252 矩阵）→ Q4_K_M **2.33 GB**，自检 398↔398 张量一致。

### 18.2 关键发现：6GB 显存有"吞吐悬崖"（以后别再踩）

同一份 4B nf4 + LoRA + 梯度检查点，bs=1，只改 seq_len：

| seq_len | s/步 | token/s | 峰值显存 |
|---|---|---|---|
| **256** | **1.30** | **196** | 4,782 MB |
| 384 | 5.91 | 65 | 5,052 MB |
| 448 | 12.37 | 36 | 5,186 MB |
| 512 | 7.3–8.5 | 60–70 | 5,509 MB |

判据：**GPU 占用 100% 但功耗只有 60 W、CPU 单核跑满** ⇒ 不是算力不够，是 Windows 驱动把显存换出到内存
（WDDM 分页）。所以本机选 seq 256 不是"为了省时间而牺牲质量"，是**唯一不触发分页的工作点**。

第二个坑：SFT 样本长度随机时，bs=1 每步都要按新尺寸申请激活块，同样掉进分页区 ——
实测 **20 s/步**。`train_qlora.py` 新增 `--sft-sort-len`（按 token 长度分桶、窗口 256 内打乱）后
降到 2.06 s/步（seq512）/ 1.47 s/步（seq256）。**跑 SFT 必须带这个开关。**

第三个坑：训练期间并行跑任何吃显存的东西（浏览器 GPU 加速、另一个基准脚本）都会让吞吐掉 10 倍 ——
本轮就因为一个吞吐基准进程和 CPT 抢卡，把 CPT 拖到 60–170 s/步，白等了半小时。

### 18.3 数据卫生

重建前的 `cpt_corpus.txt` 是 **68.1 MB**，比真语料大 55%：历史重复累积（脚本 append 写、旧版没先清空）。
已备份 `*.bak-20261022`，本轮产物是干净的 43.79 MB。**每次重建前先确认文件是新建而不是追加。**

### 18.4 产物与回滚

| 产物 | 路径 | 标识 |
|---|---|---|
| 生产 GGUF（新） | `D:\ait_llm\models\Qwen3-4B-AIT-Q4_K_M.gguf` | SHA256 前 16 位 `CCAB51F5CB6EC8C2` |
| 旧 GGUF（回滚） | 同目录 `.bak-20261022` | `205A89B2AE2BC8F9` |
| 适配器 | `lora\cpt\final`、`lora\sft\final` | 各 126.06 MB（r16）；旧 r8 版在同目录 `.bak-20261022-1930` |

回滚一条命令：`Copy-Item D:\ait_llm\models\Qwen3-4B-AIT-Q4_K_M.gguf.bak-20261022 D:\ait_llm\models\Qwen3-4B-AIT-Q4_K_M.gguf -Force`

### 18.5 评测口径与结果

同一批 56 题、同判分脚本（`eval_local_qa.py` + 严格/宽松两口径），新旧模型各跑一遍：

| 组 | 旧（10-18） | 新（10-22） |
|---|---|---|
| tuned+rag 严格 | 32.1% | **48.2%** |
| tuned+rag 宽松 | 39.3% | 57.1% |
| tuned 严格（无检索） | 7.1% | 10.7% |

配对：都中 15 / 只新 12 / 只旧 3 / 都不中 26 ⇒ 净 +9 题，**p = 0.035**。
分厂商：h3c 31→56%、huawei 44→69%、tplink 0→12%、ruijie 38→38%（持平）。

⚠ 诚实边界：**h3c/huawei 的提升集中在"命令主干 + 参数齐全"**，ruijie 没动、tplink 样本仅 8 题；
且无检索时仍是 10% 量级 —— 不要把这个模型当成"能背文档"来宣传。

### 18.6 复现

见 `RETRAIN_REPORT_20261022.md` §7（五条命令：重建集 → CPT → SFT → 导出 → 评测）。
所有日志在 `D:\ait_llm\train\log_retrain_*.txt` / `log_export_retrain.txt` / `log_eval_*.txt`。

### 18.7 四组对比补齐 + 接进应用（2026-10-22 晚）

完整 56 题、同一判分脚本重跑四组（此前只有 tuned 两组的对照）：

| 组 | 严格命中 | 宽松 | 均长 |
|---|---|---|---|
| base（官方 4B） | 7.1% | 7.1% | 686 |
| base+rag | 30.4% | 33.9% | 129 |
| tuned（自训） | 10.7% | 10.7% | 475 |
| **tuned+rag** | **48.2%** | 57.1% | 152 |

按类别（带检索）：common 31.2%→43.8%、longtail 29.2%→54.2%。检索环节两组一致（命中源 82%、
事实 100% 进上下文），差异来自模型"读没读进去"，不是检索。

**接进应用**（原来点「启动本地模型」固定起 mid 档，自训模型用不上）：

- `config/settings.py` 新增 `local_tier`（默认 `""`=自动探测；`ait`=自训）；
- `core/local_model.py: start_server(tier=None)` 读该设置并传 `--tier`，且**档位不一致时明确报错**
  （端口上已有实例时 `run_server.py` 会拒绝起新进程并返回成功 —— 不说清楚就等于"切了没生效"，实测踩过）；
- `ui/ai_settings_dialog.py` 本地模型区新增「档位」下拉（自动 / ait），保存落盘。

验证 7/7：命令行带 `--tier ait`、pid 文件 `tier=ait`/`model=Qwen3-4B-AIT-Q4_K_M.gguf`、
`core.llm.build_client(provider="local")` 出话、收尾无残留。回归：`test_local_model.py` 30/0、
`test_local_model_ui.py` 17/0、`test_local_model_startup_label.py` 11/0、`test_ai_connection.py` 通过。

⚠ 切档位要先停一次本地服务；`ait` 档英文手册类问题略差，默认仍是"自动"。

### 18.8 复盘：本轮 5 次失败 = 2 个根因（同一个"共享单实例资源"家族占 4 次）

| # | 现象 | 根因 | 属类 |
|---|---|---|---|
| 1 | `ModuleNotFoundError: No module named config` | 验证脚本放 `%TEMP%`，却用 `dirname(dirname(__file__))` 推仓库根 | 路径推导 |
| 2 | e2e 验证 `tier='mid'`（应为 ait） | 8080 上已有评测起的 mid 服务，`run_server.py` 拒绝起新进程**却返回成功** | 共享端口 |
| 3 | 同参数命令被 anti-stuck 拦下 | 环境已变（服务被清）却原样重试，没换调用方式 | 重试纪律 |
| 4 | 清掉服务再验，仍是 `tier='mid'` | 我重跑基线评测时它又起了 mid 服务，验证脚本仍假设"端口是我的" | 共享端口 |
| 5 | 基线评测 `ConnectionResetError` 中断 | 我的验证脚本收尾 `stop_server()` **杀掉了评测正在用的服务** | 共享端口 |

**结论：4/5 是同一个根因 —— 把 `:8080` 的 llama-server 当成独占资源**：起之前不查谁在跑，
停的时候不问是不是自己起的，也不让两个作业排队。第 1 条是独立的路径推导错误。

**已修**（commit 3a03a9f）：`core/local_model.py: start_server()` 增加档位一致性闸门 ——
端口上已有实例且档位与请求不一致时**返回失败并说明**，不再"静默成功"。

**剩余纪律（写下来比记住可靠）**：
1. 起/停本地服务前先 `local_model.server_status()` 看 `running` + `tier`；
2. **只停自己起的服务**：脚本里记录"我起过没有"，没起过就不许 `stop_server()`；
3. 评测与任何"起停服务/占 GPU"的验证**串行跑**，不并行（本机 6GB 卡 + 单端口，没有并行空间）；
4. 同参数命令失败两次后，必须先改变量（参数/环境/脚本），不能原样第 3 次。

### 18.9 复盘：功能测试轮的 6 次失败 —— 4 次同一根因（动手前没读契约）

| # | 现象 | 根因 | 属类 |
|---|---|---|---|
| 1 | `ImportError: cannot import name 'strip_ansi'` | 看 grep 列表就猜 API，它其实是 `TerminalBuffer` 的方法 | 契约未读 |
| 2 | `TypeError: 'int' object is not callable`（`waiter.pages()`） | 同上，`pages` 是 `@property` | 契约未读 |
| 3 | 巡检报「串口被占用」 | 巡检自己开 COM5，而我的脚本还占着 —— 独占资源没建模 | 契约未读 |
| 4 | `ValueError: 不支持的协议: serial` | 我按小写传参（**顺带暴露真 bug**：`build_connector` 大小写敏感，已修） | 契约未读 |
| 5 | `-replace` 没命中却改写了文件 | PowerShell 里 `\x1b` 是 ESC 字符，文件里是字面 `\x1b` 文本 | 写手混用 |
| 6 | `edit` 拒绝「文件已变更」 | 上一条 `Set-Content` 改写了 mtime，读快照失效 | 写手混用 |

**结论**：4/6 是同一根因 —— **调用或写入前没有先读定义**：API 形状（函数/方法/属性）、资源归属（谁占 COM5）、
文件契约（`edit` 依赖读快照）。另外 2/6 是同一文件混用 shell 改写与 `edit`。

**判定**：修根因，不换路线。测试矩阵本身有效（30 PASS / 1 FAIL / 3 N/A），三个 FAIL 里只有 1 个是产品侧真缺口，
另两个是我的测试参数错（阈值 2000 与真实 1364 不符、协议名大小写）。

**纪律**：
1. 调模块函数前，先 grep 到 `def X(` 或 `@property` 的确切行，不凭列表写调用；
2. 同一文件只留一个写手：要么 `edit`，要么 shell 重写，不混；
3. 脚本里涉及独占资源（串口/端口/GPU），先显式释放再交给下一个消费者；
4. FAIL 先分类：产品 bug / 我的测试参数 / 环境缺失 —— 本轮三类都出现了，混为一谈会改错东西。

### 18.10 复盘：两个"真缺口"的修法 —— 一个加代码，一个补数据

功能测试矩阵里有 1 个产品侧缺口（用户模式 → 什么都不提示）和 1 个数据缺口（锐捷 `show …` 没进补全索引）。
两个都修了，但**修法完全不同**，混着用会修坏东西。

**A. 自动 enable（代码）**

- 只在提示符形如 `Ruijie>` / `Switch>`（`^[A-Za-z][\w.\-]*>\s*$`）时判成用户模式；
  `<H3C>` / `[H3C]` / `#` 一律不算 —— 华为/华三没有 enable 层，误发 `enable` 会把回显污染成
  `% Unknown command.`，用户看到的就是"应用在乱敲命令"。
- 接入两条路径：巡检（`auto_enable` 默认开，enable 成功后才 `disable_paging`）、
  交互会话（建会话后自动切，`connection_info` 里提示「已自动 enable（Ruijie> → Ruijie#）」）。
- **不静默失败**：没勾自动 enable 但设备在用户模式时，note 明说"停在用户模式"并指路"勾自动 enable + 填密码"。
- 真机：`privilege=privileged`、`show running-config` 拿到 1364 字符全量；关掉自动 enable →
  `privilege=user` + 4 条提示。`tests/test_enable_mode.py` 13/13。

**B. 补全数据（数据，不是代码）**

- 症状：`suggest('show vlan','ruijie')` 0 候选，`show …` 家族整共 2 条（3,377 条命令里）。
- 根因不在 `command_suggest.py`：`tools/doc_to_commands.py` 只认 `章/*.htm`，
  而锐捷「命令参考」是**单文件 fulltext.txt**（312 份），它一份都读不了。
- 修法：新增 `tools/ruijie_fulltext_commands.py`（整行成命令 + 动词限 show/display + ≤4 词 +
  只允许 `[...]`/`{a|b}` 尾巴 + 剔中文 + **按命令名去重 / 跳过已入库**），跑两批：
  命令参考 312 份（228,101 → 2,867 → 新增 2,269）+ 配置指南 366 份（140,296 → 2,251 → 新增 311）；
  `commands.jsonl` 18,108 → **21,291** 行 / 6.40 MB。
- **关键在去重**：补全索引按命令名建键，36.8 万条直接写进去会让文件涨 40 倍、加载从 0.3s 掉到
  好几秒；去重后索引 commands 12,387 → **15,570**，加载 316 → **~330 ms**（几乎不变）。
- 验证：`show vlan` → 9 候选、`show vlan ` → 5、`show ver` → 6、`show run` → 5、
  `show mac-vlan ` → 9+8、`show ` → 20+20；索引规模断言 12,387 → 15,570。
- 质量抽检：`show of`（OpenFlow）/`show si`（自定义应用）看着像碎片，回原文都是
  `【命令格式】` 小节标题，属真实命令 —— 不要用"看着不像"当过滤依据。
- **一本通这批导了又回滚**：26 份能贡献 295 条，但抽出来大量是缩写（`show runn` / `show ver` /
  `show ap sum` / `show ver detail`）。缩写进索引后，用户敲 `show run` 第一个看到的是
  "打一半的命令"，比没候选更干扰 —— 判断标准不是"是不是真命令"，而是"作为补全候选有没有用"。
  回滚方式：直接把批次备份 `commands.jsonl.bak_20261023_2137` 覆盖回去（21,291 行），
  回滚后索引自动回到 15,570（索引是派生物，不需要手工清）。

**回归**：`smoke_test` 647/0、`regression_fixes` 83/0、`command_suggest_test` 48/0、
`test_enable_mode` 13/0、`feat_matrix` 23/0（新加 `2.0 自动 enable`）。

**纪律**：
1. 看到"数据不对"先找**谁写这份数据**，别在消费端（补全/索引）加特判；本轮 806 行 `^show [a-z]`
   就在文档里躺着，症结是抽取器读不了这种文档格式。
2. 数据快照断言（索引规模）数据变了就改数字并注明来源，不放宽成 `> 0`。
3. mode 切换要能自愈：设备掉电/断开/DTR 变化都会回用户模式，测试脚本必须每次自己 `ensure_privileged`，
   否则矩阵会随机红（本轮就红过一次：`probe_session_enable` 关会话后设备回到 `Ruijie>`）。

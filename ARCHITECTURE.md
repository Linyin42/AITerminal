# NetOps Terminal 架构地图（ARCHITECTURE.md）

> 生成方式：**全部为实读事实**（文件清单+行数、`#include` 统计、RPC 派发表、混层规则的 `文件:行`），非凭印象。
> 目的：把"以后 UI 可以完全重写而不动核心"做成可执行的迁移路线。**本文件是第一步交付物，尚未动刀。**

---

## 0. 一句话现状

界面层（C++/Qt6）**没有**反向污染核心（无 `core → ui`、无 `engine/core → 旧 ui`），
但**业务判断规则有一批长在 UI 里**（终端三色、知识库来源拼装、使用总结归类呈现、闸门分支、补全触发），
所以现在的真实分层是"**core+engine 干净，ui 偏厚**"——重写 UI 时这些规则会被重写一遍并走样。

---

## 0.5 ★ 头号架构发现（实读）：契约不可用

> **39 个 RPC 方法中，22 个没有任何显式错误字段** —— 新 UI 只能靠「业务字段缺失」猜错。
> 实读分布：无错误字段 **22** ／ `{"error":"..."}` **6** ／ `{"ok":bool,"message":...}` **5** ／
> 两者混用 **5** ／ `{"available":false}` 空状态 **1**。详见 `API-CONTRACT.md` §2。
> 这不是"少个 schema"，而是**成功/失败的表达方式有五种**，多数方法失败时只表现为"该有的字段没了"。

### S1.5「错误语义统一」（**行为变更；本轮只文档化，不改代码**）
目标：`{"ok": bool, "error": {"code": "...", "message": "..."}}`（成功 `ok=true, error=null`）。
统一后**会受影响的 UI 调用点**（为将来重写 UI 铺路，逐处实读所在文件）：
| 影响面 | 位置 | 现在怎么处理错误 |
|---|---|---|
| 会话打开/写入 | `MainWindow.cpp`（submitLine/sendToSession） | 看字段有没有 |
| 安全闸门 | `MainWindow.cpp:750-786` | 读 `read_only`，无错误字段 |
| 知识库两栏 | `KnowledgeDialog.cpp` | 读 `available`/`found` |
| 使用总结 | `UsageSummaryDialog.cpp` + `MainWindow.cpp` | 读 `total`/分组字段 |
| AI | `AiPanel.cpp`、`AiSettingsDialog.cpp` | 读 `ready`/`ok`/`message`（唯一较规范） |
| 巡检/差异 | `PatrolPanel.cpp`、`DiffDialog.cpp` | 看结果数组空不空 |
| 设备库 | `HostListPanel.cpp`、`HostDelegate.cpp` | 看数组长度 |

### S8.5「契约补类型」（排在 S2–S7 之后）
脚本逐 RPC **真调一次抓真实返回**，把字段 **JSON 类型**回填 `API-CONTRACT.md`；
需真机的（`session.*`/`serial.*`/`patrol.run`）标 **「需真机」**，不编。

---

### S3 前置教训（2026-10-03，踩过并纠正）
**造基线前必须先读真实调用参数。** 我第一次抓 `knowledge.command` 基线时只传了 `{"text": cmd}`，
结果三条全 `found=False / available=False / examples=0` —— 那是"**缺参数行为**"，不是 UI 的真实拼装；
若拿它当等价门槛，就会把**错的当成规格**（比没有基线更危险，已把那份快照改名作废）。
- **真实参数（实读 `cpp/src/ui/MainWindow.cpp:472-475`）**：`params.insert("vendor", vendor)` +
  `params.insert("text", text)` + `params.insert("examples", 3)`；
- **接受判据**：同名查询必须 `found=True`（厂家"数据不完整"如实标注也算对）；
  出现 `available=False` 一律**重查，不留档**；
- 另：`KnowledgeDialog::showCommandKnowledge(info)` **已是纯渲染**（只吃引擎返回），
  所以 S3 的搬运动作集中在 **MainWindow 调用点 + 引擎返回字段 + 少量 UI 字面量**。

### 两条工具/探针纪律（2026-10-03，各踩过一次）
1. **探针的兜底分支不得覆盖主结果**。例：我调 `ai.ask` 时先发正确的 `{"question":…}`，又在"看起来没回答"
   时补发一发键名错的 `{"text":…}` 并**把结果赋值覆盖** —— 于是回显的是第二发的 `问题为空`，
   **第一发的真实回答被我自己的 fallback 吃掉了**。规矩：**只发一次；失败就原样打印 error 原文**再决定，
   不静默替换。（另注：**宽容的接口会掩盖调用错误** —— `text.classify` 缺参时回 `{"labels":[]}` 不算报错，
   不能当"参数到达"的证明；验证 RPC 要用**有必填校验**的接口，或校验返回**内容**。）
2. **写含中文的 `.ps1` 别用 pwsh here-string**：会被控制台代码页弄坏（`娌＄紪…`）→ 引号被吞 →
   `ParserError: The string is missing the terminator`。规矩：**脚本文案用纯 ASCII**，或**改由 Python 以 UTF-8 写**。
   另记：`_automation/run_all_checks.py` 跑 `.ps1` 项必须带 `-ExecutionPolicy Bypass`（本机默认策略拒绝未签名脚本）。

### 生成物文件的纪律（2026-10-03 踩坑）
**`API-CONTRACT.md` 是 `_automation/gen_api_contract.py` 的整文件重写产物** ⇒ 我手写进该文件的
「头号架构发现」段落在下一次重跑生成器时**被整段抹掉**（重跑后 grep 命中 0），而生成器自己算出来的表行保留。
- **通用形式**：**凡是"生成物"文件，手写内容一律无处安放** —— 手写段落要么进生成器模板，要么进独立文档（如本文件）。
- 处置：该段落已按**重数结果**补回（**41 个方法 / 24 个无错误字段**，且保留"39/22 → 41/24 为什么变"的更正说明）。

### 工具显示与真实内容差一层（2026-10-03 踩坑）
用 `Get-Content | ForEach-Object { '  ' + $_ }` 打印源码时，**每行前面被我加了 2 个空格** ⇒ 显示缩进 = 真实缩进 + 2。
据此手写补丁锚点会**整体多 2 空格而失配**（S5 的 UI 补丁第一次就这么挂了：实测真实缩进 12，我按 14 写）。
- 规矩：**锚点缩进一律用 `^[ \t]*` 动态取行首空白**（`re.match(r"[ \t]*", line)`），不要凭显示值手抄；
- 也不要用"行首到匹配点的距离"当缩进（那会连带 `const bool …` 一起算进去 —— 我第二次就是这么错的，实测 41）。

## 1. 模块清单（实读：文件数 + 行数）

### 1.1 `cpp/src/ui` —— 32 个文件（最厚）
| 文件（行数） | 职责 |
|---|---|
| `TerminalView.cpp(1699) .h(277)` | **终端控件：ANSI 渲染 + 全部着色规则 + 补全浮层 + 按键分发**（最厚，混层最多） |
| `MainWindow.cpp(1073) .h(131)` | 主窗口：菜单/动作、会话页管理、**安全闸门分支**、RPC 调用编排 |
| `KnowledgeDialog.cpp(608) .h(70)` | 知识库两栏：**来源拼装、示例覆盖标注、短讲解行**（含渲染+取数混合） |
| `ConnectDialog.cpp(600) .h(88)` | 连接对话框（SSH/Telnet/串口/eNSP 字段与预设） |
| `UsageSummaryDialog.cpp(413) .h(63)` | 使用总结 5 页签：**分组呈现、排错链拼装** |
| `HostDelegate.cpp(405) .h(22)` | 设备卡片绘制（副标题/徽标/状态点） |
| `AiPanel.cpp(343) .h(49)` | AI 助手面板：问答流、状态行、A1 切换提示 |
| `AiSettingsDialog.cpp(274) .h(46)` | AI 设置（提供方/密钥/测试连通） |
| `TitleBar.cpp(293) .h(55)` | 自绘标题栏 + 菜单栏 + QSizeGrip |
| `PatrolPanel.cpp(298) .h(40)` | 多设备巡检面板（结果表） |
| `DiffDialog.cpp(255) .h(46)` | 配置差异比对（差异表） |
| `HostListPanel.cpp(222) .h(42)` | 设备库（搜索/分组计数/新建入口） |
| `ProblemReport.cpp(190) .h(56)` | 问题包（键记录/浮层状态/字节/截图） |
| `NavRail.cpp(157) .h(29)`、`StatusStrip.cpp(142) .h(55)`、`SessionTabBar.cpp(55) .h(31)` | 左导航条 / 状态栏 / 会话标签 |

### 1.2 `cpp/src/core` —— 6 个文件（应保持"无 UI 依赖"）
`AnsiParser.cpp(316)`（ANSI/CSI 解析）、`EngineClient.cpp(252)`（**唯一的引擎 RPC 客户端**）、`Theme.cpp(299)`（配色 token）+ 对应 .h

### 1.3 `cpp/src/model` —— 4 个文件
`Host.cpp(206)`（设备模型+副标题）、`Session.cpp(3)`（占位）

### 1.4 `engine/` —— 4 个文件
`service.py(**1125**)`（**RPC 收口 + 全部 handler**）、`rpc.py(129)`（JSON-RPC 编解码）、`main.py(53)`（启动/stdio）

### 1.5 `core/` —— 22 个文件（业务核心，能复用的资产）
`command_suggest.py(988)` 补全策略、`knowledge.py(928)` 资料库、`ai_agent.py(743)` AI、`session.py(601)` 会话、
`usage_log.py(468)` **使用总结记录器/归类/排错链**、`serial_connector.py(449)`、`web_search.py(434)`、
`patrol.py(398)`、`batch.py(379)`、`telnet_connector.py(376)`、`manual.py(364)`、`terminal.py(320)`、
`llm.py(279)`、`xlsx_writer.py(205)`、`vendor_knowledge.py(192)` 厂商库读层、`ssh_connector.py(178)`、
`logger.py(146)`、`rpc.py`/`connector.py(106)`、`safety.py(95)` **危险/配置判定**、`diff.py(90)`、`ensp_connector.py(414)`

### 1.6 `config/` —— 4 个文件
`settings.py(529)` 配置读写、`templates.py(213)` 命令模板、`presets.py(173)` 预设

---

## 2. 依赖方向（实读统计）

```
cpp/src/ui    --include core/-->  26 处      （EngineClient / Theme / AnsiParser）
cpp/src/ui    --include model/->   6 处      （Host 等 DTO）
cpp/src/ui    --include ui/---->  38 处      （对话框互相引用：交叉引用偏多）
cpp/src/core  --include ui/---->   0 处   ✅ 无反向依赖
engine/、core/ --import ui----->   0 处   ✅ 不含旧 PyQt 界面
ui --RPC--> EngineClient --stdio JSON-RPC--> engine/service.py --调用--> core/*
```

**结论（好的一面）**：层次方向**没有倒挂**——`core`/`engine` 干净、可复用。
**问题所在**：`ui` 内部 **38 处互相 include**（对话框互相 new/调用），且**业务判断规则长在 ui 里**（见 §3）。

**已知的具体依赖问题（实读）**
1. `ui → ui` 38 处：`MainWindow` 直接 `new` 各具体对话框（`ConnectDialog`/`AiPanel`/`PatrolPanel`/`DiffDialog`/`KnowledgeDialog`/`UsageSummaryDialog`/`ProblemReport`/`AiSettingsDialog`）——**UI 之间是"具体类型硬编码"，不是接口**；
2. `TerminalView.cpp` 既做渲染，又做**语义分类**（三色）与**补全触发**（1699 行，职责最杂）；
3. `KnowledgeDialog.cpp` 既渲染，又**拼装来源/覆盖文案/短讲解行**（把"讲什么"写在 UI 里）。

---

## 3. "UI 里混了业务逻辑"清单（每条给实证 `文件:行` + 该下沉到哪）

| # | 规则 | 现在在哪（实证） | 为什么算混层 | 该下沉到 |
|---|---|---|---|---|
| **D1** | **终端三色分类**（提示符/命令/IP·MAC=蓝、成功=绿、**接口名=琥珀**） | `TerminalView.cpp:612,620,640`（`promptRe/ipv4Re/looksLikeAddress/okRe/ifaceRe` + 判定顺序 667–686） | 这是**领域规则**（"什么算接口名、什么算 IP"），换 UI 就要重写一遍 → 一定走样 | `core/text_classify.py`（新），引擎经 `text.classify` 输出**每段 run 的语义标签**；UI 只把标签→颜色（颜色仍属 UI/Theme） |
| **D2** | **使用总结归类与排错链** | 归类/链的核心已在 `core/usage_log.py`（✅），但 **UI 仍拼装呈现**：`MainWindow.cpp:736,1165` + `UsageSummaryDialog.cpp`（5 页签/列/跳转） | 半下沉状态：**"怎么算一组、链怎么闭合"应从引擎返回结构化数据**，UI 不该推算 | 引擎补 `usage.summary` 返回**已分好组+已闭合的链**（DTO），UI 纯渲染 |
| **D3** | **知识库来源拼装 / 短讲解 / 示例覆盖文案** | `KnowledgeDialog.cpp:356,358,366`（`examplesCoverage`/`tech_note`/来源行） | "**该显示哪些字段、缺列怎么表述**"是产品口径，写在 UI 里 → 未来每个 UI 都要重写这套口径 | `core/vendor_knowledge.py` + 引擎：返回**面向展示的 DTO**（含"暂无"文案与来源去重结果） |
| **D4** | **安全闸门分支**（只读判定→弹确认→拦截后的处理） | `MainWindow.cpp:750-786`（`safety.check` 分支 + `answer != Yes` 擦行） | 判定本身在 `core/safety.py` ✅，但**"拦截后要做什么"（退格擦行/计数/提示）散在 UI** | 引擎：`safety.gate` 返回**决策 DTO**（放行/需确认/拦截 + 拦截后的动作序列），UI 只执行动作 |
| **D5** | **补全触发与策略** | 策略已在 `core/command_suggest.py` ✅（C++ 只送 prompt 原文）；UI 保留 150ms 去抖 + 浮层渲染：`TerminalView.cpp:837,888,889` | 基本正确，**仅剩"去抖/落地键"属交互**（保留在 UI 合理） | 维持现状；把"何时该弹/弹什么"继续锁在引擎（`commands.suggest`） |
| **D6**（新发现） | **配置根与 usage-log 根不一致** | 配置/知识库根 `~\.aiterminal\`；usage-log 根 `%APPDATA%\NetOps Terminal\usage-log\` | 属**产品决策**（数据落点），不是 UI 问题，但会影响"UI 重写后数据从哪读" | `config/settings.py` 统一根（建议 `~\.aiterminal\`）+ 一次性迁移 —— **待决策** |
| **D7**（新发现） | 串口设备字段冗余（`host:"COM5"` 与 `serial_port:"COM5"` 并存、且带 `port:22`） | `~\.aiterminal\settings.json` 实读 | 属**数据模型**问题，会让新 UI 猜字段 | `config/settings.py` 归一化（保留一个端口字段 + 迁移）—— **待确认** |

---

## 4. UI ↔ 引擎契约（实读：`engine/service.py` 派发表，**共 39 个方法**）

> 这份就是**未来任何 UI 的接入面**；参数/返回/错误语义将在第 2 步固化成 `API-CONTRACT.md`。

| 域 | 方法 |
|---|---|
| 引擎 | `engine.hello`、`engine.status` |
| 设备库 | `hosts.list`、`hosts.save`、`hosts.delete` |
| 安全 | `safety.check` |
| 会话 | `session.open`、`session.write`、`session.command`、`session.close`、`session.resize`、`session.list` |
| 串口 | `serial.ports`、`serial.remember` |
| 使用总结 | `usage.state`、`usage.list`、`usage.setMonitor`、`usage.mark`、`usage.merge`、`usage.split`、`usage.clear`、`usage.candidate`、`usage.summarize` |
| AI | `ai.status`、`ai.ask`、`ai.optimize`、`ai.test`、`ai.getSettings`、`ai.setSettings`、`ai.setProvider` |
| 知识库 | `knowledge.stats`、`knowledge.list`、`knowledge.search`、`knowledge.index`、`knowledge.import`、`knowledge.delete`、`knowledge.command` |
| 巡检 | `patrol.run` |
| 差异 | `diff.compare` |
| 补全 | `commands.suggest` |

**现状问题**：契约**没有集中定义**（派发表直接指向 `self.m_xxx`），且**没有参数/返回的 schema**、**错误语义不统一**（有的返回 `{"error": "..."}`，有的 `{"ok": false, "message": ...}`）——这正是"新 UI 接不进来"的根因。

---

## 5. 目标分层（建议）

```
                 +---------------------------+
   新 UI（任意）  |  ui/  薄壳：渲染 + 事件转发 |   只依赖 api 的接口与 DTO
                 +-------------+-------------+
                               |  只允许这一条边
                 +-------------v-------------+
   api/ 契约门面  |  EngineClient(接口) + DTO   |   方法签名/请求响应结构集中定义
                 +-------------+-------------+
                               |
                 +-------------v-------------+
   core/ 纯逻辑   |  session/knowledge/usage/  |   **不依赖任何 UI**、可单测
                 |  safety/suggest/diff/...   |
                 +---------------------------+
```

**硬约束（可机器检查）**
1. `cpp/src/core` 与 `core/`、`engine/` **不得** `#include "ui/..."` 或 `import ui`（当前 ✅，加 CI 断言守住）；
2. UI **只允许** include `api/`（`EngineClient` + DTO）与 `Theme`/`AnsiParser` 这类纯工具，**不得**直接 include 另一个具体对话框（改为信号/接口）；
3. 业务口径（"什么算接口名/缺列怎么写/链怎么闭"）只能出现在 `core/engine`，UI 里出现这类判定的字符串即视为违规。

---

## 6. 迁移步骤清单（风险低 → 高；每步都可独立验证）

| 步 | 内容 | 动哪些文件 | 预期不变的行为 | 如何验证 |
|---|---|---|---|---|
| **S1** | **契约冻结**：把 39 个方法固化成 `API-CONTRACT.md`（名/参数/返回/错误语义），引擎侧收拢为一个 `EngineFacade`（同签名、行为不变） | `engine/service.py`（只搬派发表）、新增 `API-CONTRACT.md` | 所有 RPC 行为逐字节不变 | **四套 + 各断言全绿**；再用"现状快照"冒烟：对 39 个方法各调一次并记录返回 |
| **S1.5** | **统一错误语义**（行为变更；现在只文档化） | `engine/service.py` 全部 `m_*` | **会变** | 统一后按 §0.5 调用点清单逐处复测 |
| **S2 ✅ 已完成（含 parity + 单测收尾）** | **下沉 D1 终端三色**：新增 `core/text_classify.py`，引擎加 `text.classify`；先写**现状快照测试**（把 `TerminalView` 现有规则在几十条真实回显上的判定结果存成基线），搬完必须逐条一致 | 新增 `core/text_classify.py`、`engine/service.py`、`TerminalView.cpp`（判定段→调用引擎/缓存） | 终端配色**完全不变**（像素级/行级断言） | `ait_terminal_colors_check.py` 17/0 仍绿 + 新单测对比快照 |
| **S3** | **下沉 D3 知识库呈现口径**：引擎返回展示 DTO（来源去重、"暂无"文案、覆盖标注） | `core/vendor_knowledge.py`、`engine/service.py`、`KnowledgeDialog.cpp` | 两栏内容/文案**一字不变** | `ait_knowledge_e2e_check.py` 11/0、`ait_index_sweep_check.py` 12/0 + 前后截图逐字节对比 |
| **S4** | **下沉 D2 使用总结分组/链**：`usage.summary` 返回已分组+已闭合链 | `core/usage_log.py`、`engine/service.py`、`UsageSummaryDialog.cpp` | 5 页签内容/计数/链**不变** | `ait_usage_log_check.py` 31/0 + 前3张 09-usage-*.png 逐字节对比 |
| **S5** | **下沉 D4 闸门决策**：`safety.gate` 返回决策 DTO + 拦截后动作 | `core/safety.py`、`engine/service.py`、`MainWindow.cpp` | 闸门行为不变（含点 No 后退格擦行） | 四套全绿 + 复现"点 No"用例（待交互轮） |
| **S6** | **拆 UI 交叉引用**：对话框之间改信号/接口；`MainWindow` 不 `new` 具体对话框（工厂/注册表） | `MainWindow.cpp` + 9 个具体对话框 + 5 个共享控件 .h | 全部界面行为不变 | 四套 + 23 张交付图**逐字节不变** |
| **S7** | **api/ 目录成型**：`EngineClient` 换成接口 + DTO 头文件；UI 只 include `api/` | `cpp/src/core/EngineClient.*` → `cpp/src/api/*` | 行为不变 | 四套 + 图逐字节不变 |
| **S8.5** | **契约补类型**：逐 RPC 真调抓返回，回填 JSON 类型 | `API-CONTRACT.md` + 新脚本 | 无（只写文档） | 类型表 == 真实返回 |
| **S8** | **守卫**：加机器可检查的依赖断言（`core` 不得 include `ui`；UI 不得 include 具体对话框） | CI/测试脚本 | 新增守卫本身 | 守卫测试先故意违规一次确认会红 |

**每步的收尾规矩（沿用本轮已定）**：改完**重建** → **冻结**（贴 sha256/mtime）→ **重出交付图** → 重写 `IMAGE-MANIFEST.json` → 断言 `mtime ≥ exe` → 四套 + 各断言当场输出。

---


### S2 收尾状态（2026-10-03）
- ✅ `cpp/src/core/TextClassify.{h,cpp}` 已抽出（UI 只做 label→颜色），旧规则块 `promptRe/ifaceRe` 残块归零；
- ✅ 快照门槛：33 条语料 `check_terminal_colors_snapshot.py --strict` **0 差异**；`ait_terminal_colors_check.py` 17/0；
- ✅ `text.classify` RPC 已注册（**供未来非 C++ UI，不进渲染路径**），契约 39→40；
- ✅ parity 严格版（传 `deviceColored`）**0 差异**，两侧分布一致 `{prompt 12, neutral 9, iface 8, ok 4}`；
- ✅ 单一入口 `_automation/run_all_checks.py`（13 项，总退出码；`--list`/`--only`）；
- ⏳ 待补：C++ 模块级单测（吃 33 条语料直接调 `classifyRun`）+ CMake 测试目标。

## 7. 如何"完全重写 UI"（新 UI 接入指南）

**新 UI 需要实现的东西（且只有这些）**
1. 一个 **RPC 客户端**：能对 `engine/service.py` 发 JSON-RPC（照 `API-CONTRACT.md`），并处理通知/流式输出；
2. **每段文本的语义标签 → 颜色的映射**（标签来自 `text.classify`；颜色是 UI 自己的主题）；
3. **事件转发**：键盘/粘贴/窗口尺寸 → `session.write` / `session.resize` 等。

**最小可运行接入点（三步）**
1. 起引擎：`python engine/main.py`（stdio JSON-RPC）；
2. 调 `engine.hello` 确认版本 → `hosts.list` 拿设备 → `session.open` 建会话；
3. 订阅会话输出 → 用 `text.classify` 拿标签上色 → `session.write` 发键。**到此就是一个能用的终端 UI，且不依赖任何现有 C++ 代码。**

**契约清单（新 UI 只需认这些）**：`API-CONTRACT.md` 的 39 个方法 + 通知通道；**不需要**知道 `cpp/src/ui/*` 里任何一个类。

---

## 8. 第 1 步交付状态

- 本文件 = **架构地图（第一步）**，等待批准后才动刀（S1 起）。
- **未改任何代码**；验收轮剩余项（HCL 半边 / A 剩余 / H 巡检 / O 收尾 / 交互轮）已在 `FEATURE-TESTS.md` 标注**暂停**。
- 待决策项：**D6 两个根统一**、**D7 串口字段归一化**（都属产品决策，不在纯结构迁移范围）。

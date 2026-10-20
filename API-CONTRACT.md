# AITerminal UI <-> 引擎 契约（API-CONTRACT.md）

> **S1 交付物**：由 `engine/service.py` **逐 handler 实读生成**（脚本 `_automation/gen_api_contract.py`）。
> 读不出来的字段写 `?` 并列入「未判定」，**不编造**。参数/返回的**类型**以代码实际用法为准（JSON 值均经 `json` 往返）。

## 0. 总览

- RPC 方法数：**42**（域：ai、commands、diff、engine、hosts、knowledge、patrol、safety、serial、session、text、usage）
  - 本轮 +1：`knowledge.explain`（技术解释，代码优先 / AI 兜底）；其余 41 个方法**签名与语义均未改**，
    只在 `commands.suggest` 与 `knowledge.command` 上**追加**了字段。

- 传输：stdio JSON-RPC（`engine/rpc.py`），错误以字段形式回传（见 §2）

## 1. 方法契约表

| RPC | handler（service.py:行） | 参数（实读 key） | 返回要点（实读 key） | 错误形态（实读） |
|---|---|---|---|---|
| `engine.hello` | `m_hello`:81 | ? (无 params.get / 动态) | `info` **(动态组装)** | （无显式错误字段） |
| `engine.status` | `m_hello`:82 | ? (无 params.get / 动态) | `info` **(动态组装)** | （无显式错误字段） |
| `hosts.list` | `m_hosts_list`:83 | ? (无 params.get / 动态) | `hosts` | （无显式错误字段） |
| `hosts.save` | `m_hosts_save`:84 | `host`, `original` | `hosts` | （无显式错误字段） |
| `hosts.delete` | `m_hosts_delete`:85 | `host` | `hosts` | （无显式错误字段） |
| `safety.check` | `m_safety_check`:86 | `command` | `result` **(动态组装)** | （无显式错误字段） |
| `session.open` | `m_session_open`:87 | `key`, `host`, `password` | `accepted`, `sid`, `reused` | （无显式错误字段） |
| `session.write` | `m_session_write`:88 | `data` | `ok`, `detail` | `{"ok": bool, "message": "..."}` |
| `session.command` | `m_session_command`:89 | `command` | `accepted` | `{"error": "..."}` |
| `session.close` | `m_session_close`:90 | ? (无 params.get / 动态) | `ok` | `{"ok": bool, "message": "..."}` |
| `session.resize` | `m_session_resize`:91 | `cols`, `rows` | `ok` | `{"ok": bool, "message": "..."}` |
| `session.list` | `m_session_list`:92 | ? (无 params.get / 动态) | `sessions`, `key`, `sid`, `name`, `state`, `host` | （无显式错误字段） |
| `serial.ports` | `m_serial_ports`:96 | ? (无 params.get / 动态) | `ports`, `in_use`, `last_port`, `count`, `usb_count`, `error` | `{"error": "..."}` |
| `serial.remember` | `m_serial_remember`:97 | `port` | `ok`, `error` | `{"error": "..."}` + `{"ok": bool, "message": "..."}` |
| `usage.state` | `m_usage_state`:99 | ? (无 params.get / 动态) | `st` **(动态组装)** | （无显式错误字段） |
| `usage.labels` | `m_usage_labels`:101 | ? (无 params.get / 动态) | `labels`, `window_title`, `summary_placeholder`, `search_placeholder`, `search_button`, `summarize_button`, `summarize_tooltip`, `group_titles`, `questions`, `wrong`, `dead`, `ok`, `chain_tab`, `chain_headers`, `chain_resolve`, `chain_merge`, `chain_split` | （无显式错误字段） |
| `usage.list` | `m_usage_list`:102 | `search` | `groups`, `chains`, `hits`, `state`, `markdown` | （无显式错误字段） |
| `usage.setMonitor` | `m_usage_set_monitor`:103 | `enabled` | `ok`, `error` | `{"error": "..."}` + `{"ok": bool, "message": "..."}` |
| `usage.mark` | `m_usage_mark`:104 | `chain`, `resolved` | `ok`, `chains` | （无显式错误字段） |
| `usage.merge` | `m_usage_merge`:105 | `chain`, `entry` | `ok`, `chains` | （无显式错误字段） |
| `usage.split` | `m_usage_split`:106 | `entry` | `ok`, `chains` | （无显式错误字段） |
| `usage.clear` | `m_usage_clear`:107 | ? (无 params.get / 动态) | `ok`, `cleared`, `state` | `{"ok": bool, "message": "..."}` |
| `usage.candidate` | `m_usage_candidate`:108 | `entry` | `self._usage().write_candidate(int(params.get(` **(动态组装)** | （无显式错误字段） |
| `usage.summarize` | `m_usage_summarize`:109 | ? (无 params.get / 动态) | `ok`, `error`, `provider` | `{"error": "..."}` + `{"ok": bool, "message": "..."}` |
| `ai.setProvider` | `m_ai_set_provider`:111 | `provider` | `ok`, `error` | `{"error": "..."}` + `{"ok": bool, "message": "..."}` |
| `knowledge.stats` | `m_knowledge_stats`:115 | ? (无 params.get / 动态) | `self._knowledge().stats()` **(动态组装)** | （无显式错误字段） |
| `knowledge.list` | `m_knowledge_list`:116 | `limit` | `documents`, `total` | `{"error": "..."}` |
| `knowledge.search` | `m_knowledge_search`:117 | `query`, `top_k` | `hits`, `query` | （无显式错误字段） |
| `knowledge.index` | `m_knowledge_index`:118 | `force` | `self._knowledge().build_index(force` **(动态组装)** | （无显式错误字段） |
| `knowledge.import` | `m_knowledge_import`:119 | `paths`, `recursive` | `added`, `index` | （无显式错误字段） |
| `knowledge.delete` | `m_knowledge_delete`:120 | `name` | `removed` | （无显式错误字段） |
| `ai.status` | `m_ai_status`:123 | ? (无 params.get / 动态) | `provider`, `providerLabel`, `model`, `ready`, `activeHasKey`, `configuredProviders`, `suggestedProvider`, `suggestedLabel`, `keyHint` | （无显式错误字段） |
| `ai.ask` | `m_ai_ask`:124 | `question`, `useKnowledge`, `top_k` | `answer`, `error` | `{"error": "..."}` |
| `ai.optimize` | `m_ai_optimize`:883 | `text` | `text`, `error` | `{"error": "..."}` |
| `patrol.run` | `m_patrol_run`:126 | `devices`, `commands`, `timeout` | `error`, `results` | `{"error": "..."}` + `{"ok": bool, "message": "..."}` |
| `ai.getSettings` | `m_ai_get_settings`:130 | ? (无 params.get / 动态) | `provider`, `model`, `base_url`, `hasKey`, `keyHint`, `providers` | （无显式错误字段） |
| `ai.setSettings` | `m_ai_set_settings`:131 | `provider`, `api_key`, `model`, `base_url` | `saved`, `provider` | （无显式错误字段） |
| `ai.test` | `m_ai_test`:132 | ? (无 params.get / 动态) | `ok`, `message` | `{"ok": bool, "message": "..."}` |
| `diff.compare` | `m_diff_compare`:134 | `old`, `new`, `dropBlank`, `dropPrompt`, `context` | `error`, `rows` | `{"error": "..."}` |
| `commands.suggest` | `m_commands_suggest`:138 | `limit`, `typed`, `vendor`, `model`, `prompt` | `error`, `token`, `next`, `sequences`, **`tech`**, **`tech_error`** | `{"error": "..."}` |
| `knowledge.command` | `m_knowledge_command`:143 | `vendor`, `text`, `examples` | `found`, `vendor`, `query`, `layer`, `error`, `examples`, `explanation`, `available`, `reason`, **`tech`** | `{"error": "..."}` + `{"available": false, ...}` 空状态 |
| `knowledge.explain` | `m_knowledge_explain` | `vendor`, `text`, `maxBytes`, `useAI`, `question` | `available`, `need_ai`, `source`, `how`, `note`, `reason`, `feature`, `file`, `command`, `syntax`, `view`, `markdown`, `ai_markdown`, `ai_file`, `prefer`, `truncated`, `candidates`, **`aiUsed`**, **`aiSaved`**, **`error`** | `available=false` + `reason`（**不抛异常**）；`error` 只表示"降级原因"，有值时其它字段仍可用 |
| `text.classify` | `m_text_classify`:147 | `runs` | `labels` | （无显式错误字段） |

### 1.1 `knowledge.explain` —— 技术解释（代码优先，AI 兜底）

**加粗字段是本轮新增的**，既有字段一个都没改语义（`commands.suggest` 的 `token`/`next`/`sequences`
与 `knowledge.command` 的既有 15 字段保持原样，新增字段一律追加）。

数据来自 `knowledge/<vendor>/tech/*.md`（`tools/build_tech_docs.py` 用**代码**从厂商原始文档
提取的正文）与 `tech/ai/*.md`（`tools/gen_tech_summary.py` 的 AI 整理稿，**物理隔离**）。

分派顺序（`core/tech_knowledge.py::explain`，全程零 AI，**先强后弱**）：

1. 技术名精确 → 通用命令卡片 → 索引级匹配（命令/分类名包含/命令词前缀）→ 同义词 → 跨厂商写法映射
2. 以上都不中才退到**正文级**检索（含"跨分类概念"识别）
3. 再查已沉淀的 AI 条目
4. 都没有 ⇒ `available=false, need_ai=true`，`reason` 说明查过什么、相近的有哪些

`useAI=1` 且 `need_ai=true` 时，引擎才会调模型：把候选分类的**真实厂商原文**作为唯一依据
喂进去，回答写回 `tech/ai/`（`aiSaved=true`）—— **同一个问题下次就是代码命中，不再花钱**。
`useAI` 默认 0：补全/浏览这类高频路径不该偷偷产生 API 费用。

`prefer` 是给界面的展示建议：问**技术**（`环路检测` 是什么）时 `"ai"`（卡片更合适，
原文可展开作依据）；问**命令**（`acl number` 怎么配）时 `"vendor"`（要原文里的参数与缺省值，
AI 归纳反而可能丢细节）。

## 2. 错误语义不统一（待统一）

**本轮不做统一**（属行为变更）。现状实读如下，同一份契约里存在多种错误表达：

- **（无显式错误字段）** —— 24 个方法：`engine.hello`、`engine.status`、`hosts.list`、`hosts.save`、`hosts.delete`、`safety.check`、`session.open`、`session.list`、`usage.state`、`usage.labels`、`usage.list`、`usage.mark`、`usage.merge`、`usage.split`、`usage.candidate`、`knowledge.stats`、`knowledge.search`、`knowledge.index`、`knowledge.import`、`knowledge.delete`、`ai.status`、`ai.getSettings`、`ai.setSettings`、`text.classify`
- **`{"error": "..."}`** —— 7 个方法：`session.command`、`serial.ports`、`knowledge.list`、`ai.ask`、`ai.optimize`、`diff.compare`、`commands.suggest`
- **`{"ok": bool, "message": "..."}`** —— 5 个方法：`session.write`、`session.close`、`session.resize`、`usage.clear`、`ai.test`
- **`{"error": "..."}` + `{"ok": bool, "message": "..."}`** —— 5 个方法：`serial.remember`、`usage.setMonitor`、`usage.summarize`、`ai.setProvider`、`patrol.run`
- **`{"error": "..."}` + `{"available": false, ...}` 空状态** —— 2 个方法：`knowledge.command`、`knowledge.explain`

> 影响：新 UI 必须**同时**处理这三种形态，容易漏判；统一时建议只保留一种（推荐 `{"ok": bool, "error": {"code", "message"}}`），但那是单独一步。


## 3. 未判定项（S1 如实标注，不猜)

- **参数读不出（无 `params.get`）**：`engine.hello`, `engine.status`, `hosts.list`, `session.close`, `session.list`, `serial.ports`, `usage.state`, `usage.labels`, `usage.clear`, `usage.summarize`, `knowledge.stats`, `ai.status`, `ai.getSettings`, `ai.test`
- **返回非字面量（动态组装）**：`engine.hello`, `engine.status`, `safety.check`, `usage.state`, `usage.candidate`, `knowledge.stats`, `knowledge.index`

这些项需要人工读 handler 内部（或运行时抓一次真实返回）才能定稿。


## 4. 与架构迁移的关系（S2 起用）

- 本表 = **未来任何 UI 的接入面**；S2 起每步重构后，本表**不得变化**（变了就是行为/契约变化，需要单独说明）。
- 建议在 S8 把本表变成**机器可读**（同目录 `api-contract.json`），并加「实际派发表 == 契约表」的断言。

> ★★ **头号架构发现（2026-10-03 重数）**：**41 个方法中有 24 个完全没有显式错误字段** —— 新 UI 只能靠
> 「业务字段缺失」猜错。**更正说明**：此为**重数**结果，早先记录为「39 个方法 / 22 个无错误字段」；
> 方法数随 `text.classify`、`usage.labels` 增加而到 **41**，无错误字段数同步由 **22 → 24**。
> 形态分布：无错误字段 **24** ／ `{"error":…}` **6** ／ `{"ok":bool,"message":…}` **5** ／
> 两者混用 **5** ／ `{"error"}+{"available":false}` **1**。统一方案见 `ARCHITECTURE.md §0.5` 的 **S1.5**。

> ⚠ `usage.labels`：**纯只读**（无参、无副作用），**供 UI 渲染展示文案**（不是渲染路径的判定逻辑）。

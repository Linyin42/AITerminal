# AITerminal

集成 AI 辅助功能的网络设备终端工具：SSH / Telnet / Serial(Console) 连接，
自动分析设备输出并生成下一步配置或排查命令。

![tech](https://img.shields.io/badge/Python-3.9%2B-blue) ![qt](https://img.shields.io/badge/PyQt6-6.6%2B-green) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

## 功能

- **三种连接方式**：SSH（密码/密钥）、Telnet、Serial Console
- **Console 兼容性对齐 MobaXterm**：完整帧格式（数据位/校验/停止位）、
  四种流控、DTR/RTS 电平、**Break 信号（进 BootROM）**、运行时切波特率、
  字符发送间隔、**断线自动重连**、本地回显、端口枚举带设备描述、端口占用报错可读
- **多标签会话**：同时管理多台设备，标签可拖动、单独关闭
- **终端体验**：ANSI 颜色清洗、`\r` 进度条覆盖处理、命令历史（↑↓）、Ctrl+C 中断、批量命令下发
- **AI 辅助**：识别华为 / H3C / Cisco / 锐捷语法，分析输出 → 生成下一步命令
  - 建议模式：AI 只给命令，人工确认后执行
  - 待执行队列：AI 生成的命令入队，勾选后统一下发
  - 自动模式：输出稳定后自动分析并下发，任务完成自动暂停
- **多模型提供方**：Anthropic Claude / **DeepSeek** / 任意 **OpenAI 兼容接口**
  （vLLM、Ollama、中转站）。各家的密钥、模型、接口地址分开保存，切换不互相覆盖
- **AI 会自己上网查**：内置联网检索（百度 / 必应 / DuckDuckGo / SearXNG，无需额外 Key）。
  AI 拿不准命令语法或报错含义时输出 `SEARCH: 关键词`，程序检索资料后回填再生成命令；
  也可以在"资料检索"页签直接提问，回答带可点击的参考来源
- **AI 会查你本地的资料**：点一个按钮把下载好的命令手册 / 配置案例导入本地资料库，
  自动抽取正文建索引；查命令时**先查本地资料库，再联网**。
  支持 PDF / Word / Excel / PPT / txt / md / html / csv / 日志 / 配置 / zip，
  还带检索测试，能当场验证"AI 到底查不查得到"
- **越用越厚的命令知识库**：查到的命令点「💾 存为资料」就存回本地资料库，
  自动带上时间、设备、厂商、标签；下次问同一主题直接命中自己的积累。
  笔记可按分类/标签/关键词筛选，还能一键合并导出成手册（Markdown 或可打印的 HTML）
- **屏幕内容可见**：AI 面板实时显示 AI 能看到的设备输出，一眼确认它有没有看全
- **批量下发按提示符判断**：等设备回到提示符再发下一条，自动翻页、遇到 `[Y/N]` 停下交人工；
  结果可导出 Excel
- **多设备并发巡检**：同时对多台设备跑同一组命令，结果导出 Excel
  （巡检信息 / 巡检汇总 / 命令明细 三个工作表）
- **安全兜底**：`reboot` / `delete` / `erase` / `undo all` / `factory-reset` 等危险命令代码级拦截
- **配置命令必须人工确认**：配置类命令下发前先弹出命令清单等你确认；
  AI 自动模式下生成的配置命令只入待执行队列、不会自动下发
- **厂商快捷命令**：按厂商提供常用巡检/配置命令，双击即发送
- **命令模板 + 批量下发**：选模板（创建VLAN/接口IP/静态路由/巡检/备份…）→ 填变量 → 预览 → 按间隔批量下发，危险命令自动跳过
- **配置快照与比对**：一键采集 `display current-configuration` 快照，行级着色比对两次配置差异，可导出 unified diff
- **会话恢复**：退出时记住会话列表，下次启动可一键重连
- **日志自动落盘**：按日期/设备分文件写入 `~/.aiterminal/logs/`，也可一键导出当前终端内容
- **可编辑系统提示词**：在界面里定制 AI 的行为约束

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key（三选一，在界面右侧 AI 面板里选提供方并填写即可）
#    a) DeepSeek：提供方选「DeepSeek」，填 sk- 开头的密钥
#    b) Anthropic Claude：提供方选「Anthropic Claude」，填 sk-ant- 开头的密钥
#    c) 自建/本地模型：提供方选「OpenAI 兼容接口」，填接口地址与模型名
#    也可以复制 .env.example 为 .env 并填写 ANTHROPIC_API_KEY

# 3. 运行
python main.py
```

Windows 用户也可以直接双击 `run.bat`（首次运行会自动创建虚拟环境并装依赖）。

## 接真实设备前先验证（推荐）

不用打开界面、也不用把密码贴到任何地方，命令行 5 分钟就能确认链路是否正常：

```bash
# 离线自检（不需要设备）
python tests/smoke_test.py

# 用内置模拟设备验证整条链路
python tools/fake_device.py --port 2323 --no-login --paging   # 终端 A
python tools/verify_device.py --protocol telnet --host 127.0.0.1 --port 2323   # 终端 B

# 验证真实设备（密码交互式输入，不回显、不进历史）
python tools/verify_device.py --protocol ssh --host 10.0.0.1 --user admin
python tools/verify_device.py --protocol serial --serial-port COM3 --baudrate 9600
python tools/verify_device.py --list-ports        # 先看有哪些串口
```

它会依次报告：连接是否成功 → 厂商识别 → 关闭分屏 → 每条命令能否"等提示符返回"地跑完。
完整验证流程见 **[docs/验证指南.md](docs/验证指南.md)**（含 14 项验证清单）。

## 自检

```bash
python tests/smoke_test.py
```

不需要真实设备、不联网，覆盖终端缓冲清洗、厂商识别、危险命令拦截、配置读写、
插件加载、Telnet 回退实现、模板渲染、日志落盘、快照采集、配置比对、
检索后端 HTML 解析、AI 检索协议与资料回填、串口发送/Break/DTR-RTS/改波特率、
本地资料库导入与多格式抽取（docx / xlsx / PDF）、本地优先检索链路、
一键沉淀（笔记落库 / 元信息 / 自动标签 / 增量索引后即可检索）、
Excel 写入器（多工作表 / 样式 / 非法字符清理 / 读回校验）、
笔记解析筛选与手册导出（Markdown / HTML / CSV）、
按提示符判定命令完成（分页翻页 / 确认提示暂停 / 静默兜底 / 缓冲区不被消耗）、
多设备并发巡检与 Excel/CSV 导出，以及主窗口的建标签 / 输出渲染 / 命令下发链路。

## 打包为 exe

```bash
build.bat
# 产物: dist\AITerminal.exe
```

或使用 setup.py：

```bash
pip install pyinstaller
python setup.py build_exe --onefile
```

## 目录结构

```
aiterminal/
├── main.py             # 入口
├── core/               # 连接器(SSH/Telnet/Serial)、会话、终端模拟器、AI 智能体、
│                       # 本地资料库、联网检索、批量下发、并发巡检、笔记手册、
│                       # Excel 写入、日志、配置比对
├── ui/                 # 主窗口、终端控件、会话标签、连接对话框、设备库(Vault)、
│                       # Catty 对话面板、AI 面板、资料库管理、巡检界面、
│                       # 模板/比对/提示词/串口控制对话框
├── config/             # 配置读写、预设与厂商命令库、命令模板库
├── plugins/            # 插件基类与插件管理器
├── resources/          # 图标、QSS 样式
├── tools/              # 验证工具：verify_device.py（真机验证）、fake_device.py（模拟设备）
├── tests/              # 冒烟测试
└── docs/               # 开发文档、验证指南
```

## 使用提示

- **用自己下载的手册查命令**：工具栏「导入资料」选好文件即可，资料统一放在
  `~/.aiterminal/knowledge/`（工具菜单「打开资料库目录」可直达）。
  导入后在「资料库」窗口用检索测试验证一下能不能查到。
- **把好用的命令沉淀下来**：AI 查到答案后点「💾 存为资料」，会存成带元信息的
  Markdown 笔记并立刻可检索。用得越久，自己的命令库越厚；笔记就是普通 .md 文件，
  随时能用编辑器改。
- **多台设备一起巡检**：工具栏「多设备巡检」勾选设备 → 填命令（或从模板载入）→ 开始，
  完成后导出 Excel。巡检用独立连接，不影响已经打开的会话。
- **批量下发建议用"等提示符"**：慢设备或长输出（`display current-configuration`）
  用固定间隔容易错位；遇到 `---- More ----` 会自动翻页。
- 登录华为/H3C 设备后建议先执行 `screen-length 0 temporary` 关闭分屏，
  避免长输出被分页卡住；Cisco 设备对应 `terminal length 0`。
- 采集配置快照、批量下发之前也建议先关掉分屏。
- Console 连不上时按这个顺序排查：端口是否被别的软件占用 → 波特率（9600/115200）
  → 帧格式 8-N-1 → 行结束符用 CR → 丢字符就加字符间隔。
- 资料检索默认先查本地、再联网；若联网结果不理想可在"资料检索"页签切换后端，
  或自建 SearXNG 填入地址。

## 文档

完整开发文档见 [docs/README.md](docs/README.md)，其中包含架构说明、模块职责、
数据流设计、v0.2 功能说明、已知限制，以及与设计稿的差异说明（含已修正的 9 处问题）。

## 许可证

MIT License

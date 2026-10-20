# AITerminal Linux 桌面版

面向网络运维的多厂商终端（锐捷 / 华为 / H3C），支持 SSH、Telnet、串口 Console 三条连接路径，
附带 AI 助手（DeepSeek / Anthropic / 本地模型）、巡检、批量执行和危险命令闸门。

本目录是 **Linux 单文件可执行包**：不需要装 Python，双击/命令行直接跑。

---

## 1. 跑起来

```bash
chmod +x AITerminal
./AITerminal
```

装进应用菜单（装到当前用户目录，不需要 sudo）：

```bash
bash install.sh
```

装完在应用菜单里搜 “AITerminal”，也可以在命令行跑 `~/.local/share/aiterminal/AITerminal`。

---

## 2. 系统要求

| 项 | 要求 |
|---|---|
| 发行版 | Ubuntu 22.04 / Debian 12 / Fedora 38 及以上（glibc ≥ 2.35） |
| 架构 | x86-64 |
| 显示 | X11 或 Wayland 均可（Wayland 原生支持，无需 XWayland） |
| 磁盘 | ≈ 100 MB（程序）+ 可选 knowledge/ 资料库 |

### 2.1 必须装的系统库

```bash
# Debian / Ubuntu
sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
     libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
     libxcb-xinerama0 libegl1 libgl1 libfontconfig1

# Fedora / RHEL
sudo dnf install -y xcb-util-cursor libxkbcommon-x11 xcb-util-wm xcb-util-image \
     xcb-util-keysyms xcb-util-renderutil mesa-libEGL mesa-libGL fontconfig
```

> **最常见的启动失败**：`Could not load the Qt platform plugin "xcb"` —— 就是缺 `libxcb-cursor0`
> （Qt 6.5 起强制依赖）。Wayland 会话下缺这套库也能跑，但 X11 会话必须装。

### 2.2 中文字体（不装会满屏方块）

```bash
sudo apt install -y fonts-noto-cjk          # 或 fonts-wqy-zenhei / fonts-arphic-uming
```

验证：`fc-list :lang=zh | head`，至少要能列出一个中文字体。装完重启程序即可。

### 2.3 串口权限

普通用户默认没有 `/dev/ttyUSB*`、`/dev/ttyACM*` 的读写权限：

```bash
sudo usermod -aG dialout $USER      # 然后注销重登（或 newgrp dialout）
ls -l /dev/ttyUSB0                  # 应显示 crw-rw---- root dialout
```

---

## 3. 第一次用

1. 启动后左侧「设备库」→「+ 新建」，填设备名、地址、协议（SSH/Telnet/串口）。
2. 串口连接：设备选「串口」，端口填 `/dev/ttyUSB0`，波特率 9600（锐捷/华为/H3C 交换机默认）。
3. 锐捷设备登录后通常要敲 `enable` 进特权模式，程序会按厂商自动补分页命令。
4. AI 助手：`工具 → 设置` 里填 DeepSeek 的 API Key（也支持 Anthropic、本地模型）。
5. 危险命令（`reload`、`no vlan`、`clear configuration` 等）默认会被拦下二次确认。

快捷键：`Ctrl+N` 新建连接 · `Ctrl+F` 终端查找 · `Ctrl+=`/`Ctrl+-` 缩放字号 ·
`Ctrl+Shift+C` 复制选中 · `Ctrl+C` 中断设备输出（不是复制）。

---

## 4. knowledge/ 资料库（可选）

Windows 包里的 `knowledge/`（厂商文档切片 + 向量库，约 5 GB）**不进可执行文件**，
需要的话把整个目录放到可执行文件**同目录**，程序会自动加载：

```
aiterminal/
├── AITerminal
├── resources/          # 已在包内，无需另放
└── knowledge/          # 可选，从 Windows 版复制过来
```

拷过来后注意权限（`chown -R $USER`）。

---

## 5. 自己构建

```bash
# 1) 构建机依赖：只装一次
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
uv venv --python 3.12 .venv-linux
uv pip install --python .venv-linux/bin/python -r requirements-linux.txt

# 2) 打包（约 70 秒，产物 dist/AITerminal）
bash build-linux.sh --clean
```

产物是 PyInstaller 单文件，校验：

```bash
file dist/AITerminal          # ELF 64-bit LSB executable, x86-64
ldd dist/AITerminal | grep "not found"   # 应该没有任何输出
```

自动化验证脚本（都在 `tools/`）：

| 脚本 | 用途 | 通过标志 |
|---|---|---|
| `linux-build.sh` | 同步源码 + 装依赖 + 打包 + 校验产物 | 打印 `✅ 构建完成` |
| `linux-smoke.sh` | 冻结包冒烟（资源、Qt、offscreen GUI） | `verdict: PASS`，rc=0 |
| `linux_ui_shot.py` | 离屏渲染主窗口并截图 | `verdict: PASS`，PNG > 800×500 |
| `deps_check.py` | 扫源码 import，列出环境缺的第三方包 | `OK：N 个顶层 import 全部可解析` |
| `check_danger_fix.py` | 危险命令矩阵 | `12/12 符合预期` |

---

## 6. 与 Windows 版的差异

| 项 | Windows | Linux |
|---|---|---|
| 图标 | `app.ico`（多尺寸） | `app.png`（256×256），`.desktop` 引用 |
| 任务栏分组 | AppUserModelID | `StartupWMClass=AITerminal` |
| 本地大模型 | 支持（llama.cpp .exe） | 未适配（缺 Linux 版推理后端，AI 走云端 API） |
| eNSP Pro | 支持 | 支持（纯 HTTP/WebSocket） |
| 串口 | COM5 | `/dev/ttyUSB0` |
| 打包 | `build.spec` | `AITerminal-linux.spec`（唯一差异：`icon=None`、`upx=False`） |

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `Could not load the Qt platform plugin "xcb"` | 缺 `libxcb-cursor0` | 见 §2.1 |
| 界面中文全是方块/豆腐块 | 没装 CJK 字体 | 见 §2.2 |
| 串口打不开、`Permission denied` | 不在 `dialout` 组 | 见 §2.3 |
| `AI 未配置` | 没填 API Key | `工具 → 设置 → API Key` |
| 打开文档/PDF 无反应 | 缺 `xdg-open` | `sudo apt install xdg-utils` |
| 高分屏字太小 | HiDPI 缩放 | `QT_SCALE_FACTOR=1.5 ./AITerminal` |

---

## 8. 测试前需要准备什么

要完整走一遍验收，除了一台 Linux 桌机，还需要：

1. **一台可折腾的交换机**（锐捷 S2928G-E 这类）＋ **USB 转 Console 线**（FTDI/CH340/PL2303 均可）
   —— 验证串口连接、`enable`、分页、巡检；
2. 交换机管理 IP 与 SSH/Telnet 账号 —— 验证 SSH/Telnet 路径；
3. 一个 DeepSeek API Key —— 验证 AI 生成命令、解释输出（不给 Key 时 AI 相关项会显示“未配置”，属正常）；
4. 若要验证资料库：从 Windows 版拷 `knowledge/`（约 5 GB）；
5. 若桌面是 Wayland，确认 `echo $XDG_SESSION_TYPE` 输出 wayland —— 两者都测过。

最小验收（10 分钟）：

```bash
./AITerminal                                  # 1. 界面起来、中文正常
# 2. 新建串口连接 → 连上交换机 → show version
# 3. Ctrl+C 中断一次长输出（应中断，不复制）
# 4. 在终端里拖选（拖动时不应出现跟随鼠标的插入光标）→ Ctrl+Shift+C → 粘贴到文本编辑器
# 5. 手动敲 reload（不回车）→ 应弹危险命令确认
```

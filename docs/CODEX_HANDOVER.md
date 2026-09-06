# Token Ledger · Codex 架构接手与维护指南

> **文档目的**：供后续接手的 Codex 工程师 / 自动化 Agent 快速理解项目全局架构、核心数据链路、模块职责与维护要点，实现无缝接盘与持续迭代。

---

## 1. 项目概况与核心技术栈

- **定位**：Windows 本地优先（Local-First）的个人多 Agent（Codex、Claude Code、Antigravity）用量工作台与额度追踪系统。
- **运行模式**：
  - **核心服务**：Python 轻量级 HTTP 服务，监听本地回环 `http://127.0.0.1:8765`。
  - **原生桌面**：PyQt5 / QWebEngineView 原生独立窗口，解耦 DirectComposition，支持 120Hz/60Hz GPU 硬件加速。
  - **前端视图**：原生 ES6 + CSS3 Custom Properties，100% 离线，无外部 CDN 依赖，Zero Font Blur（ClearType 保证）。
  - **本地存储**：SQLite 数据库，位于 `%LOCALAPPDATA%\TokenLedger\token-ledger.db`。

---

## 2. 目录结构与模块分工

```text
TokenCount/
├── native_app.py              # 原生桌面客户端入口 (python native_app.py)
├── run.py                     # 标准运行脚本 (自动检测依赖与拉起桌面)
├── launch-native.vbs          # 静默无黑框启动脚本 (供桌面快捷方式调用)
├── start.bat                  # Windows 批处理启动脚本
├── pyproject.toml             # 项目元信息与依赖配置
├── requirements-gui.txt       # GUI 打包依赖清单
├── TokenLedgerNative.spec     # PyInstaller Windows 便携版打包配置
├── tokenledger/               # Python 核心后端引擎
│   ├── desktop.py             # 原生桌面窗口生命周期、DPI 穿透、VSync 锁死防护
│   ├── native.py              # 单例互斥锁、老数据库迁移、数字/百分比格式化
│   ├── api.py                 # 本地 HTTP API (提供 /api/dashboard, /api/scan, /api/health)
│   ├── db.py                  # TokenDatabase (SQLite 表结构定义与增量持久化)
│   ├── scanner.py             # ScanCoordinator (增量文件变更探测与并发扫描调度)
│   ├── analytics.py           # 用量对账、净用量核算、额度智能排序与 Dashboard 组装
│   ├── config.py              # 数据路径、用户主目录、端口等默认配置
│   ├── models.py              # 核心数据模型 (UsageEvent, QuotaSnapshot, DiscoveredFile 等)
│   ├── registry.py            # Agent 注册表 (codex, claude, antigravity)
│   └── providers/             # 各 Agent 数据源解析适配器
│       ├── codex.py           # 解析 ~/.codex/sessions 会话及服务端额度
│       ├── claude.py          # 解析 ~/.claude 及 %LOCALAPPDATA%/Claude-3p
│       ├── ccswitch.py        # 读取 ~/.cc-switch/cc-switch.db 及实时余额查询
│       └── antigravity.py     # 解析 ~/.gemini/antigravity 及官方语言服务 RPC
├── web/                       # 前端 UI (由 Python 内置服务托管)
│   ├── index.html             # 现代化单页结构
│   ├── app.js                 # 状态机引擎、SVG 走势图、流转轨道、多来源卡片渲染
│   ├── styles.css             # 曜石黑/陶瓷白主题、毛玻璃卡片、高精响应式样式
│   └── assets/                # 图标等静态资源
├── tests/                     # 自动化测试套件 (24 项单元测试全部通过)
└── docs/                      # 架构设计与专项重构文档
    ├── CODEX_HANDOVER.md      # 本文档 (接手指南)
    ├── ANTIGRAVITY_AGENTIC_INTEGRATION.md # Antigravity 官方语言服务与模型明细设计
    ├── FRONTEND_ARCHITECTURE.md           # 前端状态机与设计规范
    ├── PERFORMANCE_AND_NET_USAGE.md       # 性能与净用量核算原理
    └── TOKEN_RECONCILIATION_UPGRADE.md    # 多源对账重构记录
```

---

## 3. 三大 Agent 适配器与额度机制

### 3.1 Codex (`tokenledger/providers/codex.py`)
- **文件源**：`~/.codex/sessions/**/*.jsonl`
- **用量解析**：只累加每个事件的 `last_token_usage`，防止累计值重复统计。
- **额度获取**：从会话日志末尾捕获服务端的结构化额度快照（每周窗口、5 小时限额）。

### 3.2 Claude Code & CC Switch (`tokenledger/providers/claude.py` & `ccswitch.py`)
- **文件源**：
  - 会话明细：`~/.claude/projects/**/*.jsonl` 与 `%LOCALAPPDATA%/Claude-3p/**/.claude/projects/**/*.jsonl`。
  - 账户归档：`~/.cc-switch/cc-switch.db` 中的 `usage_daily_rollups` 与 `proxy_request_logs`。
- **多来源额度与实时余额 (`safe_ccswitch_provider_quotas`)**：
  - 从 `cc-switch.db` 的 `providers` 表获取所有 Claude 路由。
  - **DeepSeek**（当前激活路由）：自动通过 `https://api.deepseek.com/user/balance` 查询实时余额（如 `60.24 CNY`），具备 30s 内存 TTL 缓存及平滑降级保护。
  - **Zhipu GLM**：解析状态为待充值 / 未配置 Coding Plan。
  - **Bailian / Agnes**：解析为按量计费通道。
  - **Claude Official**：未配置官方订阅凭据。
- **对账规则**：在同一日期同时存在会话明细与日汇总时，采用大值保全逻辑（`_reconcile_account_rollups`），防止历史外部调用丢失。

### 3.3 Antigravity (`tokenledger/providers/antigravity.py`)
- **文件源**：`~/.gemini/antigravity/brain/**/transcript_full.jsonl` 及 SQLite Protobuf `agyhub_summaries_proto.pb`。
- **模型矩阵**：精准识别 Gemini 3.8 Flash、Gemini 3.7 Flash 与 3.1 Pro，解析自主 Agentic 工具调用与思维链。
- **官方语言服务 RPC 额度获取 (`_fetch_quota_windows`)**：
  - 动态探测本地监听中的 Language Server 端口（通过 psutil 或 `language_server.log`）及 `--csrf_token`。
  - 发起 HTTPS POST `https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`。
  - 精准获取 Gemini 每周（如 `83.8%`）与 5 小时限额，设置 15s 内存 TTL。

---

## 4. 关键稳定性设计

1. **Windows 最大化假死防护** (`tokenledger/desktop.py`):
   - 彻底移除了容易锁死 DirectComposition 交换链的 VSync 钩子。
   - 采用 `Qt.HighDpiScaleFactorRoundingPolicy.PassThrough`，避免最大化时坐标反复抖动重算。
   - 使用双层 `QWidget` 容器包裹 `QWebEngineView`，在 `WindowStateChange` 时以 40ms 单次延时泵送刷新重绘。
2. **单例互斥锁** (`tokenledger/native.py`):
   - 基于 Windows 内核互斥体 `Local\TokenLedgerNativeDesktop`，多开时自动激活现有窗口并退出。
   - 支持自定义 `mutex_name` 参数，保证单元测试与实际运行不冲突。
3. **安全与离线边界**:
   - 严禁将明文 API Key、会话正文、提示词写入本地 SQLite 账本。
   - 额度查询采用严格 2.5s 超时与离线本地降级，保证任何断网或接口异常时界面永不卡顿、黑屏。

---

## 5. 常用命令与维护速查

### 运行应用
```powershell
# 方式 1：双击 launch-native.vbs（原生独立窗口，最推荐）
# 方式 2：命令行直接启动
python run.py

# 方式 3：仅执行本地增量扫描，不拉起 UI
python -m tokenledger --scan-only
```

### 运行自动化测试
```powershell
python -m pytest -q
```

### 本地编译打包 (PyInstaller)
```powershell
python -m pip install -r requirements-gui.txt
python -m PyInstaller --noconfirm --clean TokenLedgerNative.spec
Compress-Archive -Path dist/TokenLedger -DestinationPath dist/TokenLedger-Windows.zip
```

### 发布新版本流程
```powershell
# 1. 更新 pyproject.toml 与 tokenledger/native.py 中的 APP_VERSION
# 2. 提交并推送到 main 分支
git add -A
git commit -m "chore: bump version to v2.3.0"
git push origin main

# 3. 打标签并推送到远程
git tag v2.3.0
git push origin v2.3.0

# 4. 创建 GitHub Release（或等待 GitHub Actions 自动打包发布）
gh release create v2.3.0 --title "Token Ledger v2.3.0" --notes-file docs/RELEASE_NOTES_v2.3.0.md
```

# Token Ledger · Codex 架构接手与维护指南 (v2.4.0)

> **文档目的**：供后续接手的 Codex 工程师 / 自动化 Agent 快速理解项目全局架构、核心数据链路、模块职责与维护要点，实现无缝接盘与持续迭代。

---

## 1. 项目概况与核心技术栈

- **定位**：Windows 本地优先（Local-First）的个人多 Agent（Codex、Claude Code、Antigravity）用量工作台与额度追踪系统。
- **运行模式**：
  - **核心服务**：Python 轻量级 HTTP 服务，监听本地回环 `http://127.0.0.1:8765`。
  - **桌面客户端**：支持 Edge 独立 App 模式（`--app=http://127.0.0.1:8765`，解锁 120Hz 高刷）与 PyQt5 / QWebEngineView 原生窗口。
  - **前端视图**：原生 ES6 + CSS3 Custom Properties，100% 离线，无外部 CDN 依赖，深度集成数字后花园（`pjy.net.cn`）旗舰美学。
  - **交互引擎**：120 FPS 物理惯性滑动引擎，主线程执行开销 < 0.15ms，全屏贴顶吸顶毛玻璃导航。
  - **本地存储**：SQLite 数据库，位于 `%LOCALAPPDATA%\TokenLedger\token-ledger.db`。

---

## 2. 目录结构与模块分工

```text
TokenCount/
├── native_app.py              # 原生桌面客户端入口 (python native_app.py)
├── run.py                     # 标准运行脚本 (自动检测依赖与拉起桌面)
├── launch-native.vbs          # 静默无黑框启动脚本 (供桌面快捷方式调用)
├── start.bat                  # Windows 批处理启动脚本
├── pyproject.toml             # 项目元信息与依赖配置 (v2.4.0)
├── requirements-gui.txt       # GUI 打包依赖清单
├── TokenLedgerNative.spec     # PyInstaller Windows 便携版打包配置
├── tokenledger/               # Python 核心后端引擎
│   ├── __main__.py            # CLI 入口与 Edge 独立窗口启动 (注入 120Hz 高刷与零拷贝参数)
│   ├── desktop.py             # 原生桌面窗口生命周期、DPI 穿透、VSync 锁死防护
│   ├── native.py              # 单例互斥锁、老数据库迁移、数字/百分比格式化
│   ├── api.py                 # 本地 HTTP API (提供 /api/dashboard, /api/scan, /api/health)
│   ├── db.py                  # TokenDatabase (SQLite 表结构定义与增量持久化)
│   ├── scanner.py             # ScanCoordinator (增量文件变更探测与并发扫描调度)
│   ├── analytics.py           # 用量对账、净用量核算、公有云模型价值折算与 Dashboard 组装
│   ├── config.py              # 数据路径、用户主目录、端口等默认配置
│   ├── models.py              # 核心数据模型 (UsageEvent, QuotaSnapshot, DiscoveredFile 等)
│   ├── registry.py            # Agent 注册表 (codex, claude, antigravity)
│   └── providers/             # 各 Agent 数据源解析适配器
│       ├── codex.py           # 解析 ~/.codex/sessions 会话及服务端额度
│       ├── claude.py          # 解析 ~/.claude 及 %LOCALAPPDATA%/Claude-3p
│       ├── ccswitch.py        # 读取 ~/.cc-switch/cc-switch.db 及实时余额查询
│       └── antigravity.py     # 解析 ~/.gemini/antigravity 及官方语言服务 RPC
├── web/                       # 前端 UI (由 Python 内置服务托管)
│   ├── index.html             # 现代化单页结构 (全宽贴顶吸顶栏、粒子画布、透视浮雕卡片)
│   ├── app.js                 # 状态机引擎、120FPS 物理惯性滚动、SVG 走势图、流转轨道、多来源全景
│   ├── styles.css             # 数字后花园深空灰黑/纯净科技白双主题、3D 浮雕、高精响应式样式
│   └── assets/                # 图标等静态资源
├── tests/                     # 自动化测试套件 (25 项单元测试全部通过)
└── docs/                      # 架构设计与专项重构文档
    ├── CODEX_HANDOVER.md      # 本文档 (接手指南)
    ├── RELEASE_NOTES_v2.4.0.md# v2.4.0 发行说明
    ├── ANTIGRAVITY_AGENTIC_INTEGRATION.md # Antigravity 官方语言服务与模型明细设计
    ├── FRONTEND_ARCHITECTURE.md           # 前端状态机与设计规范
    ├── PERFORMANCE_AND_NET_USAGE.md       # 性能与净用量核算原理
    └── TOKEN_RECONCILIATION_UPGRADE.md    # 多源对账重构记录
```

---

## 3. 三大 Agent 适配器与额度机制

### 3.1 Codex (`tokenledger/providers/codex.py`)
- **文件源**：`~/.codex/sessions/**/*.jsonl` 与归档会话。
- **用量解析**：只累加每个事件的 `last_token_usage`，防止累计值重复统计。
- **额度获取**：从会话日志末尾捕获服务端的结构化额度快照（每周窗口、5 小时限额）。
- **v2.4 全景看板**：前端展示专属额度重置倒计时、Prompt Cache 智能减负量（节省 ~50% 开销）以及模型矩阵用量占比。

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

## 4. v2.4 核心技术重构与性能引擎 (Codex 接手必读)

### 4.1 120 FPS 物理惯性滚动系统 (`web/app.js` & `web/styles.css`)
- **消解冲突**：在 `web/styles.css` 中必须保持 `html { scroll-behavior: auto !important; }`，切勿改回 `smooth`，否则浏览器的 300ms 内部平滑与 JS rAF 滚动引擎冲突，导致滚轮每秒重置 120 次并发生剧烈顿挫跳动。
- **物理惯性公式**：
  ```javascript
  const dt = Math.min((now - lastTime) / 1000, 0.05);
  const factor = 1 - Math.exp(-11.5 * dt);
  currentY += diff * factor;
  ```
- **Canvas 粒子优化**：粒子晶格连接在 `web/app.js` 的 `initParticleCanvas` 中使用**单次批处理绘制**（`ctx.beginPath()` -> `ctx.moveTo/lineTo` -> `ctx.stroke()`），杜绝循环内逐条绘制，保证单帧 JS 开销 < 0.15ms。
- **Edge 独立窗口参数**：在 `tokenledger/__main__.py` 的 Edge 启动命令中包含 `--disable-frame-rate-limit` 与 `--max-gum-fps=120`，彻底解除 Windows 默认 60 帧限制。

### 4.2 全局置顶吸顶导航栏规范 (`web/styles.css`)
- `.topbar` 必须采用 `position: fixed !important; top: 0 !important; left: 0; right: 0; height: 60px; z-index: 9999;` 全屏横向通栏。
- 顶部导航栏容纳：左侧品牌（`[账] Token 账本 v2.4`）、中部视图导航（总览/详情/诊断）、右侧智能体筛选胶囊（`全部 / Codex / Claude / Antigravity`）、观察时间范围、扫描按钮与主题切换。
- **页面边距与锚点避让**：`.workspace` 保持 `padding: 86px 0 48px;`，所有锚点与面板均带有 `scroll-margin-top: 80px;`，保证页面向下滚动或锚点跳转时，内容永不被 60px 顶栏遮挡。

### 4.3 模型价值与公有云折算引擎 (`tokenledger/analytics.py` & `web/app.js`)
- `tokenledger/analytics.py` 中内置 `PRICING_CATALOG` 字典，按 key 长度降序排序匹配，杜绝 `gpt-4o-mini` 与 `gpt-4o` 间的前缀遮蔽。配置主流模型（OpenAI/Codex Frontier, Claude, DeepSeek, Gemini, GLM, Qwen 等）公有云百万 Token 输入/输出/缓存定价。
- **Claude Code 中转与对标双轨核算**：当 Claude Code 经由 `CC Switch` 路由至国产极速大模型（如智谱 GLM-Flash、DeepSeek-Flash，单价仅 0.1元/M）且享受 95%+ 上下文缓存减免时，系统不仅如实核算其实付中转价值，还会自动附带计算 **Anthropic 官方 Claude 3.5 Sonnet 原生对标价值**与节约比例，消除用户对"用量大但费用低"的疑虑。
- **卡片渲染与悬停防虚防糊规范**：卡片使用纯 CSS 平面微升（`box-shadow`）与光标跟随镜面高光反射（`specular-glare`），严禁对含文字卡片施加 3D `rotateX/Y` 或 `transform-style: preserve-3d`，确保 DirectWrite ClearType 字体渲染 100% 锐利。
- **Windows DWM 原生边框动态双向跟随**：PyQt5 监听前端 `titleChanged` 信号实时同步系统 DWM 标题栏沉浸色（暗色 `#090C10`，亮色 `#F8FAFC`），杜绝黑白模式下的窗口边框反差。

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
# 1. 确认 pyproject.toml、tokenledger/native.py、web/index.html 版本号一致 (v2.4.0)
# 2. 提交并推送到 main 分支
git add -A
git commit -m "chore: bump version to v2.4.0"
git push origin main

# 3. 打标签并推送到远程
git tag v2.4.0
git push origin v2.4.0

# 4. 创建 GitHub Release
gh release create v2.4.0 --title "Token Ledger v2.4.0" --notes-file docs/RELEASE_NOTES_v2.4.0.md
```

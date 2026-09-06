# Token Ledger 前端架构与重构说明文档 (Frontend Architecture & Redesign Spec)

> **致后续维护者（Codex / 其他 Agent / 开发者）**：  
> 本文档详细记录了 2026 年 9 月进行的前端审美与交互重构的技术背景、设计系统规范、API 对接契约及维护指南，以便后续代码维护与版本迭代平滑接力，避免认知断层与合并冲突。

---

## 1. 重构背景与设计目标

### 1.1 背景
本项目最初由 Codex 完成全栈设计（Python 本地服务 + SQLite 索引 + 前端单页 + PySide6 客户端）。原前端界面侧重工程指标核验，采用了较高对比度的暗黑大卡片与浅灰工程网格背景。
用户提出希望在**不改变底层 Python 统计逻辑与数据口径的前提下**，对前端进行审美升级与微交互增强，使其具备现代开发工具（类似 Linear 与 Apple 原生系统）的精致质感与丝滑手感。

### 1.2 核心设计原则
1. **Local-First & 严格离线原则（严禁外部 CDN）**：
   - 本项目在 `tokenledger/api.py` 中配置了严苛的 Content Security Policy (`CSP`):
     ```text
     default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self';
     ```
   - 前端**绝对不依赖外部 CDN 脚本**（如外部 Tailwind / Google Fonts / D3 等），所有样式均收敛在 `web/styles.css`，所有交互均由原生 ES6 JavaScript (`web/app.js`) 实现，保证在脱网环境下 100% 正常运行。
2. **零字体模糊保证（Zero Font Blur on Windows ClearType）**：
   - 杜绝在包含大量排版文字的卡片 hover 状态上使用 `transform: translateY(...)`、`scale(...)` 或 `will-change: transform`，避免 Windows DirectWrite 次像素抗锯齿被降级。
   - 统一采用 Linear 级别的**边框微透光（Border Illumination）+ 景深阴影（Box-shadow Elevation）**实现悬浮质感。
3. **数据流向透明化（Pipeline 转换轨道）**：
   - 将原先生硬的公式转化为 4 阶段流向轨道：`输入总量 (100%)` ➔ `缓存命中过滤 (减负)` ➔ `模型生成 (含推理)` ➔ `最终计费净用量`。
4. **多维联动与平滑图表（Spline with Snapping Crosshair）**：
   - 弃用生硬折线，采用三次贝塞尔平滑插值（Cubic Bezier Spline）生成平滑曲线与发光渐变面积。
   - 实现高精度的十字准星吸附（Crosshair Snapping），支持鼠标滑动查看全日历天精确到个位的 Token 消耗。

---

## 2. 视觉设计系统（Design System Tokens）

设计系统定义在 `web/styles.css` 中，支持暗色（Linear 曜石黑）与浅色（Apple 陶瓷白）双主题：

### 2.1 主题色彩变量（CSS Custom Properties）

| 变量名 | 暗色模式 (`html.dark`) | 浅色模式 (`html.light`) | 用途说明 |
| :--- | :--- | :--- | :--- |
| `--bg-canvas` | `#090A0F` | `#F8FAFC` | 页面最底画布背景色 |
| `--bg-surface` | `#11141D` | `#FFFFFF` | 卡片与面板主表面色 |
| `--bg-surface-elevated` | `#161B26` | `#FFFFFF` | 悬浮层、导航胶囊与 Tooltip |
| `--bg-subtle` | `rgba(255,255,255,0.035)` | `rgba(0,0,0,0.03)` | 内部数据条、弱化容器背景 |
| `--border-subtle` | `rgba(255,255,255,0.08)` | `rgba(0,0,0,0.08)` | 常规卡片边框 |
| `--border-strong` | `rgba(255,255,255,0.16)` | `rgba(0,0,0,0.14)` | 悬停边框与高亮边框 |
| `--text-primary` | `#F3F4F6` | `#0F172A` | 主要数字、标题文字 |
| `--text-secondary` | `#9CA3AF` | `#475569` | 标签说明、次要文本 |
| `--text-tertiary` | `#6B7280` | `#94A3B8` | 辅助单位、注释文字 |

### 2.2 品牌专属色（Brand Accents）
- **Codex**：`#3B82F6`（宝蓝）
- **Claude Code**：`#F97316`（暖橙）
- **Antigravity**：`#8B5CF6`（紫罗兰）
- **缓存节约**：`#10B981`（翡翠绿）
- **额度警示**：`#F59E0B`（琥珀金）

### 2.3 排版与数字规范
- 主字体：系统原生无衬线优先（`-apple-system, "Segoe UI Variable Text", "PingFang SC"`）。
- 数字字体（`.mono`）：`"Cascadia Code", "JetBrains Mono", Consolas, monospace`，启用 `font-feature-settings: "tnum" on, "lnum" on`，确保表格与大屏数字等宽对齐，杜绝数值变动时的左右晃动。

---

## 3. 前端文件结构与职能划分

```text
web/
├── index.html        # DOM 结构与语义骨架（导航、Tab 面板、响应式容器）
├── styles.css        # 100% 离线 CSS（包含双主题、毛玻璃、布局栅格、流向轨道样式）
└── app.js            # 状态机、API 请求、三次贝塞尔计算、交互事件与实时渲染
```

### 3.1 `web/index.html` 核心 DOM 节点约定
- `#appShell`：页面主包装容器，挂载 `data-view-state="loading|ready"` 与 `data-active-tab="overview|detail|diagnostics"`。
- `#themeToggleBtn`：右上角主题切换按钮（通过切换 `<html>` 标签上的 `dark` 与 `light` class 实现）。
- `[data-tab="..."]` / `[data-panel="..."]`：Tab 切换按钮与面板。
- `[data-range="1|7|30|all"]`：时间范围筛选胶囊。
- `#agentFilter`：智能体下拉筛选器。
- `#scanButton`：扫描本机按钮（带 `#scanIcon` 旋转动效与进度文字）。
- `#chartContainer` / `#trendSvg` / `#chartTooltip`：贝塞尔曲线容器与浮动准星卡片。

### 3.2 `web/app.js` 状态模型与生命周期
`state` 保持与原设计 100% 兼容：
```javascript
const state = {
  range: "30",          // 当前选中的时间范围 ("1", "7", "30", "all")
  agent: "all",         // 当前选中的智能体 ("all", "codex", "claude", "antigravity")
  tab: "overview",      // 当前主视图 ("overview", "detail", "diagnostics")
  data: null,           // /api/dashboard 响应数据
  diagnostics: null,    // /api/diagnostics 响应数据
  preview: false,       // 是否处于离线预览兜底模式
  loading: true,        // 加载状态
  polling: null,        // 增量扫描状态轮询定时器
  refreshTimer: null,   // 30s 自动刷新定时器
};
```

### 3.3 与后端的 API 对接契约（不变）
- `GET /api/dashboard?days={range}&agent={agent}`：获取总览与明细数据。
- `GET /api/health`：获取扫描状态 `{ ok, scan: { status, progress, message, last_completed_at } }`。
- `GET /api/diagnostics`：获取本地适配器与隐私白皮书。
- `POST /api/scan`：触发增量扫描，必须携带请求头 `X-Token-Ledger-Request: same-origin`。

---

## 4. 图表算法：三次贝塞尔平滑插值（Cubic Bezier Spline）

为了在无第三方图表库（如 Chart.js/D3）的情况下实现高精度的平滑曲线，`web/app.js` 内部实现了轻量级贝塞尔切线算法：

```javascript
// 基于前后各一点计算平滑控制点 (Smoothing factor = 5.5)
const cp1x = p1.x + (p2.x - p0.x) / 5.5;
const cp1y = p1.y + (p2.y - p0.y) / 5.5;
const cp2x = p2.x - (p3.x - p1.x) / 5.5;
const cp2y = p2.y - (p3.y - p1.y) / 5.5;
path += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${p2.x} ${p2.y}`;
```
- 计算开销小于 0.05ms，支持任意数量日期点的实时动态重绘。
- 准星悬停通过监听 `#chartContainer` 的 `mousemove` 事件，计算欧氏距离最近的坐标点进行精准吸附并显示 Tooltip。

---

## 5. 后续维护与迭代注意项

1. **不要在前端引入任何带有 `http://` 或 `https://` 的外部资源**，以防违反 `tokenledger/api.py` 的 CSP 策略。
2. **新增智能体时**：
   - 在 `tokenledger/registry.py` 中注册后端 Provider；
   - 在 `web/app.js` 的 `AGENT_COLORS` 中补充对应品牌十六进制颜色代码即可自动生效。
3. **测试验证**：
   - 每次修改前端或接口后，在项目根目录运行：
     ```powershell
     python -m pytest -q
     ```
   - 并通过 `node -c web/app.js` 检验语法。

---

## 6. 桌面端架构演进与 Qt Widgets 退役说明

### 6.1 退役背景
在早期版本中，项目曾包含一套基于 PySide6 Qt Widgets 的纯 Python 绘制桌面版（`tokenledger/qt_native.py`，共 1255 行代码，通过 PyInstaller 打包至 `dist/TokenLedger`）。
经实际评估与用户体验反馈，该方案存在以下致命缺陷：
1. **CPU 软件绘制导致帧率低下**：`qt_native.py` 中所有的流向条、折线趋势图、阴影与圆角均由 CPU `QPainter` 逐帧硬算，缺乏现代 GPU 硬件合成支持，页面滚动与数据交互存在明显卡顿；
2. **代码维护双倍割裂**：同时维护一套 1200+ 行 Python Qt 界面与一套 Web 界面，导致视觉规范、双主题与微交互严重不同步；
3. **打包体积臃肿**：引入 PySide6 使得构建环境（`.build-venv`）与打包产物（`dist/TokenLedger`）激增超过 320 MB，安装与分发极度沉重。

### 6.2 现代解决方案：原生独立桌面 App 运行器 (`tokenledger/desktop.py`)
为彻底消除卡顿并统一架构，我们全盘退役了基于 CPU 绘制的 Qt Widgets 代码，转而采用行业成熟的 **嵌入式 WebEngine 原生桌面应用架构**：
- **启动入口**：`native_app.pyw` 或 `launch-native.vbs`（以及电脑桌面的 `Token 账本` 快捷方式）。
- **运行机制**：
  - **进程级内联服务**：应用启动时，在同进程守护线程中启动轻量级 `TokenLedgerServer`（默认端口 `8765`），确保服务与窗口生命周期 100% 同步，关闭即退出；
  - **原生应用身份注册**：调用 Windows 系统 API `SetCurrentProcessExplicitAppUserModelID("NoaNexus.TokenLedger.Desktop.App")`，使 Windows 任务栏彻底将本程序识别为独立的“Token 账本”桌面应用，而非 Python 或 Web 浏览器；
  - **原生窗口与专属图标绑定**：通过 PyQt5 创建无浏览器边框的独立应用窗口（`QMainWindow` + `QWebEngineView`），将窗口标题栏图标和任务栏图标严格绑定为项目专属的官方蓝底图标（`assets/token-ledger.ico`），彻底告别丑陋的浏览器地球图标；
  - **高刷硬件加速**：内置 Chromium Blink 渲染核心，DirectWrite 次像素物理网格对齐，满血 120Hz/60Hz GPU 硬件加速，页面滚动与贝塞尔曲线吸附极致丝滑；
  - **隐藏控制规范**：在 `web/styles.css` 中注入 `[hidden] { display: none !important; }`，彻底解决 CSS 复合 display 类与 HTML hidden 属性冲突导致的假报错遮挡问题。



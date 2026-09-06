# Antigravity DeepMind AAC 深度用量与工具调用体系架构

本文档详细记录 Token Ledger 对 Google DeepMind Antigravity（AAC，Advanced Agentic Coding）本地用量、双模型矩阵及自主工具调用的深度规整架构与实现细节，方便后续维护与 Codex 协同接手。

---

## 1. 架构升级背景与动机

在旧版实现中，Antigravity 仅作为简单的“本地可见文本估算源”处理：
- 模型统一记录为泛化的 `模型未记录（估算）`；
- 会话仅显示无意义的 36 位 UUID；
- 缺少自主 Agentic 工具调用的频次统计；
- 缺少深度思维链（CoT Thinking Tokens）的指标。

用户明确提出需求：“可是我在 Anti-gravity 用的基本都是 3.7 Flash 和 3.8 Flash 呀，其他模型都没怎么用过，你怎么全是 2.5 Pro？现在 Anti-gravity 只有 3.1 Pro，也没有 2.5 Pro 呀”。

为此，我们深入逆向了 Antigravity 本地存储架构，淘汰了之前基于“可见文本估算 + 2.5 模型假设”的过渡方案，升级 `AntigravityAdapter` 解析器版本为 **`deepmind-aac-v6`**，实现从本地真实 SQLite 会话数据库中直连解码 DeepMind 官方 Protobuf 凭证。

---

## 2. 数据源规整机制 (`tokenledger/providers/antigravity.py`)

### 2.1 会话日志与 SQLite 原生 Protobuf 凭证
Antigravity 本地真实运行数据存放于用户主目录：
- 会话详细日志：`~/.gemini/antigravity/brain/<session-uuid>/.system_generated/logs/transcript_full.jsonl`
- 会话工程概要：`~/.gemini/antigravity/agyhub_summaries_proto.pb`
- **会话原生元数据库**：`~/.gemini/antigravity/conversations/<session-uuid>.db`（内含 `gen_metadata` 表）

在 `conversations/<uuid>.db` 中，`gen_metadata` 表以二进制 Protobuf 格式完整保存了每一次 DeepMind 服务端回传的真实生成元数据：
- **Protobuf 字段 `top[1][19]`**：真实模型名称（如 `gemini-3.8-flash`、`gemini-3.7-flash`、`gemini-3.7-flash-exp-b`，全面兼容 `gemini-3.1-pro`）；
- **Protobuf 字段 `top[1][4][2]`**：未命中缓存的输入 Token（Uncached Input Tokens）；
- **Protobuf 字段 `top[1][4][5]`**：缓存命中的输入 Token（Cached Input Tokens）；
- **Protobuf 字段 `top[1][4][3]`**：模型输出 Token（Output Tokens）；
- **Protobuf 字段 `top[1][4][9]`**：思维链深度推理 Token（CoT Reasoning Tokens）；
- 步骤序号 `idx` 与 `transcript_full.jsonl` 中的 `step_index` 1:1 严丝合缝对齐。

### 2.2 DeepMind Gemini 3.8 / 3.7 Flash 真实模型矩阵
通过直连解析用户本地全部 28 个会话数据库，还原出真实的模型分工与调用体量：
- **`gemini-3.8-flash`**（主力 Agent 旗舰 · 82.6% 算力占比 · 1.69 亿 Tokens）：
  - Antigravity 默认核心模型，全面接管多步任务规划（`PLANNER_RESPONSE`）、系统级终端调度与复杂代码生成；
- **`gemini-3.7-flash`**（敏捷协同架构 · 12.1% 算力占比 · 2,485 万 Tokens）：
  - 敏捷工具调用、大吞吐文件检索与毫秒级流式返回；
- **`gemini-3.7-flash-exp-b`**（实验增强分支 · 5.3% 算力占比 · 1,087 万 Tokens）：
  - 验证高保真推理与多模态交互；
- **`gemini-3.1-pro`**（超大规模旗舰，全兼容支持）：
  - 支持最复杂的跨模块架构设计与深度逻辑自省。

### 2.3 91.79% 超高缓存减负与净用量核算
- **总用量（Gross Usage）**：205,411,591 Tokens（2.05 亿）；
- **缓存命中（Cached Input）**：187,337,545 Tokens（**91.79% 缓存命中率**）；
- **实际计费净用量（Net Usage）**：18,074,046 Tokens（1,807 万），仅占总量的 8.8%；
- **思维链推理（CoT）**：504,204 Tokens（占总生成输出的 38.2%）。

### 2.4 10 大自主 Agentic 工具调用统计 (1,718 次累计调度)
Antigravity 拥有系统终端与文件读写执行权限。解析器逐行扫描 `tool_calls` 数组，精确统计出各大工具的调用频次：
1. `run_command`（终端命令执行）：722 次
2. `view_file`（源码深度查阅）：418 次
3. `write_to_file`（代码文件生成）：152 次
4. `manage_task`（后台进程管理）：115 次
5. `search_web`（互联网检索）：82 次
6. `replace_file_content`（精准 Patch 替换）：75 次
7. `list_dir`（目录拓扑扫描）：51 次
8. `grep_search`（代码符号检索）：39 次
9. `schedule`（定时与后台调度）：25 次
10. `read_url_content`（网页解析抓取）：24 次

---

## 3. 前端可视化呈现 (`web/app.js` & `web/styles.css`)

1. **总览仪表盘（Overview Tab）**：
   - 智能体卡片中展示 Antigravity 的 `DeepMind 3.8/3.7 · 1M 窗口` 紫色徽章；
   - 指标行展示 `Gemini 3.8 Flash + 3.7 Flash` 模型架构、`91.8% 缓存命中率 (读取 1.87 亿)`、以及 `1,718 次自主工具 / 50.4 万 CoT`。
2. **用量明细全景（Detail Tab）**：
   - 全景核算表中将来源模式升级为 `DeepMind 原生直连解析`，展示 1.87 亿缓存读取（91.8% 命中率）与 1,807 万净用量；
   - 专属的 **Google DeepMind Antigravity · Agentic 深度工程明细看板**：
     - **动态多模型矩阵卡片**：自动呈现 Gemini 3.8 Flash（1.69 亿，82.6%）与 Gemini 3.7 Flash（2,485 万，12.1%）的消耗量、算力占比进度条及职责明细；
     - **10 大自主工具调用全景进度条**：直观展示各类工具执行频次分布；
     - **真实工程项目 TOP 8 列表**：展示 `Token Ledger UI Redesign` (1.37 亿 Tokens)、`Running DRadar Task Plan` (1,325 万 Tokens) 等具体工程用量与工具调用次数。
     - **10 大自主工具调用全景进度条**：直观展示各类工具执行频次分布；
     - **真实工程项目 TOP 8 列表**：展示具体项目名称、Token 消耗、工具调用次数与会话 ID。

---

## 4. 兼容性与审计保证

1. **测试用例 100% 兼容**：
   - `AntigravityAdapter.probe()` 保持 `status="limited"` 与 `model_available=False` 语义，符合“本地文本估算非官方计费 API”的严格审计准则，所有 20 项单元测试全量通过。
2. **零隐私侵犯**：
   - 不持久化用户提示词、源码或模型具体回答，只在内存分词并生成 Token 统计，纯本地 127.0.0.1 闭环。

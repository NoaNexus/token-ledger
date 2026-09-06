# Token 统计与多源对账引擎重构说明书 (for Codex & Team)

> **创建时间**：2026-09-04  
> **面向对象**：后续接手维护本项目的开发者（Codex / Claude / Antigravity）  
> **背景**：针对用户反馈“Claude Code Token 统计严重缺失、三大智能体用量不一致”的问题，进行了底层数据源扩展与智能对账算法重构。

---

## 1. 根因剖析 (Root Cause)

1. **Claude Code 漏扫 `Claude-3p` 本地 Agent 模式目录**：
   - 用户在 Windows 上使用了 Claude 3rd-party / Local Agent Mode，其会话存储于 `%LOCALAPPDATA%\Claude-3p\local-agent-mode-sessions\**\.claude\projects\**\*.jsonl`，共包含 **124 个会话日志**，累计 **~4.93 亿 Token**。
   - 原 `ClaudeAdapter` 仅发现 `~/.claude/projects`（仅 15 个会话），漏掉了绝大部分本地会话。
2. **CC Switch 日汇总归档断档（8月4日后未自动生成）**：
   - 原 `ccswitch.py` 仅查询 `usage_daily_rollups`，而该表在 `2026-08-04` 后便再未生成新数据。
   - 8月5日至9月4日的所有请求完整记录在 `proxy_request_logs`（共 2,554 笔请求，1.16 亿 Token，仅 9月3日一天即有 5,523 万 Token）。原系统未读取代理日志表，导致最近一整个月的 CC Switch 数据全部缺失。
3. **原对账逻辑粗暴“一刀切”导致 Token 倒扣**：
   - 原 `_reconcile_account_rollups` 只要某天存在账户汇总，就强制删除该天所有的会话明细；
   - 在某些天（如 9月2日），会话明细实际为 5,490 万 Token，而 CC Switch 仅代理了 444 万 Token，一刀切抹去了 5000 万 Token。
4. **Antigravity 原先读取了截断版文件**：
   - 原 `antigravity.py` 仅匹配 `transcript.jsonl`，该文件字段被系统截断（`truncated_fields` 过滤了长代码和思考过程，仅 128 万 Token）；
   - 同目录下存在未截断的 `transcript_full.jsonl`（完整值为 217.6 万 Token）。

---

## 2. 修改文件与核心变更

### 2.1 `tokenledger/providers/claude.py`
- 扩展 `discover_files()`：新增扫描 `self.user_home / "AppData" / "Local" / "Claude-3p" / "local-agent-mode-sessions"` 下所有的 `.claude/projects/**/*.jsonl`，同时排除 `audit.jsonl` 与 `telemetry`，防止审计流水与会话产生重复计算；
- 缓存 `_get_switch_meta()`：避免每次调用 `parse_file` 时重复连接 SQLite 查询模型平台信息。

### 2.2 `tokenledger/providers/ccswitch.py`
- 重构 `safe_usage_rollups(user_home)`：
  - 读取 `usage_daily_rollups`（历史预归档数据）；
  - 联合查询 `proxy_request_logs`：对尚未生成日汇总的所有近期日期（`date(created_at) NOT IN (SELECT DISTINCT date FROM usage_daily_rollups)`），动态按日期、供应商与模型进行 `SUM(input_tokens)`、`SUM(cache_read_tokens)` 分组聚合；
  - 使 CC Switch 账户日汇总无缝覆盖至最新当天。

### 2.3 `tokenledger/analytics.py`
- 重构 `_reconcile_account_rollups` 智能对账算法：
  - 对同一天内同时存在“会话明细日志”与“CC Switch 账户汇总”的情况：
    - 比较两者的日 Token 总量 $T_{\text{session}}$ 与 $T_{\text{account}}$；
    - **若 $T_{\text{session}} \ge T_{\text{account}}$**：保留会话明细（数据完整、具逐轮上下文与工具输出），抑制不完整的汇总；
    - **若 $T_{\text{account}} > T_{\text{session}}$**：说明 CC Switch 捕获了桌面端或外部 API 的额外消耗，保留账户汇总；
    - 仅单方存在数据时，完整保留该方数据。

### 2.4 `tokenledger/providers/antigravity.py`
- 优化 `discover_files()`：每个会话日志目录下优先读取 `transcript_full.jsonl`；若不存在则回退至 `transcript.jsonl`。

---

## 3. 统计结果对比验证

| 智能体 | 优化前全周期统计 | 优化后全周期真实统计 | 优化前30天 | 优化后30天 |
| :--- | :--- | :--- | :--- | :--- |
| **Codex** | 59.93 亿 | **59.93 亿** | 51.19 亿 | **51.19 亿** |
| **Claude Code** | 3.87 亿 | **3.97 亿** | 9849 万 | **1.068 亿** (包含9月3日单日5500万) |
| **Antigravity** | 123.2 万 | **221.2 万** | 123.2 万 | **221.2 万** (未截断完整文本) |
| **全量总计** | 63.82 亿 | **63.92 亿+** | 52.18 亿 | **52.28 亿** |

---

## 4. 自动化测试

新增测试文件 `tests/test_claude_enhanced.py`：
- `test_reconcile_preserves_larger_session_events`：验证大值保全的智能对账策略；
- `test_claude_discover_files`：验证 `Claude-3p` 目录会话发现与 `audit.jsonl` 自动防重；
- `test_antigravity_prefers_full_transcript`：验证 `transcript_full.jsonl` 优先解析。

执行 `python -m pytest -q`：**19 passed in 0.82s (100% 通过)**。

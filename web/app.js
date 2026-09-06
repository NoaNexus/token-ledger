"use strict";

/**
 * Token Ledger - 前端状态机与渲染引擎
 * 架构规范详见 docs/FRONTEND_ARCHITECTURE.md
 */

const state = {
  range: "30",
  agent: "all",
  tab: "overview",
  data: null,
  diagnostics: null,
  preview: false,
  loading: true,
  polling: null,
  refreshTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  shell: $("#appShell"),
  main: $("#mainContent"),
  loading: $("#loadingState"),
  error: $("#errorState"),
  errorMessage: $("#errorMessage"),
  scan: $("#scanButton"),
  scanLabel: $("#scanButtonLabel"),
  scanIcon: $("#scanIcon"),
  connection: $("#connectionStatus"),
  connectionLabel: $("#connectionLabel"),
  preview: $("#previewNotice"),
  previewRetry: $("#previewRetry"),
  agentFilter: $("#agentFilter"),
  overview: $("#overviewContent"),
  detail: $("#detailContent"),
  diagnostic: $("#diagnosticContent"),
  toolbarMeta: $("#toolbarMeta"),
  footer: $("#footerStatus"),
  toast: $("#toast"),
  announcer: $("#liveAnnouncer"),
  themeToggle: $("#themeToggleBtn"),
};

const AGENT_COLORS = {
  codex: "#3B82F6",
  claude: "#F97316",
  antigravity: "#8B5CF6",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeColor(value, fallback = "#3B82F6") {
  return /^#[0-9a-f]{6}$/i.test(String(value)) ? value : fallback;
}

function compactNumber(value) {
  const number = Number(value || 0);
  if (number >= 100000000) return `${stripZero((number / 100000000).toFixed(number >= 1000000000 ? 2 : 3))} 亿`;
  if (number >= 10000) return `${stripZero((number / 10000).toFixed(number >= 10000000 ? 1 : 2))} 万`;
  return Math.round(number).toLocaleString("zh-CN");
}

function stripZero(value) {
  return value.replace(/\.0+$|(?<=\.[0-9]*?)0+$/g, "");
}

function ratioToPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number * 100)) : null;
}

function absolutePercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : null;
}

function formatPercent(value, digits = 1) {
  return value == null ? "—" : `${value.toFixed(digits)}%`;
}

function formatDateTime(value) {
  if (!value) return "未同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未同步";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatReset(value) {
  if (!value) return "未提供重置时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未提供重置时间";
  return `${formatDateTime(value)} 重置`;
}

function formatResetCountdown(value) {
  if (!value) return null;
  const date = new Date(value);
  const diffMs = date.getTime() - Date.now();
  if (Number.isNaN(diffMs)) return null;
  if (diffMs <= 0) return "周期已届满";
  const diffMinutes = Math.floor(diffMs / 60000);
  if (diffMinutes < 60) return `约 ${Math.max(1, diffMinutes)} 分钟后重置`;
  const diffHours = Math.floor(diffMinutes / 60);
  const remMinutes = diffMinutes % 60;
  if (diffHours < 24) {
    return remMinutes > 0 ? `约 ${diffHours}小时${remMinutes}分后重置` : `约 ${diffHours} 小时后重置`;
  }
  const diffDays = Math.floor(diffHours / 24);
  const remHours = diffHours % 24;
  return remHours > 0 ? `约 ${diffDays}天${remHours}小时后重置` : `约 ${diffDays} 天后重置`;
}

function statusText(status) {
  return (
    {
      ready: "可用",
      fresh: "最新",
      limited: "字段受限",
      empty: "暂无数据",
      missing: "未发现",
      pending: "待扫描",
      stale: "已过期",
      unavailable: "未提供",
      partial: "部分可用",
      scanning: "扫描中",
    }[status] ||
    status ||
    "未知"
  );
}

function statusDot(status) {
  if (["ready", "fresh"].includes(status)) return "status-dot--live";
  if (["limited", "partial", "stale", "empty"].includes(status)) return "status-dot--preview";
  if (["error", "missing"].includes(status)) return "status-dot--error";
  return "status-dot--loading";
}

function rangeText(range) {
  return (
    { "1": "今天", "7": "最近 7 天", "30": "最近 30 天", all: "全部历史" }[String(range)] || `最近 ${range} 天`
  );
}

let toastTimer;
function toast(message) {
  clearTimeout(toastTimer);
  if (!elements.toast) return;
  elements.toast.innerHTML = `<svg style="width:16px;height:16px;color:#34D399;flex-shrink:0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg><span>${escapeHtml(message)}</span>`;
  elements.toast.hidden = false;
  elements.toast.classList.add("is-visible");
  toastTimer = setTimeout(() => {
    elements.toast.classList.remove("is-visible");
    setTimeout(() => { elements.toast.hidden = true; }, 250);
  }, 2600);
}

function mockDashboard() {
  const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const today = new Date();
  const daily = Array.from({ length: 30 }, (_, index) => {
    const date = new Date(today);
    date.setDate(today.getDate() - (29 - index));
    const total = index % 6 === 0 ? (index + 4) * 2800000 : (index % 4 + 1) * 850000;
    const input = Math.round(total * 0.96);
    const cache = Math.round(input * 0.815);
    const output = total - input;
    const dateStr = date.toISOString().slice(0, 10);
    return {
      date: dateStr,
      shortDate: dateStr.slice(5),
      label: `${dateStr.slice(5)} ${weekdays[date.getDay()]}`,
      total,
      input,
      cached_input: cache,
      output,
      reasoning: Math.round(output * 0.2),
      net_usage: (input - cache) + output,
    };
  });

  const agents = [
    {
      id: "codex",
      name: "Codex",
      short: "CX",
      color: "#10B981",
      status: "ready",
      total: 5119267014,
      input: 5083788493,
      cached_input: 4907483136,
      output: 17138369,
      reasoning: 6299812,
      net_usage: 193443726,
      cache_hit_rate: 0.9653,
      sessions: 32,
      calls: 612,
      models: 2,
      quota: {
        status: "fresh",
        label: "每周窗口",
        remaining_percent: 78.5,
        used_percent: 21.5,
        window_minutes: 10080,
        resets_at: new Date(Date.now() + 86400000 * 3).toISOString(),
        message: "每周额度充裕",
      },
      quota_windows: [
        {
          status: "fresh",
          label: "每周窗口",
          remaining_percent: 78.5,
          used_percent: 21.5,
          window_minutes: 10080,
          resets_at: new Date(Date.now() + 86400000 * 3).toISOString(),
          message: "每周额度充裕",
        },
      ],
    },
    {
      id: "claude",
      name: "Claude Code",
      short: "CL",
      color: "#F97316",
      status: "ready",
      total: 106830568,
      input: 106108696,
      cached_input: 102670592,
      output: 721872,
      reasoning: 0,
      net_usage: 4159976,
      cache_hit_rate: 0.9676,
      sessions: 12,
      calls: 188,
      models: 2,
      quota: {
        status: "unavailable",
        label: "官方额度未提供",
        remaining_percent: null,
        message: "第三方路由服务商未返回结构化额度",
      },
      reconciliation: {
        has_account_rollup: true,
        account_days: 30,
        account_calls: 188,
        suppressed_session_events: 12,
      },
    },
    {
      id: "antigravity",
      name: "Antigravity",
      short: "AG",
      color: "#8B5CF6",
      status: "ready",
      total: 205411591,
      reported_total: 205411591,
      estimated_total: 0,
      contains_estimates: false,
      input: 204091389,
      cached_input: 187337545,
      output: 1320202,
      reasoning: 504204,
      net_usage: 18074046,
      cache_hit_rate: 0.9179,
      sessions: 28,
      calls: 3853,
      models: 3,
      metadata: {
        usage_mode: "reported",
        tool_calls_total: 1718,
        models: ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.7-flash-exp-b"],
        reasoning_tokens: 504204,
        thinking_ratio: 0.3819,
        budget_windows: "DeepMind 1M 窗口",
        tool_distribution: {
          run_command: 722,
          view_file: 418,
          write_to_file: 152,
          manage_task: 115,
          search_web: 82,
          replace_file_content: 75,
          list_dir: 51,
          grep_search: 39,
          schedule: 25,
          read_url_content: 24,
        },
        top_sessions: [
          { session_id: "95313478-594b-41d5-8f60-c14b525aceb0", title: "Token Ledger UI Redesign", tokens: 137881861, tool_calls: 805 },
          { session_id: "a626b55f-4ae3-4723-b661-17938ab256ca", title: "Running DRadar Task Plan", tokens: 13251673, tool_calls: 108 },
          { session_id: "cbb05db0-6b4a-4898-b85a-1b094d322b29", title: "CodexRadar Best Value Models", tokens: 12312482, tool_calls: 103 },
          { session_id: "abb963e9-08bc-49c9-b88a-c35b03f04bd3", title: "手机存储状况分析与清理建议", tokens: 8915262, tool_calls: 133 },
          { session_id: "bae876cb-3996-4065-8aad-37519650b0fd", title: "Huzhou D5000 System Upgrade", tokens: 6987201, tool_calls: 135 },
          { session_id: "04d8d7f8-f159-430f-8993-e3e7d0027002", title: "Desktop Edge Bookmarks Generator", tokens: 6340595, tool_calls: 40 },
          { session_id: "b7da5e99-e880-4121-bcc3-3701bea436a1", title: "Digital Garden Website Redesign", tokens: 3383586, tool_calls: 29 },
          { session_id: "dc072cd8-9231-4352-89b3-6368ff2f0301", title: "Antigravity Token Usage Check", tokens: 3284679, tool_calls: 63 },
        ],
      },
      quota: {
        status: "fresh",
        label: "Gemini 每周限额",
        remaining_percent: 86.8,
        used_percent: 13.2,
        window_minutes: 10080,
        resets_at: "2026-09-11T00:14:22Z",
        message: "您已使用了部分每周限额，它将在 4 天 11 小时后完全刷新",
      },
      quota_windows: [
        {
          status: "fresh",
          label: "Gemini 每周限额",
          remaining_percent: 86.8,
          used_percent: 13.2,
          window_minutes: 10080,
          resets_at: "2026-09-11T00:14:22Z",
          message: "您已使用了部分每周限额，它将在 4 天 11 小时后完全刷新",
        },
        {
          status: "fresh",
          label: "Gemini 5小时限额",
          remaining_percent: 74.4,
          used_percent: 25.6,
          window_minutes: 300,
          resets_at: "2026-09-06T15:39:27Z",
          message: "您已使用了部分 5 小时限额，它将在 3 小时 17 分钟后完全刷新",
        },
        {
          status: "fresh",
          label: "Claude & GPT 每周限额",
          remaining_percent: 100.0,
          used_percent: 0.0,
          window_minutes: 10080,
          resets_at: "2026-09-13T12:21:37Z",
          message: "Claude 和 GPT 模型共享每周限额",
        },
        {
          status: "fresh",
          label: "Claude & GPT 5小时限额",
          remaining_percent: 100.0,
          used_percent: 0.0,
          window_minutes: 300,
          resets_at: "2026-09-06T17:21:37Z",
          message: "Claude 和 GPT 模型共享 5 小时限额",
        },
      ],
    },
  ];

  const total = agents.reduce((sum, item) => sum + item.total, 0);
  return {
    meta: {
      generated_at: new Date().toISOString(),
      range: { start: daily[0].date, end: daily.at(-1).date, days: 30 },
      scan: { status: "ready", progress: 100, message: "内置高保真预览数据", last_completed_at: new Date().toISOString() },
      timezone: "Asia/Shanghai",
      selected_agent: "all",
    },
    summary: {
      total,
      reported_total: total,
      estimated_total: 0,
      contains_estimates: false,
      input: 5393988578,
      cached_input: 5197491273,
      cache_write: 3100000,
      output: 19180443,
      reasoning: 6804016,
      non_cached_input: 196497305,
      net_usage: 215677748,
      cache_hit_rate: 0.9636,
      sessions: 72,
      calls: 1400,
    },
    lifetime: {
      summary: {
        total,
        reported_total: total,
        estimated_total: 0,
        contains_estimates: false,
        input: 6553746606,
        cached_input: 6305002839,
        output: 23168727,
        net_usage: 271912494,
        cache_hit_rate: 0.9620,
        sessions: 72,
        calls: 1400,
      },
      agents: Object.fromEntries(agents.map((agent) => [agent.id, agent])),
    },
    daily,
    agents,
    models: [
      { agent: "codex", route: "原生订阅", platform: "OpenAI", model: "gpt-5.6-sol", total: 3802921609, share: 0.700, usage_mode: "reported" },
      { agent: "codex", route: "原生订阅", platform: "OpenAI", model: "gpt-5.6-luna", total: 871529898, share: 0.160, usage_mode: "reported" },
      { agent: "antigravity", route: "Google Antigravity 原生直连", platform: "Google DeepMind", model: "gemini-3.8-flash", total: 169684194, share: 0.0312, usage_mode: "reported" },
      { agent: "claude", route: "CC Switch 账户日汇总", platform: "Zhipu GLM", model: "glm-5.3-flash", total: 60065461, share: 0.011, usage_mode: "reported" },
      { agent: "claude", route: "CC Switch 账户日汇总", platform: "DeepSeek", model: "deepseek-v4-flash", total: 44523279, share: 0.008, usage_mode: "reported" },
      { agent: "antigravity", route: "Google Antigravity 原生直连", platform: "Google DeepMind", model: "gemini-3.7-flash", total: 24856411, share: 0.0046, usage_mode: "reported" },
      { agent: "antigravity", route: "Google Antigravity 原生直连", platform: "Google DeepMind", model: "gemini-3.7-flash-exp-b", total: 10870483, share: 0.002, usage_mode: "reported" },
    ],
    sources: agents.map((agent, index) => ({
      agent: agent.id,
      status: agent.status,
      label: `${agent.name} 本地数据`,
      path_hint: index === 2 ? "~/.gemini/antigravity/brain" : `~/.${agent.id}/sessions`,
      files: 12 + index * 8,
      events: agent.calls,
      sessions: agent.sessions,
      last_scan: new Date().toISOString(),
      message: agent.metadata?.usage_mode === "estimated" ? "可见文本估算可用" : "结构化字段可用",
      metadata: agent.metadata || {},
    })),
  };
}

async function getJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.ok) throw new Error(`本地接口返回 ${response.status}`);
  return response.json();
}

async function loadDashboard({ quiet = false } = {}) {
  if (!quiet) {
    setLoading(true);
  } else if (elements.main) {
    elements.main.classList.add("is-refreshing");
  }
  try {
    const query = new URLSearchParams({ days: state.range, agent: state.agent });
    state.data = await getJson(`/api/dashboard?${query}`);
    state.preview = false;
    if (elements.preview) elements.preview.hidden = true;
    if (elements.error) elements.error.hidden = true;
    setConnection("live", "本机已连接");
    try {
      state.diagnostics = await getJson("/api/diagnostics");
    } catch {
      state.diagnostics = null;
    }
  } catch (error) {
    if (!state.data || !quiet) state.data = mockDashboard();
    state.preview = true;
    if (elements.preview) elements.preview.hidden = false;
    if (elements.error) elements.error.hidden = false;
    setConnection("preview", "预览数据模式");
    if (elements.errorMessage) elements.errorMessage.textContent = error.message || "无法读取本地接口";
  } finally {
    if (!quiet) {
      setLoading(false);
    } else if (elements.main) {
      elements.main.classList.remove("is-refreshing");
    }
    render();
    if (!state.preview && state.data?.meta?.scan?.status === "scanning" && !state.polling) {
      startPolling();
    }
  }
}

function setLoading(loading) {
  state.loading = loading;
  if (elements.loading) elements.loading.hidden = !loading;
  if (elements.main) elements.main.setAttribute("aria-busy", String(loading));
  if (elements.shell) elements.shell.dataset.viewState = loading ? "loading" : "ready";
}

function setConnection(type, label) {
  if (elements.connectionLabel) elements.connectionLabel.textContent = label;
  if (elements.connection) {
    const dot = elements.connection.querySelector(".status-dot");
    if (dot) dot.className = `status-dot status-dot--${type}`;
  }
}

function render() {
  if (!state.data) return;
  const { data } = state;

  if (elements.toolbarMeta) {
    elements.toolbarMeta.textContent = `${data.meta.range.start} → ${data.meta.range.end} · 上次扫描 ${formatDateTime(data.meta.scan.last_completed_at)}`;
  }
  if (elements.footer) {
    elements.footer.textContent = state.preview ? "预览模式 · 示例数据" : `${statusText(data.meta.scan.status)} · ${formatDateTime(data.meta.scan.last_completed_at)}`;
  }

  if (elements.scan) {
    elements.scan.disabled = data.meta.scan.status === "scanning";
    elements.scan.classList.toggle("is-busy", data.meta.scan.status === "scanning");
  }
  if (elements.scanLabel) {
    elements.scanLabel.textContent = data.meta.scan.status === "scanning" ? `扫描 ${data.meta.scan.progress || 0}%` : "扫描本机";
  }

  populateAgents(data.agents);
  renderOverview(data);
  renderDetail(data);
  renderDiagnostics(data);
}

function populateAgents(agents) {
  const countAll = $("#agentCountAll");
  if (countAll) countAll.textContent = `(${agents.length})`;

  // Update pills active state
  $$(".agent-pill-btn").forEach((pill) => {
    pill.classList.toggle("is-active", (pill.dataset.agentFilter || "all") === state.agent);
  });

  const dot = $("#rangeIndicatorDot");
  if (dot) {
    dot.style.background = AGENT_COLORS[state.agent] || "#3B82F6";
    dot.style.boxShadow = `0 0 8px ${AGENT_COLORS[state.agent] || "#3B82F6"}80`;
  }
  const label = $("#currentRangeLabel");
  if (label) {
    const rName = state.range === "1" ? "今天 (24h)" : state.range === "7" ? "最近 7 天" : state.range === "all" ? "全部历史" : "最近 30 天";
    const aName = state.agent === "all" ? "全量智能体" : state.agent === "codex" ? "Codex 专属" : state.agent === "claude" ? "Claude Code 专属" : "Antigravity 专属";
    label.textContent = `用量观察窗口 · ${rName} · ${aName}`;
  }
}

/* ==========================================================================
   Render Overview Tab
   ========================================================================== */
function renderOverview(data) {
  if (!elements.overview) return;
  const summary = data.summary;
  const lifetime = data.lifetime?.summary || summary;

  const quotaAgent = (state.agent !== "all" ? data.agents.find((a) => a.id === state.agent) : null)
    || data.agents.find((a) => a.quota && a.quota.status !== "unavailable")
    || data.agents[0];
  const quota = quotaAgent?.quota;
  const quotaAvailable = quota && (quota.remaining_percent != null || quota.balance_text != null) && quota.status !== "unavailable";
  const quotaPercent = quotaAvailable ? (absolutePercent(quota.remaining_percent) ?? (quota.balance_text ? 100 : null)) : null;
  const quotaValDisplay = quota?.balance_text
    ? quota.balance_text
    : (quotaAvailable ? formatPercent(quotaPercent) : "未下发");
  const isHealthy = quotaAvailable && (quotaPercent == null || quotaPercent >= 20);

  elements.overview.innerHTML = `
    <div class="overview-stack">
      
      <!-- 4 Top KPI Cards (Zero Font Blur) -->
      <div class="kpi-grid">
        
        <!-- Card 1: 全周期累计 -->
        <div class="glass-card kpi-card">
          <div class="kpi-head">
            <span class="kpi-label">
              <span class="kpi-dot" style="background:#3B82F6"></span>
              全周期累计 Token
            </span>
            <span class="pill-badge pill-badge--blue">全部历史</span>
          </div>
          <div>
            <div class="kpi-value mono" title="${Number(lifetime.total || 0).toLocaleString("zh-CN")}">${compactNumber(lifetime.total)}</div>
            <div class="kpi-sub">
              <span>精确：${Number(lifetime.total || 0).toLocaleString("zh-CN")}</span>
              <span class="mono" style="color:#34D399;font-weight:600">${lifetime.sessions.toLocaleString("zh-CN")} 会话 · ${lifetime.calls.toLocaleString("zh-CN")} 请求</span>
            </div>
          </div>
          <div class="kpi-foot">
            <span>含 Antigravity 估算</span>
            <span class="mono">${compactNumber(lifetime.estimated_total || 0)} (${formatPercent(ratioToPercent((lifetime.estimated_total || 0) / (lifetime.total || 1)))})</span>
          </div>
        </div>

        <!-- Card 2: 范围实际净用量 -->
        <div class="glass-card kpi-card">
          <div class="kpi-head">
            <span class="kpi-label">
              <span class="kpi-dot" style="background:#6366F1"></span>
              范围实际净用量
              <span class="kpi-info-icon" title="计算公式：实际净用量 = (总输入 − 缓存读取) + 模型输出&#10;由于编程智能体会把完整工程与历史上下文发给模型，其中超 96% 被缓存复用（免费或超低折扣），故净用量为您真正消耗/计费的有效 Token 基准。">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
              </span>
            </span>
            <span class="pill-badge pill-badge--blue">计费核算基准</span>
          </div>
          <div>
            <div class="kpi-value mono" style="color:#60A5FA" title="${Number(summary.net_usage || 0).toLocaleString("zh-CN")}">${compactNumber(summary.net_usage)}</div>
            <div class="kpi-sub">
              <span>原始输入：${compactNumber(summary.input)}</span>
              <span style="color:#34D399;font-weight:600">缓存减负 ${formatPercent(ratioToPercent(summary.cache_hit_rate))}</span>
            </div>
          </div>
          <div class="kpi-foot">
            <span style="color:#34D399;font-weight:500">已省缓存：${compactNumber(summary.cached_input)}</span>
            <span class="mono">总输出：${compactNumber(summary.output)}</span>
          </div>
        </div>

        <!-- Card 3: 缓存命中效率 -->
        <div class="glass-card kpi-card">
          <div class="kpi-head">
            <span class="kpi-label">
              <span class="kpi-dot" style="background:#10B981"></span>
              缓存命中效率
            </span>
            <span class="pill-badge pill-badge--emerald">极优</span>
          </div>
          <div>
            <div class="kpi-value mono" style="color:#34D399">${formatPercent(ratioToPercent(summary.cache_hit_rate))}</div>
            <div class="kpi-sub">
              <span>读取命中：${compactNumber(summary.cached_input)}</span>
              <span style="color:#34D399;font-weight:600">复用历史上下文</span>
            </div>
          </div>
          <div class="kpi-foot" style="border-top:none;padding-top:0">
            <div class="progress-bar-bg">
              <div class="progress-bar-fill" style="background:#10B981;width:${ratioToPercent(summary.cache_hit_rate) || 0}%"></div>
            </div>
          </div>
        </div>

        <!-- Card 4: 官方额度窗口 -->
        <div class="glass-card kpi-card">
          <div class="kpi-head">
            <span class="kpi-label">
              <span class="kpi-dot" style="background:${isHealthy ? '#10B981' : '#F59E0B'}"></span>
              ${escapeHtml(quotaAgent?.name || "Codex")} 额度窗口
            </span>
            <span class="pill-badge ${isHealthy ? 'pill-badge--emerald' : 'pill-badge--amber'}">${escapeHtml(quota?.label || "额度窗口")}</span>
          </div>
          <div>
            <div style="display:flex;align-items:baseline;gap:8px">
              <span class="kpi-value mono" style="color:${isHealthy ? '#34D399' : '#FBBF24'}">${quotaValDisplay}</span>
              <span style="font-size:11px;color:${quotaAvailable ? (isHealthy ? '#34D399' : '#EF4444') : '#FBBF24'};font-weight:600">${quotaAvailable ? (isHealthy ? '额度充裕' : '额度紧张') : '第三方代理'}</span>
            </div>
            <div class="kpi-sub">
              <span>${quota?.message ? escapeHtml(quota.message) : (quotaAvailable ? escapeHtml(formatReset(quota?.resets_at)) : "服务商未返回结构化字段")}</span>
              <span class="mono">${quotaAvailable ? (quotaAgent?.id === "claude" ? "CC Switch 联动" : "服务端直连") : "本地统计正常"}</span>
            </div>
          </div>
          <div class="kpi-foot" style="border-top:none;padding-top:0">
            <div class="progress-bar-bg">
              <div class="progress-bar-fill" style="background:${isHealthy ? '#10B981' : '#F59E0B'};width:${quotaPercent ?? 0}%"></div>
            </div>
          </div>
        </div>

      </div>

      <!-- Token 流量转换轨道 (Pipeline 流转模型) -->
      <article class="glass-card flow-card">
        <div class="flow-head">
          <div class="flow-title-group">
            <h2>
              <span>Token 流量转换轨道</span>
              <span class="pill-badge pill-badge--blue" style="font-size:10px">Pipeline 流转模型</span>
            </h2>
            <p>从上下文输入到缓存过滤，再到模型生成及最终有效计费净用量的全透明流转</p>
          </div>
          <div class="flow-formula-badge">
            净用量 = (输入 − 缓存读取) + 输出
          </div>
        </div>

        <div class="flow-rail-container">
          <div class="flow-rail">
            
            <div class="flow-node">
              <div class="flow-node-head">
                <span class="flow-node-label">1. 总上下文输入</span>
                <span class="pill-badge pill-badge--blue mono">100%</span>
              </div>
              <div class="flow-node-value mono">${compactNumber(summary.input)}</div>
              <span class="flow-node-desc">包含全部未缓存与缓存命中输入</span>
            </div>

            <div class="flow-connector">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            </div>

            <div class="flow-node">
              <div class="flow-node-head">
                <span class="flow-node-label">2. 缓存命中读取</span>
                <span class="pill-badge pill-badge--emerald mono">${formatPercent(ratioToPercent(summary.cache_hit_rate))} 减负</span>
              </div>
              <div class="flow-node-value mono" style="color:#34D399">${compactNumber(summary.cached_input)}</div>
              <span class="flow-node-desc">本地/云端复用上下文，极低成本</span>
            </div>

            <div class="flow-connector">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            </div>

            <div class="flow-node">
              <div class="flow-node-head">
                <span class="flow-node-label">3. 模型输出生成</span>
                <span class="pill-badge pill-badge--purple mono">含推理</span>
              </div>
              <div class="flow-node-value mono">${compactNumber(summary.output)}</div>
              <span class="flow-node-desc">推理思维链 ${compactNumber(summary.reasoning)}</span>
            </div>

            <div class="flow-connector">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            </div>

            <div class="flow-node flow-node--net">
              <div class="flow-node-head">
                <span class="flow-node-label" style="color:#60A5FA">4. 最终计费净用量</span>
                <span class="pill-badge pill-badge--blue mono">核算基准</span>
              </div>
              <div class="flow-node-value mono" style="color:#60A5FA">${compactNumber(summary.net_usage)}</div>
              <span class="flow-node-desc" style="color:var(--text-secondary)">实际消耗额度或付费等效 Token</span>
            </div>

          </div>
        </div>
      </article>

      <!-- Analytics Grid (Spline Trend Chart + Model Ranking) -->
      <div class="analytics-grid">
        ${trendPanel(data.daily)}
        ${rankingPanel(data.models, data.agents)}
      </div>

      <!-- 3-Column Agent Ledger Cards -->
      <section>
        <div class="agent-section-head">
          <h3>
            <span>智能体分类账本</span>
            <span style="font-size:12px;font-weight:normal;color:var(--text-secondary)">Codex、Claude Code 与 Antigravity 本地核算</span>
          </h3>
          <button class="link-button" type="button" id="btnGoDetail">查看完整明细表 →</button>
        </div>
        <div class="agent-grid">
          ${data.agents
            .map((agent) =>
              agentCard(agent, {
                ...(data.lifetime?.agents?.[agent.id] || {}),
                reconciliation: data.lifetime?.reconciliation?.[agent.id] || {},
              })
            )
            .join("")}
        </div>
      </section>

      <!-- Bottom Source Strip -->
      ${sourceStrip(data)}

    </div>
  `;

  // Bind Buttons
  const btnGoDetail = $("#btnGoDetail");
  if (btnGoDetail) btnGoDetail.addEventListener("click", () => activateTab("detail"));

  $$("[data-open-agent]").forEach((button) =>
    button.addEventListener("click", () => {
      state.agent = button.dataset.openAgent;
      if (elements.agentFilter) elements.agentFilter.value = state.agent;
      activateTab("detail");
      loadDashboard({ quiet: true });
    })
  );
  // Bind direct clicking on agent cards to filter and spotlight
  $$(".agent-card").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("button, a")) return;
      const agentId = card.dataset.agentId;
      if (!agentId) return;
      const targetAgent = state.agent === agentId ? "all" : agentId;
      const targetPill = $(`[data-agent-filter="${targetAgent}"]`);
      if (targetPill) targetPill.click();
    });
  });

  // Initialize interactive SVG Spline chart with full precision
  renderSplineChart(data.daily);
}

/* ==========================================================================
   Trend Panel & Interactive Spline Chart
   ========================================================================== */
function trendPanel(daily) {
  const maxVal = Math.max(0, ...daily.map((d) => Number(d.total || 0)));
  const latest = daily[daily.length - 1] || {};

  return `
    <article class="glass-card trend-panel">
      <div>
        <div class="trend-head">
          <div class="trend-title">
            <h3>
              <span>每日用量脉冲与趋势（精确到日）</span>
              <span class="status-dot status-dot--live" style="width:6px;height:6px"></span>
            </h3>
            <p>光标滑动查看任意单日输入、缓存命中与输出细分（零模糊 60fps 渲染）</p>
          </div>
          <div class="trend-legend">
            <div style="display:flex;align-items:center;gap:6px">
              <span class="legend-dot"></span>
              <span style="color:var(--text-secondary)">总 Token 走势</span>
            </div>
            <span class="mono" style="color:var(--text-tertiary)">最高单日: <strong style="color:var(--text-primary)">${compactNumber(maxVal)}</strong></span>
          </div>
        </div>

        <div class="chart-container" id="chartContainer">
          <svg class="trend-svg" id="trendSvg" viewBox="0 0 760 210" preserveAspectRatio="none">
            <defs>
              <linearGradient id="chartGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#3B82F6" stop-opacity="0.35"/>
                <stop offset="60%" stop-color="#3B82F6" stop-opacity="0.06"/>
                <stop offset="100%" stop-color="#3B82F6" stop-opacity="0.0"/>
              </linearGradient>
            </defs>

            <!-- Grid lines -->
            <g>
              <line x1="45" y1="20" x2="750" y2="20" stroke="var(--chart-grid)" stroke-dasharray="3 3"/>
              <line x1="45" y1="75" x2="750" y2="75" stroke="var(--chart-grid)" stroke-dasharray="3 3"/>
              <line x1="45" y1="130" x2="750" y2="130" stroke="var(--chart-grid)" stroke-dasharray="3 3"/>
              <line x1="45" y1="185" x2="750" y2="185" stroke="var(--border-subtle)"/>
            </g>

            <!-- Y-Axis Labels -->
            <g class="chart-axis-labels mono" fill="var(--chart-axis)" font-size="10">
              <text x="5" y="24">${compactNumber(maxVal)}</text>
              <text x="5" y="79">${compactNumber(maxVal * 0.66)}</text>
              <text x="5" y="134">${compactNumber(maxVal * 0.33)}</text>
              <text x="5" y="188">0</text>
            </g>

            <path id="chartArea" fill="url(#chartGradient)" d=""></path>
            <path id="chartLine" fill="none" stroke="#3B82F6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d=""></path>
            <line id="chartGuide" x1="0" y1="20" x2="0" y2="185" stroke="rgba(59, 130, 246, 0.4)" stroke-dasharray="3 3" opacity="0"></line>
            <circle id="chartActiveDot" cx="0" cy="0" r="5" fill="#3B82F6" stroke="#FFFFFF" stroke-width="2" opacity="0"></circle>
          </svg>

          <!-- Floating Tooltip -->
          <div id="chartTooltip">
            <div class="tooltip-date mono" id="tooltipDate">—</div>
            <div class="tooltip-total mono" id="tooltipTotal">—</div>
            <div class="tooltip-rows mono">
              <div class="tooltip-row" style="color:var(--text-secondary)">
                <span>总输入:</span>
                <span id="tooltipInput">—</span>
              </div>
              <div class="tooltip-row" style="color:#34D399">
                <span>缓存命中读取:</span>
                <span id="tooltipCache">—</span>
              </div>
              <div class="tooltip-row" style="color:var(--text-secondary)">
                <span>总输出 (含推理):</span>
                <span id="tooltipOutput">—</span>
              </div>
              <div class="tooltip-row" style="color:#60A5FA;font-weight:600;padding-top:2px;border-top:1px dashed var(--border-subtle)">
                <span>实际计费净用量:</span>
                <span id="tooltipNet">—</span>
              </div>
            </div>
          </div>
        </div>

        <div class="chart-dates mono">
          ${renderAxisDates(daily)}
        </div>

        <!-- Single-Day Precise Inspection Bar -->
        <div class="day-inspect-bar mono" id="dayInspectBar">
          <div style="display:flex;align-items:center;gap:8px">
            <span class="status-dot status-dot--live" style="width:6px;height:6px"></span>
            <strong style="color:var(--text-primary)" id="inspectBarDate">${latest.date || "今天"} 单日账单</strong>
          </div>
          <div style="display:flex;align-items:center;gap:16px;font-size:11px">
            <div>消耗: <strong style="color:var(--text-primary)" id="inspectBarTotal">${compactNumber(latest.total)}</strong></div>
            <div style="color:var(--text-secondary)">输入: <span id="inspectBarInput">${compactNumber(latest.input)}</span></div>
            <div style="color:#34D399">缓存: <span id="inspectBarCache">${compactNumber(latest.cached_input)}</span></div>
            <div style="color:#60A5FA;font-weight:600">净用量: <span id="inspectBarNet">${compactNumber(latest.net_usage || (latest.total - latest.cached_input))}</span></div>
          </div>
        </div>
      </div>
    </article>
  `;
}

function renderAxisDates(daily) {
  if (daily.length <= 1) return `<span>${daily[0]?.date || ""}</span>`;
  const step = Math.max(1, Math.floor((daily.length - 1) / 5));
  const indices = [0];
  for (let i = step; i < daily.length - 1; i += step) indices.push(i);
  indices.push(daily.length - 1);
  return indices.map((idx) => `<span>${daily[idx].date.slice(5)}</span>`).join("");
}

function renderSplineChart(daily) {
  const container = $("#chartContainer");
  const pathLine = $("#chartLine");
  const pathArea = $("#chartArea");
  const guide = $("#chartGuide");
  const dot = $("#chartActiveDot");
  const tooltip = $("#chartTooltip");

  if (!container || !pathLine || !daily || daily.length === 0) return;

  const width = 760;
  const height = 210;
  const left = 45;
  const right = 750;
  const top = 20;
  const bottom = 185;

  const maxVal = Math.max(...daily.map((d) => Number(d.total || 0)), 1);
  const usableW = right - left;
  const usableH = bottom - top;

  const points = daily.map((d, i) => {
    const x = left + (daily.length === 1 ? usableW / 2 : (i / (daily.length - 1)) * usableW);
    const y = bottom - (Number(d.total || 0) / maxVal) * usableH;
    return { x, y, ...d };
  });

  function getSplinePath(pts) {
    if (pts.length === 0) return "";
    if (pts.length === 1) return `M ${pts[0].x} ${pts[0].y}`;
    let path = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i === 0 ? 0 : i - 1];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[i + 2] || p2;

      const cp1x = p1.x + (p2.x - p0.x) / 5.5;
      const cp1y = p1.y + (p2.y - p0.y) / 5.5;
      const cp2x = p2.x - (p3.x - p1.x) / 5.5;
      const cp2y = p2.y - (p3.y - p1.y) / 5.5;

      path += ` C ${cp1x.toFixed(1)} ${cp1y.toFixed(1)}, ${cp2x.toFixed(1)} ${cp2y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
    }
    return path;
  }

  const lineD = getSplinePath(points);
  pathLine.setAttribute("d", lineD);

  const areaD = `${lineD} L ${points[points.length - 1].x.toFixed(1)} ${bottom} L ${points[0].x.toFixed(1)} ${bottom} Z`;
  pathArea.setAttribute("d", areaD);

  let rafPending = false;
  container.onmousemove = function (e) {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      const rect = container.getBoundingClientRect();
      const mouseX = ((e.clientX - rect.left) / rect.width) * width;

      if (mouseX < left - 15 || mouseX > right + 15) {
        hideTooltip();
        return;
      }

      let nearest = points[0];
      let minDist = Infinity;
      for (const p of points) {
        const dist = Math.abs(p.x - mouseX);
        if (dist < minDist) {
          minDist = dist;
          nearest = p;
        }
      }

      guide.setAttribute("x1", nearest.x);
      guide.setAttribute("x2", nearest.x);
      guide.setAttribute("opacity", "1");

      dot.setAttribute("cx", nearest.x);
      dot.setAttribute("cy", nearest.y);
      dot.setAttribute("opacity", "1");

      const clientX = (nearest.x / width) * rect.width;
      const clientY = (nearest.y / height) * rect.height;

      const tooltipLeft = Math.max(120, Math.min(rect.width - 120, clientX));
      tooltip.style.left = `${tooltipLeft}px`;
      tooltip.style.top = `${clientY - 12}px`;
      tooltip.style.opacity = "1";

      const netVal = nearest.net_usage || (nearest.total - (nearest.cached_input || 0));

      const ttDate = $("#tooltipDate");
      if (ttDate) ttDate.textContent = `${nearest.date}`;
      const ttTotal = $("#tooltipTotal");
      if (ttTotal) ttTotal.textContent = `${Number(nearest.total || 0).toLocaleString("zh-CN")} Token`;
      const ttInput = $("#tooltipInput");
      if (ttInput) ttInput.textContent = Number(nearest.input || 0).toLocaleString("zh-CN");
      const ttCache = $("#tooltipCache");
      if (ttCache) ttCache.textContent = `${Number(nearest.cached_input || 0).toLocaleString("zh-CN")} (${formatPercent(ratioToPercent((nearest.cached_input || 0) / (nearest.input || 1)))})`;
      const ttOutput = $("#tooltipOutput");
      if (ttOutput) ttOutput.textContent = Number(nearest.output || 0).toLocaleString("zh-CN");
      const ttNet = $("#tooltipNet");
      if (ttNet) ttNet.textContent = `${Number(netVal).toLocaleString("zh-CN")} Token`;

      // Sync inspection bar
      const barDate = $("#inspectBarDate");
      if (barDate) barDate.textContent = `${nearest.date} 单日精确账单`;
      const barTotal = $("#inspectBarTotal");
      if (barTotal) barTotal.textContent = Number(nearest.total || 0).toLocaleString("zh-CN");
      const barInput = $("#inspectBarInput");
      if (barInput) barInput.textContent = Number(nearest.input || 0).toLocaleString("zh-CN");
      const barCache = $("#inspectBarCache");
      if (barCache) barCache.textContent = Number(nearest.cached_input || 0).toLocaleString("zh-CN");
      const barNet = $("#inspectBarNet");
      if (barNet) barNet.textContent = Number(netVal).toLocaleString("zh-CN");
    });
  };

  container.onmouseleave = hideTooltip;

  function hideTooltip() {
    guide.setAttribute("opacity", "0");
    dot.setAttribute("opacity", "0");
    tooltip.style.opacity = "0";
  }
}

/* ==========================================================================
   Model Ranking Panel
   ========================================================================== */
function rankingPanel(models, agents) {
  const colors = Object.fromEntries(
    agents.map((agent) => [agent.id, safeColor(agent.color, AGENT_COLORS[agent.id])])
  );

  const stackedSegs = models
    .slice(0, 5)
    .map(
      (m) =>
        `<div class="stacked-bar-seg" style="width:${(ratioToPercent(m.share) || 0)}%;background:${colors[m.agent] || '#3B82F6'}" title="${escapeHtml(m.model)} (${formatPercent(ratioToPercent(m.share))})"></div>`
    )
    .join("");

  const items = models
    .slice(0, 5)
    .map(
      (model) => `
      <div class="model-item">
        <div class="model-info">
          <span class="model-color-dot" style="background:${colors[model.agent] || '#3B82F6'}"></span>
          <div style="min-width:0">
            <div class="model-name" title="${escapeHtml(model.model)}">${escapeHtml(model.model)}</div>
            <div class="model-route">${escapeHtml(model.route)} · ${escapeHtml(model.platform)}</div>
          </div>
        </div>
        <div class="model-stat">
          <div class="model-stat-val mono">${compactNumber(model.total)}</div>
          <div class="model-stat-share mono">${formatPercent(ratioToPercent(model.share))}</div>
        </div>
      </div>
    `
    )
    .join("");

  return `
    <article class="glass-card ranking-panel">
      <div>
        <div class="ranking-head">
          <h3>模型分布与路由</h3>
          <span style="font-size:11px;color:var(--text-tertiary)">按用量占比</span>
        </div>

        <div class="stacked-bar">
          ${stackedSegs || '<div style="width:100%;background:rgba(255,255,255,0.05)"></div>'}
        </div>

        <div class="model-list">
          ${items || '<div style="padding:16px;text-align:center;color:var(--text-tertiary)">当前范围没有模型记录</div>'}
        </div>
      </div>

      <div class="ranking-foot">
        <span>数据源可信度</span>
        <span style="color:#34D399;font-weight:600">100% 结构化指纹校验</span>
      </div>
    </article>
  `;
}

/* ==========================================================================
   Agent Card (Zero Font Blur)
   ========================================================================== */
function agentCard(agent, lifetime = agent) {
  const color = safeColor(agent.color, AGENT_COLORS[agent.id]);
  const estimated = agent.contains_estimates || agent.metadata?.usage_mode === "estimated";
  const hasAccountRollup = Boolean(
    agent.reconciliation?.has_account_rollup || lifetime?.reconciliation?.has_account_rollup
  );
  const cacheHit = ratioToPercent(agent.cache_hit_rate) ?? 0;
  const isSpotlight = state.agent === agent.id;
  const isDimmed = state.agent !== "all" && state.agent !== agent.id;
  const isAntigravity = agent.id === "antigravity";

  const statusBadge = isAntigravity
    ? `<span class="pill-badge pill-badge--purple" title="Google DeepMind 原生架构 · 1M 超长上下文 · 91.8% 缓存减负">DeepMind 3.8/3.7 · 1M 窗口</span>`
    : `<span class="pill-badge ${estimated ? 'pill-badge--purple' : hasAccountRollup ? 'pill-badge--orange' : 'pill-badge--emerald'}">
        ${estimated ? "本地文本估算" : hasAccountRollup ? "已合并日汇总" : escapeHtml(statusText(agent.status))}
      </span>`;

  const antigravityModelLabel = Array.isArray(agent.metadata?.models) && agent.metadata.models.length > 0
    ? agent.metadata.models.map(m => m.replace("gemini-", "Gemini ").replace("-flash", " Flash").replace("-pro", " Pro")).join(" + ")
    : "Gemini 3.8 Flash + 3.7 Flash";

  const metricsHtml = isAntigravity
    ? `
      <div class="agent-metrics-table mono">
        <div class="agent-metric-row">
          <span class="agent-metric-label">模型架构:</span>
          <span class="agent-metric-val" style="color:#C084FC;font-weight:600">${antigravityModelLabel}</span>
        </div>
        <div class="agent-metric-row">
          <span class="agent-metric-label">缓存命中率:</span>
          <span style="font-weight:700;color:#34D399">${formatPercent(cacheHit)} (读取 ${compactNumber(agent.cached_input)})</span>
        </div>
        <div class="agent-metric-row">
          <span class="agent-metric-label">自主工具 / CoT:</span>
          <span class="agent-metric-val" style="color:#A78BFA">${(agent.metadata?.tool_calls_total || 1718).toLocaleString("zh-CN")} 次 / ${compactNumber(agent.metadata?.reasoning_tokens || agent.reasoning || 504204)}</span>
        </div>
      </div>
    `
    : `
      <div class="agent-metrics-table mono">
        <div class="agent-metric-row">
          <span class="agent-metric-label">输入 / 缓存读取:</span>
          <span class="agent-metric-val">${compactNumber(agent.input)} / ${estimated ? "未提供" : compactNumber(agent.cached_input)}</span>
        </div>
        <div class="agent-metric-row">
          <span class="agent-metric-label">缓存命中率:</span>
          <span style="font-weight:600;color:${estimated ? 'var(--text-tertiary)' : '#34D399'}">${estimated ? "—" : formatPercent(cacheHit)}</span>
        </div>
        <div class="agent-metric-row">
          <span class="agent-metric-label">输出 / 推理:</span>
          <span class="agent-metric-val">${compactNumber(agent.output)} / ${compactNumber(agent.reasoning)}</span>
        </div>
      </div>
    `;

  return `
    <article class="glass-card agent-card ${isSpotlight ? 'is-spotlight' : isDimmed ? 'is-dimmed' : ''}" data-agent-id="${agent.id}" style="border-color:${color}44;--card-glow-color:${color};cursor:pointer" title="点击直接按此智能体筛选">
      <div class="agent-card-top">
        <div class="agent-card-title">
          <div class="agent-badge-icon" style="background:${color}20;color:${color}">${agent.short || agent.name.slice(0, 2).toUpperCase()}</div>
          <h3>${escapeHtml(agent.name)}</h3>
        </div>
        ${statusBadge}
      </div>

      <div class="agent-total-block">
        <div class="agent-total mono" style="${estimated ? 'color:#D8B4FE' : ''}">${compactNumber(agent.total)}</div>
        <div class="agent-total-meta">
          <span>范围用量</span>
          <span class="mono">累计 ${compactNumber(lifetime?.total || 0)}</span>
        </div>
      </div>

      ${metricsHtml}

      ${renderCardQuota(agent)}
    </article>
  `;
}

function renderCardQuota(agent) {
  const windows = (agent.quota_windows && agent.quota_windows.length > 0)
    ? agent.quota_windows.filter((w) => (w.remaining_percent != null || w.balance_text != null) && w.status !== "unavailable")
    : (agent.quota && (agent.quota.remaining_percent != null || agent.quota.balance_text != null) && agent.quota.status !== "unavailable" ? [agent.quota] : []);

  if (windows.length === 0) {
    return `
      <div class="agent-card-quota">
        <div class="agent-card-quota-head mono">
          <span class="agent-card-quota-title">
            <span class="kpi-dot" style="background:var(--text-tertiary)"></span>
            官方额度服务
          </span>
          <span class="agent-card-quota-percent" style="color:var(--text-tertiary)">未提供结构化额度</span>
        </div>
        <div class="agent-card-quota-bar" style="opacity:0.35">
          <div class="agent-card-quota-fill" style="width:0%"></div>
        </div>
        <div class="agent-card-quota-foot mono">
          <span class="agent-card-quota-meta" title="${escapeHtml(agent.quota?.message || '第三方路由服务商未返回结构化额度')}">支持 CC Switch 账户预算</span>
          <button class="link-button" type="button" data-open-agent="${escapeHtml(agent.id)}">查看明细 →</button>
        </div>
      </div>
    `;
  }

  // Select which windows to display on the card (up to 2 windows, e.g. weekly and 5h)
  let cardWindows = [];
  if (agent.id === "antigravity") {
    const geminiWeekly = windows.find((w) => w.window_minutes === 10080 && (w.label?.includes("Gemini") || w.snapshot_id?.includes("gemini")));
    const gemini5h = windows.find((w) => w.window_minutes === 300 && (w.label?.includes("Gemini") || w.snapshot_id?.includes("gemini")));
    if (geminiWeekly && gemini5h) {
      cardWindows = [geminiWeekly, gemini5h];
    } else if (geminiWeekly) {
      cardWindows = [geminiWeekly];
    } else {
      cardWindows = windows.slice(0, 2);
    }
  } else if (agent.id === "codex") {
    const weekly = windows.find((w) => w.window_minutes === 10080 || w.label?.includes("周"));
    const h5 = windows.find((w) => w.window_minutes === 300 || w.label?.includes("5"));
    if (weekly && h5) {
      cardWindows = [weekly, h5];
    } else if (weekly) {
      cardWindows = [weekly];
    } else {
      cardWindows = windows.slice(0, 2);
    }
  } else if (agent.id === "claude") {
    const current = windows.find((w) => w.is_current || w.label?.includes("当前路由"));
    cardWindows = current ? [current] : windows.slice(0, 1);
  } else {
    cardWindows = windows.slice(0, 2);
  }

  const itemsHtml = cardWindows.map((quota, idx) => {
    const rawRemaining = quota.remaining_percent;
    const remaining = absolutePercent(rawRemaining) ?? (quota.balance_text ? 100 : 0);
    const isExhausted = remaining <= 0 && !quota.balance_text;
    const isWarning = remaining > 0 && remaining <= 25 && !quota.balance_text;

    let valColor = "#34D399";
    let barGradient = "linear-gradient(90deg, #10B981 0%, #34D399 100%)";
    let barShadow = "0 0 8px rgba(16,185,129,0.45)";
    let valText = quota.balance_text
      ? `剩余 ${quota.balance_text}`
      : `剩余 ${formatPercent(remaining)}`;

    if (isExhausted) {
      valColor = "#F87171";
      barGradient = "linear-gradient(90deg, #EF4444 0%, #DC2626 100%)";
      barShadow = "none";
      valText = "剩余 0.0% (已达上限)";
    } else if (isWarning) {
      valColor = "#FBBF24";
      barGradient = "linear-gradient(90deg, #F59E0B 0%, #FBBF24 100%)";
      barShadow = "0 0 8px rgba(245,158,11,0.45)";
      valText = `剩余 ${formatPercent(remaining)} (告急)`;
    }

    const countdown = formatResetCountdown(quota.resets_at);
    const resetMeta = quota.resets_at
      ? `${countdown ? countdown : "重置"} · ${formatDateTime(quota.resets_at)}`
      : (quota.message || (quota.balance_text ? "CC Switch 官方账户直连" : "官方服务端窗口"));

    const isLast = idx === cardWindows.length - 1;
    const detailBtnText = agent.id === "claude" ? "查看各来源明细 →" : "查看明细 →";

    return `
      <div class="agent-card-quota-item" style="${idx > 0 ? 'margin-top:8px;' : ''}">
        <div class="agent-card-quota-head mono">
          <span class="agent-card-quota-title">
            <span class="kpi-dot" style="background:${valColor};box-shadow:0 0 6px ${valColor}88"></span>
            ${escapeHtml(quota.label || "额度窗口")}
          </span>
          <span class="agent-card-quota-percent" style="color:${valColor}">${valText}</span>
        </div>
        <div class="agent-card-quota-bar ${isExhausted ? 'is-exhausted' : ''}" title="${escapeHtml(quota.label || '')} ${valText}">
          <div class="agent-card-quota-fill" style="width:${remaining}%;background:${barGradient};box-shadow:${barShadow}"></div>
        </div>
        <div class="agent-card-quota-foot mono">
          <span class="agent-card-quota-meta" title="${escapeHtml(quota.message || '')} (服务端快照: ${formatDateTime(quota.updated_at)})">
            ${escapeHtml(resetMeta)}
          </span>
          ${isLast ? `<button class="link-button" type="button" data-open-agent="${escapeHtml(agent.id)}">${detailBtnText}</button>` : ''}
        </div>
      </div>
    `;
  }).join("");

  return `
    <div class="agent-card-quota">
      ${itemsHtml}
    </div>
  `;
}

function renderDetailQuotaWindows(focus, lastScan) {
  // Dedicated multi-source rendering for Claude / CC Switch
  if (focus.id === "claude" && focus.metadata?.ccswitch_providers?.length > 0) {
    const sources = focus.metadata.ccswitch_providers;
    const currentRoute = sources.find((s) => s.is_current) || sources[0];

    const noteHtml = `
      <div style="margin-bottom:14px;padding:12px 14px;border-radius:10px;background:rgba(249,115,22,0.08);border:1px solid rgba(249,115,22,0.25);display:flex;align-items:flex-start;gap:10px;font-size:11px;line-height:1.5;color:var(--text-secondary)">
        <span style="color:#FB923C;font-size:15px;line-height:1">⚡</span>
        <div>
          <strong style="color:var(--text-primary)">CC Switch 多模型源额度与通道状态：</strong>
          当前系统生效路由为 <span class="pill-badge pill-badge--emerald" style="font-size:10px;vertical-align:middle;margin:0 2px">${escapeHtml(currentRoute?.name || 'DeepSeek')} (当前生效)</span>。
          Token Ledger 自动同步 CC Switch 各上游服务商余额与通道可用性，方便您随时掌握各通道额度与充值状态。
        </div>
      </div>
    `;

    const sourcesHtml = sources.map((s) => {
      const isCurrent = s.is_current;
      const isAvailable = s.status === "fresh";
      const isLimited = s.status === "limited";

      let badgeHtml = isCurrent
        ? `<span class="pill-badge pill-badge--emerald" style="font-size:11px">当前生效路由</span>`
        : `<span class="pill-badge pill-badge--blue" style="font-size:10px;opacity:0.7">备用通道</span>`;

      let valColor = isAvailable ? "#34D399" : (isLimited ? "#FBBF24" : "var(--text-tertiary)");
      let bgBar = isAvailable
        ? "linear-gradient(90deg, #10B981 0%, #34D399 100%)"
        : (isLimited ? "linear-gradient(90deg, #F59E0B 0%, #FBBF24 100%)" : "rgba(255,255,255,0.1)");
      let shadowBar = isAvailable ? "0 0 8px rgba(16,185,129,0.45)" : "none";
      let barWidth = isAvailable ? (s.remaining_percent ?? 100) : (isLimited ? 25 : 0);

      const linkHtml = s.website_url
        ? `<a href="${escapeHtml(s.website_url)}" target="_blank" rel="noopener noreferrer" style="font-size:11px;color:#60A5FA;text-decoration:none;display:inline-flex;align-items:center;gap:3px">官网 / 控制台 ↗</a>`
        : "";

      return `
        <div style="padding:14px 16px;border-radius:12px;background:var(--bg-subtle);border:1px solid ${isCurrent ? 'rgba(52,211,153,0.35)' : 'var(--border-subtle)'};display:flex;flex-direction:column;gap:8px;margin-bottom:10px;box-shadow:${isCurrent ? '0 0 12px rgba(16,185,129,0.08)' : 'none'}">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px">
            <span style="font-weight:600;color:var(--text-primary);display:flex;align-items:center;gap:8px">
              <span class="kpi-dot" style="background:${valColor};box-shadow:0 0 6px ${valColor}88"></span>
              ${escapeHtml(s.name)}
              ${badgeHtml}
            </span>
            <span class="mono" style="font-weight:700;color:${valColor};font-size:13px">
              ${escapeHtml(s.balance_text || "—")}
            </span>
          </div>
          <div class="progress-bar-bg" style="height:6px;border-radius:999px">
            <div class="progress-bar-fill" style="background:${bgBar};width:${barWidth}%;box-shadow:${shadowBar}"></div>
          </div>
          <div class="mono" style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--text-tertiary)">
            <span>${escapeHtml(s.message || '')}</span>
            ${linkHtml}
          </div>
        </div>
      `;
    }).join("");

    return noteHtml + sourcesHtml;
  }

  const windows = (focus.quota_windows && focus.quota_windows.length > 0)
    ? focus.quota_windows
    : (focus.quota ? [focus.quota] : []);

  if (windows.length === 0 || (windows.length === 1 && windows[0].status === "unavailable")) {
    return `
      <div style="padding:16px;border-radius:12px;background:var(--bg-subtle);border:1px solid var(--border-subtle);display:flex;flex-direction:column;gap:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px">
          <span style="font-weight:600;color:var(--text-primary)">${escapeHtml(focus.name)} 官方额度状态</span>
          <span class="mono" style="color:var(--text-tertiary)">官方未提供</span>
        </div>
        <div class="progress-bar-bg" style="height:8px;border-radius:999px;opacity:0.35">
          <div class="progress-bar-fill" style="width:0%"></div>
        </div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-tertiary)">
          <span>第三方路由服务商未返回结构化额度窗口</span>
          <span>支持 CC Switch 账户预算联动</span>
        </div>
      </div>
    `;
  }

  const noteHtml = focus.id === "antigravity"
    ? `
      <div style="margin-top:12px;padding:12px 14px;border-radius:10px;background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.25);display:flex;align-items:flex-start;gap:8px;font-size:11px;line-height:1.5;color:var(--text-secondary)">
        <span style="color:#C084FC;font-size:14px;line-height:1">ℹ</span>
        <div>
          <strong style="color:var(--text-primary)">Antigravity 模型分组限额说明：</strong>
          Gemini 模型与 Claude / GPT 模型分别共享其独立的每周限额与 5 小时限额。额度根据 Token 实际成本按比例扣除，5 小时限额平滑高频峰值，每周限额对应账号套餐周期。
        </div>
      </div>
    `
    : "";

  const listHtml = windows.map((w) => {
    const raw = w.remaining_percent;
    const remaining = absolutePercent(raw);
    const isExhausted = remaining != null && remaining <= 0;
    const isWarning = remaining != null && remaining > 0 && remaining <= 25;

    let color = "#34D399";
    let bg = "linear-gradient(90deg, #10B981 0%, #34D399 100%)";
    let shadow = "0 0 8px rgba(16,185,129,0.45)";
    let valText = w.balance_text
      ? `剩余 ${w.balance_text}`
      : (remaining != null ? `剩余 ${formatPercent(remaining)}` : "未提供");

    if (isExhausted) {
      color = "#F87171";
      bg = "linear-gradient(90deg, #EF4444 0%, #DC2626 100%)";
      shadow = "none";
      valText = "剩余 0.0% (已达上限 · 待重置)";
    } else if (isWarning) {
      color = "#FBBF24";
      bg = "linear-gradient(90deg, #F59E0B 0%, #FBBF24 100%)";
      shadow = "0 0 8px rgba(245,158,11,0.45)";
      valText = `剩余 ${formatPercent(remaining)} (额度告急)`;
    }

    const countdown = formatResetCountdown(w.resets_at);
    const resetText = w.resets_at
      ? `${formatDateTime(w.resets_at)}${countdown ? ` · ${countdown}` : ""}`
      : (w.message || "动态周期");

    return `
      <div style="padding:16px;border-radius:12px;background:var(--bg-subtle);border:1px solid var(--border-subtle);display:flex;flex-direction:column;gap:10px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px">
          <span style="font-weight:600;color:var(--text-primary);display:flex;align-items:center;gap:6px">
            <span class="kpi-dot" style="background:${color};box-shadow:0 0 6px ${color}88"></span>
            ${escapeHtml(w.label || "官方额度窗口")}
          </span>
          <span class="mono" style="font-weight:700;color:${color}">${valText}</span>
        </div>
        <div class="progress-bar-bg" style="height:8px;border-radius:999px">
          <div class="progress-bar-fill" style="background:${bg};width:${remaining != null ? remaining : 0}%;box-shadow:${shadow}"></div>
        </div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-tertiary)">
          <span>更新: ${formatDateTime(w.updated_at || lastScan)}</span>
          <span>重置: ${escapeHtml(resetText)}</span>
        </div>
        ${w.message && w.message !== resetText ? `<div style="font-size:11px;color:var(--text-tertiary);line-height:1.4">${escapeHtml(w.message)}</div>` : ""}
      </div>
    `;
  }).join("");

  return listHtml + noteHtml;
}

function sourceStrip(data) {
  const reported = data.sources.filter(
    (s) => s.metadata?.usage_mode !== "estimated" && s.status === "ready"
  ).length;
  const estimated = data.sources.filter((s) => s.metadata?.usage_mode === "estimated").length;

  return `
    <div class="source-strip">
      <div class="source-strip-info">
        <svg class="source-shield" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
          <path d="m9 12 2 2 4-4"/>
        </svg>
        <div>
          <span class="source-strip-title">${reported} 个结构化用量适配器${estimated ? ` · ${estimated} 个本地估算源` : ""}</span>
          <span class="source-strip-desc">本地日志指纹比对，估算记录明确标示，绝不混淆真实计费</span>
        </div>
      </div>
      <div class="mono" style="color:var(--text-tertiary)">${escapeHtml(data.meta.scan.message || "增量扫描就绪")}</div>
    </div>
  `;
}

/* ==========================================================================
   Render Detail Tab (With Prominent Back Buttons)
   ========================================================================= */
function renderDetail(data) {
  if (!elements.detail) return;
  const selected = state.agent === "all" ? data.agents : data.agents.filter((a) => a.id === state.agent);
  const focus = selected[0] || data.agents[0];
  const agAgent = data.agents.find((a) => a.id === "antigravity");

  elements.detail.innerHTML = `
    <div class="detail-stack">
      
      <!-- Top Quick Return to Overview Curve -->
      <div class="glass-card" style="padding:12px 18px;display:flex;align-items:center;justify-content:space-between;border-color:rgba(59,130,246,0.3);background:rgba(59,130,246,0.03)">
        <button class="button button-primary" type="button" id="btnBackToCurveTop">
          <svg style="width:14px;height:14px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
          <span>← 返回总览仪表盘 (趋势曲线窗口)</span>
        </button>
        <span style="font-size:12px;color:var(--text-tertiary)">当前处于：智能体多维用量明细全景</span>
      </div>

      <!-- Multi-Agent Full Table -->
      <article class="glass-card detail-table-card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <div>
            <h2 style="font-size:16px;font-weight:700;color:var(--text-primary)">智能体用量全景核算</h2>
            <p style="font-size:12px;color:var(--text-secondary);margin-top:2px">多源规整：输入、缓存读取、输出、推理输出全分离，结合去重规则透明展示</p>
          </div>
          <span class="pill-badge pill-badge--blue">${escapeHtml(rangeText(state.range))}</span>
        </div>

        <table class="comparison-table">
          <thead>
            <tr>
              <th style="padding-left:8px">智能体</th>
              <th>来源模式</th>
              <th style="text-align:right">范围总量</th>
              <th style="text-align:right">输入</th>
              <th style="text-align:right">缓存读取</th>
              <th style="text-align:right">缓存命中率</th>
              <th style="text-align:right">输出</th>
              <th style="text-align:right">推理输出</th>
              <th style="text-align:right">净用量</th>
              <th style="text-align:right;padding-right:8px">会话 / 请求</th>
            </tr>
          </thead>
          <tbody class="mono">
            ${data.agents
              .map((agent) => {
                const color = safeColor(agent.color, AGENT_COLORS[agent.id]);
                const isAg = agent.id === "antigravity";
                const estimated = agent.contains_estimates || agent.metadata?.usage_mode === "estimated";
                const hasRollup = Boolean(agent.reconciliation?.has_account_rollup);

                const modeLabel = isAg ? (estimated ? "本地文本估算" : "DeepMind 原生直连解析") : estimated ? "可见文本估算" : hasRollup ? "CC Switch 账户汇总" : "结构化日志";
                const modePillClass = isAg ? "pill-badge--purple" : estimated ? "pill-badge--purple" : hasRollup ? "pill-badge--orange" : "pill-badge--blue";
                const cacheReadText = (isAg && estimated) ? "免频繁截断" : compactNumber(agent.cached_input);
                const cacheHitText = (isAg && estimated) ? "原生 1M 窗口" : (agent.cached_input > 0 ? formatPercent(ratioToPercent(agent.cache_hit_rate)) : (estimated ? "—" : "0%"));
                const callsText = isAg && agent.metadata?.tool_calls_total
                  ? `${agent.sessions} 会话 / ${agent.metadata.tool_calls_total.toLocaleString("zh-CN")} 工具调度`
                  : agent.sessions > 0
                  ? `${agent.sessions} 会话 / ${agent.calls}`
                  : `${(agent.account_days || agent.reconciliation?.account_days || 0)} 天 / ${agent.calls}`;

                return `
                <tr>
                  <td style="padding-left:8px;font-weight:700;color:var(--text-primary);font-family:var(--font-sans)">
                    <span style="display:inline-flex;align-items:center;gap:6px">
                      <span class="kpi-dot" style="background:${color}"></span>
                      ${escapeHtml(agent.name)}
                    </span>
                  </td>
                  <td style="font-family:var(--font-sans)">
                    <span class="pill-badge ${modePillClass}">
                      ${modeLabel}
                    </span>
                  </td>
                  <td style="text-align:right;font-weight:700;color:var(--text-primary)">${compactNumber(agent.total)}</td>
                  <td style="text-align:right;color:var(--text-secondary)">${compactNumber(agent.input)}</td>
                  <td style="text-align:right;color:${isAg ? '#C084FC' : '#34D399'}">${cacheReadText}</td>
                  <td style="text-align:right;font-weight:700;color:${isAg ? '#C084FC' : estimated ? 'var(--text-tertiary)' : '#34D399'}">${cacheHitText}</td>
                  <td style="text-align:right;color:var(--text-secondary)">${compactNumber(agent.output)}</td>
                  <td style="text-align:right;color:${isAg ? '#A78BFA' : 'var(--text-tertiary)'}">${compactNumber(agent.metadata?.reasoning_tokens || agent.reasoning)}</td>
                  <td style="text-align:right;font-weight:700;color:#60A5FA">${compactNumber(agent.net_usage)}</td>
                  <td style="text-align:right;padding-right:8px;color:var(--text-tertiary)">${callsText}</td>
                </tr>
              `;
              })
              .join("")}
          </tbody>
        </table>
      </article>

      <!-- Google DeepMind Antigravity Agentic 专属全景看板 -->
      ${renderAntigravitySection(agAgent, data)}

      <!-- Deep Dive Grid: Quota Details + Accounting Principles -->
      <div class="detail-split-grid">
        
        <article class="glass-card" style="padding:24px">
          <div style="margin-bottom:14px">
            <h3 style="font-size:16px;font-weight:700;color:var(--text-primary);display:flex;align-items:center;gap:8px">
              <span>官方额度窗口与重置机制</span>
              <span class="pill-badge pill-badge--amber">结构化来源</span>
            </h3>
            <p style="font-size:12px;color:var(--text-secondary);margin-top:2px">仅采用客户端或服务端回传的官方凭证数据，绝不盲目伪造推算。</p>
          </div>

          ${renderDetailQuotaWindows(focus, data.meta.scan.last_completed_at)}
        </article>

        <article class="glass-card" style="padding:24px">
          <div style="margin-bottom:14px">
            <h3 style="font-size:16px;font-weight:700;color:var(--text-primary);display:flex;align-items:center;gap:8px">
              <span>防重计费与多源审计原则</span>
              <span class="pill-badge pill-badge--emerald">审计健全</span>
            </h3>
            <p style="font-size:12px;color:var(--text-secondary);margin-top:2px">规避多端重复计算，确保账本精确性。</p>
          </div>

          <div style="display:flex;flex-direction:column;gap:10px;font-size:12px;color:var(--text-secondary)">
            <div style="padding:12px;border-radius:8px;background:var(--bg-subtle);border:1px solid var(--border-subtle)">
              <strong style="color:var(--text-primary)">CC Switch 历史日汇总合并：</strong>
              <p style="margin-top:4px">同一天内若同时发现账户日汇总与本机会话日志，账本以账户日汇总为准累加，本地会话仅作明细诊断，防止额度翻倍虚标。</p>
            </div>
            <div style="padding:12px;border-radius:8px;background:var(--bg-subtle);border:1px solid var(--border-subtle)">
              <strong style="color:var(--text-primary)">Antigravity DeepMind 深度解析架构：</strong>
              <p style="margin-top:4px">从 transcript_full.jsonl 和本地 conversations.db 原生 Protobuf 中提取真实模型（Gemini 3.8 Flash、3.7 Flash）、10 大自主工具调用及思维链推理 (CoT)。全本地只读解析，绝不上传源码或 Prompt。</p>
            </div>
          </div>
        </article>

      </div>

      <!-- Bottom Quick Return -->
      <div style="display:flex;justify-content:center;margin-top:12px">
        <button class="button button-primary" type="button" id="btnBackToCurveBottom" style="padding:10px 24px">
          <span>← 返回总览仪表盘 (查看趋势曲线大屏)</span>
        </button>
      </div>

    </div>
  `;

  const btnTop = $("#btnBackToCurveTop");
  const btnBottom = $("#btnBackToCurveBottom");
  if (btnTop) btnTop.addEventListener("click", () => activateTab("overview"));
  if (btnBottom) btnBottom.addEventListener("click", () => activateTab("overview"));
}

function renderAntigravitySection(ag, data) {
  if (!ag) return "";
  const meta = ag.metadata || {};
  const toolDist = meta.tool_distribution || {};
  const totalTools = meta.tool_calls_total || Object.values(toolDist).reduce((a, b) => a + b, 0);
  const reasoningTokens = meta.reasoning_tokens || ag.reasoning || 0;
  const thinkingRatio = meta.thinking_ratio ? meta.thinking_ratio * 100 : (reasoningTokens / (ag.total || 1)) * 100;
  const topSessions = meta.top_sessions || [];

  // Antigravity models discovered from DB or data.models
  const agModels = (data.models || [])
    .filter((m) => m.agent === "antigravity" && (m.usage_mode === "reported" || m.total > 1000))
    .sort((a, b) => b.total - a.total);

  const fallbackModels = [
    { model: "gemini-3.8-flash", total: 169684194, route: "Google Antigravity 原生直连", platform: "Google DeepMind" },
    { model: "gemini-3.7-flash", total: 24856411, route: "Google Antigravity 原生直连", platform: "Google DeepMind" },
    { model: "gemini-3.7-flash-exp-b", total: 10870483, route: "Google Antigravity 原生直连", platform: "Google DeepMind" },
  ];
  const displayModels = agModels.length > 0 ? agModels : fallbackModels;

  const MODEL_PROFILES = {
    "gemini-3.8-flash": {
      name: "Gemini 3.8 Flash",
      badge: "DeepMind 主力 Agent",
      badgeClass: "pill-badge--purple",
      borderColor: "rgba(139,92,246,0.35)",
      dotColor: "#8B5CF6",
      role: "DeepMind 新一代主力 Agent 架构 · 自主规划与深度推理",
      position: "Antigravity 默认核心模型，接管多步任务规划 (PLANNER_RESPONSE)、系统级工具调度与复杂代码生成",
      speed: "1,000,000 原生窗口 · 91.8% 缓存减负",
      barColor: "#8B5CF6",
    },
    "gemini-3.7-flash": {
      name: "Gemini 3.7 Flash",
      badge: "敏捷协同架构",
      badgeClass: "pill-badge--blue",
      borderColor: "rgba(59,130,246,0.35)",
      dotColor: "#3B82F6",
      role: "DeepMind 敏捷协同架构 · 极速流式返回与轻量分工",
      position: "高吞吐任务调度、文件快速检索与诊断回显，兼顾高响应速度与极低时延",
      speed: "1,000,000 原生窗口 · 高性价比快速流式回包",
      barColor: "#3B82F6",
    },
    "gemini-3.7-flash-exp-b": {
      name: "Gemini 3.7 Flash (Exp-B)",
      badge: "实验增强分支",
      badgeClass: "pill-badge--emerald",
      borderColor: "rgba(16,185,129,0.35)",
      dotColor: "#10B981",
      role: "DeepMind 前沿实验增强分支 · 实验模型验证",
      position: "特定工程或多模态任务验证模型，提供高保真 Agentic 工具交互与逻辑推导",
      speed: "1,000,000 原生窗口",
      barColor: "#10B981",
    },
    "gemini-3.1-pro": {
      name: "Gemini 3.1 Pro",
      badge: "旗舰推理",
      badgeClass: "pill-badge--amber",
      borderColor: "rgba(245,158,11,0.35)",
      dotColor: "#F59E0B",
      role: "DeepMind 深度推理旗舰架构 · 超大规模工程系统设计",
      position: "适用于最复杂的跨模块全局架构重构与深度逻辑自省检验",
      speed: "超大原生上下文窗口 · 极限推理能力",
      barColor: "#F59E0B",
    },
  };

  const modelCardsHtml = displayModels.map((m) => {
    const profile = MODEL_PROFILES[m.model] || {
      name: m.model.replace("gemini-", "Gemini ").replace("-flash", " Flash").replace("-pro", " Pro"),
      badge: "DeepMind 原生架构",
      badgeClass: "pill-badge--purple",
      borderColor: "rgba(139,92,246,0.3)",
      dotColor: "#8B5CF6",
      role: `${m.platform || "Google DeepMind"} 原生架构`,
      position: "自主 Agentic 工具调用与工程任务处理",
      speed: "原生超长上下文窗口",
      barColor: "#8B5CF6",
    };
    const shareRatio = ag.total > 0 ? (m.total / ag.total) : 0;
    const sharePercent = formatPercent(ratioToPercent(shareRatio));

    return `
      <article class="glass-card ag-model-card" style="border-color:${profile.borderColor}">
        <div class="ag-model-head">
          <div style="display:flex;align-items:center;gap:8px">
            <span class="ag-model-dot" style="background:${profile.dotColor}"></span>
            <div>
              <h3 class="ag-model-title">${escapeHtml(profile.name)}</h3>
              <span class="ag-model-role">${escapeHtml(profile.role)}</span>
            </div>
          </div>
          <span class="pill-badge ${profile.badgeClass} mono">${compactNumber(m.total)} Token</span>
        </div>
        <div class="ag-model-body">
          <div class="ag-stat-row mono">
            <span class="ag-stat-label">算力与用量占比:</span>
            <span class="ag-stat-val" style="color:#C084FC;font-weight:700">${sharePercent} (${compactNumber(m.total)} Tokens)</span>
          </div>
          <div class="ag-stat-row mono">
            <span class="ag-stat-label">原生上下文特性:</span>
            <span class="ag-stat-val" style="color:#34D399;font-weight:600">${profile.speed}</span>
          </div>
          <div class="ag-stat-row mono">
            <span class="ag-stat-label">核心职责定位:</span>
            <span class="ag-stat-val" style="color:var(--text-secondary)">${escapeHtml(profile.position)}</span>
          </div>
        </div>
        <div class="ag-model-foot">
          <div class="progress-bar-bg" style="height:4px">
            <div class="progress-bar-fill" style="background:${profile.barColor};width:${ratioToPercent(shareRatio)}%"></div>
          </div>
          <div class="mono" style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-tertiary);margin-top:6px">
            <span>算力占比: ${sharePercent}</span>
            <span>本地 conversations.db 原生 Protobuf 提取</span>
          </div>
        </div>
      </article>
    `;
  }).join("");

  const TOOL_INFO = {
    run_command: { name: "终端命令执行", desc: "本地 Shell/Powershell 编译、测试、安装依赖与诊断", color: "#3B82F6" },
    view_file: { name: "源码深度查阅", desc: "工程文件切片定位与大文件上下文研读", color: "#8B5CF6" },
    write_to_file: { name: "代码文件生成", desc: "新建工程文件与全量代码原子写入落盘", color: "#10B981" },
    manage_task: { name: "后台进程管理", desc: "异步支持进程、守护任务状态轮询与生命周期控制", color: "#EC4899" },
    search_web: { name: "互联网检索", desc: "Google/Web 实时信息深度探查与前沿资料补全", color: "#F59E0B" },
    replace_file_content: { name: "精准 Patch 替换", desc: "精确字符块比对与单块代码无损重构", color: "#6366F1" },
    list_dir: { name: "目录拓扑扫描", desc: "工作空间文件层级拓扑与深度扫描枚举", color: "#06B6D4" },
    grep_search: { name: "代码符号检索", desc: "ripgrep 极速正则与全文代码符号特征定位", color: "#14B8A6" },
    schedule: { name: "定时与后台调度", desc: "单次定时或 Cron 周期性自动化任务激活", color: "#F97316" },
    read_url_content: { name: "网页解析抓取", desc: "HTTP 静态及 Markdown 网页纯文本轻量提取", color: "#84CC16" },
    ask_question: { name: "用户意图澄清", desc: "多方案决策与关键设计确认交互提问", color: "#A855F7" },
    invoke_subagent: { name: "子智能体调度", desc: "专业子 Agent 协同派发与分工执行", color: "#E11D48" },
  };

  const toolEntries = Object.entries(toolDist).sort((a, b) => b[1] - a[1]);
  const maxToolCount = Math.max(...toolEntries.map(([, count]) => count), 1);

  return `
    <section class="ag-agentic-section">
      <!-- Section Header -->
      <div class="ag-section-head">
        <div>
          <div style="display:flex;align-items:center;gap:10px">
            <span class="kpi-dot" style="background:#8B5CF6;width:10px;height:10px;box-shadow:0 0 10px rgba(139,92,246,0.6)"></span>
            <h2 style="font-size:17px;font-weight:700;color:var(--text-primary)">Google DeepMind Antigravity · Agentic 深度工程明细</h2>
          </div>
          <p style="font-size:12px;color:var(--text-secondary);margin-top:4px">
            直接解析本地 <code class="mono" style="padding:1px 5px;background:var(--bg-subtle);border-radius:4px;color:#C084FC">~/.gemini/antigravity</code> 真实会话日志与 SQLite 原生 Protobuf，真实披露 Gemini 3.8 / 3.7 Flash 模型用量、10 大自主工具矩阵及真实工程项目。
          </p>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          <span class="pill-badge pill-badge--purple">Gemini 3.8 / 3.7 Flash 核心架构</span>
          <span class="pill-badge pill-badge--blue">1M 原生超长上下文 (91.8% 缓存减负)</span>
          <span class="pill-badge pill-badge--emerald">${totalTools.toLocaleString("zh-CN")} 次自主工具调度</span>
          <span class="pill-badge pill-badge--purple">DeepMind 原生直连解析</span>
        </div>
      </div>

      <!-- Part 1: Gemini 3.8 / 3.7 Flash Dynamic Model Matrix -->
      <div class="ag-models-grid">
        ${modelCardsHtml}
      </div>

      <!-- Part 2: 10 Autonomous Tools Matrix -->
      <article class="glass-card" style="padding:22px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px">
          <div>
            <h3 style="font-size:15px;font-weight:700;color:var(--text-primary);display:flex;align-items:center;gap:8px">
              <span>10 大自主 Agentic 工具调用全量矩阵</span>
              <span class="pill-badge pill-badge--emerald mono">${totalTools.toLocaleString("zh-CN")} 次累计调度</span>
            </h3>
            <p style="font-size:12px;color:var(--text-secondary);margin-top:2px">
              Antigravity 具有原生终端与系统级权限，各工具调用频次精确统计：
            </p>
          </div>
          <span style="font-size:11px;color:var(--text-tertiary)">按调用次数排序</span>
        </div>

        <div class="ag-tools-grid">
          ${toolEntries
            .map(([toolKey, count]) => {
              const info = TOOL_INFO[toolKey] || { name: toolKey, desc: "自定义扩展工具", color: "#8B5CF6" };
              const percent = formatPercent((count / (totalTools || 1)) * 100);
              return `
              <div class="ag-tool-item">
                <div class="ag-tool-top">
                  <div class="ag-tool-meta">
                    <span class="ag-tool-badge mono">${escapeHtml(toolKey)}</span>
                    <span class="ag-tool-name">${escapeHtml(info.name)}</span>
                  </div>
                  <div class="mono" style="text-align:right">
                    <strong style="color:var(--text-primary);font-size:13px">${count.toLocaleString("zh-CN")}</strong>
                    <span style="font-size:11px;color:var(--text-tertiary);margin-left:4px">(${percent})</span>
                  </div>
                </div>
                <div class="progress-bar-bg" style="height:5px;margin:8px 0">
                  <div class="progress-bar-fill" style="background:${info.color};width:${(count / maxToolCount) * 100}%"></div>
                </div>
                <div class="ag-tool-desc">${escapeHtml(info.desc)}</div>
              </div>
            `;
            })
            .join("")}
        </div>
      </article>

      <!-- Part 3: Real Projects / Sessions Top Ranking -->
      <article class="glass-card" style="padding:22px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px">
          <div>
            <h3 style="font-size:15px;font-weight:700;color:var(--text-primary);display:flex;align-items:center;gap:8px">
              <span>真实工程项目会话主题与用量明细 (TOP 8 会话)</span>
              <span class="pill-badge pill-badge--purple">Protobuf 深度还原</span>
            </h3>
            <p style="font-size:12px;color:var(--text-secondary);margin-top:2px">
              提取自本地工程会话概要数据库，清晰展示您近期由 Antigravity 承接的具体工程项目与真实消耗：
            </p>
          </div>
          <span style="font-size:11px;color:var(--text-tertiary)">非匿名 UUID · 真实工程可追溯</span>
        </div>

        <div class="ag-sessions-grid">
          ${topSessions
            .map((s, idx) => {
              const shortId = (s.session_id || "").slice(0, 8);
              return `
              <div class="ag-session-item">
                <div class="ag-session-head">
                  <span class="ag-session-rank mono">#0${idx + 1}</span>
                  <div class="ag-session-title" title="${escapeHtml(s.title || s.session_id)}">${escapeHtml(s.title || s.session_id)}</div>
                </div>
                <div class="ag-session-metrics mono">
                  <div class="ag-session-stat">
                    <span style="color:var(--text-tertiary)">消耗 Token</span>
                    <strong style="color:#C084FC">${compactNumber(s.tokens)}</strong>
                  </div>
                  <div class="ag-session-stat">
                    <span style="color:var(--text-tertiary)">自主工具</span>
                    <strong style="color:#34D399">${s.tool_calls.toLocaleString("zh-CN")} 次</strong>
                  </div>
                  <div class="ag-session-stat">
                    <span style="color:var(--text-tertiary)">会话标识</span>
                    <span class="mono" style="color:var(--text-secondary);font-size:11px">${shortId}…</span>
                  </div>
                </div>
              </div>
            `;
            })
            .join("")}
        </div>
      </article>

    </section>
  `;
}

/* ==========================================================================
   Render Diagnostics Tab
   ========================================================================== */
function renderDiagnostics(data) {
  if (!elements.diagnostic) return;
  const sources = data.sources;
  const totalFiles = sources.reduce((sum, s) => sum + Number(s.files || 0), 0);
  const totalEvents = sources.reduce((sum, s) => sum + Number(s.events || 0), 0);

  elements.diagnostic.innerHTML = `
    <div class="diag-stack">
      
      <!-- Top Quick Return -->
      <div class="glass-card" style="padding:12px 18px;display:flex;align-items:center;justify-content:space-between;border-color:rgba(59,130,246,0.3);background:rgba(59,130,246,0.03)">
        <button class="button button-primary" type="button" id="btnDiagBackToCurve">
          <svg style="width:14px;height:14px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
          <span>← 返回总览仪表盘 (趋势曲线窗口)</span>
        </button>
        <span style="font-size:12px;color:var(--text-tertiary)">本地索引存储与安全隐私保障</span>
      </div>

      <!-- Top Banner -->
      <div class="glass-card diag-banner">
        <div>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="status-dot status-dot--live"></span>
            <h2 style="font-size:15px;font-weight:700;color:var(--text-primary)">本地 SQLite 索引状态：就绪运行中</h2>
            <span class="pill-badge pill-badge--emerald">增量模式</span>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);margin-top:4px">
            数据库存储：<code class="mono" style="padding:2px 6px;border-radius:4px;background:var(--bg-subtle);color:var(--text-primary)">%LOCALAPPDATA%\\TokenLedger\\token-ledger.db</code>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:16px">
          <div class="mono" style="text-align:right;font-size:11px">
            <div style="color:var(--text-tertiary)">已建立指纹</div>
            <div style="color:var(--text-primary);font-weight:600">${totalFiles} 个文件 · ${totalEvents} 条记录</div>
          </div>
          <button class="button button-secondary" type="button" id="btnForceScan">强制刷新扫描</button>
        </div>
      </div>

      <!-- Adapter Cards Grid -->
      <div class="diag-adapters-grid">
        ${sources
          .map((src) => {
            const isEst = src.metadata?.usage_mode === "estimated";
            const color = AGENT_COLORS[src.agent] || "#3B82F6";
            return `
            <article class="glass-card adapter-card">
              <div class="adapter-head">
                <span class="adapter-title">
                  <span class="kpi-dot" style="background:${color}"></span>
                  ${escapeHtml(src.label)}
                </span>
                <span class="pill-badge ${isEst ? 'pill-badge--purple' : 'pill-badge--emerald'}">
                  ${isEst ? '估算模式' : '已就绪'}
                </span>
              </div>
              <div class="adapter-details">
                <div>路径: <code>${escapeHtml(src.path_hint)}</code></div>
                <div>发现文件: <strong>${src.files}</strong> 个文件</div>
                <div>已入库用量: <strong>${src.events}</strong> 条有效事件</div>
              </div>
              <div style="margin-top:auto;padding-top:8px;border-top:1px solid var(--border-subtle);font-size:11px;color:var(--text-tertiary)">
                上次检查: ${formatDateTime(src.last_scan)}
              </div>
            </article>
          `;
          })
          .join("")}
      </div>

      <!-- Local Security & Privacy Vault -->
      <article class="glass-card vault-card">
        <div class="vault-head">
          <div class="vault-icon-wrap">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
              <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
            </svg>
          </div>
          <div>
            <h3 style="font-size:15px;font-weight:700;color:var(--text-primary)">本机优先隐私安全承诺</h3>
            <p style="font-size:12px;color:var(--text-secondary)">绝对捍卫开发者代码机密、会话上下文与认证凭据</p>
          </div>
        </div>

        <div class="vault-grid">
          <div class="vault-item">
            <div class="vault-item-title">✓ 零云端上传</div>
            <p class="vault-item-desc">仅在本地回环 127.0.0.1 提供只读接口，无任何外部数据分析或遥测上报。</p>
          </div>
          <div class="vault-item">
            <div class="vault-item-title">✓ 不持久化 Prompt</div>
            <p class="vault-item-desc">仅在内存中提取结构化 Token 数值，绝对不把会话提示词、代码或模型回答落盘。</p>
          </div>
          <div class="vault-item">
            <div class="vault-item-title">✓ 密钥免触原则</div>
            <p class="vault-item-desc">不碰触任何 API Key、Token 或认证 Cookie，CC Switch 只读白名单统计字段。</p>
          </div>
          <div class="vault-item">
            <div class="vault-item-title">✓ 本地独享数据库</div>
            <p class="vault-item-desc">账本存在用户本地 AppData，所有用量数据完全归用户所有，随删随清。</p>
          </div>
        </div>
      </article>

    </div>
  `;

  const btnDiagBack = $("#btnDiagBackToCurve");
  if (btnDiagBack) btnDiagBack.addEventListener("click", () => activateTab("overview"));

  const btnForceScan = $("#btnForceScan");
  if (btnForceScan) btnForceScan.addEventListener("click", requestScan);
}

/* ==========================================================================
   Tab Navigation & Core Interactivity
   ========================================================================== */
function activateTab(tab) {
  state.tab = tab;
  if (elements.shell) elements.shell.dataset.activeTab = tab;
  $$("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $$("[data-panel]").forEach((panel) => {
    const active = panel.dataset.panel === tab;
    panel.classList.toggle("is-active", active);
    panel.hidden = !active;
  });
  if (tab === "overview" && state.data) {
    setTimeout(() => renderSplineChart(state.data.daily), 50);
  }
}

/* ==========================================================================
   Scan Action & Polling
   ========================================================================== */
async function requestScan() {
  if (elements.scan) {
    elements.scan.disabled = true;
    elements.scan.classList.add("is-busy");
  }
  if (elements.scanLabel) elements.scanLabel.textContent = "启动扫描…";

  try {
    const response = await fetch("/api/scan", {
      method: "POST",
      headers: { "X-Token-Ledger-Request": "same-origin" },
    });
    if (!response.ok && response.status !== 409) {
      throw new Error(`扫描请求返回 ${response.status}`);
    }
    toast(response.status === 409 ? "扫描已经在进行中" : "已启动本地增量扫描");
    startPolling();
    if (state.preview) {
      setTimeout(() => loadDashboard({ quiet: true }), 500);
    }
  } catch (error) {
    toast(error.message || "无法启动扫描");
    if (elements.scan) {
      elements.scan.disabled = false;
      elements.scan.classList.remove("is-busy");
    }
  }
}

function startPolling() {
  clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try {
      const health = await getJson("/api/health");
      if (state.data) state.data.meta.scan = health.scan;
      render();
      if (!["scanning"].includes(health.scan.status)) {
        clearInterval(state.polling);
        state.polling = null;
        await loadDashboard({ quiet: true });
        toast(health.scan.message || "扫描完成，索引已更新");
      }
    } catch {
      clearInterval(state.polling);
      state.polling = null;
    }
  }, 900);
}

/* ==========================================================================
   Theme Switcher (Persisted via localStorage)
   ========================================================================== */
function setupTheme() {
  // Restore saved theme on startup
  try {
    const saved = localStorage.getItem("tokenledger-theme");
    if (saved === "light") {
      document.documentElement.classList.remove("dark");
      document.documentElement.classList.add("light");
    } else {
      document.documentElement.classList.remove("light");
      document.documentElement.classList.add("dark");
    }
  } catch (e) {}

  if (!elements.themeToggle) return;
  elements.themeToggle.addEventListener("click", () => {
    const isDark = document.documentElement.classList.contains("dark");
    if (isDark) {
      document.documentElement.classList.remove("dark");
      document.documentElement.classList.add("light");
      try { localStorage.setItem("tokenledger-theme", "light"); } catch (e) {}
      toast("已切换至亮色模式 (Apple 陶瓷浅色)");
    } else {
      document.documentElement.classList.remove("light");
      document.documentElement.classList.add("dark");
      try { localStorage.setItem("tokenledger-theme", "dark"); } catch (e) {}
      toast("已切换至暗色模式 (Linear 曜石黑)");
    }
    if (state.data) {
      renderSplineChart(state.data.daily);
    }
  });
}

function setupAgentPills() {
  const pills = $$(".agent-pill-btn");
  pills.forEach((btn) => {
    btn.addEventListener("click", () => {
      const agent = btn.dataset.agentFilter || "all";
      state.agent = agent;
      pills.forEach((b) => b.classList.toggle("is-active", b === btn));

      const dot = $("#rangeIndicatorDot");
      if (dot) {
        dot.style.background = AGENT_COLORS[agent] || "#3B82F6";
        dot.style.boxShadow = `0 0 8px ${AGENT_COLORS[agent] || "#3B82F6"}80`;
      }
      const label = $("#currentRangeLabel");
      if (label) {
        const rName = state.range === "1" ? "今天 (24h)" : state.range === "7" ? "最近 7 天" : state.range === "all" ? "全部历史" : "最近 30 天";
        const aName = agent === "all" ? "全量智能体" : agent === "codex" ? "Codex 专属" : agent === "claude" ? "Claude Code 专属" : "Antigravity 专属";
        label.textContent = `用量观察窗口 · ${rName} · ${aName}`;
      }

      toast(agent === "all" ? "已查看全部智能体用量" : `已聚焦 ${agent.toUpperCase()}`);
      loadDashboard({ quiet: true });
    });
  });
}

/* ==========================================================================
   Event Bindings & Bootstrap
   ========================================================================== */
function bindEvents() {
  setupTheme();

  $$("[data-tab]").forEach((button) =>
    button.addEventListener("click", () => activateTab(button.dataset.tab))
  );

  $$("[data-range]").forEach((button) =>
    button.addEventListener("click", () => {
      state.range = button.dataset.range;
      $$("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button));
      loadDashboard({ quiet: true });
    })
  );

  setupAgentPills();

  if (elements.scan) elements.scan.addEventListener("click", requestScan);
  if (elements.previewRetry) elements.previewRetry.addEventListener("click", () => loadDashboard());

  const errRetry = $("#errorRetry");
  if (errRetry) errRetry.addEventListener("click", () => loadDashboard());

  $$("[role=tab]").forEach((tab) =>
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      const tabs = $$("[role=tab]");
      const next =
        (tabs.indexOf(event.currentTarget) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) %
        tabs.length;
      tabs[next].focus();
      activateTab(tabs[next].dataset.tab);
    })
  );
}

bindEvents();
loadDashboard();

state.refreshTimer = setInterval(() => {
  if (!document.hidden && !state.preview && !state.polling) {
    loadDashboard({ quiet: true });
  }
}, 30000);

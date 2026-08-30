"use strict";

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
  connection: $("#connectionStatus"),
  connectionLabel: $("#connectionLabel"),
  preview: $("#previewNotice"),
  previewRetry: $("#previewRetry"),
  agentFilter: $("#agentFilter"),
  overview: $("#overviewContent"),
  detail: $("#detailContent"),
  diagnostic: $("#diagnosticContent"),
  heroTotal: $("#heroTotal"),
  heroTotalMeta: $("#heroTotalMeta"),
  heroRange: $("#heroRangeLabel"),
  heroUpdated: $("#heroUpdatedLabel"),
  toolbarMeta: $("#toolbarMeta"),
  footer: $("#footerStatus"),
  toast: $("#toast"),
  announcer: $("#liveAnnouncer"),
};

const AGENT_COLORS = { codex: "#146EF5", claude: "#F07040", antigravity: "#7857FF" };

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeColor(value, fallback = "#146EF5") {
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
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function formatReset(value) {
  if (!value) return "未提供重置时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未提供重置时间";
  return `${formatDateTime(value)} 重置`;
}

function statusText(status) {
  return ({ ready: "可用", fresh: "最新", limited: "字段受限", empty: "暂无数据", missing: "未发现", pending: "待扫描", stale: "已过期", unavailable: "未提供", partial: "部分可用", scanning: "扫描中" })[status] || status || "未知";
}

function statusDot(status) {
  if (["ready", "fresh"].includes(status)) return "status-dot--live";
  if (["limited", "partial", "stale", "empty"].includes(status)) return "status-dot--preview";
  if (["error", "missing"].includes(status)) return "status-dot--error";
  return "status-dot--loading";
}

function rangeText(range) {
  return ({ "1": "今天", "7": "最近 7 天", "30": "最近 30 天", all: "全部历史" })[String(range)] || `最近 ${range} 天`;
}

function announce(message) {
  elements.announcer.textContent = message;
}

let toastTimer;
function toast(message) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  toastTimer = setTimeout(() => { elements.toast.hidden = true; }, 2800);
}

function mockDashboard() {
  const today = new Date();
  const daily = Array.from({ length: 30 }, (_, index) => {
    const date = new Date(today);
    date.setDate(today.getDate() - (29 - index));
    const total = index % 6 === 0 ? (index + 3) * 3200000 : (index % 4) * 740000;
    return { date: date.toISOString().slice(0, 10), total, input: total * 0.96, cached_input: total * 0.82, output: total * 0.04 };
  });
  const agents = [
    { id: "codex", name: "Codex", short: "CX", color: "#146EF5", status: "ready", total: 184000000, input: 179000000, cached_input: 151000000, output: 5000000, reasoning: 920000, cache_hit_rate: 0.844, sessions: 29, calls: 612, models: 3, quota: { status: "fresh", label: "5 小时窗口", remaining_percent: 68, resets_at: new Date(Date.now() + 7200000).toISOString(), message: "预览额度" } },
    { id: "claude", name: "Claude Code", short: "CL", color: "#F07040", status: "ready", total: 72000000, input: 69900000, cached_input: 58400000, output: 2100000, reasoning: 0, cache_hit_rate: 0.835, sessions: 12, calls: 188, models: 2, quota: { status: "unavailable", label: "官方额度未提供", remaining_percent: null, message: "预览状态" } },
    { id: "antigravity", name: "Antigravity", short: "AG", color: "#7857FF", status: "limited", total: 11800000, reported_total: 0, estimated_total: 11800000, contains_estimates: true, input: 8100000, cached_input: 0, output: 3700000, reasoning: 1200000, cache_hit_rate: 0, sessions: 8, calls: 132, models: 1, metadata: { usage_mode: "estimated" }, quota: { status: "unavailable", label: "官方额度未提供", remaining_percent: null, message: "本地可见文本估算不包含官方额度" } },
  ];
  const total = agents.reduce((sum, item) => sum + item.total, 0);
  return {
    meta: { generated_at: new Date().toISOString(), range: { start: daily[0].date, end: daily.at(-1).date, days: 30 }, scan: { status: "ready", progress: 100, message: "预览数据", last_completed_at: new Date().toISOString() }, timezone: "Asia/Shanghai", selected_agent: "all" },
    summary: { total, reported_total: total - 11800000, estimated_total: 11800000, contains_estimates: true, input: 257000000, cached_input: 209400000, cache_write: 3100000, output: 10800000, reasoning: 2120000, non_cached_input: 47600000, net_usage: 58400000, cache_hit_rate: 0.815, sessions: 49, calls: 932 },
    lifetime: {
      summary: { total, reported_total: total - 11800000, estimated_total: 11800000, contains_estimates: true, input: 257000000, cached_input: 209400000, cache_write: 3100000, output: 10800000, reasoning: 2120000, non_cached_input: 47600000, net_usage: 58400000, cache_hit_rate: 0.815, sessions: 49, calls: 932 },
      agents: Object.fromEntries(agents.map((agent) => [agent.id, agent])),
    },
    daily,
    agents,
    models: [
      { agent: "codex", route: "原生订阅", platform: "OpenAI", model: "gpt-5.6-sol", total: 132000000, share: 0.516 },
      { agent: "claude", route: "CC Switch", platform: "阿里云百炼", model: "qwen-coder", total: 72000000, share: 0.281 },
      { agent: "codex", route: "原生订阅", platform: "OpenAI", model: "gpt-5.6-luna", total: 52000000, share: 0.203 },
      { agent: "antigravity", route: "本地可见文本估算", platform: "Google Antigravity", model: "模型未记录（估算）", total: 11800000, share: 0.044, usage_mode: "estimated" },
    ],
    sources: agents.map((agent, index) => ({ agent: agent.id, status: agent.status, label: `${agent.name} 本地数据`, path_hint: index === 2 ? "~/.gemini/antigravity/brain" : `~/.${agent.id}/sessions`, files: 12 + index, events: agent.calls, sessions: agent.sessions, last_scan: new Date().toISOString(), message: agent.metadata?.usage_mode === "estimated" ? "可见文本估算可用" : "结构化字段可用", metadata: agent.metadata || {} })),
  };
}

async function getJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.ok) throw new Error(`本地接口返回 ${response.status}`);
  return response.json();
}

async function loadDashboard({ quiet = false } = {}) {
  if (!quiet) setLoading(true);
  try {
    const query = new URLSearchParams({ days: state.range, agent: state.agent });
    state.data = await getJson(`/api/dashboard?${query}`);
    state.preview = false;
    elements.preview.hidden = true;
    setConnection("live", "本机数据已连接");
    try { state.diagnostics = await getJson("/api/diagnostics"); } catch { state.diagnostics = null; }
  } catch (error) {
    if (!state.data || !quiet) state.data = mockDashboard();
    state.preview = true;
    elements.preview.hidden = false;
    setConnection("preview", "本地接口未连接 · 预览");
    elements.errorMessage.textContent = error.message || "无法读取本地接口";
  } finally {
    setLoading(false);
    render();
    if (!state.preview && state.data?.meta?.scan?.status === "scanning" && !state.polling) startPolling();
  }
}

function setLoading(loading) {
  state.loading = loading;
  elements.loading.hidden = !loading;
  elements.main.setAttribute("aria-busy", String(loading));
  elements.shell.dataset.viewState = loading ? "loading" : "ready";
}

function setConnection(type, label) {
  elements.connectionLabel.textContent = label;
  const dot = elements.connection.querySelector(".status-dot");
  dot.className = `status-dot status-dot--${type}`;
}

function render() {
  if (!state.data) return;
  const { data } = state;
  const lifetime = data.lifetime?.summary || data.summary;
  elements.heroTotal.textContent = compactNumber(lifetime.total);
  elements.heroTotal.title = Number(lifetime.total || 0).toLocaleString("zh-CN");
  const estimateMeta = lifetime.contains_estimates ? ` · 含估算 ${compactNumber(lifetime.estimated_total)}` : "";
  const lifetimeSessionLabel = lifetime.session_count_complete === false ? "个已知会话" : "个会话";
  elements.heroTotalMeta.textContent = `${lifetime.sessions.toLocaleString("zh-CN")} ${lifetimeSessionLabel} · ${lifetime.calls.toLocaleString("zh-CN")} 次请求/用量记录${estimateMeta}`;
  elements.heroRange.textContent = `当前范围：${rangeText(state.range)}`;
  elements.heroUpdated.textContent = formatDateTime(data.meta.generated_at);
  elements.toolbarMeta.textContent = `${data.meta.range.start} → ${data.meta.range.end} · ${formatDateTime(data.meta.scan.last_completed_at)}`;
  elements.footer.textContent = state.preview ? "预览数据 · 非真实用量" : `${statusText(data.meta.scan.status)} · ${formatDateTime(data.meta.scan.last_completed_at)}`;
  elements.scan.disabled = data.meta.scan.status === "scanning" || state.preview;
  elements.scan.classList.toggle("is-busy", data.meta.scan.status === "scanning");
  elements.scanLabel.textContent = data.meta.scan.status === "scanning" ? `扫描 ${data.meta.scan.progress || 0}%` : "刷新本机数据";
  populateAgents(data.agents);
  renderOverview(data);
  renderDetail(data);
  renderDiagnostics(data);
}

function populateAgents(agents) {
  const current = elements.agentFilter.value || state.agent;
  elements.agentFilter.innerHTML = `<option value="all">全部智能体</option>${agents.map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name)}</option>`).join("")}`;
  elements.agentFilter.value = agents.some((agent) => agent.id === current) ? current : "all";
}

function flowStage(className, label, value, meta) {
  return `<div class="flow-stage flow-stage--${className}"><span class="flow-stage-label">${escapeHtml(label)}</span><span class="flow-node" aria-hidden="true"></span><strong class="flow-stage-value mono" title="${Number(value || 0).toLocaleString("zh-CN")}">${compactNumber(value)}</strong><span class="flow-stage-meta">${escapeHtml(meta)}</span></div>`;
}

function renderOverview(data) {
  const summary = data.summary;
  elements.overview.innerHTML = `
    <div class="section-stack">
      <article class="panel panel--dark flow-panel">
        <div class="panel-head"><div><p class="panel-kicker">当前范围账本</p><h2 class="panel-title">Token 流量轨道</h2><p class="panel-description">累计总量在页首；这里仅展示 ${escapeHtml(rangeText(state.range))}。输入包含缓存读取，净用量 = 非缓存输入 + 输出。${summary.contains_estimates ? `其中 ${compactNumber(summary.estimated_total)} 为本地估算。` : ""}</p></div><span class="panel-note">${escapeHtml(data.meta.range.start)} → ${escapeHtml(data.meta.range.end)}</span></div>
        <div class="flow-summary"><div class="flow-total-block"><span class="flow-total-label">范围总量</span><div class="flow-total-value mono">${compactNumber(summary.total)}</div><div class="flow-total-sub">${Number(summary.total || 0).toLocaleString("zh-CN")} Token</div></div><div class="flow-summary-side"><div class="summary-inline summary-inline--cache"><span class="summary-inline-label">缓存命中</span><strong class="summary-inline-value">${formatPercent(ratioToPercent(summary.cache_hit_rate))}</strong></div><div class="summary-inline"><span class="summary-inline-label">${summary.session_count_complete === false ? "已知会话" : "会话"}</span><strong class="summary-inline-value">${summary.sessions.toLocaleString("zh-CN")}</strong></div><div class="summary-inline"><span class="summary-inline-label">请求/记录</span><strong class="summary-inline-value">${summary.calls.toLocaleString("zh-CN")}</strong></div></div></div>
        <div class="flow-rail-scroll"><div class="flow-rail">
          ${flowStage("input", "输入", summary.input, "包含缓存读取")}
          ${flowStage("cache", "缓存读取", summary.cached_input, `${formatPercent(ratioToPercent(summary.cache_hit_rate))} 命中`)}
          ${flowStage("output", "输出", summary.output, `推理输出 ${compactNumber(summary.reasoning)}`)}
          ${flowStage("net", "净用量", summary.net_usage, "非缓存输入 + 输出")}
        </div><div class="flow-formula"><strong>净用量</strong><span>=</span><span>输入</span><span class="formula-arrow">−</span><span>缓存读取</span><span class="formula-arrow">+</span><span>输出</span></div></div>
      </article>
      <div class="overview-grid">
        ${trendPanel(data.daily)}
        ${rankingPanel(data.models, data.agents)}
      </div>
      <section><div class="subsection-head"><h2>智能体账本</h2><span>范围用量 / 累计 / 缓存 / 额度</span></div><div class="agent-grid">${data.agents.map((agent) => agentCard(agent, { ...(data.lifetime?.agents?.[agent.id] || {}), reconciliation: data.lifetime?.reconciliation?.[agent.id] || {} })).join("")}</div></section>
      ${sourceStrip(data)}
    </div>`;
  $$('[data-open-agent]').forEach((button) => button.addEventListener("click", () => {
    state.agent = button.dataset.openAgent;
    elements.agentFilter.value = state.agent;
    activateTab("detail");
    loadDashboard({ quiet: true });
  }));
}

function trendPanel(daily) {
  const positive = daily.some((item) => Number(item.total) > 0);
  const chart = positive ? buildTrendSvg(daily) : `<div class="chart-empty"><span>当前范围没有结构化 token 事件</span></div>`;
  return `<article class="panel trend-panel"><div class="panel-head"><div><p class="panel-kicker">每日脉冲</p><h2 class="panel-title">用量趋势</h2><p class="panel-description">按本地日期归集，空白日期保留为零。</p></div><span class="panel-note">${daily.length} 天</span></div>${chart}<div class="chart-caption"><span class="chart-legend"><span class="legend-line"></span>总 Token</span><span>峰值 ${compactNumber(Math.max(0, ...daily.map((item) => Number(item.total))))}</span></div></article>`;
}

function buildTrendSvg(daily) {
  const width = 820, height = 265, left = 42, right = 12, top = 14, bottom = 31;
  const max = Math.max(1, ...daily.map((item) => Number(item.total || 0)));
  const usableWidth = width - left - right, usableHeight = height - top - bottom;
  const points = daily.map((item, index) => ({
    x: left + (daily.length === 1 ? usableWidth / 2 : (index / (daily.length - 1)) * usableWidth),
    y: top + usableHeight - (Number(item.total || 0) / max) * usableHeight,
    ...item,
  }));
  const line = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const area = `${line} L${points.at(-1).x.toFixed(1)},${height - bottom} L${points[0].x.toFixed(1)},${height - bottom} Z`;
  const grid = [0, .5, 1].map((ratio) => {
    const y = top + usableHeight * ratio;
    const label = compactNumber(max * (1 - ratio));
    return `<line class="chart-grid-line" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"></line><text x="0" y="${y + 3}">${escapeHtml(label)}</text>`;
  }).join("");
  const labelEvery = Math.max(Math.ceil(points.length / 5), 1);
  const labels = points.map((point, index) => index % labelEvery === 0 || index === points.length - 1 ? `<text class="chart-axis-label" text-anchor="middle" x="${point.x}" y="${height - 7}">${escapeHtml(point.date.slice(5))}</text>` : "").join("");
  const dots = points.filter((point) => Number(point.total) > 0).map((point) => `<circle class="chart-dot" cx="${point.x}" cy="${point.y}" r="3"><title>${escapeHtml(point.date)} · ${Number(point.total).toLocaleString("zh-CN")}</title></circle>`).join("");
  return `<div class="chart-wrap"><svg class="trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="每日 Token 趋势">${grid}<path class="chart-area" d="${area}"></path><path class="chart-line" d="${line}"></path>${dots}${labels}</svg></div>`;
}

function rankingPanel(models, agents) {
  const colors = Object.fromEntries(agents.map((agent) => [agent.id, safeColor(agent.color, AGENT_COLORS[agent.id])]));
  const rows = models.slice(0, 8).map((model) => `
    <div class="model-row">
      <div class="model-main"><div class="model-name" title="${escapeHtml(model.model)}">${escapeHtml(model.model)}</div><div class="model-route"><span class="agent-color-dot" style="background:${colors[model.agent] || "#8d95a2"}"></span>${escapeHtml(model.route)} · ${escapeHtml(model.platform)}</div></div>
      <div class="model-track" role="progressbar" aria-label="${escapeHtml(model.model)} 占比" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${ratioToPercent(model.share) ?? 0}"><span style="--bar-size:${ratioToPercent(model.share) ?? 0}%;--bar-color:${colors[model.agent] || "#8d95a2"}"></span></div>
      <span class="model-share" title="${Number(model.total || 0).toLocaleString("zh-CN")}"><strong>${compactNumber(model.total)}</strong><small>${formatPercent(ratioToPercent(model.share))}</small></span>
    </div>`).join("");
  return `<article class="panel ranking-panel"><div class="panel-head"><div><p class="panel-kicker">模型账本</p><h2 class="panel-title">按模型</h2><p class="panel-description">平台只在日志或 CC Switch 账本可证明时归属；估算记录会明确标注。</p></div><span class="panel-note">前 ${Math.min(models.length, 8)} 项</span></div><div class="ranking-list">${rows || '<div class="model-empty">当前范围没有模型用量</div>'}</div></article>`;
}

function quotaChip(quota) {
  if (!quota || quota.status === "unavailable") return `<span class="quota-chip quota-chip--unavailable">官方额度未提供</span>`;
  const kind = quota.status === "budget" ? "budget" : "official";
  const value = quota.remaining_percent == null ? quota.label : `${quota.label} ${formatPercent(absolutePercent(quota.remaining_percent))}`;
  return `<span class="quota-chip quota-chip--${kind}" title="${escapeHtml(quota.message || "")}">${escapeHtml(value)}</span>`;
}

function accountRollupChip(agent, lifetime = {}) {
  const reconciliation = agent?.reconciliation?.has_account_rollup ? agent.reconciliation : lifetime?.reconciliation;
  if (!reconciliation?.has_account_rollup) return "";
  return `<span class="account-chip" title="账户日汇总优先，同日会话日志已自动去重">账户汇总 ${Number(reconciliation.account_days || 0).toLocaleString("zh-CN")} 天</span>`;
}

function agentCard(agent, lifetime = agent) {
  const color = safeColor(agent.color, AGENT_COLORS[agent.id]);
  if (agent.usage_available === false) {
    return `<article class="agent-card agent-card--limited" style="--agent-color:${color}"><div class="agent-card-top"><div class="agent-card-title"><span class="agent-color-dot" style="background:${color}"></span><h3>${escapeHtml(agent.name)}</h3></div><span class="agent-status"><span class="status-dot ${statusDot(agent.status)}"></span>${escapeHtml(statusText(agent.status))}</span></div><div class="agent-total agent-total--unavailable">Token 未提供</div><div class="agent-total-label">本地日志没有结构化用量字段</div><div class="agent-lifetime"><span>活动记录</span><strong>${Number(agent.activity_count || 0).toLocaleString("zh-CN")}</strong></div><div class="agent-card-metrics"><div class="agent-metric"><span class="agent-metric-label">会话</span><strong class="agent-metric-value">${agent.sessions.toLocaleString("zh-CN")}</strong></div><div class="agent-metric"><span class="agent-metric-label">模型</span><strong class="agent-metric-value">未提供</strong></div></div><p class="agent-limit-note">保留活动与会话统计，不伪造缺失字段。</p><div class="agent-card-bottom"><span class="quota-chip quota-chip--unavailable">额度未提供</span><button class="link-button" type="button" data-open-agent="${escapeHtml(agent.id)}">查看来源 →</button></div></article>`;
  }
  const cacheWidth = ratioToPercent(agent.cache_hit_rate) ?? 0;
  const estimated = agent.contains_estimates || agent.metadata?.usage_mode === "estimated";
  const hasAccountRollup = Boolean(agent.reconciliation?.has_account_rollup || lifetime?.reconciliation?.has_account_rollup);
  const sessionLabel = agent.session_count_complete === false ? "已知会话" : "会话";
  const cacheBlock = estimated
    ? `<div class="agent-cache agent-cache--unavailable"><div class="agent-cache-label"><span>缓存命中</span><strong>日志未提供</strong></div><div class="agent-cache-track agent-cache-track--unknown" aria-label="缓存命中数据未提供"></div></div>`
    : `<div class="agent-cache"><div class="agent-cache-label"><span>缓存命中</span><strong>${formatPercent(cacheWidth)}</strong></div><div class="agent-cache-track" role="progressbar" aria-label="${escapeHtml(agent.name)} 缓存命中率" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${cacheWidth}"><span style="--cache-size:${cacheWidth}%"></span></div></div>`;
  return `<article class="agent-card ${estimated ? "agent-card--estimated" : ""}" style="--agent-color:${color}"><div class="agent-card-top"><div class="agent-card-title"><span class="agent-color-dot" style="background:${color}"></span><h3>${escapeHtml(agent.name)}</h3></div><span class="agent-status"><span class="status-dot ${statusDot(agent.status)}"></span>${estimated ? "本地估算" : hasAccountRollup ? "历史账户汇总已合并" : escapeHtml(statusText(agent.status))}</span></div><div class="agent-total" title="${Number(agent.total || 0).toLocaleString("zh-CN")}">${compactNumber(agent.total)}</div><div class="agent-total-label">${escapeHtml(rangeText(state.range))} · ${estimated ? "可见文本估算" : agent.reconciliation?.has_account_rollup ? "账户汇总 + 本机会话" : "总 Token"}</div><div class="agent-lifetime"><span>累计</span><strong title="${Number(lifetime?.total || 0).toLocaleString("zh-CN")}">${compactNumber(lifetime?.total || 0)}</strong></div><div class="agent-card-metrics"><div class="agent-metric"><span class="agent-metric-label">输出</span><strong class="agent-metric-value">${compactNumber(agent.output)}</strong></div><div class="agent-metric"><span class="agent-metric-label">${sessionLabel}</span><strong class="agent-metric-value">${agent.sessions.toLocaleString("zh-CN")}</strong></div></div>${cacheBlock}<div class="agent-card-bottom"><div class="chip-group">${accountRollupChip(agent, lifetime)}${quotaChip(agent.quota)}</div><button class="link-button" type="button" data-open-agent="${escapeHtml(agent.id)}">查看明细 →</button></div></article>`;
}

function sourceStrip(data) {
  const reported = data.sources.filter((source) => source.metadata?.usage_mode !== "estimated" && source.status === "ready").length;
  const estimated = data.sources.filter((source) => source.metadata?.usage_mode === "estimated").length;
  return `<div class="source-strip"><span class="source-strip-mark" aria-hidden="true"><svg viewBox="0 0 20 20"><path d="M4 4.5h12v11H4zM7 8h6M7 11h4" fill="none" stroke="currentColor" stroke-linecap="round" stroke-width="1.5"/></svg></span><div><p class="source-strip-title">${reported} 个结构化用量源${estimated ? ` · ${estimated} 个本地估算源` : ""}</p><p class="source-strip-copy">估算值按可见文本计算并单独标记，不等同于官方计费 Token。</p></div><span class="source-strip-meta">${escapeHtml(data.meta.scan.message || "等待扫描")}</span></div>`;
}

function renderDetail(data) {
  const selected = state.agent === "all" ? data.agents : data.agents.filter((agent) => agent.id === state.agent);
  const cards = selected.map((agent) => {
    const color = safeColor(agent.color, AGENT_COLORS[agent.id]);
    const lifetime = data.lifetime?.agents?.[agent.id] || agent;
    if (agent.usage_available === false) {
      return `<article class="detail-agent-card detail-agent-card--limited" style="--agent-color:${color}"><div class="detail-agent-head"><div><h3 class="detail-agent-name">${escapeHtml(agent.name)}</h3><div class="detail-agent-id">仅有活动记录 · Token 字段缺失</div></div><span class="status-chip"><span class="status-dot ${statusDot(agent.status)}"></span>${escapeHtml(statusText(agent.status))}</span></div><div class="detail-total detail-total--unavailable">Token 未提供</div><div class="detail-total-caption">不会伪造缺失字段</div><div class="metric-table">${metricTextRow("本机会话", agent.sessions.toLocaleString("zh-CN"))}${metricTextRow("活动记录", Number(agent.activity_count || 0).toLocaleString("zh-CN"))}${metricTextRow("模型字段", "未提供")}${metricTextRow("额度字段", "未提供")}</div><div class="detail-agent-footer"><span class="quota-chip quota-chip--unavailable">活动数据可用</span><span>${agent.sessions} 个会话</span></div></article>`;
    }
    const estimated = agent.contains_estimates || agent.metadata?.usage_mode === "estimated";
    const reconciliation = agent.reconciliation || {};
    const lifetimeReconciliation = data.lifetime?.reconciliation?.[agent.id] || {};
    const shownReconciliation = reconciliation.has_account_rollup ? reconciliation : lifetimeReconciliation;
    const rollupMeta = shownReconciliation.has_account_rollup ? ` · 累计含 ${Number(shownReconciliation.account_days || 0).toLocaleString("zh-CN")} 天账户汇总` : "";
    const rollupMetrics = shownReconciliation.has_account_rollup ? `${metricTextRow("账户汇总请求", Number(shownReconciliation.account_calls || 0).toLocaleString("zh-CN"))}${metricTextRow("已去重会话记录", Number(shownReconciliation.suppressed_session_events || 0).toLocaleString("zh-CN"))}` : "";
    const sessionLabel = agent.session_count_complete === false ? "个已知会话" : "个会话";
    return `<article class="detail-agent-card ${estimated ? "detail-agent-card--estimated" : ""}" style="--agent-color:${color}"><div class="detail-agent-head"><div><h3 class="detail-agent-name">${escapeHtml(agent.name)}</h3><div class="detail-agent-id">${estimated ? "本地可见文本估算" : `${agent.models} 个模型${rollupMeta}`}</div></div><span class="status-chip"><span class="status-dot ${statusDot(agent.status)}"></span>${estimated ? "估算值" : shownReconciliation.has_account_rollup ? "历史账户汇总已合并" : escapeHtml(statusText(agent.status))}</span></div><div class="detail-total">${compactNumber(agent.total)}</div><div class="detail-total-caption">${escapeHtml(rangeText(state.range))} · 累计 ${compactNumber(lifetime.total)}</div><div class="metric-table">${metricRow("输入", agent.input)}${estimated ? metricTextRow("缓存读取", "日志未提供") : metricRow("缓存读取", agent.cached_input)}${metricRow("输出", agent.output)}${metricRow("推理输出", agent.reasoning)}${metricRow("净用量", agent.net_usage)}${rollupMetrics}</div><div class="detail-agent-footer"><div class="chip-group">${accountRollupChip(agent, { reconciliation: lifetimeReconciliation })}${estimated ? '<span class="quota-chip quota-chip--budget">可见文本估算</span>' : `<span class="cache-chip">缓存命中 ${formatPercent(ratioToPercent(agent.cache_hit_rate))}</span>`}</div><span>${agent.sessions} ${sessionLabel}</span></div></article>`;
  }).join("");
  const focus = selected[0] || data.agents[0];
  const models = data.models.filter((model) => state.agent === "all" || model.agent === state.agent);
  const single = selected.length === 1;
  const lifetimeRollups = data.lifetime?.reconciliation || {};
  const rollupAgent = selected.find((agent) => lifetimeRollups[agent.id]?.has_account_rollup);
  const selectedRollup = rollupAgent ? lifetimeRollups[rollupAgent.id] : null;
  const rollupSummary = rollupAgent?.metadata?.account_rollup;
  const rollupPolicy = selectedRollup ? `${metricTextRow("CC Switch 历史", "账户日汇总优先，同日会话不重复累加")}${metricTextRow("汇总覆盖", `${rollupSummary?.start_date || "未知"} 至 ${rollupSummary?.end_date || "未知"}；范围外缺失不等于零`)}` : "";
  elements.detail.innerHTML = `<div class="section-stack section-stack--compact"><div class="detail-intro"><div><p class="panel-kicker">智能体明细</p><h2>${escapeHtml(state.agent === "all" ? "三个智能体，一套统一口径" : focus.name)}</h2><p>输入、缓存读取、输出与推理输出分开核对；估算值明确标记，缺失字段保持为空。</p></div><span class="detail-intro-note">${escapeHtml(rangeText(state.range))}</span></div><div class="detail-workbench ${single ? "detail-workbench--single" : "detail-workbench--all"}"><div class="detail-agent-list ${single ? "detail-agent-list--single" : ""}">${cards}</div><div class="detail-quota-slot">${quotaPanel(focus)}</div></div><div class="detail-models"><article class="panel"><div class="panel-head"><div><p class="panel-kicker">路线 / 平台 / 模型</p><h2 class="panel-title">模型与路线</h2></div></div><div class="agent-model-list">${models.slice(0, 12).map((model) => modelDetailRow(model, data.agents)).join("") || '<div class="agent-empty">当前范围没有模型记录</div>'}</div></article><article class="panel"><div class="panel-head"><div><p class="panel-kicker">统计说明</p><h2 class="panel-title">核算口径</h2></div></div><div class="metric-table">${metricTextRow("总量", "结构化日志与已标记估算之和")}${metricTextRow("净用量", "非缓存输入 + 输出")}${metricTextRow("缓存命中", "缓存读取 / 输入")}${metricTextRow("平台归属", "仅采用可证明映射")}${rollupPolicy}${metricTextRow("Antigravity", "可见文本估算，不等同官方计费")}</div></article></div></div>`;
}

function metricRow(label, value) {
  return `<div class="metric-row"><span class="metric-row-label">${escapeHtml(label)}</span><strong class="metric-row-value" title="${Number(value || 0).toLocaleString("zh-CN")}">${compactNumber(value)}</strong></div>`;
}

function metricTextRow(label, value) {
  return `<div class="metric-row"><span class="metric-row-label">${escapeHtml(label)}</span><strong class="metric-row-value">${escapeHtml(value)}</strong></div>`;
}

function quotaPanel(agent) {
  const quota = agent?.quota;
  const available = quota && quota.remaining_percent != null && quota.status !== "unavailable";
  const remaining = available ? absolutePercent(quota.remaining_percent) : null;
  const meter = available ? `<div class="quota-bar-wrap"><div class="quota-bar-label"><span>剩余额度</span><strong>${formatPercent(remaining)}</strong></div><div class="quota-bar" role="progressbar" aria-label="剩余额度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${remaining}"><span style="--quota-size:${remaining}%;--quota-color:${remaining < 20 ? "#F07040" : "#29B77B"}"></span></div></div>` : `<div class="quota-unavailable"><strong>没有百分比数据</strong><span>该智能体未提供可验证的官方额度窗口</span></div>`;
  return `<article class="panel quota-panel"><div><p class="panel-kicker">额度来源</p><h3 class="quota-panel-title">${escapeHtml(quota?.label || "官方额度未提供")}</h3><p class="quota-panel-copy">${available ? "服务商或本地预算记录给出的比例" : "Token 统计仍可正常使用"}</p></div>${meter}<div class="quota-meta"><span class="quota-meta-label">重置 / 更新时间</span><strong class="quota-meta-value">${available ? escapeHtml(formatReset(quota.resets_at)) : "没有可靠服务端来源"}</strong><span class="quota-meta-value">${escapeHtml(formatDateTime(quota?.updated_at))}</span></div><p class="quota-message">${escapeHtml(quota?.message || "该智能体没有提供可验证的官方额度字段。")}</p></article>`;
}

function modelDetailRow(model, agents) {
  const agent = agents.find((item) => item.id === model.agent);
  const color = safeColor(agent?.color, AGENT_COLORS[model.agent]);
  const share = ratioToPercent(model.share) ?? 0;
  return `<div class="agent-model-row" style="--agent-color:${color}"><div><div class="model-name">${escapeHtml(model.model)}</div><div class="agent-model-platform">${escapeHtml(model.route)} → ${escapeHtml(model.platform)}</div></div><div class="agent-model-track" role="progressbar" aria-label="${escapeHtml(model.model)} 占比" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${share}"><span style="--bar-size:${share}%"></span></div><span class="agent-model-value"><strong>${compactNumber(model.total)}</strong><small>${formatPercent(share)}</small></span></div>`;
}

function renderDiagnostics(data) {
  const sources = data.sources;
  const privacy = state.diagnostics?.privacy || ["不保存提示词与回复", "不读取或记录密钥", "只绑定 127.0.0.1", "不发送第三方遥测"];
  const totalFiles = sources.reduce((sum, source) => sum + Number(source.files || 0), 0);
  const totalEvents = sources.reduce((sum, source) => sum + Number(source.events || 0), 0);
  const scanProgress = absolutePercent(data.meta.scan.progress) ?? 0;
  elements.diagnostic.innerHTML = `<div class="section-stack"><div class="diagnostic-intro"><div><p class="panel-kicker">数据源健康度</p><h2>数据从哪里来，哪里不可用。</h2><p>诊断页只展示路径模式、文件数和解析状态，不展示对话内容或凭据。</p></div><span class="detail-intro-note">${escapeHtml(formatDateTime(data.meta.scan.last_completed_at))}</span></div><div class="diagnostic-summary"><div class="diagnostic-stat"><span class="diagnostic-stat-label">数据源</span><strong class="diagnostic-stat-value">${sources.length}</strong><span class="diagnostic-stat-copy">已注册适配器</span></div><div class="diagnostic-stat"><span class="diagnostic-stat-label">文件</span><strong class="diagnostic-stat-value">${totalFiles.toLocaleString("zh-CN")}</strong><span class="diagnostic-stat-copy">已建立指纹</span></div><div class="diagnostic-stat"><span class="diagnostic-stat-label">用量记录</span><strong class="diagnostic-stat-value">${totalEvents.toLocaleString("zh-CN")}</strong><span class="diagnostic-stat-copy">结构化与估算记录</span></div></div><div class="diagnostic-layout"><article class="panel"><div class="panel-head"><div><p class="panel-kicker">本地数据源</p><h2 class="panel-title">扫描状态</h2></div><span class="panel-note">只读</span></div><div class="source-list">${sources.map(sourceRow).join("") || '<div class="source-empty">等待首次扫描</div>'}</div></article><aside class="diagnostic-side"><article class="diagnostic-card diagnostic-card--dark"><h3>隐私边界</h3><p class="diagnostic-card-intro">索引只保留统计必要字段。</p><div class="diagnostic-list">${privacy.map((item) => `<div class="diagnostic-list-row"><span class="diagnostic-list-label">边界</span><strong class="diagnostic-list-value">${escapeHtml(item)}</strong></div>`).join("")}</div><span class="privacy-badge">仅限本机</span></article><article class="diagnostic-card"><h3>扫描进度</h3><p class="diagnostic-card-intro">文件未变化时不会重复解析。</p><div class="scan-progress"><div class="scan-progress-head"><span>${escapeHtml(statusText(data.meta.scan.status))}</span><strong class="scan-progress-value">${formatPercent(scanProgress, 0)}</strong></div><div class="scan-progress-track" role="progressbar" aria-label="扫描进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${scanProgress}"><span style="--progress-size:${scanProgress}%"></span></div><p class="scan-progress-copy">${escapeHtml(data.meta.scan.message || "等待扫描")}</p></div></article></aside></div></div>`;
}

function sourceRow(source) {
  const activityOnly = source.metadata?.usage_available === false;
  const observedCount = activityOnly ? Number(source.metadata?.activity_count || 0) : Number(source.events || 0);
  const observedLabel = activityOnly ? "活动" : "事件";
  return `<div class="source-row"><div class="source-name"><span class="source-dot ${statusDot(source.status)}"></span><span class="source-name-copy"><strong class="source-label">${escapeHtml(source.label)}</strong><span class="source-agent">${escapeHtml(source.agent)} / ${escapeHtml(statusText(source.status))}</span></span></div><div class="source-path"><code>${escapeHtml(source.path_hint)}</code><span class="source-message">${escapeHtml(source.message)}</span></div><div class="source-counts"><span>${Number(source.files || 0).toLocaleString("zh-CN")} <small>文件</small></span><span>${observedCount.toLocaleString("zh-CN")} <small>${observedLabel}</small></span></div></div>`;
}

function activateTab(tab) {
  state.tab = tab;
  elements.shell.dataset.activeTab = tab;
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
}

async function requestScan() {
  if (state.preview) return;
  elements.scan.disabled = true;
  elements.scan.classList.add("is-busy");
  elements.scanLabel.textContent = "启动扫描…";
  try {
    const response = await fetch("/api/scan", { method: "POST", headers: { "X-Token-Ledger-Request": "same-origin" } });
    if (!response.ok && response.status !== 409) throw new Error(`扫描请求返回 ${response.status}`);
    toast(response.status === 409 ? "扫描已经在进行" : "已开始增量扫描");
    startPolling();
  } catch (error) {
    toast(error.message || "无法启动扫描");
    elements.scan.disabled = false;
    elements.scan.classList.remove("is-busy");
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
        toast(health.scan.message || "扫描完成");
      }
    } catch {
      clearInterval(state.polling);
      state.polling = null;
    }
  }, 900);
}

function bindEvents() {
  $$("[data-tab]").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab)));
  $$("[data-range]").forEach((button) => button.addEventListener("click", () => {
    state.range = button.dataset.range;
    $$("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button));
    loadDashboard();
  }));
  elements.agentFilter.addEventListener("change", () => { state.agent = elements.agentFilter.value; loadDashboard(); });
  elements.scan.addEventListener("click", requestScan);
  elements.previewRetry.addEventListener("click", () => loadDashboard());
  $("#errorRetry").addEventListener("click", () => loadDashboard());
  $$("[role=tab]").forEach((tab) => tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const tabs = $$("[role=tab]");
    const next = (tabs.indexOf(event.currentTarget) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    activateTab(tabs[next].dataset.tab);
  }));
}

bindEvents();
loadDashboard();
state.refreshTimer = setInterval(() => {
  if (!document.hidden && !state.preview && !state.polling) loadDashboard({ quiet: true });
}, 30000);

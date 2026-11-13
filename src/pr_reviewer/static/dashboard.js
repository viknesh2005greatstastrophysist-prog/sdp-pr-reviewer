const state = {
  mode: "demo",
  result: null,
  status: null,
  severity: "ALL",
  busy: false,
  demoCases: [],
  selectedCase: "full-spectrum",
  auth: null,
  repos: null,
  reposError: "",
  selectedRepo: "",
  pulls: null,
  job: null,
  pollToken: 0,
  elapsedTimer: null,
  jobs: null,
};

const SEVERITY_ORDER = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4 };

const workflowStages = [
  { id: "ingest", icon: "GH", title: "PR ingestion", short: "GitHub metadata and diff", description: "Retrieves pull-request metadata, the unified diff and changed file contents using GitHub REST or the fixed demo fixture.", input: "PR reference or trusted demo", safeguard: "GitHub App/token auth; bounded payloads", output: "Metadata, head SHA and raw diff" },
  { id: "parse", icon: "Δ", title: "Diff mapping", short: "Changed-line coordinates", description: "Parses unified diff hunks and maps old lines, new lines and GitHub-compatible diff positions.", input: "Unified diff text", safeguard: "Path normalization and added-line filtering", output: "Typed DiffFile and ChangedLine models" },
  { id: "context", icon: "AST", title: "AST context", short: "Functions and classes", description: "Enriches changed Python lines with their enclosing function, class and source range to ground later reasoning.", input: "Materialized changed files", safeguard: "Parsing only; pull-request code is never imported", output: "Per-line CodeContext evidence" },
  { id: "static", icon: "R+S", title: "Static analysis", short: "Ruff and Semgrep", description: "Runs deterministic Ruff and local-rule Semgrep scans, retaining diagnostics only when they point to added lines.", input: "Isolated changed-file checkout", safeguard: "No shell, cache disabled, hard timeout", output: "Rule-backed findings with confidence 1.0" },
  { id: "agents", icon: "4×", title: "Agent fan-out", short: "Four specialists", description: "Runs Bug Hunter, Performance, Style/Syntax and Architecture/Scalability concurrently through LangGraph.", input: "Diff plus AST context", safeguard: "Structured Pydantic output and per-agent timeout", output: "Fault-isolated specialist findings" },
  { id: "aggregate", icon: "⇲", title: "Consolidation", short: "Deduplicate and rank", description: "Merges static and agent findings, removes overlapping reports and orders evidence by severity and confidence.", input: "All analyzer and agent findings", safeguard: "Changed-line validation and deterministic ranking", output: "One consolidated review" },
  { id: "suggest", icon: "</>", title: "Refactoring", short: "Validated suggestions", description: "Produces minimal replacement ranges and validates that Python suggestions parse before formatting GitHub suggestion blocks.", input: "Consolidated findings", safeguard: "Added lines only; raw code; syntax validation", output: "GitHub-compatible code suggestions" },
  { id: "publish", icon: "↗", title: "Publish & persist", short: "Comment, history, evaluation", description: "Stores review history, creates an executive summary and can publish advisory inline comments when explicitly enabled.", input: "ReviewResult and PR head SHA", safeguard: "COMMENT only; never approve or merge", output: "PostgreSQL history and GitHub review" },
];

const agentDefinitions = [
  { id: "bug-hunter", name: "Bug Hunter", copy: "Correctness, security and concurrency", color: "bug" },
  { id: "performance", name: "Performance", copy: "Complexity, memory and I/O", color: "performance" },
  { id: "style", name: "Style / Syntax", copy: "Typing, clarity and API use", color: "style" },
  { id: "architecture", name: "Architecture", copy: "Boundaries, scale and reliability", color: "architecture" },
];

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
})[character]);

function errorText(payload, status) {
  const detail = payload && payload.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
      return where ? `${where}: ${item.msg}` : String(item.msg || item);
    }).join("; ");
  }
  if (status === 401) return "Sign in with GitHub first";
  if (status === 404) return "Not found";
  return status ? `Request failed (HTTP ${status})` : "Request failed";
}

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { credentials: "same-origin", ...options });
  } catch (error) {
    const failure = new Error("Cannot reach the PRISM server. Check that it is running and try again.");
    failure.network = true;
    throw failure;
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const failure = new Error(errorText(payload, response.status));
    failure.status = response.status;
    throw failure;
  }
  return payload;
}

function parseTime(value) {
  if (!value) return null;
  const text = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
  const time = Date.parse(text);
  return Number.isNaN(time) ? null : time;
}

function formatDuration(ms) {
  if (ms == null || ms < 0) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

function timeAgo(value) {
  const time = parseTime(value);
  if (time == null) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function formatDate(value) {
  const time = parseTime(value);
  return time == null ? "—" : new Date(time).toLocaleString();
}

function setView(view) {
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  const titles = { overview: "Review command center", workflow: "Pipeline workflow", findings: "Review findings", history: "Review history", configuration: "Runtime configuration" };
  $("#pageTitle").textContent = titles[view] || "PRISM";
  window.location.hash = view;
  if (view === "history") loadJobs();
}

function notify(message, type = "info") {
  const node = $("#notice");
  node.textContent = message;
  node.className = `notice ${type === "error" ? "error" : type === "success" ? "success" : ""}`;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => node.classList.add("hidden"), 8000);
}

function setMode(mode) {
  state.mode = mode;
  $("#demoToggle").classList.toggle("active", mode === "demo");
  $("#liveToggle").classList.toggle("active", mode === "live");
  $("#demoToggle").setAttribute("aria-pressed", String(mode === "demo"));
  $("#liveToggle").setAttribute("aria-pressed", String(mode === "live"));
  $("#demoControls").classList.toggle("hidden", mode !== "demo");
  $("#liveControls").classList.toggle("hidden", mode !== "live");
  $("#modeChip").textContent = mode === "demo" ? "SAFE SANDBOX" : "GITHUB REST API";
  updateLauncher();
  if (mode === "live") renderLive();
}

function selectedDemo() {
  return state.demoCases.find((item) => item.case_id === state.selectedCase);
}

function updateLauncher() {
  if (state.mode === "live") {
    $("#launcherTitle").textContent = "Review a live GitHub pull request";
    $("#launcherCopy").textContent = "Fetch a real pull request, run the complete evidence-grounded pipeline and persist its review history.";
    return;
  }
  const demo = selectedDemo();
  $("#launcherTitle").textContent = demo ? `Run: ${demo.title}` : "Run the complete demo review";
  $("#launcherCopy").textContent = demo ? demo.subtitle : "Choose a trusted case to exercise the local review pipeline.";
  if (demo) {
    $("#demoFileIcon").textContent = demo.icon;
    $("#demoFileName").textContent = demo.filename;
    $("#demoFileMeta").textContent = `${demo.line_count} added lines · ${demo.coverage.join(" · ")}`;
  }
}

function renderDemoCases() {
  const colors = { coral: "var(--coral)", amber: "var(--amber)", cyan: "var(--cyan)", violet: "var(--violet)", green: "var(--green)" };
  const picker = $("#demoCasePicker");
  picker.innerHTML = "";
  state.demoCases.forEach((demo) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `demo-case ${demo.case_id === state.selectedCase ? "active" : ""}`;
    button.style.setProperty("--case-color", colors[demo.accent] || "var(--teal)");
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(demo.case_id === state.selectedCase));
    button.title = demo.subtitle;
    button.innerHTML = `<b>${escapeHtml(demo.icon)}</b><strong>${escapeHtml(demo.title)}</strong><small>${escapeHtml(demo.coverage[0])}</small>`;
    button.addEventListener("click", () => {
      state.selectedCase = demo.case_id;
      renderDemoCases();
      updateLauncher();
    });
    picker.appendChild(button);
  });
  updateLauncher();
}

async function loadDemoCases() {
  const response = await fetch("/dashboard/demo-cases");
  if (!response.ok) throw new Error("Demo cases are unavailable");
  state.demoCases = await response.json();
  if (!state.demoCases.some((item) => item.case_id === state.selectedCase)) {
    state.selectedCase = state.demoCases[0]?.case_id || "full-spectrum";
  }
  renderDemoCases();
}

function renderPipeline(active = -1, failed = false) {
  const container = $("#pipeline");
  container.innerHTML = "";
  workflowStages.forEach((stage, index) => {
    const item = document.createElement("div");
    item.className = "pipeline-step";
    if (index < active || active === workflowStages.length) item.classList.add("done");
    if (index === active) item.classList.add(failed ? "failed" : "active");
    item.innerHTML = `<div class="pipeline-node">${stage.icon}</div><small>${stage.title}</small>`;
    container.appendChild(item);
  });
}

function renderWorkflow() {
  const map = $("#workflowMap");
  map.innerHTML = "";
  workflowStages.forEach((stage, index) => {
    const button = document.createElement("button");
    button.className = `workflow-card ${index === 0 ? "active" : ""}`;
    button.innerHTML = `<span class="workflow-number">STAGE ${String(index + 1).padStart(2, "0")}</span><h3>${stage.title}</h3><p>${stage.short}</p>`;
    button.addEventListener("click", () => {
      $$(".workflow-card", map).forEach((card) => card.classList.remove("active"));
      button.classList.add("active");
      renderWorkflowDetail(stage);
    });
    map.appendChild(button);
  });
  renderWorkflowDetail(workflowStages[0]);
}

function renderWorkflowDetail(stage) {
  $("#workflowDetail").innerHTML = `<div class="detail-icon">${stage.icon}</div><h3>${stage.title}</h3><p>${stage.description}</p><ul class="detail-list"><li><strong>INPUT</strong><span>${stage.input}</span></li><li><strong>SAFEGUARD</strong><span>${stage.safeguard}</span></li><li><strong>OUTPUT</strong><span>${stage.output}</span></li></ul>`;
}

function renderAgents(runs = []) {
  const container = $("#agentGrid");
  container.innerHTML = "";
  agentDefinitions.forEach((definition) => {
    const run = runs.find((item) => item.agent === definition.id);
    const card = document.createElement("article");
    card.className = `agent-card ${run ? run.status : ""}`;
    card.innerHTML = `<div class="agent-card-head"><div><span class="agent-dot ${definition.color}"></span><h4>${definition.name}</h4></div><span class="agent-status">${run ? run.status.replace("_", " ") : "standing by"}</span></div><p>${definition.copy}</p><div class="agent-stats"><span>${run ? `${run.finding_count} findings` : "No run"}</span><span>${run ? `${run.duration_ms} ms` : "—"}</span></div>`;
    container.appendChild(card);
  });
}

function renderReadiness(status) {
  const checks = [
    ["GitHub connection", status.github_connected, status.github_connected ? "server credentials ready" : "required for live mode"],
    ["LLM specialists", status.llm_connected, status.llm_connected ? "connected" : "offline heuristics"],
    ["Ruff analyzer", status.analyzers.ruff, "deterministic"],
    ["Semgrep analyzer", status.analyzers.semgrep, "security rules"],
    ["GitHub sign-in", status.oauth_configured, status.oauth_configured ? "configured" : "not configured"],
    ["Review publishing", status.publishing_enabled, status.publishing_enabled ? "enabled" : "safe / disabled"],
  ];
  const essential = [status.analyzers.ruff, status.analyzers.semgrep, status.database, status.github_connected || status.oauth_configured, status.llm_connected];
  const score = Math.round((essential.filter(Boolean).length / essential.length) * 100);
  $("#readinessScore").textContent = `${score}%`;
  $("#readinessList").innerHTML = checks.map(([name, ok, note]) => `<div class="readiness-item"><span>${name}</span><span class="${ok ? "ok" : "missing"}">${ok ? "●" : "○"} ${note}</span></div>`).join("");
}

function renderConfiguration(status) {
  const cards = [
    ["GH", "GitHub", status.github_connected, status.github_connected ? "Authenticated for live ingestion" : "Add GitHub token or App credentials", "GITHUB_TOKEN / GITHUB_APP_ID"],
    ["AI", "LLM provider", status.llm_connected, status.llm_connected ? "Structured specialist outputs enabled" : "Using deterministic offline agents", "LLM_API_KEY"],
    ["DB", "Review history", true, `${status.database.toUpperCase()} database configured`, "DATABASE_URL"],
    ["R", "Ruff", status.analyzers.ruff, "Python quality and security diagnostics", "ruff check"],
    ["S", "Semgrep", status.analyzers.semgrep, "Local deterministic security rules", "semgrep scan"],
    ["WH", "GitHub webhook", status.webhook_configured, status.webhook_configured ? "Signature validation configured" : "Add a webhook signing secret", "GITHUB_WEBHOOK_SECRET"],
    ["↗", "Publishing permission", status.publishing_enabled, status.publishing_enabled ? "Server allows advisory COMMENT reviews, only when ticked per review" : "Disabled: reviews can never be posted to GitHub", "PUBLISH_REVIEWS"],
    ["WP", "Webhook auto-publish", status.webhook_auto_publish, status.webhook_auto_publish ? "Webhook reviews are posted automatically" : "Off: webhook reviews are stored, not posted", "WEBHOOK_AUTO_PUBLISH"],
    ["ID", "GitHub sign-in (OAuth)", status.oauth_configured, status.oauth_configured ? "Users sign in to pick repositories" : "Not configured: live reviews use server credentials", "GITHUB_OAUTH_CLIENT_ID / _SECRET"],
    ["LM", "Model", Boolean(status.llm_model), status.llm_model ? `Using ${status.llm_model}` : "No model: offline heuristics", "LLM_MODEL"],
    ["≥", "Minimum confidence", true, `Findings below ${formatPercent(status.min_confidence)} are dropped`, "MIN_CONFIDENCE"],
    ["∥", "Review concurrency", true, `${status.review_concurrency ?? "?"} review job${status.review_concurrency === 1 ? "" : "s"} at a time`, "REVIEW_CONCURRENCY"],
    ["4×", "Agent graph", true, `${status.agents.length} concurrent specialists`, "LangGraph fan-out / fan-in"],
    ["EV", "Evaluation", true, "Ready for held-out labelled JSONL", "pr-reviewer-evaluate"],
  ];
  $("#configGrid").innerHTML = cards.map(([icon, name, ok, copy, code]) => `<article class="surface config-card"><div class="config-card-head"><span class="config-card-icon">${icon}</span><span class="config-state ${ok ? "ok" : ""}">${ok ? "READY" : "OPTIONAL"}</span></div><h3>${name}</h3><p>${copy}</p><code>${code}</code></article>`).join("");
}

function formatPercent(value) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
}

async function loadStatus() {
  try {
    state.status = await api("/dashboard/status");
    $("#engineState").textContent = `${state.status.database} · API ready`;
    renderReadiness(state.status);
    renderConfiguration(state.status);
    renderLive();
  } catch (error) {
    $("#engineState").textContent = "Connection failed";
    notify(error.message, "error");
  }
}

function activityEntries(result) {
  const entries = result.demo_case ? [["Scenario selected", result.demo_case.title]] : [];
  entries.push(
    ["Diff parsed", `${result.files.length} changed file${result.files.length === 1 ? "" : "s"} mapped`],
    ["Context extracted", "Changed lines grounded in AST symbols"],
    ["Static evidence collected", `${result.findings.filter((f) => ["ruff", "semgrep"].includes(f.source)).length} deterministic matches`],
    ["Specialists consolidated", `${result.agent_runs.filter((run) => run.status === "success").length}/${result.agent_runs.length} agents completed`],
    ["Review assembled", `${result.findings.length} deduplicated findings ranked`],
  );
  return entries;
}

function renderResult(result) {
  state.result = result;
  result.files = result.files || [];
  result.findings = result.findings || [];
  result.warnings = result.warnings || [];
  result.agent_runs = result.agent_runs || [];
  const additions = result.files.reduce((sum, file) => sum + (file.lines || []).filter((line) => line.kind === "ADD").length, 0);
  const elevated = result.findings.filter((finding) => ["CRITICAL", "HIGH"].includes(finding.severity)).length;
  const fixes = result.findings.filter((finding) => finding.suggestion && finding.suggestion.validated).length;
  $("#filesMetric").textContent = result.files.length;
  $("#linesMetric").textContent = `${additions} added lines mapped`;
  $("#findingsMetric").textContent = result.findings.length;
  $("#severityMetric").textContent = `${elevated} high-priority`;
  $("#agentsMetric").textContent = `${result.agent_runs.filter((run) => run.status === "success").length}/${result.agent_runs.length}`;
  $("#fixesMetric").textContent = fixes;
  $("#findingBadge").textContent = result.findings.length;
  renderAgents(result.agent_runs);
  renderFindings();
  $("#activityFeed").innerHTML = activityEntries(result).map(([title, copy]) => `<div class="activity-item"><i></i><div><strong>${title}</strong><small>${copy}</small></div></div>`).join("");
  renderMeta(result);
}

function renderMeta(result) {
  const box = $("#reviewMeta");
  if (!result) { box.classList.add("hidden"); return; }
  const parts = [];
  if (result.summary) parts.push(`<div><span class="section-kicker">SUMMARY</span><p class="meta-summary">${escapeHtml(result.summary)}</p></div>`);
  if (result.head_sha) parts.push(`<p class="meta-line">Reviewed commit <code>${escapeHtml(result.head_sha.slice(0, 10))}</code></p>`);
  if (result.published) parts.push(`<p class="meta-line ok">Advisory comments were posted to GitHub as a COMMENT review.</p>`);
  else if (result.publish_error) parts.push(`<p class="meta-line bad">Posting to GitHub failed: ${escapeHtml(result.publish_error)}</p>`);
  if (result.warnings.length) parts.push(`<div class="meta-warnings"><span class="section-kicker">WARNINGS</span><ul>${result.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul></div>`);
  box.innerHTML = parts.join("");
  box.classList.toggle("hidden", !parts.length);
}

function renderFindings() {
  const container = $("#findingsList");
  container.innerHTML = "";
  if (!state.result) {
    container.innerHTML = `<div class="empty-state"><span>◇</span><h3>No review loaded</h3><p>Run the demo or connect a live pull request to inspect findings.</p></div>`;
    return;
  }
  const findings = state.result.findings
    .filter((finding) => state.severity === "ALL" || finding.severity === state.severity)
    .map((finding, index) => ({ finding, index }))
    .sort((a, b) => (SEVERITY_ORDER[a.finding.severity] ?? 9) - (SEVERITY_ORDER[b.finding.severity] ?? 9) || (b.finding.confidence || 0) - (a.finding.confidence || 0) || a.index - b.index)
    .map((entry) => entry.finding);
  if (!findings.length) {
    container.innerHTML = `<div class="empty-state"><span>✓</span><h3>No ${state.severity.toLowerCase()} findings</h3><p>The current review has no evidence in this severity band.</p></div>`;
    return;
  }
  findings.forEach((finding) => {
    const fragment = $("#findingTemplate").content.cloneNode(true);
    const card = $(".finding-card", fragment);
    const pill = $(".severity-pill", fragment);
    pill.textContent = finding.severity;
    pill.classList.add(finding.severity.toLowerCase());
    $(".finding-source", fragment).textContent = finding.source;
    $(".confidence", fragment).textContent = typeof finding.confidence === "number" ? `${Math.round(finding.confidence * 100)}% confidence` : "confidence n/a";
    $("h3", fragment).textContent = finding.title;
    $(".finding-message", fragment).textContent = finding.message;
    $(".code-location code", fragment).textContent = `${finding.path}:${finding.line}${finding.end_line ? `–${finding.end_line}` : ""}${finding.rule_id ? ` · ${finding.rule_id}` : ""}`;
    $(".evidence-block p", fragment).textContent = finding.evidence || "";
    if (finding.suggestion) {
      const suggestion = $(".suggestion-block", fragment);
      suggestion.classList.remove("hidden");
      $("code", suggestion).textContent = finding.suggestion.replacement;
      const badge = $(".validated-badge", suggestion);
      badge.textContent = finding.suggestion.validated ? "✓ validated" : "not validated";
      badge.classList.toggle("ok", Boolean(finding.suggestion.validated));
      $("p", suggestion).textContent = finding.suggestion.rationale || "";
    }
    container.appendChild(card);
  });
}

function setBusy(busy) {
  state.busy = busy;
  $$("#reviewForm button[type=submit], .js-live-action").forEach((button) => { button.disabled = busy; });
  $("#runState").className = `run-state ${busy ? "running" : state.result ? "complete" : "idle"}`;
  $("#runState").innerHTML = `<i></i>${busy ? "Analyzing" : state.result ? "Complete" : "Ready"}`;
  renderPublishControl();
}

async function runReview() {
  if (state.busy) return;
  if (state.mode === "live") { submitLive(manualReference()); return; }
  setBusy(true);
  let stage = 0;
  renderPipeline(stage);
  const ticker = setInterval(() => { stage = Math.min(stage + 1, workflowStages.length - 1); renderPipeline(stage); }, 650);
  try {
    const payload = await api("/dashboard/demo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ case_id: state.selectedCase }) });
    clearInterval(ticker);
    renderPipeline(workflowStages.length);
    renderResult(payload);
    notify(`${payload.demo_case ? payload.demo_case.title : "Demo"} demo completed through the local pipeline.`);
  } catch (error) {
    clearInterval(ticker);
    renderPipeline(stage, true);
    notify(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function rowAttrs(kind, id) {
  return `tabindex="0" role="button" data-${kind}="${escapeHtml(id)}" aria-label="Open ${kind} ${escapeHtml(id)} in Findings"`;
}

async function loadHistory(repository) {
  const [owner, repo] = repository.split("/");
  const rows = $("#historyRows");
  rows.innerHTML = `<tr><td colspan="6" class="table-empty">Loading review history…</td></tr>`;
  try {
    const data = await api(`/history/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`);
    rows.innerHTML = data.length ? data.map((item) => `<tr class="click-row" ${rowAttrs("review", item.id)}><td>#${escapeHtml(item.id)}</td><td>${escapeHtml(item.repository)}</td><td>#${escapeHtml(item.pull_number)}</td><td><code>${escapeHtml(item.head_sha ? item.head_sha.slice(0, 8) : "—")}</code></td><td>${escapeHtml(item.finding_count)}</td><td>${escapeHtml(formatDate(item.created_at))}</td></tr>`).join("") : `<tr><td colspan="6" class="table-empty">No reviews are stored for this repository yet.</td></tr>`;
  } catch (error) {
    rows.innerHTML = `<tr><td colspan="6" class="table-empty">${escapeHtml(error.message)}</td></tr>`;
  }
}

async function loadJobs() {
  const rows = $("#jobRows");
  if (!state.jobs) rows.innerHTML = `<tr><td colspan="7" class="table-empty">Loading jobs…</td></tr>`;
  try {
    state.jobs = await api("/jobs?limit=25");
    rows.innerHTML = state.jobs.length ? state.jobs.map((job) => {
      const start = parseTime(job.started_at);
      const end = parseTime(job.finished_at);
      const done = job.status === "completed";
      return `<tr class="click-row${done ? "" : " static"}" ${done ? rowAttrs("job", job.id) : ""}><td><span class="status-pill ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span></td><td>${escapeHtml(job.repository)}</td><td>#${escapeHtml(job.pull_number)}</td><td>${escapeHtml(job.trigger)}</td><td>${job.status === "completed" ? escapeHtml(job.finding_count ?? 0) : "—"}</td><td>${escapeHtml(formatDate(job.started_at || job.created_at))}</td><td>${start != null && end != null ? formatDuration(end - start) : "—"}</td></tr>`;
    }).join("") : `<tr><td colspan="7" class="table-empty">No review jobs yet. Start a live review and it will appear here.</td></tr>`;
  } catch (error) {
    state.jobs = null;
    rows.innerHTML = `<tr><td colspan="7" class="table-empty">${escapeHtml(error.message)}</td></tr>`;
  }
}

async function openStored(kind, id) {
  try {
    const record = await api(kind === "job" ? `/jobs/${encodeURIComponent(id)}` : `/reviews/${encodeURIComponent(id)}`);
    if (!record.result) throw new Error("This review has no stored result to show.");
    renderResult(record.result);
    setView("findings");
    notify(`Loaded ${record.repository} #${record.pull_number} (${record.result.findings.length} finding${record.result.findings.length === 1 ? "" : "s"}).`, "success");
  } catch (error) {
    notify(error.message, "error");
  }
}

// Account ------------------------------------------------------------------------------------
async function loadAuth() {
  try {
    state.auth = await api("/auth/status");
  } catch (error) {
    state.auth = { oauth_configured: false, password_login_enabled: false, authenticated: false, user: null, failed: true };
    notify(`Could not check sign-in state: ${error.message}`, "error");
  }
  renderAccount();
  renderLive();
}

function renderAccount() {
  const area = $("#accountArea");
  const auth = state.auth;
  if (!auth) { area.innerHTML = ""; return; }
  if (auth.authenticated && auth.user) {
    const user = auth.user;
    const avatar = /^https:\/\//.test(user.avatar_url || "") ? `<img class="user-avatar" src="${escapeHtml(user.avatar_url)}" alt="" width="30" height="30" referrerpolicy="no-referrer">` : `<span class="user-avatar fallback" aria-hidden="true">${escapeHtml((user.login || "?").slice(0, 2).toUpperCase())}</span>`;
    const badge = user.auth_method === "password" ? `<span class="method-badge" title="Signed in with a demo account">Demo</span>` : "";
    area.innerHTML = `${avatar}<span class="user-login" title="${escapeHtml(user.name || user.login)}">${escapeHtml(user.login)}</span>${badge}<button type="button" class="secondary-button small" id="signOutButton">Sign out</button>`;
    $("#signOutButton").addEventListener("click", signOut);
  } else if (auth.oauth_configured || auth.password_login_enabled) {
    const github = auth.oauth_configured ? `<a class="secondary-button small" href="/auth/github/login">Sign in with GitHub</a>` : "";
    const demo = auth.password_login_enabled ? `<button type="button" class="secondary-button small" id="demoSignInButton">Demo account</button>` : "";
    area.innerHTML = github + demo;
    const demoButton = $("#demoSignInButton");
    if (demoButton) demoButton.addEventListener("click", () => { setMode("live"); setView("overview"); $("#demoUsername").focus(); });
  } else {
    area.innerHTML = `<span class="account-note" title="Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET on the server to enable sign-in">Sign-in not configured</span>`;
  }
}

async function signOut() {
  try {
    await api("/auth/logout", { method: "POST" });
    state.auth = { ...state.auth, authenticated: false, user: null };
    state.repos = null; state.pulls = null; state.selectedRepo = ""; state.jobs = null;
    stopPolling();
    renderAccount();
    renderLive();
    notify("Signed out.", "success");
  } catch (error) {
    notify(`Sign out failed: ${error.message}`, "error");
  }
}

function handleAuthRedirect() {
  const params = new URLSearchParams(window.location.search);
  const result = params.get("auth");
  if (!result) return;
  if (result === "ok") { setMode("live"); notify("Signed in with GitHub.", "success"); }
  else if (result === "denied") notify("GitHub sign-in was cancelled or denied. You are not signed in.", "error");
  else if (result === "error") notify("GitHub sign-in failed (expired or invalid sign-in attempt). Please try again.", "error");
  params.delete("auth");
  const query = params.toString();
  history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
}

// Live mode ----------------------------------------------------------------------------------
function liveAccess() {
  const auth = state.auth, status = state.status;
  if (!auth || !status) return { kind: "loading" };
  if ((auth.oauth_configured || auth.password_login_enabled) && !auth.authenticated) return { kind: "needs-signin" };
  if (auth.authenticated && auth.user && auth.user.auth_method === "password") return { kind: "password-user" };
  if (auth.authenticated) return { kind: "signed-in" };
  if (status.github_connected) return { kind: "server" };
  return { kind: "unconfigured" };
}

function renderLive() {
  const access = liveAccess();
  const notes = {
    loading: "Checking what this server allows…",
    "needs-signin": state.auth && state.auth.oauth_configured ? "Sign in with GitHub or a demo account to review pull requests." : "Sign in with a demo account to review pull requests.",
    "password-user": "Signed in with a demo account. Enter the pull request below; the review uses the server's GitHub access.",
    "signed-in": "Pick a repository, then a pull request. Reviews use your GitHub access.",
    server: "GitHub sign-in is not configured, so reviews use the server's GitHub credentials. Enter the pull request below.",
    unconfigured: "No GitHub access is configured on this server. Set GITHUB_TOKEN (or GITHUB_APP_ID with a private key) for server-side access, or GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET to let users sign in. You can still submit below to see the server's answer. Demo mode needs none of this.",
  };
  $("#liveNote").textContent = notes[access.kind];
  $("#signInPrompt").classList.toggle("hidden", access.kind !== "needs-signin");
  $("#githubSignIn").classList.toggle("hidden", !(state.auth && state.auth.oauth_configured));
  $("#passwordForm").classList.toggle("hidden", !(state.auth && state.auth.password_login_enabled));
  $("#pickerBlock").classList.toggle("hidden", access.kind !== "signed-in");
  const manual = $("#manualBlock");
  manual.classList.toggle("hidden", access.kind === "needs-signin" || access.kind === "loading");
  if (access.kind !== "signed-in" && !manual.dataset.touched) manual.open = true;
  renderPublishControl();
  if (access.kind === "signed-in" && state.mode === "live" && state.repos === null) loadRepos();
}

async function passwordSignIn() {
  const username = $("#demoUsername").value.trim(), password = $("#demoPassword").value;
  const error = $("#passwordError");
  const fail = (message) => { error.textContent = message; error.classList.remove("hidden"); };
  error.classList.add("hidden");
  if (!username || !password) { fail("Enter both the username and the password."); return; }
  const submit = $("#passwordSubmit");
  submit.disabled = true;
  try {
    await api("/auth/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
    $("#demoPassword").value = "";
    state.repos = null; state.jobs = null;
    await loadAuth();
    notify("Signed in with a demo account.", "success");
  } catch (failure) {
    $("#demoPassword").value = "";
    if (failure.status === 401) fail("That username or password is not right.");
    else if (failure.status === 429) fail("Too many failed attempts. Wait 10 minutes, then try again.");
    else fail(failure.message);
  } finally {
    submit.disabled = false;
  }
}

function renderPublishControl() {
  const enabled = Boolean(state.status && state.status.publishing_enabled);
  const input = $("#publishInput");
  input.disabled = !enabled || state.busy;
  if (!enabled) input.checked = false;
  $("#publishNote").textContent = !state.status
    ? "Checking whether this server allows posting…"
    : enabled
      ? "Off by default. When ticked, PRISM posts one GitHub review of type COMMENT after you confirm. It never approves, requests changes or merges."
      : "Posting is disabled on this server (PUBLISH_REVIEWS is off), so this review will only be shown here. PRISM never approves or merges in any case.";
  $("#publishBox").classList.toggle("disabled", !enabled);
}

async function loadRepos() {
  const list = $("#repoList");
  list.innerHTML = `<p class="list-empty">Loading your repositories…</p>`;
  try {
    state.repos = await api("/me/repos");
    state.reposError = "";
  } catch (error) {
    state.repos = null;
    state.reposError = error.message;
    list.innerHTML = `<p class="list-empty error">${escapeHtml(error.message)}</p>`;
    notify(`Could not load repositories: ${error.message}`, "error");
    return;
  }
  renderRepos();
}

function renderRepos() {
  const list = $("#repoList");
  if (state.repos === null) return;
  const term = $("#repoSearch").value.trim().toLowerCase();
  const repos = state.repos.filter((repo) => repo.full_name.toLowerCase().includes(term));
  if (!state.repos.length) { list.innerHTML = `<p class="list-empty">No repositories found for your account.</p>`; return; }
  if (!repos.length) { list.innerHTML = `<p class="list-empty">No repository matches “${escapeHtml(term)}”.</p>`; return; }
  list.innerHTML = repos.map((repo) => `<button type="button" role="option" aria-selected="${repo.full_name === state.selectedRepo}" class="repo-item${repo.full_name === state.selectedRepo ? " active" : ""}" data-repo="${escapeHtml(repo.full_name)}"><strong>${escapeHtml(repo.full_name)}</strong><small>${repo.private ? "private" : "public"}${repo.language ? ` · ${escapeHtml(repo.language)}` : ""} · updated ${escapeHtml(timeAgo(repo.updated_at))}</small></button>`).join("");
}

async function selectRepo(fullName) {
  state.selectedRepo = fullName;
  state.pulls = null;
  renderRepos();
  $("#prHeading").textContent = `Open pull requests in ${fullName}`;
  const [owner, repo] = fullName.split("/");
  $("#ownerInput").value = owner;
  $("#repoInput").value = repo;
  await loadPulls();
}

async function loadPulls() {
  if (!state.selectedRepo) return;
  const target = state.selectedRepo;
  const list = $("#prList");
  list.innerHTML = `<p class="list-empty">Loading open pull requests…</p>`;
  const [owner, repo] = target.split("/");
  try {
    const pulls = await api(`/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/pulls?state=open`);
    if (target !== state.selectedRepo) return;
    state.pulls = pulls;
  } catch (error) {
    if (target !== state.selectedRepo) return;
    list.innerHTML = `<p class="list-empty error">${escapeHtml(error.message)}</p>`;
    notify(`Could not load pull requests: ${error.message}`, "error");
    return;
  }
  renderPulls();
}

function renderPulls() {
  const list = $("#prList");
  if (!state.pulls) return;
  if (!state.pulls.length) { list.innerHTML = `<p class="list-empty">No open pull requests in ${escapeHtml(state.selectedRepo)}.</p>`; return; }
  list.innerHTML = state.pulls.map((pull) => {
    const link = /^https:\/\//.test(pull.html_url || "") ? ` · <a href="${escapeHtml(pull.html_url)}" target="_blank" rel="noopener noreferrer">View on GitHub</a>` : "";
    return `<div class="pr-row"><div class="pr-info"><strong>${escapeHtml(pull.title)}${pull.draft ? ' <span class="draft-badge">Draft</span>' : ""}</strong><small>#${escapeHtml(pull.number)} · ${escapeHtml(pull.author || "unknown")} · updated ${escapeHtml(timeAgo(pull.updated_at))}${link}</small></div><button type="button" class="primary-button small js-live-action" data-pr="${escapeHtml(pull.number)}"${state.busy ? " disabled" : ""}>Review this PR</button></div>`;
  }).join("");
}

function manualReference() {
  return { owner: $("#ownerInput").value.trim(), repo: $("#repoInput").value.trim(), number: Number($("#numberInput").value) };
}

function validateReference(ref) {
  const name = /^[A-Za-z0-9_.-]+$/;
  if (!name.test(ref.owner)) return "Enter the repository owner (letters, numbers, dots, dashes, underscores).";
  if (!name.test(ref.repo)) return "Enter the repository name (letters, numbers, dots, dashes, underscores).";
  if (!Number.isInteger(ref.number) || ref.number < 1) return "Enter a pull request number of 1 or more.";
  return "";
}

async function submitLive(ref) {
  if (state.busy) return;
  const problem = validateReference(ref);
  if (problem) { notify(problem, "error"); return; }
  const publish = $("#publishInput").checked && !$("#publishInput").disabled;
  if (publish && !window.confirm(`Post advisory comments to ${ref.owner}/${ref.repo} #${ref.number} on GitHub?\n\nPRISM will add one review of type COMMENT. It never approves, requests changes or merges. This cannot be undone from here.`)) return;
  setBusy(true);
  stopPolling();
  try {
    const payload = await api("/github/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...ref, publish }) });
    state.job = payload.job;
    renderJob();
    if (!payload.created) notify("This pull request commit is already being reviewed or was just reviewed; showing that job.");
    pollJob(payload.job.id);
  } catch (error) {
    setBusy(false);
    state.job = null;
    renderJob();
    renderPipeline(0, true);
    notify(error.message, "error");
  }
}

// Job tracking -------------------------------------------------------------------------------
function stopPolling() {
  state.pollToken += 1;
  clearInterval(state.elapsedTimer);
  state.elapsedTimer = null;
}

function pollJob(id) {
  const token = ++state.pollToken;
  let failures = 0;
  clearInterval(state.elapsedTimer);
  state.elapsedTimer = setInterval(updateElapsed, 1000);
  const tick = async () => {
    if (token !== state.pollToken) return;
    let delay = 2000;
    try {
      const job = await api(`/jobs/${encodeURIComponent(id)}`);
      if (token !== state.pollToken) return;
      if (failures) notify("Reconnected to the server.", "success");
      failures = 0;
      state.job = job;
      renderJob();
      if (job.status === "completed" || job.status === "failed") { finishJob(job); return; }
    } catch (error) {
      if (token !== state.pollToken) return;
      if (!error.network) { jobStopped(`Stopped tracking this job: ${error.message}`); return; }
      failures += 1;
      delay = Math.min(2000 * 2 ** failures, 30000);
      if (failures >= 8) { jobStopped("Lost contact with the server. The review may still be running; check Recent review jobs under History."); return; }
      notify(`Connection lost. Retrying in ${Math.round(delay / 1000)}s (attempt ${failures}).`, "error");
    }
    setTimeout(tick, delay);
  };
  setTimeout(tick, 400);
}

function jobStopped(message) {
  stopPolling();
  setBusy(false);
  renderPipeline(0, true);
  notify(message, "error");
}

function finishJob(job) {
  stopPolling();
  setBusy(false);
  if (job.status === "completed" && job.result) {
    renderPipeline(workflowStages.length);
    renderResult(job.result);
    setBusy(false);
    notify(job.published ? "Review finished and advisory comments were posted to GitHub." : "Review finished and saved.", "success");
  } else if (job.status === "completed") {
    renderPipeline(workflowStages.length);
    notify("Review finished, but its stored result could not be loaded.", "error");
  } else {
    renderPipeline(0, true);
    notify(`Review failed: ${job.error || "no error text was recorded"}`, "error");
  }
  state.jobs = null;
}

function jobElapsed(job) {
  const start = parseTime(job.started_at) ?? parseTime(job.created_at);
  if (start == null) return null;
  const end = parseTime(job.finished_at) ?? Date.now();
  return Math.max(0, end - start);
}

function updateElapsed() {
  const node = $("#jobElapsed");
  if (node && state.job) node.textContent = formatDuration(jobElapsed(state.job));
}

function renderJob() {
  const card = $("#jobCard");
  const job = state.job;
  if (!job) { card.classList.add("hidden"); card.innerHTML = ""; return; }
  const rank = { queued: 0, running: 1, completed: 2, failed: 2 }[job.status] ?? 0;
  const labels = ["Queued", "Running", job.status === "failed" ? "Failed" : "Completed"];
  const steps = labels.map((label, index) => {
    const cls = index < rank ? "done" : index === rank ? (job.status === "failed" ? "failed" : job.status === "completed" ? "done" : "active") : "";
    return `<li class="${cls}"><i></i><span>${label}</span></li>`;
  }).join("");
  let outcome = "";
  if (job.status === "failed") outcome = `<p class="job-outcome bad"><strong>Review failed.</strong> ${escapeHtml(job.error || "No error text was recorded.")}</p>`;
  else if (job.status === "completed") {
    if (job.published) outcome = `<p class="job-outcome ok"><strong>Posted.</strong> Advisory comments were published to GitHub as a COMMENT review.</p>`;
    else if (job.publish_error) outcome = `<p class="job-outcome bad"><strong>Posting failed.</strong> ${escapeHtml(job.publish_error)}. The review itself is saved and shown in Findings.</p>`;
    else outcome = `<p class="job-outcome">${job.publish_requested ? "Posting was requested but nothing was published." : "Not posted to GitHub (you did not ask for it)."} ${escapeHtml(job.finding_count ?? 0)} finding${job.finding_count === 1 ? "" : "s"} saved.</p>`;
  }
  card.innerHTML = `<div class="job-head"><div><span class="section-kicker">REVIEW JOB</span><h3>${escapeHtml(job.repository)} #${escapeHtml(job.pull_number)}</h3></div><div class="job-time"><small>Elapsed</small><strong id="jobElapsed">${formatDuration(jobElapsed(job))}</strong></div></div><ol class="stepper">${steps}</ol>${outcome}<p class="job-meta">Job ${escapeHtml(job.id)} · ${escapeHtml(job.trigger)}${job.requested_by ? ` · by ${escapeHtml(job.requested_by)}` : ""}${job.head_sha ? ` · commit <code>${escapeHtml(job.head_sha.slice(0, 8))}</code>` : ""}</p>`;
  card.classList.remove("hidden");
  const stage = job.status === "queued" ? 0 : Math.min(1 + Math.floor((jobElapsed(job) || 0) / 2500), workflowStages.length - 1);
  if (job.status === "queued" || job.status === "running") renderPipeline(stage);
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$('[data-view-link]').forEach((button) => button.addEventListener("click", () => setView(button.dataset.viewLink)));
  $$(".mode-switch button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  // A div, not a nested <form>: the sign-in prompt lives inside the review form.
  $("#passwordSubmit").addEventListener("click", passwordSignIn);
  $("#passwordForm").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); passwordSignIn(); } });
  $("#reviewForm").addEventListener("submit", (event) => { event.preventDefault(); runReview(); });
  $("#manualBlock").addEventListener("toggle", () => { $("#manualBlock").dataset.touched = "1"; });
  $("#repoSearch").addEventListener("input", renderRepos);
  $("#repoSearch").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
  $("#repoList").addEventListener("click", (event) => { const item = event.target.closest("[data-repo]"); if (item) selectRepo(item.dataset.repo); });
  $("#prList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-pr]");
    if (!button || !state.selectedRepo) return;
    const [owner, repo] = state.selectedRepo.split("/");
    submitLive({ owner, repo, number: Number(button.dataset.pr) });
  });
  $("#prReload").addEventListener("click", () => { if (state.selectedRepo) loadPulls(); else notify("Choose a repository first."); });
  $("#jobsReload").addEventListener("click", loadJobs);
  $("#view-history").addEventListener("click", (event) => { const row = event.target.closest("tr[data-job], tr[data-review]"); if (row) openStored(row.dataset.job ? "job" : "review", row.dataset.job || row.dataset.review); });
  $("#view-history").addEventListener("keydown", (event) => { if (event.key !== "Enter" && event.key !== " ") return; const row = event.target.closest("tr[data-job], tr[data-review]"); if (row) { event.preventDefault(); openStored(row.dataset.job ? "job" : "review", row.dataset.job || row.dataset.review); } });
  $("#refreshButton").addEventListener("click", async () => {
    await Promise.all([loadStatus(), loadAuth()]);
    if (state.mode === "demo") await runReview();
    else if (state.auth && state.auth.authenticated && state.auth.user.auth_method !== "password") { state.repos = null; loadRepos(); if (state.selectedRepo) loadPulls(); }
    if ($("#view-history").classList.contains("active")) loadJobs();
  });
  $("#historyForm").addEventListener("submit", (event) => { event.preventDefault(); loadHistory($("#historyRepo").value.trim()); });
  $$("#severityFilters button").forEach((button) => button.addEventListener("click", () => {
    state.severity = button.dataset.severity;
    $$("#severityFilters button").forEach((item) => item.classList.toggle("active", item === button));
    renderFindings();
  }));
}

async function init() {
  renderPipeline();
  renderWorkflow();
  renderAgents();
  renderMeta(null);
  bindEvents();
  setMode(state.mode);
  const requestedView = window.location.hash.slice(1);
  if (["overview", "workflow", "findings", "history", "configuration"].includes(requestedView)) setView(requestedView);
  await Promise.all([loadStatus(), loadAuth(), loadDemoCases().catch((error) => notify(error.message, "error"))]);
  handleAuthRedirect();
  if (state.mode === "demo") await runReview();
}

document.addEventListener("DOMContentLoaded", init);

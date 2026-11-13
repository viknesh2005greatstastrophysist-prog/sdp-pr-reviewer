const state = {
  mode: "demo",
  result: null,
  status: null,
  severity: "ALL",
  busy: false,
  demoCases: [],
  selectedCase: "full-spectrum",
};

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

function setView(view) {
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  const titles = { overview: "Review command center", workflow: "Pipeline workflow", findings: "Review findings", history: "Review history", configuration: "Runtime configuration" };
  $("#pageTitle").textContent = titles[view] || "PRISM";
  window.location.hash = view;
}

function notify(message, type = "info") {
  const node = $("#notice");
  node.textContent = message;
  node.className = `notice ${type === "error" ? "error" : ""}`;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => node.classList.add("hidden"), 8000);
}

function setMode(mode) {
  state.mode = mode;
  const isLive = mode === "live";
  $("#demoToggle").classList.toggle("active", mode === "demo");
  $("#liveToggle").classList.toggle("active", mode === "live");
  $("#demoControls").classList.toggle("hidden", mode !== "demo");
  $("#liveControls").classList.toggle("hidden", mode !== "live");
  $$("#liveControls input").forEach((input) => {
    input.disabled = !isLive;
    input.required = isLive;
  });
  $("#modeChip").textContent = mode === "demo" ? "SAFE SANDBOX" : "GITHUB REST API";
  updateLauncher();
  if (mode === "live" && state.status && !state.status.github_connected) {
    notify("Live mode needs GitHub credentials in .env. Demo mode remains fully available.");
  }
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
    ["GitHub connection", status.github_connected, "required for live mode"],
    ["LLM specialists", status.llm_connected, status.llm_connected ? "connected" : "offline heuristics"],
    ["Ruff analyzer", status.analyzers.ruff, "deterministic"],
    ["Semgrep analyzer", status.analyzers.semgrep, "security rules"],
    ["Review publishing", status.publishing_enabled, status.publishing_enabled ? "enabled" : "safe / disabled"],
  ];
  const essential = [status.analyzers.ruff, status.analyzers.semgrep, status.database];
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
    ["↗", "Publishing", status.publishing_enabled, status.publishing_enabled ? "Advisory comments enabled" : "Disabled for safe demonstration", "PUBLISH_REVIEWS"],
    ["4×", "Agent graph", true, `${status.agents.length} concurrent specialists`, "LangGraph fan-out / fan-in"],
    ["EV", "Evaluation", true, "Ready for held-out labelled JSONL", "pr-reviewer-evaluate"],
  ];
  $("#configGrid").innerHTML = cards.map(([icon, name, ok, copy, code]) => `<article class="surface config-card"><div class="config-card-head"><span class="config-card-icon">${icon}</span><span class="config-state ${ok ? "ok" : ""}">${ok ? "READY" : "OPTIONAL"}</span></div><h3>${name}</h3><p>${copy}</p><code>${code}</code></article>`).join("");
}

async function loadStatus() {
  try {
    const response = await fetch("/dashboard/status");
    if (!response.ok) throw new Error("Status endpoint unavailable");
    state.status = await response.json();
    $("#engineState").textContent = `${state.status.database} · API ready`;
    renderReadiness(state.status);
    renderConfiguration(state.status);
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
    ["Specialists consolidated", `${result.agent_runs.filter((run) => run.status === "success").length}/4 agents completed`],
    ["Review assembled", `${result.findings.length} deduplicated findings ranked`],
  );
  return entries;
}

function renderResult(result) {
  state.result = result;
  const additions = result.files.reduce((sum, file) => sum + file.lines.filter((line) => line.kind === "ADD").length, 0);
  const elevated = result.findings.filter((finding) => ["CRITICAL", "HIGH"].includes(finding.severity)).length;
  const fixes = result.findings.filter((finding) => finding.suggestion && finding.suggestion.validated).length;
  $("#filesMetric").textContent = result.files.length;
  $("#linesMetric").textContent = `${additions} added lines mapped`;
  $("#findingsMetric").textContent = result.findings.length;
  $("#severityMetric").textContent = `${elevated} high-priority`;
  $("#agentsMetric").textContent = `${result.agent_runs.filter((run) => run.status === "success").length || 4}/4`;
  $("#fixesMetric").textContent = fixes;
  $("#findingBadge").textContent = result.findings.length;
  renderAgents(result.agent_runs);
  renderFindings();
  $("#activityFeed").innerHTML = activityEntries(result).map(([title, copy]) => `<div class="activity-item"><i></i><div><strong>${title}</strong><small>${copy}</small></div></div>`).join("");
  if (result.warnings.length) notify(result.warnings.join(" · "));
}

function renderFindings() {
  const container = $("#findingsList");
  container.innerHTML = "";
  if (!state.result) {
    container.innerHTML = `<div class="empty-state"><span>◇</span><h3>No review loaded</h3><p>Run the demo or connect a live pull request to inspect findings.</p></div>`;
    return;
  }
  const findings = state.result.findings.filter((finding) => state.severity === "ALL" || finding.severity === state.severity);
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
    $(".confidence", fragment).textContent = `${Math.round(finding.confidence * 100)}% confidence`;
    $("h3", fragment).textContent = finding.title;
    $(".finding-message", fragment).textContent = finding.message;
    $(".code-location code", fragment).textContent = `${finding.path}:${finding.line}${finding.end_line ? `–${finding.end_line}` : ""}${finding.rule_id ? ` · ${finding.rule_id}` : ""}`;
    $(".evidence-block p", fragment).textContent = finding.evidence;
    if (finding.suggestion) {
      const suggestion = $(".suggestion-block", fragment);
      suggestion.classList.remove("hidden");
      $("code", suggestion).textContent = finding.suggestion.replacement;
      $("p", suggestion).textContent = `${finding.suggestion.rationale} · ${finding.suggestion.validated ? "syntax validated" : "validation pending"}`;
    }
    container.appendChild(card);
  });
}

function setBusy(busy) {
  state.busy = busy;
  const submit = $("#reviewForm button[type=submit]:not(.hidden)") || $("#reviewForm button[type=submit]");
  $$("#reviewForm button[type=submit]").forEach((button) => { button.disabled = busy; });
  $("#runState").className = `run-state ${busy ? "running" : state.result ? "complete" : "idle"}`;
  $("#runState").innerHTML = `<i></i>${busy ? "Analyzing" : state.result ? "Complete" : "Ready"}`;
  if (submit && busy) submit.blur();
}

async function runReview() {
  if (state.busy) return;
  setBusy(true);
  let stage = 0;
  renderPipeline(stage);
  const ticker = setInterval(() => { stage = Math.min(stage + 1, workflowStages.length - 1); renderPipeline(stage); }, 650);
  try {
    let url = "/dashboard/demo";
    let body;
    if (state.mode === "live") {
      url = "/github/review";
      body = JSON.stringify({ owner: $("#ownerInput").value.trim(), repo: $("#repoInput").value.trim(), number: Number($("#numberInput").value) });
    } else {
      body = JSON.stringify({ case_id: state.selectedCase });
    }
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Review failed (${response.status})`);
    clearInterval(ticker);
    renderPipeline(workflowStages.length);
    renderResult(payload);
    notify(state.mode === "demo" ? `${payload.demo_case.title} demo completed through the local pipeline.` : "Live pull request review completed and persisted.");
  } catch (error) {
    clearInterval(ticker);
    renderPipeline(stage, true);
    notify(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function loadHistory(repository) {
  const [owner, repo] = repository.split("/");
  const rows = $("#historyRows");
  rows.innerHTML = `<tr><td colspan="6" class="table-empty">Loading review history…</td></tr>`;
  try {
    const response = await fetch(`/history/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`);
    if (!response.ok) throw new Error("History lookup failed");
    const data = await response.json();
    rows.innerHTML = data.length ? data.map((item) => `<tr><td>#${escapeHtml(item.id)}</td><td>${escapeHtml(item.repository)}</td><td>#${escapeHtml(item.pull_number)}</td><td><code>${escapeHtml(item.head_sha ? item.head_sha.slice(0, 8) : "—")}</code></td><td>${escapeHtml(item.finding_count)}</td><td>${escapeHtml(new Date(item.created_at).toLocaleString())}</td></tr>`).join("") : `<tr><td colspan="6" class="table-empty">No reviews are stored for this repository yet.</td></tr>`;
  } catch (error) {
    rows.innerHTML = `<tr><td colspan="6" class="table-empty">${escapeHtml(error.message)}</td></tr>`;
  }
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$('[data-view-link]').forEach((button) => button.addEventListener("click", () => setView(button.dataset.viewLink)));
  $$(".mode-switch button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  $("#reviewForm").addEventListener("submit", (event) => { event.preventDefault(); runReview(); });
  $("#refreshButton").addEventListener("click", async () => { await loadStatus(); if (state.mode === "demo") await runReview(); });
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
  bindEvents();
  setMode(state.mode);
  const requestedView = window.location.hash.slice(1);
  if (["overview", "workflow", "findings", "history", "configuration"].includes(requestedView)) setView(requestedView);
  await Promise.all([loadStatus(), loadDemoCases()]);
  await runReview();
}

document.addEventListener("DOMContentLoaded", init);

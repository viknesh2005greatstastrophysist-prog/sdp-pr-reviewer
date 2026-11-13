// PRISM dashboard. Every value that comes from the API is inserted with textContent through
// el(); no markup is ever built from response data.
const state = {
  mode: "demo",
  result: null,
  origin: null,
  status: null,
  severity: "ALL",
  expanded: new Set(),
  busy: false,
  demoCases: [],
  selectedCase: "full-spectrum",
  auth: null,
  repos: null,
  reposError: "",
  selectedRepo: "",
  pulls: null,
  job: null,
  demoRun: null,
  latest: null,
  pollToken: 0,
  elapsedTimer: null,
  jobs: null,
};

const VIEWS = ["overview", "findings", "history", "workflow", "configuration"];
const VIEW_TITLES = { overview: "Overview", findings: "Findings", history: "History", workflow: "Workflow", configuration: "Configuration" };
const SEVERITY_ORDER = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4 };
const SEVERITY = {
  CRITICAL: { word: "Critical", tone: "red" },
  HIGH: { word: "High", tone: "orange" },
  MEDIUM: { word: "Medium", tone: "yellow" },
  LOW: { word: "Low", tone: "grey" },
  INFO: { word: "Info", tone: "grey" },
};
const JOB_STATUS = {
  queued: { word: "Queued", tone: "grey" },
  running: { word: "Reviewing", tone: "blue" },
  completed: { word: "Completed", tone: "green" },
  failed: { word: "Failed", tone: "red" },
};
const TRIGGERS = { dashboard: "Dashboard", webhook: "Webhook", api: "API" };

const agentDefinitions = [
  { id: "bug-hunter", name: "Bug Hunter", copy: "Correctness, security and concurrency" },
  { id: "performance", name: "Performance", copy: "Complexity, memory and I/O" },
  { id: "style", name: "Style", copy: "Typing, clarity and API use" },
  { id: "architecture", name: "Architecture", copy: "Boundaries, scale and reliability" },
];
const SOURCE_NAMES = { ruff: "Ruff", semgrep: "Semgrep", ...Object.fromEntries(agentDefinitions.map((agent) => [agent.id, agent.name])) };

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// Safe DOM builder: strings become text nodes, never markup.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fill(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

function statusEl(word, tone = "grey", extra = "") {
  return el("span", { class: `status ${tone} ${extra}`.trim(), text: word });
}

const plural = (count, word, many = `${word}s`) => `${count} ${count === 1 ? word : many}`;
const joinWords = (items) => (items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`);

function errorText(payload, status) {
  const detail = payload && payload.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
      return where ? `${where}: ${item.msg}` : String(item.msg || item);
    }).join("; ");
  }
  if (status === 401) return "Sign in first";
  if (status === 404) return "Not found";
  return status ? `The request failed (HTTP ${status})` : "The request failed";
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
  if (ms == null || ms < 0) return "";
  if (ms < 10000) return `${(ms / 1000).toFixed(1)}s`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

function timeAgo(value) {
  const time = parseTime(value);
  if (time == null) return "";
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function formatDate(value) {
  const time = parseTime(value);
  return time == null ? "" : new Date(time).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function formatPercent(value) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "";
}

const shortSha = (sha, size = 7) => (sha ? String(sha).slice(0, size) : "");

// Navigation ---------------------------------------------------------------------------------
function setView(view) {
  if (!VIEWS.includes(view)) view = "overview";
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  $$(".nav-row").forEach((item) => {
    const active = item.dataset.view === view;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
  });
  document.title = view === "overview" ? "PRISM · Pull request review" : `${VIEW_TITLES[view]} · PRISM`;
  if (window.location.hash.slice(1) !== view) history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${view}`);
  window.scrollTo(0, 0);
  if (view === "history") loadJobs();
}

function notify(message, type = "info") {
  const node = $("#notice");
  const tone = type === "error" ? "red" : type === "success" ? "green" : "blue";
  fill(node, statusEl(message, tone), el("button", { type: "button", class: "toast-close", "aria-label": "Dismiss message", text: "Dismiss", onclick: () => node.classList.add("hidden") }));
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.classList.remove("hidden");
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => node.classList.add("hidden"), type === "error" ? 12000 : 6000);
}

// Launcher -----------------------------------------------------------------------------------
function setMode(mode) {
  state.mode = mode;
  [["#demoToggle", "demo"], ["#liveToggle", "live"]].forEach(([selector, value]) => {
    const button = $(selector);
    button.classList.toggle("active", mode === value);
    button.setAttribute("aria-checked", String(mode === value));
    button.tabIndex = mode === value ? 0 : -1;
  });
  $("#demoControls").classList.toggle("hidden", mode !== "demo");
  $("#liveControls").classList.toggle("hidden", mode !== "live");
  updateLauncher();
  if (mode === "live") renderLive();
}

function selectedDemo() {
  return state.demoCases.find((item) => item.case_id === state.selectedCase);
}

function updateLauncher() {
  const demo = selectedDemo();
  $("#demoFileMeta").textContent = demo ? `${demo.filename}, ${plural(demo.line_count, "line")} added` : "Loading examples";
}

function renderDemoCases() {
  const picker = $("#demoCasePicker");
  fill(picker, state.demoCases.map((demo) => {
    const checked = demo.case_id === state.selectedCase;
    return el("button", {
      type: "button", role: "radio", class: "choice", "aria-checked": String(checked), tabindex: checked ? "0" : "-1",
      dataset: { case: demo.case_id },
      onclick: () => chooseCase(demo.case_id),
    },
    el("span", { class: "choice-text" }, el("span", { class: "choice-title", text: demo.title }), el("span", { class: "choice-sub", text: demo.subtitle })),
    el("span", { class: "choice-check", "aria-hidden": "true" }));
  }));
  updateLauncher();
}

function chooseCase(caseId, focus = false) {
  state.selectedCase = caseId;
  renderDemoCases();
  if (focus) $(`[data-case="${CSS.escape(caseId)}"]`)?.focus();
}

async function loadDemoCases() {
  const response = await fetch("/dashboard/demo-cases");
  if (!response.ok) throw new Error("The bundled examples could not be loaded.");
  state.demoCases = await response.json();
  if (!state.demoCases.some((item) => item.case_id === state.selectedCase)) {
    state.selectedCase = state.demoCases[0]?.case_id || "full-spectrum";
  }
  renderDemoCases();
}

// Readiness and configuration ------------------------------------------------------------------
function renderReadiness(status) {
  const rows = [
    ["GitHub", status.github_connected ? ["Server access", "green"] : status.oauth_configured ? ["Through sign-in", "green"] : ["Not connected", "orange"]],
    ["Language model", status.llm_connected ? [status.llm_model || "Connected", "green"] : ["Offline rules", "orange"]],
    ["Ruff", status.analyzers.ruff ? ["Installed", "green"] : ["Missing", "red"]],
    ["Semgrep", status.analyzers.semgrep ? ["Installed", "green"] : ["Missing", "red"]],
    ["History", [status.database === "postgresql" ? "PostgreSQL" : "SQLite", "green"]],
    ["Posting to GitHub", status.publishing_enabled ? ["Allowed", "green"] : ["Off", "grey"]],
  ];
  fill($("#readinessList"), rows.map(([name, [word, tone]]) => el("li", { class: "row" },
    el("span", { class: "row-main" }, el("span", { class: "row-title", text: name })),
    el("span", { class: "row-end" }, statusEl(word, tone)))));
  // What a live review needs. Each entry reflects real server state; nothing is assumed ready.
  const essential = [
    ["GitHub access", status.github_connected || status.oauth_configured],
    ["a language model", status.llm_connected],
    ["Ruff", status.analyzers.ruff],
    ["Semgrep", status.analyzers.semgrep],
  ];
  const missing = essential.filter(([, ok]) => !ok).map(([name]) => name);
  $("#readinessSummary").textContent = missing.length
    ? `Not set up: ${joinWords(missing)}. Demo reviews work without ${missing.length === 1 ? "it" : "them"}.`
    : "Everything a live review needs is set up.";
}

function renderConfiguration(status) {
  const on = (ok, yes = "On", no = "Off") => (ok ? [yes, "green"] : [no, "grey"]);
  const groups = [
    ["GitHub", [
      ["Server access", on(status.github_connected, "Set up", "Not set up"), "GITHUB_TOKEN or GITHUB_APP_ID", "Lets the server fetch pull requests itself."],
      ["Sign in with GitHub", on(status.oauth_configured, "Set up", "Not set up"), "GITHUB_OAUTH_CLIENT_ID", "People pick from their own repositories."],
      ["Demo accounts", on(status.password_login_enabled, "Set up", "Not set up"), "DEMO_ACCOUNTS", "Username and password sign-in for reviewers without GitHub."],
      ["Webhook", on(status.webhook_configured, "Set up", "Not set up"), "GITHUB_WEBHOOK_SECRET", "Signed pull request events start reviews automatically."],
      ["Posting reviews", on(status.publishing_enabled, "Allowed", "Off"), "PUBLISH_REVIEWS", "Only when asked for a single review, and only as comments."],
      ["Post webhook reviews", on(status.webhook_auto_publish), "WEBHOOK_AUTO_PUBLISH", "Webhook reviews are posted without anyone asking."],
    ]],
    ["Analysis", [
      ["Review mode", [status.review_mode === "full" ? "Full" : status.review_mode === "focused" ? "Focused" : String(status.review_mode || "Not reported"), null], "REVIEW_MODE",
        status.review_mode === "full"
          ? "Every finding from every tool and agent is shown."
          : status.review_mode === "focused"
            ? "Only likely defects are shown: security, bug and error rules, confident agent findings, and a few per file."
            : null],
      ["Language model", status.llm_connected ? [status.llm_model || "Connected", "green"] : ["Offline rules", "orange"], "LLM_API_KEY, LLM_MODEL", "Without one, the agents use deterministic rules."],
      ["Ruff", on(status.analyzers.ruff, "Installed", "Missing"), null, "Python lint and security rules."],
      ["Semgrep", on(status.analyzers.semgrep, "Installed", "Missing"), null, "The project's local defect and security rules."],
      ["Specialist agents", [joinWords(status.agents.map((id) => SOURCE_NAMES[id] || id)), null], null, null],
      ["Minimum confidence", [formatPercent(status.min_confidence), null], "MIN_CONFIDENCE", "Findings below this are dropped."],
      ["Reviews at once", [String(status.review_concurrency ?? ""), null], "REVIEW_CONCURRENCY", null],
    ]],
    ["Storage and safety", [
      ["Review history", [status.database === "postgresql" ? "PostgreSQL" : "SQLite", null], "DATABASE_URL", null],
      ["Pull request code", ["Parsed, never run", null], null, "Changed files are read and parsed. Nothing from the pull request is imported or executed."],
      ["Actions on GitHub", ["Comments only", null], null, "PRISM never approves, requests changes or merges."],
    ]],
  ];
  fill($("#configGroups"), groups.map(([title, rows]) => el("section", {},
    el("h2", { class: "section-title", text: title }),
    el("ul", { class: "inset" }, rows.map(([name, [word, tone], env, note]) => el("li", { class: "row" },
      el("span", { class: "row-main" }, el("span", { class: "row-title", text: name }), note ? el("span", { class: "row-sub", text: note }) : null),
      el("span", { class: "detail-value" }, tone ? statusEl(word, tone) : el("span", { class: "plain", text: word }), env ? el("span", { class: "env", text: env }) : null)))))));
}

async function loadStatus() {
  try {
    state.status = await api("/dashboard/status");
    renderReadiness(state.status);
    renderConfiguration(state.status);
    renderLive();
    return true;
  } catch (error) {
    $("#readinessSummary").textContent = `Could not check this server: ${error.message}`;
    notify(error.message, "error");
    return false;
  }
}

// Findings -----------------------------------------------------------------------------------
function isStatic(finding) {
  return finding.source === "ruff" || finding.source === "semgrep";
}

function ruleName(ruleId) {
  return ruleId ? String(ruleId).split(".").pop() : "";
}

function displayTitle(finding) {
  // Static tools title findings "ruff: S307"; their message is the readable sentence.
  if (/^(ruff|semgrep):/i.test(finding.title || "") && finding.message) return finding.message;
  return finding.title || finding.message;
}

function sourceLabel(finding) {
  const name = SOURCE_NAMES[finding.source] || finding.source;
  return isStatic(finding) && finding.rule_id ? `${name} ${ruleName(finding.rule_id)}` : name;
}

function fileFor(path) {
  return (state.result?.files || []).find((file) => file.path === path);
}

function newLines(file, from, to) {
  if (!file) return [];
  return (file.lines || []).filter((line) => line.new_line != null && line.kind !== "DELETE" && line.new_line >= from && line.new_line <= to);
}

function contextFor(finding) {
  const context = fileFor(finding.path)?.contexts?.[String(finding.line)];
  if (!context || !context.symbol) return "";
  return context.node_type === "ClassDef" ? `in class ${context.symbol}` : `in ${context.symbol}()`;
}

function codeLine(number, sign, text, kind = "") {
  return el("div", { class: `code-line ${kind}`.trim() },
    el("span", { class: "ln", text: number == null ? "" : String(number) }),
    el("span", { class: "sign", text: sign }),
    el("span", { text: text === "" ? " " : text }));
}

function evidenceText(finding) {
  // Semgrep rule ids carry the rule file's absolute path; show only the rule's own name.
  const text = finding.evidence || "";
  return finding.rule_id && finding.rule_id.includes(".") ? text.split(finding.rule_id).join(ruleName(finding.rule_id)) : text;
}

function evidenceBlock(finding) {
  const end = finding.end_line || finding.line;
  const lines = newLines(fileFor(finding.path), finding.line - 2, end + 2);
  const block = el("div", {}, el("div", { class: "block-label" }, el("span", { text: "Evidence" })));
  if (lines.length) {
    block.appendChild(el("div", { class: "code", role: "region", "aria-label": `Code at ${finding.path} line ${finding.line}`, tabindex: "0" },
      lines.map((line) => codeLine(line.new_line, line.kind === "ADD" ? "+" : "", line.text, line.new_line >= finding.line && line.new_line <= end ? "mark" : ""))));
    if (finding.evidence) block.appendChild(el("p", { class: "block-text", text: evidenceText(finding) }));
  } else {
    block.appendChild(el("div", { class: "code" }, el("div", { class: "code-plain", text: evidenceText(finding) || "No evidence text was recorded." })));
  }
  return block;
}

function suggestionBlock(finding) {
  const suggestion = finding.suggestion;
  if (!suggestion) return null;
  const current = newLines(fileFor(finding.path), suggestion.start_line, suggestion.end_line);
  const replacement = String(suggestion.replacement ?? "").replace(/\n$/, "").split("\n");
  const well = el("div", { class: "code", role: "region", "aria-label": "Suggested replacement", tabindex: "0" },
    current.map((line) => codeLine(line.new_line, "−", line.text, "del")),
    replacement.map((text, index) => codeLine(suggestion.start_line + index, "+", text, "add")));
  return el("div", {},
    el("div", { class: "block-label" },
      el("span", { text: current.length ? suggestion.end_line !== suggestion.start_line ? `Suggested fix, lines ${suggestion.start_line}–${suggestion.end_line}` : `Suggested fix, line ${suggestion.start_line}` : "Suggested replacement" }),
      suggestion.validated ? statusEl("Validated", "green") : statusEl("Not validated", "grey")),
    well,
    suggestion.rationale ? el("p", { class: "block-text", text: suggestion.rationale }) : null);
}

function sortedFindings() {
  return (state.result?.findings || [])
    .map((finding, index) => ({ finding, index }))
    .sort((a, b) => (SEVERITY_ORDER[a.finding.severity] ?? 9) - (SEVERITY_ORDER[b.finding.severity] ?? 9) || (b.finding.confidence || 0) - (a.finding.confidence || 0) || a.index - b.index);
}

function renderFindings() {
  const list = $("#findingsList");
  if (!state.result) {
    fill(list, el("li", { class: "empty" },
      el("h2", { text: "No review loaded" }),
      el("p", { text: "Run an example or review a pull request from Overview, or open a past review from History." }),
      el("button", { type: "button", class: "btn btn-small", text: "Go to Overview", onclick: () => setView("overview") })));
    return;
  }
  const entries = sortedFindings().filter(({ finding }) => state.severity === "ALL" || finding.severity === state.severity);
  if (!state.result.findings.length) {
    fill(list, el("li", { class: "empty" },
      el("h2", { text: "Nothing to fix" }),
      el("p", { text: "No tool or agent found a problem on the changed lines." })));
    return;
  }
  fill(list, entries.map(({ finding, index }) => {
    const id = `finding-${index}`;
    const open = state.expanded.has(index);
    const severity = SEVERITY[finding.severity] || { word: finding.severity, tone: "grey" };
    const meta = [
      el("span", { class: "loc", text: `${finding.path}:${finding.line}${finding.end_line && finding.end_line !== finding.line ? `–${finding.end_line}` : ""}` }),
      el("span", { text: sourceLabel(finding) }),
      isStatic(finding) && finding.category ? el("span", { text: finding.category }) : null,
      contextFor(finding) ? el("span", { text: contextFor(finding) }) : null,
      typeof finding.confidence === "number" ? el("span", { text: `${formatPercent(finding.confidence)} confidence` }) : null,
    ].filter(Boolean);
    const metaNodes = meta.flatMap((node, position) => (position ? [el("span", { "aria-hidden": "true", text: "·" }), node] : [node]));
    const title = displayTitle(finding);
    const body = el("div", { class: "finding-body", id, hidden: !open },
      finding.message && finding.message !== title ? el("p", { text: finding.message }) : null,
      evidenceBlock(finding),
      suggestionBlock(finding),
      finding.rule_id && !isStatic(finding) ? el("p", { class: "job-meta" }, "Rule ", el("code", { text: finding.rule_id })) : null);
    const toggle = el("button", {
      type: "button", class: "row stack finding-toggle", "aria-expanded": String(open), "aria-controls": id,
      onclick: () => {
        const nowOpen = !state.expanded.has(index);
        if (nowOpen) state.expanded.add(index); else state.expanded.delete(index);
        toggle.setAttribute("aria-expanded", String(nowOpen));
        body.hidden = !nowOpen;
      },
    },
    el("span", { class: "row-main" }, el("span", { class: "row-title", text: title }), el("span", { class: "finding-meta" }, metaNodes)),
    el("span", { class: "row-end" }, statusEl(severity.word, severity.tone), el("span", { class: "chevron", "aria-hidden": "true" })));
    return el("li", {}, toggle, body);
  }));
}

function renderSeverityControls() {
  const findings = state.result?.findings || [];
  const counts = {};
  findings.forEach((finding) => { counts[finding.severity] = (counts[finding.severity] || 0) + 1; });
  const present = Object.keys(SEVERITY).filter((key) => counts[key]);
  // One quiet figure row: only severities that occur.
  const figures = $("#severityFigures");
  fill(figures, present.map((key) => el("div", { class: "figure" },
    statusEl(SEVERITY[key].word, SEVERITY[key].tone),
    el("strong", { text: String(counts[key]) }))));
  figures.classList.toggle("hidden", !present.length);
  if (!present.includes(state.severity)) state.severity = "ALL";
  $$("#severityFilters button").forEach((button) => {
    const key = button.dataset.severity;
    button.classList.toggle("hidden", key !== "ALL" && !counts[key]);
    const active = key === state.severity;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $("#findingsListHead").classList.toggle("hidden", present.length < 2);
}

function renderSummary(result) {
  const box = $("#reviewSummary");
  const findings = result.findings;
  const parts = [];
  if (!findings.length) {
    parts.push(el("p", { class: "summary-lead", text: `No problems found in ${plural(result.files.length, "changed file")}.` }));
  } else {
    const elevated = findings.filter((finding) => ["CRITICAL", "HIGH"].includes(finding.severity)).length;
    const top = sortedFindings()[0].finding;
    parts.push(el("p", { class: "summary-lead", text: `${plural(findings.length, "finding")} in ${plural(result.files.length, "changed file")}${elevated ? `, ${elevated} high or critical` : ""}. Start with “${displayTitle(top)}” at ${top.path}:${top.line}.` }));
    const fixes = findings.filter((finding) => finding.suggestion);
    const validated = fixes.filter((finding) => finding.suggestion.validated).length;
    if (fixes.length) parts.push(el("p", { class: "summary-line", text: `${plural(fixes.length, "finding comes", "findings come")} with a suggested fix; ${validated === fixes.length ? (fixes.length === 1 ? "it parses" : "all parse") : `${validated} of ${fixes.length} parse`} as valid Python.` }));
  }
  if (result.published) parts.push(el("p", { class: "summary-line ok", text: "Posted to GitHub as one comment-only review." }));
  else if (result.publish_error) parts.push(el("p", { class: "summary-line bad", text: `Posting to GitHub failed: ${result.publish_error}` }));
  if (result.warnings.length) parts.push(el("ul", { class: "summary-warnings", "aria-label": "Warnings" }, result.warnings.map((warning) => el("li", { text: warning }))));
  fill(box, parts);
  box.classList.remove("hidden");
}

function renderAgents(runs) {
  const list = $("#agentList");
  fill(list, agentDefinitions.map((definition) => {
    const run = runs.find((item) => item.agent === definition.id);
    const status = !run ? ["Did not run", "grey"] : run.status === "success" ? ["Finished", "green"] : run.status === "timed_out" ? ["Timed out", "orange"] : ["Failed", "red"];
    const figures = run ? `${plural(run.finding_count, "finding")}, ${run.duration_ms < 1 ? "under 1 ms" : `${run.duration_ms} ms`}` : "";
    return el("li", { class: "row" },
      el("span", { class: "row-main" },
        el("span", { class: "row-title", text: definition.name }),
        el("span", { class: "row-sub", text: run && run.error ? run.error : run ? `${definition.copy}. ${figures}.` : definition.copy })),
      el("span", { class: "row-end" }, statusEl(status[0], status[1])));
  }));
  $("#agentSection").classList.toggle("hidden", !runs.length);
}

function renderResult(result, origin) {
  result.files = result.files || [];
  result.findings = result.findings || [];
  result.warnings = result.warnings || [];
  result.agent_runs = result.agent_runs || [];
  state.result = result;
  state.origin = origin || null;
  state.severity = "ALL";
  state.expanded = new Set();
  const top = sortedFindings()[0];
  if (top) state.expanded.add(top.index);
  $("#findingCount").textContent = result.findings.length ? String(result.findings.length) : "";
  $("#findingsSource").textContent = origin?.label || "Loaded review";
  renderSummary(result);
  renderSeverityControls();
  renderFindings();
  renderAgents(result.agent_runs);
}

function clearResult() {
  state.result = null;
  state.origin = null;
  state.expanded = new Set();
  $("#findingCount").textContent = "";
  $("#findingsSource").textContent = "No review loaded yet.";
  ["#reviewSummary", "#severityFigures", "#findingsListHead", "#agentSection"].forEach((selector) => $(selector).classList.add("hidden"));
  renderFindings();
}

function originForDemo(payload) {
  const demo = payload.demo_case || selectedDemo();
  return { demo: true, label: demo ? `Example “${demo.title}”, ${demo.filename}. Reviewed on this server; not sent to GitHub.` : "Bundled example" };
}

function originForRecord(record, result) {
  const sha = shortSha(record.head_sha || result.head_sha);
  return { label: `${record.repository} #${record.pull_number}${sha ? `, commit ${sha}` : ""}` };
}

// Demo run -----------------------------------------------------------------------------------
function setBusy(busy) {
  state.busy = busy;
  $$("#reviewForm button[type=submit], .js-live-action").forEach((button) => { button.disabled = busy; });
  $("#demoSubmit").textContent = busy && state.latest?.kind === "demo" ? "Reviewing" : "Run review";
  $("#manualSubmit").textContent = busy && state.latest?.kind === "live" ? "Reviewing" : "Review pull request";
  renderPublishControl();
}

async function runReview({ quiet = false } = {}) {
  if (state.busy) return;
  if (state.mode === "live") { submitLive(manualReference()); return; }
  const demo = selectedDemo();
  stopPolling();
  state.demoRun = { title: demo ? demo.title : "Example", filename: demo?.filename || "", status: "running", started: Date.now(), finished: null, error: "", count: 0 };
  state.latest = { kind: "demo" };
  setBusy(true);
  renderJob();
  state.elapsedTimer = setInterval(updateElapsed, 200);
  try {
    const payload = await api("/dashboard/demo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ case_id: state.selectedCase }) });
    renderResult(payload, originForDemo(payload));
    Object.assign(state.demoRun, { status: "completed", finished: Date.now(), count: payload.findings.length });
    if (!quiet) notify(`Review finished: ${plural(payload.findings.length, "finding")}.`, "success");
  } catch (error) {
    Object.assign(state.demoRun, { status: "failed", finished: Date.now(), error: error.message });
    notify(error.message, "error");
  } finally {
    clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
    setBusy(false);
    renderJob();
  }
}

// History ------------------------------------------------------------------------------------
function jobDuration(job) {
  const start = parseTime(job.started_at);
  const end = parseTime(job.finished_at);
  return start != null && end != null ? formatDuration(end - start) : "";
}

async function loadHistory(repository) {
  const rows = $("#historyRows");
  rows.classList.remove("hidden");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
    fill(rows, el("li", { class: "row row-empty row-error", text: "Enter the repository as owner/name, for example octocat/hello-world." }));
    return;
  }
  const [owner, repo] = repository.split("/");
  fill(rows, el("li", { class: "row row-empty", text: "Loading reviews" }));
  try {
    const data = await api(`/history/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`);
    if (!data.length) { fill(rows, el("li", { class: "row row-empty", text: `No reviews are stored for ${repository} yet.` })); return; }
    fill(rows, data.map((item) => el("li", {}, el("button", { type: "button", class: "row", onclick: () => openStored("review", item.id), "aria-label": `Open review of ${item.repository} pull request ${item.pull_number}` },
      el("span", { class: "row-main" },
        el("span", { class: "row-title", text: `${item.repository} #${item.pull_number}` }),
        el("span", { class: "row-sub", text: [plural(item.finding_count ?? 0, "finding"), item.head_sha ? `commit ${shortSha(item.head_sha)}` : "", formatDate(item.created_at)].filter(Boolean).join(" · ") })),
      el("span", { class: "row-end" }, el("span", { class: "chevron", "aria-hidden": "true" }))))));
  } catch (error) {
    fill(rows, el("li", { class: "row row-empty row-error", text: error.message }));
  }
}

async function loadJobs() {
  const rows = $("#jobRows");
  if (!state.jobs) fill(rows, el("li", { class: "row row-empty", text: "Loading reviews" }));
  try {
    state.jobs = await api("/jobs?limit=25");
    if (!state.jobs.length) { fill(rows, el("li", { class: "row row-empty", text: "No live reviews yet. Start one from Overview and it will appear here." })); return; }
    fill(rows, state.jobs.map((job) => {
      const status = JOB_STATUS[job.status] || { word: job.status, tone: "grey" };
      const done = job.status === "completed";
      const facts = [
        TRIGGERS[job.trigger] || job.trigger,
        job.requested_by ? `by ${job.requested_by}` : "",
        done ? plural(job.finding_count ?? 0, "finding") : "",
        job.published ? "posted to GitHub" : "",
        timeAgo(job.created_at),
        jobDuration(job) ? `took ${jobDuration(job)}` : "",
      ].filter(Boolean).join(" · ");
      const content = [
        el("span", { class: "row-main" },
          el("span", { class: "row-title", text: `${job.repository} #${job.pull_number}` }),
          el("span", { class: "row-sub", text: facts }),
          job.status === "failed" && job.error ? el("span", { class: "row-sub row-error", text: job.error }) : null),
        el("span", { class: "row-end" }, statusEl(status.word, status.tone), done ? el("span", { class: "chevron", "aria-hidden": "true" }) : null),
      ];
      return el("li", {}, done
        ? el("button", { type: "button", class: "row stack", onclick: () => openStored("job", job.id), "aria-label": `Open review of ${job.repository} pull request ${job.pull_number}` }, content)
        : el("div", { class: "row stack" }, content));
    }));
  } catch (error) {
    state.jobs = null;
    if (error.status === 401) {
      fill(rows, el("li", { class: "row" },
        el("span", { class: "row-main" }, el("span", { class: "row-title", text: "Sign in to see reviews" }), el("span", { class: "row-sub", text: "History is visible to signed-in people only." })),
        el("span", { class: "row-end" }, el("button", { type: "button", class: "btn btn-small", text: "Sign in", onclick: goToSignIn }))));
      return;
    }
    fill(rows, el("li", { class: "row row-empty row-error", text: error.message }));
  }
}

async function openStored(kind, id) {
  try {
    const record = await api(kind === "job" ? `/jobs/${encodeURIComponent(id)}` : `/reviews/${encodeURIComponent(id)}`);
    if (!record.result) throw new Error("This review has no stored result to show.");
    renderResult(record.result, originForRecord(record, record.result));
    setView("findings");
  } catch (error) {
    notify(error.message, "error");
  }
}

// Account ------------------------------------------------------------------------------------
async function loadAuth() {
  let ok = true;
  try {
    state.auth = await api("/auth/status");
  } catch (error) {
    ok = false;
    state.auth = { oauth_configured: false, password_login_enabled: false, authenticated: false, user: null, failed: true };
    notify(`Could not check sign-in: ${error.message}`, "error");
  }
  renderAccount();
  renderLive();
  return ok;
}

function goToSignIn() {
  setView("overview");
  setMode("live");
  const target = state.auth?.password_login_enabled ? $("#demoUsername") : $("#githubSignIn a");
  target?.focus();
}

function renderAccount() {
  const area = $("#accountArea");
  const auth = state.auth;
  if (!auth) { area.replaceChildren(); return; }
  if (auth.authenticated && auth.user) {
    const user = auth.user;
    const login = user.login || "?";
    const avatar = /^https:\/\//.test(user.avatar_url || "")
      ? el("img", { class: "avatar", src: user.avatar_url, alt: "", width: "32", height: "32", referrerpolicy: "no-referrer" })
      : el("span", { class: "avatar", "aria-hidden": "true", text: login.slice(0, 2).toUpperCase() });
    fill(area,
      el("div", { class: "account-who" }, avatar,
        el("span", { class: "account-name" }, el("strong", { text: login, title: user.name || login }), el("small", { text: user.auth_method === "password" ? "Demo account" : "GitHub" }))),
      el("div", { class: "account-actions" }, el("button", { type: "button", class: "btn-text", id: "signOutButton", text: "Sign out", onclick: signOut })));
  } else if (auth.oauth_configured || auth.password_login_enabled) {
    fill(area,
      el("span", { class: "account-name account-who-text" }, el("small", { text: "Not signed in" })),
      el("div", { class: "account-actions" }, el("button", { type: "button", class: "btn btn-small", text: "Sign in", onclick: goToSignIn })));
  } else {
    fill(area, el("span", { class: "account-name account-who-text" }, el("small", { text: "Sign-in is not set up on this server" })));
  }
}

async function signOut() {
  try {
    await api("/auth/logout", { method: "POST" });
    state.auth = { ...state.auth, authenticated: false, user: null };
    state.repos = null; state.pulls = null; state.selectedRepo = ""; state.jobs = null;
    stopPolling();
    setBusy(false);
    fill($("#repoList"));
    fill($("#prList"), el("p", { class: "list-empty", text: "Choose a repository to see its open pull requests." }));
    $("#prHeading").textContent = "Open pull requests";
    // A live or stored review belongs to the signed-in person; the bundled example does not.
    if (state.origin && !state.origin.demo) clearResult();
    if (state.latest?.kind === "live") { state.job = null; state.latest = state.demoRun ? { kind: "demo" } : null; renderJob(); }
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
  else if (result === "error") notify("GitHub sign-in failed because the attempt expired or was invalid. Please try again.", "error");
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
    loading: "Checking what this server allows.",
    "needs-signin": state.auth && state.auth.oauth_configured
      ? "Sign in with GitHub to pick from your repositories, or use a demo account."
      : "Sign in with a demo account to review a pull request.",
    "password-user": state.status && state.status.github_connected
      ? "Enter the pull request. The review uses this server's GitHub access."
      : "This server has no GitHub access yet, so it will refuse live reviews until GITHUB_TOKEN or a GitHub App is set up.",
    "signed-in": "Pick a repository, then a pull request. The review uses your GitHub access.",
    server: "Enter the pull request. The review uses this server's GitHub access.",
    unconfigured: "This server has no GitHub access yet. Set GITHUB_TOKEN or a GitHub App on the server, or set up GitHub sign-in. You can still submit to see the server's answer; the demo needs none of this.",
  };
  $("#liveNote").textContent = notes[access.kind];
  const signin = access.kind === "needs-signin";
  $("#signInPrompt").classList.toggle("hidden", !signin);
  $("#githubSignIn").classList.toggle("hidden", !(state.auth && state.auth.oauth_configured));
  $("#passwordForm").classList.toggle("hidden", !(state.auth && state.auth.password_login_enabled));
  $("#passwordSubmit").classList.toggle("btn-primary", !(state.auth && state.auth.oauth_configured));
  $("#pickerBlock").classList.toggle("hidden", access.kind !== "signed-in");
  const manual = $("#manualBlock");
  const ready = !signin && access.kind !== "loading";
  manual.classList.toggle("hidden", !ready);
  // Only GitHub users have a repository picker; everyone else always types the reference.
  manual.classList.toggle("always-open", access.kind !== "signed-in");
  if (access.kind !== "signed-in") manual.open = true;
  $("#publishBox").classList.toggle("hidden", !ready);
  $("#manualActions").classList.toggle("hidden", !ready || (access.kind === "signed-in" && !manual.open));
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
    if (await loadAuth()) {
      notify("Signed in with a demo account.", "success");
      $("#ownerInput").focus();
    }
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
    ? "Checking whether this server allows posting."
    : enabled
      ? "Off by default. When on, you confirm first, then PRISM adds one comment-only review. It never approves or merges."
      : "Turned off on this server (PUBLISH_REVIEWS), so results are shown here only.";
  $("#publishBox").classList.toggle("disabled", !enabled);
}

async function loadRepos() {
  const list = $("#repoList");
  fill(list, el("p", { class: "list-empty", text: "Loading your repositories" }));
  try {
    state.repos = await api("/me/repos");
    state.reposError = "";
  } catch (error) {
    state.repos = null;
    state.reposError = error.message;
    fill(list, el("p", { class: "list-empty error", text: error.message }));
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
  if (!state.repos.length) { fill(list, el("p", { class: "list-empty", text: "No repositories found for your account." })); return; }
  if (!repos.length) { fill(list, el("p", { class: "list-empty", text: `No repository matches “${term}”.` })); return; }
  fill(list, repos.map((repo) => {
    const selected = repo.full_name === state.selectedRepo;
    return el("button", { type: "button", role: "option", class: "choice", "aria-selected": String(selected), dataset: { repo: repo.full_name } },
      el("span", { class: "choice-text" },
        el("span", { class: "choice-title", text: repo.full_name }),
        el("span", { class: "choice-sub", text: [repo.private ? "Private" : "Public", repo.language, repo.updated_at ? `updated ${timeAgo(repo.updated_at)}` : ""].filter(Boolean).join(" · ") })),
      el("span", { class: "choice-check", "aria-hidden": "true" }));
  }));
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
  fill(list, el("p", { class: "list-empty", text: "Loading open pull requests" }));
  const [owner, repo] = target.split("/");
  try {
    const pulls = await api(`/me/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/pulls?state=open`);
    if (target !== state.selectedRepo) return;
    state.pulls = pulls;
  } catch (error) {
    if (target !== state.selectedRepo) return;
    fill(list, el("p", { class: "list-empty error", text: error.message }));
    notify(`Could not load pull requests: ${error.message}`, "error");
    return;
  }
  renderPulls();
}

function renderPulls() {
  const list = $("#prList");
  if (!state.pulls) return;
  if (!state.pulls.length) { fill(list, el("p", { class: "list-empty", text: `No open pull requests in ${state.selectedRepo}.` })); return; }
  fill(list, state.pulls.map((pull) => {
    const link = /^https:\/\//.test(pull.html_url || "") ? el("a", { href: pull.html_url, target: "_blank", rel: "noopener noreferrer", text: "View on GitHub" }) : null;
    const facts = [`#${pull.number}`, pull.draft ? "Draft" : "", pull.author || "", pull.updated_at ? `updated ${timeAgo(pull.updated_at)}` : ""].filter(Boolean).join(" · ");
    return el("div", { class: "pr-row" },
      el("span", { class: "choice-text" }, el("span", { class: "choice-title", text: pull.title }), el("span", { class: "choice-sub" }, facts, link ? " · " : "", link)),
      el("button", { type: "button", class: "btn btn-small js-live-action", dataset: { pr: pull.number }, disabled: state.busy, text: "Review", "aria-label": `Review pull request ${pull.number}` }));
  }));
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
  liveError("");
  if (problem) { liveError(problem); return; }
  const publish = $("#publishInput").checked && !$("#publishInput").disabled;
  if (publish && !window.confirm(`Post advisory comments to ${ref.owner}/${ref.repo} #${ref.number} on GitHub?\n\nPRISM will add one review of type COMMENT. It never approves, requests changes or merges. This cannot be undone from here.`)) return;
  state.latest = { kind: "live" };
  setBusy(true);
  stopPolling();
  try {
    const payload = await api("/github/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...ref, publish }) });
    state.job = payload.job;
    renderJob();
    if (!payload.created) notify("This commit is already being reviewed or was just reviewed, so that review is shown.");
    pollJob(payload.job.id);
  } catch (error) {
    state.latest = state.job ? { kind: "live" } : state.demoRun ? { kind: "demo" } : null;
    setBusy(false);
    renderJob();
    liveError(`Could not start the review: ${error.message}`);
  }
}

function liveError(message) {
  const node = $("#liveError");
  node.textContent = message;
  node.classList.toggle("hidden", !message);
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
      if (!error.network) { jobStopped(`Stopped following this review: ${error.message}`); return; }
      failures += 1;
      delay = Math.min(2000 * 2 ** failures, 30000);
      if (failures >= 8) { jobStopped("Lost contact with the server. The review may still be running; check Recent reviews in History."); return; }
      notify(`Connection lost. Trying again in ${Math.round(delay / 1000)}s (attempt ${failures}).`, "error");
    }
    setTimeout(tick, delay);
  };
  setTimeout(tick, 400);
}

function jobStopped(message) {
  stopPolling();
  setBusy(false);
  renderJob();
  notify(message, "error");
}

function finishJob(job) {
  stopPolling();
  if (job.status === "completed" && job.result) {
    renderResult(job.result, originForRecord(job, job.result));
    notify(job.published ? "Review finished and posted to GitHub as comments." : `Review finished: ${plural(job.result.findings.length, "finding")}.`, "success");
  } else if (job.status === "completed") {
    notify("The review finished, but its stored result could not be loaded.", "error");
  } else {
    notify(`The review failed: ${job.error || "no error text was recorded"}`, "error");
  }
  setBusy(false);
  renderJob();
  state.jobs = null;
}

function jobElapsed(job) {
  const start = parseTime(job.started_at) ?? parseTime(job.created_at);
  if (start == null) return null;
  const end = parseTime(job.finished_at) ?? Date.now();
  return Math.max(0, end - start);
}

function latestElapsed() {
  if (state.latest?.kind === "demo" && state.demoRun) return (state.demoRun.finished ?? Date.now()) - state.demoRun.started;
  if (state.latest?.kind === "live" && state.job) return jobElapsed(state.job);
  return null;
}

function updateElapsed() {
  const node = $("#jobElapsed");
  if (node) node.textContent = formatDuration(latestElapsed());
}

function stepper(steps) {
  return el("ol", { class: "stepper", "aria-label": "Progress" }, steps.map(([label, stepState]) => el("li", { class: `step ${stepState}`.trim(), "aria-current": stepState === "active" ? "step" : null },
    el("span", { class: "step-node", "aria-hidden": "true" }),
    el("span", { text: label }),
    stepState === "done" || stepState === "failed" ? el("span", { class: "visually-hidden", text: stepState === "done" ? " (done)" : " (failed)" }) : null)));
}

function renderJob() {
  const card = $("#jobCard");
  const latest = state.latest;
  if (!latest || (latest.kind === "live" && !state.job) || (latest.kind === "demo" && !state.demoRun)) {
    fill(card, el("p", { class: "empty-line", text: "No review has run in this session yet." }));
    return;
  }
  let name, status, steps, outcome, meta, canOpen;
  if (latest.kind === "demo") {
    const run = state.demoRun;
    name = `Example: ${run.title}`;
    status = JOB_STATUS[run.status];
    const reviewing = run.status === "running" ? "active" : run.status === "failed" ? "failed" : "done";
    steps = [["Submitted", "done"], ["Reviewing", reviewing], ["Finished", run.status === "completed" ? "done" : ""]];
    outcome = run.status === "failed"
      ? el("p", { class: "job-outcome bad", text: run.error })
      : run.status === "completed"
        ? el("p", { class: "job-outcome", text: `${plural(run.count, "finding")} in ${run.filename}. Reviewed on this server; nothing was sent to GitHub.` })
        : null;
    meta = null;
    canOpen = run.status === "completed";
  } else {
    const job = state.job;
    name = `${job.repository} #${job.pull_number}`;
    status = JOB_STATUS[job.status] || { word: job.status, tone: "grey" };
    const failed = job.status === "failed";
    const completed = job.status === "completed";
    steps = [
      ["Queued", job.status === "queued" ? "active" : "done"],
      ["Reviewing", job.status === "running" ? "active" : failed ? "failed" : completed ? "done" : ""],
      ["Saved", completed ? "done" : ""],
    ];
    if (job.publish_requested) steps.push(["Posted to GitHub", job.published ? "done" : job.publish_error ? "failed" : ""]);
    if (failed) outcome = el("p", { class: "job-outcome bad", text: job.error || "No error text was recorded." });
    else if (completed) {
      const count = plural(job.finding_count ?? 0, "finding");
      if (job.published) outcome = el("p", { class: "job-outcome", text: `${count} saved and posted to GitHub as comments.` });
      else if (job.publish_error) outcome = el("p", { class: "job-outcome bad", text: `${count} saved, but posting to GitHub failed: ${job.publish_error}` });
      else outcome = el("p", { class: "job-outcome", text: `${count} saved. ${job.publish_requested ? "Posting was requested but nothing was published." : "Not posted to GitHub."}` });
    }
    meta = el("p", { class: "job-meta" },
      [TRIGGERS[job.trigger] || job.trigger, job.requested_by ? `by ${job.requested_by}` : "", `job ${shortSha(job.id, 8)}`].filter(Boolean).join(" · "),
      job.head_sha ? " · commit " : "", job.head_sha ? el("code", { text: shortSha(job.head_sha, 8) }) : null);
    canOpen = completed && Boolean(job.result);
  }
  fill(card,
    el("div", { class: "job-head" },
      el("div", {}, el("h3", { class: "job-name", text: name }), statusEl(status.word, status.tone)),
      el("div", { class: "job-time" }, el("small", { text: "Time" }), el("strong", { id: "jobElapsed", text: formatDuration(latestElapsed()) }))),
    stepper(steps),
    outcome,
    canOpen || meta ? el("div", { class: "job-foot" }, meta || el("span"), canOpen ? el("button", { type: "button", class: "btn btn-small", text: "Open findings", onclick: () => setView("findings") }) : null) : null);
}

// Workflow -----------------------------------------------------------------------------------
function renderWorkflow() {
  const steps = [
    ["Fetch the pull request", "The GitHub REST API returns the metadata, head commit, unified diff and changed files. The demo uses a bundled pull request instead.", null],
    ["Map the changed lines", "Each diff hunk is parsed so every added line has its file line number and GitHub diff position, plus the function or class around it from the Python syntax tree.", null],
    ["Analyse in parallel", "Two branches run at the same time on the changed lines only.", [
      ["Static analysis", [["Ruff", "lint and security rules"], ["Semgrep", "local rule set"]], "No shell, no cache, hard timeout. Matches are certain."],
      ["Four specialist agents", agentDefinitions.map((agent) => [agent.name, agent.copy.charAt(0).toLowerCase() + agent.copy.slice(1)]), "Structured output and a timeout per agent. A failure is isolated."],
    ]],
    ["Validate, deduplicate and rank", "Anything not on an added line or below the minimum confidence is dropped. Reports of the same problem are merged. The rest is ordered by severity, then confidence.", null],
    ["Suggest fixes", "A suggestion replaces a small range of added lines. Python replacements must parse before they are marked validated.", null],
    ["Store, and post if asked", "The review is saved to history. If you turn on Post advisory comments and the server allows it, PRISM adds one GitHub review of type COMMENT. It never approves, requests changes or merges.", null],
  ];
  fill($("#workflowSteps"), steps.map(([title, text, branches], index) => el("li", {},
    el("span", { class: "step-num", "aria-hidden": "true", text: String(index + 1) }),
    el("div", {},
      el("h2", { class: "step-title", text: title }),
      el("p", { class: "step-text", text }),
      branches ? el("div", { class: "step-branches" }, branches.map(([name, items, note]) => el("div", { class: "branch" },
        el("h3", { class: "branch-title", text: name }),
        el("ul", {}, items.map(([item, what]) => el("li", {}, el("strong", { text: item }), `, ${what}`))),
        el("p", { class: "step-text", text: note })))) : null))));
  const entries = [
    ["This dashboard", "Demo or Live, from Overview.", "/"],
    ["API", "Queues a review of a pull request and returns the job at once.", "POST /github/review"],
    ["Webhook", "Signed pull request events: opened, reopened, new commits, ready for review.", "POST /webhooks/github"],
    ["Raw diff", "Reviews pasted diff text with the agents only; static tools stay off.", "POST /review"],
    ["Evaluation", "Compares static-only, single-agent and multi-agent review on a labelled set.", "pr-reviewer-evaluate"],
  ];
  fill($("#entryList"), entries.map(([title, text, endpoint]) => el("li", { class: "row" },
    el("span", { class: "row-main" }, el("span", { class: "row-title", text: title }), el("span", { class: "row-sub", text })),
    el("span", { class: "detail-value" }, el("span", { class: "env", text: endpoint })))));
}

// Events -------------------------------------------------------------------------------------
function radioKeys(container, selector, choose) {
  container.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const items = $$(selector, container).filter((item) => !item.classList.contains("hidden"));
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1;
    const next = items[(index + step + items.length) % items.length];
    choose(next);
  });
}

function setSeverity(button) {
  state.severity = button.dataset.severity;
  renderSeverityControls();
  renderFindings();
  button.focus();
}

function bindEvents() {
  $$(".nav-row").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  window.addEventListener("hashchange", () => setView(window.location.hash.slice(1)));
  $$(".launcher > .segmented button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  radioKeys($(".launcher > .segmented"), "button", (next) => { setMode(next.dataset.mode); next.focus(); });
  radioKeys($("#demoCasePicker"), ".choice", (next) => chooseCase(next.dataset.case, true));
  // A div, not a nested <form>: the sign-in fields live inside the review form.
  $("#passwordSubmit").addEventListener("click", passwordSignIn);
  $("#passwordForm").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); passwordSignIn(); } });
  $("#reviewForm").addEventListener("submit", (event) => { event.preventDefault(); runReview(); });
  $("#manualBlock").addEventListener("toggle", () => {
    if (liveAccess().kind === "signed-in") $("#manualActions").classList.toggle("hidden", !$("#manualBlock").open);
  });
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
  $("#statusReload").addEventListener("click", async () => {
    const [statusOk, authOk] = await Promise.all([loadStatus(), loadAuth()]);
    if (state.auth && state.auth.authenticated && state.auth.user.auth_method !== "password") { state.repos = null; if (state.mode === "live") loadRepos(); if (state.selectedRepo) loadPulls(); }
    if (statusOk && authOk) notify("Server status checked.", "success");
  });
  $("#historyForm").addEventListener("submit", (event) => { event.preventDefault(); loadHistory($("#historyRepo").value.trim()); });
  $$("#severityFilters button").forEach((button) => button.addEventListener("click", () => setSeverity(button)));
  radioKeys($("#severityFilters"), "button", setSeverity);
}

async function init() {
  renderWorkflow();
  renderFindings();
  renderAgents([]);
  bindEvents();
  setMode(state.mode);
  const requestedView = window.location.hash.slice(1);
  setView(VIEWS.includes(requestedView) ? requestedView : "overview");
  await Promise.all([loadStatus(), loadAuth(), loadDemoCases().catch((error) => notify(error.message, "error"))]);
  handleAuthRedirect();
  if (state.mode === "demo") await runReview({ quiet: true });
}

document.addEventListener("DOMContentLoaded", init);

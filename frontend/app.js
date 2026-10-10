// Ditto Resilience Workbench dashboard. Plain JS, no build step, served by the local API.
// Read-only except for three human actions the API already offers: refresh discovery,
// capture a baseline, plan experiments, and record an approval decision. Nothing here
// executes a fault; the API's execute endpoint always returns 503.

const $ = (selector) => document.querySelector(selector);
const escapeText = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

async function getJson(path, options) {
  const response = await fetch(path, options);
  let payload = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

const fmt = {
  pct: (value) => (value === null || value === undefined ? "—" : `${(value * 100).toFixed(value >= 0.999 || value === 0 ? 0 : 1)}%`),
  ms: (value) => (value === null || value === undefined ? "—" : `${Number(value).toFixed(value < 10 ? 1 : 0)} ms`),
  secs: (value) => {
    if (value === null || value === undefined) return "—";
    return value < 1 ? `${Math.round(value * 1000)} ms` : `${Number(value).toFixed(value < 10 ? 2 : 1)} s`;
  },
  time: (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"),
  ago: (iso) => {
    if (!iso) return "—";
    const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 60) return `${Math.round(seconds)} s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    return `${(seconds / 3600).toFixed(1)} h ago`;
  },
  label: (value) => String(value ?? "").replaceAll("_", " "),
};

const STATUS_CLASS = { completed: "state-approved", aborted: "state-warn", failed: "state-bad", approved: "state-approved", rejected: "state-bad", available: "state-approved" };
const tag = (text, extra = "") => `<span class="state-tag ${STATUS_CLASS[text] || ""} ${extra}">${escapeText(fmt.label(text))}</span>`;

let policyCache = null;
async function policy() {
  if (!policyCache) policyCache = await getJson("/api/v1/policy");
  return policyCache;
}

// ------------------------------------------------------------------ router
const views = ["overview", "architecture", "experiments", "runs", "safety"];
let pollTimer = null;

function route() {
  const [, view = "overview", detail] = (location.hash || "#/overview").split("/");
  const active = views.includes(view) ? view : "overview";
  for (const name of views) $(`#view-${name}`).hidden = name !== active;
  document.querySelectorAll(".tabs a").forEach((link) => link.classList.toggle("active", link.dataset.view === active));
  clearInterval(pollTimer);
  if (route.last !== active) window.scrollTo(0, 0);
  route.last = active;
  const loaders = { overview: loadOverview, architecture: loadArchitecture, experiments: loadExperiments, runs: () => loadRuns(detail), safety: loadSafety };
  loaders[active]().catch(showError);
  if (active === "overview") pollTimer = setInterval(() => loadOverview().catch(showError), 5000);
}

function showError(error) {
  console.error(error);
  $("#overview-updated").textContent = error.message;
}

// ------------------------------------------------------------------ charts
function sparkline(svg, values, { threshold = null, min = null, max = null, invert = false } = {}) {
  const width = 200;
  const height = 36;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const points = values.map((value, index) => ({ value, index })).filter((point) => point.value !== null && point.value !== undefined);
  if (points.length < 2) { svg.innerHTML = ""; return; }
  const all = points.map((point) => point.value).concat(threshold !== null ? [threshold] : []);
  const lo = min ?? Math.min(...all);
  const hi = max ?? Math.max(...all);
  const span = hi - lo || 1;
  const x = (index) => (index / Math.max(1, values.length - 1)) * width;
  const y = (value) => height - 3 - ((value - lo) / span) * (height - 6);
  const path = points.map((point, i) => `${i ? "L" : "M"}${x(point.index).toFixed(1)},${y(point.value).toFixed(1)}`).join(" ");
  const limit = threshold !== null ? `<line class="spark-limit" x1="0" x2="${width}" y1="${y(threshold)}" y2="${y(threshold)}"/>` : "";
  svg.innerHTML = `${limit}<path class="spark-line ${invert ? "invert" : ""}" d="${path}"/>`;
}

function lineChart({ title, points, limit, limitLabel, unit, format }) {
  // points: [{label, value, phase}]
  const width = 560;
  const height = 150;
  const pad = { l: 46, r: 10, t: 14, b: 26 };
  const values = points.map((p) => p.value).filter((v) => v !== null && v !== undefined);
  if (!values.length) return `<div class="chart-empty">${escapeText(title)}: no samples</div>`;
  const hi = Math.max(...values, limit ?? 0) * 1.15 || 1;
  const x = (i) => pad.l + (i / Math.max(1, points.length - 1)) * (width - pad.l - pad.r);
  const y = (v) => pad.t + (1 - v / hi) * (height - pad.t - pad.b);
  const segments = points.map((p, i) => (p.value === null || p.value === undefined ? "" : `${i && points[i - 1].value !== null && points[i - 1].value !== undefined ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`)).join(" ");
  const dots = points.map((p, i) => (p.value === null || p.value === undefined ? "" : `<circle class="dot ${escapeText(p.phase)}" cx="${x(i)}" cy="${y(p.value)}" r="3.5"><title>${escapeText(p.label)}: ${escapeText(format(p.value))}</title></circle>`)).join("");
  const limitLine = limit !== null && limit !== undefined
    ? `<line class="chart-limit" x1="${pad.l}" x2="${width - pad.r}" y1="${y(limit)}" y2="${y(limit)}"/><text class="chart-limit-label" x="${width - pad.r}" y="${y(limit) - 4}" text-anchor="end">${escapeText(limitLabel)} ${escapeText(format(limit))}</text>`
    : "";
  const ticks = [0, hi / 2, hi].map((v) => `<text class="chart-tick" x="${pad.l - 6}" y="${y(v) + 3}" text-anchor="end">${escapeText(format(v))}</text>`).join("");
  const xLabels = points.map((p, i) => `<text class="chart-tick" x="${x(i)}" y="${height - 8}" text-anchor="middle">${escapeText(p.label)}</text>`).join("");
  return `<figure class="chart"><figcaption>${escapeText(title)}</figcaption>
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeText(title)}">
      <line class="chart-axis" x1="${pad.l}" x2="${width - pad.r}" y1="${height - pad.b}" y2="${height - pad.b}"/>
      ${ticks}${limitLine}<path class="chart-line" d="${segments}"/>${dots}${xLabels}
    </svg></figure>`;
}

// ------------------------------------------------------------------ overview
async function loadOverview() {
  const [health, agents, graph, slis, series, baselines, runs] = await Promise.all([
    getJson("/api/v1/health"),
    getJson("/api/v1/agents"),
    getJson("/api/v1/dependency-map"),
    getJson("/api/v1/slis?window=30"),
    getJson("/api/v1/slis/series?minutes=5&bucket=10"),
    getJson("/api/v1/baselines?limit=1"),
    getJson("/api/v1/runs?limit=1"),
  ]);
  $("#target-status").textContent = graph.status === "available" ? "Manifest discovered" : "Awaiting manifest";
  $("#graph-version").textContent = graph.graph_version || "Not generated";
  $("#execution-status").textContent = health.execution_enabled ? "Operator CLI enabled · API never" : "API never executes";
  renderLiveSlis(slis, series);
  renderAgents(agents.agents);
  renderBaselineCard($("#overview-baseline"), baselines.baselines[0]);
  renderRunCard($("#overview-run"), runs.runs[0]);
  $("#overview-updated").textContent = `Updated ${fmt.time(new Date().toISOString())}`;
}

function renderLiveSlis(slis, series) {
  const live = slis.live;
  $("#live-dot").classList.toggle("stale", !live);
  $("#live-label").textContent = live ? "LIVE · LOCAL" : "LOCAL · NO LIVE SAMPLES";
  $("#sli-source").textContent = live ? `Live · last sample ${fmt.ago(slis.latest_sample_at)}` : slis.latest_sample_at ? `Stale · last sample ${fmt.ago(slis.latest_sample_at)}` : "No samples yet";
  const writeOk = slis.write_error_rate === null ? null : 1 - slis.write_error_rate;
  const searchOk = slis.search_error_rate === null ? null : 1 - slis.search_error_rate;
  $("#write-success").textContent = fmt.pct(writeOk);
  $("#write-detail").textContent = slis.write_samples ? `${slis.write_samples} writes` : "No samples";
  $("#write-p95").textContent = fmt.ms(slis.write_p95_ms);
  $("#write-p95-detail").textContent = slis.write_samples ? "successful writes only" : "No samples";
  $("#search-success").textContent = fmt.pct(searchOk);
  $("#search-detail").textContent = slis.search_samples ? `${slis.search_samples} searches` : "No samples";
  $("#freshness-p95").textContent = fmt.secs(slis.freshness_p95_seconds);
  $("#freshness-detail").textContent = slis.freshness_samples ? `${slis.freshness_samples} changes · ${slis.freshness_method || "?"}` : "No samples";
  const points = series.points;
  sparkline($("#spark-write"), points.map((p) => p.write_success), { min: 0, max: 1 });
  sparkline($("#spark-p95"), points.map((p) => p.write_p95_ms), { min: 0, invert: true });
  sparkline($("#spark-search"), points.map((p) => p.search_success), { min: 0, max: 1 });
  sparkline($("#spark-freshness"), points.map((p) => p.freshness_p95_seconds), { min: 0, invert: true });
}

function renderAgents(agents) {
  $("#agent-list").innerHTML = agents.map((agent) => `
    <div class="agent-item">
      <span class="agent-name">${escapeText(agent.name)}</span>
      <span class="agent-status ${agent.status === "ready" ? "ready" : ""}">${escapeText(fmt.label(agent.status))}</span>
    </div>`).join("");
}

function renderBaselineCard(target, baseline) {
  if (!baseline) { target.innerHTML = '<p class="muted">No baseline captured yet. Start the workload, wait 120 s, then capture one under Experiments.</p>'; return; }
  target.innerHTML = `
    <div class="kv-grid">
      <div><span>ID</span><strong>${escapeText(baseline.baseline_id)}</strong></div>
      <div><span>Captured</span><strong>${escapeText(fmt.ago(baseline.captured_at))}</strong></div>
      <div><span>Write p95</span><strong>${escapeText(fmt.ms(baseline.write_p95_ms))}</strong></div>
      <div><span>Write errors</span><strong>${escapeText(fmt.pct(baseline.write_error_rate))}</strong></div>
      <div><span>Samples</span><strong>${escapeText(baseline.write_samples ?? "—")} writes</strong></div>
      <div><span>Freshness p95</span><strong>${escapeText(fmt.secs(baseline.freshness_p95_seconds))}</strong></div>
    </div>`;
}

function renderRunCard(target, run) {
  if (!run) { target.innerHTML = '<p class="muted">No runs recorded yet.</p>'; return; }
  target.innerHTML = `
    <a class="run-summary" href="#/runs/${encodeURIComponent(run.run_id)}">
      <div class="activity-title"><strong>${escapeText(run.experiment_id)}</strong>${tag(run.status)}</div>
      <p class="activity-meta">${escapeText(run.run_id)} · ${escapeText(fmt.ago(run.created_at))}${run.forced_test ? " · forced kill-switch test" : ""}</p>
      <p>${run.abort_reason ? `Aborted: ${escapeText(fmt.label(run.abort_reason))}. ` : ""}Recovery ${run.recovery_verified ? `verified in ${escapeText(fmt.secs(run.recovery_time_seconds))}` : "not verified"}.</p>
    </a>`;
}

// ------------------------------------------------------------------ architecture
const LAYER = (node) => {
  if (node.labels?.provenance_type === "manual") return 0;
  if (node.kind === "proxy") return 1;
  if (node.id === "gateway") return 2;
  if (node.kind === "database") return 4;
  return 3;
};

async function loadArchitecture() {
  const graph = await getJson("/api/v1/dependency-map");
  renderGraph(graph);
  renderArchitectureTables(graph);
}

function renderGraph(graph) {
  const svg = $("#architecture-graph");
  const nodes = graph.nodes || [];
  $("#node-count").textContent = `${nodes.length} components · ${(graph.edges || []).length} edges`;
  if (!nodes.length) { svg.hidden = true; $("#graph-empty").hidden = false; return; }
  $("#graph-empty").hidden = true;
  svg.hidden = false;
  const width = Math.max(svg.clientWidth || 900, 640);
  const layers = [0, 1, 2, 3, 4].map((layer) => nodes.filter((node) => LAYER(node) === layer));
  const rowHeight = 64;
  const height = Math.max(...layers.map((items) => items.length)) * rowHeight + 40;
  const colWidth = width / layers.length;
  const nodeWidth = Math.min(168, colWidth - 26);
  const positions = new Map();
  layers.forEach((items, layer) => items.forEach((node, index) => {
    const offset = (height - items.length * rowHeight) / 2;
    positions.set(node.id, { x: layer * colWidth + (colWidth - nodeWidth) / 2, y: offset + index * rowHeight + 8, w: nodeWidth });
  }));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const seen = new Set();
  const edges = (graph.edges || []).filter((edge) => {
    const key = `${edge.source}>${edge.target}>${edge.provenance_type}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return positions.has(edge.source) && positions.has(edge.target);
  }).map((edge) => {
    const a = positions.get(edge.source);
    const b = positions.get(edge.target);
    const forward = b.x >= a.x;
    const x1 = forward ? a.x + a.w : a.x;
    const x2 = forward ? b.x : b.x + b.w;
    const y1 = a.y + 22;
    const y2 = b.y + 22;
    const bend = Math.max(30, Math.abs(x2 - x1) / 2);
    const d = a.x === b.x
      ? `M${a.x + a.w},${y1} C${a.x + a.w + 40},${y1} ${b.x + b.w + 40},${y2} ${b.x + b.w},${y2}`
      : `M${x1},${y1} C${x1 + (forward ? bend : -bend)},${y1} ${x2 - (forward ? bend : -bend)},${y2} ${x2},${y2}`;
    return `<path class="graph-link ${escapeText(edge.provenance_type)}" d="${d}" marker-end="url(#arrow)"><title>${escapeText(edge.provenance_type)} · ${escapeText(fmt.label(edge.relation))} · ${escapeText(edge.provenance)}</title></path>`;
  }).join("");
  const boxes = nodes.map((node) => {
    const p = positions.get(node.id);
    const manual = node.labels?.provenance_type === "manual";
    const slis = node.labels?.workbench_slis ? node.labels.workbench_slis.replaceAll(",", " · ") : (manual ? "workbench" : node.kind);
    return `<g class="graph-node ${manual ? "manual" : ""}" transform="translate(${p.x},${p.y})"><title>${escapeText(node.id)} — ${escapeText(node.image || node.labels?.purpose || node.kind)}</title>
      <rect width="${p.w}" height="44" rx="3"/><text x="10" y="19">${escapeText(node.id)}</text><text class="node-slis" x="10" y="35">${escapeText(slis)}</text></g>`;
  }).join("");
  svg.innerHTML = `<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#758c86"/></marker></defs>${edges}${boxes}`;
}

function renderArchitectureTables(graph) {
  const paths = { write: ["nginx", "gateway", "policies", "things", "mongodb"], search: ["nginx", "gateway", "policies", "things-search", "mongodb"], freshness: ["nginx", "gateway", "policies", "things", "mongodb"] };
  $("#sli-paths").innerHTML = `<thead><tr><th>SLI</th><th>Request path</th></tr></thead><tbody>${Object.entries(paths).map(([sli, path]) => `<tr><td><strong>${escapeText(sli)}</strong></td><td>${path.map(escapeText).join(" → ")}</td></tr>`).join("")}</tbody>`;
  const services = (graph.nodes || []).filter((node) => node.source === "compose");
  $("#service-table").innerHTML = `<thead><tr><th>Service</th><th>Image</th><th>Published</th><th>SLIs</th></tr></thead><tbody>${services.map((node) => {
    const ports = (node.ports || []).filter((port) => port.host_port).map((port) => `${port.host_ip}:${port.host_port}→${port.container_port}`).join(", ") || "—";
    return `<tr><td><strong>${escapeText(node.id)}</strong></td><td><code>${escapeText((node.image || "").replace("docker.io/", ""))}</code></td><td>${escapeText(ports)}</td><td>${escapeText((node.labels?.workbench_slis || "—").replaceAll(",", ", "))}</td></tr>`;
  }).join("")}</tbody>`;
}

$("#refresh-button").addEventListener("click", async () => {
  const button = $("#refresh-button");
  button.disabled = true;
  try { await getJson("/api/v1/discovery/refresh", { method: "POST" }); await loadArchitecture(); } catch (error) { $("#graph-empty").textContent = error.message; $("#graph-empty").hidden = false; } finally { button.disabled = false; }
});

// ------------------------------------------------------------------ experiments
async function loadExperiments() {
  const [baselines, plans, limits] = await Promise.all([getJson("/api/v1/baselines?limit=10"), getJson("/api/v1/experiments?limit=20"), policy()]);
  renderBaselines(baselines.baselines, limits.baseline_max_age_seconds);
  renderProposals(plans.plans);
}

const isStale = (baseline, maxAgeSeconds) => (Date.now() - new Date(baseline.captured_at).getTime()) / 1000 > maxAgeSeconds;

function renderBaselines(baselines, maxAgeSeconds = 3600) {
  const list = $("#baseline-list");
  const select = $("#baseline-select");
  const previous = select.value;
  if (!baselines.length) {
    list.innerHTML = '<p class="muted">No baselines yet.</p>';
    select.innerHTML = '<option value="">Capture a baseline first</option>';
    return;
  }
  list.innerHTML = baselines.map((b) => `
    <article class="compact-row">
      <div class="activity-title"><strong>${escapeText(b.baseline_id)}</strong>${isStale(b, maxAgeSeconds) ? tag("stale", "state-bad") : tag(b.telemetry_status)}</div>
      <p class="activity-meta">${escapeText(fmt.ago(b.captured_at))} · ${escapeText(b.write_samples ?? "?")} writes · p95 ${escapeText(fmt.ms(b.write_p95_ms))} · errors ${escapeText(fmt.pct(b.write_error_rate))} · freshness ${escapeText(fmt.secs(b.freshness_p95_seconds))} (${escapeText(b.freshness_method || "?")})</p>
    </article>`).join("");
  select.innerHTML = baselines.map((b) => `<option value="${escapeText(b.baseline_id)}">${escapeText(b.baseline_id)} · ${escapeText(fmt.ago(b.captured_at))}${isStale(b, maxAgeSeconds) ? " (stale)" : ""}</option>`).join("");
  if (previous && baselines.some((b) => b.baseline_id === previous)) select.value = previous;
}

function commandBlock(label, command) {
  return `<div class="command"><span>${escapeText(label)}</span><code>${escapeText(command)}</code><button class="copy" type="button" data-copy="${escapeText(command)}">Copy</button></div>`;
}

function renderProposals(plans) {
  const entries = plans.flatMap((plan) => (plan.candidates || []).map((candidate) => ({ plan, candidate })));
  $("#plan-count").textContent = `${entries.length} proposals`;
  const list = $("#plan-history");
  if (!entries.length) { list.innerHTML = '<p class="muted">No plans saved yet.</p>'; return; }
  list.innerHTML = entries.map(({ plan, candidate }) => {
    const status = candidate.approval_status || "pending";
    const approval = candidate.approval;
    const expires = approval?.expires_at ? new Date(approval.expires_at) : null;
    const expired = expires && expires.getTime() < Date.now();
    const predicted = candidate.predicted_slis ? Object.entries(candidate.predicted_slis).map(([k, v]) => `${k}: ${v}`).join(" · ") : (candidate.predicted_impacts || []).join(", ");
    const commands = status === "approved" && !expired ? `
      <div class="commands">
        ${commandBlock("Rehearse (changes nothing)", `python -m safety.cli execute ${candidate.id} --dry-run`)}
        ${commandBlock("Real run", `CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute ${candidate.id}`)}
        ${commandBlock("Kill-switch proof", `CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute ${candidate.id} --forced-test`)}
      </div>` : "";
    return `<article class="proposal-row">
      <div class="activity-title"><strong>${escapeText(candidate.target_component)} · ${escapeText(fmt.label(candidate.action))} · ${escapeText(candidate.duration_seconds)} s</strong>${tag(expired && status === "approved" ? "expired" : status)}</div>
      <p>${escapeText(candidate.hypothesis)}</p>
      <p class="activity-meta">Predicted ${escapeText(predicted)} · risk ${escapeText(candidate.risk_score)} · score ${Number(candidate.selection_score || 0).toFixed(2)}${candidate.reason ? ` · ${escapeText(candidate.reason)}` : ""}</p>
      <p class="activity-meta">${escapeText(plan.plan_id)} · ${escapeText(fmt.ago(plan.created_at))} · <code>${escapeText(candidate.id)}</code></p>
      ${approval ? `<p class="activity-meta">${escapeText(approval.decision)} — ${escapeText(approval.reason)}${expires ? ` · ${expired ? "expired" : "expires"} ${escapeText(fmt.time(approval.expires_at))}` : ""}</p>` : ""}
      ${status === "pending" ? `<div class="proposal-actions">
        <button class="button button-primary" type="button" data-decide="approve" data-id="${escapeText(candidate.id)}" data-hash="${escapeText(candidate.candidate_hash)}">Approve…</button>
        <button class="button button-secondary" type="button" data-decide="reject" data-id="${escapeText(candidate.id)}" data-hash="${escapeText(candidate.candidate_hash)}">Reject…</button></div>` : ""}
      ${commands}
    </article>`;
  }).join("");
}

$("#baseline-button").addEventListener("click", async () => {
  const button = $("#baseline-button");
  const message = $("#baseline-message");
  button.disabled = true;
  message.hidden = false;
  message.className = "notice";
  message.textContent = "Capturing a baseline from the latest live run…";
  try {
    const baseline = await getJson("/api/v1/baselines", { method: "POST" });
    message.className = "notice ok";
    message.textContent = `Baseline ${baseline.baseline_id} captured.`;
    await loadExperiments();
    $("#baseline-select").value = baseline.baseline_id;
  } catch (error) {
    message.className = "notice bad";
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#plan-button").addEventListener("click", async () => {
  const baselineId = $("#baseline-select").value;
  const results = $("#plan-results");
  if (!baselineId) { results.innerHTML = '<p class="muted">Capture a baseline first.</p>'; return; }
  results.innerHTML = '<p class="muted">Ranking evidence-backed candidates…</p>';
  try {
    const plan = await getJson("/api/v1/experiments/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ baseline_id: baselineId, max_candidates: 3 }) });
    results.innerHTML = plan.candidates?.length
      ? `<p class="muted">${plan.candidates.length} candidate(s) saved as ${escapeText(plan.plan_id)}. Review them under Proposals.</p>${(plan.policy_rejections || []).map((r) => `<p class="activity-meta">Not planned: ${escapeText(r.target)} — ${escapeText(r.reason)}</p>`).join("")}`
      : `<p class="muted">No candidates. ${escapeText(plan.policy_rejections?.[0]?.reason || "")}</p>`;
    await loadExperiments();
  } catch (error) {
    results.innerHTML = `<p class="muted">${escapeText(error.message)}</p>`;
  }
});

document.addEventListener("click", async (event) => {
  const copy = event.target.closest("button[data-copy]");
  if (copy) {
    try { await navigator.clipboard.writeText(copy.dataset.copy); copy.textContent = "Copied"; } catch { copy.textContent = "Select & copy"; }
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    return;
  }
  const decide = event.target.closest("button[data-decide]");
  if (decide) openDecision(decide.dataset);
});

// Human decision dialog: requires a name, a reason and the typed word (like the CLI).
let pendingDecision = null;
function openDecision({ decide, id, hash }) {
  pendingDecision = { decision: decide, id, hash };
  const word = decide === "approve" ? "APPROVE" : "REJECT";
  $("#approve-title").textContent = decide === "approve" ? "Approve this experiment" : "Reject this experiment";
  $("#approve-word").textContent = word;
  $("#approve-summary").innerHTML = `<code>${escapeText(id)}</code><p class="activity-meta">Recording a decision never executes anything. Approvals are single-use and expire.</p>`;
  $("#approve-confirm").value = "";
  $("#approve-error").hidden = true;
  $("#approve-submit").textContent = decide === "approve" ? "Record approval" : "Record rejection";
  $("#approve-dialog").showModal();
}

$("#approve-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "confirm" || !pendingDecision) return;
  event.preventDefault();
  const word = pendingDecision.decision === "approve" ? "APPROVE" : "REJECT";
  const by = $("#approve-by").value.trim();
  const reason = $("#approve-reason").value.trim();
  const error = $("#approve-error");
  if ($("#approve-confirm").value.trim() !== word) { error.textContent = `Type ${word} exactly to confirm.`; error.hidden = false; return; }
  if (!by || !reason) { error.textContent = "Name and reason are required."; error.hidden = false; return; }
  try {
    await getJson(`/api/v1/experiments/${encodeURIComponent(pendingDecision.id)}/approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: pendingDecision.decision, reason: `approved_by=${by}; ${reason}`, candidate_hash: pendingDecision.hash }),
    });
    $("#approve-dialog").close();
    pendingDecision = null;
    await loadExperiments();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  }
});

// ------------------------------------------------------------------ runs
let runsCache = [];
async function loadRuns(selected) {
  const runs = (await getJson("/api/v1/runs?limit=50")).runs;
  runsCache = runs;
  $("#run-count").textContent = `${runs.length} recorded`;
  const list = $("#run-list");
  if (!runs.length) { list.innerHTML = '<p class="muted">No runs recorded yet.</p>'; $("#run-detail").innerHTML = '<p class="muted">Run an approved experiment from the operator CLI.</p>'; return; }
  const current = selected ? decodeURIComponent(selected) : runs[0].run_id;
  list.innerHTML = runs.map((run) => `
    <a class="run-item ${run.run_id === current ? "selected" : ""}" href="#/runs/${encodeURIComponent(run.run_id)}">
      <div class="activity-title"><strong>${escapeText(run.experiment_id.replace(/-[0-9a-f]{16}$/, ""))}</strong>${tag(run.status)}</div>
      <p class="activity-meta">${escapeText(fmt.time(run.created_at))} · ${escapeText(run.run_id)}${run.forced_test ? ' · <b>forced test</b>' : ""}</p>
    </a>`).join("");
  const run = runs.find((item) => item.run_id === current) || runs[0];
  await renderRunDetail(run);
}

async function renderRunDetail(run) {
  const [findings, limits] = await Promise.all([getJson(`/api/v1/findings?run_id=${encodeURIComponent(run.run_id)}`), policy()]);
  const timeline = run.timeline || {};
  const start = new Date(timeline.injected_at || run.created_at).getTime();
  const rel = (iso) => (iso ? `${((new Date(iso).getTime() - start) / 1000).toFixed(1)} s` : "—");
  const steps = [["created_at", "Preflight"], ["injected_at", "Fault injected"], ["action_completed_at", "Action returned"], ["abort_at", "Kill switch fired"], ["rollback_at", "Rolled back"], ["recovered_at", "Recovered"]]
    .filter(([key]) => timeline[key])
    .sort((a, b) => new Date(timeline[a[0]]) - new Date(timeline[b[0]]));
  const observations = run.observations || [];
  const baselineP95 = run.baseline_write_p95_ms;
  const multiplier = run.forced_test ? 0.01 : limits.abort_thresholds.write_p95_multiplier;
  const labelFor = (o, i) => (o.phase === "pre_fault" ? "pre" : o.phase === "fault" ? `poll ${observations.slice(0, i + 1).filter((x) => x.phase === "fault").length}` : o.phase === "recovered" ? "rec" : o.phase || "");
  const pts = (key) => observations.map((o, i) => ({ label: labelFor(o, i), value: o[key] ?? null, phase: o.phase }));
  const recovery = run.recovery || {};
  const score = run.resilience_score || {};
  $("#run-detail").innerHTML = `
    <div class="activity-title"><h2>${escapeText(run.experiment_id)}</h2>${tag(run.status)}</div>
    <p class="activity-meta">${escapeText(run.run_id)} · ${escapeText(new Date(run.created_at).toLocaleString())}${run.forced_test ? ` · <b>forced kill-switch test</b> (${escapeText(run.forced_test_rule || "")}) · ${run.forced_test_passed ? "passed" : "did not pass"}` : ""}</p>
    <div class="kv-grid">
      <div><span>Abort reason</span><strong>${escapeText(run.abort_reason ? fmt.label(run.abort_reason) : "none")}</strong></div>
      <div><span>Cleanup</span><strong>${escapeText(run.cleanup_state)}</strong></div>
      <div><span>Recovery</span><strong>${run.recovery_verified ? `verified · ${escapeText(fmt.secs(run.recovery_time_seconds))}` : "not verified"}</strong></div>
      <div><span>Baseline write p95</span><strong>${escapeText(fmt.ms(baselineP95))}</strong></div>
      <div><span>Watchdog</span><strong>${run.watchdog ? (run.watchdog.armed ? "still armed" : "armed → disarmed") : "—"}</strong></div>
      <div><span>Score</span><strong>${escapeText(score.score ?? fmt.label(score.status || "—"))}</strong></div>
    </div>
    <h3>Timeline</h3>
    <ol class="timeline">${steps.map(([key, label]) => `<li class="${key === "abort_at" ? "abort" : ""}"><span>${escapeText(label)}</span><b>${escapeText(rel(timeline[key]))}</b><small>${escapeText(fmt.time(timeline[key]))}</small></li>`).join("")}</ol>
    <h3>Kill switch, poll by poll</h3>
    <div class="chart-grid">
      ${lineChart({ title: "Write p95 latency", points: pts("write_p95_ms"), limit: baselineP95 ? baselineP95 * multiplier : null, limitLabel: run.forced_test ? "forced limit" : `${multiplier}× baseline`, format: (v) => fmt.ms(v) })}
      ${lineChart({ title: "Write error rate", points: pts("write_error_rate"), limit: limits.abort_thresholds.write_error_rate, limitLabel: "abort above", format: (v) => fmt.pct(v) })}
      ${lineChart({ title: "Search error rate (expected impact, not an abort signal)", points: pts("search_error_rate"), limit: null, format: (v) => fmt.pct(v) })}
      ${lineChart({ title: "Freshness p95", points: pts("freshness_p95_seconds"), limit: limits.abort_thresholds.freshness_p95_seconds, limitLabel: "abort above", format: (v) => fmt.secs(v) })}
    </div>
    <p class="source-note">Each point is one 30 s window of live SLI samples: <b>pre</b> = steady-state check, <b>poll n</b> = during the fault, <b>rec</b> = after rollback.</p>
    <h3>Recovery check</h3>
    <p>${recovery.target ? `<code>/health</code> ${escapeText(recovery.health_status ?? "?")} · canary <code>${escapeText(recovery.canary_path || "")}</code> ${escapeText(recovery.canary_status ?? "?")} · SLIs back within thresholds: ${recovery.slis_within_baseline ? "yes" : "no"}` : '<span class="muted">No recovery check recorded.</span>'}</p>
    <h3>Findings</h3>
    ${findings.findings.length ? findings.findings.map((f) => `<article class="finding-row"><div class="activity-title"><strong>${escapeText(f.summary)}</strong>${tag(f.severity)}</div><p>${escapeText(f.recommendation)}</p><p class="activity-meta">${(f.evidence || []).map(escapeText).join(" · ")}</p></article>`).join("") : '<p class="muted">No findings recorded for this run.</p>'}
  `;
}

// ------------------------------------------------------------------ safety
const AUDIT_LABELS = {
  safety_preflight_started: "Preflight started", fault_started: "Fault injected", experiment_aborted: "Kill switch fired",
  cleanup_complete: "Rolled back", cleanup_failed: "Cleanup failed", recovery_checked: "Recovery checked",
  experiment_refused: "Refused (steady state)", runner_error: "Runner error",
};

async function loadSafety() {
  const [limits, watchdog, audit] = await Promise.all([policy(), getJson("/api/v1/watchdog?limit=20"), getJson("/api/v1/audit?limit=40")]);
  $("#policy-view").innerHTML = `<div class="kv-grid">
      <div><span>Compose project</span><strong>${escapeText(limits.compose_project)}</strong></div>
      <div><span>Allowed targets</span><strong>${limits.allowed_targets.map(escapeText).join(", ")}</strong></div>
      <div><span>Denied</span><strong>${limits.denied_targets.map(escapeText).join(", ")}</strong></div>
      <div><span>Actions</span><strong>${limits.enabled_actions.map((a) => escapeText(fmt.label(a))).join(", ")}</strong></div>
      <div><span>Max fault duration</span><strong>${escapeText(limits.max_duration_seconds)} s</strong></div>
      <div><span>Approval expires</span><strong>${escapeText(limits.approval_expires_after_minutes)} min · single use</strong></div>
      <div><span>Baseline max age</span><strong>${escapeText(Math.round(limits.baseline_max_age_seconds / 60))} min</strong></div>
      <div><span>Recovery timeout</span><strong>${escapeText(limits.recovery_timeout_seconds)} s</strong></div>
      <div><span>Watchdog grace</span><strong>${escapeText(limits.watchdog_grace_seconds)} s</strong></div>
      <div><span>Operator CLI</span><strong>${limits.operator_execution_enabled ? "enabled" : "disabled"} · API never</strong></div>
    </div>`;
  const t = limits.abort_thresholds;
  $("#threshold-view").innerHTML = `<table class="data-table"><tbody>
      <tr><td>Write error rate</td><td>abort above <b>${escapeText(fmt.pct(t.write_error_rate))}</b></td></tr>
      <tr><td>Write p95 latency</td><td>abort above <b>${escapeText(t.write_p95_multiplier)}× baseline</b></td></tr>
      <tr><td>Freshness p95</td><td>abort above <b>${escapeText(t.freshness_p95_seconds)} s</b></td></tr>
      <tr><td>Missing samples</td><td>fewer than ${escapeText(t.min_write_samples)} writes or ${escapeText(t.min_freshness_samples)} freshness sample(s) → <b>abort</b></td></tr>
      <tr><td>Other services</td><td>any not running/healthy → <b>abort</b></td></tr>
      <tr><td>Window / poll</td><td>last ${escapeText(t.window_seconds)} s, every ${escapeText(t.poll_interval_seconds)} s during the fault</td></tr>
    </tbody></table><p class="source-note">Limits come only from the policy file; observations can never set their own limit.</p>`;
  $("#watchdog-list").innerHTML = watchdog.events.length ? watchdog.events.map((e) => `<article class="compact-row"><div class="activity-title"><strong>${escapeText(fmt.label(e.event))}</strong><span class="quiet-count">${escapeText(fmt.time(e.ts))}</span></div><p class="activity-meta">${escapeText(e.target || "")}${e.result ? ` · ${escapeText(e.result)}` : ""}${e.pid ? ` · pid ${escapeText(e.pid)}` : ""}</p></article>`).join("") : '<p class="muted">No watchdog events.</p>';
  $("#audit-count").textContent = String(audit.events.length);
  $("#audit-list").innerHTML = audit.events.length ? audit.events.map((e) => `<article class="compact-row"><div class="activity-title"><strong>${escapeText(AUDIT_LABELS[e.event] || fmt.label(e.event))}</strong><span class="quiet-count">${escapeText(fmt.time(e.recorded_at))}</span></div><p class="activity-meta">${escapeText(e.payload?.run_id || "")}${e.payload?.reason ? ` · ${escapeText(fmt.label(e.payload.reason))}` : ""}${e.payload?.action ? ` · ${escapeText(fmt.label(e.payload.action))}` : ""}${e.payload?.verified !== undefined ? ` · verified ${escapeText(e.payload.verified)}` : ""}</p></article>`).join("") : '<p class="muted">No audit events.</p>';
}

window.addEventListener("hashchange", route);
window.addEventListener("resize", () => { if (!$("#view-architecture").hidden) loadArchitecture().catch(showError); });
route();

const agentList = document.querySelector("#agent-list");
const graphSvg = document.querySelector("#architecture-graph");
const graphEmpty = document.querySelector("#graph-empty");
const planResults = document.querySelector("#plan-results");
const baselineInput = document.querySelector("#baseline-id");
const planHistory = document.querySelector("#plan-history");

const escapeText = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

async function getJson(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function renderAgents(agents) {
  agentList.innerHTML = agents.map((agent) => `
    <div class="agent-item">
      <span class="agent-name">${escapeText(agent.name)}</span>
      <span class="agent-status ${agent.status === "ready" ? "ready" : ""}">${escapeText(agent.status.replaceAll("_", " "))}</span>
    </div>`).join("");
}

function renderGraph(graph) {
  const nodes = graph.nodes || [];
  document.querySelector("#graph-version").textContent = graph.graph_version || "Not loaded";
  document.querySelector("#node-count").textContent = `${nodes.length} component${nodes.length === 1 ? "" : "s"}`;
  document.querySelector("#target-status").textContent = graph.status === "available" ? "Manifest discovered" : "Awaiting manifest";
  if (!nodes.length) {
    graphSvg.hidden = true;
    graphEmpty.hidden = false;
    return;
  }
  graphEmpty.hidden = true;
  graphSvg.hidden = false;
  const width = Math.max(graphSvg.clientWidth, 320);
  const rowHeight = 86;
  const columns = width < 620 ? 1 : 2;
  const columnWidth = width / columns;
  const nodeWidth = Math.min(220, columnWidth - 48);
  const height = Math.max(210, Math.ceil(nodes.length / columns) * rowHeight + 40);
  graphSvg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const positions = new Map(nodes.map((node, index) => [node.id, { x: 24 + (index % columns) * columnWidth, y: 34 + Math.floor(index / columns) * rowHeight, width: nodeWidth }]));
  const edges = graph.edges || [];
  const edgeMarkup = edges.map((edge) => {
    const from = positions.get(edge.source);
    const to = positions.get(edge.target);
    if (!from || !to) return "";
    const provenance = `${edge.provenance_type || edge.evidence_type} · ${edge.provenance} · confidence ${edge.confidence}`;
    return `<g class="graph-edge"><title>${escapeText(provenance)}</title><line class="graph-link ${escapeText(edge.provenance_type || "")}" x1="${from.x + from.width}" y1="${from.y + 32}" x2="${to.x}" y2="${to.y + 32}" marker-end="url(#arrow)"/></g>`;
  }).join("");
  const nodeMarkup = nodes.map((node) => {
    const point = positions.get(node.id);
    const affectedSlis = (node.affected_slis || []).join(", ");
    return `<g class="graph-node" transform="translate(${point.x},${point.y})"><title>SLIs: ${escapeText(affectedSlis)}</title><rect width="${point.width}" height="64"/><text x="12" y="21">${escapeText(node.name)}</text><text class="node-kind" x="12" y="39">${escapeText(node.kind)} · ${escapeText(node.source)}</text><text class="node-slis" x="12" y="55" textLength="${Math.max(80, point.width - 24)}" lengthAdjust="spacingAndGlyphs">${escapeText(affectedSlis)}</text></g>`;
  }).join("");
  const clusterMarkup = (graph.clusters || []).map((cluster) => {
    const points = cluster.members.map((member) => positions.get(member)).filter(Boolean);
    if (!points.length) return "";
    const left = Math.max(12, Math.min(...points.map((point) => point.x)) - 12);
    const top = Math.max(10, Math.min(...points.map((point) => point.y)) - 19);
    const right = Math.min(width - 12, Math.max(...points.map((point) => point.x + point.width + 12)) + 12);
    const bottom = Math.max(...points.map((point) => point.y + 77)) + 10;
    return `<rect class="graph-cluster" x="${left}" y="${top}" width="${right - left}" height="${bottom - top}"/><text class="graph-cluster-label" x="${left + 8}" y="${top + 13}">${escapeText(cluster.id)} (${cluster.complete ? "complete" : "partial documentation match"})</text>`;
  }).join("");
  graphSvg.innerHTML = `<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#758c86"/></marker></defs>${clusterMarkup}${edgeMarkup}${nodeMarkup}`;
}

function renderTelemetry(telemetry) {
  document.querySelector("#telemetry-status").textContent = telemetry.status || "Unavailable";
  document.querySelector("#coverage").textContent = `${Math.round((telemetry.coverage || 0) * 100)}% coverage`;
  const list = document.querySelector("#telemetry-list");
  if (!telemetry.observations?.length) {
    list.innerHTML = `<p class="muted">${escapeText(telemetry.warnings?.[0] || "No observations available.")}</p>`;
    return;
  }
  list.innerHTML = telemetry.observations.slice(0, 12).map((item) => `
    <div class="telemetry-row"><strong>${escapeText(item.metric)}</strong><span>${escapeText(item.value)} · ${escapeText(item.quality)}</span></div>`).join("");
}

function renderPlan(plan) {
  if (!plan.candidates?.length) {
    planResults.innerHTML = `<p class="muted">No candidates. ${escapeText(plan.policy_rejections?.[0]?.reason || "No allowed target met the evidence and safety preconditions.")}</p>`;
    return;
  }
  planResults.innerHTML = plan.candidates.map((candidate) => `
    <article class="candidate"><h3>${escapeText(candidate.target_component)} / ${escapeText(candidate.action)}</h3>
      <p>${escapeText(candidate.hypothesis)}</p>
      <p class="candidate-meta">Score ${Number(candidate.selection_score).toFixed(2)} · impact ${Number(candidate.score_terms.impact).toFixed(2)} · novelty ${Number(candidate.score_terms.novelty).toFixed(2)} · uncertainty ${Number(candidate.score_terms.uncertainty).toFixed(2)} · risk ${Number(candidate.risk_score).toFixed(2)}</p>
    </article>`).join("");
}

function renderSavedPlans(plans) {
  const entries = plans.flatMap((plan) => (plan.candidates || []).map((candidate) => ({ plan, candidate })));
  document.querySelector("#plan-count").textContent = String(entries.length);
  if (!entries.length) {
    planHistory.innerHTML = '<p class="muted">No plans saved yet.</p>';
    return;
  }
  planHistory.innerHTML = entries.map(({ plan, candidate }) => {
    const status = candidate.approval_status || "pending";
    const approval = candidate.approval;
    return `<article class="proposal-row">
      <div class="activity-title"><strong>${escapeText(candidate.target_component)} / ${escapeText(candidate.action)}</strong><span class="state-tag ${status === "approved" ? "state-approved" : ""}">${escapeText(status)}</span></div>
      <p>${escapeText(candidate.hypothesis)}</p>
      <p class="activity-meta">Plan ${escapeText(plan.plan_id)} · ${escapeText(plan.created_at || "")}</p>
      ${approval ? `<p class="activity-meta">${escapeText(approval.decision)}: ${escapeText(approval.reason)}</p>` : ""}
      ${status === "pending" ? `<div class="proposal-actions"><button class="button button-secondary" type="button" data-experiment-id="${escapeText(candidate.id)}" data-candidate-hash="${escapeText(candidate.candidate_hash)}" data-decision="approve">Approve</button><button class="button button-secondary" type="button" data-experiment-id="${escapeText(candidate.id)}" data-candidate-hash="${escapeText(candidate.candidate_hash)}" data-decision="reject">Reject</button></div>` : ""}
    </article>`;
  }).join("");
}

function renderRuns(runs) {
  document.querySelector("#run-count").textContent = String(runs.length);
  const list = document.querySelector("#run-history");
  if (!runs.length) {
    list.innerHTML = '<p class="muted">No run results saved.</p>';
    return;
  }
  list.innerHTML = runs.map((run) => `
    <article class="run-row">
      <div class="activity-title"><strong>${escapeText(run.experiment_id)}</strong><span class="state-tag">${escapeText(run.status)}</span></div>
      <p class="activity-meta">${escapeText(run.run_id)} · ${escapeText(run.created_at)}</p>
      <div class="comparison-grid"><div><h4>Predicted</h4><pre>${escapeText(JSON.stringify(run.prediction || {}, null, 2))}</pre></div><div><h4>Observed</h4><pre>${escapeText(JSON.stringify(run.observations || [], null, 2))}</pre></div></div>
    </article>`).join("");
}

function renderFindings(findings) {
  document.querySelector("#finding-count").textContent = String(findings.length);
  const list = document.querySelector("#finding-list");
  if (!findings.length) {
    list.innerHTML = '<p class="muted">No findings saved.</p>';
    return;
  }
  list.innerHTML = findings.map((finding) => `
    <article class="finding-row">
      <div class="activity-title"><strong>${escapeText(finding.summary)}</strong><span class="state-tag">${escapeText(finding.severity)}</span></div>
      <p>${escapeText(finding.recommendation)}</p>
      <p class="activity-meta">${escapeText(finding.run_id)} · confidence ${Math.round(Number(finding.confidence) * 100)}%</p>
      <ul>${(finding.evidence || []).map((item) => `<li>${escapeText(item)}</li>`).join("")}</ul>
    </article>`).join("");
}

async function loadSavedRecords() {
  const [plans, runs, findings] = await Promise.all([
    getJson("/api/v1/experiments"),
    getJson("/api/v1/runs"),
    getJson("/api/v1/findings"),
  ]);
  renderSavedPlans(plans.plans);
  renderRuns(runs.runs);
  renderFindings(findings.findings);
}

async function loadDashboard() {
  document.querySelector("#refresh-button").disabled = true;
  try {
    const [agents, graph, telemetry] = await Promise.all([
      getJson("/api/v1/agents"),
      getJson("/api/v1/architecture"),
      getJson("/api/v1/telemetry"),
    ]);
    renderAgents(agents.agents);
    renderGraph(graph);
    renderTelemetry(telemetry);
    document.querySelector("#baseline-button").disabled = graph.status !== "available" || telemetry.status !== "available";
    await loadSavedRecords();
  } catch (error) {
    agentList.innerHTML = `<p class="muted">${escapeText(error.message)}</p>`;
  } finally {
    document.querySelector("#refresh-button").disabled = false;
  }
}

document.querySelector("#refresh-button").addEventListener("click", async () => {
  try {
    await getJson("/api/v1/discovery/refresh", { method: "POST" });
    await loadDashboard();
  } catch (error) {
    graphEmpty.textContent = error.message;
    graphEmpty.hidden = false;
  }
});

document.querySelector("#baseline-button").addEventListener("click", async () => {
  const button = document.querySelector("#baseline-button");
  button.disabled = true;
  try {
    const baseline = await getJson("/api/v1/baselines", { method: "POST" });
    baselineInput.value = baseline.baseline_id;
    planResults.innerHTML = `<p class="muted">Baseline ${escapeText(baseline.baseline_id)} captured. Plan a candidate when ready.</p>`;
  } catch (error) {
    planResults.innerHTML = `<p class="muted">${escapeText(error.message)}</p>`;
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#plan-button").addEventListener("click", async () => {
  planResults.innerHTML = "<p class=\"muted\">Ranking evidence-backed candidates…</p>";
  try {
    const plan = await getJson("/api/v1/experiments/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ baseline_id: baselineInput.value.trim(), max_candidates: 3 }),
    });
    renderPlan(plan);
    await loadSavedRecords();
  } catch (error) {
    planResults.innerHTML = `<p class="muted">${escapeText(error.message)}</p>`;
  }
});

planHistory.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-experiment-id]");
  if (!button) return;
  const reason = document.querySelector("#approval-reason").value.trim();
  const note = document.querySelector("#approval-note");
  if (!reason) {
    note.textContent = "Enter a reason before recording a decision.";
    return;
  }
  button.disabled = true;
  try {
    await getJson(`/api/v1/experiments/${encodeURIComponent(button.dataset.experimentId)}/approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: button.dataset.decision, reason, candidate_hash: button.dataset.candidateHash }),
    });
    note.textContent = `Decision recorded. Fault execution remains disabled.`;
    await loadSavedRecords();
  } catch (error) {
    note.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

loadDashboard();
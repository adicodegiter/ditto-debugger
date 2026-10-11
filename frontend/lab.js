// Ditto Resilience Lab: an experimental, read-only view. It never calls an execute endpoint and never touches
// containers. "Simulate attack" replays real recorded runs (or a clearly labelled model) in the browser only.
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = (p) => fetch("/api/v1/" + p).then((r) => { if (!r.ok) throw new Error(p + " → " + r.status); return r.json(); });
const params = new URLSearchParams(location.search);
const D = {};
const running = () => sim && sim.phase !== "done";
let sim = null, timer = null, selScenario = null, selNode = null, tab = params.get("tab") || "next";
const LABEL = { restart_container: "Restart", stop_container: "Stop", pause_container: "Pause", cpu_throttle: "CPU throttle", traffic_burst: "Traffic burst" };
const ROLLBACK = { restart_container: "start the container again", stop_container: "start the container again", pause_container: "unpause the container", cpu_throttle: "restore the CPU quota", traffic_burst: "terminate the burst process", compound: "roll back every fault, newest first" };
// A combined run's action is "compound:a+b" and its target "x+y" (two staged faults, one kill switch).
const actionKey = (a) => (String(a || "").startsWith("compound:") ? "compound" : a);
const labelOf = (a) => (actionKey(a) === "compound" ? "Combined failure" : LABEL[a] || a);
const targetsOf = (t) => String(t || "").split("+").filter(Boolean);
const TICK_MS = 450, POLL_S = 2;
const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
const fmt = { ms: (v) => (v == null ? "—" : `${v.toFixed(v < 100 ? 1 : 0)} ms`), pct: (v) => (v == null ? "—" : `${(v * 100).toFixed(v < 0.1 && v > 0 ? 1 : 0)}%`), s: (v) => (v == null ? "—" : `${v < 10 ? v.toFixed(2) : v.toFixed(1)} s`) };

async function load() {
  try {
    const [health, policy, graph, slis, series, runs, plans, ranked, baselines] = await Promise.all([
      api("health"), api("policy"), api("dependency-map"), api("slis?window=30"), api("slis/series?minutes=5&bucket=10"),
      api("runs?limit=50"), api("experiments?limit=1"), api("findings/ranked"), api("baselines?limit=1"),
    ]);
    Object.assign(D, { health, policy, graph, slis, series: series.points, runs: runs.runs, plan: (plans.plans || [])[0], ranked: ranked.ranked, baseline: (baselines.baselines || [])[0] });
    if (!selScenario) selScenario = buildScenarios()[0]?.id;
    renderAll();
    const demo = params.get("demo");
    if (demo && !sim && !load.demoed) { load.demoed = true; const s = buildScenarios().find((x) => x.id.includes(demo) || x.action === demo); if (s) { selScenario = s.id; renderScenarios(); startSim(); } }
  } catch (e) {
    $("#pills").innerHTML = `<span class="pill bad"><i></i>API unreachable: ${esc(e.message)}</span>`;
  }
}

/* ---------- header ---------- */
function renderPills() {
  const { health: h, policy: p, slis: s } = D;
  const pill = (cls, text) => `<span class="pill ${cls}"><i></i>${esc(text)}</span>`;
  $("#pills").innerHTML = [
    pill(h.status === "ok" ? "ok" : "bad", `Stack API ${h.status}`),
    pill(s.live ? "ok" : "warn", s.live ? "Workload live" : "No live samples (workload stopped)"),
    pill(p.api_executes ? "bad" : "ok", p.api_executes ? "API can execute" : "API never executes"),
    pill("ok", `Allowed: ${(p.allowed_targets || []).join(", ")} · denied: ${(p.denied_targets || []).join(", ")}`),
  ].join("");
}

/* ---------- graph ---------- */
function graphModel() {
  const nodes = D.graph.nodes.filter((n) => n.source === "compose");
  const ids = new Set(nodes.map((n) => n.id));
  const seen = new Set();
  const edges = D.graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target) && !seen.has(e.source + ">" + e.target) && seen.add(e.source + ">" + e.target));
  const incoming = new Set(edges.map((e) => e.target));
  const depth = {};
  const queue = nodes.filter((n) => !incoming.has(n.id)).map((n) => { depth[n.id] = 0; return n.id; });
  while (queue.length) { const c = queue.shift(); for (const e of edges) if (e.source === c && depth[e.target] === undefined) { depth[e.target] = depth[c] + 1; queue.push(e.target); } }
  nodes.forEach((n) => { if (depth[n.id] === undefined) depth[n.id] = 0; });
  const maxD = Math.max(...Object.values(depth));
  const W = 150, H = 46, cols = {};
  nodes.forEach((n) => (cols[depth[n.id]] ||= []).push(n));
  const pos = {};
  Object.entries(cols).forEach(([d, list]) => list.sort((a, b) => a.id.localeCompare(b.id)).forEach((n, i) => {
    pos[n.id] = { x: 14 + (maxD ? (d / maxD) * (640 - 28 - W - 40) : 0), y: (i + 0.5) * (330 / list.length) - H / 2 };
  }));
  return { nodes, edges, pos, W, H, depth };
}
function affectedSet(target) {
  const out = new Set();
  (D.graph.impact_paths?.[target] || []).forEach((i) => { if (i.impacted_component !== target) out.add(i.impacted_component); });
  return out;
}
function renderGraph() {
  const m = graphModel();
  const sc = sim && sim.phase !== "done" ? sim.scenario : null;
  const targets = new Set(sc ? targetsOf(sc.target) : []);
  const hit = new Set([...targets].flatMap((t) => [...affectedSet(t)]).filter((id) => !targets.has(id)));
  const recov = sim && (sim.phase === "rollback" || sim.phase === "recovering");
  const edgeSvg = m.edges.map((e) => {
    const a = m.pos[e.source], b = m.pos[e.target];
    const same = m.depth[e.source] === m.depth[e.target];
    const sx = a.x + m.W, sy = a.y + m.H / 2, tx = same ? b.x + m.W : b.x, ty = b.y + m.H / 2;
    const d = same ? `M${sx},${sy} C${sx + 46},${sy} ${sx + 46},${ty} ${tx},${ty}` : `M${sx},${sy} C${sx + 40},${sy} ${tx - 40},${ty} ${tx},${ty}`;
    const hot = sc && !recov && (targets.has(e.source) || targets.has(e.target) || (hit.has(e.source) && hit.has(e.target)));
    return `<path class="edge ${e.provenance_type === "documented" ? "documented" : ""} ${hot ? "hot" : ""}" d="${d}" marker-end="url(#arr)"><title>${esc(e.source)} → ${esc(e.target)} · ${esc(e.relation)} · ${esc(e.provenance_type)}</title></path>`;
  }).join("");
  const nodeSvg = m.nodes.map((n) => {
    const p = m.pos[n.id];
    const cls = [n.id === selNode ? "sel" : "", sc && targets.has(n.id) ? (recov ? "recover" : "target") : "", sc && hit.has(n.id) ? (recov ? "recover" : "impacted") : ""].join(" ");
    const SHORT = { service_availability: "avail", error_rate: "err", p95_latency: "p95", staleness_p95: "stale" };
    const slis = (n.affected_slis || []).map((x) => SHORT[x] || x).join(" · ") || n.labels?.workbench_slis?.split(",").join(" · ") || "";
    return `<g class="node ${cls}" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})"><rect width="${m.W}" height="${m.H}"/><text x="10" y="19">${esc(n.id)}</text><text class="slis" x="10" y="35">${esc(slis)}</text></g>`;
  }).join("");
  $("#graph").innerHTML = `<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#5b7790"/></marker></defs>${edgeSvg}${nodeSvg}`;
  $("#graph").querySelectorAll(".node").forEach((g) => g.addEventListener("click", () => { selNode = g.dataset.id; renderGraph(); renderNodeCard(); }));
  $("#legend").innerHTML = `<span><b></b>configured in compose</span><span><b class="dash"></b>documented by Ditto</span><span><em style="background:#3b1519;border:1px solid var(--bad)"></em>attacked</span><span><em style="background:#3a2d0f;border:1px solid var(--warn)"></em>impacted</span><span><em style="background:#10283f;border:1px solid var(--info)"></em>rolling back</span>`;
}
function renderNodeCard() {
  const n = D.graph.nodes.find((x) => x.id === selNode);
  if (!n) { $("#node-card").textContent = "Click a service to see what depends on it."; return; }
  const imp = [...affectedSet(n.id)];
  const ports = (n.ports || []).filter((p) => p.host_port).map((p) => `${p.host_ip}:${p.host_port}`).join(", ") || "not published";
  $("#node-card").innerHTML = `<strong>${esc(n.id)}</strong> · <code>${esc((n.image || n.kind || "").replace("docker.io/", ""))}</code> · ports ${esc(ports)}<br>Carries SLIs: ${(n.affected_slis || []).map((s) => `<span class="chip">${esc(s)}</span>`).join("") || "none"}<br>If it fails, this also hurts: ${imp.map((i) => `<span class="chip">${esc(i)}</span>`).join("") || "nothing else in the graph"}`;
}

/* ---------- scenarios ---------- */
function buildScenarios() {
  const out = [];
  const runs = (D.runs || []).filter((r) => !r.dry_run && !r.forced_test && r.diagnosis?.action && (r.observations || []).some((o) => o.phase === "fault"));
  const seen = new Set();
  runs.forEach((r) => {
    const key = r.diagnosis.action + r.diagnosis.target_component;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ id: r.run_id, kind: "replay", run: r, action: r.diagnosis.action, target: r.diagnosis.target_component, title: `${labelOf(r.diagnosis.action)} · ${targetsOf(r.diagnosis.target_component).join(" + ")}`, sub: `replays ${r.run_id.slice(0, 16)} (${r.status}${r.abort_reason ? ", " + r.abort_reason.replace(/_/g, " ") : ""})` });
  });
  [{ action: "stop_container", target: "gateway", title: "Stop · gateway", sub: "model: front door goes down, everything fails", seconds: 45 },
   { action: "cpu_throttle", target: "mongodb", title: "CPU throttle · mongodb", sub: "model: database slows, latency climbs", seconds: 45 }]
    .forEach((m) => { if (!seen.has(m.action + m.target)) out.push({ id: "model-" + m.action, kind: "model", ...m }); });
  return out;
}
function renderScenarios() {
  const list = buildScenarios();
  $("#scenarios").innerHTML = list.map((s) => `<button class="scn ${s.id === selScenario ? "on" : ""}" data-id="${esc(s.id)}" ${running() ? "disabled" : ""}><strong>${esc(s.title)}</strong><small>${esc(s.sub)}</small><div class="src ${s.kind === "replay" ? "real" : "model"}">${s.kind === "replay" ? "● REAL RUN REPLAY" : "◆ MODEL · NOT MEASURED"}</div></button>`).join("");
  $("#scenarios").querySelectorAll(".scn").forEach((b) => b.addEventListener("click", () => {
    if (running()) return;
    selScenario = b.dataset.id;
    if (sim) { resetSim(); return; } // a finished run's charts give way to live data again
    renderScenarios();
  }));
}

/* ---------- simulation ---------- */
function thresholds() { return D.policy.abort_thresholds || { write_error_rate: 0.2, write_p95_multiplier: 3, freshness_p95_seconds: 30 }; }
function baselineP95(sc) { return num(sc.run?.baseline_write_p95_ms) ?? num(D.baseline?.write_p95_ms) ?? 50; }
function pollsFor(sc) {
  const base = baselineP95(sc);
  if (sc.kind === "replay") {
    const obs = sc.run.observations || [];
    const pre = obs.find((o) => o.phase === "pre_fault");
    const fault = obs.filter((o) => o.phase === "fault").map((o) => ({ p95: num(o.write_p95_ms), werr: num(o.write_error_rate) ?? 0, serr: num(o.search_error_rate) ?? 0, fresh: num(o.freshness_p95_seconds) ?? 0 }));
    return { base, pre: pre ? { p95: num(pre.write_p95_ms) ?? base, werr: 0, serr: 0, fresh: num(pre.freshness_p95_seconds) ?? 0.1 } : { p95: base, werr: 0, serr: 0, fresh: 0.1 }, fault };
  }
  const n = Math.round((sc.seconds || 45) / POLL_S), fault = [];
  for (let i = 0; i < n; i++) {
    const k = i + 1;
    if (sc.action === "stop_container") fault.push({ p95: base * (1 + k * 0.4), werr: Math.min(1, k * 0.18), serr: Math.min(1, k * 0.25), fresh: k * 2.2 });
    else fault.push({ p95: base * (1 + 0.18 * k), werr: 0, serr: 0, fresh: 0.1 + k * 0.02 });
  }
  return { base, pre: { p95: base, werr: 0, serr: 0, fresh: 0.1 }, fault };
}
function startSim() {
  if (sim && sim.phase === "done") sim = null;
  const sc = buildScenarios().find((s) => s.id === selScenario);
  if (!sc || sim) return;
  const m = pollsFor(sc);
  sim = { scenario: sc, base: m.base, pre: m.pre, fault: m.fault, tick: -1, phase: "pre", points: [], log: [], killed: null, recoverTicks: 0, last: m.pre, xCount: m.fault.length + 8, t0: Date.now() };
  document.body.classList.add("simulating");
  $("#mode").textContent = "SIMULATION"; $("#mode").classList.add("sim");
  $("#btn-attack").disabled = true; $("#btn-kill").disabled = false;
  $("#tele-sub").textContent = `simulated: ${sc.title} (${sc.kind === "replay" ? "real recorded numbers" : "model"})`;
  $("#feed").innerHTML = "";
  logEv(0, "info", `Simulation started: ${sc.title}. No container is touched.`);
  logEv(0, "info", `Kill switch armed: write errors > ${fmt.pct(thresholds().write_error_rate)}, write p95 > ${thresholds().write_p95_multiplier}× baseline (${fmt.ms(sim.base * thresholds().write_p95_multiplier)}), freshness p95 > ${thresholds().freshness_p95_seconds} s.`);
  renderScenarios(); renderGraph(); renderTabBody();
  timer = setInterval(step, TICK_MS);
}
const tSec = () => Math.max(0, sim.points.length - 1) * POLL_S;
function logEv(t, cls, text) {
  const li = document.createElement("li"); li.className = cls;
  li.innerHTML = `<time>t+${t}s</time><span>${esc(text)}</span>`;
  $("#feed").prepend(li);
}
function breach(p) {
  const th = thresholds(), why = [];
  if (p.werr > th.write_error_rate) why.push(["write errors", `${fmt.pct(p.werr)} > ${fmt.pct(th.write_error_rate)}`]);
  if (p.p95 != null && p.p95 > sim.base * th.write_p95_multiplier) why.push(["write p95", `${fmt.ms(p.p95)} > ${fmt.ms(sim.base * th.write_p95_multiplier)}`]);
  if (p.fresh > th.freshness_p95_seconds) why.push(["freshness", `${fmt.s(p.fresh)} > ${th.freshness_p95_seconds} s`]);
  return why;
}
function fire(kind, reason) {
  if (!sim || sim.killed || !["pre", "fault"].includes(sim.phase)) return;
  sim.killed = { kind, reason, t: tSec() };
  sim.tripped = new Set(breach(sim.last).map((w) => w[0]));
  logEv(tSec(), "bad", `KILL SWITCH FIRED (${kind}): ${reason}`);
  logEv(tSec(), "warn", `Rollback: ${ROLLBACK[actionKey(sim.scenario.action)] || "undo the fault"}.`);
  sim.phase = "rollback"; sim.recoverTicks = 0;
  $("#btn-kill").disabled = true;
}
function step() {
  if (!sim) return;
  sim.tick++;
  if (sim.phase === "pre") {
    sim.points.push(sim.pre); sim.last = sim.pre;
    logEv(0, "ok", "Steady state checked: SLIs healthy, fault window opens.");
    sim.phase = "fault"; sim.faultIdx = 0;
  } else if (sim.phase === "fault") {
    if (sim.faultIdx === 0) logEv(tSec() + POLL_S, "warn", `Fault injected: ${sim.scenario.title}.`);
    const p = sim.fault[sim.faultIdx++];
    sim.points.push(p); sim.last = p;
    const why = breach(p);
    if (why.length) fire("auto", why.map((w) => `${w[0]} ${w[1]}`).join("; "));
    else if (params.get("kill") && sim.faultIdx === Number(params.get("kill"))) fire("manual", "operator pressed the kill switch");
    else if (sim.faultIdx >= sim.fault.length) {
      logEv(tSec(), "ok", "Fault window ended without a breach. Rolling back.");
      sim.phase = "rollback"; sim.recoverTicks = 0; $("#btn-kill").disabled = true;
    }
  } else if (sim.phase === "rollback") {
    sim.recoverTicks++;
    sim.points.push(sim.last);
    if (sim.recoverTicks >= 2) { sim.phase = "recovering"; sim.recoverTicks = 0; logEv(tSec(), "info", "Rolled back. Checking /health and the search canary."); }
  } else if (sim.phase === "recovering") {
    sim.recoverTicks++;
    const f = Math.max(0, 1 - sim.recoverTicks / 4), b = sim.pre;
    const p = { p95: b.p95 + ((sim.last.p95 ?? b.p95) - b.p95) * f, werr: sim.last.werr * f, serr: sim.last.serr * f, fresh: b.fresh + (sim.last.fresh - b.fresh) * f };
    sim.points.push(p); sim.last = p;
    if (sim.recoverTicks >= 4) {
      sim.phase = "done";
      logEv(tSec(), "ok", "Recovery verified: SLIs back within baseline.");
      logEv(tSec(), sim.killed ? "bad" : "ok", sim.killed ? `Result: ABORTED by the kill switch (${sim.killed.kind}) at t+${sim.killed.t}s. System recovered.` : "Result: COMPLETED without a breach.");
      endSim();
    }
  }
  renderTelemetry(); renderRules(); renderGraph();
  const total = sim.fault.length + 7;
  $("#progress-bar").style.width = Math.min(100, (sim.tick / total) * 100) + "%";
  $("#timer").textContent = `t+${tSec()}s · ${sim.phase}`;
}
function endSim() {
  clearInterval(timer); timer = null;
  document.body.classList.remove("simulating"); $("#mode").classList.remove("sim");
  // The charts keep showing the finished run until the operator picks a scenario or runs again,
  // so the badge must not claim LIVE VIEW meanwhile.
  $("#mode").textContent = "SIMULATION DONE · pick a scenario for live view";
  $("#btn-attack").disabled = false; $("#btn-kill").disabled = true;
  $("#btn-attack").textContent = "↻ Run again";
  renderScenarios(); renderTabBody(); // re-enable the cards and "Simulate this" buttons now, not at the next 15 s refresh
}
function resetSim() {
  sim = null;
  $("#mode").textContent = "LIVE VIEW";
  $("#btn-attack").textContent = "▶ Simulate attack";
  $("#tele-sub").textContent = "last 5 minutes from the fleet workload";
  $("#feed").innerHTML = `<li class="muted">Nothing yet. Pick a scenario and press “Simulate attack”.</li>`;
  $("#progress-bar").style.width = "0"; $("#timer").textContent = "idle";
  renderAll();
}
function renderRules() {
  const th = thresholds();
  const p = sim?.last, base = sim?.base ?? baselineP95({});
  const was = (k) => sim?.tripped?.has(k);
  const rows = [
    ["Write errors", p ? fmt.pct(p.werr) : "—", `abort above ${fmt.pct(th.write_error_rate)}${was("write errors") ? " · TRIPPED" : ""}`, was("write errors") || (p && p.werr > th.write_error_rate)],
    ["Write p95", p ? fmt.ms(p.p95) : "—", `abort above ${th.write_p95_multiplier}× baseline = ${fmt.ms(base * th.write_p95_multiplier)}${was("write p95") ? " · TRIPPED" : ""}`, was("write p95") || (p && p.p95 != null && p.p95 > base * th.write_p95_multiplier)],
    ["Freshness p95", p ? fmt.s(p.fresh) : "—", `abort above ${th.freshness_p95_seconds} s${was("freshness") ? " · TRIPPED" : ""}`, was("freshness") || (p && p.fresh > th.freshness_p95_seconds)],
    ["Missing samples", "treated as unhealthy", "never read as healthy", false],
  ];
  $("#rules").innerHTML = rows.map(([n, v, lim, tr]) => `<div class="rule ${tr ? "tripped" : ""}"><i></i><div><strong>${esc(n)}</strong> ${esc(v)}<small>${esc(lim)}</small></div></div>`).join("");
}

/* ---------- telemetry ---------- */
function lineChart({ title, series, yMax, limit, limitLabel, base, xCount, xl, fmtY }) {
  const W = 480, Hh = 150, L = 42, R = 10, T = 14, B = 18, iw = W - L - R, ih = Hh - T - B;
  const X = (i) => L + (xCount <= 1 ? 0 : (i / (xCount - 1)) * iw), Y = (v) => T + ih - Math.min(1, Math.max(0, v / yMax)) * ih;
  let g = [0, 0.5, 1].map((f) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${T + ih - f * ih}" y2="${T + ih - f * ih}"/><text x="${L - 4}" y="${T + ih - f * ih + 3}" text-anchor="end">${fmtY(yMax * f)}</text>`).join("");
  if (base != null) g += `<line x1="${L}" x2="${W - R}" y1="${Y(base)}" y2="${Y(base)}" stroke="#6b7f92" stroke-dasharray="3 3"/><text x="${W - R}" y="${Y(base) - 3}" text-anchor="end">baseline</text>`;
  if (limit != null) g += `<line x1="${L}" x2="${W - R}" y1="${Y(limit)}" y2="${Y(limit)}" stroke="#f87171" stroke-dasharray="5 3"/><text x="${W - R}" y="${Y(limit) - 3}" text-anchor="end" style="fill:#f87171">${esc(limitLabel)}</text>`;
  const lines = series.map((s) => {
    let d = "", pen = false;
    s.points.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)} `; pen = true; });
    const last = s.points.length - 1, lv = s.points[last];
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2"/>${lv != null ? `<circle cx="${X(last)}" cy="${Y(lv)}" r="3.4" fill="${s.color}"/>` : ""}`;
  }).join("");
  const key = series.length > 1 ? `<span>${series.map((s) => `<span style="color:${s.color}">● ${esc(s.name)}</span>`).join("  ")}</span>` : "";
  const empty = series.every((x) => x.points.every((v) => v == null)) ? `<text x="${W / 2}" y="${Hh / 2}" text-anchor="middle" style="fill:#8b9bab;font-size:11px">no samples in this window · start the fleet workload to see live data</text>` : "";
  return `<div class="chart"><h3><span>${esc(title)}</span>${key}</h3><svg viewBox="0 0 ${W} ${Hh}">${g}${lines}${empty}<text x="${L}" y="${Hh - 4}">${esc(xl[0])}</text><text x="${W - R}" y="${Hh - 4}" text-anchor="end">${esc(xl[1])}</text></svg></div>`;
}
function renderTelemetry() {
  const th = thresholds();
  let p95, werr, serr, fresh, xCount, xl, base, kp;
  if (sim) {
    p95 = sim.points.map((p) => p.p95); werr = sim.points.map((p) => p.werr * 100); serr = sim.points.map((p) => p.serr * 100); fresh = sim.points.map((p) => p.fresh);
    xCount = sim.xCount; xl = ["t+0s", `t+${(xCount - 1) * POLL_S}s`]; base = sim.base;
    const l = sim.last; kp = { w: 1 - l.werr, p95: l.p95, s: 1 - l.serr, f: l.fresh, live: true };
  } else {
    const pts = D.series || [];
    p95 = pts.map((p) => num(p.write_p95_ms)); werr = pts.map((p) => (p.write_success == null ? null : (1 - p.write_success) * 100)); serr = pts.map((p) => (p.search_success == null ? null : (1 - p.search_success) * 100)); fresh = pts.map((p) => num(p.freshness_p95_seconds));
    xCount = Math.max(pts.length, 2); xl = ["−5 min", "now"]; base = num(D.baseline?.write_p95_ms);
    const s = D.slis; kp = { w: s.write_samples ? 1 - (s.write_error_rate ?? 0) : null, p95: num(s.write_p95_ms), s: s.search_samples ? 1 - (s.search_error_rate ?? 0) : null, f: num(s.freshness_p95_seconds), live: s.live };
  }
  const lim = base != null ? base * th.write_p95_multiplier : null;
  const state = (v, okAt, warnAt) => (v == null ? "none" : v >= okAt ? "ok" : v >= warnAt ? "warn" : "bad");
  const stateHi = (v, okMax, warnMax) => (v == null ? "none" : v <= okMax ? "ok" : v <= warnMax ? "warn" : "bad");
  const sub = sim ? "simulated" : kp.live ? "last 30 s" : "no samples in the last 30 s";
  $("#kpis").innerHTML = [
    ["Write success", kp.w == null ? "—" : fmt.pct(kp.w), state(kp.w, 0.99, 0.8)],
    ["Write p95", fmt.ms(kp.p95), lim == null || kp.p95 == null ? "none" : stateHi(kp.p95, lim * 0.5, lim)],
    ["Search success", kp.s == null ? "—" : fmt.pct(kp.s), state(kp.s, 0.99, 0.8)],
    ["Freshness p95", fmt.s(kp.f), stateHi(kp.f, th.freshness_p95_seconds * 0.2, th.freshness_p95_seconds)],
  ].map(([l, v, st]) => `<div class="kpi ${st}"><small>${l.toUpperCase()}</small><b>${esc(v)}</b><span>${esc(sub)}</span></div>`).join("");
  const yMaxP = Math.max(lim ?? 0, ...p95.filter((v) => v != null), 10) * 1.1;
  $("#charts").innerHTML =
    lineChart({ title: "Write p95 latency", series: [{ name: "p95", color: "#2dd4bf", points: p95 }], yMax: yMaxP, limit: lim, limitLabel: `kill at ${lim != null ? fmt.ms(lim) : "—"}`, base, xCount, xl, fmtY: (v) => `${Math.round(v)}ms` }) +
    lineChart({ title: "Error rate", series: [{ name: "write", color: "#60a5fa", points: werr }, { name: "search", color: "#fbbf24", points: serr }], yMax: 100, limit: th.write_error_rate * 100, limitLabel: `kill write > ${th.write_error_rate * 100}%`, xCount, xl, fmtY: (v) => `${Math.round(v)}%` }) +
    lineChart({ title: "Freshness p95 (staleness)", series: [{ name: "freshness", color: "#a78bfa", points: fresh }], yMax: Math.max(th.freshness_p95_seconds * 1.1, ...fresh.filter((v) => v != null)), limit: th.freshness_p95_seconds, limitLabel: `kill at ${th.freshness_p95_seconds}s`, xCount, xl, fmtY: (v) => `${Math.round(v)}s` });
}

/* ---------- AI ranking ---------- */
function chips(pred) { return Object.entries(pred || {}).map(([k, v]) => `<span class="chip ${esc(v)}">${esc(k)}: ${esc(v)}</span>`).join(""); }
function renderTabBody() {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  const el = $("#tab-body");
  if (tab === "next") {
    const cands = [...(D.plan?.candidates || [])].sort((a, b) => (b.selection_score ?? 0) - (a.selection_score ?? 0));
    if (!cands.length) { el.innerHTML = `<div class="empty">No plan yet. Capture a baseline, then plan: <code>python -m safety.cli baseline</code> → <code>plan &lt;baseline_id&gt;</code></div>`; return; }
    el.innerHTML = `<p class="hyp">The planner ranks every allowed fault by information gain and risk (${esc(D.plan.ranking_method)}). Higher score = test it sooner. Predicted SLI impact shown per fault.</p><div class="rank-list">${cands.map((c, i) => `<div class="rank"><div class="n">${i + 1}</div><div><h4>${esc(labelOf(c.action))} · ${esc(targetsOf(c.target_component).join(" + "))} <span class="sev ${c.risk_score > 0.25 ? "medium" : "info"}">risk ${esc(c.risk_score)}</span> <span class="chip">${esc(c.duration_seconds)} s</span> <span class="chip">${esc(c.approval_status)}</span> <button class="row-btn" data-sim="${esc(c.action)}|${esc(c.target_component)}" ${running() ? "disabled" : ""}>Simulate this</button></h4><div class="bar"><i style="width:${Math.round((c.selection_score ?? 0) * 100)}%"></i></div><div>${chips(c.predicted_slis)}</div><div class="hyp">${esc(c.hypothesis || "")}</div>${c.approval_status === "approved" ? `<div class="hyp">Operator command: <code>CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute ${esc(c.id)}</code></div>` : ""}</div></div>`).join("")}</div>`;
    el.querySelectorAll("[data-sim]").forEach((b) => b.addEventListener("click", () => {
      if (running()) return;
      const [a, t] = b.dataset.sim.split("|"); const s = buildScenarios().find((x) => x.action === a && x.target === t) || buildScenarios().find((x) => x.action === a);
      if (s) { selScenario = s.id; if (sim) sim = null; renderScenarios(); window.scrollTo({ top: 0, behavior: "smooth" }); startSim(); }
    }));
  } else if (tab === "findings") {
    const r = D.ranked || [];
    if (!r.length) { el.innerHTML = `<div class="empty">No scored runs yet.</div>`; return; }
    el.innerHTML = `<p class="hyp">Ranked by severity, then confidence. Measured facts come straight from run records. The fix is a <b>hypothesis</b> until a validation re-run confirms it.</p><div class="rank-list">${r.map((f) => `<div class="rank"><div class="n">${esc(f.rank)}</div><div><h4><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span> ${esc(labelOf(f.action))} · ${esc(targetsOf(f.target).join(" + "))} <span class="chip">${esc(f.run_id)}</span></h4><div>${esc(f.summary)}</div><div class="facts">${(f.measured_facts || []).map((m) => `<span class="fact"><small>${esc(m.fact)}</small> <b>${esc(typeof m.value === "number" ? Math.round(m.value * 1000) / 1000 : m.value)}</b></span>`).join("")}</div><div class="hyp"><b>Hypothesis${f.hypothesis?.ai_generated ? " (AI-written)" : ""}:</b> ${esc(f.hypothesis?.text)}</div><div class="hyp"><b>Validate by:</b> ${esc(f.validation_test)}</div></div></div>`).join("")}</div>`;
  } else {
    const runs = (D.runs || []).filter((r) => r.resilience_score && r.resilience_score.status === "complete");
    if (!runs.length) { el.innerHTML = `<div class="empty">No complete scores yet.</div>`; return; }
    el.innerHTML = `<p class="hyp">Score = weighted blend of availability 30%, latency 20%, recovery 20%, containment 15%, detection 15%. Every component is computed from the run record.</p><div class="score-grid">${runs.map((r) => {
      const s = r.resilience_score, c = s.score >= 80 ? "var(--ok)" : s.score >= 60 ? "var(--warn)" : "var(--bad)";
      return `<div class="score-card"><div class="ring" style="--v:${s.score};--c:${c}"><span>${esc(Math.round(s.score))}</span></div><div><strong>${esc(r.diagnosis?.action ? labelOf(r.diagnosis.action) : "run")} · ${esc(targetsOf(r.diagnosis?.target_component).join(" + "))}</strong> <span class="chip">${esc(r.status)}</span>${Object.entries(s.components).map(([k, v]) => `<div class="comp"><span>${esc(k)}</span><div class="bar"><i style="width:${Math.round(v * 100)}%"></i></div><span>${v.toFixed(2)}</span></div>`).join("")}<div class="hyp">${esc(r.run_id)}</div></div></div>`;
    }).join("")}</div>`;
  }
}

function renderAll() { renderPills(); renderGraph(); renderNodeCard(); renderScenarios(); renderRules(); renderTelemetry(); renderTabBody(); }
$("#btn-attack").addEventListener("click", () => { $("#btn-attack").textContent = "▶ Simulate attack"; startSim(); });
$("#btn-kill").addEventListener("click", () => fire("manual", "operator pressed the kill switch"));
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { tab = b.dataset.tab; renderTabBody(); } });
load();
setInterval(() => { if (!running()) load(); }, 15000);

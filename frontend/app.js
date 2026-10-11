// Ditto Resilience Workbench dashboard. Plain JS, no build step, served by the local API.
// Read-only except for the human actions the API already offers: refresh discovery, capture a
// baseline, plan experiments, and record an approval decision. Nothing here executes a fault;
// the API's execute endpoint always returns 503. "Replay" on the Runs page re-plays a recorded
// run in the browser and never touches a container.

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const num = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));

async function getJson(path, options) {
  const response = await fetch(path, options);
  let payload = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

const fmt = {
  pct: (v) => (num(v) === null ? "—" : `${(v * 100).toFixed(v >= 0.999 || v === 0 ? 0 : 1)}%`),
  ms: (v) => (num(v) === null ? "—" : `${Number(v).toFixed(v < 10 ? 1 : 0)} ms`),
  secs: (v) => {
    if (num(v) === null) return "—";
    return v < 1 ? `${Math.round(v * 1000)} ms` : `${Number(v).toFixed(v < 10 ? 1 : 0)} s`;
  },
  rel: (s) => (num(s) === null ? "—" : `${s < 0 ? "−" : "+"}${Math.abs(s).toFixed(1)} s`),
  clock: (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"),
  hm: (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"),
  date: (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"),
  ago: (iso) => {
    if (!iso) return "—";
    const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 60) return `${Math.round(seconds)} s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h ago`;
    return `${Math.round(seconds / 86400)} d ago`;
  },
  label: (value) => String(value ?? "").replaceAll("_", " "),
  sentence: (value) => { const s = fmt.label(value); return s.charAt(0).toUpperCase() + s.slice(1); },
  short: (id, n = 8) => String(id ?? "").slice(0, n),
};

// ------------------------------------------------------------------ icons (Lucide paths, ISC licence)
const ICONS = {
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  shieldCheck: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
  key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/><path d="m15.5 7.5 3 3L22 7l-3-3"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  box: '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  plug: '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/>',
  car: '<path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/><circle cx="7" cy="17" r="2"/><path d="M9 17h6"/><circle cx="17" cy="17" r="2"/>',
  server: '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01"/><path d="M6 18h.01"/>',
  terminal: '<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
  eye: '<path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"/><circle cx="12" cy="12" r="3"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>',
  fileCheck: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="m9 15 2 2 4-4"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  play: '<path d="M6 3l14 9-14 9z"/>',
  pause: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
  user: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="m16 11 2 2 4-4"/>',
  sparkles: '<path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.13-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0L14.06 8.5a2 2 0 0 0 1.44 1.44l6.13 1.58a.5.5 0 0 1 0 .96L15.5 14.06a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z"/>',
  share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.59 13.51 6.83 3.98"/><path d="m15.41 6.51-6.82 3.98"/>',
  gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
  route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
  rotate: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  octagon: '<path d="M2.59 16.73A2 2 0 0 1 2 15.31V8.69a2 2 0 0 1 .59-1.42l4.68-4.68A2 2 0 0 1 8.69 2h6.62a2 2 0 0 1 1.42.59l4.68 4.68A2 2 0 0 1 22 8.69v6.62a2 2 0 0 1-.59 1.42l-4.68 4.68a2 2 0 0 1-1.42.59H8.69a2 2 0 0 1-1.42-.59z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  copy: '<rect x="8" y="8" width="14" height="14" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  arrow: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
  flask: '<path d="M10 2v7.53a2 2 0 0 1-.21.9L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.07-10.12a2 2 0 0 1-.21-.9V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
};
const icon = (name, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
const svgIcon = (name, x, y, size = 18) => `<g class="ico" transform="translate(${x},${y}) scale(${size / 24})"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</g></g>`;

// ------------------------------------------------------------------ shared vocabulary
const chip = (text, kind = "", iconName = "") => `<span class="chip ${kind}">${iconName ? icon(iconName) : ""}${esc(text)}</span>`;
const ACTIONS = {
  restart_container: { label: "Restart", icon: "rotate", rollback: "start the container again" },
  stop_container: { label: "Stop", icon: "octagon", rollback: "start the container again" },
  pause_container: { label: "Pause", icon: "pause", rollback: "unpause the container" },
  cpu_throttle: { label: "CPU throttle", icon: "gauge", rollback: "restore the CPU quota" },
  traffic_burst: { label: "Traffic burst", icon: "zap", rollback: "stop the burst process" },
};
const SHORT_ACTION = { restart: "restart_container", stop: "stop_container", pause: "pause_container", throttle: "cpu_throttle", burst: "traffic_burst" };
const actionLabel = (action) => ACTIONS[action]?.label || fmt.sentence(action || "unknown action");

function runAction(run) {
  const diagnosis = run.diagnosis || {};
  if (diagnosis.action) return { action: diagnosis.action, target: diagnosis.target_component };
  const match = /^([a-z]+)-(.+?)-[0-9a-f]{16}$/.exec(run.experiment_id || "");
  return match ? { action: SHORT_ACTION[match[1]] || match[1], target: match[2] } : { action: null, target: null };
}
const isScored = (run) => !run.dry_run && !run.forced_test;
const approvedBy = (approval) => (/approved_by=([^;]+)/.exec(approval?.reason || "") || [])[1]?.trim() || null;
const approvalReason = (approval) => String(approval?.reason || "").replace(/approved_by=[^;]+;?\s*/, "");

let policyCache = null;
async function policy() {
  if (!policyCache) policyCache = await getJson("/api/v1/policy");
  return policyCache;
}

function tile({ cls = "", title, iconName = "", why = "", source = "", right = "", body = "", foot = "", id = "" }) {
  return `<section class="tile ${cls}" ${id ? `id="${id}"` : ""}>
    <div class="tile-head"><div><h2>${iconName ? icon(iconName) : ""}${esc(title)}</h2>${why ? `<p class="why">${esc(why)}</p>` : ""}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end">${right}${source ? `<span class="source">${source}</span>` : ""}</div></div>
    ${body}${foot ? `<div class="tile-foot">${foot}</div>` : ""}
  </section>`;
}
const emptyState = (title, detail) => `<div class="empty"><strong>${esc(title)}</strong>${detail}</div>`;

// Global SVG gradients, referenced by id from every chart, ring and map.
document.body.insertAdjacentHTML("afterbegin", `<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <linearGradient id="ringGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#007bc0"/><stop offset="1" stop-color="#2fc6c0"/></linearGradient>
  <linearGradient id="laneGrad" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#007bc0"/><stop offset="1" stop-color="#00a8b0"/></linearGradient>
  <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4fb0ec" stop-opacity="0.55"/><stop offset="1" stop-color="#4fb0ec" stop-opacity="0"/></linearGradient>
  <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#8b949c"/></marker>
  <marker id="arrow-hot" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#eff1f2"/></marker>
  <marker id="arrow-manual" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#d589ce"/></marker>
</defs></svg>`);
$("#safety-strip-icon").outerHTML = icon("shieldCheck");
$("#drawer-close").innerHTML = icon("x");

// ------------------------------------------------------------------ rings
function ring(value, { size = 40, stroke = 4, text = "", sub = "", cls = "" } = {}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = num(value) === null ? 0 : clamp(value, 0, 1);
  return `<svg class="ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">
    <circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}"/>
    <circle class="value ${cls}" cx="${size / 2}" cy="${size / 2}" r="${r}" stroke-width="${stroke}" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${c.toFixed(2)}" data-offset="${(c * (1 - v)).toFixed(2)}" transform="rotate(-90 ${size / 2} ${size / 2})"/>
    ${text ? `<text x="50%" y="${sub ? "47%" : "50%"}" text-anchor="middle" dominant-baseline="middle" font-size="${size / 3.6}">${esc(text)}</text>` : ""}
    ${sub ? `<text class="sub" x="50%" y="66%" text-anchor="middle" dominant-baseline="middle" font-size="${Math.max(11, size / 11)}">${esc(sub)}</text>` : ""}
  </svg>`;
}
function animateRings(root) {
  requestAnimationFrame(() => requestAnimationFrame(() => $$("circle.value[data-offset]", root).forEach((circle) => circle.setAttribute("stroke-dashoffset", circle.dataset.offset))));
}

// ------------------------------------------------------------------ charts
// One renderer for every time series: live SLIs (x = epoch seconds) and run polls (x = seconds from injection).
function chart(o) {
  const W = o.width || 520;
  const H = o.height || 150;
  const pad = { l: W < 400 ? 42 : 50, r: 10, t: (o.markers || []).length ? 36 : 24, b: 22 };
  const [x0, x1] = o.x;
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const sx = (x) => pad.l + ((x - x0) / ((x1 - x0) || 1)) * plotW;
  const pts = (o.points || []).filter((p) => num(p.y) !== null);
  const label = esc(o.label || "");
  if (!pts.length) {
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}: no samples">
      <line class="axis" x1="${pad.l}" x2="${W - pad.r}" y1="${H - pad.b}" y2="${H - pad.b}"/>
      <text class="chart-empty" x="${pad.l + plotW / 2}" y="${pad.t + plotH / 2}" text-anchor="middle">${esc(o.emptyText || "No samples in this window")}</text></svg>`;
  }
  const ys = pts.map((p) => p.y).sort((a, b) => a - b);
  const dataMax = ys[ys.length - 1];
  let top = o.yMax;
  if (top === undefined) {
    const p90 = ys[Math.floor((ys.length - 1) * 0.9)];
    top = Math.max(p90 * 1.35, o.band ? o.band.value * 1.6 : 0, 1e-6);
    if (o.kill && o.kill.value <= top * 4) top = Math.max(top, o.kill.value * 1.12);
    if (dataMax <= top * 2.5) top = Math.max(top, dataMax * 1.1);
  }
  const sy = (y) => pad.t + (1 - clamp(y, 0, top) / top) * plotH;
  const parts = [];
  for (const g of [0, 0.5, 1]) {
    parts.push(`<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${sy(top * g)}" y2="${sy(top * g)}"/><text x="${pad.l - 8}" y="${sy(top * g) + 4}" text-anchor="end">${esc(o.fmt(top * g))}</text>`);
  }
  if (o.evalWindow) {
    const a = clamp(sx(o.evalWindow[0]), pad.l, W - pad.r);
    const b = clamp(sx(o.evalWindow[1]), pad.l, W - pad.r);
    parts.push(`<rect class="eval" x="${a}" y="${pad.t}" width="${Math.max(0, b - a)}" height="${plotH}"/><text class="eval-label" x="${b - 4}" y="${pad.t + 13}" text-anchor="end">30 s window</text>`);
  }
  if (o.band && num(o.band.value) !== null && o.band.value <= top) {
    const by = sy(o.band.value);
    parts.push(`<rect class="band-fill" x="${pad.l}" y="${by}" width="${plotW}" height="${H - pad.b - by}"/><line class="band-edge" x1="${pad.l}" x2="${W - pad.r}" y1="${by}" y2="${by}"/><text class="band-label" x="${pad.l + 6}" y="${by - 5}">${esc(o.band.label)}</text>`);
  }
  if (o.kill && num(o.kill.value) !== null) {
    if (o.kill.value <= top) {
      const ky = sy(o.kill.value);
      parts.push(`<line class="kill" x1="${pad.l}" x2="${W - pad.r}" y1="${ky}" y2="${ky}"/><text class="kill-label" x="${pad.l + plotW * 0.55}" y="${o.kill.below ? ky + 14 : ky - 5}" text-anchor="middle">${esc(o.kill.label)}</text>`);
    } else {
      if (!(o.markers || []).length) parts.push(`<text class="kill-label" x="${W - pad.r}" y="${pad.t - 9}" text-anchor="end">${esc(o.kill.label)} · above this scale ↑</text>`);
    }
  }
  let lastLabelEnd = -Infinity;
  let row = 0;
  for (const marker of o.markers || []) {
    if (marker.x < x0 || marker.x > x1) continue;
    const mx = sx(marker.x);
    const width = marker.label.length * 6.4;
    row = mx < lastLabelEnd + 4 ? 1 - row : 0;
    const right = mx + width > W - pad.r;
    lastLabelEnd = right ? mx : mx + width;
    parts.push(`<line class="marker ${marker.kind}" x1="${mx}" x2="${mx}" y1="${pad.t - (row ? 8 : 20)}" y2="${H - pad.b}"/><text class="marker-label ${marker.kind}" x="${right ? mx - 3 : mx + 3}" y="${row ? pad.t - 2 : pad.t - 14}" text-anchor="${right ? "end" : "start"}">${esc(marker.label)}</text>`);
  }
  // Line and area, broken wherever a bucket has no samples.
  const segments = [];
  let current = [];
  for (const p of o.points || []) {
    if (num(p.y) === null) { if (current.length) segments.push(current); current = []; } else current.push(p);
  }
  if (current.length) segments.push(current);
  for (const seg of segments) {
    const d = seg.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ");
    if (seg.length > 1) parts.push(`<path class="area" d="${d} L${sx(seg[seg.length - 1].x).toFixed(1)},${H - pad.b} L${sx(seg[0].x).toFixed(1)},${H - pad.b} Z"/>`);
    parts.push(`<path class="line" d="${d}"/>`);
  }
  const breach = (y) => o.kill && num(o.kill.value) !== null && (o.kill.below ? y < o.kill.value : y > o.kill.value);
  if (pts.length <= 60) {
    for (const p of pts) parts.push(`<circle class="dot ${breach(p.y) ? "breach" : ""}" cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="${breach(p.y) ? 4 : 2.6}"><title>${esc(o.xFmt(p.x))}: ${esc(o.fmt(p.y))}</title></circle>`);
  }
  const clipped = pts.filter((p) => p.y > top);
  if (clipped.length) {
    const peak = clipped.reduce((a, b) => (b.y > a.y ? b : a));
    const px = sx(peak.x);
    parts.push(`<path class="clip-mark" d="M${px - 5},${pad.t + 6} L${px + 5},${pad.t + 6} L${px},${pad.t} Z"/><text x="${clamp(px + 8, pad.l, W - pad.r - 70)}" y="${pad.t + 12}" fill="#ffd84d">peak ${esc(o.fmt(peak.y))}</text>`);
  }
  if (num(o.cursor) !== null && o.cursor >= x0 && o.cursor <= x1) parts.push(`<line class="cursor" x1="${sx(o.cursor)}" x2="${sx(o.cursor)}" y1="${pad.t - 4}" y2="${H - pad.b}"/>`);
  const ticks = o.xTicks || [x0, (x0 + x1) / 2, x1];
  ticks.forEach((t, i) => parts.push(`<text x="${sx(t)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"}">${esc(o.xFmt(t))}</text>`));
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">
    <line class="axis" x1="${pad.l}" x2="${W - pad.r}" y1="${H - pad.b}" y2="${H - pad.b}"/>${parts.join("")}</svg>`;
}

// ------------------------------------------------------------------ system & dependency map
// One map, used on Overview, System map and Runs. Columns come from each node's kind in the
// manifest-derived graph; edges and their style come only from graph.edges.
const ROLES = {
  gateway: "API + authentication", policies: "Who may read/write each twin", things: "Vehicle twin state",
  "things-search": "Search index built from twin changes", connectivity: "External brokers, idle here",
  mongodb: "Persistence", "ditto-ui": "Ditto's own web UI, side tool",
  "fleet-workload": "Simulated vehicles: writes, search, live (SSE) updates",
  "workbench-api": "Serves this dashboard and its data", "operator-cli": "Runner, kill switch, rollback",
  watchdog: "Restores the target if the runner dies", "data-metrics": "SLI samples in data/metrics/*.jsonl",
};
const NODE_ICONS = {
  nginx: "globe", gateway: "key", policies: "lock", things: "box", "things-search": "search", connectivity: "plug",
  mongodb: "database", "ditto-ui": "monitor", "fleet-workload": "car", "workbench-api": "server",
  "operator-cli": "terminal", watchdog: "eye", "data-metrics": "file",
};
const VERBS = {
  http_telemetry_write: "HTTP + SSE", http_search_query: "HTTP + SSE", sse_subscription: "HTTP + SSE",
  compose_depends_on: "depends on", pekko_cluster_communication: "messages", configured_database_host: "persists to",
  documented_persistence: "persists to", documented_search_projection: "feeds index", writes_samples: "writes samples",
  reads_samples: "reads samples", http_recovery_check: "recovery check", arms_watchdog: "arms",
  docker_compose_action: "fault action", docker_compose_start: "restarts",
};
const PROVENANCE_RANK = { configured: 3, documented: 2, manual: 1 };
const SERVICE_ORDER = ["policies", "things", "things-search", "connectivity"];
const BAND_ORDER = ["data-metrics", "workbench-api", "operator-cli", "watchdog"];
const COLUMNS = ["Clients", "Entry", "API", "Ditto services", "Storage"];
const SLI_LETTERS = [["write", "W"], ["search", "S"], ["freshness", "F"]];

function roleOf(node) {
  if (node.kind === "proxy") {
    const port = (node.ports || []).find((p) => p.host_port);
    return port ? `Front door on ${port.host_ip || "127.0.0.1"}:${port.host_port}` : "Reverse proxy";
  }
  return ROLES[node.id] || node.labels?.purpose || node.labels?.ditto_role || fmt.label(node.kind);
}
const slisOf = (node) => String(node.labels?.workbench_slis || "").split(",").map((s) => s.trim()).filter(Boolean);

function columnOf(node) {
  if (node.source !== "compose") return node.kind === "load_generator" ? 0 : -1; // -1 = workbench band
  if (node.kind === "database") return 4;
  if (node.kind === "proxy" || node.id === "ditto-ui") return 1;
  if (node.id === "gateway" || String(node.labels?.ditto_role || "").toLowerCase() === "gateway") return 2;
  return 3;
}

function wrap(text, max) {
  const words = String(text).split(" ");
  const lines = [""];
  for (const word of words) {
    const line = lines[lines.length - 1];
    if ((line + " " + word).trim().length > max && line) lines.push(word); else lines[lines.length - 1] = (line + " " + word).trim();
  }
  if (lines.length > 2) { lines.length = 2; lines[1] = lines[1].replace(/.{0,2}$/, "…"); }
  return lines;
}

// SLI -> services carrying it, from each compose node's workbench_slis label, ordered by distance
// from the entry node along the graph's edges. This is a request path, not a per-hop measurement.
function sliPaths(graph) {
  const nodes = (graph.nodes || []).filter((node) => node.source === "compose");
  const ids = new Set(nodes.map((node) => node.id));
  const edges = (graph.edges || []).filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const labelled = nodes.filter((node) => slisOf(node).length);
  const hasIncoming = new Set(edges.map((edge) => edge.target));
  const depth = new Map(labelled.filter((node) => !hasIncoming.has(node.id)).map((node) => [node.id, 0]));
  for (let queue = [...depth.keys()]; queue.length;) {
    const current = queue.shift();
    for (const edge of edges) if (edge.source === current && !depth.has(edge.target)) { depth.set(edge.target, depth.get(current) + 1); queue.push(edge.target); }
  }
  const paths = {};
  for (const node of labelled) for (const sli of slisOf(node)) (paths[sli] ||= []).push(node.id);
  for (const list of Object.values(paths)) list.sort((a, b) => (depth.get(a) ?? 99) - (depth.get(b) ?? 99) || a.localeCompare(b));
  return paths;
}

function mapModel(graph, { band = true } = {}) {
  const NW = 160, NH = 92, STEP = 244, ROW = 126, TOP = 58;
  const nodes = (graph.nodes || []).filter((node) => band || columnOf(node) !== -1);
  const pos = new Map();
  const cols = [[], [], [], [], []];
  const bandNodes = [];
  for (const node of nodes) { const c = columnOf(node); if (c === -1) bandNodes.push(node); else cols[c].push(node); }
  cols[3].sort((a, b) => (SERVICE_ORDER.indexOf(a.id) + 1 || 99) - (SERVICE_ORDER.indexOf(b.id) + 1 || 99) || a.id.localeCompare(b.id));
  cols[1].sort((a, b) => (a.kind === "proxy" ? -1 : 0) - (b.kind === "proxy" ? -1 : 0));
  const rows = Math.max(...cols.map((c) => c.length), 1);
  const mid = TOP + ((rows - 1) * ROW) / 2;
  cols.forEach((list, c) => list.forEach((node, i) => {
    const y = c === 3 || list.length === rows ? TOP + i * ROW : mid + (i - (list.length - 1) / 2) * ROW;
    pos.set(node.id, { x: 16 + c * STEP, y, w: NW, h: NH, col: c, row: i });
  }));
  // Fleet workload sits level with the entry proxy so the request edge runs straight.
  const proxy = cols[1].find((n) => n.kind === "proxy");
  for (const node of cols[0]) if (proxy) pos.get(node.id).y = pos.get(proxy.id).y;
  const coreBottom = TOP + (rows - 1) * ROW + NH;
  let height = coreBottom + 24;
  let bandBox = null;
  if (bandNodes.length) {
    bandNodes.sort((a, b) => (BAND_ORDER.indexOf(a.id) + 1 || 99) - (BAND_ORDER.indexOf(b.id) + 1 || 99));
    const by = coreBottom + 64;
    bandBox = { x: 4, y: by - 34, w: 16 + 4 * STEP + NW + 8, h: NH + 50 };
    bandNodes.forEach((node, i) => pos.set(node.id, { x: 16 + i * STEP, y: by, w: NW, h: NH, col: -1, row: i }));
    height = bandBox.y + bandBox.h + 8;
  }
  // Merge parallel edges between the same pair; keep every relation and provenance for tooltips.
  const merged = new Map();
  for (const edge of graph.edges || []) {
    if (!pos.has(edge.source) || !pos.has(edge.target)) continue;
    const key = `${edge.source}>${edge.target}`;
    const entry = merged.get(key) || { source: edge.source, target: edge.target, relations: new Set(), provenances: new Set(), sources: new Set() };
    entry.relations.add(edge.relation);
    entry.provenances.add(edge.provenance_type);
    if (edge.provenance) entry.sources.add(edge.provenance);
    merged.set(key, entry);
  }
  const edges = [...merged.values()].map((e) => {
    const best = [...e.provenances].sort((a, b) => (PROVENANCE_RANK[b] || 0) - (PROVENANCE_RANK[a] || 0))[0];
    const runtime = [...e.relations].filter((r) => r !== "compose_depends_on");
    const a = pos.get(e.source);
    const b = pos.get(e.target);
    return {
      ...e, provenance: best, verb: VERBS[runtime[0]] || fmt.label(runtime[0] || "depends on"),
      startupOnly: runtime.length === 0, sameColumn: a.col === b.col && a.col !== -1,
      control: a.col === -1 || b.col === -1,
    };
  });
  return { nodes, pos, edges, height, width: 16 + 4 * STEP + NW + 16, bandBox, NW, NH, STEP, TOP };
}

function edgeGeometry(a, b) {
  const ac = { x: a.x + a.w / 2, y: a.y + a.h / 2 };
  const bc = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
  if (a.col === -1 || b.col === -1) {
    if (a.col === -1 && b.col === -1) {
      const forward = b.x > a.x;
      const x1 = forward ? a.x + a.w : a.x; const x2 = forward ? b.x : b.x + b.w;
      return { d: `M${x1},${ac.y} L${x2},${bc.y}`, mid: { x: (x1 + x2) / 2, y: ac.y } };
    }
    const up = a.y > b.y; // band node -> core node goes upward
    const y1 = up ? a.y : a.y + a.h; const y2 = up ? b.y + b.h : b.y;
    const k = Math.abs(y2 - y1) / 2;
    return { d: `M${ac.x},${y1} C${ac.x},${y1 + (up ? -k : k)} ${bc.x},${y2 + (up ? k : -k)} ${bc.x},${y2}`, mid: { x: (ac.x + bc.x) / 2, y: (y1 + y2) / 2 } };
  }
  if (a.col === b.col) {
    if (Math.abs(a.row - b.row) === 1) {
      const down = b.y > a.y;
      const y1 = down ? a.y + a.h : a.y; const y2 = down ? b.y : b.y + b.h;
      return { d: `M${ac.x},${y1} L${bc.x},${y2}`, mid: { x: ac.x, y: (y1 + y2) / 2 }, vertical: true };
    }
    const x = a.x + a.w;
    return { d: `M${x},${ac.y} C${x + 46},${ac.y} ${x + 46},${bc.y} ${x},${bc.y}`, mid: { x: x + 36, y: (ac.y + bc.y) / 2 } };
  }
  const forward = b.x > a.x;
  const x1 = forward ? a.x + a.w : a.x; const x2 = forward ? b.x : b.x + b.w;
  const bend = Math.abs(x2 - x1) / 2;
  return { d: `M${x1},${ac.y} C${x1 + (forward ? bend : -bend)},${ac.y} ${x2 - (forward ? bend : -bend)},${bc.y} ${x2},${bc.y}`, mid: { x: (x1 + x2) / 2, y: (ac.y + bc.y) / 2 }, x1, x2, y1: ac.y, y2: bc.y };
}

// Edge labels go in the gutters between columns, never on cards. Fans that share a source (or a
// target) and a verb get one label with a count instead of one label per edge.
function edgeLabels(model, visible) {
  const labels = [];
  const used = new Set();
  const group = (keyFn) => {
    const groups = new Map();
    for (const e of visible) if (!used.has(e) && !e.control && !e.sameColumn) { const k = keyFn(e); (groups.get(k) || groups.set(k, []).get(k)).push(e); }
    return groups;
  };
  for (const [, list] of group((e) => `${e.source}|${e.verb}`)) {
    if (list.length < 2) continue;
    const a = model.pos.get(list[0].source);
    labels.push({ x: a.x + a.w + 8, y: a.y + a.h / 2 - 9, anchor: "start", text: `${list[0].verb} ×${list.length}`, edges: list });
    list.forEach((e) => used.add(e));
  }
  for (const [, list] of group((e) => `${e.target}|${e.verb}`)) {
    if (list.length < 2) continue;
    const b = model.pos.get(list[0].target);
    labels.push({ x: b.x - 8, y: b.y + b.h / 2 - 9, anchor: "end", text: `${list[0].verb} ×${list.length}`, edges: list });
    list.forEach((e) => used.add(e));
  }
  for (const e of visible) {
    if (used.has(e)) continue;
    const g = edgeGeometry(model.pos.get(e.source), model.pos.get(e.target));
    if (g.vertical) labels.push({ x: g.mid.x + 8, y: g.mid.y + 4, anchor: "start", text: e.verb, edges: [e] });
    else labels.push({ x: g.mid.x, y: g.mid.y - 7, anchor: "middle", text: e.verb, edges: [e] });
  }
  return labels;
}

function renderMap(container, graph, limits, opts = {}) {
  const model = mapModel(graph, { band: opts.band !== false });
  if (!model.nodes.length) { container.innerHTML = emptyState("No dependency map yet", "<p>Run <code>python -m ai.dependency_map</code>, then refresh discovery.</p>"); return; }
  const { pos, NW, NH } = model;
  const allowed = new Set(limits?.allowed_targets || []);
  const denied = new Set(limits?.denied_targets || []);
  const visible = model.edges.filter((e) => (!e.control || opts.showControl) && (!(e.startupOnly && e.sameColumn) || opts.showStartup));
  const hiddenStartup = model.edges.filter((e) => e.startupOnly && e.sameColumn).length;
  const parts = [];
  COLUMNS.forEach((label, c) => parts.push(`<text class="col-label" x="${16 + c * model.STEP}" y="22">${esc(label)}</text>`));
  if (model.bandBox) {
    const b = model.bandBox;
    parts.push(`<rect class="zone zone-wb" x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="12"/><text class="zone-label zone-wb-label" x="${b.x + 12}" y="${b.y + 20}">Workbench · outside Ditto · tools that observe and control the experiment</text>`);
  }
  // Request lane overlay (drawn under the cards).
  let laneNodes = new Set();
  if (opts.lane) {
    const path = ["fleet-workload", ...(sliPaths(graph)[opts.lane] || [])].filter((id) => pos.has(id));
    laneNodes = new Set(path);
    const pts = path.map((id) => { const p = pos.get(id); return { x: p.x + p.w / 2, y: p.y + p.h / 2, col: p.col, left: p.x }; });
    let d = "";
    pts.forEach((p, i) => {
      if (!i) { d = `M${p.x},${p.y}`; return; }
      const q = pts[i - 1];
      if (q.col === p.col) { const gx = p.left - 18; d += ` L${gx},${q.y} L${gx},${p.y} L${p.x},${p.y}`; } else d += ` C${(q.x + p.x) / 2},${q.y} ${(q.x + p.x) / 2},${p.y} ${p.x},${p.y}`;
    });
    parts.push(`<path class="lane-path" d="${d}"/><path class="lane-pulse" d="${d}"/>`);
  }
  for (const e of model.edges) {
    const isVisible = visible.includes(e);
    const g = edgeGeometry(pos.get(e.source), pos.get(e.target));
    const relations = [...e.relations].map(fmt.label).join(", ");
    parts.push(`<path class="edge ${esc(e.provenance)} ${e.startupOnly ? "startup" : ""} ${isVisible ? "" : "off"}" data-s="${esc(e.source)}" data-t="${esc(e.target)}" d="${g.d}" marker-end="url(#${e.provenance === "manual" ? "arrow-manual" : "arrow"})"><title>${esc(e.source)} → ${esc(e.target)} · ${esc(relations)} · ${esc([...e.provenances].join(" + "))}${e.sources.size ? ` · ${esc([...e.sources].join(", "))}` : ""}</title></path>`);
  }
  for (const label of edgeLabels(model, visible)) {
    const w = label.text.length * 6.6 + 8;
    const bx = label.anchor === "start" ? label.x - 3 : label.anchor === "end" ? label.x - w + 3 : label.x - w / 2;
    const ends = label.edges.flatMap((e) => [e.source, e.target]).join(" ");
    parts.push(`<g class="edge-label" data-ends="${esc(ends)}"><rect class="edge-label-bg" x="${bx}" y="${label.y - 11}" width="${w}" height="16" rx="4" opacity="0.85"/><text x="${label.x}" y="${label.y + 1}" text-anchor="${label.anchor}">${esc(label.text)}</text></g>`);
  }
  const fault = opts.fault || null;
  const impacted = new Set(fault?.impacted || []);
  for (const node of model.nodes) {
    const p = pos.get(node.id);
    const wb = p.col === -1;
    const slis = slisOf(node);
    const role = wrap(roleOf(node), 23);
    const pips = node.source !== "compose" ? "" : SLI_LETTERS.map(([sli, letter], i) => `<g class="sli-pip ${slis.includes(sli) ? "on" : ""}" transform="translate(${NW - 62 + i * 18},${NH - 24})"><rect width="16" height="16" rx="4"/><text x="8" y="12" text-anchor="middle">${letter}</text><title>${esc(sli)} SLI ${slis.includes(sli) ? "passes through here" : "does not pass here"}</title></g>`).join("");
    let badge = "";
    if (opts.policyOverlay !== false && !(fault && fault.target === node.id)) {
      if (allowed.has(node.id)) badge = `<g class="badge testable" transform="translate(0,-20)"><rect width="66" height="16"/><text x="33" y="12" text-anchor="middle">testable</text></g>`;
      if (denied.has(node.id)) badge = `<g class="badge protected" transform="translate(0,-20)"><rect width="72" height="16"/><text x="36" y="12" text-anchor="middle">protected</text></g>`;
    }
    let overlay = "";
    if (fault && fault.target === node.id) {
      const w = Math.min(fault.label.length * 6.7 + 16, 300);
      overlay = `<rect class="ring-fault" x="-5" y="-5" width="${NW + 10}" height="${NH + 10}" rx="12"/><g class="fault-chip" transform="translate(-5,-30)"><rect width="${w}" height="20" rx="5"/><text x="8" y="14">${esc(fault.label)}</text></g>`;
    } else if (impacted.has(node.id)) {
      overlay = `<rect class="ring-impact" x="-4" y="-4" width="${NW + 8}" height="${NH + 8}" rx="11"/>`;
    }
    parts.push(`<g class="node ${wb ? "wb" : ""} ${laneNodes.has(node.id) ? "lane" : ""}" data-id="${esc(node.id)}" transform="translate(${p.x},${p.y})" tabindex="0" role="button" aria-label="${esc(node.id)}: ${esc(roleOf(node))}">
      ${overlay}<rect class="card" width="${NW}" height="${NH}" rx="9"/>${svgIcon(NODE_ICONS[node.id] || (wb ? "server" : "box"), 12, 11)}
      <text class="name" x="38" y="25">${esc(node.id)}</text>${pips}
      ${role.map((line, i) => `<text class="role" x="12" y="${47 + i * 15}">${esc(line)}</text>`).join("")}${badge}</g>`);
  }
  const lanePath = opts.lane ? (sliPaths(graph)[opts.lane] || []) : [];
  container.innerHTML = `<div class="map-wrap"><svg class="map" viewBox="0 0 ${model.width} ${model.height}" role="img" aria-label="System and dependency map: ${model.nodes.length} components">${parts.join("")}</svg></div>
    <div class="inspector" aria-live="polite">${opts.lane ? `<span>${icon("route")} <strong>${esc(fmt.sentence(opts.lane))} request path:</strong> ${["fleet-workload", ...lanePath].map(esc).join(" → ")}</span><span class="muted">Only the end-to-end p95 is measured; there is no per-hop latency.</span>${opts.laneNote ? `<span>${opts.laneNote}</span>` : ""}`
      : `<span class="muted">${icon("info")} Hover or focus a component to see what it touches and what fails with it. Click for details.${hiddenStartup && !opts.showStartup ? ` ${hiddenStartup} startup-order edges inside Ditto services are hidden.` : ""}</span>`}</div>`;
  const svg = $("svg.map", container);
  const inspector = $(".inspector", container);
  const resting = inspector.innerHTML;
  const neighbours = (id) => new Set(model.edges.filter((e) => e.source === id || e.target === id).flatMap((e) => [e.source, e.target]));
  const focusNode = (id) => {
    const near = neighbours(id);
    svg.classList.add("dim");
    $$(".node", svg).forEach((n) => n.classList.toggle("hot", near.has(n.dataset.id) || n.dataset.id === id));
    $$(".edge", svg).forEach((e) => {
      const hot = e.dataset.s === id || e.dataset.t === id;
      e.classList.toggle("hot", hot);
      e.setAttribute("marker-end", `url(#${hot ? "arrow-hot" : e.classList.contains("manual") ? "arrow-manual" : "arrow"})`);
    });
    $$(".edge-label", svg).forEach((l) => l.classList.toggle("hot", l.dataset.ends.split(" ").includes(id)));
    inspector.innerHTML = nodeSummary(graph, id, limits);
  };
  const clearFocus = () => {
    svg.classList.remove("dim");
    $$(".hot", svg).forEach((el) => el.classList.remove("hot"));
    $$(".edge", svg).forEach((e) => e.setAttribute("marker-end", `url(#${e.classList.contains("manual") ? "arrow-manual" : "arrow"})`));
    inspector.innerHTML = resting;
  };
  $$(".node", svg).forEach((n) => {
    n.addEventListener("mouseenter", () => focusNode(n.dataset.id));
    n.addEventListener("focus", () => focusNode(n.dataset.id));
    n.addEventListener("mouseleave", clearFocus);
    n.addEventListener("blur", clearFocus);
    n.addEventListener("click", () => openDrawer(graph, n.dataset.id, limits));
    n.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDrawer(graph, n.dataset.id, limits); } });
  });
}

function impactOf(graph, id) {
  return [...new Set((graph.impact_paths?.[id] || []).map((p) => p.impacted_component).filter((c) => c !== id))];
}

function nodeSummary(graph, id, limits) {
  const node = (graph.nodes || []).find((n) => n.id === id) || {};
  const port = (node.ports || []).filter((p) => p.host_port).map((p) => `${p.host_ip}:${p.host_port}`).join(", ");
  const impact = impactOf(graph, id);
  const policyTag = (limits?.allowed_targets || []).includes(id) ? chip("testable", "info", "target") : (limits?.denied_targets || []).includes(id) ? chip("protected", "bad", "lock") : "";
  return `<span><strong>${esc(id)}</strong> · ${esc(roleOf(node))}</span>
    ${node.image ? `<span class="mono">${esc(node.image.replace("docker.io/", ""))}</span>` : node.labels?.module ? `<span class="mono">${esc(node.labels.module)}</span>` : ""}
    ${port ? `<span>port ${esc(port)}</span>` : ""}
    ${slisOf(node).length ? `<span>SLIs: ${slisOf(node).map(esc).join(", ")}</span>` : ""}${policyTag}
    ${node.source === "compose" ? `<span class="impact">${icon("alert", "sm")} If this fails: ${impact.length ? impact.map(esc).join(", ") : "nothing else in the graph"}</span>` : ""}`;
}

function openDrawer(graph, id, limits) {
  const node = (graph.nodes || []).find((n) => n.id === id);
  if (!node) return;
  const edges = (graph.edges || []).filter((e) => e.source === id || e.target === id);
  const impact = graph.impact_paths?.[id] || [];
  const ports = (node.ports || []).filter((p) => p.host_port).map((p) => `${p.host_ip}:${p.host_port} → ${p.container_port}`).join(", ");
  $("#drawer-title").innerHTML = `${icon(NODE_ICONS[id] || "box")} ${esc(id)}`;
  $("#drawer-body").innerHTML = `
    <p>${esc(roleOf(node))}</p>
    <div class="kv">
      <div><span>Source</span><strong>${esc(node.source)}</strong></div>
      <div><span>Kind</span><strong>${esc(fmt.label(node.kind))}</strong></div>
      ${node.image ? `<div style="grid-column:1/-1"><span>Image (pinned)</span><strong class="mono">${esc(node.image)}</strong></div>` : ""}
      ${node.labels?.module ? `<div style="grid-column:1/-1"><span>Module</span><strong class="mono">${esc(node.labels.module)}</strong></div>` : ""}
      <div><span>Published port</span><strong>${esc(ports || "none")}</strong></div>
      <div><span>SLIs carried</span><strong>${esc(slisOf(node).join(", ") || "none")}</strong></div>
      ${node.health_url ? `<div style="grid-column:1/-1"><span>Health check</span><strong class="mono">${esc(node.health_url)}</strong></div>` : ""}
      <div><span>Fault policy</span><strong>${(limits?.allowed_targets || []).includes(id) ? "testable (allowlisted)" : (limits?.denied_targets || []).includes(id) ? "protected (denied)" : "not a fault target"}</strong></div>
    </div>
    <div><h3>If this fails (from impact_paths)</h3>${impact.length > 1 ? `<ul>${impact.filter((p) => p.impacted_component !== id).map((p) => `<li><strong>${esc(p.impacted_component)}</strong> <span class="muted">via ${p.path.map(esc).join(" → ")}</span></li>`).join("")}</ul>` : '<p class="muted">Nothing else in the graph depends on it.</p>'}</div>
    <div><h3>Edges (${edges.length})</h3><ul>${edges.map((e) => `<li>${esc(e.source)} → ${esc(e.target)} · ${esc(fmt.label(e.relation))} <span class="muted">· ${esc(e.provenance_type)}${e.provenance ? ` · ${esc(e.provenance)}` : ""}</span></li>`).join("")}</ul></div>`;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-close").focus();
}
const closeDrawer = () => { $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); };
$("#drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });

// Toolbar shared by every map instance: request lanes and edge toggles.
function mapToolbar(prefix, state, { lanes = true, toggles = true } = {}) {
  const lane = (key, text) => `<button type="button" data-lane="${key}" aria-pressed="${state.lane === key}">${text}</button>`;
  return `<div class="map-toolbar">
    ${lanes ? `<span class="label">Follow a request</span><div class="seg" role="group" aria-label="Follow a request">${lane("", "Off")}${lane("write", "Write")}${lane("search", "Search")}${lane("freshness", "Freshness")}</div>` : ""}
    ${toggles ? `<label class="toggle"><input type="checkbox" data-toggle="showStartup" ${state.showStartup ? "checked" : ""}>Startup order</label>
    <label class="toggle"><input type="checkbox" data-toggle="showControl" ${state.showControl ? "checked" : ""}>Workbench control edges</label>` : ""}
    <div class="legend" style="margin-left:auto"><span><i></i>configured</span><span><i class="documented"></i>documented</span><span><i class="manual"></i>manual (workbench)</span></div>
  </div>`;
}
function bindToolbar(root, state, redraw) {
  $$("[data-lane]", root).forEach((b) => b.addEventListener("click", () => { state.lane = b.dataset.lane; redraw(); }));
  $$("[data-toggle]", root).forEach((c) => c.addEventListener("change", () => { state[c.dataset.toggle] = c.checked; redraw(); }));
}

// ------------------------------------------------------------------ router and polling
const VIEWS = ["overview", "architecture", "experiments", "runs", "safety"];
let pollTimer = null;
const replay = { playing: false };

function route() {
  const [, view = "overview", detail] = (location.hash || "#/overview").split("/");
  const active = VIEWS.includes(view) ? view : "overview";
  for (const name of VIEWS) $(`#view-${name}`).hidden = name !== active;
  $$(".tabs a").forEach((link) => { link.classList.toggle("active", link.dataset.view === active); link.toggleAttribute("aria-current", link.dataset.view === active); });
  clearInterval(pollTimer);
  stopReplay();
  closeDrawer();
  if (route.last !== active) window.scrollTo(0, 0);
  route.last = active;
  const loaders = { overview: loadOverview, architecture: loadArchitecture, experiments: loadExperiments, runs: () => loadRuns(detail), safety: loadSafety };
  loaders[active]().catch(showError);
  if (active === "overview" || active === "safety") {
    pollTimer = setInterval(() => { if (!replay.playing && !document.hidden) loaders[active]().catch(showError); }, 15000);
  } else {
    getJson("/api/v1/slis?window=30").then(setLiveChip).catch(() => {});
  }
}

function showError(error) {
  console.error(error);
  const view = $(`#view-${route.last || "overview"}`);
  if (view && !view.innerHTML.trim()) view.innerHTML = emptyState("Could not reach the workbench API", `<p>${esc(error.message)}. Start it with <code>python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000</code>.</p>`);
  $("#footer-updated").textContent = `Last error: ${error.message}`;
}

function setLiveChip(slis) {
  const el = $("#live-chip");
  el.classList.toggle("live", !!slis.live);
  el.classList.toggle("stale", !slis.live);
  $("span", el).textContent = slis.live ? "Live · 127.0.0.1" : slis.latest_sample_at ? `No live samples · last ${fmt.ago(slis.latest_sample_at)}` : "No live samples · local";
}
const stamp = () => { $("#footer-updated").textContent = `Updated ${fmt.clock(new Date().toISOString())}`; };

// ------------------------------------------------------------------ overview
const overviewMap = { lane: "", showStartup: false, showControl: false };
let overviewStage = null;
let handoffPlayed = false;

async function loadOverview() {
  const [health, graph, slis, series, baselines, plans, runs, ranked, watchdog, limits] = await Promise.all([
    getJson("/api/v1/health"), getJson("/api/v1/dependency-map"), getJson("/api/v1/slis?window=30"),
    getJson("/api/v1/slis/series?minutes=5&bucket=10"), getJson("/api/v1/baselines?limit=5"),
    getJson("/api/v1/experiments?limit=20"), getJson("/api/v1/runs?limit=50"), getJson("/api/v1/findings/ranked"),
    getJson("/api/v1/watchdog?limit=5"), policy(),
  ]);
  setLiveChip(slis);
  const baseline = baselines.baselines[0] || null;
  const scoredRuns = sortRuns(runs.runs).filter(isScored);
  const latestRun = sortRuns(runs.runs)[0] || null;
  const data = { health, graph, slis, series: series.points || [], baseline, plans: plans.plans || [], runs: sortRuns(runs.runs), ranked: ranked.ranked || [], watchdog: watchdog.events || [], limits };
  const view = $("#view-overview");
  const stages = workflowStages(data);
  if (!overviewStage || !stages.find((s) => s.key === overviewStage)) overviewStage = (stages.find((s) => s.active) || stages[stages.length - 1]).key;
  view.innerHTML = `
    <div class="page-head"><div><p class="eyebrow">Eclipse Ditto ${esc(versionOf(graph))} · local Docker Compose</p><h1 id="h-overview">System overview</h1>
      <p>Map the twin backend, measure it under a simulated fleet, let the planner rank safe experiments, and let a human decide. Only the control plane executes.</p></div></div>
    ${pipelineTile(stages, data)}
    <div class="bento" style="margin-top:16px">${sliTiles(data)}</div>
    <div class="bento">
      <div class="c7">${latestRunTile(scoredRuns[0] || latestRun, data)}</div>
      <div class="c5">${baselineTile(data)}</div>
    </div>
    ${tile({ cls: "", title: "System and dependency map", iconName: "share", why: "Every component and edge comes from the Compose manifest, Ditto's docs or the workbench itself. Nothing is inferred.", source: `graph <code>${esc(fmt.short(graph.graph_version))}</code> · ${(graph.nodes || []).length} components`, body: `${mapToolbar("ov", overviewMap)}<div id="overview-map"></div>` })}`;
  const drawMap = () => {
    $$("[data-lane]", view).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lane === overviewMap.lane)));
    renderMap($("#overview-map"), graph, limits, { ...overviewMap, band: overviewMap.showControl, laneNote: laneNote(overviewMap.lane, slis, baseline) });
  };
  bindToolbar(view, overviewMap, drawMap);
  drawMap();
  bindPipeline(view, stages, data);
  animateRings(view);
  stamp();
}

function laneNote(lane, slis, baseline) {
  if (!lane) return "";
  const live = slis.live;
  if (lane === "write") return live ? `End-to-end write p95 now <strong>${esc(fmt.ms(slis.write_p95_ms))}</strong> (live)` : baseline ? `Baseline write p95 <strong>${esc(fmt.ms(baseline.write_p95_ms))}</strong> (recorded)` : "";
  if (lane === "freshness") return live ? `End-to-end freshness p95 now <strong>${esc(fmt.secs(slis.freshness_p95_seconds))}</strong> (live)` : baseline ? `Baseline freshness p95 <strong>${esc(fmt.secs(baseline.freshness_p95_seconds))}</strong> (recorded)` : "";
  if (lane === "search") return live && slis.search_error_rate !== null ? `Search success now <strong>${esc(fmt.pct(1 - slis.search_error_rate))}</strong> (live)` : "";
  return "";
}

const versionOf = (graph) => ((graph.nodes || []).find((n) => n.id === "gateway")?.image || "").split(":")[1] || "";
const sortRuns = (runs) => [...(runs || [])].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));

function workflowStages(d) {
  const plan = d.plans[0];
  const candidates = d.plans.flatMap((p) => (p.candidates || []).map((c) => ({ ...c, plan_id: p.plan_id })));
  const approvals = candidates.filter((c) => c.approval && c.approval.decision === "approve").sort((a, b) => Date.parse(b.approval.created_at) - Date.parse(a.approval.created_at));
  const latestApproval = approvals[0];
  const run = d.runs.find(isScored) || d.runs[0];
  const findings = run ? d.ranked.filter((f) => f.run_id === run.run_id) : [];
  const fresh = d.baseline && (Date.now() - Date.parse(d.baseline.captured_at)) / 1000 <= d.limits.baseline_max_age_seconds;
  const faultRunning = d.watchdog[0]?.event === "armed";
  const stages = [
    { key: "topology", band: "advises", kind: "agent", iconName: "share", title: "Topology", who: "Agent 1",
      state: d.graph.status === "available" ? "done" : "waiting",
      artifact: d.graph.graph_version ? `Graph <code>${esc(fmt.short(d.graph.graph_version))}</code> · ${(d.graph.nodes || []).length} nodes · ${(d.graph.edges || []).length} edges` : "No graph yet",
      inputs: "Compose manifest, Ditto architecture docs", outputs: "Versioned dependency graph and impact paths", mustnot: "Never invents a service or an edge without a manifest source", owner: "backend/app/agents/discovery.py, mapping.py" },
    { key: "measure", band: "advises", kind: "agent", iconName: "gauge", title: "Measure", who: "Agent 2",
      state: d.slis.live ? "working" : fresh ? "done" : d.baseline ? "stale" : "waiting",
      artifact: d.baseline ? `Baseline · ${esc(d.baseline.write_samples ?? "?")} writes · ${esc(fmt.ago(d.baseline.captured_at))}${d.slis.live ? ` · workload live, ${esc(d.slis.write_samples)} writes/30 s` : ""}` : d.slis.live ? `Workload live · ${esc(d.slis.write_samples)} writes/30 s` : "No baseline yet",
      inputs: "Graph ID, simulated fleet workload", outputs: "120 s healthy baseline for write, search and freshness", mustnot: "Missing samples are never reported as healthy", owner: "simulator/, monitor/" },
    { key: "planner", band: "advises", kind: "agent", iconName: "route", title: "Planner", who: "Agent 3",
      state: plan ? "done" : "waiting",
      artifact: plan ? `Plan · ${(plan.candidates || []).length} candidates · ${esc(fmt.ago(plan.created_at))}` : "No plan yet",
      inputs: "Graph, baseline, run history, policy allowlist", outputs: "Ranked candidates with per-SLI predictions", mustnot: "Agent 3 never executes anything", owner: "backend/app/agents/planning.py" },
    { key: "approval", band: "decides", kind: "human", iconName: "user", title: "Approval", who: "Human operator",
      state: latestApproval ? "done" : plan ? "human" : "waiting",
      artifact: latestApproval ? `${esc(approvedBy(latestApproval.approval) || "approved")} · ${Date.parse(latestApproval.approval.expires_at) > Date.now() ? "expires" : "expired"} ${esc(fmt.hm(latestApproval.approval.expires_at))}` : plan ? "Waiting for a typed approval" : "Nothing to approve yet",
      inputs: "One candidate and its hash", outputs: `Single-use approval, expires after ${esc(d.limits.approval_expires_after_minutes)} min`, mustnot: "An approval never runs anything by itself", owner: "POST /api/v1/experiments/{id}/approval" },
    { key: "control", band: "executes", kind: "control", iconName: "terminal", title: "Control plane", who: "Code, not an agent",
      state: faultRunning ? "working" : run ? "done" : "waiting",
      artifact: faultRunning ? `Fault running now · watchdog armed on ${esc(d.watchdog[0].target)}` : run ? `<code>${esc(run.run_id)}</code> · ${esc(actionLabel(runAction(run).action))} ${esc(run.action_params?.duration_seconds ?? "")} s` : "No run yet",
      inputs: "Approved, immutable plan", outputs: "Run record, rollback, recovery check", mustnot: "Only predefined actions on allowlisted targets", owner: "safety/ (operator CLI only)" },
    { key: "impact", band: "advises", kind: "agent", iconName: "activity", title: "Impact", who: "Agent 2",
      state: run ? "done" : "waiting",
      artifact: run ? (run.status === "aborted" ? `Kill switch fired · recovered in ${esc(fmt.secs(run.recovery_time_seconds))}` : run.recovery_verified ? `Recovered in ${esc(fmt.secs(run.recovery_time_seconds))}` : "Recovery not verified") : "No impact measured yet",
      inputs: "One 30 s SLI window per 2 s poll", outputs: "Observed impact and recovery time", mustnot: "Missing samples are never reported as healthy", owner: "monitor/" },
    { key: "report", band: "advises", kind: "agent", iconName: "fileCheck", title: "Report", who: "Agent 3",
      state: run && (findings.length || num(run.resilience_score?.score) !== null) ? "done" : "waiting",
      artifact: run ? `Findings ${findings.length} · score ${esc(run.resilience_score?.score ?? "—")}` : "No findings yet",
      inputs: "Prediction vs observation, measured facts", outputs: "Ranked findings, resilience score", mustnot: "The optional LLM may only reword text", owner: "ai/" },
  ];
  const firstOpen = stages.findIndex((s) => s.state !== "done");
  if (firstOpen >= 0) stages[firstOpen].active = true;
  return stages;
}

const STAGE_STATE = {
  done: ["Done", "ok", "check"], working: ["Working", "info", "activity"], waiting: ["Waiting", "", "clock"],
  human: ["Waiting for human", "warn", "user"], stale: ["Baseline stale", "warn", "clock"],
};

function pipelineTile(stages, d) {
  const live = stages.some((s) => s.state === "working");
  const bands = [["advises", "Advises", "agents", 3], ["decides", "Decides", "human", 1], ["executes", "Executes", "control plane only", 1], ["advises", "Advises", "agents", 2]];
  return tile({
    cls: "hero", title: "Agent workflow", iconName: "route",
    why: "Three agents advise, one human decides, and only the deterministic control plane executes.",
    right: live ? chip("Live", "info", "activity") : chip("Replay of last cycle · recorded", "outline", "clock"),
    body: `<div class="pipeline">
      <div class="bands">${bands.map(([cls, name, sub, span]) => `<div class="band ${cls}" style="grid-column:span ${span}">${esc(name)} <small>${esc(sub)}</small></div>`).join("")}</div>
      <div class="stages">${stages.map((s, i) => {
        const [text, kind, ic] = STAGE_STATE[s.state];
        return `<button type="button" class="stage k-${s.kind} ${s.active ? "active" : ""} ${s.key === overviewStage ? "selected" : ""}" data-stage="${s.key}" aria-describedby="stage-detail">
          <div class="stage-top"><span class="stage-icon">${icon(s.iconName)}</span>${chip(text, kind, ic)}</div>
          <h3>${i + 1}. ${esc(s.title)}</h3><span class="who">${esc(s.who)}</span>
          ${s.active ? '<div class="progress"><i></i></div>' : ""}
          <span class="artifact">${s.artifact}</span>
          ${i < stages.length - 1 ? '<span class="connector"><span class="token"></span></span>' : ""}
        </button>`;
      }).join("")}</div>
      <div class="stage-detail" id="stage-detail"></div></div>`,
  });
}

function bindPipeline(root, stages, d) {
  const detail = $("#stage-detail", root);
  const show = (key) => {
    const s = stages.find((x) => x.key === key);
    overviewStage = key;
    $$(".stage", root).forEach((el) => el.classList.toggle("selected", el.dataset.stage === key));
    detail.innerHTML = `<div><span>${esc(s.title)} · ${esc(s.who)}</span>${s.artifact}</div><div><span>Takes in</span>${esc(s.inputs)}</div><div><span>Hands on</span>${esc(s.outputs)}</div><div><span>Must not</span><span class="mustnot">${icon("octagon", "sm")} ${esc(s.mustnot)}</span><span class="mono" style="margin-top:4px">${esc(s.owner)}</span></div>`;
  };
  $$(".stage", root).forEach((el) => {
    el.addEventListener("mouseenter", () => show(el.dataset.stage));
    el.addEventListener("focus", () => show(el.dataset.stage));
    el.addEventListener("click", () => show(el.dataset.stage));
  });
  show(overviewStage);
  if (!handoffPlayed) {
    handoffPlayed = true;
    const until = stages.findIndex((s) => s.active);
    const last = until < 0 ? stages.length - 1 : until;
    $$(".connector", root).slice(0, last).forEach((c, i) => setTimeout(() => c.classList.add("handoff"), 250 + i * 420));
  }
}

function sliTiles(d) {
  const { slis, series, baseline, limits } = d;
  const t = limits.abort_thresholds;
  const now = Date.now() / 1000;
  const xs = series.map((p) => Date.parse(p.t) / 1000);
  const x = [xs[0] ?? now - 300, now];
  const xFmt = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const evalWindow = [now - t.window_seconds, now];
  const live = slis.live;
  const liveChip = live ? chip("Live", "info") : chip("No live samples", "warn");
  const noSamples = { kind: "bad", text: "No samples · unhealthy", icon: "alert" };
  const emptyText = live ? "No samples in this bucket range" : "Workload stopped · start simulator/fleet_workload.py";
  const size = { width: 300, height: 128 };
  const pts = (key, transform = (v) => v) => series.map((p, i) => ({ x: xs[i], y: num(p[key]) === null ? null : transform(p[key]) }));
  const baseP95 = baseline?.write_p95_ms ?? null;
  const killP95 = baseP95 !== null ? baseP95 * t.write_p95_multiplier : null;
  const writeOk = num(slis.write_error_rate) === null ? null : 1 - slis.write_error_rate;
  const searchOk = num(slis.search_error_rate) === null ? null : 1 - slis.search_error_rate;
  const status = (value, breach, okText = "Healthy") => (value === null ? noSamples : breach ? { kind: "bad", text: "Over kill limit", icon: "octagon" } : { kind: "ok", text: okText, icon: "check" });
  const tiles = [
    {
      title: "Write success", why: "Telemetry writes that Ditto accepted", value: fmt.pct(writeOk), raw: writeOk,
      sub: slis.write_samples ? `${slis.write_samples} writes in the last ${t.window_seconds} s` : "",
      status: status(writeOk, writeOk !== null && writeOk < 1 - t.write_error_rate),
      chart: chart({ ...size, label: "Write success, last 5 minutes", x, points: pts("write_success"), yMax: 1, fmt: (v) => fmt.pct(v), xFmt, evalWindow, kill: { value: 1 - t.write_error_rate, label: `Kill below ${fmt.pct(1 - t.write_error_rate)}`, below: true }, emptyText }),
      source: "write.jsonl",
    },
    {
      title: "Write latency (p95)", why: "How long a successful telemetry write takes", value: fmt.ms(slis.write_p95_ms), raw: slis.write_p95_ms,
      sub: baseP95 !== null ? `baseline ${fmt.ms(baseP95)} · kill at ${t.write_p95_multiplier}×` : "no baseline yet: kill limit unknown",
      status: status(num(slis.write_p95_ms), killP95 !== null && slis.write_p95_ms > killP95),
      chart: chart({ ...size, label: "Write p95, last 5 minutes", x, points: pts("write_p95_ms"), fmt: (v) => fmt.ms(v), xFmt, evalWindow, band: baseP95 !== null ? { value: baseP95, label: `baseline ${fmt.ms(baseP95)}` } : null, kill: killP95 !== null ? { value: killP95, label: `Kill at ${fmt.ms(killP95)}` } : null, emptyText }),
      source: "write.jsonl",
    },
    {
      title: "Search success", why: "Fleet search queries that returned. Not a kill signal: search is the expected impact.", value: fmt.pct(searchOk), raw: searchOk,
      sub: slis.search_samples ? `${slis.search_samples} searches in the last ${t.window_seconds} s` : "",
      status: searchOk === null ? noSamples : { kind: searchOk >= 0.99 ? "ok" : "warn", text: searchOk >= 0.99 ? "Healthy" : "Degraded", icon: searchOk >= 0.99 ? "check" : "alert" },
      chart: chart({ ...size, label: "Search success, last 5 minutes", x, points: pts("search_success"), yMax: 1, fmt: (v) => fmt.pct(v), xFmt, evalWindow, emptyText }),
      source: "search.jsonl", extra: chip("not a kill signal", "outline"),
    },
    {
      title: "Data freshness", why: "How stale a twin change is when it arrives on the live (SSE) stream", value: fmt.secs(slis.freshness_p95_seconds), raw: slis.freshness_p95_seconds,
      sub: slis.freshness_samples ? `p95 of ${slis.freshness_samples} changes · ${slis.freshness_method === "sse" ? "SSE" : slis.freshness_method || ""}` : "",
      status: status(num(slis.freshness_p95_seconds), slis.freshness_p95_seconds > t.freshness_p95_seconds),
      chart: chart({ ...size, label: "Freshness p95, last 5 minutes", x, points: pts("freshness_p95_seconds"), fmt: (v) => fmt.secs(v), xFmt, evalWindow, band: num(baseline?.freshness_p95_seconds) !== null ? { value: baseline.freshness_p95_seconds, label: `baseline ${fmt.secs(baseline.freshness_p95_seconds)}` } : null, kill: { value: t.freshness_p95_seconds, label: `Kill at ${fmt.secs(t.freshness_p95_seconds)}` }, emptyText }),
      source: "freshness.jsonl",
    },
  ];
  return tiles.map((s) => `<section class="tile c3 ${s.status.kind === "bad" ? "t-red" : ""}">
      <div class="tile-head" style="margin-bottom:6px"><div><h2>${esc(s.title)}</h2><p class="why">${esc(s.why)}</p></div></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${chip(s.status.text, s.status.kind, s.status.icon)}${liveChip}${s.extra || ""}</div>
      <div class="sli-value"><strong class="${s.raw === null || s.raw === undefined ? "none" : ""}">${esc(s.value)}</strong></div>
      <div class="sli-sub">${esc(s.sub)}</div>
      ${s.chart}
      <div class="tile-foot"><span>Last 5 min · 10 s buckets</span><span class="source">${icon("file", "sm")}<code>data/metrics/${esc(s.source)}</code></span></div>
    </section>`).join("");
}

function headline(run, limits, baselineP95Override) {
  const { action, target } = runAction(run);
  const t = limits.abort_thresholds;
  const tl = run.timeline || {};
  const inj = Date.parse(tl.injected_at || run.created_at);
  const at = (iso) => (iso ? (Date.parse(iso) - inj) / 1000 : null);
  const faults = (run.observations || []).filter((o) => o.phase === "fault");
  const worst = (key) => faults.reduce((m, o) => (num(o[key]) !== null && (m === null || o[key] > m) ? o[key] : m), null);
  const recovery = run.recovery_verified ? `Rolled back, recovered in <b>${esc(fmt.secs(run.recovery_time_seconds))}</b>.` : "Recovery <b>not verified</b>.";
  if (run.dry_run) return `Dry run of ${esc(actionLabel(action))} on ${esc(target)}: nothing was changed. Dry runs are never scored or ranked.`;
  if (run.status === "aborted") {
    const base = baselineP95Override ?? run.baseline_write_p95_ms;
    const last = faults[faults.length - 1] || {};
    const reason = String(run.abort_reason || "");
    let why = esc(fmt.label(reason));
    if (/p95|latency/.test(reason) && num(last.write_p95_ms) !== null && num(base) !== null) why = `write latency <b>${esc(fmt.ms(last.write_p95_ms))}</b> passed the <b>${esc(fmt.ms(base * t.write_p95_multiplier))}</b> limit (${esc(t.write_p95_multiplier)}× baseline ${esc(fmt.ms(base))})`;
    else if (/error/.test(reason) && num(last.write_error_rate) !== null) why = `write errors <b>${esc(fmt.pct(last.write_error_rate))}</b> passed the <b>${esc(fmt.pct(t.write_error_rate))}</b> limit`;
    else if (/fresh/.test(reason) && num(last.freshness_p95_seconds) !== null) why = `freshness <b>${esc(fmt.secs(last.freshness_p95_seconds))}</b> passed the <b>${esc(fmt.secs(t.freshness_p95_seconds))}</b> limit`;
    else if (/sample|telemetry/.test(reason)) why = "too few samples in the 30 s window, which counts as unhealthy";
    const prefix = run.forced_test ? "Forced kill-switch test: " : "";
    return `${prefix}${esc(actionLabel(action))} on ${esc(target)}. Kill switch fired at <b>${esc(fmt.rel(at(tl.abort_at)))}</b>: ${why}. ${recovery}${run.forced_test ? " Aborted by design; not scored." : ""}`;
  }
  return `${esc(actionLabel(action))} on ${esc(target)} for ${esc(run.action_params?.duration_seconds ?? "?")} s. Worst write errors <b>${esc(fmt.pct(worst("write_error_rate")))}</b>, worst search errors <b>${esc(fmt.pct(worst("search_error_rate")))}</b>, worst write p95 <b>${esc(fmt.ms(worst("write_p95_ms")))}</b>. ${recovery}`;
}

function scoreRingFor(run, size = 120) {
  const score = run.resilience_score || {};
  if (!isScored(run)) return ring(0, { size, stroke: 9, text: "—", sub: "not scored" });
  if (num(score.score) === null) return ring(0, { size, stroke: 9, text: "—", sub: fmt.label(score.status || "not scored") });
  return ring(score.score / 100, { size, stroke: 9, text: Number(score.score).toFixed(1), sub: score.status === "complete" ? "of 100" : "incomplete" });
}

function latestRunTile(run, d) {
  if (!run) return tile({ title: "Latest run", iconName: "flask", why: "The most recent experiment the control plane ran.", body: emptyState("No runs recorded yet", "<p>Approve a proposal under Experiments, then run it from the operator CLI.</p>") });
  const { action, target } = runAction(run);
  return tile({
    cls: run.status === "aborted" ? "t-red" : "t-teal", title: "Latest run", iconName: "flask",
    why: "The most recent scored experiment, in plain words.",
    source: `run <code>${esc(run.run_id)}</code>`,
    right: chip(run.status === "aborted" ? "Kill switch fired" : fmt.sentence(run.status), run.status === "aborted" ? "bad" : "ok", run.status === "aborted" ? "octagon" : "check"),
    body: `<div class="score-layout"><div>${scoreRingFor(run)}</div><div><p class="run-headline">${headline(run, d.limits)}</p>
      <div style="display:flex;gap:6px;margin-top:12px;flex-wrap:wrap">${chip(`${actionLabel(action)} · ${target}`, "outline", ACTIONS[action]?.icon || "zap")}${run.diagnosis?.prediction_accuracy !== undefined ? chip(`Prediction accuracy ${fmt.pct(run.diagnosis.prediction_accuracy)}`, "outline") : ""}${chip(fmt.date(run.created_at), "outline", "clock")}</div></div></div>`,
    foot: `<span>Recorded run</span><a href="#/runs/${encodeURIComponent(run.run_id)}">Open and replay ${icon("arrow", "sm")}</a>`,
  });
}

function baselineTile(d) {
  const b = d.baseline;
  if (!b) return tile({ title: "Healthy baseline", iconName: "gauge", why: "The steady state every experiment is judged against.", body: emptyState("No baseline captured yet", "<p>Start the workload, wait 120 s, then capture one under Experiments.</p>") });
  const age = (Date.now() - Date.parse(b.captured_at)) / 1000;
  const stale = age > d.limits.baseline_max_age_seconds;
  return tile({
    cls: stale ? "t-yellow" : "t-teal", title: "Healthy baseline", iconName: "gauge",
    why: "The steady state every experiment is judged against.",
    right: stale ? chip(`Stale · older than ${Math.round(d.limits.baseline_max_age_seconds / 60)} min`, "warn", "clock") : chip("Fresh", "ok", "check"),
    body: `<div class="kv">
      <div><span>Write p95</span><strong>${esc(fmt.ms(b.write_p95_ms))}</strong></div>
      <div><span>Write errors</span><strong>${esc(fmt.pct(b.write_error_rate))}</strong></div>
      <div><span>Search errors</span><strong>${esc(fmt.pct(b.search_error_rate))}</strong></div>
      <div><span>Freshness p95</span><strong>${esc(fmt.secs(b.freshness_p95_seconds))} · ${esc(b.freshness_method === "sse" ? "SSE" : b.freshness_method || "?")}</strong></div>
      <div><span>Samples</span><strong>${esc(b.write_samples ?? "?")} writes · ${esc(Math.round(b.window?.seconds ?? 0))} s</strong></div>
      <div><span>Captured</span><strong>${esc(fmt.ago(b.captured_at))}</strong></div></div>`,
    foot: `<span class="source">baseline <code>${esc(b.baseline_id)}</code></span><a href="#/experiments">Experiments ${icon("arrow", "sm")}</a>`,
  });
}

// ------------------------------------------------------------------ system map view
const archMap = { lane: "", showStartup: false, showControl: false };
async function loadArchitecture() {
  const [graph, limits, slis, baselines] = await Promise.all([getJson("/api/v1/dependency-map"), policy(), getJson("/api/v1/slis?window=30"), getJson("/api/v1/baselines?limit=1")]);
  setLiveChip(slis);
  const view = $("#view-architecture");
  const edges = graph.edges || [];
  const count = (type) => edges.filter((e) => e.provenance_type === type).length;
  const runtime = edges.filter((e) => e.runtime_observed).length;
  const paths = sliPaths(graph);
  const services = (graph.nodes || []).filter((n) => n.source === "compose");
  view.innerHTML = `
    <div class="page-head"><div><p class="eyebrow">Agent 1 · topology and evidence</p><h1 id="h-architecture">System map</h1>
      <p>One map for the whole system: Ditto's services from the Compose manifest, and the workbench tools around them. Edge style shows where each edge comes from.</p></div>
      <div class="actions"><button id="refresh-button" class="button" type="button">${icon("refresh")}Refresh discovery</button></div></div>
    <div id="refresh-message"></div>
    ${tile({ cls: "hero", title: "System and dependency map", iconName: "share", why: `${(graph.nodes || []).length} components and ${edges.length} edges: ${count("configured")} configured, ${count("documented")} documented, ${count("manual")} manual, ${runtime} observed at runtime.`, source: `graph <code>${esc(graph.graph_version || "—")}</code> · ${esc(fmt.ago(graph.created_at))}`, body: `${mapToolbar("arch", archMap)}<div id="arch-map"></div>` })}
    <div class="bento">
      ${tile({ cls: "c5", title: "SLI request paths", iconName: "route", why: "Which services each measured signal passes through. From the workbench_slis labels in the Compose file.", body: `<div class="table-wrap"><table class="table"><thead><tr><th>SLI</th><th>Path</th></tr></thead><tbody>${Object.entries(paths).map(([sli, path]) => `<tr><td><strong>${esc(fmt.sentence(sli))}</strong></td><td>${path.map(esc).join(" → ")}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">No SLI labels in the manifest.</td></tr>'}</tbody></table></div>`, foot: "<span>Only end-to-end latency is measured. There is no per-hop timing.</span>" })}
      ${tile({ cls: "c7", title: "Services", iconName: "server", why: "Every Compose service, its pinned image and what it is exposed on.", source: `${(graph.source_files || []).map((f) => `<code>${esc(String(f.path || f).split("/").slice(-2).join("/"))}</code>`).slice(0, 1).join("")}`, body: `<div class="table-wrap"><table class="table"><thead><tr><th>Service</th><th>Image</th><th>Published</th><th>SLIs</th><th>Fault policy</th></tr></thead><tbody>${services.map((node) => {
        const ports = (node.ports || []).filter((p) => p.host_port).map((p) => `${p.host_ip}:${p.host_port}→${p.container_port}`).join(", ") || "—";
        const pol = limits.allowed_targets.includes(node.id) ? chip("testable", "info", "target") : limits.denied_targets.includes(node.id) ? chip("protected", "bad", "lock") : '<span class="muted">—</span>';
        return `<tr><td><strong>${esc(node.id)}</strong></td><td><code>${esc((node.image || "").replace("docker.io/", ""))}</code></td><td>${esc(ports)}</td><td>${esc(slisOf(node).join(", ") || "—")}</td><td>${pol}</td></tr>`;
      }).join("")}</tbody></table></div>` })}
    </div>`;
  const draw = () => {
    $$("[data-lane]", view).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lane === archMap.lane)));
    renderMap($("#arch-map"), graph, limits, { ...archMap, laneNote: laneNote(archMap.lane, slis, baselines.baselines[0]) });
  };
  bindToolbar(view, archMap, draw);
  draw();
  $("#refresh-button").addEventListener("click", async () => {
    const button = $("#refresh-button");
    button.disabled = true;
    try { await getJson("/api/v1/discovery/refresh", { method: "POST" }); await loadArchitecture(); } catch (error) { $("#refresh-message").innerHTML = `<div class="notice bad" style="margin-bottom:16px">${esc(error.message)}</div>`; } finally { button.disabled = false; }
  });
  stamp();
}

// ------------------------------------------------------------------ experiments
let selectedCandidate = null;
async function loadExperiments() {
  const [baselines, plans, limits, runs, graph] = await Promise.all([getJson("/api/v1/baselines?limit=10"), getJson("/api/v1/experiments?limit=20"), policy(), getJson("/api/v1/runs?limit=50"), getJson("/api/v1/dependency-map")]);
  const view = $("#view-experiments");
  const allBaselines = baselines.baselines || [];
  const fresh = allBaselines.filter((b) => (Date.now() - Date.parse(b.captured_at)) / 1000 <= limits.baseline_max_age_seconds);
  const planList = plans.plans || [];
  const plan = planList[0];
  const candidates = plan ? [...(plan.candidates || [])].sort((a, b) => (b.selection_score ?? 0) - (a.selection_score ?? 0)) : [];
  const allCandidates = planList.flatMap((p) => (p.candidates || []).map((c) => ({ ...c, plan })));
  const activeApproval = allCandidates.find((c) => c.approval_status === "approved" && Date.parse(c.approval?.expires_at) > Date.now());
  const runList = sortRuns(runs.runs);
  if (!selectedCandidate || !allCandidates.some((c) => c.id === selectedCandidate)) selectedCandidate = (activeApproval || candidates[0] || {}).id || null;
  const ranForActive = activeApproval && runList.find((r) => r.experiment_id === activeApproval.id && Date.parse(r.created_at) > Date.parse(activeApproval.approval.created_at));
  const steps = [
    { n: 1, title: "Baseline", cls: fresh.length ? "done" : "wait", text: fresh.length ? `${fresh.length} fresh · ${esc(fmt.ago(fresh[0].captured_at))}` : allBaselines.length ? "All baselines stale" : "Capture one" },
    { n: 2, title: "Plan", cls: plan ? "done" : "wait", text: plan ? `${candidates.length} candidates · ${esc(plan.ranking_method || "")}` : "Plan from a fresh baseline" },
    { n: 3, title: "Human approval", cls: activeApproval ? "done" : plan ? "wait" : "", text: activeApproval ? `${esc(approvedBy(activeApproval.approval) || "approved")} · expires ${esc(fmt.hm(activeApproval.approval.expires_at))}` : "Typed, single use, expires" },
    { n: 4, title: "Dry run", cls: "cli", text: "Operator CLI · changes nothing" },
    { n: 5, title: "Execute", cls: ranForActive ? "done" : "cli", text: ranForActive ? `Ran as ${esc(ranForActive.run_id)}` : "Operator CLI only" },
  ];
  view.innerHTML = `
    <div class="page-head"><div><p class="eyebrow">Agent 3 plans · a human decides</p><h1 id="h-experiments">Experiments</h1>
      <p>The planner ranks safe, allowlisted experiments from the graph, a fresh baseline and past runs. You approve one; only the operator CLI can run it.</p></div></div>
    <div class="steps">${steps.map((s) => `<div class="step ${s.cls}"><span class="n">${s.cls === "done" ? icon("check", "sm") : s.n}</span><div><b>${esc(s.title)}</b><small>${s.text}</small></div></div>`).join("")}</div>
    <section class="tile" style="margin-top:16px"><div class="kpis">
      <div class="kpi"><b>${fresh.length}<span style="font-size:15px;color:var(--text-3)"> / ${allBaselines.length}</span></b><span>Baselines fresh (under ${Math.round(limits.baseline_max_age_seconds / 60)} min)</span></div>
      <div class="kpi"><b>${candidates.length}</b><span>Candidates in the latest plan</span></div>
      <div class="kpi"><b>${esc(limits.approval_expires_after_minutes)} min</b><span>Approval window, single use</span></div>
      <div class="kpi"><b style="color:var(--red-fg)">503</b><span>API execute, by design</span></div></div></section>
    <div class="bento">
      ${tile({ cls: "c6", title: "1 · Capture a baseline", iconName: "gauge", why: "Built from the latest live workload run only. Needs at least 120 s of samples and every SLI healthy.", right: `<button id="baseline-button" class="button" type="button">${icon("gauge")}Capture baseline</button>`, body: `<div id="baseline-message"></div><div class="row-list">${allBaselines.length ? allBaselines.slice(0, 5).map((b) => {
        const stale = (Date.now() - Date.parse(b.captured_at)) / 1000 > limits.baseline_max_age_seconds;
        return `<div class="row-item"><div><b class="mono">${esc(b.baseline_id)}</b><small>${esc(b.write_samples ?? "?")} writes · p95 ${esc(fmt.ms(b.write_p95_ms))} · errors ${esc(fmt.pct(b.write_error_rate))} · freshness ${esc(fmt.secs(b.freshness_p95_seconds))} (${esc(b.freshness_method || "?")})</small></div><div style="text-align:right">${stale ? chip("Stale", "warn", "clock") : chip("Fresh", "ok", "check")}<small>${esc(fmt.ago(b.captured_at))}</small></div></div>`;
      }).join("") : emptyState("No baselines yet", "<p>Start the workload (<code>python simulator/fleet_workload.py --duration-seconds 900</code>), wait 120 s, then capture.</p>")}</div>` })}
      ${tile({ cls: "c6", title: "2 · Plan experiments", iconName: "route", why: "The planner proposes only allowlisted actions and targets from the policy file. Planning never executes.", body: `<div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap"><label class="field" style="flex:1;min-width:220px">Baseline<select id="baseline-select">${allBaselines.length ? allBaselines.map((b) => `<option value="${esc(b.baseline_id)}">${esc(b.baseline_id)} · ${esc(fmt.ago(b.captured_at))}${(Date.now() - Date.parse(b.captured_at)) / 1000 > limits.baseline_max_age_seconds ? " (stale)" : ""}</option>`).join("") : '<option value="">Capture a baseline first</option>'}</select></label>
        <button id="plan-button" class="button button-primary" type="button">${icon("route")}Plan experiments</button></div>
        <div id="plan-results" style="margin-top:12px">${plan ? `<p class="muted">Latest plan <code>${esc(plan.plan_id)}</code> · ${esc(fmt.ago(plan.created_at))} · ranked by <code>${esc(plan.ranking_method || "?")}</code></p>${(plan.policy_rejections || []).map((r) => `<p class="muted" style="margin-top:6px">${icon("lock", "sm")} Not planned: <strong>${esc(r.target)}</strong> · ${esc(r.reason)}</p>`).join("")}` : '<p class="muted">Pick a baseline and plan.</p>'}</div>
        <div class="tile-foot"><span>Allowed targets: ${limits.allowed_targets.map(esc).join(", ")}</span><span>Protected: ${limits.denied_targets.map(esc).join(", ")}</span></div>` })}
    </div>
    <div class="bento">
      ${tile({ cls: "c7 hero", title: "3 · Ranked proposals", iconName: "flask", why: "Ranked by the planner. Predictions are per SLI: hold, degrade or fail.", source: plan ? `plan <code>${esc(plan.plan_id)}</code>` : "", body: candidates.length ? `<div class="cand-list"><div class="cand cand-head" aria-hidden="true"><span>#</span><span>Experiment</span><span class="h-dur">Time</span><span class="h-risk">Risk</span><span class="h-score">Score</span><span>W · S · F</span><span class="h-status">Status</span></div>${candidates.map((c, i) => candidateRow(c, i, limits, runList)).join("")}</div>` : emptyState("No proposals yet", "<p>Capture a fresh baseline, then plan.</p>") })}
      <div class="c5" id="candidate-detail"></div>
    </div>
    ${planList.length > 1 ? `<section class="tile"><details class="earlier"><summary>Earlier plans (${planList.length - 1}) and their decisions</summary><div class="row-list">${planList.slice(1).flatMap((p) => (p.candidates || []).map((c) => `<div class="row-item"><div><b>${esc(actionLabel(c.action))} · ${esc(c.target_component)} · ${esc(c.duration_seconds)} s</b><small class="mono">${esc(c.id)} · ${esc(p.plan_id)} · ${esc(fmt.ago(p.created_at))}</small>${c.approval ? `<small>${esc(c.approval.decision)} by ${esc(approvedBy(c.approval) || "?")} · ${esc(approvalReason(c.approval))}</small>` : ""}</div><div>${statusChip(c, runList)}</div></div>`)).join("")}</div></details></section>` : ""}`;
  const all = [...candidates, ...planList.slice(1).flatMap((p) => p.candidates || [])];
  const renderDetail = () => {
    const c = all.find((x) => x.id === selectedCandidate);
    $("#candidate-detail").innerHTML = c ? candidateDetail(c, graph, limits, runList, plan) : tile({ title: "Proposal", body: emptyState("Nothing selected", "<p>Pick a proposal to see why it was ranked and what it predicts.</p>") });
    $$(".cand[data-id]", view).forEach((row) => row.classList.toggle("selected", row.dataset.id === selectedCandidate));
    animateRings($("#candidate-detail"));
  };
  $$(".cand[data-id]", view).forEach((row) => row.addEventListener("click", () => { selectedCandidate = row.dataset.id; renderDetail(); }));
  renderDetail();
  bindExperimentActions();
  stamp();
}

function statusChip(c, runs) {
  const expires = c.approval?.expires_at ? Date.parse(c.approval.expires_at) : null;
  const ran = runs.find((r) => r.experiment_id === c.id);
  if (c.approval_status === "approved" && ran) return chip("Ran", "ok", "check");
  if (c.approval_status === "approved" && expires && expires < Date.now()) return chip("Expired", "", "clock");
  if (c.approval_status === "approved") return chip("Approved", "ok", "check");
  if (c.approval_status === "rejected" || c.approval?.decision === "reject") return chip("Rejected", "bad", "x");
  return chip("Pending", "warn", "user");
}

// Prediction pills pair colour with an icon and a word (or the SLI letter in compact rows).
const PILL_ICON = { hold: "check", degrade: "alert", fail: "x" };
const pill = (state, name, compact = false) => {
  const known = ["hold", "degrade", "fail"].includes(state);
  const text = compact ? name.charAt(0).toUpperCase() : known ? fmt.sentence(state) : "Unknown";
  return `<span class="pill ${known ? state : "unknown"}" title="${esc(fmt.sentence(name))}: ${esc(state || "unknown")}">${known ? icon(PILL_ICON[state]) : ""}${esc(text)}</span>`;
};

function candidateRow(c, i, limits, runs) {
  const p = c.predicted_slis || {};
  const maxRisk = limits.max_risk_score || 1;
  return `<button type="button" class="cand" data-id="${esc(c.id)}">
    <span class="rank">${i + 1}</span>
    <span class="what"><b>${icon(ACTIONS[c.action]?.icon || "zap", "sm")} ${esc(actionLabel(c.action))} · ${esc(c.target_component)}</b><small>${esc(fmt.short(c.id.replace(/-[0-9a-f]{16}$/, ""), 28))}</small></span>
    <span class="dur">${esc(c.duration_seconds)} s</span>
    <span class="meter risk" title="Risk ${esc(c.risk_score)} of max ${esc(maxRisk)}"><span class="bar"><i style="width:${clamp((c.risk_score / maxRisk) * 100, 0, 100)}%"></i></span>${esc(c.risk_score)}</span>
    <span class="meter score" title="Selection score"><span class="bar"><i style="width:${clamp((c.selection_score ?? 0) * 100, 0, 100)}%"></i></span>${esc(Number(c.selection_score ?? 0).toFixed(2))}</span>
    <span class="pills">${pill(p.write, "write", true)}${pill(p.search, "search", true)}${pill(p.freshness, "freshness", true)}</span>
    <span class="status-cell">${statusChip(c, runs)}</span>
  </button>`;
}

function candidateDetail(c, graph, limits, runs, plan) {
  const terms = c.score_terms || {};
  const trials = num(terms.novelty) ? Math.max(0, Math.round(1 / terms.novelty - 1)) : null;
  const dependents = graph.criticality?.[c.target_component]?.configured_dependents;
  const impactText = terms.impact === 0 ? "impact 0 = no configured dependants in the graph" : `${dependents ?? "?"} configured dependant(s) × ${actionLabel(c.action).toLowerCase()} severity`;
  const approval = c.approval;
  const expires = approval?.expires_at ? Date.parse(approval.expires_at) : null;
  const active = c.approval_status === "approved" && expires && expires > Date.now();
  const ran = runs.filter((r) => r.experiment_id === c.id);
  const commands = active ? `<h4 style="margin:16px 0 8px;font-size:12px;color:var(--text-3)">Run from the operator terminal</h4>
    ${commandBlock("Rehearse (changes nothing)", `python -m safety.cli execute ${c.id} --dry-run`)}
    ${commandBlock("Real run", `CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute ${c.id}`)}
    ${commandBlock("Kill-switch proof", `CHAOS_EXECUTION_ENABLED=true python -m safety.cli execute ${c.id} --forced-test`)}` : "";
  const p = c.predicted_slis || {};
  return tile({
    cls: active ? "t-yellow" : "", title: `${actionLabel(c.action)} · ${c.target_component}`, iconName: ACTIONS[c.action]?.icon || "zap",
    why: `${c.duration_seconds} s · rollback: ${ACTIONS[c.action]?.rollback || "predefined"} · max ${limits.max_duration_seconds} s`,
    right: statusChip(c, runs),
    body: `<p class="plain-text">${esc(c.hypothesis)}</p>
      <h4 style="margin:16px 0 8px;font-size:12px;color:var(--text-3)">Why this experiment</h4>
      <div class="why-rings">
        <div class="why-ring">${ring(terms.impact, { size: 44, stroke: 4, text: num(terms.impact) !== null ? terms.impact.toFixed(1) : "—" })}<div><b>Impact</b><small>${esc(impactText)}</small></div></div>
        <div class="why-ring">${ring(terms.novelty, { size: 44, stroke: 4, text: num(terms.novelty) !== null ? terms.novelty.toFixed(1) : "—" })}<div><b>Novelty</b><small>${trials === 0 ? "never tested before" : `tested ${esc(trials)} time(s) before`}</small></div></div>
        <div class="why-ring">${ring(terms.uncertainty, { size: 44, stroke: 4, text: num(terms.uncertainty) !== null ? terms.uncertainty.toFixed(1) : "—" })}<div><b>Uncertainty</b><small>${trials === 0 ? "no earlier run to learn from" : "earlier runs narrow the guess"}</small></div></div>
      </div>
      ${c.reason ? `<p class="muted" style="margin-top:10px">${esc(c.reason)}</p>` : ""}
      ${(c.ranking_method || plan?.ranking_method) === "weighted_heuristic_v1" ? '<p class="muted" style="margin-top:4px">Score = 0.40 impact + 0.35 novelty + 0.25 uncertainty (rule-based, no LLM).</p>' : ""}
      <h4 style="margin:16px 0 8px;font-size:12px;color:var(--text-3)">Predicted impact</h4>
      <div class="pvo">${["write", "search", "freshness"].map((s) => `<div class="pvo-item"><div class="sli">${esc(fmt.sentence(s))}</div>${pill(p[s], s)}</div>`).join("")}</div>
      <h4 style="margin:16px 0 8px;font-size:12px;color:var(--text-3)">Approval record</h4>
      ${approval ? `<div class="kv"><div><span>Decision</span><strong>${esc(fmt.sentence(approval.decision))} by ${esc(approvedBy(approval) || "?")}</strong></div><div><span>${expires && expires < Date.now() ? "Expired" : "Expires"}</span><strong>${esc(fmt.clock(approval.expires_at))}</strong></div><div style="grid-column:1/-1"><span>Reason</span><strong>${esc(approvalReason(approval))}</strong></div><div style="grid-column:1/-1"><span>Bound to hash</span><strong class="mono">${esc(fmt.short(approval.candidate_hash, 26))}…</strong></div></div>`
        : `<p class="muted">No decision yet. Hash <code>${esc(fmt.short(c.candidate_hash, 26))}…</code></p>`}
      ${ran.length ? `<p style="margin-top:10px">${ran.map((r) => `<a href="#/runs/${encodeURIComponent(r.run_id)}">${icon("flask", "sm")} ${esc(r.run_id)}</a>`).join(" · ")}</p>` : ""}
      ${c.approval_status === "pending" || !c.approval_status ? `<div style="display:flex;gap:8px;margin-top:16px"><button class="button button-primary" type="button" data-decide="approve" data-id="${esc(c.id)}" data-hash="${esc(c.candidate_hash)}">${icon("user")}Approve…</button><button class="button" type="button" data-decide="reject" data-id="${esc(c.id)}" data-hash="${esc(c.candidate_hash)}">Reject…</button></div>` : ""}
      ${commands}`,
    foot: `<span class="mono">${esc(c.id)}</span><span>Approving never executes anything</span>`,
  });
}

function commandBlock(label, command) {
  return `<div class="command"><span>${esc(label)}</span><code>${esc(command)}</code><button class="button button-sm" type="button" data-copy="${esc(command)}">${icon("copy", "sm")}Copy</button></div>`;
}

function bindExperimentActions() {
  $("#baseline-button").addEventListener("click", async () => {
    const button = $("#baseline-button");
    const message = $("#baseline-message");
    button.disabled = true;
    message.innerHTML = '<div class="notice" style="margin-bottom:10px">Capturing a baseline from the latest live run…</div>';
    try {
      const baseline = await getJson("/api/v1/baselines", { method: "POST" });
      await loadExperiments();
      $("#baseline-message").innerHTML = `<div class="notice ok" style="margin-bottom:10px">Baseline ${esc(baseline.baseline_id)} captured.</div>`;
      $("#baseline-select").value = baseline.baseline_id;
    } catch (error) {
      message.innerHTML = `<div class="notice bad" style="margin-bottom:10px">${esc(error.message)}</div>`;
      button.disabled = false;
    }
  });
  $("#plan-button").addEventListener("click", async () => {
    const baselineId = $("#baseline-select").value;
    const results = $("#plan-results");
    if (!baselineId) { results.innerHTML = '<p class="muted">Capture a baseline first.</p>'; return; }
    results.innerHTML = '<p class="muted">Ranking evidence-backed candidates…</p>';
    try {
      const plan = await getJson("/api/v1/experiments/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ baseline_id: baselineId, max_candidates: 6 }) });
      selectedCandidate = null;
      await loadExperiments();
      $("#plan-results").innerHTML = `<div class="notice ok">${plan.candidates?.length ? `${plan.candidates.length} candidate(s) saved as ${esc(plan.plan_id)}.` : "No candidates."}</div>${(plan.policy_rejections || []).map((r) => `<p class="muted" style="margin-top:6px">${icon("lock", "sm")} Not planned: <strong>${esc(r.target)}</strong> · ${esc(r.reason)}</p>`).join("")}`;
    } catch (error) {
      results.innerHTML = `<div class="notice bad">${esc(error.message)}</div>`;
    }
  });
}

document.addEventListener("click", async (event) => {
  const copy = event.target.closest("button[data-copy]");
  if (copy) {
    const original = copy.innerHTML;
    try { await navigator.clipboard.writeText(copy.dataset.copy); copy.textContent = "Copied"; } catch { copy.textContent = "Select and copy"; }
    setTimeout(() => { copy.innerHTML = original; }, 1500);
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
  $("#approve-summary").innerHTML = `<code>${esc(id)}</code><span>Bound to <code>${esc(fmt.short(hash, 26))}…</code></span><span>Recording a decision never executes anything. Approvals are single use and expire.</span>`;
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
    selectedCandidate = pendingDecision.id;
    pendingDecision = null;
    await loadExperiments();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  }
});

// ------------------------------------------------------------------ runs and replay
const RULE_FOR_REASON = [[/p95|latency/, "p95"], [/error/, "errors"], [/fresh/, "fresh"], [/sample|telemetry/, "samples"], [/unrelated|component|service|health/, "others"], [/duration|timeout/, "duration"]];

async function loadRuns(selected) {
  const [runsPayload, limits, graph, ranked] = await Promise.all([getJson("/api/v1/runs?limit=50"), policy(), getJson("/api/v1/dependency-map"), getJson("/api/v1/findings/ranked")]);
  const runs = sortRuns(runsPayload.runs);
  const view = $("#view-runs");
  const head = `<div class="page-head"><div><p class="eyebrow">Control plane runs · Agent 2 measures · Agent 3 explains</p><h1 id="h-runs">Runs</h1>
    <p>Every run the operator CLI executed, poll by poll. Replay one to watch the kill switch decide; replay happens in this browser only.</p></div><span class="muted">${runs.length} recorded</span></div>`;
  if (!runs.length) { view.innerHTML = head + emptyState("No runs recorded yet", "<p>Approve a proposal under Experiments, then run it from the operator CLI.</p>"); stamp(); return; }
  const current = selected ? decodeURIComponent(selected) : runs[0].run_id;
  const run = runs.find((r) => r.run_id === current) || runs[0];
  view.innerHTML = `${head}<div class="runs-layout"><nav class="run-list" aria-label="Recorded runs">${runs.map((r) => {
    const { action, target } = runAction(r);
    const status = r.status === "aborted" ? chip(r.forced_test ? "Forced abort" : "Kill switch", "bad", "octagon") : chip(fmt.sentence(r.status), r.status === "completed" ? "ok" : "", r.status === "completed" ? "check" : "");
    return `<a class="run-item ${r.run_id === run.run_id ? "selected" : ""}" href="#/runs/${encodeURIComponent(r.run_id)}" ${r.run_id === run.run_id ? 'aria-current="true"' : ""}>
      <div class="ri-top"><span class="ri-title">${esc(actionLabel(action))} · ${esc(target)}</span>${status}</div>
      <div class="ri-meta">${esc(fmt.date(r.created_at))} · <span class="mono">${esc(r.run_id)}</span></div>
      <div class="ri-meta">${isScored(r) ? `score ${esc(r.resilience_score?.score ?? "—")}` : r.forced_test ? "forced test · not scored" : "dry run · not scored"}</div></a>`;
  }).join("")}</nav><div id="run-detail"></div></div>`;
  const findings = ranked.ranked?.filter((f) => f.run_id === run.run_id) || [];
  const legacy = findings.length ? [] : (await getJson(`/api/v1/findings?run_id=${encodeURIComponent(run.run_id)}`)).findings || [];
  renderRunDetail(run, limits, graph, findings, legacy);
  stamp();
}

function runModel(run, limits) {
  const tl = run.timeline || {};
  const inj = Date.parse(tl.injected_at || run.created_at);
  const at = (iso) => (iso ? (Date.parse(iso) - inj) / 1000 : null);
  const obs = (run.observations || []).map((o, i) => ({ ...o, i, t: o.window_end ? (Date.parse(o.window_end) - inj) / 1000 : null }));
  obs.forEach((o, i) => { if (o.t === null) o.t = i * limits.abort_thresholds.poll_interval_seconds; });
  const events = [
    ["created_at", "Preflight", "preflight"], ["injected_at", "Fault injected", "fault"], ["action_completed_at", "Action returned", "action"],
    ["abort_at", "Kill switch fired", "kill"], ["rollback_at", "Rolled back", "rollback"], ["recovered_at", "Recovered", "recovered"],
  ].filter(([key]) => tl[key]).map(([key, label, kind]) => ({ key, label, kind, t: at(tl[key]) })).sort((a, b) => a.t - b.t);
  const ts = [...events.map((e) => e.t), ...obs.map((o) => o.t)];
  const tMin = Math.min(...ts) - 1;
  const tMax = Math.max(...ts) + 1;
  const base = run.baseline_write_p95_ms;
  const thresholds = limits.abort_thresholds;
  const forcedRule = run.forced_test ? String(run.forced_test_rule || "") : "";
  const tripKey = run.status === "aborted" ? (RULE_FOR_REASON.find(([re]) => re.test(String(run.abort_reason || ""))) || [])[1] : null;
  const abortT = at(tl.abort_at);
  const rollbackT = at(tl.rollback_at);
  const recoveredT = at(tl.recovered_at);
  const endFault = abortT ?? rollbackT;
  return { tl, inj, obs, events, tMin, tMax, base, thresholds, forcedRule, tripKey, abortT, rollbackT, recoveredT, endFault, at };
}

function rulesAt(run, m, limits, cursor) {
  const o = [...m.obs].reverse().find((x) => x.t <= cursor) || null;
  const t = m.thresholds;
  const inFault = o && o.phase === "fault";
  const p95Limit = num(m.base) !== null ? m.base * t.write_p95_multiplier : null;
  const elapsed = clamp(cursor, 0, m.endFault ?? cursor);
  const rows = [
    { key: "p95", name: "Write latency p95", hint: `${t.write_p95_multiplier}× baseline`, value: o?.write_p95_ms, limit: p95Limit, f: fmt.ms },
    { key: "errors", name: "Write errors", hint: "share of failed writes", value: o?.write_error_rate, limit: t.write_error_rate, f: fmt.pct },
    { key: "fresh", name: "Freshness p95", hint: "SSE staleness", value: o?.freshness_p95_seconds, limit: t.freshness_p95_seconds, f: fmt.secs },
    { key: "samples", name: "Missing samples", hint: `min ${t.min_write_samples} writes · ${t.min_freshness_samples} freshness`, short: o ? (o.write_samples ?? 0) < t.min_write_samples || (o.freshness_samples ?? 0) < t.min_freshness_samples : null, text: o ? `${o.write_samples ?? 0} · ${o.freshness_samples ?? 0}` : "—" },
    { key: "others", name: "Other services", hint: "unrelated components healthy", value: o ? (o.unrelated_component_healthy === false ? 1 : 0) : null, limit: 1, text: !o ? "—" : o.unrelated_component_healthy === false ? "unhealthy" : o.unrelated_component_healthy === true ? "all healthy" : "not checked" },
    { key: "duration", name: "Max duration", hint: "fault time vs policy cap", value: cursor >= 0 ? elapsed : null, limit: limits.max_duration_seconds, f: (v) => `${v.toFixed(1)} s` },
  ];
  for (const r of rows) {
    if (r.key === "samples") {
      r.ratio = r.short ? 1 : r.short === false ? 0.15 : 0;
      r.breach = inFault && !!r.short;
    } else {
      r.ratio = num(r.value) === null || num(r.limit) === null ? 0 : clamp(r.value / r.limit, 0, 1);
      r.breach = inFault && num(r.value) !== null && num(r.limit) !== null && (r.key === "others" ? r.value >= 1 : r.value > r.limit);
    }
    r.tripped = m.tripKey === r.key && num(m.abortT) !== null && cursor >= m.abortT;
    r.na = r.key === "others" && o && o.unrelated_component_healthy === undefined;
    r.display = r.text ?? `${num(r.value) === null ? "—" : r.f(r.value)} / ${num(r.limit) === null ? "?" : r.f(r.limit)}`;
    if (m.forcedRule && RULE_FOR_REASON.some(([re, key]) => key === r.key && re.test(m.forcedRule))) r.hint = "forced limit (test)";
  }
  rows.sort((a, b) => (b.tripped - a.tripped));
  return { o, rows };
}

function ksStateAt(run, m, cursor) {
  if (cursor < 0) return "armed";
  if (num(m.abortT) !== null && cursor >= m.abortT && (num(m.rollbackT) === null || cursor < m.rollbackT + 0.6)) return "breach";
  if (num(m.recoveredT) !== null && cursor >= m.recoveredT) return run.recovery_verified ? "verified" : "rolling";
  if (num(m.rollbackT) !== null && cursor >= m.rollbackT) return "rolling";
  return "watching";
}
const KS_STATES = [["armed", "Armed", "before the fault"], ["watching", "Watching", "polls every 2 s"], ["breach", "Breach detected", "a rule tripped"], ["rolling", "Rolling back", "predefined rollback"], ["verified", "Recovery verified", "/health, canary, SLIs"]];

function killSwitchPanel(states, current, rows, heartbeat, { title = "Kill switch", why = "", source = "" } = {}) {
  const idx = KS_STATES.findIndex(([k]) => k === current);
  return tile({
    cls: "t-red", title, iconName: "octagon", why, source,
    body: `<div class="ks-states" role="list">${KS_STATES.map(([key, label, sub], i) => `<div role="listitem" class="ks-state ${i < idx ? "past" : ""} ${key === current ? `now ${current === "breach" ? "breach" : current === "verified" ? "verified" : ""}` : ""}" ${key === current ? 'aria-current="step"' : ""}><b>${key === current ? icon(current === "breach" ? "octagon" : current === "verified" ? "check" : "activity", "sm") : ""}${esc(label)}</b>${esc(sub)}</div>`).join("")}</div>
      ${heartbeat}
      <div class="rules">${rows.map((r) => `<div class="rule ${r.tripped ? "tripped" : r.breach ? "tripped" : r.ratio > 0.7 ? "warn" : ""} ${r.na ? "na" : ""}">
        <svg class="mini-ring" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle class="t" cx="10" cy="10" r="7.5"/><circle class="v" cx="10" cy="10" r="7.5" stroke-dasharray="47.1" stroke-dashoffset="${(47.1 * (1 - r.ratio)).toFixed(1)}" transform="rotate(-90 10 10)"/></svg>
        <span class="r-name">${esc(r.name)}<small>${r.tripped ? `${icon("octagon", "sm")} Tripped at ${esc(fmt.rel(r.trippedAt))}` : esc(r.hint)}</small></span>
        <span class="bar"><i style="width:${(r.ratio * 100).toFixed(0)}%"></i></span>
        <span class="r-val"><b>${esc(r.display.split(" / ")[0])}</b>${r.display.includes(" / ") ? ` / ${esc(r.display.split(" / ")[1])}` : ""}</span></div>`).join("")}</div>`,
  });
}

let replayTick = null;
function stopReplay() {
  replay.playing = false;
  cancelAnimationFrame(replayTick);
  document.body.classList.remove("simulating");
}

function renderRunDetail(run, limits, graph, findings, legacy) {
  const m = runModel(run, limits);
  const { action, target } = runAction(run);
  const host = $("#run-detail");
  const score = run.resilience_score || {};
  const labels = `${run.forced_test ? chip("Forced kill-switch test · not scored", "warn", "alert") : ""}${run.dry_run ? chip("Dry run · not scored", "warn", "info") : ""}`;
  const state = { cursor: m.tMin, aborted: false, log: [] };
  Object.assign(replay, { playing: false });
  host.innerHTML = `
    <section class="tile hero ${run.status === "aborted" ? "t-red" : ""}">
      <div class="tile-head"><div><p class="eyebrow">${esc(fmt.date(run.created_at))} · <span class="mono">${esc(run.run_id)}</span></p><h2 style="font-size:20px">${icon(ACTIONS[action]?.icon || "zap", "lg")} ${esc(actionLabel(action))} · ${esc(target)}</h2></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">${run.status === "aborted" ? chip("Kill switch fired", "bad", "octagon") : chip(fmt.sentence(run.status), "ok", "check")}${labels}${chip("Recorded", "outline")}</div></div>
      <p class="run-headline">${headline(run, limits)}</p>
      <div class="scrubber" style="margin-top:14px">${scrubTrack(m)}</div>
      <div class="replay-bar">
        <button class="button" type="button" id="replay-play">${icon("play")}Replay at 4×</button>
        <button class="button" type="button" id="replay-reset">${icon("rotate")}Reset</button>
        <span class="clock" id="replay-clock">${esc(fmt.rel(m.tMin))}</span>
        <span class="muted">Replay of recorded observations in this browser. No container is touched.</span>
      </div>
    </section>
    <div class="bento">
      <div class="c7" id="ks-panel"></div>
      <div class="c5">
        ${tile({ cls: "t-red", title: "Manual kill switch", iconName: "octagon", why: "During a replay, hold to end the replayed fault early and roll back (simulated).", body: `<button class="hold-button" type="button" id="hold-kill" disabled><svg class="hold-ring" viewBox="0 0 44 44" aria-hidden="true"><circle class="t" cx="22" cy="22" r="18"/><circle class="v" cx="22" cy="22" r="18"/></svg><span class="stop-core"></span><span><b>Hold 1 s to kill</b><small id="hold-hint">Active only while the replayed fault runs.</small></span></button>` })}
        <section class="tile" style="margin-top:16px"><div class="tile-head"><div><h2>${icon("terminal")}Replay log</h2><p class="why">Each step as the control plane saw it.</p></div><span class="source">recorded run</span></div><div class="feed" id="replay-feed" aria-live="polite"><div><span>—</span>Press Replay to step through this run.</div></div></section>
        <div style="margin-top:16px">${recoveryTile(run, limits)}</div>
      </div>
    </div>
    ${tile({ title: "SLIs, poll by poll", iconName: "activity", why: "Each point is one 30 s window of live samples, polled every 2 s while the fault ran. Markers: fault, kill, rollback, recovered.", source: `${m.obs.length} windows · baseline write p95 ${esc(fmt.ms(m.base))}`, body: '<div class="chart-grid" id="run-charts"></div>' })}
    ${tile({ title: "Where the fault landed", iconName: "share", why: "Red: the attacked component, while the fault is active. Amber: components that impact_paths in the graph says could be affected. Not a measurement.", body: '<div id="run-map"></div>' })}
    <div class="bento">
      ${tile({ cls: "c7 t-teal", title: "Resilience score", iconName: "gauge", why: "0.30 availability + 0.20 latency + 0.20 recovery + 0.15 containment + 0.15 detection.", source: isScored(run) ? `coverage ${esc(score.coverage ?? "—")}` : "not scored", body: scoreBlock(run) })}
      ${tile({ cls: "c5", title: "Predicted vs observed", iconName: "route", why: "Agent 3's per-SLI prediction before the run, against what Agent 2 measured.", right: run.diagnosis?.prediction_accuracy !== undefined ? chip(`Accuracy ${fmt.pct(run.diagnosis.prediction_accuracy)}`, "info") : "", body: predictionBlock(run) })}
    </div>
    ${tile({ title: "Findings", iconName: "fileCheck", why: "Measured facts, each with its source, kept apart from the hypothesis and fix. Violet marks text the optional LLM wrote.", source: `${findings.length || legacy.length} for this run`, body: findingsBlock(findings, legacy) })}`;
  animateRings(host);

  const feed = $("#replay-feed");
  const log = (t, text, cls = "") => { state.log.push({ t, text, cls }); feed.innerHTML = state.log.map((e) => `<div class="${e.cls}"><span>${esc(fmt.rel(e.t))}</span>${esc(e.text)}</div>`).join(""); feed.scrollTop = feed.scrollHeight; };
  const passed = new Set();
  const update = (playing) => {
    const cursor = state.cursor;
    $("#replay-clock").textContent = fmt.rel(cursor);
    $("#scrub-input").value = String(cursor);
    const ks = state.aborted ? "rolling" : ksStateAt(run, m, cursor);
    const { rows } = rulesAt(run, m, limits, cursor);
    rows.forEach((r) => { r.trippedAt = m.abortT; });
    const ticks = m.obs.filter((o) => o.t <= cursor).map((o) => {
      const breach = o.phase === "fault" && num(m.abortT) !== null && Math.abs(o.t - m.abortT) < limits.abort_thresholds.poll_interval_seconds && run.status === "aborted";
      return `<i class="${o.phase === "pre_fault" ? "pre" : o.phase === "recovered" ? "rec" : breach ? "breach" : ""}" title="${esc(fmt.rel(o.t))} · ${esc(fmt.label(o.phase))}"></i>`;
    }).join("");
    $("#ks-panel").innerHTML = killSwitchPanel(KS_STATES, ks, rows, `<div class="heartbeat"><span class="label">Polls</span>${ticks || '<span class="muted" style="font-size:12px">none yet</span>'}</div>`, { why: state.aborted ? "Replay stopped by the manual kill (simulated)." : "Each rule is checked against the last 30 s on every 2 s poll. Limits come only from the policy file.", source: `${limits.abort_thresholds.poll_interval_seconds} s polls · ${limits.abort_thresholds.window_seconds} s window` });
    renderRunCharts(run, m, limits, cursor);
    const faultOn = cursor >= 0 && cursor < (m.rollbackT ?? m.tMax) && !state.aborted;
    const faultLabel = run.status === "aborted" && num(m.abortT) !== null && cursor >= m.abortT ? `${actionLabel(action)} · kill switch fired at ${fmt.rel(m.abortT)}` : `${actionLabel(action)} · fault active`;
    const fault = faultOn ? { target, label: faultLabel, impacted: impactOf(graph, target) } : null;
    const mapKey = fault ? fault.label : "none";
    if (state.mapKey !== mapKey) { state.mapKey = mapKey; renderMap($("#run-map"), graph, limits, { band: false, policyOverlay: false, fault }); }
    const hold = $("#hold-kill");
    const canKill = playing && faultOn && !state.aborted;
    hold.disabled = !canKill;
    $("#hold-hint").textContent = canKill ? "Ends the replayed fault and rolls back now (simulation)." : "Active only while the replayed fault runs.";
    if (playing) {
      for (const e of m.events) if (cursor >= e.t && !passed.has(e.key)) { passed.add(e.key); log(e.t, eventText(e, run, m, limits, action, target), e.kind === "kill" ? "kill" : e.kind === "recovered" ? "ok" : ""); }
      for (const o of m.obs) if (cursor >= o.t && !passed.has(`o${o.i}`)) { passed.add(`o${o.i}`); if (o.phase === "fault") log(o.t, `Poll: write p95 ${fmt.ms(o.write_p95_ms)} · write errors ${fmt.pct(o.write_error_rate)} · search errors ${fmt.pct(o.search_error_rate)} · freshness ${fmt.secs(o.freshness_p95_seconds)}`); }
    }
  };
  $("#scrub-input").addEventListener("input", (ev) => { stopReplay(); setPlayLabel(); state.cursor = Number(ev.target.value); state.aborted = false; update(false); });
  const setPlayLabel = () => { $("#replay-play").innerHTML = replay.playing ? `${icon("pause")}Pause` : `${icon("play")}Replay at 4×`; };
  const frame = (now) => {
    if (!replay.playing) return;
    const dt = (now - (replay.last || now)) / 1000;
    replay.last = now;
    state.cursor = Math.min(m.tMax, state.cursor + dt * 4);
    update(true);
    if (state.cursor >= m.tMax) { log(state.cursor, "Replay finished.", "sim"); stopReplay(); setPlayLabel(); return; }
    replayTick = requestAnimationFrame(frame);
  };
  $("#replay-play").addEventListener("click", () => {
    if (replay.playing) { stopReplay(); setPlayLabel(); return; }
    if (state.cursor >= m.tMax || state.aborted) { state.cursor = m.tMin; state.aborted = false; state.log = []; passed.clear(); }
    if (!state.log.length) log(state.cursor, "Replay started: recorded observations at 4× speed. No container is touched.", "sim");
    replay.playing = true; replay.last = null;
    document.body.classList.add("simulating");
    setPlayLabel();
    replayTick = requestAnimationFrame(frame);
  });
  $("#replay-reset").addEventListener("click", () => { stopReplay(); setPlayLabel(); state.cursor = m.tMin; state.aborted = false; state.log = []; passed.clear(); feed.innerHTML = '<div><span>—</span>Press Replay to step through this run.</div>'; update(false); });
  bindHold($("#hold-kill"), () => {
    stopReplay(); setPlayLabel();
    state.aborted = true;
    log(state.cursor, `Manual kill (simulated) at ${fmt.rel(state.cursor)}: replay stopped and rolled back. The UI cannot stop a real fault; the operator terminal can.`, "kill");
    update(false);
  });
  update(false);
}

function eventText(e, run, m, limits, action, target) {
  if (e.kind === "preflight") return `Preflight: steady state checked against baseline, watchdog armed on ${target}.`;
  if (e.kind === "fault") return `Fault injected: ${actionLabel(action)} on ${target}.`;
  if (e.kind === "action") return "Fault action returned; kill switch keeps polling.";
  if (e.kind === "kill") return `Kill switch fired: ${fmt.label(run.abort_reason || "rule tripped")}.`;
  if (e.kind === "rollback") return `Rollback: ${ACTIONS[action]?.rollback || "predefined rollback"}.`;
  if (e.kind === "recovered") return `Recovery ${run.recovery_verified ? "verified" : "not verified"}: /health ${run.recovery?.health_status ?? "?"} · canary ${run.recovery?.canary_status ?? "?"} · SLIs within baseline ${run.recovery?.slis_within_baseline ? "yes" : "no"}.`;
  return e.label;
}

function scrubTrack(m) {
  const span = m.tMax - m.tMin || 1;
  const pct = (t) => `${(((t - m.tMin) / span) * 100).toFixed(2)}%`;
  const faultEnd = m.endFault ?? m.rollbackT;
  const merged = [];
  for (const e of m.events) {
    const prev = merged[merged.length - 1];
    if (prev && Math.abs(e.t - prev.t) < span * 0.05) { prev.label += ` + ${e.label.toLowerCase()}`; prev.kinds.push(e.kind); } else merged.push({ ...e, kinds: [e.kind] });
  }
  return `<div class="scrub-track">
    <div class="scrub-line"></div>
    ${num(faultEnd) !== null ? `<div class="scrub-phase fault" style="left:${pct(0)};width:calc(${pct(faultEnd)} - ${pct(0)})"></div>` : ""}
    ${num(m.rollbackT) !== null && num(m.recoveredT) !== null ? `<div class="scrub-phase recover" style="left:${pct(m.rollbackT)};width:calc(${pct(m.recoveredT)} - ${pct(m.rollbackT)})"></div>` : ""}
    ${merged.map((e, i) => { const at = (e.t - m.tMin) / span; return `<span class="scrub-dot ${e.kinds.includes("kill") ? "kill" : e.kinds[e.kinds.length - 1]}" style="left:${pct(e.t)}"></span><div class="scrub-event ${i % 2 ? "down" : "up"} ${at < 0.15 ? "edge-l" : at > 0.85 ? "edge-r" : ""}" style="left:${pct(e.t)}"><b>${esc(e.label)}</b><small>${esc(fmt.rel(e.t))}</small></div>`; }).join("")}
    <input class="scrub-input" id="scrub-input" type="range" min="${m.tMin}" max="${m.tMax}" step="0.1" value="${m.tMin}" aria-label="Scrub through the run timeline">
  </div>`;
}

function renderRunCharts(run, m, limits, cursor) {
  const t = limits.abort_thresholds;
  const markers = [];
  for (const e of m.events.filter((x) => ["fault", "kill", "rollback", "recovered"].includes(x.kind))) {
    const prev = markers[markers.length - 1];
    if (prev && Math.abs(prev.x - e.t) < (m.tMax - m.tMin) * 0.06) prev.label += " + " + e.kind; else markers.push({ x: e.t, kind: e.kind, label: e.kind });
  }
  const pts = (key) => m.obs.map((o) => ({ x: o.t, y: o[key] ?? null }));
  const xFmt = (s) => fmt.rel(s);
  const p95Limit = num(m.base) !== null ? m.base * t.write_p95_multiplier : null;
  const common = { x: [m.tMin, m.tMax], xFmt, markers, cursor, width: 560, height: 170 };
  const card = (title, tag, svg) => `<div class="chart-card"><h3>${esc(title)} ${tag}</h3>${svg}</div>`;
  $("#run-charts").innerHTML = [
    card("Write latency p95", chip(`kill at ${t.write_p95_multiplier}× baseline`, "bad"), chart({ ...common, label: "Write p95 per poll", points: pts("write_p95_ms"), fmt: fmt.ms, band: num(m.base) !== null ? { value: m.base, label: `baseline ${fmt.ms(m.base)}` } : null, kill: p95Limit !== null ? { value: p95Limit, label: run.forced_test && /p95|latency/.test(m.forcedRule) ? "forced test limit" : `Kill at ${fmt.ms(p95Limit)}` } : null })),
    card("Write errors", chip(`kill above ${fmt.pct(t.write_error_rate)}`, "bad"), chart({ ...common, label: "Write error rate per poll", points: pts("write_error_rate"), yMax: Math.max(t.write_error_rate * 1.5, ...m.obs.map((o) => o.write_error_rate ?? 0)), fmt: fmt.pct, kill: { value: t.write_error_rate, label: `Kill at ${fmt.pct(t.write_error_rate)}` } })),
    card("Search errors", chip("expected impact · not a kill signal", "outline"), chart({ ...common, label: "Search error rate per poll", points: pts("search_error_rate"), yMax: Math.max(0.1, ...m.obs.map((o) => (o.search_error_rate ?? 0) * 1.1)), fmt: fmt.pct })),
    card("Freshness p95", chip(`kill above ${fmt.secs(t.freshness_p95_seconds)}`, "bad"), chart({ ...common, label: "Freshness p95 per poll", points: pts("freshness_p95_seconds"), fmt: fmt.secs, kill: { value: t.freshness_p95_seconds, label: `Kill at ${fmt.secs(t.freshness_p95_seconds)}` } })),
  ].join("");
}

function recoveryTile(run, limits) {
  const r = run.recovery || {};
  const item = (ok, title, sub) => `<div class="check">${ok ? `<span class="ok">${icon("check")}</span>` : `<span class="bad">${icon("x")}</span>`}<div>${title}<small>${sub}</small></div></div>`;
  return tile({
    cls: run.recovery_verified ? "t-green" : "t-red", title: "Recovery check", iconName: "shieldCheck",
    why: `Run after rollback, up to ${limits.recovery_timeout_seconds} s.`,
    right: run.recovery_verified ? chip(`Verified · ${fmt.secs(run.recovery_time_seconds)}`, "ok", "check") : chip("Not verified", "bad", "x"),
    body: r.target ? `<div style="display:flex;flex-direction:column;gap:8px">
      ${item(r.health_status === 200, `<code>/health</code> → ${esc(r.health_status ?? "?")}`, `${esc(r.target)} answers its health check`)}
      ${item(r.canary_status === 200, `Canary → ${esc(r.canary_status ?? "?")}`, `<code>${esc(r.canary_path || "")}</code>`)}
      ${item(!!r.slis_within_baseline, "SLIs back within thresholds", esc(r.sli_reason || (r.slis_within_baseline ? "write, search and freshness" : "not yet")))}</div>
      <div class="kv" style="margin-top:14px"><div><span>Cleanup</span><strong>${esc(run.cleanup_state || "—")}</strong></div><div><span>Watchdog</span><strong>${run.watchdog ? (run.watchdog.armed ? "still armed" : "armed → disarmed") : "—"}</strong></div></div>`
      : emptyState("No recovery check recorded", ""),
  });
}

function scoreBlock(run) {
  const score = run.resilience_score || {};
  if (!isScored(run)) return emptyState("Not scored", `<p>${run.forced_test ? "Forced kill-switch tests" : "Dry runs"} prove the safety controls and are never scored or ranked.</p>`);
  const components = score.components || run.score_components || {};
  const inputs = run.score_inputs || {};
  const weights = inputs.weights || { availability: 0.3, latency: 0.2, recovery: 0.2, containment: 0.15, detection: 0.15 };
  const names = Object.keys(components);
  if (!names.length) return emptyState("No score components", `<p>${esc(fmt.label(score.status || "not scored"))}</p>`);
  const describe = (name) => {
    const entry = inputs[name];
    if (!entry || typeof entry !== "object") return { rule: "", values: "" };
    const values = Object.entries(entry).filter(([k]) => k !== "rule").map(([k, v]) => `${fmt.label(k)} ${typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(3)) : v}`).join(" · ");
    return { rule: entry.rule || "", values };
  };
  return `<div class="score-layout"><div>${scoreRingFor(run, 140)}${score.status !== "complete" ? `<p class="muted" style="margin-top:6px">Coverage ${esc(score.coverage)} is below 0.80: incomplete.</p>` : ""}</div>
    <div class="components">${names.map((name) => {
      const v = components[name];
      const d = describe(name);
      return `<div class="component">${ring(v, { size: 40, stroke: 4, text: num(v) !== null ? Math.round(v * 100) : "—", cls: v >= 0.8 ? "" : v >= 0.5 ? "solid-warn" : "solid-bad" })}<div>
        <div class="c-name"><b>${esc(fmt.sentence(name))}</b><span>weight ${esc(Math.round((weights[name] ?? 0) * 100))}%</span></div>
        ${d.rule ? `<div class="c-rule">${esc(d.rule)}</div>` : ""}${d.values ? `<div class="c-inputs">${esc(d.values)}</div>` : !run.score_inputs ? '<div class="c-rule">Inputs not recorded for this older run.</div>' : ""}</div></div>`;
    }).join("")}</div></div>`;
}

const VERDICT = { match: ["Matched", "ok"], better_than_predicted: ["Better than predicted", "info"], worse_than_predicted: ["Worse than predicted", "bad"] };
function predictionBlock(run) {
  const comparison = run.diagnosis?.comparison;
  if (!comparison) return emptyState("No diagnosis for this run", "<p>Diagnosis is written after a real run completes.</p>");
  return `<div class="pvo">${["write", "search", "freshness"].filter((s) => comparison[s]).map((s) => {
    const c = comparison[s];
    const [text, kind] = VERDICT[c.verdict] || [fmt.sentence(c.verdict), ""];
    return `<div class="pvo-item"><div class="sli">${esc(fmt.sentence(s))}</div><div class="flow"><span class="muted">Predicted</span>${pill(c.predicted, "predicted")}</div><div class="flow"><span class="muted">Observed</span>${pill(c.observed, "observed")}</div><div class="verdict">${chip(text, kind)}</div></div>`;
  }).join("")}</div>${run.diagnosis.summary ? `<p class="muted" style="margin-top:12px">${esc(run.diagnosis.summary)}</p>` : ""}`;
}

function factValue(fact) {
  const v = fact.value;
  const name = String(fact.fact || "");
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (num(v) === null) return fmt.sentence(v ?? "—");
  if (/rate/.test(name)) return fmt.pct(v);
  if (/\(ms\)/.test(name)) return fmt.ms(v);
  if (/\(s\)/.test(name)) return fmt.secs(v);
  return String(Number.isInteger(v) ? v : v.toFixed(2));
}

const SEVERITY = { critical: "bad", high: "bad", medium: "warn", low: "info", info: "" };
function findingsBlock(findings, legacy) {
  if (!findings.length && !legacy.length) return emptyState("No findings for this run", "<p>Findings are written when a real run shows a measurable weakness.</p>");
  const ranked = findings.map((f) => `<article class="finding">
    <div class="finding-head"><strong>${f.rank ? `#${esc(f.rank)} · ` : ""}${esc(f.summary)}</strong>${chip(fmt.sentence(f.severity), SEVERITY[f.severity] || "", "alert")}</div>
    <h4>${icon("activity", "sm")}Measured facts</h4>
    <div class="facts">${(f.measured_facts || []).map((x) => `<div class="fact"><b>${esc(factValue(x))}</b><span>${esc(x.fact)}</span><code>${esc(x.source)}</code></div>`).join("")}</div>
    ${f.hypothesis ? `<h4>${f.hypothesis.ai_generated ? icon("sparkles", "sm") : icon("info", "sm")}Hypothesis and fix ${f.hypothesis.ai_generated ? chip("AI-written", "ai", "sparkles") : chip("rule-based", "outline")}</h4><p class="${f.hypothesis.ai_generated ? "ai-text" : "plain-text"}">${esc(f.hypothesis.text)}</p>` : ""}
    ${f.validation_test ? `<h4>${icon("check", "sm")}How to validate the fix</h4><p class="muted">${esc(f.validation_test)}</p>` : ""}
  </article>`).join("");
  const old = legacy.map((f) => `<article class="finding"><div class="finding-head"><strong>${esc(f.summary)}</strong>${chip(fmt.sentence(f.severity), SEVERITY[f.severity] || "", "alert")}</div>
    <h4>${f.ai_generated ? icon("sparkles", "sm") : icon("info", "sm")}Recommendation ${f.ai_generated ? chip("AI-written", "ai", "sparkles") : chip("rule-based", "outline")}</h4><p class="${f.ai_generated ? "ai-text" : "plain-text"}">${esc(f.recommendation)}</p>
    <h4>Evidence</h4><p class="mono muted">${(f.evidence || []).map(esc).join(" · ")}</p></article>`).join("");
  return ranked + old;
}

function bindHold(button, onDone) {
  let timer = null;
  const start = (ev) => {
    if (button.disabled) return;
    ev.preventDefault();
    button.classList.add("holding");
    timer = setTimeout(() => { button.classList.remove("holding"); onDone(); }, 1000);
  };
  const cancel = () => { clearTimeout(timer); button.classList.remove("holding"); };
  button.addEventListener("pointerdown", start);
  ["pointerup", "pointerleave", "pointercancel"].forEach((e) => button.addEventListener(e, cancel));
  button.addEventListener("keydown", (ev) => { if ((ev.key === " " || ev.key === "Enter") && !ev.repeat) start(ev); });
  button.addEventListener("keyup", cancel);
}

// ------------------------------------------------------------------ safety
const AUDIT_LABELS = {
  safety_preflight_started: ["Preflight", ""], fault_started: ["Fault injected", "warn"], experiment_aborted: ["Kill switch fired", "bad"],
  cleanup_complete: ["Rolled back", "flow"], cleanup_failed: ["Cleanup failed", "bad"], recovery_checked: ["Recovery checked", "ok"],
  experiment_refused: ["Refused: steady state", "warn"], runner_error: ["Runner error", "bad"],
};

async function loadSafety() {
  const [limits, watchdog, audit, slis, baselines] = await Promise.all([policy(), getJson("/api/v1/watchdog?limit=20"), getJson("/api/v1/audit?limit=60"), getJson("/api/v1/slis?window=30"), getJson("/api/v1/baselines?limit=1")]);
  setLiveChip(slis);
  const t = limits.abort_thresholds;
  const baseline = baselines.baselines[0];
  const events = watchdog.events || [];
  const faultRunning = events[0]?.event === "armed";
  const p95Limit = baseline ? baseline.write_p95_ms * t.write_p95_multiplier : null;
  const missing = (slis.write_samples ?? 0) < t.min_write_samples || (slis.freshness_samples ?? 0) < t.min_freshness_samples;
  const rows = [
    { name: "Write latency p95", hint: baseline ? `${t.write_p95_multiplier}× baseline ${fmt.ms(baseline.write_p95_ms)}` : "needs a baseline", value: slis.write_p95_ms, limit: p95Limit, f: fmt.ms },
    { name: "Write errors", hint: "share of failed writes", value: slis.write_error_rate, limit: t.write_error_rate, f: fmt.pct },
    { name: "Freshness p95", hint: "SSE staleness", value: slis.freshness_p95_seconds, limit: t.freshness_p95_seconds, f: fmt.secs },
    { name: "Missing samples", hint: missing ? "would abort: missing counts as unhealthy" : `min ${t.min_write_samples} writes · ${t.min_freshness_samples} freshness`, text: `${slis.write_samples ?? 0} · ${slis.freshness_samples ?? 0}`, breach: missing, ratio: missing ? 1 : 0.2 },
    { name: "Other services", hint: "checked by the runner during a fault", text: "runner only", ratio: 0, na: true },
    { name: "Max duration", hint: "policy cap on any fault", text: `— / ${limits.max_duration_seconds} s`, ratio: 0, na: true },
  ].map((r) => {
    const has = num(r.value) !== null && num(r.limit) !== null;
    const ratio = r.ratio ?? (has ? clamp(r.value / r.limit, 0, 1) : num(r.value) === null ? 1 : 0);
    const breach = r.na ? false : r.breach ?? (has ? r.value > r.limit : num(r.value) === null);
    return { ...r, ratio, breach, tripped: false, display: r.text ?? `${num(r.value) === null ? "no samples" : r.f(r.value)} / ${num(r.limit) === null ? "?" : r.f(r.limit)}` };
  });
  const view = $("#view-safety");
  const runs = groupAudit(audit.events || []);
  view.innerHTML = `
    <div class="page-head"><div><p class="eyebrow">Deterministic control plane · configs/experiment-policy.yaml</p><h1 id="h-safety">Safety</h1>
      <p>The kill switch, the watchdog and the policy that bounds every experiment. Limits come only from the policy file; an observation can never set its own limit.</p></div></div>
    <div class="bento">
      <div class="c7">${killSwitchPanel(KS_STATES, faultRunning ? "watching" : "armed", rows, `<div class="heartbeat"><span class="label">${faultRunning ? `Fault running now · watchdog armed on ${esc(events[0].target)}` : "Idle · no fault running"}</span></div>`, { title: "Kill switch · live check", why: "The same rules the runner applies, checked now against the last 30 s of live samples. If a fault started now, this is what it would see.", source: slis.live ? "live · last 30 s" : "no live samples" })}</div>
      <div class="c5">
        ${tile({ cls: "t-red", title: "Manual kill switch", iconName: "octagon", why: "Ends a running fault and rolls back.", body: `<button class="hold-button" type="button" disabled><svg class="hold-ring" viewBox="0 0 44 44" aria-hidden="true"><circle class="t" cx="22" cy="22" r="18"/><circle class="v" cx="22" cy="22" r="18"/></svg><span class="stop-core"></span><span><b>Hold 1 s to kill</b><small>${faultRunning ? "A fault is running, but this UI cannot stop it: use the operator terminal." : "No fault is running. This UI never executes or stops faults; the operator terminal does."}</small></span></button><p class="muted" style="margin-top:10px">Try it in a replay on the Runs page.</p>` })}
        <div style="margin-top:16px">${tile({ cls: "t-purple", title: "Watchdog · second line of defence", iconName: "eye", why: `An independent process. If the runner dies mid-fault, it restores the target after a ${limits.watchdog_grace_seconds} s grace.`, source: `${events.length} events`, body: events.length ? `<div class="row-list">${events.slice(0, 8).map((e) => `<div class="row-item"><div><b>${e.event === "armed" ? `Armed on ${esc(e.target)}` : fmt.sentence(e.event)}</b><small>${e.action ? `${esc(actionLabel(e.action))} · ` : ""}pid ${esc(e.pid ?? "?")}${e.result ? ` · ${esc(e.result)}` : ""}</small></div><span class="mono muted">${esc(fmt.clock(e.ts))}</span></div>`).join("")}</div>` : emptyState("No watchdog events yet", "<p>The operator CLI arms the watchdog when a real fault starts and disarms it after rollback. Dry runs and planning never arm it.</p>") })}</div>
      </div>
    </div>
    <div class="bento">
      ${tile({ cls: "c6", title: "Policy and limits", iconName: "shield", why: "What the control plane may touch, and for how long.", source: `<code>compose project ${esc(limits.compose_project)}</code>`, body: `
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">${limits.allowed_targets.map((x) => chip(`${x} · testable`, "info", "target")).join("")}${limits.denied_targets.map((x) => chip(`${x} · protected`, "bad", "lock")).join("")}</div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Action</th><th>Rollback</th></tr></thead><tbody>${limits.enabled_actions.map((a) => `<tr><td><strong>${icon(ACTIONS[a]?.icon || "zap", "sm")} ${esc(actionLabel(a))}</strong></td><td>${esc(ACTIONS[a]?.rollback || "predefined")}</td></tr>`).join("")}</tbody></table></div>
        <div class="kv" style="margin-top:14px">
          <div><span>Max fault duration</span><strong>${esc(limits.max_duration_seconds)} s</strong></div>
          <div><span>Max risk score</span><strong>${esc(limits.max_risk_score ?? "—")}</strong></div>
          <div><span>Approval</span><strong>${limits.approval_required ? "required" : "optional"} · ${esc(limits.approval_expires_after_minutes)} min · single use</strong></div>
          <div><span>Baseline max age</span><strong>${esc(Math.round(limits.baseline_max_age_seconds / 60))} min</strong></div>
          <div><span>Recovery timeout</span><strong>${esc(limits.recovery_timeout_seconds)} s</strong></div>
          <div><span>Watchdog grace</span><strong>${esc(limits.watchdog_grace_seconds)} s</strong></div>
          <div><span>Operator CLI</span><strong>${limits.operator_execution_enabled ? "enabled" : "disabled"}</strong></div>
          <div><span>API executes</span><strong style="color:var(--red-fg)">${limits.api_executes ? "yes" : "never · 503"}</strong></div>
        </div>` })}
      ${tile({ cls: "c6", title: "Abort thresholds", iconName: "octagon", why: `Checked on every ${t.poll_interval_seconds} s poll against the last ${t.window_seconds} s.`, body: `<div class="table-wrap"><table class="table"><tbody>
        <tr><td><strong>Write errors</strong></td><td>Abort above <strong>${esc(fmt.pct(t.write_error_rate))}</strong></td></tr>
        <tr><td><strong>Write latency p95</strong></td><td>Abort above <strong>${esc(t.write_p95_multiplier)}× the baseline p95</strong></td></tr>
        <tr><td><strong>Freshness p95</strong></td><td>Abort above <strong>${esc(t.freshness_p95_seconds)} s</strong></td></tr>
        <tr><td><strong>Missing samples</strong></td><td>Fewer than ${esc(t.min_write_samples)} writes or ${esc(t.min_freshness_samples)} freshness sample(s): <strong>abort</strong>. Missing is never healthy.</td></tr>
        <tr><td><strong>Other services</strong></td><td>Any unrelated component not running or healthy: <strong>abort</strong></td></tr>
        <tr><td><strong>Max duration</strong></td><td>Any fault past <strong>${esc(limits.max_duration_seconds)} s</strong> is ended</td></tr>
        <tr><td><strong>Search errors</strong></td><td class="muted">Not an abort signal: it is the expected impact of a search fault</td></tr>
      </tbody></table></div>` })}
    </div>
    ${tile({ title: "Audit trail", iconName: "file", why: "Every step the control plane took, grouped by run.", source: `${(audit.events || []).length} events`, body: runs.length ? runs.map(([runId, list]) => `<div class="audit-run"><div class="audit-run-head"><a class="mono" href="#/runs/${encodeURIComponent(runId)}">${esc(runId)}</a><span class="muted">${esc(fmt.date(list[0].recorded_at))}</span></div>
        <div class="audit-steps">${list.map((e, i) => { const [label, kind] = AUDIT_LABELS[e.event] || [fmt.sentence(e.event), ""]; return `${i ? `<span class="arrow">${icon("arrow", "sm")}</span>` : ""}<span title="${esc(fmt.clock(e.recorded_at))}${e.payload?.reason ? ` · ${esc(fmt.label(e.payload.reason))}` : ""}">${chip(`${label}${e.payload?.verified === false ? " · not verified" : ""}`, kind)}</span>`; }).join("")}</div></div>`).join("")
        : emptyState("No audit events yet", "<p>Every preflight, fault start, abort, cleanup and recovery check is written here by the control plane when the operator CLI runs an experiment.</p>") })}`;
  stamp();
}

function groupAudit(events) {
  const groups = new Map();
  for (const e of [...events].sort((a, b) => Date.parse(a.recorded_at) - Date.parse(b.recorded_at))) {
    const id = e.payload?.run_id || "no run id";
    (groups.get(id) || groups.set(id, []).get(id)).push(e);
  }
  return [...groups.entries()].sort((a, b) => Date.parse(b[1][0].recorded_at) - Date.parse(a[1][0].recorded_at));
}

window.addEventListener("hashchange", route);
route();

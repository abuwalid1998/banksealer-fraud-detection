// UI helpers: escaping, formatting, chips, tables, modals, drawers, toasts and small SVG charts.
export const S = { me: null, info: null };

export function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

// ---------- formatting ----------
function tz() { return (S.me && S.me.tenant && S.me.tenant.timezone) || undefined; }
function parse(iso) { if (!iso) return null; const d = new Date(iso); return isNaN(d) ? null : d; }
export function fmtDate(iso, { time = true, seconds = false } = {}) {
  const d = parse(iso); if (!d) return "";
  try {
    return new Intl.DateTimeFormat("en-GB", { timeZone: tz(), day: "2-digit", month: "short", year: "numeric",
      ...(time ? { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) } : {}) }).format(d);
  } catch { return d.toLocaleString(); }
}
export function fmtRel(iso) {
  const d = parse(iso); if (!d) return "";
  const s = (Date.now() - d.getTime()) / 1000, a = Math.abs(s);
  const f = a < 60 ? `${Math.round(a)}s` : a < 3600 ? `${Math.round(a / 60)}m` : a < 86400 ? `${Math.round(a / 3600)}h` : `${Math.round(a / 86400)}d`;
  return s >= 0 ? `${f} ago` : `in ${f}`;
}
export function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || v === "") return "–";
  return Number(v).toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function fmtMoney(v, cur) {
  if (v === null || v === undefined) return "•••";
  return `${fmtNum(v, 2)} ${cur || (S.me && S.me.tenant.currency) || ""}`.trim();
}
export function fmtScore(v) { return v === null || v === undefined ? "–" : Number(v).toFixed(2); }
export function pct(a, b) { return b ? `${((100 * a) / b).toFixed(1)}%` : "–"; }
export function label(s) { return s ? String(s).replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase()) : ""; }

// ---------- chips ----------
export function chip(value, kind) {
  if (value === null || value === undefined || value === "") return '<span class="muted">–</span>';
  const k = (kind || value).toString().toLowerCase();
  return `<span class="chip c-${esc(k)}">${esc(label(value).toUpperCase())}</span>`;
}
export const decisionChip = (d) => chip(d, d === "ERROR_POLICY" ? "error_policy" : d);
export const riskChip = (r) => chip(r);

// ---------- icons (inline SVG, stroke currentColor) ----------
const P = {
  overview: "M3 13h8V3H3zm10 8h8V11h-8zM3 21h8v-6H3zm10-18v6h8V3z",
  transactions: "M4 7h13l-3-3M20 17H7l3 3",
  customers: "M16 20v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm13 9v-1a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  alerts: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4m0 4h.01",
  cases: "M3 7h18v13H3zM8 7V4h8v3M3 12h18",
  decision: "M12 3 4 7v6c0 4.4 3.4 7.6 8 8.5 4.6-.9 8-4.1 8-8.5V7zm-3 9 2 2 4-4",
  reports: "M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zm0 0v6h6M8 13h8M8 17h5",
  models: "M12 3a9 9 0 1 0 9 9M12 3v9h9M12 3a9 9 0 0 1 9 9",
  integrations: "M8 3v4M16 3v4M6 7h12v4a6 6 0 0 1-12 0zm6 10v4",
  policies: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
  operations: "M3 12h4l3-8 4 16 3-8h4",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7.4-3a7.4 7.4 0 0 0-.1-1.3l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2.2-1.3L14.4 3h-4l-.3 2.4a7.6 7.6 0 0 0-2.2 1.3l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.6l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2.2 1.3l.3 2.4h4l.3-2.4a7.6 7.6 0 0 0 2.2-1.3l2.4 1 2-3.4-2-1.6c.1-.4.1-.9.1-1.3z",
  search: "m21 21-4.3-4.3M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15z",
  download: "M12 3v12m0 0-4-4m4 4 4-4M4 17v3h16v-3",
  plus: "M12 5v14M5 12h14",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  play: "M7 4v16l13-8z", stop: "M6 6h12v12H6z", check: "m5 12 5 5L20 7", menu: "M4 6h16M4 12h16M4 18h16",
  empty: "M3 7h18v13H3zM3 7l3-4h12l3 4M9 12h6",
  user: "M20 21v-1a5 5 0 0 0-5-5H9a5 5 0 0 0-5 5v1M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  clusters: "M7 8a3 3 0 1 0 0-.01M17 7a2 2 0 1 0 0-.01M15 17a3.5 3.5 0 1 0 0-.01M6 18a1.5 1.5 0 1 0 0-.01",
  notifications: "M4 6h16v12H4zM4 7l8 6 8-6",
  agents: "M12 3v3M8 6h8a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V9a3 3 0 0 1 3-3zM9.5 11v1.5M14.5 11v1.5M3 11v3M21 11v3",
  database: "M4 6c0 1.7 3.6 3 8 3s8-1.3 8-3-3.6-3-8-3-8 1.3-8 3zm0 0v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  key: "M15 7a4 4 0 1 1-3.9 5H3v3h3v2h3v-2h2.1A4 4 0 0 1 15 7z",
  heart: "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z",
  sliders: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  shield: "M12 3 4 7v6c0 4.4 3.4 7.6 8 8.5 4.6-.9 8-4.1 8-8.5V7z",
  screening: "M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15zm10.5 3-5.2-5.2M7.5 10.5h6M10.5 7.5v6",
  reviews: "M9 11l2 2 4-4M5 4h14v17H5zM9 2h6v4H9z",
  str: "M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h5",
  network: "M6 6a2 2 0 1 0 0-.01M18 6a2 2 0 1 0 0-.01M12 18a2 2 0 1 0 0-.01M7.5 7.5l3.4 8.8M16.5 7.5l-3.4 8.8M8 6h8",
  networks: "M5 7a2 2 0 1 0 0-.01M19 7a2 2 0 1 0 0-.01M12 19a2 2 0 1 0 0-.01M5 19a2 2 0 1 0 0-.01M7 7h10M6 9l5 8M18 9l-5 8M7 19h3",
  cheques: "M3 7h18v10H3zM6 11h7M6 14h4M15 14h3M17 7V5",
  card: "M3 6h18v12H3zM3 10h18M7 15h4",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z",
  shield: "M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z",
  server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
  sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
};
export function icon(name, extra = "") {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" ${extra}><path d="${P[name] || P.empty}"/></svg>`;
}
export const LOGO = `<svg viewBox="0 0 64 64" fill="none"><path d="M32 5 L54 13 V30 C54 44 44.5 54.5 32 59 C19.5 54.5 10 44 10 30 V13 Z" stroke="#0BAA9B" stroke-width="5" stroke-linejoin="round"/><path d="M21 32 L29 40 L44 24" stroke="#0BAA9B" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

// ---------- feedback ----------
export function toast(msg, type = "ok", ms = 3800) {
  const el = document.createElement("div");
  el.className = `toast ${type === "error" ? "err" : type === "warn" ? "warn" : ""}`;
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}
export function errToast(e) { toast(e && e.message ? e.message : String(e), "error", 6000); }
export const loading = () => '<div class="loading"><span class="spin"></span></div>';
export const empty = (msg = "Nothing to show yet.") => `<div class="empty">${icon("empty")}${esc(msg)}</div>`;

// Busy state for a button while an async action runs.
export async function busy(btn, fn) {
  const old = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>'; }
  try { return await fn(); } finally { if (btn) { btn.disabled = false; btn.innerHTML = old; } }
}

// ---------- modal / drawer ----------
export function modal({ title, body, actions = [], wide = false, onOpen }) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "overlay";
    ov.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="hd"><h2>${esc(title)}</h2><button class="x" aria-label="Close">×</button></div>
      <div class="bd">${body}</div>
      <div class="ft">${actions.map((a, i) => `<button class="btn ${a.cls || ""}" data-i="${i}">${esc(a.label)}</button>`).join("")}</div></div>`;
    document.body.appendChild(ov);
    const close = (v) => { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") close(null); };
    document.addEventListener("keydown", onKey);
    ov.querySelector(".x").onclick = () => close(null);
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(null); });
    ov.querySelectorAll(".ft .btn").forEach((b) => {
      b.onclick = async () => {
        const a = actions[+b.dataset.i];
        if (!a.onClick) return close(a.value ?? null);
        try {
          const r = await busy(b, () => a.onClick(ov.querySelector(".modal")));
          if (r !== false) close(r === undefined ? true : r);
        } catch (e) { errToast(e); }
      };
    });
    const first = ov.querySelector("input,select,textarea");
    if (first) setTimeout(() => first.focus(), 30);
    if (onOpen) onOpen(ov.querySelector(".modal"));
  });
}
export function confirmDialog(title, text, okLabel = "Confirm", cls = "primary") {
  return modal({ title, body: `<p>${esc(text)}</p>`, actions: [{ label: "Cancel", value: false }, { label: okLabel, cls, value: true }] });
}
export function drawer(html, onClose) {
  document.querySelectorAll(".drawer-wrap").forEach((d) => d.remove());
  const w = document.createElement("div");
  w.className = "drawer-wrap";
  w.innerHTML = `<div class="drawer" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(w);
  const close = () => { w.remove(); document.removeEventListener("keydown", onKey); if (onClose) onClose(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  w.addEventListener("mousedown", (e) => { if (e.target === w) close(); });
  w.querySelectorAll("[data-close]").forEach((b) => (b.onclick = close));
  return { el: w.querySelector(".drawer"), close, set(html2) { w.querySelector(".drawer").innerHTML = html2; w.querySelectorAll("[data-close]").forEach((b) => (b.onclick = close)); } };
}
export function formData(root) {
  const out = {};
  root.querySelectorAll("[name]").forEach((el) => {
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = el.value === "" ? null : Number(el.value);
    else out[el.name] = el.value;
  });
  return out;
}

// ---------- tables ----------
// columns: [{key, label, render?(row), num?, cls?}] ; rows: array ; opts: {onRow, rowKey, empty, selectable}
export function table(columns, rows, opts = {}) {
  if (!rows || !rows.length) return empty(opts.empty || "No records match the current filters.");
  const head = `<tr>${opts.selectable ? '<th style="width:28px"><input type="checkbox" data-selall></th>' : ""}${columns
    .map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr>`;
  const body = rows.map((r, i) => `<tr class="${opts.onRow ? "click" : ""}" data-row="${i}">${opts.selectable
    ? `<td><input type="checkbox" data-sel="${i}" ${opts.isSelectable && !opts.isSelectable(r) ? "disabled" : ""}></td>` : ""}${columns
    .map((c) => `<td class="${c.num ? "num" : ""} ${c.cls || ""}">${c.render ? c.render(r) : esc(r[c.key])}</td>`).join("")}</tr>`).join("");
  return `<div class="tbl-wrap"><table class="tbl"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}
export function bindTable(root, rows, onRow) {
  root.querySelectorAll("tr[data-row]").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("input,button,a,select")) return;
      onRow(rows[+tr.dataset.row], e);
    });
  });
}
export function pager(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  return `<div class="pager"><span>${fmtNum(total)} records · page ${page} of ${pages}</span>
    <button class="btn sm" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Previous</button>
    <button class="btn sm" data-page="${page + 1}" ${page >= pages ? "disabled" : ""}>Next</button></div>`;
}
export function kv(pairs) {
  return `<dl class="kv">${pairs.filter((p) => p).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v === undefined || v === null || v === "" ? '<span class="muted">–</span>' : v}</dd>`).join("")}</dl>`;
}
export function kpi(labelText, value, hint = "", cls = "") {
  return `<div class="kpi ${cls}"><div class="label">${esc(labelText)}</div><div class="value">${value}</div>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
}
export function card(title, body, { actions = "", sub = "", flush = false, id = "" } = {}) {
  return `<section class="card" ${id ? `id="${id}"` : ""}><div class="hd"><h2>${esc(title)}</h2>${sub ? `<span class="sub">${sub}</span>` : ""}<div class="actions">${actions}</div></div><div class="bd ${flush ? "flush" : ""}">${body}</div></section>`;
}
export function scoreBar(v, labelText) {
  const x = Math.max(0, Math.min(1, Number(v) || 0));
  const cls = x >= 0.8 ? "red" : x >= 0.5 ? "amber" : "teal";
  return `<span>${esc(labelText)}</span><div class="bar ${cls}"><i style="width:${(x * 100).toFixed(1)}%"></i></div><b class="right">${x.toFixed(2)}</b>`;
}
export function codes(list) {
  return list && list.length ? `<div class="codes">${list.map((c) => `<span class="code">${esc(c)}</span>`).join("")}</div>` : '<span class="muted">–</span>';
}
export function options(list, selected, { blank = null } = {}) {
  const items = blank !== null ? [["", blank], ...list] : list;
  return items.map((o) => {
    const [v, t] = Array.isArray(o) ? o : [o, label(o)];
    return `<option value="${esc(v)}" ${String(v) === String(selected ?? "") ? "selected" : ""}>${esc(t)}</option>`;
  }).join("");
}

// ---------- charts (dependency-free SVG) ----------
export const COLORS = { PASS: "#0BAA9B", REVIEW: "#3569C8", HOLD: "#E8A33A", DECLINE: "#C84B55", ERROR_POLICY: "#6B7C93",
  LOW: "#0BAA9B", MEDIUM: "#3569C8", HIGH: "#E8A33A", CRITICAL: "#C84B55" };
const PALETTE = ["#10233F", "#0BAA9B", "#3569C8", "#E8A33A", "#C84B55", "#7A8CA6", "#5B3D8C", "#2E8B57"];

export function stackedBars(data, keys, { height = 180, labelKey = "day", colors = COLORS } = {}) {
  if (!data.length) return empty("No data in this period.");
  const W = 640, H = height, pad = { l: 34, r: 8, t: 8, b: 22 };
  const max = Math.max(1, ...data.map((d) => keys.reduce((s, k) => s + (d[k] || 0), 0)));
  const bw = (W - pad.l - pad.r) / data.length;
  let bars = "", ticks = "";
  data.forEach((d, i) => {
    let y = H - pad.b;
    keys.forEach((k) => {
      const v = d[k] || 0; if (!v) return;
      const h = ((H - pad.t - pad.b) * v) / max;
      y -= h;
      bars += `<rect x="${pad.l + i * bw + bw * 0.18}" y="${y}" width="${bw * 0.64}" height="${h}" rx="2" fill="${colors[k] || "#999"}"><title>${esc(d[labelKey])} · ${esc(k)}: ${v}</title></rect>`;
    });
    if (data.length <= 16 || i % Math.ceil(data.length / 12) === 0)
      ticks += `<text x="${pad.l + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(String(d[labelKey]).slice(5))}</text>`;
  });
  const grid = [0, 0.5, 1].map((f) => { const y = H - pad.b - f * (H - pad.t - pad.b);
    return `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" stroke="#E1E7EE"/><text x="${pad.l - 6}" y="${y + 3}" text-anchor="end">${Math.round(f * max)}</text>`; }).join("");
  const legend = `<div class="legend">${keys.map((k) => `<span><i style="background:${colors[k]}"></i>${esc(label(k))}</span>`).join("")}</div>`;
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img">${grid}${bars}${ticks}</svg>${legend}</div>`;
}
export function hbars(items, { color = "#3569C8", fmt = (v) => fmtNum(v), max: forcedMax } = {}) {
  if (!items.length) return empty("No data.");
  const max = forcedMax || Math.max(1, ...items.map((i) => i.value));
  return `<div style="display:grid;grid-template-columns:minmax(90px,max-content) 1fr 52px;gap:8px 10px;align-items:center;font-size:12.5px">${items
    .map((i) => `<span class="nowrap" title="${esc(i.label)}" style="max-width:240px;overflow:hidden;text-overflow:ellipsis">${esc(i.label)}</span><div class="bar"><i style="width:${(100 * i.value) / max}%;background:${i.color || color}"></i></div><b class="right">${fmt(i.value)}</b>`)
    .join("")}</div>`;
}
export function donut(items, { size = 150, centerLabel = "" } = {}) {
  const total = items.reduce((s, i) => s + i.value, 0);
  if (!total) return empty("No data.");
  const r = 52, c = 2 * Math.PI * r;
  let off = 0, arcs = "";
  items.forEach((it, i) => {
    const len = (c * it.value) / total;
    arcs += `<circle r="${r}" cx="70" cy="70" fill="none" stroke="${it.color || PALETTE[i % PALETTE.length]}" stroke-width="18" stroke-dasharray="${len} ${c - len}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)"><title>${esc(it.label)}: ${it.value}</title></circle>`;
    off += len;
  });
  return `<div class="row" style="gap:18px;align-items:center"><svg viewBox="0 0 140 140" width="${size}" height="${size}">${arcs}<text x="70" y="68" text-anchor="middle" style="font-size:20px;font-weight:700;fill:#10233F">${fmtNum(total)}</text><text x="70" y="86" text-anchor="middle">${esc(centerLabel)}</text></svg>
    <div class="legend" style="flex-direction:column;gap:6px">${items.map((it, i) => `<span><i style="background:${it.color || PALETTE[i % PALETTE.length]}"></i>${esc(it.label)} <b>${fmtNum(it.value)}</b></span>`).join("")}</div></div>`;
}
export function lineChart(points, { height = 150, yMax = 1, threshold = null, colorFn } = {}) {
  if (!points.length) return empty("No scored transactions yet.");
  const W = 640, H = height, pad = { l: 30, r: 8, t: 8, b: 18 };
  const x = (i) => pad.l + (i * (W - pad.l - pad.r)) / Math.max(1, points.length - 1);
  const y = (v) => H - pad.b - (Math.min(v, yMax) / yMax) * (H - pad.t - pad.b);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const dots = points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.v)}" r="3" fill="${colorFn ? colorFn(p) : "#3569C8"}"><title>${esc(p.label)}: ${Number(p.v).toFixed(2)}</title></circle>`).join("");
  const th = threshold !== null ? `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(threshold)}" y2="${y(threshold)}" stroke="#E8A33A" stroke-dasharray="4 4"/>` : "";
  const grid = [0, 0.5, 1].map((f) => `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(f * yMax)}" y2="${y(f * yMax)}" stroke="#EDF1F5"/><text x="${pad.l - 5}" y="${y(f * yMax) + 3}" text-anchor="end">${(f * yMax).toFixed(1)}</text>`).join("");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}">${grid}${th}<path d="${path}" fill="none" stroke="#9FB3CF" stroke-width="1.5"/>${dots}</svg></div>`;
}
export function hourBars(hours) {
  const max = Math.max(1, ...hours);
  const W = 480, H = 90, bw = W / 24;
  const bars = hours.map((v, h) => `<rect x="${h * bw + 2}" y="${H - 16 - ((H - 22) * v) / max}" width="${bw - 4}" height="${((H - 22) * v) / max}" rx="2" fill="#3569C8"><title>${h}:00 · ${v}</title></rect>${h % 3 === 0 ? `<text x="${h * bw + bw / 2}" y="${H - 3}" text-anchor="middle">${h}</text>` : ""}`).join("");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}">${bars}</svg></div>`;
}

// ---------- live refresh ----------
// Runs fn every ms while el is on screen; pauses while the tab is hidden or an overlay is open.
export function live(el, ms, fn) {
  let busyRun = false;
  const id = setInterval(async () => {
    if (!el.isConnected) return clearInterval(id);
    if (document.hidden || busyRun || document.querySelector(".overlay, .drawer-wrap, .menu")) return;
    busyRun = true;
    try { await fn(); } catch { /* next tick */ } finally { busyRun = false; }
  }, ms);
  return () => clearInterval(id);
}
export function liveBadge(text = "LIVE") {
  return `<span class="livepill"><i></i>${esc(text)}</span>`;
}

// ---------- switches ----------
export function toggle(name, checked, labelText = "", { id = "" } = {}) {
  return `<label class="switch" ${id ? `id="${id}"` : ""}><input type="checkbox" name="${esc(name)}" ${checked ? "checked" : ""}><span class="track"><i></i></span>${labelText ? `<span>${esc(labelText)}</span>` : ""}</label>`;
}
export function statusDot(ok, text) {
  return `<span class="sdot ${ok ? "on" : "off"}"><i></i>${esc(text)}</span>`;
}

// ---------- scatter plot (clusters) ----------
// points: [{x, y, group, label, size?, ring?}] ; groups: {key: {label, color}}
export function scatter(points, groups, { height = 380, centers = [], onPoint } = {}) {
  if (!points.length) return empty("No points to plot.");
  const W = 760, H = height, pad = 26;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const q = (arr, f) => { const s = [...arr].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.max(0, Math.round(f * (s.length - 1))))]; };
  // robust bounds so a few extreme outliers do not squash the clusters
  let x0 = q(xs, 0.01), x1 = q(xs, 0.99), y0 = q(ys, 0.01), y1 = q(ys, 0.99);
  const dx = (x1 - x0) || 1, dy = (y1 - y0) || 1;
  x0 -= dx * 0.12; x1 += dx * 0.12; y0 -= dy * 0.12; y1 += dy * 0.12;
  const sx = (v) => pad + ((Math.min(Math.max(v, x0), x1) - x0) / (x1 - x0)) * (W - 2 * pad);
  const sy = (v) => H - pad - ((Math.min(Math.max(v, y0), y1) - y0) / (y1 - y0)) * (H - 2 * pad);
  const grid = [0.25, 0.5, 0.75].map((f) => `<line x1="${pad}" x2="${W - pad}" y1="${pad + f * (H - 2 * pad)}" y2="${pad + f * (H - 2 * pad)}" stroke="var(--grid)"/><line y1="${pad}" y2="${H - pad}" x1="${pad + f * (W - 2 * pad)}" x2="${pad + f * (W - 2 * pad)}" stroke="var(--grid)"/>`).join("");
  const dots = points.map((p, i) => {
    const g = groups[p.group] || { color: "#9AA8B9" };
    const clipped = p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1;
    return `<circle data-i="${i}" cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="${p.size || 4}" fill="${g.color}" fill-opacity="${p.ring ? 0.95 : 0.7}" stroke="${p.ring ? "#C84B55" : "#fff"}" stroke-width="${p.ring ? 2 : 0.8}" ${clipped ? 'stroke-dasharray="2 1"' : ""}><title>${esc(p.label)}</title></circle>`;
  }).join("");
  const cs = centers.map((c) => { const g = groups[c.group] || {}; return `<g transform="translate(${sx(c.x)},${sy(c.y)})"><path d="M-7 0H7M0 -7V7" stroke="${g.color || "#10233F"}" stroke-width="3"/><circle r="10" fill="none" stroke="${g.color || "#10233F"}" stroke-width="1.5"/><text y="-13" text-anchor="middle" style="font-weight:700;fill:var(--ink)">${esc(c.label || "")}</text></g>`; }).join("");
  const legend = `<div class="legend">${Object.entries(groups).map(([k, g]) => `<span><i style="background:${g.color}"></i>${esc(g.label)}</span>`).join("")}</div>`;
  const id = "sc" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => {
    const svg = document.getElementById(id);
    if (!svg || !onPoint) return;
    svg.addEventListener("click", (e) => { const c = e.target.closest("circle[data-i]"); if (c) onPoint(points[+c.dataset.i]); });
  });
  return `<div class="chart scatter"><svg id="${id}" viewBox="0 0 ${W} ${H}" role="img">${grid}${dots}${cs}</svg>${legend}</div>`;
}
export const CLUSTER_COLORS = ["#3569C8", "#0BAA9B", "#E8A33A", "#5B3D8C", "#2E8B57", "#C84B55", "#10233F", "#D9730D", "#0E7490", "#8B5CF6", "#B45309", "#64748B"];

// ---------- area / line trend with two series ----------
export function trendChart(rows, { key = "count", key2 = null, labelKey = "day", height = 170, color = "#3569C8", color2 = "#E8A33A", fmt = (v) => fmtNum(v) } = {}) {
  if (!rows.length) return empty("No data in this period.");
  const W = 640, H = height, pad = { l: 44, r: 10, t: 10, b: 22 };
  const max = Math.max(4, ...rows.map((r) => r[key] || 0));
  const x = (i) => pad.l + (i * (W - pad.l - pad.r)) / Math.max(1, rows.length - 1);
  const y = (v, m = max) => H - pad.b - ((v || 0) / m) * (H - pad.t - pad.b);
  const line = rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[key]).toFixed(1)}`).join("");
  const area = `${line}L${x(rows.length - 1)},${H - pad.b}L${x(0)},${H - pad.b}Z`;
  let second = "";
  if (key2) {
    const m2 = Math.max(1, ...rows.map((r) => r[key2] || 0));
    second = `<path d="${rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[key2], m2).toFixed(1)}`).join("")}" fill="none" stroke="${color2}" stroke-width="2" stroke-dasharray="5 3"/>`;
  }
  const grid = [0, 0.5, 1].map((f) => `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(f * max)}" y2="${y(f * max)}" stroke="var(--grid)"/><text x="${pad.l - 6}" y="${y(f * max) + 3}" text-anchor="end">${fmt(f * max)}</text>`).join("");
  const step = Math.ceil(rows.length / 10);
  const ticks = rows.map((r, i) => i % step === 0 ? `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(String(r[labelKey]).slice(5))}</text>` : "").join("");
  const dots = rows.map((r, i) => `<circle cx="${x(i)}" cy="${y(r[key])}" r="2.5" fill="${color}"><title>${esc(r[labelKey])}: ${fmt(r[key])}${key2 ? ` · ${esc(label(key2))}: ${fmtNum(r[key2])}` : ""}</title></circle>`).join("");
  const gid = "g" + Math.random().toString(36).slice(2, 8);
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}"><defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>${grid}<path d="${area}" fill="url(#${gid})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2.2"/>${second}${dots}${ticks}</svg></div>`;
}

// ---------- network graph (force-directed, dependency-free) ----------
// data: {nodes: [{id, kind, label, name?, risk?, alerts?, focus?, watched?}], edges: [{source, target, kind, count, fraud}]}
export function networkGraph(data, { height = 520, onNode } = {}) {
  const nodes = data.nodes.map((n) => ({ ...n }));
  if (!nodes.length) return empty("No linked accounts in this period.");
  const W = 900, H = height;
  const idx = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
  const edges = data.edges.filter((e) => e.source in idx && e.target in idx);
  nodes.forEach((n, i) => {
    const a = (i / nodes.length) * Math.PI * 2;
    n.x = W / 2 + Math.cos(a) * (n.focus ? 0 : 180 + (i % 5) * 18);
    n.y = H / 2 + Math.sin(a) * (n.focus ? 0 : 150 + (i % 7) * 12);
  });
  for (let it = 0; it < 260; it++) {
    const t = 1 - it / 260;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y; let d2 = dx * dx + dy * dy || 0.01;
        if (d2 > 90000) continue;
        const f = 1800 / d2; const d = Math.sqrt(d2);
        dx /= d; dy /= d; a.x += dx * f * t; a.y += dy * f * t; b.x -= dx * f * t; b.y -= dy * f * t;
      }
    }
    edges.forEach((e) => {
      const a = nodes[idx[e.source]], b = nodes[idx[e.target]];
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1, k = (d - 70) * 0.02 * t;
      a.x += (dx / d) * k; a.y += (dy / d) * k; b.x -= (dx / d) * k; b.y -= (dy / d) * k;
    });
    nodes.forEach((n) => {
      if (n.focus) { n.x += (W / 2 - n.x) * 0.2; n.y += (H / 2 - n.y) * 0.2; }
      n.x += (W / 2 - n.x) * 0.004; n.y += (H / 2 - n.y) * 0.004;
      n.x = Math.max(24, Math.min(W - 24, n.x)); n.y = Math.max(20, Math.min(H - 20, n.y));
    });
  }
  const color = { customer: "#3569C8", counterparty: "#0BAA9B", device: "#5B3D8C", ip: "#E8A33A" };
  const lines = edges.map((e) => {
    const a = nodes[idx[e.source]], b = nodes[idx[e.target]];
    return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke="${e.fraud ? "#C84B55" : "#B7C3D3"}" stroke-width="${Math.min(4, 1 + Math.log2(e.count || 1))}" ${e.direction === "in" ? 'stroke-dasharray="4 3"' : ""}><title>${esc(e.kind)} · ${e.count} operation(s)${e.amount ? ` · ${fmtNum(e.amount, 0)}` : ""}</title></line>`;
  }).join("");
  const shapes = nodes.map((n, i) => {
    const c = n.kind === "customer" ? (n.risk === "HIGH" ? "#C84B55" : color.customer) : color[n.kind] || "#999";
    const r = n.kind === "customer" ? (n.focus ? 13 : 8 + Math.min(5, n.alerts || 0)) : 6;
    const shape = n.kind === "customer" ? `<circle r="${r}" fill="${c}" stroke="${n.focus ? "#10233F" : "#fff"}" stroke-width="${n.focus ? 3 : 1.5}"/>`
      : `<rect x="-${r}" y="-${r}" width="${2 * r}" height="${2 * r}" rx="2" fill="${c}" stroke="${n.watched ? "#C84B55" : "#fff"}" stroke-width="${n.watched ? 3 : 1.2}" transform="rotate(45)"/>`;
    const showLabel = nodes.length <= 40 || n.focus || n.alerts || n.watched || n.kind !== "customer";
    const text = !showLabel ? "" : n.kind === "customer" ? `${n.label}${n.name ? " · " + n.name : ""}` : String(n.label).slice(0, 22);
    return `<g data-n="${i}" transform="translate(${n.x.toFixed(1)},${n.y.toFixed(1)})" style="cursor:${n.kind === "customer" ? "pointer" : "default"}">${shape}
      <text y="${r + 11}" text-anchor="middle" style="font-size:9.5px;fill:var(--ink-2)">${esc(text.length > 30 ? text.slice(0, 29) + "…" : text)}</text>
      <title>${esc(n.kind)}: ${esc(n.label)}${n.name ? " – " + esc(n.name) : ""}${n.alerts ? ` · ${n.alerts} alert(s)` : ""}${n.watched ? " · WATCH-LISTED" : ""}</title></g>`;
  }).join("");
  const id = "ng" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => {
    const svg = document.getElementById(id); if (!svg || !onNode) return;
    svg.addEventListener("click", (e) => { const g = e.target.closest("g[data-n]"); if (g) onNode(nodes[+g.dataset.n]); });
  });
  const legend = `<div class="legend"><span><i style="background:${color.customer}"></i>Customer</span><span><i style="background:#C84B55"></i>High-risk customer / confirmed fraud link</span>
    <span><i style="background:${color.counterparty}"></i>Counterparty account</span><span><i style="background:${color.device}"></i>Device</span><span><i style="background:${color.ip}"></i>IP address</span><span>dashed = money in</span></div>`;
  return `<div class="chart"><svg id="${id}" viewBox="0 0 ${W} ${H}" style="background:linear-gradient(#FBFCFE,#F6F8FB);border:1px solid var(--line-2);border-radius:10px">${lines}${shapes}</svg>${legend}</div>`;
}

// ---------- city heat map (schematic map of Palestinian cities) ----------
const CITY_POS = { Jenin: [35.30, 32.46], Tubas: [35.37, 32.32], Tulkarm: [35.03, 32.31], Nablus: [35.26, 32.22], Qalqilya: [34.97, 32.19],
  Salfit: [35.18, 32.08], Ramallah: [35.20, 31.90], "Al-Bireh": [35.22, 31.91], Jericho: [35.45, 31.86], Jerusalem: [35.23, 31.78],
  Bethlehem: [35.20, 31.70], Hebron: [35.10, 31.53], Gaza: [34.46, 31.50], "Khan Younis": [34.31, 31.35], Rafah: [34.25, 31.29] };
export function cityHeatMap(rows, { valueKey = "count", rateKey = null, height = 360 } = {}) {
  const items = rows.filter((r) => CITY_POS[r.city]);
  if (!items.length) return empty("No city information in this period.");
  const W = 360, H = height;
  const x = (lon) => 30 + ((lon - 34.15) / (35.6 - 34.15)) * (W - 60);
  const y = (lat) => 20 + ((32.6 - lat) / (32.6 - 31.2)) * (H - 40);
  const max = Math.max(...items.map((r) => r[valueKey] || 0), 1);
  const maxRate = rateKey ? Math.max(...items.map((r) => r[rateKey] || 0), 0.0001) : 1;
  const heat = (v) => { const t = Math.min(1, v); return `rgb(${Math.round(11 + 189 * t)},${Math.round(170 - 95 * t)},${Math.round(155 - 70 * t)})`; };
  const outline = `<path d="M${x(35.55)},${y(32.55)} L${x(34.95)},${y(32.45)} L${x(34.92)},${y(32.05)} L${x(35.05)},${y(31.55)} L${x(35.25)},${y(31.35)} L${x(35.55)},${y(31.45)} L${x(35.57)},${y(31.85)} Z" fill="#EEF3F8" stroke="#C9D3DE"/>
    <path d="M${x(34.55)},${y(31.6)} L${x(34.2)},${y(31.32)} L${x(34.25)},${y(31.22)} L${x(34.5)},${y(31.45)} Z" fill="#EEF3F8" stroke="#C9D3DE"/>`;
  const dots = items.map((r) => {
    const [lon, lat] = CITY_POS[r.city];
    const rad = 6 + 18 * Math.sqrt((r[valueKey] || 0) / max);
    const fill = heat(rateKey ? (r[rateKey] || 0) / maxRate : (r[valueKey] || 0) / max);
    return `<g transform="translate(${x(lon).toFixed(1)},${y(lat).toFixed(1)})"><circle r="${rad.toFixed(1)}" fill="${fill}" fill-opacity=".8" stroke="#fff" stroke-width="1.5"><title>${esc(r.city)}: ${fmtNum(r[valueKey])}${rateKey ? ` · ${(100 * (r[rateKey] || 0)).toFixed(1)}% intervention rate` : ""}</title></circle>
      <text y="${(rad + 11).toFixed(1)}" text-anchor="middle" style="font-size:10px;fill:var(--ink-2)">${esc(r.city)}</text></g>`;
  }).join("");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" style="max-height:${H}px">${outline}${dots}</svg>
    <div class="legend"><span>circle size = ${rateKey ? "transactions" : "volume"}</span><span><i style="background:${heat(0)}"></i>low</span><span><i style="background:${heat(1)}"></i>high ${rateKey ? "intervention rate" : ""}</span></div></div>`;
}

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

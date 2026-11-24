// Customer clusters: how the segmentation model and the fraud engine's peer-group model group customers.
import { api } from "../api.js";
import { navigate } from "../app.js";
import { CLUSTER_COLORS, S, card, chip, empty, esc, fmtNum, hbars, kv, label, loading, scatter, table } from "../ui.js";

const RISK = { LOW: "#0BAA9B", MEDIUM: "#3569C8", HIGH: "#C84B55" };

export async function render(el) {
  el.innerHTML = loading();
  const d = await api.get("/models/clusters");
  let tab = d.segmentation ? "segments" : "peers";
  let colorBy = "segment";
  const draw = () => {
    el.innerHTML = `<div class="tabs"><button data-t="segments" class="${tab === "segments" ? "on" : ""}">Customer segments (KYC + behaviour)</button>
      <button data-t="peers" class="${tab === "peers" ? "on" : ""}">Fraud-engine peer groups (DBSCAN + CBLOF)</button></div><div id="cl"></div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { tab = b.dataset.t; draw(); }));
    const box = el.querySelector("#cl");
    if (tab === "segments") segments(box, d.segmentation, colorBy, (c) => { colorBy = c; draw(); });
    else peers(box, d.peer_groups);
  };
  draw();
}

function openCustomer(ref) { if (S.me.navigation.some((n) => n.id === "customers")) navigate(`#/customers/${encodeURIComponent(ref)}`); }

function segments(box, s, colorBy, setColor) {
  if (!s) { box.innerHTML = empty("No active segmentation model in this workspace yet."); return; }
  const groups = {};
  s.segments.forEach((g, i) => { groups[g.key] = { label: `S${g.key + 1} · ${g.size} customers`, color: CLUSTER_COLORS[i % CLUSTER_COLORS.length] }; });
  const riskGroups = { LOW: { label: "Low risk (institution)", color: RISK.LOW }, MEDIUM: { label: "Medium risk", color: RISK.MEDIUM }, HIGH: { label: "High risk", color: RISK.HIGH } };
  const pts = s.points.map((p) => ({ x: p.x, y: p.y, group: colorBy === "risk" ? p.risk : p.segment, ring: p.alerts > 0, size: p.alerts > 0 ? 5 : 3.6, ref: p.ref,
    label: `${p.ref}${p.name ? " · " + p.name : ""}\nSegment S${(p.segment ?? -1) + 1} · ${p.type.toLowerCase()} · risk ${p.risk}${p.alerts ? `\n${p.alerts} alert(s)` : ""}` }));
  const centers = colorBy === "risk" ? [] : s.centers.map((c) => ({ x: c.x, y: c.y, group: c.segment, label: `S${c.segment + 1}` }));
  const m = s.metrics;
  box.innerHTML = `<div class="grid g-21">
    ${card("Customer map", `<div class="row" style="margin-bottom:10px"><span class="small muted">Colour by</span>
        <button class="btn sm ${colorBy === "segment" ? "primary" : ""}" data-c="segment">Segment</button><button class="btn sm ${colorBy === "risk" ? "primary" : ""}" data-c="risk">Institution risk rating</button>
        <span class="spacer"></span><span class="tiny muted">Red ring = customer with alerts · click a dot to open the customer</span></div>
      ${scatter(pts, colorBy === "risk" ? riskGroups : groups, { centers, onPoint: (p) => openCustomer(p.ref) })}
      <p class="tiny muted" style="margin-top:8px">2-D projection (PCA) of the standardised features the model uses; it explains ${Math.round(100 * (s.explained_variance[0] + s.explained_variance[1]))}% of the variation. Axes are mainly driven by: ${esc(s.axis_drivers.join(", "))}.</p>`,
      { sub: esc(s.model) })}
    ${card("How the model groups customers", `${kv([["Algorithm", esc(s.algorithm)], ["Segments (k)", fmtNum(m.k)], ["Silhouette", `${m.silhouette} <span class="tiny muted">(separation; > 0.25 is reasonable for behaviour data)</span>`],
        ["Stability", `${m.stability_rand_index} <span class="tiny muted">(Rand index across re-runs)</span>`], ["Customers", fmtNum(m.customers)], ["With history", fmtNum(m.with_history)], ["Cold start (KYC only)", fmtNum(m.cold_start)]])}
      <p class="small" style="margin-top:12px">Each customer is compared with the peers of his segment. Segments are <b>neutral comparison groups</b>, not risk ratings: they make "unusual for this kind of customer" measurable from day one.</p>
      <h3 style="margin:14px 0 6px">Customers per segment</h3>${hbars(s.segments.map((g, i) => ({ label: `S${g.key + 1}`, value: g.size, color: CLUSTER_COLORS[i % CLUSTER_COLORS.length] })))}`)}
  </div>
  <h3 style="margin:18px 0 10px">Segment profiles</h3>
  <div class="grid g3">${s.segments.map((g, i) => segCard(g, CLUSTER_COLORS[i % CLUSTER_COLORS.length])).join("")}</div>`;
  box.querySelectorAll("[data-c]").forEach((b) => (b.onclick = () => setColor(b.dataset.c)));
}

function segCard(g, color) {
  const prof = Object.values(g.profile || {});
  const risk = g.risk_mix || {};
  const total = Object.values(risk).reduce((a, b) => a + b, 0) || 1;
  return `<div class="seg-card"><div><span class="sw" style="background:${color}"></span><b>${esc(g.label)}</b></div>
    <div class="meta">${fmtNum(g.size)} customers · ${fmtNum(g.alerts)} alerts · ${esc(g.description)}</div>
    <div class="bar" style="display:flex;height:8px">${["LOW", "MEDIUM", "HIGH"].map((r) => `<i style="width:${(100 * (risk[r] || 0)) / total}%;background:${RISK[r]};border-radius:0"></i>`).join("")}</div>
    <div class="tiny muted">Institution risk mix: ${["LOW", "MEDIUM", "HIGH"].map((r) => `${label(r)} ${risk[r] || 0}`).join(" · ")}</div>
    <table class="tbl" style="font-size:12px;margin-top:4px"><tbody>${prof.slice(0, 8).map((p) => `<tr><td class="muted">${esc(p.label)}</td><td class="num"><b>${fmtNum(p.centre, p.centre < 10 ? 2 : 0)}</b></td><td class="num tiny muted">${p.p10 !== null ? `${fmtNum(p.p10, p.p10 < 10 ? 2 : 0)}–${fmtNum(p.p90, p.p90 < 10 ? 2 : 0)}` : ""}</td></tr>`).join("")}</tbody></table>
  </div>`;
}

function peers(box, g) {
  if (!g) { box.innerHTML = empty("The fraud engine has no fitted peer groups in this workspace yet."); return; }
  const groups = { "-1": { label: "Noise / outliers (no dense group)", color: "#C84B55" } };
  g.groups.forEach((x, i) => { groups[x.cluster] = { label: `Group #${x.cluster} · ${x.size}${x.large ? "" : " (small)"}`, color: x.large ? CLUSTER_COLORS[i % CLUSTER_COLORS.length] : "#9AA8B9" }; });
  const pts = g.points.map((p) => ({ x: p.x, y: p.y, group: p.cluster, ring: p.score >= 0.7, size: 3 + 3 * p.score, ref: p.ref,
    label: `${p.ref}${p.name ? " · " + p.name : ""}\nPeer group #${p.cluster} · outlier score ${p.score.toFixed(2)}` }));
  const top = [...g.points].sort((a, b) => b.score - a.score).slice(0, 12);
  box.innerHTML = `<div class="grid g-21">
    ${card("Peer-group map", `${scatter(pts, groups, { onPoint: (p) => openCustomer(p.ref) })}
      <p class="tiny muted" style="margin-top:8px">Each dot is a customer's 30-day spending vector (whitened, Mahalanobis space) projected to 2-D. Dot size grows with the CBLOF outlier score; red ring = score ≥ 0.70 (strong outlier among peers).</p>`, { sub: esc(g.model || "") })}
    ${card("How the engine finds peers", `${kv([["Algorithm", esc(g.algorithm)], ...Object.entries(g.parameters).map(([k, v]) => [label(k), esc(v)]), ["Groups", fmtNum(g.groups.length)], ["Customers in no dense group", fmtNum(g.noise_customers)]])}
      <p class="small" style="margin-top:12px">The <b>global profile</b> adds a score when a customer's overall spending pattern sits far from every large peer group – e.g. a retail customer suddenly behaving like a business. It is one of three profiles combined into the fraud score (with the customer's own history and the 24-hour window).</p>`)}
  </div>
  <div class="grid g2" style="margin-top:16px">
    ${card("Peer groups", table([{ label: "Group", render: (r) => `<b>#${r.cluster}</b> ${r.large ? chip("LARGE", "ok") : chip("SMALL", "neutral")}` }, { label: "Customers", key: "size", num: true },
      { label: "Outliers", key: "outliers", num: true }, { label: "Transfers / 30d", num: true, render: (r) => fmtNum(r.profile["bank transfers per 30 days"], 1) },
      { label: "Avg amount", num: true, render: (r) => fmtNum(r.profile["average amount per operation"], 0) }, { label: "Foreign share", num: true, render: (r) => `${Math.round(100 * (r.profile["share of transfers to foreign IBANs"] || 0))}%` }], g.groups), { flush: true })}
    ${card("Most unusual customers vs peers", table([{ label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.ref)}">${esc(r.ref)}</a> <span class="tiny muted">${esc(r.name)}</span>` },
      { label: "Group", render: (r) => r.cluster === -1 ? chip("NOISE", "warn") : `#${r.cluster}` }, { label: "Outlier score", num: true, render: (r) => `<b>${r.score.toFixed(2)}</b>` }], top), { flush: true })}
  </div>`;
}

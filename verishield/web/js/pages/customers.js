// Customer list and Customer 360 profile (BRD 9.4).
import { api } from "../api.js";
import { navigate } from "../app.js";
import { S, bindTable, busy, card, chip, decisionChip, errToast, esc, fmtDate, fmtMoney, fmtNum, fmtRel, fmtScore,
  hbars, hourBars, kpi, kv, label, lineChart, live, liveBadge, loading, modal, options, pager, table, toast } from "../ui.js";
import { createCaseDialog, openTransaction } from "./transactions.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el, { sub, params, setTitle }) {
  if (sub[0]) return profile(el, sub[0], setTitle);
  const f = { search: params.search || "", segment: params.segment || "", risk: "", customer_type: "", history: params.history || "", sort: params.sort || "risk", page: 1 };
  let segs = [];
  if (can("models.view") || can("customers.view_full")) { try { segs = (await api.get("/models/segments")).segments; } catch { /* optional */ } }
  el.innerHTML = `${card("Customers", `<div class="filters" id="flt">
      <input class="input" type="search" name="search" placeholder="Customer reference or name" value="${esc(f.search)}">
      <select class="input" name="segment">${options(segs.map((s) => [s.segment_key, s.label]), f.segment, { blank: "All peer segments" })}</select>
      <select class="input" name="risk">${options(["LOW", "MEDIUM", "HIGH", "UNKNOWN"], "", { blank: "All risk categories" })}</select>
      <select class="input" name="customer_type">${options(["INDIVIDUAL", "BUSINESS"], "", { blank: "All types" })}</select>
      <select class="input" name="history">${options([["limited", "Limited history (cold start)"], ["established", "Established history"]], f.history, { blank: "Any history" })}</select>
      <select class="input" name="sort">${options([["risk", "Sort: highest live risk"], ["recent", "Sort: most recent activity"], ["", "Sort: customer reference"]], f.sort)}</select>
    </div>`, { sub: `${liveBadge()} Scores update as transactions are decided. Peer segments are comparison aids, not risk grades.` })}<div class="card" style="margin-top:16px" id="list">${loading()}</div>`;
  const load = async () => {
    const q = new URLSearchParams(Object.entries(f).filter(([, v]) => v !== "")).toString();
    const box = el.querySelector("#list");
    try {
      const d = await api.get(`/customers?${q}&page_size=50`);
      box.innerHTML = table([
        { label: "Customer", render: (r) => `<b>${esc(r.customer_ref)}</b><div class="tiny muted">${esc(r.display_name)}</div>` },
        { label: "Type", render: (r) => esc(label(r.customer_type)) },
        { label: "Institution risk", render: (r) => r.risk_category ? chip(r.risk_category) : '<span class="muted">masked</span>' },
        { label: "Peer segment", render: (r) => r.segment_label ? `<span class="small">${esc(r.segment_label)}</span>` : '<span class="muted">unassigned</span>' },
        { label: "Fit", num: true, render: (r) => r.segment_confidence ? `${Math.round(r.segment_confidence * 100)}%` : "–" },
        { label: "Transactions", num: true, render: (r) => fmtNum(r.transactions) },
        { label: "Latest score", num: true, render: (r) => r.latest_score === undefined || r.latest_score === null ? "–" : `${fmtScore(r.latest_score)} <span class="tiny muted">${esc(fmtRel(r.latest_at))}</span>` },
        { label: "Max score 30d", num: true, render: (r) => r.max_score_30d === undefined || r.max_score_30d === null ? "–" : `<b style="color:${r.max_score_30d >= 0.8 ? "var(--red)" : r.max_score_30d >= 0.5 ? "#B7791F" : "inherit"}">${fmtScore(r.max_score_30d)}</b>` },
        { label: "Last decision", render: (r) => r.latest_decision ? decisionChip(r.latest_decision) : '<span class="muted">–</span>' },
        { label: "History", render: (r) => r.limited_history ? chip("LIMITED", "warn") : chip("ESTABLISHED", "ok") },
        { label: "Open alerts", num: true, render: (r) => r.open_alerts === null ? "–" : r.open_alerts ? `<b style="color:var(--red)">${r.open_alerts}</b>` : "0" },
      ], d.items, { onRow: true }) + pager(d.total, f.page, 50);
      bindTable(box, d.items, (r) => { if (!r.customer_ref.includes("•")) navigate(`#/customers/${encodeURIComponent(r.customer_ref)}`); });
      box.querySelectorAll("[data-page]").forEach((b) => (b.onclick = () => { f.page = +b.dataset.page; load(); }));
    } catch (e) { errToast(e); }
  };
  let t;
  el.querySelectorAll("#flt [name]").forEach((inp) => inp.addEventListener(inp.type === "search" ? "input" : "change", () => {
    f[inp.name] = inp.value; f.page = 1; clearTimeout(t); t = setTimeout(load, 250);
  }));
  load();
  live(el, 12000, load);
}

async function profile(el, ref, setTitle) {
  await drawProfile(el, ref, setTitle);
  live(el, 12000, () => drawProfile(el, ref, setTitle, true));
}

async function drawProfile(el, ref, setTitle, silent = false) {
  const y = window.scrollY;
  const d = await api.get(`/customers/${encodeURIComponent(ref)}/profile`);
  const p = d.profile, a = d.aggregates;
  setTitle(`Customer ${ref}`, "Customers › Customer 360");
  const actions = [];
  if (can("cases.create")) actions.push('<button class="btn" data-act="refer">Refer to analyst</button><button class="btn" data-act="alert">Manual alert</button>');
  if (can("reports.aml") || can("reports.executive") || can("reports.assigned")) actions.push('<button class="btn" data-act="pdf">Profile report</button>');
  el.innerHTML = `<div class="row" style="margin-bottom:14px"><a href="#/customers" class="btn sm ghost">← Customers</a><span class="spacer"></span>${actions.join("")}</div>
    ${d.limited_history ? `<div class="notice warn" style="margin-bottom:14px"><b>Limited history.</b> ${esc(d.monitoring_treatment)}</div>` : ""}
    <div class="kpis">
      ${kpi("Customer", esc(ref), esc(p.display_name || label(p.customer_type)), "accent")}
      ${kpi("Institution risk", p.risk_category ? chip(p.risk_category) : "–", esc(label(p.kyc_review_status || "")))}
      ${kpi("Transactions", fmtNum(a.transactions), d.limited_history ? "limited history" : "established profile")}
      ${kpi("30-day turnover", fmtMoney(a.last_30d_amount), a.turnover_ratio ? `${a.turnover_ratio}× declared` : "", a.turnover_ratio > 2.5 ? "warn" : "")}
      ${kpi("Median amount", fmtMoney(a.median_amount))}
    </div>
    <div class="grid g2" style="margin-top:16px">
      ${card("Profile & KYC", kv([["Type", esc(label(p.customer_type))], ["Business activity", esc(p.business_activity)], ["Country", esc(p.country)],
        ["Tenure", p.tenure_months !== undefined ? `${p.tenure_months} months` : ""], ["Products", esc((p.products || []).join(", "))],
        ["Expected monthly turnover", p.expected_monthly_turnover !== undefined ? fmtMoney(p.expected_monthly_turnover) : ""],
        ["Expected monthly transactions", esc(p.expected_monthly_tx)], ["Account purpose", esc(p.account_purpose)], ["KYC review", chip(p.kyc_review_status)]]))}
      ${card("Peer segment", `${kv([["Segment", `<b>${esc(p.segment_label || "Unassigned")}</b>`], ["Assignment confidence", p.segment_confidence ? `${Math.round(p.segment_confidence * 100)}%` : "–"],
        ["Basis", esc(p.segment_basis)], ["Model", `<span class="mono small">${esc(p.segment_model)}</span>`]])}
        ${d.segment ? `<p class="small muted" style="margin-top:10px">${esc(d.segment.description)}</p>` : ""}
        <p class="tiny muted" style="margin-top:8px">A segment is a peer-comparison aid – not a label of guilt, a regulatory risk grade or a standalone reason to block.</p>`)}
    </div>
    ${d.score_timeline ? `<div style="margin-top:16px">${card("Fraud score timeline (live)", lineChart(d.score_timeline.map((s) => ({ v: s.score, label: `${fmtDate(s.ts)} ${s.decision}`, decision: s.decision })), { threshold: 0.5, colorFn: (pt) => ({ PASS: "#0BAA9B", REVIEW: "#3569C8", HOLD: "#E8A33A", DECLINE: "#C84B55" }[pt.decision] || "#6B7C93") }), { sub: "Dashed line: default review threshold" })}</div>` : ""}
    <div class="grid g3" style="margin-top:16px">
      ${card("Usual hours", hourBars(d.usual.hours))}
      ${card("Usual channels", hbars(d.usual.channels.map(([l, v]) => ({ label: label(l), value: v }))))}
      ${card("Top beneficiaries & countries", hbars(d.usual.beneficiaries.map(([l, v]) => ({ label: l, value: v }))) + '<div style="height:12px"></div>' + hbars(d.usual.countries.map(([l, v]) => ({ label: l, value: v, color: "#0BAA9B" }))))}
    </div>
    ${d.transactions ? `<div style="margin-top:16px">${card("Recent transactions", table([
      { label: "Time", render: (r) => fmtDate(r.ts) }, { label: "Transaction", render: (r) => `<span class="mono small">${esc(r.tx_ref)}</span>` },
      { label: "Product", render: (r) => esc(label(r.tx_type)) }, { label: "Amount", num: true, render: (r) => fmtMoney(r.amount, r.currency) },
      { label: "Beneficiary", render: (r) => `<span class="mono small">${esc(r.beneficiary)}</span>` }, { label: "Timing", render: (r) => chip(r.mode) },
      { label: "Score", num: true, render: (r) => fmtScore(r.fraud_score) }, { label: "Decision", render: (r) => r.decision ? decisionChip(r.decision) : chip("HISTORY", "neutral") },
    ], d.transactions, { onRow: true }), { flush: true, id: "txs" })}</div>` : ""}
    ${d.alerts ? `<div class="grid g2" style="margin-top:16px">
      ${card("Alerts", table([{ label: "Raised", render: (r) => fmtRel(r.created_at) }, { label: "Severity", render: (r) => chip(r.severity) },
        { label: "Decision", render: (r) => decisionChip(r.decision) }, { label: "Status", render: (r) => chip(r.status) }, { label: "Summary", render: (r) => `<span class="small">${esc(r.summary)}</span>` }], d.alerts, { empty: "No alerts for this customer." }), { flush: true })}
      ${card("Investigations", table([{ label: "Case", render: (r) => `<a href="#/cases/${encodeURIComponent(r.id)}">${esc(r.id)}</a>` }, { label: "Status", render: (r) => chip(r.status) },
        { label: "Assignee", key: "assignee_name" }, { label: "Disposition", render: (r) => esc((r.disposition || {}).label || "") }], d.cases, { empty: "No cases for this customer." }), { flush: true })}
    </div>` : ""}
    ${d.engine_profile && d.engine_profile.temporal && d.engine_profile.temporal.mean ? `<div style="margin-top:16px">${card("Learned behavioural baseline (BankSealer profiles)", `<div class="grid g2">
      <div>${kv(Object.entries(d.engine_profile.temporal.mean).map(([k, v]) => [label(k) + " (24h mean)", `${fmtNum(v, 2)} <span class="muted small">± ${fmtNum(d.engine_profile.temporal.std[k], 2)}</span>`]))}</div>
      <div>${kv(Object.entries(d.engine_profile.local || {}).map(([k, v]) => [label(k), `${fmtNum(v.count)} past operations${v.amount_median ? ` · median ${fmtMoney(v.amount_median)}` : ""}`]))}
      ${d.engine_profile.global && d.engine_profile.global.cluster !== undefined ? `<p class="small muted" style="margin-top:8px">Engine peer group #${esc(d.engine_profile.global.cluster)} · outlier score ${fmtScore(d.engine_profile.global.score)}</p>` : ""}</div></div>`)}</div>` : ""}`;
  if (silent) window.scrollTo(0, y);
  if (d.transactions) {
    const box = el.querySelector("#txs");
    if (box) el.querySelectorAll("#txs tr[data-row]").forEach((tr) => tr.addEventListener("click", () => openTransaction(d.transactions[+tr.dataset.row].tx_ref)));
  }
  const act = (n) => el.querySelector(`[data-act=${n}]`);
  if (act("refer")) act("refer").onclick = async () => {
    const r = await createCaseDialog({ customer_refs: [ref], referral: true });
    if (r) navigate(`#/cases/${encodeURIComponent(r.id)}`);
  };
  if (act("alert")) act("alert").onclick = async () => {
    const r = await modal({ title: `Manual alert for ${ref}`, body: `<label class="f">Summary<textarea class="input" name="summary" required></textarea></label>
      <label class="f" style="margin-top:10px">Severity<select class="input" name="severity">${options(["LOW", "MEDIUM", "HIGH", "CRITICAL"], "MEDIUM")}</select></label>`,
      actions: [{ label: "Cancel" }, { label: "Raise alert", cls: "primary", onClick: (m) => api.post("/alerts", { customer_ref: ref,
        summary: m.querySelector("[name=summary]").value, severity: m.querySelector("[name=severity]").value }) }] });
    if (r) toast("Alert raised in the AML queue");
  };
  if (act("pdf")) act("pdf").onclick = (e) => busy(e.currentTarget, async () => {
    try { const j = await api.post("/reports", { type: "customer_profile", format: "pdf", params: { customer_ref: ref } });
      await api.download(`/reports/${encodeURIComponent(j.id)}/download`); } catch (err) { errToast(err); }
  });
}

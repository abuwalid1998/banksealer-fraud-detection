// Transactions list + decision detail drawer (shared with other pages).
import { api } from "../api.js";
import { navigate } from "../app.js";
import { S, bindTable, busy, card, chip, codes, decisionChip, drawer, errToast, esc, fmtDate, fmtMoney, fmtScore,
  kv, label, loading, modal, options, pager, scoreBar, table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el, { params }) {
  const f = { search: params.search || "", decision: params.decision || "", risk_level: "", mode: "", tx_type: "",
    channel: "", execution_state: params.execution_state || "", date_from: "", date_to: "", page: 1 };
  el.innerHTML = `${card("Transactions", `<div class="filters" id="flt">
      <input class="input" type="search" name="search" placeholder="Transaction, customer or beneficiary" value="${esc(f.search)}">
      <select class="input" name="decision">${options(["PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY"], f.decision, { blank: "All decisions" })}</select>
      <select class="input" name="risk_level">${options(["LOW", "MEDIUM", "HIGH", "CRITICAL"], "", { blank: "All risk levels" })}</select>
      <select class="input" name="mode">${options([["PRE_AUTH", "Pre-authorisation"], ["POST_EVENT", "Post-event"]], "", { blank: "All timings" })}</select>
      <select class="input" name="tx_type">${options(["bank_transfer", "phone_recharge", "prepaid_card"], "", { blank: "All products" })}</select>
      <select class="input" name="execution_state">${options(["PENDING", "HELD", "AUTHORISED", "DECLINED", "SETTLED", "EXPIRED", "RELEASE_REQUESTED", "DECLINE_REQUESTED", "RECALLED"], f.execution_state, { blank: "All execution states" })}</select>
      <input class="input" type="date" name="date_from" title="From"><input class="input" type="date" name="date_to" title="To">
    </div>`, { sub: can("tx.view_masked") && !can("tx.view_full") ? "Technical view – customer identifiers and amounts are masked" :
      can("tx.view_assigned") ? "Only transactions linked to your assigned cases" : "" })}
    <div class="card" style="margin-top:16px" id="list">${loading()}</div>`;
  const load = async () => {
    const q = new URLSearchParams(Object.entries(f).filter(([, v]) => v !== "" && v !== null)).toString();
    const box = el.querySelector("#list");
    try {
      const d = await api.get(`/transactions?${q}&page_size=50`);
      box.innerHTML = table([
        { label: "Time", render: (r) => `<span class="nowrap">${fmtDate(r.ts)}</span>` },
        { label: "Transaction", render: (r) => `<span class="mono">${esc(r.tx_ref)}</span>` },
        { label: "Customer", render: (r) => esc(r.customer_ref) },
        { label: "Product", render: (r) => esc(label(r.tx_type)) },
        { label: "Amount", num: true, render: (r) => fmtMoney(r.amount, r.currency) },
        { label: "Channel", render: (r) => esc(label(r.channel)) },
        { label: "Timing", render: (r) => chip(r.mode) },
        { label: "Score", num: true, render: (r) => fmtScore(r.fraud_score) },
        { label: "Decision", render: (r) => decisionChip(r.decision) },
        { label: "AML", render: (r) => r.aml_signal_level && r.aml_signal_level !== "NONE" ? chip(r.aml_signal_level) : '<span class="muted">–</span>' },
        { label: "Execution", render: (r) => chip(r.execution_state) },
      ], d.items, { onRow: true }) + pager(d.total, f.page, 50);
      bindTable(box, d.items, (r) => openTransaction(r.tx_ref, load));
      box.querySelectorAll("[data-page]").forEach((b) => (b.onclick = () => { f.page = +b.dataset.page; load(); }));
    } catch (e) { errToast(e); }
  };
  let t;
  el.querySelectorAll("#flt [name]").forEach((inp) => inp.addEventListener(inp.type === "search" ? "input" : "change", () => {
    f[inp.name] = inp.value; f.page = 1; clearTimeout(t); t = setTimeout(load, 250);
  }));
  await load();
  if (params.open) openTransaction(params.open, load);
}

export async function openTransaction(ref, onChange) {
  const dr = drawer(`<div class="dh"><h2>Transaction ${esc(ref)}</h2><button class="x" data-close>×</button></div><div class="db">${loading()}</div>`);
  let d;
  try { d = await api.get(`/transactions/${encodeURIComponent(ref)}`); } catch (e) { errToast(e); return dr.close(); }
  const t = d.transaction, dec = d.decision;
  const masked = !!t.masked;
  const actions = [];
  if (can("cases.create") && !masked) actions.push(`<button class="btn" data-act="case">Create case</button>`);
  if (d.can_bank_action) actions.push(`<button class="btn teal" data-act="release">Release</button><button class="btn danger" data-act="reject">Reject</button>`);
  if (!masked && (can("reports.aml") || can("reports.executive") || can("reports.assigned"))) actions.push(`<button class="btn" data-act="pdf">PDF report</button>`);
  const exec = d.execution_events.map((e) => `<li><div>${chip(e.state)} <span class="small">${esc(e.source)}</span>${e.actor ? ` · ${esc(e.actor)}` : ""}</div><div class="small">${esc(e.detail)}</div><div class="t">${fmtDate(e.created_at, { seconds: true })}</div></li>`).join("");
  dr.set(`<div class="dh"><div><h2>${esc(label(t.tx_type))} · ${fmtMoney(t.amount, t.currency)}</h2><div class="small muted mono">${esc(t.tx_ref)}</div></div>
      <div class="row" style="margin-left:auto">${decisionChip(t.decision)} ${chip(t.mode)}</div><button class="x" data-close style="margin-left:8px">×</button></div>
    <div class="db">
      ${t.mode === "POST_EVENT" ? '<div class="notice">Post-event monitoring: this transaction was already executed when received. It was <b>not</b> blocked – the outcome is a review signal; use the bank\'s recall/recovery process if needed.</div>' : ""}
      ${dec && dec.decision === "ERROR_POLICY" ? `<div class="notice crit">No reliable decision (${esc(dec.error)}). Institution fallback applied: <b>${esc((dec.shadow || {}).fallback || "")}</b>.</div>` : ""}
      ${actions.length ? `<div class="row">${actions.join("")}</div>` : ""}
      <div class="grid g2">
        ${card("Transaction", kv([["Customer", masked ? esc(t.customer_ref) : `<a href="#/customers/${encodeURIComponent(t.customer_ref)}">${esc(t.customer_ref)}</a>`],
          ["Time", fmtDate(t.ts, { seconds: true })], ["Product", esc(label(t.tx_type))], ["Amount", fmtMoney(t.amount, t.currency)],
          ["Channel", esc(label(t.channel))], ["Source", esc(t.source)], ["Beneficiary", `<span class="mono">${esc(t.beneficiary)}</span>`],
          ["Beneficiary country", esc(t.beneficiary_country)], ["Connection", esc(t.connection)], ["IP", esc(t.ip)],
          t.scenario ? ["Synthetic scenario", chip(t.scenario, "neutral")] : null]))}
        ${dec ? card("Decision", `<div class="scorebars">${scoreBar(dec.local_score, "Local (customer)")}${scoreBar(dec.temporal_score, "Temporal (24h window)")}${scoreBar(dec.global_score, "Global (peer group)")}${scoreBar(dec.fraud_score, "Combined fraud score")}</div>
          <div style="margin-top:12px">${kv([["Decision", decisionChip(dec.decision)], ["Risk level", chip(dec.risk_level)], ["AML signal", chip(dec.aml_signal_level)],
            ["Customer risk context", chip(dec.customer_risk_context)], ["Limited history", dec.undertrained ? chip("YES", "warn") : "No"],
            ["Policy / model", `<span class="mono small">${esc(dec.policy_version)} · ${esc(dec.fraud_model_version)}</span>`],
            ["Decision ID", `<span class="mono small">${esc(dec.id)}</span>`], ["Correlation", `<span class="mono small">${esc(dec.correlation_id)}</span>`],
            ["Latency", `${esc(dec.latency_ms)} ms`]])}</div>`) : card("Decision", '<p class="muted">Historical record – imported for model training, not decided.</p>')}
      </div>
      ${dec && dec.reason_details && dec.reason_details.length ? card("Reason codes", `<div class="tbl-wrap"><table class="tbl"><tbody>${dec.reason_details.map((r) => `<tr><td class="mono small">${esc(r.code)}</td><td>${esc(r.text)}</td></tr>`).join("")}</tbody></table></div>`, { flush: true }) : ""}
      ${dec && dec.explanation ? card("Why", `<pre class="box" style="white-space:pre-wrap">${esc(dec.explanation)}</pre>`) : ""}
      ${dec && dec.aml_indicators && dec.aml_indicators.length ? card("AML indicators", `<ul class="timeline">${dec.aml_indicators.map((i) => `<li><b class="mono small">${esc(i.code)}</b><div>${esc(i.message)}</div></li>`).join("")}</ul><p class="tiny muted">Indicators are inputs for human review – not a finding of money laundering.</p>`) : ""}
      ${dec && dec.shadow && dec.shadow.model_shadow ? card("Shadow model (not used for the decision)", kv([["Model", esc(dec.shadow.model_shadow.model)], ["Score", fmtScore(dec.shadow.model_shadow.fraud_score)], ["Risk level", chip(dec.shadow.model_shadow.risk_level)]])) : ""}
      ${card("Execution status (bank side)", `<ul class="timeline">${exec || '<li class="muted">No execution events.</li>'}</ul><p class="tiny muted">HOLD and DECLINE take effect only when the bank's authorisation system acknowledges them.</p>`)}
      ${d.alerts.length ? card("Alerts", table([{ label: "Alert", render: (a) => `<span class="mono small">${esc(a.id)}</span>` }, { label: "Severity", render: (a) => chip(a.severity) }, { label: "Status", render: (a) => chip(a.status) }, { label: "Case", render: (a) => a.case_id ? `<a href="#/cases/${encodeURIComponent(a.case_id)}">${esc(a.case_id)}</a>` : "–" }], d.alerts), { flush: true }) : ""}
      ${d.cases.length ? card("Cases", d.cases.map((c) => `<a href="#/cases/${encodeURIComponent(c)}" class="btn sm">${esc(c)}</a>`).join(" ")) : ""}
    </div>`);
  dr.el.querySelectorAll("a[href^='#/']").forEach((a) => a.addEventListener("click", () => dr.close()));
  const act = (n) => dr.el.querySelector(`[data-act=${n}]`);
  if (act("pdf")) act("pdf").onclick = (e) => busy(e.currentTarget, async () => {
    try { const j = await api.post("/reports", { type: "transaction_decision", format: "pdf", params: { tx_ref: t.tx_ref } });
      await api.download(`/reports/${encodeURIComponent(j.id)}/download`); } catch (err) { errToast(err); }
  });
  if (act("case")) act("case").onclick = async () => {
    const res = await createCaseDialog({ tx_refs: [t.tx_ref] });
    if (res) { dr.close(); navigate(`#/cases/${encodeURIComponent(res.id)}`); }
  };
  for (const a of ["release", "reject"]) if (act(a)) act(a).onclick = async () => {
    const r = await modal({ title: `${a === "release" ? "Release" : "Reject"} payment ${t.tx_ref}`,
      body: `<p class="small muted">The command is sent through the bank's authorised execution adapter${S.me.tenant.demo_mode ? " (simulated in Demo Mode)" : ""}. A rationale is mandatory and audited.</p><label class="f">Rationale<textarea class="input" name="rationale" required></textarea></label>`,
      actions: [{ label: "Cancel" }, { label: a === "release" ? "Release payment" : "Reject payment", cls: a === "release" ? "teal" : "danger",
        onClick: async (m) => api.post(`/transactions/${encodeURIComponent(t.tx_ref)}/${a}`, { rationale: m.querySelector("[name=rationale]").value }) }] });
    if (r) { toast(r.message || "Command recorded"); dr.close(); openTransaction(ref, onChange); if (onChange) onChange(); }
  };
}

export async function createCaseDialog(seed) {
  let analysts = [];
  try { analysts = (await api.get("/users/assignable")).users; } catch { /* manager only */ }
  const due = new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10);
  const what = [seed.alert_ids && seed.alert_ids.length ? `${seed.alert_ids.length} alert(s)` : "", seed.tx_refs && seed.tx_refs.length ? `${seed.tx_refs.length} transaction(s)` : "",
    seed.customer_refs && seed.customer_refs.length ? `customer ${seed.customer_refs.join(", ")}` : ""].filter(Boolean).join(", ");
  return modal({ title: seed.referral ? "Refer customer profile to an analyst" : "Create AML case", wide: true,
    body: `<p class="small muted" style="margin-bottom:12px">Linked: <b>${esc(what)}</b>. Analysts only see the linked transactions and customers.</p>
      <div class="form-grid">
        <label class="f">Title<input class="input" name="title" placeholder="Optional – generated if empty"></label>
        <label class="f">Priority<select class="input" name="priority">${options(["LOW", "MEDIUM", "HIGH", "URGENT"], "MEDIUM")}</select></label>
        <label class="f">Due date<input class="input" type="date" name="due_at" value="${due}"></label>
        <label class="f">Assign to<select class="input" name="assignee_id">${options(analysts.map((u) => [u.id, `${u.name} (${u.username})`]), "", { blank: "– keep in Manager queue –" })}</select></label>
        <label class="f">Classification<select class="input" name="classification">${options([["AML_SUSPICION", "AML suspicion"], ["FRAUD_SUSPICION", "Fraud suspicion"]], "AML_SUSPICION")}</select></label>
      </div>
      <label class="f" style="margin-top:12px">Purpose / objective<textarea class="input" name="purpose" placeholder="What should the investigation establish?"></textarea></label>
      <label class="f" style="margin-top:12px">Questions to answer (one per line)<textarea class="input" name="questions"></textarea></label>
      ${seed.referral ? `<div class="form-grid" style="margin-top:12px"><label class="f">Review window from<input class="input" type="date" name="window_from"></label><label class="f">to<input class="input" type="date" name="window_to"></label></div>` : ""}`,
    actions: [{ label: "Cancel" }, { label: "Create case", cls: "primary", onClick: async (m) => {
      const v = {}; m.querySelectorAll("[name]").forEach((i) => (v[i.name] = i.value));
      return api.post("/cases", { ...seed, ...v, questions: (v.questions || "").split("\n"), due_at: v.due_at || null,
        window_from: v.window_from || null, window_to: v.window_to || null, assignee_id: v.assignee_id || null });
    } }] });
}

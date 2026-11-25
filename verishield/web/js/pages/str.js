// Regulatory reports: suspicious transaction reports (STR) for the Financial Follow-Up Unit.
import { api } from "../api.js";
import { S, busy, card, chip, drawer, errToast, esc, fmtDate, fmtNum, kpi, kv, loading, modal, options, table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el, { params = {} } = {}) {
  const tabs = [];
  if (can("str.prepare") || can("str.approve")) tabs.push(["str", "Suspicious transaction reports"]);
  if (can("ctr.view") || can("ctr.manage")) tabs.push(["ctr", "Cash transaction reports"], ["stats", "Regulator statistics"]);
  let cur = params.tab || tabs[0][0];
  const draw = () => {
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    ({ str: strList, ctr: ctrTab, stats: statsTab })[cur](el.querySelector("#tab")).catch(errToast);
  };
  draw();
}

async function strList(el) {
  el.innerHTML = loading();
  const d = await api.get("/str");
  el.innerHTML = card("Suspicious transaction reports", table([
    { label: "Report", render: (r) => `<b class="mono small">${esc(r.id)}</b>` }, { label: "Case", render: (r) => `<a href="#/cases/${encodeURIComponent(r.case_id)}">${esc(r.case_id)}</a>` },
    { label: "Status", render: (r) => chip(r.status, r.status === "SUBMITTED" ? "ok" : r.status === "APPROVED" ? "info" : "warn") },
    { label: "Transactions", num: true, render: (r) => fmtNum(r.transactions) }, { label: "Prepared by", key: "prepared_by" },
    { label: "Approved", render: (r) => r.approved_by ? `${esc(r.approved_by)}<div class="tiny muted">${fmtDate(r.approved_at)}</div>` : "–" },
    { label: "FFU reference", render: (r) => r.submitted_ref ? `<span class="mono">${esc(r.submitted_ref)}</span>` : "–" },
  ], d.reports, { onRow: true, empty: "No STR prepared yet – open a case and choose 'Prepare STR'." }), { flush: true,
    sub: "Draft → approved by the AML Manager / Chief → submitted to the FFU (reference recorded). Export as goAML-style XML or branded PDF." });
  el.querySelectorAll("tr[data-row]").forEach((tr) => tr.addEventListener("click", () => openStr(d.reports[+tr.dataset.row].id, () => strList(el))));
}

// ------------------------------------------------------------------ cash transaction reports
async function ctrTab(box, day = "") {
  const d = await api.get(`/ctr${day ? `?business_date=${day}` : ""}`);
  const rows = d.reports;
  const cnt = (st) => rows.filter((r) => r.status === st).length;
  box.innerHTML = `<div class="kpis">${kpi("Reports", fmtNum(rows.length), day ? `business date ${day}` : "latest 500")}${kpi("Draft", fmtNum(cnt("DRAFT")), "to review", cnt("DRAFT") ? "warn" : "")}
      ${kpi("Reviewed", fmtNum(cnt("REVIEWED")), "ready to submit")}${kpi("Submitted", fmtNum(cnt("SUBMITTED")), "reference recorded", "")}
      ${kpi("Threshold", `${fmtNum(d.threshold)} ${esc(S.me.tenant.currency || "")}`, d.aggregate_daily ? "single or aggregated per day" : "single operation")}</div>
    <div style="margin-top:16px">${card("Cash transaction reports", table([
      { label: "", render: (r) => (r.status === "DRAFT" || r.status === "REVIEWED") && d.can_manage ? `<input type="checkbox" data-pick="${r.id}">` : "" },
      { label: "CTR", render: (r) => `<b class="mono small">${esc(r.number)}</b>` }, { label: "Business date", key: "business_date" },
      { label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a><div class="tiny muted">${esc(r.customer_name)}</div>` },
      { label: "Cash in", num: true, render: (r) => fmtNum(r.cash_in, 2) }, { label: "Cash out", num: true, render: (r) => fmtNum(r.cash_out, 2) },
      { label: "Ops", num: true, render: (r) => fmtNum(r.transactions) }, { label: "Trigger", render: (r) => chip(r.trigger, r.trigger === "AGGREGATED" ? "warn" : "neutral") },
      { label: "Status", render: (r) => chip(r.status, r.status === "SUBMITTED" ? "ok" : r.status === "EXEMPT" ? "neutral" : r.status === "REVIEWED" ? "info" : "warn") + (r.submitted_ref ? `<div class="tiny muted">${esc(r.submitted_ref)}</div>` : r.exempt_reason ? `<div class="tiny muted">${esc(r.exempt_reason)}</div>` : "") },
      { label: "", render: (r) => d.can_manage && r.status === "DRAFT" ? `<div class="row nowrap"><button class="btn sm" data-rev="${r.id}">Reviewed</button><button class="btn sm" data-ex="${r.id}">Exempt</button></div>` : "" },
    ], rows, { empty: "No cash transaction reports for this selection." }), { flush: true,
      sub: "Generated hourly on the leader node for the last business days; cash split into several operations on one day is aggregated.",
      actions: `<select class="input" id="day" style="width:auto">${options([["", "All dates"], ...d.dates.map((x) => [x, x])], day)}</select>
        ${d.can_manage ? '<button class="btn sm" id="gen">Generate for a date</button><button class="btn sm primary" id="sub">Record submission</button>' : ""}
        <a class="btn sm" href="#" data-x="xml">XML</a><a class="btn sm" href="#" data-x="csv">CSV</a><a class="btn sm" href="#" data-x="pdf">PDF</a>` })}</div>`;
  box.querySelector("#day").onchange = (e) => ctrTab(box, e.target.value);
  const act = (sel, fn) => box.querySelectorAll(sel).forEach((b) => (b.onclick = () => busy(b, async () => { try { await fn(b); ctrTab(box, day); } catch (e) { errToast(e); } })));
  act("[data-rev]", (b) => api.post(`/ctr/${b.dataset.rev}/review`));
  box.querySelectorAll("[data-ex]").forEach((b) => (b.onclick = async () => {
    const r = await modal({ title: "Exempt this CTR", body: '<label class="f">Reason (e.g. government entity on the exemption list)<textarea class="input" name="reason"></textarea></label>',
      actions: [{ label: "Cancel" }, { label: "Exempt", cls: "primary", onClick: (m) => api.post(`/ctr/${b.dataset.ex}/exempt`, { reason: m.querySelector("[name=reason]").value }) }] });
    if (r) { toast("Exempted"); ctrTab(box, day); }
  }));
  const gen = box.querySelector("#gen");
  if (gen) gen.onclick = async () => {
    const r = await modal({ title: "Generate CTRs", body: '<label class="f">Business date<input class="input" type="date" name="d"></label>',
      actions: [{ label: "Cancel" }, { label: "Generate", cls: "primary", onClick: (m) => api.post("/ctr/generate", { business_date: m.querySelector("[name=d]").value }) }] });
    if (r) { toast(`${r.created} new, ${r.updated} updated`); ctrTab(box, r.business_date || day); }
  };
  const sub = box.querySelector("#sub");
  if (sub) sub.onclick = async () => {
    const ids = [...box.querySelectorAll("[data-pick]:checked")].map((x) => x.dataset.pick);
    if (!ids.length) { toast("Select the reports that were submitted", "warn"); return; }
    const r = await modal({ title: `Record submission of ${ids.length} CTR(s)`, body: '<label class="f">Regulator submission reference<input class="input" name="ref"></label>',
      actions: [{ label: "Cancel" }, { label: "Record", cls: "primary", onClick: (m) => api.post("/ctr/submit", { ids, reference: m.querySelector("[name=ref]").value }) }] });
    if (r) { toast(`${r.submitted} report(s) submitted`); ctrTab(box, day); }
  };
  box.querySelectorAll("[data-x]").forEach((a) => (a.onclick = async (e) => {
    e.preventDefault();
    const ids = [...box.querySelectorAll("[data-pick]:checked")].map((x) => x.dataset.pick).join(",");
    try { await api.download(`/ctr/export?fmt=${a.dataset.x}${ids ? `&ids=${ids}` : day ? `&business_date=${day}` : ""}`, `CTR-${day || "selection"}.${a.dataset.x}`); } catch (err) { errToast(err); }
  }));
}

// ------------------------------------------------------------------ regulator statistics
async function statsTab(box) {
  const first = new Date(); first.setDate(1);
  const prevEnd = new Date(first); prevEnd.setDate(0);
  const prevStart = new Date(prevEnd.getFullYear(), prevEnd.getMonth(), 1);
  const iso = (d) => d.toISOString().slice(0, 10);
  let from = iso(prevStart), to = iso(prevEnd);
  const draw = async () => {
    const s = await api.get(`/regulator/statistics?date_from=${from}&date_to=${to}`);
    const pairs = (o) => Object.entries(o || {}).map(([k, v]) => [k.replace(/_/g, " ").toLowerCase(), v]);
    const small = (title, rows) => card(title, table([{ label: "", render: (r) => esc(r[0]) }, { label: "", num: true, render: (r) => fmtNum(r[1]) }], rows, { empty: "None." }), { flush: true });
    box.innerHTML = `<div class="row" style="margin-bottom:12px;gap:10px"><label class="f" style="flex-direction:row;align-items:center;gap:6px">From<input class="input" type="date" id="f" value="${from}"></label>
        <label class="f" style="flex-direction:row;align-items:center;gap:6px">To<input class="input" type="date" id="t" value="${to}"></label><button class="btn sm" id="go">Show</button>
        <span class="spacer"></span><button class="btn sm primary" id="pdf">Statistics return (PDF)</button></div>
      <div class="kpis">${kpi("Transactions monitored", fmtNum(s.transactions.total))}${kpi("Alerts", fmtNum(s.alerts.total))}
        ${kpi("Cases opened / closed", `${fmtNum(s.cases.opened)} / ${fmtNum(s.cases.closed)}`, s.cases.avg_days_to_close != null ? `${s.cases.avg_days_to_close} days on average` : "")}
        ${kpi("STR / CTR", `${fmtNum(s.str.total)} / ${fmtNum(s.ctr.total)}`, `${fmtNum(s.ctr.submitted)} CTR submitted`)}</div>
      <div class="grid g3" style="margin-top:16px">${small("Transactions by type", pairs(s.transactions.by_type))}${small("Alerts by severity", pairs(s.alerts.by_severity))}
        ${small("Decisions", pairs(s.decisions))}${small("STR by status", pairs(s.str.by_status))}${small("Screening matches", pairs(s.screening.by_status))}
        ${small("Customers by risk", pairs(s.customers_by_risk))}</div>`;
    box.querySelector("#go").onclick = () => { from = box.querySelector("#f").value; to = box.querySelector("#t").value; draw().catch(errToast); };
    const pdf = box.querySelector("#pdf");
    pdf.onclick = () => busy(pdf, async () => {
      try { const j = await api.post("/reports", { type: "regulator_statistics", format: "pdf", params: { date_from: from, date_to: to } });
        await api.download(`/reports/${j.id || j.job?.id}/download`, `AML-statistics-${from}-${to}.pdf`); } catch (e) { errToast(e); }
    });
  };
  await draw();
}

export async function openStr(id, onChange) {
  const dr = drawer(loading(), onChange);
  const load = async () => {
    const r = await api.get(`/str/${encodeURIComponent(id)}`);
    const editable = r.status === "DRAFT" && (can("str.prepare") || can("str.approve"));
    dr.set(`<div class="dh"><b>${esc(r.id)}</b> ${chip(r.status)}<span class="spacer"></span>
        <a class="btn sm" href="#" data-x="xml">XML</a><a class="btn sm" href="#" data-x="pdf">PDF</a><button class="x" data-close>×</button></div>
      <div class="db">
        ${card("Report", kv([["Case", `<a href="#/cases/${encodeURIComponent(r.case_id)}">${esc(r.case_id)}</a>`], ["Indicators", (r.indicators || []).map((c) => `<span class="code">${esc(c)}</span>`).join(" ")],
          ["Prepared by", esc(r.prepared_by)], ["Approved by", esc(r.approved_by || "–")], ["FFU reference", esc(r.submitted_ref || "–")]]))}
        ${card("Reason for suspicion", `<textarea class="input" id="reason" rows="3" ${editable ? "" : "disabled"}>${esc(r.reason)}</textarea>`)}
        ${card("Narrative", `<textarea class="input" id="narr" rows="12" ${editable ? "" : "disabled"}>${esc(r.narrative)}</textarea>
          ${editable ? '<div class="row" style="margin-top:8px"><button class="btn sm" id="regen">Regenerate draft from the case</button><span class="tiny muted">Uses the AI assistant when it is switched on; otherwise the case facts.</span></div>' : ""}`)}
        ${card("Action taken", `<textarea class="input" id="action" rows="2" ${editable ? "" : "disabled"}>${esc(r.action_taken)}</textarea>`)}
        ${card("Subjects", table([{ label: "Role", key: "role" }, { label: "Name", key: "name" }, { label: "Reference / account", render: (s) => `<span class="mono small">${esc(s.ref || s.account || "")}</span>` }, { label: "Country", key: "country" }], r.subjects), { flush: true })}
        ${card("Transactions", table([{ label: "Date", render: (t) => esc(t.date.slice(0, 16).replace("T", " ")) }, { label: "Type", render: (t) => esc(t.type.replace(/_/g, " ")) },
          { label: "Amount", num: true, render: (t) => `${fmtNum(t.amount, 2)} ${esc(t.currency)}` }, { label: "Counterparty", render: (t) => `<span class="mono small">${esc(t.counterparty || "")}</span>` }, { label: "Decision", render: (t) => chip(t.decision) }], r.transactions), { flush: true })}
        <div class="row">${editable ? '<button class="btn" id="save">Save draft</button>' : ""}${r.status === "DRAFT" && can("str.approve") ? '<button class="btn primary" id="approve">Approve</button>' : ""}
          ${r.status === "APPROVED" && can("str.approve") ? '<input class="input" id="ref" placeholder="FFU submission reference" style="max-width:260px"><button class="btn primary" id="submit">Record submission</button>' : ""}</div>
        <p class="tiny muted">The XML follows the goAML report structure; validate it against the schema version published by the FFU before uploading.</p></div>`);
    const body = () => ({ reason: dr.el.querySelector("#reason").value, narrative: dr.el.querySelector("#narr").value, action_taken: dr.el.querySelector("#action").value });
    const on = (sel, fn) => { const b = dr.el.querySelector(sel); if (b) b.onclick = () => busy(b, async () => { try { await fn(); await load(); } catch (e) { errToast(e); } }); };
    on("#save", async () => { await api.put(`/str/${id}`, body()); toast("Draft saved"); });
    on("#approve", async () => { await api.put(`/str/${id}`, body()); await api.post(`/str/${id}/approve`); toast("Approved"); });
    on("#submit", async () => { await api.post(`/str/${id}/submit`, { reference: dr.el.querySelector("#ref").value }); toast("Submission recorded"); });
    const regen = dr.el.querySelector("#regen");
    if (regen) regen.onclick = () => busy(regen, async () => { try { const a = await api.post(`/cases/${encodeURIComponent(r.case_id)}/ai-summary`, { purpose: "str" });
      dr.el.querySelector("#narr").value = a.text; toast(a.note, "ok", 7000); } catch (e) { errToast(e); } });
    dr.el.querySelectorAll("[data-x]").forEach((a) => (a.onclick = async (e) => { e.preventDefault(); try { await api.download(`/str/${id}/export?fmt=${a.dataset.x}`, `${id}.${a.dataset.x}`); } catch (err) { errToast(err); } }));
  };
  load().catch(errToast);
}

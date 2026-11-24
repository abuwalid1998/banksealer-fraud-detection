// AML alert queue: triage, close with reason, merge into cases.
import { api } from "../api.js";
import { navigate } from "../app.js";
import { S, card, chip, decisionChip, errToast, esc, fmtDate, fmtRel, fmtScore, live, liveBadge, loading, modal,
  options, pager, table, toast } from "../ui.js";
import { createCaseDialog, openTransaction } from "./transactions.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el, { params }) {
  const tabs = [["queue", "Alert queue"], ["groups", "Grouped by customer"]];
  if (can("confirmations.answer") || can("alerts.view_all")) tabs.push(["confirm", "Customer SMS confirmations"]);
  let cur = params.tab || "queue";
  const draw = () => {
    el.innerHTML = `<div class="row" style="margin-bottom:0"><div class="tabs" style="flex:1">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div>
      ${can("fraud.report") ? '<button class="btn danger sm" id="rf" style="margin-bottom:16px">Customer reports fraud</button>' : ""}</div><div id="atab"></div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const rf = el.querySelector("#rf");
    if (rf) rf.onclick = async () => { const { reportFraud } = await import("./customers.js"); reportFraud(); };
    const box = el.querySelector("#atab");
    (cur === "queue" ? queue(box, params) : cur === "groups" ? groups(box) : confirmations(box)).catch(errToast);
  };
  draw();
}

async function groups(box) {
  const d = await api.get("/alerts/groups?hours=168");
  box.innerHTML = card(`${d.groups.length} groups from ${d.alerts} open alerts (last 7 days)`, table([
    { label: "Customer", render: (g) => `<a href="#/customers/${encodeURIComponent(g.customer_ref)}">${esc(g.customer_ref)}</a><div class="tiny muted">${esc(g.customer_name)}</div>` },
    { label: "Main reason", render: (g) => `<span class="code">${esc(g.reason)}</span>${g.kind !== "TRANSACTION" ? " " + chip(g.kind, "warn") : ""}` },
    { label: "Alerts", num: true, render: (g) => `<b>${g.count}</b>` }, { label: "Severity", render: (g) => chip(g.severity) },
    { label: "Max score", num: true, render: (g) => fmtScore(g.max_score) },
    { label: "Decisions", render: (g) => Object.entries(g.decisions).map(([k, v]) => `${decisionChip(k)} ${v}`).join(" ") },
    { label: "Period", render: (g) => `<span class="small">${fmtRel(g.first)} → ${fmtRel(g.last)}</span>` },
    { label: "", render: (g) => `<div class="row nowrap">${can("cases.create") ? `<button class="btn sm primary" data-case="${esc(g.key)}">Case for group</button>` : ""}${can("alerts.triage") ? `<button class="btn sm" data-close="${esc(g.key)}">Close group</button>` : ""}</div>` },
  ], d.groups, { empty: "No open alerts." }), { flush: true, sub: "Alerts of the same customer and main reason are handled together – one decision instead of many." });
  const find = (k) => d.groups.find((g) => g.key === k);
  box.querySelectorAll("[data-case]").forEach((b) => (b.onclick = async () => {
    const g = find(b.dataset.case);
    try { const c = await api.post("/cases", { alert_ids: g.alert_ids, priority: g.severity === "CRITICAL" ? "URGENT" : g.severity === "HIGH" ? "HIGH" : "MEDIUM", auto_assign: true,
      purpose: `${g.count} alerts for ${g.customer_ref}: ${g.reason}` }); toast(`Case ${c.id} created`); navigate(`#/cases/${encodeURIComponent(c.id)}`); } catch (e) { errToast(e); }
  }));
  box.querySelectorAll("[data-close]").forEach((b) => (b.onclick = async () => {
    const g = find(b.dataset.close);
    const reasons = (await api.get("/alerts?page_size=1")).close_reasons;
    const r = await modal({ title: `Close ${g.count} alert(s) – ${g.customer_ref}`, body: `<label class="f">Reason<select class="input" name="reason">${options(Object.entries(reasons))}</select></label>
      <label class="f" style="margin-top:10px">Note<textarea class="input" name="note" required></textarea></label>`,
      actions: [{ label: "Cancel" }, { label: "Close all", cls: "primary", onClick: (m) => api.post("/alerts/bulk-close", { alert_ids: g.alert_ids, reason: m.querySelector("[name=reason]").value, note: m.querySelector("[name=note]").value }) }] });
    if (r) { toast(`${r.closed} alert(s) closed${r.errors.length ? ` – ${r.errors.length} error(s)` : ""}`); groups(box); }
  }));
}

async function confirmations(box) {
  const d = await api.get("/confirmations");
  const c = d.counts;
  box.innerHTML = `${d.simulated ? '<div class="notice warn" style="margin-bottom:12px">Demo Mode: SMS messages are simulated – record the customer\'s answer with the buttons to see the release / reject flow.</div>' : ""}
    ${card(`SMS confirmations · ${c.SENT || 0} waiting · ${c.CONFIRMED || 0} confirmed · ${c.DENIED || 0} denied · ${c.EXPIRED || 0} expired`, table([
      { label: "Sent", render: (x) => fmtRel(x.sent_at) }, { label: "Payment", render: (x) => `<a href="#" data-tx="${esc(x.tx_ref)}" class="mono small">${esc(x.tx_ref)}</a>` },
      { label: "Customer", render: (x) => `<a href="#/customers/${encodeURIComponent(x.customer_ref)}">${esc(x.customer_ref)}</a><div class="tiny muted">${esc(x.phone)}</div>` },
      { label: "Message", render: (x) => `<span class="small">${esc(x.message)}</span>` }, { label: "Status", render: (x) => chip(x.status, x.status === "CONFIRMED" ? "ok" : x.status === "DENIED" ? "crit" : x.status === "SENT" ? "warn" : "neutral") + (x.reply ? `<div class="tiny muted">reply: ${esc(x.reply)}</div>` : "") },
      { label: "", render: (x) => x.status === "SENT" && can("confirmations.answer") ? `<div class="row nowrap"><button class="btn sm teal" data-ans="${x.id}" data-r="1">Customer: YES</button><button class="btn sm danger" data-ans="${x.id}" data-r="2">Customer: NO</button></div>` : "" },
    ], d.confirmations, { empty: "No confirmation requests yet – they are sent when a payment is held." }), { flush: true,
      sub: "Held payments ask the customer by SMS: YES releases the payment, NO rejects it, opens an urgent case and watch-lists the beneficiary." })}`;
  box.querySelectorAll("[data-tx]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openTransaction(a.dataset.tx); }));
  box.querySelectorAll("[data-ans]").forEach((b) => (b.onclick = async () => {
    try { const r = await api.post(`/confirmations/${b.dataset.ans}/answer`, { reply: b.dataset.r });
      toast(r.result === "released" ? "Payment released – alert closed" : `Payment rejected – case ${r.case_id} opened`, "ok", 6000); confirmations(box); } catch (e) { errToast(e); }
  }));
}

async function queue(el, params) {
  const f = { status: params.status || "ACTIVE", severity: "", timing: "", decision: "", search: "", page: 1 };
  const selected = new Set();
  el.innerHTML = `${card("AML alert queue", `<div class="filters" id="flt">
      <input class="input" type="search" name="search" placeholder="Alert, customer or transaction">
      <select class="input" name="status">${options([["ACTIVE", "Open + triaged"], ["OPEN", "Open"], ["TRIAGED", "Triaged"], ["IN_CASE", "In a case"], ["CLOSED", "Closed"]], f.status, { blank: "All statuses" })}</select>
      <select class="input" name="severity">${options(["CRITICAL", "HIGH", "MEDIUM", "LOW"], "", { blank: "All severities" })}</select>
      <select class="input" name="timing">${options([["PRE_EVENT", "Pre-event"], ["POST_EVENT", "Post-event"]], "", { blank: "Any timing" })}</select>
      <select class="input" name="decision">${options(["REVIEW", "HOLD", "DECLINE", "ERROR_POLICY", "PASS"], "", { blank: "All decisions" })}</select>
    </div>`, { actions: can("cases.create") ? '<button class="btn primary" id="mk" disabled>Create case from selected</button>' : "",
      sub: `${liveBadge()} An alert is a signal for review – not a confirmed crime.` })}
    <div class="card" style="margin-top:16px" id="list">${loading()}</div>`;
  const mk = el.querySelector("#mk");
  const load = async () => {
    const q = new URLSearchParams(Object.entries(f).filter(([, v]) => v !== "")).toString();
    const box = el.querySelector("#list");
    try {
      const d = await api.get(`/alerts?${q}&page_size=50`);
      selected.clear(); if (mk) mk.disabled = true;
      box.innerHTML = table([
        { label: "Raised", render: (r) => `<span class="nowrap" title="${esc(fmtDate(r.created_at))}">${fmtRel(r.created_at)}</span>` },
        { label: "Alert", render: (r) => `<span class="mono small">${esc(r.id.replace(/^.*?-ALR-/, "ALR-"))}</span>${r.related_alert_id ? '<div class="tiny muted">related alerts</div>' : ""}` },
        { label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a>` },
        { label: "Severity", render: (r) => chip(r.severity) },
        { label: "Decision", render: (r) => decisionChip(r.decision) },
        { label: "Timing", render: (r) => chip(r.timing) },
        { label: "Score", num: true, render: (r) => fmtScore(r.score) },
        { label: "AML", render: (r) => r.aml_signal_level !== "NONE" ? chip(r.aml_signal_level) : '<span class="muted">–</span>' },
        { label: "Summary", render: (r) => `<span class="small">${esc(r.summary)}</span>` },
        { label: "Status", render: (r) => chip(r.status) + (r.case_id ? ` <a class="small" href="#/cases/${encodeURIComponent(r.case_id)}">case</a>` : "") },
        { label: "", render: (r) => `<div class="row nowrap">${r.tx_ref ? `<button class="btn sm" data-tx="${esc(r.tx_ref)}">Details</button>` : ""}${can("alerts.triage") && ["OPEN", "TRIAGED"].includes(r.status)
          ? `${r.status === "OPEN" ? `<button class="btn sm" data-triage="${esc(r.id)}">Retain</button>` : ""}<button class="btn sm" data-close="${esc(r.id)}">Close</button>` : ""}</div>` },
      ], d.items, { selectable: can("cases.create"), isSelectable: (r) => ["OPEN", "TRIAGED"].includes(r.status), empty: "The queue is clear." }) + pager(d.total, f.page, 50);
      box.querySelectorAll("[data-sel]").forEach((cb) => cb.addEventListener("change", () => {
        const a = d.items[+cb.dataset.sel]; cb.checked ? selected.add(a.id) : selected.delete(a.id);
        cb.closest("tr").classList.toggle("sel", cb.checked); if (mk) mk.disabled = !selected.size;
      }));
      const all = box.querySelector("[data-selall]");
      if (all) all.onchange = () => box.querySelectorAll("[data-sel]:not(:disabled)").forEach((cb) => { cb.checked = all.checked; cb.dispatchEvent(new Event("change")); });
      box.querySelectorAll("[data-tx]").forEach((b) => (b.onclick = () => openTransaction(b.dataset.tx, load)));
      box.querySelectorAll("[data-triage]").forEach((b) => (b.onclick = async () => {
        try { await api.post(`/alerts/${encodeURIComponent(b.dataset.triage)}/triage`, {}); toast("Alert retained for review (triaged)"); load(); } catch (e) { errToast(e); }
      }));
      box.querySelectorAll("[data-close]").forEach((b) => (b.onclick = async () => {
        const r = await modal({ title: "Close alert", body: `<label class="f">Reason<select class="input" name="reason_code">${options(Object.entries(d.close_reasons))}</select></label>
          <label class="f" style="margin-top:10px">Rationale (mandatory)<textarea class="input" name="note" required></textarea></label>`,
          actions: [{ label: "Cancel" }, { label: "Close alert", cls: "primary", onClick: (m) => api.post(`/alerts/${encodeURIComponent(b.dataset.close)}/close`,
            { reason_code: m.querySelector("[name=reason_code]").value, note: m.querySelector("[name=note]").value }) }] });
        if (r) { toast("Alert closed"); load(); }
      }));
      box.querySelectorAll("[data-page]").forEach((b) => (b.onclick = () => { f.page = +b.dataset.page; load(); }));
    } catch (e) { errToast(e); }
  };
  if (mk) mk.onclick = async () => {
    const r = await createCaseDialog({ alert_ids: [...selected] });
    if (r) navigate(`#/cases/${encodeURIComponent(r.id)}`);
  };
  let t;
  el.querySelectorAll("#flt [name]").forEach((inp) => inp.addEventListener(inp.type === "search" ? "input" : "change", () => {
    f[inp.name] = inp.value; f.page = 1; clearTimeout(t); t = setTimeout(load, 250);
  }));
  load();
  live(el, 8000, async () => { if (!selected.size && f.page === 1) await load(); });
}

// AML alert queue: triage, close with reason, merge into cases.
import { api } from "../api.js";
import { navigate } from "../app.js";
import { S, card, chip, decisionChip, errToast, esc, fmtDate, fmtRel, fmtScore, live, liveBadge, loading, modal,
  options, pager, table, toast } from "../ui.js";
import { createCaseDialog, openTransaction } from "./transactions.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el, { params }) {
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

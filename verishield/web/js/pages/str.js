// Regulatory reports: suspicious transaction reports (STR) for the Financial Follow-Up Unit.
import { api } from "../api.js";
import { S, busy, card, chip, drawer, errToast, esc, fmtDate, fmtNum, kv, loading, table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el) {
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
  el.querySelectorAll("tr[data-row]").forEach((tr) => tr.addEventListener("click", () => openStr(d.reports[+tr.dataset.row].id, () => render(el))));
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

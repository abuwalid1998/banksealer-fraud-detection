// Periodic customer (KYC / risk) reviews: scheduled by institution risk rating, assigned, worked and completed.
import { api } from "../api.js";
import { S, busy, card, chip, errToast, esc, fmtDate, fmtNum, fmtRel, kpi, loading, modal, options, table, toast } from "../ui.js";

export async function render(el) {
  let status = "OPEN";
  const draw = async () => {
    el.innerHTML = loading();
    const d = await api.get(`/reviews?status=${status}`);
    const users = d.can_manage ? (await api.get("/users/assignable")).users : [];
    const open = d.reviews.filter((r) => r.status !== "COMPLETED");
    el.innerHTML = `<div class="kpis">${kpi("Open reviews", fmtNum(open.length), "", "accent")}${kpi("Overdue", fmtNum(open.filter((r) => r.overdue).length), "", open.some((r) => r.overdue) ? "crit" : "")}
        ${kpi("Unassigned", fmtNum(open.filter((r) => !r.assignee_id).length))}${kpi("Frequency", `${d.settings.months.HIGH} / ${d.settings.months.MEDIUM} / ${d.settings.months.LOW}`, "months for HIGH / MEDIUM / LOW risk")}</div>
      <div style="margin-top:16px">${card("Customer reviews", table([
        { label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a><div class="tiny muted">${esc(r.customer_name)}</div>` },
        { label: "Reason", render: (r) => chip(r.reason, r.reason === "PERIODIC" ? "neutral" : "warn") }, { label: "Risk", render: (r) => chip(r.risk_at_open) },
        { label: "Due", render: (r) => `${fmtDate(r.due_at, { time: false })} <span class="tiny ${r.overdue ? "" : "muted"}" style="${r.overdue ? "color:var(--red);font-weight:600" : ""}">${r.overdue ? "overdue" : fmtRel(r.due_at)}</span>` },
        { label: "Assignee", render: (r) => d.can_manage && r.status !== "COMPLETED" ? `<select class="input" data-as="${r.id}" style="width:auto;padding:4px 6px;font-size:12px">${options(users.map((u) => [u.id, u.name]), r.assignee_id || "", { blank: "– assign –" })}</select>` : esc(r.assignee_name || "–") },
        { label: "Checklist", render: (r) => `${r.checklist.filter((i) => i.done).length}/${r.checklist.length}` },
        { label: "Status", render: (r) => chip(r.status) + (r.outcome ? `<div class="tiny muted">${esc(r.outcome)} → ${esc(r.new_risk)}</div>` : "") },
        { label: "", render: (r) => r.status !== "COMPLETED" ? `<button class="btn sm primary" data-work="${r.id}">Work on review</button>` : "" },
      ], d.reviews, { empty: "No reviews in this view." }), { flush: true,
        actions: `<select class="input" id="st" style="width:auto">${options([["OPEN", "Open"], ["COMPLETED", "Completed"], ["", "All"]], status)}</select>${d.can_manage ? '<button class="btn sm" id="sched">Schedule due reviews</button>' : ""}`,
        sub: "High-risk customers are reviewed every 6 months, medium every 12, low every 36 – plus reviews triggered by screening matches or the AML Manager." })}</div>`;
    el.querySelector("#st").onchange = (e) => { status = e.target.value; draw(); };
    const sch = el.querySelector("#sched");
    if (sch) sch.onclick = () => busy(sch, async () => { try { const r = await api.post("/reviews/schedule"); toast(`${r.created} review(s) scheduled`); draw(); } catch (e) { errToast(e); } });
    el.querySelectorAll("[data-as]").forEach((s) => (s.onchange = async () => { try { await api.post(`/reviews/${s.dataset.as}/assign`, { assignee_id: s.value }); toast("Assigned"); draw(); } catch (e) { errToast(e); } }));
    el.querySelectorAll("[data-work]").forEach((b) => (b.onclick = () => work(d.reviews.find((r) => r.id === b.dataset.work), draw)));
  };
  await draw();
}

async function work(r, done) {
  const res = await modal({ title: `Review – ${r.customer_ref} ${r.customer_name}`, wide: true, body: `
    <div class="grid g2"><div><h4 style="margin-bottom:8px">Checklist</h4>${r.checklist.map((i, n) => `<label class="check" style="margin-bottom:6px"><input type="checkbox" data-i="${n}" ${i.done ? "checked" : ""}> ${esc(i.item)}</label>`).join("<br>")}
      <p class="small" style="margin-top:10px"><a href="#/customers/${encodeURIComponent(r.customer_ref)}" target="_blank">Open the customer profile and timeline ↗</a></p></div>
    <div class="stack" style="gap:10px"><label class="f">Outcome<select class="input" name="outcome">${options([["NO_CHANGE", "No change – profile confirmed"], ["RISK_CHANGED", "Risk rating changed"], ["EDD", "Enhanced due diligence required"], ["EXIT", "Recommend relationship exit"]], "NO_CHANGE")}</select></label>
      <label class="f">Risk rating after review<select class="input" name="new_risk">${options(["LOW", "MEDIUM", "HIGH"], r.risk_at_open)}</select></label>
      <label class="f">Conclusion<textarea class="input" name="notes" placeholder="Documents checked, activity compared with the declared profile…"></textarea></label></div></div>`,
    actions: [{ label: "Close" }, { label: "Save checklist", onClick: (m) => api.post(`/reviews/${r.id}/checklist`, { checklist: r.checklist.map((i, n) => ({ item: i.item, done: m.querySelector(`[data-i="${n}"]`).checked })) }) },
      { label: "Complete review", cls: "primary", onClick: async (m) => {
        await api.post(`/reviews/${r.id}/checklist`, { checklist: r.checklist.map((i, n) => ({ item: i.item, done: m.querySelector(`[data-i="${n}"]`).checked })) });
        return api.post(`/reviews/${r.id}/complete`, { outcome: m.querySelector("[name=outcome]").value, new_risk: m.querySelector("[name=new_risk]").value, notes: m.querySelector("[name=notes]").value });
      } }] });
  if (res) { toast("Saved"); done(); }
}
export { S };

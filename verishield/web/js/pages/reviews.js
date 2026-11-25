// Periodic customer (KYC / risk) reviews: scheduled by institution risk rating, assigned, worked and completed.
import { api } from "../api.js";
import { S, busy, card, chip, errToast, esc, fmtDate, fmtNum, fmtRel, kpi, label, loading, modal, options, table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el, { params = {} } = {}) {
  const tabs = [];
  if (can("reviews.manage") || can("reviews.work")) tabs.push(["reviews", "Periodic reviews"]);
  if (can("cra.manage") || can("cra.approve") || can("cra.view")) tabs.push(["ratings", "Risk rating changes"], ["model", "Customer risk model"]);
  let cur = params.tab || tabs[0][0];
  const draw = () => {
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    ({ reviews: reviewsTab, ratings: ratingsTab, model: modelTab })[cur](el.querySelector("#tab")).catch(errToast);
  };
  draw();
}

async function reviewsTab(el) {
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


// ------------------------------------------------------------------ customer risk assessment
const TONE = { LOW: "ok", MEDIUM: "warn", HIGH: "crit" };

async function ratingsTab(box, status = "") {
  const d = await api.get(`/risk-assessments${status ? `?status=${status}` : ""}`);
  const dist = d.distribution || {};
  const total = Object.values(dist).reduce((a, b) => a + b, 0) || 1;
  box.innerHTML = `<div class="kpis">${["LOW", "MEDIUM", "HIGH"].map((l) => kpi(`${l.toLowerCase()} risk customers`, fmtNum(dist[l] || 0), `${((100 * (dist[l] || 0)) / total).toFixed(1)}%`, l === "HIGH" ? "crit" : "")).join("")}
      ${kpi("Awaiting approval", fmtNum(d.pending), "downgrades need a second person", d.pending ? "warn" : "")}</div>
    <div style="margin-top:16px">${card("Rating changes", table([
      { label: "When", render: (r) => fmtDate(r.created_at) },
      { label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a><div class="tiny muted">${esc(r.customer_name)}</div>` },
      { label: "Change", render: (r) => `${chip(r.previous_level || "–", TONE[r.previous_level] || "neutral")} → ${chip(r.level, TONE[r.level])}` },
      { label: "Score", num: true, render: (r) => r.score }, { label: "Method", render: (r) => chip(r.method, "neutral") },
      { label: "Reason", render: (r) => `<span class="small">${esc(r.reason)}</span><div class="tiny muted">${esc(r.requested_by)}${r.approved_by ? ` · approved ${esc(r.approved_by)}` : ""}</div>` },
      { label: "Status", render: (r) => chip(r.status) },
      { label: "", render: (r) => r.status === "PENDING_APPROVAL" && d.can_approve ? `<div class="row nowrap"><button class="btn sm primary" data-a="approve" data-id="${r.id}">Approve</button><button class="btn sm" data-a="reject" data-id="${r.id}">Reject</button></div>` : "" },
    ], d.assessments, { empty: "No rating changes." }), { flush: true,
      sub: "Customers are re-assessed daily. Upgrades apply at once (and open a KYC review when a customer becomes HIGH); downgrades wait for approval by a second person.",
      actions: `<select class="input" id="st" style="width:auto">${options([["", "All changes"], ["PENDING_APPROVAL", "Awaiting approval"], ["APPLIED", "Applied"], ["REJECTED", "Rejected"]], status)}</select>${d.can_manage ? '<button class="btn sm" id="runall">Re-assess all customers</button>' : ""}` })}</div>`;
  box.querySelector("#st").onchange = (e) => ratingsTab(box, e.target.value);
  const run = box.querySelector("#runall");
  if (run) run.onclick = () => busy(run, async () => { try { const r = await api.post("/risk-assessments/run-all"); toast(`${r.assessed} assessed · ${r.upgraded} upgraded · ${r.pending_downgrades} downgrade(s) to approve`); ratingsTab(box, status); } catch (e) { errToast(e); } });
  box.querySelectorAll("[data-a]").forEach((b) => (b.onclick = () => busy(b, async () => {
    try { await api.post(`/risk-assessments/${b.dataset.id}/${b.dataset.a}`); toast(b.dataset.a === "approve" ? "Approved" : "Rejected"); ratingsTab(box, status); } catch (e) { errToast(e); }
  })));
}

async function modelTab(box) {
  const d = await api.get("/cra/model");
  const m = d.model;
  const ro = d.can_manage ? "" : "disabled";
  box.innerHTML = `<form id="cm">${card("Customer risk assessment model", `
      <div class="grid g2"><div><h4 style="margin-bottom:8px">Factor weights</h4>${Object.entries(m.weights).map(([k, v]) => `<label class="f" style="flex-direction:row;align-items:center;justify-content:space-between;margin-bottom:6px">${esc(label(k))}<input class="input" type="number" min="0" max="100" step="1" data-w="${k}" value="${v}" style="width:90px" ${ro}></label>`).join("")}</div>
      <div><h4 style="margin-bottom:8px">Rating thresholds (score 0-100)</h4>
        <label class="f">MEDIUM from<input class="input" type="number" name="MEDIUM" value="${m.thresholds.MEDIUM}" ${ro}></label>
        <label class="f">HIGH from<input class="input" type="number" name="HIGH" value="${m.thresholds.HIGH}" ${ro}></label>
        <label class="check" style="margin-top:10px"><input type="checkbox" name="auto_downgrade" ${m.auto_downgrade ? "checked" : ""} ${ro}> Apply automatic downgrades without approval</label>
        <h4 style="margin:14px 0 6px">Minimum level</h4><p class="small">${Object.entries(m.minimum_level).map(([k, v]) => `${esc(label(k))} → <b>${esc(v)}</b>`).join("<br>")}</p>
        <h4 style="margin:14px 0 6px">High-risk activities</h4><p class="tiny muted">${esc(m.occupation_risk.high.join(", "))}</p></div></div>`,
    { sub: `Model ${esc(m.version)}. Changes are audited and apply to the next assessment.`, actions: d.can_manage ? '<button class="btn sm primary">Save</button>' : "" })}</form>`;
  const f = box.querySelector("#cm");
  f.onsubmit = (e) => { e.preventDefault(); busy(f.querySelector("button.primary"), async () => {
    try {
      const weights = {}; f.querySelectorAll("[data-w]").forEach((i) => (weights[i.dataset.w] = Number(i.value)));
      await api.put("/cra/model", { weights, thresholds: { MEDIUM: Number(f.querySelector("[name=MEDIUM]").value), HIGH: Number(f.querySelector("[name=HIGH]").value) },
        auto_downgrade: f.querySelector("[name=auto_downgrade]").checked });
      toast("Risk model saved"); modelTab(box);
    } catch (err) { errToast(err); } }); };
}

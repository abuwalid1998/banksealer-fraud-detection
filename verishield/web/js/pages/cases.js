// AML case list and case file (BRD 7.3, 10).
import { api } from "../api.js";
import { navigate } from "../app.js";
import { S, bindTable, busy, card, chip, confirmDialog, errToast, esc, fmtDate, fmtNum, fmtRel, icon, kv, label, loading,
  modal, options, pager, table, toast, toggle } from "../ui.js";
import { openTransaction } from "./transactions.js";

const can = (p) => S.me.permissions.includes(p);
const FLOW = ["NEW", "TRIAGED", "ASSIGNED", "IN_PROGRESS", "RECOMMENDATION_SUBMITTED", "MANAGER_REVIEW", "APPROVED_DISPOSITION", "CLOSED"];

export async function render(el, { sub, params, setTitle }) {
  if (sub[0]) return caseFile(el, sub[0], setTitle);
  const f = { status: params.status || "", priority: "", assignee_id: "", search: "", open_only: params.open_only ?? "true", page: 1 };
  let analysts = [];
  if (can("cases.view_all")) { try { analysts = (await api.get("/users/assignable")).users; } catch { /* ignore */ } }
  el.innerHTML = `${card("AML cases", `<div class="filters" id="flt">
      <input class="input" type="search" name="search" placeholder="Case id or title">
      <select class="input" name="status">${options(FLOW.concat(["RETURNED_FOR_MORE_INFO"]), f.status, { blank: "All statuses" })}</select>
      <select class="input" name="priority">${options(["URGENT", "HIGH", "MEDIUM", "LOW"], "", { blank: "All priorities" })}</select>
      ${analysts.length ? `<select class="input" name="assignee_id">${options(analysts.map((u) => [u.id, u.name]), "", { blank: "All analysts" })}</select>` : ""}
      <label class="check"><input type="checkbox" name="open_only" ${f.open_only === "true" ? "checked" : ""}> Open only</label>
    </div>`, { sub: can("cases.view_all") ? "" : "Cases assigned to you", actions: can("cases.assign") ? '<button class="btn sm" id="auto">Automation</button>' : "" })}<div class="card" style="margin-top:16px" id="list">${loading()}</div>`;
  const auto = el.querySelector("#auto");
  if (auto) auto.onclick = async () => {
    const c = (await api.get("/case-automation")).config;
    const r = await modal({ title: "Case automation", body: `<div class="stack" style="gap:12px">
      ${toggle("auto_assign", c.auto_assign, "Assign new cases automatically when no analyst is chosen")}
      <label class="f">Assignment strategy<select class="input" name="strategy">${options([["LEAST_LOADED", "Least loaded analyst (fewest open cases)"], ["ROUND_ROBIN", "Round robin"]], c.strategy)}</select></label>
      ${toggle("auto_case_critical", c.auto_case_critical, "Open a case automatically for every CRITICAL alert")}
      <label class="f">Escalate open cases this many hours before the due date<input class="input" type="number" name="escalate_hours_before_due" value="${c.escalate_hours_before_due}"></label>
      ${toggle("escalate_bump_priority", c.escalate_bump_priority, "Raise the priority when escalating (and notify the AML Manager)")}</div>`,
      actions: [{ label: "Cancel" }, { label: "Save", cls: "primary", onClick: (m) => api.put("/case-automation", {
        auto_assign: m.querySelector("[name=auto_assign]").checked, strategy: m.querySelector("[name=strategy]").value,
        auto_case_critical: m.querySelector("[name=auto_case_critical]").checked, escalate_bump_priority: m.querySelector("[name=escalate_bump_priority]").checked,
        escalate_hours_before_due: Number(m.querySelector("[name=escalate_hours_before_due]").value) }) }] });
    if (r) toast("Automation saved");
  };
  const load = async () => {
    const q = new URLSearchParams(Object.entries(f).filter(([, v]) => v !== "" && v !== "false")).toString();
    const box = el.querySelector("#list");
    try {
      const d = await api.get(`/cases?${q}&page_size=50`);
      box.innerHTML = table([
        { label: "Case", render: (r) => `<b class="mono small">${esc(r.id)}</b>` }, { label: "Title", render: (r) => esc(r.title) },
        { label: "Type", render: (r) => chip(r.case_type, "neutral") }, { label: "Priority", render: (r) => chip(r.priority) },
        { label: "Status", render: (r) => chip(r.status) }, { label: "Assignee", render: (r) => esc(r.assignee_name || "–") },
        { label: "Checklist", num: true, render: (r) => `${r.checklist_done}/${r.checklist_total}` },
        { label: "Due", render: (r) => r.due_at ? `<span class="nowrap">${fmtDate(r.due_at, { time: false })}</span> ${r.sla_breached ? chip("OVERDUE", "crit") : ""}` : "–" },
        { label: "Updated", render: (r) => fmtRel(r.updated_at) },
      ], d.items, { onRow: true, empty: "No cases match." }) + pager(d.total, f.page, 50);
      bindTable(box, d.items, (r) => navigate(`#/cases/${encodeURIComponent(r.id)}`));
      box.querySelectorAll("[data-page]").forEach((b) => (b.onclick = () => { f.page = +b.dataset.page; load(); }));
    } catch (e) { errToast(e); }
  };
  el.querySelectorAll("#flt [name]").forEach((inp) => inp.addEventListener(inp.type === "search" ? "input" : "change", () => {
    f[inp.name] = inp.type === "checkbox" ? String(inp.checked) : inp.value; f.page = 1; load();
  }));
  load();
}

async function caseFile(el, id, setTitle) {
  let c;
  const reload = async () => { c = await api.get(`/cases/${encodeURIComponent(id)}`); draw(); };
  const call = async (btn, fn, msg) => busy(btn, async () => { try { await fn(); if (msg) toast(msg); await reload(); } catch (e) { errToast(e); } });

  function draw() {
    setTitle(c.title, `AML Cases › ${c.id}`);
    const perm = c.permissions;
    const curIdx = FLOW.indexOf(c.status === "RETURNED_FOR_MORE_INFO" ? "IN_PROGRESS" : c.status);
    const stepper = `<div class="stepper">${FLOW.map((s, i) => `<div class="st ${i < curIdx ? "done" : i === curIdx ? "cur" : ""}">${esc(label(s))}</div>`).join("")}</div>`;
    const acts = [];
    if (perm.assign) acts.push(`<button class="btn" data-a="assign">${c.assignee_id ? "Reassign" : "Assign"}</button>`);
    if (S.me.user.role === "AML_EMPLOYEE" && ["ASSIGNED", "RETURNED_FOR_MORE_INFO"].includes(c.status)) acts.push('<button class="btn teal" data-a="start">Start investigation</button>');
    if (perm.review) acts.push('<button class="btn" data-a="review">Begin Manager review</button>');
    if (perm.dispose) acts.push('<button class="btn primary" data-a="dispose">Record disposition</button>');
    if (perm.close) acts.push('<button class="btn" data-a="close">Close case</button>');
    if (perm.reopen) acts.push('<button class="btn" data-a="reopen">Reopen</button>');
    acts.push('<button class="btn" data-a="pdf">Investigation report</button>');
    acts.push(`<button class="btn" data-a="ai">${icon("sparkle")} Summary</button>`);
    if (can("str.prepare") || can("str.approve")) acts.push('<button class="btn" data-a="str">Prepare STR</button>');
    if (c.links.some((l) => l.type === "CUSTOMER")) acts.push(`<button class="btn" data-a="net">${icon("network")} Network</button>`);
    const links = c.links.filter((l) => l.type !== "ALERT");
    const rec = c.recommendation || {}, disp = c.disposition || {};
    el.innerHTML = `<div class="row" style="margin-bottom:14px"><a href="#/cases" class="btn sm ghost">← Cases</a>
        ${chip(c.status)} ${chip(c.priority)} ${chip(c.case_type, "neutral")} ${c.classification === "CONFIRMED_FRAUD" ? chip("CONFIRMED FRAUD", "crit") : ""}
        ${c.escalation ? chip(c.escalation, "warn") : ""}<span class="spacer"></span>${acts.join("")}</div>
      <section class="card"><div class="bd">${stepper}</div></section>
      ${c.status === "RETURNED_FOR_MORE_INFO" ? `<div class="notice warn" style="margin-top:14px">Returned by the Manager for more work – see the chronology for the Manager's note.</div>` : ""}
      <div class="grid g-21" style="margin-top:16px">
        <div class="stack">
          ${card("Case", kv([["Case ID", `<span class="mono">${esc(c.id)}</span>`], ["Owner (Manager)", esc(c.owner_name)], ["Assigned investigator", esc(c.assignee_name || "Not assigned")],
            ["Due", c.due_at ? `${fmtDate(c.due_at)} <span class="small ${c.sla_breached ? "" : "muted"}" style="${c.sla_breached ? "color:var(--red);font-weight:600" : ""}">(${c.sla_breached ? "SLA breached" : fmtRel(c.due_at)})</span>` : ""],
            ["Purpose", esc(c.purpose)], ["Questions", c.questions.length ? `<ol style="margin:0;padding-left:18px">${c.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ol>` : ""],
            ["Review window", c.window_from ? `${fmtDate(c.window_from, { time: false })} – ${fmtDate(c.window_to, { time: false })}` : ""],
            ["Linked entities", links.map((l) => l.type === "CUSTOMER" ? `<a class="btn sm" href="#/customers/${encodeURIComponent(l.ref)}">Customer ${esc(l.ref)}</a>`
              : `<button class="btn sm" data-tx="${esc(l.ref)}">Transaction ${esc(l.ref)}</button>`).join(" ")],
            ["Alerts", c.alerts.map((a) => `${chip(a.severity)} <span class="small">${esc(a.summary)}</span>`).join("<br>")]]))}
          ${card("Investigation checklist", `<div class="stack" style="gap:8px" id="chk">${c.checklist.map((i, n) => `<label class="check"><input type="checkbox" data-i="${n}" ${i.done ? "checked" : ""} ${perm.work ? "" : "disabled"}> ${esc(i.item)}</label>`).join("")}</div>`,
            { sub: `${c.checklist_done}/${c.checklist_total} complete` })}
          ${card("Notes & findings", `${c.notes.length ? `<ul class="timeline">${c.notes.map((n) => `<li><div><b>${esc(n.author_name)}</b> ${chip(n.kind, "neutral")} ${n.restricted ? chip("RESTRICTED", "warn") : ""}</div><div style="white-space:pre-wrap">${esc(n.body)}</div><div class="t">${fmtDate(n.created_at)}</div></li>`).join("")}</ul>` : '<p class="muted">No notes yet.</p>'}
            ${perm.work ? `<form id="note" class="stack" style="gap:8px;margin-top:10px"><textarea class="input" name="body" placeholder="Investigation step, observation, source of funds, related party…" required></textarea>
              <div class="row"><select class="input" name="kind" style="width:auto">${options([["NOTE", "Note"], ["FINDING", "Finding"], ["SOURCE_OF_FUNDS", "Source of funds / purpose"], ["RELATED_PARTY", "Related party"], ["INFO_REQUEST", "Information request"]], "NOTE")}</select>
              ${can("cases.view_all") ? '<label class="check"><input type="checkbox" name="restricted"> Restricted note</label>' : ""}<span class="spacer"></span><button class="btn primary">Add note</button></div></form>` : ""}`)}
          ${card("Evidence", `${c.evidence.length ? table([{ label: "File / reference", render: (e) => e.size ? `<a href="#" data-ev="${esc(e.id)}">${esc(e.filename)}</a>` : esc(e.external_reference || e.filename) },
              { label: "Description", key: "description" }, { label: "SHA-256", render: (e) => `<span class="mono tiny">${esc(e.sha256.slice(0, 16))}…</span>` },
              { label: "Size", num: true, render: (e) => e.size ? `${fmtNum(e.size / 1024, 1)} KB` : "ref" }, { label: "Added", render: (e) => `${esc(e.uploaded_by_name)}<div class="tiny muted">${fmtDate(e.created_at)}</div>` }], c.evidence)
              : '<p class="muted">No evidence yet.</p>'}
            ${perm.work ? `<form id="ev" class="form-grid" style="margin-top:12px;align-items:end"><label class="f">File (PDF, image, CSV, XLSX, DOCX, TXT, EML – max 10 MB)<input class="input" type="file" name="file"></label>
              <label class="f">or external reference (DMS link / document id)<input class="input" name="external_reference"></label>
              <label class="f">Description<input class="input" name="description"></label><button class="btn">Add evidence</button></form>` : ""}`)}
        </div>
        <div class="stack">
          ${card("Analyst recommendation", rec.value ? `${kv([["Recommendation", `<b>${esc(rec.label)}</b>`], ["Rationale", `<span style="white-space:pre-wrap">${esc(rec.rationale)}</span>`],
              ["Findings", esc(rec.findings)], ["Submitted", `${esc(rec.submitted_by)} · ${fmtDate(rec.submitted_at)}`]])}` : perm.recommend ? `<form id="rec" class="stack" style="gap:10px">
              <label class="f">Recommendation<select class="input" name="value">${options(Object.entries(c.recommendation_options))}</select></label>
              <label class="f">Rationale<textarea class="input" name="rationale" required></textarea></label>
              <label class="f">Findings summary<textarea class="input" name="findings"></textarea></label>
              <button class="btn primary">Submit to Manager</button><p class="tiny muted">The AML Manager records the final internal decision.</p></form>` : '<p class="muted">Awaiting the assigned investigator.</p>')}
          ${card("Manager disposition", disp.value ? `${kv([["Disposition", `<b>${esc(disp.label)}</b>${disp.superseded ? " " + chip("SUPERSEDED", "neutral") : ""}`], ["Rationale", `<span style="white-space:pre-wrap">${esc(disp.rationale)}</span>`],
              ["Decided", `${esc(disp.decided_by)} · ${fmtDate(disp.decided_at)}`],
              ["Payment execution", (disp.execution || []).map((x) => `${chip(x.state)} <span class="small">${esc(x.tx_ref)} – ${esc(x.message)}</span>`).join("<br>")]])}`
              : `<p class="muted small">${perm.dispose ? "Use “Record disposition” once the evidence and recommendation have been reviewed." : "Pending Manager decision."}</p>`)}
          ${card("Chronology", `<ul class="timeline">${c.events.slice().reverse().map((e) => `<li><div><b>${esc(label(e.action))}</b>${e.to_status ? ` → ${chip(e.to_status)}` : ""}</div>${e.detail ? `<div class="small">${esc(e.detail)}</div>` : ""}<div class="t">${esc(e.actor_name)} · ${fmtDate(e.created_at, { seconds: true })}</div></li>`).join("")}</ul>`)}
          <div class="notice tiny">A suspicious alert is not a confirmed crime. VeriShield does not file STR/SAR reports – escalation goes to the institution's authorised compliance process.</div>
        </div>
      </div>`;
    bind();
  }

  function bind() {
    const a = (n) => el.querySelector(`[data-a=${n}]`);
    el.querySelectorAll("[data-tx]").forEach((b) => (b.onclick = () => openTransaction(b.dataset.tx)));
    el.querySelectorAll("[data-ev]").forEach((l) => (l.onclick = (e) => { e.preventDefault();
      api.download(`/cases/${encodeURIComponent(c.id)}/evidence/${encodeURIComponent(l.dataset.ev)}`).catch(errToast); }));
    if (a("pdf")) a("pdf").onclick = (e) => busy(e.currentTarget, async () => {
      try { const j = await api.post("/reports", { type: "investigation", format: "pdf", params: { case_id: c.id } });
        await api.download(`/reports/${encodeURIComponent(j.id)}/download`); } catch (err) { errToast(err); }
    });
    if (a("ai")) a("ai").onclick = (e) => busy(e.currentTarget, async () => {
      try {
        const r = await api.post(`/cases/${encodeURIComponent(c.id)}/ai-summary`, {});
        await modal({ title: `Case summary – ${c.id}`, wide: true, body: `<div class="notice ${r.source === "ai" ? "" : "warn"}" style="margin-bottom:12px">${esc(r.note)}${r.model ? ` · ${esc(r.model)}` : ""}</div>
          <textarea class="input" id="sumtxt" rows="16">${esc(r.text)}</textarea>`,
          actions: [{ label: "Close" }, ...(c.permissions.work || c.permissions.dispose ? [{ label: "Save as case note", cls: "primary",
            onClick: (m) => api.post(`/cases/${c.id}/notes`, { body: m.querySelector("#sumtxt").value, kind: "FINDING" }) }] : [])] });
        reload();
      } catch (err) { errToast(err); }
    });
    if (a("str")) a("str").onclick = (e) => busy(e.currentTarget, async () => {
      try { const r = await api.post(`/cases/${encodeURIComponent(c.id)}/str`); const { openStr } = await import("./str.js"); openStr(r.id, reload); } catch (err) { errToast(err); }
    });
    if (a("net")) a("net").onclick = async () => {
      const ref = c.links.find((l) => l.type === "CUSTOMER").ref;
      const { showNetwork } = await import("./customers.js");
      showNetwork(ref);
    };
    if (a("start")) a("start").onclick = (e) => call(e.currentTarget, () => api.post(`/cases/${c.id}/start`), "Investigation started");
    if (a("review")) a("review").onclick = (e) => call(e.currentTarget, () => api.post(`/cases/${c.id}/review`), "Manager review started");
    if (a("close")) a("close").onclick = (e) => call(e.currentTarget, () => api.post(`/cases/${c.id}/close`), "Case closed");
    if (a("assign")) a("assign").onclick = async () => {
      const users = (await api.get("/users/assignable")).users;
      const r = await modal({ title: "Assign investigation", body: `<label class="f">AML Employee<select class="input" name="assignee_id">${options(users.map((u) => [u.id, `${u.name} (${u.username})`]), c.assignee_id)}</select></label>
        <div class="form-grid" style="margin-top:10px"><label class="f">Priority<select class="input" name="priority">${options(["LOW", "MEDIUM", "HIGH", "URGENT"], c.priority)}</select></label>
        <label class="f">Due date<input class="input" type="date" name="due_at" value="${c.due_at ? c.due_at.slice(0, 10) : ""}"></label></div>
        <label class="f" style="margin-top:10px">Note to the analyst<textarea class="input" name="note"></textarea></label>`,
        actions: [{ label: "Cancel" }, { label: "Assign", cls: "primary", onClick: async (m) => {
          const v = (n) => m.querySelector(`[name=${n}]`).value;
          await api.patch(`/cases/${c.id}`, { priority: v("priority"), due_at: v("due_at") || null });
          return api.post(`/cases/${c.id}/assign`, { assignee_id: v("assignee_id"), note: v("note") });
        } }] });
      if (r) { toast("Case assigned"); reload(); }
    };
    if (a("dispose")) a("dispose").onclick = async () => {
      const txs = c.links.filter((l) => l.type === "TRANSACTION").length;
      const r = await modal({ title: "Manager disposition", body: `${c.recommendation.value ? `<div class="notice" style="margin-bottom:12px">Analyst recommends: <b>${esc(c.recommendation.label)}</b></div>` : ""}
        <label class="f">Disposition<select class="input" name="value">${options(Object.entries(c.disposition_options))}</select></label>
        <label class="f" style="margin-top:10px">Rationale (mandatory)<textarea class="input" name="rationale" required></textarea></label>
        <label class="check" style="margin-top:10px"><input type="checkbox" name="close" checked> Close the case after recording the disposition</label>
        <p class="tiny muted" style="margin-top:10px">Release/Reject applies to ${txs} linked transaction(s) still pending or held, through the bank's authorised integration${S.me.tenant.demo_mode ? " (simulated in Demo Mode)" : ""}. “Return to analyst” sends the case back for more work.</p>`,
        actions: [{ label: "Cancel" }, { label: "Record disposition", cls: "primary", onClick: (m) => api.post(`/cases/${c.id}/disposition`,
          { value: m.querySelector("[name=value]").value, rationale: m.querySelector("[name=rationale]").value, close: m.querySelector("[name=close]").checked }) }] });
      if (r) { toast("Disposition recorded"); reload(); }
    };
    if (a("reopen")) a("reopen").onclick = async () => {
      const r = await modal({ title: "Reopen case", body: '<label class="f">Reason<textarea class="input" name="reason" required></textarea></label>',
        actions: [{ label: "Cancel" }, { label: "Reopen", cls: "primary", onClick: (m) => api.post(`/cases/${c.id}/reopen`, { reason: m.querySelector("[name=reason]").value }) }] });
      if (r) { toast("Case reopened"); reload(); }
    };
    const note = el.querySelector("#note");
    if (note) note.onsubmit = (e) => { e.preventDefault();
      call(note.querySelector("button"), () => api.post(`/cases/${c.id}/notes`, { body: note.body.value, kind: note.kind.value, restricted: note.restricted ? note.restricted.checked : false }), "Note added"); };
    const ev = el.querySelector("#ev");
    if (ev) ev.onsubmit = (e) => { e.preventDefault();
      const fd = new FormData();
      if (ev.file.files[0]) fd.append("file", ev.file.files[0]);
      fd.append("description", ev.description.value); fd.append("external_reference", ev.external_reference.value);
      call(ev.querySelector("button"), () => api.upload(`/cases/${c.id}/evidence`, fd), "Evidence added"); };
    const rec = el.querySelector("#rec");
    if (rec) rec.onsubmit = async (e) => { e.preventDefault();
      if (!(await confirmDialog("Submit recommendation", "The case will move to the AML Manager for the final disposition. Continue?", "Submit"))) return;
      call(rec.querySelector("button"), () => api.post(`/cases/${c.id}/recommendation`, { value: rec.value.value, rationale: rec.rationale.value, findings: rec.findings.value }), "Recommendation submitted"); };
    el.querySelectorAll("#chk input").forEach((cb) => (cb.onchange = async () => {
      const list = c.checklist.map((i, n) => ({ item: i.item, done: n === +cb.dataset.i ? cb.checked : i.done }));
      try { await api.put(`/cases/${c.id}/checklist`, { checklist: list }); await reload(); } catch (e) { errToast(e); }
    }));
  }
  el.innerHTML = loading();
  await reload();
}

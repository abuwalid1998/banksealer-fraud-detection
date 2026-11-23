// Operations & audit: platform health, incidents, tamper-evident audit trail.
import { api } from "../api.js";
import { S, busy, card, chip, errToast, esc, fmtDate, fmtNum, kpi, kv, label, loading, modal, options, pager,
  table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el) {
  const tabs = [];
  if (can("system.view")) tabs.push(["health", "Platform health"], ["incidents", "Incidents"]);
  if (can("audit.view")) tabs.push(["audit", "Audit trail"]);
  let cur = tabs[0][0];
  const draw = () => {
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const box = el.querySelector("#tab");
    ({ health, incidents, audit })[cur](box).catch(errToast);
  };
  draw();
}

async function health(box) {
  const h = await api.get("/system/health");
  const lat = h.latency;
  box.innerHTML = `<div class="kpis">
      ${kpi("Database", h.database.ok ? chip("OK", "ok") : chip("DOWN", "crit"), esc(h.database.engine), "accent")}
      ${kpi("Decision service (live)", h.decision_service.live ? chip("READY", "ok") : chip("NO MODEL", "warn"))}
      ${kpi("Decision service (demo)", h.decision_service.demo ? chip("READY", "ok") : chip("NO MODEL", "warn"))}
      ${kpi("Open incidents", fmtNum(h.open_incidents), "", h.open_incidents ? "warn" : "")}
      ${kpi("Dead letters pending", fmtNum(h.dead_letters_pending))}
      ${kpi("Uptime", `${fmtNum(h.metrics.uptime_seconds / 3600, 1)} h`, `v${esc(h.version)} · ${esc(h.environment)}`)}
    </div>
    <div class="grid g2" style="margin-top:16px">
      ${card("Latency (since process start)", table([{ label: "Route", key: "k" }, { label: "Count", num: true, render: (r) => fmtNum(r.count) },
        { label: "p50 ms", num: true, render: (r) => r.p50 ?? "–" }, { label: "p95 ms", num: true, render: (r) => r.p95 ?? "–" }, { label: "p99 ms", num: true, render: (r) => r.p99 ?? "–" }],
        Object.entries(h.metrics.latency).map(([k, v]) => ({ k, ...v }))), { flush: true })}
      ${card("Counters", `<div class="tbl-wrap"><table class="tbl"><tbody>${Object.entries(h.metrics.counters).sort().map(([k, v]) => `<tr><td class="mono small">${esc(k)}</td><td class="num">${fmtNum(v)}</td></tr>`).join("")}</tbody></table></div>`, { flush: true })}
    </div>
    <div class="grid g2" style="margin-top:16px">
      ${["live", "demo"].map((s) => card(`Models – ${s} workspace`, kv(Object.entries(h.models[s]).map(([k, v]) => [label(k), esc(v === null ? "–" : v)])))).join("")}
    </div>
    <div style="margin-top:16px">${card("Recent errors", table([{ label: "Time", render: (e) => fmtDate(new Date(e.at * 1000).toISOString(), { seconds: true }) }, { label: "Category", key: "category" }, { label: "Message", render: (e) => `<span class="small">${esc(e.message)}</span>` }],
      h.metrics.recent_errors.slice().reverse(), { empty: "No errors recorded." }), { flush: true, sub: "Prometheus metrics at /metrics · readiness at /ready" })}</div>`;
}

const FLOW = ["DETECTED", "ACKNOWLEDGED", "INVESTIGATING", "MITIGATED", "RESOLVED", "POSTMORTEM"];
async function incidents(box) {
  const d = await api.get("/incidents");
  box.innerHTML = card("Incidents", table([
    { label: "Incident", render: (i) => `<span class="mono small">${esc(i.id)}</span>` }, { label: "Severity", render: (i) => chip(i.severity, i.severity === "SEV1" ? "crit" : "warn") },
    { label: "Title", key: "title" }, { label: "Category", render: (i) => `<span class="code">${esc(i.category)}</span>` }, { label: "Status", render: (i) => chip(i.status) },
    { label: "Occurrences", num: true, key: "occurrences" }, { label: "Started", render: (i) => fmtDate(i.started_at) }, { label: "Owner", key: "owner" },
    { label: "", render: (i) => i.status !== "POSTMORTEM" ? `<button class="btn sm" data-inc="${esc(i.id)}">Update</button>` : "" },
  ], d.incidents, { empty: "No incidents recorded." }), { flush: true, sub: "Lifecycle: DETECTED → ACKNOWLEDGED → INVESTIGATING → MITIGATED → RESOLVED → POSTMORTEM" });
  box.querySelectorAll("[data-inc]").forEach((b) => (b.onclick = async () => {
    const inc = d.incidents.find((x) => x.id === b.dataset.inc);
    const next = FLOW.slice(FLOW.indexOf(inc.status));
    const r = await modal({ title: `Update ${inc.id}`, body: `<p class="small">${esc(inc.title)}</p>
      <div class="form-grid"><label class="f">Status<select class="input" name="status">${options(next, inc.status)}</select></label><label class="f">Owner<input class="input" name="owner" value="${esc(inc.owner || S.me.user.name)}"></label></div>
      <label class="f" style="margin-top:10px">Notes (impact, mitigation, root cause)<textarea class="input" name="notes"></textarea></label>
      ${inc.timeline.length ? `<ul class="timeline" style="margin-top:12px">${inc.timeline.map((t) => `<li>${chip(t.status)} ${esc(t.note || "")}<div class="t">${esc(t.by || "")} ${esc(t.at)}</div></li>`).join("")}</ul>` : ""}`,
      actions: [{ label: "Cancel" }, { label: "Save", cls: "primary", onClick: (m) => api.patch(`/incidents/${inc.id}`, { status: m.querySelector("[name=status]").value,
        owner: m.querySelector("[name=owner]").value, notes: m.querySelector("[name=notes]").value }) }] });
    if (r) { toast("Incident updated"); incidents(box); }
  }));
}

async function audit(box) {
  const f = { action: "", actor: "", resource: "", outcome: "", offset: 0 };
  const load = async () => {
    const q = new URLSearchParams(Object.entries(f).filter(([, v]) => v !== "")).toString();
    const d = await api.get(`/audit?${q}&limit=100`);
    box.querySelector("#al").innerHTML = table([
      { label: "#", num: true, key: "id" }, { label: "Time", render: (e) => `<span class="nowrap">${fmtDate(e.created_at, { seconds: true })}</span>` },
      { label: "Actor", render: (e) => `${esc(e.actor_name)}<div class="tiny muted">${esc(label(e.actor_role))}</div>` },
      { label: "Action", render: (e) => `<span class="code">${esc(e.action)}</span>` }, { label: "Resource", render: (e) => `<span class="small">${esc(e.resource_type)} ${esc(e.resource_id)}</span>` },
      { label: "Outcome", render: (e) => chip(e.outcome, e.outcome === "SUCCESS" ? "ok" : "crit") }, { label: "Space", render: (e) => chip(e.space, "neutral") },
      { label: "Detail", render: (e) => `<span class="mono tiny">${esc((e.reason ? e.reason + " " : "") + JSON.stringify(e.after || "").slice(0, 120))}</span>` },
    ], d.events) + pager(d.total, f.offset / 100 + 1, 100);
    box.querySelectorAll("[data-page]").forEach((b) => (b.onclick = () => { f.offset = (+b.dataset.page - 1) * 100; load().catch(errToast); }));
  };
  box.innerHTML = card("Audit trail", `<div class="filters" style="margin-bottom:12px">
      <input class="input" name="action" placeholder="Action (e.g. case.dispose)"><input class="input" name="actor" placeholder="Actor"><input class="input" name="resource" placeholder="Resource id/type">
      <select class="input" name="outcome">${options(["SUCCESS", "FAILURE"], "", { blank: "Any outcome" })}</select>
      <span class="spacer"></span><button class="btn" id="verify">Verify integrity</button><span id="vres" class="small"></span></div><div id="al">${loading()}</div>`,
    { sub: "Hash-chained: altering or deleting a past event breaks the chain", flush: false });
  box.querySelectorAll(".filters [name]").forEach((i) => i.addEventListener("change", () => { f[i.name] = i.value; f.offset = 0; load().catch(errToast); }));
  box.querySelector("#verify").onclick = (e) => busy(e.currentTarget, async () => {
    try { const r = await api.get("/audit/verify");
      box.querySelector("#vres").innerHTML = r.valid ? `${chip("CHAIN VALID", "ok")} ${fmtNum(r.checked)} events checked` : `${chip("TAMPERING DETECTED", "crit")} at event #${r.broken_at}`; } catch (err) { errToast(err); }
  });
  await load();
}

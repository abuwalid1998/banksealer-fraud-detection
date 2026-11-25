// Operations & audit: platform health, incidents, tamper-evident audit trail.
import { api } from "../api.js";
import { S, busy, card, chip, errToast, esc, fmtDate, fmtNum, kpi, kv, label, loading, modal, options, pager,
  table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el) {
  const tabs = [];
  if (can("system.view")) tabs.push(["health", "Platform health"], ["cluster", "Cluster"], ["incidents", "Incidents"]);
  if (can("audit.view")) tabs.push(["audit", "Audit trail"]);
  if (can("tenant.manage")) tabs.push(["archive", "Archiving"]);
  let cur = tabs[0][0];
  const draw = () => {
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const box = el.querySelector("#tab");
    ({ health, cluster, incidents, audit, archive })[cur](box).catch(errToast);
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

async function archive(box) {
  const d = await api.get("/archive");
  const p = d.preview;
  box.innerHTML = `<div class="grid g2">${card("Archive old records", `${kv([["Retention (transactions)", `${fmtNum(p.retention_days)} days`], ["Cut-off", fmtDate(p.cutoff)],
      ["Transactions to archive", fmtNum(p.transactions)], ["Decisions", fmtNum(p.decisions)], ["Closed alerts", fmtNum(p.alerts)], ["Kept for open cases", fmtNum(p.protected_by_open_cases)]])}
      <p class="small" style="margin-top:10px">Records older than the retention period (Users & Settings › Institution) are written to a compressed archive file in the data folder and removed from the live database. Records linked to open cases are kept. The run is audited.</p>
      <label class="f" style="margin-top:10px">Type ARCHIVE to confirm<input class="input mono" id="cf"></label><div class="row" style="margin-top:10px"><button class="btn primary" id="run" ${p.transactions || p.alerts ? "" : "disabled"}>Archive now</button></div>`)}
    ${card("Archive files", table([{ label: "File", render: (f) => `<span class="mono small">${esc(f.file)}</span>` }, { label: "Workspace", render: (f) => chip(f.space, "neutral") },
      { label: "Size", num: true, render: (f) => `${fmtNum(f.bytes / 1024, 0)} KB` }, { label: "Created", render: (f) => fmtDate(f.created) }], d.files, { empty: "No archives yet." }), { flush: true, sub: "Keep these files in the bank's backup / records system" })}</div>`;
  const run = box.querySelector("#run");
  run.onclick = () => busy(run, async () => { try { const r = await api.post("/archive/run", { confirm: box.querySelector("#cf").value }); toast(`${fmtNum(r.transactions)} transactions archived`); archive(box); } catch (e) { errToast(e); } });
}

async function cluster(box) {
  const c = await api.get("/system/cluster");
  box.innerHTML = `${(c.warnings || []).map((w) => `<div class="notice warn" style="margin-bottom:12px">${esc(w)}</div>`).join("")}
    <div class="kpis">${kpi("Mode", c.cluster_mode ? "Cluster" : "Single server", c.cluster_mode ? "several servers, one database" : "set VERISHIELD_CLUSTER=1 to add servers", "accent")}
      ${kpi("Servers alive", fmtNum(c.alive), `${c.nodes.length} registered`)}${kpi("Leader", `<span class="mono small">${esc(c.leader || "none")}</span>`, "runs channels and background jobs")}
      ${kpi("Database", esc(c.database), c.database === "postgresql" ? "shared by all nodes" : "single server only")}</div>
    <div style="margin-top:16px">${card("Application servers", table([
      { label: "Node", render: (n) => `<b class="mono small">${esc(n.node_id)}</b>${n.this_node ? ` ${chip("THIS SERVER", "info")}` : ""}` },
      { label: "Role", render: (n) => chip(n.role, n.role === "LEADER" ? "ok" : "neutral") },
      { label: "Heartbeat", render: (n) => (n.alive ? chip("ALIVE", "ok") : chip("SILENT", "crit")) + `<div class="tiny muted">${n.seconds_ago}s ago</div>` },
      { label: "Version", key: "version" }, { label: "Channels running", num: true, render: (n) => fmtNum(n.connectors_running) },
      { label: "Started", render: (n) => fmtDate(n.started_at) },
    ], c.nodes, { empty: "No node registered yet." }), { flush: true,
      sub: "Every server scores decisions behind the load balancer. One leader (database lease, renewed every 10 s, taken over after 30 s) runs the channels and background jobs." })}</div>
    <div class="grid g2" style="margin-top:16px">
      ${card("Shared model checkpoints", table([{ label: "Model", render: (x) => `<span class="mono small">${esc(x.model_id)}</span>` }, { label: "#", num: true, key: "seq" },
        { label: "Size", num: true, render: (x) => `${x.size_mb} MB` }, { label: "Saved by", render: (x) => `<span class="mono tiny">${esc(x.node)}</span>` }, { label: "When", render: (x) => fmtDate(x.saved_at) }],
        c.checkpoints, { empty: "Checkpoints are written every 5 minutes in cluster mode." }), { flush: true, sub: "Signed (HMAC) - a tampered checkpoint is never loaded" })}
      ${card("How to add a server", `<ol class="small" style="margin:0;padding-left:18px;line-height:1.7">
        <li>Use PostgreSQL for <span class="code">VERISHIELD_DATABASE_URL</span> on every server.</li>
        <li>Give all servers the same <span class="code">VERISHIELD_SECRET_KEY</span> and vault key ring file.</li>
        <li>Set <span class="code">VERISHIELD_CLUSTER=1</span> and a unique <span class="code">VERISHIELD_NODE_ID</span>.</li>
        <li>Put the servers behind the load balancer; route by <span class="code">X-Customer-Id</span> when possible (same customer → same server).</li>
        <li>Health check: <span class="code">GET /ready</span> (returns the role).</li></ol>`)}
    </div>`;
}

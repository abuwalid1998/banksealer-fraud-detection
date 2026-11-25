// Integrations & channels: connector registry, test, mapping preview, secrets, lifecycle, dead letters.
import { api } from "../api.js";
import { S, busy, card, chip, confirmDialog, drawer, empty, errToast, esc, fmtDate, fmtNum, fmtRel, icon, kpi, kv, label, live,
  liveBadge, loading, modal, options, table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);
let TYPES = {};

export async function render(el) {
  TYPES = (await api.get("/connectors/types")).types;
  let tab = "connectors";
  const draw = () => {
    const dbTab = can("tenant.manage") || can("connectors.manage");
    el.innerHTML = `<div class="tabs"><button data-t="connectors" class="${tab === "connectors" ? "on" : ""}">Channels &amp; connectors</button>${dbTab ? `<button data-t="db" class="${tab === "db" ? "on" : ""}">Fraud engine database</button>` : ""}<button data-t="quality" class="${tab === "quality" ? "on" : ""}">Data quality</button><button data-t="dlq" class="${tab === "dlq" ? "on" : ""}">Dead-letter queue</button><button data-t="push" class="${tab === "push" ? "on" : ""}">Push API</button>${can("siem.manage") ? `<button data-t="siem" class="${tab === "siem" ? "on" : ""}">Monitoring &amp; SIEM</button>` : ""}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { tab = b.dataset.t; draw(); }));
    const box = el.querySelector("#tab");
    (tab === "connectors" ? connectors(box) : tab === "db" ? database(box) : tab === "quality" ? quality(box) : tab === "dlq" ? deadLetters(box) : tab === "siem" ? monitoring(box) : push(box)).catch(errToast);
    if (tab === "connectors") live(box, 5000, () => connectors(box));
  };
  draw();
}

const TYPE_ICON = { demo_stream: "transactions", tcp_json: "integrations", http_listener: "integrations", file_drop: "reports",
  db_results: "database", db_replica: "database", rest_pull: "refresh", rest_webhook: "integrations", outbound_webhook: "play" };
const RUNNABLE = new Set(["demo_stream", "tcp_json", "http_listener", "file_drop", "db_replica", "rest_pull", "db_results"]);

function channelCard(c, manage) {
  const runnable = RUNNABLE.has(c.type) && c.runtime_supported;
  const on = c.status === "ENABLED" || c.status === "DEGRADED";
  const cfg = c.config || {};
  const where = c.type === "tcp_json" || c.type === "http_listener" ? `${cfg.host}:${cfg.port}${cfg.path || ""}` : c.type === "file_drop" ? cfg.path
    : c.type === "db_results" ? `${cfg.url} › ${cfg.table || "fraud_results"}` : c.type === "demo_stream" ? `catch-up ${cfg.speed || 60}× then real time · fraud ratio ${cfg.fraud_ratio ?? 0.03}` : (cfg.url || "");
  const isOut = c.direction === "OUTBOUND";
  return `<div class="channel-card ${c.running ? "running" : ""}" data-open="${esc(c.id)}">
    <div class="top"><div class="ic">${icon(TYPE_ICON[c.type] || "integrations")}</div>
      <div style="min-width:0;flex:1"><b>${esc(c.name)}</b><div class="tiny muted">${esc(c.type_label)}</div></div>
      ${c.running ? liveBadge(isOut ? "WRITING" : "STREAMING") : chip(c.status)}</div>
    <div class="tiny mono muted" style="word-break:break-all">${esc(where || "")}</div>
    <div class="stats"><span><b>${fmtNum(c.counters.received || 0)}</b>${isOut ? "written" : "received"}</span>${isOut ? "" : `<span><b>${fmtNum(c.counters.accepted || 0)}</b>scored</span><span><b>${fmtNum(c.counters.rejected || 0)}</b>rejected</span>`}
      <span><b>${c.last_event_at ? esc(fmtRel(c.last_event_at)) : "–"}</b>last ${isOut ? "write" : "event"}</span></div>
    ${c.last_error && c.status === "DEGRADED" ? `<div class="notice crit tiny">${esc(c.last_error)}</div>` : ""}
    ${manage && runnable ? `<div class="row">${on
      ? `<button class="btn danger sm" data-stop="${esc(c.id)}">${icon("stop")} ${isOut ? "Stop writing" : "Stop streaming"}</button>`
      : `<button class="btn teal sm" data-start="${esc(c.id)}" ${c.last_test_ok ? "" : "disabled title='Test the connection first'"}>${icon("play")} ${isOut ? "Start writing results" : "Start streaming"}</button>`}
      <button class="btn sm" data-test="${esc(c.id)}">Test</button></div>` : ""}
  </div>`;
}

async function connectors(box) {
  const d = await api.get("/connectors");
  const rows = d.connectors;
  const manage = d.can_manage;
  const runningIn = rows.filter((c) => c.running && c.direction !== "OUTBOUND").length;
  const channels = rows.filter((c) => RUNNABLE.has(c.type) && c.status !== "RETIRED");
  box.innerHTML = `${!runningIn && manage ? `<div class="hero" style="margin-bottom:16px"><div style="flex:1;min-width:260px;position:relative;z-index:1">
      <h2>No channel is streaming</h2><p>Start a channel to receive transactions from the payment gateway. Each transaction is scored by the fraud engine as it arrives, and the result is stored and written to the results database.${S.me.tenant.demo_mode ? " In Demo Mode the gateway simulator produces realistic customer activity; nothing is pre-loaded." : ""}</p></div>
      ${channels.find((c) => c.type === "demo_stream" && c.last_test_ok) ? `<button class="btn teal" data-start="${esc(channels.find((c) => c.type === "demo_stream").id)}">${icon("play")} Start the payment gateway simulator</button>` : ""}</div>` : ""}
    <div class="kpis">
      ${kpi("Channels streaming", fmtNum(runningIn), `${S.me.tenant.demo_mode ? "Demo" : "Live"} workspace`, runningIn ? "accent" : "")}
      ${kpi("Enabled", fmtNum(rows.filter((c) => c.status === "ENABLED").length))}
      ${kpi("Degraded", fmtNum(rows.filter((c) => c.status === "DEGRADED").length), "", rows.some((c) => c.status === "DEGRADED") ? "crit" : "")}
      ${kpi("Events received", fmtNum(rows.filter((c) => c.direction !== "OUTBOUND").reduce((s, c) => s + (c.counters.received || 0), 0)))}
      ${kpi("Results written", fmtNum(rows.filter((c) => c.type === "db_results").reduce((s, c) => s + (c.counters.received || 0), 0)), "to the results database")}
    </div>
    ${channels.length ? `<h3 style="margin:18px 0 10px">Channels ${liveBadge("LIVE COUNTERS")}</h3><div class="grid g3">${channels.map((c) => channelCard(c, manage)).join("")}</div>` : ""}
    <div style="margin-top:16px">${card("All connectors", table([
      { label: "Connector", render: (c) => `<b>${esc(c.name)}</b><div class="tiny muted">${esc(c.type_label)}</div>` },
      { label: "Status", render: (c) => chip(c.status) + (c.running ? ' <span class="chip c-ok plain">RUNNING</span>' : "") },
      { label: "Env", render: (c) => chip(c.environment, "neutral") }, { label: "Direction", render: (c) => esc(label(c.direction)) },
      { label: "Mode", render: (c) => c.direction === "INBOUND" ? chip(c.ingest_mode) : "–" },
      { label: "Received", num: true, render: (c) => fmtNum(c.counters.received || 0) }, { label: "Accepted", num: true, render: (c) => fmtNum(c.counters.accepted || 0) },
      { label: "Rejected", num: true, render: (c) => fmtNum(c.counters.rejected || 0) },
      { label: "Last test", render: (c) => c.last_test_at ? `${c.last_test_ok ? chip("OK", "ok") : chip("FAILED", "crit")} <span class="tiny muted">${fmtRel(c.last_test_at)}</span>` : '<span class="muted">never</span>' },
      { label: "Last event", render: (c) => c.last_event_at ? fmtRel(c.last_event_at) : "–" },
      { label: "Credential", render: (c) => c.credential_expiry_warning ? chip(`EXPIRES ${c.credential_expires_in_days}D`, "warn") : c.secret_configured ? chip(c.secret_ref_kind, "ok") : '<span class="muted">none</span>' },
    ], rows, { onRow: true, empty: "No connectors yet." }), { flush: true, actions: manage ? '<button class="btn primary" id="add">Add connector</button>' : "" })}</div>
    <div class="notice" style="margin-top:16px">Channels start only when an IT Admin starts them. Secrets are stored in the encrypted vault or referenced from environment variables (<span class="code">env:NAME</span>) – never in connector configuration. Database sources are read-only; the results database receives every decision.</div>
    ${S.me.tenant.demo_mode ? `<div class="notice" style="margin-top:12px"><b>Other ways to send data:</b> append rows to the CSV drop file, POST JSON to the HTTP listener, or stream JSON lines to the TCP feed (start the channel first), e.g. <span class="code">python mock_channel/generate_transactions.py --verishield --output tcp --port 9009</span></div>` : ""}`;
  box.querySelectorAll("tr[data-row]").forEach((tr) => tr.addEventListener("click", () => openConnector(rows[+tr.dataset.row].id, () => connectors(box))));
  box.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", (e) => { if (!e.target.closest("button")) openConnector(el.dataset.open, () => connectors(box)); }));
  const act = (sel, attr, path, msg) => box.querySelectorAll(sel).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); busy(b, async () => {
    try { const r = await api.post(`/connectors/${b.dataset[attr]}/${path}`); toast(typeof msg === "function" ? msg(r) : msg); connectors(box); } catch (err) { errToast(err); } }); }));
  act("[data-start]", "start", "enable", "Channel started – transactions are scored as they arrive");
  act("[data-stop]", "stop", "disable", "Channel stopped");
  act("[data-test]", "test", "test", (r) => `${r.ok ? "Connection OK" : "Test failed"}: ${r.message}`);
  const add = box.querySelector("#add");
  if (add) add.onclick = () => editConnector(null, () => connectors(box));
}

async function database(box) {
  const d = await api.get("/system/database");
  const p = d.primary;
  box.innerHTML = `<div class="grid g2">
    ${card("Fraud engine database (primary)", `${kv([["Engine", `<b>${esc(p.engine)}</b>`], ["Connection", `<span class="mono small">${esc(p.url)}</span>`],
      ["Size", p.size_bytes ? `${fmtNum(p.size_bytes / 1048576, 1)} MB` : "–"], ["Connection pool", p.pool_size ?? "–"],
      ["Pending change", p.pending_change ? `<span class="mono small">${esc(p.pending_change)}</span> ${chip("RESTART REQUIRED", "warn")}` : "none"]])}
      <h3 style="margin:14px 0 6px">Stored records</h3>${kv(Object.entries(p.row_counts).map(([k, v]) => [label(k), fmtNum(v)]))}`, { sub: "customers, transactions, scores, alerts, cases and audit" })}
    ${card("Move the engine to another database", `<p class="small">Use the bank's PostgreSQL server for production volumes. The schema is created automatically at the next start; existing records are not copied (use a database migration or start fresh).</p>
      <label class="f" style="margin-top:10px">Database URL<input class="input mono" id="dburl" placeholder="postgresql://verishield:PASSWORD@dbserver:5432/verishield"></label>
      <div class="row" style="margin-top:10px"><button class="btn" id="dbtest">Test connection</button><span class="spacer"></span><button class="btn primary" id="dbsave">Save (applies after restart)</button></div>
      <div id="dbres" style="margin-top:10px"></div>`)}
  </div>
  <div style="margin-top:16px">${card("Results databases (decision write-back)", d.results_databases.length ? table([
    { label: "Name", render: (c) => `<b>${esc(c.name)}</b>` }, { label: "Target", render: (c) => `<span class="mono small">${esc(c.config.url)} › ${esc(c.config.table || "fraud_results")}</span>` },
    { label: "Status", render: (c) => chip(c.status) + (c.running ? ` ${liveBadge("WRITING")}` : "") }, { label: "Rows written", num: true, render: (c) => fmtNum(c.counters.received || 0) },
    { label: "Last write", render: (c) => c.last_event_at ? fmtRel(c.last_event_at) : "–" }], d.results_databases) : empty("No results database configured – add a connector of type “Fraud engine results database”."),
    { flush: true, sub: "Every scored transaction is written with its fraud score, profile scores, risk level, AML signal, decision, reasons and model / policy versions" })}</div>`;
  const res = box.querySelector("#dbres");
  box.querySelector("#dbtest").onclick = (e) => busy(e.currentTarget, async () => {
    try { const r = await api.post("/system/database/test", { url: box.querySelector("#dburl").value });
      res.innerHTML = `<div class="notice ${r.ok ? "ok" : "crit"}">${esc(r.detail)}</div>`; } catch (err) { errToast(err); } });
  box.querySelector("#dbsave").onclick = async () => {
    if (!(await confirmDialog("Change the fraud engine database", "VeriShield will use the new database after the service restarts. Continue?", "Save"))) return;
    try { const r = await api.put("/system/database", { url: box.querySelector("#dburl").value, confirm: "CHANGE" });
      res.innerHTML = `<div class="notice warn">${esc(r.message)}</div>`; } catch (err) { errToast(err); }
  };
}

function fieldInputs(type, cfg = {}) {
  const fields = (TYPES[type] || {}).fields || [];
  const hint = { url: "https://bank.example/api/transactions (use {secret} for a password placeholder)", records_path: "e.g. data.items",
    poll_seconds: "seconds", host: "hostname or IP", port: "", path: "/path or file path", table: "table name", id_column: "monotonic id column (rowid for SQLite)",
    speed: "catch-up speed (x real time), e.g. 60", fraud_ratio: "0.0 – 0.3", backfill: "true = also write past decisions", follow: "true/false", delimiter: ",", batch: "rows per poll" };
  if (!fields.length) return '<p class="small muted">No connection settings – the bank pushes events to VeriShield with an API key.</p>';
  return `<div class="form-grid">${fields.map((f) => `<label class="f">${esc(label(f))}<input class="input" name="cfg.${f}" value="${esc(cfg[f] ?? (type === "demo_stream" && f === "speed" ? 60 : type === "demo_stream" && f === "fraud_ratio" ? 0.03 : type === "db_results" && f === "table" ? "fraud_results" : ""))}" placeholder="${esc(hint[f] || "")}"></label>`).join("")}</div>`;
}

async function editConnector(c, done) {
  const types = Object.entries(TYPES).filter(([k, v]) => !v.demo_only || S.me.tenant.demo_mode);
  const type0 = c ? c.type : (S.me.tenant.demo_mode ? "demo_stream" : "rest_webhook");
  const r = await modal({ title: c ? `Edit ${c.name}` : "Add connector", wide: true,
    body: `<div class="form-grid">
        <label class="f">Type<select class="input" name="type" ${c ? "disabled" : ""}>${options(types.map(([k, v]) => [k, v.label]), type0)}</select></label>
        <label class="f">Name<input class="input" name="name" value="${esc(c ? c.name : "")}" required></label>
        <label class="f">Environment<select class="input" name="environment">${options(S.me.tenant.demo_mode ? ["DEV", "UAT"] : ["DEV", "UAT", "PROD"], c ? c.environment : "DEV")}</select></label>
        <label class="f">Ingest mode<select class="input" name="ingest_mode">${options([["POST_EVENT", "Post-event monitoring"], ["PRE_AUTH", "Pre-authorisation"], ["HISTORY", "History import (training)"]], c ? c.ingest_mode : "POST_EVENT")}</select></label>
        <label class="f">Authentication<select class="input" name="auth_method"></select></label>
        <label class="f">Owner<input class="input" name="owner" value="${esc(c ? c.owner : S.me.user.name)}"></label>
        <label class="f">Credential expiry<input class="input" type="date" name="credential_expires_at" value="${c && c.credential_expires_at ? c.credential_expires_at.slice(0, 10) : ""}"></label>
        <label class="f">Data scope<input class="input" name="data_scope" value="${esc(c ? c.data_scope : "")}" placeholder="e.g. retail transfers, read-only replica"></label>
      </div>
      <h3 style="margin:16px 0 8px">Connection settings</h3><div id="cfg"></div>
      <div class="form-grid" style="margin-top:12px">
        <label class="f">Field mapping (JSON, source → canonical)<textarea class="input mono" name="field_mapping" placeholder='{"cust_no": "user_id", "trx_amount": "amount"}'>${esc(c && Object.keys(c.field_mapping).length ? JSON.stringify(c.field_mapping) : "")}</textarea></label>
        <label class="f">Schedule / retries (JSON)<textarea class="input mono" name="schedule" placeholder='{"timeout_seconds": 5, "max_retries": 3, "backoff_seconds": 10}'>${esc(c && Object.keys(c.schedule).length ? JSON.stringify(c.schedule) : "")}</textarea></label>
        <label class="f">Allowed source IPs (comma separated)<input class="input" name="allowed_ips" value="${esc(c ? c.allowed_ips.join(", ") : "")}"></label>
        <label class="check" style="align-self:end"><input type="checkbox" name="tls_verify" ${!c || c.tls_verify ? "checked" : ""}> Verify TLS certificates</label>
      </div><p class="tiny muted" style="margin-top:8px" id="help"></p>`,
    onOpen: (m) => {
      const sel = m.querySelector("[name=type]");
      const sync = () => {
        const t = TYPES[sel.value];
        m.querySelector("#cfg").innerHTML = fieldInputs(sel.value, c ? c.config : {});
        m.querySelector("[name=auth_method]").innerHTML = options(t.auth, c ? c.auth_method : t.auth[0]);
        m.querySelector("#help").textContent = t.help || (t.runtime ? "" : "This type is registered and monitored; consuming requires a broker/transfer client adapter that is not bundled.");
      };
      sel.onchange = sync; sync();
    },
    actions: [{ label: "Cancel" }, { label: c ? "Save" : "Create connector", cls: "primary", onClick: async (m) => {
      const v = (n) => m.querySelector(`[name="${n}"]`);
      const cfg = {};
      m.querySelectorAll("[name^='cfg.']").forEach((i) => { if (i.value !== "") cfg[i.name.slice(4)] = isNaN(i.value) || i.value.trim() === "" ? (i.value === "true" ? true : i.value === "false" ? false : i.value) : Number(i.value); });
      const parse = (n) => { const t = v(n).value.trim(); if (!t) return {}; try { return JSON.parse(t); } catch { throw new Error(`${label(n)} must be valid JSON`); } };
      const body = { type: v("type").value, name: v("name").value, environment: v("environment").value, ingest_mode: v("ingest_mode").value,
        auth_method: v("auth_method").value, owner: v("owner").value, data_scope: v("data_scope").value, config: cfg,
        field_mapping: parse("field_mapping"), schedule: parse("schedule"), tls_verify: v("tls_verify").checked,
        allowed_ips: v("allowed_ips").value.split(",").map((x) => x.trim()).filter(Boolean), credential_expires_at: v("credential_expires_at").value || null };
      return c ? api.put(`/connectors/${c.id}`, body) : api.post("/connectors", body);
    } }] });
  if (r) { toast(c ? "Connector saved" : "Connector created as DRAFT – test it next"); done(); if (!c) openConnector(r.id, done); }
}

async function openConnector(id, done) {
  const dr = drawer(`<div class="dh"><h2>Connector</h2><button class="x" data-close>×</button></div><div class="db">${loading()}</div>`);
  const load = async () => {
    const [c, logs] = await Promise.all([api.get(`/connectors/${id}`), api.get(`/connectors/${id}/logs`)]);
    const manage = can("connectors.manage") && c.status !== "RETIRED";
    dr.set(`<div class="dh"><div><h2>${esc(c.name)}</h2><div class="small muted">${esc(c.type_label)} · v${c.version}</div></div><div class="row" style="margin-left:auto">${chip(c.status)}</div><button class="x" data-close style="margin-left:8px">×</button></div>
      <div class="db">
        ${manage ? `<div class="row"><button class="btn" data-a="test">Test connection</button><button class="btn" data-a="preview">Preview mapping</button>
          <button class="btn" data-a="secret">${c.secret_configured ? "Rotate" : "Set"} credential</button><button class="btn" data-a="edit">Edit</button>
          ${["VALIDATED", "DISABLED", "DEGRADED"].includes(c.status) ? '<button class="btn teal" data-a="enable">Enable</button>' : ""}
          ${["ENABLED", "DEGRADED"].includes(c.status) ? '<button class="btn" data-a="restart">Retry / restart</button><button class="btn" data-a="disable">Disable</button>' : ""}
          <button class="btn danger" data-a="retire">Retire</button></div>` : ""}
        ${c.last_error ? `<div class="notice crit"><b>Last error:</b> ${esc(c.last_error)}</div>` : ""}
        ${!c.runtime_supported ? '<div class="notice warn">Registered for monitoring and reachability tests; consuming this source needs a broker/transfer client adapter that is not bundled in this build.</div>' : ""}
        ${c.type === "rest_webhook" ? `<div class="notice">The bank pushes to <span class="code">POST ${esc(location.origin)}/api/v1/transactions/events</span> (async) or <span class="code">/api/v1/decisions/evaluate</span> (synchronous) with an <span class="code">X-API-Key</span>.</div>` : ""}
        <div class="grid g2">
          ${card("Health", kv([["Status", chip(c.status)], ["Runtime", c.running ? chip("RUNNING", "ok") : chip("STOPPED", "neutral")], ["Last test", c.last_test_at ? `${c.last_test_ok ? chip("OK", "ok") : chip("FAILED", "crit")} ${fmtDate(c.last_test_at)}<div class="small">${esc(c.last_test_message)}</div>` : "never"],
            ["Last success", c.last_success_at ? fmtDate(c.last_success_at) : "–"], ["Last event", c.last_event_at ? fmtDate(c.last_event_at) : "–"],
            ["Received / accepted", `${fmtNum(c.counters.received || 0)} / ${fmtNum(c.counters.accepted || 0)}`], ["Rejected / duplicates", `${fmtNum(c.counters.rejected || 0)} / ${fmtNum(c.counters.duplicates || 0)}`],
            ["Last batch latency", c.runtime.last_batch_ms ? `${c.runtime.last_batch_ms} ms` : "–"]]))}
          ${card("Configuration", kv([["Environment", esc(c.environment)], ["Direction", esc(c.direction)], ["Ingest mode", chip(c.ingest_mode)], ["Auth", esc(c.auth_method)],
            ["Credential", c.secret_configured ? `${chip(c.secret_ref_kind, "ok")} <span class="tiny muted">value never shown</span>` : "none"],
            ["Credential expiry", c.credential_expires_at ? `${fmtDate(c.credential_expires_at, { time: false })} ${c.credential_expiry_warning ? chip("ROTATE SOON", "warn") : ""}` : "–"],
            ["TLS verification", c.tls_verify ? "on" : chip("OFF", "warn")], ["Permission", esc(c.permission)], ["Settings", `<span class="mono small">${esc(JSON.stringify(c.config))}</span>`],
            ["Field mapping", `<span class="mono small">${esc(JSON.stringify(c.field_mapping))}</span>`], ["Owner", esc(c.owner)]]))}
        </div>
        <div id="pv"></div>
        ${card("Activity log", table([{ label: "Time", render: (l) => fmtDate(l.created_at, { seconds: true }) }, { label: "Level", render: (l) => chip(l.level, l.level === "ERROR" ? "crit" : "neutral") },
          { label: "Message", render: (l) => `<span class="small">${esc(l.message)}</span>` }, { label: "By", key: "actor" }], logs.logs), { flush: true })}
      </div>`);
    const a = (n) => dr.el.querySelector(`[data-a=${n}]`);
    const act = (n, fn) => { if (a(n)) a(n).onclick = (e) => busy(e.currentTarget, async () => { try { await fn(); await load(); done(); } catch (err) { errToast(err); } }); };
    act("test", async () => { const r = await api.post(`/connectors/${id}/test`); toast(r.message, r.ok ? "ok" : "error", 6000); });
    act("enable", async () => { await api.post(`/connectors/${id}/enable`); toast("Connector enabled"); });
    act("disable", async () => { await api.post(`/connectors/${id}/disable`); toast("Connector disabled"); });
    act("restart", async () => { await api.post(`/connectors/${id}/restart`); toast("Connector restarted"); });
    if (a("edit")) a("edit").onclick = () => editConnector(c, () => { load(); done(); });
    if (a("retire")) a("retire").onclick = async () => {
      const r = await modal({ title: "Retire connector", body: `<p>This stops the connector permanently. Type <b>${esc(c.name)}</b> to confirm.</p><input class="input" name="confirm">`,
        actions: [{ label: "Cancel" }, { label: "Retire", cls: "danger", onClick: (m) => api.post(`/connectors/${id}/retire`, { confirm: m.querySelector("[name=confirm]").value }) }] });
      if (r) { toast("Connector retired"); dr.close(); done(); }
    };
    if (a("secret")) a("secret").onclick = async () => {
      const r = await modal({ title: "Set credential", body: `<p class="small muted">Stored encrypted in the vault, or reference an environment variable injected by your secrets manager. The value is never displayed again.</p>
        <label class="f">Secret value<input class="input" type="password" name="value" autocomplete="off"></label><p class="center small muted" style="margin:8px">or</p>
        <label class="f">Environment variable name<input class="input mono" name="env_ref" placeholder="BOP_CORE_API_TOKEN"></label>
        <label class="f" style="margin-top:10px">Expires<input class="input" type="date" name="expires_at"></label>`,
        actions: [{ label: "Cancel" }, { label: "Save credential", cls: "primary", onClick: (m) => api.post(`/connectors/${id}/secret`, {
          value: m.querySelector("[name=value]").value || null, env_ref: m.querySelector("[name=env_ref]").value || null, expires_at: m.querySelector("[name=expires_at]").value || null }) }] });
      if (r) { toast("Credential stored"); load(); }
    };
    if (a("preview")) a("preview").onclick = async () => {
      const sample = ["file_drop", "db_replica", "rest_pull"].includes(c.type);
      const r = await modal({ title: "Preview field mapping", wide: true, body: `${sample ? '<p class="small muted">Leave empty to read up to 5 records from the source, or paste sample JSON.</p>' : '<p class="small muted">Paste one or more sample records (JSON object or array).</p>'}
        <textarea class="input mono" name="samples" style="min-height:140px" placeholder='[{"transaction_id":"T1","customer_id":"C00001","transaction_type":"bank_transfer","amount":120,"beneficiary_reference":"PS92PALS000000000400123456702"}]'></textarea>`,
        actions: [{ label: "Cancel" }, { label: "Preview", cls: "primary", onClick: async (m) => {
          const t = m.querySelector("[name=samples]").value.trim();
          let samples = null;
          if (t) { try { samples = JSON.parse(t); if (!Array.isArray(samples)) samples = [samples]; } catch { throw new Error("Sample must be valid JSON"); } }
          return api.post(`/connectors/${id}/preview`, { samples });
        } }] });
      if (r) dr.el.querySelector("#pv").innerHTML = card(`Mapping preview – ${r.accepted}/${r.samples} accepted (masked)`, table([
        { label: "", render: (x) => x.ok ? chip("OK", "ok") : chip("REJECTED", "crit") }, { label: "Transaction", render: (x) => esc(x.transaction_id || "") },
        { label: "Customer", render: (x) => esc(x.customer_id || "") }, { label: "Type", render: (x) => esc(x.type || "") }, { label: "Amount", num: true, render: (x) => esc(x.amount ?? "") },
        { label: "Beneficiary", render: (x) => esc(x.beneficiary || "") }, { label: "Error / source fields", render: (x) => `<span class="small">${esc(x.error || x.source_fields.join(", "))}</span>` }], r.rows), { flush: true });
    };
  };
  load().catch((e) => { errToast(e); dr.close(); });
}

async function deadLetters(box) {
  const f = { status: "PENDING", category: "" };
  const load = async () => {
    const d = await api.get(`/dead-letters?status=${f.status}&category=${f.category}`);
    box.innerHTML = card("Dead-letter queue", `<div class="filters" style="margin-bottom:12px"><select class="input" name="status">${options(["PENDING", "REPLAYED", "DISCARDED"], f.status, { blank: "All" })}</select>
      <select class="input" name="category">${options(["VALIDATION", "DUPLICATE", "PROCESSING"], f.category, { blank: "All categories" })}</select>
      <span class="small muted">${Object.entries(d.totals).map(([k, v]) => `${label(k)}: ${v}`).join(" · ")}</span></div>` + table([
      { label: "Received", render: (r) => fmtDate(r.created_at) }, { label: "Source", key: "source" }, { label: "Category", render: (r) => chip(r.category, r.category === "DUPLICATE" ? "neutral" : "warn") },
      { label: "Reason", render: (r) => `<span class="small">${esc(r.reason)}</span>` }, { label: "Payload", render: (r) => `<span class="mono tiny">${esc(JSON.stringify(r.payload).slice(0, 140))}</span>` },
      { label: "Retries", num: true, key: "retries" }, { label: "Status", render: (r) => chip(r.status) },
      { label: "", render: (r) => can("connectors.manage") && r.status === "PENDING" ? `<div class="row nowrap"><button class="btn sm" data-replay="${esc(r.id)}">Replay</button><button class="btn sm" data-discard="${esc(r.id)}">Discard</button></div>` : "" },
    ], d.items, { empty: "No records in the dead-letter queue." }));
    box.querySelectorAll("select").forEach((s) => (s.onchange = () => { f[s.name] = s.value; load(); }));
    box.querySelectorAll("[data-replay]").forEach((b) => (b.onclick = async () => {
      const r = await modal({ title: "Replay record", body: `<p class="small muted">Optionally correct fields before replaying (JSON patch merged into the payload; audited).</p><textarea class="input mono" name="patch" placeholder='{"amount": 120.5}'></textarea>`,
        actions: [{ label: "Cancel" }, { label: "Replay", cls: "primary", onClick: (m) => { const t = m.querySelector("[name=patch]").value.trim(); return api.post(`/dead-letters/${b.dataset.replay}/replay`, { patch: t ? JSON.parse(t) : null }); } }] });
      if (r) { toast(r.status === "REPLAYED" ? "Replayed successfully" : "Replay rejected again – see the new dead-letter entry", r.status === "REPLAYED" ? "ok" : "warn"); load(); }
    }));
    box.querySelectorAll("[data-discard]").forEach((b) => (b.onclick = async () => {
      if (!(await confirmDialog("Discard record", "Mark this record as discarded? It stays in the audit trail."))) return;
      try { await api.post(`/dead-letters/${b.dataset.discard}/discard`); load(); } catch (e) { errToast(e); }
    }));
  };
  await load();
}

async function push(box) {
  box.innerHTML = card("Push integration (REST / webhook)", `<p>Bank systems integrate through versioned APIs authenticated with an API key (scopes control what each system may do). Keys are bound to the workspace in which they were created (${S.me.tenant.demo_mode ? "<b>Demo</b>" : "<b>Live</b>"} now).</p>
    <table class="tbl" style="margin-top:12px"><thead><tr><th>Endpoint</th><th>Scope</th><th>Purpose</th></tr></thead><tbody>
    <tr><td class="mono small">POST /api/v1/decisions/evaluate</td><td class="mono small">decisions:write</td><td>Synchronous pre-authorisation decision (idempotent)</td></tr>
    <tr><td class="mono small">POST /api/v1/transactions/events</td><td class="mono small">events:write</td><td>Asynchronous post-event monitoring or history import (batch up to 5,000)</td></tr>
    <tr><td class="mono small">POST /api/v1/transactions/{id}/execution-result</td><td class="mono small">execution:write</td><td>Bank reports held / authorised / declined / settled / recalled</td></tr>
    <tr><td class="mono small">GET /api/v1/transactions/{id}/decision</td><td class="mono small">decisions:write</td><td>Original decision, policy and model version</td></tr>
    <tr><td class="mono small">GET /health · /ready · /metrics</td><td>–</td><td>Liveness, readiness and Prometheus metrics</td></tr></tbody></table>
    <p class="small" style="margin-top:12px">Create keys under <a href="#/settings">Users &amp; Settings › API clients</a>. Full contracts: <a href="/api/docs" target="_blank" rel="noopener">OpenAPI documentation</a>.</p>`);
}

async function quality(box) {
  const d = await api.get("/connectors-quality?hours=24");
  box.innerHTML = `<div class="grid g2">${d.channels.map((q) => card(q.name, `<div class="row" style="margin-bottom:10px">${chip(q.health, q.health === "OK" ? "ok" : "warn")} ${chip(q.status)}<span class="spacer"></span>
      <span class="small muted">${q.rate_per_hour}/h${q.baseline_per_hour ? ` · usual ${q.baseline_per_hour}/h` : ""}</span></div>
    ${q.issues.length ? `<div class="notice warn" style="margin-bottom:10px">${q.issues.map(esc).join("<br>")}</div>` : ""}
    ${kv([["Received (24h)", fmtNum(q.received)], ["Accepted", fmtNum(q.accepted)], ["Rejected", fmtNum(q.rejected)], ["Duplicates", fmtNum(q.duplicates)],
      ["Unknown types", fmtNum(q.unknown_types)], ["Median delivery delay", q.median_delay_min == null ? "–" : `${q.median_delay_min} min`]])}
    <h4 style="margin:12px 0 6px">Missing fields</h4>${hbarsQ(Object.entries(q.missing_pct).map(([k, v]) => ({ label: label(k), value: v ?? 0, color: (v || 0) > 20 ? "#E8A33A" : "#0BAA9B" })))}
    ${Object.keys(q.types).length ? `<h4 style="margin:12px 0 6px">Products</h4><div class="codes">${Object.entries(q.types).map(([k, v]) => `<span class="code">${esc(k)} ${v}</span>`).join("")}</div>` : ""}
    ${q.top_rejections.length ? `<h4 style="margin:12px 0 6px">Top rejection reasons</h4><ul class="small">${q.top_rejections.map(([r, n]) => `<li>${esc(r)} (${n})</li>`).join("")}</ul>` : ""}`,
    { sub: esc(q.type) })).join("") || empty("No inbound channels.")}</div>
  <p class="tiny muted" style="margin-top:10px">Checked hourly: a drop below 30% of the usual volume, more than 5% rejected records, unknown transaction types or missing key fields raise an operations incident.</p>`;
}
function hbarsQ(items) { return items.length ? items.map((i) => `<div style="display:grid;grid-template-columns:130px 1fr 52px;gap:8px;align-items:center;font-size:12.5px;margin-bottom:4px"><span>${esc(i.label)}</span><div class="bar"><i style="width:${i.value}%;background:${i.color}"></i></div><b class="right">${i.value}%</b></div>`).join("") : ""; }

// ------------------------------------------------------------------ monitoring & SIEM
async function monitoring(box) {
  const d = await api.get("/siem");
  const c = d.config, st = d.state || {};
  box.innerHTML = `<div class="grid g2"><form id="sf">${card("Forward security events to the SIEM", `
      <div class="row" style="margin-bottom:14px"><label class="check"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""}> Forwarding ON</label><span class="spacer"></span>
        ${c.enabled ? (st.last_error ? chip("ERROR", "crit") : chip("FORWARDING", "ok")) : chip("OFF", "neutral")}</div>
      <div class="form-grid">
        <label class="f">Protocol<select class="input" name="protocol">${options([["udp", "Syslog UDP"], ["tcp", "Syslog TCP"], ["tls", "Syslog TLS"], ["https", "HTTPS collector (Splunk HEC, Elastic...)"]], c.protocol)}</select></label>
        <label class="f">Format<select class="input" name="format">${options([["cef", "CEF (ArcSight, QRadar, Sentinel)"], ["json", "JSON"]], c.format)}</select></label>
        <label class="f">Host<input class="input mono" name="host" value="${esc(c.host)}" placeholder="siem.bop.local"></label>
        <label class="f">Port<input class="input" type="number" name="port" value="${esc(c.port)}"></label>
        <label class="f" style="grid-column:1/-1">Collector URL (HTTPS)<input class="input mono" name="url" value="${esc(c.url)}" placeholder="https://splunk.bop.local:8088/services/collector"></label>
        <label class="f">Syslog facility<input class="input" type="number" name="facility" value="${esc(c.facility)}"></label>
        <label class="f">Token prefix (HTTPS)<input class="input" name="token_prefix" value="${esc(c.token_prefix)}"></label>
        <label class="check"><input type="checkbox" name="verify_tls" ${c.verify_tls ? "checked" : ""}> Verify the certificate</label>
        <label class="check"><input type="checkbox" name="include_demo" ${c.include_demo ? "checked" : ""}> Include Demo workspace events</label>
      </div>
      <div class="row" style="margin-top:12px"><span class="small">Collector token: ${c.token_configured ? chip("STORED IN VAULT", "ok") : chip("NOT SET", "neutral")}</span><button class="btn sm" type="button" id="tok">Set token</button></div>`,
    { actions: '<button class="btn sm" type="button" id="test">Send test event</button><button class="btn sm primary">Save</button>',
      sub: "Every audit event - sign-ins and failures, permission denials, configuration changes, decisions by people, exports, agent calls - is sent once, in order." })}</form>
    ${card("Forwarding state", kv([["Events sent", fmtNum(st.sent || 0)], ["Waiting (lag)", st.lag == null ? "–" : fmtNum(st.lag)], ["Last sent", st.last_sent_at ? fmtRel(st.last_sent_at) : "–"],
      ["Last error", st.last_error ? `<span class="small" style="color:var(--red)">${esc(st.last_error)}</span>` : "none"], ["Cursor (audit id)", esc(st.cursor ?? "–")]]) +
      `<h4 style="margin:16px 0 6px">Prometheus / Grafana</h4><p class="small">Scrape <span class="code">GET /metrics</span> on every server (bearer token <span class="code">VERISHIELD_METRICS_TOKEN</span> when set).
       Gauges include the cluster leader, open alerts and cases, overdue cases, channel status, dead letters, frozen cards, draft CTRs, decisions in the last 5 minutes and the SIEM lag.
       A ready-made Grafana dashboard and alert rules are in <span class="code">deploy/monitoring/</span>.</p>`)}</div>`;
  const f = box.querySelector("#sf");
  f.onsubmit = (e) => { e.preventDefault(); busy(f.querySelector("button.primary"), async () => {
    try { const v = {}; f.querySelectorAll("[name]").forEach((i) => (v[i.name] = i.type === "checkbox" ? i.checked : i.type === "number" ? Number(i.value) : i.value.trim()));
      await api.put("/siem", v); toast("SIEM settings saved"); monitoring(box); } catch (err) { errToast(err); } }); };
  const t = box.querySelector("#test");
  t.onclick = () => busy(t, async () => { try { const r = await api.post("/siem/test"); toast(r.ok ? r.detail : `Not sent: ${r.detail}`, r.ok ? "ok" : "error", 8000); } catch (e) { errToast(e); } });
  box.querySelector("#tok").onclick = async () => {
    const r = await modal({ title: "SIEM collector token", body: '<label class="f">Token<input class="input" type="password" name="v" autocomplete="new-password"></label><p class="tiny muted" style="margin-top:8px">Stored encrypted in the vault.</p>',
      actions: [{ label: "Cancel" }, { label: "Store", cls: "primary", onClick: (m) => api.post("/siem/secret", { value: m.querySelector("[name=v]").value }) }] });
    if (r) { toast("Stored in the vault"); monitoring(box); }
  };
}

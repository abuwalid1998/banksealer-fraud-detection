// Core banking APIs: the IT Admin configures connections, operations (APIs) and their purposes; AML users request
// operations (reverse, block, freeze...) and approve them; everyone with access sees the call log.
import { api } from "../api.js";
import { S, busy, card, chip, confirmDialog, drawer, errToast, esc, fmtDate, fmtNum, fmtRel, kpi, kv, label, loading,
  modal, options, table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);
const STATUS_TONE = { SUCCESS: "ok", FAILED: "crit", PENDING_APPROVAL: "warn", REJECTED: "neutral", DRY_RUN: "info", NO_API: "neutral" };
const lines = (obj, sep = " = ") => Object.entries(obj || {}).map(([k, v]) => `${k}${sep}${v}`).join("\n");
const parseLines = (text, sep = "=") => Object.fromEntries((text || "").split("\n").map((l) => l.trim()).filter(Boolean)
  .map((l) => { const i = l.indexOf(sep); return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + sep.length).trim()]; }).filter(([k]) => k));
const pretty = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));

export async function render(el, { params = {} } = {}) {
  const tabs = [];
  if (can("core.configure")) tabs.push(["apis", "Operations (APIs)"], ["connections", "Connections"], ["purposes", "Purposes"]);
  if (can("core.execute")) tabs.push(["request", "Request an operation"]);
  if (can("core.approve")) tabs.push(["approvals", "Approvals"]);
  if (can("core.view") || can("core.configure")) tabs.push(["log", "Call log"]);
  let cur = params.tab || tabs[0][0];
  const draw = async () => {
    el.innerHTML = `<div id="kp">${loading()}</div><div class="tabs" style="margin-top:14px">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const ov = await api.get("/core/overview");
    const t = ov.today || {};
    el.querySelector("#kp").innerHTML = `<div class="kpis">${kpi("Purposes with an active API", `${Object.values(ov.purposes).filter((p) => p.active).length} / ${Object.keys(ov.purposes).length}`, `${ov.space === "DEMO" ? "Demo" : "Live"} workspace`, "accent")}
      ${kpi("Operations configured", fmtNum(ov.apis.length))}${kpi("Calls today", fmtNum(Object.values(t).reduce((a, b) => a + b, 0)), `${fmtNum(t.SUCCESS || 0)} successful`)}
      ${kpi("Failed today", fmtNum(t.FAILED || 0), "", t.FAILED ? "crit" : "")}${kpi("Waiting for approval", fmtNum(ov.pending_approvals), "four eyes", ov.pending_approvals ? "warn" : "")}</div>`;
    const box = el.querySelector("#tab");
    ({ apis: apisTab, connections: connTab, purposes: purposesTab, request: requestTab, approvals: approvalsTab, log: logTab })[cur](box, ov, draw).catch(errToast);
  };
  await draw();
}

// ------------------------------------------------------------------ operations (APIs)
async function apisTab(box, ov, reload) {
  const conns = Object.fromEntries(ov.connections.map((c) => [c.id, c]));
  box.innerHTML = card("Core banking operations", table([
    { label: "Operation", render: (a) => `<b>${esc(a.name)}</b><div class="tiny muted">${esc(a.description || "")}</div>` },
    { label: "Purpose", render: (a) => `<span class="code">${esc(a.purpose)}</span>` },
    { label: "Request", render: (a) => `<span class="mono small">${esc(a.method)} ${esc(a.path)}</span><div class="tiny muted">${esc((conns[a.connection_id] || {}).name || "?")}</div>` },
    { label: "Workspaces", render: (a) => `${a.live_enabled ? chip("LIVE", "ok") : chip("LIVE OFF", "neutral")} ${a.demo_enabled ? chip("DEMO", "warn") : ""}` },
    { label: "Approval", render: (a) => (a.requires_approval ? chip("FOUR EYES", "warn") : "–") },
    { label: "Version", num: true, render: (a) => `v${a.version}<div class="tiny muted">${esc(a.updated_by || "")}</div>` },
    { label: "", render: (a) => `<div class="row nowrap"><button class="btn sm" data-ed="${a.id}">Edit</button><button class="btn sm" data-te="${a.id}">Test</button><button class="btn sm" data-du="${a.id}">Copy</button><button class="btn sm danger" data-de="${a.id}">Delete</button></div>` },
  ], ov.apis, { empty: "No operation yet." }), { flush: true,
    sub: "One operation per purpose and workspace is used (lowest priority number first). Release / reject of held payments and card freezes use these operations automatically.",
    actions: '<button class="btn sm primary" id="add">Add operation</button>' });
  box.querySelector("#add").onclick = () => editApi(null, ov, reload);
  box.querySelectorAll("[data-ed]").forEach((b) => (b.onclick = () => editApi(ov.apis.find((a) => a.id === b.dataset.ed), ov, reload)));
  box.querySelectorAll("[data-te]").forEach((b) => (b.onclick = () => testApi(ov.apis.find((a) => a.id === b.dataset.te))));
  box.querySelectorAll("[data-du]").forEach((b) => (b.onclick = () => busy(b, async () => { try { await api.post(`/core/apis/${b.dataset.du}/duplicate`); toast("Copied (Live off)"); reload(); } catch (e) { errToast(e); } })));
  box.querySelectorAll("[data-de]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Delete operation", "VeriShield will no longer call this API.", "Delete", "danger"))) return;
    try { await api.del(`/core/apis/${b.dataset.de}`); toast("Deleted"); reload(); } catch (e) { errToast(e); }
  }));
}

async function editApi(a, ov, reload) {
  a = a || { method: "POST", body_type: "JSON", success_codes: [200, 201, 202, 204], idempotency_header: "Idempotency-Key", allowed_roles: ["AML_MANAGER", "CHIEF_AML"], demo_enabled: true, priority: 100, output_mapping: { reference: "reference", status: "status", message: "message" } };
  const purposes = Object.values(ov.purposes);
  const inputsText = (a.inputs || []).map((i) => [i.name, i.label, i.required ? "required" : "optional", i.example || ""].join(" | ")).join("\n");
  const r = await modal({ title: a.id ? `Edit ${a.name}` : "Add core banking operation", wide: true, body: `
    <div class="form-grid">
      <label class="f">Name<input class="input" name="name" value="${esc(a.name || "")}" placeholder="e.g. T24 card block"></label>
      <label class="f">Purpose<select class="input" name="purpose">${options(purposes.map((p) => [p.code, `${p.label} (${p.code})`]), a.purpose || "")}</select></label>
      <label class="f">Connection<select class="input" name="connection_id">${options(ov.connections.map((c) => [c.id, c.name]), a.connection_id || "")}</select></label>
      <label class="f">Method<select class="input" name="method">${options(ov.enums.methods.map((m) => [m, m]), a.method)}</select></label>
      <label class="f" style="grid-column:1/-1">Path<span class="help">relative to the connection's base URL - placeholders like {{transaction_id}} are URL-encoded</span><input class="input mono" name="path" value="${esc(a.path || "")}" placeholder="/payments/{{transaction_id}}/reject"></label>
      <label class="f">Query parameters<span class="help">one per line: name = template</span><textarea class="input mono" name="query" rows="3">${esc(lines(a.query))}</textarea></label>
      <label class="f">Headers<span class="help">one per line: name = template</span><textarea class="input mono" name="headers" rows="3">${esc(lines(a.headers))}</textarea></label>
      <label class="f">Body type<select class="input" name="body_type">${options(ov.enums.body_types.map((m) => [m, m]), a.body_type)}</select></label>
      <label class="f">Idempotency header<input class="input" name="idempotency_header" value="${esc(a.idempotency_header || "")}"></label>
      <label class="f" style="grid-column:1/-1">Body template<span class="help">JSON (or FORM as JSON object, or XML text). "{{amount}}" keeps the number type; filters: |cents |int |upper |lower |date |last4 |digits |default:x</span><textarea class="input mono" name="body_template" rows="6">${esc(a.body_template || "")}</textarea></label>
      <label class="f" style="grid-column:1/-1">Inputs<span class="help">one per line: name | label | required/optional | example - VeriShield fills transaction, customer, card and amount fields automatically</span><textarea class="input mono" name="inputs" rows="4">${esc(inputsText)}</textarea></label>
      <label class="f">Output mapping<span class="help">one per line: result field = response path (data.reference, items[0].id, Body/Ref)</span><textarea class="input mono" name="output_mapping" rows="4">${esc(lines(a.output_mapping))}</textarea></label>
      <div class="stack" style="gap:8px">
        <label class="f">Success HTTP codes<input class="input" name="success_codes" value="${esc((a.success_codes || []).join(", "))}"></label>
        <label class="f">…and result field<input class="input" name="success_field" value="${esc(a.success_field || "")}" placeholder="status"></label>
        <label class="f">…equal to one of<input class="input" name="success_values" value="${esc((a.success_values || []).join(", "))}" placeholder="OK, ACCEPTED"></label></div>
      <label class="f">Timeout override (s)<input class="input" type="number" step="0.5" name="timeout_seconds" value="${esc(a.timeout_seconds ?? "")}"></label>
      <label class="f">Retries override<input class="input" type="number" name="retries" value="${esc(a.retries ?? "")}"></label>
      <label class="f">Priority<span class="help">lowest is used first for its purpose</span><input class="input" type="number" name="priority" value="${esc(a.priority ?? 100)}"></label>
      <label class="f" style="grid-column:1/-1">Description<input class="input" name="description" value="${esc(a.description || "")}"></label>
    </div>
    <div class="row" style="margin-top:12px;gap:20px;flex-wrap:wrap">
      <label class="check"><input type="checkbox" name="live_enabled" ${a.live_enabled ? "checked" : ""}> Use in the <b>Live</b> workspace</label>
      <label class="check"><input type="checkbox" name="demo_enabled" ${a.demo_enabled ? "checked" : ""}> Use in the Demo workspace</label>
      <label class="check"><input type="checkbox" name="requires_approval" ${a.requires_approval ? "checked" : ""}> Manual requests need a second person's approval</label></div>
    <div class="row" style="margin-top:8px;gap:14px;flex-wrap:wrap"><span class="small">May be requested by:</span>${ov.enums.roles.map((r) => `<label class="check"><input type="checkbox" data-role="${r}" ${(a.allowed_roles || []).includes(r) ? "checked" : ""}> ${esc(label(r))}</label>`).join("")}</div>`,
    actions: [{ label: "Cancel" }, { label: a.id ? "Save new version" : "Create", cls: "primary", onClick: (m) => {
      const v = (n) => m.querySelector(`[name=${n}]`);
      const body = {
        name: v("name").value.trim(), purpose: v("purpose").value, connection_id: v("connection_id").value, method: v("method").value,
        path: v("path").value.trim(), query: parseLines(v("query").value), headers: parseLines(v("headers").value),
        body_type: v("body_type").value, body_template: v("body_template").value, idempotency_header: v("idempotency_header").value.trim(),
        inputs: v("inputs").value.split("\n").map((l) => l.split("|").map((x) => x.trim())).filter((x) => x[0]).map(([name, lab, req, ex]) => ({ name, label: lab || name, required: (req || "required").toLowerCase() !== "optional", example: ex || "" })),
        output_mapping: parseLines(v("output_mapping").value),
        success_codes: v("success_codes").value.split(",").map((x) => x.trim()).filter(Boolean).map(Number),
        success_field: v("success_field").value.trim(), success_values: v("success_values").value.split(",").map((x) => x.trim()).filter(Boolean),
        timeout_seconds: v("timeout_seconds").value ? Number(v("timeout_seconds").value) : null, retries: v("retries").value !== "" ? Number(v("retries").value) : null,
        priority: Number(v("priority").value || 100), description: v("description").value,
        live_enabled: v("live_enabled").checked, demo_enabled: v("demo_enabled").checked, requires_approval: v("requires_approval").checked,
        allowed_roles: [...m.querySelectorAll("[data-role]")].filter((x) => x.checked).map((x) => x.dataset.role),
      };
      return a.id ? api.put(`/core/apis/${a.id}`, body) : api.post("/core/apis", body);
    } }] });
  if (r) { toast(a.id ? `Saved as version ${r.version}` : "Operation created"); reload(); }
}

async function testApi(a) {
  const sample = Object.fromEntries((a.inputs || []).map((i) => [i.name, i.example || ""]));
  const dr = drawer(`<div class="dh"><h2>Test – ${esc(a.name)}</h2><button class="x" data-close>×</button></div>
    <div class="db"><label class="f">Inputs (JSON)<textarea class="input mono" id="in" rows="7">${esc(JSON.stringify(sample, null, 2))}</textarea></label>
      <div class="row" style="margin:10px 0"><button class="btn" id="dry">Dry run (render only)</button><button class="btn danger" id="send">Send for real</button>
      <span class="tiny muted">A real call changes data in the core system of this connection.</span></div><div id="out"></div></div>`);
  const run = (send) => async () => {
    let inputs;
    try { inputs = JSON.parse(dr.el.querySelector("#in").value || "{}"); } catch { toast("Inputs must be valid JSON", "error"); return; }
    if (send && !(await confirmDialog("Send for real", "The request is sent to the connection's system. Use a test system or test data.", "Send", "danger"))) return;
    try { const c = await api.post(`/core/apis/${a.id}/test`, { inputs, send }); dr.el.querySelector("#out").innerHTML = callDetail(c); } catch (e) { errToast(e); }
  };
  dr.el.querySelector("#dry").onclick = run(false);
  dr.el.querySelector("#send").onclick = run(true);
}

function callDetail(c) {
  return `${card("Result", kv([["Status", chip(c.status, STATUS_TONE[c.status] || "neutral")], ["HTTP", esc(c.http_status ?? "–")], ["Attempts", esc(c.attempts)],
      ["Duration", c.duration_ms ? `${c.duration_ms} ms` : "–"], ["Idempotency key", `<span class="mono small">${esc(c.idempotency_key || "–")}</span>`],
      ["Error", c.error ? `<span style="color:var(--red)">${esc(c.error)}</span>` : "–"], ["Requested by", esc(c.requested_by)], ["Approved by", esc(c.approved_by || "–")]]))}
    ${card("Mapped result", `<pre class="box mono small">${esc(pretty(c.result || {}))}</pre>`)}
    ${card("Request sent (masked)", `<pre class="box mono small">${esc(pretty(c.request || {}))}</pre>`)}
    ${c.response_excerpt ? card("Response (masked excerpt)", `<pre class="box mono small">${esc(c.response_excerpt)}</pre>`) : ""}
    ${card("Inputs", `<pre class="box mono small">${esc(pretty(c.inputs || {}))}</pre>`)}`;
}

// ------------------------------------------------------------------ connections
async function connTab(box, ov, reload) {
  box.innerHTML = card("Connections to core systems", table([
    { label: "Connection", render: (c) => `<b>${esc(c.name)}</b><div class="mono tiny muted">${esc(c.base_url)}</div>` },
    { label: "Security", render: (c) => `${chip(c.auth_type, "neutral")} ${c.secret_configured ? chip("SECRET IN VAULT", "ok") : c.auth_type !== "NONE" ? chip("NO SECRET", "crit") : ""}${c.client_cert_file ? " " + chip("mTLS", "info") : ""}${c.verify_tls ? "" : " " + chip("TLS NOT VERIFIED", "warn")}` },
    { label: "Timeout / retries", render: (c) => `${c.timeout_seconds}s / ${c.retries}` },
    { label: "Last test", render: (c) => (c.last_test_at ? `${chip(c.last_test_ok ? "OK" : "FAILED", c.last_test_ok ? "ok" : "crit")} <span class="tiny muted">${fmtRel(c.last_test_at)} · ${esc(c.last_test_detail)}</span>` : "never") },
    { label: "Status", render: (c) => (c.enabled ? chip("ENABLED", "ok") : chip("DISABLED", "neutral")) },
    { label: "", render: (c) => `<div class="row nowrap"><button class="btn sm" data-ed="${c.id}">Edit</button><button class="btn sm" data-sec="${c.id}">Secret</button><button class="btn sm" data-te="${c.id}">Test</button><button class="btn sm danger" data-de="${c.id}">Delete</button></div>` },
  ], ov.connections, { empty: "No connection yet." }), { flush: true, sub: "Secrets (API keys, tokens, passwords, client secrets, HMAC keys) are stored encrypted in the vault and never shown again.",
    actions: '<button class="btn sm primary" id="add">Add connection</button>' });
  box.querySelector("#add").onclick = () => editConn(null, ov, reload);
  box.querySelectorAll("[data-ed]").forEach((b) => (b.onclick = () => editConn(ov.connections.find((c) => c.id === b.dataset.ed), ov, reload)));
  box.querySelectorAll("[data-te]").forEach((b) => (b.onclick = () => busy(b, async () => { try { const r = await api.post(`/core/connections/${b.dataset.te}/test`); toast(r.detail, r.ok ? "ok" : "error", 7000); reload(); } catch (e) { errToast(e); } })));
  box.querySelectorAll("[data-de]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Delete connection", "Only possible when no operation uses it.", "Delete", "danger"))) return;
    try { await api.del(`/core/connections/${b.dataset.de}`); toast("Deleted"); reload(); } catch (e) { errToast(e); }
  }));
  box.querySelectorAll("[data-sec]").forEach((b) => (b.onclick = async () => {
    const r = await modal({ title: "Connection secret", body: '<label class="f">API key / token / password / client secret / HMAC key<input class="input" type="password" name="v" autocomplete="new-password"></label><p class="tiny muted" style="margin-top:8px">Stored encrypted in the vault.</p>',
      actions: [{ label: "Cancel" }, { label: "Store", cls: "primary", onClick: (m) => api.post(`/core/connections/${b.dataset.sec}/secret`, { value: m.querySelector("[name=v]").value }) }] });
    if (r) { toast("Stored in the vault"); reload(); }
  }));
}

async function editConn(c, ov, reload) {
  c = c || { auth_type: "API_KEY", verify_tls: true, timeout_seconds: 10, retries: 1, enabled: true, auth: {}, default_headers: {} };
  const au = c.auth || {};
  const r = await modal({ title: c.id ? `Edit ${c.name}` : "Add connection", wide: true, body: `<div class="form-grid">
      <label class="f">Name<input class="input" name="name" value="${esc(c.name || "")}" placeholder="e.g. T24 core banking (production)"></label>
      <label class="f">Base URL<span class="help">https://… (or mock://core for the built-in simulator)</span><input class="input mono" name="base_url" value="${esc(c.base_url || "")}"></label>
      <label class="f">Authentication<select class="input" name="auth_type">${options([["NONE", "None"], ["API_KEY", "API key header"], ["BEARER", "Bearer token"], ["BASIC", "Basic (user / password)"], ["OAUTH2_CC", "OAuth2 client credentials"], ["HMAC", "HMAC request signature"]], c.auth_type)}</select></label>
      <label class="f">Header name<span class="help">API key or signature header</span><input class="input" name="header" value="${esc(au.header || "")}" placeholder="X-API-Key"></label>
      <label class="f">Prefix<input class="input" name="prefix" value="${esc(au.prefix || "")}" placeholder="optional"></label>
      <label class="f">Username (basic)<input class="input" name="username" value="${esc(au.username || "")}"></label>
      <label class="f">Token URL (OAuth2)<input class="input mono" name="token_url" value="${esc(au.token_url || "")}"></label>
      <label class="f">Client id (OAuth2)<input class="input" name="client_id" value="${esc(au.client_id || "")}"></label>
      <label class="f">Scope (OAuth2)<input class="input" name="scope" value="${esc(au.scope || "")}"></label>
      <label class="f">Timestamp header (HMAC)<input class="input" name="timestamp_header" value="${esc(au.timestamp_header || "")}" placeholder="X-Timestamp"></label>
      <label class="f">CA certificate file<span class="help">bank CA bundle (PEM) on the server</span><input class="input mono" name="ca_file" value="${esc(c.ca_file || "")}"></label>
      <label class="f">Client certificate (mTLS)<input class="input mono" name="client_cert_file" value="${esc(c.client_cert_file || "")}"></label>
      <label class="f">Client key (mTLS)<input class="input mono" name="client_key_file" value="${esc(c.client_key_file || "")}"></label>
      <label class="f">Timeout (s)<input class="input" type="number" step="0.5" name="timeout_seconds" value="${esc(c.timeout_seconds)}"></label>
      <label class="f">Retries<span class="help">on network errors and HTTP 5xx, same idempotency key</span><input class="input" type="number" name="retries" value="${esc(c.retries)}"></label>
      <label class="f" style="grid-column:1/-1">Default headers<span class="help">one per line: name = value / template</span><textarea class="input mono" name="default_headers" rows="3">${esc(lines(c.default_headers))}</textarea></label></div>
    <div class="row" style="margin-top:10px;gap:20px"><label class="check"><input type="checkbox" name="verify_tls" ${c.verify_tls ? "checked" : ""}> Verify the server certificate</label>
      <label class="check"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""}> Enabled</label></div>
    <p class="tiny muted" style="margin-top:8px">Set the secret with the "Secret" button after saving.</p>`,
    actions: [{ label: "Cancel" }, { label: "Save", cls: "primary", onClick: (m) => {
      const v = (n) => m.querySelector(`[name=${n}]`);
      const auth = {}; ["header", "prefix", "username", "token_url", "client_id", "scope", "timestamp_header"].forEach((k) => { if (v(k).value.trim()) auth[k] = v(k).value.trim(); });
      const body = { name: v("name").value.trim(), base_url: v("base_url").value.trim(), auth_type: v("auth_type").value, auth, ca_file: v("ca_file").value.trim(),
        client_cert_file: v("client_cert_file").value.trim(), client_key_file: v("client_key_file").value.trim(), timeout_seconds: Number(v("timeout_seconds").value),
        retries: Number(v("retries").value), default_headers: parseLines(v("default_headers").value), verify_tls: v("verify_tls").checked, enabled: v("enabled").checked };
      return c.id ? api.put(`/core/connections/${c.id}`, body) : api.post("/core/connections", body);
    } }] });
  if (r) { toast("Connection saved"); reload(); }
}

// ------------------------------------------------------------------ purposes
async function purposesTab(box, ov, reload) {
  const rows = Object.values(ov.purposes);
  box.innerHTML = card("Purposes", table([
    { label: "Purpose", render: (p) => `<b>${esc(p.label)}</b> ${p.builtin ? "" : chip("CUSTOM", "info")}<div class="tiny muted"><span class="code">${esc(p.code)}</span> ${esc(p.description || "")}</div>` },
    { label: "Category", render: (p) => chip(p.category, "neutral") },
    { label: "Inputs", render: (p) => (p.inputs || []).map((i) => `<span class="code">${esc(i.name)}</span>`).join(" ") },
    { label: "Used automatically", render: (p) => (p.automatic ? chip("YES", "info") : "–") },
    { label: "Approval by default", render: (p) => (p.approval ? chip("FOUR EYES", "warn") : "–") },
    { label: "Active API", render: (p) => (p.active ? chip("ACTIVE", "ok") : p.apis ? chip("CONFIGURED", "neutral") : chip("NONE", "neutral")) },
    { label: "", render: (p) => `<div class="row nowrap"><button class="btn sm" data-ed="${esc(p.code)}">Edit</button>${p.builtin ? "" : `<button class="btn sm danger" data-de="${esc(p.code)}">Delete</button>`}</div>` },
  ], rows), { flush: true, sub: "What each operation is for. Built-in purposes are used by VeriShield's own flows (held payments, card freezes); add the bank's own purposes for anything else.",
    actions: '<button class="btn sm primary" id="add">Add purpose</button>' });
  box.querySelector("#add").onclick = () => editPurpose(null, ov, reload);
  box.querySelectorAll("[data-ed]").forEach((b) => (b.onclick = () => editPurpose(ov.purposes[b.dataset.ed], ov, reload)));
  box.querySelectorAll("[data-de]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Delete purpose", "Only possible when no operation uses it.", "Delete", "danger"))) return;
    try { await api.del(`/core/purposes/${b.dataset.de}`); toast("Deleted"); reload(); } catch (e) { errToast(e); }
  }));
}

async function editPurpose(p, ov, reload) {
  p = p || { category: "OTHER", inputs: [], approval: true };
  const r = await modal({ title: p.code ? `Purpose ${p.code}` : "Add purpose", wide: true, body: `<div class="form-grid">
      <label class="f">Code<input class="input mono" name="code" value="${esc(p.code || "")}" ${p.code ? "disabled" : ""} placeholder="NOTIFY_BRANCH"></label>
      <label class="f">Label<input class="input" name="label" value="${esc(p.label || "")}"></label>
      <label class="f">Category<select class="input" name="category">${options(ov.enums.categories.map((c) => [c, label(c)]), p.category)}</select></label>
      <label class="f" style="grid-column:1/-1">Description<input class="input" name="description" value="${esc(p.description || "")}"></label>
      <label class="f" style="grid-column:1/-1">Inputs<span class="help">one per line: name | label | required/optional | example</span><textarea class="input mono" name="inputs" rows="5">${esc((p.inputs || []).map((i) => [i.name, i.label, i.required ? "required" : "optional", i.example || ""].join(" | ")).join("\n"))}</textarea></label></div>
    <div class="row" style="margin-top:10px;gap:20px"><label class="check"><input type="checkbox" name="automatic" ${p.automatic ? "checked" : ""}> VeriShield may call it automatically</label>
      <label class="check"><input type="checkbox" name="approval" ${p.approval ? "checked" : ""}> New operations need approval by default</label></div>`,
    actions: [{ label: "Cancel" }, { label: "Save", cls: "primary", onClick: (m) => {
      const v = (n) => m.querySelector(`[name=${n}]`);
      const code = p.code || v("code").value.trim().toUpperCase();
      return api.put(`/core/purposes/${encodeURIComponent(code)}`, { label: v("label").value, category: v("category").value, description: v("description").value,
        automatic: v("automatic").checked, approval: v("approval").checked,
        inputs: v("inputs").value.split("\n").map((l) => l.split("|").map((x) => x.trim())).filter((x) => x[0]).map(([name, lab, req, ex]) => ({ name, label: lab || name, required: (req || "required").toLowerCase() !== "optional", example: ex || "" })) });
    } }] });
  if (r) { toast("Purpose saved"); reload(); }
}

// ------------------------------------------------------------------ request / approve / log
async function requestTab(box, ov, reload) {
  const d = await api.get("/core/operations");
  box.innerHTML = card("Request a core banking operation", table([
    { label: "Operation", render: (o) => `<b>${esc(o.label)}</b><div class="tiny muted">${esc(o.description || "")}</div>` },
    { label: "Category", render: (o) => chip(o.category, "neutral") }, { label: "Approval", render: (o) => (o.requires_approval ? chip("FOUR EYES", "warn") : "–") },
    { label: "", render: (o) => (o.allowed ? `<button class="btn sm primary" data-go="${esc(o.purpose)}">Request</button>` : '<span class="tiny muted">not for your role</span>') },
  ], d.operations, { empty: "No operation is enabled for this workspace - ask the IT Admin." }), { flush: true,
    sub: "The same operations are also offered on a transaction and on a customer profile ('Core banking action')." });
  box.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => openCoreOperation({ purpose: b.dataset.go }).then(() => reload())));
}

async function approvalsTab(box, ov, reload) {
  const d = await api.get("/core/calls?status=PENDING_APPROVAL");
  box.innerHTML = card("Waiting for approval", table([
    { label: "Requested", render: (c) => `${fmtRel(c.created_at)}<div class="tiny muted">${esc(c.requested_by)}</div>` },
    { label: "Operation", render: (c) => `<span class="code">${esc(c.purpose)}</span>` }, { label: "Target", render: (c) => `<span class="mono small">${esc(c.target_ref)}</span>` },
    { label: "Inputs", render: (c) => `<span class="small">${esc(Object.entries(c.inputs || {}).filter(([k]) => !["reason"].includes(k)).slice(0, 6).map(([k, v]) => `${k}: ${v}`).join(" · "))}</span>` },
    { label: "Reason", render: (c) => `<span class="small">${esc(c.reason)}</span>` },
    { label: "", render: (c) => `<div class="row nowrap"><button class="btn sm primary" data-a="approve" data-id="${c.id}">Approve & execute</button><button class="btn sm" data-a="reject" data-id="${c.id}">Reject</button></div>` },
  ], d.calls, { empty: "Nothing waiting." }), { flush: true, sub: "The approver must be a different person from the requester." });
  box.querySelectorAll("[data-a]").forEach((b) => (b.onclick = () => busy(b, async () => {
    try { const c = await api.post(`/core/calls/${b.dataset.id}/${b.dataset.a}`); toast(b.dataset.a === "approve" ? `Executed: ${c.status}${c.error ? " - " + c.error : ""}` : "Rejected", c.status === "FAILED" ? "error" : "ok", 7000); reload(); } catch (e) { errToast(e); }
  })));
}

async function logTab(box) {
  let status = "", purpose = "";
  const draw = async () => {
    const d = await api.get(`/core/calls?status=${status}&purpose=${purpose}`);
    box.innerHTML = card("Call log", table([
      { label: "Time", render: (c) => fmtDate(c.created_at) }, { label: "Operation", render: (c) => `<span class="code">${esc(c.purpose)}</span>` },
      { label: "Trigger", render: (c) => chip(c.trigger, "neutral") }, { label: "Target", render: (c) => `<span class="mono small">${esc(c.target_ref)}</span>` },
      { label: "Status", render: (c) => chip(c.status, STATUS_TONE[c.status] || "neutral") + (c.error ? `<div class="tiny muted">${esc(c.error).slice(0, 80)}</div>` : "") },
      { label: "HTTP", num: true, render: (c) => esc(c.http_status ?? "–") }, { label: "ms", num: true, render: (c) => (c.duration_ms ? fmtNum(c.duration_ms, 0) : "–") },
      { label: "Reference", render: (c) => `<span class="mono small">${esc((c.result || {}).reference || "")}</span>` },
      { label: "By", render: (c) => `${esc(c.requested_by)}${c.approved_by ? `<div class="tiny muted">approved ${esc(c.approved_by)}</div>` : ""}` },
    ], d.calls, { onRow: true, empty: "No calls yet." }), { flush: true,
      actions: `<select class="input" id="st" style="width:auto">${options([["", "All statuses"], ["SUCCESS", "Success"], ["FAILED", "Failed"], ["PENDING_APPROVAL", "Waiting for approval"], ["REJECTED", "Rejected"], ["DRY_RUN", "Dry runs"], ["NO_API", "No API configured"]], status)}</select>` });
    box.querySelector("#st").onchange = (e) => { status = e.target.value; draw(); };
    box.querySelectorAll("tr[data-row]").forEach((tr) => tr.addEventListener("click", () => {
      const c = d.calls[+tr.dataset.row];
      drawer(`<div class="dh"><h2>${esc(c.purpose)} · ${esc(c.id)}</h2><button class="x" data-close>×</button></div><div class="db">${callDetail(c)}</div>`);
    }));
  };
  await draw();
}

// ------------------------------------------------------------------ shared dialog (transaction / customer screens)
export async function openCoreOperation({ purpose = "", inputs = {} } = {}) {
  const d = await api.get("/core/operations");
  const ops = d.operations.filter((o) => o.allowed);
  if (!ops.length) { toast("No core banking operation is enabled for your role in this workspace", "warn"); return null; }
  let cur = ops.find((o) => o.purpose === purpose) || ops[0];
  const form = (o) => `${(o.inputs || []).map((i) => `<label class="f">${esc(i.label)}${i.required ? "" : ' <span class="help">optional</span>'}<input class="input" data-in="${esc(i.name)}" value="${esc(inputs[i.name] ?? "")}" placeholder="${esc(i.example || "")}"></label>`).join("")}
    <p class="tiny muted" style="grid-column:1/-1">Empty fields are filled from VeriShield's records (transaction, customer, card) where possible.</p>`;
  const r = await modal({ title: "Core banking action", wide: true, body: `
      <label class="f">Operation<select class="input" id="op">${options(ops.map((o) => [o.purpose, `${o.label}${o.requires_approval ? " – needs approval" : ""}`]), cur.purpose)}</select></label>
      <p class="small muted" id="desc" style="margin:6px 0 10px">${esc(cur.description || "")}</p>
      <div class="form-grid" id="fields">${form(cur)}</div>
      <label class="f" style="margin-top:10px">Reason<textarea class="input" id="reason" placeholder="Why is this needed? (audited, sent to the core when the API uses it)"></textarea></label>`,
    onOpen: (m) => { m.querySelector("#op").onchange = (e) => { cur = ops.find((o) => o.purpose === e.target.value); m.querySelector("#fields").innerHTML = form(cur); m.querySelector("#desc").textContent = cur.description || ""; }; },
    actions: [{ label: "Cancel" }, { label: "Submit", cls: "primary", onClick: (m) => {
      const vals = {}; m.querySelectorAll("[data-in]").forEach((i) => { if (i.value.trim()) vals[i.dataset.in] = i.value.trim(); });
      return api.post("/core/operations", { purpose: cur.purpose, api_id: cur.api_id, inputs: vals, reason: m.querySelector("#reason").value });
    } }] });
  if (r) toast(r.status === "PENDING_APPROVAL" ? "Sent for approval (Core Banking APIs › Approvals)" : r.status === "SUCCESS" ? `Done – core reference ${(r.result || {}).reference || "-"}` : `${r.status}: ${r.error || ""}`, r.status === "FAILED" ? "error" : "ok", 8000);
  return r;
}

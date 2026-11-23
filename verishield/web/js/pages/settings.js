// Users & settings (IT Admin): users, institution & security policy, branding, API clients, Demo Mode.
import { api } from "../api.js";
import { refreshMe } from "../app.js";
import { S, busy, card, chip, confirmDialog, errToast, esc, fmtDate, fmtRel, kv, label, loading, modal, options,
  table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el) {
  const tabs = [];
  if (can("users.manage")) tabs.push(["users", "Users"]);
  if (can("tenant.manage")) tabs.push(["institution", "Institution & security"], ["branding", "Report branding"]);
  if (can("api_clients.manage")) tabs.push(["api", "API clients"]);
  if (can("demo.manage")) tabs.push(["demo", "Demo Mode"]);
  let cur = tabs[0][0];
  const draw = () => {
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    ({ users, institution, branding, api: apiClients, demo })[cur](el.querySelector("#tab")).catch(errToast);
  };
  draw();
}

function secretOnce(title, secret, note) {
  return modal({ title, body: `<div class="notice warn" style="margin-bottom:12px">${esc(note)}</div>
    <div class="row"><input class="input mono" readonly value="${esc(secret)}" id="sec"><button class="btn" id="cp">Copy</button></div>`,
    actions: [{ label: "I have stored it securely", cls: "primary", value: true }],
    onOpen: (m) => { m.querySelector("#cp").onclick = () => { navigator.clipboard && navigator.clipboard.writeText(secret).then(() => toast("Copied")); m.querySelector("#sec").select(); }; } });
}

async function users(box) {
  const d = await api.get("/users");
  box.innerHTML = card("Users", table([
    { label: "Username", render: (u) => `<b class="mono">${esc(u.username)}</b>${u.is_bootstrap ? ' <span class="tiny muted">bootstrap</span>' : ""}` },
    { label: "Name", render: (u) => esc(u.name) }, { label: "E-mail", render: (u) => `${esc(u.email)} ${u.email && !u.email_verified ? '<span class="tiny muted">(unverified)</span>' : ""}` },
    { label: "Role", render: (u) => `${esc(u.role_label)}${u.extra_permissions.length ? `<div class="tiny muted">+ ${esc(u.extra_permissions.join(", "))}</div>` : ""}` },
    { label: "Status", render: (u) => chip(u.status) + (u.must_change_password ? ' <span class="tiny muted">password change pending</span>' : "") },
    { label: "MFA", render: (u) => u.mfa_enabled ? chip("ON", "ok") : chip("OFF", "neutral") },
    { label: "Last sign-in", render: (u) => u.last_login_at ? fmtRel(u.last_login_at) : "never" },
    { label: "", render: (u) => `<div class="row nowrap"><button class="btn sm" data-edit="${u.id}">Edit</button>
      <select class="input" data-act="${u.id}" style="width:auto;padding:4px 6px;font-size:12px"><option value="">Actions…</option>
      ${u.status === "DISABLED" ? '<option value="enable">Enable</option>' : '<option value="disable">Disable</option>'}${u.status === "LOCKED" ? '<option value="unlock">Unlock</option>' : ""}
      <option value="reset-password">Reset password</option>${u.mfa_enabled ? '<option value="reset-mfa">Reset MFA</option>' : ""}</select></div>` },
  ], d.users), { flush: true, actions: '<button class="btn primary" id="add">Create user</button>',
    sub: "Usernames are identifiers, not security factors. Temporary passwords are single-use and expire." });
  box.querySelector("#add").onclick = async () => {
    const r = await modal({ title: "Create user", body: `<div class="form-grid">
        <label class="f">Username<span class="help">unique 4–5 digits</span><input class="input mono" name="username" pattern="\\d{4,5}" required></label>
        <label class="f">Role<select class="input" name="role">${options(d.roles.map((x) => [x.id, x.label]), "AML_EMPLOYEE")}</select></label>
        <label class="f">First name<input class="input" name="first_name" required></label><label class="f">Last name<input class="input" name="last_name" required></label>
        <label class="f">E-mail<input class="input" type="email" name="email" required></label>
        <label class="f">Temporary password<span class="help">leave empty to generate a human-friendly one</span><input class="input" name="temporary_password" autocomplete="off"></label></div>`,
      actions: [{ label: "Cancel" }, { label: "Create user", cls: "primary", onClick: (m) => { const v = {}; m.querySelectorAll("[name]").forEach((i) => (v[i.name] = i.value.trim()));
        if (!v.temporary_password) delete v.temporary_password; return api.post("/users", v); } }] });
    if (r) { await secretOnce(`Temporary password for ${r.user.username}`, r.temporary_password, r.note); users(box); }
  };
  box.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = async () => {
    const u = d.users.find((x) => x.id === b.dataset.edit);
    const grant = d.grantable[u.role] || [];
    const r = await modal({ title: `Edit ${u.username}`, body: `<div class="form-grid">
        <label class="f">First name<input class="input" name="first_name" value="${esc(u.first_name)}"></label><label class="f">Last name<input class="input" name="last_name" value="${esc(u.last_name)}"></label>
        <label class="f">E-mail<input class="input" name="email" value="${esc(u.email)}"></label>
        <label class="f">Role<select class="input" name="role" ${u.id === S.me.user.id ? "disabled" : ""}>${options(d.roles.map((x) => [x.id, x.label]), u.role)}</select></label></div>
        ${grant.length ? `<h3 style="margin:14px 0 6px">Explicit additional permissions (audited)</h3>${grant.map((g) => `<label class="check"><input type="checkbox" data-grant="${g}" ${u.extra_permissions.includes(g) ? "checked" : ""}> ${esc(g)}</label>`).join("<br>")}` : ""}`,
      actions: [{ label: "Cancel" }, { label: "Save", cls: "primary", onClick: (m) => api.patch(`/users/${u.id}`, { first_name: m.querySelector("[name=first_name]").value,
        last_name: m.querySelector("[name=last_name]").value, email: m.querySelector("[name=email]").value, role: m.querySelector("[name=role]").value,
        extra_permissions: grant.length ? [...m.querySelectorAll("[data-grant]")].filter((c) => c.checked).map((c) => c.dataset.grant) : undefined }) }] });
    if (r) { toast("User updated"); users(box); }
  }));
  box.querySelectorAll("[data-act]").forEach((s) => (s.onchange = async () => {
    const action = s.value; s.value = "";
    if (!action) return;
    const u = d.users.find((x) => x.id === s.dataset.act);
    if (!(await confirmDialog(`${label(action)} – ${u.username}`, `Apply “${label(action)}” to ${u.name}? This is audited.`, label(action), action === "disable" ? "danger" : "primary"))) return;
    try {
      const r = await api.post(`/users/${u.id}/${action}`);
      if (r.temporary_password) await secretOnce(`New temporary password for ${u.username}`, r.temporary_password, r.note);
      else toast("Done");
      users(box);
    } catch (e) { errToast(e); }
  }));
}

async function institution(box) {
  const { tenant: t } = await api.get("/tenant");
  const sec = t.settings.security || {}, ret = t.settings.retention || {}, gov = t.settings.governance || {}, lim = t.settings.role_limits || {};
  box.innerHTML = `<form id="tf" class="stack">
    ${card("Institution identity", `<div class="form-grid">
      <label class="f">Institution code<input class="input mono" value="${esc(t.id)}" disabled></label>
      <label class="f">Legal name<input class="input" name="legal_name" value="${esc(t.legal_name)}"></label>
      <label class="f">Display name<input class="input" name="display_name" value="${esc(t.display_name)}"></label>
      <label class="f">Timezone<input class="input" name="timezone" value="${esc(t.timezone)}"></label>
      <label class="f">Base currency<input class="input" name="base_currency" value="${esc(t.base_currency)}" maxlength="3"></label>
      <label class="f">Locale<input class="input" name="locale" value="${esc(t.locale)}"></label>
      <label class="f">Languages<input class="input" name="languages" value="${esc((t.languages || []).join(", "))}"></label>
      <label class="f">Region<input class="input" name="region" value="${esc(t.region)}"></label></div>`)}
    <div class="grid g2">
      ${card("Security policy", `<div class="form-grid">
        <label class="check"><input type="checkbox" name="s.mfa_required" ${sec.mfa_required ? "checked" : ""}> Require MFA (TOTP) for all roles</label>
        <label class="check"><input type="checkbox" name="require_email_verification" ${t.settings.require_email_verification ? "checked" : ""}> Require verified e-mail</label>
        <label class="f">Minimum password length<input class="input" type="number" min="8" name="s.password_min_length" value="${esc(sec.password_min_length)}"></label>
        <label class="check"><input type="checkbox" name="s.password_require_symbol" ${sec.password_require_symbol ? "checked" : ""}> Require a symbol</label>
        <label class="f">Temporary password validity (hours)<input class="input" type="number" name="s.temp_password_hours" value="${esc(sec.temp_password_hours)}"></label>
        <label class="f">Session idle timeout (minutes)<input class="input" type="number" name="s.session_idle_minutes" value="${esc(sec.session_idle_minutes)}"></label>
        <label class="f">Session maximum (hours)<input class="input" type="number" name="s.session_absolute_hours" value="${esc(sec.session_absolute_hours)}"></label>
        <label class="f">Lock after failed sign-ins<input class="input" type="number" name="s.lockout_threshold" value="${esc(sec.lockout_threshold)}"></label></div>`)}
      ${card("Governance & retention", `<div class="form-grid">
        <label class="check"><input type="checkbox" name="g.maker_checker" ${gov.maker_checker ? "checked" : ""}> Maker-checker for policy changes</label>
        <label class="f">Active AML Managers allowed<input class="input" type="number" min="1" name="l.AML_MANAGER" value="${esc(lim.AML_MANAGER ?? "")}"></label>
        <label class="f">Active Chiefs allowed<input class="input" type="number" min="1" name="l.CHIEF_AML" value="${esc(lim.CHIEF_AML ?? "")}"></label>
        <label class="f">Transactions retention (days)<input class="input" type="number" name="r.transactions_days" value="${esc(ret.transactions_days)}"></label>
        <label class="f">Cases retention (days)<input class="input" type="number" name="r.cases_days" value="${esc(ret.cases_days)}"></label>
        <label class="f">Audit retention (days)<input class="input" type="number" name="r.audit_days" value="${esc(ret.audit_days)}"></label></div>
        <p class="tiny muted" style="margin-top:8px">Retention periods must be confirmed by Legal/Compliance (FATF R.11 suggests at least five years for transaction records).</p>`)}
    </div><div><button class="btn primary">Save institution settings</button></div></form>`;
  const f = box.querySelector("#tf");
  f.onsubmit = (e) => { e.preventDefault();
    const v = (n) => f.querySelector(`[name="${n}"]`);
    const sec2 = {}, ret2 = {}, gov2 = {}, lim2 = {};
    f.querySelectorAll("[name^='s.']").forEach((i) => (sec2[i.name.slice(2)] = i.type === "checkbox" ? i.checked : Number(i.value)));
    f.querySelectorAll("[name^='r.']").forEach((i) => (ret2[i.name.slice(2)] = Number(i.value)));
    f.querySelectorAll("[name^='g.']").forEach((i) => (gov2[i.name.slice(2)] = i.checked));
    f.querySelectorAll("[name^='l.']").forEach((i) => (lim2[i.name.slice(2)] = i.value ? Number(i.value) : null));
    busy(f.querySelector("button"), async () => { try {
      await api.put("/tenant", { legal_name: v("legal_name").value, display_name: v("display_name").value, timezone: v("timezone").value,
        base_currency: v("base_currency").value, locale: v("locale").value, region: v("region").value, languages: v("languages").value.split(",").map((x) => x.trim()).filter(Boolean),
        settings: { security: sec2, retention: ret2, governance: gov2, role_limits: lim2, require_email_verification: v("require_email_verification").checked } });
      toast("Institution settings saved"); } catch (err) { errToast(err); } });
  };
}

async function branding(box) {
  const { branding: b, editable_fields: ed } = await api.get("/branding");
  const field = (k, l, type = "text") => `<label class="f">${esc(l)}<input class="input" type="${type}" name="${k}" value="${esc(b[k] ?? "")}" ${ed.includes(k) ? "" : "disabled"}></label>`;
  box.innerHTML = `<div class="grid g-21"><form id="bf" class="stack">
    ${card("Institution identity on reports", `<div class="form-grid">${field("display_name", "Display name")}${field("report_title_prefix", "Report title prefix")}${field("numbering_prefix", "Report numbering prefix")}
      ${field("primary_color", "Primary colour", "color")}${field("accent_color", "Accent colour", "color")}${field("secondary_color", "Secondary colour", "color")}
      ${field("locale", "Locale")}${field("date_format", "Date format (strftime)")}${field("page_number_format", "Page number format")}</div>`)}
    ${card("Header, footer & notices", `<div class="form-grid">${field("header_text", "Header text")}${field("footer_text", "Footer text")}${field("confidentiality_notice", "Confidentiality notice")}
      ${field("watermark", "Watermark (e.g. CONFIDENTIAL, DRAFT)")}${field("prepared_by_label", "Prepared-by label")}${field("reviewed_by_label", "Reviewed-by label")}${field("approved_by_label", "Approved-by label")}
      <label class="check"><input type="checkbox" name="signature_blocks" ${b.signature_blocks ? "checked" : ""}> Sign-off blocks</label></div>
      <label class="f" style="margin-top:10px">Mandatory disclaimer<textarea class="input" name="disclaimer">${esc(b.disclaimer)}</textarea></label>`)}
    <div><button class="btn primary">Save branding</button></div></form>
    <div class="stack">${card("Logo", `${S.me.tenant.logo || b.logo_key ? `<img src="/api/v1/public/logo/${esc(S.me.tenant.id)}?t=${Date.now()}" alt="logo" style="max-width:100%;max-height:90px;background:${esc(b.primary_color)};padding:10px;border-radius:8px">` : '<p class="muted small">No logo uploaded.</p>'}
      <form id="lf" class="stack" style="gap:8px;margin-top:10px"><input class="input" type="file" name="file" accept="image/png,image/jpeg" required><button class="btn">Upload PNG/JPEG</button></form>`)}
      ${card("Preview", `<div style="border:1px solid var(--line);border-radius:8px;overflow:hidden"><div id="pv" style="background:${esc(b.primary_color)};color:#fff;padding:12px 14px;border-bottom:3px solid ${esc(b.accent_color)}"><b>${esc(b.display_name)}</b><div class="tiny" style="opacity:.8">${esc(b.header_text)}</div></div>
        <div style="padding:14px;font-size:12px"><b style="color:${esc(b.primary_color)}">${esc(b.report_title_prefix)} Executive Fraud &amp; AML Overview</b><p class="muted">Report ${esc(b.numbering_prefix)}-2026-00042</p></div>
        <div class="tiny muted" style="padding:8px 14px;border-top:1px solid var(--line)">${esc(b.confidentiality_notice)}<br>${esc(b.footer_text)}</div></div>
        <p class="tiny muted" style="margin-top:8px">Generate any report to see the full branded PDF. AML report wording is maintained by the AML Manager (Reports page).</p>`)}</div></div>`;
  const f = box.querySelector("#bf");
  f.onsubmit = (e) => { e.preventDefault();
    const body = {};
    f.querySelectorAll("[name]").forEach((i) => { if (!i.disabled && ed.includes(i.name)) body[i.name] = i.type === "checkbox" ? i.checked : i.value; });
    busy(f.querySelector("button"), async () => { try { await api.put("/branding", body); toast("Branding saved"); branding(box); } catch (err) { errToast(err); } });
  };
  const lf = box.querySelector("#lf");
  lf.onsubmit = (e) => { e.preventDefault(); const fd = new FormData(); fd.append("file", lf.file.files[0]);
    busy(lf.querySelector("button"), async () => { try { await api.upload("/branding/logo", fd); toast("Logo uploaded"); await refreshMe(); } catch (err) { errToast(err); } }); };
}

async function apiClients(box) {
  const d = await api.get("/api-clients");
  box.innerHTML = card("API clients", table([
    { label: "Name", render: (c) => `<b>${esc(c.name)}</b>` }, { label: "Key prefix", render: (c) => `<span class="mono">${esc(c.key_prefix)}…</span>` },
    { label: "Workspace", render: (c) => chip(c.space, c.space === "DEMO" ? "warn" : "ok") }, { label: "Scopes", render: (c) => c.scopes.map((s) => `<span class="code">${esc(s)}</span>`).join(" ") },
    { label: "Rate limit", render: (c) => `${c.rate_limit_per_minute}/min` }, { label: "Status", render: (c) => c.active ? chip("ACTIVE", "ok") : chip("REVOKED", "crit") },
    { label: "Last used", render: (c) => c.last_used_at ? fmtRel(c.last_used_at) : "never" }, { label: "Created", render: (c) => fmtDate(c.created_at) },
    { label: "", render: (c) => c.active ? `<button class="btn sm danger" data-rev="${esc(c.id)}">Revoke</button>` : "" },
  ], d.clients, { empty: "No API clients yet." }), { flush: true, actions: '<button class="btn primary" id="add">Create API client</button>',
    sub: `New keys are bound to the current ${S.me.tenant.demo_mode ? "Demo" : "Live"} workspace` });
  box.querySelector("#add").onclick = async () => {
    const r = await modal({ title: "Create API client", body: `<label class="f">Name<input class="input" name="name" placeholder="e.g. BOP payment switch" required></label>
      <h3 style="margin:14px 0 6px">Scopes</h3>${d.scopes.map((s) => `<label class="check"><input type="checkbox" data-scope="${s}" ${["decisions:write", "events:write", "execution:write"].includes(s) ? "checked" : ""}> <span class="code">${esc(s)}</span></label>`).join("<br>")}
      <label class="f" style="margin-top:12px">Rate limit (requests/minute)<input class="input" type="number" name="rl" value="600"></label>
      <p class="tiny muted" style="margin-top:8px">For the optional MCP server use <span class="code">read:operations</span>, <span class="code">read:aml</span>, <span class="code">reports:generate</span> and <span class="code">policies:simulate</span> only.</p>`,
      actions: [{ label: "Cancel" }, { label: "Create", cls: "primary", onClick: (m) => api.post("/api-clients", { name: m.querySelector("[name=name]").value,
        scopes: [...m.querySelectorAll("[data-scope]")].filter((c) => c.checked).map((c) => c.dataset.scope), rate_limit_per_minute: Number(m.querySelector("[name=rl]").value) }) }] });
    if (r) { await secretOnce(`API key for ${r.client.name}`, r.api_key, r.note); apiClients(box); }
  };
  box.querySelectorAll("[data-rev]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Revoke API client", "Calls with this key will be rejected immediately.", "Revoke", "danger"))) return;
    try { await api.del(`/api-clients/${b.dataset.rev}`); toast("Revoked"); apiClients(box); } catch (e) { errToast(e); }
  }));
}

async function demo(box) {
  const d = await api.get("/demo");
  box.innerHTML = `<div class="grid g2">
    ${card("Demo Mode", `${kv([["Status", d.demo_mode ? chip("ENABLED", "warn") : chip("DISABLED", "neutral")], ["Synthetic dataset", d.has_demo_data ? "present" : "not generated"]])}
      <p class="small" style="margin-top:12px">Demo Mode switches every user's workspace to an isolated synthetic namespace: separate customers, transactions, models, connectors, API keys and reports. A persistent banner and report watermark are shown; payment execution is simulated and production connectors cannot be activated.</p>
      <div class="row" style="margin-top:12px"><button class="btn ${d.demo_mode ? "" : "primary"}" id="toggle">${d.demo_mode ? "Switch to live workspace" : "Enable Demo Mode"}</button></div>`)}
    ${card("Reset synthetic data", `<p class="small">Deletes every Demo-space record and artifact (customers, transactions, decisions, alerts, cases, evidence, models, demo connectors, demo API keys, demo reports) and regenerates a fresh dataset. <b>Live data is never touched.</b> The reset is audited.</p>
      <label class="f" style="margin-top:10px">Type RESET to confirm<input class="input mono" id="confirm"></label>
      <div class="row" style="margin-top:10px"><button class="btn danger" id="reset">Reset demo data</button></div>`)}
  </div>
  <div style="margin-top:16px">${card("Scenarios available", `<ul class="small" style="padding-left:18px">${Object.entries(d.scenarios).map(([k, v]) => `<li><b>${esc(label(k))}</b> – ${esc(v)}</li>`).join("")}</ul><p class="small muted">Inject them from the Decision Console while Demo Mode is enabled.</p>`)}</div>`;
  box.querySelector("#toggle").onclick = (e) => busy(e.currentTarget, async () => {
    try { await api.post("/demo/mode", { enabled: !d.demo_mode }); toast(d.demo_mode ? "Live workspace active" : "Demo Mode enabled"); await refreshMe(); } catch (err) { errToast(err); }
  });
  box.querySelector("#reset").onclick = (e) => busy(e.currentTarget, async () => {
    try { const r = await api.post("/demo/reset", { confirm: box.querySelector("#confirm").value });
      toast(`Demo data regenerated (${r.seed ? r.seed.scored : 0} decisions)`); demo(box); } catch (err) { errToast(err); }
  });
}

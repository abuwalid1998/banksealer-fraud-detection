// E-mail, SMS and mobile-app push (IT Admin): the bank's SMTP relay, SMS gateway and app backend, each ON / OFF.
import { api } from "../api.js";
import { busy, card, chip, errToast, esc, fmtRel, loading, modal, options, statusDot, table, toast, toggle } from "../ui.js";

export async function render(el) {
  el.innerHTML = loading();
  const d = await api.get("/notifications");
  const e = d.config.email, s = d.config.sms, ev = d.config.events, pu = d.config.push || {};
  el.innerHTML = `<div class="grid g2">
    <form id="ef">${card("E-mail (SMTP server)", `
      <div class="row" style="margin-bottom:14px">${toggle("enabled", e.enabled, "E-mail notifications ON")}<span class="spacer"></span>${statusDot(e.ready, e.ready ? "Ready" : "Off / incomplete")}</div>
      <div class="form-grid">
        <label class="f">SMTP server<input class="input" name="host" value="${esc(e.host)}" placeholder="smtp.bop.local"></label>
        <label class="f">Port<input class="input" type="number" name="port" value="${esc(e.port)}"></label>
        <label class="f">Security<select class="input" name="security">${options(d.security_options.map((x) => [x, x === "STARTTLS" ? "STARTTLS (587)" : x === "SSL" ? "SSL / TLS (465)" : "None (internal relay)"]), e.security)}</select></label>
        <label class="f">Username<span class="help">empty = anonymous relay</span><input class="input" name="username" value="${esc(e.username)}" autocomplete="off"></label>
        <label class="f">Sender address<input class="input" name="from_address" value="${esc(e.from_address)}" placeholder="verishield@bop.ps"></label>
        <label class="f">Sender name<input class="input" name="from_name" value="${esc(e.from_name)}"></label>
        <label class="f">Timeout (s)<input class="input" type="number" name="timeout" value="${esc(e.timeout)}"></label>
        <label class="check" style="align-self:end"><input type="checkbox" name="verify_tls" ${e.verify_tls ? "checked" : ""}> Verify the server certificate</label>
      </div>
      <div class="row" style="margin-top:12px"><span class="small">Password: ${e.secret_configured ? chip("STORED IN VAULT", "ok") : chip("NOT SET", "neutral")}</span>
        <button class="btn sm" type="button" data-secret="email">${e.secret_configured ? "Replace" : "Set"} password</button></div>`,
      { actions: '<button class="btn sm" type="button" data-test="email">Send test</button><button class="btn sm primary">Save</button>' })}</form>
    <form id="sf">${card("SMS gateway (HTTP API)", `
      <div class="row" style="margin-bottom:14px">${toggle("enabled", s.enabled, "SMS notifications ON")}<span class="spacer"></span>${statusDot(s.ready, s.ready ? "Ready" : "Off / incomplete")}</div>
      <div class="form-grid">
        <label class="f" style="grid-column:1/-1">Gateway URL<span class="help">placeholders: {to} {to_digits} {text} {sender} {secret}</span><input class="input mono" name="url" value="${esc(s.url)}" placeholder="https://sms.operator.ps/api/send"></label>
        <label class="f">Method<select class="input" name="method">${options([["POST_JSON", "POST – JSON body"], ["POST_FORM", "POST – form fields"], ["GET", "GET – parameters in URL"]], s.method)}</select></label>
        <label class="f">Sender ID<input class="input" name="sender_id" value="${esc(s.sender_id)}" placeholder="BOP"></label>
        <label class="f">Default country code<input class="input" name="country_code" value="${esc(s.country_code)}"></label>
        <label class="f">Auth header<input class="input" name="auth_header" value="${esc(s.auth_header)}"></label>
        <label class="f">Auth prefix<input class="input" name="auth_prefix" value="${esc(s.auth_prefix)}" placeholder="Bearer"></label>
        <label class="f">Success text (optional)<input class="input" name="success_contains" value="${esc(s.success_contains)}" placeholder='e.g. "status":"OK"'></label>
        <label class="f" style="grid-column:1/-1">Body template<textarea class="input mono" name="body_template" rows="2">${esc(s.body_template)}</textarea></label>
        <label class="check"><input type="checkbox" name="verify_tls" ${s.verify_tls ? "checked" : ""}> Verify the gateway certificate</label>
      </div>
      <div class="row" style="margin-top:12px"><span class="small">API token: ${s.secret_configured ? chip("STORED IN VAULT", "ok") : chip("NOT SET", "neutral")}</span>
        <button class="btn sm" type="button" data-secret="sms">${s.secret_configured ? "Replace" : "Set"} token</button></div>`,
      { actions: '<button class="btn sm" type="button" data-test="sms">Send test</button><button class="btn sm primary">Save</button>' })}</form>
  </div>
  <div style="margin-top:16px"><form id="pf">${card("Mobile app push (customer payment confirmations)", `
      <div class="row" style="margin-bottom:14px">${toggle("enabled", pu.enabled, "In-app confirmations ON")}<span class="spacer"></span>${statusDot(pu.ready, pu.ready ? "Ready" : "Off / incomplete")}</div>
      <div class="form-grid">
        <label class="f" style="grid-column:1/-1">Mobile-app backend URL<span class="help">VeriShield POSTs a signed JSON request (X-VeriShield-Signature: HMAC-SHA256 of timestamp.body) that the app shows to the customer</span><input class="input mono" name="url" value="${esc(pu.url || "")}" placeholder="https://mobile-backend.bop.local/verishield/confirmations"></label>
        <label class="f">Timeout (s)<input class="input" type="number" name="timeout" value="${esc(pu.timeout || 10)}"></label>
        <label class="check" style="align-self:end"><input type="checkbox" name="verify_tls" ${pu.verify_tls !== false ? "checked" : ""}> Verify the server certificate</label>
      </div>
      <p class="small muted" style="margin-top:10px">The app answers with <span class="code">POST /api/v1/confirmations/{id}/respond {"answer": "YES" | "NO"}</span> using an API key with the <span class="code">confirmations:respond</span> scope (and <span class="code">cards:control</span> to let customers freeze their cards). Without an answer the SMS follows after the fallback time of the policy.</p>
      <div class="row" style="margin-top:12px"><span class="small">Signing secret: ${pu.secret_configured ? chip("STORED IN VAULT", "ok") : chip("NOT SET", "neutral")}</span>
        <button class="btn sm" type="button" data-secret="push">${pu.secret_configured ? "Replace" : "Set"} secret</button></div>`,
      { actions: '<button class="btn sm" type="button" data-test="push">Send test</button><button class="btn sm primary">Save</button>' })}</form></div>
  <div class="grid g-12" style="margin-top:16px">
    <form id="vf">${card("What is sent", `${Object.entries(d.events).map(([k, txt]) => `<div style="margin-bottom:10px">${toggle(k, ev[k], txt)}</div>`).join("")}
      <p class="tiny muted">Codes and passwords are never stored in the outbox. Forgotten-password codes need the channel ON and an e-mail address / mobile number on the user's profile.</p>`,
      { actions: '<button class="btn sm primary">Save</button>' })}</form>
    ${card("Outbox", table([{ label: "Time", render: (r) => fmtRel(r.created_at) }, { label: "Channel", render: (r) => chip(r.channel, "neutral") },
      { label: "Event", render: (r) => esc(r.event) }, { label: "Recipient", render: (r) => `<span class="small">${esc(r.recipient)}</span>` },
      { label: "Subject / text", render: (r) => `<span class="small">${esc(r.subject || r.preview)}</span>` },
      { label: "Status", render: (r) => chip(r.status, r.status === "SENT" ? "ok" : r.status === "FAILED" ? "crit" : "neutral") + (r.error ? `<div class="tiny muted">${esc(r.error)}</div>` : "") }],
      d.outbox, { empty: "Nothing sent yet." }), { flush: true, sub: "last 60 messages" })}
  </div>`;
  const collect = (form) => {
    const out = {};
    form.querySelectorAll("[name]").forEach((i) => { out[i.name] = i.type === "checkbox" ? i.checked : i.type === "number" ? (i.value === "" ? null : Number(i.value)) : i.value.trim(); });
    return out;
  };
  const save = (form, key) => form.addEventListener("submit", (e2) => {
    e2.preventDefault();
    busy(form.querySelector("button.primary"), async () => {
      try { await api.put("/notifications", { [key]: collect(form) }); toast("Saved"); render(el); } catch (err) { errToast(err); }
    });
  });
  save(el.querySelector("#ef"), "email");
  save(el.querySelector("#sf"), "sms");
  save(el.querySelector("#pf"), "push");
  save(el.querySelector("#vf"), "events");
  el.querySelectorAll("[data-secret]").forEach((b) => (b.onclick = async () => {
    const ch = b.dataset.secret;
    const r = await modal({ title: ch === "email" ? "SMTP password" : ch === "push" ? "Push signing secret" : "SMS gateway token", body: `<label class="f">${ch === "email" ? "Password" : ch === "push" ? "Shared HMAC secret" : "Token / API key"}<input class="input" type="password" name="v" autocomplete="new-password"></label><p class="tiny muted" style="margin-top:8px">Stored encrypted in the vault; it is never shown again.</p>`,
      actions: [{ label: "Cancel" }, { label: "Store", cls: "primary", onClick: (m) => api.post("/notifications/secret", { channel: ch, value: m.querySelector("[name=v]").value }) }] });
    if (r) { toast("Stored in the vault"); render(el); }
  }));
  el.querySelectorAll("[data-test]").forEach((b) => (b.onclick = async () => {
    const ch = b.dataset.test;
    const r = await modal({ title: `Send a test ${ch === "sms" ? "SMS" : ch === "push" ? "app notification" : "e-mail"}`, body: `<label class="f">${ch === "sms" ? "Mobile number" : ch === "push" ? "Customer reference (test)" : "E-mail address"}<input class="input" name="to"></label><p class="tiny muted" style="margin-top:8px">Save the settings first – the test uses the stored configuration.</p>`,
      actions: [{ label: "Cancel" }, { label: "Send", cls: "primary", onClick: (m) => api.post("/notifications/test", { channel: ch, to: m.querySelector("[name=to]").value }) }] });
    if (r) { toast(r.ok ? "Sent successfully" : `Not sent: ${r.detail}`, r.ok ? "ok" : "error", 8000); render(el); }
  }));
}

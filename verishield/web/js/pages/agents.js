// AI agents (MCP): switch access ON / OFF, choose the tools, issue access keys and see what agents did.
import { api } from "../api.js";
import { busy, card, chip, confirmDialog, errToast, esc, fmtRel, kv, loading, modal, options, statusDot, table, toast, toggle } from "../ui.js";

export async function render(el) {
  el.innerHTML = loading();
  const d = await api.get("/mcp/settings");
  const c = d.config;
  const pre = (obj) => `<div class="snippet"><pre class="box">${esc(typeof obj === "string" ? obj : JSON.stringify(obj, null, 2))}</pre><button class="btn sm" type="button" data-copy="${esc(typeof obj === "string" ? obj : JSON.stringify(obj, null, 2))}">Copy</button></div>`;
  el.innerHTML = `<form id="mf">${card("AI-agent access (Model Context Protocol)", `
      <div class="row" style="gap:28px;margin-bottom:14px">${toggle("enabled", c.enabled, "Allow AI agents")}${toggle("http_enabled", c.http_enabled, "Built-in HTTP endpoint")}
        <label class="f" style="flex-direction:row;align-items:center;gap:8px">Max tool calls / minute<input class="input" type="number" name="max_calls_per_minute" value="${esc(c.max_calls_per_minute)}" style="width:90px"></label>
        <span class="spacer"></span>${statusDot(c.enabled, c.enabled ? "Agents can connect" : "Switched off")}</div>
      <p class="small">Agents such as Claude connect through MCP and can only call the read-oriented tools enabled below, with the scopes of their access key. There is no SQL access, no secret retrieval and no payment block / release tool. Every call is written to the audit trail.</p>
      <h3 style="margin:14px 0 8px">Tools agents may use</h3>
      <div class="grid g2" style="gap:6px 18px">${d.tools.map((t) => `<label class="check" style="align-items:flex-start"><input type="checkbox" data-tool="${esc(t.name)}" ${c.tools.includes(t.name) ? "checked" : ""}>
        <span><span class="code">${esc(t.name)}</span> ${t.method === "POST" ? chip("ACTION", "warn") : ""}<br><span class="tiny muted">${esc(t.description)}</span></span></label>`).join("")}</div>
      <label class="f" style="margin-top:12px">Notes (purpose, approvals)<input class="input" name="notes" value="${esc(c.notes)}"></label>`,
    { actions: '<button class="btn primary sm">Save</button>' })}</form>
  <div class="grid g2" style="margin-top:16px">
    ${card("Connect an agent", `${kv([["HTTP endpoint", `<span class="mono">${esc(d.endpoint)}</span>`], ["Authentication", 'header <span class="code">X-API-Key: &lt;MCP access key&gt;</span>']])}
      <h3 style="margin:14px 0 6px">Claude Code</h3>${pre(d.snippets.claude_code)}
      <h3 style="margin:14px 0 6px">Claude Desktop / other MCP clients (HTTP)</h3>${pre(d.snippets.http)}
      <h3 style="margin:14px 0 6px">Local stdio bridge (older clients)</h3>${pre(d.snippets.stdio)}`)}
    ${card("Access keys", table([{ label: "Name", render: (k) => `<b>${esc(k.name)}</b>` }, { label: "Workspace", render: (k) => chip(k.space, k.space === "DEMO" ? "warn" : "ok") },
      { label: "Prefix", render: (k) => `<span class="mono">${esc(k.key_prefix)}…</span>` }, { label: "Status", render: (k) => k.active ? chip("ACTIVE", "ok") : chip("REVOKED", "crit") },
      { label: "Last used", render: (k) => k.last_used_at ? fmtRel(k.last_used_at) : "never" },
      { label: "", render: (k) => k.active ? `<button class="btn sm danger" data-rev="${esc(k.id)}">Revoke</button>` : "" }], d.keys, { empty: "No MCP access keys yet." }),
      { flush: true, actions: '<button class="btn primary sm" id="newkey">Create access key</button>' })}
  </div>
  <div style="margin-top:16px">${card("Recent agent activity", table([{ label: "Time", render: (r) => fmtRel(r.created_at) }, { label: "Agent", render: (r) => esc(r.actor_name) },
    { label: "Action", render: (r) => `<span class="code">${esc(r.action)}</span>` }, { label: "Tool", render: (r) => esc(r.resource_id || "") },
    { label: "Outcome", render: (r) => chip(r.outcome, r.outcome === "SUCCESS" ? "ok" : "crit") }], d.recent_calls, { empty: "No agent activity yet." }), { flush: true })}</div>
  <div style="margin-top:16px" id="aibox"></div>`;
  aiAssistant(el.querySelector("#aibox")).catch(errToast);
  el.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied"))));
  const f = el.querySelector("#mf");
  f.addEventListener("submit", (e) => {
    e.preventDefault();
    busy(f.querySelector("button.primary"), async () => {
      try {
        await api.put("/mcp/settings", { enabled: f.querySelector("[name=enabled]").checked, http_enabled: f.querySelector("[name=http_enabled]").checked,
          max_calls_per_minute: Number(f.querySelector("[name=max_calls_per_minute]").value), notes: f.querySelector("[name=notes]").value,
          tools: [...f.querySelectorAll("[data-tool]")].filter((x) => x.checked).map((x) => x.dataset.tool) });
        toast("AI-agent settings saved"); render(el);
      } catch (err) { errToast(err); }
    });
  });
  el.querySelector("#newkey").onclick = async () => {
    const r = await modal({ title: "Create MCP access key", body: `<div class="form-grid"><label class="f">Agent name<input class="input" name="name" placeholder="e.g. Claude – AML team" required></label>
      <label class="f">Workspace<select class="input" name="space">${options([["LIVE", "Live"], ["DEMO", "Demo"]], "LIVE")}</select></label></div>
      <p class="tiny muted" style="margin-top:8px">Scopes: mcp:use, read:operations, read:aml, read:executive, policies:simulate, reports:generate. Transaction and customer data are returned masked.</p>`,
      actions: [{ label: "Cancel" }, { label: "Create", cls: "primary", onClick: (m) => api.post("/mcp/keys", { name: m.querySelector("[name=name]").value, space: m.querySelector("[name=space]").value }) }] });
    if (!r) return;
    await modal({ title: "MCP access key", body: `<div class="notice warn" style="margin-bottom:12px">${esc(r.note)}</div><input class="input mono" readonly value="${esc(r.api_key)}" data-selectall>`,
      actions: [{ label: "I have stored it securely", cls: "primary", value: true }],
      onOpen: (m) => m.querySelectorAll("[data-selectall]").forEach((i) => (i.onclick = () => i.select())) });
    render(el);
  };
  el.querySelectorAll("[data-rev]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Revoke access key", "Agents using this key are disconnected immediately.", "Revoke", "danger"))) return;
    try { await api.del(`/api-clients/${b.dataset.rev}`); toast("Revoked"); render(el); } catch (e) { errToast(e); }
  }));
}

// AI assistant used inside VeriShield (case summaries, STR narratives).
async function aiAssistant(box) {
  const d = await api.get("/ai/settings");
  const c = d.config;
  box.innerHTML = `<form id="af">${card("AI assistant for case summaries and STR narratives", `
    <div class="row" style="margin-bottom:12px">${toggle("enabled", c.enabled, "AI assistant ON")}<span class="spacer"></span>
      ${c.sdk_installed ? chip("ANTHROPIC SDK READY", "ok") : chip("anthropic PACKAGE NOT INSTALLED", "crit")} ${c.api_key_configured ? chip("API KEY IN VAULT", "ok") : chip("NO API KEY", "neutral")}</div>
    <div class="form-grid">
      <label class="f">Model<select class="input" name="model">${options(d.models.map((m) => [m, m]), c.model)}</select></label>
      <label class="f">Effort<select class="input" name="effort">${options(["low", "medium", "high"], c.effort)}</select></label>
      <label class="f">API endpoint (optional)<span class="help">empty = Anthropic API; or the bank's approved gateway</span><input class="input mono" name="base_url" value="${esc(c.base_url)}"></label>
      <label class="f">API key<span class="help">stored encrypted; leave empty to keep</span><input class="input" type="password" name="api_key" autocomplete="new-password"></label></div>
    <div class="row" style="margin-top:12px;gap:22px">${toggle("mask_identifiers", c.mask_identifiers, "Mask customer names and account numbers before sending")}${toggle("fallbacks", c.fallbacks, "Server-side fallback model if a request is declined")}</div>
    <p class="small muted" style="margin-top:10px">When OFF (default) – or when the server cannot reach the model – VeriShield writes the narrative from the case facts itself. With AI ON, a case fact sheet is sent to Claude; analysts always review and edit the draft before it is saved. Check the bank's data-protection approval before switching it on.</p>`,
    { actions: '<button class="btn sm primary">Save</button>' })}</form>`;
  const f = box.querySelector("#af");
  f.onsubmit = (e) => { e.preventDefault(); busy(f.querySelector("button.primary"), async () => {
    try {
      const v = {}; f.querySelectorAll("[name]").forEach((i) => (v[i.name] = i.type === "checkbox" ? i.checked : i.value));
      if (!v.api_key) delete v.api_key;
      await api.put("/ai/settings", v); toast("AI assistant settings saved"); aiAssistant(box);
    } catch (err) { errToast(err); } }); };
}

// Policies & thresholds: versioned policy editor, simulation, publish / maker-checker / rollback (BRD 8.4).
import { api } from "../api.js";
import { S, busy, card, chip, confirmDialog, errToast, esc, fmtDate, fmtNum, label, loading, modal, options,
  stackedBars, table, toast } from "../ui.js";

const OUT = ["PASS", "REVIEW", "HOLD", "DECLINE"];

export async function render(el, { sub, setTitle }) {
  const list = await api.get("/policies");
  if (sub[0]) return editor(el, sub[0], list, setTitle);
  const a = list.active;
  const th = a.document.engine_thresholds;
  el.innerHTML = `<div class="grid g-21">
    ${card(`Active policy ${a.id}`, `<div class="kpis">
        <div class="kpi accent"><div class="label">Review from</div><div class="value">${th.suspicious}</div><div class="hint">combined fraud score</div></div>
        <div class="kpi warn"><div class="label">High from</div><div class="value">${th.high}</div></div>
        <div class="kpi crit"><div class="label">Critical from</div><div class="value">${th.critical}</div></div>
        <div class="kpi"><div class="label">Business rules</div><div class="value">${a.document.rules.filter((r) => r.enabled).length}</div><div class="hint">enabled</div></div>
      </div>
      <div class="row" style="margin-top:14px">${Object.entries(a.document.actions).map(([lvl, act]) => `${chip(lvl)} → ${chip(act)}`).join(" &nbsp; ")}</div>
      <p class="small muted" style="margin-top:12px">${esc(a.justification)}</p>`,
      { actions: list.can_edit ? '<button class="btn primary" id="draft">New draft from active</button>' : "", sub: `activated ${fmtDate(a.activated_at)}` })}
    ${card("Governance", `<p class="small">${list.maker_checker ? "<b>Maker-checker is enabled</b>: a second, permissioned approver must approve each published change." : "Maker-checker is <b>off</b>: the AML Manager publishes changes directly (IT Admin can enable approval under Users &amp; Settings › Institution)."}</p>
      <ul class="small" style="padding-left:18px"><li>Every change creates a new immutable version with editor, timestamp, justification and before/after values.</li>
      <li>Simulate against stored decisions before publishing. Original decisions are never rewritten.</li><li>Emergency rollback restores a previous version as a new version.</li>
      <li>Threshold numbers must be calibrated with institution-approved data before enforcement.</li></ul>`)}
  </div>
  <div style="margin-top:16px">${card("Policy versions", table([
    { label: "Version", render: (r) => `<a href="#/policies/${encodeURIComponent(r.id)}" class="mono small">${esc(r.id)}</a>` }, { label: "Name", key: "name" },
    { label: "Status", render: (r) => chip(r.status) }, { label: "Created by", key: "created_by_name" }, { label: "Created", render: (r) => fmtDate(r.created_at) },
    { label: "Activated", render: (r) => r.activated_at ? fmtDate(r.activated_at) : "–" }, { label: "Approved by", render: (r) => esc(r.approved_by_name || "") },
    { label: "Justification", render: (r) => `<span class="small">${esc((r.justification || "").slice(0, 90))}</span>` },
  ], list.versions), { flush: true })}</div>`;
  const d = el.querySelector("#draft");
  if (d) d.onclick = () => busy(d, async () => {
    try { const p = await api.post("/policies", { based_on: a.id, name: `Draft based on ${a.id}` }); location.hash = `#/policies/${encodeURIComponent(p.id)}`; } catch (e) { errToast(e); }
  });
}

const num = (name, value, step = "0.01", extra = "") => `<input class="input" type="number" step="${step}" name="${name}" value="${esc(value ?? "")}" ${extra}>`;

async function editor(el, id, list, setTitle) {
  const p = await api.get(`/policies/${encodeURIComponent(id)}`);
  setTitle(`Policy ${p.id}`, "Policies & Thresholds");
  const doc = p.document;
  const editable = p.status === "DRAFT" && list.can_edit;
  const dis = editable ? "" : "disabled";
  const th = doc.engine_thresholds, aml = doc.aml;
  const ruleRow = (r, i) => `<tr data-rule="${i}">
    <td><input type="checkbox" data-k="enabled" ${r.enabled ? "checked" : ""} ${dis}></td>
    <td><input class="input" data-k="id" value="${esc(r.id)}" ${dis} style="min-width:110px"></td>
    <td><input class="input" data-k="name" value="${esc(r.name)}" ${dis} style="min-width:200px"></td>
    <td><div class="small" data-k="summary" style="min-width:240px">${esc(ruleSummary(r.conditions))}</div>
      <textarea class="input mono hidden" data-k="conditions" ${dis}>${esc(JSON.stringify(r.conditions))}</textarea>
      <div class="row" style="margin-top:4px">${editable ? '<button type="button" class="btn sm" data-build>Edit conditions</button>' : ""}<button type="button" class="btn sm ghost" data-json>JSON</button></div></td>
    <td><select class="input" data-k="action" ${dis}>${options(OUT, r.action)}</select></td>
    <td><input class="input mono" data-k="reason_code" value="${esc(r.reason_code || "")}" ${dis}></td>
    <td>${editable ? '<button class="btn sm danger" data-del>Remove</button>' : ""}</td></tr>`;
  el.innerHTML = `<div class="row" style="margin-bottom:14px"><a href="#/policies" class="btn sm ghost">← Policies</a>${chip(p.status)}<span class="small muted">based on ${esc(p.based_on || "–")} · by ${esc(p.created_by_name)} · ${fmtDate(p.created_at)}</span><span class="spacer"></span>
      ${editable ? '<button class="btn" id="save">Save draft</button>' : ""}<button class="btn" id="sim">Simulate impact</button>
      ${editable && !list.maker_checker ? '<button class="btn primary" id="publish">Publish</button>' : ""}
      ${editable && list.maker_checker ? '<button class="btn primary" id="submit">Submit for approval</button>' : ""}
      ${p.status === "PENDING_APPROVAL" && list.can_approve ? '<button class="btn teal" id="approve">Approve &amp; activate</button><button class="btn danger" id="reject">Reject</button>' : ""}
      ${["SUPERSEDED"].includes(p.status) && list.can_edit ? '<button class="btn danger" id="rollback">Emergency rollback to this version</button>' : ""}
      ${editable ? '<button class="btn ghost" id="discard">Discard</button>' : ""}</div>
    ${p.status === "ACTIVE" ? '<div class="notice ok" style="margin-bottom:14px">This is the active policy. Published versions are immutable – create a new draft to change them.</div>' : ""}
    <form id="pf" class="stack">
      <div class="grid g2">
        ${card("Identification", `<div class="form-grid"><label class="f">Name<input class="input" name="name" value="${esc(p.name)}" ${dis}></label>
          <label class="f">Effective from (optional)<input class="input" type="datetime-local" name="effective_from" value="${p.effective_from ? p.effective_from.slice(0, 16) : ""}" ${dis}></label></div>
          <label class="f" style="margin-top:10px">Justification (mandatory to publish)<textarea class="input" name="justification" ${dis}>${esc(p.justification)}</textarea></label>`)}
        ${card("Risk level → outcome", `<div class="form-grid">${["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((l) => `<label class="f">${chip(l)}<select class="input" name="actions.${l}" ${dis}>${options(OUT, doc.actions[l])}</select></label>`).join("")}</div>
          <div class="form-grid" style="margin-top:12px"><label class="f">Limited-history cap<span class="help">New customers are never auto-held/declined on behaviour alone</span><select class="input" name="undertrained.cap_action" ${dis}>${options(OUT, doc.undertrained.cap_action)}</select></label>
          <label class="f">Hold expiry (minutes)${num("hold_expiry_minutes", doc.hold_expiry_minutes, "1", dis)}</label><label class="f">Decision validity (minutes)${num("decision_ttl_minutes", doc.decision_ttl_minutes, "1", dis)}</label></div>`)}
      </div>
      ${card("Fraud score thresholds (BankSealer engine)", `<div class="form-grid">
        <label class="f">Review (suspicious) from<span class="help">combined score 0–1</span>${num("th.suspicious", th.suspicious, "0.01", dis)}</label>
        <label class="f">High from${num("th.high", th.high, "0.01", dis)}</label><label class="f">Critical from${num("th.critical", th.critical, "0.01", dis)}</label>
        <label class="f">Local profile alert level${num("th.local_alert", th.local_alert, "0.01", dis)}</label><label class="f">Temporal profile alert level${num("th.temporal_alert", th.temporal_alert, "0.01", dis)}</label>
        <label class="f">Peer-group alert level${num("th.global_alert", th.global_alert, "0.01", dis)}</label>
        <label class="f">Hard-limit score floor${num("th.hard_rule_score", th.hard_rule_score, "0.01", dis)}</label>
        <label class="f">High-risk country boost${num("th.high_risk_country_boost", th.high_risk_country_boost, "0.01", dis)}</label>
        <label class="f">Night boost${num("th.night_boost", th.night_boost, "0.01", dis)}</label>
        ${Object.entries(th.max_amount).map(([k, v]) => `<label class="f">Max amount · ${esc(label(k))}<span class="help">${esc(S.me.tenant.currency)} per transaction</span>${num(`th.max_amount.${k}`, v, "1", dis)}</label>`).join("")}
        <label class="f">High-risk countries<span class="help">ISO codes, comma separated</span><input class="input" name="th.high_risk_countries" value="${esc(th.high_risk_countries.join(", "))}" ${dis}></label>
        <label class="f">Night hours<span class="help">comma separated</span><input class="input" name="th.night_hours" value="${esc(th.night_hours.join(", "))}" ${dis}></label>
      </div>`)}
      ${card("AML unusual-activity indicators", `<div class="form-grid">
        <label class="check"><input type="checkbox" name="aml.enabled" ${aml.enabled ? "checked" : ""} ${dis}> Indicators enabled</label>
        <label class="f">Window (hours)${num("aml.window_hours", aml.window_hours, "1", dis)}</label>
        <label class="f">Reporting threshold<span class="help">${esc(S.me.tenant.currency)}</span>${num("aml.reporting_threshold", aml.reporting_threshold, "1", dis)}</label>
        <label class="f">Structuring band<span class="help">fraction below threshold</span>${num("aml.structuring_band", aml.structuring_band, "0.01", dis)}</label>
        <label class="f">Structuring count${num("aml.structuring_count", aml.structuring_count, "1", dis)}</label>
        <label class="f">Velocity count (window)${num("aml.velocity_count", aml.velocity_count, "1", dis)}</label>
        <label class="f">Velocity vs declared activity<span class="help">× expected transactions in the window</span>${num("aml.velocity_expected_multiplier", aml.velocity_expected_multiplier, "0.5", dis)}</label>
        <label class="f">Turnover minimum excess<span class="help">${esc(S.me.tenant.currency)} above declared</span>${num("aml.turnover_min_excess", aml.turnover_min_excess, "100", dis)}</label>
        <label class="f">Turnover multiplier<span class="help">× declared monthly turnover</span>${num("aml.turnover_multiplier", aml.turnover_multiplier, "0.1", dis)}</label>
        <label class="f">Shared counterparty · customers${num("aml.shared_counterparty_customers", aml.shared_counterparty_customers, "1", dis)}</label>
        <label class="f">Shared counterparty · days${num("aml.shared_counterparty_days", aml.shared_counterparty_days, "1", dis)}</label>
        <label class="f">High-risk customer amount${num("aml.high_risk_customer_amount", aml.high_risk_customer_amount, "1", dis)}</label>
        <label class="f">ELEVATED signal →<select class="input" name="aml.elevated_action" ${dis}>${options(OUT, aml.elevated_action)}</select></label>
        <label class="f">HIGH signal →<select class="input" name="aml.high_action" ${dis}>${options(OUT, aml.high_action)}</select></label>
      </div>`)}
      ${card("Business rules (scoped)", `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>On</th><th>ID</th><th>Name</th><th>Conditions</th><th>Action</th><th>Reason code</th><th></th></tr></thead><tbody id="rules">${doc.rules.map(ruleRow).join("")}</tbody></table></div>
        ${editable ? '<button type="button" class="btn sm" id="addrule" style="margin-top:10px">Add rule</button>' : ""}
        <p class="tiny muted" style="margin-top:8px">Condition keys: ${list.condition_keys.map((k) => `<span class="code">${esc(k)}</span>`).join(" ")} – e.g. <span class="code">{"tx_types":["bank_transfer"],"channels":["mobile_banking"],"min_amount":5000,"segments":[2],"customer_risk":["HIGH"],"new_beneficiary":true,"velocity_count":5,"velocity_hours":1}</span></p>`)}
      ${card("Fallback when no reliable decision can be produced (ERROR_POLICY)", `<div class="form-grid">${Object.entries(doc.fallback).map(([k, v]) => `<label class="f">${esc(label(k))}<select class="input" name="fallback.${k}" ${dis}>${options(list.fallbacks.map((f) => [f, label(f)]), v)}</select></label>`).join("")}</div>
        <p class="tiny muted" style="margin-top:8px">The platform never selects a fallback silently: the decision is returned as ERROR_POLICY with the institution-approved fallback and an incident is raised.</p>`)}
      ${p.changes && p.changes.length ? card(`Changes vs ${p.based_on}`, table([{ label: "Setting", render: (c) => `<span class="mono small">${esc(c.path)}</span>` }, { label: "Before", render: (c) => `<span class="mono small">${esc(JSON.stringify(c.before))}</span>` }, { label: "After", render: (c) => `<span class="mono small">${esc(JSON.stringify(c.after))}</span>` }], p.changes), { flush: true }) : ""}
      <div id="simres"></div>
    </form>`;

  const collect = () => {
    const f = el.querySelector("#pf");
    const out = JSON.parse(JSON.stringify(doc));
    const set = (path, v) => { const ks = path.split("."); let o = out; ks.slice(0, -1).forEach((k) => (o = o[k])); o[ks[ks.length - 1]] = v; };
    f.querySelectorAll("[name]").forEach((i) => {
      const n = i.name;
      if (["name", "justification", "effective_from"].includes(n)) return;
      let v = i.type === "checkbox" ? i.checked : i.type === "number" ? (i.value === "" ? null : Number(i.value)) : i.value;
      if (n === "th.high_risk_countries") v = i.value.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
      if (n === "th.night_hours") v = i.value.split(",").map((x) => parseInt(x, 10)).filter((x) => !isNaN(x));
      set(n.replace(/^th\./, "engine_thresholds."), v);
    });
    out.rules = [...el.querySelectorAll("#rules tr")].map((tr) => {
      const g = (k) => tr.querySelector(`[data-k=${k}]`);
      let cond;
      try { cond = JSON.parse(g("conditions").value || "{}"); } catch { throw new Error(`Rule ${g("id").value}: conditions must be valid JSON`); }
      return { id: g("id").value.trim(), name: g("name").value, enabled: g("enabled").checked, conditions: cond, action: g("action").value, reason_code: g("reason_code").value.trim() };
    });
    return { document: out, name: f.name.value, justification: f.justification.value, effective_from: f.effective_from.value || null };
  };
  const bindRules = () => {
    el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => b.closest("tr").remove()));
    el.querySelectorAll("[data-json]").forEach((b) => (b.onclick = () => b.closest("td").querySelector("[data-k=conditions]").classList.toggle("hidden")));
    el.querySelectorAll("[data-build]").forEach((b) => (b.onclick = async () => {
      const td = b.closest("td");
      const ta = td.querySelector("[data-k=conditions]");
      let cond = {};
      try { cond = JSON.parse(ta.value || "{}"); } catch { /* start empty */ }
      const res = await ruleBuilder(cond);
      if (res) { ta.value = JSON.stringify(res); td.querySelector("[data-k=summary]").textContent = ruleSummary(res); }
    }));
  };
  bindRules();
  const add = el.querySelector("#addrule");
  if (add) add.onclick = () => { el.querySelector("#rules").insertAdjacentHTML("beforeend", ruleRow({ id: `R-${Date.now().toString(36).toUpperCase()}`, name: "New rule", enabled: true, conditions: { tx_types: ["bank_transfer"], min_amount: 5000 }, action: "REVIEW", reason_code: "" }, el.querySelectorAll("#rules tr").length)); bindRules(); };
  const save = async () => { const body = collect(); return api.put(`/policies/${encodeURIComponent(p.id)}`, body); };
  const on = (sel, fn) => { const b = el.querySelector(sel); if (b) b.onclick = (e) => busy(e.currentTarget, async () => { try { await fn(); } catch (err) { errToast(err); } }); };
  on("#save", async () => { await save(); toast("Draft saved"); render(el, { sub: [p.id], setTitle }); });
  on("#sim", async () => {
    if (editable) await save();
    const r = await api.post(`/policies/${encodeURIComponent(p.id)}/simulate`, { days: 30 });
    const rows = OUT.map((o) => ({ o, cur: r.current[o], prop: r.proposed[o], delta: r.delta[o] }));
    el.querySelector("#simres").innerHTML = card(`Simulation – ${fmtNum(r.evaluated)} decisions, last ${r.days} days (${r.space === "DEMO" ? "Demo data" : "Live data"})`, `<div class="grid g2">
      <div>${stackedBars([{ day: ".....Current", ...r.current }, { day: ".....Proposed", ...r.proposed }], OUT, { height: 150 })}</div>
      <div>${table([{ label: "Outcome", render: (x) => chip(x.o) }, { label: "Current", num: true, render: (x) => fmtNum(x.cur) }, { label: "Proposed", num: true, render: (x) => fmtNum(x.prop) },
        { label: "Change", num: true, render: (x) => `<b style="color:${x.delta > 0 ? "var(--red)" : x.delta < 0 ? "var(--ok)" : "inherit"}">${x.delta > 0 ? "+" : ""}${x.delta}</b>` }], rows)}</div></div>
      <p class="small" style="margin:10px 0">Recomputed live: AML signal changed for <b>${fmtNum(r.aml_signal_changes)}</b> decisions, business-rule hits changed for <b>${fmtNum(r.rule_hit_changes)}</b>.</p>
      <p class="tiny muted" style="margin:0 0 10px">${esc(r.note)}</p>
      ${r.changed_examples.length ? table([{ label: "Transaction", render: (x) => `<a href="#/transactions?open=${encodeURIComponent(x.tx_ref)}" class="mono small">${esc(x.tx_ref)}</a>` }, { label: "Customer", key: "customer_ref" },
        { label: "Amount", num: true, render: (x) => fmtNum(x.amount, 2) }, { label: "Score", num: true, render: (x) => Number(x.fraud_score).toFixed(2) },
        { label: "Current", render: (x) => chip(x.current) }, { label: "Proposed", render: (x) => chip(x.proposed) }], r.changed_examples) : '<p class="small muted">No individual decisions would change.</p>'}`);
    el.querySelector("#simres").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  on("#publish", async () => {
    await save();
    if (!(await confirmDialog("Publish policy", "New decisions will use this version from its effective time. Historical decisions keep their original policy version.", "Publish"))) return;
    await api.post(`/policies/${encodeURIComponent(p.id)}/publish`, { justification: el.querySelector("[name=justification]").value });
    toast("Policy published"); location.hash = "#/policies";
  });
  on("#submit", async () => { await save(); await api.post(`/policies/${encodeURIComponent(p.id)}/submit`); toast("Submitted for approval"); render(el, { sub: [p.id], setTitle }); });
  on("#approve", async () => { await api.post(`/policies/${encodeURIComponent(p.id)}/approve`); toast("Approved and activated"); location.hash = "#/policies"; });
  on("#reject", async () => { await api.post(`/policies/${encodeURIComponent(p.id)}/reject`, { reason: "" }); toast("Rejected"); location.hash = "#/policies"; });
  on("#discard", async () => { if (!(await confirmDialog("Discard draft", "Discard this draft?", "Discard", "danger"))) return; await api.post(`/policies/${encodeURIComponent(p.id)}/discard`); location.hash = "#/policies"; });
  on("#rollback", async () => {
    const r = await modal({ title: `Emergency rollback to ${p.id}`, body: '<label class="f">Justification<textarea class="input" name="j" required></textarea></label>',
      actions: [{ label: "Cancel" }, { label: "Roll back now", cls: "danger", onClick: (m) => api.post(`/policies/${encodeURIComponent(p.id)}/rollback`, { justification: m.querySelector("[name=j]").value }) }] });
    if (r) { toast(`Rolled back – new version ${r.id} active`); location.hash = "#/policies"; }
  });
}

// ------------------------------------------------------------------ visual rule builder
const TX_TYPES = ["bank_transfer", "phone_recharge", "prepaid_card", "card_payment", "cash_withdrawal", "cash_deposit", "incoming_transfer"];
const CHANNELS = ["mobile_banking", "internet_banking", "branch", "atm", "atm_kiosk", "card_pos", "card_ecom", "core_banking", "swift_in"];

export function ruleSummary(c) {
  const parts = [];
  if (c.tx_types && c.tx_types.length) parts.push(c.tx_types.map(label).join(" / "));
  if (c.channels && c.channels.length) parts.push(`via ${c.channels.map(label).join(", ")}`);
  if (c.min_amount != null) parts.push(`amount ≥ ${c.min_amount}`);
  if (c.max_amount != null) parts.push(`amount ≤ ${c.max_amount}`);
  if (c.new_beneficiary != null) parts.push(c.new_beneficiary ? "new counterparty" : "known counterparty");
  if (c.countries && c.countries.length) parts.push(`countries ${c.countries.join(", ")}`);
  if (c.customer_risk && c.customer_risk.length) parts.push(`customer risk ${c.customer_risk.join("/")}`);
  if (c.customer_types && c.customer_types.length) parts.push(c.customer_types.map(label).join("/"));
  if (c.hours && c.hours.length) parts.push(`hours ${c.hours.join(",")}`);
  if (c.velocity_count) parts.push(`≥ ${c.velocity_count} in ${c.velocity_hours || 24}h`);
  if (c.entry_modes && c.entry_modes.length) parts.push(`entry ${c.entry_modes.join("/")}`);
  if (c.mccs && c.mccs.length) parts.push(`MCC ${c.mccs.join(",")}`);
  if (c.min_fraud_score != null) parts.push(`score ≥ ${c.min_fraud_score}`);
  if (c.aml_levels && c.aml_levels.length) parts.push(`AML ${c.aml_levels.join("/")}`);
  if (c.modes && c.modes.length) parts.push(c.modes.map(label).join("/"));
  return parts.length ? `When ${parts.join(" · ")}` : "Always (no conditions)";
}

function ruleBuilder(c) {
  const multi = (name, list, sel = []) => `<div class="row" style="gap:6px 12px">${list.map((v) => `<label class="check"><input type="checkbox" data-m="${name}" value="${esc(v)}" ${sel.includes(v) ? "checked" : ""}> ${esc(label(v))}</label>`).join("")}</div>`;
  const num = (name, v, ph = "") => `<input class="input" type="number" step="any" name="${name}" value="${v ?? ""}" placeholder="${ph}">`;
  return modal({ title: "Rule conditions", wide: true, body: `<p class="small muted" style="margin-bottom:12px">All the conditions you fill must be true for the rule to act. Empty fields are ignored. Test the result with <b>Simulate impact</b> before publishing.</p>
    <div class="stack" style="gap:14px">
      <div><h4 style="margin-bottom:6px">Transaction types</h4>${multi("tx_types", TX_TYPES, c.tx_types || [])}</div>
      <div><h4 style="margin-bottom:6px">Channels</h4>${multi("channels", CHANNELS, c.channels || [])}</div>
      <div class="form-grid">
        <label class="f">Minimum amount${num("min_amount", c.min_amount)}</label><label class="f">Maximum amount${num("max_amount", c.max_amount)}</label>
        <label class="f">Counterparty<select class="input" name="new_beneficiary">${options([["", "Any"], ["true", "New (never used by the customer)"], ["false", "Known"]], c.new_beneficiary == null ? "" : String(c.new_beneficiary))}</select></label>
        <label class="f">Countries (counterparty or connection)<input class="input" name="countries" value="${esc((c.countries || []).join(", "))}" placeholder="e.g. KY, PA, RU"></label>
        <label class="f">At least N operations…${num("velocity_count", c.velocity_count, "e.g. 5")}</label><label class="f">…within hours${num("velocity_hours", c.velocity_hours, "24")}</label>
        <label class="f">Hours of day<input class="input" name="hours" value="${esc((c.hours || []).join(", "))}" placeholder="e.g. 0, 1, 2, 3"></label>
        <label class="f">Merchant category codes (MCC)<input class="input" name="mccs" value="${esc((c.mccs || []).join(", "))}" placeholder="e.g. 6051, 7995"></label>
        <label class="f">Minimum fraud score (0–1)${num("min_fraud_score", c.min_fraud_score)}</label>
      </div>
      <div class="grid g3">
        <div><h4 style="margin-bottom:6px">Customer risk</h4>${multi("customer_risk", ["LOW", "MEDIUM", "HIGH"], c.customer_risk || [])}</div>
        <div><h4 style="margin-bottom:6px">Customer type</h4>${multi("customer_types", ["INDIVIDUAL", "BUSINESS"], c.customer_types || [])}</div>
        <div><h4 style="margin-bottom:6px">Card entry mode</h4>${multi("entry_modes", ["ECOM", "CHIP", "CONTACTLESS", "MAGSTRIPE"], c.entry_modes || [])}</div>
        <div><h4 style="margin-bottom:6px">AML signal</h4>${multi("aml_levels", ["LOW", "ELEVATED", "HIGH"], c.aml_levels || [])}</div>
        <div><h4 style="margin-bottom:6px">Mode</h4>${multi("modes", ["PRE_AUTH", "POST_EVENT"], c.modes || [])}</div>
      </div>
      <p class="small" id="rsum" style="font-weight:600"></p></div>`,
    onOpen: (m) => {
      const upd = () => { m.querySelector("#rsum").textContent = ruleSummary(read(m)); };
      m.querySelectorAll("input,select").forEach((i) => i.addEventListener("input", upd)); upd();
    },
    actions: [{ label: "Cancel" }, { label: "Apply", cls: "primary", onClick: (m) => read(m) }] });
  function read(m) {
    const out = {};
    ["tx_types", "channels", "customer_risk", "customer_types", "entry_modes", "aml_levels", "modes"].forEach((k) => {
      const v = [...m.querySelectorAll(`[data-m=${k}]:checked`)].map((x) => x.value); if (v.length) out[k] = v; });
    ["min_amount", "max_amount", "velocity_count", "velocity_hours", "min_fraud_score"].forEach((k) => {
      const v = m.querySelector(`[name=${k}]`).value; if (v !== "") out[k] = Number(v); });
    const nb = m.querySelector("[name=new_beneficiary]").value; if (nb) out.new_beneficiary = nb === "true";
    const list = (k, f = (x) => x) => m.querySelector(`[name=${k}]`).value.split(",").map((x) => x.trim()).filter(Boolean).map(f);
    if (list("countries").length) out.countries = list("countries", (x) => x.toUpperCase());
    if (list("hours").length) out.hours = list("hours", (x) => parseInt(x, 10)).filter((x) => !isNaN(x));
    if (list("mccs").length) out.mccs = list("mccs");
    return out;
  }
}

export { loading };

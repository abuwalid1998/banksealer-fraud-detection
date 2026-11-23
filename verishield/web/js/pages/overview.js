// Role-specific overview dashboards (BRD 11.1 - 11.4).
import { api } from "../api.js";
import { navigate } from "../app.js";
import { COLORS, S, card, chip, codes, decisionChip, donut, empty, esc, fmtDate, fmtMoney, fmtNum, fmtRel, hbars,
  kpi, label, stackedBars, table } from "../ui.js";

const OUT = ["PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY"];

export async function render(el) {
  const d = await api.get("/dashboard");
  const role = d.role;
  const hello = `<div class="row" style="margin-bottom:16px"><div><h2 style="font-size:18px">Welcome, ${esc(S.me.user.first_name || S.me.user.name)}</h2>
    <div class="muted small">${esc(S.me.user.role_label)} workspace · ${d.space === "DEMO" ? "Demo Mode – figures are synthetic" : "Live data"} · updated ${fmtDate(d.generated_at)}</div></div>
    <span class="spacer"></span><span class="small muted">Policy <b>${esc(d.models_summary.policy || "–")}</b> · Fraud model <b>${esc(d.models_summary.fraud || "not trained")}</b></span></div>`;
  if (role === "IT_ADMIN") el.innerHTML = hello + itAdmin(d);
  else if (role === "AML_EMPLOYEE") el.innerHTML = hello + employee(d);
  else el.innerHTML = hello + manager(d, role === "CHIEF_AML");
  el.querySelectorAll("[data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); navigate(a.dataset.go); }));
}

function manager(d, chief) {
  const k = d.kpis;
  const c = d.cases;
  let html = `<div class="kpis">
    ${kpi("Open alerts", fmtNum(k.alerts_open), `${fmtNum(k.alerts_24h)} new in 24h`, "warn")}
    ${kpi("Open cases", fmtNum(k.cases_open), `${fmtNum(c.awaiting_manager)} awaiting Manager`, "accent")}
    ${kpi("SLA breached", fmtNum(c.breached), "open cases past due date", c.breached ? "crit" : "")}
    ${kpi("Held payments", fmtNum(k.held_count), fmtMoney(k.held_amount), k.held_count ? "warn" : "")}
    ${kpi("Decisions (24h)", fmtNum(k.decisions_24h), "pre-auth + post-event")}
    ${kpi("Escalations", fmtNum(d.alert_outcomes.escalations), "STR/SAR assessment referrals")}
  </div>`;
  html += `<div class="grid g-21" style="margin-top:16px">
    ${card("Decision outcomes – last 14 days", stackedBars(d.trend, OUT.slice(0, 4)), { sub: "Post-event decisions are monitoring outcomes, not blocks" })}
    ${card("Active alerts by severity", donut(["CRITICAL", "HIGH", "MEDIUM", "LOW"].map((s) => ({ label: s, value: d.alerts_by_severity[s] || 0, color: COLORS[s] })).filter((x) => x.value), { centerLabel: "alerts" })
      + `<div class="row small" style="margin-top:10px">${Object.entries(d.alerts_by_timing).map(([t, n]) => `${chip(t)} ${n}`).join(" ")}</div>`,
      { actions: '<a href="#" data-go="#/alerts" class="btn sm">Open queue</a>' })}
  </div>`;
  html += `<div class="grid g2" style="margin-top:16px">
    ${card("Held transactions awaiting decision", table([
      { label: "Transaction", render: (r) => `<a href="#/transactions?open=${encodeURIComponent(r.tx_ref)}">${esc(r.tx_ref)}</a>` },
      { label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a>` },
      { label: "Amount", num: true, render: (r) => fmtMoney(r.amount, r.currency) },
      { label: "Decision", render: (r) => decisionChip(r.decision) },
      { label: "State", render: (r) => chip(r.state) },
      { label: "Hold time left", num: true, render: (r) => r.minutes_left > 0 ? `${fmtNum(r.minutes_left)} min` : '<span class="chip c-expired">EXPIRY DUE</span>' },
    ], d.held, { empty: "No payments are currently held." }), { flush: true })}
    ${card("Analyst workload", table([
      { label: "Analyst", key: "name" }, { label: "Open", key: "open", num: true },
      { label: "SLA breached", num: true, render: (r) => r.breached ? `<b style="color:var(--red)">${r.breached}</b>` : "0" },
      { label: "Recommendations", key: "submitted", num: true },
      { label: "Avg turnaround", num: true, render: (r) => r.avg_turnaround_h === null ? "–" : `${r.avg_turnaround_h} h` },
    ], d.workload, { empty: "No AML Employees yet." }), { flush: true })}
  </div>`;
  html += `<div class="grid g3" style="margin-top:16px">
    ${card("Open case ageing", hbars(Object.entries(c.ageing).map(([l, v]) => ({ label: l, value: v, color: l === ">30d" ? "#C84B55" : l === "8-30d" ? "#E8A33A" : "#3569C8" }))))}
    ${card("Cases by status", hbars(Object.entries(c.by_status).map(([l, v]) => ({ label: label(l), value: v }))))}
    ${card("Alerts by peer segment (14d)", hbars(d.alerts_by_segment.map((s) => ({ label: s.segment.split(" · ").slice(0, 2).join(" · "), value: s.alerts }))))}
  </div>`;
  const byType = Object.entries(d.by_type).map(([t, v]) => ({ label: label(t), value: (v.REVIEW || 0) + (v.HOLD || 0) + (v.DECLINE || 0) }));
  const byChannel = Object.entries(d.by_channel).map(([t, v]) => ({ label: label(t), value: (v.REVIEW || 0) + (v.HOLD || 0) + (v.DECLINE || 0) }));
  html += `<div class="grid g3" style="margin-top:16px">
    ${card("Interventions by product (14d)", hbars(byType, { color: "#E8A33A" }))}
    ${card("Interventions by channel (14d)", hbars(byChannel, { color: "#E8A33A" }))}
    ${card("Case dispositions", hbars(Object.entries(c.dispositions).map(([l, v]) => ({ label: l, value: v, color: "#0BAA9B" }))))}
  </div>`;
  html += `<div class="grid g2" style="margin-top:16px">
    ${card("Policy version performance", table([{ label: "Policy", key: "p" }, ...OUT.map((o) => ({ label: o, num: true, render: (r) => fmtNum(r[o] || 0) }))],
      Object.entries(d.policy_performance).map(([p, v]) => ({ p, ...v }))), { flush: true, sub: `Active: ${esc(d.policy.active)}` })}
    ${card("Latest alerts", table([
      { label: "Alert", render: (r) => esc(r.id.split("-").slice(-1)[0]) },
      { label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a>` },
      { label: "Severity", render: (r) => chip(r.severity) }, { label: "Decision", render: (r) => decisionChip(r.decision) },
      { label: "Raised", render: (r) => fmtRel(r.created_at) }], d.recent_alerts), { flush: true, actions: '<a href="#" data-go="#/alerts" class="btn sm">All alerts</a>' })}
  </div>`;
  if (chief) {
    const m = d.model_effectiveness;
    html += `<div class="grid g3" style="margin-top:16px">
      ${card("Model effectiveness", `<dl class="kv">${Object.entries(m).map(([k, v]) => `<dt>${esc(label(k))}</dt><dd><b>${esc(v)}</b></dd>`).join("")}</dl>
        <p class="tiny muted" style="margin-top:10px">${d.space === "DEMO" ? "Demo figures use synthetic ground truth and are not production performance." : "Labels come from Manager dispositions."}</p>`)}
      ${card("Risk level trend (14d)", stackedBars(d.risk_trend || [], ["LOW", "MEDIUM", "HIGH", "CRITICAL"]))}
      ${card("Platform summary", `<dl class="kv"><dt>Decision service</dt><dd>${d.system_summary.decision_service_ready ? chip("READY", "ok") : chip("NOT READY", "crit")}</dd>
        <dt>Open incidents</dt><dd>${fmtNum(d.system_summary.open_incidents)}</dd><dt>Decision p95</dt><dd>${d.system_summary.decision_p95_ms ?? "–"} ms</dd></dl>`)}
    </div>`;
    html += `<div style="margin-top:16px">${card("High-priority open cases", table([
      { label: "Case", render: (r) => `<a href="#/cases/${encodeURIComponent(r.id)}">${esc(r.id)}</a>` }, { label: "Title", key: "title" },
      { label: "Priority", render: (r) => chip(r.priority) }, { label: "Status", render: (r) => chip(r.status) },
      { label: "Assignee", key: "assignee_name" }, { label: "Due", render: (r) => r.due_at ? `${fmtDate(r.due_at, { time: false })} ${r.sla_breached ? chip("BREACHED", "crit") : ""}` : "–" },
    ], d.high_risk_cases, { empty: "No high-priority open cases." }), { flush: true })}</div>`;
  }
  return html;
}

function employee(d) {
  const k = d.kpis;
  return `<div class="kpis">
    ${kpi("My open cases", fmtNum(k.assigned_open), "", "accent")}
    ${kpi("Due in 24h", fmtNum(k.due_24h), "", k.due_24h ? "warn" : "")}
    ${kpi("SLA breached", fmtNum(k.breached), "", k.breached ? "crit" : "")}
    ${kpi("Returned for more work", fmtNum(k.returned), "", k.returned ? "warn" : "")}
    ${kpi("Awaiting Manager", fmtNum(k.awaiting_manager))}
    ${kpi("Avg turnaround", d.my_metrics.avg_turnaround_h === null ? "–" : `${d.my_metrics.avg_turnaround_h} h`, `${fmtNum(d.my_metrics.recommendations)} recommendations`)}
  </div>
  <div style="margin-top:16px">${card("My investigations", table([
    { label: "Case", render: (r) => `<a href="#/cases/${encodeURIComponent(r.id)}">${esc(r.id)}</a>` },
    { label: "Title", key: "title" }, { label: "Priority", render: (r) => chip(r.priority) }, { label: "Status", render: (r) => chip(r.status) },
    { label: "Checklist", render: (r) => `${r.checklist_done}/${r.checklist_total}` },
    { label: "Due", render: (r) => r.due_at ? `${fmtDate(r.due_at)} <span class="small ${r.sla_breached ? "" : "muted"}" style="${r.sla_breached ? "color:var(--red);font-weight:600" : ""}">${r.sla_breached ? "overdue" : fmtRel(r.due_at)}</span>` : "–" },
  ], d.cases, { empty: "No cases are assigned to you right now." }), { flush: true, sub: "Only cases assigned to you and their linked context are visible." })}</div>`;
}

function itAdmin(d) {
  const k = d.kpis;
  const lat = d.latency;
  const live = d.models.live, demo = d.models.demo;
  return `<div class="kpis">
    ${kpi("Events received (24h)", fmtNum(k.received_24h), "excluding history imports", "accent")}
    ${kpi("Accepted", fmtNum(k.accepted_24h))}
    ${kpi("Rejected", fmtNum(k.rejected_24h), "dead-letter queue", k.rejected_24h ? "warn" : "")}
    ${kpi("Duplicates", fmtNum(k.duplicates_24h))}
    ${kpi("Connectors enabled", fmtNum(k.connectors_enabled))}
    ${kpi("Connectors degraded", fmtNum(k.connectors_degraded), "", k.connectors_degraded ? "crit" : "")}
  </div>
  <div class="grid g3" style="margin-top:16px">
    ${card("Decision service", `<dl class="kv"><dt>Live workspace</dt><dd>${live.ready ? chip("READY", "ok") : chip("NO MODEL", "warn")} <span class="small muted">${esc(live.active_model || "")}</span></dd>
      <dt>Demo workspace</dt><dd>${demo.ready ? chip("READY", "ok") : chip("NO MODEL", "warn")} <span class="small muted">${esc(demo.active_model || "")}</span></dd>
      <dt>Shadow model</dt><dd>${esc(live.shadow_model || demo.shadow_model || "–")}</dd>
      <dt>Decision latency</dt><dd>p50 ${lat.decision.p50 ?? "–"} ms · p95 ${lat.decision.p95 ?? "–"} ms</dd>
      <dt>API latency</dt><dd>p50 ${lat.api.p50 ?? "–"} ms · p95 ${lat.api.p95 ?? "–"} ms</dd></dl>`)}
    ${card("Users", `<dl class="kv">${Object.entries(d.users).map(([s, n]) => `<dt>${chip(s)}</dt><dd>${n}</dd>`).join("")}</dl>`, { actions: '<a href="#" data-go="#/settings" class="btn sm">Manage</a>' })}
    ${card("Recent errors", d.errors.length ? `<ul class="timeline">${d.errors.slice().reverse().map((e) => `<li><div>${esc(e.category)}: ${esc(e.message)}</div><div class="t">${fmtRel(new Date(e.at * 1000).toISOString())}</div></li>`).join("")}</ul>` : empty("No errors recorded since start."))}
  </div>
  <div style="margin-top:16px">${card("Channels", table([
    { label: "Connector", render: (r) => `<b>${esc(r.name)}</b><div class="tiny muted">${esc(r.type_label)}</div>` },
    { label: "Status", render: (r) => chip(r.status) }, { label: "Env", key: "environment" }, { label: "Mode", render: (r) => chip(r.ingest_mode) },
    { label: "Received", num: true, render: (r) => fmtNum(r.counters.received || 0) }, { label: "Rejected", num: true, render: (r) => fmtNum(r.counters.rejected || 0) },
    { label: "Duplicates", num: true, render: (r) => fmtNum(r.counters.duplicates || 0) },
    { label: "Last event", render: (r) => r.last_event_at ? fmtRel(r.last_event_at) : "–" },
    { label: "Credential", render: (r) => r.credential_expiry_warning ? chip(`EXPIRES IN ${r.credential_expires_in_days}D`, "warn") : r.secret_configured ? chip("SET", "ok") : '<span class="muted">–</span>' },
  ], d.connectors, { empty: "No connectors configured yet – add one under Integrations & Channels." }), { flush: true, actions: '<a href="#" data-go="#/integrations" class="btn sm">Integrations</a>' })}</div>
  <div class="grid g2" style="margin-top:16px">
    ${card("Incidents", table([{ label: "Severity", render: (r) => chip(r.severity, r.severity === "SEV1" ? "crit" : "warn") }, { label: "Title", key: "title" },
      { label: "Status", render: (r) => chip(r.status) }, { label: "Count", key: "occurrences", num: true }, { label: "Last seen", render: (r) => fmtRel(r.last_seen_at) }], d.incidents, { empty: "No incidents." }),
      { flush: true, actions: '<a href="#" data-go="#/operations" class="btn sm">Operations</a>' })}
    ${card("Recent configuration & audit events", table([{ label: "Time", render: (r) => fmtRel(r.created_at) }, { label: "Actor", key: "actor_name" },
      { label: "Action", render: (r) => `<span class="code">${esc(r.action)}</span>` }, { label: "Resource", render: (r) => esc(r.resource_id || r.resource_type) }], d.config_changes), { flush: true })}
  </div>`;
}

export { codes };

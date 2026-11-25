// Role-specific overview dashboards (BRD 11.1 - 11.4).
import { api } from "../api.js";
import { navigate } from "../app.js";
import { COLORS, S, card, chip, codes, decisionChip, donut, empty, esc, fmtDate, fmtMoney, fmtNum, fmtRel, hbars,
  cityHeatMap, icon, kpi, label, live, liveBadge, stackedBars, table, trendChart } from "../ui.js";

const OUT = ["PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY"];

export async function render(el) {
  const draw = async () => {
    const d = await api.get("/dashboard");
    const role = d.role;
    const hello = `<div class="row" style="margin-bottom:16px"><div><h2 style="font-size:18px">Welcome, ${esc(S.me.user.first_name || S.me.user.name)}</h2>
      <div class="muted small">${esc(S.me.user.role_label)} workspace · ${d.space === "DEMO" ? "Demo workspace (live scoring, simulated payment actions)" : "Live workspace"} · ${liveBadge("AUTO-REFRESH")} updated ${fmtDate(d.generated_at, { seconds: true })}</div></div>
      <span class="spacer"></span><span class="small muted">Policy <b>${esc(d.models_summary.policy || "–")}</b> · Fraud model <b>${esc(d.models_summary.fraud || "not trained")}</b></span></div>`;
    const pulse = await api.get("/live/pulse").catch(() => ({}));
    const idle = !pulse.channels_running && !pulse.decisions_last_5_minutes;
    const hint = idle && role !== "AML_EMPLOYEE" ? `<div class="hero" style="margin-bottom:16px"><div style="flex:1;min-width:260px;position:relative;z-index:1">
        <h2>${role === "IT_ADMIN" ? "Start a channel to stream transactions" : "Waiting for live transactions"}</h2>
        <p>${role === "IT_ADMIN" ? "No channel is streaming. Start the payment gateway (or the simulator in Demo Mode) under Integrations & Channels; every transaction is scored as it arrives and written to the results database."
          : "No channel is streaming right now. Scores, alerts and figures update automatically as soon as the IT Admin starts a channel."}</p></div>
        ${role === "IT_ADMIN" ? `<a href="#" data-go="#/integrations" class="btn teal">${icon("play")} Open Integrations &amp; Channels</a>` : ""}</div>` : "";
    if (role === "IT_ADMIN") el.innerHTML = hello + hint + itAdmin(d);
    else if (role === "AML_EMPLOYEE") el.innerHTML = hello + employee(d);
    else if (role === "CEO") { el.innerHTML = hello + hint + executive(d); valueCard(el.querySelector("#value")); whatIf(el.querySelector("#whatif")); }
    else el.innerHTML = hello + hint + manager(d, role === "CHIEF_AML");
    el.querySelectorAll("[data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); navigate(a.dataset.go); }));
  };
  await draw();
  live(el, 10000, draw);
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

function executive(d) {
  const k = d.kpis, sys = d.system, cur = S.me.tenant.currency;
  const big = (v) => Math.abs(v) >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : Math.abs(v) >= 1e4 ? `${(v / 1e3).toFixed(1)}K` : fmtNum(v);
  return `<div class="exec-grid">
    ${kpi("Transactions scored (30d)", fmtNum(k.transactions), `${fmtNum(k.transactions_24h)} in the last 24h`, "accent")}
    ${kpi("Value scored (30d)", `${big(k.value)} <span style="font-size:13px;color:var(--muted)">${esc(cur)}</span>`, `${big(k.value_24h)} ${esc(cur)} in 24h`)}
    ${kpi("Value protected", `${big(k.protected_value)} <span style="font-size:13px;color:var(--muted)">${esc(cur)}</span>`, "held or declined before execution", k.protected_value ? "warn" : "")}
    ${kpi("Intervention rate", `${k.intervention_rate.toFixed(2)}%`, "review, hold or decline")}
    ${kpi("Alerts open", fmtNum(k.alerts_open), `${fmtNum(k.alerts_period)} raised in 30 days`, k.alerts_open ? "warn" : "")}
    ${kpi("AML cases open", fmtNum(k.cases_open), `${fmtNum(k.cases_breached)} past due · ${fmtNum(k.escalations)} escalated`, k.cases_breached ? "crit" : "")}
    ${kpi("Customers", fmtNum(k.customers), `${fmtNum(k.customers_active)} active · ${fmtNum(k.customers_high_risk)} high risk`)}
    ${kpi("Platform", sys.decision_service_ready ? chip("OPERATIONAL", "ok") : chip("DEGRADED", "crit"), `${sys.channels_running}/${sys.channels_total} channels streaming · ${sys.open_incidents} open incidents`)}
  </div>
  <div class="grid g-21" style="margin-top:16px">
    ${card("Daily transactions and interventions – 30 days", trendChart(d.trend, { key: "count", key2: "interventions" }) + '<div class="legend" style="margin-top:6px"><span><i style="background:#3569C8"></i>Transactions</span><span><i style="background:#E8A33A"></i>Interventions (dashed)</span></div>')}
    ${card("Decision mix", donut(["PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY"].map((x) => ({ label: label(x), value: d.decisions[x] || 0, color: COLORS[x] })).filter((x) => x.value), { centerLabel: "decisions" }))}
  </div>
  <div class="grid g3" style="margin-top:16px">
    ${card("By channel", hbars(d.by_channel.map((c) => ({ label: label(c.channel), value: c.count }))))}
    ${card("By product", hbars(d.by_type.map((c) => ({ label: label(c.type), value: c.count, color: "#0BAA9B" }))))}
    ${card("Risk heat map by city", cityHeatMap(d.by_city, { valueKey: "count", rateKey: "rate" }), { sub: "size = transactions · colour = intervention rate" })}
  </div>
  <div class="grid g3" style="margin-top:16px">
    ${card("Customer risk (institution rating)", donut(["LOW", "MEDIUM", "HIGH"].map((r) => ({ label: label(r), value: d.customer_risk[r] || 0, color: COLORS[r === "HIGH" ? "DECLINE" : r] })).filter((x) => x.value), { centerLabel: "customers" }))}
    ${card("Customers by segment", hbars(d.segments.map((x) => ({ label: x.segment, value: x.customers, color: "#3569C8" }))), { actions: '<a href="#" data-go="#/clusters" class="btn sm">Clusters</a>' })}
    ${card("Open alerts by severity", hbars(["CRITICAL", "HIGH", "MEDIUM", "LOW"].map((x) => ({ label: label(x), value: d.alerts_by_severity[x] || 0, color: COLORS[x] }))))}
  </div>
  <div class="grid g2" style="margin-top:16px"><div id="value"></div><div id="whatif"></div></div>
  <div class="grid g2" style="margin-top:16px">
    ${card("Customers with the most interventions", table([{ label: "Customer", render: (r) => `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a>` },
      { label: "Name", key: "name" }, { label: "Interventions", key: "interventions", num: true }, { label: "Highest score", num: true, render: (r) => Number(r.max_score).toFixed(2) }],
      d.top_risk_customers.filter((r) => r.interventions), { empty: "No interventions in the period." }), { flush: true })}
    ${card("Board reports", `<p class="small">Generate formal, institution-branded PDF / Excel reports on the platform, transactions and customers:</p>
      <ul class="small" style="padding-left:18px"><li>Board report – fraud, AML &amp; platform overview</li><li>Transaction activity report</li><li>Customer portfolio report</li><li>Executive fraud &amp; AML overview, model health, channel health</li></ul>
      <a href="#" data-go="#/reports" class="btn primary">${icon("reports")} Open reports</a>
      <p class="tiny muted" style="margin-top:10px">Platform: decision latency p50 ${sys.decision_p50_ms ?? "–"} ms · p95 ${sys.decision_p95_ms ?? "–"} ms · model ${esc(sys.active_model || "–")}</p>`)}
  </div>`;
}

let whatIfValue = 1.0;
const cache = {};
async function cached(path, ttl = 60000) {
  const hit = cache[path];
  if (hit && Date.now() - hit.at < ttl) return hit.data;
  const data = await api.get(path);
  cache[path] = { at: Date.now(), data };
  return data;
}
async function valueCard(box) {
  if (!box) return;
  const v = await cached("/value?days=30").catch(() => null);
  if (!v) return;
  const cur = v.currency;
  box.innerHTML = card("Money saved – last 30 days", `<div class="kpis" style="grid-template-columns:repeat(2,1fr)">
      ${kpi("Fraud prevented", `${fmtNum(v.prevented_loss, 0)} <span style="font-size:12px">${esc(cur)}</span>`, "confirmed fraud stopped before execution", "accent")}
      ${kpi("Net benefit", `${fmtNum(v.net_benefit, 0)} <span style="font-size:12px">${esc(cur)}</span>`, v.return_per_unit_cost ? `${v.return_per_unit_cost}× the alert-handling cost` : "after alert-handling cost")}
      ${kpi("Cost per alert", `${fmtNum(v.cost_per_alert, 2)} ${esc(cur)}`, `${fmtNum(v.alerts)} alerts${v.false_positive_share != null ? ` · ${v.false_positive_share}% closed as false positive` : ""}`)}
      ${kpi("Detection by value", v.detection_rate_by_value == null ? "–" : `${v.detection_rate_by_value}%`, `missed ${fmtNum(v.missed_fraud_value, 0)} ${esc(cur)}`)}</div>
    <p class="tiny muted" style="margin-top:8px">Based on confirmed outcomes (dispositions, customer answers, reports). Assumptions: ${v.assumptions.minutes_per_alert} min per alert at ${fmtNum(v.assumptions.analyst_hourly_cost)} ${esc(cur)}/h.</p>`);
}
async function whatIf(box) {
  if (!box) return;
  const draw = async () => {
    box.innerHTML = card("What if the policy were stricter or more lenient?", '<div class="loading"><span class="spin"></span></div>');
    const w = await cached(`/what-if?strictness=${whatIfValue}&days=14`, 120000).catch(() => null);
    if (!w) { box.innerHTML = ""; return; }
    const delta = w.interventions.proposed - w.interventions.current;
    box.innerHTML = card("What if the policy were stricter or more lenient?", `<label class="f">Strictness <span class="help">left = stricter thresholds (more alerts) · right = more lenient</span>
        <input type="range" min="0.6" max="1.4" step="0.05" value="${whatIfValue}" id="wf"></label>
      <div class="row small" style="justify-content:space-between"><span>stricter</span><b>${whatIfValue === 1 ? "current policy" : `thresholds × ${whatIfValue.toFixed(2)}`}</b><span>more lenient</span></div>
      <div class="kpis" style="grid-template-columns:repeat(3,1fr);margin-top:10px">
        ${kpi("Interventions (14 days)", `${fmtNum(w.interventions.proposed)}`, `now ${fmtNum(w.interventions.current)} (${delta >= 0 ? "+" : ""}${delta})`, delta > 0 ? "warn" : "")}
        ${kpi("Analyst hours", `${w.analyst_hours.proposed}`, `now ${w.analyst_hours.current}`)}
        ${kpi("Thresholds", `${w.thresholds.suspicious}`, `high ${w.thresholds.high} · critical ${w.thresholds.critical}`)}</div>
      <p class="tiny muted" style="margin-top:8px">Replays the last ${w.evaluated} decisions with the changed thresholds – nothing is changed. The AML Manager turns a decision into a new policy version under Policies & Thresholds.</p>`);
    box.querySelector("#wf").onchange = (e) => { whatIfValue = Number(e.target.value); draw(); };
  };
  draw();
}

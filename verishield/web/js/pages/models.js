// Models & segments: fraud model registry (activate/shadow/rollback), segmentation, onboarding data quality & imports.
import { api } from "../api.js";
import { S, busy, card, chip, confirmDialog, errToast, esc, fmtDate, fmtNum, hbars, kpi, kv, label, loading, table,
  toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el) {
  const tabs = [["fraud", "Fraud models"], ["segments", "Customer segments"]];
  if (can("models.train") || can("models.view")) tabs.push(["onboarding", "Onboarding data"]);
  let cur = "fraud";
  const draw = async () => {
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const box = el.querySelector("#tab");
    try {
      if (cur === "fraud") await fraud(box, draw);
      else if (cur === "segments") await segments(box, draw);
      else await onboarding(box, draw);
    } catch (e) { errToast(e); }
  };
  draw();
}

async function fraud(box, redraw) {
  const d = await api.get("/models");
  const st = d.status, perf = d.performance;
  const fraudModels = d.models.filter((m) => m.kind === "FRAUD");
  box.innerHTML = `<div class="kpis">
      ${kpi("Engine", st.ready ? chip("READY", "ok") : chip("NOT TRAINED", "warn"), esc(st.state), "accent")}
      ${kpi("Active model", `<span style="font-size:16px">${esc(st.active_model || "–")}</span>`)}
      ${kpi("Shadow model", `<span style="font-size:16px">${esc(st.shadow_model || "–")}</span>`, "scored in parallel, never decides")}
      ${kpi("Undertrained profiles", fmtNum(st.undertrained_profiles), "limited-history customers")}
      ${kpi("Alert rate", esc(perf.alert_rate), `${fmtNum(perf.decisions)} decisions`)}
      ${perf.precision ? kpi("Precision / recall", `${perf.precision} / ${perf.recall}`, S.me.tenant.demo_mode ? "synthetic ground truth" : "labelled outcomes") : ""}
    </div>
    ${d.can_train ? `<div class="row" style="margin:16px 0"><button class="btn primary" data-train="activate">Train &amp; activate new version</button>
      <button class="btn" data-train="shadow">Train as shadow (challenger)</button>
      <button class="btn" data-train="trained">Train only (review first)</button><span class="small muted">Training uses history plus non-alerted decisions; confirmed-fraud labels are excluded.</span></div>` : ""}
    ${card("Fraud model versions", table([
      { label: "Model", render: (m) => `<b class="mono small">${esc(m.id)}</b>` }, { label: "Status", render: (m) => chip(m.status) },
      { label: "Trained", render: (m) => `${fmtDate(m.trained_at)}<div class="tiny muted">by ${esc(m.trained_by)}</div>` },
      { label: "Training set", num: true, render: (m) => fmtNum(m.training_size) },
      { label: "Customers", num: true, render: (m) => fmtNum(m.metrics.customers) }, { label: "Peer groups", num: true, render: (m) => fmtNum(m.metrics.peer_groups) },
      { label: "Undertrained", num: true, render: (m) => fmtNum(m.metrics.undertrained_customers) },
      { label: "Data window", render: (m) => `<span class="small">${fmtDate(m.training_from, { time: false })} – ${fmtDate(m.training_to, { time: false })}</span>` },
      { label: "", render: (m) => d.can_activate ? `<div class="row nowrap">${m.status !== "ACTIVE" ? `<button class="btn sm" data-act="${esc(m.id)}">${m.status === "RETIRED" ? "Roll back to" : "Activate"}</button>` : ""}
          ${m.status === "TRAINED" || m.status === "RETIRED" ? `<button class="btn sm" data-shadow="${esc(m.id)}">Shadow</button>` : ""}${m.status === "SHADOW" ? '<button class="btn sm" data-clear>Stop shadow</button>' : ""}</div>` : "" },
    ], fraudModels, { empty: "No fraud model trained in this workspace yet – import history under Onboarding data, then train." }), { flush: true })}
    <div class="grid g2" style="margin-top:16px">
      ${card("Profiles in the BankSealer engine", `<p class="small">Each decision combines three behavioural profiles with the active policy:</p>
        <ul class="small"><li><b>Local</b> – per-customer HBOS histograms (amount, beneficiary, country, connection, IP, hour).</li>
        <li><b>Temporal</b> – 24-hour activity window versus the customer's mean + k·σ.</li><li><b>Global</b> – peer groups by iterative DBSCAN with CBLOF outlier scoring.</li></ul>
        <p class="tiny muted">Every decision preserves the exact policy and model version it was produced with. Shadow models never influence decisions.</p>`)}
      ${card("Outcome measures", kv(Object.entries(perf).map(([k, v]) => [label(k), `<b>${esc(v)}</b>`])))}
    </div>`;
  box.querySelectorAll("[data-train]").forEach((b) => (b.onclick = () => busy(b, async () => {
    try { const m = await api.post("/models/fraud/train", { mode: b.dataset.train }); toast(`Model ${m.id} trained (${m.status})`); redraw(); } catch (e) { errToast(e); }
  })));
  box.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Activate model", `New decisions will use ${b.dataset.act}. Previous decisions keep their original model version. Continue?`, "Activate"))) return;
    busy(b, async () => { try { await api.post(`/models/${encodeURIComponent(b.dataset.act)}/activate`); toast("Model activated"); redraw(); } catch (e) { errToast(e); } });
  }));
  box.querySelectorAll("[data-shadow]").forEach((b) => (b.onclick = () => busy(b, async () => {
    try { await api.post(`/models/${encodeURIComponent(b.dataset.shadow)}/shadow`); toast("Shadow model enabled"); redraw(); } catch (e) { errToast(e); } })));
  box.querySelectorAll("[data-clear]").forEach((b) => (b.onclick = () => busy(b, async () => {
    try { await api.post(`/models/${encodeURIComponent(st.shadow_model)}/clear-shadow`); toast("Shadow stopped"); redraw(); } catch (e) { errToast(e); } })));
}

async function segments(box, redraw) {
  const d = await api.get("/models/segments");
  const all = await api.get("/models");
  const segModels = all.models.filter((m) => m.kind === "SEGMENTATION");
  const m = d.model;
  box.innerHTML = `${m ? `<div class="kpis">
      ${kpi("Active segmentation", `<span style="font-size:16px">${esc(m.id)}</span>`, fmtDate(m.trained_at), "accent")}
      ${kpi("Segments", fmtNum(m.metrics.k), "k chosen by silhouette")}
      ${kpi("Silhouette", m.metrics.silhouette, "cluster separation (-1…1)")}
      ${kpi("Stability", m.metrics.stability_rand_index, "agreement across re-runs")}
      ${kpi("Cold-start customers", fmtNum(m.metrics.cold_start), "assigned from KYC profile only")}
      ${kpi("Low-confidence fits", fmtNum(d.low_confidence_assignments), "confidence below 60%", d.low_confidence_assignments ? "warn" : "")}
    </div>` : '<div class="notice warn">No segmentation model is active. Import customers (and history if available), then build segments.</div>'}
    ${all.can_train ? `<div class="row" style="margin:16px 0"><button class="btn primary" id="build">Build new segmentation</button><span class="small muted">k-means over approved KYC + behavioural features; protected attributes are never used.</span></div>` : ""}
    <div class="grid g2" style="margin-top:16px">${d.segments.map((s) => card(s.label, `${hbars([{ label: "Customers now", value: s.customers_now }], { max: Math.max(...d.segments.map((x) => x.customers_now), 1) })}
      <p class="small muted" style="margin:10px 0">${esc(s.description)}</p>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Feature</th><th class="num">Centre</th><th class="num">P10 – P90</th></tr></thead><tbody>${Object.values(s.profile).map((f) => `<tr><td>${esc(f.label)}</td><td class="num">${fmtNum(f.centre, 2)}</td><td class="num">${fmtNum(f.p10, 2)} – ${fmtNum(f.p90, 2)}</td></tr>`).join("")}</tbody></table></div>`,
      { sub: `<a href="#/customers?segment=${s.segment_key}">view customers</a>` })).join("")}</div>
    <div style="margin-top:16px">${card("Segmentation versions", table([{ label: "Model", render: (x) => `<span class="mono small">${esc(x.id)}</span>` }, { label: "Status", render: (x) => chip(x.status) },
      { label: "Built", render: (x) => fmtDate(x.trained_at) }, { label: "Customers", num: true, render: (x) => fmtNum(x.training_size) }, { label: "k", num: true, render: (x) => x.metrics.k },
      { label: "Silhouette", num: true, render: (x) => x.metrics.silhouette }, { label: "", render: (x) => all.can_activate && x.status !== "ACTIVE" ? `<button class="btn sm" data-act="${esc(x.id)}">Activate</button>` : "" }], segModels), { flush: true })}</div>
    <div class="notice" style="margin-top:16px">A cluster is a peer-comparison aid – not a label of guilt, a regulatory customer-risk grade or a standalone reason to block a customer.</div>`;
  const b = box.querySelector("#build");
  if (b) b.onclick = () => busy(b, async () => { try { const r = await api.post("/models/segmentation/build", { activate: true }); toast(`Segmentation ${r.id} built and activated`); redraw(); } catch (e) { errToast(e); } });
  box.querySelectorAll("[data-act]").forEach((x) => (x.onclick = () => busy(x, async () => {
    try { await api.post(`/models/${encodeURIComponent(x.dataset.act)}/activate`); toast("Segmentation activated – customers reassigned"); redraw(); } catch (e) { errToast(e); } })));
}

async function onboarding(box, redraw) {
  const q = await api.get("/onboarding/data-quality");
  const trainer = can("models.train");
  box.innerHTML = `<div class="kpis">
      ${kpi("Customers", fmtNum(q.customers), `${fmtNum(q.customers_with_kyc)} with KYC record`, "accent")}
      ${kpi("Transactions", fmtNum(q.transactions), `${fmtNum(q.history_transactions)} historical`)}
      ${kpi("History coverage", `${fmtNum(q.time_coverage.days)} days`, q.time_coverage.from ? `${fmtDate(q.time_coverage.from, { time: false })} – ${fmtDate(q.time_coverage.to, { time: false })}` : "")}
      ${kpi("No history (cold start)", fmtNum(q.customers_without_history))}
      ${kpi("Limited history", fmtNum(q.customers_limited_history), "< 10 transactions")}
      ${kpi("Ready for modelling", q.ready_for_modelling ? chip("YES", "ok") : chip("NOT YET", "warn"))}
    </div>
    ${q.issues.length ? `<div class="notice warn" style="margin-top:16px"><b>Data-quality findings</b><ul style="margin:6px 0 0;padding-left:18px">${q.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : '<div class="notice ok" style="margin-top:16px">No blocking data-quality issues detected.</div>'}
    <div class="grid g2" style="margin-top:16px">
      ${card("Customer field completeness", table([{ label: "Field", render: (r) => esc(label(r.f)) }, { label: "Missing", num: true, render: (r) => fmtNum(r.missing) },
        { label: "Missing %", num: true, render: (r) => `<span style="${r.pct >= 20 ? "color:var(--red);font-weight:600" : ""}">${r.pct}%</span>` }],
        Object.entries(q.missing_fields).map(([f, v]) => ({ f, ...v }))), { flush: true })}
      ${card("Transactions by product & rejected records", hbars(Object.entries(q.transaction_types).map(([k, v]) => ({ label: label(k), value: v }))) +
        `<div style="margin-top:14px">${Object.keys(q.rejected_records).length ? hbars(Object.entries(q.rejected_records).map(([k, v]) => ({ label: label(k), value: v, color: "#C84B55" }))) : '<p class="small muted">No rejected records.</p>'}</div>`)}
    </div>
    ${trainer ? `<div class="grid g2" style="margin-top:16px">
      ${card("1 · Import customer master / KYC", `<form id="fc" class="stack" style="gap:10px"><input class="input" type="file" name="file" accept=".csv,.json,.jsonl" required>
        <div class="row"><button class="btn primary">Upload customers</button><a href="#" data-tpl="customers" class="small">Download CSV template</a></div></form>
        <p class="tiny muted" style="margin-top:8px">Fields: customer type, business activity, institution risk category, KYC status, tenure, products, expected monthly turnover / transactions, account purpose.</p>`)}
      ${card("2 · Import historical transactions", `<form id="ft" class="stack" style="gap:10px"><input class="input" type="file" name="file" accept=".csv,.json,.jsonl" required>
        <label class="f">Field mapping (optional JSON: source field → VeriShield field)<input class="input mono" name="field_mapping" placeholder='{"cust_no": "user_id", "value": "amount"}'></label>
        <div class="row"><button class="btn primary">Upload history</button><a href="#" data-tpl="transactions" class="small">Download CSV template</a></div></form>
        <p class="tiny muted" style="margin-top:8px">Stored for training only (no decisions). Rejected rows go to the dead-letter queue with reasons.</p>`)}
    </div>
    <div class="notice" style="margin-top:16px">3 · Then build segments and train the fraud model (tabs above). Review thresholds with the AML Manager and run in shadow/monitoring mode before enforcement.</div>
    <div id="imp" style="margin-top:12px"></div>` : ""}`;
  box.querySelectorAll("[data-tpl]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); api.download(`/onboarding/templates/${a.dataset.tpl}`).catch(errToast); }));
  const up = (id, path) => {
    const f = box.querySelector(id);
    if (!f) return;
    f.onsubmit = (e) => { e.preventDefault();
      const fd = new FormData(); fd.append("file", f.file.files[0]);
      if (f.field_mapping) fd.append("field_mapping", f.field_mapping.value);
      busy(f.querySelector("button"), async () => {
        try {
          const r = await api.upload(path, fd);
          toast("Import finished");
          box.querySelector("#imp").innerHTML = `<div class="notice ok">Import result: ${esc(JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k]) => !["data_quality", "errors", "decisions"].includes(k)))))}${r.errors && r.errors.length ? `<br>${r.errors.map(esc).join("<br>")}` : ""}</div>`;
          setTimeout(redraw, 1500);
        } catch (err) { errToast(err); }
      });
    };
  };
  up("#fc", "/onboarding/customers");
  up("#ft", "/onboarding/transactions");
}

// Decision console: send a transaction through the real decision API; Demo scenario injector.
import { api } from "../api.js";
import { S, busy, card, chip, codes, decisionChip, errToast, esc, fmtMoney, fmtScore, formData, kv, label, options,
  scoreBar, table, toast } from "../ui.js";
import { openTransaction } from "./transactions.js";

export async function render(el) {
  const demo = S.me.tenant.demo_mode;
  let info = { scenarios: {} };
  try { info = await api.get("/demo"); } catch { /* ignore */ }
  let customers = [];
  try { customers = (await api.get("/customers?page_size=60&history=established")).items.map((c) => c.customer_ref).filter((r) => !r.includes("•")); } catch { /* masked */ }
  const now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  el.innerHTML = `
    ${demo ? "" : '<div class="notice warn" style="margin-bottom:16px">You are in the <b>live</b> workspace: test decisions are stored as real decisions and audited. Prefer Demo Mode for experiments.</div>'}
    <div class="grid g2">
      ${card("Evaluate a transaction", `<form id="f" class="stack" style="gap:12px">
        <div class="form-grid">
          <label class="f">Transaction ID<input class="input" name="transaction_id" value="UI-${Date.now().toString(36).toUpperCase()}" required></label>
          <label class="f">Customer<input class="input" name="customer_id" list="custs" value="${esc(customers[0] || "C00001")}" required><datalist id="custs">${customers.map((c) => `<option value="${esc(c)}">`).join("")}</datalist></label>
          <label class="f">Product<select class="input" name="transaction_type">${options(["bank_transfer", "phone_recharge", "prepaid_card"], "bank_transfer")}</select></label>
          <label class="f">Amount (${esc(S.me.tenant.currency)})<input class="input" name="amount" type="number" step="0.01" min="0.01" value="1250" required></label>
          <label class="f">Channel<select class="input" name="channel">${options(["mobile_banking", "internet_banking", "branch", "atm_kiosk", "api"], "mobile_banking")}</select></label>
          <label class="f">Timestamp<input class="input" type="datetime-local" name="timestamp" value="${now}"></label>
          <label class="f">Beneficiary (IBAN / phone / card)<input class="input" name="beneficiary_reference" value="PS92PALS000000000400123456702"></label>
          <label class="f">Beneficiary country<input class="input" name="beneficiary_country" value="PS" maxlength="2"></label>
          <label class="f">IP address<input class="input" name="ip_address" value="213.6.10.20"></label>
          <label class="f">IP country<input class="input" name="ip_country" value="PS" maxlength="2"></label>
          <label class="f">ASN<input class="input" name="asn" value="AS12975"></label>
          <label class="f">Idempotency key<input class="input" name="idempotency_key" placeholder="defaults to transaction ID"></label>
        </div>
        <div class="row"><button class="btn primary">Evaluate (pre-authorisation)</button><button type="button" class="btn" id="risky">Fill a risky example</button></div>
      </form>`, { sub: "POST /api/v1/decisions/evaluate" })}
      <div id="result">${card("Result", '<p class="muted">Submit a transaction to see the decision, scores, reason codes and explanation.</p>')}</div>
    </div>
    ${demo && info.can_inject ? `<div style="margin-top:16px">${card("Demo scenarios", `<p class="small muted" style="margin-bottom:12px">Generate a synthetic scenario now and send it through the real decision pipeline (Demo workspace only).</p>
      <div class="row" style="margin-bottom:12px"><label class="f" style="min-width:220px">Customer (optional)<input class="input" id="sc-cust" list="custs" placeholder="random"></label>
      <label class="f">Timing<select class="input" id="sc-mode">${options([["PRE_AUTH", "Pre-authorisation"], ["POST_EVENT", "Post-event"]], "PRE_AUTH")}</select></label></div>
      <div class="row">${Object.entries(info.scenarios).map(([k, v]) => `<button class="btn" data-sc="${esc(k)}" title="${esc(v)}">${esc(label(k))}</button>`).join("")}</div>
      <div id="sc-out" style="margin-top:14px"></div>`)}</div>` : ""}
    <div style="margin-top:16px">${card("Call it from the bank's systems", `<p class="small muted">Create an API client with the <span class="code">decisions:write</span> scope under Users &amp; Settings › API clients, then:</p>
<pre class="box">curl -X POST ${esc(location.origin)}/api/v1/decisions/evaluate \\
  -H "X-API-Key: vs_xxxxxxxx.yyyyyyyy" -H "Content-Type: application/json" \\
  -d '{"transaction_id":"TX-1","customer_id":"C00012","transaction_type":"bank_transfer",
       "amount":1250,"currency":"${esc(S.me.tenant.currency)}","channel":"mobile_banking",
       "beneficiary_reference":"PS92PALS000000000400123456702","beneficiary_country":"PS"}'

# the bank reports the real execution status (hold acknowledged, released, declined, settled):
curl -X POST ${esc(location.origin)}/api/v1/transactions/TX-1/execution-result \\
  -H "X-API-Key: ..." -H "Content-Type: application/json" -d '{"state":"HELD"}'</pre>
<p class="small muted" style="margin-top:8px">Full contract in the <a href="/api/docs" target="_blank" rel="noopener">OpenAPI documentation</a>.</p>`)}</div>`;

  const f = el.querySelector("#f");
  el.querySelector("#risky").onclick = () => {
    f.amount.value = "18500"; f.beneficiary_reference.value = "KY71CAYB0000000000000000123456"; f.beneficiary_country.value = "KY";
    f.ip_address.value = "95.24.13.7"; f.ip_country.value = "RU"; f.asn.value = "AS12389"; f.transaction_id.value = `UI-${Date.now().toString(36).toUpperCase()}`;
  };
  f.onsubmit = (e) => {
    e.preventDefault();
    const v = formData(f);
    Object.keys(v).forEach((k) => { if (v[k] === "" || v[k] === null) delete v[k]; });
    if (v.timestamp) v.timestamp = new Date(v.timestamp).toISOString();
    busy(f.querySelector("button"), async () => {
      try {
        const r = await api.post("/decisions/evaluate", v);
        showResult(el.querySelector("#result"), r);
        f.transaction_id.value = `UI-${Date.now().toString(36).toUpperCase()}`;
      } catch (err) { errToast(err); }
    });
  };
  el.querySelectorAll("[data-sc]").forEach((b) => (b.onclick = () => busy(b, async () => {
    try {
      const r = await api.post("/demo/scenarios", { scenario: b.dataset.sc, customer_ref: el.querySelector("#sc-cust").value || null, mode: el.querySelector("#sc-mode").value });
      toast(`${r.results.length} synthetic transaction(s) decided`);
      const out = el.querySelector("#sc-out");
      out.innerHTML = table([{ label: "Transaction", render: (x) => `<a href="#" data-open="${esc(x.transaction_id)}" class="mono small">${esc(x.transaction_id)}</a>` },
        { label: "Customer", key: "customer_id" }, { label: "Amount", num: true, render: (x) => fmtMoney(x.amount) }, { label: "Score", num: true, render: (x) => fmtScore(x.fraud_score) },
        { label: "Decision", render: (x) => decisionChip(x.decision) }, { label: "AML", render: (x) => chip(x.aml) }, { label: "Reason codes", render: (x) => codes(x.reason_codes.slice(0, 5)) }], r.results);
      out.querySelectorAll("[data-open]").forEach((a) => (a.onclick = (ev) => { ev.preventDefault(); openTransaction(a.dataset.open); }));
    } catch (e) { errToast(e); }
  })));
}

function showResult(box, r) {
  const tone = { PASS: "ok", REVIEW: "", HOLD: "warn", DECLINE: "crit", ERROR_POLICY: "crit" }[r.decision];
  box.innerHTML = card(`Decision ${r.decision}`, `
    <div class="notice ${tone}" style="margin-bottom:12px">${decisionChip(r.decision)} ${r.idempotent_replay ? chip("IDEMPOTENT REPLAY", "neutral") : ""}
      <span class="small">${{ PASS: "No configured control requires intervention.", REVIEW: "Alert raised for analyst review; payment treatment follows policy.",
        HOLD: "A clearance is required before execution – the bank keeps the payment pending.", DECLINE: "Reject the payment attempt under the approved policy.",
        ERROR_POLICY: `No reliable decision; institution fallback ${esc(r.fallback || "")} applies.` }[r.decision]}</span></div>
    <div class="scorebars">${scoreBar(r.fraud_score, "Fraud score")}</div>
    <div style="margin-top:12px">${kv([["Risk level", chip(r.risk_level)], ["AML signal", chip(r.aml_signal_level)], ["Customer risk context", chip(r.customer_risk_context)],
      ["Execution required", r.execution_required ? "Yes – bank must hold/decline" : "No"], ["Execution state", chip(r.execution_state)],
      ["Policy / model", `<span class="mono small">${esc(r.policy_version)} · ${esc(r.fraud_model_version)}</span>`], ["Decision ID", `<span class="mono small">${esc(r.decision_id)}</span>`],
      ["Expires", esc(r.expires_at)], ["Correlation", `<span class="mono small">${esc(r.correlation_id)}</span>`]])}</div>
    ${r.reason_details && r.reason_details.length ? `<h3 style="margin:14px 0 6px">Reason codes</h3><div class="tbl-wrap"><table class="tbl"><tbody>${r.reason_details.map((x) => `<tr><td class="mono small">${esc(x.code)}</td><td>${esc(x.text)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    ${r.explanation ? `<h3 style="margin:14px 0 6px">Explanation</h3><pre class="box" style="white-space:pre-wrap">${esc(r.explanation)}</pre>` : ""}
    <div class="row" style="margin-top:12px"><button class="btn" id="open-tx">Open transaction</button></div>`);
  box.querySelector("#open-tx").onclick = () => openTransaction(r.transaction_id);
}

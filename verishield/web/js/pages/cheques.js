// Cheques: deposits and own cheques, returned (bounced) cheques, drawers with repeated returns.
import { api } from "../api.js";
import { card, chip, errToast, esc, fmtDate, fmtNum, kpi, loading, modal, options, table, toast } from "../ui.js";

export async function render(el) {
  let days = 90;
  const draw = async () => {
    el.innerHTML = loading();
    const d = await api.get(`/cheques/summary?days=${days}`);
    const reasons = Object.entries(d.reasons || {}).sort((a, b) => b[1] - a[1]);
    el.innerHTML = `<div class="kpis">${kpi("Cheques deposited", fmtNum(d.deposits), `last ${days} days`)}${kpi("Own cheques paid", fmtNum(d.payments), `last ${days} days`)}
        ${kpi("Returned unpaid", fmtNum(d.returned), d.deposits + d.payments ? `${((100 * d.returned) / (d.deposits + d.payments)).toFixed(1)}% of cheques` : "", d.returned ? "warn" : "")}
        ${kpi("Drawers with returns", fmtNum(d.top_drawers.length), "accounts whose cheques bounced", d.top_drawers.some((x) => x.returned >= 2) ? "crit" : "")}</div>
      <div class="grid g2" style="margin-top:16px">
        ${card("What VeriShield checks on every cheque", `<ul class="small" style="margin:0;padding-left:18px;line-height:1.7">
          <li><b>Duplicate presentment</b> - same drawer account and cheque number already deposited → hold</li>
          <li><b>Cheque book sequence</b> - own cheque far outside the customer's recent numbers (stolen book) → review</li>
          <li><b>Bounced-cheque drawer</b> - drawer with 2+ returns in 12 months → review, 4+ → hold</li>
          <li><b>Post-dated / stale</b> - presented before its date or older than 6 months → review</li>
          <li><b>Kiting</b> - money taken out within 2 days of depositing uncleared cheques from new drawers, well above the customer's normal outflow → hold</li>
          <li>The behavioural profile also learns each customer's usual drawers, drawer banks, branches and amounts.</li></ul>`,
          { sub: "Thresholds and actions are in the decision policy (signals section)." })}
        ${card("Return reasons", table([{ label: "Reason", render: (r) => esc(d.reasons_catalogue[r[0]] || r[0]) }, { label: "Returns", num: true, render: (r) => fmtNum(r[1]) }], reasons,
          { empty: "No returned cheques in this period." }), { flush: true })}
      </div>
      <div class="grid g2" style="margin-top:16px">
        ${card("Drawer accounts with returned cheques", table([{ label: "Drawer account", render: (x) => `<span class="mono small">${esc(x.account)}</span>` },
          { label: "Returned", num: true, render: (x) => (x.returned >= 2 ? `<b style="color:var(--red)">${x.returned}</b>` : x.returned) }], d.top_drawers,
          { empty: "None." }), { flush: true })}
        ${card("Recent returns", table([{ label: "Returned", render: (r) => fmtDate(r.returned_at, { time: false }) },
          { label: "Cheque", render: (r) => `<span class="mono small">${esc(r.cheque_number || "-")}</span><div class="tiny muted">${esc(r.drawer_account)}</div>` },
          { label: "Customer", render: (r) => (r.customer_ref ? `<a href="#/customers/${encodeURIComponent(r.customer_ref)}">${esc(r.customer_ref)}</a> ${chip(r.role, r.role === "DRAWER" ? "warn" : "neutral")}` : "-") },
          { label: "Amount", num: true, render: (r) => fmtNum(r.amount, 2) }, { label: "Reason", render: (r) => esc(r.reason) }], d.recent_returns, { empty: "None." }),
          { flush: true, actions: `<select class="input" id="days" style="width:auto">${options([[30, "30 days"], [90, "90 days"], [365, "12 months"]], days)}</select>${d.can_manage ? '<button class="btn sm primary" id="add">Record a returned cheque</button>' : ""}` })}
      </div>`;
    el.querySelector("#days").onchange = (e) => { days = +e.target.value; draw(); };
    const add = el.querySelector("#add");
    if (add) add.onclick = async () => {
      const r = await modal({ title: "Record a returned cheque", body: `<div class="form-grid">
          <label class="f">Deposit transaction (optional)<input class="input" name="transaction_id" placeholder="e.g. MB-2026-118204"></label>
          <label class="f">Drawer account<input class="input mono" name="drawer_account" placeholder="PS.. IBAN of the drawer"></label>
          <label class="f">Cheque number<input class="input" name="cheque_number"></label>
          <label class="f">Amount<input class="input" type="number" step="0.01" name="amount"></label>
          <label class="f">Customer (our customer involved)<input class="input" name="customer_id"></label>
          <label class="f">Role<select class="input" name="role">${options([["DEPOSITOR", "Our customer deposited it"], ["DRAWER", "Our customer issued it"]], "DEPOSITOR")}</select></label>
          <label class="f">Reason<select class="input" name="reason">${options(Object.entries(d.reasons_catalogue), "INSUFFICIENT_FUNDS")}</select></label></div>
        <p class="tiny muted" style="margin-top:8px">The clearing system can also send returns automatically as <span class="code">record_type: "cheque_return"</span> events.</p>`,
        actions: [{ label: "Cancel" }, { label: "Record", cls: "primary", onClick: (m) => {
          const v = {}; m.querySelectorAll("[name]").forEach((i) => { if (i.value) v[i.name] = i.value; });
          return api.post("/cheques/returns", v);
        } }] });
      if (r) { toast("Returned cheque recorded"); draw(); }
    };
  };
  await draw().catch(errToast);
}

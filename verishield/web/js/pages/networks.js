// Networks of linked customers (rings) with risk propagated from confirmed fraud, and suspected mule accounts.
import { api } from "../api.js";
import { S, busy, card, chip, errToast, esc, fmtNum, fmtRel, kpi, loading, modal, table, toast } from "../ui.js";
import { showNetwork } from "./customers.js";

const can = (p) => S.me.permissions.includes(p);
const tone = (s) => (s >= 0.75 ? "crit" : s >= 0.45 ? "warn" : "neutral");

export async function render(el, { params = {} } = {}) {
  let cur = params.tab || "rings";
  const draw = () => {
    el.innerHTML = `<div class="tabs">${[["rings", "Rings of linked customers"], ["mules", "Suspected mule accounts"]]
      .map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    (cur === "rings" ? rings : mules)(el.querySelector("#tab")).catch(errToast);
  };
  draw();
}

async function rings(box) {
  const d = await api.get("/networks/rings");
  box.innerHTML = `<div class="kpis">${kpi("Rings", fmtNum(d.rings.length), "groups of 2+ linked customers")}
      ${kpi("Rings with confirmed risk", fmtNum(d.risky), "fraud, STR, watch list or sanctions", d.risky ? "crit" : "")}
      ${kpi("Customers in risky rings", fmtNum(d.customers_in_risky_rings), "", d.customers_in_risky_rings ? "warn" : "")}
      ${kpi("Last computed", d.computed_at ? fmtRel(d.computed_at) : "never", "every 30 minutes on the leader node")}</div>
    <div style="margin-top:16px">${card("Rings by risk", table([
      { label: "Ring", render: (r) => `<b class="mono small">${esc(r.ring_id)}</b>` },
      { label: "Customers", num: true, render: (r) => fmtNum(r.size) },
      { label: "Confirmed risk", num: true, render: (r) => r.seeded ? `<b style="color:var(--red)">${r.seeded}</b>` : "0" },
      { label: "Highest score", render: (r) => chip(r.max_score.toFixed(2), tone(r.max_score)) },
      { label: "Members", render: (r) => r.members.slice(0, 5).map((m) => `<a href="#/customers/${encodeURIComponent(m.customer_ref)}">${esc(m.customer_ref)}</a>`).join(", ") + (r.members.length > 5 ? ` +${r.members.length - 5}` : "") },
      { label: "", render: (r) => `<button class="btn sm" data-ring="${esc(r.ring_id)}">Details</button>` },
    ], d.rings, { empty: "No rings yet - compute the network scores." }), { flush: true,
      sub: "Customers are linked when they transfer to the same account, use the same device or connect from the same IP. Popular payees, shops, ATMs and values shared by more than 25 customers are left out.",
      actions: d.can_manage ? '<button class="btn sm primary" id="re">Recompute now</button>' : "" })}</div>`;
  const re = box.querySelector("#re");
  if (re) re.onclick = () => busy(re, async () => { try { const r = await api.post("/networks/recompute"); toast(`${r.rings} ring(s), ${r.seeds} customer(s) with confirmed risk`); rings(box); } catch (e) { errToast(e); } });
  box.querySelectorAll("[data-ring]").forEach((b) => (b.onclick = () => ringDetail(d.rings.find((r) => r.ring_id === b.dataset.ring))));
}

function ringDetail(r) {
  const body = card(`${r.ring_id} - ${r.size} linked customers`, table([
    { label: "Customer", render: (m) => `<a href="#/customers/${encodeURIComponent(m.customer_ref)}">${esc(m.customer_ref)}</a><div class="tiny muted">${esc(m.name)}</div>` },
    { label: "Network risk", render: (m) => chip(m.score.toFixed(2), tone(m.score)) },
    { label: "Hops to confirmed risk", render: (m) => (m.hops === null || m.hops === undefined ? "-" : m.hops === 0 ? chip("SOURCE", "crit") : m.hops) },
    { label: "Why", render: (m) => `<span class="small">${esc((m.reasons || []).join("; "))}</span>` },
    { label: "", render: (m) => `<button class="btn sm" data-g="${esc(m.customer_ref)}">Graph</button>` },
  ], r.members), { flush: true });
  modal({ title: "Ring details", wide: true, body, actions: [{ label: "Close" }],
    onOpen: (m) => m.querySelectorAll("[data-g]").forEach((b) => (b.onclick = () => showNetwork(b.dataset.g))) });
}

async function mules(box) {
  const d = await api.get("/networks/mules");
  box.innerHTML = card("Accounts receiving money from many new senders", table([
    { label: "Account", render: (a) => `<span class="mono small">${esc(a.account)}</span>${a.watched ? " " + chip("WATCH-LISTED", "crit") : ""}` },
    { label: "Senders (30 days)", num: true, render: (a) => fmtNum(a.senders) },
    { label: "New senders", num: true, render: (a) => fmtNum(a.new_senders) },
    { label: "Confirmed fraud victims", num: true, render: (a) => (a.fraud_senders ? `<b style="color:var(--red)">${a.fraud_senders}</b>` : "0") },
    { label: "Received", num: true, render: (a) => fmtNum(a.amount, 2) },
    { label: "Mule score", render: (a) => chip(a.score.toFixed(2), tone(a.score)) },
    { label: "", render: (a) => (can("watchlist.manage") && !a.watched ? `<button class="btn sm danger" data-w="${esc(a.account)}">Watch-list</button>` : "") },
  ], d.accounts, { empty: "No suspected mule accounts in the last 30 days." }), { flush: true,
    sub: "Scored live in every decision: payments to an account with a mule score of 0.45+ go to review, 0.75+ are held (policy signals). Senders who already paid the account before are treated as established relationships." });
  box.querySelectorAll("[data-w]").forEach((b) => (b.onclick = () => busy(b, async () => {
    try { await api.post("/watchlist", { kind: "IBAN", value: b.dataset.w, reason: "Suspected mule account (network analysis)" }); toast("Added to the watch list"); mules(box); } catch (e) { errToast(e); }
  })));
}

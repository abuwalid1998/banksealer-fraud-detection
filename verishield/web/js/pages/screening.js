// Sanctions & PEP screening, list import and the fraud watch list.
import { api } from "../api.js";
import { S, busy, card, chip, confirmDialog, errToast, esc, fmtDate, fmtNum, fmtRel, kpi, label, loading, modal, options,
  table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);

export async function render(el, { params = {} } = {}) {
  const tabs = [];
  if (can("screening.manage") || can("screening.view")) tabs.push(["matches", "Screening matches"], ["check", "Name check"], ["lists", "Sanctions & PEP lists"]);
  if (can("watchlist.manage") || can("alerts.view_all")) tabs.push(["watch", "Fraud watch list"]);
  let cur = params.tab || tabs[0][0];
  const draw = () => {
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${k === cur ? "on" : ""}" data-t="${k}">${l}</button>`).join("")}</div><div id="tab">${loading()}</div>`;
    el.querySelectorAll("[data-t]").forEach((b) => (b.onclick = () => { cur = b.dataset.t; draw(); }));
    const box = el.querySelector("#tab");
    ({ matches, check, lists, watch })[cur](box).catch(errToast);
  };
  draw();
}

async function matches(box, status = "OPEN") {
  const d = await api.get(`/screening/matches?status=${status}`);
  const c = d.counts;
  box.innerHTML = `<div class="kpis">${kpi("Open matches", fmtNum(c.OPEN || 0), "to review", c.OPEN ? "warn" : "")}${kpi("True matches", fmtNum(c.TRUE_MATCH || 0), "confirmed", c.TRUE_MATCH ? "crit" : "")}
      ${kpi("False positives", fmtNum(c.FALSE_POSITIVE || 0), "remembered – not raised again")}</div>
    <div style="margin-top:16px">${card("Matches", table([
      { label: "When", render: (m) => fmtRel(m.created_at) }, { label: "Subject", render: (m) => `${chip(m.subject_type, "neutral")} <b>${esc(m.subject_name)}</b>${m.customer_ref ? `<div class="tiny muted">customer <a href="#/customers/${encodeURIComponent(m.customer_ref)}">${esc(m.customer_ref)}</a>${m.tx_ref ? ` · payment ${esc(m.tx_ref)}` : ""}</div>` : ""}` },
      { label: "List", render: (m) => chip(m.list_code, m.list_code === "PEP" ? "warn" : "crit") }, { label: "Listed name", render: (m) => esc(m.matched_name) },
      { label: "Similarity", num: true, render: (m) => `<b>${Number(m.score).toFixed(2)}</b>` }, { label: "Status", render: (m) => chip(m.status) + (m.note ? `<div class="tiny muted">${esc(m.note)}</div>` : "") },
      { label: "", render: (m) => m.status === "OPEN" && (can("screening.manage") || can("alerts.triage")) ? `<div class="row nowrap"><button class="btn sm danger" data-res="${m.id}" data-s="TRUE_MATCH">True match</button><button class="btn sm" data-res="${m.id}" data-s="FALSE_POSITIVE">False positive</button></div>` : "" },
    ], d.matches, { empty: status === "OPEN" ? "No open matches." : "Nothing here." }), { flush: true,
      actions: `<select class="input" id="st" style="width:auto">${options([["OPEN", "Open"], ["TRUE_MATCH", "True matches"], ["FALSE_POSITIVE", "False positives"], ["", "All"]], status)}</select>${can("screening.manage") ? '<button class="btn primary sm" id="run">Screen all customers now</button>' : ""}`,
      sub: "Customers are screened when lists change; payment counterparties are screened in real time" })}</div>`;
  box.querySelector("#st").onchange = (e) => matches(box, e.target.value);
  const run = box.querySelector("#run");
  if (run) run.onclick = () => busy(run, async () => { try { const r = await api.post("/screening/run", {}); toast(`${r.customers} customers screened · ${r.new_matches} new match(es)`); matches(box, status); } catch (e) { errToast(e); } });
  box.querySelectorAll("[data-res]").forEach((b) => (b.onclick = async () => {
    const r = await modal({ title: b.dataset.s === "TRUE_MATCH" ? "Confirm true match" : "Mark as false positive",
      body: `<p class="small">${b.dataset.s === "TRUE_MATCH" ? "A customer true match opens an urgent KYC review; a counterparty true match is added to the watch list so payments to it are held." : "The match is closed and will not be raised again for this subject and list entry."}</p>
        <label class="f" style="margin-top:10px">Reason / evidence<textarea class="input" name="note" required></textarea></label>`,
      actions: [{ label: "Cancel" }, { label: "Confirm", cls: b.dataset.s === "TRUE_MATCH" ? "danger" : "primary", onClick: (m) => api.post(`/screening/matches/${b.dataset.res}/resolve`, { status: b.dataset.s, note: m.querySelector("[name=note]").value }) }] });
    if (r) { toast("Recorded"); matches(box, status); }
  }));
}

async function check(box) {
  box.innerHTML = card("Check a name", `<form id="cf" class="row" style="align-items:flex-end"><label class="f" style="flex:1">Name (Arabic or Latin script)<input class="input" name="name" placeholder="e.g. محمد عبد الرحمن or Mohammed Abdul Rahman" required></label>
      <label class="f">Account / ID (optional)<input class="input mono" name="identifier"></label><button class="btn primary">Screen</button></form><div id="cres" style="margin-top:14px"></div>
      <p class="tiny muted" style="margin-top:10px">Names are normalised (case, accents, honorifics, Arabic transliteration, Mohammad / Mohammed / Muhammad …) and compared word by word; every check is audited.</p>`);
  const f = box.querySelector("#cf");
  f.onsubmit = (e) => { e.preventDefault(); busy(f.querySelector("button"), async () => {
    try {
      const r = await api.post("/screening/check", { name: f.name.value, identifier: f.identifier.value || null });
      box.querySelector("#cres").innerHTML = `<p class="small">Normalised: <span class="code">${esc(r.normalised)}</span></p>` + table([
        { label: "List", render: (m) => chip(m.list_code, m.list_code === "PEP" ? "warn" : "crit") }, { label: "Listed name", render: (m) => `<b>${esc(m.name)}</b>` },
        { label: "Matched on", key: "matched_on" }, { label: "Similarity", num: true, render: (m) => Number(m.score).toFixed(2) }], r.matches, { empty: "No match above 0.80 – clear." });
    } catch (err) { errToast(err); } }); };
}

async function lists(box) {
  const d = await api.get("/screening/lists");
  box.innerHTML = `<div class="grid g-21">${card("Loaded lists", table([{ label: "List", render: (l) => `${chip(l.code, "neutral")} ${esc(l.name)}` },
      { label: "Entries", num: true, render: (l) => fmtNum(l.entries) }, { label: "Loaded", render: (l) => l.loaded_at ? fmtDate(l.loaded_at) : "–" }], d.lists, { empty: "No lists loaded yet." }), { flush: true })}
    ${d.can_manage ? card("Import a list (offline)", `<form id="lf" class="stack" style="gap:10px">
      <label class="f">List<select class="input" name="list_code">${options(Object.entries(d.codes).map(([k, v]) => [k, `${k} – ${v}`]))}</select></label>
      <label class="f">File format<select class="input" name="fmt">${options(Object.entries(d.formats).map(([k, v]) => [k, v]))}</select></label>
      <label class="f">File<input class="input" type="file" name="file" required></label>
      <label class="check"><input type="checkbox" name="replace" checked> Replace the current entries of this list</label>
      <button class="btn primary">Import</button></form>
      <p class="tiny muted" style="margin-top:10px">Download the official files on a PC with internet (UN, OFAC, EU) and import them here – the server needs no internet. Customers are re-screened after import.</p>`) : ""}</div>`;
  const f = box.querySelector("#lf");
  if (f) f.onsubmit = (e) => { e.preventDefault(); const fd = new FormData();
    fd.append("list_code", f.list_code.value); fd.append("fmt", f.fmt.value); fd.append("replace", f.replace.checked ? "true" : "false"); fd.append("file", f.file.files[0]);
    busy(f.querySelector("button"), async () => { try { const r = await api.upload("/screening/lists", fd); toast(`${fmtNum(r.entries)} entries imported`);
      const s = await api.post("/screening/run", {}); toast(`${s.new_matches} new match(es) after re-screening`); lists(box); } catch (err) { errToast(err); } }); };
}

async function watch(box) {
  const d = await api.get("/watchlist");
  box.innerHTML = card("Fraud watch list", table([
    { label: "Kind", render: (w) => chip(w.kind, "neutral") }, { label: "Value", render: (w) => `<span class="mono">${esc(w.value)}</span>` },
    { label: "Action", render: (w) => chip(w.action) }, { label: "Reason", render: (w) => `<span class="small">${esc(w.reason)}</span>` },
    { label: "Source", render: (w) => `${esc(label(w.source))}${w.source_ref ? `<div class="tiny muted">${esc(w.source_ref)}</div>` : ""}` },
    { label: "Hits", num: true, render: (w) => fmtNum(w.hits) }, { label: "Last hit", render: (w) => w.last_hit_at ? fmtRel(w.last_hit_at) : "–" },
    { label: "Expires", render: (w) => w.expires_at ? fmtDate(w.expires_at, { time: false }) : "never" },
    { label: "", render: (w) => d.can_manage ? `<button class="btn sm danger" data-del="${w.id}">Remove</button>` : "" },
  ], d.entries, { empty: "The watch list is empty." }), { flush: true, actions: d.can_manage ? '<button class="btn primary sm" id="add">Add entry</button>' : "",
    sub: "Payments to, from or through a listed IBAN, phone, card, merchant, device or IP are stopped automatically. Confirmed fraud cases, customer reports and 'NO' SMS answers add entries." });
  const add = box.querySelector("#add");
  if (add) add.onclick = async () => {
    const r = await modal({ title: "Add to the watch list", body: `<div class="form-grid">
      <label class="f">Kind<select class="input" name="kind">${options(d.kinds)}</select></label><label class="f">Value<input class="input mono" name="value" required></label>
      <label class="f">Action<select class="input" name="action">${options(["HOLD", "DECLINE", "REVIEW"], "HOLD")}</select></label>
      <label class="f">Expires after (days)<input class="input" type="number" name="days" placeholder="never"></label></div>
      <label class="f" style="margin-top:10px">Reason<textarea class="input" name="reason" required></textarea></label>`,
      actions: [{ label: "Cancel" }, { label: "Add", cls: "primary", onClick: (m) => api.post("/watchlist", { kind: m.querySelector("[name=kind]").value, value: m.querySelector("[name=value]").value,
        action: m.querySelector("[name=action]").value, days: Number(m.querySelector("[name=days]").value) || null, reason: m.querySelector("[name=reason]").value }) }] });
    if (r) { toast("Added"); watch(box); }
  };
  box.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Remove from watch list", "Payments to this value will no longer be stopped automatically.", "Remove", "danger"))) return;
    try { await api.del(`/watchlist/${b.dataset.del}`); toast("Removed"); watch(box); } catch (e) { errToast(e); }
  }));
}

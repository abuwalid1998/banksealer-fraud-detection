// Report catalogue, generation and download; AML report wording for the Manager.
import { api } from "../api.js";
import { S, busy, card, chip, errToast, esc, fmtDate, fmtNum, label, loading, options, table, toast } from "../ui.js";

const can = (p) => S.me.permissions.includes(p);
const PARAM_LABEL = { tx_ref: "Transaction ID", customer_ref: "Customer reference", case_id: "Case ID" };

export async function render(el) {
  const cat = await api.get("/reports/catalogue");
  const from = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
  const to = new Date().toISOString().slice(0, 10);
  el.innerHTML = `<div class="grid g-21">
    ${card("Generate a report", `<form id="gen" class="stack" style="gap:12px">
      <label class="f">Report<select class="input" name="type">${options(cat.reports.map((r) => [r.id, r.title]))}</select></label>
      <div id="params" class="form-grid"></div>
      <div class="form-grid">
        <label class="f">From<input class="input" type="date" name="date_from" value="${from}"></label>
        <label class="f">To<input class="input" type="date" name="date_to" value="${to}"></label>
        <label class="f">Product<select class="input" name="tx_type">${options(["bank_transfer", "phone_recharge", "prepaid_card"], "", { blank: "All" })}</select></label>
        <label class="f">Risk level<select class="input" name="risk_level">${options(["LOW", "MEDIUM", "HIGH", "CRITICAL"], "", { blank: "All" })}</select></label>
        <label class="f">Format<select class="input" name="format">${options([["pdf", "PDF (branded)"], ["xlsx", "Excel (XLSX)"], ["csv", "CSV"]], "pdf")}</select></label>
      </div>
      <div class="row"><button class="btn primary">Generate &amp; download</button><span class="small muted">${S.me.tenant.demo_mode ? "Demo Mode reports carry a DEMO DATA watermark." : "Every generation and download is audited."}</span></div>
    </form>`)}
    ${card("Available reports", `<ul style="margin:0;padding-left:18px" class="small">${cat.reports.map((r) => `<li>${esc(r.title)}</li>`).join("")}</ul>`)}
  </div>
  ${can("branding.reports") ? `<div style="margin-top:16px" id="brand">${loading()}</div>` : ""}
  <div class="card" style="margin-top:16px" id="jobs">${loading()}</div>`;
  const form = el.querySelector("#gen");
  const drawParams = () => {
    const r = cat.reports.find((x) => x.id === form.type.value);
    el.querySelector("#params").innerHTML = (r.params || []).map((p) => `<label class="f">${esc(PARAM_LABEL[p] || p)}<input class="input" name="${p}" required></label>`).join("");
  };
  form.type.onchange = drawParams; drawParams();
  form.onsubmit = (e) => {
    e.preventDefault();
    const params = {};
    form.querySelectorAll("#params [name], [name=date_from], [name=date_to], [name=tx_type], [name=risk_level]").forEach((i) => { if (i.value) params[i.name] = i.value; });
    busy(form.querySelector("button"), async () => {
      try {
        const j = await api.post("/reports", { type: form.type.value, format: form.format.value, params });
        toast(`Report ${j.number} generated`);
        await api.download(`/reports/${encodeURIComponent(j.id)}/download`);
        loadJobs();
      } catch (err) { errToast(err); }
    });
  };
  const loadJobs = async () => {
    const box = el.querySelector("#jobs");
    const d = await api.get("/reports");
    box.innerHTML = `<div class="hd"><h2>Generated reports</h2></div>` + table([
      { label: "Number", render: (r) => `<span class="mono small">${esc(r.number)}</span>` }, { label: "Report", key: "title" },
      { label: "Format", render: (r) => chip(r.fmt.toUpperCase(), "neutral") }, { label: "Status", render: (r) => chip(r.status === "DONE" ? "OK" : r.status, r.status === "DONE" ? "ok" : "crit") },
      { label: "Size", num: true, render: (r) => r.size ? `${fmtNum(r.size / 1024, 1)} KB` : "–" }, { label: "By", key: "created_by_name" },
      { label: "Created", render: (r) => fmtDate(r.created_at) },
      { label: "", render: (r) => r.status === "DONE" ? `<button class="btn sm" data-dl="${esc(r.id)}">Download</button>` : `<span class="small muted">${esc(r.error)}</span>` },
    ], d.reports, { empty: "No reports generated yet." });
    box.querySelectorAll("[data-dl]").forEach((b) => (b.onclick = () => api.download(`/reports/${encodeURIComponent(b.dataset.dl)}/download`).catch(errToast)));
  };
  loadJobs();
  if (can("branding.reports")) {
    const b = (await api.get("/branding")).branding;
    const box = el.querySelector("#brand");
    box.innerHTML = card("AML report wording", `<form id="bf" class="form-grid">
      <label class="f">AML report header<input class="input" name="aml_header_text" value="${esc(b.aml_header_text)}" placeholder="${esc(b.header_text)}"></label>
      <label class="f">AML report footer<input class="input" name="aml_footer_text" value="${esc(b.aml_footer_text)}" placeholder="${esc(b.footer_text)}"></label>
      <label class="f">Additional AML disclaimer<input class="input" name="disclaimer_addendum" value="${esc(b.disclaimer_addendum || "")}"></label>
      <div class="row" style="align-self:end"><button class="btn">Save wording</button></div></form>
      <p class="tiny muted" style="margin-top:8px">Logo, colours, confidentiality notice and the mandatory disclaimer are maintained by the IT Admin (tenant-wide branding).</p>`,
      { sub: "Applies to investigation, case register, SLA and held/declined reports" });
    const bf = box.querySelector("#bf");
    bf.onsubmit = (e) => { e.preventDefault();
      busy(bf.querySelector("button"), async () => { try { await api.put("/branding", { aml_header_text: bf.aml_header_text.value,
        aml_footer_text: bf.aml_footer_text.value, disclaimer_addendum: bf.disclaimer_addendum.value }); toast("AML report wording saved"); } catch (err) { errToast(err); } }); };
  }
}

export { label };

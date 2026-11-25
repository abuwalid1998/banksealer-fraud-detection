import { useEffect, useState } from "react";
import { api, TX_TYPES } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBox, Modal, useToast } from "../components/ui";
import { useLoad } from "../hooks";

const TH_FIELDS: [string, string, string][] = [
  ["suspicious", "Suspicious from score", "combined score ≥ this ⇒ MEDIUM risk / alert"],
  ["high", "HIGH risk from score", ""], ["critical", "CRITICAL risk from score", ""],
  ["local_alert", "Local profile alert level", "a single profile above this also raises an alert"],
  ["temporal_alert", "Temporal profile alert level", ""], ["global_alert", "Global profile alert level", ""],
  ["hard_rule_score", "Score when a limit is broken", ""],
  ["high_risk_country_boost", "High-risk country boost", "added to the score"], ["night_boost", "Night-time boost", "added to the score"],
];

function ThresholdForm({ value, base, onChange, disabled }: { value: any; base?: any; onChange: (v: any) => void; disabled?: boolean }) {
  const v = value || {};
  const inherited = (k: string) => base && JSON.stringify(base[k]) === JSON.stringify(v[k]);
  return (
    <div className="form-grid three">
      {TH_FIELDS.map(([k, label, hint]) => (
        <label key={k} className={inherited(k) ? "inherited" : ""}>{label}
          <input type="number" step="0.01" min={0} max={1.01} disabled={disabled} value={v[k] ?? ""} onChange={(e) => onChange({ ...v, [k]: e.target.value === "" ? null : Number(e.target.value) })} />
          {hint && <small>{hint}</small>}</label>
      ))}
      {Object.entries(TX_TYPES).map(([t, label]) => (
        <label key={t} className={base && JSON.stringify(base.max_amount?.[t]) === JSON.stringify(v.max_amount?.[t]) ? "inherited" : ""}>Max amount – {label}
          <input type="number" step="1" min={0} disabled={disabled} value={v.max_amount?.[t] ?? ""} placeholder="no limit"
            onChange={(e) => onChange({ ...v, max_amount: { ...(v.max_amount || {}), [t]: e.target.value === "" ? null : Number(e.target.value) } })} /></label>
      ))}
      <label className={inherited("high_risk_countries") ? "inherited span-2" : "span-2"}>High-risk countries (ISO codes)
        <input disabled={disabled} value={(v.high_risk_countries || []).join(", ")} onChange={(e) => onChange({ ...v, high_risk_countries: e.target.value.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean) })} /></label>
      <label className={inherited("night_hours") ? "inherited" : ""}>Night hours
        <input disabled={disabled} value={(v.night_hours || []).join(", ")} onChange={(e) => onChange({ ...v, night_hours: e.target.value.split(",").map((x) => parseInt(x.trim(), 10)).filter((x) => !isNaN(x)) })} /></label>
    </div>
  );
}

function diff(effective: any, defaults: any) {
  const out: any = {};
  Object.entries(effective).forEach(([k, val]: any) => {
    if (k === "max_amount") {
      const d: any = {};
      Object.entries(val || {}).forEach(([t, a]) => { if (a !== null && a !== (defaults.max_amount || {})[t]) d[t] = a; });
      if (Object.keys(d).length) out[k] = d;
    } else if (JSON.stringify(val) !== JSON.stringify(defaults[k])) out[k] = val;
  });
  return out;
}

function NumberField({ label, value, onChange, step = "0.01", hint }: any) {
  return <label>{label}<input type="number" step={step} value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />{hint && <small>{hint}</small>}</label>;
}

export default function Configuration() {
  const { can } = useAuth();
  const admin = can("admin");
  const { toast, node } = useToast();
  const { data, error, reload } = useLoad(() => api.get("/config"), []);
  const [tab, setTab] = useState("defaults");
  const [sections, setSections] = useState<any>(null);
  const [bank, setBank] = useState<any>(null);
  const [newBank, setNewBank] = useState<any>(null);
  const [yamlText, setYamlText] = useState("");
  const [includeChannels, setIncludeChannels] = useState(false);
  useEffect(() => { if (data) setSections(JSON.parse(JSON.stringify(data.sections))); }, [data]);
  if (!data || !sections) return <><h1>Banks &amp; thresholds</h1><ErrorBox error={error} /></>;

  const saveSection = async (key: string) => {
    try { await api.put(`/config/${key}`, sections[key]); toast(`${key} saved${["thresholds", "engine"].includes(key) ? " – transactions are being re-evaluated" : ""}`); reload(); }
    catch (e: any) { toast(e.message, "error"); }
  };
  const setSec = (key: string, value: any) => setSections({ ...sections, [key]: value });
  const saveBank = async () => {
    try {
      await api.put(`/banks/${bank.code}`, { code: bank.code, name: bank.name, active: bank.active, thresholds: diff(bank.effective, data.sections.thresholds) });
      toast(`${bank.code} saved – transactions are being re-evaluated`); setBank(null); reload();
    } catch (e: any) { toast(e.message, "error"); }
  };
  const eng = sections.engine, loc = sections.local, tmp = sections.temporal, glo = sections.global, app = sections.app;
  return (
    <>
      <h1>Banks &amp; thresholds</h1>
      <p className="muted">The configuration lives in the database. Changes apply to the running worker within seconds; threshold changes re-evaluate every scored transaction.</p>
      <div className="tabs">
        {[["defaults", "Default thresholds"], ["banks", "Banks / companies"], ["engine", "Engine & profiles"], ["app", "Application"], ["yaml", "Import / export"]].map(([k, l]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === "defaults" && (
        <Card title="Default thresholds (every bank/company without an override)" actions={admin && <button className="primary" onClick={() => saveSection("thresholds")}>Save</button>}>
          <ThresholdForm value={sections.thresholds} onChange={(v) => setSec("thresholds", v)} disabled={!admin} />
          {data.meta.thresholds?.updated_by && <p className="muted small">Last change {data.meta.thresholds.updated_at} by {data.meta.thresholds.updated_by}</p>}
        </Card>
      )}
      {tab === "banks" && (
        <Card title="Banks / companies" actions={admin && <button className="primary" onClick={() => setNewBank({ code: "", name: "" })}>+ Add bank</button>}>
          <table className="table">
            <thead><tr><th>Code</th><th>Name</th><th>Active</th><th>Suspicious</th><th>High</th><th>Critical</th><th>Max transfer</th><th>Overrides</th><th /></tr></thead>
            <tbody>{data.banks.map((b: any) => (
              <tr key={b.code}><td><b>{b.code}</b></td><td>{b.name}</td><td>{b.active ? "yes" : "no"}</td>
                <td>{b.effective.suspicious}</td><td>{b.effective.high}</td><td>{b.effective.critical}</td><td>{b.effective.max_amount?.bank_transfer ?? "-"}</td>
                <td className="small mono">{Object.keys(b.thresholds).join(", ") || "inherits defaults"}</td>
                <td>{admin && <><button className="small" onClick={() => setBank(JSON.parse(JSON.stringify(b)))}>Edit</button>{" "}
                  <button className="small ghost danger-text" onClick={async () => { if (confirm(`Delete ${b.code}?`)) { await api.del(`/banks/${b.code}`); reload(); } }}>Delete</button></>}</td></tr>
            ))}</tbody>
          </table>
        </Card>
      )}
      {tab === "engine" && (
        <div className="stack">
          <Card title="Combination of the three profiles" actions={admin && <button className="primary" onClick={() => saveSection("engine")}>Save</button>}>
            <div className="form-grid three">
              {["local", "temporal", "global"].map((p) => <NumberField key={p} label={`Weight – ${p}`} value={eng.profile_weights[p]} onChange={(x: number) => setSec("engine", { ...eng, profile_weights: { ...eng.profile_weights, [p]: x } })} />)}
              <NumberField label="Min. transactions before first training" step="1" value={eng.min_training_transactions} onChange={(x: number) => setSec("engine", { ...eng, min_training_transactions: x })} />
              <NumberField label="Refit peer groups every N scored" step="1" value={eng.retrain_global_every} onChange={(x: number) => setSec("engine", { ...eng, retrain_global_every: x })} />
              <NumberField label="Reasons listed per alert" step="1" value={eng.max_reasons} onChange={(x: number) => setSec("engine", { ...eng, max_reasons: x })} />
              <label className="check"><input type="checkbox" checked={eng.update_profiles_online} onChange={(e) => setSec("engine", { ...eng, update_profiles_online: e.target.checked })} /> Learn online from new transactions</label>
              <label className="check span-2"><input type="checkbox" checked={eng.exclude_suspicious_from_learning} onChange={(e) => setSec("engine", { ...eng, exclude_suspicious_from_learning: e.target.checked })} /> Never learn from suspicious transactions</label>
            </div>
          </Card>
          <Card title="Local profile (HBOS)" actions={admin && <button className="primary" onClick={() => saveSection("local")}>Save</button>}>
            <div className="form-grid three">
              <NumberField label="Undertrained below (past ops)" step="1" value={loc.min_history} onChange={(x: number) => setSec("local", { ...loc, min_history: x })} />
              <NumberField label="Shrinkage k (λ = n/(n+k))" step="1" value={loc.shrinkage_k} onChange={(x: number) => setSec("local", { ...loc, shrinkage_k: x })} />
              <NumberField label="Smoothing α (never-seen values)" value={loc.smoothing_alpha} onChange={(x: number) => setSec("local", { ...loc, smoothing_alpha: x })} />
              <NumberField label="Amount histogram bins" step="1" value={loc.amount_bins} onChange={(x: number) => setSec("local", { ...loc, amount_bins: x })} />
              <NumberField label="Score scale" step="0.5" value={loc.score_scale} onChange={(x: number) => setSec("local", { ...loc, score_scale: x })} hint="score = 1 − exp(−HBOS/scale)" />
              <NumberField label="Min. contribution to explain" step="0.1" value={loc.min_reason_contribution} onChange={(x: number) => setSec("local", { ...loc, min_reason_contribution: x })} />
            </div>
            <h4>Feature weights</h4>
            <table className="table compact"><thead><tr><th>Operation type</th>{["amount", "iban", "iban_cc", "phone_number", "operator", "card_id", "cc_asn", "ip", "hour"].map((f) => <th key={f}>{f}</th>)}</tr></thead>
              <tbody>{Object.entries(loc.feature_weights).map(([t, w]: any) => (
                <tr key={t}><td>{TX_TYPES[t]}</td>{["amount", "iban", "iban_cc", "phone_number", "operator", "card_id", "cc_asn", "ip", "hour"].map((f) => (
                  <td key={f}>{f in w ? <input className="tiny" type="number" step="0.1" value={w[f]} onChange={(e) => setSec("local", { ...loc, feature_weights: { ...loc.feature_weights, [t]: { ...w, [f]: Number(e.target.value) } } })} /> : <span className="muted">–</span>}</td>
                ))}</tr>))}</tbody></table>
          </Card>
          <Card title="Temporal profile" actions={admin && <button className="primary" onClick={() => saveSection("temporal")}>Save</button>}>
            <div className="form-grid three">
              <NumberField label="Window (hours)" step="1" value={tmp.window_hours} onChange={(x: number) => setSec("temporal", { ...tmp, window_hours: x })} />
              <NumberField label="Threshold k (mean + k·std)" step="0.1" value={tmp.sigma_multiplier} onChange={(x: number) => setSec("temporal", { ...tmp, sigma_multiplier: x })} />
              <NumberField label="Min. samples" step="1" value={tmp.min_windows} onChange={(x: number) => setSec("temporal", { ...tmp, min_windows: x })} />
              <NumberField label="Score scale" step="0.5" value={tmp.score_scale} onChange={(x: number) => setSec("temporal", { ...tmp, score_scale: x })} />
              {Object.entries(tmp.feature_weights).map(([f, w]: any) => <NumberField key={f} label={`Weight – ${f}`} step="0.1" value={w} onChange={(x: number) => setSec("temporal", { ...tmp, feature_weights: { ...tmp.feature_weights, [f]: x } })} />)}
            </div>
          </Card>
          <Card title="Global profile (iterative DBSCAN + CBLOF)" actions={admin && <button className="primary" onClick={() => saveSection("global")}>Save</button>}>
            <div className="form-grid three">
              <NumberField label="DBSCAN eps (Mahalanobis)" step="0.1" value={glo.eps} onChange={(x: number) => setSec("global", { ...glo, eps: x })} />
              <NumberField label="DBSCAN min samples" step="1" value={glo.min_samples} onChange={(x: number) => setSec("global", { ...glo, min_samples: x })} />
              <NumberField label="eps growth per iteration" step="0.1" value={glo.eps_growth} onChange={(x: number) => setSec("global", { ...glo, eps_growth: x })} />
              <NumberField label="Iterations" step="1" value={glo.max_iterations} onChange={(x: number) => setSec("global", { ...glo, max_iterations: x })} />
              <NumberField label="CBLOF α" value={glo.cblof_alpha} onChange={(x: number) => setSec("global", { ...glo, cblof_alpha: x })} />
              <NumberField label="CBLOF β" step="0.5" value={glo.cblof_beta} onChange={(x: number) => setSec("global", { ...glo, cblof_beta: x })} />
              <label className="check"><input type="checkbox" checked={glo.weight_by_cluster_size} onChange={(e) => setSec("global", { ...glo, weight_by_cluster_size: e.target.checked })} /> Weight by cluster size</label>
            </div>
            <p className="muted small">Profile hyper-parameters apply at the next training (Engine &amp; training › Retrain).</p>
          </Card>
        </div>
      )}
      {tab === "app" && (
        <Card title="Reports" actions={admin && <button className="primary" onClick={() => saveSection("app")}>Save</button>}>
          <div className="form-grid">
            <label className="check"><input type="checkbox" checked={!!app.auto_report} onChange={(e) => setSec("app", { ...app, auto_report: e.target.checked })} /> Generate a PDF report for every suspicious transaction</label>
            <label>From risk level<select value={app.auto_report_min_level} onChange={(e) => setSec("app", { ...app, auto_report_min_level: e.target.value })}>
              {["MEDIUM", "HIGH", "CRITICAL"].map((l) => <option key={l}>{l}</option>)}</select></label>
          </div>
        </Card>
      )}
      {tab === "yaml" && admin && (
        <Card title="Import / export (YAML)" actions={<>
          <button onClick={async () => setYamlText((await api.get("/config/export")).yaml)}>Export current</button>
          <label className="check"><input type="checkbox" checked={includeChannels} onChange={(e) => setIncludeChannels(e.target.checked)} /> import channels too</label>
          <button className="primary" disabled={!yamlText.trim()} onClick={async () => {
            try { await api.post("/config/import", { yaml: yamlText, include_channels: includeChannels }); toast("Configuration imported"); reload(); } catch (e: any) { toast(e.message, "error"); }
          }}>Import</button></>}>
          <textarea className="mono" rows={26} value={yamlText} onChange={(e) => setYamlText(e.target.value)} placeholder="Export the current configuration, edit it or paste one from another environment, then import." />
        </Card>
      )}
      {bank && (
        <Modal title={`Bank / company ${bank.code}`} onClose={() => setBank(null)} width={900}
          footer={<><span className="muted small">Greyed fields inherit the default thresholds.</span><button onClick={() => setBank(null)}>Cancel</button><button className="primary" onClick={saveBank}>Save</button></>}>
          <div className="form-grid">
            <label>Name<input value={bank.name} onChange={(e) => setBank({ ...bank, name: e.target.value })} /></label>
            <label className="check"><input type="checkbox" checked={bank.active} onChange={(e) => setBank({ ...bank, active: e.target.checked })} /> Active</label>
          </div>
          <ThresholdForm value={bank.effective} base={data.sections.thresholds} onChange={(v) => setBank({ ...bank, effective: v })} />
        </Modal>
      )}
      {newBank && (
        <Modal title="Add bank / company" onClose={() => setNewBank(null)} footer={<><button onClick={() => setNewBank(null)}>Cancel</button>
          <button className="primary" onClick={async () => { try { await api.post("/banks", newBank); setNewBank(null); reload(); } catch (e: any) { toast(e.message, "error"); } }}>Create</button></>}>
          <div className="form-grid">
            <label>Code (as in transactions' bank_id)<input value={newBank.code} onChange={(e) => setNewBank({ ...newBank, code: e.target.value })} /></label>
            <label>Name<input value={newBank.name} onChange={(e) => setNewBank({ ...newBank, name: e.target.value })} /></label>
          </div>
          <p className="muted small">The new bank starts with the default thresholds; edit it afterwards to override them.</p>
        </Modal>
      )}
      {node}
    </>
  );
}

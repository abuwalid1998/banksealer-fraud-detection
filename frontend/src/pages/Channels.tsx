import { useState } from "react";
import { api, fmtNum } from "../api";
import { useAuth } from "../auth";
import PreparationEditor from "../components/PreparationEditor";
import { Card, ErrorBox, KeyValueEditor, Modal, StatusBadge, useToast } from "../components/ui";
import { useLoad } from "../hooks";

const TARGET_FIELDS = ["tx_id", "user_id", "timestamp", "tx_type", "amount", "bank_id", "currency", "iban", "iban_cc", "ip", "cc_asn",
  "session_id", "card_id", "phone_number", "operator", "label", "scenario"];

function parseOption(v: string) {
  if (v === "true") return true;
  if (v === "false") return false;
  if (v.trim() !== "" && !isNaN(Number(v))) return Number(v);
  return v;
}

export function ChannelEditor({ channel, types, banks, onClose, onSaved }: any) {
  const [c, setC] = useState<any>(channel || { name: "", type: "http", description: "", enabled: false, options: { ...types.find((t: any) => t.type === "http")?.default_options }, field_mapping: {}, preparation: {} });
  const [tab, setTab] = useState("connection");
  const [error, setError] = useState<string | null>(null);
  const [sample, setSample] = useState("");
  const [preview, setPreview] = useState<any>(null);
  const type = types.find((t: any) => t.type === c.type);
  const save = async () => {
    try {
      const saved = c.id ? await api.put(`/channels/${c.id}`, c) : await api.post("/channels", c);
      onSaved(saved);
    } catch (e: any) { setError(e.message); }
  };
  const test = async () => {
    try { setPreview(await api.post("/preparation/preview", { field_mapping: c.field_mapping, preparation: c.preparation, text: sample })); setError(null); }
    catch (e: any) { setError(e.message); }
  };
  return (
    <Modal title={c.id ? `Channel: ${c.name}` : "Link a new channel"} onClose={onClose} width={900}
      footer={<><span className="muted small">Saving restarts the channel in the worker with the new configuration.</span><button onClick={onClose}>Cancel</button><button className="primary" onClick={save}>Save channel</button></>}>
      <div className="tabs">
        {[["connection", "Connection"], ["mapping", "Field mapping"], ["preparation", "Preparation rules"], ["test", "Test with sample"]].map(([k, l]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      <ErrorBox error={error} />
      {tab === "connection" && (
        <div className="form-grid">
          <label>Name<input value={c.name} onChange={(e) => setC({ ...c, name: e.target.value })} /></label>
          <label>Type<select value={c.type} disabled={!!c.id} onChange={(e) => setC({ ...c, type: e.target.value, options: { ...types.find((t: any) => t.type === e.target.value)?.default_options } })}>
            {types.map((t: any) => <option key={t.type} value={t.type}>{t.type} – {t.description}</option>)}</select></label>
          <label className="span-2">Description<input value={c.description || ""} onChange={(e) => setC({ ...c, description: e.target.value })} /></label>
          <label className="check span-2"><input type="checkbox" checked={c.enabled} onChange={(e) => setC({ ...c, enabled: e.target.checked })} /> Enabled (the worker starts it)</label>
          <h4 className="span-2">Connector options</h4>
          {Object.keys({ ...(type?.default_options || {}), ...c.options }).map((k) => (
            <label key={k}>{k}<input value={c.options[k] === undefined || c.options[k] === null ? "" : String(c.options[k])} type={k === "token" ? "password" : "text"}
              onChange={(e) => setC({ ...c, options: { ...c.options, [k]: parseOption(e.target.value) } })} /></label>
          ))}
        </div>
      )}
      {tab === "mapping" && (
        <>
          <p className="muted">Map fields of the source schema to BankSealer fields. Unmapped fields are matched through known aliases (e.g. <code>customer_id</code> → <code>user_id</code>).</p>
          <KeyValueEditor value={c.field_mapping} onChange={(m) => setC({ ...c, field_mapping: m })} keyLabel="Source field" valueLabel="BankSealer field" valueOptions={TARGET_FIELDS} />
        </>
      )}
      {tab === "preparation" && <PreparationEditor value={c.preparation} onChange={(p) => setC({ ...c, preparation: p })} banks={banks} />}
      {tab === "test" && (
        <>
          <p className="muted">Paste a few records (CSV with header, JSON array or JSON lines) to see how this channel prepares them, before saving.</p>
          <textarea className="mono" rows={7} value={sample} onChange={(e) => setSample(e.target.value)}
            placeholder={'customer_id,date_time,type,value,beneficiary_iban\nC1,2026-11-16 10:30,wire,"1.234,50",IT60X0542811101000000123456'} />
          <button onClick={test} disabled={!sample.trim()}>Run preparation</button>
          {preview && <PreviewTable results={preview.results} />}
        </>
      )}
    </Modal>
  );
}

export function PreviewTable({ results }: { results: any[] }) {
  return (
    <table className="table compact preview">
      <thead><tr><th>#</th><th>Result</th><th>Customer</th><th>Time</th><th>Type</th><th className="num">Amount</th><th>Bank</th><th>Beneficiary</th><th>Country/ASN</th></tr></thead>
      <tbody>{results.map((r, i) => r.ok ? (
        <tr key={i}><td>{i + 1}</td><td><span className="badge" style={{ background: "#15803d" }}>accepted</span></td><td>{r.transaction.user_id}</td><td className="nowrap">{r.transaction.timestamp}</td>
          <td>{r.transaction.tx_type}</td><td className="num">{r.transaction.amount} {r.transaction.currency}</td><td>{r.transaction.bank_id}</td>
          <td className="mono">{(r.transaction.tx_type === "bank_transfer" ? r.transaction.iban : r.transaction.tx_type === "phone_recharge" ? r.transaction.phone_number : r.transaction.card_id) || "-"}{r.transaction.iban_cc ? ` (${r.transaction.iban_cc})` : ""}</td><td className="mono">{r.transaction.cc_asn}</td></tr>
      ) : (
        <tr key={i} className="rejected"><td>{i + 1}</td><td><span className="badge" style={{ background: "#b91c1c" }}>rejected</span></td><td colSpan={7}>{r.error}</td></tr>
      ))}</tbody>
    </table>
  );
}

export default function Channels() {
  const { can } = useAuth();
  const { toast, node } = useToast();
  const { data, error, reload } = useLoad(() => api.get("/channels"), [], 3000);
  const { data: types } = useLoad(() => api.get("/channels/types"), []);
  const { data: banks } = useLoad(() => api.get("/banks"), []);
  const [editing, setEditing] = useState<any>(null);
  const [errors, setErrors] = useState<any>(null);
  const act = async (c: any, action: string) => {
    try { await api.post(`/channels/${c.id}/${action}`); toast(`${c.name}: ${action} requested`); reload(); } catch (e: any) { toast(e.message, "error"); }
  };
  const remove = async (c: any) => {
    if (!confirm(`Delete channel "${c.name}"?`)) return;
    try { await api.del(`/channels/${c.id}`); reload(); } catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <>
      <div className="page-head"><h1>Channels</h1>{can("admin") && types && <button className="primary" onClick={() => setEditing({})}>+ Link channel</button>}</div>
      <p className="muted">Transaction channels are configured in the database and run inside the worker. Each channel has its own field mapping and data-preparation rules; records that fail preparation are rejected with a reason.</p>
      <ErrorBox error={error} />
      <div className="channel-grid">
        {data?.map((c: any) => (
          <Card key={c.id} title={<>{c.name} <span className="muted small">· {c.type}</span></>} actions={<StatusBadge status={c.runtime.status} />}>
            <p className="muted small">{c.description}</p>
            <div className="mini-stats">
              <div><b>{fmtNum(c.runtime.received)}</b><span>received</span></div>
              <div><b>{fmtNum(c.runtime.accepted)}</b><span>accepted</span></div>
              <div><b className={c.runtime.rejected ? "warn-text" : ""}>{fmtNum(c.runtime.rejected)}</b><span>rejected</span></div>
              <div><b>{fmtNum(c.runtime.duplicates)}</b><span>duplicates</span></div>
              <div><b>{fmtNum(c.runtime.history)}</b><span>training</span></div>
            </div>
            <div className="mono small options">{Object.entries(c.options).filter(([k]) => k !== "token").map(([k, v]) => `${k}=${v}`).join("  ")}</div>
            <div className="small">Mapping: {Object.keys(c.field_mapping).length ? Object.entries(c.field_mapping).map(([k, v]) => `${k}→${v}`).join(", ") : "aliases only"} · Rules: {Object.keys(c.preparation).join(", ") || "none"}</div>
            {c.runtime.last_error && <div className="alert error small">{c.runtime.last_error}</div>}
            <div className="actions">
              {can("admin") && (c.enabled ? <button onClick={() => act(c, "stop")}>Stop</button> : <button className="success" onClick={() => act(c, "start")}>Start</button>)}
              {can("admin") && c.enabled && <button onClick={() => act(c, "restart")}>Restart</button>}
              {can("admin") && <button onClick={() => setEditing(c)}>Configure</button>}
              <button onClick={async () => setErrors({ channel: c, ...(await api.get(`/channels/${c.id}/errors`)) })}>Rejected records</button>
              {can("admin") && <button className="ghost danger-text" onClick={() => remove(c)}>Delete</button>}
            </div>
          </Card>
        ))}
      </div>
      {editing && types && <ChannelEditor channel={editing.id ? editing : null} types={types} banks={banks || []} onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); toast("Channel saved – the worker applies it within seconds"); reload(); }} />}
      {errors && (
        <Modal title={`Rejected records – ${errors.channel.name} (${errors.total})`} onClose={() => setErrors(null)} width={900}>
          <table className="table compact"><thead><tr><th>Time</th><th>Reason</th><th>Raw record</th></tr></thead>
            <tbody>{errors.items.map((e: any) => <tr key={e.id}><td className="nowrap">{e.created_at}</td><td>{e.message}</td><td className="mono small">{JSON.stringify(e.raw).slice(0, 220)}</td></tr>)}</tbody></table>
          {!errors.items.length && <p className="muted">No rejected records.</p>}
        </Modal>
      )}
      {node}
    </>
  );
}

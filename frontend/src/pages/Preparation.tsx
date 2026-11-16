import { useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import PreparationEditor from "../components/PreparationEditor";
import { Card, ErrorBox, KeyValueEditor, Pagination } from "../components/ui";
import { useLoad } from "../hooks";
import { PreviewTable } from "./Channels";

const SAMPLE = `client,op_date,op,value_cents,card,msisdn,src_ip
Z100,16/11/2026 10:30,CARD_LOAD,125050,4024 **** 1881,,93.40.1.7
Z100,16/11/2026 10:41,TOPUP,2500,,+39 333 1234567,93.40.1.7
Z101,2026-11-16,CARD_LOAD,9900,4024 **** 2201,,
Z102,16/11/2026 11:02,WIRE,10000,,,`;

export default function Preparation() {
  const { can } = useAuth();
  const { data: schema } = useLoad(() => api.get("/preparation/schema"), []);
  const { data: channels } = useLoad(() => api.get("/channels"), []);
  const { data: banks } = useLoad(() => api.get("/banks"), []);
  const [channelId, setChannelId] = useState<string>("");
  const [mapping, setMapping] = useState<any>({ client: "user_id", op_date: "timestamp", op: "tx_type", value_cents: "amount", card: "card_id", msisdn: "phone_number", src_ip: "ip" });
  const [rules, setRules] = useState<any>({ amount_scale: 0.01, timestamp_format: "%d/%m/%Y %H:%M", type_mapping: { CARD_LOAD: "prepaid_card", TOPUP: "phone_recharge" }, default_bank: "BANK001", defaults: { cc_asn: "IT-AS3269" } });
  const [text, setText] = useState(SAMPLE);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [errPage, setErrPage] = useState(1);
  const [errChannel, setErrChannel] = useState("");
  const { data: errs, reload: reloadErrs } = useLoad(() => api.get("/preparation/errors", { page_no: errPage, size: 20, channel: errChannel }), [errPage, errChannel], 10000);

  const loadChannel = (id: string) => {
    setChannelId(id);
    const c = channels?.find((x: any) => String(x.id) === id);
    if (c) { setMapping(c.field_mapping || {}); setRules(c.preparation || {}); }
  };
  const run = async () => {
    try { setResult(await api.post("/preparation/preview", { field_mapping: mapping, preparation: rules, text })); setError(null); }
    catch (e: any) { setError(e.message); }
  };
  const saveToChannel = async () => {
    const c = channels?.find((x: any) => String(x.id) === channelId);
    if (!c) return;
    try { await api.put(`/channels/${c.id}`, { ...c, field_mapping: mapping, preparation: rules }); setError(null); alert(`Saved to channel "${c.name}"`); }
    catch (e: any) { setError(e.message); }
  };
  return (
    <>
      <h1>Data preparation</h1>
      <p className="muted">Every record received by a channel passes through the preparation pipeline before scoring. Design and test a channel's mapping and rules here.</p>
      <div className="grid-2 wide-left">
        <Card title="Preparation workbench" actions={
          <select value={channelId} onChange={(e) => loadChannel(e.target.value)}><option value="">Ad-hoc configuration</option>
            {channels?.map((c: any) => <option key={c.id} value={c.id}>Load channel: {c.name}</option>)}</select>}>
          <h4>1 · Field mapping</h4>
          <KeyValueEditor value={mapping} onChange={setMapping} keyLabel="Source field" valueLabel="BankSealer field" valueOptions={schema?.fields.map((f: any) => f.name)} />
          <h4>2 · Rules</h4>
          <PreparationEditor value={rules} onChange={setRules} banks={banks || []} />
          <h4>3 · Sample records</h4>
          <textarea className="mono" rows={7} value={text} onChange={(e) => setText(e.target.value)} />
          <div className="actions">
            <button className="primary" onClick={run}>Run preparation</button>
            {can("admin") && channelId && <button onClick={saveToChannel}>Save mapping &amp; rules to channel</button>}
          </div>
          <ErrorBox error={error} />
          {result && <><p className="small">Accepted {result.stats.accepted} · rejected {result.stats.rejected} · duplicates {result.stats.duplicates}</p><PreviewTable results={result.results} /></>}
        </Card>
        <div className="stack">
          <Card title="Pipeline">
            <ol className="steps">{schema?.steps.map((s: string) => <li key={s}>{s}</li>)}</ol>
          </Card>
          <Card title="BankSealer fields">
            <table className="table compact"><tbody>{schema?.fields.map((f: any) => (
              <tr key={f.name}><td className="mono"><b>{f.name}</b>{f.required && <span className="req">*</span>}</td><td className="small">{f.description}<div className="muted">aliases: {f.aliases.join(", ")}</div></td></tr>
            ))}</tbody></table>
          </Card>
        </div>
      </div>
      <Card title={`Rejected records (${errs?.total ?? 0})`} actions={<>
        <select value={errChannel} onChange={(e) => { setErrChannel(e.target.value); setErrPage(1); }}><option value="">All sources</option>
          {Object.entries(errs?.by_channel || {}).map(([k, n]: any) => <option key={k} value={k}>{k} ({n})</option>)}</select>
        {can("admin") && <button onClick={async () => { await api.del("/preparation/errors", { channel: errChannel }); reloadErrs(); }}>Clear</button>}</>}>
        <table className="table compact"><thead><tr><th>Time</th><th>Source</th><th>Reason</th><th>Raw record</th></tr></thead>
          <tbody>{errs?.items.map((e: any) => <tr key={e.id}><td className="nowrap">{e.created_at}</td><td>{e.channel}</td><td>{e.message}</td><td className="mono small">{JSON.stringify(e.raw).slice(0, 200)}</td></tr>)}</tbody></table>
        {errs && <Pagination page={errPage} size={20} total={errs.total} onPage={setErrPage} />}
      </Card>
    </>
  );
}

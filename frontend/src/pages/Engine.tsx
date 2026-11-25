import { useState } from "react";
import { api, fmtNum } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBox, Kpi, StatusBadge, useToast } from "../components/ui";
import { useLoad } from "../hooks";

export default function Engine() {
  const { can } = useAuth();
  const { toast, node } = useToast();
  const { data: st } = useLoad(() => api.get("/engine/status"), [], 3000);
  const { data: cmds, reload } = useLoad(() => api.get("/engine/commands"), [], 3000);
  const { data: channels } = useLoad(() => api.get("/channels"), []);
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState("train");
  const [channelId, setChannelId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const run = async (path: string, label: string, confirmText?: string) => {
    if (confirmText && !confirm(confirmText)) return;
    try { await api.post(path); toast(`${label} queued`); reload(); } catch (e: any) { toast(e.message, "error"); }
  };
  const upload = async () => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file); form.append("mode", mode);
    if (channelId) form.append("channel_id", channelId);
    try { await api.post("/engine/upload", form); toast("File uploaded – processing in the worker"); setError(null); reload(); }
    catch (e: any) { setError(e.message); }
  };
  return (
    <>
      <h1>Engine &amp; training</h1>
      <div className="kpis">
        <Kpi label="Worker" value={st?.online ? "online" : "offline"} accent={st?.online ? "#15803d" : "#b91c1c"} hint={st?.heartbeat ? `heartbeat ${st.heartbeat}` : ""} />
        <Kpi label="Profiles" value={st?.trained ? "trained" : "collecting"} accent="#1e3a8a" hint={st?.trained ? `on ${fmtNum(st.training_size)} transactions` : `${fmtNum(st?.buffered)} buffered`} />
        <Kpi label="Customers profiled" value={fmtNum(st?.customers)} accent="#0e7490" hint={`${fmtNum(st?.peer_groups)} peer groups`} />
        <Kpi label="Scored" value={fmtNum(st?.scored_total)} accent="#7c3aed" hint={st?.last_fit ? `last fit ${st.last_fit}` : ""} />
      </div>
      <div className="grid-2">
        <Card title="Training">
          <p>Profiles are trained on historical data: records a channel marks as history (e.g. <code>{'{"history": true, "transactions": [...]}'}</code> on the webhook), uploaded history files, or the first live transactions when no history exists. Suspicious transactions are never learned unless an analyst marks them legitimate.</p>
          <div className="actions">
            {can("analyst") && <button className="primary" onClick={() => run("/engine/retrain", "Retrain")}>Retrain on history + legitimate transactions</button>}
            {can("admin") && <button onClick={() => run("/config/reevaluate", "Re-evaluation")}>Re-apply thresholds to all transactions</button>}
          </div>
          {can("admin") && <div className="actions">
            <button className="ghost danger-text" onClick={() => run("/engine/reset", "Model reset", "Discard the trained profiles?")}>Reset model</button>
            <button className="ghost danger-text" onClick={() => run("/engine/purge", "Purge", "Delete ALL transactions, scores, reports and profiles?")}>Purge operational data</button>
          </div>}
        </Card>
        {can("analyst") && (
          <Card title="Upload a file">
            <div className="form-grid">
              <label>File (.csv, .json, .jsonl)<input type="file" accept=".csv,.json,.jsonl" onChange={(e) => setFile(e.target.files?.[0] || null)} /></label>
              <label>Purpose<select value={mode} onChange={(e) => setMode(e.target.value)}><option value="train">History – train the profiles</option><option value="score">Transactions – score them</option></select></label>
              <label className="span-2">Prepare with the mapping/rules of channel<select value={channelId} onChange={(e) => setChannelId(e.target.value)}>
                <option value="">(standard fields and aliases)</option>{channels?.map((c: any) => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
            </div>
            <ErrorBox error={error} />
            <button className="primary" disabled={!file} onClick={upload}>Upload and process</button>
          </Card>
        )}
      </div>
      <Card title="Worker commands">
        <table className="table compact">
          <thead><tr><th>#</th><th>Command</th><th>Status</th><th>By</th><th>Created</th><th>Finished</th><th>Result</th></tr></thead>
          <tbody>{cmds?.map((c: any) => (
            <tr key={c.id}><td>{c.id}</td><td>{c.kind}</td><td><StatusBadge status={c.status} /></td><td>{c.created_by}</td><td className="nowrap">{c.created_at}</td>
              <td className="nowrap">{c.finished_at || "-"}</td><td className="mono small">{c.result ? JSON.stringify(c.result) : ""}</td></tr>
          ))}</tbody>
        </table>
      </Card>
      {node}
    </>
  );
}

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmtMoney, openPdf, TX_TYPES } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBox, RiskBadge, ScoreBar, StatusBadge, useToast } from "../components/ui";
import { useLoad } from "../hooks";

export default function TransactionDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { can } = useAuth();
  const { toast, node } = useToast();
  const { data: d, error, reload } = useLoad(() => api.get(`/transactions/${encodeURIComponent(id)}`), [id]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  if (!d) return <><ErrorBox error={error} />{!error && <p className="muted">Loading…</p>}</>;
  const t = d.transaction, s = d.score, th = d.bank.thresholds;

  const feedback = async (status: string) => {
    setBusy(true);
    try { await api.post(`/transactions/${encodeURIComponent(id)}/feedback`, { status, note: note || null }); toast(`Marked as ${status}`); reload(); }
    catch (e: any) { toast(e.message, "error"); } finally { setBusy(false); }
  };
  const report = async () => {
    setBusy(true);
    try { const r = await api.post(`/transactions/${encodeURIComponent(id)}/report`); await openPdf(r.id); reload(); }
    catch (e: any) { toast(e.message, "error"); } finally { setBusy(false); }
  };
  const fields: [string, any][] = [
    ["Type", TX_TYPES[t.tx_type]], ["Amount", fmtMoney(t.amount, t.currency)], ["Customer", <Link to={`/customers/${encodeURIComponent(t.user_id)}`}>{t.user_id}</Link>],
    ["Date / time", t.timestamp], ["Beneficiary", <span className="mono">{t.beneficiary || "-"}</span>], ["Beneficiary country", t.iban_cc || "-"],
    ["IP address", <span className="mono">{t.ip || "-"}</span>], ["Country / ASN", <span className="mono">{t.cc_asn || "-"}</span>],
    ["Channel", t.channel || "-"], ["Session", <span className="mono">{t.session_id || "-"}</span>], ["Bank / company", `${d.bank.code} – ${d.bank.name}`],
    ["Peer group", s.cluster === null || s.cluster === undefined ? "-" : `#${s.cluster}`],
  ];
  const local = d.customer_profile?.local?.[t.tx_type];
  return (
    <>
      <div className="page-head">
        <div>
          <button className="link" onClick={() => nav(-1)}>‹ Back</button>
          <h1 className="mono">{t.tx_id}</h1>
          <div className="headline"><RiskBadge level={s.risk_level} /> <b>{s.suspicious ? "SUSPICIOUS" : "Normal"}</b> · suspicion score <b>{s.combined.toFixed(2)}</b> · bank threshold {th.suspicious} · <StatusBadge status={s.status} /></div>
        </div>
        <div className="actions">
          {can("analyst") && <button className="primary" disabled={busy} onClick={report}>Generate PDF report</button>}
        </div>
      </div>
      <div className="grid-2 wide-right">
        <div className="stack">
          <Card title="BankSealer scores">
            <ScoreBar label="Local (HBOS)" value={s.local} threshold={th.suspicious} wide />
            <ScoreBar label="Temporal" value={s.temporal} threshold={th.suspicious} wide />
            <ScoreBar label="Global (CBLOF)" value={s.global} threshold={th.suspicious} wide />
            <ScoreBar label="Suspicion score" value={s.combined} threshold={th.suspicious} wide />
            <p className="muted small">Raw: HBOS {s.raw?.local_hbos} (history {s.raw?.local_history} ops) · temporal excess {s.raw?.temporal_excess} · CBLOF {s.raw?.global_cblof}{s.undertrained ? " · customer undertrained: compared with peer group" : ""}</p>
          </Card>
          <Card title="Transaction">
            <dl className="fields">{fields.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
          </Card>
          {can("analyst") && (
            <Card title="Analyst decision">
              <textarea placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
              <div className="actions">
                <button className="danger" disabled={busy} onClick={() => feedback("fraud")}>Confirm fraud</button>
                <button className="success" disabled={busy} onClick={() => feedback("legit")}>Legitimate (learn)</button>
                <button disabled={busy} onClick={() => feedback("open")}>Re-open</button>
              </div>
              {s.reviewed_by && <p className="muted small">Last review by {s.reviewed_by} at {s.reviewed_at}{s.analyst_note ? ` – “${s.analyst_note}”` : ""}</p>}
            </Card>
          )}
        </div>
        <div className="stack">
          <Card title={s.suspicious ? "Why this transaction is suspicious" : "Deviations from the customer's profile"}>
            {d.reasons.length ? (
              <ol className="reasons">
                {d.reasons.map((r: any, i: number) => (
                  <li key={i}><span className={`src src-${r.profile}`}>{r.source}</span><span className="msg">{r.message}</span>
                    {r.contribution ? <span className="impact">+{r.contribution.toFixed(2)}</span> : <span className="impact alert">alert</span>}</li>
                ))}
              </ol>
            ) : <p className="muted">No feature deviates from the customer's usual behaviour.</p>}
          </Card>
          <Card title="Customer's usual behaviour">
            {local ? (
              <dl className="fields">
                <div><dt>Past {TX_TYPES[t.tx_type].toLowerCase()}s</dt><dd>{local.count}</dd></div>
                <div><dt>Median / P95 amount</dt><dd>{fmtMoney(local.amount_median)} / {fmtMoney(local.amount_p95)}</dd></div>
                {["top_iban", "top_phone_number", "top_card_id", "top_cc_asn", "top_iban_cc"].filter((k) => local[k]?.length).map((k) => (
                  <div key={k}><dt>{k.replace("top_", "usual ").replace("_", " ")}</dt><dd className="mono">{local[k].map((x: any) => `${x[0]} (${x[1]})`).join(", ")}</dd></div>
                ))}
              </dl>
            ) : <p className="muted">No learned history for this operation type.</p>}
          </Card>
          <Card title="Reports">
            {d.reports.length ? <ul className="plain">{d.reports.map((r: any) => (
              <li key={r.id}><button className="link" onClick={() => openPdf(r.id)}>Report #{r.id}</button> <span className="muted">{r.created_at} by {r.created_by}</span></li>
            ))}</ul> : <p className="muted">No report yet.</p>}
          </Card>
        </div>
      </div>
      {node}
    </>
  );
}

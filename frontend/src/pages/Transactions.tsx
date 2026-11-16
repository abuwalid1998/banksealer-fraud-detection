import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, download, fmtMoney, TX_TYPES } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBox, Pagination, RiskBadge, ScoreChip, StatusBadge, useToast } from "../components/ui";
import { useLoad } from "../hooks";

export default function Transactions({ alerts = false }: { alerts?: boolean }) {
  const nav = useNavigate();
  const { can } = useAuth();
  const { toast, node } = useToast();
  const [f, setF] = useState<any>({ q: "", bank: "", tx_type: "", risk: "", status: alerts ? "open" : "", suspicious: alerts ? "true" : "" });
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState(alerts ? "score" : "timestamp");
  const [order, setOrder] = useState("desc");
  useEffect(() => { setPage(1); }, [JSON.stringify(f)]);
  const params = { ...f, page_no: page, size: 50, sort, order };
  const { data, error } = useLoad(() => api.get("/transactions", params), [JSON.stringify(params)], 10000);
  const { data: banks } = useLoad(() => api.get("/banks"), []);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const sortBy = (col: string) => { if (sort === col) setOrder(order === "desc" ? "asc" : "desc"); else { setSort(col); setOrder("desc"); } };
  const arrow = (col: string) => (sort === col ? (order === "desc" ? " ▾" : " ▴") : "");
  const filters = { ...f, suspicious: f.suspicious || undefined };

  const summary = async () => {
    try { const r = await api.post("/reports/summary", undefined, filters); toast(`Summary report created (${r.transactions} transactions) – see Reports`); }
    catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <>
      <div className="page-head">
        <h1>{alerts ? "Alerts" : "Transactions"}</h1>
        <div className="actions">
          <button onClick={() => download("/export/transactions.csv", "banksealer_transactions.csv", filters)}>Export CSV</button>
          {can("analyst") && <button className="primary" onClick={summary}>Summary report (PDF)</button>}
        </div>
      </div>
      {alerts && <p className="muted">Suspicious transactions waiting for review. Open one to see why it was flagged, generate its report and record your decision.</p>}
      <Card>
        <div className="filters">
          <input placeholder="Search id, customer, IBAN, ASN, IP…" value={f.q} onChange={set("q")} />
          <select value={f.bank} onChange={set("bank")}><option value="">All banks</option>{banks?.map((b: any) => <option key={b.code} value={b.code}>{b.code} – {b.name}</option>)}</select>
          <select value={f.tx_type} onChange={set("tx_type")}><option value="">All types</option>{Object.entries(TX_TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
          <select value={f.risk} onChange={set("risk")}><option value="">Any risk</option><option value="MEDIUM">MEDIUM and above</option><option value="HIGH">HIGH and above</option><option value="CRITICAL">CRITICAL</option></select>
          <select value={f.status} onChange={set("status")}><option value="">Any status</option><option value="open">Open</option><option value="fraud">Confirmed fraud</option><option value="legit">Legitimate</option></select>
          {!alerts && <select value={f.suspicious} onChange={set("suspicious")}><option value="">All</option><option value="true">Suspicious only</option><option value="false">Normal only</option></select>}
        </div>
        <ErrorBox error={error} />
        <div className="table-wrap">
          <table className="table clickable">
            <thead><tr>
              <th onClick={() => sortBy("timestamp")}>Time{arrow("timestamp")}</th><th>Transaction</th><th onClick={() => sortBy("customer")}>Customer{arrow("customer")}</th>
              <th>Bank</th><th>Type</th><th className="num" onClick={() => sortBy("amount")}>Amount{arrow("amount")}</th><th>Beneficiary</th><th>Country/ASN</th>
              <th onClick={() => sortBy("local")}>Local{arrow("local")}</th><th onClick={() => sortBy("temporal")}>Temp.{arrow("temporal")}</th><th onClick={() => sortBy("global")}>Global{arrow("global")}</th>
              <th onClick={() => sortBy("score")}>Score{arrow("score")}</th><th>Risk</th><th>Status</th>{alerts && <th>Main reason</th>}
            </tr></thead>
            <tbody>
              {data?.items.map((t: any) => (
                <tr key={t.tx_id} onClick={() => nav(`/transactions/${encodeURIComponent(t.tx_id)}`)} className={t.score.suspicious ? "flagged" : ""}>
                  <td className="nowrap">{t.timestamp.slice(0, 16)}</td><td className="mono">{t.tx_id}</td><td>{t.user_id}</td><td>{t.bank_id}</td>
                  <td className="nowrap">{TX_TYPES[t.tx_type]}</td><td className="num">{fmtMoney(t.amount)}</td><td className="mono ellipsis">{t.beneficiary}</td><td className="mono">{t.cc_asn}</td>
                  <td>{t.score.local.toFixed(2)}</td><td>{t.score.temporal.toFixed(2)}</td><td>{t.score.global.toFixed(2)}</td>
                  <td><ScoreChip value={t.score.combined} /></td><td><RiskBadge level={t.score.risk_level} /></td><td><StatusBadge status={t.score.status} /></td>
                  {alerts && <td className="reason" title={t.main_reason || ""}><span>{t.main_reason}</span></td>}
                </tr>
              ))}
              {data && !data.items.length && <tr><td colSpan={15} className="muted">No transactions match the filters.</td></tr>}
            </tbody>
          </table>
        </div>
        {data && <Pagination page={page} size={50} total={data.total} onPage={setPage} />}
      </Card>
      {node}
    </>
  );
}

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, fmtMoney, openPdf } from "../api";
import { Card, ErrorBox, Pagination, RiskBadge } from "../components/ui";
import { useLoad } from "../hooks";

export default function Reports() {
  const [page, setPage] = useState(1);
  const [kind, setKind] = useState("");
  const [tx, setTx] = useState("");
  const { data, error } = useLoad(() => api.get("/reports", { page_no: page, size: 50, kind, tx_id: tx }), [page, kind, tx], 10000);
  return (
    <>
      <h1>Reports</h1>
      <p className="muted">PDF reports are generated automatically for every suspicious transaction (configurable in Banks &amp; thresholds › Application), on demand from a transaction, or as summaries from the Alerts page.</p>
      <Card>
        <div className="filters">
          <input placeholder="Transaction id…" value={tx} onChange={(e) => { setTx(e.target.value); setPage(1); }} />
          <select value={kind} onChange={(e) => { setKind(e.target.value); setPage(1); }}><option value="">All reports</option><option value="transaction">Transaction reports</option><option value="summary">Summary reports</option></select>
        </div>
        <ErrorBox error={error} />
        <table className="table">
          <thead><tr><th>#</th><th>Created</th><th>Kind</th><th>Transaction</th><th>Customer</th><th>Bank</th><th className="num">Amount</th><th>Risk</th><th>By</th><th /></tr></thead>
          <tbody>{data?.items.map((r: any) => (
            <tr key={r.id}>
              <td>{r.id}</td><td className="nowrap">{r.created_at}</td><td>{r.kind}</td>
              <td className="mono">{r.tx_id ? <Link to={`/transactions/${encodeURIComponent(r.tx_id)}`}>{r.tx_id}</Link> : "-"}</td>
              <td>{r.user_id || "-"}</td><td>{r.bank_id || "-"}</td><td className="num">{r.amount ? fmtMoney(r.amount) : "-"}</td>
              <td><RiskBadge level={r.risk_level} /></td><td>{r.created_by}</td>
              <td><button className="small" onClick={() => openPdf(r.id)}>Open PDF</button></td>
            </tr>
          ))}</tbody>
        </table>
        {data && <Pagination page={page} size={50} total={data.total} onPage={setPage} />}
      </Card>
    </>
  );
}

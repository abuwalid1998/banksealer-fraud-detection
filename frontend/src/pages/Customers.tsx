import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, fmtMoney, fmtNum } from "../api";
import { Card, ErrorBox, Pagination, ScoreChip } from "../components/ui";
import { useLoad } from "../hooks";

export default function Customers() {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [bank, setBank] = useState("");
  const [flagged, setFlagged] = useState(false);
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState("max_score");
  useEffect(() => setPage(1), [q, bank, flagged]);
  const { data, error } = useLoad(() => api.get("/customers", { q, bank, flagged, page_no: page, size: 50, sort }), [q, bank, flagged, page, sort], 15000);
  const { data: banks } = useLoad(() => api.get("/banks"), []);
  const th = (col: string, label: string, cls = "") => <th className={cls} onClick={() => setSort(col)}>{label}{sort === col ? " ▾" : ""}</th>;
  return (
    <>
      <h1>Customers</h1>
      <Card>
        <div className="filters">
          <input placeholder="Search customer…" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={bank} onChange={(e) => setBank(e.target.value)}><option value="">All banks</option>{banks?.map((b: any) => <option key={b.code} value={b.code}>{b.code} – {b.name}</option>)}</select>
          <label className="check"><input type="checkbox" checked={flagged} onChange={(e) => setFlagged(e.target.checked)} /> With suspicious transactions</label>
        </div>
        <ErrorBox error={error} />
        <table className="table clickable">
          <thead><tr>{th("user_id", "Customer")}<th>Bank</th>{th("transactions", "Transactions", "num")}{th("suspicious", "Suspicious", "num")}<th className="num">Confirmed fraud</th>
            {th("max_score", "Max score")}{th("avg_score", "Avg score")}{th("amount", "Total amount", "num")}{th("last_activity", "Last activity")}<th>Peer group</th><th>Global score</th></tr></thead>
          <tbody>
            {data?.items.map((c: any) => (
              <tr key={c.user_id} onClick={() => nav(`/customers/${encodeURIComponent(c.user_id)}`)}>
                <td><b>{c.user_id}</b></td><td>{c.bank_id}</td><td className="num">{fmtNum(c.transactions)}</td>
                <td className={`num ${c.suspicious ? "warn-text" : ""}`}>{fmtNum(c.suspicious)}</td><td className="num">{fmtNum(c.confirmed_fraud)}</td>
                <td><ScoreChip value={c.max_score} /></td><td>{c.avg_score.toFixed(2)}</td><td className="num">{fmtMoney(c.amount)}</td>
                <td className="nowrap">{c.last_activity}</td><td>{c.cluster === null ? "-" : c.cluster < 0 ? "outlier" : `#${c.cluster}`}</td>
                <td>{c.global_score === null ? "-" : <ScoreChip value={c.global_score} />}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && <Pagination page={page} size={50} total={data.total} onPage={setPage} />}
      </Card>
    </>
  );
}

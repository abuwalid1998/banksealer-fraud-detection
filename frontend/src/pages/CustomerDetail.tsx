import { Bar, Scatter } from "react-chartjs-2";
import { useNavigate, useParams } from "react-router-dom";
import { api, fmtMoney, fmtNum, RISK_COLORS, TX_TYPES } from "../api";
import { baseOptions } from "../charts";
import { Card, ErrorBox, Kpi, RiskBadge, ScoreChip, StatusBadge } from "../components/ui";
import { useLoad } from "../hooks";

export default function CustomerDetail() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { data: d, error } = useLoad(() => api.get(`/customers/${encodeURIComponent(id)}`), [id], 15000);
  if (!d) return <><ErrorBox error={error} />{!error && <p className="muted">Loading…</p>}</>;
  const st = d.stats || {};
  const p = d.profile || {};
  const txs = [...d.transactions].reverse();
  const timeline = {
    datasets: [{ label: "suspicion score", data: txs.map((t: any, i: number) => ({ x: i, y: t.score.combined })),
      pointBackgroundColor: txs.map((t: any) => RISK_COLORS[t.score.risk_level]), pointRadius: 4, showLine: false }],
  };
  const hours = new Array(24).fill(0);
  Object.values(p.local || {}).forEach((info: any) => (info.hour_hist || []).forEach((n: number, h: number) => (hours[h] += n)));
  const alertHours = new Array(24).fill(0);
  d.transactions.filter((t: any) => t.score.suspicious).forEach((t: any) => (alertHours[Number(t.timestamp.slice(11, 13))] += 1));
  const hourChart = { labels: hours.map((_, h) => String(h)), datasets: [
    { label: "usual activity (learned)", data: hours, backgroundColor: "#93c5fd" },
    { label: "suspicious", data: alertHours, backgroundColor: RISK_COLORS.HIGH }] };
  const tmean = p.temporal?.mean || {};
  return (
    <>
      <div className="page-head"><div><button className="link" onClick={() => nav(-1)}>‹ Back</button><h1>Customer {d.user_id}</h1></div></div>
      <div className="kpis">
        <Kpi label="Transactions" value={fmtNum(st.transactions)} hint={`${fmtNum(d.training_transactions)} used for training`} />
        <Kpi label="Suspicious" value={fmtNum(st.suspicious)} accent={RISK_COLORS.HIGH} hint={`${fmtNum(st.confirmed_fraud)} confirmed fraud`} />
        <Kpi label="Max score" value={(st.max_score ?? 0).toFixed(2)} accent={RISK_COLORS.CRITICAL} hint={`avg ${(st.avg_score ?? 0).toFixed(2)}`} />
        <Kpi label="Peer group" value={d.cluster === null ? "-" : d.cluster < 0 ? "outlier" : `#${d.cluster}`} accent="#7c3aed" hint={`global score ${(d.global_score ?? 0).toFixed(2)}`} />
        <Kpi label="Home country" value={p.temporal?.home_country || "-"} accent="#0e7490" hint={`${(tmean.tx_count ?? 0).toFixed(1)} ops / 24h on average`} />
      </div>
      <div className="grid-2">
        <Card title="Suspicion score of each transaction (oldest → newest)"><div className="chart"><Scatter data={timeline} options={{ ...baseOptions, plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { min: 0, max: 1 } } }} /></div></Card>
        <Card title="Hours of activity"><div className="chart"><Bar data={hourChart} options={baseOptions} /></div></Card>
      </div>
      <Card title="Learned local profile">
        <table className="table">
          <thead><tr><th>Operation type</th><th className="num">Past ops</th><th className="num">Median</th><th className="num">P95</th><th>Usual beneficiaries</th><th>Usual connections</th></tr></thead>
          <tbody>{Object.entries(p.local || {}).map(([type, info]: any) => (
            <tr key={type}><td>{TX_TYPES[type]}</td><td className="num">{info.count}</td><td className="num">{fmtMoney(info.amount_median)}</td><td className="num">{fmtMoney(info.amount_p95)}</td>
              <td className="mono small">{(info.top_iban || info.top_phone_number || info.top_card_id || []).map((x: any) => `${x[0]} (${x[1]})`).join(", ")}</td>
              <td className="mono small">{(info.top_cc_asn || []).map((x: any) => `${x[0]} (${x[1]})`).join(", ")}</td></tr>
          ))}</tbody>
        </table>
        {!p.local && <p className="muted">The profile is published by the worker after training.</p>}
      </Card>
      <Card title="Transactions">
        <table className="table clickable">
          <thead><tr><th>Time</th><th>Type</th><th className="num">Amount</th><th>Beneficiary</th><th>Country/ASN</th><th>Score</th><th>Risk</th><th>Status</th></tr></thead>
          <tbody>{d.transactions.map((t: any) => (
            <tr key={t.tx_id} className={t.score.suspicious ? "flagged" : ""} onClick={() => nav(`/transactions/${encodeURIComponent(t.tx_id)}`)}>
              <td className="nowrap">{t.timestamp.slice(0, 16)}</td><td>{TX_TYPES[t.tx_type]}</td><td className="num">{fmtMoney(t.amount)}</td>
              <td className="mono ellipsis">{t.beneficiary}</td><td className="mono">{t.cc_asn}</td><td><ScoreChip value={t.score.combined} /></td>
              <td><RiskBadge level={t.score.risk_level} /></td><td><StatusBadge status={t.score.status} /></td></tr>
          ))}</tbody>
        </table>
      </Card>
    </>
  );
}

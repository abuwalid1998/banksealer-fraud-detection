import { useState } from "react";
import { Bar } from "react-chartjs-2";
import { Link, useNavigate } from "react-router-dom";
import { api, fmtMoney, fmtNum, RISK_COLORS, TX_TYPES } from "../api";
import { baseOptions, PALETTE } from "../charts";
import { Card, ErrorBox, Kpi, RiskBadge, ScoreChip, StatusBadge } from "../components/ui";
import { useLoad } from "../hooks";

export default function Dashboard() {
  const [bank, setBank] = useState("");
  const nav = useNavigate();
  const { data: d, error } = useLoad(() => api.get("/dashboard", { bank }), [bank], 5000);
  if (!d) return <><h1>Dashboard</h1><ErrorBox error={error} />{!error && <p className="muted">Loading…</p>}</>;
  const k = d.kpis;
  const th = d.thresholds;
  const hist = {
    labels: d.histogram.map((_: number, i: number) => (i / 20).toFixed(2)),
    datasets: [{ label: "transactions", data: d.histogram, backgroundColor: d.histogram.map((_: number, i: number) => {
      const v = i / 20; return v >= th.critical ? RISK_COLORS.CRITICAL : v >= th.high ? RISK_COLORS.HIGH : v >= th.suspicious ? RISK_COLORS.MEDIUM : "#94a3b8";
    }) }],
  };
  const perDay = {
    labels: d.by_day.map((r: any) => r.date.slice(5)),
    datasets: ["MEDIUM", "HIGH", "CRITICAL"].map((lvl) => ({ label: lvl, data: d.by_day.map((r: any) => r[lvl]), backgroundColor: RISK_COLORS[lvl], stack: "s" })),
  };
  const banks = Array.from(new Set(d.by_bank_type.map((r: any) => r.bank_id))) as string[];
  const bankType = {
    labels: Object.values(TX_TYPES),
    datasets: banks.map((b, i) => ({ label: d.banks.find((x: any) => x.code === b)?.name || b, backgroundColor: PALETTE[i % PALETTE.length],
      data: Object.keys(TX_TYPES).map((t) => d.by_bank_type.find((r: any) => r.bank_id === b && r.tx_type === t)?.count || 0) })),
  };
  const prof = {
    labels: ["Local (HBOS)", "Temporal", "Global (CBLOF)"],
    datasets: [["normal", "#94a3b8"], ["suspicious", RISK_COLORS.HIGH]].map(([key, color]) => ({
      label: key, backgroundColor: color, data: ["local", "temporal", "global"].map((p) => d.profiles[key]?.[p] || 0) })),
  };
  const reasons = {
    labels: d.top_reasons.map((r: any) => r.reason),
    datasets: [{ label: "alerts", data: d.top_reasons.map((r: any) => r.count), backgroundColor: "#1e3a8a" }],
  };
  return (
    <>
      <div className="page-head">
        <h1>Dashboard</h1>
        <select value={bank} onChange={(e) => setBank(e.target.value)}>
          <option value="">All banks / companies</option>
          {d.banks.map((b: any) => <option key={b.code} value={b.code}>{b.code} – {b.name}</option>)}
        </select>
      </div>
      <ErrorBox error={error} />
      <div className="kpis">
        <Kpi label="Transactions scored" value={fmtNum(k.transactions)} hint={`${fmtNum(k.customers)} customers`} />
        <Kpi label="Suspicious" value={fmtNum(k.suspicious)} accent={RISK_COLORS.HIGH} hint={`${(k.rate * 100).toFixed(2)}% of transactions`} />
        <Kpi label="Critical" value={fmtNum(k.critical)} accent={RISK_COLORS.CRITICAL} hint={`${fmtNum(k.high)} high`} />
        <Kpi label="Open alerts" value={fmtNum(k.open_alerts)} accent="#7c3aed" hint={`${fmtNum(k.confirmed_fraud)} confirmed fraud`} />
        <Kpi label="Amount at risk" value={fmtMoney(k.amount_at_risk)} accent="#0e7490" hint={`of ${fmtMoney(k.amount)}`} />
        <Kpi label="Avg suspicion score" value={k.avg_score.toFixed(3)} accent="#334155" hint={`threshold ${th.suspicious}`} />
      </div>
      <div className="grid-2">
        <Card title="Suspicion score distribution"><div className="chart"><Bar data={hist} options={{ ...baseOptions, plugins: { legend: { display: false } }, scales: { ...baseOptions.scales, y: { ...baseOptions.scales.y, type: "linear" } } }} /></div></Card>
        <Card title="Suspicious transactions per day"><div className="chart"><Bar data={perDay} options={{ ...baseOptions, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true } } }} /></div></Card>
        <Card title="Suspicious by bank and operation type"><div className="chart"><Bar data={bankType} options={baseOptions} /></div></Card>
        <Card title="Average profile scores"><div className="chart"><Bar data={prof} options={{ ...baseOptions, scales: { ...baseOptions.scales, y: { min: 0, max: 1 } } }} /></div></Card>
      </div>
      <div className="grid-2 wide-left">
        <Card title="Latest suspicious transactions" actions={<Link to="/alerts">All alerts →</Link>}>
          <table className="table clickable">
            <thead><tr><th>Time</th><th>Customer</th><th>Bank</th><th>Type</th><th className="num">Amount</th><th>Score</th><th>Risk</th><th>Main reason</th></tr></thead>
            <tbody>
              {d.latest.map((t: any) => (
                <tr key={t.tx_id} onClick={() => nav(`/transactions/${encodeURIComponent(t.tx_id)}`)}>
                  <td className="nowrap">{t.timestamp.slice(0, 16)}</td><td>{t.user_id}</td><td>{t.bank_id}</td>
                  <td className="nowrap">{TX_TYPES[t.tx_type]}</td><td className="num">{fmtMoney(t.amount)}</td>
                  <td><ScoreChip value={t.score.combined} /></td><td><RiskBadge level={t.score.risk_level} /></td>
                  <td className="reason" title={t.main_reason || ""}><span>{t.main_reason}</span></td>
                </tr>
              ))}
              {!d.latest.length && <tr><td colSpan={8} className="muted">No suspicious transactions yet.</td></tr>}
            </tbody>
          </table>
        </Card>
        <div className="stack">
          <Card title="Top detection reasons"><div className="chart small"><Bar data={reasons} options={{ ...baseOptions, indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true }, y: { grid: { display: false }, ticks: { font: { size: 10 } } } } }} /></div></Card>
          <Card title="Channels" actions={<Link to="/channels">Manage →</Link>}>
            <table className="table compact">
              <tbody>{d.channels.map((c: any) => (
                <tr key={c.id}><td>{c.name}</td><td className="muted">{c.type}</td><td><StatusBadge status={c.enabled ? c.status : "disabled"} /></td><td className="num">{fmtNum(c.received)}</td></tr>
              ))}</tbody>
            </table>
          </Card>
        </div>
      </div>
    </>
  );
}

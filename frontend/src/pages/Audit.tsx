import { useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Pagination } from "../components/ui";
import { useLoad } from "../hooks";

export default function Audit() {
  const [page, setPage] = useState(1);
  const [action, setAction] = useState("");
  const { data, error } = useLoad(() => api.get("/audit", { page_no: page, size: 50, action }), [page, action], 10000);
  return (
    <>
      <h1>Audit log</h1>
      <Card>
        <div className="filters"><input placeholder="Filter action (e.g. config, channel, transaction)…" value={action} onChange={(e) => { setAction(e.target.value); setPage(1); }} /></div>
        <ErrorBox error={error} />
        <table className="table compact">
          <thead><tr><th>Time (UTC)</th><th>User</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
          <tbody>{data?.items.map((a: any) => (
            <tr key={a.id}><td className="nowrap">{a.ts}</td><td>{a.username || "-"}</td><td><code>{a.action}</code></td><td>{a.target}</td>
              <td className="mono small">{a.detail ? JSON.stringify(a.detail).slice(0, 240) : ""}</td></tr>
          ))}</tbody>
        </table>
        {data && <Pagination page={page} size={50} total={data.total} onPage={setPage} />}
      </Card>
    </>
  );
}

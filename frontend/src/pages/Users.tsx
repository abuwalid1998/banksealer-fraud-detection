import { useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Modal, useToast } from "../components/ui";
import { useLoad } from "../hooks";

export default function Users() {
  const { data, error, reload } = useLoad(() => api.get("/users"), []);
  const [edit, setEdit] = useState<any>(null);
  const { toast, node } = useToast();
  const save = async () => {
    try {
      if (edit.id) await api.put(`/users/${edit.id}`, edit); else await api.post("/users", edit);
      setEdit(null); reload(); toast("User saved");
    } catch (e: any) { toast(e.message, "error"); }
  };
  return (
    <>
      <div className="page-head"><h1>Users</h1><button className="primary" onClick={() => setEdit({ username: "", full_name: "", role: "analyst", password: "", active: true })}>+ Add user</button></div>
      <p className="muted"><b>viewer</b>: read-only · <b>analyst</b>: reviews alerts, reports, uploads, retraining · <b>admin</b>: channels, banks, thresholds, users.</p>
      <ErrorBox error={error} />
      <Card>
        <table className="table">
          <thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Active</th><th>Last login</th><th /></tr></thead>
          <tbody>{data?.map((u: any) => (
            <tr key={u.id}><td><b>{u.username}</b></td><td>{u.full_name}</td><td>{u.role}</td><td>{u.active ? "yes" : "no"}</td><td>{u.last_login || "-"}</td>
              <td><button className="small" onClick={() => setEdit({ ...u, password: "" })}>Edit</button>{" "}
                <button className="small ghost danger-text" onClick={async () => { if (confirm(`Delete ${u.username}?`)) { try { await api.del(`/users/${u.id}`); reload(); } catch (e: any) { toast(e.message, "error"); } } }}>Delete</button></td></tr>
          ))}</tbody>
        </table>
      </Card>
      {edit && (
        <Modal title={edit.id ? `Edit ${edit.username}` : "Add user"} onClose={() => setEdit(null)} footer={<><button onClick={() => setEdit(null)}>Cancel</button><button className="primary" onClick={save}>Save</button></>}>
          <div className="form-grid">
            <label>Username<input disabled={!!edit.id} value={edit.username} onChange={(e) => setEdit({ ...edit, username: e.target.value })} /></label>
            <label>Full name<input value={edit.full_name || ""} onChange={(e) => setEdit({ ...edit, full_name: e.target.value })} /></label>
            <label>Role<select value={edit.role} onChange={(e) => setEdit({ ...edit, role: e.target.value })}><option>viewer</option><option>analyst</option><option>admin</option></select></label>
            <label>{edit.id ? "New password (optional)" : "Password"}<input type="password" value={edit.password} onChange={(e) => setEdit({ ...edit, password: e.target.value })} /></label>
            <label className="check"><input type="checkbox" checked={edit.active} onChange={(e) => setEdit({ ...edit, active: e.target.checked })} /> Active</label>
          </div>
        </Modal>
      )}
      {node}
    </>
  );
}

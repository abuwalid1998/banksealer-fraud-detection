import { FormEvent, useState } from "react";
import { useAuth } from "../auth";

export default function Login() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await login(username, password); } catch (err: any) { setError(err.message); } finally { setBusy(false); }
  };
  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <div className="brand big"><span className="logo">✓</span><div><b>BankSealer</b><small>Enterprise fraud analysis console</small></div></div>
        <label>Username<input autoFocus value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
        {error && <div className="alert error">{error}</div>}
        <button className="primary" disabled={busy || !username || !password}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="muted small">Local profiles (HBOS) · temporal thresholds · peer groups (DBSCAN + CBLOF)</p>
      </form>
    </div>
  );
}

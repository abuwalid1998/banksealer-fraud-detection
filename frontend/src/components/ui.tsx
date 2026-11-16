import { ReactNode, useEffect, useState } from "react";
import { RISK_COLORS } from "../api";

export function RiskBadge({ level }: { level?: string }) {
  if (!level) return null;
  return <span className="badge" style={{ background: RISK_COLORS[level] || "#666" }}>{level}</span>;
}

const STATUS: Record<string, [string, string]> = {
  open: ["Open", "#475569"], fraud: ["Confirmed fraud", "#b91c1c"], legit: ["Legitimate", "#15803d"],
  running: ["Running", "#15803d"], stopped: ["Stopped", "#64748b"], disabled: ["Disabled", "#94a3b8"],
  error: ["Error", "#b91c1c"], starting: ["Starting", "#d4a106"], finished: ["Finished", "#2563eb"],
  pending: ["Pending", "#d4a106"], done: ["Done", "#15803d"], failed: ["Failed", "#b91c1c"],
};
export function StatusBadge({ status }: { status?: string }) {
  if (!status) return null;
  const [label, color] = STATUS[status] || [status, "#64748b"];
  return <span className="badge outline" style={{ color, borderColor: color }}>{label}</span>;
}

export function ScoreBar({ value, threshold, label, wide }: { value: number; threshold?: number; label?: string; wide?: boolean }) {
  const color = value >= 0.8 ? RISK_COLORS.CRITICAL : value >= 0.65 ? RISK_COLORS.HIGH
    : threshold !== undefined && value >= threshold ? RISK_COLORS.MEDIUM : RISK_COLORS.LOW;
  return (
    <div className={`scorebar ${wide ? "wide" : ""}`}>
      {label && <span className="scorebar-label">{label}</span>}
      <div className="scorebar-track">
        <div className="scorebar-fill" style={{ width: `${Math.min(100, Math.max(0, value * 100))}%`, background: color }} />
        {threshold !== undefined && <div className="scorebar-threshold" style={{ left: `${threshold * 100}%` }} title={`threshold ${threshold}`} />}
      </div>
      <span className="scorebar-value">{value.toFixed(2)}</span>
    </div>
  );
}

export function ScoreChip({ value }: { value: number }) {
  const color = value >= 0.8 ? RISK_COLORS.CRITICAL : value >= 0.65 ? RISK_COLORS.HIGH : value >= 0.5 ? RISK_COLORS.MEDIUM : "#94a3b8";
  return <span className="scorechip" style={{ background: `${color}22`, color, borderColor: `${color}66` }}>{value.toFixed(2)}</span>;
}

export function Kpi({ label, value, accent, hint }: { label: string; value: ReactNode; accent?: string; hint?: string }) {
  return (
    <div className="kpi" style={{ borderTopColor: accent || "#1e3a8a" }}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" style={{ color: accent || "#1e3a8a" }}>{value}</div>
      {hint && <div className="kpi-hint">{hint}</div>}
    </div>
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className || ""}`}>
      {(title || actions) && <header className="card-head"><h3>{title}</h3><div className="card-actions">{actions}</div></header>}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Pagination({ page, size, total, onPage }: { page: number; size: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / size));
  return (
    <div className="pagination">
      <span>{total.toLocaleString()} rows</span>
      <button disabled={page <= 1} onClick={() => onPage(page - 1)}>‹ Prev</button>
      <span>Page {page} / {pages}</span>
      <button disabled={page >= pages} onClick={() => onPage(page + 1)}>Next ›</button>
    </div>
  );
}

export function Modal({ title, onClose, children, footer, width }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; width?: number }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" style={{ maxWidth: width || 720 }} onMouseDown={(e) => e.stopPropagation()}>
        <header><h3>{title}</h3><button className="ghost" onClick={onClose} aria-label="Close">✕</button></header>
        <div className="modal-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function ErrorBox({ error }: { error?: string | null }) {
  return error ? <div className="alert error">{error}</div> : null;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Editable key -> value table (field mappings, type mappings, defaults). */
export function KeyValueEditor({ value, onChange, keyLabel, valueLabel, valueOptions }: {
  value: Record<string, any>; onChange: (v: Record<string, any>) => void; keyLabel: string; valueLabel: string; valueOptions?: string[];
}) {
  const [rows, setRows] = useState<[string, string][]>(Object.entries(value || {}).map(([k, v]) => [k, String(v)]));
  useEffect(() => { setRows(Object.entries(value || {}).map(([k, v]) => [k, String(v)])); }, [JSON.stringify(value)]);
  const emit = (next: [string, string][]) => {
    setRows(next);
    const obj: Record<string, string> = {};
    next.forEach(([k, v]) => { if (k.trim()) obj[k.trim()] = v; });
    onChange(obj);
  };
  return (
    <table className="kv">
      <thead><tr><th>{keyLabel}</th><th>{valueLabel}</th><th /></tr></thead>
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i}>
            <td><input value={k} onChange={(e) => emit(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} /></td>
            <td>{valueOptions
              ? <select value={v} onChange={(e) => emit(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}>
                  <option value="" />{valueOptions.map((o) => <option key={o} value={o}>{o}</option>)}</select>
              : <input value={v} onChange={(e) => emit(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />}</td>
            <td><button className="ghost" onClick={() => emit(rows.filter((_, j) => j !== i))}>✕</button></td>
          </tr>
        ))}
        <tr><td colSpan={3}><button className="link" onClick={() => setRows([...rows, ["", ""]])}>+ add row</button></td></tr>
      </tbody>
    </table>
  );
}

export function useToast() {
  const [msg, setMsg] = useState<{ text: string; kind: string } | null>(null);
  useEffect(() => { if (!msg) return; const t = setTimeout(() => setMsg(null), 4000); return () => clearTimeout(t); }, [msg]);
  const node = msg ? <div className={`toast ${msg.kind}`}>{msg.text}</div> : null;
  return { toast: (text: string, kind = "ok") => setMsg({ text, kind }), node };
}

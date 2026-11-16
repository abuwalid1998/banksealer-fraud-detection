import { TX_TYPES } from "../api";
import { KeyValueEditor } from "./ui";

/** Editor for the preparation rules of a channel (see banksealer.preparation.PREPARATION_RULES). */
export default function PreparationEditor({ value, onChange, banks }: { value: any; onChange: (v: any) => void; banks: any[] }) {
  const v = value || {};
  const set = (k: string, val: any) => {
    const next = { ...v, [k]: val };
    if (val === "" || val === null || (Array.isArray(val) && !val.length) || (typeof val === "object" && val && !Array.isArray(val) && !Object.keys(val).length)) delete next[k];
    onChange(next);
  };
  const num = (k: string) => (e: any) => set(k, e.target.value === "" ? "" : Number(e.target.value));
  const allowed: string[] = v.allowed_types || [];
  return (
    <div className="prep-editor">
      <div className="form-grid">
        <label>Default bank / company
          <select value={v.default_bank || ""} onChange={(e) => set("default_bank", e.target.value)}>
            <option value="">(from record, else DEFAULT)</option>{banks.map((b) => <option key={b.code} value={b.code}>{b.code} – {b.name}</option>)}
          </select></label>
        <label>Default currency<input value={v.default_currency || ""} placeholder="EUR" onChange={(e) => set("default_currency", e.target.value.toUpperCase())} /></label>
        <label>Amount scale<input type="number" step="any" value={v.amount_scale ?? ""} placeholder="1 (0.01 for cents)" onChange={num("amount_scale")} /></label>
        <label>Timestamp format<input value={v.timestamp_format || ""} placeholder="auto (ISO, epoch, dd/mm/yyyy…)" onChange={(e) => set("timestamp_format", e.target.value)} /></label>
        <label>Reject amounts below<input type="number" step="any" value={v.min_amount ?? ""} onChange={num("min_amount")} /></label>
        <label>Reject amounts above<input type="number" step="any" value={v.max_amount ?? ""} onChange={num("max_amount")} /></label>
      </div>
      <div className="field-row">
        <span className="label">Accepted operation types</span>
        {Object.entries(TX_TYPES).map(([k, label]) => (
          <label key={k} className="check"><input type="checkbox" checked={!allowed.length || allowed.includes(k)}
            onChange={(e) => {
              const current = allowed.length ? allowed : Object.keys(TX_TYPES);
              const next = e.target.checked ? [...current, k] : current.filter((x) => x !== k);
              set("allowed_types", next.length === 3 ? [] : next);
            }} /> {label}</label>
        ))}
      </div>
      <div className="grid-2 tight">
        <div><h4>Operation type mapping</h4><p className="muted small">Source code → BankSealer type</p>
          <KeyValueEditor value={v.type_mapping || {}} onChange={(m) => set("type_mapping", m)} keyLabel="Source value" valueLabel="Type" valueOptions={Object.keys(TX_TYPES)} /></div>
        <div><h4>Constant defaults</h4><p className="muted small">Used when the record has no value</p>
          <KeyValueEditor value={v.defaults || {}} onChange={(m) => set("defaults", m)} keyLabel="Field" valueLabel="Value" /></div>
      </div>
    </div>
  );
}

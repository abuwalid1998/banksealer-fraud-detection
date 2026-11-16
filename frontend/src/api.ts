// Thin REST client for the BankSealer Enterprise API.
const TOKEN_KEY = "banksealer.token";

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(token: string | null) {
  try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

type Params = Record<string, string | number | boolean | undefined | null>;

export function qs(params?: Params): string {
  if (!params) return "";
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") p.set(k, String(v)); });
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function request<T = any>(method: string, path: string, body?: unknown, params?: Params): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(`/api${path}${qs(params)}`, { method, headers, body: payload });
  if (res.status === 401 && !path.startsWith("/auth/login")) {
    setToken(null);
    window.dispatchEvent(new Event("banksealer:logout"));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    throw new ApiError(res.status, detail);
  }
  const type = res.headers.get("content-type") || "";
  return (type.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  get: <T = any>(path: string, params?: Params) => request<T>("GET", path, undefined, params),
  post: <T = any>(path: string, body?: unknown, params?: Params) => request<T>("POST", path, body, params),
  put: <T = any>(path: string, body?: unknown, params?: Params) => request<T>("PUT", path, body, params),
  del: <T = any>(path: string, params?: Params) => request<T>("DELETE", path, undefined, params),
};

/** Download a protected file (PDF/CSV) with the bearer token. */
export async function download(path: string, filename: string, params?: Params) {
  const res = await fetch(`/api${path}${qs(params)}`, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (!res.ok) throw new ApiError(res.status, `download failed (${res.status})`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

export async function openPdf(reportId: number) {
  const res = await fetch(`/api/reports/${reportId}/download`, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (!res.ok) throw new ApiError(res.status, "report not found");
  const url = URL.createObjectURL(await res.blob());
  window.open(url, "_blank");
}

export const TX_TYPES: Record<string, string> = {
  bank_transfer: "Bank transfer", phone_recharge: "Phone recharge", prepaid_card: "Prepaid card",
};
export const RISK_LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
export const RISK_COLORS: Record<string, string> = {
  LOW: "#2e7d32", MEDIUM: "#d4a106", HIGH: "#e8710a", CRITICAL: "#c62828",
};
export const fmtMoney = (v: number | null | undefined, cur = "") =>
  v === null || v === undefined ? "-" : `${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${cur ? " " + cur : ""}`;
export const fmtNum = (v: number | null | undefined) => (v === null || v === undefined ? "-" : v.toLocaleString());
export const fmtScore = (v: number | null | undefined) => (v === null || v === undefined ? "-" : v.toFixed(2));

// Thin API client: bearer session token kept in sessionStorage (cleared when the tab closes).
const KEY = "vs_token";

export class ApiError extends Error {
  constructor(message, status, detail) { super(message); this.status = status; this.detail = detail || {}; }
}

function token() { try { return sessionStorage.getItem(KEY); } catch { return null; } }

export const api = {
  onUnauthorized: null,
  setToken(t) { try { t ? sessionStorage.setItem(KEY, t) : sessionStorage.removeItem(KEY); } catch { /* private mode */ } },
  hasToken() { return !!token(); },

  async req(method, path, body, { raw = false, form = false } = {}) {
    const headers = {};
    const t = token();
    if (t) headers.Authorization = `Bearer ${t}`;
    let payload;
    if (form) payload = body;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    let res;
    try {
      res = await fetch(path.startsWith("/api/") || path.startsWith("/health") ? path : `/api/v1${path}`,
        { method, headers, body: payload });
    } catch (e) {
      throw new ApiError("Cannot reach the VeriShield server - is it running?", 0);
    }
    if (raw && res.ok) return res;
    let data = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) data = await res.json();
    else if (!res.ok) data = { detail: { message: await res.text() } };
    if (!res.ok) {
      const d = (data && data.detail) || {};
      const msg = typeof d === "string" ? d : d.message || `Request failed (${res.status})`;
      if (res.status === 401 && api.onUnauthorized && !path.includes("/auth/login") && !path.includes("/mfa/verify")) {
        api.onUnauthorized(d.code, msg);
      }
      throw new ApiError(msg, res.status, typeof d === "object" ? d : {});
    }
    return data;
  },
  get(p) { return this.req("GET", p); },
  post(p, b = {}) { return this.req("POST", p, b); },
  put(p, b = {}) { return this.req("PUT", p, b); },
  patch(p, b = {}) { return this.req("PATCH", p, b); },
  del(p) { return this.req("DELETE", p); },
  upload(p, formData) { return this.req("POST", p, formData, { form: true }); },
  async download(p, fallbackName = "download") {
    const res = await this.req("GET", p, undefined, { raw: true });
    const blob = await res.blob();
    const cd = res.headers.get("content-disposition") || "";
    const m = /filename="?([^";]+)"?/i.exec(cd);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = m ? m[1] : fallbackName;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  },
};

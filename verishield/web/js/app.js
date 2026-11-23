// VeriShield single-page app: sign-in, first-login setup, layout and hash router.
import { api } from "./api.js";
import { $, $$, LOGO, S, busy, errToast, esc, fmtRel, formData, icon, live, modal, toast } from "./ui.js";

const PAGES = {
  overview: () => import("./pages/overview.js"),
  transactions: () => import("./pages/transactions.js"),
  customers: () => import("./pages/customers.js"),
  alerts: () => import("./pages/alerts.js"),
  cases: () => import("./pages/cases.js"),
  decision: () => import("./pages/decision.js"),
  reports: () => import("./pages/reports.js"),
  models: () => import("./pages/models.js"),
  integrations: () => import("./pages/integrations.js"),
  policies: () => import("./pages/policies.js"),
  operations: () => import("./pages/operations.js"),
  settings: () => import("./pages/settings.js"),
  account: () => import("./pages/account.js"),
};
const root = document.getElementById("root");

api.onUnauthorized = (code, msg) => {
  api.setToken(null);
  S.me = null;
  if (code === "MFA_REQUIRED") return renderLogin("Enter your verification code.", true);
  renderLogin(msg || "Your session has ended. Please sign in again.");
};

export function navigate(hash) { if (location.hash === hash) route(); else location.hash = hash; }
window.addEventListener("hashchange", () => route());

async function boot() {
  try { S.info = await api.get("/public/info"); } catch (e) { S.info = { ready: false }; }
  if (!api.hasToken()) return renderLogin();
  await loadMe();
}

async function loadMe() {
  try {
    S.me = await api.get("/auth/me");
  } catch (e) {
    if (e.status !== 401) errToast(e);
    return;
  }
  if (S.me.restrictions.length) return renderSetup();
  renderShell();
  route();
}

// ------------------------------------------------------------------ sign in
function devAccounts() {
  if (!S.info || !S.info.dev_accounts) return "";
  const rows = [["1000", "Admin", "IT Admin (bootstrap)"], ["2000", "Manager#2026", "AML Manager"],
    ["3001", "Analyst1#2026", "AML Employee 1"], ["3002", "Analyst2#2026", "AML Employee 2"], ["4000", "Chief#2026", "Chief AML / Compliance"]];
  return `<div class="notice devbox"><b>Local development accounts</b> – click to fill. Each temporary password must be changed at first sign-in (this list is hidden in production).
    <table>${rows.map((r) => `<tr data-u="${r[0]}" data-p="${esc(r[1])}"><td class="mono">${r[0]}</td><td class="mono">${esc(r[1])}</td><td>${r[2]}</td></tr>`).join("")}</table></div>`;
}

function authLayout(inner) {
  return `<div class="auth">
    <div class="art">
      <svg class="rings" viewBox="0 0 400 400">${[60, 90, 120, 150, 180].map((r) => `<circle cx="200" cy="200" r="${r}" fill="none" stroke="#3B5A86" stroke-width="1"/>`).join("")}</svg>
      <div class="brand" style="padding:0">${LOGO}<div><div class="word">VERISHIELD</div></div></div>
      <div class="eyebrow" style="margin-top:auto">ENTERPRISE TRANSACTION INTELLIGENCE</div>
      <h1 style="margin-top:8px">VeriShield</h1>
      <p class="tag">Transaction Intelligence. Trusted Decisions.</p>
      <div class="pillars"><div><b>DETECT</b>Behavioural signals across each transaction.</div><div><b>UNDERSTAND</b>Customer patterns from day one.</div>
        <div><b>INVESTIGATE</b>Assigned AML cases with evidence and decisions.</div><div><b>CONTROL</b>Configurable pass, review, hold and block outcomes.</div></div>
      <p class="small" style="color:#7F93B0;margin-top:22px">${esc(S.info && S.info.name ? S.info.name : "")}${S.info && S.info.demo_mode ? " · Demo Mode active (live scoring, simulated payment actions)" : ""}</p>
    </div>
    <div class="panel"><div class="form">${inner}</div></div></div>`;
}

function renderLogin(message = "", mfa = false) {
  document.title = "Sign in · VeriShield";
  root.innerHTML = authLayout(`<div class="card">
      <h2 style="font-size:20px">${mfa ? "Verification code" : "Sign in"}</h2>
      <p class="muted small" style="margin:4px 0 16px">${mfa ? "Enter the 6-digit code from your authenticator app." : "Use your 4–5 digit VeriShield username."}</p>
      ${message ? `<div class="notice warn" style="margin-bottom:12px">${esc(message)}</div>` : ""}
      <form id="login" class="stack" style="gap:12px">
        ${mfa ? `<label class="f">Code<input class="input" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required></label>` : `
        <label class="f">Institution code<input class="input" name="tenant" value="${esc((S.info && S.info.tenant) || "")}" autocomplete="organization"></label>
        <label class="f">Username<input class="input" name="username" inputmode="numeric" autocomplete="username" required></label>
        <label class="f">Password<input class="input" name="password" type="password" autocomplete="current-password" required></label>`}
        <button class="btn primary" style="justify-content:center;padding:10px">${mfa ? "Verify" : "Sign in"}</button>
      </form>
    </div>${mfa ? "" : devAccounts()}
    <p class="tiny muted" style="margin-top:14px;text-align:center">VeriShield ${S.info && S.info.environment === "production" ? "" : "· " + esc(S.info ? S.info.environment : "")} · <a href="/api/docs" target="_blank" rel="noopener">API documentation</a></p>`);
  $$(".devbox tr").forEach((tr) => tr.addEventListener("click", () => {
    $("[name=username]").value = tr.dataset.u; $("[name=password]").value = tr.dataset.p;
  }));
  $("#login").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    const f = formData(e.target);
    await busy(btn, async () => {
      try {
        if (mfa) {
          await api.post("/auth/mfa/verify", { code: f.code });
        } else {
          const r = await api.post("/auth/login", { username: f.username.trim(), password: f.password, tenant: f.tenant || null });
          api.setToken(r.token);
          if (r.mfa_required) return renderLogin("", true);
        }
        await loadMe();
      } catch (err) { errToast(err); }
    });
  });
}

// ------------------------------------------------------------------ first-login setup
async function renderSetup() {
  const r = S.me.restrictions;
  const all = ["CHANGE_PASSWORD", "COMPLETE_PROFILE", "VERIFY_EMAIL", "ENROL_MFA"];
  const step = r[0];
  const idx = all.indexOf(step);
  const u = S.me.user;
  const steps = `<div class="steps">${all.filter((s) => s === step || r.includes(s) || all.indexOf(s) < idx).map((s) => `<span class="${all.indexOf(s) <= idx ? "on" : ""}"></span>`).join("")}</div>`;
  let body = "";
  if (step === "CHANGE_PASSWORD") {
    body = `<h2>Set your own password</h2><p class="muted small">Your temporary password is single-use. Choose a new one: at least 10 characters with upper and lower case letters, a digit and a symbol.</p>
      <form id="f" class="stack" style="gap:12px;margin-top:12px">
      <label class="f">Current (temporary) password<input class="input" type="password" name="current_password" autocomplete="current-password" required></label>
      <label class="f">New password<input class="input" type="password" name="new_password" autocomplete="new-password" required></label>
      <label class="f">Repeat new password<input class="input" type="password" name="repeat" autocomplete="new-password" required></label>
      <button class="btn primary">Change password</button></form>`;
  } else if (step === "COMPLETE_PROFILE") {
    body = `<h2>Complete the administrator profile</h2><p class="muted small">The bootstrap account must be linked to a named person before setup can continue.</p>
      <form id="f" class="stack" style="gap:12px;margin-top:12px">
      <label class="f">First name<input class="input" name="first_name" value="${esc(u.first_name)}" required></label>
      <label class="f">Last name<input class="input" name="last_name" value="${esc(u.last_name)}" required></label>
      <label class="f">E-mail<input class="input" type="email" name="email" value="${esc(u.email)}" required></label>
      <button class="btn primary">Save and continue</button></form>`;
  } else if (step === "VERIFY_EMAIL") {
    body = `<h2>Verify your e-mail</h2><p class="muted small">We send a 6-digit code to <b>${esc(u.email)}</b>.</p>
      <div class="row" style="margin:12px 0"><button class="btn" id="send">Send code</button><span id="devcode" class="small muted"></span></div>
      <form id="f" class="stack" style="gap:12px"><label class="f">Code<input class="input" name="code" required></label><button class="btn primary">Verify</button></form>`;
  } else if (step === "ENROL_MFA") {
    body = `<h2>Enrol multi-factor authentication</h2><p class="muted small">Your institution requires MFA. Add VeriShield to an authenticator app (Microsoft/Google Authenticator, 1Password…) using the key below, then enter the 6-digit code.</p>
      <div id="mfa" style="margin:12px 0">${'<span class="spin"></span>'}</div>
      <form id="f" class="stack" style="gap:12px"><label class="f">Code<input class="input" name="code" inputmode="numeric" maxlength="6" required></label><button class="btn primary">Enable MFA</button></form>`;
  }
  root.innerHTML = authLayout(`<div class="card">${steps}${body}</div><p class="center small" style="margin-top:12px"><a href="#" id="out">Sign out</a></p>`);
  $("#out").onclick = (e) => { e.preventDefault(); signOut(); };
  if (step === "ENROL_MFA") {
    try {
      const m = await api.post("/auth/mfa/setup");
      $("#mfa").innerHTML = `<label class="f">Secret key<input class="input mono" readonly value="${esc(m.secret)}"></label>
        <p class="tiny muted" style="word-break:break-all;margin-top:6px">${esc(m.otpauth_uri)}</p>`;
    } catch (e) { errToast(e); }
  }
  if (step === "VERIFY_EMAIL") {
    $("#send").onclick = async () => {
      try { const r = await api.post("/auth/email/send-code"); toast(r.sent ? "Code sent" : "Code generated");
        if (r.dev_code) $("#devcode").textContent = `Development: code ${r.dev_code} (SMTP not configured)`; }
      catch (e) { errToast(e); }
    };
  }
  $("#f").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = formData(e.target);
    await busy(e.target.querySelector("button"), async () => {
      try {
        if (step === "CHANGE_PASSWORD") {
          if (f.new_password !== f.repeat) throw new Error("The new passwords do not match");
          S.me = await api.post("/auth/change-password", { current_password: f.current_password, new_password: f.new_password });
          toast("Password changed");
        } else if (step === "COMPLETE_PROFILE") S.me = await api.put("/auth/profile", f);
        else if (step === "VERIFY_EMAIL") S.me = await api.post("/auth/email/verify", { code: f.code });
        else if (step === "ENROL_MFA") S.me = await api.post("/auth/mfa/enable", { code: f.code });
        if (S.me.restrictions.length) renderSetup(); else { renderShell(); navigate("#/overview"); }
      } catch (err) { errToast(err); }
    });
  });
}

async function signOut() {
  try { await api.post("/auth/logout"); } catch { /* ignore */ }
  api.setToken(null); S.me = null; location.hash = ""; renderLogin("You have signed out.");
}

// ------------------------------------------------------------------ shell
function renderShell() {
  const m = S.me;
  const initials = (m.user.first_name[0] || "") + (m.user.last_name[0] || "") || m.user.username.slice(0, 2);
  root.innerHTML = `<div class="app ${m.tenant.demo_mode ? "demo-on" : ""}">
    <aside class="sidebar" id="sidebar">
      <div class="brand">${LOGO}<div><div class="word">VERISHIELD</div><div class="tag">Transaction Intelligence</div></div></div>
      <nav class="nav">${m.navigation.map((n) => `<a href="#/${n.id}" data-nav="${n.id}">${icon(n.id)}<span>${esc(n.label)}</span></a>`).join("")}</nav>
      <div class="foot">${esc(m.tenant.name)}<br>${esc(m.user.role_label)}${m.tenant.demo_mode ? '<br><b style="color:#E8A33A">Demo workspace</b>' : ""}</div>
    </aside>
    <div class="main">
      ${m.tenant.demo_mode ? '<div class="demo-banner" role="status">DEMO MODE – LIVE SCORING · PAYMENT ACTIONS ARE SIMULATED</div>' : ""}
      <header class="topbar">
        <button class="btn ghost menu-toggle" id="menu-toggle" aria-label="Menu">${icon("menu")}</button>
        <div class="title"><div class="crumb" id="crumb">${esc(m.tenant.name)}</div><h1 id="page-title">Overview</h1></div>
        <div class="pulse" id="pulse" title="Real-time scoring activity in this workspace"></div>
        <form class="search" id="gsearch" role="search">${icon("search")}<input class="input" name="q" placeholder="Search transaction, customer or case…" aria-label="Global search"></form>
        <div class="usermenu"><button id="um" aria-haspopup="true"><span class="avatar">${esc(initials.toUpperCase())}</span><span class="who"><b>${esc(m.user.name)}</b><span>${esc(m.user.role_label)} · ${esc(m.user.username)}</span></span></button></div>
      </header>
      <main class="content" id="content"></main>
    </div></div>`;
  $("#menu-toggle").onclick = () => $("#sidebar").classList.toggle("open");
  const pulse = async () => {
    const p = await api.get("/live/pulse");
    const on = p.channels_running > 0 || p.decisions_last_minute > 0;
    $("#pulse").innerHTML = `<span class="livepill ${on ? "" : "off"}"><i></i>${on ? "LIVE" : "IDLE"}</span>
      <span><b>${p.decisions_last_minute}</b> scored / min · <b>${p.channels_running}</b> channel${p.channels_running === 1 ? "" : "s"} running</span>
      ${p.last_decision ? `<span>last ${esc(p.last_decision.decision)} ${Number(p.last_decision.fraud_score).toFixed(2)} · ${esc(fmtRel(p.last_decision.at))}</span>` : ""}`;
  };
  pulse().catch(() => {});
  live(document.getElementById("pulse"), 5000, pulse);
  $("#um").onclick = (e) => {
    e.stopPropagation();
    const old = $(".menu"); if (old) return old.remove();
    const menu = document.createElement("div");
    menu.className = "menu";
    menu.innerHTML = `<a href="#/account">My account &amp; security</a><a href="/api/docs" target="_blank" rel="noopener">API documentation</a><button id="so">Sign out</button>`;
    $(".usermenu").appendChild(menu);
    $("#so").onclick = signOut;
    setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }));
  };
  $("#gsearch").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = e.target.q.value.trim(); if (!q) return;
    e.target.q.value = "";
    if (/CASE-/i.test(q) && S.me.permissions.some((p) => p.startsWith("cases."))) return navigate(`#/cases/${encodeURIComponent(q.toUpperCase())}`);
    if (/^[A-Z]\d{4,}$/i.test(q) && S.me.navigation.some((n) => n.id === "customers")) return navigate(`#/customers/${encodeURIComponent(q.toUpperCase())}`);
    navigate(`#/transactions?search=${encodeURIComponent(q)}`);
  });
}

let currentPage = null;
async function route() {
  if (!S.me || S.me.restrictions.length) return;
  const raw = location.hash.replace(/^#\/?/, "") || "overview";
  const [pathPart, query = ""] = raw.split("?");
  const [page, ...rest] = pathPart.split("/");
  const params = Object.fromEntries(new URLSearchParams(query));
  const allowed = new Set([...S.me.navigation.map((n) => n.id), "account"]);
  const id = PAGES[page] && allowed.has(page) ? page : "overview";
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === id));
  $("#sidebar").classList.remove("open");
  const content = $("#content");
  content.innerHTML = '<div class="loading"><span class="spin"></span></div>';
  const navItem = S.me.navigation.find((n) => n.id === id);
  setTitle(navItem ? navItem.label : id === "account" ? "My account" : "Overview");
  currentPage = id;
  try {
    const mod = await PAGES[id]();
    if (currentPage !== id) return;
    const wrap = document.createElement("div");
    wrap.className = "page";
    content.innerHTML = "";
    content.appendChild(wrap);
    await mod.render(wrap, { sub: rest.map(decodeURIComponent), params, setTitle });
  } catch (e) {
    errToast(e);
    content.innerHTML = `<div class="notice crit">Could not load this page: ${esc(e.message)}</div>`;
  }
}
export function setTitle(t, crumb) {
  $("#page-title").textContent = t;
  $("#crumb").textContent = crumb || (S.me ? S.me.tenant.name : "");
  document.title = `${t} · VeriShield`;
}
export async function refreshMe() { S.me = await api.get("/auth/me"); renderShell(); route(); }

boot();

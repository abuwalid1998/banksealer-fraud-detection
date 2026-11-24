// My account: profile, password and MFA for every role.
import { api } from "../api.js";
import { refreshMe } from "../app.js";
import { S, busy, card, chip, errToast, esc, formData, kv, toast } from "../ui.js";

export async function render(el) {
  const u = S.me.user;
  el.innerHTML = `<div class="grid g2">
    ${card("Profile", `${kv([["Username", `<span class="mono">${esc(u.username)}</span>`], ["Role", esc(u.role_label)], ["Institution", esc(S.me.tenant.name)],
      ["E-mail", `${esc(u.email)} ${u.email_verified ? chip("VERIFIED", "ok") : chip("UNVERIFIED", "neutral")}`], ["MFA", u.mfa_enabled ? chip("ENABLED", "ok") : chip("NOT ENABLED", "warn")]])}
      <form id="pf" class="form-grid" style="margin-top:14px"><label class="f">First name<input class="input" name="first_name" value="${esc(u.first_name)}" required></label>
      <label class="f">Last name<input class="input" name="last_name" value="${esc(u.last_name)}" required></label><label class="f">E-mail<input class="input" type="email" name="email" value="${esc(u.email)}" required></label>
      <label class="f">Mobile (SMS codes)<input class="input" name="phone" value="${esc(u.phone || "")}" placeholder="+970 59 ..."></label>
      <div style="align-self:end"><button class="btn">Save profile</button></div></form>`)}
    ${card("Change password", `<form id="cp" class="stack" style="gap:10px"><label class="f">Current password<input class="input" type="password" name="current_password" autocomplete="current-password" required></label>
      <label class="f">New password<input class="input" type="password" name="new_password" autocomplete="new-password" required></label><button class="btn primary">Change password</button></form>`)}
    ${card("Multi-factor authentication", u.mfa_enabled ? '<p class="small">MFA is enabled for your account. Ask your IT Admin to reset it if you lose your device.</p>'
      : `<p class="small">Protect your account with a time-based code from an authenticator app.</p><div id="mfa" style="margin-top:10px"><button class="btn primary" id="start">Set up MFA</button></div>`)}
  </div>`;
  const pf = el.querySelector("#pf");
  pf.onsubmit = (e) => { e.preventDefault(); busy(pf.querySelector("button"), async () => { try { await api.put("/auth/profile", formData(pf)); toast("Profile saved"); await refreshMe(); } catch (err) { errToast(err); } }); };
  const cp = el.querySelector("#cp");
  cp.onsubmit = (e) => { e.preventDefault(); busy(cp.querySelector("button"), async () => { try { await api.post("/auth/change-password", formData(cp)); toast("Password changed"); cp.reset(); } catch (err) { errToast(err); } }); };
  const st = el.querySelector("#start");
  if (st) st.onclick = () => busy(st, async () => {
    try {
      const m = await api.post("/auth/mfa/setup");
      el.querySelector("#mfa").innerHTML = `<label class="f">Secret key (add to your authenticator)<input class="input mono" readonly value="${esc(m.secret)}"></label>
        <p class="tiny muted" style="word-break:break-all;margin:6px 0 10px">${esc(m.otpauth_uri)}</p>
        <form id="mf" class="row"><input class="input" name="code" placeholder="6-digit code" maxlength="6" style="width:160px" required><button class="btn primary">Enable MFA</button></form>`;
      const mf = el.querySelector("#mf");
      mf.onsubmit = (e) => { e.preventDefault(); busy(mf.querySelector("button"), async () => { try { await api.post("/auth/mfa/enable", { code: mf.code.value }); toast("MFA enabled"); await refreshMe(); } catch (err) { errToast(err); } }); };
    } catch (err) { errToast(err); }
  });
}

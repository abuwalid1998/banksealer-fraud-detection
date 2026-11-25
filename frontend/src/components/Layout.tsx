import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { useLoad } from "../hooks";

const NAV: { to: string; label: string; icon: string; role?: string; section?: string }[] = [
  { to: "/", label: "Dashboard", icon: "◧", section: "Monitoring" },
  { to: "/alerts", label: "Alerts", icon: "⚑" },
  { to: "/transactions", label: "Transactions", icon: "⇄" },
  { to: "/customers", label: "Customers", icon: "◉" },
  { to: "/reports", label: "Reports", icon: "▤" },
  { to: "/channels", label: "Channels", icon: "⇶", section: "Data" },
  { to: "/preparation", label: "Data preparation", icon: "⚙" },
  { to: "/engine", label: "Engine & training", icon: "◎" },
  { to: "/configuration", label: "Banks & thresholds", icon: "☰", section: "Administration" },
  { to: "/users", label: "Users", icon: "☺", role: "admin" },
  { to: "/audit", label: "Audit log", icon: "✎", role: "admin" },
];

export default function Layout() {
  const { user, logout, can } = useAuth();
  const { data: engine } = useLoad(() => api.get("/engine/status"), [], 10000);
  const [open, setOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);
  return (
    <div className="shell">
      {open && <div className="scrim" onClick={() => setOpen(false)} />}
      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="brand"><span className="logo">✓</span><div><b>BankSealer</b><small>Enterprise fraud analysis</small></div></div>
        <nav>
          {NAV.filter((n) => !n.role || can(n.role)).map((n) => (
            <div key={n.to}>
              {n.section && <div className="nav-section">{n.section}</div>}
              <NavLink to={n.to} end={n.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
                <span className="nav-icon">{n.icon}</span>{n.label}
              </NavLink>
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className={`engine-pill ${engine?.online ? (engine?.trained ? "ok" : "warn") : "down"}`}>
            <span className="dot" />
            {engine ? (engine.online ? (engine.trained ? "Engine online" : "Engine training") : "Worker offline") : "…"}
          </div>
          {engine?.state && <small className="engine-state">{engine.state}</small>}
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <button className="ghost menu-btn" aria-label="Menu" onClick={() => setOpen(!open)}>☰</button>
          <div className="user">
            <span>{user?.full_name || user?.username}</span>
            <span className="role">{user?.role}</span>
            <button className="ghost" onClick={logout}>Sign out</button>
          </div>
        </header>
        <main className="content"><Outlet /></main>
      </div>
    </div>
  );
}

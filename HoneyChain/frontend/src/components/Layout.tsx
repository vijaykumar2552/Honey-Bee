import { useEffect, useState, type ReactNode } from "react";
import type { User } from "../services/api";
import { Toaster } from "./toast";

const ALL_STAFF = ["ADMIN", "BEEKEEPER", "FPO_AGGREGATOR", "PROCESSOR", "LAB", "LOGISTICS", "RETAILER", "AUTHORITY"];
const HOLDERS = ["ADMIN", "BEEKEEPER", "FPO_AGGREGATOR", "PROCESSOR", "LOGISTICS", "RETAILER", "AUTHORITY"];

/** Navigation shown per role. Hiding is convenience only -- every action is
 * authorised again by the backend. */
export const NAV: { path: string; label: string; roles: string[] }[] = [
  { path: "dashboard", label: "Dashboard", roles: ALL_STAFF },
  { path: "hives", label: "Hives", roles: ["ADMIN", "BEEKEEPER", "FPO_AGGREGATOR", "AUTHORITY"] },
  { path: "insights", label: "AI insights", roles: ALL_STAFF },
  { path: "harvests", label: "Harvests", roles: ["ADMIN", "BEEKEEPER", "FPO_AGGREGATOR", "AUTHORITY"] },
  { path: "batches", label: "Batches", roles: ALL_STAFF },
  { path: "lab", label: "Laboratory", roles: ["LAB"] },
  { path: "custody", label: "Custody", roles: HOLDERS },
  { path: "shipments", label: "Shipments", roles: HOLDERS },
  { path: "retail", label: "Retail", roles: ["ADMIN", "RETAILER", "LOGISTICS", "AUTHORITY"] },
  { path: "recalls", label: "Recalls", roles: ALL_STAFF },
  { path: "blockchain", label: "Blockchain", roles: ALL_STAFF },
  { path: "authority", label: "Authority", roles: ["AUTHORITY", "ADMIN"] },
  { path: "settings", label: "Settings", roles: ALL_STAFF },
];

export function useHashRoute(): string[] {
  const parse = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const [parts, setParts] = useState(parse);
  useEffect(() => {
    const on = () => setParts(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parts;
}

export const link = (...parts: string[]) => "#/" + parts.map(encodeURIComponent).join("/");

export default function Layout({ user, route, onSignOut, children }: {
  user: User; route: string[]; onSignOut: () => void; children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const items = NAV.filter((n) => n.roles.includes(user.role));
  const section = NAV.find((n) => n.path === route[0]);
  return (
    <div className={`app${collapsed ? " collapsed" : ""}`}>
      <a className="skip" href="#main">Skip to content</a>
      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="brand">HoneyChain</div>
        <button className="secondary collapse" aria-pressed={collapsed} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                onClick={() => setCollapsed(!collapsed)}>{collapsed ? ">" : "<"}</button>
        <nav aria-label="Main navigation">
          {items.map((n) => (
            <a key={n.path} href={link(n.path)} aria-current={route[0] === n.path ? "page" : undefined}
               onClick={() => setOpen(false)}>{n.label}</a>
          ))}
        </nav>
      </aside>
      <div className="main-col">
        <header className="topbar">
          <button className="secondary menu" aria-expanded={open} aria-label="Toggle navigation"
                  onClick={() => setOpen(!open)}>Menu</button>
          <nav aria-label="Breadcrumb" className="crumbs">
            <a href={link("dashboard")}>Home</a>
            {section && <> / <a href={link(section.path)}>{section.label}</a></>}
            {route[1] && <> / <span aria-current="page">{route[1]}</span></>}
          </nav>
          <span className="muted small who">{user.name} - {user.role.replace(/_/g, " ")}</span>
          <button className="secondary" onClick={onSignOut}>Sign out</button>
        </header>
        {/* keyed by route: each navigation replays a short fade/translate */}
        <main id="main" tabIndex={-1} key={route.join("/")} className="page">{children}</main>
        <Toaster />
      </div>
    </div>
  );
}

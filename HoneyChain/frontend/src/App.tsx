import { useEffect, useState } from "react";
import { ApiError, getToken, logout, me, type User } from "./services/api";
import Login from "./pages/Login";
import AuthorityDashboard from "./pages/AuthorityDashboard";
import ConsumerVerify from "./pages/ConsumerVerify";
import Layout, { NAV, useHashRoute } from "./components/Layout";
import { Dashboard, HiveDetail, Hives, Insights } from "./pages/Operations";
import { BatchDetail, Batches, Custody, Harvests, Lab, NotAllowed, Recalls, Retail, Shipments } from "./pages/SupplyChain";
import { Blockchain, Settings } from "./pages/Chain";

export default function App() {
  // Public consumer page: no login, no staff session (QR codes land here).
  if (window.location.pathname.replace(/\/+$/, "").endsWith("/verify")) return <ConsumerVerify />;
  return <StaffApp />;
}

function StaffApp() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState<boolean>(Boolean(getToken()));
  const route = useHashRoute();

  useEffect(() => {
    if (!getToken()) return;
    me()
      .then(setUser)
      .catch((e: unknown) => {
        if (!(e instanceof ApiError && e.status === 401)) console.error(e);
      })
      .finally(() => setChecking(false));
  }, []);

  if (checking) return <main className="centered">Checking your session...</main>;
  if (!user) return <Login onLogin={setUser} />;

  const signOut = async () => {
    await logout();
    setUser(null);
    window.location.hash = "";
  };

  const allowed = NAV.filter((n) => n.roles.includes(user.role)).map((n) => n.path);
  const page = route[0] && allowed.includes(route[0]) ? route[0] : allowed[0] ?? "settings";
  if (user.role === "CONSUMER") {
    return (
      <main className="centered panel">
        <p>Consumer accounts verify products at <a href="/app/verify">/app/verify</a>.</p>
        <button onClick={signOut}>Sign out</button>
      </main>
    );
  }

  let body;
  switch (page) {
    case "dashboard": body = <Dashboard />; break;
    case "hives": body = route[1] ? <HiveDetail hiveId={route[1]} role={user.role} /> : <Hives role={user.role} />; break;
    case "insights": body = <Insights />; break;
    case "harvests": body = <Harvests role={user.role} />; break;
    case "batches": body = route[1] ? <BatchDetail batchId={route[1]} user={user} /> : <Batches role={user.role} />; break;
    case "lab": body = <Lab />; break;
    case "custody": body = <Custody user={user} />; break;
    case "shipments": body = <Shipments user={user} />; break;
    case "retail": body = <Retail role={user.role} />; break;
    case "recalls": body = <Recalls role={user.role} />; break;
    case "blockchain": body = <Blockchain user={user} />; break;
    case "authority": body = <AuthorityDashboard />; break;
    case "settings": body = <Settings user={user} onSignOut={signOut} />; break;
    default: body = <NotAllowed what={page} />;
  }
  return <Layout user={user} route={[page, ...route.slice(1)]} onSignOut={signOut}>{body}</Layout>;
}

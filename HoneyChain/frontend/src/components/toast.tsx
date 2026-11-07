import { useEffect, useState } from "react";

type Toast = { id: number; text: string; ok: boolean; leaving?: boolean };
const listeners = new Set<(t: Toast) => void>();
let nextId = 1;

/** Show feedback for a real API outcome (success or failure). */
export function toast(text: string, ok = true): void {
  const t = { id: nextId++, text, ok };
  listeners.forEach((l) => l(t));
}

export function Toaster() {
  const [items, setItems] = useState<Toast[]>([]);
  useEffect(() => {
    const add = (t: Toast) => {
      setItems((xs) => [...xs.slice(-3), t]);
      window.setTimeout(() => setItems((xs) => xs.map((x) => (x.id === t.id ? { ...x, leaving: true } : x))), 3800);
      window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== t.id)), 4000);
    };
    listeners.add(add);
    return () => { listeners.delete(add); };
  }, []);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((t) => <div key={t.id} className={`toast ${t.ok ? "ok" : "bad"} ${t.leaving ? "leaving" : ""}`}>{t.text}</div>)}
    </div>
  );
}

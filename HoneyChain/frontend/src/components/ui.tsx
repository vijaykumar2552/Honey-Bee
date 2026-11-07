import { useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "./toast";
import { describeError } from "../services/api";

const TONE: Record<string, string> = {
  HEALTHY: "ok", PASSED: "ok", ACTIVE: "ok", ACCEPTED: "ok", DELIVERED: "ok", ON_SHELF: "ok", CONFIRMED: "ok",
  AUTHENTIC: "ok", NORMAL: "ok", SOLD: "info", RECEIVED: "info", IN_TRANSIT: "info", ISSUED: "info",
  WARNING: "warn", WATCH: "warn", PENDING: "warn", CREATED: "warn", REVIEW: "warn", EXPECTED: "warn",
  NOT_TESTED: "warn", SUBMITTED: "warn", PROVISIONED: "warn", SIMULATED: "warn",
  CRITICAL: "bad", FAILED: "bad", ANOMALOUS: "bad", EXCEPTION: "bad", REJECTED: "bad", RECALLED: "bad",
  REVOKED: "bad", OFFLINE: "bad", CANCELLED: "muted", CONSUMED: "muted",
};

/** True only when `value` changed from a previous real value (never on mount). */
function useChanged(value: unknown): boolean {
  const prev = useRef(value);
  const [changed, setChanged] = useState(false);
  useEffect(() => {
    if (prev.current !== value && prev.current !== undefined) {
      setChanged(true);
      const id = window.setTimeout(() => setChanged(false), 900);
      prev.current = value;
      return () => window.clearTimeout(id);
    }
    prev.current = value;
  }, [value]);
  return changed;
}

export function Badge({ value }: { value: string | null | undefined }) {
  const changed = useChanged(value);
  if (!value) return <span className="badge muted">-</span>;
  // Text label always present: state is never conveyed by colour alone.
  return <span className={`badge ${TONE[value] ?? "muted"}${changed ? " changed" : ""}`}>{value.replace(/_/g, " ")}</span>;
}

/** A live value that highlights briefly when a NEW reading changes it. */
export function Flash({ value, suffix = "" }: { value: unknown; suffix?: string }) {
  const changed = useChanged(value);
  return <span className={changed ? "flash" : undefined}>{fmt(value)}{value === null || value === undefined ? "" : suffix}</span>;
}

export function Card({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      {(title || actions) && <div className="panel-head">{title && <h2>{title}</h2>}{actions}</div>}
      {children}
    </section>
  );
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return <div aria-busy="true" aria-label="Loading">{Array.from({ length: rows }, (_, i) => <div key={i} className="skeleton" />)}</div>;
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="banner bad" role="alert">
      {message} {onRetry && <button className="link" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted empty">{children}</p>;
}

/** Loading / error / empty / data, consistently. */
export function Async<T>({ r, empty, children }: {
  r: { data: T | null; error: string | null; loading: boolean; reload: () => void };
  empty?: (d: T) => boolean; children: (d: T) => ReactNode;
}) {
  if (r.loading && !r.data) return <Skeleton />;
  if (r.error && !r.data) return <ErrorBox message={r.error} onRetry={r.reload} />;
  if (!r.data) return null;
  if (empty && empty(r.data)) return <Empty>Nothing here yet.</Empty>;
  return <>{children(r.data)}</>;
}

export interface Column<T> { label: string; render: (row: T) => ReactNode }

export function Table<T>({ rows, cols, caption }: { rows: T[]; cols: Column<T>[]; caption?: string }) {
  if (!rows.length) return <Empty>Nothing here yet.</Empty>;
  return (
    <div className="table-wrap">
      <table>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead><tr>{cols.map((c) => <th key={c.label} scope="col">{c.label}</th>)}</tr></thead>
        <tbody>{rows.map((row, i) => <tr key={i}>{cols.map((c) => <td key={c.label} data-label={c.label}>{c.render(row)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

/** Runs a real API action with busy state, success feedback, error text and
 * an optional confirmation step for destructive actions. */
export function Action({ label, run, confirm, onDone, variant }: {
  label: string; run: () => Promise<unknown>; confirm?: string; onDone?: (result: unknown) => void;
  variant?: "danger" | "secondary";
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [asking, setAsking] = useState(false);
  const go = async () => {
    setAsking(false); setBusy(true); setMsg(null);
    try {
      const result = await run();
      setMsg({ ok: true, text: "Done" });
      toast(`${label}: done`);
      onDone?.(result);
    } catch (e) {
      setMsg({ ok: false, text: describeError(e) });
      toast(`${label}: ${describeError(e)}`, false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="action">
      {asking ? (
        <span role="alertdialog" aria-label={confirm} className="confirm">
          {confirm} <button className="danger" onClick={go}>Confirm</button>
          <button className="secondary" onClick={() => setAsking(false)}>Cancel</button>
        </span>
      ) : (
        <button className={variant} disabled={busy} aria-busy={busy} onClick={() => (confirm ? setAsking(true) : go())}>
          {busy ? "Working..." : label}
        </button>
      )}
      {msg && <span className={msg.ok ? "ok-text" : "error"} role="status">{msg.text}</span>}
    </span>
  );
}

/** Simple accessible form: real submit, field labels, server error shown. */
export function Form({ fields, submitLabel, onSubmit, onDone }: {
  fields: { name: string; label: string; type?: string; required?: boolean; placeholder?: string;
            options?: string[]; step?: string; defaultValue?: string }[];
  submitLabel: string;
  onSubmit: (values: Record<string, string>) => Promise<unknown>;
  onDone?: (result: unknown) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  return (
    <form className="form" onSubmit={async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const values = Object.fromEntries(new FormData(form).entries()) as Record<string, string>;
      setBusy(true); setMsg(null);
      try {
        const result = await onSubmit(values);
        setMsg({ ok: true, text: "Saved" });
        toast(`${submitLabel}: saved`);
        form.reset();
        onDone?.(result);
      } catch (err) {
        setMsg({ ok: false, text: describeError(err) });
        toast(`${submitLabel}: ${describeError(err)}`, false);
      } finally {
        setBusy(false);
      }
    }}>
      {fields.map((f) => (
        <label key={f.name}>{f.label}{f.required && <span aria-hidden="true"> *</span>}
          {f.options ? (
            <select name={f.name} required={f.required} defaultValue={f.defaultValue}>
              {f.options.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : (
            <input name={f.name} type={f.type ?? "text"} required={f.required} placeholder={f.placeholder}
                   step={f.step} defaultValue={f.defaultValue} />
          )}
        </label>
      ))}
      <div className="form-foot">
        <button type="submit" disabled={busy} aria-busy={busy}>{busy ? "Saving..." : submitLabel}</button>
        {msg && <span className={msg.ok ? "ok-text" : "error"} role="status">{msg.text}</span>}
      </div>
    </form>
  );
}

/** Minimal SVG line chart for real readings (no chart dependency). */
export function LineChart({ points, label, unit }: { points: { t: string; v: number | null }[]; label: string; unit: string }) {
  const vals = points.filter((p) => p.v !== null) as { t: string; v: number }[];
  if (vals.length < 2) return <Empty>Not enough {label.toLowerCase()} readings to chart.</Empty>;
  const min = Math.min(...vals.map((p) => p.v)), max = Math.max(...vals.map((p) => p.v));
  const span = max - min || 1, W = 320, H = 90;
  const xy = vals.map((p, i) => `${(i / (vals.length - 1)) * W},${H - ((p.v - min) / span) * (H - 10) - 5}`).join(" ");
  return (
    <figure className="chart">
      <figcaption>{label}: <Flash value={vals[vals.length - 1].v} suffix={unit} /> <span className="muted small">(range {min}-{max}{unit}, {vals.length} readings)</span></figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${label} trend, ${vals.length} readings`}>
        {/* keyed by the newest reading: the draw animation replays only when real new data arrives */}
        <polyline key={vals[vals.length - 1].t} points={xy} fill="none" stroke="currentColor" strokeWidth="2" />
        <circle className="last" cx={W} cy={H - ((vals[vals.length - 1].v - min) / span) * (H - 10) - 5} r="3" />
      </svg>
    </figure>
  );
}

export const fmt = (v: unknown) => (v === null || v === undefined || v === "" ? "-" : String(v));

import { useCallback, useEffect, useState } from "react";
import {
  authorityHotspots, authorityOverview, authorityRecalls, authoritySignals,
  type Counts, type Hotspots, type Overview, type RecallView, type Signals,
} from "../services/api";

type Load<T> = { data: T | null; error: string | null; loading: boolean };
const initial = { data: null, error: null, loading: true };

function useLoad<T>(fetcher: () => Promise<T>, deps: unknown[]): Load<T> {
  const [state, setState] = useState<Load<T>>(initial);
  const run = useCallback(fetcher, deps);
  useEffect(() => {
    let alive = true;
    setState(initial);
    run()
      .then((data) => alive && setState({ data, error: null, loading: false }))
      .catch((e: unknown) =>
        alive && setState({ data: null, error: e instanceof Error ? e.message : "Failed", loading: false }));
    return () => { alive = false; };
  }, [run]);
  return state;
}

function Section<T>({ title, load, children }:
  { title: string; load: Load<T>; children: (data: T) => React.ReactNode }) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      {load.loading && <p className="muted">Loading...</p>}
      {load.error && <p className="error" role="alert">Could not load: {load.error}</p>}
      {load.data && children(load.data)}
    </section>
  );
}

const pct = (v: number | null) => (v === null ? "n/a (no data)" : `${v}%`);

function CountList({ counts }: { counts: Counts }) {
  const entries = Object.entries(counts);
  if (!entries.length) return <span className="muted">none</span>;
  return <span>{entries.map(([k, v]) => `${k}: ${v}`).join(" - ")}</span>;
}

function Card({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="card"><div className="muted">{label}</div><div className="value">{value}</div></div>;
}

export default function AuthorityDashboard() {
  const [region, setRegion] = useState("");
  const overview = useLoad<Overview>(() => authorityOverview(region), [region]);
  const hotspots = useLoad<Hotspots>(() => authorityHotspots(region), [region]);
  const signals = useLoad<Signals>(authoritySignals, []);
  const recalls = useLoad<RecallView[]>(authorityRecalls, []);
  const regions = overview.data?.production.by_region.map((r) => r.region) ?? [];

  return (
    <main className="grid">
      <section className="panel toolbar">
        <h1>Authority oversight</h1>
        <label>
          Region{" "}
          <select value={region} onChange={(e) => setRegion(e.target.value)}>
            <option value="">All regions</option>
            {[...new Set([region, ...regions].filter(Boolean))].map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </label>
        {overview.data && <p className="muted small">{overview.data.note}</p>}
      </section>

      <Section title="Production" load={overview}>
        {(o) => (
          <div className="cards">
            <Card label="Active beekeepers" value={o.production.active_beekeepers} />
            <Card label="Active hives" value={o.production.active_hives} />
            <Card label="Harvests" value={o.production.harvests} />
            <Card label="Harvested (kg)" value={o.production.harvest_kg} />
          </div>
        )}
      </Section>

      <Section title="Supply chain" load={overview}>
        {(o) => (
          <ul className="plain">
            <li>Batches: {o.supply_chain.batches}</li>
            <li>Processing events: {o.supply_chain.processing_events}</li>
            <li>Shipments: <CountList counts={o.supply_chain.shipments_by_status} /></li>
            <li>Retail: <CountList counts={o.supply_chain.retail_by_status} /></li>
            <li>Product units: <CountList counts={o.supply_chain.units_by_status} /></li>
          </ul>
        )}
      </Section>

      <Section title="Compliance and quality" load={overview}>
        {(o) => (
          <>
            <div className="cards">
              <Card label="Units traceable to a harvest" value={pct(o.compliance.units_traceable_to_harvest_pct)} />
              <Card label="Batches with a passed lab test" value={pct(o.compliance.batches_with_passed_lab_pct)} />
              <Card label="Active recalls" value={o.compliance.active_recalls} />
            </div>
            <p>Lab results: <CountList counts={o.compliance.lab_results_by_status} /></p>
            <h3>Failed lab tests</h3>
            {o.compliance.failed_lab_batches.length === 0 ? <p className="muted">None recorded.</p> : (
              <table><thead><tr><th>Batch</th><th>Test date</th><th>Laboratory</th></tr></thead>
                <tbody>{o.compliance.failed_lab_batches.map((f, i) => (
                  <tr key={i}><td>{f.batch_id}</td><td>{f.test_date}</td><td>{f.laboratory ?? "-"}</td></tr>))}
                </tbody></table>)}
            <h3>Shipment exceptions</h3>
            {o.compliance.shipment_exceptions.length === 0 ? <p className="muted">None recorded.</p> : (
              <table><thead><tr><th>Batch</th><th>From</th><th>To</th><th>Reason</th></tr></thead>
                <tbody>{o.compliance.shipment_exceptions.map((s, i) => (
                  <tr key={i}><td>{s.batch_id}</td><td>{s.sender ?? "-"}</td><td>{s.receiver ?? "-"}</td>
                    <td>{s.exception_reason ?? "-"}</td></tr>))}</tbody></table>)}
          </>
        )}
      </Section>

      <Section title="Hive health hotspots" load={hotspots}>
        {(h) => h.regions.length === 0 ? <p className="muted">No hives registered.</p> : (
          <>
            <table><thead><tr><th>Region</th><th>Hives</th><th>Critical</th><th>Warning</th>
              <th>Healthy</th><th>No data</th><th>Devices offline</th></tr></thead>
              <tbody>{h.regions.map((r) => (
                <tr key={r.region} className={r.CRITICAL ? "alert" : ""}><td>{r.region}</td><td>{r.hives}</td>
                  <td>{r.CRITICAL}</td><td>{r.WARNING}</td><td>{r.HEALTHY}</td><td>{r.NO_DATA}</td>
                  <td>{r.devices_offline}</td></tr>))}</tbody></table>
            <p className="muted small">Harvest locations: {h.harvest_geo_cells.length} cells ({h.geo_precision}).</p>
          </>
        )}
      </Section>

      <Section title="Suspicious scan signals" load={signals}>
        {(s) => (
          <>
            <p className="notice">{s.note}</p>
            <p className="muted small">
              {s.scans_total} verification attempts; invalid codes not naming a real unit:{" "}
              <CountList counts={s.invalid_scans_without_a_real_unit} />
            </p>
            {s.signals.length === 0 ? <p className="muted">No signals.</p> : (
              <table><thead><tr><th>Severity</th><th>Signal</th><th>Unit</th><th>Batch</th>
                <th>Count</th><th>Last seen</th><th>Evidence</th></tr></thead>
                <tbody>{s.signals.map((g, i) => (
                  <tr key={i} className={`sev-${g.severity}`}><td>{g.severity}</td><td>{g.type}</td>
                    <td>{g.unit_id}</td><td>{g.batch_id ?? "-"}</td><td>{g.occurrences}</td>
                    <td>{g.last_seen}</td><td>{g.evidence}</td></tr>))}</tbody></table>)}
          </>
        )}
      </Section>

      <Section title="Recalls" load={recalls}>
        {(rs) => rs.length === 0 ? <p className="muted">No recalls.</p> : (
          <table><thead><tr><th>Batch</th><th>Status</th><th>Reason</th><th>Affected batches</th>
            <th>Units</th><th>Retailers</th><th>Scans since recall</th></tr></thead>
            <tbody>{rs.map((r) => (
              <tr key={r.recall_id}><td>{r.root_batch_id}</td><td>{r.status}</td><td>{r.reason}</td>
                <td>{r.affected_batches.join(", ")}</td><td><CountList counts={r.units_by_status} /></td>
                <td>{r.retailers.join(", ") || "-"}</td><td>{r.consumer_scans_since_recall}</td></tr>))}
            </tbody></table>)}
      </Section>
    </main>
  );
}

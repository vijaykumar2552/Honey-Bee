import { useState } from "react";
import { post } from "../services/api";
import { useApi } from "../hooks/useApi";
import { link } from "../components/Layout";
import { Action, Async, Badge, Card, Empty, Flash, Form, LineChart, Table, fmt } from "../components/ui";

type Row = Record<string, any>;
const STALE_MINUTES = 15;

const ageMinutes = (ts?: string | null) =>
  ts ? (Date.now() - Date.parse(ts.replace(" ", "T") + "Z")) / 60000 : Infinity;

function Freshness({ ts }: { ts?: string | null }) {
  const age = ageMinutes(ts);
  if (!ts) return <Badge value="NO_DATA" />;
  return age > STALE_MINUTES
    ? <span className="badge warn" title={`Last reading ${Math.round(age)} min ago`}>STALE ({Math.round(age)} min)</span>
    : <span className="badge ok">LIVE</span>;
}

const originNote = (o?: string) =>
  o === "SIMULATED" ? "Simulated device" : o === "PHYSICAL" ? "Physical device" : "Unattributed (legacy webhook)";

export function Dashboard() {
  const summary = useApi<Row>("/api/summary", 10000);
  const latest = useApi<Row[]>("/api/telemetry/latest?limit=10", 10000);
  const notes = useApi<Row[]>("/api/notifications");
  const alerts = (latest.data ?? []).filter((r) => r.health_status !== "HEALTHY");
  return (
    <div className="grid stagger">
      <Card title="Overview">
        <Async r={summary}>{(s) => (
          <div className="cards">
            <div className="card"><div className="muted">Active hives</div><div className="value">{s.active_hives}</div></div>
            <div className="card"><div className="muted">Readings</div><div className="value">{s.reading_count}</div></div>
            <div className="card"><div className="muted">Avg temperature</div><div className="value">{fmt(s.avg_temperature)} C</div></div>
            <div className="card"><div className="muted">Healthy readings</div><div className="value">{fmt(s.overall_health_percent)}%</div></div>
          </div>
        )}</Async>
        <p className="muted small">Refreshes every 10 s from the live API (authenticated polling).</p>
      </Card>
      <Card title="Alerts">
        {latest.loading && !latest.data ? null : alerts.length === 0
          ? <Empty>No WARNING or CRITICAL readings among the latest.</Empty>
          : <Table rows={alerts} cols={[
              { label: "Hive", render: (r) => <a href={link("hives", r.hive_id)}>{r.hive_id}</a> },
              { label: "Status", render: (r) => <Badge value={r.health_status} /> },
              { label: "Reason", render: (r) => r.reason },
              { label: "At", render: (r) => r.timestamp },
            ]} />}
        {(notes.data ?? []).length > 0 && (
          <><h3>Notifications</h3><Table rows={notes.data ?? []} cols={[
            { label: "Type", render: (n) => <Badge value={n.notification_type} /> },
            { label: "Title", render: (n) => n.title },
            { label: "When", render: (n) => n.created_at },
          ]} /></>)}
      </Card>
      <Card title="Live hive telemetry">
        <Async r={latest} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "Hive", render: (r) => <a href={link("hives", r.hive_id)}>{r.hive_id}</a> },
            { label: "Freshness", render: (r) => <Freshness ts={r.timestamp} /> },
            { label: "Temp C", render: (r) => <Flash value={r.temperature} /> },
            { label: "Humidity %", render: (r) => <Flash value={r.humidity} /> },
            { label: "Weight kg", render: (r) => <Flash value={r.weight} /> },
            { label: "Reading at", render: (r) => <span className="small">{r.device_timestamp ?? r.timestamp}</span> },
            { label: "Health", render: (r) => <Badge value={r.health_status} /> },
            { label: "Source", render: (r) => <span className="small">{originNote(r.data_origin)}</span> },
          ]} />
        )}</Async>
      </Card>
    </div>
  );
}

export function Hives({ role }: { role: string }) {
  const hives = useApi<Row[]>("/api/hives");
  const devices = useApi<Row[]>("/api/devices");
  const [secret, setSecret] = useState<Row | null>(null);
  const canWrite = role === "ADMIN" || role === "BEEKEEPER";
  return (
    <div className="grid">
      <Card title="Hives">
        <Async r={hives} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "Hive", render: (h) => <a href={link("hives", h.hive_id)}>{h.hive_id}</a> },
            { label: "Name", render: (h) => fmt(h.name) },
            { label: "Location", render: (h) => fmt(h.location) },
            { label: "Status", render: (h) => <Badge value={h.status} /> },
            { label: "Devices", render: (h) => (devices.data ?? []).filter((d) => d.hive_id === h.hive_id).length },
          ]} />
        )}</Async>
      </Card>
      {canWrite && (
        <Card title="Register a hive">
          <Form submitLabel="Create hive" onDone={hives.reload} fields={[
            { name: "hive_id", label: "Hive ID", required: true, placeholder: "HIVE-001" },
            { name: "name", label: "Name" }, { name: "location", label: "Location (region)" },
          ]} onSubmit={(v) => post("/api/hives", v)} />
        </Card>
      )}
      {canWrite && (
        <Card title="Register a device">
          <Form submitLabel="Register device" onDone={(r) => { setSecret((r as Row).credentials); devices.reload(); }} fields={[
            { name: "hive_id", label: "Hive ID", required: true },
            { name: "data_origin", label: "Device type", options: ["PHYSICAL", "SIMULATED"] },
          ]} onSubmit={(v) => post("/api/devices", v)} />
          {secret && (
            <div className="banner warn" role="alert">
              <strong>Store this device secret now - it is shown only once.</strong>
              <div>Device: <code>{secret.device_id}</code></div>
              <div>Secret: <code className="wrap">{secret.device_secret}</code></div>
              <button className="secondary" onClick={() => setSecret(null)}>I have stored it</button>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

export function HiveDetail({ hiveId, role }: { hiveId: string; role: string }) {
  const history = useApi<Row[]>(`/api/hives/${encodeURIComponent(hiveId)}/history`, 15000);
  const devices = useApi<Row[]>("/api/devices");
  const mine = (devices.data ?? []).filter((d) => d.hive_id === hiveId);
  const series = (key: string) => [...(history.data ?? [])].reverse().slice(-60)
    .map((r) => ({ t: r.timestamp as string, v: r[key] as number | null }));
  const latest = history.data?.[0];
  return (
    <div className="grid">
      <Card title={`Hive ${hiveId}`}>
        <Async r={history} empty={(d) => d.length === 0}>{() => (
          <>
            <p>Latest: <Badge value={latest?.health_status} /> <Freshness ts={latest?.timestamp} />{" "}
              <span className="muted small">{latest?.reason}</span></p>
            <div className="charts">
              <LineChart label="Temperature" unit=" C" points={series("temperature")} />
              <LineChart label="Humidity" unit=" %" points={series("humidity")} />
              <LineChart label="Weight" unit=" kg" points={series("weight")} />
            </div>
          </>
        )}</Async>
      </Card>
      <Card title="Devices">
        <Table rows={mine} cols={[
          { label: "Device", render: (d) => d.device_id },
          { label: "Status", render: (d) => <Badge value={d.status} /> },
          { label: "Type", render: (d) => <Badge value={d.data_origin} /> },
          { label: "Last seen", render: (d) => <>{fmt(d.last_seen_at)} <Freshness ts={d.last_seen_at} /></> },
          { label: "Battery", render: (d) => fmt(d.battery_level) },
          { label: "", render: (d) => (role === "ADMIN" || role === "BEEKEEPER") && d.status !== "REVOKED"
            ? <Action label="Revoke" variant="danger" confirm={`Revoke ${d.device_id}? It will stop being accepted.`}
                      run={() => post(`/api/devices/${d.device_id}/revoke`)} onDone={devices.reload} /> : null },
        ]} />
      </Card>
    </div>
  );
}

export function Insights() {
  const ml = useApi<Row>("/api/ml/status");
  const latest = useApi<Row[]>("/api/telemetry/latest?limit=20", 15000);
  return (
    <div className="grid">
      <Card title="Model status">
        <Async r={ml}>{(m) => (
          <>
            <p><Badge value={m.available ? "ACTIVE" : "OFFLINE"} /> {fmt(m.model_version)}{" "}
              {m.error && <span className="error">{m.error}</span>}</p>
            <p>Training data: <Badge value={m.training_data_source} />{" "}
              {m.real_world_data_used === false && <strong>No real-world data was used to train this model.</strong>}</p>
            <p className="banner warn">{m.notice} Real-world accuracy: <strong>not measured</strong> (no labelled real hive data).</p>
            <p className="small muted">ML may raise a reading to at most <Badge value={m.fusion_policy?.ml_max_escalation} />;
              deterministic rules stay authoritative. ML is not applied when sensors are missing or flagged.</p>
          </>
        )}</Async>
      </Card>
      <Card title="Recent intelligence output">
        <Async r={latest} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "Hive", render: (r) => <a href={link("hives", r.hive_id)}>{r.hive_id}</a> },
            { label: "Health (rules)", render: (r) => <Badge value={r.health_status} /> },
            { label: "ML band", render: (r) => <Badge value={r.ml_band} /> },
            { label: "Score", render: (r) => fmt(r.anomaly_score) },
            { label: "Sensor quality", render: (r) => r.sensor_flags
              ? <span className="badge warn">{r.sensor_flags}</span> : <span className="muted">no flags</span> },
            { label: "Data", render: (r) => <Badge value={r.data_origin} /> },
            { label: "Explanation", render: (r) => <span className="small">{r.reason}</span> },
          ]} />
        )}</Async>
      </Card>
    </div>
  );
}

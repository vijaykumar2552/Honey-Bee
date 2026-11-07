import { Fragment, useState } from "react";
import { describeError, get, post, type User } from "../services/api";
import { useApi } from "../hooks/useApi";
import { link } from "../components/Layout";
import { Action, Async, Badge, Card, Table, fmt } from "../components/ui";

type Row = Record<string, any>;

function EventProof({ eventId }: { eventId: string }) {
  const [proof, setProof] = useState<Row | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (err) return <span className="error">{err}</span>;
  if (!proof) return <button className="secondary" onClick={() =>
    get<Row>(`/api/chain/events/${eventId}`).then((e) => setProof(e.proof)).catch((e) => setErr(describeError(e)))}>Check anchoring</button>;
  if (!proof.checkpoint_id) return <span className="badge warn">recorded, not yet in a checkpoint</span>;
  return (
    <span className="small">
      <Badge value={proof.checkpoint_status} /> {proof.checkpoint_id}{" "}
      {proof.proof_valid ? <span className="ok-text">inclusion proof valid</span> : <span className="error">proof invalid</span>}
      {proof.tx_hash ? <div>tx <code className="wrap">{proof.tx_hash}</code> (chain {proof.chain_id})</div> : <div className="muted">no transaction yet</div>}
    </span>
  );
}

function CheckpointVerify({ id }: { id: string }) {
  const [v, setV] = useState<Row | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (err) return <span className="error">{err}</span>;
  if (!v) return <button className="secondary" onClick={() =>
    get<Row>(`/api/chain/checkpoints/${id}/verify`).then(setV).catch((e) => setErr(describeError(e)))}>Verify</button>;
  return <span className="small">local root {v.local_root_valid ? <Badge value="CONFIRMED" /> : <Badge value="FAILED" />} on-chain <Badge value={String(v.onchain).split(":")[0]} /></span>;
}

export function Blockchain({ user }: { user: User }) {
  const oversight = user.role === "ADMIN" || user.role === "AUTHORITY";
  const events = useApi<Row[]>("/api/chain/events");
  const checkpoints = useApi<Row[]>(oversight ? "/api/chain/checkpoints" : null);
  const [log, setLog] = useState<Row | null>(null);
  return (
    <div className="grid">
      <Card title="How anchoring works">
        <p className="small">Every supply-chain event is appended to a hash-linked log. Periodically a Merkle
          root over new events is anchored on-chain in one transaction (checkpoint). An event is shown as anchored
          only when its checkpoint is CONFIRMED by a mined transaction - hashes are never fabricated.</p>
      </Card>
      {oversight && (
        <Card title="Checkpoints" actions={<span className="action">
          <Action label="Verify whole log" variant="secondary" run={() => get<Row>("/api/chain/log/verify")} onDone={(r) => setLog(r as Row)} />
          {user.role === "ADMIN" && <>
            <Action label="Create checkpoint" run={() => post("/api/chain/checkpoints")} onDone={checkpoints.reload} />
            <Action label="Submit to chain" run={() => post("/api/chain/checkpoints/submit")} onDone={checkpoints.reload} /></>}
        </span>}>
          {log && <p className={log.valid ? "banner good" : "banner bad"} role="status">
            Event log {log.valid ? "valid" : "INVALID"}: {log.event_count} events{log.problems?.length ? `, ${log.problems.length} problem(s)` : ""}</p>}
          <Async r={checkpoints} empty={(d) => d.length === 0}>{(rows) => (
            <Table rows={rows} cols={[
              { label: "Checkpoint", render: (c) => c.checkpoint_id },
              { label: "Events", render: (c) => `${c.from_seq}-${c.to_seq}` },
              { label: "Status", render: (c) => <Badge value={c.status} /> },
              { label: "Transaction", render: (c) => c.tx_hash ? <code className="wrap small">{c.tx_hash}</code> : <span className="muted">none</span> },
              { label: "Block", render: (c) => fmt(c.block_number) },
              { label: "Error", render: (c) => c.last_error ? <span className="small error">{c.last_error}</span> : "" },
              { label: "", render: (c) => <CheckpointVerify id={c.checkpoint_id} /> },
            ]} />)}</Async>
        </Card>)}
      <Card title="Recorded events">
        <Async r={events} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "#", render: (e) => e.seq },
            { label: "Event", render: (e) => <Badge value={e.event_type} /> },
            { label: "Resource", render: (e) => e.resource_type === "honey_batches"
              ? <a href={link("batches", e.resource_id)}>{e.resource_id}</a> : e.resource_id },
            { label: "When", render: (e) => e.occurred_at },
            { label: "Event hash", render: (e) => <code className="small">{String(e.event_hash).slice(0, 16)}...</code> },
            { label: "Anchoring", render: (e) => <EventProof eventId={e.event_id} /> },
          ]} />)}</Async>
      </Card>
    </div>
  );
}

export function Settings({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const health = useApi<Row>("/api/health");
  const orgs = useApi<Row[]>(user.role === "ADMIN" || user.role === "AUTHORITY" ? "/api/admin/organizations" : null);
  return (
    <div className="grid">
      <Card title="Profile and session">
        <dl className="dl">
          <dt>Name</dt><dd>{user.name}</dd><dt>Email</dt><dd>{user.email}</dd>
          <dt>Role</dt><dd><Badge value={user.role} /></dd><dt>Organization</dt><dd>{fmt(user.organization_id)}</dd>
        </dl>
        <p className="muted small">Your session token is held only in this browser tab and is cleared when you sign out or close the tab.</p>
        <button onClick={onSignOut}>Sign out</button>
      </Card>
      {(user.role === "ADMIN" || user.role === "AUTHORITY") && (
        <Card title="Organizations">
          <Async r={orgs} empty={(d) => d.length === 0}>{(rows) => (
            <Table rows={rows} cols={[
              { label: "ID", render: (o) => o.org_id }, { label: "Name", render: (o) => o.name },
              { label: "Type", render: (o) => o.org_type }, { label: "Status", render: (o) => <Badge value={o.status} /> },
            ]} />)}</Async>
        </Card>)}
      <Card title="System status">
        <Async r={health}>{(h) => (
          <dl className="dl">
            <dt>Configuration</dt><dd>{h.configuration_ok ? <Badge value="CONFIRMED" /> : <Badge value="FAILED" />}</dd>
            {Object.entries(h).filter(([, v]) => typeof v !== "object").map(([k, v]) =>
              <Fragment key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{String(v)}</dd></Fragment>)}
          </dl>)}</Async>
      </Card>
    </div>
  );
}

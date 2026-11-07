import { useState } from "react";
import { describeError, post, type User } from "../services/api";
import { useApi } from "../hooks/useApi";
import { link } from "../components/Layout";
import { Action, Async, Badge, Card, Empty, ErrorBox, Form, Table, fmt } from "../components/ui";

type Row = Record<string, any>;
const HOLDERS = ["BEEKEEPER", "FPO_AGGREGATOR", "PROCESSOR", "LOGISTICS", "RETAILER"];
const num = (v: string) => (v === "" || v === undefined ? undefined : Number(v));
const clean = (o: Row) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v !== undefined));

export function Harvests({ role }: { role: string }) {
  const list = useApi<Row[]>("/api/harvest");
  return (
    <div className="grid">
      {(role === "BEEKEEPER" || role === "ADMIN") && (
        <Card title="Record a harvest">
          <p className="muted small">Creates the harvest event, its batch and ledger block, and a BATCH_CREATED chain event.</p>
          <Form submitLabel="Record harvest" onDone={list.reload} fields={[
            { name: "hive_id", label: "Hive ID", required: true },
            { name: "harvest_date", label: "Harvest date", type: "date", required: true },
            { name: "total_weight", label: "Weight (kg)", type: "number", step: "0.01", required: true },
            { name: "floral_type", label: "Floral source", required: true },
            { name: "latitude", label: "Latitude", type: "number", step: "any" },
            { name: "longitude", label: "Longitude", type: "number", step: "any" },
          ]} onSubmit={(v) => post("/api/harvest", clean({ ...v, total_weight: num(v.total_weight),
            latitude: num(v.latitude), longitude: num(v.longitude) }))} />
        </Card>
      )}
      <Card title="Harvest history">
        <Async r={list} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "Harvest", render: (h) => h.harvest_id },
            { label: "Hive", render: (h) => <a href={link("hives", h.hive_id)}>{h.hive_id}</a> },
            { label: "Date", render: (h) => h.harvest_date },
            { label: "kg", render: (h) => h.quantity_kg },
            { label: "Batch", render: (h) => (h.batches ?? []).map((b: Row) =>
              <a key={b.batch_id} href={link("batches", b.batch_id)}>{b.batch_id}</a>) },
            { label: "Status", render: (h) => <Badge value={h.status} /> },
          ]} />
        )}</Async>
      </Card>
    </div>
  );
}

export function Batches({ role }: { role: string }) {
  const list = useApi<Row[]>("/api/batches");
  return (
    <div className="grid">
      <Card title="Batches">
        <Async r={list} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "Batch", render: (b) => <a href={link("batches", b.batch_id)}>{b.batch_id}</a> },
            { label: "Floral", render: (b) => b.floral_type },
            { label: "Harvested", render: (b) => b.harvest_date },
            { label: "kg", render: (b) => b.total_weight },
            { label: "Hive(s)", render: (b) => b.hive_id },
            { label: "Anchored", render: (b) => b.anchor_tx_hash ? <Badge value="CONFIRMED" /> : <span className="muted">not yet</span> },
          ]} />
        )}</Async>
      </Card>
      {role === "PROCESSOR" && (
        <Card title="Blend batches">
          <p className="muted small">Format: <code>HNY-2026-0001:5, HNY-2026-0002:3.5</code> (batch:kg). Every input stays traceable.</p>
          <Form submitLabel="Create blend" onDone={list.reload} fields={[
            { name: "inputs", label: "Input batches", required: true },
            { name: "floral_type", label: "Floral label of the blend" },
          ]} onSubmit={(v) => post("/api/blends", clean({ floral_type: v.floral_type,
            inputs: v.inputs.split(",").map((p) => p.trim()).filter(Boolean).map((p) => {
              const [batch_id, kg] = p.split(":"); return { batch_id: batch_id.trim(), weight_kg: Number(kg) }; }) }))} />
        </Card>
      )}
    </div>
  );
}

type StageState = "done" | "pending" | "none" | "failed" | "current";
const STAGE_TEXT: Record<StageState, string> = {
  done: "(confirmed)", pending: "(in progress)", none: "(not reached)", failed: "(blocked)", current: "(current stage)" };

function Stage({ name, state, children }: { name: string; state: StageState; children?: React.ReactNode }) {
  return (
    <li className={`stage ${state}`} aria-current={state === "current" ? "step" : undefined}>
      <span className="dot" aria-hidden="true" />
      <div><strong>{name}</strong> <span className="small muted">{STAGE_TEXT[state]}</span>
        <div className="small">{children}</div></div>
    </li>
  );
}

export function BatchDetail({ batchId, user }: { batchId: string; user: User }) {
  const trace = useApi<Row>(`/api/batches/${encodeURIComponent(batchId)}/trace`);
  const units = useApi<Row[]>(`/api/batches/${encodeURIComponent(batchId)}/units`);
  const [issued, setIssued] = useState<Row[] | null>(null);
  const [qr, setQr] = useState<Record<string, string>>({});
  const reload = () => { trace.reload(); units.reload(); };
  const role = user.role;
  const holder = trace.data?.state?.holder_organization_id === user.organization_id;

  const showQr = async (unit: Row) => {
    try {
      const r = await post<Row>(`/api/units/${unit.unit_id}/qr`, { unit_secret: unit.unit_secret });
      setQr((q) => ({ ...q, [unit.unit_id]: r.qr_png_base64 }));
    } catch (e) { setQr((q) => ({ ...q, [unit.unit_id]: "ERROR:" + describeError(e) })); }
  };

  return (
    <Async r={trace}>{(t) => {
      const shipped = (t.shipments ?? []) as Row[]; const retail = (t.retail ?? []) as Row[];
      const custody = (t.custody ?? []) as Row[]; const events = (t.chain_events ?? []) as Row[];
      const unitCount = (units.data ?? []).length;
      const st: Record<string, StageState> = {
        hive: t.hive_id ? "done" : "none",
        harvest: (t.harvests ?? []).length || (t.blended_from ?? []).length ? "done" : "none",
        batch: "done",
        processing: (t.processing ?? []).length ? "done" : "none",
        lab: t.lab?.status === "PASSED" ? "done" : t.lab?.status === "FAILED" ? "failed"
          : t.lab?.status === "NOT_TESTED" ? "none" : "pending",
        packaging: unitCount ? "done" : "none",
        blockchain: events.length ? "done" : "none",
        custody: custody.some((c) => c.status === "ACCEPTED") ? "done" : custody.length ? "pending" : "none",
        shipment: shipped.some((s) => s.status === "EXCEPTION") ? "failed"
          : shipped.some((s) => s.status === "DELIVERED") ? "done" : shipped.length ? "pending" : "none",
        retail: retail.some((r) => ["ON_SHELF", "SOLD"].includes(r.status)) ? "done" : retail.length ? "pending" : "none",
        consumer: (units.data ?? []).some((u) => u.status === "SOLD") ? "done" : "none",
      };
      if (t.recall) st.retail = st.consumer = "failed";
      const order = Object.keys(st);
      const firstOpen = order.find((k) => st[k] === "none");
      if (firstOpen && order.slice(0, order.indexOf(firstOpen)).every((k) => st[k] === "done")) st[firstOpen] = "current";
      return (
        <div className="grid">
          {t.recall && <div className="banner bad" role="alert"><strong>RECALLED:</strong> {t.recall.reason} ({t.recall.issued_at})</div>}
          <Card title={`Batch ${t.batch_id}`}>
            <p>{t.floral_type} - {t.total_weight} kg - harvested {t.harvest_date} - held by{" "}
              <strong>{fmt(t.state?.holder_organization_id)}</strong> - <Badge value={t.state?.status} />{" "}
              {fmt(t.state?.remaining_kg)} kg remaining - ledger {t.ledger_verified ? <Badge value="CONFIRMED" /> : <Badge value="FAILED" />}</p>
            <ol className="journey" aria-label="Supply-chain journey">
              <Stage name="Hive" state={st.hive}>{t.hive_id}</Stage>
              <Stage name="Harvest" state={st.harvest}>
                {(t.harvests ?? []).map((h: Row) => `${h.harvest_id} (${h.harvest_date})`).join(", ")}
                {(t.blended_from ?? []).length > 0 && <>Blend of {(t.blended_from as Row[]).map((b) =>
                  <a key={b.batch_id} href={link("batches", b.batch_id)}> {b.batch_id} ({b.weight_kg} kg)</a>)}</>}</Stage>
              <Stage name="Batch" state={st.batch}>block #{t.block_index}</Stage>
              <Stage name="Processing" state={st.processing}>
                {(t.processing ?? []).map((p: Row) => `${p.process_type} ${p.input_kg ?? ""}->${p.output_kg ?? ""} kg`).join("; ")}</Stage>
              <Stage name="Lab" state={st.lab}>
                <Badge value={t.lab?.status} />{st.lab === "failed" && " - packaging is blocked by the server until a test passes."}</Stage>
              <Stage name="Packaging" state={st.packaging}>{unitCount ? `${unitCount} units` : ""}</Stage>
              <Stage name="Blockchain" state={st.blockchain}>{events.length} events recorded -{" "}
                <a href={link("blockchain")}>anchoring status</a></Stage>
              <Stage name="Custody" state={st.custody}>
                {custody.map((c) => `${c.from_organization_id}->${c.to_organization_id} ${c.status}`).join("; ")}</Stage>
              <Stage name="Shipment" state={st.shipment}>
                {shipped.map((s) => `${s.shipment_id} ${s.status}`).join("; ")}</Stage>
              <Stage name="Retail" state={st.retail}>
                {retail.map((r) => `${r.retailer_organization_id} ${r.status}`).join("; ")}</Stage>
              <Stage name="Consumer" state={st.consumer}>
                Consumers verify units at <a href="/app/verify">/app/verify</a> by scanning the label.</Stage>
            </ol>
          </Card>

          <Card title="Laboratory results">
            {(t.lab?.results ?? []).length === 0 ? <Empty>No completed lab results.</Empty> :
              <Table rows={t.lab.results} cols={[
                { label: "Result", render: (r: Row) => <Badge value={r.status} /> },
                { label: "Lab", render: (r: Row) => r.lab_organization_id },
                { label: "Test date", render: (r: Row) => fmt(r.test_date) },
                { label: "Moisture %", render: (r: Row) => fmt(r.moisture_pct) },
                { label: "HMF mg/kg", render: (r: Row) => fmt(r.hmf_mg_kg) },
                { label: "C4 %", render: (r: Row) => fmt(r.c4_sugar_pct) },
                { label: "Certificate", render: (r: Row) => fmt(r.certificate_id) },
              ]} />}
            {holder && HOLDERS.includes(role) && (
              <Form submitLabel="Request lab test" onDone={reload} fields={[
                { name: "lab_organization_id", label: "Laboratory organization ID", required: true, placeholder: "ORG-..." },
              ]} onSubmit={(v) => post(`/api/batches/${batchId}/lab-requests`, v)} />)}
          </Card>

          {role === "PROCESSOR" && holder && (
            <Card title="Process / package">
              <h3>Record processing</h3>
              <Form submitLabel="Record processing" onDone={reload} fields={[
                { name: "process_type", label: "Process", options: ["FILTERING", "HEATING", "SETTLING", "STORAGE"] },
                { name: "output_kg", label: "Output (kg)", type: "number", step: "0.01", required: true },
                { name: "temperature_c", label: "Temperature C", type: "number", step: "0.1" },
              ]} onSubmit={(v) => post(`/api/batches/${batchId}/processing`, clean({ process_type: v.process_type,
                output_kg: num(v.output_kg), temperature_c: num(v.temperature_c) }))} />
              <h3>Package into product units</h3>
              <p className="muted small">The server refuses packaging unless a lab result has PASSED
                (current lab status: <Badge value={t.lab?.status} />). If it refuses, the reason is shown below the button.</p>
              <Form submitLabel="Package" onDone={(r) => { setIssued((r as Row).units); reload(); }} fields={[
                { name: "unit_size_g", label: "Unit size (g)", type: "number", required: true },
                { name: "unit_count", label: "Number of units", type: "number", required: true },
                { name: "best_before", label: "Best before", type: "date", required: true },
              ]} onSubmit={(v) => post(`/api/batches/${batchId}/packaging`, { unit_size_g: num(v.unit_size_g),
                unit_count: num(v.unit_count), best_before: v.best_before })} />
              {issued && (
                <div className="banner warn" role="alert">
                  <strong>Print these labels now. Unit secrets are shown only once.</strong>
                  <Table rows={issued} cols={[
                    { label: "Unit", render: (u) => u.unit_id },
                    { label: "Secret", render: (u) => <code className="wrap small">{u.unit_secret}</code> },
                    { label: "QR", render: (u) => qr[u.unit_id]
                      ? (qr[u.unit_id].startsWith("ERROR:") ? <span className="error">{qr[u.unit_id].slice(6)}</span>
                        : <img alt={`QR code for ${u.unit_id}`} width={120} src={`data:image/png;base64,${qr[u.unit_id]}`} />)
                      : <button className="secondary" onClick={() => showQr(u)}>Show QR</button> },
                  ]} />
                  <button className="secondary" onClick={() => setIssued(null)}>Labels printed - hide secrets</button>
                </div>)}
            </Card>)}

          <Card title="Product units">
            <Async r={units} empty={(d) => d.length === 0}>{(rows) => (
              <Table rows={rows} cols={[
                { label: "Unit", render: (u) => u.unit_id },
                { label: "Status", render: (u) => <Badge value={u.status} /> },
                { label: "Packaging", render: (u) => u.packaging_id },
                { label: "", render: (u) => (role === "PROCESSOR" || role === "ADMIN")
                  ? <Action label="Revoke label" variant="danger" confirm={`Revoke the label of ${u.unit_id}? Scans will show REVOKED.`}
                            run={() => post(`/api/units/${u.unit_id}/revoke-qr`)} onDone={units.reload} /> : null },
              ]} />)}</Async>
            {(role === "PROCESSOR" || role === "ADMIN") && (
              <><h3>Re-issue a unit QR</h3><p className="muted small">Requires the unit secret issued at packaging.</p>
              <ReissueForm /></>)}
          </Card>

          {holder && HOLDERS.includes(role) && (
            <Card title="Hand over custody">
              <Form submitLabel="Offer transfer" onDone={reload} fields={[
                { name: "to_organization_id", label: "Receiving organization ID", required: true, placeholder: "ORG-..." },
              ]} onSubmit={(v) => post("/api/custody/transfers", { batch_id: batchId, ...v })} />
            </Card>)}
          {(role === "AUTHORITY" || role === "ADMIN") && !t.recall && (
            <Card title="Recall this batch"><RecallForm batchId={batchId} onDone={reload} /></Card>)}
        </div>
      );
    }}</Async>
  );
}

function ReissueForm() {
  const [png, setPng] = useState<string | null>(null);
  return (
    <>
      <Form submitLabel="Re-issue QR" onDone={(r) => setPng((r as Row).qr_png_base64)} fields={[
        { name: "unit_id", label: "Unit ID", required: true }, { name: "unit_secret", label: "Unit secret", type: "password", required: true },
      ]} onSubmit={(v) => post(`/api/units/${encodeURIComponent(v.unit_id)}/qr`, { unit_secret: v.unit_secret })} />
      {png && <img alt="Re-issued QR code" width={160} src={`data:image/png;base64,${png}`} />}
    </>
  );
}

function RecallForm({ batchId, onDone }: { batchId?: string; onDone: () => void }) {
  return (
    <Form submitLabel="Issue recall" onDone={onDone} fields={[
      ...(batchId ? [] : [{ name: "batch_id", label: "Batch ID", required: true }]),
      { name: "reason", label: "Reason (shown to consumers)", required: true },
    ]} onSubmit={(v) => post("/api/recalls", { batch_id: batchId ?? v.batch_id, reason: v.reason })} />
  );
}

export function Lab() {
  const reqs = useApi<Row[]>("/api/lab/requests");
  const today = new Date().toISOString().slice(0, 10);
  return (
    <Card title="Laboratory requests">
      <Async r={reqs} empty={(d) => d.length === 0}>{(rows) => (
        <Table rows={rows} cols={[
          { label: "Request", render: (r) => r.lab_result_id },
          { label: "Batch", render: (r) => <a href={link("batches", r.batch_id)}>{r.batch_id}</a> },
          { label: "Status", render: (r) => <Badge value={r.status} /> },
          { label: "Record result", render: (r) => r.status !== "PENDING" ? <span className="muted">recorded (immutable)</span> : (
            <Form submitLabel="Submit result" onDone={reqs.reload} fields={[
              { name: "status", label: "Verdict", options: ["PASSED", "FAILED", "REVIEW"] },
              { name: "test_date", label: "Test date", type: "date", required: true, defaultValue: today },
              { name: "moisture_pct", label: "Moisture %", type: "number", step: "0.01" },
              { name: "hmf_mg_kg", label: "HMF mg/kg", type: "number", step: "0.01" },
              { name: "c4_sugar_pct", label: "C4 sugar %", type: "number", step: "0.01" },
              { name: "certificate_id", label: "Certificate reference" },
            ]} onSubmit={(v) => post(`/api/lab/results/${r.lab_result_id}`, clean({ ...v, moisture_pct: num(v.moisture_pct),
              hmf_mg_kg: num(v.hmf_mg_kg), c4_sugar_pct: num(v.c4_sugar_pct) }))} />) },
        ]} />)}</Async>
    </Card>
  );
}

export function Custody({ user }: { user: User }) {
  const list = useApi<Row[]>("/api/custody/transfers");
  const act = (id: string, action: string, body?: Row) => post(`/api/custody/transfers/${id}/${action}`, body);
  return (
    <Card title="Custody transfers">
      <Async r={list} empty={(d) => d.length === 0}>{(rows) => (
        <Table rows={rows} cols={[
          { label: "Transfer", render: (t) => t.transfer_id },
          { label: "Batch", render: (t) => <a href={link("batches", t.batch_id)}>{t.batch_id}</a> },
          { label: "From", render: (t) => t.from_organization_id }, { label: "To", render: (t) => t.to_organization_id },
          { label: "kg", render: (t) => fmt(t.quantity_kg) },
          { label: "Status", render: (t) => <Badge value={t.status} /> },
          { label: "Actions", render: (t) => t.status !== "PENDING" ? null : t.to_organization_id === user.organization_id ? (
            <><Action label="Accept" run={() => act(t.transfer_id, "accept")} onDone={list.reload} />
              <Action label="Reject" variant="danger" confirm="Reject this transfer?" run={() => act(t.transfer_id, "reject", { reason: "Rejected by receiver" })} onDone={list.reload} /></>
          ) : t.from_organization_id === user.organization_id ? (
            <Action label="Cancel" variant="danger" confirm="Cancel this transfer?" run={() => act(t.transfer_id, "cancel")} onDone={list.reload} />
          ) : null },
        ]} />)}</Async>
      <p className="muted small">Acceptance is refused by the server until any shipment for the transfer is DELIVERED.</p>
    </Card>
  );
}

const NEXT: Record<string, string[]> = { CREATED: ["IN_TRANSIT", "EXCEPTION"], IN_TRANSIT: ["DELIVERED", "EXCEPTION"], EXCEPTION: ["IN_TRANSIT"] };

function ShipmentActions({ s, onDone }: { s: Row; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <span className="action">
      {(NEXT[s.status] ?? []).map((to) => to === "EXCEPTION" ? (
        <span key={to}><label className="sr-only" htmlFor={`r-${s.shipment_id}`}>Exception reason</label>
          <input id={`r-${s.shipment_id}`} placeholder="Exception reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          <Action label="Report exception" variant="danger" run={async () => {
            setErr(null);
            if (reason.trim().length < 5) { setErr("Give a reason of at least 5 characters."); throw new Error("Reason required"); }
            return post(`/api/shipments/${s.shipment_id}/status`, { status: "EXCEPTION", exception_reason: reason });
          }} onDone={onDone} /></span>
      ) : <Action key={to} label={`Mark ${to.replace("_", " ").toLowerCase()}`}
                 run={() => post(`/api/shipments/${s.shipment_id}/status`, { status: to })} onDone={onDone} />)}
      {err && <span className="error">{err}</span>}
    </span>
  );
}

export function Shipments({ user }: { user: User }) {
  const list = useApi<Row[]>("/api/shipments");
  return (
    <div className="grid">
      <Card title="Shipments">
        <Async r={list} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "Shipment", render: (s) => s.shipment_id },
            { label: "Batch", render: (s) => <a href={link("batches", s.batch_id)}>{s.batch_id}</a> },
            { label: "Route", render: (s) => `${fmt(s.origin)} -> ${fmt(s.destination)}` },
            { label: "To", render: (s) => s.to_organization_id },
            { label: "Status", render: (s) => <><Badge value={s.status} />{s.exception_reason && <div className="small error">{s.exception_reason}</div>}</> },
            { label: "Update", render: (s) => s.from_organization_id === user.organization_id
              ? <ShipmentActions s={s} onDone={list.reload} /> : null },
          ]} />)}</Async>
      </Card>
      {HOLDERS.includes(user.role) && (
        <Card title="Create shipment">
          <p className="muted small">Ships the goods of a PENDING custody transfer you initiated.</p>
          <Form submitLabel="Create shipment" onDone={list.reload} fields={[
            { name: "custody_transfer_id", label: "Custody transfer ID", required: true, placeholder: "CUS-..." },
            { name: "origin", label: "Origin" }, { name: "destination", label: "Destination" },
            { name: "vehicle", label: "Vehicle" }, { name: "driver", label: "Driver" },
          ]} onSubmit={(v) => post("/api/shipments", clean(v))} />
        </Card>)}
    </div>
  );
}

export function Retail({ role }: { role: string }) {
  const list = useApi<Row[]>("/api/retail");
  const isRetailer = role === "RETAILER";
  return (
    <div className="grid">
      <Card title="Retail records">
        <Async r={list} empty={(d) => d.length === 0}>{(rows) => (
          <Table rows={rows} cols={[
            { label: "Record", render: (r) => r.retail_record_id },
            { label: "Batch", render: (r) => <a href={link("batches", r.batch_id)}>{r.batch_id}</a> },
            { label: "Retailer", render: (r) => r.retailer_organization_id },
            { label: "Status", render: (r) => <Badge value={r.status} /> },
            { label: "Received", render: (r) => fmt(r.received_at) }, { label: "On shelf", render: (r) => fmt(r.shelved_at) },
            { label: "", render: (r) => !isRetailer ? null
              : r.status === "EXPECTED" ? <Action label="Receive" run={() => post(`/api/retail/${r.retail_record_id}/receive`)} onDone={list.reload} />
              : r.status === "RECEIVED" ? <Action label="Put on shelf" run={() => post(`/api/retail/${r.retail_record_id}/shelve`)} onDone={list.reload} />
              : null },
          ]} />)}</Async>
      </Card>
      {isRetailer && (
        <Card title="Sell a unit">
          <Form submitLabel="Mark sold" onDone={list.reload} fields={[{ name: "unit_id", label: "Unit ID", required: true }]}
                onSubmit={(v) => post(`/api/retail/units/${encodeURIComponent(v.unit_id)}/sell`)} />
        </Card>)}
    </div>
  );
}

export function Recalls({ role }: { role: string }) {
  const list = useApi<Row[]>("/api/recalls");
  const oversight = role === "AUTHORITY" || role === "ADMIN";
  const detail = useApi<Row[]>(oversight ? "/api/authority/recalls" : null);
  return (
    <div className="grid">
      {oversight && <Card title="Issue a recall"><RecallForm onDone={() => { list.reload(); detail.reload(); }} /></Card>}
      <Card title="Recalls">
        {oversight ? (
          <Async r={detail} empty={(d) => d.length === 0}>{(rows) => (
            <Table rows={rows} cols={[
              { label: "Batch", render: (r) => <a href={link("batches", r.root_batch_id)}>{r.root_batch_id}</a> },
              { label: "Status", render: (r) => <Badge value={r.status} /> }, { label: "Reason", render: (r) => r.reason },
              { label: "Affected batches", render: (r) => r.affected_batches.join(", ") },
              { label: "Units", render: (r) => Object.entries(r.units_by_status).map(([k, v]) => `${k}: ${v}`).join(", ") || "-" },
              { label: "Retailers", render: (r) => r.retailers.join(", ") || "-" },
              { label: "Consumer scans since", render: (r) => r.consumer_scans_since_recall },
            ]} />)}</Async>
        ) : (
          <Async r={list} empty={(d) => d.length === 0}>{(rows) => (
            <Table rows={rows} cols={[
              { label: "Batch", render: (r) => <a href={link("batches", r.batch_id)}>{r.batch_id}</a> },
              { label: "Status", render: (r) => <Badge value={r.status} /> },
              { label: "Reason", render: (r) => r.reason }, { label: "Issued", render: (r) => r.issued_at },
            ]} />)}</Async>
        )}
        <p className="muted small">Consumers scanning any affected unit see the recall notice at /app/verify.</p>
      </Card>
    </div>
  );
}

export function NotAllowed({ what }: { what: string }) {
  return <ErrorBox message={`Your role cannot open ${what}.`} />;
}

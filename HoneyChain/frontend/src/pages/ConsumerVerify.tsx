import { useEffect, useState, type FormEvent } from "react";
import { codeFromLocation, INVALID_REASONS, verifyCode, type BatchView, type VerifyResult } from "../services/verify";

function RecallBanner({ reason, issued }: { reason?: string; issued?: string }) {
  return (
    <div className="banner bad" role="alert">
      <strong>RECALL NOTICE - do not consume this product.</strong>
      {reason && <div>Reason: {reason}</div>}
      {issued && <div className="small">Issued {issued} UTC</div>}
    </div>
  );
}

function Batch({ batch, depth = 0 }: { batch: BatchView; depth?: number }) {
  if (batch.missing) return <p className="muted">Batch {batch.batch_id}: record not available.</p>;
  const lab = batch.lab;
  return (
    <div className={depth ? "nested" : ""}>
      <h3>{depth ? "Source batch" : "Batch"} {batch.batch_id}</h3>
      <p className="muted small">
        {batch.floral_type} - harvested {batch.harvest_date} - record{" "}
        {batch.ledger_record_intact ? "intact" : "FAILED its integrity check"}
      </p>
      <ol className="timeline">
        {batch.harvests?.map((h, i) => (
          <li key={`h${i}`}><b>Harvest</b> {h.harvest_date} - hive {h.hive}
            {h.producer && ` - ${h.producer}`}{h.floral_source && ` (${h.floral_source})`}</li>
        ))}
        {batch.processing?.map((p, i) => (
          <li key={`p${i}`}><b>Processing</b> {p.process} on {p.date}
            {p.processor && ` by ${p.processor}`}
            {p.input_kg != null && p.output_kg != null && ` (${p.input_kg} kg to ${p.output_kg} kg)`}</li>
        ))}
        {lab && (
          <li><b>Laboratory</b> {lab.status === "NOT_TESTED" ? "not tested - no passed test on record"
            : lab.status === "PENDING" ? "test pending - no result yet" : lab.status}
            {lab.pending_tests > 0 && lab.status !== "PENDING" && ` (${lab.pending_tests} further test pending)`}
            {lab.results.length > 0 && (
              <ul>{lab.results.map((r, i) => (
                <li key={i}>{r.verdict} on {r.test_date}{r.laboratory && ` by ${r.laboratory}`}
                  {r.moisture_pct != null && ` - moisture ${r.moisture_pct}%`}
                  {r.hmf_mg_kg != null && ` - HMF ${r.hmf_mg_kg} mg/kg`}
                  {r.c4_sugar_pct != null && ` - C4 sugar ${r.c4_sugar_pct}%`}
                  {r.certificate_reference && ` - ref ${r.certificate_reference}`}</li>))}</ul>)}
          </li>
        )}
        {batch.custody?.map((c, i) => (
          <li key={`c${i}`}><b>Handed over</b> {c.date}: {c.from ?? "?"} to {c.to ?? "?"}</li>
        ))}
        {batch.shipments?.map((s, i) => (
          <li key={`s${i}`}><b>Shipment</b> {s.from ?? "?"} to {s.to ?? "?"}: {s.status}</li>
        ))}
        {batch.retail?.map((r, i) => (
          <li key={`r${i}`}><b>Retail</b> {r.retailer ?? "?"}: {r.status}</li>
        ))}
      </ol>
      {batch.blended_from && batch.blended_from.length > 0 && (
        <div>
          <p><b>This honey is a blend of {batch.blended_from.length} source batches:</b></p>
          {batch.blended_from.map((b, i) => (
            <div key={i}><p className="small muted">{b.weight_kg} kg from:</p>
              <Batch batch={b.source} depth={depth + 1} /></div>
          ))}
        </div>
      )}
      {batch.blockchain && batch.blockchain.length > 0 && depth === 0 && (
        <details>
          <summary>Blockchain records ({batch.blockchain.filter((e) => e.anchored_on_chain).length} of{" "}
            {batch.blockchain.length} anchored)</summary>
          <ul className="small">{batch.blockchain.map((e, i) => (
            <li key={i}>{e.event}: {e.anchored_on_chain ? `anchored (tx ${e.tx_hash}, chain ${e.chain_id})`
              : "recorded, not yet anchored on chain"}</li>))}</ul>
        </details>
      )}
    </div>
  );
}

function Outcome({ data }: { data: VerifyResult }) {
  switch (data.result) {
    case "AUTHENTIC":
      return (
        <>
          {data.recall.active && <RecallBanner reason={data.recall.reason} issued={data.recall.issued_at} />}
          {data.scan_signal?.suspicious && (
            <div className="banner warn" role="alert">
              <strong>SUSPICIOUS ACTIVITY DETECTED</strong>
              {data.scan_signal.reasons.map((r, i) => <div key={i}>{r}</div>)}
              <div className="small">{data.scan_signal.note}</div>
            </div>
          )}
          <div className={`banner ${data.recall.active ? "bad" : "good"}`}>
            <strong>Authentic HoneyChain code</strong> - unit {data.unit.unit_id} ({data.unit.status})
          </div>
          <p className="muted small">{data.authenticity_note}</p>
          {data.warnings.length > 0 && (
            <ul className="warnings">{data.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>)}
          <section className="panel">
            <h2>This jar</h2>
            <p>{data.unit.size_g != null && `${data.unit.size_g} g - `}packed {data.unit.packed_at ?? "?"}
              {data.unit.packager && ` by ${data.unit.packager}`}
              {data.unit.best_before && ` - best before ${data.unit.best_before}`}</p>
            <p className="muted small">Verified {data.unit.times_verified} time(s)
              {data.unit.first_verified_at && `, first on ${data.unit.first_verified_at} UTC`}.</p>
          </section>
          <section className="panel"><h2>Journey</h2><Batch batch={data.provenance} /></section>
        </>
      );
    case "REVOKED":
      return (
        <>
          {data.recall?.active && <RecallBanner reason={data.recall.reason} issued={data.recall.issued_at} />}
          <div className="banner bad" role="alert"><strong>Revoked label - do not trust this product</strong>
            <div>{data.message}</div></div>
        </>
      );
    case "RATE_LIMITED":
      return <div className="banner warn" role="alert">{data.message}</div>;
    default:
      return (
        <div className="banner bad" role="alert">
          <strong>Not verified</strong>
          <div>{INVALID_REASONS[data.reason] ?? "This code could not be verified."}</div>
          <div className="small">{data.message}</div>
        </div>
      );
  }
}

export default function ConsumerVerify() {
  const [code, setCode] = useState(() => codeFromLocation(window.location.hash));
  const [input, setInput] = useState("");
  const [data, setData] = useState<VerifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!code) return;
    let alive = true;
    setLoading(true);
    setError(null);
    setData(null);
    verifyCode(code)
      .then((d) => alive && setData(d))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : "Verification failed"))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [code]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setCode(input.trim());
  };

  return (
    <main className="consumer">
      <h1>HoneyChain product verification</h1>
      {!code && (
        <form className="panel" onSubmit={submit} aria-label="Enter a product code">
          <label>Product code (from the label)
            <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="HC1.UNIT-..." required />
          </label>
          <button type="submit">Verify</button>
        </form>
      )}
      {loading && <p className="muted" role="status">Checking this product code...</p>}
      {error && <div className="banner bad" role="alert">Verification service unavailable - product not verified. ({error}){" "}
        <button className="link" onClick={() => { const c = code; setCode(""); window.setTimeout(() => setCode(c), 0); }}>Try again</button></div>}
      {data && <div className="verify-result" key={data.result}><Outcome data={data} /></div>}
    </main>
  );
}

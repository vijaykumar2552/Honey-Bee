// Public consumer verification (no login). Uses the EXISTING endpoint
// POST /api/public/verify; the QR payload travels in the request body, never
// in a URL, and no staff session token is ever attached.

export interface BatchView {
  batch_id: string;
  missing?: boolean;
  floral_type?: string;
  harvest_date?: string;
  ledger_record_intact?: boolean;
  harvests?: { hive: string; harvest_date: string; producer: string | null; floral_source: string | null }[];
  processing?: { process: string; date: string; input_kg: number | null; output_kg: number | null; processor: string | null }[];
  blended_from?: { weight_kg: number; source: BatchView }[];
  lab?: {
    status: string;
    pending_tests: number;
    results: {
      verdict: string; test_date: string; laboratory: string | null; moisture_pct: number | null;
      hmf_mg_kg: number | null; c4_sugar_pct: number | null; acidity_meq_kg: number | null;
      certificate_reference: string | null;
    }[];
  };
  custody?: { from: string | null; to: string | null; date: string }[];
  shipments?: { from: string | null; to: string | null; status: string; departed: string | null; arrived: string | null }[];
  retail?: { retailer: string | null; status: string }[];
  blockchain?: { event: string; anchored_on_chain: boolean; status: string; tx_hash: string | null; chain_id: number | null }[];
}

export interface Recall { active: boolean; reason?: string; issued_at?: string }

export type VerifyResult =
  | {
      result: "AUTHENTIC";
      authenticity_note: string;
      recall: Recall;
      warnings: string[];
      scan_signal?: { suspicious: boolean; reasons: string[]; note: string };
      unit: {
        unit_id: string; status: string; size_g: number | null; packed_at: string | null;
        best_before: string | null; packager: string | null; times_verified: number;
        first_verified_at: string | null;
      };
      provenance: BatchView;
    }
  | { result: "REVOKED"; unit_id: string; message: string; recall?: Recall }
  | { result: "INVALID"; reason: string; message: string }
  | { result: "RATE_LIMITED"; message: string };

export async function verifyCode(code: string): Promise<VerifyResult> {
  const response = await fetch("/api/public/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ qr: code }),
    credentials: "omit",
  });
  const body = (await response.json().catch(() => null)) as VerifyResult | null;
  if (body && typeof body === "object" && "result" in body) return body;
  throw new Error(`Verification service error (${response.status})`);
}

export function codeFromLocation(hash: string): string {
  if (!hash || hash.length < 2) return "";
  try {
    return decodeURIComponent(hash.slice(1)).trim();
  } catch {
    return "";
  }
}

export const INVALID_REASONS: Record<string, string> = {
  MALFORMED: "This is not a HoneyChain product code.",
  UNSUPPORTED_VERSION: "This code uses a format this service does not recognise.",
  NOT_RECOGNISED: "This code does not match any genuine HoneyChain product.",
};

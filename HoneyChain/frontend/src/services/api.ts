// Thin client for the real HoneyChain Flask API. No mock data anywhere.
// The bearer token lives in sessionStorage (cleared when the tab closes);
// React escapes all rendered text, and the backend enforces every permission.

const TOKEN_KEY = "honeychain.token";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...init, headers });
  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!response.ok) {
    const server =
      body && typeof body === "object" && "message" in body
        ? String((body as { message: unknown }).message)
        : "";
    if (response.status === 401) setToken(null);
    throw new ApiError(response.status, server || STATUS_MESSAGES[response.status] || `Request failed (${response.status})`);
  }
  return body as T;
}

const STATUS_MESSAGES: Record<number, string> = {
  400: "The request was not valid.",
  401: "Your session has expired. Please sign in again.",
  403: "Your role or organization is not allowed to do this.",
  404: "Not found.",
  409: "This conflicts with the current state (it may already have been done).",
  422: "Some fields are invalid.",
  429: "Too many requests. Please wait a moment and try again.",
  500: "The server hit an error. Please try again.",
  503: "The service is not configured or unavailable.",
};

/** User-facing text for any thrown error, including network failures. */
export function describeError(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof TypeError) return "Network error: the server could not be reached.";
  return e instanceof Error ? e.message : "Something went wrong.";
}

export const get = <T,>(path: string) => api<T>(path);
export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export interface User {
  user_id: string;
  name: string;
  email: string;
  role: string;
  organization_id: string | null;
}

export async function login(email: string, password: string): Promise<User> {
  const body = await api<{ token: string; user: User }>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  setToken(body.token);
  return body.user;
}

export async function logout(): Promise<void> {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    setToken(null);
  }
}

export const me = () => api<User>("/api/auth/me");

// ---- Authority oversight (Phase 10) -------------------------------------
export type Counts = Record<string, number>;

export interface Overview {
  region_filter: string | null;
  note: string;
  production: {
    active_beekeepers: number;
    active_hives: number;
    harvests: number;
    harvest_kg: number;
    by_region: { region: string; harvests: number; harvest_kg: number; hives: number }[];
  };
  supply_chain: {
    batches: number;
    processing_events: number;
    shipments_by_status: Counts;
    retail_by_status: Counts;
    units_by_status: Counts;
  };
  compliance: {
    lab_results_by_status: Counts;
    failed_lab_batches: { batch_id: string; test_date: string; laboratory: string | null }[];
    batches_with_passed_lab_pct: number | null;
    units_traceable_to_harvest_pct: number | null;
    units_total: number;
    active_recalls: number;
    shipment_exceptions: {
      batch_id: string; exception_reason: string | null; updated_at: string | null;
      sender: string | null; receiver: string | null;
    }[];
  };
}

export interface Signal {
  type: string;
  severity: "HIGH" | "MEDIUM" | "LOW";
  classification: "SIGNAL";
  unit_id: string;
  batch_id: string | null;
  occurrences: number;
  first_seen: string;
  last_seen: string;
  evidence: string;
}

export interface Signals {
  note: string;
  thresholds: { forged_tag_attempts: number; scan_velocity_24h: number };
  signals: Signal[];
  invalid_scans_without_a_real_unit: Counts;
  scans_total: number;
}

export interface Hotspots {
  regions: {
    region: string; hives: number; CRITICAL: number; WARNING: number; HEALTHY: number;
    NO_DATA: number; devices_offline: number;
  }[];
  harvest_geo_cells: { lat: number; lng: number; harvests: number; harvest_kg: number }[];
  geo_precision: string;
}

export interface RecallView {
  recall_id: string;
  root_batch_id: string;
  status: string;
  reason: string;
  issued_at: string;
  affected_batches: string[];
  units_by_status: Counts;
  retailers: string[];
  consumer_scans_since_recall: number;
}

const q = (region: string) => (region ? `?region=${encodeURIComponent(region)}` : "");
export const authorityOverview = (region = "") => api<Overview>(`/api/authority/overview${q(region)}`);
export const authorityHotspots = (region = "") => api<Hotspots>(`/api/authority/hotspots${q(region)}`);
export const authoritySignals = () => api<Signals>("/api/authority/signals");
export const authorityRecalls = () => api<RecallView[]>("/api/authority/recalls");

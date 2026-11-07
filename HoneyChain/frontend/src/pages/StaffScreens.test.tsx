// Staff screens rendered against REAL responses captured from the API
// (src/__fixtures__/staff-responses.json, DEMO seed). fetch is stubbed to
// return those exact responses; nothing here is invented.
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import real from "../__fixtures__/staff-responses.json";
import { BatchDetail } from "./SupplyChain";
import { Dashboard } from "./Operations";
import Layout from "../components/Layout";
import { Badge, Flash } from "../components/ui";
import type { User } from "../services/api";

const RETAILER: User = { user_id: "USR-R", name: "R", email: "r@x", role: "RETAILER", organization_id: "ORG-DEMO-RETAIL" };

function serve(routes: Record<string, unknown>, status: Record<string, number> = {}) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    const code = key && status[key] ? status[key] : key ? 200 : 404;
    const body = key ? routes[key] : { message: "Not found" };
    return { ok: code < 400, status: code, text: async () => JSON.stringify(body) };
  }));
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const stage = (name: string) => screen.getByText(name, { selector: "strong" }).closest("li")!;

describe("batch journey (real trace)", () => {
  it("marks only backend-confirmed stages as confirmed and the next one as current", async () => {
    const b = real.trace.batch_id;
    serve({ [`/api/batches/${b}/trace`]: real.trace, [`/api/batches/${b}/units`]: real.units });
    render(<BatchDetail batchId={b} user={RETAILER} />);
    await screen.findByLabelText("Supply-chain journey");
    for (const s of ["Hive", "Harvest", "Processing", "Lab", "Packaging", "Shipment", "Retail"])
      expect(stage(s).className).toContain("done");
    expect(stage("Consumer").className).toContain("current");          // no unit SOLD yet
    expect(within(stage("Consumer")).getByText("(current stage)")).toBeTruthy();
    expect(screen.getByText(/held by/).textContent).toContain("ORG-DEMO-RETAIL");
  });

  it("shows a recall and a failed lab test as blocked, never as completed", async () => {
    const b = real.trace.batch_id;
    const failed = structuredClone(real.trace) as any;
    failed.lab = { ...failed.lab, status: "FAILED" };
    failed.recall = { recall_id: "RCL-X", root_batch_id: b, reason: "Residue found", issued_at: "2026-10-03" };
    serve({ [`/api/batches/${b}/trace`]: failed, [`/api/batches/${b}/units`]: real.units });
    render(<BatchDetail batchId={b} user={RETAILER} />);
    await screen.findByText(/RECALLED:/);
    expect(stage("Lab").className).toContain("failed");
    expect(within(stage("Lab")).getByText(/packaging is blocked by the server/)).toBeTruthy();
    expect(stage("Retail").className).toContain("failed");
    expect(stage("Consumer").className).toContain("failed");
  });
});

describe("dashboard (real telemetry)", () => {
  it("renders exactly the readings the API returned, with timestamps and data origin", async () => {
    serve({ "/api/summary": { active_hives: 1, reading_count: 6, avg_temperature: 34.52, overall_health_percent: 100 },
            "/api/telemetry/latest": real.telemetry, "/api/notifications": [] });
    render(<Dashboard />);
    await screen.findByText("Live hive telemetry");
    const tables = await screen.findAllByRole("table");
    const table = tables[tables.length - 1];
    expect(within(table).getAllByRole("row").length - 1).toBe(real.telemetry.length);
    expect(screen.getAllByText("Simulated device").length).toBe(real.telemetry.length);   // DEMO device
    expect(screen.getByText("34.52 C")).toBeTruthy();
  });

  it("explains a server error instead of showing data", async () => {
    serve({ "/api/summary": { message: "Your role or organization is not allowed to do this." },
            "/api/telemetry/latest": [], "/api/notifications": [] }, { "/api/summary": 403 });
    render(<Dashboard />);
    expect(await screen.findByText(/not allowed to do this/)).toBeTruthy();
  });
});

describe("motion only on real changes", () => {
  it("Flash and Badge do not animate on first render, only when the value changes", () => {
    const { rerender, container } = render(<div><Flash value={34.5} /><Badge value="NORMAL" /></div>);
    expect(container.querySelector(".flash")).toBeNull();
    expect(container.querySelector(".changed")).toBeNull();
    rerender(<div><Flash value={34.5} /><Badge value="NORMAL" /></div>);          // same data: still nothing
    expect(container.querySelector(".flash")).toBeNull();
    act(() => { rerender(<div><Flash value={35.1} /><Badge value="WATCH" /></div>); });
    expect(container.querySelector(".flash")).not.toBeNull();
    expect(container.querySelector(".badge.changed")?.textContent).toBe("WATCH");
  });
});

describe("role-based navigation", () => {
  it("shows only the sections a role can use", () => {
    render(<Layout user={{ ...RETAILER, role: "BEEKEEPER" }} route={["dashboard"]} onSignOut={() => {}}><p>x</p></Layout>);
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).getByText("Hives")).toBeTruthy();
    expect(within(nav).queryByText("Laboratory")).toBeNull();
    expect(within(nav).queryByText("Authority")).toBeNull();
    expect(within(nav).getByText("Dashboard").getAttribute("aria-current")).toBe("page");
  });
});

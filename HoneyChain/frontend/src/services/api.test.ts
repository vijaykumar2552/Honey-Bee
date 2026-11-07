// Every HTTP failure class maps to a useful user-facing message.
import { afterEach, describe, expect, it, vi } from "vitest";
import { describeError, get } from "./api";

afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

function respond(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: status < 400, status, text: async () => (body === null ? "" : JSON.stringify(body)) }));
}

describe("API error messages", () => {
  for (const [status, text] of [
    [401, /session has expired/], [403, /not allowed/], [404, /Not found/], [409, /conflicts/],
    [422, /invalid/], [429, /Too many requests/], [500, /server hit an error/], [503, /not configured or unavailable/],
  ] as const) {
    it(`${status} without a server message gets a clear default`, async () => {
      respond(status, null);
      const err = await get("/api/x").catch((e) => e);
      expect(describeError(err)).toMatch(text);
    });
  }

  it("prefers the server's own message when present", async () => {
    respond(409, { message: "Batch is in pending custody transfer CUS-1" });
    expect(describeError(await get("/api/x").catch((e) => e))).toBe("Batch is in pending custody transfer CUS-1");
  });

  it("401 clears the stored session token", async () => {
    sessionStorage.setItem("honeychain.token", "t");
    respond(401, null);
    await get("/api/x").catch(() => undefined);
    expect(sessionStorage.getItem("honeychain.token")).toBeNull();
  });

  it("network failure is explained, never shown as data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    expect(describeError(await get("/api/x").catch((e) => e))).toMatch(/Network error/);
  });
});

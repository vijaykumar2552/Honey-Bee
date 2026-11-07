// Renders the public verification page against REAL responses captured from
// POST /api/public/verify (src/__fixtures__/verify-responses.json).
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import fixtures from "../__fixtures__/verify-responses.json";
import ConsumerVerify from "./ConsumerVerify";

const CODE = "HC1.UNIT-ABCDEFGHIJKLMNOP." + "a".repeat(43);

function renderWith(response: unknown, status = 200, hash = `#${CODE}`) {
  window.location.hash = hash;
  const fetchMock = vi.fn().mockResolvedValue({ status, json: () => Promise.resolve(response) });
  vi.stubGlobal("fetch", fetchMock);
  render(<ConsumerVerify />);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.location.hash = "";
  sessionStorage.clear();
});

describe("consumer verification page", () => {
  it("posts the code from the URL fragment to the existing API, without any staff token", async () => {
    sessionStorage.setItem("honeychain.token", "staff-token-must-not-leak");
    const fetchMock = renderWith(fixtures.authentic);
    await screen.findByText(/Authentic HoneyChain code/);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/public/verify");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ qr: CODE });
    expect(JSON.stringify(init.headers)).not.toContain("staff-token-must-not-leak");
    expect(init.credentials).toBe("omit");
  });

  it("valid: authentic, with the not-a-safety-claim note, lab result and journey", async () => {
    renderWith(fixtures.authentic);
    await screen.findByText(/Authentic HoneyChain code/);
    expect(screen.getByText(/does not by itself mean the product is safe/)).toBeTruthy();
    expect(screen.getByText(/PASSED on/)).toBeTruthy();
    expect(screen.getAllByText(/Handed over/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/RECALL NOTICE/)).toBeNull();
    expect(screen.queryByText(/SUSPICIOUS ACTIVITY DETECTED/)).toBeNull();
  });

  it("invalid: each reason is explained and no provenance is shown", async () => {
    for (const [key, text] of [
      ["invalid_not_recognised", /does not match any genuine HoneyChain product/],
      ["invalid_malformed", /not a HoneyChain product code/],
      ["invalid_unsupported_version", /format this service does not recognise/],
    ] as const) {
      renderWith(fixtures[key]);
      await screen.findByText(/Not verified/);
      expect(screen.getByText(text)).toBeTruthy();
      expect(screen.queryByText(/Journey/)).toBeNull();
      cleanup();
    }
  });

  it("revoked: rejected, and an active recall is still shown", async () => {
    renderWith(fixtures.revoked_with_recall);
    await screen.findByText(/Revoked label - do not trust this product/);
    expect(screen.getByText(/RECALL NOTICE/)).toBeTruthy();
    expect(screen.getByText(/Antibiotic residue found/)).toBeTruthy();
    expect(screen.queryByText(/Authentic HoneyChain code/)).toBeNull();
  });

  it("recalled: the code is authentic but the recall dominates", async () => {
    renderWith(fixtures.recalled);
    await screen.findByText(/RECALL NOTICE - do not consume this product/);
    expect(screen.getByText(/Reason: Antibiotic residue found/)).toBeTruthy();
    const banner = screen.getByText(/Authentic HoneyChain code/).closest(".banner");
    expect(banner?.className).toContain("bad");            // not shown as a green success
  });

  it("suspicious: risk signal shown with its reason, framed as not proof", async () => {
    renderWith(fixtures.suspicious);
    await screen.findByText("SUSPICIOUS ACTIVITY DETECTED");
    expect(screen.getAllByText(/verified 3 times in the last 24 hours/).length).toBeGreaterThan(0);
    expect(screen.getByText(/not proof of counterfeiting/)).toBeTruthy();
  });

  it("rate limited and service failure are never shown as verified", async () => {
    renderWith({ result: "RATE_LIMITED", message: "Too many verification requests; try again shortly." }, 429);
    await screen.findByText(/Too many verification requests/);
    cleanup();
    window.location.hash = `#${CODE}`;
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<ConsumerVerify />);
    await screen.findByText(/product not verified/);
    expect(screen.queryByText(/Authentic HoneyChain code/)).toBeNull();
  });

  it("without a code in the URL, offers manual entry and verifies it", async () => {
    const fetchMock = renderWith(fixtures.authentic, 200, "");
    expect(fetchMock).not.toHaveBeenCalled();
    const input = screen.getByPlaceholderText("HC1.UNIT-...") as HTMLInputElement;
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(input, { target: { value: `  ${CODE}  ` } });
    fireEvent.submit(input.closest("form")!);
    await screen.findByText(/Authentic HoneyChain code/);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ qr: CODE });
  });

  it("renders hostile strings from the response as text, never as markup", async () => {
    const hostile = structuredClone(fixtures.recalled);
    hostile.recall.reason = '<img src=x onerror="alert(1)">';
    renderWith(hostile);
    await screen.findByText(/RECALL NOTICE/);
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/onerror/)).toBeTruthy();
  });
});

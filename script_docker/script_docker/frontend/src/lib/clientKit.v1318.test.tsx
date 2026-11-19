import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const apiMock = vi.hoisted(() => ({ get: vi.fn(), download: vi.fn() }));
vi.mock("./api", () => ({ api: apiMock, resolveApiUrl: (p: string) => p }));

import { CLIENT_SETUP_FILENAME, downloadClientSetup, getClientKitInfo, resetClientKitCache } from "./clientKit";
import { clientSetupState, ClientSetupPanel } from "../components/forensic/ClientSetupPanel";
import { invalidateHelperHealthCache, listHostMobileDevices } from "./hostDriveHelper";
import { clientAgentStatus } from "./clientEvidenceAgent";
import { inferApiService } from "./apiRouting";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  invalidateHelperHealthCache();
  resetClientKitCache();
  apiMock.get.mockReset();
  apiMock.download.mockReset();
});

describe("client kit (V1.31.8)", () => {
  it("asks the server which helper version it hands out", async () => {
    apiMock.get.mockResolvedValue({ ok: true, helper_version: "5.63" });
    expect((await getClientKitInfo())?.helper_version).toBe("5.63");
    expect(apiMock.get).toHaveBeenCalledWith("/api/client-kit/info", undefined, { timeoutMs: 10_000 });
  });

  it("treats an older server (no client kit) as unknown", async () => {
    apiMock.get.mockRejectedValue(new Error("404"));
    expect(await getClientKitInfo()).toBeNull();
  });

  it("downloads the setup file for this console's address", async () => {
    apiMock.download.mockResolvedValue(undefined);
    await downloadClientSetup("https://forensic.example.in");
    expect(apiMock.download).toHaveBeenCalledWith(
      "/api/client-kit/setup.cmd",
      CLIENT_SETUP_FILENAME,
      { origin: "https://forensic.example.in" },
      60_000,
    );
  });

  it("routes client kit calls like the helper calls (the page's own API)", () => {
    expect(inferApiService("/api/client-kit/info")).toBe(inferApiService("/api/hostdrive/acquisition/devices"));
  });
});

describe("what this computer needs", () => {
  const base = { serverHelperVersion: "5.63", officeHost: false };
  it("outdated helper: update before collecting", () => {
    expect(clientSetupState({ ...base, localHelper: { version: "5.60", outdated: true }, helperOnline: true })).toBe("outdated");
  });
  it("no helper on a client computer: set it up", () => {
    expect(clientSetupState({ ...base, localHelper: null, helperOnline: false })).toBe("missing");
    expect(clientSetupState({ ...base, localHelper: null, helperOnline: false, officeHost: true })).toBeNull();
    expect(clientSetupState({ ...base, localHelper: null, helperOnline: null })).toBeNull();
  });
  it("working but older than the server: an update is available", () => {
    expect(clientSetupState({ ...base, localHelper: { version: "5.62", outdated: false }, helperOnline: true })).toBe(
      "update_available",
    );
    expect(clientSetupState({ ...base, localHelper: { version: "5.63", outdated: false }, helperOnline: true })).toBeNull();
    expect(
      clientSetupState({ ...base, serverHelperVersion: "", localHelper: { version: "5.62", outdated: false }, helperOnline: true }),
    ).toBeNull();
  });

  it("the panel explains the outdated helper and downloads the setup file", async () => {
    apiMock.download.mockResolvedValue(undefined);
    render(<ClientSetupPanel state="outdated" localVersion="5.60" serverVersion="5.63" />);
    expect(screen.getByTestId("client-setup-panel").textContent).toContain("5.60");
    expect(screen.getByTestId("client-setup-panel").textContent).toContain("not listed until it is updated");
    fireEvent.click(screen.getByRole("button", { name: /Update this computer/ }));
    await waitFor(() => expect(screen.getByTestId("client-setup-downloaded")).toBeTruthy());
    expect(apiMock.download).toHaveBeenCalledTimes(1);
  });

  it("the panel shows a failed download", async () => {
    apiMock.download.mockRejectedValue(new Error("Download failed (403)"));
    render(<ClientSetupPanel state="missing" />);
    fireEvent.click(screen.getByRole("button", { name: /Set up this computer/ }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("403"));
  });
});

describe("this computer's helper", () => {
  function stubFetch(health: Record<string, unknown>, extra?: (url: string) => unknown) {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(String(url));
        const u = String(url);
        if (u.includes(":9877/")) throw new TypeError("Failed to fetch");
        const body = u.endsWith("/health")
          ? health
          : (extra?.(u) ?? { ok: true, mobile_devices: [{ id: "x", name: "Apple iPhone", os_hint: "ios" }] });
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
    return calls;
  }

  beforeEach(() => {
    apiMock.get.mockResolvedValue(null);
  });

  it("does not list an outdated helper's phones (the unplugged iPhone case)", async () => {
    stubFetch({ ok: true, helper_version: "5.60", port: 9876, root: "C:\\Polaron" });
    const r = await listHostMobileDevices(2_000);
    expect(r?.mobile_devices ?? []).toEqual([]);
  });

  it("lists a current helper's phones", async () => {
    stubFetch({ ok: true, helper_version: "5.63", port: 9876, root: "C:\\Polaron" });
    const r = await listHostMobileDevices(2_000);
    expect((r?.mobile_devices ?? []).map((d) => d.name)).toEqual(["Apple iPhone"]);
  });

  it("does not call port 9877 when the helper says the agent is not running", async () => {
    const calls = stubFetch({ ok: true, helper_version: "5.63", port: 9876, root: "C:\\Polaron", client_agent_running: false });
    await listHostMobileDevices(2_000); // reads the helper's /health
    const status = await clientAgentStatus(1_000, { force: true });
    expect(status.state).toBe("offline");
    expect(calls.some((u) => u.includes(":9877/"))).toBe(false);
  });

  it("still asks the agent when the helper says it runs, or cannot say (older helper)", async () => {
    const calls = stubFetch({ ok: true, helper_version: "5.62", port: 9876, root: "C:\\Polaron" });
    await listHostMobileDevices(2_000);
    await clientAgentStatus(1_000, { force: true });
    expect(calls.some((u) => u.includes(":9877/health"))).toBe(true);
  });
});

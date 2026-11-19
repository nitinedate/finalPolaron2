import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./forensicApi", () => ({
  acquisitionApi: {
    clientImportCapabilities: vi.fn(),
    startClientImport: vi.fn(),
    getClientImport: vi.fn(),
    cancelClientImport: vi.fn(),
  },
}));

vi.mock("./hostDriveHelper", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./hostDriveHelper")>();
  return { ...actual, isBrowserOnOfficeHost: vi.fn(() => false) };
});

import { acquisitionApi } from "./forensicApi";
import { isBrowserOnOfficeHost } from "./hostDriveHelper";
import {
  ClientAgentRequiredError,
  clientRunSealed,
  copyClientRunToServer,
  deviceSite,
  findClientCaseDir,
  resolveSiteIdentity,
} from "./clientMobileImport";

const api = acquisitionApi as unknown as Record<string, ReturnType<typeof vi.fn>>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type FakeOptions = {
  agent?: "online" | "offline" | "unpaired" | "local-only";
  helperName?: string | null;
  dirs?: Record<string, Array<{ name: string; kind: "dir" | "file"; mtime_ms?: number }>>;
};

const SHARE = {
  ok: true, share_id: "ce00112233445566778899aa", token: "t".repeat(43), root: "C:\\evidence\\CASE-2026-001",
  hostname: "EXAM-PC-07", addresses: ["10.20.30.41"], port: 9877, lan_bound: true, file_count: 12, dir_count: 6,
  total_bytes: 4096, manifest_sha256: "a".repeat(64), truncated: false, skipped: 0, expires_utc: "", agent_version: "1.2.0",
  files_preview: [], relay_enabled: true, relay_supported: true,
};

function installFakes(opts: FakeOptions = {}) {
  const calls: Array<{ url: string; method: string; body?: Record<string, unknown> }> = [];
  const mode = opts.agent || "online";
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || "GET").toUpperCase();
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
    calls.push({ url, method, body });
    if (url.startsWith("http://127.0.0.1:9876")) {
      if (opts.helperName === null) throw new TypeError("Failed to fetch");
      return json({ ok: true, service: "host-drive-helper", helper_version: "5.58", computer_name: opts.helperName ?? "EXAM-PC-07" });
    }
    if (!/^http:\/\/(127\.0\.0\.1|localhost):9877/.test(url)) return json({ ok: false }, 404);
    if (mode === "offline") throw new TypeError("Failed to fetch");
    const path = url.replace(/^http:\/\/(127\.0\.0\.1|localhost):9877/, "");
    if (path === "/health") {
      return json({ ok: true, service: "polaron-client-evidence-agent", origin_allowed: mode !== "unpaired", lan_bound: true });
    }
    if (path === "/list-dir") {
      const target = String(body?.path || "");
      const entries = opts.dirs?.[target];
      if (!entries) return json({ ok: false, error: "Folder not found", code: "not_found" }, 404);
      return json({ ok: true, path: target, parent: null, entries, count: entries.length, truncated: false });
    }
    if (path === "/shares" && method === "POST") {
      return json(mode === "local-only" ? { ...SHARE, lan_bound: false, relay_enabled: false } : SHARE);
    }
    if (path.startsWith("/shares/") && method === "DELETE") return json({ ok: true });
    return json({ ok: false }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const CASE_DIRS = {
  "C:\\evidence": [
    { name: "CASE-2026-001", kind: "dir" as const },
    { name: "CASE-2026-002", kind: "dir" as const },
  ],
  "C:\\evidence\\CASE-2026-001\\07_Logs": [
    { name: "CASE-2026-001_E01_A_FFS_20261115_083000", kind: "dir" as const, mtime_ms: 2000 },
    { name: "CASE-2026-001_E01_A_LOGICAL_20261101_101500", kind: "dir" as const, mtime_ms: 1000 },
  ],
  "C:\\evidence\\CASE-2026-001\\07_Logs\\CASE-2026-001_E01_A_FFS_20261115_083000": [
    { name: "collection.log", kind: "file" as const },
  ],
  "C:\\evidence\\CASE-2026-001\\07_Logs\\CASE-2026-001_E01_A_LOGICAL_20261101_101500": [
    { name: "collection_summary.json", kind: "file" as const },
  ],
};

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  (isBrowserOnOfficeHost as unknown as ReturnType<typeof vi.fn>).mockReturnValue(false);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("where the phone is plugged in", () => {
  it("routes server-attached phones to the server and this-PC phones to the client unless this PC is the server", () => {
    expect(deviceSite({ attach_host: "office_server" }, false)).toBe("server");
    expect(deviceSite({ attach_host: "office_server" }, null)).toBe("server");
    expect(deviceSite({ attach_host: "this_pc" }, false)).toBe("client");
    expect(deviceSite({ attach_host: "this_pc" }, null)).toBe("client");
    expect(deviceSite({ attach_host: "this_pc" }, true)).toBe("server");
    expect(deviceSite({}, false)).toBe("client");
    expect(deviceSite({}, null)).toBe("server");
    expect(deviceSite(null, false)).toBe("server");
  });

  it("compares this PC's helper with the forensic server's helper by computer name", async () => {
    installFakes({ helperName: "EXAM-PC-07" });
    api.clientImportCapabilities.mockResolvedValue({ office_helper: { computer_name: "FORENSIC-SRV" }, default_client_case_root: "C:\\evidence" });
    expect((await resolveSiteIdentity({ force: true })).localIsServer).toBe(false);

    installFakes({ helperName: "forensic-srv" });
    expect((await resolveSiteIdentity({ force: true })).localIsServer).toBe(true);

    installFakes({ helperName: null });
    const unknown = await resolveSiteIdentity({ force: true });
    expect(unknown.localIsServer).toBeNull();
    expect(unknown.serverComputer).toBe("FORENSIC-SRV");

    (isBrowserOnOfficeHost as unknown as ReturnType<typeof vi.fn>).mockReturnValue(true);
    expect((await resolveSiteIdentity({ force: true })).localIsServer).toBe(true);
  });
});

describe("finding the sealed run on this computer", () => {
  it("matches the case folder however the collector spelled the case ID", async () => {
    installFakes({ dirs: CASE_DIRS });
    expect(await findClientCaseDir("C:\\evidence", "case 2026 001")).toBe("C:\\evidence\\CASE-2026-001");
    expect(await findClientCaseDir("C:\\evidence", "CASE-2026-009")).toBeNull();
    expect(await findClientCaseDir("D:\\missing", "CASE-2026-001")).toBeNull();
  });

  it("waits for the newest run's collection_summary.json and never falls back to an older run", async () => {
    installFakes({ dirs: CASE_DIRS });
    const caseDir = "C:\\evidence\\CASE-2026-001";
    expect(await clientRunSealed(caseDir)).toEqual({ sealed: false, runName: "CASE-2026-001_E01_A_FFS_20261115_083000" });
    expect(await clientRunSealed(caseDir, "CASE-2026-001_E01_A_LOGICAL_20261101_101500")).toEqual({
      sealed: true,
      runName: "CASE-2026-001_E01_A_LOGICAL_20261101_101500",
    });
    expect(await clientRunSealed("C:\\evidence\\CASE-2026-002")).toEqual({ sealed: false });
  });
});

describe("copying the client image to the forensic server", () => {
  it("shares the case folder read-only, starts the server copy and follows it to the server-path result", async () => {
    const calls = installFakes();
    api.startClientImport.mockResolvedValue({ import_id: "f".repeat(20), status: "queued", detail: "queued" });
    api.getClientImport
      .mockResolvedValueOnce({ import_id: "f".repeat(20), status: "running", bytes_done: 10, total_bytes: 40 })
      .mockResolvedValueOnce({
        import_id: "f".repeat(20), status: "completed", run_name: "R",
        result: { ok: true, paths: { exports: "D:\\Polaron\\evidence\\cases\\CASE-2026-001\\05_Exports\\R" } },
      });
    const seen: string[] = [];
    const state = await copyClientRunToServer({
      caseDir: "C:\\evidence\\CASE-2026-001",
      caseId: "CASE-2026-001",
      runName: "R",
      scope: "packages",
      onProgress: (s) => seen.push(s.status),
    });
    expect(state.status).toBe("completed");
    expect(seen).toEqual(["queued", "running", "completed"]);

    const share = calls.find((c) => c.url.endsWith("/shares") && c.method === "POST");
    expect(share?.body?.path).toBe("C:\\evidence\\CASE-2026-001");
    expect(String(share?.body?.relay_url)).toMatch(/\/api\/client-agent\/relay$/);
    const payload = api.startClientImport.mock.calls[0][0];
    expect(payload).toMatchObject({
      share_id: SHARE.share_id, token: SHARE.token, client_root: SHARE.root, relay: true,
      case_id: "CASE-2026-001", run_name: "R", scope: "packages", manifest_sha256: SHARE.manifest_sha256,
    });
    await vi.waitFor(() =>
      expect(calls.some((c) => c.method === "DELETE" && c.url.endsWith(`/shares/${SHARE.share_id}`))).toBe(true),
    );
  }, 15_000);

  it("withdraws the share on this computer when the server refuses the copy", async () => {
    const calls = installFakes();
    api.startClientImport.mockRejectedValue(new Error("The HostDrive helper on the forensic server is older than this release"));
    await expect(
      copyClientRunToServer({ caseDir: "C:\\evidence\\CASE-2026-001", caseId: "CASE-2026-001" }),
    ).rejects.toThrow(/older than this release/);
    expect(calls.some((c) => c.method === "DELETE" && c.url.endsWith(`/shares/${SHARE.share_id}`))).toBe(true);
  });

  it("explains what to install when the Client Evidence Agent is missing, unpaired or local-only", async () => {
    installFakes({ agent: "offline" });
    await expect(copyClientRunToServer({ caseDir: "C:\\evidence\\X", caseId: "X" })).rejects.toBeInstanceOf(ClientAgentRequiredError);
    installFakes({ agent: "unpaired" });
    await expect(copyClientRunToServer({ caseDir: "C:\\evidence\\X", caseId: "X" })).rejects.toThrow(/not paired/);
    const calls = installFakes({ agent: "local-only" });
    await expect(copyClientRunToServer({ caseDir: "C:\\evidence\\X", caseId: "X" })).rejects.toThrow(/listening on this computer only/);
    expect(calls.some((c) => c.method === "DELETE")).toBe(true);
    expect(api.startClientImport).not.toHaveBeenCalled();
  });
});

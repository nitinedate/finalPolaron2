import { afterEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), download: vi.fn() }));
vi.mock("./api", () => ({ api: apiMock, resolveApiUrl: (p: string) => p }));

import {
  createClientStream,
  followClientStream,
  streamProgressLine,
  streamStartFields,
} from "./clientStream";
import { getHostStreamStatus, invalidateHelperHealthCache, startHostAcquisitionRun } from "./hostDriveHelper";
import type { ClientImportState } from "./types/forensic";

const SESSION = "0123456789abcdef0123";
const JOB = "ab".repeat(16);

function state(overrides: Partial<ClientImportState> = {}): ClientImportState {
  return {
    import_id: SESSION,
    status: "running",
    stage: "receiving",
    detail: "",
    scope: "full",
    case_id: "CASE-7",
    case_folder: "CASE-7",
    server_case_folder: "CASE-7",
    run_name: "CASE-7_E01_X_LOGICAL_20261118_101500",
    client_host: "EXAMINER-PC-07",
    client_case_dir: "",
    transport: "client_stream",
    total_files: 10,
    total_bytes: 1000,
    files_done: 4,
    bytes_done: 400,
    files_present: 0,
    progress_pct: 40,
    server_case_dir: "D:\\Polaron\\evidence\\cases\\CASE-7",
    started_utc: "",
    ended_utc: null,
    warnings: [],
    error: null,
    result: null,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  invalidateHelperHealthCache();
  apiMock.get.mockReset();
  apiMock.post.mockReset();
});

describe("client collection streamed to the server (V1.31.14)", () => {
  it("hands the helper the session and token, never a server address", () => {
    const fields = streamStartFields({ session_id: SESSION, token: "tok".repeat(15), service: "mobile-ios" }, false);
    expect(fields).toEqual({
      stream_session: SESSION,
      stream_token: "tok".repeat(15),
      stream_service: "mobile-ios",
      stream_keep_local: "0",
      stream_scope: "full",
    });
    expect(Object.keys(fields).some((k) => /server|url|origin/i.test(k))).toBe(false);
    // "Packages only" sends part of the run: the local copy is always kept.
    expect(streamStartFields({ session_id: SESSION, token: "t", service: "" }, false, "packages").stream_keep_local).toBe("1");
  });

  it("opens the upload session on the server", async () => {
    apiMock.post.mockResolvedValue({ session_id: SESSION, token: "x".repeat(43), chunk_bytes: 8 << 20, service: "forensic", state: state() });
    const out = await createClientStream({ caseId: "CASE-7", scope: "full", keepLocal: false, clientHost: "PC-7" });
    expect(out.session?.session_id).toBe(SESSION);
    expect(apiMock.post).toHaveBeenCalledWith(
      "/api/acquisition/client-stream",
      { case_id: "CASE-7", scope: "full", keep_local: false, client_host: "PC-7" },
      { timeoutMs: 60_000 },
    );
    apiMock.post.mockRejectedValue(new Error("The HostDrive helper on the forensic server is not running"));
    const failed = await createClientStream({ caseId: "CASE-7", scope: "full", keepLocal: false });
    expect(failed.session).toBeNull();
    expect(failed.error).toMatch(/not running/);
  });

  it("falls back cleanly when the helper refuses the session (not set up)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).endsWith("/health")) return jsonResponse({ ok: true, helper_version: "5.66" });
        return jsonResponse(
          { ok: false, error: "stream_not_configured", detail: "This computer was not set up with the Polaron setup file" },
          409,
        );
      }),
    );
    const res = await startHostAcquisitionRun({ adapter: "ios_lockdown", stream_session: SESSION });
    expect(res?.ok).toBe(false);
    expect(res?.stream_refused).toBe("stream_not_configured");
    expect(res?.busy).toBeUndefined();
    expect(res?.error).toMatch(/setup file/);
  });

  it("follows the server until the run is registered", async () => {
    apiMock.get
      .mockResolvedValueOnce(state({ status: "waiting" as ClientImportState["status"] }))
      .mockResolvedValueOnce(state({ files_done: 9 }))
      .mockResolvedValueOnce(state({ status: "completed", files_done: 10, bytes_done: 1000 }));
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true, state: "sending", files_sent: 9 })));
    const seen: number[] = [];
    const done = await followClientStream({ sessionId: SESSION, jobId: JOB, pollMs: 1, onProgress: (s) => seen.push(s.files_done) });
    expect(done.status).toBe("completed");
    expect(seen).toEqual([4, 9, 10]);
    expect(apiMock.get).toHaveBeenCalledWith(`/api/acquisition/client-import/${SESSION}`, undefined, { timeoutMs: 20_000 });
  });

  it("stops waiting when this computer's uploader gave up (the image stays local)", async () => {
    apiMock.get.mockResolvedValue(state());
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ ok: false, state: "kept_local", detail: "The server no longer accepts this upload.", error: "unknown_session" }),
      ),
    );
    const done = await followClientStream({ sessionId: SESSION, jobId: JOB, pollMs: 1 });
    expect(done.status).toBe("failed");
    expect(done.error).toBe("unknown_session");
  });

  it("reads the uploader status from this computer's helper only for a real job id", async () => {
    const fetchMock = vi.fn(async (_url: string) => jsonResponse({ ok: true, state: "sending" }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await getHostStreamStatus("../../etc")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect((await getHostStreamStatus(JOB))?.state).toBe("sending");
    expect(String(fetchMock.mock.calls[0][0])).toMatch(new RegExp(`/acquisition/stream/${JOB}$`));
  });

  it("describes the progress in one line", () => {
    expect(streamProgressLine(state({ files_done: 0, bytes_done: 0 }))).toMatch(/as soon as the first files/);
    expect(streamProgressLine(state({ files_done: 1234, bytes_done: 3 * 1024 ** 3 }))).toBe(
      "Sent to the forensic server and verified there: 1,234 file(s), 3.00 GB.",
    );
    expect(streamProgressLine(state(), { state: "kept_local", detail: "Kept on this computer." })).toBe("Kept on this computer.");
  });
});

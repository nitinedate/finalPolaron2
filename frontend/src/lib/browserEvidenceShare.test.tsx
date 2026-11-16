// @vitest-environment node
// Node 22 has the same File, Blob, WebCrypto, CompressionStream and fetch APIs the browser uses.
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildBrowserManifest,
  createBrowserShare,
  encodeForWire,
  folderFromInputFiles,
  isAllZero,
  markBrowserShareRegistered,
  reconnectBrowserShare,
  sha256Fallback,
  sha256Hex,
  stopBrowserShare,
  storedBrowserShare,
  type BrowserPickedFolder,
} from "./browserEvidenceShare";

const store = new Map<string, string>();
(globalThis as unknown as { window: unknown }).window = {
  sessionStorage: {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => store.set(k, String(v)),
    removeItem: (k: string) => store.delete(k),
  },
  addEventListener() {},
};

async function inflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

function same(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** Random bytes in 64 KiB steps (getRandomValues refuses larger requests). */
function randomBytes(n: number): Uint8Array {
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i += 65_536) crypto.getRandomValues(out.subarray(i, Math.min(n, i + 65_536)));
  return out;
}

function file(path: string, data: Uint8Array | string, lastModified: number): File {
  const f = new File([data as BlobPart], path.split("/").pop() as string, { lastModified });
  Object.defineProperty(f, "webkitRelativePath", { value: `Case-0423/${path}` });
  return f;
}

function folder(files: Array<[string, Uint8Array | string, number]>): BrowserPickedFolder {
  return folderFromInputFiles(files.map(([p, d, m]) => file(p, d, m))) as BrowserPickedFolder;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("frozen file list (same as the Client Evidence Agent)", () => {
  it("matches the server's manifest digest for the same files", async () => {
    // Expected value computed by backend app.services.client_evidence.agent_client.manifest_digest.
    const f = folder([
      ["a/b.E01", new Uint8Array(10), 1_700_000_000_000],
      ["A.txt", "12345", 1],
      ["sub/Café.txt", "abc", 1_731_234_567_890],
    ]);
    const manifest = await buildBrowserManifest(f);
    expect(manifest.files.map((x) => x.path)).toEqual(["A.txt", "a/b.E01", "sub/Café.txt"]);
    expect(manifest.digest).toBe("86183fbb7782f2b722d13ea5fafe834bc67cb64beafd5af84cb6b1ebb2b23626");
    expect(manifest.totalBytes).toBe(18);
    expect(manifest.dirs.sort()).toEqual(["a", "sub"]);
  });

  it("uses the same SHA-256 when WebCrypto is unavailable (console over http://)", async () => {
    for (const n of [0, 1, 55, 56, 63, 64, 65, 1000, 70_000]) {
      const text = "x".repeat(n);
      const expected = await sha256Hex(text);
      const got = Array.from(sha256Fallback(new TextEncoder().encode(text)), (b) => b.toString(16).padStart(2, "0")).join("");
      expect(got).toBe(expected);
    }
  });

  it("strips the chosen folder name and leaves out the folders the agent skips", () => {
    const f = folder([
      ["laptop.E01", "x", 1],
      ["$RECYCLE.BIN/deleted.E01", "x", 1],
      ["Case/System Volume Information/tracking.log", "x", 1],
      ["Case/notes.txt", "x", 1],
    ]);
    expect(f.name).toBe("Case-0423");
    expect(f.files.map((x) => x.path).sort()).toEqual(["Case/notes.txt", "laptop.E01"]);
    expect(f.dirs).toEqual(["Case"]);
  });
});

describe("wire encoding (agent 1.2 compatible)", () => {
  it("sends empty space as a zero marker and compresses text as raw DEFLATE", async () => {
    expect(isAllZero(new Uint8Array(4099))).toBe(true);
    expect(isAllZero(Uint8Array.from([0, 0, 0, 0, 1]))).toBe(false);
    const zero = await encodeForWire(new Uint8Array(128 * 1024), "zero,deflate");
    expect(zero.encoding).toBe("zero");
    expect(zero.body.length).toBe(0);
    const text = new TextEncoder().encode("2026-11-16 INFO service started\n".repeat(5000));
    const packed = await encodeForWire(text, "zero,deflate");
    expect(packed.encoding).toBe("deflate");
    expect(packed.body.length).toBeLessThan(text.length / 10);
    expect(same(await inflateRaw(packed.body), text)).toBe(true);
    const random = randomBytes(65_536 * 2);
    expect((await encodeForWire(random, "zero,deflate")).encoding).toBeNull();
    expect((await encodeForWire(text, "")).encoding).toBeNull();
  });
});

/* --------------------------------------------------------------------- */
/* Scripted relay server                                                   */
/* --------------------------------------------------------------------- */

type Answer = { url: string; headers: Record<string, string>; body: Uint8Array };

function installRelay(messages: Array<Record<string, unknown> | number>) {
  const answers: Answer[] = [];
  const acks: string[] = [];
  const polls: Array<Record<string, string>> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const headers = init?.headers as Record<string, string>;
    if (url.endsWith("/poll")) {
      polls.push(headers);
      const next = messages.shift();
      if (next === undefined) {
        // Nothing queued: hold the poll open until the share stops.
        return new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        });
      }
      if (typeof next === "number") return new Response(null, { status: next });
      return new Response(JSON.stringify(next), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (url.includes("/ack/")) {
      acks.push(url.split("/ack/")[1]);
      return new Response('{"ok":true}');
    }
    if (url.includes("/respond/")) {
      answers.push({ url, headers, body: new Uint8Array(init?.body as ArrayBuffer) });
      return new Response('{"ok":true}');
    }
    return new Response("{}", { status: 404 });
  }));
  return { answers, acks, polls };
}

async function until(fn: () => boolean, ms = 4000) {
  const end = Date.now() + ms;
  while (!fn()) {
    if (Date.now() > end) throw new Error("timed out");
    await new Promise((r) => setTimeout(r, 10));
  }
}

describe("serving a folder from this tab", () => {
  it("answers manifest and range requests exactly like the agent, and stops when the server revokes", async () => {
    const e01 = randomBytes(300_000);
    const f = folder([
      ["laptop.E01", e01, 1_731_000_000_123],
      ["empty-space.dd", new Uint8Array(200_000), 1_731_000_000_456],
    ]);
    const relay = installRelay([
      404, // not registered yet: the tab keeps polling
      { req_id: "r1", op: "manifest" },
      { req_id: "r2", op: "range", index: 1, start: 1000, end: 70_999, accept: "zero" },
      { req_id: "r3", op: "range", index: 0, start: 199_990, end: 400_000, accept: "" },
      { req_id: "r3b", op: "range", index: 0, start: 250_000, end: 260_000, accept: "zero" },
      { req_id: "r4", op: "range", index: 5, start: 0, end: 10 },
      { req_id: "r5", op: "revoke" },
    ]);
    const share = await createBrowserShare({ folder: f, jobId: "job-1", relay: { url: "https://polaron.example/api/client-agent/relay", service: "mobile-android" } });
    expect(share.share_id).toMatch(/^ce[0-9a-f]{24}$/);
    expect(share.token.length).toBeGreaterThanOrEqual(43);
    expect(share.relay_enabled && share.relay_supported).toBe(true);
    await until(() => relay.answers.length === 6);
    expect(relay.polls[0]).toMatchObject({ Authorization: `Bearer ${share.token}`, "X-Polaron-Share": share.share_id, "X-Aetheris-Service": "mobile-android" });
    expect(relay.acks).toEqual(["r1", "r2", "r3", "r3b", "r4", "r5"]);

    const byId = Object.fromEntries(relay.answers.map((a) => [a.url.split("/respond/")[1], a]));
    const manifest = JSON.parse(new TextDecoder().decode(byId.r1.body));
    expect(manifest).toMatchObject({ ok: true, share_id: share.share_id, root: "Case-0423", file_count: 2, manifest_sha256: share.manifest_sha256 });
    expect(manifest.files.map((x: { path: string }) => x.path)).toEqual(["empty-space.dd", "laptop.E01"]);

    // Index 1 in manifest order is laptop.E01 (ordinal sort): the empty file is index 0.
    expect(byId.r2.headers["X-Relay-Status"]).toBe("206");
    expect(byId.r2.headers["X-Relay-ETag"]).toBe('"300000-1731000000123"');
    expect(byId.r2.headers["X-Relay-Content-Range"]).toBe("bytes 1000-70999/300000");
    expect(same(byId.r2.body, e01.subarray(1000, 71_000))).toBe(true);

    // A range running past the end is clipped to the file, like the agent; a range starting past it is refused.
    expect(byId.r3.headers["X-Relay-Status"]).toBe("206");
    expect(byId.r3.headers["X-Relay-Content-Range"]).toBe("bytes 199990-199999/200000");
    expect(byId.r3.headers["X-Relay-Encoding"]).toBeUndefined();
    expect(Array.from(byId.r3.body)).toEqual(new Array(10).fill(0));
    expect(byId.r3b.headers["X-Relay-Status"]).toBe("416");
    expect(byId.r4.headers["X-Relay-Status"]).toBe("404");

    await until(() => storedBrowserShare(share.share_id) === null && relay.answers.length === 6);
  });

  it("refuses a file that changed after it was chosen (409: processing stops)", async () => {
    const f = folder([["laptop.E01", randomBytes(1000), 1_731_000_000_000]]);
    const changed = f.files[0].file;
    // What the browser does when the file on disk changed: reading the old File fails.
    changed.slice = () => ({ arrayBuffer: () => Promise.reject(new DOMException("changed", "NotReadableError")) }) as unknown as Blob;
    const relay = installRelay([{ req_id: "r1", op: "range", index: 0, start: 0, end: 99, accept: "zero" }]);
    const share = await createBrowserShare({ folder: f, jobId: "job-2", relay: { url: "https://polaron.example/api/client-agent/relay", service: "forensic" } });
    await until(() => relay.answers.length === 1);
    expect(relay.answers[0].headers["X-Relay-Status"]).toBe("409");
    expect(relay.answers[0].headers["X-Relay-Error"]).toBe("evidence_modified");
    stopBrowserShare(share.share_id);
  });

  it("reconnects after a reload only with the same, unchanged folder", async () => {
    installRelay([]);
    const files: Array<[string, Uint8Array | string, number]> = [["laptop.E01", "abcdef", 1_731_000_000_000]];
    const share = await createBrowserShare({ folder: folder(files), jobId: "job-3", relay: { url: "https://polaron.example/api/client-agent/relay", service: "forensic" } });
    expect(storedBrowserShare(share.share_id)).toBeNull(); // offered only once the server accepted it
    markBrowserShareRegistered(share.share_id);
    expect(storedBrowserShare(share.share_id)).toMatchObject({ name: "Case-0423", jobId: "job-3", fileCount: 1 });
    await expect(reconnectBrowserShare(share.share_id, folder([["laptop.E01", "abcdeX!", 1_731_000_000_000]]))).rejects.toThrow(/does not match/);
    const stats = await reconnectBrowserShare(share.share_id, folder(files));
    expect(stats.shareId).toBe(share.share_id);
    stopBrowserShare(share.share_id);
    expect(storedBrowserShare(share.share_id)).toBeNull();
  });
});

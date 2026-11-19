/**
 * "This computer" evidence served by the browser tab itself - no agent to install.
 *
 * The examiner picks a folder in the computer's own folder window (every drive: C:, D:, USB,
 * mapped network drives). The tab then plays the part of the Polaron Client Evidence Agent's
 * outbound connection (V1.22): it long-polls the console at /api/client-agent/relay, and when
 * the forensic server needs bytes it reads exactly that range of the selected file and posts it
 * back. Extraction, parsing and RAG run on the server unchanged; the server stores no copy of
 * the image.
 *
 * Forensic handling matches the agent:
 *  - The file list is frozen when the folder is chosen (relative path, size, modified time) and
 *    its SHA-256 digest is computed exactly like the agent's, so the server verifies it.
 *  - Every range answer carries the ETag "<size>-<mtime_ms>". A file that changed after it was
 *    chosen cannot be read any more (the browser refuses) and is answered with 409, which stops
 *    processing for evidence integrity.
 *  - The share token is random (256 bit) and kept in this tab only (sessionStorage for
 *    "Reconnect" after a reload). It authorizes nothing but answering for this one share.
 */

import { type ClientRelayTarget, type ClientShare } from "./clientEvidenceAgent";

export const BROWSER_AGENT_VERSION = "browser-1.0.0";
/** Folders the agent leaves out too. */
const SKIP_DIRS = new Set(["$recycle.bin", "system volume information", "config.msi", "recovery"]);
/** Same caps as the agent. */
export const MAX_BROWSER_FILES = 250_000;
const MAX_RELAY_CHUNK = 64 * 1024 * 1024;
/** Long polls kept open. Small enough not to starve the console's own requests over HTTP/1.1. */
const POLLERS = 3;
const STORAGE_KEY = "polaron.browserEvidenceShares.v1";

export type BrowserPickedFile = { path: string; file: File };

export type BrowserPickedFolder = {
  /** Name of the chosen folder (browsers do not reveal its full path). */
  name: string;
  files: BrowserPickedFile[];
  dirs: string[];
  truncated: boolean;
  skipped: number;
  /** How the folder was chosen. */
  via: "directory-picker" | "input" | "files";
};

export type BrowserManifestFile = { path: string; size: number; mtime_ms: number };

export type BrowserManifest = {
  files: BrowserManifestFile[];
  dirs: string[];
  totalBytes: number;
  digest: string;
};

export type BrowserShareState = "connecting" | "serving" | "waiting" | "rejected" | "stopped";

export type BrowserShareStats = {
  shareId: string;
  jobId: string;
  name: string;
  fileCount: number;
  totalBytes: number;
  /** Bytes the server asked for and this tab read from disk. */
  logicalBytes: number;
  /** Bytes that actually went over the network (empty space skipped, data compressed). */
  wireBytes: number;
  requests: number;
  state: BrowserShareState;
  lastContactMs: number;
  error?: string;
};

type StoredShare = {
  shareId: string;
  token: string;
  jobId: string;
  name: string;
  digest: string;
  fileCount: number;
  totalBytes: number;
  relayUrl: string;
  service: string;
  registered: boolean;
  createdAt: number;
};

/* ------------------------------------------------------------------------ */
/* Choosing a folder                                                        */
/* ------------------------------------------------------------------------ */

type DirHandle = {
  name: string;
  kind: "file" | "directory";
  getFile(): Promise<File>;
  values(): AsyncIterable<DirHandle>;
};

type PickerWindow = Window & {
  showDirectoryPicker?: (opts?: { id?: string; mode?: "read" | "readwrite" }) => Promise<DirHandle>;
};

export type BrowserPickerSupport = "directory-picker" | "input" | null;

/** What this browser can use to choose a folder (Chrome/Edge: folder window; Firefox/Safari: folder input). */
export function browserFolderPickerSupport(): BrowserPickerSupport {
  if (typeof window === "undefined" || typeof document === "undefined") return null;
  if (typeof (window as PickerWindow).showDirectoryPicker === "function") return "directory-picker";
  const input = document.createElement("input");
  input.type = "file";
  return "webkitdirectory" in input ? "input" : null;
}

function skipPath(rel: string): boolean {
  return rel.split("/").slice(0, -1).some((part) => SKIP_DIRS.has(part.toLowerCase()));
}

function safeRelative(rel: string): boolean {
  return Boolean(rel) && !rel.startsWith("/") && !rel.split("/").some((part) => part === "" || part === "." || part === "..");
}

async function walkDirectory(root: DirHandle, onProgress?: (files: number) => void): Promise<BrowserPickedFolder> {
  const files: BrowserPickedFile[] = [];
  const dirs: string[] = [];
  let truncated = false;
  let skipped = 0;
  const stack: Array<{ dir: DirHandle; rel: string }> = [{ dir: root, rel: "" }];
  while (stack.length && !truncated) {
    const { dir, rel } = stack.pop() as { dir: DirHandle; rel: string };
    try {
      for await (const entry of dir.values()) {
        if (files.length >= MAX_BROWSER_FILES) {
          truncated = true;
          break;
        }
        const path = rel ? `${rel}/${entry.name}` : entry.name;
        if (entry.kind === "directory") {
          if (SKIP_DIRS.has(entry.name.toLowerCase())) continue;
          dirs.push(path);
          stack.push({ dir: entry, rel: path });
          continue;
        }
        try {
          files.push({ path, file: await entry.getFile() });
          if (onProgress && files.length % 500 === 0) onProgress(files.length);
        } catch {
          skipped += 1;
        }
      }
    } catch {
      skipped += 1;
    }
  }
  return { name: root.name, files, dirs, truncated, skipped, via: "directory-picker" };
}

function pickWithInput(): Promise<File[] | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.setAttribute("webkitdirectory", "");
    input.setAttribute("directory", "");
    input.style.display = "none";
    let done = false;
    const finish = (value: File[] | null) => {
      if (done) return;
      done = true;
      input.remove();
      resolve(value);
    };
    input.addEventListener("change", () => finish(input.files ? Array.from(input.files) : null));
    input.addEventListener("cancel", () => finish(null));
    document.body.appendChild(input);
    input.click();
  });
}

/** Folder chosen through a folder <input>: names come from webkitRelativePath ("Folder/sub/file"). */
export function folderFromInputFiles(list: File[]): BrowserPickedFolder | null {
  if (!list.length) return null;
  const first = (list[0] as File & { webkitRelativePath?: string }).webkitRelativePath || list[0].name;
  const name = first.split("/")[0] || "Selected folder";
  const files: BrowserPickedFile[] = [];
  const dirSet = new Set<string>();
  let truncated = false;
  for (const file of list) {
    const full = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
    const rel = full.includes("/") ? full.slice(full.indexOf("/") + 1) : full;
    if (!safeRelative(rel) || skipPath(rel)) continue;
    if (files.length >= MAX_BROWSER_FILES) {
      truncated = true;
      break;
    }
    files.push({ path: rel, file });
    const parts = rel.split("/");
    for (let i = 1; i < parts.length; i += 1) dirSet.add(parts.slice(0, i).join("/"));
  }
  return { name, files, dirs: Array.from(dirSet), truncated, skipped: 0, via: "input" };
}

/** Image files chosen one by one (for example every segment of an E01 set at the root of a USB disk). */
export function folderFromLooseFiles(list: File[]): BrowserPickedFolder | null {
  const sorted = [...list].sort((a, b) => ordinal(a.name, b.name));
  const files: BrowserPickedFile[] = [];
  const seen = new Set<string>();
  let truncated = false;
  for (const file of sorted) {
    if (!safeRelative(file.name) || file.name.includes("/") || seen.has(file.name)) continue;
    if (files.length >= MAX_BROWSER_FILES) {
      truncated = true;
      break;
    }
    seen.add(file.name);
    files.push({ path: file.name, file });
  }
  if (!files.length) return null;
  const lead = files.find((f) => IMAGE_FILE_RE.test(f.path)) ?? files[0];
  const name = lead.path.replace(/\.[^.]+$/, "") || lead.path;
  return { name, files, dirs: [], truncated, skipped: 0, via: "files" };
}

const IMAGE_FILE_RE = /\.(?:e01|ex01|l01|lx01|s01|001|dd|raw|img|bin|iso|vmdk|vhdx?|aff4?|dmg|ufdx?|pas|zip|tar|ad1)$/i;

function pickFilesWithInput(): Promise<File[] | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.style.display = "none";
    let done = false;
    const finish = (value: File[] | null) => {
      if (done) return;
      done = true;
      input.remove();
      resolve(value);
    };
    input.addEventListener("change", () => finish(input.files ? Array.from(input.files) : null));
    input.addEventListener("cancel", () => finish(null));
    document.body.appendChild(input);
    input.click();
  });
}

/**
 * Open this computer's file window and take the selected image files. Unlike a folder, files can be
 * chosen anywhere, including the root of a drive (browsers refuse a whole drive as a folder).
 */
export async function pickBrowserFiles(): Promise<BrowserPickedFolder | null> {
  const list = await pickFilesWithInput();
  return list && list.length ? folderFromLooseFiles(list) : null;
}

/**
 * Open this computer's folder window. Call it straight from a click handler (browsers only allow
 * it in response to a user action). Returns null when the examiner cancels.
 */
export async function pickBrowserFolder(onProgress?: (files: number) => void): Promise<BrowserPickedFolder | null> {
  const picker = (window as PickerWindow).showDirectoryPicker;
  if (typeof picker === "function") {
    let handle: DirHandle;
    try {
      handle = await picker.call(window, { id: "polaron-evidence", mode: "read" });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return null;
      throw err;
    }
    return walkDirectory(handle, onProgress);
  }
  const list = await pickWithInput();
  return list ? folderFromInputFiles(list) : null;
}

/* ------------------------------------------------------------------------ */
/* Manifest (same format and digest as the agent)                           */
/* ------------------------------------------------------------------------ */

function ordinal(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

export function manifestText(files: BrowserManifestFile[]): string {
  return files.map((f) => `${f.path}\t${Math.trunc(f.size)}\t${Math.trunc(f.mtime_ms)}\n`).join("");
}

export async function buildBrowserManifest(folder: BrowserPickedFolder): Promise<BrowserManifest> {
  const files = folder.files
    .map((f) => ({ path: f.path, size: f.file.size, mtime_ms: Math.trunc(f.file.lastModified || 0) }))
    .sort((a, b) => ordinal(a.path, b.path));
  const dirs = [...folder.dirs].sort(ordinal);
  const totalBytes = files.reduce((sum, f) => sum + f.size, 0);
  return { files, dirs, totalBytes, digest: await sha256Hex(manifestText(files)) };
}

/* ------------------------------------------------------------------------ */
/* SHA-256 (WebCrypto, with a small fallback for non-HTTPS consoles)        */
/* ------------------------------------------------------------------------ */

export async function sha256Hex(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  const subtle = typeof crypto !== "undefined" ? crypto.subtle : undefined;
  if (subtle) {
    const digest = await subtle.digest("SHA-256", data);
    return toHex(new Uint8Array(digest));
  }
  return toHex(sha256Fallback(data));
}

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
  0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
  0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
  0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

/** Plain SHA-256 for consoles opened over http:// on the LAN, where WebCrypto is unavailable. */
export function sha256Fallback(data: Uint8Array): Uint8Array {
  const bitLen = data.length * 8;
  const padded = new Uint8Array(((data.length + 9 + 63) >> 6) << 6);
  padded.set(data);
  padded[data.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bitLen / 0x100000000));
  view.setUint32(padded.length - 4, bitLen >>> 0);
  const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < padded.length; off += 64) {
    for (let i = 0; i < 16; i += 1) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i += 1) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = Array.from(h);
    for (let i = 0; i < 64; i += 1) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + s1 + ch + K[i] + w[i]) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (s0 + maj) >>> 0;
      hh = g;
      g = f;
      f = e;
      e = (d + t1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) >>> 0;
    }
    h[0] = (h[0] + a) >>> 0;
    h[1] = (h[1] + b) >>> 0;
    h[2] = (h[2] + c) >>> 0;
    h[3] = (h[3] + d) >>> 0;
    h[4] = (h[4] + e) >>> 0;
    h[5] = (h[5] + f) >>> 0;
    h[6] = (h[6] + g) >>> 0;
    h[7] = (h[7] + hh) >>> 0;
  }
  const out = new Uint8Array(32);
  const outView = new DataView(out.buffer);
  h.forEach((v, i) => outView.setUint32(i * 4, v));
  return out;
}

/* ------------------------------------------------------------------------ */
/* Wire encoding (same as agent 1.2: "zero" marker, raw DEFLATE)            */
/* ------------------------------------------------------------------------ */

export function isAllZero(data: Uint8Array): boolean {
  let i = 0;
  if (data.byteOffset % 4 === 0) {
    const words = new Uint32Array(data.buffer, data.byteOffset, data.byteLength >> 2);
    for (; i < words.length; i += 1) if (words[i] !== 0) return false;
    i = words.length << 2;
  }
  for (; i < data.length; i += 1) if (data[i] !== 0) return false;
  return true;
}

function deflateRawSupported(): boolean {
  if (typeof CompressionStream === "undefined") return false;
  try {
    new CompressionStream("deflate-raw" as CompressionFormat);
    return true;
  } catch {
    return false;
  }
}

async function deflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new CompressionStream("deflate-raw" as CompressionFormat));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function worthCompressing(raw: Uint8Array): Promise<boolean> {
  const sample = 4096;
  for (let i = 0; i < 4; i += 1) {
    const offset = Math.floor(((raw.length - sample) * i) / 3);
    if ((await deflateRaw(raw.subarray(offset, offset + sample))).length <= (sample * 9) / 10) return true;
  }
  return false;
}

export async function encodeForWire(raw: Uint8Array, accept: string): Promise<{ body: Uint8Array; encoding: string | null }> {
  const a = (accept || "").toLowerCase();
  if (!a || raw.length === 0) return { body: raw, encoding: null };
  if (a.includes("zero") && isAllZero(raw)) return { body: new Uint8Array(0), encoding: "zero" };
  if (a.includes("deflate") && raw.length >= 4096 && deflateRawSupported()) {
    if (raw.length > 64 * 1024 && !(await worthCompressing(raw))) return { body: raw, encoding: null };
    const packed = await deflateRaw(raw);
    if (packed.length < raw.length - Math.floor(raw.length / 10)) return { body: packed, encoding: "deflate" };
  }
  return { body: raw, encoding: null };
}

/* ------------------------------------------------------------------------ */
/* Serving one share from this tab                                          */
/* ------------------------------------------------------------------------ */

type ServedFile = BrowserManifestFile & { file: File };

type RelayMessage = { req_id?: string; op?: string; index?: number; start?: number; end?: number; accept?: string };

type RangeAnswer = {
  status: number;
  body: Uint8Array;
  etag?: string;
  contentRange?: string;
  error?: string;
  encoding?: string | null;
  rawLength?: number;
};

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return toHex(buf);
}

function randomToken(): string {
  const buf = new Uint8Array(32);
  crypto.getRandomValues(buf);
  let bin = "";
  buf.forEach((b) => {
    bin += String.fromCharCode(b);
  });
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function browserHostname(): string {
  const nav = typeof navigator !== "undefined" ? (navigator as Navigator & { userAgentData?: { platform?: string } }) : null;
  const platform = nav?.userAgentData?.platform || (nav?.platform || "").replace(/^Win\w*/i, "Windows");
  return platform ? `This computer (browser on ${platform})` : "This computer (browser)";
}

class BrowserShareServer {
  readonly shareId: string;
  readonly token: string;
  readonly jobId: string;
  readonly name: string;
  readonly hostname: string;
  private readonly files: ServedFile[];
  private readonly manifest: BrowserManifest;
  private readonly relayUrl: string;
  private readonly service: string;
  private readonly abort = new AbortController();
  private readonly started = Date.now();
  stats: BrowserShareStats;

  constructor(opts: {
    shareId: string;
    token: string;
    jobId: string;
    folder: BrowserPickedFolder;
    manifest: BrowserManifest;
    relay: ClientRelayTarget;
  }) {
    this.shareId = opts.shareId;
    this.token = opts.token;
    this.jobId = opts.jobId;
    this.name = opts.folder.name;
    this.hostname = browserHostname();
    this.manifest = opts.manifest;
    this.relayUrl = opts.relay.url.replace(/\/$/, "");
    this.service = opts.relay.service;
    const byPath = new Map(opts.folder.files.map((f) => [f.path, f.file]));
    this.files = opts.manifest.files.map((m) => ({ ...m, file: byPath.get(m.path) as File }));
    this.stats = {
      shareId: this.shareId,
      jobId: this.jobId,
      name: this.name,
      fileCount: this.files.length,
      totalBytes: opts.manifest.totalBytes,
      logicalBytes: 0,
      wireBytes: 0,
      requests: 0,
      state: "connecting",
      lastContactMs: 0,
    };
  }

  get stopped(): boolean {
    return this.abort.signal.aborted;
  }

  start(): void {
    for (let i = 0; i < POLLERS; i += 1) void this.loop();
  }

  stop(state: BrowserShareState = "stopped", error?: string): void {
    if (this.stopped) return;
    this.update({ state, error });
    this.abort.abort();
  }

  private update(patch: Partial<BrowserShareStats>): void {
    this.stats = { ...this.stats, ...patch };
    notify();
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const h: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
      "X-Polaron-Share": this.shareId,
      ...extra,
    };
    if (this.service) h["X-Aetheris-Service"] = this.service;
    return h;
  }

  private post(suffix: string, body: BodyInit, extra: Record<string, string> = {}): Promise<Response> {
    return fetch(`${this.relayUrl}${suffix}`, {
      method: "POST",
      headers: this.headers(extra),
      body,
      cache: "no-store",
      credentials: "omit",
      signal: this.abort.signal,
    });
  }

  private async loop(): Promise<void> {
    let backoff = 1000;
    let unknownSince = 0;
    const hello = JSON.stringify({ agent_version: BROWSER_AGENT_VERSION });
    while (!this.stopped) {
      try {
        const resp = await this.post("/poll", hello, { "Content-Type": "application/json", "X-Polaron-Wait": "25" });
        if (resp.status === 200 || resp.status === 204) {
          backoff = 1000;
          unknownSince = 0;
          if (this.stats.state !== "serving") this.update({ state: "serving", error: undefined });
          this.stats = { ...this.stats, lastContactMs: Date.now() };
          if (resp.status === 204) continue;
          const msg = (await resp.json()) as RelayMessage;
          await this.handle(msg);
          continue;
        }
        if (resp.status === 401 || resp.status === 403) {
          this.stop("rejected", "The forensic server no longer accepts this folder (it was released or replaced).");
          forgetStoredShare(this.shareId);
          break;
        }
        if (resp.status === 404) {
          // Not registered yet (the server registers it right after this tab starts), or the
          // server is rebuilding its share list after a restart: keep trying.
          if (!unknownSince) unknownSince = Date.now();
          if (Date.now() - unknownSince > 6 * 3600_000) {
            this.stop("stopped", "The forensic server does not know this folder.");
            break;
          }
          const young = Date.now() - this.started < 3 * 60_000;
          await sleep(young ? 1000 : 15_000, this.abort.signal);
          continue;
        }
        this.update({ state: "waiting", error: `The server answered HTTP ${resp.status}; retrying.` });
      } catch (err) {
        if (this.stopped) break;
        this.update({ state: "waiting", error: err instanceof Error ? err.message : "Network error; retrying." });
      }
      await sleep(backoff, this.abort.signal);
      backoff = Math.min(backoff * 2, 30_000);
    }
  }

  private async handle(msg: RelayMessage): Promise<void> {
    const reqId = String(msg.req_id || "");
    if (!/^[A-Za-z0-9]{1,64}$/.test(reqId)) return;
    // Acknowledge at once so the server can tell a slow answer from a lost request.
    try {
      await this.post(`/ack/${reqId}`, new Uint8Array(0), { "Content-Type": "application/octet-stream" });
    } catch {
      /* the answer below still counts */
    }
    let answer: RangeAnswer;
    try {
      if (msg.op === "range") {
        answer = await this.range(Number(msg.index), Number(msg.start), Number(msg.end), String(msg.accept || ""));
      } else if (msg.op === "manifest") {
        answer = { status: 200, body: new TextEncoder().encode(JSON.stringify(this.manifestPayload())) };
      } else if (msg.op === "revoke") {
        answer = { status: 200, body: new Uint8Array(0) };
      } else if (msg.op === "health") {
        answer = {
          status: 200,
          body: new TextEncoder().encode(
            JSON.stringify({ ok: true, service: "polaron-client-evidence-agent", version: BROWSER_AGENT_VERSION, origin_allowed: true, relay_supported: true }),
          ),
        };
      } else {
        answer = { status: 400, body: new Uint8Array(0), error: "unknown_op" };
      }
    } catch {
      answer = { status: 500, body: new Uint8Array(0), error: "agent_error" };
    }
    const extra: Record<string, string> = {
      "Content-Type": "application/octet-stream",
      "X-Relay-Status": String(answer.status),
    };
    if (answer.etag) extra["X-Relay-ETag"] = answer.etag;
    if (answer.contentRange) extra["X-Relay-Content-Range"] = answer.contentRange;
    if (answer.error) extra["X-Relay-Error"] = answer.error;
    if (answer.encoding) {
      extra["X-Relay-Encoding"] = answer.encoding;
      extra["X-Relay-Raw-Length"] = String(answer.rawLength ?? 0);
    }
    try {
      await this.post(`/respond/${reqId}`, answer.body as BodyInit, extra);
      if (msg.op === "range" && answer.status === 206) {
        this.update({
          logicalBytes: this.stats.logicalBytes + (answer.rawLength ?? answer.body.length),
          wireBytes: this.stats.wireBytes + answer.body.length,
          requests: this.stats.requests + 1,
        });
      }
    } catch {
      /* the server gives up after its timeout and simply asks again */
    }
    if (msg.op === "revoke") {
      forgetStoredShare(this.shareId);
      this.stop("stopped");
      servers.delete(this.shareId);
      notify();
    }
  }

  private manifestPayload(): Record<string, unknown> {
    return {
      ok: true,
      agent_version: BROWSER_AGENT_VERSION,
      share_id: this.shareId,
      root: this.name,
      hostname: this.hostname,
      files: this.manifest.files,
      dirs: this.manifest.dirs,
      file_count: this.manifest.files.length,
      total_bytes: this.manifest.totalBytes,
      manifest_sha256: this.manifest.digest,
      truncated: false,
    };
  }

  private async range(index: number, start: number, end: number, accept: string): Promise<RangeAnswer> {
    const empty = new Uint8Array(0);
    if (!Number.isInteger(index) || index < 0 || index >= this.files.length) return { status: 404, body: empty, error: "not_found" };
    const f = this.files[index];
    if (!f.file) return { status: 404, body: empty, error: "not_found" };
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start || start >= f.size) {
      return { status: 416, body: empty, error: "bad_range" };
    }
    const last = Math.min(end, f.size - 1);
    const length = last - start + 1;
    if (length > MAX_RELAY_CHUNK) return { status: 416, body: empty, error: "range_too_large" };
    // A file that changed since it was chosen no longer matches: the browser refuses to read it.
    if (f.file.size !== f.size || Math.trunc(f.file.lastModified || 0) !== f.mtime_ms) {
      return { status: 409, body: empty, error: "evidence_modified" };
    }
    let raw: Uint8Array;
    try {
      raw = new Uint8Array(await f.file.slice(start, last + 1).arrayBuffer());
    } catch {
      return { status: 409, body: empty, error: "evidence_modified" };
    }
    if (raw.length !== length) return { status: 409, body: empty, error: "evidence_modified" };
    const wire = await encodeForWire(raw, accept);
    return {
      status: 206,
      body: wire.body,
      etag: `"${f.size}-${f.mtime_ms}"`,
      contentRange: `bytes ${start}-${last}/${f.size}`,
      encoding: wire.encoding,
      rawLength: length,
    };
  }
}

/* ------------------------------------------------------------------------ */
/* Registry of shares served by this tab                                    */
/* ------------------------------------------------------------------------ */

const servers = new Map<string, BrowserShareServer>();
const listeners = new Set<() => void>();
let unloadGuard = false;
let lockRelease: (() => void) | null = null;

function notify(): void {
  listeners.forEach((fn) => {
    try {
      fn();
    } catch {
      /* a listener error must not stop serving */
    }
  });
}

function anyServing(): boolean {
  return Array.from(servers.values()).some((s) => !s.stopped);
}

function guardTab(): void {
  if (!unloadGuard && typeof window !== "undefined") {
    unloadGuard = true;
    window.addEventListener("beforeunload", (event) => {
      if (!anyServing()) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }
  // A held Web Lock keeps the browser from freezing this tab while it serves evidence.
  const locks = typeof navigator !== "undefined" ? (navigator as Navigator & { locks?: LockManager }).locks : undefined;
  if (!lockRelease && locks?.request) {
    void locks.request("polaron-browser-evidence", () =>
      new Promise<void>((resolve) => {
        lockRelease = resolve;
      }),
    ).catch(() => undefined);
  }
}

function releaseTabGuard(): void {
  if (!anyServing() && lockRelease) {
    lockRelease();
    lockRelease = null;
  }
}

export function subscribeBrowserShares(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function activeBrowserShares(): BrowserShareStats[] {
  return Array.from(servers.values()).filter((s) => !s.stopped).map((s) => s.stats);
}

export function browserShareStats(shareId: string): BrowserShareStats | null {
  const s = servers.get(shareId);
  return s && !s.stopped ? s.stats : null;
}

/* ------------------------------------------------------------------------ */
/* Remembered credentials (this tab only) for "Reconnect" after a reload    */
/* ------------------------------------------------------------------------ */

function readStore(): Record<string, StoredShare> {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Record<string, StoredShare>) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeStore(store: Record<string, StoredShare>): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    /* storage unavailable: Reconnect just will not be offered */
  }
}

function rememberShare(entry: StoredShare): void {
  const store = readStore();
  store[entry.shareId] = entry;
  writeStore(store);
}

function forgetStoredShare(shareId: string): void {
  const store = readStore();
  if (store[shareId]) {
    delete store[shareId];
    writeStore(store);
  }
}

export function storedBrowserShare(shareId: string): { name: string; jobId: string; fileCount: number } | null {
  const entry = readStore()[shareId];
  return entry && entry.registered ? { name: entry.name, jobId: entry.jobId, fileCount: entry.fileCount } : null;
}

/* ------------------------------------------------------------------------ */
/* Public API used by the evidence panels                                   */
/* ------------------------------------------------------------------------ */

/**
 * Start serving a chosen folder from this tab and return the same share description the agent
 * returns, ready for POST /api/jobs/{id}/evidence/client-source (with browser: true).
 */
export async function createBrowserShare(opts: {
  folder: BrowserPickedFolder;
  jobId: string;
  relay: ClientRelayTarget;
}): Promise<ClientShare> {
  if (!opts.folder.files.length) throw new Error(`${opts.folder.name} contains no files.`);
  const manifest = await buildBrowserManifest(opts.folder);
  const shareId = `ce${randomHex(12)}`;
  const token = randomToken();
  const server = new BrowserShareServer({ shareId, token, jobId: opts.jobId, folder: opts.folder, manifest, relay: opts.relay });
  servers.set(shareId, server);
  guardTab();
  server.start();
  notify();
  rememberShare({
    shareId,
    token,
    jobId: opts.jobId,
    name: opts.folder.name,
    digest: manifest.digest,
    fileCount: manifest.files.length,
    totalBytes: manifest.totalBytes,
    relayUrl: opts.relay.url,
    service: opts.relay.service,
    registered: false,
    createdAt: Date.now(),
  });
  return {
    ok: true,
    share_id: shareId,
    token,
    root: opts.folder.name,
    hostname: server.hostname,
    addresses: [],
    port: 9877,
    lan_bound: false,
    file_count: manifest.files.length,
    dir_count: manifest.dirs.length,
    total_bytes: manifest.totalBytes,
    manifest_sha256: manifest.digest,
    truncated: opts.folder.truncated,
    skipped: opts.folder.skipped,
    expires_utc: "",
    agent_version: BROWSER_AGENT_VERSION,
    files_preview: manifest.files.slice(0, 500).map((f) => ({ path: f.path, size: f.size })),
    relay_enabled: true,
    relay_supported: true,
  };
}

/** The server accepted the share: offer "Reconnect" for it after a page reload. */
export function markBrowserShareRegistered(shareId: string): void {
  const store = readStore();
  if (store[shareId]) {
    store[shareId].registered = true;
    writeStore(store);
  }
}

/** Stop serving a share from this tab (registration failed, or the examiner released it). */
export function stopBrowserShare(shareId: string): void {
  const server = servers.get(shareId);
  if (server) server.stop("stopped");
  servers.delete(shareId);
  forgetStoredShare(shareId);
  releaseTabGuard();
  notify();
}

/**
 * Serve an already-registered share again after the page was reloaded. The examiner chooses the
 * same folder; it is accepted only if its frozen file list is identical (same digest).
 */
export async function reconnectBrowserShare(shareId: string, folder: BrowserPickedFolder): Promise<BrowserShareStats> {
  const entry = readStore()[shareId];
  if (!entry || !entry.registered) throw new Error("This tab has no record of that folder. Select the evidence folder again.");
  const manifest = await buildBrowserManifest(folder);
  if (manifest.digest !== entry.digest) {
    throw new Error(
      `${folder.name} does not match the folder that was selected for this job (different files, sizes or modified times). Choose the original folder; it must be unchanged.`,
    );
  }
  servers.get(shareId)?.stop("stopped");
  const server = new BrowserShareServer({
    shareId,
    token: entry.token,
    jobId: entry.jobId,
    folder,
    manifest,
    relay: { url: entry.relayUrl, service: entry.service },
  });
  servers.set(shareId, server);
  guardTab();
  server.start();
  notify();
  return server.stats;
}

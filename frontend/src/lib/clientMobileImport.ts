/**
 * V1.27.0 — a phone plugged into the examiner's own PC (not the forensic server).
 *
 * 1. The HostDrive helper on this PC (127.0.0.1:9876) creates the image here, in
 *    C:\evidence\<CASE>\… (same 01–10 case layout as the server).
 * 2. The Client Evidence Agent on this PC (127.0.0.1:9877) publishes that case folder
 *    as a read-only share.
 * 3. The forensic server copies the run into its own case folder "as usual"
 *    (SHA-256 verified on the server disk) and returns a result with server paths.
 *    Registration, parsing, RAG and the report then run on the server.
 */

import { ClientAgentError, clientAgentStatus, clientRelayTargetForService, createClientShare, joinClientPath, listClientDirectory, revokeClientShare } from "./clientEvidenceAgent";
import { inferApiService } from "./apiRouting";
import { createBrowserShare, stopBrowserShare, type BrowserPickedFolder } from "./browserEvidenceShare";
import { acquisitionApi } from "./forensicApi";
import { getLocalHelperIdentity, isBrowserOnOfficeHost } from "./hostDriveHelper";
import type { ClientImportCapabilities, ClientImportState } from "./types/forensic";

export const DEFAULT_CLIENT_CASE_ROOT = "C:\\evidence";

export type ClientImportScope = "full" | "packages";

/** Where the phone is plugged in, from the browser's point of view. */
export type AcquisitionSite = "server" | "client";

export type SiteIdentity = {
  /** true: this PC is the forensic server; false: a client PC; null: cannot tell. */
  localIsServer: boolean | null;
  localComputer: string;
  serverComputer: string;
  capabilities: ClientImportCapabilities | null;
};

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

let identityCache: { at: number; value: SiteIdentity } | null = null;

/** Compare the local helper's computer name with the server helper's (both report it from 5.58). */
export async function resolveSiteIdentity(opts?: { force?: boolean }): Promise<SiteIdentity> {
  if (!opts?.force && identityCache && Date.now() - identityCache.at < 60_000) return identityCache.value;
  const [local, capabilities] = await Promise.all([
    getLocalHelperIdentity().catch(() => null),
    acquisitionApi.clientImportCapabilities().catch(() => null),
  ]);
  const localComputer = String(local?.computer_name || "").trim();
  const serverComputer = String(capabilities?.office_helper?.computer_name || "").trim();
  let localIsServer: boolean | null = null;
  if (isBrowserOnOfficeHost()) localIsServer = true;
  else if (localComputer && serverComputer) localIsServer = localComputer.toLowerCase() === serverComputer.toLowerCase();
  const value: SiteIdentity = { localIsServer, localComputer, serverComputer, capabilities };
  identityCache = { at: Date.now(), value };
  return value;
}

/**
 * office_server devices are always collected by the server. A this_pc device is a
 * client-side collection unless this PC is the forensic server itself.
 */
export function deviceSite(device: { attach_host?: string } | null | undefined, localIsServer: boolean | null): AcquisitionSite {
  if (!device) return "server";
  if (device.attach_host === "office_server") return "server";
  if (device.attach_host === "this_pc") return localIsServer === true ? "server" : "client";
  return localIsServer === false ? "client" : "server";
}

function normCase(value: string): string {
  return value.replace(/[^A-Za-z0-9]+/g, "").toUpperCase();
}

/**
 * C:\evidence\<case folder> for a case ID. Native MTP/iOS jobs keep the case ID
 * (non-word characters become "_"); the ADB orchestrator upper-cases it.
 */
export async function findClientCaseDir(clientCaseRoot: string, caseId: string): Promise<string | null> {
  const want = normCase(caseId);
  if (!want) return null;
  let listing;
  try {
    listing = await listClientDirectory(clientCaseRoot);
  } catch {
    return null;
  }
  const dirs = listing.entries.filter((e) => e.kind === "dir");
  const hit = dirs.find((d) => d.name === caseId) || dirs.find((d) => normCase(d.name) === want);
  return hit ? joinClientPath(listing.path || clientCaseRoot, hit.name) : null;
}

/**
 * A run is sealed once 07_Logs/<run>/collection_summary.json exists: the collection
 * jobs write it last, after the export packages. Copying earlier would miss files.
 */
export async function clientRunSealed(caseDir: string, runName?: string): Promise<{ sealed: boolean; runName?: string }> {
  const logsDir = joinClientPath(caseDir, "07_Logs");
  let logs;
  try {
    logs = await listClientDirectory(logsDir);
  } catch {
    return { sealed: false };
  }
  const wanted = String(runName || "").trim().toLowerCase();
  const runs = logs.entries
    .filter((e) => e.kind === "dir")
    .sort((a, b) => (b.mtime_ms || 0) - (a.mtime_ms || 0) || b.name.localeCompare(a.name));
  // The named run, or else the newest one (the collection that was just written): an
  // older sealed run must never stand in for a collection that is still being written.
  const run = (wanted && runs.find((e) => e.name.toLowerCase() === wanted)) || runs[0];
  if (!run) return { sealed: false };
  try {
    const inner = await listClientDirectory(joinClientPath(logsDir, run.name));
    if (inner.entries.some((e) => e.kind === "file" && e.name.toLowerCase() === "collection_summary.json")) {
      return { sealed: true, runName: run.name };
    }
  } catch {
    /* folder vanished or unreadable */
  }
  return { sealed: false, runName: run.name };
}

export class ClientAgentRequiredError extends ClientAgentError {
  constructor(message: string, code = "agent_offline") {
    super(message, code);
    this.name = "ClientAgentRequiredError";
  }
}

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

async function follow(
  start: ClientImportState,
  onProgress?: (state: ClientImportState) => void,
  signal?: AbortSignal,
): Promise<ClientImportState> {
  let state = start;
  let misses = 0;
  let cancelSent = false;
  onProgress?.(state);
  while (!TERMINAL.has(state.status)) {
    await sleep(1_500);
    if (signal?.aborted && !cancelSent) {
      cancelSent = true;
      await acquisitionApi.cancelClientImport(state.import_id).catch(() => undefined);
    }
    try {
      state = await acquisitionApi.getClientImport(state.import_id);
      misses = 0;
      onProgress?.(state);
    } catch (e) {
      // The copy runs on the server; a few failed polls (API restart, Wi-Fi) do not stop it.
      misses += 1;
      if (misses > 40) throw e;
    }
  }
  return state;
}

/** Reattach to a copy that is already running on the server (page reload). */
export async function resumeClientImport(
  importId: string,
  onProgress?: (state: ClientImportState) => void,
  signal?: AbortSignal,
): Promise<ClientImportState | null> {
  try {
    const state = await acquisitionApi.getClientImport(importId);
    return await follow(state, onProgress, signal);
  } catch {
    return null;
  }
}

/** Share the client case folder read-only and have the forensic server copy the run. */
export async function copyClientRunToServer(opts: {
  caseDir: string;
  caseId: string;
  runName?: string;
  scope?: ClientImportScope;
  onProgress?: (state: ClientImportState) => void;
  signal?: AbortSignal;
  /** V1.31.2: a case folder chosen in this browser (no agent): this tab serves it to the server. */
  browser?: BrowserPickedFolder;
}): Promise<ClientImportState> {
  if (opts.browser) return copyBrowserFolderToServer({ ...opts, browser: opts.browser });
  const agent = await clientAgentStatus(2_500);
  if (agent.state !== "online") {
    throw new ClientAgentRequiredError(
      agent.state === "unpaired"
        ? "The Client Evidence Agent on this computer is not paired with this console. Run its installer again for this console address, then copy again."
        : "The Client Evidence Agent is not running on this computer. Install it once (instructions below) so the forensic server can copy the image.",
      agent.state === "unpaired" ? "agent_unpaired" : "agent_offline",
    );
  }
  const service = inferApiService("/api/acquisition/client-import");
  const share = await createClientShare({
    path: opts.caseDir,
    jobId: `mobile-image-${opts.caseId}`.slice(0, 80),
    label: `Mobile image ${opts.caseId}`.slice(0, 200),
    relay: clientRelayTargetForService(service),
  });
  if (!share.lan_bound && !share.relay_enabled) {
    await revokeClientShare(share.share_id);
    throw new ClientAgentRequiredError(
      "The Client Evidence Agent is listening on this computer only, so the forensic server cannot reach it. Install the latest agent (it connects out to the server).",
      "agent_local_only",
    );
  }
  let started: ClientImportState;
  try {
    started = await acquisitionApi.startClientImport({
      share_id: share.share_id,
      token: share.token,
      port: share.port,
      addresses: share.addresses,
      hostname: share.hostname,
      client_root: share.root,
      file_count: share.file_count,
      total_bytes: share.total_bytes,
      manifest_sha256: share.manifest_sha256,
      relay: Boolean(share.relay_enabled),
      skipped: share.skipped || 0,
      agent_version: share.agent_version,
      case_id: opts.caseId,
      run_name: opts.runName || "",
      scope: opts.scope || "full",
    });
  } catch (e) {
    // The server never accepted the share: withdraw it on this computer as well.
    await revokeClientShare(share.share_id);
    throw e;
  }
  try {
    return await follow(started, opts.onProgress, opts.signal);
  } finally {
    // The server revokes the share when the copy ends; this is a second, local attempt.
    void revokeClientShare(share.share_id);
  }
}

/**
 * V1.31.2: the same server copy, for a case folder chosen in the browser. This tab answers the server's
 * reads over the outbound relay (like the agent since V1.22), so it must stay open until the copy ends.
 */
async function copyBrowserFolderToServer(opts: {
  caseId: string;
  runName?: string;
  scope?: ClientImportScope;
  onProgress?: (state: ClientImportState) => void;
  signal?: AbortSignal;
  browser: BrowserPickedFolder;
}): Promise<ClientImportState> {
  const service = inferApiService("/api/acquisition/client-import");
  const share = await createBrowserShare({
    folder: opts.browser,
    jobId: `mobile-image-${opts.caseId}`.slice(0, 80),
    relay: clientRelayTargetForService(service),
  });
  let started: ClientImportState;
  try {
    started = await acquisitionApi.startClientImport({
      share_id: share.share_id,
      token: share.token,
      port: share.port,
      addresses: [],
      hostname: share.hostname,
      client_root: share.root,
      file_count: share.file_count,
      total_bytes: share.total_bytes,
      manifest_sha256: share.manifest_sha256,
      relay: true,
      browser: true,
      skipped: share.skipped || 0,
      agent_version: share.agent_version,
      case_id: opts.caseId,
      run_name: opts.runName || "",
      scope: opts.scope || "full",
    });
  } catch (e) {
    stopBrowserShare(share.share_id);
    throw e;
  }
  try {
    return await follow(started, opts.onProgress, opts.signal);
  } finally {
    // The server revokes the share when the copy ends; this stops serving in this tab as well.
    stopBrowserShare(share.share_id);
  }
}

export function formatImportBytes(bytes: number | null | undefined): string {
  const n = Number(bytes) || 0;
  if (n >= 1024 ** 4) return `${(n / 1024 ** 4).toFixed(2)} TB`;
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${Math.max(0, Math.round(n))} B`;
}

/**
 * V1.31.14 — a collection on this computer is sent to the forensic server while it runs.
 *
 * The page asks the server for an upload session and hands it to this computer's helper with the
 * collection. The helper's uploader sends several finished files at a time, and several pieces of a large file at once; the server checks each file's
 * SHA-256 on its own disk. When the collection ends, the server registers the run and the copy on this
 * computer is removed (unless the examiner keeps one). The page follows the session like a client copy
 * (GET /api/acquisition/client-import/<id>) and, through the helper, the uploader itself, so a refused
 * upload ends the wait instead of polling forever.
 *
 * The upload token goes to the helper only; the page keeps the session id, never the token.
 */
import { acquisitionApi } from "./forensicApi";
import { getHostStreamStatus, type HostStreamStatus } from "./hostDriveHelper";
import type { ClientImportScope } from "./clientMobileImport";
import type { ClientImportState, ClientStreamSession } from "./types/forensic";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
/** Uploader states after which it sends nothing more. */
export const UPLOADER_STOPPED = new Set(["kept_local", "cancelled", "failed"]);

export type StreamStartFields = {
  stream_session: string;
  stream_token: string;
  stream_service: string;
  stream_keep_local: "0" | "1";
  stream_scope: ClientImportScope;
};

/** Fields added to the helper's /acquisition/run body. "Packages only" never deletes the local copy. */
export function streamStartFields(
  session: Pick<ClientStreamSession, "session_id" | "token" | "service">,
  keepLocal: boolean,
  scope: ClientImportScope = "full",
): StreamStartFields {
  return {
    stream_session: session.session_id,
    stream_token: session.token,
    stream_service: session.service || "",
    stream_keep_local: keepLocal || scope !== "full" ? "1" : "0",
    stream_scope: scope,
  };
}

export async function createClientStream(opts: {
  caseId: string;
  scope: ClientImportScope;
  keepLocal: boolean;
  clientHost?: string;
}): Promise<{ session: ClientStreamSession | null; error: string }> {
  try {
    const session = await acquisitionApi.createClientStream({
      case_id: opts.caseId,
      scope: opts.scope,
      keep_local: opts.keepLocal || opts.scope !== "full",
      client_host: opts.clientHost || "",
    });
    if (!session?.session_id || !session.token) return { session: null, error: "The server did not open an upload session." };
    return { session, error: "" };
  } catch (e) {
    return { session: null, error: e instanceof Error ? e.message : "The server did not open an upload session." };
  }
}

/** Ends an upload session the helper will not use (old helper, refused). */
export async function dropClientStream(sessionId: string): Promise<void> {
  await acquisitionApi.cancelClientImport(sessionId).catch(() => undefined);
}

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

/**
 * Waits until the server registered the run (or the upload stopped). The collection itself may still
 * be running; ``onProgress`` gets the server's view, ``onUploader`` the uploader's.
 */
export async function followClientStream(opts: {
  sessionId: string;
  jobId?: string;
  onProgress?: (state: ClientImportState) => void;
  onUploader?: (status: HostStreamStatus) => void;
  signal?: AbortSignal;
  pollMs?: number;
}): Promise<ClientImportState> {
  const pollMs = opts.pollMs ?? 2_000;
  let state: ClientImportState | null = null;
  let misses = 0;
  let stoppedSeen = 0;
  for (let tick = 0; ; tick += 1) {
    if (opts.signal?.aborted) throw new DOMException("aborted", "AbortError");
    try {
      state = await acquisitionApi.getClientImport(opts.sessionId);
      misses = 0;
      opts.onProgress?.(state);
      if (TERMINAL.has(state.status)) return state;
    } catch (e) {
      // The session lives on the server; a few failed polls (API restart, Wi-Fi) do not end it.
      misses += 1;
      if (misses > 40) throw e;
    }
    if (opts.jobId && tick % 2 === 0) {
      const up = await getHostStreamStatus(opts.jobId);
      if (up) {
        opts.onUploader?.(up);
        if (up.state && UPLOADER_STOPPED.has(up.state)) {
          // Give the server a moment: the uploader may have aborted the session itself.
          stoppedSeen += 1;
          if (stoppedSeen >= 2 && state) {
            return {
              ...state,
              status: up.state === "cancelled" ? "cancelled" : "failed",
              error: up.error || up.detail || "This computer stopped sending the collection to the server.",
            };
          }
        }
      }
    }
    await sleep(pollMs);
  }
}

export function formatStreamBytes(n: number | null | undefined): string {
  const v = Number(n || 0);
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(1)} MB`;
  if (v >= 1024) return `${Math.round(v / 1024)} KB`;
  return `${v} B`;
}

/** One line for the Collect card while the collection runs. */
export function streamProgressLine(state: ClientImportState | null, uploader?: HostStreamStatus | null): string {
  const files = Number(state?.files_done ?? uploader?.files_sent ?? 0);
  const bytes = Number(state?.bytes_done ?? uploader?.bytes_sent ?? 0);
  if (uploader?.state && UPLOADER_STOPPED.has(uploader.state)) {
    return uploader.detail || "This computer stopped sending the collection to the server.";
  }
  if (!files) return "Sending to the forensic server as soon as the first files are finished.";
  return `Sent to the forensic server and verified there: ${files.toLocaleString()} file(s), ${formatStreamBytes(bytes)}.`;
}

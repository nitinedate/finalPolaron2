import { useCallback, useEffect, useReducer, useState } from "react";
import { FolderOpen, Laptop } from "lucide-react";

import { Button, Card } from "../ui";
import { forensicApi, type ClientEvidenceSourceStatus } from "../../lib/forensicApi";
import { formatClientBytes } from "../../lib/clientEvidenceAgent";
import {
  browserShareStats,
  pickBrowserFolder,
  reconnectBrowserShare,
  storedBrowserShare,
  subscribeBrowserShares,
} from "../../lib/browserEvidenceShare";

/**
 * Shown on a job whose evidence stays on the examiner's computer. While the
 * pipeline runs it re-checks every 30 s whether the server can still reach that
 * computer, so a sleeping laptop is visible before extraction stalls.
 */
export function ClientEvidenceSourceCard({
  jobId,
  fallback,
  active,
}: {
  jobId: string;
  fallback?: { hostname?: string; client_root?: string; file_count?: number; total_bytes?: number } | null;
  active: boolean;
}) {
  const [status, setStatus] = useState<ClientEvidenceSourceStatus | null>(null);
  const [, refresh] = useReducer((n: number) => n + 1, 0);
  const [reconnecting, setReconnecting] = useState(false);
  const [reconnectError, setReconnectError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      return await forensicApi.getClientEvidenceSource(jobId, true);
    } catch {
      return null; /* keep the last known state */
    }
  }, [jobId]);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      const next = await load();
      if (!cancelled && next) setStatus(next);
    };
    void tick();
    if (!active) return () => {
      cancelled = true;
    };
    const id = window.setInterval(() => void tick(), 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [load, active]);

  // A folder chosen in the browser is served by this tab: follow its counters live.
  useEffect(() => {
    const unsubscribe = subscribeBrowserShares(() => undefined);
    const id = window.setInterval(refresh, 2000);
    return () => {
      unsubscribe();
      window.clearInterval(id);
    };
  }, []);

  const shareId = status?.share_id || "";
  const browserShare = Boolean(status?.browser_share);
  const served = browserShare && shareId ? browserShareStats(shareId) : null;
  const remembered = browserShare && shareId && !served ? storedBrowserShare(shareId) : null;

  async function reconnect() {
    setReconnectError(null);
    setReconnecting(true);
    try {
      const picked = await pickBrowserFolder();
      if (!picked) return;
      await reconnectBrowserShare(shareId, picked);
      const next = await load();
      if (next) setStatus(next);
    } catch (e) {
      setReconnectError(e instanceof Error ? e.message : "Could not reconnect the folder.");
    } finally {
      setReconnecting(false);
    }
  }

  const host = status?.hostname || fallback?.hostname || "client computer";
  const root = status?.client_root || fallback?.client_root || "";
  const files = status?.file_count ?? fallback?.file_count;
  const bytes = status?.total_bytes ?? fallback?.total_bytes;
  const reader = status?.reader;
  const transfer = status?.transfer;
  const offline = status?.agent_online === false && !served;
  const failed = reader?.state === "failed";
  const waiting = reader?.state === "waiting";

  const tone = failed
    ? "border-red-200 bg-red-50 text-red-950"
    : offline || waiting
      ? "border-amber-200 bg-amber-50 text-amber-950"
      : "border-emerald-200 bg-emerald-50 text-emerald-950";

  return (
    <Card className={`mb-4 p-4 text-sm ${tone}`}>
      <p className="flex items-center gap-1.5 font-semibold">
        <Laptop className="h-4 w-4" />
        Evidence directory ready on {host}
      </p>
      <p className="mt-1 text-xs opacity-90">
        {browserShare
          ? "The image stays on that computer. The forensic server reads it in place over the network through the browser tab that chose the folder and stores no copy of it. Keep that tab open and the computer awake until extraction and indexing finish."
          : "The image stays on that computer. The forensic server reads it in place over the network through the Client Evidence Agent and stores no copy of it. Keep that computer on, awake and connected until extraction finishes."}
      </p>
      {root ? <p className="mt-2 break-all font-mono text-xs">{root}</p> : null}
      {files != null ? (
        <p className="mt-1 text-xs opacity-80">
          {files.toLocaleString()} file(s){bytes ? `, ${formatClientBytes(bytes)}` : ""}
          {status?.truncated ? " (folder listing was capped; choose a narrower folder if evidence is missing)" : ""}
        </p>
      ) : null}
      {transfer && transfer.logical_bytes > 0 ? (
        <p className="mt-2 text-xs" data-testid="client-transfer">
          Read from {host} so far: <span className="font-semibold">{formatClientBytes(transfer.logical_bytes)}</span>
          {bytes ? ` of the ${formatClientBytes(bytes)} evidence` : ""}
          {transfer.wire_bytes < transfer.logical_bytes
            ? `, ${formatClientBytes(transfer.wire_bytes)} sent over the network (empty space skipped and data compressed)`
            : ""}
          .
        </p>
      ) : null}
      {served ? (
        <p className="mt-2 text-xs font-medium" data-testid="browser-share-serving">
          Served by this browser tab: {formatClientBytes(served.logicalBytes)} read so far
          {served.wireBytes < served.logicalBytes ? ` (${formatClientBytes(served.wireBytes)} sent)` : ""}.
          {served.state === "waiting" ? " Reconnecting to the server…" : " Keep this tab open."}
        </p>
      ) : null}
      {browserShare && !served ? (
        <div className="mt-2 space-y-1.5 text-xs" data-testid="browser-share-reconnect">
          <p className="font-medium">
            {remembered
              ? `This tab is no longer serving ${remembered.name} (the page was reloaded). Reconnect the same folder to continue; then use Resume if processing paused.`
              : "The folder is served by the browser tab that chose it. Keep that tab open, or reconnect the same folder here if that tab was closed."}
          </p>
          {remembered ? (
            <Button variant="outline" className="h-7 px-2 text-[11px]" loading={reconnecting} onClick={() => void reconnect()}>
              <FolderOpen className="mr-1 h-3 w-3" />
              Reconnect folder from this computer…
            </Button>
          ) : null}
          {reconnectError ? <p className="text-red-800" role="alert">{reconnectError}</p> : null}
        </div>
      ) : null}
      {status ? (
        <p className="mt-2 text-xs font-medium" role="status">
          {failed
            ? reader?.detail || "Reading from the client computer stopped."
            : waiting
              ? reader?.detail || `Waiting for ${host} to come back on the network…`
              : offline
                ? `${host} is not answering. Extraction pauses until it is back; then use Resume if needed.`
                : status.agent_online
                  ? browserShare
                    ? `Connected to ${host} through its browser tab. Speed depends on its upload.`
                    : status.transport === "relay"
                    ? `Connected to ${host} through its outbound connection to this server. Speed depends on its upload.`
                    : `Connected to ${host}${status.agent_url ? ` (${status.agent_url})` : ""}.`
                  : null}
        </p>
      ) : null}
    </Card>
  );
}

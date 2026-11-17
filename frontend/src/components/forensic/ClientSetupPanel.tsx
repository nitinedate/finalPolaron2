import { useState } from "react";
import { Download, MonitorSmartphone } from "lucide-react";

import { Button } from "../ui";
import { CLIENT_SETUP_FILENAME, downloadClientSetup } from "../../lib/clientKit";
import { compareHelperVersions, type LocalHelperState } from "../../lib/hostDriveHelper";

export type ClientSetupState = "outdated" | "missing" | "update_available";

/**
 * V1.31.8: what this computer needs before phones plugged into it can be listed and collected.
 * - outdated: the helper here is too old to trust (its phone list is not shown);
 * - missing: no helper answers here (not set up, or stopped), and this is not the server itself;
 * - update_available: the helper works, but the server has a newer one.
 */
export function clientSetupState(opts: {
  localHelper: LocalHelperState | null;
  helperOnline: boolean | null;
  serverHelperVersion: string;
  officeHost: boolean;
}): ClientSetupState | null {
  const { localHelper, helperOnline, serverHelperVersion, officeHost } = opts;
  if (localHelper?.outdated) return "outdated";
  if (helperOnline === false && !officeHost) return "missing";
  if (
    localHelper?.version &&
    serverHelperVersion &&
    compareHelperVersions(localHelper.version, serverHelperVersion) < 0
  ) {
    return "update_available";
  }
  return null;
}

export function ClientSetupPanel({
  state,
  localVersion,
  serverVersion,
}: {
  state: ClientSetupState;
  localVersion?: string;
  serverVersion?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloaded, setDownloaded] = useState(false);

  const title =
    state === "outdated"
      ? "Update this computer before collecting"
      : state === "missing"
        ? "Set up this computer for phone collection"
        : "An update is available for this computer";
  const button = state === "missing" ? "Set up this computer" : "Update this computer";
  const tone =
    state === "update_available" ? "border-ink-200 bg-ink-50 text-ink-700" : "border-amber-300 bg-amber-50 text-amber-900";

  async function download() {
    setBusy(true);
    setError(null);
    try {
      await downloadClientSetup();
      setDownloaded(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "The setup file could not be downloaded.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div data-testid="client-setup-panel" data-state={state} className={`rounded-lg border px-3 py-3 text-sm ${tone}`}>
      <div className="flex items-start gap-3">
        <MonitorSmartphone className="mt-0.5 h-5 w-5 shrink-0" />
        <div className="min-w-0 flex-1 space-y-1.5">
          <p className="font-medium">{title}</p>
          {state === "outdated" && (
            <p>
              This computer runs Polaron helper {localVersion || "(an old version)"}
              {serverVersion ? `; the server has ${serverVersion}` : ""}. That helper cannot be trusted to say which
              phone is plugged in (it can keep showing an unplugged iPhone and miss an Android phone), so phones on
              this computer are not listed until it is updated.
            </p>
          )}
          {state === "missing" && (
            <p>
              The Polaron helper is not running on this computer, so a phone plugged in here cannot be seen. Phones
              plugged into the office server are still listed.
            </p>
          )}
          {state === "update_available" && (
            <p>
              This computer runs Polaron helper {localVersion}; the server has {serverVersion}. A computer set up with
              the setup file updates itself within about 10 minutes (never during a collection). Otherwise update it
              here.
            </p>
          )}
          <ol className="list-decimal space-y-0.5 pl-5 text-xs">
            <li>
              Click <strong>{button}</strong> and open <code>{CLIENT_SETUP_FILENAME}</code> (Windows may ask you to
              confirm running it).
            </li>
            <li>
              It downloads the Polaron client files from this server, restarts the helper (never while a collection is
              running), and from then on keeps this computer updated by itself. It offers the Client Evidence Agent
              too, which copies images made here to the server.
            </li>
            <li>When it says Done, click Rescan.</li>
          </ol>
          {downloaded && (
            <p className="text-xs font-medium" data-testid="client-setup-downloaded">
              Downloaded. Open {CLIENT_SETUP_FILENAME} from your Downloads folder, then click Rescan when it is done.
            </p>
          )}
          {error && (
            <p className="text-xs font-medium text-rose-700" role="alert">
              {error}
            </p>
          )}
        </div>
        <Button variant={state === "update_available" ? "outline" : "brand"} onClick={() => void download()} loading={busy}>
          <Download className="h-4 w-4" />
          {button}
        </Button>
      </div>
    </div>
  );
}

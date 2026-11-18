import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  ChevronRight,
  Copy,
  Folder,
  FolderOpen,
  HardDrive,
  Laptop,
  RefreshCw,
  Server,
  ShieldCheck,
  Usb,
  X,
} from "lucide-react";

import { Button, Modal } from "../ui";
import {
  EVIDENCE_FILE_RE,
  HIDDEN_CLIENT_DIR_RE,
  clientAgentDownloadUrls,
  clientAgentInstallCommand,
  clientAgentStatus,
  clientPathLeaf,
  consoleOrigin,
  formatClientBytes,
  joinClientPath,
  listClientDirectory,
  listClientDrives,
  type ClientAgentStatus,
  type ClientDirListing,
  type ClientDriveVolume,
} from "../../lib/clientEvidenceAgent";
import { browserFolderPickerSupport, pickBrowserFolder, type BrowserPickedFolder, pickBrowserFiles } from "../../lib/browserEvidenceShare";

export type EvidenceLocation = "server" | "client";

export type ClientSelectedFolder = {
  path: string;
  name: string;
  files: Array<{ name: string; size: number }>;
  evidenceCount: number;
  /** Chosen in the browser's own folder window: this tab serves the files (no agent needed). */
  browser?: BrowserPickedFolder;
};

/* ------------------------------------------------------------------------ */
/* Where is the evidence?                                                   */
/* ------------------------------------------------------------------------ */

export function EvidenceLocationSwitch({
  value,
  onChange,
  disabled,
  mobile,
}: {
  value: EvidenceLocation;
  onChange: (next: EvidenceLocation) => void;
  disabled?: boolean;
  mobile?: boolean;
}) {
  const options: Array<{ id: EvidenceLocation; title: string; detail: string; Icon: typeof Server }> = [
    {
      id: "server",
      title: "Forensic server",
      detail: mobile
        ? "The extraction is on a drive attached to the forensic server."
        : "The image is on a drive attached to the forensic server.",
      Icon: Server,
    },
    {
      id: "client",
      title: "This computer",
      detail: mobile
        ? "The extraction stays on the computer you are using. The server reads it over the network."
        : "The image stays on the computer you are using. The server reads it over the network.",
      Icon: Laptop,
    },
  ];
  return (
    <div className="mb-3" data-testid="evidence-location">
      <p id="evidence-location-label" className="mb-1.5 text-xs font-semibold text-ink-700">
        Where is the evidence?
      </p>
      <div role="radiogroup" aria-labelledby="evidence-location-label" className="grid gap-2 sm:grid-cols-2">
        {options.map(({ id, title, detail, Icon }) => {
          const selected = value === id;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={disabled}
              onClick={() => onChange(id)}
              className={`flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 disabled:cursor-not-allowed disabled:opacity-60 ${
                selected
                  ? "border-brand-500 bg-brand-50 ring-1 ring-brand-300"
                  : "border-ink-200 bg-white hover:border-brand-300 hover:bg-brand-50/40"
              }`}
            >
              <span
                aria-hidden="true"
                className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
                  selected ? "border-brand-600" : "border-ink-300"
                }`}
              >
                {selected ? <span className="h-2 w-2 rounded-full bg-brand-600" /> : null}
              </span>
              <span className="min-w-0">
                <span className="flex items-center gap-1.5 text-sm font-semibold text-ink-800">
                  <Icon className={`h-4 w-4 ${selected ? "text-brand-600" : "text-ink-400"}`} />
                  {title}
                </span>
                <span className="mt-0.5 block text-xs text-ink-500">{detail}</span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Agent not installed / not paired                                         */
/* ------------------------------------------------------------------------ */

export function ClientAgentSetup({
  status,
  checking,
  onRetry,
}: {
  status: ClientAgentStatus | null;
  checking: boolean;
  onRetry: () => void;
}) {
  const origin = consoleOrigin();
  const urls = clientAgentDownloadUrls();
  const command = clientAgentInstallCommand(origin);
  const [copied, setCopied] = useState(false);
  const unpaired = status?.state === "unpaired";
  const blocked = status?.state === "blocked";

  async function copyCommand() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-amber-200 bg-amber-50/70 px-3 py-3 text-xs text-amber-950" role="status">
      <div className="flex items-start gap-2">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
        <div className="min-w-0">
          <p className="text-sm font-semibold">
            {unpaired
              ? "Pair this console with the Client Evidence Agent"
              : blocked
                ? "This browser blocks this site from the Client Evidence Agent"
                : "Install the Client Evidence Agent on this computer (one time)"}
          </p>
          <p className="mt-1 text-amber-900">
            {blocked
              ? `The browser does not let ${origin} reach apps on this computer, so an installed agent cannot answer. Click the icon to the left of the address bar, open Site settings, set "Local network access" (or "Apps on this device") to Allow, then use Check again.`
              : unpaired
              ? `Directories stay hidden because the agent is running but does not accept requests from ${origin}. Run the installer again to add this console.`
              : "Directories stay hidden because nothing is answering at 127.0.0.1:9877. The agent lets the forensic server read the folder you select here. Files are opened read-only and stay on this computer; the server reads them over the network and makes no copy of the image. A failed check is remembered and is not raised as an error."}
          </p>
        </div>
      </div>
      <ol className="ml-6 list-decimal space-y-1 text-amber-950">
        <li>
          Download{" "}
          <a className="font-medium text-brand-700 underline" href={urls.installer} download>
            Install-PolaronClientEvidenceAgent.cmd
          </a>{" "}
          and{" "}
          <a className="font-medium text-brand-700 underline" href={urls.script} download>
            polaron-client-evidence-agent.ps1
          </a>{" "}
          into the same folder.
        </li>
        <li>
          Run the <span className="font-mono">.cmd</span> file, approve the administrator prompt, and enter{" "}
          <span className="font-mono font-semibold">{origin}</span> when asked for the console address.
        </li>
        <li>Keep this window open. It connects as soon as the agent answers.</li>
      </ol>
      <div>
        <p className="mb-1 text-amber-900">Or paste this into Windows PowerShell:</p>
        <div className="flex items-start gap-2">
          <code className="block min-w-0 flex-1 overflow-x-auto whitespace-pre rounded border border-amber-200 bg-white px-2 py-1.5 font-mono text-[11px] text-ink-800">
            {command}
          </code>
          <Button variant="outline" className="h-7 shrink-0 border-amber-300 bg-white px-2 text-[11px]" onClick={() => void copyCommand()}>
            <Copy className="mr-1 h-3 w-3" />
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" className="h-7 border-amber-300 bg-white px-2 text-[11px]" loading={checking} onClick={onRetry}>
          <RefreshCw className="mr-1 h-3 w-3" />
          Check again
        </Button>
        <span className="text-[11px] text-amber-800">Windows 10 or 11. Use Chrome, Edge or Firefox for this mode.</span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Choose a folder in the browser's own folder window (no agent)             */
/* ------------------------------------------------------------------------ */

export function BrowserFolderChooser({
  mobile,
  disabled,
  allowFiles = true,
  onUseFolder,
}: {
  mobile?: boolean;
  disabled?: boolean;
  /** Offer "Choose image files…" too (not for flows that need a whole case folder). */
  allowFiles?: boolean;
  onUseFolder: (folder: ClientSelectedFolder) => void;
}) {
  const support = useMemo(() => browserFolderPickerSupport(), []);
  const [reading, setReading] = useState<"folder" | "files" | null>(null);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);

  async function choose(kind: "folder" | "files") {
    // Called straight from the click: browsers open the folder/file window only for a user action.
    setError(null);
    setProgress(0);
    setReading(kind);
    try {
      const picked = kind === "files" ? await pickBrowserFiles() : await pickBrowserFolder((count) => setProgress(count));
      if (!picked) return;
      if (!picked.files.length) {
        setError(`${picked.name} contains no files. Choose the folder that holds the evidence.`);
        return;
      }
      const top = picked.files.filter((f) => !f.path.includes("/"));
      onUseFolder({
        path: picked.name,
        name: picked.name,
        files: top.map((f) => ({ name: f.path, size: f.file.size })),
        evidenceCount: picked.files.filter((f) => EVIDENCE_FILE_RE.test(f.path)).length,
        browser: picked,
      });
    } catch (e) {
      const name = e instanceof DOMException ? e.name : "";
      setError(
        name === "SecurityError" || name === "NotAllowedError"
          ? "The browser did not allow this folder (a whole drive such as E:\\, or a folder with system files). Choose the evidence folder itself, for example E:\\Case-0423, or use Choose image files… for an image that sits directly on the drive."
          : e instanceof Error
            ? e.message
            : "Could not open this computer's folder window."
      );
    } finally {
      setReading(null);
    }
  }

  const evidenceWord = mobile ? ".pas / .ufd / .ufdx / .zip" : ".E01 / .E02 / .dd / .raw";
  const filesHint = mobile
    ? "Image directly on a drive (for example E:\\phone.zip)? Use Choose image files… and select that .zip or .pas file."
    : "Image directly on a drive (for example E:\\laptop.E01)? Browsers do not accept a whole drive as a folder: use Choose image files… and select every segment (E01, E02, …).";
  return (
    <div className="space-y-2 rounded-lg border border-brand-200 bg-brand-50/50 px-3 py-3 text-xs text-ink-700" data-testid="browser-folder-chooser">
      <p className="flex items-center gap-1.5 text-sm font-semibold text-ink-800">
        <FolderOpen className="h-4 w-4 text-brand-600" />
        Choose a folder on this computer
      </p>
      <p className="text-ink-600">
        Opens this computer&apos;s own folder window with all of its drives (C:, D:, USB and mapped network drives).
        Nothing to install. Pick the folder that holds the {evidenceWord} files. The forensic server then reads the
        files through this browser tab while it extracts, parses and indexes them, and makes no copy of the image, so
        keep this tab open and the computer awake until processing finishes.
      </p>
      {support ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="brand" className="h-8 px-3 text-xs" disabled={disabled || reading !== null} loading={reading === "folder"} onClick={() => void choose("folder")}>
            <FolderOpen className="mr-1 h-3.5 w-3.5" />
            Choose folder on this computer…
          </Button>
          {allowFiles ? (
            <Button variant="outline" className="h-8 px-3 text-xs" disabled={disabled || reading !== null} loading={reading === "files"} onClick={() => void choose("files")}>
              Choose image files…
            </Button>
          ) : null}
          {reading && progress ? <span className="text-ink-500">Listing files… {progress.toLocaleString()} so far</span> : null}
          {support === "directory-picker" ? (
            <span className="text-[11px] text-ink-500">The browser asks once whether this site may view the folder&apos;s files.</span>
          ) : (
            <span className="text-[11px] text-ink-500">The browser may say &quot;upload&quot;: files are only read when the server needs them.</span>
          )}
        </div>
      ) : (
        <p className="text-amber-900">This browser cannot open a folder window. Use Chrome, Edge or Firefox, or install the Client Evidence Agent below.</p>
      )}
      {support && allowFiles ? <p className="text-[11px] text-ink-500">{filesHint}</p> : null}
      {error ? (
        <p className="rounded border border-red-200 bg-red-50 px-2 py-1.5 text-red-800" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Folder browser for this computer                                         */
/* ------------------------------------------------------------------------ */

function driveBadge(d: ClientDriveVolume): string | null {
  if (d.drive_type === "removable") return "USB";
  if (d.drive_type === "network") return "Network";
  if (d.drive_type === "fixed" && d.letter.toUpperCase() !== "C") return "HDD / SSD";
  return null;
}

function driveSort(a: ClientDriveVolume, b: ClientDriveVolume): number {
  const rank = (d: ClientDriveVolume) =>
    d.drive_type === "removable" ? 0 : d.drive_type === "fixed" && d.letter.toUpperCase() !== "C" ? 1 : d.drive_type === "network" ? 2 : 3;
  return rank(a) - rank(b) || a.letter.localeCompare(b.letter);
}

export function ClientEvidenceBrowser({
  mobile,
  disabled,
  busy,
  actionLabel = "Use this directory",
  listMaxHeight = "max-h-56",
  allowBrowserFiles = true,
  onUseFolder,
}: {
  mobile?: boolean;
  disabled?: boolean;
  busy?: boolean;
  actionLabel?: string;
  listMaxHeight?: string;
  /** Offer "Choose image files…" next to the browser folder window. */
  allowBrowserFiles?: boolean;
  onUseFolder: (folder: ClientSelectedFolder) => void;
}) {
  const [status, setStatus] = useState<ClientAgentStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [drives, setDrives] = useState<ClientDriveVolume[]>([]);
  const [arrived, setArrived] = useState<string[]>([]);
  const [listing, setListing] = useState<ClientDirListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const driveKey = useRef("");

  const check = useCallback(async (quiet = false) => {
    if (!quiet) setChecking(true);
    try {
      const next = await clientAgentStatus(2000, { force: !quiet });
      setStatus(next);
      if (next.state === "online") {
        try {
          const nextDrives = (await listClientDrives()).sort(driveSort);
          const key = nextDrives.map((d) => `${d.letter}|${d.ready ? 1 : 0}|${d.label || ""}`).join(",");
          const before = new Set(driveKey.current ? driveKey.current.split(",").map((row) => row.split("|")[0]) : []);
          if (key !== driveKey.current) {
            if (before.size) {
              const added = nextDrives.map((d) => d.letter.toUpperCase()).filter((letter) => !before.has(letter));
              if (added.length) setArrived(added);
            }
            driveKey.current = key;
            setDrives(nextDrives);
          }
          setError(null);
        } catch (e) {
          setError(e instanceof Error ? e.message : "Could not list this computer's drives.");
        }
      }
    } finally {
      if (!quiet) setChecking(false);
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  // Connect on its own once the examiner finishes installing the agent.
  // Quiet checks reuse the offline backoff, so a down agent is not probed every tick.
  useEffect(() => {
    if (status?.state === "online") return;
    const id = window.setInterval(() => void check(true), 5000);
    return () => window.clearInterval(id);
  }, [status?.state, check]);

  // Same idea as server drive changes: a disk attached while this window is open shows up on its own.
  useEffect(() => {
    if (status?.state !== "online") return;
    const id = window.setInterval(() => void check(true), 8000);
    return () => window.clearInterval(id);
  }, [status?.state, check]);

  useEffect(() => {
    if (!arrived.length) return;
    const timer = window.setTimeout(() => setArrived([]), 15_000);
    return () => window.clearTimeout(timer);
  }, [arrived]);

  const openPath = useCallback(async (path: string) => {
    const mine = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await listClientDirectory(path);
      if (mine === seq.current) setListing(next);
    } catch (e) {
      if (mine === seq.current) setError(e instanceof Error ? e.message : `Could not open ${path}.`);
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  if (!status || (checking && status.state !== "online")) {
    return (
      <div className="space-y-3">
        <BrowserFolderChooser mobile={mobile} disabled={disabled || busy} allowFiles={allowBrowserFiles} onUseFolder={onUseFolder} />
        <p className="px-1 text-xs text-ink-500">Looking for the Client Evidence Agent on this computer…</p>
      </div>
    );
  }
  if (status.state !== "online") {
    // No agent answering: the browser's own folder window works without installing anything.
    // The agent stays available for a drive list that also keeps working with this tab closed.
    return (
      <div className="space-y-3">
        <BrowserFolderChooser mobile={mobile} disabled={disabled || busy} allowFiles={allowBrowserFiles} onUseFolder={onUseFolder} />
        <details className="group rounded-lg" open={status.state === "unpaired" || status.state === "blocked"}>
          <summary className="cursor-pointer select-none px-1 text-xs font-medium text-ink-600 hover:text-ink-800">
            Optional: show this computer&apos;s drive list with the Client Evidence Agent (one-time install; keeps
            working when this tab is closed)
          </summary>
          <div className="mt-2">
            <ClientAgentSetup status={status} checking={checking} onRetry={() => void check()} />
          </div>
        </details>
      </div>
    );
  }

  const health = status.health;
  const dirs = (listing?.entries || []).filter((e) => e.kind === "dir" && !HIDDEN_CLIENT_DIR_RE.test(e.name));
  const unseen = [
    ...(listing?.entries || [])
      .filter((e) => e.kind === "dir" && HIDDEN_CLIENT_DIR_RE.test(e.name))
      .map((e) => ({ name: e.name, detail: "System folder. Left out the same way a disk that is not ready is left out of the usable list." })),
    ...(listing?.omitted || []).map((e) => ({ name: e.name, detail: e.detail || "Windows could not open this directory." })),
  ];
  const notReady = drives.filter((d) => !d.ready);
  const files = (listing?.entries || []).filter((e) => e.kind === "file");
  const evidenceCount = files.filter((f) => EVIDENCE_FILE_RE.test(f.name)).length;
  const locked = disabled || busy;
  const evidenceWord = mobile ? ".pas / .ufd / .ufdx / .zip" : ".E01 / .E02 / .dd / .raw";

  function useCurrent() {
    if (!listing) return;
    onUseFolder({
      path: listing.path,
      name: clientPathLeaf(listing.path),
      files: files.map((f) => ({ name: f.name, size: Number(f.size_bytes || 0) })),
      evidenceCount,
    });
  }

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="inline-flex items-center gap-1.5 text-emerald-800">
          <ShieldCheck className="h-3.5 w-3.5" />
          Agent connected on <span className="font-semibold">{health?.hostname || "this computer"}</span>
          {health?.version ? <span className="text-emerald-700/80">v{health.version}</span> : null}
        </span>
        <Button variant="outline" className="h-7 px-2 text-[11px]" loading={checking} disabled={locked} onClick={() => void check()}>
          <RefreshCw className="mr-1 h-3 w-3" />
          Refresh drives
        </Button>
      </div>

      {!health?.relay_supported ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          {health?.lan_bound === false
            ? "The agent is listening on this computer only, so the forensic server cannot reach it. "
            : "This agent version works on the office network only. "}
          Install the latest agent (steps above the drive list when it is missing) to use this computer from home or
          another site: it connects out to the Polaron server instead.
        </div>
      ) : null}

      {arrived.length ? (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-900" role="status">
          New drive detected: {arrived.map((letter) => `${letter}:`).join(", ")}. Select it to browse.
        </div>
      ) : null}

      {notReady.length ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900" role="status">
          {notReady.map((d) => `${d.letter.length === 1 ? `${d.letter}:` : d.root} is visible but not ready (locked, ejected or empty)`).join(". ")}.
        </div>
      ) : null}

      {drives.length ? (
        <div className="flex flex-wrap gap-1.5" aria-label="Drives on this computer">
          {drives.map((d) => {
            const badge = driveBadge(d);
            const active = Boolean(listing?.path && listing.path.toUpperCase().startsWith(d.root.toUpperCase()));
            const Icon = d.drive_type === "removable" ? Usb : HardDrive;
            return (
              <button
                key={d.root}
                type="button"
                disabled={locked || !d.ready}
                onClick={() => void openPath(d.root)}
                title={d.ready ? `Open ${d.root}` : "Drive is not ready (locked, ejected or empty)"}
                className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50 ${
                  active ? "border-brand-500 bg-brand-50 font-semibold text-brand-900" : "border-ink-300 bg-white text-ink-700 hover:bg-brand-50/60"
                }`}
              >
                <Icon className="h-3.5 w-3.5 text-brand-600" />
                {d.letter.length === 1 ? `${d.letter}:` : d.root}
                {d.label ? <span className="text-ink-500">{d.label}</span> : null}
                {badge ? <span className="text-brand-800">{badge}</span> : null}
                {d.ready && d.size_bytes ? <span className="text-ink-400">{formatClientBytes(d.size_bytes)}</span> : null}
              </button>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-ink-500">No drives reported by the agent yet.</p>
      )}

      {error ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">{error}</div>
      ) : null}

      {listing ? (
        <div className="rounded-md border border-ink-200 bg-white">
          <div className="flex flex-wrap items-center gap-2 border-b border-ink-100 px-3 py-2">
            <p className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-800" title={listing.path}>
              {listing.path}
            </p>
            {listing.parent ? (
              <Button variant="ghost" className="h-7 px-2 text-xs" disabled={locked || loading} onClick={() => void openPath(listing.parent as string)}>
                Up
              </Button>
            ) : null}
            <Button variant="brand" className="h-7 px-2.5 text-xs" disabled={locked || loading} onClick={useCurrent}>
              {actionLabel}
              {evidenceCount ? ` (${evidenceCount} evidence file${evidenceCount === 1 ? "" : "s"})` : ""}
            </Button>
          </div>
          {loading ? (
            <p className="px-3 py-3 text-xs text-ink-400">Reading directory…</p>
          ) : (
            <ul className={`${listMaxHeight} divide-y divide-ink-50 overflow-y-auto`}>
              {dirs.map((entry) => (
                <li key={`dir-${entry.name}`}>
                  <button
                    type="button"
                    disabled={locked}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-brand-50/60"
                    onClick={() => void openPath(joinClientPath(listing.path, entry.name))}
                  >
                    <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
                    <span className="truncate font-medium text-ink-800">{entry.name}</span>
                    <ChevronRight className="ml-auto h-3 w-3 shrink-0 text-ink-300" />
                  </button>
                </li>
              ))}
              {files.slice(0, 200).map((entry) => (
                <li key={`file-${entry.name}`} className="flex items-center gap-2 px-3 py-1.5 text-xs text-ink-600">
                  <FolderOpen className="h-3.5 w-3.5 shrink-0 text-ink-300" />
                  <span className="min-w-0 flex-1 truncate font-mono">{entry.name}</span>
                  {EVIDENCE_FILE_RE.test(entry.name) ? (
                    <span className="shrink-0 rounded bg-brand-100 px-1 text-[10px] text-brand-800">evidence</span>
                  ) : null}
                  <span className="shrink-0 text-ink-400">{formatClientBytes(entry.size_bytes ?? null)}</span>
                </li>
              ))}
              {!dirs.length && !files.length ? <li className="px-3 py-2 text-xs text-ink-400">Directory is empty.</li> : null}
            </ul>
          )}
          {!loading && unseen.length ? (
            <div className="border-t border-amber-100 bg-amber-50/70 px-3 py-1.5 text-[11px] text-amber-950" role="status">
              <p className="font-semibold">Directories not listed ({unseen.length})</p>
              <ul className="mt-1 space-y-0.5">
                {unseen.map((entry) => (
                  <li key={entry.name}>
                    <span className="font-medium">{entry.name}</span> — {entry.detail}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {listing.truncated || files.length > 200 ? (
            <p className="border-t border-ink-100 px-3 py-1.5 text-[11px] text-ink-400">
              Showing the first entries only. Every file in the folder is still processed.
            </p>
          ) : null}
          {!loading && !evidenceCount ? (
            <p className="border-t border-ink-100 px-3 py-1.5 text-[11px] text-ink-500">
              No {evidenceWord} files directly in this folder. Subfolders are included when you use it.
            </p>
          ) : null}
        </div>
      ) : drives.length ? (
        <p className="text-xs text-ink-500">Open a drive, go to the evidence directory, then choose {actionLabel}.</p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Popup used by "Browse this computer" / "Select extraction directory…"     */
/* ------------------------------------------------------------------------ */

export function ClientBrowseFolderDialog({
  open,
  mobile,
  onClose,
  onConfirm,
}: {
  open: boolean;
  mobile?: boolean;
  onClose: () => void;
  onConfirm: (folders: ClientSelectedFolder[]) => void;
}) {
  const [folders, setFolders] = useState<ClientSelectedFolder[]>([]);

  useEffect(() => {
    if (!open) setFolders([]);
  }, [open]);

  function addFolder(next: ClientSelectedFolder) {
    setFolders((prev) =>
      prev.some((f) => f.path.toLowerCase() === next.path.toLowerCase() && Boolean(f.browser) === Boolean(next.browser))
        ? prev
        : [...prev, next],
    );
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Select evidence directory on this computer"
      size="lg"
      draggable
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="brand" disabled={!folders.length} onClick={() => onConfirm(folders)}>
            Start extraction ({folders.length} {folders.length === 1 ? "directory" : "directories"})
          </Button>
        </>
      }
    >
      <div className="space-y-4 text-sm text-ink-700">
        <p className="text-ink-600">
          {mobile
            ? "Choose the extraction directory that holds the .pas / .ufd / .ufdx / .zip files."
            : "Choose the directory that holds the .E01 / .E02 image set."}{" "}
          It stays on this computer. The forensic server reads it in place over the network, so keep this computer on
          and connected until extraction finishes.
        </p>
        {open ? <ClientEvidenceBrowser mobile={mobile} actionLabel="Use this directory" listMaxHeight="max-h-44" onUseFolder={addFolder} /> : null}
        {folders.some((f) => f.browser) ? (
          <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900" role="status">
            Folders chosen in this browser are read through this tab: keep it open until extraction and indexing
            finish. Closing or reloading it pauses processing until you reconnect the folder from the job page.
          </p>
        ) : null}
        <div className="rounded-xl border border-ink-300 bg-white p-3">
          <p className="mb-2 text-xs font-semibold text-ink-700">Selected directories ({folders.length})</p>
          {folders.length ? (
            <ul className="max-h-40 space-y-1 overflow-y-auto" aria-label="Selected directories on this computer">
              {folders.map((folder, index) => (
                <li key={folder.path} className="flex items-center gap-2 rounded-lg border border-ink-200 px-2 py-1.5">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-semibold">
                      {index + 1}. {folder.name}
                    </p>
                    <p className="truncate font-mono text-[10px] text-ink-500">
                      {folder.browser
                        ? `${folder.name} · chosen in this browser · ${folder.browser.files.length.toLocaleString()} file${folder.browser.files.length === 1 ? "" : "s"}`
                        : folder.path}{" "}
                      · {folder.evidenceCount} evidence file{folder.evidenceCount === 1 ? "" : "s"}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setFolders((prev) => prev.filter((f) => f.path !== folder.path))}
                    className="rounded p-1 text-ink-400 hover:text-red-600"
                    aria-label={`Remove ${folder.name}`}
                  >
                    <X className="h-4 w-4" />
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="rounded-lg border border-dashed border-ink-300 px-4 py-4 text-center text-xs text-ink-500">
              Choose a folder on this computer, or open a drive in the agent&apos;s list and choose <strong>Use this directory</strong>.
            </p>
          )}
        </div>
        <p className="flex items-center gap-1.5 text-xs text-ink-500">
          <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" />
          Nothing is copied in advance. The server reads the files in place, read-only, and stops if any of them change.
        </p>
      </div>
    </Modal>
  );
}

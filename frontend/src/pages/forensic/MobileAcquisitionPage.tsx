import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Cable,
  FileCheck2,
  FolderOpen,
  Info,
  Laptop,
  Loader2,
  RefreshCw,
  Server,
  ShieldAlert,
  ShieldCheck,
  Smartphone,
  StopCircle,
  Trash2,
  Wand2,
  XCircle,
} from "lucide-react";
import { Badge, Button, Card, Field, Input, Modal, PageHeader, Select, Spinner } from "../../components/ui";
import { ClientAgentSetup, ClientEvidenceBrowser, type ClientSelectedFolder } from "../../components/forensic/ClientEvidenceBrowser";
import { ClientSetupPanel, clientSetupState } from "../../components/forensic/ClientSetupPanel";
import { getClientKitInfo } from "../../lib/clientKit";
import { deviceMatchesPlatform, noPhoneText } from "../../lib/phonePlatform";
import { clientAgentStatus, type ClientAgentStatus } from "../../lib/clientEvidenceAgent";
import {
  createClientStream,
  dropClientStream,
  followClientStream,
  streamProgressLine,
  streamStartFields,
} from "../../lib/clientStream";
import {
  ClientAgentRequiredError,
  DEFAULT_CLIENT_CASE_ROOT,
  clientRunSealed,
  copyClientRunToServer,
  deviceSite,
  findClientCaseDir,
  formatImportBytes,
  resolveSiteIdentity,
  resumeClientImport,
  type ClientImportScope,
  type SiteIdentity,
} from "../../lib/clientMobileImport";
import { acquisitionApi, forensicApi } from "../../lib/forensicApi";
import { streamAcquisition } from "../../lib/acquisitionSse";
import {
  ensureHostDriveHelper,
  getHostAcquisitionAdapters,
  getHostAcquisitionDevices,
  getHostAcquisitionPreview,
  hostDriveHelperStartHint,
  startHostAcquisitionRun,
  getHostAcquisitionJob,
  listHostAcquisitionJobs,
  cancelHostAcquisitionJob,
  removeHostAcquisitionJob,
  isHostAcquisitionJobId,
  friendlyMobileAcquireError,
  startHostDriveAgentFromBrowser,
  hostMobileToAcquisitionDevice,
  deviceNeedsPhoneAction,
  phoneActionLabel,
  listHostMobileDevices,
  mobileAttachHostLabel,
  mobileKindLabel,
  isUsbCollectionBusyError,
  isBrowserOnOfficeHost,
  getHostStreamStatus,
  type HostStreamStatus,
  type LocalHelperState,
} from "../../lib/hostDriveHelper";
import { UsbCollectionBusyBanner } from "../../components/forensic/StopUsbCollectionButton";
import { useToast } from "../../lib/toast";
import { mobilePlatformService, mobileUiBasePath } from "../../lib/serviceMode";
import type {
  AcquisitionAdapter,
  AcquisitionDeviceProfile,
  AcquisitionPreview,
  AcquisitionResult,
  AcquisitionRunDetail,
  AcquisitionRunSummary,
  ClientImportState,
  CollectionMethod,
  DetectedDevice,
} from "../../lib/types/forensic";

type AuthorityForm = {
  case_id: string;
  evidence_id: string;
  legal_authority: string;
  case_root: string;
  cable_adapter_asset_id: string;
  device_condition: string;
  backup_password: string;
  examiner_notes: string;
  network_isolated: boolean;
  legal_ack: boolean;
  cloud_mail_provider: string;
  cloud_mail_address: string;
  cloud_mail_secret: string;
  // Android WhatsApp decrypt agent (device-modifying; off by default).
  wa_generate_backup: boolean;
  wa_allow_device_modification: boolean;
  wa_64_digit_key: string;
};

function ownerAgentForFamily(family?: string | null): { id: string; label: string } | null {
  const raw = String(family || "").toLowerCase();
  if (raw.includes("ios") || raw.includes("iphone") || raw.includes("ipad")) {
    return { id: "iosagent", label: "iOS Agent" };
  }
  if (raw.includes("android")) {
    return { id: "androidagent", label: "Android Agent" };
  }
  return null;
}

function slugPart(value: string, max = 12): string {
  return value.replace(/[^A-Za-z0-9]+/g, "").slice(0, max).toUpperCase() || "DEV";
}

function formatCollectedBytes(bytes: number): string {
  const n = Number(bytes) || 0;
  if (n >= 1073741824) return `${(n / 1073741824).toFixed(2)} GB`;
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${Math.max(0, Math.round(n))} B`;
}

/** Exhibit-style evidence id from IMEI / serial / UDID — never invents legal authority. */
function evidenceIdFromProfile(profile: AcquisitionDeviceProfile): string {
  const raw = profile.imei || profile.serial || profile.udid || "";
  const digits = raw.replace(/\D/g, "");
  if (digits.length >= 6) return `E-${digits.slice(-6)}`;
  if (raw) return `E-${slugPart(raw, 8)}`;
  return "";
}

function deviceConditionFromProfile(profile: AcquisitionDeviceProfile): string {
  const parts: string[] = ["Powered on, connected"];
  if (profile.lock_state) parts.push(profile.lock_state.replace(/_/g, " "));
  if (profile.battery_percent != null) parts.push(`battery ${profile.battery_percent}%`);
  if (profile.pairing_trusted) parts.push("trusted/paired");
  if (profile.usb_debugging_authorized) parts.push("USB debugging authorised");
  if (profile.sim_present === true) parts.push("SIM present");
  else if (profile.sim_present === false) parts.push("no SIM");
  if (profile.encryption_state) {
    parts.push(`encryption ${profile.encryption_state.replace(/_/g, " ")}`);
  }
  if (profile.rooted_or_jailbroken === true) parts.push("rooted/jailbroken indicated");
  return parts.join(", ");
}

function examinerNotesFromProfile(profile: AcquisitionDeviceProfile): string {
  const lines = [
    `Device: ${profile.device_label || `${profile.manufacturer} ${profile.model}`.trim()}`,
    profile.os_family
      ? `OS: ${profile.os_family} ${profile.os_version || ""}`.trim()
      : "",
    profile.build_id ? `Build: ${profile.build_id}` : "",
    profile.udid ? `UDID: ${profile.udid}` : "",
    profile.serial ? `Serial: ${profile.serial}` : "",
    profile.imei ? `IMEI: ${profile.imei}` : "",
    profile.iccid ? `ICCID: ${profile.iccid}` : "",
    profile.imsi ? `IMSI: ${profile.imsi}` : "",
    profile.chipset ? `Chipset: ${profile.chipset}` : "",
    profile.connection_mode ? `Connection: ${profile.connection_mode}` : "",
    profile.required_cable ? `Required cable: ${profile.required_cable}` : "",
    ...(profile.observations ?? []),
  ].filter(Boolean);
  return lines.join("\n");
}

/**
 * Fill examiner form fields that can be derived from the handset profile.
 * Legal authority, backup password, and cable asset ID stay examiner-entered.
 */
function applyDeviceAutofill(
  form: AuthorityForm,
  profile: AcquisitionDeviceProfile,
  opts: { force?: boolean } = {},
): AuthorityForm {
  const force = Boolean(opts.force);
  const next: AuthorityForm = { ...form };
  const setIfEmpty = (key: keyof AuthorityForm, value: string) => {
    if (!value) return;
    if (force || !String(next[key] ?? "").trim()) {
      (next as Record<string, unknown>)[key] = value;
    }
  };

  const ymd = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  setIfEmpty(
    "case_id",
    `CASE-${ymd}-${slugPart(profile.model || profile.manufacturer || profile.os_family, 10)}`,
  );
  setIfEmpty("evidence_id", evidenceIdFromProfile(profile));
  setIfEmpty("case_root", "/evidence/cases");
  setIfEmpty("device_condition", deviceConditionFromProfile(profile));
  setIfEmpty("examiner_notes", examinerNotesFromProfile(profile));

  if (profile.network_isolated != null && (force || !form.network_isolated)) {
    next.network_isolated = Boolean(profile.network_isolated);
  }
  return next;
}

type Step = "connect" | "identify" | "authorise" | "collect" | "complete";

const STEPS: { id: Step; label: string; hint: string }[] = [
  { id: "connect", label: "Connect", hint: "Detect the device" },
  { id: "identify", label: "Identify", hint: "Profile and capability" },
  { id: "authorise", label: "Authorise", hint: "Authority and method" },
  { id: "collect", label: "Collect", hint: "Acquire and hash" },
  { id: "complete", label: "Complete", hint: "Package and verify" },
];

// Objectives map to the minimum method that can satisfy them. This is what
// drives least-intrusive-sufficient selection on the server: the examiner states
// what they need to prove, not which method to run.
const OBJECTIVES: { id: string; label: string; requires: CollectionMethod[]; note: string }[] = [
  {
    id: "communications",
    label: "Messages, calls and contacts",
    requires: ["logical", "backup", "advanced_logical", "file_system", "full_file_system"],
    note: "Needs USB debugging (tap Allow). File System collects shared storage, WhatsApp media and encrypted backups. Private chats and keys are collected when existing root or app run-as access permits it, including Android user profiles. You can also import a WhatsApp chat export with media. Encrypted backups require a matching key; an Android backup prompt does not guarantee private WhatsApp data.",
  },
  {
    id: "media",
    label: "Photos, video, audio and downloads",
    requires: ["logical", "advanced_logical", "file_system", "full_file_system"],
    note: "DCIM, Pictures, Movies, Download, WhatsApp/Telegram media under Android/media, and recycle-bin folders on shared storage.",
  },
  {
    id: "app_data",
    label: "Application data and databases",
    requires: ["logical", "advanced_logical", "file_system", "full_file_system"],
    note: "Android/media and Android/data on shared storage (streamed via ADB tar to avoid Windows path limits), plus adb backup/content providers when USB debugging is authorised.",
  },
  {
    id: "deleted",
    label: "Deleted records and journals",
    requires: ["logical", "backup", "advanced_logical", "file_system", "full_file_system", "physical"],
    note: "Copies recycle/.trashed folders and SQLite WAL journals that are still on shared storage. Unallocated flash recovery needs root or a vendor physical image.",
  },
  {
    id: "system_state",
    label: "System state, usage and location history",
    requires: ["logical", "advanced_logical", "file_system", "full_file_system"],
    note: "Usage and log files that the OS exposes on shared storage or via adb. Deeper system domains need backup or filesystem access.",
  },
  {
    id: "credentials",
    label: "Keychain / keystore material",
    requires: ["full_file_system"],
    note: "Not available over MTP/file-transfer. Needs an encrypted iOS backup password or a full-filesystem method.",
  },
];

function labelOf(method: string | null | undefined): string {
  if (!method || method === "none") return "None";
  return method.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function resultErrorList(result: AcquisitionResult | null | undefined): string[] {
  if (!result) return [];
  const bogus = new Set(["acquisition_record", "errors", "ok", "run_name", ""]);
  const raw = (result as { errors?: unknown; error?: unknown }).errors;
  const out: string[] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const s = String(item || "").trim();
      if (s && !bogus.has(s)) out.push(s);
    }
  }
  const single = String((result as { error?: string }).error || "").trim();
  if (single && !bogus.has(single) && !out.includes(single)) out.unshift(single);
  return out;
}

function exportPackagesOf(result: AcquisitionResult | null | undefined): Record<string, string> {
  if (!result) return {};
  const pick = (raw: unknown): Record<string, string> => {
    if (!raw || typeof raw !== "object") return {};
    const obj = raw as { packages?: Record<string, unknown> } & Record<string, unknown>;
    const fromNested = obj.packages && typeof obj.packages === "object" ? obj.packages : obj;
    const out: Record<string, string> = {};
    for (const ext of ["zip", "ufdx", "ufd", "pas"]) {
      const value = fromNested[ext];
      if (typeof value === "string" && value.trim()) out[ext] = value;
    }
    return out;
  };
  const nested = pick(result.evidence_package?.export_packages);
  if (Object.keys(nested).length) return nested;
  const top = pick((result as { export_packages?: unknown }).export_packages);
  if (Object.keys(top).length) return top;
  const paths = ((result as { paths?: Record<string, string> }).paths || {}) as Record<string, string>;
  const fromPaths: Record<string, string> = {};
  for (const ext of ["zip", "ufdx", "ufd", "pas"]) {
    const value = paths[`export_${ext}`] || paths[ext];
    if (typeof value === "string" && value.trim()) fromPaths[ext] = value;
  }
  return fromPaths;
}

function contentInventoryOf(result: AcquisitionResult | null | undefined) {
  const pkgInv = result?.evidence_package?.content_inventory;
  const top = (result as { content_inventory?: unknown })?.content_inventory;
  return (pkgInv || top || {}) as {
    summary?: string;
    counts?: Record<string, number>;
    totals?: Record<string, number>;
    gaps?: string[];
    categories?: Record<string, string[]>;
  };
}

function filesCollected(result: AcquisitionResult | null | undefined): number {
  if (!result) return 0;
  const pkg = result.evidence_package as { file_count?: number; extraction_data?: string[] } | undefined;
  if (typeof pkg?.file_count === "number" && pkg.file_count > 0) return pkg.file_count;
  const rec = result.acquisition_record as { file_count?: number } | undefined;
  // file_count 0 with a large output_size is a stale/empty record — do not treat 0 as truth.
  if (typeof rec?.file_count === "number" && rec.file_count > 0) return rec.file_count;
  const inv = contentInventoryOf(result);
  const fromInv = Number(
    inv?.totals?.files ||
      inv?.counts?.files ||
      inv?.counts?.total ||
      inv?.counts?.file_count ||
      0,
  );
  if (fromInv > 0) return fromInv;
  const top = Number((result as { file_count?: number; files_seen?: number }).file_count
    || (result as { files_seen?: number }).files_seen
    || 0);
  if (top > 0) return top;
  return pkg?.extraction_data?.length ?? 0;
}

function collectionSucceeded(result: AcquisitionResult | null | undefined): boolean {
  if (!result) return false;
  if (isIncompleteIosBackup(result)) return false;
  if (result.ok === false) return false;
  if (result.evidence_package && result.evidence_package.complete === false) return false;
  if (filesCollected(result) > 0) return true;
  return result.ok === true;
}

function isIncompleteIosBackup(result: AcquisitionResult | null | undefined): boolean {
  if (!result) return false;
  const blob = [
    result.error,
    result.stage_reached,
    ...(result.errors || []),
    result.evidence_package?.complete === false ? "incomplete" : "",
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return (
    blob.includes("ios_backup_incomplete") ||
    blob.includes("missing_manifest") ||
    blob.includes("no_manifest") ||
    blob.includes("manifest.db") ||
    blob.includes("usb backup session dropped") ||
    blob.includes("connectionterminated") ||
    blob.includes("connection terminated") ||
    blob.includes("usb_connection_dropped")
  );
}

function isCancelledAcquisition(
  detail: { status?: string; error?: string | null; result?: AcquisitionResult | null } | null | undefined,
): boolean {
  const status = String(detail?.status || "").toLowerCase();
  if (status === "cancelled" || status === "cancelling") return true;
  const result = detail?.result as { stage_reached?: string; error?: string; errors?: string[] } | undefined;
  const stage = String(result?.stage_reached || "").toLowerCase();
  if (stage === "cancelled") return true;
  const blob = `${detail?.error || ""} ${result?.error || ""} ${(result?.errors || []).join(" ")}`.toLowerCase();
  return blob.includes("cancelled by examiner") || blob.includes("stopped by examiner");
}

/** Host jobs write an ok:false / stage=acquire placeholder as soon as PowerShell starts. That is not a finished collection. */
function isInFlightHostResult(
  status: string | undefined,
  result: { ok?: boolean; stage_reached?: string } | null | undefined,
): boolean {
  const st = String(status || "").toLowerCase();
  if (st === "running" || st === "queued" || st === "starting" || !st) return true;
  if (st === "cancelled" || st === "cancelling") return false;
  const stage = String(result?.stage_reached || "").toLowerCase();
  return result?.ok === false && (stage === "acquire" || stage === "starting");
}

function StepRail({ current }: { current: Step }) {
  const index = STEPS.findIndex((s) => s.id === current);
  return (
    <ol className="mb-6 flex flex-wrap gap-2">
      {STEPS.map((step, i) => {
        const state = i < index ? "done" : i === index ? "active" : "todo";
        return (
          <li
            key={step.id}
            className={[
              "flex items-center gap-2 rounded-lg border px-3 py-2 text-sm",
              state === "active"
                ? "border-brand-300 bg-brand-50 text-brand-800"
                : state === "done"
                  ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                  : "border-ink-100 bg-white text-ink-400",
            ].join(" ")}
          >
            {state === "done" ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : (
              <span className="flex h-5 w-5 items-center justify-center rounded-full border text-xs">
                {i + 1}
              </span>
            )}
            <span className="font-medium">{step.label}</span>
            <span className="hidden text-xs opacity-70 sm:inline">{step.hint}</span>
          </li>
        );
      })}
    </ol>
  );
}

function Notice({
  tone,
  title,
  children,
}: {
  tone: "info" | "warn" | "error" | "ok";
  title: string;
  children?: React.ReactNode;
}) {
  const styles = {
    info: "border-ink-200 bg-ink-50 text-ink-700",
    warn: "border-amber-200 bg-amber-50 text-amber-900",
    error: "border-rose-200 bg-rose-50 text-rose-900",
    ok: "border-emerald-200 bg-emerald-50 text-emerald-900",
  }[tone];
  const Icon = { info: Info, warn: AlertTriangle, error: XCircle, ok: CheckCircle2 }[tone];
  return (
    <div className={`rounded-lg border px-3 py-2.5 text-sm ${styles}`}>
      <div className="flex items-start gap-2">
        <Icon className="mt-0.5 h-4 w-4 shrink-0" />
        <div className="min-w-0">
          <p className="font-medium">{title}</p>
          {children ? <div className="mt-1 space-y-1 opacity-90">{children}</div> : null}
        </div>
      </div>
    </div>
  );
}

const LIVE_RUN_KEY = "aetheris.mobileAcquisition.live";

type LiveRunSnapshot = {
  run_id: string;
  case_id?: string;
  case_root?: string;
  adapter?: string;
  device_id?: string;
  os_family?: string;
  /** V1.27.0: phone on this (client) PC — image in client_case_root, then copied to the server. */
  client_mode?: boolean;
  client_case_root?: string;
  scope?: string;
  import_id?: string;
  /** V1.31.14: upload session id — the collection is sent to the server while it runs (no token here). */
  stream_id?: string;
};

function persistLiveRun(snapshot: LiveRunSnapshot) {
  try {
    // Rewrites of the same run keep fields they do not mention (client mode, copy id).
    const prev = readLiveRun();
    const defined = Object.fromEntries(
      Object.entries(snapshot).filter(([, value]) => value !== undefined),
    ) as LiveRunSnapshot;
    const merged = prev && prev.run_id === snapshot.run_id ? { ...prev, ...defined } : snapshot;
    const raw = JSON.stringify(merged);
    sessionStorage.setItem(LIVE_RUN_KEY, raw);
    localStorage.setItem(LIVE_RUN_KEY, raw);
  } catch {
    /* private mode */
  }
}

function readLiveRun(): LiveRunSnapshot | null {
  try {
    const raw = sessionStorage.getItem(LIVE_RUN_KEY) || localStorage.getItem(LIVE_RUN_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as LiveRunSnapshot;
    if (!parsed?.run_id) return null;
    return parsed;
  } catch {
    return null;
  }
}

function clearLiveRun() {
  try {
    sessionStorage.removeItem(LIVE_RUN_KEY);
    localStorage.removeItem(LIVE_RUN_KEY);
  } catch {
    /* ignore */
  }
}

export function MobileAcquisitionPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const toast = useToast();
  const fixedPlatform = mobilePlatformService();
  const fixedPlatformLabel = fixedPlatform === "ios" ? "iOS" : fixedPlatform === "android" ? "Android" : null;
  const mobileBase = mobileUiBasePath(fixedPlatform);

  const [step, setStep] = useState<Step>("connect");
  const [adapters, setAdapters] = useState<AcquisitionAdapter[]>([]);
  const [devices, setDevices] = useState<DetectedDevice[]>([]);
  const [detectWarnings, setDetectWarnings] = useState<string[]>([]);
  const [scanning, setScanning] = useState(false);
  // V1.31.13: when the phone list was last read, for "nothing connected (checked ...)".
  const [lastScanAt, setLastScanAt] = useState<Date | null>(null);
  // V1.31.8: the Polaron helper on this computer (null = not running) and the version the server has.
  const [localHelper, setLocalHelper] = useState<LocalHelperState | null>(null);
  const [helperOnline, setHelperOnline] = useState<boolean | null>(null);
  const [serverHelperVersion, setServerHelperVersion] = useState("");

  const [selected, setSelected] = useState<DetectedDevice | null>(null);
  const [preview, setPreview] = useState<AcquisitionPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const [objectives, setObjectives] = useState<string[]>(["communications", "media", "app_data"]);
  const [methodOverride, setMethodOverride] = useState<string>("full_file_system");
  const [form, setForm] = useState<AuthorityForm>({
    case_id: "",
    evidence_id: "",
    legal_authority: "",
    case_root: "/evidence/cases",
    cable_adapter_asset_id: "",
    device_condition: "",
    backup_password: "",
    examiner_notes: "",
    network_isolated: false,
    legal_ack: false,
    cloud_mail_provider: "gmail",
    cloud_mail_address: "",
    cloud_mail_secret: "",
    wa_generate_backup: false,
    wa_allow_device_modification: false,
    wa_64_digit_key: "",
  });

  const [running, setRunning] = useState(false);
  const [starting, setStarting] = useState(false);

  const [result, setResult] = useState<AcquisitionResult | null>(null);
  const [run, setRun] = useState<AcquisitionRunSummary | null>(null);
  const [waKeyItems, setWaKeyItems] = useState<Array<{ directory: string; decrypted: boolean; key_available: boolean; key_hex?: string }>>([]);
  const [waKeyRevealed, setWaKeyRevealed] = useState(false);
  const [waKeyLoading, setWaKeyLoading] = useState(false);
  const [waKeyError, setWaKeyError] = useState("");

  const [startingAnalysis, setStartingAnalysis] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [usbBusy, setUsbBusy] = useState(false);
  const streamAbort = useRef<AbortController | null>(null);
  const finishingRef = useRef(false);
  const lastFinishToastRef = useRef("");
  const appliedTerminalRef = useRef("");
  const liveBusyRef = useRef(false);
  const autoProcessRef = useRef(false);
  const startAnalysisRef = useRef<(override?: AcquisitionResult | null) => Promise<void>>(async () => {});
  const formRef = useRef(form);
  const scanInFlightRef = useRef(false);
  const lastDeviceKeyRef = useRef("");
  const runRef = useRef(run);
  const stepRef = useRef(step);
  formRef.current = form;
  runRef.current = run;
  stepRef.current = step;

  // V1.27.0 — phone attached to this (client) PC: image created here, then copied to the server.
  const [site, setSite] = useState<SiteIdentity | null>(null);
  const [clientMode, setClientModeState] = useState(false);
  const [clientCaseRoot, setClientCaseRoot] = useState<string>(DEFAULT_CLIENT_CASE_ROOT);
  const [importScope, setImportScope] = useState<ClientImportScope>("full");
  const [clientImport, setClientImport] = useState<ClientImportState | null>(null);
  const [clientImportError, setClientImportError] = useState("");
  const [clientCollected, setClientCollected] = useState(false);
  const [clientAgentMissing, setClientAgentMissing] = useState<ClientAgentStatus | null>(null);
  // V1.31.14: the collection on this computer is sent to the forensic server while it runs.
  const [keepClientCopy, setKeepClientCopy] = useState(false);
  const [streamState, setStreamState] = useState<ClientImportState | null>(null);
  const [streamUploader, setStreamUploader] = useState<HostStreamStatus | null>(null);
  const [streamActive, setStreamActive] = useState(false);
  const streamIdRef = useRef("");
  const streamFailedRef = useRef(false);
  const [pullOpen, setPullOpen] = useState(false);
  const [pulling, setPulling] = useState(false);
  const siteRef = useRef<SiteIdentity | null>(null);
  const clientModeRef = useRef(false);
  const clientCaseRootRef = useRef(clientCaseRoot);
  const importScopeRef = useRef(importScope);
  const clientFlightRef = useRef<Promise<boolean> | null>(null);
  const clientCopyBlockedRef = useRef(false);
  const importAbortRef = useRef<AbortController | null>(null);
  siteRef.current = site;
  clientCaseRootRef.current = clientCaseRoot;
  importScopeRef.current = importScope;
  const setClientMode = (value: boolean) => {
    clientModeRef.current = value;
    setClientModeState(value);
  };

  /** Apply a finished run (live or recovered from disk) to the Complete step. */
  const applyFinishedRun = useCallback(
    (detail: AcquisitionRunDetail, toastMsg?: string) => {
      finishingRef.current = true;
      streamAbort.current?.abort();
      setStarting(false);
      setRunning(false);
      const result = detail.result as AcquisitionResult | undefined;
      const cancelled = isCancelledAcquisition(detail);
      if (clientModeRef.current && result && !result.client_import && !cancelled && collectionSucceeded(result)) {
        // Sealed on this PC, but the forensic server copy does not exist yet. Its paths
        // name this computer, so it must not reach "Start analysis". The copy step finishes it.
        finishingRef.current = false;
        setClientCollected(true);
        // V1.31.5: the collection itself IS finished. Show that, instead of leaving the last
        // in-flight numbers (for example "95%, acquire") on screen while only the copy is left.
        const rec = (result.acquisition_record || {}) as { output_size?: number; file_count?: number };
        setRun((prev) =>
          prev
            ? {
                ...prev,
                progress: {
                  ...prev.progress,
                  stage: "complete",
                  category: "Complete",
                  item: "",
                  detail: streamIdRef.current && !streamFailedRef.current
                    ? "Collection finished on this computer. Sending its last files to the forensic server."
                    : "Image created on this computer. What is left: copy it to the forensic server for processing.",
                  progress_pct: 100,
                  bytes_done: Math.max(Number(rec.output_size || 0), Number(prev.progress?.bytes_done || 0)),
                  files_seen: Math.max(Number(rec.file_count || 0), Number(prev.progress?.files_seen || 0)),
                  updated_utc: new Date().toISOString(),
                },
              }
            : prev,
        );
        return false;
      }
      const collected = filesCollected(result);
      const incomingId = String(detail.run_id || "");
      const live = runRef.current;
      const liveId = String(live?.run_id || "");
      const liveBusy =
        liveBusyRef.current ||
        Boolean(
          live &&
            (live.status === "running" ||
              liveId === "pending" ||
              live.progress?.item === "starting" ||
              live.progress?.item === "collecting"),
        );
      if (liveBusy && incomingId && liveId && liveId !== "pending" && incomingId !== liveId) {
        const sameCase =
          String(detail.case_id || "").trim().toLowerCase() ===
          String(live?.case_id || formRef.current.case_id || "").trim().toLowerCase();
        const incomingDone =
          !isInFlightHostResult(detail.status, result) &&
          (collectionSucceeded(result) || filesCollected(result) > 0);
        if (!(sameCase && incomingDone)) return false;
      }
      if (liveBusy && (liveId === "pending" || !incomingId) && cancelled && collected <= 0) {
        return false;
      }
      if (!cancelled && isInFlightHostResult(detail.status, result) && collected <= 0) {
        finishingRef.current = false;
        return false;
      }
      if (cancelled && collected <= 0) {
        if (liveBusy && liveId && liveId !== "pending" && incomingId && incomingId !== liveId) {
          return false;
        }
        liveBusyRef.current = false;
        appliedTerminalRef.current = String(detail.run_id || "");
        finishingRef.current = false;
        clearLiveRun();
        setRun(null);
        setResult(null);
        setUsbBusy(false);
        setStep((prev) => (prev === "collect" || prev === "complete" ? "authorise" : prev));
        const key = `${detail.run_id}:cancelled-empty`;
        if (lastFinishToastRef.current !== key) {
          lastFinishToastRef.current = key;
          toast.info("USB collection stopped. You can start a new collection.");
        }
        return true;
      }
      if (!result) {
        finishingRef.current = false;
        return false;
      }
      const wasLive = liveBusy;
      liveBusyRef.current = false;
      appliedTerminalRef.current = String(detail.run_id || "");
      setRun(detail);
      setResult(result);
      setStep("complete");
      if (
        wasLive &&
        !cancelled &&
        collectionSucceeded(result) &&
        !isIncompleteIosBackup(result) &&
        !autoProcessRef.current
      ) {
        autoProcessRef.current = true;
        window.setTimeout(() => {
          void startAnalysisRef.current(result);
        }, 50);
      }
      if (isHostAcquisitionJobId(detail.run_id) && !collectionSucceeded(result)) {
        persistLiveRun({
          run_id: detail.run_id,
          case_id: detail.case_id,
          case_root: formRef.current.case_root,
        });
      } else {
        clearLiveRun();
      }
      const failText = friendlyMobileAcquireError(
        String(detail.error || result.error || ""),
      );
      const key = `${detail.run_id}:${detail.status}:${toastMsg || ""}`;
      if (lastFinishToastRef.current === key) return true;
      lastFinishToastRef.current = key;
      if (toastMsg) toast.success(toastMsg);
      else if (collectionSucceeded(result)) {
        toast.success(`Acquisition complete — ${detail.run_name}`);
      } else if (cancelled) {
        toast.info("Collection stopped — files already written stay in the case folder.");
      } else if (isIncompleteIosBackup(result)) {
        toast.error(
          friendlyMobileAcquireError(
            String(detail.error || result.error || result.errors?.[0] || ""),
          ),
        );
      } else {
        toast.error(failText || "Acquisition finished with errors — review the limitations below");
      }
      return true;
    },
    [toast],
  );

  /** Live copy progress → the Collect card (the run itself already finished on this PC). */
  const onImportProgress = useCallback((state: ClientImportState) => {
    setClientImport(state);
    const live = readLiveRun();
    if (live?.run_id && state.import_id && live.import_id !== state.import_id) {
      persistLiveRun({ ...live, import_id: state.import_id });
    }
    setRun((prev) => {
      if (!prev || state.status === "completed") return prev;
      const total = Number(state.total_bytes || 0);
      const done = Number(state.bytes_done || 0);
      return {
        ...prev,
        progress: {
          ...prev.progress,
          stage: "import",
          item: "client_import",
          category: state.transport === "client_stream" ? "Sending to the forensic server" : "Copying to the forensic server",
          detail: state.detail || "Copying the image to the forensic server",
          bytes_done: done,
          bytes_total: total || null,
          files_seen: Number(state.files_done || 0),
          progress_pct: total ? Math.min(100, Math.round((done / total) * 1000) / 10) : prev.progress?.progress_pct ?? null,
          progress_mode: "bytes" as const,
          case_path: state.server_case_dir || prev.progress?.case_path,
          updated_utc: new Date().toISOString(),
        },
      };
    });
  }, []);

  /**
   * Client mode: the run was sealed in C:\evidence on this PC. Share its case folder
   * read-only and let the forensic server copy it (SHA-256 verified) into its own case
   * folder; then finish with the server-path result. Single flight; a failure is not
   * retried automatically (each attempt walks the case folder) — the examiner retries.
   */
  const recoverClientCopy = useCallback(
    async (caseId?: string, opts?: { manual?: boolean }): Promise<boolean> => {
      if (clientFlightRef.current) return clientFlightRef.current;
      if (clientCopyBlockedRef.current && !opts?.manual) return false;
      const id = (caseId || runRef.current?.case_id || formRef.current.case_id || "").trim();
      if (!id) return false;
      const finish = (state: ClientImportState): boolean | null => {
        if (state.status === "completed" && state.run) {
          const liveId = String(runRef.current?.run_id || "");
          const detail = {
            ...state.run,
            run_id: liveId && liveId !== "pending" ? liveId : state.run.run_id || state.import_id,
          } as AcquisitionRunDetail;
          setClientImport(state);
          setClientImportError("");
          const applied = applyFinishedRun(
            detail,
            `Image created on ${state.client_host || "this computer"} and copied to the forensic server — ${state.run_name}`,
          );
          if (applied) setClientMode(false);
          return applied;
        }
        if (state.status === "failed" || state.status === "cancelled") {
          clientCopyBlockedRef.current = true;
          setClientImport(state);
          setClientImportError(
            state.status === "cancelled"
              ? "The copy to the forensic server was stopped. Files already verified on the server are kept; copy again to continue."
              : state.error || "The copy to the forensic server did not finish.",
          );
          return false;
        }
        return null;
      };
      const flight = (async (): Promise<boolean> => {
        try {
          // V1.31.14: the collection was sent to the server while it ran; wait for the server to register it.
          const live = readLiveRun();
          const streamId = streamIdRef.current || live?.stream_id || "";
          if (streamId && !streamFailedRef.current && !opts?.manual) {
            streamIdRef.current = streamId;
            setStreamActive(true);
            setClientCollected(true);
            setClientImportError("");
            const state = await followClientStream({
              sessionId: streamId,
              jobId: String(live?.run_id || runRef.current?.run_id || ""),
              onProgress: onImportProgress,
              onUploader: setStreamUploader,
            });
            if (state.status === "completed") {
              const done = finish(state);
              if (done !== null) return done;
            }
            // The uploader keeps the image on this computer when it could not finish; the copy takes over.
            streamFailedRef.current = true;
            setStreamActive(false);
            clientCopyBlockedRef.current = true;
            setClientImport(state);
            setClientImportError(
              `Sending to the forensic server stopped: ${state.error || state.detail || state.status}. ` +
                "The image is still on this computer; copy it to the forensic server with the button below.",
            );
            return false;
          }
          const agent = await clientAgentStatus(2_000);
          if (agent.state !== "online") {
            // Without the agent the server cannot read the finished image on this PC.
            clientCopyBlockedRef.current = true;
            setClientCollected(true);
            setClientAgentMissing(agent);
            setClientImportError(
              agent.state === "unpaired"
                ? "The Client Evidence Agent on this computer is not paired with this console, so the forensic server cannot copy the image yet."
                : "The Client Evidence Agent is not running on this computer, so the forensic server cannot copy the image yet. The image is safe in the folder on this computer.",
            );
            return false;
          }
          const saved = readLiveRun();
          if (saved?.import_id && !opts?.manual) {
            const resumed = await resumeClientImport(saved.import_id, onImportProgress);
            if (resumed && resumed.status !== "cancelled") {
              const done = finish(resumed);
              if (done !== null) return done;
            }
          }
          const root = (clientCaseRootRef.current || DEFAULT_CLIENT_CASE_ROOT).trim();
          const caseDir = await findClientCaseDir(root, id);
          if (!caseDir) {
            if (opts?.manual) setClientImportError(`No case folder for ${id} was found in ${root} on this computer.`);
            return false;
          }
          const liveName = String(runRef.current?.run_name || "").trim();
          const runHint = liveName && liveName.includes("_") && liveName.toLowerCase() !== id.toLowerCase() ? liveName : undefined;
          const sealed = await clientRunSealed(caseDir, runHint);
          if (!sealed.sealed) {
            if (opts?.manual) {
              setClientImportError(
                "The collection on this computer has not finished yet (07_Logs\\<run>\\collection_summary.json is missing). Wait for it to finish, then copy.",
              );
            }
            return false;
          }
          clientCopyBlockedRef.current = false;
          setClientCollected(true);
          setClientImportError("");
          setClientAgentMissing(null);
          importAbortRef.current = new AbortController();
          const state = await copyClientRunToServer({
            caseDir,
            caseId: id,
            runName: sealed.runName,
            scope: importScopeRef.current,
            onProgress: onImportProgress,
            signal: importAbortRef.current.signal,
          });
          return finish(state) ?? false;
        } catch (e) {
          clientCopyBlockedRef.current = true;
          const msg = e instanceof Error ? e.message : "The copy to the forensic server failed";
          setClientImportError(msg);
          if (e instanceof ClientAgentRequiredError) {
            setClientAgentMissing(await clientAgentStatus(1_500));
          }
          toast.error(msg);
          return false;
        } finally {
          clientFlightRef.current = null;
        }
      })();
      clientFlightRef.current = flight;
      return flight;
    },
    [applyFinishedRun, onImportProgress, toast],
  );

  /** When SSE dies or the API restarted, rebuild Complete from the case folder. */
  const recoverFromDisk = useCallback(
    async (caseRoot?: string, caseId?: string, opts?: { force?: boolean }): Promise<boolean> => {
      if (clientModeRef.current) {
        // The sealed run is on this PC (C:\evidence); the server's case folder gets it by copy.
        return recoverClientCopy(caseId);
      }
      const root = (caseRoot || formRef.current.case_root || "/evidence/cases").trim();
      const id = (caseId || runRef.current?.case_id || formRef.current.case_id || "").trim();
      if (!root || !id) return false;
      const live = runRef.current;
      if (
        !opts?.force &&
        (liveBusyRef.current ||
          (live && (live.status === "running" || live.run_id === "pending")))
      ) {
        return false;
      }
      setRecovering(true);
      try {
        let lastError: unknown = null;
        for (let attempt = 0; attempt < 3; attempt += 1) {
          try {
            const data = await acquisitionApi.recoverRuns(root, id);
            const wantedName = String(live?.run_name || "").trim().toLowerCase();
            const evidenceId = String(formRef.current.evidence_id || "").trim().toLowerCase();
            const finished = (data.runs || [])
              .filter((r) => {
                const status = String(r.status || "").toLowerCase();
                if (status !== "completed" && status !== "failed") return false;
                const detail = r as AcquisitionRunDetail;
                if (!detail.result) return false;
                if (isCancelledAcquisition(detail) && filesCollected(detail.result) <= 0) return false;
                return collectionSucceeded(detail.result) || filesCollected(detail.result) > 0;
              })
              .sort((a, b) => {
                const an = String(a.run_name || "").toLowerCase();
                const bn = String(b.run_name || "").toLowerCase();
                if (wantedName) {
                  const am = an === wantedName || an.includes(wantedName) || wantedName.includes(an) ? 1 : 0;
                  const bm = bn === wantedName || bn.includes(wantedName) || wantedName.includes(bn) ? 1 : 0;
                  if (bm !== am) return bm - am;
                }
                if (evidenceId) {
                  const am = an.includes(evidenceId) ? 1 : 0;
                  const bm = bn.includes(evidenceId) ? 1 : 0;
                  if (bm !== am) return bm - am;
                }
                const fa = filesCollected((a as AcquisitionRunDetail).result);
                const fb = filesCollected((b as AcquisitionRunDetail).result);
                if (fb !== fa) return fb - fa;
                return String(b.run_name || "").localeCompare(String(a.run_name || ""));
              })[0] as AcquisitionRunDetail | undefined;
            if (finished?.result) {
              if (opts?.force) liveBusyRef.current = false;
              return applyFinishedRun(
                finished,
                `Collection recovered from case folder — ${finished.run_name}`,
              );
            }
            return false;
          } catch (err) {
            lastError = err;
            await new Promise((resolve) => window.setTimeout(resolve, 1500 * (attempt + 1)));
          }
        }
        void lastError;
        return false;
      } catch {
        return false;
      } finally {
        setRecovering(false);
      }
    },
    [applyFinishedRun, recoverClientCopy],
  );

  /** Which machine runs this PC's helper vs the forensic server's helper (once per page). */
  useEffect(() => {
    let active = true;
    void resolveSiteIdentity().then((identity) => {
      if (!active) return;
      setSite(identity);
      const preferred = identity.capabilities?.default_client_case_root;
      if (preferred && !readLiveRun()?.client_case_root) setClientCaseRoot(preferred);
    });
    return () => {
      active = false;
    };
  }, []);

  /** V1.31.14: while the collection runs, show what already reached the forensic server. */
  const liveRunId = run?.run_id || "";
  useEffect(() => {
    if (!running || !clientMode || !streamActive) return;
    const id = streamIdRef.current || readLiveRun()?.stream_id || "";
    if (!id) return;
    let active = true;
    const tick = async () => {
      try {
        const st = await acquisitionApi.getClientImport(id);
        if (active) setStreamState(st);
      } catch {
        /* the next tick tries again */
      }
      if (/^[0-9a-f]{32}$/.test(liveRunId)) {
        const up = await getHostStreamStatus(liveRunId);
        if (active && up) setStreamUploader(up);
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), 4_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [running, clientMode, streamActive, liveRunId]);

  const ensureSite = useCallback(async (): Promise<SiteIdentity> => {
    if (siteRef.current) return siteRef.current;
    const identity = await resolveSiteIdentity();
    siteRef.current = identity;
    setSite(identity);
    return identity;
  }, []);

  /**
   * Should the HostDrive helper on *this* PC (127.0.0.1:9876) drive the device?
   * Yes for a phone on this PC. For a phone on the forensic server only when this PC is
   * the server; a remote browser goes through the API so the server's own helper does it.
   */
  const localHelperDrives = useCallback((device: DetectedDevice): boolean => {
    const localIsServer = siteRef.current?.localIsServer ?? null;
    if (deviceSite(device, localIsServer) === "client") return true;
    if (device.attach_host === "office_server" && localIsServer !== true) return false;
    return (
      device.attach_host === "this_pc" ||
      device.adapter === "android_mtp" ||
      device.adapter === "ios_lockdown" ||
      device.status === "wpd" ||
      device.status === "mtp"
    );
  }, []);

  const selectedSite = useMemo(
    () => deviceSite(selected, site?.localIsServer ?? null),
    [selected, site],
  );

  // Prefill case id from an open forensic job when navigated with ?jobId=
  useEffect(() => {
    const jobId = searchParams.get("jobId") || searchParams.get("job_id");
    if (!jobId) return;
    let cancelled = false;
    void (async () => {
      try {
        const job = await forensicApi.getJob(jobId);
        if (cancelled) return;
        setForm((prev) => ({
          ...prev,
          case_id: prev.case_id.trim() || job.case_id || jobId.slice(0, 8).toUpperCase(),
          case_root: prev.case_root.trim() || "/evidence/cases",
        }));
      } catch {
        /* job lookup is optional for standalone acquire */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [searchParams]);

  const loadAdapters = useCallback(async () => {
    try {
      const localHealth = await startHostDriveAgentFromBrowser(20_000);
      const [data, hostDirect] = await Promise.all([
        acquisitionApi.listAdapters(),
        localHealth?.ok ? getHostAcquisitionAdapters() : Promise.resolve({ ok: false, adapters: [] as AcquisitionAdapter[] }),
      ]);

      const merged = new Map<string, AcquisitionAdapter>();
      for (const adapter of data.adapters ?? []) merged.set(adapter.name, adapter);
      if (hostDirect.ok) {
        for (const adapter of hostDirect.adapters) {
          const current = merged.get(adapter.name);
          merged.set(adapter.name, {
            name: adapter.name,
            os_family: adapter.os_family,
            available: Boolean(adapter.available || current?.available),
            reason: adapter.reason || current?.reason || "Available through Windows host helper",
          });
        }
      }
      const adaptersNow = [...merged.values()].filter((adapter) => {
        if (!fixedPlatform) return true;
        return String(adapter.os_family || "").toLowerCase().includes(fixedPlatform);
      });
      setAdapters(adaptersNow);

      const host = (data as { host_helper?: { reachable?: boolean; hint?: string } }).host_helper;
      if (host && host.reachable === false && !hostDirect.ok && !localHealth?.ok) {
        toast.error(
          host.hint ||
            `USB helper is offline. Allow the HostDrive prompt if Windows asks, or run: ${hostDriveHelperStartHint()}`,
        );
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not read adapter status");
    }
  }, [fixedPlatform, toast]);


  useEffect(() => {
    void loadAdapters();
  }, [loadAdapters]);

  const scan = useCallback(async (opts?: { silent?: boolean }) => {
    const silent = Boolean(opts?.silent);
    if (scanInFlightRef.current) return;
    scanInFlightRef.current = true;
    if (!silent) {
      setScanning(true);
      setSelected(null);
      setPreview(null);
    }
    try {
      const health = silent
        ? await ensureHostDriveHelper(2_000, 1_000, { force: true })
        : await startHostDriveAgentFromBrowser(20_000);
      const hostPromise = getHostAcquisitionDevices();
      const hostAdaptersPromise = health?.ok
        ? getHostAcquisitionAdapters()
        : Promise.resolve({ ok: false, adapters: [] as AcquisitionAdapter[] });
      const [data, hostDirect, hostAdaptersDirect, wpdDirect, kitInfo] = await Promise.all([
        acquisitionApi.detectDevices().catch(() => ({ devices: [], warnings: [] as string[], adapters: [] })),
        hostPromise,
        hostAdaptersPromise,
        listHostMobileDevices(silent ? 4_000 : 8_000),
        getClientKitInfo(),
      ]);
      setHelperOnline(Boolean(health?.ok));
      setLocalHelper(hostDirect.local_helper);
      setServerHelperVersion(kitInfo?.helper_version || "");

      const mergedDevices = new Map<string, DetectedDevice>();
      for (const device of data.devices ?? []) {
        mergedDevices.set(`${device.adapter}:${device.device_id}`, device);
      }
      if (hostDirect.ok) {
        for (const device of hostDirect.devices) {
          mergedDevices.set(`${device.adapter}:${device.device_id}`, {
            ...(device as DetectedDevice),
            attach_host: device.attach_host,
          });
        }
      }
      // V1.31.9: when this computer's list is outdated or stopped updating, none of its phones are shown
      // (they may be unplugged long ago); its other list is skipped too.
      const localUntrusted = Boolean(hostDirect.local_helper?.outdated || hostDirect.local_helper?.stale);
      for (const mobile of wpdDirect?.mobile_devices ?? []) {
        if (localUntrusted && mobile.attach_host !== "office_server") continue;
        const converted = hostMobileToAcquisitionDevice(mobile);
        const key = `${converted.adapter}:${converted.device_id}`;
        if (!mergedDevices.has(key)) {
          mergedDevices.set(key, converted as DetectedDevice);
        }
      }
      const allDevices = [...mergedDevices.values()];
      // V1.31.13: a page lists only its own platform; a phone of the other kind is not shown here at all.
      const devicesNow = allDevices.filter((device) => deviceMatchesPlatform(device, fixedPlatform));
      setDevices(devicesNow);
      setLastScanAt(new Date());
      setSelected((current) => {
        if (!current) return current;
        const still = devicesNow.find((d) => d.adapter === current.adapter && d.device_id === current.device_id);
        if (still) return still;
        // Unplugged: forget it while choosing a phone; keep it once identification or collection has begun.
        return stepRef.current === "connect" ? null : current;
      });

      let warnings = [...(data.warnings ?? []), ...(hostDirect.warnings ?? [])];
      // The update panel explains an outdated helper; do not repeat it under Detection limitations.
      if (hostDirect.local_helper?.outdated) {
        warnings = warnings.filter((w) => !/^The Polaron helper on this computer is /.test(w));
      }
      if (hostDirect.ok || (wpdDirect?.mobile_devices?.length ?? 0) > 0 || devicesNow.length) {
        warnings = warnings.filter(
          (w) =>
            !/host drive helper is offline/i.test(w) &&
            !/Office HostDrive helper is offline/i.test(w) &&
            !/No USB tools in the API container/i.test(w) &&
            !/USB debugging \/ Trust/i.test(w) &&
            !/USB acquisition tools are not available/i.test(w),
        );
      }
      setDetectWarnings([...new Set(warnings)]);

      if (data.adapters?.length || hostAdaptersDirect.adapters?.length) {
        setAdapters((current) => {
          const map = new Map(current.map((a) => [a.name, a]));
          for (const a of data.adapters ?? []) map.set(a.name, a);
          for (const a of hostAdaptersDirect.adapters ?? []) {
            const currentAdapter = map.get(a.name);
            map.set(a.name, {
              name: a.name,
              os_family: a.os_family,
              available: Boolean(a.available || currentAdapter?.available),
              reason: a.reason || currentAdapter?.reason || "Available through Windows host helper",
            });
          }
          return [...map.values()].filter((adapter) => {
            if (!fixedPlatform) return true;
            return String(adapter.os_family || "").toLowerCase().includes(fixedPlatform);
          });
        });
      } else if (!silent) {
        void loadAdapters();
      }

      const readyNow = devicesNow.filter((d) => !deviceNeedsPhoneAction(d));
      const waitingNow = devicesNow.filter((d) => deviceNeedsPhoneAction(d));
      const nextKey = devicesNow.map((d) => `${d.adapter}:${d.device_id}`).sort().join("|");
      const appeared = readyNow.length > 0 && nextKey !== lastDeviceKeyRef.current;
      const waitingAppeared = !readyNow.length && waitingNow.length > 0 && nextKey !== lastDeviceKeyRef.current;
      lastDeviceKeyRef.current = nextKey;
      if (appeared) {
        const kinds = [...new Set(readyNow.map((d) => mobileKindLabel(d.os_family)))].join(", ");
        const first = readyNow[0];
        const where = mobileAttachHostLabel(first.attach_host);
        toast.success(`${kinds} detected on ${where}${readyNow.length > 1 ? ` (${readyNow.length} devices)` : ""}.`);
      } else if (waitingAppeared || (!silent && !readyNow.length && waitingNow.length)) {
        const first = waitingNow[0];
        toast.error(
          `${first.label || "Android phone"} is plugged in but cannot be collected yet: ${
            first.guidance || "unlock it and choose USB: File transfer, then Rescan."
          }`,
        );
      } else if (!silent && !devicesNow.length) {
        toast.error(noPhoneText(fixedPlatform));
      }
    } catch (e) {
      if (!silent) toast.error(e instanceof Error ? e.message : "Device scan failed");
    } finally {
      scanInFlightRef.current = false;
      if (!silent) setScanning(false);
    }
  }, [fixedPlatform, loadAdapters, toast]);

  useEffect(() => {
    void scan({ silent: true });
  }, [scan]);

  useEffect(() => {
    if (step !== "connect") return;
    const id = window.setInterval(() => void scan({ silent: true }), 8_000);
    return () => window.clearInterval(id);
  }, [step, scan]);


  async function identify(device: DetectedDevice) {
    if (deviceNeedsPhoneAction(device)) {
      toast.error(device.guidance || "Unlock the phone and choose USB: File transfer on it, then Rescan.");
      return;
    }
    setSelected(device);
    setPreviewing(true);
    setPreview(null);
    try {
      let data: AcquisitionPreview | null = null;
      await ensureSite();
      const hostAttached = localHelperDrives(device);
      if (hostAttached) {
        await startHostDriveAgentFromBrowser(15_000);
        data = await getHostAcquisitionPreview<AcquisitionPreview>(
          device.adapter,
          device.device_id,
          20_000,
          device.label,
        );
        if (!data?.ok) {
          await startHostDriveAgentFromBrowser(15_000);
          data = await getHostAcquisitionPreview<AcquisitionPreview>(
            device.adapter,
            device.device_id,
            20_000,
            device.label,
          );
        }
        if (!data?.ok) {
          throw new Error(
            "Could not identify the phone on this PC. Keep the cable seated, unlock the screen, and click the device again.",
          );
        }
      } else {
        try {
          data = await acquisitionApi.previewDevice(device.adapter, device.device_id);
        } catch (apiError) {
          data = await getHostAcquisitionPreview<AcquisitionPreview>(
            device.adapter,
            device.device_id,
            20_000,
            device.label,
          );
          if (!data?.ok) {
            throw apiError instanceof Error
              ? apiError
              : new Error("Could not identify the phone. Keep the cable seated and try again.");
          }
        }
      }
      if (!data?.ok || !data.device_profile) {
        throw new Error("Could not identify the phone on this PC. Keep the cable seated and try again.");
      }
      setPreview(data);
      setForm((prev) => applyDeviceAutofill(prev, data.device_profile, { force: false }));
      const methods = data.capability?.supported_methods ?? [];
      if (methods.includes("advanced_logical")) {
        setMethodOverride("full_file_system");
      } else if (methods.includes("backup")) {
        setMethodOverride("backup");
      } else if (methods.includes("logical")) {
        setMethodOverride("logical");
      } else {
        setMethodOverride("");
      }
      setStep("identify");
      toast.success(
        "Device profile loaded — use Advanced Logical / Backup for WhatsApp, photos and downloads.",
      );
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Device identification failed");
      setSelected(null);
    } finally {
      setPreviewing(false);
    }
  }

  function refillFromDevice(force = true) {
    if (!preview?.device_profile) {
      toast.error("Identify a device first");
      return;
    }
    setForm((prev) => applyDeviceAutofill(prev, preview.device_profile, { force }));
    toast.success(
      force
        ? "Authority fields refreshed from the connected device"
        : "Empty authority fields filled from the device",
    );
  }

  const requiredMethods = useMemo(() => {
    const sets = OBJECTIVES.filter((o) => objectives.includes(o.id)).map((o) => o.requires);
    if (!sets.length) return [];
    // Intersection: a method must satisfy EVERY selected objective.
    return sets.reduce((acc, cur) => acc.filter((m) => cur.includes(m)));
  }, [objectives]);

  const supported = preview?.capability?.supported_methods ?? [];
  const satisfiable = useMemo(
    () => supported.filter((m) => !requiredMethods.length || requiredMethods.includes(m)),
    [supported, requiredMethods],
  );

  const unmetObjectives = useMemo(
    () =>
      OBJECTIVES.filter(
        (o) => objectives.includes(o.id) && !o.requires.some((m) => supported.includes(m)),
      ),
    [objectives, supported],
  );

  /** Attach to a running collection and follow it to completion. */
  const attach = useCallback(
    (runId: string) => {
      streamAbort.current?.abort();
      finishingRef.current = false;
      const controller = new AbortController();
      streamAbort.current = controller;
      setRunning(true);
      setStep("collect");
      void streamAcquisition({
        runId,
        signal: controller.signal,
        onProgress: (snapshot) => {
          setRun(snapshot);
          // Host may mark progress complete before the SSE "done" frame arrives
          // (large result JSON). Advance as soon as stage says finished.
          const stage = String(snapshot.progress?.stage || "").toLowerCase();
          if (
            !finishingRef.current &&
            (stage === "complete" || stage === "completed" || stage === "done")
          ) {
            finishingRef.current = true;
            void (async () => {
              try {
                const detail = await acquisitionApi.getRun(runId);
                if (detail.result) {
                  applyFinishedRun(detail);
                  return;
                }
              } catch {
                /* fall through */
              }
              const ok = await recoverFromDisk(undefined, snapshot.case_id);
              if (!ok) finishingRef.current = false;
            })();
          }
        },
        onDone: (detail) => {
          if (detail.result) {
            applyFinishedRun(detail);
          } else {
            setRun(detail);
            setRunning(false);
            setStep("authorise");
            toast.error(detail.error ?? "Acquisition failed before any data was collected");
          }
        },
        onError: (err) => {
          // A broken stream is a lost view, not a lost collection. Poll once,
          // then recover sealed evidence from the case folder if the run is gone.
          void (async () => {
            try {
              const detail = await acquisitionApi.getRun(runId);
              if (
                detail.status === "completed" ||
                detail.status === "failed" ||
                detail.status === "cancelled"
              ) {
                if (detail.result) {
                  applyFinishedRun(detail);
                  return;
                }
              }
              if (detail.status === "running" || detail.status === "queued") {
                setRun(detail);
                setRunning(true);
                toast.error(
                  `Progress stream interrupted (${err.message}). Collection is still running — ` +
                    "progress will refresh automatically.",
                );
                // Best-effort reattach after a short pause.
                window.setTimeout(() => {
                  if (streamAbort.current === controller) attach(runId);
                }, 3000);
                return;
              }
            } catch {
              /* run missing from memory — try disk */
            }
            const ok = await recoverFromDisk();
            if (!ok) {
              setRunning(false);
              toast.error(
                `Progress stream interrupted (${err.message}). If the phone backup already ` +
                  "finished, use “Recover from case folder” below.",
              );
            }
          })();
        },
      });
    },
    [toast, applyFinishedRun, recoverFromDisk],
  );

  // Reattach to a collection already in flight. Live host/Docker jobs always
  // win over a previously finished Complete screen — refresh must not freeze
  // the wizard on the last sealed run while USB copy continues on this PC.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const applyLiveHost = (
          jobId: string,
          snap?: LiveRunSnapshot,
          hint?: {
            run_name?: string;
            case_id?: string;
            detail?: string;
            files_seen?: number;
            bytes_done?: number;
          },
        ) => {
          persistLiveRun({
            run_id: jobId,
            case_id: snap?.case_id || hint?.case_id,
            case_root: snap?.case_root,
            adapter: snap?.adapter,
            device_id: snap?.device_id,
            os_family: snap?.os_family,
          });
          if (snap?.case_id || hint?.case_id) {
            setForm((prev) => ({
              ...prev,
              case_id: snap?.case_id || hint?.case_id || prev.case_id,
              case_root: snap?.case_root || prev.case_root,
            }));
          }
          setRun({
            run_id: jobId,
            run_name: String(hint?.run_name || snap?.case_id || ""),
            status: "running",
            case_id: snap?.case_id || hint?.case_id || "",
            adapter: snap?.adapter || "android_mtp",
            device_id: snap?.device_id || "",
            host_job: true,
            progress: {
              stage: "acquire",
              item: "collecting",
              detail: hint?.detail || "Collection in progress on this PC",
              bytes_done: Number(hint?.bytes_done || 0),
              files_seen: Number(hint?.files_seen || 0),
              updated_utc: new Date().toISOString(),
            },
          } as AcquisitionRunDetail);
          setRunning(true);
          setStep("collect");
          toast.success("Reattached to a collection already in progress on this PC");
        };

        const resumeHost = async (
          jobId: string,
          snap?: LiveRunSnapshot,
          hint?: {
            status?: string;
            run_name?: string;
            case_id?: string;
            detail?: string;
            files_seen?: number;
            bytes_done?: number;
          },
        ) => {
          const job = await getHostAcquisitionJob(jobId);
          const status = String(job?.status || hint?.status || "").toLowerCase();
          const hostResult = (job?.result || {}) as AcquisitionResult & {
            detail?: string;
            acquisition_record?: { output_size?: number; file_count?: number };
          };
          if (snap?.case_id || hint?.case_id) {
            setForm((prev) => ({
              ...prev,
              case_id: snap?.case_id || hint?.case_id || prev.case_id,
              case_root: snap?.case_root || prev.case_root,
            }));
          }
          if (status === "completed" || status === "failed" || status === "cancelled") {
            if (
              status === "cancelled" &&
              Number(hint?.files_seen || hostResult.acquisition_record?.file_count || 0) <= 0 &&
              filesCollected(hostResult) <= 0
            ) {
              clearLiveRun();
              return false;
            }
            const recovered = await recoverFromDisk(snap?.case_root, snap?.case_id || hint?.case_id, {
              force: true,
            });
            if (recovered) return true;
            if (!hostResult || Object.keys(hostResult).length === 0) return false;
            applyFinishedRun({
              run_id: jobId,
              run_name: String(hostResult.run_name || snap?.case_id || jobId),
              status: status === "completed" ? "completed" : status === "cancelled" ? "cancelled" : "failed",
              case_id: snap?.case_id || hint?.case_id || "",
              error: String((hostResult as { error?: string }).error || job?.error || ""),
              result: hostResult,
            } as AcquisitionRunDetail);
            return true;
          }
          if (!job) return false;
          applyLiveHost(jobId, snap, {
            run_name: String(hostResult.run_name || hint?.run_name || snap?.case_id || ""),
            case_id: snap?.case_id || hint?.case_id,
            detail:
              hostResult.detail ||
              hint?.detail ||
              "Collection in progress on this PC",
            files_seen: Number(
              hostResult.acquisition_record?.file_count || hint?.files_seen || 0,
            ),
            bytes_done: Number(
              hostResult.acquisition_record?.output_size || hint?.bytes_done || 0,
            ),
          });
          return true;
        };

        const saved = readLiveRun();
        if (saved?.client_mode) {
          // A client-side collection (or its copy to the server) was in flight before the reload.
          setClientMode(true);
          if (saved.client_case_root) {
            clientCaseRootRef.current = saved.client_case_root;
            setClientCaseRoot(saved.client_case_root);
          }
          if (saved.scope === "packages" || saved.scope === "full") {
            importScopeRef.current = saved.scope;
            setImportScope(saved.scope);
          }
          if (saved.stream_id) {
            // V1.31.14: this collection is sent to the server while it runs; show that again.
            streamIdRef.current = saved.stream_id;
            setStreamActive(true);
          }
        }
        const allHostJobs = await listHostAcquisitionJobs();
        const hostJobs = allHostJobs.filter((j) => {
          const status = String(j.status || "").toLowerCase();
          return !status || status === "running" || status === "queued";
        });
        const finishedHostJobs = allHostJobs.filter((j) => {
          const status = String(j.status || "").toLowerCase();
          if (status === "cancelled") return false;
          return (status === "completed" || status === "failed") && Number(j.files_seen || 0) > 0;
        });
        if (saved?.run_id && isHostAcquisitionJobId(saved.run_id)) {
          if (await resumeHost(saved.run_id, saved, allHostJobs.find((j) => j.job_id === saved.run_id))) {
            return;
          }
        }
        if (hostJobs[0]?.job_id) {
          if (
            await resumeHost(
              hostJobs[0].job_id,
              {
                run_id: hostJobs[0].job_id,
                case_id: hostJobs[0].case_id || saved?.case_id,
                case_root: saved?.case_root,
              },
              hostJobs[0],
            )
          ) {
            return;
          }
        }
        const wantedCase = String(saved?.case_id || "").toLowerCase();
        const finishedMatch = finishedHostJobs.find((j) => {
          const cid = String(j.case_id || "").toLowerCase();
          if (saved?.run_id && j.job_id === saved.run_id) return true;
          return Boolean(wantedCase) && cid === wantedCase;
        });
        if (finishedMatch?.job_id) {
          if (
            await resumeHost(
              finishedMatch.job_id,
              {
                run_id: finishedMatch.job_id,
                case_id: finishedMatch.case_id || saved?.case_id,
                case_root: saved?.case_root,
              },
              finishedMatch,
            )
          ) {
            return;
          }
        }

        const data = await acquisitionApi.listRuns(true);
        if (cancelled) return;
        if (data.runs.length) {
          const live = data.runs.find((r) => r.status === "running" || r.status === "queued") || data.runs[0];
          if (isHostAcquisitionJobId(live.run_id) || live.host_job) {
            const ok = await resumeHost(live.run_id, {
              run_id: live.run_id,
              case_id: live.case_id || saved?.case_id,
              case_root: saved?.case_root,
              adapter: live.adapter,
              device_id: live.device_id,
            }, {
              run_name: live.run_name,
              case_id: live.case_id,
              detail: live.progress?.detail,
              files_seen: live.progress?.files_seen,
              bytes_done: live.progress?.bytes_done,
            });
            if (ok || cancelled) return;
            applyLiveHost(live.run_id, {
              run_id: live.run_id,
              case_id: live.case_id || saved?.case_id,
              case_root: saved?.case_root,
              adapter: live.adapter,
              device_id: live.device_id,
            }, {
              run_name: live.run_name,
              case_id: live.case_id,
              detail: live.progress?.detail,
              files_seen: live.progress?.files_seen,
              bytes_done: live.progress?.bytes_done,
            });
            return;
          }
          setRun(live);
          const detail = await acquisitionApi.getRun(live.run_id);
          if (cancelled) return;
          setSelected({
            adapter: detail.adapter,
            device_id: detail.device_id,
            os_family: detail.result?.device_profile?.os_family ?? "",
          });
          if (detail.result?.device_profile && detail.result.capability) {
            setPreview({
              ok: true,
              device_profile: detail.result.device_profile,
              capability: detail.result.capability,
              preparation_steps: detail.result.preparation_steps ?? [],
              method_profiles: {},
            });
          }
          if (
            detail.status === "completed" ||
            detail.status === "failed" ||
            detail.status === "cancelled"
          ) {
            applyFinishedRun(detail);
            return;
          }
          persistLiveRun({
            run_id: live.run_id,
            case_id: detail.case_id || live.case_id,
            adapter: detail.adapter,
            device_id: detail.device_id,
          });
          attach(live.run_id);
          toast.success("Reattached to a collection already in progress");
          return;
        }

        // No active collection. Stay on Connect — do not auto-open the last
        // finished Complete screen. Use “Recover from case folder” for that.
        return;
      } catch {
        /* nothing in flight, or the endpoint is unavailable — start fresh */
      }
    })();
    return () => {
      cancelled = true;
    };
    // Mount only: reattaching on every render would thrash the stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Watchdog: poll host helper / Docker so Collect stays live after refresh.
  // Also retry when Complete is showing a 0-file stub after a 502.
  useEffect(() => {
    const cancelledEmpty =
      runRef.current?.status === "cancelled" ||
      isCancelledAcquisition({ status: runRef.current?.status, result });
    const zeroStub = step === "complete" && filesCollected(result) <= 0 && !cancelledEmpty;
    if (step !== "collect" && !zeroStub) return;
    if (result && !zeroStub) return;
    const tick = () => {
      void (async () => {
        const current = runRef.current;
        if (!current) return;
        if (appliedTerminalRef.current && appliedTerminalRef.current === current.run_id) return;
        try {
          if (isHostAcquisitionJobId(current.run_id)) {
            const job = await getHostAcquisitionJob(current.run_id);
            const status = String(job?.status || "").toLowerCase();
            const hostResult = (job?.result || {}) as AcquisitionResult & {
              detail?: string;
              error?: string;
              progress_pct?: number;
              item?: string;
              output_path?: string;
              acquisition_record?: { output_size?: number; file_count?: number };
            };
            if (
              (status === "cancelled" || isCancelledAcquisition({ status, result: hostResult })) &&
              !isInFlightHostResult(status, hostResult)
            ) {
              applyFinishedRun({
                ...current,
                status: "cancelled",
                error: hostResult.error || current.error,
                result: hostResult,
              } as AcquisitionRunDetail);
              return;
            }
            if (isInFlightHostResult(status, hostResult) || status === "running" || !status) {
              if (!job && (status === "not_found" || !status)) {
                const listed = (await listHostAcquisitionJobs()).find(
                  (j) => j.job_id === current.run_id,
                );
                const listStatus = String(listed?.status || "").toLowerCase();
                if (listStatus === "completed" || listStatus === "failed") {
                  liveBusyRef.current = false;
                  const recovered = await recoverFromDisk(undefined, current.case_id, { force: true });
                  if (recovered) return;
                  applyFinishedRun({
                    ...current,
                    status: listStatus,
                    result: {
                      ok: listStatus === "completed",
                      run_name: listed?.run_name || current.run_name,
                      stage_reached: listStatus === "completed" ? "complete" : "failed",
                      acquisition_record: {
                        file_count: Number(listed?.files_seen || 0),
                        output_size: Number(listed?.bytes_done || 0),
                      },
                    },
                  } as unknown as AcquisitionRunDetail);
                  return;
                }
                const sealing =
                  current.progress?.stage === "seal" ||
                  Number(current.progress?.progress_pct || 0) >= 100;
                if (sealing) {
                  liveBusyRef.current = false;
                  await recoverFromDisk(undefined, current.case_id, { force: true });
                }
                return;
              }
              const hostJob = job as {
                files_seen?: number;
                bytes_done?: number;
                progress_pct?: number;
                item?: string;
                output_path?: string;
                case_path?: string;
                media_path?: string;
                detail?: string;
                stage?: string;
              } | null;
              const detailText =
                hostResult.detail ||
                hostJob?.detail ||
                current.progress?.detail ||
                "Collection in progress on this PC";
              const itemText =
                hostResult.item ||
                hostJob?.item ||
                current.progress?.item ||
                "ios_backup";
              const stageText = String(
                (hostResult as { stage?: string }).stage ||
                  hostJob?.stage ||
                  current.progress?.stage ||
                  "",
              ).toLowerCase();
              const collectionDone =
                itemText === "complete" ||
                stageText === "complete" ||
                /collection finished/i.test(detailText) ||
                String(hostResult.stage_reached || "").toLowerCase() === "complete";
              if (collectionDone) {
                liveBusyRef.current = false;
                const recovered = await recoverFromDisk(undefined, current.case_id, { force: true });
                if (recovered) return;
                if (hostResult && (collectionSucceeded(hostResult) || hostResult.run_name)) {
                  applyFinishedRun({
                    ...current,
                    status: collectionSucceeded(hostResult) ? "completed" : "failed",
                    error: hostResult.error || current.error,
                    result: hostResult,
                  } as AcquisitionRunDetail);
                }
                return;
              }
              setRun((prev) => {
                if (!prev) return prev;
                const rec = (hostResult.acquisition_record || {}) as {
                  output_size?: number;
                  file_count?: number;
                };
                const files = Math.max(
                  Number(rec.file_count || 0),
                  Number(hostJob?.files_seen || 0),
                  Number(prev.progress?.files_seen || 0),
                );
                const bytes = Math.max(
                  Number(rec.output_size || 0),
                  Number(hostJob?.bytes_done || 0),
                  Number(prev.progress?.bytes_done || 0),
                );
                const hostPct = Number(
                  (hostResult as { progress_pct?: number }).progress_pct ??
                    hostJob?.progress_pct ??
                    0,
                );
                // The evidence ZIP is written after the collection ("Writing full evidence ZIP…",
                // "Packaging the evidence ZIP…"): label it as packaging with its real percentage
                // (90-99.5), not as "acquire", and not as 100% before it is done (V1.31.5).
                const sealing =
                  itemText === "export_packages" ||
                  stageText === "seal" ||
                  /package|seal|evidence zip/i.test(detailText) ||
                  /package|seal/i.test(String(itemText));
                const pct = sealing
                  ? hostPct > 0
                    ? Math.min(99.5, Math.max(90, hostPct))
                    : 90
                  : hostPct > 0
                    ? Math.min(100, hostPct)
                    : bytes > 0
                      ? Math.min(99, 12 + Math.min(66, Math.round(Math.log10(1 + bytes / 1_048_576) * 18)))
                      : files > 0
                        ? Math.min(99, 12 + Math.min(66, Math.round(files / 80)))
                        : Math.max(8, Number(prev.progress?.progress_pct || 0));
                return {
                  ...prev,
                  progress: {
                    ...prev.progress,
                    stage: sealing ? "seal" : "acquire",
                    category: sealing ? "Sealing" : "Collecting",
                    detail: detailText,
                    item: itemText,
                    output_path:
                      (hostResult as { output_path?: string }).output_path ||
                      hostJob?.output_path ||
                      prev.progress?.output_path,
                    case_path: hostJob?.case_path || prev.progress?.case_path,
                    media_path: hostJob?.media_path || prev.progress?.media_path,
                    bytes_done: bytes,
                    files_seen: files,
                    progress_pct: pct,
                    progress_mode: hostPct > 0 ? ("bytes" as const) : ("estimated" as const),
                    updated_utc: new Date().toISOString(),
                  },
                };
              });
              return;
            }
            if (status === "completed" || status === "failed") {
              liveBusyRef.current = false;
              const recovered = await recoverFromDisk(undefined, current.case_id, { force: true });
              if (recovered) return;
              if (hostResult && (hostResult.ok === true || hostResult.ok === false || hostResult.run_name)) {
                applyFinishedRun({
                  ...current,
                  status: collectionSucceeded(hostResult) ? "completed" : "failed",
                  error: hostResult.error || current.error,
                  result: hostResult,
                } as AcquisitionRunDetail);
              }
              return;
            }
            return;
          }
          if (current.run_id && current.run_id !== "pending") {
            const detail = await acquisitionApi.getRun(current.run_id);
            if (
              detail.status === "completed" ||
              detail.status === "failed" ||
              detail.status === "cancelled"
            ) {
              if (detail.result) applyFinishedRun(detail);
              return;
            }
            setRun(detail);
            const stage = String(detail.progress?.stage || "").toLowerCase();
            if (stage === "complete" || stage === "completed") {
              const ok = await recoverFromDisk(undefined, detail.case_id);
              if (ok) return;
            }
          }
        } catch {
          if (!isHostAcquisitionJobId(current.run_id)) {
            await recoverFromDisk(undefined, current.case_id);
          }
        }
      })();
    };
    const id = window.setInterval(tick, 4_000);
    // Immediate check — covers the "stuck for an hour" page already open.
    tick();
    return () => window.clearInterval(id);
  }, [step, result, applyFinishedRun, recoverFromDisk]);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      const jobs = await listHostAcquisitionJobs();
      if (cancelled) return;
      if (jobs.some((j) => String(j.status || "").toLowerCase() === "running")) {
        setUsbBusy(true);
      }
    }
    void tick();
    const id = window.setInterval(() => void tick(), 8000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => () => streamAbort.current?.abort(), []);

  useEffect(() => {
    if (step === "connect" || step === "identify" || step === "authorise") {
      setStarting(false);
      setRunning(false);
    }
  }, [step]);

  async function retryIncompleteCollection() {
    lastFinishToastRef.current = "";
    appliedTerminalRef.current = "";
    finishingRef.current = false;
    clearLiveRun();
    setResult(null);
    setRun(null);
    setUsbBusy(false);
    setStarting(false);
    setRunning(false);
    setStep("authorise");
    toast.info("Keep the iPhone unlocked, screen awake, and the cable seated. Starting collection again…");
    window.setTimeout(() => {
      void startAcquisition();
    }, 0);
  }

  async function startAcquisition() {
    if (starting || running) return;
    if (!selected) return;
    if (!form.legal_authority.trim()) {
      toast.error("Legal authority reference is required before any collection may begin");
      return;
    }
    if (!form.legal_ack) {
      toast.error("Confirm lawful authority and authorised scope");
      return;
    }
    await ensureSite();
    const onClient = deviceSite(selected, siteRef.current?.localIsServer ?? null) === "client";
    const clientRoot = clientCaseRoot.trim();
    if (!form.case_id.trim() || !form.evidence_id.trim() || (!onClient && !form.case_root.trim())) {
      toast.error("Case ID, evidence ID and case root are required");
      return;
    }
    if (onClient && !/^[A-Za-z]:\\/.test(clientRoot)) {
      toast.error("Enter the image folder on this computer as a full Windows path, for example C:\\evidence");
      return;
    }
    setClientMode(onClient);
    clientCopyBlockedRef.current = false;
    setClientImport(null);
    setClientImportError("");
    setClientCollected(false);
    setClientAgentMissing(null);
    streamIdRef.current = "";
    streamFailedRef.current = false;
    setStreamActive(false);
    setStreamState(null);
    setStreamUploader(null);
    // V1.31.14: send the collection to the server while it runs (the helper falls back to copy-after if it cannot).
    let streamSession: Awaited<ReturnType<typeof createClientStream>>["session"] = null;
    if (onClient) {
      const created = await createClientStream({
        caseId: form.case_id.trim(),
        scope: importScope,
        keepLocal: keepClientCopy,
        clientHost: siteRef.current?.localComputer || "",
      });
      streamSession = created.session;
      if (!streamSession) {
        toast.info(`The image is written to ${clientRoot} first and copied to the forensic server afterwards (${created.error}).`);
      }
    }
    const preflightAgent = () => {
      // Pre-flight for the copy step; the collection itself does not need either of these.
      void clientAgentStatus(1_500).then((agent) => {
        if (agent.state !== "online") {
          setClientAgentMissing(agent);
          toast.info(
            "Install the Client Evidence Agent on this computer while the phone is collected — the forensic server needs it to copy the image.",
          );
        }
      });
    };
    if (onClient) {
      if (!streamSession) preflightAgent();
      const helper = siteRef.current?.capabilities?.office_helper;
      if (helper && (!helper.reachable || !helper.client_import)) {
        toast.error(
          `The forensic server's HostDrive helper ${helper.reachable ? `(v${helper.helper_version || "?"}) is too old` : "is not running"} to store the copy. Restart it on the server (scripts\\ensure-host-drive-helper.ps1) before the collection finishes.`,
        );
      }
    }
    finishingRef.current = false;
    lastFinishToastRef.current = "";
    appliedTerminalRef.current = "";
    liveBusyRef.current = true;
    setStarting(true);
    setRunning(true);
    setResult(null);
    const pendingRun = {
      run_id: "pending",
      run_name: "",
      case_id: form.case_id.trim(),
      evidence_id: form.evidence_id.trim(),
      adapter: selected.adapter,
      device_id: selected.device_id,
      examiner: "",
      status: "running" as const,
      started_utc: new Date().toISOString(),
      ended_utc: null,
      cancel_requested: false,
      progress: {
        stage: "acquire",
        item: "starting",
        detail: onClient
          ? `Starting collection on this computer into ${clientRoot}`
          : "Starting collection on this PC",
        category: "Collecting",
        bytes_done: 0,
        bytes_total: null,
        progress_pct: null,
        files_seen: 0,
        updated_utc: new Date().toISOString(),
      },
      error: null,
      result: null,
    };
    setRun(pendingRun as AcquisitionRunDetail);
    setStep("collect");
    const startPayload = {
      adapter: selected.adapter,
      device_id: selected.device_id,
      device_label: selected.label,
      case_id: form.case_id.trim(),
      evidence_id: form.evidence_id.trim(),
      legal_authority: form.legal_authority.trim(),
      // Client mode: the image is created on this PC (C:\evidence\<CASE>\…) first.
      case_root: onClient ? clientRoot : form.case_root.trim(),
      objective_methods: requiredMethods,
      method_override: methodOverride || null,
      cable_adapter_asset_id: form.cable_adapter_asset_id,
      backup_password: form.backup_password || null,
      examiner_notes: form.examiner_notes,
      device_condition: form.device_condition,
      network_isolated: form.network_isolated,
      create_working_copy: true,
      authorized_actions: [], // Android WhatsApp processing is offline and read-only.
      whatsapp_64_digit_key: form.wa_64_digit_key.trim(),
      cloud_mailboxes: form.cloud_mail_address.trim() && form.cloud_mail_secret
        ? [
            {
              provider: form.cloud_mail_provider || "gmail",
              address: form.cloud_mail_address.trim(),
              secret: form.cloud_mail_secret,
            },
          ]
        : [],
    };
    const hostAttached = localHelperDrives(selected);
    const liveExtras = onClient
      ? { client_mode: true, client_case_root: clientRoot, scope: importScope }
      : { client_mode: false };
    try {
      const startOnHost = async () => {
        await startHostDriveAgentFromBrowser(15_000);
        if (!streamSession) return startHostAcquisitionRun(startPayload);
        const session = streamSession;
        const first = await startHostAcquisitionRun({
          ...startPayload,
          ...streamStartFields(session, keepClientCopy, importScope),
        });
        if (first?.stream_refused) {
          // Nothing was started: collect into the folder on this computer and copy afterwards, as before.
          streamSession = null;
          void dropClientStream(session.session_id);
          toast.info(`${first.error} The image is written to ${clientRoot} and copied to the forensic server afterwards.`);
          preflightAgent();
          return startHostAcquisitionRun(startPayload);
        }
        if (first?.ok && !first.stream) {
          // Helper older than 5.66 ignored the session: the image is copied after the collection.
          streamSession = null;
          void dropClientStream(session.session_id);
          preflightAgent();
        }
        if (first?.ok && streamSession) {
          streamIdRef.current = session.session_id;
          setStreamActive(true);
        }
        return first;
      };
      if (hostAttached) {
        const host = await startOnHost();
        if (!host?.ok || !host.job_id) {
          if (host?.busy || isUsbCollectionBusyError(host?.error)) setUsbBusy(true);
          throw new Error(
            host?.error ||
              "Collection could not start on this PC. Keep the host helper running on port 9876 and the phone unlocked on File transfer.",
          );
        }
        toast.info(
          onClient && streamIdRef.current
            ? "Collection started on this computer. Every finished file is sent to the forensic server right away and checked there."
            : onClient
              ? `Collection started on this computer. The image is written to ${clientRoot} first, then copied to the forensic server.`
              : "Collection started on this PC — Docker USB bridge is not required.",
        );
        persistLiveRun({
          run_id: String(host.job_id),
          case_id: form.case_id.trim(),
          case_root: form.case_root.trim(),
          adapter: selected.adapter,
          device_id: selected.device_id,
          os_family: selected.os_family,
          ...liveExtras,
          stream_id: streamIdRef.current || undefined,
        });
        setRun((prev) =>
          ({
            ...(prev || pendingRun),
            run_id: String(host.job_id),
            status: "running",
          }) as AcquisitionRunDetail,
        );
        setStep("collect");
        for (let i = 0; i < 3600; i++) {
          await new Promise((r) => setTimeout(r, 2000));
          const job = await getHostAcquisitionJob(host.job_id);
          const status = String(job?.status || "").toLowerCase();
          const hostResult = (job?.result || {}) as AcquisitionResult & {
            detail?: string;
            acquisition_record?: { output_size?: number; file_count?: number };
          };
          if (status === "running" || !status || isInFlightHostResult(status, hostResult)) {
            setRun((prev) =>
              prev
                ? {
                    ...prev,
                    run_id: String(host.job_id),
                    run_name: String(hostResult.run_name || prev.run_name || ""),
                    progress: {
                      ...prev.progress,
                      stage: "acquire",
                      item: "collecting",
                      detail:
                        hostResult.detail ||
                        prev.progress?.detail ||
                        "Collection in progress on this PC",
                      bytes_done: Number(
                        hostResult.acquisition_record?.output_size || prev.progress?.bytes_done || 0,
                      ),
                      files_seen: Number(
                        hostResult.acquisition_record?.file_count || prev.progress?.files_seen || 0,
                      ),
                      updated_utc: new Date().toISOString(),
                    },
                  }
                : prev,
            );
            continue;
          }
          if (status === "completed" || status === "failed" || status === "cancelled") {
            if (status === "cancelled" || isCancelledAcquisition({ status, result: hostResult })) {
              applyFinishedRun(
                {
                  run_id: host.job_id,
                  run_name: String(hostResult.run_name || form.case_id.trim()),
                  status: "cancelled",
                  case_id: form.case_id.trim(),
                  error: String(hostResult.error || job?.error || ""),
                  result: hostResult,
                } as AcquisitionRunDetail,
              );
              return;
            }
            liveBusyRef.current = false;
            const recovered = await recoverFromDisk(form.case_root.trim(), form.case_id.trim(), {
              force: true,
            });
            if (recovered) return;
            if (clientModeRef.current && status === "completed") {
              // Sealed on this PC; the Collect card now shows the copy to the server (or its error).
              setClientCollected(true);
              setStarting(false);
              return;
            }
            applyFinishedRun(
              {
                run_id: host.job_id,
                run_name: String(hostResult.run_name || form.case_id.trim()),
                status: status === "completed" ? "completed" : "failed",
                case_id: form.case_id.trim(),
                error: String(hostResult.error || job?.error || ""),
                result: hostResult,
              } as AcquisitionRunDetail,
            );
            return;
          }
        }
        throw new Error("Host collection is still running — leave this page open or recover from the case folder later.");
      }
      try {
        const queued = await acquisitionApi.startAcquisition(startPayload);
        setRun(queued);
        attach(queued.run_id);
        return;
      } catch (apiError) {
        const host = hostAttached ? null : await startOnHost();
        if (!host?.ok || !host.job_id) {
          if (host?.busy || isUsbCollectionBusyError(host?.error)) setUsbBusy(true);
          throw apiError instanceof Error
            ? apiError
            : new Error(host?.error || "Acquisition could not be started");
        }
        toast.info("Collection started on this PC — Docker USB bridge is not required.");
        persistLiveRun({
          run_id: String(host.job_id),
          case_id: form.case_id.trim(),
          case_root: form.case_root.trim(),
          adapter: selected.adapter,
          device_id: selected.device_id,
          os_family: selected.os_family,
        });
        setStep("collect");
        for (let i = 0; i < 3600; i++) {
          await new Promise((r) => setTimeout(r, 2000));
          const job = await getHostAcquisitionJob(host.job_id);
          const status = String(job?.status || "").toLowerCase();
          const hostResult = (job?.result || {}) as AcquisitionResult;
          if (status === "running" || !status || isInFlightHostResult(status, hostResult)) {
            continue;
          }
          if (status === "completed" || status === "failed" || status === "cancelled") {
            if (status === "cancelled" || isCancelledAcquisition({ status, result: hostResult })) {
              applyFinishedRun(
                {
                  run_id: host.job_id,
                  run_name: form.case_id.trim(),
                  status: "cancelled",
                  case_id: form.case_id.trim(),
                  error: String((hostResult as { error?: string })?.error || job?.error || ""),
                  result: hostResult,
                } as AcquisitionRunDetail,
              );
              return;
            }
            liveBusyRef.current = false;
            const recovered = await recoverFromDisk(form.case_root.trim(), form.case_id.trim(), {
              force: true,
            });
            if (recovered) return;
            if (job?.result) {
              applyFinishedRun(
                {
                  run_id: host.job_id,
                  run_name: form.case_id.trim(),
                  status: status === "completed" ? "completed" : "failed",
                  case_id: form.case_id.trim(),
                  error: String((job.result as { error?: string })?.error || job?.error || ""),
                  result: job.result as AcquisitionResult,
                } as AcquisitionRunDetail,
              );
              return;
            }
            throw new Error(job?.error || "Host collection finished without a sealed package");
          }
        }
        throw new Error("Host collection is still running — leave this page open or recover from the case folder later.");
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Acquisition could not be started";
      if (!/still running/i.test(msg)) liveBusyRef.current = false;
      setStarting(false);
      setRunning(false);
      if (isUsbCollectionBusyError(msg)) setUsbBusy(true);
      toast.error(friendlyMobileAcquireError(msg));
      setStep("authorise");
    }
  }

  async function cancelRun() {
    if (!run || cancelling) return;
    setCancelling(true);
    setRun((prev) => (prev ? { ...prev, cancel_requested: true } : prev));
    try {
      if (isHostAcquisitionJobId(run.run_id)) {
        const res = await cancelHostAcquisitionJob(run.run_id);
        if (res?.ok) toast.success(res.message || "Host collection stopped");
        else toast.error(res?.error || "Could not stop the host collection");
        return;
      }
      const res = await acquisitionApi.cancelRun(run.run_id);
      (res.accepted ? toast.success : toast.error)(res.message);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Cancel request failed";
      toast.error(
        msg.includes("timed out")
          ? "Stop signal sent. The host backup should end in a few seconds — refresh if the spinner stays."
          : msg,
      );
    } finally {
      setCancelling(false);
    }
  }

  function resetWizardAfterRemove() {
    finishingRef.current = true;
    streamAbort.current?.abort();
    setRunning(false);
    setStarting(false);
    setCancelling(false);
    setRemoving(false);
    setResult(null);
    setRun(null);
    setPreview(null);
    setSelected(null);
    setStep("connect");
  }

  function removePayload(fromResult?: AcquisitionResult | null) {
    const paths: Record<string, string> = {};
    const src = fromResult?.paths || {};
    for (const [key, value] of Object.entries(src)) {
      if (typeof value === "string" && value.trim()) paths[key] = value;
    }
    if (run?.progress?.output_path) paths.original = paths.original || run.progress.output_path;
    if (run?.progress?.case_path) paths.case_root = paths.case_root || run.progress.case_path;
    if (run?.progress?.media_path) paths.working = paths.working || run.progress.media_path;
    return {
      run_name: fromResult?.run_name || run?.run_name || "",
      case_id: run?.case_id || form.case_id || "",
      paths,
    };
  }

  async function stopAndRemove(fromResult?: AcquisitionResult | null) {
    const runId = run?.run_id || run?.host_job_id;
    if (!runId || removing) return;
    const confirmed = window.confirm(
      "Stop this collection and permanently delete every file written for this run (original extract, working copy, exports, logs, hashes, and staging backup)? The case folder stays. This cannot be undone.",
    );
    if (!confirmed) return;
    setRemoving(true);
    setRun((prev) => (prev ? { ...prev, cancel_requested: true } : prev));
    const extras = removePayload(fromResult);
    try {
      if (isHostAcquisitionJobId(runId)) {
        const res = await removeHostAcquisitionJob(runId, extras);
        if (!res?.ok) {
          const fallback = await acquisitionApi.removeRun(runId, extras);
          if (!fallback.accepted) {
            toast.error(res?.error || fallback.message || "Could not stop and remove this collection");
            return;
          }
        }
        toast.success(res?.message || "Collection stopped and this run's files were removed");
        resetWizardAfterRemove();
        return;
      }
      const res = await acquisitionApi.removeRun(runId, extras);
      if (!res.accepted) {
        toast.error(res.message || "Could not stop and remove this collection");
        return;
      }
      toast.success(res.message);
      resetWizardAfterRemove();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Stop and remove failed");
    } finally {
      setRemoving(false);
    }
  }

  async function startAnalysis(override?: AcquisitionResult | null) {
    const payload = override || result;
    if (!payload || startingAnalysis) return;
    if (clientModeRef.current && !payload.client_import) {
      toast.error("This image is still only on this computer. Copy it to the forensic server first — processing runs there.");
      return;
    }
    // 05_Exports/<run> holds the four evidence packages (<run>.zip/.ufd/.pas/.ufdx);
    // analysis reads the .zip. 07_Logs/<run> carries export_catalog.json. 03_Working_Copy is usually empty and the
    // sealed original is a junction Docker cannot walk.
    const working = (
      payload.paths?.exports ||
      payload.paths?.working ||
      payload.paths?.original ||
      ""
    ).trim();
    let family = (
      payload.device_profile?.os_family ||
      preview?.device_profile?.os_family ||
      selected?.os_family ||
      ""
    ).toLowerCase();
    const nameHint = String(payload.run_name || "").toLowerCase();
    if (!family.includes("android") && !family.includes("ios")) {
      if (/android|galaxy|samsung|pixel|xiaomi|oppo|vivo|oneplus|adb|mtp/.test(nameHint)) {
        family = "android";
      } else if (/iphone|ipad|ios|apple/.test(nameHint)) {
        family = "ios";
      }
    }
    const mobileOs = fixedPlatform || (family.includes("android") ? "android" : family.includes("ios") ? "ios" : "other");
    setStartingAnalysis(true);
    try {
      const job = await forensicApi.createJob({
        type: fixedPlatform ? `${fixedPlatform}_mobile` : "mobile_extraction",
        domain_pack: "forensic",
        mobile_os: mobileOs,
        source_type: "mobile",
        acquisition_mode: "acquire",
        legal_authority_acknowledged: true,
        evidence_path: working || undefined,
      });
      toast.success("Analysis job created — registering the acquired folder");
      navigate(fixedPlatform ? `${mobileBase}/jobs/${job.id}` : `/forensic/jobs/${job.id}`, {
        state: {
          autoSelectFolder: !working,
          lockedSourceType: "mobile",
          mobileOs,
          prefillEvidencePath: working || undefined,
        },
      });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not start analysis");
      autoProcessRef.current = false;
    } finally {
      setStartingAnalysis(false);
    }
  }
  startAnalysisRef.current = startAnalysis;

  const importRunning = Boolean(
    clientImport && !["completed", "failed", "cancelled"].includes(clientImport.status),
  );

  async function stopClientCopy() {
    importAbortRef.current?.abort();
    if (!clientImport?.import_id) return;
    try {
      const res = await acquisitionApi.cancelClientImport(clientImport.import_id);
      (res.accepted ? toast.info : toast.error)(res.message);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not stop the copy");
    }
  }

  /** Requirement: image already on this computer → the forensic server copies it, then processes it. */
  async function pullFromClient(folder: ClientSelectedFolder) {
    setPullOpen(false);
    if (pulling) return;
    setPulling(true);
    setClientImport(null);
    setClientImportError("");
    setClientAgentMissing(null);
    try {
      importAbortRef.current = new AbortController();
      const state = await copyClientRunToServer({
        caseDir: folder.path,
        caseId: form.case_id.trim() || folder.name,
        scope: importScope,
        browser: folder.browser,
        onProgress: setClientImport,
        signal: importAbortRef.current.signal,
      });
      if (state.status === "completed" && state.run) {
        applyFinishedRun(
          state.run as AcquisitionRunDetail,
          `Image pulled from ${state.client_host || "this computer"} into the forensic server — ${state.run_name}`,
        );
        return;
      }
      setClientImportError(
        state.status === "cancelled"
          ? "The copy was stopped. Files already verified on the server are kept; pull the same folder again to continue."
          : state.error || "The copy to the forensic server did not finish.",
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Could not pull the image from this computer";
      setClientImportError(msg);
      if (e instanceof ClientAgentRequiredError) setClientAgentMissing(await clientAgentStatus(1_500));
      toast.error(msg);
    } finally {
      setPulling(false);
    }
  }

  /** Requirement: image already on the forensic server → use it there (no copy). */
  async function pullFromServer() {
    setClientMode(false);
    const caseId = form.case_id.trim();
    const root = form.case_root.trim() || "/evidence/cases";
    if (caseId) {
      const ok = await recoverFromDisk(root, caseId, { force: true });
      if (!ok) toast.error(`No finished collection for ${caseId} was found in the forensic server case folder.`);
      return;
    }
    setRecovering(true);
    try {
      const data = await acquisitionApi.recoverRecentRuns(root);
      const finished = (data.runs || []).find((r) => {
        const detail = r as AcquisitionRunDetail;
        return Boolean(detail.result) && (collectionSucceeded(detail.result) || filesCollected(detail.result) > 0);
      }) as AcquisitionRunDetail | undefined;
      if (!finished) {
        toast.error("No finished collection was found in the forensic server case folder. Enter its case ID and try again.");
        return;
      }
      applyFinishedRun(finished, `Collection loaded from the forensic server — ${finished.run_name}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not read the forensic server case folder");
    } finally {
      setRecovering(false);
    }
  }

  function renderClientCopy(where: "collect" | "pull") {
    const st = clientImport;
    const pct = st?.total_bytes
      ? Math.min(100, (Number(st.bytes_done || 0) / Number(st.total_bytes)) * 100)
      : st?.status === "completed"
        ? 100
        : 0;
    const retry = () => {
      if (where === "pull") {
        setPullOpen(true);
        return;
      }
      void recoverClientCopy(run?.case_id || form.case_id, { manual: true });
    };
    return (
      <div className="mb-4 mt-3 rounded-lg border border-emerald-200 bg-emerald-50/50 px-3 py-3 text-sm" data-testid="client-copy-status">
        <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-emerald-800">
          <ShieldCheck className="h-4 w-4" /> Forensic server copy
        </p>
        {where === "collect" && streamActive && (
          <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-ink-700" data-testid="client-stream-steps">
            <li>
              Every finished file of the collection on this computer{site?.localComputer ? ` (${site.localComputer})` : ""} is sent
              to the forensic server{site?.serverComputer ? ` (${site.serverComputer})` : ""} right away, several files at a time, and several pieces of a large file at once.
            </li>
            <li>The server checks each file with SHA-256 on its own disk and never overwrites one.</li>
            <li>
              {keepClientCopy || importScope !== "full"
                ? "A copy stays on this computer, as chosen."
                : "When the server has every file, the copy on this computer is removed. A receipt stays in the case folder."}
            </li>
          </ol>
        )}
        {where === "collect" && !streamActive && (
          <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-ink-700">
            <li>
              The image is created on this computer{site?.localComputer ? ` (${site.localComputer})` : ""} in{" "}
              <span className="font-mono">{clientCaseRoot}</span>.
            </li>
            <li>
              The forensic server{site?.serverComputer ? ` (${site.serverComputer})` : ""} then copies it into its own case
              folder and checks every file with SHA-256. Both copies are kept; processing runs on the server.
            </li>
          </ol>
        )}
        {st ? (
          <>
            <p className="mt-2 font-medium text-ink-900">{st.detail}</p>
            <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-emerald-100">
              <div
                className="h-full rounded-full bg-emerald-500 transition-[width] duration-700 ease-out"
                style={{ width: `${Math.max(2, pct)}%` }}
              />
            </div>
            <div className="mt-2 grid gap-x-6 gap-y-1 text-xs text-ink-600 sm:grid-cols-2">
              <span>
                {st.transport === "client_stream" ? "Sent" : "Copied"}:{" "}
                <strong className="text-ink-800">{formatImportBytes(st.bytes_done)}</strong> of{" "}
                {formatImportBytes(st.total_bytes)}
              </span>
              <span>
                Files: <strong className="text-ink-800">{Number(st.files_done || 0).toLocaleString()}</strong> of{" "}
                {Number(st.total_files || 0).toLocaleString()}
                {st.files_present ? ` (${st.files_present.toLocaleString()} already on the server)` : ""}
              </span>
              <span className="break-all">
                From: <span className="font-mono">{st.client_host} {st.client_case_dir}</span>
              </span>
              <span>
                Connection:{" "}
                {st.transport === "relay"
                  ? "this computer's outbound connection"
                  : st.transport === "client_stream"
                    ? "sent by this computer while collecting"
                    : "office network"}
              </span>
              {st.server_case_dir && (
                <span className="break-all sm:col-span-2">
                  Server case folder: <span className="font-mono">{st.server_case_dir}</span>
                </span>
              )}
            </div>
            {importRunning && (
              <div className="mt-2">
                <Button variant="outline" onClick={() => void stopClientCopy()}>
                  <StopCircle className="h-4 w-4" /> {st.transport === "client_stream" ? "Stop sending" : "Stop copy"}
                </Button>
              </div>
            )}
          </>
        ) : where === "collect" && streamActive && !clientImportError ? (
          <p className="mt-2 text-ink-700" data-testid="client-stream-progress">
            {streamProgressLine(streamState, streamUploader)}
          </p>
        ) : where === "collect" && clientCollected && !clientImportError ? (
          <p className="mt-2 text-ink-700">Collection sealed on this computer. Preparing the copy to the forensic server…</p>
        ) : where === "pull" && pulling ? (
          <p className="mt-2 text-ink-700">Sharing the folder read-only with the forensic server…</p>
        ) : null}
        {clientImportError && (
          <div className="mt-2">
            <Notice tone="error" title="The copy to the forensic server did not finish">
              <p>{clientImportError}</p>
              <div className="flex flex-wrap gap-2 pt-2">
                <Button variant="brand" disabled={importRunning || pulling} onClick={retry}>
                  <RefreshCw className="h-4 w-4" /> Copy to the forensic server again
                </Button>
                {clientAgentMissing ? (
                  <Button variant="outline" disabled={importRunning || pulling} onClick={() => setPullOpen(true)}>
                    <Laptop className="h-4 w-4" /> Copy through this browser instead (no install)
                  </Button>
                ) : null}
              </div>
              {clientAgentMissing ? (
                <p className="pt-2 text-xs">
                  Without the agent: choose the case folder in this browser, and keep this tab open until the copy
                  finishes (it goes through this computer&apos;s upload). For large images the agent is faster and keeps
                  copying when the tab is closed.
                </p>
              ) : null}
            </Notice>
          </div>
        )}
        {clientAgentMissing && (
          <div className="mt-2">
            <ClientAgentSetup status={clientAgentMissing} checking={false} onRetry={retry} />
          </div>
        )}
      </div>
    );
  }

  useEffect(() => {
    const isAndroid = String(result?.device_profile?.os_family || selected?.os_family || "").toLowerCase().includes("android");
    if (step !== "complete" || !result || !run?.run_id || !isAndroid || Boolean(run.host_job)) {
      setWaKeyItems([]);
      setWaKeyRevealed(false);
      return;
    }
    let active = true;
    acquisitionApi.getAndroidWhatsAppKeys(run.run_id, false)
      .then((data) => { if (active) setWaKeyItems(data.items); })
      .catch(() => { if (active) setWaKeyItems([]); });
    return () => { active = false; };
  }, [step, result, run?.run_id, selected?.os_family]);

  return (
    <div>
      <PageHeader
        title={fixedPlatformLabel ? `${fixedPlatformLabel} device → image` : "Acquire from device"}
        subtitle={fixedPlatformLabel
          ? `Only ${fixedPlatformLabel} devices are shown here. The resulting evidence remains in the ${fixedPlatformLabel} backend for extraction and RAG.`
          : "Plug a phone into this PC — it is detected automatically. iOS Agent owns iPhone extract and RAG; Android Agent owns Android extract and RAG. The two never mix."}
        actions={
          <Button variant="ghost" onClick={() => navigate(fixedPlatform ? mobileBase : "/forensic/mobile")}>
            <ArrowLeft className="h-4 w-4" /> Back
          </Button>
        }
      />

      <StepRail current={step} />

      {step !== "complete" && step !== "collect" && (
        <UsbCollectionBusyBanner
          show={usbBusy}
          className="mb-4"
          onStopped={() => setUsbBusy(false)}
        />
      )}

      {/* ---------------- Step 1: connect ---------------- */}
      <Card className="mb-4 p-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Cable className="h-5 w-5 text-brand-600" />
            <h3 className="font-semibold text-ink-800">Device connection</h3>
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              loading={recovering}
              disabled={recovering}
              onClick={() =>
                void (async () => {
                  const jobs = (await listHostAcquisitionJobs()).filter((j) => {
                    const status = String(j.status || "").toLowerCase();
                    return (
                      (status === "completed" || status === "failed") &&
                      Number(j.files_seen || 0) > 0
                    );
                  });
                  const androidJobs = jobs.filter((j) =>
                    /android|oppo|samsung|pixel|xiaomi|oneplus|adb|mtp/i.test(
                      `${j.run_name || ""} ${j.case_id || ""}`,
                    ),
                  );
                  const pick =
                    androidJobs.sort((a, b) => Number(b.files_seen || 0) - Number(a.files_seen || 0))[0] ||
                    jobs.sort((a, b) => Number(b.files_seen || 0) - Number(a.files_seen || 0))[0];
                  const caseId = form.case_id.trim() || pick?.case_id || "";
                  if (!caseId) {
                    toast.error("No finished collection was found on this PC.");
                    return;
                  }
                  const ok = await recoverFromDisk(form.case_root || undefined, caseId);
                  if (!ok && pick?.job_id) {
                    await getHostAcquisitionJob(pick.job_id).then((job) => {
                      const hostResult = (job?.result || {}) as AcquisitionResult;
                      if (hostResult && (collectionSucceeded(hostResult) || hostResult.run_name)) {
                        applyFinishedRun({
                          run_id: pick.job_id,
                          run_name: String(hostResult.run_name || pick.run_name || caseId),
                          status: "completed",
                          case_id: caseId,
                          result: hostResult,
                        } as AcquisitionRunDetail);
                      }
                    });
                  }
                })()
              }
            >
              <RefreshCw className="h-4 w-4" /> Reload finished collection
            </Button>
            <Button variant="ghost" onClick={() => void loadAdapters()}>
              <RefreshCw className="h-4 w-4" /> Adapters
            </Button>
            <Button variant="brand" loading={scanning} onClick={() => void scan()}>
              {scanning ? "Scanning…" : devices.length ? "Rescan" : "Scan now"}
            </Button>
          </div>
        </div>

        <div className="mb-3 flex flex-wrap gap-2">
          {adapters.map((a) => (
            <span key={a.name} title={a.reason} className="inline-flex">
              <Badge tone={a.available ? "green" : "neutral"}>
                {a.name} {a.available ? "ready" : "unavailable"}
              </Badge>
            </span>
          ))}
        </div>

        {(() => {
          const setupState = clientSetupState({
            localHelper,
            helperOnline,
            serverHelperVersion,
            officeHost: isBrowserOnOfficeHost(),
          });
          return setupState ? (
            <div className="mb-3">
              <ClientSetupPanel
                state={setupState}
                localVersion={localHelper?.version}
                serverVersion={serverHelperVersion}
              />
            </div>
          ) : null;
        })()}

        {detectWarnings.length > 0 && (
          <div className="mb-3">
            <Notice tone="warn" title="Detection limitations">
              <ul className="list-disc pl-4">
                {detectWarnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
              <p className="pt-1 text-xs">
                A device type whose adapter is unavailable cannot be detected. Absence from this
                list does not mean no such device is connected.
              </p>
            </Notice>
          </div>
        )}

        {devices.length === 0 ? (
          <div className="grid gap-1" data-testid="no-phone-connected">
            <p className="text-sm text-ink-600">{noPhoneText(fixedPlatform)}</p>
            {lastScanAt ? (
              <p className="text-xs text-ink-500">
                Checked {lastScanAt.toLocaleTimeString()}
                {localHelper && !localHelper.outdated && !localHelper.stale
                  ? localHelper.live
                    ? " · this computer's USB devices read live"
                    : " · this computer's phone list"
                  : ""}
                . The list follows the cable: a phone appears here within a few seconds of being plugged in and
                unlocked, and disappears when it is unplugged.
              </p>
            ) : null}
          </div>
        ) : (
          <div className="grid gap-3">
            {devices.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-2">
            {devices.map((d) => {
              const active = selected?.device_id === d.device_id && selected?.adapter === d.adapter;
              const kind = mobileKindLabel(d.os_family);
              const owner = ownerAgentForFamily(d.os_family);
              if (deviceNeedsPhoneAction(d)) {
                // V1.31.7: on the cable, but the phone must be switched to a USB mode Windows can read first.
                return (
                  <div
                    key={`${d.adapter}:${d.device_id}`}
                    data-testid="phone-needs-action"
                    className="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-left"
                  >
                    <Smartphone className="mt-0.5 h-5 w-5 shrink-0 text-amber-700" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-medium text-ink-800">{d.label || "Android phone"}</p>
                      <p className="text-xs text-ink-500">
                        {kind} · {mobileAttachHostLabel(d.attach_host)} · {phoneActionLabel(d)}
                      </p>
                      <p className="mt-1 text-xs text-amber-800">
                        {d.guidance ||
                          "Unlock the phone, open the USB notification and choose File transfer, then Rescan."}
                      </p>
                    </div>
                    <Badge tone="amber">Action needed</Badge>
                  </div>
                );
              }
              return (
                <button
                  key={`${d.adapter}:${d.device_id}`}
                  onClick={() => void identify(d)}
                  className={[
                    "flex items-center gap-3 rounded-lg border p-3 text-left transition",
                    active
                      ? "border-brand-400 bg-brand-50"
                      : "border-ink-100 bg-white hover:border-brand-200",
                  ].join(" ")}
                >
                  <Smartphone className="h-5 w-5 text-brand-600" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-ink-800">{d.label || d.device_id}</p>
                    <p className="text-xs text-ink-500">
                      {kind} · {owner ? `${owner.label} · ` : ""}
                      {mobileAttachHostLabel(d.attach_host)}
                      {d.status ? ` · ${d.status}` : ""}
                    </p>
                  </div>
                  <Badge tone={kind === "iPhone / iPad" ? "green" : kind === "Android" ? "green" : "neutral"}>
                    {kind}
                  </Badge>
                  {previewing && active ? <Spinner className="ml-auto h-4 w-4" /> : null}
                </button>
              );
            })}
            </div>
            )}
          </div>
        )}
      </Card>

      {/* ---------------- Pull an existing image (client or server) ---------------- */}
      {step === "connect" && !running && (
        <Card className="mb-4 p-5">
          <div className="mb-1 flex items-center gap-2">
            <FolderOpen className="h-5 w-5 text-brand-600" />
            <h3 className="font-semibold text-ink-800">Pull an existing image</h3>
          </div>
          <p className="mb-3 text-sm text-ink-600">
            Already collected? Take the image from where it is. Registration, parsing, RAG and the report always run on
            the forensic server.
          </p>
          <div className="mb-3 grid gap-3 sm:grid-cols-2">
            <Field label="Case ID (optional)" hint="Picks this case's collection; leave empty for the newest.">
              <Input
                value={form.case_id}
                onChange={(e) => setForm({ ...form, case_id: e.target.value })}
                placeholder="CASE-2026-001"
              />
            </Field>
            <Field label="What to copy from this computer" hint="Applies to images on this computer only.">
              <Select value={importScope} onChange={(e) => setImportScope(e.target.value as ClientImportScope)}>
                <option value="full">Whole run (original extraction, working copy, packages, logs, hashes)</option>
                <option value="packages">Evidence packages, logs and hashes only</option>
              </Select>
            </Field>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <button
              type="button"
              disabled={pulling || importRunning}
              onClick={() => setPullOpen(true)}
              className="flex items-start gap-3 rounded-lg border border-ink-200 bg-white p-3 text-left transition hover:border-brand-300 hover:bg-brand-50/40 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Laptop className="mt-0.5 h-5 w-5 shrink-0 text-brand-600" />
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-ink-800">From this computer</span>
                <span className="mt-0.5 block text-xs text-ink-500">
                  Browse this computer (for example {clientCaseRoot}\&lt;CASE&gt;). The forensic server copies the run
                  into its case folder and verifies every file before processing.
                </span>
              </span>
            </button>
            <button
              type="button"
              disabled={recovering || pulling}
              onClick={() => void pullFromServer()}
              className="flex items-start gap-3 rounded-lg border border-ink-200 bg-white p-3 text-left transition hover:border-brand-300 hover:bg-brand-50/40 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Server className="mt-0.5 h-5 w-5 shrink-0 text-brand-600" />
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-ink-800">From the forensic server</span>
                <span className="mt-0.5 block text-xs text-ink-500">
                  A collection already in the server case folder ({form.case_root || "/evidence/cases"}). Used in
                  place; nothing is copied.
                </span>
              </span>
            </button>
          </div>
          {(pulling || clientImport || clientImportError || clientAgentMissing) && !clientMode && renderClientCopy("pull")}
        </Card>
      )}

      <Modal
        open={pullOpen}
        onClose={() => setPullOpen(false)}
        title="Pull an image from this computer"
        size="lg"
        draggable
        footer={
          <Button variant="ghost" onClick={() => setPullOpen(false)}>
            Cancel
          </Button>
        }
      >
        <div className="space-y-3 text-sm text-ink-700">
          <p className="text-ink-600">
            Choose the case folder of the collection (for example <span className="font-mono">{clientCaseRoot}\CASE-2026-001</span>,
            the folder that holds 02_Original_Extraction and 07_Logs). The forensic server copies the newest sealed run
            into its own case folder, checks every file with SHA-256, and processes it there. The folder on this computer
            is opened read-only and is not changed.
          </p>
          {pullOpen ? (
            <ClientEvidenceBrowser
              mobile
              actionLabel="Copy this folder to the forensic server"
              listMaxHeight="max-h-56"
              allowBrowserFiles={false}
              onUseFolder={(folder) => void pullFromClient(folder)}
            />
          ) : null}
        </div>
      </Modal>

      {/* ---------------- Step 2: identify ---------------- */}
      {preview && (
        <Card className="mb-4 p-5">
          <h3 className="mb-3 font-semibold text-ink-800">Device profile and capability</h3>

          <div className="mb-4 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-3">
            {(
              [
                ["Device", preview.device_profile.device_label],
                ["OS", `${preview.device_profile.os_family} ${preview.device_profile.os_version}`],
                [
                  preview.device_profile.os_family === "ios" ? "Build" : "Security patch",
                  preview.device_profile.os_family === "ios"
                    ? (preview.device_profile.build_id || preview.device_profile.security_patch_level || "not determined")
                    : (preview.device_profile.security_patch_level || "not determined"),
                ],
                ["Serial / IMEI", preview.device_profile.imei || preview.device_profile.serial || preview.device_profile.udid],
                ["Lock state", preview.device_profile.lock_state],
                ["Encryption", preview.device_profile.encryption_state],
                ["Connection", preview.device_profile.connection_mode],
                ["Chipset", preview.device_profile.chipset || "not determined"],
                ["Battery", preview.device_profile.battery_percent != null ? `${preview.device_profile.battery_percent}%` : "unknown"],
              ] as [string, string][]
            ).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-3 border-b border-ink-50 py-1">
                <span className="text-ink-500">{k}</span>
                <span className="truncate font-medium text-ink-800">{v || "—"}</span>
              </div>
            ))}
          </div>

          <div className="mb-3 flex items-center gap-2">
            <Badge tone={supported.length ? "green" : "amber"}>
              {preview.capability?.capability_label ?? "Capability unknown"}
            </Badge>
            {preview.device_profile?.rooted_or_jailbroken ? (
              <Badge tone="amber">Rooted / jailbroken</Badge>
            ) : null}
          </div>

          {(preview.device_profile?.unknown_fields?.length ?? 0) > 0 && (
            <div className="mb-3">
              <Notice tone="warn" title="Device state not fully determined">
                <p>
                  Could not determine: {(preview.device_profile?.unknown_fields ?? []).join(", ")}. These
                  must be recorded as limitations. Do not report an undetermined lock state as
                  &ldquo;unlocked&rdquo;.
                </p>
              </Notice>
            </div>
          )}

          {(preview.capability?.warnings?.length ?? 0) > 0 && (
            <div className="mb-3">
              <Notice tone="warn" title="Capability warnings">
                <ul className="list-disc pl-4">
                  {(preview.capability?.warnings ?? []).map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </Notice>
            </div>
          )}

          {(preview.preparation_steps?.length ?? 0) > 0 && (
            <div className="mb-3">
              <Notice tone="info" title="Device preparation required">
                <ol className="list-decimal pl-4">
                  {(preview.preparation_steps ?? []).map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ol>
              </Notice>
            </div>
          )}

          {Object.keys(preview.capability?.blocked_methods ?? {}).length > 0 && (
            <details className="rounded-lg border border-ink-100 bg-ink-50/60 px-3 py-2 text-sm">
              <summary className="cursor-pointer font-medium text-ink-700">
                Methods not available on this device (
                {Object.keys(preview.capability?.blocked_methods ?? {}).length})
              </summary>
              <ul className="mt-2 space-y-1.5">
                {Object.entries(preview.capability?.blocked_methods ?? {}).map(([m, why]) => (
                  <li key={m}>
                    <span className="font-medium text-ink-800">{labelOf(m)}</span>
                    <span className="text-ink-600"> — {why}</span>
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-ink-500">
                An unavailable method is a capability gap. It must be reported as a limitation, not
                as an absence of evidence on the device.
              </p>
            </details>
          )}

          {step === "identify" && (
            <div className="mt-4 flex justify-end">
              <Button variant="brand" onClick={() => setStep("authorise")}>
                Continue to authorisation
              </Button>
            </div>
          )}
        </Card>
      )}

      {/* ---------------- Step 3: authorise ---------------- */}
      {preview && (step === "authorise" || step === "collect" || step === "complete") && (
        <Card className="mb-4 p-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-semibold text-ink-800">Authority, objective and method</h3>
            {step === "authorise" && (
              <Button variant="ghost" onClick={() => refillFromDevice(true)}>
                <Wand2 className="h-4 w-4" /> Fill from device
              </Button>
            )}
          </div>
          {step === "authorise" && selectedSite === "client" && (
            <div className="mb-4">
              <Notice tone="info" title="Phone on this computer — image here first, then on the forensic server">
                <p>
                  This phone is connected to {site?.localComputer || "this computer"}, not to the forensic server. The
                  image is created on this computer in <span className="font-mono">{clientCaseRoot || DEFAULT_CLIENT_CASE_ROOT}</span>{" "}
                  and then copied to the forensic server{site?.serverComputer ? ` (${site.serverComputer})` : ""} into its
                  case folder as usual, with every file checked by SHA-256. Both copies are kept. Analysis runs on the server.
                </p>
                <p className="pt-1">
                  Needs, on this computer: the HostDrive helper (USB collection) and the Client Evidence Agent (lets the
                  server read the finished image). Keep this computer on until the copy is done.
                </p>
              </Notice>
            </div>
          )}
          {step === "authorise" && (
            <div className="mb-4">
              <Notice tone="info" title="Autofilled from the connected handset">
                <p>
                  Case ID suggestion, evidence ID (IMEI/serial), device condition, case root and
                  examiner notes come from the device profile. Enter legal authority, cable asset ID
                  and backup password yourself — those are not on the handset.
                </p>
              </Notice>
            </div>
          )}

          <div className="mb-5 grid gap-4 sm:grid-cols-2">
            <Field label="Case ID" hint="Used in the extraction run name. Suggested from model + date; edit if your case file differs.">
              <Input
                value={form.case_id}
                onChange={(e) => setForm({ ...form, case_id: e.target.value })}
                placeholder="CASE-2026-001"
              />
            </Field>
            <Field label="Evidence ID" hint="Exhibit reference — suggested from IMEI/serial.">
              <Input
                value={form.evidence_id}
                onChange={(e) => setForm({ ...form, evidence_id: e.target.value })}
                placeholder="E01"
              />
            </Field>
            <Field label="Legal authority" hint="Warrant, consent or order reference. Required — not taken from the device.">
              <Input
                value={form.legal_authority}
                onChange={(e) => setForm({ ...form, legal_authority: e.target.value })}
                placeholder="Warrant 2026/SW/4471"
              />
            </Field>
            {selectedSite === "client" ? (
              <>
                <Field
                  label="Image folder on this computer"
                  hint="The case folder (01–10 layout) is created here first, then copied to the forensic server."
                >
                  <Input
                    value={clientCaseRoot}
                    onChange={(e) => setClientCaseRoot(e.target.value)}
                    placeholder={DEFAULT_CLIENT_CASE_ROOT}
                  />
                </Field>
                <Field
                  label="Send to the forensic server"
                  hint="Sent while the phone is collected, file by file. Whole run matches a collection made on the server; packages only sends less over a slow link."
                >
                  <Select value={importScope} onChange={(e) => setImportScope(e.target.value as ClientImportScope)}>
                    <option value="full">Whole run (original extraction, working copy, packages, logs, hashes)</option>
                    <option value="packages">Evidence packages, logs and hashes only</option>
                  </Select>
                </Field>
                <label className="flex items-start gap-2 text-sm text-ink-700 sm:col-span-2" data-testid="keep-client-copy">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={keepClientCopy || importScope !== "full"}
                    disabled={importScope !== "full"}
                    onChange={(e) => setKeepClientCopy(e.target.checked)}
                  />
                  <span>
                    Keep a copy on this computer
                    <span className="block text-xs text-ink-500">
                      Off: the image is moved to the forensic server. The copy here is removed only after the server
                      has checked every file (SHA-256); a receipt stays in the case folder.
                    </span>
                  </span>
                </label>
              </>
            ) : (
              <Field label="Case root" hint="Encrypted case workspace. The run folder is created here.">
                <Input
                  value={form.case_root}
                  onChange={(e) => setForm({ ...form, case_root: e.target.value })}
                  placeholder="/evidence/cases"
                />
              </Field>
            )}
            <Field label="Cable / adapter asset ID" hint="Lab inventory ID for the validated cable — enter manually.">
              <Input
                value={form.cable_adapter_asset_id}
                onChange={(e) => setForm({ ...form, cable_adapter_asset_id: e.target.value })}
                placeholder="CBL-USBC-014"
              />
            </Field>
            <Field label="Device condition on receipt" hint="Derived from lock state, battery, pairing and SIM.">
              <Input
                value={form.device_condition}
                onChange={(e) => setForm({ ...form, device_condition: e.target.value })}
                placeholder="Powered on, AFU, screen intact"
              />
            </Field>
          </div>

          {preview.device_profile.os_family === "ios" && (
            <div className="mb-5">
              <Field
                label="Backup password"
                hint="An ENCRYPTED backup includes keychain, Health and Safari history. An unencrypted backup omits them entirely."
              >
                <Input
                  type="password"
                  value={form.backup_password}
                  onChange={(e) => setForm({ ...form, backup_password: e.target.value })}
                  placeholder="Set a known password and record it in the case file"
                />
              </Field>
              {!form.backup_password && (
                <div className="mt-2">
                  <Notice tone="warn" title="Unencrypted backup will omit evidence categories">
                    <p>
                      Without a backup password, the keychain, Health data, Safari history and call
                      history will be absent from this extraction as a direct consequence of the
                      backup type — not because the data was absent on the device.
                    </p>
                  </Notice>
                </div>
              )}
            </div>
          )}

          {preview.device_profile.os_family === "android" && (
            <div className="mb-5 rounded-lg border border-ink-100 bg-ink-50/50 p-3">
              <h4 className="mb-2 text-sm font-semibold text-ink-800">WhatsApp decryption</h4>
              <p className="mb-3 text-xs text-ink-500">
                The decrypt agent recovers the WhatsApp database <span className="font-semibold">without
                altering the device</span>, trying these in order: (1) an existing encrypted backup
                with the 64-digit key below; (2) an existing backup with the device key file
                (files/key) captured by a rooted/full-file-system extraction; (3) the live
                msgstore.db read directly when root is already present. No key is ever generated or
                guessed.
              </p>
              <Field
                label="64-digit WhatsApp key (optional)"
                hint="The key WhatsApp shows under the 64-digit backup option, or an already-known root key. Decrypts an existing crypt15 backup. Stored with Intake; never shown in reports or logs."
              >
                <Input
                  type="password"
                  value={form.wa_64_digit_key}
                  onChange={(e) => setForm({ ...form, wa_64_digit_key: e.target.value })}
                  placeholder="64 hexadecimal characters"
                />
              </Field>
              {form.wa_64_digit_key.trim() &&
                !/^[0-9a-fA-F]{64}$/.test(form.wa_64_digit_key.replace(/[\s:-]/g, "")) && (
                  <p className="mt-1 text-xs text-amber-700">
                    Expected 64 hexadecimal characters (spaces and dashes are ignored).
                  </p>
                )}

              <p className="mt-3 text-xs text-ink-500">After Android extraction completes, Polaron automatically scans the acquired folders for msgstore.db.crypt15 and tries read-only offline decryption using any matching previously acquired key. Missing keys are recorded as an evidence limitation; no action on the phone is attempted.</p>

            </div>
          )}

          <div className="mb-5 rounded-lg border border-ink-100 bg-ink-50/50 p-3">
            <h4 className="mb-2 text-sm font-semibold text-ink-800">Cloud mailboxes (optional)</h4>
            <p className="mb-3 text-xs text-ink-500">
              Device USB only sees on-handset mail. For full Gmail/Outlook cloud mailboxes, enter an
              app password (IMAP). Used for this run only.
            </p>
            <div className="grid gap-3 md:grid-cols-3">
              <Field label="Provider">
                <select
                  className="w-full rounded-md border border-ink-200 bg-white px-3 py-2 text-sm"
                  value={form.cloud_mail_provider}
                  onChange={(e) => setForm({ ...form, cloud_mail_provider: e.target.value })}
                >
                  <option value="gmail">Gmail</option>
                  <option value="outlook">Outlook / Microsoft 365</option>
                  <option value="imap">Other IMAP</option>
                </select>
              </Field>
              <Field label="Email address">
                <Input
                  value={form.cloud_mail_address}
                  onChange={(e) => setForm({ ...form, cloud_mail_address: e.target.value })}
                  placeholder="user@gmail.com"
                />
              </Field>
              <Field label="App password">
                <Input
                  type="password"
                  value={form.cloud_mail_secret}
                  onChange={(e) => setForm({ ...form, cloud_mail_secret: e.target.value })}
                  placeholder="App password"
                />
              </Field>
            </div>
          </div>

          <div className="mb-5">
            <p className="mb-2 text-sm font-medium text-ink-700">
              Authorised objective — what must this collection establish?
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {OBJECTIVES.map((o) => {
                const checked = objectives.includes(o.id);
                const reachable = o.requires.some((m) => supported.includes(m));
                return (
                  <label
                    key={o.id}
                    className={[
                      "flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-sm",
                      checked ? "border-brand-300 bg-brand-50" : "border-ink-100 bg-white",
                      reachable ? "" : "opacity-70",
                    ].join(" ")}
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={checked}
                      onChange={(e) =>
                        setObjectives(
                          e.target.checked
                            ? [...objectives, o.id]
                            : objectives.filter((x) => x !== o.id),
                        )
                      }
                    />
                    <span className="min-w-0">
                      <span className="font-medium text-ink-800">{o.label}</span>
                      <span className="block text-xs text-ink-500">{o.note}</span>
                      {!reachable && (
                        <span className="mt-1 block text-xs font-medium text-amber-700">
                          Not achievable on this device with the available methods.
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>

          {unmetObjectives.length > 0 && (
            <div className="mb-4">
              <Notice tone="warn" title="Selected objectives cannot be met by any available method">
                <ul className="list-disc pl-4">
                  {unmetObjectives.map((o) => (
                    <li key={o.id}>{o.label}</li>
                  ))}
                </ul>
                <p className="pt-1">
                  Proceeding is permitted, but the report must state that these categories were not
                  collectable. Do not present them as absent from the device.
                </p>
              </Notice>
            </div>
          )}

          <div className="mb-5">
            <Field
              label="Method"
              hint="Leave on automatic to apply the least intrusive method sufficient for the objective. An override is recorded as a documented deviation."
            >
              <Select value={methodOverride} onChange={(e) => setMethodOverride(e.target.value)}>
                <option value="">
                  Automatic — least intrusive sufficient
                  {satisfiable.length ? ` (${labelOf(satisfiable[0])})` : ""}
                </option>
                {supported.map((m) => (
                  <option key={m} value={m}>
                    {labelOf(m)}
                    {!satisfiable.includes(m) ? " — exceeds the stated objective" : ""}
                  </option>
                ))}
              </Select>
            </Field>

            {methodOverride && preview.method_profiles[methodOverride] && (
              <div className="mt-3 space-y-2">
                <Notice tone="info" title={`${labelOf(methodOverride)} — coverage`}>
                  <p>{preview.method_profiles[methodOverride].coverage}</p>
                  <p className="pt-1">
                    <strong>Limitations:</strong>{" "}
                    {preview.method_profiles[methodOverride].limitations}
                  </p>
                  <p>
                    <strong>Deleted data:</strong>{" "}
                    {preview.method_profiles[methodOverride].deleted_data}
                  </p>
                </Notice>
                <Notice tone="warn" title="Method override will be recorded as a deviation">
                  <p>
                    Automatic selection applies the least intrusive sufficient method. Overriding it
                    is permitted where legally authorised and technically necessary, and is written
                    to the audit trail.
                  </p>
                </Notice>
              </div>
            )}
          </div>

          <Field label="Examiner notes" hint="Recorded in the evidence package.">
            <Input
              value={form.examiner_notes}
              onChange={(e) => setForm({ ...form, examiner_notes: e.target.value })}
              placeholder="Airplane mode engaged at intake."
            />
          </Field>

          <label className="mt-4 flex items-start gap-3 rounded-lg border border-ink-100 bg-ink-50/60 px-3 py-3 text-sm text-ink-700">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={form.network_isolated}
              onChange={(e) => setForm({ ...form, network_isolated: e.target.checked })}
            />
            <span>
              The device is isolated from all networks (airplane mode, Faraday bag or shielded
              enclosure). Without isolation, remote wipe and message sync remain possible during
              collection.
            </span>
          </label>

          <label className="mt-2 flex items-start gap-3 rounded-lg border border-ink-100 bg-ink-50/60 px-3 py-3 text-sm text-ink-700">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={form.legal_ack}
              onChange={(e) => setForm({ ...form, legal_ack: e.target.checked })}
            />
            <span>
              I confirm lawful authority and authorised scope for this collection. Locked-device
              bypass is not provided; specialist hand-off is required where access cannot be
              obtained lawfully.
            </span>
          </label>

          {step === "authorise" && (
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setStep("identify")}>
                Back
              </Button>
              <Button
                variant="brand"
                loading={starting}
                disabled={starting}
                onClick={() => void startAcquisition()}
              >
                Start collection
              </Button>
            </div>
          )}
        </Card>
      )}

      {/* ---------------- Step 4: collect ---------------- */}
      {step === "collect" && run && (
        <Card className="mb-4 p-6">
          <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
            <div className="flex items-start gap-3">
              {running ? (
                <Loader2 className="mt-0.5 h-5 w-5 animate-spin text-brand-600" />
              ) : (
                <CheckCircle2 className="mt-0.5 h-5 w-5 text-emerald-600" />
              )}
              <div>
                <p className="font-semibold text-ink-800">
                  {run.cancel_requested
                    ? "Stopping collection…"
                    : clientMode && importRunning && clientImport?.transport === "client_stream"
                      ? "Collected on this computer — sending the last files to the forensic server"
                      : clientMode && importRunning
                      ? "Collected on this computer — copying the image to the forensic server"
                      : clientMode && clientCollected && clientImportError
                        ? "Collected on this computer — the copy to the forensic server did not finish"
                        : clientMode && clientCollected
                          ? "Collected on this computer — preparing the server copy"
                          : recovering
                      ? "Checking case folder for a finished collection…"
                      : running && clientMode && streamActive
                        ? "Collection in progress — sent to the forensic server as it runs"
                      : running
                        ? "Collection in progress"
                        : "Collection status unknown — recover if the phone backup already finished"}
                </p>
                <p className="mt-0.5 font-mono text-xs text-ink-500">
                  {run.run_name || run.run_id}
                </p>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                loading={recovering}
                disabled={recovering || importRunning}
                onClick={() =>
                  void (clientMode
                    ? recoverClientCopy(run.case_id || form.case_id || undefined, { manual: !streamActive })
                    : recoverFromDisk(
                        form.case_root || undefined,
                        run.case_id || form.case_id || undefined,
                      )
                  ).then((ok) => {
                    if (!ok && !clientMode) {
                      toast.error(
                        "No sealed collection found yet for this case. Wait for the host backup to finish, then try again.",
                      );
                    }
                  })
                }
              >
                {clientMode ? <Laptop className="h-4 w-4" /> : <RefreshCw className="h-4 w-4" />}
                {clientMode && streamActive ? "Check the server copy" : clientMode ? "Copy to the forensic server" : "Recover from case folder"}
              </Button>
              <Button
                variant="outline"
                loading={cancelling}
                disabled={cancelling || removing || run.cancel_requested || !running}
                onClick={() => void cancelRun()}
              >
                <StopCircle className="h-4 w-4" />
                {run.cancel_requested && !removing ? "Cancelling…" : "Cancel collection"}
              </Button>
              <Button
                variant="danger"
                loading={removing}
                disabled={cancelling || removing}
                onClick={() => void stopAndRemove()}
              >
                <Trash2 className="h-4 w-4" />
                {removing ? "Removing…" : "Stop and remove"}
              </Button>
            </div>
          </div>

          <div className="mb-4 rounded-lg border border-brand-200 bg-brand-50/70 px-3 py-3 text-sm">
            <p className="text-xs font-semibold uppercase tracking-wide text-brand-800">
              {ownerAgentForFamily(selected?.os_family || preview?.device_profile?.os_family)?.label
                || "Phone agent"}{" "}
              {run.progress?.stage === "complete" ? "finished on this computer" : "currently fetching"}
            </p>
            <p className="mt-1 font-medium text-ink-900">
              {run.progress?.category || "Preparing"}
              {run.progress?.detail ? ` — ${run.progress.detail}` : ""}
            </p>
            {run.progress?.item && run.progress.item !== "collecting" && (
              <p className="mt-1 truncate font-mono text-xs text-ink-500" title={run.progress.item}>
                Last file: {run.progress.item}
              </p>
            )}
          </div>

          {clientMode && renderClientCopy("collect")}

          <div className="mb-4 rounded-lg border border-ink-100 bg-ink-50/80 px-3 py-3 text-sm">
            <p className="text-xs font-semibold uppercase tracking-wide text-ink-600">
              Live collection on this workstation
            </p>
            <div className="mt-2 grid gap-x-6 gap-y-1 sm:grid-cols-2">
              <p>
                <span className="text-ink-500">Written: </span>
                <strong className="text-ink-800">{formatCollectedBytes(Number(run.progress?.bytes_done || 0))}</strong>
              </p>
              <p>
                <span className="text-ink-500">Files: </span>
                <strong className="text-ink-800">
                  {Number(run.progress?.files_seen || 0).toLocaleString()}
                </strong>
              </p>
              <p>
                <span className="text-ink-500">Overall progress: </span>
                <strong className="text-ink-800">
                  {run.progress?.progress_pct != null ? `${run.progress.progress_pct}%` : "starting"}
                </strong>
              </p>
              <p>
                <span className="text-ink-500">Updated: </span>
                <strong className="text-ink-800">
                  {run.progress?.updated_utc
                    ? new Date(run.progress.updated_utc).toLocaleTimeString()
                    : "—"}
                </strong>
              </p>
            </div>
            {run.progress?.output_path && (
              <p className="mt-2 break-all font-mono text-xs text-ink-800">
                <span className="font-sans font-medium text-ink-600">Current write/staging location: </span>
                {run.progress.output_path}
              </p>
            )}
            {run.progress?.media_path && run.progress.media_path !== run.progress.output_path && (
              <p className="mt-1 break-all font-mono text-xs text-ink-800">
                <span className="font-sans font-medium text-ink-600">Case image folder: </span>
                {run.progress.media_path}
              </p>
            )}
            {run.progress?.case_path && (
              <p className="mt-1 break-all font-mono text-xs text-ink-800">
                <span className="font-sans font-medium text-ink-600">Case folder: </span>
                {run.progress.case_path}
              </p>
            )}
          </div>

          {/* Progress advances from collected bytes when the adapter reports a
              total; otherwise the API estimates from stage + volume so the bar
              still moves during unbounded iOS/Android pulls. */}
          <div className="mb-2 h-2 w-full overflow-hidden rounded-full bg-ink-100">
            {(run.progress?.progress_pct ?? null) != null ? (
              <div
                className="h-full rounded-full bg-brand-500 transition-[width] duration-700 ease-out"
                style={{ width: `${Math.max(2, Math.min(100, run.progress!.progress_pct!))}%` }}
              />
            ) : (
              <div className="acq-indet h-full w-1/3 rounded-full bg-brand-400" />
            )}
          </div>

          <div className="mb-4 flex flex-wrap gap-x-6 gap-y-1 text-sm text-ink-600">
            <span>
              Stage: <strong className="text-ink-800">{run.progress?.stage || "starting"}</strong>
            </span>
            <span>
              Collected:{" "}
              <strong className="text-ink-800">
                {formatCollectedBytes(Number(run.progress?.bytes_done || 0))}
              </strong>
            </span>
            <span>
              Items: <strong className="text-ink-800">{run.progress?.files_seen ?? 0}</strong>
            </span>
            {run.progress?.progress_pct != null && (
              <span>
                Progress:{" "}
                <strong className="text-ink-800">
                  {run.progress.progress_pct}%
                  {run.progress.progress_mode === "estimated" ? " est." : ""}
                </strong>
              </span>
            )}
          </div>

          <Notice tone="info" title="Safe to leave this page">
            <p>
              The collection runs on the workstation, not in this browser tab. Closing the tab or
              losing the network will not stop it — reopen this page to reattach to the live
              progress. Do not disconnect the cable or power down the device.
            </p>
            <p className="pt-1">
              Volatile device state is captured first, then the payload is streamed and hashed in a
              single pass. Warnings, errors and interruptions are written to the audit trail as they
              occur.
            </p>
          </Notice>

          {run.cancel_requested && (
            <div className="mt-3">
              <Notice tone="warn" title="Cancellation stops at a safe boundary">
                <p>
                  The current item will finish, then whatever was collected is sealed and hashed.
                  The partial extraction remains evidence and must be reported as an interrupted
                  collection — not as an absence of data on the device.
                </p>
              </Notice>
            </div>
          )}
        </Card>
      )}

      {/* ---------------- Step 5: complete ---------------- */}
      {result && step === "complete" && (
        <Card className="p-5">
          <div className="mb-4 flex flex-wrap items-center gap-3">
            {collectionSucceeded(result) ? (
              <Badge tone="green">Collection complete</Badge>
            ) : isIncompleteIosBackup(result) ? (
              <Badge tone="red">Collection incomplete — retry</Badge>
            ) : (
              <Badge tone="amber">Completed with errors</Badge>
            )}
            <span className="font-mono text-sm text-ink-700">{result.run_name || "unnamed run"}</span>
            <Badge tone="neutral">
              {labelOf(result.method_decision?.selected ?? "none")}
            </Badge>
            {ownerAgentForFamily(
              result.device_profile?.os_family || preview?.device_profile?.os_family || selected?.os_family,
            ) && (
              <Badge tone="green">
                {ownerAgentForFamily(
                  result.device_profile?.os_family || preview?.device_profile?.os_family || selected?.os_family,
                )?.label}
              </Badge>
            )}
          </div>

          {result.client_import && (
            <div className="mb-4">
              <Notice tone="ok" title="Image on this computer and on the forensic server">
                <p>
                  Created on {result.client_import.client_host || "the client computer"} in{" "}
                  <span className="font-mono">{result.client_import.client_case_dir}</span>; copied to the forensic server in{" "}
                  <span className="font-mono">{result.client_import.server_case_dir}</span>
                  {result.client_import.scope === "packages" ? " (evidence packages, logs and hashes)" : ""}, every file
                  checked with SHA-256. Analysis uses the server copy.
                </p>
                {result.client_import.provenance && (
                  <p className="break-all pt-1 text-xs">Copy record: {result.client_import.provenance}</p>
                )}
              </Notice>
            </div>
          )}

          {String(result.device_profile?.os_family || selected?.os_family || "").toLowerCase().includes("android") && (
            <section className="mb-4 rounded-lg border border-ink-200 bg-ink-50/50 p-3" aria-label="WhatsApp recovered key">
              <h4 className="text-sm font-semibold text-ink-800">WhatsApp key — post-extraction result</h4>
              {!waKeyItems.length ? (
                <p className="mt-2 text-xs text-ink-500">No crypt15 key results are available for this acquisition. Missing keys cannot be generated from the backup.</p>
              ) : waKeyItems.map((item) => (
                <div key={item.directory} className="mt-2 break-all border-t border-ink-100 pt-2 text-xs">
                  <p className="font-mono">{item.directory}</p>
                  <p className="text-ink-600">{item.decrypted ? "msgstore.db verified" : "Not decrypted"} · {item.key_available ? "Matching key available" : "Key unavailable"}</p>
                  {item.key_available && <p className="mt-1 font-mono select-text">{waKeyRevealed && item.key_hex ? item.key_hex : "••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••"}</p>}
                </div>
              ))}
              {waKeyItems.some((item) => item.key_available) && run?.run_id && !run.host_job && (
                <Button variant="outline" disabled={waKeyLoading} onClick={async () => {
                  if (waKeyRevealed) { setWaKeyRevealed(false); setWaKeyItems((prev) => prev.map(({key_hex: _secret, ...rest}) => rest)); return; }
                  setWaKeyLoading(true); setWaKeyError("");
                  try {
                    const data = await acquisitionApi.getAndroidWhatsAppKeys(run.run_id, true);
                    setWaKeyItems(data.items); setWaKeyRevealed(true);
                  } catch { setWaKeyError("Unable to reveal the key. Check examiner permission or acquisition availability."); }
                  finally { setWaKeyLoading(false); }
                }}>{waKeyRevealed ? "Hide key" : waKeyLoading ? "Loading…" : "Reveal key"}</Button>
              )}
              {waKeyRevealed && <p className="mt-2 text-xs text-amber-800">Sensitive key material is visible on this screen. Hide it when finished; do not include it in reports or screenshots.</p>}
              {waKeyError && <p role="alert" className="mt-2 text-xs text-red-700">{waKeyError}</p>}
            </section>
          )}

          <div className="mb-4 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            {(
              [
                [
                  "Files collected",
                  String(filesCollected(result)),
                ],
                [
                  "Output size",
                  `${(((result.acquisition_record?.output_size as number) || 0) / 1_048_576).toFixed(1)} MB`,
                ],
                [
                  "Working copy verified",
                  result.verification?.ok
                    ? `${result.verification.verified} files`
                    : "not verified",
                ],
                ["Audit events", String(result.audit?.event_count ?? 0)],
              ] as [string, string][]
            ).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-3 border-b border-ink-50 py-1">
                <span className="text-ink-500">{k}</span>
                <span className="font-medium text-ink-800">{v}</span>
              </div>
            ))}
          </div>

          {isIncompleteIosBackup(result) && (
            <div className="mb-4">
              <Notice tone="error" title="iPhone backup did not finish — this is not a complete collection">
                <p>
                  USB dropped before Manifest.db was written. Four leftover files are not an Advanced
                  Logical image. Keep the phone unlocked and the cable seated, then retry. iOS Agent
                  resumes into the same short staging folder.
                </p>
              </Notice>
            </div>
          )}

          <div className="mb-4">
            <Notice tone="info" title="Method selection rationale">
              <p>
                {result.method_decision?.rationale ||
                  (result.ok
                    ? "Method rationale was not recorded for this run."
                    : "Collection did not complete method selection — see errors below.")}
              </p>
              {(result.method_decision?.deeper_available?.length ?? 0) > 0 && (
                <p className="pt-1">
                  Deeper methods remain available if further authority is obtained:{" "}
                  {result.method_decision!.deeper_available.map(labelOf).join(", ")}.
                </p>
              )}
            </Notice>
          </div>

          {(result.limitations?.length ?? 0) > 0 && (
            <div className="mb-4">
              <Notice tone="warn" title="Reportable limitations — must appear in the report">
                <ul className="list-disc space-y-1 pl-4">
                  {(result.limitations ?? []).map((l) => (
                    <li key={l}>{l}</li>
                  ))}
                </ul>
              </Notice>
            </div>
          )}

          {resultErrorList(result).length > 0 && (
            <div className="mb-4">
              <Notice tone="error" title="Errors">
                <ul className="list-disc pl-4">
                  {resultErrorList(result).map((e) => (
                    <li key={e}>{friendlyMobileAcquireError(e)}</li>
                  ))}
                </ul>
              </Notice>
            </div>
          )}

          {(result.acquisition_record?.missing_fields?.length ?? 0) > 0 && (
            <div className="mb-4">
              <Notice tone="warn" title="Acquisition record incomplete">
                <p>
                  Complete before peer review:{" "}
                  {result.acquisition_record!.missing_fields!.join(", ")}.
                </p>
              </Notice>
            </div>
          )}

          <details className="mb-4 rounded-lg border border-ink-100 bg-ink-50/60 px-3 py-2 text-sm" open>
            <summary className="cursor-pointer font-medium text-ink-700">
              Portable image packages (.zip / .ufdx / .ufd / .pas)
            </summary>
            <p className="mt-2 text-xs text-ink-500">
              Written under <span className="font-mono">05_Exports</span>. These are Polaron
              portable packages (ZIP/XML), not native Cellebrite UFED binaries — Physical Analyzer
              will not open them as UFED extracts; use folder import of{" "}
              <span className="font-mono">ios_backup</span> /{" "}
              <span className="font-mono">shared_storage</span> /{" "}
              <span className="font-mono">afc_media</span> when needed.
            </p>
            <dl className="mt-2 space-y-1">
              {Object.entries(exportPackagesOf(result)).map(([ext, path]) => (
                <div key={ext} className="flex flex-wrap gap-2">
                  <dt className="font-mono text-ink-600">.{ext}</dt>
                  <dd className="break-all font-mono text-xs text-ink-800">{path}</dd>
                </div>
              ))}
              {!Object.keys(exportPackagesOf(result)).length && (
                <p className="text-ink-500">
                  No export packages on this result — re-run collection after the API reload to
                  generate .zip/.ufdx/.ufd/.pas.
                </p>
              )}
            </dl>
          </details>

          <details className="mb-4 rounded-lg border border-ink-100 bg-ink-50/60 px-3 py-2 text-sm" open>
            <summary className="cursor-pointer font-medium text-ink-700">
              Content inventory (WhatsApp / messages / media)
            </summary>
            {(() => {
              const inv = contentInventoryOf(result);
              return (
                <div className="mt-2 space-y-2">
                  <p className="text-ink-700">{inv.summary || "No inventory recorded for this run."}</p>
                  {inv.counts && (
                    <ul className="flex flex-wrap gap-3 text-xs text-ink-600">
                      {Object.entries(inv.counts).map(([k, n]) => (
                        <li key={k}>
                          <strong className="text-ink-800">{k}</strong>: {n}
                        </li>
                      ))}
                    </ul>
                  )}
                  {(inv.categories?.whatsapp?.length ?? 0) > 0 && (
                    <div>
                      <p className="text-xs font-medium text-ink-600">WhatsApp sample paths</p>
                      <ul className="mt-1 max-h-28 overflow-auto font-mono text-[11px] text-ink-700">
                        {inv.categories!.whatsapp!.slice(0, 12).map((p) => (
                          <li key={p}>{p}</li>
                        ))}
                      </ul>
                      <p className="mt-2 text-xs text-ink-500">
                        Open named DBs under{" "}
                        <span className="font-mono">05_Exports/…/readable_artifacts/whatsapp/</span>{" "}
                        (e.g. <span className="font-mono">ChatStorage.sqlite</span>). Photos alone
                        live under <span className="font-mono">afc_media/DCIM</span>.
                      </p>
                    </div>
                  )}
                  {(inv.gaps?.length ?? 0) > 0 && (
                    <Notice tone="warn" title="Why chats may be missing">
                      <ul className="list-disc space-y-1 pl-4">
                        {inv.gaps!.map((g) => (
                          <li key={g}>{g}</li>
                        ))}
                      </ul>
                    </Notice>
                  )}
                </div>
              );
            })()}
          </details>

          <details className="mb-4 rounded-lg border border-ink-100 bg-ink-50/60 px-3 py-2 text-sm">
            <summary className="cursor-pointer font-medium text-ink-700">
              Evidence package paths
            </summary>
            <dl className="mt-2 space-y-1">
              {Object.entries(result.paths || {}).map(([k, v]) => (
                <div key={k} className="flex flex-wrap gap-2">
                  <dt className="text-ink-500">{k}</dt>
                  <dd className="break-all font-mono text-xs text-ink-800">{v}</dd>
                </div>
              ))}
              {!Object.keys(result.paths || {}).length && (
                <p className="text-ink-500">No package paths were written for this run.</p>
              )}
            </dl>
          </details>

          <details className="mb-4 rounded-lg border border-ink-100 bg-ink-50/60 px-3 py-2 text-sm">
            <summary className="cursor-pointer font-medium text-ink-700">
              Chain of custody ({result.chain_of_custody?.length ?? 0})
            </summary>
            <ol className="mt-2 space-y-1.5">
              {(result.chain_of_custody ?? []).map((c) => (
                <li key={`${c.stage}-${c.timestamp_utc}`} className="flex flex-wrap gap-2">
                  <Badge tone="neutral">{c.stage}</Badge>
                  <span className="text-ink-700">{c.detail}</span>
                  <span className="text-xs text-ink-400">{c.timestamp_utc}</span>
                </li>
              ))}
            </ol>
          </details>

          <div className="flex flex-wrap justify-end gap-2">
            {isIncompleteIosBackup(result) && (
              <Button variant="brand" loading={starting} disabled={starting} onClick={() => void retryIncompleteCollection()}>
                <RefreshCw className="h-4 w-4" />
                Retry iPhone backup
              </Button>
            )}
            {filesCollected(result) <= 0 && !isIncompleteIosBackup(result) && (
              <Button
                variant="outline"
                loading={recovering}
                disabled={recovering}
                onClick={() =>
                  void recoverFromDisk(
                    form.case_root || undefined,
                    run?.case_id || form.case_id || result.run_name || undefined,
                  ).then((ok) => {
                    if (!ok) {
                      toast.error(
                        "Could not reload this collection. If the phone is still connected, acquire it again.",
                      );
                    }
                  })
                }
              >
                <RefreshCw className="h-4 w-4" />
                Reload collection result
              </Button>
            )}
            <Button
              variant="danger"
              loading={removing}
              disabled={removing || !run?.run_id}
              onClick={() => void stopAndRemove(result)}
            >
              <Trash2 className="h-4 w-4" />
              {removing ? "Removing…" : "Stop and remove"}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setResult(null);
                setStep("connect");
                setSelected(null);
                setPreview(null);
              }}
            >
              Acquire another device
            </Button>
            <Button
              variant="ghost"
              onClick={async () => {
                try {
                  const original = result.paths?.original;
                  const manifest = result.evidence_package?.hash_manifest ?? "";
                  if (!original || !manifest) {
                    toast.error("This run has no sealed original path or hash manifest to verify.");
                    return;
                  }
                  const v = await acquisitionApi.verifyExtraction(original, manifest);
                  (v.ok ? toast.success : toast.error)(v.interpretation);
                } catch (e) {
                  toast.error(e instanceof Error ? e.message : "Verification failed");
                }
              }}
            >
              <FileCheck2 className="h-4 w-4" /> Re-verify integrity
            </Button>
            <Button
              variant="brand"
              loading={startingAnalysis}
              disabled={startingAnalysis || !collectionSucceeded(result)}
              onClick={() => void startAnalysis()}
            >
              Process this extraction
            </Button>
          </div>
        </Card>
      )}

      <p className="mt-6 flex items-start gap-2 text-xs text-ink-500">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
        <span>
          Collection uses documented device interfaces only. Lock bypass, bootloader or chipset
          exploitation, and vendor advanced-access workflows are not implemented; where such a
          capability would be required, the capability model reports a gap so the report states a
          limitation rather than an empty result.
        </span>
      </p>
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { Download, FileSearch, FolderOpen, ShieldCheck, Trash2, UploadCloud } from "lucide-react";
import { api } from "../../lib/api";

type MatchMode = "phrase" | "all_words" | "any_words";
type OcrRow = {
  path: string;
  sha256: string;
  matched: boolean | null;
  snippet?: string;
  error?: string;
  engine: string;
};
type OcrState = {
  id: string;
  status: "receiving" | "queued" | "running" | "completed" | "completed_with_errors" | "failed";
  query: string;
  total: number;
  done: number;
  matched: number;
  errors: number;
  error?: string;
  results: OcrRow[];
  purged: boolean;
  source_can_quarantine?: boolean;
  source_pruned?: boolean;
  quarantined_source_files?: { path: string; sha256: string }[];
};

function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "OCR request failed";
}

export function OcrDocumentsPage() {
  const folderPicker = useRef<HTMLInputElement>(null);
  const [source, setSource] = useState<"upload" | "server">("upload");
  const [files, setFiles] = useState<File[]>([]);
  const [serverPath, setServerPath] = useState("");
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<MatchMode>("phrase");
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState<OcrState | null>(null);
  const [problem, setProblem] = useState("");
  const [confirmCopyDeletion, setConfirmCopyDeletion] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [confirmQuarantine, setConfirmQuarantine] = useState("");
  const [quarantining, setQuarantining] = useState(false);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    // Folder upload is supported by Chromium browsers; standard file input stays as fallback.
    folderPicker.current?.setAttribute("webkitdirectory", "");
    folderPicker.current?.setAttribute("directory", "");
  }, []);

  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    const refresh = async () => {
      try {
        const info = await api.get<OcrState>(`/api/ocr/jobs/${jobId}`, undefined, { service: "forensic" });
        if (alive) setJob(info);
      } catch (e: unknown) {
        if (alive) setProblem(readableError(e));
      }
    };
    void refresh();
    const interval = window.setInterval(() => {
      if (job?.status === "completed" || job?.status === "completed_with_errors" || job?.status === "failed") return;
      void refresh();
    }, 2500);
    return () => { alive = false; window.clearInterval(interval); };
  }, [jobId, job?.status]);

  const terminal = job?.status === "completed" || job?.status === "completed_with_errors";
  const matches = (job?.results || []).filter((item) => item.matched === true);
  const unmatched = (job?.results || []).filter((item) => item.matched === false);
  const failures = (job?.results || []).filter((item) => item.matched === null);

  async function start() {
    setProblem("");
    const form = new FormData();
    form.set("query", query.trim());
    form.set("match_mode", mode);
    if (source === "server") {
      form.set("source_path", serverPath.trim());
    } else {
      for (const file of files) {
        const relative = file.webkitRelativePath || file.name;
        form.append("files", file, relative);
      }
    }
    setBusy(true);
    setJob(null);
    setJobId("");
    try {
      const response = await api.postForm<{ id: string; status: string }>("/api/ocr/jobs", form, {
        service: "forensic", timeoutMs: 0,
      });
      setJobId(response.id);
    } catch (e: unknown) {
      setProblem(readableError(e));
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    if (!jobId) return;
    setDownloading(true);
    setProblem("");
    try {
      await api.download(`/api/ocr/jobs/${jobId}/matches.zip`, `ocr-matches-${jobId.slice(0, 8)}.zip`, undefined, 180_000);
    } catch (e: unknown) {
      setProblem(readableError(e));
    } finally {
      setDownloading(false);
    }
  }

  async function quarantineSource() {
    if (!jobId || confirmQuarantine !== "QUARANTINE UNMATCHED SOURCE FILES") return;
    setQuarantining(true);
    setProblem("");
    const form = new FormData();
    form.set("confirmation", confirmQuarantine);
    try {
      await api.postForm(`/api/ocr/jobs/${jobId}/quarantine-unmatched`, form, { service: "forensic" });
      setJob(await api.get<OcrState>(`/api/ocr/jobs/${jobId}`, undefined, { service: "forensic" }));
      setConfirmQuarantine("");
    } catch (e: unknown) {
      setProblem(readableError(e));
    } finally {
      setQuarantining(false);
    }
  }

  async function removeUnmatched() {
    if (!jobId || confirmCopyDeletion !== "DELETE UNMATCHED COPIES") return;
    setDeleting(true);
    setProblem("");
    const form = new FormData();
    form.set("confirmation", confirmCopyDeletion);
    try {
      await api.postForm(`/api/ocr/jobs/${jobId}/remove-unmatched`, form, { service: "forensic" });
      const latest = await api.get<OcrState>(`/api/ocr/jobs/${jobId}`, undefined, { service: "forensic" });
      setJob(latest);
      setConfirmCopyDeletion("");
    } catch (e: unknown) {
      setProblem(readableError(e));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6 pb-12">
      <header className="space-y-2">
        <div className="flex items-center gap-3"><FileSearch className="h-7 w-7 text-brand-600" /><h1 className="text-2xl font-semibold">OCR — Document search</h1></div>
        <p className="max-w-3xl text-sm text-ink-600">Find exact phrases or words across PDFs, scanned images, Office documents and text files. Native text is searched first; OCR is used for image-only pages. Each document is processed independently.</p>
      </header>
      <section className="rounded-xl border border-ink-200 bg-white p-6 shadow-sm space-y-5">
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="radio" checked={source === "upload"} onChange={() => setSource("upload")} />Upload a folder or files</label>
          <label className="flex items-center gap-2"><input type="radio" checked={source === "server"} onChange={() => setSource("server")} />Use an approved server directory</label>
        </div>
        {source === "upload" ? (
          <div className="flex flex-wrap items-center gap-4">
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-ink-300 px-4 py-2 text-sm hover:bg-ink-50"><FolderOpen className="h-4 w-4" />Choose folder<input ref={folderPicker} aria-label="Browse document folder" type="file" className="sr-only" multiple onChange={(e) => setFiles(Array.from(e.target.files || []))} /></label>
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-ink-300 px-4 py-2 text-sm hover:bg-ink-50"><UploadCloud className="h-4 w-4" />Choose files<input aria-label="Browse document files" type="file" className="sr-only" multiple onChange={(e) => setFiles(Array.from(e.target.files || []))} /></label>
            <span className="text-sm text-ink-600">{files.length ? `${files.length.toLocaleString()} selected file(s)` : "No documents selected"}</span>
          </div>
        ) : (
          <label className="block space-y-1 text-sm"><span className="font-medium">Server directory path (allowlisted only)</span><input value={serverPath} onChange={(e) => setServerPath(e.target.value)} placeholder="/app/data/ocr_search/inbox/<your-tenant-schema>/my-documents" className="w-full rounded-lg border border-ink-300 px-3 py-2" /><span className="block text-xs text-ink-500">Administrators configure OCR_DOCUMENT_SOURCE_ROOTS. Client computer paths must be uploaded using the folder selector.</span></label>
        )}
        <div className="grid gap-4 md:grid-cols-[2fr_1fr]">
          <label className="space-y-1 text-sm"><span className="block font-medium">Sentence or words to find</span><textarea value={query} onChange={(e) => setQuery(e.target.value)} maxLength={1000} rows={3} placeholder="Enter the exact sentence or keywords…" className="w-full rounded-lg border border-ink-300 px-3 py-2" /></label>
          <label className="space-y-1 text-sm"><span className="block font-medium">Matching rule</span><select value={mode} onChange={(e) => setMode(e.target.value as MatchMode)} className="w-full rounded-lg border border-ink-300 px-3 py-2"><option value="phrase">Exact phrase (case-insensitive)</option><option value="all_words">Contains all words</option><option value="any_words">Contains any word</option></select><span className="block text-xs text-ink-500">Punctuation and extra spaces are normalized; matches are not semantic/AI guesses.</span></label>
        </div>
        <button type="button" disabled={busy || !query.trim() || (source === "upload" ? !files.length : !serverPath.trim())} onClick={() => void start()} className="rounded-lg bg-brand-600 px-5 py-2.5 font-medium text-white disabled:opacity-50 hover:bg-brand-700">{busy ? "Submitting documents…" : "Search documents"}</button>
      </section>
      {problem && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{problem}</div>}
      {job && <section className="rounded-xl border border-ink-200 bg-white p-6 shadow-sm space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-semibold">Search results</h2><span className="rounded-full bg-ink-100 px-3 py-1 text-xs font-medium">{job.status.replaceAll("_", " ")}</span></div>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4 text-sm"><div>Documents <strong className="block text-xl">{job.total}</strong></div><div>Processed <strong className="block text-xl">{job.done}</strong></div><div>Matches <strong className="block text-xl">{job.matched}</strong></div><div>Errors <strong className="block text-xl">{job.errors}</strong></div></div>
        <div className="h-2 overflow-hidden rounded-full bg-ink-100"><div className="h-full bg-brand-500 transition-all" style={{ width: `${job.total ? 100 * job.done / job.total : 0}%` }} /></div>
        {job.error && <p role="alert" className="text-sm text-red-700">{job.error}</p>}
        {terminal && <div className="flex flex-wrap items-center gap-4"><button type="button" onClick={() => void download()} disabled={downloading} className="inline-flex items-center gap-2 rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"><Download className="h-4 w-4" />{downloading ? "Preparing download…" : `Download ${job.matched} matched document(s) as ZIP`}</button><span className="flex items-center gap-1 text-xs text-ink-600"><ShieldCheck className="h-4 w-4" />ZIP includes checksums and search manifest</span></div>}
        {matches.length > 0 && <div className="space-y-3"><h3 className="font-semibold">Matching documents ({matches.length})</h3><div className="max-h-[540px] overflow-auto divide-y divide-ink-100 rounded-lg border border-ink-200">{matches.slice(0, 400).map((row) => <div key={row.path} className="p-3 text-sm"><div className="font-medium break-all">{row.path}</div><p className="mt-1 text-xs text-ink-600 break-words">{row.snippet}</p><div className="mt-1 text-xs text-ink-500">{row.engine} · SHA-256 {row.sha256.slice(0, 16)}…</div></div>)}</div>{matches.length > 400 && <p className="text-xs text-ink-600">Showing first 400; all matches are in the ZIP.</p>}</div>}
        {terminal && !matches.length && <p className="text-sm">No matching documents were found in successfully processed files.</p>}
        {failures.length > 0 && <div className="space-y-2"><h3 className="font-semibold">Files needing review ({failures.length})</h3>{failures.slice(0, 30).map((row) => <p key={row.path} className="text-sm text-amber-800">{row.path}: {row.error}</p>)}</div>}
        {terminal && unmatched.length > 0 && !job.purged && <div className="space-y-3 border-t border-ink-200 pt-4"><h3 className="flex items-center gap-2 font-semibold"><Trash2 className="h-4 w-4" />Remove unmatched workspace copies</h3><p className="text-sm text-ink-600">{unmatched.length} definitely nonmatching copies can be deleted from this OCR job's private working directory. Original documents, original selected directory and failed/unreadable documents will not be deleted. This is not an evidence erasure action.</p><label className="block space-y-1 text-xs"><span>Type <strong>DELETE UNMATCHED COPIES</strong> to confirm</span><input className="block w-full max-w-sm rounded-lg border border-ink-300 px-3 py-2 text-sm" value={confirmCopyDeletion} onChange={(e) => setConfirmCopyDeletion(e.target.value)} /></label><button type="button" onClick={() => void removeUnmatched()} disabled={confirmCopyDeletion !== "DELETE UNMATCHED COPIES" || deleting} className="rounded-lg border border-red-300 px-4 py-2 text-sm text-red-800 disabled:opacity-40">{deleting ? "Removing copies…" : "Delete unmatched working copies only"}</button></div>}
        {job.purged && <p className="text-sm text-green-700">Unmatched workspace copies removed; source files remain unchanged.</p>}
        {terminal && job.source_can_quarantine && unmatched.length > 0 && !job.source_pruned && <div className="space-y-3 border-t border-ink-200 pt-4">
          <h3 className="font-semibold">Prune a dedicated server OCR inbox</h3>
          <p className="text-sm text-ink-600">Optional administrator-enabled operation: remove verified nonmatching files from the *selected server OCR inbox* by moving them into a recoverable quarantine, retaining SHA-256 evidence. This does not affect original forensic case folders, browser-uploaded directories or unreadable files.</p>
          <label className="block space-y-1 text-xs"><span>Type <strong>QUARANTINE UNMATCHED SOURCE FILES</strong> to confirm</span><input className="block w-full max-w-lg rounded-lg border border-ink-300 px-3 py-2 text-sm" value={confirmQuarantine} onChange={(e) => setConfirmQuarantine(e.target.value)} /></label>
          <button type="button" disabled={confirmQuarantine !== "QUARANTINE UNMATCHED SOURCE FILES" || quarantining} onClick={() => void quarantineSource()} className="rounded-lg border border-amber-300 px-4 py-2 text-sm text-amber-800 disabled:opacity-40">{quarantining ? "Quarantining files…" : "Remove nonmatches from selected OCR inbox (reversible)"}</button>
        </div>}
        {job.source_pruned && <p className="text-sm text-green-700">Unmatched server-inbox files moved to recoverable OCR quarantine; matching and unreadable files stay in the chosen directory.</p>}
      </section>}
    </div>
  );
}

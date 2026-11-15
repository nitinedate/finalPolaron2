import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Button, Card, Spinner } from "../ui";
import { forensicApi } from "../../lib/forensicApi";
import type { Artifact, SuspiciousActivityCard, SuspiciousActivityPage, PriorityEvidencePage, PriorityEvidenceRecord } from "../../lib/types/forensic";
import { EvidenceFrame } from "./EvidenceFrame";
import { ArtifactPreviewPanel } from "./ArtifactPreviewPanel";

const DATABASE_FILE = /\.(sqlite3?|db|sqlitedb)(-wal|-shm|-journal)?$/i;

function isDatabasePath(path: string): boolean {
  const base = path.split(/[\\/]/).pop() || "";
  return DATABASE_FILE.test(base);
}

const families = [
  ["whatsapp_messages", "WhatsApp chats"], ["deleted_whatsapp", "Deleted / residual chats"],
  ["browser", "Web / browser activity"], ["pictures", "Pictures"], ["videos", "Videos"],
  ["audio", "Audio"], ["documents", "Documents"], ["messages", "All messages"], ["emails", "Emails"], ["binary", "Binary fragments"], ["structured_data", "Structured exports"], ["downloads", "Downloads"], ["applications", "Applications"], ["accounts", "Accounts / users"], ["suspicious", "Suspicious Activity"],
] as const;

export function PriorityEvidencePanel({ jobId }: { jobId: string }) {
  const [expanded, setExpanded] = useState(false);
  const [family, setFamily] = useState("whatsapp_messages");
  const [page, setPage] = useState(1);
  const [priority, setPriority] = useState<PriorityEvidencePage | null>(null);
  const [media, setMedia] = useState<SuspiciousActivityPage | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [opened, setOpened] = useState<PriorityEvidenceRecord | null>(null);
  const requestId = useRef(0);
  const load = useCallback(async () => {
    const request = ++requestId.current;
    setLoading(true); setError("");
    try {
      if (family === "suspicious") {
        const result = await forensicApi.getSuspiciousActivity(jobId, page);
        if (request === requestId.current) setMedia(result);
      } else {
        const result = await forensicApi.getPriorityEvidence(jobId, family, page);
        if (request === requestId.current) setPriority(result);
      }
    } catch (e) { if (request === requestId.current) setError(e instanceof Error ? e.message : "Evidence could not be loaded"); }
    finally { if (request === requestId.current) setLoading(false); }
  }, [jobId, family, page ]);
  useEffect(() => { if (expanded) void load(); }, [load, expanded]);
  const openSource = (item: PriorityEvidenceRecord) => {
    setError("");
    setOpened(item);
  };
  const total = family === "suspicious" ? media?.total || 0 : priority?.total || 0;
  const openedPath = String(opened?.forensic?.source_path || "");
  const openedBody = String(opened?.data?.body || opened?.data?.url || opened?.data?.title || opened?.data?.name || opened?.data?.note || "");
  const openedIsDatabase = isDatabasePath(openedPath);
  const openedArtifact: Artifact | null = opened?.job_artifact_id && !openedIsDatabase ? {
    id: opened.job_artifact_id,
    job_id: jobId,
    file_id: null,
    parent_artifact_id: null,
    artifact_type: opened.artifact_type || "media",
    axiom_category: "",
    axiom_sub_category: null,
    title: openedPath.split(/[\\/]/).pop() || "Source file",
    file_name: openedPath.split(/[\\/]/).pop() || "",
    source_path: openedPath,
    artifact_datetime: opened.timestamp_utc,
    preview_uri: null,
    storage_uri: null,
    metadata: {},
    tags: null,
    examiner_comment: null,
    parser_version: null,
    confidence: null,
    created_at: opened.timestamp_utc || "",
  } : null;
  return <Card className="mb-4 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <button type="button" className="flex min-w-0 flex-1 items-start gap-2 text-left" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        {expanded ? <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-ink-500" /> : <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-ink-500" />}
        <span className="min-w-0"><h3 className="font-semibold text-ink-900">Priority evidence</h3>
        <p className="text-xs text-ink-500">Source-linked records, recovered states and image/video observations.</p></span>
      </button>
      <Button variant="outline" onClick={() => setExpanded((value) => !value)}>{expanded ? "Collapse" : "Expand"}</Button>
      {expanded ? <Button variant="outline" onClick={() => void load()}>Refresh</Button> : null}
    </div>
    {!expanded ? null : <>
    <div className="mb-3 flex flex-wrap gap-2">{families.map(([key, label]) => <Button key={key}
      variant={family === key ? "brand" : "outline"} onClick={() => { setFamily(key); setPage(1); setOpened(null); }}>
      {label}{key !== "suspicious" && priority ? ` (${(priority.families?.[key] || 0).toLocaleString()})` : ""}
    </Button>)}</div>
    {family === "suspicious" && <p className="mb-3 text-xs text-ink-600">{media?.note}</p>}
    {error && <p role="alert" className="mb-3 text-sm text-red-700">{error}</p>}
    {loading ? <Spinner /> : <>
      {family === "suspicious" ? <div className="max-h-[520px] space-y-3 overflow-auto">{media?.items?.map(item => {
        const card = {...item,...item.data,job_id:jobId} as SuspiciousActivityCard;
        return <div key={card.id} className="rounded-lg border p-3 text-xs">
          <p className="font-semibold">{card.category.replace(/_/g," ")} · pending examiner review</p>
          <p className="break-all">{card.source_path}</p><p className="break-all font-mono text-[10px]">SHA256 {card.source_sha256 || "Unavailable"}</p>
          <EvidenceFrame card={card} /><p className="my-2 whitespace-pre-wrap">{card.explanation}</p>
          <blockquote className="whitespace-pre-wrap border-l-2 pl-2">{card.excerpt}</blockquote>
          <p className="my-2">Method: {card.method} · Source record: {card.source_record_id}</p>
          {card.frame_sha256 && <p className="break-all">Frame SHA256 {card.frame_sha256} · {card.timestamp_seconds ?? "Image"}</p>}
          {card.job_artifact_id && <Button variant="outline" onClick={() => openSource({ artifact_id: card.id, job_artifact_id: card.job_artifact_id, artifact_type: card.category, state: "pending examiner review", timestamp_utc: card.timestamp_utc, data: { body: card.excerpt, note: card.explanation }, forensic: { source_path: card.source_path, source_sha256: card.source_sha256 }, linked_media: [] })}>Open original evidence</Button>}
        </div>;
      })}</div> : <div className="max-h-[520px] overflow-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b"><th className="p-2">Record / time</th><th className="p-2">Content</th><th className="p-2">Source evidence</th></tr></thead><tbody>
        {priority?.items?.map((item) => <tr key={item.artifact_id} className="border-b align-top"><td className="p-2">{item.artifact_type}<br />{item.timestamp_utc || "Time unavailable"}<br /><strong>{String(item.state || "unknown").replace(/_/g, " ")}</strong></td>
          <td className="max-w-md p-2"><p className="whitespace-pre-wrap break-words">{String(item.data.body || item.data.url || item.data.title || item.data.name || item.data.note || "Source record")}</p>
            {item.data.reference_only === true && <p className="font-semibold text-amber-800">Reference only — this database entry does not establish recovery of the file content.</p>}
            <details><summary className="cursor-pointer">Full record and provenance</summary><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({ data: item.data, forensic: item.forensic }, null, 2)}</pre></details>
            {item.linked_media?.map((link) => <Button key={link.id} variant="outline" onClick={() => openSource({ artifact_id: link.id, job_artifact_id: link.id, artifact_type: "media", state: "allocated", timestamp_utc: item.timestamp_utc, data: { name: String(link.file_path || "").split("/").pop() }, forensic: { source_path: link.file_path, source_sha256: link.sha256 }, linked_media: [] })}>{String(link.file_path || "Evidence").split("/").pop()} · {String(link.match_basis || "linked").replace(/_/g, " ")}</Button>)}</td>
          <td className="max-w-xs break-all p-2">{String(item.forensic.source_path || "")}<br /><span className="font-mono text-[10px]">{String(item.forensic.source_sha256 || "")}</span>
            {item.job_artifact_id && <Button variant="outline" onClick={() => openSource(item)}>Open source</Button>}</td></tr>)}</tbody></table></div>}
      {total === 0 && <p className="py-4 text-xs text-ink-600">No records captured in this category yet. For Android chats and browser history, check acquired private databases and matching backup keys. Deleted or overwritten bytes may be unavailable.</p>}
      <div className="mt-3 flex items-center gap-3 text-xs"><span>{total.toLocaleString()} records · Page {page}</span><Button variant="outline" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button><Button variant="outline" disabled={page * 50 >= total} onClick={() => setPage(page + 1)}>Next</Button></div>
    </>}
    {opened && <div className="mt-4 rounded-lg border border-ink-200 bg-white p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h4 className="text-sm font-semibold text-ink-900">Opened record</h4>
        <Button variant="outline" onClick={() => setOpened(null)}>Close</Button>
      </div>
      <p className="whitespace-pre-wrap text-sm text-ink-800">{openedBody || "No message text recovered."}</p>
      <p className="mt-2 text-xs text-ink-500">{opened?.timestamp_utc || "Time unavailable"} · {String(opened?.state || "").replace(/_/g, " ")}</p>
      <p className="mt-1 break-all font-mono text-[10px] text-ink-500">{openedPath}</p>
      {openedIsDatabase ? <p className="mt-3 text-xs text-ink-600">This record lives in the chat database shown above. The database itself stays on this page so the screen is not replaced by the file.</p> : null}
      {openedArtifact ? <ArtifactPreviewPanel jobId={jobId} artifact={openedArtifact} /> : null}
    </div>}
    </>}
  </Card>;
}

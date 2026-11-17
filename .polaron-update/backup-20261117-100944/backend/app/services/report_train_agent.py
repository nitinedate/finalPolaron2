"""ReportTrain agent.

Sample reports live in knowledge/reports_train/disk and knowledge/reports_train/mobile.
The trained model is knowledge/reports_train/report_train_model.json.

On the first run, and again whenever a sample report is added or replaced,
the model is trained from those files. It stores plain-English questions that
tell the report what to look for. It does not store names, accounts, web
addresses, dates, or counts from the samples.

A later report is written from three things only: this model, the saved
records for that case, and what the user wrote in the case form.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "reports_train"
_MODEL_PATH = _DIR / "report_train_model.json"
AGENT_NAME = "ReportTrain"
_PROMPT_VERSION = 2
_CACHE: dict[str, Any] = {"signature": "", "model": None}

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\b")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_LONG_NUMBER_RE = re.compile(r"\b\d[\d,./:-]{2,}\b")
_OBJECTIVE_END_RE = re.compile(r"\s*objective\s*:?\s*$", re.I)

# Questions the report asks, in everyday words, and which saved records can answer them.
DATA_PULLS: tuple[dict[str, Any], ...] = (
    {
        "id": "remote_access",
        "match": ("remote access", "rdp", "anydesk", "teamviewer"),
        "what": "Programs that let someone else use the computer, and any record that a remote session happened",
        "how": "Look at the programs that were installed and at the saved remote-session records. A program being installed does not mean someone used it. A session does not mean someone took control without permission.",
        "questions": (
            "Is a remote-access program in the saved list of installed programs?",
            "Is there a saved record that a remote session actually happened?",
            "Does that record show only that a session existed, or also that someone took control?",
        ),
        "means_if_found": "a remote-access program or a remote session is in the saved records. That does not by itself show the program was used or that someone took control.",
        "means_if_none": "the saved records do not show a remote-access program or a remote session.",
        "sources": ("installed_programs", "rdp_sessions"),
    },
    {
        "id": "external_devices",
        "match": ("usb", "external device", "mobile device"),
        "what": "Records of USB sticks and other storage that was plugged in",
        "how": "Look at the saved records of things that were plugged in. Skip keyboards, mice, hubs, and the computer's own drives. A plug-in record does not mean files were copied.",
        "questions": (
            "Which storage items were plugged in?",
            "Does the record show only that it was plugged in, or also that files were copied?",
        ),
        "means_if_found": "something was plugged in. The saved records do not by themselves show that files were copied.",
        "means_if_none": "the saved records do not show storage that was plugged in.",
        "sources": ("connected_devices", "usb_devices"),
    },
    {
        "id": "cloud_storage",
        "match": ("cloud storage", "cloud-based", "google drive", "dropbox", "onedrive"),
        "what": "Web pages for online file storage that were opened",
        "how": "Look at the saved web history. Keep only pages for online file storage. Opening a page is not the same as sending or saving a file.",
        "questions": (
            "Did the saved web history show a visit to an online file-storage page?",
            "Does that visit show only that the page was opened?",
        ),
        "means_if_found": "an online file-storage page was opened. That does not show a file was sent or saved.",
        "means_if_none": "the saved web history does not show a visit to an online file-storage page.",
        "sources": ("browser_history",),
    },
    {
        "id": "web_chat",
        "match": ("chat", "messaging", "communication platform", "whatsapp"),
        "what": "Chat pages that were opened, and on a phone the messages already saved",
        "how": "On a computer, look at the saved web history for chat pages. On a phone, read the messages already saved for this case. Do not start a new backup. Opening a chat page is not the same as sending a message.",
        "questions": (
            "Did the saved records show a chat page or saved messages?",
            "Do those records show a message, or only that a chat page was opened?",
        ),
        "means_if_found": "a chat page or saved messages are in the records. Opening a page is not the same as sending a message.",
        "means_if_none": "the saved records do not show a chat page or saved messages.",
        "sources": ("browser_history", "mobile_messages"),
    },
    {
        "id": "social_media",
        "match": ("social media", "facebook", "instagram", "linkedin"),
        "what": "Social media pages that were opened",
        "how": "Look at the saved web history and keep social media pages. Opening a page does not show that a post or a message was sent, and it does not show who owns the account.",
        "questions": (
            "Did the saved web history show a social media page?",
            "Does that visit show a post or a message, or only that the page was opened?",
        ),
        "means_if_found": "a social media page was opened. That does not show a post, a message, or who owns the account.",
        "means_if_none": "the saved web history does not show a social media page.",
        "sources": ("browser_history",),
    },
    {
        "id": "internet_network",
        "match": ("internet usage", "network activity", "browsing records"),
        "what": "Web pages that were opened and network connections that were saved",
        "how": "Look at the saved web history and the saved connection records. A connection does not mean a file was sent.",
        "questions": (
            "Which web pages or connections are in the saved records?",
            "Does a connection show that a file was sent?",
        ),
        "means_if_found": "web pages or connections are in the saved records. A connection does not show that a file was sent.",
        "means_if_none": "the saved records do not show a web page or a connection for this question.",
        "sources": ("browser_history", "network_connections"),
    },
    {
        "id": "anti_forensics",
        "match": ("hide user activity", "permanently delete", "anti-forensic", "ccleaner"),
        "what": "Programs that can clean or wipe records",
        "how": "Look at the saved list of installed programs and keep cleaning or wiping programs. A program being installed does not mean it was run, and it does not mean records were destroyed.",
        "questions": (
            "Is a cleaning or wiping program in the saved list of installed programs?",
            "Do the records show that it was run, or only that it was installed?",
        ),
        "means_if_found": "a cleaning or wiping program was installed. That does not show it was run or that records were destroyed.",
        "means_if_none": "the saved records do not show a cleaning or wiping program.",
        "sources": ("installed_programs",),
    },
    {
        "id": "torrent",
        "match": ("torrent", "file-sharing"),
        "what": "Torrent or file-sharing pages in the web history",
        "how": "Look at the saved web history and keep torrent or file-sharing pages. Opening a page does not mean a file was transferred.",
        "questions": (
            "Did the saved web history show a torrent or file-sharing page?",
            "Does that visit show a file transfer, or only that the page was opened?",
        ),
        "means_if_found": "a torrent or file-sharing page was opened. That does not show a file was transferred.",
        "means_if_none": "the saved web history does not show a torrent or file-sharing page.",
        "sources": ("browser_history",),
    },
    {
        "id": "malware_phishing",
        "match": ("phishing", "malware", "suspicious activity"),
        "what": "Web addresses already marked as harmful or misleading",
        "how": "Look at the web-address checks already saved for this case. A marked address does not mean a file was downloaded or that the computer was infected.",
        "questions": (
            "Which saved web addresses were already marked as harmful or misleading?",
            "Does a mark show that a file was downloaded or that something harmful ran?",
        ),
        "means_if_found": "a web address was already marked as harmful or misleading. That does not show a file was downloaded or that something harmful ran.",
        "means_if_none": "the saved checks do not show a web address marked as harmful or misleading.",
        "sources": ("classified_urls",),
    },
    {
        "id": "mobile_calls_contacts",
        "match": ("call log", "contacts", "dialer"),
        "what": "Calls and contact names already saved from the phone",
        "how": "Read the call list and the contact list already saved for this case. Use a person's name only when that name is in the contact list.",
        "questions": (
            "Which calls are in the saved call list?",
            "Which of those numbers also have a name in the saved contact list?",
        ),
        "means_if_found": "the saved call list or contact list has records. A name is used only when the contact list already has that name.",
        "means_if_none": "the saved call list and contact list do not have records for this question.",
        "sources": ("call_log", "contacts"),
    },
)

_WRITING_STYLE = (
    "Use short everyday words. Describe only what the saved records show and what the user wrote in the case form. "
    "Do not name the phone or the computer. Say what was found, then say what that does not prove. "
    "End with one sentence that starts with This means."
)

_DISK_SECTIONS = (
    "introduction",
    "tools_used",
    "forensic_imaging",
    "os_information",
    "artifact_summary",
    "objectives_procedure_observation",
    "annexure",
    "final_analysis_summary",
    "appendix",
)
_MOBILE_SECTIONS = (
    "introduction",
    "tools_used",
    "device_information",
    "extraction_summary",
    "objectives_procedure_observation",
    "annexure",
)

_MOBILE_DEVICE_FIELDS = (
    {"field": "Make", "pull": "build.prop ro.product.manufacturer or ro.product.brand, else the acquisition vendor"},
    {"field": "Model", "pull": "build.prop ro.product.model or the acquisition device model. Never the case folder name."},
    {"field": "Serial Number", "pull": "build.prop ro.serialno, ro.boot.serialno, or the dump serial"},
    {"field": "IMEI", "pull": "IMEI values recovered from the phone dump or the exhibit form"},
    {"field": "Operating System", "pull": "Android or iOS plus build.prop ro.build.version.release"},
    {"field": "Capacity", "pull": "storage capacity on the exhibit or the device dump"},
)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _clean_title(title: str) -> str:
    """Keep a reusable objective name and drop numbering, bullets and client tokens."""
    cleaned = re.sub(r"^\d+\s*[.)]\s*", "", (title or "").strip())
    cleaned = cleaned.strip(" -:\u2022•")
    folded = _norm(cleaned)
    if len(folded) < 8 or "rrp" in folded.split():
        return ""
    return cleaned


def _safe_procedure(text: str) -> str:
    cleaned = _URL_RE.sub("", text or "")
    cleaned = _EMAIL_RE.sub("", cleaned)
    cleaned = _LONG_NUMBER_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) < 40:
        return ""
    if not cleaned.endswith("."):
        cleaned += "."
    return cleaned[:700]


def _public_pull(pull: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": pull["id"],
        "what": pull["what"],
        "how": pull["how"],
        "questions": list(pull.get("questions") or []),
        "means_if_found": pull.get("means_if_found") or "",
        "means_if_none": pull.get("means_if_none") or "",
        "sources": list(pull["sources"]),
    }


def _pulls_for(text: str) -> list[dict[str, Any]]:
    hay = _norm(text)
    chosen: list[dict[str, Any]] = []
    for pull in DATA_PULLS:
        if any(_norm(token) in hay for token in pull["match"]):
            chosen.append(_public_pull(pull))
    return chosen


def _prompt(title: str, pulls: list[dict[str, Any]]) -> dict[str, Any]:
    questions: list[str] = []
    for pull in pulls:
        for question in pull.get("questions") or []:
            if question not in questions:
                questions.append(question)
    if not questions:
        questions = [
            f"What do the saved records show for {title}?",
            "What did the user write about this in the case form?",
        ]
    return {"questions": questions[:6], "instruction": _WRITING_STYLE}


def _simple_procedure(pulls: list[dict[str, Any]]) -> str:
    if not pulls:
        return (
            "We read the saved records for this question and what the user wrote in the case form. "
            "We describe only what those records show."
        )
    parts: list[str] = []
    for pull in pulls[:3]:
        how = str(pull.get("how") or "").strip()
        if not how:
            continue
        if not how.endswith("."):
            how += "."
        parts.append(how)
    return " ".join(parts)[:700]


def learn_lines(lines: list[str], *, kind: str) -> list[dict[str, Any]]:
    """Learn objective title, procedure and data pull from one report's lines."""
    learned: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        title = ""
        if line and _OBJECTIVE_END_RE.search(line) and "procedure" not in line.lower():
            title = _OBJECTIVE_END_RE.sub("", line).strip(" -:.")
            if _norm(title) in {"", "objective"}:
                title = ""
                back = index - 1
                while back >= 0 and not lines[back].strip():
                    back -= 1
                if back >= 0:
                    title = lines[back].strip(" -:")
        title = _clean_title(title)
        if not title:
            index += 1
            continue
        index += 1
        while index < len(lines) and not lines[index].strip().lower().startswith("procedure"):
            index += 1
        procedure_lines: list[str] = []
        if index < len(lines) and lines[index].strip().lower().startswith("procedure"):
            index += 1
            while index < len(lines) and not lines[index].strip().lower().startswith("observation"):
                piece = lines[index].strip()
                if piece and not piece.lower().startswith("objective"):
                    procedure_lines.append(piece)
                index += 1
        procedure = _safe_procedure(" ".join(procedure_lines))
        if not procedure:
            continue
        pulls = _pulls_for(f"{title} {procedure}")
        learned.append(
            {
                "title": title,
                "kind": kind,
                "procedure": procedure,
                "simple_procedure": _simple_procedure(pulls),
                "data_pulls": pulls,
                "prompt": _prompt(title, pulls),
            }
        )
    return learned


def _read_lines(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from docx import Document

        return [" ".join(paragraph.text.split()) for paragraph in Document(str(path)).paragraphs]
    if suffix == ".pdf":
        import fitz

        lines: list[str] = []
        document = fitz.open(str(path))
        try:
            for page in document:
                lines.extend(page.get_text().splitlines())
        finally:
            document.close()
        return [" ".join(line.split()) for line in lines]
    return []


def _merge_objectives(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["kind"], _norm(row["title"]))
        current = by_key.get(key)
        if current is None or len(row["procedure"]) > len(current["procedure"]):
            by_key[key] = row
    return list(by_key.values())


def _sample_signature(root: Path) -> str:
    """A fingerprint of the sample files. File names are not stored."""
    parts: list[str] = []
    for kind, folder in (("disk", "disk"), ("mobile", "mobile")):
        source = root / folder
        if not source.is_dir():
            continue
        for path in sorted(source.iterdir()):
            if path.suffix.lower() not in {".pdf", ".docx"}:
                continue
            stat = path.stat()
            parts.append(f"{kind}|{stat.st_size}|{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _model_file(root: Path) -> Path:
    return root / "report_train_model.json"


def train_directory(root: Path | None = None) -> dict[str, Any]:
    """Train from reports_train/disk and reports_train/mobile and write the model."""
    root = root or _DIR
    learned: list[dict[str, Any]] = []
    documents: list[dict[str, str]] = []
    for kind, folder in (("disk", "disk"), ("mobile", "mobile")):
        source = root / folder
        if not source.is_dir():
            continue
        for path in sorted(source.iterdir()):
            if path.suffix.lower() not in {".pdf", ".docx"}:
                continue
            rows = learn_lines(_read_lines(path), kind=kind)
            learned.extend(rows)
            documents.append({"kind": kind, "objectives_learned": len(rows)})
    model = {
        "agent": AGENT_NAME,
        "version": "report-train-v2",
        "prompt_version": _PROMPT_VERSION,
        "sample_signature": _sample_signature(root),
        "purpose": (
            "Plain-English questions, section order, and which saved records each question may use. "
            "A report is written from this model, the saved records, and the case form. "
            "Sample names, accounts, web addresses, dates, and counts are not stored."
        ),
        "writing_style": _WRITING_STYLE,
        "documents": documents,
        "disk_sections": list(_DISK_SECTIONS),
        "mobile_sections": list(_MOBILE_SECTIONS),
        "mobile_device_fields": list(_MOBILE_DEVICE_FIELDS),
        "data_pulls": [_public_pull(pull) for pull in DATA_PULLS],
        "objectives": _merge_objectives(learned),
    }
    model_path = _model_file(root)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps(model, indent=2), encoding="utf-8")
    if root == _DIR:
        _CACHE["signature"] = model["sample_signature"]
        _CACHE["model"] = model
    return model


def ensure_trained(root: Path | None = None) -> dict[str, Any]:
    """Train on first use, and again when a sample report is added or replaced."""
    root = root or _DIR
    signature = _sample_signature(root)
    if root == _DIR and _CACHE.get("signature") == signature and _CACHE.get("model"):
        return _CACHE["model"]
    model_path = _model_file(root)
    current: dict[str, Any] = {}
    if model_path.is_file():
        try:
            current = json.loads(model_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    fresh = (
        current.get("sample_signature") == signature
        and current.get("prompt_version") == _PROMPT_VERSION
        and current.get("objectives") is not None
    )
    if not fresh:
        current = train_directory(root)
    elif root == _DIR:
        _CACHE["signature"] = signature
        _CACHE["model"] = current
    return current


def load_report_train_model() -> dict[str, Any]:
    try:
        return ensure_trained(_DIR)
    except Exception:
        return {
            "agent": AGENT_NAME,
            "objectives": [],
            "disk_sections": list(_DISK_SECTIONS),
            "mobile_sections": list(_MOBILE_SECTIONS),
            "writing_style": _WRITING_STYLE,
        }


def _best_objective(title: str, statement: str = "", *, kind: str | None = None) -> dict[str, Any] | None:
    title_n = _norm(title)
    query = set(_norm(f"{title} {statement}").split())
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for row in load_report_train_model().get("objectives") or []:
        if kind and row.get("kind") != kind:
            continue
        name = _norm(row.get("title"))
        if title_n and title_n == name:
            return row
        overlap = query & set(name.split())
        if len(overlap) < 2:
            continue
        score = len(overlap) / max(1, min(len(query), len(name.split())))
        if score > best[0]:
            best = (score, row)
    return best[1]


def trained_procedure(title: str, statement: str = "", *, kind: str | None = None) -> str:
    row = _best_objective(title, statement, kind=kind)
    if not row:
        return ""
    return str(row.get("simple_procedure") or row.get("procedure") or "").strip()


def data_plan(title: str, statement: str = "", *, kind: str | None = None) -> dict[str, Any]:
    """The plain-English questions and the saved records that can answer them."""
    row = _best_objective(title, statement, kind=kind)
    model = load_report_train_model()
    pulls = list((row or {}).get("data_pulls") or [])
    prompt = (row or {}).get("prompt") or _prompt(title, pulls)
    return {
        "agent": AGENT_NAME,
        "matched_title": (row or {}).get("title") or "",
        "procedure": str((row or {}).get("simple_procedure") or (row or {}).get("procedure") or ""),
        "prompt": prompt,
        "data_pulls": pulls,
        "writing_style": model.get("writing_style") or _WRITING_STYLE,
        "disk_sections": list(model.get("disk_sections") or _DISK_SECTIONS),
        "mobile_sections": list(model.get("mobile_sections") or _MOBILE_SECTIONS),
        "mobile_device_fields": list(model.get("mobile_device_fields") or _MOBILE_DEVICE_FIELDS),
    }


def plain_observation(title: str, brief: dict[str, Any] | None, intake: dict[str, Any] | None = None) -> str:
    """Write the finding in everyday words from the saved records and the case form."""
    brief = brief or {}
    objective = brief.get("objective") or {}
    statement = str(objective.get("statement") or objective.get("description") or "")
    plan = data_plan(title, statement)
    pulls = plan.get("data_pulls") or []
    looked = str((pulls[0].get("what") if pulls else "") or title or "this question").strip()
    looked = looked[0].lower() + looked[1:] if looked else "this question"
    sentences = [f"We checked the saved records for {looked}."]

    case_fact = brief.get("case_fact") or {}
    fact_status = str(case_fact.get("status") or "").upper()
    evidence = [str(item).strip().rstrip(".") for item in (case_fact.get("safe_evidence") or []) if str(item).strip()]
    entities: list[str] = []
    for values in (case_fact.get("key_entities") or {}).values():
        for value in values or []:
            text = str(value).strip()
            if text and text not in entities:
                entities.append(text)

    positive: list[dict[str, Any]] = []
    for result in brief.get("report_results") or []:
        role = str(result.get("objective_role") or "DIRECT").upper()
        count = result.get("reported_count")
        if role == "DIRECT" and isinstance(count, int) and count > 0:
            positive.append(result)

    found = fact_status == "CONFIRMED" or bool(evidence) or bool(entities) or bool(positive)
    if fact_status == "NOT_FOUND":
        found = False

    if found and (evidence or entities):
        if entities:
            sentences.append("The saved records name " + ", ".join(entities[:3]) + ".")
        if evidence:
            sentences.append("The saved records show " + "; ".join(evidence[:3]) + ".")
        means = str((pulls[0].get("means_if_found") if pulls else "") or "only the saved records were used.")
    elif found and positive:
        result = positive[0]
        count = int(result["reported_count"])
        unit = str(result.get("count_unit") or "record").replace("_", " ")
        word = unit if count != 1 else (unit[:-1] if unit.endswith("s") else unit)
        sentences.append(f"The saved records include {count} {word}.")
        means = str((pulls[0].get("means_if_found") if pulls else "") or "only the saved records were used.")
    else:
        sentences.append("The saved records do not show anything that answers this question.")
        means = str((pulls[0].get("means_if_none") if pulls else "") or "nothing in the saved records answers this question.")

    intake_text = ""
    for key in ("incident_summary", "background"):
        value = str((intake or {}).get(key) or "").strip()
        if value:
            intake_text = re.sub(r"\s+", " ", value)[:240].strip()
            break
    if intake_text and not re.search(r"[A-Za-z]:\\|\\\\|https?://", intake_text):
        if not intake_text.endswith("."):
            intake_text += "."
        sentences.append(f"The case form says: {intake_text}")

    if means.lower().startswith("this means"):
        closing = means
    else:
        closing = "This means " + means[0].lower() + means[1:]
    if not closing.endswith("."):
        closing += "."
    sentences.append(closing)
    return " ".join(sentences[:6])[:1400]


if __name__ == "__main__":
    trained = train_directory()
    print(AGENT_NAME, len(trained.get("objectives") or []), "objectives", len(trained.get("documents") or []), "documents")

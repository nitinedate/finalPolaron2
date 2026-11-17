"""Browse parsed WhatsApp records, including decrypted backups and text exports."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

from app.db.sql_helpers import fetchall, fetchone

_APPLE_EPOCH = 978307200
_INDEX_CACHE: dict[str, tuple[float, str, str]] = {}
_NAME_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_INDEX_TTL = 1800
_MEDIA_TOKEN = {
    "jpg", "jpeg", "png", "gif", "webp", "heic", "mp4", "mov", "opus", "m4a", "aac", "pdf",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "sticker",
}

DELETED = ("database_deleted", "wal_recovered", "journal_recovered", "freelist_candidate", "fragment")
FAMILIES = {"whatsapp_messages", "whatsapp_chats", "whatsapp_deleted_messages"}
_CONVERSATION = "COALESCE(NULLIF(data->>'conversation_id',''),forensic->>'source_path',artifact_id)"
# iOS ChatStorage stores Delete-for-Everyone as ZMESSAGETYPE=14 and still marks
# the row allocated. Android sets deleted_flag / revoked. The recovery state
# column alone therefore hides every one of those messages from the deleted board.
_REVOKED_SQL = """(
  COALESCE(data->>'message_type','') = '14'
  OR lower(COALESCE(data->>'deleted_flag','')) IN ('1','true')
  OR lower(COALESCE(data->>'revoked','')) IN ('1','true')
)"""
_DELETED_SQL = f"(state=ANY(CAST(:deleted AS text[])) OR {_REVOKED_SQL})"


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def _session_labels(db, job_id) -> dict[str, dict]:
    """Map ChatStorage session id (Z_PK) to the saved partner or group name.

    Message rows only store the numeric session id. The display name lives on
    the conversation row (ZPARTNERNAME / ZCONTACTJID).
    """
    rows = fetchall(db, """
        SELECT COALESCE(data->'raw'->>'Z_PK', data->>'chat_row_id', data->>'conversation_id') AS pk,
               NULLIF(btrim(COALESCE(
                   data->'raw'->>'ZPARTNERNAME',
                   data->>'conversation_name',
                   data->>'display_name',
                   data->>'partner_name'
               )), '') AS name,
               NULLIF(btrim(COALESCE(
                   data->'raw'->>'ZCONTACTJID',
                   data->>'chat_jid',
                   data->>'jid'
               )), '') AS jid
        FROM mobile_normalized_artifacts
        WHERE job_id=:jid
          AND artifact_type IN ('conversation', 'app_conversation')
          AND COALESCE(data->>'application', 'whatsapp')='whatsapp'
    """, {"jid": job_id})
    out: dict[str, dict] = {}
    for row in rows:
        pk = str(row.get("pk") or "").strip()
        name = str(row.get("name") or "").strip()
        jid = str(row.get("jid") or "").strip()
        if not pk or not name:
            continue
        out[pk] = {"name": name, "jid": jid, "is_group": jid.lower().endswith("@g.us")}
    return out


def _identifier(value: str) -> bool:
    text = (value or "").strip()
    low = text.lower()
    if "@" in low:
        return True
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) >= 10 and len(digits) >= len(text) - 2


def _apply_session_label(row: dict, labels: dict[str, dict], *, heading: bool = False) -> None:
    meta = row.get("metadata") or {}
    info = labels.get(str(meta.get("conversation_id") or "").strip())
    if not info:
        return
    name = str(info.get("name") or "").strip()
    if not name:
        return
    meta["conversation"] = name
    meta["conversation_name"] = name
    meta["person_name"] = name
    jid = str(info.get("jid") or "").strip()
    if jid:
        meta["chat_jid"] = jid
        meta["is_group"] = bool(info.get("is_group"))
    if heading:
        row["title"] = name
        meta["sender_display_name"] = name
        return
    sender = str(meta.get("sender") or "").strip()
    from_me = str(meta.get("from_me") or "").lower() in {"1", "true"}
    if from_me or info.get("is_group") or not _identifier(sender):
        return
    meta["sender_display_name"] = name
    meta["sender"] = name
    preview = str(meta.get("preview_body") or "")
    if sender and sender in preview:
        meta["preview_body"] = preview.replace(sender, name, 1)


def _cocoa_seconds(value) -> float | None:
    if value is None or value == "":
        return None
    if hasattr(value, "timestamp"):
        return float(value.timestamp()) - _APPLE_EPOCH
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() - _APPLE_EPOCH


def _clean_index_text(text: str) -> str:
    words = str(text or "").split()
    while words and (
        words[-1] in {"G", "gp"}
        or (words[-1].startswith("g") and len(words[-1]) >= 8 and any(ch.isupper() for ch in words[-1]))
    ):
        words.pop()
    return " ".join(words).strip()


def _recovered_body(text: str) -> str | None:
    cleaned = _clean_index_text(text)
    if not cleaned or _identifier(cleaned):
        return None
    words = cleaned.split()
    if len(words) == 1 and words[0].lower() in _MEDIA_TOKEN:
        kind = words[0].lower()
        return f"Deleted {kind} attachment. The file is not in this extraction."
    if len(cleaned) < 2:
        return None
    return cleaned


def _index_paths(db, job_id: str) -> tuple[str | None, str | None]:
    cached = _INDEX_CACHE.get(job_id)
    if cached and time.monotonic() - cached[0] < _INDEX_TTL and os.path.exists(cached[1]) and os.path.exists(cached[2]):
        return cached[1], cached[2]
    from app.services.artifact_locator import read_from_package

    rows = fetchall(db, """
        SELECT id::text AS id, file_path, file_name, size_bytes, sha256
        FROM job_artifacts
        WHERE job_id=:j
          AND (
            lower(file_name) LIKE '%chatstorage.sqlite'
            OR lower(file_name) LIKE '%chatsearch%'
          )
          AND lower(file_name) NOT LIKE '%newsletter%'
          AND lower(file_name) NOT LIKE '%-wal'
          AND lower(file_name) NOT LIKE '%-shm'
        ORDER BY size_bytes DESC NULLS LAST
    """, {"j": job_id})
    chosen: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("file_name") or "").lower()
        kind = "store" if "chatstorage" in name else "search" if "chatsearch" in name else ""
        if kind and kind not in chosen:
            chosen[kind] = row
    paths: dict[str, str] = {}
    for kind, row in chosen.items():
        data = read_from_package(db, job_id, row, max_bytes=200_000_000)
        if not data or not data[:16].startswith(b"SQLite format"):
            continue
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        paths[kind] = path
    if "store" not in paths or "search" not in paths:
        return None, None
    _INDEX_CACHE[job_id] = (time.monotonic(), paths["store"], paths["search"])
    return paths["store"], paths["search"]


def _chat_token(store: sqlite3.Connection, search: sqlite3.Connection, session_id: str) -> str | None:
    try:
        stamps = store.execute(
            """
            SELECT ZMESSAGEDATE FROM ZWAMESSAGE
            WHERE ZCHATSESSION=? AND ZTEXT IS NOT NULL AND length(ZTEXT)>1
            ORDER BY ZMESSAGEDATE DESC LIMIT 6
            """,
            (int(session_id),),
        ).fetchall()
    except sqlite3.Error:
        return None
    counts: dict[str, int] = {}
    for (stamp,) in stamps:
        if stamp is None:
            continue
        try:
            hit = search.execute(
                """
                SELECT d.c2chat FROM metadata m
                JOIN docs_content d ON d.docid=m.docID
                WHERE m.date BETWEEN ? AND ?
                ORDER BY abs(m.date - ?) LIMIT 1
                """,
                (float(stamp) - 1, float(stamp) + 1, float(stamp)),
            ).fetchone()
        except sqlite3.Error:
            return None
        if hit and hit[0]:
            counts[str(hit[0])] = counts.get(str(hit[0]), 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _recover_deleted_text(db, job_id: str, rows: list[dict]) -> None:
    """Fill Delete-for-Everyone rows from ChatSearch when WhatsApp left the words there.

    The chat table clears ZTEXT. The search index sometimes still has the same
    message, stored as its words rather than the original sentence.
    """
    pending = []
    for row in rows:
        meta = row.get("metadata") or {}
        if not meta.get("is_deleted"):
            continue
        body = str(meta.get("body") or "")
        if "removed the original text" not in body and "this message was deleted" not in body.lower():
            continue
        session_id = str(meta.get("conversation_id") or "").strip()
        stamp = _cocoa_seconds(meta.get("timestamp") or row.get("artifact_datetime"))
        if session_id.isdigit() and stamp is not None:
            pending.append((row, session_id, stamp))
    if not pending:
        return
    store_path, search_path = _index_paths(db, job_id)
    if not store_path or not search_path:
        return
    try:
        store = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
        search = sqlite3.connect(f"file:{search_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        tokens: dict[str, str | None] = {}
        for row, session_id, stamp in pending:
            if session_id not in tokens:
                tokens[session_id] = _chat_token(store, search, session_id)
            token = tokens[session_id]
            if not token:
                continue
            try:
                hit = search.execute(
                    """
                    SELECT d.c0text FROM metadata m
                    JOIN docs_content d ON d.docid=m.docID
                    WHERE d.c2chat=? AND m.date BETWEEN ? AND ?
                    ORDER BY abs(m.date - ?) LIMIT 1
                    """,
                    (token, stamp - 0.5, stamp + 0.5, stamp),
                ).fetchone()
            except sqlite3.Error:
                continue
            recovered = _recovered_body(str(hit[0] or "")) if hit else None
            if not recovered:
                continue
            meta = row["metadata"]
            meta["body"] = recovered
            meta["original_content"] = recovered
            meta["message_title"] = recovered.splitlines()[0][:140]
            attachment_only = recovered.startswith("Deleted ") and "file is not in this extraction" in recovered
            meta["recovery"] = {
                "state": "search index",
                "classification": (
                    "attachment type only; file not in this extraction"
                    if attachment_only else "words recovered; original order not kept"
                ),
                "confidence": "low" if attachment_only else "medium",
            }
            preview = str(meta.get("preview_body") or "")
            placeholder = "This message was deleted. WhatsApp removed the original text from this extraction."
            if placeholder in preview:
                meta["preview_body"] = preview.replace(placeholder, recovered, 1)
            row["title"] = meta["message_title"]
    finally:
        store.close()
        search.close()


def _varint(blob: bytes, index: int) -> tuple[int | None, int | None]:
    value = 0
    shift = 0
    for step in range(5):
        if index + step >= len(blob):
            return None, None
        byte = blob[index + step]
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index + step + 1
        shift += 7
    return None, None


def _readable_text(chunk: bytes) -> str:
    if not chunk or len(chunk) < 4 or len(chunk) > 4000:
        return ""
    if any(byte < 9 or (13 < byte < 32) for byte in chunk):
        return ""
    try:
        text = chunk.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""
    letters = sum(1 for ch in text if ch.isalpha())
    if letters < 2:
        return ""
    compact = text.replace(" ", "")
    if compact and compact.isdigit():
        return ""
    if " " not in text and all(ch in "0123456789abcdefABCDEF" for ch in text) and len(text) >= 16:
        return ""
    if text.upper() in {"TRANSACTIONAL", "UTILITY", "MARKETING", "AUTHENTICATION", "THIRD_PARTY"}:
        return ""
    return text


def _template_strings(blob: bytes) -> list[str]:
    """Readable strings stored as protobuf fields inside a WhatsApp template blob."""
    found: list[str] = []
    index = 0
    size = len(blob or b"")
    while index < size - 2:
        tag, after_tag = _varint(blob, index)
        if tag is not None and after_tag is not None and (tag & 7) == 2:
            length, after_len = _varint(blob, after_tag)
            if (
                length is not None and after_len is not None
                and 4 <= length <= 4000 and after_len + length <= size
            ):
                text = _readable_text(blob[after_len:after_len + length])
                if text and text not in found:
                    found.append(text)
                    index = after_len + length
                    continue
        index += 1
    return [text for text in found if not any(text != other and text in other for other in found)]


def _recover_template_text(db, job_id: str, rows: list[dict]) -> None:
    """Fill business-template rows whose wording is stored beside the message.

    Current WhatsApp keeps these as message type 19 with an empty ZTEXT and the
    sentence in ZWAMEDIAITEM.ZMETADATA. They are live messages, not deletions.
    """
    pending: list[tuple[dict, int]] = []
    for row in rows:
        meta = row.get("metadata") or {}
        if meta.get("is_deleted"):
            continue
        body = str(meta.get("body") or "")
        if body not in {"", "No message text recovered."}:
            continue
        forensic = meta.get("forensic") if isinstance(meta.get("forensic"), dict) else {}
        raw_id = forensic.get("source_row_id") or meta.get("source_row_id")
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        pending.append((row, row_id))
    if not pending:
        return
    store_path, _search_path = _index_paths(db, job_id)
    if not store_path:
        return
    try:
        store = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        ids = [row_id for _row, row_id in pending]
        blobs: dict[int, bytes] = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            try:
                queried = store.execute(
                    f"SELECT ZMESSAGE, ZMETADATA FROM ZWAMEDIAITEM WHERE ZMESSAGE IN ({marks})",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                return
            for message_id, blob in queried:
                if isinstance(blob, bytes) and len(blob) > 20:
                    blobs[int(message_id)] = blob
        placeholder = "No message text recovered."
        for row, row_id in pending:
            parts = _template_strings(blobs.get(row_id, b""))
            if not parts:
                continue
            recovered = "\n".join(parts).strip()
            if len(recovered) < 4:
                continue
            meta = row["metadata"]
            meta["body"] = recovered
            meta["message_title"] = recovered.splitlines()[0][:140]
            preview = str(meta.get("preview_body") or "")
            if placeholder in preview:
                meta["preview_body"] = preview.replace(placeholder, recovered, 1)
            row["title"] = meta["message_title"]
    finally:
        store.close()


def _thread_summary(group_id, matched, *, name, is_group, total) -> dict:
    def _sum(key: str) -> int:
        return sum(int(record.get(key) or 0) for record in matched)

    return {
        "person_id": group_id,
        "person_name": name or "",
        "is_group": bool(is_group),
        "message_count": int(total or 0),
        "current_count": _sum("current_count"),
        "deleted_count": _sum("deleted_count"),
        "historical_count": _sum("historical_count"),
        "media_count": _sum("media_count"),
        "call_count": 0,
        "fragment_count": 0,
        "applications": ["WhatsApp"],
        "participants": [],
        "participant_count": 0,
    }


def _message_revoked(data: dict) -> bool:
    """True for Delete-for-Everyone placeholders and explicit deleted flags.

    iOS keeps these rows in the live table with message type 14. Their state
    stays ``allocated``, so a state-only deleted filter returns an empty board.
    """
    if str(data.get("message_type") or "") == "14":
        return True
    if str(data.get("deleted_flag") or "").lower() in {"1", "true"}:
        return True
    if str(data.get("revoked") or "").lower() in {"1", "true"}:
        return True
    return False


def whatsapp_recovery_notice(job_id, family, db=None):
    statuses = []
    if db is not None:
        table = fetchone(db, "SELECT to_regclass('mobile_normalized_artifacts') AS t")
        if table and table.get("t"):
            rows = fetchall(db, """SELECT data,forensic FROM mobile_normalized_artifacts
                WHERE job_id=:jid AND data->>'application'='whatsapp'
                AND artifact_type='app_backup_encrypted' ORDER BY artifact_id""", {"jid": job_id})
            for row in rows:
                data = _json(row.get("data"))
                forensic = _json(row.get("forensic"))
                diag = data.get("decrypt_diagnostics") or {}
                statuses.append({"source_path": forensic.get("source_path") or data.get("path"),
                                 "state": data.get("decryption_state") or "unverified",
                                 "reason": diag.get("reason") or "not_processed",
                                 "keys_tried": diag.get("keys_tried") or 0})
    body = (
        "No readable WhatsApp records in this view yet.\n\n"
        "Supported sources: plaintext msgstore.db / ChatStorage.sqlite (with available WAL files), "
        "authenticated crypt12/14/15 backups with a matching key, structurally checked legacy crypt5/7/8, or WhatsApp TXT chat exports with their media.\n\n"
        "crypt7/8/12/14: collect files/key (typically 158 bytes) from the matching WhatsApp installation. "
        "crypt5 requires the exact original Android Google account email. Legacy CBC formats have no authentication tag. "
        "crypt15: collect files/encrypted_backup.key (raw or Java-serialized), or enter the owner's 64-character backup key. "
        "An adb run-as error saved as a key file is invalid; a backup password alone is not a local decryption key.\n\n"
        "Save a valid key under Case Intake → Decrypt keys, then choose Reprocess mobile evidence when the job is idle. "
        "Every collected key is tried against each backup. Backup status gives the reason if authentication fails.\n\n"
        "For acquisition, include private app databases/keys from an already-accessible filesystem extraction, "
        "including WhatsApp Business and Android user profiles. If the owner's phone can open WhatsApp, export each required chat "
        "with media and import the export as evidence. An ordinary shared-storage ADB/MTP pull cannot read a production app's private sandbox.\n"
        "Exports contain only the history the app exports. Deleted recovery requires surviving database rows, WAL or residual evidence; "
        "overwritten/absent messages cannot be supplied by a parser."
    )
    if statuses:
        totals = {}
        for item in statuses:
            reason = item["reason"] if item["state"] != "decrypted" else "decrypted"
            totals[reason] = totals.get(reason, 0) + 1
        body += "\n\nCollected backup results: " + "; ".join(f"{reason}: {count}" for reason, count in sorted(totals.items()))
    return {"id": f"ev-notice-{family}", "job_id": job_id, "artifact_type": "notice",
            "title": "WhatsApp recovery sources and next steps", "source_path": None,
            "metadata": {"evidence_kind": "notice", "preview_body": body, "body": body,
                         "backup_results": statuses}, "tags": ["notice", "no_chat_data"]}


def _saved_names(db, job_id) -> dict[str, str]:
    """JID -> saved contact or group name. Cached; a missing address book returns {}."""
    cached = _NAME_CACHE.get(str(job_id))
    if cached and time.monotonic() - cached[0] < _INDEX_TTL:
        return cached[1]
    names: dict[str, str] = {}
    try:
        import sqlite3
        import tempfile
        from app.services.mobile_forensic.sqlite_counts import _read_artifact_bytes
        from app.services.mobile_forensic.whatsapp_names import jid_display_names, phone_names_from_job

        phone_names = phone_names_from_job(db, job_id)
        source = fetchone(db, """
            SELECT forensic->>'source_path' AS path
            FROM mobile_normalized_artifacts
            WHERE job_id=:jid AND artifact_type='app_message'
              AND coalesce(forensic->>'source_path','') ILIKE '%msgstore.db'
              AND coalesce(forensic->>'source_path','') NOT ILIKE '%.crypt%'
            LIMIT 1
        """, {"jid": job_id})
        raw = None
        if source and source.get("path"):
            raw = _read_artifact_bytes(db, job_id, str(source["path"]))
        if raw and raw[:16].startswith(b"SQLite format"):
            fd, path = tempfile.mkstemp(suffix=".db")
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    names = jid_display_names(conn, phone_names)
                finally:
                    conn.close()
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
    except Exception:
        names = {}
    _NAME_CACHE[str(job_id)] = (time.monotonic(), names)
    return names


def _prefer_saved_name(current: str, jid: str, names: dict[str, str] | None) -> str:
    saved = ""
    if names:
        saved = str(names.get(jid) or names.get(str(jid).lower()) or "")
    if saved and (not current or current == jid or _identifier(current)):
        return saved
    return current


def _row(record, job_id, names: dict[str, str] | None = None):
    from app.services.artifact_group_browse import _person_key_from_row

    data = _json(record.get("data"))
    forensic = _json(record.get("forensic"))
    state = record.get("state") or forensic.get("state") or "unverified"
    revoked = _message_revoked(data)
    deleted = state in DELETED or revoked
    candidate = state in {"freelist_candidate", "fragment"}
    conversation = str(data.get("conversation_id") or forensic.get("source_path") or record["artifact_id"])
    title = _prefer_saved_name(
        str(data.get("conversation_name") or data.get("conversation_title") or conversation),
        conversation,
        names,
    )
    body = str(data.get("body") or "")
    media_name = str(data.get("media_name") or "").strip()
    if _identifier(body) and " " not in body.strip():
        body = ""
    if revoked and not body.strip():
        body = "This message was deleted. WhatsApp removed the original text from this extraction."
    elif not body.strip() and media_name:
        body = f"[attachment] {media_name}"
    elif not body.strip():
        body = "No message text recovered."
    message_title = body.strip().splitlines()[0][:140]
    stamp = record.get("timestamp_utc")
    stamp = stamp.isoformat() if hasattr(stamp, "isoformat") else stamp
    sender = str(data.get("sender") or "")
    saved_sender = _prefer_saved_name(str(data.get("sender_display_name") or sender), sender, names)
    meta = {**data, "evidence_kind": "whatsapp_message", "social_app": "whatsapp",
            "normalized_artifact_id": record["artifact_id"],
            "conversation_id": conversation, "conversation": title,
            "conversation_name": title,
            "chat_jid": conversation if "@" in conversation else None,
            "is_group": conversation.endswith("@g.us"), "is_deleted": None if candidate else deleted,
            "is_deleted_candidate": candidate,
            "body": body, "message_title": message_title,
            "sender_display_name": saved_sender if saved_sender and saved_sender != sender else data.get("sender_display_name"),
            "timestamp": stamp or data.get("timestamp_local"),
            "source_path": forensic.get("source_path"), "source_sha256": forensic.get("source_sha256"),
            "forensic_state": state, "forensic": forensic,
            "classification": forensic.get("ui_label") or state.upper(),
            # Legacy grouping treats any recovery_state as deleted. Preserve
            # historical/exported state separately so it cannot leak into Deleted.
            "recovery_state": state if deleted else "",
            "preview_body": f"WhatsApp: {title}\nTime: {stamp or data.get('timestamp_local') or 'unspecified'}\n"
                            f"Sender: {saved_sender or data.get('sender') or 'unspecified'}\nState: {state}\n\n{body}\n\n"
                            f"Source: {forensic.get('source_path') or ''}\nSHA256: {forensic.get('source_sha256') or ''}"}
    row = {"id": "ev-mobile-" + record["artifact_id"], "job_id": job_id, "artifact_type": "whatsapp_message",
           "title": title, "source_path": forensic.get("source_path"), "artifact_datetime": stamp,
           "metadata": meta, "tags": ["whatsapp", state] + (["deleted"] if deleted else [])}
    person, label, group = _person_key_from_row(row)
    meta.update({"person_id": person, "base_person_id": person, "person_name": label, "is_group": group})
    return row


def _link_media(db, job_id, rows):
    names = sorted({str(row["metadata"].get("media_name") or "").lower() for row in rows} - {""})
    if not names:
        return
    files = fetchall(db, """SELECT id,file_path,file_name FROM job_artifacts
        WHERE job_id=:jid AND lower(file_name)=ANY(CAST(:names AS text[]))""", {"jid": job_id, "names": names})
    for row in rows:
        meta = row["metadata"]
        name = str(meta.get("media_name") or "").lower()
        path = str(meta.get("media_path") or "").replace("\\", "/")
        matches = [item for item in files if str(item["file_name"]).lower() == name]
        exact = [item for item in matches if str(item["file_path"]).replace("\\", "/") == path]
        match = exact[0] if len(exact) == 1 else matches[0] if len(matches) == 1 else None
        if match:
            meta["media_artifact_id"] = str(match["id"])
            meta["media_path"] = match["file_path"]


def list_whatsapp_evidence(db, job_id, *, family="whatsapp_messages", page=1, page_size=50,
                           group_by=None, group_id=None, message_filter=None, q=None):
    """Return None before normalized parsing; otherwise SQL-page complete records."""
    if family not in FAMILIES:
        return None
    table = fetchone(db, "SELECT to_regclass('mobile_normalized_artifacts') AS t")
    if not table or not table.get("t"):
        return None
    present = fetchone(db, """SELECT 1 AS found FROM mobile_normalized_artifacts
        WHERE job_id=:jid AND data->>'application'='whatsapp' AND artifact_type='app_message' LIMIT 1""", {"jid": job_id})
    if not present:
        return None
    page = max(int(page or 1), 1)
    page_size = max(1, min(int(page_size or 50), 200))
    params = {"jid": job_id, "deleted": list(DELETED), "limit": page_size, "offset": (page - 1) * page_size}
    where = "job_id=:jid AND data->>'application'='whatsapp' AND artifact_type='app_message'"
    deleted_only = family == "whatsapp_deleted_messages" or str(group_id or "").endswith("::deleted")
    if deleted_only or message_filter == "deleted":
        where += f" AND {_DELETED_SQL}"
    elif message_filter == "current":
        where += f" AND state='allocated' AND NOT {_REVOKED_SQL}"
    elif family != "whatsapp_deleted_messages" and not group_id:
        where += " AND NOT(state=ANY(CAST(:deleted AS text[])))"
    if message_filter == "media":
        where += " AND (data->>'has_attachment'='true' OR NULLIF(data->>'media_name','') IS NOT NULL)"
    if message_filter == "calls":
        where += " AND data->>'message_type_label' ILIKE '%call%'"
    if q:
        where += " AND strpos(lower(data::text),lower(:q))>0"
        params["q"] = str(q)
    grouped = group_by in {"person", "conversation"}
    summaries = []
    labels = _session_labels(db, job_id) if grouped or group_id else {}
    names = _saved_names(db, job_id)
    matched_summaries = []
    thread_name = None
    thread_group = False
    if grouped or group_id:
        base = f"SELECT *, {_CONVERSATION} AS conv FROM mobile_normalized_artifacts WHERE {where}"
        summaries = fetchall(db, f"""WITH base AS ({base}), counts AS (
            SELECT conv,count(*) AS message_count,
                count(*) FILTER(WHERE state='allocated' AND NOT {_REVOKED_SQL}) AS current_count,
                count(*) FILTER(WHERE {_DELETED_SQL}) AS deleted_count,
                count(*) FILTER(WHERE state IN ('historical','backup_historical')) AS historical_count,
                count(*) FILTER(WHERE data->>'has_attachment'='true') AS media_count
            FROM base GROUP BY conv), latest AS (
            SELECT DISTINCT ON(conv) * FROM base ORDER BY conv,timestamp_utc DESC NULLS LAST,artifact_id DESC)
            SELECT latest.*,counts.message_count,counts.current_count,counts.deleted_count,counts.historical_count,counts.media_count,
                deleted_pick.deleted_body
            FROM latest JOIN counts USING(conv)
            LEFT JOIN (
                SELECT DISTINCT ON (conv) conv,
                    COALESCE(NULLIF(btrim(data->>'body'), ''), 'This message was deleted. WhatsApp removed the original text from this extraction.') AS deleted_body
                FROM base
                WHERE {_DELETED_SQL}
                ORDER BY conv, timestamp_utc DESC NULLS LAST, artifact_id DESC
            ) deleted_pick USING (conv)""", params)
    if group_id:
        want = str(group_id).removesuffix("::deleted")
        conversations = []
        for record in summaries:
            labeled = _row(record, job_id, names)
            _apply_session_label(labeled, labels, heading=True)
            meta = labeled["metadata"]
            identity = meta["person_id"] if group_by == "person" else meta["conversation_id"]
            if identity == want:
                conversations.append(record["conv"])
                matched_summaries.append(record)
                thread_name = labeled.get("title") or thread_name
                thread_group = bool(meta.get("is_group"))
        where += f" AND {_CONVERSATION}=ANY(CAST(:conversations AS text[]))"
        params["conversations"] = conversations
    if grouped and not group_id:
        buckets = {}
        for record in summaries:
            row = _row(record, job_id, names)
            _apply_session_label(row, labels, heading=True)
            meta = row["metadata"]
            identity = meta["person_id"] if group_by == "person" else meta["conversation_id"]
            if deleted_only:
                identity += "::deleted"
            if identity not in buckets:
                kind = "group" if meta["is_group"] else "person" if group_by == "person" else "conversation"
                row.update({"id": "ev-normalized-" + hashlib.sha256(identity.encode()).hexdigest()[:24], "artifact_type": kind})
                meta.update({"evidence_kind": kind, "person_id": identity, "deleted_bucket": deleted_only,
                             "applications": ["WhatsApp"], "message_count": 0, "current_count": 0,
                             "deleted_count": 0, "historical_count": 0, "media_count": 0})
                buckets[identity] = row
            bucket = buckets[identity]["metadata"]
            for key in ("message_count", "current_count", "deleted_count", "historical_count", "media_count"):
                bucket[key] += int(record.get(key) or 0)
            deleted_body = str(record.get("deleted_body") or "").strip()
            if deleted_body and not bucket.get("deleted_preview"):
                bucket["deleted_preview"] = deleted_body[:500]
                bucket["last_message"] = f"Deleted: {deleted_body[:80]}"
            deleted_line = (
                f"\n\nDeleted message:\n{bucket['deleted_preview']}"
                if bucket.get("deleted_preview") else ""
            )
            bucket["preview_body"] = (f"WhatsApp: {bucket['conversation']}\nMessages: {bucket['message_count']}\n"
                                      f"Current: {bucket['current_count']}\nHistorical: {bucket['historical_count']}\n"
                                      f"Deleted/recovery candidates: {bucket['deleted_count']}\nMedia references: {bucket['media_count']}"
                                      f"{deleted_line}")
        from app.services.artifact_group_browse import chat_label_sort_key

        rows = sorted(buckets.values(), key=lambda row: chat_label_sort_key(str(row.get("title") or "")))
        total = len(rows)
        rows = rows[params["offset"]:params["offset"] + page_size]
        summary = None
    else:
        total = int((fetchone(db, f"SELECT count(*) AS n FROM mobile_normalized_artifacts WHERE {where}", params) or {}).get("n") or 0)
        records = fetchall(db, f"""SELECT * FROM mobile_normalized_artifacts WHERE {where}
            ORDER BY CASE WHEN {_DELETED_SQL} THEN 0 ELSE 1 END,
                timestamp_utc DESC NULLS LAST,forensic->>'source_path',
                CASE WHEN data->>'source_line_start' ~ '^[0-9]+$' THEN (data->>'source_line_start')::bigint END,
                artifact_id LIMIT :limit OFFSET :offset""", params)
        rows = [_row(record, job_id, names) for record in records]
        for row in rows:
            _apply_session_label(row, labels, heading=False)
            shown = str((row.get("metadata") or {}).get("message_title") or "").strip()
            if shown:
                row["title"] = shown
        _link_media(db, job_id, rows)
        if group_id:
            _recover_deleted_text(db, job_id, rows)
            _recover_template_text(db, job_id, rows)
        summary = _thread_summary(
            group_id, matched_summaries, name=thread_name, is_group=thread_group, total=total,
        ) if group_id else None
    return {"items": rows, "total": total, "page": page, "page_size": page_size,
            "group_by": group_by, "group_id": group_id, "message_filter": message_filter or "all",
            "thread_summary": summary, "evidence_domain": "whatsapp_message", "evidence_label": family,
            "normalized_evidence": True}

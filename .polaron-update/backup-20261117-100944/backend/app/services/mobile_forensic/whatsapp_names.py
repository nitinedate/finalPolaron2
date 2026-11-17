"""Resolve WhatsApp chat labels to names the phone already stored.

A modern Android chat is often ``<digits>@lid``. That value is a linked
identity, not a phone number and not a contact name. The phone number is
on ``jid`` / ``jid_map``, and the name is on the Android address book
(or on ``lid_display_name`` / ``wa_contacts`` when WhatsApp saved one).
"""

from __future__ import annotations

import sqlite3


def phone_key(value: object) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    return digits[-10:]


def looks_like_identifier(value: object) -> bool:
    text = str(value or "").strip()
    if not text or "@" in text:
        return True
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) >= 8 and len(digits) >= len(text) - 2


def parse_android_contacts_dump(text: str) -> dict[str, str]:
    """Phone suffix -> display name from an Android contacts provider dump."""
    records: list[str] = []
    current = ""
    for line in str(text or "").splitlines():
        if line.startswith("Row:"):
            if current:
                records.append(current)
            current = line.strip()
        elif current:
            current += " " + line.strip()
    if current:
        records.append(current)
    names: dict[str, str] = {}
    for record in records:
        if "vnd.android.cursor.item/phone_v2" not in record:
            continue
        name = _field(record, "display_name")
        number = _field(record, "data1")
        key = phone_key(number)
        if not key or not name or name == "NULL" or looks_like_identifier(name):
            continue
        names.setdefault(key, name)
    return names


def jid_display_names(conn: sqlite3.Connection, phone_names: dict[str, str] | None = None) -> dict[str, str]:
    """Map a WhatsApp JID (``user@server``) to a saved human name."""
    phone_names = dict(phone_names or {})
    tables = {str(row[0]).lower() for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "jid" not in tables:
        return {}
    jids: dict[int, dict[str, str]] = {}
    for row_id, user, server, raw in conn.execute("SELECT _id, user, server, raw_string FROM jid"):
        try:
            jid_id = int(row_id)
        except (TypeError, ValueError):
            continue
        user_s = str(user or "")
        server_s = str(server or "")
        raw_s = str(raw or "").strip() or (f"{user_s}@{server_s}" if user_s and server_s else "")
        jids[jid_id] = {"user": user_s, "server": server_s, "raw": raw_s}

    subjects: dict[int, str] = {}
    if "chat" in tables:
        for jid_id, subject in conn.execute("SELECT jid_row_id, subject FROM chat"):
            label = str(subject or "").strip()
            if not label or looks_like_identifier(label):
                continue
            try:
                subjects[int(jid_id)] = label
            except (TypeError, ValueError):
                continue

    profile_names: dict[int, str] = {}
    if "lid_display_name" in tables:
        for jid_id, display_name, username in conn.execute(
            "SELECT lid_row_id, display_name, username FROM lid_display_name"
        ):
            label = _human(display_name) or _human(username)
            if not label:
                continue
            try:
                profile_names[int(jid_id)] = label
            except (TypeError, ValueError):
                continue

    contact_raw: dict[str, str] = {}
    if "wa_contacts" in tables:
        columns = {str(row[1]).lower() for row in conn.execute("PRAGMA table_info(wa_contacts)")}
        if "jid" in columns:
            name_cols = [col for col in ("display_name", "wa_name", "given_name", "family_name") if col in columns]
            number_col = "number" if "number" in columns else ""
            selected = ", ".join(name_cols + ([number_col] if number_col else []))
            if selected:
                for record in conn.execute(f"SELECT jid, {selected} FROM wa_contacts"):
                    raw = str(record[0] or "").strip()
                    label = ""
                    values = record[1:]
                    name_values = values[:-1] if number_col else values
                    for value in name_values:
                        label = _human(value)
                        if label:
                            break
                    if raw and label:
                        contact_raw[raw] = label
                        matched = _jid_id_for_raw(jids, raw)
                        if matched >= 0:
                            profile_names.setdefault(matched, label)
                    if number_col and label:
                        key = phone_key(values[-1])
                        if key:
                            phone_names.setdefault(key, label)

    mapped_phone: dict[int, str] = {}
    if "jid_map" in tables:
        for lid_id, phone_id in conn.execute("SELECT lid_row_id, jid_row_id FROM jid_map"):
            try:
                other = jids.get(int(phone_id))
                if other and other["server"] == "s.whatsapp.net" and other["user"]:
                    mapped_phone[int(lid_id)] = other["user"]
            except (TypeError, ValueError):
                continue

    names: dict[str, str] = {}
    for jid_id, info in jids.items():
        raw = info["raw"]
        if not raw:
            continue
        if info["server"] == "g.us" and jid_id in subjects:
            names[raw] = subjects[jid_id]
            continue
        if jid_id in profile_names and profile_names[jid_id]:
            names[raw] = profile_names[jid_id]
            continue
        if jid_id in subjects and info["server"] not in {"lid", "s.whatsapp.net"}:
            names[raw] = subjects[jid_id]
            continue
        phone = info["user"] if info["server"] == "s.whatsapp.net" else mapped_phone.get(jid_id)
        saved = phone_names.get(phone_key(phone) or "")
        if saved:
            names[raw] = saved
    for raw, label in contact_raw.items():
        if raw and label:
            names.setdefault(raw, label)
    return names


def phone_names_from_job(db, job_id: str) -> dict[str, str]:
    """Read the collected Android contacts dump for this job, when one exists."""
    from app.db.sql_helpers import fetchone
    from app.services.mobile_forensic.sqlite_counts import _read_artifact_bytes

    row = fetchone(
        db,
        """SELECT file_path FROM job_artifacts
           WHERE job_id=:jid AND lower(file_name)='contacts.txt'
           ORDER BY size_bytes DESC NULLS LAST LIMIT 1""",
        {"jid": job_id},
    )
    if not row or not row.get("file_path"):
        return {}
    data = _read_artifact_bytes(db, job_id, str(row["file_path"]), max_bytes=20_000_000)
    if not data:
        return {}
    return parse_android_contacts_dump(data.decode("utf-8", "replace"))


def _field(record: str, name: str) -> str:
    token = name + "="
    start = record.find(token)
    if start < 0:
        return ""
    start += len(token)
    end = record.find(",", start)
    value = record[start:] if end < 0 else record[start:end]
    return value.strip()


def _human(value: object) -> str:
    text = str(value or "").strip()
    if not text or text == "NULL" or looks_like_identifier(text):
        return ""
    return text


def _jid_id_for_raw(jids: dict[int, dict[str, str]], raw: str) -> int:
    wanted = raw.strip().lower()
    for jid_id, info in jids.items():
        if info["raw"].lower() == wanted:
            return jid_id
    return -1

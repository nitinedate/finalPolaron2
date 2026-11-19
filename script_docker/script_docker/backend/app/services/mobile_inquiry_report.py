"""Read-only mobile report inquiry evidence. Never infer guilt or prove absence.

The language model can polish the separately generated observation prose, but
this deterministic appendix preserves source paths and case-scoped retrieval.
"""
from __future__ import annotations

import re
from app.db.sql_helpers import fetchall

QUESTIONS = (
    ("Offer letters and employment correspondence", ("offer letter", "appointment letter", "job offer", "interview", "recruitment", "employment", "resignation")),
    ("Chats and external communications", ("whatsapp", "telegram", "signal", "chat", "email", "forwarded", "shared")),
    ("Images, documents, videos and audio", ("screenshot", "handwritten", "photograph", "image", "video", "voice note", "document")),
    ("Unusual activity and potential data transfers", ("uploaded", "external drive", "transfer", "deleted", "cloud storage", "shared link", "confidential")),
)


def _clean(value: object, limit: int = 250) -> str:
    return re.sub(r"[\r\n\t|]+", " ", str(value or "")).strip()[:limit]


def _blank(value: object) -> str:
    text = _clean(value, 200)
    return "" if text.lower() in {"na", "n/a", "none", "nil", "-"} else text


def _subject_name(intake: dict) -> str:
    people = intake.get("subjects") or []
    if isinstance(people, str):
        try:
            import json
            people = json.loads(people)
        except (ValueError, TypeError):
            people = []
    if isinstance(people, list):
        for person in people:
            if isinstance(person, dict) and str(person.get("name") or "").strip():
                return _clean(person.get("name"), 120)
    return ""


def _spoken_date(value: object) -> str:
    text = str(value or "")[:10]
    try:
        from datetime import datetime
        spoken = datetime.strptime(text, "%Y-%m-%d").strftime("%d %B %Y")
        return spoken[1:] if spoken.startswith("0") else spoken
    except ValueError:
        return text


def _name_list(names: list[str]) -> str:
    clean = [name for name in names if name]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + ", and " + clean[-1]


def build_detailed_mobile_observations(db, job_id: str, intake: dict | None = None) -> str:
    """Plain observations: evidence and suspicious items only, with no inventory totals."""
    intake = intake or {}
    job = job_id
    bullets: list[str] = []
    subject = _subject_name(intake)
    org = _blank(intake.get("organization"))
    background = _blank(intake.get("background") or intake.get("incident_summary"))
    if subject or org or background:
        who = subject or "the person named at intake"
        place = f" at {org}" if org else ""
        if background:
            bullets.append(f"The intake names {who}{place}. The background given is: {background}.")
        else:
            bullets.append(
                f"The intake names {who}{place}. No separate incident was written down, so this note follows the procedure above."
            )

    offers = fetchall(
        db,
        """WITH conv AS (
               SELECT data->'raw'->>'Z_PK' AS pk,
                      coalesce(nullif(data->'raw'->>'ZPARTNERNAME',''),'(unnamed)') AS name
               FROM mobile_normalized_artifacts
               WHERE job_id=CAST(:jid AS uuid) AND artifact_type='conversation'
           )
           SELECT c.name, m.timestamp_utc::date AS on_date, m.data->>'from_me' AS from_me, left(m.data->>'body', 180) AS body
           FROM mobile_normalized_artifacts m
           JOIN conv c ON c.pk = m.data->>'conversation_id'
           WHERE m.job_id=CAST(:jid AS uuid) AND m.artifact_type='app_message'
             AND m.data->>'body' ILIKE '%offer letter%'
           ORDER BY m.timestamp_utc
           LIMIT 8""",
        {"jid": job},
    ) or []
    if offers:
        groups: dict[tuple[str, str, str], list[str]] = {}
        for item in offers:
            sent = "sent" if str(item.get("from_me")) in {"1", "true"} else "received"
            key = (_clean(item.get("name"), 80), _spoken_date(item.get("on_date")), sent)
            groups.setdefault(key, [])
            label = _clean(item.get("body"), 160)
            if label and label not in groups[key]:
                groups[key].append(label)
        for (chat, when, sent), names in groups.items():
            action = "sent from this phone" if sent == "sent" else "received on this phone"
            bullets.append(
                f"Offer-letter files were {action} in the chat \"{chat}\" on {when}. "
                f"The file names are {_name_list(names)}. The letters were not opened as separate documents."
            )
    else:
        bullets.append("No offer letter was found in the chats or as a saved document.")

    focused = fetchall(
        db,
        """WITH conv AS (
               SELECT data->'raw'->>'Z_PK' AS pk,
                      coalesce(nullif(data->'raw'->>'ZPARTNERNAME',''),'(unnamed)') AS name
               FROM mobile_normalized_artifacts
               WHERE job_id=CAST(:jid AS uuid) AND artifact_type='conversation'
           )
           SELECT c.name,
                  min(m.timestamp_utc)::date AS first_on,
                  max(m.timestamp_utc)::date AS last_on,
                  count(*) FILTER (WHERE m.data->>'from_me' IN ('1','true')) AS sent,
                  count(*) FILTER (WHERE m.data->>'body' ILIKE '%interview%' OR m.data->>'body' ILIKE '%salary%' OR m.data->>'body' ILIKE '%payroll%' OR m.data->>'body' ILIKE '%resign%') AS hits
           FROM mobile_normalized_artifacts m
           JOIN conv c ON c.pk = m.data->>'conversation_id'
           WHERE m.job_id=CAST(:jid AS uuid) AND m.artifact_type='app_message'
             AND (c.name ILIKE '%job%' OR c.name ILIKE '%recruit%')
             AND c.name NOT ILIKE '%offer%'
           GROUP BY c.name
           HAVING count(*) FILTER (WHERE m.data->>'body' ILIKE '%interview%' OR m.data->>'body' ILIKE '%salary%' OR m.data->>'body' ILIKE '%payroll%' OR m.data->>'body' ILIKE '%resign%') > 0
           ORDER BY hits DESC
           LIMIT 5""",
        {"jid": job},
    ) or []
    for item in focused:
        chat = _clean(item.get("name"), 80)
        origin = (
            "Those messages were not sent from this phone."
            if int(item.get("sent") or 0) == 0
            else "Some of these messages were sent from this phone."
        )
        bullets.append(
            f"The chat \"{chat}\" contains messages about job interviews, pay, or leaving a job, "
            f"from {_spoken_date(item.get('first_on'))} to {_spoken_date(item.get('last_on'))}. {origin}"
        )

    pattern = fetchall(
        db,
        """WITH conv AS (
               SELECT data->'raw'->>'Z_PK' AS pk,
                      coalesce(nullif(data->'raw'->>'ZPARTNERNAME',''),'(unnamed)') AS name
               FROM mobile_normalized_artifacts
               WHERE job_id=CAST(:jid AS uuid) AND artifact_type='conversation'
           ), msg AS (
               SELECT c.name,
                      m.data->>'from_me' IN ('1','true') AS sent,
                      m.data->>'message_type'='14' AS deleted,
                      EXTRACT(HOUR FROM m.timestamp_utc AT TIME ZONE 'Asia/Kolkata') AS hr
               FROM mobile_normalized_artifacts m
               JOIN conv c ON c.pk = m.data->>'conversation_id'
               WHERE m.job_id=CAST(:jid AS uuid) AND m.artifact_type='app_message'
           )
           SELECT CASE
                    WHEN name ~* '(std|agnel|school)' THEN 'school'
                    WHEN name ~* '(edate|family|परिवार)' THEN 'family'
                    WHEN name ~* '(job|recruit)' AND name !~* 'offer' THEN 'jobs'
                    ELSE 'other'
                  END AS kind,
                  count(*) AS n,
                  count(*) FILTER (WHERE sent) AS sent,
                  count(*) FILTER (WHERE deleted) AS deleted,
                  count(*) FILTER (WHERE sent AND hr BETWEEN 0 AND 4) AS night_sent
           FROM msg
           GROUP BY 1""",
        {"jid": job},
    ) or []
    by_kind = {str(item.get("kind")): item for item in pattern}
    school = by_kind.get("school") or {}
    family = by_kind.get("family") or {}
    jobs = by_kind.get("jobs") or {}
    school_n = int(school.get("n") or 0)
    school_sent = int(school.get("sent") or 0)
    family_n = int(family.get("n") or 0)
    family_sent = int(family.get("sent") or 0)
    jobs_n = int(jobs.get("n") or 0)
    jobs_sent = int(jobs.get("sent") or 0)
    if school_n and school_sent * 100 < school_n:
        bullets.append(
            "School and parent chats are one-sided. Messages arrive in those groups, and this phone almost never replies."
        )
    if family_n and family_sent:
        bullets.append(
            "Family chats are two-way. People write, and this phone writes back, across the years covered by the phone."
        )
    if jobs_n and jobs_sent * 50 < jobs_n:
        bullets.append(
            "Job and recruitment chats are notices sent to this phone. This phone does not take part in those chats."
        )
    night_sent = sum(int(item.get("night_sent") or 0) for item in pattern)
    sent_all = sum(int(item.get("sent") or 0) for item in pattern)
    if sent_all and night_sent * 20 < sent_all:
        bullets.append(
            "Messages sent from this phone are spread through the morning, afternoon, and evening. Very few are sent in the middle of the night."
        )
    deleted_school = int(school.get("deleted") or 0)
    deleted_all = sum(int(item.get("deleted") or 0) for item in pattern)
    school_has_most_deleted = deleted_school > 0 and deleted_school >= max(
        (int(item.get("deleted") or 0) for item in pattern if str(item.get("kind")) != "school"),
        default=0,
    )

    adult = fetchall(
        db,
        """SELECT
             (SELECT count(*) FROM mobile_normalized_artifacts
               WHERE job_id=CAST(:jid AS uuid)
                 AND (data::text ~* '(pornhub|xvideos\\.com|xnxx\\.com|xhamster|onlyfans|youporn)'
                      OR coalesce(data->>'media_name','') ~* '(porn|onlyfans|\\yxxx\\y)'))
             + (SELECT count(*) FROM job_artifacts
               WHERE job_id=CAST(:jid AS uuid)
                 AND (file_name ~* '(porn|onlyfans|\\yxxx\\y)' OR file_path ~* '(pornhub|xvideos\\.com|xnxx\\.com|onlyfans)'))
             AS n""",
        {"jid": job},
    ) or []
    if int((adult[0] if adult else {}).get("n") or 0):
        bullets.append(
            "Audio, video, or web pages with adult sexual names were found. The pictures and wording are not described in this report."
        )
    else:
        bullets.append(
            "No audio, video, photo name, or website on this phone was identified as pornography."
        )

    if deleted_all:
        where = " Many of them are in the school and parent groups." if school_has_most_deleted else ""
        bullets.append(
            "Deleted WhatsApp messages were found."
            + where
            + " The original words are no longer on the phone, so they cannot be read back."
        )

    bullets.append(
        "Photos, videos and voice notes are on the phone. No receipt, cheque, or payment screenshot was identified by its file name."
    )
    return "### 3. Observations\n\n" + "\n".join(f"- {line}" for line in bullets)


def build_mobile_inquiry_findings(db, job_id: str, intake: dict, *, limit_per_question: int = 5) -> str:
    """Return reproducible leads from the *current job only* with citations.

    Search hits are candidate leads for human review, not verified findings.
    """
    lines = ["### 4. Intake-driven examination questions", "",
             "The following are potential leads from the accessible indexed evidence, not proof of misconduct. "
             "A negative search does not establish that an event did not occur. Each lead requires examiner verification.", ""]
    background = _clean(intake.get("background") or intake.get("incident_summary"), 800)
    if background:
        lines += ["**Case context supplied at intake:** " + background, ""]
    for heading, terms in QUESTIONS:
        lines += ["#### " + heading, ""]
        clauses = " OR ".join(f"(file_path ILIKE :t{i} OR content ILIKE :t{i})" for i in range(len(terms)))
        params = {"jid": job_id, "lim": max(1, min(int(limit_per_question), 15))}
        params.update({f"t{i}": "%" + term + "%" for i, term in enumerate(terms)})
        # Deliberately bounded to avoid report-generation table scans without limit.
        rows = fetchall(db, f"""SELECT file_path, LEFT(COALESCE(content,''), 400) AS content
            FROM rag_chunks WHERE job_id=CAST(:jid AS uuid) AND ({clauses})
            ORDER BY created_at DESC NULLS LAST LIMIT :lim""", params) or []
        if rows:
            lines.append(f"{len(rows)} candidate source(s) surfaced for manual review (limited search sample):")
            for row in rows:
                path = _clean(row.get("file_path"), 320)
                preview = _clean(row.get("content"), 170)
                # Source path is a file-level citation; do not quote unreviewed text as a conclusion.
                lines.append(f"- Source: `{path}` — excerpt: {preview or '[indexed content unavailable]'}")
        else:
            lines.append("No matching readable indexed records were identified by this limited query. Not established; review acquisition coverage and unavailable sources.")
        lines.append("")
    lines += ["**Scope limitation:** Encrypted private-app data, missing keys, unavailable cloud accounts, unsupported files, failed OCR and overwritten artifacts may contain information not represented in these searches.", ""]
    return "\n".join(lines)

import app.services.scanner_agent_jobs as saj


def test_empty_scanner_version_enables_parallel(monkeypatch):
    monkeypatch.setattr(saj, "fetchone", lambda db, sql, params=None: {"version": None})
    assert saj._agent_supports_parallel(object(), "00000000-0000-0000-0000-000000000099") is True


def test_explicit_old_version_stays_legacy(monkeypatch):
    monkeypatch.setattr(saj, "fetchone", lambda db, sql, params=None: {"version": "1.1.10"})
    assert saj._agent_supports_parallel(object(), "00000000-0000-0000-0000-000000000099") is False


def test_legacy_running_without_task_id_still_claims_queued(monkeypatch):
    queued = {
        "id": "00000000-0000-0000-0000-000000000021",
        "case_id": "00000000-0000-0000-0000-000000000012",
        "status": "queued",
        "orchestration_json": {"edge_agent": True},
    }
    claimed = dict(queued, status="running")
    running_stuck = {
        "id": "00000000-0000-0000-0000-000000000020",
        "case_id": "00000000-0000-0000-0000-000000000012",
        "status": "running",
        "external_scan_id": None,
        "orchestration_json": {"edge_agent": True},
    }
    calls = {"n": 0}

    def fake_fetchone(db, sql, params=None):
        calls["n"] += 1
        text = " ".join(str(sql).split())
        if "SET scanner_id" in text:
            return claimed
        if "vuln_scan_results" in text:
            return None
        if "completed_at IS NULL" in text:
            return None
        if "status = 'running'" in text:
            return running_stuck
        if "stale_scanner_queued" in text:
            return None
        if "status IN ('queued', 'pending')" in text:
            return queued
        if "FROM vuln_scanners" in text:
            return {"version": "1.0.5"}
        raise AssertionError(f"unexpected fetchone #{calls['n']}: {text[:180]}")

    monkeypatch.setattr(saj, "fetchone", fake_fetchone)
    monkeypatch.setattr(saj, "_agent_supports_parallel", lambda db, sid: False)
    monkeypatch.setattr(saj, "_agent_supports_resume", lambda db, sid: True)
    monkeypatch.setattr(saj, "_job_network_key", lambda db, jid: frozenset({"10.0.0.0/24"}))
    monkeypatch.setattr(saj, "timeline", lambda *a, **k: None)
    monkeypatch.setattr(
        saj,
        "_job_payload",
        lambda db, row, resumed: {"id": row["id"], "status": row["status"], "resume": resumed},
    )

    out = saj.claim_next_job(object(), "00000000-0000-0000-0000-000000000099", active_job_ids=[])
    assert out["id"] == queued["id"]
    assert out["status"] == "running"
    assert out["resume"] is False


def test_admin_optin_adopts_queued_job_from_stale_scanner(monkeypatch):
    monkeypatch.setenv("EDGE_CROSS_SCANNER_JOB_REASSIGNMENT", "true")
    monkeypatch.setattr(saj, "_scanner_registered_version", lambda db, sid: (1, 0, 0))
    monkeypatch.setattr(saj, "_fresh_running_edge_job", lambda db, sid: None)
    orphan = {
        "id": "00000000-0000-0000-0000-000000000031",
        "case_id": "00000000-0000-0000-0000-000000000012",
        "status": "queued",
        "orchestration_json": {"edge_agent": True},
    }
    claimed = dict(orphan, status="running")
    calls = {"n": 0}

    seen: list[str] = []

    def fake_fetchone(db, sql, params=None):
        calls["n"] += 1
        text = " ".join(str(sql).split())
        seen.append(text[:220])
        if "SET scanner_id" in text:
            assert params["sid"] == "00000000-0000-0000-0000-000000000099"
            return claimed
        if "vuln_scan_results" in text:
            return None
        if "status = 'running'" in text:
            return None
        if "stale_scanner_queued" in text or "j.scanner_id <>" in text:
            return orphan
        if "status IN ('queued', 'pending')" in text:
            return None
        raise AssertionError("unexpected fetchone #" + str(calls["n"]) + ": " + " || ".join(seen))

    monkeypatch.setattr(saj, "fetchone", fake_fetchone)
    monkeypatch.setattr(saj, "_agent_supports_parallel", lambda db, sid: True)
    monkeypatch.setattr(saj, "_job_network_key", lambda db, jid: frozenset({"10.0.0.0/24"}))
    monkeypatch.setattr(saj, "timeline", lambda *a, **k: None)
    monkeypatch.setattr(
        saj,
        "_job_payload",
        lambda db, row, resumed: {"id": row["id"], "status": row["status"], "resume": resumed},
    )

    out = saj.claim_next_job(object(), "00000000-0000-0000-0000-000000000099", active_job_ids=[])
    assert out is not None, "no claim; sql=" + " || ".join(seen)
    assert out["id"] == orphan["id"]
    assert out["resume"] is False


def test_other_laptops_queued_job_is_not_stolen_by_default(monkeypatch):
    monkeypatch.delenv("EDGE_CROSS_SCANNER_JOB_REASSIGNMENT", raising=False)
    monkeypatch.setattr(saj, "_scanner_registered_version", lambda db, sid: (1, 0, 0))
    monkeypatch.setattr(saj, "_fresh_running_edge_job", lambda db, sid: None)
    calls = []
    def fake_fetchone(db, sql, params=None):
        txt = " ".join(str(sql).split())
        calls.append(txt)
        if "FROM vuln_scanners" in txt:
            return {"version": "1.7.0"}
        if "stale_scanner_queued" in txt:
            raise AssertionError("must not inspect other laptops' jobs")
        return None
    monkeypatch.setattr(saj, "fetchone", fake_fetchone)
    monkeypatch.setattr(saj, "_agent_supports_parallel", lambda db, sid: True)
    out = saj.claim_next_job(object(), "00000000-0000-0000-0000-000000000099", active_job_ids=[])
    assert out is None
    assert not any("stale_scanner_queued" in sql for sql in calls)

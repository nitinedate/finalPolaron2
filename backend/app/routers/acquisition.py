"""Mobile acquisition API — Device Wizard backend (Architecture §3.1, §7).

Endpoint flow mirrors the examiner's screen sequence:

    GET  /api/acquisition/adapters          which access technologies are usable here
    GET  /api/acquisition/devices           §7.1  device detector
    POST /api/acquisition/preview           §7.2-4 profile, capability, prep steps
    POST /api/acquisition/start             §7.5-10 full collection run
    POST /api/acquisition/verify            §12   re-verify a stored extraction
    GET  /api/acquisition/methods           §6    method reference for the UI

    V1.27.0 — phone attached to a client PC (image created there in C:\\evidence):
    GET  /api/acquisition/client-import/capabilities   server helper identity / support
    POST /api/acquisition/client-import                 copy the run from the client share
    GET  /api/acquisition/client-import/{id}            copy progress + server-path result
    POST /api/acquisition/client-import/{id}/cancel

    V1.31.14 — the collection on a client PC is sent to the server while it runs:
    POST /api/acquisition/client-stream                 upload session for the client helper
    PUT  /api/acquisition/client-stream/{id}/chunk      (client helper, session token) file bytes
    POST /api/acquisition/client-stream/{id}/commit     SHA-256 verified on the server disk
    POST /api/acquisition/client-stream/{id}/files      many small files in one request
    POST /api/acquisition/client-stream/{id}/discard    resend a file that changed on the client
    GET  /api/acquisition/client-stream/{id}/status     what the server already holds (resume)
    POST /api/acquisition/client-stream/{id}/finish     all files present -> registered run
    POST /api/acquisition/client-stream/{id}/abort      collection failed on the client
    (progress and result: GET /api/acquisition/client-import/{id})

`preview` is deliberately separate from `start`: §7 requires the examiner to see
the resolved capability, the cable/preparation instructions and the coverage
limitations of each method BEFORE a collection begins. Collapsing the two would
mean the examiner authorises a method without seeing what it does not cover.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import CurrentUser, firm_db, require_firm_permission
from app.services.mobile_acquire import (
    AcquisitionRequest,
    CollectionMethod,
    CollectionOrchestrator,
    DeviceDetector,
    IntegrityManifest,
    METHOD_PROFILES,
    coverage_statement,
    verify_against_manifest,
)
from app.services.mobile_acquire.entitlements import load_profile
from app.services.mobile_acquire.orchestrator import TOOL_VERSION
from app.services.mobile_acquire import host_bridge
from app.services.mobile_acquire.run_registry import TERMINAL_STATES, get_registry
from app.service_identity import mobile_service_platform
from app.services.mobile_platform_agents import detect_mobile_platform
from app.services.mobile_acquire.validation import (
    ACCEPTANCE_AREAS,
    CRITICAL_DEVICE_FAMILIES,
    ToolVersionLedger,
)

router = APIRouter(prefix="/api/acquisition", tags=["acquisition"])


def _required_mobile_platform() -> str | None:
    return mobile_service_platform()


def _platform_allowed(*hints: Any) -> bool:
    required = _required_mobile_platform()
    if not required:
        return True
    detected = detect_mobile_platform(*hints)
    return detected == required


def _assert_platform(*hints: Any) -> None:
    required = _required_mobile_platform()
    if not required:
        return
    detected = detect_mobile_platform(*hints)
    if detected != required:
        got = detected or "unknown"
        raise HTTPException(
            status_code=403,
            detail=(
                f"This is the {required.title()} mobile backend. "
                f"The selected device/adapter is {got}; use the matching mobile backend."
            ),
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PreviewRequest(BaseModel):
    adapter: str
    device_id: str


class StartRequest(BaseModel):
    adapter: str
    device_id: str
    case_id: str
    evidence_id: str
    legal_authority: str
    case_root: str

    objective_methods: list[str] = Field(default_factory=list)
    authorized_methods: list[str] = Field(default_factory=list)
    method_override: str | None = None

    cable_adapter_asset_id: str = ""
    license_endpoint_id: str = ""
    backup_password: str | None = None
    examiner_notes: str = ""
    device_condition: str = ""
    network_isolated: bool | None = None
    create_working_copy: bool = True
    cloud_mailboxes: list[dict[str, Any]] = Field(default_factory=list)
    # Explicit on-device authorizations (Android only). The WhatsApp decrypt
    # agent's backup creation ("whatsapp_generate_key_and_backup") WRITES to the
    # evidence device and only runs when present here.
    authorized_actions: list[str] = Field(default_factory=list)
    whatsapp_64_digit_key: str = ""


class ValidationRecordRequest(BaseModel):
    device_family: str
    passed: bool
    approver: str = ""
    notes: str = ""
    tool_version: str | None = None


class VerifyRequest(BaseModel):
    extraction_path: str
    manifest_path: str


class ClientImportRequest(BaseModel):
    """Read-only share of the client case folder, as returned by the Client Evidence Agent."""

    share_id: str = Field(min_length=10, max_length=80, pattern=r"^ce[0-9a-f]{16,64}$")
    token: str = Field(min_length=24, max_length=256)
    port: int = Field(default=9877, ge=1, le=65535)
    addresses: list[str] = Field(default_factory=list, max_length=16)
    hostname: str = Field(default="", max_length=253)
    client_root: str = Field(default="", max_length=1024)
    file_count: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    manifest_sha256: str = Field(default="", max_length=64)
    agent_url: str | None = Field(default=None, max_length=200)
    relay: bool = False
    # V1.31.2: the folder was chosen in the browser and this tab serves it (outbound connection only).
    browser: bool = False
    skipped: int = Field(default=0, ge=0)
    agent_version: str = Field(default="", max_length=40)
    # Which run to copy. Empty run_name = newest sealed run in the folder.
    case_id: str = Field(default="", max_length=128)
    run_name: str = Field(default="", max_length=255)
    # full = whole run (original, working copy, exports, logs, hashes) like a server-side
    # collection; packages = 05_Exports + logs/hashes only (much less to send off-site).
    scope: str = Field(default="full", pattern=r"^(full|packages)$")


class RemoveRunRequest(BaseModel):
    run_name: str = ""
    case_id: str = ""
    progress_file: str = ""
    paths: dict[str, Any] = Field(default_factory=dict)


def _methods(values: list[str]) -> list[CollectionMethod]:
    out: list[CollectionMethod] = []
    for value in values or []:
        try:
            out.append(CollectionMethod(value.strip().lower()))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown collection method '{value}'. Valid values: "
                       + ", ".join(m.value for m in CollectionMethod),
            )
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/adapters")
def list_adapters(
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """Which device-access technologies this workstation can actually use.

    Reported honestly: an adapter whose external tooling is missing is listed as
    unavailable with the reason, so a device that cannot be seen is never
    mistaken for a device that is not connected.

    When the API runs in Docker (no USB), availability is merged from the
    examiner host drive helper on port 9876.
    """
    from app.services.mobile_acquire import host_bridge

    detector = DeviceDetector()
    adapters = []
    for adapter in detector.adapters:
        try:
            available = adapter.tool_available()
            reason = "" if available else (
                "Required external tooling is not installed in this process "
                "(typical for Docker API). Host helper will be used when available."
            )
        except Exception as exc:
            available, reason = False, str(exc)
        adapters.append({
            "name": adapter.name,
            "os_family": adapter.os_family,
            "available": available,
            "reason": reason,
        })
    helper_ok = host_bridge.helper_reachable()
    host = host_bridge.fetch_host_adapters() if helper_ok else []
    merged = host_bridge.merge_adapters(adapters, host)
    required = _required_mobile_platform()
    if required:
        merged = [a for a in merged if _platform_allowed(a)]
    return {
        "adapters": merged,
        "host_helper": {
            "url": host_bridge.helper_base_url(),
            "reachable": helper_ok,
            "hint": (
                None if helper_ok else
                "Host drive helper on port 9876 is offline or not answering "
                "(often blocked by an old single-threaded acquire). On the examiner PC run: "
                "powershell -ExecutionPolicy Bypass -File scripts\\ensure-host-drive-helper.ps1"
            ),
        },
    }


@router.get("/methods")
def list_methods(
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """§6 method reference, including the mandatory coverage caveat per method."""
    return {
        "methods": [
            {
                "method": method.value,
                **coverage_statement(method),
                "intrusiveness_rank": rank,
            }
            for rank, method in enumerate(
                sorted(METHOD_PROFILES, key=lambda m: m.value), start=1)
        ],
        "selection_principle": (
            "Select the least intrusive method that is sufficient for the authorised "
            "objective, while preserving the option to perform a deeper method when "
            "legally authorised and technically necessary. Never assume that a "
            "particular device supports physical or full-file-system acquisition."
        ),
    }


@router.get("/devices")
def detect_devices(
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """§7 step 1 — device detector (local tools, else examiner host USB bridge)."""
    from app.services.mobile_acquire import host_bridge

    detector = DeviceDetector()
    devices, warnings = detector.scan()
    # Docker has no USB. Adapter "tooling missing" is expected and must not
    # drown the examiner UI — phones are detected on the Windows host helper.
    warnings = [
        w for w in warnings
        if "external tooling is not installed" not in w
        and "Devices of this type will not be detected" not in w
    ]
    out = [d.as_dict() for d in devices]
    helper_ok = host_bridge.helper_reachable()
    host_adapters: list[dict[str, Any]] = []
    local_adapters: list[dict[str, Any]] = []
    for adapter in detector.adapters:
        try:
            available = adapter.tool_available()
        except Exception:
            available = False
        local_adapters.append({
            "name": adapter.name,
            "os_family": adapter.os_family,
            "available": available,
            "reason": "",
        })

    # Docker API rarely sees USB — merge host helper detection.
    if helper_ok:
        host_adapters = host_bridge.fetch_host_adapters()
        host_ready = {a["name"] for a in host_adapters if a.get("available")}
        # Drop container-local "tooling missing" noise when the examiner host has the adapter.
        warnings = [
            w for w in warnings
            if not any(
                f"Adapter '{name}' is unavailable" in w for name in host_ready
            )
        ]
        host_devices, host_warnings = host_bridge.fetch_host_devices()
        warnings = list(warnings) + list(host_warnings)
        seen = {(d.get("adapter"), d.get("device_id")) for d in out}
        for hd in host_devices:
            key = (hd.get("adapter"), hd.get("device_id"))
            if key not in seen and hd.get("device_id"):
                out.append(hd)
                seen.add(key)
    elif not out:
        warnings = list(warnings) + [
            "No USB tools in the API container and host drive helper is offline. "
            "On the examiner PC run: scripts\\ensure-host-drive-helper.ps1 — then click Scan again."
        ]

    required = _required_mobile_platform()
    merged_adapters = host_bridge.merge_adapters(local_adapters, host_adapters)
    if required:
        out = [d for d in out if _platform_allowed(d)]
        merged_adapters = [a for a in merged_adapters if _platform_allowed(a)]
    return {
        "devices": out,
        "warnings": warnings,
        "device_count": len(out),
        "host_helper_reachable": helper_ok,
        "mobile_platform": required,
        "adapters": merged_adapters,
    }


@router.post("/preview")
def preview_device(
    payload: PreviewRequest,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """§7 steps 2-4 — identify, resolve capability, show preparation instructions."""
    from app.services.mobile_acquire import host_bridge

    _assert_platform({"adapter": payload.adapter, "device_id": payload.device_id})
    detector = DeviceDetector()
    adapter = detector.adapter_by_name(payload.adapter)
    if adapter is not None:
        _assert_platform({"adapter": payload.adapter, "os_family": getattr(adapter, "os_family", "")})
    if adapter is not None and adapter.tool_available():
        orchestrator = CollectionOrchestrator(detector)
        preview = orchestrator.preview(payload.adapter, payload.device_id)
        if not preview.get("ok"):
            raise HTTPException(status_code=400, detail=preview.get("error", "Preview failed."))
        return preview

    if host_bridge.helper_reachable():
        host_preview = host_bridge.host_preview(payload.adapter, payload.device_id)
        if host_preview and host_preview.get("ok"):
            return host_preview
        detail = (host_preview or {}).get("error") or "Host preview failed."
        raise HTTPException(status_code=400, detail=detail)

    raise HTTPException(
        status_code=503,
        detail=(
            "USB acquisition tools are not available inside Docker. Start the host drive "
            "helper (port 9876) on the examiner PC, plug in the phone, then retry."
        ),
    )


def _intake_whatsapp_keys(db, case_id: str) -> list[str]:
    """Return any WhatsApp key hex already captured in Intake for this case.

    Lets the decrypt agent try the existing key without re-reading the device.
    Never returns raw key bytes to the client; this is server-internal only.
    """
    try:
        from app.db.sql_helpers import fetchone

        row = fetchone(
            db,
            """SELECT disk_source FROM jobs
               WHERE (disk_source->>'case_id' = :cid OR id::text = :cid)
               ORDER BY created_at DESC LIMIT 1""",
            {"cid": str(case_id)},
        )
        ds = (row or {}).get("disk_source") or {}
        if isinstance(ds, str):
            import json as _json
            ds = _json.loads(ds or "{}")
        key = str((ds or {}).get("whatsapp_key_hex") or "").strip()
        return [key] if key else []
    except Exception:
        return []


@router.post("/start")
def start_acquisition(
    payload: StartRequest,
    db: Session = Depends(firm_db),
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
) -> dict[str, Any]:
    """Architecture section 7 steps 5-10 — queue the full collection run.

    Returns immediately with a run id. A full-filesystem collection can run for
    hours; holding the HTTP request open for that long means a dropped
    connection orphans the collection and the examiner never sees the outcome
    even though the evidence was sealed correctly. Progress is streamed from
    /runs/{run_id}/stream and the result is durable in the case folder.
    """
    _assert_platform({"adapter": payload.adapter, "device_id": payload.device_id})
    if not payload.legal_authority.strip():
        raise HTTPException(
            status_code=400,
            detail="Legal authority is required before any collection may begin (section 14).",
        )

    override = _methods([payload.method_override])[0] if payload.method_override else None

    request = AcquisitionRequest(
        case_id=payload.case_id,
        evidence_id=payload.evidence_id,
        examiner=getattr(user, "email", None) or getattr(user, "id", "unknown"),
        legal_authority=payload.legal_authority,
        case_root=payload.case_root,
        adapter_name=payload.adapter,
        device_id=payload.device_id,
        objective_methods=_methods(payload.objective_methods),
        authorized_methods=_methods(payload.authorized_methods),
        examiner_method_override=override,
        cable_adapter_asset_id=payload.cable_adapter_asset_id,
        license_endpoint_id=payload.license_endpoint_id,
        backup_password=payload.backup_password,
        examiner_notes=payload.examiner_notes,
        device_condition=payload.device_condition,
        network_isolated=payload.network_isolated,
        create_working_copy=payload.create_working_copy,
        cloud_mailboxes=list(payload.cloud_mailboxes or []),
        authorized_actions=list(payload.authorized_actions or []),
        whatsapp_64_digit_key=(payload.whatsapp_64_digit_key or ""),
        intake_key_candidates=_intake_whatsapp_keys(db, payload.case_id),
    )

    try:
        run = get_registry().start(request)
    except RuntimeError as exc:
        # Device already being acquired — a conflict, not a server fault.
        raise HTTPException(status_code=409, detail=str(exc))
    return run.summary()


def _merge_host_helper_runs(
    runs: list[dict[str, Any]],
    *,
    active_only: bool,
) -> list[dict[str, Any]]:
    """Surface USB collections that live in the host helper, not this API process.

    Browser refresh used to ask this endpoint for active runs, get an empty
    list (host jobs are not in the in-memory registry), then fall through to
    the last finished Docker run and freeze the wizard on Complete.
    """
    seen: set[str] = set()
    for run in runs:
        rid = str(run.get("run_id") or "")
        hid = str(run.get("host_job_id") or "")
        if rid:
            seen.add(rid)
        if hid:
            seen.add(hid)
    helper_runs: list[dict[str, Any]] = []
    try:
        for job in host_bridge.fetch_helper_jobs():
            if not _platform_allowed(job):
                continue
            jid = str(job.get("job_id") or "")
            if not jid or jid in seen:
                continue
            status = str(job.get("status") or "").lower()
            if active_only and status not in ("", "running", "queued"):
                continue
            helper_runs.append(host_bridge.helper_job_as_run(job))
            seen.add(jid)
    except Exception:
        pass
    return helper_runs + runs


@router.get("/runs")
def list_runs(
    active_only: bool = Query(False),
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """Every acquisition this workstation has started, including host-helper USB jobs."""
    registry = get_registry()
    runs = _merge_host_helper_runs(
        registry.list(include_finished=not active_only),
        active_only=active_only,
    )
    active_count = sum(1 for r in runs if r.get("status") not in TERMINAL_STATES)
    return {
        "runs": runs,
        "active_count": active_count,
    }


@router.get("/runs/recover")
def recover_runs(
    case_root: str = Query(...),
    case_id: str = Query(...),
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """Rebuild run history from the case folder after an API restart."""
    return {"runs": get_registry().recover(case_root, case_id)}


@router.get("/runs/recover-recent")
def recover_recent_runs(
    case_root: str = Query("/evidence/cases"),
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """Rehydrate the newest sealed collection when the in-memory run was lost."""
    return {"runs": get_registry().recover_recent(case_root)}


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    run = get_registry().get(run_id)
    if run is not None:
        return run.detail()
    if host_bridge.is_host_job_id(run_id):
        job = host_bridge.fetch_helper_job(run_id)
        if job is not None:
            return host_bridge.helper_job_as_run(job, detail=True)
    raise HTTPException(status_code=404, detail="Acquisition run not found.")


@router.get("/runs/{run_id}/whatsapp-keys")
def acquired_android_whatsapp_keys(
    run_id: str,
    response: Response,
    reveal: bool = Query(False),
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
) -> dict[str, Any]:
    """Case-bound, opt-in access to matching keys from an Android acquisition.

    Key bytes are deliberately not embedded in ordinary run/result JSON. The
    caller must have write-level forensic permission and own the acquisition
    (or be a tenant administrator). This reads only already-acquired files.
    """
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    from pathlib import Path
    from app.services.mobile_forensic.whatsapp_crypt import parse_key_material

    run = get_registry().get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Acquisition run unavailable on this worker")
    if run.status not in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="Collection has not finished")
    if "android" not in str(run.adapter).lower():
        raise HTTPException(status_code=400, detail="Android acquisitions only")
    if str(run.examiner).casefold() != str(user.email).casefold() and not user.is_tenant_admin():
        raise HTTPException(status_code=403, detail="Only the acquiring examiner or tenant admin can access keys")
    paths = (run.result or {}).get("paths") or {}
    base = paths.get("original")
    if not isinstance(base, str) or not base:
        return {"items": [], "status": "not_available"}
    root = Path(base).resolve(strict=False)
    if not root.is_dir() or root.is_symlink():
        return {"items": [], "status": "not_available"}
    # Only read a bounded number of scoped key files; never accept a path from
    # the browser. Exclude symlinks and ensure matching decrypted DB exists.
    items = []
    import os
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if not (Path(folder) / d).is_symlink()]
        if 'msgstore.db.crypt15' not in files:
            continue
        d = Path(folder)
        candidate = d / 'encrypted_backup.key'
        entry = {"directory": str(d.relative_to(root)),
                 "decrypted": (d / 'msgstore.db').is_file(),
                 "key_available": False}
        try:
            if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 512:
                parsed = parse_key_material(candidate.read_bytes())
                if parsed and len(parsed.raw32) == 32:
                    entry['key_available'] = True
                    entry['key_file'] = 'encrypted_backup.key'
                    if reveal:
                        entry['key_hex'] = parsed.raw32.hex()
        except OSError:
            pass
        items.append(entry)
        if len(items) >= 128:
            break
    # No key secrets in response headers/logs/audit; browser uses no-cache.
    return {"items": items, "status": "complete"}


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
) -> dict[str, Any]:
    """Stop the collection immediately and keep whatever is already on disk."""
    registry = get_registry()
    run = registry.get(run_id)
    if run is None and host_bridge.is_host_job_id(run_id):
        res = host_bridge.host_cancel_job(run_id)
        accepted = bool(res and res.get("ok"))
        return {
            "accepted": accepted,
            "status": "cancelling" if accepted else "unknown",
            "message": (
                "Collection is stopping now. Files already written stay in the case folder "
                "as an interrupted extraction."
                if accepted else
                str((res or {}).get("error") or "Host collection could not be stopped.")
            ),
        }
    if run is None:
        raise HTTPException(status_code=404, detail="Acquisition run not found.")
    accepted = registry.cancel(run_id)
    return {
        "accepted": accepted,
        "status": "cancelling" if accepted else run.status,
        "message": (
            "Collection is stopping now. Files already written stay in the case folder "
            "as an interrupted extraction."
            if accepted else
            f"Run is already {run.status}; nothing to cancel."
        ),
    }


@router.post("/runs/{run_id}/remove")
def remove_run(
    run_id: str,
    body: RemoveRunRequest | None = None,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
) -> dict[str, Any]:
    """Stop the collection and delete this run's extracted files from the case."""
    import shutil
    from pathlib import Path

    payload = body or RemoveRunRequest()
    registry = get_registry()
    run = registry.get(run_id)
    paths: dict[str, Any] = dict(payload.paths or {})
    run_name = (payload.run_name or "").strip()
    case_id = (payload.case_id or "").strip()
    host_id = run_id
    if run is not None:
        run.request_cancel()
        run_name = run.run_name or run_name
        case_id = run.case_id or case_id
        host_id = run.host_job_id or run_id
        if isinstance(run.result, dict):
            raw = run.result.get("paths")
            if isinstance(raw, dict):
                for key, value in raw.items():
                    paths.setdefault(str(key), value)
        if run.progress:
            if run.progress.output_path:
                paths.setdefault("original", run.progress.output_path)
            if run.progress.case_path:
                paths.setdefault("case_root", run.progress.case_path)
        run.status = "cancelled"
        run.error = "removed_by_examiner"
        run.result = {
            "ok": False,
            "run_name": run_name,
            "stage_reached": "removed",
            "errors": ["Collection stopped and this run's files were removed"],
            "paths": {},
        }
    elif not host_bridge.is_host_job_id(run_id):
        raise HTTPException(status_code=404, detail="Acquisition run not found.")
    res = None
    if host_bridge.is_host_job_id(host_id) or host_bridge.is_host_job_id(run_id):
        res = host_bridge.host_remove_job(
            host_id if host_bridge.is_host_job_id(host_id) else run_id,
            run_name=run_name,
            case_id=case_id,
            progress_file=payload.progress_file,
            paths=paths or None,
        )
    protected_prefixes = ("CASE-", "02_", "03_", "05_", "07_", "08_")
    for key in ("original", "working", "logs", "hashes", "exports"):
        p = paths.get(key)
        if not p:
            continue
        try:
            target = Path(str(p))
            if not target.is_dir():
                continue
            if target.name.startswith(protected_prefixes):
                continue
            if run_name and run_name not in str(target):
                continue
            shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass
    accepted = bool((res and res.get("ok")) or run is not None)
    return {
        "accepted": accepted,
        "status": "removed",
        "removed": (res or {}).get("removed") or [],
        "message": (
            (res or {}).get("message")
            or "Collection stopped and this run's files were removed."
        ),
    }


# ---------------------------------------------------------------------------
# V1.27.0 — image created on a client PC, copied to the forensic server
# ---------------------------------------------------------------------------

def _client_import_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if "could not reach" in message or "Timed out" in message:
        status, code = 502, "client_agent_unreachable"
    elif "HostDrive helper" in message:
        status, code = 503, "server_helper_unavailable"
    elif "/dev/fuse" in message or "libfuse" in message or "disabled" in message:
        status, code = 503, "client_import_unavailable"
    else:
        status, code = 400, "client_import_failed"
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


@router.get("/client-import/capabilities")
def client_import_capabilities(
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """Whether this server can store client-created images, and which computer its helper runs on.

    The browser compares ``office_helper.computer_name`` with its own local helper's
    name: equal means the phone is on the forensic server itself (normal flow).
    """
    from app.services.client_evidence import enabled as client_evidence_enabled
    from app.services.mobile_acquire import client_import

    caps = client_import.helper_capabilities()
    return {
        "enabled": client_import.enabled() and client_evidence_enabled(),
        "office_helper": caps,
        "default_client_case_root": os.environ.get("CLIENT_MOBILE_CASE_ROOT", "C:\\evidence") or "C:\\evidence",
        "server_case_root": client_import.server_case_root(),
        "scopes": ["full", "packages"],
    }


@router.post("/client-import")
def start_client_import(
    payload: ClientImportRequest,
    request: Request,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
) -> dict[str, Any]:
    """Copy a run collected on the examiner's PC into the server case folder ("as usual")."""
    from app.routers.client_evidence import _request_ip
    from app.services.client_evidence import ClientEvidenceError
    from app.services.mobile_acquire import client_import

    try:
        return client_import.start_import(
            share_id=payload.share_id,
            token=payload.token,
            port=payload.port,
            addresses=payload.addresses,
            hostname=payload.hostname,
            client_root=payload.client_root,
            file_count=payload.file_count,
            total_bytes=payload.total_bytes,
            manifest_sha256=payload.manifest_sha256,
            agent_url=payload.agent_url,
            relay_supported=payload.relay,
            browser_share=payload.browser,
            skipped=payload.skipped,
            agent_version=payload.agent_version,
            case_id=payload.case_id,
            run_name=payload.run_name,
            scope=payload.scope,
            request_ip=_request_ip(request),
            user_email=str(getattr(user, "email", "") or ""),
            platform=_required_mobile_platform(),
        )
    except ClientEvidenceError as exc:
        raise _client_import_error(exc)


@router.get("/client-import/{import_id}")
def get_client_import(
    import_id: str,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    from app.services.mobile_acquire import client_import

    state = client_import.load_state(import_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Copy not found (it may have expired).")
    return client_import.public_state(state)


@router.post("/client-import/{import_id}/cancel")
def cancel_client_import(
    import_id: str,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
) -> dict[str, Any]:
    from app.services.mobile_acquire import client_import

    accepted = client_import.request_cancel(import_id)
    return {
        "accepted": accepted,
        "message": (
            "Stopping the copy. Files already verified on the server are kept."
            if accepted else "This copy is not running."
        ),
    }


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> StreamingResponse:
    """Server-sent progress for a running acquisition.

    Any number of readers may attach and detach freely; the stream is a view of
    the shared snapshot, never the owner of the collection. Closing this stream
    does NOT stop the acquisition.
    """
    registry = get_registry()
    if registry.get(run_id) is None and not (
        host_bridge.is_host_job_id(run_id) and host_bridge.fetch_helper_job(run_id)
    ):
        raise HTTPException(status_code=404, detail="Acquisition run not found.")

    async def event_gen() -> AsyncIterator[str]:
        last_payload: str | None = None
        idle_ticks = 0
        while True:
            run = registry.get(run_id)
            mapped: dict[str, Any] | None = None
            if run is None:
                job = (
                    host_bridge.fetch_helper_job(run_id)
                    if host_bridge.is_host_job_id(run_id)
                    else None
                )
                if job is None:
                    yield f"event: error\ndata: {json.dumps({'message': 'Run disappeared.'})}\n\n"
                    return
                mapped = host_bridge.helper_job_as_run(job, detail=True)
                payload = json.dumps({k: v for k, v in mapped.items() if k != "result"})
            else:
                payload = json.dumps(run.summary())

            if payload != last_payload:
                yield f"event: progress\ndata: {payload}\n\n"
                last_payload = payload
                idle_ticks = 0
            else:
                idle_ticks += 1
                # Heartbeat every ~15s. Without it, proxies close an idle
                # connection during the long quiet stretches of a big pull and
                # the examiner sees the run as dead while it is still running.
                if idle_ticks >= 15:
                    yield ": keep-alive\n\n"
                    idle_ticks = 0

            if mapped is not None:
                if mapped.get("status") in TERMINAL_STATES:
                    yield f"event: done\ndata: {json.dumps(mapped)}\n\n"
                    return
            elif run is not None and run.status in TERMINAL_STATES:
                yield f"event: done\ndata: {json.dumps(run.detail())}\n\n"
                return

            await asyncio.sleep(1)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _ledger() -> ToolVersionLedger | None:
    path = os.environ.get("FORENSIC_VALIDATION_LEDGER")
    if not path:
        return None
    try:
        return ToolVersionLedger(path)
    except Exception:
        return None


@router.get("/licence")
def licence_status(
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """Architecture section 3.3 — what this endpoint is licensed and equipped to do.

    Distinguishes a licensing gap (procurement can fix it) from a capability the
    platform does not implement at all. Neither may be reported as an absence of
    evidence on the device.
    """
    profile = load_profile()
    return {"tool_version": TOOL_VERSION, "licence": profile.as_dict()}


@router.get("/validation")
def validation_status(
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """Architecture section 15/22 — production readiness of the current build."""
    ledger = _ledger()
    if ledger is None:
        return {
            "tool_version": TOOL_VERSION,
            "configured": False,
            "production_ready": False,
            "message": (
                "No validation ledger is configured (FORENSIC_VALIDATION_LEDGER). "
                "Acceptance testing against a known-device corpus cannot be evidenced. "
                "Complete section 22 acceptance testing before casework."
            ),
            "acceptance_areas": ACCEPTANCE_AREAS,
            "critical_device_families": list(CRITICAL_DEVICE_FAMILIES),
        }
    status = ledger.status(TOOL_VERSION)
    return {
        **status,
        "configured": True,
        "acceptance_areas": ACCEPTANCE_AREAS,
        "version_drift": ledger.drift_from(TOOL_VERSION),
        "versions_seen": ledger.versions_seen(),
    }


@router.post("/validation/record")
def record_validation(
    payload: ValidationRecordRequest,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
) -> dict[str, Any]:
    """Record an acceptance area validated outside the automated harness.

    Peer review, network controls, analysis import and report generation cannot
    be self-certified by the tool. They are attested here by a named examiner so
    the ledger reflects the whole of section 22, not just the automatable part.
    """
    ledger = _ledger()
    if ledger is None:
        raise HTTPException(
            status_code=400,
            detail="No validation ledger configured (FORENSIC_VALIDATION_LEDGER).",
        )
    if payload.device_family not in CRITICAL_DEVICE_FAMILIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown device family. Expected one of: "
                   + ", ".join(CRITICAL_DEVICE_FAMILIES),
        )
    record = ledger.record_manual(
        tool_version=payload.tool_version or TOOL_VERSION,
        device_family=payload.device_family,
        examiner=getattr(user, "email", None) or getattr(user, "id", "unknown"),
        passed=payload.passed,
        approver=payload.approver,
        notes=payload.notes,
    )
    return {"recorded": record.as_dict(), "status": ledger.status(TOOL_VERSION)}


@router.post("/verify")
def verify_extraction(
    payload: VerifyRequest,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.read")),
) -> dict[str, Any]:
    """§12 — re-verify a stored extraction against its manifest."""
    import json
    from pathlib import Path

    manifest_file = Path(payload.manifest_path)
    if not manifest_file.is_file():
        raise HTTPException(status_code=404, detail="Manifest not found.")
    extraction = Path(payload.extraction_path)
    if not extraction.is_dir():
        raise HTTPException(status_code=404, detail="Extraction directory not found.")

    data = json.loads(manifest_file.read_text())
    manifest = IntegrityManifest(
        run_name=data.get("run_name", ""),
        created_utc=data.get("created_utc", ""),
        algorithms=tuple(data.get("algorithms") or ()),
    )
    result = verify_against_manifest(extraction, data)
    return {
        "run_name": manifest.run_name,
        "manifest_created_utc": manifest.created_utc,
        **result.as_dict(),
        "interpretation": (
            "Verified: the extraction is byte-identical to the state recorded at "
            "acquisition."
            if result.ok else
            "FAILED: the extraction no longer matches its acquisition-time hashes. "
            "Do not analyse this copy. Restore from the immutable original and "
            "record the discrepancy."
        ),
    }


# ---------------------------------------------------------------------------
# V1.31.14 — collection on a client PC streamed to the forensic server
# ---------------------------------------------------------------------------

class ClientStreamRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    scope: str = Field(default="full", pattern=r"^(full|packages)$")
    keep_local: bool = False
    client_host: str = Field(default="", max_length=120)


def _stream_error(exc: Exception) -> JSONResponse:
    from app.services.mobile_acquire.client_stream import StreamError

    if isinstance(exc, StreamError):
        return JSONResponse({"ok": False, "error": exc.code, "detail": exc.message, **exc.extra}, status_code=exc.status)
    return JSONResponse({"ok": False, "error": "stream_failed", "detail": str(exc)[:500]}, status_code=500)


def _stream_token(request: Request) -> str:
    from app.services.mobile_acquire.client_stream import TOKEN_HEADER

    return (request.headers.get(TOKEN_HEADER) or "").strip()


@router.post("/client-stream")
def create_client_stream(
    payload: ClientStreamRequest,
    user: CurrentUser = Depends(require_firm_permission("forensic.job.create")),
):
    """Upload session for a collection that runs on the examiner's PC; the page hands it to the helper there."""
    from app.service_identity import current_service
    from app.services.mobile_acquire import client_stream

    try:
        return client_stream.create_session(
            case_id=payload.case_id,
            scope=payload.scope,
            keep_local=payload.keep_local,
            client_host=payload.client_host,
            examiner=str(getattr(user, "email", "") or ""),
            platform=_required_mobile_platform(),
            service=str(current_service()),
        )
    except Exception as exc:
        return _stream_error(exc)


@router.put("/client-stream/{session_id}/chunk")
async def client_stream_chunk(session_id: str, request: Request):
    """File bytes from the client helper (session token, not a user sign-in)."""
    from fastapi.concurrency import run_in_threadpool

    from app.services.mobile_acquire import client_stream

    try:
        declared = int(request.headers.get("content-length") or 0)
        if declared > client_stream.MAX_CHUNK_BYTES:
            raise client_stream.StreamError(413, "bad_chunk", "Chunks are at most 64 MiB.")
        rel = client_stream.decode_rel(request.headers.get("x-stream-rel") or "")
        offset = int(request.headers.get("x-stream-offset") or 0)
        piece = (request.headers.get("x-stream-piece") or "").strip() == "1"
        data = await request.body()
        status, body = await run_in_threadpool(
            client_stream.put_chunk,
            session_id,
            _stream_token(request),
            rel,
            offset,
            data,
            "",
            piece,
        )
        return JSONResponse(body, status_code=status)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad_request", "detail": "X-Stream-Offset must be a number"}, status_code=400)
    except Exception as exc:
        return _stream_error(exc)


@router.post("/client-stream/{session_id}/files")
async def client_stream_files(session_id: str, request: Request):
    """Many small files in one request (client helper, session token)."""
    from fastapi.concurrency import run_in_threadpool

    from app.services.mobile_acquire import client_stream

    try:
        declared = int(request.headers.get("content-length") or 0)
        if declared > client_stream.MAX_BUNDLE_BYTES + 4 * 1024 * 1024:
            raise client_stream.StreamError(413, "bad_bundle", "Bundles are at most 64 MiB.")
        body = await request.body()
        return await run_in_threadpool(client_stream.put_bundle, session_id, _stream_token(request), body)
    except Exception as exc:
        return _stream_error(exc)


class ClientStreamDiscard(BaseModel):
    rel: str = Field(min_length=3, max_length=4096)


@router.post("/client-stream/{session_id}/discard")
def client_stream_discard(session_id: str, payload: ClientStreamDiscard, request: Request):
    from app.services.mobile_acquire import client_stream

    try:
        return client_stream.discard_file(session_id, _stream_token(request), payload.rel)
    except Exception as exc:
        return _stream_error(exc)


class ClientStreamCommit(BaseModel):
    rel: str = Field(min_length=3, max_length=4096)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    mtime_ms: int = Field(default=0, ge=0)
    total_files: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)


@router.post("/client-stream/{session_id}/commit")
def client_stream_commit(session_id: str, payload: ClientStreamCommit, request: Request):
    from app.services.mobile_acquire import client_stream

    try:
        return client_stream.commit_file(
            session_id,
            _stream_token(request),
            rel=payload.rel,
            size=payload.size,
            sha256=payload.sha256,
            mtime_ms=payload.mtime_ms,
            total_files=payload.total_files,
            total_bytes=payload.total_bytes,
        )
    except Exception as exc:
        return _stream_error(exc)


@router.get("/client-stream/{session_id}/status")
def client_stream_status(session_id: str, request: Request, format: str = Query(default="json", pattern="^(json|tsv)$")):
    """What the server holds. ``format=tsv``: a status line, then one "<size>TAB<sha256>TAB<rel>" line per file
    (an iPhone backup has 100 000+ files; Windows PowerShell 5.1 reads that faster than JSON)."""
    from app.services.mobile_acquire import client_stream

    try:
        out = client_stream.session_status(session_id, _stream_token(request))
    except Exception as exc:
        return _stream_error(exc)
    if format != "tsv":
        return out
    head = "\t".join(["#status", str(out["status"] or ""), out["run_name"], out["server_case_dir"]])
    lines = [head] + [f"{size}\t{sha}\t{rel}" for rel, (size, sha) in out["files"].items()]
    return Response(content=("\n".join(lines) + "\n").encode("utf-8"), media_type="text/plain; charset=utf-8")


class ClientStreamFile(BaseModel):
    rel: str = Field(min_length=3, max_length=4096)
    size: int = Field(ge=0)
    sha256: str = Field(default="", pattern=r"^([0-9a-fA-F]{64})?$")


class ClientStreamFinish(BaseModel):
    run_name: str = Field(min_length=1, max_length=255)
    files: list[ClientStreamFile] = Field(default_factory=list, max_length=2_000_000)
    summary: dict[str, Any] | None = None
    client_host: str = Field(default="", max_length=120)
    client_case_dir: str = Field(default="", max_length=1024)
    warnings: list[str] = Field(default_factory=list, max_length=200)


@router.post("/client-stream/{session_id}/finish")
def client_stream_finish(session_id: str, payload: ClientStreamFinish, request: Request):
    from app.services.mobile_acquire import client_stream

    try:
        out = client_stream.finish(
            session_id,
            _stream_token(request),
            run_name=payload.run_name,
            files=[f.model_dump() for f in payload.files],
            summary=payload.summary,
            client_host=payload.client_host,
            client_case_dir=payload.client_case_dir,
            warnings=payload.warnings,
        )
        return out if out.get("ok") else JSONResponse(out, status_code=409)
    except Exception as exc:
        return _stream_error(exc)


class ClientStreamAbort(BaseModel):
    reason: str = Field(default="", max_length=2000)


@router.post("/client-stream/{session_id}/abort")
def client_stream_abort(session_id: str, payload: ClientStreamAbort, request: Request):
    from app.services.mobile_acquire import client_stream

    try:
        return client_stream.abort(session_id, _stream_token(request), payload.reason)
    except Exception as exc:
        return _stream_error(exc)

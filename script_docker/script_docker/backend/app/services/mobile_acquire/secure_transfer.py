"""Private, resumable transfer of a mobile acquisition package to its owning service.

Only a short-lived, signed single-acquisition HMAC capability can write chunks.  Evidence bytes and
server paths never flow through browser APIs.  Storage is scoped by tenant and
Android/iOS, verifies per-chunk and whole-file SHA-256 and publishes atomically.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings

CHUNK_BYTES = 8 * 1024 * 1024
MAX_PARTS = 131072  # 1 TiB with 8 MiB parts
MAX_IMAGE_BYTES = CHUNK_BYTES * MAX_PARTS
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
REF_PREFIX = "mobile-transfer:"
TOKEN_TYPE = "mobile-acquisition-transfer-v1"


class TransferError(ValueError):
    pass


def _platform(os_name: str) -> str:
    if os_name not in ("android", "ios"):
        raise TransferError("Android or iOS platform is required")
    return os_name


def _hex_id(value: str) -> str:
    if not ID_RE.fullmatch(str(value or "")):
        raise TransferError("Invalid transfer identifier")
    return value


def _sha(value: str) -> str:
    digest = str(value or "").lower()
    if not SHA_RE.fullmatch(digest):
        raise TransferError("Invalid SHA-256 digest")
    return digest


def _tenant_key(tenant: str) -> str:
    if not tenant or not tenant.strip():
        raise TransferError("Tenant is required")
    return hashlib.sha256(tenant.strip().lower().encode()).hexdigest()[:32]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class MobileEvidenceTransfer:
    def __init__(self, root: str | Path | None = None, secret: str | None = None):
        settings = get_settings()
        self.root = Path(root or settings.data_root).resolve() / "private_mobile_transfers_v1"
        self.secret = secret if secret is not None else settings.jwt_secret
        if not self.secret:
            raise TransferError("Server transfer signing secret is not configured")

    def ticket(self, *, tenant: str, platform: str, case_id: str, evidence_id: str, examiner: str) -> dict[str, str]:
        platform = _platform(platform)
        if not case_id.strip() or not evidence_id.strip() or not examiner.strip():
            raise TransferError("Case, evidence and examiner are required")
        tid = uuid.uuid4().hex
        expiration = datetime.now(timezone.utc) + timedelta(hours=24)
        payload = {
            "type": TOKEN_TYPE, "tid": tid, "tenant": tenant.strip().lower(),
            "platform": platform, "case_id": case_id.strip(),
            "evidence_id": evidence_id.strip(), "sub": examiner,
            "exp": int(expiration.timestamp()),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        signature = hmac.new(self.secret.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).digest()
        token = b64 + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return {"transfer_id": tid, "token": token, "platform": platform,
                "tenant": tenant.strip().lower(), "expires_at": expiration.isoformat()}

    def verify_ticket(self, token: str, *, service_platform: str | None = None) -> dict[str, Any]:
        try:
            if len(token) > 4096:
                raise ValueError("Oversized ticket")
            message, signed = token.split(".", 1)
            expected = hmac.new(self.secret.encode("utf-8"), message.encode("ascii"), hashlib.sha256).digest()
            supplied = base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4))
            if not hmac.compare_digest(expected, supplied):
                raise ValueError("Signature mismatch")
            raw = base64.urlsafe_b64decode(message + "=" * (-len(message) % 4))
            payload = json.loads(raw)
            if not isinstance(payload, dict) or int(payload.get("exp") or 0) <= datetime.now(timezone.utc).timestamp():
                raise ValueError("Expired ticket")
        except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise TransferError("Invalid or expired transfer ticket") from exc
        if payload.get("type") != TOKEN_TYPE:
            raise TransferError("Invalid transfer ticket type")
        _hex_id(payload.get("tid", ""))
        platform = _platform(payload.get("platform", ""))
        if service_platform is not None and service_platform != platform:
            raise TransferError("Transfer is not authorized for this mobile service")
        _tenant_key(payload.get("tenant", ""))
        if not payload.get("case_id") or not payload.get("evidence_id"):
            raise TransferError("Incomplete transfer ticket")
        return payload

    def directory(self, claims: dict[str, Any]) -> Path:
        return self.root / _tenant_key(claims["tenant"]) / _platform(claims["platform"]) / _hex_id(claims["tid"])

    def _prepare_private_directory(self, claims: dict[str, Any]) -> Path:
        """Restrict every newly created server directory to the service account."""
        parts = (
            self.root,
            self.root / _tenant_key(claims["tenant"]),
            self.root / _tenant_key(claims["tenant"]) / _platform(claims["platform"]),
            self.directory(claims),
        )
        for folder in parts:
            if folder.is_symlink():
                raise TransferError("A private evidence directory cannot be a symlink")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                folder.chmod(0o700)
            except OSError:
                # The service account may run on a filesystem without POSIX modes.
                # Deployment must provide equivalent volume ACLs.
                pass
        return parts[-1]

    def put_part(self, claims: dict[str, Any], index: int, blob: bytes, checksum: str) -> dict[str, Any]:
        if not isinstance(index, int) or index < 0 or index >= MAX_PARTS:
            raise TransferError("Invalid chunk number")
        expected = _sha(checksum)
        if not blob or len(blob) > CHUNK_BYTES:
            raise TransferError("Chunk must contain 1 to 8 MiB")
        actual = hashlib.sha256(blob).hexdigest()
        if actual != expected:
            raise TransferError("Chunk SHA-256 mismatch")
        base = self._prepare_private_directory(claims)
        if (base / "receipt.json").exists():
            raise TransferError("Evidence is already sealed")
        parts = base / "parts"
        parts.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = parts / f"{index:06d}.part"
        if path.exists():
            with path.open("rb") as fh:
                previous = hashlib.sha256(fh.read()).hexdigest()
            if previous != expected:
                raise TransferError("Chunk index has different stored content")
            return {"accepted": True, "index": index, "sha256": actual, "replayed": True}
        fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=parts)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(blob)
                out.flush()
                os.fsync(out.fileno())
            # Never overwrite an existing chunk, even after a concurrent retry.
            try:
                os.link(tmp, path)
            except FileExistsError:
                with path.open("rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != expected:
                        raise TransferError("Concurrent chunk hash mismatch")
        finally:
            Path(tmp).unlink(missing_ok=True)
        return {"accepted": True, "index": index, "sha256": actual}

    def complete(self, claims: dict[str, Any], *, part_count: int, byte_count: int, sha256: str) -> dict[str, Any]:
        if part_count < 1 or part_count > MAX_PARTS or byte_count < 1 or byte_count > MAX_IMAGE_BYTES:
            raise TransferError("Invalid evidence size or part count")
        digest = _sha(sha256)
        base = self._prepare_private_directory(claims)
        receipt = base / "receipt.json"
        if receipt.exists():
            existing = json.loads(receipt.read_text(encoding="utf-8"))
            if (existing.get("sha256"), existing.get("size_bytes"), existing.get("part_count")) != (digest, byte_count, part_count):
                raise TransferError("Transfer already sealed with different content")
            return self.public_receipt(existing)
        parts = base / "parts"
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        hasher = hashlib.sha256()
        total = 0
        fd, staged = tempfile.mkstemp(prefix=".evidence-", dir=base)
        try:
            with os.fdopen(fd, "wb") as out:
                for idx in range(part_count):
                    part = parts / f"{idx:06d}.part"
                    if not part.is_file():
                        raise TransferError(f"Missing evidence chunk {idx}")
                    with part.open("rb") as inp:
                        while True:
                            block = inp.read(1024 * 1024)
                            if not block:
                                break
                            total += len(block)
                            if total > byte_count:
                                raise TransferError("Evidence size does not match manifest")
                            hasher.update(block)
                            out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if total != byte_count or hasher.hexdigest() != digest:
                raise TransferError("Whole-evidence checksum or byte count mismatch")
            # SHA verification occurs before the immutable source is published.
            sealed = base / "evidence.zip"
            if sealed.exists():
                raise TransferError("Evidence package already exists without a valid receipt")
            os.link(staged, sealed)  # atomic create-only: no overwrite on concurrent completion
            try:
                sealed.chmod(0o400)
            except OSError:
                pass
            info = {
                "transfer_id": _hex_id(claims["tid"]), "tenant": claims["tenant"],
                "platform": _platform(claims["platform"]), "case_id": claims["case_id"],
                "evidence_id": claims["evidence_id"], "examiner": claims["sub"],
                "sha256": digest, "size_bytes": total, "part_count": part_count,
                "received_utc": _utc(), "verified": True,
            }
            fd2, temp_receipt = tempfile.mkstemp(prefix=".receipt-", dir=base)
            try:
                with os.fdopen(fd2, "w", encoding="utf-8") as fp:
                    json.dump(info, fp, sort_keys=True)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(temp_receipt, receipt)
            finally:
                Path(temp_receipt).unlink(missing_ok=True)
            # Leave sealed evidence alone; uploaded chunks can be cleaned after receipt.
            for part in parts.glob("*.part"):
                part.unlink(missing_ok=True)
            try:
                parts.rmdir()
            except OSError:
                pass
            return self.public_receipt(info)
        finally:
            Path(staged).unlink(missing_ok=True)

    @staticmethod
    def public_receipt(info: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True, "server_stored": True, "verified": True,
            "transfer_id": info["transfer_id"],
            "evidence_ref": REF_PREFIX + info["transfer_id"],
            "sha256": info["sha256"], "size_bytes": info["size_bytes"],
        }

    def resolve(self, evidence_ref: str, *, tenant: str, platform: str, case_id: str | None = None) -> Path:
        if not evidence_ref.startswith(REF_PREFIX):
            raise TransferError("Expected private mobile evidence reference")
        tid = _hex_id(evidence_ref[len(REF_PREFIX):])
        base = self.directory({"tid": tid, "platform": platform, "tenant": tenant})
        receipt = base / "receipt.json"
        evidence = base / "evidence.zip"
        if not receipt.is_file() or not evidence.is_file() or evidence.is_symlink():
            raise TransferError("Server evidence is missing or not verified")
        info = json.loads(receipt.read_text(encoding="utf-8"))
        if (info.get("tenant"), info.get("platform"), info.get("transfer_id"), info.get("verified")) != (
            tenant.strip().lower(), platform, tid, True
        ):
            raise TransferError("Evidence tenant or platform mismatch")
        if case_id is not None and info.get("case_id") != case_id:
            raise TransferError("Evidence does not belong to the selected case")
        if evidence.stat().st_size != info.get("size_bytes"):
            raise TransferError("Stored evidence size differs from verified receipt")
        return evidence

    def verify_sealed(self, evidence_ref: str, *, tenant: str, platform: str) -> dict[str, Any]:
        file = self.resolve(evidence_ref, tenant=tenant, platform=platform)
        manifest = json.loads((file.parent / "receipt.json").read_text(encoding="utf-8"))
        hasher = hashlib.sha256()
        with file.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(block)
        ok = hasher.hexdigest() == manifest.get("sha256")
        return {"ok": ok, "verified": ok, "sha256": hasher.hexdigest(),
                "size_bytes": file.stat().st_size}

#!/usr/bin/env python3
"""Examiner-host mobile package uploader: stdlib only; no browser evidence bytes.

Usage: python scripts/mobile_server_transfer.py --payload run.json --exports DIR
The supplied payload contains a one-run capability and gateway URL. Never log it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHUNK_BYTES = 8 * 1024 * 1024


def validate_gateway(value: str) -> str:
    url = urllib.parse.urlsplit(str(value or "").rstrip("/"))
    local = url.hostname in ("localhost", "127.0.0.1", "::1")
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Invalid transfer endpoint")
    if url.scheme != "https" and not (local and url.scheme == "http"):
        raise ValueError("Remote mobile evidence transfer requires HTTPS")
    if not url.path.endswith("/api/acquisition/transfer"):
        raise ValueError("Unexpected transfer endpoint")
    return urllib.parse.urlunsplit((url.scheme, url.netloc, url.path, "", ""))


def request_json(url: str, token: str, tenant: str, platform: str, method: str, body: bytes,
                 checksum: str = "", retries: int = 4) -> dict:
    headers = {
        "Authorization": "Bearer " + token,
        "X-Tenant": tenant,
        "X-Aetheris-Service": "mobile-" + platform,
        "Content-Type": "application/octet-stream" if method == "PUT" else "application/json",
    }
    if checksum:
        headers["X-Chunk-SHA256"] = checksum
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read(128 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Retry only transient server errors, never auth or SHA mismatches.
            if exc.code < 500 or attempt + 1 == retries:
                raise RuntimeError(f"Mobile evidence transfer returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt + 1 == retries:
                raise RuntimeError("Mobile evidence transfer interrupted; preserved locally for retry") from None
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("Mobile evidence transfer failed")


def full_package(exports: str | Path) -> Path:
    folder = Path(exports)
    packages = [p for p in folder.glob("*.zip") if p.is_file() and p.stat().st_size]
    if len(packages) != 1:
        raise ValueError("Expected exactly one nonempty full evidence ZIP package")
    return packages[0]


def upload_package(payload: dict, exports: str | Path) -> dict:
    transfer = payload.get("server_transfer") or {}
    tid = str(transfer.get("transfer_id") or "")
    token = str(transfer.get("token") or "")
    platform = str(transfer.get("platform") or "")
    tenant = str(transfer.get("tenant") or "")
    if not (len(tid) == 32 and token and platform in ("android", "ios") and tenant):
        raise ValueError("Missing scoped server transfer ticket; image not marked complete")
    if platform != ("ios" if str(payload.get("adapter") or "").startswith("ios") else "android"):
        raise ValueError("Acquisition adapter and transfer platform mismatch")
    base = validate_gateway(transfer.get("base_url", ""))
    package = full_package(exports)
    hasher = hashlib.sha256()
    size = 0
    count = 0
    with package.open("rb") as source:
        while True:
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            checksum = hashlib.sha256(chunk).hexdigest()
            request_json(f"{base}/{tid}/chunks/{count}", token, tenant, platform,
                         "PUT", chunk, checksum)
            hasher.update(chunk)
            size += len(chunk)
            count += 1
    if not count:
        raise ValueError("Evidence package is empty")
    receipt = request_json(f"{base}/{tid}/complete", token, tenant, platform, "POST",
                           json.dumps({"part_count": count, "size_bytes": size,
                                       "sha256": hasher.hexdigest()}).encode("utf-8"))
    if (not receipt.get("server_stored") or not receipt.get("verified")
            or receipt.get("sha256") != hasher.hexdigest() or receipt.get("size_bytes") != size):
        raise RuntimeError("Server did not confirm full evidence SHA-256 verification")
    # The canonical image ZIP is now verified and private on the server.
    # Remove the *generated ZIP copy* on the examiner workstation. Original
    # collection staging remains under forensic retention/chain-of-custody policy.
    try:
        package.unlink()
        removed = True
    except OSError:
        removed = False
    return {"server_stored": True, "verified": True, "evidence_ref": receipt["evidence_ref"],
            "sha256": receipt["sha256"], "size_bytes": size, "transfer_id": tid,
            "client_package_removed": removed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True)
    ap.add_argument("--exports", required=True)
    args = ap.parse_args()
    try:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8-sig"))
        print(json.dumps({"ok": True, "receipt": upload_package(payload, args.exports)}))
        return 0
    except Exception as exc:
        # Redact exception messages that could include credential or local paths.
        print(json.dumps({"ok": False, "error": type(exc).__name__ + ": " +
                          (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "transfer failed")}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Mobile acquisition server transfer integrity + isolation regression suite."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.services.mobile_acquire.secure_transfer import (
    CHUNK_BYTES,
    MobileEvidenceTransfer,
    TransferError,
    REF_PREFIX,
)


def provision(tmp_path, os_name="android", tenant="examiner-firm", case="CASE-20261117"):
    store = MobileEvidenceTransfer(root=tmp_path, secret="forensic-unit-test-secret")
    ticket = store.ticket(tenant=tenant, platform=os_name, case_id=case, evidence_id="E-1", examiner="unit-user")
    claims = store.verify_ticket(ticket["token"], service_platform=os_name)
    return store, ticket, claims


def test_empty_and_invalid_platform_rejected(tmp_path):
    store, _, _ = provision(tmp_path)
    with pytest.raises(TransferError):
        store.ticket(tenant="firm", platform="disk", case_id="CASE", evidence_id="E", examiner="user")
    with pytest.raises(TransferError):
        store.ticket(tenant="firm", platform="ios", case_id="", evidence_id="E", examiner="user")


def test_complete_chunks_durable_sha_and_private_path(tmp_path):
    store, ticket, claims = provision(tmp_path)
    image = b"logical-mobile-evidence" * 6000
    data = [image[:60000], image[60000:]]
    for i, chunk in enumerate(data):
        result = store.put_part(claims, i, chunk, hashlib.sha256(chunk).hexdigest())
        assert result["accepted"]
    checksum = hashlib.sha256(image).hexdigest()
    receipt = store.complete(claims, part_count=len(data), byte_count=len(image), sha256=checksum)
    assert receipt["server_stored"] and receipt["verified"]
    assert receipt["evidence_ref"] == REF_PREFIX + ticket["transfer_id"]
    assert "private_mobile" not in json.dumps(receipt)
    stored = store.resolve(receipt["evidence_ref"], tenant="examiner-firm", platform="android", case_id="CASE-20261117")
    assert stored.read_bytes() == image
    assert stored.name == "evidence.zip"
    assert not (stored.parent / "parts").exists()
    assert store.complete(claims, part_count=2, byte_count=len(image), sha256=checksum) == receipt


def test_other_mobile_platform_cannot_reuse_ticket(tmp_path):
    store, ticket, claims = provision(tmp_path, os_name="ios")
    with pytest.raises(TransferError, match="not authorized"):
        store.verify_ticket(ticket["token"], service_platform="android")
    with pytest.raises(TransferError):
        store.resolve(REF_PREFIX + claims["tid"], tenant="examiner-firm", platform="android")


def test_cross_tenant_and_case_read_blocked(tmp_path):
    store, _, claims = provision(tmp_path)
    blob = b"evidence"
    store.put_part(claims, 0, blob, hashlib.sha256(blob).hexdigest())
    r = store.complete(claims, part_count=1, byte_count=len(blob), sha256=hashlib.sha256(blob).hexdigest())
    for kw in ({"tenant": "other", "platform": "android"},
               {"tenant": "examiner-firm", "platform": "ios"},
               {"tenant": "examiner-firm", "platform": "android", "case_id": "WRONG"}):
        with pytest.raises(TransferError):
            store.resolve(r["evidence_ref"], **kw)


def test_tampered_ticket_fails(tmp_path):
    store, ticket, _ = provision(tmp_path)
    with pytest.raises(TransferError):
        store.verify_ticket(ticket["token"] + "xyz", service_platform="android")
    with pytest.raises(TransferError):
        store.verify_ticket("not.a.valid.ticket", service_platform="android")


def test_corrupt_chunk_and_conflicting_retry_rejected(tmp_path):
    store, _, claims = provision(tmp_path)
    blob = b"first"
    with pytest.raises(TransferError, match="SHA-256 mismatch"):
        store.put_part(claims, 0, blob, hashlib.sha256(b"different").hexdigest())
    assert store.put_part(claims, 0, blob, hashlib.sha256(blob).hexdigest())["accepted"]
    assert store.put_part(claims, 0, blob, hashlib.sha256(blob).hexdigest())["replayed"]
    with pytest.raises(TransferError, match="different stored content"):
        store.put_part(claims, 0, b"second", hashlib.sha256(b"second").hexdigest())


def test_missing_chunks_and_wrong_size_never_seal(tmp_path):
    store, _, claims = provision(tmp_path)
    blob = b"mobile"
    store.put_part(claims, 0, blob, hashlib.sha256(blob).hexdigest())
    with pytest.raises(TransferError, match="Missing evidence chunk"):
        store.complete(claims, part_count=2, byte_count=len(blob)*2,
                       sha256=hashlib.sha256(blob * 2).hexdigest())
    with pytest.raises(TransferError, match="checksum or byte count"):
        store.complete(claims, part_count=1, byte_count=len(blob)+1,
                       sha256=hashlib.sha256(blob).hexdigest())
    assert not (store.directory(claims) / "receipt.json").exists()


def test_invalid_id_and_checksum_blocked(tmp_path):
    store, _, claims = provision(tmp_path)
    with pytest.raises(TransferError):
        store.put_part(claims, -1, b"x", hashlib.sha256(b"x").hexdigest())
    with pytest.raises(TransferError):
        store.put_part(claims, 0, b"x", "1234")
    with pytest.raises(TransferError):
        store.resolve("mobile-transfer:../escape", tenant="examiner-firm", platform="android")


def test_no_chunk_after_seal(tmp_path):
    store, _, claims = provision(tmp_path)
    blob = b"ok"
    checksum = hashlib.sha256(blob).hexdigest()
    store.put_part(claims, 0, blob, checksum)
    store.complete(claims, part_count=1, byte_count=len(blob), sha256=checksum)
    with pytest.raises(TransferError, match="already sealed"):
        store.put_part(claims, 0, blob, checksum)

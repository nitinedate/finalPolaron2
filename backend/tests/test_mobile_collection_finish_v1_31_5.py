"""V1.31.5: the evidence ZIP reports progress to the end, not only for the backup part.

The ZIP holds the sealed backup (02_Original_Extraction) and the readable copies (WhatsApp, messages,
photos). Both are counted in the total, but only the backup part reported progress, so the bar stopped
where the backup ended (about 95% for an iPhone) for as long as the readable copies took to write.
"""

from __future__ import annotations

import zipfile


def test_readable_copies_report_progress_to_the_end(tmp_path):
    from app.services.mobile_acquire.export_packages import build_export_packages

    original = tmp_path / "original"
    (original / "ios_image" / "UDID").mkdir(parents=True)
    (original / "ios_image" / "UDID" / "Manifest.db").write_bytes(b"m" * 4096)
    for i in range(600):
        (original / "ios_image" / "UDID" / f"f{i:04d}").write_bytes(b"b" * 1024)
    readable = tmp_path / "working" / "readable_artifacts"
    (readable / "whatsapp").mkdir(parents=True)
    for i in range(1200):
        (readable / "whatsapp" / f"media{i:04d}.jpg").write_bytes(b"r" * 1024)

    events: list[dict] = []
    bundle = build_export_packages(
        run_name="run1", export_dir=tmp_path / "exports", original=original, method="advanced_logical",
        full_payload=True, readable=readable, progress=lambda e: events.append(dict(e)),
    )
    assert bundle.full_payload_in_zip is True
    zip_events = [e for e in events if e.get("phase") == "export_zip" and "export_bytes_done" in e]
    backup_part = [e for e in zip_events if "backup and media files" in str(e.get("detail"))]
    readable_part = [e for e in zip_events if "readable copies" in str(e.get("detail"))]
    assert backup_part and readable_part, "both parts of the ZIP report progress"
    total = zip_events[-1]["export_bytes_total"]
    assert total == 4096 + 600 * 1024 + 1200 * 1024
    # The readable part continues where the backup part stopped, up to the end.
    assert readable_part[0]["export_bytes_done"] > backup_part[-1]["export_bytes_done"]
    assert readable_part[-1]["export_bytes_done"] >= total * 0.99
    assert readable_part[-1]["progress_pct"] >= 99
    assert max(e["progress_pct"] for e in backup_part) < 95
    assert "Packaging the evidence ZIP - readable copies (WhatsApp, messages, photos)" in readable_part[-1]["detail"]
    with zipfile.ZipFile(tmp_path / "exports" / "run1.zip") as zf:
        names = set(zf.namelist())
    assert "readable_artifacts/whatsapp/media1199.jpg" in names
    assert "02_Original_Extraction/ios_image/UDID/Manifest.db" in names

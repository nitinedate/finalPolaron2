"""WAN E01 copies must count their transferred bytes as useful progress."""
from app.services.progress_agent import acquisition_fingerprint, acquisition_diagnosis


def test_remote_transfer_bytes_change_fingerprint_not_heartbeat():
    base = {'pipeline_progress': {'extraction_activity': {
        'subphase': 'remote_staging', 'total_bytes': 4000, 'copied_bytes': 1000,
        'last_path': 'segment.E01'}}}
    progressed = {'pipeline_progress': {'extraction_activity': {
        'subphase': 'remote_staging', 'total_bytes': 4000, 'copied_bytes': 2000,
        'last_path': 'segment.E01'}}}
    heartbeat = {'pipeline_progress': {'extraction_activity': {
        **base['pipeline_progress']['extraction_activity'], 'updated_at': '2026-11-19T08:30:00'}}}
    assert acquisition_fingerprint(base) != acquisition_fingerprint(progressed)
    assert acquisition_fingerprint(base) == acquisition_fingerprint(heartbeat)


def test_missing_client_mount_reports_precise_subphase():
    job = {'pipeline_progress': {'extraction_activity': {'subphase': 'remote_mount'}}}
    text = acquisition_diagnosis(job, 100)
    assert 'Client evidence mount' in text
    assert 'FUSE' in text

"""Offline Android post-extraction wiring regression tests."""
from pathlib import Path
from app.services.mobile_acquire.adapters.android_adb import AndroidAdbAdapter
from app.services.mobile_acquire.device_profile import DeviceProfile
from app.services.mobile_acquire.methods import CollectionMethod


def test_auto_scan_runs_after_collection_without_phone_access(monkeypatch, tmp_path):
    adapter = AndroidAdbAdapter()
    monkeypatch.setattr(adapter, '_collect_state', lambda *a, **k: None)
    monkeypatch.setattr(adapter, '_pull_targets', lambda *a, **k: None)
    monkeypatch.setattr('app.services.mobile_acquire.privileged_app_pull.pull_whatsapp_key_evidence',
                        lambda **k: {'bytes': 0, 'whatsapp_key_files': [], 'limitations': [], 'errors': []})
    folder = tmp_path / 'shared_storage' / 'Android' / 'media' / 'com.whatsapp' / 'WhatsApp' / 'Databases'
    folder.mkdir(parents=True)
    (folder / 'msgstore.db.crypt15').write_bytes(b'fictional')
    outcome = adapter.acquire(profile=DeviceProfile(os_family='android', serial='TEST'),
                              method=CollectionMethod.LOGICAL, destination=tmp_path,
                              authorized_actions=['whatsapp_generate_key_and_backup'])
    assert (tmp_path / 'whatsapp-post-extraction.json').is_file()
    assert any('missing-key' in x for x in outcome.warnings)
    assert not (folder / 'msgstore.db').exists()
    assert not (folder / 'encrypted_backup.key').exists()

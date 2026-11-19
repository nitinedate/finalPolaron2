from pathlib import Path
from unittest.mock import patch

from app.services.mobile_acquire.whatsapp_device_recovery import (
    DeviceRecoveryUnavailable, _capture_key, attempt_authorized_rebackup,
)


def test_disabled_by_default(tmp_path: Path):
    with patch.dict('os.environ', {}, clear=True):
        assert attempt_authorized_rebackup(adb='adb', serial='test', acquisition_root=tmp_path)['status'] == 'device_modification_disabled'


def test_requires_examiner_authorization(tmp_path: Path):
    with patch.dict('os.environ', {'POLARON_WHATSAPP_ALLOW_DEVICE_MUTATION': '1'}):
        assert attempt_authorized_rebackup(adb='adb', serial='test', acquisition_root=tmp_path)['status'] == 'authorization_required'


def test_exact_key_from_device_ui():
    groups = ['ABCD'] * 16
    with patch('app.services.mobile_acquire.whatsapp_device_recovery._screen', return_value=[{'text': ' '.join(groups), 'description': ''}]):
        assert _capture_key('adb', 'x') == 'abcd' * 16


def test_no_key_never_fabricated():
    with patch('app.services.mobile_acquire.whatsapp_device_recovery._screen', return_value=[{'text':'Turn on', 'description':''}]):
        try:
            _capture_key('adb', 'x')
            assert False, 'must fail when key is hidden'
        except DeviceRecoveryUnavailable as exc:
            assert str(exc) == 'device_generated_key_not_exposed_to_automation'

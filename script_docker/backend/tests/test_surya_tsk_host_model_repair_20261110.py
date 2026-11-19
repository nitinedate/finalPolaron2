from __future__ import annotations
import importlib.util
import io
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]

def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_surya_model_id_accepted(monkeypatch):
    adapter = load('surya_adapter_case1', 'backend/app/services/surya_adapter.py')
    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(adapter, 'urlopen', lambda *args, **kw: Response(json.dumps({'data':[{'id':'datalab-to/surya-ocr-2.5'}]}).encode()))
    adapter._ready('http://host.docker.internal:8000/v1')


def test_surya_gguf_path_rejected_before_inference(monkeypatch):
    adapter = load('surya_adapter_case2', 'backend/app/services/surya_adapter.py')
    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(adapter, 'urlopen', lambda *args, **kw: Response(json.dumps({'data':[{'id':r'C:\Users\test\surya-2.5.gguf'}]}).encode()))
    with pytest.raises(adapter.SuryaUnavailable, match='model identity mismatch'):
        adapter._ready('http://host.docker.internal:8000/v1')


def test_launcher_sets_stable_alias():
    source = (ROOT/'scripts/ensure_host_surya.ps1').read_text()
    assert "'--alias', $requiredModelId" in source
    assert 'Stop-Process -Id $savedPid' in source
    assert "Get-CimInstance Win32_Process" in source


def test_tsk_can_recover_name_metadata():
    source = (ROOT/'backend/app/services/tsk_parallel_inventory.py').read_text()
    assert "getattr(info.name, 'meta_addr', None)" in source
    assert 'fs_info.open_meta(inode=int(name_addr))' in source
    assert "count('metadata_unresolved')" in source


def test_coverage_does_not_conflate_loop_and_metadata():
    source = (ROOT/'backend/app/services/extracted_disk.py').read_text()
    assert 'recovered by inode' in source
    assert '(loop protection); analyst review required' not in source
    assert 'tsk_metadata_unresolved' in source

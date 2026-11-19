"""Regression tests for host saturation caused by nested Celery/native threads."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import adaptive_semaphore, cpu_budget
from app.services.host_capacity import HostSnapshot, build_capacity_plan


def test_effective_cpu_count_respects_cgroup_v2_quota_and_cpuset(tmp_path, monkeypatch):
    (tmp_path / 'cpu.max').write_text('150000 100000\n')
    (tmp_path / 'cpuset.cpus.effective').write_text('0-7\n')
    monkeypatch.setattr(cpu_budget.os, 'cpu_count', lambda: 24)
    monkeypatch.setattr(cpu_budget.os, 'sched_getaffinity', lambda _p: set(range(24)))
    assert cpu_budget.effective_cpu_count(root=str(tmp_path)) == 1
    assert cpu_budget.cgroup_quota_cores(root=str(tmp_path)) == 1.5
    (tmp_path / 'cpu.max').write_text('max 100000\n')
    assert cpu_budget.effective_cpu_count(root=str(tmp_path)) == 8


def test_effective_memory_respects_cgroup_v2(tmp_path):
    (tmp_path / 'memory.max').write_text(str(4 * 1024 ** 3))
    (tmp_path / 'memory.current').write_text(str(3 * 1024 ** 3))
    assert cpu_budget.effective_memory_mb(64000, 24000, root=str(tmp_path)) == (4096, 1024)


def test_nonblocking_host_cpu_sample_is_not_load_average(monkeypatch):
    states = iter([('cpu 100 0 100 800 0 0 0 0', 10.0),
                   ('cpu 300 0 300 1000 0 0 0 0', 11.0)])
    data = []
    def read(_path):
        return data[-1][0]
    monkeypatch.setattr(cpu_budget, '_CPU_SAMPLE', None)
    monkeypatch.setattr(cpu_budget, '_read_text', read)
    monkeypatch.setattr(cpu_budget.time, 'monotonic', lambda: data[-1][1])
    data.append(next(states))
    assert cpu_budget.host_cpu_utilization_percent() is None
    data.append(next(states))
    # Busy ticks increase by 400, idle increases by 200 => 66.7% busy.
    assert cpu_budget.host_cpu_utilization_percent() == pytest.approx(66.67, abs=0.1)


def test_high_actual_cpu_busy_reduces_slots_and_threads(monkeypatch):
    monkeypatch.setenv('CPU_SAFE_TARGET_PCT', '50')
    sample = SimpleNamespace(
        profile='workstation', cpu_logical=16, cpu_util_pct=95,
        load_1m=2, cpu_temp_c=45, mem_total_mb=64000,
        mem_available_mb=40000, gpu_available=False,
    )
    assert adaptive_semaphore._cpu_pace_from_snapshot(sample) <= 0.20
    assert adaptive_semaphore.resource_capacity('cpu_heavy', requested=5, snapshot=sample) == 1
    assert adaptive_semaphore.cap_parallelism('parse_workers', 10, snapshot=sample) == 1


def test_cpu_safe_plan_caps_12gb_gpu_profile_when_busy(monkeypatch):
    monkeypatch.setenv('CPU_SAFE_TARGET_PCT', '50')
    monkeypatch.setenv('CPU_SAFE_MODE_ENABLED', 'true')
    plan = build_capacity_plan(HostSnapshot(
        cpu_logical=16, cpu_util_pct=90.0, mem_total_mb=32000,
        mem_available_mb=18000, profile='laptop', gpu_available=True,
        gpu_vram_total_mb=12288,
    ))
    assert int(plan.env['EXTRACT_DISK_WORKERS']) == 1
    assert int(plan.env['PARSE_WORKERS']) == 1
    assert int(plan.env['MAX_CONCURRENT_DISK_BUILDS']) == 1
    assert int(plan.env['PARSE_PARALLEL_BUCKETS']) == 1


def _entrypoint():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'dynamic_perf_entrypoint.py'
    spec = importlib.util.spec_from_file_location('dynamic_perf_entrypoint_cpu', path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_combined_disk_inventory_queue_no_longer_bypasses_plan(monkeypatch):
    entry = _entrypoint()
    monkeypatch.setenv('FORENSIC_WORKER_CONCURRENCY', '2')
    argv = ['celery', '-A', 'app.celery_app.celery', 'worker',
            '--concurrency=8', '-Q', 'disk-build,disk-inventory']
    assert '--concurrency=2' in entry._rewrite_celery_concurrency(argv)


def test_all_celery_queues_use_cgroup_quota_process_cap(monkeypatch):
    entry = _entrypoint()
    monkeypatch.setenv('CPU_SAFE_MODE_ENABLED', 'true')
    monkeypatch.setenv('CPU_SAFE_TARGET_PCT', '50')
    monkeypatch.setattr(cpu_budget, 'effective_cpu_count', lambda: 4)
    for queue in ('disk-build,disk-inventory', 'disk-parse,disk-inventory',
                  'ocr-documents', 'quality-triage', 'rag-prep'):
        argv = ['celery', '-A', 'app.celery_app.celery', 'worker', '--concurrency=8', '-Q', queue]
        assert '--concurrency=2' in entry._limit_celery_concurrency(argv)


def test_native_blas_threads_are_bounded_even_when_old_env_sets_many(monkeypatch):
    entry = _entrypoint()
    monkeypatch.setenv('CPU_SAFE_MODE_ENABLED', 'true')
    monkeypatch.setenv('CPU_NATIVE_THREADS', '1')
    monkeypatch.setenv('OPENBLAS_NUM_THREADS', '24')
    monkeypatch.setenv('MKL_NUM_THREADS', '16')
    monkeypatch.setenv('OMP_NUM_THREADS', '8')
    entry._apply_native_thread_limits()
    assert cpu_budget.os.getenv('OPENBLAS_NUM_THREADS') == '1'
    assert cpu_budget.os.getenv('MKL_NUM_THREADS') == '1'
    assert cpu_budget.os.getenv('OMP_NUM_THREADS') == '1'
    assert cpu_budget.os.getenv('TOKENIZERS_PARALLELISM') == 'false'


def test_tesseract_media_threads_cannot_bypass_quota(monkeypatch):
    from app.services import forensic_media_review
    monkeypatch.setattr(cpu_budget, 'effective_cpu_count', lambda: 4)
    monkeypatch.setenv('CPU_SAFE_TARGET_PCT', '50')
    monkeypatch.setenv('MEDIA_REVIEW_WORKERS', '16')
    assert forensic_media_review.media_review_workers() == 2
    monkeypatch.delenv('MEDIA_REVIEW_WORKERS')
    assert forensic_media_review.media_review_workers() == 2


def test_dynamic_cpu_budget_does_not_starve_database_or_mobile(monkeypatch):
    monkeypatch.setenv('CPU_SAFE_TARGET_PCT', '50')
    assert cpu_budget.target_parallel_cores(8) == 4
    assert cpu_budget.target_parallel_cores(1) == 1

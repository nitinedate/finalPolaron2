"""Multi-image case must not release downstream work on partial extracts."""
from app.services.next_evidence_directory import combined_handoff_ready


def test_two_directories_merged():
    assert combined_handoff_ready("extracted", None, selected_count=2, saved_count=2) == (
        True, "All selected directories are saved and merged"
    )


def test_unfinished_image_holds_artifacts_and_rag():
    for status in ("busy", "waiting", "failed"):
        ready, _ = combined_handoff_ready(status, None, selected_count=2, saved_count=1)
        assert not ready


def test_no_missing_registered_directory_can_be_ignored():
    assert not combined_handoff_ready("extracted", None, selected_count=3, saved_count=2)[0]


def test_inventory_registration_must_complete():
    assert not combined_handoff_ready("extracted", None, selected_count=2, saved_count=2, materialize_pending=True)[0]


def test_source_registration_pending_holds_work():
    assert not combined_handoff_ready("none", "Waiting for every evidence directory", selected_count=1, saved_count=1)[0]


def test_one_directory_backward_compatibility():
    assert combined_handoff_ready("none", None, selected_count=1, saved_count=1)[0]


def test_disk_task_barrier_precedes_rag_handoff():
    from pathlib import Path
    script = (Path(__file__).parents[1] / "app/services/disk.py").read_text()
    start = script.index('appended = extract_pending_directories')
    assert start < script.index('combined_handoff_ready(', start) < script.index('should_continue_after_extract(', start)


def test_direct_rag_and_report_guarded_from_partial_manifest():
    from pathlib import Path
    app = Path(__file__).parents[1] / "app"
    task = (app / "tasks.py").read_text()
    rag = task[task.index("def rag_index_sync("):task.index("def phase3_shard_sync(")]
    assert "directory_extract_gate" in rag
    report = (app / "services/process_completion.py").read_text()
    assert "unfinished_directory_reason(db, job_id)" in report


def test_disk_ready_resume_holds_for_any_incomplete_directory():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/routers/jobs.py").read_text()
    assert "if directory_copy_is_live(row) or pending_reason:" in src


def test_unpublished_first_directory_blocks_all_downstream_work():
    from pathlib import Path
    src = (Path(__file__).parents[1] / "app/services/next_evidence_directory.py").read_text()
    assert 'len(registered) > 1 and not manifest.get("segment_paths")' in src
    assert 'pending = list(registered)' in src

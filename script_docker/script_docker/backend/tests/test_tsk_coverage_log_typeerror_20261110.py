"""Regression: coverage log expression must not call a string as a function."""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'app/services/extracted_disk.py'


def _coverage_message(coverage):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'write_disk_log'
             and len(n.args) >= 3
             and 'TSK source limitation:' in ast.unparse(n.args[2])]
    assert len(calls) == 1, f'Expected exactly one coverage event, found {len(calls)}'
    expression = ast.Expression(body=calls[0].args[2])
    ast.fix_missing_locations(expression)
    outstanding = bool(coverage['unreadable_directories'] or coverage['metadata_unresolved'])
    return eval(compile(expression, str(SOURCE), 'eval'), {'__builtins__': {}},
                {'enum_coverage': coverage, 'outstanding': outstanding})


def test_coverage_warning_unresolved_metadata():
    counts = dict(unreadable_directories=0, alias_directories_skipped=242,
                  metadata_missing=15882, metadata_recovered=50, metadata_unresolved=15832)
    msg = _coverage_message(counts)
    assert '15,882' not in msg  # existing log uses integer rendering
    assert '15882 metadata-missing entries' in msg
    assert '15832 unresolved' in msg
    assert 'analyst review of unresolved evidence required' in msg


def test_coverage_info_when_all_metadata_recovered():
    counts = dict(unreadable_directories=0, alias_directories_skipped=242,
                  metadata_missing=15882, metadata_recovered=15882, metadata_unresolved=0)
    msg = _coverage_message(counts)
    assert 'no unresolved metadata entries detected' in msg
    assert 'analyst review' not in msg


def test_coverage_warning_unreadable_directories_only():
    counts = dict(unreadable_directories=3, alias_directories_skipped=0,
                  metadata_missing=0, metadata_recovered=0, metadata_unresolved=0)
    assert 'analyst review of unresolved evidence required' in _coverage_message(counts)

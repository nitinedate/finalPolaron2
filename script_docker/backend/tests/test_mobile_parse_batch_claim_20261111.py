"""Focused isolated tests: no database or forensic payload is required."""
import ast
from collections import deque
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'app/services/forensic_serial_mobile.py'
TEXT = SOURCE.read_text()
TREE = ast.parse(TEXT)


def compile_helper(name, namespace):
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), namespace)
    return namespace[name]


def test_claim_batch_orders_earliest_priority_and_uses_skip_locked():
    queries = []
    def fetchall(db, sql, params):
        queries.append((sql, params))
        return [{'id': 'id-1', 'payload': [{'path':'a'}]}, {'id':'id-2', 'payload': [{'path':'b'}]}]
    claim = compile_helper('_claim_work_batch', {'fetchall': fetchall})
    rows = claim(None, 'stage-1', 'slot-1', 24)
    assert len(rows) == 2
    assert queries[0][1] == {'sid':'stage-1','worker':'slot-1','batch':24}
    assert 'ORDER BY priority, id' in queries[0][0]
    assert 'FOR UPDATE SKIP LOCKED' in queries[0][0]
    assert "status='running'" in queries[0][0]


def test_release_only_unstarted_claims():
    calls=[]
    class DB:
        def commit(self): calls.append('commit')
    def execute(db, sql, params): calls.append((sql,params))
    release = compile_helper('_release_unstarted_claims', {'execute': execute})
    release(DB(), 'stage-1', 'slot-2', [{'id':'id-2'}, {'id':'id-3'}])
    sql, params = calls[0]
    assert "status='running'" in sql
    assert "worker_id=:worker" in sql
    assert params['ids'] == ['id-2','id-3']
    assert calls[-1] == 'commit'


def test_no_release_when_no_outstanding_claims():
    def execute(*args): raise AssertionError('must not write')
    release = compile_helper('_release_unstarted_claims', {'execute': execute})
    release(None, 'stage', 'slot', [])


def test_batch_claim_survives_cancel_and_avoids_marking_unstarted_done():
    src=TEXT[TEXT.index('    def worker(slot):'):TEXT.index('    totals = {',TEXT.index('    def worker(slot):'))]
    assert 'claimed = deque()' in src
    assert 'claimed.popleft()' in src
    assert '_release_unstarted_claims(' in src
    assert 'wdb.commit()' in src
    assert 'context.clear_byte_cache()' in src
    assert 'if stage == "recovery"' in src


def test_existing_mobile_parser_coverage_and_sqlite_sidecars_retained():
    assert 'registry.route(item, context)' in TEXT
    assert 'registry.recovery_analyzers' in TEXT
    assert 'SIDECARS = ("-wal", "-shm", "-journal")' in TEXT
    assert 'get_plugin_registry()' in TEXT

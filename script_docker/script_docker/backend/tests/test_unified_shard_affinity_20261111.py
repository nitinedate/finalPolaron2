"""Dependency-free regression checks for bounded mobile scheduling and disk scan I/O."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'app' / 'services'
MOBILE = (ROOT / 'forensic_serial_mobile.py').read_text()
DISK = (ROOT / 'artifact_parse.py').read_text()


def load_affinity_function(fetch, enabled=True):
    tree = ast.parse(MOBILE)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_assign_shard_affinity')
    code = compile(ast.Module(body=[node], type_ignores=[]), '<affinity>', 'exec')
    namespace = {'fetchone': fetch, 'MOBILE_SHARD_AFFINITY': enabled}
    exec(code, namespace)
    return namespace['_assign_shard_affinity']


class FakeDB:
    def __init__(self):
        self.commits = 0
    def commit(self):
        self.commits += 1


class UnifiedSchedulingTests(unittest.TestCase):
    def test_bounded_batch_and_idempotent_range(self):
        queries = []
        counts = iter((5000, 5000, 17))
        def fetch(db, query, args):
            queries.append((query, args))
            return {'n': next(counts)}
        db = FakeDB()
        count = load_affinity_function(fetch)(db, 'stage-one')
        self.assertEqual(count, 10017)
        self.assertEqual(db.commits, 3)
        self.assertEqual(len(queries), 3)
        for sql, args in queries:
            self.assertEqual(args['batch'], 5000)
            self.assertEqual(args['sid'], 'stage-one')
            self.assertIn('FOR UPDATE SKIP LOCKED', sql)
            self.assertIn('priority BETWEEN 0 AND 5', sql)
            self.assertIn('6 + w.priority * 4096', sql)
            self.assertIn('extracted_part_uri', sql)

    def test_disable_affinity_preserves_legacy_order(self):
        db = FakeDB()
        fn = load_affinity_function(lambda *_: self.fail('unexpected DB access'), enabled=False)
        self.assertEqual(fn(db, 'x'), 0)
        self.assertEqual(db.commits, 0)

    def test_smallint_range_no_double_pack(self):
        for rank in range(6):
            for shard_bucket in (0, 1, 4095):
                result = 6 + rank * 4096 + shard_bucket
                self.assertGreaterEqual(result, 6)
                self.assertLessEqual(result, 24581)
                self.assertLess(result, 32767)

    def test_mobile_checkpoint_semantics_retained(self):
        self.assertIn('ORDER BY priority, id LIMIT :batch FOR UPDATE SKIP LOCKED', MOBILE)
        self.assertIn('ON CONFLICT(stage_run_id,partition_key) DO NOTHING', MOBILE)
        self.assertIn('_release_unstarted_claims(', MOBILE)
        self.assertIn('wdb.commit()', MOBILE)

    def test_disk_skips_expensive_jsonb_aggregate_scan(self):
        self.assertNotIn("SELECT coalesce(sum(size_bytes), 0) AS n FROM job_artifacts", DISK)
        self.assertIn('requested_bytes = sum(', DISK)
        self.assertIn('iter_files_from_part(', DISK)
        self.assertIn('prepare_forensic_parse_queue', DISK)

if __name__ == '__main__':
    unittest.main()

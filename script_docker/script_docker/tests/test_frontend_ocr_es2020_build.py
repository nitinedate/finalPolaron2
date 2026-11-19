"""Regression guard for the OCR status label on the ES2020 frontend target."""
from pathlib import Path
import json
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'frontend/src/pages/forensic/OcrDocumentsPage.tsx'
CONFIG = ROOT / 'frontend/tsconfig.json'


def test_ocr_status_label_is_es2020_compatible():
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    assert 'ES2020' in config['compilerOptions']['lib']
    page = PAGE.read_text(encoding='utf-8')
    assert 'job.status.replace(/_/g, " ")' in page
    assert 'job.status.replaceAll(' not in page


def test_status_label_expression_typechecks_with_es2020(tmp_path):
    tsc = shutil.which('tsc')
    if not tsc:
        import pytest
        pytest.skip('TypeScript compiler not installed in this environment')
    example = tmp_path / 'ocr_status.ts'
    example.write_text('''\
type JobStatus = "running" | "completed" | "queued" | "failed" | "receiving" | "completed_with_errors";
function statusLabel(status: JobStatus): string { return status.replace(/_/g, " "); }
const actual: string = statusLabel("completed_with_errors");
void actual;
''', encoding='utf-8')
    process = subprocess.run(
        [tsc, '--noEmit', '--target', 'ES2020', '--lib', 'ES2020', '--skipLibCheck', str(example)],
        cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=30, check=False,
    )
    assert process.returncode == 0, process.stdout

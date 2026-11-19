"""Offline OCR search and evidence-integrity regression tests (2026-11-19)."""
import hashlib
import json
import zipfile

import pytest

from app.services import document_search as ocr


def _state_queued(job_id):
    state = ocr.read_state(job_id)
    state['status'] = 'queued'
    ocr.write_state(job_id, state)


def test_exact_phrase_is_unicode_punctuation_and_whitespace_insensitive():
    assert ocr.match_document('ACME:  Invoice\nNo.  1234!', 'acme invoice no 1234', 'phrase')[0]
    assert not ocr.match_document('One two three', 'one three', 'phrase')[0]
    assert ocr.match_document('One two three', 'one three', 'all_words')[0]
    assert not ocr.match_document('One two three', 'one zebra', 'all_words')[0]
    assert ocr.match_document('One two three', 'one zebra', 'any_words')[0]


def test_relative_paths_block_escape():
    for value in ('../evidence.pdf', '/etc/passwd', r'C:\sensitive\file.pdf', 'a/../../bad.pdf', r'a:\x.pdf'):
        with pytest.raises(ValueError):
            ocr.clean_relative(value)
    assert ocr.clean_relative(r'docs\folder\report.pdf') == 'docs/folder/report.pdf'


def test_tenant_scoped_server_inbox_blocks_cross_firm_read(tmp_path, monkeypatch):
    root = tmp_path / 'ocr'
    allowed = root / 'inbox'
    alice = allowed / 'firm_alice' / 'project'
    bob = allowed / 'firm_bob' / 'secret'
    alice.mkdir(parents=True)
    bob.mkdir(parents=True)
    (alice / 'a.txt').write_text('a')
    (bob / 'b.txt').write_text('b')
    monkeypatch.setenv('OCR_DOCUMENT_WORK_ROOT', str(root))
    monkeypatch.setenv('OCR_DOCUMENT_SOURCE_ROOTS', str(allowed))
    assert ocr.allowed_source_path(str(alice), tenant='firm_alice') == alice
    with pytest.raises(ValueError, match='another tenant'):
        ocr.allowed_source_path(str(bob), tenant='firm_alice')
    with pytest.raises(ValueError):
        ocr.allowed_source_path(str(tmp_path), tenant='firm_alice')


def test_search_zip_then_delete_unmatched_copies_does_not_touch_origins(tmp_path, monkeypatch):
    root = tmp_path / 'ocr'
    inbox = root / 'inbox' / 'firm_alpha' / 'batch'
    inbox.mkdir(parents=True)
    (inbox / 'agreement.txt').write_text('Signed Agreement: Service Contract No. 7001')
    (inbox / 'notes.txt').write_text('Unrelated notes about service delivery')
    (inbox / 'unreadable.png').write_bytes(b'not really an image')
    monkeypatch.setenv('OCR_DOCUMENT_WORK_ROOT', str(root))
    monkeypatch.setenv('OCR_DOCUMENT_SOURCE_ROOTS', str(root / 'inbox'))
    job_id, _ = ocr.new_job(tenant='firm_alpha', owner='user', query='contract no 7001',
                            match_mode='phrase', source_path=str(inbox))
    _state_queued(job_id)
    results = ocr.scan_job(job_id)
    assert results['status'] == 'completed_with_errors'
    assert results['matched'] == 1
    assert results['errors'] == 1  # bad image must NOT be treated as nonmatching
    assert results['done'] == results['total'] == 3
    with zipfile.ZipFile(ocr.job_folder(job_id) / 'matches.zip') as archive:
        assert sorted(archive.namelist()) == ['OCR_SEARCH_MANIFEST.json', 'agreement.txt']
        assert 'Contract No. 7001' in archive.read('agreement.txt').decode()
        manifest = json.loads(archive.read('OCR_SEARCH_MANIFEST.json'))
        assert manifest['matched'] == 1
        assert manifest['originals_untouched'] is True
    deleted = ocr.delete_unmatched_copies(job_id)
    assert deleted['purged'] is True
    assert len(deleted['removed_workspace_copies']) == 1
    assert (inbox / 'notes.txt').exists()  # Original unaffected
    assert (inbox / 'unreadable.png').exists()  # Failed OCR not deleted
    assert not (ocr.job_folder(job_id) / 'sources' / 'notes.txt').exists()
    assert (ocr.job_folder(job_id) / 'sources' / 'unreadable.png').exists()
    assert (ocr.job_folder(job_id) / 'sources' / 'agreement.txt').exists()
    assert ocr.delete_unmatched_copies(job_id)['purged']  # repeated request idempotent


def test_search_uploaded_snapshot_without_exposing_originals(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_DOCUMENT_WORK_ROOT', str(tmp_path / 'ocr'))
    job_id, _ = ocr.new_job(tenant='firm_alpha', owner='user', query='requested phrase', match_mode='phrase')
    folder = ocr.job_folder(job_id) / 'sources'
    (folder / 'one.txt').write_text('This contains the requested phrase.')
    (folder / 'two.csv').write_text('This does not contain it.')
    _state_queued(job_id)
    state = ocr.scan_job(job_id)
    assert state['status'] == 'completed'
    assert state['matched'] == 1
    assert state['errors'] == 0
    assert sorted(x['path'] for x in state['results']) == ['one.txt', 'two.csv']
    assert state['results'][0]['sha256'] == hashlib.sha256((folder / 'one.txt').read_bytes()).hexdigest()


def test_reads_word_xml_without_macro_execution(tmp_path):
    path = tmp_path / 'invoice.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml',
                         '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Vendor 6789 Statement</w:t>'
                         '</w:r></w:p></w:body></w:document>')
    text, engine = ocr.extract_text(path)
    assert 'Vendor 6789' in text
    assert engine == 'office-xml'


def test_pdf_native_text_skips_ocr_engine(tmp_path, monkeypatch):
    import fitz
    pdf = fitz.open()
    page = pdf.new_page()
    line = 'Invoice number 7788 with a long native-searchable paragraph written directly into the PDF. '
    page.insert_text((50, 60), line * 2, fontsize=9)
    path = tmp_path / 'native.pdf'
    pdf.save(path)
    pdf.close()
    monkeypatch.setattr(ocr, '_recognize_many', lambda images: (_ for _ in ()).throw(AssertionError('OCR not needed')))
    result, engine = ocr.extract_text(path)
    assert 'Invoice number 7788' in result
    assert engine == 'pymupdf-text'


def test_pdf_with_small_footer_and_full_page_image_uses_ocr(tmp_path, monkeypatch):
    import fitz
    from PIL import Image
    from io import BytesIO
    image = Image.new('RGB', (420, 200), 'white')
    contents = BytesIO()
    image.save(contents, format='PNG')
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((20, 25), 'Page 1')
    page.insert_image(fitz.Rect(20, 40, 440, 240), stream=contents.getvalue())
    path = tmp_path / 'scanned.pdf'
    pdf.save(path)
    pdf.close()
    calls = []
    def fake_ocr(images):
        calls.append(len(images))
        return ['The needle is in this scanned document'] * len(images)
    monkeypatch.setattr(ocr, '_recognize_many', fake_ocr)
    result, engine = ocr.extract_text(path)
    assert 'needle' in result
    assert calls == [1]
    assert 'surya-or-tesseract' in engine


def test_server_inbox_quarantine_is_opt_in_recoverable_and_keeps_unreadable(tmp_path, monkeypatch):
    root = tmp_path / 'ocr'
    inbox = root / 'inbox' / 'firm_alpha' / 'batch_001'
    inbox.mkdir(parents=True)
    (inbox / 'hit.txt').write_text('We found the phrase that is needed')
    (inbox / 'miss.txt').write_text('Something else entirely')
    (inbox / 'bad.png').write_bytes(b'not-a-real-image')
    monkeypatch.setenv('OCR_DOCUMENT_WORK_ROOT', str(root))
    monkeypatch.setenv('OCR_DOCUMENT_SOURCE_ROOTS', str(root / 'inbox'))
    jid, _ = ocr.new_job(tenant='firm_alpha', owner='owner1', query='phrase that is needed',
                         match_mode='phrase', source_path=str(inbox))
    _state_queued(jid)
    state = ocr.scan_job(jid)
    assert state['matched'] == 1 and state['errors'] == 1
    assert not ocr.can_quarantine_source(state)
    with pytest.raises(ValueError, match='only an approved|Only an approved'):
        ocr.quarantine_unmatched_in_inbox(jid)
    monkeypatch.setenv('OCR_ALLOW_INBOX_PRUNE', 'true')
    assert ocr.can_quarantine_source(state)
    outcome = ocr.quarantine_unmatched_in_inbox(jid)
    assert outcome['source_pruned'] is True
    assert len(outcome['quarantined_source_files']) == 1
    assert (inbox / 'hit.txt').exists()
    assert (inbox / 'bad.png').exists()
    assert not (inbox / 'miss.txt').exists()
    archived = root / 'quarantine' / 'firm_alpha' / jid / 'miss.txt'
    assert archived.read_text() == 'Something else entirely'
    assert (root / 'quarantine' / 'firm_alpha' / jid / 'QUARANTINE_MANIFEST.json').is_file()
    assert len(ocr.quarantine_unmatched_in_inbox(jid)['quarantined_source_files']) == 1


def test_source_changed_after_search_cannot_be_quarantined(tmp_path, monkeypatch):
    root = tmp_path / 'ocr'
    inbox = root / 'inbox' / 'firm_alpha' / 'batch_002'
    inbox.mkdir(parents=True)
    (inbox / 'nonmatch.txt').write_text('unrelated')
    monkeypatch.setenv('OCR_DOCUMENT_WORK_ROOT', str(root))
    monkeypatch.setenv('OCR_DOCUMENT_SOURCE_ROOTS', str(root / 'inbox'))
    monkeypatch.setenv('OCR_ALLOW_INBOX_PRUNE', 'true')
    jid, _ = ocr.new_job(tenant='firm_alpha', owner='owner', query='needle',
                         match_mode='phrase', source_path=str(inbox))
    _state_queued(jid)
    assert ocr.scan_job(jid)['status'] == 'completed'
    (inbox / 'nonmatch.txt').write_text('changed after search')
    with pytest.raises(ValueError, match='checksum changed'):
        ocr.quarantine_unmatched_in_inbox(jid)
    assert (inbox / 'nonmatch.txt').read_text() == 'changed after search'


def test_interrupted_server_snapshot_can_resume_without_duplicate_results(tmp_path, monkeypatch):
    root = tmp_path / 'ocr'
    source = root / 'inbox' / 'firm_a' / 'project'
    source.mkdir(parents=True)
    (source / 'a.txt').write_text('needle in the paragraph')
    (source / 'b.txt').write_text('other')
    monkeypatch.setenv('OCR_DOCUMENT_WORK_ROOT', str(root))
    monkeypatch.setenv('OCR_DOCUMENT_SOURCE_ROOTS', str(root / 'inbox'))
    jid, _ = ocr.new_job(tenant='firm_a', owner='owner', query='needle',
                         match_mode='phrase', source_path=str(source))
    _state_queued(jid)
    copy_dir = ocr.job_folder(jid) / 'sources'
    (copy_dir / 'a.txt').write_text('needle in the paragraph')
    (copy_dir / 'b.txt.ocr-partial').write_bytes(b'partial old write')
    # Pretend a worker crashed after counting the first source.
    state = ocr.read_state(jid)
    state.update({'status': 'running', 'done': 1, 'matched': 1,
                  'results': [{'path': 'a.txt', 'matched': True}]})
    ocr.write_state(jid, state)
    finished = ocr.scan_job(jid)
    assert finished['status'] == 'completed'
    assert finished['total'] == finished['done'] == 2
    assert finished['matched'] == 1
    assert sorted(row['path'] for row in finished['results']) == ['a.txt', 'b.txt']
    assert (copy_dir / 'b.txt').read_text() == 'other'
    assert not (copy_dir / 'b.txt.ocr-partial').exists()

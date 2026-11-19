"""OCR document search with immutable intake copies and a separate Celery lane.

This is NOT an evidence deletion tool: the originals are never modified. All
removal is limited to user-confirmed, job-owned OCR workspace copies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import Lock
from xml.etree import ElementTree

SUPPORTED = frozenset({
    '.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp',
    '.docx', '.xlsx', '.pptx', '.txt', '.md', '.csv', '.tsv', '.json',
    '.xml', '.html', '.htm', '.log', '.eml',
})
IMAGE = frozenset({'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'})
OFFICE = frozenset({'.docx', '.xlsx', '.pptx'})
TEXT = SUPPORTED - IMAGE - OFFICE - {'.pdf'}
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_FILES = 3000
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_STATE_LOCK = Lock()


def work_root() -> Path:
    return Path(os.environ.get('OCR_DOCUMENT_WORK_ROOT') or '/app/data/ocr_search').resolve()


def job_folder(job_id: str) -> Path:
    if not re.fullmatch(r'[a-f0-9]{32}', str(job_id)):
        raise ValueError('Invalid OCR job id')
    return work_root() / 'jobs' / job_id


def clean_relative(raw: str) -> str:
    """No absolute paths, drive names, dot-dot or symlink traversal."""
    raw = str(raw or '').replace('\\', '/')
    parts = PurePosixPath(raw).parts
    if (not parts or raw.startswith('/') or re.match(r'^[A-Za-z]:', raw)
            or any(part in {'.', '..', ''} for part in parts)
            or any(':' in part or '\x00' in part for part in parts)):
        raise ValueError('Invalid document relative path')
    return '/'.join(parts)


def allowed_source_path(raw: str, *, tenant: str | None = None) -> Path:
    """Server path intake is explicitly allowlisted; never scan arbitrary host/evidence paths."""
    root = work_root()
    allowed = os.environ.get('OCR_DOCUMENT_SOURCE_ROOTS', str(root / 'inbox'))
    candidate = Path(raw).expanduser().resolve(strict=True)
    if not candidate.is_dir() or candidate.is_symlink():
        raise ValueError('OCR source must be an existing directory')
    for item in allowed.split(os.pathsep):
        if not item.strip():
            continue
        base = Path(item.strip()).expanduser().resolve(strict=False)
        if candidate == base or base in candidate.parents:
            if (root / 'jobs') == candidate or (root / 'jobs') in candidate.parents:
                raise ValueError('Cannot treat an OCR job directory as a new source')
            # Firm-to-firm isolation: even a user allowed to run OCR can only
            # select their own tenant subdirectory under every approved root.
            if tenant:
                if not re.fullmatch(r'[a-zA-Z0-9_]+', tenant):
                    raise ValueError('Invalid tenant schema identifier')
                own = (base / tenant).resolve(strict=False)
                if candidate != own and own not in candidate.parents:
                    raise ValueError('Server folder belongs to another tenant; use your tenant OCR inbox')
            return candidate
    raise ValueError('Source directory is not under OCR_DOCUMENT_SOURCE_ROOTS; use folder upload instead')


def _state_path(job_id: str) -> Path:
    return job_folder(job_id) / 'state.json'


def read_state(job_id: str) -> dict:
    with _state_path(job_id).open('r', encoding='utf-8') as stream:
        return json.load(stream)


def write_state(job_id: str, state: dict) -> None:
    """Atomic JSON snapshots survive API restarts; caller owns logical updates."""
    target = _state_path(job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    state['updated_at'] = datetime.now(timezone.utc).isoformat()
    fd, temp_name = tempfile.mkstemp(prefix='.status-', suffix='.tmp', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def new_job(*, tenant: str, owner: str, query: str, match_mode: str, source_path: str = '') -> tuple[str, dict]:
    if match_mode not in {'phrase', 'all_words', 'any_words'}:
        raise ValueError('Use phrase, all_words or any_words')
    if not query.strip() or len(query) > 1000:
        raise ValueError('Enter up to 1,000 characters of words or a sentence')
    job_id = uuid.uuid4().hex
    folder = job_folder(job_id)
    (folder / 'sources').mkdir(parents=True, exist_ok=False)
    state = {'id': job_id, 'tenant': str(tenant), 'owner': str(owner),
             'query': query.strip(), 'match_mode': match_mode, 'source_path': source_path,
             'status': 'receiving', 'total': 0, 'done': 0, 'matched': 0,
             'errors': 0, 'results': [], 'failures': [], 'purged': False,
             'created_at': datetime.now(timezone.utc).isoformat()}
    write_state(job_id, state)
    return job_id, state


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_office(path: Path) -> str:
    """Read searchable Office XML without invoking external conversions/macros."""
    strings = []
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 20000:
            raise ValueError('Office package exceeds safe entry count')
        total_xml_bytes = 0
        for info in archive.infolist():
            if info.file_size > 48 * 1024 * 1024:
                continue
            name = info.filename
            keep = (name.startswith('word/') and name.endswith('.xml') and '/_rels/' not in name)
            keep |= (name.startswith('ppt/slides/slide') and name.endswith('.xml'))
            keep |= (name.startswith('xl/worksheets/') and name.endswith('.xml'))
            keep |= (name in {'xl/sharedStrings.xml', 'xl/workbook.xml'})
            if not keep or '..' in PurePosixPath(name).parts:
                continue
            total_xml_bytes += info.file_size
            if total_xml_bytes > 128 * 1024 * 1024:
                raise ValueError('Office document exceeds safe XML expansion budget')
            try:
                node = ElementTree.fromstring(archive.read(info))
                strings.extend(x.text for x in node.iter()
                               if x.text and x.tag.rsplit('}', 1)[-1] in {'t', 'v', 'f'})
            except (ElementTree.ParseError, ValueError):
                continue
    return '\n'.join(strings)


def _tesseract_image(pil_image) -> str:
    from app.services.ocr_gpu import _tesseract_image as tess
    return tess(pil_image)[0]


def extract_text(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext in TEXT:
        return path.read_text(encoding='utf-8', errors='replace'), 'native-text'
    if ext in OFFICE:
        return _read_office(path), 'office-xml'
    if ext == '.pdf':
        # Native PDF text is many times faster and usually more accurate than
        # rasterizing everything. Only rasterize pages with little/no native
        # text and visible embedded images, then batch Surya requests.
        import fitz
        from PIL import Image
        result = []
        ocr_indices = []
        ocr_images = []
        engines = {'pymupdf-text'}
        def flush_ocr() -> None:
            if not ocr_images:
                return
            recognized = _recognize_many(ocr_images)
            for index, text in zip(ocr_indices, recognized):
                result[index] += '\n' + text
            ocr_indices.clear()
            ocr_images.clear()
            engines.add('surya-or-tesseract')
        with fitz.open(path) as pdf:
            if pdf.needs_pass:
                raise RuntimeError('Password-protected PDF; provide an authorized unlocked copy')
            if len(pdf) > 3000:
                raise ValueError('PDF exceeds safe 3000-page limit')
            for page in pdf:
                native = page.get_text('text') or ''
                result.append(native)
                # Some scans contain a small digitally printed page number or
                # watermark; that should not suppress OCR of the main image.
                needs_ocr = len(native.strip()) < 120 and (not native.strip() or bool(page.get_images(full=True)))
                if not needs_ocr:
                    continue
                if float(page.rect.width) * float(page.rect.height) * (1.6 ** 2) > 20_000_000:
                    raise ValueError('PDF raster page exceeds safe OCR pixel budget')
                pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
                image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                ocr_indices.append(len(result) - 1)
                ocr_images.append(image)
                if len(ocr_images) >= 8:
                    flush_ocr()
            flush_ocr()
        return '\n'.join(result), '+'.join(sorted(engines))
    if ext in IMAGE:
        from PIL import Image
        with Image.open(path) as opened:
            image = opened.convert('RGB')
        return _recognize(image), 'surya-or-tesseract'
    raise ValueError('Unsupported document type')


def _recognize_many(images: list) -> list[str]:
    """Batch Surya for scanned PDF pages; fall back to Tesseract page by page."""
    if (os.environ.get('OCR_ENGINE') or 'surya').lower() == 'surya':
        try:
            from app.services.ocr_gpu import _surya_ocr_images
            results = _surya_ocr_images(images, prompt='Text Recognition:')
            if len(results) == len(images) and all(text.strip() for text, _ in results):
                return [text for text, _ in results]
        except Exception:
            pass
    return [_recognize(image) for image in images]


def _recognize(image) -> str:
    """Prefer configured Surya inference; fall back to local Tesseract.

    A missing OCR engine is an ERROR, never a false 'no match'.
    """
    import os
    engine = (os.environ.get('OCR_ENGINE') or 'surya').lower()
    failure = None
    if engine == 'surya':
        try:
            from app.services.ocr_gpu import _surya_ocr_image
            text, _confidence = _surya_ocr_image(image, prompt='Text Recognition:')
            if text.strip():
                return text
        except Exception as exc:
            failure = exc
    try:
        return _tesseract_image(image)
    except Exception as exc:
        raise RuntimeError(f'OCR engine failed (Surya={failure}; Tesseract={exc})') from exc


def normalized(value: str) -> str:
    """Unicode, whitespace and punctuation folding for sentence matching."""
    value = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(re.findall(r'\w+', value, flags=re.UNICODE))


def match_document(text: str, query: str, mode: str) -> tuple[bool, str]:
    hay = normalized(text)
    needle = normalized(query)
    if not needle or not hay:
        return False, ''
    if mode == 'phrase':
        matched = f' {needle} ' in f' {hay} '
        position = hay.find(needle)
    else:
        tokens = needle.split(' ')
        found = set(hay.split(' '))
        matched = all(t in found for t in tokens) if mode == 'all_words' else any(t in found for t in tokens)
        position = next((hay.find(t) for t in tokens if t in found), -1)
    snippet = hay[max(0, position - 90): position + 230] if position >= 0 else ''
    return matched, snippet


def _source_files(folder: Path) -> list[tuple[str, Path]]:
    root = folder.resolve()
    out = []
    total_bytes = 0
    for child in folder.rglob('*'):
        # No symbolic links or special files, even inside allowlisted directories.
        if child.is_symlink() or not child.is_file():
            continue
        if child.suffix.lower() not in SUPPORTED:
            continue
        if child.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f'{child.name}: document exceeds 200 MB')
        total_bytes += child.stat().st_size
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('OCR job exceeds 4 GiB total size budget')
        if root not in child.resolve().parents:
            raise ValueError('Source includes a file outside the chosen directory')
        out.append((child.relative_to(folder).as_posix(), child))
        if len(out) > MAX_FILES:
            raise ValueError('OCR job exceeds document count limit')
    return sorted(out)


def _copy_source_folder(src: Path, target: Path) -> None:
    docs = _source_files(src)
    if not docs:
        raise ValueError('No supported documents found under the selected source directory')
    for relative, old in docs:
        clean_relative(relative)
        new = target / relative
        new.parent.mkdir(parents=True, exist_ok=True)
        if new.exists():
            if new.is_symlink() or not new.is_file() or hash_file(old) != hash_file(new):
                raise IOError(f'Snapshot has conflicting or changed bytes: {relative}')
            continue  # Complete verified copy from a previous interrupted run.
        partial = new.with_name(new.name + '.ocr-partial')
        if partial.is_symlink():
            raise ValueError(f'Cannot resume symlinked OCR snapshot: {relative}')
        partial.unlink(missing_ok=True)  # Incomplete temp copy is not evidence.
        initial = old.stat()
        try:
            with old.open('rb') as source, partial.open('xb') as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            end = old.stat()
            if (initial.st_size != end.st_size or initial.st_mtime_ns != end.st_mtime_ns
                    or end.st_size != partial.stat().st_size):
                raise IOError(f'Source file changed during snapshot: {relative}')
            os.replace(partial, new)
        finally:
            partial.unlink(missing_ok=True)


def scan_job(job_id: str) -> dict:
    """Celery worker owns one job. Thread-pool processes documents, never other jobs."""
    folder = job_folder(job_id)
    # A duplicate Celery delivery must not run the same source copy and OCR
    # threads at once. Independent job IDs never share this lock.
    import fcntl
    with (folder / '.ocr-job.lock').open('a+b') as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return read_state(job_id)
        try:
            return _scan_job_locked(job_id)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _scan_job_locked(job_id: str) -> dict:
    folder = job_folder(job_id)
    state = read_state(job_id)
    if state['status'] not in {'queued', 'running'}:
        return state
    # A previously interrupted task may have persisted part of its counters;
    # reprocess the intact snapshots once rather than double-counting results.
    state.update({'done': 0, 'matched': 0, 'errors': 0, 'total': 0,
                  'results': [], 'failures': []})
    state['status'] = 'running'
    write_state(job_id, state)
    try:
        if state.get('source_path'):
            src = allowed_source_path(str(state['source_path']), tenant=str(state['tenant']))
            _copy_source_folder(src, folder / 'sources')
        files = _source_files(folder / 'sources')
        if not files:
            raise ValueError('No supported documents were submitted')
        state['total'] = len(files)
        write_state(job_id, state)
        from app.services.cpu_budget import effective_cpu_count, target_parallel_cores
        workers = max(1, min(4, int(os.environ.get('OCR_DOCUMENT_THREADS', '2')),
                             target_parallel_cores(effective_cpu_count())))

        def one(item: tuple[str, Path]) -> dict:
            relative, path = item
            digest = hash_file(path)
            try:
                text, engine = extract_text(path)
                ok, snippet = match_document(text, state['query'], state['match_mode'])
                return {'path': relative, 'sha256': digest, 'matched': ok,
                        'snippet': snippet if ok else '', 'engine': engine}
            except Exception as exc:
                return {'path': relative, 'sha256': digest, 'matched': None,
                        'error': str(exc)[:400], 'engine': 'failed'}

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ocr-document') as pool:
            futures = [pool.submit(one, item) for item in files]
            for future in as_completed(futures):
                row = future.result()
                state['results'].append(row)
                state['done'] += 1
                if row['matched'] is True:
                    state['matched'] += 1
                elif row['matched'] is None:
                    state['errors'] += 1
                write_state(job_id, state)
        state['status'] = 'completed' if not state['errors'] else 'completed_with_errors'
        state['results'].sort(key=lambda x: x['path'])
        zip_matches(job_id, state)
    except Exception as exc:
        state['status'] = 'failed'
        state['error'] = str(exc)[:600]
    write_state(job_id, state)
    return state


def zip_matches(job_id: str, state: dict | None = None) -> Path:
    state = state or read_state(job_id)
    folder = job_folder(job_id)
    target = folder / 'matches.zip'
    temp = folder / 'matches.partial.zip'
    with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
        for result in state.get('results') or []:
            if result['matched'] is not True:
                continue
            relative = clean_relative(result['path'])
            source = folder / 'sources' / relative
            if not source.is_file() or source.is_symlink():
                raise FileNotFoundError(relative)
            if hash_file(source) != result['sha256']:
                raise IOError(f'Snapshot checksum changed before packaging: {relative}')
            archive.write(source, arcname=relative)
        archive.writestr('OCR_SEARCH_MANIFEST.json', json.dumps({
            'id': job_id, 'query': state['query'], 'match_mode': state['match_mode'],
            'matched': state['matched'], 'total': state['total'], 'errors': state['errors'],
            'results': state['results'], 'originals_untouched': True,
        }, indent=2))
    os.replace(temp, target)
    return target


def delete_unmatched_copies(job_id: str) -> dict:
    """Only remove copied, definitely nonmatching OCR workspace items."""
    with _STATE_LOCK:
        state = read_state(job_id)
        if state['status'] not in {'completed', 'completed_with_errors'}:
            raise ValueError('OCR job must finish before removing unmatched copies')
        if state.get('purged'):
            return state
        removed = []
        for row in state['results']:
            if row.get('matched') is not False:
                continue  # errors stay available for manual review
            rel = clean_relative(row['path'])
            target = job_folder(job_id) / 'sources' / rel
            if target.is_symlink() or not target.is_file():
                continue
            if hash_file(target) != row['sha256']:
                raise IOError(f'Snapshot checksum changed: {rel}')
            target.unlink()
            removed.append({'path': rel, 'sha256': row['sha256']})
        state['purged'] = True
        state['removed_workspace_copies'] = removed
        write_state(job_id, state)
        return state


def can_quarantine_source(state: dict) -> bool:
    """Only an explicitly enabled, tenant-scoped *disposable* OCR inbox is mutable.

    Never move/delete a forensic evidence image, general server folder,
    browser-uploaded source, or path outside the dedicated OCR inbox.
    """
    if os.environ.get('OCR_ALLOW_INBOX_PRUNE', 'false').lower() not in {'true', '1', 'yes'}:
        return False
    if not str(state.get('source_path') or ''):
        return False
    tenant = str(state.get('tenant') or '')
    if not re.fullmatch(r'[a-zA-Z0-9_]+', tenant):
        return False
    try:
        source = allowed_source_path(str(state['source_path']), tenant=tenant)
        approved = (work_root() / 'inbox' / tenant).resolve(strict=False)
        return source != approved and approved in source.parents
    except (ValueError, OSError):
        return False


def quarantine_unmatched_in_inbox(job_id: str) -> dict:
    """Move verified nonmatches out of a disposable OCR server inbox, reversibly.

    This operation deliberately never destroys the originals. The selected
    directory is pruned, but the source bytes and their SHA-256 are preserved
    under OCR quarantine for administrator-led restoration or retention.
    """
    with _STATE_LOCK:
        state = read_state(job_id)
        if state.get('status') not in {'completed', 'completed_with_errors'}:
            raise ValueError('OCR search must finish before pruning a source inbox')
        if not can_quarantine_source(state):
            raise ValueError('Only an approved OCR tenant inbox may be pruned, with OCR_ALLOW_INBOX_PRUNE=true')
        if not (job_folder(job_id) / 'matches.zip').is_file():
            raise ValueError('Download package has not been generated; cannot prune source')
        root = allowed_source_path(str(state['source_path']), tenant=str(state['tenant']))
        quarantine = work_root() / 'quarantine' / str(state['tenant']) / job_id
        quarantine.mkdir(parents=True, exist_ok=True)
        moved = list(state.get('quarantined_source_files') or [])
        moved_paths = {row['path'] for row in moved}
        for row in state.get('results') or []:
            if row.get('matched') is not False:
                continue  # Unreadable/failed files are never interpreted as safe-to-prune.
            relative = clean_relative(row['path'])
            if relative in moved_paths:
                continue
            original = root / relative
            destination = quarantine / relative
            if destination.is_symlink() or original.is_symlink():
                raise ValueError(f'Symbolic link encountered: {relative}; no source pruned')
            if not original.is_file() or root not in original.resolve().parents:
                raise ValueError(f'Original source file unavailable or changed: {relative}')
            if hash_file(original) != row['sha256']:
                raise ValueError(f'Source checksum changed since OCR: {relative}; leaving source unchanged')
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ValueError(f'Quarantine collision: {relative}')
            os.replace(original, destination)
            if hash_file(destination) != row['sha256']:
                raise IOError(f'Quarantined file checksum mismatch: {relative}')
            moved.append({'path': relative, 'sha256': row['sha256']})
            moved_paths.add(relative)
            state['quarantined_source_files'] = moved
            write_state(job_id, state)  # durable after each safely moved item
        state['source_pruned'] = True
        state['quarantined_source_files'] = moved
        write_state(job_id, state)
        _write_json = quarantine / 'QUARANTINE_MANIFEST.json'
        _write_json.write_text(json.dumps({
            'job_id': job_id, 'tenant': state['tenant'], 'source_path': str(root),
            'files': moved, 'method': 'atomic_move', 'original_bytes_preserved': True,
        }, indent=2), encoding='utf-8')
        return state

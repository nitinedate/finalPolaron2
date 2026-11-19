"""Protected OCR document search. Originals are never deleted or modified."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.deps import CurrentUser, require_firm_permission
from app.services import document_search as search

router = APIRouter(prefix='/api/ocr', tags=['ocr-documents'])


def _verify(job_id: str, current: CurrentUser) -> dict:
    try:
        data = search.read_state(job_id)
    except (OSError, ValueError):
        raise HTTPException(404, 'OCR job not found')
    if data.get('tenant') != str(current.schema_name):
        raise HTTPException(404, 'OCR job not found')
    return data


@router.post('/jobs')
async def create_job(
    query: str = Form(...), match_mode: str = Form('phrase'),
    source_path: str = Form(''), files: list[UploadFile] = File(default=[]),
    current: CurrentUser = Depends(require_firm_permission('job:run')),
):
    if bool(files) == bool(source_path.strip()):
        raise HTTPException(400, 'Choose either uploaded files or an allowlisted server directory')
    try:
        verified = str(search.allowed_source_path(source_path, tenant=str(current.schema_name))) if source_path.strip() else ''
        if len(files) > search.MAX_FILES:
            raise ValueError('Too many files for one OCR search')
        job_id, state = search.new_job(
            tenant=str(current.schema_name), owner=str(current.user_id),
            query=query, match_mode=match_mode, source_path=verified,
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc))
    try:
        received_bytes = 0
        for item in files:
            relative = search.clean_relative(item.filename or '')
            if Path(relative).suffix.lower() not in search.SUPPORTED:
                continue
            target = search.job_folder(job_id) / 'sources' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with target.open('xb') as stream:
                while block := await item.read(1024 * 1024):
                    count += len(block)
                    received_bytes += len(block)
                    if count > search.MAX_FILE_BYTES:
                        raise ValueError(f'{relative}: file exceeds 200 MB')
                    if received_bytes > search.MAX_TOTAL_BYTES:
                        raise ValueError('OCR upload exceeds 4 GiB total size budget')
                    stream.write(block)
            await item.close()
        state['status'] = 'queued'
        search.write_state(job_id, state)
        from app.tasks import ocr_document_search_task
        ocr_document_search_task.apply_async(args=(job_id,), queue='ocr-documents')
        return {'id': job_id, 'status': 'queued'}
    except Exception as exc:
        shutil.rmtree(search.job_folder(job_id), ignore_errors=True)
        raise HTTPException(400, f'Could not queue OCR search: {exc}')


@router.get('/jobs/{job_id}')
def get_job(job_id: str, current: CurrentUser = Depends(require_firm_permission('job:read'))):
    state = _verify(job_id, current)
    # Never expose server paths or internal tenant identifiers.
    return {**{k: v for k, v in state.items() if k not in {'tenant', 'owner', 'source_path'}},
            'source_can_quarantine': search.can_quarantine_source(state)}


@router.get('/jobs/{job_id}/matches.zip')
def download_matches(job_id: str, current: CurrentUser = Depends(require_firm_permission('job:read'))):
    state = _verify(job_id, current)
    if state['status'] not in {'completed', 'completed_with_errors'}:
        raise HTTPException(409, 'Search is not complete')
    target = search.job_folder(job_id) / 'matches.zip'
    if not target.is_file():
        raise HTTPException(404, 'Match package not available')
    return FileResponse(target, media_type='application/zip', filename=f'ocr-matches-{job_id[:8]}.zip')


@router.post('/jobs/{job_id}/remove-unmatched')
def remove_unmatched(
    job_id: str,
    confirmation: str = Form(...),
    current: CurrentUser = Depends(require_firm_permission('job:run')),
):
    _verify(job_id, current)
    if confirmation != 'DELETE UNMATCHED COPIES':
        raise HTTPException(400, 'Type DELETE UNMATCHED COPIES to confirm deletion of OCR workspace copies')
    try:
        state = search.delete_unmatched_copies(job_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc))
    return {'ok': True, 'removed': len(state.get('removed_workspace_copies', [])),
            'originals_untouched': True}


@router.post('/jobs/{job_id}/quarantine-unmatched')
def quarantine_unmatched_source(
    job_id: str,
    confirmation: str = Form(...),
    current: CurrentUser = Depends(require_firm_permission('job:run')),
):
    state = _verify(job_id, current)
    if state.get('owner') != str(current.user_id) and not (current.is_firm_admin() or current.is_tenant_admin()):
        raise HTTPException(403, 'Only the OCR job owner or administrator may prune its source inbox')
    if confirmation != 'QUARANTINE UNMATCHED SOURCE FILES':
        raise HTTPException(400, 'Type QUARANTINE UNMATCHED SOURCE FILES to confirm')
    try:
        updated = search.quarantine_unmatched_in_inbox(job_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc))
    return {'ok': True, 'moved': len(updated.get('quarantined_source_files', [])),
            'quarantine_available_for_recovery': True}

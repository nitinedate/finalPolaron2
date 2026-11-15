#!/usr/bin/env python3
"""Conservatively migrate a Polaron .env to Surya-only OCR.

Dry-run by default. --apply creates a timestamped backup, preserves all other
lines (including secrets), and writes atomically with original file permissions.
Never prints secret values or touches acquired evidence.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone

DEPRECATED = re.compile(r'(?i)^(?:glm-ocr(?::[^,\s]*)?|zai-org/glm-ocr(?::[^,\s]*)?)$')

def migrated(text: str) -> tuple[str, list[str]]:
    changes=[]
    output=[]
    present=set()
    newline='\r\n' if '\r\n' in text else '\n'
    for line in text.splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            output.append(line); continue
        key, val = line.split('=', 1)
        k=key.strip()
        present.add(k)
        if k=='OCR_ENGINE':
            if val.strip().lower() != 'surya': changes.append('OCR_ENGINE -> surya')
            output.append(key+'=surya')
        elif k=='OCR_MODEL':
            if val.strip() != 'datalab-to/surya-ocr-2.5': changes.append('OCR_MODEL -> Surya model')
            output.append(key+'=datalab-to/surya-ocr-2.5')
        elif k in {'OLLAMA_PULL_MODELS','HF_PULL_MODELS'}:
            items=val.split(',')
            keep=[item for item in items if not DEPRECATED.fullmatch(item.strip())]
            if len(keep)!=len(items):changes.append(k+' removed obsolete model entry')
            output.append(key+'='+','.join(keep))
        elif k=='FORENSIC_MEDIA_VISION_MODEL' and DEPRECATED.fullmatch(val.strip()):
            changes.append('FORENSIC_MEDIA_VISION_MODEL cleared; set an independently validated vision model if required')
            output.append(key+'=')
        elif k=='OCR_GLM_MICRO_BATCH':
            changes.append('OCR_GLM_MICRO_BATCH renamed to SURYA_PAGE_BATCH')
            if 'SURYA_PAGE_BATCH' not in present and 'SURYA_PAGE_BATCH=' not in text:
                output.append('SURYA_PAGE_BATCH='+val)
            else: output.append('# removed obsolete OCR_GLM_MICRO_BATCH')
        else: output.append(line)
    for key,value in [('OCR_ENGINE','surya'),('OCR_MODEL','datalab-to/surya-ocr-2.5')]:
        if key not in present:
            output.append(key+'='+value);changes.append('added '+key)
    return newline.join(output)+newline,changes

def main() -> int:
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--env',default='.env',type=Path)
    a.add_argument('--apply',action='store_true')
    args=a.parse_args()
    f=args.env
    if not f.is_file():
        print('No .env at specified path; no changes made');return 2
    original=f.read_bytes()
    if b'\x00' in original:raise ValueError('Refusing to modify binary configuration')
    content=original.decode('utf-8-sig')
    changed,notes=migrated(content)
    if not notes:
        print('Configuration already uses Surya-only OCR; no changes needed');return 0
    print('Planned changes:')
    for note in notes: print(' -',note)
    if not args.apply:
        print('Dry run; use --apply to save a backup and make the changes')
        return 0
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=f.with_name(f.name+'.before-surya-'+stamp+'.bak')
    shutil.copy2(f,backup)
    fd,tmp=tempfile.mkstemp(prefix='.surya_',dir=f.parent)
    try:
        with os.fdopen(fd,'wb') as h:
            h.write(changed.encode('utf-8'));h.flush();os.fsync(h.fileno())
        os.chmod(tmp,f.stat().st_mode)
        os.replace(tmp,f)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    print('Updated .env. Backup saved beside it (contains existing secrets; protect it).')
    return 0

if __name__=='__main__':raise SystemExit(main())

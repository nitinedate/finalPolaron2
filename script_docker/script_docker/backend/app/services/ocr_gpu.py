"""Forensic OCR: native text, Surya vLLM and optional Tesseract."""

from __future__ import annotations

import io
import json
import logging
import multiprocessing
import os
import shutil
import threading
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

from app.config import get_settings
from app.db.sql_helpers import execute, fetchall, fetchone, is_retryable_db_error
from app.services.disk_build_log import write_disk_log

log = logging.getLogger("ocr_gpu")

_fitz_lock = threading.Lock()











def release_ocr_client_resources() -> bool:
    """Surya executes in an external service; this worker owns no OCR model weights."""
    return False


def _surya_ocr_images(images: list, *, prompt: str) -> list[tuple[str, float]]:
    """Recognize all pages in input order; failures stay retryable."""
    if selected_ocr_engine() != "surya":
        raise RuntimeError("Surya inference requested while OCR_ENGINE is not surya")
    from app.services.surya_adapter import recognize_pages
    results = recognize_pages(images)
    if len(results) != len(images):
        raise RuntimeError(f"Surya returned {len(results)} pages for {len(images)} inputs")
    return results


def _surya_ocr_image(image, *, prompt: str) -> tuple[str, float]:
    result = _surya_ocr_images([image], prompt=prompt)
    if len(result) != 1:
        raise RuntimeError("Surya returned an unexpected number of OCR pages")
    return result[0]


IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic"})
VIDEO_OCR_EXT = frozenset({".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm"})
# Poor-quality media/documents routed by the evidence filter carry this marker.
# They remain OCR candidates even when normal documents-only policy would park photos/videos.
OCR_QUALITY_BUCKET_SQL = "coalesce(metadata->>'ocr_quality_bucket','false') = 'true'"
# Office files are not added to the OCR queue. Text-only Word/Excel/PowerPoint
# never reaches Surya. A file that does arrive is handled in cpu_prepare: pictures
# only, and a document with no pictures is finished from its own text.
OFFICE_EXT = frozenset({".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx"})
DOCUMENT_EXT = frozenset({".pdf", *IMAGE_EXT})

OCR_ELIGIBLE_EXT_SQL = """
lower(coalesce(extension, '')) IN (
  '.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp', '.bmp', '.gif', '.heic'
)
"""
OCR_PENDING_CANDIDATE_SQL = f"({OCR_QUALITY_BUCKET_SQL})"
OCR_DOC_FOLDER_SQL = """
(
  lower(file_path) LIKE '%/documents/%'
  OR lower(file_path) LIKE '%whatsapp%/documents/%'
  OR lower(file_path) LIKE '%/download%'
  OR lower(file_path) LIKE '%/downloads/%'
  OR lower(file_path) LIKE '%/desktop/%'
)
"""
# Cache/thumbs/OS vendor / app chrome only — never Pictures, DCIM, screenshots, WhatsApp images.
OCR_NOISE_SQL = """
(
  lower(file_path) LIKE '%/cache/%'
  OR lower(file_path) LIKE '%/.cache/%'
  OR lower(file_path) LIKE '%/.thumbnails/%'
  OR lower(file_path) LIKE '%/thumbs/%'
  OR lower(file_path) LIKE '%thumbcache%'
  OR lower(file_path) LIKE '%thumbs.db'
  OR lower(file_path) LIKE '%glide_disk_cache%'
  OR lower(file_path) LIKE '%/emoji/%'
  OR lower(file_path) LIKE '%/stickers/%'
  OR lower(file_path) LIKE '%/sticker/%'
  OR lower(file_path) LIKE '%/appdata/%'
  OR lower(file_path) LIKE '%appdata/%'
  OR lower(file_path) LIKE '%accountpictures%'
  OR lower(file_path) LIKE '%/application data/%'
  OR lower(file_path) LIKE '%/service worker/%'
  OR lower(file_path) LIKE '%/code cache/%'
  OR lower(file_path) LIKE '%/gpucache/%'
  OR lower(file_path) LIKE '%/shadercache/%'
  OR lower(file_path) LIKE '%/inetcache/%'
  OR lower(file_path) LIKE '%/temporary internet%'
  OR lower(file_path) LIKE '%/teams/backgrounds/%'
  OR lower(file_path) LIKE '%/node_modules/%'
  OR lower(file_path) LIKE '%/temp/%'
  OR lower(file_path) LIKE '%/tmp/%'
  OR lower(file_path) LIKE '%program files%'
  OR lower(file_path) LIKE '%programdata/%'
  OR lower(file_path) LIKE '%windows/system32%'
  OR lower(file_path) LIKE '%windows/syswow64%'
  OR lower(file_path) LIKE '%windows/winsxs%'
  OR lower(file_path) LIKE '%windows/servicing%'
  OR lower(file_path) LIKE '%windows/systemapps%'
  OR lower(file_path) LIKE '%$recycle.bin%'
  OR lower(file_path) LIKE '%system volume information%'
  OR lower(file_path) LIKE '%/plug_ins/%'
  OR lower(file_path) LIKE '%/stamps/%'
  OR lower(file_path) LIKE '%adobe/reader%'
  OR lower(file_path) LIKE '%adobe/acrobat%'
  OR lower(file_path) LIKE '%/windows/fonts/%'
  OR lower(file_path) LIKE '%/windows/installer/%'
  OR lower(coalesce(extension, '')) = '.ico'
  OR (
    coalesce(size_bytes, 0) > 0 AND coalesce(size_bytes, 0) < 4096
    AND lower(coalesce(extension, '')) <> '.pdf'
  )
)
"""
_CACHE_OCR_MARKERS = (
    "/cache/",
    "/.cache/",
    "/.thumbnails/",
    "/thumbs/",
    "thumbcache",
    "thumbs.db",
    "glide_disk_cache",
    "/emoji/",
    "/stickers/",
    "/sticker/",
    "/appdata/",
    "/application data/",
    "/service worker/",
    "/code cache/",
    "/gpucache/",
    "/shadercache/",
    "/inetcache/",
    "/temporary internet",
    "/teams/backgrounds/",
    "/node_modules/",
    "/temp/",
    "/tmp/",
)

_PHOTO_PATH_MARKERS = (
    "/dcim/",
    "/camera/",
    "/camera roll/",
    "/pictures/",
    "/photos/",
    "/screenshots/",
    "whatsapp image",
    "whatsapp video",
    "/whatsapp%/media/",
)
_PHOTO_NAME_PREFIXES = ("img_", "dsc_", "pxl_", "dscn", "img-")
_OS_VENDOR_OCR_MARKERS = (
    "program files/",
    "program files (x86)/",
    "programdata/",
    "windows/system32",
    "windows/syswow64",
    "windows/winsxs",
    "windows/servicing",
    "windows/systemapps",
    "$recycle.bin",
    "system volume information",
    "/plug_ins/",
    "/stamps/",
    "adobe/reader",
    "adobe/acrobat",
)




def is_os_vendor_ocr_path(path: str) -> bool:
    """True for Windows/OS vendor trees that burn OCR with no case evidence."""
    lower = str(path or "").replace("\\", "/").lower()
    if not lower:
        return False
    return any(marker in lower for marker in _OS_VENDOR_OCR_MARKERS)


def is_photo_like_path(path: str) -> bool:
    """True for camera rolls / screenshots. Used only when OCR_DOCUMENTS_ONLY=true."""
    lower = str(path or "").replace("\\", "/").lower()
    if not lower or lower.endswith(".pdf"):
        return False
    name = lower.rsplit("/", 1)[-1]
    if any(marker in lower for marker in _PHOTO_PATH_MARKERS):
        return True
    if name.startswith(_PHOTO_NAME_PREFIXES):
        return True
    if "screenshot" in name or name.startswith("screen shot") or name.startswith("snip"):
        return True
    return False


def forensic_photos_allowed() -> bool:
    """Camera rolls are not OCR'd unless OCR_DOCUMENTS_ONLY is off.

    Surya is reserved for scanned PDFs and document-folder images with no
    extractable text. Pictures / DCIM / screenshots are skipped.
    """
    return not bool(getattr(get_settings(), "ocr_documents_only", True))


def is_document_folder_path(path: str) -> bool:
    lower = str(path or "").replace("\\", "/").lower()
    return (
        "/documents/" in lower
        or "/download" in lower
        or "/downloads/" in lower
        or "/desktop/" in lower
    )


def is_ocr_noise_path(path: str, *, size_bytes: int = 0, extension: str = "") -> bool:
    """True for cache, thumbs, icons, and OS vendor trees — not user pictures."""
    if is_os_vendor_ocr_path(path):
        return True
    lower = str(path or "").replace("\\", "/").lower()
    if any(marker in lower for marker in _CACHE_OCR_MARKERS):
        return True
    ext = (extension or "").lower()
    if not ext and "." in lower:
        ext = "." + lower.rsplit(".", 1)[-1]
    if ext == ".ico":
        return True
    if ext != ".pdf" and int(size_bytes or 0) > 0 and int(size_bytes or 0) < 4096:
        return True
    return False


def ocr_status_for_artifact(path: str, *, extension: str = "", size_bytes: int = 0) -> str:
    """pending / skipped / na for a newly parsed artifact."""
    ext = (extension or "").lower()
    if not ext.startswith(".") and ext:
        ext = f".{ext}"
    if not ext:
        name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
        if "." in name:
            ext = "." + name.rsplit(".", 1)[-1].lower()
    if ext not in DOCUMENT_EXT:
        return "na"
    if is_ocr_noise_path(path, size_bytes=size_bytes, extension=ext):
        return "skipped"
    if ext != ".pdf" and not forensic_photos_allowed() and not is_document_folder_path(path):
        return "skipped"
    return "pending"


def ocr_status_sql_for_parse_skip() -> str:
    """CASE expression used when bulk-skipping media from parse (keep OCR pending)."""
    keep = _ocr_eligible_keep_sql()
    return (
        f"CASE WHEN {keep} THEN 'pending' "
        f"WHEN {OCR_ELIGIBLE_EXT_SQL} THEN 'skipped' ELSE 'na' END"
    )


def prepare_ocr_image(img, *, max_edge: int = 1280):
    """Downscale for Surya and drop icons too small to be documents.

    Full-resolution phone photos make token generation much slower without
    improving forensic OCR quality.
    """
    try:
        width, height = img.size
    except Exception:
        return img
    if width < 80 or height < 80:
        return None
    edge = max(width, height)
    limit = max(int(max_edge or 1280), 480)
    if edge <= limit:
        return img
    scale = limit / float(edge)
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    try:
        from PIL import Image

        resample = getattr(Image, "Resampling", Image).BILINEAR
        return img.resize(new_size, resample)
    except Exception:
        return img.resize(new_size)


def _ocr_prompt_for_path(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".xls", ".xlsx", ".csv")):
        return "Table Recognition:"
    if "formula" in lower or lower.endswith((".tex",)):
        return "Formula Recognition:"
    return "Text Recognition:"


def _cuda_usable() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available() and getattr(torch.version, "cuda", None))
    except Exception:
        return False


def ocr_should_use_gpu() -> bool:
    """Whether the OCR agent should dispatch a GPU drain.

    Surya must not run on CPU (minutes per page). Text-layer PDFs stay on CPU
    inside the drain. This policy is evaluated on the huddle/supervisor
    worker (which may lack CUDA) as well as on worker-ocr-gpu, so it does
    not require a local GPU — the OCR worker verifies CUDA before load.
    """
    settings = get_settings()
    requested = str(getattr(settings, "ocr_device", None) or "auto").strip().lower()
    if requested in ("none", "off", "disabled"):
        return False
    if requested in ("cuda", "gpu", "auto", ""):
        return True
    # OCR_DEVICE=cpu: still take GPU when embeddings are not occupying it.
    if not bool(getattr(settings, "rag_embedding_enabled", False)):
        return True
    try:
        from app.services.job_locks import inspect_process_lanes

        lanes = inspect_process_lanes() or {}
        return int(lanes.get("gpu_slots_free") or 0) > 0
    except Exception:
        return True


def _resolve_ocr_device() -> tuple[str, bool]:
    if not ocr_should_use_gpu():
        return "cpu", False
    if _cuda_usable():
        return "cuda:0", True
    log.warning(
        "OCR agent wants CUDA but this process has no GPU — text-layer PDFs only here"
    )
    return "cpu", False




def _ocr_micro_batch_size() -> int:
    """How many page images share one Surya forward pass. Same model and token cap."""
    try:
        raw = int(os.environ.get("SURYA_PAGE_BATCH") or 4)
    except (TypeError, ValueError):
        raw = 4
    return max(1, min(raw, 8))












def _ocr_image_bytes(data: bytes, *, path: str) -> tuple[str, float]:
    prep = cpu_prepare_ocr_item(data, path=path, allow_photos=True)
    if prep.get("status") == "cpu_done":
        return str(prep.get("text") or ""), float(prep.get("conf") or 0.0)
    if prep.get("status") != "needs_gpu":
        return "", 0.0
    texts: list[str] = []
    confs: list[float] = []
    for seg in prep.get("segments") or []:
        if seg.get("type") == "text" and seg.get("text"):
            texts.append(str(seg["text"]))
            confs.append(0.90)
            continue
        img = seg.get("image")
        if img is None:
            continue
        text, conf = _surya_ocr_image(img, prompt=str(seg.get("prompt") or "Text Recognition:"))
        if text:
            texts.append(text)
            confs.append(conf)
    if not texts:
        return "", 0.0
    avg = sum(confs) / len(confs) if confs else 0.0
    return "\n\n".join(texts), avg


def _is_office_path(path: str) -> bool:
    lower = str(path or "").lower()
    return any(lower.endswith(ext) for ext in OFFICE_EXT)


def _xml_text(blob: bytes) -> str:
    import re

    raw = blob.decode("utf-8", errors="ignore")
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _office_plain_text(data: bytes, path: str, *, full: bool = False) -> str:
    """Pull the already-digital text out of a Word, Excel, or PowerPoint file."""
    lower = path.lower()
    try:
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            chosen: list[str] = []
            if lower.endswith((".docx", ".docm")):
                chosen = [n for n in names if n == "word/document.xml" or n.startswith("word/header")]
            elif lower.endswith((".xlsx", ".xlsm")):
                chosen = [n for n in names if n.startswith("xl/sharedStrings") or n.startswith("xl/worksheets/")]
            elif lower.endswith(".pptx"):
                chosen = [n for n in names if n.startswith("ppt/slides/slide")]
            parts = []
            for name in chosen if full else chosen[:30]:
                try:
                    parts.append(_xml_text(zf.read(name)))
                except Exception:
                    continue
            return "\n".join(p for p in parts if p)
    except Exception:
        return ""
    return ""


def _office_images(data: bytes) -> list:
    """Raster pictures embedded in an Office file. Text-only files return none."""
    from PIL import Image  # type: ignore

    images = []
    try:
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if not any(lower.startswith(prefix) for prefix in ("word/media/", "ppt/media/", "xl/media/")):
                    continue
                if lower.endswith("/"):
                    continue
                try:
                    img = Image.open(io.BytesIO(zf.read(name))).convert("RGB")
                except Exception:
                    continue
                images.append(img)
    except Exception:
        return []
    return images


def _prepare_office_ocr(data: bytes, *, path: str, full: bool = False) -> dict:
    """OCR only pictures inside Office files. Clear text with no pictures stays off the GPU."""
    text = _office_plain_text(data, path, full=full)
    images = _office_images(data)
    settings = get_settings()
    max_edge = int(getattr(settings, "ocr_max_image_edge", 1280) or 1280)
    ocr_segs = []
    for img in images:
        prepared = prepare_ocr_image(img, max_edge=max_edge)
        if prepared is None or not _raster_needs_actual_ocr(prepared):
            continue
        ocr_segs.append(
            {"type": "image", "image": prepared, "prompt": _ocr_prompt_for_path(path)}
        )
    if not ocr_segs:
        if _digital_text_usable(text):
            return {
                "status": "cpu_done",
                "path": path,
                "text": text,
                "conf": 0.90,
                "engine": "office-text",
                "segments": [],
            }
        return {
            "status": "skip",
            "path": path,
            "text": "",
            "conf": 0.0,
            "engine": "office-no-image",
            "segments": [],
        }
    segments = ([{"type": "text", "text": text}] if _digital_text_usable(text) else []) + ocr_segs
    return {
        "status": "needs_gpu",
        "path": path,
        "text": "",
        "conf": 0.0,
        "engine": "surya",
        "segments": segments,
    }


def _pdf_page_has_images(page) -> bool:
    try:
        if page.get_images():
            return True
    except Exception:
        pass
    try:
        info = page.get_text("dict") or {}
        for block in info.get("blocks") or []:
            if block.get("type") == 1:
                return True
    except Exception:
        pass
    return False


def _pdf_bytes_have_images(data: bytes, *, full: bool = False) -> bool:
    try:
        import fitz  # type: ignore

        with _fitz_lock:
            doc = fitz.open(stream=data, filetype="pdf")
            try:
                limit = len(doc) if full else min(len(doc), 12)
                for i in range(limit):
                    if _pdf_page_has_images(doc[i]):
                        return True
            finally:
                doc.close()
    except Exception:
        return False
    return False


def _digital_text_usable(text: str | None) -> bool:
    """True when a PDF already has extractable digital text — skip Surya."""
    t = (text or "").strip()
    if len(t) < 8:
        return False
    return sum(ch.isalnum() for ch in t) >= 8


def _is_clear_photograph(img) -> bool:
    """True for a color picture. Scanned pages are mostly gray paper and ink."""
    try:
        small = img.convert("RGB")
        small.thumbnail((48, 48))
        reader = getattr(small, "get_flattened_data", None) or small.getdata
        pixels = list(reader())
        if len(pixels) < 16:
            return False
        colorful = 0
        for r, g, b in pixels:
            if max(r, g, b) - min(r, g, b) > 28:
                colorful += 1
        return (colorful / float(len(pixels))) >= 0.38
    except Exception:
        return False


def _raster_needs_actual_ocr(img) -> bool:
    """True for scanned pages with no digital text.

    Blank pages and clear color photographs are skipped. Surya stays on
    document scans. Born-digital PDFs never reach here because
    `_digital_text_usable` already returned cpu_done.
    """
    if _is_clear_photograph(img):
        return False
    try:
        gray = img.convert("L")
        w, h = gray.size
        if w < 48 or h < 48:
            return False
        small = gray.copy()
        small.thumbnail((160, 160))
        extrema = small.getextrema()
        if extrema is None or extrema[1] - extrema[0] < 24:
            return False
        hist = small.histogram()
        pixels = float(sum(hist) or 1)
        if max(hist) / pixels > 0.92:
            return False
        return True
    except Exception:
        return True


def cpu_prepare_ocr_item(
    data: bytes | None,
    *,
    path: str,
    max_pages: int | None = None,
    allow_photos: bool = False,
) -> dict:
    """CPU-only OCR prep: text-layer PDFs, decode/downscale, render scan pages.

    Returns status skip | cpu_done | needs_gpu. Surya is never called here so a
    thread pool can run this while the GPU is busy on the previous document.
    """
    path = str(path or "")
    if not data:
        return {"status": "skip", "path": path, "text": "", "conf": 0.0, "engine": "na", "segments": []}
    if is_os_vendor_ocr_path(path):
        return {"status": "skip", "path": path, "text": "", "conf": 0.0, "engine": "os_vendor", "segments": []}
    if not allow_photos and is_photo_like_path(path):
        return {"status": "skip", "path": path, "text": "", "conf": 0.0, "engine": "photo", "segments": []}
    lower = path.lower()
    if _is_office_path(lower):
        return _prepare_office_ocr(data, path=path, full=max_pages == 0)
    if lower.endswith(".pdf"):
        text, conf = _extract_pdf_text_layer(data, full=True) if max_pages == 0 else _extract_pdf_text_layer(data)
        # Clear digital text and no pictures: nothing for Surya to read.
        has_images = _pdf_bytes_have_images(data, full=True) if max_pages == 0 else _pdf_bytes_have_images(data)
        if _digital_text_usable(text) and not has_images:
            return {
                "status": "cpu_done",
                "path": path,
                "text": text,
                "conf": conf,
                "engine": "pypdf",
                "segments": [],
            }
        segments = _cpu_pdf_segments(data, path=path, max_pages=max_pages)
        native_parts = [
            str(seg.get("text") or "")
            for seg in segments
            if seg.get("type") == "text" and seg.get("text")
        ]
        ocr_segs = [
            seg
            for seg in segments
            if seg.get("type") == "image" and _raster_needs_actual_ocr(seg.get("image"))
        ]
        native = "\n\n".join(p for p in native_parts if p)
        if _digital_text_usable(native) and not ocr_segs:
            return {
                "status": "cpu_done",
                "path": path,
                "text": native,
                "conf": 0.90,
                "engine": "pymupdf-text",
                "segments": [],
            }
        if ocr_segs:
            out = ([{"type": "text", "text": native}] if native else []) + ocr_segs
            return {
                "status": "needs_gpu",
                "path": path,
                "text": "",
                "conf": 0.0,
                "engine": "surya",
                "segments": out,
            }
        if _digital_text_usable(native):
            return {
                "status": "cpu_done",
                "path": path,
                "text": native,
                "conf": 0.90,
                "engine": "pymupdf-text",
                "segments": [],
            }
        return {"status": "skip", "path": path, "text": "", "conf": 0.2, "engine": "stub", "segments": []}
    if any(lower.endswith(ext) for ext in VIDEO_OCR_EXT):
        # Video OCR is frame-based. The filtering stage only marks poor videos;
        # sample a bounded number of source-timestamped frames and send only poor
        # frames to Surya. Clear videos finish without GPU OCR.
        import tempfile
        try:
            from itertools import islice
            from app.services.forensic_media_review import media_needs_ocr, video_frames
            suffix = Path(path).suffix or '.mp4'
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temp:
                temp.write(data)
                temp.flush()
                max_frames = max(1, int(os.environ.get('OCR_VIDEO_MAX_FRAMES', '6') or 6))
                segments = []
                for blob, timing, _coverage in islice(video_frames(temp.name), max_frames):
                    poor, score = media_needs_ocr(blob)
                    if not poor:
                        continue
                    from PIL import Image  # type: ignore
                    img = Image.open(io.BytesIO(blob)).convert('RGB')
                    prepared = prepare_ocr_image(img, max_edge=int(getattr(get_settings(), 'ocr_max_image_edge', 1280) or 1280))
                    if prepared is not None and _raster_needs_actual_ocr(prepared):
                        segments.append({
                            'type': 'image',
                            'image': prepared,
                            'prompt': 'Text Recognition:',
                            'video_timestamp_seconds': timing.get('timestamp_seconds'),
                            'clarity_score': score,
                        })
                if segments:
                    return {'status': 'needs_gpu', 'path': path, 'text': '', 'conf': 0.0, 'engine': 'surya-video', 'segments': segments}
                return {'status': 'skip', 'path': path, 'text': '', 'conf': 0.0, 'engine': 'clear_video', 'segments': []}
        except Exception as exc:
            log.warning('Video OCR prepare failed for %s: %s', path, exc)
            return {'status': 'skip', 'path': path, 'text': '', 'conf': 0.0, 'engine': 'video_prepare_fail', 'segments': []}
    if any(lower.endswith(ext) for ext in IMAGE_EXT):
        try:
            from PIL import Image  # type: ignore

            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return {"status": "skip", "path": path, "text": "", "conf": 0.0, "engine": "na", "segments": []}
        settings = get_settings()
        max_edge = int(getattr(settings, "ocr_max_image_edge", 1280) or 1280)
        prepared = prepare_ocr_image(img, max_edge=max_edge)
        if prepared is None or not _raster_needs_actual_ocr(prepared):
            return {"status": "skip", "path": path, "text": "", "conf": 0.0, "engine": "blank", "segments": []}
        return {
            "status": "needs_gpu",
            "path": path,
            "text": "",
            "conf": 0.0,
            "engine": "surya",
            "segments": [
                {"type": "image", "image": prepared, "prompt": _ocr_prompt_for_path(path)}
            ],
        }
    return {"status": "skip", "path": path, "text": "", "conf": 0.0, "engine": "na", "segments": []}


def _cpu_pdf_segments(data: bytes, *, path: str, max_pages: int | None = None) -> list[dict]:
    settings = get_settings()
    page_limit = int(max_pages if max_pages is not None else (getattr(settings, "ocr_max_pages", 8) or 8))
    page_limit = max(1, page_limit)
    try:
        import fitz  # type: ignore

        # PyMuPDF is not thread-safe — serialize open/render across CPU workers.
        with _fitz_lock:
            doc = fitz.open(stream=data, filetype="pdf")
            segments: list[dict] = []
            try:
                for page_num in range(len(doc) if max_pages == 0 else min(len(doc), page_limit)):
                    page = doc[page_num]
                    native = (page.get_text("text") or "").strip()
                    # Clear text stays as text. A page with no embedded image is
                    # not scanned, so rendering it for Surya only burns the GPU.
                    if _digital_text_usable(native):
                        segments.append({"type": "text", "text": native})
                        continue
                    if not _pdf_page_has_images(page):
                        if native:
                            segments.append({"type": "text", "text": native})
                        continue
                    pix = page.get_pixmap(dpi=110)
                    img_bytes = pix.tobytes("png")
                    from PIL import Image  # type: ignore

                    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    max_edge = int(getattr(settings, "ocr_max_image_edge", 1280) or 1280)
                    prepared = prepare_ocr_image(img, max_edge=max_edge)
                    if prepared is None:
                        continue
                    segments.append(
                        {
                            "type": "image",
                            "image": prepared,
                            "prompt": _ocr_prompt_for_path(f"{path}#page{page_num + 1}"),
                        }
                    )
            finally:
                try:
                    doc.close()
                except Exception:
                    pass
            return segments
    except Exception as exc:
        log.warning("PyMuPDF prepare failed for %s: %s", path, exc)
        return []


def _gpu_ocr_prepared(prep: dict) -> tuple[str, float, str]:
    """Run Surya only on CPU-prepared image segments; keep native PDF text as-is."""
    if prep.get("status") == "cpu_done":
        return str(prep.get("text") or ""), float(prep.get("conf") or 0.0), str(prep.get("engine") or "pypdf")
    if prep.get("status") != "needs_gpu":
        engine = str(prep.get("engine") or "na")
        return "", 0.0, engine
    texts: list[str] = []
    confs: list[float] = []
    image_segs: list[dict] = []
    for seg in prep.get("segments") or []:
        if seg.get("type") == "text" and seg.get("text"):
            texts.append(str(seg["text"]))
            confs.append(0.90)
            continue
        if seg.get("image") is not None:
            image_segs.append(seg)
    micro = _ocr_micro_batch_size()
    idx = 0
    while idx < len(image_segs):
        chunk = image_segs[idx : idx + micro]
        idx += len(chunk)
        prompt = str(chunk[0].get("prompt") or "Text Recognition:")
        if any(str(seg.get("prompt") or "Text Recognition:") != prompt for seg in chunk):
            prompt = "Text Recognition:"
        try:
            decoded = _surya_ocr_images([seg.get("image") for seg in chunk], prompt=prompt)
        except TimeoutError as exc:
            raise RuntimeError(f"SuryaOCR timed out for {prep.get('path')}; page remains retryable") from exc
        for text, conf in decoded:
            if text:
                texts.append(text)
                confs.append(conf)
    if not texts:
        return "", 0.0, "ocr_unavailable"
    return "\n\n".join(texts), (sum(confs) / len(confs) if confs else 0.0), selected_ocr_engine()


def _extract_pdf_text_layer(data: bytes, *, full: bool = False) -> tuple[str, float]:
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages if full else reader.pages[:50]:
            t = page.extract_text() or ""
            if t.strip():
                pages.append(t.strip())
        if pages:
            return "\n\n".join(pages), 0.90
    except Exception as exc:
        log.debug("pypdf extract failed: %s", exc)
    return "", 0.0


def _ocr_pdf_pages(data: bytes, *, path: str, max_pages: int | None = None) -> tuple[str, float]:
    from app.services.gpu_thermal import GpuThermalAbort, thermal_guard_after_batch, thermal_guard_before_batch

    settings = get_settings()
    page_limit = int(max_pages if max_pages is not None else (getattr(settings, "ocr_max_pages", 10) or 10))
    page_limit = max(1, page_limit)
    try:
        import fitz  # type: ignore

        doc = fitz.open(stream=data, filetype="pdf")
        all_text: list[str] = []
        confidences: list[float] = []
        for page_num in range(len(doc) if max_pages == 0 else min(len(doc), page_limit)):
            thermal_guard_before_batch(reason=f"surya_pdf_p{page_num + 1}")
            page = doc[page_num]
            native = (page.get_text("text") or "").strip()
            if _digital_text_usable(native) or not _pdf_page_has_images(page):
                if native:
                    all_text.append(native)
                    confidences.append(0.90)
                thermal_guard_after_batch(reason="surya_pdf_text")
                continue
            pix = page.get_pixmap(dpi=110)
            img_bytes = pix.tobytes("png")
            text, conf = _ocr_image_bytes(img_bytes, path=f"{path}#page{page_num + 1}")
            if text:
                all_text.append(text)
                confidences.append(conf)
            thermal_guard_after_batch(reason="surya_pdf_page")
        if all_text:
            avg_conf = sum(confidences) / len(confidences) if confidences else 0.7
            return "\n\n".join(all_text), avg_conf
    except GpuThermalAbort:
        raise
    except Exception as exc:
        log.debug("SuryaOCR PDF render failed: %s", exc)
    return "", 0.0


def _ocr_cpu_worker_count(*, gpu: bool, n_items: int, num_buckets: int = 1) -> int:
    settings = get_settings()
    wanted = int(getattr(settings, "ocr_cpu_workers", 8) or 8)
    buckets = max(int(num_buckets or 1), 1)
    if buckets > 1:
        wanted = max(2, wanted // buckets)
    from app.services.forensic_serial_policy import parallelism

    return parallelism(min(wanted, max(n_items, 1)))


def open_ocr_cpu_pool(workers: int):
    """CPU text-layer pool. Celery prefork workers cannot spawn processes.

    Spawning a process after ``torch.cuda.is_available()`` has already created a
    CUDA context deadlocks on this host (the pool never starts, so Surya never
    runs and the OCR card stays put). Threads share the process and stay safe.
    """
    from app.services.forensic_serial_policy import parallelism

    n = parallelism(workers)
    cuda_ready = False
    try:
        import torch

        cuda_ready = bool(torch.cuda.is_initialized())
    except Exception:
        cuda_ready = False
    if multiprocessing.current_process().daemon or cuda_ready:
        log.info(
            "OCR CPU pool — %s thread(s) (CUDA context or prefork worker)",
            n,
        )
        return ThreadPoolExecutor(max_workers=n, thread_name_prefix="ocr-cpu")
    try:
        ctx = multiprocessing.get_context("spawn")
        return ProcessPoolExecutor(max_workers=n, mp_context=ctx)
    except Exception as extra:
        log.warning("OCR process pool unavailable (%s) — using threads", extra)
        return ThreadPoolExecutor(max_workers=n, thread_name_prefix="ocr-cpu")


def _ocr_cpu_process_one(payload: dict) -> dict:
    """Picklable process-pool worker: text-layer / CPU prepare, no CUDA."""
    row = payload.get("row") or {}
    data = payload.get("data")
    path = str(payload.get("path") or "")
    try:
        prep = cpu_prepare_ocr_item(
            data,
            path=path,
            max_pages=payload.get("max_pages"),
            allow_photos=bool(payload.get("allow_photos")),
        )
    except Exception as exc:
        return {
            "row": row,
            "status": "skip",
            "engine": "prep_fail",
            "text": "",
            "conf": 0.0,
            "error": str(exc)[:200],
        }
    if prep.get("status") == "cpu_done":
        return {
            "row": row,
            "status": "ok",
            "text": str(prep.get("text") or ""),
            "conf": float(prep.get("conf") or 0.0),
            "engine": str(prep.get("engine") or "pypdf"),
        }
    if prep.get("status") == "skip":
        return {
            "row": row,
            "status": "skip",
            "engine": str(prep.get("engine") or "na"),
            "text": "",
            "conf": 0.0,
        }
    return {
        "row": row,
        "status": "needs_gpu",
        "engine": "surya",
        "text": "",
        "conf": 0.0,
    }


def ocr_bytes(
    data: bytes,
    *,
    path: str,
    max_pages: int | None = None,
) -> tuple[str, float, str]:
    prep = cpu_prepare_ocr_item(
        data, path=path, max_pages=max_pages, allow_photos=forensic_photos_allowed()
    )
    if prep.get("status") == "skip":
        engine = str(prep.get("engine") or "na")
        if engine == "stub":
            return "", 0.0, "ocr_unavailable"
        return "", 0.0, engine
    text, conf, engine = _gpu_ocr_prepared(prep)
    if text:
        return text, conf, engine
    if str(path).lower().endswith(".pdf"):
        return "", 0.0, "ocr_unavailable"
    if any(str(path).lower().endswith(ext) for ext in IMAGE_EXT):
        return "", 0.0, "ocr_unavailable"
    return "", 0.0, "na"


def selected_ocr_engine() -> str:
    """Honor explicit OCR_ENGINE; Surya 2 uses a distinct remote vLLM backend."""
    requested = (os.environ.get("OCR_ENGINE") or getattr(get_settings(), "ocr_engine", "surya") or "surya").strip().lower()
    if requested in {"surya", "surya-ocr", "suryaocr"}:
        return "surya"
    if requested in {"tesseract", "fast"}:
        return "tesseract"
    raise ValueError(f"Unknown OCR_ENGINE={requested!r}; use surya or tesseract")


def _tesseract_tsv_to_text(raw: str) -> tuple[str, float]:
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) <= 1:
        return "", 0.0
    words: list[str] = []
    confs: list[float] = []
    last_line: str | None = None
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        text = cols[11].strip()
        if not text:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        line_no = cols[4]
        if last_line is not None and line_no != last_line:
            words.append("\n")
        words.append(text)
        confs.append(conf)
        last_line = line_no
    body = " ".join(words).replace(" \n ", "\n").strip()
    if not body or not confs:
        return "", 0.0
    return body, (sum(confs) / len(confs)) / 100.0


def _tesseract_image(img) -> tuple[str, float]:
    import subprocess

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    env = os.environ.copy()
    # One thread per process. Eight Tesseract processes then use the CPU
    # instead of each one starting a full OpenMP team.
    env["OMP_THREAD_LIMIT"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    proc = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "6", "tsv"],
        input=buf.getvalue(),
        capture_output=True,
        timeout=25,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        return "", 0.0
    return _tesseract_tsv_to_text(proc.stdout.decode("utf-8", errors="replace"))


def _tesseract_payload(payload: dict, *, read_file_fn, max_pages, allow_default_photos: bool) -> dict:
    row = (payload.get("row") or {}) if isinstance(payload, dict) else {}
    path = str(payload.get("path") or row.get("file_path") or "")
    data = payload.get("data")
    if data is None and read_file_fn is not None:
        try:
            data = read_file_fn(path)
        except Exception:
            data = None
    prep = cpu_prepare_ocr_item(
        data or b"",
        path=path,
        max_pages=max_pages,
        allow_photos=bool(payload.get("allow_photos")) or allow_default_photos,
    )
    if prep.get("status") == "cpu_done" and prep.get("text"):
        return {
            "row": row,
            "text": str(prep.get("text") or ""),
            "conf": float(prep.get("conf") or 0.9),
            "engine": str(prep.get("engine") or "pypdf"),
        }
    if prep.get("status") != "needs_gpu":
        return {"row": row, "text": "", "conf": 0.0, "engine": str(prep.get("engine") or "na")}
    texts: list[str] = []
    confs: list[float] = []
    for seg in prep.get("segments") or []:
        if seg.get("type") == "text" and seg.get("text"):
            texts.append(str(seg["text"]))
            confs.append(0.90)
            continue
        img = seg.get("image")
        if img is None:
            continue
        text, conf = _tesseract_image(img)
        if text:
            texts.append(text)
            confs.append(conf)
    if not texts:
        return {"row": row, "text": "", "conf": 0.0, "engine": "tesseract"}
    return {
        "row": row,
        "text": "\n\n".join(texts),
        "conf": sum(confs) / len(confs),
        "engine": "tesseract",
    }


def _ocr_engine_available() -> bool:
    engine = selected_ocr_engine()
    if engine == "tesseract":
        return shutil.which("tesseract") is not None
    if engine == "surya":
        try:
            from app.services.surya_adapter import _ready, _server_url
            _ready(_server_url())
            return True
        except Exception as exc:
            log.warning("External SuryaOCR is not ready: %s", exc)
            return False
    return False


def park_pictures_for_evidence(db, job_id: str) -> int:
    """Leave camera and chat images for image/video evidence, not Surya.

    OCR is for documents that are not already clear text. Pictures and videos
    are examined by the media-evidence stage, which reads visible text and
    describes what is in the frame.
    """
    result = execute(
        db,
        f"""UPDATE job_artifacts SET ocr_status='skipped', updated_at=NOW()
            WHERE job_id=:jid AND ocr_status='pending'
              AND lower(coalesce(extension, '')) <> '.pdf'
              AND NOT ({OCR_DOC_FOLDER_SQL})
              AND NOT ({OCR_QUALITY_BUCKET_SQL})""",
        {"jid": job_id},
    )
    parked = int(getattr(result, "rowcount", 0) or 0)
    if parked:
        from app.services.disk_build_log import write_disk_log

        write_disk_log(
            db,
            job_id,
            (
                f"Image/video evidence kept {parked:,} picture(s) out of OCR. "
                "GPU OCR continues on documents that are not already clear text."
            ),
            stage="ocr",
        )
    return parked


def _update_ocr_in_claimed_batches(db, job_id: str, *, predicate: str, new_status: str,
                                   batch_size: int = 250, max_batches: int = 100000) -> int:
    """Short, deterministic OCR transitions without a job-wide UPDATE lock storm.

    SKIP LOCKED lets parse/media writers finish their own transactions.  Rows
    skipped because another transaction owns them will be revisited by the next
    sweep; callers must not interpret this function's return as full coverage.
    """
    import random
    import time
    from sqlalchemy import text

    total = 0
    for _ in range(max_batches):
        for attempt in range(5):
            try:
                result = db.execute(text(f"""
                    WITH claimed AS MATERIALIZED (
                        SELECT id FROM job_artifacts
                        WHERE job_id = :jid AND ocr_status='pending' AND ({predicate})
                        ORDER BY id
                        LIMIT :batch_size
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE job_artifacts AS artifact
                    SET ocr_status=:status, updated_at=NOW()
                    FROM claimed
                    WHERE artifact.id=claimed.id
                    RETURNING artifact.id
                """), {"jid": job_id, "status": new_status, "batch_size": batch_size})
                changed = len(result.fetchall())
                # Release row locks before any GPU work or filesystem I/O.
                db.commit()
                total += changed
                break
            except Exception as exc:
                db.rollback()
                if not is_retryable_db_error(exc) or attempt == 4:
                    raise
                time.sleep(min(0.8, 0.05 * (2 ** attempt)) + random.uniform(0, 0.04))
        if changed < batch_size:
            break
    else:
        log.warning("OCR status batch safety limit reached job=%s status=%s", job_id, new_status)
    return total


def skip_ocr_noise_pending(db, job_id: str) -> int:
    """Demote OCR-ineligible rows without locking every job artifact at once."""
    try:
        from app.services.rag_image_evidence import is_image_evidence_job
        if is_image_evidence_job(db, job_id):
            return 0
    except Exception:
        db.rollback()
        # The image-evidence policy cannot be guessed if its lookup failed.
        raise

    skipped = _update_ocr_in_claimed_batches(
        db, job_id, predicate=f"NOT ({OCR_PENDING_CANDIDATE_SQL})", new_status="na")
    skipped += _update_ocr_in_claimed_batches(
        db, job_id,
        predicate=f"({OCR_NOISE_SQL}) AND NOT ({OCR_QUALITY_BUCKET_SQL})",
        new_status="skipped")
    if not forensic_photos_allowed():
        skipped += _update_ocr_in_claimed_batches(
            db, job_id,
            predicate=f"lower(coalesce(extension, '')) <> '.pdf' "
                      f"AND NOT ({OCR_DOC_FOLDER_SQL}) AND NOT ({OCR_QUALITY_BUCKET_SQL})",
            new_status="skipped")
    return skipped


def _ocr_eligible_keep_sql() -> str:
    """V67: CUDA OCR is the poor-quality bucket, not every PDF/image.

    Native parsers handle readable documents; media review handles clear pictures/video.
    The quality triage explicitly marks scanned/blurred/unreadable evidence.
    """
    return f"({OCR_QUALITY_BUCKET_SQL})"


def _requeue_eligible_ocr_batched(db, job_id: str, keep: str) -> int:
    import random
    import time
    from sqlalchemy import text
    total = 0
    for _ in range(100000):
        for attempt in range(5):
            try:
                rows = db.execute(text(f"""
                    WITH claimed AS MATERIALIZED (
                        SELECT id FROM job_artifacts
                        WHERE job_id=:jid AND coalesce(ocr_status, '') IN ('na','')
                          AND ({keep})
                        ORDER BY id LIMIT 250 FOR UPDATE SKIP LOCKED
                    )
                    UPDATE job_artifacts AS artifact
                    SET ocr_status='pending', updated_at=NOW()
                    FROM claimed WHERE artifact.id=claimed.id
                    RETURNING artifact.id
                """), {"jid": job_id}).fetchall()
                count = len(rows)
                db.commit()
                total += count
                break
            except Exception as exc:
                db.rollback()
                if not is_retryable_db_error(exc) or attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt) + random.uniform(0, 0.04))
        if count < 250:
            break
    else:
        log.warning("OCR requeue batch safety limit reached job=%s", job_id)
    return total


def enqueue_eligible_ocr(db, job_id: str) -> int:
    """Queue PDFs/images that were never attempted (``na`` / empty status).

    Policy skips stay skipped. Re-opening ``skipped`` every huddle/drain
    bounce put camera-roll files back on the OCR queue and froze the card
    at 1% with hundreds of pending photos that do not need Surya.
    """
    keep = _ocr_eligible_keep_sql()
    _update_ocr_in_claimed_batches(
        db, job_id,
        predicate=f"coalesce(metadata->>'ocr_quality_checked','false')='true' "
                  f"AND NOT ({OCR_QUALITY_BUCKET_SQL})",
        new_status="skipped")
    # Requeue eligible OCR records in short batches as well.  The helper
    # below uses a different predicate because eligible rows begin as 'na'.
    queued = _requeue_eligible_ocr_batched(db, job_id, keep)
    return queued


def reopen_failed_ocr_without_results(db, job_id: str) -> int:
    """Re-queue eligible files that CPU drain marked failed because Surya never ran."""
    keep = _ocr_eligible_keep_sql()
    result = execute(
        db,
        f"""UPDATE job_artifacts SET ocr_status='pending', updated_at=NOW()
            WHERE job_id=:jid AND ocr_status='failed' AND {keep}
              AND NOT EXISTS (
                    SELECT 1 FROM ocr_results o WHERE o.job_artifact_id = job_artifacts.id
              )""",
        {"jid": job_id},
    )
    return int(getattr(result, "rowcount", 0) or 0)


def reopen_skipped_forensic_ocr(db, job_id: str) -> int:
    """Re-queue skipped/never-attempted files that still match live OCR policy.

    ``enqueue_eligible_ocr`` only touches ``na`` / empty so huddle cannot bounce
    camera-roll skips. This path is the dedicated Surya retry: Documents/Downloads
    PDFs and images that were parked as ``skipped`` when a CPU worker had no CUDA.
    Noise and camera rolls stay skipped because they fail ``_ocr_eligible_keep_sql``.
    """
    keep = _ocr_eligible_keep_sql()
    result = execute(
        db,
        f"""UPDATE job_artifacts SET ocr_status='pending', updated_at=NOW()
            WHERE job_id=:jid
              AND coalesce(ocr_status, '') IN ('skipped', 'na', '')
              AND {keep}""",
        {"jid": job_id},
    )
    return int(getattr(result, "rowcount", 0) or 0)


def count_ocr_unfinished(db, job_id: str) -> int:
    """Work still to do: pending plus failed files that never produced OCR text.

    Skipped/failed-with-results files are finished policy outcomes — they must
    not keep the OCR card at 85% after the queue is empty.
    """
    keep = _ocr_eligible_keep_sql()
    row = fetchone(
        db,
        f"""SELECT count(*)::int AS c FROM job_artifacts
            WHERE job_id=:jid AND {keep}
              AND (
                coalesce(ocr_status, '') IN ('pending', 'na', '')
                OR (
                  ocr_status = 'failed'
                  AND NOT EXISTS (
                    SELECT 1 FROM ocr_results o WHERE o.job_artifact_id = job_artifacts.id
                  )
                )
              )""",
        {"jid": job_id},
    )
    return int((row or {}).get("c") or 0)


def count_ocr_eligible(db, job_id: str) -> int:
    """PDFs/images that should be OCR'd (any status), excluding cache/OS noise."""
    keep = _ocr_eligible_keep_sql()
    row = fetchone(
        db,
        f"SELECT count(*)::int AS c FROM job_artifacts WHERE job_id=:jid AND {keep}",
        {"jid": job_id},
    )
    return int((row or {}).get("c") or 0)


def count_pending_ocr(
    db,
    job_id: str,
    *,
    apply_skip: bool = False,
    ocr_bucket: int | None = None,
    ocr_buckets: int | None = None,
) -> int:
    """Count artifacts still waiting for OCR. Read-only unless apply_skip=True.

    Counting must not skip evidence. A previous version called skip_ocr_noise_pending
    here, which mass-skipped Pictures/screenshots and made the UI report 100% OCR
    in a few seconds.
    """
    if apply_skip:
        try:
            skip_ocr_noise_pending(db, job_id)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    bucket_sql = _ocr_bucket_sql(ocr_bucket, ocr_buckets)
    row = fetchone(
        db,
        f"""SELECT count(*)::int AS c FROM job_artifacts
            WHERE job_id=:jid AND ocr_status='pending' AND {OCR_PENDING_CANDIDATE_SQL}{bucket_sql} AND (coalesce(metadata->>'ocr_quality_checked','false') <> 'true' OR coalesce(metadata->>'ocr_quality_bucket','false') = 'true')""",
        {"jid": job_id},
    )
    return int((row or {}).get("c") or 0)


def write_ocr_live_progress(db, job_id: str, *, label: str | None = None) -> None:
    """Merge OCR counts into jobs.pipeline_progress without wiping orchestration."""
    from app.services.pipeline_progress import write_merged_pipeline_progress

    pending = count_pending_ocr(db, job_id)
    done_row = fetchone(
        db,
        "SELECT count(*)::int AS c FROM job_artifacts WHERE job_id=:jid AND ocr_status='done'",
        {"jid": job_id},
    )
    cum_done = int((done_row or {}).get("c") or 0)
    # Empty bucket is a completed OCR stage, not 0/1. If poor evidence arrives
    # later the pending count grows and the ratio automatically falls below 100%.
    if cum_done == 0 and pending == 0:
        progress_done, progress_total = 1, 1
        default_label = "OCR — bucket empty, 100% complete"
    else:
        progress_done, progress_total = cum_done, max(cum_done + pending, 1)
        default_label = f"OCR — {cum_done:,} done, {pending:,} pending"
    write_merged_pipeline_progress(
        db,
        job_id,
        {
            "phase": "ocr",
            "completed": progress_done,
            "total": progress_total,
            "label": label or default_label,
        },
        writer="ocr",
    )
    # V57: when OCR is running in parallel with recovery, mirror live counts
    # into the pending OCR stage row. We deliberately do not set its DB status
    # to running because the serial controller allows only one authoritative
    # running stage per job. The UI renders this sidecar state from details.
    try:
        concurrent_stage = fetchone(
            db,
            """SELECT stage FROM pipeline_stage_runs
               WHERE job_id=:jid AND stage IN ('materialize','parse','recovery')
                 AND status='running' ORDER BY CASE stage
                 WHEN 'materialize' THEN 1 WHEN 'parse' THEN 2 ELSE 3 END DESC LIMIT 1""",
            {"jid": job_id},
        )
        if concurrent_stage:
            execute(
                db,
                """UPDATE pipeline_stage_runs
                   SET details=coalesce(details,'{}'::jsonb) || CAST(:d AS jsonb),
                       total_items=:total, completed_items=:done, heartbeat_at=NOW(), updated_at=NOW()
                   WHERE job_id=:jid AND stage='ocr' AND status='pending'""",
                {
                    "jid": job_id,
                    "total": progress_total,
                    "done": progress_done,
                    "d": json.dumps({
                        "parallel_active": pending > 0,
                        "parallel_complete": pending == 0,
                        "parallel_with": str(concurrent_stage["stage"]),
                        "parallel_completed": progress_done,
                        "parallel_total": progress_total,
                        "parallel_label": label or default_label,
                    }),
                },
            )
    except Exception:
        pass


def _ocr_bucket_sql(bucket_id: int | None, num_buckets: int | None) -> str:
    if bucket_id is None or not num_buckets or int(num_buckets) <= 1:
        return ""
    return f" AND mod(abs(hashtext(file_path)), {int(num_buckets)}) = {int(bucket_id)}"


def _ocr_pending_filter_sql(*, cpu_bucket: bool) -> str:
    """CPU buckets take only small PDFs/documents that may have a cheap text layer.

    Scans and image evidence remain pending for the CUDA Surya drain. A CPU bucket
    that discovers a scan leaves it pending, so the GPU lane owns recognition.
    """
    if cpu_bucket:
        limit = max(262144, int(getattr(get_settings(), "ocr_cpu_small_file_max_bytes", 8 * 1024 * 1024) or 8 * 1024 * 1024))
        return (" AND lower(coalesce(extension, '')) IN ('.pdf','.txt','.csv','.rtf')"
                f" AND coalesce(size_bytes,0) <= {limit}")
    return ""


def _ocr_pending_order_sql(*, surya_first: bool) -> str:
    """GPU drain pulls images/scans first so Surya is not stuck behind digital PDFs."""
    if surya_first:
        return """
               ORDER BY
                 CASE
                   WHEN lower(coalesce(extension, '')) <> '.pdf' THEN 0
                   WHEN lower(file_path) LIKE '%scan%' THEN 1
                   WHEN lower(file_path) LIKE '%/documents/%' THEN 2
                   WHEN lower(file_path) LIKE '%/download%' THEN 3
                   ELSE 4
                 END,
                 size_bytes DESC NULLS LAST
        """
    return " ORDER BY size_bytes DESC NULLS LAST"


def this_is_surya_drain_task() -> bool:
    """True only inside ``ocr_drain_task`` (the dedicated CUDA worker)."""
    try:
        from celery import current_task

        name = str(getattr(current_task, "name", "") or "")
        return name == "app.tasks.ocr_drain_task"
    except Exception:
        return False


def surya_defer_log_message(n: int, *, cpu_bucket: bool, cuda_ok: bool) -> str:
    """Honest status when scans are left pending for Surya."""
    count = int(n or 0)
    if cpu_bucket:
        return f"OCR agent — {count} scan(s) left for Surya service"
    if cuda_ok:
        return f"OCR agent — {count} scan(s) queued for Surya service"
    return (
        f"OCR agent — {count} scan(s) need Surya service, currently unavailable"
    )


def handoff_surya_scans_to_worker(
    schema_name: str,
    job_id: str,
    count: int,
    *,
    cpu_bucket: bool,
) -> tuple[str, bool]:
    """Queue Surya scans onto ``ocr_drain_task`` (CUDA worker). Returns (log, handed_off).

    CPU buckets only park the rows. A full drain running on a disk/agent worker
    must not sit on ``CUDA is unavailable`` — it hands the scans to worker-ocr-gpu.
    """
    n = int(count or 0)
    if cpu_bucket:
        return surya_defer_log_message(n, cpu_bucket=True, cuda_ok=_cuda_usable()), False
    if this_is_surya_drain_task():
        return surya_defer_log_message(n, cpu_bucket=False, cuda_ok=_cuda_usable()), False
    if ocr_should_use_gpu():
        try:
            from app.tasks import ocr_drain_task

            ocr_drain_task.delay(schema_name, job_id)
            return f"OCR agent — {n} scan(s) handed to Surya worker", True
        except Exception as exc:
            log.warning("OCR handoff to CUDA worker failed: %s", exc)
    return surya_defer_log_message(n, cpu_bucket=False, cuda_ok=_cuda_usable()), False


def ocr_is_gpu_only() -> bool:
    """OCR Celery work belongs on worker-ocr-gpu only (no CPU bucket farm)."""
    settings = get_settings()
    if getattr(settings, "ocr_gpu_only", True):
        return True
    return int(getattr(settings, "ocr_parallel_buckets", 0) or 0) <= 0


def queue_parallel_ocr_buckets(
    schema_name: str,
    job_id: str,
    *,
    num_buckets: int | None = None,
) -> int:
    """Fan CPU OCR across N Celery workers. No-op when OCR is GPU-only."""
    if ocr_is_gpu_only():
        return 0
    settings = get_settings()
    buckets = max(int(num_buckets or getattr(settings, "ocr_parallel_buckets", 0) or 0), 0)
    buckets = min(buckets, 8)
    if buckets <= 0:
        return 0
    from app.tasks import ocr_bucket_task
    from app.forensic_common.pipeline_routing import parse_queue

    cpu_queue = parse_queue()
    for bucket_id in range(buckets):
        ocr_bucket_task.apply_async(args=(schema_name, job_id, bucket_id, buckets), queue=cpu_queue)
    return buckets


def dispatch_ocr_agent(schema_name: str, job_id: str) -> int:
    """Start CUDA recognition and small-file CPU extraction together.

    CPU buckets are limited to cheap/small document work. Any item that really
    needs OCR remains pending for the unsharded CUDA Surya drain.
    """
    from app.tasks import ocr_drain_task

    ocr_drain_task.delay(schema_name, job_id)
    buckets = queue_parallel_ocr_buckets(schema_name, job_id)
    return 1 + int(buckets or 0)


def _serial_ocr_tick(db, job_id: str, *, advanced: bool, operation: str = "SuryaOCR processing") -> None:
    """Keep the OCR stage alive while a document is read or recognized.

    The stage deadline is five minutes. Preparing a batch on CPU workers never
    moved that clock, so the supervisor stopped OCR before recognition ran and
    the card returned to 0.
    """
    try:
        from app.services.progress_agent import note_operation

        note_operation(
            db,
            job_id,
            "ocr",
            operation,
            timeout_seconds=900,
            advanced=advanced,
        )
    except Exception:
        return
    if not advanced:
        return
    try:
        pending = count_pending_ocr(db, job_id)
        done = int(
            (
                fetchone(
                    db,
                    "SELECT count(*)::int AS c FROM job_artifacts WHERE job_id=:jid AND ocr_status='done'",
                    {"jid": job_id},
                )
                or {}
            ).get("c")
            or 0
        )
        total = done + pending
        if total <= 0:
            return
        from app.services.forensic_serial_stages import report_progress

        report_progress(
            db,
            job_id,
            "ocr",
            total=total,
            completed=done,
            label=operation,
        )
    except Exception:
        log.debug("Serial OCR progress update skipped", exc_info=True)


def _load_ocr_batch_bytes(read_file_fn, payloads: list[dict]) -> None:
    """Fill a Surya batch from one pass over each archive part.

    A per-file read restarts a multi-GB tar from the beginning. The OCR card
    then sits on that scan instead of on the GPU.
    """
    if not payloads or read_file_fn is None:
        return
    missing = [
        payload
        for payload in payloads
        if payload.get("data") is None
        and str(payload.get("path") or (payload.get("row") or {}).get("file_path") or "")
    ]
    if not missing:
        return
    iter_grouped = getattr(read_file_fn, "iter_grouped", None)
    if callable(iter_grouped):
        by_path: dict[str, dict] = {}
        paths: list[str] = []
        for payload in missing:
            path = str(payload.get("path") or (payload.get("row") or {}).get("file_path") or "")
            by_path[path.replace("\\", "/")] = payload
            paths.append(path)
        log.info("OCR reading %d file(s) in one archive pass", len(paths))
        try:
            for path, data in iter_grouped(paths):
                payload = by_path.get(str(path).replace("\\", "/"))
                if payload is not None and payload.get("data") is None:
                    payload["data"] = data
        except Exception as exc:
            log.warning("OCR batch file read failed: %s", exc)
        return
    preload = getattr(read_file_fn, "preload", None)
    if not callable(preload):
        return
    try:
        preload([
            str(payload.get("path") or (payload.get("row") or {}).get("file_path") or "")
            for payload in missing
        ])
    except Exception as exc:
        log.warning("OCR batch file read failed: %s", exc)


def run_ocr_for_job(
    db,
    job_id: str,
    *,
    schema_name: str,
    read_file_fn,
    paths: list[str] | None = None,
    batch_limit: int | None = None,
    ocr_bucket: int | None = None,
    ocr_buckets: int | None = None,
    gpu_permit_owned: bool = False,
) -> dict:
    settings = get_settings()
    if not settings.ocr_enabled:
        return {"status": "skipped", "ocr_count": 0}

    from app.services.gpu_thermal import recommended_ocr_batch_limit

    queued = enqueue_eligible_ocr(db, job_id)
    skipped_noise = skip_ocr_noise_pending(db, job_id)
    skipped_noise += park_pictures_for_evidence(db, job_id)
    # The UPDATE inside skip_ocr_noise_pending starts a transaction even when it
    # changes zero rows.  Always commit before waiting for GPU/model/file I/O so
    # PostgreSQL does not retain RowExclusiveLock for hours.
    if queued:
        log.info("ocr enqueue job=%s queued=%s", job_id, queued)
    db.commit()

    image_ev = False
    try:
        from app.services.rag_image_evidence import is_image_evidence_job

        image_ev = is_image_evidence_job(db, job_id)
    except Exception:
        image_ev = False
    ocr_page_budget = None
    if image_ev:
        ocr_page_budget = int(
            getattr(settings, "rag_image_ocr_max_pages", None)
            or getattr(settings, "ocr_max_pages", 10)
            or 200
        )
    from app.services.forensic_serial_policy import current_stage

    if current_stage() == "ocr":
        # Zero is the explicit all-pages sentinel, carried into CPU threads.
        ocr_page_budget = 0

    # Thermal-safe batch — live plan + in-process governor (never above .env ceiling).
    configured = max(int(batch_limit or getattr(settings, "ocr_batch_limit", 40) or 40), 1)
    try:
        from app.services.perf_broadcast import live_ocr_batch

        configured = min(configured, live_ocr_batch(configured))
    except Exception:
        pass
    limit = recommended_ocr_batch_limit(configured)
    limit = min(max(limit, 1), 80)
    # Tesseract reads each archive part once. A 30-file batch would rescan that
    # part from the beginning for every later batch, so take the pending set.
    if not paths and selected_ocr_engine() == "tesseract":
        limit = 4000

    if paths:
        rows = fetchall(
            db,
            f"""SELECT id, file_path, size_bytes, sha256, metadata FROM job_artifacts
               WHERE job_id=:jid AND ocr_status='pending' AND {OCR_PENDING_CANDIDATE_SQL}
                 AND file_path = ANY(:paths)
               {_ocr_pending_order_sql(surya_first=True)}
               LIMIT :lim""",
            {"jid": job_id, "paths": paths, "lim": limit},
        )
    else:
        cpu_bucket = ocr_bucket is not None
        bucket_sql = _ocr_bucket_sql(ocr_bucket, ocr_buckets)
        filt = _ocr_pending_filter_sql(cpu_bucket=cpu_bucket)
        rows = fetchall(
            db,
            f"""SELECT id, file_path, size_bytes, sha256, metadata FROM job_artifacts
               WHERE job_id=:jid AND ocr_status='pending' AND {OCR_PENDING_CANDIDATE_SQL}{bucket_sql}{filt}
               {_ocr_pending_order_sql(surya_first=not cpu_bucket)}
               LIMIT :lim""",
            {"jid": job_id, "lim": limit},
        )
    # Candidate selection is read-only, but SQLAlchemy still opens a transaction.
    # Close it before the potentially long GPU-slot/model-load phase.
    db.commit()
    if not rows:
        return {"status": "ok", "ocr_count": 0, "skipped_noise": skipped_noise}

    # Scans and images are recognized by Surya service. A CPU worker pool is used
    # only when this deployment is explicitly not GPU-only and this process has
    # no CUDA device.
    device_label, gpu = _resolve_ocr_device()
    if ocr_bucket is not None:
        # Path-hash buckets are CPU-only. Surya stays on the unsharded GPU drain.
        device_label, gpu = "cpu", False
    gpu_only = bool(gpu) or (ocr_bucket is None and ocr_is_gpu_only())
    cpu_n = 0 if gpu_only else _ocr_cpu_worker_count(
        gpu=False, n_items=len(rows), num_buckets=int(ocr_buckets or 1)
    )
    fast_engine = selected_ocr_engine() == "tesseract"
    write_disk_log(
        db,
        job_id,
        (
            f"OCR — Tesseract {len(rows)} document(s)"
            if fast_engine
            else (
                f"GPU OCR — {len(rows)} document(s) on CUDA"
                if gpu or gpu_only
                else f"CPU OCR — {len(rows)} text-layer document(s) with {cpu_n} CPU worker(s)"
            )
        ),
        stage="ocr",
        metadata={"gpu": bool(gpu or gpu_only), "device": "cuda:0" if gpu else device_label, "ocr_cpu_workers": cpu_n},
    )
    write_ocr_live_progress(
        db,
        job_id,
        label=(
            f"OCR — Tesseract {len(rows)} document(s)"
            if fast_engine
            else (
                f"GPU OCR — {len(rows)} document(s) on CUDA"
                if gpu or gpu_only
                else f"CPU OCR — {len(rows)} document(s)"
            )
        ),
    )
    db.commit()

    done = 0

    def _mark_ocr_skip(row: dict, *, engine: str) -> None:
        # Policy skips stay skipped (will not match enqueue keep). Attempt
        # failures stay failed so huddle does not re-queue them forever.
        st = (
            "skipped"
            if engine in ("na", "photo", "tiny", "os_vendor", "blank", "stub")
            else "failed"
        )
        execute(
            db,
            "UPDATE job_artifacts SET ocr_status=:st, updated_at=NOW() WHERE id=:id",
            {"id": row["id"], "st": st},
        )
        if image_ev:
            try:
                execute(
                    db,
                    """UPDATE rag_image_assets SET ocr_status=:st, ocr_engine=:eng,
                           updated_at=NOW() WHERE job_artifact_id=:aid""",
                    {"aid": row["id"], "st": st, "eng": engine},
                )
            except Exception:
                pass

    def _persist_ocr_ok(row: dict, text: str, conf: float, engine: str) -> None:
        safe_text = (text or "").replace("\x00", "").replace("\u0000", "")[:500_000]
        execute(
            db,
            """INSERT INTO ocr_results (job_artifact_id, ocr_text, confidence, engine)
               VALUES (:aid, :text, :conf, :eng)""",
            {"aid": row["id"], "text": safe_text, "conf": conf, "eng": engine},
        )
        execute(
            db,
            "UPDATE job_artifacts SET ocr_status='done', updated_at=NOW() WHERE id=:id",
            {"id": row["id"]},
        )
        if image_ev:
            try:
                execute(
                    db,
                    """UPDATE rag_image_assets SET
                           ocr_text=:text, ocr_confidence=:conf, ocr_engine=:eng,
                           ocr_status='done', stage_status='searchable', updated_at=NOW()
                         WHERE job_artifact_id=:aid""",
                    {"aid": row["id"], "text": safe_text, "conf": conf, "eng": engine},
                )
            except Exception:
                pass

    def _reuse_ocr(row: dict) -> tuple[str | None, float, str]:
        sha = (row.get("sha256") or "").strip()
        if not sha and image_ev:
            try:
                sh_row = fetchone(
                    db,
                    "SELECT sha256 FROM rag_image_assets WHERE job_artifact_id=:aid LIMIT 1",
                    {"aid": row["id"]},
                )
                sha = (sh_row or {}).get("sha256") or ""
            except Exception:
                sha = ""
        if not sha:
            return None, 0.0, "reuse"
        prior = fetchone(
            db,
            """SELECT o.ocr_text, o.confidence, o.engine
               FROM ocr_results o
               JOIN job_artifacts ja ON ja.id = o.job_artifact_id
               WHERE ja.sha256 = :sha
                 AND length(trim(coalesce(o.ocr_text, ''))) > 10
                 AND ja.id <> :aid
               ORDER BY o.created_at DESC
               LIMIT 1""",
            {"sha": sha, "aid": row["id"]},
        )
        if not prior:
            try:
                prior = fetchone(
                    db,
                    """SELECT ocr_text, ocr_confidence AS confidence, ocr_engine AS engine
                       FROM rag_image_assets
                       WHERE sha256 = :sha AND ocr_status = 'done'
                         AND length(trim(coalesce(ocr_text, ''))) > 10
                         AND (job_artifact_id IS NULL OR job_artifact_id <> :aid)
                       LIMIT 1""",
                    {"sha": sha, "aid": row["id"]},
                )
            except Exception:
                prior = None
        if prior and prior.get("ocr_text"):
            return (
                str(prior["ocr_text"]),
                float(prior.get("confidence") or 0.9),
                f"reuse:{prior.get('engine') or 'prior'}",
            )
        return None, 0.0, "reuse"

    work_rows: list[dict] = []
    for row in rows:
        row_path = str(row.get("file_path") or "")
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                import json as _json
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        quality_routed = str((meta or {}).get("ocr_quality_bucket") or "").lower() in {"true", "1", "yes"} or (meta or {}).get("ocr_quality_bucket") is True
        skip_photo = (not quality_routed) and (not image_ev) and (not forensic_photos_allowed()) and is_photo_like_path(row_path)
        if skip_photo or (is_os_vendor_ocr_path(row_path) and not quality_routed):
            execute(
                db,
                "UPDATE job_artifacts SET ocr_status='skipped', updated_at=NOW() WHERE id=:id",
                {"id": row["id"]},
            )
            db.commit()
            continue
        text, conf, engine = _reuse_ocr(row)
        if text:
            _persist_ocr_ok(row, text, conf, engine)
            db.commit()
            done += 1
            continue
        db.commit()
        row_copy = dict(row)
        row_copy["_quality_routed"] = bool(quality_routed)
        work_rows.append(row_copy)

    payloads: list[dict] = []
    preload = getattr(read_file_fn, "preload", None)
    if callable(preload) and work_rows and not gpu_only:
        try:
            preload([str(r.get("file_path") or "") for r in work_rows])
        except Exception as exc:
            log.warning("OCR batch file read failed: %s", exc)
    for r in work_rows:
        path = str(r.get("file_path") or "")
        data = None
        if not gpu_only:
            try:
                data = read_file_fn(r["file_path"])
            except Exception:
                data = None
        payloads.append(
            {
                "row": {"id": r["id"], "file_path": path},
                "data": data,
                "path": path,
                "max_pages": ocr_page_budget,
                "allow_photos": image_ev or forensic_photos_allowed() or bool(r.get("_quality_routed")),
            }
        )

    inflight: dict = {}
    payload_iter = iter(payloads)
    needs_gpu_rows: list[dict] = []

    def _fill(pool) -> None:
        while len(inflight) < cpu_n:
            try:
                nxt = next(payload_iter)
            except StopIteration:
                return
            inflight[pool.submit(_ocr_cpu_process_one, nxt)] = nxt

    if gpu_only:
        needs_gpu_rows = payloads
    else:
        with open_ocr_cpu_pool(cpu_n) as pool:

            _fill(pool)
            while inflight:
                finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    payload = inflight.pop(fut, None)
                    try:
                        result = fut.result()
                    except Exception as exc:
                        log.warning("CPU OCR worker failed: %s", exc)
                        continue
                    row = result.get("row") or {}
                    if result.get("status") == "ok" and result.get("text"):
                        log.info("OCR text-layer %s", row.get("file_path") or row.get("id"))
                        _persist_ocr_ok(
                            row,
                            str(result.get("text") or ""),
                            float(result.get("conf") or 0.0),
                            str(result.get("engine") or "pypdf"),
                        )
                        db.commit()
                        done += 1
                        continue
                    if result.get("status") == "needs_gpu":
                        needs_gpu_rows.append(dict(payload or {}) if payload else {"row": row})
                        continue
                    _mark_ocr_skip(row, engine=str(result.get("engine") or "na"))
                    db.commit()
                _fill(pool)

    if needs_gpu_rows and selected_ocr_engine() == "tesseract":
        from app.services.forensic_serial_policy import parallelism

        workers = parallelism(8)
        # The serial stage uses 0 as "every page". A long PDF then blocks the
        # fast pass. Keep the configured page budget, which is 8 by default.
        page_cap = int(ocr_page_budget or 0)
        if page_cap <= 0:
            page_cap = max(1, int(getattr(settings, "ocr_max_pages", 8) or 8))
        write_disk_log(
            db,
            job_id,
            f"OCR — Tesseract {len(needs_gpu_rows)} document(s), {workers} workers",
            stage="ocr",
        )
        write_ocr_live_progress(
            db,
            job_id,
            label=f"OCR — Tesseract, {len(needs_gpu_rows)} document(s)",
        )
        db.commit()
        allow_photos = forensic_photos_allowed()
        iter_grouped = getattr(read_file_fn, "iter_grouped", None)
        if not callable(iter_grouped):
            preload = getattr(read_file_fn, "preload", None)
            if callable(preload):
                try:
                    preload([str(payload.get("path") or "") for payload in needs_gpu_rows])
                except Exception as exc:
                    log.warning("Tesseract batch read failed: %s", exc)
            for payload in needs_gpu_rows:
                if payload.get("data") is None and read_file_fn is not None:
                    try:
                        payload["data"] = read_file_fn(payload.get("path") or "")
                    except Exception:
                        payload["data"] = None

        def _accept(result: dict) -> None:
            nonlocal done
            row = result.get("row") or {}
            if result.get("text"):
                _persist_ocr_ok(row, str(result["text"]), float(result["conf"] or 0.0), str(result["engine"]))
                done += 1
            else:
                _mark_ocr_skip(row, engine=str(result.get("engine") or "tesseract"))
            db.commit()
            _serial_ocr_tick(db, job_id, advanced=True, operation="Tesseract OCR")

        def _run_one(payload: dict, data):
            item = dict(payload)
            if data is not None:
                item["data"] = data
            return _tesseract_payload(
                item,
                read_file_fn=None if data is not None else read_file_fn,
                max_pages=page_cap,
                allow_default_photos=allow_photos,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            inflight: dict = {}

            def _drain(block: bool) -> None:
                if not inflight:
                    return
                if block:
                    finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                else:
                    finished = [fut for fut in inflight if fut.done()]
                for fut in finished:
                    inflight.pop(fut, None)
                    try:
                        _accept(fut.result())
                    except Exception as exc:
                        log.warning("Tesseract OCR failed: %s", exc)

            if callable(iter_grouped):
                by_path = {}
                for payload in needs_gpu_rows:
                    path = str(payload.get("path") or "")
                    by_path[path] = payload
                    by_path[path.replace("\\", "/")] = payload

                def _on_scan(_scanned, _yielded) -> None:
                    _serial_ocr_tick(db, job_id, advanced=True, operation="Tesseract OCR")

                for path, data in iter_grouped(
                    [str(payload.get("path") or "") for payload in needs_gpu_rows],
                    on_progress=_on_scan,
                ):
                    payload = by_path.get(path) or by_path.get(str(path).replace("\\", "/"))
                    if payload is None:
                        continue
                    inflight[pool.submit(_run_one, payload, data)] = path
                    if len(inflight) >= workers:
                        _drain(True)
            else:
                for payload in needs_gpu_rows:
                    inflight[pool.submit(_run_one, payload, payload.get("data"))] = payload.get("path")
                    if len(inflight) >= workers:
                        _drain(True)
            while inflight:
                _drain(True)
        needs_gpu_rows = []

    if needs_gpu_rows and (gpu or selected_ocr_engine() == "surya"):
        # Read the batch before taking the GPU slot. The slot must cover Surya,
        # not a multi-GB archive scan.
        _load_ocr_batch_bytes(read_file_fn, needs_gpu_rows)
        from app.services.job_locks import gpu_heavy_slot

        surya_ran = False
        # Serialize GPU-heavy workloads on a shared GPU host.  Earlier fail-open behaviour let
        # OCR overlap RAG after a slot timeout, causing VRAM/thermal spikes.
        # The Celery drain catches GpuHeavySlotTimeout and requeues cleanly.
        from contextlib import ExitStack
        from app.services.gpu_thermal import release_gpu_model_after_task_enabled

        with ExitStack() as gpu_stack:
            got_gpu = True if gpu_permit_owned else gpu_stack.enter_context(
                gpu_heavy_slot(reason="ocr", wait_sec=8.0, fail_closed=True)
            )
            if release_gpu_model_after_task_enabled() and not gpu_permit_owned:
                # External Surya owns its GPU memory; local cleanup is a no-op.
                gpu_stack.callback(release_ocr_client_resources)
            write_disk_log(
                db,
                job_id,
                (
                    f"OCR agent — {selected_ocr_engine()} {len(needs_gpu_rows)} scan(s)/image(s)"
                ),
                stage="ocr",
                metadata={
                    "gpu": gpu,
                    "device": device_label,
                    "surya_docs": len(needs_gpu_rows),
                    "slot": bool(got_gpu),
                },
            )
            write_ocr_live_progress(
                db,
                job_id,
                label=f"OCR — {selected_ocr_engine()}, {len(needs_gpu_rows)} scan(s)",
            )
            db.commit()
            surya_ran = True
            micro = _ocr_micro_batch_size()
            surya_load_logged = False

            def _save_ocr_doc(payload: dict, prep: dict, pieces: list[str], confs: list[float]) -> None:
                nonlocal done
                row = (payload.get("row") or {}) if isinstance(payload, dict) else {}
                if pieces:
                    text = "\n\n".join(pieces)
                    conf = sum(confs) / len(confs) if confs else 0.0
                    engine = (
                        selected_ocr_engine()
                        if prep.get("status") != "cpu_done"
                        else str(prep.get("engine") or "pypdf")
                    )
                    _persist_ocr_ok(row, text, conf, engine)
                    db.commit()
                    done += 1
                elif prep.get("status") == "skip":
                    _mark_ocr_skip(row, engine=str(prep.get("engine") or "na"))
                    db.commit()
                else:
                    _mark_ocr_skip(row, engine="surya_empty")
                    db.commit()
                write_ocr_live_progress(db, job_id, label="SuryaOCR progress")
                db.commit()
                _serial_ocr_tick(db, job_id, advanced=True)

            for start in range(0, len(needs_gpu_rows), micro):
                chunk = needs_gpu_rows[start : start + micro]
                prepared: list[tuple[dict, dict]] = []
                for payload in chunk:
                    _serial_ocr_tick(db, job_id, advanced=False)
                    row = (payload.get("row") or {}) if isinstance(payload, dict) else {}
                    path = str(payload.get("path") or row.get("file_path") or "")
                    data = payload.get("data")
                    if data is None:
                        try:
                            data = read_file_fn(path)
                        except Exception:
                            data = None
                    prep = cpu_prepare_ocr_item(
                        data or b"",
                        path=path,
                        max_pages=ocr_page_budget,
                        allow_photos=bool(payload.get("allow_photos")) or forensic_photos_allowed(),
                    )
                    prepared.append((payload, prep))
                pending_gpu: list[tuple[dict, dict, list[str], list[float], list[tuple[object, str]]]] = []
                for payload, prep in prepared:
                    pieces: list[str] = []
                    confs: list[float] = []
                    images: list[tuple[object, str]] = []
                    if prep.get("status") == "cpu_done" and prep.get("text"):
                        pieces.append(str(prep.get("text") or ""))
                        confs.append(float(prep.get("conf") or 0.9))
                    else:
                        for seg in prep.get("segments") or []:
                            if seg.get("type") == "text" and seg.get("text"):
                                pieces.append(str(seg["text"]))
                                confs.append(0.90)
                                continue
                            img = seg.get("image")
                            if img is None:
                                continue
                            images.append((img, str(seg.get("prompt") or "Text Recognition:")))
                    if images:
                        pending_gpu.append((payload, prep, pieces, confs, images))
                    else:
                        # Clear text is evidence already. Do not wait for the
                        # model download before the card can leave zero.
                        _save_ocr_doc(payload, prep, pieces, confs)
                if not pending_gpu:
                    continue
                if not surya_load_logged:
                    write_disk_log(
                        db,
                        job_id,
                        "Connecting SuryaOCR to external vLLM",
                        stage="ocr",
                    )
                    db.commit()
                    surya_load_logged = True
                grouped: dict[str, list[tuple[int, object]]] = {}
                for doc_i, (_payload, _prep, _pieces, _confs, images) in enumerate(pending_gpu):
                    for img, prompt in images:
                        grouped.setdefault(prompt, []).append((doc_i, img))
                for prompt, jobs in grouped.items():
                    try:
                        decoded = _surya_ocr_images([img for _i, img in jobs], prompt=prompt)
                    except Exception as exc:
                        log.warning("OCR micro-batch failed: %s", exc)
                        # A missing Surya server or failed vLLM response is an
                        # infrastructure failure, NOT an empty/processed document.
                        # Keep these rows unfinished for operator-supervised retry.
                        raise RuntimeError(f"SuryaOCR backend failed; batch remains retryable: {exc}") from exc
                    for (doc_i, _img), (text, conf) in zip(jobs, decoded):
                        if text:
                            pending_gpu[doc_i][2].append(text)
                            pending_gpu[doc_i][3].append(conf)
                for payload, prep, pieces, confs, _images in pending_gpu:
                    _save_ocr_doc(payload, prep, pieces, confs)
        gpu_deferred = bool(needs_gpu_rows) and not surya_ran
    elif needs_gpu_rows and not gpu:
        defer_msg, handed_off = handoff_surya_scans_to_worker(
            schema_name,
            job_id,
            len(needs_gpu_rows),
            cpu_bucket=ocr_bucket is not None,
        )
        write_disk_log(
            db,
            job_id,
            defer_msg,
            stage="ocr",
            metadata={
                "gpu": False,
                "surya_pending": len(needs_gpu_rows),
                "cpu_bucket": ocr_bucket is not None,
                "handed_to_cuda": handed_off,
            },
        )
        db.commit()
        gpu_deferred = True
    else:
        gpu_deferred = False

    pending_left = count_pending_ocr(
        db, job_id, ocr_bucket=ocr_bucket, ocr_buckets=ocr_buckets
    )
    lane = "GPU" if gpu else "CPU"
    write_disk_log(
        db,
        job_id,
        f"{lane} OCR batch — {done} document(s) on {device_label}"
        + (f"; {pending_left:,} still pending" if pending_left else " (queue empty)"),
        stage="ocr",
        metadata={"gpu": gpu, "device": device_label, "pending_left": pending_left},
    )
    db.commit()
    return {
        "status": "ok",
        "ocr_count": done,
        "engine": selected_ocr_engine() if gpu or selected_ocr_engine() == "surya" else "cpu-ocr",
        "gpu": gpu,
        "device": device_label,
        "pending_left": pending_left,
        "skipped_noise": skipped_noise,
        "gpu_deferred": bool(gpu_deferred),
        "reason": "",
    }

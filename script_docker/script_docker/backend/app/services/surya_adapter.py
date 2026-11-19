"""Surya 2.5 OCR integration against a separately managed OpenAI-compatible vLLM.

No network access is attempted during module import. We explicitly require an
already-running, administrator-configured Surya endpoint; the adapter does NOT
start a second inference service or try to install weights during case analysis.

Each call preserves page order. Missing/failed pages fail the batch rather than
becoming fabricated OCR results or an erroneous 100% completion record.
"""
from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import logging
import json
import os
import threading
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

log = logging.getLogger("surya_ocr")
_lock = threading.Lock()
_recognizer = None
_endpoint = None


class SuryaUnavailable(RuntimeError):
    """External Surya model or its client dependencies are not available."""


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div", "table", "tr", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_data(self, data):
        self.parts.append(data)

    def result(self):
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def _field(obj, field, default=None):
    return obj.get(field, default) if isinstance(obj, dict) else getattr(obj, field, default)


def _recognition_text(result) -> tuple[str, float]:
    """Extract v2 ``blocks[].html`` or legacy text-lines without guessing labels."""
    blocks = _field(result, "blocks")
    if blocks is None:
        blocks = _field(result, "text_lines", [])
    parts = []
    confidences = []
    for block in blocks or []:
        value = _field(block, "html") or _field(block, "text") or ""
        extractor = _TextExtractor()
        extractor.feed(str(value))
        body = unescape(extractor.result()).strip()
        if body:
            parts.append(body)
        confidence = _field(block, "confidence")
        if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
            confidences.append(float(confidence))
    # No invented 0.92 confidence. Surya doesn't always return a calibrated score.
    return "\n\n".join(parts), sum(confidences) / len(confidences) if confidences else 0.0


def _server_url() -> str:
    url = (os.environ.get("SURYA_INFERENCE_URL") or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if not url or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SuryaUnavailable("SURYA_INFERENCE_URL must be the running Surya vLLM endpoint ending in /v1")
    if not url.endswith("/v1"):
        raise SuryaUnavailable("SURYA_INFERENCE_URL must end in /v1")
    if os.environ.get("SURYA_INFERENCE_BACKEND", "llamacpp").strip().lower() not in {"vllm", "llamacpp"}:
        raise SuryaUnavailable("SURYA_INFERENCE_BACKEND must be vllm or llamacpp")
    return url


def _ready(endpoint: str, *, timeout=3) -> None:
    # Check before lazy initialization. Otherwise Surya may try to auto-launch
    # a vLLM server and compete for the forensic GPU.
    req = Request(endpoint + "/models", headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise SuryaUnavailable(f"Surya inference /models returned {resp.status}")
            payload = json.load(resp)
            models = [str(m.get("id", "")) for m in payload.get("data", []) if isinstance(m, dict)]
            expected = (os.environ.get("SURYA_SERVER_MODEL_ID") or "datalab-to/surya-ocr-2.5").strip()
            if expected not in models:
                raise SuryaUnavailable(
                    f"Surya server model identity mismatch: expected {expected!r}, got {models!r}. "
                    "Windows llama-server must be started with --alias " + expected +
                    "; cached model weights do not need to be downloaded again."
                )
    except (OSError, TimeoutError, ValueError) as exc:
        raise SuryaUnavailable(f"Surya inference is unreachable at {endpoint}: {exc}") from exc


def _get_recognizer():
    global _recognizer, _endpoint
    endpoint = _server_url()
    if _recognizer is not None and _endpoint == endpoint:
        return _recognizer
    with _lock:
        if _recognizer is not None and _endpoint == endpoint:
            return _recognizer
        _ready(endpoint)
        try:
            from surya.inference import SuryaInferenceManager
            from surya.recognition import RecognitionPredictor
            manager = SuryaInferenceManager()
            predictor = RecognitionPredictor(manager)
        except Exception as exc:
            raise SuryaUnavailable(f"Surya client initialization failed: {exc}") from exc
        _recognizer = predictor
        _endpoint = endpoint
        log.info("Attached SuryaOCR recognition client to %s", endpoint)
    return _recognizer


def recognize_pages(images: list) -> list[tuple[str, float]]:
    """Recognize image pages, preserving positions, in bounded vLLM batches."""
    if not images:
        return []
    predictor = _get_recognizer()
    max_batch = max(1, min(8, int(os.environ.get("SURYA_PAGE_BATCH", "4"))))
    out = []
    for start in range(0, len(images), max_batch):
        subset = images[start : start + max_batch]
        try:
            predictions = predictor(subset)
        except Exception as exc:
            raise SuryaUnavailable(f"Surya inference failed on page {start + 1}: {exc}") from exc
        if len(predictions) != len(subset):
            raise SuryaUnavailable(f"Surya returned {len(predictions)} pages for {len(subset)} inputs")
        out.extend(_recognition_text(r) for r in predictions)
    return out

"""Surya-only OCR regression tests; no model or GPU needed."""
from pathlib import Path
import pytest


def test_ocr_engine_rejects_removed_engine(monkeypatch):
    from app.services.ocr_gpu import selected_ocr_engine
    monkeypatch.setenv("OCR_ENGINE", "surya")
    assert selected_ocr_engine() == "surya"
    monkeypatch.setenv("OCR_ENGINE", "glm-ocr")
    with pytest.raises(ValueError):
        selected_ocr_engine()


def test_surya_dispatch_preserves_page_order(monkeypatch):
    from app.services import ocr_gpu, surya_adapter
    monkeypatch.setenv("OCR_ENGINE", "surya")
    monkeypatch.setattr(surya_adapter, "recognize_pages", lambda images: [(str(i), 0.0) for i in images])
    assert ocr_gpu._surya_ocr_images(["a", "b", "c"], prompt="text") == [("a", 0.0), ("b", 0.0), ("c", 0.0)]


def test_surya_errors_not_silenced(monkeypatch):
    from app.services import ocr_gpu, surya_adapter
    monkeypatch.setenv("OCR_ENGINE", "surya")
    def fail(images):
        raise RuntimeError("server disconnected")
    monkeypatch.setattr(surya_adapter, "recognize_pages", fail)
    with pytest.raises(RuntimeError, match="server disconnected"):
        ocr_gpu._surya_ocr_image("page", prompt="text")


def test_surya_wrong_batch_count_is_error(monkeypatch):
    from app.services import ocr_gpu, surya_adapter
    monkeypatch.setenv("OCR_ENGINE", "surya")
    monkeypatch.setattr(surya_adapter, "recognize_pages", lambda images: [("one", 0.0)])
    with pytest.raises(RuntimeError, match="pages for 2 inputs"):
        ocr_gpu._surya_ocr_images(["a", "b"], prompt="text")


def test_timeout_does_not_finish_partial_document(monkeypatch):
    from app.services import ocr_gpu, surya_adapter
    monkeypatch.setenv("OCR_ENGINE", "surya")
    monkeypatch.setenv("SURYA_PAGE_BATCH", "1")
    def recognize(pages):
        if pages[0] == "bad":
            raise TimeoutError("server timeout")
        return [("done", 0.6)]
    monkeypatch.setattr(surya_adapter, "recognize_pages", recognize)
    preparation = {"status":"needs_gpu", "path":"test.pdf", "segments":[{"image":"good"}, {"image":"bad"}]}
    with pytest.raises(RuntimeError, match="retryable"):
        ocr_gpu._gpu_ocr_prepared(preparation)


def test_tesseract_still_available(monkeypatch):
    from app.services.ocr_gpu import selected_ocr_engine
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    assert selected_ocr_engine() == "tesseract"


def test_surya_batch_reads_each_archive_once():
    from app.services.ocr_gpu import _load_ocr_batch_bytes

    calls = []

    def read(path):
        calls.append(("one", path))
        return b"slow"

    def grouped(paths, on_progress=None):
        calls.append(("pass", tuple(paths)))
        for path in paths:
            yield path, b"img-" + path.encode()

    read.iter_grouped = grouped
    payloads = [{"path": "a.jpg", "data": None}, {"path": "b.jpg", "data": None}]
    _load_ocr_batch_bytes(read, payloads)
    assert calls == [("pass", ("a.jpg", "b.jpg"))]
    assert payloads[0]["data"] == b"img-a.jpg"
    assert payloads[1]["data"] == b"img-b.jpg"

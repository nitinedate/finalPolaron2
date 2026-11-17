"""Polaron ESG letterhead, logo watermark, and circular confidential stamp."""

from __future__ import annotations

import io
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "static" / "report"

_PAPER_GRAY_MIN = 210
_BRAND_CHROMA = 40
_LETTERHEAD_PAD_X_RATIO = 0.04
_LETTERHEAD_PAD_Y_RATIO = 0.05
_LETTERHEAD_PAD_X_MIN = 18
_LETTERHEAD_PAD_Y_MIN = 8
# A little smaller than the source artwork, and far enough from the page edge
# that the left wing is not clipped when the band is fitted to A4.
_HEADER_LOGO_SCALE = 0.86
_HEADER_LEFT_INSET = 0.075
_HEADER_RIGHT_INSET = 0.04
_HEADER_ASPECT = 210 / 44


def _asset(name: str) -> bytes:
    path = _STATIC / name
    return path.read_bytes() if path.is_file() else b""


def _is_brand_pixel(red: int, green: int, blue: int, alpha: int = 255) -> bool:
    """True for logo/flag color and dark ink — not light gray page chrome."""
    if alpha < 16:
        return False
    if max(red, green, blue) - min(red, green, blue) >= _BRAND_CHROMA:
        return True
    return min(red, green, blue) < 160


def _is_paper_fill(red: int, green: int, blue: int) -> bool:
    return max(red, green, blue) - min(red, green, blue) < _BRAND_CHROMA and min(red, green, blue) >= _PAPER_GRAY_MIN


def _pad_letterhead_canvas(image: "Image.Image") -> "Image.Image":
    """Add safe white margins so the left logo/right accents never touch the page edge."""
    from PIL import Image

    rgb = image.convert("RGB")
    width, height = rgb.size
    pad_x = max(_LETTERHEAD_PAD_X_MIN, int(round(width * _LETTERHEAD_PAD_X_RATIO)))
    pad_y = max(_LETTERHEAD_PAD_Y_MIN, int(round(height * _LETTERHEAD_PAD_Y_RATIO)))
    out = Image.new("RGB", (width + 2 * pad_x, height + 2 * pad_y), (255, 255, 255))
    out.paste(rgb, (pad_x, pad_y))
    return out


def flatten_letterhead_band(image: "Image.Image") -> "Image.Image":
    """Crop gray page chrome and paint remaining paper to opaque white."""
    from PIL import Image

    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixels[x, y]
            if _is_brand_pixel(red, green, blue, alpha):
                left = min(left, x)
                top = min(top, y)
                right = max(right, x)
                bottom = max(bottom, y)
    if right < left or bottom < top:
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return _pad_letterhead_canvas(Image.alpha_composite(white, rgba).convert("RGB"))

    row_min = max(8, (right - left + 1) // 50)
    col_min = max(8, (bottom - top + 1) // 12)

    def brand_in_row(y: int) -> int:
        return sum(
            1
            for x in range(left, right + 1)
            if _is_brand_pixel(*pixels[x, y])
        )

    def brand_in_col(x: int) -> int:
        return sum(
            1
            for y in range(top, bottom + 1)
            if _is_brand_pixel(*pixels[x, y])
        )

    while top < bottom and brand_in_row(top) < row_min:
        top += 1
    while bottom > top and brand_in_row(bottom) < row_min:
        bottom -= 1
    while left < right and brand_in_col(left) < col_min:
        left += 1
    while right > left and brand_in_col(right) < col_min:
        right -= 1

    cropped = rgba.crop((left, top, right + 1, bottom + 1))
    pixels = cropped.load()
    crop_w, crop_h = cropped.size
    border = 3
    for y in range(crop_h):
        for x in range(crop_w):
            red, green, blue, alpha = pixels[x, y]
            on_edge = x < border or y < border or x >= crop_w - border or y >= crop_h - border
            paper = alpha < 16 or _is_paper_fill(red, green, blue)
            edge_gray = (
                on_edge
                and max(red, green, blue) - min(red, green, blue) < _BRAND_CHROMA
                and min(red, green, blue) >= 180
            )
            if paper or edge_gray:
                pixels[x, y] = (255, 255, 255, 255)
    white = Image.new("RGBA", cropped.size, (255, 255, 255, 255))
    return _pad_letterhead_canvas(Image.alpha_composite(white, cropped).convert("RGB"))


def _ink_bbox(image: "Image.Image") -> tuple[int, int, int, int] | None:
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            red, green, blue = pixels[x, y]
            if min(red, green, blue) < 245:
                left = min(left, x)
                top = min(top, y)
                right = max(right, x)
                bottom = max(bottom, y)
    if right < left or bottom < top:
        return None
    return left, top, right + 1, bottom + 1


def _header_split_x(image: "Image.Image") -> int | None:
    """X where the logo block ends and the right-hand accent begins."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()

    def ink(x: int) -> bool:
        return any(min(pixels[x, y]) < 245 for y in range(height))

    gap_start = None
    best = None
    for x in range(int(width * 0.28), int(width * 0.82)):
        if not ink(x):
            if gap_start is None:
                gap_start = x
        elif gap_start is not None:
            if best is None or x - gap_start > best[1] - best[0]:
                best = (gap_start, x)
            gap_start = None
    if best is None or best[1] - best[0] < 24:
        return None
    return best[0]


def prepare_header_png(raw: bytes) -> bytes:
    """Shrink the logo slightly and keep its left wing inside the page."""
    if not raw:
        return raw
    from PIL import Image

    flat = flatten_letterhead_band(Image.open(io.BytesIO(raw)))
    width, height = flat.size
    canvas_h = max(height, int(round(width / _HEADER_ASPECT)))
    canvas = Image.new("RGB", (width, canvas_h), (255, 255, 255))
    split = _header_split_x(flat)
    if split is None:
        box = _ink_bbox(flat)
        parts = [(flat.crop(box), True)] if box else []
    else:
        logo_box = _ink_bbox(flat.crop((0, 0, split, height)))
        accent_box = _ink_bbox(flat.crop((split, 0, width, height)))
        parts = []
        if logo_box:
            parts.append((flat.crop(logo_box), True))
        if accent_box:
            parts.append((flat.crop((split + accent_box[0], accent_box[1], split + accent_box[2], accent_box[3])), False))
    for crop, is_logo in parts:
        if is_logo:
            target_w = max(1, int(round(crop.width * _HEADER_LOGO_SCALE)))
            target_h = max(1, int(round(crop.height * _HEADER_LOGO_SCALE)))
            placed = crop.resize((target_w, target_h), Image.Resampling.LANCZOS)
            x = int(round(width * _HEADER_LEFT_INSET))
            y = max(0, (canvas_h - target_h) // 2)
        else:
            placed = crop
            x = width - placed.width - int(round(width * _HEADER_RIGHT_INSET))
            y = max(0, (canvas_h - placed.height) // 2)
        x = max(0, min(x, width - placed.width))
        y = max(0, min(y, canvas_h - placed.height))
        canvas.paste(placed, (x, y))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def white_letterhead_png(raw: bytes) -> bytes:
    """Return a letterhead band PNG with gray chrome removed and paper set to white."""
    if not raw:
        return raw
    from PIL import Image

    out = flatten_letterhead_band(Image.open(io.BytesIO(raw)))
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def rewrite_white_letterhead_assets() -> list[Path]:
    """Rewrite on-disk header/footer PNGs onto a white paper background."""
    written: list[Path] = []
    for name in ("letterhead-header.png", "letterhead-footer.png"):
        path = _STATIC / name
        if not path.is_file():
            continue
        flattened = white_letterhead_png(path.read_bytes())
        if flattened:
            path.write_bytes(flattened)
            written.append(path)
    return written


def apply_forensic_letterhead(pdf_bytes: bytes) -> bytes:
    """Stamp every page with letterhead header/footer, logo watermark, confidential seal."""
    if not pdf_bytes:
        return pdf_bytes
    try:
        import fitz
    except ImportError:
        return pdf_bytes

    header = prepare_header_png(_asset("letterhead-header.png"))
    footer = white_letterhead_png(_asset("letterhead-footer.png"))
    watermark = _asset("watermark.png")
    seal = _asset("confidential-stamp.png")
    if not (header and footer):
        return pdf_bytes

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        rect = page.rect
        header_h = rect.height * (44 / 297)
        footer_h = rect.height * (26 / 297)
        if header:
            page.insert_image(
                fitz.Rect(0, 0, rect.width, header_h),
                stream=header,
                overlay=True,
                keep_proportion=False,
            )
        if footer:
            page.insert_image(
                fitz.Rect(0, rect.height - footer_h, rect.width, rect.height),
                stream=footer,
                overlay=True,
                keep_proportion=False,
            )
        if watermark:
            wm_w = rect.width * 0.62
            wm_h = wm_w * 0.29
            page.insert_image(
                fitz.Rect(
                    (rect.width - wm_w) / 2,
                    (rect.height - wm_h) / 2,
                    (rect.width + wm_w) / 2,
                    (rect.height + wm_h) / 2,
                ),
                stream=watermark,
                overlay=False,
            )
        if seal:
            seal_size = min(rect.width, rect.height) * 0.145
            inset_x = 18
            inset_y = 22
            page.insert_image(
                fitz.Rect(
                    rect.width - seal_size - inset_x,
                    inset_y,
                    rect.width - inset_x,
                    inset_y + seal_size,
                ),
                stream=seal,
                overlay=True,
            )
        # Same "Page N" band the UI report reserves above the letterhead footer.
        page_band = fitz.Rect(0, rect.height - footer_h - (10 / 297) * rect.height, rect.width, rect.height - footer_h)
        page.insert_textbox(
            page_band,
            f"Page {page.number + 1}",
            fontsize=8,
            fontname="times-roman",
            color=(100 / 255, 116 / 255, 139 / 255),
            align=1,
        )
    out = doc.tobytes()
    doc.close()
    return out

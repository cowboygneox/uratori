"""Turning an uploaded file into a page fact and a word layer.

Everything in this module is a pure function of bytes already in hand: no
lock, no database, no network. The routes in `app.py` call these outside
`s.lock_for(tenant)` -- a multi-hundred-page rasterise or OCR pass inside the
tenant's lock would stall every other write to that tenant for as long as it
ran -- and take only the DB write of facts and the word layer under the lock
once parsing is done.

**Why pypdfium2, never PyMuPDF.** pypdfium2 ships Google's PDFium binary
under BSD/Apache-2.0; PyMuPDF is AGPL-3.0, which is incompatible with
distributing this project under its own licence (BUSL). Both can render a
page and read its text layer -- the licence is the whole reason, not a
capability gap.

**The coordinate contract** (`docs/http-api.md`): every word box is
page-normalised `[0,1]` in the *rendered* frame -- CropBox applied,
`/Rotate` applied, origin top-left, y down. pypdfium2's `render()` already
produces that frame as pixels (rotation and crop are baked into the bitmap),
so OCR word boxes need no transform beyond dividing by the image size. The
PDF text layer is the harder case: `get_charbox` answers in the page's
*unrotated* coordinate space (origin bottom-left, y up, relative to the
media box) regardless of `/Rotate`, so `_rendered_corner` below carries out
the same rotation pypdfium2's renderer performs internally, by hand, for
every character box. Verified against all four rotations with a scratch
script before this module was written (a small filled rectangle near one
corner, rendered, and the resulting pixel bounding box compared against
this function's prediction).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..lang.hash import version_of
from ..lang.plan import CompiledFact, Library
from .words import Word

RENDERER_VERSION = 1
"""Bumped whenever the rendering or word-extraction logic below changes in
a way that could move a box or a page's text -- the render cache's key and
`words_sha` both include it, so an upgrade invalidates stale renders and
pages re-ingested under it get a fresh `words_sha` (a fact change every
extract downstream notices, D1/D4)."""

PAGE_KEY_WIDTH = 4
"""`<document_id>/p0007` -- zero-padded so page keys sort the way pages
read: `/p0002` before `/p0010`, unlike the unpadded form (where `/10` sorts
before `/2` as a string) in every key-ordered surface and in `FieldPick`'s
same-instant tie-break."""

# A generous default; large multi-year bundles get a bigger explicit limit
# via URATORI_DOCUMENT_MAX_BYTES rather than a silent one.
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Tesseract wants a few hundred DPI to read small print reliably; 1/72in
# canvas units times this scale is roughly 300 DPI.
OCR_RENDER_SCALE = 300 / 72


class DocumentKindError(Exception):
    """A direct write or delete against a document or page kind through the
    facts route -- refused in the route (`app.py`), never in `verify.py`,
    whose shared `verify_writes` ignores deletes by design and the facade
    shares it (documents-plan-v3, D1, review finding 6)."""


def document_kinds(library: Library) -> dict[str, str]:
    """Every document kind in this library, mapped to its one page kind."""
    page_of: dict[str, str] = {}
    for fact in library.facts.values():
        if fact.shape == "page" and fact.page_of is not None:
            page_of[fact.page_of] = fact.name
    return {
        fact.name: page_of[fact.name]
        for fact in library.facts.values()
        if fact.shape == "document" and fact.name in page_of
    }


def document_and_page_kinds(library: Library) -> frozenset[str]:
    """Every kind a direct fact write or delete must be refused against:
    every document kind and every page kind, in one set the facts route
    checks membership against."""
    kinds = document_kinds(library)
    return frozenset(kinds) | frozenset(kinds.values())


def refuse_document_kind_writes(
    library: Library,
    writes: Mapping[str, Mapping[str, Mapping[str, object]]] | None,
    deletes: Mapping[str, Sequence[str]] | None,
) -> None:
    """The facts route's door for document-shaped kinds: a write or a
    delete naming one is refused whole, naming the documents routes
    instead. `verify_writes` cannot make this refusal itself (it ignores
    deletes on purpose, and the facade shares it with embedding hosts that
    have no documents routes to point at), so it lives here, called by
    `POST /tenants/{t}/facts` before anything else runs.
    """
    guarded = document_and_page_kinds(library)
    offending = sorted((set(writes or {}) | set(deletes or {})) & guarded)
    if offending:
        names = ", ".join(offending)
        raise DocumentKindError(
            f'"{names}" {"is" if len(offending) == 1 else "are"} a document or page '
            "kind. These move only through the documents routes (POST/GET/DELETE "
            f"/tenants/{{tenant}}/documents/{{kind}}), which keep a document's bytes, "
            "its pages and its facts in step; the facts route never writes or "
            "deletes them directly."
        )


def page_key(document_id: str, number: int) -> str:
    return f"{document_id}/p{number:0{PAGE_KEY_WIDTH}d}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uploaded_at_now() -> str:
    """The upload moment, as the document shape's `uploaded_at as moment`
    field expects it: ISO-8601, UTC."""
    return datetime.now(UTC).isoformat()


def document_id_of(sha256: str) -> str:
    """The fact key for a document: the first 16 hex characters of its
    sha256, so re-uploading identical bytes is idempotent by construction
    rather than a second record of the same file."""
    return sha256[:16]


def words_sha_of(words: Sequence[Word]) -> str:
    """The word layer's build hash -- the page shape's `words_sha` field.
    Covers the renderer version and every word's text and position, so a
    re-ingest under a better OCR pass (or this module's own next revision)
    changes it, which is a fact change every extract downstream notices."""
    return version_of(
        {
            "renderer": RENDERER_VERSION,
            "words": [
                [w.id, w.text, w.x0, w.y0, w.x1, w.y1, w.line, w.block, w.source, w.confidence]
                for w in words
            ],
        }
    )


@dataclass(frozen=True)
class IngestedPage:
    words: tuple[Word, ...]
    text_source: Literal["pdf", "ocr", "none"]


@dataclass(frozen=True)
class IngestedDocument:
    pages: tuple[IngestedPage, ...]
    mime: str = "application/pdf"


def parse_fact(library: Library, kind: str) -> CompiledFact:
    fact = library.facts.get(kind)
    if fact is None:  # pragma: no cover - callers already checked document_kinds
        raise DocumentKindError(f'"{kind}" is not a fact kind.')
    return fact


# --------------------------------------------------------------- parsing --


def _rotation(raw: int) -> Literal[0, 90, 180, 270]:
    value = raw % 360
    if value not in (0, 90, 180, 270):  # pragma: no cover - PDFium only emits these
        raise ValueError(f"unsupported page rotation {raw}")
    return value  # type: ignore[return-value]


def _rendered_corner(
    x: float, y: float, w0: float, h0: float, rotation: Literal[0, 90, 180, 270]
) -> tuple[float, float, float, float]:
    """One point of the page's *unrotated* coordinate space (origin
    bottom-left, y up, already relative to the CropBox origin), carried
    through to the rendered frame (origin top-left, y down, `/Rotate`
    applied) -- plus the rendered canvas's own width and height, since a
    90/270 rotation swaps them. Matches what pypdfium2's own `render()`
    produces as pixels; see the module docstring for how this was checked.
    """
    x0, y0 = x, h0 - y  # unrotated visual frame: top-left origin, y down
    if rotation == 0:
        return x0, y0, w0, h0
    if rotation == 90:
        return h0 - y0, x0, h0, w0
    if rotation == 180:
        return w0 - x0, h0 - y0, w0, h0
    return y0, w0 - x0, h0, w0  # 270


@dataclass(frozen=True)
class _RawBox:
    """A word's text and rendered-frame box, before the id and line/block
    it is assigned once every word on the page is known. Typed rather than
    a `dict[str, object]` so the float/int conversions below are checked,
    not merely hoped for.

    `base_y0`/`base_y1` are the *unrotated* visual frame's y-band (origin
    top-left, y down, before `/Rotate` is applied) -- rotation-independent,
    so two words on the same content-stream line always share a y-band
    here regardless of the page's declared rotation. Used only for line
    clustering (`_assign_lines`); `y0`/`y1` (and `x0`/`x1`) stay the
    rendered-frame box every reader of a `Word` actually sees."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    base_y0: float = 0.0
    base_y1: float = 0.0
    line: int = 0
    block: int = 0
    confidence: float | None = None


def _pdf_text_words(page: object) -> list[_RawBox]:
    """Every word of a PDF page's own text layer, in reading order, boxed
    in the rendered frame and normalised to `[0,1]`. Returns `[]` for a
    page with no text layer (scans, faxes, image-only pages) -- the caller
    falls back to OCR."""
    textpage = page.get_textpage()  # type: ignore[attr-defined]
    count = textpage.count_chars()
    if count == 0:
        return []
    text = textpage.get_text_range()
    crop_l, crop_b, crop_r, crop_t = page.get_cropbox()  # type: ignore[attr-defined]
    w0 = crop_r - crop_l
    h0 = crop_t - crop_b
    rotation = _rotation(page.get_rotation())  # type: ignore[attr-defined]

    words: list[_RawBox] = []
    buf: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    base_ys: list[float] = []

    def flush() -> None:
        if not buf:
            return
        words.append(
            _RawBox(
                text="".join(buf),
                x0=min(xs),
                y0=min(ys),
                x1=max(xs),
                y1=max(ys),
                base_y0=min(base_ys),
                base_y1=max(base_ys),
            )
        )
        buf.clear()
        xs.clear()
        ys.clear()
        base_ys.clear()

    for i in range(count):
        char = text[i] if i < len(text) else ""
        if char.isspace() or char == "":
            flush()
            continue
        left, bottom, right, top = textpage.get_charbox(i)
        for px, py in (
            (left - crop_l, bottom - crop_b),
            (left - crop_l, top - crop_b),
            (right - crop_l, bottom - crop_b),
            (right - crop_l, top - crop_b),
        ):
            rx, ry, cw, ch = _rendered_corner(px, py, w0, h0, rotation)
            xs.append(rx / cw)
            ys.append(ry / ch)
            # The same corner's y in the UNROTATED visual frame (origin
            # top-left, y down, before `/Rotate`) -- `_rendered_corner`'s
            # own first step, repeated here rather than threaded out of it,
            # so line clustering below groups by the text's own layout, not
            # by what a rotation did to it.
            base_ys.append((h0 - py) / h0)
        buf.append(char)
    flush()
    return words


def _assign_lines(words: Sequence[_RawBox]) -> list[int]:
    """Deterministic line grouping over a PDF text layer, which carries no
    structural "line" of its own (unlike Tesseract's `line_num`): a word
    joins the most recent line whose vertical band it overlaps, in reading
    order, else opens a new one. Good enough for the simple, single-column
    layouts this MR's fixtures and matcher vocabulary target; a genuinely
    multi-column page is future work for the matcher vocabulary (D4), not
    this extraction step.

    Clustered in the text's own (unrotated) frame -- `word.base_y0`/
    `base_y1` -- deliberately, **not** the rendered `y0`/`y1`: on a
    `/Rotate 90` page, a content-stream line that reads as one line of
    text becomes a column of words once rotated into the rendered frame,
    and clustering on the rendered box would hand every word of it its own
    single-word "line". `Word.line`/`Word.block` describe the text's own
    layout (which words a same-line matcher, D4, reads together), not a
    property of how the page happens to be rotated for display -- the
    rendered box is still what every box on the wire reports."""
    bands: list[tuple[float, float]] = []
    out: list[int] = []
    for word in words:
        mid = (word.base_y0 + word.base_y1) / 2
        height = max(word.base_y1 - word.base_y0, 1e-6)
        placed = False
        for i, (lo, hi) in enumerate(bands):
            if lo - height / 2 <= mid <= hi + height / 2:
                bands[i] = (min(lo, word.base_y0), max(hi, word.base_y1))
                out.append(i)
                placed = True
                break
        if not placed:
            bands.append((word.base_y0, word.base_y1))
            out.append(len(bands) - 1)
    return out


def _ocr_words(image: Any) -> list[_RawBox]:
    """Every word Tesseract finds on an already-rendered page image, boxed
    in that image's own pixel frame (which is the rendered frame: `render()`
    already applied CropBox and `/Rotate`) and normalised to `[0,1]`."""
    import pytesseract

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    w_img, h_img = image.size
    out: list[_RawBox] = []
    count = len(data["text"])
    for i in range(count):
        text = str(data["text"][i]).strip()
        if not text:
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            # Tesseract's own "this is a layout node, not a word" marker.
            continue
        left, top = float(data["left"][i]), float(data["top"][i])
        width, height = float(data["width"][i]), float(data["height"][i])
        out.append(
            _RawBox(
                text=text,
                x0=left / w_img,
                y0=top / h_img,
                x1=(left + width) / w_img,
                y1=(top + height) / h_img,
                line=int(data["line_num"][i]),
                block=int(data["block_num"][i]),
                confidence=conf,
            )
        )
    return out


def _ingest_page(page: object) -> IngestedPage:
    raw = _pdf_text_words(page)
    if raw:
        lines = _assign_lines(raw)
        words = tuple(
            Word(
                id=i,
                text=w.text,
                x0=w.x0,
                y0=w.y0,
                x1=w.x1,
                y1=w.y1,
                line=lines[i],
                block=0,
                source="pdf",
                confidence=None,
            )
            for i, w in enumerate(raw)
        )
        return IngestedPage(words=words, text_source="pdf")

    bitmap = page.render(scale=OCR_RENDER_SCALE)  # type: ignore[attr-defined]
    image = bitmap.to_pil()
    raw_ocr = _ocr_words(image)
    if raw_ocr:
        words = tuple(
            Word(
                id=i,
                text=w.text,
                x0=w.x0,
                y0=w.y0,
                x1=w.x1,
                y1=w.y1,
                line=w.line,
                block=w.block,
                source="ocr",
                confidence=w.confidence,
            )
            for i, w in enumerate(raw_ocr)
        )
        return IngestedPage(words=words, text_source="ocr")
    return IngestedPage(words=(), text_source="none")


def ingest_pdf(data: bytes) -> IngestedDocument:
    """Parse a PDF's bytes into its pages' word layers. Pure and
    synchronous -- CPU-bound (rasterising, OCR) -- so every caller runs it
    through `asyncio.to_thread`, outside any lock."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        pages = tuple(_ingest_page(doc[i]) for i in range(len(doc)))
    finally:
        doc.close()
    return IngestedDocument(pages=pages)


def render_page_png(data: bytes, page_number: int, scale: float) -> bytes:
    """Rasterise one page (1-based `page_number`) to PNG bytes at `scale`
    (PDF canvas units per pixel). Pure and synchronous; callers run it
    through `asyncio.to_thread`."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        if not 1 <= page_number <= len(doc):
            raise ValueError(f"page {page_number} does not exist in a {len(doc)}-page document")
        page = doc[page_number - 1]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


# ----------------------------------------------------------- render cache --


class RenderCache:
    """A bounded on-disk LRU of rendered page images, beside the blobs --
    never read on the hot path of a fact pass, only by the page-image
    route. Keyed `(sha256, page, scale, renderer version)`: a renderer
    upgrade (`RENDERER_VERSION`) or a different zoom is a cache miss, never
    a stale image served under a new key's name.

    Eviction scans the cache directory's file sizes and ages on every
    write and deletes the oldest (by mtime) until back under the cap --
    simple rather than indexed, which is an acceptable cost at the
    thousands-of-entries scale one tenant's rendered pages reach, and a
    deliberate simplification over a size-tracking index this package does
    not need yet.
    """

    def __init__(self, root: str | Path, max_bytes: int = 512 * 1024 * 1024) -> None:
        self._root = Path(root)
        self._max_bytes = max_bytes

    def _path(self, tenant: str, sha256: str, page: int, scale: float) -> Path:
        name = f"{sha256}-p{page:0{PAGE_KEY_WIDTH}d}-s{scale:g}-r{RENDERER_VERSION}.png"
        return self._root / tenant / sha256[:2] / name

    def get(self, tenant: str, sha256: str, page: int, scale: float) -> bytes | None:
        path = self._path(tenant, sha256, page, scale)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        with contextlib.suppress(FileNotFoundError):  # pragma: no cover - raced with an evict
            path.touch()  # bump mtime: this entry was just used
        return data

    def put(self, tenant: str, sha256: str, page: int, scale: float, data: bytes) -> None:
        path = self._path(tenant, sha256, page, scale)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if not self._root.exists():
            return
        entries = []
        total = 0
        for path in self._root.rglob("*.png"):
            try:
                stat = path.stat()
            except FileNotFoundError:  # pragma: no cover - raced with an evict
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= self._max_bytes:
            return
        for _mtime, size, path in sorted(entries):
            if total <= self._max_bytes:
                break
            try:
                path.unlink()
                total -= size
            except FileNotFoundError:  # pragma: no cover - raced with another evict
                pass

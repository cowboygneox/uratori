"""Tiny synthetic PDFs for the documents tests.

Built with reportlab in-process rather than committed as binary fixtures --
`reportlab` is a dev-only dependency (`pyproject.toml`'s `dev` extra), BSD
licensed, considered over fpdf2 (LGPL-3.0-only) for exactly that reason.
Every PDF here is a handful of KB and built fresh per call, so there is
nothing to keep in sync with the code that reads it.
"""

from __future__ import annotations

import io


def _image_with_text(text: str) -> object:
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    img = Image.new("RGB", (800, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 80), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def sample_bundle_pdf() -> bytes:
    """A three-page bundle:

    1. An ordinary text-layer page (an identity line and a weight line).
    2. An image-only page -- no text layer, drawn as pixels, so the ingest
       pipeline's OCR fallback is exercised rather than its text-layer path.
    3. A flowsheet page: three dated rows, for `many by row` matchers later
       in the plan (D4) and as a shape the example in `examples/records/`
       reuses.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    c.drawString(72, 700, "Patient Chart")
    c.drawString(72, 680, "MRN: 004412")
    c.drawString(72, 660, "Weight: 82 kg")
    c.showPage()

    c.drawImage(_image_with_text("Weight: 91 kg"), 50, 600, width=400, height=100)
    c.showPage()

    y = 700
    for date, weight in (("2024-01-02", "80"), ("2024-02-03", "81"), ("2024-03-04", "82")):
        c.drawString(72, y, f"Date: {date}  Wt: {weight} kg")
        y -= 20
    c.showPage()

    c.save()
    return buf.getvalue()


def rotated_page_pdf() -> bytes:
    """One page, `/Rotate 90` -- scans are routinely rotated, and this is
    the fixture the coordinate-transform tests (and the ingest pipeline's
    own correctness) are checked against."""
    import pypdfium2 as pdfium
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "Rotated: Weight: 77 kg")
    c.showPage()
    c.save()

    doc = pdfium.PdfDocument(buf.getvalue())
    try:
        doc[0].set_rotation(90)
        out = io.BytesIO()
        doc.save(out)
    finally:
        doc.close()
    return out.getvalue()


def blank_page_pdf() -> bytes:
    """One page with no text layer and no image -- a genuinely blank page,
    which should ingest with `text_source == "none"` rather than crash OCR
    on an empty image."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.showPage()
    c.save()
    return buf.getvalue()

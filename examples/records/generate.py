"""Synthetic medical-chart PDFs for the `examples/records` worked example
(documents-plan-v3, D4/D5).

**No real PHI.** "778123", "550219", every date and every vitals reading
below is invented for this file -- there is no person behind either MRN,
and the numbers are chosen to exercise the engine (a carried-forward
height, a page in pounds and inches, a rotated scan, an OCR'd page, a
flowsheet, two pages that deliberately cannot be read), not to resemble
any real chart.

Deterministic by construction: every value below is a literal, not drawn
from a random source, so `build_bundle()` returns byte-identical PDFs on
every call. reportlab (BSD) builds them in-process, the same dev-only
pattern `tests/pdf_fixtures.py` uses for the engine's own document tests
-- `pyproject.toml`'s `dev` extra already carries reportlab for exactly
this reason, so nothing here needs a new dependency, in the package
extras or the Docker image.

Run directly, this writes the bundle to `examples/records/data/` so the
PDFs can be opened by hand:

    python examples/records/generate.py

`load.py` and `tests/test_records_example.py` both call `build_bundle()`
in-process instead; neither touches a file on disk.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent

# Two invented people. A real chart carries a medical record number, never
# just a name (names collide and are mistyped; an MRN does not) -- the
# whole reason `page_identity` exists (docs/documents.md, D4).
PATIENT_A = "778123"
"""Years of visits. Height is measured exactly once, early, in the first
page's own pounds-and-inches units; every later visit is weight-only, in
kilograms, under three different spellings -- the carry-forward story
`docs/documents.md` and this example's README walk end to end."""

PATIENT_B = "550219"
"""A second, much smaller chart: two visits, each with both measurements,
under the two spellings `PATIENT_A`'s chart never needs ("Ht:" and
"Height"). Not part of the carry-forward narrative -- it exists only to
round out the spelling list the extract's `measurement` declaration
carries."""

# ----------------------------------------------------------- patient A's --

A_HEIGHT_FT = 5
A_HEIGHT_IN = 9
A_HEIGHT_CM = A_HEIGHT_FT * 30.48 + A_HEIGHT_IN * 2.54  # 175.26

A_VISIT1_DATE = "2018-11-01"
A_VISIT1_WEIGHT_LB = 160
A_VISIT1_WEIGHT_KG = A_VISIT1_WEIGHT_LB * 0.45359237  # ~72.5748
"""`A_VISIT1_DATE` is deliberately within the last decade, not further
back: `carried forward`'s own `MAX_CARRY_BUCKETS` ceiling (`carry.py`,
documents-plan-v3 D5) refuses to carry a value more than 3,660 buckets --
about ten years of days -- from its own change up to *now*, wherever now
has got to when a pass actually runs. A date here any older would start
failing this example's own carry-forward story as real time passes, not
because anything is broken, but because D5 says plainly that ten-plus
years of history hits this ceiling. If this file is still being read
after 2028 or so, move this date forward again."""

A_VISIT2_DATE = "2020-02-15"
A_VISIT2_WEIGHT_KG = 74.0

A_VISIT3_DATE = "2021-09-10"
A_VISIT3_WEIGHT_KG = 75.0

A_VISIT4_DATE = "2022-11-20"
A_VISIT4_WEIGHT_KG = 76.0

A_VISIT5_DATE = "2023-09-01"  # the /Rotate 90 page
A_VISIT5_WEIGHT_KG = 77.0

A_VISIT6_DATE = "2024-06-18"  # the image-only (OCR) page
A_VISIT6_WEIGHT_KG = 78.0

A_FLOWSHEET_DATES = ("2025-04-01", "2025-04-08", "2025-04-15")
A_FLOWSHEET_WEIGHTS_KG = (80.0, 81.0, 82.0)

A_FAILURE_DATE = "2026-01-10"
A_FAILURE_WEIGHT_PRINTED = 79
"""Weight printed with no unit at all, and `measurement.weight_kg`
declares two (`in kg or lb`) -- a deliberate failure: D4 refuses to guess
which one "79" means."""

# ----------------------------------------------------------- patient B's --

B_VISIT1_DATE = "2026-02-02"
B_VISIT1_HEIGHT_CM = 165.0
B_VISIT1_WEIGHT_KG = 60.0

B_VISIT2_DATE = "2026-05-05"
B_VISIT2_HEIGHT_CM = 166.0
B_VISIT2_WEIGHT_KG = 61.0

# ------------------------------------------------------------- misfiled --

MISFILED_DATE = "2026-06-06"
MISFILED_WEIGHT_KG = 50.0
"""A page with vitals on it and no identifier anywhere -- a deliberate
failure: `page_identity` writes no record, and D4 says filing a
measurement under nobody is worse than filing nothing."""


@dataclass(frozen=True)
class Bundle:
    documents: dict[str, bytes]
    """Filename -> PDF bytes, ready for `POST /tenants/{t}/documents/{kind}`."""

    patients: dict[str, dict[str, Any]]
    """The `patient` roster, ready for `POST /tenants/{t}/facts` -- D5's
    `[sean]` note: nothing patient-specific lives in the engine, so this
    roster is an ordinary host write, same door any provider's record
    goes through."""


def _header(c: Any, mrn: str | None) -> None:
    c.drawString(72, 700, "Patient Chart")
    c.drawString(72, 680, "VITAL SIGNS")
    if mrn is not None:
        c.drawString(72, 660, f"MRN: {mrn}")


def _vitals_page(c: Any, mrn: str | None, line: str) -> None:
    """One page: the chart header, then one combined `Date: ... <vitals>`
    line -- every matcher in `definitions.fig` reads a value from the same
    line its label is on, so a visit's fields always share one line, the
    same shape `docs/language.md`'s own worked example uses."""
    _header(c, mrn)
    c.drawString(72, 640, line)
    c.showPage()


def _image_page(c: Any, mrn: str, date: str, weight_line: str) -> None:
    """A page with no text layer at all -- the chart's header and one
    vitals line, drawn as pixels, so ingest falls through to its OCR path
    (`docs/setup.md`) instead of reading a PDF text layer."""
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    img = Image.new("RGB", (900, 260), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), "Patient Chart", fill="black")
    draw.text((20, 80), "VITAL SIGNS", fill="black")
    draw.text((20, 140), f"MRN: {mrn}", fill="black")
    draw.text((20, 200), f"Date: {date}  {weight_line}", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    c.drawImage(ImageReader(buf), 50, 420, width=450, height=130)
    c.showPage()


def _chart_a_bytes() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    # `invariant=1`: reportlab otherwise stamps the real wall-clock
    # `CreationDate`/`ModDate` (and a random-looking file id) into every
    # PDF it writes, which is the one thing that kept this file's own
    # "byte-identical on every call" claim true only within a single
    # process's one `datetime.now()` call, never across two actual runs
    # of `generate.py` -- exactly how the README's pasted `document_id`s
    # (content-derived, D1) stopped matching a fresh run (whole-review,
    # section 3). Every `canvas.Canvas(...)` in this file takes it.
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)

    # 1: the only height this chart ever carries, printed in pounds and
    # inches -- the carried-forward value every later day on this
    # patient reads.
    _vitals_page(
        c,
        PATIENT_A,
        f"Date: {A_VISIT1_DATE}  Wt: {A_VISIT1_WEIGHT_LB} lb  "
        f"Ht: {A_HEIGHT_FT} ft {A_HEIGHT_IN} in",
    )
    # 2-4: weight-only visits, one spelling each.
    _vitals_page(c, PATIENT_A, f"Date: {A_VISIT2_DATE}  Weight: {A_VISIT2_WEIGHT_KG:g} kg")
    _vitals_page(c, PATIENT_A, f"Date: {A_VISIT3_DATE}  Wt: {A_VISIT3_WEIGHT_KG:g} kg")
    _vitals_page(
        c, PATIENT_A, f"Date: {A_VISIT4_DATE}  WEIGHT (kg): {A_VISIT4_WEIGHT_KG:g} kg"
    )
    # 5: this page is rotated 90 degrees after the fact, below.
    _vitals_page(c, PATIENT_A, f"Date: {A_VISIT5_DATE}  Wt: {A_VISIT5_WEIGHT_KG:g} kg")
    # 6: image-only, OCR'd.
    _image_page(c, PATIENT_A, A_VISIT6_DATE, f"Weight: {A_VISIT6_WEIGHT_KG:g} kg")
    # 7: a flowsheet -- three dated rows, `many by row`'s own anchor field
    # (`measured_at`) finding one match per line.
    _header(c, PATIENT_A)
    c.drawString(72, 640, "Flowsheet")
    y = 620
    for date, weight in zip(A_FLOWSHEET_DATES, A_FLOWSHEET_WEIGHTS_KG, strict=True):
        c.drawString(72, y, f"Date: {date}  Wt: {weight:g} kg")
        y -= 20
    c.showPage()
    # 8: the deliberate failure -- a weight with no unit at all.
    _vitals_page(c, PATIENT_A, f"Date: {A_FAILURE_DATE}  Weight: {A_FAILURE_WEIGHT_PRINTED}")
    c.save()
    return buf.getvalue()


_PDF_ID_PAIR = re.compile(rb"/ID\[<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>\]")
"""`pdfium.PdfDocument.save` stamps its own second, run-to-run-random file
id into the trailer's `/ID` array on every re-save (the first id, derived
from the source document, stays stable) -- `invariant=1` on reportlab's
own `Canvas` (above) does not reach this second save at all, since it
runs after reportlab is done. Confirmed by generating twice a second
apart: everything but this one hex string matched byte for byte. Caught
and neutralised in `_rotate_page`, below (review finding H)."""


def _stabilize_pdf_id(data: bytes) -> bytes:
    """Replace each `/ID` pair's second (random) id with a fixed,
    same-length placeholder, so the file's byte length and every xref
    offset after it are untouched -- only the cosmetic id itself changes,
    never anything a reader or `pdfium` parses as content."""

    def _fixed(m: re.Match[bytes]) -> bytes:
        first, second = m.group(1), m.group(2)
        return b"/ID[<" + first + b"><" + b"0" * len(second) + b">]"

    return _PDF_ID_PAIR.sub(_fixed, data)


def _rotate_page(pdf_bytes: bytes, page_index: int, degrees: int) -> bytes:
    """Set one page's `/Rotate` after the fact -- reportlab draws upright
    content; a scanner's own rotation is a page attribute, not a drawing,
    so it is applied post-hoc with pypdfium2, the same as
    `tests/pdf_fixtures.py`'s `rotated_vitals_line_pdf`."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        doc[page_index].set_rotation(degrees)
        out = io.BytesIO()
        doc.save(out)
    finally:
        doc.close()
    return _stabilize_pdf_id(out.getvalue())


def _chart_b_bytes() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    _vitals_page(
        c,
        PATIENT_B,
        f"Date: {B_VISIT1_DATE}  Ht: {B_VISIT1_HEIGHT_CM:g} cm  "
        f"Weight: {B_VISIT1_WEIGHT_KG:g} kg",
    )
    _vitals_page(
        c,
        PATIENT_B,
        f"Date: {B_VISIT2_DATE}  Height: {B_VISIT2_HEIGHT_CM:g} cm  "
        f"Weight: {B_VISIT2_WEIGHT_KG:g} kg",
    )
    c.save()
    return buf.getvalue()


def _misfiled_bytes() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    _vitals_page(c, None, f"Date: {MISFILED_DATE}  Wt: {MISFILED_WEIGHT_KG:g} kg")
    c.save()
    return buf.getvalue()


def build_bundle() -> Bundle:
    chart_a = _rotate_page(_chart_a_bytes(), page_index=4, degrees=90)
    documents = {
        "chart_a.pdf": chart_a,
        "chart_b.pdf": _chart_b_bytes(),
        "misfiled.pdf": _misfiled_bytes(),
    }
    patients = {
        PATIENT_A: {"mrn": PATIENT_A},
        PATIENT_B: {"mrn": PATIENT_B},
    }
    return Bundle(documents=documents, patients=patients)


def main() -> None:
    bundle = build_bundle()
    out = HERE / "data"
    out.mkdir(exist_ok=True)
    for name, data in bundle.documents.items():
        path = out / name
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")


if __name__ == "__main__":
    main()

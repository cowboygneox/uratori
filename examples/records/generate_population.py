"""A twelve-patient synthetic population for the `examples/records` worked
example (documents-plan-v3, D4/D5) -- `generate.py`'s two-chart bundle makes
one engine trace legible; this file exists to make the *population* case
legible instead: a dozen patients, a dozen files, uploaded one file per
patient, each a believable multi-year chart rather than a handful of pages
built to hit one engine path apiece.

**No real PHI.** Every name below is invented for this file alone (picked
for no reason beyond not repeating across patients); every MRN is a
synthetic six-digit number in the `600xxx` block, chosen only to sit
outside `generate.py`'s own `778123`/`550219`; every date, weight and
height is a literal computed from the constants in `ROSTER`, not drawn
from a random source for anything that must compare equal across runs.
`random.Random(POPULATION_SEED)` is used in exactly one place -- the
"noisy" weight trend -- and always re-seeded fresh from the same constant,
so `build_population()` is as byte-stable as `generate.py`'s
`build_bundle()` (same `invariant=1` canvases, same post-hoc `/Rotate`
and PDF-id stabilisation, reused directly from `generate.py` rather than
re-implemented).

**The date window.** Every visit below falls between 2023-01-01 and
2025-12-31, comfortably inside `carried forward`'s own ten-year
`MAX_CARRY_BUCKETS` ceiling (`docs/language.md`, "On-change data";
`generate.py`'s own `A_VISIT1_DATE` note says the same thing) as measured
from any run between now and roughly 2033. If this file is still being
read after that, move the window forward, not the design.

**Reuses `generate.py` rather than forking it:** the page header, the
image-only (OCR) page renderer, the post-hoc page rotation and PDF-id
stabilisation are the same three building blocks the single-patient
bundle already uses (`_header`, `_image_page`, `_rotate_page`), imported
from it directly so there is exactly one implementation of "how this
example draws a page" and one of "how reportlab's and pdfium's own
timestamps get neutralised".

## The roster

Three clinic "templates" cover every spelling and unit combination
`measurement` declares (`definitions.fig`): `weight_kg` in kg or lb,
`height_cm` in cm, in or ft+in.

| template | weight | height |
|---|---|---|
| A | `Wt: <n> kg` | `Ht: <n> cm` |
| B | `Weight (lb): <n> lb` | `Height: <n> ft <n> in` |
| C | `WEIGHT <n> kg` | `HEIGHT <n> in` |

(Template B's weight line repeats the unit after the number on purpose --
`measurement.weight_kg` declares two units, so a number needs *something*
printed right after it to say which one; the `(lb)` in the label is the
clinic's own annotation, not what the matcher reads.)

| # | name | MRN | age | sex | cadence | template(s) | trend | quirk |
|---|---|---|---|---|---|---|---|---|
| 1 | Odalys Ferreira | 600001 | 69 | F | frequent (18 monthly) | A | steady loss | rotated page, a plain OCR page that reads fine, an OCR-*degraded* page that fails outright, 2 labs, 1 discharge, **receives patient 10's misfiled page** |
| 2 | Marcus Oyelaran | 600002 | 64 | M | frequent (18 monthly) | A -> C (visit 13) | steady gain | last 3 visits merged into one flowsheet, 1 lab, 1 discharge, fax cover at front |
| 3 | Agnes Toumaschat | 600003 | 42 | F | routine (11, ~100d) | B | stable | height measured twice; one rotated page |
| 4 | Desmond Okoronkwo | 600004 | 77 | M | routine (11, ~95d) | A -> B (visit 6) | steady gain | 1 lab, fax cover at front |
| 5 | Priya Nandakumar | 600005 | 35 | F | routine (11, ~100d) | C | steady loss | 1 plain OCR page -- Tesseract merges "WEIGHT" into the number and the weight is quietly absent that day |
| 6 | Lucien Belanger | 600006 | 24 | M | routine (11, ~90d) | A | stable | 1 lab, "Body-Wt:" spelling-miss *extra* page |
| 7 | Beatrix Olumide | 600007 | 61 | F | routine (11, ~95d) | B | steady gain | **never measures height** -- BMI absent, stated |
| 8 | Tobias Lindqvist | 600008 | 29 | M | routine (11, ~90d) | C -> A (visit 6) | noisy | height measured twice; 2 visits merged into one flowsheet; 1 discharge |
| 9 | Soraya Ibarra | 600009 | 19 | F | routine (11, ~85d) | A | steady gain | 1 plain OCR page that also fails ("Wt" survives, "57.4kg" doesn't parse as a number -- a genuine failure, unlike patient 5's silent absence); unitless-weight *failure* page (extra) |
| 10 | Hugo Castellanos | 600010 | 82 | M | sparse (2) | B | n/a | visit 2's header prints patient 1's MRN -- a misfiled page, not this patient's |
| 11 | Wren Abimbola | 600011 | 50 | F | sparse (1) | C | n/a | single visit |
| 12 | Felix Dzhaparidze | 600012 | 31 | M | sparse (1, year 3 only) | A | n/a | single visit, 2025 only |

Patient 7's BMI is absent -- not zero -- on every day she is weighed,
because `patient.height`'s `latest(... ) carried forward` has nothing to
carry when no `measurement.height_cm` was ever written for her, and
`patient.bmi`'s division (`uratori/engine/evaluate.py::_arith`) answers
`None` for any non-numeric operand, never a number. Patient 10's
misfiled page is not an engine failure at all -- `page_identity` reads
whatever MRN is printed, `measurement.patient_id` copies it verbatim, and
the record lands under patient 1 because that is genuinely the MRN on
that page. Nothing in the engine cross-checks a page's printed identifier
against the file it arrived in (D4: identity comes from the page, never
the filename), which is exactly the risk this one page is here to show.

**On "four deliberate failures":** only two of this roster's four quirks
land on `measurement`'s own failures route the way the single-patient
bundle's "no printed unit" page does -- the unitless weight (patient 9)
and the OCR-degraded page (patient 1), both a value *found but
unreadable*, which `uratori/documents/extract.py`'s own `_match_record`
treats as a hard `ExtractFailure`. The other two are not failures by the
language's own design: a spelling no alternative covers (patient 6) is
never *found*, so it is `_Absent` -- a quiet omission, the same as a
weight-only visit's missing height -- and the misfiled page (patient 10)
reads and copies cleanly, just under the wrong patient. A third, unplanned
failure showed up once this bundle actually ran against Tesseract:
patient 9's plain (non-degraded) OCR page also fails outright ("Wt"
survives OCR, "57.4kg" does not parse as a number), while patient 5's
otherwise-identical plain OCR page fails silently instead ("WEIGHT"
merges into the number, so `_find_alternative` never matches it at all).
All three are pinned by `tests/test_records_population.py` against
whatever this environment's Tesseract actually produces, the same
posture `tests/test_records_example.py` already takes on its own OCR
page.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any, Literal

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import generate  # type: ignore[import-not-found]  # noqa: E402

POPULATION_SEED = 20250101
"""Re-seeded fresh on every call that needs it -- never a module-level
`Random()` instance -- so two calls to `build_population()` in the same
process, or two separate process runs, draw the same "noisy" sequence."""

PageKind = Literal[
    "note", "lab", "discharge", "fax_cover", "ocr_good", "ocr_degraded"
]

TemplateName = Literal["A", "B", "C"]

LB_PER_KG = 1.0 / 0.45359237
IN_PER_CM = 1.0 / 2.54


@dataclass(frozen=True)
class Visit:
    date: str
    weight_kg: float | None
    height_cm: float | None = None
    template: TemplateName = "A"
    kind: PageKind = "note"
    rotate: bool = False
    weight_unitless: bool = False
    """The deliberate "a number with no printed unit" failure: prints the
    bare number with nothing after it, for a field that declares two."""
    spelling_miss: bool = False
    """The deliberate spelling the bundle's three templates never cover
    (`Body-Wt:`, a single token so the per-word matcher's own
    case/colon-insensitive normalisation -- which would otherwise let a
    bare "wt" anywhere on the line satisfy the `Wt:` alternative -- does
    not accidentally rescue it; see the module docstring of
    `tests/test_records_population.py` for the check that proves this)."""
    header_mrn_override: str | None = None
    """Set only on patient 10's second visit: the MRN actually printed on
    this page, a different (real) patient's."""
    flowsheet_group: str | None = None
    """Consecutive visits sharing a group id are drawn as one flowsheet
    page (several dated rows) instead of one page each."""


@dataclass(frozen=True)
class PatientSpec:
    mrn: str
    name: str
    age: int
    sex: Literal["F", "M"]
    cadence: Literal["frequent", "routine", "sparse"]
    visits: tuple[Visit, ...]


def _template_line(template: TemplateName, visit: Visit) -> str:
    """One visit's vitals line, under its own template's spelling and
    units -- the three rows of the module docstring's own table."""
    weight_kg = visit.weight_kg
    assert weight_kg is not None
    parts = [f"Date: {visit.date}"]

    if visit.weight_unitless:
        parts.append(f"Weight: {weight_kg:.10g}")
    elif visit.spelling_miss:
        parts.append(f"Body-Wt: {weight_kg:.10g} kg")
    elif template == "A":
        parts.append(f"Wt: {weight_kg:.10g} kg")
    elif template == "B":
        parts.append(f"Weight (lb): {weight_kg * LB_PER_KG:.10g} lb")
    else:
        parts.append(f"WEIGHT {weight_kg:.10g} kg")

    if visit.height_cm is not None:
        if template == "A":
            parts.append(f"Ht: {visit.height_cm:.10g} cm")
        elif template == "B":
            total_in = visit.height_cm * IN_PER_CM
            feet = int(total_in // 12)
            inches = total_in - feet * 12
            parts.append(f"Height: {feet} ft {inches:.10g} in")
        else:
            parts.append(f"HEIGHT {visit.height_cm * IN_PER_CM:.10g} in")

    return "  ".join(parts)


def _lab_page(c: Any, mrn: str, date: str) -> None:
    """A lab report: the patient's own identifier, so `page_identity`
    still reads it, but no "VITAL SIGNS"/"Vitals"/"Wt:" anywhere -- the
    page `page_class` must never classify as vitals, and so `measurement`
    (gated `over page_class.vitals`) must never read.

    `page_class`'s own `WordLadder` has no `otherwise` rung
    (`definitions.fig`), so a page it cannot classify is not a quiet
    absence the way an "after"-matcher's miss would be -- it is a hard
    `ExtractFailure` (`uratori/documents/extract.py::_word_ladder`'s
    plain `"no alternative matched"` return, never special-cased into
    `_Absent` the way `NumberAfter`/`DateAfter`/`TextAfter` are). Every
    lab, discharge and fax-cover page in this population failed to
    classify -- the first time anything in this repo's test suite
    exercises that path, since every page `generate.py` ever built
    carries "VITAL SIGNS" in its header."""
    c.drawString(72, 700, "Patient Chart")
    c.drawString(72, 680, "LABORATORY REPORT")
    c.drawString(72, 660, f"MRN: {mrn}")
    c.drawString(72, 640, f"Date: {date}  Glucose: 95 mg/dL  Sodium: 140 mmol/L")
    c.showPage()


def _discharge_page(c: Any, mrn: str, date: str) -> None:
    c.drawString(72, 700, "Patient Chart")
    c.drawString(72, 680, "DISCHARGE SUMMARY")
    c.drawString(72, 660, f"MRN: {mrn}")
    c.drawString(72, 640, f"Date: {date}  Diagnosis: resolved, follow-up in 3 months")
    c.showPage()


def _fax_cover_page(c: Any, date: str) -> None:
    """No identifier anywhere -- `page_identity` finds nothing, which
    (its own extract has exactly one field) is the same "no alternative
    matched for any field" failure `generate.py`'s `misfiled.pdf` already
    demonstrates, reproduced here by a different kind of page."""
    c.drawString(72, 700, "FAX COVER SHEET")
    c.drawString(72, 680, "To: Records Department")
    c.drawString(72, 660, "From: Referring Clinic")
    c.drawString(72, 640, f"Date: {date}  Pages: 3")
    c.showPage()


def _flowsheet_page(c: Any, mrn: str, group: list[Visit]) -> None:
    c.drawString(72, 700, "Patient Chart")
    c.drawString(72, 680, "VITAL SIGNS")
    c.drawString(72, 660, f"MRN: {mrn}")
    c.drawString(72, 640, "Flowsheet")
    y = 620
    for visit in group:
        line = _template_line(visit.template, visit)
        # Drop the "Date: ..." prefix _template_line already repeats and
        # keep the row to one line, same shape as generate.py's own
        # flowsheet.
        c.drawString(72, y, line)
        y -= 20
    c.showPage()


def _patient_pdf_bytes(patient: PatientSpec) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    rotate_pages: list[int] = []
    page_index = 0

    visits = list(patient.visits)
    i = 0
    while i < len(visits):
        visit = visits[i]
        if visit.flowsheet_group is not None:
            group = [visit]
            j = i + 1
            while j < len(visits) and visits[j].flowsheet_group == visit.flowsheet_group:
                group.append(visits[j])
                j += 1
            _flowsheet_page(c, patient.mrn, group)
            page_index += 1
            i = j
            continue

        mrn = visit.header_mrn_override or patient.mrn
        if visit.kind == "lab":
            _lab_page(c, mrn, visit.date)
        elif visit.kind == "discharge":
            _discharge_page(c, mrn, visit.date)
        elif visit.kind == "fax_cover":
            _fax_cover_page(c, visit.date)
        elif visit.kind == "ocr_good":
            generate._image_page(
                c, mrn, visit.date, _template_line(visit.template, visit).split("  ", 1)[1]
            )
        elif visit.kind == "ocr_degraded":
            _degraded_image_page(c, mrn, visit)
        else:
            generate._header(c, mrn)
            c.drawString(72, 640, _template_line(visit.template, visit))
            c.showPage()
        if visit.rotate:
            rotate_pages.append(page_index)
        page_index += 1
        i += 1

    c.save()
    data = buf.getvalue()
    for page in rotate_pages:
        data = generate._rotate_page(data, page_index=page, degrees=90)
    return data


def _degraded_image_page(c: Any, mrn: str, visit: Visit) -> None:
    """A scanned page rendered deliberately hard to OCR: light-grey text
    (low contrast against white) on a slightly rotated canvas -- "may or
    may not read" is the honest framing (documents-plan-v3's own posture
    on OCR fidelity, echoed in `generate.py`'s good-OCR page and
    `tests/test_records_example.py`'s comment on it); this repository's
    test pins whichever way Tesseract in this environment actually comes
    down, rather than asserting a specific reading the fidelity of a
    third-party OCR engine cannot promise."""
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    img = Image.new("RGB", (900, 260), "white")
    draw = ImageDraw.Draw(img)
    grey = (170, 170, 170)
    draw.text((20, 20), "Patient Chart", fill=grey)
    draw.text((20, 80), "VITAL SIGNS", fill=grey)
    draw.text((20, 140), f"MRN: {mrn}", fill=grey)
    draw.text((20, 200), f"Date: {visit.date}  {_template_line(visit.template, visit).split('  ', 1)[1]}", fill=grey)
    img = img.rotate(3, expand=True, fillcolor="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    c.drawImage(ImageReader(buf), 50, 400, width=450, height=150)
    c.showPage()


def _dates(start: str, count: int, step_days: int) -> list[str]:
    from datetime import date, timedelta

    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=step_days * i)).isoformat() for i in range(count)]


def _trend(
    kind: Literal["loss", "gain", "stable", "noisy"],
    base: float,
    count: int,
    rate: float = 0.3,
    rng: Random | None = None,
) -> list[float]:
    if kind == "loss":
        return [round(base - rate * i, 2) for i in range(count)]
    if kind == "gain":
        return [round(base + rate * i, 2) for i in range(count)]
    if kind == "stable":
        wobble = (0.3, -0.1, 0.0, -0.2)
        return [round(base + wobble[i % len(wobble)], 2) for i in range(count)]
    assert rng is not None
    return [round(base + rng.uniform(-1.5, 1.5), 2) for i in range(count)]


def _build_roster() -> list[PatientSpec]:
    rng = Random(POPULATION_SEED)

    # ---- patient 1: frequent, steady loss, template A throughout ------
    p1_dates = _dates("2023-06-01", 18, 30)
    p1_weights = _trend("loss", 88.0, 18, rate=0.35)
    p1_visits = [
        Visit(date=d, weight_kg=w, height_cm=(175.0 if i == 0 else None), template="A")
        for i, (d, w) in enumerate(zip(p1_dates, p1_weights, strict=True))
    ]
    p1_visits[5] = Visit(
        date=p1_visits[5].date, weight_kg=p1_visits[5].weight_kg, template="A", rotate=True
    )
    p1_visits[8] = Visit(
        date=p1_visits[8].date, weight_kg=p1_visits[8].weight_kg, template="A", kind="ocr_good"
    )
    patient_1 = PatientSpec(
        mrn="600001",
        name="Odalys Ferreira",
        age=69,
        sex="F",
        cadence="frequent",
        visits=(
            *p1_visits,
            Visit(date="2023-08-15", weight_kg=86.0, template="A", kind="lab"),
            Visit(date="2024-02-15", weight_kg=82.0, template="A", kind="lab"),
            Visit(date="2024-05-01", weight_kg=80.0, template="A", kind="discharge"),
            Visit(date="2024-07-01", weight_kg=79.0, template="A", kind="ocr_degraded"),
        ),
    )

    # ---- patient 2: frequent, steady gain, A -> C switch, flowsheet ----
    p2_dates = _dates("2024-01-01", 18, 30)
    p2_weights = _trend("gain", 75.0, 18, rate=0.4)
    p2_visits = []
    for i, (d, w) in enumerate(zip(p2_dates, p2_weights, strict=True)):
        template_2: TemplateName = "A" if i < 13 else "C"
        height = 168.0 if i == 0 else None
        group = "p2_flow" if i >= 15 else None
        p2_visits.append(
            Visit(date=d, weight_kg=w, height_cm=height, template=template_2, flowsheet_group=group)
        )
    patient_2 = PatientSpec(
        mrn="600002",
        name="Marcus Oyelaran",
        age=64,
        sex="M",
        cadence="frequent",
        visits=(
            Visit(date="2023-12-20", weight_kg=None, template="A", kind="fax_cover"),
            *p2_visits,
            Visit(date="2024-04-01", weight_kg=76.0, template="A", kind="lab"),
            Visit(date="2024-09-01", weight_kg=78.0, template="C", kind="discharge"),
        ),
    )

    # ---- patient 3: routine, stable, template B, height twice ----------
    p3_dates = _dates("2023-01-15", 11, 100)
    p3_weights = _trend("stable", 70.0, 11)
    p3_visits = [
        Visit(
            date=d,
            weight_kg=w,
            height_cm=(162.0 if i == 0 else (163.0 if i == 6 else None)),
            template="B",
        )
        for i, (d, w) in enumerate(zip(p3_dates, p3_weights, strict=True))
    ]
    p3_visits[2] = Visit(
        date=p3_visits[2].date, weight_kg=p3_visits[2].weight_kg, template="B", rotate=True
    )
    patient_3 = PatientSpec(
        mrn="600003", name="Agnes Toumaschat", age=42, sex="F", cadence="routine",
        visits=tuple(p3_visits),
    )

    # ---- patient 4: routine, steady gain, A -> B switch -----------------
    p4_dates = _dates("2023-03-01", 11, 95)
    p4_weights = _trend("gain", 95.0, 11, rate=0.5)
    p4_visits = [
        Visit(
            date=d,
            weight_kg=w,
            height_cm=(182.0 if i == 0 else None),
            template=("A" if i < 6 else "B"),
        )
        for i, (d, w) in enumerate(zip(p4_dates, p4_weights, strict=True))
    ]
    patient_4 = PatientSpec(
        mrn="600004",
        name="Desmond Okoronkwo",
        age=77,
        sex="M",
        cadence="routine",
        visits=(
            Visit(date="2023-02-15", weight_kg=None, template="A", kind="fax_cover"),
            *p4_visits,
            Visit(date="2024-01-10", weight_kg=97.0, template="B", kind="lab"),
        ),
    )

    # ---- patient 5: routine, steady loss, template C --------------------
    p5_dates = _dates("2023-02-01", 11, 100)
    p5_weights = _trend("loss", 60.0, 11, rate=0.25)
    p5_visits = [
        Visit(date=d, weight_kg=w, height_cm=(158.0 if i == 0 else None), template="C")
        for i, (d, w) in enumerate(zip(p5_dates, p5_weights, strict=True))
    ]
    p5_visits[3] = Visit(
        date=p5_visits[3].date, weight_kg=p5_visits[3].weight_kg, template="C", kind="ocr_good"
    )
    patient_5 = PatientSpec(
        mrn="600005", name="Priya Nandakumar", age=35, sex="F", cadence="routine",
        visits=tuple(p5_visits),
    )

    # ---- patient 6: routine, stable, template A, spelling-miss ----------
    p6_dates = _dates("2023-05-01", 11, 90)
    p6_weights = _trend("stable", 78.0, 11)
    p6_visits = [
        Visit(date=d, weight_kg=w, height_cm=(174.0 if i == 0 else None), template="A")
        for i, (d, w) in enumerate(zip(p6_dates, p6_weights, strict=True))
    ]
    patient_6 = PatientSpec(
        mrn="600006",
        name="Lucien Belanger",
        age=24,
        sex="M",
        cadence="routine",
        visits=(
            *p6_visits,
            Visit(date="2023-09-01", weight_kg=77.5, template="A", kind="lab"),
            Visit(date="2023-12-01", weight_kg=77.0, template="A", spelling_miss=True),
        ),
    )

    # ---- patient 7: routine, steady gain, template B, NO height ever ---
    p7_dates = _dates("2023-04-01", 11, 95)
    p7_weights = _trend("gain", 85.0, 11, rate=0.3)
    patient_7 = PatientSpec(
        mrn="600007",
        name="Beatrix Olumide",
        age=61,
        sex="F",
        cadence="routine",
        visits=tuple(
            Visit(date=d, weight_kg=w, height_cm=None, template="B")
            for d, w in zip(p7_dates, p7_weights, strict=True)
        ),
    )

    # ---- patient 8: routine, noisy, C -> A switch, height twice --------
    p8_dates = _dates("2023-06-15", 11, 90)
    p8_weights = _trend("noisy", 68.0, 11, rng=rng)
    p8_visits = []
    for i, (d, w) in enumerate(zip(p8_dates, p8_weights, strict=True)):
        template_8: TemplateName = "C" if i < 6 else "A"
        height = 170.0 if i == 0 else (171.0 if i == 6 else None)
        group = "p8_flow" if i in (8, 9) else None
        p8_visits.append(
            Visit(date=d, weight_kg=w, height_cm=height, template=template_8, flowsheet_group=group)
        )
    patient_8 = PatientSpec(
        mrn="600008",
        name="Tobias Lindqvist",
        age=29,
        sex="M",
        cadence="routine",
        visits=(
            *p8_visits,
            Visit(date="2024-02-01", weight_kg=69.0, template="A", kind="discharge"),
        ),
    )

    # ---- patient 9: routine, steady gain, template A, unitless weight ---
    p9_dates = _dates("2023-01-01", 11, 85)
    p9_weights = _trend("gain", 55.0, 11, rate=0.4)
    p9_visits = [
        Visit(date=d, weight_kg=w, height_cm=(160.0 if i == 0 else None), template="A")
        for i, (d, w) in enumerate(zip(p9_dates, p9_weights, strict=True))
    ]
    p9_visits[6] = Visit(
        date=p9_visits[6].date, weight_kg=p9_visits[6].weight_kg, template="A", kind="ocr_good"
    )
    patient_9 = PatientSpec(
        mrn="600009",
        name="Soraya Ibarra",
        age=19,
        sex="F",
        cadence="routine",
        visits=(
            *p9_visits,
            Visit(date="2023-10-01", weight_kg=57, template="A", weight_unitless=True),
        ),
    )

    # ---- patient 10: sparse (2), template B, misfiled 2nd page ----------
    patient_10 = PatientSpec(
        mrn="600010",
        name="Hugo Castellanos",
        age=82,
        sex="M",
        cadence="sparse",
        visits=(
            Visit(date="2023-03-10", weight_kg=79.0, height_cm=173.0, template="B"),
            Visit(date="2025-09-10", weight_kg=74.0, template="B", header_mrn_override="600001"),
        ),
    )

    # ---- patient 11: sparse (1), template C ------------------------------
    patient_11 = PatientSpec(
        mrn="600011",
        name="Wren Abimbola",
        age=50,
        sex="F",
        cadence="sparse",
        visits=(Visit(date="2024-07-01", weight_kg=66.0, height_cm=164.0, template="C"),),
    )

    # ---- patient 12: sparse (1, year 3 only), template A -----------------
    patient_12 = PatientSpec(
        mrn="600012",
        name="Felix Dzhaparidze",
        age=31,
        sex="M",
        cadence="sparse",
        visits=(Visit(date="2025-11-01", weight_kg=74.0, height_cm=180.0, template="A"),),
    )

    return [
        patient_1, patient_2, patient_3, patient_4, patient_5, patient_6,
        patient_7, patient_8, patient_9, patient_10, patient_11, patient_12,
    ]


ROSTER: list[PatientSpec] = _build_roster()
"""Built once at import time -- every constant in it is a literal or a
`Random(POPULATION_SEED)` draw re-seeded inside `_build_roster()`, so
re-importing or re-calling `build_population()` always reproduces the
same roster, which is what byte-stability depends on."""


@dataclass(frozen=True)
class Population:
    documents: dict[str, bytes]
    """`patient-<mrn>.pdf` -> bytes, one file per patient -- the point of
    this bundle over `generate.py`'s is one upload per patient, not one
    upload for several."""

    patients: dict[str, dict[str, Any]]
    """The `patient` roster write, same shape as `generate.py`'s."""


def build_population() -> Population:
    documents = {
        f"patient-{patient.mrn}.pdf": _patient_pdf_bytes(patient) for patient in ROSTER
    }
    patients = {patient.mrn: {"mrn": patient.mrn} for patient in ROSTER}
    return Population(documents=documents, patients=patients)


def main() -> None:
    population = build_population()
    out = HERE / "data" / "population"
    out.mkdir(parents=True, exist_ok=True)
    for name, data in population.documents.items():
        path = out / name
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")


if __name__ == "__main__":
    main()

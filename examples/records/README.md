# A patient's records, defined

The worked example for `extract` (documents-plan-v3, D4/D5): a years-long
chart as a PDF bundle in, body-mass index out, with every number traced
back to the words on a page. This is the story `docs/documents.md` points
at.

**Everything in it is invented.** `generate.py` builds a small synthetic
bundle with [reportlab](https://www.reportlab.com/) -- two made-up
patients, two made-up medical record numbers, dates and vitals chosen to
exercise the engine, not to resemble anyone. There is no real PHI
anywhere in this directory.

| | |
|---|---|
| `schema.json` | Empty -- the world is declared in `definitions.fig`, same as the NFL example's. |
| `definitions.fig` | The facts (a document, its pages, the identity and classification extracted from them, a reading of vitals), the extracts that read them, the three figures that turn vitals into a BMI series, and a second reader (`audit`) over every page. |
| `generate.py` | Writes the synthetic bundle: three PDFs, deterministic, fixed dates. |
| `generate_population.py` | Writes a twelve-patient population: one PDF per patient, deterministic, 2023-2025 -- see "Population sample" below. |
| `load.py` | The host: teaches the engine, pushes the `patient` roster, uploads the bundle, prints the trace below. `--population` loads the twelve-patient bundle instead. |
| `author.py` | The authoring aid -- reads the failures route and asks Claude to propose the alternatives a declaration is missing. Never run by the server. |

## Run it

```bash
docker compose up -d
python examples/records/load.py --base http://localhost:8080
```

`load.py` calls `generate.py`'s `build_bundle()` directly -- nothing is
read from disk. To look at the PDFs themselves:

```bash
python examples/records/generate.py   # writes examples/records/data/*.pdf
```

## What the bundle contains

One patient (`778123`) carries a years-long chart in `chart_a.pdf`:

- a first visit with both height and weight, printed in pounds and
  inches (`Wt: 160 lb`, `Ht: 5 ft 9 in`) -- the only height this chart
  ever carries;
- four later visits, weight only, each under a different spelling the
  bundle's templates actually use (`Weight:`, `Wt:`, `WEIGHT (kg):`);
- one of those later pages scanned sideways (`/Rotate 90`);
- one visit scanned as a photo with no text layer at all, read back by
  OCR;
- a flowsheet page with three dated rows, one `measurement` record per
  row (`many by row`);
- one page where a weight is printed with no unit at all -- a
  deliberate failure, because `weight_kg` declares two (`in kg or lb`)
  and "79" beside neither is not a reading.

A second, much smaller chart (`chart_b.pdf`, patient `550219`) exists
only to show the two spellings the first chart never needs (`Ht:`,
`Height`), each with both measurements on the same day.

A third file (`misfiled.pdf`) is a single page with vitals on it and no
identifier anywhere -- a second deliberate failure: `page_identity`
writes no record for a page with no name on it, and every extract that
copies from it fails rather than filing a measurement under nobody.

## The trace

Running `load.py` against a fresh tenant prints this (edited only for the
BMI series, whose height-carrying rows -- one per day between the single
height measurement and today, mostly absent -- are the cost `docs/
language.md`'s `carried forward` section states plainly and this output
does not reproduce in full):

```
engine dev at http://localhost:8080
library loaded: 3 extracts, 3 figures

pushing the patient roster (2 patients)
uploading chart_a.pdf (9810 bytes) ...
  -> id af49ca08103e9694, 8 page(s), written=1
uploading chart_b.pdf (1956 bytes) ...
  -> id 8f374f880e25ae71, 2 page(s), written=1
uploading misfiled.pdf (1403 bytes) ...
  -> id 1d27c6e09380c623, 1 page(s), written=1

patient.bmi for 778123 (days with a weight on record):
  778123@2018-11-01: 23.6
  778123@2020-02-15: 24.1
  778123@2021-09-10: 24.4
  778123@2022-11-20: 24.7
  778123@2023-09-01: 25.1
  778123@2024-06-18: 25.4
  778123@2025-04-01: 26.0
  778123@2025-04-08: 26.4
  778123@2025-04-15: 26.7

evidence for patient.bmi?subject=778123@2020-02-15:
  part: patient.weight
  part: patient.height

evidence for patient.weight?subject=778123@2020-02-15:
  field weight_kg: page af49ca08103e9694/p0002, printed '74 kg', 2 box(es)

evidence for patient.height?subject=778123@2020-02-15:
  field height_cm: page af49ca08103e9694/p0001, printed '5 ft 9 in', 4 box(es)

failures for measurement:
  1d27c6e09380c623/p0001#r1 field=patient_id: no page_identity record on this page
  af49ca08103e9694/p0008#r1 field=weight_kg: a number with no printed unit, and more than one unit is declared

loaded. Try:
  http://localhost:8080/ui/  (tenant "records")
```

Read the evidence block the way a reviewer would: BMI on 2020-02-15 is
two parts, weight and height. Weight's own citation is page 2 of the
chart, "74 kg", the plain-text visit on that same day. Height's
citation is page **1** -- the one visit, years earlier, where it was
actually measured in pounds and inches -- because `patient.height`
carries it forward across every day nobody re-measured it, and its
evidence always names the record it carried from, never a fabrication
on today's page.

The two failures are the bundle's own deliberate ones, named exactly as
the failures route states them: a weight with no printed unit, and a
page with no identifier at all.

## The audit

`definitions.fig` also declares a second reader over every page
(documents-plan-v3, D6): `audit medical_record_page.vitals_audit`,
verifying `page_identity` and `measurement`. It compiles and serves with
no provider configured at all -- every page simply stays `unaudited`,
stated plainly on the declaration page and at `GET /tenants/{t}/audits/
medical_record_page.vitals_audit/findings`, never a silent gap. Setting
`URATORI_AUDIT_PROVIDER=fake` starts the worker under
`uratori.audit.fake.FakeAuditProvider` -- no network, no model, a
deterministic scan of the same word layer the extract already read --
and `claude` is the real second reader, behind the `audit` extra; see
`docs/setup.md`'s "Audits and PHI egress" before pointing it at bytes
that matter. `tests/test_records_example.py` runs the fake provider over
this bundle and forces two genuine disputes: a page the reader is told
has no weight although `measurement` plainly has one (`disagrees`), and
the bundle's own no-printed-unit failure page, where a reader claiming a
weight is a `missed` because no record exists to agree or disagree with.

## The authoring loop

`measurement`'s alternatives in `definitions.fig` did not arrive by
guessing. The loop D4 describes is: run the extract, read the failures,
hand them to a model with the declaration as written, take its revision,
recompile, run again. `author.py` is that loop's second half:

```bash
export ANTHROPIC_API_KEY=...
python examples/records/author.py --tenant records --extract measurement
```

It reads `GET /tenants/{t}/extracts/measurement/failures`, hands each
failure's reason, field, the declaration exactly as written, and the
page's own words (in reading order) to Claude, and prints one fenced
`extract` declaration to paste back into `definitions.fig` and recompile.
With `--sample-pages N --sample-kind medical_record`, it also shows
Claude a sample of already-succeeding pages, so a revision adds rather
than rewrites. It never writes a definition or a fact itself, and the
engine never runs a model at its own run time -- only here, while someone
is authoring.

## Population sample

`generate.py`'s bundle makes one engine trace legible. `generate_population.py`
makes the *population* case legible instead: twelve invented patients, each
their own PDF, uploaded one file per patient -- which is the point, over
one upload carrying several.

```bash
python examples/records/load.py --base http://localhost:8080 --population
```

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
| 9 | Soraya Ibarra | 600009 | 19 | F | routine (11, ~85d) | A | steady gain | 1 plain OCR page that also fails ("Wt" survives, "57.4kg" doesn't parse); unitless-weight *failure* page (extra) |
| 10 | Hugo Castellanos | 600010 | 82 | M | sparse (2) | B | n/a | visit 2's header prints patient 1's MRN -- a misfiled page, not this patient's |
| 11 | Wren Abimbola | 600011 | 50 | F | sparse (1) | C | n/a | single visit |
| 12 | Felix Dzhaparidze | 600012 | 31 | M | sparse (1, year 3 only) | A | n/a | single visit, 2025 only |

Three clinic templates cover every spelling and unit `measurement` declares:
`Wt: <n> kg` / `Ht: <n> cm` (A), `Weight (lb): <n> lb` / `Height: <n> ft <n> in`
(B), `WEIGHT <n> kg` / `HEIGHT <n> in` (C). Patients 2, 4 and 8 switch
template mid-chart, the way a real clinic's charting system changeover
shows up on paper.

Only two of the four roster quirks land on `measurement`'s own failures
route as an `ExtractFailure` -- the unitless weight and the OCR-degraded
page, both a value *found but unreadable*. The spelling miss is never
*found* at all, so it is a quiet absence, same as a weight-only visit's
missing height; the misfiled page reads and copies cleanly, just under
the wrong patient -- not a failure, the risk D4's page-level identity
model accepts in exchange for never trusting a filename. Running this
bundle against a real Tesseract added a third, unplanned failure of its
own: patient 9's plain (non-degraded) OCR page fails too, while patient
5's otherwise-identical plain OCR page fails silently instead. All three
are pinned in `tests/test_records_population.py` against whatever this
environment's Tesseract actually produces.

Running `load.py --population` against a fresh tenant prints this (the
BMI trace is patient 4's: weight read from a `Weight (lb):` page after her
template switch, height carried forward from her very first, `Wt:`/`Ht:`
visit two years earlier):

```
engine dev at http://localhost:8099
library loaded: 3 extracts, 3 figures

pushing the patient roster (12 patients)
uploading patient-600001.pdf (20408 bytes) ...
  -> id 46b89f225cd4703e, 22 page(s), written=1
uploading patient-600002.pdf (10830 bytes) ...
  -> id 3125d0850d4c3580, 19 page(s), written=1
uploading patient-600003.pdf (6432 bytes) ...
  -> id a246cca74cd4bede, 11 page(s), written=1
uploading patient-600004.pdf (7749 bytes) ...
  -> id aeb78d932702dc30, 13 page(s), written=1
uploading patient-600005.pdf (11693 bytes) ...
  -> id 6966c19618b282dc, 11 page(s), written=1
uploading patient-600006.pdf (7638 bytes) ...
  -> id 065ab99afab19841, 13 page(s), written=1
uploading patient-600007.pdf (6776 bytes) ...
  -> id f27c40ab10bed3ad, 11 page(s), written=1
uploading patient-600008.pdf (6703 bytes) ...
  -> id 4e6c60da70b040b4, 11 page(s), written=1
uploading patient-600009.pdf (11731 bytes) ...
  -> id eaaff19fd56e24d9, 12 page(s), written=1
uploading patient-600010.pdf (1988 bytes) ...
  -> id a481907e61941fa6, 2 page(s), written=1
uploading patient-600011.pdf (1438 bytes) ...
  -> id fac6dbcf9890189f, 1 page(s), written=1
uploading patient-600012.pdf (1430 bytes) ...
  -> id ed7cd4b058da6ab9, 1 page(s), written=1

patient.bmi for 600004 (days with a weight on record):
  600004@2023-03-01: 28.7
  ...
  600004@2024-12-25: 29.7
  ...
  600004@2025-10-06: 30.2

evidence for patient.weight?subject=600004@2024-12-25:
  field weight_kg: page aeb78d932702dc30/p0009, printed '217.1553283 lb', 2 box(es)

evidence for patient.height?subject=600004@2024-12-25:
  field height_cm: page aeb78d932702dc30/p0002, printed '182 cm', 2 box(es)

failures for measurement:
  46b89f225cd4703e/p0022 field=measured_at: no alternative matched anywhere on the page
  eaaff19fd56e24d9/p0007#r1 field=weight_kg: no number followed the matched text
  eaaff19fd56e24d9/p0012#r1 field=weight_kg: a number with no printed unit, and more than one unit is declared

population (12 patients):
  mrn      visits pages measured bmi n latest bmi failures
  600001       19    22       19    19       24.2        1
  600002       18    19       18    18       29.0        0
  600003       11    11       11    11       26.3        0
  600004       11    13       11    11       30.2        0
  600005       11    11       10    10       23.0        0
  600006       12    13       11    11       25.8        0
  600007       11    11       11     0     absent        0
  600008       11    11       11    11       22.9        0
  600009       12    12       10    10       23.0        2
  600010        2     2        1     1       26.4        0
  600011        1     1        1     1       24.5        0
  600012        1     1        1     1       22.8        0

loaded. Try:
  http://localhost:8099/ui/  (tenant "records")
```

`measured` is weighed days actually on record; `bmi n` is non-null BMI
days (patient 7's is `0`, `absent`, never `0.0`); `failures` is how many
of `measurement`'s own failures cite a page from that patient's own
file -- patient 1's one is the OCR-degraded page, patient 9's two are the
unitless weight and her own plain OCR page's failure.

## What is synthetic

Every name, medical record number, date and vitals reading in either
`generate.py` or `generate_population.py` is invented for this example
alone. `A_VISIT1_DATE` and every population visit date are chosen to sit
comfortably inside `carried forward`'s own ten-year ceiling
(`docs/language.md`, "On-change data") as measured from when this was
written -- a worked example that outlives that margin needs its dates
moved forward, not a different design.

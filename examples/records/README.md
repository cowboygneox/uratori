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
| `load.py` | The host: teaches the engine, pushes the `patient` roster, uploads the bundle, prints the trace below. |
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

## What is synthetic

Every name, medical record number, date and vitals reading in
`generate.py` is invented for this example alone. `A_VISIT1_DATE` is
chosen to sit comfortably inside `carried forward`'s own ten-year
ceiling (`docs/language.md`, "On-change data") as measured from when this
was written -- a worked example that outlives that margin needs its dates
moved forward, not a different design.

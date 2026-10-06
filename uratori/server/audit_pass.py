"""The `audit` sub-pass: a verdict for every page whose derived rows moved.

Called from `run_pass`, after `facade.run` -- a verdict judged against
stale derived rows would be judging last pass's answer, and `facade.run`
is what makes the rows current. No model call lives here: a reading is an
input this module only reads back (`uratori.server.db`'s `audit_reading`
table); producing one is the worker's job (`uratori/audit/`, 5d). A page
with no reading yet stores the word `unaudited` -- the roster stays
complete, and "not yet read" is a stated fact rather than a missing row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from ..audit.judge import AuditFinding, AuditReading, FieldReading, VerifiedField, judge
from ..documents.extract import ROW_SEPARATOR
from ..engine.change import Change, Outcome
from ..facade import RunReport, Uratori
from ..lang.ast import NumberAfter
from ..lang.plan import AuditPlan, Library
from ..store import FactSource
from ..store.postgres import PostgresEngineStore
from . import db
from .words import WordStore


def page_of(record_key: str) -> str:
    """A derived record's own page -- its bare key for a non-`many`
    extract, or the part before `#rNN` for a `many by row` one."""
    return record_key.split(ROW_SEPARATOR, 1)[0]


def verified_fields_of(library: Library, audit: AuditPlan) -> dict[tuple[str, str], VerifiedField]:
    """What `judge` needs to parse a reading against: every verified
    extract's declared fields, by (extract name, field name)."""
    out: dict[tuple[str, str], VerifiedField] = {}
    for extract_name in audit.verifies:
        extract = library.extracts.get(extract_name)
        fact = library.facts.get(extract_name)
        if extract is None or fact is None:  # pragma: no cover - checker refuses this
            continue
        declared = {f.name: f.type for f in fact.fields}
        for field in extract.fields:
            units = field.matcher.units if isinstance(field.matcher, NumberAfter) else ()
            out[(extract_name, field.name)] = VerifiedField(
                type=declared.get(field.name), units=tuple(units)
            )
    return out


def _pages_from_moves(
    audit: AuditPlan,
    moved: Mapping[str, Sequence[str]],
    vanished: Mapping[str, Sequence[str]],
    touched: Mapping[str, Sequence[str]],
) -> set[str]:
    """Every page this pass's extract pre-pass gives this audit reason to
    (re-)judge: a page whose verified extract moved or vanished a record,
    plus every page the pre-pass actually ran the runner over
    (`touched`, keyed by page kind -- `uratori.server.extract_pass.
    run_extracts`'s third return). The second half is load-bearing (review
    finding A/F1): a page the extractor visited and found nothing on moves
    no derived key, so `moved`/`vanished` alone would silently drop it from
    the roster, the exact cheap-path narrowing rule 4 forbids."""
    pages: set[str] = set()
    for extract_name in audit.verifies:
        for key in (*moved.get(extract_name, ()), *vanished.get(extract_name, ())):
            pages.add(page_of(key))
    pages.update(touched.get(audit.scope, ()))
    return pages


def dump_fields(fields: Sequence[FieldReading]) -> list[dict[str, Any]]:
    return [
        {
            "extract": f.extract,
            "field": f.field,
            "row": f.row,
            "status": f.status,
            "words": list(f.words),
            "box": list(f.box) if f.box is not None else None,
            "seen_text": f.seen_text,
            "anchored": f.anchored,
        }
        for f in fields
    ]


def load_fields(parsed: Sequence[Mapping[str, Any]]) -> tuple[FieldReading, ...]:
    return tuple(
        FieldReading(
            extract=p["extract"],
            field=p["field"],
            row=p["row"],
            status=p["status"],
            words=tuple(p.get("words", ())),
            box=tuple(p["box"]) if p.get("box") is not None else None,
            seen_text=p.get("seen_text"),
            anchored=p.get("anchored", True),
        )
        for p in parsed
    )


def dump_finding(f: AuditFinding) -> dict[str, Any]:
    return {
        "extract": f.extract,
        "field": f.field,
        "record": f.record,
        "row": f.row,
        "verdict": f.verdict,
        "seen": f.seen,
        "extracted": f.extracted,
        "words": list(f.words),
        "boxes": [b.as_json() for b in f.boxes],
        "anchored": f.anchored,
        "seen_text": f.seen_text,
        "note": f.note,
    }


async def _current_rows_by_page(
    facts: FactSource, tenant: str, audit: AuditPlan
) -> dict[str, dict[str, list[tuple[str, Mapping[str, Any]]]]]:
    """Every verified extract's current records, grouped by page and then
    by extract -- fetched once per audit per pass (one `of_kind` per
    verified extract), never once per page: the same "load a kind once"
    discipline the engine's own `_readers` holds."""
    out: dict[str, dict[str, list[tuple[str, Mapping[str, Any]]]]] = {}
    for extract_name in audit.verifies:
        for row in await facts.of_kind(tenant, extract_name):
            page = page_of(row.key)
            out.setdefault(page, {}).setdefault(extract_name, []).append((row.key, row.value))
    return out


async def run_audits(
    pool: asyncpg.Pool[Any],
    word_store: WordStore,
    facts: FactSource,
    library: Library,
    tenant: str,
    facade: Uratori,
    *,
    moved: Mapping[str, Sequence[str]],
    vanished: Mapping[str, Sequence[str]],
    touched: Mapping[str, Sequence[str]],
    full: bool,
) -> RunReport | None:
    """Judge (or mark `unaudited`) every page an audit needs to answer for,
    `accept` each one, and merge the results into one `RunReport` -- so a
    caller that pushes this pass's report pushes the audit movement in the
    same delivery, rather than running a second, unannounced pass.

    `None` when there is nothing to do: no `audit` declared, or no page any
    of them reaches this time. `full` widens every audit's scope to its
    whole page roster (a full pass recomputes everything, the same
    escalation `facade.execute` already makes for a figure); the warm path
    narrows to the pages `moved`/`vanished`/`touched` actually reached this
    pass (see `_pages_from_moves`).
    """
    if not library.audits:
        return None

    changes: list[Change] = []
    results: list[Any] = []
    reached: set[str] = set()
    engine_store = PostgresEngineStore(pool)

    for audit in library.audits.values():
        if full:
            pages = {row.key for row in await facts.of_kind(tenant, audit.scope)}
        else:
            # A redefined (or brand new) audit's version has never judged
            # a page yet -- no row anywhere under (name, version). Rule
            # 3/4: the first warm pass this version sees must roster the
            # audit's whole scope itself, the same escalation a cold
            # extract pointer gets (`extract_pass._stale_pages`), rather
            # than leaving the new version's rows absent until an operator
            # happens to run an explicit `full` pass (review finding A/F1).
            existing = await engine_store.values(tenant, audit.name, audit.version)
            if not existing:
                pages = {row.key for row in await facts.of_kind(tenant, audit.scope)}
            else:
                pages = _pages_from_moves(audit, moved, vanished, touched)
        if not pages:
            continue
        alive = {row.key for row in await facts.of_kind(tenant, audit.scope)}
        pages &= alive
        if not pages:
            continue

        verified_fields = verified_fields_of(library, audit)
        rows_by_page = await _current_rows_by_page(facts, tenant, audit)

        for page_key in sorted(pages):
            stored = await db.audit_reading(pool, tenant, audit.name, audit.version, page_key)
            label = page_key
            if stored is None:
                value: str = "unaudited"
                members: tuple[str, ...] = ()
            else:
                words = await word_store.words_of(tenant, audit.scope, page_key)
                words_by_id = {w.id: w for w in words}
                reading = AuditReading(
                    page_key=page_key,
                    words_sha=stored["words_sha"],
                    prompt=stored["prompt"],
                    model=stored["model"],
                    response=stored["response"],
                    fields=load_fields(stored["parsed"]),
                )
                current_rows = rows_by_page.get(page_key, {})
                value, members, findings = judge(reading, current_rows, verified_fields, words_by_id)
                await db.replace_audit_findings(
                    pool, tenant, audit.name, page_key, [dump_finding(f) for f in findings]
                )
            report = await facade.accept(tenant, audit.name, page_key, value, members, label)
            changes.extend(report.outcome.changes)
            results.extend(report.results)
            reached |= report.moved

    if not changes and not results:
        return None
    outcome = Outcome(
        changes=tuple(changes),
        covered=frozenset(a.scope for a in library.audits.values()),
        reindexed=(),
        rebuilt=(),
    )
    return RunReport(outcome=outcome, results=tuple(results), moved=frozenset(reached))


__all__ = [
    "dump_fields",
    "dump_finding",
    "load_fields",
    "page_of",
    "run_audits",
    "verified_fields_of",
]

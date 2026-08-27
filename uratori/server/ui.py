"""The built-in investigation UI, and the read-only endpoints that feed it.

A developer standing behind the firewall gets one page answering two
questions: *what does this deployment know* (every declaration of every kind,
its source as written, its dependencies traced down to the fact kinds and the
records themselves) and *what happened when the facts moved* (the persisted
run log, each pass with the movements it caused, frozen at the time).

Decisions a reader should not have to rediscover:

- **Unauthenticated by design, so OFF by default beside a token.** The UI's
  door is the network -- a firewall, a private ingress, a reverse proxy. That
  is a sound posture only when it is chosen: the moment `URATORI_TOKEN`
  protects the API, silently mounting an open UI would hand every fact and
  figure to anyone who can reach the port. So the default follows the token
  (open server: UI on; token'd server: UI off) and `URATORI_UI` overrides it
  explicitly in either direction.
- **Embedding is a per-deployment grant.** `frame-ancestors` on the HTML is
  the whole mechanism: default `'self'`, set `URATORI_UI_FRAME_ANCESTORS` to
  the embedding application's origin to let it iframe this. There is no CORS
  configuration because none is needed -- the page and its JSON share an
  origin, and a host that proxies `/ui/` under its own origin needs even the
  frame grant only if it also splits hosts.
- **`/ui/api/*` is the UI's contract, not the server's.** Hosts integrate
  against the documented API; these shapes serve the bundled page and may
  move with it. That is also why they live here and not in `contract.py`.
- **No build step.** The page is hand-written HTML, CSS and one ES module,
  shipped inside the wheel. A Node toolchain in this repository would cost
  every contributor more than this UI is worth, and the browser's job here is
  layout only -- every number, display string and verdict arrives rendered,
  per the engine's clients-compute-nothing rule.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from ..engine.buckets import SEPARATOR, measure_of, subject_of
from ..engine.project import format_value
from ..engine.serve import _citing_spaces, _label_of, serve_figure
from ..engine.serve import availability as figure_availability
from ..facade import DEFAULT_TRAILING
from ..lang.ast import ByAge, ByComposite, ByField, FigureUnit, IndexBy, IndexField
from ..lang.lex import DefinitionError, lex
from ..lang.plan import CompiledFactField, CompiledIndex, CompiledMeasure, Library
from ..lang.source import declaration_prose, declaration_source
from ..results import Availability, Evidence, Ok, Result, Subject, Unavailable
from ..schema import EFFORT_HOURS_SETTING, Schema
from ..store.postgres import PostgresEngineStore
from . import db
from .runtime import (
    State,
    World,
    compile_for_teach,
    facade_for,
    ready,
    record_pass,
    run_out,
    state_of,
    taught_schema,
)

STATIC = Path(__file__).parent / "static"

_ASSETS = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
"""Servable files, by allowlist. A directory-walking static mount would serve
whatever lands beside these; two known names cannot."""


DeclarationKind = Literal[
    "fact", "group", "filter", "measure", "figure", "reading", "projection", "summary"
]

DependencyType = Literal[
    "fact",
    "setting",
    "group",
    "filter",
    "measure",
    "figure",
    "reading",
    "projection",
    "summary",
]


class Dependency(BaseModel):
    """One edge of the trace. `fact` and `setting` are the leaves: a fact kind
    is where the records live, a setting is a dial the tenant turns."""

    type: DependencyType
    name: str


class DeclarationOut(BaseModel):
    name: str
    kind: DeclarationKind
    version: str | None
    """None for a group, filter or measure: they have no version of their own
    -- their text is hashed into every definition that reads them."""

    doc: str
    source: str | None
    unit: str | None = None
    mode: str | None = None
    fact_kind: str | None = None
    rests_on: list[Dependency]
    moved_by: list[Dependency] = []
    """The impact answer, precomputed: the transitive closure of `rests_on`
    reduced to its leaves -- fact kinds and settings. The question a reader
    brings to a definition is "if data changes, does this number move?", and
    the direct edges answer it only after a walk. This is that walk done once,
    server-side, so the page may say "these records and these dials can move
    this figure -- nothing else can" and be entitled to the second half."""


class WorldOut(BaseModel):
    version: str
    kinds: list[str]
    name_fields: dict[str, str]
    refusal: str | None
    """Set when definitions are stored but this build's compiler refused them;
    the declarations list is then empty and this says why, so the page can
    state the truth instead of rendering an inexplicably bare library."""

    editable: bool
    """Whether this deployment grants editing from the UI -- the page decides
    whether to offer the editor from the payload it already loads, rather than
    probing a write route to see what happens."""

    declarations: list[DeclarationOut]


class DeclaredName(BaseModel):
    """A declaration's name and kind, and nothing else -- what a completion
    list needs, without the full catalogue's traced edges."""

    name: str
    kind: DeclarationKind


class SourceOut(BaseModel):
    """The stored definitions source, served for editing.

    `source` is the text as stored, verbatim -- an editor loading a
    reconstruction would save its own artefacts back as if a person wrote
    them. Beside it rides what a completion engine cannot mine from the text
    alone: the fact kinds with their declared fields (empty lists in a
    schema-taught world, which knows its kinds but not their fields), every
    dial a definition may name plus the reserved rendering dial, and the
    declared names.
    """

    source: str
    fingerprint: str
    """Names the exact text served, so a save can say which text it edited --
    see `SaveIn.expected`."""

    editable: bool
    refusal: str | None
    """Why the library is bare when it is: the stored source no longer
    compiles under this build. The editor is the repair tool the boot path
    promised, so it must see the refused text and the reason together."""

    kinds: dict[str, list[str]]
    dials: list[str]
    declarations: list[DeclaredName]


class CheckIn(BaseModel):
    """A candidate source for the dry-run compile -- checked, never stored."""

    source: str


class RefusalOut(BaseModel):
    """A compile refusal, structured for an editor: the message to print and
    the position to point at. The lexer and parser know their column; the
    checker only its line, and `column` is then null rather than a fabricated
    zero a client would dutifully point a caret at."""

    message: str
    line: int | None
    column: int | None


class DeclChange(BaseModel):
    """One declaration's fate under a candidate source.

    `changed` means the saved text would serve differently: its version hash
    moved (which happens when a group, filter or measure it reads moved,
    even if its own lines are untouched -- the diff must tell the cascade's
    truth) or its own tokens moved. Display and prose edits move neither and
    are reported `unchanged`; the save still stores them. A label IS part of
    what serves (`changed`), yet owes no pass -- that split is `SaveOut.stale`'s
    to state, not this one's.
    """

    name: str
    kind: DeclarationKind
    change: Literal["new", "changed", "unchanged", "removed"]


class CheckOut(BaseModel):
    ok: bool
    refusal: RefusalOut | None
    adoption: str | None = None
    """Stated when the candidate source declares facts over a schema that
    declared kinds: the save retires the schema's kinds, name fields and url
    fields, which no per-declaration diff row can carry. A change the page
    does not state is a change the reader cannot review."""

    declarations: list[DeclChange]


class SaveIn(BaseModel):
    source: str
    expected: str
    """The fingerprint of the text this editor loaded. A save names what it
    edited, so a save against text that has moved is refused with the state
    of play rather than silently overwriting the other author's work."""


class SaveOut(BaseModel):
    """What the save did: the new text's fingerprint (the `expected` of the
    next save), the adoption sentence when the schema's kinds retired, and
    the same per-declaration diff the check serves."""

    ok: bool
    fingerprint: str
    adoption: str | None = None
    stale: bool
    """Whether this save leaves tenants owing a pass. True exactly when
    stored state is now behind the text: a figure moved (its values and
    pointers are stored) or some grouping's spec moved (its memberships
    are stored). A changed label or reading changes nothing stored -- it
    serves its new text immediately -- and a page that offered "run a pass"
    for it would send every tenant through a rebuild that moves nothing."""

    declarations: list[DeclChange]


class EditRunIn(BaseModel):
    """A pass with no new facts; `full` rebuilds everything from storage."""

    full: bool = False


class EditRunOut(BaseModel):
    """The pass, reduced to what the saved panel's one line needs; the full
    record lands in the activity log like every other pass."""

    ok: bool
    changed: int
    rebuilt: list[str]


class TenantOut(BaseModel):
    tenant: str
    facts: int


class TenantsOut(BaseModel):
    tenants: list[TenantOut]


class KindCount(BaseModel):
    kind: str
    records: int


class FactKindsOut(BaseModel):
    kinds: list[KindCount]


class FactRecordOut(BaseModel):
    key: str
    name: str | None
    """The record's own name, by the schema's name_field for this kind --
    extracted here because the browser must not guess which field names a
    record."""

    value: dict[str, Any]
    source_stamp: str | None


class FactPageOut(BaseModel):
    kind: str
    records: list[FactRecordOut]
    more: bool
    total: int


class RunOutLog(BaseModel):
    id: int
    at: str
    trigger: Literal["facts", "facts-deferred", "run"]
    """Which door the pass came through -- the same three words `engine_run.
    cause` documents. All three, because this model validates history: a
    trigger it does not admit 500s the whole log, and the one that was
    missing ("facts-deferred") is exactly the door bulk imports come
    through."""
    full: bool
    written: int
    deleted: int
    changed: int
    not_shown: int
    """`changed` minus the rows in `shown` -- computed here so the page can
    state what the capped sample is missing without doing arithmetic itself."""

    rebuilt: list[str]
    covered: list[str]
    shown: list[dict[str, Any]]
    """The frozen `ShownChange` rows, served exactly as stored. A dict rather
    than the model on purpose: history written under an older shape must keep
    serving after the model grows a field, and revalidating frozen rows
    against the current model would 500 the whole log the day it changes."""


class ActivityOut(BaseModel):
    runs: list[RunOutLog]
    total: int
    """How many runs match the listing (the whole kept log, not this page) --
    the honest number beside a limit-capped list."""

    quiet_hidden: int
    """How many do-nothing runs the default listing is not showing."""


class MembershipBucket(BaseModel):
    bucket: str
    members: int


class MembershipOut(BaseModel):
    """Where a group or filter actually filed this tenant's records -- the
    stored membership the engine computed with, not a re-evaluation.

    `state` carries the same honesty as a figure's: a tenant never bucketed
    answers never-computed rather than a fabricated zero, and definitions that
    moved since the last pass answer behind-deploy, because the stored rows
    describe the old text. When it is not Ok, every count is served empty and
    the page renders the sentence instead.
    """

    name: str
    kind: Literal["group", "filter"]
    fact_kind: str
    """Whose records the spec reads."""

    id_space: str
    """Whose ids the members are -- differs from `fact_kind` under `keyed as`,
    and it is the kind member keys resolve in."""

    state: Availability
    members: int
    """Distinct members across every bucket; a record can be in several."""

    population: int
    """Stored records of `id_space` -- the M in "N of M match", without which
    a filter matching everything and one matching a single record read the
    same."""

    buckets: list[MembershipBucket]
    buckets_total: int
    """How many buckets exist, beside a limit-capped list."""

    buckets_more: bool = False
    """Whether buckets follow the last one listed -- the pager's fact."""

    note: str | None = None
    """A server-rendered caveat the counts cannot carry themselves. Today:
    this index's spec reads dials (an age threshold, a zone), and a dial that
    moved since the last pass shows here only after the next one -- spec
    hashes deliberately exclude settings, so `state` cannot see it (the
    engine's own stamp carries a dial fingerprint and rebuilds at that next
    pass; this page compares specs alone). Figures over the same index
    refuse with `setting-moved` in that window; membership states its weaker
    guarantee instead of silently wearing an Ok it has not earned."""


class MemberRecordOut(BaseModel):
    key: str
    name: str | None
    held: bool
    """False when the membership row's record is not stored -- the engine's
    claim is listed either way, because hiding it would un-say it."""


class MemberPageOut(BaseModel):
    name: str
    bucket: str
    records: list[MemberRecordOut]
    more: bool
    total: int


class MeasuredRecordOut(BaseModel):
    key: str
    name: str | None
    display: str | None
    """This record's measurement, rendered by the server. None means the
    measure has nothing for this record (an unmerged change has no
    open-duration) -- an absence, never a rendered nought."""


class MeasuredPageOut(BaseModel):
    name: str
    fact_kind: str
    unit: str
    records: list[MeasuredRecordOut]
    more: bool
    total: int


class FiledOut(BaseModel):
    """One grouping's verdict on one record: where it filed it, or that it
    did not take it -- "not a member" is a finding a verification surface
    states rather than leaves to inference."""

    index: str
    kind: Literal["group", "filter"]
    member: bool
    buckets: list[str]


class RecordMeasureOut(BaseModel):
    measure: str
    display: str | None


class RecordOut(BaseModel):
    """The leaf of every trace: one record, everything stored about it, and
    what the library makes of it -- its classification under every grouping
    keyed by its kind's ids, and its value under every measure of its kind."""

    kind: str
    key: str
    name: str | None
    url: str | None
    value: dict[str, Any]
    source_stamp: str | None
    filed: list[FiledOut]
    filed_state: Availability
    """The same honesty as membership: when the tenant was never bucketed or
    the definitions moved since, `filed` is empty and this says why."""

    measured: list[RecordMeasureOut]


ABOUT_ROWS = 60
"""Per-entry row cap on the about payload. A courier with years of day rows
must not make the record page a dump -- the entry says it truncated (`more`)
and the figure's own page holds the rest. Capped per entry rather than over
the payload, so one prolific figure cannot push another off the page."""


class AboutFigureOut(BaseModel):
    """One figure scoped to the record's kind, narrowed to this record.

    The engine's own `Result` -- state, unit, rendered rows -- because the
    record page must show exactly what the figure's page would, and a second
    rendering path is a second calculation system waiting to disagree."""

    result: Result
    more: bool


class CitedRowOut(BaseModel):
    """One stored value that counted this record: whose row it is (`subject`,
    for the link back to that record's page), the label frozen when it was
    written, the day or dimension cell when the figure has one, and the value
    as rendered now."""

    id: str
    subject: str
    name: str
    dimension: str | None
    display: str | None


class CitedFigureOut(BaseModel):
    """One leaf figure whose members are keys of this record's kind. An entry
    with no rows is a stated verdict -- "this figure did not count it" -- not
    an omission, which is why every matching figure appears."""

    figure: str
    label: str
    scope: str
    """The fact kind the figure's subjects are ids of -- where a row's
    `subject` links to."""

    state: Availability
    rows: list[CitedRowOut]
    more: bool


class AboutPageOut(BaseModel):
    """This record's row on one projection of its kind, exactly as the page
    serves it. `present: false` under an Ok state is a verdict the definition
    reached (from-set, omit gate, sort and limit), and `note` says so."""

    projection: str
    label: str
    state: Availability
    present: bool
    row: Subject | None
    note: str | None


class AboutOut(BaseModel):
    """What the library makes OF this record, upward: the values computed for
    it (figures scoped to its kind), the values that counted it (the reverse
    citation), and its rows on the pages of its kind. The record route serves
    the downward half -- the stored document, the filings, the measurements."""

    kind: str
    key: str
    state: Availability
    """Whether there was a library to derive anything with. Not Ok means the
    sections below are empty because nothing is loaded -- a truth the page
    must print instead of "no figure is scoped to this kind", which is a
    verdict about definitions that do not exist."""

    figures: list[AboutFigureOut]
    cited: list[CitedFigureOut]
    pages: list[AboutPageOut]


def router(frame_ancestors: str, *, edit: bool = False) -> APIRouter:
    ui = APIRouter()

    def _state(request: Request) -> State:
        return state_of(request)

    def _granted() -> None:
        """The editor's whole gate. Request-time rather than mount-time, so a
        read-only deployment still serves the source and answers a write
        attempt with the fix instead of a 404 that reads as a broken page.
        The open GET is a real decision, not an oversight: with a compiling
        world it serves nothing the declaration pages don't already, and
        with a boot-refused world -- where those pages go bare -- it is the
        repair path's only window onto the stored text."""
        if not edit:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This deployment does not grant editing from the UI. "
                    "Set URATORI_UI_EDIT=on (an explicit operator choice) to enable it; "
                    "the default grants editing only where the API itself is open."
                ),
            )

    # ------------------------------------------------------------- page --

    @ui.get("/ui", include_in_schema=False)
    async def bare() -> RedirectResponse:
        # Relative, so the page survives being proxied under a sub-path.
        return RedirectResponse(url="ui/", status_code=308)

    @ui.get("/ui/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            STATIC / "index.html",
            media_type="text/html; charset=utf-8",
            headers={
                # frame-ancestors governs the document being framed, so the
                # grant rides the HTML. The default 'self' means "nobody may
                # embed this but itself" -- loosened only by explicit config.
                "Content-Security-Policy": f"frame-ancestors {frame_ancestors}",
                "Cache-Control": "no-cache",
            },
        )

    @ui.get("/ui/{asset}", include_in_schema=False)
    async def asset(asset: str) -> FileResponse:
        media_type = _ASSETS.get(asset)
        if media_type is None:
            raise HTTPException(status_code=404, detail=f"No such asset: {asset}")
        return FileResponse(
            STATIC / asset, media_type=media_type, headers={"Cache-Control": "no-cache"}
        )

    # -------------------------------------------------------------- world --

    @ui.get("/ui/api/world", response_model=WorldOut, include_in_schema=False)
    async def world(request: Request) -> WorldOut:
        s = _state(request)
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        held = s.world
        library = held.library
        world_schema = taught_schema(held)
        return WorldOut(
            version=s.version,
            kinds=sorted(world_schema.kinds),
            name_fields=dict(world_schema.name_fields),
            refusal=held.refusal,
            editable=edit,
            declarations=_declarations(library, world_schema) if library else [],
        )

    # ------------------------------------------------------------- editor --

    @ui.get("/ui/api/source", response_model=SourceOut, include_in_schema=False)
    async def source(request: Request) -> SourceOut:
        s = _state(request)
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        held = s.world
        text = held.source or ""
        return SourceOut(
            source=text,
            fingerprint=_fingerprint(text),
            editable=edit,
            refusal=held.refusal,
            kinds=_kind_fields(held),
            dials=sorted({*taught_schema(held).declarable, EFFORT_HOURS_SETTING}),
            declarations=[
                DeclaredName(name=name, kind=kind)
                for name, (kind, _) in sorted(_named(held.library).items())
            ],
        )

    @ui.post("/ui/api/check", response_model=CheckOut, include_in_schema=False)
    async def check(body: CheckIn, request: Request) -> CheckOut:
        """A dry run of the save: the same compile, nothing stored.

        Gated like the writes even though it writes nothing: it is the
        editor's inner loop, and a deployment that refuses the save has no
        business inviting the drafting either.
        """
        s = _state(request)
        _granted()
        _within_reason(body.source)
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        try:
            library, _schema, _document, adopted = compile_for_teach(body.source, s.world)
        except DefinitionError as refusal:
            return CheckOut(ok=False, refusal=_refusal_out(refusal), declarations=[])
        return CheckOut(
            ok=True,
            refusal=None,
            adoption=_ADOPTION if adopted else None,
            declarations=_changes(s.world.library, library),
        )

    @ui.put("/ui/api/source", response_model=SaveOut, include_in_schema=False)
    async def save(body: SaveIn, request: Request) -> SaveOut:
        """The same teach `PUT /definitions` performs, with two editor-shaped
        differences: the refusal arrives structured rather than as prose, and
        the save names the text it edited so two editors cannot silently
        overwrite each other."""
        s = _state(request)
        _granted()
        _within_reason(body.source)
        # The whole read-check-compile-write rides the teach lock: the save
        # awaits the database between reading the fingerprint and swapping
        # the world, and two saves interleaving across that await would BOTH
        # pass the edited-since-loaded check -- the silent overwrite the
        # fingerprint exists to refuse.
        async with s.teach:
            if s.world is None:
                raise HTTPException(status_code=409, detail="No schema has been declared yet")
            held = s.world
            if body.expected != _fingerprint(held.source or ""):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The stored source has changed since this editor loaded it. "
                        "Reload, reapply the edit, and save again -- saving now would "
                        "silently overwrite what the other author taught."
                    ),
                )
            try:
                library, schema, document, adopted = compile_for_teach(body.source, held)
            except DefinitionError as refusal:
                raise HTTPException(
                    status_code=422, detail=_refusal_out(refusal).model_dump()
                ) from refusal
            # The diff against what was taught before, not after: the response
            # is the review record of what this save did.
            changes = _changes(held.library, library)
            stale = _owes_a_pass(held.library, library, changes)
            await db.save_world(s.pool, document, body.source)
            s.world = World(
                schema=schema,
                schema_document=document,
                source=body.source,
                library=library,
            )
        return SaveOut(
            ok=True,
            fingerprint=_fingerprint(body.source),
            adoption=_ADOPTION if adopted else None,
            stale=stale,
            declarations=changes,
        )

    @ui.post(
        "/ui/api/tenants/{tenant}/runs",
        response_model=EditRunOut,
        include_in_schema=False,
    )
    async def run_pass(tenant: str, body: EditRunIn, request: Request) -> EditRunOut:
        """A pass with no new facts, from the editor: a save leaves every
        tenant honestly behind-deploy until one runs, and without this door
        the editing loop dead-ends at curl. Same lock, same log, same helpers
        as the API's `POST /tenants/{tenant}/runs` -- it IS that pass."""
        s = _state(request)
        _granted()
        world, library = ready(s)
        if not await db.tenant_exists(s.pool, tenant):
            # Refused rather than run: a pass for a tenant nobody has pushed
            # facts for computes nothing, writes a run-log row that makes the
            # name look real, and leaks a per-name lock for the process's
            # lifetime. Tenants are created by pushing facts through the API.
            # An existence check, not the counted tenant list -- the guard
            # was costing a full fact scan per editor pass.
            raise HTTPException(
                status_code=404, detail=f"No tenant called {tenant} holds any facts here"
            )
        async with s.lock_for(tenant):
            settings = await db.load_settings(s.pool, tenant)
            # The same debt rule as the API's run door: a bulk import that
            # deferred its pass left this tenant owing a FULL one, and an
            # editor pass that ran incremental over that gap would settle
            # the debt's flag without paying it.
            full = body.full or await db.deferred(s.pool, tenant)
            report = await facade_for(s, world, library).run(tenant, settings, full=full)
            if full:
                await db.clear_deferred(s.pool, tenant)
            out = run_out(report, world, library, settings, written=0, deleted=0)
            await record_pass(s, tenant, "run", full=full, out=out)
        return EditRunOut(ok=True, changed=out.changed, rebuilt=out.rebuilt)

    # -------------------------------------------------------------- facts --

    @ui.get("/ui/api/tenants", response_model=TenantsOut, include_in_schema=False)
    async def tenants(request: Request) -> TenantsOut:
        s = _state(request)
        return TenantsOut(
            tenants=[TenantOut(**row) for row in await db.list_tenants(s.pool)]
        )

    @ui.get("/ui/api/tenants/{tenant}/facts", response_model=FactKindsOut, include_in_schema=False)
    async def fact_kinds(tenant: str, request: Request) -> FactKindsOut:
        """Every kind the schema declares, with its stored count -- a kind
        nobody has pushed appears at zero, because "nothing collected" is a
        finding and a silently missing row is how it goes unfound."""
        s = _state(request)
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        counts = await db.fact_kind_counts(s.pool, tenant)
        names = sorted(set(taught_schema(s.world).kinds) | set(counts))
        return FactKindsOut(
            kinds=[KindCount(kind=name, records=counts.get(name, 0)) for name in names]
        )

    @ui.get("/ui/api/tenants/{tenant}/facts/{kind}", response_model=FactPageOut, include_in_schema=False)
    async def facts(
        tenant: str,
        kind: str,
        request: Request,
        after: str | None = None,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> FactPageOut:
        s = _state(request)
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        name_field = taught_schema(s.world).name_fields.get(kind)
        records, more, total = await db.page_facts(
            s.pool, tenant, kind, after=after, q=q, limit=limit
        )
        return FactPageOut(
            kind=kind,
            records=[
                FactRecordOut(
                    key=row["key"],
                    name=_field_at(row["value"], name_field),
                    value=row["value"],
                    source_stamp=row["source_stamp"],
                )
                for row in records
            ],
            more=more,
            total=total,
        )

    @ui.get(
        "/ui/api/tenants/{tenant}/facts/{kind}/{key:path}",
        response_model=RecordOut,
        include_in_schema=False,
    )
    async def record(tenant: str, kind: str, key: str, request: Request) -> RecordOut:
        """One record, with the library's verdicts on it. `:path` because a
        provider key is the provider's business and may contain anything,
        slashes included."""
        s = _state(request)
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        row = await db.fact_record(s.pool, tenant, kind, key)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Nothing is stored for {key} under {kind}"
            )
        value = row["value"]
        # Through taught_schema, like every surface that describes the world:
        # a fact-taught library carries the name and url fields, and reading
        # the stored document here would leave every record on this page
        # nameless while the Facts tab two clicks away names it fine.
        schema = taught_schema(s.world)
        library = s.world.library

        filed: list[FiledOut] = []
        measured: list[RecordMeasureOut] = []
        if library is None:
            # Records are browsable before definitions exist; classification
            # is not, and the state says which absence this is.
            filed_state: Availability = Unavailable(
                because="never-computed",
                detail="no definitions are loaded, so there is no classification to report",
            )
        else:
            # Only groupings whose members are ids of THIS kind: `keyed as`
            # files one kind's records under another kind's ids, and asking
            # "which bucket holds this commit" of a person-keyed index would
            # always answer "none" -- a stated absence the index never claimed.
            over = sorted(
                name
                for name, index in library.indexes.items()
                if index.id_space == kind
            )
            # The verdict table quotes every grouping in `over`, so its
            # honesty rests on all of them -- one stale grouping and the
            # whole table would mix eras.
            filed_state = await _membership_state(s.pool, library, tenant, over)
            if isinstance(filed_state, Ok) and over:
                held = await db.memberships_of(s.pool, tenant, key, over)
                filed = [
                    FiledOut(
                        index=name,
                        kind=_grouping_kind(library.indexes[name]),
                        member=name in held,
                        buckets=held.get(name, []),
                    )
                    for name in over
                ]
            settings = schema.settings_for(await db.load_settings(s.pool, tenant))
            at_ms = time.time() * 1000.0
            try:
                for name in sorted(library.measures):
                    measure = library.measures[name]
                    if measure.kind != kind:
                        continue
                    measured.append(
                        RecordMeasureOut(
                            measure=name,
                            display=_measured_display(measure, value, settings, at_ms),
                        )
                    )
            except KeyError as gap:
                # Same fixable configuration as the measured route (an effort
                # measure with no hoursPerDay dial): the sentence, not a 500.
                raise HTTPException(
                    status_code=409, detail=gap.args[0] if gap.args else str(gap)
                ) from gap

        return RecordOut(
            kind=kind,
            key=key,
            name=_field_at(value, schema.name_fields.get(kind)),
            url=_field_at(value, schema.url_fields.get(kind)),
            value=value,
            source_stamp=row["source_stamp"],
            filed=filed,
            filed_state=filed_state,
            measured=measured,
        )

    @ui.get(
        "/ui/api/tenants/{tenant}/about/{kind}/{key:path}",
        response_model=AboutOut,
        include_in_schema=False,
    )
    async def about(tenant: str, kind: str, key: str, request: Request) -> AboutOut:
        """Everything the library derives from this record, walking up: the
        record route answers "what is stored and where was it filed", this
        one answers "what numbers did it become". Split from the record route
        because this half prices differently -- it serves figures and
        evaluates projections -- and the page fetches the two in parallel."""
        s = _state(request)
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        row = await db.fact_record(s.pool, tenant, kind, key)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Nothing is stored for {key} under {kind}"
            )
        library = s.world.library
        if library is None:
            # Records are browsable before definitions exist; the sections
            # are empty because nothing is loaded, and the state says so --
            # the client must not turn this into "no figure is scoped here".
            return AboutOut(
                kind=kind,
                key=key,
                state=Unavailable(
                    because="never-computed",
                    detail="no definitions are loaded, so nothing is derived from any record",
                ),
                figures=[],
                cited=[],
                pages=[],
            )

        store = PostgresEngineStore(s.pool)
        raw = await db.load_settings(s.pool, tenant)
        document = taught_schema(s.world).settings_for(raw)

        # One guard over all three sections, the same sentence the record
        # route serves: a fixable dial gap (an effort figure with no
        # hoursPerDay dial) must be the 409 that names the dial, never a
        # 500 that takes the whole upward half of the page with it.
        try:
            figures: list[AboutFigureOut] = []
            for plan in library.figures:
                if plan.scope != kind:
                    continue
                result = await serve_figure(
                    store, library, tenant, plan, document, subject=key
                )
                more = len(result.subjects) > ABOUT_ROWS
                if more:
                    # The LATEST rows survive the cap, still in the page's own
                    # ascending order: this page verifies what a number says
                    # now, and keeping the head would show the first sixty
                    # days of the oldest season and nothing current.
                    result = result.model_copy(
                        update={"subjects": result.subjects[-ABOUT_ROWS:]}
                    )
                figures.append(AboutFigureOut(result=result, more=more))

            # Every figure whose stored members MAY be keys of this kind --
            # the spaces, not the single-kind reduction the evidence panel
            # uses, because an arithmetic over two id spaces stores the union
            # as its citation and dropping it here would omit a figure that
            # counted the record. Rollups answer an empty space set: their
            # members are stored cells, and their parts' own citations
            # already name the records underneath. One store call for all of
            # them: the citation index is probed once with the record key.
            cited_plans = [
                plan
                for plan in library.figures
                if kind in _citing_spaces(plan, library)
            ]
            citing = (
                await store.values_citing(
                    tenant,
                    key,
                    {plan.name: plan.version for plan in cited_plans},
                    limit=ABOUT_ROWS + 1,
                )
                if cited_plans
                else {}
            )
            cited: list[CitedFigureOut] = []
            for plan in cited_plans:
                state = await figure_availability(store, library, tenant, plan, document)
                rows: list[CitedRowOut] = []
                more = False
                if isinstance(state, Ok):
                    found = citing.get(plan.name, [])
                    more = len(found) > ABOUT_ROWS
                    for stored in found[:ABOUT_ROWS]:
                        tail = (
                            stored.subject.split(SEPARATOR, 1)[1]
                            if SEPARATOR in stored.subject
                            else None
                        )
                        rows.append(
                            CitedRowOut(
                                id=stored.subject,
                                subject=subject_of(stored.subject),
                                name=stored.label,
                                dimension=tail,
                                display=format_value(stored.value, plan.unit, document),
                            )
                        )
                cited.append(
                    CitedFigureOut(
                        figure=plan.name,
                        label=_label_of(plan.name),
                        scope=plan.scope,
                        state=state,
                        rows=rows,
                        more=more,
                    )
                )

            pages: list[AboutPageOut] = []
            facade = facade_for(s, s.world, library)
            for project in library.projections:
                if project.kind != kind:
                    continue
                # The projection evaluated whole, then this record's row picked
                # out -- never the formulas re-run over one record, because the
                # from-set, the omit gate, the sort and the limit are part of
                # what the page says, and a bespoke single-row path would show a
                # row the real page refused.
                answer = await facade.answer(tenant, project.name, raw)
                if answer is None:  # pragma: no cover - the plan came from the library
                    continue
                match = next((sub for sub in answer.subjects if sub.id == key), None)
                note = None
                if isinstance(answer.state, Ok) and match is None:
                    note = (
                        "Not on this page. Its from-set, omit gate, sort and limit "
                        "decide the rows, and they did not take this record."
                    )
                pages.append(
                    AboutPageOut(
                        projection=project.name,
                        label=_label_of(project.name),
                        state=answer.state,
                        present=match is not None,
                        row=match,
                        note=note,
                    )
                )


        except KeyError as gap:
            raise HTTPException(
                status_code=409, detail=gap.args[0] if gap.args else str(gap)
            ) from gap

        return AboutOut(
            kind=kind, key=key, state=Ok(), figures=figures, cited=cited, pages=pages
        )

    # ---------------------------------------------------------- membership --

    @ui.get(
        "/ui/api/tenants/{tenant}/membership/{name}",
        response_model=MembershipOut,
        include_in_schema=False,
    )
    async def membership(
        tenant: str,
        name: str,
        request: Request,
        buckets_after: str | None = None,
        buckets_limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    ) -> MembershipOut:
        s = _state(request)
        _world, library = ready(s)
        index = library.indexes.get(name)
        if index is None:
            raise HTTPException(status_code=404, detail=_not_a_grouping(library, name))
        state = await _membership_state(s.pool, library, tenant, [name])
        if not isinstance(state, Ok):
            # Every count empty under a not-Ok state: a number beside the
            # sentence would be read instead of it.
            return MembershipOut(
                name=name,
                kind=_grouping_kind(index),
                fact_kind=index.kind,
                id_space=index.id_space,
                state=state,
                members=0,
                population=0,
                buckets=[],
                buckets_total=0,
            )
        buckets, more, members, buckets_total = await db.bucket_counts(
            s.pool, tenant, name, after=buckets_after, limit=buckets_limit
        )
        dials = [e.name for e in _spec_edges(index.spec) if e.type == "setting"]
        return MembershipOut(
            name=name,
            kind=_grouping_kind(index),
            fact_kind=index.kind,
            id_space=index.id_space,
            state=state,
            members=members,
            population=await db.count_kind(s.pool, tenant, index.id_space),
            buckets=[MembershipBucket(**b) for b in buckets],
            buckets_total=buckets_total,
            buckets_more=more,
            note=(
                f"Membership here reads {' and '.join(dials)}: the filing shown "
                "is from the last pass, so a dial moved since then shows only "
                "after the next one."
                if dials
                else None
            ),
        )

    @ui.get(
        "/ui/api/tenants/{tenant}/membership/{name}/members",
        response_model=MemberPageOut,
        include_in_schema=False,
    )
    async def membership_members(
        tenant: str,
        name: str,
        request: Request,
        bucket: str = "",
        after: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> MemberPageOut:
        s = _state(request)
        world, library = ready(s)
        index = library.indexes.get(name)
        if index is None:
            raise HTTPException(status_code=404, detail=_not_a_grouping(library, name))
        state = await _membership_state(s.pool, library, tenant, [name])
        if not isinstance(state, Ok):
            # 409 like `ready`'s: a fixable state (run a pass), and the page
            # that links here has already shown the sentence.
            detail = state.detail or state.because
            raise HTTPException(status_code=409, detail=detail)
        rows, more, total = await db.page_members(
            s.pool, tenant, name, bucket, index.id_space, after=after, limit=limit
        )
        name_field = taught_schema(world).name_fields.get(index.id_space)
        return MemberPageOut(
            name=name,
            bucket=bucket,
            records=[
                MemberRecordOut(
                    key=row["member"],
                    name=_field_at(row["value"], name_field),
                    held=row["value"] is not None,
                )
                for row in rows
            ],
            more=more,
            total=total,
        )

    # ------------------------------------------------------------ measured --

    @ui.get(
        "/ui/api/tenants/{tenant}/measured/{name}",
        response_model=MeasuredPageOut,
        include_in_schema=False,
    )
    async def measured(
        tenant: str,
        name: str,
        request: Request,
        after: str | None = None,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> MeasuredPageOut:
        """What a measure reads off each stored record of its kind, rendered.

        Evaluated on request rather than stored -- a measure has no version and
        stores nothing, so this is the same arithmetic a figure would run,
        shown record by record. One instant serves every clock measurement in
        the page, or the oldest wait would disagree with itself."""
        s = _state(request)
        world, library = ready(s)
        measure = library.measures.get(name)
        if measure is None:
            raise HTTPException(status_code=404, detail=_not_a_measure(library, name))
        records, more, total = await db.page_facts(
            s.pool, tenant, measure.kind, after=after, q=q, limit=limit
        )
        held_schema = taught_schema(world)
        settings = held_schema.settings_for(await db.load_settings(s.pool, tenant))
        at_ms = time.time() * 1000.0
        name_field = held_schema.name_fields.get(measure.kind)
        try:
            measured_rows = [
                MeasuredRecordOut(
                    key=row["key"],
                    name=_field_at(row["value"], name_field),
                    display=_measured_display(measure, row["value"], settings, at_ms),
                )
                for row in records
            ]
        except KeyError as gap:
            # An effort measure in a world that provides no hoursPerDay dial
            # compiles fine and only fails at render time. That is a fixable
            # configuration, so it must be the raiser's own sentence naming
            # the dial -- never a 500 the operator has to go digging for.
            raise HTTPException(
                status_code=409, detail=gap.args[0] if gap.args else str(gap)
            ) from gap
        return MeasuredPageOut(
            name=name,
            fact_kind=measure.kind,
            unit=_measure_unit(measure),
            records=measured_rows,
            more=more,
            total=total,
        )

    # ----------------------------------------------------------- activity --

    @ui.get("/ui/api/tenants/{tenant}/activity", response_model=ActivityOut, include_in_schema=False)
    async def activity(
        tenant: str,
        request: Request,
        quiet: bool = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> ActivityOut:
        s = _state(request)
        runs, hidden, total = await db.page_runs(s.pool, tenant, limit=limit, quiet=quiet)
        return ActivityOut(
            runs=[
                RunOutLog(
                    id=row["id"],
                    at=row["at"],
                    trigger=row["cause"],
                    full=row["full"],
                    written=row["written"],
                    deleted=row["deleted"],
                    changed=row["changed"],
                    not_shown=max(row["changed"] - len(row["shown"]), 0),
                    rebuilt=row["rebuilt"],
                    covered=row["covered"],
                    shown=row["shown"],
                )
                for row in runs
            ],
            total=total,
            quiet_hidden=hidden,
        )

    # ------------------------------------------------------------ answers --

    @ui.get("/ui/api/tenants/{tenant}/results/{name}", response_model=Result, include_in_schema=False)
    async def result(
        tenant: str,
        name: str,
        request: Request,
        trailing: Annotated[list[int] | None, Query()] = None,
    ) -> Result:
        """The same answer the API serves, reachable from the definition's own
        page -- refusal semantics identical to the authenticated route, so the
        UI never shows a number the API would not."""
        s = _state(request)
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            answer = await facade.answer(
                tenant,
                name,
                await db.load_settings(s.pool, tenant),
                trailing=trailing or DEFAULT_TRAILING,
            )
        except ValueError as refusal:
            raise HTTPException(status_code=400, detail=str(refusal)) from refusal
        except NotImplementedError as gap:
            raise HTTPException(status_code=501, detail=str(gap)) from gap
        if answer is None:
            raise HTTPException(status_code=404, detail=f"No definition called {name}")
        return answer

    @ui.get("/ui/api/tenants/{tenant}/evidence/{name}", response_model=Evidence, include_in_schema=False)
    async def evidence(tenant: str, name: str, subject: str, request: Request) -> Evidence:
        s = _state(request)
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            answer = await facade.evidence(
                tenant, name, subject, await db.load_settings(s.pool, tenant)
            )
        except LookupError as refusal:
            raise HTTPException(status_code=404, detail=str(refusal)) from refusal
        if answer is None:
            raise HTTPException(
                status_code=404,
                detail=f"Nothing is stored for {subject} under {name}",
            )
        return answer

    return ui


def _field_at(value: Mapping[str, Any] | None, field: str | None) -> str | None:
    """A record's schema-declared field, path-aware, or nothing -- never a
    guess. The engine's own resolver rather than a `.get`: a name field is a
    path in the schema's terms (`accounts.account_id` is legal), and every
    surface -- the facts list, the drill pages, evidence -- must name one
    record identically, so there is exactly one implementation to disagree
    with."""
    from ..engine.serve import _field_of

    return _field_of(value, field)


async def _membership_state(
    pool: Any, library: Library, tenant: str, names: Sequence[str]
) -> Availability:
    """Whether the named groupings' stored rows may honestly serve as current.

    The same two absences a figure states, detected per grouping: no built
    version anywhere means no pass ever bucketed this tenant, and a named
    grouping whose built version is missing or differs means ITS definition
    arrived or moved since -- the neighbours it never met keep serving,
    which is the whole point of per-index staleness. The one-time upgrade
    window is honoured too: a pre-0.7 whole-set stamp matching this library
    is the same proof of currency the pass's seed accepts. What this
    deliberately cannot see is a moved *bucketing dial* (an age threshold,
    a zone) with no pass since -- spec hashes exclude settings by design,
    and figures catch that case through their own fingerprints. Until a
    pass runs, membership describes the last pass.
    """
    from ..engine.engine import _index_version, _versions_if_legacy_current

    built = await db.index_versions(pool, tenant)
    legacy = None
    if not built:
        legacy = await db.legacy_index_set_version(pool, tenant)
        built = _versions_if_legacy_current(legacy, library) or {}
    stale = [
        name
        for name in names
        if name in library.indexes
        and built.get(name) != _index_version(library.indexes[name])
    ]
    if not stale:
        return Ok()
    if not built and legacy is None:
        return Unavailable(
            because="never-computed",
            detail="no pass has ever bucketed this tenant, so there is no membership to report",
        )
    # Behind-deploy either way from here -- a mismatched legacy stamp means
    # the tenant WAS bucketed, under an older library, and never-computed
    # would tell an upgraded deployment its history vanished. The detail
    # tells arrival from movement: an arrived grouping has no old rows to
    # withhold, and claiming some would send the reader hunting them.
    arrived = all(name not in built for name in stale)
    return Unavailable(
        because="behind-deploy",
        detail=(
            "this grouping arrived since the tenant's last pass; it has no "
            "buckets until one runs"
            if arrived
            else "this grouping moved since the tenant's last pass; its stored "
            "membership would describe the old text and is withheld until a pass runs"
        ),
    )


def _kind_of_name(library: Library, name: str) -> str | None:
    if name in library.indexes:
        return _grouping_kind(library.indexes[name])
    if name in library.measures:
        return "measure"
    if library.figure(name) is not None:
        return "figure"
    if library.reading(name) is not None:
        return "reading"
    if library.projection(name) is not None:
        return "projection"
    if library.summary(name) is not None:
        return "summary"
    return None


def _not_a_grouping(library: Library, name: str) -> str:
    """404 detail with a forwarding address: "no" alone dead-ends the exact
    reader the drill-down exists for."""
    held = _kind_of_name(library, name)
    if held is None:
        return f"No group or filter called {name}"
    if held == "measure":
        return (
            f"{name} is a measure, not a grouping -- the measured route lists "
            "what it reads off each record"
        )
    return (
        f"{name} is a {held}, not a grouping -- its data lives on the results "
        "route, and a figure's citations on the evidence route"
    )


def _not_a_measure(library: Library, name: str) -> str:
    held = _kind_of_name(library, name)
    if held is None:
        return f"No measure called {name}"
    if held in ("group", "filter"):
        return (
            f"{name} is a {held}, not a measure -- the membership route lists "
            "the records it filed"
        )
    return (
        f"{name} is a {held}, not a measure -- its data lives on the results "
        "route, and a figure's citations on the evidence route"
    )


def _measure_unit(measure: CompiledMeasure) -> FigureUnit:
    """What a measurement *is*, for the renderer: a duration measure produces
    seconds, a moment measure epoch milliseconds, and a field measure carries
    its declared unit (a bare field is a count)."""
    if measure.shape == "duration":
        return "duration"
    if measure.shape == "moment":
        return "moment"
    return measure.unit or "count"


def _measured_display(
    measure: CompiledMeasure,
    record: Mapping[str, Any],
    settings: Mapping[str, Any],
    at_ms: float,
) -> str | None:
    """One record's measurement, rendered -- or None, which is an absence the
    page states as one, never a nought."""
    held = measure_of(measure, record, at_ms)
    if held is None:
        return None
    return format_value(held, _measure_unit(measure), settings)


# ----------------------------------------------------------- declarations --


def _declarations(library: Library, schema: Schema) -> list[DeclarationOut]:
    """Every declaration, each with the edges to walk it back to the facts.

    Emitted in kind order (indexes, measures, figures, readings, projections,
    summaries) and library order within a kind, which is source order -- the
    page groups them itself, but a stable order keeps the payload diffable.
    """
    out: list[DeclarationOut] = []

    # Facts first: they are the leaves every trace bottoms out on, and a
    # fact-taught world whose schema was invisible here would dead-end the
    # exact reader the catalogue exists for. Schema-taught worlds have no
    # entries -- their kinds have no declaration to show.
    for name, fact in library.facts.items():
        out.append(
            DeclarationOut(
                name=name,
                kind="fact",
                version=fact.version,
                doc=declaration_prose(library, name),
                source=declaration_source(library, name),
                fact_kind=name,
                rests_on=[],
            )
        )

    for name, index in library.indexes.items():
        edges = [Dependency(type="fact", name=index.kind)]
        if index.id_space != index.kind:
            # `keyed as`: the members are another kind's ids, so that kind is
            # part of what this group rests on.
            edges.append(Dependency(type="fact", name=index.id_space))
        edges += _spec_edges(index.spec)
        out.append(
            DeclarationOut(
                name=name,
                kind=_grouping_kind(index),
                version=None,
                doc=declaration_prose(library, name),
                source=declaration_source(library, name),
                fact_kind=index.kind,
                rests_on=_dedup(edges),
            )
        )

    for name, measure in library.measures.items():
        out.append(
            DeclarationOut(
                name=name,
                kind="measure",
                version=None,
                doc=declaration_prose(library, name),
                source=declaration_source(library, name),
                unit=measure.unit,
                fact_kind=measure.kind,
                rests_on=[
                    Dependency(type="fact", name=measure.kind),
                    *_effort_edges(measure.unit),
                ],
            )
        )

    for figure in library.figures:
        # The scope kind first: the subjects, the roster the backfill writes
        # noughts over, and the labels all come from its records -- a trace
        # that reached only the kinds the sets read would miss it.
        edges = [Dependency(type="fact", name=figure.scope)]
        if figure.across is not None:
            edges.append(Dependency(type="fact", name=figure.across))
        edges += [_grouping_edge(library, n) for n in figure.indexes]
        edges += [Dependency(type="measure", name=n) for n in figure.measures]
        sources = set(figure.reads) | {src for src, _ in figure.combines.values()}
        edges += [Dependency(type="figure", name=n) for n in sorted(sources)]
        edges += [
            Dependency(type="setting", name=n)
            for n in (*figure.settings, *figure.band_settings)
        ]
        edges += _effort_edges(figure.unit)
        out.append(
            DeclarationOut(
                name=figure.name,
                kind="figure",
                version=figure.version,
                doc=figure.doc,
                source=declaration_source(library, figure.name),
                unit=figure.unit,
                rests_on=_dedup(edges),
            )
        )

    for reading in library.readings:
        edges = [Dependency(type="fact", name=reading.scope)]
        if reading.source is not None:
            edges.append(Dependency(type="figure", name=reading.source))
        if reading.live_measure is not None:
            edges.append(Dependency(type="measure", name=reading.live_measure))
        edges += [_grouping_edge(library, n) for n in reading.indexes]
        edges += [Dependency(type="setting", name=n) for n in reading.settings]
        edges += _effort_edges(reading.unit)
        out.append(
            DeclarationOut(
                name=reading.name,
                kind="reading",
                version=reading.version,
                doc=reading.doc,
                source=declaration_source(library, reading.name),
                unit=reading.unit,
                mode=reading.mode,
                rests_on=_dedup(edges),
            )
        )

    for projection in library.projections:
        figures = set(projection.figures) | {name for _, name, _, _ in projection.reads}
        edges = [Dependency(type="fact", name=projection.kind)]
        edges += [Dependency(type="figure", name=n) for n in sorted(figures)]
        edges += [_grouping_edge(library, n) for n in projection.indexes]
        edges += [Dependency(type="fact", name=j.kind) for j in projection.joins]
        edges += [Dependency(type="setting", name=n) for n in projection.settings]
        edges += _effort_edges(
            *(unit for _, _, unit, _ in projection.reads),
            *(unit for _, _, unit in projection.values),
        )
        out.append(
            DeclarationOut(
                name=projection.name,
                kind="projection",
                version=projection.version,
                doc=projection.doc,
                source=declaration_source(library, projection.name),
                fact_kind=projection.kind,
                rests_on=_dedup(edges),
            )
        )

    for summary in library.summaries:
        edges = [Dependency(type="projection", name=summary.over)]
        edges += [Dependency(type="setting", name=n) for n in summary.settings]
        edges += _effort_edges(
            *(unit for _, _, unit, _ in summary.totals),
            *(unit for _, _, unit in summary.values),
        )
        out.append(
            DeclarationOut(
                name=summary.name,
                kind="summary",
                version=summary.version,
                doc=summary.doc,
                source=declaration_source(library, summary.name),
                rests_on=_dedup(edges),
            )
        )

    _fill_moved_by(out)
    return out


def _effort_edges(*units: str | None) -> list[Dependency]:
    """The render-time dial an `effort` unit reads, as a dependency edge.

    `format_value` divides an effort by `tenant.hoursPerDay` when the number
    becomes text, so the dial never appears in any compiled plan -- and a
    closure built only from plan edges would let the page claim "nothing else
    can move this" about a number whose rendered text moves the moment the
    dial does. The edge is added at the declaration, so the closure carries
    it to everything built on top.
    """
    if any(unit == "effort" for unit in units):
        return [Dependency(type="setting", name=EFFORT_HOURS_SETTING)]
    return []


def _fill_moved_by(declarations: list[DeclarationOut]) -> None:
    """The closure walk, done once per declaration so no reader repeats it.

    Leaves only -- an intermediate declaration in `moved_by` would re-open the
    walk the field exists to close. The `seen` guard is per declaration and
    includes itself, so a future cycle (the compiler refuses them today)
    terminates rather than spins.
    """
    by_name = {d.name: d for d in declarations}
    for declaration in declarations:
        leaves: dict[tuple[str, str], Dependency] = {}
        seen = {declaration.name}
        frontier = list(declaration.rests_on)
        while frontier:
            edge = frontier.pop()
            if edge.type in ("fact", "setting"):
                leaves[(edge.type, edge.name)] = edge
            elif edge.name not in seen:
                seen.add(edge.name)
                below = by_name.get(edge.name)
                if below is not None:
                    frontier.extend(below.rests_on)
        # Facts first, then settings, each alphabetical: a stable order keeps
        # the payload diffable, and the records lead because they are the
        # answer most readers came for.
        declaration.moved_by = sorted(
            leaves.values(), key=lambda e: (e.type != "fact", e.name)
        )


_ADOPTION = (
    "The source declares its own facts, so this save retires the schema's "
    "declared kinds, name fields and url fields: record names and links come "
    "from the fact declarations alone from here on."
)

_SOURCE_CAP = 2_000_000
"""Two megabytes of definitions -- fifty times the largest real corpus --
before the editor refuses to compile. The check runs on every pause in
typing on the single-worker event loop; without a ceiling, one pasted blob
stalls every pass and socket on the deployment."""


def _within_reason(source: str) -> None:
    if len(source) > _SOURCE_CAP:
        raise HTTPException(
            status_code=413,
            detail=(
                f"This source is {len(source):,} characters; the editor compiles "
                f"at most {_SOURCE_CAP:,}. Definitions that size belong in files "
                "taught through PUT /definitions."
            ),
        )


def _fingerprint(source: str) -> str:
    """Names a source text, for the editor's edited-since-loaded check. The
    same twelve-hex convention as declaration versions, over the raw text."""
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def _refusal_out(refusal: DefinitionError) -> RefusalOut:
    line = getattr(refusal, "line", None)
    column = getattr(refusal, "column", None)
    message = getattr(refusal, "message", None) or str(refusal)
    return RefusalOut(message=message, line=line, column=column)


def _flat_fields(fields: tuple[CompiledFactField, ...], prefix: str = "") -> list[str]:
    """Field names flattened to the dotted paths definitions write: a nested
    `many abbrs: abbr` completes as `abbrs.abbr`, because that is the token a
    `through` clause needs."""
    out: list[str] = []
    for field in fields:
        if field.children:
            out += _flat_fields(field.children, f"{prefix}{field.name}.")
        else:
            out.append(f"{prefix}{field.name}")
    return out


def _kind_fields(world: World) -> dict[str, list[str]]:
    library = world.library
    if library is not None and library.facts:
        return {name: sorted(_flat_fields(f.fields)) for name, f in library.facts.items()}
    # Schema-taught: the kinds are known, their fields are not -- an empty
    # list is the honest answer, not a guess mined from stored records.
    return {kind: [] for kind in sorted(taught_schema(world).kinds)}


def _named(library: Library | None) -> dict[str, tuple[DeclarationKind, str | None]]:
    """Every declared name with its kind and version (None for the unversioned
    three, whose text hashes into their readers instead)."""
    if library is None:
        return {}
    held: dict[str, tuple[DeclarationKind, str | None]] = {}
    for name, fact in library.facts.items():
        held[name] = ("fact", fact.version)
    for name, index in library.indexes.items():
        held[name] = (_grouping_kind(index), None)
    for name in library.measures:
        held[name] = ("measure", None)
    for figure in library.figures:
        held[figure.name] = ("figure", figure.version)
    for reading in library.readings:
        held[reading.name] = ("reading", reading.version)
    for projection in library.projections:
        held[projection.name] = ("projection", projection.version)
    for summary in library.summaries:
        held[summary.name] = ("summary", summary.version)
    return held


def _changes(old: Library | None, new: Library) -> list[DeclChange]:
    """What teaching `new` over `old` does to each declaration.

    `changed` is version-moved OR calculation-text-moved, because each catches
    what the other misses: a figure's version moves when a filter it reads is
    edited (its own text untouched -- the cascade's truth), and a fact's text
    moves when its name directive is repointed (its version deliberately
    blind to rendering). Display and prose edits move neither, and are
    reported `unchanged` -- the save stores them, but no plan moves.
    """
    before = _named(old)
    after = _named(new)
    out: list[DeclChange] = []
    for name, (kind, version) in after.items():
        if name not in before or old is None:
            out.append(DeclChange(name=name, kind=kind, change="new"))
            continue
        moved = version != before[name][1] or (
            _calc_tokens(declaration_source(old, name))
            != _calc_tokens(declaration_source(new, name))
        )
        out.append(
            DeclChange(name=name, kind=kind, change="changed" if moved else "unchanged")
        )
    for name, (kind, _version) in before.items():
        if name not in after:
            out.append(DeclChange(name=name, kind=kind, change="removed"))
    return sorted(out, key=lambda change: change.name)


def _owes_a_pass(
    old: Library | None, new: Library, changes: list[DeclChange]
) -> bool:
    """Whether tenants' stored state is now behind the saved text.

    Two stores exist: figure values/pointers, and the bucketed memberships.
    So a pass is owed exactly when a figure's version moved (new, changed or
    removed) or some grouping's spec moved -- compared per grouping, with
    the same per-index hash the engine's stamps record. Readings,
    projections and summaries store nothing (computed at serve time), and
    an index label is deliberately outside the index hash: those serve
    their new text immediately, and claiming they leave tenants
    behind-deploy would invite a rebuild that moves nothing.
    """
    from ..engine.engine import _index_version

    if old is None:
        return True
    if any(c.kind == "figure" and c.change != "unchanged" for c in changes):
        return True
    return {name: _index_version(idx) for name, idx in old.indexes.items()} != {
        name: _index_version(idx) for name, idx in new.indexes.items()
    }


def _calc_tokens(text: str | None) -> object:
    """A declaration's calculation, as the lexer reads it.

    Compared instead of the raw text because the raw text over-reports: a
    re-spaced expression or a comment slipped into the body changes no token
    and moves no version, and a diff calling that `changed` makes the saved
    panel claim a behind-deploy that will never happen -- and offer a pass
    that recomputes nothing. Text the lexer refuses falls back to the text
    itself; a fragment that cannot lex has no fairer identity than its bytes.
    """
    if text is None:
        return None
    try:
        return [(token.kind, token.value) for token in lex(text)]
    except DefinitionError:
        return text


def _grouping_kind(index: CompiledIndex) -> Literal["group", "filter"]:
    """The declaration keyword, recovered from the compiled shape: a group
    fans records out (bucketed), a filter is a single narrowing bucket."""
    return "group" if index.bucketed else "filter"


def _grouping_edge(library: Library, name: str) -> Dependency:
    held = library.indexes.get(name)
    return Dependency(
        type=_grouping_kind(held) if held is not None else "group", name=name
    )


def _spec_edges(spec: IndexBy) -> list[Dependency]:
    """What an index reads beyond its own kind's records: the `through` hop
    into another kind, and the dials (`by day in <zone setting>`, age
    thresholds) that decide membership."""
    if isinstance(spec, ByAge):
        return [Dependency(type="setting", name=spec.setting)]
    if isinstance(spec, ByField):
        return _part_edges(spec.part)
    if isinstance(spec, ByComposite):
        edges: list[Dependency] = []
        for part in spec.parts:
            edges += _part_edges(part)
        return edges
    return []


def _part_edges(part: IndexField) -> list[Dependency]:
    edges: list[Dependency] = []
    if part.through is not None:
        edges.append(Dependency(type="fact", name=part.through.kind))
    if part.zone is not None:
        edges.append(Dependency(type="setting", name=part.zone))
    return edges


def _dedup(edges: list[Dependency]) -> list[Dependency]:
    seen: set[tuple[str, str]] = set()
    kept: list[Dependency] = []
    for edge in edges:
        key = (edge.type, edge.name)
        if key not in seen:
            seen.add(key)
            kept.append(edge)
    return kept

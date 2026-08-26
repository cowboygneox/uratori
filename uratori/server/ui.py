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

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from ..engine.buckets import measure_of
from ..engine.project import format_value
from ..facade import DEFAULT_TRAILING
from ..lang.ast import ByAge, ByComposite, ByField, FigureUnit, IndexBy, IndexField
from ..lang.plan import CompiledIndex, CompiledMeasure, Library
from ..lang.source import declaration_prose, declaration_source
from ..results import Availability, Evidence, Ok, Result, Unavailable
from ..schema import Schema
from . import db
from .runtime import State, facade_for, ready, state_of, taught_schema

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

    declarations: list[DeclarationOut]


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
    trigger: Literal["facts", "run"]
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


def router(frame_ancestors: str) -> APIRouter:
    ui = APIRouter()

    def _state(request: Request) -> State:
        return state_of(request)

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
            declarations=_declarations(library, world_schema) if library else [],
        )

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
                    name=_name_of(row["value"], name_field),
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
        schema = s.world.schema
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
            filed_state = await _membership_state(s.pool, library, tenant)
            # Only groupings whose members are ids of THIS kind: `keyed as`
            # files one kind's records under another kind's ids, and asking
            # "which bucket holds this commit" of a person-keyed index would
            # always answer "none" -- a stated absence the index never claimed.
            over = sorted(
                name
                for name, index in library.indexes.items()
                if index.id_space == kind
            )
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
        buckets_limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> MembershipOut:
        s = _state(request)
        _world, library = ready(s)
        index = library.indexes.get(name)
        if index is None:
            raise HTTPException(status_code=404, detail=_not_a_grouping(library, name))
        state = await _membership_state(s.pool, library, tenant)
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
        buckets, members, buckets_total = await db.bucket_counts(
            s.pool, tenant, name, limit=buckets_limit
        )
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
        state = await _membership_state(s.pool, library, tenant)
        if not isinstance(state, Ok):
            # 409 like `ready`'s: a fixable state (run a pass), and the page
            # that links here has already shown the sentence.
            detail = state.detail or state.because
            raise HTTPException(status_code=409, detail=detail)
        rows, more, total = await db.page_members(
            s.pool, tenant, name, bucket, index.id_space, after=after, limit=limit
        )
        name_field = world.schema.name_fields.get(index.id_space)
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
        settings = world.schema.settings_for(await db.load_settings(s.pool, tenant))
        at_ms = time.time() * 1000.0
        name_field = world.schema.name_fields.get(measure.kind)
        return MeasuredPageOut(
            name=name,
            fact_kind=measure.kind,
            unit=_measure_unit(measure),
            records=[
                MeasuredRecordOut(
                    key=row["key"],
                    name=_field_at(row["value"], name_field),
                    display=_measured_display(measure, row["value"], settings, at_ms),
                )
                for row in records
            ],
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


def _name_of(value: dict[str, Any], name_field: str | None) -> str | None:
    if name_field is None:
        return None
    held = value.get(name_field)
    return held if isinstance(held, str) else None


def _field_at(value: Mapping[str, Any] | None, field: str | None) -> str | None:
    """A record's schema-declared field, path-aware, or nothing -- never a
    guess. `read_path` rather than `.get` because a name field is a path in
    the schema's own terms (`accounts.account_id` is legal), and the flat
    lookup would silently un-name every record of such a kind."""
    from ..engine.buckets import read_path

    if value is None or field is None:
        return None
    found = read_path(value, field)
    text = found[0] if found else None
    return text if isinstance(text, str) and text else None


async def _membership_state(pool: Any, library: Library, tenant: str) -> Availability:
    """Whether stored membership rows may honestly be served as current.

    The same two absences a figure states, detected the same way: no
    index-set row means no pass ever bucketed this tenant, and a row naming
    a different index-set hash means the definitions moved since. What this
    deliberately cannot see is a moved *bucketing dial* (an age threshold, a
    zone) with no pass since -- the index-set hash excludes settings by
    design, and figures catch that case through their own per-definition
    fingerprints. Until a pass runs, membership describes the last pass.
    """
    from ..engine.engine import _index_set_version

    stored = await db.index_set_version(pool, tenant)
    if stored is None:
        return Unavailable(
            because="never-computed",
            detail="no pass has ever bucketed this tenant, so there is no membership to report",
        )
    if stored != _index_set_version(library):
        return Unavailable(
            because="behind-deploy",
            detail=(
                "the definitions have moved since this tenant's last pass; the stored "
                "membership describes the old text and is withheld until a pass runs"
            ),
        )
    return Ok()


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
                rests_on=[Dependency(type="fact", name=measure.kind)],
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

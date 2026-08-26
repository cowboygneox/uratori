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

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from ..facade import DEFAULT_TRAILING
from ..lang.ast import ByAge, ByComposite, ByField, IndexBy, IndexField
from ..lang.plan import CompiledIndex, Library
from ..lang.source import declaration_prose, declaration_source
from ..results import Evidence, Result
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
    "group", "filter", "measure", "figure", "reading", "projection", "summary"
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


# ----------------------------------------------------------- declarations --


def _declarations(library: Library, schema: Schema) -> list[DeclarationOut]:
    """Every declaration, each with the edges to walk it back to the facts.

    Emitted in kind order (indexes, measures, figures, readings, projections,
    summaries) and library order within a kind, which is source order -- the
    page groups them itself, but a stable order keeps the payload diffable.
    """
    out: list[DeclarationOut] = []

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

    return out


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

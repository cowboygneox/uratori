# The Python library

The service is a thin wrapper; everything it does is importable. A host that
is itself Python can construct the engine in-process and skip the HTTP hop
entirely: same schema, same definitions, same `Result` objects, no serialising
in between. This page is the embedding guide. For the model behind the words
used here -- facts, figures, versions, tenants, absences -- read
[Concepts](concepts.md) first; for running the engine as a service instead,
see [Setup](setup.md).

## Install

uratori is not yet on PyPI (publishing is planned). Until it is, install from
a checkout or straight from git:

```bash
# from a checkout
pip install -e .

# or from git
pip install "uratori @ git+https://github.com/cowboygneox/uratori"
```

Python 3.12 or newer. The core has exactly one runtime dependency, pydantic --
it is the wire: every answer is a pydantic model, so a host gets a schema
(and, through it, generated client types) rather than a dict it has to
describe twice. Two extras exist:

- `uratori[postgres]` -- adds `asyncpg`, for the Postgres store pair.
- `uratori[server]` -- adds FastAPI and uvicorn, for hosts that want to mount
  or extend the service itself. An embedding host pays for none of it.

## A complete embedding

The whole cycle -- declare a world, compile definitions, construct the
engine, push facts, run, read -- in one script that runs as written:

```python
import asyncio

from uratori import (
    MemoryEngineStore,
    MemoryFactStore,
    Schema,
    Uratori,
    compile_source,
)

WORLD = Schema(
    kinds=frozenset({"shop_order", "shop_courier"}),
    name_fields={"shop_courier": "name", "shop_order": "ref"},
    figure_settings=("limits.carrying.over",),
    defaults={"tenant": {"hoursPerDay": 8}, "limits": {"carrying": {"over": 3}}},
)

SOURCE = '''
index shop_order.carried_by from courier_id
index shop_order.open where status != "delivered"

figure shop_courier.carrying:
    """How many orders this courier is carrying right now."""
    display "{value} orders in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)

figure shop_courier.load_band:
    """Whether a courier is over the carrying limit."""
    display "{value}"
    combine:
        carrying = shop_courier.carrying
    calculate:
        when carrying >= limits.carrying.over then "over"
        otherwise "ok"
'''


async def main() -> None:
    library = compile_source(SOURCE, WORLD)

    facts = MemoryFactStore()
    engine = Uratori(
        schema=WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    engine.subscribe(
        lambda tenant, outcome, results: print(f"{tenant}: {len(outcome.changes)} moved")
    )

    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    facts.put("t1", "shop_order", "o1", {"ref": "A-1", "courier_id": "c1", "status": "riding"})

    report = await engine.run("t1", written={"shop_order": ["o1"]})
    for change in report.outcome.changes:
        print(change.figure, change.subject, change.before, "->", change.after)
    # shop_courier.carrying c1 None -> 1.0
    # shop_courier.load_band c1 None -> ok

    answer = await engine.answer("t1", "shop_courier.carrying")
    assert answer is not None and answer.state.ok
    for subject in answer.subjects:
        print(subject.name, subject.value, subject.display)   # Aki 1.0 1


asyncio.run(main())
```

Step by step:

- **`compile_source(source, schema)`** checks and lowers the definitions into
  a `Library`. A bad definition raises a `DefinitionError` with the line and
  the reason -- `SyntaxError_` from the lexer or parser, `CheckError` from
  the checker, and one `except DefinitionError` catches the lot. This is the
  same compile the service runs behind `PUT /definitions`, so what compiles
  here runs there.
- **`Uratori(schema=..., library=..., store=..., facts=...)`** takes the four
  things a host declares: the world, the compiled definitions, an
  `EngineStore` for the engine's own state, and a `FactSource` for records.
  All keyword-only, because four positional arguments of similar shape is a
  transposition waiting to happen.
- **`facts.put(...)`** writes records into the fact store. The engine never
  writes facts; how they arrive is your business.
- **`await engine.run(...)`** does a pass, re-serves the answers, and
  delivers to listeners, in that order.
- **`await engine.answer(...)`** reads one definition's current `Result` by
  name.

Note the first run needed no special flag: a tenant whose pointers are
missing (or stale, after a deploy or a moved dial) rebuilds the affected
figures from all facts on the next pass, whatever shape the call had.

## `run`, `execute`, and escalation

`run` is `execute` + `results` + listener dispatch, in the one correct order.
It returns a `RunReport` with two fields:

- `outcome` -- what the pass did. `outcome.changes` is the complete change
  stream: one `Change` per value that moved or was removed, carrying
  `figure`, `subject`, `kind` (`"moved"` or `"removed"`), both ends
  (`before`, `after`), the subject's `label` frozen at the moment of change,
  and the figure's `display` sentence. `outcome.covered` names the fact kinds
  the pass actually read, and `outcome.rebuilt` names the figures rebuilt
  from scratch -- the observable difference between a narrow settings save
  and a full rebuild is work, and without this field the two are
  indistinguishable from outside.
- `results` -- the re-served `Result`s for what the pass touched, the same
  objects a listener just received.

A failed run **raises**. It never returns an empty report, because "nothing
changed" is itself information and must stay distinguishable from "did not
finish".

Call the halves yourself when the ordering between them is yours to own --
for instance a host that must record the pass durably before responses are
built, so a failure rendering them still leaves the run on the record:

```python
outcome = await engine.execute("t1", written={"shop_order": ["o1"]})
...  # your bookkeeping here
results = await engine.results("t1", touched={c.figure for c in outcome.changes})
```

That ordering is a host decision, and `run` bundling it would take it away.

### Escalation

Two shapes of pass are escalated to a full recompute at the front door,
whatever you asked for -- both are explained in
[Concepts](concepts.md#full-passes-and-when-the-engine-escalates):

- **Any pass carrying deletions** (`deleted={"shop_order": ["o1"]}`). The
  cold branch never reads the deletion list, so a delete landing while any
  pointer is stale would leave the departed record's index memberships in
  place. Full in every branch is the only shape that cannot be wrong.
- **Any pass writing a record of a *through* kind** -- a kind that indexes
  only resolve through (`from courier_ref through shop_courier.handles`),
  with no index over the kind itself. The warm path sees the write and
  rebuilds nothing, while every record resolving through the moved row stays
  filed under the old answer.

Both are rare (departures, identity changes, operator actions), and the full
recompute is a fair price for a pass that cannot otherwise be trusted.
Ordinary writes never pay it. You can also force one yourself with
`full=True` -- useful after restoring a backup or when a host wants a
periodic reconcile.

Settings ride along on every verb: `run`, `execute`, `results` and `answer`
all take an optional sparse settings document, completed over
`schema.defaults` at the boundary, exactly once. Moving a dial needs no
special call -- pass the new document on the next run, and the pointer
fingerprints do the rest, rebuilding only the figures that name the dial.

## Serving: `results` and `answer`

`results(tenant)` with no `touched` argument is a client's first paint: the
current `Result` for every definition worth serving in bulk. With
`touched={...}` it serves only the figures named and the readings built on
them -- which is what `run` does internally, so a socket fed from listeners
pushes only what moved. Projections are served every time, with no gate, and
that is not laziness: a projection is evaluated at the instant it is asked
and the clock is one of its inputs, so no gate could be right.

Day-keyed and dimensioned figures are excluded from the bulk surface -- no
screen subscribes to every stored person-day -- but every definition serves
by name:

```python
result = await engine.answer("t1", "shop_courier.carrying")
```

`answer` returns `None` when nothing is called that, and raises for a live
reading (not servable yet) -- "no such definition" and "not built yet" send
a caller to different fixes, so they are different signals.

Readings are served over trailing windows. The default is
`DEFAULT_TRAILING = (30, 14, 7)`; pass `trailing=` to choose your own. Which
windows exist is presentation, not calculation -- a reading's statistics,
minimums and band are hashed into its version, and a window only narrows
which stored days take part. That is the only reason it may be a parameter
at all.

## Listeners

```python
unsubscribe = engine.subscribe(listener)
```

A `Listener` is any callable of `(tenant, outcome, results)`, sync or async
-- the host's choice. `subscribe` returns the detach function.

The contract, each clause pinned by a test:

- **Listeners receive the very objects `results` returns** -- the same
  `Result` instances, not copies, and the same tuple `run` reports. There is
  deliberately no listener-only shape: a second shape is a second contract,
  and a second contract is where a hand-written republishing step -- and with
  it duplicate arithmetic -- comes back. A host wiring a websocket serialises
  what it is handed and is done.
- **Async listeners are awaited before `run` returns.** A host that publishes
  to a socket from a listener needs the publish to have happened when `run`
  hands back; fire-and-forget would let a caller record a pass whose delivery
  is still in flight.
- **A raising listener is isolated.** The values are committed by the time
  delivery starts, so a listener raising costs exactly one log line -- it
  neither breaks the run nor starves the listeners after it. A subscriber bug
  must not become a board that stops computing.
- **A pass that moved nothing notifies nobody.** A poll in which nothing
  happened notifying every listener is how listeners stop being read. (One
  consequence to know about: a library containing projections re-serves them
  on every pass -- the clock moved -- so with projections loaded, listeners
  hear every pass.)

## Choosing stores

Two pairs ship, and a parity suite keeps them one behaviour.

**The in-memory pair** -- `MemoryEngineStore`, `MemoryFactStore` -- are
complete implementations, not test doubles. They are the honest smallest
deployment: a host that recomputes from facts on boot, a notebook, a test
suite. `MemoryFactStore` has a deliberately plain write surface --
synchronous `put(tenant, kind, key, value)` and `drop(tenant, kind, key)` --
and the *caller* keeps track of what moved, because the caller just wrote the
old value and can answer that question itself.

**The Postgres pair** -- `uratori.store.postgres.PostgresEngineStore` and
`PostgresFactStore`, behind `uratori[postgres]` -- each wrap an
`asyncpg.Pool`:

```python
import asyncpg
from uratori.store.postgres import PostgresEngineStore, PostgresFactStore

pool = await asyncpg.create_pool(dsn)
engine = Uratori(
    schema=WORLD,
    library=library,
    store=PostgresEngineStore(pool),
    facts=PostgresFactStore(pool),
)
```

Two things to know:

- **You own the DDL.** `uratori.store.postgres.SCHEMA_SQL` is the schema, to
  be applied from your own migration mechanism -- the library deliberately
  never migrates a database it does not own, because that is a library racing
  its host's migrations. (`tenant_id` is `text` in the shipped DDL; a host
  with narrower tenant keys can keep its own DDL as long as the columns the
  store reads and writes exist.) The *service* migrates itself at boot, but
  it owns its database; an embedded engine does not.
- **`PostgresFactStore` brings change detection and a stale-write guard.**
  `await facts.upsert(tenant, kind, records, stamps=...)` writes a batch and
  returns the keys whose value actually moved -- an identical rewrite is not
  a change, so what you pass to `run(written=...)` is exactly what needs
  recomputing. `stamps` are the provider's own updated-at instants, and they
  are the guard against the reconcile-vs-webhook race: a batch built from a
  snapshot read before an event must not put the pre-event record back. The
  comparison is `>=`, so a rewrite at the same stamp still lands (that is how
  a parser change reaches records nobody touched), and a missing stamp on
  either side means the write goes through -- a guard that cannot see the
  versions must never be the thing that drops data. `delete(tenant, kind,
  keys)` removes records; remember to tell the next pass with `deleted=`.

## Writing your own store

Both storage roles are `Protocol`s, in `uratori/store/base.py`, and an
implementation is anything with the right shape -- no base class to inherit.

`FactSource` is two methods: `of_kind(tenant, kind)` and
`some(tenant, kind, keys)`. Its narrowness is the point: no filters, no
orderings, no projections, because every method a fact source grows is a way
the calculation could start depending on where the records live. Resist the
temptation to add a filtered read for performance -- that is the cheap path
narrowing a population.

`EngineStore` is the engine's own state: definitions, per-tenant pointers,
index memberships, stored values. The docstrings on the protocol carry the
key shapes and their reasons -- most load-bearing: a value is keyed by
`(tenant, name, version, subject)`, so a definition change is a cache miss
rather than an invalidation; and a definition row is *not* tenant-scoped,
because two tenants running the same hash are running the same definition.

The executable spec is the parity suite, `tests/test_store_parity.py`. Every
scenario there runs over the in-memory pair and the Postgres pair and asserts
identical answers -- orderings, range boundaries (day ranges are inclusive at
both ends), diff reporting, the shapes a stored value may take (a number, a
word, a list, a null -- a store that coerces a null to a nought is the
one-calculation-system rule failing at the persistence layer). To trust a
store of your own, add it to that fixture's params and run the suite; a store
that greens it holds every property the engine's own tests assume.

## The async model

Every engine verb is a coroutine: `execute`, `run`, `results` and `answer`
must be awaited, and both store protocols are async throughout. On the
interaction path, what stays synchronous is `subscribe` (and the detach
function it returns), `compile_source`, `Schema` construction and
`MemoryFactStore.put`/`drop`.

The engine holds no event-loop state of its own, but your stores may:
`asyncpg` pools are bound to the loop that created them, so create the pool
and drive the engine on the same loop. Under pytest, that means one loop for
the whole session rather than one per test (this repository sets
`asyncio_default_test_loop_scope = "session"` for exactly that reason -- a
loop per test hands the second test connections bound to a loop that has
already closed).

There is no internal queue or background task: a pass happens when you await
`run`, and listener delivery has completed when it returns. Concurrency
control is yours -- if two syncs for one tenant can race in your host,
serialise them around the call.

## Embed, or deploy the service?

Embed when the host is Python and wants to own the surrounding machinery:
facts written in the host's own transactions, passes triggered from its own
sync pipeline, results served through its own API, listeners feeding its own
socket. The engine is then a library with opinions about storage keys and
nothing else -- no network in the middle, no second process to operate.

Deploy [the service](setup.md) when more than one process (or language)
needs the answers, or when you want schema and definition persistence,
per-tenant settings storage, auth and a websocket handled for you -- the
service is this same library behind HTTP, nothing more, which is why what
compiles and computes in-process behaves identically deployed. The routes are
documented in [the HTTP & websocket API](http-api.md).

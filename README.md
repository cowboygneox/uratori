# uratori (裏取り)

[![CI](https://github.com/cowboygneox/uratori/actions/workflows/ci.yml/badge.svg)](https://github.com/cowboygneox/uratori/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)

**A definition engine you can deploy.** Write what your numbers *mean* in a
small definition language; push facts at the engine; read back computed,
versioned, explainable answers -- over HTTP, over a websocket, or in-process.

裏付け (*urazuke*) is the backing a claim has; 裏取り (*uratori*) is the act of
going and getting it. This engine grew out of [urazuke](https://urazuke.com),
a standup board whose whole premise is that no number on screen is
unexplained; uratori is that board's calculation core with the product carved
away.

---

## The model

```
your app ──facts──▶ uratori ──Results──▶ your screens
             │         │
          schema   definitions (.fig)
```

- **A `Schema`** declares your world once: which fact kinds exist, which field
  carries a record's human name, which settings dials a definition may read,
  and their defaults.
- **Definitions** (`.fig`) declare what to compute: *indexes* bucket records,
  *measures* read quantities off them, *figures* are stored per-subject values
  recomputed incrementally as facts move, *readings* summarise stored days,
  *projections* assemble live rows at the instant they are asked.
- **Facts** are plain JSON records, pushed in batches. The engine's own change
  detection decides what moved, and a cascade recomputes exactly the figures
  that depended on it -- including figures built on other figures.
- **Results** are one envelope for every answer, with the definition's version
  (a content hash: the citation) and the evidence behind it. An absence is
  never a zero: a missing answer says *why* it is missing.

Every definition change is a new version, computed fresh; the old version's
values stay intact to explain any history that cites them. A version is the
hash of the definition's semantics -- prose changes don't fork it, and the
versions the server reports after `PUT /definitions` are the ones your own
build compiled, so "the server runs what I reviewed" is a check, not a hope.

## Start it

With a Postgres of its own to point at:

```bash
docker run -p 8080:8080 \
  -e DATABASE_URL=postgres://user:pass@your-postgres:5432/uratori \
  cowboygneox/uratori:latest
```

Or clone this repository and `docker compose up` for an engine with its
database beside it. Then teach it and feed it:

| Verb | What it does |
|---|---|
| `PUT /schema`, `GET /schema` | Declare (or replace) the world; read it back. A replacement is refused whole if the loaded definitions no longer compile under it. |
| `PUT /definitions`, `GET /definitions` | Compile and load source (a bad definition is a 422 in the checker's own words); read back names and versions. |
| `PUT /tenants/{t}/settings` | Store a tenant's sparse dial document. |
| `POST /tenants/{t}/facts` | Apply writes/deletes (with the provider's own stamps as the stale-write guard), run the pass, get back counts, a ranked change sample, and the re-served `Result`s. |
| `POST /tenants/{t}/runs` | A pass with no new facts (a moved dial, `{"full": true}` to rebuild). |
| `GET /tenants/{t}/results[/{name}]` | Current answers. |
| `DELETE /tenants/{t}` | Every row the tenant owns, gone; answers the counts, because "ok" is the least useful true thing a destructive route can say. |
| `WS /stream` | Subscribe to a tenant; get the current answers, then every one that moves. |
| `GET /health` | `{ok, version, ready, figures, readings}`. |

Set `URATORI_TOKEN` to require `Authorization: Bearer …` on everything but
`/health` -- the websocket included, header only, since a token in a query
string lands in every access log on the way. `DATABASE_URL` names a Postgres
database of uratori's own; it refuses one that belongs to something else, and
it migrates itself at boot.

Images live on [Docker Hub](https://hub.docker.com/r/cowboygneox/uratori) as
`cowboygneox/uratori` and on GHCR as `ghcr.io/cowboygneox/uratori`, built for
amd64 and arm64. If your Postgres is a container on the same machine, the
`DATABASE_URL` host is `host.docker.internal`, not `localhost` -- inside the
container, `localhost` is the container.

## Documentation

| | |
|---|---|
| [Concepts](docs/concepts.md) | Facts, schemas, definitions, versions, tenants -- the model in full. |
| [Setup](docs/setup.md) | Deploying the container: environment, database, token, health, upgrades. |
| [HTTP & websocket API](docs/http-api.md) | Every route and frame, with request and response shapes. |
| [The definition language](docs/language.md) | Writing `.fig`: indexes, measures, figures, readings, projections. |
| [The Python library](docs/library.md) | Embedding the engine in-process: `Schema`, `Uratori`, stores, callbacks. |

## The library

The service is a thin wrapper; everything is importable. In sketch (the
runnable version, line by line, is [the library guide](docs/library.md)):

```python
from uratori import Schema, Uratori, MemoryEngineStore, MemoryFactStore, compile_source

schema = Schema(kinds=frozenset({"shop_order", "shop_courier"}),
                name_fields={"shop_courier": "name"})
library = compile_source(open("defs.fig").read(), schema)

facts = MemoryFactStore()
engine = Uratori(schema=schema, library=library, store=MemoryEngineStore(), facts=facts)
engine.subscribe(lambda tenant, outcome, results: ...)   # every movement, served

facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
facts.put("t1", "shop_order", "o1", {"ref": "A-1", "courier_id": "c1", "status": "riding"})
report = await engine.run("t1", written={"shop_courier": ["c1"], "shop_order": ["o1"]})
```

Stores are protocols: `uratori.store.postgres` ships the default pair (the
`postgres` extra; install from a checkout for now -- PyPI is planned), the
in-memory pair is complete and shipped, and a parity suite keeps the two one
behaviour.

## The rules it inherits

1. **One calculation system.** A number means a definition; there is no second
   place arithmetic can live.
2. **Clients compute nothing.** Values arrive rendered beside their magnitudes;
   the payload carries no raw collection to recompute from.
3. **An absence is never a zero.** Missing means *not computed*, and every
   response says why (`never-computed`, `behind-deploy`, `setting-moved`,
   `nothing-collected`).
4. **A cheap path may not narrow a population** -- not the one a calculation
   runs over, nor the one it is reported over.

## Development

```bash
uv venv --python 3.12 && uv pip install -e ".[server,dev]"
export TEST_DATABASE_URL="postgres://user:pass@localhost:5432/uratori_test"
.venv/bin/python -m pytest && .venv/bin/python -m mypy uratori && .venv/bin/python -m ruff check uratori tests
```

The Postgres-backed tests fail rather than skip without `TEST_DATABASE_URL`;
they keep their tables in a schema of their own, so the database can be shared
with other suites.

## License

[BUSL-1.1](LICENSE): production use is granted for anything *except* two
things -- offering the engine itself as a hosted or managed service, in any
domain; and using or embedding it in a product or service for
project-management or software-delivery insights, the space
[urazuke](https://urazuke.com) occupies. The Additional Use Grant in the
LICENSE names both in full. Internal tools and embedding in products outside
that space are fine. Each release becomes plain Apache 2.0 four years after
it ships.

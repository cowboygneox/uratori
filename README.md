# uratori (裏取り)

[![CI](https://github.com/cowboygneox/uratori/actions/workflows/ci.yml/badge.svg)](https://github.com/cowboygneox/uratori/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)

Your product shows numbers. Ask what one of them actually *means* -- which
items it counts, why it moved overnight, why two screens disagree -- and the
answer is usually a dig through code that was never written to be read. The
people who own a metric cannot read the place it is defined, and the people
who defined it cannot prove it does what was agreed.

uratori closes that gap. What every number means is written down in a small
definition language that a product owner can read and an engineer can
review, and the engine computes exactly what is written -- nothing else
computes anything:

```
group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{value} orders in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)
```

Those lines are the whole of what the number means: which records count as
whose, the explanation served wherever the number is cited, the sentence a
screen prints, and the calculation, in one reviewable place. You feed the engine plain JSON records -- your orders, your couriers,
whatever your world is made of; uratori calls them **facts** -- and it keeps
every defined number current as they change, serving each answer with the
version of the definition that computed it and the evidence behind it, over
HTTP and over a websocket.

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

- **Facts** are the plain JSON records you push, in batches. The engine's own
  change detection decides what moved, and a cascade recomputes exactly the
  figures that depended on it -- including figures built on other figures.
- **A `Schema`** declares your world once -- which fact kinds exist, which
  field carries a record's human name, which carries its link -- or the world
  lives in the definitions themselves as `fact` declarations and the schema
  carries nothing. There are no settings: every number a definition needs
  comes from a fact or is written in the definition.
- **Definitions** (`.fig`) declare what to compute: *groups* bucket records
  by a field, *filters* narrow them to whatever passes a test, *measures*
  read quantities off them, *figures* are stored per-subject values
  recomputed incrementally as facts move, *readings* summarise stored buckets,
  *projections* assemble live rows at the instant they are asked.
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
| `PUT /definitions`, `GET /definitions` | Compile and load source (a bad definition is a 422 in the checker's own words); read back the library described -- names, versions, prose, formulas and what each rests on. |
| `POST /tenants/{t}/facts` | Apply writes/deletes (with the provider's own stamps as the stale-write guard), run the pass, get back counts, a ranked change sample, and the re-served `Result`s. `defer: true` writes without the pass, for bulk imports that close with one full run. |
| `POST /tenants/{t}/runs` | A pass with no new facts (a redeployed definition, `{"full": true}` to rebuild). |
| `GET /tenants/{t}/results[/{name}]` | Current answers. |
| `GET /tenants/{t}/evidence/{name}?subject=…` | The records behind one stored value: the citation, joined back to the records it names. |
| `DELETE /tenants/{t}` | Every row the tenant owns, gone; answers the counts, because "ok" is the least useful true thing a destructive route can say. |
| `WS /stream` | Subscribe to a tenant; get the current answers, then every one that moves. |
| `GET /health` | `{ok, version, ready, figures, readings}`. |

Set `URATORI_TOKEN` to require `Authorization: Bearer …` on everything but
`/health` -- the websocket included, header only, since a token in a query
string lands in every access log on the way. `DATABASE_URL` names a Postgres
database of uratori's own; it refuses one that belongs to something else, and
it migrates itself at boot.

An open server also serves a [built-in investigation UI](docs/ui.md) at
`/ui/` -- the library as written with dependencies traced down to the facts,
the stored records themselves, an activity log of what each pushed fact
cascaded to, and an editor that teaches definitions the same way
`PUT /definitions` does, checked by the real compiler as you type. It is
unauthenticated by design (the firewall is the door), so setting
`URATORI_TOKEN` turns it off unless `URATORI_UI=on` says otherwise; editing
is a second grant (`URATORI_UI_EDIT`), off by default beside a token.

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
| [The definition language](docs/language.md) | Writing `.fig`: facts, groups, filters, measures, figures, readings, projections, summaries, bundles. |
| [The NFL example](examples/nfl/) | A loadable showcase: the whole play-by-play era in one tenant -- every play a fact, bucketed by season, game number and weekday -- every construct the language has in one library. |
| [The built-in UI](docs/ui.md) | The investigation surface at `/ui/`: definitions as written, dependency traces, facts, and the activity log. |

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
.venv/bin/python -m pytest && .venv/bin/python -m mypy uratori examples/nfl/load.py && .venv/bin/python -m ruff check uratori tests examples
```

The Postgres-backed tests fail rather than skip without `TEST_DATABASE_URL`;
they keep their tables in a schema of their own, so the database can be shared
with other suites.

**A language construct is not landed until every `/ui` surface renders
everything it carries.** New wire fields, declaration kinds and result kinds
are enumerated by `tests/test_ui_parity.py`, which goes red naming the
unrendered surface -- render the addition, or write the reason it stays off
the page into that test's allowlist. The history that earned this rule:
bundles reached the language two releases before the page could show one,
and a reading's `series` travelled on the wire with nothing drawing it.

## License

[BUSL-1.1](LICENSE): production use is granted for anything *except* two
things -- offering the engine itself as a hosted or managed service, in any
domain; and using or embedding it in a product or service for
project-management or software-delivery insights, the space
[urazuke](https://urazuke.com) occupies. The Additional Use Grant in the
LICENSE names both in full. Internal tools and embedding in products outside
that space are fine. Each release becomes plain Apache 2.0 four years after
it ships.

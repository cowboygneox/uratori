# The NFL, defined

A complete, loadable demonstration of everything the engine and the
definition language can do, at scale, over a world everybody already
understands: teams play games, players rack up yards, and somebody is
always asking whether their team is any good. **Every single play since
1999 is a fact** -- roughly 1.3 million of them across the era -- and a
game's plays are imported only after they rebuild its final score exactly,
so what the figures count is play-by-play the data itself has vouched for.

Three files do all the work:

| | |
|---|---|
| `schema.json` | The dials a tenant can turn, and their defaults. |
| `definitions.fig` | The world -- six `fact` declarations, every field typed -- and every number the demo serves, defined beside it. Each construct the language has appears at least once, with its explanation attached. |
| `load.py` | The host: downloads seasons from [nflverse](https://github.com/nflverse/nflverse-data), shapes them into plain records, teaches the engine and pushes the facts. Stdlib only. |

Because the facts are declared in the language, every field a definition
reads is checked at compile time, and every record the loader pushes is
verified at the write boundary -- a drifted field name is a refused batch
naming the kind, key and field, never a silently empty bucket.

The data is nflverse's community-maintained public record of NFL games,
weekly player stat lines and play-by-play. The loader computes no figure of
its own -- it shapes records, and every number on every screen comes from a
definition, served with its version and its evidence.

The one check the loader does make is about the data, not the numbers: per
game, the points its plays claim must add up to the final score on the
schedule, both teams, exactly. A played game that fails -- points missing,
points invented, no play-by-play at all -- is excluded whole (game, sides,
stat lines and plays) and counted out loud, because a silent cap reads as
"covered everything". Fixtures with no result yet have nothing to be
inaccurate about, and load as fixtures. Play clocks get the same treatment:
a game's wall-clock stamps are kept only when they agree with its scheduled
kickoff, which quietly retires the early seasons that recorded local time
wearing a UTC suffix -- their plays load, their clocks do not.

## Run it

From the repository root, an engine with its database beside it, then the
loader:

```bash
docker compose up -d
python examples/nfl/load.py            # the whole era: --seasons 1999-2026
python examples/nfl/load.py --seasons 2024-2026   # or start smaller
```

Each season is a **tenant** -- the same definitions over different facts,
which is exactly what a tenant is. The full era downloads roughly 650 MB of
nflverse CSV on the first run (cached under `examples/nfl/data/`), and the
engine computes as it ingests -- expect a few minutes per season, with the
run report printed batch by batch, and around 70,000 facts per tenant when
it settles.

Reloading against a database taught before the fact declarations existed
is handled in place: the settings-only schema is refused beside the stored
kind-declaring world, so the loader teaches the new definitions first --
which retires the stored kinds without touching a single tenant -- and
then lands the schema. Nothing needs wiping.

Then look around. The built-in UI is the guided tour -- every definition,
its source, its dependency trace down to the records, and the run log of
every pass:

```
http://localhost:8080/ui/
```

Or straight to the answers:

```bash
# The standings: stored figures side by side, ordered by the server.
curl -s localhost:8080/tenants/2025/results/nfl_team.standings | jq .

# A season of scoring, day by day, with the sparkline series behind the
# total. Windows are the client's one choice; 365 covers a whole season.
curl -s 'localhost:8080/tenants/2025/results/nfl_team.scoring?trailing=365' | jq .

# Why does that number say 14 wins? The evidence: every game it counted.
curl -s 'localhost:8080/tenants/2025/evidence/nfl_team.wins?subject=GB' | jq .
```

## What to look at, construct by construct

**The standings** (`nfl_team.standings`) is a projection with nine `read`
bindings per row over the stored regular-season figures -- counts, sums, a
rollup (`point_diff` is built *on* `points_scored` and `points_allowed`, so
the difference and its parts cannot disagree), a share with a `band:`
(`win_rate`, whose value and whose band word arrive as two bindings off one
figure), and a word figure (`form`) built on that share. Losses are derived
in the row from played, wins and ties -- the one place that arithmetic
belongs. The postseason is deliberately not in these columns; it has its
own page (`nfl_game.postseason`), whose population is a set expression --
`from nfl_game.finished & nfl_game.playoff`.

**The scoreboard and its summary** (`nfl_game.scoreboard`,
`nfl_game.season`) show the projection language: fields off the record,
joins through the team kind for full names, values with ladders and
`days from ... to now`, `max()` for the absolute margin, flags with
server-rendered sentences, and a one-row summary whose counts are defined
as being over the projection's whole population -- never the page.

**The upcoming page** (`nfl_game.upcoming`) is `omit` doing the one thing
`from` cannot: a population that moves with the clock. A game drops off the
page the moment its score lands, decided fresh at every ask. Load the next
season's schedule (tenant `2026`) and this page is the point: fixtures with
`days_until`, while the stored team figures honestly answer
`nothing-collected` -- an absence with a reason, never a fabricated zero.

**The offence page** (`nfl_team.offense`) is where the play-scale figures
surface: snaps run, yards gained, giveaways and the third-down conversion
rate -- a share banded against dials -- every one computed over the actual
plays. `GET /tenants/2024/evidence/nfl_team.turnovers?subject=DAL` lists
the exact plays the number counts, by name.

**Rest between games** (`nfl_team.rest`) is the duration pipeline: a
measure subtracting two moments (`kickoff - previous_kickoff`), a
time-keyed `list` figure, and a windowed reading taking `mean`, `median`
and `worst` -- withheld together under two games, and banded `high`
against a `{good, poor}` dial. Thursday football is visible from here.

**Scoring rhythm** (`nfl_team.scoring_rhythm`) is the sub-day grain:
scoring plays with a verified wall clock, stored into quarter-hours of the
tenant's own calendar, then grouped into hours at read time --
`series(slots) by hour`. Ask for a Sunday and watch the league's afternoon.

**Touchdowns by opponent** (`nfl_player.tds_versus`, `nfl_player.touchdowns`)
is `across` and the rollup: one value per (player, franchise) pair, and a
total defined as the sum of its parts. Fetch the total's evidence and you
get the parts, each citing its own figure -- not the records re-counted a
second way. The stat is touchdowns *accounted for*: a thrown touchdown
credits the passer and the catcher alike, which is why the quarterbacks
top the leaders page -- and the explanation served with the number says
exactly that.

**The franchise hop.** Every group over games reaches its team `through
nfl_team.abbrs.abbr`: a franchise is one subject however the sheet spells
it. Load `2016` and the Raiders' record says OAK while the subject is the
same one `2025` calls LV -- the identity join a person with two logins
needs, wearing shoulder pads.

**The dials.** Every threshold in the definitions is a named setting a
tenant can move -- and moving one recomputes exactly what read it. The
stored document is the tenant's whole (sparse) settings document, so send
everything you mean in one `PUT`:

```bash
# Blowouts start at 10 points now, and contending starts at 75%. The first
# is a projection dial (free to turn); the second is a figure dial, which
# marks what read it `setting-moved` until a pass recomputes.
curl -s -X PUT localhost:8080/tenants/2025/settings \
  -H 'Content-Type: application/json' \
  -d '{"document": {"thresholds": {"blowout": 10, "contending": {"rate": 0.75}}}}'
curl -s -X POST localhost:8080/tenants/2025/runs \
  -H 'Content-Type: application/json' -d '{}'
```

**A live season.** `nfl_team.recent_wins` and the age filter behind it read
the wall clock; over a finished season they are honestly nought. Re-run the
loader weekly during the season -- pushes are idempotent, the engine's own
change detection decides what moved, and the websocket at `/stream` carries
every figure that did.

## Showing it to the world

The compose file is the local playground: no token, and therefore (by the
server's own default) the investigation UI is on. Exposing that to the
public exposes **write** routes too -- anyone could push facts or delete a
tenant. Put a proxy in front that forwards only reads:

```nginx
location / {
    # GET everywhere (results, evidence, definitions, the UI, /health);
    # every mutating verb stays inside.
    limit_except GET { deny all; }
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
}

location /stream {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1h;
}
```

Load and update the tenants from inside that boundary (or over a second,
private vhost). Setting `URATORI_TOKEN` instead protects everything but
also turns the UI off by default -- the right posture for an instance that
is anybody's business but yours, the wrong one for a public showcase.

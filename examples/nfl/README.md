# The NFL, defined

A complete, loadable demonstration of everything the engine and the
definition language can do, at scale, over a world everybody already
understands: teams play games, players rack up yards, and somebody is
always asking whether their team is any good. **Every single play since
1999 is a fact** -- roughly 1.3 million of them, two million records in
all -- and a game's plays are imported only after they rebuild its final
score exactly, so what the figures count is play-by-play the data itself
has vouched for.

The whole era lives in **one tenant**. The history is not partitioned; it
is *bucketed*, by declaration: the same games and plays are read per
franchise across the era, per season, per game number within a season, per
day of the week, per calendar day and per quarter-hour of the clock. Each
cut is a definition -- a season is a subject, a game slot is a subject, a
Thursday is a subject -- and each number is served with its version and
the records behind it.

Three files do all the work:

| | |
|---|---|
| `schema.json` | The dials a tenant can turn, and their defaults. |
| `definitions.fig` | The world -- ten `fact` declarations, every field typed -- and every number the demo serves, defined beside it. Each construct the language has appears at least once, with its explanation attached. |
| `load.py` | The host: downloads seasons from [nflverse](https://github.com/nflverse/nflverse-data), shapes them into plain records, teaches the engine and pushes the facts. Stdlib only. |

Because the facts are declared in the language, every field a definition
reads is checked at compile time, and every record the loader pushes is
verified at the write boundary -- a drifted field name is a refused batch
naming the kind, key and field, never a silently empty bucket.

The data is nflverse's community-maintained public record of NFL games,
weekly player stat lines and play-by-play. The loader computes no figure of
its own -- it shapes records, and every number on every screen comes from a
definition, served with its version and its evidence. Its inventions are
identity -- which franchise an abbreviation belongs to (OAK and LV are one
subject), and which *team-season* a record belongs to, in that season's
own spelling, so the 2003 entry on the greatest-seasons page is the
Oakland Raiders, whoever holds the franchise now -- plus two derivations
the language deliberately does not own: the weekday a kickoff falls on,
and the game's ordinal within its team's season (a rank over the whole
schedule, which no single record carries).

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

The full era downloads roughly 650 MB of nflverse CSV on the first run
(cached under `examples/nfl/data/`). The loader pushes every batch with
`defer: true` -- the engine verifies and stores each one and runs no pass,
because a pass per batch would re-read buckets every earlier batch already
filled -- and closes the import with one `POST /runs {"full": true}` that
computes everything. Expect the pushes to take a few minutes per season
and the closing run to be the long part: minutes for a couple of seasons,
around an hour for the era. Until it finishes, results honestly answer
`never-computed`.

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
# The greatest seasons of the era: every (team, season) ranked by the
# win rate the server computed, its offence's numbers beside it.
curl -s localhost:8080/tenants/nfl/results/nfl_team_season.best | jq .

# The league season by season: games, points, points per game, snaps.
curl -s localhost:8080/tenants/nfl/results/nfl_season.eras | jq .

# Career touchdown leaders, era-deep.
curl -s localhost:8080/tenants/nfl/results/nfl_player.leaders | jq .

# Why does that row say 16 wins? The evidence: every game it counted.
curl -s 'localhost:8080/tenants/nfl/evidence/nfl_team_season.wins?subject=2007-NE' | jq .
```

## What to look at, construct by construct

**The bucketing.** One set of records, four cuts, each a page:

- **Per season** -- `nfl_season.eras`: a season is a subject
  (`group nfl_game.by_season from season`), so games, points, points per
  game (banded: the scoring climate) and league-wide snaps land one row
  per year, 1999 to now.
- **Per team-season** -- `nfl_team_season.best`: the greatest seasons of
  the era, ranked by win rate, with each season's own giveaways and
  third-down conversion computed from its actual plays. The 16-0 season
  carries a `Perfect regular season` flag the server rendered.
- **Per game number** -- `nfl_game_slot.pace`: every franchise's Game 1 in
  one bucket, every Game 17 in another, era-wide -- does scoring climb as
  offences warm up?
- **Per day of the week** -- `nfl_weekday.slate`: Sunday's thousands of
  games against Thursday's short weeks. A day with no football reads a
  real 0 games and an honest dash for the average -- division by nought
  answers nothing.

**The split and its rollup** (`nfl_team.wins_by_season`, `nfl_team.wins`)
is `across` at era scale: wins per (franchise, season) as one figure, and
the franchise's era wins defined as the sum of those parts -- the split
and the total cannot disagree, and the total's evidence *is* the seasons.

**The era standings** (`nfl_team.standings`) is a projection with nine
`read` bindings per row over a quarter of a century: counts, sums, a
rollup (`point_diff` is built *on* `points_scored` and `points_allowed`),
a share with a `band:`, and a word figure (`form`) built on that share.
Losses are derived in the row from played, wins and ties -- the one place
that arithmetic belongs. The postseason is deliberately not in these
columns; it has its own page (`nfl_game.postseason`), whose population is
a set expression -- `from nfl_game.finished & nfl_game.playoff` -- and now
carries every playoff game since 1999.

**The scoreboard and its summary** (`nfl_game.scoreboard`,
`nfl_game.history`) show the projection language: fields off the record,
joins through the team kind for full names, values with ladders and
`days from ... to now`, `max()` for the absolute margin, flags with
server-rendered sentences, and a one-row summary whose counts are defined
as being over the projection's whole population -- seven thousand games,
never the forty on the page.

**The upcoming page** (`nfl_game.upcoming`) is `omit` doing the one thing
`from` cannot: a population that moves with the clock. A game drops off the
page the moment its score lands, decided fresh at every ask. Next season's
fixtures sit here with `days_until`, and its team-season rows do not exist
yet -- they appear when the season's football does, because a table of
played-0 won-0 noughts about games that never happened is exactly the
fabricated zero this engine refuses elsewhere.

**The offence page** (`nfl_team.offense`) is where the play-scale figures
surface: snaps run, yards gained, giveaways and the third-down conversion
rate -- a share banded by its own ladder -- every one computed over the actual
plays, era-deep.
`GET /tenants/nfl/evidence/nfl_team_season.giveaways?subject=2025-MIN`
lists the exact plays a season's number counts, by name.

**Rest between games** (`nfl_team.rest`) is the duration pipeline: a
measure subtracting two moments (`kickoff - previous_kickoff`), a
time-keyed `list` figure, and a windowed reading taking `mean`, `median`
and `worst` -- withheld together under two games, and banded by a ladder
over the mean. The gaps are within-season by construction:
an opener carries no previous kickoff, because an off-season is not a bye
week.

**Scoring rhythm** (`nfl_team.scoring_rhythm`) is the sub-day grain:
scoring plays with a verified wall clock, stored into quarter-hours and,
under its own name, into hours of the tenant's own calendar -- two
declarations, two hashes, each bucketing the plays directly. The reading
walks the hour figure's sequence, so `?trailing=1-48` is two days of
hours and each series point is one of them. Ask for a window and watch
the league's afternoon.

**Careers** (`nfl_player.tds_versus`, `nfl_player.tds_by_season`,
`nfl_player.touchdowns`) are `across` twice and a rollup: the same
touchdowns split by opponent franchise and by season, and a career total
defined as the sum of the per-opponent parts. Fetch the total's evidence
and you get the parts, each citing its own figure -- not the records
re-counted a second way. The stat is touchdowns *accounted for*: a thrown
touchdown credits the passer and the catcher alike, which is why the
quarterbacks top the leaders page -- and the explanation served with the
number says exactly that.

**The franchise hop.** Every group over games reaches its team `through
nfl_team.abbrs.abbr`: a franchise is one subject however the sheet spells
it. The 2003 Raiders' games say OAK, the 2026 fixtures say LV, and the era
standings count both under one row -- the identity join a person with two
logins needs, wearing shoulder pads. The team-*seasons* stay era-spelt on
purpose: relocation is franchise identity, not a rename of history.

**The dials.** Every threshold in the definitions is a named setting a
tenant can move -- and moving one recomputes exactly what read it. The
stored document is the tenant's whole (sparse) settings document, so send
everything you mean in one `PUT`:

```bash
# Blowouts start at 10 points now, and a contending season starts at 75%.
# The first is a projection dial (free to turn); the second is a figure
# dial, which marks what read it `setting-moved` until a pass recomputes.
curl -s -X PUT localhost:8080/tenants/nfl/settings \
  -H 'Content-Type: application/json' \
  -d '{"document": {"thresholds": {"blowout": 10, "contending": {"rate": 0.75}}}}'
curl -s -X POST localhost:8080/tenants/nfl/runs \
  -H 'Content-Type: application/json' -d '{}'
```

**A live season.** `nfl_team.recent_wins` and the age filter behind it read
the wall clock; between seasons they are honestly nought. Re-run the loader
weekly during the season -- pushes are idempotent, the engine's own change
detection decides what moved, and the websocket at `/stream` carries every
figure that did. Use `--update` for those refreshes: it pushes without
`defer` and skips the closing full run, because the ordinary per-batch
pass is exactly right when the batch is one week of football.

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

Load and update the tenant from inside that boundary (or over a second,
private vhost). Setting `URATORI_TOKEN` instead protects everything but
also turns the UI off by default -- the right posture for an instance that
is anybody's business but yours, the wrong one for a public showcase.

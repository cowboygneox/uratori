# Running the tutorial's world

## Two lines

```bash
cd docs/tutorial
docker compose up
```

Then open **<http://localhost:8080/ui/>**.

That brings up Postgres, the engine, and a one-shot seeder that loads the
tutorial's definitions and its eighty records into a tenant called `desk` and
then exits. When the seeder's output stops, everything in
[the tutorial](../tutorial.md) is there to look at.

Start reading at [`docs/tutorial.md`](../tutorial.md).

## What is in here

| file | |
|---|---|
| [`support.fig`](support.fig) | every definition the tutorial teaches, in one compiling file, in tutorial order |
| [`facts.json`](facts.json) | the dataset, exactly as [`docs/tutorial.md`](../tutorial.md) prints it |
| [`seed.py`](seed.py) | schema, definitions, facts, and two example reads. Standard library only |
| [`docker-compose.yml`](docker-compose.yml) | the one-command stack |
| [`check_tutorial.py`](check_tutorial.py) | compiles every `.fig` block in every chapter, and `support.fig` itself |
| `01-` … `13-` | the chapters |

## What to look at first

The UI at `/ui/` is the engine's own investigation surface -- built for the
developer standing behind the firewall, not for end users, which makes it
exactly the right thing to poke at while reading.

- **Definitions** -- every declaration as written, with its version hash (the
  citation every number carries), the prose above it, and three answers:
  *moved by* (the fact kinds a change to which can move this number, and
  nothing else can), *built from*, and *used by*.
- **A value's worksheet** -- the best thing here for this tutorial's reader.
  Try <http://localhost:8080/ui/#/work/desk_agent.in_hand/ag-mira>: one number,
  taken apart, each step of the calculation with its value beside it.
- **Facts** -- the eighty records, by kind, each expanding to its stored JSON.
  A record's own page walks both ways: what it landed in, and every number it
  counted toward.
- **Activity** -- one entry per pass, newest first, cause before effect.

## Going back to a clean world

```bash
docker compose down -v && docker compose up
```

`-v` removes the named volume `uratori-tutorial-data`, which is where the
schema, the definitions, every fact and every computed value live. Without
`-v` the state survives, which is what you want between reading sessions.

## Without Docker

Any uratori you can reach will do. Point the seeder at it:

```bash
python docs/tutorial/seed.py --base http://localhost:8080 --tenant desk
```

To bring an engine up by hand, see [`docs/setup.md`](../setup.md). The short
version, with a Postgres of uratori's own to point at:

```bash
docker run -p 8080:8080 \
  -e DATABASE_URL=postgres://user:pass@your-postgres:5432/uratori \
  cowboygneox/uratori:latest
```

Set no `URATORI_TOKEN`: the UI is mounted exactly when there is no token, and
the UI is most of what makes this tutorial worth running.

If the server *does* have a token, the seeder takes one:

```bash
python seed.py --base https://your-engine --tenant desk --token "$URATORI_TOKEN"
```

## Checking the tutorial still holds

```bash
python docs/tutorial/check_tutorial.py
```

Compiles `support.fig` and every fenced `.fig` block in every chapter -- each
block against `support.fig` truncated at the first declaration that block
shows, which is exactly what the tutorial had introduced by then. A chapter
that reaches forward to something it has not taught yet fails here too.

## Two things that move with the clock

Almost every number in the tutorial is fixed: counts, medians, monthly
totals. Three kinds are not, and the chapters say so where they appear.

- **Ages.** The queue page's `waiting_days`, and anything that reads it --
  `overdue`, the flags, the summary's totals. A projection stores nothing and
  reads the clock at the instant you ask, so these are larger on your copy
  than in the text.
- **Age filters.** `desk_ticket.stale` holds more tickets now than it did on
  30 June, for the same reason.
- **How far a carried sequence runs.** `desk_team.reply_target_month` extends
  to whatever month your engine last ran a pass in, carrying `1.0h` forward.
  The *values* are the tutorial's; only the length of the sequence grows.

**Every number the tutorial quotes was computed as at 2026-06-30 12:00 UTC.**

"""Load the NFL's play-by-play era into a running uratori, one tenant.

The data is nflverse (https://github.com/nflverse/nflverse-data): the
community-maintained public record of NFL games, weekly player stat lines and
play-by-play. This script is a *host*: it downloads the seasons, shapes them
into plain records -- **every single play is a fact** -- teaches the engine
its settings (schema.json) and its world and numbers (definitions.fig, where
the fact declarations live), and pushes it all over the HTTP API into a
single tenant. The bucketing -- per season, per game number, per day of the
week, per franchise across relocations -- is the definitions' job, not a
partitioning of tenants. The loader's cuts are identity (which team-season
a record belongs to, in that season's own spelling), the accuracy gates
below, and two derivations the language deliberately does not own: the
weekday a kickoff falls on (there is no day-of-week truncation -- a range
over days can group by date, and day-of-week is a fact about the schedule,
not a window) and the game's ordinal within its team's season (a rank over
a population and ordering no single record carries). It computes no figure
of its own: every number the demo serves comes from a definition, with its
version and its evidence.

Only accurate play-by-play is imported, and the loader proves it per game:
a played game loads only if its plays rebuild its final score exactly --
both teams, to the point. A game that fails that reconciliation is excluded
whole (game, sides, stat lines and plays), and the loader says how many it
dropped, because a silent cap reads as "covered everything". Fixtures with
no result yet have nothing to be inaccurate about and load as fixtures --
that is what the upcoming page shows.

Batches are pushed with `defer` -- the engine writes and verifies them but
runs no pass, because a pass per batch would re-read buckets every earlier
batch already filled -- and the import closes with one full run that
computes everything. Until that run finishes, results honestly answer
never-computed.

Stdlib only, so it runs anywhere Python 3.12 does:

    python examples/nfl/load.py --base http://localhost:8080 \
        --seasons 1999-2026

The default is the whole play-by-play era, 1999 onwards: roughly 1.3
million plays in one tenant. A season whose games have no scores yet (next
season's schedule) loads as fixtures only -- the upcoming page fills, and
its team-season rows are not created at all until its football starts.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

Record = dict[str, Any]

HERE = Path(__file__).parent
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"

# Kickoff times in games.csv are US Eastern wall clock; the loader renders
# them as instants so `by day` has a real moment to file.
EASTERN = ZoneInfo("America/New_York")

# One franchise, several abbreviations: the relocations inside the nflverse
# era. The hop in the definitions (`through nfl_team.abbrs.abbr`) is what
# makes OAK and LV one subject; this map is what tells the loader they are.
FRANCHISE = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

# ISO weekday order, so the slate page can sort the week even though the
# engine sees the day names as words.
WEEKDAYS = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}

BATCH = 20000


# --------------------------------------------------------------- download --


def fetch(url: str, cache: Path) -> bytes:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        return cache.read_bytes()
    print(f"  downloading {url.rsplit('/', 1)[-1]} ...", flush=True)
    with urllib.request.urlopen(url) as response:
        body: bytes = response.read()
    cache.write_bytes(body)
    return body


def rows_of(blob: bytes) -> list[dict[str, str]]:
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    return list(csv.DictReader(io.TextIOWrapper(io.BytesIO(blob), encoding="utf-8")))


PBP_COLUMNS = (
    "game_id",
    "play_id",
    "desc",
    "posteam",
    "defteam",
    "play_type",
    "down",
    "yards_gained",
    "qtr",
    "sp",
    "time_of_day",
    "posteam_score",
    "defteam_score",
    "posteam_score_post",
    "defteam_score_post",
    "third_down_converted",
    "interception",
    "fumble_lost",
    "fumbled_1_team",
)


def pbp_rows(blob: bytes) -> list[dict[str, str]]:
    """Play-by-play trimmed to the columns the loader reads while parsing.

    A season is ~50,000 rows by ~370 columns; a full DictReader list is
    gigabytes across the era, and the loader reads eighteen of them.
    """
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    reader = csv.reader(io.TextIOWrapper(io.BytesIO(blob), encoding="utf-8"))
    header = next(reader)
    wanted = [(column, header.index(column)) for column in PBP_COLUMNS if column in header]
    return [
        {column: row[index] if index < len(row) else "" for column, index in wanted}
        for row in reader
        if row
    ]


# ------------------------------------------------------------------ shape --


def number(text: str) -> float | int | None:
    """CSV numerics arrive as text; an empty cell is an absence, not a zero."""
    if text in ("", "NA"):
        return None
    value = float(text)
    return int(value) if value == int(value) else value


def put(record: Record, field: str, text: str) -> None:
    value = number(text)
    if value is not None:
        record[field] = value


def kickoff_of(game: dict[str, str]) -> str:
    day = game["gameday"]
    # 1999 is the one season whose schedule mostly lacks a kickoff time; the
    # 1pm window is the league's default slot, and a day bucket needs *some*
    # instant. The day is real; the hour, for those games, is conventional.
    clock = game["gametime"] or "13:00"
    moment = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=EASTERN)
    return moment.isoformat()


def weekday_of(kickoff: str) -> str:
    """The day of the week the kickoff falls on, in the league's calendar --
    the instants are built in Eastern above, so this is Eastern too. A London
    morning game is still Sunday football to the league's own clock."""
    return datetime.fromisoformat(kickoff).strftime("%A").lower()


def franchise(abbr: str) -> str:
    return FRANCHISE.get(abbr, abbr)


def team_facts(teams_csv: list[dict[str, str]], season_abbrs: set[str]) -> dict[str, Record]:
    """One record per franchise, carrying every abbreviation the franchise
    has answered to -- the identity the `through` hop resolves."""
    by_abbr = {row["team_abbr"]: row for row in teams_csv}
    facts: dict[str, Record] = {}
    for abbr in sorted(season_abbrs):
        key = franchise(abbr)
        row = by_abbr.get(key) or by_abbr[abbr]
        facts[key] = {
            "name": row["team_name"],
            "abbrs": sorted(
                [{"abbr": a} for a in {key, *[o for o, n in FRANCHISE.items() if n == key]}],
                key=lambda entry: entry["abbr"],
            ),
            "conference": row["team_conf"],
            "division": row["team_division"],
            "url": f"https://www.nfl.com/teams/{row['team_name'].lower().replace(' ', '-')}/",
            "logo": row.get("team_logo_espn", ""),
            # The league keeps one clock, so every franchise carries it --
            # as a fact about the team rather than a dial over the board.
            "timezone": "America/New_York",
        }
    return facts


def season_fact(season: int) -> dict[str, Record]:
    return {str(season): {"name": str(season), "year": season}}


def team_season_facts(
    season: int, season_abbrs: set[str], teams_csv: list[dict[str, str]]
) -> dict[str, Record]:
    """One record per (team, season), keyed and named in the season's own
    spelling -- the 2003 entry is the Oakland Raiders, whoever holds the
    franchise now. The `team` field is that era spelling too; the franchise
    join happens in the definitions, through the abbrs list."""
    by_abbr = {row["team_abbr"]: row for row in teams_csv}
    facts: dict[str, Record] = {}
    for abbr in sorted(season_abbrs):
        row = by_abbr.get(abbr) or by_abbr[franchise(abbr)]
        facts[f"{season}-{abbr}"] = {
            "name": f"{season} {row['team_name']}",
            "team": abbr,
            "season": season,
        }
    return facts


def slot_facts(count: int) -> dict[str, Record]:
    """Game-number slots 1..count. Keys are the numbers' own spelling,
    because that is what a numeric `game_number` field buckets to."""
    return {str(n): {"name": f"Game {n}", "order": n} for n in range(1, count + 1)}


def weekday_facts() -> dict[str, Record]:
    return {
        day: {"name": day.capitalize(), "order": order} for day, order in WEEKDAYS.items()
    }


def game_facts(games: list[dict[str, str]]) -> tuple[dict[str, Record], dict[str, Record]]:
    """Games as fixtures-plus-results, and the two per-side outcome records a
    finished game earns. An unplayed game carries no score fields at all --
    an absence must stay an absence -- and `finished` says which it is,
    explicitly, because a 0 score is a measured nought the definitions must
    not have to distinguish from a missing one.

    Each side carries its team-season key and, for a finished regular-season
    game, its game number -- the Nth game that team played that year, counted
    off the schedule in kickoff order. The count is taken before the accuracy
    gate runs, deliberately: a game the gate later excludes still happened,
    and the games around it keep their true ordinals."""
    game_records: dict[str, Record] = {}
    side_records: dict[str, Record] = {}
    latest: dict[str, str] = {}
    game_numbers: dict[str, int] = {}
    for game in sorted(games, key=kickoff_of):
        kickoff = kickoff_of(game)
        season = int(game["season"])
        away, home = game["away_team"], game["home_team"]
        label = f"Week {game['week']}: {away} @ {home}"
        record: Record = {
            "name": label,
            "season": season,
            "week": int(game["week"]),
            "weekday": weekday_of(kickoff),
            "game_type": game["game_type"],
            "kickoff": kickoff,
            "away_team": away,
            "home_team": home,
            "venue": {
                key: value
                for key, value in {
                    "stadium": game["stadium"],
                    "roof": game.get("roof", ""),
                    "surface": game.get("surface", ""),
                }.items()
                if value
            },
            "went_overtime": game["overtime"] == "1",
            "division_game": game["div_game"] == "1",
        }
        if game["espn"]:
            record["url"] = f"https://www.espn.com/nfl/game/_/gameId/{game['espn']}"
        put(record, "away_score", game["away_score"])
        put(record, "home_score", game["home_score"])
        # An explicit finished flag, because the definitions cannot gate on
        # `home_score is set`: the language deliberately reads a numeric
        # nought as absent, and a shutout of the home team is finished
        # football, not an unplayed fixture. A boolean is present either way.
        record["finished"] = "home_score" in record and "away_score" in record
        # The combined score is derived from the two fields the accuracy
        # gate verifies, not read off the CSV's own `total` column: a game
        # with a blank total would land in every count and contribute to no
        # sum, understating the per-game averages with nothing to see.
        if record["finished"]:
            record["total"] = record["away_score"] + record["home_score"]
        game_records[game["game_id"]] = record

        if not record["finished"]:
            continue
        scores = {home: record["home_score"], away: record["away_score"]}
        for abbr, opponent in ((home, away), (away, home)):
            side: Record = {
                "name": f"{abbr} vs {opponent}, week {game['week']} of {season}",
                "team": abbr,
                "team_season": f"{season}-{abbr}",
                "opponent": opponent,
                "season": season,
                "week": int(game["week"]),
                "game_type": game["game_type"],
                "kickoff": kickoff,
                "points_for": scores[abbr],
                "points_against": scores[opponent],
                "won": scores[abbr] > scores[opponent],
                "tied": scores[abbr] == scores[opponent],
                "home": abbr == home,
            }
            if game["game_type"] == "REG":
                game_numbers[abbr] = game_numbers.get(abbr, 0) + 1
                side["game_number"] = game_numbers[abbr]
            if abbr in latest:
                side["previous_kickoff"] = latest[abbr]
            side_records[f"{game['game_id']}@{abbr}"] = side
        latest[home] = kickoff
        latest[away] = kickoff
    return game_records, side_records


def player_facts(
    stats: list[dict[str, str]], kickoffs: dict[str, str], season: int
) -> tuple[dict[str, Record], dict[str, Record]]:
    players: dict[str, Record] = {}
    lines: dict[str, Record] = {}
    for row in stats:
        game_id = row["game_id"]
        kickoff = kickoffs.get(game_id)
        if kickoff is None:
            continue
        pid = row["player_id"]
        # One record per player across the era: seasons load oldest first, so
        # the card shows the last listing -- a mid-career trade shows the
        # later team, and a career is one subject.
        players[pid] = {
            "name": row["player_display_name"],
            "position": row["position"],
            "team": row["team"],
            "url": row.get("headshot_url", ""),
            # A player's days are cut on their own record's calendar, because
            # the calendar is read off the subject and a player is the subject
            # here. Reading it off the team looked right and matched nothing:
            # the group's keys are player ids, so every lookup in the team
            # table missed and the figure served nothing at all.
            "timezone": "America/New_York",
        }
        line: Record = {
            "name": f"{row['player_display_name']}, week {row['week']} of {season}",
            "player_id": pid,
            "team": row["team"],
            "opponent": row["opponent_team"],
            "season": season,
            "week": int(row["week"]),
            "game_type": row["season_type"],
            "kickoff": kickoff,
        }
        for field in (
            "completions",
            "attempts",
            "passing_yards",
            "passing_tds",
            "passing_interceptions",
            "carries",
            "rushing_yards",
            "rushing_tds",
            "receptions",
            "targets",
            "receiving_yards",
            "receiving_tds",
        ):
            put(line, field, row.get(field, ""))
        # Scrimmage yards and touchdowns-accounted-for are the league's own
        # composite stats, and they must be record fields: a measure is
        # deliberately not arithmetic (one field per record), and the figures
        # that need these -- a per-day sum, the per-opponent split -- cannot
        # be composed from parts (a time-keyed figure cannot be combined, a
        # rollup totals at most one split figure). Where composition *is*
        # expressible, the definitions do it: see nfl_team.point_diff and
        # nfl_player.touchdowns. Note total_tds credits a thrown touchdown
        # to passer and catcher alike, as the league does; the definitions'
        # prose says so wherever the number is served.
        line["scrimmage_yards"] = (line.get("rushing_yards") or 0) + (
            line.get("receiving_yards") or 0
        )
        line["total_tds"] = (
            (line.get("passing_tds") or 0)
            + (line.get("rushing_tds") or 0)
            + (line.get("receiving_tds") or 0)
        )
        lines[f"{game_id}@{pid}"] = line
    return players, lines


SNAP_TYPES = frozenset({"pass", "run", "qb_kneel", "qb_spike"})
"""The play types where the offence actually snapped the ball and the play
counted. Kickoffs (where nflverse's `posteam` is the *receiving* team),
punts, kicks and clock rows all carry a `posteam`; a nullified `no_play`
was snapped and then wiped off the books. None of them is a snap run."""


def play_facts(
    pbp: list[dict[str, str]],
    game_types: dict[str, str],
    season: int,
    era_of: dict[str, str],
) -> tuple[dict[str, dict[str, Record]], dict[str, dict[str, int]]]:
    """Every single play, one fact each, grouped by game -- plus, per game,
    the final score those plays rebuild, which is what the reconciliation
    gate compares against the schedule.

    A play that moved the scoreboard carries `points` and `scored_by`
    (whoever the points went to, pick-sixes and safeties included), judged
    by the score delta rather than by parsing the text. A giveaway carries
    `lost_by` -- whoever actually lost the ball, which on a muffed punt is
    the returner, not the team whose punt it was. `offense_season` and
    `lost_by_season` name the team-season in the schedule's own spelling
    for that year (`era_of` maps either spelling of a franchise to it), so
    the per-season play figures and the per-side records agree on what a
    team-season is called. The wall-clock instant travels when the league
    recorded one; whether it can be *trusted* is checked per game against
    the scheduled kickoff, in `load_season`."""
    by_game: dict[str, dict[str, Record]] = {}
    rebuilt: dict[str, dict[str, int]] = {}

    def season_key(team: str) -> str | None:
        era = era_of.get(franchise(team))
        return f"{season}-{era}" if era is not None else None

    for row in pbp:
        game_id = row.get("game_id", "")
        play_id = row.get("play_id", "")
        if not game_id or not play_id:
            continue
        description = (row.get("desc") or "").strip()
        play: Record = {"name": description[:140] or f"play {play_id}", "season": season}
        if game_id in game_types:
            play["season_type"] = game_types[game_id]
        if row.get("posteam"):
            play["offense"] = row["posteam"]
            offense_season = season_key(row["posteam"])
            if offense_season is not None:
                play["offense_season"] = offense_season
        if row.get("play_type"):
            play["play_type"] = row["play_type"]
            if row["play_type"] in SNAP_TYPES:
                play["snap"] = True
        if row.get("time_of_day"):
            play["clock_time"] = row["time_of_day"]
        # True, false and unknown are three different answers: "1" and "0"
        # are the league's judgement either way, a blank is no judgement at
        # all, and a boolean false is *present* to the language's `is set`.
        if row.get("third_down_converted") == "1":
            play["third_down_converted"] = True
        elif row.get("third_down_converted") == "0":
            play["third_down_converted"] = False
        loser = ""
        if row.get("interception") == "1":
            loser = row.get("posteam", "")
        elif row.get("fumble_lost") == "1":
            loser = row.get("fumbled_1_team") or row.get("posteam", "")
        if loser:
            play["lost_by"] = loser
            lost_season = season_key(loser)
            if lost_season is not None:
                play["lost_by_season"] = lost_season
        put(play, "down", row.get("down", ""))
        put(play, "yards_gained", row.get("yards_gained", ""))
        put(play, "quarter", row.get("qtr", ""))

        if row.get("sp") == "1":
            before_pos = number(row.get("posteam_score", ""))
            before_def = number(row.get("defteam_score", ""))
            after_pos = number(row.get("posteam_score_post", ""))
            after_def = number(row.get("defteam_score_post", ""))
            # All four ends or nothing: a delta against a baseline coerced
            # to nought would fabricate points from an absence. A scoring
            # play whose ends are unreadable carries no points, and the
            # shortfall is exactly what the reconciliation gate then sees.
            if (
                before_pos is not None
                and before_def is not None
                and after_pos is not None
                and after_def is not None
            ):
                points = int((after_pos + after_def) - (before_pos + before_def))
                team = row["posteam"] if after_pos > before_pos else row["defteam"]
                if points > 0 and team:
                    play["points"] = points
                    play["scored_by"] = team
                    game_score = rebuilt.setdefault(game_id, {})
                    game_score[team] = game_score.get(team, 0) + points

        by_game.setdefault(game_id, {})[f"{game_id}@{play_id}"] = play
    return by_game, rebuilt


def credible_clock(kickoff: str, stamps: list[str]) -> bool:
    """Whether a game's play clocks can be trusted, judged against its own
    scheduled kickoff.

    nflverse's `time_of_day` is genuine UTC from 2005 onwards; 2003-2004
    stamp stadium-local wall time with a `Z`, and 2001-2002 carry a bare
    clock with no date at all. The points gate cannot see any of that, so
    the clocks get a gate of their own: the earliest play instant must sit
    within three hours of kickoff -- generous for a weather delay, well
    inside the four-hour minimum a mislabelled timezone produces."""
    try:
        earliest = min(
            datetime.fromisoformat(stamp.replace("Z", "+00:00")) for stamp in stamps
        )
        start = datetime.fromisoformat(kickoff)
    except ValueError:
        return False
    return abs((earliest - start).total_seconds()) <= 3 * 3600


def reconciled(
    game_records: dict[str, Record], rebuilt: dict[str, dict[str, int]]
) -> tuple[set[str], list[str]]:
    """Which finished games the play-by-play accounts for exactly.

    The gate: a played game is imported only if the points its plays claim
    add up to the final score on the schedule -- per team, matched through
    the franchise map, because old schedules say OAK where the play-by-play
    says LV. Anything else -- a missing game, a missing play, a score that
    does not balance -- excludes the game whole."""
    kept: set[str] = set()
    dropped: list[str] = []
    for game_id, record in game_records.items():
        if not record["finished"]:
            continue
        claimed: dict[str, int] = {}
        for team, points in rebuilt.get(game_id, {}).items():
            key = franchise(team)
            claimed[key] = claimed.get(key, 0) + points
        final = {
            franchise(record["home_team"]): record["home_score"],
            franchise(record["away_team"]): record["away_score"],
        }
        # A real shutout leaves the shut-out team absent from `claimed`;
        # a nought on the schedule is the same claim.
        if {t: p for t, p in final.items() if p != 0} == claimed:
            kept.add(game_id)
        else:
            dropped.append(game_id)
    return kept, sorted(dropped)


# ------------------------------------------------------------------- push --


class Client:
    def __init__(self, base: str, token: str | None):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: Record | None = None) -> Record:
        answer = self.attempt(method, path, body)
        assert answer is not None, "attempt only returns None for a tolerated status"
        return answer

    def attempt(
        self, method: str, path: str, body: Record | None = None, tolerate: int | None = None
    ) -> Record | None:
        """One JSON call; a refusal exits with the server's own words --
        except a status the caller declared it can handle, which returns
        None instead."""
        request = urllib.request.Request(self.base + path, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        payload = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(request, data=payload) as response:
                answer: Record = json.loads(response.read())
                return answer
        except urllib.error.HTTPError as refusal:
            if refusal.code == tolerate:
                return None
            detail = refusal.read().decode(errors="replace")
            sys.exit(f"{method} {path} -> {refusal.code}: {detail}")


KIND_ORDER = (
    "nfl_season",
    "nfl_weekday",
    "nfl_game_slot",
    "nfl_team",
    "nfl_team_season",
    "nfl_game",
    "nfl_team_game",
    "nfl_player",
    "nfl_stat_line",
    "nfl_play",
)


def teach(client: Client) -> Record:
    """Schema then definitions -- with the one migration the fixed order
    cannot make. A server taught before facts joined the language holds a
    kind-declaring schema and a source with no fact blocks; a settings-only
    schema is refused against that stored source (it names kinds nothing
    would declare). The engine's own repair is definitions-first: loading
    the fact-declaring source retires the stored kinds without blanking a
    single tenant, and the settings schema then lands clean. Only a schema
    refusal takes the detour, so a fresh server keeps the plain path."""
    schema_document = json.loads((HERE / "schema.json").read_text())
    source = {"source": (HERE / "definitions.fig").read_text()}
    if client.attempt("PUT", "/schema", schema_document, tolerate=422) is None:
        print("  stored world predates the fact declarations; teaching definitions first")
        library = client.call("PUT", "/definitions", source)
        client.call("PUT", "/schema", schema_document)
    else:
        library = client.call("PUT", "/definitions", source)
    return library


def push(
    client: Client,
    tenant: str,
    facts: dict[str, dict[str, Record]],
    label: str,
    defer: bool = True,
) -> int:
    """Deferred batches by default: the engine verifies and writes each one
    and runs no pass -- `close` below owes the one that computes. A weekly
    in-season update (`--update`) pushes without `defer` instead: one week
    of football is exactly the batch the ordinary warm pass is for."""
    pending: list[tuple[str, str, Record]] = [
        (kind, key, record)
        for kind in KIND_ORDER
        for key, record in facts.get(kind, {}).items()
    ]
    total_written = total_changed = 0
    for start in range(0, len(pending), BATCH):
        writes: dict[str, dict[str, Record]] = {}
        for kind, key, record in pending[start : start + BATCH]:
            writes.setdefault(kind, {})[key] = record
        body: Record = {"writes": writes}
        if defer:
            body["defer"] = True
        report = client.call("POST", f"/tenants/{tenant}/facts", body)
        if defer and report["changed"]:
            # An engine that predates `defer` ignores the unknown field and
            # runs a pass per batch -- correct answers, at a cost that turns
            # an era import into days. Movements on a deferred batch are
            # that engine announcing itself.
            sys.exit("this engine ignored `defer`; upgrade it before a bulk import")
        total_written += report["written"]
        total_changed += report["changed"]
    suffix = "" if defer else f", {total_changed} figure movements"
    print(f"  [{label}] pushed: {total_written} records landed{suffix}", flush=True)
    return total_written


def close(client: Client, tenant: str) -> None:
    """The one pass the deferred batches owe. Everything computes here, so
    over a full era this runs for a while -- the engine reindexes a couple
    of million records and recomputes every figure there is."""
    print("closing the import: one full run (this is the long part) ...", flush=True)
    started = time.monotonic()
    report = client.call("POST", f"/tenants/{tenant}/runs", {"full": True})
    minutes = (time.monotonic() - started) / 60
    print(
        f"computed: {report['changed']} figure movements across "
        f"{len(report['rebuilt'])} definitions in {minutes:.1f} minutes"
    )


# ------------------------------------------------------------------- main --


def load_season(
    client: Client,
    tenant: str,
    season: int,
    cache: Path,
    teams_csv: list[dict[str, str]],
    games_csv: list[dict[str, str]],
    defer: bool = True,
) -> None:
    print(f"season {season}")
    games = [row for row in games_csv if row["season"] == str(season)]
    if not games:
        sys.exit(f"season {season}: nflverse has no schedule for it")
    game_records, side_records = game_facts(games)
    finished = [gid for gid, record in game_records.items() if record["finished"]]

    abbrs = {g["home_team"] for g in games} | {g["away_team"] for g in games}
    era_of = {franchise(abbr): abbr for abbr in abbrs}
    if len(era_of) != len(abbrs):
        # Two spellings of one franchise inside a single season's schedule
        # would make which one wins the era map arbitrary -- and the plays
        # and sides would file the same team-season under two keys. No
        # nflverse season does this; if one ever does, stopping is the only
        # honest move.
        sys.exit(f"season {season}: the schedule spells one franchise two ways")

    by_game: dict[str, dict[str, Record]] = {}
    rebuilt: dict[str, dict[str, int]] = {}
    if finished:
        try:
            pbp = pbp_rows(
                fetch(
                    f"{NFLVERSE}/pbp/play_by_play_{season}.csv.gz",
                    cache / f"play_by_play_{season}.csv.gz",
                )
            )
        except urllib.error.HTTPError:
            print(f"  no play-by-play published for {season}")
        else:
            by_game, rebuilt = play_facts(
                pbp,
                {gid: str(r["game_type"]) for gid, r in game_records.items()},
                season,
                era_of,
            )

    # The accuracy gate: a played game is imported only when its plays
    # rebuild its final score. Fixtures pass by having nothing to check.
    kept, dropped = reconciled(game_records, rebuilt)
    if dropped:
        shown = ", ".join(dropped[:5]) + (" ..." if len(dropped) > 5 else "")
        print(
            f"  excluded {len(dropped)} of {len(finished)} played games -- "
            f"play-by-play does not rebuild the final score: {shown}"
        )
    admitted = kept | {gid for gid, record in game_records.items() if not record["finished"]}
    game_records = {gid: r for gid, r in game_records.items() if gid in admitted}
    side_records = {key: r for key, r in side_records.items() if key.split("@")[0] in kept}
    plays = {key: r for gid in kept for key, r in by_game.get(gid, {}).items()}

    orphans = len(set(by_game) - set(admitted) - {gid for gid in dropped})
    if orphans:
        print(f"  ignored play-by-play for {orphans} games the schedule does not list")

    # The clocks get their own per-game gate (see credible_clock): a game
    # whose stamps disagree with its scheduled kickoff keeps its plays and
    # loses their wall-clock instants, so the sub-day figures file only
    # clocks the data itself has vouched for.
    unclocked = 0
    for gid in kept:
        records = by_game.get(gid, {})
        stamps = [str(r["clock_time"]) for r in records.values() if "clock_time" in r]
        if stamps and not credible_clock(str(game_records[gid]["kickoff"]), stamps):
            for record in records.values():
                record.pop("clock_time", None)
            unclocked += 1
    if unclocked:
        print(
            f"  clocks unverifiable for {unclocked} games (they disagree with the "
            "scheduled kickoff); their plays load without wall-clock instants"
        )

    # Every other exclusion here is printed; a team-season key the era map
    # could not resolve must be too, or the per-season play figures narrow
    # with nothing to see while the franchise-level ones (which resolve
    # through the abbrs list) stay whole -- a disagreement nothing reports.
    unmapped = sum(
        1
        for record in plays.values()
        if ("offense" in record and "offense_season" not in record)
        or ("lost_by" in record and "lost_by_season" not in record)
    )
    if unmapped:
        print(
            f"  {unmapped} plays name a team the season's schedule does not spell; "
            "they load without a team-season key"
        )

    kickoffs = {gid: str(game_records[gid]["kickoff"]) for gid in kept}
    slots = max(
        (int(side["game_number"]) for side in side_records.values() if "game_number" in side),
        default=0,
    )
    facts: dict[str, dict[str, Record]] = {
        "nfl_season": season_fact(season),
        "nfl_weekday": weekday_facts(),
        "nfl_game_slot": slot_facts(slots),
        "nfl_team": team_facts(teams_csv, abbrs),
        # A season's team rows appear when its football does: a roster
        # record gets a measured nought for every count, and 32 rows of
        # played-0 won-0 for a season that has not kicked off would be
        # noughts about games that never happened. The season record
        # itself stays -- its 0 finished games is a claim about a loaded
        # schedule, and the fixtures fill the upcoming page meanwhile.
        "nfl_team_season": team_season_facts(season, abbrs, teams_csv) if kept else {},
        "nfl_game": game_records,
        "nfl_team_game": side_records,
        "nfl_play": plays,
    }

    if kept:
        try:
            stats = rows_of(
                fetch(
                    f"{NFLVERSE}/stats_player/stats_player_week_{season}.csv",
                    cache / f"stats_player_week_{season}.csv",
                )
            )
        except urllib.error.HTTPError:
            print(f"  no weekly player stats published for {season}; skipping stat lines")
        else:
            # The kickoffs map carries reconciled games only, so stat lines
            # from an excluded game are excluded with it.
            players, lines = player_facts(stats, kickoffs, season)
            facts["nfl_player"] = players
            facts["nfl_stat_line"] = lines

    for kind in KIND_ORDER:
        if kind in facts and kind not in ("nfl_weekday", "nfl_game_slot", "nfl_season"):
            print(f"  {kind}: {len(facts[kind])}")
    push(client, tenant, facts, str(season), defer=defer)


def seasons_of(text: str) -> list[int]:
    """"1999-2003,2016" -> [1999, 2000, 2001, 2002, 2003, 2016]."""
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            first, last = part.split("-", 1)
            if int(first) > int(last):
                sys.exit(f"--seasons range {part!r} runs backwards")
            out.extend(range(int(first), int(last) + 1))
        else:
            out.append(int(part))
    if not out:
        sys.exit("--seasons named no seasons")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--token", default=None)
    parser.add_argument("--tenant", default="nfl")
    parser.add_argument(
        "--seasons",
        default="1999-2026",
        help="comma-separated years and ranges; the default is the whole play-by-play era",
    )
    parser.add_argument("--cache", default=str(HERE / "data"))
    parser.add_argument(
        "--update",
        action="store_true",
        help="an in-season refresh: push without defer and skip the closing "
        "full run -- the ordinary warm pass is exactly right when the batch "
        "is one week of football",
    )
    arguments = parser.parse_args()

    client = Client(arguments.base, arguments.token)
    cache = Path(arguments.cache)

    health = client.call("GET", "/health")
    print(f"engine {health['version']} at {arguments.base}")

    library = teach(client)
    print(
        f"library loaded: {len(library['figures'])} figures, "
        f"{len(library['readings'])} readings, {len(library['projections'])} projections, "
        f"{len(library['summaries'])} summaries"
    )

    teams_csv = rows_of(
        fetch(f"{NFLVERSE}/teams/teams_colors_logos.csv", cache / "teams_colors_logos.csv")
    )
    games_csv = rows_of(fetch(f"{NFLVERSE}/schedules/games.csv", cache / "games.csv"))

    # Oldest first, whatever order the flag named: a player's one record
    # keeps the *latest* listing only because later seasons land later.
    for season in sorted(seasons_of(arguments.seasons)):
        load_season(
            client,
            arguments.tenant,
            season,
            cache,
            teams_csv,
            games_csv,
            defer=not arguments.update,
        )

    if not arguments.update:
        close(client, arguments.tenant)

    print("\nloaded. Try:")
    print(f"  {arguments.base}/ui/  (tenant \"{arguments.tenant}\")")
    print(f"  {arguments.base}/tenants/{arguments.tenant}/results/nfl_team_season.best")
    print(f"  {arguments.base}/tenants/{arguments.tenant}/results/nfl_season.eras")
    print(f"  {arguments.base}/tenants/{arguments.tenant}/results/nfl_player.leaders")
    print(f"  {arguments.base}/tenants/{arguments.tenant}/results/nfl_team.scoring?trailing=9999")


if __name__ == "__main__":
    main()

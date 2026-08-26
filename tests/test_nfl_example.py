"""The NFL example is the language's showcase, and this pins it as one.

The example lives in examples/nfl and is loaded into a running engine by its
own script; nothing in the package imports it. Left untested, a language
change could silently strand it -- the first person to notice would be
somebody following the README against a fresh checkout. So: the schema must
build, the definitions must compile under it, and the compiled library must
keep exercising every kind of declaration the language has -- an example
that quietly stopped demonstrating readings would still compile, and this is
what fails instead.

The loader's shaping functions are tested here too, on fixtures, because
their load-bearing behaviours are requirements rather than conveniences: an
unplayed game must carry no score fields and `finished: false`, a shutout
must stay a finished game, and a relocated franchise must land under one
key with every abbreviation it has answered to (the `through` hop is only
honest if the loader is).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from uratori import compile_source
from uratori.server.contract import SchemaIn

EXAMPLE = Path(__file__).parent.parent / "examples" / "nfl"


def _load_module():
    spec = importlib.util.spec_from_file_location("nfl_load", EXAMPLE / "load.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nfl_load"] = module
    spec.loader.exec_module(module)
    return module


def _library():
    schema = SchemaIn(**json.loads((EXAMPLE / "schema.json").read_text())).build()
    return compile_source((EXAMPLE / "definitions.fig").read_text(), schema)


def test_the_example_compiles_under_its_own_schema() -> None:
    """The README's first step is PUT /schema then PUT /definitions; if this
    fails, so does every fresh install following it."""
    _library()


def test_the_example_exercises_every_declaration_kind() -> None:
    """The example exists to show the language off, so a refactor that drops
    the last reading (or projection, or summary) has broken it even though
    it still compiles."""
    library = _library()
    assert library.figures, "no figures left in the showcase"
    assert library.readings, "no readings left in the showcase"
    assert library.projections, "no projections left in the showcase"
    assert library.summaries, "no summaries left in the showcase"
    assert library.indexes, "no groups or filters left in the showcase"
    assert library.measures, "no measures left in the showcase"


def test_an_unplayed_game_carries_no_score_fields() -> None:
    """`filter nfl_game.finished where finished == true` is the definition of
    finished, and the score fields must stay absent too: a loader that wrote
    0-0 for next season's fixtures would put every future game on the
    scoreboard as a tie."""
    module = _load_module()
    fixture = {
        "game_id": "2026_01_DAL_PHI",
        "season": "2026",
        "week": "1",
        "game_type": "REG",
        "gameday": "2026-09-10",
        "gametime": "20:20",
        "away_team": "DAL",
        "home_team": "PHI",
        "away_score": "",
        "home_score": "",
        "total": "",
        "overtime": "",
        "div_game": "1",
        "stadium": "Lincoln Financial Field",
        "espn": "",
    }
    games, sides = module.game_facts([fixture])
    record = games["2026_01_DAL_PHI"]
    assert "home_score" not in record and "away_score" not in record
    assert record["finished"] is False
    assert sides == {}, "an unplayed game must not produce outcome records"


def test_a_shutout_of_the_home_team_is_still_a_finished_game() -> None:
    """The language reads a numeric nought as absent, so gating finished on
    `home_score is set` would drop every home shutout -- BUF 16 @ NE 0 --
    from the scoreboard, the summary and the bracket at once, while the
    upcoming page's gate still read it as played: a real game on no page at
    all. The explicit flag is what prevents that, and the nought itself must
    keep travelling as a real score."""
    module = _load_module()
    games, sides = module.game_facts(
        [_game("2016_04_BUF_NE", "2016-10-02", "BUF", "NE", "16", "0")]
    )
    record = games["2016_04_BUF_NE"]
    assert record["finished"] is True
    assert record["home_score"] == 0, "a shutout is a measured nought, not an absence"
    assert sides["2016_04_BUF_NE@NE"]["won"] is False
    assert sides["2016_04_BUF_NE@BUF"]["points_against"] == 0


def test_a_finished_game_produces_two_sides_and_rest_gaps() -> None:
    """One outcome record per team per game, and the second game a team
    plays knows when its first was -- the duration measure reads exactly
    that gap. The first game of the season has no previous kickoff, and an
    absent gap must stay absent rather than read as a rest of nought."""
    module = _load_module()

    def game(gid: str, day: str, away: str, home: str, away_score: str, home_score: str):
        return {
            "game_id": gid,
            "season": "2025",
            "week": gid.split("_")[1].lstrip("0"),
            "game_type": "REG",
            "gameday": day,
            "gametime": "13:00",
            "away_team": away,
            "home_team": home,
            "away_score": away_score,
            "home_score": home_score,
            "total": str(int(away_score) + int(home_score)),
            "overtime": "0",
            "div_game": "0",
            "stadium": "Somewhere Field",
            "espn": "",
        }

    _, sides = module.game_facts(
        [
            game("2025_01_KC_LAC", "2025-09-05", "KC", "LAC", "21", "27"),
            game("2025_02_LAC_DEN", "2025-09-14", "LAC", "DEN", "10", "13"),
        ]
    )
    opener = sides["2025_01_KC_LAC@LAC"]
    assert opener["won"] is True and opener["points_for"] == 27
    assert "previous_kickoff" not in opener
    second = sides["2025_02_LAC_DEN@LAC"]
    assert second["won"] is False
    assert second["previous_kickoff"] == opener["kickoff"]


def test_a_relocated_franchise_is_one_subject() -> None:
    """OAK and LV are the same team; the loader files both abbreviations on
    one record so the `through nfl_team.abbrs.abbr` hop resolves either to
    the same subject."""
    module = _load_module()
    teams_csv = [
        {
            "team_abbr": "OAK",
            "team_name": "Oakland Raiders",
            "team_conf": "AFC",
            "team_division": "AFC West",
            "team_logo_espn": "",
        },
        {
            "team_abbr": "LV",
            "team_name": "Las Vegas Raiders",
            "team_conf": "AFC",
            "team_division": "AFC West",
            "team_logo_espn": "",
        },
    ]
    facts = module.team_facts(teams_csv, {"OAK"})
    assert set(facts) == {"LV"}, "the franchise key is the current abbreviation"
    record = facts["LV"]
    assert record["name"] == "Oakland Raiders", "named as the loaded season knew it"
    assert {entry["abbr"] for entry in record["abbrs"]} == {"OAK", "LV"}


def test_every_play_is_a_fact_and_scoring_follows_the_scoreboard() -> None:
    """Every play lands as a record; the ones that moved a scoreboard carry
    `points` and `scored_by`, judged by the score delta rather than by
    parsing the text -- a safety credits the defence -- and a play that
    moved nothing carries neither, so it cannot file into a scoring
    bucket."""
    module = _load_module()

    by_game, rebuilt = module.play_facts(
        [
            _pbp_row("55"),
            _pbp_row("56", sp="1", posteam_score_post="0", defteam_score_post="5"),
            _pbp_row("57"),  # an ordinary snap: still a fact
            _pbp_row("58", time_of_day=""),  # no instant: a fact with no clock
        ]
    )
    plays = by_game["2025_01_ARI_NO"]
    assert len(plays) == 4, "every single play is a fact"
    ordinary = plays["2025_01_ARI_NO@57"]
    assert "points" not in ordinary and "scored_by" not in ordinary
    assert ordinary["offense"] == "NO" and ordinary["down"] == 3
    touchdown = plays["2025_01_ARI_NO@55"]
    assert touchdown["points"] == 6 and touchdown["scored_by"] == "NO"
    safety = plays["2025_01_ARI_NO@56"]
    assert safety["points"] == 2 and safety["scored_by"] == "ARI"
    assert "clock_time" not in plays["2025_01_ARI_NO@58"]
    assert rebuilt == {"2025_01_ARI_NO": {"NO": 6, "ARI": 2}}


# ---------------------------------------------------------------- fixtures --


def _game(gid: str, day: str, away: str, home: str, away_score: str, home_score: str, **extra: str):
    row = {
        "game_id": gid,
        "season": "2025",
        "week": gid.split("_")[1].lstrip("0"),
        "game_type": "REG",
        "gameday": day,
        "gametime": "13:00",
        "away_team": away,
        "home_team": home,
        "away_score": away_score,
        "home_score": home_score,
        "total": str(int(away_score) + int(home_score)) if away_score else "",
        "overtime": "0",
        "div_game": "0",
        "stadium": "Somewhere Field",
        "espn": "401001",
    }
    row.update(extra)
    return row


def _stat_row(**extra: str):
    row = {
        "player_id": "00-0000001",
        "player_display_name": "Test Player",
        "position": "RB",
        "team": "LAC",
        "opponent_team": "KC",
        "season": "2025",
        "week": "1",
        "season_type": "REG",
        "game_id": "2025_01_KC_LAC",
        "headshot_url": "",
        "completions": "",
        "attempts": "",
        "passing_yards": "",
        "passing_tds": "",
        "passing_interceptions": "",
        "carries": "12",
        "rushing_yards": "80",
        "rushing_tds": "1",
        "receptions": "3",
        "targets": "4",
        "receiving_yards": "25",
        "receiving_tds": "",
    }
    row.update(extra)
    return row


TEAMS_CSV = [
    {
        "team_abbr": abbr,
        "team_name": name,
        "team_conf": "AFC",
        "team_division": "AFC West",
        "team_logo_espn": "",
    }
    for abbr, name in (
        ("KC", "Kansas City Chiefs"),
        ("LAC", "Los Angeles Chargers"),
        ("DEN", "Denver Broncos"),
    )
]


# ------------------------------------------------- loader <-> definitions --


def test_every_field_the_definitions_read_exists_on_a_loader_record() -> None:
    """The schema declares kinds, not fields, so the compile cannot catch the
    loader and the definitions drifting apart -- rename `points_for` on
    either side and every team figure reads a silent nought. This is the
    suite's `SPECIMENS` pattern with the loader's own outputs as the
    specimens: every group and filter field, every `through` path and every
    measure path must resolve on a record the loader actually builds."""
    from uratori.engine.buckets import read_path
    from uratori.lang.check import _index_fields

    module = _load_module()
    library = _library()

    games, sides = module.game_facts(
        [
            _game("2025_01_KC_LAC", "2025-09-05", "KC", "LAC", "21", "27"),
            _game("2025_02_LAC_DEN", "2025-09-14", "LAC", "DEN", "10", "13"),
        ]
    )
    kickoffs = {gid: record["kickoff"] for gid, record in games.items()}
    # A line that has thrown as well as run and caught, so every measured
    # stat field exists on the specimen -- absent stats stay absent.
    players, lines = module.player_facts(
        [_stat_row(completions="20", attempts="30", passing_yards="200", passing_tds="2")],
        kickoffs,
    )
    # One snap carrying everything at once -- a converted third down that
    # scored and was somehow also fumbled away -- so every path the
    # definitions read resolves on it.
    by_game, _ = module.play_facts(
        [_pbp_row("55", down="3", third_down_converted="1", fumble_lost="1")]
    )
    plays = by_game["2025_01_ARI_NO"]
    specimens = {
        "nfl_team": next(iter(module.team_facts(TEAMS_CSV, {"KC"}).values())),
        "nfl_game": games["2025_01_KC_LAC"],
        # The *second* side a team plays: an opener has no previous_kickoff
        # by design, and the rest measure's paths must resolve somewhere.
        "nfl_team_game": sides["2025_02_LAC_DEN@LAC"],
        "nfl_player": next(iter(players.values())),
        "nfl_stat_line": next(iter(lines.values())),
        "nfl_play": next(iter(plays.values())),
    }

    for name, index in library.indexes.items():
        specimen = specimens[index.kind]
        for part in _index_fields(index.spec):
            assert read_path(specimen, part.field), (
                f"{name} reads {part.field}, which no loaded {index.kind} record carries"
            )
            if part.through is not None:
                owner = specimens[part.through.kind]
                assert read_path(owner, part.through.path), (
                    f"{name} resolves through {part.through.path}, which no loaded "
                    f"{part.through.kind} record carries"
                )
    for name, measure in library.measures.items():
        specimen = specimens[measure.kind]
        for path in (measure.field_path, measure.moment, measure.earlier, measure.later):
            if path is not None:
                assert read_path(specimen, path), (
                    f"{name} reads {path}, which no loaded {measure.kind} record carries"
                )


def test_the_loader_pushes_every_kind_the_schema_declares() -> None:
    """A kind the push order misses is a kind that silently never loads --
    the loader would still print its build counts and exit 0."""
    import inspect

    module = _load_module()
    schema = json.loads((EXAMPLE / "schema.json").read_text())
    pushed = {
        kind for kind in schema["kinds"] if f'"{kind}"' in inspect.getsource(module.push)
    }
    assert pushed == set(schema["kinds"])


def test_push_delivers_every_record_exactly_once_across_batches() -> None:
    """The batch slicing must neither drop nor duplicate the tail, whatever
    the batch size -- and kinds must arrive in dependency order, teams before
    the records that resolve through them."""
    module = _load_module()

    class FakeClient:
        def __init__(self) -> None:
            self.batches: list[dict] = []

        def call(self, method: str, path: str, body: dict | None = None) -> dict:
            assert method == "POST" and body is not None
            self.batches.append(body["writes"])
            return {"written": 0, "changed": 0}

    original = module.BATCH
    module.BATCH = 3
    try:
        facts = {
            "nfl_stat_line": {f"line-{n}": {"n": n} for n in range(4)},
            "nfl_team": {f"team-{n}": {"n": n} for n in range(2)},
            "nfl_game": {f"game-{n}": {"n": n} for n in range(2)},
        }
        client = FakeClient()
        module.push(client, "t", facts)
    finally:
        module.BATCH = original

    delivered: list[tuple[str, str]] = [
        (kind, key) for batch in client.batches for kind, keys in batch.items() for key in keys
    ]
    assert len(delivered) == len(set(delivered)) == 8, "a record was dropped or duplicated"
    kind_order = [kind for kind, _ in delivered]
    assert kind_order.index("nfl_team") == 0
    assert kind_order == sorted(
        kind_order, key=["nfl_team", "nfl_game", "nfl_stat_line"].index
    ), "kinds must arrive teams-first, in dependency order"
    assert max(sum(len(keys) for keys in batch.values()) for batch in client.batches) <= 3


# ----------------------------------------------------------- shaping rules --


def test_stat_line_composites_are_the_sum_of_their_parts() -> None:
    """Scrimmage yards are rushing plus receiving; touchdowns-accounted-for
    are thrown plus rushed plus caught -- the league's own composites,
    carried as record fields because a measure reads one field and the
    figures that need these cannot be composed from parts. A line with none
    of the underlying stats composes to a measured nought: the row exists
    because the player took the field that week."""
    module = _load_module()
    kickoffs = {"2025_01_KC_LAC": "2025-09-05T13:00:00-04:00"}

    _, lines = module.player_facts(
        [_stat_row(passing_tds="2", receiving_tds="1")], kickoffs
    )
    line = lines["2025_01_KC_LAC@00-0000001"]
    assert line["scrimmage_yards"] == 80 + 25
    assert line["total_tds"] == 2 + 1 + 1

    _, empty = module.player_facts(
        [
            _stat_row(
                carries="", rushing_yards="", rushing_tds="",
                receptions="", targets="", receiving_yards="",
            )
        ],
        kickoffs,
    )
    quiet = empty["2025_01_KC_LAC@00-0000001"]
    assert quiet["scrimmage_yards"] == 0 and quiet["total_tds"] == 0
    assert "rushing_yards" not in quiet, "an absent stat stays absent"


def test_kickoffs_are_eastern_wall_clock_rendered_with_the_right_offset() -> None:
    """games.csv times are US Eastern wall clock. The instant must be that
    wall time *placed in* the Eastern calendar -- never the machine's clock
    shifted -- or every `by day in tenant.timezone` bucket in the demo moves.
    Both sides of the DST boundary, and the missing-gametime default."""
    module = _load_module()
    november = module.kickoff_of(_game("2025_10_A_B", "2025-11-16", "A", "B", "0", "3"))
    assert november == "2025-11-16T13:00:00-05:00"
    september = module.kickoff_of(_game("2025_01_A_B", "2025-09-14", "A", "B", "0", "3"))
    assert september == "2025-09-14T13:00:00-04:00"
    unstated = module.kickoff_of(
        _game("2025_01_C_D", "2025-09-14", "C", "D", "0", "3", gametime="")
    )
    assert unstated == "2025-09-14T13:00:00-04:00", "no stated time means the 1pm window"


def test_previous_kickoff_does_not_depend_on_input_order() -> None:
    """games.csv carries no ordering promise; the rest measure depends on the
    loader sequencing each team's games itself."""
    module = _load_module()
    first = _game("2025_01_KC_LAC", "2025-09-05", "KC", "LAC", "21", "27")
    second = _game("2025_02_LAC_DEN", "2025-09-14", "LAC", "DEN", "10", "13")
    _, sides = module.game_facts([second, first])
    assert "previous_kickoff" not in sides["2025_01_KC_LAC@LAC"]
    assert (
        sides["2025_02_LAC_DEN@LAC"]["previous_kickoff"]
        == sides["2025_01_KC_LAC@LAC"]["kickoff"]
    )


def _pbp_row(play_id: str, **overrides: str):
    row = {
        "game_id": "2025_01_ARI_NO",
        "play_id": play_id,
        "sp": "1" if play_id in ("55", "56") else "0",
        "time_of_day": "2025-09-07T17:39:15Z",
        "posteam": "NO",
        "defteam": "ARI",
        "play_type": "run",
        "down": "3",
        "yards_gained": "18",
        "posteam_score": "0",
        "defteam_score": "3",
        "posteam_score_post": "6" if play_id == "55" else "0",
        "defteam_score_post": "3" if play_id != "56" else "5",
        "qtr": "1",
        "third_down_converted": "0",
        "interception": "0",
        "fumble_lost": "0",
        "desc": "A.Kamara right guard for 18 yards, TOUCHDOWN.",
    }
    if play_id == "57":
        row["posteam_score_post"] = "0"
    row.update(overrides)
    return row


def test_a_play_the_league_never_flagged_as_scoring_earns_no_points() -> None:
    """`sp` is the league's own scoring-play marker. Without the guard, any
    row whose scoreboard columns happen to move -- a correction, a stray
    charting artefact -- would mint points onto the clock figures. The play
    itself still loads: it happened."""
    module = _load_module()
    by_game, rebuilt = module.play_facts([_pbp_row("55", sp="0")])
    play = by_game["2025_01_ARI_NO"]["2025_01_ARI_NO@55"]
    assert "points" not in play and "scored_by" not in play
    assert rebuilt == {}


def test_a_scoring_play_with_no_before_scores_earns_no_points() -> None:
    """A delta against a baseline coerced to nought fabricates points from
    an absence -- the exact thing rule 3 exists to prevent. The shortfall
    then fails the game's reconciliation, which is the gate doing its job."""
    module = _load_module()
    by_game, rebuilt = module.play_facts(
        [_pbp_row("55", posteam_score="", defteam_score="NA")]
    )
    assert "points" not in by_game["2025_01_ARI_NO"]["2025_01_ARI_NO@55"]
    assert rebuilt == {}


def test_the_gate_keeps_games_whose_plays_rebuild_the_final_score() -> None:
    """The accuracy rule, whole: a played game is imported only when its
    plays account for its final score exactly. A game the play-by-play
    cannot rebuild -- points missing, points invented, or no plays at all
    -- is excluded; a fixture with no result has nothing to check and
    stays. Old schedules spell relocated teams differently from the
    play-by-play, so the comparison runs through the franchise map."""
    module = _load_module()
    games, _ = module.game_facts(
        [
            _game("2016_01_ARI_OAK", "2016-09-11", "ARI", "OAK", "3", "6"),
            _game("2016_02_OAK_TEN", "2016-09-18", "OAK", "TEN", "10", "17"),
            _game("2016_03_DEN_OAK", "2016-09-25", "DEN", "OAK", "", ""),
        ]
    )
    rebuilt = {
        # Matches, with the modern abbreviation for the 2016 Raiders.
        "2016_01_ARI_OAK": {"LV": 6, "ARI": 3},
        # Accounts for only some of the points.
        "2016_02_OAK_TEN": {"TEN": 17, "LV": 3},
    }
    kept, dropped = module.reconciled(games, rebuilt)
    assert kept == {"2016_01_ARI_OAK"}
    assert dropped == ["2016_02_OAK_TEN"], "the fixture is not checkable, so not dropped"


def test_the_gate_reads_a_shutout_as_accounted_for() -> None:
    """A shut-out team scored on no play, so the plays legitimately say
    nothing about it -- silence and a nought on the schedule are the same
    claim, and demanding a zero entry would drop every accurate shutout."""
    module = _load_module()
    games, _ = module.game_facts(
        [_game("2016_04_BUF_NE", "2016-10-02", "BUF", "NE", "16", "0")]
    )
    kept, dropped = module.reconciled(games, {"2016_04_BUF_NE": {"BUF": 16}})
    assert kept == {"2016_04_BUF_NE"} and dropped == []


def test_the_showcase_keeps_the_constructs_the_readme_promises() -> None:
    """`README.md` tours specific constructs by name; the compile-level test
    above cannot notice one quietly refactored away. Source-text presence is
    deliberate here -- the claim is about what the showcase *shows*."""
    source = (EXAMPLE / "definitions.fig").read_text()
    for construct in (
        "through nfl_team.abbrs.abbr",  # the identity hop
        "by day in tenant.timezone",  # calendar bucketing
        "by 15 minutes in tenant.timezone",  # the sub-day grain
        "younger than form.recentDays",  # an age filter
        "= kickoff - previous_kickoff",  # a duration measure
        "= moment kickoff",  # a moment measure
        "across nfl_team",  # the second dimension
        "combine:",  # figures on figures
        "band:",  # a figure's thresholds
        "requires:",  # a reading's floor
        "series(slots) by hour",  # the grouped sub-day series
        "band high against pace.restDays",  # a reading's band
        "from nfl_game.finished & nfl_game.playoff",  # a set-expression population
        "omit when",  # the row-level gate
        "days from now to",  # the signed calendar span
        "summarise nfl_game.season over nfl_game.scoreboard",  # the one-row summary
    ):
        assert construct in source, f"the showcase no longer contains `{construct}`"

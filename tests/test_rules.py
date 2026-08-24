"""The rules the comments state, each with a test that catches its inversion.

**Every test here exists because a mutation survived the suite.** A review broke
each of these rules in turn and the whole build stayed green -- which means the
rule was written down, argued for at length, and enforced by nothing. A rule
with no test is a comment, and a comment is what the next person deletes when it
is inconvenient.

They are grouped by the file that states the rule, and each one is named for
what the *mistake* would have done rather than for the function it covers. A
test called `test_compare_works` teaches nobody why the branch is there.
"""

from __future__ import annotations

import pytest

from uratori.engine.buckets import (
    buckets_of,
    day_in,
    measure_of,
    part_of,
    read_number,
    read_path,
)
from uratori.engine.evaluate import Parts, Readers, evaluate, same_value
from uratori.engine.project import holds, ordered, summarise
from uratori.engine.read import Sample, level_of, sample_from_days, statistics_of, unmet_of
from uratori.lang.ast import Condition, Number, Part, SortDecl
from uratori.lang.plan import ProjectPlan
from uratori.lang.settings import fingerprint, seconds_per

from .world import DEFAULTS, compile_source

# --------------------------------------------------------------- buckets --

BASE = """
index work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
index work_issue.active where active == true
index work_issue.stuck where status_changed_at older than thresholds.longWipDays
index work_issue.fresh where status_changed_at younger than thresholds.longWipDays
index work_issue.by_day from (assignee_account_id through team_person.accounts.account_id, completed_at by day in tenant.timezone)

measure work_issue.estimate = estimate_seconds in effort
measure work_issue.moved = moment updated_at
measure work_issue.lead = completed_at - created_at
measure work_issue.waiting = now - created_at

figure team_person.count:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)
"""

LIB = compile_source(BASE)
NOW = 1_756_000_000_000.0  # 2025-08-24T02:26:40Z
DAY = 86_400_000.0


def _index(name: str):  # type: ignore[no-untyped-def]
    return LIB.indexes[name]


def _resolve(kind: str, path: str, value: str) -> list[str]:
    return [f"person-of-{value}"]


def test_older_than_and_younger_than_are_not_the_same_predicate() -> None:
    """Inverted, every stale merge request reads fresh and every fresh one reads
    stale -- and the board's whole "not moving" column means its opposite."""
    old = {"status_changed_at": "2025-01-01T00:00:00Z"}
    recent = {"status_changed_at": "2025-08-23T00:00:00Z"}

    assert buckets_of(_index("work_issue.stuck"), old, DEFAULTS, _resolve, NOW) == [""]
    assert buckets_of(_index("work_issue.stuck"), recent, DEFAULTS, _resolve, NOW) == []
    assert buckets_of(_index("work_issue.fresh"), recent, DEFAULTS, _resolve, NOW) == [""]
    assert buckets_of(_index("work_issue.fresh"), old, DEFAULTS, _resolve, NOW) == []


def test_a_record_with_no_readable_moment_is_in_no_age_bucket() -> None:
    """Not in the "old" one. An absent timestamp is not evidence of age."""
    assert buckets_of(_index("work_issue.stuck"), {}, DEFAULTS, _resolve, NOW) == []
    assert buckets_of(_index("work_issue.fresh"), {}, DEFAULTS, _resolve, NOW) == []


def test_a_day_belongs_to_the_tenants_calendar_and_not_to_utc() -> None:
    """Two figures on one card, one cut by UTC days and one by the tenant's,
    are two rows headed "30d" measuring two different months."""
    just_past_utc_midnight = 1_756_000_000_000.0  # 02:26 UTC, so still yesterday in LA
    assert day_in(just_past_utc_midnight, None) == "2025-08-24"
    assert day_in(just_past_utc_midnight, "America/Los_Angeles") == "2025-08-23"


def test_an_index_buckets_by_day_in_the_zone_its_definition_names() -> None:
    record = {
        "assignee_account_id": "a1",
        "completed_at": "2025-08-24T02:26:40Z",
    }
    la = {**DEFAULTS, "tenant": {**DEFAULTS["tenant"], "timezone": "America/Los_Angeles"}}
    utc = {**DEFAULTS, "tenant": {**DEFAULTS["tenant"], "timezone": "UTC"}}
    assert buckets_of(_index("work_issue.by_day"), record, la, _resolve, NOW) == [
        "person-of-a1@2025-08-23"
    ]
    assert buckets_of(_index("work_issue.by_day"), record, utc, _resolve, NOW) == [
        "person-of-a1@2025-08-24"
    ]


def test_a_key_part_containing_the_separator_is_refused_rather_than_encoded() -> None:
    """`@` joins the two halves of a composite subject. A value carrying one
    produces a subject that decomposes into the wrong pair, silently."""
    assert part_of("plain") == "plain"
    with pytest.raises(ValueError, match="may not contain"):
        part_of("a@b")


def test_several_numbers_at_one_path_is_not_a_quantity() -> None:
    """First-wins would answer with whichever the provider happened to order
    first, which is a number about one thing presented as a number about the
    record."""
    assert read_number({"n": 5}, "n") == 5.0
    assert read_number({"n": [5, 7]}, "n") is None
    assert read_number({}, "n") is None
    # A numeric *string* is refused on purpose: every timestamp is one.
    assert read_number({"n": "5"}, "n") is None


def test_a_finite_number_is_a_key_and_an_infinity_is_not() -> None:
    """They were not keys once, and an absent value satisfies `!=`, so a
    predicate over a numeric field matched every record rather than none."""
    assert read_path({"n": 3}, "n") == ["3"]
    assert read_path({"n": float("inf")}, "n") == []
    assert read_path({"n": float("nan")}, "n") == []


def test_a_clock_measure_refuses_to_invent_an_instant() -> None:
    """A per-record clock never produces a visibly wrong number -- it produces a
    queue whose oldest wait disagrees with itself by milliseconds, and an `at`
    on the response describing one row of it."""
    waiting = LIB.measures["work_issue.waiting"]
    record = {"created_at": "2025-08-01T00:00:00Z"}
    assert measure_of(waiting, record, NOW) is not None
    with pytest.raises(ValueError, match="no instant was supplied"):
        measure_of(waiting, record, None)


# -------------------------------------------------------------- evaluate --


def _readers(
    buckets: dict[tuple[str, str | None], frozenset[str]] | None = None,
    measures: dict[tuple[str, str], float | None] | None = None,
    moments: dict[tuple[str, str], float | None] | None = None,
    parts: dict[tuple[str, str], Parts] | None = None,
    settings: dict[str, float] | None = None,
) -> Readers:
    return Readers(
        buckets=lambda i, b: (buckets or {}).get((i, b), frozenset()),
        measures=lambda m, r: (measures or {}).get((m, r)),
        moments=lambda m, r: (moments or {}).get((m, r)),
        parts=lambda f, s: (parts or {}).get((f, s), Parts(values=(), subjects=())),
        settings=lambda p: (settings or {}).get(p, 0.0),
    )


LADDER = compile_source(
    BASE
    + """
figure team_person.band:
    \"\"\"d\"\"\"
    display "x"
    combine:
        n = team_person.count
    calculate:
        when n >= thresholds.wip.over then "over"
        when n >= thresholds.wip.warn then "warn"
        otherwise "ok"
"""
)


def test_a_ladder_stops_on_an_unknown_rather_than_falling_to_its_bottom_rung() -> None:
    """The most-repeated rule in this codebase, and nothing enforced it.

    `otherwise` is the bottom of the band. Falling through to it bands somebody
    the engine has never computed as **comfortable** -- a confident green against
    a number that does not exist, which is the exact failure everything here is
    arranged around avoiding.
    """
    plan = LADDER.figure("team_person.band")
    assert plan is not None
    settings = {"thresholds.wip.over": 5.0, "thresholds.wip.warn": 3.0}

    # Nothing stored for this subject: the rung's left side is unknown.
    absent = evaluate(plan, "nobody", _readers(settings=settings))
    assert absent.value is None, "an unmeasured subject was banded"

    known = evaluate(
        plan,
        "p1",
        _readers(parts={("team_person.count", "p1"): Parts((1.0,), ("p1",))}, settings=settings),
    )
    assert known.value == "ok", "the control: a measured subject still bands"


def test_max_propagates_an_absence_rather_than_letting_the_known_side_win() -> None:
    """Sound about data and wrong about this engine: a missing value means *not
    computed*, never "the subject has none of it" -- the backfill writes a real
    nought for anybody who genuinely has none. Letting the known side win reports
    a commitment too small, and every share divided by it reads high."""
    plan = compile_source(
        BASE
        + """
figure team_person.bigger:
    \"\"\"d\"\"\"
    display "x"
    unit count
    combine:
        a = team_person.count
        b = team_person.count
    calculate:
        max(a, b)
"""
    ).figure("team_person.bigger")
    assert plan is not None
    # `a` resolves, `b` does not.
    readers = _readers(parts={("team_person.count", "p1"): Parts((4.0,), ("p1",))})
    assert evaluate(plan, "p1", readers).value == 4.0
    assert evaluate(plan, "nobody", _readers()).value is None


EXTREME = compile_source(
    BASE
    + """
figure team_person.last_moved:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person}
    calculate:
        latest(work_issue.moved over mine)
"""
)


def test_the_latest_of_nothing_is_nothing_and_not_the_epoch() -> None:
    """Nought is a real instant. An epic created this morning would read as
    untouched for fifty-six years -- and so would one all of whose children carry
    a timestamp the engine cannot parse."""
    plan = EXTREME.figure("team_person.last_moved")
    assert plan is not None

    empty = evaluate(plan, "p1", _readers())
    assert empty.value is None

    unreadable = evaluate(
        plan,
        "p1",
        _readers(
            buckets={("work_issue.assigned_to", "p1"): frozenset({"CX-1"})},
            moments={},  # the record is in the set and its timestamp will not read
        ),
    )
    assert unreadable.value is None, "an unreadable timestamp was counted as 1970"

    real = evaluate(
        plan,
        "p1",
        _readers(
            buckets={("work_issue.assigned_to", "p1"): frozenset({"CX-1", "CX-2"})},
            moments={("work_issue.moved", "CX-1"): 100.0, ("work_issue.moved", "CX-2"): 900.0},
        ),
    )
    assert real.value == 900.0


def test_set_difference_removes_the_right_side() -> None:
    """Nothing in the suite evaluated a `-`, and the shipped
    `open_mrs_by_source` is built on one: the fresh-draft exclusion."""
    plan = compile_source(
        BASE
        + """
figure team_person.not_active:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person} - work_issue.active
    calculate:
        count(mine)
"""
    ).figure("team_person.not_active")
    assert plan is not None
    readers = _readers(
        buckets={
            ("work_issue.assigned_to", "p1"): frozenset({"a", "b", "c"}),
            ("work_issue.active", None): frozenset({"b"}),
        }
    )
    assert evaluate(plan, "p1", readers).value == 2.0


def test_the_evidence_of_a_list_lines_up_with_its_values() -> None:
    """Read back positionally, so a screen can say which record produced which
    number. Keep an unmeasurable record in the members and every duration after
    it is attributed to the wrong merge request."""
    plan = compile_source(
        BASE
        + """
figure team_person.leads:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person}
    calculate:
        list(work_issue.lead over mine)
"""
    ).figure("team_person.leads")
    assert plan is not None
    readers = _readers(
        buckets={("work_issue.assigned_to", "p1"): frozenset({"a", "b", "c"})},
        measures={("work_issue.lead", "a"): 10.0, ("work_issue.lead", "c"): 30.0},
    )
    result = evaluate(plan, "p1", readers)
    assert result.value == [10.0, 30.0]
    assert result.members == ("a", "c"), "the unmeasurable record stayed in the evidence"


def test_same_value_is_not_naive_equality() -> None:
    """`None == None` is true, so a naive test discards **every band change on
    the board** -- the movement from one unknown to another is not a movement,
    and the movement from warn to over is."""
    assert same_value(None, None) is True
    assert same_value("warn", "over") is False
    assert same_value(1.0, 1.0) is True
    assert same_value([1.0, 2.0], [1.0, 2.0]) is True
    assert same_value([1.0], [1.0, 2.0]) is False
    # A word and a number are not the same value even where Python would agree.
    assert same_value(0.0, False) is False


# ------------------------------------------------------------- projection --


def test_a_presence_test_answers_before_the_null_guard() -> None:
    """"Is there a value at all" is never itself unknown -- that is the whole
    point of the two tests. Moved after the guard they answer nothing, and a
    definition loses its only way to say *the engine has not scored this*."""
    nothing = Condition(left=Part(name="x"), op="nothing")
    something = Condition(left=Part(name="x"), op="something")

    assert holds(nothing, {"x": None}, DEFAULTS, 0.0) is True
    assert holds(something, {"x": None}, DEFAULTS, 0.0) is False
    assert holds(nothing, {"x": 3.0}, DEFAULTS, 0.0) is False
    assert holds(something, {"x": 3.0}, DEFAULTS, 0.0) is True

    # And an ordinary comparison against an unknown is still unknown.
    assert holds(Condition(left=Part(name="x"), op=">=", right=Number(value=1)), {"x": None}, DEFAULTS, 0.0) is None


def test_an_unsorted_row_goes_last_in_either_direction() -> None:
    """Written as a constant rank it sorts *first* descending, and with a limit
    it pushes real rows off the page -- a short list that reads as complete."""
    from uratori.engine.project import ProjectedRow

    def row(name: str, key: float | None) -> ProjectedRow:
        return ProjectedRow(id=name, values={}, units={}, flags=(), sort_key=key)

    rows = [row("a", 5.0), row("b", None), row("c", 90.0), row("d", None), row("e", 1.0)]
    for direction, expected in (
        ("descending", ["c", "a", "e"]),
        ("ascending", ["e", "a", "c"]),
    ):
        plan = ProjectPlan(
            name="work_issue.x",
            kind="work_issue",
            doc="",
            sort=SortDecl(name="k", direction=direction),  # type: ignore[arg-type]
        )
        got = [r.id for r in ordered(plan, rows)]
        assert got[:3] == expected, direction
        assert set(got[3:]) == {"b", "d"}, f"unsorted rows did not go last, {direction}"


# ---------------------------------------------------------------- reading --

READINGS = compile_source(
    BASE
    + """
figure team_person.per_day:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        list(work_issue.lead over mine)

figure team_person.volume:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        count(mine)

reading team_person.speed(range):
    \"\"\"d\"\"\"
    display "x"
    band low against flow.leadTimeDays
    depends:
        m = team_person.per_day in range
    requires:
        at least 3 values in m
    calculate:
        mean(m)
        worst(m)

reading team_person.shipped(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.volume in range
    calculate:
        sum(m)

reading team_person.pace(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.per_day in range
    calculate:
        mean(m)
"""
)


def test_a_low_band_puts_small_numbers_in_the_good_column() -> None:
    """Inverted, the fastest reviewer on the board is the one flagged red."""
    plan = READINGS.reading("team_person.speed")
    assert plan is not None
    good, poor = 7 * 86_400.0, 21 * 86_400.0
    assert level_of(plan, {"mean": good - 1}, DEFAULTS) == "ok"
    assert level_of(plan, {"mean": (good + poor) / 2}, DEFAULTS) == "warn"
    assert level_of(plan, {"mean": poor + 1}, DEFAULTS) == "over"


def test_a_band_over_an_absent_statistic_is_unknown_rather_than_good() -> None:
    plan = READINGS.reading("team_person.speed")
    assert plan is not None
    assert level_of(plan, {"mean": None}, DEFAULTS) == "unknown"


def test_the_serve_path_reads_both_stored_shapes() -> None:
    """A `list` figure keeps every value for a day and a `count` figure keeps one
    scalar. Dropping the scalar branch made every volume figure stored,
    versioned and unreadable -- with the checker and the reader agreeing, so no
    request ever came back wrong."""
    listed = sample_from_days([("2025-08-01", [10.0, 20.0])], "2025-08-01", "2025-08-01")
    assert listed.values == (10.0, 20.0)
    assert listed.days_covered == 1

    scalar = sample_from_days([("2025-08-01", 4.0)], "2025-08-01", "2025-08-01")
    assert scalar.values == (4.0,), "a count figure's stored scalar was dropped"
    assert scalar.days_covered == 1


def test_a_sum_of_nothing_is_nought_and_a_mean_of_nothing_is_unknown() -> None:
    """Deliberate asymmetry. A queue that took no tickets took no tickets; an
    average of no values is a claim nobody can make."""
    empty = Sample(values=(), per_day=(), days_covered=0, days_requested=7)

    speed = READINGS.reading("team_person.speed")
    shipped = READINGS.reading("team_person.shipped")
    assert speed is not None and shipped is not None

    assert statistics_of(speed, empty)["mean"] is None
    assert statistics_of(shipped, empty)["total"] == 0.0


def test_a_requirement_names_what_fell_short_rather_than_only_that_it_did() -> None:
    """A suppressed mean is otherwise an undifferentiated dash whose reason
    lives in a constant nobody can see from the board."""
    plan = READINGS.reading("team_person.speed")
    assert plan is not None
    short = Sample(values=(1.0,), per_day=(), days_covered=1, days_requested=7)
    unmet = unmet_of(plan, short)
    assert unmet and "3" in unmet[0] and "1" in unmet[0]

    enough = Sample(values=(1.0, 2.0, 3.0), per_day=(), days_covered=3, days_requested=7)
    assert unmet_of(plan, enough) == []


def test_one_value_renders_and_only_an_empty_window_is_withheld_with_a_reason() -> None:
    """The unwritten floor is one value: a small team's first merge renders
    rather than sitting behind a dash, and an empty window names what fell
    short instead of nulling silently."""
    plan = READINGS.reading("team_person.pace")
    assert plan is not None

    one = Sample(values=(5.0,), per_day=(), days_covered=1, days_requested=7)
    assert unmet_of(plan, one) == []
    assert statistics_of(plan, one)["mean"] == 5.0

    empty = Sample(values=(), per_day=(), days_covered=0, days_requested=7)
    unmet = unmet_of(plan, empty)
    # The full sentence, because "at least 1 value" is also a substring of the
    # plural and would pass against "at least 1 values".
    assert unmet == ["needs at least 1 value; there are 0"]


# --------------------------------------------------------------- settings --


def test_a_dial_set_to_nought_moves_the_fingerprint() -> None:
    """`or` treats nought, false and "" as unset. The fingerprint then does not
    move, the figure is never listed as pending, no rebuild happens, and the
    pointer keeps validating -- so the board bands against the old number for
    ever while the settings page shows the new one.

    `setting_value` already had this right, which is what made the disagreement
    invisible: evaluation used the nought and invalidation did not.
    """
    named = ["thresholds.wip.warn"]
    default = fingerprint({}, named)
    zeroed = fingerprint({"thresholds": {"wip": {"warn": 0}}}, named)
    assert default != zeroed, "a dial set to nought looked identical to unset"

    moved = fingerprint({"thresholds": {"wip": {"warn": 9}}}, named)
    assert moved not in (default, zeroed)


def test_the_threshold_units_are_the_ones_a_definition_can_write() -> None:
    """`work_hours` was declared, resolved to exactly 3,600 seconds, and was
    therefore a synonym for `hours` -- with a docstring claiming a working day
    mattered and nothing that made it."""
    assert seconds_per("minutes", DEFAULTS) == 60.0
    assert seconds_per("hours", DEFAULTS) == 3_600.0
    assert seconds_per("days", DEFAULTS) == 86_400.0
    with pytest.raises(ValueError, match="not a threshold unit"):
        seconds_per("work_hours", DEFAULTS)


# ---------------------------------------------------------------- checker --


def refuses(source: str, fragment: str) -> None:
    from uratori.lang.check import CheckError

    with pytest.raises(CheckError) as caught:
        compile_source(BASE + source)
    assert fragment in caught.value.message, caught.value.message


def test_a_set_may_not_mix_two_id_spaces() -> None:
    """Intersecting ids that mean different things yields the empty set, which
    is a figure reading nought for everybody rather than an error anybody sees.
    The rule was written on the plan and enforced nowhere."""
    refuses(
        """
index code_change.open where state == "open"

figure team_person.mixed:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.assigned_to:{team_person} & code_change.open
    calculate:
        count(m)
""",
        "combines indexes over",
    )


def test_a_measure_must_be_over_the_same_kind_as_the_set_it_is_applied_to() -> None:
    """Applied to other ids every lookup misses, every record is skipped, and
    the total answers nought for everybody. `test_fields.py` cannot catch it: it
    checks a measure's path against its *own* kind's specimen."""
    refuses(
        """
index code_review_request.asked_of from reviewer_account_id through team_person.accounts.account_id

figure team_person.wrong:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_review_request.asked_of:{team_person}
    calculate:
        sum(work_issue.estimate over m)
""",
        "Every lookup would miss",
    )


def test_a_band_with_no_on_needs_the_reading_to_calculate_a_mean() -> None:
    """The default is the mean, so a band over a reading that calculates only a
    worst case coloured nothing -- every row permanently grey, which reads as
    missing data rather than as a broken definition."""
    refuses(
        """
figure team_person.spans:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        list(work_issue.lead over mine)

reading team_person.worst_only(range):
    \"\"\"d\"\"\"
    display "x"
    band low against flow.leadTimeDays
    depends:
        m = team_person.spans in range
    calculate:
        worst(m)
""",
        "the mean, by default",
    )


def test_a_figure_takes_its_unit_from_the_binding_it_actually_reads() -> None:
    """Looping over every `combine` meant a figure that binds a count and an
    effort and reads the count came out as effort -- and 3 renders through the
    effort branch as "0.4h"."""
    lib = compile_source(
        BASE
        + """
figure team_person.effort:
    \"\"\"d\"\"\"
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person}
    calculate:
        sum(work_issue.estimate over mine)

figure team_person.reads_the_count:
    \"\"\"d\"\"\"
    display "x"
    combine:
        e = team_person.effort
        c = team_person.count
    calculate:
        c
"""
    )
    plan = lib.figure("team_person.reads_the_count")
    assert plan is not None
    assert plan.unit == "count", "the unit came from a binding this figure does not read"


# ---------------------------------------------------------------- summary --


def test_an_unknown_does_not_count_towards_a_summary() -> None:
    """A row the engine has not measured is not evidence that the thing being
    counted is true, so a count is a floor."""
    from uratori.engine.project import ProjectedRow
    from uratori.lang.plan import SummarisePlan

    plan = SummarisePlan(
        name="work_issue.roll",
        over="work_issue.item",
        doc="",
        counts=(("big", Condition(left=Part(name="n"), op=">=", right=Number(value=5))),),
    )
    rows = [
        ProjectedRow(id="a", values={"n": 9.0}, units={}, flags=(), sort_key=None),
        ProjectedRow(id="b", values={"n": None}, units={}, flags=(), sort_key=None),
        ProjectedRow(id="c", values={"n": 1.0}, units={}, flags=(), sort_key=None),
    ]
    assert summarise(plan, rows, DEFAULTS, 0.0).values["big"] == 1.0


def test_an_unknown_contribution_makes_a_whole_total_absent() -> None:
    """The opposite decision to a count, deliberately. A sum that skipped the
    unmeasured rows is arithmetic over a population nobody chose: it reads low,
    plausibly, and repairs itself later, which is the sawtooth signature."""
    from uratori.engine.project import ProjectedRow
    from uratori.lang.plan import SummarisePlan

    plan = SummarisePlan(
        name="work_issue.roll",
        over="work_issue.item",
        doc="",
        totals=(("all", "n", "count", None),),
    )
    complete = [
        ProjectedRow(id="a", values={"n": 2.0}, units={}, flags=(), sort_key=None),
        ProjectedRow(id="b", values={"n": 3.0}, units={}, flags=(), sort_key=None),
    ]
    assert summarise(plan, complete, DEFAULTS, 0.0).values["all"] == 5.0

    gap = [*complete, ProjectedRow(id="c", values={"n": None}, units={}, flags=(), sort_key=None)]
    assert summarise(plan, gap, DEFAULTS, 0.0).values["all"] is None

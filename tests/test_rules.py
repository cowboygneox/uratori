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
    day_range,
    end_of_day_ms,
    label_in,
    measure_of,
    part_of,
    read_number,
    read_path,
)
from uratori.engine.evaluate import Parts, Readers, evaluate, same_value
from uratori.engine.project import holds, ordered, summarise
from uratori.engine.read import (
    Sample,
    level_of,
    sample_from_buckets,
    sample_from_days,
    series_of,
    statistics_of,
    unmet_of,
)
from uratori.engine.serve import serve_reading
from uratori.lang.ast import Condition, Number, Part, SortDecl
from uratori.lang.plan import ProjectPlan
from uratori.lang.settings import fingerprint, seconds_per
from uratori.results import Ok
from uratori.store.base import Pointer
from uratori.store.memory import MemoryEngineStore

from .world import DEFAULTS, compile_source

# --------------------------------------------------------------- buckets --

BASE = """
group work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
filter work_issue.active where active == true
filter work_issue.stuck where status_changed_at older than thresholds.longWipDays
filter work_issue.fresh where status_changed_at younger than thresholds.longWipDays
group work_issue.by_day from (assignee_account_id through team_person.accounts.account_id, completed_at by day in tenant.timezone)
group work_issue.by_quarter from (assignee_account_id through team_person.accounts.account_id, completed_at by 15 minutes in tenant.timezone)
group work_issue.by_minute from (assignee_account_id through team_person.accounts.account_id, completed_at by minute in tenant.timezone)

measure work_issue.estimate = estimate_seconds in effort
measure work_issue.moved = moment updated_at
measure work_issue.lead = completed_at - created_at
measure work_issue.waiting = now - created_at

# d
figure team_person.count:
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


def test_a_sub_day_bucket_is_labelled_in_the_tenants_calendar() -> None:
    """The label is local time truncated to the grain, exactly as a day key is
    the local date -- so which quarter-hour an event belongs to is decided by
    the calendar the definition names, not by whichever zone the provider
    happened to write."""
    record = {"assignee_account_id": "a1", "completed_at": "2025-08-24T02:26:40Z"}
    la = {**DEFAULTS, "tenant": {**DEFAULTS["tenant"], "timezone": "America/Los_Angeles"}}
    utc = {**DEFAULTS, "tenant": {**DEFAULTS["tenant"], "timezone": "UTC"}}

    assert buckets_of(_index("work_issue.by_quarter"), record, la, _resolve, NOW) == [
        "person-of-a1@2025-08-23T19:15"
    ]
    assert buckets_of(_index("work_issue.by_quarter"), record, utc, _resolve, NOW) == [
        "person-of-a1@2025-08-24T02:15"
    ]
    assert buckets_of(_index("work_issue.by_minute"), record, la, _resolve, NOW) == [
        "person-of-a1@2025-08-23T19:26"
    ]


def test_the_repeated_hour_of_a_fall_back_merges_into_one_labelled_bucket() -> None:
    """When the clocks go back, 01:30 local happens twice. Both instants carry
    the same label, so their records share a bucket -- the honest answer to
    "what happened in the quarter-hour labelled 01:30", which occurred twice.
    The alternative, keying by UTC, would make every local midnight sit
    mid-bucket in most of the world's zones."""
    chicago = {**DEFAULTS, "tenant": {**DEFAULTS["tenant"], "timezone": "America/Chicago"}}
    cdt = {"assignee_account_id": "a1", "completed_at": "2025-11-02T06:30:00Z"}  # 01:30 CDT
    cst = {"assignee_account_id": "a1", "completed_at": "2025-11-02T07:30:00Z"}  # 01:30 CST

    first = buckets_of(_index("work_issue.by_quarter"), cdt, chicago, _resolve, NOW)
    second = buckets_of(_index("work_issue.by_quarter"), cst, chicago, _resolve, NOW)
    assert first == second == ["person-of-a1@2025-11-02T01:30"]


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
# d
figure team_person.band:
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
# d
figure team_person.bigger:
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
# d
figure team_person.last_moved:
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
# d
figure team_person.not_active:
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
# d
figure team_person.leads:
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
# d
figure team_person.per_day:
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
figure team_person.volume:
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        count(mine)

# d
reading team_person.speed(range):
    display "x"
    band low against flow.leadTimeDays
    depends:
        m = team_person.per_day in range
    requires:
        at least 3 values in m
    calculate:
        mean(m)
        worst(m)

# d
reading team_person.shipped(range):
    display "x"
    depends:
        m = team_person.volume in range
    calculate:
        sum(m)

# d
reading team_person.pace(range):
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
    empty = Sample(values=(), points=(), days_covered=0, days_requested=7)

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
    short = Sample(values=(1.0,), points=(), days_covered=1, days_requested=7)
    unmet = unmet_of(plan, short)
    assert unmet and "3" in unmet[0] and "1" in unmet[0]

    enough = Sample(values=(1.0, 2.0, 3.0), points=(), days_covered=3, days_requested=7)
    assert unmet_of(plan, enough) == []


def test_one_value_renders_and_only_an_empty_window_is_withheld_with_a_reason() -> None:
    """The unwritten floor is one value: a small team's first merge renders
    rather than sitting behind a dash, and an empty window names what fell
    short instead of nulling silently."""
    plan = READINGS.reading("team_person.pace")
    assert plan is not None

    one = Sample(values=(5.0,), points=(), days_covered=1, days_requested=7)
    assert unmet_of(plan, one) == []
    assert statistics_of(plan, one)["mean"] == 5.0

    empty = Sample(values=(), points=(), days_covered=0, days_requested=7)
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
filter code_change.open where state == "open"

# d
figure team_person.mixed:
    display "x"
    depends:
        m = work_issue.assigned_to:{team_person} & code_change.open
    calculate:
        count(m)
""",
        "combines record sets over",
    )


def test_a_measure_must_be_over_the_same_kind_as_the_set_it_is_applied_to() -> None:
    """Applied to other ids every lookup misses, every record is skipped, and
    the total answers nought for everybody. `test_fields.py` cannot catch it: it
    checks a measure's path against its *own* kind's specimen."""
    refuses(
        """
group code_review_request.asked_of from reviewer_account_id through team_person.accounts.account_id

# d
figure team_person.wrong:
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
# d
figure team_person.spans:
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
reading team_person.worst_only(range):
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
# d
figure team_person.effort:
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person}
    calculate:
        sum(work_issue.estimate over mine)

# d
figure team_person.reads_the_count:
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


# -------------------------------------------------------- sub-day grouping --

GRAINED = compile_source(
    BASE
    + """
# d
figure team_person.quarter_volume:
    display "x"
    depends:
        mine = work_issue.by_quarter:{team_person}
    calculate:
        count(mine)

# d
figure team_person.quarter_lead:
    display "x"
    depends:
        mine = work_issue.by_quarter:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
reading team_person.quarter_throughput(range):
    display "x"
    depends:
        m = team_person.quarter_volume in range
    calculate:
        sum(m)
        series(m) by hour

# d
reading team_person.quarter_pace(range):
    display "x"
    depends:
        m = team_person.quarter_lead in range
    requires:
        at least 3 values in m
    calculate:
        mean(m)
        series(m) by hour

# d
reading team_person.quarter_typical(range):
    display "x"
    depends:
        m = team_person.quarter_lead in range
    calculate:
        median(m)

# d
reading team_person.quarter_daily(range):
    display "x"
    depends:
        m = team_person.quarter_volume in range
    calculate:
        sum(m)
        series(m) by day

# d
reading team_person.quarter_fine(range):
    display "x"
    depends:
        m = team_person.quarter_volume in range
    calculate:
        sum(m)
        series(m) by 15 minutes
"""
)


def test_a_grouped_series_sums_counts_and_a_hole_stays_a_hole() -> None:
    """An hour nobody merged in is not an hour somebody merged nothing in --
    summing absences to nought would draw a floor that never happened, which is
    the same lie per hour that the day series already refuses per day."""
    buckets = [
        ("2025-08-01T00:15", 2.0),
        ("2025-08-01T00:30", 3.0),
        ("2025-08-01T01:00", 1.0),
    ]
    sample = sample_from_buckets(buckets, "2025-08-01", "2025-08-01", by="hour")
    series = series_of(sample)
    assert len(series) == 24
    assert series[0] == 5.0, "two quarters of one hour did not add up"
    assert series[1] == 1.0
    assert series[2:] == [None] * 22, "an empty hour was invented as a value"
    assert sample.values == (2.0, 3.0, 1.0)
    assert sample.days_covered == 1


def test_grouping_changes_the_series_and_never_the_statistics() -> None:
    """The scalar statistics run over the raw stored values whatever the series
    grain is. A mean of the group means would weight each hour equally instead
    of each record -- 37.5 here -- which is the mean-of-means trap wearing a new
    grain."""
    buckets = [
        ("2025-08-01T09:00", [10.0, 20.0]),
        ("2025-08-01T10:15", [60.0]),
    ]
    sample = sample_from_buckets(buckets, "2025-08-01", "2025-08-01", by="hour")
    plan = READINGS.reading("team_person.speed")
    assert plan is not None
    assert statistics_of(plan, sample)["mean"] == 30.0

    series = series_of(sample)
    assert series[9] == 15.0, "an hour's point is the mean of its own records"
    assert series[10] == 60.0


def test_quarters_grouped_by_day_agree_with_what_a_day_figure_would_have_stored() -> None:
    """One count, added up -- a day's point over quarter-hour buckets must equal
    the count a `by day` index would have written, or the two grains are two
    answers to one question."""
    buckets = [
        ("2025-08-01T09:15", 2.0),
        ("2025-08-01T23:45", 1.0),
        ("2025-08-03T00:00", 4.0),
    ]
    sample = sample_from_buckets(buckets, "2025-08-01", "2025-08-03", by="day")
    assert series_of(sample) == [3.0, None, 4.0]
    assert sample.days_covered == 2
    assert sample.days_requested == 3


def test_a_native_grain_series_lands_each_bucket_in_its_own_slot() -> None:
    """Equal is the boundary of "no finer than the store": a quarter-hour
    series over a quarter-hour figure is one point per bucket, at the position
    its label says -- 09:15 is slot 37 of a day's 96."""
    sample = sample_from_buckets(
        [("2025-08-01T09:15", 2.0)], "2025-08-01", "2025-08-01", by="15 minutes"
    )
    series = series_of(sample)
    assert len(series) == 96
    assert series[9 * 4 + 1] == 2.0
    assert sum(1 for point in series if point is not None) == 1


def test_a_sub_day_sample_with_no_series_still_carries_its_values() -> None:
    """A sub-day reading that declares no series has nothing to group, and the
    scalar statistics must not notice the difference: the values and the day
    coverage are the same either way."""
    buckets = [("2025-08-01T09:15", 2.0), ("2025-08-02T10:00", 3.0)]
    sample = sample_from_buckets(buckets, "2025-08-01", "2025-08-03", by=None)
    assert sample.values == (2.0, 3.0)
    assert sample.days_covered == 2
    assert sample.days_requested == 3
    assert series_of(sample) == []


def test_every_local_day_carries_the_same_labels_whatever_the_clocks_did() -> None:
    """The fall-back day's repeated labels merged at write time and the
    spring-forward day's missing hour is a run of holes, so a grouped series
    never gains or loses slots to DST -- a day is 24 hourly points, always."""
    fall_back = sample_from_buckets(
        [("2025-11-02T01:30", 5.0)], "2025-11-02", "2025-11-02", by="hour"
    )
    assert len(series_of(fall_back)) == 24
    assert series_of(fall_back)[1] == 5.0

    spring = sample_from_buckets([], "2026-03-08", "2026-03-08", by="hour")
    assert len(series_of(spring)) == 24


async def test_a_bucket_on_the_windows_final_day_is_served_not_lexicographically_lost() -> None:
    """Every label on the window's last day -- "2025-08-24T04:45" -- sorts
    *after* the bare day "2025-08-24", so day-string bounds would silently drop
    the current day from every sub-day window: a board that reports the team
    has shipped nothing all morning, every morning."""
    store = MemoryEngineStore()
    figure = GRAINED.figure("team_person.quarter_volume")
    reading = GRAINED.reading("team_person.quarter_throughput")
    assert figure is not None and reading is not None

    tenant = "t1"
    stamp = fingerprint(dict(DEFAULTS), list(figure.settings))
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint=stamp)
    )
    await store.set_buckets(tenant, "work_issue.by_quarter", "w1", ["p1@2025-08-24T04:45"])
    # The hand-written labels below are only honest if they are what the write
    # path produces -- pin one against `label_in` so the two cannot drift.
    assert label_in(1_756_035_900_000.0, "America/Los_Angeles", "15 minutes") == (
        "2025-08-24T04:45"  # 2025-08-24T11:45Z
    )
    for subject, value in (
        ("p1@2025-08-24T04:45", 2.0),  # the current LA day, mid-morning
        ("p1@2025-08-24T23:45", 4.0),  # the final quarter of the final day
        ("p1@2025-08-20T10:00", 3.0),  # mid-window
        ("p1@2025-08-18T00:00", 1.0),  # the first quarter of the first day
        ("p1@2025-08-17T23:45", 8.0),  # the quarter before the window opens
        ("p1@2025-08-10T09:00", 7.0),  # well before the window; must not leak in
    ):
        await store.save(tenant, figure.name, figure.version, subject, value, (), "P One")

    at = 1_756_036_800_000.0  # 2025-08-24T12:00Z, 05:00 in Los Angeles
    result = await serve_reading(store, GRAINED, tenant, reading, DEFAULTS, [7], at_ms=at)

    assert isinstance(result.state, Ok)
    assert len(result.subjects) == 1
    window = result.subjects[0].windows[0]
    assert (window.frm, window.to) == ("2025-08-18", "2025-08-24")
    assert window.total == 10.0, "a bucket at a window edge was dropped or leaked"
    assert window.series_by == "hour"
    assert window.series is not None and len(window.series) == 168
    assert window.series[6 * 24 + 4] == 2.0  # 04:00 on the final day
    assert window.series[6 * 24 + 23] == 4.0  # 23:00 on the final day
    assert window.series[2 * 24 + 10] == 3.0  # 10:00 on 2025-08-20
    assert window.series[0] == 1.0  # 00:00 on the first day
    assert sum(1 for point in window.series if point is not None) == 4


async def test_a_failed_floor_withholds_the_grouped_series_with_everything_else() -> None:
    """Every statistic is withheld together, and the series is a statistic: a
    sparkline drawn beside a suppressed mean would be the outlier's shape
    published under a heading that says the sample was too small to say
    anything -- and a grain shipped beside a null series would claim a shape
    that was never sent."""
    store = MemoryEngineStore()
    figure = GRAINED.figure("team_person.quarter_lead")
    reading = GRAINED.reading("team_person.quarter_pace")
    assert figure is not None and reading is not None

    tenant = "t1"
    stamp = fingerprint(dict(DEFAULTS), list(figure.settings))
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint=stamp)
    )
    await store.set_buckets(tenant, "work_issue.by_quarter", "w1", ["p1@2025-08-24T04:45"])
    await store.save(
        tenant, figure.name, figure.version, "p1@2025-08-24T04:45", [3600.0, 7200.0], (), "P One"
    )

    at = 1_756_036_800_000.0  # 2025-08-24T12:00Z
    result = await serve_reading(store, GRAINED, tenant, reading, DEFAULTS, [7], at_ms=at)

    assert isinstance(result.state, Ok)
    window = result.subjects[0].windows[0]
    assert window.unmet, "two values against a floor of three should have fallen short"
    assert window.mean is None
    assert window.series is None
    assert window.series_by is None, "a grain travelled beside a series that did not"


def test_an_anchor_resolves_to_its_days_last_moment_in_the_zone() -> None:
    """The instant an anchor date stands for is the final millisecond of that
    local day: one more and it is tomorrow, any less and part of the anchor
    day sits outside its own window. The DST rows are the reason the anchor
    is built from the next midnight rather than from a literal 23:59:59.999
    -- Santiago and Beirut both fall back *at* midnight, giving the anchor
    day a second 23:00-23:59 stretch, and the naive wall time names the
    first of them: an hour of the anchor day left outside its own window."""
    for zone, day, next_day in (
        ("Pacific/Kiritimati", "2026-06-30", "2026-07-01"),
        ("America/Santiago", "2026-04-04", "2026-04-05"),
        ("Asia/Beirut", "2026-10-24", "2026-10-25"),
    ):
        at = end_of_day_ms(day, zone)
        assert day_in(at, zone) == day, zone
        assert day_in(at + 1.0, zone) == next_day, zone


def test_a_window_crossing_fall_back_still_opens_the_full_span_back() -> None:
    """Los Angeles gains an hour on 2026-11-01, so stepping exact days back
    from that day's last moment crosses one midnight too few: 23:59:59.999
    lands on 00:59:59.999 of the day *after* the intended start, and a
    two-day window quietly serves one day while claiming two. A window is
    counted in local calendar days, and an anchored request always sits at a
    day's last millisecond -- exactly the hour this bites. (Spring-forward
    is the harmless direction: stepping back over it moves the wall clock
    further from midnight.)"""
    zone = "America/Los_Angeles"
    assert day_range(end_of_day_ms("2026-11-01", zone), zone, 2) == (
        "2026-10-31",
        "2026-11-01",
    )
    assert day_range(end_of_day_ms("2026-11-05", zone), zone, 7) == (
        "2026-10-30",
        "2026-11-05",
    )


def test_the_calendars_own_edges_answer_rather_than_overflow() -> None:
    """Pydantic accepts 9999-12-31 and 0001-01-01 as calendar days, so the
    engine has to answer about them: the far edge has no next midnight to
    build the anchor from, and enough trailing days from the near edge walk
    out of `date`'s range entirely. Both used to surface as a 500 -- an
    OverflowError worn as a server fault for a well-formed question whose
    honest answer is just an empty window."""
    at = end_of_day_ms("9999-12-31", "Pacific/Kiritimati")
    assert day_in(at, "Pacific/Kiritimati") == "9999-12-31"

    early = end_of_day_ms("0001-01-05", "UTC")
    assert day_range(early, "UTC", 30) == ("0001-01-01", "0001-01-05")


async def test_an_anchor_day_ends_the_windows_in_the_readings_own_zone() -> None:
    """`at_day` is an argument: it moves which stored days take part and
    touches nothing else. The windows must end on the anchor day *in the
    tenant's calendar*, a day stored after the anchor must not leak in, and
    the served `at` must be the anchor day's last moment rather than the wall
    clock -- otherwise the response claims a historical answer was computed
    now, and the provenance line from figure to screen breaks exactly where
    the feature exists to preserve it."""
    store = MemoryEngineStore()
    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None

    tenant = "t1"
    stamp = fingerprint(dict(DEFAULTS), list(figure.settings))
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint=stamp)
    )
    await store.set_buckets(tenant, "work_issue.by_day", "w1", ["p1@2025-08-24"])
    for subject, values in (
        ("p1@2025-08-18", [2.0]),  # the first day inside a 7-day window
        ("p1@2025-08-24", [4.0]),  # the anchor day itself
        ("p1@2025-08-25", [64.0]),  # the day after the anchor; must not leak in
        ("p1@2025-08-17", [128.0]),  # the day before the window opens
    ):
        await store.save(tenant, figure.name, figure.version, subject, values, (), "P One")

    result = await serve_reading(
        store, READINGS, tenant, reading, DEFAULTS, [7], at_day="2025-08-24"
    )

    assert isinstance(result.state, Ok)
    window = result.subjects[0].windows[0]
    assert (window.frm, window.to) == ("2025-08-18", "2025-08-24")
    assert window.mean == 3.0, "a day beyond the anchor leaked into the sample"
    assert window.days_covered == 2
    assert window.days_requested == 7
    # The served `at` is the anchor day's last moment in Los Angeles --
    # compared as an instant, not as a rendering, so a correct change of ISO
    # spelling stays green and a wall-clock `at` fails.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    served = datetime.fromisoformat(result.at).astimezone(ZoneInfo("America/Los_Angeles"))
    assert served.date().isoformat() == "2025-08-24"
    assert (served.hour, served.minute, served.second) == (23, 59, 59)


async def test_a_declared_floor_and_band_apply_unchanged_under_an_anchor() -> None:
    """The anchor decides which days are in the sample and *nothing* decides
    differently because of it: `team_person.speed` requires three values, so
    the anchor that catches two of the three stored days gets the floor's
    refusal, the anchor one day later gets the mean and its band word, and
    both answers cite the same version -- an anchor that moved the version
    would be an argument leaking into the calculation's identity."""
    store = MemoryEngineStore()
    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.speed")
    assert figure is not None and reading is not None

    tenant = "t1"
    stamp = fingerprint(dict(DEFAULTS), list(figure.settings))
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint=stamp)
    )
    await store.set_buckets(tenant, "work_issue.by_day", "w1", ["p1@2025-08-24"])
    for subject, values in (
        ("p1@2025-08-22", [86_400.0]),
        ("p1@2025-08-23", [172_800.0]),
        ("p1@2025-08-24", [259_200.0]),
    ):
        await store.save(tenant, figure.name, figure.version, subject, values, (), "P One")

    short = await serve_reading(
        store, READINGS, tenant, reading, DEFAULTS, [7], at_day="2025-08-23"
    )
    window = short.subjects[0].windows[0]
    assert window.unmet, "two values against a floor of three should have fallen short"
    assert window.mean is None
    assert window.level == "unknown"

    met = await serve_reading(
        store, READINGS, tenant, reading, DEFAULTS, [7], at_day="2025-08-24"
    )
    window = met.subjects[0].windows[0]
    assert window.unmet == []
    assert window.mean == 172_800.0
    # Two days under flow.leadTimeDays.good (seven), so the low band says ok.
    assert window.level == "ok"

    assert short.version == met.version, "an anchor moved a version hash"


async def test_a_sub_day_reading_under_an_anchor_keeps_its_final_quarters() -> None:
    """The anchored window's last day is a caller's day, not today, and the
    sub-day fetch bounds have to stretch to its final label all the same: a
    bucket at 23:45 of the anchor day belongs in, the first quarter of the
    next day stays out, and the grouped series still spans the whole window
    hour by hour."""
    store = MemoryEngineStore()
    figure = GRAINED.figure("team_person.quarter_volume")
    reading = GRAINED.reading("team_person.quarter_throughput")
    assert figure is not None and reading is not None

    tenant = "t1"
    stamp = fingerprint(dict(DEFAULTS), list(figure.settings))
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint=stamp)
    )
    await store.set_buckets(tenant, "work_issue.by_quarter", "w1", ["p1@2025-08-24T23:45"])
    for subject, value in (
        ("p1@2025-08-24T23:45", 4.0),  # the final quarter of the anchor day
        ("p1@2025-08-25T00:00", 9.0),  # the first quarter after it; must not leak
        ("p1@2025-08-20T10:00", 3.0),  # mid-window
    ):
        await store.save(tenant, figure.name, figure.version, subject, value, (), "P One")

    result = await serve_reading(
        store, GRAINED, tenant, reading, DEFAULTS, [7], at_day="2025-08-24"
    )

    assert isinstance(result.state, Ok)
    window = result.subjects[0].windows[0]
    assert (window.frm, window.to) == ("2025-08-18", "2025-08-24")
    assert window.total == 7.0, "a quarter at the anchored window's edge leaked or fell out"
    assert window.series is not None and len(window.series) == 168
    assert window.series[6 * 24 + 23] == 4.0  # 23:00 on the anchor day
    assert window.series[2 * 24 + 10] == 3.0  # 10:00 on 2025-08-20


# ------------------------------------------------------------ bucket spans --
#
# A window is a span of stored buckets counted back from the anchor: `31-60`
# is the thirty days before the trailing thirty, `1-4h` the last four hours.
# The spec is an argument -- it narrows which stored buckets take part and
# may never change the calculation -- so every declared rule (floor, band,
# series) applies per span unchanged.


async def _day_reading_store(figure, days):  # type: ignore[no-untyped-def]
    store = MemoryEngineStore()
    stamp = fingerprint(dict(DEFAULTS), list(figure.settings))
    await store.set_pointer(
        "t1", figure.name, Pointer(version=figure.version, settings_fingerprint=stamp)
    )
    await store.set_buckets("t1", "work_issue.by_day", "w1", ["p1@2025-08-24"])
    for subject, values in days:
        await store.save("t1", figure.name, figure.version, subject, values, (), "P One")
    return store


async def _quarter_store(figure, buckets):  # type: ignore[no-untyped-def]
    store = MemoryEngineStore()
    stamp = fingerprint(dict(DEFAULTS), list(figure.settings))
    await store.set_pointer(
        "t1", figure.name, Pointer(version=figure.version, settings_fingerprint=stamp)
    )
    await store.set_buckets("t1", "work_issue.by_quarter", "w1", ["p1@2025-08-24T04:45"])
    for subject, value in buckets:
        await store.save("t1", figure.name, figure.version, subject, value, (), "P One")
    return store


async def test_offset_buckets_partition_the_days_with_no_overlap_and_no_gap() -> None:
    """`1-3, 4-6` anchored on 2025-08-24: the near bucket is the 22nd-24th
    and the far one the 19th-21st. The boundary days are the test -- a value
    on the 21st belongs to the far bucket alone and one on the 22nd to the
    near bucket alone, so an off-by-one at either edge double-counts a day
    or loses one."""
    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None
    store = await _day_reading_store(
        figure,
        (
            ("p1@2025-08-24", [10.0]),  # anchor day: near bucket
            ("p1@2025-08-22", [20.0]),  # near bucket's oldest day
            ("p1@2025-08-21", [40.0]),  # far bucket's newest day
            ("p1@2025-08-19", [80.0]),  # far bucket's oldest day
            ("p1@2025-08-18", [160.0]),  # beyond both; must appear nowhere
        ),
    )

    result = await serve_reading(
        store, READINGS, "t1", reading, DEFAULTS, ["1-3", "4-6"], at_day="2025-08-24"
    )
    near, far = result.subjects[0].windows
    assert (near.frm, near.to) == ("2025-08-22", "2025-08-24")
    assert (far.frm, far.to) == ("2025-08-19", "2025-08-21")
    assert near.mean == 15.0, "the near bucket must hold the 22nd and the 24th alone"
    assert far.mean == 60.0, "the far bucket must hold the 19th and the 21st alone"
    assert (near.span, near.bucket, near.trailing) == ("3", "day", 3)
    assert (far.span, far.bucket, far.trailing) == ("4-6", "day", None)
    assert far.days_requested == 3


async def test_a_bare_number_and_its_explicit_span_are_one_window() -> None:
    """`3` and `1-3` are two spellings of one question and must serve
    byte-identically -- canonical spelling included, so a client cannot see
    two shapes for one answer."""
    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None
    store = await _day_reading_store(
        figure, (("p1@2025-08-24", [10.0]), ("p1@2025-08-22", [20.0]))
    )
    result = await serve_reading(
        store, READINGS, "t1", reading, DEFAULTS, [3, "1-3"], at_day="2025-08-24"
    )
    bare, explicit = result.subjects[0].windows
    assert bare == explicit
    assert bare.span == "3" and bare.trailing == 3


async def test_the_floor_and_band_hold_per_bucket_not_per_request() -> None:
    """`team_person.speed` requires three values. The near bucket holds
    three, the far bucket one: the near one answers with its band word and
    the far one withholds every statistic together, names the shortfall, and
    bands unknown. One bucket's wealth must not lend the other its floor."""
    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.speed")
    assert figure is not None and reading is not None
    store = await _day_reading_store(
        figure,
        (
            ("p1@2025-08-24", [86_400.0]),
            ("p1@2025-08-23", [172_800.0]),
            ("p1@2025-08-22", [259_200.0]),
            ("p1@2025-08-20", [86_400.0]),  # the far bucket's only value
        ),
    )
    result = await serve_reading(
        store, READINGS, "t1", reading, DEFAULTS, ["1-3", "4-6"], at_day="2025-08-24"
    )
    near, far = result.subjects[0].windows
    assert near.unmet == [] and near.mean == 172_800.0 and near.level == "ok"
    assert far.unmet == ["needs at least 3 values; there is 1"]
    assert far.mean is None and far.worst is None
    assert far.level == "unknown"


async def test_an_hour_span_slices_hours_not_whole_days() -> None:
    """The driving case: a median over the last four hours of a quarter-hour
    figure. A value from one hour ago is in; a value from the same local day
    but before the span's oldest hour must be out -- a day window cannot make
    that cut, which is what the unit exists for."""
    figure = GRAINED.figure("team_person.quarter_lead")
    reading = GRAINED.reading("team_person.quarter_typical")
    assert figure is not None and reading is not None
    store = await _quarter_store(
        figure,
        (
            ("p1@2025-08-24T04:45", [100.0]),  # one hour ago: inside
            ("p1@2025-08-24T02:00", [300.0]),  # the span's oldest hour: inside
            ("p1@2025-08-24T01:45", [900.0]),  # same local day, older: OUT
            ("p1@2025-08-23T06:15", [700.0]),  # yesterday: OUT
        ),
    )
    at = 1_756_036_800_000.0  # 2025-08-24T12:00Z, 05:00 in Los Angeles
    result = await serve_reading(store, GRAINED, "t1", reading, DEFAULTS, ["1-4h"], at_ms=at)
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-08-24T02:00", "2025-08-24T05:00")
    assert (window.span, window.bucket) == ("4", "hour")
    assert window.trailing is None, "an hour span is not a count of trailing days"
    assert window.median == 200.0, (
        "the median must run over the two in-span values alone; 300 or more "
        "means a same-day value leaked in through day arithmetic"
    )
    assert window.days_requested == 1 and window.days_covered == 1


async def test_bare_numbers_stay_days_whatever_the_grain() -> None:
    """The re-scale trap, pinned: over a quarter-hour figure a bare `2` is
    two *days*, never two grain units. Were bare numbers denominated in the
    figure's grain, regrading a figure would silently re-scale every
    bookmarked URL and bundle -- this test is what would catch that design
    arriving. The 01:00 bucket sits 28 hours before the anchor: outside two
    of any sub-day unit anyone might denominate in, inside two days."""
    figure = GRAINED.figure("team_person.quarter_volume")
    reading = GRAINED.reading("team_person.quarter_throughput")
    assert figure is not None and reading is not None
    store = await _quarter_store(
        figure,
        (
            ("p1@2025-08-24T04:45", 2.0),  # today
            ("p1@2025-08-23T01:00", 3.0),  # yesterday, 28h before the anchor
            ("p1@2025-08-22T23:45", 5.0),  # two local days back: OUT of `2`
        ),
    )
    at = 1_756_036_800_000.0  # 05:00 in Los Angeles
    result = await serve_reading(store, GRAINED, "t1", reading, DEFAULTS, [2], at_ms=at)
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-08-23", "2025-08-24")
    assert (window.span, window.bucket, window.trailing) == ("2", "day", 2)
    assert window.total == 5.0, "a bare 2 must cover two whole local days"


async def test_a_sub_day_span_over_day_storage_is_refused_naming_the_grain() -> None:
    """Hour buckets over a day-keyed figure have nothing to slice; the
    refusal names the storage so the fix is in the caller's hands. Silent
    rounding to days would serve a plausible window nobody asked for."""
    from uratori.windows import WindowError

    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None
    store = await _day_reading_store(figure, ())
    with pytest.raises(WindowError, match="stored by day"):
        await serve_reading(
            store, READINGS, "t1", reading, DEFAULTS, ["1-48h"], at_day="2025-08-24"
        )
    with pytest.raises(WindowError, match="stored by 15 minutes"):
        # Minutes cannot slice quarter-hour storage either: a minute bucket
        # would split a stored bucket.
        quarter = GRAINED.reading("team_person.quarter_typical")
        assert quarter is not None
        await serve_reading(
            MemoryEngineStore(), GRAINED, "t1", quarter, DEFAULTS, ["1-90m"]
        )


async def test_a_day_series_does_not_fit_inside_an_hour_span() -> None:
    """`quarter_daily` declares series(...) by day; served over `1-4h` the
    day point would claim twenty hours the span does not cover. Refused with
    the reason, never served partially covered."""
    from uratori.windows import WindowError

    reading = GRAINED.reading("team_person.quarter_daily")
    assert reading is not None
    with pytest.raises(WindowError, match="does not fit"):
        await serve_reading(MemoryEngineStore(), GRAINED, "t1", reading, DEFAULTS, ["1-4h"])


async def test_an_hour_spans_series_covers_exactly_its_buckets() -> None:
    """Series points inside a sub-day span run from the span's oldest bucket
    through the end of its newest -- four hourly points for `1-4h`, sixteen
    quarter-hour points for the same span under a finer series grain -- and
    a bucket outside the span must not appear as a point."""
    figure = GRAINED.figure("team_person.quarter_volume")
    throughput = GRAINED.reading("team_person.quarter_throughput")
    fine = GRAINED.reading("team_person.quarter_fine")
    assert figure is not None and throughput is not None and fine is not None
    store = await _quarter_store(
        figure,
        (
            ("p1@2025-08-24T04:45", 2.0),
            ("p1@2025-08-24T02:15", 3.0),
            ("p1@2025-08-24T01:45", 9.0),  # outside the span
        ),
    )
    at = 1_756_036_800_000.0  # 05:00 in Los Angeles

    result = await serve_reading(store, GRAINED, "t1", throughput, DEFAULTS, ["1-4h"], at_ms=at)
    [window] = result.subjects[0].windows
    assert window.series == [3.0, None, 2.0, None], (
        "hours 02,03,04,05 of the anchor day: 02:15 in the first point, "
        "04:45 in the third, the anchor hour empty -- and 01:45 nowhere"
    )
    assert window.total == 5.0

    finer = await serve_reading(store, GRAINED, "t1", fine, DEFAULTS, ["1-4h"], at_ms=at)
    [window] = finer.subjects[0].windows
    assert window.series is not None
    assert len(window.series) == 16, (
        "a quarter-hour series over four hour buckets is sixteen points, "
        "through the END of the newest bucket -- not stopping at its label"
    )
    assert window.series[1] == 3.0  # 02:15
    assert window.series[11] == 2.0  # 04:45


def test_an_hour_span_counts_labels_across_fall_back_not_elapsed_hours() -> None:
    """Stored labels live in wall-clock space and the fall-back hour's two
    passes merged at write time, so a span steps back through labels: three
    hour buckets ending at 01:00 on the fall-back day are 01:00, 00:00 and
    23:00 -- whatever the elapsed time says."""
    from uratori.engine.buckets import bucket_span
    from uratori.windows import WindowSpec

    zone = "America/Los_Angeles"
    # 2025-11-02T09:30Z is 01:30 PST, the second pass through 01:00 local.
    at = 1_762_075_800_000.0
    assert label_in(at, zone, "15 minutes") == "2025-11-02T01:30"
    assert bucket_span(at, zone, WindowSpec(first=1, last=3, unit="hour")) == (
        "2025-11-01T23:00",
        "2025-11-02T01:00",
    )


def test_a_span_clamps_at_the_calendars_edge_rather_than_overflowing() -> None:
    from uratori.engine.buckets import bucket_span
    from uratori.windows import WindowSpec

    frm, to = bucket_span(
        end_of_day_ms("0001-01-03", "UTC"), "UTC", WindowSpec(first=2, last=10, unit="day")
    )
    assert (frm, to) == ("0001-01-01", "0001-01-02")
    frm, to = bucket_span(
        end_of_day_ms("0001-01-01", "UTC"), "UTC", WindowSpec(first=1, last=5, unit="hour")
    )
    assert to == "0001-01-01T23:00"


def test_the_far_calendar_edge_answers_west_of_utc() -> None:
    """9999-12-31's last local millisecond in New York lands in year 10000
    by UTC's clock; the anchor must clamp to something `datetime` can carry
    while still filing under the anchor's own local day."""
    zone = "America/New_York"
    at = end_of_day_ms("9999-12-31", zone)
    assert day_in(at, zone) == "9999-12-31"
    from datetime import UTC, datetime

    datetime.fromtimestamp(at / 1000.0, tz=UTC)  # must not raise


async def test_a_sub_day_span_under_a_day_anchor_ends_in_the_anchor_days_last_bucket() -> None:
    """The anchor generalises: `?at=` resolves to the day's final moment, so
    bucket 1 of an hour span is the anchor day's 23:00 bucket -- the last
    four hours OF THAT DAY, not of whenever the request ran. A quarter from
    the next local day must stay out."""
    figure = GRAINED.figure("team_person.quarter_lead")
    reading = GRAINED.reading("team_person.quarter_typical")
    assert figure is not None and reading is not None
    store = await _quarter_store(
        figure,
        (
            ("p1@2025-08-24T23:45", [100.0]),  # the anchor day's final quarter: in
            ("p1@2025-08-24T20:00", [300.0]),  # the span's oldest hour: in
            ("p1@2025-08-24T19:45", [900.0]),  # an hour too early: out
            ("p1@2025-08-25T00:00", [700.0]),  # the next local day: out
        ),
    )
    result = await serve_reading(
        store, GRAINED, "t1", reading, DEFAULTS, ["1-4h"], at_day="2025-08-24"
    )
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-08-24T20:00", "2025-08-24T23:00")
    assert window.median == 200.0

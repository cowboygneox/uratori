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
    ordinal_weekday_day,
    part_of,
    read_number,
    read_path,
    selected_day,
)
from uratori.engine.evaluate import Parts, Readers, evaluate, same_value
from uratori.engine.project import holds, ordered, summarise
from uratori.engine.read import (
    Sample,
    level_of,
    sample_over,
    series_of,
    statistics_of,
    unmet_of,
)
from uratori.engine.serve import serve_reading
from uratori.lang.ast import Condition, Number, Part, SortDecl
from uratori.lang.plan import ProjectPlan
from uratori.results import Ok
from uratori.store import MemoryFactStore
from uratori.store.base import Pointer
from uratori.store.memory import MemoryEngineStore

from .world import compile_source

# --------------------------------------------------------------- buckets --

BASE = """
group work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
filter work_issue.active where active == true
filter work_issue.stuck where status_changed_at older than 14 days
filter work_issue.fresh where status_changed_at younger than 14 days
group work_issue.by_day from (assignee_account_id through team_person.accounts.account_id, completed_at by day in team_person.timezone)
group work_issue.by_quarter from (assignee_account_id through team_person.accounts.account_id, completed_at by 15 minutes in team_person.timezone)
group work_issue.by_minute from (assignee_account_id through team_person.accounts.account_id, completed_at by minute in team_person.timezone)

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


def _calendars(by_subject: dict[str, str]) -> MemoryFactStore:
    """A fact store holding nothing but each subject's calendar.

    A group's `by day in team_person.timezone` reads the field off the
    subject's record, so a serving path that resolves windows needs the
    records -- and a test that supplied none would serve every subject in
    UTC while claiming to test a zone.
    """
    facts = MemoryFactStore()
    for key, zone in by_subject.items():
        facts.put("t1", "team_person", key, {"display_name": key, "timezone": zone})
    return facts


LA = _calendars({"p1": "America/Los_Angeles", "w1": "America/Los_Angeles"})
"""The calendar this file's subjects keep, as the tenant dial used to."""


def test_older_than_and_younger_than_are_not_the_same_predicate() -> None:
    """Inverted, every stale merge request reads fresh and every fresh one reads
    stale -- and the board's whole "not moving" column means its opposite."""
    old = {"status_changed_at": "2025-01-01T00:00:00Z"}
    recent = {"status_changed_at": "2025-08-23T00:00:00Z"}

    assert buckets_of(_index("work_issue.stuck"), old, _resolve, NOW) == [""]
    assert buckets_of(_index("work_issue.stuck"), recent, _resolve, NOW) == []
    assert buckets_of(_index("work_issue.fresh"), recent, _resolve, NOW) == [""]
    assert buckets_of(_index("work_issue.fresh"), old, _resolve, NOW) == []


def test_a_record_with_no_readable_moment_is_in_no_age_bucket() -> None:
    """Not in the "old" one. An absent timestamp is not evidence of age."""
    assert buckets_of(_index("work_issue.stuck"), {}, _resolve, NOW) == []
    assert buckets_of(_index("work_issue.fresh"), {}, _resolve, NOW) == []


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
    la = {"person-of-a1": "America/Los_Angeles"}
    utc = {"person-of-a1": "UTC"}
    assert buckets_of(_index("work_issue.by_day"), record, _resolve, NOW, zones=la) == [
        "person-of-a1@2025-08-23"
    ]
    assert buckets_of(_index("work_issue.by_day"), record, _resolve, NOW, zones=utc) == [
        "person-of-a1@2025-08-24"
    ]


def test_a_sub_day_bucket_is_labelled_in_the_tenants_calendar() -> None:
    """The label is local time truncated to the grain, exactly as a day key is
    the local date -- so which quarter-hour an event belongs to is decided by
    the calendar the definition names, not by whichever zone the provider
    happened to write."""
    record = {"assignee_account_id": "a1", "completed_at": "2025-08-24T02:26:40Z"}
    la = {"person-of-a1": "America/Los_Angeles"}
    utc = {"person-of-a1": "UTC"}

    assert buckets_of(_index("work_issue.by_quarter"), record, _resolve, NOW, zones=la) == [
        "person-of-a1@2025-08-23T19:15"
    ]
    assert buckets_of(_index("work_issue.by_quarter"), record, _resolve, NOW, zones=utc) == [
        "person-of-a1@2025-08-24T02:15"
    ]
    assert buckets_of(_index("work_issue.by_minute"), record, _resolve, NOW, zones=la) == [
        "person-of-a1@2025-08-23T19:26"
    ]


def test_the_repeated_hour_of_a_fall_back_merges_into_one_labelled_bucket() -> None:
    """When the clocks go back, 01:30 local happens twice. Both instants carry
    the same label, so their records share a bucket -- the honest answer to
    "what happened in the quarter-hour labelled 01:30", which occurred twice.
    The alternative, keying by UTC, would make every local midnight sit
    mid-bucket in most of the world's zones."""
    chicago = {"person-of-a1": "America/Chicago"}
    cdt = {"assignee_account_id": "a1", "completed_at": "2025-11-02T06:30:00Z"}  # 01:30 CDT
    cst = {"assignee_account_id": "a1", "completed_at": "2025-11-02T07:30:00Z"}  # 01:30 CST

    first = buckets_of(_index("work_issue.by_quarter"), cdt, _resolve, NOW, zones=chicago)
    second = buckets_of(_index("work_issue.by_quarter"), cst, _resolve, NOW, zones=chicago)
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
    calculate:
        when team_person.count >= 5 then "over"
        when team_person.count >= 3 then "warn"
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
figure team_person.opened:
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person}
    calculate:
        count(mine)

# d
figure team_person.bigger:
    display "x"
    unit count
    calculate:
        max(team_person.count, team_person.opened)
"""
    ).figure("team_person.bigger")
    assert plan is not None
    # **Two different reads, deliberately.** Written as `max(x, x)` the mixed
    # case is unconstructible -- both operands resolve together or neither
    # does -- so letting the known side win was a change nothing could see.
    both = _readers(
        parts={
            ("team_person.count", "p1"): Parts((4.0,), ("p1",)),
            ("team_person.opened", "p1"): Parts((9.0,), ("p1",)),
        }
    )
    assert evaluate(plan, "p1", both).value == 9.0, "the control: both known"

    one = _readers(parts={("team_person.count", "p1"): Parts((4.0,), ("p1",))})
    assert evaluate(plan, "p1", one).value is None, (
        "one side was never computed and the other won, so the figure reports "
        "a commitment too small and every share divided by it reads high"
    )
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

    assert holds(nothing, {"x": None}, 0.0) is True
    assert holds(something, {"x": None}, 0.0) is False
    assert holds(nothing, {"x": 3.0}, 0.0) is False
    assert holds(something, {"x": 3.0}, 0.0) is True

    # And an ordinary comparison against an unknown is still unknown.
    assert (
        holds(Condition(left=Part(name="x"), op=">=", right=Number(value=1)), {"x": None}, 0.0)
        is None
    )


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
figure team_person.per_day bucketed:
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
figure team_person.volume bucketed:
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        count(mine)

# d
reading team_person.speed(range):
    display "x"
    band:
        when value > 1814400 then "over"
        when value > 604800 then "warn"
        otherwise "ok"
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


def test_a_ladder_is_tested_in_written_order_so_small_numbers_stay_good() -> None:
    """The direction lives in the comparison operator now, where a reader can
    see it. Inverted -- `<` where the definition means `>` -- the fastest
    reviewer on the board is the one flagged red, and nothing but the operator
    says which way round it should be."""
    plan = READINGS.reading("team_person.speed")
    assert plan is not None
    good, poor = 7 * 86_400.0, 21 * 86_400.0
    assert level_of(plan, {"mean": good - 1}) == "ok"
    assert level_of(plan, {"mean": (good + poor) / 2}) == "warn"
    assert level_of(plan, {"mean": poor + 1}) == "over"


def test_a_band_over_an_absent_statistic_is_unknown_rather_than_good() -> None:
    plan = READINGS.reading("team_person.speed")
    assert plan is not None
    assert level_of(plan, {"mean": None}) == "unknown"


def test_the_serve_path_reads_both_stored_shapes() -> None:
    """A `list` figure keeps every value for a day and a `count` figure keeps one
    scalar. Dropping the scalar branch made every volume figure stored,
    versioned and unreadable -- with the checker and the reader agreeing, so no
    request ever came back wrong."""
    listed = sample_over([("2025-08-01", [10.0, 20.0])], ["2025-08-01"])
    assert listed.values == (10.0, 20.0)
    assert listed.buckets_covered == 1

    scalar = sample_over([("2025-08-01", 4.0)], ["2025-08-01"])
    assert scalar.values == (4.0,), "a count figure's stored scalar was dropped"
    assert scalar.buckets_covered == 1


def test_a_sum_of_nothing_is_nought_and_a_mean_of_nothing_is_unknown() -> None:
    """Deliberate asymmetry. A queue that took no tickets took no tickets; an
    average of no values is a claim nobody can make."""
    empty = Sample(values=(), points=(), buckets_covered=0, buckets_requested=7)

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
    short = Sample(values=(1.0,), points=(), buckets_covered=1, buckets_requested=7)
    unmet = unmet_of(plan, short)
    assert unmet and "3" in unmet[0] and "1" in unmet[0]

    enough = Sample(values=(1.0, 2.0, 3.0), points=(), buckets_covered=3, buckets_requested=7)
    assert unmet_of(plan, enough) == []


def test_one_value_renders_and_only_an_empty_window_is_withheld_with_a_reason() -> None:
    """The unwritten floor is one value: a small team's first merge renders
    rather than sitting behind a dash, and an empty window names what fell
    short instead of nulling silently."""
    plan = READINGS.reading("team_person.pace")
    assert plan is not None

    one = Sample(values=(5.0,), points=(), buckets_covered=1, buckets_requested=7)
    assert unmet_of(plan, one) == []
    assert statistics_of(plan, one)["mean"] == 5.0

    empty = Sample(values=(), points=(), buckets_covered=0, buckets_requested=7)
    unmet = unmet_of(plan, empty)
    # The full sentence, because "at least 1 value" is also a substring of the
    # plural and would pass against "at least 1 values".
    assert unmet == ["needs at least 1 value; there are 0"]


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
figure team_person.spans bucketed:
    display "x"
    depends:
        mine = work_issue.by_day:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
reading team_person.worst_only(range):
    display "x"
    band:
        when value > 604800 then "over"
        otherwise "ok"
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
    calculate:
        team_person.count
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
    assert summarise(plan, rows, 0.0).values["big"] == 1.0


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
    assert summarise(plan, complete, 0.0).values["all"] == 5.0

    gap = [*complete, ProjectedRow(id="c", values={"n": None}, units={}, flags=(), sort_key=None)]
    assert summarise(plan, gap, 0.0).values["all"] is None


# -------------------------------------------------------- sub-day grouping --

GRAINED = compile_source(
    BASE
    + """
# d
figure team_person.quarter_volume bucketed:
    display "x"
    depends:
        mine = work_issue.by_quarter:{team_person}
    calculate:
        count(mine)

# d
figure team_person.quarter_lead bucketed:
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
        series(m)

# d
reading team_person.quarter_pace(range):
    display "x"
    depends:
        m = team_person.quarter_lead in range
    requires:
        at least 3 values in m
    calculate:
        mean(m)
        series(m)

# d
reading team_person.quarter_typical(range):
    display "x"
    depends:
        m = team_person.quarter_lead in range
    calculate:
        median(m)

group work_issue.by_month from (assignee_account_id through team_person.accounts.account_id, completed_at by month in team_person.timezone)
group work_issue.by_calendar_quarter from (assignee_account_id through team_person.accounts.account_id, completed_at by quarter in team_person.timezone)
group work_issue.by_hour from (assignee_account_id through team_person.accounts.account_id, completed_at by hour in team_person.timezone)
group work_issue.by_week from (assignee_account_id through team_person.accounts.account_id, completed_at by week in team_person.timezone)
group work_issue.by_first_monday from (assignee_account_id through team_person.accounts.account_id, completed_at by first monday of month in team_person.timezone)
group work_issue.by_fifth_monday from (assignee_account_id through team_person.accounts.account_id, completed_at by fifth monday of month)

# d
figure team_person.weekly_lead bucketed:
    display "x"
    depends:
        mine = work_issue.by_week:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
reading team_person.weekly_pace(range):
    display "x"
    depends:
        m = team_person.weekly_lead in range
    calculate:
        median(m)

# d
figure team_person.monthly_volume bucketed:
    display "x"
    depends:
        mine = work_issue.by_month:{team_person}
    calculate:
        count(mine)

# d
figure team_person.monthly_lead bucketed:
    display "x"
    depends:
        mine = work_issue.by_month:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
figure team_person.quarterly_volume bucketed:
    display "x"
    depends:
        mine = work_issue.by_calendar_quarter:{team_person}
    calculate:
        count(mine)

# d
figure team_person.hourly_lead bucketed:
    display "x"
    depends:
        mine = work_issue.by_hour:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
figure team_person.first_monday_lead bucketed:
    display "x"
    depends:
        mine = work_issue.by_first_monday:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
figure team_person.fifth_monday_volume bucketed:
    display "x"
    depends:
        mine = work_issue.by_fifth_monday:{team_person}
    calculate:
        count(mine)

# d
reading team_person.monthly_shipped(range):
    display "x"
    depends:
        m = team_person.monthly_volume in range
    calculate:
        sum(m)
        series(m)

# d
reading team_person.monthly_pace(range):
    display "x"
    depends:
        m = team_person.monthly_lead in range
    calculate:
        mean(m)

# d
reading team_person.quarterly_shipped(range):
    display "x"
    depends:
        m = team_person.quarterly_volume in range
    calculate:
        sum(m)

# d
reading team_person.hourly_typical(range):
    display "x"
    depends:
        m = team_person.hourly_lead in range
    calculate:
        median(m)

# d
reading team_person.first_monday_pace(range):
    display "x"
    depends:
        m = team_person.first_monday_lead in range
    calculate:
        median(m)
        series(m)

# d
reading team_person.fifth_monday_shipped(range):
    display "x"
    depends:
        m = team_person.fifth_monday_volume in range
    calculate:
        sum(m)

# d
figure team_person.minute_lead bucketed:
    display "x"
    depends:
        mine = work_issue.by_minute:{team_person}
    calculate:
        list(work_issue.lead over mine)

# d
reading team_person.minute_typical(range):
    display "x"
    depends:
        m = team_person.minute_lead in range
    calculate:
        median(m)
"""
)


def test_a_series_point_is_the_stored_bucket_and_a_hole_stays_a_hole() -> None:
    """A series is one point per bucket of the figure's own sequence, holes
    included: a quarter-hour nobody merged in is not a quarter-hour somebody
    merged nothing in -- inventing a nought would draw a floor that never
    happened."""
    labels = ["2025-08-01T00:15", "2025-08-01T00:30", "2025-08-01T00:45"]
    sample = sample_over(
        [("2025-08-01T00:15", 2.0), ("2025-08-01T00:45", 1.0)], labels
    )
    assert series_of(sample) == [2.0, None, 1.0]
    assert sample.values == (2.0, 1.0)
    assert sample.buckets_covered == 2
    assert sample.buckets_requested == 3


def test_a_list_buckets_point_is_its_records_mean_and_the_statistics_pool() -> None:
    """The scalar statistics run over the window's raw records; a bucket's
    series point is the mean of its own. A mean of the points would weight
    each month equally instead of each record -- 37.5 here -- the
    mean-of-means trap wearing a calendar."""
    sample = sample_over(
        [("2025-07", [10.0, 20.0]), ("2025-08", [60.0])], ["2025-07", "2025-08"]
    )
    plan = READINGS.reading("team_person.speed")
    assert plan is not None
    assert statistics_of(plan, sample)["mean"] == 30.0
    assert series_of(sample) == [15.0, 60.0]


# ------------------------------------------------------ resolving sequences --


def test_a_month_span_resolves_to_calendar_months_across_the_year_edge() -> None:
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    at = end_of_day_ms("2026-02-10", "UTC")
    assert resolve_span(at, "UTC", WindowSpec(1, 4), "month") == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]
    assert resolve_span(at, "UTC", WindowSpec(3, 4), "month") == ["2025-11", "2025-12"]


def test_a_quarter_span_walks_calendar_quarters_across_the_year_edge() -> None:
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    at = end_of_day_ms("2026-02-10", "UTC")
    assert resolve_span(at, "UTC", WindowSpec(1, 4), "quarter") == [
        "2025-Q2",
        "2025-Q3",
        "2025-Q4",
        "2026-Q1",
    ]


def test_a_week_span_is_iso_weeks_year_53_included() -> None:
    """A week label carries the **ISO** year, which is not always the calendar
    year of the week's Monday -- so a New Year anchor must not tear the
    sequence.

    2027-01-01 alone cannot show this: its week's Monday is 2026-12-28, whose
    calendar year already equals its ISO year, so a resolver using either
    answers `2026-W53` and the test passes on a coincidence. 2025-12-31 is the
    case that distinguishes them -- its week's Monday is 2025-12-29, calendar
    year 2025 but ISO year **2026** -- so a calendar-year label would say
    `2025-W01`, a week fifty-one places out of order.
    """
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    at = end_of_day_ms("2027-01-01", "UTC")
    assert resolve_span(at, "UTC", WindowSpec(1, 2), "week") == [
        "2026-W52",
        "2026-W53",
    ], "a 53-week year has a W53, and the sequence must reach it"

    edge = end_of_day_ms("2025-12-31", "UTC")
    assert resolve_span(edge, "UTC", WindowSpec(1, 2), "week") == [
        "2025-W52",
        "2026-W01",
    ], (
        "the anchor week's Monday is 2025-12-29 -- calendar year 2025, ISO "
        "year 2026. A calendar-year label would file it as 2025-W01 and sort "
        "it before the week it follows"
    )


def test_the_anchor_month_is_the_zones_month_not_utcs() -> None:
    """2026-09-01T02:00Z is still 31 August in Los Angeles: bucket 1 of a
    month span must be 2026-08 there, and 2026-09 in UTC -- the control that
    catches a resolver doing zone arithmetic on the wrong side."""
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    at_ms = 1_787_536_800_000.0  # 2026-09-01T02:00:00Z... pinned below
    from datetime import UTC, datetime

    at_ms = datetime(2026, 9, 1, 2, 0, tzinfo=UTC).timestamp() * 1000.0
    assert resolve_span(at_ms, "America/Los_Angeles", WindowSpec(1, 1), "month") == ["2026-08"]
    assert resolve_span(at_ms, None, WindowSpec(1, 1), "month") == ["2026-09"]


def test_first_monday_positions_walk_the_calendars_sparse_sequence() -> None:
    """Bucket 1 is the most recent first-Monday at or before the anchor --
    the anchor's own day when it *is* one -- and the positions step back one
    first-Monday per month, sparse days and all."""
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    rule = "first monday of month"
    at = end_of_day_ms("2026-08-28", "UTC")
    assert resolve_span(at, "UTC", WindowSpec(1, 3), rule) == [
        "2026-06-01",
        "2026-07-06",
        "2026-08-03",
    ]
    # On the day itself it is bucket 1; the day before, the previous month's.
    assert resolve_span(end_of_day_ms("2026-08-03", "UTC"), "UTC", WindowSpec(1, 1), rule) == [
        "2026-08-03"
    ]
    assert resolve_span(end_of_day_ms("2026-08-02", "UTC"), "UTC", WindowSpec(1, 1), rule) == [
        "2026-07-06"
    ]


def test_fifth_mondays_skip_months_without_one_rather_than_inventing_buckets() -> None:
    """The sequence is the calendar's own: months with no fifth Monday
    contribute no position, so the last three fifth-Mondays before September
    2026 span five months -- and a month that has one but no data is a hole
    in the window, never a skipped position."""
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    at = end_of_day_ms("2026-09-01", "UTC")
    assert resolve_span(at, "UTC", WindowSpec(1, 3), "fifth monday of month") == [
        "2026-03-30",
        "2026-06-29",
        "2026-08-31",
    ]
    assert resolve_span(at, "UTC", WindowSpec(2, 4), "fifth monday of month") == [
        "2025-12-29",
        "2026-03-30",
        "2026-06-29",
    ]


def test_an_hour_span_counts_labels_across_fall_back_not_elapsed_hours() -> None:
    """Stored labels live in wall-clock space and the fall-back hour's two
    passes merged at write time, so a span steps back through labels: three
    hour buckets ending at 01:00 on the fall-back day are 01:00, 00:00 and
    23:00 -- whatever the elapsed time says."""
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    zone = "America/Los_Angeles"
    # 2025-11-02T09:30Z is 01:30 PST, the second pass through 01:00 local.
    at = 1_762_075_800_000.0
    assert label_in(at, zone, "15 minutes") == "2025-11-02T01:30"
    assert resolve_span(at, zone, WindowSpec(1, 3), "hour") == [
        "2025-11-01T23:00",
        "2025-11-02T00:00",
        "2025-11-02T01:00",
    ]


def test_a_span_clamps_at_the_calendars_edge_rather_than_overflowing() -> None:
    from uratori.engine.buckets import resolve_span
    from uratori.windows import WindowSpec

    labels = resolve_span(
        end_of_day_ms("0001-01-03", "UTC"), "UTC", WindowSpec(2, 10), "day"
    )
    assert (labels[0], labels[-1]) == ("0001-01-01", "0001-01-02")
    labels = resolve_span(
        end_of_day_ms("0001-01-01", "UTC"), "UTC", WindowSpec(1, 5), "hour"
    )
    assert labels[-1] == "0001-01-01T23:00"
    labels = resolve_span(
        end_of_day_ms("0001-01-01", "UTC"), "UTC", WindowSpec(1, 1000), "hour"
    )
    assert (labels[0], labels[-1]) == ("0001-01-01T00:00", "0001-01-01T23:00")
    labels = resolve_span(
        end_of_day_ms("0001-02-15", "UTC"), "UTC", WindowSpec(1, 30), "month"
    )
    assert (labels[0], labels[-1]) == ("0001-01", "0001-02")


def test_every_stored_label_files_under_the_month_its_own_day_is_in() -> None:
    """The write-time property the trio hangs off: an instant's month label
    is the month of its *zoned day*, its quarter is that month's quarter and
    its ISO week holds that day -- one calendar application, at the day, so
    a month figure and a day figure can never disagree about one event.
    Probed across DST transitions, midnights and year edges; the UTC-vs-zone
    boundary case is the control that fails a resolver applying zones
    anywhere else."""
    from datetime import UTC, datetime

    probes = []
    for spec in (
        (2026, 1, 1, 0, 0),
        (2025, 12, 31, 23, 59),
        (2026, 3, 8, 10, 30),  # US spring-forward day
        (2025, 11, 2, 9, 30),  # US fall-back day, the repeated hour
        (2026, 6, 30, 23, 45),
        (2026, 7, 1, 0, 15),
        (2026, 9, 1, 2, 0),
    ):
        probes.append(datetime(*spec, tzinfo=UTC).timestamp() * 1000.0)
    for zone in (None, "America/Los_Angeles", "Pacific/Kiritimati", "Asia/Beirut"):
        for at in probes:
            day = label_in(at, zone, "day")
            month = label_in(at, zone, "month")
            quarter = label_in(at, zone, "quarter")
            week = label_in(at, zone, "week")
            assert month == day[:7], (zone, day)
            assert quarter == f"{day[:4]}-Q{(int(day[5:7]) - 1) // 3 + 1}", (zone, day)
            from datetime import date

            iso = date.fromisoformat(day).isocalendar()
            assert week == f"{iso.year:04d}-W{iso.week:02d}", (zone, day)
    # The control: at 2026-09-01T02:00Z the LA month and the UTC month
    # genuinely differ, so a month rule reading the wrong calendar cannot
    # pass the loop above by coincidence.
    boundary = datetime(2026, 9, 1, 2, 0, tzinfo=UTC).timestamp() * 1000.0
    assert label_in(boundary, "America/Los_Angeles", "month") == "2026-08"
    assert label_in(boundary, None, "month") == "2026-09"


def test_a_record_lands_in_its_first_monday_bucket_or_in_none_at_all() -> None:
    """The selective rule is partial and the partiality is the filter: a
    record completed on the first Monday buckets under that day, and a
    record on any other day is in no bucket -- there is no narrowing step a
    cheap path could skip, because off-rule the function is undefined."""
    la = {"person-of-a1": "America/Los_Angeles"}
    on_monday = {"assignee_account_id": "a1", "completed_at": "2026-08-03T18:30:00-07:00"}
    mondays = GRAINED.indexes["work_issue.by_first_monday"]
    assert buckets_of(mondays, on_monday, _resolve, NOW, zones=la) == ["person-of-a1@2026-08-03"]
    off_monday = {"assignee_account_id": "a1", "completed_at": "2026-08-04T18:30:00-07:00"}
    assert buckets_of(mondays, off_monday, _resolve, NOW, zones=la) == []
    # The zone decides which day -- and therefore whether the rule holds at
    # all: 2026-08-04T02:00Z is still Monday the 3rd in Los Angeles.
    edge = {"assignee_account_id": "a1", "completed_at": "2026-08-04T02:00:00Z"}
    assert buckets_of(mondays, edge, _resolve, NOW, zones=la) == ["person-of-a1@2026-08-03"]
    utc = {"person-of-a1": "UTC"}
    assert buckets_of(mondays, edge, _resolve, NOW, zones=utc) == []


def test_a_coarse_bucket_holds_the_records_of_its_own_period_directly() -> None:
    """**A month bucket is every record of the month, never a rollup of day
    buckets.** This is the claim the whole coarse-grain design rests on: two
    grains are two declarations, each computed from the records, so each is
    citable on its own rather than one being a re-slicing of the other.

    Every other coarse-grain test hand-seeds storage and then reads it back,
    which proves the read paths agree given equal storage -- a write path
    that only bucketed some of the records would sail straight through all of
    them. This one drives raw records into `buckets_of`, the single place a
    record's buckets are decided.

    Be precise about what "directly" means here, because the source does the
    opposite of one reading of it: `label_in` derives a month, quarter or week
    label from the record's own *local day*, deliberately, so a month figure
    and a day figure can never disagree about which month an event was in. The
    claim under test is not that the label avoids the day -- it is that the
    month bucket is filled by **the records themselves**, one pass over each,
    rather than by rolling up what a day *figure* already computed. A rollup
    would inherit the day figure's population, and any record the day rule
    dropped would silently vanish from the month.

    So the load-bearing assertion is the coverage one: every record lands in
    exactly one bucket of every total rule, none dropped and none invented.
    That is what catches a narrowing `_keys_for`, and it is the only test in
    the suite that does. The month and quarter lists beneath it pin the
    calendar arithmetic at the edges; the day-rule comparison at the end is
    the agreement `label_in` promises, restated where a reader of this test
    will look for it.
    """
    la = {"person-of-a1": "America/Los_Angeles"}
    # Deliberately spread across month, quarter and year edges, and across a
    # DST boundary, in a zone whose day differs from UTC's for part of each.
    stamps = (
        "2026-01-01T00:30:00-08:00",
        "2026-01-31T23:30:00-08:00",
        "2026-02-01T00:30:00-08:00",
        "2026-03-08T03:30:00-07:00",
        "2026-03-31T23:00:00-07:00",
        "2026-04-01T00:00:00-07:00",
        "2026-06-30T23:59:00-07:00",
        "2026-07-01T00:01:00-07:00",
        "2026-12-31T23:30:00-08:00",
        "2027-01-01T00:30:00-08:00",
    )
    records = [{"assignee_account_id": "a1", "completed_at": stamp} for stamp in stamps]

    by_rule = {
        rule: [
            buckets_of(GRAINED.indexes[index], record, _resolve, NOW, zones=la)
            for record in records
        ]
        for rule, index in (
            ("day", "work_issue.by_day"),
            ("month", "work_issue.by_month"),
            ("quarter", "work_issue.by_calendar_quarter"),
        )
    }

    # Every total rule files every record exactly once. A coarse rule that
    # dropped a record -- or that only knew about records some other rule had
    # already bucketed -- fails here, and that is the narrowing rule 4
    # forbids.
    for rule, filed in by_rule.items():
        assert [len(b) for b in filed] == [1] * len(records), (
            f"{rule} left a record in no bucket, or in two"
        )

    # The coarse label is the record's own period. Spelled out per record
    # rather than computed, so a wrong month at one edge names itself instead
    # of being reproduced by the same arithmetic on both sides.
    months = [b[0].split("@", 1)[1] for b in by_rule["month"]]
    assert months == [
        "2026-01", "2026-01", "2026-02", "2026-03", "2026-03",
        "2026-04", "2026-06", "2026-07", "2026-12", "2027-01",
    ]
    quarters = [b[0].split("@", 1)[1] for b in by_rule["quarter"]]
    assert quarters == [
        "2026-Q1", "2026-Q1", "2026-Q1", "2026-Q1", "2026-Q1",
        "2026-Q2", "2026-Q2", "2026-Q3", "2026-Q4", "2027-Q1",
    ]

    # The agreement `label_in` promises, restated here: a month figure and a
    # day figure can never disagree about which month an event was in,
    # because both read the same zoned day. Not a control for the assertions
    # above -- it is the same relation from the other side -- but the
    # property a reader of a two-grain board depends on.
    days = [b[0].split("@", 1)[1] for b in by_rule["day"]]
    assert [d[:7] for d in days] == months
    assert [f"{d[:4]}-Q{(int(d[5:7]) - 1) // 3 + 1}" for d in days] == quarters


# --------------------------------------------------------- serving sequences --


async def _figure_store(figure, index_name, rows):  # type: ignore[no-untyped-def]
    store = MemoryEngineStore()
    await store.set_pointer(
        "t1", figure.name, Pointer(version=figure.version, settings_fingerprint="")
    )
    await store.set_buckets("t1", index_name, "w1", ["seed"])
    for subject, value in rows:
        await store.save("t1", figure.name, figure.version, subject, value, (), "P One")
    return store


async def test_a_sub_day_figures_windows_are_positions_in_its_own_sequence() -> None:
    """`over 1-96` on a quarter-hour figure is the last ninety-six stored
    quarter-hours -- a day of them -- both edges inclusive: the bucket at
    position 96 is in, the one behind it is out, and the labels on the
    window say exactly which buckets those were."""
    figure = GRAINED.figure("team_person.quarter_volume")
    reading = GRAINED.reading("team_person.quarter_throughput")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_quarter",
        (
            ("p1@2025-08-24T05:00", 2.0),  # the anchor bucket itself: position 1
            ("p1@2025-08-24T04:45", 3.0),  # position 2
            ("p1@2025-08-23T05:15", 5.0),  # position 96, the far edge: in
            ("p1@2025-08-23T05:00", 9.0),  # position 97: out
        ),
    )
    at = 1_756_036_800_000.0  # 2025-08-24T12:00Z, exactly 05:00 in Los Angeles
    result = await serve_reading(store, GRAINED, "t1", reading, ["1-96"], at_ms=at, facts=LA)
    assert isinstance(result.state, Ok)
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-08-23T05:15", "2025-08-24T05:00")
    assert (window.span, window.bucket, window.trailing) == ("96", "15 minutes", None)
    assert window.total == 10.0, "a bucket at a window edge was dropped or leaked"
    assert window.series is not None and len(window.series) == 96
    assert window.series[0] == 5.0 and window.series[-1] == 2.0 and window.series[-2] == 3.0
    assert window.buckets_requested == 96 and window.buckets_covered == 3


async def test_a_failed_floor_withholds_the_series_with_everything_else() -> None:
    """Every statistic is withheld together, and the series is a statistic: a
    sparkline drawn beside a suppressed mean would be the outlier's shape
    published under a heading that says the sample was too small to say
    anything."""
    store = MemoryEngineStore()
    figure = GRAINED.figure("team_person.quarter_lead")
    reading = GRAINED.reading("team_person.quarter_pace")
    assert figure is not None and reading is not None

    tenant = "t1"
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint="")
    )
    await store.set_buckets(tenant, "work_issue.by_quarter", "w1", ["p1@2025-08-24T04:45"])
    await store.save(
        tenant, figure.name, figure.version, "p1@2025-08-24T04:45", [3600.0, 7200.0], (), "P One"
    )

    at = 1_756_036_800_000.0  # 2025-08-24T12:00Z
    result = await serve_reading(store, GRAINED, tenant, reading, [7], at_ms=at, facts=LA)

    assert isinstance(result.state, Ok)
    window = result.subjects[0].windows[0]
    assert window.unmet, "two values against a floor of three should have fallen short"
    assert window.mean is None
    assert window.series is None


async def test_a_month_window_pools_the_months_own_buckets() -> None:
    """`over 6` on a month figure is the last six calendar months, bucket 1
    the anchor's own month-so-far: the sum pools exactly those buckets, the
    edges travel as month labels, and the series is one point per month with
    honest holes."""
    figure = GRAINED.figure("team_person.monthly_volume")
    reading = GRAINED.reading("team_person.monthly_shipped")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_month",
        (
            ("p1@2026-08", 2.0),
            ("p1@2026-07", 3.0),
            ("p1@2026-02", 9.0),  # one month beyond the span: out
        ),
    )
    result = await serve_reading(store, GRAINED, "t1", reading, [6], at_day="2026-08-15"
    )
    assert isinstance(result.state, Ok)
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2026-03", "2026-08")
    assert (window.span, window.bucket, window.trailing) == ("6", "month", None)
    assert window.total == 5.0
    assert window.series == [None, None, None, None, 3.0, 2.0]
    assert window.buckets_requested == 6 and window.buckets_covered == 2
    assert window.buckets is None, "a contiguous rule needs no bucket list; the edges say it"


async def test_each_serves_one_window_per_bucket_with_every_rule_per_window() -> None:
    """`each:1-3` on a month reading is three one-month windows, nearest
    first, each floored and answered on its own -- August's mean, July's
    honest shortfall, June's mean -- and it serves identically to the
    enumerated spelling, which is all `each` is."""
    figure = GRAINED.figure("team_person.monthly_lead")
    reading = GRAINED.reading("team_person.monthly_pace")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_month",
        (
            ("p1@2026-08", [86_400.0]),
            ("p1@2026-06", [86_400.0, 172_800.0]),
        ),
    )
    sugared = await serve_reading(store, GRAINED, "t1", reading, ["each:1-3"], at_day="2026-08-15"
    )
    august, july, june = sugared.subjects[0].windows
    # Bucket 1's one-bucket window spells canonically as the trailing "1".
    assert [w.span for w in (august, july, june)] == ["1", "2-2", "3-3"]
    assert (august.frm, august.to) == ("2026-08", "2026-08")
    assert august.mean == 86_400.0
    assert july.mean is None and july.unmet, "an empty month must fall short on its own"
    assert june.mean == 129_600.0

    spelled = await serve_reading(store, GRAINED, "t1", reading, ["1", "2-2", "3-3"], at_day="2026-08-15"
    )
    assert sugared.subjects[0].windows == spelled.subjects[0].windows

    # The pooled control: `over 3` is ONE window over the same buckets, so
    # the distinction each exists for is visible in the answer.
    pooled = await serve_reading(store, GRAINED, "t1", reading, [3], at_day="2026-08-15"
    )
    [window] = pooled.subjects[0].windows
    assert window.mean == 115_200.0, "the pooled mean runs over all three records"


async def test_a_quarter_window_walks_calendar_quarters_across_the_year() -> None:
    figure = GRAINED.figure("team_person.quarterly_volume")
    reading = GRAINED.reading("team_person.quarterly_shipped")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_calendar_quarter",
        (
            ("p1@2026-Q1", 1.0),  # position 1 at a February anchor: out of 2-3
            ("p1@2025-Q4", 2.0),
            ("p1@2025-Q3", 4.0),
            ("p1@2025-Q2", 8.0),  # position 4: out
        ),
    )
    result = await serve_reading(store, GRAINED, "t1", reading, ["2-3"], at_day="2026-02-10"
    )
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-Q3", "2025-Q4")
    assert (window.span, window.bucket) == ("2-3", "quarter")
    assert window.total == 6.0


async def test_an_hour_figure_serves_the_last_hours_of_its_own_sequence() -> None:
    """The hour grain end to end: `over 4` is the last four stored hours in
    the tenant's calendar -- a value in the anchor hour is in, one four
    hours back is in at the edge, one five hours back is out."""
    figure = GRAINED.figure("team_person.hourly_lead")
    reading = GRAINED.reading("team_person.hourly_typical")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_hour",
        (
            ("p1@2025-08-24T05:00", [100.0]),  # the anchor hour: in
            ("p1@2025-08-24T02:00", [300.0]),  # position 4, the far edge: in
            ("p1@2025-08-24T01:00", [900.0]),  # position 5: out
        ),
    )
    at = 1_756_036_800_000.0  # 05:00 in Los Angeles
    result = await serve_reading(store, GRAINED, "t1", reading, [4], at_ms=at, facts=LA)
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-08-24T02:00", "2025-08-24T05:00")
    assert (window.span, window.bucket, window.trailing) == ("4", "hour", None)
    assert window.median == 200.0


async def test_a_sparse_window_carries_its_buckets_because_edges_cannot() -> None:
    """Six first-Mondays span half a year of dates, and the days between
    them are not covered: the window carries the full bucket list -- the
    honest wire shape for a sequence whose members are not contiguous --
    and a stored day that is not on the rule's sequence stays out even when
    it sits between the edges."""
    figure = GRAINED.figure("team_person.first_monday_lead")
    reading = GRAINED.reading("team_person.first_monday_pace")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_first_monday",
        (
            ("p1@2026-08-03", [100.0]),
            ("p1@2026-06-01", [300.0]),
            ("p1@2026-05-05", [999.0]),  # between the edges, off the sequence: out
            ("p1@2026-02-02", [777.0]),  # position 7: out
        ),
    )
    result = await serve_reading(store, GRAINED, "t1", reading, [6], at_day="2026-08-28"
    )
    [window] = result.subjects[0].windows
    assert window.bucket == "first monday of month"
    assert window.buckets == [
        "2026-03-02",
        "2026-04-06",
        "2026-05-04",
        "2026-06-01",
        "2026-07-06",
        "2026-08-03",
    ]
    assert (window.frm, window.to) == ("2026-03-02", "2026-08-03")
    assert window.median == 200.0, "an off-sequence or out-of-span day leaked into the sample"
    assert window.series == [None, None, None, 300.0, None, 100.0]
    assert window.buckets_requested == 6 and window.buckets_covered == 2


async def test_fifth_monday_windows_pool_only_the_buckets_that_exist() -> None:
    figure = GRAINED.figure("team_person.fifth_monday_volume")
    reading = GRAINED.reading("team_person.fifth_monday_shipped")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_fifth_monday",
        (
            ("p1@2026-08-31", 2.0),
            ("p1@2026-06-29", 3.0),
        ),
    )
    result = await serve_reading(store, GRAINED, "t1", reading, [3], at_day="2026-09-01"
    )
    [window] = result.subjects[0].windows
    assert window.buckets == ["2026-03-30", "2026-06-29", "2026-08-31"]
    assert window.total == 5.0
    assert window.buckets_requested == 3, (
        "three positions spanning five months: months without a fifth "
        "Monday are not positions"
    )


async def test_a_month_figure_agrees_with_the_day_figure_over_the_same_days() -> None:
    """The equivalence that makes the trio trustworthy: a month window's
    statistic over the month figure equals the same statistic over the day
    figure's days, when the two figures hold the same records -- because a
    month bucket is the month's records, not a second arithmetic. The
    control narrows the day window by one day and must disagree."""
    day_figure = READINGS.figure("team_person.per_day")
    day_reading = READINGS.reading("team_person.pace")
    month_figure = GRAINED.figure("team_person.monthly_lead")
    month_reading = GRAINED.reading("team_person.monthly_pace")
    assert day_figure is not None and day_reading is not None
    assert month_figure is not None and month_reading is not None

    day_store = await _figure_store(
        day_figure,
        "work_issue.by_day",
        (
            ("p1@2026-03-01", [10.0]),
            ("p1@2026-05-15", [20.0, 40.0]),
            ("p1@2026-08-28", [30.0]),
        ),
    )
    month_store = await _figure_store(
        month_figure,
        "work_issue.by_month",
        (
            ("p1@2026-03", [10.0]),
            ("p1@2026-05", [20.0, 40.0]),
            ("p1@2026-08", [30.0]),
        ),
    )

    months = await serve_reading(
        month_store, GRAINED, "t1", month_reading, [6], at_day="2026-08-28", facts=LA
    )
    # 2026-03-01 .. 2026-08-28 inclusive is 181 days.
    days = await serve_reading(
        day_store, READINGS, "t1", day_reading, [181], at_day="2026-08-28", facts=LA
    )
    assert months.subjects[0].windows[0].mean == days.subjects[0].windows[0].mean == 25.0

    control = await serve_reading(day_store, READINGS, "t1", day_reading, [180], at_day="2026-08-28"
    )
    assert control.subjects[0].windows[0].mean == 30.0, (
        "the control window drops 1 March; agreeing anyway would mean the "
        "equivalence test cannot fail"
    )


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


def test_the_far_calendar_edge_answers_west_of_utc() -> None:
    """9999-12-31's last local millisecond in New York lands in year 10000
    by UTC's clock; the anchor must clamp to something `datetime` can carry
    while still filing under the anchor's own local day."""
    zone = "America/New_York"
    at = end_of_day_ms("9999-12-31", zone)
    assert day_in(at, zone) == "9999-12-31"
    from datetime import UTC, datetime

    datetime.fromtimestamp(at / 1000.0, tz=UTC)  # must not raise


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
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint="")
    )
    await store.set_buckets(tenant, "work_issue.by_day", "w1", ["p1@2025-08-24"])
    for subject, values in (
        ("p1@2025-08-18", [2.0]),  # the first day inside a 7-day window
        ("p1@2025-08-24", [4.0]),  # the anchor day itself
        ("p1@2025-08-25", [64.0]),  # the day after the anchor; must not leak in
        ("p1@2025-08-17", [128.0]),  # the day before the window opens
    ):
        await store.save(tenant, figure.name, figure.version, subject, values, (), "P One")

    result = await serve_reading(store, READINGS, tenant, reading, [7],
        at_day="2025-08-24",
        facts=_calendars({"p1": "America/Los_Angeles"}),
    )

    assert isinstance(result.state, Ok)
    window = result.subjects[0].windows[0]
    assert (window.frm, window.to) == ("2025-08-18", "2025-08-24")
    assert window.mean == 3.0, "a day beyond the anchor leaked into the sample"
    assert window.buckets_covered == 2
    assert window.buckets_requested == 7
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
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint="")
    )
    await store.set_buckets(tenant, "work_issue.by_day", "w1", ["p1@2025-08-24"])
    for subject, values in (
        ("p1@2025-08-22", [86_400.0]),
        ("p1@2025-08-23", [172_800.0]),
        ("p1@2025-08-24", [259_200.0]),
    ):
        await store.save(tenant, figure.name, figure.version, subject, values, (), "P One")

    short = await serve_reading(store, READINGS, tenant, reading, [7], at_day="2025-08-23"
    )
    window = short.subjects[0].windows[0]
    assert window.unmet, "two values against a floor of three should have fallen short"
    assert window.mean is None
    assert window.level == "unknown"

    met = await serve_reading(store, READINGS, tenant, reading, [7], at_day="2025-08-24"
    )
    window = met.subjects[0].windows[0]
    assert window.unmet == []
    assert window.mean == 172_800.0
    # Two days under flow.leadTimeDays.good (seven), so the low band says ok.
    assert window.level == "ok"

    assert short.version == met.version, "an anchor moved a version hash"


async def test_a_sub_day_reading_under_an_anchor_ends_in_the_anchor_days_last_bucket() -> None:
    """`?at=` resolves to the day's final moment, so bucket 1 of a
    quarter-hour figure's window is the anchor day's 23:45 bucket -- the
    last quarters OF THAT DAY, not of whenever the request ran. A quarter
    from the next local day must stay out."""
    store = MemoryEngineStore()
    figure = GRAINED.figure("team_person.quarter_volume")
    reading = GRAINED.reading("team_person.quarter_throughput")
    assert figure is not None and reading is not None

    tenant = "t1"
    await store.set_pointer(
        tenant, figure.name, Pointer(version=figure.version, settings_fingerprint="")
    )
    await store.set_buckets(tenant, "work_issue.by_quarter", "w1", ["p1@2025-08-24T23:45"])
    for subject, value in (
        ("p1@2025-08-24T23:45", 4.0),  # the anchor day's final quarter: bucket 1
        ("p1@2025-08-24T22:15", 3.0),  # bucket 7, the far edge: in
        ("p1@2025-08-24T22:00", 8.0),  # bucket 8: out
        ("p1@2025-08-25T00:00", 9.0),  # the next local day: out
    ):
        await store.save(tenant, figure.name, figure.version, subject, value, (), "P One")

    result = await serve_reading(store, GRAINED, tenant, reading, [7], at_day="2025-08-24"
    )

    assert isinstance(result.state, Ok)
    window = result.subjects[0].windows[0]
    assert (window.frm, window.to) == ("2025-08-24T22:15", "2025-08-24T23:45")
    assert window.total == 7.0, "a quarter at the anchored window's edge leaked or fell out"
    assert window.series is not None and len(window.series) == 7
    assert window.series[0] == 3.0 and window.series[-1] == 4.0


# ------------------------------------------------------------ bucket spans --
#
# A window is a span of stored buckets counted back from the anchor: `31-60`
# is the thirty days before the trailing thirty, `1-4h` the last four hours.
# The spec is an argument -- it narrows which stored buckets take part and
# may never change the calculation -- so every declared rule (floor, band,
# series) applies per span unchanged.


async def _day_reading_store(figure, days):  # type: ignore[no-untyped-def]
    store = MemoryEngineStore()
    await store.set_pointer(
        "t1", figure.name, Pointer(version=figure.version, settings_fingerprint="")
    )
    await store.set_buckets("t1", "work_issue.by_day", "w1", ["p1@2025-08-24"])
    for subject, values in days:
        await store.save("t1", figure.name, figure.version, subject, values, (), "P One")
    return store


async def _quarter_store(figure, buckets):  # type: ignore[no-untyped-def]
    store = MemoryEngineStore()
    await store.set_pointer(
        "t1", figure.name, Pointer(version=figure.version, settings_fingerprint="")
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

    result = await serve_reading(store, READINGS, "t1", reading, ["1-3", "4-6"], at_day="2025-08-24"
    )
    near, far = result.subjects[0].windows
    assert (near.frm, near.to) == ("2025-08-22", "2025-08-24")
    assert (far.frm, far.to) == ("2025-08-19", "2025-08-21")
    assert near.mean == 15.0, "the near bucket must hold the 22nd and the 24th alone"
    assert far.mean == 60.0, "the far bucket must hold the 19th and the 21st alone"
    assert (near.span, near.bucket, near.trailing) == ("3", "day", 3)
    assert (far.span, far.bucket, far.trailing) == ("4-6", "day", None)
    assert far.buckets_requested == 3


async def test_a_bare_number_and_its_explicit_span_are_one_window() -> None:
    """`3` and `1-3` are two spellings of one question and must serve
    byte-identically -- canonical spelling included, so a client cannot see
    two shapes for one answer. Served as two requests, because one request
    naming both is refused as the duplicate it is."""
    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None
    store = await _day_reading_store(
        figure, (("p1@2025-08-24", [10.0]), ("p1@2025-08-22", [20.0]))
    )
    served = []
    for spelling in (3, "1-3"):
        result = await serve_reading(store, READINGS, "t1", reading, [spelling], at_day="2025-08-24"
        )
        served.append(result.subjects[0].windows[0])
    bare, explicit = served
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
    result = await serve_reading(store, READINGS, "t1", reading, ["1-3", "4-6"], at_day="2025-08-24"
    )
    near, far = result.subjects[0].windows
    assert near.unmet == [] and near.mean == 172_800.0 and near.level == "ok"
    assert far.unmet == ["needs at least 3 values; there is 1"]
    assert far.mean is None and far.worst is None
    assert far.level == "unknown"


async def test_a_bare_number_is_positions_in_the_figures_own_sequence() -> None:
    """The pinned model: a bucket is an integer in order, so a bare `2` over
    a quarter-hour figure is the last TWO QUARTER-HOURS -- never two days.
    The unit lives in the group clause, hashed; regrading the figure is a
    new declaration with a new hash, so the old worry (a regrade silently
    re-scaling bookmarked spans under one citation) cannot arise."""
    figure = GRAINED.figure("team_person.quarter_volume")
    reading = GRAINED.reading("team_person.quarter_throughput")
    assert figure is not None and reading is not None
    store = await _quarter_store(
        figure,
        (
            ("p1@2025-08-24T05:00", 2.0),  # the anchor quarter: in
            ("p1@2025-08-24T04:45", 3.0),  # position 2: in
            ("p1@2025-08-24T04:30", 5.0),  # position 3: out
        ),
    )
    at = 1_756_036_800_000.0  # exactly 05:00 in Los Angeles
    result = await serve_reading(store, GRAINED, "t1", reading, [2], at_ms=at, facts=LA)
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-08-24T04:45", "2025-08-24T05:00")
    assert (window.span, window.bucket, window.trailing) == ("2", "15 minutes", None)
    assert window.total == 5.0, "a bare 2 must cover exactly two stored buckets"


async def test_unit_suffixed_window_tokens_are_refused_at_the_serving_door() -> None:
    """The v0.12 tokens are retired, not reinterpreted: `1-48h` parsing as
    48 buckets would silently re-scale the question. The refusal points at
    the declarations, where the unit now lives."""
    from uratori.windows import WindowError

    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None
    store = await _day_reading_store(figure, ())
    for token in ("1-48h", "1-90m", "30d"):
        with pytest.raises(WindowError, match="retired"):
            await serve_reading(store, READINGS, "t1", reading, [token], at_day="2025-08-24"
            )


async def test_a_calendar_edge_anchor_answers_at_every_grain_and_span_shape() -> None:
    """The overflow this guards has moved twice: `end_of_day_ms` was fixed,
    then the traceback reappeared one frame later in `_labels_between`,
    which steps its cursor past the last label with no `date.max` guard --
    for a day range ending 9999-12-31 and for a sub-day label range on that
    day alike. An anchor at the calendar's far edge must answer, empty
    windows and all, at both grains."""
    figure = READINGS.figure("team_person.per_day")
    pace = READINGS.reading("team_person.pace")
    assert figure is not None and pace is not None
    store = await _day_reading_store(figure, ())
    result = await serve_reading(store, READINGS, "t1", pace, [7], at_day="9999-12-31"
    )
    assert result.empty is not None
    assert result.empty.windows[0].to == "9999-12-31"

    quarter_figure = GRAINED.figure("team_person.quarter_volume")
    throughput = GRAINED.reading("team_person.quarter_throughput")
    assert quarter_figure is not None and throughput is not None
    grained_store = await _quarter_store(quarter_figure, ())
    for windows in (["2-4"], [7]):
        result = await serve_reading(grained_store, GRAINED, "t1", throughput, windows, at_day="9999-12-31"
        )
        assert result.empty is not None
        far = result.empty.windows[0].to
        assert far is not None and far.startswith("9999-12-31")

    # Third time in the same place. `ordinal_weekday_day` found its candidate
    # by adding days to the first of the month, so December 9999 -- whose
    # fifth Monday would be 10000-01-03 -- raised OverflowError instead of
    # answering "no such day". OverflowError is an ArithmeticError, which no
    # route catches, so a wire-reachable anchor produced a 500 rather than an
    # empty window. The write path hit the same line through `selected_day`,
    # so a record stamped that month crashed a pass.
    assert ordinal_weekday_day(9999, 12, 5, 0) is None, (
        "December 9999 has four Mondays, so the answer is None -- not a raise"
    )
    ordinal_reading = GRAINED.reading("team_person.fifth_monday_shipped")
    ordinal_figure = GRAINED.figure("team_person.fifth_monday_volume")
    assert ordinal_reading is not None and ordinal_figure is not None
    ordinal_store = await _figure_store(ordinal_figure, "work_issue.by_fifth_monday", ())
    answer = await serve_reading(ordinal_store, GRAINED, "t1", ordinal_reading, [3], at_day="9999-12-31"
    )
    assert answer.empty is not None
    assert answer.empty.windows[0].bucket == "fifth monday of month"


def _far_edge_ms() -> float:
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    return _dt(9999, 12, 31, 12, tzinfo=_UTC).timestamp() * 1000.0


async def test_a_record_stamped_at_the_calendar_edge_buckets_without_crashing() -> None:
    """The same overflow on the *write* path: `selected_day` asks
    `ordinal_weekday_day` about the record's own month, so a fact timestamped
    in December 9999 crashed the pass that filed it -- before any window was
    ever asked for. It must simply land in no bucket, which is what a
    selective rule means for a day that is not the rule's day."""
    for rule in (
        "first monday of month",
        "fifth monday of month",
        "fifth sunday of month",
        "fifth tuesday of month",
        "fifth saturday of month",
    ):
        assert selected_day(_far_edge_ms(), "UTC", rule) is None, rule
        assert selected_day(_far_edge_ms(), None, rule) is None, rule


async def test_a_minute_figure_serves_its_minutes_bucket_for_bucket() -> None:
    """The finest grain, end to end: `90` over a minute-grain figure is the
    last ninety stored minutes, both edges inclusive -- the 03:31 bucket is
    bucket 90 and in, the 03:29 bucket is out."""
    figure = GRAINED.figure("team_person.minute_lead")
    reading = GRAINED.reading("team_person.minute_typical")
    assert figure is not None and reading is not None
    store = MemoryEngineStore()
    await store.set_pointer(
        "t1", figure.name, Pointer(version=figure.version, settings_fingerprint="")
    )
    await store.set_buckets("t1", "work_issue.by_minute", "w1", ["p1@2025-08-24T04:45"])
    for subject, values in (
        ("p1@2025-08-24T04:45", [100.0]),  # in
        ("p1@2025-08-24T03:31", [300.0]),  # the oldest minute of the span: in
        ("p1@2025-08-24T03:29", [900.0]),  # two minutes too early: out
    ):
        await store.save("t1", figure.name, figure.version, subject, values, (), "P One")

    at = 1_756_036_800_000.0  # 05:00 in Los Angeles
    result = await serve_reading(store, GRAINED, "t1", reading, [90], at_ms=at, facts=LA)
    [window] = result.subjects[0].windows
    assert (window.frm, window.to) == ("2025-08-24T03:31", "2025-08-24T05:00")
    assert (window.span, window.bucket, window.trailing) == ("90", "minute", None)
    assert window.median == 200.0


async def test_a_duplicate_span_in_one_request_is_refused_across_spellings() -> None:
    """The bundle grammar's rule at the request door: the same span twice is
    the same answer twice, and a repeatable parameter with no duplicate
    check is a cost multiplier the reach ceiling cannot see. Canonical, so
    `30` and `1-30` collide the way they hash."""
    from uratori.windows import WindowError

    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None
    store = await _day_reading_store(figure, ())
    with pytest.raises(WindowError, match="twice"):
        await serve_reading(store, READINGS, "t1", reading, ["30", "1-30"])


async def test_the_fetch_reaches_no_further_than_the_spans_it_serves() -> None:
    """An offset span (`391-400`) ends far behind the anchor; fetching up to
    the anchor anyway would walk the whole offset -- the very cost the reach
    ceiling bounds -- to serve a ten-day window. The fetch's bounds are the
    resolved spans' own."""
    figure = READINGS.figure("team_person.per_day")
    reading = READINGS.reading("team_person.pace")
    assert figure is not None and reading is not None
    store = await _day_reading_store(figure, (("p1@2025-08-24", [10.0]),))

    asked: list[tuple[str, str]] = []
    original = store.values_in_range

    async def recording(tenant, name, version, frm, to):  # type: ignore[no-untyped-def]
        asked.append((frm, to))
        return await original(tenant, name, version, frm, to)

    store.values_in_range = recording  # type: ignore[method-assign]
    await serve_reading(store, READINGS, "t1", reading, ["391-400"], at_day="2025-08-24"
    )
    [(frm, to)] = asked
    assert frm == "2024-07-21"
    assert to == "2024-07-30", (
        "the fetch walked past the span's newest day toward the anchor -- "
        "the whole offset paid to serve a ten-day window"
    )


async def test_the_serving_door_refuses_a_reach_past_the_horizon_too() -> None:
    """Two ceilings guard a span, and only one of them fires at the request
    door for a coarse rule.

    `MAX_BUCKETS` catches anything past 3,660 positions whatever the rule, so
    it is what refuses a runaway day span. But 3,000 monthly buckets is two
    and a half centuries and sits comfortably *inside* it -- only the
    rule-aware reach ceiling knows that, and only the figure's declaration
    knows the rule. `check.py` claims "one shared implementation, so the
    tile's build error and the route's 422 speak the same words"; until this
    test the route half could be deleted whole and the suite stayed green,
    because every other reach test routes through the compile-time twin.
    """
    from uratori.windows import WindowError

    figure = GRAINED.figure("team_person.monthly_lead")
    reading = GRAINED.reading("team_person.monthly_pace")
    assert figure is not None and reading is not None
    store = await _figure_store(figure, "work_issue.by_month", (("p1@2026-08", [86_400.0]),))

    with pytest.raises(WindowError) as refused:
        await serve_reading(store, GRAINED, "t1", reading, ["3000"], at_day="2026-08-15"
        )
    assert "3660" in str(refused.value)
    assert "month" in str(refused.value), (
        "the refusal must name the rule it converted through, or the number "
        "reads as arbitrary against a span of three thousand"
    )

    # Inside the horizon, the same span shape answers -- so this is the
    # ceiling talking and not the coarse rule being refused outright.
    answer = await serve_reading(store, GRAINED, "t1", reading, ["118"], at_day="2026-08-15"
    )
    assert answer.subjects[0].windows[0].span == "118"


async def test_a_week_window_walks_iso_weeks_across_the_year_edge() -> None:
    """The week rule, served end to end -- the one grain that had resolution
    and label tests but no figure behind them anywhere.

    The anchor is the last day of 2025, whose ISO week is `2026-W01`: bucket 1
    carries a label a calendar year ahead of the anchor's own year, which is
    the shape a week sequence has to get right and the one a naive
    `{day.year}-W{week}` gets wrong.
    """
    figure = GRAINED.figure("team_person.weekly_lead")
    reading = GRAINED.reading("team_person.weekly_pace")
    assert figure is not None and reading is not None
    store = await _figure_store(
        figure,
        "work_issue.by_week",
        (
            ("p1@2026-W01", [86_400.0]),
            ("p1@2025-W52", [172_800.0]),
        ),
    )

    answer = await serve_reading(store, GRAINED, "t1", reading, ["each:1-2"], at_day="2025-12-31"
    )
    this_week, last_week = answer.subjects[0].windows
    assert (this_week.bucket, this_week.frm, this_week.to) == ("week", "2026-W01", "2026-W01")
    assert this_week.median == 86_400.0
    assert (last_week.frm, last_week.to) == ("2025-W52", "2025-W52")
    assert last_week.median == 172_800.0
    # `trailing` stays null: this is not a count of days, and a "2" here
    # would read as one on any screen keying off it.
    assert this_week.trailing is None
    # And the contiguous rule carries no bucket list -- the edges say it all.
    assert this_week.buckets is None


async def test_a_span_that_resolves_to_no_bucket_says_so_rather_than_empty_strings() -> None:
    """Where the calendar runs out there is no bucket to name, and the window
    must say that rather than carry `""` in a date field.

    An empty string is a value: it renders, it sorts, and on the page it went
    through a template literal that turned `null` into the word "null" -- a
    date-shaped nothing where rule 3 wants a stated absence. Reachable only at
    year 1, but this is the field a reader trusts to say which buckets an
    answer covered.
    """
    figure = GRAINED.figure("team_person.monthly_lead")
    reading = GRAINED.reading("team_person.monthly_pace")
    assert figure is not None and reading is not None
    store = await _figure_store(figure, "work_issue.by_month", (("p1@2026-08", [86_400.0]),))

    answer = await serve_reading(store, GRAINED, "t1", reading, ["2-5"], at_day="0001-01-15"
    )
    window = (answer.empty or answer.subjects[0]).windows[0]
    assert window.frm is None and window.to is None, (
        'no bucket resolved, so there is no label to report -- "" would be a '
        "value where this is an absence"
    )
    assert window.buckets_requested == 0
    assert window.buckets_covered == 0

    # The control: one month later there *is* a bucket, and the edges are it.
    # Without this the assertion above would also pass on a resolver that had
    # simply stopped resolving anything.
    reachable = await serve_reading(store, GRAINED, "t1", reading, ["1-2"], at_day="0001-03-15"
    )
    edges = (reachable.empty or reachable.subjects[0]).windows[0]
    assert (edges.frm, edges.to) == ("0001-02", "0001-03")

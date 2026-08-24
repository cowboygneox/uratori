"""The language, and every rule it enforces.

Each refusal in `check.py` exists because the alternative *compiles, runs and
produces a plausible number*. So each test here asserts the refusal and, in its
name, says what the mistake would have done -- because a test called
`test_rejects_bad_input` teaches nobody why the rule is there, and the next
person to find the rule inconvenient deletes it.

Every test that asserts a refusal is paired with a control asserting the
near-identical *correct* definition compiles. Without the control, a checker
that refused everything would pass this file.
"""

from __future__ import annotations

import pytest

from uratori.lang.ast import ByPredicate
from uratori.lang.check import CheckError
from uratori.lang.lex import SyntaxError_

from .world import compile_source

# A minimal preamble every fixture can build on.
BASE = """
index work_issue.assigned_to from assigneeAccountId through team_person.accounts.accountId
index work_issue.active where active == true
index work_issue.sized where estimateSeconds is set
index work_issue.stuck where statusChangedAt older than thresholds.longWipDays
index work_issue.in_container from containerId
index code_change.open where state == "open"
index code_change.by_source from connectionId
index code_change.authored_in from (authorAccountId through team_person.accounts.accountId, connectionId)
index code_change.merged_by_day from (authorAccountId through team_person.accounts.accountId, mergedAt by day in tenant.timezone)
index code_review_request.asked_of from reviewerAccountId through team_person.accounts.accountId
index code_review_request.pending where pending == true
index work_issue.delivered_by_day from (assigneeAccountId through team_person.accounts.accountId, completedAt by day in tenant.timezone)

measure code_change.open_seconds = mergedAt - createdAt
measure work_issue.estimate = estimateSeconds in effort
measure work_issue.moved = moment updatedAt
measure code_review_request.waiting_seconds = now - requestedAt

figure team_person.wip:
    \"\"\"In progress.\"\"\"
    display "{team_person} wip"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)

figure team_person.time_to_merge:
    \"\"\"Time to merge.\"\"\"
    display "{team_person} to merge"
    depends:
        merged = code_change.merged_by_day:{team_person}
    calculate:
        list(code_change.open_seconds over merged)
"""


def compile_ok(extra: str = ""):
    return compile_source(BASE + extra)


def refuses(extra: str, *fragments: str) -> str:
    with pytest.raises(CheckError) as caught:
        compile_source(BASE + extra)
    message = caught.value.message
    for fragment in fragments:
        assert fragment in message, f"expected {fragment!r} in {message!r}"
    return message


# ------------------------------------------------------------- baseline --


def test_the_base_library_compiles() -> None:
    """The control for every refusal below. Without it a checker that rejected
    everything would pass this file."""
    lib = compile_ok()
    assert lib.figure("team_person.wip") is not None
    assert lib.figure("team_person.time_to_merge") is not None


# ------------------------------------------------------------------ lex --


def test_a_hash_inside_a_string_is_not_a_comment() -> None:
    """Naive comment stripping would truncate the label and still compile, so
    the sentence would simply be cut in half on screen."""
    lib = compile_source(BASE + '\nindex code_change.hashed where title == "fix #12"\n')
    spec = lib.indexes["code_change.hashed"].spec
    assert isinstance(spec, ByPredicate)
    assert spec.value == "fix #12"


def test_an_unclosed_docstring_is_a_syntax_error() -> None:
    with pytest.raises(SyntaxError_):
        compile_source('figure a.b:\n    """unterminated\n')


def test_indentation_that_matches_no_block_is_refused_rather_than_guessed() -> None:
    """Guessing turns a typo into a silently different block."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            "figure work_issue.a:\n"
            "        display \"x\"\n"
            "     unit share\n"
        )
    assert "does not match any enclosing block" in caught.value.message


# ---------------------------------------------------------------- kinds --


def test_a_figure_over_a_kind_nobody_stores_is_refused_with_the_list() -> None:
    refuses(
        """
figure nonsense_thing.count:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.active
    calculate:
        count(m)
""",
        "is not a fact kind",
    )


# ---------------------------------------------------------------- index --


def test_an_index_may_only_name_a_bucket_setting() -> None:
    """Moving one re-buckets a tenant's whole history, which is why the list is
    short and closed."""
    refuses(
        "\nindex code_change.late where mergedAt older than flow.leadTimeDays\n",
        "not a setting an index may name",
    )


def test_the_control_an_age_index_over_a_bucket_setting_compiles() -> None:
    compile_ok("\nindex code_change.late where mergedAt older than thresholds.staleChangeDays\n")


def test_two_indexes_may_not_disagree_about_a_kinds_id_space() -> None:
    """Otherwise the guard is defeated by writing a second index and leaving the
    clause off, which is the quietest possible way to lose it."""
    refuses(
        """
index code_review.a keyed as code_change where wasApproved == true
index code_review.b keyed as work_issue where wasApproved == false
""",
        "has one id space",
    )


# --------------------------------------------------------------- figure --


def test_a_figure_with_no_scope_index_has_no_subjects() -> None:
    """It would compute one number, for nobody, and render as a board-wide
    total attributed to no one."""
    refuses(
        """
figure team_person.loose:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.active
    calculate:
        count(m)
""",
        "no subjects",
    )


def test_a_field_index_read_without_a_bucket_reads_zero_for_everybody() -> None:
    refuses(
        """
figure team_person.unbucketed:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.assigned_to
    calculate:
        count(m)
""",
        "needs a bucket",
    )


def test_a_predicate_index_cannot_be_addressed_per_subject() -> None:
    """Its bucket is keyed by the empty string, so it would simply miss."""
    refuses(
        """
figure team_person.bad:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.active:{team_person}
    calculate:
        count(m)
""",
        "single bucket",
    )


def test_a_figure_may_not_have_both_depends_and_combine() -> None:
    refuses(
        """
figure team_person.mixed:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.assigned_to:{team_person}
    combine:
        w = team_person.wip
    calculate:
        count(m)
""",
        "two populations",
    )


def test_a_pair_index_without_across_is_refused_because_every_reader_is_silently_wrong() -> None:
    refuses(
        """
figure team_person.pairs:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(m)
""",
        "does not say what the second part is",
    )


def test_the_control_the_same_figure_with_across_compiles() -> None:
    lib = compile_ok(
        """
figure team_person.pairs across data_connection:
    \"\"\"d\"\"\"
    display "{team_person} in {data_connection}"
    depends:
        m = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(m)
"""
    )
    plan = lib.figure("team_person.pairs")
    assert plan is not None and plan.across == "data_connection"


def test_a_day_may_not_be_a_dimension() -> None:
    """A day has no roster and no name, and whether a figure is day-keyed is
    what decides if a reading may roll it up over a range."""
    refuses(
        """
figure team_person.days across data_connection:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)
""",
        "A day is not a dimension",
    )


def test_a_bare_read_of_a_dimensioned_figure_would_take_whichever_part_sorted_first() -> None:
    refuses(
        """
figure team_person.pairs across data_connection:
    \"\"\"d\"\"\"
    display "{team_person} in {data_connection}"
    depends:
        m = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(m)

figure team_person.total:
    \"\"\"d\"\"\"
    display "x"
    combine:
        s = team_person.pairs
    calculate:
        s
""",
        "whichever part sorted first",
    )


def test_a_rollup_of_an_undimensioned_figure_totals_one_value_and_looks_right() -> None:
    refuses(
        """
figure team_person.total:
    \"\"\"d\"\"\"
    display "x"
    combine:
        s = team_person.wip over data_connection
    calculate:
        sum(s)
""",
        "not split across anything",
    )


def test_a_rollups_depth_is_deeper_than_its_parts_so_a_cold_build_orders_them() -> None:
    """Declaration order is not dependency order. On a cold build the wrong
    order stores a nought for everybody and never revisits it."""
    lib = compile_ok(
        """
figure team_person.pairs across data_connection:
    \"\"\"d\"\"\"
    display "{team_person} in {data_connection}"
    depends:
        m = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(m)

figure team_person.total:
    \"\"\"d\"\"\"
    display "x"
    combine:
        s = team_person.pairs over data_connection
    calculate:
        sum(s)
"""
    )
    parts = lib.figure("team_person.pairs")
    total = lib.figure("team_person.total")
    assert parts is not None and total is not None
    assert total.depth > parts.depth


def test_a_figure_may_not_read_one_declared_after_it() -> None:
    """A cycle has no line number, so it is refused at the point the name
    cannot be resolved."""
    refuses(
        """
figure team_person.first:
    \"\"\"d\"\"\"
    display "x"
    combine:
        s = team_person.second
    calculate:
        s

figure team_person.second:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.assigned_to:{team_person}
    calculate:
        count(m)
""",
        "there is no figure called",
    )


def test_a_figure_may_not_read_itself() -> None:
    refuses(
        """
figure team_person.loop:
    \"\"\"d\"\"\"
    display "x"
    combine:
        s = team_person.loop
    calculate:
        s
""",
        "there is no figure called",
    )


# ------------------------------------------------------- clock and time --


def test_a_figure_may_not_list_a_clock_measure() -> None:
    """A stored value computed from the clock is stale the instant it is written
    and nothing would ever move it: every number would be real exactly once."""
    refuses(
        """
figure team_person.waits:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_review_request.asked_of:{team_person}
    calculate:
        list(code_review_request.waiting_seconds over m)
""",
        "measured to now",
    )


def test_a_figure_may_not_measure_a_span_of_days() -> None:
    refuses(
        """
figure team_person.span:
    \"\"\"d\"\"\"
    display "x"
    unit days
    depends:
        m = work_issue.assigned_to:{team_person}
    calculate:
        days from createdAt to now
""",
        "reads the clock",
    )


def test_an_age_index_is_allowed_because_membership_crosses_a_line_once() -> None:
    """The control for the two above: a clock *measure* decays continuously, and
    membership does not -- it changes on a knowable day, and until it does the
    answer is unchanged."""
    lib = compile_ok(
        """
figure team_person.stuck_count:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.assigned_to:{team_person} & work_issue.stuck
    calculate:
        count(m)
"""
    )
    assert lib.figure("team_person.stuck_count") is not None


# ------------------------------------------------------- units and kinds --


def test_arithmetic_must_declare_a_unit_because_the_same_operands_mean_two_things() -> None:
    refuses(
        """
figure team_person.ratio:
    \"\"\"d\"\"\"
    display "x"
    combine:
        a = team_person.wip
        b = team_person.wip
    calculate:
        a / b
""",
        "produces a number nothing can name",
    )


def test_a_count_may_not_declare_a_unit_because_a_second_place_is_a_place_to_disagree() -> None:
    refuses(
        """
figure team_person.counted:
    \"\"\"d\"\"\"
    display "x"
    unit count
    depends:
        m = work_issue.assigned_to:{team_person}
    calculate:
        count(m)
""",
        "already says what the number is",
    )


def test_a_ladder_must_return_a_word_not_a_number() -> None:
    """A numeric ladder would carry an absence out under a numeric unit, where
    nothing downstream can hold it."""
    refuses(
        """
figure team_person.numeric_ladder:
    \"\"\"d\"\"\"
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= 5 then 1
        otherwise 0
""",
        "returns a number from its when ladder",
    )


def test_a_ladder_may_not_mix_words_and_numbers() -> None:
    refuses(
        """
figure team_person.mixed_ladder:
    \"\"\"d\"\"\"
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= 5 then "over"
        otherwise 0
""",
        "a number from one branch and a word from another",
    )


def test_a_ladder_must_end_in_otherwise() -> None:
    """Falling off the end and stopping on an unknown both render as a dash, and
    only one of them is a claim the definition makes."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + """
figure team_person.open_ladder:
    \"\"\"d\"\"\"
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= 5 then "over"
"""
        )
    assert "otherwise" in caught.value.message


def test_a_word_may_not_be_combined_arithmetically() -> None:
    refuses(
        """
figure team_person.level:
    \"\"\"d\"\"\"
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= 5 then "over"
        otherwise "ok"

figure team_person.uses_level:
    \"\"\"d\"\"\"
    display "x"
    unit count
    combine:
        l = team_person.level
    calculate:
        l + 1
""",
        "stores a word rather than a number",
    )


def test_a_field_measure_may_not_be_listed_and_a_duration_may_not_be_totalled() -> None:
    """Two halves of one rule: a list is the evidence behind a span, and a field
    total is a sum."""
    refuses(
        """
figure team_person.listed_field:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.assigned_to:{team_person}
    calculate:
        list(work_issue.estimate over m)
""",
        "reads a field rather than measuring",
    )
    refuses(
        """
figure team_person.summed_duration:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        sum(code_change.open_seconds over m)
""",
        "seconds between two moments",
    )


def test_an_extreme_needs_a_moment_measure() -> None:
    """Otherwise it is a general maximum over a column, which is a construct no
    definition has asked for."""
    refuses(
        """
figure work_container.biggest:
    \"\"\"d\"\"\"
    display "x"
    depends:
        c = work_issue.in_container:{work_container}
    calculate:
        latest(work_issue.estimate over c)
""",
        "measures a quantity rather than naming an instant",
    )


def test_both_extreme_directions_exist_and_produce_a_moment() -> None:
    """v1 shipped `latest` alone because the two directions disagree about an
    unreadable timestamp. Both ship here, so the disagreement is a test rather
    than a paragraph."""
    lib = compile_ok(
        """
figure work_container.last_moved:
    \"\"\"d\"\"\"
    display "x"
    depends:
        c = work_issue.in_container:{work_container}
    calculate:
        latest(work_issue.moved over c)

figure work_container.first_moved:
    \"\"\"d\"\"\"
    display "x"
    depends:
        c = work_issue.in_container:{work_container}
    calculate:
        earliest(work_issue.moved over c)
"""
    )
    for name in ("work_container.last_moved", "work_container.first_moved"):
        plan = lib.figure(name)
        assert plan is not None and plan.unit == "moment"


def test_a_figure_may_only_name_a_figure_setting() -> None:
    refuses(
        """
figure team_person.banded:
    \"\"\"d\"\"\"
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= flow.leadTimeDays then "over"
        otherwise "ok"
""",
        "not a setting a calculation may name",
    )


# -------------------------------------------------------------- reading --


def test_a_reading_may_not_read_a_reading() -> None:
    """Composing them is how a team number becomes a mean of means, weighting
    each person equally instead of each record."""
    refuses(
        """
reading team_person.first(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)

reading team_person.second(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.first in range
    calculate:
        mean(m)
""",
        "a reading may only read a figure",
    )


def test_a_windowed_reading_needs_a_day_keyed_source() -> None:
    refuses(
        """
reading team_person.nope(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.wip in range
    calculate:
        mean(m)
""",
        "not day-keyed",
    )


def test_a_mean_over_daily_counts_is_a_mean_per_day_wearing_the_wrong_label() -> None:
    refuses(
        """
figure team_person.merges:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)

reading team_person.per_day(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.merges in range
    calculate:
        mean(m)
""",
        "per *day* wearing a label that says per record",
    )


def test_the_control_a_sum_over_daily_counts_is_allowed() -> None:
    """`sum` says which of the two readings was meant, which is exactly what
    made counts readable at all."""
    lib = compile_ok(
        """
figure team_person.merges:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)

reading team_person.shipped(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.merges in range
    calculate:
        sum(m)
"""
    )
    assert lib.reading("team_person.shipped") is not None


def test_a_sum_may_not_sit_beside_a_distribution() -> None:
    """Two numbers a reader can divide produce a third that no definition
    claims."""
    refuses(
        """
reading team_person.both(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        sum(m)
        mean(m)
""",
        "a third that no definition claims",
    )


# -------------------------------------------------------- minimum sample --


def test_an_unwritten_minimum_is_one_value_rather_than_no_floor() -> None:
    """Without the default, a distribution over an empty window returns silent
    nulls -- a dash with no written reason, when every absence must say why.

    All three distribution statistics trigger it, each asserted alone: a filter
    narrowed to `mean` would leave a worst-only reading dashing silently."""
    lib = compile_ok(
        """
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)

reading team_person.slowest(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        worst(m)

reading team_person.typical(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        median(m)
"""
    )
    for name in ("to_merge", "slowest", "typical"):
        plan = lib.reading(f"team_person.{name}")
        assert plan is not None
        assert [(r.count, r.set) for r in plan.requires] == [(1, "m")], name


def test_the_default_minimum_hashes_like_a_written_one() -> None:
    """Applied at read time instead, two engines could render the same version
    differently -- the definition's meaning would live outside its hash."""
    bare = compile_ok(
        """
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
"""
    ).reading("team_person.to_merge")
    written = compile_ok(
        """
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    requires:
        at least 1 values in m
    calculate:
        mean(m)
"""
    ).reading("team_person.to_merge")
    assert bare is not None and written is not None
    assert bare.version == written.version


def test_the_floor_is_in_the_version_because_a_looser_one_reads_differently() -> None:
    """The control for the test above, which would pass vacuously if
    requirements fell out of the hash entirely -- `at least 1` and `at least 3`
    sharing a version is two different definitions citing identically."""

    def floored(count: int):
        return compile_ok(
            f"""
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    requires:
        at least {count} values in m
    calculate:
        mean(m)
"""
        ).reading("team_person.to_merge")

    one, three = floored(1), floored(3)
    assert one is not None and three is not None
    assert one.version != three.version


def test_a_written_minimum_overrides_the_default_rather_than_stacking() -> None:
    lib = compile_ok(
        """
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    requires:
        at least 3 values in m
    calculate:
        mean(m)
"""
    )
    plan = lib.reading("team_person.to_merge")
    assert plan is not None
    assert [(r.count, r.set) for r in plan.requires] == [(3, "m")]


def test_a_sum_reading_takes_no_default_minimum() -> None:
    """Injected here, a person who shipped nothing would read unmet instead of
    nought -- and a sum of nothing is nought, deliberately."""
    lib = compile_ok(
        """
figure team_person.merges:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)

reading team_person.shipped(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.merges in range
    calculate:
        sum(m)
"""
    )
    plan = lib.reading("team_person.shipped")
    assert plan is not None
    assert plan.requires == ()


def test_a_live_reading_takes_no_default_minimum() -> None:
    """An empty queue is a real reading of nought pending, not a shortfall."""
    lib = compile_ok(
        """
reading team_person.queue():
    \"\"\"d\"\"\"
    display "x"
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)
        worst(w)
"""
    )
    plan = lib.reading("team_person.queue")
    assert plan is not None
    assert plan.requires == ()


def test_a_live_reading_takes_no_argument() -> None:
    """Written (range) it would accept a window, ignore it, and return today's
    answer under a heading saying thirty days."""
    refuses(
        """
reading team_person.queue(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)
""",
        "takes no arguments",
    )


def test_a_windowed_reading_must_declare_range() -> None:
    refuses(
        """
reading team_person.nowindow():
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
""",
        "must declare (range)",
    )


def test_count_is_live_only_because_a_windowed_sample_already_reports_it() -> None:
    refuses(
        """
reading team_person.counted(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        count(m)
""",
        "already reported as the sample",
    )


def test_a_live_reading_needs_exactly_one_scope_bucketed_index() -> None:
    """With none there are no subjects at all, and the empty case -- what
    somebody with nothing looks like -- would hold the whole board's answer."""
    refuses(
        """
reading team_person.unscoped():
    \"\"\"d\"\"\"
    display "x"
    depends:
        w = code_review_request.waiting_seconds over code_review_request.pending
    calculate:
        count(w)
""",
        "fanned out by 0 indexes",
    )


def test_a_band_on_a_count_may_not_be_written_in_a_time_unit() -> None:
    """Left to the duration path a count of 3 becomes 3/86400 against a
    threshold in days, and every queue on every board bands good for ever."""
    refuses(
        """
reading team_person.queue():
    \"\"\"d\"\"\"
    display "x"
    band low on count against flow.pendingReviews in minutes
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)
""",
        "has no time in it",
    )


def test_a_band_may_only_colour_a_statistic_the_reading_calculates() -> None:
    refuses(
        """
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    band low on median against flow.reviewLatencyDays
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
""",
        "which it does not calculate",
    )


def test_an_effort_figure_may_not_be_read_over_a_range() -> None:
    """Every renderer on the reading path branches on count or duration, so an
    effort would be banded as wall-clock and printed as raw seconds."""
    refuses(
        """
figure team_person.effort_by_day:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.delivered_by_day:{team_person}
    calculate:
        sum(work_issue.estimate over m)

reading team_person.effort(range):
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = team_person.effort_by_day in range
    calculate:
        sum(m)
""",
        "measured in effort",
    )


# ----------------------------------------------------------- projection --

PROJECTION = """
projection work_issue.item:
    \"\"\"One row per issue.\"\"\"
    field:
        key = key as text
        status_changed = statusChangedAt as date
        active = active as flag
    value:
        age_days in days = days from status_changed to now
        stuck in count =
            when active == 0 then 0
            when age_days >= thresholds.longWipDays then 1
            otherwise 0
    flag issue-long-wip when stuck == 1:
        label "Stuck {age_days}"
        detail "Has not changed status in {age_days}."
        severity attention
    sort by age_days descending
    limit 300
"""


def test_a_projection_compiles_and_carries_its_flag() -> None:
    lib = compile_ok(PROJECTION)
    plan = lib.projection("work_issue.item")
    assert plan is not None
    assert [f.name for f in plan.flags] == ["issue-long-wip"]


def test_the_old_project_keyword_says_what_to_write_instead() -> None:
    """`project` read as an imperative where every other keyword here names the
    thing declared. Renamed -- and the old spelling is refused *by name*, because
    falling through to "expected a declaration" points at a line whose problem is
    one word and does not say which."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(BASE + PROJECTION.replace("projection ", "project "))
    assert 'the keyword is "projection"' in caught.value.message


def test_a_projection_is_still_refused_when_it_is_not_a_keyword_at_all() -> None:
    """The control. A parser that answered the rename message for anything
    unrecognised would pass the test above while telling somebody who typed
    `projct` to write `projection` -- true, unhelpful, and hiding the typo."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(BASE + PROJECTION.replace("projection ", "projekt "))
    assert 'expected "index"' in caught.value.message
    assert '"projection"' in caught.value.message


def test_a_projection_may_not_aggregate() -> None:
    """Those are figures, and offering them here would be a second way to
    compute a number this product claims has exactly one."""
    refuses(
        """
projection work_issue.counting:
    \"\"\"d\"\"\"
    field:
        key = key as text
    value:
        n in count = count(everything)
""",
        "aggregates nothing",
    )


def test_a_span_needs_two_declared_moments() -> None:
    """`as date` is what says a field holds an instant; without it the binding
    is not in the moment namespace and the span would read a value that is not
    there."""
    refuses(
        """
projection work_issue.spanless:
    \"\"\"d\"\"\"
    field:
        opened = createdAt as text
    value:
        age in days = days from opened to now
""",
        "is not a moment",
    )


def test_a_limit_without_a_sort_returns_an_arbitrary_subset_that_looks_complete() -> None:
    refuses(
        """
projection work_issue.capped:
    \"\"\"d\"\"\"
    field:
        key = key as text
    limit 10
""",
        "does not say in what order",
    )


def test_a_flag_placeholder_naming_nothing_would_print_the_word_undefined() -> None:
    refuses(
        """
projection work_issue.badflag:
    \"\"\"d\"\"\"
    field:
        key = key as text
    flag issue-bad when key is something:
        label "{nope}"
        detail "d"
        severity info
""",
        "which nothing here binds",
    )


def test_a_projection_may_not_read_a_figure_scoped_to_another_kind() -> None:
    """Every row would be looked up under an id from another space and find
    nothing -- a column of dashes, on every row, for ever."""
    refuses(
        """
projection work_issue.wrong:
    \"\"\"d\"\"\"
    field:
        key = key as text
    read:
        w = team_person.wip
""",
        "another space",
    )


def test_a_join_is_hashed_because_it_decides_which_record_a_path_is_read_off() -> None:
    a = compile_ok(
        """
projection work_issue.joined:
    \"\"\"d\"\"\"
    field:
        key = key as text
        epic_start = startDate from containerId through work_container.id as text
"""
    ).projection("work_issue.joined")
    b = compile_ok(
        """
projection work_issue.joined:
    \"\"\"d\"\"\"
    field:
        key = key as text
        epic_start = startDate as text
"""
    ).projection("work_issue.joined")
    assert a is not None and b is not None
    assert a.version != b.version


# ------------------------------------------------------------- summary --

SUMMARY = (
    PROJECTION
    + """
summarise work_issue.backlog over work_issue.item:
    \"\"\"The backlog, in one row.\"\"\"
    count items
    count items_stuck where stuck == 1
    total days_waiting in days = age_days where stuck == 1
    value:
        verdict =
            when items_stuck > 0 then "attention"
            otherwise "clear"
    flag backlog-stuck when items_stuck > 0:
        label "Stuck work"
        detail "{items_stuck} {items_stuck|item is:items are} sat still"
        severity attention
"""
)


def test_a_summary_compiles_over_its_projection() -> None:
    lib = compile_ok(SUMMARY)
    plan = lib.summary("work_issue.backlog")
    assert plan is not None
    assert [n for n, _ in plan.counts] == ["items", "items_stuck"]


def test_a_summary_hashes_its_projections_version() -> None:
    """Rename what a row value means and every count moves, so a version that
    did not follow would claim nothing had changed."""
    first = compile_ok(SUMMARY).summary("work_issue.backlog")
    second = compile_ok(SUMMARY.replace("age_days >= thresholds.longWipDays", "age_days >= 99")).summary(
        "work_issue.backlog"
    )
    assert first is not None and second is not None
    assert first.version != second.version


def test_a_summary_may_not_shadow_a_row_value() -> None:
    """One word would otherwise mean one row in one line and the whole
    population in the next, whichever way it resolved."""
    refuses(
        PROJECTION
        + """
summarise work_issue.shadow over work_issue.item:
    \"\"\"d\"\"\"
    count stuck
""",
        "already a value of",
    )


def test_a_summary_total_may_only_add_up_a_number() -> None:
    refuses(
        PROJECTION
        + """
summarise work_issue.badtotal over work_issue.item:
    \"\"\"d\"\"\"
    total keys in count = key
""",
        "Only a number may be summed",
    )


def test_a_summary_value_may_not_read_a_row_value() -> None:
    """A row value is one number per record and the summary holds hundreds, so
    a name that resolves per row has no single value here."""
    refuses(
        PROJECTION
        + """
summarise work_issue.leaky over work_issue.item:
    \"\"\"d\"\"\"
    count items
    value:
        wrong in count = age_days + 1
""",
        "which nothing binds",
    )


def test_a_summary_needs_a_projection_that_exists() -> None:
    refuses(
        """
summarise work_issue.orphan over work_issue.nothing:
    \"\"\"d\"\"\"
    count items
""",
        "there is no projection called",
    )


# ------------------------------------------------------------ namespace --


def test_two_declarations_may_not_share_a_name() -> None:
    """A citation is name@version, so two definitions under one name make it
    ambiguous -- and the Data screen addresses a definition by name alone."""
    refuses(
        """
figure team_person.wip:
    \"\"\"d\"\"\"
    display "x"
    depends:
        m = work_issue.assigned_to:{team_person}
    calculate:
        count(m)
""",
        "already a figure",
    )


# -------------------------------------------------------------- version --


def test_prose_does_not_move_a_version() -> None:
    """Fixing a typo in a docstring must not fork a version and recompute three
    hundred values."""
    first = compile_ok().figure("team_person.wip")
    second = compile_source(BASE.replace("In progress.", "In progress, reworded.")).figure(
        "team_person.wip"
    )
    assert first is not None and second is not None
    assert first.version == second.version


def test_an_index_definition_moves_the_version_of_every_figure_that_reads_it() -> None:
    """Changing what an index *means* changes what the figure counts, even
    though the figure's own text is untouched."""
    first = compile_ok().figure("team_person.wip")
    second = compile_source(
        BASE.replace('index work_issue.active where active == true', 'index work_issue.active where active == false')
    ).figure("team_person.wip")
    assert first is not None and second is not None
    assert first.version != second.version


def test_an_index_label_does_not_move_a_version() -> None:
    """The control for the test above: a label is prose."""
    first = compile_ok().figure("team_person.wip")
    second = compile_source(
        BASE.replace(
            "index work_issue.active where active == true",
            'index work_issue.active where active == true label "underway"',
        )
    ).figure("team_person.wip")
    assert first is not None and second is not None
    assert first.version == second.version


def test_a_rollup_hashes_its_sources_version() -> None:
    """Redefine the parts and the total must rebuild too, or it reads a number
    derived from a definition that no longer exists, for ever, with the
    corrected parts printed underneath it."""
    body = """
figure team_person.pairs across data_connection:
    \"\"\"d\"\"\"
    display "{team_person} in {data_connection}"
    depends:
        m = code_change.authored_in:{team_person} & code_change.OPENSTATE
    calculate:
        count(m)

figure team_person.total:
    \"\"\"d\"\"\"
    display "x"
    combine:
        s = team_person.pairs over data_connection
    calculate:
        sum(s)
"""
    a = compile_ok(body.replace("OPENSTATE", "open")).figure("team_person.total")
    b = compile_ok(body.replace("OPENSTATE", "open - code_change.open")).figure(
        "team_person.total"
    )
    assert a is not None and b is not None
    assert a.version != b.version


def test_a_band_unit_is_hashed_because_the_same_numbers_read_differently() -> None:
    """The same threshold in minutes is a band 1,440x tighter, and a colour
    change under a version claiming nothing moved is the one thing a
    content-addressed version must not do."""
    body = """
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    band low against flow.reviewLatencyDaysUNIT
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
"""
    a = compile_ok(body.replace("UNIT", "")).reading("team_person.to_merge")
    b = compile_ok(body.replace("UNIT", " in hours")).reading("team_person.to_merge")
    assert a is not None and b is not None
    assert a.version != b.version


def test_days_written_out_is_the_same_definition_as_days_left_unwritten() -> None:
    """The control: `days` hashes as absent, so a band written before the
    keyword existed keeps its version."""
    body = """
reading team_person.to_merge(range):
    \"\"\"d\"\"\"
    display "x"
    band low against flow.reviewLatencyDaysUNIT
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
"""
    a = compile_ok(body.replace("UNIT", "")).reading("team_person.to_merge")
    b = compile_ok(body.replace("UNIT", " in days")).reading("team_person.to_merge")
    assert a is not None and b is not None
    assert a.version == b.version


# ------------------------------------------------------------------ band --
#
# A band was a figure of its own for one release -- a `level`-unit figure
# combining the one below it -- and the board found the pair by scanning the
# library at serve time. So the word on screen came from a definition the page
# never named, and the page showing the formula did not contain it. Reported as
# *"where the hell is band coming from and why isn't it in the figure
# definition"*.
#
# Folding it in is only an improvement if it cannot quietly become a second
# calculation inside the first. Each refusal below compiles as something
# plausible without it.

BANDED = """
figure team_person.banded:
    \"\"\"A count with a band.\"\"\"
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)
    band:
        when value >= thresholds.wip.over then "over"
        otherwise "ok"
"""


def test_a_figure_carries_its_own_band() -> None:
    lib = compile_ok(BANDED)
    plan = lib.figure("team_person.banded")
    assert plan is not None
    assert plan.band is not None
    # Still a count. The band is a second answer about the number, not a
    # change to what the number is -- a unit of `level` here would tell every
    # renderer downstream to stop formatting it as a quantity.
    assert plan.unit == "count"
    assert plan.band_settings == ("thresholds.wip.over",)


def test_a_band_reads_nothing_but_the_figures_own_value() -> None:
    """`mine` is a set this figure genuinely has. Reading it here would make the
    band a second calculation over the same population, sharing the first one's
    name and version and appearing on screen as a word in a Band column."""
    refuses(
        BANDED.replace("when value >=", "when mine >="),
        "The only thing in scope here",
    )


def test_a_band_must_answer_a_word() -> None:
    """A rung answering a number is a second figure written inside the first,
    and it reaches a screen as an unexplained integer in a column of words."""
    refuses(BANDED.replace('then "over"', "then 3"), "must answer a word")


def test_a_band_may_only_name_a_settings_dial_a_calculation_may() -> None:
    refuses(
        BANDED.replace("thresholds.wip.over", "flow.leadTimeDays"),
        "not a setting a calculation may name",
    )


def test_a_word_cannot_be_banded() -> None:
    """There is nothing to compare a word against, so every subject would fall
    through to the bottom rung -- a whole board banded comfortable, silently."""
    refuses(
        """
figure team_person.worded:
    \"\"\"d\"\"\"
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= 5 then "over"
        otherwise "ok"
    band:
        when value == "over" then "over"
        otherwise "ok"
""",
        "cannot be banded",
    )


def test_a_list_figure_cannot_be_banded() -> None:
    """A list has no single value for a rung to compare, so `band_of` answers
    nothing for every row -- a whole board of measured values chipped
    "unknown", under `banded: true`, which reads as data trouble rather than
    an authoring mistake. The old guard checked the *unit*, and a list figure's
    unit is the unit of its members ("duration"), so it never fired."""
    refuses(
        """
figure team_person.listed:
    \"\"\"d\"\"\"
    display "x"
    depends:
        merged = code_change.merged_by_day:{team_person}
    calculate:
        list(code_change.open_seconds over merged)
    band:
        when value >= 3600 then "slow"
        otherwise "ok"
""",
        "cannot be banded",
    )


def test_the_band_is_in_the_version_and_costs_nothing_to_change() -> None:
    """A figure that starts banding differently is a different definition: the
    band is one of the answers it gives. It is free to move -- a band is
    evaluated on read and stored nowhere, so a new version invalidates nothing.
    """
    plain = compile_ok(BANDED.split("    band:")[0]).figure("team_person.banded")
    banded = compile_ok(BANDED).figure("team_person.banded")
    moved = compile_ok(BANDED.replace('otherwise "ok"', 'otherwise "fine"')).figure(
        "team_person.banded"
    )
    assert plain is not None and banded is not None and moved is not None
    assert plain.version != banded.version
    assert banded.version != moved.version


def test_a_figure_written_before_bands_existed_keeps_its_version() -> None:
    """The control on the rule above. Hashed absent-unless-declared, so adding
    the construct rebuilt nothing on any board -- if this ever fails, a release
    silently recomputed every value on every tenant."""
    lib = compile_ok()
    plan = lib.figure("team_person.wip")
    assert plan is not None
    assert plan.band is None
    # The hash of a figure with no band must not mention one at all.
    assert plan.version == compile_ok().figure("team_person.wip").version


def test_a_projection_may_bind_a_bands_word_beside_its_number() -> None:
    lib = compile_ok(
        BANDED
        + """
projection team_person.card:
    \"\"\"d\"\"\"
    field:
        name = display_name as text
    read:
        n = team_person.banded
        b = band of team_person.banded
    value:
        score in count =
            when b == "over" then 2
            otherwise 0
"""
    )
    plan = lib.projection("team_person.card")
    assert plan is not None
    assert ("n", "team_person.banded", "count", False) in plan.reads
    assert ("b", "team_person.banded", "level", True) in plan.reads


def test_a_projection_may_not_bind_the_band_of_a_figure_that_has_none() -> None:
    """Every row would bind nothing under that name, so every rung testing it
    would stop and every flag gated on it would never fire -- a column of dashes
    and a page that is silently short."""
    refuses(
        """
projection team_person.card:
    \"\"\"d\"\"\"
    field:
        name = display_name as text
    read:
        b = band of team_person.wip
""",
        "declares no band",
    )


def test_every_compile_refusal_is_a_definition_error() -> None:
    """The public contract for an embedding host: one except clause catches a
    definition that cannot load, whichever layer refused it. Without a shared
    base, a host guards its boot path with `except CheckError` and a missing
    colon -- which fails one layer earlier, in the parser -- crashes straight
    through the guard it thought it had."""
    # Imported from the package root deliberately: the *public* surface is
    # what a host writes its except clause against.
    from uratori import DefinitionError

    with pytest.raises(DefinitionError) as syntax:
        compile_source("this is not a definition\n")
    assert isinstance(syntax.value, SyntaxError_)
    assert "line 1" in str(syntax.value)

    with pytest.raises(DefinitionError) as meaning:
        compile_source("index nothing.here from nowhere\n")
    assert isinstance(meaning.value, CheckError)

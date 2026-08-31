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
group work_issue.assigned_to from assigneeAccountId through team_person.accounts.accountId
filter work_issue.active where active == true
filter work_issue.sized where estimateSeconds is set
filter work_issue.stuck where statusChangedAt older than 14 days
group work_issue.in_container from containerId
filter code_change.open where state == "open"
group code_change.by_source from connectionId
group code_change.authored_in from (authorAccountId through team_person.accounts.accountId, connectionId)
group code_change.merged_by_day from (authorAccountId through team_person.accounts.accountId, mergedAt by day)
group code_review_request.asked_of from reviewerAccountId through team_person.accounts.accountId
filter code_review_request.pending where pending == true
group work_issue.delivered_by_day from (assigneeAccountId through team_person.accounts.accountId, completedAt by day)

measure code_change.open_seconds = mergedAt - createdAt
measure work_issue.estimate = estimateSeconds in effort
measure work_issue.moved = moment updatedAt
measure code_review_request.waiting_seconds = now - requestedAt

# In progress.
figure team_person.wip:
    display "{team_person} wip"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)

# Time to merge.
figure team_person.time_to_merge bucketed:
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
    lib = compile_source(BASE + '\nfilter code_change.hashed where title == "fix #12"\n')
    spec = lib.indexes["code_change.hashed"].spec
    assert isinstance(spec, ByPredicate)
    assert spec.value == "fix #12"


def test_even_an_unterminated_docstring_is_refused_with_directions() -> None:
    """Half-typed old syntax must not fall through to "unclosed string": the
    refusal fires at the opening quotes, so termination never matters."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source('figure a.b:\n    """unterminated\n')
    assert "not inside the block" in caught.value.message


def test_indentation_that_matches_no_block_is_refused_rather_than_guessed() -> None:
    """Guessing turns a typo into a silently different block."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            "figure work_issue.a:\n"
            "        display \"x\"\n"
            "     unit share\n"
        )
    assert "does not match any enclosing block" in caught.value.message


# ----------------------------------------------------------- vocabulary --


def test_index_is_refused_by_name_with_both_replacements() -> None:
    """One keyword covered two different questions -- fanning records out by a
    field, and narrowing to the records matching a test. A `.fig` written
    against the old spelling should say what to change, not fail as "expected
    a declaration" -- so the assertion pins the pointer's own words, which the
    generic expected-a-declaration fallback (quoting both keywords too) would
    not satisfy."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source("index work_issue.active where active == true\n")
    assert '"index" split into "group" and "filter"' in caught.value.message
    assert "`group code_change.by_author from ...`" in caught.value.message
    assert "`filter code_change.open where ...`" in caught.value.message


def test_index_is_refused_by_name_after_other_declarations_too() -> None:
    """The pointer must fire wherever the old keyword appears, not only at the
    top of the file -- mid-document the parser arrives here through a
    different path, after dedents and newlines."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(BASE + "\nindex work_issue.late where active == true\n")
    assert '"index" split into "group" and "filter"' in caught.value.message


def test_a_group_that_narrows_is_refused_and_pointed_at_filter() -> None:
    """`group x where ...` would compile to a single bucket wearing a keyword
    that says it fans out, and every later reader trusts the keyword. The
    assertion pins the direction of the advice: both messages mention both
    keywords, so `"filter" in message` alone would pass with the advice
    written backwards."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source("group work_issue.broken where active == true\n")
    assert "write `filter work_issue.broken where ...`" in caught.value.message


def test_a_filter_that_fans_out_is_refused_and_pointed_at_group() -> None:
    """The mirror image: `filter x from ...` would bucket by every value of
    the field under a keyword that promises one bucket."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source("filter work_issue.broken from containerId\n")
    assert "write `group work_issue.broken from ...`" in caught.value.message


def test_the_cross_shape_pointer_carries_a_keyed_as_along() -> None:
    """Following the advice must not silently drop the id-space claim -- a
    filter rewritten without its `keyed as` compiles and then intersects as
    the wrong id space, which is the empty-set failure the clause exists to
    prevent."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source("group code_review.broken keyed as code_change where a == true\n")
    assert (
        "write `filter code_review.broken keyed as code_change where ...`"
        in caught.value.message
    )


def test_a_group_with_no_tail_is_refused_expecting_from() -> None:
    with pytest.raises(SyntaxError_) as caught:
        compile_source("group work_issue.broken containerId\n")
    assert 'expected "from" after the group name' in caught.value.message


def test_a_filter_with_no_tail_is_refused_expecting_where() -> None:
    with pytest.raises(SyntaxError_) as caught:
        compile_source("filter work_issue.broken active == true\n")
    assert 'expected "where" after the filter name' in caught.value.message


def test_each_shape_compiles_under_its_own_keyword() -> None:
    """The control for the refusals above: the same declarations, each under
    the right word, produce the shapes `index` used to -- `keyed as` and
    `label` included, since both ride on the shared production."""
    lib = compile_ok(
        '\ngroup code_review.extra_by keyed as code_change from authorAccountId label "by"'
        '\nfilter work_issue.extra_open where active == true label "open"\n'
    )
    assert lib.indexes["code_review.extra_by"].bucketed
    assert lib.indexes["code_review.extra_by"].id_space == "code_change"
    assert lib.indexes["code_review.extra_by"].label == "by"
    assert not lib.indexes["work_issue.extra_open"].bucketed
    assert lib.indexes["work_issue.extra_open"].label == "open"


def test_the_rename_left_every_version_where_it_was() -> None:
    """The keyword split is vocabulary only, so it must not move a single
    stored value. These literals were computed on the tree before the split,
    when the same specs were written `index` -- between them they hash a
    filter, a group with a `through` hop, and a composite with `by day in` a
    zone, so a change to any part of the compiled spec fails here as a moved
    version, which in production is every tenant's history orphaned."""
    lib = compile_ok()
    assert lib.figure("team_person.wip").version == "d7fe57cb385c"
    assert lib.figure("team_person.time_to_merge").version == "5b655b06ef70"


def test_a_groups_checker_messages_speak_the_group_word() -> None:
    """`_decl_word`'s two branches are messages-only, so nothing structural
    fails if a group is reported as a filter -- only the author's vocabulary.
    The filter word is pinned by the age-setting refusal above; this pins the
    group word through the zone refusal."""
    refuses(
        "\ngroup code_change.by_bad_day from (authorAccountId, mergedAt by day in flow.leadTimeDays)\n",
        "not a fact kind",
    )


def test_a_groups_comment_is_served_as_its_prose() -> None:
    """`declaration_prose` misses and no-prose both answer the empty string,
    so only a group that *has* a comment can prove the source scan recognises
    the `group` header -- if it still looked for `index`, the explanation
    would silently vanish from the definition-source route."""
    from uratori.lang.source import declaration_prose, declaration_source

    lib = compile_source(
        BASE.replace(
            "group work_issue.in_container from containerId",
            "# Which container holds it.\ngroup work_issue.in_container from containerId",
        )
    )
    assert declaration_prose(lib, "work_issue.in_container") == "Which container holds it."
    assert declaration_source(lib, "work_issue.in_container") is not None


# ---------------------------------------------------------------- kinds --


def test_a_figure_over_a_kind_nobody_stores_is_refused_with_the_list() -> None:
    refuses(
        """
# d
figure nonsense_thing.count:
    display "x"
    depends:
        m = work_issue.active
    calculate:
        count(m)
""",
        "is not a fact kind",
    )


# ------------------------------------------------------------- group/filter --


def test_an_age_filter_may_not_name_a_dial() -> None:
    """The hardest position to take a dial out of: a filter runs over records
    before anything buckets them by subject, so there is no subject whose goal
    figure could be looked up. The refusal carries both answers -- read the
    threshold off the record's owner, or write the days here."""
    with pytest.raises(SyntaxError_) as caught:
        compile_ok("\nfilter code_change.late where mergedAt older than flow.leadTimeDays\n")
    assert "tenant dial" in str(caught.value) and "owner" in str(caught.value)


def test_the_control_an_age_filter_over_a_written_threshold_compiles() -> None:
    compile_ok("\nfilter code_change.late where mergedAt older than 3 days\n")


def test_two_declarations_may_not_disagree_about_a_kinds_id_space() -> None:
    """Otherwise the guard is defeated by writing a second index and leaving the
    clause off, which is the quietest possible way to lose it."""
    refuses(
        """
filter code_review.a keyed as code_change where wasApproved == true
filter code_review.b keyed as work_issue where wasApproved == false
""",
        "has one id space",
    )


# --------------------------------------------------------------- figure --


def test_a_figure_with_no_scope_index_has_no_subjects() -> None:
    """It would compute one number, for nobody, and render as a board-wide
    total attributed to no one."""
    refuses(
        """
# d
figure team_person.loose:
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
# d
figure team_person.unbucketed:
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
# d
figure team_person.bad:
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
# d
figure team_person.mixed:
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
# d
figure team_person.pairs:
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
# d
figure team_person.pairs across data_connection:
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
# d
figure team_person.days across data_connection:
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)
""",
        "A bucket of time is not a dimension",
    )


def test_a_time_bucket_may_not_be_a_groups_first_part() -> None:
    """The first part of a group names the subject -- who or what the figure
    is about. A time bucket there fans the figure out by date instead, and a
    day has no roster and no name, so there is nothing for the figure to be
    *of*.

    Both bucket kinds, because the rule reads the truncation and the
    selective rule separately and only the first half was ever asserted: the
    check could be deleted whole and the suite stayed green.
    """
    for rule in ("by day", "by month", "by first monday of month"):
        message = refuses(
            f"group code_change.wrong_way from (mergedAt {rule}, authorAccountId)\n"
            """
# d
figure team_person.upside_down:
    display "x"
    depends:
        m = code_change.wrong_way:{team_person}
    calculate:
        count(m)
""",
            "fans",
            "no roster and no name",
        )
        assert rule.removeprefix("by ") in message, (
            "the refusal must name the rule that did it, so an author with a "
            "two-part group knows which part to move"
        )


def test_a_bare_read_of_a_dimensioned_figure_would_take_whichever_part_sorted_first() -> None:
    refuses(
        """
# d
figure team_person.pairs across data_connection:
    display "{team_person} in {data_connection}"
    depends:
        m = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(m)

# d
figure team_person.total:
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
# d
figure team_person.total:
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
# d
figure team_person.pairs across data_connection:
    display "{team_person} in {data_connection}"
    depends:
        m = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(m)

# d
figure team_person.total:
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
# d
figure team_person.first:
    display "x"
    combine:
        s = team_person.second
    calculate:
        s

# d
figure team_person.second:
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
# d
figure team_person.loop:
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
# d
figure team_person.waits:
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
# d
figure team_person.span:
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
# d
figure team_person.stuck_count:
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
# d
figure team_person.ratio:
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
# d
figure team_person.counted:
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
# d
figure team_person.numeric_ladder:
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
# d
figure team_person.mixed_ladder:
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
# d
figure team_person.open_ladder:
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
# d
figure team_person.level:
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= 5 then "over"
        otherwise "ok"

# d
figure team_person.uses_level:
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
# d
figure team_person.listed_field:
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
# d
figure team_person.summed_duration bucketed:
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
# d
figure work_container.biggest:
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
# d
figure work_container.last_moved:
    display "x"
    depends:
        c = work_issue.in_container:{work_container}
    calculate:
        latest(work_issue.moved over c)

# d
figure work_container.first_moved:
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
# d
figure team_person.banded:
    display "x"
    combine:
        w = team_person.wip
    calculate:
        when w >= flow.leadTimeDays then "over"
        otherwise "ok"
""",
        "tenant dial",
    )


# -------------------------------------------------------------- reading --


def test_a_reading_may_not_read_a_reading() -> None:
    """Composing them is how a team number becomes a mean of means, weighting
    each person equally instead of each record."""
    refuses(
        """
# d
reading team_person.first(range):
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)

# d
reading team_person.second(range):
    display "x"
    depends:
        m = team_person.first in range
    calculate:
        mean(m)
""",
        "a reading may only read a figure",
    )


def test_a_windowed_reading_needs_a_time_keyed_source() -> None:
    refuses(
        """
# d
reading team_person.nope(range):
    display "x"
    depends:
        m = team_person.wip in range
    calculate:
        mean(m)
""",
        "not time-keyed",
    )


def test_a_mean_over_daily_counts_is_a_mean_per_day_wearing_the_wrong_label() -> None:
    refuses(
        """
# d
figure team_person.merges bucketed:
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)

# d
reading team_person.per_day(range):
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
# d
figure team_person.merges bucketed:
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)

# d
reading team_person.shipped(range):
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
# d
reading team_person.both(range):
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
# d
reading team_person.to_merge(range):
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)

# d
reading team_person.slowest(range):
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        worst(m)

# d
reading team_person.typical(range):
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
# d
reading team_person.to_merge(range):
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
"""
    ).reading("team_person.to_merge")
    written = compile_ok(
        """
# d
reading team_person.to_merge(range):
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
# d
reading team_person.to_merge(range):
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
# d
reading team_person.to_merge(range):
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
# d
figure team_person.merges bucketed:
    display "x"
    depends:
        m = code_change.merged_by_day:{team_person}
    calculate:
        count(m)

# d
reading team_person.shipped(range):
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
# d
reading team_person.queue():
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
# d
reading team_person.queue(range):
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
# d
reading team_person.nowindow():
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
# d
reading team_person.counted(range):
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        count(m)
""",
        "already reported as the sample",
    )


def test_a_live_reading_needs_exactly_one_scope_bucketed_group() -> None:
    """With none there are no subjects at all, and the empty case -- what
    somebody with nothing looks like -- would hold the whole board's answer."""
    refuses(
        """
# d
reading team_person.unscoped():
    display "x"
    depends:
        w = code_review_request.waiting_seconds over code_review_request.pending
    calculate:
        count(w)
""",
        "fanned out by 0 groups",
    )


def test_a_live_readings_band_may_not_compare_against_a_sequence() -> None:
    """A live reading has no window, so there is nothing to reduce a sequenced
    goal over -- the comparison would have to pick a bucket, and whichever it
    picked would be a fabrication.

    This replaces the old `on count ... in minutes` refusal. That rule existed
    because a dial is a bare number: left to the duration path a count of 3
    became 3/86400 against a threshold in days, and every queue on every board
    banded good for ever. A threshold is a figure now, and it carries its own
    unit for the checker to compare, so the mistake is unwritable rather than
    refused.
    """
    refuses(
        """
# d
reading team_person.queue():
    display "x"
    band on count:
        when value > team_person.time_to_merge then "over"
        otherwise "ok"
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)
""",
        "no window",
    )


def test_a_band_may_only_colour_a_statistic_the_reading_calculates() -> None:
    refuses(
        """
# d
reading team_person.to_merge(range):
    display "x"
    band on median:
        when value > 604800 then "over"
        otherwise "ok"
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
# d
figure team_person.effort_by_day bucketed:
    display "x"
    depends:
        m = work_issue.delivered_by_day:{team_person}
    calculate:
        sum(work_issue.estimate over m)

# d
reading team_person.effort(range):
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
# One row per issue.
projection work_issue.item:
    field:
        key = key as text
        status_changed = statusChangedAt as date
        active = active as flag
    value:
        age_days in days = days from status_changed to now
        stuck in count =
            when active == 0 then 0
            when age_days >= 14 then 1
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
    assert 'expected "fact"' in caught.value.message
    assert '"projection"' in caught.value.message


def test_a_projection_may_not_aggregate() -> None:
    """Those are figures, and offering them here would be a second way to
    compute a number this product claims has exactly one."""
    refuses(
        """
# d
projection work_issue.counting:
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
# d
projection work_issue.spanless:
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
# d
projection work_issue.capped:
    field:
        key = key as text
    limit 10
""",
        "does not say in what order",
    )


OMITTED = """
# One row per change, with the parked ones off the page.
projection code_change.active_board:
    field:
        key = title as text
        parked = parked as flag
    omit when parked == 1
"""


def test_an_omit_gate_moves_the_projection_version() -> None:
    """`omit` is the definition of *on the page* as surely as `from` is, so a
    projection that starts omitting rows must cite differently -- otherwise two
    different pages carry one version on library.json, the surface a review
    reads, and the row that vanished is traceable to nothing.

    Two comparisons, deliberately. Gated-vs-gateless alone would pass a hash
    that recorded only the gate's *presence* -- under which `parked == 1` and
    `parked == 0` cite identically, and inverting the entire page ships with
    an unmoved version. The second comparison is the one that pins the
    condition itself."""
    gated = compile_ok(OMITTED).projection("code_change.active_board")
    control = compile_ok(
        OMITTED.replace("    omit when parked == 1\n", "")
    ).projection("code_change.active_board")
    inverted = compile_ok(
        OMITTED.replace("omit when parked == 1", "omit when parked == 0")
    ).projection("code_change.active_board")
    assert gated is not None and control is not None and inverted is not None
    assert gated.omit is not None
    assert gated.version != control.version
    assert gated.version != inverted.version


def test_an_omit_naming_nothing_would_judge_every_row_by_a_missing_value() -> None:
    refuses(
        """
# d
projection work_issue.ghosted:
    field:
        key = key as text
    omit when ghost == 1
""",
        '"ghost", which nothing binds',
    )


def test_an_omit_reading_a_dial_registers_it_and_an_unknown_dial_is_refused() -> None:
    """The gate's condition may compare against a tenant dial, and the dial
    has to land in the plan's settings the way a value's or a flag's would --
    an unregistered dial would evaluate to nothing, the gate would never
    fire, and a narrowing everyone can read on library.json would silently
    not happen: the exact class of lie the checker exists for."""
    lib = compile_ok(
        """
# d
projection work_issue.aged:
    field:
        status_changed = statusChangedAt as date
    value:
        age_days in days = days from status_changed to now
    omit when age_days >= 14
"""
    )
    plan = lib.projection("work_issue.aged")
    assert plan is not None
    assert plan.settings == (), (
        "a projection reads no dials at all now -- its thresholds are figures "
        "bound with `read:`, or numbers written where the reader can see them"
    )

    refuses(
        """
# d
projection work_issue.misdialed:
    field:
        status_changed = statusChangedAt as date
    value:
        age_days in days = days from status_changed to now
    omit when age_days >= thresholds.longWipDays
""",
        "tenant dial",
    )


def test_a_second_omit_would_leave_two_gates_racing_for_the_page() -> None:
    """Two conditions that must both hold are a `value` that is a ladder,
    tested by its word -- the same answer the flag grammar gives."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + OMITTED
            + "    omit when parked == 0\n"
        )
    assert 'more than one "omit"' in caught.value.message


def test_a_flag_placeholder_naming_nothing_would_print_the_word_undefined() -> None:
    refuses(
        """
# d
projection work_issue.badflag:
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
# d
projection work_issue.wrong:
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
# d
projection work_issue.joined:
    field:
        key = key as text
        epic_start = startDate from containerId through work_container.id as text
"""
    ).projection("work_issue.joined")
    b = compile_ok(
        """
# d
projection work_issue.joined:
    field:
        key = key as text
        epic_start = startDate as text
"""
    ).projection("work_issue.joined")
    assert a is not None and b is not None
    assert a.version != b.version


def test_a_projection_may_declare_the_population_it_is_over() -> None:
    """`from` is the definition of "on the page", written in the projection
    where a reader can check it. The plan must carry both the expression and
    the indexes it reads, because the engine loads exactly those buckets to
    resolve the population."""
    lib = compile_ok(
        PROJECTION.replace(
            "    field:", "    from work_issue.active | work_issue.sized\n    field:", 1
        )
    )
    plan = lib.projection("work_issue.item")
    assert plan is not None
    assert plan.frm is not None
    assert set(plan.indexes) == {"work_issue.active", "work_issue.sized"}


def test_a_projection_population_may_only_name_a_filter() -> None:
    """A projection has no depends block, so a bare name in `from` can only be
    a typo -- and resolving it to the empty set would empty the page while
    looking like a complete one."""
    refuses(
        PROJECTION.replace("    field:", "    from live\n    field:", 1),
        "not a declared filter",
    )


def test_a_projection_population_needs_a_filter_that_exists() -> None:
    refuses(
        PROJECTION.replace("    field:", "    from work_issue.imaginary\n    field:", 1),
        "no group or filter called",
    )


def test_a_projection_population_has_no_row_to_scope_a_bucket_by() -> None:
    """`from` decides which records become rows, so at the moment it is
    resolved there is no row whose id a scoped bucket could be read under."""
    refuses(
        PROJECTION.replace(
            "    field:", "    from work_issue.in_container:{work_container}\n    field:", 1
        ),
        "no row to scope",
    )


def test_a_projection_population_refuses_a_fan_out_index() -> None:
    """Read whole, a fan-out index looks up a bucket keyed by the empty string
    and finds nothing -- an empty page that looks like a complete one."""
    refuses(
        PROJECTION.replace("    field:", "    from work_issue.in_container\n    field:", 1),
        "single bucket",
    )


def test_a_projection_population_must_hold_ids_of_its_own_kind() -> None:
    """Ids from another space match no record of this kind, so every row would
    be filtered away and the page would be empty for ever, with nothing
    thrown."""
    refuses(
        PROJECTION.replace("    field:", "    from code_change.open\n    field:", 1),
        "whose members are",
    )


def test_a_population_refusal_reaches_the_right_operand_of_a_set_op() -> None:
    """The walker must descend both sides: checked left-only, `a | wrong`
    compiles, the union resolves against ids of another space, and the page
    is empty while looking complete -- the exact failure class every arm
    exists to refuse."""
    refuses(
        PROJECTION.replace(
            "    field:", "    from work_issue.active | code_change.open\n    field:", 1
        ),
        "whose members are",
    )


def test_a_projection_population_refuses_an_age_filter() -> None:
    """An age bucket is resolved against the clock at reindex time, so a
    population read from one is as stale as the last reconcile -- and no
    pointer covers an index only a `from` reads, so moving the dial it names
    would change nothing until the next full sync."""
    refuses(
        PROJECTION.replace("    field:", "    from work_issue.stuck\n    field:", 1),
        "narrows by age against the clock",
    )


# ------------------------------------------------------------- summary --

SUMMARY = (
    PROJECTION
    + """
# The backlog, in one row.
summarise work_issue.backlog over work_issue.item:
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
    second = compile_ok(SUMMARY.replace("age_days >= 14", "age_days >= 99")).summary(
        "work_issue.backlog"
    )
    assert first is not None and second is not None
    assert first.version != second.version


def test_a_projection_with_no_population_keeps_its_historic_version() -> None:
    """The `from_indexes` key is dropped from the hash when there is no
    `from` (`canonical` drops None-valued keys), so every projection and
    summary written before populations existed keeps its version -- pinned as
    literals, because the alternative is every deployed library.json moving
    for no semantic reason and nothing here noticing.

    The pins moved once, deliberately: this fixture used to read
    `thresholds.longWipDays` and now writes the number, which is a change to
    what the definition says and so a change to what it hashes. That is the
    pin doing its job rather than failing at it."""
    lib = compile_ok(SUMMARY)
    item = lib.projection("work_issue.item")
    backlog = lib.summary("work_issue.backlog")
    assert item is not None and backlog is not None
    assert item.version == "309bfe9aceb7", "the pre-population hash of PROJECTION"
    assert backlog.version == "cbf1b637abfa", "the pre-population hash of SUMMARY"


def test_a_projection_population_is_part_of_its_version() -> None:
    """Which records get a row is what the projection *means*, and the summary
    follows through its projection's version because every count is over the
    population."""
    first = compile_ok(SUMMARY)
    second = compile_ok(
        SUMMARY.replace("    field:", "    from work_issue.active\n    field:", 1)
    )
    for name, fp, sp in (
        ("work_issue.item", first.projection("work_issue.item"), second.projection("work_issue.item")),
        ("work_issue.backlog", first.summary("work_issue.backlog"), second.summary("work_issue.backlog")),
    ):
        assert fp is not None and sp is not None
        assert fp.version != sp.version, f"{name} kept its version across a population change"


def test_redefining_an_index_a_population_reads_moves_the_projections_version() -> None:
    """Hashed by name alone, an index redefinition would change which records
    get a row while library.json -- the review surface -- showed untouched
    projection and summary versions, and the wire claimed nothing had moved.
    A live reading hashes its index specs for exactly this reason; the
    population does the same."""
    with_from = SUMMARY.replace("    field:", "    from work_issue.active\n    field:", 1)
    first = compile_source(BASE + with_from)
    second = compile_source(
        BASE.replace(
            "filter work_issue.active where active == true",
            "filter work_issue.active where active == false",
        )
        + with_from
    )
    for name, fp, sp in (
        ("work_issue.item", first.projection("work_issue.item"), second.projection("work_issue.item")),
        ("work_issue.backlog", first.summary("work_issue.backlog"), second.summary("work_issue.backlog")),
    ):
        assert fp is not None and sp is not None
        assert fp.version != sp.version, (
            f"{name} kept its version while the population's index changed meaning"
        )


def test_a_population_indexes_label_does_not_move_a_version() -> None:
    """The control: a label is prose."""
    with_from = SUMMARY.replace("    field:", "    from work_issue.active\n    field:", 1)
    first = compile_source(BASE + with_from).projection("work_issue.item")
    second = compile_source(
        BASE.replace(
            "filter work_issue.active where active == true",
            'filter work_issue.active where active == true label "underway"',
        )
        + with_from
    ).projection("work_issue.item")
    assert first is not None and second is not None
    assert first.version == second.version


def test_a_summary_may_not_shadow_a_row_value() -> None:
    """One word would otherwise mean one row in one line and the whole
    population in the next, whichever way it resolved."""
    refuses(
        PROJECTION
        + """
# d
summarise work_issue.shadow over work_issue.item:
    count stuck
""",
        "already a value of",
    )


def test_a_summary_total_may_only_add_up_a_number() -> None:
    refuses(
        PROJECTION
        + """
# d
summarise work_issue.badtotal over work_issue.item:
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
# d
summarise work_issue.leaky over work_issue.item:
    count items
    value:
        wrong in count = age_days + 1
""",
        "which nothing binds",
    )


def test_a_summary_needs_a_projection_that_exists() -> None:
    refuses(
        """
# d
summarise work_issue.orphan over work_issue.nothing:
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
# d
figure team_person.wip:
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
    """Fixing a typo in an explanation must not fork a version and recompute
    three hundred values."""
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
        BASE.replace('filter work_issue.active where active == true', 'filter work_issue.active where active == false')
    ).figure("team_person.wip")
    assert first is not None and second is not None
    assert first.version != second.version


def test_an_index_label_does_not_move_a_version() -> None:
    """The control for the test above: a label is prose."""
    first = compile_ok().figure("team_person.wip")
    second = compile_source(
        BASE.replace(
            "filter work_issue.active where active == true",
            'filter work_issue.active where active == true label "underway"',
        )
    ).figure("team_person.wip")
    assert first is not None and second is not None
    assert first.version == second.version


def test_a_rollup_hashes_its_sources_version() -> None:
    """Redefine the parts and the total must rebuild too, or it reads a number
    derived from a definition that no longer exists, for ever, with the
    corrected parts printed underneath it."""
    body = """
# d
figure team_person.pairs across data_connection:
    display "{team_person} in {data_connection}"
    depends:
        m = code_change.authored_in:{team_person} & code_change.OPENSTATE
    calculate:
        count(m)

# d
figure team_person.total:
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


def test_a_readings_band_ladder_is_hashed() -> None:
    """A reading that starts banding differently is a different definition.

    This used to be a test about the band's *unit* -- the same threshold in
    minutes was a band 1,440 times tighter, under a version claiming nothing
    moved. The unit existed because a dial is a bare number that does not know
    what it measures; a ladder compares against a figure or a literal written
    in the reading's own terms, so there is no unit left to get wrong, and what
    is hashed is the comparison itself.
    """
    body = """
# d
reading team_person.to_merge(range):
    display "x"
    band:
        when value > THRESHOLD then "over"
        otherwise "ok"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
"""
    a = compile_ok(body.replace("THRESHOLD", "604800")).reading("team_person.to_merge")
    b = compile_ok(body.replace("THRESHOLD", "86400")).reading("team_person.to_merge")
    assert a is not None and b is not None
    assert a.version != b.version


def test_the_default_statistic_written_out_is_the_same_definition() -> None:
    """The control: `mean` is what an unwritten `on` means, so writing it out
    is the same definition and every reading banded before `on` existed keeps
    its version."""
    body = """
# d
reading team_person.to_merge(range):
    display "x"
    band ON:
        when value > 604800 then "over"
        otherwise "ok"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
"""
    a = compile_ok(body.replace("ON", "")).reading("team_person.to_merge")
    b = compile_ok(body.replace("ON", "on mean")).reading("team_person.to_merge")
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
# A count with a band.
figure team_person.banded:
    display "x"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)
    band:
        when value >= 5 then "over"
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
    # A literal threshold reads no figure. `band_reads` is what the serving
    # side follows to notice a goal moving, so an empty tuple here is the
    # claim that this band depends on nothing but its own value.
    assert plan.band_reads == ()


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


def test_a_band_may_not_name_a_dial() -> None:
    """A dial is a control outside the fact stream: the one number on a card
    that no evidence can explain. The refusal carries the rewrite, because an
    author staring at a working definition needs to be told what to write
    instead."""
    refuses(
        BANDED.replace("5", "thresholds.wip.over"),
        "tenant dial",
        "figure",
    )


def test_a_word_cannot_be_banded() -> None:
    """There is nothing to compare a word against, so every subject would fall
    through to the bottom rung -- a whole board banded comfortable, silently."""
    refuses(
        """
# d
figure team_person.worded:
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
# d
figure team_person.listed bucketed:
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
# d
projection team_person.card:
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
# d
projection team_person.card:
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
        compile_source("group nothing.here from nowhere\n")
    assert isinstance(meaning.value, CheckError)


# ---------------------------------------------------------------- prose --

# A declaration's explanation is written as `#` comment lines directly above
# it -- one spelling for all six kinds, and the block below the header holds
# nothing but the directives a reviewer came to check. The docstring-inside
# spelling is gone: prose buried in the block hid the directives it sat among.

EXPLAINED = """
group work_issue.assigned_to from assigneeAccountId through team_person.accounts.accountId
filter work_issue.active where active == true

# How much is in flight right now.
figure team_person.wip:
    display "{team_person} wip"

    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active

    calculate:
        count(mine)
"""


def test_the_comment_above_a_figure_is_its_customer_facing_doc() -> None:
    """The explanation is served wherever the number is cited; a parser that
    dropped it with the other comments would render every citation blank."""
    figure = compile_source(EXPLAINED).figure("team_person.wip")
    assert figure is not None
    assert figure.doc == "How much is in flight right now."


def test_a_docstring_inside_the_block_is_refused_with_directions() -> None:
    """The old spelling. Refused by name rather than left to shatter into
    string tokens, so a migrating author is told where the prose now lives
    instead of being told about an unterminated string."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            EXPLAINED.replace(
                '    display "{team_person} wip"',
                '    \"\"\"In progress.\"\"\"\n    display "{team_person} wip"',
            )
        )
    assert "not inside the block" in caught.value.message


def test_a_figure_with_no_explanation_is_refused() -> None:
    """The explanation is the customer-facing definition; a figure nobody can
    read is the thing this language exists to prevent, and it must fail at
    compile time rather than render a bare name on screen."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(EXPLAINED.replace("# How much is in flight right now.\n", ""))
    assert "team_person.wip" in caught.value.message
    assert "#" in caught.value.message


@pytest.mark.parametrize(
    ("what", "declaration"),
    [
        (
            "reading team_person.to_merge",
            """
reading team_person.to_merge(range):
    display "x"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)
""",
        ),
        (
            "projection work_issue.item",
            """
projection work_issue.item:
    field:
        key = key as text
""",
        ),
        (
            "summarise work_issue.backlog",
            """
# One row per issue.
projection work_issue.item:
    field:
        key = key as text

summarise work_issue.backlog over work_issue.item:
    count items
""",
        ),
    ],
)
def test_every_rendered_kind_requires_an_explanation(what: str, declaration: str) -> None:
    """All four rendered kinds, not just figures: each is served to a reader,
    so each unexplained one is refused naming itself. (The summarise case
    carries an explained projection, so only the summary is at fault -- and
    the refusal must be the explanation one, not a dangling reference.)"""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(BASE + declaration)
    assert what.split()[-1] in caught.value.message
    assert "no explanation" in caught.value.message


def test_the_explanation_may_sit_one_blank_line_up_but_not_two() -> None:
    """One blank line is how people naturally space a file, and v1 measured
    what refusing it cost: a third of its declarations rendered bare with the
    explanation stranded just above. Two blanks is a detached paragraph, and
    silently adopting it would attach prose the author never aimed here."""
    one = EXPLAINED.replace(
        "# How much is in flight right now.\nfigure",
        "# How much is in flight right now.\n\nfigure",
    )
    spaced = compile_source(one).figure("team_person.wip")
    assert spaced is not None
    assert spaced.doc == "How much is in flight right now."

    two = EXPLAINED.replace(
        "# How much is in flight right now.\nfigure",
        "# How much is in flight right now.\n\n\nfigure",
    )
    with pytest.raises(SyntaxError_):
        compile_source(two)


def test_a_file_banner_is_not_an_explanation() -> None:
    """A build that concatenates .fig files draws a `# ---- name.fig ----`
    rule between them; without this, the first figure of every file would
    adopt the previous file's banner as its own explanation."""
    with pytest.raises(SyntaxError_):
        compile_source(
            EXPLAINED.replace("# How much is in flight right now.", "# ---- board.fig ----")
        )


def test_comment_lines_join_as_one_explanation() -> None:
    stacked = EXPLAINED.replace(
        "# How much is in flight right now.",
        "# How much is in flight right now.\n# Only active work counts.",
    )
    figure = compile_source(stacked).figure("team_person.wip")
    assert figure is not None
    assert figure.doc == "How much is in flight right now.\nOnly active work counts."


def test_the_served_prose_and_formula_split_holds_for_comment_explanations() -> None:
    """`declaration_prose` is what a Data screen shows as the meaning and
    `declaration_source` is the formula: the explanation must land in the
    first and stay out of the second, or the reviewer reads prose where they
    came for arithmetic."""
    from uratori.lang.source import declaration_prose, declaration_source

    lib = compile_source(EXPLAINED)
    assert declaration_prose(lib, "team_person.wip") == "How much is in flight right now."
    formula = declaration_source(lib, "team_person.wip") or ""
    assert "count(mine)" in formula
    assert "How much is in flight" not in formula
    assert "display" not in formula


def test_every_rendered_kind_carries_its_comment_as_its_doc() -> None:
    """Attachment, not just refusal, for all four kinds -- with distinct texts
    so a cross-wire (every kind served the figure's prose) fails too. Also the
    control for the refusal test above: explained, all of these compile."""
    lib = compile_source(
        BASE
        + """
# Days to land a change.
reading team_person.to_merge(range):
    display "x"

    depends:
        m = team_person.time_to_merge in range

    calculate:
        mean(m)

# One row per issue.
projection work_issue.item:
    field:
        key = key as text

# The backlog, in one row.
summarise work_issue.backlog over work_issue.item:
    count items
"""
    )
    reading = lib.reading("team_person.to_merge")
    projection = lib.projection("work_issue.item")
    summary = lib.summary("work_issue.backlog")
    assert reading is not None and projection is not None and summary is not None
    assert reading.doc == "Days to land a change."
    assert projection.doc == "One row per issue."
    assert summary.doc == "The backlog, in one row."


def test_an_index_may_not_take_a_figures_name() -> None:
    """One namespace covers all six kinds, not just the four rendered ones. An
    index shadowing a figure splits the citation: the source pane, addressed
    by name alone, would serve the index's one-liner where the figure's
    formula belongs, beside prose that came from the figure."""
    refuses(
        "\nfilter team_person.wip where active == true\n",
        "team_person.wip",
        "already",
    )


def test_a_measure_may_not_take_a_figures_name_either() -> None:
    refuses(
        "\nmeasure team_person.wip = moment updatedAt\n",
        "team_person.wip",
        "already",
    )


def test_a_paragraph_break_is_a_bare_hash_not_a_blank_line() -> None:
    """`#` on its own line is the paragraph spelling. A blank LINE inside the
    run ends it -- the detached upper half is silently not prose -- and both
    halves are pinned here so a future "cross blanks anywhere" relaxation has
    to declare itself."""
    joined = EXPLAINED.replace(
        "# How much is in flight right now.",
        "# How much is in flight right now.\n#\n# Only active work counts.",
    )
    two_paragraphs = compile_source(joined).figure("team_person.wip")
    assert two_paragraphs is not None
    assert two_paragraphs.doc == "How much is in flight right now.\n\nOnly active work counts."

    split = EXPLAINED.replace(
        "# How much is in flight right now.",
        "# A stranded first paragraph.\n\n# How much is in flight right now.",
    )
    one_paragraph = compile_source(split).figure("team_person.wip")
    assert one_paragraph is not None
    assert one_paragraph.doc == "How much is in flight right now."


def test_a_dashed_aside_is_prose_but_a_dash_rule_is_not() -> None:
    """`# --- see the note below` is somebody's writing; `# ----------` and
    `# ---- board.fig ----` are rules. A banner pattern that swallowed any
    three dashes refused explanations that start with a dash -- with a message
    telling the author to write exactly what they had written."""
    aside = EXPLAINED.replace(
        "# How much is in flight right now.", "# --- see the note below"
    )
    figure = compile_source(aside).figure("team_person.wip")
    assert figure is not None
    assert figure.doc == "--- see the note below"

    for rule in ("# ----------", "# ---- board.fig ----", "# ------------- reviews --"):
        with pytest.raises(SyntaxError_):
            compile_source(
                EXPLAINED.replace("# How much is in flight right now.", rule)
            )


def test_the_two_refusals_say_what_the_docs_quote() -> None:
    """docs/language.md quotes both messages verbatim, so the wording is the
    contract: rewording either must force the doc quote to move with it."""
    with pytest.raises(SyntaxError_) as no_prose:
        compile_source(EXPLAINED.replace("# How much is in flight right now.\n", ""))
    assert no_prose.value.message == (
        "figure team_person.wip has no explanation. Write `#` comment lines directly "
        "above the declaration -- they are the customer-facing definition, rendered "
        "wherever the number is cited, and a figure nobody can read is the thing "
        "this language exists to prevent. (A `# ----` rule line is a file banner, "
        "not prose.)"
    )

    with pytest.raises(SyntaxError_) as inside:
        compile_source('figure a.b:\n    """d"""\n')
    assert inside.value.message == (
        "a docstring. A declaration's explanation is written as `#` comment lines "
        "directly above it, not inside the block"
    )


def test_an_indexs_comment_serves_as_prose_without_being_required() -> None:
    """Indexes and measures are plumbing: their comment is documentation when
    present and an honest empty string when not -- never a refusal."""
    from uratori.lang.source import declaration_prose

    lib = compile_source(
        EXPLAINED.replace(
            "filter work_issue.active where active == true",
            "# Only work someone is actively doing.\n"
            "filter work_issue.active where active == true",
        )
    )
    assert (
        declaration_prose(lib, "work_issue.active") == "Only work someone is actively doing."
    )
    assert declaration_prose(lib, "work_issue.assigned_to") == ""


def test_a_full_width_comment_inside_a_block_does_not_truncate_the_served_formula() -> None:
    """The lexer skips a column-0 comment wherever it sits, so the block
    continues past it -- and the source pane must agree, or it serves a
    formula with its calculate silently missing. The trailing case is the
    control: the NEXT declaration's explanation is not this block's tail."""
    from uratori.lang.source import declaration_source

    lib = compile_source(
        EXPLAINED.replace("    calculate:", "# only active work counts here\n    calculate:")
        + "\n# About sizing.\nfilter work_issue.sized where estimateSeconds is set\n"
    )
    formula = declaration_source(lib, "team_person.wip") or ""
    assert "count(mine)" in formula
    assert "About sizing." not in formula


# ---------------------------------------------------------- sub-day grain --

# A composite whose tail is a quarter-hour rather than a day, and a count
# figure over it. Every test below builds on these two.
QUARTER = """
group code_change.merged_by_quarter from (authorAccountId through team_person.accounts.accountId, mergedAt by 15 minutes)

# d
figure team_person.merge_rate bucketed:
    display "x"
    depends:
        mine = code_change.merged_by_quarter:{team_person}
    calculate:
        count(mine)
"""


def test_a_sub_day_truncation_is_the_figures_grain() -> None:
    """The control for everything in this section: `by 15 minutes` and
    `by minute` compile, carry the zone a day does, and mark the figure as
    keyed by that grain -- which is what decides everything a reading may do
    over it."""
    lib = compile_ok(
        QUARTER
        + """
group code_change.merged_by_minute from (authorAccountId through team_person.accounts.accountId, mergedAt by minute)

# d
figure team_person.merge_minutes bucketed:
    display "x"
    depends:
        mine = code_change.merged_by_minute:{team_person}
    calculate:
        count(mine)
"""
    )
    quarters = lib.figure("team_person.merge_rate")
    minutes = lib.figure("team_person.merge_minutes")
    assert quarters is not None and quarters.grain == "15 minutes"
    assert minutes is not None and minutes.grain == "minute"
    day = lib.figure("team_person.time_to_merge")
    assert day is not None and day.grain == "day"
    plain = lib.figure("team_person.wip")
    assert plain is not None and plain.grain is None


def test_a_grain_nobody_asked_for_is_refused_by_name() -> None:
    """A bucket rule decides how many values a figure has, so each one is a
    product decision rather than a convenience -- the calendar grains are a
    closed list, and five-minute buckets are not on it."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + "group code_change.by_five from (authorAccountId, mergedAt by 5 minutes)\n"
        )
    assert "not a bucket rule" in caught.value.message

    with pytest.raises(SyntaxError_):
        compile_source(BASE + "group code_change.by_ten from (authorAccountId, mergedAt by fortnight)\n")


def test_the_calendar_grains_are_declarable_and_are_the_figures_sequence() -> None:
    """`by hour`, `by week`, `by month` and `by quarter` are stored grains
    beside `by day`: the one place calendar vocabulary belongs, because the
    rule decides what a stored value means and is hashed here. A coarser
    figure beside a finer one is two declarations with two names -- never
    one figure re-sliced at read time -- and a reading's integer window
    walks whichever sequence its figure declared."""
    lib = compile_ok(
        """
group code_change.merged_by_hour from (authorAccountId through team_person.accounts.accountId, mergedAt by hour)
group code_change.merged_by_week from (authorAccountId through team_person.accounts.accountId, mergedAt by week)
group code_change.merged_by_month from (authorAccountId through team_person.accounts.accountId, mergedAt by month)
group code_change.merged_by_calendar_quarter from (authorAccountId through team_person.accounts.accountId, mergedAt by quarter)

# d
figure team_person.merges_hourly bucketed:
    display "x"
    depends:
        mine = code_change.merged_by_hour:{team_person}
    calculate:
        count(mine)

# d
figure team_person.merges_weekly bucketed:
    display "x"
    depends:
        mine = code_change.merged_by_week:{team_person}
    calculate:
        count(mine)

# d
figure team_person.merges_monthly bucketed:
    display "x"
    depends:
        mine = code_change.merged_by_month:{team_person}
    calculate:
        count(mine)

# d
figure team_person.merges_quarterly bucketed:
    display "x"
    depends:
        mine = code_change.merged_by_calendar_quarter:{team_person}
    calculate:
        count(mine)
"""
    )
    for name, grain in (
        ("team_person.merges_hourly", "hour"),
        ("team_person.merges_weekly", "week"),
        ("team_person.merges_monthly", "month"),
        ("team_person.merges_quarterly", "quarter"),
    ):
        plan = lib.figure(name)
        assert plan is not None and plan.grain == grain, name


def test_an_ordinal_weekday_rule_parses_and_is_the_figures_sequence() -> None:
    """`by first monday of month` is the selective family: sparse day
    buckets, one per month at most, and the figure's grain is the rule's
    own text so a window over it walks first-Mondays."""
    lib = compile_ok(
        """
group code_change.merged_first_mondays from (authorAccountId through team_person.accounts.accountId, mergedAt by first monday of month)

# d
figure team_person.first_monday_merges bucketed:
    display "x"
    depends:
        mine = code_change.merged_first_mondays:{team_person}
    calculate:
        count(mine)
"""
    )
    plan = lib.figure("team_person.first_monday_merges")
    assert plan is not None and plan.grain == "first monday of month"


def test_an_ordinal_rule_is_spelled_exactly_or_refused_with_directions() -> None:
    """The family is spelled one way -- `<first..fifth> <weekday> of month` --
    and each way of getting it wrong must say what the right spelling is.

    The message is the assertion, not the raise. A bare `pytest.raises` here
    could not fail if every one of these degraded to a generic keyword error,
    which is what two of them did: `of year` and a missing `of` fall out of
    the keyword expectations rather than the rule's own refusal.
    """
    for wrong, expected in (
        # The ordinal and the weekday are recognised, so the rule's own
        # refusal fires and names the whole spelling.
        ("by first blursday of month", "first monday of month"),
        ("by sixth monday of month", "ordinal weekday"),
        # These two miss the keyword the grammar is looking for, and the
        # refusal is allowed to be the keyword's -- but it must still name
        # the word it wanted, or an author sees only that something is wrong.
        ("by first friday of year", "month"),
        ("by first tuesday month", "of"),
    ):
        with pytest.raises(SyntaxError_) as caught:
            compile_source(
                BASE
                + f"group code_change.odd from (authorAccountId, mergedAt {wrong})\n"
            )
        assert expected in caught.value.message, (wrong, caught.value.message)


def test_an_ordinal_rules_zone_must_be_a_bucket_setting() -> None:
    """The selective rules sit exactly where the grains sit: `in <setting>`
    names whose calendar decides which month a day belongs to, and turning
    that dial re-buckets history -- so only a bucket setting may sit there.
    (The moment requirement is checked against a declared world in
    test_facts.)"""
    refuses(
        "group code_change.odd from (authorAccountId, mergedAt by first monday of month in nowhere.zone)\n",
        "not a fact kind",
    )


def test_a_series_grouping_is_retired_toward_a_coarser_declaration() -> None:
    """`series(...) by hour` regrouped stored buckets on the way out --
    read-time truncation under the reading's name. The coarser view is now
    its own hour-grained figure, so the clause refuses with directions, and
    a bare series is one point per stored bucket, sub-day sources
    included."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + QUARTER
            + """
# d
reading team_person.throughput(range):
    display "x"
    depends:
        m = team_person.merge_rate in range
    calculate:
        series(m) by hour
"""
        )
    assert "retired" in caught.value.message
    assert "by hour" in caught.value.message

    # The control: the bare series compiles over the quarter-hour figure,
    # and its points are that figure's own buckets.
    lib = compile_ok(
        QUARTER
        + """
# d
reading team_person.throughput(range):
    display "x"
    depends:
        m = team_person.merge_rate in range
    calculate:
        series(m)
"""
    )
    plan = lib.reading("team_person.throughput")
    assert plan is not None
    assert [s.fn for s in plan.calculate] == ["series"]


def test_a_minute_grain_figure_refuses_a_series() -> None:
    """Over a sparse figure a minute bucket holds one record, so the point
    *is* the record -- the raw collection the payload exists to withhold.
    The scalar statistics stay legal: they pool the window's values."""
    minute_figure = """
group code_change.merged_by_minute from (authorAccountId through team_person.accounts.accountId, mergedAt by minute)

# d
figure team_person.merges_by_minute bucketed:
    display "x"
    depends:
        mine = code_change.merged_by_minute:{team_person}
    calculate:
        count(mine)
"""
    refuses(
        minute_figure
        + """
# d
reading team_person.minute_rate(range):
    display "x"
    depends:
        m = team_person.merges_by_minute in range
    calculate:
        series(m)
""",
        "the record",
    )

    # The control: the same reading without the series compiles.
    lib = compile_ok(
        minute_figure
        + """
# d
reading team_person.minute_rate(range):
    display "x"
    depends:
        m = team_person.merges_by_minute in range
    calculate:
        sum(m)
"""
    )
    assert lib.reading("team_person.minute_rate") is not None


def test_a_by_clause_on_any_statistic_is_refused_with_the_retirement() -> None:
    """`series(...) by <grain>` is retired, and the parser no longer looks at
    which statistic wears the clause -- so the refusal must be the retirement,
    naming where a coarser view now lives, for a scalar as much as for a
    series.

    This test used to claim the old rule ("only a series takes a grain") and
    asserted only that "series" appeared in the message. That word survives in
    the retirement text, so it passed on a coincidence about a rule that no
    longer exists. The message is what it pins now."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + QUARTER
            + """
# d
reading team_person.throughput(range):
    display "x"
    depends:
        m = team_person.merge_rate in range
    calculate:
        sum(m) by hour
"""
        )
    message = caught.value.message
    assert "retired" in message
    assert "group the figure" in message, (
        "the refusal must send the author to the declaration that replaces "
        "the clause, not merely report that the clause is gone"
    )

    # And the same words for the statistic the clause was once legal on, so
    # the two spellings cannot drift into two explanations.
    with pytest.raises(SyntaxError_) as on_series:
        compile_source(
            BASE
            + QUARTER
            + """
# d
reading team_person.shape(range):
    display "x"
    depends:
        m = team_person.merge_rate in range
    calculate:
        series(m) by hour
"""
        )
    assert on_series.value.message == message


def test_two_series_under_one_reading_are_refused() -> None:
    """A response carries one series; two declared would mean whichever the
    serve path kept, silently. Two grains are two readings."""
    refuses(
        QUARTER
        + """
# d
reading team_person.throughput(range):
    display "x"
    depends:
        m = team_person.merge_rate in range
    calculate:
        series(m)
        series(m)
""",
        "two series",
    )


def test_a_live_reading_has_no_buckets_to_be_the_points() -> None:
    """A series' points are the stored buckets of a figure's sequence, and a
    live reading has neither figure nor sequence -- so the statistic is
    refused whole, not just a grain on it. (It once compiled bare, meaning
    nothing anything could serve.)"""
    refuses(
        """
# d
reading team_person.queue():
    display "x"
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)
        series(w)
""",
        "live",
    )

    # The control: the same reading without the series compiles -- the
    # refusal is about the series, not about live readings.
    lib = compile_ok(
        """
# d
reading team_person.queue():
    display "x"
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)
"""
    )
    assert lib.reading("team_person.queue") is not None


def test_a_time_keyed_figure_cannot_be_read_as_a_single_value() -> None:
    """A bare read would take whichever bucket sorted first -- one quarter-hour
    out of a history, presented as the subject's number."""
    refuses(
        QUARTER
        + """
# d
figure team_person.merge_total:
    display "x"
    combine:
        rate = team_person.merge_rate
    calculate:
        rate
""",
        "time-keyed",
    )


def test_a_projection_may_not_read_a_time_keyed_figure() -> None:
    """A row holds one value; a time-keyed figure has one per bucket, so every
    row would be about whichever bucket the lookup happened to find."""
    refuses(
        QUARTER
        + """
# d
projection team_person.card:
    field:
        name = display_name as text
    read:
        rate = team_person.merge_rate
""",
        "time-keyed",
    )


def test_the_stored_grain_is_in_the_version_hash() -> None:
    """The same figure over minutes and over quarter-hours stores values that
    mean different things; reusing them across the change would file a
    quarter's count under a minute's key."""
    minute = compile_source(
        BASE + QUARTER.replace("by 15 minutes", "by minute")
    ).figure("team_person.merge_rate")
    quarter = compile_source(BASE + QUARTER).figure("team_person.merge_rate")
    assert minute is not None and quarter is not None
    assert minute.version != quarter.version


def test_a_changed_bucket_rule_moves_the_figures_version() -> None:
    """The bucket rule changes what a stored number means -- a month of
    merges filed under a day's key is a 30x error citing one hash -- so
    day, month and first-monday cuts of one group are three versions."""
    def figure_with(rule: str) -> str:
        return (
            BASE
            + f"""
group code_change.cut from (authorAccountId through team_person.accounts.accountId, mergedAt by {rule})

# d
figure team_person.cut_count bucketed:
    display "x"
    depends:
        mine = code_change.cut:{{team_person}}
    calculate:
        count(mine)
"""
        )

    versions = {}
    for rule in ("day", "month", "quarter", "first monday of month", "second monday of month"):
        plan = compile_source(figure_with(rule)).figure("team_person.cut_count")
        assert plan is not None, rule
        versions[rule] = plan.version
    assert len(set(versions.values())) == len(versions), versions


def test_the_retirement_does_not_move_a_day_readings_version() -> None:
    """The invariant the retirement must hold: a reading that never wrote a
    series grain or a window unit hashes exactly as it did -- its statistics
    hash as [fn, set] pairs with nothing riding along -- so every stored
    citation under the old grammar survives the grammar's narrowing."""
    lib = compile_ok(
        """
# d
reading team_person.to_merge(range):
    display "x"
    depends:
        merged = team_person.time_to_merge in range
    calculate:
        mean(merged)
"""
    )
    plan = lib.reading("team_person.to_merge")
    assert plan is not None
    # Pinned to the value the pre-retirement compiler (v0.15.0) produced for
    # this exact definition. If this moves, stored history re-cites for a
    # change that claimed to touch only the argument grammar.
    assert plan.version == "5f36aa77bc08"


def test_a_keyed_as_kinds_projection_may_filter_through_its_own_index() -> None:
    """`keyed as` says a kind's record keys ARE another kind's ids, so an
    index over its own records holds exactly the keys its rows carry and the
    population resolves row for row. Compared by raw kind this was refused --
    with a message claiming every row would be filtered away, the opposite of
    the truth. The id-space rule itself still stands: the test above holds a
    plain kind to it."""
    lib = compile_source(
        BASE
        + """
filter code_review.approving keyed as code_change where wasApproved == true

# The approvals on record.
projection code_review.approvals:
    from code_review.approving

    field:
        change = changeId as text
"""
    )
    assert lib.projection("code_review.approvals") is not None

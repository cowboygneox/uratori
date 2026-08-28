"""The step function, on its own.

`carried_rows` is the whole of what `carried forward` *means*, separated from
where the anchors came from and from what materialises the result. Everything
here is a claim about the step: where it starts, which direction it runs, what
a later change does to the buckets before it, and what each row cites.

The separation is deliberate. The three triggers (a fact landing, a pass
extending, a read filling) are *meant* to share this one function, so a rule
proved here is proved for all three at once -- which is the only reason
three triggers are allowed to exist at all.

None of those triggers exists yet: there is no `carried forward` in the
grammar, so nothing under `uratori/` calls into this module. These are the
step's own rules, pinned ahead of the wiring; the integration tests that
prove the three triggers write byte-identical rows are owed and not here.
"""

from __future__ import annotations

import pytest

from uratori.engine.carry import (
    Anchor,
    CarriedRow,
    CarryReachExceeded,
    carried_rows,
    sequence_to_present,
)


def months(*labels: str) -> list[str]:
    return list(labels)


def month_walker(present: str) -> object:
    """A stand-in for the engine's own span resolver: the n most recent
    months, oldest first, ending at `present`.

    The real caller hands `sequence_to_present` the *same* resolver a window
    uses, which is the point of the parameter -- see the agreement test at
    the bottom of this file.
    """

    year, month = (int(p) for p in present.split("-"))

    def labels_back(n: int) -> list[str]:
        out: list[str] = []
        for k in range(n - 1, -1, -1):
            y, m = year, month - k
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
            if y < 1:
                continue
            out.append(f"{y:04d}-{m:02d}")
        return out

    return labels_back


def test_nothing_to_carry_is_no_rows_at_all() -> None:
    """Before the first anchor there is an **absence**, never a nought.

    The mistake this refuses: seeding the sequence at its start so every
    bucket has a value, which would band January as comfortable against a
    goal nobody had set yet.
    """
    rows = carried_rows([], months("2026-01", "2026-02", "2026-03"))
    assert rows == []


def test_buckets_before_the_first_anchor_are_absent() -> None:
    anchors = [Anchor(label="2026-02", value=1800.0, members=("g1",))]
    rows = carried_rows(anchors, months("2026-01", "2026-02", "2026-03"))
    assert [r.label for r in rows] == ["2026-02", "2026-03"]
    # January is not in the output at all -- an absent row, not a null one.
    assert all(r.label != "2026-01" for r in rows)


def test_an_anchor_bucket_carries_its_own_value_and_cites_itself() -> None:
    anchors = [Anchor(label="2026-02", value=1800.0, members=("g1",))]
    rows = carried_rows(anchors, months("2026-02"))
    assert rows == [
        CarriedRow(label="2026-02", value=1800.0, members=("g1",), anchor="2026-02")
    ]


def test_a_gap_carries_the_preceding_anchor_and_cites_it() -> None:
    """The carry runs **forward**. A March with no change reads February's
    goal, and says so by citing February."""
    anchors = [Anchor(label="2026-02", value=1800.0, members=("g1",))]
    rows = carried_rows(anchors, months("2026-02", "2026-03", "2026-04"))
    assert [(r.label, r.value, r.anchor) for r in rows] == [
        ("2026-02", 1800.0, "2026-02"),
        ("2026-03", 1800.0, "2026-02"),
        ("2026-04", 1800.0, "2026-02"),
    ]


def test_a_carried_row_cites_its_anchors_evidence_not_its_own_bucket() -> None:
    """March's row is made of February's record. The evidence chain has to
    say so, or a reader asked "why 30m in March" is shown an empty bucket."""
    anchors = [Anchor(label="2026-02", value=1800.0, members=("g1", "g2"))]
    rows = carried_rows(anchors, months("2026-02", "2026-03"))
    assert rows[1].members == ("g1", "g2")


def test_a_later_anchor_supersedes_from_its_own_bucket_forward() -> None:
    """The worked scenario Sean pinned: 30m set in February, 25m set in June.

    Feb is the anchor, Mar-May carry it, June is the new anchor, July onward
    carries the new one. January never existed.
    """
    anchors = [
        Anchor(label="2026-02", value=1800.0, members=("g1",)),
        Anchor(label="2026-06", value=1500.0, members=("g2",)),
    ]
    sequence = months(
        "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
        "2026-06", "2026-07", "2026-08",
    )
    rows = carried_rows(anchors, sequence)
    assert [(r.label, r.value, r.anchor) for r in rows] == [
        ("2026-02", 1800.0, "2026-02"),
        ("2026-03", 1800.0, "2026-02"),
        ("2026-04", 1800.0, "2026-02"),
        ("2026-05", 1800.0, "2026-02"),
        ("2026-06", 1500.0, "2026-06"),
        ("2026-07", 1500.0, "2026-06"),
        ("2026-08", 1500.0, "2026-06"),
    ]


def test_a_retroactive_anchor_rewrites_forward_and_leaves_history_alone() -> None:
    """A change *entered* late but *dated* in April rewrites April onward and
    touches nothing before it.

    This is the property that makes carried rows safe to store: the answer for
    a bucket depends only on the anchors at or before it, so a later arrival
    can never make an earlier bucket wrong.
    """
    before = carried_rows(
        [Anchor(label="2026-02", value=1800.0, members=("g1",))],
        months("2026-02", "2026-03", "2026-04", "2026-05"),
    )
    after = carried_rows(
        [
            Anchor(label="2026-02", value=1800.0, members=("g1",)),
            Anchor(label="2026-04", value=1200.0, members=("g3",)),
        ],
        months("2026-02", "2026-03", "2026-04", "2026-05"),
    )
    # Earlier buckets are byte-identical -- history is never rewritten.
    assert after[:2] == before[:2]
    # From the new anchor forward, the value and the citation both move.
    assert [(r.label, r.value, r.anchor) for r in after[2:]] == [
        ("2026-04", 1200.0, "2026-04"),
        ("2026-05", 1200.0, "2026-04"),
    ]


def test_an_anchor_before_the_requested_sequence_still_governs_it() -> None:
    """A read asking only for the second half of the year must still see the
    goal set in June.

    The lazy path fills a window, not the whole history, so the governing
    anchor is routinely outside the labels being written. Starting the carry
    at the window's own first label instead would answer an absence for
    exactly the months a goal has been in force longest.
    """
    anchors = [Anchor(label="2026-06", value=1500.0, members=("g2",))]
    rows = carried_rows(anchors, months("2026-09", "2026-10"))
    assert [(r.label, r.value, r.anchor) for r in rows] == [
        ("2026-09", 1500.0, "2026-06"),
        ("2026-10", 1500.0, "2026-06"),
    ]


def test_the_sequence_bounds_the_output_exactly() -> None:
    """The function invents no label. Whatever "the present bucket" is, the
    caller decides it by what it puts in `sequence` -- so never-past-present
    is one rule in one place rather than a guard in each of three triggers.
    """
    anchors = [
        Anchor(label="2026-02", value=1800.0, members=("g1",)),
        Anchor(label="2026-06", value=1500.0, members=("g2",)),
    ]
    rows = carried_rows(anchors, months("2026-03", "2026-04"))
    assert [r.label for r in rows] == ["2026-03", "2026-04"]


def test_anchors_out_of_order_produce_the_same_answer() -> None:
    """The caller reads anchors out of the store, whose ordering is its own
    business. A carry that depended on that order would be right in tests and
    wrong the first time a store returned rows by insertion.
    """
    ordered = carried_rows(
        [
            Anchor(label="2026-02", value=1800.0, members=("g1",)),
            Anchor(label="2026-06", value=1500.0, members=("g2",)),
        ],
        months("2026-02", "2026-06", "2026-07"),
    )
    shuffled = carried_rows(
        [
            Anchor(label="2026-06", value=1500.0, members=("g2",)),
            Anchor(label="2026-02", value=1800.0, members=("g1",)),
        ],
        months("2026-02", "2026-06", "2026-07"),
    )
    assert ordered == shuffled


def test_an_anchor_whose_value_is_absent_carries_the_absence_forward() -> None:
    """A bucket whose records could not be measured is an anchor that says
    "nobody can tell", and that is what the months after it inherit.

    The alternative -- skip an unmeasurable anchor and carry the one before
    it -- would report a goal that had demonstrably been replaced, which is
    worse than reporting nothing.
    """
    anchors = [
        Anchor(label="2026-02", value=1800.0, members=("g1",)),
        Anchor(label="2026-05", value=None, members=("g4",)),
    ]
    rows = carried_rows(anchors, months("2026-04", "2026-05", "2026-06"))
    assert [(r.label, r.value, r.anchor) for r in rows] == [
        ("2026-04", 1800.0, "2026-02"),
        ("2026-05", None, "2026-05"),
        ("2026-06", None, "2026-05"),
    ]


def test_an_empty_sequence_writes_nothing() -> None:
    anchors = [Anchor(label="2026-02", value=1800.0, members=("g1",))]
    assert carried_rows(anchors, []) == []


def test_a_word_valued_anchor_carries_as_the_word() -> None:
    """Nothing here is numeric. The carry copies whatever the figure stored,
    so a ladder-valued on-change figure steps exactly as a duration one does.
    """
    anchors = [Anchor(label="2026-02", value="strict", members=("s1",))]
    rows = carried_rows(anchors, months("2026-02", "2026-03"))
    assert [r.value for r in rows] == ["strict", "strict"]


# ------------------------------------------------- reaching the present --
#
# Which labels a carried figure owes is a question about the calendar, and
# this package already has exactly one answer to that: the resolver a
# window uses to turn `over 1-6` into six concrete buckets. `sequence_to_present`
# takes that resolver as an argument rather than walking the calendar itself,
# so a carried figure can never materialise a label a window would not ask
# for -- the two cannot disagree, because there is only one of them.


def test_the_sequence_runs_from_the_anchor_to_the_present_bucket() -> None:
    got = sequence_to_present("2026-02", month_walker("2026-08"), cap=120)
    assert got == [
        "2026-02", "2026-03", "2026-04", "2026-05",
        "2026-06", "2026-07", "2026-08",
    ]


def test_the_present_bucket_is_included_and_nothing_after_it() -> None:
    """**Never past the present.** Whatever asks -- a pass, a read, a fact --
    materialisation stops at the bucket containing now. A sequence running
    one bucket further would put a value on a month that has not happened,
    and a band would colour it."""
    got = sequence_to_present("2026-07", month_walker("2026-08"), cap=120)
    assert got[-1] == "2026-08"
    assert all(label <= "2026-08" for label in got)


def test_an_anchor_in_the_present_bucket_is_the_whole_sequence() -> None:
    assert sequence_to_present("2026-08", month_walker("2026-08"), cap=120) == ["2026-08"]


def test_a_future_dated_anchor_materialises_nothing_yet() -> None:
    """A change dated next month is not a bucket to write; it is a bucket to
    wait for. The pass that reaches September will anchor it then."""
    assert sequence_to_present("2026-09", month_walker("2026-08"), cap=120) == []


def test_the_sequence_is_whatever_the_resolver_says_it_is() -> None:
    """The delegation, proved by handing over a calendar nothing could guess.

    A resolver whose sequence skips arbitrary positions -- as a selective
    rule's really does, a fifth Monday existing in some months and not
    others -- must come back verbatim. An implementation that worked the
    span out for itself, by counting months or days between labels, would
    invent the missing entries and materialise buckets no window ever asks
    for; one that did its own arithmetic on these labels could not produce
    them at all.

    Asserting the resolver was merely *called* is not enough: a second
    calendar that also called it would pass that, which is how the earlier
    version of this test managed to stay green when the search was replaced
    with month arithmetic.
    """
    sparse = [
        "2026-01-05", "2026-02-02", "2026-04-06", "2026-04-27", "2026-09-07",
    ]

    def resolver(n: int) -> list[str]:
        return sparse[-min(n, len(sparse)) :]

    assert sequence_to_present("2026-02-02", resolver, cap=120) == [
        "2026-02-02", "2026-04-06", "2026-04-27", "2026-09-07",
    ], "the gaps are the calendar's own; nothing here may fill them in"

    # And the whole of it when the anchor predates the sequence.
    assert sequence_to_present("2025-01-01", resolver, cap=120) == sparse


def test_the_walk_asks_the_resolver_and_stays_inside_the_cap() -> None:
    seen: list[int] = []
    walker = month_walker("2026-08")

    def spy(n: int) -> list[str]:
        seen.append(n)
        return walker(n)  # type: ignore[operator]

    sequence_to_present("2026-02", spy, cap=120)
    assert seen == [1, 2, 4, 8], (
        "the walk doubles until it reaches back past the anchor and then stops; "
        f"it asked for {seen}"
    )


def test_a_short_answer_ends_the_walk_rather_than_widening_to_the_cap() -> None:
    """A resolver that has run out of calendar is asked once more and no more.

    A short answer is the only "there are no older buckets" signal the walk
    gets. Ignoring it still returns the right labels -- which is why every
    value assertion in this file misses it -- while doubling on to the
    ceiling and then probing past it: nine calendar resolutions per subject
    where three would do, on exactly the figures whose first change predates
    collection.
    """
    seen: list[int] = []
    calendar = ["2026-06", "2026-07", "2026-08"]

    def clamped(n: int) -> list[str]:
        seen.append(n)
        return calendar[-min(n, len(calendar)) :]

    assert sequence_to_present("1990-01", clamped, cap=120) == calendar
    assert seen, "the resolver is the only thing that knows what a bucket is"
    assert max(seen) <= 2 * len(calendar), (
        "the walk carried on past the answer that told it the calendar had "
        f"ended -- it asked for {max(seen)} buckets of a {len(calendar)}-bucket "
        f"calendar: {seen}"
    )


def test_a_resolver_that_refuses_to_look_further_becomes_our_own_refusal() -> None:
    """The ceiling probe asks for one bucket more than the cap, and the
    engine's own span resolver refuses a reach past its bound rather than
    clamping.

    Left unhandled that refusal escapes as the resolver's exception type,
    from a call the caller never made, naming a limit the caller never set.
    It answers the question the probe was asking, so it becomes the carry's
    own refusal.
    """
    calendar = [f"2026-{m:02d}" for m in range(1, 13)]

    def bounded(n: int) -> list[str]:
        if n > len(calendar):
            raise ValueError("this span reaches further back than the ceiling")
        return calendar[-n:]

    with pytest.raises(CarryReachExceeded):
        sequence_to_present("1990-01", bounded, cap=len(calendar))


def test_the_reach_is_capped_rather_than_walked_for_ever() -> None:
    """An anchor far outside any horizon is a refusal, not a million rows.

    A figure whose first change was dated 1970 would otherwise materialise
    six hundred months per subject on the first read that touched it. The
    cap is the same reasoning the window's own reach bound records: the
    reach *is* the cost.
    """
    with pytest.raises(CarryReachExceeded):
        sequence_to_present("1900-01", month_walker("2026-08"), cap=24)


def test_a_calendar_that_stops_growing_ends_the_walk() -> None:
    """A resolver clamped at the start of its calendar answers the same list
    however much more is asked of it, and the search has to notice rather
    than spin."""

    def clamped(n: int) -> list[str]:
        # A two-bucket calendar: asked for more than it has, it answers all
        # of it -- oldest first, still ending at the present bucket.
        return ["0001-01", "0001-02"][-min(n, 2) :]

    assert sequence_to_present("0001-01", clamped, cap=120) == ["0001-01", "0001-02"]


def test_two_anchors_in_one_bucket_are_refused_rather_than_silently_picked() -> None:
    """A bucket has one value, so a second anchor for it is a caller error.

    Left to fall through, the sort is stable and `bisect_right` takes the
    last of a tied run, so the answer would be whichever the caller happened
    to list second -- a deterministic, invisible coin toss whose loser is
    then carried forward over every later bucket. Aborting is the choice
    `evaluate._scalar` makes in the same situation: a wrong number nobody
    can see is worse than a failed pass somebody can.
    """
    clashing = [
        Anchor(label="2026-06", value=1500.0, members=("g2",)),
        Anchor(label="2026-06", value=1200.0, members=("g3",)),
    ]
    with pytest.raises(ValueError) as caught:
        carried_rows(clashing, months("2026-06"))
    assert "2026-06" in str(caught.value)

    # The control: the same two values in *different* buckets are ordinary.
    fine = [
        Anchor(label="2026-06", value=1500.0, members=("g2",)),
        Anchor(label="2026-07", value=1200.0, members=("g3",)),
    ]
    assert [r.value for r in carried_rows(fine, months("2026-06", "2026-07"))] == [
        1500.0,
        1200.0,
    ]


def test_a_calendar_exactly_as_long_as_the_cap_is_returned_whole() -> None:
    """The boundary the search cannot see without asking.

    It never requests more than `cap`, so a calendar of exactly `cap`
    buckets looks identical to one that runs for ever -- and refusing to
    carry a sequence that would have fitted is the wrong error. One probe
    past the ceiling tells them apart.
    """
    calendar = [f"2026-{m:02d}-{d:02d}" for m in (1, 2) for d in range(1, 13)]
    assert len(calendar) == 24

    def resolver(n: int) -> list[str]:
        return calendar[-min(n, len(calendar)) :]

    assert sequence_to_present("1990-01-01", resolver, cap=24) == calendar

    # And one bucket more than the cap is still refused: the reach is the cost.
    longer = ["2025-12-31", *calendar]

    def bigger(n: int) -> list[str]:
        return longer[-min(n, len(longer)) :]

    with pytest.raises(CarryReachExceeded):
        sequence_to_present("1990-01-01", bigger, cap=24)

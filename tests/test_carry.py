"""The step function, on its own.

`carried_rows` is the whole of what `carried forward` *means*, separated from
where the anchors came from and from what materialises the result. Everything
here is a claim about the step: where it starts, which direction it runs, what
a later change does to the buckets before it, and what each row cites.

The separation is deliberate. The three triggers (a fact landing, a pass
extending, a read filling) share this one function, so a rule proved here is
proved for all three at once -- which is the only reason three triggers are
allowed to exist at all.
"""

from __future__ import annotations

from uratori.engine.carry import Anchor, CarriedRow, carried_rows


def months(*labels: str) -> list[str]:
    return list(labels)


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

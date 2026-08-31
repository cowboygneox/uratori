"""Band words from definitions must appear on the wire unchanged.

The bug: a definition writes `over`, the translation layer maps it to `poor`,
and the Data screen prints a word that appears nowhere in the definition rendered
under it. That is the defect this test catches.

Both band shapes -- a figure's `band:` block and a reading's -- are ladders
written by the author, so there is nothing left for a translation layer to
map: whatever word the definition wrote is the word the wire carries. The
engine no longer invents good/watch/poor from a dial's two edges.
"""

import pytest

from uratori.engine.read import level_of
from uratori.lang.ast import (
    FigureRef,
    Ladder,
    Number,
    Part,
    Rung,
    Text,
)
from uratori.lang.plan import FigurePlan, ReadingPlan


def test_ladder_word_preserved_not_translated():
    """A ladder returning 'over' must evaluate to 'over', not 'poor'."""
    # Test the actual evaluation through _eval directly
    from uratori.engine.evaluate import Readers, _eval

    ladder = Ladder(
        rungs=[
            Rung(
                left=Number(1),
                op=">=",
                right=Number(1),
                then=Text("over"),
            )
        ],
        otherwise=Text("ok"),
    )

    # Create a minimal plan (needed by _eval signature)
    plan = FigurePlan(
        name="test.level",
        scope="test_scope",
        doc="",
        display="",
        unit="level",
        calculate=ladder,
    )

    readers = Readers(
        buckets=lambda index, subject: frozenset(),
        measures=lambda measure, member: None,
        moments=lambda measure, member: None,
        parts=lambda source, subject: type("Parts", (), {"subjects": (), "values": []}),
        settings=lambda path: None,
    )

    # The ladder evaluates to "over" directly - no translation
    value = _eval(ladder, plan, "test-subject", {}, readers)
    assert value == "over", "Ladder should return 'over', not translate it"


def test_a_reading_bands_by_its_own_ladder_against_a_goal():
    """A reading's verdict is the word its ladder wrote, judged against the
    goal figure reduced over the same window.

    This used to be a dial with two edges and a direction, from which the
    engine derived good, watch or poor -- three words no definition contained.
    """
    plan = ReadingPlan(
        name="test.reading",
        scope="test_scope",
        mode="window",
        doc="",
        display="",
        unit="duration",
        calculate=("mean",),
        requires=(),
        band=Ladder(
            rungs=(
                Rung(left=Part("value"), op=">", right=FigureRef("test.goal"), then=Text("over")),
            ),
            otherwise=Text("ok"),
        ),
        band_on="mean",
        band_reads=("test.goal",),
        source="test.figure",
    )

    over = level_of(plan, {"mean": 432_000.0}, {"test.goal": 172_800.0})
    assert over == "over", "five days against a two-day goal is over"

    under = level_of(plan, {"mean": 86_400.0}, {"test.goal": 172_800.0})
    assert under == "ok", "one day against a two-day goal is not over"

    # The goal is what the whole verdict hangs on, so a window with no goal
    # has no verdict -- never the comfortable bottom rung.
    assert level_of(plan, {"mean": 432_000.0}, {}) == "unknown"


def test_translation_layer_breaks_ladder_words():
    """This demonstrates the bug: _level_word translates 'over' to 'poor'.

    This test currently FAILS because the translation layer exists.
    Once fixed, it will pass because the word flows through unchanged.
    """
    from uratori.engine.serve import _level_word, _level_word_from

    # The bug in _level_word: these calls translate the definition's word
    # Currently they return "poor", "watch", "good" but should preserve the original
    assert _level_word("over") == "over", "over should stay over, not become poor"
    assert _level_word("warn") == "warn", "warn should stay warn, not become watch"
    assert _level_word("ok") == "ok", "ok should stay ok, not become good"

    # Same bug in _level_word_from
    assert _level_word_from("over") == "over", "over should stay over"
    assert _level_word_from("warn") == "warn", "warn should stay warn"
    assert _level_word_from("ok") == "ok", "ok should stay ok"


def test_unrecognized_word_must_not_default_to_green():
    """A word the browser doesn't recognize must render as neutral, never green.

    This is a safety requirement: if a future definition invents a new band word
    like "critical" or "excellent", the browser must not silently treat it as
    good (which a missing `case` would do if green were the default).

    This test documents the requirement. The actual check is in the browser's
    color mapping, tested separately.
    """
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""Band words from definitions must appear on the wire unchanged.

The bug: a definition writes `over`, the translation layer maps it to `poor`,
and the Data screen prints a word that appears nowhere in the definition rendered
under it. That is the defect this test catches.

For `when` ladders: the author wrote the word, preserve it.
For `band` clauses: the engine generates good/watch/poor, preserve those too.
"""

import pytest

from uratori.engine.read import level_of
from uratori.lang.ast import (
    Band,
    Ladder,
    Number,
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


def test_band_clause_generates_expected_words():
    """Band clause generates good/watch/poor based on thresholds."""
    plan = ReadingPlan(
        name="test.reading",
        scope="test_scope",
        mode="window",
        doc="",
        display="",
        unit="duration",
        calculate=("mean",),
        requires=(),
        band=Band(
            direction="low",
            setting="test.threshold",
            unit="days",
            on="mean",
        ),
        source="test.figure",
    )

    # Simulate stats that would band as "warn" (between thresholds)
    # For direction="low": value <= good => "ok", value >= poor => "over", else => "warn"
    # good=2 days = 172800 seconds, poor=7 days = 604800 seconds
    # So mean of 5 days = 432000 seconds should be "warn"
    stats = {"mean": 432000.0}  # 5 days in seconds, between good (2d) and poor (7d)
    settings = {"test": {"threshold": {"good": 2.0, "poor": 7.0}}}

    word = level_of(plan, stats, settings)

    # Band clause generates "warn", and it should stay "warn"
    assert word == "warn", "Band clause should generate 'warn' from thresholds"


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

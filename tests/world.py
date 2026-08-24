"""The world the test definitions are written against.

A mirror of the origin project's schema, kept as plain test data. The suite's
inline definitions were written against these kinds and dials, and rewriting
three thousand lines of behavioural spec to a synthetic vocabulary would risk
exactly the drift the tests exist to catch -- so the vocabulary stays and the
declaration moves here, which is itself the proof that nothing in the engine
knows these names: `test_schema.py` runs the same machinery over a disjoint
world.
"""

from __future__ import annotations

from typing import Any

from uratori import Library, Schema
from uratori import compile_source as _compile

DEFAULTS: dict[str, Any] = {
    "tenant": {"timezone": "America/Los_Angeles", "hoursPerDay": 8},
    "thresholds": {
        "staleChangeDays": 3,
        "longWipDays": 14,
        "draftGraceDays": 2,
        "openChanges": {"warn": 3, "over": 6},
        "wip": {"warn": 3, "over": 5},
        "reviewQueue": {"warn": 4, "over": 8},
    },
    "windows": {"historyDays": 90},
    "roadmap": {"atRiskVariance": -0.1, "offTrackVariance": -0.25, "stalledDays": 14},
    "flow": {
        "reviewLatencyDays": {"good": 2, "poor": 5},
        "reviewResponseDays": {"good": 1, "poor": 3},
        "leadTimeDays": {"good": 7, "poor": 21},
        "pendingReviews": {"good": 3, "poor": 8},
    },
}

_AGE = (
    "thresholds.staleChangeDays",
    "thresholds.longWipDays",
    "thresholds.draftGraceDays",
    "windows.historyDays",
)

WORLD = Schema(
    kinds=frozenset(
        {
            "work_account",
            "work_issue",
            "work_container",
            "code_account",
            "code_repo",
            "code_change",
            "code_review",
            "code_review_request",
            "team_person",
            "data_connection",
            "config_setting",
        }
    ),
    name_fields={
        "work_account": "display_name",
        "work_issue": "title",
        "work_container": "title",
        "code_account": "display_name",
        "code_repo": "path",
        "code_change": "title",
        "code_review": "change_id",
        "code_review_request": "title",
        "team_person": "display_name",
        "data_connection": "label",
        "config_setting": "path",
    },
    bucket_settings=("tenant.timezone", *_AGE),
    figure_settings=(
        "thresholds.openChanges.warn",
        "thresholds.openChanges.over",
        "thresholds.wip.warn",
        "thresholds.wip.over",
        "thresholds.reviewQueue.warn",
        "thresholds.reviewQueue.over",
    ),
    reading_settings=(
        "flow.reviewLatencyDays",
        "flow.reviewResponseDays",
        "flow.leadTimeDays",
        "flow.pendingReviews",
    ),
    project_settings=(
        "thresholds.longWipDays",
        "thresholds.staleChangeDays",
        "thresholds.wip.over",
        "thresholds.openChanges.over",
        "windows.historyDays",
        "roadmap.atRiskVariance",
        "roadmap.offTrackVariance",
        "roadmap.stalledDays",
    ),
    defaults=DEFAULTS,
)


def compile_source(source: str) -> Library:
    """The library's `compile_source`, against this suite's world. Same name on
    purpose: the tests read as they always did, and the schema argument is the
    one thing this suite holds constant."""
    return _compile(source, WORLD)


SPECIMENS: dict[str, dict[str, Any]] = {
    # One record per kind the suite's inline libraries read, shaped like the
    # records the origin project collects. This pins the *suite's* internal
    # consistency -- its definitions against the facts its tests write; the
    # product-side half of the old guard (shipped definitions against real
    # record types) lives on in the host's own test_fields.py, where the real
    # record constructors are.
    "work_issue": {
        "title": "Ship the thing",
        "assignee_account_id": "acc1",
        "active": True,
        "estimate_seconds": 14_400,
    },
    "code_change": {
        "title": "Fix the thing",
        "state": "open",
        "connection_id": "conn1",
        "author_account_id": "acc1",
        "created_at": "2026-01-01T00:00:00Z",
        "merged_at": "2026-01-02T03:04:05Z",
    },
    "team_person": {
        "display_name": "Aki",
        "accounts": [{"account_id": "acc1"}],
    },
}

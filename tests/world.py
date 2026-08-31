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

DEFAULTS: dict[str, Any] = {}
"""The settings document, and there is nothing in it.

A definition names no dial: its thresholds are figures or literals, its
calendars are fields on subjects' records, and an effort renders in hours.
The name survives because the engine still *accepts* a document at its door
-- a host mid-migration should find its old one inert rather than refused --
and because a suite that passed nothing would not prove that.
"""

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
    url_fields={
        "work_issue": "url",
        "work_container": "url",
        "code_change": "url",
    },
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

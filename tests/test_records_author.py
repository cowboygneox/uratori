"""`examples/records/author.py` -- the authoring aid (documents-plan-v3,
D4). Unit tests only: the prompt the script builds, and how it parses
Claude's reply, both against a stubbed client. No network call is ever
made from this file -- `propose()` takes `client` as a seam for exactly
that reason.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EXAMPLE = Path(__file__).parent.parent / "examples" / "records"


def _load_module():
    spec = importlib.util.spec_from_file_location("records_author", EXAMPLE / "author.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["records_author"] = module
    spec.loader.exec_module(module)
    return module


author = _load_module()

DECLARATION = (
    'extract measurement from medical_record_page:\n'
    '    over page_class.vitals\n'
    '    many by row up to 5\n'
    '    patient_id  = page_identity.patient_id\n'
    '    measured_at = date after any of ["Date:", "Visit date", "DOS"]\n'
    '    weight_kg   = number after any of ["Weight:", "Wt:"] in kg or lb\n'
)


def _failure_payload() -> dict:
    return {
        "extract": "measurement",
        "version": "abc123",
        "declaration": DECLARATION,
        "failures": [
            {
                "subject": "d1/p0002",
                "field": "weight_kg",
                "reason": "no alternative matched",
                "page_key": "d1/p0002",
                "words": [
                    {"id": 0, "text": "WEIGHT", "line": 0},
                    {"id": 1, "text": "(kg):", "line": 0},
                    {"id": 2, "text": "76", "line": 0},
                ],
            }
        ],
    }


def test_failures_of_reads_the_route_shape_into_the_dataclass() -> None:
    failures = author.failures_of(_failure_payload())
    assert len(failures) == 1
    [f] = failures
    assert f.subject == "d1/p0002"
    assert f.field == "weight_kg"
    assert f.reason == "no alternative matched"
    assert [w.text for w in f.words] == ["WEIGHT", "(kg):", "76"]


def test_build_prompt_carries_the_declaration_and_every_failure() -> None:
    failures = author.failures_of(_failure_payload())
    prompt = author.build_prompt("measurement", DECLARATION, failures)
    assert DECLARATION in prompt
    assert "d1/p0002" in prompt
    assert "no alternative matched" in prompt
    assert "weight_kg" in prompt
    # The page's own words, grouped back into one line, must be legible in
    # the prompt -- not a bag of tokens with no line between them.
    assert "WEIGHT (kg): 76" in prompt


def test_build_prompt_with_no_failures_asks_for_a_first_draft() -> None:
    prompt = author.build_prompt("measurement", DECLARATION, ())
    assert "first draft" in prompt


def test_build_prompt_includes_samples_when_given() -> None:
    sample = (author.Word(text="Weight:", line=0), author.Word(text="80", line=0))
    prompt = author.build_prompt("measurement", DECLARATION, (), sample_pages=(sample,))
    assert "already extract correctly" in prompt
    assert "Weight: 80" in prompt


def test_parse_candidate_extracts_the_one_fenced_block() -> None:
    reply = f"Here is the revised declaration:\n\n```\n{DECLARATION}\n```\n\nThat should do it."
    assert author.parse_candidate(reply) == DECLARATION.strip()


def test_parse_candidate_strips_a_language_tag_on_the_fence() -> None:
    reply = f"```fig\n{DECLARATION}\n```"
    assert author.parse_candidate(reply) == DECLARATION.strip()


def test_parse_candidate_refuses_a_reply_with_no_fence() -> None:
    with pytest.raises(ValueError, match="no fenced code block"):
        author.parse_candidate("just prose, no code block here")


def test_parse_candidate_refuses_more_than_one_fence() -> None:
    reply = f"```\n{DECLARATION}\n```\n\nor maybe\n\n```\nsomething else\n```"
    with pytest.raises(ValueError, match="expected exactly one"):
        author.parse_candidate(reply)


class _StubMessages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_kwargs: dict | None = None

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.last_kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


class _StubClient:
    def __init__(self, text: str) -> None:
        self.messages = _StubMessages(text)


def test_propose_calls_the_stubbed_client_and_returns_the_parsed_candidate() -> None:
    reply_text = f"```\n{DECLARATION}\n```"
    client = _StubClient(reply_text)
    failures = author.failures_of(_failure_payload())

    candidate = author.propose(
        "measurement", DECLARATION, failures, model="claude-opus-5-5", client=client
    )

    assert candidate == DECLARATION.strip()
    assert client.messages.last_kwargs is not None
    assert client.messages.last_kwargs["model"] == "claude-opus-5-5"
    [message] = client.messages.last_kwargs["messages"]  # type: ignore[index]
    assert "weight_kg" in message["content"]


def test_propose_never_touches_the_network_without_an_injected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `client` and no `ANTHROPIC_API_KEY` must fail locally, never
    attempt a real request -- the contract this whole test file relies on
    to stay network-free."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    failures = author.failures_of(_failure_payload())
    with pytest.raises(KeyError):
        author.propose("measurement", DECLARATION, failures)

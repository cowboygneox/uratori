"""The authoring aid for `extract` declarations (documents-plan-v3, D4).

A model's job in this language is never at run time -- the engine runs
only the deterministic patterns an `extract` declaration writes down
(`docs/language.md`, "extract -- records read off a page"). Its job is at
*authoring* time: reading a sample of a bundle's pages, or the failures a
first draft left behind, and proposing the alternative or matcher that
closes the gap. This script is that loop's second half -- the one D4
calls "closing gaps with it": read `GET /tenants/{t}/extracts/{name}/
failures`, hand each failure's reason, field, the declaration as written,
and the page's own words to Claude, and print the revised declaration it
proposes, to paste and recompile.

**Never run by the server.** This is a development tool an operator runs
by hand, where they choose, against a tenant they can already read --
nothing here writes a definition or a fact. Running it needs
`ANTHROPIC_API_KEY` and the `anthropic` package (`pip install anthropic`);
neither is a project dependency -- this script is example-only, so a model
API client stays out of `pyproject.toml`'s extras and the Docker image,
same as reportlab stays out for `generate.py`.

    export ANTHROPIC_API_KEY=...
    python examples/records/author.py --tenant records --extract measurement

Before writing any Claude code here, this file's author read the
`claude-api` skill for the model id and SDK usage; `claude-opus-5-5`
below is that skill's own current default, never guessed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL = "claude-opus-5-5"


@dataclass(frozen=True)
class Word:
    text: str
    line: int


@dataclass(frozen=True)
class Failure:
    subject: str
    field: str | None
    reason: str
    page_key: str
    words: tuple[Word, ...]


def _words_of(raw: list[dict[str, Any]]) -> tuple[Word, ...]:
    """A page's word layer, exactly as the failures route serves it
    (`docs/http-api.md`), kept in reading order (word id)."""
    return tuple(Word(text=w["text"], line=w["line"]) for w in sorted(raw, key=lambda w: w["id"]))


def failures_of(payload: dict[str, Any]) -> tuple[Failure, ...]:
    return tuple(
        Failure(
            subject=f["subject"],
            field=f.get("field"),
            reason=f["reason"],
            page_key=f["page_key"],
            words=_words_of(f.get("words", [])),
        )
        for f in payload.get("failures", [])
    )


def _page_text(words: tuple[Word, ...]) -> str:
    """The page's own words, grouped back into lines in reading order --
    the shape a person (or a model) reads off a scan, not a bag of
    tokens with no lines between them."""
    lines: dict[int, list[str]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w.text)
    return "\n".join(" ".join(lines[line]) for line in sorted(lines))


def build_prompt(
    extract_name: str,
    declaration: str,
    failures: tuple[Failure, ...],
    sample_pages: tuple[tuple[Word, ...], ...] = (),
) -> str:
    """The prompt handed to Claude: the declaration exactly as written,
    each failure's reason/field/page words, and -- when the caller asked
    for them -- a sample of pages that already extract correctly, so the
    model proposes an addition rather than a rewrite that breaks what
    already works (D4: "successes travel too, on request")."""
    sections = [
        "You are revising one `extract` declaration in uratori's definition "
        "language (docs/language.md, \"extract -- records read off a "
        "page\"). The engine runs only the deterministic patterns written "
        "in this declaration -- never a model at its own run time, only "
        "here, while a person is authoring it.",
        f"The declaration as written, for `{extract_name}`:\n\n```\n{declaration}\n```",
    ]
    if failures:
        parts = []
        for f in failures:
            page_lines = "\n".join(f"    {line}" for line in _page_text(f.words).splitlines())
            parts.append(
                f"- subject {f.subject}, field {f.field or '(whole record)'}: "
                f"{f.reason}\n  page words, in reading order:\n{page_lines}"
            )
        sections.append("Pages this declaration could not read:\n" + "\n".join(parts))
    else:
        sections.append("No failures were supplied -- propose a first draft.")
    if sample_pages:
        shown = "\n\n".join(_page_text(p) for p in sample_pages)
        sections.append("Pages that already extract correctly -- do not break these:\n" + shown)
    sections.append(
        "Propose a revised `extract` declaration -- the whole block, not a "
        "diff -- that reads every failing page above without breaking the "
        "pages that already work. Add alternatives or matchers from the "
        "language's own vocabulary (`number after`, `date after`, `text "
        "after`, a classification ladder, a copy of another extract's "
        "field); never a model-backed matcher, which does not exist in "
        "this language. Reply with exactly one fenced code block "
        "containing the revised declaration, and nothing else inside the "
        "fence."
    )
    return "\n\n".join(sections)


_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.DOTALL)


def parse_candidate(reply: str) -> str:
    """Pull the one fenced declaration out of Claude's reply. The
    authoring loop pastes this straight into a `.fig` file, so a reply
    with no fence, or more than one, is a `ValueError` rather than a
    guess at which block was meant."""
    fences: list[str] = _FENCE.findall(reply)
    if not fences:
        raise ValueError("no fenced code block in the reply")
    if len(fences) > 1:
        raise ValueError(f"{len(fences)} fenced code blocks in the reply -- expected exactly one")
    return fences[0].strip()


def _client(api_key: str) -> object:
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def propose(
    extract_name: str,
    declaration: str,
    failures: tuple[Failure, ...],
    sample_pages: tuple[tuple[Word, ...], ...] = (),
    *,
    model: str = DEFAULT_MODEL,
    client: object | None = None,
    api_key: str | None = None,
) -> str:
    """Build the prompt, ask Claude, parse the one candidate declaration
    out of the reply. `client` is a seam for tests -- anything with a
    `.messages.create(...)` returning an object whose `.content` holds
    blocks with `.type`/`.text`, the same shape `anthropic.Anthropic()`
    itself returns."""
    prompt = build_prompt(extract_name, declaration, failures, sample_pages)
    cl = client if client is not None else _client(api_key or os.environ["ANTHROPIC_API_KEY"])
    response = cl.messages.create(  # type: ignore[attr-defined]
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    return parse_candidate(text)


def _get(base: str, path: str, token: str | None) -> dict[str, Any]:
    request = urllib.request.Request(base.rstrip("/") + path, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request) as response:
            result: dict[str, Any] = json.loads(response.read())
            return result
    except urllib.error.HTTPError as refusal:
        detail = refusal.read().decode(errors="replace")
        sys.exit(f"GET {path} -> {refusal.code}: {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--tenant", default="records")
    parser.add_argument("--extract", required=True)
    parser.add_argument("--token", default=os.environ.get("URATORI_TOKEN"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--sample-pages",
        type=int,
        default=0,
        help="also show Claude this many already-succeeding pages' word "
        "layers, read from --sample-kind, so it adds rather than rewrites",
    )
    parser.add_argument(
        "--sample-kind", default=None, help="the document kind to sample pages from"
    )
    arguments = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY is not set")

    payload = _get(
        arguments.base,
        f"/tenants/{arguments.tenant}/extracts/{arguments.extract}/failures",
        arguments.token,
    )
    failures = failures_of(payload)
    print(
        f"{arguments.extract} @ {payload['version']}: {len(failures)} failing subject(s)",
        file=sys.stderr,
    )

    samples: list[tuple[Word, ...]] = []
    if arguments.sample_pages and arguments.sample_kind:
        docs = _get(
            arguments.base,
            f"/tenants/{arguments.tenant}/documents/{arguments.sample_kind}"
            f"?limit={arguments.sample_pages}",
            arguments.token,
        )
        for doc in docs.get("documents", [])[: arguments.sample_pages]:
            words_payload = _get(
                arguments.base,
                f"/tenants/{arguments.tenant}/documents/{arguments.sample_kind}/"
                f"{doc['id']}/pages/1/words",
                arguments.token,
            )
            samples.append(_words_of(words_payload.get("words", [])))

    candidate = propose(
        arguments.extract,
        payload["declaration"],
        failures,
        tuple(samples),
        model=arguments.model,
        api_key=api_key,
    )
    print(candidate)


if __name__ == "__main__":
    main()

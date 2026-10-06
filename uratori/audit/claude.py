"""Claude via the Anthropic SDK -- a real second reader, behind the
`audit` extra. Selected by `URATORI_AUDIT_PROVIDER=claude`.

`anthropic` is imported lazily, inside `__init__`, so it is never a
dependency of the pure half of this package (`judge.py`) or of a
deployment that never sets the env var -- the same lazy-import discipline
`examples/records/author.py` already uses for the same reason (package 4).

The call is one message, non-streaming: a page image (when the renderer
produced one) plus the prompt plus a numbered word list, asking for one
fenced JSON block back. Structured-output enforcement is deliberately not
used here -- a fenced-JSON-block-and-parse round trip is the same pattern
`author.py` already uses for a one-shot structured answer, and it needs no
schema registration to get right. Effort is `low`: reading a short list of
declared fields off one page is a bounded extraction, not a reasoning
task.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from ..server.words import Word
from .judge import FieldReading
from .provider import AuditProviderReading, FieldToRead

if TYPE_CHECKING:
    # Type-checking only: `anthropic` stays an optional, lazily-imported
    # dependency at runtime (module docstring) -- this import never
    # executes, mypy only needs it to resolve the `cast` target below.
    from collections.abc import Iterable

    from anthropic.types import MessageParam

DEFAULT_MAX_TOKENS = 4096

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


class ClaudeAuditProvider:
    def __init__(self, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        import anthropic  # local import -- see module docstring

        self._client = anthropic.AsyncAnthropic()
        self._max_tokens = max_tokens

    async def read_page(
        self,
        *,
        prompt: str,
        model: str,
        page_png: bytes | None,
        words: Sequence[Word],
        fields: Sequence[FieldToRead],
        page_key: str,
    ) -> AuditProviderReading:
        del page_key  # not sent to the model; kept for provider-side logging only
        content: list[dict[str, object]] = []
        if page_png is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(page_png).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": _full_prompt(prompt, words)})

        # The content blocks are built dynamically (an image block only
        # when the renderer produced one), so they are handed to the SDK as
        # plain dicts rather than its own TypedDicts -- `cast` here, not a
        # hand-typed reconstruction of `MessageParam`'s content union,
        # which the SDK itself validates at the wire boundary regardless.
        response = await self._client.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            output_config={"effort": "low"},
            messages=cast("Iterable[MessageParam]", [{"role": "user", "content": content}]),
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return AuditProviderReading(response=text, fields=_parse(text, fields))


def _full_prompt(prompt: str, words: Sequence[Word]) -> str:
    word_list = "\n".join(f"{w.id}: {w.text}" for w in sorted(words, key=lambda w: w.id))
    return (
        f"{prompt}\n\n"
        "Numbered word list for this page (id: text):\n"
        f"{word_list}\n\n"
        "Answer with exactly one fenced JSON block, no other text outside it, "
        'of the shape: ```json\n{"readings": [{"extract": "...", "field": "...", '
        '"row": 0, "status": "seen" | "not_on_page" | "cannot_read", '
        '"words": [1, 2], "seen_text": "..."}]}\n```'
    )


def _parse(text: str, fields: Sequence[FieldToRead]) -> tuple[FieldReading, ...]:
    m = _FENCE.search(text)
    if m is None:
        # No parseable answer at all: every field is `cannot_read` rather
        # than silently empty -- an audit with no reading is `unaudited`,
        # and that must stay a stated absence, never confused with "the
        # model looked and found nothing to say", which this is.
        return tuple(
            FieldReading(extract=f.extract, field=f.field, row=0, status="cannot_read")
            for f in fields
        )
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        return tuple(
            FieldReading(extract=f.extract, field=f.field, row=0, status="cannot_read")
            for f in fields
        )
    out: list[FieldReading] = []
    for row in parsed.get("readings", []):
        status = row.get("status")
        if status not in ("seen", "not_on_page", "cannot_read"):
            continue
        out.append(
            FieldReading(
                extract=str(row.get("extract", "")),
                field=str(row.get("field", "")),
                row=int(row.get("row", 0)),
                status=status,
                words=tuple(int(w) for w in row.get("words", ())),
                seen_text=row.get("seen_text"),
            )
        )
    return tuple(out)

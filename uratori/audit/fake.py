"""A deterministic provider, from the word layer alone -- no network, no
model, selected by `URATORI_AUDIT_PROVIDER=fake`.

For each verified field, scans the page's words in reading order (grouped
by line, so a `many by row` extract's flowsheet gets one candidate per
line) for the first token matching the field's declared type, reusing the
same primitives `uratori.audit.judge` parses a reading with -- a number
via the unit table, a date via the date grammar. This is deliberately a
*different* (simpler) algorithm than an extract's own matcher: it has no
alternatives to search for, so it finds the first number/date/word on a
line full stop, which is exactly what makes it useful as a second reader
rather than a mirror of the first -- a page with an unrelated number
earlier in reading order than the one the extract's label-anchored search
found genuinely disagrees, the same way an inattentive human reader might.

`focus` lets a test pin an exact answer (status, word ids, transcription)
for one `(page_key, extract, field)`, for engineering a specific
disagreement, miss, or "cannot read" without hand-building a whole page of
text -- test ergonomics, not part of the `AuditProvider` protocol itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..documents.extract import _parse_date_tokens, _read_number
from ..server.words import Word
from .judge import FieldReading, ReadingStatus
from .provider import AuditProviderReading, FieldToRead


@dataclass(frozen=True)
class FakeAnswer:
    status: ReadingStatus = "seen"
    words: tuple[int, ...] = ()
    seen_text: str | None = None
    anchored: bool = True
    box: tuple[float, float, float, float] | None = None


class FakeAuditProvider:
    def __init__(self, focus: Mapping[tuple[str, str, str], Sequence[FakeAnswer]] | None = None) -> None:
        self._focus: dict[tuple[str, str, str], list[FakeAnswer]] = {
            k: list(v) for k, v in (focus or {}).items()
        }

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
        del prompt, model, page_png  # unread: this provider is not a model
        out: list[FieldReading] = []
        for f in fields:
            forced = self._focus.get((page_key, f.extract, f.field))
            if forced is not None:
                for row, answer in enumerate(forced):
                    out.append(
                        FieldReading(
                            extract=f.extract,
                            field=f.field,
                            row=row,
                            status=answer.status,
                            words=answer.words,
                            box=answer.box,
                            seen_text=answer.seen_text,
                            anchored=answer.anchored,
                        )
                    )
                continue
            out.extend(_scan(f, words))
        return AuditProviderReading(response="fake-provider: scanned the word layer", fields=tuple(out))


def _lines_of(words: Sequence[Word]) -> list[list[Word]]:
    by_line: dict[int, list[Word]] = {}
    for w in sorted(words, key=lambda w: w.id):
        by_line.setdefault(w.line, []).append(w)
    return [by_line[line] for line in sorted(by_line)]


def _scan(f: FieldToRead, words: Sequence[Word]) -> list[FieldReading]:
    out: list[FieldReading] = []
    row = 0
    for line_words in _lines_of(words):
        found = _scan_line(f, line_words)
        if found is None:
            continue
        cited, text = found
        out.append(
            FieldReading(
                extract=f.extract,
                field=f.field,
                row=row,
                status="seen",
                words=tuple(w.id for w in cited),
                seen_text=text,
            )
        )
        row += 1
    if not out:
        out.append(
            FieldReading(extract=f.extract, field=f.field, row=0, status="not_on_page")
        )
    return out


def _scan_line(f: FieldToRead, line_words: list[Word]) -> tuple[list[Word], str] | None:
    if f.type == "number":
        found = _read_number(line_words, 0, f.units)
        if found is None or isinstance(found, str):
            return None
        _value, _unit, cited = found
        return cited, " ".join(w.text for w in cited)
    if f.type == "moment":
        for start in range(len(line_words)):
            tokens = [w.text for w in line_words[start : start + 3]]
            parsed = _parse_date_tokens(tokens)
            if parsed is not None and parsed != "ambiguous":
                _iso, consumed = parsed
                cited = line_words[start : start + consumed]
                return cited, " ".join(w.text for w in cited)
        return None
    if f.type == "text":
        for w in line_words:
            if ":" not in w.text and w.text.strip():
                return [w], w.text
        return None
    return None

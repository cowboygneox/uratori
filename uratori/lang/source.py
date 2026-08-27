"""A declaration, split into what it *says* and what it *does*.

"Every number, backed by evidence" means traceable to the event that moved it
*and* to a written definition of how it was computed, and v1 half-shipped the
second: the source reached two of its six declaration kinds, both two clicks
deep. Everything added after that -- joins, presence tests, extremes, flag
templates, spans -- was live, versioned, cited on screen, and readable only by
checking the repository out. It was reported by the maintainer as *"I click on
it but don't see a formula so I cannot review the language changes"*, which is
the honest description of what that gap looks like from outside.

So there is one extractor, it covers every kind, and the API serves it.

## Prose and formula are separated here, not by the reader

The first version answered with the whole block -- the comment above it, the
docstring, the `display` template and the calculation -- under one heading. Read
on screen that is a page of paragraphs with four lines of arithmetic somewhere in
the middle, and the reader who came to check the arithmetic has to find it. It
was reported the same way the first gap was: *"all I want to see is the meat and
potatoes of the formula"*.

Two functions now, and the split is the same one the screen already draws:

- `declaration_prose` is the explanation -- the `#` comment lines above the
  declaration, the one spelling every declaration kind shares. (The language
  once also had a docstring inside the block, Python-style; it buried the
  directives it sat among, and the two spellings meant a reader had to know a
  declaration's kind to find out what it means.)
- `declaration_source` is the calculation, with the prose and the `display`
  template removed. `display` is a sentence for a card to print; on a page
  showing the formula it is one more paragraph in the way of it.

`lex.prose_above` is the one implementation of what counts as an explanation;
the parser attaches docs with it and this module serves prose with it, so the
two can never disagree about which lines belong to a declaration.

Neither is in the version hash, which is why this can be reorganised freely:
rewording an explanation must not fork a version and recompute every value.

## The scan crosses one blank line

v1's first version required the comment to be contiguous with the declaration,
and a review measured what that cost: a third of its declarations rendered as a
bare line with their explanation stranded one blank line above.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .lex import prose_above
from .plan import Library

_HEADERS = (
    r"^fact\s+{name}\s*:",
    r"^(?:group|filter)\s+{name}\s",
    r"^measure\s+{name}\s*=",
    r"^figure\s+{name}(\s+across\s+\w+)?\s*:",
    r"^reading\s+{name}\s*\(",
    r"^projection\s+{name}\s*:",
    r"^summarise\s+{name}\s+over\s",
    r"^bundle\s+{name}\s*:",
)


@lru_cache(maxsize=8)
def _lines(source: str) -> tuple[str, ...]:
    """Split once per library rather than per lookup.

    A projection's pane asks for its own block plus every definition it rests
    on, which for a large one is a couple of dozen lookups over a quarter of a
    megabyte.
    """
    return tuple(source.split("\n"))


_DISPLAY = re.compile(r'^\s*display\s+"')


def _locate(library: Library, name: str) -> tuple[tuple[str, ...], int, int] | None:
    """The lines, and where this declaration's header and body sit.

    Returned as one tuple because both public functions need the same
    boundaries, and computing them twice is how the two drift into disagreeing
    about which lines belong to which -- at which point a paragraph is either
    printed under both headings or under neither.
    """
    lines = _lines(library.source)
    pattern = re.compile("|".join(h.format(name=re.escape(name)) for h in _HEADERS))

    at = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    if at is None:
        return None

    # A column-0 comment does not end the block: the lexer skips it wherever
    # it sits, so the block continues past it -- and a scan that stopped there
    # served a formula with its calculate silently missing. The walk-back then
    # sheds the trailing comment/blank run, which belongs to whatever comes
    # next (typically the following declaration's explanation).
    end = at + 1
    while end < len(lines) and (
        lines[end].strip() == "" or lines[end][:1].isspace() or lines[end].startswith("#")
    ):
        end += 1
    while end > at + 1 and (lines[end - 1].strip() == "" or lines[end - 1].startswith("#")):
        end -= 1

    return lines, at, end


def declaration_source(library: Library, name: str) -> str | None:
    """The calculation, and nothing else.

    The explanation lives above the header, so the block needs only `display`
    taken out. What is left is what the engine actually evaluates, which is
    the thing somebody clicking "the definition, as written" came to check.
    """
    found = _locate(library, name)
    if found is None:
        return None
    lines, at, end = found

    body = [line for line in lines[at:end] if not _DISPLAY.match(line)]

    # Removing `display` can leave a run of blank lines where it sat. Trimming
    # them here rather than in the browser keeps the served text and the
    # rendered text the same thing, which is the whole point of this route.
    return "\n".join(_collapse(body)).rstrip() or None


def _collapse(lines: list[str]) -> list[str]:
    """Squeeze runs of blanks to one, and none directly under the header.

    Without the second rule a declaration whose `display` sat first opens with
    a gap where the template used to be -- which reads as a formula missing its
    first line rather than as one with a sentence removed.
    """
    out: list[str] = []
    for line in lines:
        if line.strip():
            out.append(line)
        elif len(out) > 1 and out[-1].strip():
            out.append("")
    return out


def declaration_prose(library: Library, name: str) -> str:
    """What this declaration means, in the author's words.

    The `#` comment lines above it, via the same `prose_above` the parser
    attaches docs with, so the two can never disagree about which lines are
    the explanation.

    Empty string rather than None when there is none (an index or a measure
    may go uncommented), because the caller puts this in a `doc` field that is
    already a string for every other kind and a second absent-value spelling
    would be a second branch on every screen.
    """
    found = _locate(library, name)
    if found is None:
        return ""
    lines, at, _ = found
    return prose_above(lines, at + 1)

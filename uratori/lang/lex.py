"""Tokenizer for the definition language.

Indentation is significant, Python-style, because the whole point of the
language is that somebody who does not write code can read a definition and say
whether it is right. Braces would cost a line of noise per block for no gain in
precision.

Two deliberate simplicities:

  - **Keywords are not reserved.** `group`, `filter`, `figure`, `depends` and
    the rest come out as name tokens and the parser matches on their text. A
    reserved word list is a thing that grows and then collides with somebody's
    filter called `display`, and nothing here is ambiguous without it.

  - **No line continuation.** Every statement is one line. The grammar has no
    construct long enough to need wrapping, and adding continuation before
    anything needs it is how a lexer acquires rules nobody can remember. A
    definition that wants to wrap should be split into named sets instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

TokenKind: TypeAlias = Literal[
    "name", "string", "number", "op", "newline", "indent", "dedent", "eof"
]


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: str
    line: int
    column: int


class DefinitionError(Exception):
    """A definition that cannot load, whichever layer refused it. The public
    contract for a host's except clause: `SyntaxError_` (lexer and parser) and
    `CheckError` (checker) are both this, so one guard covers the whole
    compile -- a host that caught only the checker would let a missing colon
    crash straight through the boot path it thought it had guarded."""


class SyntaxError_(DefinitionError):
    """A definition that does not lex. Named with a trailing underscore so it
    cannot be confused with the builtin, which means something else entirely."""

    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(f"line {line}: {message}")
        self.message = message
        self.line = line
        self.column = column


# `>` and `<` are matched *after* the two-character operators so that `>=` is
# one token rather than two. That ordering is the whole of the longest-match
# rule and it lives in `_lex_line` rather than in these lists.
#
# `+`, `*` and `/` are here for arithmetic, and `-` was already here as set
# difference. That overload is real and it is resolved by block rather than by
# token: a `depends` block parses a set expression where `-` can only be
# difference, and `calculate` parses a value expression where it can only be
# subtraction. Nothing parses both, so nothing has to guess -- which is why `-`
# did not need a second spelling and a dimension did.
SINGLE = frozenset(":=&|-+*/(){},<>")
DOUBLE = ("==", "!=", ">=", "<=")

_NAME_START = re.compile(r"[A-Za-z_]")
_NAME_PART = re.compile(r"[A-Za-z0-9_.]")
_NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")


def _strip_comment(raw: str) -> str:
    """Drop a trailing `#` comment, respecting string literals.

    A naive split on `#` would truncate `label "waiting #1"`, and worse, would
    do it silently -- the definition would still compile and the label would be
    cut in half on screen.
    """
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"' and (i == 0 or raw[i - 1] != "\\"):
            in_string = not in_string
        if ch == "#" and not in_string:
            break
        out.append(ch)
        i += 1
    return "".join(out)


# A rule line, in the three shapes real files draw them: the banner a
# concatenating build writes between two .fig files (bare dashes, or dashes
# carrying a file name), and the section rule a long file draws between its
# own regions (`# ------------- reviews --` -- a dash run at BOTH ends).
# Deliberately NOT any comment that merely starts with dashes -- `# --- see
# the note below` is somebody's writing, and a pattern that swallowed it
# refused explanations while telling the author to write exactly what they
# had written.
_BANNER = re.compile(r"^#\s*-{3,}\s*$|^#\s*-{3,}.*\.fig|^#\s*-{3,}.*-{2,}\s*$")


def _is_prose(line: str) -> bool:
    """A comment line, but not the banner between two files.

    Without the banner carve-out, the first declaration in every concatenated
    file adopts the previous file's banner as its own explanation -- it shows
    up immediately as a heading that belongs to the wrong file.
    """
    return line.startswith("#") and not _BANNER.match(line)


def prose_above(lines: Sequence[str], header: int, *, indented: bool = False) -> str:
    """The contiguous `#` run directly above line `header` (1-based).

    This is the one implementation of what counts as a declaration's
    explanation -- the parser attaches docs with it and `source.py` serves
    prose with it, so the two can never disagree about which lines belong to
    a declaration.

    Crosses at most one blank line: the origin project measured what strict
    contiguity cost, and a third of its declarations rendered bare with the
    explanation stranded a single blank line up. Two blanks is a detached
    paragraph, and adopting it would attach prose the author never aimed
    here. A banner also ends the run: what sits above one belongs to the
    previous file.

    `indented` is for the one place prose lives inside a block -- the run
    above a fact's field -- where the `#` sits at the field's own indent. A
    declaration header is at column 0 and keeps the strict rule, so a
    stranded indented comment can never become a declaration's explanation.
    """

    def prose(line: str) -> bool:
        if not indented:
            return _is_prose(line)
        # The comment must itself be indented: a column-0 `#` inside a block
        # is a stray note (or the next declaration's explanation), and
        # adopting it would serve somebody's TODO as a field's description.
        return line[:1] in (" ", "\t") and _is_prose(line.lstrip())

    i = header - 2
    if i >= 0 and lines[i].strip() == "":
        i -= 1
    run: list[str] = []
    while i >= 0 and prose(lines[i]):
        run.append(lines[i].lstrip().lstrip("#").strip())
        i -= 1
    run.reverse()
    return "\n".join(run).strip()


def lex(source: str) -> list[Token]:
    tokens: list[Token] = []
    # A stack of column positions, never popped below the base level. Python's
    # rule: a line indented to a column not on the stack is an error rather than
    # a guess, because guessing turns a typo into a silently different block.
    indents = [0]
    lines = source.split("\n")

    i = 0
    while i < len(lines):
        raw = lines[i]
        lineno = i + 1
        i += 1

        stripped = _strip_comment(raw)
        if stripped.strip() == "":
            continue

        indent = len(stripped) - len(stripped.lstrip())
        if indent > indents[-1]:
            indents.append(indent)
            tokens.append(Token("indent", "", lineno, indent))
        else:
            while indent < indents[-1]:
                indents.pop()
                tokens.append(Token("dedent", "", lineno, indent))
            if indent != indents[-1]:
                raise SyntaxError_(
                    f"indentation of {indent} does not match any enclosing block", lineno, indent
                )

        body = stripped[indent:]
        if body.startswith('"""'):
            # Refused by name rather than left to shatter into string tokens:
            # this was the language's docstring spelling once, and the author
            # typing it deserves directions, not "unterminated string".
            raise SyntaxError_(
                "a docstring. A declaration's explanation is written as `#` comment "
                "lines directly above it, not inside the block",
                lineno,
                indent,
            )

        _lex_line(body, lineno, indent, tokens)

    while len(indents) > 1:
        indents.pop()
        tokens.append(Token("dedent", "", len(lines), 0))
    tokens.append(Token("eof", "", len(lines), 0))
    return tokens


def _lex_line(body: str, line: int, offset: int, tokens: list[Token]) -> None:
    col = 0
    while col < len(body):
        ch = body[col]
        if ch in " \t":
            col += 1
            continue

        if ch == '"':
            end = col + 1
            buf: list[str] = []
            while end < len(body) and body[end] != '"':
                if body[end] == "\\" and end + 1 < len(body):
                    buf.append(body[end + 1])
                    end += 2
                    continue
                buf.append(body[end])
                end += 1
            if end >= len(body):
                raise SyntaxError_("a string was opened and never closed", line, offset + col)
            tokens.append(Token("string", "".join(buf), line, offset + col))
            col = end + 1
            continue

        two = body[col : col + 2]
        if two in DOUBLE:
            tokens.append(Token("op", two, line, offset + col))
            col += 2
            continue

        if ch in SINGLE:
            tokens.append(Token("op", ch, line, offset + col))
            col += 1
            continue

        if ch.isdigit():
            m = _NUMBER.match(body, col)
            assert m is not None
            tokens.append(Token("number", m.group(0), line, offset + col))
            col = m.end()
            continue

        if _NAME_START.match(ch):
            end = col
            while end < len(body) and _NAME_PART.match(body[end]):
                end += 1
            tokens.append(Token("name", body[col:end], line, offset + col))
            col = end
            continue

        raise SyntaxError_(f'unexpected character "{ch}"', line, offset + col)

    tokens.append(Token("newline", "", line, offset + len(body)))

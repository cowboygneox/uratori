"""The default prompt, and the one placeholder rule a template follows.

Pure string work -- no I/O, no store. The worker resolves `read:` bindings
to this page's actual values (`uratori.server.audit_worker`) and passes
the result in as `bound`; this module only renders.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ..lang.plan import AuditPlan
from .provider import FieldToRead

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def interpolate(template: str, bound: Mapping[str, str]) -> str:
    """`{name}` -> `bound[name]`. The checker has already refused any name
    not in `bound`, so a miss here would mean the compiled plan disagrees
    with what the checker approved -- never a reason to print the word
    `undefined` at the model."""
    return _PLACEHOLDER.sub(lambda m: bound.get(m.group(1), f"{{{m.group(1)}}}"), template)


def default_prompt(fields: Sequence[FieldToRead]) -> str:
    """Built from the verified fields alone (D6): name, type, unit, the
    `#` prose -- never the extract's alternatives or its current value."""
    lines = [
        "Read this page carefully and answer the following fields. For each "
        'field, answer EITHER the exact word ids (from the numbered word list '
        'below) that the value is printed in, OR the literal text "not on this '
        'page" if the page does not carry it, OR "cannot read" if it is there '
        "but illegible. If a field repeats on this page (a flowsheet with "
        "several dated rows), answer it once per row you can find, in the "
        "order the rows appear on the page.",
        "",
        "Fields to read:",
    ]
    for f in fields:
        unit = f" ({'/'.join(f.units)})" if f.units else ""
        prose = f" -- {f.prose}" if f.prose else ""
        lines.append(f"- {f.extract}.{f.field}: a {f.type or 'value'}{unit}{prose}")
    return "\n".join(lines)


def build_prompt(audit: AuditPlan, fields: Sequence[FieldToRead], bound: Mapping[str, str]) -> str:
    if audit.prompt is not None:
        return interpolate(audit.prompt, bound)
    base = default_prompt(fields)
    if audit.context is not None:
        base += "\n\n" + interpolate(audit.context, bound)
    return base

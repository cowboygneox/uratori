"""uratori (裏取り) -- a definition engine.

Every number a host serves is computed by a written, versioned definition:
figures stored and recomputed incrementally as facts move, readings evaluated
over stored days, projections assembled at the instant they are asked. The
host declares its world once (`Schema`), compiles its definitions
(`compile_source`), chooses storage (`uratori.store`), and drives the engine
through `Uratori`.

The name is the point: 裏付け (urazuke) is the backing a claim has; 裏取り
(uratori) is the act of going and getting it.
"""

from .engine.activity import SHOWN_KEEP
from .engine.buckets import SEPARATOR
from .engine.change import Change, Outcome
from .engine.engine import Engine
from .facade import DEFAULT_TRAILING, Listener, RunReport, Uratori
from .lang.check import CheckError, compile_source
from .lang.lex import DefinitionError, SyntaxError_
from .lang.plan import Library, Value
from .results import (
    Availability,
    BundleMemberResult,
    BundleResult,
    Flag,
    Level,
    Ok,
    Result,
    Row,
    Subject,
    Unavailable,
    Unit,
    Window,
)
from .schema import EFFORT_HOURS_SETTING, Schema
from .store import (
    BucketChange,
    EngineStore,
    FactRow,
    FactSource,
    MemoryEngineStore,
    MemoryFactStore,
    Pointer,
    StoredValue,
)
from .verify import FactError, verify_writes
from .windows import MAX_REACH_DAYS, WindowError, WindowSpec

__all__ = [
    "DEFAULT_TRAILING",
    "EFFORT_HOURS_SETTING",
    "MAX_REACH_DAYS",
    "SEPARATOR",
    "SHOWN_KEEP",
    "Availability",
    "BucketChange",
    "BundleMemberResult",
    "BundleResult",
    "Change",
    "CheckError",
    "DefinitionError",
    "Engine",
    "EngineStore",
    "FactError",
    "FactRow",
    "FactSource",
    "Flag",
    "Level",
    "Library",
    "Listener",
    "MemoryEngineStore",
    "MemoryFactStore",
    "Ok",
    "Outcome",
    "Pointer",
    "Result",
    "Row",
    "RunReport",
    "Schema",
    "StoredValue",
    "Subject",
    "SyntaxError_",
    "Unavailable",
    "Unit",
    "Uratori",
    "Value",
    "Window",
    "WindowError",
    "WindowSpec",
    "compile_source",
    "verify_writes",
]

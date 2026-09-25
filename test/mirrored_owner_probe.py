"""Purge-and-reimport probe for the mirrored-owner rule, and the discovery it uses.

Importing a module binds it onto its parent package: ``importlib`` finishes a load
with ``setattr(parent, child, module)``. So purging an owner and importing it again
changes THREE places -- the ``sys.modules`` entry, the parent package's attribute,
and any memo the mirroring module keeps -- while a test that restores only the first
leaves the parent naming one object and ``sys.modules`` another. That residue lasts
for the worker's life and is the very split-brain the rule exists to forbid, so the
probe runs in a child process and the residue leaves with it. Nothing is restored,
because nothing in the parent process is disturbed.

``main`` is that child: it takes module names, probes each, and prints one JSON
object per line. The functions are importable so the parent process can pin the
discovery rule directly rather than trusting it.
"""

from __future__ import annotations

import importlib
import json
import sys
from types import ModuleType

#: Value written onto the freshly imported owner, then looked for through the mirror.
SENTINEL = "kiro-crew-fresh-owner-sentinel"

_ABSENT = object()


def one_reexported_pair(module: ModuleType) -> tuple[str, str] | None:
    """Return one ``(attribute, owner module name)`` pair *module* re-exports.

    Discovered rather than configured: each mirroring module spells its own table,
    so this reads the module's own dicts and accepts only a pair it can prove -- the
    owner imports, lives in this same top-level package, and really holds that
    attribute. The three spellings a value can take are a dotted module name, a bare
    submodule name relative to the mirroring module or its parent, and an
    ``(owner, symbol)`` pair; a module object is read for its own ``__name__``.

    Keys are walked in sorted order, so the pair is the same on every run and a
    failure names the same attribute each time. Two kinds of value are passed over:
    a module, which the import system rebinds itself, and a ``__future__`` feature
    flag, which lands in every module that uses it and so looks owned while naming
    no control.
    """
    root = module.__name__.split(".")[0]
    parent = module.__name__.rpartition(".")[0]
    # Snapshotted: importing a candidate below binds that submodule onto its parent,
    # which mutates this very namespace while it is being read.
    for attribute, table in list(vars(module).items()):
        if attribute.startswith("__") or not isinstance(table, dict):
            continue
        for key in sorted(k for k in table if isinstance(k, str) and not k.startswith("__")):
            value = table[key]
            if isinstance(value, ModuleType):
                candidates = [value.__name__]
            elif isinstance(value, tuple) and value and isinstance(value[0], str):
                candidates = [value[0], f"{module.__name__}.{value[0]}", f"{parent}.{value[0]}"]
            elif isinstance(value, str):
                candidates = [value, f"{module.__name__}.{value}", f"{parent}.{value}"]
            else:
                continue
            for candidate in candidates:
                if not candidate.startswith(f"{root}.") or candidate == module.__name__:
                    continue
                try:
                    owner = importlib.import_module(candidate)
                except Exception:
                    continue
                held = getattr(owner, key, _ABSENT)
                if held is _ABSENT or isinstance(held, ModuleType):
                    continue
                if type(held).__module__ == "__future__":
                    continue
                return key, candidate
    return None


def probe(name: str) -> dict[str, object]:
    """Purge one module's owner, import it again, and report what the mirror sees.

    The verdict has two parts, keyed to the two ways a second storage location
    shows itself: ``read_resolves`` is whether a read through the mirror answers
    from the owner ``sys.modules`` now holds, and ``retained`` names any mapping
    still holding the discarded module.

    A module this probe cannot measure -- one that does not import, or whose table
    spelling the discovery rule does not recognise -- reports ``unmeasurable`` and
    the parent turns that into a failure. A probe that cannot run is a red: a
    verdict nobody can produce is indistinguishable from a rule nobody checks.
    """
    result: dict[str, object] = {"module": name}
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        result["unmeasurable"] = f"does not import: {type(exc).__name__}"
        return result

    pair = one_reexported_pair(module)
    if pair is None:
        result["unmeasurable"] = "no re-exported attribute discovered"
        return result
    attribute, owner_name = pair
    result["attribute"] = attribute
    result["owner"] = owner_name

    purged = sys.modules.pop(owner_name)
    fresh = importlib.import_module(owner_name)
    result["reimport_is_new"] = fresh is not purged
    setattr(fresh, attribute, SENTINEL)

    try:
        read = getattr(module, attribute)
    except Exception as exc:
        result["read_resolves"] = False
        result["read"] = f"raised {type(exc).__name__}"
    else:
        result["read_resolves"] = read == SENTINEL
        result["read"] = SENTINEL if read == SENTINEL else f"{type(read).__name__} from elsewhere"

    result["retained"] = sorted(
        attr
        for attr, value in list(vars(module).items())
        if isinstance(value, dict) and purged in value.values()
    )
    return result


def main(argv: list[str]) -> int:
    for name in argv:
        print(json.dumps(probe(name)))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a child process
    raise SystemExit(main(sys.argv[1:]))

"""The apps whose wheel copy still ships, held by name for the tests that walk the copies.

A test that walks the wheel copies under ``src/kiro_crew/apps/builtins/`` needs a floor
under its walk, or a walk that finds nothing passes every case it parametrizes. A count
floor (``>= 20``) is the wrong one while the copies are deleted one per pull request:
each deletion lowers what the walk finds, so the floor turns red on whichever unrelated
deletion crosses it, and two deletions that land together each pass it.

``shipped_wheel_copies.txt`` names every copy on its own line instead, and a walk is
compared with it exactly: :func:`census_drift` names what the walk dropped and added. A
deletion removes its own line, so two deletions merge into the right list or conflict as
text. The list only shrinks, because Kiro Crew accepts no new built-in app. The frontend
tests read the same file (``website/src/test/shippedWheelCopies.ts``). No app is named here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

LIST = Path(__file__).with_name("shipped_wheel_copies.txt")
_BUILTINS = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "apps" / "builtins"


def pinned_wheel_copies() -> frozenset[str]:
    """The app names the list holds: every line that is neither blank nor a ``#`` comment."""
    lines = (line.strip() for line in LIST.read_text(encoding="utf-8").splitlines())
    return frozenset(line for line in lines if line and not line.startswith("#"))


def drift(found: Iterable[str], expected: Iterable[str]) -> str:
    """``""`` when the two name sets are equal, else what *found* dropped and added."""
    seen, wanted = frozenset(found), frozenset(expected)
    if seen == wanted:
        return ""
    return f"dropped {sorted(wanted - seen)}, added {sorted(seen - wanted)}"


def census_drift(found: Iterable[str]) -> str:
    """``""`` when a walk of the wheel copies found exactly the listed ones, else what to do."""
    differs = drift(found, pinned_wheel_copies())
    if not differs:
        return ""
    return (
        f"the walk found other wheel copies than {LIST.name} lists: {differs}. A pull"
        " request that deletes a copy removes its line there. A dropped copy that still"
        " ships means the walk stopped reading it: fix the walk. An added copy is a new"
        " built-in app, which Kiro Crew does not accept."
    )


def declared_agent_names() -> frozenset[str]:
    """The ``name`` of every agent spec a listed copy's ``app.json`` declares in ``agents``."""
    names: set[str] = set()
    for app in sorted(pinned_wheel_copies()):
        root = _BUILTINS / app.replace("-", "_")
        try:
            manifest = json.loads((root / "app.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AssertionError(
                f"{LIST.name} lists {app}, which ships no app.json: remove its line"
            ) from exc
        for spec in manifest.get("agents", []):
            names.add(json.loads((root / spec).read_text(encoding="utf-8"))["name"])
    return frozenset(names)

"""Every crewmate prompt piece, read from one file a maintainer can find and tune.

The text a crewmate is told -- its conductor's system section, its worker's prompt,
each gateway wake (heartbeat, check-ins, goal setup, milestones, worker report,
upgrade notice, welcome) and the hand-off capability note -- lives in
``crewmate_prompts.json`` beside this module, one key per piece. The modules that
assemble a prompt keep only the assembly: which piece, in what order, filled with
what. ``_about`` in the same file names, per key, where it is assembled and which
placeholders it takes.

A value is a string, or a list of lines joined with ``"\\n"`` (for a piece that is
read as lines). :func:`text` returns a piece as it is; :func:`fill` fills its named
``str.format`` placeholders and refuses a missing or an unknown one, so a renamed
placeholder fails at the first call rather than shipping a prompt with a hole.

``scripts/crewmate_prompt_preview.py`` assembles every piece from a fixture offline,
so a wording change can be read and diffed without a running gateway.
"""

from __future__ import annotations

import json
import string
from functools import lru_cache
from pathlib import Path
from typing import Any

#: The file every piece is read from (package data).
PROMPTS_PATH = Path(__file__).with_name("crewmate_prompts.json")
#: The key holding each piece's note: where it is assembled, its placeholders.
ABOUT_KEY = "_about"


class PromptError(KeyError):
    """A piece that is not in the file, or a fill whose placeholders do not match."""


@lru_cache(maxsize=1)
def _table() -> dict[str, str]:
    raw = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    about = raw.get(ABOUT_KEY) or {}
    pieces: dict[str, str] = {}
    for key, value in raw.items():
        if key == ABOUT_KEY:
            continue
        if isinstance(value, list) and all(isinstance(line, str) for line in value):
            value = "\n".join(value)
        if not isinstance(value, str):
            raise PromptError(f"crewmate prompt {key!r} is neither text nor a list of lines")
        if key not in about:
            raise PromptError(f"crewmate prompt {key!r} has no {ABOUT_KEY} entry")
        pieces[key] = value
    stray = set(about) - set(pieces)
    if stray:
        raise PromptError(f"{ABOUT_KEY} names pieces the file does not hold: {sorted(stray)}")
    return pieces


def keys() -> frozenset[str]:
    """Every piece the file holds."""
    return frozenset(_table())


def placeholders(key: str) -> frozenset[str]:
    """The named placeholders piece *key* takes."""
    return frozenset(
        name.split(".")[0].split("[")[0]
        for _lit, name, _spec, _conv in string.Formatter().parse(text(key))
        if name
    )


def text(key: str) -> str:
    """Piece *key* as written in the file."""
    try:
        return _table()[key]
    except KeyError:
        raise PromptError(f"no crewmate prompt {key!r} in {PROMPTS_PATH.name}") from None


def fill(key: str, /, **values: Any) -> str:
    """Piece *key* with its named placeholders filled from *values*, all of them and no other."""
    wanted = placeholders(key)
    given = frozenset(values)
    if wanted != given:
        missing = sorted(wanted - given)
        unknown = sorted(given - wanted)
        raise PromptError(
            f"crewmate prompt {key!r}: missing placeholders {missing}, unknown {unknown}"
        )
    return text(key).format(**values)

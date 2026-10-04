"""The script-hook write boundary: the fail-soft timeout normalization and the
raising field validator both store write paths share.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _SKILLS_ONLY_EVENTS,
        HOOK_EVENTS_ALL,
        HOOK_EVENTS_KAS_ONLY,
        HOOK_TIMEOUT_DEFAULT,
        HOOK_TIMEOUT_MAX,
        HOOK_TIMEOUT_MIN,
    )


def _normalize_hook_timeout(value: object) -> int:
    """Coerce a persisted/edited timeout to an int within the allowed bounds.

    ``hooks.json`` is hand-editable and older files predate the 1–300 bound, so a
    missing / non-int / out-of-range value must degrade to a SAFE in-range value
    rather than propagate: ``None`` or junk → the default; a numeric value is
    clamped into ``[HOOK_TIMEOUT_MIN, HOOK_TIMEOUT_MAX]``. Used by ``from_dict``
    (fail-soft on load); the raising ``validate_hook_fields`` is what rejects a
    bad value at the create/update API boundary. A bool is rejected (``bool`` is
    an ``int`` subclass but ``True`` as a timeout is meaningless).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return HOOK_TIMEOUT_DEFAULT
    try:
        ivalue = int(value)
    except (ValueError, OverflowError):
        return HOOK_TIMEOUT_DEFAULT
    return max(HOOK_TIMEOUT_MIN, min(HOOK_TIMEOUT_MAX, ivalue))


def validate_hook_fields(
    *, event: str, timeout: object, command: str, skills: list, matcher: str, matcher_mode: str
) -> None:
    """Enforce the script-hook invariants at a WRITE boundary, raising on any breach.

    The single source of truth for what makes a hook well-formed, shared by
    ``ScriptHookStore.create`` and ``ScriptHookStore.update`` so a hook persisted
    by EITHER path is held to the same contract — closing the gap where the
    command+skills invariant, event membership, and timeout bounds were checked
    only in ``update``. Deserialization (``ScriptHook.from_dict``) does NOT call
    this: a malformed persisted hook must load fail-soft (normalized), never abort
    the whole store, so it uses the ``_normalize_hook_*`` helpers instead.

    Raises ``ValueError`` (which the dashboard handler maps to HTTP 400) when:

    * ``event`` is not one of ``HOOK_EVENTS_ALL``;
    * ``timeout`` is not an int in ``[1, 300]``;
    * neither ``command`` nor ``skills`` is present (an empty hook);
    * ``skills`` is combined with a ``command`` (the skills would never fire);
    * ``skills`` is paired with an event other than UserPromptSubmit/AgentSpawn
      (the "Load skills:" directive has no consumer there);
    * ``matcher`` is paired with one of ``HOOK_EVENTS_KAS_ONLY`` -- no event fires
      those, so no payload exists for a matcher to filter and the field's subject
      is undefined; storing one now would hand the round that defines the payload
      a filter written against a different subject than the one it picks;
    * ``matcher_mode`` is ``regex`` with a syntactically invalid ``matcher``.
    """
    if event not in HOOK_EVENTS_ALL:
        raise ValueError(f"invalid event: {event}")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not (HOOK_TIMEOUT_MIN <= timeout <= HOOK_TIMEOUT_MAX)
    ):
        raise ValueError(
            f"timeout must be an integer between {HOOK_TIMEOUT_MIN} and {HOOK_TIMEOUT_MAX}"
        )
    if not command and not skills:
        raise ValueError("either command or skills must be provided")
    if skills:
        if command:
            raise ValueError(
                "skills cannot be combined with a command — the skills would "
                "never fire; use a skills-only hook or drop the skills"
            )
        if event not in _SKILLS_ONLY_EVENTS:
            raise ValueError(
                f"skills hooks cannot fire on {event} events — "
                "choose UserPromptSubmit or AgentSpawn"
            )
    if matcher and event in HOOK_EVENTS_KAS_ONLY:
        raise ValueError(
            f"a matcher cannot be set on {event} — no event fires it, so there is "
            "no payload to filter; leave the matcher empty"
        )
    if matcher_mode == "regex" and matcher:
        try:
            re.compile(matcher)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from None

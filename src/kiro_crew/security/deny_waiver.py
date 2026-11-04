"""What a person's in-session override of a command deny rule is keyed on.

A refusal from :func:`kiro_crew.security.is_denied` starts with
``DENY_REASON_PREFIX`` followed by the key of the rule that fired: a catalog or
operator pattern, a git-publish tag, or a floor rule id. That key is the identity
an override waives (``is_denied(..., waived=...)``), so the dashboard reads it
back off the first line here rather than re-deriving it from the command.

A rule an enterprise governance policy force-pins is never waivable:
:func:`deny_key_is_pinned` maps a key to its rule id and checks it against the
pinned ids. A companion ``SecurityOverlay`` pattern is never waivable either
(:func:`deny_key_is_overlay`), and neither is a rule that guards the agent's own
controls (:func:`deny_key_is_self_guard`). Only a backend whose sub-agent calls
the transport can tell apart is offered an override at all
(:func:`child_calls_marked`).
Design: ``docs/request-for-change/rfc-deny-session-override.md``.
"""

from __future__ import annotations

from collections.abc import Iterable

from kiro_crew.security.denied_rules import (
    _GIT_PUBLISH_FLOOR_BY_ID,
    DENY_REASON_PREFIX,
    _rule_id_for_pattern,
)
from kiro_crew.security.denied_rules_tables import (
    _RULES_BY_ID,
    _SELF_PROTECTION_FLOOR_RULE_IDS,
    _SELF_PROTECTION_UNGATED_FLOOR_IDS,
)

#: The key the git-publish floor's unverifiable-target branch reports
#: (``security.deny_gate.is_denied``): the brace-expansion row's pattern.
_UNVERIFIABLE_PUSH_KEY = _GIT_PUBLISH_FLOOR_BY_ID.get(
    "git-publish-push-brace-expansion-refspec", ""
)


def deny_reason_key(reason: str) -> str | None:
    """The key the first line of *reason* names after ``DENY_REASON_PREFIX``.

    ``None`` when the first line is not a deny-rule refusal or names nothing.
    """
    head = (reason or "").split("\n", 1)[0]
    if not head.startswith(DENY_REASON_PREFIX):
        return None
    key = head[len(DENY_REASON_PREFIX) :]
    return key if key.strip() else None


def deny_key_is_pinned(key: str, pinned_ids: Iterable[str]) -> bool:
    """Whether the rule *key* names is one of *pinned_ids*.

    A key is either a rule id itself (a git-publish tag, a floor rule id) or a
    pattern; a pattern is resolved to its built-in rule id, so an operator rule
    typed identical to a pinned built-in is treated as pinned too, the
    conservative reading.
    """
    pinned = set(pinned_ids)
    if not pinned:
        return False
    return key in pinned or _rule_id_for_pattern(key) in pinned


def deny_key_description(key: str) -> str:
    """A plain sentence on what the rule *key* blocks, or ``""`` when none is known.

    A built-in catalog row's own description (matched by id or pattern), or a
    floor's note. An operator rule has no description; its operator note already
    travels on the refusal's second line.
    """
    from kiro_crew.security import denied_rules as _rules

    for rule in _rules.BUILTIN_DENIED_RULES:
        if key in (rule.id, rule.pattern):
            return rule.description
    return _rules._SELF_PROTECTION_FLOOR_NOTES.get(key, "")


_SELF_GUARD_FAMILIES = ("self-protection", "credential-exfil")


def deny_key_is_self_guard(key: str) -> bool:
    """Whether *key* names a rule that stops the agent disabling its own controls.

    Every self-protection floor (the ungated ones included), the git-publish
    floor's unverifiable-target key, and every rule of the self-protection or
    credential-exfil family, by category or id prefix. No override reaches one:
    waiving the credential-mint rule would hand the agent the owner credential
    that answers override cards, and waiving the unverifiable-target key would let
    a push to a protected branch through behind any ``$(...)``. A pattern resolves
    to its rule id, so an operator rule typed identical to one is held the same way.
    """
    rule_id = _rule_id_for_pattern(key) or key
    rule = _RULES_BY_ID.get(rule_id)
    return bool(key) and (
        key == _UNVERIFIABLE_PUSH_KEY
        or rule_id in _SELF_PROTECTION_UNGATED_FLOOR_IDS
        or rule_id in _SELF_PROTECTION_FLOOR_RULE_IDS
        or (rule is not None and rule.category in _SELF_GUARD_FAMILIES)
        or rule_id.startswith(tuple(f + "-" for f in _SELF_GUARD_FAMILIES))
    )


def waivable_keys(
    keys: Iterable[str], pinned_ids: Iterable[str], overlay_patterns: Iterable[str] = ()
) -> frozenset[str]:
    """*keys* without every key :func:`deny_key_is_pinned` holds for, and without
    every companion overlay pattern (:func:`deny_key_is_overlay`)."""
    pinned = set(pinned_ids)
    overlay = set(overlay_patterns)
    return frozenset(
        k for k in keys if k and k not in overlay and not deny_key_is_pinned(k, pinned)
    )


def deny_key_is_overlay(key: str, overlay_patterns: Iterable[str]) -> bool:
    """Whether *key* is one of the companion ``SecurityOverlay``'s patterns.

    An overlay pattern is ADD-only enterprise policy, held as hard as a
    governance pin: no card offers it and no waiver steps it aside. A key that is
    also an operator or catalog pattern of the same text counts as the overlay's,
    the conservative reading.
    """
    return bool(key) and key in set(overlay_patterns)


# ── Backends whose sub-agent calls are told apart ──
#
# An override and a session waiver belong to the person's own agent, never to a
# sub-agent it started. A backend qualifies only where the transport that reads
# its permission frames has a proven mark for a child's call; every other backend
# gets no card and no waiver (fail closed). The two transports mark different
# things, so each has its own harness capability set:
# ``ACP_BACKENDS_DIRECT_CLIENT_CHILD_CALLS_MARKED`` and
# ``ACP_BACKENDS_SHARED_RUNTIME_CHILD_CALLS_MARKED`` (``agent_sdk.backends``).


def child_calls_marked(backend: object, verified: frozenset[str]) -> bool:
    """Whether *backend* is one of *verified*, the transport's proven backends.

    *backend* is read as the transport holds it; anything but a string (an
    unset attribute read as ``None``, a test double) is not on any list, so an
    unknown backend fails closed. Kiro CLI's id is ``""``, so the caller must not
    default a missing backend to ``""``.
    """
    return isinstance(backend, str) and backend in verified

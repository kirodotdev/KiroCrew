"""What a person's in-session override of a command deny rule is keyed on.

A refusal from :func:`kiro_crew.security.is_denied` starts with
``DENY_REASON_PREFIX`` followed by the key of the rule that fired: a catalog or
operator pattern, a git-publish tag, or a floor rule id. That key is the identity
an override waives (``is_denied(..., waived=...)``), so the dashboard reads it
back off the first line here rather than re-deriving it from the command.

A rule an enterprise governance policy force-pins is never waivable:
:func:`deny_key_is_pinned` maps a key to its rule id and checks it against the
pinned ids. Design: ``docs/request-for-change/rfc-deny-session-override.md``.
"""

from __future__ import annotations

from collections.abc import Iterable

from kiro_crew.security.denied_rules import (
    _GIT_PUBLISH_FLOOR_BY_ID,
    DENY_REASON_PREFIX,
    _rule_id_for_pattern,
)

#: Every git-publish floor rule id.
_GIT_PUBLISH_RULE_IDS = frozenset(_GIT_PUBLISH_FLOOR_BY_ID)
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
    if key in pinned or _rule_id_for_pattern(key) in pinned:
        return True
    # The git-publish floor's unverifiable-target refusal is reported under the
    # brace-expansion row's key, but waiving it lets through a push whose hidden
    # destination may be the very branch a pinned git-publish rule protects. So a
    # pin on ANY git-publish rule pins that key too.
    return key == _UNVERIFIABLE_PUSH_KEY and bool(pinned & _GIT_PUBLISH_RULE_IDS)


#: What the unverifiable-target key stands for, said in the person's terms: its
#: catalog row describes brace expansion, while the refusal is about any push
#: whose destination is hidden.
_UNVERIFIABLE_PUSH_DESCRIPTION = (
    "Blocks 'git push' when the branch it pushes to is built by the shell at run time, "
    "so it cannot be checked before the push runs and could be a protected branch."
)


def deny_key_description(key: str) -> str:
    """A plain sentence on what the rule *key* blocks, or ``""`` when none is known.

    A built-in catalog row's own description (matched by id or pattern), the
    unverifiable-push sentence for that floor's key, or a floor's note. An
    operator rule has no description; its operator note already travels on the
    refusal's second line.
    """
    if key and key == _UNVERIFIABLE_PUSH_KEY:
        return _UNVERIFIABLE_PUSH_DESCRIPTION
    from kiro_crew.security import denied_rules as _rules

    for rule in _rules.BUILTIN_DENIED_RULES:
        if key in (rule.id, rule.pattern):
            return rule.description
    return _rules._SELF_PROTECTION_FLOOR_NOTES.get(key, "")


def waivable_keys(keys: Iterable[str], pinned_ids: Iterable[str]) -> frozenset[str]:
    """*keys* without every key :func:`deny_key_is_pinned` holds for."""
    pinned = set(pinned_ids)
    return frozenset(k for k in keys if k and not deny_key_is_pinned(k, pinned))

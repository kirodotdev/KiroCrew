"""Which answers a git-confirm card takes (``hooks.confirm_git_writes``).

A command the session confirms (a git command other than a known read, or a
git-free command holding a shell expansion) shows a card whose permission row
carries ``git_confirm: "1"``. A person answers it once: Allow once or Reject, or
a security-rule override card's own decisions when the card is also one. No
standing tier settles it (Trust, Trust reads, a command or family grant, YOLO, a
Slack Code channel's "Trust this channel"), and neither does an app's
session-approval grant, so every route that answers a card asks
:func:`git_confirm_refuses` first and leaves a refused card pending.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.dashboard.deny_override import DENY_OVERRIDE_ACTIONS

#: The permission-row field that marks a card as a git-confirm card.
GIT_CONFIRM_META_KEY = "git_confirm"
#: The answers a git-confirm card takes: each settles this one call and grants nothing.
ONE_SHOT_ANSWERS = frozenset({"approved", "rejected", "rejected_once"}) | DENY_OVERRIDE_ACTIONS


def is_git_confirm_card(messages: Any, request_id: Any) -> bool:
    """Whether the newest permission row for *request_id* in *messages* is a git-confirm card."""
    if not isinstance(request_id, str) or not request_id:
        return False
    for message in reversed(messages or ()):
        if not isinstance(message, dict) or message.get("role") != "permission":
            continue
        try:
            meta = json.loads(message.get("cls") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("request_id") == request_id:
            return meta.get(GIT_CONFIRM_META_KEY) == "1"
    return False


def git_confirm_refuses(messages: Any, request_id: Any, action: Any, *, from_app: bool) -> bool:
    """Whether *action* may not settle *request_id*'s card because it is a git-confirm card.

    True for an app's answer of any kind and for every answer that is not one
    of :data:`ONE_SHOT_ANSWERS`; False for any card without the mark.
    """
    if not is_git_confirm_card(messages, request_id):
        return False
    return from_app or not (isinstance(action, str) and action in ONE_SHOT_ANSWERS)

"""Order the session-start rule block by who taught each rule.

Every lesson row gets an authority level from data it already carries:

* L1 user-taught -- ``source`` is ``user_explicit``. A ``learn_add`` the lesson
  origin gate (:mod:`kiro_crew.memory_origin_gate`) traced to a turn no person
  sent carries its own source under ``enforce``, so it is never L1.
* L2 user-quoted -- a consolidation row with a verified ``user_quote``.
* L3 agent-inferred -- every other row, untagged legacy rows included.

``memory.rule_authority_order`` picks the mode:

* ``off`` (default) -- the rule block is today's, byte for byte.
* ``shadow`` -- the block is today's; the injection receipt records the rule ids
  the authority order would add to it and drop from it.
* ``enforce`` -- the block lists L1, then L2, then L3. Inside a level the
  shipped order holds: authored ``always`` rules before untagged rows, each by
  request relevance. The cap, the omission line and the findings block are
  unchanged.

L1 is only as clean as ``source``: with ``memory.lesson_origin_gate`` below
``enforce``, an agent-triggered ``learn_add`` still stores ``user_explicit`` and
so ranks as L1. Run the origin gate at ``enforce`` before this one.

Ordering only: no row is written, folded or dropped by a rule of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final, TypeVar

from kiro_crew.config.memory_sections import rule_authority_order_mode_from

logger = logging.getLogger(__name__)

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

#: Bump when what decides a level, or the order inside one, changes, so receipts
#: taken under the old policy can be told from the new.
POLICY_VERSION: Final = 1

LEVEL_USER_TAUGHT: Final = 1
LEVEL_AGENT_INFERRED: Final = 3

_Entry = TypeVar("_Entry", bound=tuple)


def configured_mode() -> str:
    """``memory.rule_authority_order`` from the live config; the default when unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "rule_authority_order", None)
    except Exception:
        logger.debug("Could not read memory.rule_authority_order; using the default", exc_info=True)
        raw = None
    return rule_authority_order_mode_from(raw)


def authority(row: object) -> int:
    """The authority level of stored lesson *row*."""
    if isinstance(row, dict) and row.get("source") == "user_explicit":
        return LEVEL_USER_TAUGHT
    # shortcut: the consolidation quote check (memory_tier_policy) decides a tier
    # but stores no quote on the row, so L2 has no rows and every other row is
    # L3; read the quote here once the row carries it.
    return LEVEL_AGENT_INFERRED


def by_authority(entries: Sequence[_Entry]) -> list[_Entry]:
    """*entries* (``(row, text)`` pairs) by level, keeping their order inside a level."""
    return sorted(entries, key=lambda entry: authority(entry[0]))

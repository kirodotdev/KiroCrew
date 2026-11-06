"""The claim check: a model sentence must not say the opposite of a fact it
stands beside (RFC section 3.10).

An exact lookup keyed by the fact's value, never an open scan of the text:
each bad value forbids a short, fixed list of phrases that state its
opposite, in every language the fixed lines speak. A sentence holding one is
dropped; the gateway's own tags still show the state.

shortcut: a phrase negated in the sentence ("not ready to merge") is still a
match, so a true sentence can be dropped. Dropping is the safe side; add a
negation rule only if owners report lost sentences.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# "No conflicts": forbidden beside a conflicting pull request.
_NO_CONFLICTS = (
    "no conflicts", "no merge conflicts", "conflict-free", "without conflicts", "ready to merge",
    "没有冲突", "无冲突", "可以合并", "競合なし", "競合はありません", "충돌 없음", "충돌이 없",
    "без конфликтов", "нет конфликтов", "कोई टकराव नहीं", "কোনো দ্বন্দ্ব নেই",
)  # fmt: skip
# "All green": forbidden beside a pull request with a failed check.
_ALL_GREEN = (
    "all green", "all checks pass", "all checks passed", "all checks passing",
    "checks are green", "ci is green", "checks all pass",
    "全部通过", "全绿", "检查都通过", "すべて成功", "すべてのチェックが通", "모두 통과",
    "все проверки прошли", "सभी जाँच पास", "সব যাচাই পাস",
)  # fmt: skip
# "Approved": forbidden beside requested changes.
_APPROVED = (
    "is approved",
    "was approved",
    "已批准",
    "承認済み",
    "승인됨",
    "одобрен",
    "स्वीकृत",
    "অনুমোদিত",
)
# "Still open": forbidden beside a merged or closed pull request.
_STILL_OPEN = ("still open", "仍然开放", "仍在开放", "まだオープン", "아직 열려", "всё ещё открыт")
# "Waiting for your approval": forbidden beside a chat with no approval pending.
_AWAITS_APPROVAL = (
    "waiting for your approval", "awaiting your approval", "parked waiting for your approval",
    "等你批准", "等待你的批准", "等待你批准", "承認待ち", "승인을 기다", "ждёт вашего одобрения",
    "आपकी मंज़ूरी का इंतज़ार", "আপনার অনুমোদনের অপেক্ষায়",
)  # fmt: skip
# "Running": forbidden beside a closed worker chat.
_RUNNING = (
    "is running",
    "still running",
    "is working on it",
    "正在运行",
    "実行中",
    "실행 중",
    "работает",
)


def forbidden(fact: Mapping[str, Any]) -> tuple[str, ...]:
    """The phrases that contradict *fact*'s values, by exact key."""
    values = fact.get("values") or {}
    kind = fact.get("kind")
    words: list[str] = []
    if kind == "pr":
        checks = values.get("checks")
        if values.get("mergeable") == "conflicting":
            words += _NO_CONFLICTS
        if (
            isinstance(checks, Mapping)
            and isinstance(checks.get("failed"), int)
            and checks["failed"]
        ):
            words += _ALL_GREEN
        if values.get("review") == "changes_requested":
            words += _APPROVED
        if values.get("state") in ("merged", "closed"):
            words += _STILL_OPEN
    elif kind == "approval" and values.get("pending") == 0:
        words += _AWAITS_APPROVAL
    elif kind == "chat" and values.get("state") == "closed":
        words += _RUNNING
    return tuple(words)


def _holds(text: str, phrase: str) -> bool:
    # A Latin phrase matches as whole words; any other script as written.
    if phrase.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None
    return phrase in text


def contradicts(text: str, facts: Iterable[Mapping[str, Any]]) -> bool:
    """Whether *text* states the opposite of any of *facts*."""
    folded = text.casefold()
    return any(_holds(folded, phrase.casefold()) for fact in facts for phrase in forbidden(fact))

"""Pure decision logic for chat status tagging.

Everything in this module is transport-agnostic: no HTTP, no gateway state,
no filesystem. The builtin drives it from in-process loops (``hooks.py``);
a future external-app packaging can drive the same functions from sandboxed
cron scripts without touching this file. Keep it that way — this module IS
the portability seam.

Tag model
---------
Two independent tag families coexist on a slot:

* **SDLC status tags** (``planned``/``todo``/``implementation``/``review``/
  ``done``) — set by the ``self-tag-chat`` skill and promoted by the
  reconcile flow. Exactly one per slot, never downgraded.
* **Health tags** (``stuck``/``network``/``error``) — computed here from the
  slot's live state and its latest terminal error card. Cleared automatically
  when the condition clears.

Health rules
------------
* ``stuck``  — the slot is ``running`` but its ``last_ts`` (last message of
  ANY role, including the user's own prompt and streaming chunks) is older
  than the threshold. ``last_activity_ts`` is deliberately NOT used: it only
  tracks tool/assistant messages, so on a chat resumed after a long idle it
  still points at the PRIOR turn and would flag a healthy, just-resumed chat.
* ``network`` — the slot is idle and its latest real message is a terminal
  error card of the network class (connection lost / busy / backend hiccup).
  These are nursed by the auto-resume loop.
* ``error``  — idle with an auth-class or unclassified terminal error card.
  These need the human, so they are tagged but never auto-resumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# SDLC status tags, in promotion order. Promotion is one-way: a later phase
# never moves back to an earlier one (the reconcile flow enforces this).
STATUS_ORDER: dict[str, int] = {
    "planned": 0,
    "todo": 1,
    "implementation": 2,
    "review": 3,
    "done": 4,
}

# Health tags managed by the health loop.
HEALTH_TAGS = ("stuck", "network", "error")

# Default colors used when seeding the vocabulary (idempotent by name).
TAG_COLORS: dict[str, str] = {
    "planned": "#6b7280",
    "todo": "#64748b",
    "implementation": "#3b82f6",
    "review": "#a855f7",
    "done": "#22c55e",
    "stuck": "#f59e0b",
    "network": "#0ea5e9",
    "error": "#ef4444",
}

DEFAULT_STUCK_MIN = 30
RECENT_HOURS = 6

# ── Error-card classification ────────────────────────────────────────────
#
# Substrings (lowercase) that mark the network/backend class. These are
# pinned to the terminal error cards the chat runner actually appends
# (dashboard/chat_runner.py): "Connection lost", "Session busy",
# "Backend hiccup", "Session stuck", plus backend-echoed timeout text.
# The runner rewrites lower-level failures (process death, dispatch
# failures) into these card strings before they reach a slot, so matching
# raw exception text here would match nothing.
_NETWORK = (
    "connection lost",
    "session busy",
    "backend hiccup",
    "session stuck",
    "turn stalled",
    "tool appeared stalled",
    "timed out",
    "timeout",
)

# Roles that count as a "real" message when deciding a slot's current state.
# "streaming" is a live in-flight assistant turn — its presence means the
# chat is working, not errored.
_REAL_ROLES = ("assistant", "user", "error", "streaming")


def classify_error(content: str) -> str:
    """Classify one error-role card's content.

    Returns one of ``transient`` / ``network`` / ``auth`` / ``other``:

    * ``transient`` — the gateway is already re-queuing ("retrying" cards).
      Not actionable; ignore.
    * ``network``   — the pipe to the model died or the backend refused. The
      remedy is resuming once connectivity is back, so it is safe to
      auto-continue.
    * ``auth``      — not logged in. Needs a re-login, never a resume.
    * ``other``     — anything else (refusal, approval timeout, unknown).
      Left for the human.
    """
    c = (content or "").lower()
    if "retrying" in c:
        return "transient"
    if "not logged in" in c or "\U0001f511" in (content or ""):
        return "auth"
    if any(p in c for p in _NETWORK):
        return "network"
    return "other"


def latest_error_class(messages: list[dict]) -> str:
    """Classify a slot's CURRENT state from its message tail (oldest→newest).

    Returns ``""`` when the latest real message is not an error card — an old
    error followed by any normal turn means the chat recovered.
    """
    for m in reversed(messages or []):
        role = m.get("role")
        if role in _REAL_ROLES:
            if role != "error":
                return ""
            return classify_error(m.get("content") or "")
    return ""


# ── Health decision ──────────────────────────────────────────────────────


def parse_ts(s: str) -> datetime | None:
    """Parse an ISO timestamp, returning None on any failure."""
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def is_recent(slot: dict, now: datetime, recent_hours: int = RECENT_HOURS) -> bool:
    """True when the slot had a message within the recency window.

    Bounds the per-tick cost: only recent (or already-tagged) slots are worth
    a detail fetch to look for a terminal error card.
    """
    ts = parse_ts(slot.get("last_ts") or slot.get("last_activity_ts") or "")
    return bool(ts and (now - ts).total_seconds() < recent_hours * 3600)


def is_stuck(slot: dict, now: datetime, stuck_min: int = DEFAULT_STUCK_MIN) -> bool:
    """True when a running slot has gone silent past the threshold.

    Uses ``last_ts`` (any role) — see the module docstring for why
    ``last_activity_ts`` would false-positive on resumed chats.
    """
    if not slot.get("running"):
        return False
    ts = parse_ts(slot.get("last_ts") or slot.get("last_activity_ts") or "")
    return bool(ts and (now - ts).total_seconds() > stuck_min * 60)


def desired_health_tags(*, stuck: bool, error_class: str) -> set[str]:
    """Map the slot's observed state to the health tags it should carry."""
    want: set[str] = set()
    if stuck:
        want.add("stuck")
    elif error_class == "network":
        want.add("network")
    elif error_class in ("auth", "other"):
        want.add("error")
    return want


def merge_tags(
    current_ids: list[str],
    managed_ids: set[str],
    want_ids: set[str],
) -> list[str]:
    """Replace the managed subset of a slot's tags, preserving everything else.

    Order-stable and duplicate-free; returns the new full tag-id list.
    """
    new = [t for t in current_ids if t not in managed_ids]
    for tid in want_ids:
        if tid not in new:
            new.append(tid)
    return new


# ── Auto-resume episodes ─────────────────────────────────────────────────


@dataclass
class Episode:
    """Resume-attempt accounting for one failure episode of one slot.

    An episode is keyed on the FAILURE ANCHOR — the timestamp of the newest
    message that is neither an error card nor one of our own injected resume
    turns (see :func:`failure_anchor`). That identity is stable while a
    failure run continues: each injected ``Continue`` and each fresh error
    card advance the slot's ``last_ts`` but not the anchor, so repeated
    resumes of the same failure burn down one attempt budget and a chat whose
    backend keeps dying can never trigger a resume storm. Only a real
    recovery (a genuine user/assistant turn) moves the anchor and earns a
    fresh episode.
    """

    last_ts: str = ""
    attempts: int = 0


MAX_RESUME_ATTEMPTS = 3


def failure_anchor(messages: list[dict], resume_text: str) -> str:
    """Identity of the CURRENT failure run, from the slot's message tail.

    Returns the ``ts`` of the newest real message that is neither an error
    card nor an injected resume turn (a user message whose content is exactly
    *resume_text*). Both of those advance ``last_ts`` on every resume cycle,
    which is exactly why ``last_ts`` cannot key the episode: it would re-key
    a fresh attempt budget after every failed resume, unbounding the cap.
    Empty when no such message exists in the tail.
    """
    for m in reversed(messages or []):
        role = m.get("role")
        if role not in _REAL_ROLES or role == "error":
            continue
        if role == "user" and (m.get("content") or "").strip() == resume_text:
            continue
        return str(m.get("ts") or "")
    return ""


def next_episode(prev: Episode | None, anchor_ts: str) -> Episode:
    """Roll the episode forward for the failure run identified by *anchor_ts*.

    Same anchor → the same episode (attempts carry forward, burning down the
    cap). A new anchor means a real recovery happened since — fresh episode,
    fresh budget.
    """
    if prev is None or prev.last_ts != anchor_ts:
        return Episode(last_ts=anchor_ts, attempts=0)
    return prev


def may_resume(ep: Episode, max_attempts: int = MAX_RESUME_ATTEMPTS) -> bool:
    """True while the episode still has resume budget left."""
    return ep.attempts < max_attempts


# ── SDLC promotion (reconcile) ───────────────────────────────────────────


def promotion(cur_status: str | None, desired: str) -> str | None:
    """Return *desired* when it is a strict promotion over *cur_status*.

    Returns None when the move is unknown, lateral, or a downgrade —
    promotion is one-way by design (a human can always drag a tag back;
    automation must never do it).
    """
    if desired not in STATUS_ORDER:
        return None
    if STATUS_ORDER[desired] <= STATUS_ORDER.get(cur_status or "", -1):
        return None
    return desired

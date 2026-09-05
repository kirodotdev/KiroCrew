"""Tests for the pure decision logic.

The classifier cases are pinned to the TERMINAL ERROR CARD STRINGS the chat
runner actually appends (``dashboard/chat_runner.py``) — if a card string
changes there, the corresponding case here must be updated in lockstep, which
is the point: the classifier must never silently drift away from the cards
it classifies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew.apps.builtins.chat_status_tags import logic

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def _ts(delta_min: float) -> str:
    return (NOW + timedelta(minutes=delta_min)).isoformat()


# ── classify_error: pinned to chat_runner's real card strings ────────────


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        # Transient auto-retry cards (chat_runner appends these while the
        # gateway is already re-queuing; both ellipsis spellings occur).
        ("⟳ Connection lost — retrying…", "transient"),
        ("⟳ Connection lost (exit 1) — retrying...", "transient"),
        ("⟳ Session busy — retrying…", "transient"),
        ("⟳ Backend hiccup — retrying…", "transient"),
        # Terminal network-class cards — safe to auto-resume.
        ("⟳ Connection lost — please retry.", "network"),
        ("⟳ Session busy — please retry.", "network"),
        ("⟳ Backend hiccup — please retry.", "network"),
        ("Session stuck — please start a new chat.", "network"),
        ("Session stuck (exit 137) — please start a new chat.", "network"),
        ("⟳ Turn stalled — please retry.", "network"),
        ("⟳ Tool appeared stalled — please retry.", "network"),
        ("⏱️ request timed out after 60s", "network"),
        # Auth — needs a re-login, never a resume.
        (
            "kiro-cli is not logged in. Run `kiro-cli login` in your terminal, "
            "then start a new chat.",
            "auth",
        ),
        # Everything else is left for the human.
        ("Response declined by the model. Try rephrasing your request.", "other"),
        ("some unrecognised failure", "other"),
        ("", "other"),
    ],
)
def test_classify_error(card: str, expected: str) -> None:
    assert logic.classify_error(card) == expected


# ── latest_error_class ───────────────────────────────────────────────────


def test_latest_error_class_healthy_after_recovery() -> None:
    msgs = [
        {"role": "error", "content": "⟳ Connection lost — please retry."},
        {"role": "assistant", "content": "back and answering"},
    ]
    assert logic.latest_error_class(msgs) == ""


def test_latest_error_class_trailing_error() -> None:
    msgs = [
        {"role": "assistant", "content": "working on it"},
        {"role": "error", "content": "⟳ Backend hiccup — please retry."},
    ]
    assert logic.latest_error_class(msgs) == "network"


def test_latest_error_class_streaming_tail_is_healthy() -> None:
    # A live in-flight turn (role "streaming") means the chat is working.
    msgs = [
        {"role": "error", "content": "⟳ Connection lost — please retry."},
        {"role": "streaming", "content": "partial answer…"},
    ]
    assert logic.latest_error_class(msgs) == ""


def test_latest_error_class_skips_non_real_roles() -> None:
    msgs = [
        {"role": "error", "content": "Session stuck — please start a new chat."},
        {"role": "notice", "content": "✅ Conversation compacted"},
        {"role": "tool_result", "content": "..."},
    ]
    assert logic.latest_error_class(msgs) == "network"


def test_latest_error_class_empty() -> None:
    assert logic.latest_error_class([]) == ""


# ── stuck detection: last_ts, NOT last_activity_ts ───────────────────────


def test_stuck_running_and_silent() -> None:
    slot = {"running": True, "last_ts": _ts(-45)}
    assert logic.is_stuck(slot, NOW, stuck_min=30)


def test_not_stuck_when_idle() -> None:
    slot = {"running": False, "last_ts": _ts(-120)}
    assert not logic.is_stuck(slot, NOW)


def test_not_stuck_when_last_ts_fresh_even_if_activity_stale() -> None:
    # A just-resumed chat: the user's new prompt refreshes last_ts while
    # last_activity_ts still points at the prior turn's assistant message.
    # Using last_activity_ts here is the documented false-positive.
    slot = {"running": True, "last_ts": _ts(-1), "last_activity_ts": _ts(-300)}
    assert not logic.is_stuck(slot, NOW, stuck_min=30)


def test_stuck_falls_back_to_activity_ts_when_last_ts_missing() -> None:
    slot = {"running": True, "last_activity_ts": _ts(-45)}
    assert logic.is_stuck(slot, NOW, stuck_min=30)


def test_stuck_unparseable_ts_is_not_stuck() -> None:
    assert not logic.is_stuck({"running": True, "last_ts": "not-a-date"}, NOW)


# ── recency window ───────────────────────────────────────────────────────


def test_is_recent_inside_window() -> None:
    assert logic.is_recent({"last_ts": _ts(-60)}, NOW)


def test_is_recent_outside_window() -> None:
    assert not logic.is_recent({"last_ts": _ts(-7 * 60)}, NOW)


# ── desired health tags + merge ──────────────────────────────────────────


def test_desired_health_tags_stuck_wins() -> None:
    # A running-but-stuck slot cannot also carry an error card verdict.
    assert logic.desired_health_tags(stuck=True, error_class="") == {"stuck"}


@pytest.mark.parametrize(
    ("error_class", "want"),
    [
        ("network", {"network"}),
        ("auth", {"error"}),
        ("other", {"error"}),
        ("transient", set()),
        ("", set()),
    ],
)
def test_desired_health_tags_by_class(error_class: str, want: set[str]) -> None:
    assert logic.desired_health_tags(stuck=False, error_class=error_class) == want


def test_merge_tags_preserves_unmanaged() -> None:
    managed = {"h1", "h2", "h3"}
    cur = ["s-review", "h1", "user-tag"]
    new = logic.merge_tags(cur, managed, {"h2"})
    assert new == ["s-review", "user-tag", "h2"]


def test_merge_tags_clear() -> None:
    assert logic.merge_tags(["h1", "x"], {"h1"}, set()) == ["x"]


def test_merge_tags_no_duplicates() -> None:
    assert logic.merge_tags(["x", "h1"], {"h1"}, {"h1"}) == ["x", "h1"]


# ── resume episodes ──────────────────────────────────────────────────────


def test_episode_new_failure_resets_attempts() -> None:
    prev = logic.Episode(last_ts="t1", attempts=3)
    ep = logic.next_episode(prev, "t2")
    assert ep.attempts == 0 and ep.last_ts == "t2"


def test_episode_same_failure_keeps_attempts() -> None:
    prev = logic.Episode(last_ts="t1", attempts=2)
    assert logic.next_episode(prev, "t1") is prev


def test_episode_cap() -> None:
    assert logic.may_resume(logic.Episode(last_ts="t", attempts=2))
    assert not logic.may_resume(logic.Episode(last_ts="t", attempts=3))


# ── promote-only status ordering ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("cur", "desired", "expected"),
    [
        (None, "review", "review"),
        ("implementation", "review", "review"),
        ("review", "done", "done"),
        ("review", "review", None),  # lateral — no-op
        ("done", "review", None),  # downgrade — refused
        ("review", "implementation", None),  # downgrade — refused
        ("todo", "bogus", None),  # unknown phase — refused
    ],
)
def test_promotion(cur: str | None, desired: str, expected: str | None) -> None:
    assert logic.promotion(cur, desired) == expected

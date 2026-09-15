"""Tests: DashboardState.resolve_skill_review_notifications.

Approving, dismissing, or TTL-pruning a pending skill candidate retires the
review request its bell notification exists to surface. This method is the
gateway-side half of the consumed-hook seam (see ``set_pending_consumed_hook``
in ``kiro_crew.skills``): it acks — never deletes — every matching unread
skill-review row, persists once, and broadcasts ``notification_ack`` per row so
an open feed drops the badge live.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


@pytest.fixture()
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _skill_note(slug: str, ts: str, *, acked: bool = False) -> dict:
    # Shape mirrors what the staged hook produces: bus channel system.skills
    # (with the derived legacy kind) and the meta payload ({slug,
    # candidate_kind, target}) flat-merged by the bus.
    note = {
        "kind": "skills",
        "channel": "system.skills",
        "title": "New skill awaiting review",
        "body": f"**{slug}**",
        "ts": ts,
        "slug": slug,
        "candidate_kind": "new",
    }
    if acked:
        note["acked"] = True
    return note


@pytest.mark.asyncio
async def test_acks_matching_rows_and_broadcasts(state):
    state._notification_log = [
        _skill_note("deploy-helper", "t1"),
        # Re-surfaced duplicate for the same candidate: both rows must retire.
        _skill_note("deploy-helper", "t2"),
        _skill_note("other-skill", "t3"),
        {"kind": "cron", "title": "Job", "body": "b", "ts": "t4", "slug": "deploy-helper"},
    ]
    sent: list[tuple[str, object]] = []
    state.broadcast_ws = lambda msg_type, data: sent.append((msg_type, data))

    assert await state.resolve_skill_review_notifications("deploy-helper", "t9") == 2

    by_ts = {n["ts"]: n for n in state._notification_log}
    assert by_ts["t1"]["acked"] is True
    assert by_ts["t2"]["acked"] is True
    # A different candidate's row stays unread.
    assert "acked" not in by_ts["t3"]
    # A non-skills note that happens to carry a `slug` key is never touched.
    assert "acked" not in by_ts["t4"]
    assert sent == [
        ("notification_ack", {"ts": "t1"}),
        ("notification_ack", {"ts": "t2"}),
    ]


@pytest.mark.asyncio
async def test_already_acked_rows_are_left_alone(state):
    state._notification_log = [_skill_note("done-skill", "t1", acked=True)]
    sent: list[tuple[str, object]] = []
    state.broadcast_ws = lambda msg_type, data: sent.append((msg_type, data))

    # Nothing to do → no rewrite, no broadcast.
    assert await state.resolve_skill_review_notifications("done-skill", "t9") == 0
    assert sent == []


@pytest.mark.asyncio
async def test_no_match_is_a_noop(state):
    state._notification_log = [_skill_note("some-skill", "t1")]
    assert await state.resolve_skill_review_notifications("unrelated", "t9") == 0
    assert "acked" not in state._notification_log[0]


@pytest.mark.asyncio
async def test_empty_slug_matches_nothing(state):
    # A defensive caller with a blank slug must not sweep rows whose slug is
    # also blank/absent (note.get("slug") would equal "" for neither here,
    # but the guard makes the contract explicit).
    state._notification_log = [_skill_note("real-skill", "t1")]
    assert await state.resolve_skill_review_notifications("", "t9") == 0
    assert "acked" not in state._notification_log[0]


@pytest.mark.asyncio
async def test_ack_persists_to_disk(state, tmp_path):
    state._notification_log = [_skill_note("persist-me", "t1")]
    assert await state.resolve_skill_review_notifications("persist-me", "t9") == 1
    text = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8")
    assert '"acked": true' in text


@pytest.mark.asyncio
async def test_replacement_generation_staged_after_cutoff_survives(state):
    # The consumed_at cutoff is stamped BEFORE the pending dir is removed, and
    # staging refuses to overwrite an existing candidate — so a same-slug
    # replacement staged after consumption carries a later ts. Its (actionable)
    # notification must survive the resolve; only the consumed generation acks.
    state._notification_log = [
        _skill_note("deploy-helper", "t1"),  # consumed generation
        _skill_note("deploy-helper", "t8"),  # replacement, staged after cutoff
    ]
    sent: list[tuple[str, object]] = []
    state.broadcast_ws = lambda msg_type, data: sent.append((msg_type, data))

    assert await state.resolve_skill_review_notifications("deploy-helper", "t5") == 1

    by_ts = {n["ts"]: n for n in state._notification_log}
    assert by_ts["t1"]["acked"] is True
    assert "acked" not in by_ts["t8"]
    assert sent == [("notification_ack", {"ts": "t1"})]


@pytest.mark.asyncio
async def test_empty_cutoff_matches_nothing(state):
    # A consumption event without a stamp cannot prove which generation it
    # consumed — fail safe by leaving every row unread.
    state._notification_log = [_skill_note("some-skill", "t1")]
    assert await state.resolve_skill_review_notifications("some-skill", "") == 0
    assert "acked" not in state._notification_log[0]


@pytest.mark.asyncio
async def test_app_channel_note_with_skills_kind_is_never_acked(state):
    # An app can register a channel like "myapp.skills", whose notes derive the
    # legacy kind "skills" and may flat-merge their own `slug` meta key. Only
    # the gateway's own producer can carry channel system.skills ("system" is a
    # reserved app name), so matching pins the channel — the app's durable
    # notification must survive a same-slug candidate consumption.
    app_note = {
        "kind": "skills",
        "channel": "myapp.skills",
        "title": "App item",
        "body": "b",
        "ts": "t1",
        "slug": "deploy-helper",
    }
    state._notification_log = [app_note, _skill_note("deploy-helper", "t2")]
    sent: list[tuple[str, object]] = []
    state.broadcast_ws = lambda msg_type, data: sent.append((msg_type, data))

    assert await state.resolve_skill_review_notifications("deploy-helper", "t9") == 1

    assert "acked" not in state._notification_log[0]
    assert state._notification_log[1]["acked"] is True
    assert sent == [("notification_ack", {"ts": "t2"})]

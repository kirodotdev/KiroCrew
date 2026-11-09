"""Archived channels never crowd live ones out of the surface store's cap."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    CH,
    _bind,
    _click,
    _git,
    _register_workspace,
    _wrap,
    env_fixture,
    worktree_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.slack.code_channels import surface_store as ss
from kiro_crew.slack.code_channels import wrapup
from kiro_crew.slack.code_channels.surface_store import SurfaceStore


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: Any) -> SurfaceStore:
    monkeypatch.setattr(ss, "MAX_RECORDS", 3)
    return SurfaceStore(tmp_path / "s.json")


def test_archived_wrapped_up_channels_go_before_a_live_one(store: SurfaceStore) -> None:
    for ch in ("A1", "A2", "A3"):  # wrapped up (posted in Slack), then archived
        store.update(ch, summary_ts="1.0", plan_canvas_id=f"F{ch}")
        store.retire(ch)
    store.update("L1", context_bar=({"key": "k", "label": "v"},), wrapup_pending_since=5.0)
    store.update("L2", commands_digest="d")  # an unrelated channel's write
    live = store.get("L1")
    assert live is not None and live.wrapup_pending_since == 5.0
    assert store.get("L2") is not None


def test_a_write_after_an_unarchive_makes_the_record_live_again(store: SurfaceStore) -> None:
    store.update("C1", plan_canvas_id="F1")
    store.retire("C1")
    assert store.get("C1").archived_at  # type: ignore[union-attr]
    store.update("C1", commands_digest="d")  # the unarchived channel's next write
    record = store.get("C1")
    assert record is not None and record.archived_at == 0.0 and record.plan_canvas_id == "F1"


def test_retiring_a_channel_with_no_record_writes_nothing(store: SurfaceStore) -> None:
    store.retire("C9")
    assert store.get("C9") is None


def test_the_archive_retires_the_channels_surface_record(env: Any, worktree: Any) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree, _git("rev-parse", "HEAD", cwd=root))
    asyncio.run(ss.update_async(CH, plan_canvas_id="F1"))
    _, ts = _wrap(env)
    assert not asyncio.run(ss.get_async(CH)).archived_at  # type: ignore[union-attr]
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    record = asyncio.run(ss.get_async(CH))
    assert record is not None and record.archived_at and record.plan_canvas_id == "F1"

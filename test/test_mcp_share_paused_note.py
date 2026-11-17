"""One bell note when a shared MCP server that was approved gets paused.

Contract under test:
- a share-list server refused as ``changed_needs_reapproval`` is named in one
  ``system.approval`` note that links to the Sharing view's Review dialog;
- a server never approved (``added_outside_dashboard``) and a refused server
  that is not on the share list raise no note, and sharing off raises none;
- a later start that finds the same launches paused publishes nothing, read or
  not; a different server set, or the same server refused for a different
  launch, publishes again under the same key;
- a start that finds nothing paused removes the stored note, so a server
  that was approved is not left shown as paused and the same launch can be
  announced if it pauses again;
- a stored note is the only thing that stops a repeat: deleted while the
  servers are still paused, it is published again at the next start;
- a note whose keyed write fails is not reported as published;
- a new pause first removes the stored note, so the new one is not a quiet
  same-key replacement; when that removal is not saved, nothing is published;
- a start with no shared server still runs the publisher, so a note naming
  servers that left the share list is removed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import launch_approval
from kiro_crew.notifications import mcp_share_paused_note as note_mod
from kiro_crew.notifications.bus import NotificationBus

CHANGED = launch_approval.REFUSED_CHANGED
UNAPPROVED = launch_approval.REFUSED_UNAPPROVED

pytestmark = pytest.mark.asyncio


class _State:
    """What the note publisher reads off the dashboard state."""

    def __init__(self) -> None:
        self._notification_log: list[dict] = []
        self.notification_bus = NotificationBus(sink=self._deliver)
        self.rewrites = 0
        self.persist_ok = True
        self.last_notification_persist: asyncio.Future[bool] | None = None
        self.dismiss_saves = True

    def _deliver(self, note: dict) -> None:
        self._notification_log.append(note)
        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        done.set_result(self.persist_ok)
        self.last_notification_persist = done

    async def dismiss_notification(self, *, source: str, key: str) -> tuple[bool, bool]:
        held = [
            n for n in self._notification_log if n.get("source") == source and n.get("key") == key
        ]
        if not self.dismiss_saves:
            return bool(held), False
        self._notification_log[:] = [n for n in self._notification_log if n not in held]
        return bool(held), True

    async def ack_notification(self, ts: str) -> bool:
        for note in self._notification_log:
            if note["ts"] == ts:
                note["acked"] = True
        return True

    async def _rewrite_notifications_async(self) -> None:
        self.rewrites += 1


def _world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refused: dict[str, str],
    shared: tuple[str, ...] = ("alpha-mcp", "beta-mcp"),
    enabled: bool = True,
    launches: dict[str, str] | None = None,
) -> None:
    """Sharing config and the approval store's refusals, as one gateway start left them."""
    from kiro_crew.config import loader

    gateway = SimpleNamespace(enabled=enabled, stub_servers=list(shared))
    monkeypatch.setattr(
        loader.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(mcp_gateway=gateway))
    )
    monkeypatch.setattr(
        launch_approval,
        "refused_servers",
        lambda path=None: {
            name: {"reason": reason, "expected_launch": (launches or {}).get(name, "launch-1")}
            for name, reason in refused.items()
        },
    )


async def test_names_each_approved_share_that_is_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, refused={"beta-mcp": CHANGED, "alpha-mcp": CHANGED})
    state = _State()

    assert await note_mod.publish(state) is True

    (note,) = state._notification_log
    assert note["channel"] == "system.approval"
    assert note["priority"] == "default"
    assert note["url"] == "/mcp?view=sharing&review=1"
    assert note["key"] == note_mod.KEY
    assert note["body"] == (
        "Paused: alpha-mcp, beta-mcp. Review shared servers to approve the update and share again."
    )
    assert note["i18n"] == {"id": "mcp_share_paused", "params": {"names": "alpha-mcp, beta-mcp"}}
    assert note[note_mod.NOTE_KEY] == {
        "launches": {"alpha-mcp": "launch-1", "beta-mcp": "launch-1"}
    }


async def test_a_note_that_was_not_stored_is_not_reported_as_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED})
    state = _State()
    state.persist_ok = False

    assert await note_mod.publish(state) is False


@pytest.mark.parametrize(
    ("refused", "shared", "enabled"),
    [
        pytest.param({"alpha-mcp": UNAPPROVED}, ("alpha-mcp",), True, id="never-approved"),
        pytest.param({"other-mcp": CHANGED}, ("alpha-mcp",), True, id="not-on-the-share-list"),
        pytest.param({"alpha-mcp": CHANGED}, ("alpha-mcp",), False, id="sharing-off"),
        pytest.param({}, ("alpha-mcp",), True, id="nothing-refused"),
    ],
)
async def test_raises_no_note(
    monkeypatch: pytest.MonkeyPatch,
    refused: dict[str, str],
    shared: tuple[str, ...],
    enabled: bool,
) -> None:
    _world(monkeypatch, refused=refused, shared=shared, enabled=enabled)
    state = _State()

    assert await note_mod.publish(state) is False

    assert state._notification_log == []
    assert state.rewrites == 0


async def test_the_same_paused_servers_are_not_announced_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED})
    state = _State()
    assert await note_mod.publish(state) is True
    # Read by the owner: a restart with the same servers paused is still not news.
    state._notification_log[0]["acked"] = True

    assert await note_mod.publish(state) is False

    assert len(state._notification_log) == 1


async def test_a_different_set_is_announced_under_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED})
    state = _State()
    assert await note_mod.publish(state) is True
    _world(monkeypatch, refused={"alpha-mcp": CHANGED, "beta-mcp": CHANGED})

    assert await note_mod.publish(state) is True

    newest = state._notification_log[-1]
    assert newest["key"] == note_mod.KEY
    assert newest["i18n"]["params"] == {"names": "alpha-mcp, beta-mcp"}


async def test_the_same_server_refused_for_another_launch_is_announced_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-1"})
    state = _State()
    assert await note_mod.publish(state) is True
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-2"})

    assert await note_mod.publish(state) is True

    (note,) = state._notification_log
    assert note[note_mod.NOTE_KEY] == {"launches": {"alpha-mcp": "launch-2"}}


async def test_a_new_pause_is_not_published_when_the_old_note_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-1"})
    state = _State()
    assert await note_mod.publish(state) is True
    state.dismiss_saves = False
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-2"})

    assert await note_mod.publish(state) is False

    (note,) = state._notification_log
    assert note[note_mod.NOTE_KEY] == {"launches": {"alpha-mcp": "launch-1"}}


async def test_a_start_that_finds_nothing_paused_removes_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED})
    state = _State()
    assert await note_mod.publish(state) is True
    _world(monkeypatch, refused={})

    assert await note_mod.publish(state) is False

    assert state._notification_log == []


async def test_a_resolved_note_whose_removal_is_not_saved_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    _world(monkeypatch, refused={"alpha-mcp": CHANGED})
    state = _State()
    assert await note_mod.publish(state) is True
    (note,) = state._notification_log
    state.dismiss_saves = False
    _world(monkeypatch, refused={})

    with caplog.at_level(logging.WARNING, logger=note_mod.__name__):
        assert await note_mod.publish(state) is False

    assert state._notification_log == [note]
    assert not note.get("acked")
    assert (
        note_mod.__name__,
        logging.WARNING,
        "paused-share note: could not remove the resolved note",
    ) in caplog.record_tuples


async def test_the_same_launch_is_announced_again_after_sharing_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-2"})
    state = _State()
    assert await note_mod.publish(state) is True
    _world(monkeypatch, refused={})
    assert await note_mod.publish(state) is False
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-2"})

    assert await note_mod.publish(state) is True

    (note,) = state._notification_log
    assert note[note_mod.NOTE_KEY] == {"launches": {"alpha-mcp": "launch-2"}}
    assert not note.get("acked")


async def test_a_second_pause_after_approval_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-1"})
    state = _State()
    assert await note_mod.publish(state) is True
    _world(monkeypatch, refused={})
    await note_mod.publish(state)
    _world(monkeypatch, refused={"alpha-mcp": CHANGED}, launches={"alpha-mcp": "launch-2"})

    assert await note_mod.publish(state) is True

    assert state._notification_log[-1].get("acked") is None


async def test_a_deleted_note_is_published_again_while_the_servers_stay_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world(monkeypatch, refused={"alpha-mcp": CHANGED})
    state = _State()
    assert await note_mod.publish(state) is True
    state._notification_log.clear()

    assert await note_mod.publish(state) is True

    assert len(state._notification_log) == 1


async def test_nothing_paused_and_no_note_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, refused={})
    state = _State()

    assert await note_mod.publish(state) is False

    assert state.rewrites == 0


# ── the gateway start's call ──


async def _run_persist(orch, approvals) -> None:
    """Schedule the approval persist, mark the process ready, and wait for the task."""
    from kiro_crew.testing.wait import await_until

    orch._background_tasks = set()
    orch._schedule_mcp_launch_approval_persist(approvals)
    (task,) = orch._background_tasks
    orch._mcp_launch_approval_ready.set()
    await await_until(task, what="the approval persist task finishing")


@pytest.mark.parametrize("refused", [{"ALPHA_MCP": CHANGED}, {}], ids=["refused", "none"])
async def test_a_start_publishes_once_its_refusals_are_saved(refused: dict[str, str]) -> None:
    from unittest.mock import AsyncMock, patch

    from slack_gateway_coverage_helpers import _make_orchestrator

    from kiro_crew.slack import gateway as gw

    orch = _make_orchestrator()
    orch.dashboard_state = object()
    order: list[str] = []
    with (
        patch.object(gw, "save_pass", side_effect=lambda approvals: order.append("saved")),
        patch.object(
            note_mod, "publish", AsyncMock(side_effect=lambda state: order.append("published"))
        ) as publish,
    ):
        await _run_persist(orch, SimpleNamespace(refused=refused))

    publish.assert_awaited_once_with(orch.dashboard_state)
    assert order == ["saved", "published"]


@pytest.mark.parametrize(
    ("refused", "save_error"),
    [
        pytest.param({"ALPHA_MCP": CHANGED}, OSError("disk full"), id="refusals-not-saved"),
    ],
)
async def test_a_start_whose_refusals_were_not_saved_publishes_nothing(
    refused: dict[str, str], save_error
) -> None:
    from unittest.mock import AsyncMock, patch

    from slack_gateway_coverage_helpers import _make_orchestrator

    from kiro_crew.slack import gateway as gw

    orch = _make_orchestrator()
    orch.dashboard_state = object()
    with (
        patch.object(gw, "save_pass", side_effect=save_error),
        patch.object(note_mod, "publish", AsyncMock()) as publish,
    ):
        await _run_persist(orch, SimpleNamespace(refused=refused))

    publish.assert_not_awaited()


async def test_a_failed_note_is_logged_and_does_not_fail_the_start(caplog) -> None:
    import logging
    from unittest.mock import AsyncMock, patch

    from slack_gateway_coverage_helpers import _make_orchestrator

    from kiro_crew.slack import gateway as gw

    orch = _make_orchestrator()
    orch.dashboard_state = object()
    with (
        patch.object(gw, "save_pass"),
        patch.object(note_mod, "publish", AsyncMock(side_effect=RuntimeError("bus down"))),
        caplog.at_level(logging.WARNING, logger="kiro_crew.slack.gateway"),
    ):
        await _run_persist(orch, SimpleNamespace(refused={"ALPHA_MCP": CHANGED}))

    assert "paused-share note failed" in caplog.text


async def test_a_start_with_no_shared_server_still_runs_the_publisher() -> None:
    from unittest.mock import AsyncMock, patch

    from slack_gateway_coverage_helpers import _make_orchestrator

    from kiro_crew.testing.wait import await_until

    orch = _make_orchestrator()
    orch.dashboard_state = object()
    orch._background_tasks = set()
    with patch.object(note_mod, "publish", AsyncMock()) as publish:
        await orch._init_mcp_gateway(stub_servers=frozenset())
        (task,) = orch._background_tasks
        orch._mcp_launch_approval_ready.set()
        await await_until(task, what="the paused-share note task finishing")

    publish.assert_awaited_once_with(orch.dashboard_state)

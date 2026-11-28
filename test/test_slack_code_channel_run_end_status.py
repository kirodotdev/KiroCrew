"""Code-channel status at the end of a run: the lead's turn ended while its workers
ran, then the workers finished in some order. Slack's "Working..." loader (and its
Stop) must be gone once the lead turn and every worker are done, and stay while any
worker still runs.

The real status driver over a fake Slack web client, the real turn hooks of
:mod:`~kiro_crew.slack.code_channels.status` and the workspace service, and the
real watch pass; only the dashboard's slots are fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import test_slack_code_channel_workspace as _ws
from slack_code_channels_helpers import store_channel_binding
from test_slack_code_channel_worker_status import _StatusSlack

from kiro_crew.slack.code_channels import status
from kiro_crew.slack.code_channels.board import source
from kiro_crew.slack.code_channels.workspace import service
from kiro_crew.slack.code_channels.workspace.context_bar import ACTION_STOP

_no_sandbox = _ws._no_sandbox
_feature_on = _ws._feature_on
audits = _ws.audits
_repo_template = _ws._repo_template
repo = _ws.repo

WORKERS = ["chat-w1", "chat-w2", "chat-w3", "chat-w4"]


class _Slack(_StatusSlack):
    """Also keeps what Slack took (a failed write shows nothing new); with
    ``refuse_active`` set every ``active`` fails."""

    def __init__(self) -> None:
        super().__init__()
        self.taken: list[str] = []
        self.refuse_active = False
        #: When set, an ``active`` write waits for it (a slow Slack reply).
        self.hold_active: asyncio.Event | None = None
        self.active_pending = False

    async def _api_call(self, method: str, **kwargs: Any) -> Any:
        if self.refuse_active and kwargs["params"]["status"] == "active":
            self.fail = ConnectionError("connection reset")
        if self.hold_active is not None and kwargs["params"]["status"] == "active":
            self.active_pending = True
            await self.hold_active.wait()
            self.active_pending = False
        resp = await super()._api_call(method, **kwargs)
        self.taken.append(kwargs["params"]["status"])
        return resp


@pytest.fixture
def slack():
    fake = _Slack()
    status.set_slack_provider(lambda: fake)
    status._status_locks.clear()
    yield fake
    status.set_slack_provider(None)
    status._status_locks.clear()
    status._held.clear()
    status._after_turn.clear()
    status._ended.clear()


@pytest.fixture
def scope_admits():
    source.set_scope_predicate(lambda _state, _binding: True)
    yield
    source.set_scope_predicate(None)


class _Run:
    """A conductor-led channel: the lead's turns go through the real turn hooks."""

    def __init__(self, tmp_path, repo, monkeypatch, *, filed: bool) -> None:
        self.h = _ws.Harness(tmp_path)
        self.dash = _ws.FakeDashboard()
        store_channel_binding(tmp_path, monkeypatch)

        async def create_folder(state, parent_id, name, project_dir):
            return state.add_folder(f"F{len(state._folders)}", name, parent_id, project_dir)

        self.h.svc._create_folder = create_folder
        self.folder_id, self.worktree = self.h.run(
            self.h.svc.plan_session(
                self.dash,
                channel_id="C1",
                team_id="T1",
                title="t",
                channel_name="c",
                project_dir=str(repo),
                parent_folder_id="F0",
            )
        )
        monkeypatch.setattr(self.h.svc, "schedule_refresh", lambda *_a, **_k: None)
        self.lead = self.dash.add_slot("chat-conductor", self.folder_id)
        self.binding = _ws.FakeBinding(
            "C1", project_dir=self.worktree, session_key="chat-conductor"
        )
        self.filed = filed

    def spawn(self, key: str) -> None:
        from kiro_crew.slack.code_channels.store import store

        # Filed under the channel's folder, or a worker in its own tree known
        # only by lineage (the store's record of the conductor that made it).
        slot = self.dash.add_slot(key, self.folder_id if self.filed else "", running=True)
        slot._turn_generation = 1
        if not self.filed:
            store().record_worker(key, "chat-conductor")

    def finish(self, key: str) -> None:
        self.dash._slots[key].turn_running = False

    async def tick(self) -> None:
        await self.h.svc.watch_tick(self.dash)

    async def lead_start(self) -> None:
        self.lead.turn_running = True
        self.lead._turn_generation += 1
        await status._on_start(self.binding, None)
        await self.h.svc.on_turn_start(self.binding)

    async def lead_end(self) -> None:
        # The end hooks run before the slot stops reading as running.
        await status._on_end(self.binding, None)
        await self.h.svc.on_turn_end(self.binding)
        self.lead.turn_running = False

    def lead_task(self, running: bool) -> None:
        """The lead's slot holds a task that fires no turn hooks: a wake that ends
        at the dispatch gates (an agent-started turn with nothing pending), a queue
        drain that runs no turn. It still reads as running, under a new generation."""
        if running:
            self.lead._turn_generation += 1
        self.lead.turn_running = running


def _shown(slack: _Slack) -> str:
    """What Slack shows: the last status it took (``active`` before any)."""
    return slack.taken[-1] if slack.taken else "active"


async def _lead_dispatches_and_ends(run: _Run) -> None:
    await run.lead_start()
    assert _stop_offered(run.h.client.of("set_context_bar")[-1])  # the lead's turn runs
    for key in WORKERS:
        run.spawn(key)
    await run.tick()  # a pass while the lead's turn still runs
    await run.lead_end()
    await run.tick()


ORDERS = {
    "in-order": WORKERS,
    "reverse": list(reversed(WORKERS)),
    "middle-first": ["chat-w2", "chat-w4", "chat-w1", "chat-w3"],
}


@pytest.mark.parametrize("filed", [True, False], ids=["filed", "lineage"])
@pytest.mark.parametrize("order", list(ORDERS), ids=list(ORDERS))
@pytest.mark.parametrize(
    "hand_back",
    [
        "none",
        "between-passes",
        "pass-in-turn",
        "before-pass-sees-it",
        "no-hooks-across-a-pass",
        "no-hooks-between-passes",
        "turn-end-clear-fails",
    ],
)
def test_the_last_worker_finishing_after_the_lead_turn_ends_clears_the_status(
    tmp_path, repo, audits, monkeypatch, slack, scope_admits, filed, order, hand_back
):
    """``hand_back`` is how each finished worker wakes the lead (its report starts a
    lead turn): never; a whole lead turn between two passes; a pass while that turn
    runs; the lead's turn starting before any pass saw the worker finish; a lead
    task that fires no turn hooks, seen by a pass or only by its new generation;
    or a lead turn whose turn-end ``active`` Slack did not take."""
    run = _Run(tmp_path, repo, monkeypatch, filed=filed)

    async def scenario() -> list[str]:
        await _lead_dispatches_and_ends(run)
        assert _shown(slack) == "processing"
        trail = []
        for i, key in enumerate(ORDERS[order]):
            last = i == len(WORKERS) - 1
            if hand_back == "before-pass-sees-it":
                run.finish(key)
                await run.lead_start()
                await run.lead_end()
                await run.tick()
            elif hand_back == "pass-in-turn":
                run.finish(key)
                await run.tick()
                await run.lead_start()
                await run.tick()
                await run.lead_end()
                await run.tick()
            elif hand_back == "no-hooks-across-a-pass":
                run.finish(key)
                run.lead_task(True)
                await run.tick()
                run.lead_task(False)
                await run.tick()
            elif hand_back == "no-hooks-between-passes":
                run.finish(key)
                run.lead_task(True)
                run.lead_task(False)
                await run.tick()
            elif hand_back == "turn-end-clear-fails":
                run.finish(key)
                await run.tick()
                await run.lead_start()
                slack.fail = ConnectionError("connection reset")
                await run.lead_end()
                await run.tick()
            elif hand_back == "between-passes":
                run.finish(key)
                await run.tick()
                await run.lead_start()
                await run.lead_end()
                await run.tick()
            else:
                run.finish(key)
                await run.tick()
            await run.tick()
            trail.append(_shown(slack))
            if not last:
                assert _shown(slack) == "processing", (key, slack.sent)
        return trail

    trail = asyncio.run(scenario())
    # Slack's "Working..." and its "Stop agent" go with ``processing``.
    assert trail[-1] == "active", slack.sent
    assert not status.owes_clear("C1")
    # The context bar's own Stop went with the lead's turn.
    asyncio.run(run.h.svc.refresh(run.binding))
    bars = run.h.client.of("set_context_bar")
    assert bars and not _stop_offered(bars[-1]), bars[-1]


def _stop_offered(bar: dict) -> bool:
    """Whether the context bar carries the Stop action."""
    return any(
        item.get("item_type") == "action" and item.get("key") == ACTION_STOP
        for item in bar["context_bar_items"]
    )


def test_a_run_with_one_worker_still_going_stays_working(
    tmp_path, repo, audits, monkeypatch, slack, scope_admits
):
    run = _Run(tmp_path, repo, monkeypatch, filed=False)

    async def scenario() -> None:
        await _lead_dispatches_and_ends(run)
        for key in WORKERS[:3]:
            run.finish(key)
            await run.tick()
            await run.lead_start()
            await run.lead_end()
            await run.tick()
        for _ in range(3):
            await run.tick()

    asyncio.run(scenario())
    assert _shown(slack) == "processing", slack.sent
    assert status.owes_clear("C1")


def test_a_stranded_status_slack_keeps_refusing_is_retried_a_bounded_number_of_times(
    tmp_path, repo, audits, monkeypatch, slack, scope_admits
):
    run = _Run(tmp_path, repo, monkeypatch, filed=False)

    async def scenario() -> None:
        await _lead_dispatches_and_ends(run)
        for key in WORKERS:
            run.finish(key)
        run.lead_task(True)  # the last hand-back's wake fires no turn hooks
        await run.tick()
        run.lead_task(False)
        slack.refuse_active = True
        for _ in range(10):
            await run.tick()

    asyncio.run(scenario())
    tries = slack.sent[slack.sent.index("processing", 2) :].count("active")
    assert tries == service._STATUS_RETRIES


def test_a_lead_turn_starting_while_the_stranded_status_is_cleared_still_shows_working(
    tmp_path, repo, audits, monkeypatch, slack, scope_admits
):
    """The clear is in flight when the lead's next turn starts: the turn's
    ``processing`` lands after it, not deduplicated against the stale record."""
    run = _Run(tmp_path, repo, monkeypatch, filed=False)

    async def scenario() -> None:
        await _lead_dispatches_and_ends(run)
        for key in WORKERS:
            run.finish(key)
        run.lead_task(True)  # the last hand-back's wake fires no turn hooks
        await run.tick()
        run.lead_task(False)
        slack.hold_active = asyncio.Event()
        clear = asyncio.ensure_future(run.tick())
        await _ws._until(lambda: slack.active_pending)
        start = asyncio.ensure_future(run.lead_start())
        # The turn's start runs as far as it can: done, or parked on the lock.
        lock = status._status_lock("C1")
        await _ws._until(lambda: start.done() or bool(getattr(lock, "_waiters", None)))
        slack.hold_active.set()
        await clear
        await start

    asyncio.run(scenario())
    assert _shown(slack) == "processing", slack.sent


def test_a_lead_turn_whose_start_hook_gives_up_during_the_clear_still_shows_working(
    tmp_path, repo, audits, monkeypatch, slack, scope_admits
):
    """The clear is slower than the start hook's deadline: the hook is cancelled
    while it waits, and the clear puts the running turn's ``processing`` back."""
    run = _Run(tmp_path, repo, monkeypatch, filed=False)
    monkeypatch.setattr(status, "_channel_of", lambda _key: run.binding)
    monkeypatch.setattr(status, "_turn_running", lambda _binding: run.lead.turn_running)

    async def scenario() -> None:
        await _lead_dispatches_and_ends(run)
        for key in WORKERS:
            run.finish(key)
        run.lead_task(True)  # the last hand-back's wake fires no turn hooks
        await run.tick()
        run.lead_task(False)
        slack.hold_active = asyncio.Event()
        clear = asyncio.ensure_future(run.tick())
        await _ws._until(lambda: slack.active_pending)
        run.lead.turn_running = True
        run.lead._turn_generation += 1
        start = asyncio.ensure_future(status._on_start(run.binding, None))
        lock = status._status_lock("C1")
        await _ws._until(lambda: bool(getattr(lock, "_waiters", None)))
        start.cancel()  # what hooks._run's deadline does to a hook still waiting
        with pytest.raises(asyncio.CancelledError):
            await start
        slack.hold_active.set()
        await clear

    asyncio.run(scenario())
    assert _shown(slack) == "processing", slack.sent


def test_a_stranded_status_is_not_cleared_while_the_channels_policy_denies_slack(
    tmp_path, repo, audits, monkeypatch, slack, scope_admits
):
    from kiro_crew.slack.code_channels import surface

    run = _Run(tmp_path, repo, monkeypatch, filed=False)
    allowed = {"slack": True}

    async def outbound() -> bool:
        return allowed["slack"]

    monkeypatch.setattr(surface, "outbound_permitted", outbound)

    async def scenario() -> list[str]:
        await _lead_dispatches_and_ends(run)
        for key in WORKERS:
            run.finish(key)
        run.lead_task(True)  # the last hand-back's wake fires no turn hooks
        await run.tick()
        run.lead_task(False)
        allowed["slack"] = False
        for _ in range(3):
            await run.tick()
        denied = list(slack.sent)
        allowed["slack"] = True
        await run.tick()
        return denied

    denied = asyncio.run(scenario())
    assert denied[-1] == "processing", denied
    assert _shown(slack) == "active", slack.sent

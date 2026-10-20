"""Code channels: every side effect this PR adds asks the live fences right before it.

The write gate (:func:`kiro_crew.slack.code_channels.surface.write_refusal`) before
each Slack write, and the act gate (:func:`~kiro_crew.slack.code_channels.surface.act_refusal`)
before each inbound side effect (a turn routed, comments marked seen, a wrap-up
armed), each asked after the last await. Split from ``test_slack_code_channel_api_gaps``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_slack_code_channel_api_gaps import (  # noqa: F401 - the env fixture
    CH,
    OWNER,
    SESSION,
    SLOT,
    STRANGER,
    _bind,
    _calls,
    _canvas,
    _plan,
    _thread,
    env,
)

from kiro_crew.slack.code_channels import (
    actions,
    commands,
    comments,
    notices,
    status,
    surface,
    tools,
)
from kiro_crew.slack.code_channels.board import publisher
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import store as surface_store

pytestmark = pytest.mark.asyncio

# ── The write gate: every Slack write this PR adds asks it right before it ──
# (channels policy, archive, Slack's Stop, Off/Review, the board's scope).


def _deny_policy(monkeypatch: Any) -> None:
    async def denied() -> bool:
        return False

    monkeypatch.setattr(surface, "outbound_permitted", denied)


def test_the_resource_is_held_back_by_the_channels_policy(tmp_path: Path, monkeypatch: Any) -> None:
    """GPT on cc92885304: the PR resource went out with the channels policy denying Slack."""
    from kiro_crew.slack.code_channels.workspace.service import WorkspaceService
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    sent: list[dict[str, Any]] = []

    class Client:
        async def set_agent_resource(self, channel_id: str, **resource: Any) -> None:
            sent.append(resource)

    from kiro_crew.slack.code_channels.workspace import service as service_mod

    monkeypatch.setattr(service_mod, "_activation_admits", lambda b: True)  # On
    _deny_policy(monkeypatch)
    svc = WorkspaceService(lambda: Client(), store=None)
    record = WorkspaceRecord(
        team_id="T1",
        channel_id=CH,
        pr_number=42,
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state="open",
        pr_title="Fix login",
    )
    asyncio.run(svc._publish_resource(record, SimpleNamespace(channel_id=CH)))
    assert sent == [] and record.resource_hash == ""


async def test_a_view_write_queued_behind_the_lock_asks_the_gate_again(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """GPT on cc92885304: admitted when the tool started, Off by the time the view
    lock was free: the queued view is not sent."""
    from kiro_crew.slack.code_channels import views
    from kiro_crew.testing.wait import until_parked

    _bind(env)
    admitted = {"now": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admitted["now"])
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    lock = views._lock(CH)
    async with lock:  # another write or an eviction holds the channel's views
        running = asyncio.ensure_future(
            tools.run_tool(
                env.state,
                SESSION,
                tools.SET_VIEW,
                {"type": "html", "view_key": "report", "content": "<html></html>"},
            )
        )
        await until_parked(lock, timeout=5)
        admitted["now"] = False  # the operator sets the channel Off meanwhile
    out = await asyncio.wait_for(running, timeout=10)
    assert not out.ok and out.text == tools.CHANNEL_OFF_TEXT
    assert _calls(env, "agents.conversations.setView") == []


async def test_the_live_summary_is_held_back_by_the_channels_policy(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    binding = _bind(env)
    _deny_policy(monkeypatch)
    await publisher._record_summary(binding, "300.000001")
    assert _calls(env, "agents.conversations.setProperties") == []


async def test_an_approval_status_is_held_back_by_the_channels_policy(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    _bind(env)
    writes: list[str] = []

    async def set_status(channel_id: str, value: str) -> bool:
        writes.append(value)
        return True

    monkeypatch.setattr(status, "set_status", set_status)
    monkeypatch.setattr(status, "_gate", lambda: True)
    monkeypatch.setattr(status, "_turn_running", lambda b: True)
    _deny_policy(monkeypatch)
    env.store.load()
    status.note_approval(SLOT, pending=True, request_id="r1")
    await asyncio.wait_for(asyncio.gather(*list(status._approval_tasks)), timeout=10)
    assert writes == []


async def test_a_click_reply_is_held_back_by_the_channels_policy(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    _bind(env)
    _deny_policy(monkeypatch)
    before = len(env.slack.ephemerals)
    await actions._responder(CH, OWNER)("That button isn't one Kiro Crew offers any more.")
    assert len(env.slack.ephemerals) == before


async def test_a_workspace_eviction_is_held_back_by_the_channels_policy(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import views
    from kiro_crew.slack.code_channels.workspace.service import _view_gate

    surface_store().update(CH, views=[{"key": "old", "view_id": "Vold", "type": "html", "at": "1"}])
    removed: list[str] = []

    class Client:
        async def list_views(self, channel_id: str) -> list[dict[str, str]]:
            return [{"view_id": "Vold", "view_key": "old"}]

        async def remove_view(self, channel_id: str, view_id: str) -> None:
            removed.append(view_id)

    _deny_policy(monkeypatch)
    assert await views.make_room(Client(), CH, for_key="diff", admits=_view_gate(CH, None)) == ""
    assert removed == []


def _policy(monkeypatch: Any) -> dict[str, bool]:
    allowed = {"now": True}

    async def permitted() -> bool:
        return allowed["now"]

    monkeypatch.setattr(surface, "outbound_permitted", permitted)
    return allowed


async def test_the_command_set_is_held_back_by_a_policy_denied_mid_sync(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """Admitted at the top of the sync, denied while the gh check ran: not sent."""
    binding = _bind(env)
    allowed = _policy(monkeypatch)

    async def capability(_b: Any) -> tuple[bool, str]:
        allowed["now"] = False  # the operator denies Slack meanwhile
        return False, "not_a_repo"

    monkeypatch.setattr(commands, "pr_capability", capability)
    await commands.sync_commands(binding)
    assert _calls(env, "agents.conversations.setCommands") == []


async def test_a_stop_settle_is_held_back_by_the_channels_policy(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    _bind(env)
    sent: list[str] = []

    class Driver:
        async def settle_after_stop(self, channel: str, _ts: Any) -> None:
            sent.append(channel)

    monkeypatch.setattr(status, "_driver", lambda: Driver())
    _deny_policy(monkeypatch)
    await status.settle_after_stop(CH)
    assert sent == []


@pytest.mark.parametrize("hook", ["_on_start", "_on_end"])
async def test_a_turn_hook_status_is_held_back_by_the_channels_policy(
    env: Any, monkeypatch: Any, hook: str  # noqa: F811
) -> None:
    binding = _bind(env)
    writes: list[str] = []

    async def set_status(channel_id: str, value: str) -> bool:
        writes.append(value)
        return True

    monkeypatch.setattr(status, "set_status", set_status)
    _deny_policy(monkeypatch)
    await getattr(status, hook)(binding, SimpleNamespace(outcome="ok"))
    assert writes == []


async def test_an_archive_stamped_after_the_callers_copy_is_seen_by_the_gate(
    env: Any,  # noqa: F811
) -> None:  # noqa: F811
    """The in-process fence is lifted once the stamp is written: the gate reads
    the stamp as stored now, not only the caller's older copy."""
    from dataclasses import replace

    stale = _bind(env)
    env.store.replace_binding(replace(stale, archived_at=1700000000.0))
    assert surface.write_refusal_now(CH, stale) == "archived"


async def test_an_unreadable_config_refuses_a_write_that_needs_the_channel_on(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import binding as binding_mod

    binding = _bind(env)
    monkeypatch.setattr(binding_mod, "live_config", lambda _orch=None: None)
    assert surface.write_refusal_now(CH, binding, activation=True) == "channel_off"


# ── The act gate: every inbound side effect after an await asks the live fences ──
# (inbound policy, workspace, archive, rebind, Stop, Off/Review) right before it.


def _during_canvas_read(
    env: Any, monkeypatch: Any, change: Callable[[], None]  # noqa: F811
) -> None:  # noqa: F811
    """*change* happens while getCanvas is out (after the read's own early gates)."""
    real = surface.client()

    class Racing:
        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        async def get_canvas(self, *args: Any, **kwargs: Any) -> Any:
            data = await real.get_canvas(*args, **kwargs)
            change()
            return data

    monkeypatch.setattr(surface, "client", lambda: Racing())


def _archive(env: Any, binding: Binding) -> None:  # noqa: F811
    from dataclasses import replace

    env.store.replace_binding(replace(binding, archived_at=1700000000.0))


def _seen() -> tuple[str, ...]:
    record = surface_store().get(CH)
    return tuple(record.canvas_seen) if record is not None else ()


async def test_a_channel_archived_during_the_canvas_read_routes_and_marks_nothing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """GPT on 6cf7487e7e: archived while getCanvas was out, the feedback still
    started a turn and the comments were marked seen."""
    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(
        _thread("A1", OWNER, "hi"), _thread("A2", STRANGER, "not mine to deliver")
    )
    _during_canvas_read(env, monkeypatch, lambda: _archive(env, binding))
    assert await comments.read_comments(binding) == 0
    assert env.routed == [] and _seen() == ()


@pytest.mark.parametrize("fence", ["off", "policy", "rebound"])
async def test_a_fence_shut_during_the_canvas_read_leaves_the_comments_unseen(
    env: Any, monkeypatch: Any, fence: str  # noqa: F811
) -> None:
    from dataclasses import replace

    from kiro_crew.slack.code_channels import routing

    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    admits = {"ok": True}
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda *_a: admits["ok"])

    def shut() -> None:
        if fence == "off":
            admits["ok"] = False
        elif fence == "policy":
            env.inbound_ok = False
        else:
            env.store.replace_binding(replace(binding, session_key="chat-9-9"))

    _during_canvas_read(env, monkeypatch, shut)
    assert await comments.read_comments(binding) == 0
    assert env.routed == [] and _seen() == ()


async def test_a_baseline_read_on_a_channel_archived_meanwhile_writes_nothing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    binding = _bind(env, running=False)
    _plan(env, baseline=False)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "old"))
    _during_canvas_read(env, monkeypatch, lambda: _archive(env, binding))
    assert await comments.read_comments(binding) == 0
    record = surface_store().get(CH)
    assert record is not None and record.canvas_baseline == "" and _seen() == ()


async def test_comments_passed_over_are_not_marked_once_the_channel_is_archived(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """Archived during the delivery: the stranger's comment is left as it was;
    the delivered one is recorded, so an unarchive never delivers it twice."""
    from kiro_crew.slack.code_channels import routing

    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(
        _thread("A1", OWNER, "hi"), _thread("A2", STRANGER, "nope")
    )
    real_route = routing._route_to_slot

    async def route_then_archive(*args: Any, **kwargs: Any) -> Any:
        routed = await real_route(*args, **kwargs)
        _archive(env, binding)
        return routed

    monkeypatch.setattr(routing, "_route_to_slot", route_then_archive)
    assert await comments.read_comments(binding) == 1
    assert _seen() == ("A1",)


async def test_a_click_on_a_channel_shut_while_its_binding_was_read_does_nothing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from kiro_crew.slack.agent_sessions import session_events

    binding = _bind(env)
    stops: list[Any] = []

    async def handle_stopped(_orch: Any, event: dict[str, Any]) -> str:
        stops.append(event)
        return "channel"

    monkeypatch.setattr(session_events, "handle_stopped", handle_stopped)
    real_store = actions.store

    class Store:
        def get_binding(self, channel: str) -> Any:
            env.inbound_ok = False  # the channels policy denies inbound Slack meanwhile
            return real_store().get_binding(channel)

    monkeypatch.setattr(actions, "store", lambda: Store())
    payload = {"team_id": "T1", "event_id": "Ev1"}
    event = {"type": actions.EVENT, "channel": CH, "user": OWNER, "action_key": "stop"}
    assert await actions.handle(env.orch, payload, event) == "denied"
    assert stops == [] and binding.channel_id == CH


async def test_a_click_on_an_archived_channel_does_nothing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:  # noqa: F811
    from kiro_crew.slack.agent_sessions import session_events

    _archive(env, _bind(env))
    stops: list[Any] = []

    async def handle_stopped(_orch: Any, event: dict[str, Any]) -> str:
        stops.append(event)
        return "channel"

    monkeypatch.setattr(session_events, "handle_stopped", handle_stopped)
    payload = {"team_id": "T1", "event_id": "Ev2"}
    event = {"type": actions.EVENT, "channel": CH, "user": OWNER, "action_key": "stop"}
    assert await actions.handle(env.orch, payload, event) == "denied"
    assert stops == []


@pytest.mark.parametrize("command", ["summarize", "wrap-up"])
async def test_a_command_shut_during_its_own_checks_starts_no_turn(
    env: Any, monkeypatch: Any, command: str  # noqa: F811
) -> None:
    """The archive read is an await: a deny during it holds back the arm and the turn."""
    from kiro_crew.slack.code_channels import wrapup

    _bind(env, running=False)

    async def is_archived(_binding: Any) -> bool:
        env.inbound_ok = False  # the channels policy denies inbound Slack meanwhile
        return False

    monkeypatch.setattr(surface, "is_archived", is_archived)

    async def respond(_text: str) -> None:
        return None

    await commands.run_command(
        env.orch, command=command, channel=CH, user=OWNER, team="T1", text="", respond=respond
    )
    assert env.routed == []
    record = surface_store().get(CH)
    assert record is None or not record.wrapup_pending_since
    assert CH not in wrapup._requests


def _archive_while_the_window_loads(env: Any, binding: Binding) -> None:  # noqa: F811
    """The session is a sidebar row restored after a restart: the route reads its
    transcript off the loop first, and the channel is archived during that read."""
    slot = env.state.linked[binding.anchor_ts]
    slot.window_pending = True

    def read(_slot: Any) -> tuple[()]:
        _archive(env, binding)
        return ()

    slot._lazy_window = SimpleNamespace(read=read)
    slot.load_window = lambda *_a: setattr(slot, "window_pending", False)


async def test_a_comment_route_waits_for_the_window_then_asks_the_fences(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    _archive_while_the_window_loads(env, binding)
    assert await comments.read_comments(binding) == 0
    assert env.routed == [] and _seen() == ()


@pytest.mark.parametrize("command", ["summarize", "wrap-up"])
async def test_a_command_route_waits_for_the_window_then_asks_the_fences(
    env: Any, monkeypatch: Any, command: str  # noqa: F811
) -> None:
    """deliver_directed's own awaits (a rebind, the window's read) come after the
    command's checks: the fences are asked again right before the route."""
    from kiro_crew.slack.code_channels import wrapup

    binding = _bind(env, running=False)
    _archive_while_the_window_loads(env, binding)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    await commands.run_command(
        env.orch, command=command, channel=CH, user=OWNER, team="T1", text="", respond=respond
    )
    assert env.routed == [] and replies[-1] == commands.ARCHIVED_TEXT
    assert CH not in wrapup._requests
    record = surface_store().get(CH)
    assert record is None or not record.wrapup_pending_since


async def test_a_command_archived_during_its_reply_binds_no_new_session(
    env: Any,  # noqa: F811
) -> None:
    """The session was closed in the dashboard, so delivery would bind a new one
    (a kickoff post, a session): the fences are asked before that bind too."""
    binding = _bind(env, running=False)
    del env.state.linked[binding.anchor_ts]  # closed in the dashboard

    async def respond(_text: str) -> None:
        _archive(env, binding)  # archived while the acknowledgement went out

    await commands.run_command(
        env.orch, command="summarize", channel=CH, user=OWNER, team="T1", text="", respond=respond
    )
    assert env.created_sessions == [] and env.routed == []


async def test_a_refused_wrap_up_leaves_another_armed_wrap_up_alone(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from dataclasses import replace

    from kiro_crew.slack.code_channels import wrapup

    binding = _bind(env, running=False)

    async def is_archived(_binding: Any) -> bool:
        # The channel is rebound while this /wrap-up checks, and another /wrap-up
        # on the new binding is admitted and armed: this one is refused unarmed.
        env.store.replace_binding(replace(binding, session_key="chat-9-9"))
        await wrapup.request_wrap_up(CH, OWNER)
        return False

    monkeypatch.setattr(surface, "is_archived", is_archived)

    async def respond(_text: str) -> None:
        return None

    await commands.run_command(
        env.orch, command="wrap-up", channel=CH, user=OWNER, team="T1", text="", respond=respond
    )
    assert CH in wrapup._requests


@pytest.mark.parametrize("change", ["toggle", "rebound"])
async def test_a_write_that_needs_the_channel_on_reads_the_toggle_and_binding_live(
    env: Any, change: str  # noqa: F811
) -> None:
    from dataclasses import replace

    binding = _bind(env)
    assert surface.write_refusal_now(CH, binding, activation=True) == ""
    if change == "toggle":
        env.on = False  # code channels turned off while the write waited
        expected = "channel_off"
    else:
        env.store.replace_binding(replace(binding, session_key="chat-9-9"))
        expected = "rebound"
    assert surface.write_refusal_now(CH, binding, activation=True) == expected
    # A turn-end status (no activation) still clears its loader.
    assert surface.write_refusal_now(CH, binding) == ""


async def test_a_refusal_notice_for_a_channel_archived_meanwhile_is_not_sent(
    env: Any,  # noqa: F811
) -> None:  # noqa: F811
    _archive(env, _bind(env))
    assert not await notices.notify_refusal(CH, "not_authorized", session_key=SLOT)
    assert env.notified == []


# ── Off the loop: no coroutine this PR adds reads or writes a store on it ──
# (Opus on a9189a875a: notices read the code-channel store on the event loop.)


def _store_off_the_loop(monkeypatch: Any) -> None:
    """Fail any code-channel or surface store read or write made on an event loop."""
    from kiro_crew.slack.code_channels.store import CodeChannelStore
    from kiro_crew.slack.code_channels.surface_store import SurfaceStore

    def guarded(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return real(*args, **kwargs)  # a worker thread: allowed
            raise AssertionError(f"{real.__qualname__} ran on the event loop")

        return call

    for cls, names in (
        (CodeChannelStore, ("get_binding", "bindings", "get_route", "_read")),
        (SurfaceStore, ("get", "update")),
    ):
        for name in names:
            monkeypatch.setattr(cls, name, guarded(getattr(cls, name)))


async def test_a_refusal_notice_reads_the_stores_off_the_loop(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    _bind(env)
    await asyncio.to_thread(env.store.load)  # warm: the guard is about the call, not a load
    _store_off_the_loop(monkeypatch)
    assert await notices.notify_refusal(CH, "not_authorized", session_key=SLOT)
    [(args, _kwargs)] = env.notified
    assert "#fix-login" in args[2]


async def test_an_unreadable_store_refuses_the_notice_without_hiding_other_errors(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """Only the stores' own failures are caught: an on-loop guard still surfaces."""
    _bind(env)

    async def unreadable(_channel: str) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(notices, "get_async", unreadable)
    assert not await notices.notify_refusal(CH, "not_authorized")
    assert env.notified == []

    async def guard(_channel: str) -> Any:
        raise AssertionError("ran on the event loop")

    monkeypatch.setattr(notices, "get_async", guard)
    with pytest.raises(AssertionError):
        await notices.notify_refusal(CH, "not_authorized")


#: The modules this PR adds whole: every coroutine in them is this PR's.
_PR_MODULES = (
    "actions.py",
    "comments.py",
    "notices.py",
    "views.py",
    "workspace/publishing.py",
)


def test_no_coroutine_this_pr_adds_calls_a_store_or_a_file_on_the_loop() -> None:
    """A store or file call inside an ``async def`` must be handed to a thread.

    ``asyncio.to_thread(store().get_binding, ...)`` passes the bound method (no
    call on the loop); ``store().get_binding(...)`` in a coroutine is the bug.
    """
    import ast
    import re

    root = Path(surface.__file__).parent
    pattern = re.compile(
        r"(^|\.)(store\(\)|surface_store\(\)|_store)\.\w+$|"
        r"(^|\.)(read_text|write_text|read_bytes|write_bytes)$|^open$|^json\.load$"
    )
    found: list[str] = []
    for name in _PR_MODULES:
        tree = ast.parse((root / name).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and pattern.search(ast.unparse(node.func)):
                    found.append(f"{name}:{node.lineno} {fn.name}: {ast.unparse(node.func)}")
    assert found == []


# ── Views: a tab is ours by its recorded view id, never by its name ──
# (GPT on a9189a875a: an owned tab named "diff" kept the Code tab from making room.)


async def test_the_code_tab_makes_room_even_when_an_owned_tab_is_named_diff(
    env: Any,  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels.models import CodeChannelApiError
    from kiro_crew.slack.code_channels.workspace.service import WorkspaceService
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    _bind(env)
    # The agent's own tab, literally keyed "diff", is our stalest view: the Code
    # tab (not in the view record) must not mistake it for itself.
    surface_store().update(
        CH,
        views=[
            {"key": "diff", "view_id": "Vagent", "type": "html", "at": "1"},
            {"key": "report", "view_id": "Vreport", "type": "html", "at": "2"},
        ],
    )
    removed: list[str] = []
    sent = {"n": 0}

    class Client:
        async def set_diff_view(self, channel_id: str, content: str, **_k: Any) -> str:
            sent["n"] += 1
            if sent["n"] == 1:
                raise CodeChannelApiError("agents.conversations.setView", "too_many_views")
            return "Vcode"

        async def list_views(self, channel_id: str) -> list[dict[str, Any]]:
            return [{"view_id": "Vagent", "view_key": "diff"}, {"view_id": "Vreport"}]

        async def remove_view(self, channel_id: str, view_id: str = "", **_k: Any) -> None:
            removed.append(view_id)

    svc = WorkspaceService(lambda: Client())
    record = WorkspaceRecord(team_id="T1", channel_id=CH)
    assert await svc._call("set_diff_view", record, "diff", room_for="diff") == "Vcode"
    assert removed == ["Vagent"] and sent["n"] == 2


async def test_rewriting_a_tab_keeps_that_tab_and_evicts_the_stalest_other(
    env: Any,  # noqa: F811
) -> None:  # noqa: F811
    """The view being rewritten (its recorded key) is kept; the stalest other goes."""
    from kiro_crew.slack.code_channels import views

    _bind(env)
    surface_store().update(
        CH,
        views=[
            {"key": "report", "view_id": "Vreport", "type": "html", "at": "1"},
            {"key": "notes", "view_id": "Vnotes", "type": "html", "at": "2"},
        ],
    )
    removed: list[str] = []

    class Client:
        async def list_views(self, channel_id: str) -> list[dict[str, Any]]:
            return [{"view_id": "Vreport"}, {"view_id": "Vnotes"}]

        async def remove_view(self, channel_id: str, view_id: str = "", **_k: Any) -> None:
            removed.append(view_id)

    assert await views.make_room(Client(), CH, for_key="report") == "notes"
    assert removed == ["Vnotes"]


# ── Restart drain: an acknowledged click is gateway work in flight ──
# (GPT on d8eb95ddb3: action tasks escaped the update restart's drain.)


async def test_a_dispatched_click_is_counted_by_the_restart_drain(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from kiro_crew.slack.agent_sessions import session_events

    _bind(env)
    release = asyncio.Event()

    async def handle_stopped(_orch: Any, _event: dict[str, Any]) -> str:
        await release.wait()  # the click is still running when the update checks
        return "channel"

    monkeypatch.setattr(session_events, "handle_stopped", handle_stopped)
    handler_tasks: set[asyncio.Task[Any]] = set()
    monkeypatch.setattr(env.orch, "_handler_tasks", handler_tasks, raising=False)
    payload = {"team_id": "T1", "event_id": "EvDrain"}
    event = {"type": actions.EVENT, "channel": CH, "user": OWNER, "action_key": "stop"}
    actions.dispatch(env.orch, payload, event)
    assert len(handler_tasks) == 1 and not next(iter(handler_tasks)).done()
    release.set()
    await asyncio.wait_for(actions.drain(), timeout=10)
    assert handler_tasks == set()  # removed once it finished


# ── The per-channel ceiling at each inbound gate this PR adds ──
# (allowed_channel_ids, paired with the channels ceiling as every Slack gate is.)


async def test_a_click_in_a_channel_the_allowlist_leaves_out_does_nothing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    from kiro_crew.slack.agent_sessions import session_events

    _bind(env)
    stops: list[Any] = []

    async def handle_stopped(_orch: Any, event: dict[str, Any]) -> str:
        stops.append(event)
        return "channel"

    monkeypatch.setattr(session_events, "handle_stopped", handle_stopped)
    rows = allow_only(monkeypatch, "C0OTHERCHAN")
    payload = {"team_id": "T1", "event_id": "EvPolicy"}
    event = {"type": actions.EVENT, "channel": CH, "user": OWNER, "action_key": "stop"}
    assert await actions.handle(env.orch, payload, event) == "denied"
    assert stops == [] and env.slack.ephemerals == []
    assert policy_refusals(rows, actions.OPERATION)  # audited at the policy seam


async def test_comments_of_a_channel_the_allowlist_leaves_out_are_not_read(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from slack_code_channel_policy_helpers import allow_only

    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    allow_only(monkeypatch, "C0OTHERCHAN")
    assert await comments.read_comments(binding) == 0
    assert _calls(env, "agents.conversations.getCanvas") == []
    assert env.routed == [] and _seen() == ()


async def test_the_act_gate_names_a_channel_the_allowlist_leaves_out(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from slack_code_channel_policy_helpers import allow_only

    binding = _bind(env)
    allow_only(monkeypatch, "C0OTHERCHAN")
    assert await surface.act_refusal(CH, binding, team="T1") == "channel_policy"
    allow_only(monkeypatch, CH)
    assert await surface.act_refusal(CH, binding, team="T1") == ""


# ── Views: a removal whose answer was lost never takes a second tab ──
# (GPT on d37958c2c7: a timed-out removeView that landed, then the next tab went too.)


@pytest.mark.parametrize("landed", [True, False])
async def test_a_lost_removal_answer_never_removes_a_second_tab(
    env: Any, landed: bool  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import views
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    _bind(env)
    surface_store().update(
        CH,
        views=[
            {"key": "old", "view_id": "Vold", "type": "html", "at": "1"},
            {"key": "older", "view_id": "Volder", "type": "html", "at": "2"},
        ],
    )
    live = {"Vold", "Volder"}
    removed: list[str] = []

    class Client:
        async def list_views(self, channel_id: str) -> list[dict[str, Any]]:
            return [{"view_id": v} for v in sorted(live)]

        async def remove_view(self, channel_id: str, view_id: str = "", **_k: Any) -> None:
            removed.append(view_id)
            if view_id == "Vold":
                if landed:
                    live.discard(view_id)  # Slack removed it; the answer was lost
                raise CodeChannelApiError(
                    "agents.conversations.removeView", "transport_error", maybe_applied=True
                )
            live.discard(view_id)

    out = await views.make_room(Client(), CH, for_key="new")
    assert removed == ["Vold"]  # never a second removal on a guess
    assert out == ("old" if landed else "")
    assert live == ({"Volder"} if landed else {"Vold", "Volder"})
    keys = [v["key"] for v in surface_store().get(CH).views]
    assert keys == (["older"] if landed else ["old", "older"])

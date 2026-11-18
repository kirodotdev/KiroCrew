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
    _cap_responses,
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
from kiro_crew.testing.wait import default_timeout

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
        await until_parked(lock)
        admitted["now"] = False  # the operator sets the channel Off meanwhile
    out = await asyncio.wait_for(running, timeout=default_timeout())
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
    await asyncio.wait_for(asyncio.gather(*list(status._approval_tasks)), timeout=default_timeout())
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


async def test_the_turn_end_after_a_stop_still_moves_the_status_off_processing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """Opus on a9c3ed8d42: a Stop of a RUNNING turn settles nothing itself (only
    the idle branch does), so the stopped turn's own end must still set the status."""
    binding = _bind(env)
    writes: list[str] = []

    async def set_status(channel_id: str, value: str) -> bool:
        writes.append(value)
        return True

    monkeypatch.setattr(status, "set_status", set_status)
    await surface.note_turn_start(binding, None)
    surface.mark_stopped(CH)  # routing._stop_bound's first step
    assert surface.channel_stopped(CH)
    await status._on_end(binding, SimpleNamespace(outcome="cancelled"))
    assert writes == ["active"]


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
    """The comment ids recorded as seen (each stored entry is ``<ts> <id>``)."""
    record = surface_store().get(CH)
    return tuple(e.rpartition(" ")[2] for e in record.canvas_seen) if record is not None else ()


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

    binding = _bind(env)
    stops: list[Any] = []  # every Create PR the click would run

    async def run_command(_orch: Any, **kw: Any) -> None:
        stops.append(kw)

    monkeypatch.setattr(commands, "run_command", run_command)
    real_store = actions.store

    class Store:
        def get_binding(self, channel: str) -> Any:
            env.inbound_ok = False  # the channels policy denies inbound Slack meanwhile
            return real_store().get_binding(channel)

    monkeypatch.setattr(actions, "store", lambda: Store())
    payload = {"team_id": "T1", "event_id": "Ev1"}
    event = {
        "type": actions.EVENT,
        "channel": CH,
        "user": OWNER,
        "action_key": actions.ACTION_CREATE_PR,
    }
    assert await actions.handle(env.orch, payload, event) == "denied"
    assert stops == [] and binding.channel_id == CH


async def test_a_click_on_an_archived_channel_does_nothing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:  # noqa: F811

    _archive(env, _bind(env))
    stops: list[Any] = []  # every Create PR the click would run

    async def run_command(_orch: Any, **kw: Any) -> None:
        stops.append(kw)

    monkeypatch.setattr(commands, "run_command", run_command)
    payload = {"team_id": "T1", "event_id": "Ev2"}
    event = {
        "type": actions.EVENT,
        "channel": CH,
        "user": OWNER,
        "action_key": actions.ACTION_CREATE_PR,
    }
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
    await asyncio.wait_for(actions.drain(), timeout=default_timeout())
    assert handler_tasks == set()  # removed once it finished


# ── The per-channel ceiling at each inbound gate this PR adds ──
# (allowed_channel_ids, paired with the channels ceiling as every Slack gate is.)


async def test_a_click_in_a_channel_the_allowlist_leaves_out_does_nothing(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    _bind(env)
    stops: list[Any] = []  # every Create PR the click would run

    async def run_command(_orch: Any, **kw: Any) -> None:
        stops.append(kw)

    monkeypatch.setattr(commands, "run_command", run_command)
    rows = allow_only(monkeypatch, "C0OTHERCHAN")
    payload = {"team_id": "T1", "event_id": "EvPolicy"}
    event = {
        "type": actions.EVENT,
        "channel": CH,
        "user": OWNER,
        "action_key": actions.ACTION_CREATE_PR,
    }
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


# ── The client's commit point: each site's own fence is asked there too ──
# GPT on eb8e4009d7: ``SurfaceClient._call`` awaits the outbound policy before
# every send, so an Off, Review or scope change during THAT await must still
# hold the send back (``surface.send_fence``).


def _off_at_the_clients_read(
    monkeypatch: Any, method: str, *, nth: int = 1, flip: Callable[[], None] | None = None
) -> dict[str, Any]:
    """Set the channel Off (or run *flip*) inside ``SurfaceClient._call``'s own
    outbound-policy read for the *nth* *method*: after every gate the site asks
    itself, right before the client's commit point."""
    import sys

    from kiro_crew.slack.code_channels import routing

    on: dict[str, Any] = {"ok": True, "seen": 0}
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda *_a: on["ok"])

    async def permitted() -> bool:
        caller = sys._getframe(1)
        if caller.f_code.co_name == "_call" and caller.f_locals.get("method") == method:
            on["seen"] += 1
            if on["seen"] == nth:
                if flip is None:
                    on["ok"] = False
                else:
                    flip()
        return True

    monkeypatch.setattr(surface, "outbound_permitted", permitted)
    return on


@pytest.mark.parametrize(
    "method, nth, sent",
    [
        ("agents.conversations.setView", 1, {"setView": 0, "removeView": 0}),
        ("agents.conversations.removeView", 1, {"setView": 1, "removeView": 0}),
        ("agents.conversations.setView", 2, {"setView": 1, "removeView": 1}),
    ],
    ids=["the-write", "the-removal", "the-resend"],
)
async def test_an_off_during_the_clients_own_read_sends_no_view_write(
    env: Any, monkeypatch: Any, method: str, nth: int, sent: dict[str, int]  # noqa: F811
) -> None:
    _bind(env)
    surface_store().update(CH, views=[{"key": "old", "view_id": "Vold", "type": "html", "at": "1"}])
    _cap_responses(
        env,
        [{"view_id": "Vold", "view_key": "old"}]
        + [{"view_id": f"V{n}", "view_key": f"theirs-{n}"} for n in range(4)],
    )
    on = _off_at_the_clients_read(monkeypatch, method, nth=nth)
    out = await tools.run_tool(
        env.state,
        SESSION,
        tools.SET_VIEW,
        {"type": "html", "view_key": "report", "content": "<html></html>"},
    )
    assert on["seen"] == nth and not out.ok and out.code == "channel_off"
    assert {
        "setView": len(_calls(env, "agents.conversations.setView")),
        "removeView": len(_calls(env, "agents.conversations.removeView")),
    } == sent


async def test_make_room_carries_its_own_gate_to_the_removals_commit_point(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """Without a tool around it (the Code tab's room): views' own fence holds."""
    from kiro_crew.slack.code_channels import views

    binding = _bind(env)
    surface_store().update(CH, views=[{"key": "old", "view_id": "Vold", "type": "html", "at": "1"}])
    _cap_responses(env, [{"view_id": "Vold", "view_key": "old"}])
    on = _off_at_the_clients_read(monkeypatch, "agents.conversations.removeView")
    gate = lambda: surface.write_refusal_now(CH, binding, activation=True)  # noqa: E731
    assert await views.make_room(surface.client(), CH, for_key="diff", admits=gate) == ""
    assert on["seen"] == 1 and _calls(env, "agents.conversations.removeView") == []


async def test_an_off_during_the_clients_own_read_sends_no_tool_write(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """Every send a tool makes: run_tool's own gates ride to the commit point."""
    _bind(env)
    on = _off_at_the_clients_read(monkeypatch, "agents.sessions.rename")
    out = await tools.run_tool(env.state, SESSION, tools.RENAME, {"title": "after"})
    assert on["seen"] == 1 and not out.ok
    assert _calls(env, "agents.sessions.rename") == []


async def test_an_off_during_the_clients_own_read_sends_no_workspace_write(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """``WorkspaceService._call`` (the bar, the Code tab, the resource)."""
    from kiro_crew.slack.code_channels.workspace.service import WorkspaceService
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    binding = _bind(env)
    on = _off_at_the_clients_read(monkeypatch, "agents.conversations.setProperties")
    svc = WorkspaceService(surface.client, store=None)
    record = WorkspaceRecord(
        team_id="T1",
        channel_id=CH,
        pr_number=42,
        pr_url="https://github.com/acme/widgets/pull/42",
        pr_state="open",
        pr_title="Fix login",
    )
    await svc._publish_resource(record, binding)
    assert on["seen"] == 1 and record.resource_hash == ""
    assert _calls(env, "agents.conversations.setProperties") == []


async def test_a_scope_change_during_the_clients_own_read_sends_no_live_summary(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    binding = _bind(env)
    scope = {"ok": True}
    on = _off_at_the_clients_read(
        monkeypatch,
        "agents.conversations.setProperties",
        flip=lambda: scope.__setitem__("ok", False),
    )
    await publisher._record_summary(binding, "300.000001", lambda: scope["ok"])
    assert on["seen"] == 1
    assert _calls(env, "agents.conversations.setProperties") == []


async def test_an_off_during_the_clients_own_read_sends_no_command_set(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    binding = _bind(env)

    async def capability(_b: Any) -> tuple[bool, str]:
        return False, "not_a_repo"

    monkeypatch.setattr(commands, "pr_capability", capability)
    on = _off_at_the_clients_read(monkeypatch, "agents.conversations.setCommands")
    await commands.sync_commands(binding)
    assert on["seen"] == 1
    assert _calls(env, "agents.conversations.setCommands") == []
    record = surface_store().get(CH)
    assert record is None or not record.commands_digest


async def test_an_off_during_the_clients_own_read_reads_no_canvas(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    on = _off_at_the_clients_read(monkeypatch, "agents.conversations.getCanvas")
    assert await comments.read_comments(binding) == 0
    assert on["seen"] == 1 and env.routed == []
    assert _calls(env, "agents.conversations.getCanvas") == []


# ── After a Stop, on an idle channel (Opus on eb8e4009d7) ─────────────────
# The stopped turn is over once its slot is idle: what runs then is a pass of
# its own, held back only by a Stop that lands during it.


async def test_the_poll_delivers_a_comment_left_after_a_stop_once_the_slot_is_idle(
    env: Any,  # noqa: F811
) -> None:
    from slack_code_channels_helpers import stop_running_turn

    _bind(env, running=False)  # the stopped turn has ended
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    stop_running_turn(CH)
    assert await comments.poll_once() == 1
    assert len(env.routed) == 1
    assert surface.channel_stopped(CH)  # a turn's own reader still sees the Stop


async def test_a_turn_the_poll_starts_is_fenced_by_its_own_start_only(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """Opus on 82274a2654: the turn's task copies the route's context, so the
    poll's pass and admission must not reach it (its Stop would fence its own
    turn-end bar send through the poll's mark)."""
    import contextvars

    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute

    _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    seen: list[contextvars.Context] = []

    async def route(*_a: Any, **_k: Any) -> Any:
        seen.append(contextvars.copy_context())  # what a turn task it starts copies
        return LinkedRoute.ROUTED

    monkeypatch.setattr(routing, "_route_to_slot", route)
    assert await comments.poll_once() == 1
    surface.mark_stopped(CH)  # a Stop of the turn that comment started
    assert len(seen) == 1
    assert not seen[0].run(surface.admitted_work_stopped, CH)
    assert not seen[0].run(surface._OWN_PASS.get)


async def test_a_stop_during_the_polls_own_read_still_delivers_nothing(
    env: Any,  # noqa: F811
) -> None:
    from slack_code_channels_helpers import stop_running_turn

    _bind(env, running=False)
    _plan(env)

    def stop_then_answer(_body: dict[str, Any]) -> dict[str, Any]:
        stop_running_turn(CH)  # a Stop lands while the poll reads the canvas
        return _canvas(_thread("A1", OWNER, "hi"))

    env.api.responses["agents.conversations.getCanvas"] = stop_then_answer
    assert await comments.poll_once() == 0
    assert env.routed == []
    record = surface_store().get(CH)
    assert record is not None and record.canvas_seen == ()  # read again next time


async def test_create_pr_after_a_stop_corrects_the_command_set_once_the_slot_is_idle(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from slack_code_channels_helpers import stop_running_turn

    _bind(env, running=False)

    async def capability(_b: Any) -> tuple[bool, str]:
        return False, "not_a_repo"

    async def respond(_text: str) -> None:
        return None

    monkeypatch.setattr(commands, "pr_capability", capability)
    stop_running_turn(CH)
    await commands.run_command(
        env.orch,
        command=commands.CREATE_PR,
        channel=CH,
        user=OWNER,
        team="T1",
        text="",
        respond=respond,
    )
    await commands.drain()
    assert len(_calls(env, "agents.conversations.setCommands")) == 1


# ── A refusal a poll would repeat (Opus on eb8e4009d7) ────────────────────


async def test_a_refused_comment_is_answered_once_not_on_every_read(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """``route_linked_thread`` answered it in the channel (REFUSED): never re-read."""
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute

    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    answered: list[str] = []

    async def refuse(_orch: Any, _binding: Any, _text: str, user: str, **_k: Any) -> Any:
        answered.append(user)  # the refusal it posts into the channel
        return LinkedRoute.REFUSED

    monkeypatch.setattr(routing, "_route_to_slot", refuse)
    assert await comments.read_comments(binding) == 0
    assert await comments.read_comments(binding) == 0
    assert answered == [OWNER]
    record = surface_store().get(CH)
    assert record is not None and _seen() == ("A1",)


# ── The route's own commit point (GPT on a3b518cb0e) ──────────────────────
# ``route_linked_thread`` awaits after the caller's gate (the agent read, the
# window load, the image copy): the caller's gate is asked again there, last,
# right before the row is appended or the turn starts.


@pytest.mark.parametrize("admits", [True, False])
async def test_the_linked_route_asks_the_callers_gate_last(admits: bool) -> None:
    from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

    from kiro_crew.slack import handler
    from kiro_crew.slack.handler import LinkedRoute

    slot = MagicMock()
    type(slot).running = PropertyMock(return_value=False)
    type(slot).window_pending = PropertyMock(return_value=False)
    slot.key = "slot1"
    slot._queue = []
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._slots = {"slot1": slot}
    ds._background_tasks = set()
    asked: list[bool] = []

    async def admit() -> bool:
        asked.append(slot.append.called)  # nothing appended before the gate
        return admits

    slack = MagicMock()
    slack.post_message = AsyncMock()
    noted: list[str] = []
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch(
            "kiro_crew.dashboard.session_control_parts.code_channel_exemption.note_channel_sender",
            lambda _slot, user: noted.append(user),
        ),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        routed = await handler.route_linked_thread(
            "hello", "slack:t1", OWNER, CH, slack, "t1", admit=admit
        )
    assert asked == [False]
    assert routed is (LinkedRoute.ROUTED if admits else LinkedRoute.HELD)
    assert slot.append.called is admits and run_chat.called is admits
    assert noted == ([OWNER] if admits else [])  # a held message carried no author
    if not admits:
        slack.post_message.assert_not_called()  # held: nothing said either


def _off_during_the_route(monkeypatch: Any, flip: Callable[[], None]) -> list[Any]:
    """A route that lands *flip* during its own awaits, then asks the caller's
    gate at its commit point as ``route_linked_thread`` does."""
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute

    answers: list[Any] = []

    async def route(_orch: Any, _binding: Any, _text: str, _user: str, **kw: Any) -> Any:
        flip()
        admit = kw.get("admit")
        routed = LinkedRoute.ROUTED if admit is None or await admit() else LinkedRoute.HELD
        answers.append(routed)
        return routed

    monkeypatch.setattr(routing, "_route_to_slot", route)
    return answers


async def test_a_comment_held_at_the_routes_commit_point_stays_unseen(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute

    binding = _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    on = {"ok": True}
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda *_a: on["ok"])
    answers = _off_during_the_route(monkeypatch, lambda: on.__setitem__("ok", False))
    assert await comments.read_comments(binding) == 0
    assert answers == [LinkedRoute.HELD]
    record = surface_store().get(CH)
    assert record is not None and record.canvas_seen == ()  # read again once On


async def test_a_stop_during_the_polls_route_holds_the_comment(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    """The gate at the route's commit point keeps the poll's Stop fence."""
    from slack_code_channels_helpers import stop_running_turn

    from kiro_crew.slack.handler import LinkedRoute

    _bind(env, running=False)
    _plan(env)
    env.api.responses["agents.conversations.getCanvas"] = _canvas(_thread("A1", OWNER, "hi"))
    answers = _off_during_the_route(monkeypatch, lambda: stop_running_turn(CH))
    assert await comments.poll_once() == 0
    assert answers == [LinkedRoute.HELD]


@pytest.mark.parametrize("command", ["summarize", "wrap-up"])
async def test_a_command_held_at_the_routes_commit_point_says_why_and_disarms(
    env: Any, monkeypatch: Any, command: str  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import routing, wrapup
    from kiro_crew.slack.handler import LinkedRoute

    _bind(env, running=False)
    on = {"ok": True}
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda *_a: on["ok"])
    answers = _off_during_the_route(monkeypatch, lambda: on.__setitem__("ok", False))
    said: list[str] = []

    async def respond(text: str) -> None:
        said.append(text)

    await commands.run_command(
        env.orch, command=command, channel=CH, user=OWNER, team="T1", text="", respond=respond
    )
    assert answers == [LinkedRoute.HELD]
    assert said[-1] == commands.NOT_AUTHORIZED_TEXT
    assert CH not in wrapup._requests


async def test_a_create_pr_click_held_at_the_routes_commit_point_says_why(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from test_slack_code_channel_api_gaps import ACTION_CREATE_PR, _click

    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute

    _bind(env, running=False)

    async def capability(_b: Any) -> tuple[bool, str]:
        return True, ""

    async def target(_b: Any) -> tuple[str, str]:
        return "", ""

    monkeypatch.setattr(commands, "pr_capability", capability)
    monkeypatch.setattr(commands, "_pr_target", target)
    on = {"ok": True}
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda *_a: on["ok"])
    answers = _off_during_the_route(monkeypatch, lambda: on.__setitem__("ok", False))
    assert await actions.handle(env.orch, *_click(ACTION_CREATE_PR)) == "create-pr"
    assert answers == [LinkedRoute.HELD]
    assert (CH, OWNER, commands.NOT_AUTHORIZED_TEXT) in env.slack.ephemerals


def _real_route_slot(key: str = "slot1") -> Any:
    from unittest.mock import MagicMock, PropertyMock

    slot = MagicMock()
    type(slot).running = PropertyMock(return_value=False)
    slot.window_pending = False
    slot.key = key
    slot._queue = []
    return slot


async def test_the_real_route_holds_what_its_gate_refuses_after_the_agent_read() -> None:
    """The Off lands inside route_linked_thread's own await (the agent-mismatch
    read): the gate at its commit point holds the message, silently."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.slack import handler
    from kiro_crew.slack.handler import LinkedRoute

    slot = _real_route_slot()
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._slots = {"slot1": slot}
    ds._background_tasks = set()
    off = {"now": False}

    async def mismatch(_channel: str, _slot: Any, _key: str) -> None:
        off["now"] = True  # the operator sets the channel Off during this read

    async def admit() -> bool:
        return not off["now"]

    slack = MagicMock()
    slack.post_message = AsyncMock()
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch.object(handler, "linked_slot_channel_mismatch", mismatch),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        routed = await handler.route_linked_thread(
            "hello", "slack:t1", OWNER, CH, slack, "t1", admit=admit
        )
    assert off["now"] and routed is LinkedRoute.HELD
    slot.append.assert_not_called()
    run_chat.assert_not_called()
    slack.post_message.assert_not_called()


@pytest.mark.parametrize("reentry", ["window-load", "image-copy"])
async def test_a_pinned_reentry_of_the_route_asks_the_gate_exactly_once(reentry: str) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.slack import handler
    from kiro_crew.slack.handler import LinkedRoute

    first, second = _real_route_slot("slot1"), _real_route_slot("slot2")
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=first)
    ds._slots = {"slot1": first, "slot2": second}
    ds._background_tasks = set()
    copies = {"n": 0}

    def relink() -> None:
        ds.get_linked_slot.return_value = second  # relinked during the route's await

    async def load(_slot: Any) -> None:
        relink()
        _slot.window_pending = False

    async def store(_state: Any, _slot: Any, text: str, **_k: Any) -> str | None:
        copies["n"] += 1
        if reentry == "image-copy" and copies["n"] == 1:
            relink()
            return None  # the slot changed during the copy
        return text

    if reentry == "window-load":
        first.window_pending = True
    asked: list[int] = []

    async def admit() -> bool:
        asked.append(1)
        return True

    slack = MagicMock()
    slack.post_message = AsyncMock()
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch.object(handler, "load_window_off_loop", load),
        patch.object(handler, "store_linked_thread_images", store),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
    ):
        routed = await handler.route_linked_thread(
            "hello", "slack:t1", OWNER, CH, slack, "t1", admit=admit
        )
    assert routed is LinkedRoute.ROUTED and asked == [1]
    first.append.assert_not_called()
    second.append.assert_called_once()


# ── Delivered once, never again, whatever a listing shows (GPT on e86c4173bb) ──
# getCanvas lists only the newest 100 threads and 100 replies of each, and no
# resolved thread: what it leaves out may come back.


def _thread_at(cid: str, ts: str, user: str, text: str, *replies: tuple[str, str, str]) -> Any:
    thread = _thread(cid, user, text, *replies)
    thread["ts"] = ts
    return thread


def _listing(env: Any, *threads: Any) -> None:  # noqa: F811
    env.api.responses["agents.conversations.getCanvas"] = _canvas(*threads)


async def test_a_delivered_thread_that_leaves_the_listing_and_returns_is_not_redelivered(
    env: Any,  # noqa: F811
) -> None:
    binding = _bind(env, running=False)
    _plan(env)
    old = _thread_at("A1", "100.000001", OWNER, "first")
    _listing(env, old)
    assert await comments.read_comments(binding) == 1
    # 100 newer threads push it out of the window (or it is resolved) ...
    newer = [_thread_at(f"B{n}", f"200.{n:06d}", "UBOT", "") for n in range(100)]
    _listing(env, *newer)
    assert await comments.read_comments(binding) == 0
    # ... and it comes back when one of them resolves (or it is reopened).
    _listing(env, old, *newer[1:])
    assert await comments.read_comments(binding) == 0
    assert len(env.routed) == 1


async def test_a_delivered_reply_that_leaves_the_listing_and_returns_is_not_redelivered(
    env: Any,  # noqa: F811
) -> None:
    binding = _bind(env, running=False)
    _plan(env)
    reply = ("100.000002", OWNER, "a reply")
    _listing(env, _thread_at("A1", "100.000001", "UBOT", "", reply))
    assert await comments.read_comments(binding) == 1
    later = [(f"300.{n:06d}", "UBOT", "") for n in range(100)]
    _listing(env, _thread_at("A1", "100.000001", "UBOT", "", *later))  # the newest 100
    assert await comments.read_comments(binding) == 0
    _listing(env, _thread_at("A1", "100.000001", "UBOT", "", reply, *later[1:]))
    assert await comments.read_comments(binding) == 0
    assert len(env.routed) == 1


async def test_a_delivered_comment_stays_delivered_across_a_restart(
    env: Any, tmp_path: Path  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels.surface_store import SurfaceStore
    from kiro_crew.slack.code_channels.surface_store import set_store as set_surface_store

    binding = _bind(env, running=False)
    _plan(env)
    old = _thread_at("A1", "100.000001", OWNER, "first")
    _listing(env, old)
    assert await comments.read_comments(binding) == 1
    _listing(env, *[_thread_at(f"B{n}", f"200.{n:06d}", "UBOT", "") for n in range(100)])
    assert await comments.read_comments(binding) == 0
    set_surface_store(SurfaceStore(tmp_path / "surface.json"))  # a restart: read from disk
    comments._last_read.clear()
    _listing(env, old)
    assert await comments.read_comments(binding) == 0
    assert len(env.routed) == 1


async def test_at_the_bound_what_is_dropped_never_reads_as_new(
    env: Any, monkeypatch: Any  # noqa: F811
) -> None:
    binding = _bind(env, running=False)
    _plan(env)
    monkeypatch.setattr(comments, "MAX_SEEN", 3)
    first = [_thread_at(f"A{n}", f"100.{n:06d}", OWNER, f"c{n}") for n in range(3)]
    _listing(env, *first)
    assert await comments.read_comments(binding) == 3
    newer = _thread_at("A9", "200.000001", OWNER, "newer")
    _listing(env, newer)
    assert await comments.read_comments(binding) == 1  # the oldest is dropped
    record = surface_store().get(CH)
    assert record is not None and len(record.canvas_seen) == 3
    assert record.canvas_floor == "100.000000"  # every dropped one is at or before it
    _listing(env, *first, newer)  # all of them listed again
    assert await comments.read_comments(binding) == 0
    _listing(env, *first, newer, _thread_at("A10", "300.000001", OWNER, "new"))
    assert await comments.read_comments(binding) == 1  # a new one still arrives
    assert len(env.routed) == 3


async def test_a_baselined_canvas_keeps_what_its_first_listing_left_out_as_history(
    env: Any,  # noqa: F811
) -> None:
    binding = _bind(env, running=False)
    _plan(env, baseline=False)  # a canvas from before read-back
    older = _thread_at("A0", "100.000001", OWNER, "history, past the newest 100")
    window = [_thread_at(f"B{n}", f"200.{n:06d}", OWNER, "history") for n in range(100)]
    _listing(env, *window)
    assert await comments.read_comments(binding) == 0  # the baseline
    _listing(env, older, *window[1:])  # a thread from before the baseline comes back
    assert await comments.read_comments(binding) == 0
    _listing(env, *window, _thread_at("C1", "300.000001", OWNER, "after the baseline"))
    assert await comments.read_comments(binding) == 1
    assert len(env.routed) == 1


# ── Nothing is stored that the store cannot load back (GPT on 32af7ed2ca) ──


async def test_a_view_answered_without_an_id_is_not_recorded_and_the_store_still_loads(
    env: Any, tmp_path: Path  # noqa: F811
) -> None:
    from kiro_crew.slack.code_channels import views
    from kiro_crew.slack.code_channels.surface_store import SurfaceStore

    _bind(env)
    await views.record_view(CH, key="report", view_id="", type="html")
    assert surface_store().get(CH) is None or surface_store().get(CH).views == ()
    assert SurfaceStore(tmp_path / "surface.json").get("C0OTHER") is None  # loads after a restart


def test_the_store_refuses_a_write_it_could_not_load_back(tmp_path: Path) -> None:
    from kiro_crew.slack.code_channels.surface_store import SurfaceStore

    store = SurfaceStore(tmp_path / "surface.json")
    store.update("C1", plan_canvas_id="F1")
    with pytest.raises(ValueError):
        store.update("C1", views=[{"key": "report", "view_id": ""}])
    with pytest.raises(ValueError):
        store.update("C2", board_ts=None)
    reloaded = SurfaceStore(tmp_path / "surface.json")  # the next start
    record = reloaded.get("C1")
    assert record is not None and record.plan_canvas_id == "F1" and record.views == ()
    assert reloaded.get("C2") is None

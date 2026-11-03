"""Code-channel agent surface: channel-pinned tools, runtime commands, wrap-up + archive.

Recorded Slack payloads in, recorded calls out (``slack_code_channels_helpers.Env``),
real git against throwaway repositories for the archive cleanup. Each test names
the nearby input it varies: an unbound caller, a caller naming another channel, a
replaced session, a redelivered command, a non-git project, a refused Archive
click, a second click, uncommitted work, Slack refusing the feature, the toggle
off, the toggle on without the scope.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    BRANCH,
    CH,
    ORIGIN,
    OTHER_CH,
    OTHER_SESSION,
    OTHER_SLOT,
    SESSION,
    SLOT,
    _aturn,
    _bind,
    _calls,
    _channel_tree,
    _click,
    _flaky_archive_stamp,
    _git,
    _noop,
    _passthrough_spawn,
    _register_workspace,
    _repo_with_channel_tree,
    _Request,
    _retarget_service,
    _run,
    _slash,
    _tool_rows,
    _turn,
    _wrap,
    env_fixture,
    worktree_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW
from kiro_crew.mcp_tools import code_channels as mcp_code_channels
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import agent_bar
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import commands, status, surface, tools, wrapup
from kiro_crew.slack.code_channels.surface_store import SurfaceStore
from kiro_crew.slack.code_channels.surface_store import store as surface_store
from kiro_crew.slack.code_channels.workspace import service
from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore

# ── Pinning ───────────────────────────────────────────────────────────────


def test_a_session_not_bound_to_a_code_channel_is_refused_and_audited(env: Any) -> None:
    _bind(env, channel=OTHER_CH, slot=OTHER_SLOT, anchor="100.000009")
    out = _run(env, tools.SET_VIEW, {"view_key": "r", "content": "<p>x</p>"})
    assert not out.ok and out.code == "not_bound"
    assert "not bound to a Slack code channel" in out.text
    assert env.api.calls == []
    assert _tool_rows(env, "denied")


def test_an_unidentified_caller_is_refused(env: Any) -> None:
    _bind(env)
    out = _run(env, tools.RENAME, {"title": "x"}, session="cron:job-1")
    assert out.code == "not_bound" and env.api.calls == []


def test_a_tool_acts_only_on_the_callers_own_channel(env: Any) -> None:
    _bind(env)
    _bind(env, channel=OTHER_CH, slot=OTHER_SLOT, anchor="100.000009")
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    assert _run(env, tools.SET_VIEW, {"view_key": "r", "content": "<p>x</p>"}).ok
    assert _run(env, tools.RENAME, {"title": "Other"}, session=OTHER_SESSION).ok
    assert [b["channel_id"] for b in _calls(env, "agents.conversations.setView")] == [CH]
    assert [b["channel_id"] for b in _calls(env, "agents.sessions.rename")] == [OTHER_CH]


@pytest.mark.parametrize("arg", ["channel", "channel_id", "team_id"])
def test_an_argument_naming_a_channel_is_refused_before_slack(env: Any, arg: str) -> None:
    _bind(env)
    out = _run(env, tools.RENAME, {"title": "x", arg: OTHER_CH})
    assert out.code == "channel_argument" and env.api.calls == []
    assert any(arg in r["error"] for r in _tool_rows(env, "denied"))


def test_a_replaced_session_no_longer_owns_the_channel(env: Any) -> None:
    binding = _bind(env)
    # The channel's Slack link now resolves to a fresh slot (the old one closed).
    env.state.linked[binding.anchor_ts] = SimpleNamespace(key="chat-9-1", running=False)
    out = _run(env, tools.RENAME, {"title": "x"})
    assert out.code == "not_bound" and env.api.calls == []


def test_the_plan_tab_is_not_a_set_view_type(env: Any) -> None:
    _bind(env)
    surface_store().update(CH, plan_canvas_id="F0MINE")
    out = _run(env, tools.SET_VIEW, {"type": "canvas", "view_key": "x"})
    assert out.code == "invalid_arguments" and "publish_plan_canvas" in out.text
    out = _run(env, tools.SET_VIEW, {"view_key": "x", "content": "<p/>", "canvas_id": "F0THEIRS"})
    assert out.code == "invalid_arguments" and env.api.calls == []


def test_an_archived_or_stopped_channel_is_left_alone(env: Any, monkeypatch: Any) -> None:
    _bind(env)
    env.store.update_binding(CH, archived_at=1.0)
    assert _run(env, tools.RENAME, {"title": "x"}).code == "archived"
    env.store.update_binding(CH, archived_at=0.0)
    monkeypatch.setattr(surface, "channel_stopped", lambda ch: ch == CH)
    assert _run(env, tools.RENAME, {"title": "x"}).code == "stopped"
    assert env.api.calls == []


# ── Gate ──────────────────────────────────────────────────────────────────


def test_toggle_off_refuses_every_tool_and_lists_none(env: Any, monkeypatch: Any) -> None:
    _bind(env)
    env.on = False
    assert _run(env, tools.RENAME, {"title": "x"}).code == "feature_off"
    assert env.api.calls == []
    monkeypatch.setattr(mcp_code_channels, "_toggle_on", lambda: False)
    assert mcp_code_channels.advertised() == []
    monkeypatch.setattr(mcp_code_channels, "_toggle_on", lambda: True)
    assert {t["name"] for t in mcp_code_channels.advertised()} == set(tools.TOOL_NAMES)


def test_toggle_on_without_the_scope_behaves_as_off(env: Any) -> None:
    _bind(env)
    features.reset_capabilities()
    features.record_granted_scopes(["chat:write"])
    assert _run(env, tools.RENAME, {"title": "x"}).code == "feature_off"
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/summarize"), _noop)) is False
    payload = _click(OWNER, "300.000001")
    assert asyncio.run(wrapup.handle_archive_click(env.orch, payload)) is False
    assert env.api.calls == [] and env.routed == []


def test_feature_disabled_degrades_and_latches(env: Any) -> None:
    _bind(env)
    env.api.responses["agents.sessions.rename"] = {"ok": False, "error": "feature_disabled"}
    out = _run(env, tools.RENAME, {"title": "x"})
    assert not out.ok and "not available" in out.text
    assert not cc_binding.code_channels_on(env.cfg())
    assert _run(env, tools.RENAME, {"title": "y"}).code == "feature_off"


def test_missing_scope_answer_degrades(env: Any) -> None:
    _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": False, "error": "missing_scope"}
    out = _run(env, tools.SET_VIEW, {"view_key": "r", "content": "<p/>"})
    assert out.code == "missing_scope" and "not available" in out.text


# ── Views, canvas, bar, rename, status ────────────────────────────────────


def test_html_view_is_redacted_and_sends_no_csp(env: Any) -> None:
    _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    secret = "ghp_" + "A" * 36
    out = _run(
        env, tools.SET_VIEW, {"view_key": "r.html", "name": "Report", "content": f"<p>{secret}</p>"}
    )
    assert out.ok
    body = _calls(env, "agents.conversations.setView")[0]
    assert secret not in body["content"] and "csp" not in body and body["name"] == "Report"


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"type": "diff", "content": "x"}, "published from the worktree"),
        ({"type": "html", "view_key": "plan", "content": "x"}, "plan canvas"),
        (
            {"type": "block_kit", "view_key": "b", "blocks": [{"type": "actions", "elements": []}]},
            "display-only",
        ),
        (
            {
                "type": "block_kit",
                "view_key": "b",
                "blocks": [
                    {
                        "type": "context",
                        "elements": [
                            {"type": "image", "image_url": "https://x/y.png", "alt_text": "a"}
                        ],
                    }
                ],
            },
            "remote",
        ),
        (
            {
                "type": "block_kit",
                "view_key": "b",
                "blocks": [{"type": "card", "actions": [{"type": "button", "url": "https://x"}]}],
            },
            "display-only",
        ),
        ({"type": "html", "view_key": "bad key!", "content": "x"}, "view_key"),
        ({"type": "html", "view_key": "k", "content": "x", "csp": {}}, "does not take"),
    ],
)
def test_view_arguments_are_checked_before_slack(env: Any, args: dict, needle: str) -> None:
    _bind(env)
    out = _run(env, tools.SET_VIEW, args)
    assert out.code == "invalid_arguments" and needle in out.text
    assert env.api.calls == []


def test_block_kit_view_publishes(env: Any) -> None:
    _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V3"}
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Status*"}}]
    assert _run(
        env, tools.SET_VIEW, {"type": "block_kit", "view_key": "status", "blocks": blocks}
    ).ok
    assert _calls(env, "agents.conversations.setView")[0]["blocks"] == blocks


def test_plan_canvas_is_created_once_then_updated(env: Any) -> None:
    _bind(env)
    env.api.responses["conversations.canvases.create"] = {"ok": True, "canvas_id": "F1"}
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "VP"}
    assert "Created" in _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"}).text
    assert "Updated" in _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v2"}).text
    assert len(_calls(env, "conversations.canvases.create")) == 1
    assert _calls(env, "agents.conversations.setCanvasContent")[0]["canvas_id"] == "F1"
    view = _calls(env, "agents.conversations.setView")
    assert len(view) == 1 and view[0]["access_level"] == "comment"


def test_an_existing_canvas_is_never_retried_or_adopted(env: Any) -> None:
    _bind(env)
    env.api.responses["conversations.canvases.create"] = {
        "ok": False,
        "error": "channel_canvas_already_exists",
    }
    # Slack names the channel's canvas, but no create of ours ever ran: not adopted.
    env.api.responses["conversations.info"] = _canvas_info("F7")
    for _ in range(2):
        out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"})
        assert not out.ok and "did not create" in out.text
    # One create per call, never a retry inside a call, and nothing written.
    assert len(_calls(env, "conversations.canvases.create")) == 2
    assert _calls(env, "agents.conversations.setCanvasContent") == []
    assert surface_store().get(CH) is None or not surface_store().get(CH).plan_canvas_id


def _canvas_info(canvas_id: str) -> dict[str, Any]:
    props: dict[str, Any] = {"canvas": {"file_id": canvas_id}} if canvas_id else {}
    return {"ok": True, "channel": {"id": CH, "properties": props}}


@pytest.mark.parametrize("pending_survives", ["memory", "restart"])
def test_a_canvas_whose_create_answer_was_lost_is_adopted_next_time(
    env: Any, monkeypatch: Any, pending_survives: str
) -> None:
    """A create that may have landed (the answer was lost) is ours: the next call's
    "already exists" adopts the channel's canvas instead of refusing it for good."""
    _bind(env)
    env.api.responses["conversations.canvases.create"] = ConnectionResetError("socket reset")
    real_update = tools.surface_update

    async def stamp_fails(channel: str, **fields: Any) -> Any:
        if fields.get("canvas_create_pending_since"):
            raise OSError("disk full")  # only this process's memory remembers it
        return await real_update(channel, **fields)

    if pending_survives == "memory":
        tools.surface_update = stamp_fails  # type: ignore[assignment]
    try:
        out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"})
    finally:
        tools.surface_update = real_update  # type: ignore[assignment]
    assert not out.ok and "may have applied" in out.text
    if pending_survives == "restart":
        tools.forget_channel(CH)  # only the stored record carries it over
    env.api.responses["conversations.canvases.create"] = {
        "ok": False,
        "error": "channel_canvas_already_exists",
    }
    env.api.responses["conversations.info"] = _canvas_info("F9")
    env.api.responses["files.info"] = _our_canvas(monkeypatch)  # our bot made it, just now
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "VP"}
    out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v2"})
    assert out.ok, out.text
    assert _calls(env, "files.info")[0]["file"] == "F9"
    assert surface_store().get(CH).plan_canvas_id == "F9"
    assert not surface_store().get(CH).canvas_create_pending_since
    assert _calls(env, "agents.conversations.setCanvasContent")[0]["canvas_id"] == "F9"
    # Adopted once: the next call writes the stored canvas without a create.
    assert _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v3"}).ok
    assert len(_calls(env, "conversations.canvases.create")) == 2


def _our_canvas(monkeypatch: Any, *, user: str = "UBOT", bot: str = "", age: float = 0.0) -> Any:
    """files.info for the channel canvas: its creator and when it was made."""
    import time as time_mod

    from kiro_crew.slack import enterprise

    monkeypatch.setattr(enterprise, "validated_self_user_id", lambda: "UBOT")
    monkeypatch.setattr(enterprise, "validated_self_bot_id", lambda: "BBOT")
    created = int(time_mod.time() + age)

    def info(_params: dict) -> dict:
        file: dict[str, Any] = {"id": "F9", "user": user, "created": created}
        if bot:
            file["bot_id"] = bot
        return {"ok": True, "file": file}

    return info


@pytest.mark.parametrize(
    "creator",
    [
        {"user": "UHUMAN"},  # a person's canvas
        {"user": "UHUMAN", "bot": "BOTHER"},  # another app's canvas
        {"user": "UBOT", "age": -3600.0},  # ours, but made long before the lost create
    ],
)
def test_a_foreign_canvas_is_never_adopted_or_overwritten(
    env: Any, monkeypatch: Any, creator: dict
) -> None:
    """A lost create of ours only lets the next "already exists" adopt a canvas
    files.info says our bot made, after that create began: anything else is a
    canvas someone else made, left untouched and refused as usual."""
    _bind(env)
    env.api.responses["conversations.canvases.create"] = ConnectionResetError("socket reset")
    assert not _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"}).ok
    env.api.responses["conversations.canvases.create"] = {
        "ok": False,
        "error": "channel_canvas_already_exists",
    }
    env.api.responses["conversations.info"] = _canvas_info("F9")
    env.api.responses["files.info"] = _our_canvas(monkeypatch, **creator)
    out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v2"})
    assert not out.ok and "did not create" in out.text
    assert _calls(env, "agents.conversations.setCanvasContent") == []
    assert _calls(env, "agents.conversations.setView") == []
    assert not (surface_store().get(CH) and surface_store().get(CH).plan_canvas_id)


@pytest.mark.parametrize("case", ["bot_id_only", "two_lost_creates"])
def test_our_canvas_is_adopted_by_bot_id_and_after_two_lost_creates(
    env: Any, monkeypatch: Any, case: str
) -> None:
    """Our bot id alone identifies our canvas, and a second lost create keeps the
    first one's start, so a canvas the first one made still counts."""
    import time as time_mod

    _bind(env)
    env.api.responses["conversations.canvases.create"] = ConnectionResetError("socket reset")
    first = time_mod.time()
    user = "UOTHER" if case == "bot_id_only" else "UBOT"
    # Made now, by the first create: read before the clock is moved below.
    canvas = _our_canvas(monkeypatch, user=user, bot="BBOT")
    assert not _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"}).ok
    if case == "two_lost_creates":
        # The second lost create starts long after the first one's canvas was made.
        # Only tools' own clock binding moves: the stdlib time module is shared
        # by the whole worker (every store write and log timestamp reads it).
        from kiro_crew.testing.clock import ManualClock

        ManualClock(start=first + 3600).install(monkeypatch, tools)
        assert not _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"}).ok
        assert surface_store().get(CH).canvas_create_pending_since == pytest.approx(first, abs=5)
    env.api.responses["conversations.canvases.create"] = {
        "ok": False,
        "error": "channel_canvas_already_exists",
    }
    env.api.responses["conversations.info"] = _canvas_info("F9")
    env.api.responses["files.info"] = canvas
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "VP"}
    out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v2"})
    assert out.ok, out.text
    assert surface_store().get(CH).plan_canvas_id == "F9"


@pytest.mark.parametrize("unknown", ["missing_scope", "transport", "no_self_identity"])
def test_an_unverifiable_canvas_is_refused_and_code_channels_stay_on(
    env: Any, monkeypatch: Any, unknown: str
) -> None:
    """When who made the canvas can't be told (no files:read, a dropped read, our
    own identity unknown) it is refused, nothing written, and the install-wide
    latch is never tripped by that read."""
    from kiro_crew.slack import enterprise, features

    _bind(env)
    env.api.responses["conversations.canvases.create"] = ConnectionResetError("socket reset")
    assert not _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"}).ok
    env.api.responses["conversations.canvases.create"] = {
        "ok": False,
        "error": "channel_canvas_already_exists",
    }
    env.api.responses["conversations.info"] = _canvas_info("F9")
    env.api.responses["files.info"] = _our_canvas(monkeypatch)
    if unknown == "missing_scope":
        env.api.responses["files.info"] = {"ok": False, "error": "missing_scope"}
    elif unknown == "transport":
        env.api.responses["files.info"] = ConnectionResetError("socket reset")
    else:
        monkeypatch.setattr(enterprise, "validated_self_user_id", lambda: "")
        monkeypatch.setattr(enterprise, "validated_self_bot_id", lambda: "")
    out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v2"})
    assert not out.ok and "did not create" in out.text
    assert _calls(env, "agents.conversations.setCanvasContent") == []
    assert features.CODE_CHANNELS not in features.latched()


def test_a_lost_create_answer_adopts_nothing_when_slack_names_no_canvas(env: Any) -> None:
    _bind(env)
    env.api.responses["conversations.canvases.create"] = ConnectionResetError("socket reset")
    assert not _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"}).ok
    env.api.responses["conversations.canvases.create"] = {
        "ok": False,
        "error": "channel_canvas_already_exists",
    }
    env.api.responses["conversations.info"] = _canvas_info("")
    out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v2"})
    assert not out.ok and "did not create" in out.text
    assert _calls(env, "agents.conversations.setCanvasContent") == []


def test_context_bar_items_follow_the_workspace_items(env: Any) -> None:
    base = [{"key": k, "label": k} for k in ("repo", "branch", "pr", "ci")]
    extra = [
        {"key": "tokens", "label": "1.2k"},
        {"key": "docs", "label": "Docs"},
        {"key": "pr", "label": "x"},
    ]
    items, dropped = agent_bar.merge(base, extra)
    assert [i["key"] for i in items] == ["repo", "branch", "pr", "ci", "tokens"]
    assert dropped == ["docs", "pr"]


def test_set_context_bar_without_a_workspace_publishes_the_agents_items(env: Any) -> None:
    _bind(env)
    items = [
        {"key": "tokens", "label": "1.2k tokens", "icon": "terminal"},
        {"key": "docs", "label": "Docs", "url": "https://example.com/d"},
    ]
    assert _run(env, tools.SET_CONTEXT_BAR, {"items": items}).ok
    sent = _calls(env, "agents.conversations.setProperties")[0]
    assert sent["channel_id"] == CH
    assert [i["key"] for i in sent["code_channel"]["context_bar_items"]] == ["tokens", "docs"]


@pytest.mark.parametrize(
    "item, needle",
    [
        ({"key": "branch", "label": "x"}, "workspace's own"),
        ({"key": "go", "label": "Go", "item_type": "action"}, "action items are not supported"),
        ({"key": "u", "label": "u", "url": "http://plain"}, "https"),
        ({"key": "i", "label": "i", "icon": "rocket"}, "icon"),
    ],
)
def test_bad_context_bar_items_are_refused(env: Any, item: dict, needle: str) -> None:
    _bind(env)
    out = _run(env, tools.SET_CONTEXT_BAR, {"items": [item]})
    assert out.code == "invalid_arguments" and needle in out.text and env.api.calls == []


def test_rename_and_suspended_status_outlast_the_turn(env: Any) -> None:
    binding = _bind(env)
    assert _run(env, tools.RENAME, {"title": "Migrate cron"}).ok
    assert _calls(env, "agents.sessions.rename") == [{"channel_id": CH, "title": "Migrate cron"}]
    assert _run(env, tools.SET_STATUS, {"status": "suspended"}).ok
    asyncio.run(status._on_end(binding, SimpleNamespace(outcome="ok")))
    statuses = [b["status"] for b in _calls(env, "agents.sessions.setStatus")]
    assert statuses == ["suspended", "suspended"]
    assert all("thread_ts" not in b for b in _calls(env, "agents.sessions.setStatus"))


# ── Commands ──────────────────────────────────────────────────────────────


def test_summarize_becomes_a_prompt_for_the_bound_session(env: Any) -> None:
    _bind(env)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    assert asyncio.run(commands.handle_slash(env.orch, _slash("/summarize"), respond))
    assert (
        len(env.routed) == 1
        and env.routed[0][0] == CH
        and "Summarize this session" in env.routed[0][1]
    )
    assert env.sel.ops("slack.code_channel.command")[-1]["outcome"] == "allowed"


def test_a_redelivered_command_runs_once(env: Any) -> None:
    _bind(env)
    for _ in range(3):
        assert asyncio.run(
            commands.handle_slash(env.orch, _slash("/summarize", trigger="T-same"), _noop)
        )
    assert len(env.routed) == 1
    assert asyncio.run(
        commands.handle_slash(env.orch, _slash("/summarize", trigger="T-new"), _noop)
    )
    assert len(env.routed) == 2


def test_a_command_from_an_unauthorized_user_is_refused(env: Any) -> None:
    _bind(env)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    assert asyncio.run(
        commands.handle_slash(env.orch, _slash("/summarize", user="USTRANGER"), respond)
    )
    assert env.routed == [] and "not authorized" in replies[0]
    assert env.sel.ops("slack.code_channel.command")[-1]["outcome"] == "denied"


def test_a_command_is_refused_when_the_origin_turned_the_bot_off(env: Any) -> None:
    _bind(env)
    env.activation[ORIGIN] = ACTIVATION_OFF
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up"), _noop))
    assert env.routed == []


@pytest.mark.parametrize("activation", [ACTIVATION_OFF, ACTIVATION_REVIEW])
@pytest.mark.parametrize("command", ["/summarize", "/wrap-up", "/create-pr"])
def test_a_command_is_refused_when_the_code_channel_itself_is_off_or_in_review(
    env: Any, activation: str, command: str
) -> None:
    """The code channel's own activation applies on top of an active origin, as on
    the message path, so no turn runs and nothing is published unreviewed."""
    _bind(env)
    env.activation[CH] = activation
    assert asyncio.run(commands.handle_slash(env.orch, _slash(command), _noop))
    assert env.routed == []
    last = env.sel.ops("slack.code_channel.command")[-1]
    assert last["outcome"] == "denied"


def test_create_pr_on_a_non_git_project_explains_and_does_not_run(env: Any) -> None:
    _bind(env)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    assert asyncio.run(commands.handle_slash(env.orch, _slash("/create-pr"), respond))
    assert env.routed == [] and "not a git repository" in replies[0]


def test_create_pr_lets_an_orchestrating_agent_delegate_on_the_channels_branch(
    env: Any, monkeypatch: Any
) -> None:
    """A conductor-led channel keeps /create-pr: the prompt names the channel's own
    worktree and branch and lets the agent hand the PR to a worker there."""
    _bind(env)
    WorkspaceStore().put(
        WorkspaceRecord(
            team_id="T1", channel_id=CH, worktree="/wt/fix-login", branch="kc/fix-login"
        )
    )

    async def able(_b: Any) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(commands, "pr_capability", able)
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/create-pr", text="Fix it"), _noop))
    assert len(env.routed) == 1
    prompt = env.routed[0][1]
    assert "`kc/fix-login`" in prompt and "`/wt/fix-login`" in prompt
    assert "dispatch one worker" in prompt and "not a new worktree or branch" in prompt
    assert 'Title it "Fix it"' in prompt


def test_a_command_with_the_toggle_off_is_not_ours(env: Any) -> None:
    _bind(env)
    env.on = False
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/summarize"), _noop)) is False
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/kirocrew"), _noop)) is False


def test_command_set_offers_create_pr_only_when_possible(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    able = {"v": (False, "not_a_repo")}

    async def capability_now(_b: Any) -> tuple[bool, str]:
        return able["v"]

    monkeypatch.setattr(commands, "pr_capability", capability_now)
    asyncio.run(commands.sync_commands(binding))
    asyncio.run(commands.sync_commands(binding))  # unchanged: not sent again
    able["v"] = (True, "")
    asyncio.run(commands.sync_commands(binding))
    sent = [
        [c["name"] for c in b["commands"]] for b in _calls(env, "agents.conversations.setCommands")
    ]
    assert sent == [["summarize", "wrap-up"], ["summarize", "wrap-up", "create-pr"]]


def test_no_actions_url_is_not_stored_as_the_channels_own_refusal(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setCommands"] = {"ok": False, "error": "no_actions_url"}
    asyncio.run(commands.sync_commands(binding))
    # An install-wide answer: nothing is written on the channel, so fixing the
    # app's configuration is enough for the next sync to register the commands.
    assert not (surface_store().get(CH) or SimpleNamespace(commands_off="")).commands_off
    del env.api.responses["agents.conversations.setCommands"]
    asyncio.run(commands.sync_commands(binding))
    assert len(_calls(env, "agents.conversations.setCommands")) == 2
    assert surface_store().get(CH).commands_digest


def test_a_channel_specific_refusal_switches_commands_off_for_the_channel(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setCommands"] = {
        "ok": False,
        "error": "not_agent_channel",
    }
    asyncio.run(commands.sync_commands(binding))
    asyncio.run(commands.sync_commands(binding))
    assert len(_calls(env, "agents.conversations.setCommands")) == 1
    assert surface_store().get(CH).commands_off == "not_agent_channel"


def test_an_archived_refusal_does_not_switch_commands_off_for_good(env: Any) -> None:
    """A sync racing the archive event: an unarchive undoes ``is_archived``, so the
    next sync registers the commands instead of the channel staying without them."""
    binding = _bind(env)
    env.api.responses["agents.conversations.setCommands"] = {"ok": False, "error": "is_archived"}
    asyncio.run(commands.sync_commands(binding))
    assert not (surface_store().get(CH) or SimpleNamespace(commands_off="")).commands_off
    del env.api.responses["agents.conversations.setCommands"]
    asyncio.run(commands.sync_commands(binding))
    assert len(_calls(env, "agents.conversations.setCommands")) == 2
    assert surface_store().get(CH).commands_digest


def test_an_install_wide_refusal_is_not_stored_on_the_channel(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setCommands"] = {"ok": False, "error": "missing_scope"}
    asyncio.run(commands.sync_commands(binding))
    assert not (surface_store().get(CH) or SimpleNamespace(commands_off="")).commands_off
    # The scope is granted and Slack reconnects: the latch clears, commands register.
    del env.api.responses["agents.conversations.setCommands"]
    features.clear_latch(features.CODE_CHANNELS)
    asyncio.run(commands.sync_commands(binding))
    sent = _calls(env, "agents.conversations.setCommands")
    assert len(sent) == 2 and [c["name"] for c in sent[-1]["commands"]][:2] == [
        "summarize",
        "wrap-up",
    ]
    assert surface_store().get(CH).commands_digest


def test_an_install_wide_code_stored_by_an_older_build_is_ignored(env: Any) -> None:
    binding = _bind(env)
    asyncio.run(wrapup.update_async(CH, commands_off="missing_scope"))
    asyncio.run(commands.sync_commands(binding))
    assert len(_calls(env, "agents.conversations.setCommands")) == 1
    assert surface_store().get(CH).commands_digest


# ── Wrap-up and archive ──────────────────────────────────────────────────


def test_wrap_up_posts_the_summary_with_an_archive_button(env: Any) -> None:
    _bind(env)
    blocks, ts = _wrap(env)
    assert env.slack.blocks_posts[-1][0] == CH and env.slack.blocks_posts[-1][3] is None
    text = blocks[0]["text"]["text"]
    assert "Fixed the flaky login test" in text and "<!channel>" not in text
    button = blocks[-1]["elements"][0]
    assert button["action_id"] == wrapup.ARCHIVE_ACTION_ID and button["value"] == CH
    assert _calls(env, "agents.conversations.setProperties")[0]["code_channel"][
        "summary_message"
    ] == {"message_ts": ts}
    assert env.store.get_binding(CH).wrapped_up_at > 0


def test_wrap_up_without_the_tool_call_still_posts_the_facts(env: Any) -> None:
    binding = _bind(env)
    asyncio.run(wrapup.on_turn_start(binding, SimpleNamespace(prompt="earlier work")))
    tag = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert len(env.slack.blocks_posts) == 0  # the turn that ended began before /wrap-up
    _turn(binding, f"ran /wrap-up ({tag})")  # its own turn, without the tool call
    assert len(env.slack.blocks_posts) == 1
    assert wrapup.DEFAULT_SUMMARY in env.slack.blocks_posts[0][1][0]["text"]["text"]


def test_archive_click_archives_with_the_summary_once(env: Any) -> None:
    _bind(env)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert _calls(env, "agents.conversations.archive") == [
        {"channel_id": CH, "summary_message_ts": ts}
    ]
    assert env.store.get_binding(CH).archived_at > 0
    results = [r["resources"] for r in env.sel.ops("slack.code_channel.archive")]
    assert any("result=archived" in r for r in results) and any(
        "already_archived" in r for r in results
    )
    # The button was retired before the archive call, so a stale client cannot double-click.
    assert env.slack.updates and all(b.get("type") != "actions" for b in env.slack.updates[0][3])


@pytest.mark.parametrize(
    "case",
    [
        "unauthorized_user",
        "origin_off",
        "channel_off",
        "channel_review",
        "stale_message",
        "forged_value",
        "unbound_channel",
    ],
)
def test_archive_click_refusals_archive_nothing(env: Any, case: str) -> None:
    _bind(env)
    _, ts = _wrap(env)
    payload = _click(OWNER, ts)
    if case == "unauthorized_user":
        payload = _click("USTRANGER", ts)
    elif case == "origin_off":
        env.activation[ORIGIN] = ACTIVATION_OFF
    elif case == "channel_off":
        env.activation[CH] = ACTIVATION_OFF
    elif case == "channel_review":
        env.activation[CH] = ACTIVATION_REVIEW
    elif case == "stale_message":
        payload = _click(OWNER, "999.000001")
    elif case == "forged_value":
        payload = _click(OWNER, ts, value=OTHER_CH)
    elif case == "unbound_channel":
        payload = _click(OWNER, ts, channel=OTHER_CH, value=OTHER_CH)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, payload))
    assert _calls(env, "agents.conversations.archive") == []
    assert env.sel.ops("slack.code_channel.archive")[-1]["outcome"] == "denied"
    assert env.slack.ephemerals


def test_archive_failure_restores_the_button(env: Any) -> None:
    _bind(env)
    _, ts = _wrap(env)
    env.api.responses["agents.conversations.archive"] = {"ok": False, "error": "not_authorized"}
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not env.store.get_binding(CH).archived_at
    assert env.slack.updates[-1][3] == [{"type": "section"}]


def test_a_newer_wrap_up_makes_the_older_button_stale_without_editing_it(env: Any) -> None:
    _bind(env)
    _, first = _wrap(env)
    _, second = _wrap(env)
    assert first != second
    assert env.slack.updates == []  # the older summary keeps its text
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, first)))
    assert _calls(env, "agents.conversations.archive") == []


# ── Archive cleanup against a real worktree ──────────────────────────────


def test_archive_keeps_a_worktree_with_uncommitted_changes(env: Any, worktree: Any) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    (tree / "README.md").write_text("changed\n")
    blocks, ts = _wrap(env)
    assert "uncommitted" in blocks[1]["elements"][0]["text"]
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and (tree / "README.md").read_text() == "changed\n"
    assert any(
        "result=kept" in r["resources"] for r in env.sel.ops("slack.code_channel.worktree_release")
    )
    assert env.notified and str(tree) in env.notified[0][0][2]


def test_archive_removes_a_clean_worktree(env: Any, worktree: Any) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists()
    # Nothing was committed on it: the branch goes too.
    assert _git("branch", "--list", BRANCH, cwd=root) == ""
    assert any(
        "result=removed" in r["resources"]
        for r in env.sel.ops("slack.code_channel.worktree_release")
    )


def test_wrap_up_facts_name_the_branch(env: Any, worktree: Any) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    blocks, _ = _wrap(env)
    assert BRANCH in blocks[1]["elements"][0]["text"]


# ── Transport: the MCP forwarder and the gateway route ───────────────────


def test_the_mcp_tool_forwards_the_strict_key_and_no_channel(monkeypatch: Any) -> None:
    from kiro_crew import mcp_core

    sent: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda refusal: (SESSION, ""))

    def post(path: str, body: dict, *, session_key: str, timeout: float) -> dict:
        sent.append((path, body, session_key))
        return {"ok": True, "text": "Renamed."}

    monkeypatch.setattr(mcp_core, "_post", post)
    assert (
        mcp_code_channels.HANDLERS["code_channel_rename"]("code_channel_rename", {"title": "x"})
        == "Renamed."
    )
    assert sent == [
        (
            "/api/slack/code-channel/tool",
            {"tool": "code_channel_rename", "args": {"title": "x"}},
            SESSION,
        )
    ]


def test_the_mcp_tool_refuses_an_unidentified_caller(monkeypatch: Any) -> None:
    from kiro_crew import mcp_core

    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda refusal: ("", refusal + " (why)")
    )
    monkeypatch.setattr(
        mcp_core, "_post", lambda *a, **k: pytest.fail("must not reach the gateway")
    )
    out = mcp_code_channels.HANDLERS["code_channel_wrap_up"](
        "code_channel_wrap_up", {"summary": "s"}
    )
    assert out.startswith("Error:") and "could not be identified" in out


@pytest.mark.parametrize(
    "flags, headers, status",
    [
        ({}, {"X-Session-Key": SESSION}, 403),  # not a local caller
        ({"internal_auth": True}, {}, 400),  # no session named
        ({"internal_auth": True}, {"X-Session-Key": SESSION}, 403),  # named, not attested
    ],
)
def test_the_route_refuses_an_unverified_caller(
    monkeypatch: Any, flags: dict, headers: dict, status: int
) -> None:
    from kiro_crew.dashboard.handlers import slack_code_channel_tools as route

    monkeypatch.setattr(tools, "run_tool", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    request = _Request(headers=headers, body={"tool": tools.RENAME, "args": {}}, **flags)
    assert asyncio.run(route.api_code_channel_tool(request)).status == status  # type: ignore[arg-type]


def test_the_route_runs_the_tool_for_the_attested_session(monkeypatch: Any) -> None:
    import json

    from kiro_crew.dashboard.handlers import slack_code_channel_tools as route

    seen: list[tuple] = []

    async def run_tool(state: Any, session: str, tool: str, args: dict) -> tools.ToolOutcome:
        seen.append((state, session, tool, args))
        return tools.ToolOutcome(True, "done")

    monkeypatch.setattr(tools, "run_tool", run_tool)
    request = _Request(
        headers={"X-Session-Key": SESSION},
        body={"tool": tools.RENAME, "args": {"title": "x"}},
        internal_auth=True,
        peer_verified=True,
    )
    resp = asyncio.run(route.api_code_channel_tool(request))  # type: ignore[arg-type]
    assert resp.status == 200 and json.loads(resp.text) == {"ok": True, "text": "done", "code": ""}
    assert seen == [("STATE", SESSION, tools.RENAME, {"title": "x"})]


def test_set_context_bar_rides_the_workspace_publish(
    env: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.workspace import core as workspace_core

    _bind(env)
    sent: list[list[dict]] = []

    class Client:
        async def set_context_bar(self, channel_id: str, items: list) -> dict:
            assert channel_id == CH
            sent.append(items)
            return {"ok": True}

    svc = service.WorkspaceService(lambda: Client())
    monkeypatch.setattr(workspace_core, "_SERVICE", svc)
    WorkspaceStore().put(WorkspaceRecord(team_id="T1", channel_id=CH, project_dir=str(tmp_path)))
    items = [{"key": "tokens", "label": "1.2k tokens"}]
    out = _run(env, tools.SET_CONTEXT_BAR, {"items": items})
    assert out.ok, out.text
    assert [i["key"] for i in sent[-1]] == ["folder", "tokens"]
    assert _calls(env, "agents.conversations.setProperties") == []  # one publisher
    assert _run(env, tools.SET_CONTEXT_BAR, {"items": []}).ok
    assert [i["key"] for i in sent[-1]] == ["folder"]


def test_group_pings_are_neutralized(env: Any) -> None:
    _bind(env)
    assert _run(env, tools.WRAP_UP, {"summary": "Done <!subteam^S0123|@oncall> <!here>"}).ok
    text = env.slack.blocks_posts[-1][1][0]["text"]["text"]
    assert "<!subteam" not in text and "<!here>" not in text and "@\u200boncall" in text


def test_no_wrap_up_is_posted_into_an_archived_channel(env: Any) -> None:
    binding = _bind(env)
    _, ts = _wrap(env)
    asyncio.run(wrapup.request_wrap_up(CH, OWNER))  # a /wrap-up turn is running
    asyncio.run(wrapup.on_turn_start(binding, None))
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    posts = len(env.slack.blocks_posts)
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert asyncio.run(wrapup.post_wrap_up(binding, "late", caller=SESSION)) == ""
    assert len(env.slack.blocks_posts) == posts


def test_a_replaced_sessions_old_button_is_stale(env: Any) -> None:
    _bind(env)
    _, ts = _wrap(env)
    env.store.update_binding(CH, session_key="chat-9-1", anchor_ts="100.000077")
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert _calls(env, "agents.conversations.archive") == []


def test_marking_wrapped_up_never_rewrites_a_replaced_binding(env: Any) -> None:
    _bind(env)
    env.store.update_binding(CH, session_key="chat-9-1")
    wrapup._mark_wrapped(CH, 5.0)
    stored = env.store.get_binding(CH)
    assert stored.session_key == "chat-9-1" and stored.wrapped_up_at == 5.0


def test_an_unexpected_failure_is_audited_not_raised(env: Any, monkeypatch: Any) -> None:
    _bind(env)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(surface_store(), "update", boom)
    out = _run(env, tools.SET_CONTEXT_BAR, {"items": []})
    assert out.code == "internal" and _tool_rows(env, "error")


# ── Governance, redaction, persistence ───────────────────────────────────


def test_a_command_is_refused_when_the_channels_policy_denies_slack_inbound(env: Any) -> None:
    _bind(env)
    env.inbound_ok = False
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/create-pr"), _noop))
    assert env.routed == []
    row = env.sel.ops("slack.code_channel.command")[-1]
    assert row["outcome"] == "denied" and "governance" in row["error"]


def test_tools_wrap_up_and_archive_respect_the_outbound_ceiling(env: Any) -> None:
    _bind(env)
    _, ts = _wrap(env)
    env.outbound_ok = False
    assert _run(env, tools.RENAME, {"title": "x"}).code == "channels_policy"
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert _calls(env, "agents.sessions.rename") == []
    assert _calls(env, "agents.conversations.archive") == []


def test_exfiltration_urls_are_redacted_and_bad_links_refused(env: Any) -> None:
    from kiro_crew.security import redact

    _bind(env)
    leak = "https://attacker.example/c?d=" + "ghp_" + "B" * 36
    assert redact(leak) != leak
    out = _run(env, tools.SET_CONTEXT_BAR, {"items": [{"key": "x", "label": "x", "url": leak}]})
    assert out.code == "invalid_arguments" and env.api.calls == []
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    assert _run(env, tools.SET_VIEW, {"view_key": "r", "content": f'<a href="{leak}">x</a>'}).ok
    assert leak not in _calls(env, "agents.conversations.setView")[0]["content"]


def test_a_failed_store_write_changes_nothing_and_fails_the_tool(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    _bind(env)
    env.api.responses["conversations.canvases.create"] = {"ok": True, "canvas_id": "F1"}

    def refuse(*_a: Any, **_k: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(ss, "atomic_write", refuse)
    out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"})
    assert not out.ok and out.code == "internal"
    assert surface_store().get(CH) is None


def test_a_wrap_up_that_never_reached_the_session_is_disarmed(env: Any) -> None:
    binding = _bind(env)
    env.route_result = False
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up"), _noop))
    assert not surface_store().get(CH).wrapup_pending_since
    asyncio.run(wrapup.on_turn_start(binding, None))
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert env.slack.blocks_posts == []


def test_wrap_up_facts_and_identifiers_pass_the_redaction(env: Any, monkeypatch: Any) -> None:
    _bind(env)
    token = "ghp_" + "C" * 36

    async def facts(_b: Any) -> wrapup.Facts:
        return wrapup.Facts(branch=f"x<!channel>{token}", changes="1 file changed")

    monkeypatch.setattr(wrapup, "gather_facts", facts)
    blocks, _ = _wrap(env)
    text = blocks[1]["elements"][0]["text"]
    assert token not in text and "<!channel>" not in text
    out = _run(env, tools.SET_VIEW, {"view_key": token, "content": "<p/>"})
    assert out.code == "invalid_arguments" and _calls(env, "agents.conversations.setView") == []
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    blocks_in = [{"type": "section", "text": {"type": "mrkdwn", "text": "ok"}, token: "x"}]
    assert _run(env, tools.SET_VIEW, {"type": "block_kit", "view_key": "k", "blocks": blocks_in}).ok
    assert token not in str(_calls(env, "agents.conversations.setView")[0]["blocks"])


def test_the_wrap_up_start_hook_runs_before_every_other(monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import hooks

    monkeypatch.setattr(hooks, "_START_HOOKS", [status._on_start])
    monkeypatch.setattr(hooks, "_END_HOOKS", [])
    monkeypatch.setattr(surface, "_INSTALLED", False)
    surface.install(lambda: None)
    assert hooks._START_HOOKS[0] is wrapup.on_turn_start


@pytest.mark.parametrize(
    "forgery", ["base_at_tip", "base_ref_is_the_branch", "foreign_worktree", "foreign_branch"]
)
def test_a_forged_workspace_record_never_drives_the_cleanup(
    env: Any, worktree: Any, tmp_path: Path, forgery: str
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    (tree / "work.txt").write_text("unpushed\n")
    _git("add", "work.txt", cwd=tree)
    _git("commit", "-q", "-m", "unpushed work", cwd=tree)
    tip = _git("rev-parse", "HEAD", cwd=tree)
    if forgery == "base_at_tip":
        _register_workspace(root, tree, base=tip)  # would let the branch be deleted
    elif forgery == "base_ref_is_the_branch":
        _register_workspace(root, tree, base=tip)
        record = WorkspaceStore().get("T1", CH)
        record.base_ref = BRANCH  # merge-base(tip, tip) would vouch for itself
        WorkspaceStore().put(record)
    elif forgery == "foreign_worktree":
        other = tmp_path / "elsewhere"
        other.mkdir()
        _register_workspace(root, other)
    else:
        _register_workspace(root, tree)
        record = WorkspaceStore().get("T1", CH)
        record.branch = "main"
        WorkspaceStore().put(record)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    # The unpushed commit always stays reachable on its branch.
    assert _git("rev-parse", BRANCH, cwd=root) == tip
    rows = env.sel.ops("slack.code_channel.worktree_release")
    if forgery.startswith("base"):
        # The base is never trusted: the clean tree goes, the branch and its commit stay.
        assert not tree.exists() and rows[-1]["outcome"] == "allowed"
    else:
        assert tree.is_dir() and rows[-1]["outcome"] == "denied"


def test_a_workspace_the_enterprise_gate_rejects_runs_nothing(env: Any, monkeypatch: Any) -> None:
    _bind(env)
    _, ts = _wrap(env)
    monkeypatch.setattr(surface, "workspace_admitted", lambda team: False)
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/summarize"), _noop))
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert env.routed == [] and _calls(env, "agents.conversations.archive") == []
    for op in ("slack.code_channel.command", "slack.code_channel.archive"):
        assert env.sel.ops(op)[-1]["error"] == "enterprise_origin_mismatch"


def test_an_archived_channel_fires_no_turn_hooks(env: Any) -> None:
    from kiro_crew.slack.code_channels import binding as binding_mod

    binding = _bind(env)
    env.store.update_binding(CH, archived_at=1.0)
    fired = asyncio.run(
        binding_mod.notify_turn_start(CH, binding.anchor_ts, SLOT, source="dashboard")
    )
    assert fired is False


def test_a_channel_with_no_bound_project_cleans_the_worktree_its_bind_made(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    from kiro_crew.slack.code_channels.workspace import core as workspace_core

    root, tree = worktree
    _bind(env)  # no project on the binding: the bind used the dashboard default
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.resolved_project = str(root)
    WorkspaceStore().put(record)

    class Client:
        async def set_context_bar(self, *_a: Any, **_k: Any) -> dict:
            return {"ok": True}

    svc = service.WorkspaceService(lambda: Client(), store=WorkspaceStore(tmp_path / "shared.json"))
    svc.store.put(WorkspaceStore().get("T1", CH))
    monkeypatch.setattr(workspace_core, "_SERVICE", svc)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists()
    # The cleanup wrote through the service's own store.
    assert svc.store.get("T1", CH).worktree == ""


def test_a_wrap_up_request_that_fails_to_persist_moves_no_fence(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    tag = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    asyncio.run(wrapup.on_turn_start(binding, SimpleNamespace(prompt=f"/wrap-up ({tag})")))

    async def refuse(*_a: Any, **_k: Any) -> Any:
        raise OSError("read-only")

    monkeypatch.setattr(wrapup, "update_async", refuse)
    with pytest.raises(OSError):
        asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    # The failed second request took nothing over: the running one is still live.
    assert wrapup._requests[CH] == tag


def test_an_unrecorded_archive_cleans_nothing_and_fences_the_channel(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    _, ts = _wrap(env)

    def refuse(*_a: Any, **_k: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(wrapup, "_mark_archived", refuse)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert _calls(env, "agents.conversations.archive")  # Slack archived it
    assert tree.is_dir()  # but nothing was cleaned up
    assert env.sel.ops("slack.code_channel.archive")[-1]["outcome"] == "error"
    assert env.sel.ops("slack.code_channel.worktree_release") == []
    assert _run(env, tools.RENAME, {"title": "x"}).code == "archived"
    assert env.notified


def test_a_channel_unarchived_before_the_retry_is_never_stamped_or_cleaned(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    _, ts = _wrap(env)
    _flaky_archive_stamp(env, monkeypatch, failures=2)
    # Someone unarchived it in Slack before the first retry ran.
    env.api.responses["conversations.info"] = {
        "ok": True,
        "channel": {"id": CH, "is_archived": False},
    }

    async def click_then_wait() -> None:
        assert await wrapup.handle_archive_click(env.orch, _click(OWNER, ts))
        await wrapup.drain_recoveries()

    asyncio.run(click_then_wait())
    assert not env.store.get_binding(CH).archived_at and tree.is_dir()
    assert CH not in surface._archived_here  # live again in this process
    assert env.sel.ops("slack.code_channel.worktree_release") == []
    assert any(
        "result=unarchived" in r["resources"] for r in env.sel.ops("slack.code_channel.archive")
    )


def test_an_unrecorded_archive_is_recorded_and_cleaned_up_by_the_retry(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    _, ts = _wrap(env)
    _flaky_archive_stamp(env, monkeypatch, failures=2)

    async def click_then_wait() -> None:
        assert await wrapup.handle_archive_click(env.orch, _click(OWNER, ts))
        await wrapup.drain_recoveries()

    asyncio.run(click_then_wait())
    assert env.store.get_binding(CH).archived_at > 0
    assert not tree.exists()  # the retry released the worktree
    assert any(
        "result=removed" in r["resources"]
        for r in env.sel.ops("slack.code_channel.worktree_release")
    )
    assert CH not in surface._archived_here  # recorded: the stored binding answers again
    notice = env.notified[0][0][2]
    assert "retrying" in notice and "archive event" not in notice and str(tree) in notice


def test_a_click_on_a_fenced_unrecorded_archive_retries_instead_of_answering_done(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    _, ts = _wrap(env)
    _flaky_archive_stamp(env, monkeypatch, failures=5)  # the click's 2 + the retry's 3

    async def first_click() -> None:
        assert await wrapup.handle_archive_click(env.orch, _click(OWNER, ts))
        await wrapup.drain_recoveries()

    asyncio.run(first_click())
    assert not env.store.get_binding(CH).archived_at and tree.is_dir()
    assert "remove it by hand" in env.notified[-1][0][2]

    async def second_click() -> None:
        assert await wrapup.handle_archive_click(env.orch, _click(OWNER, ts))
        await wrapup.drain_recoveries()

    asyncio.run(second_click())
    assert env.store.get_binding(CH).archived_at > 0 and not tree.exists()
    assert any("archive_retry" in r["resources"] for r in env.sel.ops("slack.code_channel.archive"))


def test_archive_moves_the_bound_session_and_folder_off_the_tree_before_removing_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.folder_id = "f1"
    WorkspaceStore().put(record)
    env.state._folders = [{"id": "f1", "name": "fix", "project_dir": str(tree)}]

    async def mutate_folders(apply: Any, **_k: Any) -> Any:
        _changed, result = apply(env.state._folders)
        return result

    env.state.mutate_folders = mutate_folders
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert events[0] == ("set_project", f"{SLOT}->{root}")
    assert [e[0] for e in events] == ["set_project", "worktree_remove"]
    assert not tree.exists()
    assert env.state._folders[0]["project_dir"] == str(root)


def test_archive_keeps_the_tree_when_a_session_cannot_be_moved_off_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=False)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir()
    assert [e[0] for e in events] == ["set_project"]
    kept = [
        r
        for r in env.sel.ops("slack.code_channel.worktree_release")
        if "result=kept" in r["resources"]
    ]
    assert kept and "a session still works in it" in str(kept[-1])


def test_archive_keeps_the_tree_while_a_worker_is_mid_turn_in_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    from kiro_crew.slack.code_channels.workspace import folders as cc_folders

    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.folder_id = "f1"
    WorkspaceStore().put(record)
    monkeypatch.setattr(cc_folders, "subtree_ids", lambda _s, fid: frozenset({fid}))
    monkeypatch.setattr(cc_folders, "sessions_in", lambda _s, _ids: {"chat-7-1": (True, 3)})
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and events == []  # nothing moved, nothing removed
    kept = [
        r
        for r in env.sel.ops("slack.code_channel.worktree_release")
        if "result=kept" in r["resources"]
    ]
    assert kept and "mid-turn" in str(kept[-1])


def test_sessions_moved_off_a_tree_git_then_keeps_are_moved_back(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True, refuse_remove=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir()
    assert events == [
        ("set_project", f"{SLOT}->{root}"),
        ("worktree_remove", str(tree)),
        ("set_project", f"{SLOT}->{tree}"),
    ]


def test_sessions_are_moved_back_when_the_removal_raises(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True, refuse_remove="raise")
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and events[-1] == ("set_project", f"{SLOT}->{tree}")
    kept = [
        r
        for r in env.sel.ops("slack.code_channel.worktree_release")
        if "result=kept" in r["resources"]
    ]
    assert kept and "cleanup failed" in str(kept[-1])


def test_archive_keeps_the_tree_while_the_bound_session_is_mid_turn(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    monkeypatch.setattr(
        env.state, "_slots", {SLOT: SimpleNamespace(turn_running=True)}, raising=False
    )
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and events == []


@pytest.mark.parametrize("change", ["bound_turn", "worker_turn", "worker_joined"])
def test_archive_keeps_the_tree_when_a_turn_starts_while_sessions_move(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path, change: str
) -> None:
    """Idle when checked is not idle at removal: a turn started (or a worker filed)
    while the sessions were being moved keeps the tree, and they move back."""
    from kiro_crew.slack.code_channels.workspace import folders as cc_folders

    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.folder_id = "f1"
    WorkspaceStore().put(record)
    bound = SimpleNamespace(turn_running=False, _turn_generation=1, folder_id="")
    worker = SimpleNamespace(turn_running=False, _turn_generation=4, folder_id="f1")
    slots = {SLOT: bound, "chat-7-1": worker}
    monkeypatch.setattr(env.state, "_slots", slots, raising=False)
    monkeypatch.setattr(cc_folders, "subtree_ids", lambda _s, fid: frozenset({fid}))
    events: list[tuple[str, str]] = []
    svc = _retarget_service(monkeypatch, tmp_path, events, applies=True)
    moved = svc._set_project

    projects: dict[str, str] = {}

    async def set_project(key: str, path: str) -> bool:
        ok = await moved(key, path)
        projects[key] = path
        if path == str(root):  # the move off the tree; a turn starts meanwhile
            if change == "bound_turn":
                bound.turn_running, bound._turn_generation = True, 2
            elif change == "worker_turn":
                worker._turn_generation = 5  # began and ended between the reads
            else:
                slots["chat-8-1"] = SimpleNamespace(
                    turn_running=False, _turn_generation=0, folder_id="f1"
                )
        return ok

    svc._set_project = set_project

    async def reader(key: str) -> str:
        return projects.get(key, str(tree))

    svc._project_of = reader
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir()
    assert not any(e[0] == "worktree_remove" for e in events)
    assert ("set_project", f"{SLOT}->{tree}") in events  # moved back
    kept = [
        r
        for r in env.sel.ops("slack.code_channel.worktree_release")
        if "result=kept" in r["resources"]
    ]
    assert kept


def test_a_retry_takes_its_stamp_back_when_slack_was_unarchived_meanwhile(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    _, ts = _wrap(env)
    _flaky_archive_stamp(env, monkeypatch, failures=2)
    answers = iter([True, False])  # archived at the check, unarchived right after the stamp

    async def still_archived(_channel: str) -> bool:
        return next(answers)

    monkeypatch.setattr(wrapup, "_still_archived", still_archived)

    async def click_then_wait() -> None:
        assert await wrapup.handle_archive_click(env.orch, _click(OWNER, ts))
        await wrapup.drain_recoveries()

    asyncio.run(click_then_wait())
    assert not env.store.get_binding(CH).archived_at and tree.is_dir()
    assert CH not in surface._archived_here
    assert env.sel.ops("slack.code_channel.worktree_release") == []


def test_a_failed_binding_read_does_not_end_the_recovery(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    _, ts = _wrap(env)
    _flaky_archive_stamp(env, monkeypatch, failures=2)
    real_get = env.store.get_binding
    failed = [False]

    def flaky_get(channel: str) -> Any:
        # The first read after Slack archived the channel is the recovery's.
        if _calls(env, "agents.conversations.archive") and not failed[0]:
            failed[0] = True
            raise OSError("busy")
        return real_get(channel)

    monkeypatch.setattr(env.store, "get_binding", flaky_get)

    async def click_then_wait() -> None:
        assert await wrapup.handle_archive_click(env.orch, _click(OWNER, ts))
        await wrapup.drain_recoveries()

    asyncio.run(click_then_wait())
    assert env.store.get_binding(CH).archived_at > 0 and not tree.exists()


# ── The record's guards ──────────────────────────────────────────────────────


def test_the_record_lives_in_the_fenced_routing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record sits beside the core's binding record in ``routing/``, which the
    shared floors fence as a whole: agent file tools can neither read nor write it,
    under either home prefix, while the same basename in a project stays writable."""
    from kiro_crew.security.paths import is_sensitive_path, is_sensitive_write_path
    from kiro_crew.slack.code_channels import store as core_store
    from kiro_crew.slack.code_channels import surface_store

    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path / "home")
    default = surface_store._default_path()
    assert default.parent == core_store._default_path().parent
    assert default.parent.name == core_store.STORE_DIRNAME == "routing"

    home = os.path.expanduser
    for prefix in ("~/.kirocrew", "~/.kiro/crew"):
        fenced = home(f"{prefix}/routing/slack-code-channel-surface.json")
        assert is_sensitive_path(fenced), fenced
        assert is_sensitive_write_path(fenced), fenced
        # The atomic-write temp beside it is inside the fenced directory too.
        assert is_sensitive_write_path(home(f"{prefix}/routing/tmpabc123.tmp"))
    assert not is_sensitive_write_path(str(tmp_path / "slack-code-channel-surface.json"))


def test_a_missing_routing_directory_is_created_on_first_write(tmp_path: Path) -> None:
    """A missing file reads as no records; the first write creates ``routing/``
    (a host with no sandbox pre-create) and the next reader sees it."""
    path = tmp_path / "routing" / "slack-code-channel-surface.json"
    first = SurfaceStore(path)
    assert first.get(CH) is None
    first.update(CH, plan_canvas_id="F1")
    assert SurfaceStore(path).get(CH).plan_canvas_id == "F1"


def test_a_pre_created_empty_record_reads_as_no_records_and_stays_writable(
    tmp_path: Path,
) -> None:
    """An empty ``{}`` document reads as empty and the gateway's first write replaces it."""
    path = tmp_path / "slack-code-channel-surface.json"
    path.write_text("{}", encoding="utf-8")
    first = SurfaceStore(path)
    assert first.get(CH) is None
    first.update(CH, plan_canvas_id="F1")
    assert SurfaceStore(path).get(CH).plan_canvas_id == "F1"

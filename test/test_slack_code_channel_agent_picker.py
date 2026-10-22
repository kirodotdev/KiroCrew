"""Slack Code: each code channel's agent -- the kickoff picker and ``/kirocrew agent``.

Recorded Slack payloads in, recorded calls out (``slack_code_channels_helpers.Env``).
The dashboard's agent-switch transaction is faked at ``switch_slot_agent`` itself,
so every switch here goes through the module's real call into it. Each test names
the input it varies: which default resolves, a pick by the owner, by someone
else, on a message that is not the kickoff, during a turn, an unknown agent, a
refused switch, the toggle off, agents that cannot be listed, Slack refusing the
Block Kit post.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from slack_code_channels_helpers import OWNER, Env, make_env, mention

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import slack_code_settings as scs
from kiro_crew.slack import interactions
from kiro_crew.slack.code_channels import agent_picker, commands, routing, surface
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import SurfaceStore
from kiro_crew.slack.code_channels.surface_store import set_store as set_surface_store

#: The real list reader, captured before ``Env`` swaps in a fixed list.
REAL_AGENT_CHOICES = agent_picker.agent_choices

CH = "C0CODE"
ANCHOR = "300.000001"
SLOT = "chat-1-1"
STRANGER = "USTRANGER"


class Switches:
    """Stand-in for ``switch_slot_agent``: records each call, answers per queue."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: list[tuple[int, dict[str, Any]]] = []

    async def __call__(
        self, state: Any, slot: Any, name: str, agent: Any, kind: Any, caller: Any
    ) -> web.Response:
        self.calls.append(
            {"slot": slot, "name": name, "agent": agent, "kind": kind, "caller": caller}
        )
        if self.answers:
            status, body = self.answers.pop(0)
        elif getattr(slot, "running", False):
            # The real transaction's guard: never under an in-flight turn.
            status, body = 409, {"error": "a turn is in flight", "code": "turn_in_flight"}
        else:
            status, body = 200, {"ok": True}
        if status == 200 and body.get("ok"):
            slot.agent = agent
            body = {"agent": agent, **body}
        return web.json_response(body, status=status)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Env]:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    gen = make_env(monkeypatch, tmp_path)
    e = next(gen)
    set_surface_store(SurfaceStore(tmp_path / "surface.json"))
    for mod in (commands, agent_picker):
        monkeypatch.setattr(mod, "live_config", e.cfg)
    monkeypatch.setattr(surface, "sel", lambda: e.sel)
    surface.set_slack_provider(lambda: e.slack)
    monkeypatch.setattr(surface, "dashboard_state", lambda: e.state)
    monkeypatch.setattr(surface, "workspace_admitted", lambda team: team == "T1")
    e.allowed = {OWNER}
    from kiro_crew.messaging import identity
    from kiro_crew.slack import handler
    from kiro_crew.slack.handler_runtime import access

    monkeypatch.setattr(access, "is_allowed_user", lambda u: u in e.allowed)
    monkeypatch.setattr(handler, "is_allowed_user", lambda u: u in e.allowed)

    async def permitted(_channel: str) -> bool:
        return True

    monkeypatch.setattr(identity, "channel_inbound_permitted", permitted)
    monkeypatch.setattr(identity, "channel_outbound_permitted", permitted)
    e.switches = Switches()
    monkeypatch.setattr(chat_handlers, "switch_slot_agent", e.switches)
    e.transcript = []
    e.broadcasts = []
    assert e.state is not None
    e.state.push_slots_update = lambda: None
    e.state.broadcast_ws = lambda kind, data: e.broadcasts.append((kind, data))
    commands._seen = None
    surface._archived_here.clear()
    yield e
    set_surface_store(None)
    surface.set_slack_provider(None)
    try:
        next(gen)
    except StopIteration:
        pass


def _bind(env: Env, agent: str = "kirocrew-conductor", *, running: bool = False) -> Any:
    env.store.put_binding(
        Binding(
            channel_id=CH,
            team_id="T1",
            session_key=SLOT,
            anchor_ts=ANCHOR,
            origin_channel_id="C0ORIGIN",
            origin_ts="1700000000.000100",
            title="Fix flaky login",
        )
    )
    slot = SimpleNamespace(
        key=SLOT,
        running=running,
        agent=agent,
        project="",
        folder_id="",
        append=lambda role, text, cls: env.transcript.append(text),
    )
    assert env.state is not None
    env.state.linked[ANCHOR] = slot
    return slot


def _pick(agent: str, *, user: str = OWNER, ts: str = ANCHOR) -> dict[str, Any]:
    """A recorded ``block_actions`` payload for the kickoff's static_select."""
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user, "team_id": "T1"},
        "channel": {"id": CH},
        "message": {"ts": ts, "blocks": []},
        "actions": [
            {
                "type": "static_select",
                "action_id": agent_picker.AGENT_SELECT_ACTION_ID,
                "block_id": "kc_code_channel_agent.1",
                "selected_option": {
                    "text": {"type": "plain_text", "text": agent},
                    "value": agent,
                },
            }
        ],
    }


def _slash(text: str, *, user: str = OWNER, trigger: str = "T-1") -> dict[str, Any]:
    return {
        "command": "/kirocrew",
        "text": text,
        "channel_id": CH,
        "user_id": user,
        "team_id": "T1",
        "trigger_id": trigger,
        "response_url": "https://hooks.slack.com/commands/T1/1/x",
    }


def _select(blocks: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for block in blocks or []:
        accessory = block.get("accessory") or {}
        if accessory.get("action_id") == agent_picker.AGENT_SELECT_ACTION_ID:
            return accessory
    return None


def _initial(blocks: list[dict[str, Any]] | None) -> str:
    select = _select(blocks) or {}
    return str((select.get("initial_option") or {}).get("value") or "")


def _rows(env: Env, outcome: str | None = None) -> list[dict[str, Any]]:
    rows = env.sel.ops(agent_picker.OPERATION)
    assert all(r["caller"] == agent_picker.AUDIT_CALLER for r in rows)
    return [r for r in rows if outcome is None or r["outcome"] == outcome]


async def _open(env: Env) -> list[dict[str, Any]]:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": CH}
    assert await routing.maybe_open_code_channel(env.orch, mention(), "fix the login test", OWNER)
    binding = env.store.get_binding(CH)
    assert binding is not None
    return env.slack.blocks.get(binding.anchor_ts) or []


# ── The kickoff picker ────────────────────────────────────────────────────


def test_the_kickoff_carries_a_picker_preselected_with_the_conductor(env: Env) -> None:
    blocks = asyncio.run(_open(env))
    select = _select(blocks)
    assert select is not None and select["type"] == "static_select"
    assert [o["value"] for o in select["options"]] == env.agents
    # Zero config: the auto "Code channels" folder's agent.
    assert _initial(blocks) == "kirocrew-conductor"
    # The kickoff's own text is still its first block and its fallback text.
    kickoff_text = env.slack.posts[0][1]
    assert blocks[0]["text"]["text"] == kickoff_text


def test_an_explicit_default_agent_preselects_over_the_folder(env: Env) -> None:
    env.defaults["agent"] = "builder"
    assert _initial(asyncio.run(_open(env))) == "builder"


def test_a_folder_default_agent_preselects_when_no_explicit_one(env: Env) -> None:
    assert env.state is not None
    env.state.folders.append(
        {"id": "abcdef012345", "name": "Work", "parent_id": "", "default_agent": "builder"}
    )
    env.defaults["folder"] = "Work"
    assert _initial(asyncio.run(_open(env))) == "builder"


def test_agents_that_cannot_be_listed_leave_a_plain_kickoff(env: Env) -> None:
    env.agents = None
    assert asyncio.run(_open(env)) == []
    assert env.slack.posts[0][0] == CH  # the channel still opened, text only


def test_a_refused_block_kit_post_falls_back_to_text(env: Env) -> None:
    async def refused(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("missing_scope")

    env.slack.post_blocks = refused
    assert asyncio.run(_open(env)) == []
    assert env.store.get_binding(CH) is not None


def test_slacks_option_cap_keeps_the_selected_agent() -> None:
    names = [f"agent-{i:03d}" for i in range(150)]
    blocks = agent_picker.kickoff_blocks("hi", names, "agent-140")
    select = _select(blocks)
    assert select is not None and len(select["options"]) == agent_picker.MAX_OPTIONS
    assert _initial(blocks) == "agent-140"


# ── A pick in the kickoff ─────────────────────────────────────────────────


def test_the_owners_pick_rebinds_through_the_agent_switch_path(env: Env) -> None:
    slot = _bind(env)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    [call] = env.switches.calls
    assert call["slot"] is slot and call["name"] == SLOT
    assert call["agent"] == "builder" and call["kind"] == "template"
    # A Slack pick never claims the dashboard owner's authority.
    assert call["caller"].owner is False and call["caller"].request is None
    assert slot.agent == "builder"
    # The kickoff now shows the choice, and who made it.
    [(channel, ts, _text, blocks)] = env.slack.edits
    assert (channel, ts) == (CH, ANCHOR) and _initial(blocks) == "builder"
    assert f"<@{OWNER}>" in json.dumps(blocks)
    assert env.slack.ephemerals == []
    [row] = _rows(env, "allowed")
    assert "agent=builder" in row["resources"] and "result=switched" in row["resources"]
    assert env.broadcasts == [("slot_agent_switch", {"slot": SLOT, "agent": "builder"})]
    assert env.transcript and "builder" in env.transcript[0]


def test_someone_elses_pick_is_refused_ephemerally_and_changes_nothing(env: Env) -> None:
    slot = _bind(env)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder", user=STRANGER)))
    assert env.switches.calls == [] and slot.agent == "kirocrew-conductor"
    assert env.slack.ephemerals == [(CH, STRANGER, agent_picker.NOT_ALLOWED_TEXT)]
    # The select is put back on the agent the session runs.
    assert _initial(env.slack.edits[-1][3]) == "kirocrew-conductor"
    [row] = _rows(env, "denied")
    assert f"user={STRANGER}" in row["resources"] and row["error"] == "unauthorized user"


def test_the_dispatcher_refuses_a_non_owner_before_the_picker(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    monkeypatch.setattr(interactions, "_orch", env.orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: u in env.allowed)
    monkeypatch.setattr(interactions, "sel", lambda: env.sel)
    asyncio.run(interactions.dispatch(_pick("builder", user=STRANGER)))
    assert env.switches.calls == []
    assert [e[1] for e in env.slack.ephemerals] == [STRANGER]
    [row] = env.sel.ops("slack.interactive")
    assert row["outcome"] == "denied"
    [picker_row] = _rows(env, "denied")
    assert f"user={STRANGER}" in picker_row["resources"]
    assert picker_row["error"] == "unauthorized user"


def test_the_dispatcher_hands_the_owners_pick_to_the_picker(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    monkeypatch.setattr(interactions, "_orch", env.orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: u in env.allowed)

    async def permitted(_channel: str) -> bool:
        return True

    monkeypatch.setattr(interactions, "channel_inbound_permitted", permitted)
    asyncio.run(interactions.dispatch(_pick("builder")))
    assert [c["agent"] for c in env.switches.calls] == ["builder"]


def test_a_pick_on_a_message_that_is_not_the_kickoff_is_refused(env: Env) -> None:
    _bind(env)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder", ts="999.000001")))
    assert env.switches.calls == []
    assert _rows(env, "denied")[0]["error"] == "not a code channel kickoff"


def test_a_pick_during_a_turn_is_refused_with_try_again(env: Env) -> None:
    """``turn_in_flight``: nothing is held; the person is asked to retry."""
    slot = _bind(env, running=True)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "kirocrew-conductor"
    assert [c["agent"] for c in env.switches.calls] == ["builder"]
    assert env.slack.ephemerals == [(CH, OWNER, agent_picker.BUSY_TEXT.format(agent="builder"))]
    assert "when this turn ends" in agent_picker.BUSY_TEXT
    # The select is put back on the agent the session runs.
    assert _initial(env.slack.edits[-1][3]) == "kirocrew-conductor"
    [row] = _rows(env, "error")
    assert "result=refused code=turn_in_flight" in row["resources"]
    # Same answer through the command; a later pick, after the turn, switches.
    handled, replies = _command(env, "agent builder")
    assert handled and replies == [agent_picker.BUSY_TEXT.format(agent="builder")]
    slot.running = False
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "builder"


def test_a_refused_switch_is_said_ephemerally(env: Env) -> None:
    slot = _bind(env)
    env.switches.answers.append((503, {"error": "store down", "code": "history_unavailable"}))
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "kirocrew-conductor"
    [(_, user, text)] = env.slack.ephemerals
    assert user == OWNER and "store down" in text
    assert _rows(env, "error")[0]["error"] == "store down"


# ── /kirocrew agent ───────────────────────────────────────────────────────


def _command(env: Env, text: str, **kw: Any) -> tuple[bool, list[str]]:
    replies: list[str] = []

    async def respond(reply: str) -> None:
        replies.append(reply)

    handled = asyncio.run(commands.handle_slash(env.orch, _slash(text, **kw), respond))
    return handled, replies


def test_kirocrew_agent_name_switches_the_channel(env: Env) -> None:
    slot = _bind(env)
    handled, replies = _command(env, "agent builder")
    assert handled and replies == [agent_picker.SWITCHED_TEXT.format(agent="builder")]
    assert slot.agent == "builder" and env.routed == []  # not a prompt for the session
    assert _initial(env.slack.edits[-1][3]) == "builder"
    assert "via=command" in _rows(env, "allowed")[0]["resources"]


def test_kirocrew_agent_refuses_an_unknown_agent_with_the_list(env: Env) -> None:
    slot = _bind(env)
    handled, [reply] = _command(env, "agent nope")
    assert handled and env.switches.calls == [] and slot.agent == "kirocrew-conductor"
    assert "`nope`" in reply
    assert all(f"`{name}`" in reply for name in env.agents or [])
    assert _rows(env, "denied")[0]["error"] == "unknown agent"


def test_kirocrew_agent_from_someone_else_is_refused(env: Env) -> None:
    slot = _bind(env)
    handled, replies = _command(env, "agent builder", user=STRANGER)
    assert handled and replies == [agent_picker.NOT_ALLOWED_TEXT]
    assert env.switches.calls == [] and slot.agent == "kirocrew-conductor"
    assert _rows(env, "denied")[0]["error"] == "unauthorized user"
    assert env.sel.ops("slack.code_channel.command")[-1]["outcome"] == "denied"


def test_bare_kirocrew_agent_lists_the_agents(env: Env) -> None:
    _bind(env)
    handled, [reply] = _command(env, "agent")
    assert handled and "`kirocrew-conductor`" in reply and "`builder`" in reply
    assert env.switches.calls == []


def test_other_kirocrew_text_is_not_the_channels(env: Env) -> None:
    _bind(env)
    assert _command(env, "help") == (False, [])
    assert _command(env, "", trigger="T-2") == (False, [])


def test_toggle_off_leaves_both_doors_to_normal_routing(env: Env) -> None:
    _bind(env)
    env.on = False
    assert _command(env, "agent builder") == (False, [])
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder"))) is False
    assert env.switches.calls == []


# ── One agent list for every picker ───────────────────────────────────────


def test_settings_and_channel_pickers_list_the_same_choosable_roster(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings -> Slack's default-agent picker, the kickoff picker and
    ``/kirocrew agent`` read ONE list, ``_choosable_agent_names``: every agent a
    channel can run, never lite, guest, the dashboard manager or the heartbeat."""
    from kiro_crew import agent_discovery
    from kiro_crew.agent_discovery import AgentInfo
    from kiro_crew.dashboard.handlers import agent_catalog

    def row(name: str, owned: bool = True) -> AgentInfo:
        return AgentInfo(
            name=name, description="", model="", filename=f"{name}.json", kirocrew_owned=owned
        )

    installed = [
        row("kirocrew"),
        row("kirocrew-conductor"),
        row("kirocrew-team-lead"),
        row("kirocrew-guest"),
        row("kirocrew-lite"),
        row("kirocrew-heartbeat"),
        row("kirocrew-dashboard-manager"),
        row("my-agent", owned=False),
    ]
    monkeypatch.setattr(agent_discovery, "list_agents", lambda project_dir=None: installed)
    monkeypatch.setattr(agent_catalog.agent_state, "all_fork_info", lambda: {})
    monkeypatch.setattr(agent_catalog, "_name_would_be_masked", lambda name: False)

    async def both() -> tuple[Any, Any]:
        view = await scs.code_settings_view(env.cfg(), env.state)
        return view["code_channel_agents"], await REAL_AGENT_CHOICES(None)

    settings_list, channel_list = asyncio.run(both())
    assert settings_list == channel_list == scs._choosable_agent_names(None)
    assert {"kirocrew", "kirocrew-conductor", "kirocrew-team-lead", "my-agent"} <= set(channel_list)
    hidden = {"kirocrew-guest", "kirocrew-lite", "kirocrew-heartbeat", "kirocrew-dashboard-manager"}
    assert not hidden & set(channel_list)


# ── Review regressions ────────────────────────────────────────────────────


def test_kirocrew_agent_outside_a_code_channel_is_the_apps_own_command(env: Env) -> None:
    """Toggle on, a DM or ordinary channel: the global /kirocrew agent runs."""
    replies: list[str] = []

    async def respond(reply: str) -> None:
        replies.append(reply)

    dm = {**_slash("agent builder"), "channel_id": "D0DM"}
    assert asyncio.run(commands.handle_slash(env.orch, dm, respond)) is False
    assert replies == [] and env.switches.calls == []
    assert env.sel.ops("slack.code_channel.command") == []


def test_kirocrew_agent_splits_its_argument_like_the_router(env: Env) -> None:
    slot = _bind(env)
    handled, _ = _command(env, "agent\tbuilder")
    assert handled and slot.agent == "builder"


def test_running_sub_agents_refuse_the_pick(env: Env) -> None:
    slot = _bind(env)
    env.switches.answers.append(
        (409, {"error": "sub-agents are running", "code": "slot_subagents_running"})
    )
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "kirocrew-conductor"
    [(_, _, text)] = env.slack.ephemerals
    assert "sub-agents are running" in text


def test_an_app_slot_is_switched_in_its_own_app_scope(env: Env) -> None:
    slot = _bind(env)
    slot._app = "my-app"
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert env.switches.calls[0]["caller"].app == "my-app"


def test_an_agent_name_past_slacks_value_cap_is_not_offered() -> None:
    long_name = "a" * 151
    select = _select(agent_picker.kickoff_blocks("hi", ["kirocrew", long_name], "kirocrew"))
    assert select is not None and [o["value"] for o in select["options"]] == ["kirocrew"]


@pytest.mark.parametrize("mode", ["off", "review"])
def test_the_code_channels_own_activation_refuses_every_door(env: Env, mode: str) -> None:
    """The origin stays on; the code channel itself is off or in review."""
    from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW

    slot = _bind(env)
    env.activation[CH] = ACTIVATION_OFF if mode == "off" else ACTIVATION_REVIEW
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    handled, replies = _command(env, "agent builder")
    assert handled and replies == [agent_picker.NOT_ALLOWED_TEXT]
    assert env.switches.calls == [] and slot.agent == "kirocrew-conductor"
    assert env.slack.ephemerals == [(CH, OWNER, agent_picker.NOT_ALLOWED_TEXT)]

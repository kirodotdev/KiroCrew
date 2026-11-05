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
from dataclasses import replace
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
REAL_AGENT_MENU = agent_picker.agent_menu

CH = "C0CODE"
ANCHOR = "300.000001"
SLOT = "chat-1-1"
STRANGER = "USTRANGER"


class Switches:
    """Stand-in for ``switch_slot_agent``: records each call, answers per queue."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: list[tuple[int, dict[str, Any]]] = []
        #: Runs inside the transaction, before its commit (a gate closing there).
        self.before_commit: Any = None

    async def __call__(
        self, state: Any, slot: Any, name: str, agent: Any, kind: Any, caller: Any
    ) -> web.Response:
        self.calls.append(
            {"slot": slot, "name": name, "agent": agent, "kind": kind, "caller": caller}
        )
        if self.before_commit is not None:
            self.before_commit()
        still_permitted = getattr(caller, "still_permitted", None)
        if self.answers:
            status, body = self.answers.pop(0)
        elif still_permitted is not None and not still_permitted():
            # The real transaction's commit-point check of the caller's gates.
            status, body = 403, {"error": "not permitted", "code": "caller_not_permitted"}
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


def test_agents_that_cannot_be_listed_leave_the_kickoff_without_a_picker(env: Env) -> None:
    env.agents = None
    blocks = asyncio.run(_open(env))
    assert _select(blocks) is None  # no agent picker ...
    from kiro_crew.slack.code_channels import approval_picker

    ids = [(b.get("accessory") or {}).get("action_id") for b in blocks]
    assert approval_picker.APPROVAL_SELECT_ACTION_ID in ids  # ... but the mode select stays
    assert env.store.get_binding(CH) is not None  # the channel still opened


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
    # _announce writes the one /agent line, so no second boundary row.
    assert call["caller"].notice is False
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
    assert env.slack.ephemerals == [(CH, STRANGER, agent_picker.OWNER_ONLY_TEXT)]
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
    assert env.slack.ephemerals == [(CH, STRANGER, agent_picker.OWNER_ONLY_TEXT)]
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
    assert _rows(env, "denied")[0]["error"] == "stale kickoff"
    # A kickoff a rebind replaced points at the live one, not "you can't".
    assert env.slack.ephemerals == [
        (CH, OWNER, agent_picker.STALE_PICKER_TEXT.format(command="kirocrew"))
    ]


def test_a_pick_from_a_workspace_not_admitted_posts_nothing(env: Env) -> None:
    _bind(env)
    # A stale picker: no live kickoff to check first, so the workspace must be.
    payload = _pick("builder", ts="999.000001")
    payload["team"] = {"id": "T-OTHER"}
    payload["user"]["team_id"] = "T-OTHER"
    assert asyncio.run(agent_picker.handle_select(env.orch, payload))
    assert env.switches.calls == [] and env.slack.ephemerals == []
    assert _rows(env, "denied")[0]["error"] == "enterprise_origin_mismatch"


def test_a_switch_that_raises_is_answered_audited_and_redrawn(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """switch_slot_agent rolls back and re-raises (e.g. a failed session reset)."""
    slot = _bind(env)

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("session reset failed")

    monkeypatch.setattr(chat_handlers, "switch_slot_agent", boom)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "kirocrew-conductor"
    assert env.slack.ephemerals == [
        (
            CH,
            OWNER,
            agent_picker.FAILED_TEXT.format(
                agent="builder", reason=agent_picker.SWITCH_FAILED_REASON
            ),
        )
    ]
    [row] = _rows(env, "error")
    assert "result=failed" in row["resources"] and row["error"] == "switch raised"
    # The select is put back on the agent the session runs.
    assert _initial(env.slack.edits[-1][3]) == "kirocrew-conductor"
    # The command door answers too, instead of raising into the slash handler.
    handled, replies = _command(env, "agent builder")
    assert handled and replies == [
        agent_picker.FAILED_TEXT.format(agent="builder", reason=agent_picker.SWITCH_FAILED_REASON)
    ]


def test_a_refusal_reason_is_redacted_before_it_reaches_slack(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    key = "AKIA" + "QWERTYUIOPASDFGH"
    url = (
        "https://attacker.example/collect?data="
        + "c2VjcmV0LXRva2VuLXZhbHVlLWhlcmUtMTIzNDU2Nzg5MA=="
    )
    # Split by markup: only the display sink's display-form scan finds it.
    split = "AKIA**" + "ZXCVBNMASDFGHJKL**"
    env.switches.answers.append(
        (503, {"error": f"unavailable: {key} {split} {url}", "code": "store_unavailable"})
    )
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    [(_, _, text)] = env.slack.ephemerals
    for secret in (key, "ZXCVBNMASDFGHJKL", "attacker.example/collect"):
        assert secret not in text
    assert "[REDACTED" in text
    # The audit row carries the same redacted reason.
    [row] = [r for r in _rows(env) if "store_unavailable" in r["resources"]]
    assert "[REDACTED" in row["error"] and "ZXCVBNMASDFGHJKL" not in row["error"]


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
    assert env.switches.calls[0]["caller"].notice is False
    assert _initial(env.slack.edits[-1][3]) == "builder"
    assert "via=command" in _rows(env, "allowed")[0]["resources"]


@pytest.mark.parametrize("via", ["command", "select"])
def test_picking_a_template_over_a_same_named_crewmate_switches(env: Env, via: str) -> None:
    """A folder or Slack default can resolve a channel to the ``builder``
    crewmate; picking the ``builder`` template must run the template, not
    answer "unchanged" and keep the crewmate's bindings and memory."""
    slot = _bind(env)
    slot.agent, slot.agent_kind = "builder", "member"
    if via == "command":
        handled, replies = _command(env, "agent builder")
        assert handled and replies == [agent_picker.SWITCHED_TEXT.format(agent="builder")]
    else:
        assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert [c["agent"] for c in env.switches.calls] == ["builder"]


def test_picking_the_template_a_channel_already_runs_is_unchanged(env: Env) -> None:
    slot = _bind(env)
    slot.agent, slot.agent_kind = "builder", "template"
    handled, replies = _command(env, "agent builder")
    assert handled and replies == [agent_picker.SAME_AGENT_TEXT.format(agent="builder")]
    assert env.switches.calls == []


def test_the_kickoff_options_say_what_each_agent_does(env: Env) -> None:
    """Each option has a plain label that tells it apart and is never cut, and what
    the agent does in the option's own ``description`` line, within Slack's caps."""
    env.agents = [
        "kirocrew",
        "kirocrew-conductor",
        "kirocrew-team-lead",
        "kirocrew-worker",
        "builder",
    ]
    env.agent_descriptions = {
        "builder": "Builds and tests changes in your repo",
        "kirocrew-conductor": "Splits a goal into work items <!channel> " + "word " * 40,
        "kirocrew-team-lead": "Splits a goal into work items " + "word " * 40,
    }
    select = _select(asyncio.run(_open(env))) or {}
    options = select["options"]
    labels = [o["text"]["text"] for o in options]
    # Every label is unique, within the cap, and is the whole label (never cut).
    assert len(set(labels)) == len(labels)
    for option in options:
        assert option["text"]["type"] == "plain_text"
        assert 0 < len(option["text"]["text"]) <= 75 and "…" not in option["text"]["text"]
        if "description" in option:
            assert option["description"]["type"] == "plain_text"
            assert 0 < len(option["description"]["text"]) <= 75
    by_value = {o["value"]: o for o in options}
    assert by_value["kirocrew-team-lead"]["text"]["text"] == "Team lead (kirocrew-team-lead)"
    assert by_value["builder"]["text"]["text"] == "builder"  # not shipped: its name
    assert by_value["builder"]["description"]["text"] == "Builds and tests changes in your repo"
    assert "description" not in by_value["kirocrew"]  # no description: none shown
    # Two descriptions that start alike are cut at a word, but the labels differ.
    cut = by_value["kirocrew-conductor"]["description"]["text"]
    assert cut.endswith("…") and not cut.endswith(" …") and cut.split()[-1] == "word…"
    assert (
        by_value["kirocrew-conductor"]["text"]["text"]
        != by_value["kirocrew-team-lead"]["text"]["text"]
    )
    assert select["initial_option"] in options  # Slack requires an exact match


def test_kirocrew_agent_lists_what_each_agent_does(env: Env) -> None:
    _bind(env)
    env.agent_descriptions = {"builder": "Builds and tests changes in your repo"}
    _handled, [current] = _command(env, "agent")
    _handled, [unknown] = _command(env, "agent nope", trigger="T-2")
    for reply in (current, unknown):
        assert "\n• `builder` — Builds and tests changes in your repo" in reply
        assert "\n• `kirocrew`" in reply


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
    assert handled and replies == [agent_picker.OWNER_ONLY_TEXT]
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
            name=name,
            description=f"what {name} does",
            model="",
            filename=f"{name}.json",
            kirocrew_owned=owned,
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
        return view["code_channel_agents"], await REAL_AGENT_MENU(None)

    settings_list, channel_menu = asyncio.run(both())
    channel_list = list(channel_menu)
    assert settings_list == channel_list == scs._choosable_agent_names(None)
    # Each comes with what it does, for the kickoff and /kirocrew agent to show.
    assert channel_menu["my-agent"] == "what my-agent does"
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
    # A "try again", like a turn in flight -- not a failure.
    assert env.slack.ephemerals == [
        (CH, OWNER, agent_picker.SUBAGENTS_BUSY_TEXT.format(agent="builder"))
    ]


def test_an_app_slot_is_switched_in_its_own_app_scope(env: Env) -> None:
    slot = _bind(env)
    slot._app = "my-app"
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert env.switches.calls[0]["caller"].app == "my-app"


@pytest.mark.parametrize("length", [76, 151])
def test_an_agent_name_past_slacks_label_cap_is_not_offered(length: int) -> None:
    """Its label would be cut, so it is not offered (``/kirocrew agent`` still reaches it)."""
    long_name = "a" * length
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


def _pending_window(env: Env, slot: Any) -> list[str]:
    """Make *slot* a restored row whose window is still on disk.

    Records each window read with the thread it ran on, and fails a test
    whenever the switch path reads the rows while the window is pending (the
    real ``messages`` would then read the transcript inline on the loop).
    """
    import threading

    reads: list[str] = []
    loop_thread = threading.get_ident()

    def read(_slot: Any) -> tuple[list[Any], int]:
        reads.append("loop" if threading.get_ident() == loop_thread else "worker")
        return ([], 0)

    def load_window(*_prefetched: Any) -> None:
        slot.window_pending = False

    slot.window_pending = True
    slot._lazy_window = SimpleNamespace(read=read)
    slot.load_window = load_window
    original = env.switches.__call__

    async def guarded(*args: Any) -> Any:
        assert slot.window_pending is False, "switch read rows with the window pending"
        return await original(*args)

    real_append = slot.append

    def append(role: str, text: str, cls: str) -> None:
        assert slot.window_pending is False, "announcement appended with the window pending"
        real_append(role, text, cls)

    slot.append = append
    env.guarded = guarded
    return reads


def test_a_restored_slots_window_is_loaded_off_the_loop_before_the_switch(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notice=False skips the built-in notice's off-loop load, so the picker loads it."""
    slot = _bind(env)
    reads = _pending_window(env, slot)
    monkeypatch.setattr(chat_handlers, "switch_slot_agent", env.guarded)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "builder"
    assert reads == ["worker"]
    assert env.transcript and "builder" in env.transcript[-1]


def test_a_window_that_cannot_be_loaded_refuses_before_switching(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _bind(env)
    _pending_window(env, slot)

    def broken(_slot: Any) -> Any:
        raise OSError("disk gone")

    slot._lazy_window = SimpleNamespace(read=broken)
    monkeypatch.setattr(chat_handlers, "switch_slot_agent", env.guarded)
    handled, replies = _command(env, "agent builder")
    assert handled and replies == [agent_picker.NO_SESSION_TEXT]
    assert slot.agent == "kirocrew-conductor" and env.transcript == []
    assert _rows(env, "error")[0]["error"] == "transcript window could not be loaded"


@pytest.mark.parametrize(
    "change",
    [
        "activation_off",
        "activation_review",
        "archived",
        "stored_archived",
        "slack_denied",
        "feature_off",
        "owner_removed",
        "workspace_denied",
    ],
)
def test_a_gate_that_closes_while_the_window_loads_refuses_the_commit(
    env: Env, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """authorize ran before the awaits; the gates are checked again before the commit."""
    from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW
    from kiro_crew.messaging import identity

    slot = _bind(env)

    async def load_and_close(_slot: Any) -> None:
        if change == "activation_off":
            env.activation[CH] = ACTIVATION_OFF
        elif change == "activation_review":
            env.activation[CH] = ACTIVATION_REVIEW
        elif change == "archived":
            surface._archived_here.add(CH)
        elif change == "stored_archived":
            env.store.update_binding(CH, archived_at=1.0)
        elif change == "feature_off":
            env.on = False
        elif change == "owner_removed":
            env.allowed.discard(OWNER)
        elif change == "workspace_denied":
            monkeypatch.setattr(surface, "workspace_admitted", lambda team: False)
        else:

            async def denied(_channel: str) -> bool:
                return False

            monkeypatch.setattr(identity, "channel_inbound_permitted", denied)

    monkeypatch.setattr("kiro_crew.dashboard.state.load_window_off_loop", load_and_close)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert env.switches.calls == [] and slot.agent == "kirocrew-conductor"
    assert env.slack.ephemerals == [(CH, OWNER, agent_picker.NOT_ALLOWED_TEXT)]
    assert _rows(env, "denied")[-1]["error"] == "the code channel no longer admits the bot"


def test_a_cancel_before_the_commit_puts_the_select_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)

    async def cancelled(_slot: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("kiro_crew.dashboard.state.load_window_off_loop", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert env.switches.calls == []
    assert _initial(env.slack.edits[-1][3]) == "kirocrew-conductor"


def test_a_typed_name_that_cannot_be_an_agent_is_not_echoed_or_audited(env: Env) -> None:
    _bind(env)
    handled, replies = _command(env, "agent foo=x`bar")
    assert handled and len(replies) == 1
    assert "foo=x" not in replies[0] and agent_picker.INVALID_NAME in replies[0]
    [row] = _rows(env, "denied")
    assert "foo=x" not in row["resources"]
    assert f"agent={agent_picker.INVALID_NAME}" in row["resources"]


def test_a_rebind_while_the_window_loads_points_at_the_live_kickoff(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _bind(env)

    async def load_and_rebind(_slot: Any) -> None:
        current = env.store.get_binding(CH)
        assert current is not None
        env.store.replace_binding(replace(current, session_key="chat-other-1", anchor_ts="2.0"))

    monkeypatch.setattr("kiro_crew.dashboard.state.load_window_off_loop", load_and_rebind)
    handled, replies = _command(env, "agent builder")
    assert handled and replies == [agent_picker.STALE_PICKER_TEXT.format(command="kirocrew")]
    assert env.switches.calls == [] and slot.agent == "kirocrew-conductor"
    assert _rows(env, "denied")[-1]["error"] == agent_picker.REBOUND


def test_an_agent_name_with_an_underscore_is_audited_by_name(env: Env) -> None:
    _bind(env)
    handled, replies = _command(env, "agent code_reviewr")
    assert handled and "`code_reviewr`" in replies[0]
    assert "agent=code_reviewr" in _rows(env, "denied")[-1]["resources"]


@pytest.mark.parametrize(
    "change",
    [
        "activation_off",
        "archived",
        "archive_finished",
        "dropped",
        "rebound",
        "feature_off",
        "owner_removed",
        "workspace_denied",
    ],
)
def test_a_gate_that_closes_inside_the_switch_refuses_its_commit(
    env: Env, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The synchronous gates ride into switch_slot_agent as still_permitted."""
    from kiro_crew.config.loader import ACTIVATION_OFF

    slot = _bind(env)

    def close() -> None:
        if change == "activation_off":
            env.activation[CH] = ACTIVATION_OFF
        elif change == "archived":
            surface._archived_here.add(CH)
        elif change == "archive_finished":
            # wrapup stores the stamp, then lifts the in-process fence.
            env.store.update_binding(CH, archived_at=1.0)
            surface._archived_here.discard(CH)
        elif change == "dropped":
            env.store.drop_binding(CH)
        elif change == "rebound":
            current = env.store.get_binding(CH)
            assert current is not None
            env.store.replace_binding(replace(current, session_key="chat-other-1"))
        elif change == "feature_off":
            env.on = False
        elif change == "owner_removed":
            env.allowed.discard(OWNER)
        else:
            monkeypatch.setattr(surface, "workspace_admitted", lambda team: False)

    env.switches.before_commit = close
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "kirocrew-conductor"
    assert env.slack.ephemerals == [(CH, OWNER, agent_picker.NOT_ALLOWED_TEXT)]
    row = _rows(env, "denied")[-1]
    assert "code=caller_not_permitted" in row["resources"]
    assert _initial(env.slack.edits[-1][3]) == "kirocrew-conductor"


def _refuse_channel(monkeypatch: pytest.MonkeyPatch, *, after: list[bool] | None = None) -> None:
    from kiro_crew.slack import channel_policy

    def verdict(channel: str) -> str:
        if channel == CH and (after is None or after[0]):
            return "slack channel refused by the security policy"
        return ""

    monkeypatch.setattr(channel_policy, "_verdict", verdict)


def test_a_refused_channels_agent_pick_switches_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-channel ceiling (allowed_channel_ids), in the dispatcher and in authorize."""
    from kiro_crew.slack import interactions

    slot = _bind(env)
    _refuse_channel(monkeypatch)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    handled, replies = _command(env, "agent builder")
    assert handled and replies == []  # dropped without a reply at the inbound gate
    assert env.switches.calls == [] and slot.agent == "kirocrew-conductor"
    monkeypatch.setattr(interactions, "_orch", env.orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: u in env.allowed)
    asyncio.run(interactions.dispatch(_pick("builder")))
    assert env.switches.calls == []


def test_a_channel_refused_mid_switch_is_not_switched(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy narrows inside the switch: still_permitted refuses at the commit."""
    slot = _bind(env)
    flag = [False]
    _refuse_channel(monkeypatch, after=flag)
    env.switches.before_commit = lambda: flag.__setitem__(0, True)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "kirocrew-conductor"
    assert "code=caller_not_permitted" in _rows(env, "denied")[-1]["resources"]


# ── Each per-channel policy guard, alone ──────────────────────────────────


def test_authorize_refuses_a_policy_refused_channel_silently(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """authorize's own guard: refused, audited, and "" so nothing is posted."""
    _bind(env)
    binding = env.store.get_binding(CH)
    assert asyncio.run(agent_picker.authorize(env.orch, binding, OWNER, "T1", "r")) is None
    _refuse_channel(monkeypatch)
    assert asyncio.run(agent_picker.authorize(env.orch, binding, OWNER, "T1", "r")) == ""
    assert _rows(env, "denied")[-1]["error"] == "channels governance policy"


def test_the_commit_gates_close_on_a_policy_refused_channel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_sync_gates' own guard: the synchronous commit fence answers False."""
    _bind(env)
    binding = env.store.get_binding(CH)
    assert agent_picker._sync_gates(env.orch, binding, OWNER, "T1") is True
    _refuse_channel(monkeypatch)
    assert agent_picker._sync_gates(env.orch, binding, OWNER, "T1") is False


def test_the_dispatcher_drops_a_refused_channels_pick_before_the_picker(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher's agent-select guard, alone: the picker is never called."""
    _bind(env)
    called: list[Any] = []

    async def record(*a: Any, **_k: Any) -> bool:
        called.append(a)
        return True

    async def permitted(_channel: str) -> bool:
        return True

    monkeypatch.setattr(interactions, "_orch", env.orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: u in env.allowed)
    monkeypatch.setattr(interactions, "channel_inbound_permitted", permitted)
    monkeypatch.setattr(agent_picker, "handle_select", record)
    _refuse_channel(monkeypatch)
    asyncio.run(interactions.dispatch(_pick("builder")))
    assert called == []


def test_a_refused_channels_pick_posts_nothing_in_the_channel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the dispatcher, a policy refusal writes nothing into the channel."""
    slot = _bind(env)
    _refuse_channel(monkeypatch)
    assert asyncio.run(agent_picker.handle_select(env.orch, _pick("builder")))
    assert slot.agent == "kirocrew-conductor"
    assert env.slack.ephemerals == [] and env.slack.edits == []


def test_kirocrew_agent_on_a_refused_channel_switches_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _bind(env)
    _refuse_channel(monkeypatch)
    _command(env, "agent builder")
    assert env.switches.calls == [] and slot.agent == "kirocrew-conductor"


def test_kirocrew_agent_refused_mid_switch_is_not_switched(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command's switch commits through the same fence as the select."""
    slot = _bind(env)
    flag = [False]
    _refuse_channel(monkeypatch, after=flag)
    env.switches.before_commit = lambda: flag.__setitem__(0, True)
    _command(env, "agent builder")
    assert slot.agent == "kirocrew-conductor"
    assert "code=caller_not_permitted" in _rows(env, "denied")[-1]["resources"]


def test_the_switch_row_goes_through_the_display_sink(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard row a switch writes passes the shared display sink: a
    secret-shaped string in it is redacted, with no Slack mention defang."""
    from kiro_crew import agent_switch_command

    slot = _bind(env)
    key = "AKIA" + "QWERTYUIOPASDFGH"

    async def loaded(_slot: Any) -> bool:
        return True

    monkeypatch.setattr(agent_picker, "_load_window", loaded)
    monkeypatch.setattr(
        agent_switch_command,
        "switch_announcement",
        lambda a, w: f"@{a} switched {key} AKIA**ZXCVBNMASDFGHJKL** {w}",
    )
    asyncio.run(agent_picker._announce(env.state, slot, "builder", ""))
    [row] = env.transcript
    assert key not in row and "ZXCVBNMASDFGHJKL" not in row and "[REDACTED" in row
    assert "@builder" in row  # a transcript row: no Slack mention defang

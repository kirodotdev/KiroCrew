# SPDX-License-Identifier: Apache-2.0
"""A Bot's worker opened as one of its owner-listed agent templates.

The permission half (:func:`narrow_to_bot`) is pure and pinned here directly: a
template with broader pre-approvals gets nothing the Bot's own worker does not
hold, and a narrower template stays narrow. The hand-off half is driven through
the real config, resolver and ``create_session``, with the Bot fixture of
``test_mate_worker_dispatch``.
"""

from __future__ import annotations

import json

import pytest
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401

from kiro_crew import mate_worker_delegates as delegates
from kiro_crew.config import loader
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT

# ── the intersection ───────────────────────────────────────────────────────


def _bot(**extra):
    """The Bot's own worker spec: pre-approves reads and one shell command only."""
    return {
        "name": MATE_WORKER_AGENT,
        "tools": ["*"],
        "allowedTools": ["fs_read", "@kirocrew-work/work_brief", "@kirocrew-work/work_report"],
        "excludedTools": ["@builder-mcp/TicketingWriteActions"],
        "toolsSettings": {
            "shell": {"allowedCommands": ["git status"], "deniedCommands": ["rm -rf .*"]}
        },
        "permissions": {
            "rules": [
                {"capability": "web_fetch", "effect": "ask"},
                {"capability": "fs_read", "effect": "allow"},
            ]
        },
        **extra,
    }


def test_a_broader_template_gets_nothing_the_bot_forbids():
    template = {
        "name": "jarvis",
        "prompt": "I am Jarvis.",
        "tools": ["fs_read", "fs_write", "shell", "@builder-mcp"],
        "allowedTools": ["fs_read", "fs_write", "shell", "@builder-mcp"],
        "mcpServers": {"builder-mcp": {"command": "b", "autoApprove": ["InternalSearch"]}},
        "toolsSettings": {
            "shell": {"allowedCommands": ["git status", "brazil-build", "rm -rf /tmp/x"]}
        },
    }
    out = delegates.narrow_to_bot(template, _bot())

    # Pre-approvals: only what the Bot's worker also pre-approves.
    assert out["allowedTools"] == ["fs_read"]
    assert out["mcpServers"]["builder-mcp"]["autoApprove"] == []
    # Allow lists narrowed to the Bot's own; the Bot's denies added.
    assert out["toolsSettings"]["shell"]["allowedCommands"] == ["git status"]
    assert out["toolsSettings"]["shell"]["deniedCommands"] == ["rm -rf .*"]
    # The Bot's exclusions and asks hold; its allows are not copied in.
    assert "@builder-mcp/TicketingWriteActions" in out["excludedTools"]
    assert out["permissions"]["rules"] == [{"capability": "web_fetch", "effect": "ask"}]
    # The template's own identity is untouched, and nothing was added to its tools.
    assert out["prompt"] == "I am Jarvis."
    assert out["tools"] == template["tools"]
    # Pure: the caller's spec is not mutated.
    assert template["allowedTools"] == ["fs_read", "fs_write", "shell", "@builder-mcp"]


def test_a_narrower_template_stays_narrow_under_a_permissive_bot():
    permissive = _bot(allowedTools=["*"], excludedTools=[], toolsSettings={}, permissions={})
    template = {
        "name": "monica",
        "tools": ["fs_read"],
        "allowedTools": ["fs_read"],
        "excludedTools": ["fs_write"],
        "toolsSettings": {"shell": {"deniedCommands": ["git push.*"]}},
    }
    out = delegates.narrow_to_bot(template, permissive)
    assert out["tools"] == ["fs_read"]
    assert out["allowedTools"] == ["fs_read"]
    assert out["excludedTools"] == ["fs_write"]
    assert out["toolsSettings"] == {"shell": {"deniedCommands": ["git push.*"]}}
    assert "permissions" not in out


def test_a_server_grant_covers_its_tools_and_a_glob_covers_its_matches():
    bot = _bot(allowedTools=["@builder-mcp", "@slack-mcp/get_*"])
    template = {
        "allowedTools": [
            "@builder-mcp/InternalSearch",
            "@slack-mcp/get_thread",
            "@slack-mcp/post_message",
        ]
    }
    out = delegates.narrow_to_bot(template, bot)
    assert out["allowedTools"] == ["@builder-mcp/InternalSearch", "@slack-mcp/get_thread"]


def test_the_work_ledger_is_the_one_addition_and_only_its_bot_granted_verbs_auto_approve():
    out = delegates.with_work_ledger({"tools": ["fs_read"], "allowedTools": []}, _bot())
    assert "kirocrew-work" in out["mcpServers"]
    assert out["tools"] == ["fs_read", "@kirocrew-work"]
    assert out["allowedTools"] == ["@kirocrew-work/work_brief", "@kirocrew-work/work_report"]
    bare = delegates.with_work_ledger({"tools": []}, _bot(allowedTools=[]))
    assert bare["allowedTools"] == []


# ── the list ───────────────────────────────────────────────────────────────


def _config(worker_agents):
    record = type("R", (), {"worker_agents": worker_agents})()
    return type("C", (), {"agents": {"pixel": record, "ada": record}})()


@pytest.mark.parametrize("requested", ["", MATE_WORKER_AGENT, "kirocrew-worker", "pixel", "jarvis"])
def test_no_list_keeps_todays_hand_off(requested):
    assert delegates.delegate_for(_config([]), "pixel", requested) == ""


def test_a_listed_name_is_the_delegate_and_an_unlisted_one_is_refused():
    config = _config(["jarvis", "tony"])
    assert delegates.delegate_for(config, "pixel", " jarvis ") == "jarvis"
    assert delegates.delegate_for(config, "pixel", "") == ""
    assert delegates.delegate_for(config, "pixel", MATE_WORKER_AGENT) == ""
    # Another Bot keeps its own (peer) resolution.
    assert delegates.delegate_for(config, "pixel", "ada") == ""
    with pytest.raises(delegates.WorkerAgentNotAllowed, match="jarvis, tony"):
        delegates.delegate_for(config, "pixel", "howard")


@pytest.mark.parametrize(
    "raw, ok",
    [
        ([], True),
        (["jarvis", "tony"], True),
        ("jarvis", False),
        ([1], False),
        ([""], False),
        ([MATE_CONDUCTOR_AGENT], False),
        (["x"] * (delegates.WORKER_AGENTS_MAX + 1), False),
    ],
)
def test_the_owner_route_validates_the_list(raw, ok):
    assert (delegates.worker_agents_refusal(raw) is None) is ok


def test_the_loader_keeps_only_trimmed_unique_names():
    from kiro_crew.config.sections import coerce_worker_agents

    assert coerce_worker_agents([" jarvis", "jarvis", "", 3, "tony"]) == ["jarvis", "tony"]
    assert coerce_worker_agents("jarvis") == []


# ── the record ─────────────────────────────────────────────────────────────


def _record(**kw):
    base = dict(
        member_id="m1",
        store=MemoryStoreRef("member-m1", "m1"),
        selection_kind="template",
        template_id="jarvis",
    )
    return ExecutionContext(**{**base, **kw})


def test_the_record_names_its_template_and_round_trips():
    from kiro_crew.execution_context import EXECUTION_CONTEXT_KEY, execution_from_record

    record = _record(worker_agent="jarvis")
    back = execution_from_record({EXECUTION_CONTEXT_KEY: record.to_record()})
    assert back == record
    assert delegates.delegate_worker_agent(back) == "jarvis"
    assert delegates.is_bot_worker_execution(back)
    # A record written before the field reads as no delegate.
    old = {k: v for k, v in record.to_record().items() if k != "worker_agent"}
    assert execution_from_record({EXECUTION_CONTEXT_KEY: old}).worker_agent == ""


@pytest.mark.parametrize(
    "kw",
    [
        {"worker_agent": "tony"},  # not its own template
        {"worker_agent": "jarvis", "member_id": None, "store": MemoryStoreRef("default")},
    ],
)
def test_a_delegate_record_must_name_its_bot_and_its_own_template(kw):
    with pytest.raises(Exception, match="delegate worker"):
        _record(**kw)


def test_a_delegates_own_template_child_is_not_a_delegate():
    child = _record(worker_agent="jarvis").with_template("reviewer", "reviewer")
    assert child.worker_agent == ""
    assert not delegates.is_bot_worker_execution(child)


def test_a_delegate_runs_under_its_bots_profile_permissions(monkeypatch):
    from kiro_crew.dashboard import crewmate_approval

    config = type("C", (), {"agents": {"pixel": object()}})()
    monkeypatch.setattr(
        "kiro_crew.execution_context.member_config_for_id", lambda cfg, mid: ("pixel", None)
    )
    assert crewmate_approval.crewmate_name_for(config, _record(worker_agent="jarvis")) == "pixel"
    # A plain template child on the Bot's store is not a profile worker.
    assert crewmate_approval.crewmate_name_for(config, _record()) == ""


def test_a_bound_item_is_attributed_from_the_record_not_the_ledger(monkeypatch):
    records = {"dashboard:chat-1": _record(worker_agent="jarvis"), "dashboard:chat-2": _record()}
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda key: records.get(key)
    )
    assert delegates.bound_worker_agent("chat-1") == "jarvis"
    assert delegates.bound_worker_agent("chat-2") == ""
    assert delegates.bound_worker_agent(None) == ""


def test_the_session_spec_is_refused_when_the_bots_limits_cannot_be_read(monkeypatch):
    from kiro_crew.mate_worker import WorkerKitUnreadable

    monkeypatch.setattr(delegates, "session_delegate", lambda agent, key: True)

    def unreadable(_key, **_host):
        raise WorkerKitUnreadable("no limits")

    monkeypatch.setattr(delegates, "bot_worker_spec", unreadable)
    with pytest.raises(WorkerKitUnreadable):
        delegates.delegate_session_spec({"name": "jarvis"}, "jarvis", "dashboard:chat-1")
    # Not a delegate: the spec passes through untouched, nothing is read.
    monkeypatch.setattr(delegates, "session_delegate", lambda agent, key: False)
    spec = {"name": "jarvis", "allowedTools": ["*"]}
    assert delegates.delegate_session_spec(spec, "jarvis", "dashboard:chat-1") is spec


# ── through session_create ─────────────────────────────────────────────────


def _list(tmp_path, monkeypatch, names):
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["worker_agents"] = names
    path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    monkeypatch.setattr(
        loader,
        "_MATERIALIZED_AGENTS",
        frozenset({MATE_WORKER_AGENT, MATE_CONDUCTOR_AGENT, "pixel", "jarvis", "howard"}),
    )


def test_a_listed_template_opens_the_worker_as_that_template(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.members import crewmate_worker_member_id

    state, caller, execution = crewmate
    _list(tmp_path, monkeypatch, ["jarvis"])
    result = _create(state, caller, agent="jarvis")

    child = state.get_slot(result["target"])
    assert child.agent == "jarvis"
    assert child.workspace == "member-pixel"
    actual = read_session_execution(slot_history_key(child), required=True)
    assert actual.store == execution.store
    assert actual.member_id == "pixel"
    assert (actual.selection_kind, actual.template_id, actual.worker_agent) == (
        "template",
        "jarvis",
        "jarvis",
    )
    # A Bot worker: counted toward its cap and routed to its harness.
    assert crewmate_worker_member_id(slot_history_key(child)) == "pixel"


def test_an_unlisted_template_is_refused_not_run_as_the_worker(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    state, caller, _execution = crewmate
    _list(tmp_path, monkeypatch, ["jarvis"])
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller, agent="howard", agent_kind="template")
    assert refused.value.code == "worker_agent_not_allowed"
    assert "jarvis" in str(refused.value)
    assert set(state._slots) == before


def test_with_a_list_set_no_agent_still_opens_the_bots_own_worker(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    state, caller, _execution = crewmate
    _list(tmp_path, monkeypatch, ["jarvis"])
    child = state.get_slot(_create(state, caller)["target"])
    assert child.agent == MATE_WORKER_AGENT


def test_delegates_count_toward_the_bots_worker_cap(crewmate, tmp_path, monkeypatch):  # noqa: F811
    state, caller, _execution = crewmate
    _list(tmp_path, monkeypatch, ["jarvis"])
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["max_workers"] = 1
    path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    _create(state, caller, agent="jarvis")
    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)
    assert refused.value.code == "bot_worker_cap_reached"

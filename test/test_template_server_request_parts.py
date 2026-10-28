"""The read helpers of the ``request_server`` handler, each on its own.

``test_template_server_request.py`` drives the routes end to end with these
helpers replaced; here each one runs for real against stubbed owners, so the
reads the card is built from (caller template, backend, reach, grants, tools,
live apply, declared servers, rebuild verdicts) are pinned too.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import template_mcp_additions as additions
from kiro_crew.dashboard.handlers import template_server_request as tsr


class _State:
    def __init__(self, slots: dict[str, Any] | None = None, sessions: Any = None) -> None:
        self._slots = slots or {}
        self.sessions = sessions

    def get_slot(self, key: str) -> Any:
        return self._slots.get(key)


@pytest.fixture
def record(tmp_path, monkeypatch):
    monkeypatch.setattr(additions, "config_dir", lambda: tmp_path)
    return tmp_path


def _execution(monkeypatch, by_key: dict[str, Any]) -> None:
    from kiro_crew import execution_context
    from kiro_crew.dashboard import chat_utils

    monkeypatch.setattr(chat_utils, "slot_history_key", lambda slot: slot.key)

    def read(key):
        value = by_key.get(key)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(template_id=value) if value else None

    monkeypatch.setattr(execution_context, "read_session_execution", read)


def _not_crewmate(monkeypatch, *, thread=False, worker=False, mate_owned=False) -> None:
    from kiro_crew import mate_worker, members
    from kiro_crew.dashboard import mate_thread_wakes

    monkeypatch.setattr(mate_thread_wakes, "is_crewmate_thread", lambda slot: thread)
    monkeypatch.setattr(members, "is_crewmate_worker_session", lambda key: worker)
    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: mate_owned)


def _slot(key: str, title: str = "") -> SimpleNamespace:
    return SimpleNamespace(key=key, title=title)


# ── _caller_template ──


def test_the_caller_template_comes_from_the_session_record(monkeypatch):
    _not_crewmate(monkeypatch)
    _execution(monkeypatch, {"s1": "kirocrew-conductor"})
    state = _State({"s1": _slot("s1")})
    assert tsr._caller_template(state, "s1", "sk") == ("kirocrew-conductor", "", "")


def test_a_crewmate_thread_or_worker_is_sent_elsewhere(monkeypatch):
    _execution(monkeypatch, {"s1": "kirocrew-conductor"})
    state = _State({"s1": _slot("s1")})
    _not_crewmate(monkeypatch, thread=True)
    assert tsr._caller_template(state, "s1", "sk")[1] == "crewmate_caller"
    _not_crewmate(monkeypatch, worker=True)
    assert tsr._caller_template(state, "s1", "sk")[1] == "crewmate_caller"
    _not_crewmate(monkeypatch, mate_owned=True)
    assert tsr._caller_template(state, "s1", "sk")[1] == "crewmate_caller"


def test_a_session_with_no_readable_template_names_none(monkeypatch):
    _not_crewmate(monkeypatch)
    _execution(monkeypatch, {"s1": OSError("unreadable"), "s2": ""})
    state = _State({"s1": _slot("s1"), "s2": _slot("s2")})
    assert tsr._caller_template(state, "s1", "sk")[1] == "no_template"
    assert tsr._caller_template(state, "s2", "sk")[1] == "no_template"


# ── _caller_backend ──


def test_a_live_provider_names_its_own_backend():
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    caps = capabilities_for("claude")
    assert caps.backend == "claude"
    sessions = SimpleNamespace(get_provider=lambda sk: SimpleNamespace(capabilities=caps))
    assert asyncio.run(tsr._caller_backend(_State(sessions=sessions), "sk")) == "claude"


def test_with_no_provider_the_member_selector_answers(monkeypatch):
    from kiro_crew import crewmate_harness, members
    from kiro_crew.config import loader

    cfg = SimpleNamespace(agent=SimpleNamespace(member_acp_backend={}, acp_backend="kas"))
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    pin = object()
    monkeypatch.setattr(crewmate_harness, "crewmate_backend_pin", lambda config, key: pin)
    seen: list[Any] = []

    def select(sk, member, default, *, crew_backend=None):
        seen.append(crew_backend)
        return default

    monkeypatch.setattr(members, "select_provider_backend", select)
    sessions = SimpleNamespace(get_provider=lambda sk: None)
    assert asyncio.run(tsr._caller_backend(_State(sessions=sessions), "sk")) == "kas"
    # The crewmate pin check (H17) runs in the call's own arguments.
    assert seen == [pin]


def test_an_unreadable_backend_is_the_default_harness():
    def boom(sk):
        raise RuntimeError("no registry")

    sessions = SimpleNamespace(get_provider=boom)
    assert asyncio.run(tsr._caller_backend(_State(sessions=sessions), "sk")) == ""


# ── addition_reach_view ──


def test_the_reach_lists_templates_sessions_and_members(monkeypatch):
    from kiro_crew.config import loader

    _execution(
        monkeypatch,
        {
            "a": "kirocrew-conductor",
            "b": "kirocrew",
            "c": OSError("gone"),
            "d": "kirocrew-ledger-conductor",
        },
    )
    slots = {k: _slot(k, title=f"chat {k}") for k in ("a", "b", "c", "d")}
    agents = {
        "patrol": SimpleNamespace(display_name="Patrol", kiro_agent="kirocrew-conductor"),
        "other": SimpleNamespace(display_name="", kiro_agent="kirocrew"),
    }
    monkeypatch.setattr(
        loader.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(agents=agents))
    )
    view = tsr.addition_reach_view(_State(slots), "kirocrew-conductor")
    assert view["templates"] == ["kirocrew-conductor", "kirocrew-ledger-conductor"]
    assert view["aliases"] == {"kirocrew-ledger-conductor": "kirocrew-conductor"}
    assert view["sessions"] == ["chat a", "chat d"]
    assert view["members"] == ["Patrol"]


def test_an_unreadable_config_still_lists_templates_and_sessions(monkeypatch):
    from kiro_crew.config import loader

    _execution(monkeypatch, {})

    def boom(cls):
        raise OSError("config unreadable")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(boom))
    view = tsr.addition_reach_view(_State(), "kirocrew-conductor")
    assert view["members"] == [] and view["sessions"] == []
    assert view["templates"][0] == "kirocrew-conductor"


# ── existing_grants / template_declares ──


def test_an_unreadable_spec_names_no_grant(monkeypatch):
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    def boom(name):
        raise OSError("unreadable")

    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", boom)
    assert tsr.existing_grants("kirocrew-conductor", "files") == []


def test_a_server_the_template_itself_declares_is_found(record, monkeypatch):
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    spec = {"mcpServers": {"files": {"command": "x"}}}
    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", lambda name: [("p", spec)])
    assert tsr.template_declares("kirocrew-conductor", "files") is True
    assert tsr.template_declares("kirocrew-conductor", "other") is False


def test_a_server_from_the_record_is_not_the_template_s_own(record, monkeypatch):
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    additions.add_addition(
        "kirocrew-conductor", "files", {"command": "x"}, approved_by="u", card_id="c"
    )
    spec = {"mcpServers": {"files": {"command": "x"}}}
    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", lambda name: [("p", spec)])
    assert tsr.template_declares("kirocrew-conductor", "files") is False


def test_an_unreadable_template_declares_nothing(record, monkeypatch):
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    def boom(name):
        raise OSError("unreadable")

    monkeypatch.setattr(agents_handlers, "_agent_detail_candidates", boom)
    assert tsr.template_declares("kirocrew-conductor", "files") is False


# ── server_tools / live_apply ──


def test_server_tools_reads_the_last_probe(monkeypatch):
    from kiro_crew import mcp_discovery as probe

    monkeypatch.setattr(probe, "probe_metadata", lambda name: SimpleNamespace(tools=["a", "b", 3]))
    assert tsr.server_tools("files") == ["a", "b"]

    def boom(name):
        raise OSError("no cache")

    monkeypatch.setattr(probe, "probe_metadata", boom)
    assert tsr.server_tools("files") == []


def test_live_apply_asks_every_live_process_and_fails_closed(monkeypatch):
    from kiro_crew import mcp_hot_reload

    seen: list[list[Any]] = []

    def live(providers):
        seen.append(list(providers))
        return True

    monkeypatch.setattr(mcp_hot_reload, "live_sessions_apply_mcp_live", live)
    sessions = SimpleNamespace(active_providers=lambda: ["p1"], warm_providers=lambda: ["w1"])
    assert tsr.live_apply(_State(sessions=sessions)) is True
    assert seen == [["p1", "w1"]]
    assert tsr.live_apply(_State(sessions=None)) is False


# ── rebuild verdicts ──


def _rebuild(monkeypatch, *, wrote=True, held=False, raises=False) -> None:
    from kiro_crew import agent

    def rebuild(*, _held_out=None):
        if raises:
            raise RuntimeError("rebuild failed")
        if _held_out is not None:
            _held_out.append(held)
        return "p", wrote

    monkeypatch.setattr(agent, "rebuild_agent_config_reporting", rebuild)


def test_a_rebuild_counts_only_when_every_spec_was_written(monkeypatch):
    _rebuild(monkeypatch)
    assert tsr._rebuild_specs() is True
    _rebuild(monkeypatch, wrote=False)
    assert tsr._rebuild_specs() is False
    _rebuild(monkeypatch, held=True)
    assert tsr._rebuild_specs() is False
    _rebuild(monkeypatch, raises=True)
    assert tsr._rebuild_specs() is False


def test_only_a_conductor_needs_its_spec_rewritten(monkeypatch):
    _rebuild(monkeypatch, wrote=False)
    assert tsr._rebuild_required("kirocrew-conductor") is True
    assert tsr._rebuild_required("kirocrew-ledger-conductor") is True
    assert tsr._rebuild_required("aim-installed-agent") is False
    _rebuild(monkeypatch)
    assert tsr._rebuild_required("kirocrew-conductor") is False


def test_a_failed_reconcile_is_logged_not_raised(monkeypatch, caplog):
    _rebuild(monkeypatch, wrote=False)
    with caplog.at_level("WARNING"):
        tsr._reconcile_after_rollback()
    assert "next rebuild converges" in caplog.text
    _rebuild(monkeypatch)
    tsr._reconcile_after_rollback()


# ── _reach_sessions ──


def test_sessions_reset_only_when_the_change_cannot_apply_live(monkeypatch):
    from kiro_crew.dashboard.handlers import mcp, sessions

    async def reset(request):
        return 3

    monkeypatch.setattr(sessions, "_reset_all_sessions", reset)
    monkeypatch.setattr(mcp, "_mcp_hot_reload_active", lambda request: True)
    assert asyncio.run(tsr._reach_sessions(object())) == 0
    monkeypatch.setattr(mcp, "_mcp_hot_reload_active", lambda request: False)
    assert asyncio.run(tsr._reach_sessions(object())) == 3

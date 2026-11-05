"""A crew-bound spec Crew does not own starts on kiro-cli through a governed copy.

kiro-cli runs an agent's skill view, a Crew-owned copy the native projection writes,
and not the authored file. For a crew-bound spec that view carries only the grants
the governance ceiling permits (``agent_materialization.shared_template_view``), and
the start check judges the view it is handed, so a spec over the ceiling starts with
its denied grants removed. The authored file is never written, and a start that
would run it (no view) is still refused.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import native_skill_projection_helpers
import pytest

from kiro_crew import agent, agent_state
from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp_backends import ACP_BACKEND_KIRO
from kiro_crew.agent_materialization import auto_approve, fork_refresh, shared_template_view
from kiro_crew.config.loader import KiroCrewConfig

native_tree = native_skill_projection_helpers.native_tree
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)

#: A spec another tool installed: denied grants on both lists, odd formatting.
_RAW = (
    '{\n  "name": "pkg-reviewer",\n  "prompt": "review",\n'
    '  "tools": ["web_search", "web_fetch", "@gh"],\n'
    '  "allowedTools": ["web_search", "web_fetch"],\n'
    '  "mcpServers": {"gh": {"command": "gh-mcp", "autoApprove": ["*"]}}\n}\n'
)

_DENIED = {"web_fetch", "@gh"}


def _denied(ref: str) -> bool:
    return ref in _DENIED or ref.startswith("@gh")


@pytest.fixture(autouse=True)
def _fresh_reports(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(shared_template_view, "_REPORTED", set())


@pytest.fixture
def bound(native_tree, monkeypatch: pytest.MonkeyPatch):
    """``pkg-reviewer`` installed in the agents directory and bound to a crew."""
    from kiro_crew import mate_config_migration

    _home, agents, project = native_tree
    monkeypatch.setattr(agent_state, "all_fork_info", lambda: {})
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: None)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents)
    monkeypatch.setattr(agent, "agent_spec_path", lambda name: agents / f"{name}.json")
    monkeypatch.setattr(mate_config_migration, "moved_from_agents", lambda: frozenset())
    # The tightened ceiling, as the governance passes see it.
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: not _denied(ref))

    def strip(servers: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return {
            k: {kk: vv for kk, vv in v.items() if not (kk == "autoApprove" and _denied(f"@{k}"))}
            for k, v in servers.items()
        }

    monkeypatch.setattr(auto_approve, "_strip_ungoverned_auto_approve", strip)
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in ("pkg-reviewer", "loose")
            if (agents / f"{name}.json").exists()
        ],
    )
    spec = agents / "pkg-reviewer.json"
    spec.write_text(_RAW, encoding="utf-8")
    _bind(monkeypatch, {"reviewer-crew": "pkg-reviewer"})
    return spec, agents, project


def _bind(monkeypatch: pytest.MonkeyPatch, bound: dict[str, str], **flags: bool) -> None:
    agents = {crew: SimpleNamespace(kiro_agent=name) for crew, name in bound.items()}
    cfg = SimpleNamespace(
        agents=agents, agent=SimpleNamespace(native_skill_projection=True), **flags
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


def _view_file(agents: Path, prepared: Any, name: str = "pkg-reviewer") -> Path:
    return agents / f"{prepared.spawn_agent(name)}.json"


def _write_compliant_source(spec: Path) -> None:
    spec.write_text(
        json.dumps(
            {
                "name": "pkg-reviewer",
                "tools": ["web_search", "web_fetch", "@gh"],
                "allowedTools": ["web_search"],
                "mcpServers": {"gh": {"command": "gh-mcp"}},
            }
        ),
        encoding="utf-8",
    )


def test_an_over_ceiling_spec_starts_on_kiro_cli_through_its_governed_view(bound) -> None:
    spec, agents, project = bound
    prepared = projection.prepare_native_skill_projection(project)
    launch = prepared.spawn_agent("pkg-reviewer")
    assert launch.startswith("kirocrew-skill-view-")
    fork_refresh.require_shared_template_within_ceiling(
        "pkg-reviewer", ACP_BACKEND_KIRO, launch_name=launch
    )
    view = json.loads(_view_file(agents, prepared).read_text(encoding="utf-8"))
    assert view["allowedTools"] == ["web_search"]
    assert "autoApprove" not in view["mcpServers"]["gh"]
    # The denied tools stay mounted; only their auto-approval is gone.
    assert view["tools"][:3] == ["web_search", "web_fetch", "@gh"]


def test_a_start_on_the_authored_name_is_still_refused(bound) -> None:
    """No view (projection off, a busy lock) means kiro-cli reads the authored file."""
    spec, _agents, project = bound
    projection.prepare_native_skill_projection(project)
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="pkg-reviewer.json"):
        fork_refresh.require_shared_template_within_ceiling(
            "pkg-reviewer", ACP_BACKEND_KIRO, launch_name="pkg-reviewer"
        )


def test_the_authored_spec_is_byte_for_byte_unchanged(bound) -> None:
    spec, _agents, project = bound
    before = spec.read_bytes()
    prepared = projection.prepare_native_skill_projection(project)
    fork_refresh.require_shared_template_within_ceiling(
        "pkg-reviewer", ACP_BACKEND_KIRO, launch_name=prepared.spawn_agent("pkg-reviewer")
    )
    projection.prepare_native_skill_projection(project)
    assert spec.read_bytes() == before


def test_a_removed_grant_stays_removed_across_rebuilds(bound) -> None:
    _spec, agents, project = bound
    first = projection.prepare_native_skill_projection(project)
    second = projection.prepare_native_skill_projection(project)
    for prepared in (first, second):
        view = json.loads(_view_file(agents, prepared).read_text(encoding="utf-8"))
        assert "web_fetch" not in view["allowedTools"]
        assert "autoApprove" not in view["mcpServers"]["gh"]


def test_a_view_that_regained_a_denied_grant_is_refused(bound) -> None:
    """The check reads the view's own file, so a copy carrying a denied grant is refused."""
    _spec, agents, project = bound
    prepared = projection.prepare_native_skill_projection(project)
    path = _view_file(agents, prepared)
    view = json.loads(path.read_text(encoding="utf-8"))
    view["allowedTools"].append("web_fetch")
    path.write_text(json.dumps(view), encoding="utf-8")
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="skill view"):
        fork_refresh.require_shared_template_within_ceiling(
            "pkg-reviewer", ACP_BACKEND_KIRO, launch_name=prepared.spawn_agent("pkg-reviewer")
        )


def test_a_compliant_source_does_not_hide_a_denied_grant_in_its_launch_view(bound) -> None:
    spec, agents, project = bound
    _write_compliant_source(spec)
    prepared = projection.prepare_native_skill_projection(project)
    path = _view_file(agents, prepared)
    view = json.loads(path.read_text(encoding="utf-8"))
    view["allowedTools"].append("web_fetch")
    path.write_text(json.dumps(view), encoding="utf-8")

    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="skill view"):
        fork_refresh.require_shared_template_within_ceiling(
            "pkg-reviewer", ACP_BACKEND_KIRO, launch_name=prepared.spawn_agent("pkg-reviewer")
        )


def test_a_compliant_source_and_governed_launch_view_are_allowed(bound) -> None:
    spec, agents, project = bound
    _write_compliant_source(spec)
    prepared = projection.prepare_native_skill_projection(project)
    assert json.loads(_view_file(agents, prepared).read_text(encoding="utf-8"))["allowedTools"] == [
        "web_search"
    ]

    fork_refresh.require_shared_template_within_ceiling(
        "pkg-reviewer", ACP_BACKEND_KIRO, launch_name=prepared.spawn_agent("pkg-reviewer")
    )


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_a_missing_or_unparseable_view_is_refused(bound, content: str | None) -> None:
    _spec, agents, _project = bound
    name = "kirocrew-skill-view-" + "0" * 24
    if content is not None:
        (agents / f"{name}.json").write_text(content, encoding="utf-8")
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="skill view"):
        fork_refresh.require_shared_template_within_ceiling(
            "pkg-reviewer", ACP_BACKEND_KIRO, launch_name=name
        )


def test_the_view_follows_an_edit_of_the_source(bound) -> None:
    spec, agents, project = bound
    first = projection.prepare_native_skill_projection(project)
    spec.write_text(
        json.dumps(
            {
                "name": "pkg-reviewer",
                "tools": ["web_search", "web_fetch", "code"],
                "allowedTools": ["web_search", "code", "web_fetch"],
            }
        ),
        encoding="utf-8",
    )
    second = projection.prepare_native_skill_projection(project)
    assert second.spawn_agent("pkg-reviewer") != first.spawn_agent("pkg-reviewer")
    view = json.loads(_view_file(agents, second).read_text(encoding="utf-8"))
    assert view["allowedTools"] == ["web_search", "code"]
    fork_refresh.require_shared_template_within_ceiling(
        "pkg-reviewer", ACP_BACKEND_KIRO, launch_name=second.spawn_agent("pkg-reviewer")
    )


def test_the_view_follows_a_change_of_the_ceiling(bound, monkeypatch) -> None:
    _spec, agents, project = bound
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: ref != "web_search")
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads(_view_file(agents, prepared).read_text(encoding="utf-8"))
    assert view["allowedTools"] == ["web_fetch"]


def test_an_unbound_agent_s_view_keeps_its_grants(bound) -> None:
    """Only the names the start check governs are filtered."""
    _spec, agents, project = bound
    (agents / "loose.json").write_text(
        json.dumps({"name": "loose", "allowedTools": ["web_fetch"]}), encoding="utf-8"
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads(_view_file(agents, prepared, "loose").read_text(encoding="utf-8"))
    assert view["allowedTools"] == ["web_fetch"]


def test_bindings_that_cannot_be_read_whole_govern_every_view(bound, monkeypatch) -> None:
    _spec, agents, project = bound
    (agents / "loose.json").write_text(
        json.dumps({"name": "loose", "allowedTools": ["web_fetch"]}), encoding="utf-8"
    )
    _bind(monkeypatch, {}, _overlay_unreadable=True)
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads(_view_file(agents, prepared, "loose").read_text(encoding="utf-8"))
    assert view["allowedTools"] == []


def test_a_binding_on_the_file_stem_governs_the_declared_name(bound, monkeypatch) -> None:
    from kiro_crew.config import loader

    _bind(monkeypatch, {"reviewer-crew": "KiroPkg-reviewer"})
    monkeypatch.setattr(
        loader,
        "dispatch_kiro_agent",
        lambda name: "pkg-reviewer" if name == "KiroPkg-reviewer" else name,
    )
    assert shared_template_view.governed_view_names() == frozenset(
        {"KiroPkg-reviewer", "pkg-reviewer"}
    )


def test_a_moved_crewmate_s_agent_is_governed(bound, monkeypatch) -> None:
    from kiro_crew import mate_config_migration

    _bind(monkeypatch, {})
    monkeypatch.setattr(mate_config_migration, "moved_from_agents", lambda: {"old-agent"})
    assert shared_template_view.governed_view_names() == frozenset({"old-agent"})


def test_an_unreadable_ledger_governs_every_view(bound, monkeypatch) -> None:
    from kiro_crew import mate_config_migration

    def unreadable() -> frozenset[str]:
        raise mate_config_migration.LedgerUnreadable("unreadable")

    monkeypatch.setattr(mate_config_migration, "moved_from_agents", unreadable)
    assert shared_template_view.governed_view_names() is None


def test_a_config_that_will_not_load_governs_every_view(monkeypatch) -> None:
    def boom(cls: object) -> None:
        raise OSError("config.json unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
    assert shared_template_view.governed_view_names() is None


def test_the_removal_is_reported_once_and_audited(bound, monkeypatch, caplog) -> None:
    _spec, _agents, project = bound
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agent, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    )
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        projection.prepare_native_skill_projection(project)
        projection.prepare_native_skill_projection(project)
    lines = [r.getMessage() for r in caplog.records if "pkg-reviewer" in r.getMessage()]
    assert len(lines) == 1
    assert "web_fetch" in lines[0] and "@gh autoApprove '*'" in lines[0]
    assert "unchanged" in lines[0]
    withheld = [e for e in events if e.get("operation") == shared_template_view.WITHHELD_OP]
    assert len(withheld) == 1
    assert withheld[0]["source"] == "shared-template-view:pkg-reviewer"


def test_a_failed_audit_does_not_break_the_view(bound, monkeypatch) -> None:
    _spec, agents, project = bound

    def broken() -> None:
        raise RuntimeError("no audit log")

    monkeypatch.setattr(agent, "sel", broken)
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads(_view_file(agents, prepared).read_text(encoding="utf-8"))
    assert view["allowedTools"] == ["web_search"]


def test_the_report_memo_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(shared_template_view, "_REPORTED_MAX", 2)
    for n in range(5):
        shared_template_view._report(f"a{n}", frozenset({("allowedTools", "'x'")}))
    assert len(shared_template_view._REPORTED) <= 2


def test_the_report_memo_retains_only_fixed_size_digests(monkeypatch, caplog) -> None:
    monkeypatch.setattr(agent, "sel", lambda: SimpleNamespace(log_api_access=lambda **_kw: None))
    removed = frozenset({("allowedTools", "x" * 100_000)})

    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        shared_template_view._report("large-agent", removed)
        shared_template_view._report("large-agent", removed)

    assert len(shared_template_view._REPORTED) == 1
    digest = next(iter(shared_template_view._REPORTED))
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    lines = [r.getMessage() for r in caplog.records if "large-agent" in r.getMessage()]
    assert len(lines) == 1


def test_govern_view_never_adds_a_grant(bound) -> None:
    view = {"allowedTools": ["web_search"], "mcpServers": {"ok": {"command": "x"}}}
    removed = shared_template_view.govern_shared_template_view("pkg-reviewer", view)
    assert removed == frozenset()
    assert view == {"allowedTools": ["web_search"], "mcpServers": {"ok": {"command": "x"}}}


def _broker_entry() -> dict[str, Any]:
    return {
        "name": "gh",
        "command": "broker",
        "args": ["--stdio"],
        "autoApprove": ["*"],
        "custom": {"kept": True},
    }


def test_a_governed_agent_s_injected_entry_loses_denied_auto_approve(bound) -> None:
    entry = _broker_entry()

    assert shared_template_view.govern_shared_template_injected_servers(
        "pkg-reviewer", [entry]
    ) == [
        {
            "name": "gh",
            "command": "broker",
            "args": ["--stdio"],
            "custom": {"kept": True},
        }
    ]


def test_an_injected_removal_is_reported_once_and_audited(bound, monkeypatch) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agent, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    )

    for _ in range(2):
        shared_template_view.govern_shared_template_injected_servers(
            "pkg-reviewer", [_broker_entry()]
        )

    assert len(events) == 1
    assert events[0]["operation"] == shared_template_view.WITHHELD_OP
    assert events[0]["source"] == "shared-template-view:pkg-reviewer"


def test_an_unbound_agent_s_injected_entries_are_unchanged(bound) -> None:
    entries = [_broker_entry()]

    assert shared_template_view.govern_shared_template_injected_servers("loose", entries) is entries


def test_unknown_bindings_govern_every_injected_entry(bound, monkeypatch) -> None:
    monkeypatch.setattr(shared_template_view, "governed_view_names", lambda: None)

    (entry,) = shared_template_view.govern_shared_template_injected_servers(
        "loose", [_broker_entry()]
    )

    assert "autoApprove" not in entry


def test_pooled_session_servers_governs_entries_once(bound, monkeypatch) -> None:
    from kiro_crew.mcp_gateway import session_servers

    monkeypatch.setattr(
        session_servers,
        "_load_overlay_for_agent",
        lambda *_args, **_kwargs: {"mcpServers": {"gh": {session_servers._WRAPPER_MARKER: True}}},
    )
    monkeypatch.setattr(session_servers, "_registry_ceiling_exemptions", lambda: None)
    monkeypatch.setattr(session_servers, "is_unpoolable", lambda _name: False)
    monkeypatch.setattr(
        session_servers,
        "_acp_server_entry",
        lambda _name, _entry, _channel_id: _broker_entry(),
    )

    assert session_servers.pooled_session_servers(bound[2], "pkg-reviewer") == [
        {
            "name": "gh",
            "command": "broker",
            "args": ["--stdio"],
            "custom": {"kept": True},
        }
    ]


def test_runtime_pooled_entries_are_returned(bound, monkeypatch) -> None:
    from kiro_crew.acp import runtime, runtime_session_setup

    expected = [_broker_entry()]
    expected[0].pop("autoApprove")
    monkeypatch.setattr(runtime, "pooled_session_servers", lambda *_args, **_kwargs: expected)

    servers, ref_spec = runtime_session_setup._pooled_session_servers_and_ref_spec(
        None,
        "pkg-reviewer",
        ACP_BACKEND_KIRO,
        bound[2],
        ref_spec={"name": "pkg-reviewer"},
    )

    assert ref_spec == {"name": "pkg-reviewer"}
    assert servers is expected


def test_direct_client_pooled_entries_are_attached(bound, monkeypatch) -> None:
    from kiro_crew.acp import client, client_mcp_array

    expected = _broker_entry()
    expected.pop("autoApprove")
    monkeypatch.setattr(client, "pooled_session_servers", lambda *_args, **_kwargs: [expected])
    client_instance: Any = SimpleNamespace(
        _agent="pkg-reviewer",
        _channel_id="",
        _mcp_gateway_overlay=None,
        _stub_session_token="session-token",
        _work_dir=bound[2],
        backend=ACP_BACKEND_KIRO,
    )

    (server,) = client_mcp_array._pooled_broker_stubs(client_instance)

    assert "autoApprove" not in server
    assert server["custom"] == {"kept": True}
    assert server["env"]


# --- the runtime call sites hand the check the name kiro-cli is given ---------------


@pytest.mark.asyncio
async def test_the_spawn_launch_check_judges_the_agent_argument(monkeypatch) -> None:
    from kiro_crew.acp import runtime_spawn_launch
    from kiro_crew.acp.session_handle import AcpRuntimeError

    asked: list[tuple[str, str, str | None]] = []

    def gate(
        agent_name: str, backend: str, *, launch_name: str | None = None, work_dir: object = None
    ) -> None:
        asked.append((agent_name, backend, launch_name))
        if launch_name == "pkg-reviewer":
            raise agent.SharedTemplateOverCeiling("over the ceiling")

    monkeypatch.setattr(agent, "require_shared_template_within_ceiling", gate)
    rt = SimpleNamespace(_agent="pkg-reviewer", _work_dir=None)
    view = "kirocrew-skill-view-" + "1" * 24
    await runtime_spawn_launch._refuse_over_ceiling_launch(rt, view)
    with pytest.raises(AcpRuntimeError, match="over the ceiling"):
        await runtime_spawn_launch._refuse_over_ceiling_launch(rt, "pkg-reviewer")
    await runtime_spawn_launch._refuse_over_ceiling_launch(rt, None)
    assert asked == [
        ("pkg-reviewer", ACP_BACKEND_KIRO, view),
        ("pkg-reviewer", ACP_BACKEND_KIRO, "pkg-reviewer"),
        ("pkg-reviewer", ACP_BACKEND_KIRO, None),
    ]


def _projection(aliases: dict[str, str]) -> Any:
    from kiro_crew.acp.skill_projection_parts.native_projection import NativeSkillProjection

    return NativeSkillProjection(dict(aliases))


def test_a_session_start_names_the_view_its_agent_runs_under() -> None:
    from kiro_crew.acp.runtime_session_setup import _start_launch_name

    spawn = _projection({"pkg-reviewer": "kirocrew-skill-view-" + "a" * 24})
    current = _projection({"other": "kirocrew-skill-view-" + "b" * 24})
    rt = SimpleNamespace(
        _agent="pkg-reviewer",
        _launch_agent="",
        _spawn_skill_projection=spawn,
        _native_skill_projection=current,
    )
    assert _start_launch_name(rt, "pkg-reviewer") == "kirocrew-skill-view-" + "a" * 24
    assert _start_launch_name(rt, "other") == "kirocrew-skill-view-" + "b" * 24
    # No view for the agent: the authored name, whose claimants are checked.
    assert _start_launch_name(rt, "unknown") == "unknown"
    assert _start_launch_name(rt, None) is None
    rt._launch_agent = "kirocrew-app-launch-x"
    assert _start_launch_name(rt, "pkg-reviewer") == "kirocrew-app-launch-x"
    bare = SimpleNamespace(_agent="pkg-reviewer", _launch_agent="")
    assert _start_launch_name(bare, "pkg-reviewer") == "pkg-reviewer"


@pytest.mark.asyncio
async def test_a_mode_switch_refusal_ends_the_session(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from kiro_crew.acp.runtime_mode_activation import _refuse_over_ceiling_mode
    from kiro_crew.acp.session_handle import AcpRuntimeError

    asked: list[str | None] = []

    def gate(
        agent_name: str, backend: str, *, launch_name: str | None = None, work_dir: object = None
    ) -> None:
        asked.append(launch_name)
        if launch_name is None:
            raise agent.SharedTemplateOverCeiling("over the ceiling")

    monkeypatch.setattr(agent, "require_shared_template_within_ceiling", gate)
    rt = SimpleNamespace(_acp_backend=ACP_BACKEND_KIRO, terminate_session=AsyncMock())
    view = "kirocrew-skill-view-" + "c" * 24
    await _refuse_over_ceiling_mode(rt, "s1", "pkg-reviewer", view)
    rt.terminate_session.assert_not_awaited()
    with pytest.raises(AcpRuntimeError, match="over the ceiling"):
        await _refuse_over_ceiling_mode(rt, "s1", "pkg-reviewer", None)
    rt.terminate_session.assert_awaited_once_with("s1")
    assert asked == [view, None]

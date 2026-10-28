"""Kiro Crew never writes a crew-bound spec it does not own, and the ceiling still holds.

That file is the one kiro-cli loads for the agent outside Kiro Crew, so rewriting it
would strip the user's own sessions. The rebuild leaves it byte-identical on every harness; KAS
filters the grants in its projection; and a kiro-cli start on such a file is refused
while its grants exceed the ceiling, since kiro-cli would honour them before Crew's gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.acp import kas_agents
from kiro_crew.acp_backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SPEC_FILE_GRANTS,
)
from kiro_crew.agent_materialization import auto_approve, fork_refresh
from kiro_crew.config.loader import KiroCrewConfig

#: A spec a package manager installed: denied grants on both lists, odd formatting.
_RAW = (
    '{\n  "name": "pkg-reviewer",\n  "prompt": "review",\n'
    '  "tools": ["web_search", "web_fetch", "@gh"],\n'
    '  "allowedTools": ["web_search", "web_fetch"],\n'
    '  "mcpServers": {"gh": {"command": "gh-mcp", "autoApprove": ["*"]}}\n}\n'
)


def _denied(ref: str) -> bool:
    return ref == "web_fetch" or ref.startswith("@gh")


@pytest.fixture(autouse=True)
def _settled(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _floor_monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset())
    _floor_monkeypatch.setattr(fork_refresh, "_shared_template_held", False)
    _floor_monkeypatch.setattr(fork_refresh, "_shared_template_hold_record", None)
    yield


@pytest.fixture
def bound_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(agent_state, "all_fork_info", lambda: {})
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: None)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "agent_spec_path", lambda name: tmp_path / f"{name}.json")
    # The tightened ceiling, as the governance passes see it.
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: not _denied(ref))

    def strip(servers: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return {
            k: {kk: vv for kk, vv in v.items() if kk != "autoApprove"} for k, v in servers.items()
        }

    monkeypatch.setattr(auto_approve, "_strip_ungoverned_auto_approve", strip)
    spec = tmp_path / "pkg-reviewer.json"
    spec.write_text(_RAW, encoding="utf-8")
    _bind(monkeypatch, {"reviewer-crew": "pkg-reviewer"})
    return spec


def _bind(monkeypatch: pytest.MonkeyPatch, bound: dict[str, str]) -> None:
    agents = {crew: SimpleNamespace(kiro_agent=name) for crew, name in bound.items()}
    cfg = SimpleNamespace(agents=agents)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


def test_a_bound_unowned_spec_is_byte_identical_after_a_rebuild(bound_spec: Path) -> None:
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert bound_spec.read_text(encoding="utf-8") == _RAW
    # Nothing is read for it, so nothing is held for it either.
    assert fork_refresh._shared_template_held is False


def test_a_kas_session_from_that_spec_still_cannot_auto_approve_a_denied_tool(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    monkeypatch.setattr(kas_agents, "may_skip_gate_now", lambda ref: not _denied(ref))
    spec = json.loads(bound_spec.read_text(encoding="utf-8"))
    projected = kas_agents.to_client_custom_agent("pkg-reviewer", spec, "review")
    allows = [
        rule
        for rule in (projected.get("permissions") or {}).get("rules", [])
        if rule.get("effect") == "allow"
    ]
    granted = json.dumps(allows)
    assert "web_fetch" not in granted
    assert "gh" not in granted
    # The grant the ceiling permits still travels, so the filter is not a blanket drop.
    assert [r.get("capability") for r in allows] == ["web_search"]


def test_a_kiro_cli_start_on_it_is_refused_and_the_file_is_untouched(bound_spec: Path) -> None:
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="pkg-reviewer.json"):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)
    assert bound_spec.read_text(encoding="utf-8") == _RAW


def test_a_kiro_cli_start_within_the_ceiling_is_not_refused(bound_spec: Path) -> None:
    bound_spec.write_text(
        json.dumps({"name": "pkg-reviewer", "allowedTools": ["web_search"]}), encoding="utf-8"
    )
    fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_empty_grant_lists_on_a_governed_server_are_not_refused(bound_spec: Path) -> None:
    """The strip drops an empty ``autoApprove`` key, but that removes no grant."""
    bound_spec.write_text(
        json.dumps(
            {
                "name": "pkg-reviewer",
                "allowedTools": [],
                "mcpServers": {"gh": {"command": "gh-mcp", "autoApprove": []}},
            }
        ),
        encoding="utf-8",
    )
    fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_a_non_list_auto_approve_on_a_governed_server_is_refused(bound_spec: Path) -> None:
    bound_spec.write_text(
        json.dumps(
            {"name": "pkg-reviewer", "mcpServers": {"gh": {"command": "x", "autoApprove": "*"}}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_a_kas_start_on_it_is_not_refused(bound_spec: Path) -> None:
    fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KAS)
    assert bound_spec.read_text(encoding="utf-8") == _RAW


def test_a_start_on_an_unbound_spec_is_not_checked(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loose = bound_spec.parent / "loose.json"
    loose.write_text(json.dumps({"name": "loose", "allowedTools": ["web_fetch"]}))
    fork_refresh.require_shared_template_within_ceiling("loose", ACP_BACKEND_KIRO)


def test_a_crew_owned_spec_is_left_to_its_own_writer(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = bound_spec.parent / "kirocrew.json"
    raw = json.dumps({"name": "kirocrew", "allowedTools": ["web_fetch"]})
    owned.write_text(raw, encoding="utf-8")
    _bind(monkeypatch, {"default": "kirocrew"})
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    fork_refresh.require_shared_template_within_ceiling("kirocrew", ACP_BACKEND_KIRO)
    assert owned.read_text(encoding="utf-8") == raw


def test_an_unreadable_binding_refuses_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cls: object) -> None:
        raise OSError("config.json unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="could not be checked"):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_a_kiro_cli_client_spawn_runs_the_check_first(tmp_path: Path) -> None:
    """The ``AcpClient`` route to kiro-cli runs the same check as the harness."""
    from unittest.mock import patch

    from acp_launch_capture import capture

    from kiro_crew.acp import client_start

    calls: list[tuple[str | None, str]] = []
    seen: list[str | None] = []
    capture(
        ACP_BACKEND_KIRO,
        tmp_path,
        extra_patches=[
            patch.object(
                client_start,
                "require_shared_template_within_ceiling",
                side_effect=lambda agent, backend: calls.append((agent, backend)),
            )
        ],
        observe=lambda client: seen.append(client._agent),
    )
    assert calls == [(seen[0], ACP_BACKEND_KIRO)]


def test_every_claimant_of_the_name_is_checked(bound_spec: Path) -> None:
    """Which of two files claiming a name kiro-cli loads is undefined, so one over the
    ceiling refuses the start even when the stem match is clean."""
    bound_spec.write_text(json.dumps({"name": "pkg-reviewer", "allowedTools": []}))
    other = bound_spec.parent / "z-other.json"
    other.write_text(json.dumps({"name": "pkg-reviewer", "allowedTools": ["web_fetch"]}))
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="z-other.json"):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_a_hard_linked_spec_refuses_the_start(
    bound_spec: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The fenced reader refuses a second link, and kiro-cli reads the bytes anyway."""
    import os

    bound_spec.write_text(json.dumps({"name": "pkg-reviewer", "allowedTools": []}))
    os.link(bound_spec, tmp_path_factory.mktemp("outside") / "elsewhere.json")
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_a_hard_linked_markdown_file_does_not_refuse_a_json_start(
    bound_spec: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """kiro-cli never loads the markdown form, so an unread one cannot be its spec."""
    import os

    bound_spec.write_text(json.dumps({"name": "pkg-reviewer", "allowedTools": []}))
    md = bound_spec.parent / "notes.md"
    md.write_text("---\nname: notes\n---\nhello\n", encoding="utf-8")
    os.link(md, tmp_path_factory.mktemp("outside") / "elsewhere.md")
    fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN - ACP_BACKENDS_SPEC_FILE_GRANTS))
def test_only_a_host_that_reads_the_file_grants_is_refused(bound_spec: Path, backend: str) -> None:
    """Every other host gets Crew's projection, which filters or drops both lists."""
    fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", backend)


def test_kiro_cli_is_the_host_that_reads_the_file_grants() -> None:
    assert ACP_BACKEND_KIRO in ACP_BACKENDS_SPEC_FILE_GRANTS


def test_a_binding_on_the_file_stem_is_checked_under_its_declared_name(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row can record the file stem while kiro-cli is dispatched the declared name."""
    from kiro_crew.config import loader

    stem = bound_spec.parent / "KiroPkg-captain.json"
    stem.write_text(json.dumps({"name": "captain", "allowedTools": ["web_fetch"]}))
    _bind(monkeypatch, {"captain-crew": "KiroPkg-captain"})
    monkeypatch.setattr(
        loader,
        "dispatch_kiro_agent",
        lambda name: "captain" if name == "KiroPkg-captain" else name,
    )
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="KiroPkg-captain.json"):
        fork_refresh.require_shared_template_within_ceiling("captain", ACP_BACKEND_KIRO)


def test_a_config_load_missing_a_file_still_checks_the_agent(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load that could not read a config file whole may lack the binding this start
    runs, so an absent binding is no proof the spec is unbound."""
    cfg = SimpleNamespace(agents={}, _overlay_unreadable=True)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="pkg-reviewer.json"):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_the_spec_a_moved_crewmate_ran_on_is_still_checked(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation from before the move keeps the template its record names, so
    that template stays checked after no binding names it any more."""
    from kiro_crew import mate_config_migration

    _bind(monkeypatch, {"rvw": "rvw"})
    monkeypatch.setattr(mate_config_migration, "moved_from_agents", lambda: {"pkg-reviewer"})
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="pkg-reviewer.json"):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_an_unreadable_ledger_checks_the_agent_rather_than_skipping_it(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import mate_config_migration

    def unreadable() -> frozenset[str]:
        raise mate_config_migration.LedgerUnreadable("migrated.json is unreadable")

    _bind(monkeypatch, {"rvw": "rvw"})
    monkeypatch.setattr(mate_config_migration, "moved_from_agents", unreadable)
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="pkg-reviewer.json"):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)


def test_a_moved_stem_binding_is_checked_under_its_declared_name(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger records the stem the binding used; the old conversation is
    dispatched the name that file declares."""
    from kiro_crew import mate_config_migration
    from kiro_crew.config import loader

    stem = bound_spec.parent / "KiroPkg-captain.json"
    stem.write_text(json.dumps({"name": "captain", "allowedTools": ["web_fetch"]}))
    _bind(monkeypatch, {"rvw": "rvw"})
    monkeypatch.setattr(mate_config_migration, "moved_from_agents", lambda: {"KiroPkg-captain"})
    monkeypatch.setattr(
        loader,
        "dispatch_kiro_agent",
        lambda name: "captain" if name == "KiroPkg-captain" else name,
    )
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling, match="KiroPkg-captain.json"):
        fork_refresh.require_shared_template_within_ceiling("captain", ACP_BACKEND_KIRO)


def test_a_refused_start_writes_no_withheld_record(
    bound_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check runs the passes on a copy and writes nothing, so it records no
    ``mcp_auto_approve_withheld`` decision: the file still grants the tool."""
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agent, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw))
    )
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling):
        fork_refresh.require_shared_template_within_ceiling("pkg-reviewer", ACP_BACKEND_KIRO)
    assert [e for e in events if e.get("operation") == "mcp_auto_approve_withheld"] == []


def test_a_refused_auto_approve_only_spec_writes_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real strip runs on the check path with its audit off: a spec whose only
    over-ceiling grant is a server ``autoApprove`` records no withheld decision."""
    from kiro_crew.platform import governance

    monkeypatch.setattr(agent_state, "all_fork_info", lambda: {})
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: None)
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "agent_spec_path", lambda name: tmp_path / f"{name}.json")
    seen: list[bool] = []
    real = governance.strip_ungoverned_auto_approve

    def recording(servers: Any, *, audit: bool = True, **kw: Any) -> Any:
        seen.append(audit)
        return {
            k: {kk: vv for kk, vv in v.items() if kk != "autoApprove"} for k, v in servers.items()
        }

    monkeypatch.setattr(auto_approve, "strip_ungoverned_auto_approve", recording)
    (tmp_path / "pkg.json").write_text(
        json.dumps({"name": "pkg", "mcpServers": {"gh": {"command": "gh", "autoApprove": ["*"]}}})
    )
    _bind(monkeypatch, {"crew": "pkg"})
    with pytest.raises(fork_refresh.SharedTemplateOverCeiling):
        fork_refresh.require_shared_template_within_ceiling("pkg", ACP_BACKEND_KIRO)
    assert seen == [False]
    assert real is not recording

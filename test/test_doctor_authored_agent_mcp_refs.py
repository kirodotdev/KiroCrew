"""``kirocrew doctor`` checks the ``@server`` refs of the user's own agents too.

These tests pin that each agent a person wrote or a tool such as AIM installed is
judged on the backend in use, with Kiro Crew's default servers counted as a spawn
adds them (on KAS always, on kiro-cli only while a spawn would project the skill
view, whichever of the config switch, the environment switch, a config that does
not load or the view ceiling turns it off) and, on kiro-cli, the enabled global settings
``mcp.json`` servers counted for an agent that opts in to that file; that a row says
only what Kiro Crew's projection delivers, never that a server is absent; that
Kiro Crew's own and derived specs are left out; that the check is bounded,
advisory and records no permission decision; that the spawn decision it reads
warns, locks and writes nothing; and that ``_doctor`` runs it.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cli_doctor
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.doctor_checks import mcp


def _cfg(backend: str = ACP_BACKEND_KIRO) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))


def _write(name: str, spec: dict) -> Path:
    directory = kiro_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"name": name, **spec}), encoding="utf-8")
    return path


#: An agent shaped like one a package manager installs: it relies on Crew's
#: ``kirocrew-core`` and names a server nothing defines.
_AUTHORED = {
    "mcpServers": {},
    "tools": ["fs_read", "@kirocrew-core", "@kc-repro-absent"],
    "includeMcpJson": False,
}


def test_an_authored_agents_unresolved_ref_is_named(capsys) -> None:
    _write("reprobot", _AUTHORED)
    _write(
        "healthy",
        {"mcpServers": {"mine": {"command": "mine-mcp"}}, "tools": ["@mine", "@kirocrew-core"]},
    )

    mcp._doctor_authored_agent_mcp_refs(_cfg())

    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if "mcp tool refs:" in line]
    assert rows == [
        "  mcp tool refs: ⚠ agent 'reprobot' on kiro: Kiro Crew's projection delivers "
        "none of '@kc-repro-absent'"
    ]
    flat = " ".join(out.split())
    # The same hedge as the default spec's rows: a harness may mount a same-named
    # server from its own configuration, so the row never says "absent".
    assert "The harness may mount a same-named server from its own configuration" in flat
    assert "this row cannot tell which" in flat
    assert "nothing defines" not in flat
    assert "define each server in the agent's mcpServers" in flat


def test_crews_own_and_derived_specs_are_not_judged(capsys) -> None:
    from kiro_crew.dashboard.side_readonly_spec import OWNER_MARKER

    broken = {"mcpServers": {}, "tools": ["@nothing-defines-this"]}
    _write("kirocrew", broken)
    _write("kirocrew-mate-worker", broken)
    _write("kirocrew-skill-view-0123456789abcdef01234567", broken)
    _write("kirocrew-app-launch-demo", broken)
    _write("reprobot--readonly", {**broken, "description": f"{OWNER_MARKER} of 'reprobot'"})

    mcp._doctor_authored_agent_mcp_refs(_cfg())

    assert capsys.readouterr().out == ""


def test_an_agent_found_by_its_declared_name_is_judged_under_it(capsys) -> None:
    """A package-installed file (``<package>-<name>.json``) is the agent ``<name>``."""
    directory = kiro_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "local-pkg-wattson.json").write_text(
        json.dumps({"name": "wattson", **_AUTHORED}), encoding="utf-8"
    )

    mcp._doctor_authored_agent_mcp_refs(_cfg())

    out = capsys.readouterr().out
    assert (
        "agent 'wattson' on kiro: Kiro Crew's projection delivers none of '@kc-repro-absent'"
    ) in out


def test_a_server_the_global_settings_file_defines_counts_on_kiro_cli(capsys) -> None:
    """The doctor answers as the runtime guard does: only an agent that opts in with
    ``includeMcpJson`` true is served by the global settings ``mcp.json``; one that
    states no key, or false (or the ``useLegacyMcpJson`` alias false), is reported."""
    from kiro_crew import agent as agent_mod

    settings = agent_mod._KIRO_MCP_JSON
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"mcpServers": {"github": {"command": "github-mcp"}}}), encoding="utf-8"
    )
    relies = {"mcpServers": {}, "tools": ["fs_read", "@github"]}
    _write("relies-on-global", relies)
    _write("opts-in", {**relies, "includeMcpJson": True})
    _write("opts-out", {**relies, "includeMcpJson": False})
    _write("opts-out-by-alias", {**relies, "useLegacyMcpJson": False})

    mcp._doctor_authored_agent_mcp_refs(_cfg())

    rows = [line for line in capsys.readouterr().out.splitlines() if "mcp tool refs:" in line]
    assert rows == [
        "  mcp tool refs: ⚠ agent 'opts-out' on kiro: Kiro Crew's projection delivers "
        "none of '@github'",
        "  mcp tool refs: ⚠ agent 'opts-out-by-alias' on kiro: Kiro Crew's projection "
        "delivers none of '@github'",
        "  mcp tool refs: ⚠ agent 'relies-on-global' on kiro: Kiro Crew's projection "
        "delivers none of '@github'",
    ]


def test_a_disabled_global_server_does_not_serve_a_ref(capsys) -> None:
    """kiro-cli never starts a ``disabled`` server, so the doctor names the ref the
    runtime guard names; an enabled one under the same opt-in still serves it."""
    from kiro_crew import agent as agent_mod

    settings = agent_mod._KIRO_MCP_JSON
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "foo": {"command": "foo-mcp", "disabled": True},
                    "bar": {"command": "bar-mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    _write(
        "uses-muted",
        {"mcpServers": {}, "tools": ["fs_read", "@foo", "@bar"], "includeMcpJson": True},
    )

    mcp._doctor_authored_agent_mcp_refs(_cfg())

    rows = [line for line in capsys.readouterr().out.splitlines() if "mcp tool refs:" in line]
    assert rows == [
        "  mcp tool refs: ⚠ agent 'uses-muted' on kiro: Kiro Crew's projection delivers "
        "none of '@foo'"
    ]


def test_no_authored_agent_prints_nothing(capsys) -> None:
    mcp._doctor_authored_agent_mcp_refs(_cfg())
    assert capsys.readouterr().out == ""


def test_each_agent_is_judged_on_the_selected_backend_only(monkeypatch, capsys) -> None:
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    _write("reprobot", _AUTHORED)
    asked: list[tuple[str, object]] = []

    def _refs(agent, *, backends=None):
        asked.append((agent, backends))
        return True, []

    monkeypatch.setattr(acp_driver, "agent_spec_mcp_refs", _refs)
    mcp._doctor_authored_agent_mcp_refs(_cfg("codex"))
    assert asked == [("reprobot", ("codex",))]
    assert capsys.readouterr().out == ""


def test_a_backend_that_runs_no_spec_server_is_left_to_its_projection_row(
    monkeypatch, capsys
) -> None:
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    _write("reprobot", _AUTHORED)
    monkeypatch.setattr(acp_driver, "backend_delivers_spec_servers", lambda _backend: False)
    mcp._doctor_authored_agent_mcp_refs(_cfg("pi"))
    assert capsys.readouterr().out == ""


def test_the_check_is_bounded_and_says_what_it_skipped(monkeypatch, capsys) -> None:
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    for name in ("alpha", "beta", "gamma"):
        _write(name, _AUTHORED)
    asked: list[str] = []

    def _refs(agent, *, backends=None):
        asked.append(agent)
        return True, [(ACP_BACKEND_KIRO, [], False)]

    monkeypatch.setattr(acp_driver, "agent_spec_mcp_refs", _refs)
    monkeypatch.setattr(mcp, "_AUTHORED_AGENT_REF_CHECK_CAP", 2)
    mcp._doctor_authored_agent_mcp_refs(_cfg())
    assert asked == ["alpha", "beta"]
    assert capsys.readouterr().out == "  mcp tool refs: ⏹ 1 more authored agent(s) not checked\n"


def test_a_ref_carrying_terminal_controls_is_rendered_inert(capsys) -> None:
    _write("reprobot", {"mcpServers": {}, "tools": ["@srv\x1b]0;pwned\x07"]})
    mcp._doctor_authored_agent_mcp_refs(_cfg())
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "srv" in out


def test_the_row_never_moves_doctors_exit_code() -> None:
    """It takes no ``issues`` list, like the default spec's rows."""
    params = inspect.signature(mcp._doctor_authored_agent_mcp_refs).parameters
    assert list(params) == ["cfg"]


def test_the_row_is_reached_from_the_report_itself() -> None:
    assert "mcp._doctor_authored_agent_mcp_refs(cfg)" in inspect.getsource(cli_doctor._doctor)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, "kas"])
def test_the_sdk_probe_counts_crews_default_servers_where_a_spawn_adds_them(backend) -> None:
    """kiro-cli (through the skill view) and KAS (through the projected agent) add
    ``kirocrew-core`` to a custom agent, so it is not reported there."""
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    _write("reprobot", _AUTHORED)
    found, rows = acp_driver.agent_spec_mcp_refs("reprobot", backends=(backend,))
    assert found is True
    [(row_backend, unresolved, _has_mirror)] = rows
    assert row_backend == backend
    assert unresolved == ["@kc-repro-absent"]


def test_modelling_the_default_servers_records_no_permission_decision(monkeypatch) -> None:
    """Doctor only reads. A spawn under a governance ceiling withholds an
    ``allowedTools`` entry that would auto-approve Crew's added servers and records
    that in the security event log; judging the refs must record nothing."""
    from kiro_crew.acp import kas_agents
    from kiro_crew.agent_sdk.drivers import acp as acp_driver
    from kiro_crew.platform import governance

    recorded: list[tuple] = []
    monkeypatch.setattr(kas_agents, "_audit_permission_decision", lambda *a: recorded.append(a))
    monkeypatch.setattr(governance, "may_skip_gate_now", lambda _ref: False)
    _write("reprobot", {**_AUTHORED, "allowedTools": ["*"]})

    found, rows = acp_driver.agent_spec_mcp_refs("reprobot", backends=(ACP_BACKEND_KIRO,))

    assert found is True
    [(_backend, unresolved, _has_mirror)] = rows
    assert unresolved == ["@kc-repro-absent"]
    assert recorded == []


#: An agent that relies on Crew's default servers whole (``@kirocrew-core``) and
#: per tool (``@kirocrew-cron/...``), beside one server nothing defines.
_RELIES_ON_DEFAULTS = {
    "mcpServers": {},
    "tools": ["fs_read", "@kirocrew-core", "@kirocrew-cron/cron_add", "@kc-repro-absent"],
    "includeMcpJson": False,
}


def _projection_off_by_config(_monkeypatch) -> None:
    from kiro_crew.config.paths import config_dir

    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": {"native_skill_projection": False}}), encoding="utf-8")


def _projection_off_by_env(monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")


def _config_that_does_not_load(monkeypatch) -> None:
    """The opt-out may be in the file that failed to load, so a spawn does not project."""
    from kiro_crew.config import loader as config_loader

    def broken(cls):
        raise RuntimeError("config loader failed")

    monkeypatch.setattr(config_loader.KiroCrewConfig, "load", classmethod(broken))


def _views_at_the_ceiling(_monkeypatch) -> None:
    """As many skill views on disk as the ceiling allows: the next spawn falls back."""
    from kiro_crew.agent_spec_format import (
        NATIVE_SKILL_ALIAS_PREFIX,
        SKILL_VIEW_PROJECTION_CEILING,
    )

    directory = kiro_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(SKILL_VIEW_PROJECTION_CEILING):
        name = f"{NATIVE_SKILL_ALIAS_PREFIX}{index:024x}"
        (directory / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")


_FALLBACKS = pytest.mark.parametrize(
    "fall_back",
    [
        _projection_off_by_config,
        _projection_off_by_env,
        _config_that_does_not_load,
        _views_at_the_ceiling,
    ],
    ids=["config-off", "env-off", "config-load-raises", "view-ceiling"],
)


@_FALLBACKS
def test_kiro_cli_judges_the_authored_spec_when_its_spawn_skips_the_view(
    fall_back, monkeypatch
) -> None:
    """On kiro-cli Crew's default servers ride the skill view only, so a spawn that
    falls back to the authored agent runs without them, and so must the doctor."""
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    _write("reprobot", _RELIES_ON_DEFAULTS)
    fall_back(monkeypatch)

    found, rows = acp_driver.agent_spec_mcp_refs("reprobot", backends=(ACP_BACKEND_KIRO,))

    assert found is True
    [(_backend, unresolved, _has_mirror)] = rows
    assert unresolved == ["@kc-repro-absent", "@kirocrew-core", "@kirocrew-cron"]


@_FALLBACKS
def test_kas_keeps_counting_the_default_servers_it_adds_itself(fall_back, monkeypatch) -> None:
    """KAS adds them through its own projection whatever the skill view does."""
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    _write("reprobot", _RELIES_ON_DEFAULTS)
    fall_back(monkeypatch)

    found, rows = acp_driver.agent_spec_mcp_refs("reprobot", backends=("kas",))

    assert found is True
    [(_backend, unresolved, _has_mirror)] = rows
    assert unresolved == ["@kc-repro-absent"]


def test_the_doctor_row_names_the_default_servers_a_projection_off_spawn_lacks(
    monkeypatch, capsys
) -> None:
    _write("reprobot", _RELIES_ON_DEFAULTS)
    _projection_off_by_config(monkeypatch)

    mcp._doctor_authored_agent_mcp_refs(_cfg())

    rows = [line for line in capsys.readouterr().out.splitlines() if "mcp tool refs:" in line]
    assert rows == [
        "  mcp tool refs: ⚠ agent 'reprobot' on kiro: Kiro Crew's projection delivers "
        "none of '@kc-repro-absent', '@kirocrew-core', '@kirocrew-cron'"
    ]


@_FALLBACKS
def test_the_spawn_decision_the_doctor_reads_answers_each_fallback_and_acts_on_none(
    fall_back, monkeypatch
) -> None:
    """Each switch that makes a spawn fall back to the authored agent turns the
    answer off, and asking warns, prunes, locks and writes nothing."""
    from kiro_crew.acp import skill_projection

    acted: list[str] = []
    monkeypatch.setattr(
        skill_projection, "_warn_ceiling_fallback_once", lambda *_a: acted.append("warn")
    )
    monkeypatch.setattr(
        skill_projection, "_projection_alias_lock", lambda *_a: acted.append("lock")
    )
    monkeypatch.setattr(
        skill_projection, "_prune_stale_managed_aliases", lambda *_a, **_k: acted.append("prune")
    )
    _write("reprobot", _RELIES_ON_DEFAULTS)
    # Where a projection writes: its views in the agents directory, its side
    # files and its lock in the data home's side directory.
    roots = (kiro_agents_dir(), skill_projection._projection_side_dir())

    def tree() -> list[str]:
        return sorted(
            f"{root}/{path.relative_to(root).as_posix()}"
            for root in roots
            if root.exists()
            for path in (root, *root.rglob("*"))
        )

    before = tree()
    assert skill_projection.next_spawn_projects_skill_view() is True
    assert tree() == before

    fall_back(monkeypatch)
    before = tree()
    assert skill_projection.next_spawn_projects_skill_view() is False
    assert tree() == before
    assert acted == []

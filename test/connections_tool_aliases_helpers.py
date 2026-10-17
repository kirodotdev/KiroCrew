"""Builders and autouse fixtures shared by the ``test_connections_tool_aliases*.py`` modules.

Both fixtures are autouse and module-wide in every file of the family, so each module
imports them by name: one pins the kiro-cli release the spec ``permissions`` gate
believes is installed, the other gives each test its own ``KIROCREW_HOME`` so the
alias record lands in a tmp dir. The builders are the registry endpoint URLs, the
record triples and alias maps, ``_gen`` for an ``AliasGeneration``, the real-rebuild
environment, and the record-write failure injectors.
"""

import json

import pytest

from kiro_crew.connections.alias_record import AliasGeneration, spec_fingerprint
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the module-wide autouse pin of the test_connections_tool_aliases*.py modules, moved verbatim; none of them calls monkeypatch.undo(); tracked for a de-flake pass
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    The real-rebuild tests below drive ``rebuild_agent_config``, which ends in
    ``_write_derived_permissions`` and reads ``installed_kiro_cli_version``
    function-locally from ``kiro_crew.kiro_cli``: one real ``kiro-cli --version``
    spawn per binary identity, process-cached, so whichever test in the worker
    rebuilds first pays it against the HOST's install with the checkout as the
    child's cwd. Pinned to the floor release, as ``test_agent.py`` and the
    generated-writer suites pin it.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


URLS = {
    "github": "https://api.githubcopilot.com/mcp/",
    "linear": "https://mcp.linear.app/mcp/readonly",
    "vercel": "https://mcp.vercel.com",
    "gitlab": "https://gitlab.com/api/v4/mcp",
    "atlassian": "https://mcp.atlassian.com/v1/mcp/authv2",
    "neon": "https://mcp.neon.tech/mcp?readonly=true",
    "todoist": "https://ai.todoist.net/mcp",
}


_L = ("linear", "list_projects", "linear_list_projects")
_V = ("vercel", "list_projects", "vercel_list_projects")
_MAP_L = {"@linear/list_projects": "linear_list_projects"}
_MAP_LV = {**_MAP_L, "@vercel/list_projects": "vercel_list_projects"}


def _gen(aliases, emitted):
    return AliasGeneration(spec_fingerprint(aliases), frozenset(emitted))


def _raise_read_only(*_args, **_kwargs):
    """Stand-in for an unwritable data home (read-only mount, no space, no perm)."""
    raise OSError("read-only")


def _rebuild_env(tmp_path, monkeypatch):
    """Point ``rebuild_agent_config`` at a throwaway project, data home and spec."""
    from kiro_crew import agent as agent_mod
    from kiro_crew.apps import bridges

    project = tmp_path / "project" / "agents"
    project.mkdir(parents=True)
    (project / "defaults.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    (project / "prompt.md").write_text("prompt", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    spec_path = kiro_dir / "kirocrew.json"
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-kiro.json")
    monkeypatch.setattr(agent_mod, "_CC_MCP_JSON", tmp_path / "absent-cc.json")
    # Makes the write take the locked branch, as it does in production.
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: spec_path)
    return spec_path


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the module-wide autouse pin of the test_connections_tool_aliases*.py modules, moved verbatim; none of them calls monkeypatch.undo(); tracked for a de-flake pass
def _isolated_alias_record(tmp_path, monkeypatch):
    """Point the emitted-alias record at a per-test data home.

    Autouse because the pass READS the record on every run: without this a test
    would consult (and :func:`_apply` would write) the developer's real
    ``~/.kiro/crew``, and the record would leak between tests. Function-scoped, so
    each test starts with no record at all -- the missing-record case -- and the
    two ``_apply`` calls inside one test share the record the way two rebuilds do.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _fail_nth_record_write(n: int):
    """Fail the *n*-th record write and let the rest through.

    The pass writes the pending record and the caller writes the commit, so a blanket
    patch would fail the pending one first -- which the pass handles by standing down,
    never reaching the boundary under test.
    """
    from kiro_crew.connections import alias_record

    real = alias_record.atomic_write
    calls = {"n": 0}

    def _write(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            raise OSError("read-only")
        return real(*args, **kwargs)

    return _write

"""Stale-config detection: what the fingerprint scans and how.

Loader and ``includeMcpJson`` parity with each backend, the path gate before
every directory scan, listing and resolving through a pinned directory, and
the Windows path that scans nothing and reports unknown.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from stale_config_helpers import _scan_on_every_os_fixture  # noqa: F401
from stale_config_helpers import kiro_home_fixture  # noqa: F401
from stale_config_helpers import (
    _A,
    _fp,
    _gate_refuses,
    _Provider,
    _status_slot,
    _write_spec,
    _write_workspace_spec,
)

from kiro_crew.dashboard import config_staleness as cs
from kiro_crew.dashboard import stale_config
from kiro_crew.dashboard.stale_config import (
    ConfigFingerprint,
    SpawnInputs,
    compute_fingerprint,
    is_stale,
    make_record,
)

# ── Loader parity: the fingerprint watches the file each backend loads ──────

_PARITY_AGENT = "parity"


def _json_spec(path: Path, prompt: str, *, name: str = _PARITY_AGENT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "prompt": prompt}))


def _md_named(path: Path, prompt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {_PARITY_AGENT}\n---\n{prompt}\n")


def _layout(kind: str, kiro_home: Path, project: Path) -> None:
    user, ws = kiro_home / "agents", project / ".kiro" / "agents"
    if kind == "workspace-json+user-json":
        _json_spec(ws / f"{_PARITY_AGENT}.json", "ws-json")
        _json_spec(user / f"{_PARITY_AGENT}.json", "user-json")
    elif kind == "workspace-md+user-json":
        _md_named(ws / f"{_PARITY_AGENT}.md", "ws-md")
        _json_spec(user / f"{_PARITY_AGENT}.json", "user-json")
    elif kind == "user-md-only":
        _md_named(user / f"{_PARITY_AGENT}.md", "user-md")
    elif kind == "declared-vs-filename":
        # The filename claims the agent, another file DECLARES it: declared wins.
        _json_spec(user / f"{_PARITY_AGENT}.json", "filename", name="someone-else")
        _json_spec(user / "zz-declares.json", "declared")
    else:  # pragma: no cover
        raise AssertionError(kind)


def _watched_prompt(project: Path, backend: str) -> str | None:
    """The prompt of the file the fingerprint watches, or None for no spec."""
    from kiro_crew.agent_spec_format import parse_agent_spec_bytes

    fp = compute_fingerprint(SpawnInputs(_PARITY_AGENT, str(project)), backend=backend)
    path = dict(fp.parts)["spec_path"]
    if not path:
        return None
    return parse_agent_spec_bytes(Path(path).read_bytes(), Path(path)).get("prompt", "").strip()


_LAYOUTS = [
    "workspace-json+user-json",
    "workspace-md+user-json",
    "user-md-only",
    "declared-vs-filename",
]


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_kas_parity_with_load_agent_spec(kiro_home, tmp_path, layout):
    from kiro_crew.acp.kas_agents import load_agent_spec

    project = tmp_path / "proj"
    _layout(layout, kiro_home, project)

    loaded = load_agent_spec(kiro_home / "agents", _PARITY_AGENT)

    assert _watched_prompt(project, "kas") == str(loaded.get("prompt", "")).strip()


@pytest.mark.parametrize("layout", _LAYOUTS)
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode", "goose", "deepseek", "pi"])
def test_array_backend_parity_with_session_mcp(kiro_home, tmp_path, layout, backend, monkeypatch):
    """Every array backend runs on the spec ``_agent_spec_and_snapshot_for`` reads."""
    from kiro_crew.acp import session_mcp
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_SESSION_MCP_ARRAY

    # The parametrization is the array set, so a backend joining it is covered here.
    assert backend in ACP_BACKENDS_SESSION_MCP_ARRAY
    project = tmp_path / "proj"
    _layout(layout, kiro_home, project)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: None)
    monkeypatch.setattr(session_mcp, "require_fresh_derived_spec", lambda *_a: None)

    spec, _snapshot = session_mcp._agent_spec_and_snapshot_for(_PARITY_AGENT, str(project))

    expected = None if spec is None else str(spec.get("prompt", "")).strip()
    assert _watched_prompt(project, backend) == expected


def test_the_array_parity_set_is_the_array_backends():
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_SESSION_MCP_ARRAY

    assert ACP_BACKENDS_SESSION_MCP_ARRAY == {
        "claude",
        "codex",
        "opencode",
        "goose",
        "deepseek",
        "pi",
    }


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_kiro_cli_parity_with_its_json_discovery(kiro_home, tmp_path, layout):
    """kiro-cli loads JSON only: checkout first, then the user directory."""
    from kiro_crew.agent import _resolve_agent_spec, markdown_spec_for_agent
    from kiro_crew.agent_discovery import project_agent_files, project_agent_name
    from kiro_crew.agent_spec_format import is_markdown_spec, parse_agent_spec_bytes

    project = tmp_path / "proj"
    _layout(layout, kiro_home, project)

    project_json = [
        p
        for p in project_agent_files(str(project))
        if not is_markdown_spec(p) and project_agent_name(p) == _PARITY_AGENT
    ]
    loaded = (
        project_json[0]
        if project_json
        else _resolve_agent_spec(_PARITY_AGENT, None, json_only=True)
    )
    if markdown_spec_for_agent(_PARITY_AGENT, str(project)) is not None:
        # A markdown-only agent: kiro-cli has no spec for it at all.
        assert loaded is None
    expected = (
        None
        if loaded is None
        else parse_agent_spec_bytes(loaded.read_bytes(), loaded).get("prompt", "").strip()
    )
    assert _watched_prompt(project, "") == expected


# ── includeMcpJson parity: the mount rule each backend applies ─────────────

#: Spec bodies covering every shape of the flag: absent, each bool, an explicit
#: null and a non-bool a hand-edited spec can carry.
_MCP_JSON_FLAGS = [
    {},
    {"includeMcpJson": True},
    {"includeMcpJson": False},
    {"includeMcpJson": None},
    {"includeMcpJson": "yes"},
]


@pytest.mark.parametrize("body", _MCP_JSON_FLAGS)
def test_kiro_cli_mcp_json_rule_matches_agent_capabilities(body):
    """kiro-cli mounts ``mcp.json`` unless the spec says ``false``.

    The rule's real owner is the kiro-cli binary, which is not in this repo. The
    nearest in-repo statement of it is the reading ``agent_capabilities`` applies to
    the specs it edits, so the copy here is pinned to that expression: a change to
    it there turns this red.
    """
    import inspect

    from kiro_crew import agent_capabilities
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

    owner_reading = 'spec.get("includeMcpJson", True) is not False'
    assert owner_reading in inspect.getsource(agent_capabilities)
    spec = {"name": _PARITY_AGENT, **body}
    expected = spec.get("includeMcpJson", True) is not False
    assert stale_config._mcp_json_mounted("ok", spec, ACP_BACKEND_KIRO) is expected


@pytest.mark.parametrize("body", _MCP_JSON_FLAGS)
def test_kas_mcp_json_rule_matches_the_kas_projection(body):
    """KAS mounts ``mcp.json`` only on a stated ``true``.

    The projection ``to_client_custom_agent`` forwards a stated bool and drops
    anything else; KAS's own consumer (outside this repo) reads an absent flag
    as false, so what reaches KAS as ``true`` is the only mounting case.
    """
    from kiro_crew.acp.kas_agents import to_client_custom_agent
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    spec = {"name": _PARITY_AGENT, "tools": [], **body}
    projected = to_client_custom_agent(_PARITY_AGENT, spec, "prompt")
    expected = projected.get("includeMcpJson", False) is True
    assert stale_config._mcp_json_mounted("ok", spec, ACP_BACKEND_KAS) is expected


@pytest.mark.parametrize("body", _MCP_JSON_FLAGS)
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode", "goose", "deepseek", "pi"])
def test_array_backend_mcp_json_rule_matches_session_mcp_servers(kiro_home, body, backend):
    """An array backend is handed the spec's servers and Crew's own, never ``mcp.json``'s."""
    from kiro_crew.acp import session_mcp

    (kiro_home / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"from-mcp-json": {"command": "x"}}})
    )
    spec = {
        "name": _PARITY_AGENT,
        "mcpServers": {"own": {"command": "x"}},
        "tools": ["*"],
        **body,
    }
    names = {entry.get("name") for entry in session_mcp.session_mcp_servers(None, spec=spec)}
    assert "own" in names
    expected = "from-mcp-json" in names
    assert stale_config._mcp_json_mounted("ok", spec, backend) is expected


def test_every_backend_has_an_mcp_json_rule_pinned():
    """A backend joining the registry must take a side in the rules above.

    pi is the one known backend with no in-repo owner: it is outside the array set,
    so Crew hands it no servers and pi reads its spec itself. The badge reads it as
    not mounting ``mcp.json``, which is what this pins until pi gains an owner here.
    """
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        ACP_BACKEND_PI,
        ACP_BACKENDS_KNOWN,
        ACP_BACKENDS_SESSION_MCP_ARRAY,
    )

    pinned = {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_PI} | ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKENDS_KNOWN == pinned
    spec = {"name": _PARITY_AGENT, "includeMcpJson": True}
    assert stale_config._mcp_json_mounted("ok", spec, ACP_BACKEND_PI) is False


def test_the_capped_signature_is_stable_and_order_independent(tmp_path, monkeypatch):
    """Same directory, any scan order: same signature; any overflow change moves it."""
    monkeypatch.setattr(stale_config, "SIGNATURE_MAX_SPEC_ENTRIES", 3)
    agents = tmp_path / "agents"
    agents.mkdir()
    for i in range(8):
        (agents / f"a{i}.json").write_text("{}")
    first = stale_config._stat_spec_dir(agents)
    assert stale_config._stat_spec_dir(agents) == first
    # The kept entries are the smallest names, whatever order scandir yields.
    assert [e[0] for e in first[1:-1]] == ["a0.json", "a1.json", "a2.json"]

    real_scandir = os.scandir

    class _Reversed:
        def __init__(self, path):
            self._it = real_scandir(path)
            self._entries = list(self._it)[::-1]

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *exc):
            self._it.close()

    monkeypatch.setattr(os, "scandir", _Reversed)
    assert stale_config._stat_spec_dir(agents) == first
    monkeypatch.setattr(os, "scandir", real_scandir)

    st = agents.stat()
    (agents / "a6.json").unlink()  # a removal past the cap
    os.utime(agents, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert stale_config._stat_spec_dir(agents)[-1] != first[-1]


def test_the_listing_is_streamed_not_collected(tmp_path, monkeypatch):
    """No list of every name is built: the scan is consumed lazily."""
    import inspect

    src = inspect.getsource(stale_config._stat_spec_dir)
    assert "sorted(e.name for e in entries" not in src
    assert "heapq" in src


# ── The path gate before every directory scan ────────────────────────────────


def _refuse(monkeypatch, refused: Path) -> list[str]:
    """Make the path gate refuse *refused* (a link resolving to a UNC share,
    say) and record every path the scanner would hand ``os.scandir``."""
    _gate_refuses(monkeypatch, lambda raw: Path(raw) == refused)
    scanned: list[str] = []
    real_scandir = os.scandir

    def _spy(path):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _spy)
    return scanned


def test_a_refused_agents_dir_is_never_listed_by_the_signature(kiro_home, monkeypatch):
    agents = kiro_home / "agents"
    _write_spec(kiro_home, {"prompt": "a"})
    scanned = _refuse(monkeypatch, agents)

    assert stale_config._stat_spec_dir(agents) == ("refused",)
    assert str(agents) not in scanned


def test_a_refused_agents_dir_is_never_scanned_by_the_unlistable_check(kiro_home, monkeypatch):
    agents = kiro_home / "agents"
    scanned = _refuse(monkeypatch, agents)

    assert stale_config._spec_scope_unlistable("", "") == {"user"}
    assert str(agents) not in scanned


def test_a_refused_agents_dir_is_never_resolved_and_reads_unknown(kiro_home, monkeypatch):
    """The spec resolver never lists it, and the input is unknown, not changed."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    agents = kiro_home / "agents"
    _refuse(monkeypatch, agents)
    from kiro_crew import agent

    listed: list[str] = []
    real_iter = agent.iter_agent_spec_files
    monkeypatch.setattr(
        agent, "iter_agent_spec_files", lambda d, **k: listed.append(str(d)) or real_iter(d, **k)
    )

    current = _fp()

    assert str(agents) not in listed
    assert "spec" in current.unreadable
    compared = stale_config.carry_forward(recorded, current)
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_a_pinned_resolve_refuses_an_oversized_agents_dir(tmp_path, monkeypatch):
    """The listing is bounded: past the cap the resolve raises, never lists it whole."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    for i in range(4):
        _write_scope_spec(agents, f"s{i}.json", {"name": f"s{i}"})
    monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_ENTRIES", 3)

    with platform_compat.pinned_directory(agents) as pinned:
        with pytest.raises(agent.SpecDirectoryOverflowError):
            agent.pinned_agent_spec_path("s0", agents_dir=agents, pinned=pinned, json_only=False)
        monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_ENTRIES", 4)
        found = agent.pinned_agent_spec_path(
            "s0", agents_dir=agents, pinned=pinned, json_only=False
        )

    assert found == agents / "s0.json"


def test_a_pinned_resolve_refuses_specs_past_the_read_budget(tmp_path, monkeypatch):
    """The reads are bounded in aggregate: past the budget the resolve raises."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    for i in range(4):
        _write_scope_spec(agents, f"s{i}.json", {"name": f"s{i}", "prompt": "x" * 200})
    total = sum(p.stat().st_size for p in agents.iterdir())

    with platform_compat.pinned_directory(agents) as pinned:
        monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_TOTAL_BYTES", total - 1)
        with pytest.raises(agent.SpecDirectoryOverflowError):
            agent.pinned_agent_spec_path("s0", agents_dir=agents, pinned=pinned, json_only=False)
        monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_TOTAL_BYTES", total)
        found = agent.pinned_agent_spec_path(
            "s0", agents_dir=agents, pinned=pinned, json_only=False
        )

    assert found == agents / "s0.json"


def test_an_agents_dir_past_the_read_budget_reads_unknown_not_changed(kiro_home, monkeypatch):
    """Specs summing past the read budget are an unknown spec, carried forward."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    from kiro_crew import agent

    for i in range(3):
        (kiro_home / "agents" / f"pad{i}.json").write_text(json.dumps({"prompt": "x" * 400}))
    monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_TOTAL_BYTES", 512)

    current = _fp()

    assert "spec" in current.unreadable
    compared = stale_config.carry_forward(recorded, current)
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_an_oversized_agents_dir_reads_unknown_not_changed(kiro_home, monkeypatch):
    """An agents directory past the listing cap is an unknown spec, carried forward."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    from kiro_crew import agent

    for i in range(3):
        (kiro_home / "agents" / f"pad{i}.txt").write_text("x")
    monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_ENTRIES", 2)

    current = _fp()

    assert "spec" in current.unreadable
    compared = stale_config.carry_forward(recorded, current)
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_a_refused_mcp_json_is_not_stated(kiro_home, monkeypatch):
    target = kiro_home / "settings" / "mcp.json"
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", target)
    _gate_refuses(monkeypatch, lambda raw: Path(raw) == target)
    stated: list[str] = []
    real_stat = stale_config._stat_in
    monkeypatch.setattr(
        stale_config,
        "_stat_in",
        lambda d, n: stated.append(os.path.join(d.path, n)) or real_stat(d, n),
    )

    sig = dict(stale_config.input_signature(SpawnInputs("kirocrew", "")))

    assert sig["global-mcp"] == ("refused",)
    assert str(target) not in stated


def test_a_refused_workspace_agents_dir_is_never_resolved(kiro_home, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"prompt": "ws"})
    ws_agents = project / ".kiro" / "agents"
    _refuse(monkeypatch, ws_agents)
    from kiro_crew import agent

    listed: list[str] = []
    real_iter = agent.iter_agent_spec_files
    monkeypatch.setattr(
        agent, "iter_agent_spec_files", lambda d, **k: listed.append(str(d)) or real_iter(d, **k)
    )

    fp = _fp(project=str(project), backend="claude")

    assert str(ws_agents) not in listed
    assert "spec" in fp.unreadable


# ── Listing and stat'ing through a pin, never the validated name again ──────


def _link_after_the_gate(monkeypatch, link: Path, target: Path) -> list[object]:
    """Put a link at *link* pointing at *target* (a junction to a UNC share, on
    Windows) and admit it at the gate, as a swap landing between the screen and
    the scan would. Records every argument ``os.scandir`` is handed."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "kirocrew.json").write_text('{"name": "kirocrew"}')
    if link.is_dir() and not link.is_symlink():
        for child in link.iterdir():
            child.unlink()
        link.rmdir()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(stale_config, "_gated_dir", lambda directory: directory)
    scanned: list[object] = []
    real_scandir = os.scandir

    def _spy(path):
        scanned.append(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _spy)
    return scanned


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
def test_a_user_agents_dir_swapped_for_a_link_is_never_followed(kiro_home, tmp_path, monkeypatch):
    agents = kiro_home / "agents"
    scanned = _link_after_the_gate(monkeypatch, agents, tmp_path / "share")

    assert stale_config._stat_spec_dir(agents) == ("unreadable",)
    assert stale_config._spec_scope_unlistable("", "") == {"user"}
    assert scanned == []


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
def test_a_project_agents_dir_swapped_for_a_link_is_never_followed(
    kiro_home, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    scanned = _link_after_the_gate(monkeypatch, project / ".kiro" / "agents", tmp_path / "share")

    sig = dict(stale_config.input_signature(SpawnInputs("kirocrew", str(project))))

    assert sig["project-agents"] == ("unreadable",)
    assert stale_config._spec_scope_unlistable(str(project), "claude") == {"workspace"}
    # Only the user agents directory, a real one, was scanned -- through its pin.
    assert all(isinstance(path, int) for path in scanned)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX only: a pinned directory refuses a rename on Windows"
)
def test_the_listing_and_every_entry_stat_come_from_the_pinned_directory(tmp_path, monkeypatch):
    """A rename of the name after the pin is not followed by the scan or the stats."""
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.json").write_text("{}")
    moved = tmp_path / "agents.moved"
    real_pin = stale_config._pin

    def _pin_then_swap(admitted):
        pinned = real_pin(admitted)
        admitted.rename(moved)
        admitted.mkdir()
        (admitted / "a.json").write_text('{"name": "planted", "prompt": "longer"}')
        (admitted / "b.json").write_text("{}")
        return pinned

    monkeypatch.setattr(stale_config, "_pin", _pin_then_swap)

    signature = stale_config._stat_spec_dir(agents)

    st = (moved / "a.json").lstat()
    assert signature[1:] == (("a.json", (st.st_mtime_ns, st.st_size, st.st_ctime_ns)),)


# ── Resolving the spec through the pin, never the screened name again ────────


def _scope_dir(kiro_home: Path, project: Path, scope: str) -> Path:
    return kiro_home / "agents" if scope == "user" else project / ".kiro" / "agents"


def _write_scope_spec(directory: Path, filename: str, document: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(document))


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
@pytest.mark.parametrize("scope", ["user", "workspace"])
def test_a_spec_dir_swapped_for_a_link_after_the_screen_is_never_resolved(
    kiro_home, tmp_path, monkeypatch, scope
):
    """The gate admits the real directory; a link lands before the resolve.

    On Windows that link is a junction to a UNC share, and a scan by name is an
    outbound SMB authentication: the resolver must not list or read it.
    """
    project = tmp_path / "proj"
    directory = _scope_dir(kiro_home, project, scope)
    _write_scope_spec(directory, "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    share = tmp_path / "share"
    _write_scope_spec(share, "kirocrew.json", {"name": "kirocrew", "prompt": "share"})
    real_gate = stale_config._gated_dir

    def _screen_then_swap(candidate: Path):
        admitted = real_gate(candidate)
        if candidate == directory and directory.is_dir() and not directory.is_symlink():
            directory.rename(tmp_path / "moved")
            directory.symlink_to(share, target_is_directory=True)
        return admitted

    monkeypatch.setattr(stale_config, "_gated_dir", _screen_then_swap)
    from kiro_crew import agent

    listed: list[str] = []
    real_iter = agent.iter_agent_spec_files
    monkeypatch.setattr(
        agent, "iter_agent_spec_files", lambda d, **k: listed.append(str(d)) or real_iter(d, **k)
    )

    selection = stale_config._agent_spec_files("kirocrew", str(project), "claude")

    assert selection.unknown == {scope}
    assert getattr(selection, scope) is None
    assert listed == []


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
def test_an_ancestor_swapped_for_a_link_after_the_screen_is_never_followed(
    kiro_home, tmp_path, monkeypatch
):
    """The gate admits ``proj/.kiro/agents``; ``proj/.kiro`` turns into a link before the pin.

    A pin of the absolute path refuses a link at ``agents`` only and walks
    ``.kiro`` by name -- on Windows a junction there aimed at a UNC share is an
    outbound SMB authentication. The component-wise pin refuses it instead.
    """
    project = tmp_path / "proj"
    directory = project / ".kiro" / "agents"
    _write_scope_spec(directory, "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    share = tmp_path / "share" / ".kiro"
    _write_scope_spec(share / "agents", "kirocrew.json", {"name": "kirocrew", "prompt": "share"})
    real_gate = stale_config._gated_dir

    def _screen_then_swap_ancestor(candidate: Path):
        admitted = real_gate(candidate)
        ancestor = project / ".kiro"
        if candidate == directory and not ancestor.is_symlink():
            ancestor.rename(tmp_path / "moved")
            ancestor.symlink_to(share, target_is_directory=True)
        return admitted

    monkeypatch.setattr(stale_config, "_gated_dir", _screen_then_swap_ancestor)

    selection = stale_config._agent_spec_files("kirocrew", str(project), "claude")

    assert selection.unknown == {"workspace"}
    assert selection.workspace is None


def test_the_pin_opens_one_component_at_a_time_from_the_anchor(tmp_path, monkeypatch):
    """Only the anchor is opened by an absolute path; every other open is one name."""
    from kiro_crew import platform_compat

    directory = (tmp_path / "a" / "b" / "agents").resolve()
    directory.mkdir(parents=True)
    (directory / "x.json").write_text("{}")
    absolute: list[str] = []
    children: list[str] = []
    real_pinned = platform_compat.pinned_directory
    real_child = platform_compat.PinnedDirectory.child
    monkeypatch.setattr(
        platform_compat,
        "pinned_directory",
        lambda path: absolute.append(os.fspath(path)) or real_pinned(path),
    )
    monkeypatch.setattr(
        platform_compat.PinnedDirectory,
        "child",
        lambda self, name: children.append(name) or real_child(self, name),
    )

    with stale_config._pin(directory) as pinned:
        assert pinned.names() == ["x.json"]
        assert pinned.path == str(directory)

    assert absolute == [directory.anchor]
    assert children == list(directory.relative_to(directory.anchor).parts)


def test_a_plain_file_at_the_pinned_name_is_told_apart_from_one_above_it(tmp_path):
    base = tmp_path.resolve()
    (base / "agents").write_text("not a directory")
    (base / "file").write_text("not a directory either")

    with pytest.raises(stale_config._PlainFileAtName):
        stale_config._pin(base / "agents")
    with pytest.raises(OSError) as above:
        stale_config._pin(base / "file" / "agents")
    assert not isinstance(above.value, stale_config._PlainFileAtName)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX only: a pinned directory refuses a rename on Windows"
)
@pytest.mark.parametrize("scope", ["user", "workspace"])
def test_the_spec_is_resolved_and_read_from_the_pinned_directory(
    kiro_home, tmp_path, monkeypatch, scope
):
    """A rename of the scope's name after the pin is not followed by the resolve or the read."""
    project = tmp_path / "proj"
    directory = _scope_dir(kiro_home, project, scope)
    _write_scope_spec(directory, "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    expected = _fp(project=str(project), backend="claude")
    real_pin = stale_config._pin
    swapped: list[Path] = []

    def _pin_then_swap(admitted: Path):
        pinned = real_pin(admitted)
        if admitted == directory and not swapped:
            swapped.append(admitted)
            admitted.rename(tmp_path / "moved")
            _write_scope_spec(admitted, "other.json", {"name": "kirocrew", "prompt": "planted"})
        return pinned

    monkeypatch.setattr(stale_config, "_pin", _pin_then_swap)

    current = _fp(project=str(project), backend="claude")

    assert swapped == [directory]
    parts = dict(current.parts)
    assert parts["spec_path"] == str(directory / "kirocrew.json")
    assert parts["spec_fields"] == dict(expected.parts)["spec_fields"]
    assert current.unreadable == frozenset()


@pytest.mark.parametrize("json_only", [False, True])
def test_a_pinned_resolve_selects_what_the_named_resolve_selects(tmp_path, json_only):
    """Same selection rules through the handle: twins, forms, aliases, declared names."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    _write_scope_spec(agents, "a.json", {"name": "alpha"})
    (agents / "a.md").write_text("---\nname: shadowed\n---\nbody\n")
    (agents / "b.md").write_text("---\nname: beta\n---\nbody\n")
    _write_scope_spec(agents, "kirocrew-skill-view-x.json", {"name": "alias"})
    _write_scope_spec(agents, "gamma.json", {"name": "other"})
    (agents / "broken.json").write_text("{")
    names = ["alpha", "shadowed", "beta", "alias", "gamma", "broken", "missing"]
    by_name = {n: agent._resolve_agent_spec(n, agents, json_only=json_only) for n in names}
    with platform_compat.pinned_directory(agents) as pinned:
        by_pin = {
            n: agent.pinned_agent_spec_path(
                n, agents_dir=agents, pinned=pinned, json_only=json_only
            )
            for n in names
        }

    assert by_pin == by_name
    assert by_pin["alpha"] == agents / "a.json"
    assert by_pin["beta"] == (None if json_only else agents / "b.md")


def test_a_pinned_resolve_keeps_the_sensitive_path_fence(tmp_path, monkeypatch):
    """A spec the fence refuses is skipped through the handle, as it is by name."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    _write_scope_spec(agents, "kirocrew.json", {"name": "kirocrew"})
    fenced = str(agents / "kirocrew.json")
    monkeypatch.setattr(agent, "is_sensitive_canonical_path", lambda path: path == fenced)

    with platform_compat.pinned_directory(agents) as pinned:
        found = agent.pinned_agent_spec_path(
            "kirocrew", agents_dir=agents, pinned=pinned, json_only=False
        )

    assert found is None


def test_a_pure_backend_switch_names_the_backend(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    cs.KiroCrewConfig.load.return_value.agent.acp_backend = "claude"

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["agent.acp_backend"]


def test_a_member_dm_backend_switch_names_the_member_key(kiro_home, tmp_path, monkeypatch):
    """A member DM resolves from ``agent.member_acp_backend``: that key is the one named."""
    from kiro_crew import acp_backends

    monkeypatch.setattr(acp_backends, "resolve_selected_backend", lambda v: v or "kiro")
    _write_spec(kiro_home, {"prompt": "a"})
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("member-alice", mode="member")
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.push_slots_update = MagicMock()
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    key = cs.effective_session_key(slot)
    inputs = SpawnInputs("kirocrew", "")
    slot._spawn_config = make_record(
        provider, inputs, cs.current_config_fingerprint(cfg, key, inputs)
    )

    # The default backend moving does not reach a member DM at all.
    cfg.agent.acp_backend = "claude"
    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False

    cfg.agent.member_acp_backend = "claude"
    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["agent.member_acp_backend"]


def test_a_backend_label_without_a_recorded_key_falls_back_to_the_default_key():
    fp = ConfigFingerprint("r", "s", parts=(("backend", "kiro"),))
    assert cs.display_input("backend", fp, Path("agents")) == "agent.acp_backend"


def test_an_unattributed_change_names_no_particular_file(kiro_home, tmp_path, monkeypatch):
    """A recorded fingerprint with no per-input parts still reads stale, unattributed."""
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(
        tmp_path, monkeypatch, recorded=ConfigFingerprint(reconcilable="r", spawn_only="s")
    )

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["session configuration"]


# -- Windows: the badge scans nothing and reports unknown --


def _windows(monkeypatch) -> None:
    """Switch the Windows short-circuit on and make any filesystem read fail the test."""
    monkeypatch.setattr(stale_config, "WINDOWS_SCAN_DISABLED", True)

    def _touched(*_args, **_kwargs):
        raise AssertionError("the Windows stale-config check touched the filesystem")

    for name in (
        "_agent_spec_files",
        "_read_json",
        "_spec_scope_unlistable",
        "_gated_dir",
        "_stat_spec_dir",
        "_gated_stat",
    ):
        monkeypatch.setattr(stale_config, name, _touched)


@pytest.mark.parametrize("project", ["", "/some/project"])
def test_windows_fingerprint_reads_nothing_and_is_unknown(project, monkeypatch):
    _windows(monkeypatch)

    fp = compute_fingerprint(SpawnInputs(agent="kirocrew", project=project), backend="kiro")

    assert {"spec", "global_mcp"} <= fp.unreadable
    assert ("workspace_mcp" in fp.unreadable) == bool(project)
    assert fp.tool_refs == frozenset()


def test_windows_input_signature_stats_nothing_and_is_constant(monkeypatch):
    _windows(monkeypatch)

    first = stale_config.input_signature(SpawnInputs(agent="kirocrew", project=""))
    other = stale_config.input_signature(SpawnInputs(agent="other", project="/some/project"))

    assert first == other == (("windows-scan", "disabled"),)


def test_windows_status_is_unknown_without_recomputing(tmp_path, monkeypatch):
    _windows(monkeypatch)
    state, slot = _status_slot(tmp_path, monkeypatch, recorded=_A)
    recompute = MagicMock(side_effect=AssertionError("recomputed"))
    monkeypatch.setattr(cs, "_recompute", recompute)

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out == {"stale": None, "changed": [], "unreadable": []}
    recompute.assert_not_called()
    cs.KiroCrewConfig.load.assert_not_called()


def test_two_windows_fingerprints_never_compare_stale(monkeypatch):
    _windows(monkeypatch)
    inputs = SpawnInputs(agent="kirocrew", project="/some/project")
    recorded = compute_fingerprint(inputs, backend="kiro")
    current = compute_fingerprint(inputs, backend="kiro")

    baseline = stale_config.adopt_first_reads(recorded, current)
    compared = stale_config.carry_forward(baseline, current)

    assert not is_stale(baseline, compared, hot_reloads=False)
    assert stale_config.changed_inputs(baseline, compared) == frozenset()

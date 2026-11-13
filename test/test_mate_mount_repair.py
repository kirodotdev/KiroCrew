"""Startup repair of a moved Bot's withheld server mounts (``mate_mount_repair``).

Pins that a Bot the migration moved gets back the ``@server`` its old agent
mounted through ``*``, with that agent read at the user level or, when it was a
Bot's private copy, where the move retired it; and that a literal selector the
user took out, a server the old agent never mounted, one its file defines
otherwise, and a Bot pointed back at its old agent stay as they are.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_worker
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.mate_config_migration import migrate_existing_crewmates

BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "worker base prompt",
    "tools": ["fs_read", "@kirocrew-core"],
    "allowedTools": ["fs_read"],
    "mcpServers": {"kirocrew-core": {"command": "kc", "args": ["core"]}},
}

JIRA = {"command": "jira-mcp", "args": ["--stdio"], "autoApprove": ["get_issue"]}


@pytest.fixture
def layout(monkeypatch, tmp_path):
    """A user-level agents dir holding the worker base, and its sibling mate dir."""
    kiro = tmp_path / "home"
    user = kiro / "agents"
    user.mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    _write(mates, "kirocrew-mate-worker", BASE)
    return user, mates


@pytest.fixture
def persisted(monkeypatch):
    """Config writes recorded instead of persisted; the shim serves *cfg* back."""
    saved: dict[str, dict] = {}

    def install(cfg: KiroCrewConfig) -> dict[str, dict]:
        monkeypatch.setattr(
            "kiro_crew.mate_config_migration.KiroCrewConfig",
            type("Shim", (), {"load": staticmethod(lambda: cfg)}),
        )

        def persist(c, name, **kw):
            saved[name] = kw

        monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", persist)
        return saved

    return install


def _write(directory: Path, name: str, spec: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _cfg(**agents: KiroCrewAgentConfig) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = dict(agents)
    return cfg


def _file(mates: Path, name: str) -> dict:
    return json.loads((mates / f"{name}.json").read_text(encoding="utf-8"))


def _ledger(mates: Path) -> dict:
    return json.loads((mates.parent / "migrated.json").read_text(encoding="utf-8"))


def _moved_before(user: Path, mates: Path, old: dict, mate_file: dict) -> KiroCrewConfig:
    """Bot ``zxc`` as an earlier start moved it off agent ``builder`` (*old*) onto *mate_file*."""
    _write(user, "builder", old)
    _write(mates, "zxc", mate_file)
    ledger = {
        "migrated": ["zxc"],
        "migrated_ids": {"zxc": "zxc"},
        "threads": {"zxc": {"from": "builder", "repointed": [], "done": True, "member_id": "zxc"}},
    }
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")
    return _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="zxc"))


_UNMOUNTED = {"name": "zxc", "mcpServers": {"jira": JIRA}, "tools": [], "allowedTools": []}


def test_a_moved_bot_gets_back_the_server_its_old_agent_mounted(layout, persisted):
    """Its file declares jira with no selector; the old agent's ``*`` mounted it: ``@jira`` is back."""
    from kiro_crew.mate_mount_repair import CHECKED_KEY

    user, mates = layout
    old = {"name": "builder", "tools": ["*"], "mcpServers": {"jira": JIRA}}
    cfg = _moved_before(user, mates, old, _UNMOUNTED)
    persisted(cfg)

    migrate_existing_crewmates(cfg)

    written = _file(mates, "zxc")
    assert written["tools"] == ["@jira"]
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, written))
    assert "@jira" in merged["tools"] and "jira" in merged["mcpServers"]
    assert _ledger(mates)[CHECKED_KEY] == ["zxc"]

    before = (mates / "zxc.json").read_bytes()
    migrate_existing_crewmates(cfg)
    assert (mates / "zxc.json").read_bytes() == before


@pytest.mark.parametrize("selector", ["@jira", "@jira/search"])
def test_a_literal_selector_the_user_took_out_stays_out(layout, persisted, selector):
    """The move kept the old agent's own ``@jira`` (or ``@jira/tool``); its absence is the user's."""
    from kiro_crew.mate_mount_repair import CHECKED_KEY, restore_withheld_mounts

    user, mates = layout
    old = {"name": "builder", "tools": [selector], "mcpServers": {"jira": JIRA}}
    cfg = _moved_before(user, mates, old, _UNMOUNTED)
    persisted(cfg)
    before = (mates / "zxc.json").read_bytes()

    assert restore_withheld_mounts(cfg) == []
    assert (mates / "zxc.json").read_bytes() == before
    assert _ledger(mates)[CHECKED_KEY] == ["zxc"]


def test_a_star_beside_a_literal_tool_selector_restores_the_whole_server(layout, persisted):
    """``*`` spells ``@jira``, which no entry of the old agent matched, so the move withheld it."""
    from kiro_crew.mate_mount_repair import restore_withheld_mounts

    user, mates = layout
    old = {"name": "builder", "tools": ["*", "@jira/search"], "mcpServers": {"jira": JIRA}}
    cfg = _moved_before(user, mates, old, _UNMOUNTED)
    persisted(cfg)

    assert restore_withheld_mounts(cfg) == ["zxc"]
    assert _file(mates, "zxc")["tools"] == ["@jira"]
    assert restore_withheld_mounts(cfg) == []


def test_a_server_the_old_agent_never_mounted_stays_unmounted(layout, persisted):
    from kiro_crew.mate_mount_repair import CHECKED_KEY, restore_withheld_mounts

    user, mates = layout
    old = {"name": "builder", "tools": ["fs_read"], "mcpServers": {"jira": JIRA}}
    cfg = _moved_before(user, mates, old, _UNMOUNTED)
    persisted(cfg)
    before = (mates / "zxc.json").read_bytes()

    assert restore_withheld_mounts(cfg) == []
    assert (mates / "zxc.json").read_bytes() == before
    assert _ledger(mates)[CHECKED_KEY] == ["zxc"]


def test_a_server_the_file_defines_unlike_the_old_agent_stays_unmounted(layout, persisted):
    """Same name, another command: not the old agent's server, so not its selector either."""
    from kiro_crew.mate_mount_repair import restore_withheld_mounts

    user, mates = layout
    old = {"name": "builder", "tools": ["*"], "mcpServers": {"jira": JIRA}}
    other = {**_UNMOUNTED, "mcpServers": {"jira": {**JIRA, "command": "other-jira-mcp"}}}
    cfg = _moved_before(user, mates, old, other)
    persisted(cfg)

    assert restore_withheld_mounts(cfg) == []
    assert _file(mates, "zxc")["tools"] == []


def test_a_bot_pointed_back_at_its_old_agent_is_not_repaired(layout, persisted):
    from kiro_crew.mate_mount_repair import restore_withheld_mounts

    user, mates = layout
    old = {"name": "builder", "tools": ["*"], "mcpServers": {"jira": JIRA}}
    cfg = _moved_before(user, mates, old, _UNMOUNTED)
    cfg.agents["zxc"].kiro_agent = "builder"
    persisted(cfg)

    assert restore_withheld_mounts(cfg) == []
    assert _file(mates, "zxc")["tools"] == []


_STAR = {"tools": ["*"], "mcpServers": {"jira": JIRA}}
_NO_STAR = {**_STAR, "tools": []}


@pytest.mark.parametrize(
    ("source", "copies", "tools"),
    [
        ("zxc", [_STAR], ["@jira"]),
        ("zxc", [_NO_STAR, _STAR], ["@jira"]),
        ("zxc", [_STAR, _NO_STAR], []),
        ("qaz", [_STAR], ["@jira"]),
    ],
    ids=["one-copy", "newest-mounted-it", "newest-did-not", "another-bot-s-copy"],
)
def test_a_bot_moved_off_a_retired_private_copy_gets_its_server_back(
    layout, persisted, source, copies, tools
):
    """Its old agent was a Bot's private copy, retired to ``mates/retired/``; the newest counts.

    An earlier move's copy keeps ``<source>.json``, so the next one is ``<source>.1.json``.
    A Bot that ran on another Bot's copy names that copy as its old agent too.
    """
    from kiro_crew.mate_mount_repair import CHECKED_KEY, restore_withheld_mounts

    user, mates = layout
    cfg = _moved_before(user, mates, {}, _UNMOUNTED)
    (user / "builder.json").unlink()
    retired = mates.parent / "retired"
    retired.mkdir()
    for stem, copy in zip([source, f"{source}.1"], copies):
        _write(retired, stem, {"name": source, **copy})
    ledger = _ledger(mates)
    ledger["threads"]["zxc"]["from"] = source
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")
    persisted(cfg)

    assert restore_withheld_mounts(cfg) == (["zxc"] if tools else [])
    assert _file(mates, "zxc")["tools"] == tools
    assert _ledger(mates)[CHECKED_KEY] == ["zxc"]
    assert restore_withheld_mounts(cfg) == []

"""Startup repair of a moved Bot's dropped restrictions (``mate_policy_repair``).

Pins that a Bot an earlier start moved without its old agent's deny rules gets
them back in its file, is told once in its chat, and is checked once; that a
restriction its worker cannot carry is held for a person while its worker still starts;
and that a Bot whose file already keeps them, or one pointed back by hand, is
left as it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_worker
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.mate_held_rules import HELD_KEY
from kiro_crew.mate_policy_repair import CHECKED_KEY, restore_dropped_policy

BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "worker base prompt",
    "tools": ["fs_read", "@kirocrew-core"],
    "allowedTools": ["fs_read"],
    "mcpServers": {"kirocrew-core": {"command": "kc", "args": ["core"]}},
}

DENIES = [
    {"capability": "shell", "effect": "deny"},
    {"capability": "fs_write", "effect": "deny", "match": ["/**"], "exclude": ["/mates/zxc/**"]},
]

#: The file an earlier release wrote: no ``permissions`` at all.
_MOVED_FILE = {"name": "zxc", "mcpServers": {}, "tools": [], "allowedTools": []}


@pytest.fixture
def layout(monkeypatch, tmp_path):
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
        shim = type("Shim", (), {"load": staticmethod(lambda: cfg)})
        monkeypatch.setattr("kiro_crew.mate_config_migration.KiroCrewConfig", shim)
        monkeypatch.setattr("kiro_crew.mate_policy_repair.KiroCrewConfig", shim)

        def persist(c, name, **kw):
            saved[name] = {**kw, "kiro_agent": c.agents[name].kiro_agent}

        monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", persist)
        return saved

    return install


def _write(directory: Path, name: str, spec: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _file(mates: Path, name: str) -> dict:
    return json.loads((mates / f"{name}.json").read_text(encoding="utf-8"))


def _ledger(mates: Path) -> dict:
    return json.loads((mates.parent / "migrated.json").read_text(encoding="utf-8"))


def _old(**extra) -> dict:
    return {
        "name": "crew-zxc",
        "prompt": BASE["prompt"],
        "tools": list(BASE["tools"]),
        "allowedTools": list(BASE["allowedTools"]),
        "mcpServers": json.loads(json.dumps(BASE["mcpServers"])),
        **extra,
    }


def _moved_before(user: Path, mates: Path, old: dict, mate_file: dict) -> KiroCrewConfig:
    """Bot ``zxc`` as an earlier start moved it off ``crew-zxc`` (*old*) onto *mate_file*."""
    _write(user, "crew-zxc", old)
    _write(mates, "zxc", mate_file)
    ledger = {
        "migrated": ["zxc"],
        "migrated_ids": {"zxc": "zxc"},
        "threads": {"zxc": {"from": "crew-zxc", "repointed": [], "done": True, "member_id": "zxc"}},
    }
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {"zxc": KiroCrewAgentConfig(member_id="zxc", kiro_agent="zxc")}
    return cfg


def _owed(name: str) -> tuple[str, bool]:
    from kiro_crew.mate_upgrade_notice import claim_policy_notice
    from kiro_crew.members import member_slug

    return claim_policy_notice(member_slug(name), name)


def test_a_bot_moved_without_its_deny_rules_gets_them_back_and_is_told(layout, persisted):
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions={"rules": DENIES}), _MOVED_FILE)
    saved = persisted(cfg)

    assert restore_dropped_policy(cfg) == ["zxc"]

    written = _file(mates, "zxc")
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, written))
    assert merged["permissions"]["rules"] == DENIES
    assert saved == {} and cfg.agents["zxc"].kiro_agent == "zxc"
    assert _ledger(mates)[CHECKED_KEY] == ["zxc"]
    assert HELD_KEY not in _ledger(mates)
    prompt, held = _owed("zxc")
    assert "permissions rule to deny shell" in prompt and "apply to its worker again" in prompt
    assert held is False
    assert _owed("zxc") == ("", False)

    before = (mates / "zxc.json").read_bytes()
    assert restore_dropped_policy(cfg) == []
    assert (mates / "zxc.json").read_bytes() == before


def test_a_file_that_already_keeps_the_rules_is_left_as_it_is(layout, persisted):
    user, mates = layout
    cfg = _moved_before(
        user,
        mates,
        _old(permissions={"rules": DENIES}),
        {**_MOVED_FILE, "permissions": {"rules": DENIES}},
    )
    persisted(cfg)
    before = (mates / "zxc.json").read_bytes()

    assert restore_dropped_policy(cfg) == []
    assert (mates / "zxc.json").read_bytes() == before
    assert _ledger(mates)[CHECKED_KEY] == ["zxc"] and _owed("zxc") == ("", False)


def test_a_limit_the_worker_cannot_carry_is_held_and_the_worker_still_starts(layout, persisted):
    """Never a startup failure: the rest is carried, the odd one asks a person."""
    user, mates = layout
    odd = {"execute_bash": {"denyByDefault": True}}
    cfg = _moved_before(
        user, mates, _old(toolsSettings=odd, permissions={"rules": DENIES}), _MOVED_FILE
    )
    persisted(cfg)

    assert restore_dropped_policy(cfg) == ["zxc"]

    written = _file(mates, "zxc")
    assert "toolsSettings" not in written
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, written))
    rules = merged["permissions"]["rules"]
    assert all(r in rules for r in DENIES)
    assert {"capability": "shell", "effect": "ask"} in rules
    held = _ledger(mates)[HELD_KEY]["zxc"]
    assert [r["capability"] for r in held["rules"]] == ["shell"] and held["member_id"] == "zxc"
    prompt, bell = _owed("zxc")
    assert bell is True and "asks you before" in prompt


def test_a_bot_whose_worker_is_not_on_kas_gets_every_limit_held_and_none_written(
    layout, persisted, monkeypatch
):
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions={"rules": DENIES}), _MOVED_FILE)
    persisted(cfg)
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda c, n=None: "kiro-cli")

    assert restore_dropped_policy(cfg) == ["zxc"]

    assert _file(mates, "zxc") == _MOVED_FILE
    caps = {r["capability"] for r in _ledger(mates)[HELD_KEY]["zxc"]["rules"]}
    assert caps == {"shell", "fs_write"}
    assert _owed("zxc")[1] is True


def test_a_bot_pointed_back_by_hand_is_not_touched(layout, persisted):
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions={"rules": DENIES}), _MOVED_FILE)
    cfg.agents["zxc"].kiro_agent = "crew-zxc"
    saved = persisted(cfg)
    before = (mates / "zxc.json").read_bytes()

    assert restore_dropped_policy(cfg) == []
    assert (mates / "zxc.json").read_bytes() == before and saved == {}


def test_an_unreadable_old_agent_is_checked_again_at_the_next_start(layout, persisted):
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions={"rules": DENIES}), _MOVED_FILE)
    (user / "crew-zxc.json").write_text("{not json", encoding="utf-8")
    persisted(cfg)

    assert restore_dropped_policy(cfg) == []
    assert CHECKED_KEY not in _ledger(mates)


def test_the_startup_migration_runs_the_repair(layout, persisted):
    from kiro_crew.mate_config_migration import migrate_existing_crewmates

    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions={"rules": DENIES}), _MOVED_FILE)
    persisted(cfg)

    migrate_existing_crewmates(cfg)

    assert _file(mates, "zxc")["permissions"] == {"rules": DENIES}


def test_array_form_hooks_the_file_lacks_are_put_back_or_held(layout, persisted):
    user, mates = layout
    hooks = [{"event": "preToolUse", "matcher": "execute_bash", "command": "guard"}]
    cfg = _moved_before(user, mates, _old(hooks=hooks), _MOVED_FILE)
    persisted(cfg)

    assert restore_dropped_policy(cfg) == ["zxc"]

    assert _ledger(mates)[HELD_KEY]["zxc"]["rules"]


def test_a_bot_the_migration_moves_now_is_not_repaired_at_the_next_start(layout, persisted):
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.mate_config_migration import migrate_existing_crewmates

    user, mates = layout
    _write(user, "crew-qwe", _old(toolsSettings={"execute_bash": {"denyByDefault": True}}))
    cfg = KiroCrewConfig()
    cfg.agents = {"qwe": KiroCrewAgentConfig(member_id="qwe", kiro_agent="crew-qwe")}
    persisted(cfg)

    migrate_existing_crewmates(cfg)

    assert "qwe" in _ledger(mates)[CHECKED_KEY]

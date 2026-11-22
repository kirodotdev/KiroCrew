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


def test_an_old_agent_whose_tool_settings_only_grant_owes_no_notice(layout, persisted):
    """An ``allowedCommands`` / ``allowedPaths`` entry limits nothing, so the repair
    neither claims it puts one back nor tells the user earlier work ran without it."""
    user, mates = layout
    grants = {
        "execute_bash": {"allowedCommands": ["git *"]},
        "fs_write": {"allowedPaths": ["/work/**"]},
    }
    cfg = _moved_before(user, mates, _old(toolsSettings=grants), _MOVED_FILE)
    persisted(cfg)
    before = (mates / "zxc.json").read_bytes()

    assert restore_dropped_policy(cfg) == []
    assert (mates / "zxc.json").read_bytes() == before
    assert _owed("zxc") == ("", False)
    assert HELD_KEY not in _ledger(mates)


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
    odd = {"execute_bash": {"fallbackAction": "deny"}}
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
    _write(user, "crew-qwe", _old(toolsSettings={"execute_bash": {"fallbackAction": "deny"}}))
    cfg = KiroCrewConfig()
    cfg.agents = {"qwe": KiroCrewAgentConfig(member_id="qwe", kiro_agent="crew-qwe")}
    persisted(cfg)

    migrate_existing_crewmates(cfg)

    assert "qwe" in _ledger(mates)[CHECKED_KEY]


def owner_edit_lands_as_the_lock_is_taken(monkeypatch, mates: Path, name: str) -> None:
    """Make the owner's Abilities save of Bot *name* land just before a repair takes
    the Bot-file lock (``agents_spec_lock`` on the mate dir), as that save would
    when it holds the lock first. A repair that read the file outside the lock
    writes its older copy back and loses the save."""
    import contextlib

    import kiro_crew.agent as agent_mod

    real = agent_mod.agents_spec_lock
    landed: list[bool] = []

    @contextlib.contextmanager
    def lock(agents_dir: Path):
        if Path(agents_dir) == mates and not landed:
            landed.append(True)
            path = mates / f"{name}.json"
            spec = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps({**spec, "description": "owner edit"}), encoding="utf-8")
        with real(agents_dir):
            yield

    monkeypatch.setattr(agent_mod, "agents_spec_lock", lock)


def test_an_owner_edit_made_while_the_repair_runs_is_kept(layout, persisted, monkeypatch):
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions={"rules": DENIES}), _MOVED_FILE)
    persisted(cfg)
    owner_edit_lands_as_the_lock_is_taken(monkeypatch, mates, "zxc")

    assert restore_dropped_policy(cfg) == ["zxc"]

    written = _file(mates, "zxc")
    assert written["description"] == "owner edit"
    assert written["permissions"]["rules"]


_SHELL_BOTH = {
    "execute_bash": {"deniedCommands": ["rm -rf .*", "sudo .*"], "autoAllowReadonly": True},
    "shell": {"deniedCommands": ["rm -rf .*", "sudo .*"]},
}
_SHELL_FOLDED = {"execute_bash": {"deniedCommands": ["rm -rf .*", "sudo .*"]}}


def test_a_bot_whose_old_agent_wrote_both_shell_spellings_gets_its_denies_back_and_no_hold(
    layout, persisted
):
    user, mates = layout
    cfg = _moved_before(user, mates, _old(toolsSettings=_SHELL_BOTH), _MOVED_FILE)
    persisted(cfg)

    assert restore_dropped_policy(cfg) == ["zxc"]

    written = _file(mates, "zxc")
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, written))
    assert merged["toolsSettings"] == _SHELL_FOLDED
    assert HELD_KEY not in _ledger(mates)
    assert _owed("zxc")[1] is False


def test_a_bot_that_already_carries_the_folded_shell_entry_is_not_repaired_again(layout, persisted):
    user, mates = layout
    carried = {"toolsSettings": _SHELL_FOLDED}
    cfg = _moved_before(user, mates, _old(toolsSettings=_SHELL_BOTH), {**_MOVED_FILE, **carried})
    persisted(cfg)
    before = (mates / "zxc.json").read_bytes()

    assert restore_dropped_policy(cfg) == []

    assert (mates / "zxc.json").read_bytes() == before
    assert HELD_KEY not in _ledger(mates)


#: An old agent's block that names a bundle beside its rules (KAS expands the bundle
#: into allow rules on its own side) and denies one capability.
_BUNDLE_BLOCK = {
    "policies": ["read-only-shell"],
    "rules": [{"capability": "subagent", "effect": "deny", "match": ["*"]}],
}


def test_a_deny_beside_a_named_bundle_is_carried_and_nothing_asks_on_every_call(layout, persisted):
    """A bundle is a grant: the deny beside it is carried, and the Bot keeps its Trust."""
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), _MOVED_FILE)
    persisted(cfg)

    assert restore_dropped_policy(cfg) == ["zxc"]

    written = _file(mates, "zxc")
    assert written["permissions"] == {"rules": _BUNDLE_BLOCK["rules"]}
    assert "zxc" not in (_ledger(mates).get(HELD_KEY) or {})
    assert _owed("zxc")[1] is False


def test_a_bot_off_kas_holds_a_deny_beside_a_bundle_under_its_own_capability(
    layout, persisted, monkeypatch
):
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), _MOVED_FILE)
    persisted(cfg)
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda c, n=None: "kiro-cli")

    assert restore_dropped_policy(cfg) == ["zxc"]

    caps = {r["capability"] for r in _ledger(mates)[HELD_KEY]["zxc"]["rules"]}
    assert caps == {"subagent"}


def test_a_bot_an_earlier_start_held_on_every_call_is_rechecked_once(
    layout, persisted, monkeypatch
):
    """The hold an earlier release recorded for a bundle block is replaced, once."""
    from kiro_crew import mate_held_rules
    from kiro_crew.mate_held_rules import ANY, held_for
    from kiro_crew.mate_policy_repair import RESPLIT_KEY
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

    monkeypatch.setattr(mate_held_rules, "_member_of", lambda agent: agent)
    user, mates = layout
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), _MOVED_FILE)
    persisted(cfg)
    ledger = _ledger(mates)
    ledger[CHECKED_KEY] = ["zxc"]
    ledger[HELD_KEY] = {
        "zxc": {
            "member_id": "zxc",
            "rules": [{"text": "its permissions rule to deny subagent (*)", "capability": ANY}],
        }
    }
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")
    shell_call = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST, title="ls", request_id="r1", tool_kind="execute"
    )
    assert held_for(shell_call, agent="zxc")

    assert restore_dropped_policy(cfg) == ["zxc"]

    after = _ledger(mates)
    assert "zxc" not in (after.get(HELD_KEY) or {})
    assert after[RESPLIT_KEY] == ["zxc"]
    assert _file(mates, "zxc")["permissions"] == {"rules": _BUNDLE_BLOCK["rules"]}
    assert held_for(shell_call, agent="zxc") == []
    # Once: the next start leaves the Bot alone.
    assert restore_dropped_policy(cfg) == []


def _checked_every_call_hold(mates: Path) -> list[dict[str, str]]:
    from kiro_crew.mate_held_rules import ANY

    rules = [{"text": "its permissions rule to deny subagent (*)", "capability": ANY}]
    ledger = _ledger(mates)
    ledger[CHECKED_KEY] = ["zxc"]
    ledger[HELD_KEY] = {"zxc": {"member_id": "zxc", "rules": rules}}
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")
    return rules


def test_recheck_keeps_the_old_hold_when_the_deny_write_fails(layout, persisted, monkeypatch):
    from kiro_crew.mate_config import MateConfigError
    from kiro_crew.mate_policy_repair import RESPLIT_KEY

    user, mates = layout
    cfg = _moved_before(
        user,
        mates,
        _old(
            permissions=_BUNDLE_BLOCK,
            toolsSettings={"execute_bash": {"fallbackAction": "deny"}},
        ),
        _MOVED_FILE,
    )
    persisted(cfg)
    old_rules = _checked_every_call_hold(mates)
    writes = []

    def fail_write(name, spec):
        writes.append(spec)
        raise MateConfigError("deny write failed")

    monkeypatch.setattr("kiro_crew.mate_policy_repair.write_mate_spec", fail_write)

    assert restore_dropped_policy(cfg) == []

    assert writes and writes[0]["permissions"]["rules"]
    after = _ledger(mates)
    assert all(rule in after[HELD_KEY]["zxc"]["rules"] for rule in old_rules)
    assert "zxc" not in after.get(RESPLIT_KEY, [])
    assert _file(mates, "zxc") == _MOVED_FILE


def test_recheck_clears_the_hold_when_the_file_already_carries_the_deny(layout, persisted):
    from kiro_crew.mate_policy_repair import RESPLIT_KEY

    user, mates = layout
    carried = {**_MOVED_FILE, "permissions": {"rules": _BUNDLE_BLOCK["rules"]}}
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), carried)
    persisted(cfg)
    _checked_every_call_hold(mates)

    assert restore_dropped_policy(cfg) == []

    after = _ledger(mates)
    assert "zxc" not in after.get(HELD_KEY, {})
    assert after[RESPLIT_KEY] == ["zxc"]
    assert _file(mates, "zxc") == carried


def test_recheck_retries_when_clearing_the_hold_fails(layout, persisted, monkeypatch):
    from kiro_crew.mate_policy_repair import RESPLIT_KEY
    from kiro_crew.mate_restriction_carry import _hold

    user, mates = layout
    carried = {**_MOVED_FILE, "permissions": {"rules": _BUNDLE_BLOCK["rules"]}}
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), carried)
    persisted(cfg)
    old_rules = _checked_every_call_hold(mates)
    monkeypatch.setattr(
        "kiro_crew.mate_policy_repair._hold",
        lambda name, held: _hold(name, held) if held else False,
        raising=False,
    )

    assert restore_dropped_policy(cfg) == []

    after = _ledger(mates)
    assert "zxc" not in after.get(RESPLIT_KEY, [])
    assert after[HELD_KEY]["zxc"]["rules"] == old_rules
    assert _file(mates, "zxc") == carried


@pytest.mark.parametrize(
    "extra",
    [
        {"permissions": "strict"},
        {"permissions": {"defaultEffect": "deny"}},
        {"toolsSettings": ["execute_bash"]},
        {"hooks": "x"},
    ],
)
def test_a_recheck_keeps_an_every_call_hold_that_is_not_a_bundle(layout, persisted, extra):
    """Only a bundle beside its rules is read again; a limit that cannot be carried stays held."""
    from kiro_crew.mate_held_rules import ANY
    from kiro_crew.mate_policy_repair import RESPLIT_KEY

    user, mates = layout
    cfg = _moved_before(user, mates, _old(**extra), _MOVED_FILE)
    persisted(cfg)
    hold = [{"text": "one of its limits", "capability": ANY}]
    ledger = _ledger(mates)
    ledger[CHECKED_KEY] = ["zxc"]
    ledger[HELD_KEY] = {"zxc": {"member_id": "zxc", "rules": hold}}
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")

    assert restore_dropped_policy(cfg) == []

    after = _ledger(mates)
    assert after[HELD_KEY]["zxc"]["rules"] == hold
    assert after[RESPLIT_KEY] == ["zxc"]


def test_an_unchecked_bot_with_an_every_call_hold_still_gets_its_first_repair(layout, persisted):
    """A hold left by a failed first write is no reason to skip writing the deny."""
    from kiro_crew.mate_held_rules import ANY
    from kiro_crew.mate_policy_repair import RESPLIT_KEY

    user, mates = layout
    hook = {"preToolUse": [{"matcher": "*", "command": "guard"}]}
    cfg = _moved_before(user, mates, _old(hooks=hook, permissions={"rules": DENIES}), _MOVED_FILE)
    persisted(cfg)
    ledger = _ledger(mates)
    ledger[HELD_KEY] = {
        "zxc": {"member_id": "zxc", "rules": [{"text": "its preToolUse hook", "capability": ANY}]}
    }
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")

    assert restore_dropped_policy(cfg) == ["zxc"]

    written = _file(mates, "zxc")
    assert all(r in written["permissions"]["rules"] for r in DENIES)
    after = _ledger(mates)
    assert after[CHECKED_KEY] == ["zxc"]
    assert "zxc" not in (after.get(RESPLIT_KEY) or [])


def test_a_recheck_drops_the_asks_an_every_call_hold_wrote_into_the_file(layout, persisted):
    """Those asks outrank the user's own allow rules, so reads asked even off Trust."""
    from kiro_crew.mate_held_rules import _ASKABLE, ANY

    user, mates = layout
    asks = [{"capability": c, "effect": "ask"} for c in sorted(_ASKABLE)]
    own_ask = {"capability": "web_fetch", "effect": "ask"}
    block = {"policies": ["read-only-shell"], "rules": [*_BUNDLE_BLOCK["rules"], own_ask]}
    moved = {**_MOVED_FILE, "permissions": {"rules": asks}}
    cfg = _moved_before(user, mates, _old(permissions=block), moved)
    persisted(cfg)
    ledger = _ledger(mates)
    ledger[CHECKED_KEY] = ["zxc"]
    ledger[HELD_KEY] = {
        "zxc": {"member_id": "zxc", "rules": [{"text": "its permissions", "capability": ANY}]}
    }
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")

    restore_dropped_policy(cfg)

    rules = _file(mates, "zxc")["permissions"]["rules"]
    assert {"capability": "fs_read", "effect": "ask"} not in rules
    assert {"capability": "shell", "effect": "ask"} not in rules
    assert own_ask in rules
    assert _BUNDLE_BLOCK["rules"][0] in rules
    assert "zxc" not in (_ledger(mates).get(HELD_KEY) or {})


def _held_on_every_call(mates: Path, *, checked: list[str], rechecked: list[str] = ()) -> None:
    from kiro_crew.mate_held_rules import ANY
    from kiro_crew.mate_policy_repair import RESPLIT_KEY

    ledger = _ledger(mates)
    ledger[CHECKED_KEY] = checked
    if rechecked:
        ledger[RESPLIT_KEY] = list(rechecked)
    else:
        ledger[HELD_KEY] = {
            "zxc": {"member_id": "zxc", "rules": [{"text": "its permissions", "capability": ANY}]}
        }
    (mates.parent / "migrated.json").write_text(json.dumps(ledger), encoding="utf-8")


def test_a_recheck_keeps_the_asks_an_owner_pruned_the_hold_down_to(layout, persisted):
    """Only the whole set the hold wrote is removed; a pruned set is the owner's choice."""
    user, mates = layout
    kept = [
        {"capability": "shell", "effect": "ask"},
        {"capability": "fs_write", "effect": "ask", "match": ["/etc/**"]},
    ]
    moved = {**_MOVED_FILE, "permissions": {"rules": kept}}
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), moved)
    persisted(cfg)
    _held_on_every_call(mates, checked=["zxc"])

    restore_dropped_policy(cfg)

    rules = _file(mates, "zxc")["permissions"]["rules"]
    assert all(r in rules for r in kept)


def test_a_bot_an_earlier_recheck_left_with_the_asks_is_cleaned_once(layout, persisted):
    """A recheck that dropped the hold but kept the asks is followed by one that removes them."""
    from kiro_crew.mate_held_rules import _ASKABLE
    from kiro_crew.mate_policy_repair import ASKS_KEY

    user, mates = layout
    asks = [{"capability": c, "effect": "ask"} for c in sorted(_ASKABLE)]
    moved = {**_MOVED_FILE, "permissions": {"rules": [*asks, *_BUNDLE_BLOCK["rules"]]}}
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), moved)
    persisted(cfg)
    _held_on_every_call(mates, checked=["zxc"], rechecked=["zxc"])

    restore_dropped_policy(cfg)

    rules = _file(mates, "zxc")["permissions"]["rules"]
    assert not any(r in rules for r in asks)
    assert _BUNDLE_BLOCK["rules"][0] in rules
    assert _ledger(mates)[ASKS_KEY] == ["zxc"]
    before = (mates / "zxc.json").read_bytes()
    restore_dropped_policy(cfg)
    assert (mates / "zxc.json").read_bytes() == before


def test_a_bot_never_held_on_every_call_keeps_a_full_set_of_asks(layout, persisted):
    """A whole set of bare asks on a Bot no every-call hold touched is its owner's."""
    from kiro_crew.mate_held_rules import _ASKABLE

    user, mates = layout
    asks = [{"capability": c, "effect": "ask"} for c in sorted(_ASKABLE)]
    moved = {**_MOVED_FILE, "permissions": {"rules": [*asks, *_BUNDLE_BLOCK["rules"]]}}
    cfg = _moved_before(user, mates, _old(permissions=_BUNDLE_BLOCK), moved)
    persisted(cfg)
    _held_on_every_call(mates, checked=["zxc"], rechecked=["other"])
    before = (mates / "zxc.json").read_bytes()

    assert restore_dropped_policy(cfg) == []

    assert (mates / "zxc.json").read_bytes() == before

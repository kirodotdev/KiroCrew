"""Moving existing crewmates onto their worker and their own file (RFC §6).

Pins what the migration carries (the old agent's prompt, servers, approvals and
skills beyond the worker base, nothing the base already has), where it reads
from (a shared template or the crewmate's private ``crew-<hex>`` copy), that the
dry-run keeps a crewmate whose worker would grant anything else on its old
binding, that carried capabilities read as already there, and that a second run
changes nothing.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_capability_catalog, mate_worker
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.mate_config_migration import _read_back, migrate_existing_crewmates
from kiro_crew.mate_worker_kit import read_record

BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "worker base prompt",
    "tools": ["fs_read", "@kirocrew-core"],
    "allowedTools": ["fs_read"],
    "mcpServers": {"kirocrew-core": {"command": "kc", "args": ["core"]}},
}

JIRA = {"command": "jira-mcp", "args": ["--stdio"], "autoApprove": ["get_issue"]}


def _template(name: str, **extra) -> dict:
    """An agent as a template carries it: the base's own parts plus *extra*."""
    spec = {
        "name": name,
        "prompt": BASE["prompt"],
        "tools": list(BASE["tools"]),
        "allowedTools": list(BASE["allowedTools"]),
        "mcpServers": json.loads(json.dumps(BASE["mcpServers"])),
    }
    for key, value in extra.items():
        if key in ("tools", "allowedTools"):
            spec[key] = [*spec[key], *value]
        elif key == "mcpServers":
            spec[key].update(value)
        else:
            spec[key] = value
    return spec


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


def test_a_shared_template_crewmate_carries_its_kit_and_nothing_the_base_has(layout, persisted):
    user, mates = layout
    _write(
        user,
        "builder",
        _template(
            "builder",
            prompt="Follow the release SOP.",
            tools=["@jira"],
            allowedTools=["@jira/search"],
            mcpServers={"jira": JIRA},
            skills=["skill://release"],
        ),
    )
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    saved = persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["zxc"]
    written = _file(mates, "zxc")
    assert written == {
        "name": "zxc",
        "prompt": "Follow the release SOP.",
        "mcpServers": {"jira": JIRA},
        "tools": ["@jira"],
        "allowedTools": ["@jira/search"],
        "skills": ["skill://release"],
    }
    assert cfg.agents["zxc"].kiro_agent == "zxc"
    assert saved["zxc"]["changed_fields"] == {"kiro_agent"}
    # Compare-and-set on what the pass planned from, so a switch made meanwhile wins.
    assert saved["zxc"]["expected_values"] == {"kiro_agent": "builder"}
    assert read_record("zxc")["template"] == "builder"
    assert (user / "builder.json").is_file()  # kept, so the binding can point back


def test_a_template_switched_during_the_pass_is_not_overwritten(layout, persisted, monkeypatch):
    user, _ = layout
    _write(user, "builder", _template("builder", prompt="Follow the release SOP."))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    from kiro_crew.memory_stores import MemberFieldConflict

    def switched(c, name, **kw):
        # On disk the owner picked another template after the pass read "builder".
        raise MemberFieldConflict("kiro_agent")

    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", switched)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert "zxc" in report.repoint_failed


def test_a_crew_hex_crewmate_carries_its_private_copy(layout, persisted):
    user, mates = layout
    _write(user, "builder", _template("builder"))
    _write(
        user,
        "crew-0a1b2c",
        _template(
            "crew-0a1b2c",
            prompt="Edited on the Capabilities page.",
            mcpServers={"jira": JIRA},
            tools=["@jira"],
        ),
    )
    cfg = _cfg(qwe=KiroCrewAgentConfig(member_id="qwe", kiro_agent="crew-0a1b2c"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["qwe"]
    written = _file(mates, "qwe")
    assert written["prompt"] == "Edited on the Capabilities page."
    assert set(written["mcpServers"]) == {"jira"}
    assert read_record("qwe")["template"] == "crew-0a1b2c"
    assert (user / "crew-0a1b2c.json").is_file()


def test_a_default_template_crewmate_gets_a_file_that_adds_nothing(layout, persisted):
    user, mates = layout
    _write(user, "kirocrew", _template("kirocrew"))
    cfg = _cfg(rty=KiroCrewAgentConfig(member_id="rty", kiro_agent="kirocrew"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["rty"]
    written = _file(mates, "rty")
    assert written == {"name": "rty", "mcpServers": {}, "tools": [], "allowedTools": []}
    additions = mate_worker.worker_additions(BASE, _read_back("rty"))
    assert not mate_worker.has_additions(additions)
    assert read_record("rty") is None


def test_a_crewmate_whose_worker_would_differ_is_skipped_and_logged(layout, persisted, caplog):
    user, mates = layout
    # A built-in tool the worker base does not mount cannot be carried.
    _write(user, "shell", _template("shell", tools=["execute_bash"]))
    cfg = _cfg(uio=KiroCrewAgentConfig(member_id="uio", kiro_agent="shell", display_name="Ops Pal"))
    saved = persisted(cfg)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mate_config_migration"):
        report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert "execute_bash" in report.dry_run_failed["uio"]
    assert cfg.agents["uio"].kiro_agent == "shell"
    assert saved == {}
    assert not (mates / "uio.json").exists()
    assert any(r.levelno == logging.WARNING and "Ops Pal" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "field, limit",
    [
        ("toolsSettings", {"fs_write": {"deniedPaths": ["/etc/**"]}}),
        ("permissions", {"rules": [{"capability": "fs_write", "effect": "deny"}]}),
    ],
)
def test_a_crewmate_whose_template_denies_writes_is_not_moved(layout, persisted, field, limit):
    """The worker merge carries neither field, so the template's deny would be lost."""
    user, mates = layout
    _write(user, "careful", _template("careful", **{field: limit}))
    cfg = _cfg(ijk=KiroCrewAgentConfig(member_id="ijk", kiro_agent="careful"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert field in report.dry_run_failed["ijk"]
    assert cfg.agents["ijk"].kiro_agent == "careful"
    assert not (mates / "ijk.json").exists()


def test_allow_only_permissions_are_grants_not_a_restriction(layout, persisted):
    user, _mates = layout
    allow = {"rules": [{"capability": "web_search", "effect": "allow"}]}
    _write(user, "kirocrew", _template("kirocrew", permissions=allow))
    cfg = _cfg(rty=KiroCrewAgentConfig(member_id="rty", kiro_agent="kirocrew"))
    persisted(cfg)

    assert migrate_existing_crewmates(cfg).migrated == ["rty"]


def test_a_second_run_changes_nothing(layout, monkeypatch):
    user, mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    monkeypatch.setattr(
        "kiro_crew.mate_config_migration.KiroCrewConfig",
        type("Shim", (), {"load": staticmethod(lambda: cfg)}),
    )
    writes: list[str] = []

    def persist(c, name, **kw):
        writes.append(name)

    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", persist)

    assert migrate_existing_crewmates(cfg).migrated == ["zxc"]
    before = (mates / "zxc.json").read_bytes()
    second = migrate_existing_crewmates(cfg)

    assert second.migrated == [] and second.skipped_no_source == []
    assert second.dry_run_failed == {} and second.copy_failed == {}
    assert (mates / "zxc.json").read_bytes() == before
    assert writes == ["zxc"]


def test_a_crewmate_already_on_its_own_file_is_not_a_candidate(layout, persisted):
    _user, mates = layout
    _write(mates, "qwe", {"name": "qwe", "prompt": "already mine"})
    cfg = _cfg(qwe=KiroCrewAgentConfig(member_id="qwe", kiro_agent="qwe"))
    saved = persisted(cfg)
    before = (mates / "qwe.json").read_bytes()

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == [] and report.skipped_no_source == []
    assert (mates / "qwe.json").read_bytes() == before
    assert saved == {}


def test_a_crewmate_pointed_back_by_hand_stays_put(layout, persisted):
    user, mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    saved = persisted(cfg)
    assert migrate_existing_crewmates(cfg).migrated == ["zxc"]

    cfg.agents["zxc"].kiro_agent = "builder"  # the documented rollback
    saved.clear()
    again = migrate_existing_crewmates(cfg)

    assert again.migrated == []
    assert cfg.agents["zxc"].kiro_agent == "builder"
    assert saved == {}


def test_carried_capabilities_read_as_already_there(layout, persisted, monkeypatch):
    """The catalog the card path refuses ``already_has`` from lists what was carried."""
    user, _mates = layout
    _write(
        user,
        "builder",
        _template(
            "builder",
            tools=["@jira"],
            allowedTools=["@jira/search"],
            mcpServers={"jira": JIRA},
        ),
    )
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    assert migrate_existing_crewmates(cfg).migrated == ["zxc"]
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, config=None: member)
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])

    spec = mate_worker.resolve_worker_spec(dict(BASE), "zxc")
    has = mate_capability_catalog.worker_has(spec, {})

    jira = next(s for s in has["servers"] if s["name"] == "jira")
    assert jira["runs_without_asking"] == ["get_issue", "search"]


AUDIT_HOOK = {"postToolUse": [{"matcher": "execute_bash", "command": "audit-to-log"}]}


def _installed_layout(user: Path) -> dict:
    """The worker base the way Crew installs it: its own hooks and its withheld tools."""
    base = {
        **json.loads(json.dumps(BASE)),
        "tools": [*BASE["tools"], "@kirocrew-mate"],
        "allowedTools": [*BASE["allowedTools"], "@kirocrew-mate/request_capabilities"],
        "mcpServers": {**BASE["mcpServers"], "kirocrew-mate": {"command": "kc", "args": ["mate"]}},
        "hooks": AUDIT_HOOK,
        "excludedTools": ["@kirocrew-mate/request_capabilities"],
    }
    _write(user.parent / "mates" / "agents", "kirocrew-mate-worker", base)
    return base


def test_the_default_template_as_installed_moves_onto_the_worker(layout, persisted):
    """The template carries the same audit hook and the conductor-only approval the base withholds."""
    user, mates = layout
    base = _installed_layout(user)
    template = {k: v for k, v in base.items() if k != "excludedTools"}
    _write(user, "kirocrew", {**template, "name": "kirocrew"})
    cfg = _cfg(rty=KiroCrewAgentConfig(member_id="rty", kiro_agent="kirocrew"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.dry_run_failed == {}
    assert report.migrated == ["rty"]
    assert "hooks" not in _file(mates, "rty")


def test_a_hook_the_worker_base_lacks_still_keeps_the_crewmate_where_it_is(layout, persisted):
    user, mates = layout
    base = _installed_layout(user)
    hooks = {"postToolUse": [*AUDIT_HOOK["postToolUse"], {"matcher": "fs_write", "command": "x"}]}
    _write(user, "builder", {**base, "name": "builder", "hooks": hooks, "excludedTools": []})
    cfg = _cfg(uio=KiroCrewAgentConfig(member_id="uio", kiro_agent="builder"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert "hooks" in report.dry_run_failed["uio"]
    assert not (mates / "uio.json").exists()


def _ray_kirocrew(base: dict) -> dict:
    """Ray's live ``kirocrew``: the base plus approvals on servers the base owns.

    ``@kirocrew-cron`` whole, ``@kirocrew-guide/rename_self``, and Rust Lead's
    ``@kirocrew-core/ask_question`` / ``autonudge_stop`` and ``global_*`` recalls.
    """
    template = {k: v for k, v in base.items() if k != "excludedTools"}
    template["mcpServers"] = json.loads(json.dumps(template["mcpServers"]))
    template["mcpServers"]["kirocrew-core"]["autoApprove"] = ["ask_question", "autonudge_stop"]
    template["mcpServers"]["kirocrew-cron"] = {"command": "kc", "args": ["cron"]}
    template["mcpServers"]["kirocrew-guide"] = {"command": "kc", "args": ["guide"]}
    template["tools"] = [*template["tools"], "@kirocrew-cron", "@kirocrew-guide"]
    template["allowedTools"] = [
        *template["allowedTools"],
        "@kirocrew-cron",
        "@kirocrew-guide/rename_self",
        "@kirocrew-guide/global_memory_recall",
        "@kirocrew-guide/global_preference_add",
        "execute_bash",
    ]
    return template


@pytest.fixture
def platform_servers(monkeypatch):
    """The platform servers the gateway runs, as ``mate_worker.conductor_mcp_servers`` reads them."""
    names = frozenset({"kirocrew-core", "kirocrew-cron", "kirocrew-guide", "kirocrew-mate"})
    monkeypatch.setattr(mate_worker, "conductor_mcp_servers", lambda: names)
    return names


def test_approvals_the_base_withholds_on_its_own_servers_are_no_loss(
    layout, persisted, platform_servers
):
    """Ray's 11 crewmates: every one was skipped for exactly these, and every one moves."""
    user, mates = layout
    base = _installed_layout(user)
    # As installed on Ray's live: the base declares the cron and guide servers itself.
    base["mcpServers"]["kirocrew-cron"] = {"command": "kc", "args": ["cron"]}
    base["mcpServers"]["kirocrew-guide"] = {"command": "kc", "args": ["guide"]}
    base["tools"] = [*base["tools"], "@kirocrew-cron", "@kirocrew-guide"]
    _write(user.parent / "mates" / "agents", "kirocrew-mate-worker", base)
    _write(user, "kirocrew", {**_ray_kirocrew(base), "name": "kirocrew"})
    cfg = _cfg(josh=KiroCrewAgentConfig(member_id="josh", kiro_agent="kirocrew"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.dry_run_failed == {}
    assert report.migrated == ["josh"]
    # Nothing the base owns is copied into the file to "keep" it.
    assert "@kirocrew-guide/rename_self" not in _file(mates, "josh").get("allowedTools", [])


def test_an_extra_server_still_moves_and_is_kept(layout, persisted, platform_servers):
    user, mates = layout
    base = _installed_layout(user)
    template = _ray_kirocrew(base)
    template["mcpServers"]["jira"] = JIRA
    template["tools"] = [*template["tools"], "@jira"]
    template["allowedTools"] = [*template["allowedTools"], "@jira/search"]
    _write(user, "reviewer", {**template, "name": "reviewer"})
    cfg = _cfg(rex=KiroCrewAgentConfig(member_id="rex", kiro_agent="reviewer"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["rex"]
    written = _file(mates, "rex")
    assert written["mcpServers"]["jira"] == JIRA
    assert "@jira/search" in written["allowedTools"]


def test_a_real_loss_still_keeps_the_crewmate_where_it_is(layout, platform_servers):
    """A grant on a server the base does not own, which the planned file drops."""
    from kiro_crew.mate_config_migration import dry_run, migration_file

    user, _mates = layout
    base = _installed_layout(user)
    source = _ray_kirocrew(base)
    source["mcpServers"]["jira"] = JIRA
    source["tools"] = [*source["tools"], "@jira"]
    source["allowedTools"] = [*source["allowedTools"], "@jira/search"]
    planned = migration_file("uio", base, source)
    planned["allowedTools"] = [r for r in planned["allowedTools"] if r != "@jira/search"]

    reason = dry_run(base, source, planned)

    assert "would lose" in reason and "@jira/search" in reason
    # The base-owned approvals are not named among the losses.
    assert "rename_self" not in reason and "@kirocrew-cron" not in reason


def test_a_gain_still_keeps_the_crewmate_where_it_is(layout, platform_servers):
    from kiro_crew.mate_config_migration import dry_run, migration_file

    user, _mates = layout
    base = _installed_layout(user)
    source = _ray_kirocrew(base)
    planned = migration_file("uio", base, source)
    planned["mcpServers"] = {**planned["mcpServers"], "jira": JIRA}
    planned["tools"] = [*planned.get("tools", []), "@jira"]

    assert "would gain" in dry_run(base, source, planned)


def test_a_crewmate_bound_to_the_conductor_moves_without_a_notice_or_a_kit(layout, persisted):
    """The first crewmate a fresh install creates is bound to the conductor."""
    from kiro_crew.mate_upgrade_notice import NOTICE_OWED_FILENAME
    from kiro_crew.members import member_dir

    user, mates = layout
    _write(
        mates,
        "kirocrew-mate-conductor",
        _template("kirocrew-mate-conductor", excludedTools=["@kirocrew-core/spawn_run"]),
    )
    cfg = _cfg(mate=KiroCrewAgentConfig(member_id="mate", kiro_agent="kirocrew-mate-conductor"))
    saved = persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["mate"]
    assert saved["mate"]["changed_fields"] == {"kiro_agent"}
    assert _file(mates, "mate") == {
        "name": "mate",
        "mcpServers": {},
        "tools": [],
        "allowedTools": [],
    }
    assert read_record("mate") is None
    assert not (member_dir("mate") / NOTICE_OWED_FILENAME).exists()


def test_the_first_crewmate_gets_what_a_default_template_crewmate_gets(layout, persisted):
    """Mate, bound to the conductor, carries the default template's servers too."""
    from kiro_crew.mate_upgrade_notice import NOTICE_OWED_FILENAME
    from kiro_crew.members import member_dir

    user, mates = layout
    _write(mates, "kirocrew-mate-conductor", _template("kirocrew-mate-conductor"))
    _write(
        user,
        "kirocrew",
        _template(
            "kirocrew", tools=["@jira"], allowedTools=["@jira/search"], mcpServers={"jira": JIRA}
        ),
    )
    cfg = _cfg(
        mate=KiroCrewAgentConfig(member_id="mate", kiro_agent="kirocrew-mate-conductor"),
        rty=KiroCrewAgentConfig(member_id="rty", kiro_agent="kirocrew"),
    )
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert sorted(report.migrated) == ["mate", "rty"]
    mate, ordinary = _file(mates, "mate"), _file(mates, "rty")
    assert set(mate["mcpServers"]) == {"jira"}
    assert {k: v for k, v in mate.items() if k != "name"} == {
        k: v for k, v in ordinary.items() if k != "name"
    }
    # Still no notice and no kit for the crewmate whose chat did not move.
    assert read_record("mate") is None
    assert not (member_dir("mate") / NOTICE_OWED_FILENAME).exists()


def test_a_binding_without_a_member_id_is_left_alone_without_a_word(layout, persisted, caplog):
    """An older install's ``assistant`` binding is not a crewmate."""
    from kiro_crew.mate_upgrade_notice import NOTICE_OWED_FILENAME
    from kiro_crew.members import member_dir

    user, mates = layout
    _write(user, "kirocrew-assistant", _template("kirocrew-assistant", tools=["execute_bash"]))
    cfg = _cfg(assistant=KiroCrewAgentConfig(member_id="", kiro_agent="kirocrew-assistant"))
    saved = persisted(cfg)

    with caplog.at_level(logging.INFO, logger="kiro_crew.mate_config_migration"):
        report = migrate_existing_crewmates(cfg)

    assert report == type(report)()
    assert cfg.agents["assistant"].kiro_agent == "kirocrew-assistant"
    assert saved == {}
    assert not (mates / "assistant.json").exists()
    assert not (member_dir("assistant") / NOTICE_OWED_FILENAME).exists()
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


# ── pre-upgrade threads move onto the conductor with their crewmate ──


@pytest.fixture
def threads(monkeypatch):
    """Execution records under the isolated data home; nothing live or vouched."""
    from kiro_crew import execution_context

    execution_context._LIVE_EXECUTIONS.clear()
    execution_context._VOUCHED_EXECUTIONS.clear()
    yield execution_context
    execution_context._LIVE_EXECUTIONS.clear()
    execution_context._VOUCHED_EXECUTIONS.clear()


def _thread(ec, key: str, member: str, template: str, kind: str = "member"):
    """Write *key*'s record as an older build left it: *member*'s own store."""
    record = ec.ExecutionContext(
        member_id=member,
        store=ec.MemoryStoreRef(f"member-{member}-0123", member),
        selection_kind=kind,
        template_id=template,
        selection_name=member,
    )
    ec.bind_session_execution(key, record)
    return record


def _now(ec, key: str):
    ec._LIVE_EXECUTIONS.clear()  # read the durable record, as a restarted gateway does
    return ec.read_session_execution(key)


def _ledger(mates: Path) -> dict:
    return json.loads((mates.parent / "migrated.json").read_text(encoding="utf-8"))


def test_a_pre_upgrade_thread_runs_on_the_conductor_and_keeps_its_memory(
    layout, persisted, threads
):
    user, mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    key = "dashboard:member-zxc.memory-member-zxc-0123"
    before = _thread(threads, key, "zxc", "builder")

    report = migrate_existing_crewmates(cfg)

    after = _now(threads, key)
    assert after.template_id == mate_worker.MATE_CONDUCTOR_AGENT
    # Only the template moved: the memory this chat reads stays where it was.
    assert after == dataclasses.replace(before, template_id=mate_worker.MATE_CONDUCTOR_AGENT)
    assert report.threads_repointed == {"zxc": [key]}
    entry = _ledger(mates)["threads"]["zxc"]
    assert entry == {"from": "builder", "repointed": [key], "done": True, "member_id": "zxc"}


def test_the_old_thread_s_next_turn_binds_the_conductor_on_the_same_store(
    layout, persisted, threads
):
    """The turn reads the record back (``resolve_session_agent_bindings``), so the
    agent it runs is the conductor and the store it reads is the one it had."""
    from types import SimpleNamespace

    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    user, _mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    key = "dashboard:member-zxc.memory-member-zxc-0123"
    _thread(threads, key, "zxc", "builder")
    migrate_existing_crewmates(cfg)
    threads._LIVE_EXECUTIONS.clear()

    seen: dict = {}

    def resolver(config, selected, *project, **kwargs):
        seen.update(selected=selected, kind=kwargs.get("selection_kind"))
        return SimpleNamespace(kiro_agent="builder", memory_store_name="")

    bindings = resolve_session_agent_bindings(resolver, cfg, key, "zxc")

    assert seen == {"selected": "zxc", "kind": "member"}
    assert bindings.kiro_agent == mate_worker.MATE_CONDUCTOR_AGENT
    assert bindings.memory_store_name == "member-zxc-0123"


def test_other_records_and_a_skipped_crewmate_s_thread_are_left_alone(layout, persisted, threads):
    user, _mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    # A hook the worker base lacks keeps this one where it is (see the dry-run tests).
    _write(user, "hooked", _template("hooked", hooks={"stop": [{"command": "x"}]}))
    cfg = _cfg(
        zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"),
        qwe=KiroCrewAgentConfig(member_id="qwe", kiro_agent="builder"),
        sty=KiroCrewAgentConfig(member_id="sty", kiro_agent="hooked"),
    )
    persisted(cfg)
    worker = "dashboard:member-zxc.memory-worker"
    other_template = "dashboard:member-zxc.memory-other"
    peer = "dashboard:member-qwe.memory-member-qwe-0123"
    skipped = "dashboard:member-sty.memory-member-sty-0123"
    ordinary = "dashboard:chat-7"
    kept = {
        worker: _thread(threads, worker, "zxc", mate_worker.MATE_WORKER_AGENT, kind="template"),
        other_template: _thread(threads, other_template, "zxc", "something-else"),
        skipped: _thread(threads, skipped, "sty", "hooked"),
        ordinary: _thread(threads, ordinary, "zxc", "builder"),
    }
    # A template selection on zxc's store (a session it opened on a template).
    as_template = "dashboard:member-zxc.memory-as-template"
    kept[as_template] = _thread(threads, as_template, "zxc", "builder", kind="template")
    # Another member's thread whose record names zxc is not zxc's DM thread to move.
    kept[peer] = _thread(threads, peer, "zxc", "builder")

    report = migrate_existing_crewmates(cfg)

    assert "sty" not in report.migrated
    for key, record in kept.items():
        assert _now(threads, key) == record, key
    assert report.threads_repointed.get("zxc") == []


def test_a_second_boot_reads_no_thread_again(layout, persisted, threads, monkeypatch):
    user, _mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    key = "dashboard:member-zxc.memory-member-zxc-0123"
    _thread(threads, key, "zxc", "builder")
    assert migrate_existing_crewmates(cfg).threads_repointed == {"zxc": [key]}
    moved = _now(threads, key)

    from kiro_crew import mate_config_migration

    scanned: list[set[str]] = []
    real = mate_config_migration._dm_thread_keys
    monkeypatch.setattr(
        mate_config_migration, "_dm_thread_keys", lambda s: scanned.append(s) or real(s)
    )
    second = migrate_existing_crewmates(cfg)

    assert second.threads_repointed == {} and scanned == []
    assert _now(threads, key) == moved


def test_a_rolled_back_crewmate_s_threads_stay_on_its_old_agent(layout, persisted, threads):
    user, mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    from kiro_crew import mate_config_migration

    # The binding moved, and the owner pointed it back before the thread pass ran.
    real = mate_config_migration.repoint_moved_threads
    calls: list[int] = []

    def rolled_back_first():
        cfg.agents["zxc"].kiro_agent = "builder"
        calls.append(1)
        return real()

    mate_config_migration.repoint_moved_threads = rolled_back_first
    try:
        key = "dashboard:member-zxc.memory-member-zxc-0123"
        before = _thread(threads, key, "zxc", "builder")
        migrate_existing_crewmates(cfg)
    finally:
        mate_config_migration.repoint_moved_threads = real

    assert calls == [1]
    assert _now(threads, key) == before
    assert _ledger(mates)["threads"]["zxc"] == {
        "from": "builder",
        "repointed": [],
        "done": False,
        "member_id": "zxc",
    }


def test_a_crewmate_made_again_under_a_moved_name_is_moved_too(layout, persisted):
    """The ledger knows a moved crewmate by its member id, not only its name."""
    user, mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    assert migrate_existing_crewmates(cfg).migrated == ["zxc"]

    # Deleted, then made again under the same key on the shared template.
    cfg.agents["zxc"] = KiroCrewAgentConfig(member_id="zxc-2", kiro_agent="builder")
    again = migrate_existing_crewmates(cfg)

    assert again.migrated == ["zxc"]
    assert cfg.agents["zxc"].kiro_agent == "zxc"
    assert _ledger(mates)["migrated_ids"] == {"zxc": "zxc-2"}


def test_a_name_a_ledger_from_before_the_ids_lists_still_counts_as_moved(layout, persisted):
    user, mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    (mates.parent / "migrated.json").write_text(json.dumps({"migrated": ["zxc"]}))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    saved = persisted(cfg)

    assert migrate_existing_crewmates(cfg).migrated == []
    assert saved == {}


@pytest.mark.parametrize("damaged", ["{not json", "[]"])
def test_an_unreadable_ledger_moves_nobody_and_is_kept(layout, persisted, caplog, damaged):
    """Read as empty it would move a rolled-back crewmate again and drop owed passes."""
    user, mates = layout
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    ledger = mates.parent / "migrated.json"
    ledger.write_text(damaged, encoding="utf-8")
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    saved = persisted(cfg)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.mate_config_migration"):
        report = migrate_existing_crewmates(cfg)

    assert report.migrated == [] and saved == {}
    assert cfg.agents["zxc"].kiro_agent == "builder"
    assert not (mates / "zxc.json").exists()
    assert ledger.read_text(encoding="utf-8") == damaged
    assert "no crewmate moves until it is fixed or removed" in caplog.text


def test_a_ledger_entry_left_by_another_member_under_the_key_is_replaced(layout, persisted):
    from kiro_crew.mate_config_migration import _record_migrated

    _user, mates = layout
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc-2", kiro_agent="builder"))
    persisted(cfg)
    (mates.parent / "migrated.json").write_text(
        json.dumps(
            {
                "migrated": ["zxc"],
                "threads": {
                    "zxc": {"from": "old", "repointed": ["k"], "done": True, "member_id": "zxc"}
                },
            }
        ),
        encoding="utf-8",
    )

    _record_migrated("zxc", "builder")

    assert _ledger(mates)["threads"]["zxc"] == {
        "from": "builder",
        "repointed": [],
        "done": False,
        "member_id": "zxc-2",
    }


def test_a_conductor_bound_crewmate_owes_no_thread_pass(layout, persisted, threads):
    _user, mates = layout
    _write(_user, "kirocrew", _template("kirocrew"))
    cfg = _cfg(mate=KiroCrewAgentConfig(member_id="mate", kiro_agent="kirocrew-mate-conductor"))
    persisted(cfg)

    migrate_existing_crewmates(cfg)

    assert "threads" not in _ledger(mates)


def test_a_platform_server_the_base_leaves_out_is_the_conductors(layout, platform_servers):
    """A gateway server the worker base does not mount is no worker's loss."""
    from kiro_crew.mate_config_migration import dry_run, migration_file

    user, _mates = layout
    base = _installed_layout(user)
    source = _ray_kirocrew(base)  # declares kirocrew-cron / -guide; this base does not
    planned = migration_file("uio", base, source)
    for server in ("kirocrew-cron", "kirocrew-guide"):
        planned["mcpServers"].pop(server, None)
    planned["tools"] = [t for t in planned.get("tools", []) if "kirocrew-" not in t]
    planned["allowedTools"] = [t for t in planned.get("allowedTools", []) if "kirocrew-" not in t]

    assert dry_run(base, source, planned) == ""


def test_a_crewmate_whose_id_names_its_own_user_level_agent_stays(layout, persisted, threads):
    """Ray's ``rust-lead`` runs on a user-level agent named ``rust-lead``.

    A file under that id would never be read (the user level wins), so moving it
    would change only its threads: it stays, with a reason, and its threads too.
    """
    user, mates = layout
    _write(user, "rust-lead", _template("rust-lead", mcpServers={"jira": JIRA}))
    cfg = _cfg(**{"rust-lead": KiroCrewAgentConfig(member_id="rm", kiro_agent="rust-lead")})
    saved = persisted(cfg)
    key = "dashboard:member-rm.memory-member-rm-0123"
    before = _thread(threads, key, "rm", "rust-lead")

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    assert "user-level agent" in report.dry_run_failed["rust-lead"]
    assert not (mates / "rust-lead.json").exists()
    assert saved == {}
    assert _now(threads, key) == before


def test_a_crash_right_after_the_repoint_still_moves_the_old_threads(
    layout, persisted, threads, monkeypatch
):
    """The ledger intent is written before the repoint, so the next pass owes the threads."""
    from kiro_crew import mate_upgrade_notice

    ec = threads
    user, mates = layout
    _write(user, "builder", _template("builder", prompt="Follow the release SOP."))
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"))
    persisted(cfg)
    key = "dashboard:member-zxc"
    _thread(ec, key, "zxc", "builder")

    def _crash(_name):
        raise SystemExit("killed")

    monkeypatch.setattr(mate_upgrade_notice, "mark_notice_owed", _crash)
    with pytest.raises(SystemExit):
        migrate_existing_crewmates(cfg)
    assert cfg.agents["zxc"].kiro_agent == "zxc"
    assert _ledger(mates)["threads"]["zxc"]["done"] is False

    assert migrate_existing_crewmates(cfg).threads_repointed == {"zxc": [key]}
    assert _now(ec, key).template_id == mate_worker.MATE_CONDUCTOR_AGENT

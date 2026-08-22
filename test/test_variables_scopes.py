"""Tests for crew-variable config scopes and layered resolution.

Mostly in-memory: the cascade cases build config objects directly, so the layering
rules are pinned without a filesystem. The few cases that have to prove where the
data LIVES -- that config.json carries no variables and that the store is what
fills the fields -- redirect ``config_path()`` at a tmp dir and go through the real
store, since that is the property in question.
"""

from __future__ import annotations

import json
import logging

from kiro_crew.config import loader as loader_mod
from kiro_crew.config import variables_store
from kiro_crew.config.loader import (
    SCOPE_CREW,
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    SCOPE_WORKSPACE,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    _migrate_workspaces,
    coerce_variables,
    resolve_agent_bindings,
    resolve_variables,
)


def _config(
    *,
    global_vars: dict[str, str] | None = None,
    workspace_vars: dict[str, str] | None = None,
    crew_vars: dict[str, str] | None = None,
    workspace_name: str = "ops",
    crew_workspace: str | None = None,
) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.variables = dict(global_vars or {})
    cfg.workspaces = {
        "default": WorkspaceConfig(dir="workspace"),
        workspace_name: WorkspaceConfig(
            dir=f"w-{workspace_name}", variables=dict(workspace_vars or {})
        ),
    }
    cfg.default_workspace = "default"
    cfg.agents = {
        "crew1": KiroCrewAgentConfig(
            kiro_agent="kirocrew",
            workspace=workspace_name if crew_workspace is None else crew_workspace,
            variables=dict(crew_vars or {}),
        )
    }
    cfg.default_agent = "crew1"
    return cfg


class TestLayerPrecedence:
    def test_global_only(self):
        r = resolve_variables(_config(global_vars={"a": "g"}))
        assert r.values == {"a": "g"}
        assert r.winning_scope["a"] == SCOPE_GLOBAL

    def test_workspace_overrides_global(self):
        r = resolve_variables(_config(global_vars={"a": "g"}, workspace_vars={"a": "w"}))
        assert r.values["a"] == "w"
        assert r.winning_scope["a"] == SCOPE_WORKSPACE
        assert r.shadowed["a"] == [SCOPE_GLOBAL]

    def test_crew_overrides_workspace(self):
        r = resolve_variables(
            _config(global_vars={"a": "g"}, workspace_vars={"a": "w"}, crew_vars={"a": "c"})
        )
        assert r.values["a"] == "c"
        assert r.winning_scope["a"] == SCOPE_CREW
        assert r.shadowed["a"] == [SCOPE_GLOBAL, SCOPE_WORKSPACE]

    def test_session_overrides_crew(self):
        cfg = _config(global_vars={"a": "g"}, crew_vars={"a": "c"})
        r = resolve_variables(cfg, session_overrides={"a": "s"})
        assert r.values["a"] == "s"
        assert r.winning_scope["a"] == SCOPE_SESSION

    def test_disjoint_keys_all_survive(self):
        r = resolve_variables(
            _config(global_vars={"g": "1"}, workspace_vars={"w": "2"}, crew_vars={"c": "3"})
        )
        assert r.values == {"g": "1", "w": "2", "c": "3"}
        assert r.shadowed == {}

    def test_empty_string_at_narrow_scope_beats_non_empty_global(self):
        # Presence, not truthiness: a blank at a narrow scope is deliberate.
        r = resolve_variables(_config(global_vars={"a": "g"}, crew_vars={"a": ""}))
        assert r.values["a"] == ""
        assert r.winning_scope["a"] == SCOPE_CREW

    def test_no_variables_anywhere_resolves_empty(self):
        r = resolve_variables(_config())
        assert r.values == {}


class TestScopeSelection:
    def test_reports_resolved_crew_and_workspace(self):
        r = resolve_variables(_config(workspace_name="ops"))
        assert r.agent_name == "crew1"
        assert r.workspace_name == "ops"

    def test_unknown_agent_name_takes_the_default_crew(self):
        cfg = _config(crew_vars={"a": "c"})
        r = resolve_variables(cfg, agent_name="does-not-exist")
        assert r.agent_name == "crew1"
        assert r.values["a"] == "c"

    def test_explicit_agent_name_selects_that_crew(self):
        cfg = _config(crew_vars={"a": "one"})
        cfg.agents["crew2"] = KiroCrewAgentConfig(workspace="ops", variables={"a": "two"})
        r = resolve_variables(cfg, agent_name="crew2")
        assert r.agent_name == "crew2"
        assert r.values["a"] == "two"

    def test_crew_naming_a_missing_workspace_falls_back(self):
        cfg = _config(workspace_vars={"a": "w"}, crew_workspace="gone")
        cfg.workspaces["default"] = WorkspaceConfig(dir="workspace", variables={"a": "fallback"})
        r = resolve_variables(cfg)
        assert r.workspace_name == "default"
        assert r.values["a"] == "fallback"

    def test_no_agents_configured_yields_global_only(self):
        cfg = KiroCrewConfig()
        cfg.variables = {"a": "g"}
        cfg.agents = {}
        cfg.workspaces = {}
        r = resolve_variables(cfg)
        assert r.values == {"a": "g"}
        assert r.agent_name == ""

    def test_workspace_agrees_with_resolve_agent_bindings(self):
        """Guard against the two resolvers drifting apart on scope selection."""
        cases = [
            _config(workspace_name="ops"),
            _config(workspace_name="ops", crew_workspace="gone"),
        ]
        for cfg in cases:
            for name in (None, "crew1", "unknown-agent"):
                bindings = resolve_agent_bindings(cfg, agent_name=name)
                resolution = resolve_variables(cfg, agent_name=name)
                expected_dir = cfg.workspaces[resolution.workspace_name].dir
                assert str(bindings.workspace_dir) == expected_dir


class TestSessionLayerValidation:
    def test_session_override_is_validated_like_any_scope(self):
        cfg = _config(global_vars={"a": "g"})
        r = resolve_variables(cfg, session_overrides={"a": "ok", "bad-name": "x"})
        assert r.values["a"] == "ok"
        assert "bad-name" not in r.values

    def test_session_override_cannot_take_a_reserved_name(self):
        r = resolve_variables(_config(), session_overrides={"MAX_SUBAGENTS": "9"})
        assert "MAX_SUBAGENTS" not in r.values


class TestCoerceVariables:
    def test_drops_only_the_offending_pair(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = coerce_variables({"good": "v", "1bad": "x", "also_good": "w"}, "variables")
        assert out == {"good": "v", "also_good": "w"}
        assert "1bad" in caplog.text

    def test_warning_names_the_scope(self, caplog):
        with caplog.at_level(logging.WARNING):
            coerce_variables({"a-b": "x"}, "agents.oncall")
        assert "agents.oncall" in caplog.text

    def test_non_object_is_ignored(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert coerce_variables(["not", "an", "object"], "variables") == {}
        assert "expected an object" in caplog.text

    def test_missing_section_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert coerce_variables(None, "variables") == {}
        assert caplog.text == ""

    def test_coerces_scalars(self):
        assert coerce_variables({"a": 3, "b": True}, "variables") == {"a": "3", "b": "true"}


class TestWorkspaceMigrationIgnoresVariables:
    """``_migrate_workspaces`` no longer reads a ``variables`` key.

    Variables reach the cascade from the store, read at RESOLVE time rather than at
    load time (load() runs on the event loop for every session, including ones with
    no variables). So a ``variables`` key left in a hand-edited config.json is inert
    rather than authoritative, which is what stops a stale in-config copy from
    shadowing the store.
    """

    def test_a_stale_in_config_variables_key_is_not_read(self):
        out = _migrate_workspaces({"ops": {"dir": "w-ops", "variables": {"a": "1"}}})
        assert out["ops"].dir == "w-ops"
        assert out["ops"].variables == {}, "config.json is no longer a source of variables"

    def test_flat_string_entry_still_migrates(self):
        out = _migrate_workspaces({"legacy": "some-dir"})
        assert out["legacy"].dir == "some-dir"
        assert out["legacy"].variables == {}

    def test_the_store_is_what_fills_the_cascade(self, tmp_path, monkeypatch):
        """End to end through the real store: a value written by the only writer
        reaches the resolved cascade for the workspace the config defines."""
        monkeypatch.setattr(loader_mod, "config_path", lambda: tmp_path / "config.json")
        variables_store.invalidate_cache()
        variables_store.patch_store(
            scope=variables_store.SCOPE_WORKSPACE, name="ops", values={"a": "1"}
        )

        cfg = KiroCrewConfig()
        cfg.workspaces = {"ops": WorkspaceConfig(dir="w-ops")}
        cfg.default_workspace = "ops"
        cfg.agents = {"c1": KiroCrewAgentConfig(workspace="ops")}
        cfg.default_agent = "c1"

        resolved = loader_mod.resolve_variables(cfg, agent_name="c1")
        assert resolved.values.get("a") == "1"
        assert resolved.winning_scope.get("a") == "workspace"

    def test_a_store_entry_for_an_unknown_workspace_is_inert(self, tmp_path, monkeypatch):
        """Stale data -- the name was deleted after the variable was set.

        This is the property that REPLACED the deleted resurrection guard: a write
        for an unknown name can no longer create anything, so the only thing left to
        prove is that reading it neither resolves nor materializes the workspace.
        """
        monkeypatch.setattr(loader_mod, "config_path", lambda: tmp_path / "config.json")
        variables_store.invalidate_cache()
        variables_store.patch_store(
            scope=variables_store.SCOPE_WORKSPACE, name="gone", values={"a": "1"}
        )

        cfg = KiroCrewConfig()
        cfg.workspaces = {"ops": WorkspaceConfig(dir="w-ops")}
        cfg.default_workspace = "ops"
        cfg.agents = {"c1": KiroCrewAgentConfig(workspace="ops")}
        cfg.default_agent = "c1"

        resolved = loader_mod.resolve_variables(cfg, agent_name="c1")
        assert "a" not in resolved.values, "a deleted workspace's variable resolved"
        assert "gone" not in cfg.workspaces, "reading the store materialized a workspace"


class TestConfigJsonCarriesNoVariables:
    """The property that makes the whole storage move safe.

    ``save()`` is a lossy whole-document replace of exactly the keys ``to_dict()``
    emits, called from 13 async sites. While ``variables`` was one of those keys,
    every possible behaviour during an unrelated save was wrong in a different way:
    serialize the merged value (overwrites a base value the overlay shadowed, which
    is not in the merged view and so is unrecoverable), preserve it under the config
    lock (a contended flock on a sync method stalls the event loop), or preserve it
    with an unlocked read (drops a variables write that already returned 200).

    Not emitting the key at all deletes the trilemma instead of choosing a position,
    so this test is the one that has to hold. The three tests that pinned the old
    machinery -- save()-neutrality, the base-document restore, and the
    overlay-subtraction exemption -- are gone with it.
    """

    def test_to_dict_emits_no_variables_at_any_scope(self):
        cfg = _config(global_vars={"g": "1"}, workspace_vars={"w": "2"}, crew_vars={"c": "3"})

        d = cfg.to_dict()

        assert "variables" not in d, "global scope"
        assert "variables" not in d["workspaces"]["ops"], "workspace scope"
        assert "variables" not in d["agents"]["crew1"], "crew scope"

    def test_the_in_memory_fields_still_carry_the_values(self):
        """The field is dropped from the SERIALIZED shape only -- the cascade reads
        it at runtime, so a to_dict() call must not empty the live config."""
        cfg = _config(global_vars={"g": "1"}, workspace_vars={"w": "2"}, crew_vars={"c": "3"})
        cfg.to_dict()
        assert cfg.variables == {"g": "1"}
        assert cfg.workspaces["ops"].variables == {"w": "2"}
        assert cfg.agents["crew1"].variables == {"c": "3"}

    def test_non_variables_fields_at_those_scopes_still_serialize(self):
        """The drop is surgical: a sibling key at the same nesting level survives,
        so this is not just an empty-dict assertion passing for the wrong reason."""
        cfg = _config(workspace_vars={"w": "2"})
        d = cfg.to_dict()
        assert d["workspaces"]["ops"]["dir"] == "w-ops"
        assert d["agents"]["crew1"]["kiro_agent"] == "kirocrew"

    def test_save_does_not_write_the_key_either(self, tmp_path, monkeypatch):
        """to_dict() is the gate, but save() is what touches the file, and it has
        its own overlay-subtraction step. Assert on the BYTES it produced."""
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_file)
        monkeypatch.setattr(loader_mod, "config_local_path", lambda: tmp_path / "local.json")

        cfg = _config(global_vars={"g": "1"}, workspace_vars={"w": "2"}, crew_vars={"c": "3"})
        cfg.save()

        written = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "variables" not in written
        assert "variables" not in written["workspaces"]["ops"]
        assert "variables" not in written["agents"]["crew1"]

    def test_a_config_save_leaves_the_store_untouched(self, tmp_path, monkeypatch):
        """The defect this move fixed, asserted end to end: an unrelated whole-config
        save must neither delete, overwrite nor create a stored variable."""
        cfg_file = tmp_path / "config.json"
        monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_file)
        monkeypatch.setattr(loader_mod, "config_local_path", lambda: tmp_path / "local.json")
        variables_store.patch_store(scope=variables_store.SCOPE_GLOBAL, values={"KEEP": "mine"})
        before = variables_store.store_path().read_bytes()

        cfg = _config(global_vars={"KEEP": "something-else"})
        cfg.save()

        assert variables_store.store_path().read_bytes() == before


class TestUnknownCrewIsVisible:
    """A caller naming a crew that does not exist gets the DEFAULT crew's variables.

    That fallback is the right default but the wrong answer, and it used to be
    silent. It already shipped once on this feature -- the dashboard passes the
    resolved kiro-agent runtime name, which is never a key in ``config.agents`` --
    so the miss is logged at WARNING. These tests pin both directions, because a
    warning that also fires for the normal no-crew-bound case would be noise and
    would be turned off.
    """

    def _config(self):
        cfg = KiroCrewConfig()
        cfg.agents = {"real": KiroCrewAgentConfig(variables={"WHO": "real"})}
        cfg.default_agent = "real"
        return cfg

    def test_a_missing_crew_name_warns_and_names_the_crew(self, caplog):
        cfg = self._config()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            resolved = loader_mod.resolve_variables(cfg, agent_name="kirocrew")
        assert resolved.values.get("WHO") == "real", "the fallback must still resolve"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
        assert (
            "kirocrew" in warnings[0].getMessage()
        ), "the warning must name the crew that missed, or it cannot be acted on"

    def test_a_known_crew_is_silent(self, caplog):
        cfg = self._config()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            loader_mod.resolve_variables(cfg, agent_name="real")
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_no_crew_bound_is_silent(self, caplog):
        """The normal case. Warning here would fire on every unbound session."""
        cfg = self._config()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            for empty in (None, ""):
                loader_mod.resolve_variables(cfg, agent_name=empty)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

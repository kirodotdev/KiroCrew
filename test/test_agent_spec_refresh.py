"""Tests for the dynamic fields a spec refresh rewrites, and the spec migrations.

Legacy MCP-json aliases, stale managed URLs, relocated skill URIs, the
bookkeeping keys ``migrate_agent_specs`` lifts into the sidecar, the config
model sync, ``reset_agent_model``, and a wrong-typed ``agent`` section.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from agent_helpers import _run_install

# Shared with ``test_mcp_rebuild_reconsumption`` via a dedicated helpers module --
# the repo's convention, since a test module is not importable from another one.
# Re-exported under the original private names so the call sites below are unchanged.
from mcp_merge_helpers import bundled_defaults as _bundled_defaults

from kiro_crew import agent_state
from kiro_crew.agent import migrate_agent_specs


class TestRefreshDynamicFieldsLegacyMcpJsonAlias:
    """kiro-cli reads ``useLegacyMcpJson`` as an alias of ``includeMcpJson``,
    so a refreshed config that keeps both is refused as a duplicate field."""

    def test_alias_dropped_when_include_mcp_json_written(self):
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {"useLegacyMcpJson": True}
        _refresh_dynamic_fields(config)
        assert config["includeMcpJson"] is False
        assert "useLegacyMcpJson" not in config


class TestRefreshDynamicFieldsStripsStaleUrl:
    """Managed servers are stdio-only; a stale url from an old build must be
    removed on refresh so it can't propagate into the CC config."""

    def test_stale_url_and_headers_removed_from_managed_entry(self):
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "mcpServers": {
                "kirocrew-core": {
                    "url": "http://localhost:5476/api/mcp/core",
                    "headers": {"X-Stale": "1"},
                },
                "kirocrew-cron": {"url": "http://localhost:5476/api/mcp/cron"},
            }
        }
        _refresh_dynamic_fields(config)
        for name, args in (("kirocrew-core", ["mcp-core"]), ("kirocrew-cron", ["mcp-cron"])):
            entry = config["mcpServers"][name]
            assert "url" not in entry, f"{name} still has stale url"
            assert "headers" not in entry, f"{name} still has stale headers"
            assert entry["command"]
            # Windows uses the interpreter-module fallback, which prepends
            # ``-m kiro_crew.__main__`` before the same managed subcommand.
            assert entry["args"][-len(args) :] == args

    def test_non_managed_server_url_preserved(self):
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "mcpServers": {
                "deepwiki": {"url": "https://mcp.deepwiki.com/mcp"},
            }
        }
        _refresh_dynamic_fields(config)
        # A genuine remote server is not a managed one — its url must survive.
        assert config["mcpServers"]["deepwiki"]["url"] == "https://mcp.deepwiki.com/mcp"

    def test_non_managed_server_oauth_hints_preserved(self):
        """scopes/clientId are passthrough — the runtime, not Kiro Crew, uses them."""
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "mcpServers": {
                "github": {
                    "url": "https://api.githubcopilot.com/mcp/",
                    "scopes": ["read:user", "read:org"],
                    "clientId": "public-client-id",
                },
            }
        }
        _refresh_dynamic_fields(config)
        entry = config["mcpServers"]["github"]
        assert entry["scopes"] == ["read:user", "read:org"]
        assert entry["clientId"] == "public-client-id"

    def test_edition_extra_invocation_refreshed_user_keys_kept(self):
        """An edition extra's command/args are the edition's; everything else is
        the user's. A stale versioned interpreter must be replaced on refresh."""
        from kiro_crew.agent import _refresh_dynamic_fields

        extra = {
            "edition-extra": {"command": "/v2/python3", "args": ["-m", "extra"]},
            "cmd-only": {"command": "/v2/tool"},
            "user-nulled": {"command": "/v2/python3"},
        }
        mine = {"command": "/opt/mine", "args": ["serve"], "env": {"K": "v"}}
        config = {
            "mcpServers": {
                "edition-extra": {
                    "command": "/v1/python3",
                    "args": ["-m", "old"],
                    "env": {"TOKEN_PATH": "/home/u/t"},
                    "disabled": True,
                },
                "mine": dict(mine),
                "cmd-only": {"command": "/v1/tool", "args": ["--user-flag"]},
                "user-nulled": None,
            }
        }
        with patch("kiro_crew.agent._extra_mcp_servers", return_value=extra):
            _refresh_dynamic_fields(config)
        entry = config["mcpServers"]["edition-extra"]
        assert entry["command"] == "/v2/python3"
        assert entry["args"] == ["-m", "extra"]
        assert entry["env"] == {"TOKEN_PATH": "/home/u/t"}
        assert entry["disabled"] is True
        assert config["mcpServers"]["mine"] == mine
        # Only invocation keys the edition supplies are re-pinned.
        assert config["mcpServers"]["cmd-only"] == {"command": "/v2/tool", "args": ["--user-flag"]}
        # A non-object entry occupies the name as the user's.
        assert config["mcpServers"]["user-nulled"] is None

    def test_refresh_strips_legacy_denied_commands(self):
        # Upgrade path: an existing config injected by an older build carries a
        # stale toolsSettings.deniedCommands + autoAllowReadonly that kiro-cli
        # would keep enforcing ahead of the hooks gate. The refresh must remove
        # them so upgraded installs behave like a fresh (hooks-gate-only) one.
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "toolsSettings": {
                "execute_bash": {
                    "autoAllowReadonly": True,
                    "deniedCommands": ["aws .* delete-.*", "rm -rf /.*"],
                },
                "shell": {"deniedCommands": ["rm -rf /.*"]},
            }
        }
        _refresh_dynamic_fields(config)
        # The empty scaffolding is removed entirely.
        assert "toolsSettings" not in config

    def test_refresh_preserves_other_tools_settings(self):
        # Only the retired keys are stripped — a user-authored sibling stays.
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "toolsSettings": {
                "execute_bash": {
                    "deniedCommands": ["rm -rf /.*"],
                    "allowedCommands": ["ls", "cat"],
                }
            }
        }
        _refresh_dynamic_fields(config)
        assert "deniedCommands" not in config["toolsSettings"]["execute_bash"]
        assert config["toolsSettings"]["execute_bash"]["allowedCommands"] == ["ls", "cat"]


class TestMigrateRelocatedSkillUris:
    """An agent spec mapping a relocated builtin skill's old path follows the move."""

    OLD = "kirocrew-dev/prepare-pr"
    NEW = "kirocrew-dev/kirocrew-prepare-pr"

    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home = tmp_path / "home"
        skills = home / ".kiro" / "crew" / "skills"
        (skills / self.NEW).mkdir(parents=True)
        (skills / self.NEW / "SKILL.md").write_text("---\nname: kirocrew-prepare-pr\n---\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr("kiro_crew.skills.skills_dir", lambda: skills)
        agents = tmp_path / "kiro_agents"
        agents.mkdir()
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)
        return skills, agents

    def _write(self, agents: Path, resources: list) -> Path:
        spec = agents / "a.json"
        spec.write_text(json.dumps({"name": "alpha", "resources": resources}))
        return spec

    def test_rewrites_home_and_absolute_forms_in_place(self, env) -> None:
        from kiro_crew.agent import migrate_relocated_skill_uris

        skills, agents = env
        spec = self._write(
            agents,
            [
                "file://AGENTS.md",
                f"skill://~/.kiro/crew/skills/{self.OLD}/SKILL.md",
                "skill://~/.kiro/crew/skills/babysit-other/SKILL.md",
            ],
        )
        assert migrate_relocated_skill_uris() == 1
        assert json.loads(spec.read_text(encoding="utf-8"))["resources"] == [
            "file://AGENTS.md",
            f"skill://~/.kiro/crew/skills/{self.NEW}/SKILL.md",
            "skill://~/.kiro/crew/skills/babysit-other/SKILL.md",
        ]

        spec = self._write(agents, [f"skill://{(skills / self.OLD / 'SKILL.md').as_posix()}"])
        assert migrate_relocated_skill_uris() == 1
        assert json.loads(spec.read_text(encoding="utf-8"))["resources"] == [
            f"skill://{(skills / self.NEW / 'SKILL.md').as_posix()}"
        ]

    def test_drops_the_old_entry_when_the_new_path_is_already_mapped(self, env) -> None:
        from kiro_crew.agent import migrate_relocated_skill_uris

        _skills, agents = env
        new = f"skill://~/.kiro/crew/skills/{self.NEW}/SKILL.md"
        spec = self._write(agents, [new, f"skill://~/.kiro/crew/skills/{self.OLD}/SKILL.md"])
        assert migrate_relocated_skill_uris() == 1
        assert json.loads(spec.read_text(encoding="utf-8"))["resources"] == [new]

    def test_is_idempotent(self, env) -> None:
        from kiro_crew.agent import migrate_relocated_skill_uris

        _skills, agents = env
        self._write(agents, [f"skill://~/.kiro/crew/skills/{self.OLD}/SKILL.md"])
        assert migrate_relocated_skill_uris() == 1
        assert migrate_relocated_skill_uris() == 0

    def test_leaves_a_mapping_whose_old_skill_still_loads(self, env) -> None:
        from kiro_crew.agent import migrate_relocated_skill_uris

        skills, agents = env
        (skills / self.OLD).mkdir(parents=True)
        (skills / self.OLD / "SKILL.md").write_text("---\nname: prepare-pr\n---\n")
        old = f"skill://~/.kiro/crew/skills/{self.OLD}/SKILL.md"
        spec = self._write(agents, [old])
        assert migrate_relocated_skill_uris() == 0
        assert json.loads(spec.read_text(encoding="utf-8"))["resources"] == [old]

    def test_reads_and_writes_inside_the_spec_lock(self, env, monkeypatch) -> None:
        # A concurrent template PATCH holds the same lock; reading before it
        # would write a stale snapshot back over the user's saved edit.
        import kiro_crew.agent as agent_mod

        _skills, agents = env
        self._write(agents, [f"skill://~/.kiro/crew/skills/{self.OLD}/SKILL.md"])
        held: list[bool] = [False]
        seen: list[tuple[str, bool]] = []

        @contextlib.contextmanager
        def recording_lock(agents_dir):
            held[0] = True
            try:
                yield
            finally:
                held[0] = False

        real_read = agent_mod._read_agent_spec
        real_write = agent_mod._atomic_json_write

        def read(*args, **kwargs):
            seen.append(("read", held[0]))
            return real_read(*args, **kwargs)

        def write(*args, **kwargs):
            seen.append(("write", held[0]))
            return real_write(*args, **kwargs)

        monkeypatch.setattr(agent_mod, "agents_spec_lock", recording_lock)
        monkeypatch.setattr(agent_mod, "_read_agent_spec", read)
        monkeypatch.setattr(agent_mod, "_atomic_json_write", write)

        assert agent_mod.migrate_relocated_skill_uris() == 1
        assert seen == [("read", True), ("write", True)]

    def test_leaves_an_enrolled_members_saved_generation_alone(self, env, monkeypatch) -> None:
        # An enrolled member's spec carries a digest its capability intent
        # records; an in-place rewrite would make reconcile refuse sessions.
        from kiro_crew.agent import migrate_relocated_skill_uris

        _skills, agents = env
        old = f"skill://~/.kiro/crew/skills/{self.OLD}/SKILL.md"
        spec = self._write(agents, [old])

        monkeypatch.setattr(
            "kiro_crew.agent_state.get_capabilities",
            lambda name: {"status": "saved"} if name == "alpha" else None,
        )
        assert migrate_relocated_skill_uris() == 0
        assert json.loads(spec.read_text(encoding="utf-8"))["resources"] == [old]

        def unreadable(name):
            raise ValueError("capability_state_invalid")

        monkeypatch.setattr("kiro_crew.agent_state.get_capabilities", unreadable)
        assert migrate_relocated_skill_uris() == 0
        assert json.loads(spec.read_text(encoding="utf-8"))["resources"] == [old]

    def test_leaves_workspace_relative_and_wildcard_uris_alone(self, env) -> None:
        from kiro_crew.agent import migrate_relocated_skill_uris

        _skills, agents = env
        kept = [f".kiro/skills/{self.OLD}/SKILL.md", "skill://~/.kiro/crew/skills/*/SKILL.md"]
        kept = [f"skill://{kept[0]}", kept[1]]
        spec = self._write(agents, kept)
        assert migrate_relocated_skill_uris() == 0
        assert json.loads(spec.read_text(encoding="utf-8"))["resources"] == kept


class TestMigrateAgentSpecs:
    """migrate_agent_specs lifts Kiro Crew bookkeeping keys into the sidecar."""

    def test_strips_and_lifts_keys(self, tmp_path: Path):
        kiro = tmp_path / "kiro_agents"
        kiro.mkdir()
        (kiro / "a.json").write_text(
            json.dumps({"name": "alpha", "model": "m", "model_managed": False})
        )
        (kiro / "b.json").write_text(json.dumps({"name": "beta", "cc_model": "claude-sonnet-4.6"}))
        (kiro / "c.json").write_text(json.dumps({"name": "gamma", "model": "m"}))
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro):
            cleaned = migrate_agent_specs()
        assert cleaned == 2
        assert "model_managed" not in json.loads((kiro / "a.json").read_text(encoding="utf-8"))
        assert "cc_model" not in json.loads((kiro / "b.json").read_text(encoding="utf-8"))
        assert agent_state.get_model_managed("alpha") is False
        assert agent_state.get_cc_model("beta") == "claude-sonnet-4.6"

    def test_idempotent(self, tmp_path: Path):
        kiro = tmp_path / "kiro_agents"
        kiro.mkdir()
        (kiro / "a.json").write_text(json.dumps({"name": "alpha", "model_managed": True}))
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro):
            assert migrate_agent_specs() == 1
            assert migrate_agent_specs() == 0

    def test_does_not_clobber_authoritative_sidecar(self, tmp_path: Path):
        agent_state.set_model_managed("alpha", False)
        kiro = tmp_path / "kiro_agents"
        kiro.mkdir()
        (kiro / "a.json").write_text(json.dumps({"name": "alpha", "model_managed": True}))
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro):
            migrate_agent_specs()
        assert agent_state.get_model_managed("alpha") is False

    def test_installed_spec_is_schema_clean(self, tmp_path: Path):
        """After install the written spec carries no Kiro Crew bookkeeping keys."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert "model_managed" not in config
        assert "cc_model" not in config


class TestRefreshDynamicFieldsSyncsConfigModel:
    """config.json agent.model must propagate into the kiro agent file so
    kiro-cli's --agent startup load matches it."""

    def _write_mc_config(self, tmp_path: Path, model) -> Path:
        mc = tmp_path / "config.json"
        body = {} if model is None else {"agent": {"model": model}}
        mc.write_text(json.dumps(body), encoding="utf-8")
        return mc

    def test_explicit_config_model_overrides_managed_default(self, tmp_path: Path):
        from kiro_crew.agent import _refresh_dynamic_fields

        # model_managed=True would re-sync the agent file from the shipped
        # default; an explicit config.json pick must still win.
        agent_state.set_model_managed("kirocrew", True)
        mc = self._write_mc_config(tmp_path, "claude-opus-4.8")
        config = {"name": "kirocrew", "model": "stale-from-install"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-opus-4.8"

    def test_auto_sentinel_does_not_clobber_agent_model(self, tmp_path: Path):
        from kiro_crew.agent import _refresh_dynamic_fields

        # "auto" defers to managed/shipped resolution; it must not overwrite
        # the existing agent-file model with the literal "auto".
        mc = self._write_mc_config(tmp_path, "auto")
        config = {"name": "kirocrew", "model": "claude-haiku-4.5"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-haiku-4.5"

    def test_no_config_model_leaves_agent_model_untouched(self, tmp_path: Path):
        from kiro_crew.agent import _refresh_dynamic_fields

        mc = self._write_mc_config(tmp_path, None)
        config = {"name": "kirocrew", "model": "claude-sonnet-4.6"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-sonnet-4.6"


class TestResetAgentModel:
    """The explicit way back to the shipped default.

    Ownership of a spec's ``model`` cannot be inferred -- a value an older
    build's propagation wrote and one the user typed in are identical on disk --
    so the reset is a user action, and these tests pin that it clears BOTH halves
    of the state (the spec pin and the sidecar flag) and never guesses.
    """

    def _spec(self, tmp_path: Path, stem: str, body: dict) -> Path:
        spec = tmp_path / f"{stem}.json"
        spec.write_text(json.dumps(body), encoding="utf-8")
        return spec

    def test_clear_model_pin_drops_the_pin_and_resumes_tracking(self):
        from kiro_crew.agent import clear_model_pin

        config = {"name": "kirocrew", "model": "claude-opus-4.8"}
        clear_model_pin(config, "kirocrew")
        assert "model" not in config
        assert agent_state.get_model_managed("kirocrew") is True

    def test_clear_model_pin_is_idempotent_with_no_pin(self):
        from kiro_crew.agent import clear_model_pin

        config: dict = {"name": "kirocrew"}
        clear_model_pin(config, "kirocrew")
        assert "model" not in config
        assert agent_state.get_model_managed("kirocrew") is True

    def test_reset_writes_the_spec_and_reports_the_previous_model(
        self, tmp_path: Path, monkeypatch
    ):
        import kiro_crew.agent as agent_mod

        self._spec(tmp_path, "kirocrew", {"name": "kirocrew", "model": "claude-opus-4.8"})
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)

        spec_path, previous = agent_mod.reset_agent_model("kirocrew")

        assert previous == "claude-opus-4.8"
        assert json.loads(spec_path.read_text(encoding="utf-8")) == {"name": "kirocrew"}
        assert agent_state.get_model_managed("kirocrew") is True

    def test_reset_overrides_an_explicit_freeze(self, tmp_path: Path, monkeypatch):
        """A frozen editor pick is the user's own answer, and asking for a reset
        is a NEWER answer from the same user -- so it wins."""
        import kiro_crew.agent as agent_mod

        agent_state.set_model_managed("kirocrew", False)
        self._spec(tmp_path, "kirocrew", {"name": "kirocrew", "model": "claude-haiku-4.5"})
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)

        agent_mod.reset_agent_model("kirocrew")
        assert agent_state.get_model_managed("kirocrew") is True

    def test_reset_never_writes_bookkeeping_into_the_spec(self, tmp_path: Path, monkeypatch):
        """kiro-cli validates specs with deny_unknown_fields and drops the whole
        agent on an unknown key, so a stray sidecar key must be lifted out."""
        import kiro_crew.agent as agent_mod

        self._spec(
            tmp_path,
            "kirocrew",
            {"name": "kirocrew", "model": "claude-opus-4.8", "cc_model": "claude-sonnet-4.6"},
        )
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)

        spec_path, _ = agent_mod.reset_agent_model("kirocrew")
        written = json.loads(spec_path.read_text(encoding="utf-8"))
        assert "cc_model" not in written and "model_managed" not in written
        assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"

    def test_reset_resolves_a_spec_whose_filename_differs_from_its_name(
        self, tmp_path: Path, monkeypatch
    ):
        import kiro_crew.agent as agent_mod

        self._spec(tmp_path, "some-file", {"name": "custom-agent", "model": "claude-haiku-4.5"})
        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)

        spec_path, previous = agent_mod.reset_agent_model("custom-agent")
        assert spec_path.name == "some-file.json"
        assert previous == "claude-haiku-4.5"

    def test_reset_refuses_an_agent_with_no_spec(self, tmp_path: Path, monkeypatch):
        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
        with pytest.raises(FileNotFoundError):
            agent_mod.reset_agent_model("kirocrew")
        # Nothing was claimed on a failed reset.
        assert agent_state.get_model_managed("kirocrew") is None

    def test_refresh_still_leaves_an_unrecorded_spec_alone(self, tmp_path: Path):
        """The counterpart contract: with no sidecar entry the refresh must not
        reclassify a pin on its own. Inferring ownership from the value is what
        makes the explicit reset necessary rather than optional."""
        from kiro_crew.agent import _refresh_dynamic_fields

        mc = tmp_path / "config.json"
        mc.write_text(json.dumps({"agent": {"model": "auto"}}), encoding="utf-8")
        config = {"name": "kirocrew", "model": "claude-opus-4.8"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-opus-4.8"
        assert agent_state.get_model_managed("kirocrew") is None


class TestRefreshDynamicFieldsWrongTypedAgentSection:
    """The spec builder reads the config ``agent`` section as a dict. A ``None``
    or missing section means inherit; a truthy non-dict value (a list or a
    string from a hand-edited config.json) also degrades to inherit rather than
    reaching ``.get`` on a non-dict, matching the validated loader."""

    @pytest.mark.parametrize("bad_agent", [["x"], "oops", 123, 1.5])
    def test_a_truthy_non_dict_agent_section_does_not_raise(self, tmp_path, bad_agent):
        from kiro_crew.agent import _refresh_dynamic_fields

        mc = tmp_path / "config.json"
        mc.write_text(json.dumps({"agent": bad_agent}), encoding="utf-8")
        config = {"name": "kirocrew", "model": "sentinel"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        # No model was pulled from the malformed section, so the sentinel the
        # builder started with is not overwritten by a config-derived pick.
        assert config["model"] == "sentinel"

    def test_a_valid_agent_model_is_still_applied(self, tmp_path):
        from kiro_crew.agent import _refresh_dynamic_fields

        mc = tmp_path / "config.json"
        mc.write_text(json.dumps({"agent": {"model": "claude-chosen"}}), encoding="utf-8")
        config = {"name": "kirocrew", "model": "sentinel"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-chosen"

"""Tests for the kiro-cli hooks an installed spec carries.

``agent.kiro_hooks`` merged into the spec (validation, dedup, caps), and the
hook keys internal to Kiro Crew that are stripped from it.
"""

from __future__ import annotations

import codecs
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401

# Shared with ``test_mcp_rebuild_reconsumption`` via a dedicated helpers module --
# the repo's convention, since a test module is not importable from another one.
# Re-exported under the original private names so the call sites below are unchanged.
from mcp_merge_helpers import DEFAULT_MANAGED_MCPS as _DEFAULT_MANAGED_MCPS

from conftest import requires_symlinks
from kiro_crew.agent import install_agent


class TestKiroHooksMerge:
    """Tests for agent.kiro_hooks merge into kiro-cli agent config."""

    def _bundled_with_hooks(self, tmp_path: Path) -> Path:
        """Write bundled defaults with realistic list-based hooks."""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(exist_ok=True)
        defaults = {
            "model": "claude-default",
            "tools": ["ReadFile"],
            "allowedTools": ["ReadFile"],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
            "hooks": {
                "postToolUse": [
                    {"matcher": "execute_bash", "command": "audit.sh"},
                ],
            },
        }
        (cfg_dir / "defaults.json").write_text(json.dumps(defaults))
        (cfg_dir / "prompt.md").write_text("system prompt")
        return cfg_dir

    def _make_hook(self, tmp_path: Path, name: str = "hook.sh") -> str:
        """Create a real executable hook script and return its absolute path."""
        hook = tmp_path / "hooks" / name
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        return str(hook)

    def _run_with_kiro_hooks(
        self,
        tmp_path: Path,
        kiro_hooks: dict,
        existing: dict | None = None,
    ) -> dict:
        """Install agent with kiro_hooks in config.json and return the result."""
        cfg_dir = self._bundled_with_hooks(tmp_path)
        mc_config = tmp_path / "mc_config.json"
        # Disable autoimport in this helper: these tests target the explicit
        # kiro_hooks merge path only. The autoimport path is covered by
        # TestKiroHooksAutoimport in test_agent_hooks_autoimport.py.
        mc_config.write_text(
            json.dumps(
                {
                    "agent": {
                        "kiro_hooks": kiro_hooks,
                        "kiro_hooks_autoimport": False,
                    }
                }
            )
        )

        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        if existing:
            (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        prompt = cfg_dir / "prompt.md"
        patches = [
            patch.multiple(
                "kiro_crew.agent",
                KIRO_AGENTS_DIR=kiro_dir,
                _BUNDLED_CFG_DIR=cfg_dir,
                _KIROCREW_BIN="/usr/bin/kirocrew",
                _MANAGED_MCP_SERVERS=_DEFAULT_MANAGED_MCPS,
                _KIRO_MCP_JSON=tmp_path / "nonexistent_kiro_mcp.json",
                _CC_MCP_JSON=tmp_path / "nonexistent_cc_mcp.json",
            ),
            patch("kiro_crew.agent._prompt_path", return_value=prompt),
            patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json"),
            patch("kiro_crew.agent._project_dir", return_value=None),
            patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
            patch("kiro_crew.agent.shutil.which", side_effect=lambda c, **kw: c),
            patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            path = install_agent(clean=existing is None)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_user_hooks_appended_to_bundled(self, tmp_path: Path):
        """User kiro_hooks are appended after bundled hooks per event."""
        hook = self._make_hook(tmp_path, "guardian.sh")
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "postToolUse": [{"matcher": "*", "command": hook}],
            },
        )
        post = config["hooks"]["postToolUse"]
        assert post[0] == {"matcher": "execute_bash", "command": "audit.sh"}  # bundled first
        assert post[1] == {"matcher": "*", "command": hook}  # user appended

    def test_user_hooks_new_event_type(self, tmp_path: Path):
        """User kiro_hooks can add hooks for event types not in bundled."""
        hook = self._make_hook(tmp_path, "guardian.sh")
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [{"matcher": "*", "command": hook}],
            },
        )
        assert config["hooks"]["postToolUse"] == [
            {"matcher": "execute_bash", "command": "audit.sh"},
        ]
        assert config["hooks"]["preToolUse"] == [
            {"matcher": "*", "command": hook},
        ]

    def test_user_hooks_dedup_by_command(self, tmp_path: Path):
        """Duplicate commands are not added twice."""
        hook = self._make_hook(tmp_path)
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [
                    {"command": hook},
                    {"command": hook},
                ],
            },
        )
        assert len(config["hooks"]["preToolUse"]) == 1

    def test_user_hooks_dedup_against_bundled(self, tmp_path: Path):
        """User hook whose command+matcher matches a bundled hook is not added twice."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path, "audit.sh")
        bundled = {"postToolUse": [{"matcher": "execute_bash", "command": hook}]}
        user = {"postToolUse": [{"matcher": "execute_bash", "command": hook}]}
        result = _merge_kiro_hooks(bundled, user)
        assert len(result["postToolUse"]) == 1
        assert result["postToolUse"][0] == {"matcher": "execute_bash", "command": hook}

    def test_user_hooks_same_command_different_matcher(self, tmp_path: Path):
        """Same command with different matchers are kept as separate entries."""
        hook = self._make_hook(tmp_path)
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [
                    {"matcher": "execute_bash", "command": hook},
                    {"matcher": "ReadFile", "command": hook},
                ],
            },
        )
        assert len(config["hooks"]["preToolUse"]) == 2

    def test_user_hooks_malformed_skipped(self, tmp_path: Path):
        """Entries without command field are skipped."""
        hook = self._make_hook(tmp_path, "valid.sh")
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [
                    {"matcher": "*"},  # no command
                    {"command": hook},
                ],
            },
        )
        assert config["hooks"]["preToolUse"] == [{"command": hook}]

    def test_existing_config_merges_kiro_hooks(self, tmp_path: Path):
        """kiro_hooks are merged on refresh of existing config."""
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {"old": "hook"},
        }
        config = self._run_with_kiro_hooks(
            tmp_path,
            {"preToolUse": [{"matcher": "*", "command": self._make_hook(tmp_path, "guardian.sh")}]},
            existing=existing,
        )
        # Bundled hooks overwrite old hooks
        assert config["hooks"]["postToolUse"] == [
            {"matcher": "execute_bash", "command": "audit.sh"},
        ]
        # User hooks appended
        assert len(config["hooks"]["preToolUse"]) == 1
        assert config["hooks"]["preToolUse"][0]["matcher"] == "*"

    # -- Direct unit tests for _merge_kiro_hooks defensive branches --
    def test_merge_kiro_hooks_non_dict_user_hooks_returns_original(self):
        from kiro_crew.agent import _merge_kiro_hooks

        bundled = {"postToolUse": [{"command": "audit.sh"}]}
        assert _merge_kiro_hooks(bundled, ["bad"]) == bundled

    def test_merge_kiro_hooks_non_list_event_entries_skipped(self):
        from kiro_crew.agent import _merge_kiro_hooks

        bundled = {"postToolUse": [{"command": "audit.sh"}]}
        result = _merge_kiro_hooks(bundled, {"postToolUse": "not-a-list"})
        assert result == bundled

    def test_merge_kiro_hooks_non_dict_entry_in_list_skipped(self):
        from kiro_crew.agent import _merge_kiro_hooks

        result = _merge_kiro_hooks({}, {"preToolUse": ["just-a-string"]})
        assert result["preToolUse"] == []

    # -- Direct unit tests for _validate_hook_command --

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("relative/hook.sh", id="test_validate_rejects_relative_path"),
            pytest.param("/nonexistent/hook.sh", id="test_validate_rejects_nonexistent_file"),
        ],
    )
    def test_validate_rejects_a_path_that_names_no_hook_file(self, command: str):
        from kiro_crew.agent import _validate_hook_command

        assert _validate_hook_command(command, "test") is None

    def test_validate_rejects_shell_metacharacters(self, tmp_path: Path):
        from kiro_crew.agent import _validate_hook_command

        hook = self._make_hook(tmp_path)
        assert _validate_hook_command(hook + "; rm -rf /", "test") is None
        assert _validate_hook_command(hook + " | cat", "test") is None
        assert _validate_hook_command(hook + " $(evil)", "test") is None

    def test_validate_accepts_valid_hook(self, tmp_path: Path):
        from kiro_crew.agent import _validate_hook_command

        hook = self._make_hook(tmp_path)
        assert _validate_hook_command(hook, "test") is not None

    def test_merge_strips_extra_fields(self, tmp_path: Path):
        """Only command and matcher fields are kept; arbitrary keys are stripped."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {"preToolUse": [{"command": hook, "matcher": "*", "shell": True, "env": {"X": "1"}}]}
        result = _merge_kiro_hooks({}, user)
        assert result["preToolUse"] == [{"command": hook, "matcher": "*"}]

    @requires_symlinks
    def test_validate_rejects_symlink_to_sensitive(self, tmp_path: Path):
        """Symlinks resolving to sensitive paths are rejected."""
        from kiro_crew.agent import _validate_hook_command

        sensitive = tmp_path / ".ssh" / "key"
        sensitive.parent.mkdir(parents=True)
        sensitive.write_text("#!/bin/sh\n")
        sensitive.chmod(0o755)
        link = tmp_path / "hooks" / "sneaky.sh"
        link.parent.mkdir(parents=True)
        link.symlink_to(sensitive)
        with patch("kiro_crew.agent.is_sensitive_path", side_effect=lambda p: ".ssh" in p):
            assert _validate_hook_command(str(link), "test") is None

    def test_merge_rejects_non_string_matcher(self, tmp_path: Path):
        """Non-string matcher values are skipped (prevents TypeError and injection)."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {
            "preToolUse": [
                {"command": hook, "matcher": {"$regex": ".*"}},
                {"command": hook, "matcher": ["list"]},
                {"command": hook, "matcher": "*"},
            ]
        }
        result = _merge_kiro_hooks({}, user)
        assert len(result["preToolUse"]) == 1
        assert result["preToolUse"][0] == {"command": hook, "matcher": "*"}

    def test_merge_kiro_hooks_max_per_event_limit(self, tmp_path: Path):
        """At most _MAX_USER_HOOKS_PER_EVENT hooks are accepted per event."""
        from kiro_crew.agent import _MAX_USER_HOOKS_PER_EVENT, _merge_kiro_hooks

        hooks = [
            {"command": self._make_hook(tmp_path, f"hook_{i}.sh")}
            for i in range(_MAX_USER_HOOKS_PER_EVENT + 5)
        ]
        result = _merge_kiro_hooks({}, {"preToolUse": hooks})
        assert len(result["preToolUse"]) == _MAX_USER_HOOKS_PER_EVENT

    def test_merge_kiro_hooks_unknown_event_rejected(self):
        """Unknown event types are silently dropped."""
        from kiro_crew.agent import _merge_kiro_hooks

        result = _merge_kiro_hooks({}, {"onBadEvent": [{"command": "/bin/true"}]})
        assert "onBadEvent" not in result

    def test_merge_rejects_matcher_with_shell_metacharacters(self, tmp_path: Path):
        """Matchers with shell metacharacters are rejected."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {
            "preToolUse": [
                {"command": hook, "matcher": "tool; rm -rf /"},
                {"command": hook, "matcher": "tool | cat"},
                {"command": hook, "matcher": "$(evil)"},
                {"command": hook, "matcher": "tool name with spaces"},
                {"command": hook, "matcher": "*"},  # valid
            ]
        }
        result = _merge_kiro_hooks({}, user)
        assert len(result["preToolUse"]) == 1
        assert result["preToolUse"][0]["matcher"] == "*"

    def test_merge_rejects_oversized_matcher(self, tmp_path: Path):
        """Matchers exceeding max length are rejected."""
        from kiro_crew.agent import _MAX_MATCHER_LEN, _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {
            "preToolUse": [
                {"command": hook, "matcher": "a" * (_MAX_MATCHER_LEN + 1)},
                {"command": hook, "matcher": "valid"},
            ]
        }
        result = _merge_kiro_hooks({}, user)
        assert len(result["preToolUse"]) == 1
        assert result["preToolUse"][0]["matcher"] == "valid"

    def test_merge_global_hooks_limit(self, tmp_path: Path):
        """Total hooks across all events are capped at _MAX_TOTAL_USER_HOOKS."""
        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _merge_kiro_hooks

        user = {}
        for event in ("preToolUse", "postToolUse", "userPromptSubmit"):
            user[event] = [
                {"command": self._make_hook(tmp_path, f"{event}_{i}.sh")}
                for i in range(_MAX_TOTAL_USER_HOOKS)
            ]
        result = _merge_kiro_hooks({}, user)
        total = sum(len(v) for v in result.values() if isinstance(v, list))
        assert total == _MAX_TOTAL_USER_HOOKS


class TestKiroHooksFiltering:
    """Tests that hook keys internal to Kiro Crew are stripped from kiro-cli agent config."""

    def test_auto_approve_tools_stripped_from_agent_config(self, tmp_path: Path):
        """auto_approve_tools must not appear in kirocrew.json (kiro-cli rejects it)."""
        from kiro_crew.agent import _kiro_hooks_only

        hooks = {
            "auto_approve_tools": ["kirocrew browse *"],
            "postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}],
            "userPromptSubmit": [{"command": "metrics.sh"}],
        }
        result = _kiro_hooks_only(hooks)
        assert "auto_approve_tools" not in result
        assert "postToolUse" in result
        assert "userPromptSubmit" in result

    def test_auto_deny_tools_stripped_from_agent_config(self):
        """auto_deny_tools must not appear in kirocrew.json."""
        from kiro_crew.agent import _kiro_hooks_only

        hooks = {
            "auto_deny_tools": ["Dangerous*"],
            "preToolUse": [{"matcher": "*", "command": "/bin/true"}],
        }
        result = _kiro_hooks_only(hooks)
        assert "auto_deny_tools" not in result
        assert "preToolUse" in result

    def test_only_valid_kiro_events_preserved(self):
        """Only kiro-cli valid hook events survive filtering."""
        from kiro_crew.agent import _VALID_HOOK_EVENTS, _kiro_hooks_only

        hooks = {
            "auto_approve_tools": ["*"],
            "auto_deny_tools": ["*"],
            "auto_replies": [{"pattern": "x", "reply": "y"}],
            "transforms": [{"pattern": "x", "prefix": "y"}],
            "context_rules": [{"triggers": ["x"], "context": "y"}],
            "preToolUse": [{"command": "/bin/true"}],
            "postToolUse": [{"command": "/bin/true"}],
            "userPromptSubmit": [{"command": "/bin/true"}],
            "agentSpawn": [{"command": "/bin/true"}],
            "stop": [{"command": "/bin/true"}],
        }
        result = _kiro_hooks_only(hooks)
        assert set(result.keys()) == _VALID_HOOK_EVENTS

    def test_bundled_defaults_hook_keys_are_classified(self):
        """Pins the exact set of hook keys in bundled defaults.json.

        _VALID_HOOK_EVENTS derives from bundled keys minus _INTERNAL_HOOK_KEYS, so
        a new key is auto-accepted as an event by construction -- silent if it was
        actually meant to be internal (kiro-cli would then reject the whole spec at
        runtime). This ratchet forces an explicit choice: adding a bundled hook key
        means updating either this set (a real event) or _INTERNAL_HOOK_KEYS
        (Kiro-Crew-internal), never neither (a fail-loud guard)."""
        from kiro_crew.agent import _BUNDLED_CFG_DIR, _load_json

        bundled = _load_json(_BUNDLED_CFG_DIR / "defaults.json")
        bundled_hook_keys = set((bundled or {}).get("hooks", {}).keys())
        assert bundled_hook_keys == {"auto_approve_tools", "postToolUse"}, (
            f"bundled defaults.json hooks keys changed to {bundled_hook_keys} -- "
            "classify any new key as a real kiro-cli event (covered automatically "
            "via _VALID_HOOK_EVENTS) or Kiro-Crew-internal (add to "
            "_INTERNAL_HOOK_KEYS), then update this pinned set."
        )

    def test_sanitize_agent_hooks_repairs_owned_files_subtractively(self, tmp_path: Path):
        """The repair removes only Kiro Crew's legacy key from every owned spec."""
        from kiro_crew.agent import _hooks_sanitized_mtimes, _sanitize_agent_hooks
        from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

        kiro_dir = tmp_path / "agents"
        kiro_dir.mkdir()
        broken_config = {
            "name": "kirocrew",
            "hooks": {
                "auto_approve_tools": ["kirocrew browse *"],
                "postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}],
                "futureHookEvent": [{"command": "future.sh"}],
            },
        }
        for filename in OWNED_KIRO_AGENT_FILES:
            (kiro_dir / filename).write_text(json.dumps(broken_config))

        _hooks_sanitized_mtimes.clear()
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir),
            patch(
                "kiro_crew.agent_materialization.worker_agent."
                "_foreign_dashboard_author_spec_reason",
                return_value=None,
            ),
        ):
            # The dashboard-author file is repaired only when its content marks confirm it as
            # ours (the same attribution that guards the install); the other owned specs were
            # never user-creatable and are swept unconditionally.
            _sanitize_agent_hooks()

        for filename in OWNED_KIRO_AGENT_FILES:
            repaired = json.loads((kiro_dir / filename).read_text(encoding="utf-8"))
            assert "auto_approve_tools" not in repaired["hooks"]
            assert "postToolUse" in repaired["hooks"]
            assert "futureHookEvent" in repaired["hooks"]

    def test_sanitize_agent_hooks_repairs_a_bom_saved_owned_file(self, tmp_path: Path):
        """A hand-saved owned spec with a byte-order mark is repaired, not skipped."""
        from kiro_crew.agent import _hooks_sanitized_mtimes, _sanitize_agent_hooks
        from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

        kiro_dir = tmp_path / "agents"
        kiro_dir.mkdir()
        spec = {"name": "kirocrew", "hooks": {"auto_approve_tools": ["x"], "stop": []}}
        target = kiro_dir / sorted(OWNED_KIRO_AGENT_FILES)[0]
        target.write_bytes(codecs.BOM_UTF8 + json.dumps(spec).encode("utf-8"))

        _hooks_sanitized_mtimes.clear()
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir):
            _sanitize_agent_hooks()

        raw = target.read_bytes()
        assert not raw.startswith(codecs.BOM_UTF8)
        assert json.loads(raw)["hooks"] == {"stop": []}

    @pytest.mark.parametrize(
        "filename", ["other-tool.json", "kirocrew-custom.json", "sample-app--worker.json"]
    )
    def test_sanitize_agent_hooks_does_not_touch_unowned_files(self, tmp_path: Path, filename: str):
        """Foreign, prefix-lookalike, and app materialized specs stay byte-identical."""
        from kiro_crew.agent import _hooks_sanitized_mtimes, _sanitize_agent_hooks

        kiro_dir = tmp_path / "agents"
        kiro_dir.mkdir()
        original = json.dumps(
            {
                "name": "foreign-agent",
                "hooks": {
                    "auto_approve_tools": ["foreign tool"],
                    "futureHookEvent": [{"command": "future.sh"}],
                },
            },
            indent=2,
        )
        path = kiro_dir / filename
        path.write_text(original, encoding="utf-8")

        _hooks_sanitized_mtimes.clear()
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir):
            _sanitize_agent_hooks()

        assert path.read_text(encoding="utf-8") == original

    def test_sanitize_agent_hooks_skips_clean_file(self, tmp_path: Path):
        """_sanitize_agent_hooks does not rewrite configs that are already clean."""
        from kiro_crew.agent import _hooks_sanitized_mtimes, _sanitize_agent_hooks

        kiro_dir = tmp_path / "agents"
        kiro_dir.mkdir()
        clean_config = {
            "name": "kirocrew",
            "hooks": {
                "postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}],
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(clean_config))
        original_mtime = (kiro_dir / "kirocrew.json").stat().st_mtime

        _hooks_sanitized_mtimes.clear()
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir):
            _sanitize_agent_hooks()

        assert (kiro_dir / "kirocrew.json").stat().st_mtime == original_mtime

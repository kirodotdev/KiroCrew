"""An ACP backend's credential store must be unreadable to the agent.

Each experimental backend authenticates through its own vendor CLI, which
persists a live OAuth token under the user's home. The agent must not be able to
read the credential that authorises its own backend.

The parity test here is what keeps ``security._SENSITIVE_HOME_DIRS`` honest:
``security`` cannot import the backend registry (that is an import cycle), so the
list is written literally and this test is the contract that stops the two
drifting. Adding a backend with a credential path and forgetting the security
entry fails here rather than shipping an exposed token.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.acp.backends import credential_leaves


def test_acp_backend_credentials_are_protected() -> None:
    """Every registered backend's credential leaf is on the sensitive list."""
    missing = [leaf for leaf in credential_leaves() if leaf not in security._SENSITIVE_HOME_DIRS]
    assert not missing, (
        f"ACP backend credential paths absent from _SENSITIVE_HOME_DIRS: {missing}. "
        "security.py cannot import the registry (import cycle), so add the leaf "
        "literally next to the other backend entries."
    )


def test_at_least_one_backend_declares_a_credential_path() -> None:
    """Guards the parity test above against passing vacuously.

    If every descriptor stopped declaring credential paths, the loop would have
    nothing to check and would pass with the protection removed.
    """
    assert credential_leaves()


class TestCodexAuthJson:
    """The Codex token store, through both enforcement paths."""

    @property
    def auth(self) -> str:
        return str(Path.home() / ".codex" / "auth.json")

    @property
    def config(self) -> str:
        return str(Path.home() / ".codex" / "config.toml")

    def test_fs_gate_blocks_the_token_store(self) -> None:
        assert security.is_sensitive_path(self.auth)

    def test_fs_gate_still_allows_the_ordinary_config(self) -> None:
        """config.toml must stay readable.

        The tool-gate refusal message tells the operator to inspect
        approval_policy in this file, so blocking it would make the remedy
        impossible to follow.
        """
        assert not security.is_sensitive_path(self.config)

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.codex/auth.json",
            "head -c 100 ~/.codex/auth.json",
            "base64 ~/.codex/auth.json",
            "cp ~/.codex/auth.json /tmp/stolen",
            "python3 -c \"print(open('~/.codex/auth.json').read())\"",
        ],
    )
    def test_bash_matcher_blocks_reads_by_any_verb(self, command: str) -> None:
        """The catch-all is verb-independent on purpose.

        A per-verb denied-command rule would need an entry per reader; naming the
        path blocks the readers nobody enumerated too.
        """
        assert security.is_sensitive_bash_command(command)

    def test_relative_traversal_is_covered_for_leaf_entries(self) -> None:
        """A leaf credential file is protected however its path is respelled.

        A ``cd`` into the directory, a ``;`` separator, and a ``$HOME`` variable
        all resolve to the same file, so each form is blocked. This is general to
        every FILE-shaped entry in _SENSITIVE_HOME_DIRS rather than specific to
        this backend — ``~/.docker/config.json`` and ``~/.kube/config`` are
        covered on the same tree, as are DIRECTORY entries (``.ssh``, ``.aws``).
        """
        assert security.is_sensitive_bash_command("cd ~/.codex && cat auth.json")
        assert security.is_sensitive_bash_command("cd ~/.codex; cat auth.json")
        assert security.is_sensitive_bash_command("cd $HOME/.codex && cat auth.json")

    def test_a_directory_entry_does_block_relative_traversal(self) -> None:
        """Pins that directory entries are covered alike, not just leaf files."""
        assert security.is_sensitive_bash_command("cd ~/.ssh && cat id_rsa")

    def test_bash_matcher_allows_reading_the_config(self) -> None:
        assert not security.is_sensitive_bash_command("cat ~/.codex/config.toml")

    def test_codex_home_override_reanchors_only_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(override))
        security._home_targets_cache.clear()

        assert security.is_sensitive_path(str(override / "auth.json"))
        assert not security.is_sensitive_path(str(override / "config.toml"))

    @pytest.mark.parametrize(
        "command",
        ["cat $CODEX_HOME/auth.json", "cat ${CODEX_HOME}/auth.json"],
    )
    def test_shell_expansion_of_codex_home_is_blocked(
        self,
        command: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
        security._home_targets_cache.clear()
        assert security.is_sensitive_bash_command(command)

    def test_shell_cd_into_codex_home_blocks_relative_token_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "custom-codex"
        monkeypatch.setenv("CODEX_HOME", str(override))
        security._home_targets_cache.clear()

        assert security.is_sensitive_bash_command('cd "$CODEX_HOME" && cat auth.json')
        assert security.is_sensitive_bash_command("cd ${CODEX_HOME}; cat auth.json")


class TestClaudeCredentialsJson:
    """The Claude OAuth token store, including supported root overrides."""

    @property
    def credentials(self) -> str:
        return str(Path.home() / ".claude" / ".credentials.json")

    @property
    def settings(self) -> str:
        return str(Path.home() / ".claude" / "settings.json")

    def test_default_token_store_is_blocked_but_settings_are_readable(self) -> None:
        assert security.is_sensitive_path(self.credentials)
        assert not security.is_sensitive_path(self.settings)

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.claude/.credentials.json",
            "base64 ~/.claude/.credentials.json",
            "python3 -c \"print(open('~/.claude/.credentials.json').read())\"",
            "cd ~/.claude && cat .credentials.json",
        ],
    )
    def test_bash_matcher_blocks_default_token_reads(self, command: str) -> None:
        assert security.is_sensitive_bash_command(command)

    @pytest.mark.parametrize("env_name", ["CLAUDE_CONFIG_DIR", "CLAUDE_HOME"])
    def test_root_override_reanchors_only_the_token(
        self,
        env_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override = tmp_path / env_name.casefold()
        monkeypatch.setenv(env_name, str(override))
        security._home_targets_cache.clear()

        assert security.is_sensitive_path(str(override / ".credentials.json"))
        assert not security.is_sensitive_path(str(override / "settings.json"))

    @pytest.mark.parametrize("env_name", ["CLAUDE_CONFIG_DIR", "CLAUDE_HOME"])
    def test_shell_expansion_and_relative_read_under_override_are_blocked(
        self,
        env_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override = tmp_path / env_name.casefold()
        monkeypatch.setenv(env_name, str(override))
        security._home_targets_cache.clear()

        assert security.is_sensitive_bash_command(f"cat ${{{env_name}}}/.credentials.json")
        assert security.is_sensitive_bash_command(f'cd "${{{env_name}}}" && cat .credentials.json')

    @pytest.mark.parametrize("env_name", ["CLAUDE_CONFIG_DIR", "CLAUDE_HOME"])
    def test_changed_override_invalidates_the_sensitive_target_cache(
        self,
        env_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setattr(security.time, "monotonic", lambda: 1000.0)
        monkeypatch.setenv(env_name, str(first))
        security._home_targets_cache.clear()
        assert security.is_sensitive_path(str(first / ".credentials.json"))

        monkeypatch.setenv(env_name, str(second))
        assert security.is_sensitive_path(str(second / ".credentials.json"))

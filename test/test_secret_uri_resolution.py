"""Tests for kiro_crew.mcp_gateway.secret_uri."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.secret_uri import resolve_secret_uris


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    """Create a temporary vault with test secrets."""
    from kiro_crew.secrets import SecretVault

    vault = SecretVault(tmp_path)
    vault._set_sync("MY_API_KEY", "sk-live-abc123")
    vault._set_sync("DB_PASS", "super-secret-password")
    return tmp_path


@pytest.fixture()
def empty_vault_dir(tmp_path: Path) -> Path:
    """Config dir with no vault secrets."""
    return tmp_path


class TestResolveSecretUris:
    """Tests for resolve_secret_uris."""

    def test_resolves_secret_uri(self, vault_dir: Path) -> None:
        env = {"API_KEY": "secret://MY_API_KEY", "OTHER": "plain-value"}
        result, keys = resolve_secret_uris(env, vault_dir)
        assert result["API_KEY"] == "sk-live-abc123"
        assert result["OTHER"] == "plain-value"
        assert keys == {"API_KEY"}

    def test_resolves_multiple_secret_uris(self, vault_dir: Path) -> None:
        env = {
            "API_KEY": "secret://MY_API_KEY",
            "PASSWORD": "secret://DB_PASS",
            "HOST": "localhost",
        }
        result, keys = resolve_secret_uris(env, vault_dir)
        assert result["API_KEY"] == "sk-live-abc123"
        assert result["PASSWORD"] == "super-secret-password"
        assert result["HOST"] == "localhost"
        assert keys == {"API_KEY", "PASSWORD"}

    def test_plain_values_pass_through(self, vault_dir: Path) -> None:
        env = {
            "HOST": "localhost",
            "PORT": "5432",
            "PATH": "/usr/bin:/usr/local/bin",
        }
        result, keys = resolve_secret_uris(env, vault_dir)
        assert result == env
        assert keys == set()

    def test_missing_secret_raises_valueerror(self, vault_dir: Path) -> None:
        env = {"KEY": "secret://NONEXISTENT_SECRET"}
        with pytest.raises(ValueError, match="NONEXISTENT_SECRET"):
            resolve_secret_uris(env, vault_dir)

    def test_error_message_includes_env_var_name(self, vault_dir: Path) -> None:
        env = {"MY_VAR": "secret://MISSING"}
        with pytest.raises(ValueError, match="MY_VAR"):
            resolve_secret_uris(env, vault_dir)

    def test_error_message_includes_secret_name(self, vault_dir: Path) -> None:
        env = {"KEY": "secret://MISSING"}
        with pytest.raises(ValueError, match="MISSING"):
            resolve_secret_uris(env, vault_dir)

    def test_empty_env(self, vault_dir: Path) -> None:
        result, keys = resolve_secret_uris({}, vault_dir)
        assert result == {}
        assert keys == set()

    def test_secret_uri_prefix_only_no_name(self, empty_vault_dir: Path) -> None:
        """secret:// with empty name still tries to resolve (and fails)."""
        # The regex requires at least one char after secret://
        env = {"KEY": "secret://"}
        # Should pass through since regex requires .+ after secret://
        result, keys = resolve_secret_uris(env, empty_vault_dir)
        assert result["KEY"] == "secret://"
        assert keys == set()

    def test_partial_secret_prefix_not_resolved(self, vault_dir: Path) -> None:
        """Values that look like but don't match secret:// pass through."""
        env = {
            "A": "secret:/MY_API_KEY",  # single slash
            "B": "Secret://MY_API_KEY",  # wrong case
            "C": "xsecret://MY_API_KEY",  # prefix text
            "D": "https://secret.example.com",  # totally different
        }
        result, keys = resolve_secret_uris(env, vault_dir)
        # None should be resolved
        assert result == env
        assert keys == set()

    def test_returns_new_dict(self, vault_dir: Path) -> None:
        """Original env dict is not mutated."""
        env = {"KEY": "secret://MY_API_KEY"}
        original = dict(env)
        resolve_secret_uris(env, vault_dir)
        assert env == original

    def test_secrets_cleared_after_pop(self, vault_dir: Path) -> None:
        """Caller can pop secret_keys to remove plaintext from parent memory."""
        env = {"API_KEY": "secret://MY_API_KEY", "HOST": "localhost"}
        result, secret_keys = resolve_secret_uris(env, vault_dir)

        # Simulate what gatewayd does after spawn: pop secret keys
        for key in secret_keys:
            result.pop(key, None)

        # Secret is gone from the dict; non-secret remains
        assert "API_KEY" not in result
        assert result["HOST"] == "localhost"

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
        with pytest.raises(ValueError, match="does not .{0,10}exist in the vault"):
            resolve_secret_uris(env, vault_dir)

    def test_error_message_includes_env_var_name(self, vault_dir: Path) -> None:
        env = {"MY_VAR": "secret://MISSING"}
        with pytest.raises(ValueError, match="MY_VAR"):
            resolve_secret_uris(env, vault_dir)

    def test_error_message_names_the_env_key_not_the_secret_name(self, vault_dir: Path) -> None:
        """The missing-secret error names the env var, never the secret name.

        Storable names may contain injection-capable characters, so the sink
        rule is: no resolution error echoes the referenced name.
        """
        env = {"KEY": "secret://MISSING"}
        with pytest.raises(ValueError) as exc:
            resolve_secret_uris(env, vault_dir)
        msg = str(exc.value)
        assert "'KEY'" in msg
        assert "MISSING" not in msg

    def test_empty_env(self, vault_dir: Path) -> None:
        result, keys = resolve_secret_uris({}, vault_dir)
        assert result == {}
        assert keys == set()

    def test_secret_uri_prefix_only_no_name_raises(self, empty_vault_dir: Path) -> None:
        """secret:// with an empty name now fails closed (was: passed through).

        The literal ``secret://`` template must never reach the child server
        env — an empty name is a malformed reference, so it raises before any
        vault read.
        """
        env = {"KEY": "secret://"}
        with pytest.raises(ValueError, match="malformed secret:// reference"):
            resolve_secret_uris(env, empty_vault_dir)

    @pytest.mark.parametrize(
        "bad_name",
        [
            " ",  # whitespace only — strips to empty at the store, can never exist
            " MY_KEY",  # leading space — the store strips it, cannot round-trip
            "MY_KEY\n",  # trailing newline — stripped at the store
            "MY_KEY ",  # trailing space — stripped at the store
        ],
    )
    def test_invalid_secret_name_raises(self, empty_vault_dir: Path, bad_name: str) -> None:
        """A name that cannot round-trip through the store fails closed.

        The store ``.strip()``s names before writing, so an empty name or one
        with leading/trailing whitespace can never match a stored entry — the
        reference is malformed by construction and must not pass the literal
        template into the child env.
        """
        env = {"KEY": f"secret://{bad_name}"}
        with pytest.raises(ValueError, match="malformed secret:// reference"):
            resolve_secret_uris(env, empty_vault_dir)

    def test_missing_secret_error_never_echoes_the_name(self, empty_vault_dir: Path) -> None:
        """No resolution error ever echoes the secret name (CWE-117 sink guard).

        Storable names may legally contain newlines, bidi overrides, or
        zero-width characters; charset filtering here would strand stored
        entries, so injection is closed at the SINK instead — the error names
        only the operator-declared env-var key, never the referenced name.
        """
        env = {"API_KEY": "secret://MY\nEVIL\u202eKEY"}
        with pytest.raises(ValueError) as exc:
            resolve_secret_uris(env, empty_vault_dir)
        msg = str(exc.value)
        assert "API_KEY" in msg
        assert "EVIL" not in msg
        assert "\n" not in msg.replace("\\n", "")
        assert "\u202e" not in msg

    def test_punctuation_names_the_vault_accepts_resolve(self, tmp_path: Path) -> None:
        """Names the vault legitimately stores (hyphen/dot) are NOT rejected.

        The dashboard set path (`/api/secrets`) accepts any non-empty stripped
        name, so validation must not be stricter than what the vault can hold —
        only control/format/line-break chars and unstorable shapes are rejected.
        """
        from kiro_crew.secrets import SecretVault

        vault = SecretVault(tmp_path)
        vault._set_sync("my-api-key", "v-hyphen")
        vault._set_sync("my.token", "v-dot")
        env = {
            "A": "secret://my-api-key",
            "B": "secret://my.token",
        }
        result, keys = resolve_secret_uris(env, tmp_path)
        assert result["A"] == "v-hyphen"
        assert result["B"] == "v-dot"
        assert keys == {"A", "B"}

    def test_valid_name_with_interior_space_resolves(self, tmp_path: Path) -> None:
        """A stored name containing an ordinary space stays resolvable.

        The dashboard stores ``MY KEY`` (any non-empty stripped string), so a
        ``secret://MY KEY`` reference must resolve — rejecting it would make
        the stored entry unreachable and block MCP spawn for a name the
        product's own write path accepted.
        """
        from kiro_crew.secrets import SecretVault

        vault = SecretVault(tmp_path)
        vault._set_sync("MY KEY", "v-space")
        env = {"A": "secret://MY KEY"}
        result, keys = resolve_secret_uris(env, tmp_path)
        assert result["A"] == "v-space"
        assert keys == {"A"}

    def test_stored_unicode_and_control_names_resolve(self, tmp_path: Path) -> None:
        """Every storable name resolves — including unusual Unicode.

        The write path (`/api/secrets`) accepts any non-empty stripped string,
        so names with a ZWJ, private-use character, or interior newline are
        real vault entries. Rejecting them at resolution would abort MCP spawn
        for a name the product accepted; injection safety comes from never
        echoing names in errors, not from refusing to resolve.
        """
        from kiro_crew.secrets import SecretVault

        vault = SecretVault(tmp_path)
        vault._set_sync("MY\u200dKEY", "v-zwj")  # zero-width joiner (Cf)
        vault._set_sync("MY\ue000KEY", "v-pua")  # private use (Co)
        vault._set_sync("MY\nKEY", "v-nl")  # interior newline (Cc)
        env = {
            "A": "secret://MY\u200dKEY",
            "B": "secret://MY\ue000KEY",
            "C": "secret://MY\nKEY",
        }
        result, keys = resolve_secret_uris(env, tmp_path)
        assert result["A"] == "v-zwj"
        assert result["B"] == "v-pua"
        assert result["C"] == "v-nl"
        assert keys == {"A", "B", "C"}

    def test_valid_underscore_and_digits_name_resolves(self, tmp_path: Path) -> None:
        """A name matching the env-var-key grammar resolves normally."""
        from kiro_crew.secrets import SecretVault

        vault = SecretVault(tmp_path)
        vault._set_sync("JIRA_TOKEN_9f8e", "tok-123")
        vault._set_sync("_LEADING_UNDERSCORE", "u-1")
        env = {
            "A": "secret://JIRA_TOKEN_9f8e",
            "B": "secret://_LEADING_UNDERSCORE",
        }
        result, keys = resolve_secret_uris(env, tmp_path)
        assert result["A"] == "tok-123"
        assert result["B"] == "u-1"
        assert keys == {"A", "B"}

    def test_batch_reads_store_once_for_multiple_refs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """K secret refs trigger exactly one store load and one key read.

        Counts calls on the SecretVault class methods (reader-agnostic: patched
        on the class, so it holds regardless of how get_many is wired
        internally) to prove the spawn path no longer re-reads the store and
        key file per reference.
        """
        from kiro_crew.secrets import SecretVault
        from kiro_crew.secrets.vault import SecretVault as VaultClass

        vault = SecretVault(tmp_path)
        vault._set_sync("S1", "v1")
        vault._set_sync("S2", "v2")
        vault._set_sync("S3", "v3")

        load_calls = 0
        key_calls = 0
        orig_load = VaultClass._load_entries
        orig_key = VaultClass._get_or_create_key

        def counting_load(self):  # type: ignore[no-untyped-def]
            nonlocal load_calls
            load_calls += 1
            return orig_load(self)

        def counting_key(self):  # type: ignore[no-untyped-def]
            nonlocal key_calls
            key_calls += 1
            return orig_key(self)

        monkeypatch.setattr(VaultClass, "_load_entries", counting_load)
        monkeypatch.setattr(VaultClass, "_get_or_create_key", counting_key)

        env = {
            "A": "secret://S1",
            "B": "secret://S2",
            "C": "secret://S3",
            "D": "plain",
        }
        result, keys = resolve_secret_uris(env, tmp_path)
        assert result["A"] == "v1"
        assert result["B"] == "v2"
        assert result["C"] == "v3"
        assert result["D"] == "plain"
        assert keys == {"A", "B", "C"}
        # Exactly one store load and one key read for 3 references.
        assert load_calls == 1
        assert key_calls == 1

    def test_no_vault_touch_when_no_refs(
        self, empty_vault_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pass-through-only env never constructs a vault read."""
        from kiro_crew.secrets.vault import SecretVault as VaultClass

        def boom(self):  # type: ignore[no-untyped-def]
            raise AssertionError("vault must not be read when there are no refs")

        monkeypatch.setattr(VaultClass, "_load_entries", boom)
        env = {"HOST": "localhost", "PORT": "5432"}
        result, keys = resolve_secret_uris(env, empty_vault_dir)
        assert result == env
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


class TestSecretUriPrefixConstant:
    """Single-source-of-truth for the ``secret://`` scheme prefix.

    ``SECRET_URI_PREFIX`` is defined once in
    ``kiro_crew.mcp_gateway.secret_uri`` and imported by
    ``kiro_crew.secrets.migrate``.  These tests pin that relationship so the
    writer (migrate) and the reader (secret_uri) cannot drift.
    """

    def test_prefix_value(self) -> None:
        """SECRET_URI_PREFIX equals the expected scheme string."""
        from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

        assert SECRET_URI_PREFIX == "secret://"

    def test_migrate_imports_same_object(self) -> None:
        """migrate._SECRET_URI_PREFIX IS the same object as SECRET_URI_PREFIX.

        Identity (``is``) rather than equality (``==``) proves that migrate
        imports the constant from ``secret_uri`` rather than re-spelling it.
        If the import is ever severed and the literal is re-inlined in
        migrate.py, ``is`` fails even though ``==`` would still pass.
        """
        import kiro_crew.secrets.migrate as _migrate_mod
        from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

        assert _migrate_mod._SECRET_URI_PREFIX is SECRET_URI_PREFIX

"""The account era a shared deepseek (dsh) process is placed under.

``providers.chat_share.deepseek_spawn_identity`` keys a shared process on the dsh
home, the ``agent.deepseek_env`` mapping and the vault revision of each secret it
names, without decrypting any of them, and answers ``""`` (share with nothing) for
a key the child would take from its own environment.
"""

from __future__ import annotations

from types import SimpleNamespace


class TestTheDeepseekEra:
    def _deepseek(self, tmp_path, monkeypatch, mapping: dict[str, str]):
        """A config whose ``agent.deepseek_env`` is *mapping*, and a vault under tmp."""
        import kiro_crew.config.loader as loader_mod
        import kiro_crew.config.paths as paths_mod
        from kiro_crew.secrets import SecretVault

        cfg = SimpleNamespace(agent=SimpleNamespace(deepseek_env=dict(mapping)))
        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        vault_dir = tmp_path / "crew"
        vault_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(paths_mod, "config_dir", lambda: vault_dir)
        return cfg, SecretVault(vault_dir)

    def test_the_deepseek_era_follows_the_vault_entry_not_the_key(self, tmp_path, monkeypatch):
        from kiro_crew.providers.chat_share import deepseek_spawn_identity

        cfg, vault = self._deepseek(
            tmp_path, monkeypatch, {"DEEPSEEK_API_KEY": "secret://deepseek-key"}
        )
        # The referenced secret is not in the vault: nothing can be named.
        env = {"DSH_HOME": str(tmp_path / "dsh")}
        assert deepseek_spawn_identity(env) == ""
        vault.set_sync("deepseek-key", "sk-first")
        first = deepseek_spawn_identity(env)
        assert first.startswith("deepseek:") and "sk-first" not in first
        # Read again with nothing changed: the same era, so a second chat joins.
        assert deepseek_spawn_identity(dict(env)) == first
        # A rotated key (any vault write) founds a new process.
        vault.set_sync("deepseek-key", "sk-second")
        rotated = deepseek_spawn_identity(env)
        assert rotated not in ("", first)
        # Another dsh home, or a remapped secret, founds a new process too.
        assert deepseek_spawn_identity({"DSH_HOME": str(tmp_path / "other")}) not in ("", rotated)
        vault.set_sync("deepseek-key-2", "sk-third")
        cfg.agent.deepseek_env = {"DEEPSEEK_API_KEY": "secret://deepseek-key-2"}
        assert deepseek_spawn_identity(env) not in ("", rotated)
        # A literal key in the mapping is a mapping Crew refuses: fail closed.
        cfg.agent.deepseek_env = {"DEEPSEEK_API_KEY": "sk-plain"}
        assert deepseek_spawn_identity(env) == ""

    def test_the_deepseek_era_never_decrypts_the_vault(self, tmp_path, monkeypatch):
        from kiro_crew.providers.chat_share import deepseek_spawn_identity
        from kiro_crew.secrets import SecretVault

        _cfg, vault = self._deepseek(
            tmp_path, monkeypatch, {"DEEPSEEK_API_KEY": "secret://deepseek-key"}
        )
        vault.set_sync("deepseek-key", "sk-first")

        def _no_read(*_a, **_kw):
            raise AssertionError("the era decrypted a secret")

        for name in ("get", "get_many", "get_with_revision", "get_items_many", "_decrypt_entry"):
            monkeypatch.setattr(SecretVault, name, _no_read)
        assert deepseek_spawn_identity({"DSH_HOME": str(tmp_path / "dsh")}).startswith("deepseek:")

    def test_a_deepseek_key_from_the_environment_never_shares(self, tmp_path, monkeypatch):
        """A key the child takes from its inherited environment cannot be told rotated
        without reading it, so such a chat gets a key nothing matches."""
        from kiro_crew.providers.chat_share import deepseek_spawn_identity

        self._deepseek(tmp_path, monkeypatch, {})
        home = tmp_path / "dsh"
        env = {"DSH_HOME": str(home)}
        assert deepseek_spawn_identity(env).startswith("deepseek:")
        assert deepseek_spawn_identity({**env, "DEEPSEEK_API_KEY": "sk-env"}) == ""
        # A key variable the home's own config names counts the same way.
        (home / "profiles" / "acp").mkdir(parents=True)
        (home / "cordis.patch.yml").write_text(
            "- name: '@deepseek-ai/dsh-llm-pi-ai'\n"
            "  config:\n"
            "    providers:\n"
            "      gw:\n"
            "        apiKeyEnv: GW_TOKEN\n",
            encoding="utf-8",
        )
        assert deepseek_spawn_identity({**env, "GW_TOKEN": "t"}) == ""
        assert deepseek_spawn_identity({**env, "OTHER_TOKEN": "t"}).startswith("deepseek:")
        # The same variable fed from Crew's vault is the vault's key, not an ambient one.
        _cfg, vault = self._deepseek(tmp_path, monkeypatch, {"GW_TOKEN": "secret://gw"})
        vault.set_sync("gw", "t1")
        assert deepseek_spawn_identity({**env, "GW_TOKEN": "t"}).startswith("deepseek:")
        # A config layer that cannot be parsed: fail closed.
        (home / "profiles" / "acp" / "cordis.yml").write_text("key: [unclosed\n", encoding="utf-8")
        assert deepseek_spawn_identity(env) == ""

"""``agent.child_env_defaults``: operator env defaults for every kiro-cli child.

kiro-cli sizes its tokio and rayon pools by core count, so on a many-core host
that runs dozens of kiro-cli processes each one starts ~256 threads it never
needs. The operator can cap them (``TOKIO_WORKER_THREADS``, ``RAYON_NUM_THREADS``)
through this opt-in mapping without changing the gateway's own environment.

The contract under test:

* the mapping is empty by default, so nothing changes until an operator opts in;
* only ``TOKIO_WORKER_THREADS`` and ``RAYON_NUM_THREADS`` are accepted, each a
  decimal integer string in [1, 1024]; any other entry is dropped with a warning
  at config load, not at spawn;
* a default is applied only when the key is NOT already in the child's inherited
  environment (ambient or per-session overlay) -- an explicit value, even an
  empty one, always wins;
* both kiro-cli spawn paths apply it -- ``AcpRuntime`` through the kiro-family
  harness hook (kiro and KAS, which is also a kiro-cli process) and the
  auxiliary ``AcpClient`` spawn through ``_resolve_spawn_env`` -- and a foreign
  host does not.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.acp import child_env_defaults as ced
from kiro_crew.acp.child_env_defaults import apply_child_env_defaults
from kiro_crew.config.sections import AgentConfig, coerce_child_env_defaults

_CAPS = {"TOKIO_WORKER_THREADS": "4", "RAYON_NUM_THREADS": "4"}


@pytest.fixture
def configured(monkeypatch):
    """Make the configured mapping whatever the test says, without a config file."""

    def _set(mapping: dict[str, str]) -> None:
        monkeypatch.setattr(ced, "_configured_defaults", lambda: dict(mapping), raising=True)

    return _set


@pytest.fixture
def quiet_credentials(monkeypatch):
    """The harness hooks also inject/strip the API key, which reads the data home."""
    monkeypatch.setattr(
        "kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None, raising=True
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None, raising=True
    )


class TestTheConfigKey:
    def test_default_is_empty(self) -> None:
        assert AgentConfig().child_env_defaults == {}

    def test_valid_entries_are_kept(self) -> None:
        assert coerce_child_env_defaults(dict(_CAPS)) == _CAPS

    def test_non_mapping_is_empty(self) -> None:
        assert coerce_child_env_defaults(["TOKIO_WORKER_THREADS=4"]) == {}
        assert coerce_child_env_defaults(None) == {}

    @pytest.mark.parametrize(
        "key",
        [
            "NODE_OPTIONS",
            "LD_PRELOAD",
            "KIROCREW_SESSION_KEY",
            "KIRO_API_KEY",
            "OPENBLAS_NUM_THREADS",
            "tokio_worker_threads",
            " TOKIO_WORKER_THREADS",
        ],
    )
    def test_unlisted_names_are_dropped_with_a_warning(self, key, caplog) -> None:
        """Any variable but the two thread-pool knobs would reach every process
        the agent runs, so only the allowlist is accepted."""
        with caplog.at_level(logging.WARNING):
            out = coerce_child_env_defaults({key: "s3cr3t-value", "RAYON_NUM_THREADS": "4"})
        assert out == {"RAYON_NUM_THREADS": "4"}
        assert key in caplog.text
        assert "s3cr3t-value" not in caplog.text

    def test_a_non_string_name_is_dropped(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            out = coerce_child_env_defaults({7: "4", "RAYON_NUM_THREADS": "4"})
        assert out == {"RAYON_NUM_THREADS": "4"}
        assert "child_env_defaults" in caplog.text

    @pytest.mark.parametrize(
        "value",
        [
            "0",
            "-1",
            "1025",
            "four",
            "",
            " 4",
            "+4",
            "4.0",
            "\u0664",
            "9" * 40,
            4,
            None,
            True,
            ["4"],
        ],
    )
    def test_invalid_values_are_dropped_with_a_warning(self, value, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            out = coerce_child_env_defaults({"TOKIO_WORKER_THREADS": value})
        assert out == {}
        assert "TOKIO_WORKER_THREADS" in caplog.text
        if isinstance(value, str) and value:
            assert repr(value) not in caplog.text

    @pytest.mark.parametrize("value", ["1", "4", "1024"])
    def test_in_range_values_are_kept(self, value) -> None:
        assert coerce_child_env_defaults({"TOKIO_WORKER_THREADS": value}) == {
            "TOKIO_WORKER_THREADS": value
        }

    def test_a_value_is_stored_in_canonical_form(self) -> None:
        assert coerce_child_env_defaults({"RAYON_NUM_THREADS": "04"}) == {"RAYON_NUM_THREADS": "4"}

    def test_directly_constructed_config_is_coerced(self) -> None:
        cfg = AgentConfig(child_env_defaults={"RAYON_NUM_THREADS": "2", "NODE_OPTIONS": "1"})
        assert cfg.child_env_defaults == {"RAYON_NUM_THREADS": "2"}

    def test_loader_reads_it_from_config_json(self) -> None:
        from kiro_crew.config import loader

        agent = loader._build_agent_config({"child_env_defaults": dict(_CAPS)})
        assert agent.child_env_defaults == _CAPS
        assert loader._build_agent_config({}).child_env_defaults == {}


class TestOnlyWhenUnset:
    def test_unset_keys_get_the_default(self, configured) -> None:
        configured(_CAPS)
        env: dict[str, str] = {"PATH": "/bin"}
        apply_child_env_defaults(env)
        assert env == {"PATH": "/bin", **_CAPS}

    def test_an_inherited_value_wins(self, configured) -> None:
        configured(_CAPS)
        env = {"TOKIO_WORKER_THREADS": "16"}
        apply_child_env_defaults(env)
        assert env["TOKIO_WORKER_THREADS"] == "16"
        assert env["RAYON_NUM_THREADS"] == "4"

    def test_an_inherited_empty_value_wins(self, configured) -> None:
        configured(_CAPS)
        env = {"RAYON_NUM_THREADS": ""}
        apply_child_env_defaults(env)
        assert env["RAYON_NUM_THREADS"] == ""

    def test_reads_the_configured_mapping(self, configured) -> None:
        configured(_CAPS)
        env: dict[str, str] = {}
        apply_child_env_defaults(env)
        assert env == _CAPS

    def test_empty_config_changes_nothing(self, configured) -> None:
        configured({})
        env = {"PATH": "/bin"}
        apply_child_env_defaults(env)
        assert env == {"PATH": "/bin"}

    def test_unreadable_config_changes_nothing(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("config broken")

        monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", _boom, raising=True)
        env = {"PATH": "/bin"}
        apply_child_env_defaults(env)
        assert env == {"PATH": "/bin"}


class TestEveryKiroCliSpawnPath:
    def test_the_kiro_host_applies_it(self, configured, quiet_credentials) -> None:
        from kiro_crew.acp.harness.kiro import KiroHarness

        configured(_CAPS)
        env: dict[str, str] = {}
        KiroHarness().apply_spawn_env(env)
        assert {k: env[k] for k in _CAPS} == _CAPS

    def test_the_kas_host_applies_it(self, configured, quiet_credentials) -> None:
        from kiro_crew.acp.harness.kas import KasHarness

        configured(_CAPS)
        env: dict[str, str] = {}
        KasHarness().apply_spawn_env(env)
        assert {k: env[k] for k in _CAPS} == _CAPS

    def test_the_kiro_host_keeps_an_inherited_value(self, configured, quiet_credentials) -> None:
        from kiro_crew.acp.harness.kiro import KiroHarness

        configured(_CAPS)
        env = {"TOKIO_WORKER_THREADS": "32"}
        KiroHarness().apply_spawn_env(env)
        assert env["TOKIO_WORKER_THREADS"] == "32"

    def test_a_foreign_host_does_not_get_it(self, configured) -> None:
        from kiro_crew.acp.harness.codex import CodexHarness

        configured(_CAPS)
        env: dict[str, str] = {}
        CodexHarness().apply_spawn_env(env)
        assert not set(_CAPS) & set(env)

    def test_acp_client_kiro_spawn_hop_applies_it(self, configured, quiet_credentials) -> None:
        from kiro_crew.acp.client import _resolve_spawn_env

        configured(_CAPS)
        env = _resolve_spawn_env({}, kiro_api_key=True)
        assert {k: env[k] for k in _CAPS} == _CAPS

    def test_acp_client_foreign_hop_does_not(self, configured, quiet_credentials) -> None:
        from kiro_crew.acp.client import _resolve_spawn_env

        configured(_CAPS)
        env = _resolve_spawn_env({}, kiro_api_key=False)
        assert not set(_CAPS) & set(env)

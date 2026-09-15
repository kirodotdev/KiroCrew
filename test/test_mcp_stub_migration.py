"""Migration from the deprecated ``poolable_servers`` to ``stub_servers``.

This is the guarantee an existing install depends on across the upgrade that
makes the stub opt-in: a machine that had pooled servers keeps its stubs, a
fresh machine gets none, and an operator who deliberately cleared the list is
not silently re-stubbed from the old key.

The decisive property is that the choice is made on KEY PRESENCE, not on
truthiness. ``stub_servers: []`` and "no ``stub_servers`` at all" are different
statements about intent, and only one of them may fall back.
"""

import json
import unittest.mock
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig, _resolve_stub_servers


def _load_from_dict(data: object, tmp_path: Path) -> KiroCrewConfig:
    """Write *data* into the test's own tmp_path and load via KiroCrewConfig.load().

    The file stays under ``tmp_path`` rather than the shared system temp dir, so
    concurrent workers cannot see each other's config.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch(
        "kiro_crew.config.loader.config_path",
        return_value=cfg,
    ):
        return KiroCrewConfig.load()


class TestResolver:
    """The pure decision, isolated from config loading."""

    def test_the_new_key_wins_when_present(self) -> None:
        assert _resolve_stub_servers(
            {"stub_servers": ["alpha-mcp"], "poolable_servers": ["beta-mcp"]}
        ) == ["alpha-mcp"]

    def test_an_explicitly_empty_new_key_does_not_fall_back(self) -> None:
        """The load-bearing case.

        An operator who cleared the list wrote ``[]`` on purpose. Reading that as
        "falsy, so try the old key" would re-stub every server they had just
        turned off — the exact default this change exists to remove.
        """
        assert _resolve_stub_servers(
            {"stub_servers": [], "poolable_servers": ["beta-mcp", "gamma-mcp"]}
        ) == []

    def test_the_deprecated_key_is_read_only_when_the_new_one_is_absent(self) -> None:
        """An existing install keeps its behaviour: a pooled server already ran
        behind a stub, since pooling was only reachable through one."""
        assert _resolve_stub_servers(
            {"enabled": True, "poolable_servers": ["beta-mcp"]}
        ) == ["beta-mcp"]

    def test_a_disabled_gateway_migrates_to_nothing(self) -> None:
        """The other direction of the same guarantee, and the one that bites.

        Before the stub was its own decision the broker was gated on ``enabled``
        alone, so this config was running NO broker, NO overlay and NO stub --
        the allowlist sat there inert. Migrating it would hand the operator a
        daemon plus a stub process per server on upgrade, which is exactly the
        unrequested topology change this design removes.
        """
        assert _resolve_stub_servers(
            {"enabled": False, "poolable_servers": ["beta-mcp", "gamma-mcp"]}
        ) == []

    def test_an_absent_enabled_key_counts_as_disabled(self) -> None:
        """``enabled`` defaults to false, so an allowlist with no ``enabled`` was
        also inert and must not be migrated."""
        assert _resolve_stub_servers({"poolable_servers": ["beta-mcp"]}) == []

    def test_an_explicit_new_key_ignores_enabled_entirely(self) -> None:
        """Once the operator writes ``stub_servers`` there is nothing to migrate,
        so the sharing switch has no say in what is stubbed -- the two layers are
        independent by design."""
        assert _resolve_stub_servers(
            {"enabled": False, "stub_servers": ["alpha-mcp"]}
        ) == ["alpha-mcp"]
        assert _resolve_stub_servers(
            {"enabled": True, "stub_servers": ["alpha-mcp"]}
        ) == ["alpha-mcp"]

    def test_a_fresh_install_stubs_nothing(self) -> None:
        assert _resolve_stub_servers({}) == []

    def test_junk_entries_are_dropped_rather_than_carried(self) -> None:
        assert _resolve_stub_servers(
            {"stub_servers": ["ok-mcp", "", None, 7, {"a": 1}, "also-ok"]}
        ) == ["ok-mcp", "also-ok"]

    def test_a_non_list_value_is_not_trusted(self) -> None:
        assert _resolve_stub_servers({"stub_servers": "alpha-mcp"}) == []
        assert _resolve_stub_servers({"poolable_servers": {"alpha-mcp": True}}) == []


class TestThroughTheLoader:
    """The resolver wired into ``KiroCrewConfig.load``, which is what ships."""

    def test_a_legacy_config_arrives_as_stub_servers(self, tmp_path) -> None:
        cfg = _load_from_dict(
            {"mcp_gateway": {"enabled": True, "poolable_servers": ["legacy-mcp"]}},
            tmp_path,
        )
        assert cfg.mcp_gateway.stub_servers == ["legacy-mcp"]

    def test_a_legacy_config_with_the_gateway_off_arrives_empty(self, tmp_path) -> None:
        """Nothing was running, so nothing starts running."""
        cfg = _load_from_dict(
            {"mcp_gateway": {"enabled": False, "poolable_servers": ["legacy-mcp"]}},
            tmp_path,
        )
        assert cfg.mcp_gateway.stub_servers == []
        assert cfg.mcp_gateway.enabled is False

    def test_a_cleared_list_survives_the_load(self, tmp_path) -> None:
        cfg = _load_from_dict(
            {
                "mcp_gateway": {
                    "enabled": True,
                    "stub_servers": [],
                    "poolable_servers": ["legacy-mcp"],
                }
            },
            tmp_path,
        )
        assert cfg.mcp_gateway.stub_servers == []

    def test_the_shipped_default_is_empty(self, tmp_path) -> None:
        """No mcp_gateway section at all — the state of a fresh install."""
        cfg = _load_from_dict({}, tmp_path)
        assert cfg.mcp_gateway.stub_servers == []

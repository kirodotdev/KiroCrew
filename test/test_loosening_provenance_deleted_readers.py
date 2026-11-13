"""A deleted keystone file whose endorsed copy is in force is that copy to every reader.

Where the sandbox seals ``config.json`` / ``config.local.json``, the load serves the
endorsed copy of a file that was deleted (``loosening_provenance.reconciled_bytes``). A
reader that checks ``is_file()`` / ``exists()`` first and answers "absent" sees a
different document from the load: a deleted overlay that still grants trust reads as
granting nothing, so a revoke or an uninstall leaves the grant in force. Each case below
endorses a document, deletes the file, and asks one such reader.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp home.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import pytest

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _enforcing(_floor_monkeypatch):
    _floor_monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    yield
    loader._invalidate_config_cache()


def _endorse_and_delete(leaf: str, document: Any) -> None:
    """An endorsed copy of *document* for *leaf*, and no file on disk."""
    lp.record(leaf, (json.dumps(document) + "\n").encode(), strict=True)
    (config_dir() / leaf).unlink(missing_ok=True)


def _overlay_owned_trust() -> object:
    from kiro_crew.dashboard.handlers import security_trusted_apps

    return security_trusted_apps._overlay_owned_trust_settings()


def _removal_blocked() -> object:
    from kiro_crew.apps import manager_uninstall

    return manager_uninstall.trust_grant_removal_blocked("foo") is not None


def _has_grant() -> object:
    from kiro_crew.apps import manager_uninstall

    return manager_uninstall._has_trust_grant("foo")


def _grant_repository() -> object:
    from kiro_crew.apps import manager_uninstall

    return manager_uninstall._trust_grant_repository("foo")


def _grant_local() -> object:
    from kiro_crew.apps import manager_uninstall

    return manager_uninstall._trust_grant_local("foo")


def _overlay_pins() -> object:
    from kiro_crew.config.loader_locations import overlay_pins

    return overlay_pins("agent", "apps_trusted")


def _overlay_default_agent() -> object:
    from kiro_crew.config.loader_writeback import _overlay_kiro_agent

    return _overlay_kiro_agent()


def _overlay_stt() -> object:
    from kiro_crew.config.superseded_defaults import _overlay_stt_provider

    return _overlay_stt_provider()


def _base_agent() -> object:
    from kiro_crew.apps.builtin_adoption_state import _base_agent_raw

    return _base_agent_raw()


def _overlay_gateway() -> object:
    from kiro_crew.dashboard.handlers.mcp_gateway_routes import _local_overlay_section

    return _local_overlay_section()


def _computer_use_config() -> object:
    from kiro_crew.dashboard.handlers.computer_use import _read_config_strict

    return _read_config_strict()


CASES: list[tuple[str, str, Any, Callable[[], object], object]] = [
    (
        "trusted_apps_overlay",
        lp.LEAF_CONFIG_LOCAL,
        {"agent": {"apps_trusted": ["foo"]}},
        _overlay_owned_trust,
        ["apps_trusted"],
    ),
    (
        "uninstall_overlay_grant",
        lp.LEAF_CONFIG_LOCAL,
        {"agent": {"apps_trusted": ["foo"]}},
        _removal_blocked,
        True,
    ),
    ("uninstall_has_grant", lp.LEAF_CONFIG, {"agent": {"apps_trusted": ["foo"]}}, _has_grant, True),
    (
        "uninstall_repository",
        lp.LEAF_CONFIG,
        {"agent": {"apps_trusted_repositories": {"foo": "repo"}}},
        _grant_repository,
        "repo",
    ),
    (
        "uninstall_local_marker",
        lp.LEAF_CONFIG,
        {"agent": {"apps_trusted_local": ["foo"]}},
        _grant_local,
        True,
    ),
    (
        "overlay_pins",
        lp.LEAF_CONFIG_LOCAL,
        {"agent": {"apps_trusted": []}},
        _overlay_pins,
        True,
    ),
    (
        "writeback_overlay_default_agent",
        lp.LEAF_CONFIG_LOCAL,
        {"agent": {"default_agent": "nova"}},
        _overlay_default_agent,
        "nova",
    ),
    (
        "superseded_overlay_stt",
        lp.LEAF_CONFIG_LOCAL,
        {"stt": {"provider": "off"}},
        _overlay_stt,
        "off",
    ),
    ("adoption_base_agent", lp.LEAF_CONFIG, {"agent": {"x": 1}}, _base_agent, {"x": 1}),
    (
        "mcp_gateway_overlay",
        lp.LEAF_CONFIG_LOCAL,
        {"mcp_gateway": {"enabled": True}},
        _overlay_gateway,
        {"enabled": True},
    ),
    ("computer_use_config", lp.LEAF_CONFIG, {"a": 1}, _computer_use_config, {"a": 1}),
]


@pytest.mark.parametrize(
    ("leaf", "document", "read", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_a_deleted_file_reads_as_its_endorsed_copy(leaf, document, read, expected) -> None:
    _endorse_and_delete(leaf, document)

    assert read() == expected


def test_the_trust_preflight_sees_a_deleted_overlay_grant() -> None:
    from kiro_crew.dashboard.handlers import security_trusted_apps as sta

    _endorse_and_delete(lp.LEAF_CONFIG_LOCAL, {"agent": {"apps_trusted": ["foo"]}})

    with pytest.raises(sta.TrustSettingOverlayOwned):
        asyncio.run(sta._preflight_agent_config_mutable())


def test_the_trust_preflight_refuses_a_deleted_base_whose_copy_is_not_an_object() -> None:
    from kiro_crew.dashboard.handlers import security_trusted_apps as sta

    _endorse_and_delete(lp.LEAF_CONFIG, [1])

    with pytest.raises(sta.ConfigCorruptError):
        asyncio.run(sta._preflight_agent_config_mutable())


def test_save_keeps_a_deleted_overlay_owned_value_out_of_config_json() -> None:
    from kiro_crew.config.loader import KiroCrewConfig, write_config_atomically

    write_config_atomically(config_dir() / "config.json", {"agent": {"bot_name": "Base"}})
    _endorse_and_delete(lp.LEAF_CONFIG_LOCAL, {"agent": {"bot_name": "Over"}})
    cfg = KiroCrewConfig.load()
    assert cfg.agent.bot_name == "Over"

    cfg.save()

    saved = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
    assert saved["agent"]["bot_name"] == "Base"


def test_a_save_of_a_deleted_base_waits_for_the_review() -> None:
    """A deleted config.json is pending as removed: a save would recreate it from the
    endorsed copy over the removal, so it is refused and the file stays absent."""
    from kiro_crew.config.loader import (
        KiroCrewConfig,
        PendingEditBlocksSave,
        write_config_atomically,
    )

    write_config_atomically(config_dir() / "config.local.json", {"agent": {"bot_name": "Over"}})
    _endorse_and_delete(lp.LEAF_CONFIG, {"agent": {"bot_name": "Base"}})
    cfg = KiroCrewConfig.load()

    with pytest.raises(PendingEditBlocksSave):
        cfg.save()

    assert not (config_dir() / "config.json").exists()


def test_a_missing_file_with_no_copy_is_still_absent() -> None:
    from kiro_crew.apps import manager_uninstall

    assert manager_uninstall.trust_grant_removal_blocked("foo") is None
    assert manager_uninstall._has_trust_grant("foo") is False
    assert _overlay_owned_trust() == []
    assert _computer_use_config() == {}
    assert _base_agent() == {}


def test_default_installs_keep_a_deleted_base_marker_an_overlay_list_replaces() -> None:
    from kiro_crew.apps.default_installs import _applied_names
    from kiro_crew.config.loader import write_config_atomically

    key = "apps_default_installs_applied"
    _endorse_and_delete(lp.LEAF_CONFIG, {"agent": {key: ["removed-app"]}})
    write_config_atomically(config_dir() / "config.local.json", {"agent": {key: ["other"]}})

    assert "removed-app" in _applied_names()


def test_tailnet_refuses_a_deleted_overlay_whose_copy_has_a_wrong_shaped_section(capsys) -> None:
    import argparse

    from kiro_crew.cli_commands import _tailnet

    _endorse_and_delete(lp.LEAF_CONFIG_LOCAL, {"registries": 5})

    with pytest.raises(SystemExit) as exc:
        _tailnet(argparse.Namespace(tailnet_action="status"))

    assert exc.value.code == 1
    assert "is not usable" in capsys.readouterr().err

"""The ``mcp`` config section that carries ``extra_path_dirs`` (issue #5083).

Separate from ``mcp_gateway``, which configures the sharing broker: these
settings govern how MCP servers are FOUND and launched, so they apply with the
broker off too. Validation of the directories themselves belongs to the
consumer (``kiro_crew.env.augmented_path``) -- see ``test_env.py`` -- so what is
pinned here is only that the section parses, survives the save round-trip, and
degrades instead of raising on a hand-edited value.
"""

from __future__ import annotations

import json
import os

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig, McpConfig


def _load_from(tmp_path, monkeypatch, data: dict) -> KiroCrewConfig:
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return KiroCrewConfig.load()


def test_defaults_to_empty_list():
    assert KiroCrewConfig().mcp.extra_path_dirs == []


def test_parses_and_preserves_order(tmp_path, monkeypatch):
    """Order is precedence order downstream, so the loader must not reorder it."""
    cfg = _load_from(
        tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": ["/opt/a/bin", "/opt/b/bin"]}}
    )
    assert cfg.mcp.extra_path_dirs == ["/opt/a/bin", "/opt/b/bin"]


def test_round_trips_through_to_dict(tmp_path, monkeypatch):
    """A setting dropped by to_dict() would be lost the next time anything saves."""
    cfg = _load_from(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": ["/opt/a/bin"]}})
    assert cfg.to_dict()["mcp"] == {"extra_path_dirs": ["/opt/a/bin"]}


def test_non_string_entries_dropped(tmp_path, monkeypatch):
    """The field is typed list[str] and to_dict() re-emits it verbatim, so a
    non-string must not survive the load into the saved config."""
    cfg = _load_from(
        tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": ["/opt/a/bin", 42, None, {}]}}
    )
    assert cfg.mcp.extra_path_dirs == ["/opt/a/bin"]


def test_malformed_section_degrades_to_default(tmp_path, monkeypatch):
    """config.json is hand-editable; a non-dict section must not abort the load."""
    cfg = _load_from(tmp_path, monkeypatch, {"mcp": "yes please"})
    assert cfg.mcp.extra_path_dirs == []


def test_malformed_value_degrades_to_default(tmp_path, monkeypatch):
    cfg = _load_from(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": "/opt/a/bin"}})
    assert cfg.mcp.extra_path_dirs == []


def test_load_publishes_the_setting_to_the_search_path(tmp_path, monkeypatch):
    """The setting only works because ``load()`` pushes it into
    ``kiro_crew.env``. Pinned end-to-end: without this call the config field
    parses correctly and changes nothing."""
    import kiro_crew.env as env_mod

    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    _load_from(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": ["/opt/pixi/bin"]}})
    assert "/opt/pixi/bin" in env_mod.mcp_search_path("").split(os.pathsep)


def test_load_republishes_so_a_removed_setting_clears(tmp_path, monkeypatch):
    import kiro_crew.env as env_mod

    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    _load_from(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": ["/opt/pixi/bin"]}})
    _load_from(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": []}})
    assert "/opt/pixi/bin" not in env_mod.mcp_search_path("").split(os.pathsep)


def test_defaults_path_also_clears_a_stale_snapshot(tmp_path, monkeypatch):
    """The load path taken when NEITHER config file can be read returns early.

    It must still publish. Otherwise a config that is later deleted or made
    unreadable leaves the last-published directory resolving MCP commands
    forever, with nothing on disk that explains why.
    """
    import kiro_crew.env as env_mod

    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    _load_from(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": ["/opt/pixi/bin"]}})
    assert "/opt/pixi/bin" in env_mod.mcp_search_path("").split(os.pathsep)

    # Now point the loader at a home with no config files at all.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(L, "config_path", lambda: empty / "config.json")
    monkeypatch.setattr(L, "config_dir", lambda: empty)
    monkeypatch.setattr(L, "config_local_path", lambda: empty / "config.local.json")
    KiroCrewConfig.load()
    assert "/opt/pixi/bin" not in env_mod.mcp_search_path("").split(os.pathsep)


def test_section_is_in_the_schema_registry():
    """The setting must be reachable from the settings UI / config API, not just
    from a hand-edited file -- that is the whole point of making it a setting."""
    from kiro_crew.config import schema

    entry = next(e for e in schema.SCHEMA_REGISTRY if e.path == "mcp.extra_path_dirs")
    assert entry.type == "array"
    assert entry.label


def test_mcp_is_not_captured_as_an_unknown_section(tmp_path, monkeypatch):
    """Being absent from _KNOWN_CONFIG_SECTIONS would round-trip a stale copy
    alongside the parsed one."""
    cfg = _load_from(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": ["/opt/a/bin"]}})
    assert "mcp" not in cfg._extra_sections


def test_distinct_from_mcp_gateway():
    """These settings apply with the broker off, so they must not live on the
    broker's own section."""
    assert not hasattr(KiroCrewConfig().mcp_gateway, "extra_path_dirs")
    assert isinstance(KiroCrewConfig().mcp, McpConfig)

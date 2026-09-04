"""Round-trip preservation of unknown keys INSIDE a modelled config section.

``_extra_sections`` protects a whole top-level section this core does not model.
A key one level down had no equivalent, so a build that stopped modelling
``<section>.<key>`` erased the operator's value on the next ``save()`` of any
kind. See ``KiroCrewConfig._extra_keys`` and
``config/resolution.capture_extra_section_keys``.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point every config path at a temp home and return the config.json path."""
    cfgp = tmp_path / "config.json"
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return cfgp


def _write(cfgp, doc):
    cfgp.write_text(json.dumps(doc), encoding="utf-8")


# ── The defect ─────────────────────────────────────────────────────────────


def test_unknown_section_key_survives_save(cfg_home):
    """The regression: an unmodelled nested key used to vanish on any save()."""
    _write(cfg_home, {"agent": {"provider": "acp", "retired_knob": "keep-me"}})

    cfg = KiroCrewConfig.load()
    assert cfg._extra_keys.get("agent") == {"retired_knob": "keep-me"}
    assert cfg.to_dict()["agent"]["retired_knob"] == "keep-me"

    cfg.save()
    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["agent"]["retired_knob"] == "keep-me"
    # And the modelled sibling is still there, i.e. capture did not replace the
    # section with the raw copy.
    assert after["agent"]["provider"] == "acp"


def test_unknown_key_survives_a_save_that_changes_something_else(cfg_home):
    """The real-world trigger: an unrelated write (log level, workspace, agent)."""
    _write(cfg_home, {"dashboard": {"legacy_toggle": True}})

    cfg = KiroCrewConfig.load()
    cfg.timezone = "UTC"
    cfg.save()

    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["timezone"] == "UTC"
    assert after["dashboard"]["legacy_toggle"] is True


def test_unknown_keys_survive_repeated_round_trips(cfg_home):
    """Preservation must not decay: load/save twice keeps the key."""
    _write(cfg_home, {"memory": {"orphan": [1, 2, 3]}})

    KiroCrewConfig.load().save()
    KiroCrewConfig.load().save()

    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["memory"]["orphan"] == [1, 2, 3]


def test_several_sections_at_once(cfg_home):
    _write(
        cfg_home,
        {
            "agent": {"gone_a": 1},
            "session": {"gone_b": "two"},
            "telemetry": {"gone_c": {"nested": True}},
        },
    )
    cfg = KiroCrewConfig.load()
    assert set(cfg._extra_keys) == {"agent", "session", "telemetry"}
    d = cfg.to_dict()
    assert d["agent"]["gone_a"] == 1
    assert d["session"]["gone_b"] == "two"
    assert d["telemetry"]["gone_c"] == {"nested": True}


# ── What must NOT be captured ──────────────────────────────────────────────


def test_a_fully_modelled_config_captures_nothing(cfg_home):
    """No pollution: a config written by this build has no unknown keys."""
    _write(cfg_home, KiroCrewConfig().to_dict())
    assert KiroCrewConfig.load()._extra_keys == {}


def test_default_emitted_section_keys_are_all_recognized():
    """INVARIANT, drift guard: every key to_dict() emits inside a dataclass-backed
    section must be a field of that dataclass or listed in
    _SECTION_KEYS_EMITTED_ELSEWHERE. A key that is neither gets captured as
    "unknown" on every load -- noise at best, and a resurrected value at worst if
    to_dict() ever stops emitting it."""
    from dataclasses import fields, is_dataclass

    from kiro_crew.config.resolution import _SECTION_KEYS_EMITTED_ELSEWHERE

    cfg = KiroCrewConfig()
    emitted = cfg.to_dict()
    unaccounted: dict[str, set[str]] = {}
    for section, value in emitted.items():
        section_obj = getattr(cfg, section, None)
        if not isinstance(value, dict) or not is_dataclass(section_obj):
            continue
        if isinstance(section_obj, type):
            continue
        recognized = {f.name for f in fields(section_obj)} | _SECTION_KEYS_EMITTED_ELSEWHERE.get(
            section, frozenset()
        )
        extra = {k for k in value if k not in recognized and not k.startswith("_")}
        if extra:
            unaccounted[section] = extra
    assert unaccounted == {}, (
        "to_dict() emits section keys that capture would treat as unknown; add "
        f"them to _SECTION_KEYS_EMITTED_ELSEWHERE: {unaccounted}"
    )


def test_hooks_is_not_captured_because_it_round_trips_raw(cfg_home):
    """hooks is emitted as `self.hooks` verbatim, so nothing inside it can be lost
    and nothing about it needs capturing."""
    _write(cfg_home, {"hooks": {"my-hook": {"command": "true", "odd": 1}}})
    cfg = KiroCrewConfig.load()
    assert "hooks" not in cfg._extra_keys
    assert cfg.to_dict()["hooks"]["my-hook"]["odd"] == 1


def test_unknown_keys_inside_a_named_record_survive_save(cfg_home):
    """agents/workspaces/memory_stores are maps of dataclass records parsed
    field-by-field and re-emitted with asdict, so `agents.<name>.<key>` used to
    vanish exactly like `agent.<key>`. Captured one level deeper."""
    _write(
        cfg_home,
        {
            "agents": {"my-agent": {"model": "auto", "retired_flag": True}},
            "workspaces": {"my-ws": {"dir": "/tmp/ws", "colour": "teal"}},
            "memory_stores": {"my-store": {"description": "d", "shard": 3}},
        },
    )
    cfg = KiroCrewConfig.load()
    assert cfg._extra_keys["agents"] == {"my-agent": {"retired_flag": True}}
    assert cfg._extra_keys["workspaces"] == {"my-ws": {"colour": "teal"}}
    assert cfg._extra_keys["memory_stores"] == {"my-store": {"shard": 3}}

    cfg.save()
    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["agents"]["my-agent"]["retired_flag"] is True
    assert after["agents"]["my-agent"]["model"] == "auto"
    assert after["workspaces"]["my-ws"]["colour"] == "teal"
    assert after["memory_stores"]["my-store"]["shard"] == 3


def test_a_deleted_record_does_not_come_back(cfg_home):
    """Fill-only at record granularity too: deleting the record in memory must
    not resurrect it via its captured unknown keys."""
    _write(cfg_home, {"agents": {"gone": {"model": "auto", "retired_flag": True}}})
    cfg = KiroCrewConfig.load()
    cfg.agents.pop("gone")
    assert "gone" not in cfg.to_dict()["agents"]


def test_record_unknown_keys_are_not_shipped_to_the_browser(cfg_home):
    from kiro_crew.dashboard.handlers.core import _masked_config_dict

    _write(cfg_home, {"agents": {"a": {"model": "auto", "legacy_token": "xoxb-S"}}})
    cfg = KiroCrewConfig.load()
    assert cfg.to_dict()["agents"]["a"]["legacy_token"] == "xoxb-S"
    masked = _masked_config_dict(cfg)
    assert "xoxb-S" not in json.dumps(masked)


def test_private_keys_are_not_captured(cfg_home):
    """to_dict() drops private carriers on purpose; capture must not undo that."""
    _write(cfg_home, {"mcp_gateway": {"_stub_roster": ["ghost"]}})
    cfg = KiroCrewConfig.load()
    assert "mcp_gateway" not in cfg._extra_keys
    assert "_stub_roster" not in cfg.to_dict()["mcp_gateway"]


def test_a_cleared_conditional_key_is_not_resurrected(cfg_home):
    """slack.channels is emitted only when non-empty, so its absence is a
    deliberate deletion -- restoring it from capture would undo the deletion."""
    _write(
        cfg_home,
        {"slack": {"channels": {"C1": {"activation": "always"}}}},
    )
    cfg = KiroCrewConfig.load()
    assert "slack" not in cfg._extra_keys

    cfg.slack_channels = {}
    cfg.save()
    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert not after["slack"].get("channels")


def test_a_rejected_modelled_value_stays_dropped(cfg_home):
    """A key the schema DOES model but validation refused must not be restored:
    re-emitting it would make the bad value permanent and re-warn every load."""
    _write(cfg_home, {"agent": {"approval_mode": "not-a-real-mode"}})
    cfg = KiroCrewConfig.load()
    assert "agent" not in cfg._extra_keys
    assert cfg.to_dict()["agent"]["approval_mode"] != "not-a-real-mode"


def test_a_legacy_alias_is_not_pinned_in_the_file(cfg_home):
    """knowledge.auto_ingest_doc_links is still READ but save() settles on the
    canonical auto_add_documents; capture must not pin the old spelling."""
    _write(cfg_home, {"knowledge": {"auto_ingest_doc_links": True}})
    cfg = KiroCrewConfig.load()
    assert "knowledge" not in cfg._extra_keys

    d = cfg.to_dict()
    assert d["knowledge"]["auto_add_documents"] is True
    assert "auto_ingest_doc_links" not in d["knowledge"]


def test_the_auto_approve_aliases_are_not_pinned(cfg_home):
    """agent.dangerously_skip_permissions is also read as `dangerouslySkipPermissions`
    and the legacy `yolo`, but save() writes only the canonical spelling. Keeping
    the old ones would leave a contradiction in the file: the canonical grant off
    and a stale `yolo: true` beside it."""
    _write(cfg_home, {"agent": {"yolo": True, "dangerouslySkipPermissions": True}})
    cfg = KiroCrewConfig.load()
    assert "agent" not in cfg._extra_keys
    assert cfg.agent.dangerously_skip_permissions is True

    d = cfg.to_dict()
    assert d["agent"]["dangerously_skip_permissions"] is True
    assert "yolo" not in d["agent"]
    assert "dangerouslySkipPermissions" not in d["agent"]


def test_retired_keys_are_purged_not_preserved(cfg_home):
    """The feature behind these keys is gone, so re-persisting them would keep
    offering a setting with nothing behind it (TestSttRemovedFieldsAreInert)."""
    _write(
        cfg_home,
        {
            "stt": {
                "language_code": "fr-FR",
                "whisper_path": "/usr/local/bin/whisper",
                "mlx_model": "mlx-community/whisper-large-v3-turbo",
                "parakeet_model": "mlx-community/parakeet-tdt-0.6b-v3",
                "device": "cpu",
            }
        },
    )
    cfg = KiroCrewConfig.load()
    assert "stt" not in cfg._extra_keys

    written = cfg.to_dict()["stt"]
    for dead in ("whisper_path", "mlx_model", "parakeet_model", "device"):
        assert dead not in written
    # The live setting sharing the section is untouched.
    assert written["language_code"] == "fr-FR"


def test_an_unknown_nested_key_is_not_shipped_to_the_browser(cfg_home):
    """The browser-facing config response drops captured keys.

    ``_masked_config_dict`` masks by SCHEMA path, so an unknown key is invisible
    to the sensitivity walk. A credential a previous build stored under a
    since-renamed key would otherwise ship verbatim to any authenticated reader,
    which is the same reason the response already drops ``_extra_sections``."""
    from kiro_crew.dashboard.handlers.core import _masked_config_dict

    _write(cfg_home, {"slack": {"legacy_bot_token": "xoxb-SECRET"}})
    cfg = KiroCrewConfig.load()
    assert cfg._extra_keys.get("slack") == {"legacy_bot_token": "xoxb-SECRET"}

    # Preserved for save()...
    assert cfg.to_dict()["slack"]["legacy_bot_token"] == "xoxb-SECRET"
    # ...and absent from what the dashboard is handed.
    masked = _masked_config_dict(cfg)
    assert "legacy_bot_token" not in masked["slack"]
    assert "xoxb-SECRET" not in json.dumps(masked)


# ── Robustness ─────────────────────────────────────────────────────────────


def test_a_non_dict_section_does_not_crash_capture(cfg_home):
    _write(cfg_home, {"agent": "not-an-object", "session": [1, 2]})
    cfg = KiroCrewConfig.load()
    assert cfg._extra_keys == {}
    cfg.save()  # must not raise


def test_capture_never_overwrites_a_live_value(cfg_home):
    """Capture only FILLS a gap. A modelled value set in memory must win."""
    _write(cfg_home, {"agent": {"reasoning_effort": "low", "retired_knob": "x"}})
    cfg = KiroCrewConfig.load()
    cfg.agent.reasoning_effort = "high"
    d = cfg.to_dict()
    assert d["agent"]["reasoning_effort"] == "high"
    assert d["agent"]["retired_knob"] == "x"


def test_private_carriers_never_reach_the_file(cfg_home):
    _write(cfg_home, {"agent": {"retired_knob": "x"}})
    KiroCrewConfig.load().save()
    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert "_extra_keys" not in after
    assert "_extra_sections" not in after


def test_an_overlay_only_unknown_key_does_not_leak_into_the_base_file(cfg_home, tmp_path):
    """config.local.json values are subtracted on save; a key that exists ONLY in
    the overlay is not captured at all (capture reads the base view), so nothing
    about it can reach config.json."""
    _write(cfg_home, {"agent": {"provider": "acp"}})
    (tmp_path / "config.local.json").write_text(
        json.dumps({"agent": {"overlay_only_knob": "local"}}), encoding="utf-8"
    )

    cfg = KiroCrewConfig.load()
    assert "agent" not in cfg._extra_keys
    cfg.save()

    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert "overlay_only_knob" not in after["agent"]


def test_an_overlay_shadowed_unknown_key_keeps_its_base_value(cfg_home, tmp_path):
    """The base and the overlay both hold an unknown nested key with DIFFERENT
    values. Capturing the merged value made save() emit the overlay's leaf, which
    the overlay subtraction then removed -- permanently deleting the base file's
    own value, so removing the overlay later revealed nothing. Capturing the base
    view keeps the base value in config.json while the overlay still wins live."""
    _write(cfg_home, {"agent": {"provider": "acp", "retired": "BASE"}})
    (tmp_path / "config.local.json").write_text(
        json.dumps({"agent": {"retired": "OVERLAY"}}), encoding="utf-8"
    )

    cfg = KiroCrewConfig.load()
    assert cfg._extra_keys.get("agent") == {"retired": "BASE"}
    cfg.save()

    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["agent"]["retired"] == "BASE"


def test_the_base_value_survives_a_cache_hit_too(cfg_home, tmp_path):
    """The overlay is out of scope on a validated-data cache hit; the base copy
    rides in the cache sidecar so the second load captures exactly as the first."""
    _write(cfg_home, {"agent": {"provider": "acp", "retired": "BASE"}})
    (tmp_path / "config.local.json").write_text(
        json.dumps({"agent": {"retired": "OVERLAY"}}), encoding="utf-8"
    )

    first = KiroCrewConfig.load()  # disk path, populates the cache
    second = KiroCrewConfig.load()  # cache path
    assert first._extra_keys == second._extra_keys == {"agent": {"retired": "BASE"}}

    second.save()
    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["agent"]["retired"] == "BASE"


def test_cache_data_and_sidecar_are_served_together_or_not_at_all():
    """A clear() between two separate fetches would hand the loader a merged
    document with an EMPTY base shadow; get_with_sidecar makes the pair atomic."""
    from kiro_crew.config.validation import ConfigCache

    cache = ConfigCache()
    fp = ("fp",)
    cache.store({"agent": {"k": "MERGED"}}, fp, {"base_shadow": {"agent": {"k": "BASE"}}})
    got = cache.get_with_sidecar(fp)
    assert got is not None
    data, sidecar = got
    assert data == {"agent": {"k": "MERGED"}}
    assert sidecar == {"base_shadow": {"agent": {"k": "BASE"}}}
    # Deep copies: mutating what was handed out cannot poison the cache.
    data["agent"]["k"] = "X"
    sidecar["base_shadow"]["agent"]["k"] = "Y"
    assert cache.get_with_sidecar(fp) == (
        {"agent": {"k": "MERGED"}},
        {"base_shadow": {"agent": {"k": "BASE"}}},
    )
    cache.clear()
    assert cache.get_with_sidecar(fp) is None
    assert cache.get(fp) is None


def test_an_overlay_shadowed_unknown_section_keeps_its_base_values(cfg_home, tmp_path):
    """The same fix covers the older _extra_sections capture."""
    _write(cfg_home, {"agent": {}, "amazon": {"flags": "-o -s", "keep": 1}})
    (tmp_path / "config.local.json").write_text(
        json.dumps({"amazon": {"flags": "-x"}}), encoding="utf-8"
    )

    cfg = KiroCrewConfig.load()
    assert cfg._extra_sections.get("amazon") == {"flags": "-o -s", "keep": 1}
    cfg.save()

    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["amazon"] == {"flags": "-o -s", "keep": 1}

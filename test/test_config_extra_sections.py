"""Round-trip preservation of unknown top-level config.json sections.

An edition-contributed top-level section (written by a companion) must survive
the ``load()`` -> ``to_dict()`` -> ``save()`` round-trip instead of being
silently dropped. See ``KiroCrewConfig._extra_sections`` /
``_KNOWN_CONFIG_SECTIONS`` in ``config/loader.py``.
"""

from __future__ import annotations

import json

from kiro_crew.config import loader as L
from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS, KiroCrewConfig


def test_known_sections_equals_emitted_sections():
    """INVARIANT: _KNOWN_CONFIG_SECTIONS must equal the keys to_dict() emits.

    A section in _KNOWN but NOT emitted would be excluded from _extra_sections
    capture yet dropped by to_dict() (lost on save()). A section emitted but NOT
    in _KNOWN would be captured as "unknown" and could round-trip a stale copy.
    """
    emitted = set(KiroCrewConfig().to_dict().keys())
    # to_dict() also stamps slack sub-keys / meta at save() time; compare only
    # the top-level section names it writes from to_dict() itself.
    assert emitted == set(_KNOWN_CONFIG_SECTIONS), (
        "drift between to_dict() output and _KNOWN_CONFIG_SECTIONS: "
        f"emitted-only={emitted - set(_KNOWN_CONFIG_SECTIONS)}, "
        f"known-only={set(_KNOWN_CONFIG_SECTIONS) - emitted}"
    )


def test_unknown_section_round_trips(tmp_path, monkeypatch):
    cfgp = tmp_path / "config.json"
    cfgp.write_text(
        json.dumps({"agent": {"provider": "acp"}, "amazon": {"midway_flags": "-o -s", "n": [1, 2]}})
    )
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

    cfg = KiroCrewConfig.load()
    assert cfg._extra_sections.get("amazon") == {"midway_flags": "-o -s", "n": [1, 2]}
    assert cfg.to_dict().get("amazon") == {"midway_flags": "-o -s", "n": [1, 2]}


def test_extra_sections_never_clobbers_a_known_section():
    """A stale/hostile capture of a known key must not overwrite the real one."""
    c = KiroCrewConfig()
    c._extra_sections = {"agent": {"MALICIOUS": True}, "amazon": {"ok": 1}}
    d = c.to_dict()
    assert "MALICIOUS" not in d["agent"]
    assert d["amazon"] == {"ok": 1}


def test_extra_sections_excluded_from_json_schema():
    from kiro_crew.config import schema

    assert not any("_extra_sections" in e.path for e in schema.SCHEMA_REGISTRY)


def test_api_config_response_omits_extra_sections():
    """The masked GET /api/config response must NOT expose unknown sections.

    `_masked_config_dict` masks only schema-declared sensitive paths; an edition
    section is absent from the schema, so returning it verbatim would leak any
    credential it holds. The masked view must drop `_extra_sections` entirely,
    while `to_dict()` (the save() path) still carries them.
    """
    from kiro_crew.dashboard.handlers.core import _masked_config_dict

    cfg = KiroCrewConfig()
    cfg._extra_sections = {"amazon": {"api_token": "SECRET-should-not-leak", "flag": True}}

    # save()/round-trip path keeps it
    assert cfg.to_dict().get("amazon", {}).get("api_token") == "SECRET-should-not-leak"

    # browser-facing API view drops the whole unknown section
    masked = _masked_config_dict(cfg)
    assert "amazon" not in masked
    # confirm the secret string appears nowhere in the serialized response
    assert "SECRET-should-not-leak" not in json.dumps(masked)

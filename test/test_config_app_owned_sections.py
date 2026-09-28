"""Top-level config.json sections a builtin app owns (``validation._APP_OWNED_TOP_KEYS``).

``dev_fleet.repo_path`` is read straight from ``config.json`` by the Dev Fleet app
(``apps/builtins/dev_fleet/repository.py``), and the dashboard's "no checkout
found" banner tells the operator to write exactly that key. The core has no
modelled field for it, so before this set existed every launch logged
``Config: unrecognized top-level keys: dev_fleet`` -- scolding the operator for
following the product's own instructions.

Three properties hold the fix together:

* validation does not report an app-owned section as unrecognized, and still
  reports a genuinely unknown key beside it;
* the section round-trips through ``load()`` -> ``to_dict()`` like any other
  unmodelled section (it is NOT a reserved key, which save() drops);
* every member of the set names a key some app actually reads -- pinned here for
  ``dev_fleet`` against the reader itself, so the set cannot drift into a list of
  silenced typos.

The set is private to ``validation.py``: the unrecognized-key warning is its only
consumer, so it is an exclusion at that site rather than an exported key class.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config import validation
from kiro_crew.config.loader import _KNOWN_CONFIG_SECTIONS, CONFIG_RESERVED_TOP_KEYS, KiroCrewConfig

_UNRECOGNIZED = "unrecognized top-level keys"


def _unrecognized_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if _UNRECOGNIZED in r.getMessage()]


class TestAppOwnedSectionsAreNotUnrecognized:
    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_dev_fleet_section_is_not_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        data = {"agent": {"provider": "acp"}, "dev_fleet": {"repo_path": "/opt/kc"}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        assert _unrecognized_warnings(caplog) == []

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_genuinely_unknown_key_still_warns_beside_an_app_owned_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Excluding the app-owned set must not widen into silencing real typos.
        data = {"dev_fleet": {"repo_path": "/opt/kc"}, "totally_unknown_key": 1}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data(data)
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "totally_unknown_key" in warnings[0]
        assert "dev_fleet" not in warnings[0]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    @pytest.mark.parametrize("malformed", ["/opt/kc", ["/opt/kc"], 7, None])
    def test_a_non_object_dev_fleet_value_still_warns(
        self, malformed: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exclusion covers the shape the app reads, not the key name.

        ``_load_dev_fleet_cfg`` keeps only a dict and silently falls back to
        discovery on anything else, so a scalar written by hand must keep the one
        warning that says it is being ignored.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            validation.validate_config_data({"agent": {"provider": "acp"}, "dev_fleet": malformed})
        warnings = _unrecognized_warnings(caplog)
        assert warnings and "dev_fleet" in warnings[0]

    @pytest.mark.skipif(not validation._HAS_JSONSCHEMA, reason="jsonschema not installed")
    def test_full_load_of_a_dev_fleet_config_logs_no_unrecognized_warning(
        self, tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The operator-visible path: load() of a real file, not the validator alone."""
        cfgp = tmp_path / "config.json"
        cfgp.write_text(
            json.dumps({"agent": {"provider": "acp"}, "dev_fleet": {"repo_path": "/opt/kc"}})
        )
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            KiroCrewConfig.load()
        assert _unrecognized_warnings(caplog) == []


class TestAppOwnedSectionsRoundTrip:
    def test_dev_fleet_section_survives_load_and_to_dict(self, tmp_path, monkeypatch) -> None:
        """Unlike a RESERVED key, an app-owned section must not be dropped on save."""
        cfgp = tmp_path / "config.json"
        cfgp.write_text(
            json.dumps({"agent": {"provider": "acp"}, "dev_fleet": {"repo_path": "/opt/kc"}})
        )
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")

        cfg = KiroCrewConfig.load()
        assert cfg._extra_sections.get("dev_fleet") == {"repo_path": "/opt/kc"}
        assert cfg.to_dict().get("dev_fleet") == {"repo_path": "/opt/kc"}

    def test_set_is_disjoint_from_known_and_reserved(self) -> None:
        """A key in two sets would be classified two ways by the same loader."""
        assert not (validation._APP_OWNED_TOP_KEYS & set(_KNOWN_CONFIG_SECTIONS))
        assert not (validation._APP_OWNED_TOP_KEYS & CONFIG_RESERVED_TOP_KEYS)


class TestEveryAppOwnedKeyHasAReader:
    """The set is a claim that an app reads the key; pin the claim to the reader."""

    def test_members_are_exactly_the_documented_readers(self) -> None:
        # Extend this tuple together with validation._APP_OWNED_TOP_KEYS and add a
        # reader test below; a member with no reader test is a silenced typo.
        assert validation._APP_OWNED_TOP_KEYS == frozenset({"dev_fleet"})

    def test_dev_fleet_reads_repo_path_from_the_config_file(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod

        (tmp_path / "config.json").write_text(json.dumps({"dev_fleet": {"repo_path": "/opt/kc"}}))
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_DEVFLEET_REPO", raising=False)

        assert repository_mod._configured_main_repo() == "/opt/kc"

"""A ``config.json`` saved with a UTF-8 byte-order mark is still the user's config.

Editors on Windows (Notepad, PowerShell ``Out-File``, VS Code "UTF-8 with BOM")
write ``EF BB BF`` ahead of the JSON, and ``json.loads`` refuses a leading
U+FEFF. Before this, both config readers treated such a file as unparseable: the
loader marked it degraded and ran the gateway on DEFAULTS (every onboarding flag
false, so first run reopened on every load), and every locked read-modify-write
failed closed with ``ConfigReadError``, so no setting could be saved either.

The contract pinned here: the BOM is accepted on read, the real values load,
writes come back as plain UTF-8 without it, and nothing the user set is lost on
the way. A file that is actually malformed still fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_local_path,
    config_path,
    read_config_for_update,
    update_config_locked,
)

_BOM = b"\xef\xbb\xbf"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert config_path().parent == tmp_path
    return tmp_path


def _user_document() -> dict:
    # A key the dataclass does not model is the one a whole-document rewrite
    # from defaults would drop, so it is the sharpest probe for "nothing lost".
    return {
        "dashboard": {"import_onboarded": True, "onboarded": True, "theme_mode": "light"},
        "x_user_note": "kept by hand",
    }


def _write_with_bom(path: Path, document: dict) -> None:
    path.write_bytes(_BOM + json.dumps(document, indent=2).encode("utf-8"))


def test_load_reads_the_real_values_of_a_bom_saved_config(home: Path) -> None:
    _write_with_bom(config_path(), _user_document())

    cfg = KiroCrewConfig.load()

    assert cfg.dashboard.import_onboarded is True
    assert cfg.dashboard.onboarded is True
    assert cfg.dashboard.theme_mode == "light"
    assert not cfg.degraded_sections


def test_locked_update_keeps_every_user_key_and_drops_the_bom(home: Path) -> None:
    _write_with_bom(config_path(), _user_document())

    def _mutate(doc: dict) -> dict:
        doc["dashboard"]["import_onboarded"] = False
        return doc

    update_config_locked(mutate=_mutate)

    raw = config_path().read_bytes()
    assert not raw.startswith(_BOM)
    written = json.loads(raw.decode("utf-8"))
    assert written["dashboard"]["import_onboarded"] is False
    assert written["dashboard"]["onboarded"] is True
    assert written["dashboard"]["theme_mode"] == "light"
    assert written["x_user_note"] == "kept by hand"


def test_read_for_update_accepts_the_bom(home: Path) -> None:
    _write_with_bom(config_path(), _user_document())

    assert read_config_for_update() == _user_document()


def test_a_bom_saved_overlay_still_overlays(home: Path) -> None:
    config_path().write_text(json.dumps(_user_document()), encoding="utf-8")
    _write_with_bom(config_local_path(), {"dashboard": {"theme_mode": "dark"}})

    cfg = KiroCrewConfig.load()

    assert cfg.dashboard.theme_mode == "dark"
    assert cfg.dashboard.import_onboarded is True


def test_a_malformed_config_still_fails_closed(home: Path) -> None:
    # The BOM is the only thing tolerated: a truncated document is still
    # unreadable, and the write must refuse rather than replace it.
    original = _BOM + b'{"dashboard": {"import_onboarded": tr'
    config_path().write_bytes(original)

    with pytest.raises(ConfigReadError):
        update_config_locked(mutate=lambda doc: doc)

    assert config_path().read_bytes() == original


def test_hot_reload_does_not_call_a_bom_saved_config_torn(home: Path) -> None:
    # The live watcher keeps the previous snapshot while a file is "torn", so a
    # BOM read as torn would silently discard every hot edit.
    from kiro_crew.config.live import ConfigWatch

    _write_with_bom(config_path(), _user_document())
    _write_with_bom(config_local_path(), {"dashboard": {"theme_mode": "dark"}})

    assert ConfigWatch._document_is_torn() is False


def test_a_bom_saved_overlay_still_owns_its_trust_grant(home: Path) -> None:
    # The loader applies a BOM'd overlay, so the overlay-ownership gates must see
    # it too: otherwise a revoke edits only config.json, answers 200, and the
    # overlay re-grants the app on the next load.
    from kiro_crew.apps.manager import trust_grant_removal_blocked
    from kiro_crew.dashboard.handlers.security import _overlay_owned_trust_settings

    config_path().write_text(json.dumps({"agent": {"apps_trusted": ["demo"]}}), encoding="utf-8")
    _write_with_bom(config_local_path(), {"agent": {"apps_trusted": ["demo"]}})

    assert "demo" in KiroCrewConfig.load().agent.apps_trusted
    assert _overlay_owned_trust_settings() == ["apps_trusted"]
    assert trust_grant_removal_blocked("demo") is not None


def test_raw_config_reads_a_bom_saved_non_object_as_empty(home: Path) -> None:
    config_path().write_bytes(_BOM + b"[]")

    assert loader._raw_config() == {}

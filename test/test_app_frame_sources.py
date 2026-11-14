"""``ui.frameSources``: exact https origins an app's pages may frame.

The validator refuses anything but ``https://<hostname>``, and the dashboard
``frame-src`` carries a declared origin only while its app is installed, enabled and
trusted (``apps/frame_sources.py``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps import frame_sources
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    app_dir,
    disable_app,
    install_app,
    uninstall_app,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import UIConfig
from kiro_crew.dashboard.server import _apply_security_headers


def _errors(frame_sources_value: Any) -> list[str]:
    return UIConfig.from_dict({"frameSources": frame_sources_value}).frame_source_errors()


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com/path",
        "https://example.com/",
        "https://example.com:8443",
        "https://example.com?q=1",
        "http://example.com",
        "https://*.example.com",
        "https://.example.com",
        "https://1.2.3.4",
        "https://localhost",
        "https://app.localhost",
        "https://co.uk",
        "https://github.io",
        "https://user@example.com",
        "https://Example.com",
        "https://example.com; script-src *",
        "example.com",
        "",
    ],
)
def test_validator_refuses_anything_but_an_exact_https_origin(origin: str) -> None:
    assert _errors([origin]), origin


def test_validator_accepts_exact_origins_and_round_trips() -> None:
    ui = UIConfig.from_dict({"frameSources": ["https://www.example.com", "https://example.org"]})
    assert ui.frame_source_errors() == []
    assert ui.to_dict()["frameSources"] == ["https://www.example.com", "https://example.org"]


def test_absent_field_is_valid_and_not_serialized() -> None:
    ui = UIConfig.from_dict({})
    assert ui.frame_source_errors() == []
    assert "frameSources" not in ui.to_dict()


@pytest.mark.parametrize("value", [None, "https://example.com", {"a": 1}, ["https://a.com", 3]])
def test_validator_refuses_a_non_list_or_non_string_entry(value: Any) -> None:
    assert _errors(value) == ["ui.frameSources must be an array of https origins"]


def test_validator_refuses_more_than_eight_and_duplicates() -> None:
    nine = [f"https://s{i}.example.com" for i in range(9)]
    assert any("exceeds the limit of 8" in e for e in _errors(nine))
    assert _errors(nine[:8]) == []
    assert _errors(["https://a.example.com", "https://a.example.com"]) == [
        "ui.frameSources repeats an entry"
    ]


def test_manifest_validate_reports_frame_source_errors() -> None:
    manifest = AppManifest.from_dict(
        {
            "name": "frame-app",
            "version": "1.0.0",
            "displayName": "Frame App",
            "description": "d",
            "ui": {"frameSources": ["http://example.com"]},
        }
    )
    assert any(e.startswith("ui.frameSources entry") for e in manifest.validate())


# --- the published frame-src ---------------------------------------------------


@pytest.fixture
def trusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[set[str]]:
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Trust is the execution gate's own decision, covered by test_app_execution;
    # here it is a set the test controls.
    names: set[str] = set()
    monkeypatch.setattr(
        frame_sources,
        "third_party_ceiling_closed",
        lambda name: None if name in names else "untrusted",
    )
    yield names
    names.clear()
    frame_sources.refresh_frame_sources()


def _install(tmp_path: Path, name: str, origins: list[str], *, enabled: bool = True) -> None:
    source = tmp_path / "source" / name
    source.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": name,
        "description": "frames a site",
        "ui": {"frameSources": origins},
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert install_app(source).ok
    meta = _read_installed(name)
    assert meta is not None
    meta.enabled = enabled
    _write_installed(name, meta)


def _csp() -> str:
    app = web.Application()
    app["state"] = SimpleNamespace(instances_manager=None)
    resp = web.Response(text="ok")
    _apply_security_headers(resp, app)
    return resp.headers["Content-Security-Policy"]


def _frame_src() -> list[str]:
    """The ``frame-src`` source list, one entry per source."""
    directive = next(d for d in _csp().split("; ") if d.startswith("frame-src "))
    return directive.split()[1:]


def test_enabled_trusted_app_origin_reaches_frame_src_and_disable_drops_it(
    trusted: set[str], tmp_path: Path
) -> None:
    trusted.add("player")
    _install(tmp_path, "player", ["https://video.example.com"])
    frame_sources.refresh_frame_sources()
    assert _frame_src().count("https://video.example.com") == 1

    assert disable_app("player").ok
    assert _frame_src().count("https://video.example.com") == 0


def test_untrusted_or_disabled_app_adds_nothing(trusted: set[str], tmp_path: Path) -> None:
    _install(tmp_path, "stranger", ["https://untrusted.example.com"])
    trusted.add("sleeper")
    _install(tmp_path, "sleeper", ["https://disabled.example.com"], enabled=False)
    frame_sources.refresh_frame_sources()
    src = _frame_src()
    assert src.count("https://untrusted.example.com") == 0
    assert src.count("https://disabled.example.com") == 0


def test_shared_origin_stays_while_one_declaring_app_is_enabled(
    trusted: set[str], tmp_path: Path
) -> None:
    trusted.update({"one", "two"})
    _install(tmp_path, "one", ["https://shared.example.com"])
    _install(tmp_path, "two", ["https://shared.example.com"])
    frame_sources.refresh_frame_sources()
    assert _frame_src().count("https://shared.example.com") == 1

    assert disable_app("one").ok
    assert _frame_src().count("https://shared.example.com") == 1

    assert uninstall_app("two").ok
    assert _frame_src().count("https://shared.example.com") == 0


def test_hand_edited_invalid_manifest_adds_nothing(trusted: set[str], tmp_path: Path) -> None:
    trusted.add("edited")
    _install(tmp_path, "edited", ["https://ok.example.com"])
    manifest_path = app_dir("edited") / APP_MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["ui"]["frameSources"] = ["https://ok.example.com", "https://x.com; script-src *"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    frame_sources.refresh_frame_sources()
    assert _frame_src().count("https://ok.example.com") == 0
    assert "script-src *" not in _csp()


def test_update_of_an_enabled_app_republishes_its_origins(
    trusted: set[str], tmp_path: Path
) -> None:
    trusted.add("player")
    _install(tmp_path, "player", ["https://old.example.com"])
    frame_sources.refresh_frame_sources()
    assert _frame_src().count("https://old.example.com") == 1

    source = tmp_path / "source-v2" / "player"
    source.mkdir(parents=True)
    manifest = {
        "name": "player",
        "version": "1.1.0",
        "displayName": "player",
        "description": "frames a site",
        "ui": {"frameSources": ["https://new.example.com"]},
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert update_app(source).ok
    src = _frame_src()
    assert src.count("https://new.example.com") == 1
    assert src.count("https://old.example.com") == 0

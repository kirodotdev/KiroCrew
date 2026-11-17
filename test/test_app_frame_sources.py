"""``ui.frameSources``: exact https origins an app's pages may frame.

The validator refuses anything but ``https://<hostname>``, and the dashboard
``frame-src`` carries a declared origin only while its app is installed, enabled and
trusted, and only once the owner approved it (``apps/frame_sources.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps import frame_sources
from kiro_crew.apps import hook_reconcile as hr
from kiro_crew.apps import protected_app_state
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    app_dir,
    disable_app,
    enable_app,
    get_app,
    install_app,
    uninstall_app,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import UIConfig
from kiro_crew.apps.point_grants import (
    explicit_only_grant_families,
    grant_source,
    point_grant_families,
)
from kiro_crew.dashboard.server import _apply_security_headers
from kiro_crew.testing.wait import DEFAULT_INTERVAL_SECS, async_wait_until


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
    # enable_app's own execution gate, so the owner's approval path can run.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
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


def _write_v2(tmp_path: Path, name: str, origins: list[str]) -> Path:
    source = tmp_path / "source-v2" / name
    source.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.1.0",
        "displayName": name,
        "description": "frames a site",
        "ui": {"frameSources": origins},
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return source


def test_frame_sources_is_an_explicit_only_staged_grant_family() -> None:
    assert frame_sources.GRANT_FAMILY in point_grant_families()
    assert frame_sources.GRANT_FAMILY in explicit_only_grant_families()
    manifest = {"ui": {"frameSources": ["https://a.example.com", 3, "https://a.example.com"]}}
    assert grant_source(manifest)[frame_sources.GRANT_FAMILY] == ["https://a.example.com"]


def test_install_approves_what_it_declares(trusted: set[str], tmp_path: Path) -> None:
    trusted.add("player")
    _install(tmp_path, "player", ["https://video.example.com"])
    assert get_app("player")["frameSourcesGrant"] == {
        "approved": ["https://video.example.com"],
        "staged": [],
    }


def test_update_that_adds_an_origin_holds_it_until_the_owner_approves(
    trusted: set[str], tmp_path: Path
) -> None:
    trusted.add("player")
    _install(tmp_path, "player", ["https://old.example.com"])
    frame_sources.refresh_frame_sources()
    assert _frame_src().count("https://old.example.com") == 1

    source = _write_v2(tmp_path, "player", ["https://old.example.com", "https://new.example.com"])
    assert update_app(source).ok
    src = _frame_src()
    assert src.count("https://old.example.com") == 1
    assert src.count("https://new.example.com") == 0, "an added origin waits for the owner"
    assert get_app("player")["frameSourcesGrant"] == {
        "approved": ["https://old.example.com"],
        "staged": ["https://new.example.com"],
    }

    approved = enable_app("player", grants_consent={"frameSources": ["https://new.example.com"]})
    assert approved.ok, approved.error
    assert _frame_src().count("https://new.example.com") == 1


def test_update_that_drops_an_origin_stops_publishing_it(trusted: set[str], tmp_path: Path) -> None:
    trusted.add("player")
    _install(tmp_path, "player", ["https://old.example.com"])
    frame_sources.refresh_frame_sources()
    assert update_app(_write_v2(tmp_path, "player", [])).ok
    assert _frame_src().count("https://old.example.com") == 0


def test_live_edit_that_adds_an_origin_is_not_published(trusted: set[str], tmp_path: Path) -> None:
    trusted.add("edited")
    _install(tmp_path, "edited", ["https://ok.example.com"])
    manifest_path = app_dir("edited") / APP_MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["ui"]["frameSources"] = ["https://ok.example.com", "https://lookalike.example.net"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    frame_sources.refresh_frame_sources()
    src = _frame_src()
    assert src.count("https://ok.example.com") == 1
    assert src.count("https://lookalike.example.net") == 0
    assert get_app("edited")["frameSourcesGrant"]["staged"] == ["https://lookalike.example.net"]


def test_a_set_stored_before_the_key_existed_approves_no_origin(
    trusted: set[str], tmp_path: Path
) -> None:
    trusted.add("player")
    _install(tmp_path, "player", ["https://video.example.com"])
    stored = json.loads(protected_app_state.read_grants_text("player") or "{}")
    stored.pop(frame_sources.GRANT_FAMILY)
    protected_app_state.write_grants_text("player", json.dumps(stored))
    frame_sources.refresh_frame_sources()
    assert _frame_src().count("https://video.example.com") == 0


def test_an_entry_that_can_never_publish_is_not_offered(trusted: set[str], tmp_path: Path) -> None:
    trusted.add("edited")
    _install(tmp_path, "edited", ["https://ok.example.com"])
    manifest_path = app_dir("edited") / APP_MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for edited in (
        ["https://ok.example.com", "https://x.example.com/path"],
        ["https://ok.example.com", "https://new.example.com", "https://new.example.com"],
        [f"https://s{i}.example.com" for i in range(9)],
    ):
        data["ui"]["frameSources"] = edited
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        assert get_app("edited")["frameSourcesGrant"]["staged"] == [], edited


@pytest.mark.asyncio
async def test_reconciler_tick_publishes_and_drops_a_trust_change(
    trusted: set[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted.add("player")
    _install(tmp_path, "player", ["https://video.example.com"])
    # Nothing published yet: only the loop's own tick may put the origin there.
    monkeypatch.setattr(frame_sources, "_published", "")
    monkeypatch.setattr(hr, "POLL_INTERVAL_SECS", DEFAULT_INTERVAL_SECS)
    monkeypatch.setattr(hr, "list_apps", lambda: [])

    async def no_pass(installed: Any) -> None:
        return None

    monkeypatch.setattr(hr, "reconcile_once", no_pass)

    def published() -> bool:
        return _frame_src().count("https://video.example.com") > 0

    task = asyncio.ensure_future(hr._reconcile_loop())
    try:
        await async_wait_until(published)
        # A trust grant withdrawn in config.json reaches frame-src only on a tick.
        trusted.discard("player")
        await async_wait_until(lambda: not published())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

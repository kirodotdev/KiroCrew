"""``permissions.browser`` (``rfc-app-sdk-browser.md`` step 1a): the grant family, the preview, ``ctx.browser``.

Pinned here: an install holds no verb until the owner approves it on the app's page;
an update that adds ``act`` holds ``act`` back and keeps the verbs already approved;
with the ``app_browser_sdk`` preview off ``ctx.browser`` is absent and an approved verb
does nothing; and an app that does not declare the field gets no ``ctx.browser``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew.apps import browser_sdk, execution
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    disable_app,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest
from kiro_crew.apps.permissions import format_permissions_summary
from kiro_crew.apps.point_grants import explicit_only_grant_families, point_grant_families


def _config(home: Path, *, preview: bool) -> None:
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {"apps_allow_third_party": True},
                "dashboard": {"feature_previews": {"app_browser_sdk": preview}},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _config(home, preview=True)
    monkeypatch.setattr(execution, "third_party_ceiling_closed", lambda name: None)
    yield home


def _manifest(verbs: object, version: str = "1.0.0") -> dict:
    return {
        "name": "pager",
        "version": version,
        "displayName": "Pager",
        "description": "looks at its own tabs",
        "permissions": {"browser": verbs},
    }


def _source(tmp_path: Path, verbs: list[str], version: str = "1.0.0") -> Path:
    source = tmp_path / f"source-{version}" / "pager"
    source.mkdir(parents=True)
    (source / APP_MANIFEST_FILENAME).write_text(
        json.dumps(_manifest(verbs, version)), encoding="utf-8"
    )
    return source


def _install(tmp_path: Path, verbs: list[str]) -> None:
    assert install_app(_source(tmp_path, verbs)).ok
    meta = _read_installed("pager")
    assert meta is not None
    meta.enabled = True
    _write_installed("pager", meta)


def _approve(verbs: list[str]) -> None:
    result = enable_app("pager", grants_consent={"browser": verbs})
    assert result.ok, result.error


def test_browser_is_an_explicit_only_staged_family() -> None:
    assert browser_sdk.GRANT_FAMILY in point_grant_families()
    assert browser_sdk.GRANT_FAMILY in explicit_only_grant_families()


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        (["act", "view", "read"], ["view", "read", "act"]),
        (["view", "inject", "evaluate"], ["view"]),
        ("view", []),
        ({"view": True}, []),
    ],
)
def test_only_known_verbs_are_declared(raw: object, kept: list[str]) -> None:
    manifest = AppManifest.from_dict(_manifest(raw))
    assert manifest.permissions.browser == kept
    assert manifest.to_dict().get("permissions", {}).get("browser", []) == kept


def test_install_holds_no_verb_until_the_owner_approves(home: Path, tmp_path: Path) -> None:
    _install(tmp_path, ["view", "read"])
    row = get_app("pager")
    assert row is not None
    assert row["browserGrant"] == {"approved": [], "staged": ["view", "read"], "preview": True}
    assert browser_sdk.app_browser_verbs("pager") == ()

    _approve(["view"])
    assert browser_sdk.app_browser_verbs("pager") == ("view",)
    assert get_app("pager")["browserGrant"]["staged"] == ["read"]


def test_an_update_that_adds_act_holds_act_back(home: Path, tmp_path: Path) -> None:
    _install(tmp_path, ["view", "read"])
    _approve(["view", "read"])

    assert update_app(_source(tmp_path, ["view", "read", "act"], "1.1.0")).ok
    assert browser_sdk.app_browser_verbs("pager") == ("view", "read")
    assert get_app("pager")["browserGrant"]["staged"] == ["act"]

    _approve(["act"])
    assert browser_sdk.app_browser_verbs("pager") == ("view", "read", "act")


def test_preview_off_means_no_ctx_browser_and_no_verb(home: Path, tmp_path: Path) -> None:
    _install(tmp_path, ["view"])
    _approve(["view"])
    ctx = build_app_context("pager", tmp_path / "data", permissions={"browser": ["view"]})
    assert isinstance(ctx.browser, browser_sdk.BrowserSDK)
    sdk = ctx.browser
    assert sdk.verbs() == ("view",)

    _config(home, preview=False)
    assert ctx.browser is None
    # An SDK handle kept from while the preview was on holds no verb once it is off.
    assert sdk.verbs() == ()
    assert get_app("pager")["browserGrant"]["preview"] is False


def test_a_preview_turned_on_after_start_needs_no_restart(home: Path, tmp_path: Path) -> None:
    _install(tmp_path, ["read"])
    _approve(["read"])
    _config(home, preview=False)
    ctx = build_app_context("pager", tmp_path / "data", permissions={"browser": ["read"]})
    assert ctx.browser is None

    _config(home, preview=True)
    assert ctx.browser is not None
    assert ctx.browser.verbs() == ("read",)


def test_an_app_that_is_off_holds_no_verb_until_it_is_on_again(home: Path, tmp_path: Path) -> None:
    _install(tmp_path, ["act"])
    _approve(["act"])
    assert browser_sdk.app_browser_verbs("pager") == ("act",)

    assert disable_app("pager").ok
    assert browser_sdk.app_browser_verbs("pager") == ()

    assert enable_app("pager").ok
    assert browser_sdk.app_browser_verbs("pager") == ("act",)


def test_an_app_that_does_not_declare_browser_gets_no_ctx_browser(
    home: Path, tmp_path: Path
) -> None:
    ctx = build_app_context("pager", tmp_path / "data", permissions={"api": []})
    assert ctx.browser is None


def test_the_install_prompts_name_each_verb() -> None:
    manifest = AppManifest.from_dict(_manifest(["view", "act"]))
    summary = format_permissions_summary(manifest)
    assert "see the page you and the Bot are browsing" in summary
    assert "click and type for you" in summary
    assert "approved later on the app's page" in summary
    disclosed = _disclosed_permissions_manifest({"permissions": {"browser": ["act", "x"]}})
    assert disclosed == {"permissions": {"browser": ["act"]}}

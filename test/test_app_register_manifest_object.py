"""Self-registration refuses a manifest that is not a JSON object.

``POST /api/apps/register`` hands ``body["manifest"]`` to
``register_external_app`` unchanged. A non-empty list, string or number is
refused before any write, rather than reaching ``AppManifest.from_dict`` and
raising (a 500 at the route). An empty value reads as no manifest.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps.manager import _read_installed, register_external_app


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Set KIROCREW_HOME to a temp directory that admits a third-party app."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


@pytest.mark.parametrize("manifest", [["x"], "str", 7, True])
def test_a_manifest_that_is_not_an_object_is_refused_before_any_write(app_home, manifest):
    try:
        result = register_external_app("ext-app", "1.0.0", "External App", manifest_data=manifest)
    except (AttributeError, TypeError) as exc:  # a raise is a 500 at the route
        pytest.fail(f"registration raised instead of refusing: {exc!r}")
    assert not result.ok
    assert result.error == "manifest must be a JSON object"
    assert _read_installed("ext-app") is None


@pytest.mark.parametrize("manifest", [None, [], "", 0, {}])
def test_an_empty_manifest_still_registers_as_no_manifest(app_home, manifest):
    result = register_external_app("ext-app", "1.0.0", "External App", manifest_data=manifest)
    assert result.ok, result.error
    assert _read_installed("ext-app") is not None

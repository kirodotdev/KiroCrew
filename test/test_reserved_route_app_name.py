"""Reserved dashboard-route app names are refused with a machine-readable code.

``/apps/library`` is a static dashboard page registered BEFORE the
``/apps/:name`` installed-app catch-all, so an app actually named ``library``
would be unreachable: the page shadows its URL. The backend therefore refuses
the name at every admission door — and, because the frontend must offer "pick
another name" rather than render English prose, the refusal carries the wire
code ``reserved_app_name`` (``AppResult.error_code``, serialized as ``code``;
see ``test_error_code_contract.py``).

Both doors are pinned, following the ``TestUnportableAppName`` precedent in
``test_app_manager.py``: the defect class is not one wrong check but three
doors carrying three different checks.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    install_app,
    register_external_app,
)
from kiro_crew.apps.manifest import RESERVED_APP_NAME_CODE

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name):
    """Create a minimal app source directory whose manifest claims *name*.

    The source DIRECTORY is deliberately not named after the app: the
    destination identity comes from the manifest, and this mirrors the real
    hand-off case (a tree authored elsewhere, installed here).
    """
    src = tmp_path / "source" / f"{name}-src"
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": "Test App",
                "description": "A test app for unit tests",
                "author": "tester",
            }
        )
    )
    return src


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Set KIROCREW_HOME to a temp directory for isolated testing."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # The success-path assertions install a synthetic third-party app.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


# ---------------------------------------------------------------------------
# Reserved-route name refusal
# ---------------------------------------------------------------------------


class TestReservedRouteAppName:
    """``library`` must be refused by every door, and carry the wire code."""

    def test_install_refuses_library_with_wire_code(self, tmp_path, app_home):
        src = _make_app_source(tmp_path, "library")
        result = install_app(src)
        assert not result.ok
        assert "reserved" in result.error, result.error
        # The machine-readable identity the frontend switches on — the prose
        # above is advisory and untranslatable.
        assert result.error_code == RESERVED_APP_NAME_CODE
        assert result.to_dict()["code"] == RESERVED_APP_NAME_CODE
        # Refused before anything lands on disk.
        assert not (app_home / "apps" / "library").exists()

    def test_register_external_refuses_library_with_wire_code(self, app_home):
        result = register_external_app("library", "1.0.0", "Library App")
        assert not result.ok
        assert "reserved" in result.error, result.error
        assert result.error_code == RESERVED_APP_NAME_CODE
        assert result.to_dict()["code"] == RESERVED_APP_NAME_CODE
        assert _read_installed("library") is None
        assert not (app_home / "apps" / "library").exists()

    def test_wire_code_value_is_the_contract(self):
        """The literal string is the API contract (Azure/Stripe precedent:
        codes cannot change once shipped). Pin it so a rename is a conscious
        breaking change, not a refactor side-effect."""
        assert RESERVED_APP_NAME_CODE == "reserved_app_name"

    def test_a_leading_hyphen_name_is_refused(self, tmp_path, app_home):
        """A name starting with '-' must stay inadmissible: the dashboard
        routes static store sub-pages under the ``/apps/-/`` prefix (the
        Updates worklist lives at ``/apps/-/updates``) precisely because no
        legal app name can begin with a hyphen. Loosening ``KEBAB_RE`` to
        admit one would let an app shadow every ``-/`` route without touching
        ``RESERVED_APP_NAMES`` — this pin makes that a conscious decision."""
        result = register_external_app("-updates", "1.0.0", "Dash Prefix")
        assert not result.ok
        assert "kebab-case" in result.error

    def test_a_normal_name_still_passes_both_doors(self, tmp_path, app_home):
        """The reservation is exact-match: ordinary names are unaffected."""
        src = _make_app_source(tmp_path, "librarian-notes")
        result = install_app(src)
        assert result.ok, result.error
        assert result.error_code == ""

        external = register_external_app("shelf-keeper", "1.0.0", "Shelf Keeper")
        assert external.ok, external.error
        assert external.error_code == ""

    def test_non_reserved_failures_carry_no_reserved_code(self, tmp_path, app_home):
        """A refusal for a DIFFERENT reason must not borrow this code — the
        code is the failure's identity, not a generic 'invalid name' bucket."""
        src = _make_app_source(tmp_path, "Not_Kebab")
        result = install_app(src)
        assert not result.ok
        assert result.error_code != RESERVED_APP_NAME_CODE

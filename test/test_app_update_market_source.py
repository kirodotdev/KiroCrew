"""A folder update never rewrites a marketplace app's record.

A marketplace install records ``source = "market:<ref>@<n>"`` and
``sourceCommit = <zip sha256>``; its trust grant binds to ``market:<ref>``.
``update_app`` from a local folder would overwrite both with the folder path
and an empty commit, so the app would silently lose its listing provenance.
Until a marketplace update path exists, the folder update is refused and the
installed tree and record stay as they were.
"""

from __future__ import annotations

from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew.apps.manager import (
    _read_installed,
    get_app,
    install_app,
    register_external_app,
    update_app,
)

NAME = "test-app"
SOURCE = f"market:acme/{NAME}@3"
SHA = "c" * 64
HINT = (
    "This app came from Kiro Space. To change it, make a local copy: it becomes "
    "your own app and no longer gets marketplace updates."
)


def test_folder_update_over_a_market_record_is_refused(app_home, tmp_path):
    installed = install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    )
    assert installed.ok, installed.error

    result = update_app(_make_app_source(tmp_path / "v2", name=NAME, version="1.1.0"))

    assert result.ok is False
    assert result.error_code == "app_update_market_source"
    assert HINT in (result.error or "")
    meta = _read_installed(NAME)
    assert meta is not None
    assert meta.source == SOURCE
    assert meta.sourceCommit == SHA
    info = get_app(NAME)
    assert info is not None and info["version"] == "1.0.0"


def test_self_registration_over_a_market_record_is_refused(app_home, tmp_path):
    assert install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    ).ok

    result = register_external_app(NAME, "9.9.9", "Other", source="/elsewhere")

    assert result.ok is False
    assert result.error_code == "app_update_market_source"
    assert HINT in (result.error or "")
    meta = _read_installed(NAME)
    assert meta is not None
    assert meta.source == SOURCE
    assert meta.sourceCommit == SHA
    assert meta.version == "1.0.0"


def test_folder_update_of_a_local_app_still_works(app_home, tmp_path):
    assert install_app(_make_app_source(tmp_path / "v1", name=NAME)).ok
    result = update_app(_make_app_source(tmp_path / "v2", name=NAME, version="1.1.0"))
    assert result.ok, result.error

"""A folder update never rewrites a marketplace app's record.

A marketplace install records ``source = "market:<ref>@<n>"`` and
``sourceCommit = <zip sha256>``; its trust grant binds to ``market:<ref>``.
``update_app`` from a local folder would overwrite both with the folder path
and an empty commit, so the app would silently lose its listing provenance.
Until a marketplace update path exists, the folder update is refused and the
installed tree and record stay as they were.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew.apps import manager
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
    "This app came from the marketplace, and updating it from a folder would "
    "overwrite it. Uninstall it first, or install the folder under another name."
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


def _refusal_records(monkeypatch):
    recorder = MagicMock()
    monkeypatch.setattr(manager, "sel", lambda: recorder)
    return recorder.log_api_access


def test_refusal_text_offers_no_local_copy(app_home, tmp_path):
    assert install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    ).ok

    result = update_app(_make_app_source(tmp_path / "v2", name=NAME, version="1.1.0"))

    assert "local copy" not in (result.error or "")
    assert "Uninstall it first" in (result.error or "")


def test_both_refusals_write_a_security_event_record(app_home, tmp_path, monkeypatch):
    assert install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    ).ok
    log = _refusal_records(monkeypatch)

    update_app(_make_app_source(tmp_path / "v2", name=NAME, version="1.1.0"))
    register_external_app(NAME, "9.9.9", "Other", source="/elsewhere")

    calls = [c.kwargs for c in log.call_args_list]
    assert [(c["caller"], c["operation"], c["outcome"]) for c in calls] == [
        ("app_update", "provenance", "rejected"),
        ("app_register_external", "provenance", "rejected"),
    ]
    assert all(c["resources"] == f"name={NAME!r}" and c["error"] for c in calls)


def test_install_over_a_market_record_does_not_advise_the_refused_update(app_home, tmp_path):
    assert install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    ).ok

    result = install_app(_make_app_source(tmp_path / "v2", name=NAME))

    assert result.ok is False
    assert "Update it in place" not in (result.error or "")
    assert HINT in (result.error or "")


def test_install_over_a_local_record_still_advises_the_update(app_home, tmp_path):
    assert install_app(_make_app_source(tmp_path / "v1", name=NAME)).ok

    result = install_app(_make_app_source(tmp_path / "v2", name=NAME))

    assert result.ok is False
    assert "Update it in place" in (result.error or "")


@pytest.mark.asyncio
async def test_registry_install_over_a_market_record_is_refused_before_anything_is_replaced(
    app_home, tmp_path
):
    """The registry path (``/api/apps/registry/install[-stream]``) takes the app down
    in ``before_replace``, before ``update_app`` could refuse: it must not get there."""
    from kiro_crew.apps.registry import install_from_registry

    assert install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    ).ok
    taken_down: list[str] = []

    async def _take_down() -> None:
        taken_down.append("take-down")

    result = await install_from_registry(NAME, before_replace=_take_down)

    assert result["ok"] is False
    assert result["code"] == "app_update_market_source"
    assert HINT in result["error"]
    assert taken_down == []
    meta = _read_installed(NAME)
    assert meta is not None and meta.source == SOURCE and meta.sourceCommit == SHA


def test_adoption_over_a_market_record_is_refused_and_changes_nothing(app_home, tmp_path):
    from kiro_crew.apps.manager import adopt_builtin_record

    assert install_app(
        _make_app_source(tmp_path / "v1", name=NAME),
        expected_name=NAME,
        market_source=SOURCE,
        source_commit=SHA,
    ).ok

    result = adopt_builtin_record(
        NAME,
        _make_app_source(tmp_path / "v2", name=NAME, version="1.1.0"),
        repository="https://example.test/acme/test-app",
        commit="a" * 40,
    )

    assert result.ok is False
    assert result.error_code == "not_builtin_owned"
    meta = _read_installed(NAME)
    assert meta is not None and meta.source == SOURCE and meta.sourceCommit == SHA


def test_folder_update_of_a_local_app_still_works(app_home, tmp_path):
    assert install_app(_make_app_source(tmp_path / "v1", name=NAME)).ok
    result = update_app(_make_app_source(tmp_path / "v2", name=NAME, version="1.1.0"))
    assert result.ok, result.error

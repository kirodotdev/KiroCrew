"""A write that fails after the copy leaves no half-installed app.

``install_app`` writes the approved set, the record, the data dir and the app
secret after copying the tree. A failure part-way through must come back as a
failed result, with no record left that reads as installed.
"""

from __future__ import annotations

import errno

import pytest
from app_manager_helpers import _make_app_source, app_home_fixture  # noqa: F401 - fixture

from kiro_crew.apps.manager import app_dir, get_app, install_app
from kiro_crew.apps.manager_records import _approved_grants_snapshot, _write_approved_grants


def test_a_failed_secret_write_leaves_no_installed_record(
    tmp_path, app_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.dashboard.token_auth as token_auth

    real_write = token_auth.write_app_secret

    def _disk_full(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(token_auth, "write_app_secret", _disk_full)
    src = _make_app_source(tmp_path, name="half-app")

    try:
        result = install_app(src)
    except OSError as exc:
        pytest.fail(f"the write failure escaped install_app: {exc}")

    assert result.ok is False
    assert "failed to record the install" in result.error
    assert get_app("half-app") is None
    # The copied tree is kept, not deleted, for the next install to take over.
    assert app_dir("half-app").is_dir()

    monkeypatch.setattr(token_auth, "write_app_secret", real_write)
    assert install_app(src).ok is True
    assert get_app("half-app") is not None


def test_a_failed_write_puts_back_an_approved_set_that_outlived_its_record(
    tmp_path, app_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.dashboard.token_auth as token_auth

    _write_approved_grants("half-app", {"api": ["storage"], "events": []})
    prior = _approved_grants_snapshot("half-app")
    assert prior is not None

    def _disk_full(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(token_auth, "write_app_secret", _disk_full)
    assert install_app(_make_app_source(tmp_path, name="half-app")).ok is False
    assert _approved_grants_snapshot("half-app") == prior


def test_an_installed_app_is_refused_and_keeps_its_record_and_set(
    tmp_path, app_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install_app refuses an installed app before any write, so nothing is undone."""
    import kiro_crew.dashboard.token_auth as token_auth

    src = _make_app_source(tmp_path, name="half-app")
    assert install_app(src).ok is True
    record = get_app("half-app")
    grants = _approved_grants_snapshot("half-app")

    def _disk_full(*_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(token_auth, "write_app_secret", _disk_full)
    again = install_app(src)

    assert again.ok is False
    assert "already installed" in again.error
    assert get_app("half-app") == record
    assert _approved_grants_snapshot("half-app") == grants

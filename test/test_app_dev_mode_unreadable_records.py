"""A dev-mode toggle never saves over a grant record or sentinel it could not read.

The operator grant record maps an app to the ui root the operator approved, and an
out-of-install root is granted only by an explicit confirmation, so a lost entry cannot
be rebuilt from anything on disk. Enabling an app read the record with a reader that
answers "empty" for a record cut off mid-write, with the wrong shape, or unreadable by
this process, then wrote the record back holding only the new grant. Every other app's
grant was gone and the toggle answered success.

The sentinel is derived from every ``installed.json``. The same read there dropped each
other dev app from the watched set until the next restart.

An enable that cannot read the grant record changes nothing and says so. A disable
changes nothing in the record, since there is no grant to find in it. Every toggle
rebuilds the sentinel from the install records, so a sentinel that cannot be read is
never the starting point.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from test_app_dev_mode import _make_app_source, _setup_env

from kiro_crew.apps.dev_mode import _DEV_GRANTS, _DEV_SENTINEL, remove_dev_app, set_dev_mode
from kiro_crew.apps.manager import _read_installed, apps_dir, install_app

_HELD = {"other-app": "/ops/other-app/ui", "third-app": "/ops/third-app/ui"}
_TRUNCATED = b'{"other-app": "/ops/other-app/ui", "third-app": "/ops/thi'


@pytest.fixture(autouse=True)
def _reset_dev_cache():
    import kiro_crew.apps.dev_mode as dev_mode

    dev_mode._set_dev_cache(set())
    yield
    dev_mode._set_dev_cache(set())


def _install(tmp_path, monkeypatch, *names: str) -> None:
    _setup_env(tmp_path, monkeypatch)
    for name in names:
        install_app(str(_make_app_source(tmp_path, name)))


def _grants_file():
    return apps_dir() / _DEV_GRANTS


def _sentinel_file():
    return apps_dir() / _DEV_SENTINEL


def _dev_flag(name: str) -> bool:
    meta = _read_installed(name)
    assert meta is not None
    return bool(meta.dev)


class TestGrantRecordIsKept:
    @pytest.mark.parametrize(
        "body",
        [_TRUNCATED, b'["other-app"]', b'"not a record"'],
        ids=["truncated", "list", "string"],
    )
    def test_an_enable_refuses_a_record_that_does_not_parse(self, tmp_path, monkeypatch, body):
        _install(tmp_path, monkeypatch, "dev-a")
        _grants_file().write_bytes(body)

        result = set_dev_mode("dev-a", True)

        assert result["code"] == "dev_mode_grant_record_unreadable"
        assert _grants_file().read_bytes() == body
        assert _dev_flag("dev-a") is False
        assert not _sentinel_file().exists()

    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="needs a file the current user can write but not read",
    )
    def test_an_enable_refuses_a_record_this_process_cannot_read(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "dev-a")
        _grants_file().write_text(json.dumps(_HELD), encoding="utf-8")
        before = _grants_file().read_bytes()
        _grants_file().chmod(0o200)
        try:
            result = set_dev_mode("dev-a", True)
        finally:
            _grants_file().chmod(0o600)

        assert result["code"] == "dev_mode_grant_record_unreadable"
        assert _grants_file().read_bytes() == before
        assert _dev_flag("dev-a") is False

    def test_a_disable_leaves_a_record_that_does_not_parse_as_it_is(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "dev-a")
        assert set_dev_mode("dev-a", True) == {"name": "dev-a", "dev": True}
        _grants_file().write_bytes(_TRUNCATED)

        result = set_dev_mode("dev-a", False)

        assert result == {"name": "dev-a", "dev": False}
        assert _grants_file().read_bytes() == _TRUNCATED
        assert _dev_flag("dev-a") is False

    def test_an_enable_keeps_the_grants_already_held(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "dev-a")
        _grants_file().write_text(json.dumps(_HELD), encoding="utf-8")

        assert set_dev_mode("dev-a", True) == {"name": "dev-a", "dev": True}

        held = json.loads(_grants_file().read_text(encoding="utf-8"))
        assert {k: held[k] for k in _HELD} == _HELD
        assert "dev-a" in held

    def test_a_missing_record_is_created_by_the_first_enable(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "dev-a")
        _grants_file().unlink(missing_ok=True)

        assert set_dev_mode("dev-a", True) == {"name": "dev-a", "dev": True}

        assert list(json.loads(_grants_file().read_text(encoding="utf-8"))) == ["dev-a"]


class TestSentinelIsRebuiltNotEmptied:
    @pytest.mark.parametrize("body", [b'["dev-a", "de', b'{"dev-a": true}'], ids=["cut", "dict"])
    def test_an_enable_keeps_the_other_dev_apps_when_the_sentinel_is_unreadable(
        self, tmp_path, monkeypatch, body
    ):
        _install(tmp_path, monkeypatch, "dev-a", "dev-b")
        assert set_dev_mode("dev-a", True)["dev"] is True
        _sentinel_file().write_bytes(body)

        assert set_dev_mode("dev-b", True)["dev"] is True

        assert set(json.loads(_sentinel_file().read_text(encoding="utf-8"))) == {"dev-a", "dev-b"}

    def test_a_disable_keeps_the_other_dev_apps_when_the_sentinel_is_unreadable(
        self, tmp_path, monkeypatch
    ):
        _install(tmp_path, monkeypatch, "dev-a", "dev-b")
        for name in ("dev-a", "dev-b"):
            assert set_dev_mode(name, True)["dev"] is True
        _sentinel_file().write_bytes(b'["dev-a", "de')

        assert set_dev_mode("dev-b", False)["dev"] is False

        assert json.loads(_sentinel_file().read_text(encoding="utf-8")) == ["dev-a"]

    def test_a_toggle_writes_the_sentinel_from_the_install_records(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "dev-a")
        _sentinel_file().write_text(json.dumps(["gone-app"]), encoding="utf-8")

        assert set_dev_mode("dev-a", True)["dev"] is True

        assert json.loads(_sentinel_file().read_text(encoding="utf-8")) == ["dev-a"]


class TestRemoveWritesOnlyWhatItFound:
    def test_an_unreadable_grant_record_and_sentinel_are_not_rewritten(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "dev-a")
        _grants_file().write_bytes(_TRUNCATED)
        _sentinel_file().write_bytes(b'["dev-a", "de')

        remove_dev_app("dev-a")

        assert _grants_file().read_bytes() == _TRUNCATED
        assert _sentinel_file().read_bytes() == b'["dev-a", "de'

    def test_a_readable_record_loses_only_the_removed_app(self, tmp_path, monkeypatch):
        _install(tmp_path, monkeypatch, "dev-a")
        _grants_file().write_text(json.dumps({**_HELD, "dev-a": "/x/ui"}), encoding="utf-8")
        _sentinel_file().write_text(json.dumps(["dev-a", "other-app"]), encoding="utf-8")

        remove_dev_app("dev-a")

        assert json.loads(_grants_file().read_text(encoding="utf-8")) == _HELD
        assert json.loads(_sentinel_file().read_text(encoding="utf-8")) == ["other-app"]

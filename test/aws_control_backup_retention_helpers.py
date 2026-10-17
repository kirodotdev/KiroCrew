"""Shared fakes for the cloud backup retention suite.

The fake versioned drive, the isolated backup state, the SEL recorder and the sweep
runner that ``test_aws_control_backup_retention.py`` and its
``test_aws_control_backup_retention_*.py`` parts use.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup

ACCOUNT = "111122223333"


PROFILE = "p"


REGION = "us-east-1"


BUCKET = "kirocrew-drive-0123456789ab"


INSTALL = "a" * 32


def _version(key: str, modified: str, version_id: str = "", **over: Any) -> dict[str, Any]:
    """One ``list_object_versions`` row, in the shape storage returns."""
    row = {
        "key": key,
        "versionId": version_id or f"v-{key}-{modified}",
        "modified": modified,
        "size": 10,
        "latest": True,
        "deleteMarker": False,
    }
    row.update(over)
    return row


def _archives(kind: str, stamps: list[str], install: str = INSTALL) -> list[dict[str, Any]]:
    """One archive per stamp, newest stamp last, under one install's prefix."""
    sub = backup.KIND_SUBPATHS[kind]
    return [
        _version(f"{sub}/{install}/kirocrew-snapshot-{s}.tar.gz", f"2026-01-{s}T00:00:00Z")
        for s in stamps
    ]


def _key_of(row: dict[str, Any]) -> str:
    return str(row["key"])


class _Drive:
    """A fake versioned drive: records what retention asked it to do."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.listed: list[str] = []
        self.deleted: list[tuple[str, str]] = []
        self.list_error: Exception | None = None
        self.delete_error: Exception | None = None

    def list_object_versions(self, profile, region, bucket, section, subpath, *, account):
        assert section == "backup"
        assert account == ACCOUNT
        self.listed.append(subpath)
        if self.list_error is not None:
            raise self.list_error
        # The real call is prefix-scoped; the fake honours that so a test cannot
        # accidentally pass by being handed rows the real listing would not return.
        return [row for row in self.rows if _key_of(row).startswith(f"{subpath}/")]

    def delete_object_versions(self, profile, region, bucket, section, versions, *, account):
        assert section == "backup"
        assert account == ACCOUNT
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.extend(versions)
        return len(versions)

    @property
    def deleted_keys(self) -> list[str]:
        return [key for key, _ in self.deleted]


@pytest.fixture
def drive(monkeypatch):
    """A fake drive wired into ``backup.storage``, with the live gate stubbed out."""
    made = _Drive([])
    monkeypatch.setattr(backup.storage, "list_object_versions", made.list_object_versions)
    monkeypatch.setattr(backup.storage, "delete_object_versions", made.delete_object_versions)
    monkeypatch.setattr(backup, "_authorize_upload", lambda *a, **k: None)
    return made


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Isolated backup state, and a helper to set the retention override."""
    path = tmp_path / "backup.json"
    monkeypatch.setattr(backup, "_state_path", lambda: path)

    def _set(value: Any) -> None:
        # Merged, not overwritten: the sweep also reads this account's `uploads`
        # record, and a setter that replaced the document would erase it.
        _write_account({backup.RETENTION_KEEP_STATE_KEY: value})

    return _set


def _write_account(entry: dict[str, Any]) -> None:
    """Merge ``entry`` into this account's sub-dict of the isolated state file."""
    path = backup._state_path()
    doc: dict[str, Any] = {}
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
    doc.setdefault("accounts", {}).setdefault(ACCOUNT, {}).update(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _own(versions: dict[str, str]) -> None:
    """Record ``versions`` (key -> the VersionId this install wrote) as ours.

    Two maps, because the code reads two: ``uploads`` is key -> body fingerprint,
    and ``upload_versions`` is key -> version id. Retention retires only a version
    whose id is in the second one, so a sweep test that plants archives has to say
    WHICH version is its own -- which is the guard, not a fixture detail.
    """
    _write_account(
        {
            "uploads": {key: f"fp-{key}" for key in versions},
            "upload_versions": dict(versions),
        }
    )


def _prune(
    drive: _Drive,
    kind: str = backup.KIND_SNAPSHOT,
    newest: str = "",
    *,
    owned: set[str] | None = None,
) -> dict[str, Any]:
    """Run the sweep against ``drive``.

    ``owned`` is what gets recorded as this install's own uploads. It defaults to
    every key the fake drive holds, because the ordinary case is a drive holding
    only our archives and most tests here are about AGE, not ownership. A test
    about ownership passes the subset it means.
    """
    newest = newest or _key_of(drive.rows[-1])
    owned_keys = {_key_of(row) for row in drive.rows} if owned is None else owned
    # The FIRST non-marker row for each owned key is the one this install wrote;
    # anything planted after it in `drive.rows` stands for another writer's version.
    recorded: dict[str, str] = {}
    for row in drive.rows:
        key = _key_of(row)
        if key in owned_keys and key not in recorded and not row.get("deleteMarker"):
            recorded[key] = str(row["versionId"])
    _own(recorded)
    return backup._prune_remote_archives(
        ACCOUNT,
        PROFILE,
        REGION,
        BUCKET,
        kind,
        INSTALL,
        newest,
        caller=backup.CALLER_SCHEDULED,
    )


@pytest.fixture
def audit(monkeypatch):
    """Collect the SEL events the sweep files, and let a test make SEL fail."""
    logged: list[dict[str, Any]] = []
    recorder = mock.Mock()
    recorder.log_api_access.side_effect = lambda **kw: logged.append(kw)
    monkeypatch.setattr(backup, "sel", lambda: recorder)
    return logged

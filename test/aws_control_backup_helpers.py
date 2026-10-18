"""Shared constants and a Layer B harness for the ``aws_control`` backup tests.

Each importer imports ``ACCOUNT``, the account id under test. ``_store`` writes a
scope marker covering conversations under its default ``scope``, ``_SCOPED``, and
none under ``scope=None``. ``ARCHIVE_FINGERPRINT`` is the MD5 hex digest of
``ARCHIVE_BYTES``, a fake download body. ``_SessionsArchiveLayerBGateHarness``
isolates state, stubs the conversation store and seeds session halves and grants.

``test_aws_control_backup_layer_b.py`` (which also imports ``_SCOPED``) and
``test_aws_control_backup_layer_b_mid_build.py`` subclass the harness;
``test_aws_control_backup_ownership.py`` and
``test_aws_control_backup_upload_records.py`` import the two archive constants.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup

ACCOUNT = "111122223333"

#: Sentinel for `_store`'s `scope` argument. A sentinel rather than a default string
#: because `None` has to mean "write NO scope marker" -- the legacy grant shape -- and
#: that is a value a caller passes deliberately, not the absence of an argument.
_SCOPED = object()

#: What the fake downloads write, and the fingerprint a matching upload record
#: must carry -- the restore verdict is decided on the bytes that arrive.
ARCHIVE_BYTES = b"archive"
# nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5 -- a fixture fingerprint in the upload record's MD5 shape and not a security digest
ARCHIVE_FINGERPRINT = hashlib.md5(ARCHIVE_BYTES).hexdigest()


class _SessionsArchiveLayerBGateHarness:
    """Non-collected base of the ``TestSessionsArchiveLayerBGate*`` test classes.

    The autouse ``_isolated_state`` fixture keeps the state document under ``tmp_path``
    and stubs out the host's kiro-cli conversation store; ``_synthetic_store`` swaps in
    a synthetic SQLite one. ``_both_halves`` populates the Kiro Crew and kiro-cli
    session halves and ``_store`` writes the permission as raw JSON. A sessions backup
    with the push mocked runs in ``_run_capturing_names``, which returns the record and
    archive member names; ``_skip_without_pinning`` skips where pinning is unavailable.

    Its name does not start with ``Test``, so pytest collects nothing from it; the test
    class in each of ``test_aws_control_backup_layer_b.py`` and
    ``test_aws_control_backup_layer_b_mid_build.py`` subclasses it.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin moved verbatim; no test that inherits it calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        # The terminal conversation export rides the same permission, and these
        # tests assert EXACT archive member lists. Left unstubbed, a host that
        # happens to have a kiro-cli store would add a `conversations/` root on the
        # permitted path and redden an assertion about the transcript halves. The
        # two tests that are about the export point this at a synthetic store of
        # their own.
        #
        # The empty reason here is a STUB CONVENIENCE, not a pair the resolver can
        # return: real absence reports `store_absent`, which suppresses the retention
        # sweep. These tests assert member lists and the permission gate, so the reason
        # is immaterial to them and a reasonless pair keeps them from also asserting
        # suppression. Do not read it as the resolver's contract.
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))
        yield

    @staticmethod
    def _synthetic_store(tmp_path, monkeypatch, *, rows: int = 3):
        """Point the export at a synthetic conversation store. Returns its path.

        Shaped like the real one only as far as the export reads it: the
        allowlisted table plus a token-bearing table that must never ride. The
        export's own properties are pinned in
        test_aws_control_backup_conversations.py; what these tests add is whether
        the call site consults the permission at all.
        """
        db = tmp_path / "data.sqlite3"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE conversations_v2 (conversation_id TEXT PRIMARY KEY, value TEXT)"
            )
            conn.executemany(
                "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
                [(f"conv-{i}", json.dumps({"turn": i})) for i in range(rows)],
            )
            conn.execute("CREATE TABLE auth_kv (k TEXT, bearer_token TEXT)")
            conn.execute(
                "INSERT INTO auth_kv (k, bearer_token) VALUES (?, ?)",
                ("idc:default", "SECRET-BEARER-TOKEN-must-not-leak"),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        return db

    @staticmethod
    def _both_halves(tmp_path, monkeypatch):
        """Populate both halves and point the module at them. Returns the cli dir."""
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        # Both Layer B files, named as kiro-cli names them: the envelope and the
        # events blob. Withholding must withhold BOTH, not whichever one a
        # narrower filter happened to match.
        (cli / "abc.json").write_bytes(b"{}\n")
        (cli / "abc.jsonl").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        return cli

    @staticmethod
    def _store(tmp_path, value, account: str = ACCOUNT, *, scope: Any = _SCOPED) -> None:
        """Write the permission straight into the state document.

        Written as raw JSON rather than through :func:`set_sessions_layer_b`, so a
        malformed or hand-mangled value can be offered to the reader -- which is
        the case the withhold-on-anything-unparseable claim is about, and one the
        boolean-validating writer cannot produce.

        The scope marker is written by default, because a grant made through the
        owner-gated writer carries one and that is the state most of these tests are
        about. Pass ``scope=None`` for a grant recorded without it, which covers the
        ``cli`` half only.
        """
        entry: dict[str, Any] = {backup.SESSIONS_LAYER_B_KEY: value}
        if scope is _SCOPED:
            entry[backup.SESSIONS_LAYER_B_SCOPE_KEY] = (
                backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
            )
        elif scope is not None:
            entry[backup.SESSIONS_LAYER_B_SCOPE_KEY] = scope
        (tmp_path / "backup.json").write_text(
            json.dumps({"accounts": {account: entry}}),
            encoding="utf-8",
        )

    @staticmethod
    def _run_capturing_names(monkeypatch):
        """Run a sessions backup with the push mocked. Returns (record, names)."""
        captured: dict[str, Any] = {}

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return
            with tarfile.open(local_path) as tar:
                captured["names"] = sorted(tar.getnames())

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )
        return record, captured.get("names", [])

    def _skip_without_pinning(self):
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )

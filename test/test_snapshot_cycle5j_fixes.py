"""A failed restore reports the state it left behind, and holds no handle open.

Two defects with one shape: the operator is told less than they need. A traceback out of
the mutation phase does not say whether the home is on the old generation or a half-applied
new one, and a leaked database handle makes the next restore fail on Windows for a reason
that has nothing to do with the bundle.
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


def _real_db(path: Path) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


def _bundle(tmp_path: Path) -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True)
    (payload / "memory.db").write_bytes(_real_db(tmp_path / "in.db"))
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
    )
    bundle = tmp_path / "b.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


class TestAMidMutationIoFailureIsReportedNotRaised:
    def test_an_os_error_becomes_a_refusal_that_names_the_state(
        self, home, tmp_path, monkeypatch, capsys
    ):
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live.db"))
        bundle = _bundle(tmp_path)

        def boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(snap, "_do_replace_mutations", boom)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "failed partway through" in out, out
        assert "previous state was put back" in out.lower(), out
        assert "Traceback" not in out

    def test_an_untouched_target_survives_a_failure_in_an_earlier_one(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """Recovery must not delete what the phase never reached.

        A file is saved by MOVING it aside at the moment of its own mutation, so when the
        phase dies partway through, later targets have no saved copy AND still hold the
        operator's original. Reading "no saved copy" as "did not exist" deletes them.

        The failure is injected on the copy of `memory.db` -- the FIRST core file the
        phase mutates -- so `crons` is a genuinely later target the loop never reaches.
        (The move-aside now goes through pinned descriptors rather than `shutil.move`, so
        the old `shutil.move` hook no longer intercepts it; failing the copy of an earlier
        target is the current-shape way to leave a later one untouched.)
        """
        original = _real_db(tmp_path / "live.db")
        (home / "memory.db").write_bytes(original)
        (home / "crons.json").write_text('{"jobs": [{"id": "mine"}]}', encoding="utf-8")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "in.db"))
        (payload / "crons.json").write_text('{"jobs": []}', encoding="utf-8")
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved",' ' "crons": "unresolved"}}',
            encoding="utf-8",
        )
        bundle = tmp_path / "two.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        # Fail the phase during memory (its first mutation), before crons is touched at
        # all. copy_file_pinned is the copy step on BOTH the pinned and by-name paths, so
        # this fires regardless of platform.
        #
        # Scoped to copies OUT OF THE BUNDLE: recovery now uses the same pinned primitive
        # (a bare copy2 there followed a link planted at the target name), so an unscoped
        # stub would also break the put-back and the test would be asserting that recovery
        # fails rather than that it works. A copy whose source is under `pre-restore-` is
        # the recovery leg and passes through.
        real_copy = snap.pinned_fs.copy_file_pinned

        def fail_on_memory_db(src, *a, **k):
            if "pre-restore-" not in str(src) and (
                "memory.db" in str(src) or k.get("name") == "memory.db"
            ):
                raise OSError(30, "Read-only file system")
            return real_copy(src, *a, **k)

        monkeypatch.setattr(snap.pinned_fs, "copy_file_pinned", fail_on_memory_db)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory,crons"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert (home / "crons.json").is_file(), "recovery deleted a file the restore never touched"
        assert "mine" in (home / "crons.json").read_text(
            encoding="utf-8"
        ), "the operator's own crons were replaced or lost"
        assert (home / "memory.db").read_bytes() == original


class TestTheIntegrityCheckLeavesNoOpenHandle:
    def test_the_restored_database_can_be_replaced_afterwards(self, home, tmp_path, capsys):
        """A held handle is invisible on POSIX and fatal on Windows -- assert the close.

        Replacing the file is the portable way to observe it: Windows raises
        PermissionError while a handle is open, and on POSIX the assertion below still
        pins that the connection was closed.
        """
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live.db"))
        rc = snap.restore_main(
            [str(_bundle(tmp_path)), "--mode", "replace", "--force", "--components", "memory"]
            + unpinnable_argv()
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "integrity: OK" in out, out

        # Would raise PermissionError on Windows if the check leaked its connection.
        moved = tmp_path / "moved.db"
        (home / "memory.db").replace(moved)
        assert moved.is_file()

    def test_a_core_file_the_restore_created_is_removed_by_recovery(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """The other half of the same rule: a creation IS undone.

        `crons.json` does not exist before the restore, so the copy this run installed has
        no pre-restore state to return to and must go. Distinguishing this from the
        untouched case above is the whole point of tracking what the phase installed.
        """
        (home / "config.json").write_text('{"mine": true}', encoding="utf-8")
        assert not (home / "crons.json").exists(), "premise: no pre-restore crons"

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "crons.json").write_text('{"jobs": []}', encoding="utf-8")
        (payload / "config.json").write_text('{"incoming": true}', encoding="utf-8")
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"crons": "unresolved",' ' "config": "unresolved"}}',
            encoding="utf-8",
        )
        bundle = tmp_path / "created.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        # crons installs first and is a creation; config then fails before being saved.
        # The move-aside is descriptor-pinned now, so the old `shutil.move` hook no longer
        # intercepts the restore; failing the copy of `config.json` is the current-shape
        # way to make config fail AFTER crons was fully created. copy_file_pinned is the
        # copy on both platforms.
        #
        # Scoped to copies OUT OF THE BUNDLE, because recovery uses the same primitive now:
        # a copy whose source is under `pre-restore-` is the put-back leg and must succeed,
        # or this test would assert recovery fails instead of asserting it undoes the
        # creation.
        real_copy = snap.pinned_fs.copy_file_pinned

        def fail_on_config(src, *a, **k):
            if "pre-restore-" not in str(src) and (
                "config.json" in str(src) or k.get("name") == "config.json"
            ):
                raise OSError(30, "Read-only file system")
            return real_copy(src, *a, **k)

        monkeypatch.setattr(snap.pinned_fs, "copy_file_pinned", fail_on_config)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "crons,config"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert not (
            home / "crons.json"
        ).exists(), "a file this restore created was left standing after recovery"
        assert (home / "config.json").read_text(
            encoding="utf-8"
        ) == '{"mine": true}', "the untouched file was replaced or removed"

    def test_the_integrity_check_uses_closing(self):
        """The connection's own context manager ends the transaction, not the handle."""
        import inspect

        src = inspect.getsource(snap.restore_main)
        i = src.index("PRAGMA integrity_check")
        window = src[max(0, i - 400) : i]
        assert (
            "closing(sqlite3.connect" in window
        ), "the integrity check must close its connection, not just its transaction"

    def test_no_bare_connection_context_manager_remains(self):
        """`with sqlite3.connect(...)` anywhere in this module leaks a handle."""
        import re

        source = Path(snap.__file__).read_text(encoding="utf-8")
        code = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
        bare = [ln.strip() for ln in code if re.search(r"with\s+sqlite3\.connect\(", ln)]
        assert bare == [], f"bare connection context managers leak handles: {bare}"
        assert sqlite3  # the fixtures build real databases

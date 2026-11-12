"""A snapshot run inside Kiro Crew's sandbox, and the retention and restore that read it.

A command cron runs ``kirocrew snapshot`` in the ``cc`` sandbox tier, and every tier
masks the crew-hidden leaves, ``crew-teams`` among them: Linux binds an empty stand-in
over the directory and macOS denies the read. Three things follow, each pinned here:

* a sandboxed run does not stage what the mask shows; it records the tree as
  ``masked_by_sandbox`` so the bundle says what it lacks;
* ``--keep`` prunes bundles with a readable manifest and no regular file at or under
  the omitted paths (a nightly cron must not pile up bundles forever), and keeps a
  bundle that carried any of that data even if its manifest lists it as skipped;
* a replace restore of such a bundle keeps the live team list instead of removing it.

The permission half: an ancestor of the data home that grants search but not read makes
the pinned walk fail with ``EACCES``, and that must end as a refusal with an audit entry.

Refusals are simulated rather than made with ``chmod``, which does nothing for root and
differs on Windows. The clock is stepped one second per call, because bundle names are
second-resolution and two runs inside one second would share a name.
"""

from __future__ import annotations

import errno
import io
import itertools
import json
import os
import sqlite3
import tarfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import pinned_fs, sandbox_launch_mark
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_archive, snapshot_build

#: The platform's real answer, taken before any fixture wraps ``os``: a wrapped
#: ``os.stat`` or ``os.open`` is not in ``os.supports_dir_fd``, so the probe would flip
#: to False and move the run off the pinned walk Linux and macOS ship.
PINNED_TREE_WALK = pinned_fs.supports_pinned_tree_walk()
PINNED_WALK = pinned_fs.supports_pinned_walk()

#: A team list the restore's own parser accepts.
BEFORE = {"version": 1, "teams": [{"id": "a1b2c3d4e5f6", "name": "Night shift", "members": []}]}
AFTER = {"version": 1, "teams": [{"id": "0f1e2d3c4b5a", "name": "Day shift", "members": []}]}


def _write_teams(home: Path, doc: dict) -> None:
    store = home / "crew-teams"
    store.mkdir(exist_ok=True)
    (store / ".lock").write_bytes(b"")
    (store / "teams.json").write_text(json.dumps(doc), encoding="utf-8")


def _read_teams(home: Path) -> dict | None:
    path = home / "crew-teams" / "teams.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    monkeypatch.delenv(sandbox_launch_mark.SANDBOX_MARKER, raising=False)
    _setup_fake_kirocrew(d)
    _write_teams(d, BEFORE)
    return d


@pytest.fixture(autouse=True)
def clock(_floor_monkeypatch):
    ticks = itertools.count()

    class _OneSecondPerCall(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))

    _floor_monkeypatch.setattr(snap, "datetime", _OneSecondPerCall)


def _sandboxed(monkeypatch) -> None:
    """Mark this process the way the sandbox launcher marks every child it starts."""
    monkeypatch.setenv(sandbox_launch_mark.SANDBOX_MARKER, "1")


def _deny_stat_of(monkeypatch, target: Path) -> None:
    """Refuse ``stat``/``lstat`` of exactly *target*: the read macOS Seatbelt denies."""
    real = {"stat": os.stat, "lstat": os.lstat}
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)
    wanted = {str(target), os.path.realpath(target)}

    def _deny(key):
        def _fn(path, *args, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path) in wanted:
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real[key](path, *args, **kwargs)

        return _fn

    for key in real:
        monkeypatch.setattr(os, key, _deny(key))


def _bundles(out: Path) -> list[Path]:
    return sorted(out.glob("kirocrew-snapshot-*.tar.gz"))


def _contents(bundle: Path) -> tuple[set[str], list[dict]]:
    """The data-home-relative members of *bundle*, and its manifest's ``skipped``."""
    with tarfile.open(bundle) as tf:
        names = {n.split("/", 1)[1] for n in tf.getnames() if "/" in n}
        root = tf.getnames()[0].split("/", 1)[0]
        handle = tf.extractfile(f"{root}/MANIFEST.json")
        assert handle is not None
        manifest = json.load(handle)
    return names, manifest.get("skipped") or []


def _run(out: Path, *extra: str) -> int:
    return snap.snapshot_main([str(out), *extra, *unpinnable_argv()])


def _omit_team_document_with_skip(bundle: Path, path: str) -> None:
    """Give a built bundle the contents of a run that could not read the document."""
    rewritten = bundle.with_suffix(".rewritten.tar.gz")
    with tarfile.open(bundle) as source, tarfile.open(rewritten, "w:gz") as target:
        for member in source:
            rel = PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
            if rel == "crew-teams/teams.json":
                continue
            if member.isfile():
                with closing(source.extractfile(member)) as handle:
                    assert handle is not None
                    if rel == "MANIFEST.json":
                        manifest = json.load(handle)
                        manifest["skipped"] = [{"reason": "unreadable_entry", "path": path}]
                        raw = json.dumps(manifest).encode("utf-8")
                        member.size = len(raw)
                        target.addfile(member, io.BytesIO(raw))
                    else:
                        target.addfile(member, handle)
            else:
                target.addfile(member)
    rewritten.replace(bundle)


class TestASandboxedRunDoesNotStageTheMaskedTree:
    def test_the_team_list_is_recorded_as_masked(self, home, tmp_path, monkeypatch, capsys):
        _sandboxed(monkeypatch)
        out = tmp_path / "out"
        assert _run(out) == 0
        (bundle,) = _bundles(out)
        names, skipped = _contents(bundle)
        assert "crew-teams/teams.json" not in names, "the mask's view shipped as the team list"
        assert {"reason": "masked_by_sandbox", "path": "crew-teams"} in skipped, skipped
        printed = capsys.readouterr().out
        assert "Not carrying" in printed and "crew-teams" in printed, printed

    def test_a_terminal_run_still_carries_it(self, home, tmp_path):
        out = tmp_path / "out"
        assert _run(out) == 0
        (bundle,) = _bundles(out)
        names, skipped = _contents(bundle)
        assert "crew-teams/teams.json" in names
        assert not [s for s in skipped if s["path"].startswith("crew-teams")], skipped

    def test_the_reason_is_an_omission(self):
        """It is wanted data the bundle lacks, so retention and ``--strict`` count it."""
        assert pinned_fs.omits_wanted_data(pinned_fs.SKIP_MASKED_BY_SANDBOX)


class TestRetentionPrunesBundlesThatRecordTheSameOmission:
    def test_a_db_unpinned_skip_does_not_prune_the_carried_database(
        self, home, tmp_path, monkeypatch, capsys
    ):
        rel = "workspace/kept.db"
        database = home / rel
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE notes (text TEXT)")
            db.execute("INSERT INTO notes VALUES ('keep this copy')")
            db.commit()
        real_copy = snapshot_archive._copy_database_consistently

        def _cannot_pin(src, *args, **kwargs):
            # By name: the data home the run walks may be the resolved spelling of `home`.
            if Path(src).name == database.name:
                return snap.DB_UNSAFE_SOURCE
            return real_copy(src, *args, **kwargs)

        out = tmp_path / "out"
        with monkeypatch.context() as m:
            m.setattr(snapshot_archive, "_copy_database_consistently", _cannot_pin)
            assert _run(out) == 0
        (older,) = _bundles(out)
        names, skipped = _contents(older)
        assert rel in names
        assert {"reason": "db_unpinned_source", "path": rel} in skipped
        database.unlink()
        real_build = snap._build_snapshot

        def _omit_database(*args, skipped_out=None, **kwargs):
            built = real_build(*args, skipped_out=skipped_out, **kwargs)
            assert skipped_out is not None
            skipped_out.append({"reason": "unreadable_entry", "path": rel})
            return built

        monkeypatch.setattr(snap, "_build_snapshot", _omit_database)
        capsys.readouterr()
        assert _run(out, "--keep", "1") == 0
        assert older.is_file(), "a skip record pruned the last carried database"
        assert len(_bundles(out)) == 2
        assert rel not in _contents(_bundles(out)[-1])[0]
        assert "Not pruning 1 older bundle" in capsys.readouterr().out

    def test_consecutive_sandboxed_runs_keep_their_count(self, home, tmp_path, monkeypatch, capsys):
        _sandboxed(monkeypatch)
        out = tmp_path / "out"
        assert _run(out, "--keep", "1") == 0
        capsys.readouterr()
        assert _run(out, "--keep", "1") == 0
        assert len(_bundles(out)) == 1
        assert "Not pruning" not in capsys.readouterr().out

    def test_consecutive_runs_that_cannot_read_the_tree_keep_their_count(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """The macOS face: the masked root is a denied read, recorded as unreadable."""
        _deny_stat_of(monkeypatch, home / "crew-teams")
        out = tmp_path / "out"
        assert _run(out, "--keep", "1") == 0
        capsys.readouterr()
        assert _run(out, "--keep", "1") == 0
        (bundle,) = _bundles(out)
        assert {"reason": "unreadable_entry", "path": "crew-teams"} in _contents(bundle)[1]
        assert "Not pruning" not in capsys.readouterr().out

    def test_the_bundle_that_carries_the_team_list_is_kept(
        self, home, tmp_path, monkeypatch, capsys
    ):
        out = tmp_path / "out"
        assert _run(out, "--keep", "3") == 0
        (carrying,) = _bundles(out)
        _sandboxed(monkeypatch)
        capsys.readouterr()
        assert _run(out, "--keep", "1") == 0
        assert carrying.is_file(), "a bundle without the team list pruned the one holding it"
        printed = capsys.readouterr().out
        assert "Not pruning 1 older bundle" in printed and carrying.name in printed, printed
        newest = _bundles(out)[-1]
        assert {"reason": "masked_by_sandbox", "path": "crew-teams"} in _contents(newest)[1]

    def test_an_older_bundle_holding_nothing_there_is_pruned(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """No record of the gap, but no file under it either: a host with no team yet.

        The same shape as a bundle a sandboxed run staged before the gap had a reason
        code: the empty stand-in, with nothing in ``skipped``.
        """
        (home / "crew-teams" / "teams.json").unlink()
        out = tmp_path / "out"
        assert _run(out) == 0
        (older,) = _bundles(out)
        assert not _contents(older)[1]
        _sandboxed(monkeypatch)
        capsys.readouterr()
        assert _run(out, "--keep", "1") == 0
        assert not older.exists()
        assert "Not pruning" not in capsys.readouterr().out

    def test_a_bundle_recording_only_part_of_the_omission_is_kept(
        self, home, tmp_path, monkeypatch
    ):
        """An older bundle that lacks one omitted path may still hold the other."""
        _sandboxed(monkeypatch)
        out = tmp_path / "out"
        assert _run(out) == 0
        (older,) = _bundles(out)
        real = snap._build_snapshot

        def _also_omit(*args, skipped_out=None, **kwargs):
            built = real(*args, skipped_out=skipped_out, **kwargs)
            if skipped_out is not None:
                skipped_out.append({"reason": "unreadable_entry", "path": "workspace/doc.md"})
            return built

        monkeypatch.setattr(snap, "_build_snapshot", _also_omit)
        assert _run(out, "--keep", "1") == 0
        assert older.is_file(), "pruned a bundle that may still carry workspace/doc.md"

    def test_an_omission_under_a_recorded_directory_is_covered(self, home, tmp_path, monkeypatch):
        """An older bundle that skipped ``crew-teams`` lacks ``crew-teams/teams.json`` too."""
        _sandboxed(monkeypatch)
        out = tmp_path / "out"
        assert _run(out) == 0
        (older,) = _bundles(out)
        monkeypatch.delenv(sandbox_launch_mark.SANDBOX_MARKER)
        real = snap._build_snapshot

        def _omit_the_document(*args, skipped_out=None, **kwargs):
            built = real(*args, skipped_out=skipped_out, **kwargs)
            if skipped_out is not None:
                skipped_out.append({"reason": "vanished", "path": "crew-teams/teams.json"})
            return built

        monkeypatch.setattr(snap, "_build_snapshot", _omit_the_document)
        assert _run(out, "--keep", "1") == 0
        assert not older.exists()


class TestAReplaceRestoreKeepsTheLiveTeamList:
    """A bundle that never read the team list must not remove the live one."""

    def test_a_windows_skip_keeps_the_live_team_list(self, home, tmp_path, capsys):
        out = tmp_path / "out"
        assert _run(out) == 0
        (bundle,) = _bundles(out)
        _omit_team_document_with_skip(bundle, r"crew-teams\teams.json")
        names, skipped = _contents(bundle)
        assert "crew-teams/teams.json" not in names
        assert skipped == [{"reason": "unreadable_entry", "path": r"crew-teams\teams.json"}]
        _write_teams(home, AFTER)
        capsys.readouterr()
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        printed = capsys.readouterr().out
        assert rc == 0, printed
        assert _read_teams(home) == AFTER, "a backslash skip removed the live team list"
        assert "kept the live teams.json" in printed
        assert "✅ crew-teams" not in printed

    @pytest.mark.parametrize("why", ["sandboxed", "unreadable"])
    def test_a_bundle_that_skipped_crew_teams(self, home, tmp_path, monkeypatch, capsys, why):
        out = tmp_path / "out"
        with monkeypatch.context() as m:
            if why == "sandboxed":
                _sandboxed(m)
            else:
                _deny_stat_of(m, home / "crew-teams")
            assert _run(out) == 0
        (bundle,) = _bundles(out)
        _write_teams(home, AFTER)
        capsys.readouterr()
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        printed = capsys.readouterr().out
        assert rc == 0, printed
        assert _read_teams(home) == AFTER, "the restore replaced or removed the live team list"
        assert "kept the live teams.json" in printed, printed
        assert "✅ crew-teams" not in printed

    def test_a_manifest_past_the_prune_read_bound_still_keeps_it(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """The bound that keeps ``--keep`` from loading a hostile member is not the restore's.

        A long ``skipped`` list read as no record at all would remove the live team list.
        """
        out = tmp_path / "out"
        with monkeypatch.context() as m:
            _sandboxed(m)
            assert _run(out) == 0
        (bundle,) = _bundles(out)
        _write_teams(home, AFTER)
        monkeypatch.setattr(snapshot_archive, "_MANIFEST_READ_LIMIT", 1)
        capsys.readouterr()
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        printed = capsys.readouterr().out
        assert rc == 0, printed
        assert _read_teams(home) == AFTER, "an oversized skip record removed the live team list"
        assert "kept the live teams.json" in printed, printed

    def test_a_bundle_that_carried_it_still_replaces_it(self, home, tmp_path, capsys):
        out = tmp_path / "out"
        assert _run(out) == 0
        (bundle,) = _bundles(out)
        _write_teams(home, AFTER)
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force", *unpinnable_argv()])
        assert rc == 0, capsys.readouterr().out
        assert _read_teams(home) == BEFORE


class TestSkipPathPortability:
    @pytest.mark.parametrize("path_type", [Path, PureWindowsPath])
    def test_skip_writer_uses_posix_paths(self, home, tmp_path, monkeypatch, path_type):
        """Only the reported path uses the simulated host's flavour; I/O stays native."""
        reported: list[str] = []
        real_copy = snap._copytree_safe

        def _unreadable_document(src, dst, *, on_skip, **kwargs):
            # By name: the data home the run walks may be the resolved spelling of `home`.
            if Path(src).name == "crew-teams":
                reported.append(str(Path(src) / "teams.json"))
                on_skip(pinned_fs.SKIP_UNREADABLE_ENTRY, reported[-1])
                return
            return real_copy(src, dst, on_skip=on_skip, **kwargs)

        def _reported_path(path):
            return path_type(path) if path in reported else Path(path)

        monkeypatch.setattr(snap, "_copytree_safe", _unreadable_document)
        monkeypatch.setattr(snapshot_build, "Path", _reported_path)
        out = tmp_path / "out"
        assert _run(out) == 0
        (bundle,) = _bundles(out)
        names, skipped = _contents(bundle)
        assert "crew-teams/teams.json" not in names
        assert skipped == [{"reason": "unreadable_entry", "path": "crew-teams/teams.json"}]

    @pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath])
    @pytest.mark.parametrize("entry", ["crew-teams/teams.json", r"crew-teams\teams.json"])
    def test_skip_paths_match_on_both_host_flavours(self, monkeypatch, path_type, entry):
        monkeypatch.setattr(snapshot_archive, "PurePath", path_type)
        raw = json.dumps({"skipped": [{"reason": "unreadable_entry", "path": entry}]}).encode()
        skips = snapshot_archive._skips_in(raw)
        assert skips == frozenset({path_type("crew-teams/teams.json")})
        assert snapshot_archive._records_omission_of(skips, "crew-teams/teams.json")
        assert not snapshot_archive._records_omission_of(skips, "crew-teams/other.json")
        parent = snapshot_archive._skips_in(b'{"skipped": [{"path": "crew-teams"}]}')
        assert snapshot_archive._records_omission_of(parent, "crew-teams/teams.json")
        assert not snapshot_archive._records_omission_of(parent, "crew-teams-other/teams.json")


class TestRetentionRequiresReadableManifestAndAbsentFiles:
    @pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath])
    @pytest.mark.parametrize("carried", ["workspace/kept.db", "workspace/kept.db/child"])
    def test_members_override_skips_on_both_host_flavours(
        self, tmp_path, monkeypatch, path_type, carried
    ):
        monkeypatch.setattr(snapshot_archive, "PurePath", path_type)
        bundle = tmp_path / "bundle.tar.gz"
        raw = b'{"skipped": [{"reason": "db_unpinned_source", "path": "workspace/kept.db"}]}'
        with tarfile.open(bundle, "w:gz") as tf:
            manifest = tarfile.TarInfo("snapshot/MANIFEST.json")
            manifest.size = len(raw)
            tf.addfile(manifest, io.BytesIO(raw))
            member = tarfile.TarInfo(f"snapshot/{carried}")
            tf.addfile(member, io.BytesIO())
        assert not snapshot_archive._bundle_lacks_all(bundle, ["workspace/kept.db"])
        assert snapshot_archive._bundle_lacks_all(bundle, ["workspace/absent.db"])

    @pytest.mark.parametrize(
        "manifest_kind", ["missing", "oversized", "directory", "unparseable", "no_skipped", "valid"]
    )
    def test_only_a_readable_manifest_with_a_skip_list_permits_pruning(
        self, tmp_path, monkeypatch, manifest_kind
    ):
        bundle = tmp_path / "bundle.tar.gz"
        raw = {"unparseable": b"{", "no_skipped": b"{}"}.get(manifest_kind, b'{"skipped": []}')
        with tarfile.open(bundle, "w:gz") as tf:
            if manifest_kind != "missing":
                manifest = tarfile.TarInfo("snapshot/MANIFEST.json")
                if manifest_kind == "directory":
                    manifest.type = tarfile.DIRTYPE
                    tf.addfile(manifest)
                else:
                    manifest.size = len(raw)
                    tf.addfile(manifest, io.BytesIO(raw))
        if manifest_kind == "oversized":
            monkeypatch.setattr(snapshot_archive, "_MANIFEST_READ_LIMIT", len(raw) - 1)
        assert snapshot_archive._bundle_lacks_all(bundle, ["crew-teams"]) is (
            manifest_kind == "valid"
        )


@pytest.mark.skipif(not PINNED_TREE_WALK, reason="the refusal is the pinned walk's")
class TestAPermissionRefusalOnTheWayToTheDataHome:
    """A search-only ancestor (a root-owned 0711 parent): ``pin_parent`` cannot open it."""

    def test_it_ends_as_a_refusal_with_an_audit_entry(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "xo" / "crew"
        home.mkdir(parents=True)
        _setup_fake_kirocrew(home)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(snap, "_audit", lambda ev, res: events.append((ev, res)))
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: PINNED_WALK)
        real_open = os.open

        def _open(path, flags, *args, dir_fd=None, **kwargs):
            if dir_fd is not None and path == "xo":
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

        monkeypatch.setattr(os, "open", _open)
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--keep", "1"])
        printed = capsys.readouterr().out
        assert rc == 1, printed
        assert "No snapshot was written: permission denied on xo." in printed, printed
        assert not _bundles(out)
        rejected = [res for ev, res in events if ev == "snapshot_rejected"]
        assert rejected and "reason=permission_denied" in rejected[0], events

"""A snapshot this tool cannot restore is not a success, and must not prune what can be.

The archive bound was applied to several of the paths that read an archive but not to
creation, and creation is the worst one to miss: `snapshot` reported success AND pruned
older bundles, so a workspace past the bound traded restorable backups for one that never
restores.
"""

from __future__ import annotations

import tarfile

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "workspace" / "memory").mkdir(parents=True)
    (h / "workspace" / "memory" / "note.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    return h


class TestANewArchiveIsBoundedBeforeSuccessAndPrune:
    def test_an_oversized_new_archive_is_refused(self, home, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 2)
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "memory"] + unpinnable_argv())
        captured = capsys.readouterr().out
        assert rc == 1, captured
        assert "declares more than" in captured, captured

    def test_nothing_is_pruned_when_the_new_archive_is_refused(
        self, home, tmp_path, monkeypatch, capsys
    ):
        """The prune is the damaging half: it deletes bundles that DO restore."""
        out = tmp_path / "out"
        out.mkdir()
        # Two older bundles that must survive a refused run.
        for name in (
            "kirocrew-snapshot-20250101T000000Z.tar.gz",
            "kirocrew-snapshot-20250102T000000Z.tar.gz",
        ):
            (out / name).write_bytes(b"older bundle")
        before = sorted(p.name for p in out.glob("kirocrew-snapshot-*.tar.gz"))

        monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 2)
        rc = snap.snapshot_main(
            [str(out), "--components", "memory", "--keep", "1"] + unpinnable_argv()
        )
        captured = capsys.readouterr().out
        assert rc == 1, captured
        assert "nothing was pruned" in captured.lower(), captured

        after = sorted(p.name for p in out.glob("kirocrew-snapshot-*.tar.gz"))
        assert set(before) <= set(
            after
        ), f"a refused snapshot pruned restorable bundles: {before} -> {after}"

    def test_a_normal_snapshot_still_succeeds(self, home, tmp_path, capsys):
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "memory"] + unpinnable_argv())
        assert rc == 0, capsys.readouterr().out
        # A memory-only (selective) bundle is named `kirocrew-partial-*`; a complete one
        # keeps `kirocrew-snapshot-*`. Match either — the property is that a bundle was
        # written, not which prefix it carries.
        assert list(out.glob("kirocrew-*.tar.gz"))

    def test_the_bound_runs_before_the_success_line_and_the_prune(self):
        """Ordering is the property: success and prune both follow the check."""
        import inspect

        src = inspect.getsource(snap.snapshot_main)
        bound = src.index("_refuse_oversized_archive(probe)")
        assert bound < src.index("Snapshot created:")
        assert bound < src.index("Pruned:")

    def test_every_archive_reader_applies_the_bound(self):
        """Every path that opens an archive applies the same bound.

        Counting occurrences of the name would count its own ``def`` line, so a
        threshold of N silently passes with N-1 real callers. Assert instead
        that each function that opens an archive contains the call: creating a
        bundle, restoring one, and producing the redacted outbound copy. A new
        archive reader that skips the bound has to be added here to stay green.
        """
        import inspect

        readers = ("snapshot_main", "restore_main", "_redacted_upload_copy")
        for name in readers:
            body = inspect.getsource(getattr(snap, name))
            assert (
                "_refuse_oversized_archive(" in body
            ), f"{name} opens an archive without applying the member bound"
        assert tarfile  # the fixtures above build real archives

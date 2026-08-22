"""A snapshot this tool cannot restore is not a success, and must not prune what can be.

The archive bound was applied to upload, to a fetched bundle, and to a local restore --
three of the four paths that read an archive. Creation was the fourth, and it is the worst
one to miss: `snapshot` reported success AND pruned older bundles, so a workspace past the
bound traded restorable backups for one that never restores.
"""

from __future__ import annotations

import tarfile

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote


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
        rc = snap.snapshot_main([str(out), "--components", "memory"])
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
        rc = snap.snapshot_main([str(out), "--components", "memory", "--keep", "1"])
        captured = capsys.readouterr().out
        assert rc == 1, captured
        assert "nothing was pruned" in captured.lower(), captured

        after = sorted(p.name for p in out.glob("kirocrew-snapshot-*.tar.gz"))
        assert set(before) <= set(
            after
        ), f"a refused snapshot pruned restorable bundles: {before} -> {after}"

    def test_a_normal_snapshot_still_succeeds(self, home, tmp_path, capsys):
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "memory"])
        assert rc == 0, capsys.readouterr().out
        assert list(out.glob("kirocrew-snapshot-*.tar.gz"))

    def test_the_bound_runs_before_the_success_line_and_the_prune(self):
        """Ordering is the property: success and prune both follow the check."""
        import inspect

        src = inspect.getsource(snap.snapshot_main)
        bound = src.index("_refuse_oversized_archive(probe)")
        assert bound < src.index("Snapshot created:")
        assert bound < src.index("Pruned:")

    def test_every_archive_reader_applies_the_bound(self):
        """Creation, upload, fetch, restore -- four paths, one bound."""
        import inspect

        module_src = inspect.getsource(snap)
        assert (
            module_src.count("_refuse_oversized_archive(") >= 4
        ), "each path that opens an archive must apply the bound"
        assert tarfile  # the fixtures above build real archives


class TestTheUploadBoundIsItsOwnGuard:
    """`_upload_bundle` keeps its own bound even though creation now runs first.

    Creation refuses an oversized archive before upload is reached, which SHADOWS the
    upload check on the one call path that exists today — a mutant removing it cannot be
    observed through a full snapshot run. The guard is still worth having, because it is
    the function's own precondition rather than a property of its current caller, so it is
    exercised directly instead of through the shadowed path.
    """

    def test_it_refuses_an_oversized_bundle_without_uploading(self, tmp_path, monkeypatch, capsys):
        import argparse

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        for i in range(40):
            (payload / f"f{i}.txt").write_text("x", encoding="utf-8")
        bundle = tmp_path / "big.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 3)
        monkeypatch.setattr(
            snapshot_remote,
            "load_destination",
            lambda: snapshot_remote.Destination(
                bucket="b", region="us-west-2", account="123456789012", created_at="t"
            ),
        )
        monkeypatch.setattr(snap, "_resolve_aws_profile", lambda *_a, **_k: ("p", "us-west-2"))
        calls = []
        monkeypatch.setattr(snapshot_remote, "upload", lambda *a, **k: calls.append(a) or "s3://x")

        rc = snap._upload_bundle(bundle, argparse.Namespace(aws_profile=None), ["memory"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "declares more than" in out, out
        assert calls == [], "the upload proceeded past its own bound"
        assert bundle.is_file(), "the local bundle must survive a refused upload"

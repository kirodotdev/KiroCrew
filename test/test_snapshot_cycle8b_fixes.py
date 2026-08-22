"""Tests for the cycle-8 review findings.

Both are cases where a guard admitted the worst input as if it were safe: a component
root that resolves to the data home ITSELF passed the containment check, and a selective
bundle was indistinguishable to an older restore from a complete one.
"""

from __future__ import annotations

import json
import os
import tarfile

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot as snap


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    _setup_fake_kirocrew(d)
    return d


class TestARootResolvingToTheHomeItselfIsRefused:
    """`workspace/memory -> ..` resolves to the data home. The old predicate allowed
    `resolved == base`, so the "component tree" became the whole home and staging swept
    `.env`, `config.json` and `sel_hmac.key` into an archive meant to carry memory."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_link_to_the_home_is_refused(self, home):
        target = home / "workspace" / "memory"
        import shutil

        shutil.rmtree(target)
        target.symlink_to("..", target_is_directory=True)
        assert snap.safe_tree_root(target, what="component root") is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_the_home_itself_is_refused_directly(self, home):
        assert snap.safe_tree_root(home, what="component root") is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_secrets_do_not_reach_the_archive_through_such_a_link(self, home, tmp_path):
        """The consequence, asserted end to end rather than at the predicate."""
        (home / ".env").write_text("TELEGRAM_TOKEN=should-never-be-archived\n")
        import shutil

        target = home / "workspace" / "memory"
        shutil.rmtree(target)
        target.symlink_to("..", target_is_directory=True)

        out = tmp_path / "out"
        snap.snapshot_main([str(out), "--components", "memory"])
        bundles = list(out.glob("*.tar.gz"))
        if not bundles:
            return  # refused outright, which is also acceptable
        with tarfile.open(bundles[0]) as tf:
            names = tf.getnames()
        assert not any(
            n.endswith("/.env") for n in names
        ), "the data home's .env reached the archive through a link root"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_link_redirecting_INSIDE_the_home_is_refused(self, home):
        """Containment is necessary and not sufficient.

        A link to another subtree inside the home passes containment — it resolves to a
        strict descendant — while changing WHICH tree is archived. Since bundles are
        uploaded, that ships the link's target under the name of the component that was
        asked for. An earlier revision of this file asserted the opposite, on the
        reasoning that "the predicate is containment, not is-this-a-link". That was
        wrong: containment answers whether a read can escape the home, not whether this
        is the tree the component declared, and only the second question stops a
        redirect. Nothing legitimate is lost -- a link aimed at a bigger disk points
        OUTSIDE the home and containment already refuses it, so the only links
        containment ever admitted were in-home redirects, which have no honest use.
        """
        other = home / "workspace" / "elsewhere"
        other.mkdir(parents=True)
        import shutil

        target = home / "workspace" / "knowledge"
        shutil.rmtree(target)
        target.symlink_to(other, target_is_directory=True)
        assert snap.safe_tree_root(target, what="component root") is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_link_ABOVE_the_home_does_not_make_every_root_unsafe(self, tmp_path):
        """The other direction, which is the real risk in tightening this.

        A data home routinely sits behind a link (a home directory on a mounted volume,
        for one). The identity walk must stop at the home, or every component tree on
        such a host would be refused and the feature would be dead there.
        """
        real = tmp_path / "real-home"
        (real / "workspace" / "memory").mkdir(parents=True)
        linked = tmp_path / "linked-home"
        linked.symlink_to(real, target_is_directory=True)

        root = linked / "workspace" / "memory"
        assert snap.safe_tree_root(root, what="component root", home=linked) == root

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_link_at_an_INTERMEDIATE_segment_below_the_home_is_refused(self, home):
        """The redirect does not have to be the root itself."""
        other = home / "otherplace"
        (other / "memory").mkdir(parents=True)
        import shutil

        shutil.rmtree(home / "workspace")
        (home / "workspace").symlink_to(other, target_is_directory=True)
        assert snap.safe_tree_root(home / "workspace" / "memory", what="component root") is None

    def test_a_plain_directory_root_is_still_accepted(self, home):
        """Guards the tightening from refusing the ordinary case."""
        root = home / "workspace" / "memory"
        root.mkdir(parents=True, exist_ok=True)
        assert snap.safe_tree_root(root, what="component root") == root

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_redirected_root_does_not_ship_another_subtree_in_a_real_archive(
        self, home, tmp_path
    ):
        """The predicate returning None is not the property that matters; what matters
        is that the bytes never reach a bundle that gets uploaded."""
        import shutil

        secrets = home / "apps"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / ".app_secret").write_text("SHOULD NEVER BE ARCHIVED", encoding="utf-8")

        memory = home / "workspace" / "memory"
        if memory.exists():
            shutil.rmtree(memory)
        memory.symlink_to(secrets, target_is_directory=True)

        out = tmp_path / "out"
        snap.snapshot_main([str(out), "--components", "memory"])
        bundles = list(out.glob("*.tar.gz"))
        if not bundles:
            return  # refused outright, also acceptable
        with tarfile.open(bundles[0]) as tf:
            names = tf.getnames()
            bodies = [(tf.extractfile(n).read() if tf.extractfile(n) else b"") for n in names]
        assert not any(n.endswith(".app_secret") for n in names), names
        assert not any(
            b"SHOULD NEVER BE ARCHIVED" in b for b in bodies
        ), "the link target's content reached the archive under the memory component"


class TestASelectiveBundleIsRefusedByOlderRestores:
    """A released restore never reads the manifest's component map and moves each live
    core file out before checking the archive has a replacement. It DOES require the
    extracted root to start with `kirocrew-snapshot-`, so a different name turns silent
    data relocation into a clean refusal on versions already in the wild."""

    def _root_of(self, bundle, tmp_path, tag):
        work = tmp_path / f"x-{tag}"
        work.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(work)
        return next(d for d in work.iterdir() if d.is_dir()).name

    def test_a_selective_bundle_root_is_named_partial(self, home, tmp_path):
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("*.tar.gz"))
        root = self._root_of(bundle, tmp_path, "sel")
        assert root.startswith("kirocrew-partial-"), root
        assert not root.startswith("kirocrew-snapshot-")

    def test_a_complete_bundle_keeps_the_original_root(self, home, tmp_path):
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out)]) == 0
        bundle = next(out.glob("*.tar.gz"))
        root = self._root_of(bundle, tmp_path, "full")
        assert root.startswith("kirocrew-snapshot-"), root

    def test_the_tarball_name_is_unchanged_so_listing_and_pruning_still_work(self, home, tmp_path):
        """Only the inner root carries the marker; rotation globs the tarball name."""
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        assert list(
            out.glob("kirocrew-snapshot-*.tar.gz")
        ), "a partial bundle became invisible to --list and pruning"

    def test_this_version_still_restores_a_partial_bundle(self, home, tmp_path):
        md = home / "workspace" / "memory"
        (md / "preferences.md").write_text("original")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("*.tar.gz"))
        (md / "preferences.md").write_text("changed")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        assert rc == 0
        assert (md / "preferences.md").read_text() == "original"

    def test_the_manifest_still_records_the_component_map(self, home, tmp_path):
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("*.tar.gz"))
        work = tmp_path / "man"
        work.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(work)
        root = next(d for d in work.iterdir() if d.is_dir())
        man = json.loads((root / "MANIFEST.json").read_text())
        assert man["version"] == 3
        assert "memory" in man["components"]

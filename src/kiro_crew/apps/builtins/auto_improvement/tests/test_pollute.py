"""The do-not-pollute acceptance test — proof that the leak detector detects leaks.

`spine/pollute.py` is a BLOCKING safety gate: it snapshots the host paths the measurement
runtime is known to write under, boots that runtime once, re-snapshots, and refuses the
whole experiment on any diff. Nothing referenced it from a test, so the gate that decides
whether an autonomous run may start had never been shown to fire — a detector that always
returns "hermetic" passes every test a green-path suite can write, and is worthless.

Every case here drives the real functions against a tmp fake-HOME with a fake boot
callable, exactly as the module docstring requires ("NEVER call this against the real host
home in a test"). The leaks are the interesting half: each one is a distinct way a runtime
can touch the host, and each must come back as a non-zero diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import pollute


def _symlinks_supported(tmp: Path) -> bool:
    """Windows forbids symlink creation without privilege/developer mode."""
    probe = tmp / "._symlink_probe"
    try:
        (tmp / "._symlink_target").write_text("x", encoding="utf-8")
        probe.symlink_to(tmp / "._symlink_target")
        probe.unlink()
        return True
    except (OSError, NotImplementedError):
        return False


def _write(path: Path, text: str = "x") -> None:
    """A boot-callable-shaped write. ``Path.write_text`` returns the byte count, so an
    inline lambda around it is a ``Callable[[], int]`` and does not satisfy
    ``BootCallable`` (``Callable[[], None]``) — mypy is right to reject it."""
    path.write_text(text, encoding="utf-8")


def _seed(root: Path) -> Path:
    """A small tree standing in for a host path the runtime is known to write under."""
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "a.json").write_text('{"n": 1}', encoding="utf-8")
    (root / "agents" / "nested").mkdir(exist_ok=True)
    (root / "agents" / "nested" / "b.txt").write_text("b", encoding="utf-8")
    return root / "agents"


class TestHermeticBootIsAllowed:
    """The pass case. A gate that blocks a clean run is as broken as one that never
    blocks — it would make every run unstartable."""

    def test_a_boot_that_touches_nothing_is_zero_diff(self, tmp_path: Path) -> None:
        watched = _seed(tmp_path / "home")
        result = pollute.run_do_not_pollute(paths=[watched], boot=lambda: None)
        assert result.zero_diff is True
        assert result.blocked is False
        assert result.changed_paths == []
        assert result.snapshotted == 1
        assert "hermetic" in result.note

    def test_reading_the_tree_is_not_a_write(self, tmp_path: Path) -> None:
        """Hashing must not itself perturb what it measures."""
        watched = _seed(tmp_path / "home")

        def read_only_boot() -> None:
            for p in watched.rglob("*"):
                if p.is_file():
                    p.read_bytes()

        assert pollute.run_do_not_pollute(paths=[watched], boot=read_only_boot).zero_diff

    def test_a_missing_path_that_stays_missing_is_zero_diff(self, tmp_path: Path) -> None:
        """A path the target *may* write is snapshotted even when absent, and its
        continued absence must reconcile — otherwise every run blocks spuriously."""
        never = tmp_path / "home" / "not-created"
        assert pollute.run_do_not_pollute(paths=[never], boot=lambda: None).zero_diff


class TestEveryKindOfLeakIsCaught:
    """The cases that matter. Each is a different way to touch the host."""

    @staticmethod
    def _leak(tmp_path: Path, boot) -> pollute.PolluteResult:
        watched = _seed(tmp_path / "home")
        return pollute.run_do_not_pollute(paths=[watched], boot=lambda: boot(watched))

    def test_a_new_file_anywhere_in_the_tree_blocks(self, tmp_path: Path) -> None:
        r = self._leak(tmp_path, lambda w: _write(w / "nested" / "leaked.json"))
        assert r.blocked is True and r.zero_diff is False
        assert len(r.changed_paths) == 1
        assert "LEAK" in r.note

    def test_modifying_an_existing_file_blocks(self, tmp_path: Path) -> None:
        """Same name, same size class, different content — the hash must be over BYTES,
        not just the directory listing."""
        r = self._leak(tmp_path, lambda w: _write(w / "a.json", '{"n": 2}'))
        assert r.blocked is True

    def test_a_same_size_edit_still_blocks(self, tmp_path: Path) -> None:
        """The subtle one: an in-place edit that preserves the byte COUNT. A detector
        comparing only (name, size) would report this tree as untouched."""
        r = self._leak(tmp_path, lambda w: _write(w / "a.json", '{"n": 9}'))
        assert r.blocked is True, "a same-length content change was reported as hermetic"

    def test_deleting_a_file_blocks(self, tmp_path: Path) -> None:
        """A runtime that REMOVES host state has polluted it just as surely as one that
        adds — and a naive 'did anything appear?' check would miss it entirely."""
        r = self._leak(tmp_path, lambda w: (w / "nested" / "b.txt").unlink())
        assert r.blocked is True

    def test_creating_a_directory_blocks(self, tmp_path: Path) -> None:
        r = self._leak(tmp_path, lambda w: (w / "brand-new-dir").mkdir())
        assert r.blocked is True

    def test_creating_a_watched_path_that_did_not_exist_blocks(self, tmp_path: Path) -> None:
        """Pure host pollution: the runtime creates a path that was absent. This is why
        a missing path is snapshotted as a sentinel rather than skipped."""
        target = tmp_path / "home" / "appears"
        result = pollute.run_do_not_pollute(paths=[target], boot=lambda: target.mkdir(parents=True))
        assert result.blocked is True
        assert result.changed_paths == [str(target)]

    def test_a_repointed_symlink_blocks(self, tmp_path: Path) -> None:
        """Recorded by TARGET, not by following it: re-pointing a link rewrites what the
        host resolves without changing any file the walk would otherwise read."""
        if not _symlinks_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host (Windows without dev mode)")
        home = tmp_path / "home"
        watched = _seed(home)
        (home / "one").write_text("1", encoding="utf-8")
        (home / "two").write_text("2", encoding="utf-8")
        link = watched / "link"
        link.symlink_to(home / "one")

        def repoint() -> None:
            link.unlink()
            link.symlink_to(home / "two")

        assert pollute.run_do_not_pollute(paths=[watched], boot=repoint).blocked is True

    def test_a_top_level_watched_symlink_is_recorded_by_target(self, tmp_path: Path) -> None:
        if not _symlinks_supported(tmp_path):
            pytest.skip("symlink creation not permitted on this host (Windows without dev mode)")
        home = tmp_path / "home"
        home.mkdir()
        (home / "a").write_text("a", encoding="utf-8")
        (home / "b").write_text("b", encoding="utf-8")
        watched = home / "cur"
        watched.symlink_to(home / "a")

        def repoint() -> None:
            watched.unlink()
            watched.symlink_to(home / "b")

        assert pollute.run_do_not_pollute(paths=[watched], boot=repoint).blocked is True

    def test_a_watched_plain_file_edit_blocks(self, tmp_path: Path) -> None:
        """A watched path need not be a directory."""
        f = tmp_path / "home" / "config.json"
        f.parent.mkdir(parents=True)
        f.write_text("{}", encoding="utf-8")
        r = pollute.run_do_not_pollute(paths=[f], boot=lambda: _write(f, '{"x": 1}'))
        assert r.blocked is True

    def test_every_leaked_path_is_named_not_just_the_first(self, tmp_path: Path) -> None:
        """The operator has to fix ALL of them, so the report must list all of them."""
        one = _seed(tmp_path / "h1")
        two = _seed(tmp_path / "h2")
        clean = _seed(tmp_path / "h3")

        def boot() -> None:
            _write(one / "leak1")
            _write(two / "leak2")

        result = pollute.run_do_not_pollute(paths=[one, two, clean], boot=boot)
        assert result.blocked is True
        assert set(result.changed_paths) == {str(one), str(two)}
        assert str(clean) not in result.changed_paths
        assert result.snapshotted == 3


class TestExcludesAreNarrow:
    """The exclude set exists for ONE reason: the orchestrator's own data dir lives under
    a snapshot root, so its own writes would register as a phantom leak. An exclude that
    silenced more than that subtree would quietly disable the gate."""

    def test_a_write_inside_an_excluded_subtree_is_ignored(self, tmp_path: Path) -> None:
        watched = _seed(tmp_path / "home")
        mine = watched / "app-data"
        mine.mkdir()

        def boot() -> None:
            _write(mine / "activity.jsonl", "log line\n")

        result = pollute.run_do_not_pollute(paths=[watched], boot=boot, exclude=[mine])
        assert result.zero_diff is True, "the orchestrator's own write blocked the run"

    def test_a_write_OUTSIDE_the_excluded_subtree_still_blocks(self, tmp_path: Path) -> None:
        """The load-bearing half: excluding a subtree must not blind the rest of the root."""
        watched = _seed(tmp_path / "home")
        mine = watched / "app-data"
        mine.mkdir()

        def boot() -> None:
            _write(mine / "activity.jsonl", "log\n")  # ignored
            _write(watched / "nested" / "real-leak.json")

        result = pollute.run_do_not_pollute(paths=[watched], boot=boot, exclude=[mine])
        assert result.blocked is True, "an exclude blinded the gate outside its own subtree"

    def test_a_sibling_sharing_a_name_prefix_is_not_excluded(self, tmp_path: Path) -> None:
        """`app-data-other` must not be swallowed by an exclude of `app-data` — prefix
        matching on raw strings is exactly how an exclude over-reaches."""
        watched = _seed(tmp_path / "home")
        mine = watched / "app-data"
        mine.mkdir()
        sibling = watched / "app-data-other"
        sibling.mkdir()

        result = pollute.run_do_not_pollute(
            paths=[watched],
            boot=lambda: _write(sibling / "leak.json"),
            exclude=[mine],
        )
        assert result.blocked is True, "a name-prefix sibling was treated as excluded"

    def test_excluding_the_watched_root_itself_neutralizes_that_path(self, tmp_path: Path) -> None:
        """Documented behaviour, pinned so it cannot become accidental: excluding the very
        path being watched makes it hash to a constant. Worth a test precisely because it
        is the one configuration that DOES disable the gate for that path."""
        watched = _seed(tmp_path / "home")
        result = pollute.run_do_not_pollute(
            paths=[watched],
            boot=lambda: _write(watched / "anything.json"),
            exclude=[watched],
        )
        assert result.zero_diff is True

    def test_a_relative_or_symlinked_exclude_still_matches(self, tmp_path: Path) -> None:
        """Excludes are resolved, so a caller passing a symlinked path still gets the
        subtree it meant."""
        watched = _seed(tmp_path / "home")
        mine = watched / "app-data"
        mine.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(mine)

        result = pollute.run_do_not_pollute(
            paths=[watched],
            boot=lambda: _write(mine / "log.jsonl"),
            exclude=[alias],
        )
        assert result.zero_diff is True, "a symlinked exclude did not match its target"


class TestTheGateFailsLoudlyRatherThanSilently:
    def test_a_boot_that_raises_propagates(self, tmp_path: Path) -> None:
        """A runtime that cannot even boot is a hard stop, not a hermetic pass. Swallowing
        this would report `zero_diff=True` for a runtime that never ran."""
        watched = _seed(tmp_path / "home")

        def boom() -> None:
            raise RuntimeError("container failed to start")

        with pytest.raises(RuntimeError, match="container failed to start"):
            pollute.run_do_not_pollute(paths=[watched], boot=boom)

    def test_an_unreadable_entry_is_counted_not_skipped(self, tmp_path: Path) -> None:
        """A leak that creates a file we cannot read must still register. Skipping on
        OSError would make "unreadable" a way to hide a write."""
        if not hasattr(os, "geteuid"):
            pytest.skip("POSIX-only: relies on chmod 000 being unreadable (not on Windows)")
        if os.geteuid() == 0:
            pytest.skip("root bypasses the mode bits this case depends on")
        watched = _seed(tmp_path / "home")
        before = pollute.snapshot([watched])

        secret = watched / "unreadable.bin"
        secret.write_bytes(b"leaked")
        secret.chmod(0o000)
        try:
            after = pollute.snapshot([watched])
            assert pollute.diff(before, after) == [str(watched)]
        finally:
            secret.chmod(0o600)  # so tmp_path cleanup can remove it

    def test_diff_is_order_independent_and_reports_both_directions(self) -> None:
        """Keyed by path, so a reordered path list cannot change the verdict; and a key
        present on only one side counts as changed."""
        assert pollute.diff({"a": "1", "b": "2"}, {"b": "2", "a": "1"}) == []
        assert pollute.diff({"a": "1"}, {"a": "1", "b": "2"}) == ["b"]
        assert pollute.diff({"a": "1", "b": "2"}, {"a": "1"}) == ["b"]

    def test_an_empty_path_set_is_reported_as_such(self) -> None:
        """Snapshotting nothing trivially yields zero diff — which is *technically*
        hermetic and practically meaningless, so `snapshotted` carries the count that
        lets a caller notice the gate was a no-op."""
        result = pollute.run_do_not_pollute(paths=[], boot=lambda: None)
        assert result.zero_diff is True
        assert result.snapshotted == 0

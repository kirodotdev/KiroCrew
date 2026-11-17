"""Review fixes on the update-never-restarts-a-running-gateway change.

Boot-time apply refuses an untracked file the update would overwrite, and a
restart waits out a boot-time apply instead of reporting it as a hang.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_server, cli_server_restart, update_next_start
from kiro_crew.cli_server_update import _untracked_collisions
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


class TestUntrackedCollision:
    def test_a_path_the_target_adds_that_exists_untracked_is_named_and_left_alone(self, tmp_path):
        repo = str(tmp_path)
        _git(repo, "init", "-q", "-b", "main")
        (tmp_path / "keep.txt").write_text("one")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "one")
        _git(repo, "checkout", "-q", "-b", "next")
        (tmp_path / "added.txt").write_text("from upstream")
        (tmp_path / "other.txt").write_text("also upstream")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "two")
        target = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True, **UTF8_TEXT
        ).stdout.strip()
        _git(repo, "checkout", "-q", "main")
        (tmp_path / "added.txt").write_text("mine, not committed")

        found = _untracked_collisions(repo, target, dict(os.environ))

        assert found == ["added.txt"]
        assert (tmp_path / "added.txt").read_text() == "mine, not committed"

    def test_no_collision_when_nothing_untracked_sits_on_an_added_path(self, tmp_path):
        repo = str(tmp_path)
        _git(repo, "init", "-q", "-b", "main")
        (tmp_path / "a.txt").write_text("a")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "one")
        (tmp_path / "b.txt").write_text("b")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "two")
        _git(repo, "checkout", "-q", "HEAD~1")
        (tmp_path / "scratch.txt").write_text("unrelated")
        target = subprocess.run(
            ["git", "rev-parse", "main"], cwd=repo, capture_output=True, check=True, **UTF8_TEXT
        ).stdout.strip()

        assert _untracked_collisions(repo, target, dict(os.environ)) == []


class TestUntrackedCollisionShapes:
    @staticmethod
    def _upstream(tmp_path, build):
        """A repo on main with *build* committed on a branch; returns (repo, target)."""
        repo = str(tmp_path)
        _git(repo, "init", "-q", "-b", "main")
        (tmp_path / "keep.txt").write_text("one")
        (tmp_path / "old.txt").write_text("a body long enough for rename detection\n" * 20)
        (tmp_path / "tracked").write_text("tracked file")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "one")
        _git(repo, "checkout", "-q", "-b", "next")
        build(tmp_path)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "two")
        target = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True, **UTF8_TEXT
        ).stdout.strip()
        _git(repo, "checkout", "-q", "main")
        return repo, target

    def test_an_untracked_file_at_a_rename_destination_is_named(self, tmp_path):
        def build(root):
            (root / "old.txt").rename(root / "moved.txt")

        repo, target = self._upstream(tmp_path, build)
        (tmp_path / "moved.txt").write_text("mine")

        assert _untracked_collisions(repo, target, dict(os.environ)) == ["moved.txt"]

    def test_an_untracked_file_where_upstream_needs_a_directory_is_named(self, tmp_path):
        def build(root):
            (root / "newdir").mkdir()
            (root / "newdir" / "helper.py").write_text("x")

        repo, target = self._upstream(tmp_path, build)
        (tmp_path / "newdir").write_text("mine")

        assert _untracked_collisions(repo, target, dict(os.environ)) == ["newdir"]

    def test_a_tracked_file_that_upstream_turns_into_a_directory_is_not_named(self, tmp_path):
        def build(root):
            (root / "tracked").unlink()
            (root / "tracked").mkdir()
            (root / "tracked" / "inner.py").write_text("x")

        repo, target = self._upstream(tmp_path, build)

        assert _untracked_collisions(repo, target, dict(os.environ)) == []


def _require_case_sensitive_disk(path) -> None:
    probe = path / "case-probe"
    probe.write_text("x")
    if (path / "CASE-PROBE").exists():
        pytest.skip("the case-only rename cannot be built on a case-insensitive disk")
    probe.unlink()


class TestUntrackedCollisionReplacements:
    @staticmethod
    def _repo(tmp_path, before, after):
        repo = str(tmp_path)
        _git(repo, "init", "-q", "-b", "main")
        (tmp_path / "keep.txt").write_text("one")
        before(tmp_path)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "one")
        _git(repo, "checkout", "-q", "-b", "next")
        after(tmp_path)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "two")
        target = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True, **UTF8_TEXT
        ).stdout.strip()
        _git(repo, "checkout", "-q", "main")
        return repo, target

    @staticmethod
    def _dir_to_file(root):
        import shutil

        shutil.rmtree(root / "d")
        (root / "d").write_text("now a file")

    @staticmethod
    def _make_dir(root):
        (root / "d").mkdir()
        (root / "d" / "a.txt").write_text("a")

    def test_a_tracked_directory_the_target_replaces_with_a_file_is_not_a_collision(self, tmp_path):
        repo, target = self._repo(tmp_path, self._make_dir, self._dir_to_file)

        assert _untracked_collisions(repo, target, dict(os.environ)) == []

    def test_an_untracked_file_inside_that_directory_is_still_named(self, tmp_path):
        repo, target = self._repo(tmp_path, self._make_dir, self._dir_to_file)
        (tmp_path / "d" / "mine.txt").write_text("not committed")

        assert _untracked_collisions(repo, target, dict(os.environ)) == ["d"]

    @staticmethod
    def _case_rename(root):
        (root / "foo.md").rename(root / "Foo.md")

    @staticmethod
    def _make_foo(root):
        (root / "foo.md").write_text("a body long enough to be one file\n" * 5)

    def test_a_case_only_rename_is_not_a_collision_where_both_spellings_are_one_file(
        self, tmp_path, monkeypatch
    ):
        _require_case_sensitive_disk(tmp_path)
        repo, target = self._repo(tmp_path, self._make_foo, self._case_rename)
        # What a case-insensitive filesystem answers: the added spelling is the file
        # the reset deletes.
        monkeypatch.setattr(os.path, "samefile", lambda a, b: True)
        (tmp_path / "Foo.md").write_text("same file under the other spelling")

        assert _untracked_collisions(repo, target, dict(os.environ)) == []

    def test_a_case_only_rename_still_names_a_distinct_untracked_file(self, tmp_path):
        _require_case_sensitive_disk(tmp_path)
        repo, target = self._repo(tmp_path, self._make_foo, self._case_rename)
        (tmp_path / "Foo.md").write_text("mine, a different file on a case-sensitive disk")

        assert _untracked_collisions(repo, target, dict(os.environ)) == ["Foo.md"]


class TestApplyingRecord:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, _floor_monkeypatch):
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

    def test_the_marking_process_is_applying_and_another_is_not(self):
        update_next_start.mark_applying()

        assert update_next_start.applying_started(os.getpid()) is not None
        assert update_next_start.applying_started(os.getpid() + 1) is None
        update_next_start.clear_applying()
        assert update_next_start.applying_started(os.getpid()) is None

    def test_a_record_past_the_apply_bound_is_stale(self):
        update_next_start.mark_applying()
        started = update_next_start.applying_started(os.getpid())
        late = started + update_next_start.APPLY_BOUND_SECS + 120

        assert update_next_start.applying_started(os.getpid(), now=late) is None


class TestWaiterThroughAnApply:
    @staticmethod
    def _proc():
        # nosemgrep: kirocrew.mock-pid-allocatable -- process double, never signalled
        return MagicMock(pid=4321, poll=MagicMock(return_value=None))

    @pytest.fixture(autouse=True)
    def _no_sleep(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(cli_server_restart, "_RESTART_READY_POLL_INTERVAL", 0)

    def test_the_wait_outlives_its_window_while_the_replacement_applies(self):
        import time

        probe = MagicMock(side_effect=[0, 0, 0, 200])
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", probe),
            patch("kiro_crew.cli_server._replacement_is_serving", return_value=True),
            patch.object(
                update_next_start, "applying_started", return_value=time.time()
            ) as applying,
        ):
            verdict = cli_server._wait_gateway_ready(self._proc(), 7777, None, timeout=0)

        assert verdict == (cli_server._READY_OK, None)
        applying.assert_called_with(4321)
        assert probe.call_count == 4

    def test_without_an_apply_the_same_window_is_a_timeout(self):
        with (
            patch("kiro_crew.cli_server._probe_gateway_ready", return_value=0),
            patch.object(update_next_start, "applying_started", return_value=None),
        ):
            verdict = cli_server._wait_gateway_ready(self._proc(), 7777, None, timeout=0)

        assert verdict == (cli_server._READY_TIMEOUT, None)

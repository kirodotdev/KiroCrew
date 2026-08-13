"""Snapshot directory location, CLI redirection, and retention."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from kiro_crew.browser_cli import snapshots as mod


def _write(directory: Path, name: str, age_s: float) -> Path:
    """Create *name* in *directory* with an mtime *age_s* seconds in the past."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("- generic [ref=e1]\n", encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.is_file()}


def test_snapshot_dir_is_under_the_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))

    assert mod.snapshot_dir() == home / "playwright-snapshots"


def test_snapshot_dir_is_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI defaults to ``<cwd>/.playwright-cli``; the service needs one path.

    An agent turn can run from anywhere, so a cwd-derived directory would leave
    files where the pruner never looks.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    first_cwd = tmp_path / "somewhere"
    second_cwd = tmp_path / "elsewhere"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    from_first = mod.snapshot_dir()
    monkeypatch.chdir(second_cwd)
    from_second = mod.snapshot_dir()

    assert from_first == from_second
    assert first_cwd not in from_first.parents
    assert second_cwd not in from_second.parents


def test_cli_env_overrides_points_the_cli_at_the_service_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    env = mod.cli_env_overrides()

    assert env == {"PLAYWRIGHT_MCP_OUTPUT_DIR": str(mod.snapshot_dir())}


def test_cli_env_override_value_is_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI resolves a relative value against its own cwd, defeating the point."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    value = mod.cli_env_overrides()[mod.OUTPUT_DIR_ENV]

    assert Path(value).is_absolute()


def test_prune_removes_by_count_keeping_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    for i in range(5):
        _write(d, f"page-2026-02-{i + 1:02d}T00-00-00-000Z.yml", age_s=i)

    removed = mod.prune(max_age_s=10_000, max_files=2, grace_s=0)

    assert removed == 3
    # Ages 0 and 1 are the two newest.
    assert _names(d) == {
        "page-2026-02-01T00-00-00-000Z.yml",
        "page-2026-02-02T00-00-00-000Z.yml",
    }


def test_prune_removes_by_age_within_the_count_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Age alone must evict: a count bound would keep these forever on an idle host."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-06-01T00-00-00-000Z.yml", age_s=5)
    _write(d, "page-2026-01-01T00-00-00-000Z.yml", age_s=9_000)
    _write(d, "page-2026-01-02T00-00-00-000Z.yml", age_s=9_500)

    removed = mod.prune(max_age_s=3_600, max_files=100)

    assert removed == 2
    assert _names(d) == {"page-2026-06-01T00-00-00-000Z.yml"}


def test_prune_applies_both_bounds_in_one_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither bound alone produces this outcome."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-03-01T00-00-00-000Z.yml", age_s=1)
    _write(d, "page-2026-03-02T00-00-00-000Z.yml", age_s=2)
    _write(d, "page-2026-03-03T00-00-00-000Z.yml", age_s=3)
    _write(d, "page-2026-03-04T00-00-00-000Z.yml", age_s=99_999)

    # Count alone (3) would drop only "old"; age alone would drop only "old".
    # Together: "old" for age, "new-3" for count.
    removed = mod.prune(max_age_s=3_600, max_files=2, grace_s=0)

    assert removed == 2
    assert _names(d) == {
        "page-2026-03-01T00-00-00-000Z.yml",
        "page-2026-03-02T00-00-00-000Z.yml",
    }


def test_prune_never_removes_the_newest_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current session most likely still refers to it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-01-12T00-00-00-000Z.yml", age_s=100_000)
    _write(d, "page-2026-01-13T00-00-00-000Z.yml", age_s=200_000)

    removed = mod.prune(max_age_s=1, max_files=1)

    assert removed == 1
    assert _names(d) == {"page-2026-01-12T00-00-00-000Z.yml"}


def test_prune_keeps_newest_even_with_a_zero_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setting that could empty the directory would break a live turn."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-01-10T00-00-00-000Z.yml", age_s=100_000)

    assert mod.prune(max_age_s=1, max_files=0) == 0
    assert _names(d) == {"page-2026-01-10T00-00-00-000Z.yml"}


def test_prune_keeps_everything_within_both_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-01-05T00-00-00-000Z.yml", age_s=1)
    _write(d, "page-2026-01-06T00-00-00-000Z.yml", age_s=2)

    assert mod.prune(max_age_s=3_600, max_files=10) == 0
    assert _names(d) == {"page-2026-01-05T00-00-00-000Z.yml", "page-2026-01-06T00-00-00-000Z.yml"}


def test_prune_leaves_subdirectories_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI keeps traces in subdirectories; a recording may be in progress."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-01-09T00-00-00-000Z.yml", age_s=0)
    _write(d, "page-2026-01-11T00-00-00-000Z.yml", age_s=100_000)
    traces = d / "traces"
    _write(traces, "trace-old.zip", age_s=100_000)

    mod.prune(max_age_s=1, max_files=1)

    assert traces.is_dir()
    assert (traces / "trace-old.zip").exists()


def test_prune_returns_zero_when_the_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    assert not mod.snapshot_dir().exists()
    assert mod.prune(max_age_s=1, max_files=1) == 0


def test_prune_never_raises_when_a_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs on a schedule with no caller to receive an exception."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-01-09T00-00-00-000Z.yml", age_s=0)
    _write(d, "page-2026-01-07T00-00-00-000Z.yml", age_s=100_000)

    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", refuse)

    assert mod.prune(max_age_s=1, max_files=1) == 0
    assert _names(d) == {"page-2026-01-09T00-00-00-000Z.yml", "page-2026-01-07T00-00-00-000Z.yml"}


def test_prune_never_raises_when_the_directory_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _write(mod.snapshot_dir(), "page-2026-01-05T00-00-00-000Z.yml", age_s=0)

    def refuse(self: Path) -> object:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", refuse)

    assert mod.prune(max_age_s=1, max_files=1) == 0


def test_prune_tolerates_a_file_vanishing_mid_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon writes concurrently, so an entry can disappear between calls."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    d = mod.snapshot_dir()
    _write(d, "page-2026-01-09T00-00-00-000Z.yml", age_s=0)
    _write(d, "page-2026-01-08T00-00-00-000Z.yml", age_s=100_000)

    real_stat = Path.stat

    def flaky(self: Path, *a: object, **kw: object) -> os.stat_result:
        if self.name == "page-2026-01-08T00-00-00-000Z.yml":
            raise FileNotFoundError(self)
        return real_stat(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky)

    assert mod.prune(max_age_s=1, max_files=1) == 0


class TestGracePeriodPreventsRace:
    """A recently handed-out path survives pruning even past the count bound."""

    def test_file_within_grace_period_is_never_pruned_by_count(
        self, tmp_path, monkeypatch
    ):
        """Regression: the count bound could race an agent reading its own path."""
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        # A file the agent was just handed (30 seconds old).
        target = tmp_path / "page-2026-06-01T00-00-00-000Z.yml"
        target.write_text("- generic [ref=e1]\n")
        os.utime(target, (time.time() - 30, time.time() - 30))

        # Enough newer files to push target past the count bound.
        for i in range(mod.DEFAULT_MAX_FILES + 5):
            ms = i % 1000
            sec = (i // 1000) % 60
            p = tmp_path / (
                f"page-2026-07-01T00-{sec:02d}-00-{ms:03d}Z.yml"
            )
            p.write_text("- generic\n")
            os.utime(p, (time.time() - (i * 0.01), time.time() - (i * 0.01)))

        mod.prune(max_age_s=mod.DEFAULT_MAX_AGE_S, max_files=mod.DEFAULT_MAX_FILES)

        assert target.exists(), (
            "a file younger than the grace period must survive "
            "even when the count bound would delete it"
        )

    def test_file_within_grace_period_is_never_pruned_by_age(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        # A file only 60 seconds old, but max_age_s is set aggressively low.
        recent = tmp_path / "page-2026-06-01T00-00-00-000Z.yml"
        recent.write_text("- generic\n")
        os.utime(recent, (time.time() - 60, time.time() - 60))
        # A newer file so recent is not at index 0.
        newest = tmp_path / "page-2026-06-02T00-00-00-000Z.yml"
        newest.write_text("- generic\n")

        mod.prune(max_age_s=10, max_files=1)

        assert recent.exists(), (
            "a file younger than the grace period must survive "
            "even when the age bound would delete it"
        )

    def test_file_past_grace_period_is_still_pruned(
        self, tmp_path, monkeypatch
    ):
        """The disk bound survives: once grace expires, pruning resumes."""
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        # A file well past the grace period (10 minutes old, grace is 5 min).
        old = tmp_path / "page-2026-01-01T00-00-00-000Z.yml"
        old.write_text("- generic\n")
        os.utime(old, (time.time() - 600, time.time() - 600))
        # A newest file to protect index 0.
        newest = tmp_path / "page-2026-06-01T00-00-00-000Z.yml"
        newest.write_text("- generic\n")

        removed = mod.prune(max_age_s=60, max_files=1)

        assert removed == 1
        assert not old.exists()
        assert newest.exists()

    def test_disk_bound_holds_under_sustained_capture(
        self, tmp_path, monkeypatch
    ):
        """The directory stays bounded even with a large volume of old files."""
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        total = 500
        for i in range(total):
            # Encode uniqueness in the millisecond and second fields to stay
            # within the CLI's exact timestamp shape.
            ms = i % 1000
            sec = (i // 1000) % 60
            minute = (i // 60000) % 60
            p = tmp_path / (
                f"page-2026-01-01T{minute:02d}-{sec:02d}-00-{ms:03d}Z.yml"
            )
            p.write_text("- generic\n")
            # All past grace period (oldest 10 hours, newest 6 minutes).
            age = mod.GRACE_PERIOD_S + 60 + (i * 60)
            os.utime(p, (time.time() - age, time.time() - age))

        mod.prune(
            max_age_s=mod.DEFAULT_MAX_AGE_S, max_files=mod.DEFAULT_MAX_FILES
        )

        remaining = list(tmp_path.glob("page-*.yml"))
        # At most max_files survive (the count bound is enforced).
        assert len(remaining) <= mod.DEFAULT_MAX_FILES


class TestSavedStorageStateSurvivesPruning:
    """A saved login is not throwaway output and must outlive every bound."""

    def test_storage_state_is_never_pruned_by_age(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        old = time.time() - (10 * 24 * 60 * 60)
        auth = tmp_path / "storage-state-2026-01-01T00-00-00-000Z.json"
        auth.write_text('{"cookies": []}')
        os.utime(auth, (old, old))
        stale = tmp_path / "page-2026-01-01T00-00-00-000Z.yml"
        stale.write_text("- generic")
        os.utime(stale, (old, old))
        newest = tmp_path / "page-2026-06-01T00-00-00-000Z.yml"
        newest.write_text("- generic")

        removed = mod.prune(max_age_s=60.0, max_files=100)

        assert auth.exists(), "a saved login must survive an age bound"
        assert not stale.exists()
        assert newest.exists()
        assert removed == 1

    def test_storage_state_does_not_consume_the_count_budget(self, tmp_path, monkeypatch):
        # Protected files are excluded before the count test, so a directory full
        # of saved logins cannot push live snapshots out of the window.
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        for i in range(5):
            (tmp_path / f"storage-state-2026-02-{i + 1:02d}T00-00-00-000Z.json").write_text("{}")
        pages = []
        for i in range(3):
            p = tmp_path / f"page-2026-02-{i + 1:02d}T00-00-00-000Z.yml"
            p.write_text("- generic")
            os.utime(p, (time.time() - (10 - i), time.time() - (10 - i)))
            pages.append(p)

        mod.prune(max_age_s=0, max_files=2, grace_s=0)

        assert all((tmp_path / f"storage-state-2026-02-{i + 1:02d}T00-00-00-000Z.json").exists() for i in range(5))
        # Newest two pages kept: the count bound saw only the three pages.
        assert pages[2].exists() and pages[1].exists() and not pages[0].exists()


class TestPruneOnlyTouchesCliOutput:
    """A background deleter must keep what it does not recognize."""

    def test_a_human_file_in_the_output_dir_survives(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        old = time.time() - 90_000  # well past the age bound

        notes = tmp_path / "notes.txt"
        notes.write_text("my login steps")
        os.utime(notes, (old, old))

        snap = tmp_path / "page-2026-01-01T00-00-00-000Z.yml"
        snap.write_text("tree")
        os.utime(snap, (old, old))

        # A newer file so the aged ones are not spared as "the newest".
        newest = tmp_path / "page-2026-06-01T00-00-00-000Z.yml"
        newest.write_text("tree")

        mod.prune(max_age_s=3600, max_files=50)

        assert notes.exists(), "an unrecognized file must never be pruned"

        assert not snap.exists(), "CLI output past the age bound should go"
        assert newest.exists()

    def test_a_near_miss_timestamp_is_not_treated_as_cli_output(self, tmp_path, monkeypatch):
        """The shape must be the CLI's exact one, not "digits and dashes".

        A loose `T[\\d\\-.]+Z` also matched `notes-2026-08-13T1Z.txt`, so a
        hand-authored file in that shape would be deleted once it aged out.
        """
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        near = tmp_path / "notes-2026-08-13T1Z.txt"
        near.write_text("mine")
        os.utime(near, (time.time() - 90_000, time.time() - 90_000))
        (tmp_path / "page-2026-06-01T00-00-00-000Z.yml").write_text("x")

        mod.prune(max_age_s=3600, max_files=50)

        assert near.exists()

    def test_the_count_bound_also_ignores_unrecognized_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "snapshot_dir", lambda: tmp_path)
        for i in range(5):
            (tmp_path / f"page-2026-02-{i + 1:02d}T00-00-00-000Z.yml").write_text("x")
        keep = tmp_path / "README.md"
        keep.write_text("do not delete me")

        mod.prune(max_age_s=10**9, max_files=2, grace_s=0)

        assert keep.exists()
        assert len(list(tmp_path.glob("page-*.yml"))) == 2

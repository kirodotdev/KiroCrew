"""Tests for the markdown memory read surface.

Covers ``kirocrew memory show`` (the documented-but-previously-missing
command) and ``kirocrew memory export --include-markdown``, plus the
``MemoryStore`` readers behind them. The most important guard is that
``export`` WITHOUT the flag stays byte-identical to its previous shape.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import cli_commands
from kiro_crew.memory import MemoryStore

# ── Helpers ──


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=tmp_path / "ws")


def _populated_store(tmp_path: Path) -> MemoryStore:
    ms = _store(tmp_path)
    ms.init()
    ms.write_preferences("# User Preferences\n\n- prefers pytest\n")
    ms.write_projects("# Active Projects\n\n- shipping the read API\n")
    history = tmp_path / "ws" / "memory" / "history"
    (history / "2026-01-01.md").write_text(
        "# 2026-01-01\n\n#### 09:00\nold day\n", encoding="utf-8"
    )
    (history / "2026-03-05.md").write_text(
        "# 2026-03-05\n\n#### 10:00\nnew day\n", encoding="utf-8"
    )
    (history / "2026-02-02.md").write_text(
        "# 2026-02-02\n\n#### 11:00\nmid day\n", encoding="utf-8"
    )
    (history / "notes.md").write_text("not a daily file\n", encoding="utf-8")
    return ms


def _show_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "mem_action": "show",
        "target": None,
        "format": "md",
        "since": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class _EmptyVectorStore:
    """Stub with exactly the surface ``_memory_cmd`` export uses."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def init(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_all_semantic(self) -> list:
        return []

    def get_episodic_list(self, limit: int = 0) -> list:
        return []

    def get_events(self, limit: int = 0) -> list:
        return []

    def import_memory(self, data: dict) -> dict:
        return {"semantic": 0, "episodic": 0, "skipped": 0}


# ── MemoryStore readers ──


class TestMarkdownSnapshot:
    def test_missing_files_are_normal_not_errors(self, tmp_path: Path) -> None:
        snap = _store(tmp_path).markdown_snapshot()
        for key in ("preferences", "projects"):
            assert snap[key]["content"] == ""
            assert snap[key]["updated_at"] is None
            assert snap[key]["path"]
        assert snap["history"] == []

    def test_reads_content_and_utc_mtime(self, tmp_path: Path) -> None:
        snap = _populated_store(tmp_path).markdown_snapshot()
        assert "- prefers pytest" in snap["preferences"]["content"]
        assert "- shipping the read API" in snap["projects"]["content"]
        for key in ("preferences", "projects"):
            parsed = datetime.fromisoformat(snap[key]["updated_at"])
            assert parsed.utcoffset() is not None and not parsed.utcoffset()
            assert str(tmp_path) in snap[key]["path"]

    def test_history_entries_sorted_dated_and_non_daily_skipped(self, tmp_path: Path) -> None:
        entries = _populated_store(tmp_path).markdown_snapshot()["history"]
        assert [e["date"] for e in entries] == ["2026-01-01", "2026-02-02", "2026-03-05"]
        assert all("updated_at" in e and "path" in e and "content" in e for e in entries)
        assert not any("notes" in e["path"] for e in entries)

    def test_since_filters_history_days(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        entries = ms.markdown_snapshot(since=date(2026, 2, 1))["history"]
        assert [e["date"] for e in entries] == ["2026-02-02", "2026-03-05"]


class TestHistorySnapshotAggregateBounds:
    """Many valid dated files must not yield an unbounded snapshot."""

    def test_entry_count_cap_keeps_newest_days_oldest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        for day in range(1, 10):
            (history / f"2026-01-{day:02d}.md").write_text(f"day {day}\n", encoding="utf-8")
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_ENTRIES", 3)
        entries = ms.read_history_entries()
        assert [e["date"] for e in entries] == ["2026-01-07", "2026-01-08", "2026-01-09"]

    def test_cumulative_byte_cap_trims_older_days(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        for day in range(1, 5):
            (history / f"2026-01-{day:02d}.md").write_text("x" * 100, encoding="utf-8")
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_BYTES", 250)
        entries = ms.read_history_entries()
        # Newest two fit (200 bytes); the third would exceed 250 and stops the walk.
        assert [e["date"] for e in entries] == ["2026-01-03", "2026-01-04"]

    def test_single_oversized_day_is_still_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        (history / "2026-01-01.md").write_text("x" * 100, encoding="utf-8")
        (history / "2026-01-02.md").write_text("y" * 100, encoding="utf-8")
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_BYTES", 50)
        entries = ms.read_history_entries()
        # The newest entry always lands even when it alone exceeds the cap
        # (its size is bounded by the per-file read cap, not this one).
        assert [e["date"] for e in entries] == ["2026-01-02"]


class TestMarkdownSnapshotSymlinkGuard:
    """A planted link in the agent-writable memory dir must never leak file
    contents through the read API. No HOME/USERPROFILE overrides here: the
    guard is the lstat-based no-link gate plus in-root containment, which
    reject the escaping link regardless of what is_sensitive_path anchors to
    — and leaving HOME real keeps the exercised gate protecting the real
    credential roots."""

    SECRET = "aws_secret_access_key = SUPERSECRET"

    def _secret_outside_memory_root(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        secret = outside / "credentials"
        secret.write_text(self.SECRET, encoding="utf-8")
        return secret

    def _symlink_or_skip(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows CI
            pytest.skip("symlinks not available on this platform")

    def test_history_symlink_to_outside_file_is_skipped(self, tmp_path: Path) -> None:
        secret = self._secret_outside_memory_root(tmp_path)
        ms = _populated_store(tmp_path)
        history = tmp_path / "ws" / "memory" / "history"
        self._symlink_or_skip(history / "2026-05-05.md", secret)
        snapshot = ms.markdown_snapshot()
        assert self.SECRET not in json.dumps(snapshot)
        assert [e["date"] for e in snapshot["history"]] == [
            "2026-01-01",
            "2026-02-02",
            "2026-03-05",
        ]

    def test_preferences_symlink_to_outside_file_yields_empty_entry(self, tmp_path: Path) -> None:
        secret = self._secret_outside_memory_root(tmp_path)
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        self._symlink_or_skip(tmp_path / "ws" / "memory" / "preferences.md", secret)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None

    def test_symlinked_history_dir_is_refused(self, tmp_path: Path) -> None:
        real_history = tmp_path / "elsewhere" / "history"
        real_history.mkdir(parents=True)
        (real_history / "2026-04-04.md").write_text("# 2026-04-04\nleak\n", encoding="utf-8")
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        self._symlink_or_skip(tmp_path / "ws" / "memory" / "history", real_history)
        assert ms.markdown_snapshot()["history"] == []


class TestReadRefusalSelAudit:
    """Every security refusal in the guarded read surface must leave a
    tamper-evident SEL denial record — a process-log warning alone can be
    suppressed by the same same-host actor the guard defends against."""

    def _recording_sel(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        import kiro_crew.sel as sel_mod

        calls: list[dict] = []

        class _StubSel:
            def log_governance_decision(self, **kwargs: object) -> None:
                calls.append(dict(kwargs))

        monkeypatch.setattr(sel_mod, "sel", lambda: _StubSel())
        return calls

    def _symlink_or_skip(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows CI
            pytest.skip("symlinks not available on this platform")

    def test_leaf_symlink_refusal_emits_sel_denial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._recording_sel(monkeypatch)
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        link = tmp_path / "ws" / "memory" / "preferences.md"
        self._symlink_or_skip(link, outside)
        entry = ms.markdown_snapshot()["preferences"]
        assert entry["content"] == ""
        denials = [c for c in calls if c.get("outcome") == "denied"]
        assert denials, "symlink refusal must emit an SEL denial"
        assert denials[0]["rule"] == "leaf_link"
        assert denials[0]["layer"] == "memory_read_guard"
        assert str(link) in str(denials[0]["item"])

    def test_root_reparse_refusal_emits_sel_denial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._recording_sel(monkeypatch)
        real_history = tmp_path / "elsewhere" / "history"
        real_history.mkdir(parents=True)
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        self._symlink_or_skip(tmp_path / "ws" / "memory" / "history", real_history)
        assert ms.markdown_snapshot()["history"] == []
        assert any(c.get("rule") == "root_reparse_point" for c in calls)

    def test_fifo_refusal_emits_sel_denial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not hasattr(os, "mkfifo"):  # pragma: no cover - Windows CI
            pytest.skip("mkfifo not available on this platform")
        calls = self._recording_sel(monkeypatch)
        ms = _store(tmp_path)
        mem_dir = tmp_path / "ws" / "memory"
        mem_dir.mkdir(parents=True)
        os.mkfifo(mem_dir / "preferences.md")
        entry = ms.markdown_snapshot()["preferences"]
        assert entry["content"] == ""
        assert any(c.get("rule") == "not_regular_file" for c in calls)

    def test_audit_failure_never_breaks_the_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.sel as sel_mod

        def _boom() -> object:
            raise RuntimeError("sel unavailable")

        monkeypatch.setattr(sel_mod, "sel", _boom)
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        self._symlink_or_skip(tmp_path / "ws" / "memory" / "preferences.md", outside)
        entry = ms.markdown_snapshot()["preferences"]
        assert entry["content"] == ""  # refusal still fails closed, no raise


class TestGuardedReadRobustness:
    """Special, oversized, malformed, and concurrently-rewritten files must
    degrade to empty entries or a consistent retry — never a crash, an OOM
    read, or content paired with another version's metadata."""

    def test_invalid_utf8_yields_empty_entry(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        (tmp_path / "ws" / "memory" / "preferences.md").write_bytes(b"\xff\xfe broken \x80")
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None

    def test_oversized_file_yields_empty_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import hooks

        ms = _store(tmp_path)
        ms.init()
        (tmp_path / "ws" / "memory" / "preferences.md").write_text("x" * 64, encoding="utf-8")
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 16)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None

    def test_fifo_special_file_is_rejected_not_read(self, tmp_path: Path) -> None:
        import os

        if not hasattr(os, "mkfifo"):  # pragma: no cover - Windows CI
            pytest.skip("mkfifo not available on this platform")
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        os.mkfifo(history / "2026-06-06.md")
        assert ms.markdown_snapshot()["history"] == []

    def test_concurrent_rewrite_retries_to_consistent_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import memory as memory_mod

        ms = _store(tmp_path)
        ms.init()
        prefs_path = tmp_path / "ws" / "memory" / "preferences.md"
        prefs_path.write_text("old version", encoding="utf-8")
        real_reader = memory_mod.safe_read_file_bytes_nolink
        state = {"raced": False}

        def racing_reader(raw: str, **kwargs: object) -> bytes | None:
            data = real_reader(raw, **kwargs)
            if not state["raced"]:
                state["raced"] = True
                prefs_path.write_text("new version longer", encoding="utf-8")
            return data

        monkeypatch.setattr(memory_mod, "safe_read_file_bytes_nolink", racing_reader)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == "new version longer"
        assert prefs["updated_at"] is not None

    def test_file_changing_on_every_read_degrades_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import memory as memory_mod

        ms = _store(tmp_path)
        ms.init()
        prefs_path = tmp_path / "ws" / "memory" / "preferences.md"
        prefs_path.write_text("v0", encoding="utf-8")
        real_reader = memory_mod.safe_read_file_bytes_nolink
        counter = {"n": 0}

        def always_racing(raw: str, **kwargs: object) -> bytes | None:
            data = real_reader(raw, **kwargs)
            counter["n"] += 1
            # Vary the LENGTH each round: the size delta guarantees the
            # before/after comparison sees the change even on filesystems
            # whose mtime granularity is coarser than this loop.
            prefs_path.write_text("v" * (counter["n"] + 2), encoding="utf-8")
            return data

        monkeypatch.setattr(memory_mod, "safe_read_file_bytes_nolink", always_racing)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None


class TestUncWorkspaceGate:
    """On Windows, an agent-configured UNC workspace must be rejected
    LEXICALLY before any stat/glob/exists — those calls are themselves the
    outbound SMB credential probe."""

    def _unc_store(self, monkeypatch: pytest.MonkeyPatch) -> "object":
        import types

        from kiro_crew import memory as memory_mod
        from kiro_crew.memory import MemoryStore

        store = MemoryStore(workspace=Path("//evil-host/share/ws"))
        # Patch ONLY memory.py's view of os (its sole use is the gate's
        # os.name check) — patching the global os.name would make pathlib
        # dispatch WindowsPath everywhere on a POSIX test host.
        monkeypatch.setattr(memory_mod, "os", types.SimpleNamespace(name="nt"))
        return store

    def test_snapshot_refuses_unc_workspace_without_filesystem_touch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import memory as memory_mod

        ms = self._unc_store(monkeypatch)

        def boom(*args: object, **kwargs: object) -> bytes | None:  # pragma: no cover
            raise AssertionError("filesystem reader must not be reached for a UNC workspace")

        monkeypatch.setattr(memory_mod, "safe_read_file_bytes_nolink", boom)
        snapshot = ms.markdown_snapshot()
        assert snapshot["preferences"]["content"] == ""
        assert snapshot["preferences"]["updated_at"] is None
        assert snapshot["history"] == []

    def test_non_windows_is_unaffected(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        assert "- prefers pytest" in ms.markdown_snapshot()["preferences"]["content"]


# ── kirocrew memory show ──


class TestMemoryShowCli:
    def test_show_each_target_markdown(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        ms = _populated_store(tmp_path)
        expected = {
            "preferences": "- prefers pytest",
            "projects": "- shipping the read API",
            "history": "mid day",
        }
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            for target, marker in expected.items():
                cli_commands._memory_cmd(_show_args(target=target))
                assert marker in capsys.readouterr().out

    def test_show_all_targets_when_omitted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _populated_store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args())
        out = capsys.readouterr().out
        assert "- prefers pytest" in out and "- shipping the read API" in out and "old day" in out

    def test_show_json_returns_structured_entries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _populated_store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args(target="preferences", format="json"))
            prefs = json.loads(capsys.readouterr().out)
            assert set(prefs) == {"path", "updated_at", "content"}
            cli_commands._memory_cmd(_show_args(target="history", format="json"))
            history = json.loads(capsys.readouterr().out)
            assert [e["date"] for e in history] == ["2026-01-01", "2026-02-02", "2026-03-05"]

    def test_show_history_since_filter(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        ms = _populated_store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(
                _show_args(target="history", format="json", since="2026-03-01")
            )
        history = json.loads(capsys.readouterr().out)
        assert [e["date"] for e in history] == ["2026-03-05"]

    def test_show_empty_store_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args(target="preferences"))
        assert capsys.readouterr().out == ""

    def test_since_rejected_for_non_history_target(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with pytest.raises(SystemExit) as excinfo:
                cli_commands._memory_cmd(_show_args(target="preferences", since="2026-01-01"))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "--since applies only to history" in captured.err
        assert captured.out == ""

    def test_invalid_since_date_exits_nonzero_with_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A scheduled JSON consumer must get a failure signal, not non-JSON
        text on stdout with exit 0."""
        ms = _store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with pytest.raises(SystemExit) as excinfo:
                cli_commands._memory_cmd(_show_args(target="history", since="March 1st"))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Invalid --since date" in captured.err
        assert captured.out == ""

    def test_store_reads_where_the_consolidator_writes(self) -> None:
        """The read surface must anchor where the gateway's consolidator
        (the writer of this layer) writes: the bare MemoryStore default."""
        from kiro_crew.memory import MemoryStore, workspace_dir

        store = cli_commands._markdown_memory_store()
        writer = MemoryStore()
        assert store._memory_dir == writer._memory_dir
        assert str(workspace_dir()) in str(store._memory_dir)

    def test_markdown_output_strips_terminal_control_sequences(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- evil \x1b]0;pwned\x07title\n")
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args(target="preferences"))
        out = capsys.readouterr().out
        assert "\x1b" not in out and "pwned" not in out and "evil" in out


# ── kirocrew memory export --include-markdown ──


class TestAtomicMemoryWrites:
    """Writers publish via temp-file + os.replace so a concurrent reader only
    ever observes committed versions — never a truncated in-progress file."""

    def test_writers_produce_content_with_no_temp_residue(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- atomic\n")
        ms.write_projects("- project state\n")
        ms.append_history("an entry")
        memory_dir = tmp_path / "ws" / "memory"
        residue = [p.name for p in memory_dir.rglob("*.tmp")]
        assert residue == []
        snap = ms.markdown_snapshot()
        assert "- atomic" in snap["preferences"]["content"]
        assert "- project state" in snap["projects"]["content"]
        assert len(snap["history"]) == 1

    def test_failed_replace_cleans_up_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import atomic_write as atomic_write_mod

        ms = _store(tmp_path)
        ms.init()

        def boom(src: object, dst: object) -> None:
            raise OSError("simulated replace failure")

        # Patch the rename step inside the atomic_write helper the memory
        # writers delegate to — patching the global os.replace would affect
        # unrelated machinery.
        monkeypatch.setattr(atomic_write_mod, "replace_with_retry", boom)
        with pytest.raises(OSError):
            ms.write_preferences("# User Preferences\n\n- lost\n")
        residue = list((tmp_path / "ws" / "memory").rglob("*.tmp"))
        assert residue == []

    def test_history_glob_ignores_temp_names(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        history = tmp_path / "ws" / "memory" / "history"
        (history / ".2026-03-05.md.tmp-999").write_text("partial", encoding="utf-8")
        entries = ms.markdown_snapshot()["history"]
        assert [e["date"] for e in entries] == ["2026-01-01", "2026-02-02", "2026-03-05"]
        assert not any(Path(e["path"]).name.endswith("tmp-999") for e in entries)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_rewrite_preserves_existing_restrictive_mode(self, tmp_path: Path) -> None:
        """A rename-based replace installs the temp file's mode, so without
        carrying the destination's mode over, a user's 0600 memory file would
        silently widen to the umask default on the next write."""
        import stat

        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- v1\n")
        prefs = tmp_path / "ws" / "memory" / "preferences.md"
        prefs.chmod(0o600)
        ms.write_preferences("# User Preferences\n\n- v2\n")
        assert stat.S_IMODE(prefs.stat().st_mode) == 0o600
        assert "- v2" in ms.read_preferences()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_new_file_gets_umask_default_mode(self, tmp_path: Path) -> None:
        import stat

        ms = _store(tmp_path)
        ms.init()  # creates fresh files: no pre-existing mode to preserve
        prefs = tmp_path / "ws" / "memory" / "preferences.md"
        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(prefs.stat().st_mode) == (0o666 & ~umask)


class TestStaleBaselineWriteGuard:
    """Compare-and-swap on the markdown writers: a read-merge-write caller
    (the consolidator) passes the baseline its merge was computed from; a
    write landing after that baseline changed is skipped, not applied — so a
    dashboard Save during the minutes-long consolidation window is never
    silently reverted."""

    def test_write_skipped_when_baseline_is_stale(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- v1\n")
        baseline = ms.read_preferences()
        # A concurrent save lands after the baseline was read.
        ms.write_preferences("# User Preferences\n\n- user edit\n")
        wrote = ms.write_preferences(
            "# User Preferences\n\n- merged from v1\n", expected_baseline=baseline
        )
        assert wrote is False
        assert "- user edit" in ms.read_preferences()

    def test_write_applies_when_baseline_matches(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- v1\n")
        baseline = ms.read_preferences()
        wrote = ms.write_preferences("# User Preferences\n\n- merged\n", expected_baseline=baseline)
        assert wrote is True
        assert "- merged" in ms.read_preferences()

    def test_projects_guard_matches_preferences_semantics(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_projects("# Active Projects\n\n- p1\n")
        baseline = ms.read_projects()
        ms.write_projects("# Active Projects\n\n- user edit\n")
        assert (
            ms.write_projects("# Active Projects\n\n- merged\n", expected_baseline=baseline)
            is False
        )
        assert "- user edit" in ms.read_projects()
        fresh = ms.read_projects()
        assert ms.write_projects("# Active Projects\n\n- merged\n", expected_baseline=fresh) is True

    def test_whitespace_only_edit_is_still_a_stale_baseline(self, tmp_path: Path) -> None:
        """Any byte difference means the merge is stale — a save that only
        adds trailing blank lines must not be reverted by a stripped compare."""
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- v1\n")
        baseline = ms.read_preferences()
        ms.write_preferences("# User Preferences\n\n- v1\n\n\n")  # whitespace-only edit
        wrote = ms.write_preferences(
            "# User Preferences\n\n- merged from v1\n", expected_baseline=baseline
        )
        assert wrote is False
        assert ms.read_preferences() == "# User Preferences\n\n- v1\n\n\n"

    def test_no_baseline_writes_unconditionally(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("- a\n")
        assert ms.write_preferences("- b\n") is True
        assert ms.read_preferences() == "- b\n"


class TestLockFileSymlinkGuard:
    """The advisory lock files live in the agent-writable memory tree, so a
    planted ``.write.lock``/``.append.lock`` symlink must never get its
    target truncated or written through — the lock open is O_NOFOLLOW +
    fstat(regular, nlink==1), and a planted link fails the write closed."""

    def _plant_link(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows CI
            pytest.skip("symlinks not available on this platform")

    def test_planted_write_lock_symlink_fails_closed_target_intact(self, tmp_path: Path) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("precious", encoding="utf-8")
        ms = _store(tmp_path)
        ms.init()
        lock = tmp_path / "ws" / "memory" / ".write.lock"
        self._plant_link(lock, target)
        with pytest.raises(OSError):
            ms.write_preferences("# User Preferences\n\n- attack\n")
        assert target.read_text(encoding="utf-8") == "precious"

    def test_planted_append_lock_symlink_fails_closed_target_intact(self, tmp_path: Path) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("precious", encoding="utf-8")
        ms = _store(tmp_path)
        ms.init()
        lock = tmp_path / "ws" / "memory" / "history" / ".append.lock"
        self._plant_link(lock, target)
        with pytest.raises(OSError):
            ms.append_history("an entry")
        assert target.read_text(encoding="utf-8") == "precious"

    def test_regular_lock_file_reused_across_writes(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("- a\n")
        ms.write_preferences("- b\n")  # second write reuses the existing lock file
        assert ms.read_preferences() == "- b\n"


class TestWriteRootGuard:
    """The write path enforces the same single admission gate as the read
    path: a linked workspace/memory/history directory refuses EVERY writer
    loudly (raise, never a silent no-op), with the target tree untouched."""

    def _link_or_skip(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows CI
            pytest.skip("symlinks not available on this platform")

    def test_linked_memory_dir_refuses_all_writers(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-tree"
        outside.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        self._link_or_skip(ws / "memory", outside)
        ms = MemoryStore(workspace=ws)
        with pytest.raises(OSError):
            ms.write_preferences("- attack\n")
        with pytest.raises(OSError):
            ms.write_projects("- attack\n")
        with pytest.raises(OSError):
            ms.append_history("attack entry")
        with pytest.raises(OSError):
            ms.init()
        assert list(outside.iterdir()) == []  # target tree untouched

    def test_linked_history_dir_refuses_history_writer(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-history"
        outside.mkdir()
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        # Replace the real history dir with a link
        history.rmdir()
        self._link_or_skip(history, outside)
        with pytest.raises(OSError):
            ms.append_history("attack entry")
        assert list(outside.iterdir()) == []

    def test_regular_roots_write_normally(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("- fine\n")
        ms.append_history("fine entry")
        assert "- fine" in ms.read_preferences()

    def test_linked_todays_history_leaf_refuses_append(self, tmp_path: Path) -> None:
        """A link planted at TODAY'S dated name must refuse the append —
        read_text would follow it and the read-modify-write would republish
        the link target's contents into memory (and show/export)."""
        target = tmp_path / "victim.md"
        target.write_text("secret contents", encoding="utf-8")
        ms = _store(tmp_path)
        ms.init()
        today = ms._today_history_file()
        self._link_or_skip(today, target)
        with pytest.raises(OSError):
            ms.append_history("an entry")
        assert target.read_text(encoding="utf-8") == "secret contents"
        assert "secret contents" not in json.dumps(ms.markdown_snapshot())

    def test_hardlinked_todays_history_leaf_refuses_append(self, tmp_path: Path) -> None:
        """A HARDLINK passes the link/junction check (regular inode) but
        reading it republishes the shared inode's contents all the same — an
        existing leaf must be a lone regular inode (st_nlink == 1)."""
        target = tmp_path / "victim-credential.md"
        target.write_text("secret credential", encoding="utf-8")
        ms = _store(tmp_path)
        ms.init()
        today = ms._today_history_file()
        try:
            os.link(target, today)
        except (OSError, NotImplementedError):  # pragma: no cover - FS without hardlinks
            pytest.skip("hardlinks not available on this platform/filesystem")
        with pytest.raises(OSError):
            ms.append_history("an entry")
        assert target.read_text(encoding="utf-8") == "secret credential"
        assert "secret credential" not in json.dumps(ms.markdown_snapshot())

    def test_linked_preferences_leaf_refuses_write(self, tmp_path: Path) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("precious", encoding="utf-8")
        ms = _store(tmp_path)
        ms.init()
        prefs = tmp_path / "ws" / "memory" / "preferences.md"
        prefs.unlink()
        self._link_or_skip(prefs, target)
        with pytest.raises(OSError):
            ms.write_preferences("- attack\n")
        assert target.read_text(encoding="utf-8") == "precious"


class TestEmptyStateContract:
    """Documented contract: a missing OR empty file carries null metadata
    (consumers key incremental sync on updated_at); a dated empty history
    day is retained in enumeration rather than skipped as a refusal."""

    def test_empty_preferences_has_null_updated_at(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        (tmp_path / "ws" / "memory" / "preferences.md").write_text("", encoding="utf-8")
        entry = ms.markdown_snapshot()["preferences"]
        assert entry["content"] == ""
        assert entry["updated_at"] is None

    def test_empty_dated_history_day_is_retained(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        history = tmp_path / "ws" / "memory" / "history"
        (history / "2026-04-04.md").write_text("", encoding="utf-8")
        entries = ms.read_history_entries()
        dates = [e["date"] for e in entries]
        assert "2026-04-04" in dates
        empty_entry = next(e for e in entries if e["date"] == "2026-04-04")
        assert empty_entry["content"] == ""
        assert empty_entry["updated_at"] is None


class TestWorkspaceLinkGuard:
    def test_symlinked_workspace_dir_is_refused(self, tmp_path: Path) -> None:
        """A workspace swapped for a symlink would make every descendant
        check validate paths inside the link's target instead of the
        admitted tree — the root guard must reject the workspace leaf."""
        real_ws = tmp_path / "real-ws"
        (real_ws / "memory" / "history").mkdir(parents=True)
        (real_ws / "memory" / "preferences.md").write_text("# secret\n", encoding="utf-8")
        link_ws = tmp_path / "link-ws"
        try:
            link_ws.symlink_to(real_ws)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows CI
            pytest.skip("symlinks not available on this platform")
        ms = MemoryStore(workspace=link_ws)
        snap = ms.markdown_snapshot()
        assert snap["preferences"]["content"] == ""
        assert snap["history"] == []

    def test_regular_workspace_dir_is_unaffected(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        assert "prefers pytest" in ms.markdown_snapshot()["preferences"]["content"]


class TestBoundedHistoryEnumeration:
    def test_cap_selects_newest_without_materializing_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap applies during enumeration (bounded heap), and still keeps
        the newest days in oldest-first output order."""
        ms = _store(tmp_path)
        history = tmp_path / "ws" / "memory" / "history"
        history.mkdir(parents=True)
        for month in (1, 2, 3, 4, 5):
            (history / f"2026-0{month}-01.md").write_text(
                f"# 2026-0{month}-01\nday\n", encoding="utf-8"
            )
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_ENTRIES", 2)
        entries = ms.read_history_entries()
        assert [e["date"] for e in entries] == ["2026-04-01", "2026-05-01"]


class TestMemoryExportMarkdown:
    def _export_args(self, include_markdown: bool) -> argparse.Namespace:
        return argparse.Namespace(
            mem_action="export", output=None, include_markdown=include_markdown
        )

    def test_export_without_flag_is_byte_identical_to_previous_shape(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The regression guard that matters most: no flag, no shape change."""
        with patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore):
            cli_commands._memory_cmd(self._export_args(include_markdown=False))
        expected = json.dumps({"semantic": [], "episodic": [], "events": []}, indent=2, default=str)
        assert capsys.readouterr().out == expected + "\n"

    def test_export_with_flag_adds_markdown_collection(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _populated_store(tmp_path)
        with (
            patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore),
            patch.object(cli_commands, "_markdown_memory_store", lambda: ms),
        ):
            cli_commands._memory_cmd(self._export_args(include_markdown=True))
        data = json.loads(capsys.readouterr().out)
        assert list(data) == ["semantic", "episodic", "events", "markdown"]
        markdown = data["markdown"]
        assert "- prefers pytest" in markdown["preferences"]["content"]
        assert [e["date"] for e in markdown["history"]] == [
            "2026-01-01",
            "2026-02-02",
            "2026-03-05",
        ]

    def test_export_with_flag_handles_empty_markdown_layer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        with (
            patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore),
            patch.object(cli_commands, "_markdown_memory_store", lambda: ms),
        ):
            cli_commands._memory_cmd(self._export_args(include_markdown=True))
        markdown = json.loads(capsys.readouterr().out)["markdown"]
        assert markdown["preferences"]["content"] == ""
        assert markdown["history"] == []


# ── argparse wiring ──


class TestMemoryImportMarkdownNotice:
    """`memory import` never writes the markdown layer — a payload carrying the
    export-only collection must say so instead of silently dropping it."""

    def _import_args(self, file: str) -> argparse.Namespace:
        return argparse.Namespace(mem_action="import", file=file)

    def test_import_with_markdown_collection_prints_notice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        payload = tmp_path / "export.json"
        payload.write_text(
            json.dumps({"semantic": [], "episodic": [], "markdown": {"history": []}}),
            encoding="utf-8",
        )
        with patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore):
            cli_commands._memory_cmd(self._import_args(str(payload)))
        out = capsys.readouterr().out
        assert "export-only" in out and "NOT imported" in out

    def test_import_without_markdown_collection_prints_no_notice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        payload = tmp_path / "export.json"
        payload.write_text(json.dumps({"semantic": [], "episodic": []}), encoding="utf-8")
        with patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore):
            cli_commands._memory_cmd(self._import_args(str(payload)))
        assert "export-only" not in capsys.readouterr().out


class TestMemoryCliWiring:
    def test_memory_show_arguments_parse(self) -> None:
        argv = [
            "kirocrew",
            "memory",
            "show",
            "history",
            "--format",
            "json",
            "--since",
            "2026-01-01",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("kiro_crew.cli_commands._memory_cmd") as mock_cmd,
        ):
            from kiro_crew.cli import main

            main()
        ns = mock_cmd.call_args[0][0]
        assert (ns.mem_action, ns.target, ns.format, ns.since) == (
            "show",
            "history",
            "json",
            "2026-01-01",
        )

    def test_memory_show_target_optional_defaults(self) -> None:
        with (
            patch.object(sys, "argv", ["kirocrew", "memory", "show"]),
            patch("kiro_crew.cli_commands._memory_cmd") as mock_cmd,
        ):
            from kiro_crew.cli import main

            main()
        ns = mock_cmd.call_args[0][0]
        assert (ns.target, ns.format, ns.since) == (None, "md", None)

    def test_memory_export_include_markdown_defaults_off(self) -> None:
        with (
            patch.object(sys, "argv", ["kirocrew", "memory", "export"]),
            patch("kiro_crew.cli_commands._memory_cmd") as mock_cmd,
        ):
            from kiro_crew.cli import main

            main()
        assert mock_cmd.call_args[0][0].include_markdown is False


# ── memory search: the markdown layer is reachable by keyword ──
# The FTS5 index over preferences/projects/daily-history is written on every
# append and rebuilt by both the heartbeat and the gateway, but nothing ever
# queried it, and `memory search` resolved to the VECTOR store. These pin the
# markdown layer as searchable while keeping the old output shape available.


class _NoEpisodicVectorStore:
    """Vector store with exactly the surface ``_memory_cmd`` SEARCH uses.

    Deliberately separate from ``_EmptyVectorStore``, whose docstring pins it to
    the export surface: widening that stub would make it lie about what it
    stands in for.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def init(self) -> None:
        pass

    def close(self) -> None:
        pass

    def search_episodic(self, **_kwargs: object) -> list:
        return []


class _OneEpisodicVectorStore(_NoEpisodicVectorStore):
    """Returns a single episodic hit, so the section label is observable."""

    def search_episodic(self, **_kwargs: object) -> list:
        return [{"text": "shipped the pytest refactor", "importance": 0.7, "tags": "[]"}]


def _search_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {"mem_action": "search", "query": "", "layer": "all"}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestMemoryHistorySearch:
    def test_a_word_written_months_ago_is_findable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The point of the feature: recall by content, not by recency window."""
        ms = _populated_store(tmp_path)
        ms.rebuild_index()
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_search_args(query="pytest", layer="history"))
        out = capsys.readouterr().out
        assert "preferences" in out
        assert "pytest" in out

    def test_history_layer_never_opens_the_vector_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """No embedder, no vector DB creation — same contract as ``show``."""
        ms = _populated_store(tmp_path)
        ms.rebuild_index()

        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError("history search must not construct a vector store")

        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with patch.object(cli_commands, "VectorMemoryStore", _boom):
                cli_commands._memory_cmd(_search_args(query="pytest", layer="history"))
        assert "pytest" in capsys.readouterr().out

    def test_no_match_says_so_rather_than_printing_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _populated_store(tmp_path)
        ms.rebuild_index()
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_search_args(query="zzzznevermentioned", layer="history"))
        assert "No memory-history matches." in capsys.readouterr().out

    def test_layer_vector_keeps_the_previous_output_exactly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Back-compat guard: --layer vector must not gain a history section."""
        ms = _populated_store(tmp_path)
        ms.rebuild_index()
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with patch.object(cli_commands, "VectorMemoryStore", _NoEpisodicVectorStore):
                cli_commands._memory_cmd(_search_args(query="pytest", layer="vector"))
        out = capsys.readouterr().out
        assert "No episodic memories found." in out
        assert "Daily history" not in out
        assert "Episodic recall" not in out

    def test_both_sections_are_labelled_when_both_are_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Unlabelled, the first block of hits reads as the whole answer."""
        ms = _populated_store(tmp_path)
        ms.rebuild_index()
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with patch.object(cli_commands, "VectorMemoryStore", _OneEpisodicVectorStore):
                cli_commands._memory_cmd(_search_args(query="pytest", layer="all"))
        out = capsys.readouterr().out
        assert "Episodic recall" in out
        assert "Daily history" in out

    def test_layer_vector_stays_unlabelled_even_with_hits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Back-compat: the single-section output keeps its previous shape."""
        ms = _populated_store(tmp_path)
        ms.rebuild_index()
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with patch.object(cli_commands, "VectorMemoryStore", _OneEpisodicVectorStore):
                cli_commands._memory_cmd(_search_args(query="pytest", layer="vector"))
        out = capsys.readouterr().out
        assert "shipped the pytest refactor" in out
        assert "Episodic recall" not in out

    def test_all_layer_still_reaches_history_when_vector_is_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """An empty vector result is not an empty answer under the default."""
        ms = _populated_store(tmp_path)
        ms.rebuild_index()
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with patch.object(cli_commands, "VectorMemoryStore", _NoEpisodicVectorStore):
                cli_commands._memory_cmd(_search_args(query="pytest", layer="all"))
        out = capsys.readouterr().out
        assert "No episodic memories found." in out
        assert "pytest" in out


class TestLiteralFtsQuery:
    """The words a user types are text, not FTS5 expression syntax.

    Before escaping, ``PROJ-123`` raised inside the sqlite driver and
    ``MemoryStore.search``'s ``except`` turned it into ``[]``. To a model that
    reads as "the user never wrote about this", which is the wrong answer for
    one of the likeliest queries: a ticket id, a filename, a hyphenated term.
    """

    def _indexed(self, tmp_path: Path) -> MemoryStore:
        ms = _store(tmp_path)
        ms.init()
        ms.write_projects("# Active Projects\n\n- shipped PROJ-123 via cli_commands.py\n")
        ms.rebuild_index()
        return ms

    @pytest.mark.parametrize("query", ["PROJ-123", "cli_commands.py", "PROJ-123 cli_commands.py"])
    def test_syntax_bearing_queries_match_instead_of_silently_missing(
        self, tmp_path: Path, query: str
    ) -> None:
        assert self._indexed(tmp_path).search(query), f"{query!r} found nothing"

    def test_a_genuine_miss_is_still_a_miss(self, tmp_path: Path) -> None:
        assert self._indexed(tmp_path).search("zzzznevermentioned") == []

    def test_a_bare_operator_is_matched_as_a_word_not_parsed(self, tmp_path: Path) -> None:
        """``shipped AND`` is invalid FTS5; as literal tokens it is simply absent."""
        assert self._indexed(tmp_path).search("shipped AND") == []

    def test_a_whitespace_only_query_yields_no_match(self, tmp_path: Path) -> None:
        assert self._indexed(tmp_path).search("   ") == []

    def test_an_embedded_quote_is_escaped_not_injected(self) -> None:
        from kiro_crew.memory import _fts5_literal_query

        assert _fts5_literal_query('a "b"') == '"a" """b"""'


class TestOneEscapingDialect:
    """Both FTS5 readers escape through the same primitive.

    Design review's point: a second hand-rolled quoter is how the tree ends up
    with divergent dialects and one of them wrong again.
    """

    def test_memory_and_knowledge_share_the_quoting_primitive(self) -> None:
        from kiro_crew._sqlite_compat import fts5_quote_tokens

        assert fts5_quote_tokens("PROJ-123 hooks.py") == ['"PROJ-123"', '"hooks.py"']

    def test_the_join_differs_on_purpose(self) -> None:
        """Memory ANDs a deliberate query; knowledge ORs for recall."""
        from kiro_crew.knowledge.retrieval import HybridRetriever
        from kiro_crew.memory import _fts5_literal_query

        assert _fts5_literal_query("alpha beta") == '"alpha" "beta"'
        assert HybridRetriever._sanitize_fts5_query("alpha beta") == '"alpha" OR "beta"'


class TestEmptyIndexIsNotAbsence:
    """An unreadable or unbuilt index must not be reported as "never written"."""

    def test_row_count_distinguishes_empty_from_populated(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        assert ms.index_row_count() == 0
        ms.write_projects("# Active Projects\n\n- PROJ-123\n")
        ms.rebuild_index()
        assert (ms.index_row_count() or 0) > 0

    def test_an_unreadable_index_reports_none_not_zero(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("corrupt index")

        with patch.object(type(ms), "_get_db", _boom):
            assert ms.index_row_count() is None

    def test_an_unbuilt_index_is_reported_as_such_not_as_no_match(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        ms.init()  # initialised but never indexed
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_search_args(query="pytest", layer="history"))
        out = capsys.readouterr().out
        assert "empty or unavailable" in out
        assert "No memory-history matches." not in out


class TestLinkedWorkspaceAncestorGate:
    """On Windows, a linked ANCESTOR of the workspace must be refused before
    the leaf reparse-point checks -- those are lstats that resolve every
    ancestor, so the probe itself would traverse the link and open the SMB
    connection the lexical UNC gate exists to prevent (#5962)."""

    def _windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import types

        from kiro_crew import memory as memory_mod

        # Patch ONLY memory.py's view of os (same rationale as the UNC gate
        # tests above): patching the global os.name would make pathlib
        # dispatch WindowsPath everywhere on a POSIX test host. lstat is
        # carried so the read path's updated_at branch cannot surface a stub
        # AttributeError masquerading as a product bug.
        monkeypatch.setattr(memory_mod, "os", types.SimpleNamespace(name="nt", lstat=os.lstat))

    def test_linked_ancestor_is_refused_before_any_leaf_lstat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering IS the property: the leaf predicate is wired to explode,
        so a regression that lstats first fails loudly instead of silently."""
        from kiro_crew import memory as memory_mod

        ms = _populated_store(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(memory_mod, "first_linked_ancestor", lambda _p: str(tmp_path))

        def _boom(_p: object) -> bool:  # pragma: no cover
            raise AssertionError("leaf reparse check ran before the ancestor walk")

        monkeypatch.setattr(memory_mod, "is_link_or_junction", _boom)
        audits: list[tuple[str, str]] = []
        monkeypatch.setattr(
            ms,
            "_audit_read_refusal",
            lambda rule, path, reason: audits.append((rule, reason)),
        )

        snapshot = ms.markdown_snapshot()

        assert snapshot["preferences"]["content"] == ""
        assert snapshot["history"] == []
        # The guard is consulted once per read surface (preferences, projects,
        # history) and the refusal short-circuits before any per-entry work,
        # so the multiplicity is bounded by the surface count -- a regression
        # to per-entry consultation would blow this bound out.
        assert len(audits) == 3
        assert set(audits) == {("workspace_linked_ancestor", "a workspace ancestor is a link")}

    def test_bypassing_the_guard_restores_the_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation check: with the walk reporting no link, the same store
        reads again -- the refusal above is attributable to the guard."""
        from kiro_crew import memory as memory_mod

        ms = _populated_store(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(memory_mod, "first_linked_ancestor", lambda _p: None)

        snapshot = ms.markdown_snapshot()

        assert "- prefers pytest" in snapshot["preferences"]["content"]

    def test_the_walk_is_not_consulted_on_posix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX linked ancestors stay deliberately unrejected (a symlinked
        /home is a legitimate setup and those components are not
        agent-writable), so the walk must not even run there."""
        if os.name == "nt":
            pytest.skip("gate is active on Windows by design")
        from kiro_crew import memory as memory_mod

        def _boom(_p: object) -> None:  # pragma: no cover
            raise AssertionError("ancestor walk ran on POSIX")

        monkeypatch.setattr(memory_mod, "first_linked_ancestor", _boom)
        ms = _populated_store(tmp_path)
        assert "- prefers pytest" in ms.markdown_snapshot()["preferences"]["content"]

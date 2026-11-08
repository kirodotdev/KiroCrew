"""Tests for a rebuild that cannot read an existing ``kirocrew.json``.

The rebuild keeps the user's spec and skips the cycle instead of writing defaults over
it, and a rebuild that writes the spec again closes the episode: the skip and its
sibling pass, the hold that has the maintenance wake retry it, and the readers that
report the skipped cycle. Starts are not gated on the episode.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from agent_helpers import _run_install
from mcp_merge_helpers import bundled_defaults as _bundled_defaults
from unreadable_spec_helpers import custom_spec

from kiro_crew import atomic_write as aw


class TestUnreadableMainSpec:
    """The unreadable-main-spec episode, end to end through ``install_agent``."""

    _custom_spec = staticmethod(custom_spec)

    @pytest.mark.parametrize(
        "error",
        [
            PermissionError(13, "Permission denied"),
            OSError(5, "Input/output error"),
        ],
        ids=["eacces", "eio"],
    )
    def test_unreadable_existing_spec_is_kept_and_the_cycle_skipped(
        self, tmp_path: Path, caplog, held_flag, error
    ):
        """A present spec whose read fails is left byte-for-byte, never reset.

        The read failure says nothing about the file's contents, so the rebuild
        writes neither the main spec nor the two specs mirrored from it. The
        steps that re-filter other files (service and conductor specs, forks,
        hook repair) still run. It reports the skip as an unwritten, held
        rebuild so every retry path (the ceiling memo, the boot baseline, the
        maintenance wake) runs again.

        Regression pin for main's behaviour: reading a present-but-unreadable
        spec as missing (``_read_existing_spec`` returning ``None`` and
        ``mcp_aliases._durable_tool_aliases`` returning ``(False, None)`` on an
        ``OSError``, as main does) writes defaults over the file and this goes red.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        before = spec.read_bytes()

        def _failing_read(path, **_kwargs):
            if Path(path) == spec:
                raise error
            return aw.read_bytes_with_retry(path, **_kwargs)

        wrote_out: list[bool] = []
        held_out: list[bool] = []
        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _failing_read),
            patch("kiro_crew.agent.worker_agent._install_worker_agent") as worker,
            patch("kiro_crew.agent._install_heartbeat_agent") as heartbeat,
            patch("kiro_crew.agent.service_agents._install_knowledge_agent") as knowledge,
            patch(
                "kiro_crew.agent.conductor_agents._install_conductor_agent", return_value=True
            ) as conductor,
            patch("kiro_crew.agent.fork_refresh.refresh_after_rebuild") as forks,
            patch("kiro_crew.agent.repair_agent_configs") as repair,
            caplog.at_level(logging.WARNING, logger="kiro_crew.agent"),
        ):
            path = _run_install(
                tmp_path,
                cfg_dir,
                refresh_forks="defer",
                _wrote_out=wrote_out,
                _held_out=held_out,
            )

        assert path == spec
        assert spec.read_bytes() == before
        assert wrote_out == [False]
        assert held_out == [True]
        # The fork refresh is what the fork spawn gate waits on and records its
        # failures for, so skipping it would leave forks on stale grants behind
        # an open gate. It runs with the caller's own mode.
        assert forks.call_args.args[0] == "defer"
        assert held_flag._conductor_spec_held is True
        # The two specs mirrored from the main spec are left alone ...
        worker.assert_not_called()
        heartbeat.assert_not_called()
        # ... and every step that re-filters a different file still runs, so a
        # moved ceiling reaches the forks and conductor specs this cycle.
        knowledge.assert_called_once_with()
        conductor.assert_called_once()
        forks.assert_called_once()
        repair.assert_called_once_with()
        assert any(
            r.levelno == logging.WARNING and str(spec) in r.getMessage() for r in caplog.records
        )

    def test_a_readable_spec_after_a_skip_rebuilds_and_clears_the_hold(
        self, tmp_path: Path, held_flag
    ):
        """The next rebuild that can read the file writes it with the user's fields."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **_kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **_kwargs)

        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir)
        assert held_flag._conductor_spec_held is True

        wrote_out: list[bool] = []
        _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out)

        assert wrote_out == [True]
        assert held_flag._conductor_spec_held is False
        config = json.loads(spec.read_text(encoding="utf-8"))
        assert config["model"] == "claude-user-custom"
        assert "user-server" in config["mcpServers"]

    def test_a_sharing_violation_is_retried_before_the_spec_counts_as_unreadable(
        self, tmp_path: Path, monkeypatch, held_flag
    ):
        """One Windows sharing violation is waited out; the rebuild then reads and writes."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
        real_read = aw._read_bytes
        attempts = [0]

        def _locked_once(target: Path, max_bytes):
            if target == spec:
                attempts[0] += 1
                if attempts[0] == 1:
                    raise PermissionError(13, "The process cannot access the file", str(target))
            return real_read(target, max_bytes)

        def _windows_read(path, **kwargs):
            # The retry only sleeps over a sharing violation on Windows. Scoped
            # to this one read, so the rest of the rebuild keeps the host's paths.
            with patch.object(aw.platform_compat, "IS_WINDOWS", True):
                return aw.read_bytes_with_retry(path, **kwargs)

        wrote_out: list[bool] = []
        with (
            patch.object(aw, "_read_bytes", _locked_once),
            patch("kiro_crew.agent.read_bytes_with_retry", _windows_read),
        ):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out)

        # The first read failed and was retried; later reads of the spec in the
        # same rebuild (the load, the alias reconcile) succeed.
        assert attempts[0] >= 2
        assert wrote_out == [True]
        assert held_flag._conductor_spec_held is False
        config = json.loads(spec.read_text(encoding="utf-8"))
        assert config["model"] == "claude-user-custom"
        assert "user-server" in config["mcpServers"]

    def test_a_skipped_cycle_runs_no_migration_over_other_specs(self, tmp_path: Path, held_flag):
        """The probe runs before the self-heal passes, so a skip leaves other specs alone."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        sibling = spec.parent / "user-other.json"
        sibling.write_text(
            json.dumps({"name": "user-other", "model_managed": True}), encoding="utf-8"
        )
        before = sibling.read_bytes()

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        wrote_out: list[bool] = []
        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out)

        assert wrote_out == [False]
        assert sibling.read_bytes() == before

    def _skipped(self, tmp_path: Path, held_flag):
        """Run one rebuild over a ``kirocrew.json`` whose read is denied: the gate is armed."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir)
        assert held_flag._main_spec_unreadable is True
        return cfg_dir, spec, _denied

    def test_an_open_episode_changes_no_start_decision(self, tmp_path: Path, held_flag):
        """While the spec stays unreadable, starts are decided exactly as with no episode.

        Keeping the file is the whole change: the start gate refuses nothing new
        on the episode's account. The main agent, its mirrors, another installed
        agent and a name no file declares each get the same answer from
        ``require_fork_governance`` with the episode open as with it closed, on
        the same unreadable file. Mutation pin: refusing the main agent or its
        mirrors in ``require_fork_governance`` while the episode is open goes red.
        """
        _cfg_dir, spec, denied = self._skipped(tmp_path, held_flag)

        def _outcome(name: str) -> str | None:
            try:
                held_flag.require_fork_governance(name)
            except Exception as exc:  # the answer itself is what is compared
                return f"{type(exc).__name__}: {exc}"
            return None

        names = ("kirocrew", "kirocrew-heartbeat", "kirocrew-worker", "kirocrew-conductor", "nope")
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.read_bytes_with_retry", denied),
        ):
            during = {name: _outcome(name) for name in names}
            held_flag._main_spec_unreadable = False
            after = {name: _outcome(name) for name in names}
            held_flag._main_spec_unreadable = True
        assert during == after
        assert during["kirocrew"] is None, "the main agent starts as it would on main"

    def test_a_tightened_ceiling_reaches_the_kept_mirrors_in_place(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """While ``kirocrew.json`` is unreadable, the mirrors' grants still go through the ceiling.

        The skip cannot re-derive the heartbeat and worker specs from a main spec
        it cannot read, so it re-filters what is on disk: a grant the tightened
        ceiling withholds leaves ``allowedTools`` and every server's
        ``autoApprove``, a grant it still allows stays, no server is added, and
        ``kirocrew.json`` is left byte for byte. Mutation pin: skipping the
        in-place re-filter (the mirrors untouched while the main spec is
        unreadable) leaves the withheld grants on both specs and this goes red.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        _run_install(tmp_path, cfg_dir)
        worker_path = spec.parent / "kirocrew-worker.json"
        heartbeat_path = spec.parent / "kirocrew-heartbeat.json"
        # Grants an earlier, looser ceiling left on the two mirrors.
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        worker["allowedTools"].append("@kirocrew-core/learn_list")
        worker["mcpServers"]["kirocrew-core"]["autoApprove"] = ["learn_list"]
        worker["mcpServers"]["user-server"]["autoApprove"] = ["user_tool"]
        worker_path.write_text(json.dumps(worker), encoding="utf-8")
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        heartbeat["mcpServers"]["kirocrew-core"]["autoApprove"] = ["learn_list"]
        heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
        main_before = spec.read_bytes()

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        # The tightened ceiling: kirocrew-core must go through the gate.
        def _ceiling(ref: str) -> bool:
            return not ref.startswith("@kirocrew-core")

        from kiro_crew.platform import governance

        monkeypatch.setattr(held_flag, "_may_auto_approve", _ceiling)
        monkeypatch.setattr(governance, "may_skip_gate_now", _ceiling)
        wrote_out: list[bool] = []
        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out)

        assert wrote_out == [False]
        assert held_flag._main_spec_unreadable is True
        assert spec.read_bytes() == main_before
        worker_after = json.loads(worker_path.read_text(encoding="utf-8"))
        heartbeat_after = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        assert "@kirocrew-core/learn_list" not in worker_after["allowedTools"]
        assert "@kirocrew-work/work_brief" in worker_after["allowedTools"]
        assert "autoApprove" not in worker_after["mcpServers"]["kirocrew-core"]
        assert worker_after["mcpServers"]["user-server"]["autoApprove"] == ["user_tool"]
        assert "autoApprove" not in heartbeat_after["mcpServers"]["kirocrew-core"]
        # Nothing is copied in from the unreadable main spec.
        assert set(worker_after["mcpServers"]) == set(worker["mcpServers"])
        assert set(heartbeat_after["mcpServers"]) == set(heartbeat["mcpServers"])

    def test_a_mirror_re_filter_that_fails_keeps_the_sibling_pass_owed(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """A kept mirror whose re-filter could not be written keeps the sibling pass owed.

        A later skip under a recorded generation runs no pass unless the last one
        held, and then only after ``HELD_SIBLING_PASS_RETRY_SECS``. So a re-filter
        that failed must count as held, or the tightened grants it could not
        remove would never be retried. Mutation pin: ignoring the re-filter's
        result leaves no held time and this goes red.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        _run_install(tmp_path, cfg_dir)
        worker_path = spec.parent / "kirocrew-worker.json"
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        worker["allowedTools"].append("@kirocrew-core/learn_list")
        worker_path.write_text(json.dumps(worker), encoding="utf-8")

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        real_write = held_flag._atomic_json_write

        def _write(path, data, *args, **kwargs):
            if Path(path) == worker_path:
                raise OSError(5, "Input/output error", str(path))
            return real_write(path, data, *args, **kwargs)

        monkeypatch.setattr(
            held_flag, "_may_auto_approve", lambda ref: ref != "@kirocrew-core/learn_list"
        )
        monkeypatch.setattr(held_flag, "_atomic_json_write", _write)
        held_out: list[bool] = []
        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir, refresh_forks=False, _held_out=held_out)

        assert held_out == [True]
        assert (
            "@kirocrew-core/learn_list"
            in json.loads(worker_path.read_text(encoding="utf-8"))["allowedTools"]
        )
        assert held_flag._main_spec_held_pass_at is not None, "a failed re-filter was not held"

    def test_a_skip_right_after_a_close_warns_for_its_own_episode(
        self, tmp_path: Path, held_flag, monkeypatch, caplog
    ):
        """A new episode never inherits the closed one's "already warned" state.

        The close clears that state in the critical section that clears the
        flag. Here a skip on another thread opens a new episode the moment the
        close releases the episode lock, with the same errno; its first warning
        must still be a WARNING. Mutation pin: clearing the warning state after
        the lock is released (or keeping it in the shared-home set) leaves the
        old key in place, and the new episode's opening warning drops to debug.
        """
        _cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)
        monkeypatch.setattr(held_flag, "_install_sibling_specs", lambda *a, **k: False)
        real_lock = held_flag._main_spec_episode_lock
        reopen = [False]
        cause = PermissionError(13, "Permission denied", str(spec))

        class _ReopenOnRelease:
            def __enter__(self):
                return real_lock.__enter__()

            def __exit__(self, *exc):
                real_lock.__exit__(*exc)
                if reopen[0]:
                    reopen[0] = False
                    unreadable = held_flag._AgentSpecUnreadable(f"PermissionError: {cause}")
                    unreadable.__cause__ = cause
                    held_flag._skip_unreadable_main_spec(
                        spec, unreadable, "defer", clean=False, wrote_out=None, held_out=None
                    )
                return False

        monkeypatch.setattr(held_flag, "_main_spec_episode_lock", _ReopenOnRelease())
        caplog.set_level(logging.DEBUG, logger="kiro_crew.agent")
        caplog.clear()
        reopen[0] = True
        held_flag._note_main_spec_readable(spec, held_flag._main_spec_episode_revision)
        assert not reopen[0], "the close never released the episode lock"
        opening = [
            r.levelno
            for r in caplog.records
            if r.getMessage().startswith("cannot read the existing agent spec")
        ]
        assert opening == [logging.WARNING]
        assert held_flag._main_spec_unreadable is True

    def test_every_episode_warning_operation_holds_its_lock(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """The episode's warning state changes only under the episode lock.

        The lock is replaced by one that records whether it is held, and the set
        by one that notes any operation made while it is not. The skip, a failed
        retry and the close each touch the episode's state. Mutation pin:
        removing the episode lock around any of the three goes red.
        """

        class _Recording:
            def __init__(self, real) -> None:
                self.real = real
                self.held = False

            def __enter__(self):
                self.real.acquire()
                self.held = True
                return self

            def __exit__(self, *exc):
                self.held = False
                self.real.release()
                return False

        class _Guarded(set):
            def __init__(self, lock: _Recording) -> None:
                super().__init__()
                self.lock = lock
                self.ops: list[tuple[str, bool]] = []

            def _note(self, op: str) -> None:
                self.ops.append((op, self.lock.held))

            def __contains__(self, item) -> bool:
                self._note("in")
                return set.__contains__(self, item)

            def add(self, item) -> None:
                self._note("add")
                set.add(self, item)

            def clear(self) -> None:
                self._note("clear")
                set.clear(self)

        episode_lock = _Recording(threading.Lock())
        episode_set = _Guarded(episode_lock)
        monkeypatch.setattr(held_flag, "_main_spec_episode_lock", episode_lock)
        monkeypatch.setattr(held_flag, "_main_spec_warned", episode_set)
        cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
                assert held_flag.retry_held_conductor_specs() is False
            assert set(episode_set) >= {"read:13", "retry"}
            assert held_flag.retry_held_conductor_specs() is True
        assert not episode_set
        assert {op for op, _held in episode_set.ops} >= {"in", "add", "clear"}
        assert all(held for _op, held in episode_set.ops), episode_set.ops

    def test_a_retry_that_still_cannot_read_warns_once_per_episode(
        self, tmp_path: Path, held_flag, monkeypatch, caplog
    ):
        """The maintenance retry logs one WARNING an episode, then debug.

        Closing the episode clears the key, so the next episode warns again.
        Mutation pin: a plain ``logger.warning`` logs it on every retry.
        """
        cfg_dir, spec, _denied = self._skipped(tmp_path, held_flag)

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        def _retry_warnings() -> int:
            return sum(
                "still cannot be read after the retry" in r.getMessage()
                for r in caplog.records
                if r.levelno == logging.WARNING
            )

        caplog.set_level(logging.DEBUG, logger="kiro_crew.agent")
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
                for _ in range(3):
                    assert held_flag.retry_held_conductor_specs() is False
            assert _retry_warnings() == 1
            assert held_flag.retry_held_conductor_specs() is True
            with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
                _run_install(tmp_path, cfg_dir)
                assert held_flag.retry_held_conductor_specs() is False
        assert _retry_warnings() == 2

    def test_the_retry_and_the_baseline_read_an_open_episode_as_a_hold(
        self, tmp_path: Path, held_flag
    ):
        """An open episode is a hold on its own, whatever the conductor memo says.

        Mutation pins: a retry keyed on ``_conductor_spec_held`` alone returns
        without rebuilding, so the kept spec is never rewritten; a baseline keyed
        on it alone seeds a generation the unread spec never projected.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        held_flag._main_spec_unreadable = True
        held_flag._conductor_spec_held = False
        held_flag.prime_ceiling_projection()
        assert held_flag._projected_ceiling_generation is None

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            assert held_flag.retry_held_conductor_specs() is True
        assert held_flag._main_spec_unreadable is False

    def test_an_older_write_never_closes_an_episode_a_newer_skip_opened(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """GPT's race: a skip that lands while a rebuild writes is a newer episode.

        Rebuild A reads the spec and writes it. Between its write and the point it
        closes the episode, rebuild B's read fails and B skips (a profile tightening
        that reads the file A is replacing). A's write never read what B failed on, so
        A must leave the episode open and held; the maintenance retry's rebuild reads
        the recovered file and closes it. Mutation pin: closing unconditionally clears
        the newer episode and drops its hold, so the kept spec is never re-projected.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        real_note = held_flag._note_main_spec_readable
        landed = [False]

        def _note_after_a_skip_lands(*args, **kwargs):
            # A has written the spec and is about to close the episode.
            if not landed[0]:
                landed[0] = True
                held_flag._skip_unreadable_main_spec(
                    spec,
                    held_flag._AgentSpecUnreadable("OSError: EIO"),
                    "defer",
                    clean=False,
                    wrote_out=None,
                    held_out=None,
                )
            return real_note(*args, **kwargs)

        wrote: list[bool] = []
        with patch("kiro_crew.agent._note_main_spec_readable", _note_after_a_skip_lands):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
        assert wrote == [True]
        assert held_flag._main_spec_unreadable is True, "an older write closed a newer episode"
        assert held_flag._spec_hold_open(), "the newer episode lost its hold"

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            assert held_flag.retry_held_conductor_specs() is True
        assert held_flag._main_spec_unreadable is False

    def test_an_overlapping_skip_leaves_no_episode_the_retry_cannot_close(
        self, tmp_path: Path, held_flag
    ):
        """The independent review's repro: a skip that lands inside a writing rebuild.

        Rebuild A writes ``kirocrew.json`` and clears the flag, and while its
        sibling pass runs, rebuild B's read fails and B skips. A then settles the
        conductor memo to its own verdict. That settle may not drop B's hold, so
        the hourly wake still rebuilds once the file reads.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        injected = [False]

        def _conductor_install(*_args, **_kwargs):
            if not injected[0]:
                injected[0] = True
                assert held_flag._main_spec_unreadable is False  # A already cleared it
                held_flag._skip_unreadable_main_spec(
                    spec,
                    held_flag._AgentSpecUnreadable("PermissionError: sharing violation"),
                    "defer",
                    clean=False,
                    wrote_out=None,
                    held_out=None,
                )
            return True  # A's own conductor write succeeded

        wrote: list[bool] = []
        with patch("kiro_crew.agent.conductor_agents._install_conductor_agent", _conductor_install):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
        assert wrote == [True]
        assert held_flag._main_spec_unreadable is True
        assert held_flag._conductor_spec_held is True

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            assert not (
                held_flag._main_spec_unreadable and not held_flag._conductor_spec_held
            ), "episode open but nothing held: retry_held_conductor_specs will never rebuild"
            assert held_flag.retry_held_conductor_specs() is True
        assert held_flag._main_spec_unreadable is False

    @pytest.mark.parametrize(
        "skip_lands",
        [
            "before_the_write",
            "after_the_write",
            "before_the_settle",
            "after_the_settle",
            "after_the_rebuild",
            "around_the_rebuild",
        ],
    )
    def test_overlapping_rebuilds_never_leave_an_episode_without_a_retry(
        self, tmp_path: Path, held_flag, monkeypatch, skip_lands
    ):
        """One writing rebuild and one skipping rebuild, overlapped in every order.

        *skip_lands* places the failed read's skip inside the writing rebuild,
        between each pair of its flag writes, after it, or the other way round:
        the whole writing rebuild inside the skip's own sibling pass. Whatever the
        order, an open episode keeps the hold, and the maintenance retry closes
        the episode once the file reads. Mutation
        pin: a settle that drops the hold while the episode is open goes red.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        fired = [False]

        def _skip() -> None:
            held_flag._skip_unreadable_main_spec(
                spec,
                held_flag._AgentSpecUnreadable("PermissionError: sharing violation"),
                "defer",
                clean=False,
                wrote_out=None,
                held_out=None,
            )

        def _write() -> None:
            wrote: list[bool] = []
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
            assert wrote == [True]

        def _at(point: str, real):
            def _step(*args, **kwargs):
                if skip_lands == point and not fired[0]:
                    fired[0] = True
                    _skip()
                return real(*args, **kwargs)

            return _step

        real_conductor = held_flag.conductor_agents._install_conductor_agent
        real_read = held_flag._read_existing_spec

        def _around(*args, **kwargs):
            # The skip's own sibling pass runs first here, and the whole writing
            # rebuild lands inside it, before the skip's last flag write.
            if skip_lands == "around_the_rebuild" and not fired[0]:
                fired[0] = True
                _write()
            return real_conductor(*args, **kwargs)

        failed_reads = [skip_lands == "around_the_rebuild"]

        def _read_once_failing(path):
            if failed_reads[0]:
                failed_reads[0] = False
                raise held_flag._AgentSpecUnreadable("PermissionError: sharing violation")
            return real_read(path)

        with (
            patch(
                "kiro_crew.agent.migrate_agent_specs",
                _at("before_the_write", held_flag.migrate_agent_specs),
            ),
            patch(
                "kiro_crew.agent._install_aim_capabilities",
                _at("after_the_write", held_flag._install_aim_capabilities),
            ),
            patch(
                "kiro_crew.agent.conductor_agents._install_conductor_agent",
                _at("before_the_settle", _around),
            ),
            patch(
                "kiro_crew.agent.repair_agent_configs",
                _at("after_the_settle", held_flag.repair_agent_configs),
            ),
            patch(
                "kiro_crew.agent._invalidate_projection_if_answer_moved",
                _at("after_the_rebuild", held_flag._invalidate_projection_if_answer_moved),
            ),
            patch("kiro_crew.agent._read_existing_spec", _read_once_failing),
        ):
            if skip_lands == "around_the_rebuild":
                # The skipping rebuild, whose sibling pass runs the writing one.
                skipped: list[bool] = []
                _run_install(tmp_path, cfg_dir, _wrote_out=skipped)
                assert skipped == [False]
            else:
                _write()
        assert fired[0], "the skip never landed"

        if held_flag._main_spec_unreadable:
            assert held_flag._conductor_spec_held is True, "an open episode lost its hold"

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
        ):
            assert held_flag.retry_held_conductor_specs() is True
        assert held_flag._main_spec_unreadable is False
        assert held_flag._conductor_spec_held is False

    def test_repeat_skips_run_the_sibling_pass_once_per_governance_generation(
        self, tmp_path: Path, held_flag
    ):
        """The config watcher retries every tick: the sibling pass runs once per generation.

        It runs on the first skip of an episode, again only when
        ``governance_answer_generation`` moved, and again on the first skip of
        the next episode. Mutation pins: running it on every skip makes the tick
        count 5, and never re-running it misses the moved generation.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        denied = [True]
        generation = [10]

        def _failing(path, **kwargs):
            if Path(path) == spec and denied[0]:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        real_pass = held_flag._install_sibling_specs
        passes: list[bool] = []

        def _counting(*args, **kwargs):
            passes.append(kwargs.get("mirror_main", True))
            return real_pass(*args, **kwargs)

        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _failing),
            patch("kiro_crew.agent._install_sibling_specs", _counting),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: generation[0],
            ),
        ):
            for _tick in range(5):
                wrote: list[bool] = []
                held: list[bool] = []
                _run_install(tmp_path, cfg_dir, _wrote_out=wrote, _held_out=held)
                # Every skip still reports itself as an unwritten, held rebuild.
                assert (wrote, held) == ([False], [True])
                assert held_flag._conductor_spec_held is True
            assert passes == [False]
            generation[0] = 11
            _run_install(tmp_path, cfg_dir)
            _run_install(tmp_path, cfg_dir)
            assert passes == [False, False]
            # Recovery writes the spec and runs the full pass; the next episode
            # starts over with its own first pass.
            denied[0] = False
            _run_install(tmp_path, cfg_dir)
            assert passes == [False, False, True]
            assert held_flag._main_spec_skip_generation is None
            denied[0] = True
            _run_install(tmp_path, cfg_dir)
            assert passes == [False, False, True, False]

    @pytest.mark.parametrize("failure", ["held", "raises"])
    def test_a_held_sibling_pass_is_retried_under_the_same_generation(
        self, tmp_path: Path, held_flag, failure
    ):
        """A held or raising conductor write is retried under the same generation.

        The hourly retry, which comes after ``HELD_SIBLING_PASS_RETRY_SECS``,
        re-runs the pass under the same generation until one completes, so a
        tightened ceiling still reaches the conductor spec. A skip before that
        (the config watcher's 2-second retry) runs no pass. Mutation pin:
        recording the generation before the pass lets the later skip run no pass
        and this goes red.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        outcomes = [failure, failure, "ok"]
        installs: list[str] = []

        def _conductor(**_kwargs):
            outcome = outcomes[len(installs)] if len(installs) < len(outcomes) else "ok"
            installs.append(outcome)
            if outcome == "raises":
                raise OSError(13, "conductor write refused")
            return outcome == "ok"

        clock = [1000.0]
        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch("kiro_crew.agent.conductor_agents._install_conductor_agent", _conductor),
            patch("kiro_crew.agent._held_pass_clock", lambda: clock[0]),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: 42,
            ),
        ):
            for _ in range(5):
                _run_install(tmp_path, cfg_dir)
                assert held_flag._conductor_spec_held is True
                clock[0] += held_flag.HELD_SIBLING_PASS_RETRY_SECS
        assert installs == [failure, failure, "ok"]
        assert held_flag._main_spec_skip_generation == 42
        assert held_flag._main_spec_held_pass_at is None

    def test_a_held_sibling_pass_waits_out_the_retry_interval(self, tmp_path: Path, held_flag):
        """The config watcher retries every 2 s: a held pass is not re-run on each tick.

        With the main spec and a sibling spec both unreadable, a pending model
        change lands here every tick. The pass that held runs once, the ticks
        inside ``HELD_SIBLING_PASS_RETRY_SECS`` run none, and the first skip
        after it runs it again. Mutation pin: leaving the generation unrecorded
        on a held pass makes ``installs`` grow on every tick.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        installs: list[int] = []

        def _conductor(**_kwargs):
            installs.append(1)
            return False  # held: the sibling spec cannot be written

        clock = [1000.0]
        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch("kiro_crew.agent.conductor_agents._install_conductor_agent", _conductor),
            patch("kiro_crew.agent._held_pass_clock", lambda: clock[0]),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: 42,
            ),
        ):
            for _tick in range(10):
                _run_install(tmp_path, cfg_dir)
                clock[0] += 2.0
            assert len(installs) == 1
            clock[0] += held_flag.HELD_SIBLING_PASS_RETRY_SECS
            _run_install(tmp_path, cfg_dir)
            assert len(installs) == 2
            assert held_flag._conductor_spec_held is True

    def test_the_hourly_wake_re_runs_a_held_pass_inside_the_interval(
        self, tmp_path: Path, held_flag
    ):
        """The maintenance wake is the held conductor's retry, so it never waits.

        Another rebuild (the ceiling hook, the profile watch) runs the pass and it
        holds; the wake lands seconds later, well inside
        ``HELD_SIBLING_PASS_RETRY_SECS``, and still re-runs it. Mutation pin: not
        making the pass due in ``retry_held_conductor_specs`` leaves ``installs``
        at one.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        installs: list[int] = []

        def _conductor(**_kwargs):
            installs.append(1)
            return False

        def _rebuild(**kwargs):
            return _run_install(tmp_path, cfg_dir, **kwargs)

        clock = [1000.0]
        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch("kiro_crew.agent.conductor_agents._install_conductor_agent", _conductor),
            patch("kiro_crew.agent._held_pass_clock", lambda: clock[0]),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", spec.parent),
            patch("kiro_crew.agent.rebuild_agent_config", _rebuild),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: 42,
            ),
        ):
            _run_install(tmp_path, cfg_dir)
            assert len(installs) == 1
            clock[0] += 10.0
            assert held_flag.retry_held_conductor_specs() is False
            assert len(installs) == 2

    def test_a_deferred_fork_refresh_leaves_the_generation_unrecorded(
        self, tmp_path: Path, held_flag, monkeypatch
    ):
        """Boot defers the fork refresh, so the skip cannot know how that pass ends.

        The deferred pass later holds a crew-bound shared template (its read was
        denied). Read access comes back while the main spec stays unreadable: the
        next skip, under the same generation, runs the pass again with the
        refresh in-line, and records the generation only once that pass holds
        nothing. Mutation pin: recording the generation on the deferred pass
        leaves ``refreshes`` at ``["defer"]``.
        """
        from kiro_crew.agent_materialization import fork_refresh

        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        refreshes: list[object] = []
        template_readable = [False]

        def _refresh(refresh_forks, gated_off):
            refreshes.append(refresh_forks)
            # The deferred pass has not run when the rebuild returns; an in-line
            # one settles the hold before it returns, as the real one does.
            if refresh_forks is True:
                fork_refresh._shared_template_held = not template_readable[0]

        monkeypatch.setattr(fork_refresh, "_shared_template_held", False)
        clock = [1000.0]
        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch("kiro_crew.agent.fork_refresh.refresh_after_rebuild", _refresh),
            patch("kiro_crew.agent._held_pass_clock", lambda: clock[0]),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                lambda: 42,
            ),
        ):
            _run_install(tmp_path, cfg_dir, refresh_forks="defer")
            assert held_flag._main_spec_skip_generation is None
            # The deferred pass, settling after the boot rebuild returned.
            fork_refresh._shared_template_held = True
            _run_install(tmp_path, cfg_dir)
            assert held_flag._main_spec_held_pass_at is not None
            template_readable[0] = True
            clock[0] += held_flag.HELD_SIBLING_PASS_RETRY_SECS
            _run_install(tmp_path, cfg_dir)
            assert held_flag._main_spec_held_pass_at is None
            assert held_flag._main_spec_skip_generation == 42
            _run_install(tmp_path, cfg_dir)
        assert refreshes == ["defer", True, True]

    def test_a_sibling_step_that_raises_keeps_the_hold(self, tmp_path: Path, held_flag):
        """The conductors settle the memo to False, then a later step raises: still held."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _denied),
            patch(
                "kiro_crew.agent.repair_agent_configs",
                side_effect=PermissionError(13, "hook repair"),
            ),
            pytest.raises(PermissionError, match="hook repair"),
        ):
            _run_install(tmp_path, cfg_dir)

        assert held_flag._conductor_spec_held is True

    def test_the_warning_is_once_per_episode_and_recovery_logs_once(
        self, tmp_path: Path, caplog, held_flag
    ):
        """A retry every tick warns once; a new cause warns again; recovery is one INFO."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        error = [PermissionError(13, "Permission denied", str(spec))]

        def _failing(path, **kwargs):
            if Path(path) == spec and error:
                raise error[0]
            return aw.read_bytes_with_retry(path, **kwargs)

        def _messages(text: str, level: int) -> int:
            return sum(1 for r in caplog.records if r.levelno == level and text in r.getMessage())

        with (
            patch("kiro_crew.agent.read_bytes_with_retry", _failing),
            caplog.at_level(logging.INFO, logger="kiro_crew.agent"),
        ):
            for _ in range(3):
                _run_install(tmp_path, cfg_dir)
            assert _messages("cannot read the existing agent spec", logging.WARNING) == 1
            error[0] = OSError(5, "Input/output error", str(spec))
            _run_install(tmp_path, cfg_dir)
            _run_install(tmp_path, cfg_dir)
            assert _messages("cannot read the existing agent spec", logging.WARNING) == 2
            error.clear()
            _run_install(tmp_path, cfg_dir)
            _run_install(tmp_path, cfg_dir)
        assert _messages("reads again and was rebuilt", logging.INFO) == 1
        assert held_flag._conductor_spec_held is False

    def test_an_unreadable_spec_inside_the_write_lock_skips_the_cycle(
        self, tmp_path: Path, held_flag
    ):
        """The commit's own re-read (the alias reconcile) takes the same exit.

        The load read succeeded, then the spec stopped being readable before the
        write: nothing is written over it and the skip is reported as held.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        before = spec.read_bytes()
        from kiro_crew.agent_materialization import mcp_aliases

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        def _unreadable_in_the_lock(path):
            try:
                _denied(path)
            except PermissionError as exc:
                return (True, mcp_aliases._UnreadableSpec(exc))
            raise AssertionError("the stub must refuse the spec")

        wrote_out: list[bool] = []
        held_out: list[bool] = []
        with (
            patch.object(mcp_aliases, "_durable_tool_aliases", _unreadable_in_the_lock),
            patch("kiro_crew.agent.worker_agent._install_worker_agent") as worker,
        ):
            _run_install(tmp_path, cfg_dir, _wrote_out=wrote_out, _held_out=held_out)

        assert spec.read_bytes() == before
        assert (wrote_out, held_out) == ([False], [True])
        assert held_flag._conductor_spec_held is True
        worker.assert_not_called()

    def test_the_sibling_pass_after_the_lock_never_exposes_a_half_written_spec(
        self, tmp_path: Path, held_flag
    ):
        """The skip's sibling writes run outside the agent-spec lock, and stay whole.

        A concurrent start reads a sibling spec without that lock, so what keeps
        it from a torn file is that every sibling write is a complete temp file
        swapped in by ``os.replace``, the same path the healthy rebuild's
        sibling pass takes after the lock (each installer takes the lock for
        its own write). Every spec the pass changes must be the target of such a
        replace, of a temp file that already parses. Mutation pin: a sibling
        installer writing its spec in place goes red.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent_materialization import mcp_aliases

        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)
        kiro_dir = spec.parent

        def _unreadable_in_the_lock(path):
            return (True, mcp_aliases._UnreadableSpec(PermissionError(13, "Permission denied")))

        replaced: set[str] = set()
        real_replace = agent_mod.replace_with_retry

        def _checked_replace(src, dst, *args, **kwargs):
            # Complete before it becomes visible: a reader sees the old file or this one.
            json.loads(Path(src).read_text(encoding="utf-8"))
            replaced.add(Path(dst).name)
            return real_replace(src, dst, *args, **kwargs)

        before = {p.name: p.read_bytes() for p in kiro_dir.glob("*.json")}
        with (
            patch.object(mcp_aliases, "_durable_tool_aliases", _unreadable_in_the_lock),
            patch.object(agent_mod, "replace_with_retry", _checked_replace),
        ):
            _run_install(tmp_path, cfg_dir)

        assert held_flag._main_spec_unreadable is True
        changed = {p.name for p in kiro_dir.glob("*.json") if before.get(p.name) != p.read_bytes()}
        assert "kirocrew.json" not in changed
        assert changed, "the sibling pass wrote no spec, so this proves nothing"
        assert changed <= replaced, changed - replaced

    def test_a_clean_rebuild_still_resets_an_unreadable_spec(self, tmp_path: Path, held_flag):
        """``--clean`` is the documented reset, so it writes defaults over the file."""
        cfg_dir = _bundled_defaults(tmp_path)
        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            if Path(path) == spec:
                raise PermissionError(13, "Permission denied", str(path))
            return aw.read_bytes_with_retry(path, **kwargs)

        wrote_out: list[bool] = []
        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            _run_install(tmp_path, cfg_dir, clean=True, _wrote_out=wrote_out)

        assert wrote_out == [True]
        assert json.loads(spec.read_text(encoding="utf-8"))["model"] == "claude-default"

    def test_the_durable_alias_reader_reports_an_unreadable_spec_as_present(self, tmp_path: Path):
        """A failed read is not "no spec": only ``FileNotFoundError`` is absent."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent_materialization import mcp_aliases

        spec = self._custom_spec(tmp_path)

        def _denied(path, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        with patch("kiro_crew.agent.read_bytes_with_retry", _denied):
            existed, aliases = mcp_aliases._durable_tool_aliases(spec)
            assert existed is True
            with pytest.raises(agent_mod._AgentSpecUnreadable):
                mcp_aliases._reconcile_tool_aliases_from_disk(spec, {})
        assert mcp_aliases._durable_tool_aliases(tmp_path / "absent.json") == (False, None)

    def test_invalid_utf8_still_rebuilds_from_defaults(self, tmp_path: Path):
        """A file that READ but does not decode is damaged, not unreadable."""
        import kiro_crew.agent as agent_mod

        spec = tmp_path / "kirocrew.json"
        spec.write_bytes(b"\xff\xfe\x00not utf-8")
        with patch("kiro_crew.agent.build_agent_config", return_value={"model": "d"}) as b:
            config, fresh = agent_mod._load_existing_config(spec)
        assert (config, fresh) == ({"model": "d"}, True)
        b.assert_called_once()

    def test_invalid_utf8_is_rewritten_by_a_whole_rebuild(self, tmp_path: Path):
        """The durable alias read decodes inside its ``try``, so the rebuild completes.

        Invalid UTF-8 is a ``ValueError`` there, read as no usable alias map, the
        same as invalid JSON, and the write that replaces the file goes ahead.
        """
        from kiro_crew.agent_materialization import mcp_aliases

        cfg_dir = _bundled_defaults(tmp_path)
        spec = tmp_path / "kiro_agents" / "kirocrew.json"
        spec.parent.mkdir(exist_ok=True)
        spec.write_bytes(b"\xff\xfe\x00not utf-8")
        assert mcp_aliases._durable_tool_aliases(spec) == (True, None)
        wrote: list[bool] = []
        _run_install(tmp_path, cfg_dir, _wrote_out=wrote)
        assert wrote == [True]
        assert isinstance(json.loads(spec.read_text(encoding="utf-8")), dict)

    def test_a_spec_that_vanishes_before_the_read_rebuilds_from_defaults(self, tmp_path: Path):
        """``FileNotFoundError`` is a missing file, not an unreadable one."""
        import kiro_crew.agent as agent_mod

        with patch("kiro_crew.agent.build_agent_config", return_value={"model": "d"}) as b:
            config, fresh = agent_mod._load_existing_config(tmp_path / "absent.json")
        assert (config, fresh) == ({"model": "d"}, True)
        b.assert_called_once()


def test_a_dashboard_rebuild_that_did_not_write_names_both_causes(monkeypatch, caplog):
    """``wrote=False`` has two causes, and the dashboard's warning names both.

    The shared-home guard refusing the write, and an existing spec that could
    not be read and was kept unchanged. Mutation pin: the warning naming only
    the shared agent home misleads an operator whose spec is unreadable.
    """
    from kiro_crew.dashboard.handlers import _shared

    monkeypatch.setattr(
        "kiro_crew.agent.rebuild_agent_config_reporting", lambda: (Path("kirocrew.json"), False)
    )
    with caplog.at_level(logging.WARNING, logger=_shared.logger.name):
        assert _shared.rebuild_agent_spec("an MCP change") is False
    [warning] = [r.getMessage() for r in caplog.records if "not rewritten" in r.getMessage()]
    assert "shared agent home" in warning
    assert "could not be read and was kept unchanged" in warning

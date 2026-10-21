"""Failure and streaming paths of the dashboard update handlers.

``dashboard/handlers/updates.py`` owns three surfaces whose HAPPY paths are
already pinned elsewhere (``test_update_check_install_aware.py``,
``test_update_channel_and_restart.py``, ``test_update_venv_detection.py``) while
their refusal and degraded paths were untested:

* **the check** — every ``git`` subprocess in ``_check_git_checkout`` is wrapped
  in its own ``wait_for``, and each timeout has to record an error code rather
  than fall through to "you are on the latest version". A check that could not
  run must never read as a verdict.
* **the apply** — ``POST /api/update`` refuses a non-checkout, a
  governance-pinned remote and a dirty tree, and its background worker has to
  report a failed ``git pull`` instead of silently stopping.
* **the streams** — ``GET /api/logs`` and the dashboard SSE endpoint are the two
  long-lived writers. Their client-disconnect and cancellation paths are the
  ones that leak a logging handler or an SSE queue when they are wrong.

Every subprocess, config write and socket here is stubbed: the tests touch no
network, spawn no process and write only under ``tmp_path``.

This file pins the check (``GET /api/update/check``, the ``_check_git_checkout`` failure
paths, the ``_do_update_check`` dispatch and the update-info accessors), the auto-update
switch (``POST /api/update/auto``) and the changelog cache. The apply, with its worker, the
pip-install report and the simulate and cancel endpoints, is pinned in
``test_dashboard_updates_coverage_apply.py``; the streams, with the log level endpoint and
the log handlers, are pinned in ``test_dashboard_updates_coverage_streams.py``. The
module-state fixture and the request stub every file uses, and the subprocess and checkout
stubs the check and the apply share, live in ``dashboard_updates_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _FakeProc,
    _git_proj,
    _isolated_module_state,
    _request,
    _sequence_procs,
)

from kiro_crew import platform_compat
from kiro_crew.config.loader import ConfigReadError
from kiro_crew.dashboard.handlers import updates
from kiro_crew.testing.wait import async_wait_until, await_until


def _git_capability():
    """The capability a git checkout derives, as ``_check_git_checkout`` receives it.

    Built here rather than through ``derive_capability`` so the failure paths
    under test do not also depend on a real git binary answering about this
    host's own tree.
    """
    from kiro_crew.platform import update_capability

    return update_capability.UpdateCapability(
        supported=True,
        managed_by=update_capability.MANAGED_BY_GIT,
        mode=update_capability.MODE_NOTIFY,
        can_download=True,
        can_apply=True,
        requires_restart=True,
    )


class TestUpdateCheckEndpoint:
    """``GET /api/update/check`` — the cache plus the governance pin."""

    @pytest.mark.asyncio
    async def test_reports_the_pin_alongside_the_cached_verdict(self, monkeypatch):
        """The pin is what lets the panel say WHY an update is mandatory.

        Without ``min_version`` / ``update_required`` on the wire the dashboard
        can only render a bare button, so both are asserted as part of the
        payload contract rather than left to the check.
        """
        checked: list[None] = []

        async def _fake_check() -> None:
            checked.append(None)
            updates._set_update_info(
                managed_by="kirocrew",
                latest_version="9.9.9",
                update_available=True,
                check_status="succeeded",
            )

        monkeypatch.setattr(updates, "_do_update_check", _fake_check)
        monkeypatch.setattr(updates, "min_version", lambda: "0.9.0")
        monkeypatch.setattr(updates, "update_required", lambda _v: True)

        resp = await updates.api_update_check(_request())

        assert resp.status == 200
        payload = json.loads(resp.body.decode())
        assert checked == [None]
        assert payload["latest_version"] == "9.9.9"
        assert payload["update_available"] is True
        assert payload["minimum_version_enforced"] == "0.9.0"
        assert payload["update_required"] is True
        assert "auto_update" in payload


# A stand-in ``git`` whose fetch starts a helper that inherits git's stderr and
# outlives git, the way a real ``git fetch`` starts ``git remote-https``, ssh or
# a credential helper; then git hangs so the fetch timeout fires. It records its
# own pid and its helper's, each with the start-time identity it reads at the one
# moment that pid is provably the process meant: its own, and its helper's while
# it has not reaped the helper (it never does). Each record is written whole.
_HELPER_GIT = """#!{python}
import os, subprocess, sys, time
sys.dont_write_bytecode = True  # the import below writes nothing into the checkout
sys.path.insert(0, {src!r})
from kiro_crew.platform_compat import get_process_start_id


def record(path, pid):
    with open(path + ".tmp", "w") as f:
        f.write("%d %s" % (pid, get_process_start_id(pid) or ""))
    os.replace(path + ".tmp", path)


record({pidfile!r} + ".git", os.getpid())
if {with_helper}:
    helper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], cwd=os.path.dirname({pidfile!r})
    )
    record({pidfile!r}, helper.pid)
time.sleep(600)
"""


def _recorded(path: Path) -> tuple[int, str | None] | None:
    """The (pid, start-time identity) the stand-in git recorded in *path*, or None before.

    A record that exists is whole. Its identity is None when the stand-in could not
    read one.
    """
    try:
        fields = path.read_text(encoding="utf-8").split()
        return int(fields[0]), (fields[1] if len(fields) > 1 else None)
    except (OSError, ValueError, IndexError):
        return None


class _TimedOutGitWaits:
    """``asyncio`` as the updates module sees it, with every git wait timing out.

    Bound as that module's own ``asyncio``, never over the real module, so the
    event loop, the test's own bound and ``platform_compat`` keep the real one.
    ``wait_for`` does what the real one does when its deadline passes -- cancels
    the awaited ``communicate()`` and raises ``TimeoutError`` -- but on a signal,
    never a clock: it waits until the stand-in git has recorded *started_record*
    (its helper's pid when there is a helper, else git's own pid). The stand-in
    writes that record only after ``create_subprocess_exec`` has launched it and
    ``communicate()`` is reading, so the timeout fires with git provably started
    and hung -- and, with a helper, the helper holding git's stderr -- however
    long the stand-in took to start.
    """

    def __init__(self, started_record: Path) -> None:
        self._started_record: Path | None = started_record

    def __getattr__(self, name: str) -> object:
        return getattr(asyncio, name)

    async def wait_for(self, aw, timeout=None):
        record, self._started_record = self._started_record, None
        waited = asyncio.ensure_future(aw)
        if record is not None:
            await async_wait_until(
                lambda: _recorded(record) is not None,
                off_loop=True,
                describe=lambda: f"the stand-in git never recorded itself in {record}",
            )
        waited.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waited
        raise asyncio.TimeoutError


class TestGitFetchTimeoutReapsTheHelper:
    """A timed-out fetch returns even when git's transport helper holds stderr."""

    def _stand_in_git(
        self, monkeypatch, tmp_path, *, with_helper: bool, short_waits: bool = True
    ) -> Path:
        pidfile = tmp_path / "helper.pid"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        git = bindir / "git"
        git.write_text(
            _HELPER_GIT.format(
                python=sys.executable,
                src=str(Path(platform_compat.__file__).resolve().parents[1]),
                with_helper=with_helper,
                pidfile=str(pidfile),
            ),
            encoding="utf-8",
        )
        git.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        if short_waits:
            started = pidfile if with_helper else Path(f"{pidfile}.git")
            waits = _TimedOutGitWaits(started)
            monkeypatch.setattr(updates, "asyncio", waits)
        return pidfile

    @staticmethod
    def _gone(pid: int, identity: str) -> bool:
        """Whether the process the stand-in git recorded as (*pid*, *identity*) has exited.

        A zombie awaiting its reaper has exited. Once that process is reaped nothing
        holds *pid*, and the kernel can give the number to another process, so a pid
        whose start-time identity now differs, or reads None, is gone too. The state
        is read first: a zombie at the number means ours is not running, whoever it is.
        """
        zombie = platform_compat.pid_is_zombie(pid) is True
        return zombie or platform_compat.get_process_start_id(pid) != identity

    @staticmethod
    def _end_pinned(pid: int, identity: str | None) -> None:
        """SIGKILL a recorded process the test has not seen exit, while its identity holds.

        A pid whose start-time identity now differs, or reads None, is a stranger or
        nothing, so it is not signalled; nor is a record that carries no identity.
        """
        if identity is None or platform_compat.get_process_start_id(pid) != identity:
            return
        with contextlib.suppress(OSError):
            platform_compat.kill_pid_pinned(pid, identity, platform_compat.SIGKILL)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    @pytest.mark.asyncio
    async def test_a_fetch_timeout_returns_while_a_helper_holds_stderr(self, monkeypatch, tmp_path):
        pidfile = self._stand_in_git(monkeypatch, tmp_path, with_helper=True)
        helper_exited = False
        try:
            check = asyncio.ensure_future(
                updates._check_git_checkout(_git_proj(monkeypatch, tmp_path), _git_capability())
            )
            try:
                await await_until(check, what="the fetch timeout handler returning")
            except AssertionError as stalled:
                check.cancel()
                raise AssertionError(
                    "the fetch timeout handler did not return: it killed git and then "
                    "waited on a stderr pipe the helper still holds open"
                ) from stalled
            assert updates._update_info["error_code"] == updates.ERR_GIT_FETCH_FAILED
            helper = _recorded(pidfile)
            assert helper is not None, "the stand-in git never started its helper"
            helper_pid, helper_identity = helper
            assert helper_identity is not None, f"no start-time identity for helper {helper_pid}"
            # Wait on the helper's own exit by its recorded identity, rather than
            # sleeping a fixed time for the group kill.
            helper_exited = bool(
                await async_wait_until(
                    lambda: self._gone(helper_pid, helper_identity),
                    off_loop=True,
                    describe=lambda: f"helper {helper_pid} ({helper_identity}) still running",
                )
            )
        finally:
            # Never signal a helper seen exit: nothing holds its pid once it is
            # reaped. Otherwise only while that pid still has the helper's identity.
            recorded = _recorded(pidfile)
            if recorded is not None and not helper_exited:
                self._end_pinned(*recorded)
        assert helper_exited, "the timeout left git's helper running"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    @pytest.mark.asyncio
    async def test_a_fetch_timeout_without_a_helper_is_still_a_failed_check(
        self, monkeypatch, tmp_path
    ):
        pidfile = self._stand_in_git(monkeypatch, tmp_path, with_helper=False)
        await await_until(
            asyncio.ensure_future(
                updates._check_git_checkout(_git_proj(monkeypatch, tmp_path), _git_capability())
            ),
            what="the fetch timeout handler returning",
        )
        assert updates._update_info["error_code"] == updates.ERR_GIT_FETCH_FAILED
        assert updates._update_info["check_status"] == updates.CHECK_FAILED
        assert not pidfile.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    @pytest.mark.asyncio
    async def test_a_cancelled_fetch_ends_git_and_its_helper(self, monkeypatch, tmp_path):
        """A shutdown cancels the check while git fetch runs: git and its helper end too.

        git runs in its own session, where a terminal's Ctrl-C does not reach
        it, so the cancel arm is what stops it. Without that arm the
        cancellation leaves git and its helper running.
        """
        pidfile = self._stand_in_git(monkeypatch, tmp_path, with_helper=True, short_waits=False)
        task = asyncio.ensure_future(
            updates._check_git_checkout(_git_proj(monkeypatch, tmp_path), _git_capability())
        )
        helper = git = None
        git_exited = helper_exited = False
        try:
            await async_wait_until(
                lambda: task.done() or _recorded(pidfile) is not None,
                off_loop=True,
                describe=lambda: f"no helper record in {pidfile}; check done={task.done()}",
            )
            helper = _recorded(pidfile)
            assert helper is not None, "the stand-in git never started its helper"
            git = _recorded(Path(f"{pidfile}.git"))
            assert git is not None, "the stand-in git never recorded its own pid"
            assert git[1] is not None, f"no start-time identity for git {git[0]}"
            assert helper[1] is not None, f"no start-time identity for helper {helper[0]}"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the cancelled check settling")

            def _both_gone() -> bool:
                nonlocal git_exited, helper_exited
                git_exited, helper_exited = self._gone(*git), self._gone(*helper)
                return git_exited and helper_exited

            await async_wait_until(
                _both_gone,
                off_loop=True,
                describe=lambda: f"git exited={git_exited}, helper exited={helper_exited}",
            )
        finally:
            if not task.done():
                task.cancel()
            # Never signal a process seen exit; otherwise only while its pid still
            # has the start-time identity the stand-in recorded for it.
            for recorded, exited in ((git, git_exited), (helper, helper_exited)):
                if recorded is not None and not exited:
                    self._end_pinned(*recorded)
        assert git_exited, "the cancelled check left git running"
        assert helper_exited, "the cancelled check left git's helper running"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    @pytest.mark.asyncio
    async def test_the_timeout_tests_cleanup_signals_no_helper_it_saw_exit(
        self, monkeypatch, tmp_path
    ):
        """Drives the timeout test above with a recorder in place of ``os.kill``.

        That test sees the helper exit before its cleanup runs, and nothing holds the
        helper's pid once the helper is reaped: a signal sent to the pid then reaches
        whatever process holds the number. After the fetch timeout returns, the
        recorder passes a signal to that pid on only while the pid still runs the
        helper (its recorded start-time identity, not a zombie). It records any other
        and never sends it.
        """
        pidfile = tmp_path / "helper.pid"
        real_kill = os.kill
        timed_out = False
        late: list[tuple[int, int]] = []

        def aimed_at_a_gone_helper(pid: int) -> bool:
            try:
                fields = pidfile.read_text(encoding="utf-8").split()
            except OSError:
                return False
            if not fields or fields[0] != str(pid):
                return False
            identity = fields[1] if len(fields) > 1 else None
            if identity is None or platform_compat.pid_is_zombie(pid) is True:
                return True
            return platform_compat.get_process_start_id(pid) != identity

        def recorder(pid: int, sig: int) -> None:
            if timed_out and sig != 0 and aimed_at_a_gone_helper(pid):
                late.append((pid, sig))
                return
            real_kill(pid, sig)

        check = updates._check_git_checkout

        async def check_then_mark(*args: object, **kwargs: object) -> None:
            nonlocal timed_out
            try:
                await check(*args, **kwargs)
            finally:
                timed_out = True

        monkeypatch.setattr(os, "kill", recorder)
        monkeypatch.setattr(updates, "_check_git_checkout", check_then_mark)
        await self.test_a_fetch_timeout_returns_while_a_helper_holds_stderr(monkeypatch, tmp_path)
        assert not late, (
            f"the cleanup sent signal {late[0][1]} to pid {late[0][0]}, the helper's, after "
            "the helper had exited; nothing held that pid any more"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    def test_the_cleanup_signals_a_recorded_pid_only_while_its_identity_holds(
        self, monkeypatch, tmp_path
    ):
        """The failure-path kill: a pid whose start-time identity differs is not signalled.

        A child of this test stands in for the recorded process. This test holds it
        unreaped, so its pid cannot name another process, and its identity is read here.
        """
        import signal
        import subprocess

        sent: list[tuple[int, str, int]] = []
        pinned = platform_compat.kill_pid_pinned

        def record(pid: int, identity: str, sig: int) -> bool:
            sent.append((pid, identity, sig))
            return pinned(pid, identity, sig)

        monkeypatch.setattr(platform_compat, "kill_pid_pinned", record)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
        try:
            identity = platform_compat.get_process_start_id(proc.pid)
            assert identity is not None, "precondition: the child's identity is readable"
            self._end_pinned(proc.pid, f"{identity}0")
            self._end_pinned(proc.pid, None)
            assert sent == [], f"a pid whose identity did not match was signalled: {sent}"
            self._end_pinned(proc.pid, identity)
            assert sent == [(proc.pid, identity, platform_compat.SIGKILL)]
            assert proc.wait(timeout=10) == -signal.SIGKILL
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


class TestGitCheckoutFailurePaths:
    """``_check_git_checkout`` — one ``wait_for`` per git call, one error each.

    Every branch here must leave ``checked`` False. Reporting ``available:
    False`` for a check that never completed is the exact bug this module's
    ``checked`` flag was introduced to stop.
    """

    async def _run(self, monkeypatch, tmp_path, procs: list[_FakeProc]):
        argv = _sequence_procs(monkeypatch, procs)
        await updates._check_git_checkout(_git_proj(monkeypatch, tmp_path), _git_capability())
        return argv

    def _assert_failed_check(self, error: str) -> None:
        assert updates._update_info["error_code"] == error
        assert updates._update_info["check_status"] == updates.CHECK_FAILED
        assert updates._update_info["update_available"] is None
        assert updates._update_info["managed_by"] == updates.MANAGED_BY_GIT
        assert updates._update_info["can_apply"] is True

    @pytest.mark.asyncio
    async def test_fetch_timeout_is_a_failed_check_not_a_verdict(self, monkeypatch, tmp_path):
        fetch = _FakeProc(time_out=True)
        await self._run(monkeypatch, tmp_path, [fetch])
        self._assert_failed_check(updates.ERR_GIT_FETCH_FAILED)
        # Killed and then reaped, so no zombie is left behind.
        assert fetch.killed is True
        assert fetch.communicate_calls == 2

    @pytest.mark.asyncio
    async def test_a_process_that_already_exited_is_tolerated(self, monkeypatch, tmp_path):
        """``kill()`` races the process exiting; ProcessLookupError is expected."""
        fetch = _FakeProc(time_out=True, kill_raises=True)
        await self._run(monkeypatch, tmp_path, [fetch])
        self._assert_failed_check(updates.ERR_GIT_FETCH_FAILED)

    @pytest.mark.asyncio
    async def test_head_read_timeout(self, monkeypatch, tmp_path):
        await self._run(
            monkeypatch,
            tmp_path,
            [_FakeProc(), _FakeProc(time_out=True, kill_raises=True)],
        )
        self._assert_failed_check(updates.ERR_GIT_READ_FAILED)

    @pytest.mark.asyncio
    async def test_upstream_read_timeout(self, monkeypatch, tmp_path):
        await self._run(
            monkeypatch,
            tmp_path,
            [_FakeProc(), _FakeProc(out=b"aaa111\n"), _FakeProc(time_out=True, kill_raises=True)],
        )
        self._assert_failed_check(updates.ERR_GIT_READ_FAILED)

    @pytest.mark.asyncio
    async def test_behind_count_timeout(self, monkeypatch, tmp_path):
        await self._run(
            monkeypatch,
            tmp_path,
            [
                _FakeProc(),
                _FakeProc(out=b"aaa111\n"),
                _FakeProc(out=b"bbb222\n"),
                _FakeProc(time_out=True, kill_raises=True),
            ],
        )
        self._assert_failed_check(updates.ERR_GIT_READ_FAILED)

    @pytest.mark.asyncio
    async def test_behind_count_that_is_not_a_number_is_a_failed_check(self, monkeypatch, tmp_path):
        """A count we cannot read is not licence to answer "up to date"."""
        await self._run(
            monkeypatch,
            tmp_path,
            [
                _FakeProc(),
                _FakeProc(out=b"aaa111\n"),
                _FakeProc(out=b"bbb222\n"),
                _FakeProc(out=b"not a number\n"),
            ],
        )
        self._assert_failed_check(updates.ERR_GIT_READ_FAILED)

    @pytest.mark.asyncio
    async def test_version_read_timeout(self, monkeypatch, tmp_path):
        argv = await self._run(
            monkeypatch,
            tmp_path,
            [
                _FakeProc(),
                _FakeProc(out=b"aaa111\n"),
                _FakeProc(out=b"bbb222\n"),
                _FakeProc(out=b"0\t7\n"),
                _FakeProc(time_out=True, kill_raises=True),
            ],
        )
        self._assert_failed_check(updates.ERR_GIT_READ_FAILED)
        # The version is read at the REMOTE sha when the two differ, not at HEAD:
        # comparing HEAD against itself can never detect an update.
        assert argv[-1] == ("git", "show", "bbb222:src/kiro_crew/__init__.py")

    @pytest.mark.asyncio
    async def test_a_version_that_cannot_be_parsed_is_reported_as_such(self, monkeypatch, tmp_path):
        """Unparseable version AND no commit distance — nothing left to answer with.

        ``behind`` is scripted to 0 on purpose: a non-zero count is a verdict in
        its own right and would (correctly) rescue the check.
        """
        await self._run(
            monkeypatch,
            tmp_path,
            [
                _FakeProc(),
                _FakeProc(out=b"aaa111\n"),
                _FakeProc(out=b"bbb222\n"),
                _FakeProc(out=b"0\t0\n"),
                _FakeProc(out=b'__version__ = "not/a/version"\n'),
            ],
        )
        self._assert_failed_check(updates.ERR_VERSION_UNPARSEABLE)
        # The unusable value is still reported, so the panel can show what it saw.
        assert updates._update_info["latest_version"] == "not/a/version"

    @pytest.mark.asyncio
    async def test_a_changelog_diff_timeout_still_reports_the_update(self, monkeypatch, tmp_path):
        """The version comparison already succeeded — a missing diff is cosmetic.

        Discarding a good verdict because the changelog could not be read would
        hide a real update behind an optional field.
        """
        await self._run(
            monkeypatch,
            tmp_path,
            [
                _FakeProc(),
                _FakeProc(out=b"aaa111\n"),
                _FakeProc(out=b"bbb222\n"),
                _FakeProc(out=b"0\t7\n"),
                _FakeProc(out=b'__version__ = "999.0.0"\n'),
                _FakeProc(time_out=True, kill_raises=True),
            ],
        )
        assert updates._update_info["update_available"] is True
        assert updates._update_info["check_status"] == updates.CHECK_SUCCEEDED
        assert updates._update_info["latest_version"] == "999.0.0"
        assert updates._update_info["error_code"] is None
        assert updates._update_info["changes"] == ""

    @pytest.mark.asyncio
    async def test_only_added_changelog_lines_become_changes(self, monkeypatch, tmp_path):
        diff = (
            b"diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
            b"--- a/CHANGELOG.md\n"
            b"+++ b/CHANGELOG.md\n"
            b"@@ -1,2 +1,3 @@\n"
            b"+## [999.0.0]\n"
            b"+- a new thing\n"
            b"-- an old thing\n"
            b" unchanged\n"
        )
        await self._run(
            monkeypatch,
            tmp_path,
            [
                _FakeProc(),
                _FakeProc(out=b"aaa111\n"),
                _FakeProc(out=b"bbb222\n"),
                _FakeProc(out=b"0\t7\n"),
                _FakeProc(out=b'__version__ = "999.0.0"\n'),
                _FakeProc(out=diff),
            ],
        )
        # The ``+++`` header is a diff artifact, not a changelog line.
        assert updates._update_info["changes"] == "## [999.0.0]\n- a new thing"


class TestAutoUpdateToggle:
    """``POST /api/update/auto`` — the read fails CLOSED."""

    @pytest.mark.asyncio
    async def test_rejects_a_body_that_is_not_json(self):
        resp = await updates.api_update_auto(_request(ValueError("bad json")))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_an_unreadable_config_refuses_instead_of_clobbering(self, monkeypatch):
        """Treating an unreadable config as ``{}`` would write back a one-key file.

        That is a silent wipe of every other setting the user has, so the handler
        must answer 500 and write nothing at all.
        """
        wrote: list[object] = []

        def _refuse(_path, *, mutate):
            # `update_config_locked` reads under the lock, so a corrupt file raises
            # here -- before the mutator is ever given a dict to modify.
            raise ConfigReadError("truncated json")

        monkeypatch.setattr(updates, "update_config_locked", _refuse)

        resp = await updates.api_update_auto(_request({"enabled": False}))

        assert resp.status == 500
        assert json.loads(resp.body.decode())["code"] == "config_unreadable"
        assert wrote == []

    @pytest.mark.asyncio
    async def test_writes_the_flag_back_beside_every_other_setting(self, monkeypatch):
        wrote: list[dict] = []

        def _apply(_path, *, mutate):
            # Stand in for the locked read-modify-write: hand the mutator the stored
            # config and record whatever it returns. Recording the RETURN VALUE (not
            # the input dict) is the point -- `update_config_locked` treats a `None`
            # return as "do not write", so a mutator that only edits in place would
            # silently drop the write while still reporting success.
            wrote.append(mutate({"agent": {"model": "x"}}))

        monkeypatch.setattr(updates, "update_config_locked", _apply)
        # The writer above is a stand-in, so pin what a reload would now see.
        cfg = MagicMock()
        cfg.auto_update = False
        monkeypatch.setattr(updates.KiroCrewConfig, "load", staticmethod(lambda: cfg))

        resp = await updates.api_update_auto(_request({"enabled": False}))

        assert resp.status == 200
        assert json.loads(resp.body.decode()) == {
            "ok": True,
            # The EFFECTIVE value, re-read after the write.
            "auto_update": False,
            "requested": False,
            "overlay_override": False,
        }
        assert wrote == [{"agent": {"model": "x"}, "auto_update": False}]

    @pytest.mark.asyncio
    async def test_defaults_to_enabling_when_the_flag_is_omitted(self, monkeypatch):
        wrote: list[dict] = []

        def _apply(_path, *, mutate):
            wrote.append(mutate({}))

        monkeypatch.setattr(updates, "update_config_locked", _apply)

        resp = await updates.api_update_auto(_request({}))

        assert resp.status == 200
        assert wrote == [{"auto_update": True}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None, [], {}])
    async def test_a_non_boolean_flag_is_refused_without_writing(self, monkeypatch, enabled):
        """Stored verbatim and read for truthiness: ``"false"`` would read as ON."""
        wrote: list[dict] = []
        monkeypatch.setattr(
            updates, "update_config_locked", lambda _p, *, mutate: wrote.append(mutate({}))
        )

        resp = await updates.api_update_auto(_request({"enabled": enabled}))

        assert resp.status == 400
        assert json.loads(resp.body.decode())["code"] == "invalid_enabled"
        assert wrote == []

    @pytest.mark.asyncio
    async def test_an_overlay_owned_switch_is_refused_without_writing(self, monkeypatch, tmp_path):
        """config.local.json deep-merges over the file this endpoint writes.

        A 200 there would show the switch flipped while the next load snaps it
        back, so the write is refused with the reason, like the other
        overlay-owned settings.
        """
        from kiro_crew.config import loader

        local = tmp_path / "config.local.json"
        local.write_text(json.dumps({"auto_update": True}), encoding="utf-8")
        monkeypatch.setattr(loader, "config_local_path", lambda: local)
        wrote: list[dict] = []
        monkeypatch.setattr(
            updates, "update_config_locked", lambda _p, *, mutate: wrote.append(mutate({}))
        )

        req = _request({"enabled": False})
        resp = await updates.api_update_auto(req)

        body = json.loads(resp.body.decode())
        assert resp.status == 409
        assert body["code"] == "auto_update_overlay_owned"
        assert body["overlay_override"] is True
        assert "config.local.json" in body["error"]
        assert wrote == []
        req.app["state"].push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_response_reports_the_effective_value_after_the_write(self, monkeypatch):
        """Read back after the merge, never echoed from the request."""
        monkeypatch.setattr(updates, "update_config_locked", lambda _p, *, mutate: mutate({}))
        cfg = MagicMock()
        cfg.auto_update = True  # what a concurrent writer left, say
        monkeypatch.setattr(updates.KiroCrewConfig, "load", staticmethod(lambda: cfg))

        resp = await updates.api_update_auto(_request({"enabled": False}))

        body = json.loads(resp.body.decode())
        assert resp.status == 200
        assert body == {
            "ok": True,
            "auto_update": True,
            "requested": False,
            "overlay_override": False,
        }

    @pytest.mark.asyncio
    async def test_a_successful_write_tells_the_other_tabs(self, monkeypatch):
        """A second open tab must not keep showing the old switch position."""
        monkeypatch.setattr(updates, "update_config_locked", lambda _p, *, mutate: mutate({}))

        req = _request({"enabled": False})
        resp = await updates.api_update_auto(req)

        assert resp.status == 200
        req.app["state"].push_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_refused_write_sends_no_refresh(self, monkeypatch):
        req = _request({"enabled": "false"})
        resp = await updates.api_update_auto(req)

        assert resp.status == 400
        req.app["state"].push_refresh.assert_not_called()

    @pytest.mark.parametrize(
        "raw",
        [b"{not json", '{"timezone": "Europe/Z\u00fcrich"}'.encode("cp1252"), b"[]"],
        ids=["malformed", "non-utf8", "not-an-object"],
    )
    def test_an_unreadable_overlay_reports_not_pinned(self, monkeypatch, tmp_path, raw):
        """Best-effort, like the loader: a bad overlay must not 500 a committed write."""
        from kiro_crew.config import loader

        local = tmp_path / "config.local.json"
        local.write_bytes(raw)
        monkeypatch.setattr(loader, "config_local_path", lambda: local)

        assert loader.overlay_pins("auto_update") is False

    def test_a_nested_key_is_found_by_path(self, monkeypatch, tmp_path):
        from kiro_crew.config import loader

        local = tmp_path / "config.local.json"
        local.write_text(json.dumps({"telemetry": {"enabled": False}}), encoding="utf-8")
        monkeypatch.setattr(loader, "config_local_path", lambda: local)

        assert loader.overlay_pins("telemetry", "enabled") is True
        assert loader.overlay_pins("telemetry", "beacon_enabled") is False
        assert loader.overlay_pins("auto_update") is False


class TestChangelogCache:
    """``_read_changelog`` — cached on a stat signature, silent on failure."""

    def _project_changelog(self, monkeypatch, tmp_path, body: str) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        path = proj / "CHANGELOG.md"
        # newline="\n" so the byte count and content match on Windows too.
        path.write_text(body, encoding="utf-8", newline="\n")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        return path

    def test_an_unchanged_file_is_read_once(self, monkeypatch, tmp_path):
        self._project_changelog(monkeypatch, tmp_path, "# Changelog\n\n## [1.0.0]\n")
        reads: list[str] = []
        original = Path.read_text

        def _counting(self, *args, **kwargs):  # noqa: ANN001 - mirrors Path.read_text
            reads.append(str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _counting)

        first = updates._read_changelog()
        second = updates._read_changelog()

        assert first == second
        assert "## [1.0.0]" in first
        # One read for two calls: the second was served from the cache.
        assert len(reads) == 1

    def test_an_edit_invalidates_the_cache(self, monkeypatch, tmp_path):
        """A dev install edits CHANGELOG.md in place; the endpoint stays live."""
        path = self._project_changelog(monkeypatch, tmp_path, "# Changelog\n\nold\n")
        assert "old" in updates._read_changelog()
        path.write_text("# Changelog\n\nbrand new\n", encoding="utf-8", newline="\n")
        os.utime(path, (1_600_000_000, 1_600_000_000))
        assert "brand new" in updates._read_changelog()

    def test_an_unstattable_path_yields_empty_not_an_error(self, monkeypatch):
        broken = MagicMock()
        broken.stat.side_effect = OSError("stale nfs handle")
        monkeypatch.setattr(updates, "_changelog_path", lambda: broken)
        assert updates._read_changelog() == ""

    def test_an_unreadable_file_yields_empty_not_an_error(self, monkeypatch, tmp_path):
        self._project_changelog(monkeypatch, tmp_path, "# Changelog\n")

        def _boom(self, *args, **kwargs):  # noqa: ANN001 - mirrors Path.read_text
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "invalid")

        monkeypatch.setattr(Path, "read_text", _boom)
        assert updates._read_changelog() == ""

    def test_no_changelog_anywhere_yields_empty(self, monkeypatch, tmp_path):
        empty = tmp_path / "no-changelog"
        empty.mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(empty))
        monkeypatch.setattr(updates, "_changelog_path", lambda: None)
        assert updates._read_changelog() == ""

    def test_a_wheel_install_falls_back_to_the_bundled_copy(self, monkeypatch, tmp_path):
        """A pip wheel has no source tree; setup.py copies CHANGELOG.md into the package.

        The fallback is asserted without creating the file, because it lives in
        the installed package tree rather than under ``tmp_path``.
        """
        bundled = Path(updates.__file__).resolve().parents[2] / "CHANGELOG.md"
        original = Path.is_file

        def _only_bundled(self):  # noqa: ANN001 - mirrors Path.is_file
            return os.path.realpath(str(self)) == os.path.realpath(str(bundled))

        monkeypatch.setattr(Path, "is_file", _only_bundled)
        # A project dir with no CHANGELOG.md of its own must not short-circuit.
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        resolved = updates._changelog_path()

        assert resolved is not None
        assert os.path.realpath(str(resolved)) == os.path.realpath(str(bundled))
        monkeypatch.setattr(Path, "is_file", original)


class TestUpdateInfoAccessors:
    """The cache is handed out as a COPY and reset wholesale."""

    def test_get_update_info_cannot_be_mutated_through_the_caller(self):
        updates._set_update_info(
            managed_by="kirocrew", latest_version="1.2.3", check_status="succeeded"
        )
        snapshot = updates.get_update_info()
        snapshot["latest_version"] = "tampered"
        assert updates._update_info["latest_version"] == "1.2.3"

    def test_a_new_result_never_inherits_a_key_from_the_previous_one(self):
        """A stale ``latest_version`` beside a fresh ``error_code`` is a half-truth."""
        updates._set_update_info(
            managed_by="git",
            update_available=True,
            latest_version="9.9.9",
            check_status="succeeded",
        )
        updates._set_update_info(managed_by="git", error_code=updates.ERR_GIT_FETCH_FAILED)
        assert updates._update_info["latest_version"] == ""
        assert updates._update_info["update_available"] is None
        assert updates._update_info["check_status"] == updates.CHECK_UNCHECKED

    def test_invalidation_bumps_the_generation_and_unstamps_the_clock(self):
        updates._last_update_check = 1234.0
        generation = updates._check_generation
        updates._invalidate_update_check("insider")
        assert updates._check_generation == generation + 1
        assert updates._last_update_check == 0.0
        assert updates._update_info["check_status"] == updates.CHECK_UNCHECKED

    def test_an_externally_managed_install_names_its_real_update_surface(self):
        """The reason is DERIVED from the capability, not restated per surface.

        A second copy of the list is the drift this contract removed: the
        capability now answers for every stamp, including the Linux packages.
        """
        from kiro_crew.platform import update_capability

        for stamp in ("dmg", "appimage", "deb", "rpm"):
            capability = update_capability.derive_capability(install_root="", dist=stamp)
            assert capability.unavailable_reason == update_capability.UNAVAILABLE_MANAGED_BY_APP
        docker = update_capability.derive_capability(install_root="", dist="docker")
        assert docker.unavailable_reason == update_capability.UNAVAILABLE_MANAGED_BY_IMAGE

    def test_a_cdn_override_moves_the_feed_and_the_artifact_together(self, monkeypatch):
        """Splitting them would check one host and recommend an install from another."""
        monkeypatch.setenv("KIROCREW_CDN_BASE", "https://cdn.example.invalid/")
        assert updates._cdn_bases() == (
            "https://cdn.example.invalid",
            "https://cdn.example.invalid",
        )

    def test_the_recommended_command_pins_https_and_names_the_channel(self, monkeypatch):
        # Asserts the invariants, not an exact string: the builder is shared with
        # the gateway's unattended path, so its shape may change (it stopped
        # piping curl into sh, which hid download failures) while these must hold.
        monkeypatch.setenv("KIROCREW_CDN_BASE", "https://download.example.invalid")
        command = updates.wheel_update_command("insider")
        assert "--proto '=https'" in command, "must refuse a plaintext override"
        assert "https://download.example.invalid/cli.sh" in command
        assert "--channel insider" in command, "a bare re-run would default to stable"
        # The invariant is that the DOWNLOAD's failure fails the command.
        # A pipe fed from an already-checked variable preserves that; only a
        # bare `curl … | sh` would report just sh's status.
        assert '_kc_body="$(curl' in command, "curl must not feed sh directly"


class TestExternallyManagedCheck:
    """``_do_update_check`` dispatch, including the guards around it."""

    @pytest.mark.asyncio
    async def test_a_desktop_bundle_reports_its_own_update_surface(self, monkeypatch):
        """The .app embeds this backend but is replaced by the Electron updater.

        Answering with a CLI verdict here would compare against the wrong version
        stream and then recommend an installer that does not apply to a bundle.
        """
        from kiro_crew.platform import update_capability

        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "dmg")
        await updates._do_update_check()
        assert updates._update_info["managed_by"] == update_capability.MANAGED_BY_ELECTRON
        assert updates._update_info["can_apply"] is False
        assert (
            updates._update_info["unavailable_reason"]
            == update_capability.UNAVAILABLE_MANAGED_BY_APP
        )
        assert updates._update_info["check_status"] == updates.CHECK_DEFERRED

    @pytest.mark.asyncio
    async def test_an_unexpected_crash_records_an_error_rather_than_a_verdict(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))

        async def _boom(_proj: str, _capability) -> None:
            raise RuntimeError("git is not installed")

        monkeypatch.setattr(updates, "_check_git_checkout", _boom)
        await updates._do_update_check()

        assert updates._update_info["error_code"] == updates.ERR_UNKNOWN
        assert updates._update_info["managed_by"] == updates.MANAGED_BY_GIT
        assert updates._update_info["can_apply"] is True
        assert updates._update_info["check_status"] == updates.CHECK_FAILED
        # Stamped even on failure, so a broken host cannot turn the 12-hourly
        # background poll into a hot retry loop.
        assert updates._last_update_check > 0

    @pytest.mark.asyncio
    async def test_a_second_caller_awaits_the_shared_check(self, monkeypatch):
        calls: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def _count(capability) -> None:
            calls.append(capability.managed_by)
            started.set()
            await release.wait()

        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "wheel")
        monkeypatch.setattr(updates, "_check_release_feed", _count)
        leader = asyncio.create_task(updates._do_update_check())
        await await_until(started, what="the leader's check starting")
        follower = asyncio.create_task(updates._do_update_check())
        await asyncio.sleep(0)
        assert not follower.done()
        release.set()
        await asyncio.gather(leader, follower)
        assert calls == ["kirocrew"]


class TestAnUnexpectedCheckFailureKeepsTheInstallsShape:
    """A transient raise must not make a managed venv look notify-only.

    ``_set_update_info`` re-seeds ``can_arm`` to False, so the outer handler has
    to carry it the way the feed-failure paths do — otherwise one unexpected
    raise (a signature probe, a venv-layout read) hides the in-app update path
    until the next successful check.
    """

    @pytest.mark.asyncio
    async def test_can_arm_and_the_effect_survive_the_failure(self, monkeypatch):
        from kiro_crew.platform import update_capability, wheel_engine

        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda: True)
        monkeypatch.setattr(update_capability, "_source_checkout_root", lambda: None)
        monkeypatch.setattr(update_capability, "_runs_from_managed_venv", lambda: True)
        monkeypatch.setattr(update_capability, "_installer_runs_here", lambda: True)
        monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda _n: "/bin/sh")
        monkeypatch.setattr("kiro_crew.platform.update_layout.cdn_bases_are_safe", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.cdn_bases", lambda: ("https://a", "https://b")
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason", lambda _u: ""
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_required", lambda _v: False
        )

        def _boom():
            raise RuntimeError("openssl verify blew up")

        monkeypatch.setattr(updates, "derive_capability", _boom)
        monkeypatch.setattr(updates, "_auto_effect", None)
        monkeypatch.setattr(updates, "_auto_effect_task", None)
        monkeypatch.setattr(updates, "_shape_effect", None)

        await updates._run_update_check()
        await updates.prime_status_auto_update_effect()
        fields = updates.status_update_fields()

        assert fields["update_check_status"] == "failed"
        # The in-app update path is still offered, and the switch still says
        # what the switch does.
        assert fields["update_can_arm"] is True
        assert fields["update_auto_effect"] == "install"


class TestTheManualCheckRecordsTheEffect:
    """The user asked, so the derivation's git probes are warranted here.

    Before the update loop's first cycle a status frame can only answer the
    shapes that need no git; a manual Check answers every shape and records it,
    so the switch stops reading ``unknown`` without waiting for the loop.
    """

    @pytest.mark.asyncio
    async def test_the_check_derives_and_publishes_it(self, monkeypatch):
        from kiro_crew.platform.update_capability import AutoUpdateEffect

        monkeypatch.setattr(updates, "_auto_effect", None)
        monkeypatch.setattr(updates, "_auto_effect_task", None)
        monkeypatch.setattr(updates, "_do_update_check", AsyncMock())
        monkeypatch.setattr(
            updates, "auto_update_effect", lambda: AutoUpdateEffect("install", "git")
        )

        resp = await updates.api_update_check(_request({}))

        body = json.loads(resp.body.decode())
        assert body["update_auto_effect"] == "install"
        # Recorded, so the next status frame carries it too.
        assert updates.status_update_fields()["update_auto_effect"] == "install"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "x", 3])
async def test_a_non_object_auto_update_body_is_a_400_not_a_500(body):
    resp = await updates.api_update_auto(_request(body))
    assert resp.status == 400
    assert json.loads(resp.body.decode())["code"] == "invalid_json"

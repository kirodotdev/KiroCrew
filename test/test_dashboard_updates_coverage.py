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
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew.config.loader import ConfigReadError
from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_layout


@pytest.fixture(autouse=True)
def _isolated_module_state(monkeypatch, tmp_path):
    """Snapshot every module global these tests mutate, then put it all back.

    ``_update_info``, the ring buffer and the changelog cache are process-wide,
    so a test that left one dirty would change what a LATER test measures --
    exactly the class of cross-test coupling this module's cache contract exists
    to prevent.
    """
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)
    monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)
    saved_info = dict(updates._update_info)
    saved_ring = list(updates._log_ring)
    saved_cache = updates._changelog_cache
    saved_clock = updates._last_update_check
    saved_generation = updates._check_generation
    updates._changelog_cache = None
    yield
    updates._update_info.clear()
    updates._update_info.update(saved_info)
    updates._log_ring.clear()
    updates._log_ring.extend(saved_ring)
    updates._changelog_cache = saved_cache
    updates._last_update_check = saved_clock
    updates._check_generation = saved_generation
    updates._check_in_flight = False


def _request(body: object = None, *, query: dict[str, str] | None = None) -> MagicMock:
    """A request stub: only ``.json()``, ``.query`` and ``app["state"]`` are read."""
    req = MagicMock()

    async def _json() -> object:
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json
    req.query = dict(query or {})
    state = MagicMock()
    state._background_tasks = set()
    req.app = {"state": state}
    return req


class _FakeProc:
    """A stand-in for an ``asyncio`` subprocess that never spawns anything.

    ``time_out=True`` raises on the FIRST ``communicate()`` only: the handler's
    timeout branch kills the process and awaits ``communicate()`` a second time
    to reap it, so a fake that raised every time would mask that reap.
    """

    def __init__(
        self,
        *,
        out: bytes = b"",
        err: bytes = b"",
        returncode: int = 0,
        time_out: bool = False,
        kill_raises: bool = False,
    ) -> None:
        self._out = out
        self._err = err
        self.returncode = returncode
        self._time_out = time_out
        self._kill_raises = kill_raises
        self.communicate_calls = 0
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self._time_out and self.communicate_calls == 1:
            raise asyncio.TimeoutError
        return self._out, self._err

    def kill(self) -> None:
        self.killed = True
        if self._kill_raises:
            raise ProcessLookupError("already gone")


def _sequence_procs(monkeypatch, procs: list[_FakeProc]) -> list[tuple[str, ...]]:
    """Serve *procs* in order to successive ``create_subprocess_exec`` calls."""
    argv_seen: list[tuple[str, ...]] = []
    pending = list(procs)

    async def _fake_exec(*args: str, **_kwargs: object) -> _FakeProc:
        argv_seen.append(tuple(str(a) for a in args))
        if not pending:
            raise AssertionError(f"unexpected extra subprocess: {args}")
        return pending.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    return argv_seen


def _git_proj(monkeypatch, tmp_path) -> str:
    """A directory the capability derivation accepts as this install's checkout.

    ``.git/HEAD`` is written, not just ``.git/``: the derivation asks git first
    and falls back to the on-disk markers of a working tree's own root, and a
    bare ``.git`` directory satisfies neither — a fabricated one is refused on
    purpose. The git lane also requires provenance (the running package loads
    from the tree), which a fabricated directory cannot satisfy, so it is
    declared here rather than derived.
    """
    proj = tmp_path / "checkout"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(
        "kiro_crew.platform.update_capability.running_from_checkout",
        lambda root, **kw: True,
    )
    return str(proj)


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

        resp = await updates.api_update_auto(_request({"enabled": False}))

        assert resp.status == 200
        assert json.loads(resp.body.decode()) == {"ok": True, "auto_update": False}
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


class TestPipInstallFailureReport:
    """``_venv_pip_install`` — a long stderr is truncated, not dropped."""

    @pytest.mark.asyncio
    async def test_a_huge_stderr_is_truncated_with_a_marker(self, monkeypatch):
        state = MagicMock()
        proc = _FakeProc(err=("x" * 4000).encode(), returncode=1)
        _sequence_procs(monkeypatch, [proc])

        assert await updates._venv_pip_install("/tmp/proj", state) is False

        step, detail = state.push_update_progress.call_args[0]
        assert step == "error"
        # Truncated rather than dropped: the actionable line is often mid-stderr.
        assert detail.endswith("…(truncated)")
        assert len(detail) < 1200


class TestApplyRefusals:
    """``POST /api/update`` — every precondition, and the worker's own failures."""

    @pytest.mark.asyncio
    async def test_refuses_when_no_project_dir_is_configured(self):
        resp = await updates.api_update_apply(_request({}))
        assert resp.status == 400
        assert "KIROCREW_PROJECT_DIR" in json.loads(resp.body.decode())["error"]

    @pytest.mark.asyncio
    async def test_refuses_a_tarball_install_with_a_specific_message(self, monkeypatch, tmp_path):
        """No ``.git`` means ``git pull`` cannot update it — say so, not "pull failed"."""
        plain = tmp_path / "tarball"
        plain.mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(plain))
        resp = await updates.api_update_apply(_request({}))
        assert resp.status == 409
        assert "Not a git checkout" in json.loads(resp.body.decode())["error"]

    @pytest.mark.asyncio
    async def test_a_pinned_remote_is_refused_before_any_spinner_is_shown(
        self, monkeypatch, tmp_path
    ):
        """Governance is checked before ``push_refresh``.

        A dashboard token proves the caller is the operator, not that the fleet
        permits this host to pull from this remote -- and a blocked update must
        leave no "updating" overlay behind for the user to dismiss.
        """
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p: "https://example.invalid/x")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "remote is not permitted")

        req = _request({})
        resp = await updates.api_update_apply(req)

        assert resp.status == 403
        payload = json.loads(resp.body.decode())
        assert payload["governance"] is True
        assert payload["error"] == "remote is not permitted"
        req.app["state"].push_refresh.assert_not_called()
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_hung_status_check_answers_500_rather_than_hanging(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(monkeypatch, [_FakeProc(time_out=True, kill_raises=True)])

        req = _request({})
        resp = await updates.api_update_apply(req)

        assert resp.status == 500
        assert "Timed out" in json.loads(resp.body.decode())["error"]
        assert req.app["state"]._background_tasks == set()

    @pytest.mark.asyncio
    async def test_a_dirty_tree_is_refused_without_starting_the_worker(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(monkeypatch, [_FakeProc(out=b" M src/kiro_crew/cli.py\n")])

        req = _request({})
        resp = await updates.api_update_apply(req)

        assert resp.status == 409
        assert "uncommitted changes" in json.loads(resp.body.decode())["error"]
        assert req.app["state"]._background_tasks == set()

    async def _drive_worker(self, monkeypatch, tmp_path, procs: list[_FakeProc]):
        """Accept the request, then await the background worker it scheduled."""
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        _sequence_procs(monkeypatch, [_FakeProc(out=b"")] + procs)

        req = _request({})
        state = req.app["state"]
        resp = await updates.api_update_apply(req)
        assert resp.status == 200
        # The worker is registered on the state so it cannot be garbage collected
        # mid-flight; awaiting it here is what makes the test deterministic.
        assert len(state._background_tasks) == 1
        await asyncio.gather(*list(state._background_tasks))
        return state

    def _progress(self, state) -> list[tuple[str, str]]:
        return [tuple(call.args) for call in state.push_update_progress.call_args_list]

    @pytest.mark.asyncio
    async def test_a_hung_pull_is_reported_and_stops_the_worker(self, monkeypatch, tmp_path):
        state = await self._drive_worker(
            monkeypatch, tmp_path, [_FakeProc(time_out=True, kill_raises=True)]
        )
        assert ("error", "git pull timed out") in self._progress(state)
        # No build and no restart followed the failure.
        assert not any(step == "restarting" for step, _ in self._progress(state))

    @pytest.mark.asyncio
    async def test_a_failed_pull_is_reported_and_stops_the_worker(self, monkeypatch, tmp_path):
        state = await self._drive_worker(monkeypatch, tmp_path, [_FakeProc(returncode=1)])
        assert ("error", "git pull failed") in self._progress(state)

    @pytest.mark.asyncio
    async def test_an_unexpected_crash_surfaces_as_a_failed_update(self, monkeypatch, tmp_path):
        """An exception inside the worker must reach the UI, not just the log.

        The overlay has no other way to leave the "updating" state, so a
        swallowed error strands the user on a spinner forever.
        """
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", _git_proj(monkeypatch, tmp_path))
        monkeypatch.setattr(updates, "resolve_remote_url", lambda _p: "")
        monkeypatch.setattr(updates, "update_blocked_reason", lambda _u: "")
        calls = {"n": 0}

        async def _exec(*args: str, **_kwargs: object):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeProc(out=b"")
            raise RuntimeError("fork failed")

        monkeypatch.setattr("asyncio.create_subprocess_exec", _exec)

        req = _request({})
        state = req.app["state"]
        await updates.api_update_apply(req)
        await asyncio.gather(*list(state._background_tasks))

        assert ("failed", "Update failed — check logs") in self._progress(state)
        state.push_refresh.assert_any_call("update_failed")


class TestSimulateAndCancel:
    """The two overlay-driving endpoints."""

    @pytest.mark.asyncio
    async def test_a_missing_body_is_treated_as_empty_not_an_error(self, monkeypatch):
        """The simulator is a local test aid; an absent body must not 400."""
        req = _request(ValueError("no body"))
        state = req.app["state"]
        resp = await updates.api_update_simulate(req)
        assert resp.status == 200
        assert json.loads(resp.body.decode())["status"] == "simulating"
        for task in list(state._background_tasks):
            task.cancel()

    @pytest.mark.asyncio
    async def test_a_simulated_rejection_uses_the_caller_s_message(self):
        resp = await updates.api_update_simulate(
            _request({"reject": True, "reject_message": "nope"})
        )
        assert resp.status == 409
        assert json.loads(resp.body.decode())["error"] == "nope"

    @pytest.mark.asyncio
    async def test_cancel_clears_the_overlay_on_both_sides_of_the_failed_event(self):
        req = _request({})
        state = req.app["state"]
        resp = await updates.api_update_cancel(req)
        assert resp.status == 200
        # Cleared, then a "failed" event so clients drop the overlay, then cleared
        # again -- a single clear would race clients that had not yet polled.
        assert state.clear_update_progress.call_count == 2
        state.push_update_progress.assert_called_once_with("failed", "Update cancelled by user")


class TestLogLevel:
    """``POST /api/logs/level`` and its getter."""

    @pytest.fixture(autouse=True)
    def _restore_level(self):
        root = logging.getLogger("kiro_crew")
        saved = root.level
        yield
        root.setLevel(saved)

    @pytest.mark.asyncio
    async def test_rejects_a_body_that_is_not_json(self):
        resp = await updates.api_log_level(_request(ValueError("bad json")))
        assert resp.status == 400

    @pytest.mark.parametrize("level", ["TRACE", "", "verbose", "CRITICAL"])
    @pytest.mark.asyncio
    async def test_rejects_a_level_outside_the_map(self, level):
        before = logging.getLogger("kiro_crew").level
        resp = await updates.api_log_level(_request({"level": level}))
        assert resp.status == 400
        # A rejected level must not have been applied on the way to the refusal.
        assert logging.getLogger("kiro_crew").level == before

    @pytest.mark.asyncio
    async def test_applies_and_persists_a_valid_level_case_insensitively(self, monkeypatch):
        saved: list[str] = []
        cfg = MagicMock()
        cfg.save = lambda: saved.append(cfg.agent.log_level)
        monkeypatch.setattr(updates.KiroCrewConfig, "load", staticmethod(lambda: cfg))

        resp = await updates.api_log_level(_request({"level": "warning"}))

        assert resp.status == 200
        payload = json.loads(resp.body.decode())
        assert payload == {"ok": True, "level": "WARNING", "persisted": True}
        assert logging.getLogger("kiro_crew").level == logging.WARNING
        assert saved == ["WARNING"]

    @pytest.mark.asyncio
    async def test_a_failed_persist_still_applies_the_level_and_says_so(self, monkeypatch):
        """The runtime change is the point; persistence is best-effort.

        Reporting ``persisted: False`` rather than 500 keeps a read-only config
        from blocking a debugging session.
        """
        monkeypatch.setattr(
            updates.KiroCrewConfig,
            "load",
            staticmethod(MagicMock(side_effect=OSError("read-only fs"))),
        )

        resp = await updates.api_log_level(_request({"level": "DEBUG"}))

        payload = json.loads(resp.body.decode())
        assert payload["ok"] is True
        assert payload["persisted"] is False
        assert logging.getLogger("kiro_crew").level == logging.DEBUG

    @pytest.mark.asyncio
    async def test_the_getter_reports_the_live_level(self):
        logging.getLogger("kiro_crew").setLevel(logging.ERROR)
        resp = await updates.api_log_level_get(_request())
        assert json.loads(resp.body.decode()) == {"level": "ERROR"}


def _record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("kiro_crew.test", level, __file__, 1, msg, None, None)


class TestQueueLogHandler:
    """The per-connection SSE handler must never raise into ``logging``."""

    def test_formats_each_record_as_one_sse_payload(self):
        queue: asyncio.Queue[str] = asyncio.Queue()
        handler = updates._QueueLogHandler(queue)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

        handler.emit(_record("first", logging.WARNING))

        assert json.loads(queue.get_nowait()) == {
            "level": "WARNING",
            "msg": "WARNING first",
        }

    def test_a_full_queue_drops_the_entry_instead_of_raising(self):
        """``logging`` calls this from arbitrary code; raising would break callers."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        handler = updates._QueueLogHandler(queue)
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record("kept"))
        handler.emit(_record("dropped"))

        assert queue.qsize() == 1
        assert json.loads(queue.get_nowait())["msg"] == "kept"


class TestSafeWsSend:
    """A dead WebSocket subscriber is removed, not retried."""

    @pytest.mark.asyncio
    async def test_a_live_subscriber_is_kept(self):
        ws = MagicMock()
        ws.send_str = AsyncMock()
        state = MagicMock()
        state._ws_log_subscribers = {ws}

        await updates._safe_ws_send(ws, "payload", state)

        ws.send_str.assert_awaited_once_with("payload")
        assert state._ws_log_subscribers == {ws}

    @pytest.mark.asyncio
    async def test_a_dead_subscriber_is_discarded(self):
        ws = MagicMock()
        ws.send_str = AsyncMock(side_effect=ConnectionResetError("gone"))
        state = MagicMock()
        state._ws_log_subscribers = {ws}

        await updates._safe_ws_send(ws, "payload", state)

        assert state._ws_log_subscribers == set()


class TestRingLogHandler:
    """The always-on ring buffer plus its WebSocket fan-out."""

    def test_set_state_keeps_no_loop_of_its_own(self):
        """The handler defers to the dashboard's one serving loop.

        Its `emit()` runs on arbitrary threads while `set_state` captured a loop
        ONCE, so any caller attaching the state from a non-loop context latches
        None permanently and every later fan-out is dropped with nothing
        surfaced. Today's only production caller runs inside `start_dashboard`,
        so the capture did succeed -- this pins the shape, not a live bug.
        Resolving through the state at emit time removes the latch entirely.
        """
        handler = updates._RingLogHandler(collections.deque(maxlen=4))
        handler.set_state(MagicMock())
        assert not hasattr(handler, "_loop")

    def test_emit_appends_to_the_ring_and_honours_maxlen(self):
        ring: collections.deque[str] = collections.deque(maxlen=2)
        handler = updates._RingLogHandler(ring, 2)
        handler.setFormatter(logging.Formatter("%(message)s"))

        for text in ("one", "two", "three"):
            handler.emit(_record(text))

        assert [json.loads(entry)["msg"] for entry in ring] == ["two", "three"]

    def test_a_formatting_failure_is_swallowed(self):
        ring: collections.deque[str] = collections.deque(maxlen=2)
        handler = updates._RingLogHandler(ring)
        broken = MagicMock()
        broken.format.side_effect = ValueError("bad format string")
        handler.setFormatter(broken)

        handler.emit(_record())

        assert list(ring) == []

    @pytest.mark.asyncio
    async def test_emit_fans_out_to_every_websocket_subscriber(self):
        ring: collections.deque[str] = collections.deque(maxlen=4)
        handler = updates._RingLogHandler(ring)
        handler.setFormatter(logging.Formatter("%(message)s"))
        ws = MagicMock()
        ws.send_str = AsyncMock()
        state = MagicMock()
        state._ws_log_subscribers = {ws}
        state.serving_loop = asyncio.get_running_loop()
        handler.set_state(state)

        handler.emit(_record("broadcast me"))
        # The fan-out is scheduled via call_soon_threadsafe, so it lands on a
        # later loop iteration rather than inside emit().
        for _ in range(50):
            await asyncio.sleep(0.005)
            if ws.send_str.await_count:
                break

        ws.send_str.assert_awaited_once()
        payload = json.loads(ws.send_str.await_args[0][0])
        assert payload == {"type": "log", "data": {"level": "INFO", "msg": "broadcast me"}}
        assert json.loads(ring[0])["msg"] == "broadcast me"

    def test_emit_with_no_subscribers_still_fills_the_ring(self):
        ring: collections.deque[str] = collections.deque(maxlen=4)
        handler = updates._RingLogHandler(ring)
        handler.setFormatter(logging.Formatter("%(message)s"))
        state = MagicMock()
        state._ws_log_subscribers = set()
        handler.set_state(state)

        handler.emit(_record("ring only"))

        assert json.loads(ring[0])["msg"] == "ring only"

    @pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
    def test_a_closed_loop_drops_the_fan_out_but_keeps_the_ring(self):
        """The gateway shuts its loop down while the handler stays attached.

        ``call_soon_threadsafe`` then raises, and losing the ring entry over a
        subscriber that can no longer be reached would blind the Logs page during
        exactly the shutdown a reader wants to see.

        The send coroutine is built BEFORE the scheduling call, so this path also
        abandons it un-awaited -- harmless (nothing has run yet) but the reason
        for the warning filter above.
        """
        ring: collections.deque[str] = collections.deque(maxlen=4)
        handler = updates._RingLogHandler(ring)
        handler.setFormatter(logging.Formatter("%(message)s"))
        state = MagicMock()
        state._ws_log_subscribers = {MagicMock()}
        handler._state = state
        state.serving_loop = MagicMock()
        state.serving_loop.call_soon_threadsafe.side_effect = RuntimeError(
            "event loop is closed"
        )

        handler.emit(_record("during shutdown"))

        assert json.loads(ring[0])["msg"] == "during shutdown"

    def test_install_is_idempotent_and_attaches_exactly_one_handler(self, monkeypatch):
        root = logging.getLogger("kiro_crew")
        monkeypatch.setattr(updates, "_log_ring_handler_installed", False)
        monkeypatch.setattr(updates, "_log_ring_handler", None)
        before = list(root.handlers)
        try:
            first = updates.install_log_ring_handler()
            second = updates.install_log_ring_handler()
            assert first is not None
            # Second call is a no-op: two ring handlers would double every entry.
            assert second is first
            assert len([h for h in root.handlers if h not in before]) == 1
        finally:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)


def _stub_stream(monkeypatch, *, prepare_raises: BaseException | None = None) -> list[bytes]:
    """Capture what a handler writes without opening a socket."""
    writes: list[bytes] = []

    async def _prepare(self, request):  # noqa: ANN001 - stub mirrors aiohttp
        if prepare_raises is not None:
            raise prepare_raises
        return None

    async def _write(self, data):  # noqa: ANN001 - stub mirrors aiohttp
        writes.append(bytes(data))

    monkeypatch.setattr(web.StreamResponse, "prepare", _prepare)
    monkeypatch.setattr(web.StreamResponse, "write", _write)
    return writes


class TestLogsStream:
    """``GET /api/logs`` — ring replay, then a live tail."""

    @pytest.fixture(autouse=True)
    def _own_shutdown_event(self, monkeypatch):
        """The module binds ``shutdown_event`` at import, so patch it there."""
        event = asyncio.Event()
        monkeypatch.setattr(updates, "shutdown_event", event)
        return event

    @pytest.mark.asyncio
    async def test_a_client_that_hangs_up_during_prepare_is_not_an_error(self, monkeypatch):
        _stub_stream(monkeypatch, prepare_raises=ClientConnectionResetError("gone"))
        updates._log_ring.append(json.dumps({"level": "INFO", "msg": "x"}))
        resp = await updates.api_logs(_request(query={"lines": "10"}))
        assert isinstance(resp, web.StreamResponse)

    @pytest.mark.asyncio
    async def test_a_client_that_hangs_up_during_replay_is_not_an_error(self, monkeypatch):
        writes = _stub_stream(monkeypatch)

        async def _boom(self, data):  # noqa: ANN001 - stub mirrors aiohttp
            raise ConnectionResetError("gone")

        updates._log_ring.append(json.dumps({"level": "INFO", "msg": "x"}))
        monkeypatch.setattr(web.StreamResponse, "write", _boom)

        await updates.api_logs(_request(query={"lines": "5"}))

        assert writes == []
        # The queue handler is only installed AFTER the replay, so an abort here
        # must not leave one attached to the logger.
        assert not any(
            isinstance(h, updates._QueueLogHandler)
            for h in logging.getLogger("kiro_crew").handlers
        )

    @pytest.mark.parametrize("raw", ["not-a-number", "", "1e5"])
    @pytest.mark.asyncio
    async def test_an_unparseable_lines_param_falls_back_to_the_default(self, monkeypatch, raw):
        writes = _stub_stream(monkeypatch)
        for index in range(250):
            updates._log_ring.append(json.dumps({"level": "INFO", "msg": f"m{index}"}))
        event = updates.shutdown_event
        event.set()  # replay only, no live tail

        await updates.api_logs(_request(query={"lines": raw}))

        assert len(writes) == 200
        assert json.loads(writes[0].decode().removeprefix("data: ").strip())["msg"] == "m50"

    @pytest.mark.asyncio
    async def test_the_replay_is_capped_by_the_lines_param(self, monkeypatch):
        writes = _stub_stream(monkeypatch)
        for index in range(10):
            updates._log_ring.append(json.dumps({"level": "INFO", "msg": f"m{index}"}))
        updates.shutdown_event.set()

        await updates.api_logs(_request(query={"lines": "3"}))

        replayed = [json.loads(w.decode().removeprefix("data: ").strip())["msg"] for w in writes]
        # The TAIL of the ring, so the client sees the most recent history.
        assert replayed == ["m7", "m8", "m9"]

    @pytest.mark.asyncio
    async def test_live_entries_are_streamed_and_the_handler_is_removed_on_cancel(
        self, monkeypatch
    ):
        writes = _stub_stream(monkeypatch)
        root = logging.getLogger("kiro_crew")
        before = len(root.handlers)

        task = asyncio.ensure_future(updates.api_logs(_request(query={"lines": "1"})))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if any(isinstance(h, updates._QueueLogHandler) for h in root.handlers):
                break
        assert any(isinstance(h, updates._QueueLogHandler) for h in root.handlers)

        logging.getLogger("kiro_crew.stream").warning("live one")
        logging.getLogger("kiro_crew.stream").warning("live two")
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(writes) >= 2:
                break
        task.cancel()
        await task

        payloads = [json.loads(w.decode().removeprefix("data: ").strip())["msg"] for w in writes]
        assert any("live one" in p for p in payloads)
        assert any("live two" in p for p in payloads)
        # The finally block must detach the per-connection handler, or every
        # disconnected client would keep formatting records forever.
        assert len(root.handlers) == before

    @pytest.mark.asyncio
    async def test_an_idle_stream_emits_a_keepalive_comment(self, monkeypatch):
        """A silent logger must not let an intermediary time the connection out.

        The real wait is 30s, so the wait itself is short-circuited rather than
        slept through.
        """
        writes = _stub_stream(monkeypatch)
        real_wait_for = asyncio.wait_for

        async def _no_waiting(awaitable, timeout=None):
            if timeout == 30:
                awaitable.close()  # nothing has run yet; close it rather than leak it
                await asyncio.sleep(0.01)
                raise asyncio.TimeoutError
            return await real_wait_for(awaitable, timeout)

        monkeypatch.setattr(asyncio, "wait_for", _no_waiting)

        task = asyncio.ensure_future(updates.api_logs(_request()))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if b": keepalive\n\n" in writes:
                break
        task.cancel()
        await task

        assert b": keepalive\n\n" in writes


class TestDashboardStream:
    """The dashboard SSE endpoint — one event name per queued note type."""

    @pytest.fixture(autouse=True)
    def _own_shutdown_event(self, monkeypatch):
        event = asyncio.Event()
        monkeypatch.setattr(updates, "shutdown_event", event)
        return event

    def _request_with_queue(self, notes: list[dict]) -> MagicMock:
        req = _request()
        queue: asyncio.Queue[dict] = asyncio.Queue()
        for note in notes:
            queue.put_nowait(note)
        state = req.app["state"]
        state.register_sse.return_value = queue
        state.status_snapshot.return_value = {"sessions": 0, "update_available": True}
        return req

    @pytest.mark.asyncio
    async def test_a_client_that_hangs_up_during_prepare_is_not_an_error(self, monkeypatch):
        _stub_stream(monkeypatch, prepare_raises=ConnectionResetError("gone"))
        req = self._request_with_queue([])
        resp = await updates.api_stream(req)
        assert isinstance(resp, web.StreamResponse)
        # No queue was registered, so none can be leaked.
        req.app["state"].unregister_sse.assert_not_called()

    @pytest.mark.asyncio
    async def test_each_note_type_gets_its_own_sse_event_name(self, monkeypatch):
        writes = _stub_stream(monkeypatch)
        req = self._request_with_queue(
            [
                {"_type": "slots", "slots": "[]"},
                {"_type": "slot_title", "key": "s1", "title": "Renamed"},
                {"_type": "refresh", "kinds": "updating"},
                {"_type": "chat_message", "slot": "s1", "role": "assistant", "content": "hi"},
                {"kind": "toast", "text": "done"},
            ]
        )

        task = asyncio.ensure_future(updates.api_stream(req))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(writes) >= 6:
                break
        updates.shutdown_event.set()
        await asyncio.wait_for(task, timeout=5)

        text = b"".join(writes).decode()
        assert "event: slots\ndata: []\n\n" in text
        assert '"title": "Renamed"' in text
        assert "event: refresh\ndata: updating\n\n" in text
        assert "event: chat_message" in text
        assert "event: notification" in text
        # The periodic dashboard frame carries the version and the update flag.
        dashboard = [w.decode() for w in writes if w.startswith(b"event: dashboard")]
        assert dashboard
        payload = json.loads(dashboard[0].split("data: ", 1)[1].strip())
        assert payload["update_available"] is True
        assert payload["version"] == updates._local_version
        # The per-client queue is always handed back, disconnect or not.
        req.app["state"].unregister_sse.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_disconnect_mid_stream_unregisters_the_queue(self, monkeypatch):
        """A leaked queue keeps growing for a client that will never read it."""
        _stub_stream(monkeypatch)
        req = self._request_with_queue([])

        async def _boom(self, data):  # noqa: ANN001 - stub mirrors aiohttp
            raise ClientConnectionResetError("gone")

        monkeypatch.setattr(web.StreamResponse, "write", _boom)

        resp = await updates.api_stream(req)

        assert isinstance(resp, web.StreamResponse)
        req.app["state"].unregister_sse.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_queue_that_empties_mid_drain_ends_the_drain(self, monkeypatch):
        """``empty()`` then ``get_nowait()`` is not atomic under a second consumer.

        The defensive break is what keeps a raced drain from raising out of the
        stream and dropping the client.
        """
        _stub_stream(monkeypatch)
        req = _request()

        class _RacingQueue:
            def __init__(self) -> None:
                self.probes = 0

            def empty(self) -> bool:
                self.probes += 1
                return self.probes > 1

            def get_nowait(self) -> dict:
                raise asyncio.QueueEmpty

        queue = _RacingQueue()
        state = req.app["state"]
        state.register_sse.return_value = queue
        state.status_snapshot.return_value = {"sessions": 0}

        task = asyncio.ensure_future(updates.api_stream(req))
        await asyncio.sleep(0.05)
        updates.shutdown_event.set()
        resp = await asyncio.wait_for(task, timeout=5)

        assert isinstance(resp, web.StreamResponse)
        state.unregister_sse.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed_and_still_unregisters(self, monkeypatch):
        _stub_stream(monkeypatch)
        req = self._request_with_queue([])

        task = asyncio.ensure_future(updates.api_stream(req))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if req.app["state"].register_sse.called:
                break
        task.cancel()
        resp = await task

        assert isinstance(resp, web.StreamResponse)
        req.app["state"].unregister_sse.assert_called_once()


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
    async def test_a_second_caller_no_ops_while_a_check_is_in_flight(self, monkeypatch):
        calls: list[str] = []

        async def _count(capability) -> None:
            calls.append(capability.managed_by)

        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "wheel")
        monkeypatch.setattr(updates, "_check_release_feed", _count)
        with patch.object(updates, "_check_in_flight", True):
            await updates._do_update_check()
        assert calls == []

        await updates._do_update_check()
        assert calls == ["kirocrew"]

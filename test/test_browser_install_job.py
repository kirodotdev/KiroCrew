"""The gateway-owned browser install job and the routes that report it.

Every test fakes the installer: nothing here downloads, spawns npm, or touches
the real data home or Playwright cache.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.browser_cli import install as browser_cli_install
from kiro_crew.browser_cli import install_job as job_mod
from kiro_crew.dashboard.handlers import messaging as msg

_JOB_KEYS = {
    "id",
    "kind",
    "engine",
    "status",
    "stage",
    "started_at",
    "updated_at",
    "finished_at",
    "elapsed_s",
    "error_code",
    "error_detail",
}


class _Clock:
    """A settable wall clock for the job's timestamps."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(job_mod, "clock", fake)
    return fake


@pytest.fixture(autouse=True)
def _quiet_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status reads answer from a fixed detection result, never the host."""
    monkeypatch.setattr(
        msg.browser_cli_install,
        "detect",
        lambda: {
            "installed": True,
            "browser_ok": False,
            "browsers": {"chromium": False, "firefox": False, "webkit": False},
            "browser_status": {"chromium": "missing", "firefox": "missing", "webkit": "unknown"},
        },
    )
    monkeypatch.setattr(msg.browser_cli_token, "has_token", lambda: False)
    monkeypatch.setattr(msg, "_sel", lambda: MagicMock())


def _state() -> Any:
    state = type("S", (), {})()
    state.owner_id = "the-owner"
    state._browser_install_task = None
    state._browser_install_job = None
    state._browser_install_scope = None
    return state


def _request(state: Any, path: str, body: dict[str, Any] | None = None) -> Any:
    req = MagicMock()
    req.path = path
    claims = {"app": "", "user": "the-owner"}
    req.get = lambda key, default=None: claims.get(key, default)
    req.__contains__ = lambda self_inner, key: key in claims
    req.__getitem__ = lambda self_inner, key: claims[key]

    async def _json() -> dict[str, Any]:
        return body or {}

    req.json = _json
    req.app = {"state": state}
    return req


def _payload(resp: Any) -> dict[str, Any]:
    return json.loads(resp.text)


class _Gate:
    """A fake installer that parks in its worker thread until released."""

    def __init__(self, result: dict[str, Any] | None = None, stages: tuple[str, ...] = ()) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = result or {"ok": True, "steps": [{"name": "x", "ok": True}]}
        self.stages = stages
        self.calls: list[tuple[Any, ...]] = []
        self.on_stage: Any = None

    def install(self, on_stage: Any = None) -> dict[str, Any]:
        self.calls.append(("install",))
        return self._park(on_stage)

    def install_browser(self, engine: str, on_stage: Any = None) -> dict[str, Any]:
        self.calls.append(("install_browser", engine))
        return self._park(on_stage)

    def _park(self, on_stage: Any) -> dict[str, Any]:
        self.on_stage = on_stage
        for stage in self.stages:
            on_stage(stage)
        self.entered.set()
        assert self.release.wait(10), "test never released the installer"
        return self.result


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


def _wire(monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> None:
    monkeypatch.setattr(msg.browser_cli_install, "install", gate.install)
    monkeypatch.setattr(msg.browser_cli_install, "install_browser", gate.install_browser)


class TestTheJobRecord:
    def test_a_new_job_is_running_at_preparing(self, clock):
        job = job_mod.BrowserInstallJob.start(job_mod.KIND_ENGINE_DOWNLOAD, "firefox")
        snap = job.snapshot()
        assert set(snap) == _JOB_KEYS
        assert snap["status"] == "running"
        assert snap["stage"] == "preparing"
        assert snap["engine"] == "firefox"
        assert snap["finished_at"] is None
        assert snap["started_at"].endswith("Z")
        assert len(snap["id"]) == 32

    def test_elapsed_is_computed_at_read_time_and_frozen_when_finished(self, clock):
        job = job_mod.BrowserInstallJob.start(job_mod.KIND_CLI_SETUP)
        clock.now += 12.34
        assert job.snapshot()["elapsed_s"] == 12.3
        job.finish(job.id, job_mod.STATUS_SUCCEEDED)
        clock.now += 100
        assert job.snapshot()["elapsed_s"] == 12.3

    def test_a_stale_id_or_finished_job_ignores_updates(self, clock):
        job = job_mod.BrowserInstallJob.start(job_mod.KIND_CLI_SETUP)
        assert job.apply_stage("someone-else", "installing_cli") is False
        assert job.apply_stage(job.id, "not-a-stage") is False
        assert job.apply_stage(job.id, "installing_cli") is True
        assert job.finish(job.id, job_mod.STATUS_FAILED, "step_failed", "x") is True
        assert job.apply_stage(job.id, "finishing") is False
        assert job.finish(job.id, job_mod.STATUS_SUCCEEDED) is False
        assert job.snapshot()["stage"] == "installing_cli"
        assert job.snapshot()["status"] == "failed"


class TestOutcomeClassification:
    def test_success(self):
        assert job_mod.outcome_of({"ok": True, "steps": []}, "install") == (
            "succeeded",
            None,
            None,
        )

    def test_the_last_step_decides_and_names_itself(self):
        result = {
            "ok": False,
            "steps": [
                {"name": "a", "ok": False, "returncode": 1, "stderr": "recovered"},
                {"name": "install-browser", "ok": False, "returncode": 1, "stderr": "boom"},
            ],
        }
        status, code, detail = job_mod.outcome_of(result, "install")
        assert (status, code) == ("failed", "step_failed")
        assert detail == "install-browser: boom"

    def test_a_timeout_return_code_is_a_timeout(self):
        result = {
            "ok": False,
            "steps": [{"name": "npm-install-global", "ok": False, "returncode": 124}],
        }
        assert job_mod.outcome_of(result, "install")[1] == "timeout"

    def test_an_interrupted_return_code_is_an_interruption(self):
        result = {
            "ok": False,
            "steps": [
                {
                    "name": "install-browser",
                    "ok": False,
                    "returncode": browser_cli_install.INTERRUPTED_RC,
                }
            ],
        }
        assert job_mod.outcome_of(result, "install")[:2] == ("interrupted", "interrupted")

    @pytest.mark.parametrize(
        ("rc", "expected"),
        [
            (browser_cli_install.INTERRUPTED_RC, ("interrupted", "interrupted")),
            (browser_cli_install.TIMEOUT_RC, ("failed", "timeout")),
        ],
        ids=["interrupted", "timed-out"],
    )
    def test_an_interrupted_or_timed_out_download_detail_carries_no_deps_hint(
        self, monkeypatch, rc, expected
    ):
        """The installer's step is fed through ``outcome_of`` unchanged: neither
        layer may attach the missing-libraries remedy to a stopped download."""
        hint = "sudo dnf install -y nss"
        monkeypatch.setattr(browser_cli_install.os_deps, "missing_deps_hint", lambda engine: hint)
        monkeypatch.setattr(
            browser_cli_install, "_run", lambda argv, timeout: (rc, "", "stopped early")
        )
        step = browser_cli_install._download_browser(["pw"], "chromium")[0]

        status, code, detail = job_mod.outcome_of({"ok": False, "steps": [step]}, "install")

        assert (status, code) == expected
        assert detail == "install-browser-chromium: stopped early"
        assert hint not in detail

    def test_detail_is_redacted_before_it_is_truncated(self):
        head = "//r.example/:_authToken=" + "z" * 200 + "\n"
        secret = "http://admin:LEAKED_SECRET" + "x" * 110 + "@proxy.example.com"
        stderr = head * 35 + secret
        result = {
            "ok": False,
            "steps": [{"name": "npm", "ok": False, "returncode": 1, "stderr": stderr}],
        }
        _status, _code, detail = job_mod.outcome_of(result, "install")
        assert detail is not None
        assert len(detail) <= job_mod.ERROR_DETAIL_CAP
        assert "LEAKED_SECRET" not in detail
        assert "zzzz" not in detail

    def test_a_long_detail_reserves_space_for_the_hint_and_keeps_the_stderr_tail(self):
        hint = "sudo dnf install -y nss"
        stderr = (
            "x" * 3_000
            + "\nNPM_TOKEN=ghp_1234567890abcdefABCDEF1234567890abcd"
            + "\nMissing libraries:\n    libgtk-4.so.1"
            + f"\n\n{hint}"
        )
        result = {
            "ok": False,
            "steps": [
                {
                    "name": "install-browser",
                    "ok": False,
                    "returncode": 1,
                    "stderr": stderr,
                    "hint": hint,
                }
            ],
        }

        _status, _code, detail = job_mod.outcome_of(result, "install")

        assert detail is not None
        assert len(detail) <= job_mod.ERROR_DETAIL_CAP
        assert detail.endswith(hint)
        assert "Missing libraries:\n    libgtk-4.so.1" in detail
        assert "ghp_1234567890abcdefABCDEF1234567890abcd" not in detail


class TestTheRoutes:
    def test_the_job_is_published_before_the_installer_runs(self, monkeypatch, clock):
        gate = _Gate()
        _wire(monkeypatch, gate)

        async def _go() -> None:
            state = _state()
            seen_at_call: list[Any] = []
            real = gate.install_browser

            def install_browser(engine: str, on_stage: Any = None) -> dict[str, Any]:
                job = state._browser_install_job
                seen_at_call.append((job.status, job.stage, job.engine) if job else None)
                return real(engine, on_stage)

            monkeypatch.setattr(msg.browser_cli_install, "install_browser", install_browser)
            resp = await msg.api_browser_engine_install(
                _request(state, "/api/browser/engine", {"engine": "firefox"})
            )
            body = _payload(resp)
            assert resp.status == 200
            # The POST response names the job whether or not the worker started.
            assert body["installing"] is True
            assert body["install_job"]["kind"] == "engine_download"
            assert body["install_job"]["engine"] == "firefox"
            assert body["install_job"]["stage"] == "preparing"
            assert body["last_error"] is None
            await asyncio.to_thread(gate.entered.wait, 5)
            # The job existed before the installer's first line ran.
            assert seen_at_call == [("running", "preparing", "firefox")]
            gate.release.set()
            await state._browser_install_task

        asyncio.run(_go())

    def test_stages_progress_and_the_terminal_job_is_retained(self, monkeypatch, clock):
        gate = _Gate(stages=("installing_cli", "downloading_browser"))
        _wire(monkeypatch, gate)

        async def _go() -> None:
            state = _state()
            await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            await asyncio.to_thread(gate.entered.wait, 5)
            await _until(lambda: state._browser_install_job.stage == "downloading_browser")
            got = _payload(
                await msg.api_browser_install_get(_request(state, "/api/browser/install"))
            )
            assert got["install_job"]["stage"] == "downloading_browser"
            assert got["install_job"]["engine"] is None
            clock.now += 30
            gate.release.set()
            await state._browser_install_task
            done = _payload(
                await msg.api_browser_install_get(_request(state, "/api/browser/install"))
            )
            assert done["installing"] is False
            assert done["install_job"]["status"] == "succeeded"
            assert done["install_job"]["elapsed_s"] == 30.0
            assert done["install_job"]["finished_at"] is not None
            assert done["last_error"] is None

        asyncio.run(_go())

    def test_cli_setup_during_an_engine_download_is_a_conflict(self, monkeypatch, clock):
        gate = _Gate()
        _wire(monkeypatch, gate)

        async def _go() -> None:
            state = _state()
            await msg.api_browser_engine_install(
                _request(state, "/api/browser/engine", {"engine": "webkit"})
            )
            resp = await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            body = _payload(resp)
            assert resp.status == 409
            assert body["code"] == "install_already_running"
            assert body["install_job"]["engine"] == "webkit"
            gate.release.set()
            await state._browser_install_task
            assert [c[0] for c in gate.calls] == ["install_browser"]

        asyncio.run(_go())

    @pytest.mark.parametrize("first", ["cli", "engine"])
    def test_an_engine_download_during_any_job_is_a_conflict(self, monkeypatch, clock, first):
        gate = _Gate()
        _wire(monkeypatch, gate)

        async def _go() -> None:
            state = _state()
            if first == "cli":
                await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            else:
                await msg.api_browser_engine_install(
                    _request(state, "/api/browser/engine", {"engine": "chromium"})
                )
            active_id = state._browser_install_job.id
            resp = await msg.api_browser_engine_install(
                _request(state, "/api/browser/engine", {"engine": "firefox"})
            )
            body = _payload(resp)
            assert resp.status == 409
            assert body["install_job"]["id"] == active_id
            gate.release.set()
            await state._browser_install_task

        asyncio.run(_go())

    def test_a_second_cli_setup_is_folded_into_the_running_one(self, monkeypatch, clock):
        gate = _Gate()
        _wire(monkeypatch, gate)

        async def _go() -> None:
            state = _state()
            first = _payload(
                await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            )
            second_resp = await msg.api_browser_install_start(
                _request(state, "/api/browser/install")
            )
            second = _payload(second_resp)
            assert second_resp.status == 200
            assert second["install_job"]["id"] == first["install_job"]["id"]
            gate.release.set()
            await state._browser_install_task
            assert gate.calls == [("install",)]

        asyncio.run(_go())

    def test_a_malformed_engine_is_still_a_400_while_a_job_runs(self, monkeypatch, clock):
        gate = _Gate()
        _wire(monkeypatch, gate)

        async def _go() -> None:
            state = _state()
            await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            resp = await msg.api_browser_engine_install(
                _request(state, "/api/browser/engine", {"engine": "safari"})
            )
            assert resp.status == 400
            gate.release.set()
            await state._browser_install_task

        asyncio.run(_go())

    @pytest.mark.parametrize(
        ("result", "error_code"),
        [
            (
                {
                    "ok": False,
                    "steps": [
                        {
                            "name": "install-browser-firefox",
                            "ok": False,
                            "returncode": 1,
                            "stderr": "//r/:_authToken=s3cr3tvalue denied",
                        }
                    ],
                },
                "step_failed",
            ),
            (
                {
                    "ok": False,
                    "steps": [{"name": "install-browser-firefox", "ok": False, "returncode": 124}],
                },
                "timeout",
            ),
        ],
    )
    def test_failed_outcomes_are_reported_redacted(self, monkeypatch, clock, result, error_code):
        gate = _Gate(result=result)
        gate.release.set()
        _wire(monkeypatch, gate)

        async def _go() -> dict[str, Any]:
            state = _state()
            await msg.api_browser_engine_install(
                _request(state, "/api/browser/engine", {"engine": "firefox"})
            )
            await state._browser_install_task
            return _payload(
                await msg.api_browser_install_get(_request(state, "/api/browser/install"))
            )

        body = asyncio.run(_go())
        job = body["install_job"]
        assert job["status"] == "failed"
        assert job["error_code"] == error_code
        assert job["error_detail"].startswith("install-browser-firefox")
        assert "s3cr3tvalue" not in job["error_detail"]
        assert body["last_error"] == job["error_detail"]
        assert body["installing"] is False

    def test_an_exception_is_an_exception_outcome(self, monkeypatch, clock):
        def boom(on_stage: Any = None) -> dict[str, Any]:
            raise RuntimeError("proxy https://user:sup3rs3cret@proxy.example.com/ refused")

        monkeypatch.setattr(msg.browser_cli_install, "install", boom)

        async def _go() -> dict[str, Any]:
            state = _state()
            await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            await state._browser_install_task
            return state._browser_install_job.snapshot()

        job = asyncio.run(_go())
        assert (job["status"], job["error_code"]) == ("failed", "exception")
        assert "sup3rs3cret" not in job["error_detail"]

    def test_a_late_stage_from_an_old_job_does_not_move_the_new_one(self, monkeypatch, clock):
        old = _Gate()
        _wire(monkeypatch, old)

        async def _go() -> None:
            state = _state()
            await msg.api_browser_engine_install(
                _request(state, "/api/browser/engine", {"engine": "firefox"})
            )
            await asyncio.to_thread(old.entered.wait, 5)
            old.release.set()
            await state._browser_install_task
            stale_callback = old.on_stage

            new = _Gate()
            _wire(monkeypatch, new)
            await msg.api_browser_engine_install(
                _request(state, "/api/browser/engine", {"engine": "webkit"})
            )
            # The first job's worker reports once more, after it was replaced.
            stale_callback("finishing")
            await asyncio.sleep(0.05)
            current = state._browser_install_job.snapshot()
            assert current["engine"] == "webkit"
            assert current["stage"] == "preparing"
            new.release.set()
            await state._browser_install_task

        asyncio.run(_go())

    def test_a_status_read_never_starts_an_installer(self, monkeypatch, clock):
        spawned: list[Any] = []
        monkeypatch.setattr(
            msg.browser_cli_install, "install", lambda **k: spawned.append(k) or {"ok": True}
        )
        monkeypatch.setattr(
            msg.browser_cli_install.subprocess, "Popen", lambda *a, **k: spawned.append(a)
        )

        async def _go() -> dict[str, Any]:
            state = _state()
            return _payload(
                await msg.api_browser_install_get(_request(state, "/api/browser/install"))
            )

        body = asyncio.run(_go())
        assert spawned == []
        assert body["install_job"] is None
        assert body["installing"] is False
        assert body["browser_status"]["webkit"] == "unknown"

    def test_new_fields_carry_no_token_or_host_path(self, monkeypatch, clock):
        gate = _Gate()
        gate.release.set()
        _wire(monkeypatch, gate)

        async def _go() -> dict[str, Any]:
            state = _state()
            await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            await state._browser_install_task
            return _payload(
                await msg.api_browser_install_get(_request(state, "/api/browser/install"))
            )

        job = asyncio.run(_go())["install_job"]
        assert set(job) == _JOB_KEYS
        assert not any(
            isinstance(v, str) and "/" in v and "://" not in v
            for k, v in job.items()
            if k != "error_detail"
        )


class TestShutdown:
    def test_stopping_terminates_the_scope_and_marks_the_job_interrupted(self, monkeypatch, clock):
        gate = _Gate()
        _wire(monkeypatch, gate)
        terminated: list[bool] = []

        async def _go() -> dict[str, Any]:
            state = _state()
            await msg.api_browser_install_start(_request(state, "/api/browser/install"))
            await asyncio.to_thread(gate.entered.wait, 5)
            scope = state._browser_install_scope
            real_terminate = scope.terminate

            def terminate() -> int:
                terminated.append(True)
                gate.release.set()
                return real_terminate()

            scope.terminate = terminate
            await msg.stop_browser_install(state)
            assert state._browser_install_task.done()
            return state._browser_install_job.snapshot()

        job = asyncio.run(_go())
        assert terminated
        assert job["status"] == "interrupted"
        assert job["error_code"] == "interrupted"

    def test_stopping_with_no_job_is_a_no_op(self):
        asyncio.run(msg.stop_browser_install(_state()))

    def test_stopping_a_running_task_returns_once_it_has_stopped(self):
        """The child's own CancelledError is the expected outcome of the stop, and
        a child that fails while winding down (``terminate`` raising) is equally
        swallowed: the contract is "never raises"."""

        async def _obedient() -> None:
            await asyncio.sleep(60)

        async def _breaks_on_cancel() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise RuntimeError("terminate blew up") from None

        async def _go() -> list[bool]:
            outcomes: list[bool] = []
            for factory in (_obedient, _breaks_on_cancel):
                state = _state()
                state._browser_install_task = asyncio.create_task(factory())
                await asyncio.sleep(0)
                await msg.stop_browser_install(state)
                outcomes.append(state._browser_install_task.done())
            return outcomes

        assert asyncio.run(_go()) == [True, True]

    def test_an_outer_cancellation_during_the_stop_propagates(self):
        """``asyncio.wait_for(self._shutdown(), timeout)`` expiring must still cancel
        the shutdown, even while it is waiting on the install task: swallowing the
        child's CancelledError must not swallow the caller's."""

        async def _slow_to_stop() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # The real task terminates its subprocesses first, which takes time.
                await asyncio.sleep(60)
                raise

        async def _go() -> tuple[bool, bool]:
            state = _state()
            child = asyncio.create_task(_slow_to_stop())
            state._browser_install_task = child
            await asyncio.sleep(0)
            stopper = asyncio.create_task(msg.stop_browser_install(state))
            await asyncio.sleep(0.05)
            assert not stopper.done()
            stopper.cancel()
            try:
                await stopper
                propagated = False
            except asyncio.CancelledError:
                propagated = True
            still_winding_down = not child.done()
            # Tidy the fake child so the loop closes cleanly.
            child.cancel()
            await asyncio.wait({child})
            return propagated, still_winding_down

        assert asyncio.run(_go()) == (True, True)

    def test_terminate_runs_off_the_event_loop(self, monkeypatch):
        """Windows ``terminate`` shells out to taskkill, so it must not block the loop."""
        loop_threads: list[bool] = []

        class _Scope:
            def terminate(self) -> int:
                try:
                    asyncio.get_running_loop()
                    loop_threads.append(True)
                except RuntimeError:
                    loop_threads.append(False)
                return 0

        asyncio.run(msg._terminate_install_scope(_Scope()))
        assert loop_threads == [False]

    def test_shutdown_kills_a_real_tracked_installer_child(self, monkeypatch, clock, tmp_path):
        """The worker's actual subprocess is killed, not only the awaiting task."""
        import sys
        import time

        marker = tmp_path / "child.started"
        argv = [
            sys.executable,
            "-c",
            f"import time; open({str(marker)!r}, 'w').close(); time.sleep(60)",
        ]

        def slow_install(on_stage: Any = None) -> dict[str, Any]:
            rc, _out, err = browser_cli_install._run(argv, 60.0, cwd=str(tmp_path))
            return {
                "ok": rc == 0,
                "steps": [
                    {"name": "npm-install-global", "ok": rc == 0, "returncode": rc, "stderr": err}
                ],
            }

        monkeypatch.setattr(msg.browser_cli_install, "install", slow_install)

        async def _go() -> dict[str, Any]:
            state = _state()
            try:
                await msg.api_browser_install_start(_request(state, "/api/browser/install"))
                await _until(marker.exists, timeout=15)
                started = time.monotonic()
                await msg.stop_browser_install(state)
                # The worker thread returns once its child is dead.
                await _until(lambda: not state._browser_install_scope._children, timeout=15)
                assert time.monotonic() - started < 15
                return state._browser_install_job.snapshot()
            finally:
                await msg.stop_browser_install(state)

        job = asyncio.run(_go())
        assert job["status"] == "interrupted"

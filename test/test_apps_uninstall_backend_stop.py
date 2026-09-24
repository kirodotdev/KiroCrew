"""An uninstall must never report success while the app's backend still runs.

Both entry points that remove an app stop its backend on positive evidence.

The CLI is a separate process from the gateway, so it holds no handle on the
child the gateway spawned: stopping the backend locally is not available to it.
It hands the uninstall to a running gateway the way ``enable`` and ``disable``
do, carrying ``--purge-data``. When no gateway answers, it says plainly that it
stopped nothing instead of reporting success.

The HTTP handler stops the backend for every app, whatever ``resources`` holds.
That field is read from the app's own installed metadata, so honouring it lets
an app declare ``resources: "app"`` and switch off its own teardown. The
scheduler cleanup is ungated for the same reason, and because an ``app:<name>``
job is persisted in the gateway's own cron store and fired by the gateway's own
service whatever that field says.

After the stop the port is OBSERVED, because the stop's boolean answers
``False`` both for "nothing to stop" and for "something runs that I did not
stop", and ``True`` only for "the process I tracked is gone" -- which says
nothing about a worker the app spawned for itself. A port still accepting
connections is reported as ``unstopped_backend_port``.
"""

from __future__ import annotations

import argparse
import io
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import cli_commands as cc


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


class _FakeResponse:
    """Minimal context-manager stand-in for ``urlopen``'s return value."""

    def __init__(self, payload: Any) -> None:
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _manifest(entry_point: str) -> MagicMock:
    manifest = MagicMock()
    manifest.backend.entryPoint = entry_point
    return manifest


class TestCliUninstallDelegatesToTheGateway:
    """The CLI cannot signal another process's child, so it must ask the gateway."""

    def _drive(self, *, purge_data: bool) -> tuple[list[Any], MagicMock]:
        requests: list[Any] = []

        def _open(request: Any, *, timeout: int, socket_path: Any) -> _FakeResponse:
            requests.append(request)
            if request.full_url.endswith("/api/token/local?ttl=2m"):
                return _FakeResponse({"token": "dashboard-credential"})
            return _FakeResponse({"ok": True, "message": "Uninstalled demo"})

        with (
            patch(
                "kiro_crew.app_lifecycle_client.resolve_client_port_ex", return_value=(8123, True)
            ),
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value="secret"),
            patch("kiro_crew.app_lifecycle_client.unix_socket_urlopen", side_effect=_open),
            patch("kiro_crew.cli_commands.uninstall_app") as local_uninstall,
            patch("kiro_crew.cli_commands.deregister_app") as local_deregister,
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.trust_grant_removal_blocked", return_value=""),
        ):
            cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=purge_data))
        local_deregister.assert_not_called()
        return requests, local_uninstall

    def test_a_running_gateway_performs_the_uninstall(self) -> None:
        """The local path must not run: it is the one that cannot stop the backend."""
        requests, local_uninstall = self._drive(purge_data=False)

        assert "/api/apps/demo/uninstall?" in requests[1].full_url
        assert requests[1].get_method() == "POST"
        local_uninstall.assert_not_called()

    @pytest.mark.parametrize("purge_data", [True, False])
    def test_the_purge_flag_travels_in_the_request_body(self, purge_data: bool) -> None:
        """The handler defaults an absent body to "preserve data".

        So a bodyless delegated request would turn ``--purge-data`` into a silent
        data-preserving uninstall — the flag has to be on the wire.
        """
        requests, _ = self._drive(purge_data=purge_data)

        action = requests[1]
        assert action.get_header("Content-type") == "application/json"
        assert json.loads(action.data) == {"purge_data": purge_data}


class TestCliFileOnlyUninstallDoesNotClaimAStop:
    """With no gateway reachable the CLI stops nothing, and must say so."""

    def _drive(self, *, entry_point: str, recorded_port: int | None = None) -> str:
        with (
            patch("kiro_crew.app_lifecycle_client.read_local_secret", return_value=""),
            patch("kiro_crew.cli_commands.trust_grant_removal_blocked", return_value=""),
            patch("kiro_crew.cli_commands._cleanup_app_crons_from_scheduler"),
            patch("kiro_crew.cli_commands.deregister_app"),
            patch("kiro_crew.cli_commands.get_app_manifest", return_value=_manifest(entry_point)),
            patch("kiro_crew.cli_commands.recorded_backend_port", return_value=recorded_port),
            patch(
                "kiro_crew.cli_commands.uninstall_app",
                return_value=MagicMock(ok=True, message="Uninstalled demo", error=""),
            ),
        ):
            import contextlib

            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cc._handle_app(_ns(app_action="uninstall", name="demo", purge_data=False))
        return err.getvalue()

    def test_an_app_declaring_a_backend_is_reported_as_not_stopped(self) -> None:
        assert "was not stopped" in self._drive(entry_point="backend/app.py")

    def test_an_app_with_no_backend_is_not_warned_about(self) -> None:
        """Neither signal says a backend exists, so the message stays quiet."""
        assert "was not stopped" not in self._drive(entry_point="")

    def test_a_recorded_port_reports_even_when_the_manifest_denies_a_backend(self) -> None:
        """The manifest is the app's own file, so it cannot be the only gate.

        An app trusted to run code can drop its ``entryPoint`` and would otherwise
        silence the one notice about the process it is still running. The pidfile
        lives under ``KIROCREW_HOME``, outside the app directory, so the app cannot
        reach it — and this path is the default on Windows and in sandboxed shells,
        where "no gateway reached" coexists with a gateway that started the backend.
        """
        assert "was not stopped" in self._drive(entry_point="", recorded_port=9137)

    def test_a_declaration_reports_even_with_no_recorded_port(self) -> None:
        """The other half: record-absence proves nothing, so it cannot gate either.

        A backend this gateway never tracked leaves no row, which is exactly the
        case where nothing may be concluded.
        """
        assert "was not stopped" in self._drive(entry_point="backend/app.py", recorded_port=None)

    def test_an_unreadable_manifest_is_reported_rather_than_assumed_clean(self) -> None:
        """Not knowing is not the same as knowing there is nothing to stop."""
        with patch("kiro_crew.cli_commands.get_app_manifest", side_effect=ValueError("bad json")):
            assert cc._app_declares_backend("demo") is True

    def test_a_missing_manifest_is_reported_rather_than_assumed_clean(self) -> None:
        with patch("kiro_crew.cli_commands.get_app_manifest", return_value=None):
            assert cc._app_declares_backend("demo") is True


@pytest.mark.asyncio
class TestUninstallHandlerStopsAndObservesTheBackend:
    async def _run(
        self,
        *,
        resources: str,
        live_port: int | None,
        recorded_port: int | None = 9137,
        declares_backend: bool = True,
    ) -> tuple[dict[str, Any], list[str], MagicMock]:
        """Drive the handler and return (response body, call order, deregister mock)."""
        calls: list[str] = []
        fake_app = {
            "name": "test-app",
            "manifest": {"backend": {"entryPoint": "backend/app.py"}} if declares_backend else {},
            "resources": resources,
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": "test-app"}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value={})

        def _recorded(name: str) -> int | None:
            calls.append("recorded_backend_port")
            return recorded_port

        def _stop(name: str, *args: Any, **kw: Any) -> bool:
            calls.append("stop_app_backend")
            return True

        def _unstopped(name: str, **kw: Any) -> int | None:
            calls.append("unstopped_backend_port")
            assert kw.get("port_hint") == recorded_port, "the hint must survive to the probe"
            return live_port

        async def _crons(name: str, service: Any) -> int:
            calls.append("cron_cleanup")
            return 0

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch(
                "kiro_crew.apps.routes.uninstall_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True, "name": "test-app"}),
            ),
            patch("kiro_crew.apps.routes._deregister_crons_with_retry", side_effect=_crons),
            patch("kiro_crew.apps.routes.recorded_backend_port", side_effect=_recorded),
            patch("kiro_crew.apps.routes.stop_app_backend", side_effect=_stop),
            patch("kiro_crew.apps.routes.unstopped_backend_port", side_effect=_unstopped),
            patch("kiro_crew.apps.routes.deregister_app", return_value=None) as deregister,
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
                return_value={"removable": [], "shared": [], "userInstalled": []},
            ),
            patch(
                "kiro_crew.apps.routes.clean_dependencies", new_callable=AsyncMock, return_value=[]
            ),
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)
        assert resp.status == 200, resp.status
        return json.loads(resp.body), calls, deregister

    async def test_the_backend_is_stopped_even_for_a_self_managed_app(self) -> None:
        """``resources`` comes from the app's own metadata.

        Gating the stop on it lets an app keep its process alive through its own
        uninstall, so the stop ignores the field.
        """
        _, calls, _ = await self._run(resources="app", live_port=None)

        assert "stop_app_backend" in calls

    async def test_a_self_managed_app_still_owns_its_registrations(self) -> None:
        """The split is deliberate: the process is stopped and the scheduler is
        cleaned, but the agents, skills and routes an app with ``resources: "app"``
        registered for itself are left alone."""
        _, _, deregister = await self._run(resources="app", live_port=None)

        deregister.assert_not_called()

    async def test_a_gateway_managed_app_is_still_deregistered(self) -> None:
        _, _, deregister = await self._run(resources="gateway", live_port=None)

        deregister.assert_called_once()

    async def test_the_scheduler_is_cleaned_even_for_a_self_managed_app(self) -> None:
        """An ``app:<name>`` job lives in the GATEWAY's cron store and is fired by
        the gateway's own service, which applies no app-admission check at fire
        time. Gating the cleanup on app-written metadata would leave those jobs
        running against a deleted app directory, and would make uninstall clean up
        less than the strictly less destructive disable.
        """
        _, calls, _ = await self._run(resources="app", live_port=None)

        assert "cron_cleanup" in calls

    async def test_the_recorded_port_is_read_before_the_stop_drops_it(self) -> None:
        """The stop drops the tracking entry and the pidfile record, which are the
        only gateway-owned evidence of the port the backend actually used."""
        _, calls, _ = await self._run(resources="gateway", live_port=None)

        assert calls == [
            "cron_cleanup",
            "recorded_backend_port",
            "stop_app_backend",
            "unstopped_backend_port",
        ]

    async def test_a_still_listening_port_is_reported_on_the_response(self) -> None:
        body, _, _ = await self._run(resources="gateway", live_port=9137)

        assert body["ok"] is True, "an app that cannot be removed is the worse outcome"
        assert any("9137" in w for w in body["warnings"])

    async def test_the_port_reaches_both_consumers(self) -> None:
        """``print_result`` renders ``warnings`` and never ``uninstall_log``; the
        dashboard's uninstall reads ``uninstall_log`` and never ``warnings``. A
        report in only one of them is invisible to the other caller, which is the
        clean-removal claim this change exists to stop.
        """
        body, _, _ = await self._run(resources="gateway", live_port=9137)

        assert any("9137" in w for w in body["warnings"])
        assert "9137" in body["uninstall_log"]

    async def test_a_silent_port_produces_no_warning(self) -> None:
        body, _, _ = await self._run(resources="gateway", live_port=None)

        assert "warnings" not in body
        assert "uninstall_log" not in body

    async def test_no_recorded_port_is_reported_as_unverifiable(self) -> None:
        """ "Nothing observed" is not "stopped", and here it cannot even be checked.

        With no gateway-recorded port the probe has only the DECLARED one to go on,
        and ``onUninstall`` has already run app-controlled code inside the directory
        that declares it — so an untracked fixed-port backend could have relabelled
        the port it still holds. Nothing later catches that: the files are gone and
        the stop dropped the pidfile record the next start would reap from.
        """
        body, _, _ = await self._run(resources="gateway", live_port=None, recorded_port=None)

        assert any("could not verify" in w for w in body["warnings"])
        assert "could not verify" in body["uninstall_log"]

    async def test_an_app_with_no_backend_is_not_warned_about_one(self) -> None:
        """The gate is a DECLARED backend, read from the record captured before the
        hook ran, so an app cannot suppress this by rewriting its manifest and an app
        that never had a backend collects no warning about one."""
        body, _, _ = await self._run(
            resources="gateway", live_port=None, recorded_port=None, declares_backend=False
        )

        assert "warnings" not in body

    async def test_an_observed_port_is_preferred_over_the_unverifiable_report(self) -> None:
        """One message, not both: a port that answers is the stronger statement."""
        body, _, _ = await self._run(resources="gateway", live_port=9137, recorded_port=9137)

        assert all("could not verify" not in w for w in body["warnings"])
        assert len(body["warnings"]) == 1


@pytest.mark.asyncio
class TestACronWriteFailureAbortsBeforeAnythingDestructive:
    """A removal that raised did not persist, and the count cannot say so.

    ``deregister_app_crons_from_service`` answers ``0`` both for "the app owned
    nothing" and for "the write never landed", and re-counting the rows cannot
    settle it either: a failing save leaves them filtered out of the in-memory job
    list until a reload. So the exception is the only evidence, and the uninstall
    consumes the entry point that reports it.
    """

    async def _run_with_cron_failure(self, exc: Exception) -> tuple[int, dict[str, Any], MagicMock]:
        """Drive the handler with the cron removal failing. Returns status, body, script mock."""
        fake_app = {
            "name": "test-app",
            "manifest": {"setup": {"onUninstall": "scripts/teardown.sh"}},
            "resources": "app",
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": "test-app"}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value={})

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch("kiro_crew.apps.routes._deregister_crons_with_retry", side_effect=exc),
            patch("kiro_crew.apps.routes._run_lifecycle_script", new_callable=AsyncMock) as script,
            patch("kiro_crew.apps.routes.uninstall_app") as uninstall,
            patch("kiro_crew.apps.routes.deregister_app") as deregister,
            patch("kiro_crew.apps.routes.stop_app_backend") as stop,
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)
            uninstall.assert_not_called()
            deregister.assert_not_called()
            stop.assert_not_called()
        return resp.status, json.loads(resp.body), script

    async def test_the_uninstall_is_refused_rather_than_half_completed(self) -> None:
        status, body, _ = await self._run_with_cron_failure(OSError(28, "No space left on device"))

        assert status == 409
        assert body["code"] == "cron_cleanup_failed"
        assert body["retryable"] is True, "a full disk can clear, and nothing destructive ran"

    async def test_the_non_idempotent_teardown_script_never_runs(self) -> None:
        """The abort is only safe because it happens before this point.

        Once ``onUninstall`` has run, a retry re-applies a teardown that is not
        idempotent, so an abort after it would be worse than the orphaned jobs.
        """
        _, _, script = await self._run_with_cron_failure(OSError(28, "No space left on device"))

        script.assert_not_called()


@pytest.mark.asyncio
class TestTheDisablePathKeepsItsExistingDisposition:
    """The widened scope stops at uninstall.

    ``deregister_app_crons_from_service`` is what the CLI's cron cleanup calls on
    both disable and uninstall, so its answer to a failed write is deliberately
    left alone: reporting one is a decision only a caller about to do something
    irreversible needs to make.
    """

    def _failing_sdk(self, exc: Exception) -> MagicMock:
        sdk = MagicMock()
        sdk.remove_all_async = AsyncMock(side_effect=exc)
        return MagicMock(return_value=sdk)

    async def test_an_unexpected_write_failure_is_still_answered_as_zero(self) -> None:
        from kiro_crew.apps.bridges import deregister_app_crons_from_service

        with (
            patch("kiro_crew.apps.bridges.CronSDK", self._failing_sdk(OSError(28, "ENOSPC"))),
            patch("kiro_crew.apps.bridges.sel", return_value=MagicMock()),
        ):
            assert await deregister_app_crons_from_service("demo", MagicMock()) == 0

    async def test_the_reporting_entry_point_raises_on_the_same_failure(self) -> None:
        """The two entry points differ on exactly this input, which is the point."""
        from kiro_crew.apps.bridges import deregister_app_crons_reporting_failures

        with (
            patch("kiro_crew.apps.bridges.CronSDK", self._failing_sdk(OSError(28, "ENOSPC"))),
            patch("kiro_crew.apps.bridges.sel", return_value=MagicMock()),
        ):
            with pytest.raises(OSError):
                await deregister_app_crons_reporting_failures("demo", MagicMock())

    async def test_an_app_that_owned_nothing_is_still_zero_on_both(self) -> None:
        """Zero stays the honest answer when it is true, on both entry points."""
        from kiro_crew.apps.bridges import (
            deregister_app_crons_from_service,
            deregister_app_crons_reporting_failures,
        )

        sdk = MagicMock()
        sdk.remove_all_async = AsyncMock(return_value=0)
        with (
            patch("kiro_crew.apps.bridges.CronSDK", MagicMock(return_value=sdk)),
            patch("kiro_crew.apps.bridges.sel", return_value=MagicMock()),
        ):
            assert await deregister_app_crons_from_service("demo", MagicMock()) == 0
            assert await deregister_app_crons_reporting_failures("demo", MagicMock()) == 0

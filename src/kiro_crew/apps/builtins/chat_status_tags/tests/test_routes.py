"""Tests for the reconcile-prompt HTTP surface.

Two properties matter most, mirroring the ops-mission-control route tests:

* **The enabled gate** — builtin routes exist from gateway startup even while the
  app is disabled, so every handler must refuse (403) when disabled.
* **Every route is namespaced** under ``/api/apps/chat-status-tags`` — a route
  escaping the namespace would shadow a core API.

Each handler test repoints ``KIROCREW_HOME`` at a temp dir so the prompt file
starts absent and the real data home is never touched.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.chat_status_tags import settings
from kiro_crew.apps.builtins.chat_status_tags.backend import routes
from kiro_crew.apps.builtins.chat_status_tags.prompts import DEFAULT_RECONCILE_PROMPT

_BASE = "/api/apps/chat-status-tags"
_PATH = f"{_BASE}/reconcile-prompt"
_REPAIR = f"{_BASE}/reconcile-cron/repair"
_JOB_NAME = "chat-status-tags/sdlc-tag-reconcile"
_APP_OWNER = "app:chat-status-tags"


def _job(
    *,
    name: str = _JOB_NAME,
    enabled: bool = True,
    cron_expr: str | None = "23 * * * *",
    created_by: str = _APP_OWNER,
    job_id: str = "cron-1",
) -> SimpleNamespace:
    """A CronJob stand-in exposing exactly the fields CronSDK/_cron_status read.

    ``created_by`` matters: ``CronSDK.list_jobs`` filters to jobs owned by the
    app, so a job with a foreign owner must be invisible to the status helper.
    """
    return SimpleNamespace(
        id=job_id,
        name=name,
        enabled=enabled,
        created_by=created_by,
        schedule=SimpleNamespace(cron_expr=cron_expr),
    )


class _StubCronService:
    """Minimal CronService surface CronSDK + the routes exercise.

    ``list_jobs(include_disabled=...)`` is the only read CronSDK.list_jobs uses;
    ``enable_job_async`` is the resume verb the repair route calls. ``resumed``
    records enable calls so a test can assert the resume happened.
    """

    def __init__(self, jobs: list[SimpleNamespace] | None = None) -> None:
        self._jobs = jobs or []
        self.resumed: list[tuple[str, bool]] = []
        #: (job_id, message) for every update_job_async that set a message.
        self.messaged: list[tuple[str, str]] = []

    def list_jobs(self, include_disabled: bool = False) -> list[SimpleNamespace]:
        return list(self._jobs)

    async def enable_job_async(self, job_id: str, enabled: bool = True) -> bool:
        self.resumed.append((job_id, enabled))
        for job in self._jobs:
            if job.id == job_id:
                job.enabled = enabled
                return True
        return False

    async def update_job_async(self, job_id: str, **kwargs: object) -> SimpleNamespace | None:
        for job in self._jobs:
            if job.id == job_id:
                if "message" in kwargs:
                    job.message = kwargs["message"]
                    self.messaged.append((job_id, str(kwargs["message"])))
                return job
        return None


def _app_with_state(state: object | None) -> web.Application:
    """An app with routes registered and ``state`` set as the gateway would.

    ``state=None`` leaves ``app["state"]`` unset (a bare app), exercising the
    scheduler-unavailable branch.
    """
    app = web.Application()
    if state is not None:
        app["state"] = state
    routes.register_routes(app)
    return app


class TestRouteRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_routes_are_namespaced_under_the_app(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        paths = [r.canonical for r in app.router.resources() if getattr(r, "canonical", "")]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith(_BASE), f"route escapes the app namespace: {path}")

    async def test_expected_surface_is_present(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        paths = {r.canonical for r in app.router.resources() if getattr(r, "canonical", "")}
        self.assertIn(_PATH, paths)
        self.assertIn(_REPAIR, paths)


class _TmpHome:
    """Mixin: isolate KIROCREW_HOME to a temp dir per test.

    Only ever mixed into ``unittest.TestCase`` subclasses; the annotation
    below tells the type checker ``addCleanup`` comes from that host class.
    """

    if TYPE_CHECKING:
        addCleanup: Callable[..., None]

    def _isolate_home(self) -> None:
        self.tmp = tempfile.mkdtemp()
        # Registered IMMEDIATELY after allocation: a later setUp failure skips
        # tearDown entirely, and the directory would otherwise leak.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def _restore_home(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPromptRoutes(_TmpHome, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._isolate_home()

    def tearDown(self) -> None:
        self._restore_home()

    async def test_disabled_app_is_refused_get_and_put(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            async with TestClient(TestServer(app)) as client:
                get = await client.get(_PATH)
                self.assertEqual(get.status, 403)
                self.assertEqual((await get.json())["code"], "app_disabled")
                put = await client.put(_PATH, json={"prompt": "x"})
                self.assertEqual(put.status, 403)
        # The refused PUT must not have written anything.
        self.assertFalse(settings.prompt_path().exists())

    async def test_get_reports_default_when_unset(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(_PATH)
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        self.assertEqual(body["prompt"], DEFAULT_RECONCILE_PROMPT)
        self.assertTrue(body["isDefault"])
        self.assertEqual(body["defaultPrompt"], DEFAULT_RECONCILE_PROMPT)

    async def test_put_saves_and_get_reflects(self) -> None:
        custom = "Promote to done only when owned PRs are merged and released."
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                put = await client.put(_PATH, json={"prompt": custom})
                self.assertEqual(put.status, 200)
                put_body = await put.json()
                self.assertEqual(put_body["prompt"], custom)
                self.assertFalse(put_body["isDefault"])

                get_body = await (await client.get(_PATH)).json()
        self.assertEqual(get_body["prompt"], custom)
        self.assertFalse(get_body["isDefault"])
        self.assertEqual(settings.get_prompt(), custom)

    async def test_put_empty_string_resets_to_default(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                await client.put(_PATH, json={"prompt": "custom"})
                self.assertTrue(settings.prompt_path().is_file())
                resp = await client.put(_PATH, json={"prompt": ""})
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        self.assertEqual(body["prompt"], DEFAULT_RECONCILE_PROMPT)
        self.assertTrue(body["isDefault"])
        self.assertFalse(settings.prompt_path().exists())

    async def test_put_missing_field_is_400(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_PATH, json={"nope": 1})
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["code"], "missing_required_field")

    async def test_put_non_string_is_400(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_PATH, json={"prompt": 123})
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["code"], "invalid_field_type")

    async def test_put_non_object_body_is_400(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    _PATH, data="not json", headers={"Content-Type": "application/json"}
                )
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["code"], "body_not_object")

    async def test_put_too_long_is_400(self) -> None:
        app = web.Application()
        routes.register_routes(app)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_PATH, json={"prompt": "x" * (settings.MAX_PROMPT_LEN + 1)})
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["code"], "value_too_long")
        # Nothing was written.
        self.assertFalse(settings.prompt_path().exists())


class TestCronStatusInGet(_TmpHome, unittest.IsolatedAsyncioTestCase):
    """GET reflects the reconcile cron's status; prompt fields stay intact."""

    def setUp(self) -> None:
        self._isolate_home()

    def tearDown(self) -> None:
        self._restore_home()

    async def _get_cron(self, state: object | None) -> dict:
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(_PATH)
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        # Prompt fields are unchanged by the cron addition.
        self.assertEqual(body["prompt"], DEFAULT_RECONCILE_PROMPT)
        self.assertTrue(body["isDefault"])
        self.assertEqual(body["defaultPrompt"], DEFAULT_RECONCILE_PROMPT)
        return body["cron"]

    async def test_present_enabled(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job(enabled=True)]))
        cron = await self._get_cron(state)
        self.assertEqual(cron, {"present": True, "enabled": True, "schedule": "23 * * * *"})

    async def test_present_paused(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job(enabled=False)]))
        cron = await self._get_cron(state)
        self.assertTrue(cron["present"])
        self.assertFalse(cron["enabled"])
        self.assertEqual(cron["schedule"], "23 * * * *")

    async def test_missing(self) -> None:
        # A job owned by a DIFFERENT app must not count as present (owner-scoped).
        foreign = _job(name=_JOB_NAME, created_by="app:someone-else")
        state = SimpleNamespace(crons=_StubCronService([foreign]))
        cron = await self._get_cron(state)
        self.assertFalse(cron["present"])
        self.assertFalse(cron["enabled"])
        # Schedule falls back to the manifest value even when absent.
        self.assertEqual(cron["schedule"], "23 * * * *")

    async def test_schedule_falls_back_when_job_has_none(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job(cron_expr=None)]))
        cron = await self._get_cron(state)
        self.assertEqual(cron["schedule"], "23 * * * *")

    async def test_scheduler_unavailable(self) -> None:
        # No gateway state at all -> scheduler unavailable, flagged explicitly.
        cron = await self._get_cron(None)
        self.assertFalse(cron["present"])
        self.assertFalse(cron["enabled"])
        self.assertTrue(cron["schedulerUnavailable"])


class TestRepairCron(_TmpHome, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._isolate_home()

    def tearDown(self) -> None:
        self._restore_home()

    async def test_disabled_app_is_refused(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job()]))
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(_REPAIR)
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["code"], "app_disabled")

    async def test_503_when_no_scheduler(self) -> None:
        app = _app_with_state(None)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(_REPAIR)
                self.assertEqual(resp.status, 503)
                self.assertEqual((await resp.json())["code"], "cron_service_unavailable")

    async def test_registers_when_missing(self) -> None:
        svc = _StubCronService([])  # no reconcile job present
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        calls: list[tuple[str, object]] = []

        async def _heal(app_name: str, cron_service: object) -> list[str]:
            calls.append((app_name, cron_service))
            # Simulate the heal landing the job so the fresh status reads present.
            svc._jobs.append(_job())
            return [_JOB_NAME]

        with (
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app_crons_with_service", _heal),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(_REPAIR)
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(calls, [("chat-status-tags", svc)])
        self.assertTrue(body["cron"]["present"])
        self.assertTrue(body["cron"]["enabled"])
        # A missing job is healed by registration, never by a resume call.
        self.assertEqual(svc.resumed, [])

    async def test_resumes_when_paused(self) -> None:
        svc = _StubCronService([_job(enabled=False, job_id="cron-42")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)

        async def _must_not_heal(*args: object, **kwargs: object) -> list[str]:
            raise AssertionError("a present-but-paused job must be resumed, not re-registered")

        with (
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch(
                "kiro_crew.apps.bridges.register_app_crons_with_service",
                _must_not_heal,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(_REPAIR)
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        self.assertEqual(svc.resumed, [("cron-42", True)])
        self.assertTrue(body["ok"])
        self.assertTrue(body["cron"]["present"])
        self.assertTrue(body["cron"]["enabled"])

    async def test_idempotent_when_healthy(self) -> None:
        svc = _StubCronService([_job(enabled=True)])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)

        async def _must_not_heal(*args: object, **kwargs: object) -> list[str]:
            raise AssertionError("a healthy job must not be re-registered")

        with (
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch(
                "kiro_crew.apps.bridges.register_app_crons_with_service",
                _must_not_heal,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                first = await (await client.post(_REPAIR)).json()
                second = await (await client.post(_REPAIR)).json()
        # No resume, no register — and the two calls return identical status.
        self.assertEqual(svc.resumed, [])
        self.assertEqual(first, second)
        self.assertEqual(
            first["cron"], {"present": True, "enabled": True, "schedule": "23 * * * *"}
        )


_SETTINGS = f"{_BASE}/settings"


class TestSettingsRoutes(_TmpHome, unittest.IsolatedAsyncioTestCase):
    """GET/PUT of the behaviour toggles, incl. 400s, the disabled-app 403, and
    the reconciler toggle pausing/resuming the cron job."""

    def setUp(self) -> None:
        self._isolate_home()

    def tearDown(self) -> None:
        self._restore_home()

    async def test_disabled_app_is_refused_get_and_put(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job()]))
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            async with TestClient(TestServer(app)) as client:
                get = await client.get(_SETTINGS)
                self.assertEqual(get.status, 403)
                self.assertEqual((await get.json())["code"], "app_disabled")
                put = await client.put(_SETTINGS, json={"autoResumeEnabled": False})
                self.assertEqual(put.status, 403)
        # Nothing persisted, and the cron was not touched.
        self.assertFalse(settings.flags_path().exists())

    async def test_get_reports_defaults(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job()]))
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(_SETTINGS)
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        self.assertEqual(body, {"reconcilerEnabled": True, "autoResumeEnabled": True})

    async def test_put_auto_resume_only_needs_no_scheduler(self) -> None:
        # Toggling only autoResumeEnabled must NOT require the cron scheduler.
        app = _app_with_state(None)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"autoResumeEnabled": False})
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        self.assertEqual(body, {"reconcilerEnabled": True, "autoResumeEnabled": False})
        self.assertFalse(settings.get_flags()["auto_resume_enabled"])

    async def test_put_unknown_key_is_400(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job()]))
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"nope": True})
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["code"], "unknown_field")

    async def test_put_persist_failure_is_500_and_rolls_back_the_cron(self) -> None:
        """A flag write that fails (read-only FS, disk full) must not report
        success: the cron was already mutated on this request, so a swallowed
        write would leave the stored flags and the live automation disagreeing
        forever. Pin the honest path: 500, and the cron mutation rolled back."""
        cron = _StubCronService([_job()])
        state = SimpleNamespace(crons=cron)
        app = _app_with_state(state)
        applied: list[bool] = []

        real_apply = routes._apply_reconciler_flag

        async def _recording_apply(service: object, enabled: bool) -> None:
            applied.append(enabled)
            await real_apply(service, enabled)

        with (
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch.object(routes, "_apply_reconciler_flag", _recording_apply),
            mock.patch.object(routes.settings, "set_flags", side_effect=OSError("disk full")),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"reconcilerEnabled": False})
                self.assertEqual(resp.status, 500)
                self.assertEqual((await resp.json())["code"], "settings_write_failed")
        # The cron was paused for the request, then rolled back to enabled.
        self.assertEqual(applied, [False, True])
        # Nothing persisted.
        self.assertFalse(settings.flags_path().exists())

    async def test_put_non_bool_is_400(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job()]))
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"autoResumeEnabled": "yes"})
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["code"], "invalid_field_type")
        # A rejected PUT wrote nothing.
        self.assertFalse(settings.flags_path().exists())

    async def test_put_non_object_body_is_400(self) -> None:
        state = SimpleNamespace(crons=_StubCronService([_job()]))
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    _SETTINGS, data="not json", headers={"Content-Type": "application/json"}
                )
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["code"], "body_not_object")

    async def test_reconciler_false_pauses_cron(self) -> None:
        svc = _StubCronService([_job(enabled=True, job_id="cron-9")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"reconcilerEnabled": False})
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        # The job was disabled (paused) via enable_job_async(enabled=False)...
        self.assertEqual(svc.resumed, [("cron-9", False)])
        self.assertFalse(svc._jobs[0].enabled)
        # ...and the flag persisted.
        self.assertFalse(body["reconcilerEnabled"])
        self.assertFalse(settings.get_flags()["reconciler_enabled"])

    async def test_reconciler_true_resumes_paused_cron(self) -> None:
        svc = _StubCronService([_job(enabled=False, job_id="cron-9")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"reconcilerEnabled": True})
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        self.assertEqual(svc.resumed, [("cron-9", True)])
        self.assertTrue(svc._jobs[0].enabled)
        self.assertTrue(body["reconcilerEnabled"])

    async def test_reconciler_true_reregisters_missing_cron(self) -> None:
        svc = _StubCronService([])  # reconcile job absent
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        calls: list[tuple[str, object]] = []

        async def _heal(app_name: str, cron_service: object) -> list[str]:
            calls.append((app_name, cron_service))
            svc._jobs.append(_job())
            return [_JOB_NAME]

        with (
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app_crons_with_service", _heal),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"reconcilerEnabled": True})
                self.assertEqual(resp.status, 200)
        # A missing job is healed by registration, never by a resume call.
        self.assertEqual(calls, [("chat-status-tags", svc)])
        self.assertEqual(svc.resumed, [])

    async def test_reconciler_toggle_503_when_no_scheduler(self) -> None:
        app = _app_with_state(None)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"reconcilerEnabled": False})
                self.assertEqual(resp.status, 503)
                self.assertEqual((await resp.json())["code"], "cron_service_unavailable")
        # The flag must NOT have been written when the toggle could not be applied.
        self.assertFalse(settings.flags_path().exists())

    async def test_reconciler_paused_reflected_in_prompt_cron_block(self) -> None:
        # After pausing via the toggle, GET /reconcile-prompt's cron block reports
        # the job as present-but-disabled.
        svc = _StubCronService([_job(enabled=True, job_id="cron-9")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                await client.put(_SETTINGS, json={"reconcilerEnabled": False})
                cron = (await (await client.get(f"{_BASE}/reconcile-prompt")).json())["cron"]
        self.assertTrue(cron["present"])
        self.assertFalse(cron["enabled"])


class TestResumeLoopRespectsFlag(_TmpHome, unittest.IsolatedAsyncioTestCase):
    """The auto-resume loop does no work when disabled, and works when enabled."""

    def setUp(self) -> None:
        self._isolate_home()

    def tearDown(self) -> None:
        self._restore_home()

    async def _run_one_cycle(self) -> mock.MagicMock:
        """Run exactly one _resume_loop iteration and return the TagsStore mock.

        The loop's initial ``await asyncio.sleep(_RESUME_INTERVAL_SECS)`` is
        patched to a no-op, and the SECOND sleep call raises to break out after
        one pass — so the whole loop body runs once deterministically.
        """
        from kiro_crew.apps.builtins.chat_status_tags import hooks

        ctx = SimpleNamespace(config={}, data_dir=pathlib.Path(self.tmp))
        client = mock.MagicMock()
        client.list_slots.return_value = []

        sleeps = {"n": 0}

        async def _fake_sleep(_secs: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise asyncio.CancelledError

        with (
            mock.patch.object(hooks, "TagsStore", return_value=client),
            mock.patch.object(hooks.asyncio, "sleep", _fake_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await hooks._resume_loop(ctx)
        return client

    async def test_no_work_when_disabled(self) -> None:
        settings.set_flags(auto_resume_enabled=False)
        client = await self._run_one_cycle()
        # Disabled: the loop must not even enumerate slots, let alone resume.
        client.list_slots.assert_not_called()
        client.send_message.assert_not_called()

    async def test_does_work_when_enabled(self) -> None:
        # Default is enabled; the loop proceeds to enumerate slots (finds none,
        # so no resume — but it DID the work, unlike the disabled case).
        client = await self._run_one_cycle()
        client.list_slots.assert_called()
        client.send_message.assert_not_called()


class TestPromptSyncsToJobMessage(_TmpHome, unittest.IsolatedAsyncioTestCase):
    """The whole design: the effective prompt is pushed into the live reconcile
    cron's ``message`` on save/reset, repair, and the enable-toggle — so the
    agent gets it as its own instructions, not as a file to fetch and distrust."""

    def setUp(self) -> None:
        self._isolate_home()

    def tearDown(self) -> None:
        self._restore_home()

    async def test_put_pushes_prompt_into_live_job_message(self) -> None:
        custom = "Also promote a chat to review when it owns an open code review."
        svc = _StubCronService([_job(enabled=True, job_id="cron-1")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_PATH, json={"prompt": custom})
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        # The live job's message is now the custom prompt itself...
        self.assertEqual(svc.messaged, [("cron-1", custom)])
        self.assertEqual(svc._jobs[0].message, custom)
        # ...the response reports the sync and keeps the existing shape.
        self.assertTrue(body["jobMessageSynced"])
        self.assertEqual(body["prompt"], custom)
        self.assertFalse(body["isDefault"])
        self.assertEqual(body["defaultPrompt"], DEFAULT_RECONCILE_PROMPT)
        self.assertTrue(body["cron"]["present"])

    async def test_put_reset_pushes_default_into_live_job_message(self) -> None:
        svc = _StubCronService([_job(enabled=True, job_id="cron-1")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                await client.put(_PATH, json={"prompt": "custom"})
                resp = await client.put(_PATH, json={"prompt": ""})
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        # Reset syncs the DEFAULT back into the message.
        self.assertEqual(svc._jobs[0].message, DEFAULT_RECONCILE_PROMPT)
        self.assertEqual(svc.messaged[-1], ("cron-1", DEFAULT_RECONCILE_PROMPT))
        self.assertTrue(body["jobMessageSynced"])
        self.assertTrue(body["isDefault"])

    async def test_put_still_persists_when_scheduler_unavailable(self) -> None:
        custom = "custom prompt with no scheduler running"
        app = _app_with_state(None)  # no scheduler
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_PATH, json={"prompt": custom})
                self.assertEqual(resp.status, 200)
                body = await resp.json()
        # The prompt is persisted to disk; only the live sync was skipped.
        self.assertEqual(settings.get_prompt(), custom)
        self.assertFalse(body["jobMessageSynced"])
        self.assertTrue(body["cron"]["schedulerUnavailable"])

    async def test_put_reports_not_synced_when_job_absent(self) -> None:
        # Scheduler present but the reconcile job is missing -> persisted, not synced.
        svc = _StubCronService([])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_PATH, json={"prompt": "x"})
                body = await resp.json()
        self.assertEqual(settings.get_prompt(), "x")
        self.assertFalse(body["jobMessageSynced"])
        self.assertEqual(svc.messaged, [])

    async def test_repair_reapplies_custom_prompt_after_reregister(self) -> None:
        # A custom prompt is stored; the job is absent; repair re-registers it
        # from the manifest (default message) and MUST re-apply the stored prompt.
        settings.set_prompt("operator's custom reconcile prompt")
        svc = _StubCronService([])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)

        async def _heal(app_name: str, cron_service: object) -> list[str]:
            # Manifest rebuild lands the job carrying the DEFAULT message.
            svc._jobs.append(_job(job_id="cron-new"))  # message defaults to the manifest text
            svc._jobs[-1].message = DEFAULT_RECONCILE_PROMPT
            return [_JOB_NAME]

        with (
            mock.patch.object(routes, "is_app_enabled", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app_crons_with_service", _heal),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(_REPAIR)
                self.assertEqual(resp.status, 200)
        # After heal, the stored custom prompt was pushed back into the message.
        self.assertEqual(svc._jobs[0].message, "operator's custom reconcile prompt")
        self.assertEqual(svc.messaged[-1], ("cron-new", "operator's custom reconcile prompt"))

    async def test_repair_reapplies_custom_prompt_after_resume(self) -> None:
        settings.set_prompt("operator's custom reconcile prompt")
        svc = _StubCronService([_job(enabled=False, job_id="cron-7")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(_REPAIR)
                self.assertEqual(resp.status, 200)
        self.assertEqual(svc._jobs[0].message, "operator's custom reconcile prompt")

    async def test_enable_toggle_reapplies_custom_prompt(self) -> None:
        # Turning the reconciler back on re-enables the paused job AND re-applies
        # the stored custom prompt.
        settings.set_prompt("operator's custom reconcile prompt")
        svc = _StubCronService([_job(enabled=False, job_id="cron-9")])
        state = SimpleNamespace(crons=svc)
        app = _app_with_state(state)
        with mock.patch.object(routes, "is_app_enabled", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(_SETTINGS, json={"reconcilerEnabled": True})
                self.assertEqual(resp.status, 200)
        self.assertEqual(svc.resumed, [("cron-9", True)])
        self.assertEqual(svc._jobs[0].message, "operator's custom reconcile prompt")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Coverage for ``dashboard/handlers/core.py``: the STT runtime endpoints.

STT status, prepare, prewarm, ffmpeg download and transcribe, with their error branches
and cold paths. Shared stubs come from ``dashboard_handlers_core_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import seeded_config_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _json_req, _req, _seed_stt

from kiro_crew import transcribe as transcribe_mod
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.stt import models as stt_models
from kiro_crew.testing.wait import finished

# ── STT status / prepare / prewarm ──────────────────────────────────────


async def _drain_stt_background() -> list:
    """Await the handler's detached tasks and return their outcomes.

    An un-awaited task would outlive the loop and blame its exception on the next test.
    """
    tasks = list(core_mod._stt_background_tasks)
    return await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def model_store(monkeypatch) -> stt_models.ModelStore:
    """Give the test its own model store.

    The real one is a process global holding live download state, so a test that
    let a handler touch it would hand its progress block to whatever test ran
    next on the same xdist worker.
    """
    store = stt_models.ModelStore()
    monkeypatch.setattr(stt_models, "_store", store)
    return store


@pytest.fixture
def stt_background(monkeypatch) -> set:
    """Give the test its own detached-task set.

    ``_stt_background_tasks`` is a module global, so "no work was scheduled"
    assertions would otherwise be answerable by a task some other file left
    pending on the same worker. Production reads the global on every add, so
    swapping the object is enough.
    """
    tasks: set = set()
    monkeypatch.setattr(core_mod, "_stt_background_tasks", tasks)
    return tasks


def _availability(ok: bool, code: str = "", detail: str = ""):
    """Build the shape ``availability_detail`` returns, without probing the host."""
    return core_mod.stt.Availability(ok, code, detail)


class TestSttStatus:
    """``GET /api/stt/status`` is the runtime half of the settings surface.

    ``GET /api/config/stt`` serves what the operator chose; this serves what the
    host can currently do about it: the availability reason as a code the
    dashboard can localise, whether the chosen model is on disk, whether one is
    resident right now, and the progress of a transfer in flight.
    """

    @pytest.fixture(autouse=True)
    def _quiet_probes(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            core_mod, "availability_detail", lambda _cfg: _availability(True)
        )
        _floor_monkeypatch.setattr("kiro_crew.stt.models.is_present", lambda _m: False)
        _floor_monkeypatch.setattr(
            "kiro_crew.stt.engine.shared_engine", lambda *a, **k: SimpleNamespace(loaded=False)
        )
        # The decoder probe stats the operator's real package directories and can
        # hash an 80 MB executable, so the default answer is pinned; the tests
        # that assert on it override this.
        _floor_monkeypatch.setattr(core_mod, "ensure_ffmpeg_in_path", lambda: None)
        _floor_monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)

    @pytest.mark.asyncio
    async def test_voice_off_reports_no_backend_and_does_not_load_the_library(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """Opening Settings with voice off does not dlopen the speech library.

        On macOS ``whisper_print_system_info()`` costs +31.8 MB resident for the process
        lifetime. The field is absent rather than null, so the panel renders nothing.
        """
        probed = []
        monkeypatch.setattr(
            "kiro_crew.stt.engine.WhisperEngine.capabilities",
            classmethod(lambda _cls: probed.append(1)),
        )
        _seed_stt(seeded_config, enabled=False)
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert probed == []
        assert "backend" not in body

    @pytest.mark.asyncio
    async def test_status_reports_the_resolved_model_and_the_whole_catalog(
        self, seeded_config, model_store
    ) -> None:
        """The catalog cannot be a static frontend table: `present` is per-host
        state that changes as models are fetched, and it is the only thing that
        tells the picker which choice costs a download."""
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["provider"] == "local"
        assert body["model"] == stt_models.DEFAULT_MODEL
        assert body["model_present"] is False
        assert body["model_bytes"] == stt_models.resolve(stt_models.DEFAULT_MODEL).size_bytes
        # Smallest first, which is the order the picker offers them in.
        assert [row["name"] for row in body["models"]] == [m.name for m in stt_models.CATALOG]
        assert all(row["present"] is False for row in body["models"])
        assert all(row["size_bytes"] > 0 for row in body["models"])

    @pytest.mark.asyncio
    async def test_status_reports_a_present_model_as_present(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        monkeypatch.setattr("kiro_crew.stt.models.is_present", lambda _m: True)
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["model_present"] is True
        assert all(row["present"] is True for row in body["models"])

    @pytest.mark.asyncio
    async def test_status_reads_presence_through_the_owning_module(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """A materialised package attribute must not shadow the real function.

        A ``monkeypatch.setattr(stt, "is_present", ...)`` teardown writes a real attribute
        past the lazy ``__getattr__``, so the handler reads ``stt.models.is_present``. The
        test recreates that attribute because the failure shows only in full-suite order.
        """
        monkeypatch.setattr(core_mod.stt, "is_present", lambda _m: False, raising=False)
        monkeypatch.setattr("kiro_crew.stt.models.is_present", lambda _m: True)
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["model_present"] is True
        assert all(row["present"] is True for row in body["models"])

    @pytest.mark.asyncio
    async def test_status_forwards_the_availability_code_and_detail(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """The code is the contract and the prose is advisory: the dashboard
        renders localised text, so it cannot key off an English sentence, and
        "install an extra" leads somewhere completely different from "this
        platform has no prebuilt wheel"."""
        monkeypatch.setattr(
            core_mod,
            "availability_detail",
            lambda _cfg: _availability(False, core_mod.stt.CODE_EXTRA_MISSING, "needs the extra"),
        )
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["available"] is False
        assert body["code"] == core_mod.stt.CODE_EXTRA_MISSING
        assert body["detail"] == "needs the extra"

    @pytest.mark.asyncio
    async def test_status_reports_whether_a_model_is_resident(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """Residency is what decides between a 30 ms transcription and one that
        pays a model load first, so the panel can say which the user will get."""
        monkeypatch.setattr(
            "kiro_crew.stt.engine.shared_engine", lambda *a, **k: SimpleNamespace(loaded=True)
        )
        assert json.loads((await core_mod.api_stt_status(_req())).body)["engine_loaded"] is True

    @pytest.mark.asyncio
    async def test_status_republishes_the_live_download_block(
        self, seeded_config, model_store
    ) -> None:
        """A first-run fetch is 148 MB at the default, so a panel polling this is
        the only thing standing between the user and an unexplained wait."""
        model_store._set(step="downloading", model="base", done=1024, total=147_951_465)
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["download"] == {
            "step": "downloading",
            "model": "base",
            "downloaded_bytes": 1024,
            "total_bytes": 147_951_465,
            "error": "",
        }

    @pytest.mark.asyncio
    async def test_status_reports_which_decoder_the_transcode_path_would_run(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """`source` names WHICH decoder, because each one is repaired differently.

        A bundled payload is fixed by reinstalling the app, a system one by the
        host's package manager, and the store one by the decoder fetch — so a bare
        "present: true" would send the reader looking for the wrong remedy.
        """
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: transcribe_mod.FFMPEG_SOURCE_STORE)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["present"] is True
        assert body["source"] == "store"
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_AVAILABLE
        # The GATEWAY's platform, not the browser's: a dashboard can be open
        # against another machine, and the hand-off prompt names the host that
        # actually needs fixing.
        assert body["os"] == platform.system()
        assert body["arch"] == platform.machine()
        assert body["download"] == dict(core_mod.stt_decoder.store().status)

    @pytest.mark.asyncio
    async def test_status_reports_no_decoder_as_absent_with_no_source(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["present"] is False
        assert body["source"] is None

    @pytest.mark.asyncio
    async def test_status_marks_a_platform_with_no_pinned_artifact_unsupported(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """`unsupported` is not a failure: nothing a fetch does can change it.

        The page has to offer the manual system-decoder route there rather than a
        retry, which is why this is a separate value from a failed download.
        """
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(core_mod.stt_decoder, "artifact_for", lambda *a, **k: None)
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_status_marks_a_desktop_release_as_bundled(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """A release carries its own authenticated decoder and repairs by reinstall."""
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(
            core_mod.stt_decoder,
            "platform_key",
            lambda *a, **k: core_mod.stt_decoder.ARTIFACTS[0].platform_key,
        )
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_BUNDLED

    @pytest.mark.asyncio
    async def test_status_marks_a_release_with_no_bundled_decoder_unsupported(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """The macOS Intel release ships no decoder, so it is not told to reinstall.

        ``unsupported`` is what makes the page offer the system-decoder route there;
        ``bundled`` would show "the bundled decoder is missing or damaged" for a
        payload that was never in the app.
        """
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(
            core_mod.stt_decoder,
            "platform_key",
            lambda *a, **k: core_mod.stt_decoder.DECODERLESS_BUNDLE_PLATFORM,
        )
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_status_refuses_an_app_token(self, seeded_config, fake_sel, model_store) -> None:
        """``request["user"]`` is truthy for an app token too, so the cookie check
        alone does not separate a browser from an app that named this path in its
        manifest. Reading the host's setup state is operator business, not
        something an app earns by declaring a path."""
        resp = await core_mod.api_stt_status(_req(app_token="meetings"))
        assert resp.status == 403
        body = json.loads(resp.body)
        assert body["code"] == "dashboard_user_required"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_status_refuses_a_request_with_no_app_claim_at_all(
        self, seeded_config, fake_sel, model_store
    ) -> None:
        """Fail closed: an absent claim means no auth middleware published one, so
        it must be refused alongside a non-empty one rather than read as the
        dashboard user by default."""
        resp = await core_mod.api_stt_status(_req(app_token=None))
        assert resp.status == 403


class TestSttPrepare:
    """``POST /api/stt/prepare`` starts, or joins, the one-time model download."""

    @pytest.fixture(autouse=True)
    def _isolated_tasks(self, stt_background):
        """Every test here asserts on what was scheduled, so the set must be ours."""

    @pytest.fixture(autouse=True)
    def _no_real_download(self, _floor_monkeypatch):
        """Record the requested model instead of fetching 148 MB over the wire."""
        self.requested: list[str] = []

        async def _fake_ensure(name: str) -> bool:
            self.requested.append(name)
            return True

        _floor_monkeypatch.setattr("kiro_crew.stt.session.ensure_model", _fake_ensure)

    @pytest.mark.asyncio
    async def test_prepare_answers_202_and_starts_the_transfer(
        self, seeded_config, model_store
    ) -> None:
        """202, not 200: the fetch outlives the request on purpose, so the user is
        not held behind it and the caller polls the status endpoint instead."""
        resp = await core_mod.api_stt_prepare(_json_req(raises=True))
        assert resp.status == 202
        body = json.loads(resp.body)
        assert body["model"] == stt_models.DEFAULT_MODEL
        assert body["download"] == dict(model_store.status)
        assert await _drain_stt_background() == [True]
        assert self.requested == [stt_models.DEFAULT_MODEL]

    @pytest.mark.asyncio
    async def test_prepare_honours_a_requested_model(self, seeded_config, model_store) -> None:
        """The picker can offer the weights BEFORE the selection is committed, so
        the operator is not asked to save a setting to find out what it costs."""
        resp = await core_mod.api_stt_prepare(_json_req({"model": "tiny"}))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == "tiny"
        await _drain_stt_background()
        assert self.requested == ["tiny"]

    @pytest.mark.asyncio
    async def test_prepare_degrades_an_unknown_model_to_the_catalog_default(
        self, seeded_config, model_store
    ) -> None:
        """A model name becomes a filename under the models directory and a URL
        under the pinned base, so only catalog names may reach either. An unknown
        one resolves to the default the same way a stale configured value does."""
        resp = await core_mod.api_stt_prepare(_json_req({"model": "../../etc/passwd"}))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == stt_models.DEFAULT_MODEL
        await _drain_stt_background()
        # The name handed to the downloader resolves to the same catalog entry, so
        # nothing outside the catalog reaches the filesystem or the network.
        assert [stt_models.resolve(n).name for n in self.requested] == [stt_models.DEFAULT_MODEL]

    @pytest.mark.asyncio
    async def test_prepare_with_a_non_object_body_uses_the_configured_model(
        self, seeded_config, model_store
    ) -> None:
        """A JSON array, or anything else that is not an object, carries no model
        name. The configured one is the only reading that leaves the endpoint
        useful to a caller sending no body at all."""
        _seed_stt(seeded_config, model="small")
        resp = await core_mod.api_stt_prepare(_json_req(["tiny"]))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == "small"
        await _drain_stt_background()
        assert self.requested == ["small"]

    @pytest.mark.asyncio
    async def test_prepare_with_a_blank_model_uses_the_configured_one(
        self, seeded_config, model_store
    ) -> None:
        _seed_stt(seeded_config, model="small")
        resp = await core_mod.api_stt_prepare(_json_req({"model": ""}))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == "small"
        await _drain_stt_background()
        assert self.requested == ["small"]

    @pytest.mark.asyncio
    async def test_prepare_joins_a_transfer_already_running(
        self, seeded_config, model_store
    ) -> None:
        """A polling panel must not accumulate one task per poll. Concurrent
        callers are made safe by the store's own lock, so this only has to avoid
        piling up work nobody will read."""
        model_store._set(step="downloading", model="base", done=512, total=147_951_465)
        resp = await core_mod.api_stt_prepare(_json_req(raises=True))
        assert resp.status == 202
        assert json.loads(resp.body)["download"]["downloaded_bytes"] == 512
        assert await _drain_stt_background() == []
        assert self.requested == []

    @pytest.mark.asyncio
    async def test_prepare_refuses_an_app_token(self, seeded_config, fake_sel, model_store) -> None:
        """An app naming this path must not be able to start a 148 MB transfer on
        the operator's connection."""
        resp = await core_mod.api_stt_prepare(_json_req(raises=True, app_token="meetings"))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "dashboard_user_required"
        assert await _drain_stt_background() == []
        assert self.requested == []


class TestSttResolveStaysOffTheLoop:
    """Neither STT handler may resolve a model name on the gateway's event loop.

    For :data:`~kiro_crew.stt.models.CUSTOM_MODEL` ``resolve`` reads ``config.json``.
    Asserted on the thread, not a duration, because a timing bound this small flakes.
    """

    @pytest.fixture(autouse=True)
    def _quiet_probes(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            core_mod, "availability_detail", lambda _cfg: _availability(True)
        )
        _floor_monkeypatch.setattr("kiro_crew.stt.models.is_present", lambda _m: False)
        _floor_monkeypatch.setattr(
            "kiro_crew.stt.engine.shared_engine", lambda *a, **k: SimpleNamespace(loaded=False)
        )
        _floor_monkeypatch.setattr(core_mod, "ensure_ffmpeg_in_path", lambda: None)
        _floor_monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)

    @pytest.fixture
    def resolved_on(self, monkeypatch):
        """The threads ``resolve`` ran on, recorded in call order."""
        seen: list[threading.Thread] = []
        real = stt_models.resolve

        def _resolve(name: str):
            seen.append(threading.current_thread())
            return real(name)

        monkeypatch.setattr(core_mod.stt_models, "resolve", _resolve)
        return seen

    @pytest.mark.asyncio
    async def test_status_resolves_off_the_loop(
        self, seeded_config, model_store, resolved_on
    ) -> None:
        loop_thread = threading.current_thread()
        assert (await core_mod.api_stt_status(_req())).status == 200
        assert resolved_on, "resolve was never called"
        assert all(t is not loop_thread for t in resolved_on), resolved_on

    @pytest.mark.asyncio
    async def test_prepare_resolves_off_the_loop(
        self, seeded_config, model_store, stt_background, resolved_on, monkeypatch
    ) -> None:
        async def _fake_ensure(_name: str) -> bool:
            return True

        monkeypatch.setattr("kiro_crew.stt.session.ensure_model", _fake_ensure)
        loop_thread = threading.current_thread()
        assert (await core_mod.api_stt_prepare(_json_req(raises=True))).status == 202
        await _drain_stt_background()
        assert resolved_on, "resolve was never called"
        assert all(t is not loop_thread for t in resolved_on), resolved_on


class TestSttFfmpegDownload:
    """``POST /api/stt/ffmpeg/download`` fetches the audio decoder into the store.

    Same 202-then-poll contract as the model transfer: a ~30 MB wheel is not
    something to hold a request open behind, and the caller reads progress from
    ``GET /api/stt/status``'s ``ffmpeg.download``.
    """

    @pytest.fixture(autouse=True)
    def _isolated_tasks(self, stt_background):
        """Every test here asserts on what was scheduled, so the set must be ours."""

    @pytest.fixture(autouse=True)
    def decoder_store(self, _floor_monkeypatch):
        """Give the test its own decoder store, and never let it reach the network.

        The real one is a process global holding live transfer state, so a test
        that let the handler touch it would hand its progress block to whatever
        test ran next on the same xdist worker.
        """
        store = core_mod.stt_decoder.DecoderStore()
        self.ensured: list[str] = []

        async def _fake_ensure():
            self.ensured.append("ensure")
            return None

        store.ensure = _fake_ensure  # type: ignore[method-assign]
        _floor_monkeypatch.setattr(core_mod.stt_decoder, "_store", store)
        _floor_monkeypatch.setattr(
            core_mod.platform_compat, "is_bundled_interpreter", lambda: False
        )
        return store

    @pytest.mark.asyncio
    async def test_answers_202_and_starts_the_fetch(self, seeded_config, decoder_store) -> None:
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 202
        assert json.loads(resp.body)["download"] == dict(decoder_store.status)
        assert await _drain_stt_background() == [None]
        assert self.ensured == ["ensure"]

    @pytest.mark.asyncio
    async def test_joins_a_fetch_already_running(self, seeded_config, decoder_store) -> None:
        """A polling panel must not accumulate one task per poll.

        Concurrent callers are made safe by the store's own lock, so this only has
        to avoid piling up work nobody will read.
        """
        decoder_store._set(
            stage=core_mod.stt_decoder.STAGE_DOWNLOADING,
            artifact="ffmpeg-linux-x86_64-v7.0.2",
            done=512,
            total=29_498_237,
        )
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 202
        assert json.loads(resp.body)["download"]["downloaded_bytes"] == 512
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_a_platform_with_no_pinned_artifact(
        self, seeded_config, monkeypatch, decoder_store
    ) -> None:
        """409 rather than a 202 that can never finish: nothing is pinned to fetch."""
        monkeypatch.setattr(core_mod.stt_decoder, "artifact_for", lambda *a, **k: None)
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["code"] == core_mod.stt_decoder.CODE_UNSUPPORTED
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_UNSUPPORTED
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_a_bundled_desktop_release(
        self, seeded_config, monkeypatch, decoder_store
    ) -> None:
        """A release already carries an authenticated decoder its resolver uses.

        Its resolver deliberately never looks outside that payload, so a fetch here
        would spend the operator's bandwidth on a file nothing can execute — and
        the real remedy (reinstall the app) is what the page says instead.
        """
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["code"] == core_mod._CODE_STT_DECODER_BUNDLED
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_BUNDLED
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_an_app_token(self, seeded_config, fake_sel, decoder_store) -> None:
        """An app naming this path must not start a transfer on the operator's line."""
        resp = await core_mod.api_stt_ffmpeg_download(_req(app_token="meetings"))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "dashboard_user_required"
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_a_request_with_no_app_claim_at_all(
        self, seeded_config, fake_sel, decoder_store
    ) -> None:
        resp = await core_mod.api_stt_ffmpeg_download(_req(app_token=None))
        assert resp.status == 403
        assert self.ensured == []

    def test_the_route_is_registered(self) -> None:
        """The handler is unreachable without its route, and 404 says nothing."""
        from aiohttp import web as _web

        from kiro_crew.dashboard.routes import memory as memory_routes

        app = _web.Application()
        memory_routes.register(app)
        paths = {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.resource is not None
        }
        assert ("POST", "/api/stt/ffmpeg/download") in paths


class TestSttPrewarm:
    """``POST /api/stt/prewarm`` pays the load while the user is still speaking."""

    @pytest.fixture(autouse=True)
    def _isolated_tasks(self, stt_background):
        """Every test here asserts on what was scheduled, so the set must be ours."""

    @pytest.mark.asyncio
    async def test_prewarm_answers_202_without_waiting_for_the_load(
        self, seeded_config, monkeypatch
    ) -> None:
        """Prewarm answers before the first load, which compiles a GPU pipeline.

        The fake stays blocked until after the response is asserted. Non-default config
        values prove the warm-up targets the operator's selection.
        """
        _seed_stt(seeded_config, model="small", language_code="fr-FR", threads=32)
        seen: dict = {}
        release = asyncio.Event()

        async def _fake_prewarm(
            *, model_name: str = "", language: str = "", threads: int | None = None
        ):
            seen.update({"model_name": model_name, "language": language, "threads": threads})
            await release.wait()
            return _availability(True)

        monkeypatch.setattr("kiro_crew.stt.session.prewarm", _fake_prewarm)
        resp = await core_mod.api_stt_prewarm(_req())
        assert resp.status == 202
        assert json.loads(resp.body) == {"ok": True}
        release.set()
        await _drain_stt_background()
        assert seen["model_name"] == "small"
        # The BCP-47 config value is reduced to the bare language code the
        # recogniser names its languages by.
        assert seen["language"] == "fr"
        # The configured count, so a warm-up after a settings change loads the
        # context the next session will use instead of one it would rebuild.
        assert seen["threads"] == 32

    @pytest.mark.asyncio
    async def test_prewarm_failure_does_not_fail_the_request(
        self, seeded_config, monkeypatch
    ) -> None:
        """Prewarming is an optimisation. A host that cannot load the model still
        has to be able to open the microphone, where the same failure is reported
        as an error frame carrying a code."""

        async def _boom(**_kwargs):
            raise RuntimeError("no recogniser here")

        monkeypatch.setattr("kiro_crew.stt.session.prewarm", _boom)
        resp = await core_mod.api_stt_prewarm(_req())
        assert resp.status == 202
        outcomes = await _drain_stt_background()
        assert [type(o).__name__ for o in outcomes] == ["RuntimeError"]

    @pytest.mark.asyncio
    async def test_prewarm_refuses_an_app_token(self, seeded_config, fake_sel, monkeypatch) -> None:
        """Warming a resident model inside the gateway is operator setup, so an
        app token must not reach it even though ``request["user"]`` is truthy."""
        started: list[str] = []

        async def _fake_prewarm(**_kwargs):
            started.append("ran")
            return _availability(True)

        monkeypatch.setattr("kiro_crew.stt.session.prewarm", _fake_prewarm)
        resp = await core_mod.api_stt_prewarm(_req(app_token="meetings"))
        assert resp.status == 403
        assert await _drain_stt_background() == []
        assert started == []
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


# ── STT transcribe endpoint ─────────────────────────────────────────────


def _multipart_req(field) -> web.Request:
    reader = SimpleNamespace(next=AsyncMock(return_value=field))
    req = _req()
    req.multipart = AsyncMock(return_value=reader)
    return req


class TestSttTranscribe:
    """The batch upload path: record first, upload afterwards.

    Every non-2xx body here carries a machine-readable ``code``. Backend-owned
    strings have no translation catalog, so the English prose is advisory and the
    code is the only thing the dashboard can branch on.
    """

    @pytest.fixture(autouse=True)
    def _backend_ready(self, _floor_monkeypatch):
        """Pin the availability probe, which imports an optional extra.

        Left real, every test in this class would answer differently on a host
        with the ``voice`` extra installed than on one without.
        """
        _floor_monkeypatch.setattr(
            core_mod, "availability_detail", lambda _cfg: _availability(True)
        )
        _floor_monkeypatch.setattr(core_mod, "audio_exceeds_secs", AsyncMock(return_value=False))

    @pytest.mark.asyncio
    async def test_unavailable_backend_is_503_naming_the_reason(self, monkeypatch) -> None:
        """The provider's own code and prose are forwarded rather than flattened:
        the caller has to be able to tell "install an extra" from "download the
        model" without reading an English sentence."""
        monkeypatch.setattr(
            core_mod,
            "availability_detail",
            lambda _cfg: _availability(
                False, core_mod.stt.CODE_MODEL_MISSING, "speech model base is not downloaded"
            ),
        )
        resp = await core_mod.api_stt_transcribe(_req())
        assert resp.status == 503
        body = json.loads(resp.body)
        assert body["code"] == core_mod.stt.CODE_MODEL_MISSING
        assert body["error"] == "speech model base is not downloaded"

    @pytest.mark.asyncio
    async def test_a_reasonless_refusal_still_carries_a_code(self, monkeypatch) -> None:
        """A provider that answers "no" without saying why must not produce a body
        with an empty code: the dashboard would have nothing to render at all."""
        monkeypatch.setattr(core_mod, "availability_detail", lambda _cfg: _availability(False))
        resp = await core_mod.api_stt_transcribe(_req())
        assert resp.status == 503
        body = json.loads(resp.body)
        assert body["code"] == "stt_unavailable"
        assert body["error"] == "STT not available"

    @pytest.mark.asyncio
    async def test_missing_audio_field_is_400(self) -> None:
        resp = await core_mod.api_stt_transcribe(_multipart_req(None))
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["error"] == "missing audio field"
        assert body["code"] == "stt_missing_audio_field"

    @pytest.mark.asyncio
    async def test_wrong_field_name_is_400(self) -> None:
        field = SimpleNamespace(name="video", filename="x.webm", read_chunk=AsyncMock())
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "stt_missing_audio_field"

    @pytest.mark.asyncio
    async def test_oversized_upload_is_413(self) -> None:
        field = SimpleNamespace(
            name="audio",
            filename="recording.mp4",
            read_chunk=AsyncMock(return_value=b"0" * (25 * 1024 * 1024 + 1)),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 413
        body = json.loads(resp.body)
        assert body["error"] == "audio too large"
        assert body["code"] == "stt_audio_too_large"

    @pytest.mark.asyncio
    async def test_over_duration_upload_is_refused_before_transcription(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "batch_duration_cap_secs", lambda _cfg: 3600)
        probe = AsyncMock(return_value=True)
        monkeypatch.setattr(core_mod, "audio_exceeds_secs", probe)
        transcribe = AsyncMock()
        monkeypatch.setattr("kiro_crew.transcribe.transcribe_audio", transcribe)
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"audio-bytes", b""]),
        )

        resp = await core_mod.api_stt_transcribe(_multipart_req(field))

        assert resp.status == 422
        body = json.loads(resp.body)
        assert body["code"] == "stt_audio_too_long"
        assert "60-minute" in body["error"]
        transcribe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unverified_duration_is_retryable_and_not_transcribed(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "batch_duration_cap_secs", lambda _cfg: 3600)
        monkeypatch.setattr(core_mod, "audio_exceeds_secs", AsyncMock(return_value=None))
        transcribe = AsyncMock()
        monkeypatch.setattr("kiro_crew.transcribe.transcribe_audio", transcribe)
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"audio-bytes", b""]),
        )

        resp = await core_mod.api_stt_transcribe(_multipart_req(field))

        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "stt_audio_duration_unverified"
        transcribe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transcript_is_returned_and_redacted(self, monkeypatch) -> None:
        """A dictated credential must not come back in the response body: speech
        reaches this endpoint from a microphone, so nothing upstream of it has had
        a chance to screen what was said."""
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(return_value="the key is AKIAIOSFODNN7EXAMPLE thanks"),
        )
        field = SimpleNamespace(
            name="audio",
            filename="recording.ogg",
            read_chunk=AsyncMock(side_effect=[b"audio-bytes", b""]),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 200
        text = json.loads(resp.body)["text"]
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        # The surrounding speech survives: redaction replaces the secret, it does
        # not discard the transcript the user is waiting for.
        assert text.startswith("the key is ")
        assert text.endswith(" thanks")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome",
        [
            AsyncMock(return_value="hello"),
            AsyncMock(return_value=None),
            AsyncMock(side_effect=RuntimeError("boom")),
        ],
        ids=["transcribed", "failed", "raised"],
    )
    async def test_upload_is_not_kept_on_disk(self, monkeypatch, outcome) -> None:
        """Settings tells the user the gateway keeps no audio, so the uploaded
        recording must be gone once the request answers, whatever the outcome."""
        seen: list[str] = []

        async def transcribe(path, _cfg):
            seen.append(path)
            assert os.path.exists(path)
            return await outcome(path, _cfg)

        monkeypatch.setattr("kiro_crew.transcribe.transcribe_audio", transcribe)
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"audio-bytes", b""]),
        )

        await core_mod.api_stt_transcribe(_multipart_req(field))

        assert len(seen) == 1
        assert not os.path.exists(seen[0])

    @pytest.mark.asyncio
    async def test_none_backend_result_is_a_generic_500(self, monkeypatch) -> None:
        """The backend's failure sentinel must not look like a silent recording."""
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(return_value=None),
        )
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"x", b""]),
        )

        resp = await core_mod.api_stt_transcribe(_multipart_req(field))

        assert resp.status == 500
        assert json.loads(resp.body) == {
            "error": "transcription failed",
            "code": "stt_transcription_failed",
        }

    @pytest.mark.asyncio
    async def test_empty_transcript_remains_a_success(self, monkeypatch) -> None:
        """A valid recording with no recognised speech remains distinguishable."""
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(return_value=""),
        )
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"x", b""]),
        )

        resp = await core_mod.api_stt_transcribe(_multipart_req(field))

        assert resp.status == 200
        assert json.loads(resp.body) == {"text": ""}

    @pytest.mark.asyncio
    async def test_backend_failure_is_a_generic_500(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(side_effect=RuntimeError("recogniser exploded")),
        )
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"x", b""]),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 500
        # The internal exception text must not reach the client.
        body = json.loads(resp.body)
        assert body["error"] == "transcription failed"
        assert body["code"] == "stt_transcription_failed"
        assert "recogniser exploded" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_a_departed_uploader_cancels_the_decode(self, monkeypatch) -> None:
        """An aborted upload stops the recogniser instead of decoding to the end."""
        cancelled = asyncio.Event()

        async def _decode_forever(_path, _cfg):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr("kiro_crew.transcribe.transcribe_audio", _decode_forever)
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"x", b""]),
        )
        req = _multipart_req(field)
        req.transport = SimpleNamespace(is_closing=lambda: True)
        resp = await finished(
            core_mod.api_stt_transcribe(req), "the transcribe handler after its client left"
        )
        assert cancelled.is_set()
        assert resp.status == 400

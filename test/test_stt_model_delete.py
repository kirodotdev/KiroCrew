"""Removing a downloaded speech model.

``DELETE /api/stt/models/{name}`` and ``ModelStore.remove`` behind it. What is
pinned: only a catalog entry's own file under the managed whisper directory is
ever deleted; the configured, resident and downloading model are refused with a
code rather than removed; and a removal leaves the store's status honest.

Nothing here touches the network or loads a model. The conftest isolates the
data home, so ``models_dir()`` is a per-test directory.
"""

from __future__ import annotations

import asyncio
import gc
import json
import weakref
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.config.loader import config_path
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.stt import engine as stt_engine
from kiro_crew.stt import models as stt_models
from kiro_crew.stt import session as stt_session


def _req(name: str, *, app_claim: str | None = "") -> web.Request:
    req = MagicMock(spec=web.Request)
    req.match_info = {"name": name}
    req.path = f"/api/stt/models/{name}"
    claims = {"user": "dashboard", "app": app_claim}
    req.get = lambda key, default=None: claims.get(key, default)
    return req


@pytest.fixture
def store(monkeypatch) -> stt_models.ModelStore:
    """A store of the test's own; the real one is a process global."""
    s = stt_models.ModelStore()
    monkeypatch.setattr(stt_models, "_store", s)
    return s


@pytest.fixture
def configured(monkeypatch):
    """Write ``stt.model`` into the isolated config and return a setter."""

    def _set(model: str) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stt": {"model": model}}) + "\n", encoding="utf-8")

    _set("base")
    return _set


@pytest.fixture
def resident(monkeypatch):
    """Control which model file the shared recogniser reports as loaded."""
    holder = SimpleNamespace(key=None, loading=False, released=[], keep=False, on_release=None)

    class _Engine:
        @property
        def loaded_key(self):
            return holder.key

        @property
        def loading(self):
            return holder.loading

        async def release_if_resident(self, filename):
            holder.released.append(filename)
            if holder.on_release is not None:
                holder.on_release()
            # ``keep`` stands for whatever put the model back during the await
            # (a reload): the route must re-check, not assume the release held.
            if not holder.keep:
                holder.key = None
            return not holder.keep

    fake = _Engine()
    monkeypatch.setattr(stt_engine, "shared_engine", lambda *a, **k: fake)
    # A fresh claim registry per test: a claim another test left alive
    # (a reference cycle the collector has not reached yet) must not pin a model.
    monkeypatch.setattr(stt_models, "_CLAIMS", weakref.WeakSet())

    def _set(
        model: stt_models.WhisperModel | None,
        *,
        loading: bool = False,
        keep: bool = False,
        on_release=None,
    ) -> None:
        holder.loading = loading
        holder.keep = keep
        holder.on_release = on_release
        holder.key = (
            None
            if model is None
            else stt_engine.LoadedKey(
                model_path=str(stt_models.model_path(model)), language="auto", n_threads=4
            )
        )

    _set.holder = holder
    return _set


def _install(model: stt_models.WhisperModel) -> None:
    path = stt_models.model_path(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"weights")


SMALL = next(m for m in stt_models.CATALOG if m.name == "small")
BASE = next(m for m in stt_models.CATALOG if m.name == "base")


async def _delete(name: str, **kw) -> tuple[int, dict]:
    resp = await core_mod.api_stt_model_delete(_req(name, **kw))
    return resp.status, json.loads(resp.body)


class TestDeleteRoute:
    @pytest.mark.asyncio
    async def test_removes_an_installed_model_that_is_not_selected(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        _install(BASE)
        status, body = await _delete("small")
        assert (status, body) == (200, {"model": "small", "removed": True})
        assert not stt_models.model_path(SMALL).exists()
        # Only the named model's file goes.
        assert stt_models.model_path(BASE).exists()

    @pytest.mark.asyncio
    async def test_a_model_not_on_disk_is_a_harmless_no_op(
        self, store, configured, resident
    ) -> None:
        assert await _delete("small") == (200, {"model": "small", "removed": False})

    @pytest.mark.asyncio
    async def test_refuses_the_configured_model(self, store, configured, resident) -> None:
        _install(BASE)
        status, body = await _delete("base")
        assert status == 409 and body["code"] == "stt_model_selected"
        assert stt_models.model_path(BASE).exists()

    @pytest.mark.asyncio
    async def test_refuses_a_configured_alias_of_the_model(
        self, store, configured, resident
    ) -> None:
        """``medium`` is stored by older configs and resolves to large-v3-turbo,
        so that file is the one voice input would load."""
        configured("medium")
        turbo = stt_models.resolve("large-v3-turbo")
        _install(turbo)
        status, body = await _delete("large-v3-turbo")
        assert status == 409 and body["code"] == "stt_model_selected"
        assert stt_models.model_path(turbo).exists()

    @pytest.mark.asyncio
    async def test_a_deselected_model_still_resident_is_released_then_removed(
        self, store, configured, resident
    ) -> None:
        """The table's main use: select a smaller model, then remove the one just
        used. It stays resident until the idle sweep (600 s by default), which
        must not turn that Remove into a refusal."""
        _install(SMALL)
        resident(SMALL)
        status, body = await _delete("small")
        assert (status, body) == (200, {"model": "small", "removed": True})
        assert resident.holder.released == [SMALL.filename]
        assert not stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_refuses_a_model_still_resident_after_the_release(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        resident(SMALL, keep=True)
        status, body = await _delete("small")
        assert status == 409 and body["code"] == "stt_model_in_use"
        assert stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_refuses_a_model_selected_again_while_it_was_released(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        resident(SMALL, on_release=lambda: configured("small"))
        status, body = await _delete("small")
        assert status == 409 and body["code"] == "stt_model_selected"
        assert stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_refuses_a_deselected_model_a_live_session_still_uses(
        self, store, configured, resident
    ) -> None:
        """A session keeps the model it started with after the user selects
        another. Releasing and deleting it would make the session's next final
        re-download the file, or drop the utterance when offline."""
        _install(SMALL)
        resident(SMALL)
        live = stt_session.LocalSession(model_name="small")
        status, body = await _delete("small")
        assert status == 409 and body["code"] == "stt_model_in_use"
        assert resident.holder.released == []
        assert stt_models.model_path(SMALL).exists()
        live.cancel()
        assert (await _delete("small"))[0] == 200
        assert not stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_refuses_a_model_a_session_started_on_during_the_release(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        started = []
        resident(
            SMALL, on_release=lambda: started.append(stt_session.LocalSession(model_name="small"))
        )
        status, body = await _delete("small")
        assert status == 409 and body["code"] == "stt_model_in_use"
        assert stt_models.model_path(SMALL).exists()
        for s in started:
            s.cancel()

    @pytest.mark.asyncio
    async def test_a_finished_or_dropped_session_does_not_pin_its_model(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        done = stt_session.LocalSession(model_name="small")
        done.cancel()
        dropped = stt_session.LocalSession(model_name="small")
        del dropped
        gc.collect()
        assert (await _delete("small"))[0] == 200
        assert done is not None

    @pytest.mark.asyncio
    async def test_refuses_a_model_a_batch_transcription_claimed(
        self, store, configured, resident
    ) -> None:
        """A batch request fixes its model before the upload and transcode awaits;
        removing it then would fail the decode and lose the recording."""
        _install(SMALL)
        held = stt_models.claim("small")
        status, body = await _delete("small")
        assert status == 409 and body["code"] == "stt_model_in_use"
        assert stt_models.model_path(SMALL).exists()
        stt_models.release(held)
        assert (await _delete("small"))[0] == 200

    @pytest.mark.asyncio
    async def test_refuses_a_model_claimed_during_the_release(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        claims = []
        resident(SMALL, on_release=lambda: claims.append(stt_models.claim("small")))
        status, body = await _delete("small")
        assert status == 409 and body["code"] == "stt_model_in_use"
        assert stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_transcribe_route_claims_its_model_for_the_whole_request(
        self, store, configured, resident, monkeypatch
    ) -> None:
        path = config_path()
        path.write_text(
            json.dumps({"stt": {"provider": "local", "model": "small"}}) + "\n", encoding="utf-8"
        )
        seen = []

        async def body(request, cfg):
            seen.append(stt_models.is_claimed("small"))
            return web.json_response({"text": ""})

        monkeypatch.setattr(core_mod, "_stt_transcribe", body)
        await core_mod.api_stt_transcribe(MagicMock(spec=web.Request))
        assert seen == [True]
        assert not stt_models.is_claimed("small")

    @pytest.mark.asyncio
    async def test_local_batch_transcription_claims_its_model(
        self, store, resident, monkeypatch
    ) -> None:
        from kiro_crew import transcribe
        from kiro_crew.config.loader import SttConfig

        seen = []

        async def body(audio_path, stt_config):
            seen.append(stt_models.is_claimed("small"))
            return ""

        monkeypatch.setattr(transcribe, "_transcribe_local_claimed", body)
        cfg = SttConfig(enabled=True, provider="local", model="small")
        assert await transcribe._transcribe_local("voice.wav", cfg) == ""
        assert seen == [True]
        assert not stt_models.is_claimed("small")

    @pytest.mark.asyncio
    async def test_releases_nothing_for_a_model_that_is_not_resident(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        resident(BASE)
        assert (await _delete("small"))[0] == 200
        assert resident.holder.released == []

    @pytest.mark.asyncio
    async def test_refuses_any_removal_while_a_model_is_loading(
        self, store, configured, resident
    ) -> None:
        """The resident key is set only when a load finishes, so a load still
        verifying or building could be reading the very file a delete would
        unlink. Until it settles nothing is removed."""
        _install(SMALL)
        resident(None, loading=True)
        status, body = await _delete("small")
        assert status == 409 and body["code"] == "stt_model_loading"
        assert stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_refuses_the_model_being_downloaded(self, store, configured, resident) -> None:
        _install(SMALL)
        store._set(step="downloading", model="small", done=1, total=SMALL.size_bytes)
        status, body = await _delete("small")
        assert status == 409 and body["code"] == stt_models.MODEL_REMOVE_DOWNLOADING
        assert stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_a_download_of_another_model_does_not_block_removal(
        self, store, configured, resident
    ) -> None:
        _install(SMALL)
        store._set(step="downloading", model="large-v3-turbo", done=1, total=10)
        assert (await _delete("small"))[0] == 200
        assert store.status["step"] == "downloading"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["../config.json", "ggml-small.bin", "medium", "", "small/../../x", "SMALL"]
    )
    async def test_only_exact_catalog_names_are_accepted(
        self, store, configured, resident, name
    ) -> None:
        """A name is looked up in the catalog, never joined onto a path, so an
        alias, a filename or a traversal is a 404 and touches nothing."""
        _install(SMALL)
        status, body = await _delete(name)
        assert status == 404 and body["code"] == "stt_model_unknown"
        assert stt_models.model_path(SMALL).exists()

    @pytest.mark.asyncio
    async def test_an_app_claim_is_refused(self, store, configured, resident, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_sel", lambda: MagicMock())
        _install(SMALL)
        status, _ = await _delete("small", app_claim="some-app")
        assert status == 403
        assert stt_models.model_path(SMALL).exists()

    def test_the_route_is_registered(self) -> None:
        """The handler is unreachable without its route, and 404 says nothing."""
        from kiro_crew.dashboard.routes import memory as memory_routes

        app = web.Application()
        memory_routes.register(app)
        paths = {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.resource is not None
        }
        assert ("DELETE", "/api/stt/models/{name}") in paths


class TestEngineLoading:
    """``WhisperEngine.loading`` covers every stage a load can read the file in."""

    @pytest.mark.asyncio
    async def test_idle_engine_is_not_loading(self) -> None:
        assert stt_engine.WhisperEngine(idle_evict_secs=60, timeout_secs=30).loading is False

    @pytest.mark.asyncio
    async def test_held_load_lock_is_loading(self) -> None:
        """The lock spans the digest check and the native build."""
        eng = stt_engine.WhisperEngine(idle_evict_secs=60, timeout_secs=30)
        load_lock, _ = eng._locks()
        async with load_lock:
            assert eng.loading is True
        assert eng.loading is False

    @pytest.mark.asyncio
    async def test_a_load_that_outlived_its_timeout_is_still_loading(self) -> None:
        """A build that overran keeps running after the lock is released."""
        eng = stt_engine.WhisperEngine(idle_evict_secs=60, timeout_secs=30)
        future = asyncio.get_running_loop().create_future()
        eng._load_future = future
        assert eng.loading is True
        future.set_result(None)
        assert eng.loading is False


class TestEngineRelease:
    """``WhisperEngine.release_if_resident`` drops only the named file's context."""

    def _loaded(self, model: stt_models.WhisperModel) -> stt_engine.WhisperEngine:
        eng = stt_engine.WhisperEngine(idle_evict_secs=60, timeout_secs=30)
        eng._model = object()
        eng._key = stt_engine.LoadedKey(
            model_path=str(stt_models.model_path(model)), language="auto", n_threads=4
        )
        return eng

    @pytest.mark.asyncio
    async def test_releases_the_resident_model(self, store) -> None:
        eng = self._loaded(SMALL)
        assert await eng.release_if_resident(SMALL.filename) is True
        assert eng.loaded_key is None and eng._model is None

    @pytest.mark.asyncio
    async def test_leaves_a_different_resident_model_alone(self, store) -> None:
        eng = self._loaded(BASE)
        assert await eng.release_if_resident(SMALL.filename) is False
        assert eng.loaded_key is not None

    @pytest.mark.asyncio
    async def test_waits_for_a_running_decode(self, store) -> None:
        """Holding the decode lock is what keeps a release from retiring the
        context under a decode, and a second context being built after it."""
        eng = self._loaded(SMALL)
        load_lock, decode_lock = eng._locks()
        await decode_lock.acquire()
        task = asyncio.ensure_future(eng.release_if_resident(SMALL.filename))
        # The release takes the load lock first, then waits on the decode lock.
        for _ in range(100):
            if load_lock.locked():
                break
            await asyncio.sleep(0)
        assert load_lock.locked()
        assert not task.done() and eng.loaded_key is not None
        decode_lock.release()
        assert await asyncio.wait_for(task, 5) is True


class TestStoreRemove:
    def test_refuses_a_claimed_model(self, store, monkeypatch) -> None:
        monkeypatch.setattr(stt_models, "_CLAIMS", weakref.WeakSet())
        _install(SMALL)
        held = stt_models.claim("small")
        with pytest.raises(stt_models.ModelRemovalRefused) as refused:
            store.remove(SMALL)
        assert refused.value.code == stt_models.MODEL_REMOVE_IN_USE
        assert stt_models.model_path(SMALL).exists()
        stt_models.release(held)
        assert store.remove(SMALL) is True

    def test_resets_a_ready_status_for_the_removed_model(self, store) -> None:
        _install(SMALL)
        store._set(step="ready", model="small", total=SMALL.size_bytes)
        assert store.remove(SMALL) is True
        assert store.status["step"] == "idle"

    def test_leaves_another_models_status_alone(self, store) -> None:
        _install(SMALL)
        store._set(step="failed", model="tiny", error="boom")
        store.remove(SMALL)
        assert store.status["model"] == "tiny" and store.status["step"] == "failed"

    def test_removes_a_symlink_without_touching_its_target(self, store, tmp_path) -> None:
        target = tmp_path / "elsewhere.bin"
        target.write_bytes(b"keep me")
        link = stt_models.model_path(SMALL)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        assert store.remove(SMALL) is True
        assert not link.is_symlink()
        assert target.read_bytes() == b"keep me"

    @pytest.mark.parametrize("model", stt_models.CATALOG, ids=lambda m: m.name)
    def test_every_catalog_path_is_one_file_directly_in_the_models_dir(self, model) -> None:
        """The invariant that confines a removal: the path is the models
        directory plus a filename that is one plain component."""
        path = stt_models.model_path(model)
        assert path.parent == stt_models.models_dir()
        assert path.name == model.filename
        assert "/" not in model.filename and "\\" not in model.filename
        assert model.filename not in (".", "..") and not model.filename.startswith(".")

"""Speech-to-text readiness: the status report and the steps that make a provider ready.

``/api/stt/status``, the custom vocabularies list, and the model download, decoder
fetch and prewarm a user starts from Settings, each answered at once and run in the
background.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import logging
import platform

from aiohttp import web

from kiro_crew import aws_consent, stt
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.core_stt_access import (
    _CODE_STT_DECODER_BUNDLED,
    _deny_app_token,
    _spawn_stt_background,
)
from kiro_crew.dashboard.stt_stream import PROVIDER_LOCAL
from kiro_crew.stt import capabilities as stt_capabilities
from kiro_crew.stt import decoder as stt_decoder
from kiro_crew.stt import models as stt_models
from kiro_crew.stt import telemetry as stt_telemetry
from kiro_crew.transcribe import (
    VOCABULARIES_ACCESS_DENIED,
    VOCABULARIES_LIST_FAILED,
    VOCABULARIES_LIST_PERMISSION,
    VocabularyListError,
    _whisper_language,
    availability_detail,
    ensure_ffmpeg_in_path,
    ffmpeg_source,
    list_custom_vocabularies,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.core")


async def api_stt_status(request: web.Request) -> web.Response:
    """GET /api/stt/status — whether speech recognition can run, and what it needs.

    Distinct from ``GET /api/config/stt``, which serves the operator's settings:
    this is the runtime state a panel polls — the availability reason as a code
    rather than prose, whether the configured model is on disk, whether a model is
    resident right now, and the live progress of a transfer started by
    ``POST /api/stt/prepare``.
    """
    denied = _deny_app_token(request, "stt.status")
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    # Off the loop: for `custom` this reads config to find the file and
    # digest, so a catalog lookup that would otherwise stay in memory performs file
    # I/O. Same reason the three async callers in `stt/` offload it.
    model = await asyncio.to_thread(stt_models.resolve, cfg.stt.model)

    # availability_detail imports the recogniser (or the AWS client), and each
    # is_present stats a model file: none of it belongs on the loop.
    def _probe() -> tuple[
        stt.Availability,
        list[dict[str, object]],
        bool,
        str | None,
        tuple[stt_capabilities.Capabilities | None, int],
    ]:
        # kiro_crew.stt.engine is imported HERE rather than at module scope: it
        # pulls numpy, and this module is imported on the gateway boot path, where
        # a gateway with speech-to-text switched off would otherwise pay for an
        # array library before it binds its socket.
        from kiro_crew.stt import engine as stt_engine

        catalog = [
            {
                "name": m.name,
                "size_bytes": m.size_bytes,
                "present": stt_models.is_present(m),
                # Empty for the full-precision conversions. Carried because a
                # quantized build is a different artifact with different accuracy,
                # and a picker that does not say so turns a deliberate speed/accuracy
                # trade into an unexplained difference in results.
            }
            for m in stt_models.CATALOG
        ]
        if model.name == stt_models.CUSTOM_MODEL:
            # Appended rather than being a catalog row, because a custom model is
            # configuration and not a published artifact. It still has to appear
            # here: this list is what renders the picker's presence line, and
            # without a row the panel would report the selected model as absent
            # forever. Last, because the list is ordered by download size and this
            # one has none to sort by. `error` says why an absent file is unusable,
            # since there is nothing to download in its place.
            try:
                stt_models.custom_model_file(model.custom_path)
                custom_error = ""
            except stt_models.CustomModelPathError as exc:
                custom_error = str(exc)
            catalog.append(
                {
                    "name": model.name,
                    "size_bytes": model.size_bytes,
                    "present": stt_models.is_present(model),
                    "error": custom_error,
                }
            )
        ensure_ffmpeg_in_path()
        # Read from the BUILD, not from the request we make of it: whisper.cpp's
        # context defaults ask for `use_gpu` and are granted it on a CPU-only wheel,
        # so the request is not evidence. See `kiro_crew.stt.capabilities`.
        #
        # Skipped entirely for a non-local provider. Reading it dlopens the native
        # library, and on macOS that first call runs `ggml_metal_device_init` --
        # measured at +31.8 MB resident and a Metal residency set held for the process
        # lifetime, plus a one-off 6.4 s `ggml_metal_library_init` on a cold library
        # cache. A user who dictates through cloud Transcribe, or who has voice off,
        # paid all of that for opening Settings once, to learn about an acceleration
        # that governs a provider they are not using.
        local = cfg.stt.enabled and cfg.stt.provider == PROVIDER_LOCAL
        backend = stt_engine.WhisperEngine.capabilities() if local else None
        threads = stt_engine.thread_count(cfg.stt.threads) if local else 0
        # Resolved on the same thread as the rest: it lists a store directory and,
        # when a candidate is there, hashes up to 80 MB to authenticate it.
        return (
            availability_detail(cfg.stt),
            catalog,
            stt_engine.shared_engine().loaded,
            ffmpeg_source(),
            (backend, threads),
        )

    detail, catalog, engine_loaded, decoder_source, probed = await asyncio.to_thread(_probe)
    backend, backend_threads = probed
    present = {str(row["name"]): bool(row["present"]) for row in catalog}
    return web.json_response(
        {
            "provider": cfg.stt.provider,
            "available": detail.ok,
            "code": detail.code,
            "detail": detail.detail,
            "model": model.name,
            # The stored `stt.model` that `model` was resolved from, so the panel
            # can tell this answer belongs to its current config and not to a
            # selection it has since changed.
            "configured_model": cfg.stt.model,
            "model_present": present.get(model.name, False),
            "model_bytes": model.size_bytes,
            # The whole catalog, in the order the picker offers it (smallest
            # first), with sizes as bytes so the dashboard formats them in the
            # reader's locale. `present` is why this cannot be a static frontend
            # table: it is per-host state that changes as models are fetched.
            "models": catalog,
            # Residency avoids model loading, but says nothing about decode speed.
            "engine_loaded": engine_loaded,
            # What the installed native build actually links. This is the answer to
            # "why is the same model faster in other software": the published wheels
            # are CPU-only, and before this there was nowhere a user could see that.
            # `accelerated` is false when the build cannot be interrogated, so a
            # surface reading it can never overstate what is present.
            # Absent rather than null-filled for a non-local provider: the panel
            # renders this block only when it is present, and a shape that says
            # `backend: cpu` for a cloud provider would be answering a question
            # nobody asked with a fact that does not apply.
            **(
                {
                    "backend": {
                        "name": backend.backend,
                        "accelerated": backend.accelerated,
                        "encoder_only": backend.encoder_only,
                        "detail": backend.detail,
                        "cpu_features": list(backend.cpu_features),
                        # The registries the build linked -- where the backend name
                        # comes from -- so a bug report shows the evidence.
                        "sections": list(backend.sections),
                        # Verbatim build report, for a bug report to quote.
                        "system_info": backend.raw,
                        "threads": backend_threads,
                        **stt_capabilities.host_summary(),
                    }
                }
                if backend is not None
                else {}
            ),
            # Stage timings for the most recent load and decodes: durations and
            # counts only, never audio and never a transcript. This is what makes
            # "voice input is slow" answerable -- a 1.6 GB model spends seconds in
            # its digest check before the recogniser is even asked to run, and one
            # total could not tell the two apart.
            "timings": stt_telemetry.recorder().snapshot(),
            "download": dict(stt_models.store().status),
            # The decoder every compressed input goes through, and what can be
            # done about it. `source` names WHICH of the three the transcode path
            # would run, because each one is repaired differently; `auto_fetch`
            # says whether this host can be fixed in place at all, so the panel
            # offers a button instead of a shell command; `os`/`arch` are the
            # GATEWAY's, not the browser's, and are what a hand-off to an agent
            # session needs in order to name the right remedy.
            "ffmpeg": {
                "present": decoder_source is not None,
                "source": decoder_source,
                "auto_fetch": _ffmpeg_auto_fetch(),
                "os": platform.system(),
                "arch": platform.machine(),
                "download": dict(stt_decoder.store().status),
            },
        }
    )


#: Whole-request ceiling on the vocabulary listing. The worker thread carries its
#: own per-attempt bounds; this one keeps a settings panel from waiting on every
#: page and retry of a slow or unreachable endpoint in sequence.
_STT_VOCABULARIES_TIMEOUT_SECS = 30


async def api_stt_vocabularies(request: web.Request) -> web.Response:
    """GET /api/stt/vocabularies — the Amazon Transcribe custom vocabularies on offer.

    Feeds the Voice panel's picker for ``stt.transcribe_vocabulary``. Read from the
    live configuration, never from the request, so the list is always the one the
    configured profile and region would really use. Both are echoed back: a client
    that changed either since asking can tell the list describes the old target.

    Two gates come before AWS, and both answer 200 with ``listed: false`` and an
    empty list, so the panel can tell "AWS was not asked" from "AWS has none" and
    judges the stored name only against a listed answer. Neither gate may be
    dropped, because the frontend declining to ask is not a gate:

    1. ``transcribe`` is not the selected provider: nothing has business calling it.
    2. Amazon Transcribe is not confirmed for this profile and region. Listing bills
       nothing, but no request reaches a paid service's account without the
       operator's recorded consent, and the consent card already explains the gap.

    A failure returns a ``code`` and never the service's message, which names the
    caller's ARN on an access denial.
    """
    denied = _deny_app_token(request, "stt.vocabularies")
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    profile, region = cfg.stt.transcribe_profile, cfg.stt.transcribe_region
    target = {"profile": profile, "region": region}
    if cfg.stt.provider != "transcribe":
        return web.json_response({**target, "listed": False, "vocabularies": []})
    if not await aws_consent.refuse_and_log(
        aws_consent.SERVICE_TRANSCRIBE, profile=profile, region=region
    ):
        return web.json_response({**target, "listed": False, "vocabularies": []})
    try:
        listing = await asyncio.wait_for(
            asyncio.to_thread(list_custom_vocabularies, profile, region),
            timeout=_STT_VOCABULARIES_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning("Listing custom vocabularies in %s timed out", region)
        return web.json_response(
            {"error": "listing custom vocabularies timed out", "code": VOCABULARIES_LIST_FAILED},
            status=502,
        )
    except VocabularyListError as exc:
        if exc.code == VOCABULARIES_ACCESS_DENIED:
            # The one fact the fix needs, as data rather than inside a sentence.
            return web.json_response(
                {
                    "error": "not allowed to list custom vocabularies",
                    "code": VOCABULARIES_ACCESS_DENIED,
                    "permission": VOCABULARIES_LIST_PERMISSION,
                },
                status=502,
            )
        return web.json_response(
            {"error": "could not list custom vocabularies", "code": VOCABULARIES_LIST_FAILED},
            status=502,
        )
    return web.json_response(
        {
            **target,
            "listed": True,
            "truncated": listing.truncated,
            "vocabularies": [
                {"name": v.name, "language_code": v.language_code, "state": v.state}
                for v in listing.vocabularies
            ],
        }
    )


async def api_stt_prepare(request: web.Request) -> web.Response:
    """POST /api/stt/prepare — start, or join, the one-time speech-model download.

    Answers 202 immediately with the current transfer state; the caller polls
    ``GET /api/stt/status`` for progress. Concurrent callers share one transfer:
    the model store serialises them behind its own lock, so pressing this twice
    cannot start two downloads of the same file.

    An optional ``{"model": name}`` body fetches a model the operator has not
    saved yet, so the picker can offer the weights BEFORE the selection is
    committed. Only a catalog name reaches the network: an unknown one resolves to
    the default with a logged reason, the same as the configured value does, and
    ``custom`` resolves to the file and digest already in config, which are checked
    and never downloaded.
    """
    denied = _deny_app_token(request, "stt.prepare")
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    try:
        body = await request.json()
    except Exception:
        # No body, or an unparseable one. Both mean "the configured model", which
        # is the only reading that makes this endpoint useful without a client.
        body = {}
    requested = body.get("model") if isinstance(body, dict) else None
    name = requested if isinstance(requested, str) and requested else cfg.stt.model
    model = await asyncio.to_thread(stt_models.resolve, name)
    if stt_models.store().status.get("step") != "downloading":
        # Skipped while a transfer is already running purely so a polling panel
        # cannot accumulate tasks; the store's lock, not this check, is what makes
        # concurrent callers safe.
        _spawn_stt_background(stt.ensure_model(name))
    return web.json_response(
        {"model": model.name, "download": dict(stt_models.store().status)}, status=202
    )


#: Refusal codes of ``DELETE /api/stt/models/{name}`` beyond the store's own.
_CODE_STT_MODEL_UNKNOWN = "stt_model_unknown"
_CODE_STT_MODEL_SELECTED = "stt_model_selected"
_CODE_STT_MODEL_IN_USE = "stt_model_in_use"
_CODE_STT_MODEL_LOADING = "stt_model_loading"
_CODE_STT_MODEL_REMOVE_FAILED = "stt_model_remove_failed"


async def api_stt_model_delete(request: web.Request) -> web.Response:
    """DELETE /api/stt/models/{name} -- remove one downloaded speech model.

    Lets the operator take back the disk a model they do not use holds, up to
    1.6 GB for ``large-v3-turbo``. Only a catalog name is accepted, and only that
    entry's file under the managed whisper directory is deleted (see
    ``ModelStore.remove``); an alias or an unknown name is a 404, never a path.

    Refused with 409, never silently resolved, when the model is:

    - the configured ``stt.model``. Removing it would make voice input
      re-download on the next dictation. The panel asks the user to pick another
      model first, so the selection stays explicit.
    - claimed by work that still reads it: a live voice session, which keeps the
      model it started with after the user selects another, a batch
      transcription between reading the config and its decode, or a meetings
      audio import across its probe and every segment of a split. Its next decode
      would re-download a removed file, or fail and lose the audio when offline.
    - resident in the recogniser while something still holds it: a load or a
      re-selection that landed while it was being released. A deselected model
      that is merely still resident (the idle sweep has not run) is released
      first, under the engine's load and decode locks, and then removed.
    - any model while the recogniser is loading one. The resident key is set only
      when a load finishes, so until then the file being read cannot be told apart.
    - being downloaded. Deleting under a running transfer races its final rename.

    A model that is not on disk answers 200 with ``removed: false``, so a second
    click is harmless.
    """
    denied = _deny_app_token(request, "stt.model_delete")
    if denied is not None:
        return denied
    name = request.match_info.get("name", "")
    model = next((m for m in stt_models.CATALOG if m.name == name), None)
    if model is None:
        return web.json_response(
            {"error": "unknown speech model", "code": _CODE_STT_MODEL_UNKNOWN}, status=404
        )
    cfg = KiroCrewConfig.load()
    if stt_models.resolve(cfg.stt.model).name == model.name:
        return web.json_response(
            {"error": "model is selected for transcription", "code": _CODE_STT_MODEL_SELECTED},
            status=409,
        )
    # Imported here for the reason api_stt_status gives: the engine pulls numpy.
    from kiro_crew.stt import engine as stt_engine

    # Work that fixed this model before awaiting (a live session, a batch
    # transcription) claims it. Releasing it would only force that work to reload;
    # `ModelStore.remove` refuses the delete itself.
    if stt_models.is_claimed(model.name):
        return web.json_response(
            {"error": "the model is still in use", "code": _CODE_STT_MODEL_IN_USE}, status=409
        )
    engine = stt_engine.shared_engine()
    # The full path, never the file name: a custom model may live in a subfolder
    # under a catalog file name (`sub/ggml-small.bin`), and that file is the user's
    # own selected model, not this catalog entry. A catalog path is
    # `models_dir() / model.filename` and touches no disk, and the residency key
    # is built from the same function, so the strings compare exactly.
    catalog_path = str(stt_models.model_path(model))
    # A model the user just deselected stays resident until the idle sweep, which
    # would refuse the table's main use (select a smaller model, then remove the
    # large one) for up to a day. It is not the configured model, so release it.
    key = engine.loaded_key
    if not engine.loading and key is not None and key.model_path == catalog_path:
        await engine.release_if_resident(catalog_path)
        # The await opened a window: re-read the selection before trusting it.
        if stt_models.resolve(KiroCrewConfig.load().stt.model).name == model.name:
            return web.json_response(
                {"error": "model is selected for transcription", "code": _CODE_STT_MODEL_SELECTED},
                status=409,
            )

    # No await from here to the reservation in `remove`, so neither a load, a
    # download nor a new claim can start between these checks and the delete.
    if engine.loading:
        # The resident key is set only when a load finishes, so a load in flight
        # may be reading this very file. Which model it is loading is not exposed
        # until then; refusing every removal for those seconds is the safe answer.
        return web.json_response(
            {"error": "a speech model is loading", "code": _CODE_STT_MODEL_LOADING}, status=409
        )
    key = engine.loaded_key
    if key is not None and key.model_path == catalog_path:
        return web.json_response(
            {"error": "model is loaded in the recogniser", "code": _CODE_STT_MODEL_IN_USE},
            status=409,
        )
    try:
        removed = await stt_models.store().remove(model)
    except stt_models.ModelRemovalRefused as exc:
        logger.warning("Refused to remove whisper model %s: %s", model.name, exc)
        return web.json_response({"error": str(exc), "code": exc.code}, status=409)
    except OSError as exc:
        logger.warning("Could not remove whisper model %s: %s", model.name, exc)
        return web.json_response(
            {"error": "could not remove the model file", "code": _CODE_STT_MODEL_REMOVE_FAILED},
            status=500,
        )
    return web.json_response({"model": model.name, "removed": removed})


#: Values of the status endpoint's ``ffmpeg.auto_fetch``. ``bundled`` is not
#: "available on a desktop app": a release carries its own authenticated decoder
#: and repairs itself by being reinstalled, so downloading one there would install
#: a second decoder the bundled resolver refuses to look at by design.
AUTO_FETCH_AVAILABLE = "available"


AUTO_FETCH_UNSUPPORTED = "unsupported"


AUTO_FETCH_BUNDLED = "bundled"


def _ffmpeg_auto_fetch() -> str:
    """Whether this host's decoder can be fetched, and if not, why not."""
    if stt_decoder.bundle_carries_decoder():
        return AUTO_FETCH_BUNDLED
    if stt_decoder.artifact_for() is None:
        return AUTO_FETCH_UNSUPPORTED
    return AUTO_FETCH_AVAILABLE


async def api_stt_ffmpeg_download(request: web.Request) -> web.Response:
    """POST /api/stt/ffmpeg/download — start, or join, the decoder fetch.

    Answers 202 with the current transfer state and lets the caller poll
    ``GET /api/stt/status``, for the same reason ``POST /api/stt/prepare`` does: a
    ~30 MB wheel is not something to hold a request open behind. Concurrent
    callers share one transfer through the store's own lock.

    Refused on a bundled interpreter rather than quietly answering 202: a desktop
    release already carries an authenticated decoder, its resolver deliberately
    never looks anywhere else, and so a fetch there would spend the operator's
    bandwidth on a file nothing can use.
    """
    denied = _deny_app_token(request, "stt.ffmpeg_download")
    if denied is not None:
        return denied
    auto_fetch = _ffmpeg_auto_fetch()
    if auto_fetch != AUTO_FETCH_AVAILABLE:
        return web.json_response(
            {
                "error": "no decoder can be fetched for this install",
                "code": (
                    stt_decoder.CODE_UNSUPPORTED
                    if auto_fetch == AUTO_FETCH_UNSUPPORTED
                    else _CODE_STT_DECODER_BUNDLED
                ),
                "auto_fetch": auto_fetch,
            },
            status=409,
        )
    store = stt_decoder.store()
    if store.status.get("stage") != stt_decoder.STAGE_DOWNLOADING:
        # Skipped while a transfer is already running purely so a polling panel
        # cannot accumulate tasks; the store's lock, not this check, is what makes
        # concurrent callers safe.
        _spawn_stt_background(store.ensure())
    return web.json_response({"download": dict(store.status)}, status=202)


async def api_stt_prewarm(request: web.Request) -> web.Response:
    """POST /api/stt/prewarm — load and warm the recogniser ahead of the microphone.

    Fire-and-forget, and called when the user reaches for the mic rather than when
    they release it: a first-ever model load compiles a GPU pipeline (measured at
    7.4 s) and the first decode after any load allocates its graph (154-528 ms), so
    both are paid while the user is still speaking instead of after.
    """
    denied = _deny_app_token(request, "stt.prewarm")
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    if cfg.stt.provider != PROVIDER_LOCAL:
        # Prewarming is specific to the resident whisper.cpp model. Running it under
        # `apple` or `transcribe` loaded — and on a first run DOWNLOADED — 148 MB of
        # weights the configured provider will never decode with, triggered by
        # nothing more than the user reaching for the microphone.
        return web.json_response(
            {"ok": True, "skipped": "provider_not_local"},
            status=202,
        )
    _spawn_stt_background(
        stt.prewarm(
            model_name=cfg.stt.model,
            language=_whisper_language(cfg.stt.language_code),
            threads=cfg.stt.threads,
        )
    )
    return web.json_response({"ok": True}, status=202)

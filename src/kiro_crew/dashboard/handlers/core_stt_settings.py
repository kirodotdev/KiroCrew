"""The speech-to-text settings route and the choices it offers.

``GET``/``PUT /api/config/stt``: the providers, models and language codes the
Settings pickers list, and the install commands that name what a provider still
needs before it can run.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import platform
import shlex
import shutil

from aiohttp import web

from kiro_crew import platform_compat, stt
from kiro_crew.config.loader import _VALID_STT_PROVIDERS, KiroCrewConfig, config_path
from kiro_crew.config.sections import (
    STT_LANGUAGE_AUTO,
    TRANSCRIBE_LANGUAGES_MAX,
    TRANSCRIBE_LANGUAGES_MIN,
    SttConfig,
    transcribe_language_options,
    transcribe_vocabulary_name,
    valid_stt_providers,
)
from kiro_crew.cpu_affinity import affinity_cpu_count
from kiro_crew.dashboard.chat_utils import drained_to_thread
from kiro_crew.dashboard.handlers._shared import (
    _pip_install_channel_available,
    pip_extra_install_command,
)
from kiro_crew.dashboard.handlers.core_stt_access import (
    _audit_custom_model_disclosure,
    _deny_non_owner_stt_custom_model,
    _stt_custom_model_refusal,
    _stt_positive_int,
)
from kiro_crew.dashboard.stt_stream import PROVIDER_LOCAL, streaming_providers
from kiro_crew.speech_languages import COMMON_LANGUAGE_CODES
from kiro_crew.stt import decoder as stt_decoder
from kiro_crew.stt import models as stt_models
from kiro_crew.stt.limits import (
    MAX_IDLE_EVICT_SECS,
    MAX_INTERVAL_MS,
    MIN_IDLE_EVICT_SECS,
    MIN_PARTIAL_INTERVAL_MS,
    MIN_SILENCE_MS,
)
from kiro_crew.stt.limits import THREADS_AUTO as STT_THREADS_AUTO
from kiro_crew.stt.limits import THREADS_ENV as STT_THREADS_ENV
from kiro_crew.stt.limits import parse_threads as parse_stt_threads
from kiro_crew.stt.limits import resolve_thread_count as resolve_stt_thread_count
from kiro_crew.transcribe import _find_ffmpeg, availability_detail, ensure_ffmpeg_in_path

logger = logging.getLogger("kiro_crew.dashboard.handlers.core")


# ── STT (Speech-to-Text) ──


#: Speech models accepted on PUT, mapped to their download size in BYTES — the
#: number that actually decides the choice on a laptop. DERIVED from the
#: sha256-pinned catalog rather than restated, so a model the recogniser can fetch
#: cannot be rejected by the API (or the reverse). Bytes rather than a formatted
#: label because the dashboard is translated into 12 languages: a server-side
#: "~148 MB" cannot follow the reader's locale, and the frontend formats it.
_STT_MODEL_SIZES: dict[str, int] = {m.name: m.size_bytes for m in stt_models.CATALOG}


def _stt_providers() -> list[str]:
    """STT provider values offered to the UI.

    ``local`` (the resident whisper.cpp recogniser) and ``transcribe`` (paid AWS)
    run everywhere. ``apple`` (the on-device SpeechAnalyzer framework) needs
    macOS 26 or later plus a Swift toolchain, so it is omitted entirely rather
    than shown as an option that cannot be selected. This is the single source of
    truth for which providers are advertised (GET) and accepted (PUT).
    """
    # Through the loader's own set, so an edition-registered provider is offered
    # and accepted too (ahead of ``off``). The default registry gives exactly
    # ``_VALID_STT_PROVIDERS``.
    providers = list(valid_stt_providers())
    if "apple" in providers:
        from kiro_crew import apple_speech

        if not apple_speech.availability().ok:
            providers.remove("apple")
    return providers


def _builtin_stt_rows(names: list[str]) -> dict[str, dict]:
    """Capability rows for the built-in providers in *names*: ``{name, builtin}``.

    No probe runs: the dashboard reads a row's status only for a registered
    provider, and labels a built-in from its own catalog by ``name``. The
    selected built-in's readiness is the response's own ``available`` / ``code``.
    """
    return {name: {"name": name, "builtin": True} for name in names if name in _VALID_STT_PROVIDERS}


async def _stt_provider_rows(
    names: list[str], built: dict[str, dict], stt_cfg: SttConfig
) -> list[dict]:
    """The ``providers`` list: one row per name in *names*, in that order.

    *built* holds the built-in rows; the edition's providers are checked here, on
    the loop, each bounded by ``speech_providers.CHECK_TIMEOUT_SECS``.
    """
    import dataclasses

    from kiro_crew import speech_providers

    registered = tuple(
        p for n in names if n not in built and (p := speech_providers.stt_provider(n)) is not None
    )
    extra = {
        row["name"]: row
        for row in await speech_providers.registered_entries(
            registered,
            lambda provider: dataclasses.replace(stt_cfg, provider=provider.name, enabled=True),
        )
    }
    return [row for n in names if (row := built.get(n) or extra.get(n)) is not None]


# Common BCP-47 language codes surfaced in the Chat Settings STT picker, and the
# candidates the Transcribe mixed-language picker offers. The handler accepts any
# string value on PUT; this list only drives the UI dropdowns, and GET adds the
# stored values to it so a code set in config.json stays selectable. AWS
# Transcribe supports many more. The list lives in kiro_crew.speech_languages
# because the Text-to-Speech Language picker offers the same codes.
_STT_LANGUAGE_CODES: tuple[str, ...] = COMMON_LANGUAGE_CODES


def _stt_language_codes(stt_cfg: SttConfig) -> list[str]:
    """The picker's codes for *stt_cfg*: ``auto`` first for the local provider, the
    common codes, then any stored code they do not already hold.

    A stored code outside the common list comes from config.json or an older
    build. Offering it keeps it on screen as a choice, so changing the language and
    changing back cannot lose it.
    """
    codes = [STT_LANGUAGE_AUTO] if stt_cfg.provider == PROVIDER_LOCAL else []
    codes += list(_STT_LANGUAGE_CODES)
    for stored in (stt_cfg.effective_language_code, *stt_cfg.transcribe_languages):
        if stored not in codes:
            codes.append(stored)
    return codes


#: Thread counts the Settings picker offers, before trimming to the host's cores.
#: Powers of two plus the in-between widths that were benchmarked on a 128-core
#: host, where decode time kept falling to 64 threads and flattened after it.
_STT_THREAD_LADDER = (1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)


def _stt_thread_options(current: int) -> list[int]:
    """Choices for the ``stt.threads`` picker: 0 (auto), then counts this host can run.

    The host's own core count closes the list so the widest choice is always
    reachable, and *current* is kept even when it exceeds the cores here (a config
    carried over from a larger machine), so the picker shows the stored value
    rather than silently reading as a different one.
    """
    cpus = affinity_cpu_count() or 1
    counts = {n for n in _STT_THREAD_LADDER if n <= cpus}
    counts.add(cpus)
    if current > 0:
        counts.add(current)
    return [STT_THREADS_AUTO, *sorted(counts)]


def _stt_threads_env() -> str | None:
    """The thread count ``KIROCREW_STT_THREADS`` makes the engine use, or ``None``.

    ``None`` both when the variable is unset and when it does not parse: the engine
    ignores an unusable value, so reporting it would tell the panel an override is
    in force when ``stt.threads`` is what actually runs. A value that parses is
    reported as the count the engine resolves it to, not as typed, so ``auto``,
    ``0048`` or a count above this host's cores reads as the number that runs.

    Resolved through :func:`kiro_crew.stt.limits.resolve_thread_count` and
    :func:`kiro_crew.cpu_affinity.affinity_cpu_count`, both leaf modules that import
    nothing heavy, rather than :func:`kiro_crew.stt.engine.thread_count`. The engine
    module imports numpy at module scope, and this runs synchronously on the GET
    ``/api/config/stt`` path while the response dict is built; importing it here
    would pay a cold multi-hundred-millisecond import on the event loop the first
    time any gateway served that page. The resolution is pure arithmetic, so no
    import of it is needed, and no path through this function can block the loop.
    """
    raw = os.environ.get(STT_THREADS_ENV, "").strip()
    parsed = parse_stt_threads(raw) if raw else None
    if parsed is None:
        return None
    return str(resolve_stt_thread_count(parsed, affinity_cpu_count() or 1))


async def api_stt_config(request: web.Request) -> web.Response:
    """GET/PUT /api/config/stt — speech-to-text settings."""
    cfg = KiroCrewConfig.load()
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        # Every field read below is a membership test followed by a subscript, and
        # a JSON array satisfies `"model" in body` while `body["model"]` raises
        # TypeError — a 500 out of a partial-update endpoint, and out of the
        # security gate on the next line. Refused once here rather than type-tested
        # at each of the fifteen fields, matching the theme handler in ``core.py``.
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "request body must be an object", "code": "body_not_object"}, status=400
            )
        # Before the lock and before any write: the custom-model fields are the one
        # part of this endpoint a non-owner caller may not touch. See
        # `_deny_non_owner_stt_custom_model` for why they and not the rest.
        denied = await _deny_non_owner_stt_custom_model(request, body)
        if denied is not None:
            return denied
        refused = await asyncio.to_thread(_stt_custom_model_refusal, body)
        if refused is not None:
            return refused
        path = config_path()
        from kiro_crew.config.loader import ConfigReadError, update_config_locked  # noqa: F811
        from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

        # The whole read-modify-write runs inside ``update_config_locked``, which
        # holds the advisory lock on the sidecar ``<path>.lock`` from its read to
        # its atomic write (temp + fsync + os.replace), so a writer in ANOTHER
        # PROCESS cannot land between the two and a crash mid-write cannot leave
        # a corrupt file. The in-process ``_get_config_lock()`` serializes the
        # PUTs of this gateway with its other config writers. ``fresh`` is the
        # file as read under that lock, not a snapshot taken before it.
        def _apply_stt(fresh: dict) -> dict:
            stt_section = fresh.get("stt")
            if not isinstance(stt_section, dict):
                stt_section = {}
                fresh["stt"] = stt_section
            if "enabled" in body:
                stt_section["enabled"] = bool(body["enabled"])
            # Guard the type before either membership lookup.  The model catalog
            # is a dict, so a JSON object or array would otherwise raise
            # ``TypeError: unhashable type`` and turn this partial update into a
            # 500.  Wrong-typed fields follow the existing config contract: skip
            # that field while still applying valid siblings.
            if (
                "provider" in body
                and isinstance(body["provider"], str)
                and body["provider"] in _stt_providers()
            ):
                stt_section["provider"] = body["provider"]
            if "model" in body:
                # `canonical_name`, not a membership test against the catalog and not
                # `resolve`. The alias table holds names the catalog does not: a user
                # whose stored model is `medium` or `large-v2` has a legal value that
                # is not a row, and a membership test rejects it -- so saving this
                # panel failed on a field they never edited. `resolve` is the wrong
                # tool in the other direction: it answers the DEFAULT for an unknown
                # name, so using it here would let a junk value overwrite a good
                # stored one. `custom` is admitted beside it because it is legal
                # without being a catalog row either -- `resolve` special-cases it
                # against the path/digest pair -- so `canonical_name` answers None.
                canonical = (
                    stt_models.CUSTOM_MODEL
                    if body["model"] == stt_models.CUSTOM_MODEL
                    else stt_models.canonical_name(body["model"])
                )
                if canonical is not None:
                    stt_section["model"] = canonical
            # Accepted independently of `model` so the pair can be filled in BEFORE
            # `custom` is selected: the config loader degrades a `custom` selection
            # with no usable pair back to the default model, so writing the two in
            # the other order would store a selection that reads back as `base`.
            # `_stt_custom_model_refusal` has already refused a value that cannot be
            # used, so what reaches here is usable or `""`; normalised with the same
            # helpers the loader uses, so a value this endpoint stores is one the
            # loader keeps.
            if "custom_model_path" in body:
                stt_section["custom_model_path"] = stt_models.valid_custom_path(
                    body["custom_model_path"]
                )
            if "custom_model_sha256" in body:
                stt_section["custom_model_sha256"] = stt_models.valid_custom_sha256(
                    body["custom_model_sha256"]
                )
            if "transcribe_region" in body and isinstance(body["transcribe_region"], str):
                stt_section["transcribe_region"] = body["transcribe_region"]
            if "transcribe_profile" in body and isinstance(body["transcribe_profile"], str):
                stt_section["transcribe_profile"] = body["transcribe_profile"]
            # Through the loader's own rule, so a name accepted here is never one the
            # next load drops. ``""`` clears it; an unusable name is skipped like any
            # other malformed field, leaving the stored vocabulary in force.
            if "transcribe_vocabulary" in body:
                vocabulary = transcribe_vocabulary_name(body["transcribe_vocabulary"])
                if vocabulary is not None:
                    stt_section["transcribe_vocabulary"] = vocabulary
            # Same rule as the loader: a list it would drop (too long, a malformed
            # code, two dialects of one language) is skipped here, leaving the
            # stored candidates in force.
            if "transcribe_languages" in body:
                languages = transcribe_language_options(body["transcribe_languages"])
                if languages is not None:
                    stt_section["transcribe_languages"] = languages
            if "language_code" in body and isinstance(body["language_code"], str):
                stt_section["language_code"] = body["language_code"]
            if "streaming" in body and isinstance(body["streaming"], bool):
                stt_section["streaming"] = body["streaming"]
            if "endpointing" in body and isinstance(body["endpointing"], bool):
                stt_section["endpointing"] = body["endpointing"]
            if "submit_on_release" in body and isinstance(body["submit_on_release"], bool):
                stt_section["submit_on_release"] = body["submit_on_release"]
            # `polish` is the one CONSENT setting on this surface -- it sends the
            # finished transcript to a model -- so its round trip is load-bearing in a
            # way an ordinary toggle's is not. Omitting it here is what made the
            # feature unreachable: the toggle wrote nothing, the reload read the
            # default back, and `api_stt_polish` refuses while the flag is False, so
            # the endpoint, the hook and the panel were all correct and the feature
            # was still dead. Nothing in the UI said so.
            if "polish" in body and isinstance(body["polish"], bool):
                stt_section["polish"] = body["polish"]
            if "dictation_panel" in body and isinstance(body["dictation_panel"], bool):
                stt_section["dictation_panel"] = body["dictation_panel"]
            # Every bound comes from kiro_crew.stt.limits, which is what the
            # recogniser itself reads, and the endpoint accepts exactly the range
            # the config loader will keep. Refusing here rather than storing a
            # value the loader then clamps is the whole point: a setting a user
            # reads back has to be the setting in force. MIN_IDLE_EVICT_SECS is 0
            # and it means "release the model as soon as it goes idle", not
            # "never release".
            silence_ms = _stt_positive_int(
                body, "silence_ms", minimum=MIN_SILENCE_MS, maximum=MAX_INTERVAL_MS
            )
            if silence_ms is not None:
                stt_section["silence_ms"] = silence_ms
            partial_interval = _stt_positive_int(
                body,
                "partial_interval_ms",
                minimum=MIN_PARTIAL_INTERVAL_MS,
                maximum=MAX_INTERVAL_MS,
            )
            if partial_interval is not None:
                stt_section["partial_interval_ms"] = partial_interval
            idle_evict = _stt_positive_int(
                body,
                "idle_evict_secs",
                minimum=MIN_IDLE_EVICT_SECS,
                maximum=MAX_IDLE_EVICT_SECS,
            )
            if idle_evict is not None:
                stt_section["idle_evict_secs"] = idle_evict
            # Through the loader's own parser, so the endpoint stores exactly what a
            # load keeps: 0 for auto (``"auto"`` is accepted as its spelling) or a
            # count up to MAX_THREADS. Junk is skipped like any other malformed
            # field. A count above this host's cores is stored as given and lowered
            # when the model loads, so moving the config to a bigger host keeps it.
            if "threads" in body:
                threads = parse_stt_threads(body["threads"])
                if threads is not None:
                    stt_section["threads"] = threads
            return fresh

        async with _get_config_lock():
            # Off-loop: file IO under a lock another process may hold. Drained,
            # so a cancelled PUT cannot release the config lock while the thread
            # is still rewriting the file.
            try:
                await drained_to_thread(
                    functools.partial(update_config_locked, path, mutate=_apply_stt)
                )
            except ConfigReadError:
                # Fail loud on a corrupt config rather than proceeding with {}:
                # a write from a {} base would durably clobber every other user
                # setting with an stt-only file. Matches the sibling config
                # handler in ``core_config_view.py``, which returns 500 on an unparseable
                # config instead of silently resetting it.
                logger.warning("STT config PUT: config.json is unparseable", exc_info=True)
                return web.json_response({"error": "failed to read config file"}, status=500)
        cfg = KiroCrewConfig.load()

    provider = cfg.stt.provider
    # Who this response is being assembled for. The custom-model pair below is the
    # owner's alone, on the same predicate that gates writing it, so the GET cannot
    # serve a value the PUT would refuse. Local import for the reason given in
    # `_deny_non_owner_stt_custom_model`.
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    owner_request = is_owner_dashboard_request(request)
    _audit_custom_model_disclosure(request, cfg, served=owner_request)
    # Every probe below touches the filesystem or imports an optional extra, so
    # none of them belongs on the event loop, and they ride one thread rather than
    # several: _stt_prereq_commands resolves ffmpeg and Homebrew, the
    # install-channel probe reads the PEP 668 marker, and availability_detail
    # imports the recogniser (`local`) or the AWS client (`transcribe`). Leaving
    # the availability probe out of the thread would put the heaviest one of the
    # set outside the thread that exists to hold the lighter ones. Windows was
    # where this first showed up, as "event-loop heartbeat: lag".

    provider_names = _stt_providers()

    def _prereqs_and_probes() -> tuple[list[str], bool, bool, bool, bool, str, dict[str, dict]]:
        cmds = _stt_prereq_commands(provider)
        ensure_ffmpeg_in_path()
        # `_find_ffmpeg`, not a bare `which`: the settings panel must report on the
        # binary the transcode path would actually run, and that one ignores PATH.
        no_ffmpeg = _find_ffmpeg() is None
        unsupported = not _transcribe_extra_importable() and not _pip_install_channel_available()
        # The bundled desktop app is the one unsupported cause with different
        # user guidance (no Python environment of the user's own to fix), so
        # the UI needs to distinguish it from the pip-less/PEP 668 causes.
        bundled = platform_compat.is_bundled_interpreter()
        # Derive both `available` and `code` from the SAME detail (the same probe
        # api_stt_status uses) so the two fields cannot disagree, and the chat
        # modal can render the precise per-code reason from this one query.
        detail = availability_detail(cfg.stt)
        builtin_rows = _builtin_stt_rows(provider_names)
        return cmds, no_ffmpeg, unsupported, bundled, detail.ok, detail.code, builtin_rows

    (
        prereqs,
        ffmpeg_missing,
        transcribe_unsupported,
        bundled_app,
        available,
        availability_code,
        builtin_rows,
    ) = await asyncio.to_thread(_prereqs_and_probes)
    provider_rows = await _stt_provider_rows(provider_names, builtin_rows, cfg.stt)
    return web.json_response(
        {
            "enabled": cfg.stt.enabled,
            "provider": provider,
            "model": cfg.stt.model,
            # The custom-model pair, so the panel can show what is configured and
            # edit it. Served to the OWNER dashboard alone, the same bar that
            # `_deny_non_owner_stt_custom_model` puts on writing them: serving them
            # to a caller that may not write them would hand it the exact values to
            # echo back. `custom_model_dir` is the folder the file has to be in, so
            # the panel can say where to put it.
            **(
                {
                    "custom_model_path": cfg.stt.custom_model_path,
                    "custom_model_sha256": cfg.stt.custom_model_sha256,
                    "custom_model_dir": str(stt_models.models_dir()),
                }
                if owner_request
                else {}
            ),
            "available": available,
            "code": availability_code,
            "streaming": cfg.stt.streaming,
            "endpointing": cfg.stt.endpointing,
            "submit_on_release": cfg.stt.submit_on_release,
            "dictation_panel": cfg.stt.dictation_panel,
            "polish": cfg.stt.polish,
            "transcribe_region": cfg.stt.transcribe_region,
            "transcribe_profile": cfg.stt.transcribe_profile,
            "transcribe_vocabulary": cfg.stt.transcribe_vocabulary,
            "transcribe_languages": cfg.stt.transcribe_languages,
            # The bounds the mixed-language picker enforces, served from the same
            # constants the PUT and the loader validate against.
            "transcribe_languages_min": TRANSCRIBE_LANGUAGES_MIN,
            "transcribe_languages_max": TRANSCRIBE_LANGUAGES_MAX,
            "language_code": cfg.stt.effective_language_code,
            "silence_ms": cfg.stt.silence_ms,
            "partial_interval_ms": cfg.stt.partial_interval_ms,
            "idle_evict_secs": cfg.stt.idle_evict_secs,
            "threads": cfg.stt.threads,
            # The picker's choices for `threads`, sized to this host so it offers
            # no new count the engine would lower. The one exception is a stored
            # count above this host's cores (a config from a larger machine): it
            # stays listed so the picker shows it, and the engine clamps it to the
            # cores at load. `threads_env` is set only when
            # KIROCREW_STT_THREADS parses, because only then does it override the
            # stored value in the gateway: the engine logs an unusable one and keeps
            # `threads`. The panel uses it to say why a change there has no effect.
            "thread_options": _stt_thread_options(cfg.stt.threads),
            # The count Auto resolves to on this host, so the picker can label
            # Auto with the number it stands for. Pure arithmetic over the
            # affinity count, like `_stt_threads_env`, so it never imports the engine.
            "threads_auto": resolve_stt_thread_count(STT_THREADS_AUTO, affinity_cpu_count() or 1),
            "threads_env": _stt_threads_env(),
            # The PUT allowlist, so a picker built from it cannot offer a value
            # this endpoint would reject. Which of them are already on disk is
            # runtime state and belongs to GET /api/stt/status, which is also what
            # a panel polls during a transfer — probing four files on every config
            # read would put that cost on the wrong request.
            "models": _STT_MODEL_SIZES,
            # One row per selectable provider: the built-ins in today's order, then
            # the edition's (``PlatformContext.speech``), then ``off``. Each row is
            # ``{name, label, builtin, available, reason, action}``, where
            # ``action`` is a sign-in step a hosted provider asks for, or null.
            "providers": provider_rows,
            # Which of those providers can stream partial results. Served from the
            # backend's own `streaming_providers()` so the Settings UI gates the
            # streaming controls on a CAPABILITY rather than on a hardcoded provider
            # name — the latter silently hid the toggle when `apple` was added.
            "streaming_providers": list(streaming_providers()),
            "language_codes": _stt_language_codes(cfg.stt),
            "prereqs": prereqs,
            # True when no install channel can make Transcribe's import
            # requirement (`boto3` + `amazon-transcribe`) satisfiable in this
            # process — frozen build, pip-less interpreter, or PEP 668
            # externally-managed python. The Settings page shows an unsupported
            # notice instead of an empty prerequisite panel. Computed in the
            # threaded probe above: find_spec and the marker check touch the
            # filesystem.
            "transcribe_unsupported": transcribe_unsupported,
            "bundled_interpreter": bundled_app,
            # ffmpeg is required to decode the browser's .webm on the batch upload
            # path, but is_available() only logs a warning when it is absent — so
            # availability can read "ready" while an upload would fail. Served
            # separately so the UI can surface the gap even when the provider is
            # otherwise available.
            "ffmpeg_missing": ffmpeg_missing,
        }
    )


def _transcribe_extra_importable() -> bool:
    """True when AWS Transcribe's half of the ``voice`` extra imported here.

    Reads ``kiro_crew.transcribe``'s own import outcome (its module-level
    try/except sets ``boto3 = None`` on failure) rather than probing specs: a
    partial installation whose dist-info exists but whose import fails must
    surface the repair command, not suppress it. Runs off the event loop —
    ``transcribe`` is already imported at module load, so this is an attribute
    read, but callers batch it with the other filesystem probes anyway.
    """
    from kiro_crew import transcribe

    if transcribe.boto3 is None:
        return False
    try:
        import amazon_transcribe  # noqa: F401
    except ImportError:
        return False
    return True


def _ffmpeg_install_commands() -> list[str]:
    """System-decoder commands a source install can actually run, else ``[]``.

    An empty list means "there is nothing a terminal can usefully be told here",
    and the Settings page then offers the decoder fetch or a hand-off to an agent
    session instead. It is not the same as "nothing is wrong": ``ffmpeg.present``
    on ``GET /api/stt/status`` is what says whether a decoder exists.

    There is deliberately no fallback command. A distribution with no FFmpeg
    package (Amazon Linux, RHEL without EPEL) and no build script in reach gets an
    empty list rather than ``echo 'Build ffmpeg from source: …'``: a command a user
    pastes into a terminal only to get a URL echoed back -- one whose only effect is
    to print a sentence -- is a dead end wearing the costume of an instruction.
    """
    ensure_ffmpeg_in_path()
    if _find_ffmpeg():
        return []
    system = platform.system()
    if system == "Darwin":
        return ["brew install ffmpeg"]
    if system == "Windows":
        return ["winget install --id Gyan.FFmpeg"]
    if shutil.which("apt-get"):
        return ["sudo apt-get install -y ffmpeg"]
    # Amazon Linux: no ffmpeg in the distro repos. A source build is the only
    # honest answer, and only when the script is actually present -- naming a path
    # that does not exist is worse than saying nothing.
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    script = os.path.join(proj, "scripts", "build-ffmpeg.sh") if proj else ""
    if script and os.path.isfile(script):
        return [
            "sudo dnf install -y gcc make nasm diffutils 2>/dev/null"
            " || sudo yum install -y gcc make nasm diffutils",
            f"bash {shlex.quote(script)}",
        ]
    return []


def _stt_prereq_commands(provider: str = "local") -> list[str]:
    """Shell commands the user has to run themselves (they need sudo, a GUI, or a shell).

    Returns the actionable commands the UI surfaces (Settings -> Voice and the
    microphone modal): the ``pip`` command for the missing ``voice``/``voice-aws``
    extra when that extra is absent and a pip channel into this interpreter
    exists, plus the system ffmpeg install command(s) for batch WebM/voice-memo
    input. The extra to name depends on the provider, because an extra resolves
    atomically and advising the full local set to a cloud-only user can fail the
    whole install.

    Desktop builds bundle both runtime pieces and run on a frozen interpreter, so
    they return an empty list -- the steady state, meaning "nothing to do".
    """
    cmds: list[str] = []
    # Which extra to name depends on the provider, because the two halves are
    # separately installable and an extra resolves ATOMICALLY: advising the full
    # `voice` set to a cloud-only user drags in the local recogniser, which has
    # no wheel on some platforms and fails the whole install.
    extra = ""
    if provider == PROVIDER_LOCAL:
        # Only the missing-extra case is actionable by pip. A platform with no
        # prebuilt wheel needs a C++ toolchain instead, and the availability
        # `detail` on GET /api/stt/status is what says so.
        needs_extra = stt.availability().code == stt.CODE_EXTRA_MISSING
        extra = "voice"
    elif provider == "transcribe":
        needs_extra = not _transcribe_extra_importable()
        extra = "voice-aws"
    else:
        needs_extra = False
    # Suppressed where no pip channel into this interpreter exists — frozen build,
    # code-signed app bundle, pip-less or PEP 668 python. The Settings page shows
    # an unsupported notice there instead of a command that cannot succeed.
    if needs_extra and _pip_install_channel_available():
        command = pip_extra_install_command(extra)
        if command:
            cmds.append(command)
    if not stt_decoder.bundle_carries_decoder():
        cmds.extend(_ffmpeg_install_commands())
    return cmds

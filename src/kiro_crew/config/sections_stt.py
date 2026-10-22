"""The ``stt`` section: providers, models, Amazon Transcribe options, and their degradation rules.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
import re as _re
from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta, field_default
from kiro_crew.stt.limits import DEFAULT_IDLE_EVICT_SECS as _STT_DEFAULT_IDLE_EVICT_SECS
from kiro_crew.stt.limits import DEFAULT_PARTIAL_INTERVAL_MS as _STT_DEFAULT_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import DEFAULT_SILENCE_MS as _STT_DEFAULT_SILENCE_MS
from kiro_crew.stt.limits import MAX_THREADS as _STT_MAX_THREADS
from kiro_crew.stt.limits import THREADS_AUTO as _STT_THREADS_AUTO
from kiro_crew.stt.limits import parse_threads as _parse_stt_threads
from kiro_crew.stt.models import CATALOG as _STT_CATALOG
from kiro_crew.stt.models import CUSTOM_MODEL as _STT_CUSTOM_MODEL
from kiro_crew.stt.models import DEFAULT_MODEL as _STT_DEFAULT_MODEL
from kiro_crew.stt.models import resolve as _resolve_stt_model
from kiro_crew.stt.models import valid_custom_path as _valid_stt_custom_path
from kiro_crew.stt.models import valid_custom_sha256 as _valid_stt_custom_sha256

logger = logging.getLogger("kiro_crew.config.loader")


#: The default provider, and the one a RETIRED ``stt.provider`` degrades to. It is
#: the only recogniser with no precondition: recognition runs in this process on
#: every supported OS, with no account, no platform floor, and no separate install.
STT_PROVIDER_LOCAL = "local"

#: No recogniser at all. Selectable, so that "turn speech off" has a value a user
#: can write from the CLI, and the value an UNKNOWN ``stt.provider`` degrades to.
#: The distinction from ``enabled=False`` is only where it is set: both leave
#: every speech path answering "disabled", nothing is loaded and nothing bills.
#:
#: Degrading an unknown value onto ``local`` instead would put a typo, or a value
#: a bot guessed at, onto the one provider that links a native library into this
#: process: a user told to set ``stt.provider off`` while that library is
#: crashing on model load gets the crashing engine back, and learns it only from
#: a WARNING line (kirodotdev/KiroCrew#13179). A value the loader cannot honour
#: fails closed: whatever was meant, "run nothing" is the one reading that cannot
#: make things worse.
STT_PROVIDER_OFF = "off"

#: Local Whisper can detect the spoken language; forcing English corrupts
#: multilingual dictation before the recogniser can choose the right tokens.
STT_LANGUAGE_AUTO = "auto"
STT_LANGUAGE_FALLBACK = "en-US"

#: The recognisers a user can select. ``local`` runs whisper.cpp in-process,
#: ``apple`` uses macOS 26+ on-device recognition, and ``transcribe`` sends audio
#: to AWS Transcribe (billed, and gated on the AWS consent prompt). All three
#: produce partial results, so streaming is not a per-provider capability.
#: ``off`` selects no recogniser; it is deliberately absent from
#: ``stt_stream._STREAMING_PROVIDERS``, which grants by positive membership.
_VALID_STT_PROVIDERS = (STT_PROVIDER_LOCAL, "apple", "transcribe", STT_PROVIDER_OFF)


def valid_stt_providers() -> tuple[str, ...]:
    """Every ``stt.provider`` a stored config may select: built-ins plus the edition's.

    :data:`_VALID_STT_PROVIDERS` stays the built-in set. An edition's registered
    names (``PlatformContext.speech``) join it ahead of ``off``, so ``off`` stays
    last in every list a user reads. Boot refuses a registered name that collides
    with a built-in, so the union never repeats a name. With the default registry
    this IS :data:`_VALID_STT_PROVIDERS`.
    """
    # Imported here, not at module scope: ``config.sections`` mirrors this
    # module's globals, and a module alias is not part of its surface.
    from kiro_crew import speech_providers

    registered = speech_providers.registered_stt_names()
    if not registered:
        return _VALID_STT_PROVIDERS
    builtins = tuple(p for p in _VALID_STT_PROVIDERS if p != STT_PROVIDER_OFF)
    extra = tuple(p for p in registered if p not in _VALID_STT_PROVIDERS)
    return (*builtins, *extra, STT_PROVIDER_OFF)


def default_stt_provider() -> str:
    """What an absent ``stt.provider`` resolves to: the edition default, else ``local``.

    Only the ABSENT key (or a JSON ``null``) reaches this. A stored value always
    wins, and an unusable stored value keeps its own fallback, so an edition
    default is never selected because a value was mistyped.
    """
    from kiro_crew import speech_providers

    edition = speech_providers.edition_default_stt()
    if edition is not None and edition in valid_stt_providers():
        return edition
    return field_default(SttConfig, "provider")


#: Providers a stored config may still name. Each of these needed an out-of-band
#: install the user had to perform themselves (a whisper CLI on ``PATH``, or an
#: ``mlx``/``faster-whisper`` wheel), which is precisely the cost the resident
#: local engine removes, so a stored value degrades to ``local`` instead of
#: leaving voice input pointing at something that is not dispatchable.
_RETIRED_STT_PROVIDERS = ("whisper", "mlx", "parakeet", "faster")

#: Model names accepted for ``stt.model``, derived from the catalog that owns the
#: download and its sha256 pin rather than restated here. Restating it is how the
#: advertised menu comes to offer a model that cannot be fetched. ``custom`` is
#: appended because it names no catalog row: it selects the file and digest in
#: ``custom_model_path`` / ``custom_model_sha256`` instead.
_VALID_STT_MODELS = tuple(m.name for m in _STT_CATALOG) + (_STT_CUSTOM_MODEL,)

# Provider values already warned about in this process. The gateway loads config
# repeatedly, so an unusable stored provider is per-install information, not
# per-load: without this the retirement notice repeats several times a second for
# the whole session, which is how it was reported. Keyed on ``repr`` rather than
# the value itself because this arrives from ``config.json`` and may be
# unhashable (a list or dict), which must not raise on the degrade path. Exposed
# for tests to reset.
_WARNED_STT_PROVIDERS: set[str] = set()

# Unknown provider values seen before boot on a host where a companion may still
# register them, keyed like ``_WARNED_STT_PROVIDERS``. Boot re-checks each one.
_DEFERRED_STT_PROVIDERS: dict[str, object] = {}


def warn_deferred_stt_providers() -> None:
    """Re-check the provider values held back before boot, now the registry is known.

    Called by boot once the context is installed. A value the edition registered
    is now valid and says nothing; anything else gets the usual unknown-provider
    notice, once.
    """
    pending = list(_DEFERRED_STT_PROVIDERS.values())
    _DEFERRED_STT_PROVIDERS.clear()
    for value in pending:
        _validated_stt_provider(value)


def stt_provider_is_coerced(value: object) -> bool:
    """True when a stored ``stt.provider`` cannot take effect and is replaced.

    The single source of truth for "this stored value is inert", so the surface that
    offers to remove it (``kirocrew config defaults``) cannot come to disagree with
    the loader about which providers are dispatchable.
    """
    return value not in valid_stt_providers()


def stt_provider_resolution(value: object) -> str:
    """The provider a stored ``stt.provider`` of *value* runs as. Pure; never logs.

    Where an unusable value degrades TO depends on what is known about it. A
    retired name lands on ``local``: that user had local recognition and keeps
    it. Anything else lands on :data:`STT_PROVIDER_OFF`: a value nobody can
    account for must not select the provider that links a native library into
    the gateway. A JSON ``null`` names nothing, so it is the ABSENT key, not a
    wrong one: it takes the default the way a missing key does.

    Separate from :func:`_validated_stt_provider` so a surface that only needs
    the answer -- ``kirocrew config defaults --adopt`` deciding what to write --
    can ask without triggering, or having to suppress, the load-time notice.
    """
    if value in valid_stt_providers():
        return str(value)
    if value is None:
        return default_stt_provider()
    if value in _RETIRED_STT_PROVIDERS:
        return STT_PROVIDER_LOCAL
    return STT_PROVIDER_OFF


def _validated_stt_provider(value: object) -> str:
    """:func:`stt_provider_resolution`, with the load-time notice for a degrade.

    Degrades and logs; never raises. This value arrives from ``config.json``, so
    an unusable one must leave the load working the way
    :func:`_normalize_acp_backend` degrades an unusable persisted backend, rather
    than failing the load that read it.

    The notice names the command that removes the dead value. A load never writes,
    so without that pointer the line repeats on every invocation forever -- and
    unlike a superseded default there is nothing here to preserve, since the stored
    value cannot take effect either way. A ``null`` says nothing: it is the absent
    key, not a wrong one.
    """
    resolved = stt_provider_resolution(value)
    selectable = valid_stt_providers()
    if value in selectable or value is None:
        return resolved
    seen = repr(value)
    if seen in _WARNED_STT_PROVIDERS:
        return resolved
    from kiro_crew import speech_providers

    if value not in _RETIRED_STT_PROVIDERS and speech_providers.registry_pending():
        # Before boot on a companion host the edition may register this name, so
        # "unknown, drop it" could be false. Boot re-checks it
        # (``warn_deferred_stt_providers``) once the registry is installed.
        _DEFERRED_STT_PROVIDERS[seen] = value
        return resolved
    _WARNED_STT_PROVIDERS.add(seen)
    if value in _RETIRED_STT_PROVIDERS:
        logger.warning(
            "STT provider %r is retired; using %r instead. It needed a separate "
            "out-of-band install, which the bundled local engine removes while "
            "recognising the same speech. Run 'kirocrew config defaults --adopt "
            "stt.provider' to drop the stored value and this notice.",
            value,
            resolved,
        )
    else:
        logger.warning(
            "Unknown STT provider %r; using %r instead, so no recogniser runs until "
            "the value is fixed. Selectable providers: %s. Run "
            "'kirocrew config set stt.provider <provider>' to choose one, or "
            "'kirocrew config defaults --adopt stt.provider' to drop the stored value.",
            value,
            resolved,
            ", ".join(selectable),
        )
    return resolved


def _validated_stt_model(
    value: object,
    *,
    custom_path: object = "",
    custom_sha256: object = "",
) -> str:
    """Return the model name *value* selects, falling back to the default.

    Canonicalized here rather than passed through, so every consumer sees a name
    that names a real model: the name becomes a filename under the models
    directory, and an arbitrary string must not reach a path. ``resolve`` also maps
    the names older configuration used onto their current entries, so a stored
    ``turbo`` keeps the model it asked for instead of silently moving to the
    default.

    ``custom`` is the one name ``resolve`` is NOT asked about, and that is
    load-bearing rather than an optimisation: resolving it reads configuration for
    the path and digest, and this function runs *during* a configuration load. It is
    validated from the sibling values the caller passes instead, which is also the
    only place they are available before the dataclass exists. A half-configured
    custom model degrades to the default with a warning, exactly like an unknown
    name — a voice session must never be failed by the setting that selected it.
    """
    if not isinstance(value, str) or not value:
        model = field_default(SttConfig, "model")
        logger.warning("Non-string STT model %r; using %r", value, model)
        return model
    if value == _STT_CUSTOM_MODEL:
        if _valid_stt_custom_path(custom_path) and _valid_stt_custom_sha256(custom_sha256):
            return _STT_CUSTOM_MODEL
        logger.warning(
            "stt.model is %r but stt.custom_model_path (a file name) and "
            "stt.custom_model_sha256 (64 hex characters) are not both set; using %r",
            _STT_CUSTOM_MODEL,
            field_default(SttConfig, "model"),
        )
        return field_default(SttConfig, "model")
    return _resolve_stt_model(value).name


def _validated_stt_threads(value: object) -> int:
    """``stt.threads`` as stored: ``0`` for auto, or a count up to ``MAX_THREADS``.

    ``"auto"`` and a digit string are accepted spellings and come back as the int,
    so the field stays an integer on the next save. Anything else (a negative
    number, a count above ``MAX_THREADS``, a float, a word) is logged and replaced
    by auto. Auto is the safe fallback because it is what the setting did before it
    existed. Used by both the schema validator and the section builder, so the
    resolved value is the same with or without ``jsonschema``.
    """
    parsed = _parse_stt_threads(value)
    if parsed is None:
        logger.warning(
            "Unusable stt.threads %r (expected 'auto', 0, or 1-%d); using auto",
            value,
            _STT_MAX_THREADS,
        )
        return _STT_THREADS_AUTO
    return parsed


#: Amazon Transcribe's own limit on a custom vocabulary name
#: (``StartStreamTranscription``'s ``VocabularyName``). The name travels as a
#: request header on every stream, so a value outside this shape can only ever
#: fail the request it rides on.
TRANSCRIBE_VOCABULARY_NAME_MAX = 200
_TRANSCRIBE_VOCABULARY_NOTICE_VALUE_MAX = 120
_TRANSCRIBE_VOCABULARY_NAME_RE = _re.compile(r"[0-9A-Za-z._-]+")
_LAST_WARNED_TRANSCRIBE_VOCABULARY: str | None = None


def transcribe_vocabulary_name(value: object) -> str | None:
    """The custom vocabulary *value* names: ``""`` for none, None when unusable.

    Only surrounding whitespace is dropped. AWS compares vocabulary names
    case-sensitively, so folding case would select a different vocabulary, or
    none at all.

    The one rule both writers apply: ``PUT /api/config/stt`` stores only a value
    this accepts, and the loader keeps only what this accepts, so the name a user
    reads back is the name every Transcribe request carries.
    """
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name:
        return ""
    if len(name) > TRANSCRIBE_VOCABULARY_NAME_MAX:
        return None
    return name if _TRANSCRIBE_VOCABULARY_NAME_RE.fullmatch(name) else None


def _validated_transcribe_vocabulary(value: object) -> str:
    """:func:`transcribe_vocabulary_name` for a stored value: degrades to none, logs once.

    Degrades rather than failing the load, like the provider and model above. A
    ``null`` is the absent key and says nothing. An unusable name is dropped rather
    than sent, because Amazon Transcribe would refuse every stream that carried it:
    dictation keeps working without the vocabulary, and the notice says why.
    """
    global _LAST_WARNED_TRANSCRIBE_VOCABULARY

    if value is None:
        return field_default(SttConfig, "transcribe_vocabulary")
    name = transcribe_vocabulary_name(value)
    if name is not None:
        return name
    seen = repr(value)[:_TRANSCRIBE_VOCABULARY_NOTICE_VALUE_MAX]
    if seen != _LAST_WARNED_TRANSCRIBE_VOCABULARY:
        _LAST_WARNED_TRANSCRIBE_VOCABULARY = seen
        logger.warning(
            "Unusable stt.transcribe_vocabulary %s; dictation runs without a custom "
            "vocabulary. Amazon Transcribe names are 1-%d letters, digits, '.', '_' or "
            "'-'. Choose one in Settings -> Voice, or fix the value in config.json.",
            seen,
            TRANSCRIBE_VOCABULARY_NAME_MAX,
        )
    return ""


#: Amazon Transcribe streaming language identification needs at least two candidate
#: languages; AWS recommends no more than five, and that is the most a user can pick.
TRANSCRIBE_LANGUAGES_MIN = 2
TRANSCRIBE_LANGUAGES_MAX = 5
#: The shape of every Transcribe streaming locale (``en-US``, ``vi-VN``, ``ckb-IQ``).
#: Each candidate rides in one comma-joined request header, so a value outside this
#: shape could only ever fail the stream.
_TRANSCRIBE_LANGUAGE_RE = _re.compile(r"[a-z]{2,3}-[A-Z]{2}")
_LAST_WARNED_TRANSCRIBE_LANGUAGES: str | None = None


def transcribe_language_options(value: object) -> list[str] | None:
    """The candidate languages *value* names: ``[]`` for none, None when unusable.

    Usable is a list of at most :data:`TRANSCRIBE_LANGUAGES_MAX` locale codes with
    no language twice: Amazon Transcribe allows one dialect per language in a
    stream, so ``en-US`` beside ``en-GB`` fails it. Surrounding whitespace is
    dropped and the order is kept. A list holding a single language is usable: it
    is a selection in progress, and :attr:`SttConfig.transcribe_language_identification`
    keeps the one-language path in force until a second language joins it.

    The one rule both writers apply, like :func:`transcribe_vocabulary_name`:
    ``PUT /api/config/stt`` stores only a value this accepts, and the loader keeps
    only what this accepts.
    """
    if not isinstance(value, list) or len(value) > TRANSCRIBE_LANGUAGES_MAX:
        return None
    codes: list[str] = []
    languages: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None
        code = item.strip()
        if not _TRANSCRIBE_LANGUAGE_RE.fullmatch(code):
            return None
        language = code.split("-", 1)[0]
        if language in languages:
            return None
        languages.add(language)
        codes.append(code)
    return codes


def _validated_transcribe_languages(value: object) -> list[str]:
    """:func:`transcribe_language_options` for a stored value: degrades to none, logs once.

    Degrades rather than failing the load, like the vocabulary above. A ``null`` is
    the absent key and says nothing. An unusable list is dropped as a whole rather
    than trimmed, because which language to drop is the user's choice: dictation
    keeps the single language in ``language_code``, and the notice says why.
    """
    global _LAST_WARNED_TRANSCRIBE_LANGUAGES

    if value is None:
        return []
    codes = transcribe_language_options(value)
    if codes is not None:
        return codes
    seen = repr(value)[:_TRANSCRIBE_VOCABULARY_NOTICE_VALUE_MAX]
    if seen != _LAST_WARNED_TRANSCRIBE_LANGUAGES:
        _LAST_WARNED_TRANSCRIBE_LANGUAGES = seen
        logger.warning(
            "Unusable stt.transcribe_languages %s; dictation uses stt.language_code "
            "alone. Give at most %d locale codes such as vi-VN, one dialect per "
            "language. Choose them in Settings -> Voice, or fix the value in config.json.",
            seen,
            TRANSCRIBE_LANGUAGES_MAX,
        )
    return []


def _validated_stt_custom_path(value: object) -> str:
    """Return a usable-shaped ``stt.custom_model_path``, else ``""``.

    Shape only, because this runs during a configuration load and must not touch
    the disk. Whether the file may be opened -- inside the models directory, present,
    a regular file -- is decided by :func:`kiro_crew.stt.models.custom_model_file`
    when the dashboard saves the field and again on every load. The rejected value is
    described, never quoted.
    """
    path = _valid_stt_custom_path(value)
    if not path and value:
        shape = f"str of length {len(value)}" if isinstance(value, str) else type(value).__name__
        logger.warning("Ignoring stt.custom_model_path (%s): it must be a file name", shape)
    return path


def _validated_stt_custom_sha256(value: object) -> str:
    """Return a usable ``stt.custom_model_sha256``, else ``""``.

    The digest is the entire trust anchor for a custom model, so a malformed one is
    dropped rather than stored: keeping it would make ``stt.model = "custom"`` look
    configured while every load it started was guaranteed to fail verification.

    The rejected value is described, never quoted. A `%r` here published whatever was
    in the field into the log ring, and `/api/logs` carries no owner gate, so a
    credential mis-pasted into this field — an ordinary operator error on a
    hand-edited ``config.json``, which is the one path that reaches this branch —
    was readable by a non-owner. Type and length are what a reader needs to fix
    the setting, and neither can carry a secret.
    """
    digest = _valid_stt_custom_sha256(value)
    if not digest and value:
        shape = f"str of length {len(value)}" if isinstance(value, str) else type(value).__name__
        logger.warning(
            "Ignoring stt.custom_model_sha256 (%s): it must be 64 hexadecimal characters",
            shape,
        )
    return digest


@dataclass
class SttConfig:
    """Speech-to-text configuration.

    Enabled by default, with the `local` provider as the default. Actually
    recognising speech requires the optional `voice` extra: desktop builds
    bundle it, while CLI and source installs must install it themselves (it is
    not pulled in by a plain install). Settings -> Voice and the microphone
    modal surface the exact command when the extra is missing.
    """

    enabled: bool = field(
        default=True,
        metadata=_meta("Enabled", "Turn spoken input into text you can send."),
    )
    provider: str = field(
        default=STT_PROVIDER_LOCAL,
        metadata=_meta(
            "Provider",
            "Where speech is recognised. `local` runs on this machine and needs no "
            "account (it downloads one model the first time you dictate), `apple` "
            "uses the on-device recogniser built into macOS 26 and later, and "
            "`transcribe` sends your audio to AWS Transcribe, which bills your AWS "
            "account. `off` runs no recogniser at all, the same as turning speech "
            "input off.",
            enum=list(_VALID_STT_PROVIDERS),
        ),
    )
    model: str = field(
        default=_STT_DEFAULT_MODEL,
        metadata=_meta(
            "Model",
            "Which speech model the local provider downloads and runs. Bigger is "
            "more accurate and a longer first-time download: `tiny` on a machine "
            "short of memory, `base` for everyone, `small` when accents or jargon "
            "are being misheard, `large-v3-turbo` for the best accuracy available "
            "-- though on a CPU-only build it can recognise slower than you speak: "
            "an 11-second clip took 13.6 s on a 16-thread aarch64 CPU, 1.24x the "
            "audio. The Voice panel says so beside the choice when the build it "
            "measured has no acceleration. `custom` runs your own whisper.cpp model "
            "file named by `custom_model_path` instead, checked against "
            "`custom_model_sha256`.",
            enum=list(_VALID_STT_MODELS),
        ),
    )
    custom_model_path: str = field(
        default="",
        metadata=_meta(
            "Custom Model File",
            "The file name of a whisper.cpp ggml model (`.bin`) to run INSTEAD of a "
            "catalog model, used when `model` is `custom`. Put the file in the "
            "speech models folder (`models/whisper` under the Kiro Crew home) "
            "first: the path must resolve to a regular file inside that folder, so "
            "`..`, an absolute path elsewhere, and a symlink pointing out of it are "
            "refused. Nothing is downloaded. Both this and the digest must be set; "
            "otherwise `custom` degrades to the default model with a warning.",
            # Owner-only: `GET /api/config/stt` serves this to the owner alone, and
            # the un-owner-gated `GET /api/config/kirocrew` must not undo that.
            sensitive=True,
        ),
    )
    custom_model_sha256: str = field(
        default="",
        metadata=_meta(
            "Custom Model SHA-256",
            "The 64-character hex sha256 of the file at `custom_model_path`. The "
            "file is hashed on every load and not used unless it matches, so a "
            "file swapped in the models folder is refused. A `sha256:` value or a "
            "`sha256sum` line is accepted too. Get it with `shasum -a 256 <file>` "
            "(or `Get-FileHash <file> -Algorithm SHA256` on Windows).",
            # Owner-only, like the file path: `GET /api/config/stt` serves the pair
            # to the owner alone, so the general config read must mask it too.
            sensitive=True,
        ),
    )
    language_code: str = field(
        default=STT_LANGUAGE_AUTO,
        metadata=_meta(
            "Language Code",
            "Language for speech recognition (e.g. zh-CN, en-US). The local provider "
            "defaults to auto-detect; choosing a language can improve short dictation.",
        ),
    )
    polish: bool = field(
        default=False,
        metadata=_meta(
            "AI Cleanup",
            "After dictation finishes, have your configured model fix punctuation "
            "and capitalisation. Your WORDS are never changed: a "
            "reply that altered one is discarded, so the worst case is that nothing "
            "happens. Off by default because it sends the TRANSCRIPT (never the "
            "audio) to that model, so a local-only setup stays local-only until you "
            "turn this on.",
        ),
    )
    streaming: bool = field(
        default=True,
        metadata=_meta(
            "Streaming",
            "Show words in the message box while you are still speaking rather than "
            "only once you stop. Every provider supports it; turning it off spends "
            "less CPU on the local provider and fewer API calls on `transcribe`.",
        ),
    )
    silence_ms: int = field(
        default=_STT_DEFAULT_SILENCE_MS,
        metadata=_meta(
            "End-of-phrase silence",
            "How long a pause has to last, in milliseconds, before what you said is "
            "treated as a finished phrase. Raise it if you are being cut off "
            "mid-sentence; lower it if the text lags behind you.",
        ),
    )
    partial_interval_ms: int = field(
        default=_STT_DEFAULT_PARTIAL_INTERVAL_MS,
        metadata=_meta(
            "Live update interval",
            "How often the live transcript is refreshed while you speak, in "
            "milliseconds. Lower feels more immediate and costs a little more CPU "
            "per second of speech; higher is steadier to read.",
        ),
    )
    idle_evict_secs: int = field(
        default=_STT_DEFAULT_IDLE_EVICT_SECS,
        metadata=_meta(
            "Release model after",
            "How long the local model stays loaded in memory after your last "
            "recording, in seconds. It holds roughly 150 MB at the default model, "
            "and reloading it takes a fraction of a second, so lower this on a "
            "machine short of memory. 0 releases it as soon as you stop speaking.",
        ),
    )
    threads: int = field(
        default=_STT_THREADS_AUTO,
        metadata=_meta(
            "Decode threads",
            "How many CPU threads the local model uses to recognise speech. 0 (or "
            "`auto`) uses half this machine's cores, at most 16, which stays steady "
            "on a busy host. A larger machine can set more: on a 128-core host the "
            "largest model took 4 s on an 11-second clip at 16 threads and 1.25 s at "
            "64. A number above the cores this process may use is lowered to that "
            "count. The KIROCREW_STT_THREADS environment variable overrides this. A "
            "change applies at the next dictation, which reloads the model once.",
        ),
    )
    endpointing: bool = field(
        default=False,
        metadata=_meta(
            "Semantic endpointing",
            "While dictating, run a fast background model on each finished phrase to "
            "detect when you have asked a complete question, then send it without "
            "you pressing anything. Needs streaming; off by default.",
        ),
    )
    submit_on_release: bool = field(
        default=False,
        metadata=_meta(
            "Send when I stop recording",
            "Send the message once the transcript is ready when you deliberately "
            "stop dictating: letting go of hold-to-talk, releasing the push-to-talk "
            "key, or pressing the mic or the key again. Shift-click the mic, or slide "
            "sideways on the phone's hold bar, to keep a draft instead; with a "
            "push-to-talk binding that does not use Shift, Shift as you release works "
            "too. "
            "Cancelling, losing focus and the recording time limit never send. Works "
            "with streaming and batch transcription; off by default.",
        ),
    )
    dictation_panel: bool = field(
        default=True,
        metadata=_meta(
            "Dictation Panel",
            "Show the animated dictation panel while recording instead of the thin status bar. "
            "Ignored when the browser lacks WebGL2 or the OS requests reduced motion — both "
            "fall back to the status bar.",
        ),
    )
    timeout_secs: int = field(
        default=300,
        metadata=_meta(
            "Timeout",
            "Maximum transcription time in seconds. Local streaming uses the same budget "
            "for pending audio after recording stops.",
        ),
    )
    transcribe_region: str = field(
        default="us-east-1",
        metadata=_meta("Transcribe Region", "AWS region for Transcribe API."),
    )
    transcribe_profile: str = field(
        default="",
        metadata=_meta("Transcribe Profile", "AWS profile for Transcribe API."),
    )
    transcribe_vocabulary: str = field(
        default="",
        metadata=_meta(
            "Transcribe Vocabulary",
            "Name of a custom vocabulary you created in Amazon Transcribe, in the same "
            "region, so names and terms it would mishear are recognised (`transcribe` "
            "provider only). Its language must match the dictation language, or "
            "Amazon Transcribe refuses it and dictation fails. Empty uses none.",
        ),
    )
    transcribe_languages: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Transcribe Languages",
            "Languages to detect when speakers switch between them in one recording, "
            "such as vi-VN and en-US (`transcribe` provider, streaming only). With two "
            "or more, Amazon Transcribe identifies the language of each part and "
            "transcribes it in that language; with fewer, Language Code is used. At "
            "most five, and one dialect per language: en-US and en-GB cannot be "
            "picked together.",
        ),
    )

    def __post_init__(self) -> None:
        language = self.language_code
        if not isinstance(language, str) or not language.strip():
            language = field_default(SttConfig, "language_code")
        else:
            language = language.strip()
        if language.lower() == STT_LANGUAGE_AUTO:
            language = STT_LANGUAGE_AUTO
        self.language_code = language

    @property
    def effective_language_code(self) -> str:
        """Resolve a provider locale without changing the stored preference."""
        if self.language_code == STT_LANGUAGE_AUTO and self.provider != STT_PROVIDER_LOCAL:
            return STT_LANGUAGE_FALLBACK
        return self.language_code

    @property
    def transcribe_language_identification(self) -> list[str]:
        """The candidate languages a live Transcribe stream identifies, or ``[]``.

        Non-empty only from :data:`TRANSCRIBE_LANGUAGES_MIN` candidates up, because
        Amazon Transcribe refuses identification with fewer. Empty means the stream
        is transcribed in :attr:`effective_language_code`.
        """
        if len(self.transcribe_languages) < TRANSCRIBE_LANGUAGES_MIN:
            return []
        return list(self.transcribe_languages)

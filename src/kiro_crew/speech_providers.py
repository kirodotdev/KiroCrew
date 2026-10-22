"""Read the edition's speech providers from the composed platform context.

The one place core reads ``PlatformContext.speech``. The STT config loader and
the TTS provider resolver ask here which names an edition registered and which
default it chose, and union that with their own built-in sets. With the default
registry every answer is empty or ``None``, so a standalone install validates
and defaults exactly as it did before the seam existed.

Read through ``installed_context()``, not ``current_context()``. The STT half
runs inside ``KiroCrewConfig.load()``, and ``current_context()`` with nothing
installed loads config to compose its lazy default, which would re-enter this
same load. The no-context answer is "no registered providers", which is the
standalone answer and the conservative one: an unknown stored name keeps
today's fallback (STT fails closed to ``off``, TTS falls back to the built-in
local default). The TTS half reads the same way so both halves answer the same
question about one process.

Boot validates the registry once (:func:`validate_speech_registry`), so the
readers below never see a colliding or duplicate name from a booted context.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional, Tuple

from kiro_crew.platform.context import PlatformCompositionError, installed_context

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import SpeechProviderRegistry

logger = logging.getLogger(__name__)


def _installed_speech() -> "Optional[SpeechProviderRegistry]":
    """The installed context's speech registry, or ``None`` when none is installed."""
    ctx = installed_context()
    if ctx is None:
        return None
    return ctx.speech


def _names(providers: Any) -> Tuple[str, ...]:
    """The ``name`` of each provider, keeping only non-empty strings."""
    out: list[str] = []
    for provider in providers or ():
        name = getattr(provider, "name", None)
        if isinstance(name, str) and name and name not in out:
            out.append(name)
    return tuple(out)


def _read(fn_name: str, fallback: Any) -> Any:
    """Call ``speech.<fn_name>()``, degrading to *fallback* on an adapter error.

    A booted registry has already answered every one of these calls once, at
    boot, so an error here is transient; degrading to the built-in answer keeps
    voice working on the built-ins rather than breaking the config load.
    """
    speech = _installed_speech()
    if speech is None:
        return fallback
    try:
        return getattr(speech, fn_name)()
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("speech registry %s() failed; using built-ins", fn_name, exc_info=True)
        return fallback


def registered_stt_names() -> Tuple[str, ...]:
    """Names of the edition's registered STT providers, in registration order."""
    return _names(_read("stt_providers", ()))


def registered_tts_names() -> Tuple[str, ...]:
    """Names of the edition's registered TTS providers, in registration order."""
    return _names(_read("tts_providers", ()))


def _default(fn_name: str) -> Optional[str]:
    value = _read(fn_name, None)
    return value if isinstance(value, str) and value else None


def edition_default_stt() -> Optional[str]:
    """The ``stt.provider`` an absent key resolves to, or ``None`` for core's."""
    return _default("default_stt")


def edition_default_tts() -> Optional[str]:
    """The ``voice_reply.provider`` an absent key resolves to, or ``None`` for core's."""
    return _default("default_tts")


def _refuse(message: str) -> PlatformCompositionError:
    return PlatformCompositionError(f"speech provider registry: {message}")


#: A registered provider name: a lowercase ASCII slug. ``fullmatch`` with ``\Z``
#: semantics, so no trailing newline, space, upper case or invisible character
#: (a zero-width joiner, say) can make a name look like a reserved one.
_PROVIDER_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def _checked_names(
    providers: Any, kind: str, reserved: "frozenset[str]", reserved_why: str
) -> Tuple[str, ...]:
    reserved_folded = frozenset(r.casefold() for r in reserved)
    names: list[str] = []
    for provider in providers:
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or not _PROVIDER_NAME_RE.fullmatch(name):
            raise _refuse(
                f"{kind} provider {provider!r} has no usable name ({name!r}); a name "
                "is a lowercase slug of letters, digits, '-' or '_'"
            )
        if name.casefold() in reserved_folded:
            raise _refuse(
                f"{kind} provider name {name!r} {reserved_why}; an edition may add "
                "providers but never replace one"
            )
        if name in names:
            raise _refuse(f"{kind} provider name {name!r} is registered twice")
        names.append(name)
    return tuple(names)


def validate_speech_registry(registry: "SpeechProviderRegistry") -> None:
    """Refuse a registry that would shadow a built-in, at boot, fail-closed.

    Raises ``PlatformCompositionError`` when a registered STT name is a built-in,
    a retired name or ``off``; when a registered TTS name is a built-in; when a
    name is empty or registered twice; when a default names no known provider;
    or when the registry cannot be read at all. The Default registry passes.
    """
    # Imported here: this module is imported by the config loader, and both the
    # STT section and ``voice_reply`` sit above it.
    from kiro_crew.config.sections_stt import _RETIRED_STT_PROVIDERS, _VALID_STT_PROVIDERS
    from kiro_crew.voice_reply import VALID_PROVIDERS

    try:
        stt = tuple(registry.stt_providers() or ())
        tts = tuple(registry.tts_providers() or ())
        default_stt = registry.default_stt()
        default_tts = registry.default_tts()
    except PlatformCompositionError:
        raise
    except Exception as exc:
        raise _refuse(f"could not be read ({type(exc).__name__}: {exc})") from exc

    stt_names = _checked_names(
        stt,
        "STT",
        frozenset(_VALID_STT_PROVIDERS) | frozenset(_RETIRED_STT_PROVIDERS),
        "is a built-in, retired or 'off' STT provider",
    )
    tts_names = _checked_names(tts, "TTS", frozenset(VALID_PROVIDERS), "is a built-in TTS provider")

    if default_stt is not None and default_stt not in (*_VALID_STT_PROVIDERS, *stt_names):
        raise _refuse(f"default_stt() names {default_stt!r}, which is not an STT provider")
    if default_tts is not None and default_tts not in (*VALID_PROVIDERS, *tts_names):
        raise _refuse(f"default_tts() names {default_tts!r}, which is not a TTS provider")


def registry_pending() -> bool:
    """Whether an edition registry may still arrive: no context yet, a companion installed.

    True only before boot on a host with a companion entry point. There a name
    the built-ins do not know may be one the edition is about to register, so a
    caller defers calling it unknown until boot. A standalone host has no entry
    point, so this is False and every answer is immediate, as before the seam.
    """
    if _installed_speech() is not None:
        return False
    try:
        # Imported here: discovery sits above the config loader that calls this.
        from kiro_crew.platform.discovery import plugin_entry_points

        return bool(plugin_entry_points())
    except Exception:
        logger.debug("companion entry-point scan failed; not deferring", exc_info=True)
        return False


def edition_changes_stt() -> bool:
    """Whether the installed registry adds an STT provider or moves the STT default.

    False for the Default registry, so a standalone boot does nothing extra.
    """
    return bool(registered_stt_names()) or edition_default_stt() is not None

"""Local speech-to-text on a resident whisper.cpp recogniser.

The package is layered so each piece can be tested without the one above it:

- :mod:`kiro_crew.stt.limits`: the numbers configuration can set, and nothing else.
- :mod:`kiro_crew.stt.models`: the sha256-pinned model catalog and its downloader.
- :mod:`kiro_crew.stt.hallucinations`: suppression of the recogniser's own artefacts.
- :mod:`kiro_crew.stt.vad`: voice-activity detection and utterance endpointing.
- :mod:`kiro_crew.stt.engine`: the resident model, and the lock discipline around it.
- :mod:`kiro_crew.stt.session`: a live session turning PCM into partials and a final.

The public surface resolves lazily through :pep:`562` ``__getattr__``, for the
same reason :mod:`kiro_crew.config` does it. ``vad``, ``session`` and ``engine``
import numpy, and an eager re-export here would put an array library on the import
path of everything that touches any part of this package, including
``config.loader`` reading a field default and therefore ``kiro_crew.cli`` printing
``--help``. ``test_cli_lazy_imports`` guards exactly that. ``from kiro_crew.stt
import X`` keeps working for every name in ``__all__``; it just resolves on first
access instead of at package import.

**A caller and anything that substitutes what it calls must name the SAME
module.** ``__getattr__`` resolves a name fresh on every access, but only while
the package does not itself hold that name, and a ``monkeypatch.setattr(stt,
"x", ...)`` leaves the original behind as a real attribute on teardown. From then
on that attribute shadows ``__getattr__``, and a patch applied to
``stt.models.x`` is invisible to anyone reading ``stt.x``. The failure is silent
and order-dependent: it needs a full-suite run and some unrelated file to have
touched the name first.

Either side is fine as long as they agree. ``transcribe.py`` reads
``stt.transcribe_pcm`` and its tests patch the package, which works. A caller
whose substitute lives on the submodule reads the submodule
(``from kiro_crew.stt import models as stt_models``). Mixing the two is the bug,
and it cost this package one: ``GET /api/stt/status`` read ``stt.is_present``
while its test patched ``stt.models.is_present``, and reported every model absent
on a host where the files were there.

Constants and classes are safe either way, because nothing substitutes them.

Nothing here imports the recogniser binding itself. It is an optional extra, so a
gateway installed without it starts normally and :func:`availability` reports why
voice input is unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

#: Which submodule owns each public name, and the table :func:`__getattr__`
#: resolves through, so a name absent here is genuinely not part of the surface.
_EXPORTS: dict[str, str] = {
    "is_present": "models",
    "models_dir": "models",
    "Availability": "engine",
    "CODE_DECODE_FAILED": "engine",
    "CODE_EXTRA_MISSING": "engine",
    "CODE_IMPORT_FAILED": "engine",
    "CODE_MODEL_MISSING": "engine",
    "CODE_NO_WHEEL": "engine",
    "CODE_OK": "engine",
    "DecodeFailed": "engine",
    "pcm_from_int16": "engine",
    "SAMPLE_RATE_HZ": "vad",
    "DECODE_FAILED_ADVISORY": "session",
    "KIND_ERROR": "session",
    "KIND_FINAL": "session",
    "KIND_PARTIAL": "session",
    "KIND_STATUS": "session",
    "LocalSession": "session",
    "STAGE_DOWNLOADING": "session",
    "STAGE_READY": "session",
    "SttEvent": "session",
    "close": "session",
    "ensure_model": "session",
    "prewarm": "session",
    "transcribe_pcm": "session",
}

__all__ = [
    "Availability",
    "CODE_DECODE_FAILED",
    "CODE_EXTRA_MISSING",
    "CODE_IMPORT_FAILED",
    "CODE_MODEL_MISSING",
    "CODE_NO_WHEEL",
    "CODE_OK",
    "DECODE_FAILED_ADVISORY",
    "DecodeFailed",
    "KIND_ERROR",
    "KIND_FINAL",
    "KIND_PARTIAL",
    "KIND_STATUS",
    "LocalSession",
    "SAMPLE_RATE_HZ",
    "STAGE_DOWNLOADING",
    "STAGE_READY",
    "SttEvent",
    "availability",
    "close",
    "ensure_model",
    "is_present",
    "model_store",
    "models_dir",
    "pcm_from_int16",
    "prewarm",
    "resolve_model",
    "transcribe_pcm",
]

# For type checkers only; no runtime import of the heavy modules. Most of these
# are the re-exports below; ModelStore and WhisperModel are here for the
# annotations on this module's own helpers rather than to be re-exported.
if TYPE_CHECKING:
    from kiro_crew.stt.engine import (
        CODE_DECODE_FAILED,
        CODE_EXTRA_MISSING,
        CODE_IMPORT_FAILED,
        CODE_MODEL_MISSING,
        CODE_NO_WHEEL,
        CODE_OK,
        Availability,
        DecodeFailed,
        pcm_from_int16,
    )
    from kiro_crew.stt.models import (
        ModelStore,
        WhisperModel,
        is_present,
        models_dir,
    )
    from kiro_crew.stt.session import (
        DECODE_FAILED_ADVISORY,
        KIND_ERROR,
        KIND_FINAL,
        KIND_PARTIAL,
        KIND_STATUS,
        STAGE_DOWNLOADING,
        STAGE_READY,
        LocalSession,
        SttEvent,
        close,
        ensure_model,
        prewarm,
        transcribe_pcm,
    )
    from kiro_crew.stt.vad import SAMPLE_RATE_HZ


def availability() -> Availability:
    """Whether local recognition can run on this host, and if not, why.

    A thin alias for :func:`kiro_crew.stt.engine.probe` so callers outside the
    package do not have to know which module owns the check. It reports on the
    recogniser only; whether the configured model is on disk is answered by
    :func:`kiro_crew.stt.models.is_present`, because a missing model is a
    condition that resolves itself on first use.
    """
    from kiro_crew.stt.engine import probe as _probe

    return _probe()


def resolve_model(name: str) -> WhisperModel:
    """Return the catalog entry for *name*, degrading to the default if unknown."""
    from kiro_crew.stt.models import resolve as _resolve

    return _resolve(name)


def model_store() -> ModelStore:
    """The process-wide model store, which owns download state and progress."""
    from kiro_crew.stt.models import store as _store

    return _store()


def __getattr__(name: str) -> object:
    """Resolve a public name from its owning submodule (PEP 562)."""
    module = _EXPORTS.get(name)
    if module is not None:
        # Imported here, not at module scope: a top-level import would defeat this
        # seam entirely and put numpy back on the CLI's import path.
        from importlib import import_module

        return getattr(import_module(f"{__name__}.{module}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)

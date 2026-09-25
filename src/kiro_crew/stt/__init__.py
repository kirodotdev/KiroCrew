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

**A re-exported name lives in exactly one place: the submodule that defines it.**
Reading ``stt.transcribe_pcm`` reads ``stt.session.transcribe_pcm``, and writing
``stt.transcribe_pcm`` writes ``stt.session.transcribe_pcm``, so the two spellings
of a name always hold the same value. A caller and anything that substitutes what
it calls are therefore free to name either one: ``transcribe.py`` reads
``stt.transcribe_pcm`` while a test patches the package, a caller whose substitute
lives on the submodule reads the submodule
(``from kiro_crew.stt import models as stt_models``), and mixing the two spellings
resolves to the same object.

Nothing here imports the recogniser binding itself. It is an optional extra, so a
gateway installed without it starts normally and :func:`availability` reports why
voice input is unavailable.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

#: Which submodule owns each public name, and the table :func:`__getattr__`
#: resolves through, so a name absent here is genuinely not part of the surface.
_EXPORTS: dict[str, str] = {
    "is_present": "models",
    "models_dir": "models",
    "Availability": "engine",
    "CODE_DECODE_FAILED": "engine",
    "CODE_EXTRA_MISSING": "engine",
    "CODE_IMPORT_FAILED": "engine",
    "CODE_LOAD_CRASHED": "engine",
    "CODE_MODEL_MISSING": "engine",
    "CODE_NATIVE_PROBE_CRASHED": "engine",
    "CODE_NO_WHEEL": "engine",
    "CODE_OK": "engine",
    "CODE_UNSUPPORTED_CPU": "engine",
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
    "STAGE_PREPARING": "session",
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
    "CODE_LOAD_CRASHED",
    "CODE_MODEL_MISSING",
    "CODE_NATIVE_PROBE_CRASHED",
    "CODE_NO_WHEEL",
    "CODE_OK",
    "CODE_UNSUPPORTED_CPU",
    "DECODE_FAILED_ADVISORY",
    "DecodeFailed",
    "KIND_ERROR",
    "KIND_FINAL",
    "KIND_PARTIAL",
    "KIND_STATUS",
    "LocalSession",
    "SAMPLE_RATE_HZ",
    "STAGE_DOWNLOADING",
    "STAGE_PREPARING",
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
        CODE_LOAD_CRASHED,
        CODE_MODEL_MISSING,
        CODE_NATIVE_PROBE_CRASHED,
        CODE_NO_WHEEL,
        CODE_OK,
        CODE_UNSUPPORTED_CPU,
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
        STAGE_PREPARING,
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


#: Package attribute -> ``(owning module, symbol on that module)``. Each owning
#: submodule is imported on first access, not here: a top-level import would defeat
#: this seam entirely and put numpy back on the CLI's import path.
_OWNED: dict[str, tuple[str, str]] = {
    name: (f"{__name__}.{module}", name) for name, module in _EXPORTS.items()
}

_OWNERS: dict[str, ModuleType] = {}


def _owner(name: str) -> ModuleType:
    """Return the module that defines ``name``, importing it on first use."""
    module_name = _OWNED[name][0]
    owner = _OWNERS.get(module_name)
    if owner is None:
        owner = _OWNERS[module_name] = importlib.import_module(module_name)
    return owner


def __getattr__(name: str) -> Any:
    """Read a re-exported name from the module that owns it (:pep:`562`)."""
    if name not in _OWNED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner(name), _OWNED[name][1])


class _ReExportModule(ModuleType):
    """Send a write to a re-exported name to the module that owns it.

    Binding the name in this package's own namespace instead would shadow the
    owner permanently, because ``__getattr__`` runs only for a name the package
    does not already hold: the shadow would win every later read, and the owner's
    value would become unreachable through this package. Forwarding the write
    leaves one value for a test harness to remember and one to put back, which is
    what lets a caller and its test name either spelling.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _OWNED:
            setattr(_owner(name), _OWNED[name][1], value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _OWNED:
            delattr(_owner(name), _OWNED[name][1])
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _ReExportModule


def __dir__() -> list[str]:
    return list(__all__)

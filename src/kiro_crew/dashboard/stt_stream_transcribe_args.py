"""Argument shaping and start-refusal reading for a live Amazon Transcribe stream.

Split out of :mod:`kiro_crew.dashboard.stt_stream`, which imports these functions
and remains the path callers use.
"""

from __future__ import annotations

import inspect
from typing import Any

from kiro_crew.config.sections import SttConfig

try:
    from amazon_transcribe.exceptions import BadRequestException as _TranscribeBadRequest
except ImportError:  # pragma: no cover -- the streaming extra is optional
    _TranscribeBadRequest = None  # type: ignore[assignment,misc]

#: The ``start_stream_transcription`` keyword mixed-language detection needs. SDK
#: 0.6.3 and later accept it; an older install raises ``TypeError`` on it.
_IDENTIFY_MULTIPLE_LANGUAGES_KWARG = "identify_multiple_languages"


def _client_identifies_languages(client: Any) -> bool:
    """Whether *client*'s ``start_stream_transcription`` accepts mixed-language options.

    Read from the method's own signature, so an outdated SDK is reported as the
    package to upgrade instead of failing the stream with a ``TypeError`` that reads
    as a service fault. A method taking ``**kwargs`` accepts it too.
    """
    try:
        parameters = inspect.signature(client.start_stream_transcription).parameters
    except (TypeError, ValueError):
        return False
    return _IDENTIFY_MULTIPLE_LANGUAGES_KWARG in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )


def _transcribe_language_kwargs(stt_cfg: SttConfig) -> dict[str, Any]:
    """The language and vocabulary arguments of a live Transcribe stream.

    One language: ``language_code`` plus ``vocabulary_name``. Two or more
    candidates (:attr:`SttConfig.transcribe_language_identification`): Amazon Transcribe
    refuses a stream that names a ``LanguageCode`` beside
    ``IdentifyMultipleLanguages``, so the code is None (the SDK then sends no header)
    and a custom vocabulary travels as ``VocabularyNames``, the only vocabulary
    parameter identification accepts. None, not ``""``, for an unset vocabulary: the
    SDK omits the header only for None.
    """
    vocabulary = stt_cfg.transcribe_vocabulary or None
    candidates = stt_cfg.transcribe_language_identification
    if not candidates:
        return {"language_code": stt_cfg.effective_language_code, "vocabulary_name": vocabulary}
    return {
        "language_code": None,
        _IDENTIFY_MULTIPLE_LANGUAGES_KWARG: True,
        "language_options": candidates,
        "vocabulary_names": [vocabulary] if vocabulary else None,
    }


def _vocabulary_rejected(exc: BaseException, vocabulary: str) -> bool:
    """Whether a refused stream start was Amazon Transcribe refusing *vocabulary*.

    Narrow on purpose: a vocabulary must be configured, the refusal must be the
    service's ``BadRequestException`` (raised by ``start_stream_transcription``
    itself, before any audio is sent), and its message must name the vocabulary. A
    bad request for any other reason keeps the generic code, so an operator is
    never sent to change a vocabulary that is not the problem. The stem, not the
    word: with mixed languages the vocabulary rides as ``VocabularyNames``, whose
    refusals speak of "vocabularies".
    """
    if not vocabulary or _TranscribeBadRequest is None:
        return False
    if not isinstance(exc, _TranscribeBadRequest):
        return False
    return "vocabular" in str(getattr(exc, "message", "")).lower()

"""Tests for ``stt.transcribe_languages``, Amazon Transcribe mixed-language detection.

Three contracts, each with its own failure if broken:

* **The list a user reads back is the list every stream carries.** One validator
  decides what the PUT stores and what the loader keeps. Amazon Transcribe refuses
  a stream with two dialects of one language, so such a list is never stored.
* **Identification needs two candidates.** Below that the stream request is the
  single-language one, unchanged; from two up it names no ``LanguageCode`` at all,
  because Amazon Transcribe fails a request that carries both.
* **The picker offers what the backend accepts**: ``vi-VN`` is a common code, and
  a stored code outside the common list stays selectable.

The live socket's request is covered in ``test_stt_stream.py``'s lifecycle class;
these cases call the request builder and the handler directly. Per-test config
isolation comes from the autouse ``KIROCREW_HOME`` fixture in conftest.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.core as core
from kiro_crew.config import sections
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.config.sections import SttConfig
from kiro_crew.dashboard import stt_stream

# ── helpers ──────────────────────────────────────────────────────────────


def _req(method: str = "GET", body: dict | None = None) -> Any:
    """A stub dashboard request; ``api_stt_config`` reads only the method and body."""
    req = MagicMock(spec=web.Request)
    req.method = method
    req.path = "/api/config/stt"
    claims = {"user": "dashboard", "app": ""}
    req.get = lambda key, default=None: claims.get(key, default)
    if body is not None:
        req.json = AsyncMock(return_value=body)
    return req


def _write_stt(**fields: object) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stt": fields}), encoding="utf-8")


def _stored_stt() -> dict:
    return json.loads(config_path().read_text(encoding="utf-8"))["stt"]


@pytest.fixture(autouse=True)
def _stub_probes(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /api/config/stt`` probes the host; none of it is under test here."""
    _floor_monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    _floor_monkeypatch.setattr(
        core, "availability_detail", lambda stt: core.stt.Availability(False)
    )


# ── the list rule ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([], []),
        (["vi-VN", "en-US"], ["vi-VN", "en-US"]),
        # Order is the user's and is kept.
        (["en-US", "vi-VN"], ["en-US", "vi-VN"]),
        ([" vi-VN ", "en-US"], ["vi-VN", "en-US"]),
        # A three-letter language subtag is a real Transcribe locale.
        (["ckb-IQ", "ar-SA"], ["ckb-IQ", "ar-SA"]),
        # One language is a selection in progress, not an error.
        (["vi-VN"], ["vi-VN"]),
        (
            ["vi-VN", "en-US", "fr-FR", "de-DE", "ja-JP"],
            ["vi-VN", "en-US", "fr-FR", "de-DE", "ja-JP"],
        ),
    ],
)
def test_usable_lists_are_kept(value: list[str], expected: list[str]) -> None:
    assert sections.transcribe_language_options(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        # Two dialects of one language fail the stream.
        ["en-US", "en-GB"],
        ["vi-VN", "vi-VN"],
        # More than the most AWS recommends.
        ["vi-VN", "en-US", "fr-FR", "de-DE", "ja-JP", "ko-KR"],
        ["auto", "en-US"],
        ["vi-vn", "en-US"],
        ["vi_VN", "en-US"],
        ["vi-VN,en-US"],
        ["", "en-US"],
        ["vi-VN", 5],
        "vi-VN,en-US",
        {"vi-VN": True},
        None,
        42,
    ],
)
def test_unusable_lists_are_refused(value: object) -> None:
    assert sections.transcribe_language_options(value) is None


def test_defaults_to_off() -> None:
    stt = KiroCrewConfig.load().stt
    assert stt.transcribe_languages == []
    assert stt.transcribe_language_identification == []


def test_a_stored_list_is_loaded() -> None:
    _write_stt(provider="transcribe", transcribe_languages=["vi-VN", "en-US"])
    stt = KiroCrewConfig.load().stt
    assert stt.transcribe_languages == ["vi-VN", "en-US"]
    assert stt.transcribe_language_identification == ["vi-VN", "en-US"]


def test_one_stored_language_keeps_identification_off() -> None:
    _write_stt(provider="transcribe", transcribe_languages=["vi-VN"])
    stt = KiroCrewConfig.load().stt
    assert stt.transcribe_languages == ["vi-VN"]
    assert stt.transcribe_language_identification == []


@pytest.mark.parametrize("stored", [["en-US", "en-GB"], "vi-VN", [7], {"a": 1}])
def test_an_unusable_stored_list_degrades_to_off(stored: object, monkeypatch, caplog) -> None:
    """A hand-edited value can be any JSON. Sending it would make Amazon Transcribe
    refuse every stream, so dictation keeps the single language instead."""
    monkeypatch.setattr(sections, "_LAST_WARNED_TRANSCRIBE_LANGUAGES", None)
    _write_stt(provider="transcribe", language_code="vi-VN", transcribe_languages=stored)
    with caplog.at_level(logging.WARNING):
        stt = KiroCrewConfig.load().stt
    assert stt.transcribe_languages == []
    assert stt.effective_language_code == "vi-VN"
    assert any("transcribe_languages" in r.getMessage() for r in caplog.records)


def test_a_null_stored_list_is_off_without_a_notice_of_its_own(monkeypatch, caplog) -> None:
    """``null`` is the absent key; only the loader's generic type notice names it."""
    monkeypatch.setattr(sections, "_LAST_WARNED_TRANSCRIBE_LANGUAGES", None)
    _write_stt(transcribe_languages=None)
    with caplog.at_level(logging.WARNING):
        assert KiroCrewConfig.load().stt.transcribe_languages == []
    assert not any("Unusable stt.transcribe_languages" in r.getMessage() for r in caplog.records)


def test_the_degrade_notice_deduplicates_the_last_value(monkeypatch, caplog) -> None:
    monkeypatch.setattr(sections, "_LAST_WARNED_TRANSCRIBE_LANGUAGES", None)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.sections"):
        _write_stt(transcribe_languages=["en-US", "en-GB"])
        KiroCrewConfig.load()
        KiroCrewConfig.load()
        _write_stt(transcribe_languages=["fr-FR", "fr-CA"])
        KiroCrewConfig.load()
    notices = [r for r in caplog.records if "transcribe_languages" in r.getMessage()]
    assert len(notices) == 2


# ── the stream request ───────────────────────────────────────────────────


def test_one_language_sends_the_single_language_request() -> None:
    """Below two candidates the request is the one-language request, field for field."""
    stt = SttConfig(provider="transcribe", language_code="vi-VN", transcribe_languages=["vi-VN"])
    assert stt_stream._transcribe_language_kwargs(stt) == {
        "language_code": "vi-VN",
        "vocabulary_name": None,
    }


def test_auto_without_candidates_keeps_the_fallback_locale() -> None:
    stt = SttConfig(provider="transcribe", language_code="auto", transcribe_vocabulary="terms")
    assert stt_stream._transcribe_language_kwargs(stt) == {
        "language_code": sections.STT_LANGUAGE_FALLBACK,
        "vocabulary_name": "terms",
    }


@pytest.mark.parametrize(("vocabulary", "names"), [("", None), ("team-terms", ["team-terms"])])
def test_two_candidates_identify_and_name_no_language_code(
    vocabulary: str, names: list[str] | None
) -> None:
    """``LanguageCode`` beside ``IdentifyMultipleLanguages`` fails the stream, and so
    does ``VocabularyName``: identification takes ``VocabularyNames``."""
    stt = SttConfig(
        provider="transcribe",
        language_code="en-US",
        transcribe_vocabulary=vocabulary,
        transcribe_languages=["vi-VN", "en-US"],
    )
    assert stt_stream._transcribe_language_kwargs(stt) == {
        "language_code": None,
        "identify_multiple_languages": True,
        "language_options": ["vi-VN", "en-US"],
        "vocabulary_names": names,
    }


def _sdk_start_method() -> Any:
    transcribe_client = pytest.importorskip("amazon_transcribe.client")
    return transcribe_client.TranscribeStreamingClient.start_stream_transcription


@pytest.mark.parametrize("languages", [[], ["vi-VN", "en-US"]])
def test_both_requests_bind_to_the_installed_sdk(languages: list[str]) -> None:
    """The keywords are the SDK's own: a misspelt one would raise ``TypeError`` at
    stream start, on the metered provider, after the consent gate passed."""
    stt = SttConfig(
        provider="transcribe", transcribe_vocabulary="terms", transcribe_languages=languages
    )
    inspect.signature(_sdk_start_method()).bind(
        object(),
        media_sample_rate_hz=16000,
        media_encoding="pcm",
        **stt_stream._transcribe_language_kwargs(stt),
    )


def test_the_installed_sdk_supports_identification() -> None:
    """The ``voice-aws`` pin's floor is a version with the parameters."""
    method = _sdk_start_method()
    assert stt_stream._client_identifies_languages(MagicMock(start_stream_transcription=method))


def test_an_sdk_without_the_parameters_is_detected() -> None:
    """The 0.6.2 signature, which predates identification."""

    async def start_stream_transcription(
        *,
        language_code: str,
        media_sample_rate_hz: int,
        media_encoding: str,
        vocabulary_name: str | None = None,
        enable_partial_results_stabilization: bool | None = None,
        partial_results_stability: str | None = None,
    ) -> None:
        return None

    client = MagicMock(start_stream_transcription=start_stream_transcription)
    assert not stt_stream._client_identifies_languages(client)


# ── PUT / GET /api/config/stt ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    resp = await core.api_stt_config(
        _req("PUT", {"provider": "transcribe", "transcribe_languages": ["vi-VN", "en-US"]})
    )
    assert resp.status == 200
    assert json.loads(resp.body)["transcribe_languages"] == ["vi-VN", "en-US"]
    assert _stored_stt()["transcribe_languages"] == ["vi-VN", "en-US"]

    body = json.loads((await core.api_stt_config(_req("GET"))).body)
    assert body["transcribe_languages"] == ["vi-VN", "en-US"]
    assert body["transcribe_languages_min"] == sections.TRANSCRIBE_LANGUAGES_MIN
    assert body["transcribe_languages_max"] == sections.TRANSCRIBE_LANGUAGES_MAX


@pytest.mark.asyncio
async def test_an_empty_list_turns_it_off() -> None:
    await core.api_stt_config(_req("PUT", {"transcribe_languages": ["vi-VN", "en-US"]}))
    resp = await core.api_stt_config(_req("PUT", {"transcribe_languages": []}))
    assert json.loads(resp.body)["transcribe_languages"] == []
    assert _stored_stt()["transcribe_languages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bogus",
    [["en-US", "en-GB"], ["vi-VN", "en-US", "fr-FR", "de-DE", "ja-JP", "ko-KR"], "vi-VN", None],
)
async def test_an_unusable_list_is_skipped_and_siblings_still_apply(bogus: object) -> None:
    """The existing PUT contract: a malformed field is skipped, valid siblings land,
    and the stored candidates stay the ones in force."""
    await core.api_stt_config(_req("PUT", {"transcribe_languages": ["vi-VN", "en-US"]}))
    resp = await core.api_stt_config(
        _req("PUT", {"transcribe_languages": bogus, "language_code": "fr-FR"})
    )
    assert resp.status == 200
    stored = _stored_stt()
    assert stored["transcribe_languages"] == ["vi-VN", "en-US"]
    assert stored["language_code"] == "fr-FR"


@pytest.mark.asyncio
async def test_saving_another_field_keeps_the_candidates() -> None:
    """Settings saves one field per change, so an unrelated save must not reset them."""
    await core.api_stt_config(_req("PUT", {"transcribe_languages": ["vi-VN", "en-US"]}))
    await core.api_stt_config(_req("PUT", {"language_code": "vi-VN", "streaming": True}))
    assert _stored_stt()["transcribe_languages"] == ["vi-VN", "en-US"]


@pytest.mark.asyncio
async def test_vietnamese_is_offered_by_the_picker() -> None:
    await core.api_stt_config(_req("PUT", {"provider": "transcribe"}))
    body = json.loads((await core.api_stt_config(_req("GET"))).body)
    assert "vi-VN" in body["language_codes"]


@pytest.mark.asyncio
async def test_stored_codes_outside_the_common_list_stay_selectable() -> None:
    """A code set in config.json is offered, so choosing another language and
    changing back cannot lose it."""
    _write_stt(
        provider="transcribe", language_code="th-TH", transcribe_languages=["th-TH", "ms-MY"]
    )
    body = json.loads((await core.api_stt_config(_req("GET"))).body)
    codes = body["language_codes"]
    assert body["language_code"] == "th-TH"
    assert "th-TH" in codes and "ms-MY" in codes
    assert len(set(codes)) == len(codes)
    assert codes[: len(core._STT_LANGUAGE_CODES)] == list(core._STT_LANGUAGE_CODES)

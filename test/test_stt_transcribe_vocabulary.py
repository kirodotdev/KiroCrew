"""Tests for ``stt.transcribe_vocabulary``, the Amazon Transcribe custom vocabulary.

Three contracts, each with its own failure if broken:

* **The name a user reads back is the name every request carries.** One validator
  decides what the PUT stores and what the loader keeps, so neither can accept a
  value the other drops. Amazon Transcribe refuses a stream whose vocabulary name is
  malformed, so an unusable stored name is dropped rather than sent.
* **The listing never calls AWS outside the two gates** (``transcribe`` selected,
  Amazon Transcribe confirmed), and never echoes the service's text, which names the
  caller's ARN on an access denial.
* **Every page up to the bounded cap is read**, truncation is exposed, and a
  refusal is reported with the code whose fix applies.

Per-test config isolation comes from the autouse ``KIROCREW_HOME`` fixture in
conftest, so these do not take ``tmp_path`` for the config themselves.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.core as core
from kiro_crew import aws_consent
from kiro_crew import transcribe as tr
from kiro_crew.config import sections
from kiro_crew.config.loader import KiroCrewConfig, config_path

# ── helpers ──────────────────────────────────────────────────────────────


def _req(method: str = "GET", body: dict | None = None, *, app_token: str | None = "") -> Any:
    """A stub request carrying the auth claims ``_deny_app_token`` reads.

    ``app_token`` ``""`` is the dashboard user, a name is an app token, and None is
    a request no auth middleware published a claim for.
    """
    req = MagicMock(spec=web.Request)
    req.method = method
    req.path = "/api/stt/vocabularies"
    claims = {"user": "dashboard", "app": app_token}
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


class _FakeTranscribe:
    """Stands in for ``boto3.Session(...).client("transcribe", ...)``.

    ``pages`` are returned in order; each is the raw ``ListVocabularies`` response.
    """

    def __init__(self, pages: list[dict] | None = None, error: BaseException | None = None):
        self.pages = list(pages or [])
        self.error = error
        self.requests: list[dict] = []
        self.sessions: list[dict] = []
        self.clients: list[dict] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_FakeTranscribe":
        fake = self

        class _Session:
            def __init__(self, profile_name: str | None = None) -> None:
                fake.sessions.append({"profile_name": profile_name})

            def client(self, service: str, **kwargs: Any) -> "_Client":
                fake.clients.append({"service": service, **kwargs})
                return _Client()

        class _Client:
            def list_vocabularies(self, **kwargs: Any) -> dict:
                fake.requests.append(kwargs)
                if fake.error is not None:
                    raise fake.error
                return fake.pages.pop(0) if fake.pages else {"Vocabularies": []}

        monkeypatch.setattr(tr, "boto3", SimpleNamespace(Session=_Session))
        return self


def _vocab(name: str, language: str = "en-US", state: str = "READY") -> dict:
    return {"VocabularyName": name, "LanguageCode": language, "VocabularyState": state}


@pytest.fixture()
def _consented(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The consent gate grants; its own semantics are covered in test_aws_consent."""
    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(aws_consent, "refuse_and_log", gate)
    return gate


@pytest.fixture(autouse=True)
def _stub_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /api/config/stt`` probes the host; none of it is under test here."""
    monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    monkeypatch.setattr(core, "availability_detail", lambda stt: core.stt.Availability(False))


# ── the name rule ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("team-terms", "team-terms"),
        ("Team.Terms_v2", "Team.Terms_v2"),
        # Case is significant to AWS, so it is preserved, not folded.
        ("TeamTerms", "TeamTerms"),
        ("  padded  ", "padded"),
        ("", ""),
        ("   ", ""),
        (
            "x" * sections.TRANSCRIBE_VOCABULARY_NAME_MAX,
            "x" * sections.TRANSCRIBE_VOCABULARY_NAME_MAX,
        ),
    ],
)
def test_usable_names_are_kept_verbatim(value: str, expected: str) -> None:
    assert sections.transcribe_vocabulary_name(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "team terms",  # a space is not in AWS's character set
        "team/terms",
        "équipe",
        "x" * (sections.TRANSCRIBE_VOCABULARY_NAME_MAX + 1),
        "name\nX-Injected: header",
        None,
        42,
        ["team-terms"],
    ],
)
def test_unusable_names_are_refused(value: object) -> None:
    assert sections.transcribe_vocabulary_name(value) is None


def test_defaults_to_no_vocabulary() -> None:
    assert KiroCrewConfig.load().stt.transcribe_vocabulary == ""


def test_a_stored_name_is_loaded() -> None:
    _write_stt(provider="transcribe", transcribe_vocabulary=" team-terms ")
    assert KiroCrewConfig.load().stt.transcribe_vocabulary == "team-terms"


@pytest.mark.parametrize("stored", ["team terms", 7, ["team-terms"], {"name": "x"}, None])
def test_an_unusable_stored_name_degrades_to_none(stored: object, monkeypatch, caplog) -> None:
    """A hand-edited value can be any JSON. Sending it would make Amazon Transcribe
    refuse every stream, so dictation runs without a vocabulary instead."""
    monkeypatch.setattr(sections, "_LAST_WARNED_TRANSCRIBE_VOCABULARY", None)
    _write_stt(provider="transcribe", transcribe_vocabulary=stored)
    with caplog.at_level(logging.WARNING):
        assert KiroCrewConfig.load().stt.transcribe_vocabulary == ""
    if stored is not None:
        assert any("transcribe_vocabulary" in r.getMessage() for r in caplog.records)


def test_the_degrade_notice_deduplicates_the_last_value(monkeypatch, caplog) -> None:
    monkeypatch.setattr(sections, "_LAST_WARNED_TRANSCRIBE_VOCABULARY", None)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.sections"):
        _write_stt(transcribe_vocabulary="not valid")
        KiroCrewConfig.load()
        KiroCrewConfig.load()
        _write_stt(transcribe_vocabulary="also invalid")
        KiroCrewConfig.load()
        KiroCrewConfig.load()
    notices = [r for r in caplog.records if "transcribe_vocabulary" in r.getMessage()]
    assert len(notices) == 2


def test_the_degrade_notice_memory_is_bounded(monkeypatch, caplog) -> None:
    monkeypatch.setattr(sections, "_LAST_WARNED_TRANSCRIBE_VOCABULARY", None)
    with caplog.at_level(logging.CRITICAL, logger="kiro_crew.config.sections"):
        for index in range(1000):
            assert sections._validated_transcribe_vocabulary(f"not valid {index}") == ""

    remembered = sections._LAST_WARNED_TRANSCRIBE_VOCABULARY
    assert remembered == repr("not valid 999")[: sections._TRANSCRIBE_VOCABULARY_NOTICE_VALUE_MAX]
    assert len(remembered) <= sections._TRANSCRIBE_VOCABULARY_NOTICE_VALUE_MAX
    assert "_WARNED_TRANSCRIBE_VOCABULARIES" not in vars(sections)


# ── PUT / GET /api/config/stt ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    resp = await core.api_stt_config(_req("PUT", {"transcribe_vocabulary": "team-terms"}))
    assert resp.status == 200
    assert json.loads(resp.body)["transcribe_vocabulary"] == "team-terms"
    assert _stored_stt()["transcribe_vocabulary"] == "team-terms"

    resp = await core.api_stt_config(_req("GET"))
    assert json.loads(resp.body)["transcribe_vocabulary"] == "team-terms"


@pytest.mark.asyncio
async def test_an_empty_name_clears_it() -> None:
    await core.api_stt_config(_req("PUT", {"transcribe_vocabulary": "team-terms"}))
    resp = await core.api_stt_config(_req("PUT", {"transcribe_vocabulary": ""}))
    assert json.loads(resp.body)["transcribe_vocabulary"] == ""
    assert _stored_stt()["transcribe_vocabulary"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bogus", ["team terms", "x" * 201, 5, None, ["team-terms"]])
async def test_an_unusable_name_is_skipped_and_siblings_still_apply(bogus: object) -> None:
    """The existing PUT contract: a malformed field is skipped, valid siblings land,
    and the stored vocabulary stays the one in force."""
    await core.api_stt_config(_req("PUT", {"transcribe_vocabulary": "team-terms"}))
    resp = await core.api_stt_config(
        _req("PUT", {"transcribe_vocabulary": bogus, "language_code": "fr-FR"})
    )
    assert resp.status == 200
    stored = _stored_stt()
    assert stored["transcribe_vocabulary"] == "team-terms"
    assert stored["language_code"] == "fr-FR"


# ── list_custom_vocabularies ─────────────────────────────────────────────


def test_lists_every_page_sorted_by_name(monkeypatch) -> None:
    fake = _FakeTranscribe(
        pages=[
            {"Vocabularies": [_vocab("zeta"), _vocab("Alpha", "fr-FR")], "NextToken": "p2"},
            {"Vocabularies": [_vocab("beta", state="PENDING")]},
        ]
    ).install(monkeypatch)

    listing = tr.list_custom_vocabularies("team", "eu-west-1")

    assert [v.name for v in listing.vocabularies] == ["Alpha", "beta", "zeta"]
    assert listing.vocabularies[0] == tr.CustomVocabulary(
        name="Alpha", language_code="fr-FR", state="READY"
    )
    # Non-ready ones are kept: "still processing" and "does not exist" differ.
    assert listing.vocabularies[1].state == "PENDING"
    assert listing.truncated is False
    assert fake.requests == [{"MaxResults": 100}, {"MaxResults": 100, "NextToken": "p2"}]
    assert fake.sessions == [{"profile_name": "team"}]
    assert fake.clients[0]["service"] == "transcribe"
    assert fake.clients[0]["region_name"] == "eu-west-1"


def test_an_empty_profile_uses_the_default_chain(monkeypatch) -> None:
    """The stream's own resolution: no profile means boto3's default chain, which
    ``profile_name=""`` would instead refuse as an unknown profile."""
    fake = _FakeTranscribe(pages=[{"Vocabularies": []}]).install(monkeypatch)
    listing = tr.list_custom_vocabularies("", "us-east-1")
    assert listing.vocabularies == []
    assert listing.truncated is False
    assert fake.sessions == [{"profile_name": None}]


def test_stops_after_the_page_cap(monkeypatch, caplog) -> None:
    endless = [{"Vocabularies": [_vocab(f"v{i}")], "NextToken": "more"} for i in range(50)]
    fake = _FakeTranscribe(pages=endless).install(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.transcribe"):
        listing = tr.list_custom_vocabularies("", "us-east-1")
    assert len(fake.requests) == tr._VOCABULARY_MAX_PAGES
    assert len(listing.vocabularies) == tr._VOCABULARY_MAX_PAGES
    assert listing.truncated is True
    assert any("Stopped listing" in r.getMessage() for r in caplog.records)


def test_skips_an_entry_without_a_name(monkeypatch) -> None:
    _FakeTranscribe(pages=[{"Vocabularies": [{"LanguageCode": "en-US"}, _vocab("ok")]}]).install(
        monkeypatch
    )
    assert [v.name for v in tr.list_custom_vocabularies("", "us-east-1").vocabularies] == ["ok"]


def test_an_access_denial_has_its_own_code(monkeypatch) -> None:
    botocore_exceptions = pytest.importorskip("botocore.exceptions")
    denied = botocore_exceptions.ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "User: arn:aws:iam::1:user/x"}},
        "ListVocabularies",
    )
    _FakeTranscribe(error=denied).install(monkeypatch)
    with pytest.raises(tr.VocabularyListError) as raised:
        tr.list_custom_vocabularies("", "us-east-1")
    assert raised.value.code == tr.VOCABULARIES_ACCESS_DENIED


@pytest.mark.parametrize("kind", ["client", "botocore", "value"])
def test_any_other_failure_is_a_list_failure(monkeypatch, kind: str) -> None:
    botocore_exceptions = pytest.importorskip("botocore.exceptions")
    error: BaseException
    if kind == "client":
        error = botocore_exceptions.ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ListVocabularies"
        )
    elif kind == "botocore":
        error = botocore_exceptions.NoCredentialsError()
    else:
        error = ValueError("Invalid endpoint")
    _FakeTranscribe(error=error).install(monkeypatch)
    with pytest.raises(tr.VocabularyListError) as raised:
        tr.list_custom_vocabularies("", "us-east-1")
    assert raised.value.code == tr.VOCABULARIES_LIST_FAILED


def test_without_the_aws_packages_it_is_a_list_failure(monkeypatch) -> None:
    monkeypatch.setattr(tr, "boto3", None)
    with pytest.raises(tr.VocabularyListError) as raised:
        tr.list_custom_vocabularies("", "us-east-1")
    assert raised.value.code == tr.VOCABULARIES_LIST_FAILED


# ── GET /api/stt/vocabularies ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lists_for_the_configured_target(monkeypatch, _consented) -> None:
    _write_stt(provider="transcribe", transcribe_profile="team", transcribe_region="eu-west-1")
    fake = _FakeTranscribe(
        pages=[{"Vocabularies": [_vocab("team-terms"), _vocab("drafts", state="PENDING")]}]
    ).install(monkeypatch)

    resp = await core.api_stt_vocabularies(_req())

    assert resp.status == 200
    assert json.loads(resp.body) == {
        "profile": "team",
        "region": "eu-west-1",
        "listed": True,
        "truncated": False,
        "vocabularies": [
            {"name": "drafts", "language_code": "en-US", "state": "PENDING"},
            {"name": "team-terms", "language_code": "en-US", "state": "READY"},
        ],
    }
    _consented.assert_awaited_once_with(
        aws_consent.SERVICE_TRANSCRIBE, profile="team", region="eu-west-1"
    )
    assert fake.sessions == [{"profile_name": "team"}]


@pytest.mark.asyncio
async def test_endpoint_reports_a_truncated_listing(monkeypatch, _consented) -> None:
    _write_stt(provider="transcribe")
    monkeypatch.setattr(
        core,
        "list_custom_vocabularies",
        lambda profile, region: tr.CustomVocabularyListing(vocabularies=[], truncated=True),
    )

    resp = await core.api_stt_vocabularies(_req())

    assert resp.status == 200
    assert isinstance(resp.body, (bytes, bytearray))
    assert json.loads(resp.body)["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["local", "apple", "off"])
async def test_another_provider_never_reaches_aws(monkeypatch, _consented, provider: str) -> None:
    _write_stt(provider=provider)
    fake = _FakeTranscribe(pages=[{"Vocabularies": [_vocab("team-terms")]}]).install(monkeypatch)

    resp = await core.api_stt_vocabularies(_req())

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["listed"] is False
    assert body["vocabularies"] == []
    assert fake.requests == []
    _consented.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_consent_it_never_reaches_aws(monkeypatch) -> None:
    """The gated answer says AWS was not asked, so the panel cannot read its empty
    list as "the stored vocabulary does not exist"."""
    _write_stt(provider="transcribe")
    monkeypatch.setattr(aws_consent, "refuse_and_log", AsyncMock(return_value=False))
    fake = _FakeTranscribe(pages=[{"Vocabularies": [_vocab("team-terms")]}]).install(monkeypatch)

    resp = await core.api_stt_vocabularies(_req())

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["listed"] is False
    assert body["vocabularies"] == []
    assert fake.requests == []


@pytest.mark.asyncio
async def test_an_access_denial_returns_its_code_and_not_the_service_text(
    monkeypatch, _consented
) -> None:
    botocore_exceptions = pytest.importorskip("botocore.exceptions")
    _write_stt(provider="transcribe")
    _FakeTranscribe(
        error=botocore_exceptions.ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "User: arn:aws:iam::123456789012:user/alice is not authorized",
                }
            },
            "ListVocabularies",
        )
    ).install(monkeypatch)

    resp = await core.api_stt_vocabularies(_req())

    assert resp.status == 502
    body = json.loads(resp.body)
    assert body["code"] == "stt_vocabularies_access_denied"
    # The one fact the fix needs, as data the panel names verbatim.
    assert body["permission"] == "transcribe:ListVocabularies"
    assert "arn:aws" not in resp.body.decode()


@pytest.mark.asyncio
async def test_a_slow_listing_is_bounded(monkeypatch, _consented) -> None:
    _write_stt(provider="transcribe")
    monkeypatch.setattr(core, "_STT_VOCABULARIES_TIMEOUT_SECS", 0.05)

    def _slow(profile: str, region: str) -> list:
        time.sleep(0.3)
        return []

    monkeypatch.setattr(core, "list_custom_vocabularies", _slow)

    resp = await core.api_stt_vocabularies(_req())

    assert resp.status == 502
    assert json.loads(resp.body)["code"] == "stt_vocabularies_list_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("app_token", ["meetings", None])
async def test_an_app_token_is_refused(monkeypatch, _consented, app_token) -> None:
    """Listing what is in the operator's AWS account is setup business, not
    something an app earns by naming the path; an absent claim fails closed."""
    _write_stt(provider="transcribe")
    monkeypatch.setattr(core, "_sel", lambda: MagicMock())
    fake = _FakeTranscribe(pages=[{"Vocabularies": [_vocab("team-terms")]}]).install(monkeypatch)

    resp = await core.api_stt_vocabularies(_req(app_token=app_token))

    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "dashboard_user_required"
    assert fake.requests == []

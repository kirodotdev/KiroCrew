"""A destination's own expiry choices reach its ``publish()``.

A provider declares the periods a user may pick (``SharingModel.expiration_choices_days``),
the dashboard route accepts ``expires_in_days`` and ``publish_sync`` passes the resolved
period to the provider. A provider that declares no choices is never handed the keyword,
and an explicit request to it is refused before anything is uploaded.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from publish_sync_helpers import (  # noqa: F401 -- fixtures are requested by name
    DummyPublishProvider,
    fake_client_fixture,
    store_fixture,
    wire,
)

from kiro_crew import publish_provider, publish_sync
from kiro_crew.artifact_store.records import parse_publication
from kiro_crew.artifacts import ArtifactStore, ArtifactValidationError
from kiro_crew.publish_provider import (
    PROVIDER_DEFAULT_EXPIRY,
    PublishConflictError,
    PublishError,
    PublishResult,
    SharingModel,
)

_CHOICES = SharingModel(expiration_choices_days=(30, 90, 365, 730), default_expiration_days=30)


class ExpiringProvider(DummyPublishProvider):
    """The dummy provider, declaring expiry choices and accepting the keyword."""

    name = "expiring"

    def __init__(self, model: SharingModel = _CHOICES) -> None:
        super().__init__()
        self._model = model

    def sharing_model(self) -> SharingModel:
        return self._model

    async def publish(self, *, expires_in_days, **kwargs) -> PublishResult:
        self.calls.append(("expiry", {"expires_in_days": expires_in_days}))
        return await super().publish(**kwargs)


@pytest.fixture
def expiring(_floor_monkeypatch):
    def _register(**kwargs) -> ExpiringProvider:
        provider = ExpiringProvider(**kwargs)
        publish_provider.register_provider(provider.name, lambda: provider)
        publish_provider._INSTANCES[provider.name] = provider
        return provider

    return _register


# ── the descriptor ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expiration_choices_days": (90, 30), "default_expiration_days": 30},
        {"expiration_choices_days": (30, 30), "default_expiration_days": 30},
        {"expiration_choices_days": (0, 30), "default_expiration_days": 30},
        {"expiration_choices_days": (True, 30), "default_expiration_days": 30},
        {"expiration_choices_days": (30,)},
        {"expiration_choices_days": (30,), "default_expiration_days": 7},
        {"allows_no_expiration": True},
        {"default_expiration_days": 30},
    ],
    ids=[
        "descending",
        "repeat",
        "zero",
        "bool",
        "no-default",
        "default-not-offered",
        "no-expiry-without-choices",
        "default-without-choices",
    ],
)
def test_an_inconsistent_expiry_declaration_is_refused(kwargs):
    with pytest.raises(ValueError):
        SharingModel(**kwargs)


def test_resolution_follows_the_declaration():
    assert _CHOICES.resolve_expiration(PROVIDER_DEFAULT_EXPIRY) == 30
    assert _CHOICES.resolve_expiration(730) == 730
    with pytest.raises(ValueError):
        _CHOICES.resolve_expiration(7)
    with pytest.raises(ValueError):
        _CHOICES.resolve_expiration(None)
    open_ended = SharingModel(
        expiration_choices_days=(30,), allows_no_expiration=True, default_expiration_days=None
    )
    assert open_ended.resolve_expiration(None) is None
    assert open_ended.resolve_expiration(PROVIDER_DEFAULT_EXPIRY) is None
    assert SharingModel().offers_expiration_choice is False


def test_supports_expiration_follows_the_choices_unless_a_provider_sets_it():
    # Declared once: a provider that lists periods need not also set the flag.
    assert _CHOICES.supports_expiration is True
    assert SharingModel().supports_expiration is False
    # An explicit value from a provider written before the choices existed is kept.
    assert SharingModel(supports_expiration=False).supports_expiration is False
    assert SharingModel(supports_expiration=True).supports_expiration is True
    with pytest.raises(ValueError):
        SharingModel(
            supports_expiration=False,
            expiration_choices_days=(30,),
            default_expiration_days=30,
        )


# ── publish_sync ─────────────────────────────────────────────────────────────


_OPEN_ENDED = SharingModel(
    expiration_choices_days=(30,), allows_no_expiration=True, default_expiration_days=30
)


@pytest.mark.asyncio
async def test_the_default_period_reaches_the_provider(store, expiring):
    provider = expiring()
    store.create(name="Doc", content="hi", kind="text", slug="d")
    await publish_sync.publish("d", provider_name="expiring")

    assert provider.called("expiry") == [{"expires_in_days": 30}]
    assert store.get("d").publication is not None


@pytest.mark.asyncio
async def test_a_picked_period_reaches_the_provider(store, expiring):
    provider = expiring()
    store.create(name="Doc", content="hi", kind="text", slug="d")
    await publish_sync.publish("d", provider_name="expiring", expires_in_days=730)

    assert provider.called("expiry") == [{"expires_in_days": 730}]
    pub = store.get("d").publication
    assert (pub.provider, pub.last_synced_kirocrew_version) == ("expiring", 1)


@pytest.mark.asyncio
async def test_no_expiry_reaches_the_provider_as_none(store, expiring):
    provider = expiring(model=_OPEN_ENDED)
    store.create(name="Doc", content="hi", kind="text", slug="d")
    await publish_sync.publish("d", provider_name="expiring", expires_in_days=None)

    assert provider.called("expiry") == [{"expires_in_days": None}]
    pub = store.get("d").publication
    assert (pub.provider, pub.last_synced_kirocrew_version) == ("expiring", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [7, None], ids=["undeclared-period", "no-expiry"])
async def test_an_undeclared_choice_uploads_nothing(store, expiring, requested):
    provider = expiring()
    store.create(name="Doc", content="hi", kind="text", slug="d")
    with pytest.raises(ArtifactValidationError):
        await publish_sync.publish("d", provider_name="expiring", expires_in_days=requested)

    assert provider.calls == []
    assert store.get("d").publication is None


@pytest.mark.asyncio
async def test_a_provider_without_choices_keeps_its_old_signature(store, fake_client):
    # The dummy's publish() has no expires_in_days parameter: handing it the keyword
    # would raise TypeError, so a provider written before this contract still publishes.
    store.create(name="Doc", content="hi", kind="text", slug="d")
    await publish_sync.publish("d")

    assert fake_client.called("upload")
    pub = store.get("d").publication
    assert (pub.provider, pub.last_synced_kirocrew_version, pub.last_error) == ("default", 1, "")


@pytest.mark.asyncio
async def test_a_provider_without_choices_refuses_an_explicit_expiry(store, fake_client):
    store.create(name="Doc", content="hi", kind="text", slug="d")
    with pytest.raises(ArtifactValidationError):
        await publish_sync.publish("d", expires_in_days=30)

    assert fake_client.calls == []


@pytest.mark.asyncio
async def test_a_republish_naming_an_expiry_is_refused_before_content_moves(store, expiring):
    provider = expiring()
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", provider_name="expiring")
    store.update("d", content="v2", snapshot=True)
    provider.calls.clear()

    with pytest.raises(PublishConflictError):
        await publish_sync.publish("d", expires_in_days=730)

    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_republish_without_an_expiry_still_pushes(store, expiring):
    # The panel sends no expiry on a re-publish, so the normal path is unchanged.
    provider = expiring()
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", provider_name="expiring")
    store.update("d", content="v2", snapshot=True)
    provider.calls.clear()

    await publish_sync.publish("d")

    assert provider.called("upload_version")
    assert provider.called("expiry") == []
    assert store.get("d").publication.version_map == {"1": 1, "2": 2}


@pytest.mark.asyncio
async def test_two_first_publishes_racing_cannot_both_report_their_expiry(store, expiring):
    # Two panels saw the artifact unpublished: one chose "Never", the other 30 days.
    # The lock lets one create the publication; the other must fail rather than report
    # a lifetime that never reached the destination. Which one wins is not defined, so
    # the assertions hold for either order.
    provider = expiring(model=_OPEN_ENDED)
    store.create(name="Doc", content="hi", kind="text", slug="d")

    outcomes = await asyncio.gather(
        publish_sync.publish("d", provider_name="expiring", expires_in_days=None),
        publish_sync.publish("d", provider_name="expiring", expires_in_days=30),
        return_exceptions=True,
    )

    refused = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(refused) == 1
    assert isinstance(refused[0], PublishConflictError)
    (applied,) = provider.called("expiry")
    winner = outcomes.index(next(o for o in outcomes if not isinstance(o, BaseException)))
    assert applied == {"expires_in_days": (None, 30)[winner]}
    assert len(provider.called("upload")) == 1


# ── the recorded expiry ──────────────────────────────────────────────────────

_PAST = "2000-01-01T00:00:00+00:00"


async def _published_then_expired(store, provider) -> None:
    store.create(name="Doc", content="v1", kind="text", slug="d")
    await publish_sync.publish("d", provider_name="expiring", expires_in_days=30)
    store.update_publication("d", expires_at=_PAST)
    provider.calls.clear()


@pytest.mark.asyncio
async def test_a_first_publish_records_when_it_expires(store, expiring):
    expiring()
    store.create(name="Doc", content="hi", kind="text", slug="d")
    await publish_sync.publish("d", provider_name="expiring", expires_in_days=730)

    pub = store.get("d").publication
    published = datetime.fromisoformat(pub.published_at)
    assert datetime.fromisoformat(pub.expires_at) == published + timedelta(days=730)
    assert pub.is_expired() is False


@pytest.mark.asyncio
async def test_no_expiry_and_a_destination_without_choices_record_none(store, expiring):
    expiring(model=_OPEN_ENDED)
    store.create(name="Never", content="hi", kind="text", slug="never")
    store.create(name="Plain", content="hi", kind="text", slug="plain")
    await publish_sync.publish("never", provider_name="expiring", expires_in_days=None)
    await publish_sync.publish("plain")

    assert store.get("never").publication.expires_at == ""
    assert store.get("plain").publication.expires_at == ""


def test_a_record_without_the_field_never_expires():
    pub = parse_publication({"artifact_id": "uuid-1", "view_url": "https://x.example/a"})

    assert (pub.expires_at, pub.is_expired()) == ("", False)


@pytest.mark.asyncio
async def test_auto_sync_skips_an_expired_publication(store, expiring):
    provider = expiring()
    await _published_then_expired(store, provider)
    store.update("d", content="v2", snapshot=True)

    await publish_sync.push_version_by_slug("d")

    assert provider.calls == []
    pub = store.get("d").publication
    assert (pub.last_synced_kirocrew_version, pub.last_error) == (1, "")


@pytest.mark.asyncio
async def test_an_expired_publication_is_withdrawn_then_published_again(store, expiring):
    provider = expiring()
    await _published_then_expired(store, provider)
    provider.upload_response = {**provider.upload_response, "artifactId": "uuid-456"}

    summary = await publish_sync.publish("d", expires_in_days=90)

    assert [name for name, _ in provider.calls] == ["delete", "expiry", "upload"]
    assert provider.called("delete") == [{"artifact_id": "uuid-123"}]
    assert provider.called("expiry") == [{"expires_in_days": 90}]
    pub = store.get("d").publication
    assert (pub.artifact_id, pub.provider, pub.is_expired()) == ("uuid-456", "expiring", False)
    assert (summary["artifact_id"], summary["expired"]) == ("uuid-456", False)


@pytest.mark.asyncio
async def test_an_unconfirmed_withdrawal_keeps_the_expired_record(store, expiring):
    provider = expiring()
    await _published_then_expired(store, provider)
    provider.delete_response = {"error": "throttled"}

    with pytest.raises(PublishError):
        await publish_sync.publish("d", expires_in_days=90)

    assert provider.called("upload") == []
    pub = store.get("d").publication
    assert (pub.artifact_id, pub.expires_at) == ("uuid-123", _PAST)


@pytest.mark.asyncio
async def test_an_expired_publication_needs_a_chosen_expiry(store, expiring):
    provider = expiring()
    await _published_then_expired(store, provider)

    with pytest.raises(PublishConflictError):
        await publish_sync.publish("d")

    assert provider.calls == []
    assert store.get("d").publication.artifact_id == "uuid-123"


@pytest.mark.asyncio
async def test_the_served_artifact_says_whether_its_publication_expired(store, expiring):
    from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import _serialize

    provider = expiring()
    store.create(name="Live", content="hi", kind="text", slug="live")
    await publish_sync.publish("live", provider_name="expiring")
    await _published_then_expired(store, provider)

    live = _serialize(store.get("live"))["publication"]
    gone = _serialize(store.get("d"))["publication"]
    assert (bool(live["expires_at"]), live["expired"]) == (True, False)
    assert (gone["expires_at"], gone["expired"]) == (_PAST, True)


# ── the dashboard route ──────────────────────────────────────────────────────


def _publish_request(body: dict):
    from body_stream_helpers import attach_json_body

    req = MagicMock()
    req.app = {"state": MagicMock()}
    req.headers = {"X-Session-Key": "dashboard:ui"}
    req.match_info = {"slug": "doc"}
    req.query = {}
    attach_json_body(req, json.dumps(body).encode())
    return req


@pytest.fixture
def route_publish(monkeypatch, tmp_path):
    from kiro_crew import artifacts as art_mod
    from kiro_crew.dashboard.handlers import artifacts as handlers

    store = ArtifactStore(root=tmp_path / "route-artifacts")
    store.create(name="Doc", content="hi", slug="doc", kind="markdown")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(handlers, "_is_restricted_session", lambda state, request: False)
    monkeypatch.setattr(handlers, "_publish_governance_denied", lambda request, name: None)
    publish = AsyncMock(return_value=None)
    monkeypatch.setattr(handlers.publish_sync, "publish", publish)
    return publish


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, PROVIDER_DEFAULT_EXPIRY),
        ({"expires_in_days": 365}, 365),
        ({"expires_in_days": None}, None),
    ],
    ids=["absent", "days", "null"],
)
async def test_the_route_passes_the_requested_expiry(route_publish, body, expected):
    from kiro_crew.dashboard.handlers import artifacts as handlers

    resp = await handlers.api_artifact_publish(_publish_request({"visibility": "PRIVATE", **body}))

    assert resp.status == 200
    assert route_publish.await_args.kwargs["expires_in_days"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["30", True, 0, -1, 1.5], ids=["string", "bool", "zero", "negative", "fraction"]
)
async def test_the_route_refuses_a_malformed_expiry(route_publish, bad):
    from kiro_crew.dashboard.handlers import artifacts as handlers

    resp = await handlers.api_artifact_publish(
        _publish_request({"visibility": "PRIVATE", "expires_in_days": bad})
    )

    assert resp.status == 400
    route_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_republish_naming_no_provider_publishes_where_it_was_gated(
    route_publish, monkeypatch
):
    # publish() re-reads the artifact under its lock; were it handed the default, a record
    # cleared in between would send the upload to a destination the gate never saw.
    from kiro_crew.artifacts import ArtifactPublication, get_default_store
    from kiro_crew.dashboard.handlers import artifacts as handlers

    get_default_store().set_publication(
        "doc",
        ArtifactPublication(
            artifact_id="uuid-1", view_url="https://x.example/a", provider="expiring"
        ),
    )
    gated: list[str] = []
    monkeypatch.setattr(
        handlers, "_publish_governance_denied", lambda request, name: gated.append(name)
    )

    resp = await handlers.api_artifact_publish(_publish_request({"visibility": "PRIVATE"}))

    assert resp.status == 200
    assert gated == ["expiring"]
    assert route_publish.await_args.kwargs["provider_name"] == "expiring"


@pytest.mark.asyncio
async def test_the_provider_list_carries_the_choices(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import artifacts as handlers

    provider = ExpiringProvider()
    monkeypatch.setattr(handlers, "list_providers", lambda: [provider])

    resp = await handlers.api_artifact_publish_providers(
        make_mocked_request("GET", "/api/artifacts/publish-providers?kind=markdown")
    )

    (row,) = json.loads(resp.text)["providers"]
    assert row["sharing_model"]["expiration_choices_days"] == [30, 90, 365, 730]
    assert row["sharing_model"]["default_expiration_days"] == 30
    assert row["sharing_model"]["allows_no_expiration"] is False

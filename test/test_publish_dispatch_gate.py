"""The enterprise publish check at dispatch.

Every artifact send reaches its destination through a provider from
``publish_provider.get_provider``. In an enterprise build each send method of that
provider asks ``publish_governance.dispatch_denied_reason`` first, naming the
registry key the bytes go to. The handlers' own check reads the artifact's
agent-writable ``meta.json``, and ``publish_sync`` reads it again before it sends;
these tests change the record between the two and show the send is still decided
on the destination actually used.
"""

from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest
from publish_sync_helpers import DummyPublishProvider

from kiro_crew import publish_governance, publish_provider, publish_sync
from kiro_crew.artifacts import ArtifactPublication, ArtifactStore
from kiro_crew.publish_provider import DEFAULT_PROVIDER, PublishDeniedError


class _OtherProvider(DummyPublishProvider):
    name = "other-drive"


def _request(session_key: str = "s-1") -> SimpleNamespace:
    return SimpleNamespace(headers={"X-Session-Key": session_key})


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    return ArtifactStore(root=tmp_path / "artifacts")


@pytest.fixture(name="providers")
def providers_fixture(monkeypatch, store):
    """Two destinations built through ``get_provider``, so the edition decides the wrap."""
    monkeypatch.setattr(publish_sync, "get_default_store", lambda: store)
    built = {DEFAULT_PROVIDER: DummyPublishProvider(), "other-drive": _OtherProvider()}
    saved = dict(publish_provider._FACTORIES)
    publish_provider.reset_providers()
    for key, inst in built.items():
        publish_provider.register_provider(key, lambda inst=inst: inst)
    yield built
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved)
    publish_provider.reset_providers()


@pytest.fixture(name="enterprise")
def enterprise_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture(name="denied")
def denied_fixture(monkeypatch):
    """Policy: ``other-drive`` is refused, the default destination is permitted."""
    asked: list[tuple[str, str]] = []

    def decide(session_key: str, provider_name: str) -> str | None:
        asked.append((session_key, provider_name))
        return "destination refused" if provider_name == "other-drive" else None

    monkeypatch.setattr(publish_governance, "session_denied_reason", decide)
    return asked


def _published(store: ArtifactStore, provider: str = DEFAULT_PROVIDER) -> str:
    art = store.create(name="Doc", content="v1", kind="text", slug="d")
    store.set_publication(
        art.slug,
        ArtifactPublication(
            provider=provider,
            artifact_id="uuid-123",
            view_url="https://x.example/a",
            auto_sync=True,
        ),
    )
    store.update(art.slug, content="v2", snapshot=True)
    return art.slug


async def _in_request(coro_fn, session_key: str = "s-1"):
    """Run ``coro_fn`` as a handler does: in its own task, with its request bound."""

    async def run():
        publish_governance.bind_egress_session(_request(session_key))
        return await coro_fn()

    return await asyncio.create_task(run())


def test_oss_leaves_the_provider_methods_alone(providers):
    inst = publish_provider.get_provider(DEFAULT_PROVIDER)
    assert "publish" not in vars(inst)
    assert type(inst).publish is DummyPublishProvider.publish


def test_enterprise_gates_every_send_method_and_no_read(providers, enterprise):
    inst = publish_provider.get_provider(DEFAULT_PROVIDER)
    for attr in publish_provider._SEND_METHODS:
        assert attr in vars(inst), attr
    for attr in ("unpublish", "fetch_state", "fetch_content", "fetch_comments"):
        assert attr not in vars(inst), attr


@pytest.mark.asyncio
async def test_a_rewritten_meta_json_is_decided_on_the_destination_used(
    store, providers, enterprise, denied
):
    """The handler saw the default destination; the record now names another one."""
    slug = _published(store)
    assert publish_governance.session_denied_reason("s-1", DEFAULT_PROVIDER) is None
    store.update_publication(slug, provider="other-drive")

    with pytest.raises(PublishDeniedError, match="destination refused"):
        await _in_request(lambda: publish_sync.push_version_by_slug(slug))

    assert providers["other-drive"].called("upload_version") == []
    assert ("s-1", "other-drive") in denied


@pytest.mark.asyncio
async def test_a_permitted_destination_sends_with_the_bound_session(
    store, providers, enterprise, denied
):
    slug = _published(store)
    await _in_request(lambda: publish_sync.push_version_by_slug(slug), session_key="s-9")

    assert len(providers[DEFAULT_PROVIDER].called("upload_version")) == 1
    assert denied[-1] == ("s-9", DEFAULT_PROVIDER)


@pytest.mark.asyncio
async def test_a_send_with_no_bound_request_is_denied(store, providers, enterprise, denied):
    slug = _published(store)

    async def unbound():
        return await publish_sync.push_version_by_slug(slug)

    with pytest.raises(PublishDeniedError, match="no request is bound"):
        await asyncio.create_task(unbound())

    assert providers[DEFAULT_PROVIDER].calls == []
    assert denied == []


@pytest.mark.asyncio
async def test_a_first_publish_and_a_sharing_change_are_checked(
    store, providers, enterprise, denied
):
    store.create(name="Doc", content="v1", kind="text", slug="d")
    with pytest.raises(PublishDeniedError):
        await _in_request(lambda: publish_sync.publish("d", provider_name="other-drive"))
    assert providers["other-drive"].calls == []

    store.set_publication(
        "d",
        ArtifactPublication(
            provider="other-drive", artifact_id="uuid-123", view_url="https://x.example/a"
        ),
    )
    with pytest.raises(PublishDeniedError):
        await _in_request(lambda: publish_sync.update_sharing("d", visibility="PUBLIC"))
    assert providers["other-drive"].called("update_sharing") == []


@pytest.mark.asyncio
async def test_a_withdrawal_is_never_refused(store, providers, enterprise, denied):
    store.create(name="Doc", content="v1", kind="text", slug="d")
    store.set_publication(
        "d",
        ArtifactPublication(
            provider="other-drive", artifact_id="uuid-123", view_url="https://x.example/a"
        ),
    )

    await publish_sync.unpublish("d")

    assert store.get("d").publication is None


def test_the_dispatch_decision_reaches_the_governance_ceiling(monkeypatch):
    """No stub between the dispatch check and ``governance_permits``."""
    seen: list[tuple[str, str, str, bool]] = []

    def permits(scope, item, *, session_key="", fail_closed=False, **_kw):
        seen.append((scope, item, session_key, fail_closed))
        return SimpleNamespace(permitted=False, rule="r", layer="ceiling", reason="no")

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", permits)

    def check():
        publish_governance.bind_egress_session(_request("s-3"))
        return publish_governance.dispatch_denied_reason("other-drive")

    assert contextvars.copy_context().run(check) == "no"
    assert seen == [("capabilities.publish", "destinations:other-drive", "s-3", True)]


@pytest.mark.asyncio
async def test_every_artifact_write_route_binds_its_session_first():
    from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
        _non_owner_refusal,
    )

    request = {"internal_auth": True}

    class _Req(dict):
        headers = {"X-Session-Key": "s-7"}

    async def run():
        assert await _non_owner_refusal(_Req(request), "artifacts.artifact_publish") is None
        return publish_governance._EGRESS_SESSION.get()

    assert await asyncio.create_task(run()) == "s-7"


def test_a_dispatch_refusal_answers_403():
    from kiro_crew.dashboard.handlers.artifacts_parts.publish_sharing import (
        _sync_error_response,
    )

    resp = _sync_error_response(
        "artifact_publish", _request(), "d", PublishDeniedError("destination refused")
    )

    assert resp.status == 403

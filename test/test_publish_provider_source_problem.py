"""``PublishProvider.source_problem``: why a publication's remote copy cannot be read.

``fetch_state`` answers ``None`` (or raises) for "gone", "unreachable" and
"signed out" alike, so on its own ``upstream_status`` cannot tell them from
"nothing to sync". The optional hook lets a provider say which, as
``(notice_text, notice_code)``. The payload only gains ``source_notice`` and
``source_notice_code``, the hook defaults to ``None``, and it is bounded and can
never fail the status probe.
"""

from __future__ import annotations

import asyncio

import pytest
from publish_sync_helpers import (  # noqa: F401 -- fixtures are requested by name
    fake_client_fixture,
    store_fixture,
    wire,
)

from kiro_crew import publish_provider, publish_sync
from kiro_crew.artifacts import ArtifactPublication


def _published(store, slug="a"):
    store.create(name=slug, content="x", slug=slug)
    store.set_publication(
        slug,
        ArtifactPublication(
            artifact_id="ext-1",
            view_url="https://x/ext-1",
            version_map={"1": 1},
            last_synced_kirocrew_version=1,
        ),
    )
    return slug


def test_the_default_hook_answers_none(fake_client) -> None:
    assert (
        asyncio.run(publish_provider.PublishProvider.source_problem(fake_client, external_id="e"))
        is None
    )


@pytest.mark.asyncio
async def test_a_missing_state_carries_the_providers_reason(
    store, fake_client, monkeypatch
) -> None:
    slug = _published(store)
    calls: list[str] = []

    async def no_state(*, external_id):
        return None

    async def problem(*, external_id):
        calls.append(external_id)
        return ("The remote copy is in the trash.", "remote_trashed")

    monkeypatch.setattr(fake_client, "fetch_state", no_state)
    monkeypatch.setattr(fake_client, "source_problem", problem)
    status = await publish_sync.upstream_status(slug)
    assert calls == ["ext-1"]
    assert status["source_notice"] == "The remote copy is in the trash."
    assert status["source_notice_code"] == "remote_trashed"
    assert status["tracked"] is True


@pytest.mark.asyncio
async def test_a_raising_fetch_state_still_carries_the_reason(
    store, fake_client, monkeypatch
) -> None:
    slug = _published(store)

    async def boom(*, external_id):
        raise RuntimeError("network down")

    async def problem(*, external_id):
        return ("The destination cannot be reached.", "remote_unreachable")

    monkeypatch.setattr(fake_client, "fetch_state", boom)
    monkeypatch.setattr(fake_client, "source_problem", problem)
    status = await publish_sync.upstream_status(slug)
    assert status["source_notice_code"] == "remote_unreachable"
    assert status["upstream_ahead"] is False


@pytest.mark.asyncio
async def test_an_unavailable_provider_is_still_asked_why(store, fake_client, monkeypatch) -> None:
    # A provider that is listed (installable) but not available, e.g. the drive
    # with its last profile removed, still says why; fetch_state is skipped.
    slug = _published(store)
    fetched: list[str] = []

    async def state(*, external_id):
        fetched.append(external_id)
        return None

    async def problem(*, external_id):
        return ("The account behind this copy is not registered.", "remote_account_missing")

    monkeypatch.setattr(fake_client, "available", lambda: False)
    monkeypatch.setattr(fake_client, "fetch_state", state)
    monkeypatch.setattr(fake_client, "source_problem", problem)
    status = await publish_sync.upstream_status(slug)
    assert status["source_notice_code"] == "remote_account_missing"
    assert fetched == []
    assert status["upstream_ahead"] is False


@pytest.mark.asyncio
async def test_a_readable_copy_never_asks(store, fake_client, monkeypatch) -> None:
    slug = _published(store)

    async def state(*, external_id):
        return {"visibility": "PRIVATE", "current_version": 1}

    async def problem(*, external_id):
        raise AssertionError("source_problem must not run when fetch_state answered")

    monkeypatch.setattr(fake_client, "fetch_state", state)
    monkeypatch.setattr(fake_client, "source_problem", problem)
    status = await publish_sync.upstream_status(slug)
    assert "source_notice" not in status


@pytest.mark.asyncio
async def test_a_failing_or_slow_hook_adds_nothing(store, fake_client, monkeypatch) -> None:
    slug = _published(store)

    async def no_state(*, external_id):
        return None

    async def raising(*, external_id):
        raise RuntimeError("hook bug")

    never = asyncio.Event()

    async def slow(*, external_id):
        # Never answers: the event is never set, so only the bound ends it.
        await never.wait()
        return ("late", "late")

    monkeypatch.setattr(fake_client, "fetch_state", no_state)
    monkeypatch.setattr(fake_client, "source_problem", raising)
    assert "source_notice" not in await publish_sync.upstream_status(slug)

    # A zero bound has already expired, so the hook is cancelled without any
    # wall-clock wait; an unbounded call would hang here instead.
    monkeypatch.setattr(publish_sync, "SOURCE_PROBLEM_TIMEOUT_S", 0)
    monkeypatch.setattr(fake_client, "source_problem", slow)
    assert "source_notice" not in await publish_sync.upstream_status(slug)


@pytest.mark.asyncio
async def test_a_malformed_answer_adds_nothing(store, fake_client, monkeypatch) -> None:
    slug = _published(store)

    async def no_state(*, external_id):
        return None

    for bad in (("", "code"), ("text",), ("text", 3), "text"):

        async def problem(*, external_id, _bad=bad):
            return _bad

        monkeypatch.setattr(fake_client, "fetch_state", no_state)
        monkeypatch.setattr(fake_client, "source_problem", problem)
        assert "source_notice" not in await publish_sync.upstream_status(slug), bad

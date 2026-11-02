"""``ctx.embed``: the shared embedder and the bounded embed pool, behind a public name.

No model is loaded: the shared-embedder accessor is replaced with in-memory fakes,
so every case is offline and deterministic.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest

from kiro_crew import embeddings_shared, executors
from kiro_crew.apps import embed_sdk
from kiro_crew.apps.embed_sdk import EmbedSDK
from kiro_crew.testing.wait import default_timeout


class _FakeBackend:
    def __init__(self, *, ready: bool = True, loadable: bool = False, fail: bool = False) -> None:
        self._ready = ready
        self._loadable = loadable
        self._fail = fail
        self.calls: list[str] = []

    model_id = "fake-model"

    def is_ready(self) -> bool:
        return self._ready

    def can_load_on_demand(self) -> bool:
        return self._loadable

    def embed(self, text: str) -> list[float] | None:
        self.calls.append(text)
        if self._fail:
            raise RuntimeError("model exploded")
        return [1.0, 2.0, float(len(text))]


@pytest.fixture(autouse=True)
def _fresh_log_once(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(embed_sdk, "_logged", set())


def _use(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    monkeypatch.setattr(embeddings_shared, "get_shared_embedder", lambda: backend)


def test_embed_sync_uses_the_shared_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend()
    _use(monkeypatch, backend)
    sdk = EmbedSDK()
    assert sdk.available() is True
    assert sdk.model_id == "fake-model"
    assert sdk.embed_sync("abcd") == [1.0, 2.0, 4.0]
    assert backend.calls == ["abcd"]


def test_an_unloaded_model_that_can_load_on_demand_counts_as_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _FakeBackend(ready=False, loadable=True))
    assert EmbedSDK().available() is True
    assert EmbedSDK().embed_sync("x") == [1.0, 2.0, 1.0]


def test_unavailable_gives_none_without_calling_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend(ready=False, loadable=False)
    _use(monkeypatch, backend)
    sdk = EmbedSDK()
    assert sdk.available() is False
    assert sdk.embed_sync("x") is None
    assert backend.calls == []


def test_no_backend_gives_none_and_an_empty_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken() -> Any:
        raise RuntimeError("no embedding runtime")

    monkeypatch.setattr(embeddings_shared, "get_shared_embedder", _broken)
    sdk = EmbedSDK()
    assert sdk.available() is False
    assert sdk.model_id == ""
    assert sdk.embed_sync("x") is None


def test_an_embed_exception_gives_none_and_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use(monkeypatch, _FakeBackend(fail=True))
    sdk = EmbedSDK()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.embed_sdk"):
        assert sdk.embed_sync("a") is None
        assert EmbedSDK().embed_sync("b") is None
    raised = [r for r in caplog.records if "an embed raised" in r.getMessage()]
    assert len(raised) == 1
    assert raised[0].exc_info is not None


@pytest.mark.asyncio
async def test_embed_sync_on_the_event_loop_refuses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    backend = _FakeBackend()
    _use(monkeypatch, backend)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.embed_sdk"):
        assert EmbedSDK().embed_sync("on loop") is None
    assert backend.calls == []
    assert any("ctx.embed.run" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_goes_through_the_embed_pool_and_embeds_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    _use(monkeypatch, backend)
    pooled: list[Any] = []
    real_pool = executors.run_in_embed_pool

    async def _spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        pooled.append((func, args, kwargs))
        return await real_pool(func, *args, **kwargs)

    monkeypatch.setattr(executors, "run_in_embed_pool", _spy)
    sdk = EmbedSDK()
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _work(text: str, *, suffix: str) -> list[float] | None:
        seen.append(threading.get_ident())
        return sdk.embed_sync(text + suffix)

    result = await asyncio.wait_for(sdk.run(_work, "ab", suffix="c"), timeout=default_timeout())

    assert result == [1.0, 2.0, 3.0]
    assert backend.calls == ["abc"]
    assert seen and seen[0] != loop_thread
    # The call is bound before the hand-off: the pool sees one callable and no
    # keyword of the app's, so an app cannot reach the pool's own parameters.
    assert len(pooled) == 1 and pooled[0][1:] == ((), {})


@pytest.mark.asyncio
async def test_run_does_not_forward_a_keyword_to_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[dict[str, Any]] = []

    async def _spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        received.append(kwargs)
        return func()

    monkeypatch.setattr(executors, "run_in_embed_pool", _spy)

    def _echo(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    out = await asyncio.wait_for(
        EmbedSDK().run(_echo, _admission_held="x"), timeout=default_timeout()
    )
    assert out == {"_admission_held": "x"}
    assert received == [{}]

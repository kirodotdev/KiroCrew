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


# ── Batch embed ──


class _FakeBatchBackend:
    def __init__(
        self,
        *,
        ready: bool = True,
        loadable: bool = False,
        result: Any = "echo",
        model_id: str = "fake-model",
    ) -> None:
        self._ready = ready
        self._loadable = loadable
        self._result = result
        self.model_id = model_id
        self.dim = 3
        self.batches: list[tuple[list[str], int]] = []

    def is_ready(self) -> bool:
        return self._ready

    def can_load_on_demand(self) -> bool:
        return self._loadable

    def embed_batch(self, texts: list[str], *, priority: int) -> Any:
        self.batches.append((list(texts), priority))
        if self._result == "echo":
            return [[1.0, 0.0, float(len(t))] for t in texts]
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_batch_embed_returns_vectors_with_the_space_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    out = EmbedSDK().embed_batch_sync(["ab", "abc"])
    assert out.ready is True and out.reason == ""
    assert out.vectors == [[1.0, 0.0, 2.0], [1.0, 0.0, 3.0]]
    assert out.model_id == "fake-model"
    assert out.dim == 3
    assert out.space_signature == embeddings_shared.embedding_space_signature("fake-model", 3)
    assert out.to_dict()["space_signature"] == out.space_signature


def test_batch_embed_runs_behind_memory_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.embeddings_settings import PRIORITY_BULK, PRIORITY_INTERACTIVE

    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    EmbedSDK().embed_batch_sync(["x"])
    assert backend.batches[0][1] == PRIORITY_BULK
    assert backend.batches[0][1] > PRIORITY_INTERACTIVE


def test_every_app_context_can_batch_embed_without_a_grant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # Fair use comes from bulk priority and the batch cap, not a manifest key:
    # a grant would only gate the path that yields to the host's memory work.
    from kiro_crew.apps.context import AppContext, build_app_context
    from kiro_crew.apps.manifest_sections import Permissions

    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    for ctx in (
        build_app_context("a", tmp_path, permissions={}),
        AppContext(name="a", data_dir=tmp_path),
    ):
        assert ctx.embed.embed_batch_sync(["x"]).ready is True
    assert len(backend.batches) == 2
    assert not hasattr(Permissions(), "embeddings")
    assert not hasattr(embed_sdk, "REASON_NOT_PERMITTED")


def test_batch_embed_caps_the_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    sdk = EmbedSDK()
    assert sdk.embed_batch_sync(["x"] * embed_sdk.MAX_BATCH_TEXTS).ready is True
    over = sdk.embed_batch_sync(["x"] * (embed_sdk.MAX_BATCH_TEXTS + 1))
    assert over.reason == embed_sdk.REASON_TOO_MANY_TEXTS
    assert len(backend.batches) == 1


@pytest.mark.parametrize(
    ("texts", "instruction"),
    [([], None), ("abc", None), (["ok", ""], None), (["ok", 3], None), (["ok"], "  ")],
)
def test_batch_embed_refuses_malformed_input(
    monkeypatch: pytest.MonkeyPatch, texts: Any, instruction: Any
) -> None:
    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    out = EmbedSDK().embed_batch_sync(texts, instruction=instruction)
    assert out.reason == embed_sdk.REASON_INVALID_INPUT
    assert backend.batches == []


def test_batch_embed_applies_the_instruction_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    EmbedSDK().embed_batch_sync(["a", "b"], instruction="Group tickets")
    assert backend.batches[0][0] == [
        "Instruct: Group tickets\nQuery:a",
        "Instruct: Group tickets\nQuery:b",
    ]
    too_long = "x" * (embed_sdk.MAX_INSTRUCTION_CHARS + 1)
    out = EmbedSDK().embed_batch_sync(["a"], instruction=too_long)
    assert out.reason == embed_sdk.REASON_INVALID_INPUT


def test_batch_embed_reports_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    absent = _FakeBatchBackend(ready=False, loadable=False)
    _use(monkeypatch, absent)
    assert EmbedSDK().embed_batch_sync(["x"]).reason == "not_ready"
    assert absent.batches == []

    # Loadable but cold: the backend answers None while its load runs.
    cold = _FakeBatchBackend(ready=False, loadable=True, result=None)
    _use(monkeypatch, cold)
    assert EmbedSDK().embed_batch_sync(["x"]).reason == "not_ready"

    def _broken() -> Any:
        raise RuntimeError("no embedding runtime")

    monkeypatch.setattr(embeddings_shared, "get_shared_embedder", _broken)
    assert EmbedSDK().embed_batch_sync(["x"]).reason == "not_ready"


@pytest.mark.parametrize(
    "result",
    [None, RuntimeError("boom"), [[1.0, 2.0, 3.0]], [[1.0], [1.0, 2.0]], [[], []]],
)
def test_batch_embed_failures_never_raise(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    _use(monkeypatch, _FakeBatchBackend(result=result))
    out = EmbedSDK().embed_batch_sync(["a", "b"])
    assert out.ready is False and out.reason == embed_sdk.REASON_FAILED
    assert out.vectors == []


@pytest.mark.asyncio
async def test_batch_embed_sync_on_the_event_loop_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    out = EmbedSDK().embed_batch_sync(["x"])
    assert out.reason == embed_sdk.REASON_ON_EVENT_LOOP
    assert backend.batches == []


@pytest.mark.asyncio
async def test_async_batch_embed_runs_on_the_embed_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBatchBackend()
    _use(monkeypatch, backend)
    pooled: list[Any] = []
    real_pool = executors.run_in_embed_pool

    async def _spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        pooled.append(func)
        return await real_pool(func, *args, **kwargs)

    monkeypatch.setattr(executors, "run_in_embed_pool", _spy)
    out = await asyncio.wait_for(
        EmbedSDK().embed_batch(["ab"], instruction="q"),
        timeout=default_timeout(),
    )
    assert out.ready is True and len(pooled) == 1
    assert backend.batches[0][0] == ["Instruct: q\nQuery:ab"]

"""How long a fresh prompt build waits for the embedding of its first message.

A build keeps ``_PROMPT_BUILD_EMBED_TIMEOUT_SECS`` unless that embedding is already
running as it starts (a dashboard turn starts it early). Then the build only joins
the inference, and a joined wait ends on time, so it waits the shorter
``memory.startup_recall_timeout_secs``; ``0`` there turns the early embed off and
keeps the long wait. Each deadline is compared exactly against a ``ManualClock``
installed on ``budget``.
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict

import pytest

from kiro_crew import context as ctx
from kiro_crew import embeddings
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.config.memory_sections import STARTUP_RECALL_TIMEOUT_MAX_SECS
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.context_assembly import budget as budget_mod
from kiro_crew.embeddings import PRIORITY_INTERACTIVE, embedding_work
from kiro_crew.testing.clock import ManualClock

QUERY = "summarize the deploy checklist"


@pytest.mark.parametrize(
    "section, built",
    [
        pytest.param({}, 1.0, id="absent"),
        pytest.param({"startup_recall_timeout_secs": 2.5}, 2.5, id="seconds"),
        pytest.param({"startup_recall_timeout_secs": 0}, 0.0, id="off"),
        pytest.param({"startup_recall_timeout_secs": -3}, 0.0, id="negative"),
        pytest.param({"startup_recall_timeout_secs": 99}, 10.0, id="above-ceiling"),
        pytest.param({"startup_recall_timeout_secs": "soon"}, 1.0, id="not-a-number"),
        pytest.param({"startup_recall_timeout_secs": True}, 1.0, id="boolean"),
        pytest.param({"startup_recall_timeout_secs": None}, 1.0, id="null"),
        pytest.param({"startup_recall_timeout_secs": math.inf}, 1.0, id="infinite"),
    ],
)
def test_the_wait_is_clamped_and_an_unreadable_value_keeps_the_default(section, built) -> None:
    assert STARTUP_RECALL_TIMEOUT_MAX_SECS == 10.0
    assert _build_memory_config(section).startup_recall_timeout_secs == built


def _write_memory_config(**memory: object) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"memory": memory}), encoding="utf-8")
    loaded = KiroCrewConfig.load().memory.startup_recall_timeout_secs
    assert loaded == memory.get("startup_recall_timeout_secs", 1.0)


@pytest.mark.parametrize(
    "memory, running, wait",
    [
        pytest.param({}, False, ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS, id="no-early-embed"),
        pytest.param(
            {"startup_recall_timeout_secs": 2.5},
            False,
            ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS,
            id="configured-but-not-running",
        ),
        pytest.param({}, True, 1.0, id="running-default"),
        pytest.param({"startup_recall_timeout_secs": 2.5}, True, 2.5, id="running-configured"),
        pytest.param(
            {"startup_recall_timeout_secs": 0.0},
            True,
            ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS,
            id="running-but-off",
        ),
    ],
)
def test_a_build_waits_short_only_for_an_embedding_already_running(
    monkeypatch: pytest.MonkeyPatch, memory: dict[str, float], running: bool, wait: float
) -> None:
    _write_memory_config(**memory)
    asked: list[str] = []

    def in_flight(text: str) -> bool:
        asked.append(text)
        return running

    monkeypatch.setattr(embeddings, "embedding_in_flight", in_flight)
    clock = ManualClock()
    clock.install(monkeypatch, budget_mod)
    with ctx._prompt_build_embedding_deadline(True, query=QUERY):
        work = embedding_work.get()
        assert work is not None
        assert work.deadline == clock.monotonic() + wait
    assert asked == [QUERY]


def test_a_build_with_no_query_keeps_the_long_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "embedding_in_flight", lambda text: pytest.fail(text))
    clock = ManualClock()
    clock.install(monkeypatch, budget_mod)
    with ctx._prompt_build_embedding_deadline(True):
        work = embedding_work.get()
        assert work is not None
        assert work.deadline == clock.monotonic() + ctx._PROMPT_BUILD_EMBED_TIMEOUT_SECS


class _HeldBackend(embeddings.EmbeddingBackend):
    """Holds each inference until released; reports when one is running."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.running = threading.Event()

    @property
    def model_id(self) -> str:
        return "kirocrew-test-held-v1"

    @property
    def dim(self) -> int:
        return 4

    def is_ready(self) -> bool:
        return True

    def embed(self, text: str, *, priority: int = PRIORITY_INTERACTIVE) -> list[float]:
        self.running.set()
        assert self.release.wait(60), "the test never released the held inference"
        return [1.0, 0.0, 0.0, 0.0]

    def embed_batch(self, texts: list[str], *, priority: int = PRIORITY_INTERACTIVE):
        return [self.embed(t) for t in texts]

    def close(self) -> None:
        """Owns nothing."""


def test_an_embedding_is_in_flight_only_while_its_inference_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _HeldBackend()
    monkeypatch.setattr(embeddings, "_shared_embedder", backend, raising=False)
    monkeypatch.setattr(embeddings, "_sync_embed_cache", OrderedDict())
    monkeypatch.setattr(embeddings, "_sync_embed_flights", {})
    monkeypatch.setattr(embeddings, "_sync_embed_cache_backend", None)
    assert embeddings.embedding_in_flight(QUERY) is False

    owner = threading.Thread(
        target=embeddings._shared_sync_embed,
        args=(QUERY,),
        kwargs={"priority": PRIORITY_INTERACTIVE},
        daemon=True,
    )
    owner.start()
    try:
        assert backend.running.wait(30), "the inference never started"
        assert embeddings.embedding_in_flight(QUERY) is True
        assert embeddings.embedding_in_flight("another message") is False
    finally:
        backend.release.set()
        owner.join(30)
    assert not owner.is_alive()
    # Finished: the vector is cached, and nothing is running any more.
    assert embeddings.embedding_in_flight(QUERY) is False


def test_no_shared_embedder_means_nothing_is_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "_shared_embedder", None, raising=False)
    assert embeddings.embedding_in_flight(QUERY) is False

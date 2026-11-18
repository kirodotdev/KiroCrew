"""Knowledge extraction on a signed-out Kiro CLI fails fast with the remedy.

The extraction pool runs on Kiro CLI whenever the chosen agent cannot run it,
KAS included, so a gateway signed in only to KAS reaches a signed-out Kiro CLI.
Before this, every chunk started Kiro CLI on its own (twice, with the start's
retry), was refused, and was stored with no extraction, so a long document
outlived ``knowledge_add_document``'s timeout. Now the first refused start ends
the batch and the ingest, and the caller gets text saying what to do.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.transport_errors import AcpAuthRequired, AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.knowledge import llm_pool
from kiro_crew.knowledge.agent_source import add_agent_document
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.llm_pool import (
    AcpWorker,
    KnowledgeHarnessUnavailable,
    KnowledgeKiroCliSignedOut,
    LLMPool,
)
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# kiro-cli 3.4.0's own answer to session/new with no sign-in, as the client raises it.
_SIGNED_OUT = AcpError(
    "JSON-RPC error: {'code': -32000, 'message': 'Authentication required. Not logged "
    "in. Set the KIRO_API_KEY environment variable or run `kiro-cli login` first.'}"
)


class _Client:
    """An AcpClient stand-in whose start raises *exc*."""

    def __init__(self, exc: BaseException | None = None, backend: str = ACP_BACKEND_KIRO):
        self._exc = exc
        self.backend = backend
        self.is_ready = exc is None

    def authored_spec_declares_zero_tools(self) -> bool:
        return True

    async def ensure_ready(self) -> None:
        if self._exc is not None:
            raise self._exc

    async def shutdown(self) -> None:
        return None


@pytest.fixture
def login_command(monkeypatch):
    """Keep remedy assertions independent of the host's Kiro CLI install."""
    monkeypatch.setattr("kiro_crew.kiro_cli_bundle.spawn_login_command", lambda: "kiro-cli login")


@pytest.fixture
def no_spec_repair(monkeypatch):
    monkeypatch.setattr(llm_pool, "_repair_agent_spec", lambda *a, **k: True)


@pytest.fixture
def whoami(monkeypatch):
    """What the gateway's whoami check answers; False (unknown) by default."""
    answer = {"signed_out": False}

    async def _confirmed() -> bool:
        return answer["signed_out"]

    monkeypatch.setattr("kiro_crew.kiro_prerequisite.kiro_signed_out_confirmed", _confirmed)
    return answer


def _start(monkeypatch, exc: BaseException, backend: str) -> None:
    monkeypatch.setattr(llm_pool, "AcpClient", lambda **kw: _Client(exc, backend))
    worker = AcpWorker(sandbox_mode="off", acp_backend=backend)
    asyncio.run(worker._spawn_client(backend, "off"))


class TestTheWorkerNamesTheSignIn:
    def test_kiro_cli_saying_not_logged_in_is_the_refusal(
        self, monkeypatch, no_spec_repair, whoami, login_command
    ):
        with pytest.raises(KnowledgeKiroCliSignedOut) as info:
            _start(monkeypatch, _SIGNED_OUT, ACP_BACKEND_KIRO)
        text = str(info.value)
        assert "Knowledge Library extraction needs Kiro CLI signed in" in text
        assert "kiro-cli login" in text
        assert "add the document again" in text
        assert info.value.__cause__ is _SIGNED_OUT
        # The ingest's existing harness-refusal path is what fails the job.
        assert isinstance(info.value, KnowledgeHarnessUnavailable)

    def test_an_expired_token_is_the_refusal(self, monkeypatch, no_spec_repair, whoami):
        expired = AcpError("AccessDeniedException: Invalid token")
        with pytest.raises(KnowledgeKiroCliSignedOut) as info:
            _start(monkeypatch, expired, ACP_BACKEND_KIRO)
        assert info.value.__cause__ is expired

    def test_auth_required_is_the_refusal(self, monkeypatch, no_spec_repair, whoami):
        with pytest.raises(KnowledgeKiroCliSignedOut):
            _start(monkeypatch, AcpAuthRequired("x", backend=ACP_BACKEND_KIRO), ACP_BACKEND_KIRO)

    def test_auth_required_subclass_is_the_refusal(self, whoami):
        class AuthRequiredSubclass(AcpAuthRequired):
            pass

        exc = AuthRequiredSubclass("x", backend=ACP_BACKEND_KIRO)
        assert asyncio.run(llm_pool._kiro_cli_signed_out(exc)) is True

    def test_whoami_confirms_an_unworded_failure(self, monkeypatch, no_spec_repair, whoami):
        whoami["signed_out"] = True
        with pytest.raises(KnowledgeKiroCliSignedOut):
            _start(monkeypatch, AcpError("process exited"), ACP_BACKEND_KIRO)

    def test_any_other_failure_keeps_its_error(self, monkeypatch, no_spec_repair, whoami):
        err = AcpError("process exited")
        with pytest.raises(AcpError) as info:
            _start(monkeypatch, err, ACP_BACKEND_KIRO)
        assert info.value is err

    def test_another_backend_is_not_asked(self, monkeypatch, no_spec_repair, whoami):
        whoami["signed_out"] = True
        with pytest.raises(AcpError) as info:
            _start(monkeypatch, _SIGNED_OUT, ACP_BACKEND_KAS)
        assert info.value is _SIGNED_OUT

    def test_a_prompt_that_fails_as_a_connection_error_is_asked(self, whoami):
        """A signed-out bundled kiro-cli-chat opens sessions, then fails each prompt."""
        whoami["signed_out"] = True
        err = AcpError("dispatch failure")
        err.connection_failure = True
        worker = AcpWorker(sandbox_mode="off", acp_backend=ACP_BACKEND_KIRO)
        client = _Client(None, ACP_BACKEND_KIRO)
        client.send_message = AsyncMock(side_effect=err)  # type: ignore[attr-defined]
        worker._client = client  # type: ignore[assignment]
        with pytest.raises(KnowledgeKiroCliSignedOut):
            asyncio.run(worker.send_message("p"))
        assert worker._client is None

        whoami["signed_out"] = False
        worker._client = client  # type: ignore[assignment]
        with pytest.raises(AcpError) as info:
            asyncio.run(worker.send_message("p"))
        assert info.value is err


class TestTheBatchAnswersAtTheFirstRefusal:
    def test_the_refusal_does_not_wait_for_the_other_items(self):
        """gather raises the first refusal at once; nothing has to be cancelled."""
        pool = LLMPool.__new__(LLMPool)
        b_started = asyncio.Event()
        release_b = asyncio.Event()
        b_task: list[asyncio.Task] = []

        async def _send(prompt, timeout=0):
            if prompt == "a":
                await b_started.wait()  # refuse only once b is in flight
                raise KnowledgeKiroCliSignedOut()
            b_task.append(asyncio.current_task())  # type: ignore[arg-type]
            b_started.set()
            await release_b.wait()
            return "late"

        pool.send = _send  # type: ignore[method-assign]

        async def _run() -> None:
            batch = asyncio.create_task(pool.send_batch(["a", "b"]))
            with pytest.raises(KnowledgeKiroCliSignedOut):
                await asyncio.wait_for(batch, timeout=FALLBACK_TIMEOUT_SECS)
            (b,) = b_task
            assert not b.done()  # b was still pending when the refusal came
            release_b.set()
            await asyncio.wait_for(b, timeout=FALLBACK_TIMEOUT_SECS)

        asyncio.run(_run())


class _RefusingPool:
    async def send(self, prompt: str, timeout: float = 0) -> str:
        raise KnowledgeKiroCliSignedOut()

    async def send_batch(self, prompts: list[str], timeout: float = 0) -> list[str]:
        raise KnowledgeKiroCliSignedOut()


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s._close_all_for_tests()


def _one_chunk(text, **kw):
    return [
        {"content": text, "chunk_index": 0, "section_title": None, "line_start": 0, "line_end": 0}
    ]


class TestAddDocumentAnswersWithTheRemedy:
    def test_the_add_reports_the_cause_and_stores_nothing(self, kstore, login_command):
        chunker = MagicMock()
        for m in ("chunk", "chunk_markdown", "chunk_code", "chunk_slides"):
            getattr(chunker, m).side_effect = _one_chunk
        pipeline = IngestionPipeline(
            store=kstore,
            extractor=EntityExtractor(pool=_RefusingPool()),  # type: ignore[arg-type]
            chunker=chunker,
            reader=FileReader(),
            embedder=None,
            dedup_enabled=False,
        )
        result = asyncio.run(
            add_agent_document(
                pipeline, title="Notes", content="Some body text.", source_uri="https://e.test/n"
            )
        )
        assert result["status"] == "error"
        assert result["code"] == "knowledge_kiro_cli_signed_out"
        assert "needs Kiro CLI signed in" in result["error"]
        assert kstore.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        job = kstore.db.execute("SELECT status, error FROM ingestion_jobs").fetchone()
        assert job["status"] == "failed"
        assert "needs Kiro CLI signed in" in job["error"]

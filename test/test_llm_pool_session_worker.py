"""Knowledge extraction on KAS runs on the gateway's background runtime.

The pool cannot spawn its own client for KAS (KAS needs ``AcpRuntime``). When the
gateway's session manager runs its shared background runtime on KAS, each chunk is
one ``run_bg_oneliner`` there (:class:`SessionWorker`): ``kirocrew-lite``, tool
requests rejected, the session destroyed afterwards. Without that runtime, KAS falls
back to Kiro CLI, and an LTS build refuses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.knowledge import llm_pool as llm_pool_mod
from kiro_crew.knowledge import llm_pool_session as session_mod
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.llm_pool import (
    AcpWorker,
    KnowledgeHarnessUnavailable,
    KnowledgeSpecUnconfirmed,
    LLMPool,
    _get_acp_backend,
)
from kiro_crew.knowledge.llm_pool_session import (
    KnowledgeHostMemoryCritical,
    KnowledgeKasSignedOut,
    SessionWorker,
)
from kiro_crew.testing.wait import async_wait_until, default_timeout

KIRO = acp_backends.ACP_BACKEND_KIRO
KAS = acp_backends.ACP_BACKEND_KAS
CODEX = acp_backends.ACP_BACKEND_CODEX
KAS_CONFIG = {"agent": {"acp_backend": "kas"}}

REPLY = json.dumps(
    {
        "title": "DynamoDB notes",
        "entities": [{"name": "DynamoDB", "type": "technology"}],
        "relations": [],
        "category": "document",
        "summary": "About DynamoDB.",
    }
)


class FakeSessions:
    """The two reads the pool makes of the gateway's session manager."""

    def __init__(self, backend: str = KAS, *, runtime: bool = True) -> None:
        self.backend = backend
        self.runtime = runtime

    def _bg_backend_supports_runtime(self) -> bool:
        return self.runtime

    def _configured_bg_backend(self) -> str:
        return self.backend


class FakeOneliner:
    """Stands in for ``run_bg_oneliner``: counts calls, overlap and cleanup."""

    def __init__(self, reply: str = REPLY, *, delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.calls: list[dict] = []
        self.running = 0
        self.max_running = 0
        self.destroyed = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self, sessions, prompt, **kwargs):
        self.calls.append({"sessions": sessions, "prompt": prompt, **kwargs})
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
            return self.reply
        finally:
            # run_bg_oneliner destroys its session in its own ``finally``.
            self.running -= 1
            self.destroyed += 1


@pytest.fixture(autouse=True)
def _kas_selectable(_floor_monkeypatch):
    _floor_monkeypatch.setattr(acp_backends, "_baseline", {KIRO, KAS, CODEX})
    _floor_monkeypatch.setattr(acp_backends, "_selectable", {KIRO, KAS, CODEX})
    _floor_monkeypatch.setattr(llm_pool_mod, "_read_config", lambda: KAS_CONFIG)
    _floor_monkeypatch.setattr(llm_pool_mod, "_knowledge_spec_model", lambda: None)
    llm_pool_mod._WARNED_UNSAFE_BACKENDS.clear()
    yield
    llm_pool_mod._WARNED_UNSAFE_BACKENDS.clear()


def _answers(*values):
    """An async ``_lite_spec_toolless`` stand-in answering *values* in turn."""
    it = iter(values)

    async def _fake(sessions, backend):
        return next(it)

    return _fake


@pytest.fixture
def toolless(monkeypatch):
    async def _yes(sessions, backend):
        return True

    monkeypatch.setattr(session_mod, "_lite_spec_toolless", _yes)


@pytest.fixture
def oneliner(monkeypatch):
    fake = FakeOneliner()
    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", fake)
    return fake


@pytest.fixture
def no_acp_client(monkeypatch):
    """Any Kiro CLI (or other) ``AcpClient`` the pool builds fails the test."""
    client = MagicMock(side_effect=AssertionError("the pool built an AcpClient"))
    monkeypatch.setattr(llm_pool_mod, "AcpClient", client)
    return client


@pytest.fixture
def lts(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


class TestTheBackendChoice:
    def test_kas_stays_kas_when_the_background_runtime_runs_it(self):
        assert _get_acp_backend(KAS_CONFIG, sessions=FakeSessions(KAS)) == KAS

    def test_lts_kas_runs_on_kas_instead_of_refusing(self, lts):
        assert _get_acp_backend(KAS_CONFIG, sessions=FakeSessions(KAS)) == KAS

    def test_without_a_session_manager_kas_falls_back_as_before(self):
        assert _get_acp_backend(KAS_CONFIG) == KIRO

    def test_lts_without_a_session_manager_still_refuses(self, lts):
        with pytest.raises(KnowledgeHarnessUnavailable):
            _get_acp_backend(KAS_CONFIG)

    @pytest.mark.parametrize(
        "sessions",
        [FakeSessions(KIRO), FakeSessions(KAS, runtime=False)],
        ids=["runtime-on-another-backend", "no-shared-runtime"],
    )
    def test_kas_falls_back_when_the_runtime_does_not_run_kas(self, sessions):
        assert _get_acp_backend(KAS_CONFIG, sessions=sessions) == KIRO

    def test_codex_never_takes_the_session_path(self):
        # Codex runs some calls without asking, so a zero-tool spec is no ban there.
        config = {"agent": {"acp_backend": "codex"}}
        assert _get_acp_backend(config, sessions=FakeSessions(CODEX)) == KIRO

    def test_an_unreadable_session_manager_falls_back(self):
        broken = MagicMock()
        broken._bg_backend_supports_runtime.side_effect = RuntimeError("closing")
        assert _get_acp_backend(KAS_CONFIG, sessions=broken) == KIRO


class TestExtractionEffort:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ample_host_free_memory")
    @pytest.mark.parametrize(
        ("effort", "levels", "advertised", "raises", "effective", "warning"),
        [
            ("high", ["low", "medium", "high"], True, False, "high", ""),
            (None, ["low", "medium", "high"], True, False, None, ""),
            ("high", ["low", "medium", "high"], True, True, "high", "could not apply"),
            ("high", ["low", "medium"], True, False, "medium", "downgraded"),
            ("high", ["off"], True, False, None, "no supported effort"),
            ("high", [], False, False, None, "unsupported"),
        ],
        ids=["high", "default", "write-fails", "fold", "off-only", "no-option"],
    )
    async def test_pool_effort_reaches_the_selected_kas_session(
        self, monkeypatch, toolless, caplog, effort, levels, advertised, raises, effective, warning
    ):
        from kiro_crew.acp.types import EVENT_TEXT_CHUNK

        config = {**KAS_CONFIG, "knowledge": {"extraction_effort": effort}}
        monkeypatch.setattr(llm_pool_mod, "_read_config", lambda: config)
        monkeypatch.setattr(llm_pool_mod, "_knowledge_spec_model", lambda: "chosen-model")
        option = llm_pool_mod.effort_config_option_id(KAS)
        sent = []
        order = []
        session = SimpleNamespace(
            kas_registered_agents=[{"id": "kirocrew-lite", "tools": []}],
            kas_projected_agent="kirocrew-lite",
            kas_active_mode_id="",
            destroy=AsyncMock(),
        )

        async def set_model(model):
            assert model == "chosen-model"
            order.append("model")

        def supports(config_id):
            assert order == ["model"]
            assert config_id == option
            return advertised

        async def set_effort(config_id, value):
            order.append("effort")
            if raises:
                raise RuntimeError("effort unavailable")

        async def prompt(text, **kwargs):
            order.append("prompt")
            sent.append(text)
            yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=REPLY)

        session.set_model = set_model
        session.supports_config_option = MagicMock(side_effect=supports)
        session.get_valid_effort_levels = MagicMock(return_value=levels)
        session.set_config_option = AsyncMock(side_effect=set_effort)
        session.prompt = prompt
        sessions = FakeSessions(KAS)
        sessions.get_bg_session = AsyncMock(return_value=session)
        pool = LLMPool(pool_size=1, effort_key="extraction_effort", sessions=lambda: sessions)
        with caplog.at_level(logging.WARNING, logger=session_mod.logger.name):
            try:
                assert await pool.send("document chunk") == REPLY
            finally:
                await pool.shutdown()
        if effective is None:
            session.set_config_option.assert_not_awaited()
            assert order == ["model", "prompt"]
        else:
            session.set_config_option.assert_awaited_once_with(option, effective)
            assert order == ["model", "effort", "prompt"]
        if effort is None:
            session.supports_config_option.assert_not_called()
            session.get_valid_effort_levels.assert_not_called()
        messages = [r.message for r in caplog.records if r.name == session_mod.logger.name]
        if warning:
            assert any(warning in message for message in messages)
        else:
            assert messages == []
        assert len(sent) == 1 and sent[0].endswith("document chunk")
        session.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ample_host_free_memory")
    async def test_an_effort_the_model_does_not_take_is_asked_and_logged_once(
        self, monkeypatch, toolless, caplog
    ):
        from kiro_crew.acp.types import EVENT_TEXT_CHUNK

        config = {**KAS_CONFIG, "knowledge": {"extraction_effort": "high"}}
        monkeypatch.setattr(llm_pool_mod, "_read_config", lambda: config)
        monkeypatch.setattr(llm_pool_mod, "_knowledge_spec_model", lambda: "chosen-model")

        async def prompt(text, **kwargs):
            yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=REPLY)

        session = SimpleNamespace(
            kas_registered_agents=[{"id": "kirocrew-lite", "tools": []}],
            kas_projected_agent="kirocrew-lite",
            kas_active_mode_id="",
            destroy=AsyncMock(),
            set_model=AsyncMock(),
            supports_config_option=MagicMock(return_value=False),
            set_config_option=AsyncMock(),
            prompt=prompt,
        )
        sessions = FakeSessions(KAS)
        sessions.get_bg_session = AsyncMock(return_value=session)
        pool = LLMPool(pool_size=1, effort_key="extraction_effort", sessions=lambda: sessions)
        with caplog.at_level(logging.WARNING, logger=session_mod.logger.name):
            try:
                assert await pool.send("chunk 1") == REPLY
                assert await pool.send("chunk 2") == REPLY
            finally:
                await pool.shutdown()
        # Each chunk ran on its own session; only the first asked about effort.
        assert session.destroy.await_count == 2
        session.supports_config_option.assert_called_once()
        session.set_config_option.assert_not_awaited()
        warnings = [r for r in caplog.records if "unsupported" in r.getMessage()]
        assert len(warnings) == 1


class TestExtractionOnKas:
    @pytest.mark.asyncio
    async def test_extraction_runs_on_kas_and_produces_entities(
        self, toolless, oneliner, no_acp_client
    ):
        sessions = FakeSessions(KAS)
        pool = LLMPool(pool_size=2, sessions=lambda: sessions)
        try:
            results = await EntityExtractor(pool=pool).extract_batch(
                ["DynamoDB stores items.", "DynamoDB has tables."]
            )
        finally:
            await pool.shutdown()
        assert [r["entities"][0]["name"] for r in results] == ["DynamoDB", "DynamoDB"]
        assert all(isinstance(w, SessionWorker) for w in pool._workers) or not pool._workers
        no_acp_client.assert_not_called()
        assert len(oneliner.calls) == 2
        for call in oneliner.calls:
            assert call["sessions"] is sessions
            assert call["sel_source"] == session_mod.SESSION_WORKER_SEL_SOURCE
            # The knowledge agent's system prompt leads, the untrusted chunk follows.
            assert call["prompt"].startswith("You are a knowledge extraction specialist")
            assert "UNTRUSTED_CHUNK_" in call["prompt"]
            # Bulk background work: the memory gate stays on.
            assert "memory_gate" not in call
            # The session itself is judged before any text is sent on it.
            assert isinstance(call["confirm_session"].func.__self__, SessionWorker)

    @pytest.mark.asyncio
    async def test_the_pool_builds_session_workers_on_kas(self, toolless, no_acp_client):
        sessions = FakeSessions(KAS)
        pool = LLMPool(pool_size=3, sessions=lambda: sessions)
        try:
            await pool.start()
            assert len(pool._workers) == 3
            assert all(isinstance(w, SessionWorker) for w in pool._workers)
        finally:
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_kiro_pick_keeps_the_acp_client_worker(self, monkeypatch, toolless):
        monkeypatch.setattr(
            llm_pool_mod, "_read_config", lambda: {"agent": {"acp_backend": "kiro"}}
        )
        started = []

        async def _start(self):
            started.append(self._acp_backend)

        monkeypatch.setattr(AcpWorker, "start", _start)
        pool = LLMPool(pool_size=1, sessions=lambda: FakeSessions(KAS))
        try:
            await pool.start()
            assert isinstance(pool._workers[0], AcpWorker)
            assert started == [KIRO]
        finally:
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_the_pool_width_bounds_how_many_chunks_run_at_once(self, toolless, oneliner):
        oneliner.gate = asyncio.Event()
        pool = LLMPool(pool_size=2, sessions=lambda: FakeSessions(KAS))
        batch = asyncio.create_task(pool.send_batch([f"chunk {i}" for i in range(6)]))
        try:
            await async_wait_until(
                lambda: oneliner.running == 2,
                timeout=default_timeout(),
                describe=lambda: f"two gated chunks running: {oneliner.running=}",
            )
            for _ in range(3):
                await asyncio.sleep(0)
            assert oneliner.running == 2
            oneliner.gate.set()
            replies = await asyncio.wait_for(batch, timeout=default_timeout())
        finally:
            oneliner.gate.set()
            await asyncio.wait_for(pool.shutdown(), timeout=default_timeout())
            await asyncio.wait_for(
                asyncio.gather(batch, return_exceptions=True), timeout=default_timeout()
            )
        assert replies == [REPLY] * 6
        assert oneliner.max_running == 2
        assert oneliner.destroyed == 6

    @pytest.mark.asyncio
    @pytest.mark.parametrize("typed", [False, True], ids=["sign-in-text", "acp-auth-required"])
    async def test_signed_out_kas_fails_the_ingest_with_the_sign_in_step(
        self, monkeypatch, toolless, typed
    ):
        from kiro_crew.acp.client import AcpAuthRequired

        # KAS's own words when Kiro is not signed in, as the ACP error formatter
        # puts them on the turn's error (seen live on a signed-out host); or the
        # typed error the ACP client raises, whose text names no sign-in.
        signed_out = (
            AcpAuthRequired("")
            if typed
            else RuntimeError(
                "Not signed in to Kiro. Sign in from Settings → Kiro account, then send "
                "your message again. Switching models will not help — this is a sign-in "
                "issue, not a backend error."
            )
        )

        async def _refuse(sessions, prompt, **kwargs):
            raise signed_out

        monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _refuse)
        pool = LLMPool(pool_size=2, sessions=lambda: FakeSessions(KAS))
        try:
            with pytest.raises(KnowledgeKasSignedOut) as caught:
                await EntityExtractor(pool=pool).extract_batch(["a", "b", "c"])
        finally:
            await pool.shutdown()
        # The batch stops instead of storing three chunks with no entities.
        assert isinstance(caught.value, KnowledgeHarnessUnavailable)
        assert "kirocrew kas login" in str(caught.value)
        assert "nothing was added" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_memory_critical_host_fails_the_batch_instead_of_storing_it_empty(
        self, monkeypatch, toolless
    ):
        from kiro_crew.llm_helpers import BackgroundAdmissionRefused

        async def _refuse(sessions, prompt, **kwargs):
            raise BackgroundAdmissionRefused("background helper 'x' not started: critical")

        monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _refuse)
        pool = LLMPool(pool_size=2, sessions=lambda: FakeSessions(KAS))
        try:
            with pytest.raises(KnowledgeHostMemoryCritical) as caught:
                await EntityExtractor(pool=pool).extract_batch(["a", "b", "c"])
        finally:
            await pool.shutdown()
        assert isinstance(caught.value, KnowledgeHarnessUnavailable)
        assert "low on memory" in str(caught.value)

    @pytest.mark.asyncio
    async def test_any_other_failure_stays_a_failed_chunk(self, monkeypatch, toolless):
        async def _fail(sessions, prompt, **kwargs):
            raise RuntimeError("model overloaded")

        monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _fail)
        pool = LLMPool(pool_size=1, sessions=lambda: FakeSessions(KAS))
        try:
            results = await EntityExtractor(pool=pool).extract_batch(["a"])
        finally:
            await pool.shutdown()
        assert results[0]["entities"] == []


class TestZeroTools:
    @pytest.mark.asyncio
    async def test_an_unconfirmed_spec_sends_no_document_text(self, monkeypatch, oneliner):
        monkeypatch.setattr(session_mod, "_lite_spec_toolless", _answers(False, False))
        pool = LLMPool(pool_size=1, sessions=lambda: FakeSessions(KAS))
        try:
            with pytest.raises(KnowledgeSpecUnconfirmed):
                await pool.send_batch(["secret document text"])
        finally:
            await pool.shutdown()
        assert oneliner.calls == []

    @pytest.mark.asyncio
    async def test_a_spec_that_turns_unconfirmed_mid_ingest_is_refused(self, monkeypatch, oneliner):
        monkeypatch.setattr(session_mod, "_lite_spec_toolless", _answers(True, False))
        worker = SessionWorker(FakeSessions(KAS), backend=KAS)
        await worker.start()
        with pytest.raises(KnowledgeSpecUnconfirmed):
            await worker.send_message("secret document text")
        assert oneliner.calls == []
        # Reported dead, so the pool replaces it and the replacement starts the cooldown.
        assert not worker.is_alive()

    def test_the_installed_lite_spec_is_judged_as_kas_is_sent_it(self, monkeypatch):
        seen = []

        def verdict(agent, work_dir=None, *, backend=None):
            seen.append((agent, backend))
            return True

        monkeypatch.setattr(
            "kiro_crew.agent_sdk.agent_specs.authored_spec_toolless_verdict", verdict
        )
        assert session_mod._lite_spec_verdict(KAS) is True
        assert seen == [("kirocrew-lite", KAS)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("first", "second", "repairs", "expected"),
        [
            (True, None, 0, True),
            (False, True, 1, True),
            (False, False, 1, False),
            (None, None, 0, False),
        ],
        ids=["clean", "repaired", "still-mounts-after-repair", "missing"],
    )
    async def test_a_spec_that_mounts_something_is_repaired_once_or_refused(
        self, monkeypatch, first, second, repairs, expected
    ):
        answers = iter([first, second])
        monkeypatch.setattr(session_mod, "_lite_spec_verdict", lambda backend: next(answers))
        repaired = []

        async def _repair(sessions):
            repaired.append(sessions)

        monkeypatch.setattr(
            "kiro_crew.llm_helpers_parts.background_turns.repair_lite_spec", _repair
        )
        sessions = FakeSessions(KAS)
        assert await session_mod._lite_spec_toolless(sessions, KAS) is expected
        assert repaired == [sessions] * repairs

    @pytest.mark.asyncio
    async def test_the_repair_is_counted_and_resets_the_background_sessions(self, monkeypatch):
        # The background turns' own repair: a turn whose pre-spawn read raced it sees
        # the rewrite count move, and _bg/_consolidate are reset off the old file.
        from kiro_crew.llm_helpers_parts import background_turns

        answers = iter([False, True])
        monkeypatch.setattr(session_mod, "_lite_spec_verdict", lambda backend: next(answers))
        monkeypatch.setattr("kiro_crew.agent.repair_lite_agent_spec", lambda: True)
        reset = []

        async def _reset(sessions, keys, failed):
            reset.append(tuple(keys))

        monkeypatch.setattr("kiro_crew.agent.reset_after_spec_repair", _reset)
        begun = background_turns._lite_rewrites["begun"]
        assert await session_mod._lite_spec_toolless(FakeSessions(KAS), KAS) is True
        assert background_turns._lite_rewrites["begun"] == begun + 1
        assert background_turns._lite_rewrites["running"] == 0
        assert reset == [("_bg", "_consolidate")]

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            # What a live kirocrew-lite session on KAS registered (seen on a test host).
            ({"id": "kirocrew-lite", "tools": []}, True),
            ({"id": "kirocrew-lite", "tools": ["fs_read"]}, False),
            ({"id": "kirocrew-lite", "tools": "*"}, False),
            ({"id": "kirocrew-lite", "tools": [], "mcpServers": {"x": {"command": "x"}}}, False),
            (
                {
                    "id": "kirocrew-lite",
                    "tools": [],
                    "permissions": {"rules": [{"capability": "shell", "effect": "allow"}]},
                },
                False,
            ),
            ({"id": "kirocrew-lite", "tools": [], "_crewAnsweredBuiltins": ["fs_read"]}, False),
        ],
        ids=["no-tools", "a-tool", "all-tools", "a-server", "an-allow", "a-crew-answered"],
    )
    def test_the_session_is_judged_by_what_it_registered(self, entry, expected):
        session = MagicMock(spec=[])
        session.kas_registered_agents = [entry]
        session.kas_projected_agent = "kirocrew-lite"
        session.kas_active_mode_id = ""
        assert session_mod._registered_entry_mounts_nothing(session) is expected

    def test_a_session_with_no_registered_agent_is_not_confirmed(self):
        assert session_mod._registered_entry_mounts_nothing(MagicMock(spec=[])) is False

    @pytest.mark.asyncio
    async def test_a_session_that_registered_a_tool_is_sent_no_document_text(
        self, monkeypatch, toolless
    ):
        # The file check passed, but the spec changed before session/new.
        session = MagicMock(spec=[])
        session.kas_registered_agents = [{"id": "kirocrew-lite", "tools": ["shell"]}]
        session.kas_projected_agent = "kirocrew-lite"
        session.kas_active_mode_id = ""
        sent = []

        async def _oneliner(sessions, prompt, **kwargs):
            await kwargs["confirm_session"](session)
            sent.append(prompt)
            return REPLY

        monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _oneliner)
        worker = SessionWorker(FakeSessions(KAS), backend=KAS)
        await worker.start()
        with pytest.raises(KnowledgeSpecUnconfirmed):
            await worker.send_message("secret document text")
        assert sent == []
        assert not worker.is_alive()

    def test_an_unreadable_spec_is_not_confirmed(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("unreadable")

        monkeypatch.setattr("kiro_crew.agent_sdk.agent_specs.authored_spec_toolless_verdict", boom)
        assert session_mod._lite_spec_verdict(KAS) is None


def _counted_spec_check(monkeypatch, answers):
    """Patch ``_lite_spec_toolless`` to answer from *answers*; return the call log."""
    checks: list[str] = []

    async def _fake(sessions, backend):
        checks.append(backend)
        return answers()

    monkeypatch.setattr(session_mod, "_lite_spec_toolless", _fake)
    return checks


def _mounting_session():
    """A KAS session whose registered ``kirocrew-lite`` entry mounts a tool."""
    session = MagicMock(spec=[])
    session.kas_registered_agents = [{"id": "kirocrew-lite", "tools": ["shell"]}]
    session.kas_projected_agent = "kirocrew-lite"
    session.kas_active_mode_id = ""
    return session


class TestASpecRefusalStartsTheCooldown:
    """A refusal during a call holds the pool for SPEC_REFUSAL_COOLDOWN_SECS, as it says."""

    @pytest.mark.asyncio
    async def test_a_spec_that_turns_unconfirmed_holds_the_pool(self, monkeypatch, oneliner):
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock()
        clock.install(monkeypatch, llm_pool_mod)
        state = {"toolless": True}
        checks = _counted_spec_check(monkeypatch, lambda: state["toolless"])
        # One manager, as the gateway's getter returns.
        sessions = FakeSessions(KAS)
        pool = LLMPool(pool_size=1, sessions=lambda: sessions)
        try:
            await pool.start()
            state["toolless"] = False
            with pytest.raises(KnowledgeSpecUnconfirmed):
                await pool.send("chunk 1")
            assert len(checks) == 2
            for chunk in ("chunk 2", "chunk 3"):
                with pytest.raises(KnowledgeSpecUnconfirmed, match="tries again after 600"):
                    await pool.send(chunk)
            # Neither later send judged the spec again or opened a session.
            assert len(checks) == 2
            assert oneliner.calls == []

            # The window passes and the spec is clean again: the pool starts afresh.
            state["toolless"] = True
            clock.advance(llm_pool_mod.SPEC_REFUSAL_COOLDOWN_SECS + 1)
            assert await pool.send("chunk 4") == REPLY
            assert len(oneliner.calls) == 1
        finally:
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_a_session_that_registered_a_tool_holds_the_pool(self, monkeypatch, toolless):
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock()
        clock.install(monkeypatch, llm_pool_mod)
        checks = _counted_spec_check(monkeypatch, lambda: True)
        opened = []
        sent = []

        async def _oneliner(sessions, prompt, **kwargs):
            opened.append(prompt)
            await kwargs["confirm_session"](_mounting_session())
            sent.append(prompt)
            return REPLY

        monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _oneliner)
        # One manager, as the gateway's getter returns.
        sessions = FakeSessions(KAS)
        pool = LLMPool(pool_size=1, sessions=lambda: sessions)
        try:
            with pytest.raises(KnowledgeSpecUnconfirmed):
                await pool.send("chunk 1")
            assert len(opened) == 1
            checks_after_refusal = len(checks)
            for chunk in ("chunk 2", "chunk 3"):
                with pytest.raises(KnowledgeSpecUnconfirmed, match="tries again after 600"):
                    await pool.send(chunk)
            # No new session was opened and the spec was not judged again.
            assert len(opened) == 1
            assert len(checks) == checks_after_refusal
            assert sent == []
        finally:
            await pool.shutdown()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_shutdown_ends_calls_in_flight_and_their_sessions(self, toolless, oneliner):
        oneliner.gate = asyncio.Event()
        pool = LLMPool(pool_size=2, sessions=lambda: FakeSessions(KAS))
        batch = asyncio.ensure_future(pool.send_batch(["a", "b"]))
        await async_wait_until(
            lambda: oneliner.running == 2,
            timeout=default_timeout(),
            describe=lambda: f"two calls in flight before shutdown: {oneliner.running=}",
        )
        assert oneliner.running == 2
        try:
            await asyncio.wait_for(pool.shutdown(), timeout=default_timeout())
            with pytest.raises(llm_pool_mod.LLMPoolClosed):
                await asyncio.wait_for(batch, timeout=default_timeout())
            assert oneliner.running == 0
            assert oneliner.destroyed == 2
        finally:
            oneliner.gate.set()

    @pytest.mark.asyncio
    async def test_a_cancelled_caller_leaves_the_call_to_finish_and_clean_up(
        self, toolless, oneliner
    ):
        oneliner.gate = asyncio.Event()
        worker = SessionWorker(FakeSessions(KAS), backend=KAS)
        await worker.start()
        send = asyncio.ensure_future(worker.send_message("chunk"))
        await async_wait_until(
            lambda: oneliner.running == 1,
            timeout=default_timeout(),
            describe=lambda: f"call in flight before cancellation: {oneliner.running=}",
        )
        try:
            send.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(send, timeout=default_timeout())
            # Shielded: the acquisition is never cut mid-way; the call ends on its own.
            assert oneliner.running == 1
            oneliner.gate.set()
            await async_wait_until(
                lambda: not worker._calls,
                timeout=default_timeout(),
                describe=lambda: f"shielded call cleaned up: {len(worker._calls)=}",
            )
            assert oneliner.destroyed == 1
            assert not worker._calls
        finally:
            oneliner.gate.set()

    @pytest.mark.asyncio
    async def test_a_worker_whose_backend_changed_is_replaced(self, toolless, oneliner):
        sessions = FakeSessions(KAS)
        pool = LLMPool(pool_size=1, sessions=lambda: sessions)
        try:
            await pool.start()
            first = pool._workers[0]
            sessions.backend = KIRO
            assert not first.is_alive()
            with patch.object(AcpWorker, "start", autospec=True) as start:
                monkey_config = {"agent": {"acp_backend": "kiro"}}
                with patch.object(llm_pool_mod, "_read_config", lambda: monkey_config):
                    _idx, worker = await pool.acquire()
                    pool.release(_idx)
            assert isinstance(worker, AcpWorker)
            start.assert_called_once()
        finally:
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_a_closed_worker_refuses(self, toolless, oneliner):
        worker = SessionWorker(FakeSessions(KAS), backend=KAS)
        await worker.start()
        await worker.shutdown()
        assert not worker.is_alive()
        with pytest.raises(llm_pool_mod.LLMPoolClosed):
            await worker.send_message("chunk")
        assert oneliner.calls == []


class ClosingSessions(FakeSessions):
    """A session manager that ``close_all()`` has begun closing (admission closed)."""

    def __init__(self) -> None:
        super().__init__(KAS)
        self.admission_closed = False


def _refused_while_closing(sessions):
    """A ``run_bg_oneliner`` stand-in that closes *sessions*, then refuses as it does."""
    from kiro_crew.session_allocation_parts.errors import SessionClosingError

    async def _oneliner(_sessions, prompt, **kwargs):
        sessions.admission_closed = True
        raise SessionClosingError("Session manager is closing")

    return _oneliner


class TestAGatewayExitFailsTheIngest:
    """``close_all()`` refuses background sessions before the pool's own shutdown()."""

    @pytest.mark.asyncio
    async def test_a_refusal_while_closing_closes_the_batch(self, monkeypatch, toolless):
        sessions = ClosingSessions()
        monkeypatch.setattr(
            "kiro_crew.llm_helpers.run_bg_oneliner", _refused_while_closing(sessions)
        )
        pool = LLMPool(pool_size=1, sessions=lambda: sessions)
        try:
            # One chunk: the call's own refusal decides, not a later call's check.
            with pytest.raises(llm_pool_mod.LLMPoolClosed):
                await EntityExtractor(pool=pool).extract_batch(["a"])
        finally:
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_the_check_before_each_call_refuses_while_closing(self, toolless, oneliner):
        sessions = ClosingSessions()
        worker = SessionWorker(sessions, backend=KAS)
        await worker.start()
        sessions.admission_closed = True
        with pytest.raises(llm_pool_mod.LLMPoolClosed):
            await worker.send_message("chunk")
        assert oneliner.calls == []

    def test_only_a_true_flag_reads_as_closing(self):
        assert session_mod._sessions_closing(FakeSessions(KAS)) is False
        assert session_mod._sessions_closing(MagicMock()) is False

    @pytest.mark.asyncio
    async def test_nothing_is_stored_or_hashed(self, monkeypatch, toolless, tmp_path):
        from kiro_crew.knowledge.chunker import HeadingAwareChunker
        from kiro_crew.knowledge.ingestion import IngestionPipeline
        from kiro_crew.knowledge.readers import FileReader
        from kiro_crew.knowledge.store import KnowledgeStore

        sessions = ClosingSessions()
        monkeypatch.setattr(
            "kiro_crew.llm_helpers.run_bg_oneliner", _refused_while_closing(sessions)
        )
        store = KnowledgeStore(str(tmp_path / "knowledge.db"))
        pool = LLMPool(pool_size=2, sessions=lambda: sessions)
        pipeline = IngestionPipeline(
            store=store,
            extractor=EntityExtractor(pool=pool),
            chunker=HeadingAwareChunker(),
            reader=FileReader(),
            embedder=None,
            dedup_enabled=False,
        )
        doc = tmp_path / "doc.md"
        doc.write_text("# DynamoDB\n\nDynamoDB stores items in tables.", encoding="utf-8")
        try:
            with pytest.raises(llm_pool_mod.LLMPoolClosed):
                await pipeline.ingest_file(str(doc))
            source = store.get_source_by_uri(str(doc.resolve()))
            job = store.db.execute(
                "SELECT status FROM ingestion_jobs WHERE source_id = ? ORDER BY rowid DESC",
                (source["id"],),
            ).fetchone()
            assert job["status"] == "failed"
            items = store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source["id"],)
            ).fetchall()
            assert items == []
            props = store.db.execute(
                "SELECT properties FROM sources WHERE id = ?", (source["id"],)
            ).fetchone()
            assert "content_hash" not in json.loads(props["properties"] or "{}")
            assert list(store.db.execute("SELECT name FROM entities")) == []
        finally:
            await pool.shutdown()
            store.close()


class TestAMemoryCriticalRefusalFailsTheIngest:
    """The memory gate's refusal reaches the ingest like the sign-in refusal does."""

    @pytest.mark.asyncio
    async def test_nothing_is_stored_or_hashed_and_a_retry_extracts_in_full(
        self, monkeypatch, toolless, tmp_path
    ):
        from kiro_crew.knowledge.chunker import HeadingAwareChunker
        from kiro_crew.knowledge.ingestion import IngestionPipeline
        from kiro_crew.knowledge.readers import FileReader
        from kiro_crew.knowledge.store import KnowledgeStore
        from kiro_crew.llm_helpers import BackgroundAdmissionRefused

        critical = {"on": True}

        async def _oneliner(sessions, prompt, **kwargs):
            if critical["on"]:
                raise BackgroundAdmissionRefused("background helper 'x' not started: critical")
            return REPLY

        monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _oneliner)
        store = KnowledgeStore(str(tmp_path / "knowledge.db"))
        pool = LLMPool(pool_size=2, sessions=lambda: FakeSessions(KAS))
        pipeline = IngestionPipeline(
            store=store,
            extractor=EntityExtractor(pool=pool),
            chunker=HeadingAwareChunker(),
            reader=FileReader(),
            embedder=None,
            dedup_enabled=False,
        )
        doc = tmp_path / "doc.md"
        doc.write_text("# DynamoDB\n\nDynamoDB stores items in tables.", encoding="utf-8")
        try:
            with pytest.raises(KnowledgeHostMemoryCritical):
                await pipeline.ingest_file(str(doc))
            source = store.get_source_by_uri(str(doc.resolve()))
            job = store.db.execute(
                "SELECT status, error FROM ingestion_jobs WHERE source_id = ? "
                "ORDER BY rowid DESC",
                (source["id"],),
            ).fetchone()
            assert job["status"] == "failed"
            assert "low on memory" in job["error"]
            items = store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source["id"],)
            ).fetchall()
            assert items == []
            props = store.db.execute(
                "SELECT properties FROM sources WHERE id = ?", (source["id"],)
            ).fetchone()
            assert "content_hash" not in json.loads(props["properties"] or "{}")

            # Memory frees up: the unchanged document is extracted, not skipped.
            critical["on"] = False
            assert await pipeline.ingest_file(str(doc)) is not None
            # This store holds only this document, so every entity came from it.
            names = {row["name"] for row in store.db.execute("SELECT name FROM entities")}
            assert "DynamoDB" in names
        finally:
            await pool.shutdown()
            store.close()

"""The gateway's boot prewarm of the speech model, and everything it must not do.

A pointer-down on the microphone is too late to prepare the model: the digest
verification and the native load sit in front of the first utterance's own decode, so
a user who says a short phrase and stops is still waiting on them after they stop
speaking. Measured on a 32-core aarch64 CPU build, first-dictation latency is 1.36 s
without a resident model against 0.66 s with one, and 4.39 s against 2.44 s for
``small``; the saving is the hash plus the load, not the decode, which happens either
way.

Most of these tests are about RESTRAINT rather than about warming. A boot-time task
that runs on every gateway start has more ways to do harm than good: downloading
1.6 GB nobody asked for, stalling the event loop during boot, or taking the gateway
down because a model was absent. Each of those has a test here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import server as server_mod


@pytest.fixture
def warm(monkeypatch: pytest.MonkeyPatch):
    """Drive ``_stt_startup_prewarm`` with the boot delay removed and calls recorded."""
    monkeypatch.setattr(server_mod, "_STT_PREWARM_BOOT_DELAY_SECS", 0.0)
    calls: dict[str, object] = {"prewarm": [], "imported": 0, "bounds": []}

    cfg = MagicMock()
    cfg.stt.enabled = True
    cfg.stt.provider = "local"
    cfg.stt.model = "base"
    cfg.stt.language_code = "zh-CN"
    cfg.stt.idle_evict_secs = 600
    cfg.stt.timeout_secs = 300
    monkeypatch.setattr(server_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))

    engine_stub = MagicMock()
    engine_stub.shared_engine = lambda **kw: calls["bounds"].append(kw)  # type: ignore[union-attr]
    engine_stub.WhisperEngine.capabilities.return_value = MagicMock(backend="cpu")

    def _import():
        calls["imported"] = int(calls["imported"]) + 1  # type: ignore[arg-type]
        return engine_stub

    monkeypatch.setattr(server_mod, "_import_stt_engine", _import)
    # Pinned so the memory gate is deterministic rather than a property of whatever
    # host runs the suite. Generous by default; the gate's own cases override it.
    monkeypatch.setattr("kiro_crew.subagent._available_memory_gb", lambda: 62.5)
    return cfg, calls


def _patch_stt(monkeypatch: pytest.MonkeyPatch, calls: dict, *, present: bool, ok: bool = True):
    """Stand in for the two lazily-imported modules the prewarm reaches."""
    from kiro_crew.stt import models as stt_models
    from kiro_crew.stt import session as stt_session

    monkeypatch.setattr(stt_models, "is_present", lambda model: present)

    async def _prewarm(*, model_name: str, language: str):
        calls["prewarm"].append((model_name, language))
        return MagicMock(ok=ok, detail="unavailable")

    # The SUBMODULE, never the package. `kiro_crew/stt/__init__.py` resolves its
    # public names through a PEP 562 `__getattr__`, and its own docstring spells out
    # what patching the package does: `monkeypatch.setattr(stt, "prewarm", ...)`
    # leaves the original behind as a REAL attribute on teardown, which from then on
    # shadows `__getattr__` -- so every later test that patches `stt.session.prewarm`
    # is silently ignored. This file did exactly that, and it broke
    # `test_dashboard_handlers_core_coverage.py::TestSttPrewarm` in any run where the
    # two files shared a worker: order-dependent, silent, and nothing to do with the
    # code under test. The boot prewarm reads `stt.prewarm`, which resolves through
    # `__getattr__` to this same object, so patching here is visible to it.
    monkeypatch.setattr(stt_session, "prewarm", _prewarm)


class TestTheBootPrewarmWarms:
    @pytest.mark.asyncio
    async def test_a_present_model_is_warmed_with_the_configured_language(
        self, warm, monkeypatch: pytest.MonkeyPatch
    ):
        """And the language is reduced to what whisper wants (``zh-CN`` -> ``zh``).

        Routed through the repo's own `_whisper_language` rather than a local
        reduction, so a boot warm and a real session cannot disagree about which
        language the model was loaded for -- they key the same resident context.
        """
        cfg, calls = warm
        _patch_stt(monkeypatch, calls, present=True)
        await server_mod._stt_startup_prewarm()
        assert calls["prewarm"] == [("base", "zh")]

    @pytest.mark.asyncio
    async def test_the_engines_bounds_come_from_config(self, warm, monkeypatch: pytest.MonkeyPatch):
        """The engine is a process singleton, and the FIRST caller sets its bounds.

        Booting without passing them would leave the module defaults in force until
        some later caller happened to supply the operator's real values.
        """
        cfg, calls = warm
        _patch_stt(monkeypatch, calls, present=True)
        await server_mod._stt_startup_prewarm()
        assert calls["bounds"] == [{"idle_evict_secs": 600, "timeout_secs": 300}]


class TestTheBootPrewarmRefrains:
    @pytest.mark.asyncio
    async def test_an_undownloaded_model_is_never_fetched(
        self, warm, monkeypatch: pytest.MonkeyPatch
    ):
        """The most important guard here.

        A gateway that pulled 1.6 GB because it booted would spend a user's bandwidth
        on a feature they have not used. The first-run download stays an explicit
        ``POST /api/stt/prepare``.
        """
        cfg, calls = warm
        _patch_stt(monkeypatch, calls, present=False)
        await server_mod._stt_startup_prewarm()
        assert calls["prewarm"] == []

    @pytest.mark.asyncio
    async def test_speech_disabled_does_not_even_import_the_recogniser(
        self, warm, monkeypatch: pytest.MonkeyPatch
    ):
        """Asserted on the IMPORT, not just on the warm.

        The import pulls numpy and the native binding; a gateway with voice switched
        off must not pay for it at boot.
        """
        cfg, calls = warm
        cfg.stt.enabled = False
        _patch_stt(monkeypatch, calls, present=True)
        await server_mod._stt_startup_prewarm()
        assert calls["imported"] == 0
        assert calls["prewarm"] == []

    @pytest.mark.asyncio
    async def test_a_non_local_provider_warms_nothing(self, warm, monkeypatch: pytest.MonkeyPatch):
        """`apple` and `transcribe` never decode with these weights.

        Loading them would hold 148 MB (1.6 GB at the largest) that the configured
        provider cannot use.
        """
        cfg, calls = warm
        cfg.stt.provider = "transcribe"
        _patch_stt(monkeypatch, calls, present=True)
        await server_mod._stt_startup_prewarm()
        assert calls["imported"] == 0
        assert calls["prewarm"] == []

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_a_host_short_on_memory_is_left_alone(
        self, warm, monkeypatch: pytest.MonkeyPatch
    ):
        """A boot warm is a guess about intent; it must not cost the host its memory.

        `large-v3-turbo` measured 1861 MB resident. On a desktop install the gateway
        restarts with the app, so without this gate an 8 GB machine pays that on every
        launch, for `idle_evict_secs`, whether or not its owner dictates. The
        pointer-down prewarm still covers the case.
        """
        cfg, calls = warm
        import kiro_crew.dashboard.server as server_mod

        _patch_stt(monkeypatch, calls, present=True)
        # The reviewer's actual case: the 1.6 GB model on a machine with 2 GB free.
        cfg.stt.model = "large-v3-turbo"
        monkeypatch.setattr("kiro_crew.subagent._available_memory_gb", lambda: 1.95)
        await server_mod._stt_startup_prewarm()
        assert calls["prewarm"] == []

    @pytest.mark.asyncio
    async def test_a_container_limit_binds_even_when_the_host_is_roomy(
        self, warm, monkeypatch: pytest.MonkeyPatch
    ):
        """The reading has to be the one the kernel will kill against.

        ``/proc/meminfo`` reports the HOST inside a memory-capped container, so a
        1.6 GB model clears a host-wide check and is then OOM-killed against the
        cgroup limit -- and since this runs on every boot, that is a crash loop no
        config change escapes. So the gate asks ``subagent._available_memory_gb``,
        which takes the minimum of the host reading and the tightest visible cgroup
        headroom. Here the host says 64 GB and the cgroup says 1 GB: no warm.
        """
        cfg, calls = warm
        import kiro_crew.dashboard.server as server_mod

        _patch_stt(monkeypatch, calls, present=True)
        cfg.stt.model = "large-v3-turbo"
        monkeypatch.setattr(server_mod.platform_compat, "host_available_mib", lambda: 64_000)
        monkeypatch.setattr("kiro_crew.subagent._available_memory_gb", lambda: 1.0)
        await server_mod._stt_startup_prewarm()
        assert calls["prewarm"] == []

    @pytest.mark.asyncio
    async def test_an_unreadable_memory_reading_does_not_speculate(
        self, warm, monkeypatch: pytest.MonkeyPatch
    ):
        """A negative reading means the platform could not answer, not that RAM is free.

        Same direction as every other gate here: withhold unless sure.
        """
        cfg, calls = warm
        import kiro_crew.dashboard.server as server_mod

        _patch_stt(monkeypatch, calls, present=True)
        monkeypatch.setattr("kiro_crew.subagent._available_memory_gb", lambda: -1.0)
        await server_mod._stt_startup_prewarm()
        assert calls["prewarm"] == []

    @pytest.mark.asyncio
    async def test_a_failed_warm_is_not_an_error(self, warm, monkeypatch: pytest.MonkeyPatch):
        """Every reason a prewarm fails is a state the gateway is expected to run in."""
        cfg, calls = warm
        _patch_stt(monkeypatch, calls, present=True, ok=False)
        await server_mod._stt_startup_prewarm()  # must not raise
        assert calls["prewarm"] == [("base", "zh")]


class TestTheTaskIsSafeToRunOnEveryBoot:
    def test_the_boot_delay_is_short_but_not_zero(self):
        """Racing a user, but never the listener bind.

        Shorter than the idle sweep's 30 s because the point is to be resident before
        the first dictation, and someone who opens the dashboard to dictate does it
        within seconds. Non-zero so the load cannot compete with boot itself.
        """
        assert 0.0 < server_mod._STT_PREWARM_BOOT_DELAY_SECS < server_mod._STT_SWEEP_BOOT_DELAY_SECS

    def test_a_raised_exception_is_consumed_rather_than_going_unhandled(self):
        """The sweep's callback re-raises; this one must not.

        A dead janitor is a defect. A prewarm that could not run is not, and turning
        it into an unhandled task exception would report a working gateway as broken.
        """
        loop = asyncio.new_event_loop()
        try:
            task: asyncio.Task[None] = loop.create_task(_boom())
            with pytest.raises(RuntimeError):
                loop.run_until_complete(task)
            # The callback reads .exception() without re-raising, which is what marks
            # it retrieved.
            server_mod._log_prewarm_outcome(task)
        finally:
            loop.close()

    def test_a_cancelled_task_is_not_reported_as_a_failure(self):
        """Shutdown cancels it, and shutdown is not an error."""
        loop = asyncio.new_event_loop()
        try:
            task: asyncio.Task[None] = loop.create_task(_forever())
            loop.run_until_complete(asyncio.sleep(0))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                loop.run_until_complete(task)
            server_mod._log_prewarm_outcome(task)
        finally:
            loop.close()


async def _boom() -> None:
    raise RuntimeError("prewarm exploded")


async def _forever() -> None:
    await asyncio.sleep(3600)

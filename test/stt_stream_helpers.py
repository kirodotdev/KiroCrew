"""Socket reads, waits, app and config builders shared by the streaming STT tests.

``test_stt_stream.py`` and its ``_config``, ``_defensive_guards`` and ``_local_session``
siblings import these by name: bounded frame reads, the audit waits, the
``/api/ws/stt`` app, the STT config, the signal-driven duration cap and the
``transcribe_consented`` fixture.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, SttConfig
from kiro_crew.testing.wait import async_wait_until, await_until


async def _recv_json(ws):
    """One JSON frame, under the lost-run bound: a server that never answers fails by name."""
    return await await_until(asyncio.ensure_future(ws.receive_json()), what="the next JSON frame")


async def _wait_for_operation(calls: list[dict], operation: str) -> None:
    """Await *operation* appearing in *calls*, or fail with what did arrive.

    The WS error-frame handshake does NOT order the client's assertion after the
    server's audit. Every early-return path in ``api_ws_stt`` runs
    ``send_json(error)`` -> ``ws.close()`` -> ``_emit_end_audit(...)``, so
    ``receive_json()`` returns on the error frame while the handler still has two
    steps to go. Exiting the ``TestClient`` context is not a barrier either: it
    closes the client side and does not await the server handler's coroutine to
    completion. Asserting on ``calls`` right after either point is therefore a
    race that fails whenever the event loop happens not to resume the handler
    first — reproduced at roughly 1-in-8 locally and seen intermittently on CI.

    Waiting on the real condition removes the guesswork: it returns the instant the
    audit lands and fails naming what did arrive if it never does.
    """

    def audited() -> bool:
        return operation in [c["operation"] for c in calls]

    await async_wait_until(
        audited,
        describe=lambda: f"{operation!r} not audited; got {[c['operation'] for c in calls]}",
    )


async def _await_end_audit(outcomes: list[str]) -> None:
    """Wait for the end audit the handler emits after the client has left.

    Neither receiving the last frame nor leaving ``TestClient`` orders the test after
    the handler's remaining steps (see ``_wait_for_operation``), so a test reads
    ``outcomes`` only once it holds something.
    """
    await async_wait_until(lambda: outcomes, describe=lambda: f"outcomes={outcomes!r}")


def _make_app() -> web.Application:
    from kiro_crew.dashboard import stt_stream

    app = web.Application()
    app.router.add_get("/api/ws/stt", stt_stream.api_ws_stt)
    return app


def _cfg(**kwargs) -> KiroCrewConfig:
    stt = SttConfig(
        enabled=kwargs.pop("enabled", True),
        provider=kwargs.pop("provider", "transcribe"),
        streaming=kwargs.pop("streaming", True),
        **kwargs,
    )
    return KiroCrewConfig(stt=stt)


def _arm_cap(monkeypatch) -> asyncio.Event:
    """Make the duration cap fire on a signal instead of a clock.

    The transport's deadline sleeps `_MAX_STREAM_DURATION_SECS`. Pinning that to a
    sentinel and shimming the module's OWN `asyncio` binding with a delegating copy
    (the pattern `test_stop_budget_covers_the_active_partial_and_the_final` uses for
    the drain budget, minus the stdlib rebinding) lets a test fire the cap exactly
    when the session is in the state under test, instead of racing a real timer
    against loopback I/O: a cap that fires before the client's frame has been read,
    queued and fed has nothing pending and legitimately sends no final. Every other
    coroutine in the worker keeps the real stdlib sleep (tests-are-deterministic,
    item 11: never rebind a stdlib clock).
    """
    from kiro_crew.dashboard import stt_stream

    sentinel = 12_345.0
    fire = asyncio.Event()
    original_sleep = asyncio.sleep

    async def controlled_sleep(delay, result=None):
        if delay == sentinel:
            await await_until(fire, what="the test firing the cap")
            return result
        return await original_sleep(delay, result)

    monkeypatch.setattr(stt_stream, "_MAX_STREAM_DURATION_SECS", sentinel)
    monkeypatch.setattr(
        stt_stream, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": controlled_sleep})
    )
    return fire


@pytest.fixture(
    name="transcribe_consented",
)
def transcribe_consented_fixture(tmp_path_factory, monkeypatch):
    """Record operator consent for Transcribe in a throwaway data home.

    Streaming Transcribe is a paid service, so ``api_ws_stt`` refuses without a
    grant for the configured profile+region. Every class that drives that handler
    needs this; it is module-level rather than copied per class so the two cannot
    drift. Cases that assert the REFUSAL live in ``test_aws_consent.py``.

    Also stubs the live-account check, which would otherwise spawn the AWS CLI --
    these cases are about the stream, not the identity probe.
    """
    home = tmp_path_factory.mktemp("stt-consent-home")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    from kiro_crew import aws_consent
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    cfg = _cfg()
    aws_consent.record_grant(
        aws_consent.SERVICE_TRANSCRIBE,
        profile=cfg.stt.transcribe_profile,
        region=cfg.stt.transcribe_region,
        account="111122223333",
        arn="arn:aws:iam::111122223333:user/test",
        granted_at="2026-08-21T00:00:00+00:00",
    )

    async def _probe(_profile, _region, *, use_cache=True):
        return aws_consent.Identity(ok=True, account="111122223333")

    monkeypatch.setattr(aws_consent, "probe_identity", _probe)

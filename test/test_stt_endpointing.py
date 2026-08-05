"""Tests for ``stt.endpointing`` — semantic end-of-utterance auto-submit.

Two surfaces:

1. Config + API — the flag defaults OFF, round-trips through PUT/GET, only
   accepts a real boolean, and a hand-edited non-bool coerces to the default
   (so a string ``"true"`` can't silently arm auto-submit).
2. ``_Endpointer`` — the debounced, single-flight classifier that emits the
   ``endpoint`` frame. A COMPLETE verdict emits exactly one frame; INCOMPLETE
   emits none; a superseding ``final`` (debounce coalesce) wins; a closed ws
   or a failing background call never sends.

Per-test config isolation comes from the autouse KIROCREW_HOME fixture in
conftest, so these do not take tmp_path themselves.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.core as core
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.dashboard import stt_stream


def _req(method: str, body: dict | None = None):
    req = MagicMock(spec=web.Request)
    req.method = method
    if body is not None:
        req.json = AsyncMock(return_value=body)
    return req


@pytest.fixture(autouse=True)
def _stub_probes(monkeypatch):
    monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    monkeypatch.setattr(core, "is_available", lambda stt: False)


# ── Config + API ────────────────────────────────────────────────────────────


def test_defaults_to_off() -> None:
    """Endpointing (auto-submit) is opt-in, so absent from config it is off."""
    assert KiroCrewConfig.load().stt.endpointing is False


def test_explicit_true_is_honoured() -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stt": {"endpointing": True}}), encoding="utf-8")
    assert KiroCrewConfig.load().stt.endpointing is True


def test_non_bool_in_config_file_falls_back_to_off() -> None:
    """A hand-edited config.json can hold any JSON type; the loader must coerce
    a non-bool to the OFF default rather than arm auto-submit on a truthy
    string."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for bogus in ("true", "false", 1, 0, None, [], {}):
        path.write_text(json.dumps({"stt": {"endpointing": bogus}}), encoding="utf-8")
        loaded = KiroCrewConfig.load().stt.endpointing
        assert loaded is False, f"{bogus!r} should fall back to off, got {loaded!r}"


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    resp = await core.api_stt_config(_req("PUT", {"endpointing": True}))
    assert resp.status == 200
    assert json.loads(resp.body)["endpointing"] is True

    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["endpointing"] is True

    resp = await core.api_stt_config(_req("PUT", {"endpointing": False}))
    assert json.loads(resp.body)["endpointing"] is False


@pytest.mark.asyncio
async def test_get_exposes_the_flag() -> None:
    resp = await core.api_stt_config(_req("GET"))
    assert resp.status == 200
    assert "endpointing" in json.loads(resp.body)


@pytest.mark.asyncio
async def test_non_boolean_is_rejected_by_put() -> None:
    await core.api_stt_config(_req("PUT", {"endpointing": True}))
    resp = await core.api_stt_config(_req("PUT", {"endpointing": "yes"}))
    assert resp.status == 200
    # Unchanged by the bogus write.
    assert json.loads(resp.body)["endpointing"] is True


@pytest.mark.asyncio
async def test_put_does_not_disturb_sibling_stt_fields() -> None:
    await core.api_stt_config(_req("PUT", {"language_code": "fr-FR", "streaming": True}))
    await core.api_stt_config(_req("PUT", {"endpointing": True}))
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["language_code"] == "fr-FR"
    assert data["stt"]["streaming"] is True
    assert data["stt"]["endpointing"] is True


# ── _Endpointer ──────────────────────────────────────────────────────────────


def _fake_ws(*, closed: bool = False):
    ws = MagicMock()
    ws.closed = closed
    ws.send_json = AsyncMock()
    return ws


async def _drain(ep: "stt_stream._Endpointer") -> None:
    """Await the classification tasks scheduled by note_final to completion.

    aclose() would CANCEL them; here we want their natural finish so the send
    (or its absence) is observable. Snapshot before awaiting — the done callback
    discards each task from the set as it finishes.
    """
    import asyncio

    tasks = list(ep._tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_complete_verdict_emits_endpoint_frame(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.stt_stream.run_bg_oneliner",
        AsyncMock(return_value="COMPLETE"),
    )
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=0.0, timeout=1.0)
    ep.note_final("deploy the service to production")
    await _drain(ep)
    ws.send_json.assert_awaited_once_with({"type": "endpoint", "complete": True})


@pytest.mark.asyncio
async def test_incomplete_verdict_emits_nothing(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.stt_stream.run_bg_oneliner",
        AsyncMock(return_value="INCOMPLETE"),
    )
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=0.0, timeout=1.0)
    ep.note_final("so i was thinking that maybe we could")
    await _drain(ep)
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_newer_final_supersedes_the_debounced_one(monkeypatch) -> None:
    """Two finals in quick succession: the earlier task must abort after its
    debounce (gen bumped), so exactly one classification runs and one frame is
    emitted — not two."""
    bg = AsyncMock(return_value="COMPLETE")
    monkeypatch.setattr("kiro_crew.dashboard.stt_stream.run_bg_oneliner", bg)
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=0.02, timeout=1.0)
    ep.note_final("first part")
    ep.note_final("first part and the rest")
    await _drain(ep)
    assert bg.await_count == 1
    ws.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_ws_never_sends(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.stt_stream.run_bg_oneliner",
        AsyncMock(return_value="COMPLETE"),
    )
    ws = _fake_ws(closed=True)
    ep = stt_stream._Endpointer(ws, object(), debounce=0.0, timeout=1.0)
    ep.note_final("all done now")
    await _drain(ep)
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_failure_is_swallowed(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.dashboard.stt_stream.run_bg_oneliner",
        AsyncMock(side_effect=RuntimeError("model unavailable")),
    )
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=0.0, timeout=1.0)
    ep.note_final("whatever")
    await _drain(ep)  # must not raise
    ws.send_json.assert_not_awaited()


async def _drain_until_idle(ep: "stt_stream._Endpointer") -> None:
    """Await all scheduled tasks, including ones a completing task re-schedules
    (the single-flight latch), until the endpointer is quiescent.

    Yields each iteration so a completed task's ``discard`` done-callback (queued
    via ``call_soon``) actually runs — otherwise a done-but-not-yet-removed task
    lingers in ``_tasks`` forever and this never sees idle."""
    import asyncio

    for _ in range(200):
        await asyncio.sleep(0)  # let done-callbacks (self._tasks.discard) run
        live = [t for t in ep._tasks if not t.done()]
        if not live:
            return
        await asyncio.gather(*live, return_exceptions=True)
    raise AssertionError("endpointer did not reach idle")


@pytest.mark.asyncio
async def test_partial_invalidates_pending_verdict(monkeypatch) -> None:
    """A partial arriving after a final (user resumed speaking) must invalidate
    the scheduled verdict so a stale COMPLETE cannot auto-submit a truncated
    request."""
    bg = AsyncMock(return_value="COMPLETE")
    monkeypatch.setattr("kiro_crew.dashboard.stt_stream.run_bg_oneliner", bg)
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=0.02, timeout=1.0)
    ep.note_final("deploy the service")     # schedules gen 1
    ep.note_partial("deploy the service to")  # user keeps talking -> gen 2, no schedule
    await _drain_until_idle(ep)
    # The gen-1 task wakes to find gen advanced, so it never classifies or sends.
    assert bg.await_count == 0
    ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_flight_collision_reruns_not_drops(monkeypatch) -> None:
    """When a second final collides with an in-flight classification, the second
    must be latched and re-run (not dropped) so the terminal final still yields
    an endpoint frame."""
    import asyncio

    release = asyncio.Event()
    calls: list[str] = []

    async def fake(_sessions, prompt, **_kw):
        calls.append(prompt)
        if len(calls) == 1:
            await release.wait()  # hold the first call in-flight
        return "COMPLETE"

    monkeypatch.setattr("kiro_crew.dashboard.stt_stream.run_bg_oneliner", fake)
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=0.0, timeout=1.0)

    async def _yield_until(pred, label):
        for _ in range(100):
            if pred():
                return
            await asyncio.sleep(0)
        raise AssertionError(f"condition never held: {label}")

    ep.note_final("first")            # task gen 1
    await _yield_until(lambda: ep._inflight, "gen-1 reaches run_bg (in-flight)")
    ep.note_final("second")           # gen 2, collides with in-flight gen 1
    await _yield_until(lambda: ep._pending is not None, "gen-2 latches _pending")
    release.set()                     # gen-1 call returns -> re-schedules gen 2
    await _drain_until_idle(ep)
    assert len(calls) == 2            # in-flight gen 1 + re-run gen 2
    ws.send_json.assert_awaited_once_with({"type": "endpoint", "complete": True})


@pytest.mark.asyncio
async def test_empty_final_does_not_invalidate_pending(monkeypatch) -> None:
    """An empty final must be ignored entirely — it must not bump the generation
    and strand a good pending verdict."""
    bg = AsyncMock(return_value="COMPLETE")
    monkeypatch.setattr("kiro_crew.dashboard.stt_stream.run_bg_oneliner", bg)
    ws = _fake_ws()
    ep = stt_stream._Endpointer(ws, object(), debounce=0.02, timeout=1.0)
    ep.note_final("turn on the lights")  # schedules gen 1
    ep.note_final("")                    # empty: must NOT bump gen or schedule
    await _drain_until_idle(ep)
    assert bg.await_count == 1
    ws.send_json.assert_awaited_once_with({"type": "endpoint", "complete": True})

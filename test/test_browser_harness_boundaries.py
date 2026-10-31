"""The browser harness boundaries: frame URL gate, metadata redaction, install cancel."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_harness import frames, install, live_hub, ops
from kiro_crew.dashboard.handlers.browser_harness_status import stop_browser_harness_install
from kiro_crew.testing.wait import async_wait_until, wait_until


class FrameRelay:
    def __init__(self, child_session: bool) -> None:
        self.child_session = child_session
        self.url = "http://169.254.169.254/latest/meta-data/"
        self.reads: list[str] = []

    async def send(self, method: str, params: dict[str, Any], sid: str) -> Any:
        if method == "Page.getFrameTree":
            frame = {"id": "child", "url": self.url}
            return (
                {"frameTree": {"frame": frame}}
                if sid == "CHILD"
                else {"frameTree": {"frame": {"id": "top"}, "childFrames": [{"frame": frame}]}}
            )
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            self.reads.append(params["expression"])
            return {"result": {"value": {"nodes": [{"name": "private canary", "ref": "e1"}]}}}
        raise AssertionError(method)


def frame_ops(child_session: bool) -> tuple[FrameRelay, frames.FrameOps]:
    relay = FrameRelay(child_session)
    registry = frames.FrameRegistry()
    registry.parent = "TOP"
    registry.tag_of("child")
    if child_session:
        registry.handle(
            {
                "sessionId": "TOP",
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "CHILD",
                    "targetInfo": {"type": "iframe", "targetId": "child"},
                },
            }
        )
    else:
        registry.local.add("child")
    return relay, frames.FrameOps(relay, "TOP", registry, "WALKER")


@pytest.mark.asyncio
@pytest.mark.parametrize("child_session", [True, False])
async def test_refused_child_frame_outline_is_never_read(child_session: bool, monkeypatch) -> None:
    relay, frame = frame_ops(child_session)

    async def slots(_count: int) -> list[str]:
        return ["child"]

    monkeypatch.setattr(frame, "slot_frames", slots)
    ctx = ops.OpContext(
        helper=unused_helper, frames=lambda: frame, url_allowed=lambda u: u.startswith("https://")
    )
    nodes = await ops._with_frames(ctx, [{"role": "iframe", "slot": 0}])
    assert nodes == [{"role": "iframe", "state": {"unread": True}}]
    assert relay.reads == []


async def unused_helper(name: str, **kwargs: Any) -> Any:
    return {"url": "https://example.com"} if name == "current_tab" else None


@pytest.mark.asyncio
@pytest.mark.parametrize("child_session", [True, False])
async def test_frame_ref_rechecks_url_after_navigation(child_session: bool) -> None:
    relay, frame = frame_ops(child_session)
    ctx = ops.OpContext(
        helper=unused_helper, frames=lambda: frame, url_allowed=lambda u: u.startswith("https://")
    )
    result = await ops.run_op("click", {"ref": "f1e1"}, ctx)
    assert result["code"] == "redirect_refused"
    assert not result["ok"]
    assert relay.reads == []


@pytest.mark.asyncio
async def test_live_metadata_redacts_every_text_field_at_publish_boundary() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    exfil = "https://evil.example/?data=" + "x" * 80
    raw = f"{secret} {exfil}"
    wire: list[Any] = []

    async def publish(_name: str, payload: Any) -> None:
        wire.append(payload)

    hub = live_hub.LiveHub()
    hub.bind(publish, None)
    session = live_hub.LiveSession("chat-1", state="live", url=raw, title=raw)
    session.steps = [live_hub.Step(raw)]
    session.loop = {"reason": raw}
    session.handback_options = [raw]
    session.ask = SimpleNamespace(
        outcome="",
        wire=lambda: {"reason": raw, "summary": raw, "fields": [{"label": raw}], "text": raw},
    )
    await hub._emit_now(session)
    assert len(wire) == 1
    assert secret not in json.dumps(wire) and exfil not in json.dumps(wire)
    assert session.url == raw and session.steps[0].label == raw
    assert wire[0]["session_key"] == "chat-1" and wire[0]["state"] == "live"


@pytest.mark.asyncio
async def test_stop_install_reaps_owned_child_and_starts_no_next_step(
    tmp_path: Path, monkeypatch
) -> None:
    started = threading.Event()
    released = threading.Event()
    terminated: list[Any] = []
    spawns: list[Any] = []

    class Child:
        returncode = 0

        def __init__(self, argv: Any, **kwargs: Any) -> None:
            spawns.append(argv)

        def communicate(self, timeout: float) -> tuple[str, str]:
            started.set()
            if released.wait(timeout):
                return "", ""
            raise subprocess.TimeoutExpired(spawns[-1], timeout)

    def terminate(child: Any, **kwargs: Any) -> None:
        terminated.append(child)
        released.set()

    monkeypatch.setattr(install.subprocess, "Popen", Child)
    monkeypatch.setattr(platform_compat, "terminate_and_reap_sync", terminate)
    task = asyncio.create_task(install.install(tmp_path, components=(install.COMPONENT_HARNESS,)))
    state = SimpleNamespace(_browser_harness_install_task=task)
    try:
        await async_wait_until(started.is_set, timeout=30)
        await stop_browser_harness_install(state)
        assert task.cancelled()
        assert len(terminated) == 1
        assert len(spawns) == 1
        assert not (install.harness_prefix(tmp_path) / install.MARKER_FILE).exists()
    finally:
        released.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_redactor_failure_drops_event(monkeypatch) -> None:
    sent: list[Any] = []

    async def publish(_name: str, payload: Any) -> None:
        sent.append(payload)

    def refuse(_text: str) -> str:
        raise RuntimeError("scanner unavailable")

    monkeypatch.setattr(live_hub, "redact_via_context", refuse)
    hub = live_hub.LiveHub()
    hub.bind(publish, None)
    await hub._emit_now(live_hub.LiveSession("chat-1", state="live", title="private"))
    assert sent == []


def test_stop_between_steps_prevents_pip_and_marker(tmp_path: Path) -> None:
    stop = threading.Event()
    calls: list[Any] = []

    def runner(argv: Any, timeout: float) -> Any:
        calls.append(argv)
        stop.set()
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(install.InstallError, match="install stopped"):
        install.install_sync(
            tmp_path, runner=runner, stop=stop, components=(install.COMPONENT_HARNESS,)
        )
    assert len(calls) == 1
    assert "venv" in calls[0] and "pip" not in calls[0]
    assert not (install.harness_prefix(tmp_path) / install.MARKER_FILE).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_session", [True, False])
async def test_frame_that_navigates_during_the_read_drops_what_was_read(
    child_session: bool, monkeypatch
) -> None:
    relay, frame = frame_ops(child_session)
    relay.url = "https://public.example/"
    real_send = relay.send

    async def send(method: str, params: dict[str, Any], sid: str) -> Any:
        out = await real_send(method, params, sid)
        if method == "Runtime.evaluate":
            relay.url = "http://169.254.169.254/latest/meta-data/"
        return out

    relay.send = send  # type: ignore[method-assign]

    async def slots(_count: int) -> list[str]:
        return ["child"]

    monkeypatch.setattr(frame, "slot_frames", slots)
    ctx = ops.OpContext(
        helper=unused_helper, frames=lambda: frame, url_allowed=lambda u: u.startswith("https://")
    )
    nodes = await ops._with_frames(ctx, [{"role": "iframe", "slot": 0}])
    assert relay.reads, "the read itself ran against the then-allowed frame"
    assert nodes == [{"role": "iframe", "state": {"unread": True}}]
    assert "private canary" not in json.dumps(nodes)


@pytest.mark.asyncio
async def test_a_second_cancel_still_waits_for_the_installer_child(
    tmp_path: Path, monkeypatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()

    def blocking_install_sync(*_args: Any, stop: threading.Event, **_kwargs: Any) -> Any:
        started.set()
        wait_until(stop.is_set, timeout=30)
        wait_until(release.is_set, timeout=30)
        settled.set()
        raise install.InstallError(install.STAGE_PREPARING, "install stopped")

    monkeypatch.setattr(install, "install_sync", blocking_install_sync)
    task = asyncio.create_task(install.install(tmp_path))
    try:
        await async_wait_until(started.is_set, timeout=30)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert settled.is_set()
    finally:
        release.set()


def test_a_stopped_install_ends_the_chromium_download_between_chunks(tmp_path: Path) -> None:
    from kiro_crew.browser_harness import chromium

    stop = threading.Event()
    reads: list[int] = []

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def read(self, _n: int) -> bytes:
            reads.append(1)
            stop.set()
            return b"x" * 16

    opened: list[str] = []

    def opener(url: str, _timeout: float) -> Response:
        opened.append(url)
        return Response()

    with pytest.raises(chromium.ChromiumDownloadStopped):
        chromium.download_chromium(
            tmp_path,
            platform="linux64",
            urls=("https://a.example/{version}/{platform}", "https://b.example/{version}"),
            opener=opener,
            should_stop=stop.is_set,
        )
    assert len(reads) == 1
    assert opened == ["https://a.example/" + chromium.CHROMIUM_VERSION + "/linux64"]


class _StubFrames:
    def __init__(self, url: str) -> None:
        self._url = url
        self.calls = 0

    async def url_for_ref(self, _tag: int) -> str:
        return self._url

    async def call(self, *_a: Any) -> Any:
        self.calls += 1
        return {"ok": True}


@pytest.mark.asyncio
async def test_a_gone_frame_ref_is_stale_and_keeps_the_tab() -> None:
    stub = _StubFrames("")
    blanked: list[str] = []

    async def helper(name: str, **kwargs: Any) -> Any:
        if name == "goto":
            blanked.append(kwargs.get("url", ""))
        return {"url": "https://example.com"} if name == "current_tab" else None

    ctx = ops.OpContext(
        helper=helper, frames=lambda: stub, url_allowed=lambda u: u.startswith("https://")
    )
    out = await ops._page(ctx, "click", "f3e1")
    assert out == {"ok": False, "code": "ref_stale"}
    assert stub.calls == 0 and blanked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["about:blank", "about:srcdoc"])
async def test_a_parent_written_frame_is_read(url: str) -> None:
    stub = _StubFrames(url)
    ctx = ops.OpContext(
        helper=unused_helper, frames=lambda: stub, url_allowed=lambda u: u.startswith("https://")
    )
    assert await ops._page(ctx, "click", "f3e1") == {"ok": True}
    assert stub.calls == 1

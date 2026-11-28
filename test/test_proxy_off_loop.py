"""Async callers resolve the system proxy off the event loop.

``http_proxy.is_bypassed`` can consult the OS bypass matcher, which on Windows
(``proxy_bypass_registry``) can block on a DNS lookup. Each async entry that
builds a Slack or Telegram client goes through ``resolve_proxy_off_loop``, so
that matcher never runs on the loop thread. The matcher is replaced here by one
that records the thread it runs on; any entry that resolves inline fails.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.request
from typing import Any

import pytest

from kiro_crew import http_proxy

_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")


@pytest.fixture
def os_matcher_threads(monkeypatch: Any) -> list[int]:
    """NO_PROXY alone plus an OS proxy, on a host with an OS reader (Windows, macOS)."""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {"no": "example.test"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(http_proxy, "_os_proxies", lambda: {"https": "http://127.0.0.1:3128"})
    monkeypatch.setattr(http_proxy, "_os_reader", lambda: dict)  # a Windows or macOS host
    threads: list[int] = []

    def matcher(host: str) -> bool:
        threads.append(threading.get_ident())
        return False

    monkeypatch.setattr(http_proxy, "_os_bypass", lambda: matcher)
    return threads


def _run_on_loop(coro_fn: Any) -> tuple[Any, int]:
    async def main() -> tuple[Any, int]:
        return await coro_fn(), threading.get_ident()

    return asyncio.run(main())


def test_the_shared_helper_runs_the_os_matcher_off_the_loop(os_matcher_threads: list[int]) -> None:
    proxy, loop_thread = _run_on_loop(
        lambda: http_proxy.resolve_proxy_off_loop(http_proxy.SLACK_API_URL)
    )
    assert proxy == "http://127.0.0.1:3128"
    assert os_matcher_threads and loop_thread not in os_matcher_threads


def test_the_shared_access_client_resolves_off_the_loop(os_matcher_threads: list[int]) -> None:
    from kiro_crew.dashboard import slack_shared_access

    client, loop_thread = _run_on_loop(lambda: slack_shared_access._web_client("xoxb-test"))
    assert client.proxy == "http://127.0.0.1:3128"
    assert os_matcher_threads and loop_thread not in os_matcher_threads


def test_the_slack_token_check_resolves_off_the_loop(
    os_matcher_threads: list[int], monkeypatch: Any
) -> None:
    import slack_sdk.web.async_client as async_client

    from kiro_crew.dashboard.messaging_api import slack_settings

    built: list[Any] = []

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

        async def auth_test(self) -> dict[str, Any]:
            return {"ok": True}

    monkeypatch.setattr(async_client, "AsyncWebClient", _Client)
    verdict, loop_thread = _run_on_loop(
        lambda: slack_settings._validate_slack_token("SLACK_BOT_TOKEN", "xoxb-test")
    )
    assert verdict is None
    assert built[0]["proxy"] == "http://127.0.0.1:3128"
    assert os_matcher_threads and loop_thread not in os_matcher_threads


def test_the_boot_path_builds_the_slack_client_without_a_proxy_lookup() -> None:
    # no-new-work-on-gateway-boot-path: _init_services runs before the dashboard
    # binds, so the lookup waits for the client's first Web API call.
    import inspect

    from kiro_crew.slack.gateway_runtime import service_boot

    assert "resolve_proxy" not in inspect.getsource(service_boot._init_services)


def test_the_first_web_api_call_takes_the_system_proxy(monkeypatch: Any) -> None:
    # The gateway, an orphan notice or a cron: whichever calls Slack first.
    from slack_sdk.web.async_client import AsyncWebClient

    from kiro_crew.slack import client as slack_client

    resolved: list[str] = []

    async def _resolve(url: str) -> str:
        resolved.append(url)
        return "http://127.0.0.1:3128"

    sent: list[Any] = []

    async def _send(self: Any, api_method: str, **kwargs: Any) -> dict:
        sent.append((api_method, self.proxy))
        return {"ok": True}

    monkeypatch.setattr(slack_client, "resolve_proxy_off_loop", _resolve)
    monkeypatch.setattr(AsyncWebClient, "api_call", _send)
    client = slack_client.RealSlackClient("xoxb-test")

    async def _two_calls() -> None:
        await client._web.chat_postMessage(channel="D1", text="hi")
        await client._web.chat_postMessage(channel="D1", text="again")

    asyncio.run(_two_calls())
    assert sent == [("chat.postMessage", "http://127.0.0.1:3128")] * 2
    assert len(resolved) == 1


def test_the_worker_never_reads_the_environment_the_loop_writes(monkeypatch: Any) -> None:
    # channel_config_writes sets and clears os.environ on the loop; a worker
    # thread iterating it meanwhile can raise (RuntimeError / KeyError). So every
    # read of the proxy environment must happen on the loop thread, never a worker.
    import os

    monkeypatch.setenv("NO_PROXY", "example.test")
    # One fixed proxy source: the host's own system proxy must not decide the answer.
    monkeypatch.setattr(http_proxy, "_os_proxies", lambda: {})
    monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None)
    real_read = urllib.request.getproxies_environment
    readers: list[int] = []

    def _recording_read() -> dict[str, str]:
        readers.append(threading.get_ident())
        return real_read()

    monkeypatch.setattr(urllib.request, "getproxies_environment", _recording_read)
    url = "https://slack.com/api/"

    async def _resolve_while_writing() -> tuple[list[str | None], int]:
        stop = False

        async def _writer() -> None:
            # Smoke only: the answers must not change while the loop writes.
            n = 0
            while not stop:
                os.environ[f"KC_CHURN_{n % 5}"] = "1"  # flake-ok: the loop write under test
                os.environ.pop(f"KC_CHURN_{(n + 2) % 5}", None)
                n += 1
                await asyncio.sleep(0)

        task = asyncio.create_task(_writer())
        try:
            answers = [await http_proxy.resolve_proxy_off_loop(url) for _ in range(200)]
        finally:
            stop = True
            await task
            for k in range(5):
                os.environ.pop(f"KC_CHURN_{k}", None)
        return answers, threading.get_ident()

    answers, loop_thread = asyncio.run(_resolve_while_writing())
    assert answers == [None] * 200
    assert len(readers) >= 200
    assert set(readers) == {loop_thread}


def test_the_telegram_proxy_resolves_off_the_loop(os_matcher_threads: list[int]) -> None:
    from kiro_crew.telegram import client_http

    proxy, loop_thread = _run_on_loop(client_http._resolve_proxy_off_loop)
    assert proxy == "http://127.0.0.1:3128"
    assert os_matcher_threads and loop_thread not in os_matcher_threads


def test_no_thread_hop_on_a_platform_with_no_os_proxy_reader(monkeypatch: Any) -> None:
    # Linux has no OS proxy reader, so the OS phase would always find nothing:
    # resolving with no proxy variable set must stay on the loop.
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(http_proxy, "_os_reader", lambda: None)
    monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None)
    hops: list[Any] = []

    async def to_thread(fn: Any, *args: Any) -> Any:
        hops.append(fn)
        return fn(*args)

    monkeypatch.setattr(http_proxy.asyncio, "to_thread", to_thread)
    proxy, _ = _run_on_loop(lambda: http_proxy.resolve_proxy_off_loop(http_proxy.SLACK_API_URL))
    assert proxy is None
    assert hops == []

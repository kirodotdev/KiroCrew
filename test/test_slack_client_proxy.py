"""Every slack_sdk Web API client is built with an explicit proxy.

slack_sdk reads only the proxy environment variables when ``proxy`` is None.
The other Slack legs (Socket Mode and the aiohttp calls) take their proxy from
``http_proxy.resolve_proxy``, which on Windows also reads the system proxy from
the registry. A client built without ``proxy=`` would take a different route
from those legs, so this test refuses any such construction under ``src/``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import urllib.request
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
_CLIENTS = {"WebClient", "AsyncWebClient", "_SystemProxyWebClient"}
_SYNC_CLIENT = "WebClient"
_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")


def _no_proxy_env(monkeypatch: Any) -> None:
    # slack_sdk falls back to these when proxy is None, so clear them.
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


def _constructions() -> list[tuple[str, int, bool]]:
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _CLIENTS:
                has_proxy = any(kw.arg == "proxy" for kw in node.keywords)
                found.append((str(path.relative_to(_SRC)), node.lineno, has_proxy))
    return found


def test_every_slack_sdk_client_is_built_with_a_proxy() -> None:
    sites = _constructions()
    assert sites, "no slack_sdk client constructions found; the scan is broken"
    missing = [f"{path}:{line}" for path, line, has_proxy in sites if not has_proxy]
    assert missing == [], f"slack_sdk client built without proxy=: {missing}"


@pytest.mark.parametrize("proxy", ["http://proxy.example:8080", None], ids=["proxy", "none"])
def test_shared_access_client_forwards_the_resolved_proxy(
    monkeypatch: Any, proxy: str | None
) -> None:
    from kiro_crew.dashboard import slack_shared_access

    _no_proxy_env(monkeypatch)
    asked: list[str] = []

    async def _resolve(url: str) -> str | None:
        asked.append(url)
        return proxy

    monkeypatch.setattr(slack_shared_access, "resolve_proxy_off_loop", _resolve)
    client = asyncio.run(slack_shared_access._web_client("xoxb-test"))
    assert client.proxy == proxy
    assert asked == ["https://slack.com/api/"]


@pytest.mark.parametrize("proxy", ["http://proxy.example:8080", None], ids=["proxy", "none"])
def test_owner_claim_client_takes_the_proxy_it_is_given(
    monkeypatch: Any, proxy: str | None
) -> None:
    from kiro_crew.slack import owner_claim

    _no_proxy_env(monkeypatch)
    client = owner_claim.OwnerClaim._web_client(object(), "xoxb-test", proxy)
    assert client.proxy == proxy


def test_a_no_proxy_bypass_stays_direct_through_the_sdk(monkeypatch: Any) -> None:
    # slack_sdk reloads HTTPS_PROXY when proxy is None; a resolved "direct" must hold.
    from kiro_crew import http_proxy
    from kiro_crew.dashboard import slack_shared_access
    from kiro_crew.slack.client import RealSlackClient

    _no_proxy_env(monkeypatch)
    for name in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "slack.com")
    assert http_proxy.resolve_proxy(http_proxy.SLACK_API_URL) is None

    assert http_proxy.slack_web_client("xoxb-test", None).proxy is None
    assert asyncio.run(slack_shared_access._web_client("xoxb-test")).proxy is None
    real = RealSlackClient("xoxb-test")
    real._web.apply_proxy(None)
    assert real._web.proxy is None


def test_no_sync_slack_sdk_web_client_is_built_in_src() -> None:
    # slack_sdk's sync WebClient opens a None proxy with bare urlopen, which
    # reads the system proxy again; every Slack call goes through the async one.
    sync_sites = [f"{path}:{line}" for path, line, _ in _constructions_of({_SYNC_CLIENT})]
    assert sync_sites == []
    imports = []
    for path in sorted(_SRC.rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("slack_sdk"):
                if any(alias.name == _SYNC_CLIENT for alias in node.names):
                    imports.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert imports == [], f"sync slack_sdk WebClient imported: {imports}"


def _constructions_of(names: set[str]) -> list[tuple[str, int, bool]]:
    return [
        (path, line, has_proxy)
        for path, line, has_proxy in _constructions()
        if path and _called_name(path, line) in names
    ]


def _called_name(path: str, line: int) -> str | None:
    tree = ast.parse((_SRC / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.lineno == line:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _CLIENTS:
                return name
    return None


REGISTRY_SOCKS_ONLY = {"http": "socks4://h:1080", "https": "socks4://h:1080"}


@pytest.fixture
def slack_transport(monkeypatch: Any) -> list[tuple[str, str | None]]:
    """aiohttp, as the AsyncWebClient drives it: record (url, proxy) and answer ok."""
    import aiohttp

    seen: list[tuple[str, str | None]] = []
    body = {
        "ok": True,
        "team": "T",
        "team_id": "T1",
        "user_id": "U1",
        "bot_id": "B1",
        "user": {"real_name": "Ada"},
    }

    class _Res:
        status = 200
        content_type = "application/json"
        headers: dict[str, str] = {}

        async def json(self, **_: Any) -> dict:
            return body

        async def text(self) -> str:
            return "{}"

        async def __aenter__(self) -> "_Res":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    def _request(self: Any, verb: str, url: str, **kwargs: Any) -> _Res:
        seen.append((url, kwargs.get("proxy")))
        return _Res()

    monkeypatch.setattr(aiohttp.ClientSession, "request", _request)
    return seen


def _os_map(monkeypatch: Any, os_map: dict[str, str]) -> None:
    from kiro_crew import http_proxy

    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(os_map))
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(http_proxy, "_os_proxies", lambda: dict(os_map), raising=False)
    monkeypatch.setattr(http_proxy, "_os_bypass", lambda: None, raising=False)


@pytest.mark.parametrize(
    ("os_map", "expected"),
    [
        (REGISTRY_SOCKS_ONLY, None),
        ({"https": "http://p:3128"}, "http://p:3128"),
        (
            {**REGISTRY_SOCKS_ONLY, "socks": "socks://h:1080", "http": "http://p:3128"},
            "http://p:3128",
        ),
    ],
    ids=["registry-socks-only", "registry-https", "http-beside-socks"],
)
@pytest.mark.parametrize("caller", ["setup-secret", "setup-owner", "enterprise"])
def test_every_sync_caller_takes_the_route_the_resolver_chose(
    monkeypatch: Any, slack_transport: list, caller: str, os_map: dict, expected: str | None
) -> None:
    # A resolved None is direct for the sync callers too: none of them may let
    # urllib read the registry again and CONNECT to the SOCKS listener.
    from kiro_crew import cli_setup
    from kiro_crew.slack import enterprise

    _os_map(monkeypatch, os_map)
    if caller == "setup-secret":
        cli_setup._verify_slack_secret("SLACK_BOT_TOKEN", "xoxb-test")
    elif caller == "setup-owner":
        cli_setup._verify_slack_owner_id("xoxb-test", "U12345678")
    else:
        enterprise.validate_enterprise("xoxb-test")
    assert [proxy for _, proxy in slack_transport] == [expected]


def test_an_environment_proxy_reaches_a_sync_caller_unchanged(
    monkeypatch: Any, slack_transport: list
) -> None:
    from kiro_crew import cli_setup

    env = {"https": "http://envproxy:8080"}
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: dict(env))
    monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(env))
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    cli_setup._verify_slack_secret("SLACK_BOT_TOKEN", "xoxb-test")
    assert [proxy for _, proxy in slack_transport] == ["http://envproxy:8080"]


def test_the_gate_check_in_a_worker_uses_the_loops_snapshot(
    monkeypatch: Any, slack_transport: list
) -> None:
    # The gateway resolves on the loop and hands the proxy to the worker; the
    # worker must not iterate os.environ while a .env save edits it.
    from kiro_crew import http_proxy
    from kiro_crew.slack import enterprise

    def _racing_env_read() -> dict:
        raise KeyError("HTTPS_PROXY")  # what a concurrent removal raises mid-iteration

    monkeypatch.setattr(urllib.request, "getproxies_environment", _racing_env_read)

    async def _gate() -> bool:
        # Looked up at test time, so a tree without the hand-off fails by assertion.
        snapshot = getattr(http_proxy, "gate_proxy_snapshot", None)
        with snapshot("http://snapshot:3128") if snapshot else contextlib.nullcontext():
            return await asyncio.to_thread(enterprise.validate_enterprise, "xoxb-test")

    asyncio.run(_gate())
    assert [proxy for _, proxy in slack_transport] == ["http://snapshot:3128"]


def test_the_doctor_gate_check_with_no_snapshot_resolves_itself(
    monkeypatch: Any, slack_transport: list
) -> None:
    from kiro_crew.slack import enterprise

    _os_map(monkeypatch, {"https": "http://p:3128"})
    enterprise.validate_enterprise("xoxb-test")
    assert [proxy for _, proxy in slack_transport] == ["http://p:3128"]


def test_the_owner_claim_gate_check_passes_the_loops_snapshot(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from kiro_crew import http_proxy
    from kiro_crew.slack import owner_claim

    seen: list[Any] = []

    async def _resolve(url: str) -> str:
        return "http://claim:3128"

    def _validate(bot: str, *, extra_ids: Any = None) -> bool:
        handed = getattr(http_proxy, "gate_proxy", None)
        seen.append(handed.get() if handed is not None else None)
        return True

    monkeypatch.setattr(http_proxy, "resolve_proxy_off_loop", _resolve)
    ctx = SimpleNamespace(slack_gate=SimpleNamespace(validate_enterprise=_validate))
    monkeypatch.setattr("kiro_crew.platform.current_context", lambda: ctx)
    claim = object.__new__(owner_claim.OwnerClaim)
    claim._orch = SimpleNamespace(_cfg=SimpleNamespace(slack_enterprise_ids=set()))
    assert asyncio.run(claim._enterprise_permits("xoxb-test")) is True
    assert seen == ["http://claim:3128"]


def test_slack_call_sync_refuses_a_running_loop() -> None:
    # A caller on the event loop must use the async client directly.
    from kiro_crew import http_proxy

    async def _on_loop() -> None:
        with pytest.raises(RuntimeError):
            http_proxy.slack_call_sync("xoxb-test", None, "auth_test")

    asyncio.run(_on_loop())

"""Every slack_sdk Web API client is built with an explicit proxy.

slack_sdk reads only the proxy environment variables when ``proxy`` is None.
The other Slack legs (Socket Mode and the aiohttp calls) take their proxy from
``http_proxy.resolve_proxy``, which on Windows also reads the system proxy from
the registry. A client built without ``proxy=`` would take a different route
from those legs, so this test refuses any such construction under ``src/``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
_CLIENTS = {"WebClient", "AsyncWebClient"}
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
    monkeypatch.setattr(
        slack_shared_access, "resolve_proxy", lambda url: asked.append(url) or proxy
    )
    client = slack_shared_access._web_client("xoxb-test")
    assert client.proxy == proxy
    assert asked == ["https://slack.com/api/"]


@pytest.mark.parametrize("proxy", ["http://proxy.example:8080", None], ids=["proxy", "none"])
def test_owner_claim_client_forwards_the_resolved_proxy(
    monkeypatch: Any, proxy: str | None
) -> None:
    from kiro_crew.slack import owner_claim

    _no_proxy_env(monkeypatch)
    monkeypatch.setattr(owner_claim, "resolve_proxy", lambda url: proxy)
    client = owner_claim.OwnerClaim._web_client(object(), "xoxb-test")
    assert client.proxy == proxy

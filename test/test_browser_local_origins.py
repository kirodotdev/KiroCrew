"""``dashboard.browser_local_origins``: the operator's loopback exception.

The native ``browser`` tool refuses every non-public ``navigate`` target. This
setting lets the operator name exact loopback ``host:port`` origins (a Vite dev
server) the built-in panel may open anyway. These tests pin both halves:

* the coercer keeps only ``localhost`` / ``127.0.0.1`` / ``[::1]`` with an
  explicit port, so no entry can widen the gate past one loopback port;
* the tool admits a listed origin, still refuses everything else (unlisted
  ports, alternate encodings, ``*.localhost``, userinfo tricks, private
  addresses), and refuses the gateway's own port even when it is listed.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.config.sections import coerce_browser_local_origins
from kiro_crew.mcp_tools import browser as mod

GATEWAY_PORT = 5476


@pytest.fixture(autouse=True)
def _tool_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    mp = _floor_monkeypatch
    mp.setattr(mod, "_browsing_available", lambda: True)
    mp.setattr(mod.mcp_core, "_session_key_header_error", lambda sk: None)
    mp.setattr(mod.mcp_core, "_vet_browse_governance", lambda s: None)
    mp.setattr(mod, "_use_builtin_browser", lambda: True)
    mp.setattr(mod.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-7-1")
    mp.setattr(mod.mcp_core, "_api_port", lambda: GATEWAY_PORT)


# --- coercer ---------------------------------------------------------------


def test_coercer_keeps_only_loopback_with_explicit_port() -> None:
    raw = [
        "localhost:5173",
        " LOCALHOST:3000 ",  # trimmed + lowercased
        "127.0.0.1:08080",  # port rebuilt canonically
        "[::1]:4200",
        "localhost:5173",  # duplicate
        "localhost",  # no port -> would open every port
        "localhost:",  # empty port
        "localhost:0",
        "localhost:65536",
        "localhost:" + "9" * 5000,  # past int()'s digit limit: dropped, not raised
        "localhost:000080",  # six digits: over the length cap
        "localhost:+80",
        "localhost:８０",  # fullwidth digits
        "::1:4200",  # unbracketed IPv6
        "http://localhost:5173",  # scheme
        "localhost:5173/app",  # path
        "user@localhost:5173",  # userinfo
        "app.localhost:5173",  # *.localhost subdomain
        "0x7f000001:5173",  # alternate IPv4 encoding
        "127.0.0.2:5173",  # other loopback address: not one of the three spellings
        "192.168.1.10:5173",  # LAN
        "*:5173",
        5173,
        None,
    ]
    assert coerce_browser_local_origins(raw) == [
        "localhost:5173",
        "localhost:3000",
        "127.0.0.1:8080",
        "[::1]:4200",
    ]


@pytest.mark.parametrize("raw", [None, "localhost:5173", {"localhost:5173": True}, 5173])
def test_coercer_fails_closed_on_non_list(raw: object) -> None:
    assert coerce_browser_local_origins(raw) == []


def test_loader_applies_the_coercer() -> None:
    from kiro_crew.config.loader import _build_dashboard_config

    dash = _build_dashboard_config(
        set(), {"browser_local_origins": ["localhost:5173", "10.0.0.5:80"]}
    )
    assert dash.browser_local_origins == ["localhost:5173"]
    assert _build_dashboard_config(set(), {}).browser_local_origins == []


# --- matcher ---------------------------------------------------------------

ALLOWED = frozenset({"localhost:5173", "127.0.0.1:8080", "[::1]:4200", f"localhost:{GATEWAY_PORT}"})


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5173/",
        "http://localhost:5173/src/App.tsx?x=1#top",
        "HTTP://LOCALHOST:5173/",
        "https://localhost:5173/",
        "http://127.0.0.1:8080/",
        "http://[::1]:4200/",
    ],
)
def test_matcher_admits_listed_origins(url: str) -> None:
    assert mod._navigate_target_is_allowed_local(url, ALLOWED) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5174/",  # unlisted port
        "http://localhost/",  # default port 80, not listed
        "http://127.0.0.1:5173/",  # listed only as localhost: spellings are distinct
        "http://0x7f000001:8080/",  # alternate encoding of a listed address
        "http://2130706433:8080/",
        "http://app.localhost:5173/",  # *.localhost
        "http://localhost.:5173/",  # trailing-dot spelling
        "http://localhost:5173@169.254.169.254/",  # userinfo: host is IMDS
        "http://user@localhost:5173/",  # userinfo of any kind
        "http://localhost:5173\\@169.254.169.254/",  # parser differential
        "http://localhost:5173\t/",
        "ftp://localhost:5173/",
        "file:///etc/passwd",
        "http://localhost:99999/",  # invalid port
        "http://10.0.0.5:5173/",
        f"http://localhost:{GATEWAY_PORT}/api/spawn",  # gateway, even though listed
    ],
)
def test_matcher_refuses_everything_else(url: str) -> None:
    assert mod._navigate_target_is_allowed_local(url, ALLOWED) is False


def test_matcher_refuses_when_gateway_port_is_unknowable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> int:
        raise RuntimeError("no gateway")

    monkeypatch.setattr(mod.mcp_core, "_api_port", _boom)
    assert mod._navigate_target_is_allowed_local("http://localhost:5173/", ALLOWED) is False


def test_matcher_with_empty_allowlist_refuses_loopback() -> None:
    assert mod._navigate_target_is_allowed_local("http://localhost:5173/", frozenset()) is False


# --- tool end to end -------------------------------------------------------


def _recording_post(calls: list[str]):
    def _fake(bus_key: str, op: str, args: dict, session_header: str, timeout_ms: int):
        calls.append(args.get("url"))
        return 200, {"ok": True, "result": "ok"}

    return _fake


def test_tool_navigates_a_listed_dev_server(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mod, "_post_command", _recording_post(calls))
    monkeypatch.setattr(mod, "_allowed_local_origins", lambda: frozenset({"localhost:5173"}))

    out = mod.browser("browser", {"op": "navigate", "args": {"url": "http://localhost:5173/"}})

    assert calls == ["http://localhost:5173/"]
    assert out.startswith("Browser navigate:")


@pytest.mark.parametrize(
    "url",
    ["http://localhost:5174/", f"http://localhost:{GATEWAY_PORT}/", "http://169.254.169.254/"],
)
def test_tool_still_refuses_unlisted_and_gateway(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    def _must_not_post(*_a: Any, **_k: Any):
        raise AssertionError("must not POST a refused navigate target")

    monkeypatch.setattr(mod, "_post_command", _must_not_post)
    monkeypatch.setattr(
        mod,
        "_allowed_local_origins",
        lambda: frozenset({"localhost:5173", f"localhost:{GATEWAY_PORT}"}),
    )

    out = mod.browser("browser", {"op": "navigate", "args": {"url": url}})

    assert out.startswith("Error: the browser tool only opens public http(s) URLs")
    assert "dashboard.browser_local_origins" in out
    # No Settings control exists for the list, so the refusal must hand the
    # agent the exact command the user runs to add an origin.
    assert "`kirocrew config set dashboard.browser_local_origins '[\"localhost:5173\"]'`" in out


def test_allowed_local_origins_reads_config_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    cfg = SimpleNamespace(dashboard=SimpleNamespace(browser_local_origins=["localhost:5173"]))
    monkeypatch.setattr(mod.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    assert mod._allowed_local_origins() == frozenset({"localhost:5173"})

    def _broken(cls: type) -> Any:
        raise OSError("unreadable")

    monkeypatch.setattr(mod.KiroCrewConfig, "load", classmethod(_broken))
    assert mod._allowed_local_origins() == frozenset()

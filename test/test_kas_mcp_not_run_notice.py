"""KAS: the session notice names MCP servers the backend will not run at all.

KAS refuses a plain ``http`` url to a non-loopback host (``insecure-url``), and in
MCP registry mode it keeps only admin-catalog servers, dropping every server Crew
sends; the session notice names both. A plain ``http`` server with headers still
connects through Crew's local bridge (headers in cleartext) unless
``agent.kas_refuse_plain_http_mcp_headers`` is on. A remote server's headers also
reach it through the local bridge when the session has no key yet.
"""

from __future__ import annotations

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents, kas_env_launch
from kiro_crew.acp.kas_agents import (
    NOT_RUN_INSECURE_URL,
    NOT_RUN_REGISTRY,
    NOT_RUN_REGISTRY_IF_GOVERNED,
    WithheldOut,
    build_kas_custom_agents,
    to_client_custom_agent,
)
from kiro_crew.acp.kas_env_launch import bridgeable, kas_accepts_url
from kiro_crew.acp.kas_withheld_notice import withheld_credentials_notice
from kiro_crew.kas_env_shim import SHIM_MODULE


@pytest.fixture(autouse=True)
def home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _collect(servers: dict, session_key: str = "s-1") -> tuple[dict, WithheldOut]:
    withheld: WithheldOut = {}
    spec = _spec(mcpServers={"kirocrew-core": {"command": "x"}, **servers})
    entry = to_client_custom_agent(
        "a",
        spec,
        "p",
        stub_server_names=frozenset(),
        session_key=session_key,
        withheld_out=withheld,
    )
    return entry["mcpServers"], withheld


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://mcp.example/v1", True),
        ("HTTPS://mcp.example/v1", True),
        ("http://localhost:8080/mcp", True),
        ("http://127.0.0.1/mcp", True),
        ("http://[::1]:9000/mcp", True),
        ("http://127.1:8080/mcp", True),
        ("http://[0:0:0:0:0:0:0:1]:8080/mcp", True),
        ("http://0x7f000001/mcp", True),
        ("http://127.0.0.1./mcp", True),
        ("http://localhost./mcp", False),
        ("http://[::ffff:127.0.0.1]/mcp", False),
        ("http://cafe/mcp", False),
        ("http://localhost:abc/mcp", False),
        ("http://127.0.0.1:65536/mcp", False),
        ("https://mcp.example:99999/v1", False),
        ("http://mcp.example/v1", False),
        ("http://127.0.0.2/mcp", False),
        ("http://localhost.evil.example/mcp", False),
        ("ftp://localhost/mcp", False),
        ("not a url", False),
        (None, False),
    ],
)
def test_kas_accepts_url_matches_the_backends_rule(url, ok):
    assert kas_accepts_url(url) is ok


def _refuse(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(kas_env_launch, "refuses_plain_http_headers", lambda: on)


class TestInsecureUrl:
    def test_by_default_a_plain_http_server_with_headers_is_bridged_as_before(self, monkeypatch):
        _refuse(monkeypatch, False)
        remote = {"type": "http", "url": "http://mcp.example/v1", "headers": {"Authorization": "t"}}
        assert bridgeable(remote)
        servers, withheld = _collect({"remote": remote})
        assert SHIM_MODULE in servers["remote"]["args"]
        assert withheld == {}

    @pytest.mark.parametrize("url", [" http://127.0.0.1:8080/mcp", " http://mcp.example/v1 "])
    def test_a_url_with_surrounding_space_is_bridged_like_the_other_readers(self, monkeypatch, url):
        """``kas_accepts_url`` and ``bridge_url`` strip the url, so ``bridgeable`` does too."""
        _refuse(monkeypatch, False)
        assert bridgeable({"type": "http", "url": url, "headers": {"X": "y"}})

    def test_with_the_refusal_on_it_is_not_bridged(self, monkeypatch):
        _refuse(monkeypatch, True)
        remote = {"type": "http", "url": "http://mcp.example/v1", "headers": {"Authorization": "t"}}
        assert not bridgeable(remote)
        servers, withheld = _collect({"remote": remote})
        assert servers["remote"]["url"] == "http://mcp.example/v1"
        assert SHIM_MODULE not in (servers["remote"].get("args") or [])
        assert withheld == {"remote": ((), (NOT_RUN_INSECURE_URL,), False)}

    def test_a_plain_http_server_without_headers_is_named(self):
        _, withheld = _collect({"remote": {"url": "http://mcp.example/v1"}})
        assert withheld == {"remote": ((), (NOT_RUN_INSECURE_URL,), False)}

    def test_a_loopback_http_server_with_headers_is_still_bridged(self, monkeypatch):
        _refuse(monkeypatch, True)
        remote = {"type": "http", "url": "http://127.0.0.1:9/mcp", "headers": {"X": "y"}}
        servers, withheld = _collect({"remote": remote})
        assert SHIM_MODULE in servers["remote"]["args"]
        assert withheld == {}

    def test_the_notice_names_it_and_the_fix(self, monkeypatch):
        _refuse(monkeypatch, True)
        _, withheld = _collect({"remote": {"url": "http://mcp.example/v1", "headers": {"X": "y"}}})
        notice = withheld_credentials_notice(withheld)
        assert "• remote" in notice and "https://" in notice
        assert "start without the settings" not in notice


@pytest.mark.parametrize(
    ("stored", "on"), [(True, True), (False, False), ("true", False), (None, False)]
)
def test_the_refusal_is_read_from_the_agent_config(home, stored, on):
    """Only a JSON ``true`` under ``agent`` turns the refusal on; absent is off."""
    import json

    agent = {} if stored is None else {"kas_refuse_plain_http_mcp_headers": stored}
    (home / "config.json").write_text(json.dumps({"agent": agent}), encoding="utf-8")
    assert kas_env_launch.refuses_plain_http_headers() is on


class TestRegistryMode:
    def test_every_server_is_named_crews_own_conditionally(self, monkeypatch):
        """Crew withholds the third-party server itself. Its own server is still
        declared, and KAS drops it only when the Kiro profile is governed, which
        KAS reads on its own, so the notice says that part conditionally."""
        monkeypatch.setattr(kas_agents, "_registry_governed", lambda: True)
        _, withheld = _collect({"gitlab": {"command": "g", "env": {"TOKEN": "v"}}})
        assert withheld == {
            "gitlab": ((), (NOT_RUN_REGISTRY,), True),
            "kirocrew-core": ((), (NOT_RUN_REGISTRY_IF_GOVERNED,), True),
        }
        notice = withheld_credentials_notice(withheld)
        assert "• gitlab" in notice and "• kirocrew-core" in notice
        assert "If your Kiro profile is registry-governed" in notice
        assert "kirocrew config set agent.mcp_registry_mode false" in notice

    def test_registry_mode_off_names_nothing(self, monkeypatch):
        monkeypatch.setattr(kas_agents, "_registry_governed", lambda: False)
        _, withheld = _collect({"bare": {"command": "b"}})
        assert withheld == {}
        assert withheld_credentials_notice(withheld) == ""


def test_headers_reach_the_bridge_without_a_session_key(tmp_path):
    remote = {"type": "http", "url": "https://mcp.example/v1", "headers": {"X": "y"}}
    withheld: WithheldOut = {}
    spec = _spec(mcpServers={"remote": remote})
    batch = build_kas_custom_agents(tmp_path, "a", spec, session_key="", withheld_out=withheld)
    args = batch[0]["mcpServers"]["remote"]["args"]
    assert SHIM_MODULE in args
    assert args[args.index("--session") + 1].startswith("unkeyed-")
    assert withheld == {}


@pytest.mark.parametrize(
    "url", ["http://127.1:9/mcp", "http://0x7f000001/mcp", "http://127.0.0.1./mcp"]
)
def test_a_loopback_bridge_never_goes_through_an_environment_proxy(monkeypatch, url):
    """``no_proxy`` matches host spellings literally, so a loopback alias KAS
    accepts would otherwise reach ``http_proxy`` in cleartext with the headers."""
    import urllib.request

    from kiro_crew.kas_http_bridge import _opener

    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    # An empty ProxyHandler registers nothing, so no handler may carry a proxy.
    handlers = _opener(url).handlers
    assert not [h for h in handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
    # https keeps the environment proxy: it tunnels, so the headers stay encrypted.
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in _opener("https://x/").handlers)
    # So does plain http to another host: the proxy may be its only route there.
    remote = _opener("http://mcp.example/v1").handlers
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in remote)


@pytest.mark.parametrize(
    ("url", "dialled"),
    [
        ("http://127.0.0.1./mcp?a=1", "http://127.0.0.1/mcp?a=1"),
        ("http://127.1:8080/mcp", "http://127.0.0.1:8080/mcp"),
        ("http://[0:0:0:0:0:0:0:1]:9/mcp", "http://[::1]:9/mcp"),
        ("http://LOCALHOST:9/mcp", "http://localhost:9/mcp"),
        ("http://user:pw@127.0.0.1.:8080/mcp", "http://127.0.0.1:8080/mcp"),
        ("https://mcp.example/v1", "https://mcp.example/v1"),
        # The launcher refuses a url that does not start with the scheme.
        (" https://mcp.example/v1 ", "https://mcp.example/v1"),
        (" http://mcp.example/v1", "http://mcp.example/v1"),
    ],
)
def test_the_bridge_dials_the_canonical_loopback_host(home, url, dialled):
    """``127.0.0.1.`` passes the loopback test, but the OS resolver looks it up
    through DNS, so the snapshot must hold the canonical host the bridge dials."""
    import json

    from kiro_crew.acp import kas_env_launch

    entry, relayed = kas_env_launch.relay_withheld_headers(
        {"type": "http", "url": url}, {"X": "y"}, session_key="s", agent_id="a", server="r"
    )
    assert relayed
    (name,) = kas_env_launch.batch_snapshot_names([{"mcpServers": {"r": entry}}])
    doc = json.loads((home / "kas-mcp-launch" / name).read_text(encoding="utf-8"))
    assert doc["url"] == dialled
    # The host urllib hands the resolver is the canonical one, nothing else.
    import urllib.request

    host = urllib.request.Request(doc["url"]).host.rsplit(":", 1)[0].strip("[]")
    assert host in {"127.0.0.1", "::1", "localhost", "mcp.example"}


def test_a_bad_port_on_one_server_does_not_stop_the_session(tmp_path):
    """A port that does not parse refuses that one server, not the whole batch."""
    remote = {"type": "http", "url": "http://localhost:abc/mcp", "headers": {"X": "y"}}
    withheld: WithheldOut = {}
    spec = _spec(mcpServers={"bad": remote, "ok": {"command": "x"}})
    batch = build_kas_custom_agents(tmp_path, "a", spec, withheld_out=withheld)
    assert set(batch[0]["mcpServers"]) == {"bad", "ok"}
    assert withheld["bad"] == ((), (NOT_RUN_INSECURE_URL,), False)

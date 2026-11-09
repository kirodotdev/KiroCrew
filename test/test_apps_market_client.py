"""HTTP ``MarketClient`` for Kiro Space marketplace app installs.

``HttpMarketClient`` fills the installer's ``ResolvedApp`` / ``ListingDownload``
from the listing API's ``GetInstallPlan`` and ``GetListingDownload`` operations
(Smithy rpcv2Cbor: ``POST /service/KiroSpace/operation/<Op>``, CBOR bodies).
These tests drive it against a local fake server on 127.0.0.1.
"""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import market_client
from kiro_crew.apps.market_client import HttpMarketClient, cbor_decode, cbor_encode
from kiro_crew.apps.marketplace_install import MarketInstallError, folder_capabilities

REF = "acme/ticket-digest"
SHA = "a" * 64
COMP_SHA = "b" * 64


# ---------------------------------------------------------------------------
# CBOR codec: RFC 8949 Appendix A vectors, so the fake server below is not
# checked only against the codec it uses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hex_bytes", "value"),
    [
        ("00", 0),
        ("1903e8", 1000),
        ("1b000000e8d4a51000", 1000000000000),
        ("3863", -100),
        ("f93c00", 1.0),
        ("fb3ff199999999999a", 1.1),
        ("f4", False),
        ("f5", True),
        ("f6", None),
        ("4401020304", b"\x01\x02\x03\x04"),
        ("6449455446", "IETF"),
        ("7f657374726561646d696e67ff", "streaming"),
        ("83010203", [1, 2, 3]),
        ("9f018202039f0405ffff", [1, [2, 3], [4, 5]]),
        ("a201020304", {1: 2, 3: 4}),
        ("bf6346756ef563416d7421ff", {"Fun": True, "Amt": -2}),
        ("c11a514b67b0", 1363896240),
    ],
)
def test_cbor_decode_rfc_vectors(hex_bytes: str, value: Any) -> None:
    assert cbor_decode(bytes.fromhex(hex_bytes)) == value


def test_cbor_encode_matches_rfc_vectors() -> None:
    assert cbor_encode({"a": 1, "b": [2, 3]}).hex() == "a26161016162820203"
    assert cbor_encode(-100).hex() == "3863"
    assert cbor_encode(1000000000000).hex() == "1b000000e8d4a51000"
    assert cbor_encode([True, False, None]).hex() == "83f5f4f6"
    assert cbor_encode(1.1).hex() == "fb3ff199999999999a"


def test_cbor_decode_refuses_trailing_bytes_and_truncation() -> None:
    with pytest.raises(ValueError):
        cbor_decode(bytes.fromhex("0000"))
    with pytest.raises(ValueError):
        cbor_decode(bytes.fromhex("6449"))


# ---------------------------------------------------------------------------
# Fake listing server
# ---------------------------------------------------------------------------


def _perms(**over: Any) -> dict[str, Any]:
    return {"commands": [], "network": "NONE", "files": [], "secrets": [], **over}


def _node(**over: Any) -> dict[str, Any]:
    return {
        "ref": REF,
        "listingId": "lst_1",
        "version": 3,
        "semver": "1.2.0",
        "kind": "app",
        "sha256": SHA,
        "sizeBytes": 2048,
        "publisher": {"kind": "person", "provider": "midway", "id": "u-1"},
        "publishedBy": {"kind": "person", "display": "Pat Doe"},
        "permissions": _perms(
            commands=[["node", "server.js"]],
            network="HOSTS",
            hosts=["api.example.internal"],
            files=["~/notes"],
            secrets=["DIGEST_TOKEN"],
        ),
        "state": "NEW",
        "reason": "REQUESTED",
        "requiredBy": [],
        "installable": True,
        "yanked": False,
        "scan": "CLEAN",
        **over,
    }


class _LoopbackServer(ThreadingHTTPServer):
    # HTTPServer.server_bind resolves the host (socket.getfqdn), which can stall
    # on macOS; bind without the lookup (testing-conventions.md).
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeServer:
    """Answers rpcv2Cbor calls from canned replies and records every request."""

    def __init__(self) -> None:
        self.replies: dict[str, tuple[int, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.blob = b""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _record(self, body: bytes) -> None:
                fake.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": cbor_decode(body) if body else None,
                    }
                )

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self._record(body)
                op = self.path.rsplit("/", 1)[-1]
                status, reply = fake.replies.get(op, (404, {"message": "no such op"}))
                payload = cbor_encode(reply)
                self.send_response(status)
                self.send_header("Content-Type", "application/cbor")
                self.send_header("smithy-protocol", "rpc-v2-cbor")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                self._record(b"")
                self.send_response(200)
                self.send_header("Content-Length", str(len(fake.blob)))
                self.end_headers()
                self.wfile.write(fake.blob)

        self.httpd = _LoopbackServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


def _client(server: FakeServer) -> HttpMarketClient:
    return HttpMarketClient(server.url, token=lambda: "tok-123")


def _plan(*nodes: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"nodes": list(nodes), "permissionsUnion": _perms(), "planDigest": "d" * 64}


# ---------------------------------------------------------------------------
# GetInstallPlan -> ResolvedApp
# ---------------------------------------------------------------------------


def test_resolve_install_posts_rpcv2cbor_and_maps_the_requested_node(server: FakeServer) -> None:
    server.replies["GetInstallPlan"] = _plan(
        _node(),
        _node(
            ref=f"{REF}#digest",
            kind="skill",
            sha256=COMP_SHA,
            sizeBytes=None,
            reason="BUNDLED",
            requiredBy=[REF],
        ),
    )
    resolved = _client(server).resolve_install(REF, 3)

    req = server.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/service/KiroSpace/operation/GetInstallPlan"
    assert req["headers"]["smithy-protocol"] == "rpc-v2-cbor"
    assert req["headers"]["content-type"] == "application/cbor"
    assert req["headers"]["authorization"] == "Bearer tok-123"
    assert req["body"] == {"ref": REF, "version": 3}

    assert resolved.ref == REF
    assert resolved.version == 3
    assert resolved.content_sha256 == SHA
    assert resolved.size == 2048
    assert resolved.published_by == "Pat Doe"
    assert resolved.app_version == "1.2.0"
    assert resolved.permissions["commands"] == ["node server.js"]
    assert resolved.permissions["network"] == ["api.example.internal"]
    assert resolved.permissions["files"] == ["~/notes"]
    assert resolved.permissions["secrets"] == ["DIGEST_TOKEN"]
    # The API names no crons, MCP servers or agents yet: left empty, never guessed.
    for key in ("background", "mcpServers", "agents"):
        assert resolved.permissions[key] == []
    # A bundled component carries no path on the wire, so the installer's own
    # component check refuses it (fail closed) rather than skip it.
    assert resolved.components == ({"name": "digest", "kind": "skill", "sha256": COMP_SHA},)
    assert resolved.requires == ()


def test_resolve_install_without_version_sends_ref_only(server: FakeServer) -> None:
    server.replies["GetInstallPlan"] = _plan(_node())
    _client(server).resolve_install(REF, None)
    assert server.requests[0]["body"] == {"ref": REF}


def test_network_any_and_none_map_to_lists(server: FakeServer) -> None:
    server.replies["GetInstallPlan"] = _plan(_node(permissions=_perms(network="ANY")))
    assert _client(server).resolve_install(REF, None).permissions["network"] == ["*"]
    server.replies["GetInstallPlan"] = _plan(_node(permissions=_perms(network="NONE")))
    assert _client(server).resolve_install(REF, None).permissions["network"] == []


def test_dependencies_become_requires(server: FakeServer) -> None:
    server.replies["GetInstallPlan"] = _plan(
        _node(),
        _node(
            ref="acme/helper",
            listingId="lst_2",
            version=1,
            kind="mcp",
            reason="DEPENDENCY",
            requiredBy=[REF],
        ),
    )
    resolved = _client(server).resolve_install(REF, None)
    assert resolved.requires == ({"ref": "acme/helper", "version": 1, "kind": "mcp"},)


@pytest.mark.parametrize(
    "node",
    [
        _node(kind="skill"),
        _node(installable=False),
        _node(yanked=True),
        _node(sha256="not-a-hash"),
    ],
    ids=["not_an_app", "not_installable", "yanked", "bad_sha"],
)
def test_resolve_install_refuses_a_node_it_cannot_trust(
    server: FakeServer, node: dict[str, Any]
) -> None:
    server.replies["GetInstallPlan"] = _plan(node)
    with pytest.raises(MarketInstallError) as err:
        _client(server).resolve_install(REF, None)
    assert err.value.code == "market_resolve_invalid"


def test_resolve_install_refuses_a_plan_with_no_requested_node(server: FakeServer) -> None:
    server.replies["GetInstallPlan"] = _plan(_node(reason="DEPENDENCY", requiredBy=["x/y"]))
    with pytest.raises(MarketInstallError) as err:
        _client(server).resolve_install(REF, None)
    assert err.value.code == "market_resolve_invalid"


def test_api_refusal_names_the_server_code(server: FakeServer) -> None:
    server.replies["GetInstallPlan"] = (
        409,
        {"message": "version is not scanned yet", "code": "scan_pending"},
    )
    with pytest.raises(MarketInstallError) as err:
        _client(server).resolve_install(REF, None)
    assert err.value.code == "market_api_refused"
    assert "scan_pending" in err.value.message
    assert "409" in err.value.message


# ---------------------------------------------------------------------------
# GetListingDownload -> ListingDownload, and the signed-URL fetch
# ---------------------------------------------------------------------------


def _download_reply(**over: Any) -> tuple[int, dict[str, Any]]:
    return 200, {
        "url": "https://content.example/abc",
        "version": 3,
        "semver": "1.2.0",
        "sha256": SHA,
        "sizeBytes": 2048,
        "kind": "app",
        "counted": True,
        **over,
    }


def test_get_download_uses_the_resolved_listing_id(server: FakeServer) -> None:
    server.replies["GetInstallPlan"] = _plan(_node())
    server.replies["GetListingDownload"] = _download_reply()
    client = _client(server)
    client.resolve_install(REF, None)
    download = client.get_download(REF, 3)

    req = server.requests[1]
    assert req["path"] == "/service/KiroSpace/operation/GetListingDownload"
    assert req["body"] == {"listingId": "lst_1", "version": 3, "explicit": True}
    assert download.url == "https://content.example/abc"
    assert download.sha256 == SHA


def test_get_download_refuses_before_resolve_and_on_version_drift(server: FakeServer) -> None:
    client = _client(server)
    with pytest.raises(MarketInstallError) as err:
        client.get_download(REF, 3)
    assert err.value.code == "market_resolve_invalid"

    server.replies["GetInstallPlan"] = _plan(_node())
    server.replies["GetListingDownload"] = _download_reply(version=4)
    client.resolve_install(REF, None)
    with pytest.raises(MarketInstallError) as err:
        client.get_download(REF, 3)
    assert err.value.code == "market_download_refused"


def test_open_download_streams_bytes_without_the_bearer(server: FakeServer) -> None:
    server.blob = b"zip-bytes" * 1000
    got = b"".join(_client(server).open_download(f"{server.url}/blob"))
    assert got == server.blob
    assert "authorization" not in server.requests[0]["headers"]


def test_open_download_refuses_plain_http_off_loopback() -> None:
    client = HttpMarketClient("https://api.example", token=lambda: "t")
    with pytest.raises(MarketInstallError) as err:
        list(client.open_download("http://content.example/abc"))
    assert err.value.code == "market_download_refused"


@pytest.mark.parametrize("endpoint", ["http://api.example", "ftp://api.example", "api.example"])
def test_endpoint_must_be_https_off_loopback(endpoint: str) -> None:
    with pytest.raises(ValueError):
        HttpMarketClient(endpoint, token=lambda: "t")


def test_oversized_api_answer_is_refused(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market_client, "MAX_RPC_BYTES", 64)
    server.replies["GetInstallPlan"] = _plan(_node())
    with pytest.raises(MarketInstallError) as err:
        _client(server).resolve_install(REF, None)
    assert err.value.code == "market_api_invalid"


# ---------------------------------------------------------------------------
# Until GetInstallPlan names crons, MCP servers and agents, an app that has
# any is refused with a message naming each one (fail closed).
# ---------------------------------------------------------------------------


def test_app_with_crons_mcp_servers_or_agents_is_refused_while_the_api_lacks_them(
    server: FakeServer, tmp_path: Path
) -> None:
    server.replies["GetInstallPlan"] = _plan(_node())
    resolved = _client(server).resolve_install(REF, None)
    app_root = tmp_path / "app"
    (app_root / "agents").mkdir(parents=True)
    (app_root / "agents" / "digest.json").write_text(
        json.dumps({"name": "digest", "description": "d", "prompt": "hi"})
    )
    (app_root / "app.json").write_text(
        json.dumps(
            {
                "name": "ticket-digest",
                "version": "1.2.0",
                "displayName": "Ticket Digest",
                "description": "Summarise tickets",
                "author": "acme",
                "agents": ["agents/digest.json"],
                "crons": [{"name": "daily-digest", "every": 3600, "message": "digest"}],
                "mcpServers": {"tickets": {"command": "node", "args": ["mcp.js"]}},
                "permissions": {"cron": True},
            }
        )
    )
    with pytest.raises(MarketInstallError) as err:
        folder_capabilities(app_root, resolved)
    assert err.value.code == "market_undeclared_capability"
    msg = err.value.message
    assert "cron 'daily-digest'" in msg
    assert "MCP server 'tickets'" in msg
    assert "agent 'agents/digest.json'" in msg

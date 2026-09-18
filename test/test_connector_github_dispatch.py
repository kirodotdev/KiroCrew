"""W02 PR-3: a GitHub operation ACTUALLY invoked -- located, dispatched through
W01's real executor and transport, decoded, and paged -- with no naked sender.

The load-bearing test is :func:`test_real_structured_fetch_across_two_pages_over_tls`:
it drives a real :class:`~kiro_crew.connections.control_plane.executor.PageWalk`
of ``gh_list_pull_requests`` through the UNMODIFIED
:func:`~kiro_crew.connections.control_plane.production.urllib_http_send` over a
self-signed HTTPS loopback server that returns a ``Link: rel="next"`` header on
page 1 and none on page 2 -- a genuine two-page structured fetch. Custody is the
REAL, isolated, encrypted :class:`~kiro_crew.secrets.SecretVault`; no real
account, token, or org data is touched.

The rest prove the pieces in isolation with an injected fake sender (still going
through W01's real transport -- the fake replaces only the socket at the bottom,
never the auth/custody/decode chain): the locator shapes correctly and refuses a
shaping fault, the decoder reads the two GitHub pagination contracts honestly,
the descriptor projection is faithful, and multi-binding is enforced by W01's
per-binding selector, not a local substitute.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import ssl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import (
    Binding,
    VerifiedIdentity,
    binding_secret_ref,
    create_binding,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    derive_handle,
    ensure_usable,
)
from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import (
    BindingCustodyGate,
    HttpReply,
    HttpRequest,
    urllib_http_send,
)
from kiro_crew.connections.vendors.github.decoder import (
    GithubDecodeError,
    decode_cursor_page,
    decode_rest_page,
    decode_single,
)
from kiro_crew.connections.vendors.github.dispatch import (
    GITHUB_SERVICE_ID,
    GITHUB_SLUG,
    GithubDispatchError,
    build_github_transport,
    control_plane_descriptor,
    decode_for,
    dispatch_operation,
    open_page_walk,
    walk_pages,
)
from kiro_crew.connections.vendors.github.locator import (
    CURSOR_ARG,
    GITHUB_API_BASE,
    GithubLocatorError,
    build_request,
    locate,
)
from kiro_crew.secrets import SecretValue, SecretVault

_T0 = 1_000_000.0
_GRANTED: Tuple[str, ...] = ("repo", "read:org")


# =============================================================================
# control-plane fixtures (real binding / handle / selector, GitHub service)
# =============================================================================
def _verifier(*, claimed_subject: str, claimed_tenant: str, service_id: str) -> VerifiedIdentity:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _binding(*, subject: str = "octocat", tenant: str = "acme") -> Binding:
    return create_binding(
        service_id="github",
        claimed_subject=subject,
        claimed_tenant=tenant,
        credential_mode="oauth_user",
        verifier=_verifier,
        slug=GITHUB_SLUG,
    )


def _handle(binding: Binding, *, requested: Tuple[str, ...] = ("repo",)) -> DerivedHandle:
    return derive_handle(
        binding,
        granted_scopes=_GRANTED,
        requested_scopes=requested,
        now=_T0,
        ttl_seconds=3600.0,
    )


def _gate_for(binding: Binding, handle: DerivedHandle) -> BindingCustodyGate:
    """W01's per-binding custody gate: a function of the trusted handle view.

    ``trusted_binding_for`` returns THIS binding only when the executor-resolved
    view's identity matches the one composed here, else raises
    ``BindingIdentityMismatchError`` and resolves nothing (no store, no vault).
    """

    view = ensure_usable(handle, now=_T0)
    return BindingCustodyGate(
        binding=binding, binding_fingerprint=view.binding_fingerprint)


def _store_for(root: Path, *bindings: Binding) -> BindingStore:
    """A REAL on-disk L04 ``BindingStore`` holding ``bindings`` -- not a stub.

    ``build_production_transport`` resolves the credential per call via
    ``store.select_secret``, reading the ``secret_ref`` off the LIVE record, so
    the binding under custody must actually live in the store.
    """

    store = BindingStore(root / "connections" / "control_plane_bindings.json")
    for index, binding in enumerate(bindings):
        store.insert(
            binding,
            deployment_id=f"deployment://test/github/{index}",
            kiro_principal="kiro://test/owner",
        )
    return store


def _gate_kwargs(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),  # ungoverned == permit
        governance_scope="tools",
        governance_item="pulls.list",
    )
    base.update(over)
    return base


# =============================================================================
# a REAL isolated, encrypted vault holding the github binding secret
# =============================================================================
@pytest.fixture
def real_vault(tmp_path: Path) -> SecretVault:
    vault = SecretVault(tmp_path / "crewhome")
    vault.set_sync(binding_secret_ref(GITHUB_SLUG)["name"], "gh-installation-token")
    return vault


def test_the_vault_under_test_is_the_real_encrypted_store(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    """Guard for the guards: custody proofs below rest on the real vault."""

    store = tmp_path / "crewhome" / ".vault" / "secrets.enc"
    assert store.is_file()
    assert b"gh-installation-token" not in store.read_bytes()
    value = real_vault.get(binding_secret_ref(GITHUB_SLUG)["name"])
    assert value is not None and value.reveal() == "gh-installation-token"


# =============================================================================
# a REAL HTTPS loopback server (self-signed, minted per test)
# =============================================================================
def _tls_material(tmp_path: Path) -> Tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "loopback-cert.pem"
    keyfile = tmp_path / "loopback-key.pem"
    from cryptography.hazmat.primitives import serialization

    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


class _Recorder:
    def __init__(self) -> None:
        self.requests: List[Dict[str, str]] = []
        self.paths: List[str] = []


def _paging_handler(recorder: _Recorder, *, port_ref: Dict[str, int]):
    """A handler that pages: page 1 carries a Link->next, page 2 does not.

    Both pages return a GitHub-shaped pull-request array. Page 1's ``Link``
    header points at ``/page2`` on this same loopback origin, which is exactly
    the opaque continuation the decoder surfaces and the locator re-sends.
    """

    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            recorder.requests.append({k: v for k, v in self.headers.items()})
            recorder.paths.append(self.path)
            port = port_ref["port"]
            if self.path.startswith("/page2"):
                body = json.dumps([{"number": 2, "title": "second"}]).encode("utf-8")
                headers = {"Content-Type": "application/json"}
            else:
                body = json.dumps([{"number": 1, "title": "first"}]).encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "Link": f'<https://localhost:{port}/page2>; rel="next"',
                }
            self.send_response(200)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    return _H


@contextmanager
def _https_server(handler_cls: Any, certfile: Path, keyfile: Path) -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certfile), str(keyfile))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# =============================================================================
# THE load-bearing test: a real two-page structured fetch over real TLS
# =============================================================================
def test_real_structured_fetch_across_two_pages_over_tls(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))

    rec = _Recorder()
    port_ref: Dict[str, int] = {"port": 0}
    handler = _paging_handler(rec, port_ref=port_ref)

    binding = _binding()
    handle = _handle(binding)
    gate = _gate_for(binding, handle)
    store = _store_for(tmp_path, binding)
    # W01 mints a per-binding secret name (no slug collapse), so seed the vault
    # under THIS binding's own secret_ref name -- the fixture's slug-derived name
    # would miss and the transport would return HTTP 401.
    real_vault.set_sync(binding["secret_ref"]["name"], "gh-installation-token")

    with _https_server(handler, certfile, keyfile) as port:
        port_ref["port"] = port

        # Point the locator's REST base at the loopback origin so PAGE 1 is built
        # normally (endpoint template + page/perPage), then PAGE 2 is driven by
        # the Link-header cursor the server returns. Both requests travel through
        # the UNMODIFIED urllib_http_send over real TLS.
        import kiro_crew.connections.vendors.github.locator as gh_locator

        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")

        transport = build_github_transport(
            operation_id="gh_list_pull_requests",
            gate=gate,
            store=store,
            vault=real_vault,
            http_send=urllib_http_send,
        )
        walk = open_page_walk(
            operation_id="gh_list_pull_requests",
            handle=handle,
            transport=transport,
            base_args={"owner": "octo", "repo": "hello", "state": "open"},
            clock=lambda: _T0,
            **_gate_kwargs(),
        )
        outcomes = walk_pages(walk)

    # Two real pages were fetched over TLS.
    assert walk.pages == 2
    assert len(outcomes) == 2
    assert all(o.ok for o in outcomes)
    # Page 1 was partial (a next cursor remained); page 2 completed the walk.
    assert outcomes[0].result is not None and outcomes[0].result["status"] == "partial"
    assert outcomes[0].result["next_cursor"] == f"https://localhost:{port}/page2"
    assert outcomes[1].result is not None and outcomes[1].result["status"] == "ok"
    assert outcomes[1].result["next_cursor"] is None
    # The server saw two hits: page 1 built from the endpoint template, page 2
    # from the Link-header cursor -- a genuine cursor advance.
    assert len(rec.paths) == 2
    assert rec.paths[0].startswith("/repos/octo/hello/pulls")
    assert rec.paths[1].startswith("/page2")
    # The credential the transport resolved from the REAL vault reached the wire
    # as a bearer header -- proving custody ran end to end, no naked sender.
    auths = [
        next((v for k, v in req.items() if k.lower() == "authorization"), None)
        for req in rec.requests
    ]
    assert auths == ["Bearer gh-installation-token", "Bearer gh-installation-token"]


# =============================================================================
# locator: shaping is correct and refuses shaping faults
# =============================================================================
def test_locator_builds_rest_list_first_page() -> None:
    req = locate(
        service_id="github",
        credential_mode="oauth_user",
        descriptor={"operation_id": "gh_list_pull_requests"},
        request_args={"owner": "octo", "repo": "hello", "state": "open"},
    )
    assert req.method == "GET"
    assert req.url.startswith(f"{GITHUB_API_BASE}/repos/octo/hello/pulls?")
    assert "page=1" in req.url and "perPage=30" in req.url and "state=open" in req.url
    # No credential in a locator-built request.
    assert "Authorization" not in req.headers


def test_locator_rest_cursor_is_the_absolute_link_url_verbatim() -> None:
    next_url = "https://api.github.com/repositories/1/pulls?page=2"
    req = build_request(
        descriptor=__import__(
            "kiro_crew.connections.vendors.github.descriptors",
            fromlist=["get_descriptor"],
        ).get_descriptor("gh_list_pull_requests"),
        request_args={CURSOR_ARG: next_url},
    )
    assert req.url == next_url  # sent verbatim, not re-derived


def test_locator_refuses_cross_origin_pagination_cursor() -> None:
    # A crafted Link header pointing at another origin must be REFUSED: the
    # production transport would otherwise attach the binding's bearer token to
    # the attacker host. A scheme check alone is not enough -- the origin
    # (scheme+host+port) must equal GITHUB_API_BASE's.
    descriptor = __import__(
        "kiro_crew.connections.vendors.github.descriptors",
        fromlist=["get_descriptor"],
    ).get_descriptor("gh_list_pull_requests")
    for evil in (
        "https://evil.example.com/repositories/1/pulls?page=2",
        "https://api.github.com.evil.com/x?page=2",  # look-alike host prefix
        "https://api.github.com:8443/x?page=2",       # wrong port
        "http://api.github.com/x?page=2",             # wrong scheme
    ):
        with pytest.raises(GithubLocatorError):
            build_request(descriptor=descriptor, request_args={CURSOR_ARG: evil})


def test_locator_refuses_unknown_operation() -> None:
    with pytest.raises(GithubLocatorError):
        locate(
            service_id="github",
            credential_mode="oauth_user",
            descriptor={"operation_id": "gh_not_a_real_op"},
            request_args={},
        )


def test_locator_refuses_a_non_github_service() -> None:
    with pytest.raises(GithubLocatorError):
        locate(
            service_id="outlook",
            credential_mode="oauth_user",
            descriptor={"operation_id": "gh_list_pull_requests"},
            request_args={},
        )


def test_locator_refuses_path_separator_in_id_segment() -> None:
    with pytest.raises(GithubLocatorError):
        locate(
            service_id="github",
            credential_mode="oauth_user",
            descriptor={"operation_id": "gh_list_pull_requests"},
            request_args={"owner": "octo", "repo": "a/b"},
        )


def test_locator_refuses_missing_path_parameter() -> None:
    with pytest.raises(GithubLocatorError):
        locate(
            service_id="github",
            credential_mode="oauth_user",
            descriptor={"operation_id": "gh_list_pull_requests"},
            request_args={"owner": "octo"},  # missing repo
        )


# =============================================================================
# decoder: the two pagination contracts, read honestly
# =============================================================================
def test_decode_rest_page_surfaces_link_next_as_cursor() -> None:
    reply = HttpReply(
        status=200,
        headers={"Link": '<https://api.github.com/x?page=2>; rel="next"'},
        body=b"[]",
    )
    result = decode_rest_page(reply)
    assert result["status"] == "partial"
    assert result["next_cursor"] == "https://api.github.com/x?page=2"


def test_decode_rest_page_terminal_when_no_next_link() -> None:
    reply = HttpReply(status=200, headers={}, body=b"[]")
    result = decode_rest_page(reply)
    assert result["status"] == "ok" and result["next_cursor"] is None


def test_decode_cursor_page_reads_pageinfo_endcursor() -> None:
    body = json.dumps(
        {"data": {"repository": {"issues": {"pageInfo": {"hasNextPage": True, "endCursor": "Y3Vyc29y"}}}}}
    ).encode("utf-8")
    result = decode_cursor_page(HttpReply(status=200, headers={}, body=body))
    assert result["status"] == "partial" and result["next_cursor"] == "Y3Vyc29y"


def test_decode_cursor_page_terminal_when_no_next() -> None:
    body = json.dumps({"data": {"x": {"pageInfo": {"hasNextPage": False, "endCursor": None}}}}).encode(
        "utf-8"
    )
    result = decode_cursor_page(HttpReply(status=200, headers={}, body=body))
    assert result["status"] == "ok" and result["next_cursor"] is None


def test_decode_cursor_page_refuses_malformed_pageinfo() -> None:
    body = json.dumps({"pageInfo": "not-an-object"}).encode("utf-8")
    with pytest.raises(GithubDecodeError):
        decode_cursor_page(HttpReply(status=200, headers={}, body=body))


def test_decode_single_has_no_cursor() -> None:
    result = decode_single(HttpReply(status=200, headers={}, body=b'{"name":"file"}'))
    assert result["status"] == "ok" and result["next_cursor"] is None


# =============================================================================
# descriptor projection + decoder selection
# =============================================================================
def test_control_plane_descriptor_is_faithful_to_github_facts() -> None:
    d = control_plane_descriptor("gh_list_pull_requests")
    assert d["operation_id"] == "gh_list_pull_requests"
    assert d["service_id"] == GITHUB_SERVICE_ID
    assert d["operation_kind"] == "list"  # paginated read
    assert d["effect"] == "read"
    assert "oauth_user" in d["credential_modes"]


def test_control_plane_descriptor_mutation_kind() -> None:
    d = control_plane_descriptor("gh_issue_write_create")
    assert d["operation_kind"] == "mutation" and d["effect"] == "write"


def test_control_plane_descriptor_single_fetch_kind() -> None:
    d = control_plane_descriptor("gh_get_file_contents")
    assert d["operation_kind"] == "single_fetch"


def test_decode_for_matches_contract() -> None:
    assert decode_for("gh_list_pull_requests") is decode_rest_page
    assert decode_for("gh_list_issues") is decode_cursor_page
    assert decode_for("gh_get_file_contents") is decode_single


def test_decode_for_refuses_mixed() -> None:
    with pytest.raises(GithubDispatchError):
        decode_for("gh_pull_request_read")  # MIXED pagination


# =============================================================================
# dispatch drives the REAL gate chain: a denied gate emits nothing
# =============================================================================
def test_dispatch_denied_gate_never_reaches_transport(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    sent: List[HttpRequest] = []

    def _spy_send(request: HttpRequest, **_: Any) -> HttpReply:
        sent.append(request)
        return HttpReply(status=200, headers={}, body=b"[]")

    binding = _binding()
    handle = _handle(binding)
    gate = _gate_for(binding, handle)
    store = _store_for(tmp_path, binding)
    transport = build_github_transport(
        operation_id="gh_list_pull_requests",
        gate=gate,
        store=store,
        vault=real_vault,
        http_send=_spy_send,
    )
    # Offer a mode the policy does not permit -> the credential-mode gate denies
    # inside execute(), so the transport (and the sender) is never reached.
    outcome = dispatch_operation(
        operation_id="gh_list_pull_requests",
        handle=handle,
        transport=transport,
        **_gate_kwargs(
            offered_mode="fine_grained_pat",
            permitted=declare_permitted_modes(("oauth_user",)),
        ),
        request_args={"owner": "o", "repo": "r"},
        clock=lambda: _T0,
    )
    assert outcome.error is not None
    assert sent == []  # naked-sender check: nothing sent on a denied gate


# =============================================================================
# multi-binding: W01's per-binding selector, not a local substitute
# =============================================================================
def test_wrong_binding_gate_refuses_and_sends_nothing(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    sent: List[HttpRequest] = []

    def _spy_send(request: HttpRequest, **_: Any) -> HttpReply:
        sent.append(request)
        return HttpReply(status=200, headers={}, body=b"[]")

    # A handle for binding A, but a gate composed for a DIFFERENT binding B.
    handle_a = _handle(_binding(subject="a"))
    binding_b = _binding(subject="b")
    handle_b = _handle(binding_b)
    gate_b = _gate_for(binding_b, handle_b)
    store = _store_for(tmp_path, binding_b)

    transport = build_github_transport(
        operation_id="gh_list_pull_requests",
        gate=gate_b,  # custody for B
        store=store,
        vault=real_vault,
        http_send=_spy_send,
    )
    outcome = dispatch_operation(
        operation_id="gh_list_pull_requests",
        handle=handle_a,  # call authorized for A
        transport=transport,
        **_gate_kwargs(),
        request_args={"owner": "o", "repo": "r"},
        clock=lambda: _T0,
    )
    # W01's BindingCustodyGate refuses (BindingIdentityMismatchError) -> transport
    # returns a typed auth failure, never sent anything nor resolved a secret.
    assert outcome.error is not None
    assert sent == []


def test_schema_versions_are_the_ones_this_builds_against(tmp_path: Path) -> None:
    # build_github_transport asserts these; call it and confirm no drift raised.
    binding = _binding()
    handle = _handle(binding)
    transport = build_github_transport(
        operation_id="gh_list_pull_requests",
        gate=_gate_for(binding, handle),
        store=_store_for(tmp_path, binding),
        vault=SecretVault_stub(),
        http_send=lambda request, **_: HttpReply(status=200, headers={}, body=b"[]"),
    )
    assert callable(transport)


class SecretVault_stub:
    """A minimal SecretStore stub for the schema-version smoke test only."""

    def get(self, name: str) -> Optional[SecretValue]:
        return SecretValue("x")


# =============================================================================
# F4: a large listing (>100 pages) must COMPLETE, not abort every round
# =============================================================================
class _FakeWalk:
    """A minimal PageWalk stand-in: yields ``pages`` non-terminal pages, then
    sets done. Terminates via its OWN done flag (as W01's repeated-cursor guard
    would), so walk_pages must pump it to the end without a page-count cap."""

    def __init__(self, pages: int) -> None:
        self._remaining = pages
        self.done = False

    def next(self):  # noqa: A003 - mirrors PageWalk.next
        self._remaining -= 1
        if self._remaining <= 0:
            self.done = True
        return object()  # an opaque per-page outcome; walk_pages only collects


def test_walk_pages_completes_a_listing_over_100_pages() -> None:
    # 250 pages > the old max_pages=100 ceiling that used to abort. walk_pages
    # must drive it to completion (W01's done flag is the terminator).
    walk = _FakeWalk(pages=250)
    outcomes = walk_pages(walk)
    assert len(outcomes) == 250
    assert walk.done


def test_walk_pages_runaway_ceiling_still_guards_a_nonterminating_walk() -> None:
    # A walk that NEVER sets done (a provider defeating W01's guard) must still
    # be bounded by the last-resort runaway ceiling rather than loop forever.
    class _NeverDone:
        done = False

        def next(self):  # noqa: A003
            return object()

    with pytest.raises(GithubDispatchError):
        walk_pages(_NeverDone(), runaway_ceiling=32)


# =============================================================================
# F3: the issues/pulls list must carry state=all so a CLOSE is not filtered out
# =============================================================================
def test_locator_passes_state_filter_into_issues_query() -> None:
    descriptor = __import__(
        "kiro_crew.connections.vendors.github.descriptors",
        fromlist=["get_descriptor"],
    ).get_descriptor("gh_list_issues_rest")
    req = build_request(
        descriptor=descriptor,
        request_args={"owner": "octo", "repo": "hello", "state": "all"},
    )
    assert "state=all" in req.url  # a supported filter, forwarded verbatim

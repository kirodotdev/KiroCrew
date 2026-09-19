"""W01 · L09: the four PRODUCTION boundaries, proved without an all-mock seam.

The executor's own tests inject a fake transport, and the composition tests next
to them inject a fake vault and a fake sender. That is the right shape for
proving a DECISION -- but it cannot prove a WIRE property. A fake sender that
never opens a socket cannot tell you whether ``urllib`` forwards an
``Authorization`` header across a redirect (it does), and a stub vault that
returns whatever you seeded it with cannot tell you whether the custody path
resolves the right binding's entry out of a real encrypted store.

So the four boundaries here are exercised against real things:

* a REAL :class:`kiro_crew.secrets.SecretVault` -- an AES-256-GCM store on disk in
  a per-test ``tmp_path``, isolated, created and keyed by the vault itself;
* a REAL HTTPS loopback server -- ``http.server`` behind a TLS socket with a
  self-signed certificate minted in the test, driven by the UNMODIFIED
  :func:`~kiro_crew.connections.control_plane.production.urllib_http_send` over
  real sockets, so the redirect machinery, the body read, the size cap and the
  deadline are the real ones.

Each defect these pin was reproduced first. The redirect leak in particular was
observed on the wire: with the default opener, a 302 from one loopback origin to
another delivered ``Bearer <token>`` to the second server.
"""

from __future__ import annotations

import datetime
import http.client
import http.server
import socket
import ssl
import threading
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import (
    Binding,
    VerifiedIdentity,
    binding_secret_ref,
    create_binding,
)
from kiro_crew.connections.control_plane.executor import (
    EXECUTOR_SCHEMA_VERSION,
    TransportResponse,
    execute,
    is_non_idempotent_effect,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    derive_handle,
    ensure_usable,
)
from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.operation import Effect, OperationDescriptor
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_MAX_RESPONSE_BYTES,
    PRODUCTION_SCHEMA_VERSION,
    BindingCustodyGate,
    BindingIdentityMismatchError,
    HttpReply,
    HttpRequest,
    MalformedResponseBodyError,
    RedirectHop,
    RedirectRefusedError,
    ResponseTooLargeError,
    SecretResolutionError,
    TransportDeadlineExceededError,
    build_production_transport,
    decode_json_body,
    neutral_decode,
    neutral_decode_detail,
    urllib_http_send,
)
from kiro_crew.connections.control_plane.result import (
    DEFAULT_MEDIA_TYPE,
    RESULT_SCHEMA_VERSION,
    RESULT_STATUS_OK,
    RESULT_STATUS_PARTIAL,
    RESULT_STATUSES,
    BytesPayload,
)
from kiro_crew.connections.control_plane.writes import (
    ATTEMPT_FAILED_NOT_APPLIED,
    ATTEMPT_UNKNOWN,
    REPLAY_ALLOW,
    REPLAY_REFUSE,
    args_fingerprint,
    record_attempt,
    replay_decision,
)
from kiro_crew.secrets import SecretValue, SecretVault

_T0 = 1_000_000.0
_GRANTED = ("mail.read", "mail.send")


# =============================================================================
# shared control-plane fixtures (the decision side, kept minimal)
# =============================================================================
def _verifier(*, claimed_subject: str, claimed_tenant: str, service_id: str) -> VerifiedIdentity:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _binding(*, subject: str = "alice", tenant: str = "acme", slug: str = "outlook") -> Binding:
    return create_binding(
        service_id="outlook",
        claimed_subject=subject,
        claimed_tenant=tenant,
        credential_mode="oauth_user",
        verifier=_verifier,
        slug=slug,
    )


def _handle(binding: Binding, *, requested: Tuple[str, ...] = ("mail.read",)) -> DerivedHandle:
    return derive_handle(
        binding,
        granted_scopes=_GRANTED,
        requested_scopes=requested,
        now=_T0,
        ttl_seconds=300.0,
    )


#: The default binding, minted ONCE by the real constructor so the real_vault
#: fixture can seed the credential under its OWN per-binding scoped secret_ref
#: name (read off the constructor's record), never a slug name.
_DEFAULT_BINDING: Binding = _binding()


def _gate_for(binding: Binding, handle: DerivedHandle) -> BindingCustodyGate:
    """The custody gate a transport is composed with FOR ``handle``'s binding."""

    view = ensure_usable(handle, now=_T0)
    return BindingCustodyGate(binding=binding, binding_fingerprint=view.binding_fingerprint)


def _live_store(root: Path, *bindings: Binding) -> BindingStore:
    """A REAL on-disk L04 BindingStore holding ``bindings``.

    Not a stub: the file, the uniqueness domain, the lock and the fence are the
    product's own, so what the send path proves about the live-store fence is a
    property of the real store.
    """

    store = BindingStore(root / "connections" / "control_plane_bindings.json")
    for index, binding in enumerate(bindings):
        store.insert(
            binding,
            deployment_id=f"deployment://test/graph/{index}",
            kiro_principal="kiro://test/owner",
        )
    return store


def _bound(**kw: Any) -> Tuple[Binding, DerivedHandle]:
    """A binding plus a handle derived from it (the binding is what gets fenced)."""

    identity_kw = {k: v for k, v in kw.items() if k in ("subject", "tenant", "slug")}
    # The default (no identity override) binding is the shared _DEFAULT_BINDING the
    # real_vault fixture seeds under its OWN per-binding scoped name -- so the
    # per-binding path is exercised, not a slug path. A test that overrides the
    # identity mints its own binding and seeds its own name.
    binding = dict(_DEFAULT_BINDING) if not identity_kw else _binding(**identity_kw)  # type: ignore[assignment]
    requested = kw.get("requested", ("mail.read",))
    return binding, _handle(binding, requested=requested)  # type: ignore[arg-type]


def _descriptor(effect: Effect = "read") -> OperationDescriptor:
    return {
        "operation_id": "outlook.messages.list",
        "service_id": "outlook",
        "operation_kind": "list",
        "effect": effect,
        "credential_modes": ("oauth_user",),
    }


def _kw(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(
        now=_T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),  # all None == ungoverned == permit
        governance_scope="tools",
        governance_item="messages.list",
    )
    base.update(over)
    return base


def _locator_to(url: str, *, method: str = "GET", body: Optional[bytes] = None):
    def _locate(**kwargs: Any) -> HttpRequest:
        return HttpRequest(
            method=method, url=url, headers={"Accept": "application/json"}, body=body
        )

    return _locate


# =============================================================================
# a REAL isolated SecretVault (encrypted store on disk, per test)
# =============================================================================
class RecordingVault(SecretVault):
    """The REAL vault, with reads recorded.

    A subclass rather than a stub on purpose: the store, the key file and the
    AES-256-GCM crypto are the product's own, so what this proves about custody is
    a property of the real path. The override records the name and then delegates,
    so ``asked`` is the honest answer to "was the vault consulted, and for what" --
    which is exactly what the binding-mismatch counterexample needs to assert
    NEGATIVELY.
    """

    def __init__(self, config_dir: Path) -> None:
        super().__init__(config_dir)
        self.asked: List[str] = []

    def get(self, name: str) -> Optional[SecretValue]:
        self.asked.append(name)
        return super().get(name)


@pytest.fixture
def real_vault(tmp_path: Path) -> RecordingVault:
    """A real, isolated, encrypted vault holding the outlook binding secret.

    Seeded under the shared default binding's OWN per-binding scoped secret_ref
    name, so the fenced ``select_secret`` read resolves the per-binding entry.
    """

    vault = RecordingVault(tmp_path / "crewhome")
    vault.set_sync(_DEFAULT_BINDING["secret_ref"]["name"], "outlook-live-token")
    vault.asked.clear()
    return vault


def test_the_real_vault_under_test_is_an_encrypted_store_on_disk(
    tmp_path: Path, real_vault: RecordingVault
) -> None:
    """Guard for the guards: if this were a stub, nothing below would prove custody."""

    store = tmp_path / "crewhome" / ".vault" / "secrets.enc"
    key = tmp_path / "crewhome" / ".vault" / ".vault_key"
    assert store.is_file() and key.is_file()
    raw = store.read_bytes()
    # The plaintext is NOT on disk: an encrypted store, not a JSON file.
    assert b"outlook-live-token" not in raw
    assert isinstance(real_vault, SecretVault)
    # And it round-trips through the real crypto.
    value = real_vault.get(_DEFAULT_BINDING["secret_ref"]["name"])
    assert value is not None and value.reveal() == "outlook-live-token"


# =============================================================================
# a REAL HTTPS loopback server (self-signed, minted per test)
# =============================================================================
def _tls_material(tmp_path: Path) -> Tuple[Path, Path]:
    """Mint a self-signed cert valid for ``localhost`` / ``127.0.0.1``.

    Self-signed means the cert is its own CA, so pointing ``SSL_CERT_FILE`` at it
    is enough to make the stdlib client trust it -- with hostname verification and
    certificate validation left ON, which is what keeps this a real TLS path
    rather than a disabled one.
    """

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
                    x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "loopback-cert.pem"
    keyfile = tmp_path / "loopback-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


class _Recorder:
    """What each loopback server actually received."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, str]] = []

    @property
    def hits(self) -> int:
        return len(self.requests)

    def authorization_seen(self) -> List[Optional[str]]:
        return [
            next((v for k, v in req.items() if k.lower() == "authorization"), None)
            for req in self.requests
        ]


def _handler_for(
    recorder: _Recorder,
    *,
    reply: Callable[[], Tuple[int, Dict[str, str], bytes]],
    pause_before_body: float = 0.0,
):
    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self) -> None:
            recorder.requests.append({k: v for k, v in self.headers.items()})
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            status, headers, body = reply()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if status not in (204, 304):
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if pause_before_body:
                self.wfile.flush()
                time.sleep(pause_before_body)
            if status not in (204, 304) and body:
                self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond

        def log_message(self, *args: Any) -> None:
            return

    return _H


@contextmanager
def _https_server(handler_cls: Any, certfile: Path, keyfile: Path) -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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


@pytest.fixture
def trust_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[Path, Path]:
    """TLS material for the loopback servers, trusted by the stdlib client."""

    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    return certfile, keyfile


def test_the_loopback_harness_really_speaks_tls_to_the_real_sender(
    trust_loopback: Tuple[Path, Path],
) -> None:
    """Guard for the guards: the redirect tests below must not be sending over http."""

    certfile, keyfile = trust_loopback
    rec = _Recorder()
    handler = _handler_for(rec, reply=lambda: (200, {}, b'{"ok":true}'))
    with _https_server(handler, certfile, keyfile) as port:
        reply = urllib_http_send(
            HttpRequest(method="GET", url=f"https://localhost:{port}/probe", headers={}),
            timeout_seconds=10.0,
        )
    assert reply.status == 200 and reply.body == b'{"ok":true}'
    assert rec.hits == 1


# =============================================================================
# DEFECT A -- the credential must never leave its origin
# =============================================================================
def test_a_cross_origin_redirect_is_refused_by_default_and_the_target_sees_nothing(
    trust_loopback: Tuple[Path, Path],
) -> None:
    """The pre-fix behaviour, on this exact harness, delivered Bearer to hop 2."""

    certfile, keyfile = trust_loopback
    target_rec, origin_rec = _Recorder(), _Recorder()
    target_handler = _handler_for(target_rec, reply=lambda: (200, {}, b"{}"))
    with _https_server(target_handler, certfile, keyfile) as target_port:
        origin_handler = _handler_for(
            origin_rec,
            reply=lambda: (302, {"Location": f"https://localhost:{target_port}/next"}, b""),
        )
        with _https_server(origin_handler, certfile, keyfile) as origin_port:
            with pytest.raises(RedirectRefusedError) as caught:
                urllib_http_send(
                    HttpRequest(
                        method="GET",
                        url=f"https://localhost:{origin_port}/start",
                        headers={"Authorization": "Bearer LEAK-CANARY"},
                    ),
                    timeout_seconds=10.0,
                )
    # Hop 1 got the credential (it is the origin the handle authorized).
    assert origin_rec.authorization_seen() == ["Bearer LEAK-CANARY"]
    # Hop 2 was never even contacted: no request, so nothing to leak.
    assert target_rec.hits == 0
    assert "follows no redirects" in str(caught.value)


def test_a_malformed_request_url_is_refused_before_send_not_crashed() -> None:
    """F4: a request URL whose port cannot be parsed is a typed pre-send refusal.

    `_origin_of(request.url)` runs while building the opener, before any socket.
    A non-numeric port (`https://host:notaport/`) makes `urlsplit(...).port` raise
    `ValueError`; pre-fix that escaped `urllib_http_send` untyped. It is now turned
    into a `SecretResolutionError` -- the same typed pre-send refusal class as the
    non-https guard -- so no socket is opened and nothing crashes.
    """

    with pytest.raises(SecretResolutionError) as caught:
        urllib_http_send(
            HttpRequest(
                method="GET",
                url="https://host:notaport/v1/me",
                headers={"Authorization": "Bearer LEAK-CANARY"},
            ),
            timeout_seconds=10.0,
        )
    assert "could not be parsed" in str(caught.value)


def test_a_malformed_redirect_target_is_refused_not_crashed(
    trust_loopback: Tuple[Path, Path],
) -> None:
    """F2: a redirect whose URL cannot be parsed is a typed refusal, not a crash.

    A provider answering ``Location: https://host:notaport/`` makes
    ``urlsplit(...).port`` raise ``ValueError``. Pre-fix that escaped the transport
    uncaught (a crashed dispatch); post-fix it becomes a ``RedirectRefusedError``
    -- the class every other unfollowable redirect yields -- so the caller sees a
    typed refusal and hop 2 is never contacted.
    """

    certfile, keyfile = trust_loopback
    origin_rec = _Recorder()
    with _https_server(
        _handler_for(
            origin_rec,
            reply=lambda: (302, {"Location": "https://host:notaport/next"}, b""),
        ),
        certfile,
        keyfile,
    ) as origin_port:
        with pytest.raises(RedirectRefusedError) as caught:
            urllib_http_send(
                HttpRequest(
                    method="GET",
                    url=f"https://localhost:{origin_port}/start",
                    headers={"Authorization": "Bearer LEAK-CANARY"},
                ),
                timeout_seconds=10.0,
                # An allowlist is injected so the send gets PAST the follow-nothing
                # gate to the per-hop parse -- which is where the malformed target
                # makes urlsplit(...).port raise. (The malformed host would never be
                # in a real allowlist; what is under test is that the PARSE failure
                # is a typed refusal, reached before the allowlist membership check.)
                allowed_redirect_hosts=frozenset({f"localhost:{origin_port}", "host:notaport"}),
            )
    # A typed refusal that names the parse failure, not a raw ValueError.
    assert "could not be parsed" in str(caught.value)
    # Hop 1 saw the credential (its authorized origin); nothing leaked past it.
    assert origin_rec.authorization_seen() == ["Bearer LEAK-CANARY"]


def test_an_allowlisted_cross_origin_hop_travels_with_the_credential_stripped(
    trust_loopback: Tuple[Path, Path],
) -> None:
    """Rule 4, and the one that matters: allowlisted is not the same as trusted."""

    certfile, keyfile = trust_loopback
    target_rec, origin_rec = _Recorder(), _Recorder()
    target_handler = _handler_for(target_rec, reply=lambda: (200, {}, b'{"v":1}'))
    with _https_server(target_handler, certfile, keyfile) as target_port:
        origin_handler = _handler_for(
            origin_rec,
            reply=lambda: (302, {"Location": f"https://localhost:{target_port}/next"}, b""),
        )
        with _https_server(origin_handler, certfile, keyfile) as origin_port:
            hops: List[RedirectHop] = []
            reply = urllib_http_send(
                HttpRequest(
                    method="GET",
                    url=f"https://localhost:{origin_port}/start",
                    headers={
                        "Authorization": "Bearer LEAK-CANARY",
                        "Cookie": "session=CANARY",
                        "X-Api-Key": "CANARY",
                        "Accept": "application/json",
                    },
                ),
                timeout_seconds=10.0,
                allowed_redirect_hosts=frozenset(
                    {f"localhost:{origin_port}", f"localhost:{target_port}"}
                ),
                hop_log=hops,
            )

    assert reply.status == 200 and reply.body == b'{"v":1}'
    # The hop happened, off-origin, and NO credential header rode along.
    assert [(h.same_origin, h.credential_forwarded) for h in hops] == [(False, False)]
    assert hops[0].to_url.endswith("/next")
    # Observed at the second server, not inferred from the hop record.
    received = target_rec.requests[0]
    for header in ("authorization", "cookie", "x-api-key"):
        assert not any(k.lower() == header for k in received), header
    # A non-credential header is NOT stripped -- the guard is targeted.
    assert any(k.lower() == "accept" for k in received)


def test_a_same_origin_redirect_keeps_the_credential(trust_loopback: Tuple[Path, Path]) -> None:
    """The credential may follow within the origin it was authorized for."""

    certfile, keyfile = trust_loopback
    rec = _Recorder()
    replies = iter([(302, {"Location": "/second"}, b""), (200, {}, b'{"v":2}')])
    handler = _handler_for(rec, reply=lambda: next(replies))
    with _https_server(handler, certfile, keyfile) as port:
        hops: List[RedirectHop] = []
        reply = urllib_http_send(
            HttpRequest(
                method="GET",
                url=f"https://localhost:{port}/first",
                headers={"Authorization": "Bearer SAME-ORIGIN-OK"},
            ),
            timeout_seconds=10.0,
            allowed_redirect_hosts=frozenset({f"localhost:{port}"}),
            hop_log=hops,
        )
    assert reply.status == 200 and reply.body == b'{"v":2}'
    assert [(h.same_origin, h.credential_forwarded) for h in hops] == [(True, True)]
    assert rec.authorization_seen() == ["Bearer SAME-ORIGIN-OK", "Bearer SAME-ORIGIN-OK"]


def test_an_https_to_http_downgrade_hop_is_refused_even_when_allowlisted(
    trust_loopback: Tuple[Path, Path],
) -> None:
    """Rule 2 is independent of rule 3: an allowlist entry does not license clear text."""

    certfile, keyfile = trust_loopback
    plain_rec = _Recorder()
    plain_handler = _handler_for(plain_rec, reply=lambda: (200, {}, b"{}"))
    plain = http.server.ThreadingHTTPServer(("127.0.0.1", 0), plain_handler)
    plain_port = int(plain.server_address[1])
    plain_thread = threading.Thread(target=plain.serve_forever, daemon=True)
    plain_thread.start()
    try:
        origin_rec = _Recorder()
        origin_handler = _handler_for(
            origin_rec,
            reply=lambda: (302, {"Location": f"http://localhost:{plain_port}/next"}, b""),
        )
        with _https_server(origin_handler, certfile, keyfile) as origin_port:
            with pytest.raises(RedirectRefusedError) as caught:
                urllib_http_send(
                    HttpRequest(
                        method="GET",
                        url=f"https://localhost:{origin_port}/start",
                        headers={"Authorization": "Bearer LEAK-CANARY"},
                    ),
                    timeout_seconds=10.0,
                    # Explicitly allowlisted, and STILL refused.
                    allowed_redirect_hosts=frozenset({f"localhost:{plain_port}"}),
                )
    finally:
        plain.shutdown()
        plain.server_close()
        plain_thread.join(timeout=5)
    assert plain_rec.hits == 0
    assert "would leave https" in str(caught.value)


def test_a_hop_off_the_allowlist_is_refused(trust_loopback: Tuple[Path, Path]) -> None:
    """Rule 3, default-deny: absence from the allowlist is a refusal."""

    certfile, keyfile = trust_loopback
    target_rec, origin_rec = _Recorder(), _Recorder()
    target_handler = _handler_for(target_rec, reply=lambda: (200, {}, b"{}"))
    with _https_server(target_handler, certfile, keyfile) as target_port:
        origin_handler = _handler_for(
            origin_rec,
            reply=lambda: (302, {"Location": f"https://localhost:{target_port}/next"}, b""),
        )
        with _https_server(origin_handler, certfile, keyfile) as origin_port:
            with pytest.raises(RedirectRefusedError) as caught:
                urllib_http_send(
                    HttpRequest(
                        method="GET",
                        url=f"https://localhost:{origin_port}/start",
                        headers={"Authorization": "Bearer LEAK-CANARY"},
                    ),
                    timeout_seconds=10.0,
                    allowed_redirect_hosts=frozenset({f"localhost:{origin_port}"}),
                )
    assert target_rec.hits == 0
    assert "not in this send's egress allowlist" in str(caught.value)


def test_a_refused_redirect_reaches_the_executor_as_a_typed_error(
    tmp_path: Path, real_vault: RecordingVault, trust_loopback: Tuple[Path, Path]
) -> None:
    """End to end: the transport returns an envelope, it does not raise."""

    certfile, keyfile = trust_loopback
    target_rec, origin_rec = _Recorder(), _Recorder()
    target_handler = _handler_for(target_rec, reply=lambda: (200, {}, b"{}"))
    with _https_server(target_handler, certfile, keyfile) as target_port:
        origin_handler = _handler_for(
            origin_rec,
            reply=lambda: (302, {"Location": f"https://localhost:{target_port}/next"}, b""),
        )
        with _https_server(origin_handler, certfile, keyfile) as origin_port:
            binding, handle = _bound()
            transport = build_production_transport(
                gate=_gate_for(binding, handle),
                store=_live_store(tmp_path, binding),
                vault=real_vault,
                locator=_locator_to(f"https://localhost:{origin_port}/start"),
            )
            outcome = execute(_descriptor(), handle, transport, **_kw())

    assert outcome.error is not None
    assert outcome.error["error_class"] == "input"
    assert target_rec.hits == 0
    # The real vault WAS read (the credential is needed for hop 1) and the token
    # reached only the origin.
    assert real_vault.asked == [_DEFAULT_BINDING["secret_ref"]["name"]]
    assert origin_rec.authorization_seen() == ["Bearer outlook-live-token"]
    # A read has no effect to be uncertain about.
    assert outcome.write_outcome is None


# =============================================================================
# DEFECT B -- the secret is selected from the call's trusted binding
# =============================================================================
def test_the_real_vault_resolves_this_bindings_secret_for_a_matching_call(
    tmp_path: Path, real_vault: RecordingVault, trust_loopback: Tuple[Path, Path]
) -> None:
    certfile, keyfile = trust_loopback
    rec = _Recorder()
    handler = _handler_for(rec, reply=lambda: (200, {}, b'{"value":[]}'))
    with _https_server(handler, certfile, keyfile) as port:
        binding, handle = _bound()
        transport = build_production_transport(
            gate=_gate_for(binding, handle),
            store=_live_store(tmp_path, binding),
            vault=real_vault,
            locator=_locator_to(f"https://localhost:{port}/v1/me/messages"),
        )
        outcome = execute(_descriptor(), handle, transport, **_kw())

    assert outcome.error is None
    # Resolved by the per-binding NAME out of the real encrypted store (the scoped
    # name the constructor recorded, NOT the slug), and it reached the wire.
    assert real_vault.asked == [_DEFAULT_BINDING["secret_ref"]["name"]]
    assert binding_secret_ref("outlook")["name"] not in real_vault.asked
    assert rec.authorization_seen() == ["Bearer outlook-live-token"]


def test_a_transport_refuses_a_call_from_another_binding_and_never_reads_the_vault(
    tmp_path: Path,
    real_vault: RecordingVault,
) -> None:
    """The counterexample: composed for X, called for Y -> refuse, resolve nothing.

    Both bindings are the SAME service and the SAME credential mode -- the two
    axes the executor passes -- and differ only in the verified
    subject/tenant behind them. That is precisely the pair a slug-keyed composition
    cannot tell apart.
    """

    binding_x = _binding(subject="alice", tenant="acme")
    binding_y = _binding(subject="bob", tenant="globex")
    handle_x, handle_y = _handle(binding_x), _handle(binding_y)
    view_x, view_y = ensure_usable(handle_x, now=_T0), ensure_usable(handle_y, now=_T0)
    # Same trusted routing axes; different bindings.
    assert (view_x.service_id, view_x.credential_mode) == (
        view_y.service_id,
        view_y.credential_mode,
    )
    assert view_x.binding_fingerprint != view_y.binding_fingerprint

    sent: List[HttpRequest] = []

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        sent.append(request)
        return HttpReply(status=200, body=b"{}")

    transport = build_production_transport(
        gate=_gate_for(binding_x, handle_x),  # custody for X
        store=_live_store(tmp_path, binding_x, binding_y),
        vault=real_vault,
        locator=_locator_to("https://graph.example.invalid/v1/me"),
        http_send=_send,
    )
    outcome = execute(_descriptor(), handle_y, transport, **_kw())  # a call for Y

    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    # NOTHING was emitted and the vault was NEVER asked -- not for X's name, not
    # for any name -- so no plaintext existed at any point on this path.
    assert sent == []
    assert real_vault.asked == []
    assert binding_secret_ref("outlook")["name"] not in real_vault.asked


def test_the_selector_refuses_a_call_that_carries_no_trusted_identity_at_all(
    tmp_path: Path,
    real_vault: RecordingVault,
) -> None:
    """Fail CLOSED: an absent view must not fall back to the composed binding."""

    binding, handle = _bound()
    gate = _gate_for(binding, handle)
    with pytest.raises(BindingIdentityMismatchError) as caught:
        gate.trusted_binding_for(None)
    assert "no trusted binding identity" in str(caught.value)

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        raise AssertionError("must not send")

    transport = build_production_transport(
        gate=gate,
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to("https://graph.example.invalid/v1/me"),
        http_send=_send,
    )
    # Called directly, as a legacy caller that never passes trusted_view would.
    response = transport(
        service_id="outlook",
        credential_mode="oauth_user",
        descriptor=_descriptor(),
        request_args={},
    )
    assert response.http_status == 401
    assert real_vault.asked == []


def test_the_gate_refuses_a_mismatched_service_or_credential_mode() -> None:
    """The fingerprint is not the only gate: the trusted axes must agree too.

    The axes are READ OFF the gate's binding rather than passed separately, so a
    mismatch is composed by giving the gate a binding whose ``service_id`` /
    ``credential_mode`` differ from the view's -- which is also why a gate can no
    longer be built that checks one identity and presents another to the fence.
    """

    binding, handle = _bound()
    view = ensure_usable(handle, now=_T0)
    base = _gate_for(binding, handle)

    other_service: Binding = dict(binding)  # type: ignore[assignment]
    other_service["service_id"] = "github"
    wrong_service = BindingCustodyGate(
        binding=other_service, binding_fingerprint=view.binding_fingerprint
    )
    with pytest.raises(BindingIdentityMismatchError):
        wrong_service.trusted_binding_for(view)

    other_mode: Binding = dict(binding)  # type: ignore[assignment]
    other_mode["credential_mode"] = "service_to_service"
    wrong_mode = BindingCustodyGate(
        binding=other_mode, binding_fingerprint=view.binding_fingerprint
    )
    with pytest.raises(BindingIdentityMismatchError):
        wrong_mode.trusted_binding_for(view)

    # And the matching one yields the BINDING to fence -- no ref, no vault name.
    fenced = base.trusted_binding_for(view)
    assert fenced["binding_id"] == binding["binding_id"]
    assert fenced["generation"] == view.generation
    assert not hasattr(base, "slug")


def test_an_empty_composed_fingerprint_matches_nothing() -> None:
    """A blank-vs-blank comparison must not become a wildcard.

    Stronger than before: an empty (or any mismatched) fingerprint is now refused
    at CONSTRUCTION -- the gate cannot even form unless its fingerprint is the
    one-way digest of its own binding's id (F1). So a blank fingerprint never
    reaches ``trusted_binding_for``.
    """

    binding, _handle_ = _bound()
    with pytest.raises(BindingIdentityMismatchError):
        BindingCustodyGate(binding=binding, binding_fingerprint="")


def test_a_gate_whose_fingerprint_is_not_its_bindings_is_refused_at_construction() -> None:
    """F1: binding B paired with binding A's fingerprint cannot compose a gate.

    Two bindings that share service_id / credential_mode but are different
    identities: a gate built from B's binding but A's fingerprint would (pre-fix)
    fence B while a call carrying A's view passed, sending B's credential under A's
    authorized identity. The construction-time check refuses the mismatched pair.
    """

    binding_a, handle_a = _bound(subject="alice", tenant="acme")
    binding_b, _hb = _bound(subject="bob", tenant="globex")
    fp_a = ensure_usable(handle_a, now=_T0).binding_fingerprint
    # B's binding + A's fingerprint -> refused before any call.
    with pytest.raises(BindingIdentityMismatchError):
        BindingCustodyGate(binding=binding_b, binding_fingerprint=fp_a)


def test_F1_mutating_the_binding_after_construction_cannot_change_what_the_gate_fences() -> None:
    """F1 (ninth recurrence): the gate holds an IMMUTABLE snapshot, no re-readable dict.

    A gate composed for binding A must fence A's identity/secret_ref forever, even
    if the caller keeps a reference to the dict it passed in and later mutates it
    (or mutates the mapping the gate hands back). Pre-fix the gate held the caller's
    mutable dict and returned ``dict(self.binding)``, so a holder who swapped
    ``binding_id`` / ``secret_ref`` AFTER the fingerprint check passed could make an
    A-authorized gate present B's credential. The class-level fix freezes the carrier
    at construction, so there is no mutable dict left to re-read.
    """

    binding, handle = _bound()
    original_id = binding["binding_id"]
    original_ref_name = binding["secret_ref"]["name"]
    view = ensure_usable(handle, now=_T0)
    gate = BindingCustodyGate(binding=binding, binding_fingerprint=view.binding_fingerprint)

    # The caller mutates the dict it still holds a reference to -- an attacker's
    # post-validation swap of identity and secret reference.
    binding["binding_id"] = "binding://attacker-swapped"
    binding["secret_ref"]["name"] = "attacker-vault-entry"

    # The gate's own carrier is unaffected: it snapshotted at construction.
    assert gate.binding["binding_id"] == original_id
    assert gate.binding["secret_ref"]["name"] == original_ref_name

    # And the presented binding it hands downstream is the ORIGINAL identity/ref,
    # not the swapped one -- what select_secret fences and resolves is A's.
    presented = gate.trusted_binding_for(view)
    assert presented["binding_id"] == original_id
    assert presented["secret_ref"]["name"] == original_ref_name

    # The carrier is genuinely read-only: neither the top level nor the nested
    # secret_ref can be mutated through what the gate holds or hands out.
    with pytest.raises(TypeError):
        gate.binding["binding_id"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        gate.binding["secret_ref"]["name"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        presented["binding_id"] = "x"  # type: ignore[index]


def test_the_send_path_takes_the_entry_name_from_the_store_never_from_the_slug(
    tmp_path: Path,
) -> None:
    """The closed gap, pinned: per-binding separation is REAL on the send path now.

    This closes a NAMED GAP -- a transport that derived the vault entry name with
    :func:`~kiro_crew.connections.control_plane.binding.binding_secret_ref` from the
    provider SLUG alone would resolve the SAME vault entry for two bindings of the
    SAME provider (different subjects,
    different tenants). Per-binding custody would be
    apparent, not real.

    It is closed by resolving through L04's live store: the name comes from the
    STORE's record for THAT binding. Two bindings whose records name two different
    entries resolve two different credentials, while the slug both share still maps
    to a single name that neither send used.
    """

    binding_a = _binding(subject="alice", tenant="acme")
    binding_b = _binding(subject="bob", tenant="globex")
    # Per-binding entry names come from the CONSTRUCTOR's own record, not stamped
    # by the test: two bindings under one provider get two DIFFERENT scoped names,
    # the axis the slug cannot express. Read them off the records to prove it.
    name_a = binding_a["secret_ref"]["name"]
    name_b = binding_b["secret_ref"]["name"]
    assert name_a != name_b

    # The SLUG-derived name is ONE name for both -- the defect, kept as the
    # counter-example, and it is not what either send resolves.
    slug_name = binding_secret_ref("outlook")["name"]
    assert slug_name not in (name_a, name_b)

    vault = RecordingVault(tmp_path / "crewhome")
    vault.set_sync(name_a, "token-for-alice")
    vault.set_sync(name_b, "token-for-bob")
    vault.set_sync(slug_name, "the-collapsed-slug-token")
    vault.asked.clear()

    store = _live_store(tmp_path, binding_a, binding_b)
    handle_a, handle_b = _handle(binding_a), _handle(binding_b)

    sent: List[HttpRequest] = []

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        sent.append(request)
        return HttpReply(status=200, body=b"{}")

    for binding, handle in ((binding_a, handle_a), (binding_b, handle_b)):
        transport = build_production_transport(
            gate=_gate_for(binding, handle),
            store=store,
            vault=vault,
            locator=_locator_to("https://graph.example.invalid/v1/me"),
            http_send=_send,
        )
        assert execute(_descriptor(), handle, transport, **_kw()).error is None

    # TWO DIFFERENT credentials reached the wire, per binding -- B could not read
    # A's token and vice versa, because each resolves ITS OWN per-binding name.
    assert [r.headers["Authorization"] for r in sent] == [
        "Bearer token-for-alice",
        "Bearer token-for-bob",
    ]
    # And the vault was asked ONLY for the store's per-binding names -- the
    # slug-derived name was never looked up, on either call.
    assert vault.asked == [name_a, name_b]
    assert slug_name not in vault.asked


def test_l04_generation_fencing_is_judged_on_the_send_path(tmp_path: Path) -> None:
    """The other closed gap: the send path now judges ``generation``, via the store.

    Without this fence a "selector matches a BINDING, not a generation" design --
    a handle from generation N still resolving after the binding moves to N+1,
    because nothing on the path compares generations -- would leak a stale
    credential. The live-store fence
    (``assert_live`` inside ``select_secret``) compares them EXACTLY, so a stale
    generation is refused with nothing emitted and no vault read.
    """

    binding = _binding()
    handle = _handle(binding)
    store = _live_store(tmp_path, binding)
    vault = RecordingVault(tmp_path / "crewhome")
    vault.set_sync(binding["secret_ref"]["name"], "outlook-live-token")
    vault.asked.clear()

    sent: List[HttpRequest] = []

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        sent.append(request)
        return HttpReply(status=200, body=b"{}")

    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=store,
        vault=vault,
        locator=_locator_to("https://graph.example.invalid/v1/me"),
        http_send=_send,
    )
    # Live to begin with.
    assert execute(_descriptor(), handle, transport, **_kw()).error is None
    assert len(sent) == 1

    # Revoke on the LIVE store: same handle, same gate, same transport object.
    store.revoke(binding["binding_id"])
    vault.asked.clear()
    outcome = execute(_descriptor(), handle, transport, **_kw())

    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    # ZERO further calls, and the vault was never consulted: the fence refused
    # before a socket or a plaintext existed.
    assert len(sent) == 1
    assert vault.asked == []


def test_the_transport_binds_to_the_record_sourced_view_never_a_caller_chosen_one() -> None:
    """The fingerprint the selector matches on cannot be chosen by the caller.

    Two facts, and the second is the stronger one:

    1. On a clean call the transport receives a ``trusted_view`` whose
       ``binding_fingerprint`` is the one L08 recorded at issuance.
    2. A handle carrying a DIFFERENT fingerprint does not reach the transport at
       all -- ``ensure_usable`` refuses it as tampered before step 2 of the gate
       chain. So the identity the selector judges is never caller-supplied, and
       the selector's match is not the only thing standing between two bindings.
    """

    binding, handle = _bound()
    view = ensure_usable(handle, now=_T0)
    seen: List[Dict[str, Any]] = []

    def _transport(**kwargs: Any) -> TransportResponse:
        seen.append(kwargs)
        return TransportResponse(
            http_status=200, result={"status": "ok", "next_cursor": None, "payload": None}
        )

    outcome = execute(_descriptor(), handle, _transport, **_kw())
    assert outcome.error is None
    assert seen[0]["trusted_view"].binding_fingerprint == view.binding_fingerprint
    # The gate built for this binding matches the view the executor passed, and
    # yields THAT binding (stamped with the generation the call presents).
    fenced = _gate_for(binding, handle).trusted_binding_for(seen[0]["trusted_view"])
    assert fenced["binding_id"] == binding["binding_id"]
    assert fenced["generation"] == seen[0]["trusted_view"].generation

    # (2) A rewritten fingerprint never gets as far as the transport.
    tampered = dict(handle)
    tampered["binding_fingerprint"] = "0" * 64
    seen.clear()
    refused = execute(_descriptor(), tampered, _transport, **_kw())  # type: ignore[arg-type]
    assert refused.error is not None
    assert refused.error["error_class"] == "auth"
    assert seen == []


# =============================================================================
# DEFECT C -- 2xx is interpreted, not collapsed
# =============================================================================
@pytest.mark.parametrize(
    ("status", "body", "expected_status", "kind", "determined"),
    [
        (204, b"", "ok", "no_content", True),
        (200, b"", "ok", "empty_complete", True),
        (202, b"", "ok", "empty_complete", True),
        (206, b'{"value":[1]}', "partial", "partial_content", False),
        (200, b'{"value":[1,2]}', "partial", "cursor_undetermined", False),
        (201, b'{"id":"new"}', "partial", "cursor_undetermined", False),
    ],
)
def test_the_2xx_interpretation_table(
    status: int, body: bytes, expected_status: str, kind: str, determined: bool
) -> None:
    detail = neutral_decode_detail(HttpReply(status=status, body=body))
    assert detail.result["status"] == expected_status
    assert detail.content_kind == kind
    assert detail.cursor_determined is determined
    # No cursor is ever GUESSED -- that stays the vendor owner's job.
    assert detail.result["next_cursor"] is None
    assert neutral_decode(HttpReply(status=status, body=body)) == detail.result
    # Every status produced is a member of L01's closed set, and the two spellings
    # this module writes are L01's own constants -- pinned here rather than
    # trusted, since production.py writes the literals for mypy's benefit.
    assert detail.result["status"] in RESULT_STATUSES
    assert (RESULT_STATUS_OK, RESULT_STATUS_PARTIAL) == ("ok", "partial")


def test_decode_json_body_empty_body_is_a_genuine_empty_object() -> None:
    """An EMPTY body is a real empty result -> {}. This is NOT the data-loss case."""
    assert decode_json_body(HttpReply(status=200, body=b"")) == {}
    assert decode_json_body(HttpReply(status=200, body=None)) == {}


def test_decode_json_body_a_valid_object_passes_through() -> None:
    assert decode_json_body(HttpReply(status=200, body=b'{"a":1}')) == {"a": 1}


def test_decode_json_body_a_malformed_nonempty_body_is_refused_not_swallowed() -> None:
    """F2: a present-but-unparseable 2xx body must REFUSE, not collapse to {}.

    Collapsing to {} makes a truncated / non-JSON 2xx (a 200-served HTML error
    page, a mid-transfer cutoff, a gzip fault) indistinguishable from a genuine
    empty body -- a reported success carrying no data, with no signal to retry.
    The typed error is that signal.
    """
    # Truncated JSON.
    with pytest.raises(MalformedResponseBodyError):
        decode_json_body(HttpReply(status=200, body=b'{"a": 1'))
    # A 200-served HTML error page.
    with pytest.raises(MalformedResponseBodyError):
        decode_json_body(HttpReply(status=200, body=b"<html>502 Bad Gateway</html>"))
    # Invalid UTF-8.
    with pytest.raises(MalformedResponseBodyError):
        decode_json_body(HttpReply(status=200, body=b"\xff\xfe\x00"))


def test_decode_json_body_a_nonobject_json_is_refused() -> None:
    """A JSON array or scalar where an object was contracted is also refused."""
    with pytest.raises(MalformedResponseBodyError):
        decode_json_body(HttpReply(status=200, body=b"[1, 2, 3]"))
    with pytest.raises(MalformedResponseBodyError):
        decode_json_body(HttpReply(status=200, body=b'"just a string"'))
    with pytest.raises(MalformedResponseBodyError):
        decode_json_body(HttpReply(status=200, body=b"42"))


def test_a_204_and_a_200_with_a_body_no_longer_decode_the_same() -> None:
    """The counterexample this defect was reported with."""

    no_content = neutral_decode(HttpReply(status=204, body=b""))
    with_body = neutral_decode(HttpReply(status=200, body=b'{"value":[1,2,3]}'))
    assert no_content != with_body
    assert no_content == {"status": "ok", "next_cursor": None, "payload": None}
    # The body-bearing reply is `partial` AND carries its bytes: the two readings
    # differ on the payload channel as well as on the status.
    assert with_body["status"] == "partial" and with_body["next_cursor"] is None
    assert with_body["payload"] == BytesPayload(
        data=b'{"value":[1,2,3]}', media_type=DEFAULT_MEDIA_TYPE
    )
    assert neutral_decode(HttpReply(status=206, body=b"x"))["status"] == "partial"


def test_a_2xx_body_is_preserved_rather_than_discarded() -> None:
    """A cursor this module cannot read must still be readable by someone."""

    body = b'{"value":[1,2,3],"@odata.nextLink":"https://x/next"}'
    detail = neutral_decode_detail(HttpReply(status=200, body=body))
    assert detail.body == body
    assert detail.cursor_determined is False
    # 204 has no body by definition, so nothing is being hidden there.
    assert neutral_decode_detail(HttpReply(status=204, body=b"ignored")).body == b""


def test_a_real_204_and_a_real_206_off_the_wire_decode_correctly(
    trust_loopback: Tuple[Path, Path],
) -> None:
    """The statuses come from a real server, not a hand-built HttpReply."""

    certfile, keyfile = trust_loopback
    rec = _Recorder()
    handler = _handler_for(rec, reply=lambda: (204, {}, b""))
    with _https_server(handler, certfile, keyfile) as port:
        reply_204 = urllib_http_send(
            HttpRequest(method="GET", url=f"https://localhost:{port}/empty", headers={}),
            timeout_seconds=10.0,
        )
    assert reply_204.status == 204 and reply_204.body == b""
    assert neutral_decode_detail(reply_204).content_kind == "no_content"
    assert neutral_decode(reply_204) == {"status": "ok", "next_cursor": None, "payload": None}

    rec206 = _Recorder()
    handler206 = _handler_for(
        rec206,
        reply=lambda: (206, {"Content-Range": "items 0-0/9"}, b'{"value":[1]}'),
    )
    with _https_server(handler206, certfile, keyfile) as port:
        reply_206 = urllib_http_send(
            HttpRequest(method="GET", url=f"https://localhost:{port}/page", headers={}),
            timeout_seconds=10.0,
        )
    assert reply_206.status == 206
    detail = neutral_decode_detail(reply_206)
    assert detail.result["status"] == "partial"
    assert detail.body == b'{"value":[1]}'
    assert detail.cursor_determined is False
    # A real 206 off the wire carries its fragment's bytes to the consumer.
    payload = detail.result["payload"]
    assert isinstance(payload, BytesPayload) and payload.data == b'{"value":[1]}'


def test_the_transport_surfaces_the_partial_reading_for_a_body_bearing_2xx(
    tmp_path: Path, real_vault: RecordingVault, trust_loopback: Tuple[Path, Path]
) -> None:
    certfile, keyfile = trust_loopback
    rec = _Recorder()
    handler = _handler_for(rec, reply=lambda: (200, {}, b'{"value":[1,2]}'))
    with _https_server(handler, certfile, keyfile) as port:
        binding, handle = _bound()
        transport = build_production_transport(
            gate=_gate_for(binding, handle),
            store=_live_store(tmp_path, binding),
            vault=real_vault,
            locator=_locator_to(f"https://localhost:{port}/v1/me/messages"),
        )
        outcome = execute(_descriptor(), handle, transport, **_kw())
    assert outcome.result is not None
    # `ok` would have asserted completeness nobody established.
    assert outcome.result["status"] == "partial"
    assert outcome.result["next_cursor"] is None


# =============================================================================
# DEFECT D -- an ambiguous outcome is recorded as ambiguous
# =============================================================================
def _write_descriptor() -> OperationDescriptor:
    return {
        "operation_id": "outlook.messages.send",
        "service_id": "outlook",
        "operation_kind": "mutation",
        "effect": "external_send",
        "credential_modes": ("oauth_user",),
    }


def test_a_real_connection_failure_on_a_write_records_unknown_not_not_applied(
    tmp_path: Path,
    real_vault: RecordingVault,
) -> None:
    """A REAL refused TCP connection, on a port nothing is listening on."""

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to(f"https://localhost:{dead_port}/sendMail", method="POST", body=b"{}"),
    )
    outcome = execute(
        _write_descriptor(),
        handle,
        transport,
        request_idempotency_key="idem-1",
        **_kw(governance_item="messages.send"),
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "temporary"
    # The point: NOT a determinate "did not apply".
    assert outcome.write_outcome == ATTEMPT_UNKNOWN
    assert outcome.write_outcome != ATTEMPT_FAILED_NOT_APPLIED


def test_feeding_that_unknown_into_l07_refuses_a_blind_replay(
    tmp_path: Path,
    real_vault: RecordingVault,
) -> None:
    """The whole reason the field exists: L07 must see `unknown`, not a guess."""

    def _timeout_send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        raise urllib.error.URLError(TimeoutError("timed out"))

    descriptor = _write_descriptor()
    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to("https://graph.example.invalid/v1/me/sendMail", method="POST"),
        http_send=_timeout_send,
    )
    args = {"to": "someone@example.invalid"}
    outcome = execute(
        descriptor,
        handle,
        transport,
        request_args=args,
        request_idempotency_key="idem-7",
        **_kw(governance_item="messages.send"),
    )
    assert outcome.write_outcome == ATTEMPT_UNKNOWN

    # Record exactly what the transport reported, then ask L07 to replay.
    record = record_attempt(
        operation_id=descriptor["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="idem-7",
        # The literal is what mypy needs; the assert above pins it == ATTEMPT_UNKNOWN.
        outcome="unknown",
    )
    refused = replay_decision(
        descriptor, record, request_args=args, request_idempotency_key="idem-7"
    )
    assert refused["verdict"] == REPLAY_REFUSE

    # And the counterfactual that makes the defect concrete: had the transport
    # reported the old determinate 503 as "not applied", L07 would have ALLOWED
    # the second sendMail.
    misrecorded = record_attempt(
        operation_id=descriptor["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="idem-7",
        outcome="failed_not_applied",  # == ATTEMPT_FAILED_NOT_APPLIED, pinned below
    )
    assert ATTEMPT_FAILED_NOT_APPLIED == "failed_not_applied"
    assert ATTEMPT_UNKNOWN == "unknown"
    assert (
        replay_decision(
            descriptor, misrecorded, request_args=args, request_idempotency_key="idem-7"
        )["verdict"]
        == REPLAY_ALLOW
    )


def test_a_server_committed_then_disconnect_records_unknown_and_l07_refuses_replay(
    tmp_path: Path,
    real_vault: RecordingVault,
) -> None:
    """A raw ``http.client.RemoteDisconnected`` on a write -> ``unknown``, replay refused.

    Guards the escape a narrow ``except (URLError, TimeoutError, ...)``
    tuple leaves open. ``RemoteDisconnected`` is a ``ConnectionResetError``
    (-> ``ConnectionError``) AND an ``http.client.BadStatusLine``
    (-> ``HTTPException``), but is NOT a ``URLError``, so a server that COMMITTED
    the write and then dropped the reply would propagate the exception past a
    ``URLError``-only
    branch -- no ``write_outcome=unknown``, so no L07 replay-gate protection, so a
    blind retry would double-send. The fix names ``ConnectionError`` and
    ``http.client.HTTPException`` in the tuple; this pins the outcome as ``unknown``
    (NOT ``failed_not_applied``) and asserts L07 refuses the replay.
    """

    def _committed_then_dropped(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    descriptor = _write_descriptor()
    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to(
            "https://graph.example.invalid/v1/me/sendMail", method="POST", body=b"{}"
        ),
        http_send=_committed_then_dropped,
    )
    args = {"to": "someone@example.invalid"}
    outcome = execute(
        descriptor,
        handle,
        transport,
        request_args=args,
        request_idempotency_key="idem-disc-1",
        **_kw(governance_item="messages.send"),
    )
    # The exception does not escape: a structured ambiguous outcome instead.
    assert outcome.error is not None
    assert outcome.write_outcome == ATTEMPT_UNKNOWN
    assert outcome.write_outcome != ATTEMPT_FAILED_NOT_APPLIED

    # And L07 refuses to replay the non-idempotent write on that `unknown`.
    record = record_attempt(
        operation_id=descriptor["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="idem-disc-1",
        outcome="unknown",
    )
    assert (
        replay_decision(
            descriptor, record, request_args=args, request_idempotency_key="idem-disc-1"
        )["verdict"]
        == REPLAY_REFUSE
    )


def test_a_read_never_claims_an_unknown_write_outcome(
    tmp_path: Path, real_vault: RecordingVault
) -> None:
    def _timeout_send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        raise urllib.error.URLError(TimeoutError("timed out"))

    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to("https://graph.example.invalid/v1/me/messages"),
        http_send=_timeout_send,
    )
    outcome = execute(_descriptor(), handle, transport, **_kw())
    assert outcome.error is not None and outcome.error["error_class"] == "temporary"
    assert outcome.write_outcome is None
    assert is_non_idempotent_effect("read") is False


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_status_on_a_write_is_also_unknown(
    tmp_path: Path, real_vault: RecordingVault, status: int
) -> None:
    """A failure in transit reached no end-to-end verdict either."""

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        return HttpReply(status=status, body=b"")

    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to("https://graph.example.invalid/v1/me/sendMail", method="POST"),
        http_send=_send,
    )
    outcome = execute(
        _write_descriptor(),
        handle,
        transport,
        request_idempotency_key="idem-2",
        **_kw(governance_item="messages.send"),
    )
    assert outcome.write_outcome == ATTEMPT_UNKNOWN


def test_a_committed_then_500_on_a_write_is_unknown_and_l07_refuses_replay(
    trust_loopback: Tuple[Path, Path],
    tmp_path: Path,
    real_vault: RecordingVault,
) -> None:
    """A REAL 500 over TLS AFTER the server committed -> ``unknown``, replay refused.

    500 is in :data:`_AMBIGUOUS_TRANSIT_STATUSES`, so it does NOT surface with
    ``write_outcome=None`` -- which would be indistinguishable from a read, a clean
    2xx and a gate denial (executor's own contract), and would let a caller only
    infer "not applied" from a 5xx, the exact inference L07 exists to refuse, and
    replay the send -> a DOUBLE sendMail. The server here COMMITS (a real reply
    body) and then answers 500; nothing proves the effect did not land, so the
    outcome must be ``unknown`` and L07 must refuse a blind replay of the
    non-idempotent write. This goes through the real ``urllib_http_send`` (default
    http_send), a real TLS socket and a real loopback origin -- not a stub.
    """

    certfile, keyfile = trust_loopback
    rec = _Recorder()
    # Committed, then 500: a body is present (the write took effect server-side)
    # and the status is 500.
    handler = _handler_for(rec, reply=lambda: (500, {}, b'{"committed":true}'))
    binding, handle = _bound(requested=("mail.send",))
    args = {"to": "someone@example.invalid"}
    with _https_server(handler, certfile, keyfile) as port:
        transport = build_production_transport(
            gate=_gate_for(binding, handle),
            store=_live_store(tmp_path, binding),
            vault=real_vault,
            locator=_locator_to(
                f"https://localhost:{port}/v1/me/sendMail", method="POST", body=b"{}"
            ),
        )
        outcome = execute(
            _write_descriptor(),
            handle,
            transport,
            request_args=args,
            request_idempotency_key="idem-500-1",
            **_kw(governance_item="messages.send"),
        )
    # The server WAS reached (real hit), and the outcome is the safe ambiguous one.
    assert rec.hits == 1
    assert outcome.write_outcome == ATTEMPT_UNKNOWN
    assert outcome.write_outcome != ATTEMPT_FAILED_NOT_APPLIED

    # Non-idempotent write on that `unknown` -> L07 REFUSES the replay.
    record = record_attempt(
        operation_id=_write_descriptor()["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="idem-500-1",
        outcome="unknown",
    )
    assert (
        replay_decision(
            _write_descriptor(), record, request_args=args, request_idempotency_key="idem-500-1"
        )["verdict"]
        == REPLAY_REFUSE
    )

    # PRESERVED: the caller's explicit idempotence override still allows a replay
    # (the safe default did NOT make a truly-idempotent write unreplayable).
    idempotent_record = record_attempt(
        operation_id=_write_descriptor()["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="idem-500-1",
        outcome="unknown",
        idempotent=True,
    )
    assert (
        replay_decision(
            _write_descriptor(),
            idempotent_record,
            request_args=args,
            request_idempotency_key="idem-500-1",
        )["verdict"]
        == REPLAY_ALLOW
    )


def test_a_read_that_500s_never_claims_an_unknown_write_outcome(
    trust_loopback: Tuple[Path, Path],
    tmp_path: Path,
    real_vault: RecordingVault,
) -> None:
    """PRESERVED: a 500 on a READ (idempotent effect) stays ``write_outcome=None``.

    500 becoming ambiguous must not turn a READ into an unreplayable write: a read
    has no effect to have half-landed, so it claims no write_outcome even on 500.
    """

    certfile, keyfile = trust_loopback
    rec = _Recorder()
    handler = _handler_for(rec, reply=lambda: (500, {}, b'{"err":true}'))
    binding, handle = _bound()  # default descriptor is a READ
    with _https_server(handler, certfile, keyfile) as port:
        transport = build_production_transport(
            gate=_gate_for(binding, handle),
            store=_live_store(tmp_path, binding),
            vault=real_vault,
            locator=_locator_to(f"https://localhost:{port}/v1/me/messages"),
        )
        outcome = execute(_descriptor(), handle, transport, **_kw())
    assert rec.hits == 1
    assert outcome.write_outcome is None
    assert is_non_idempotent_effect("read") is False
    """Nothing left the process, so the outcome is NOT uncertain.

    Over-reporting `unknown` is safe but not free: it blocks a replay the caller
    is entitled to. A refusal raised before a socket exists is determinate and
    says so.
    """

    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to("http://graph.example.invalid/v1/me/sendMail", method="POST"),
    )
    outcome = execute(
        _write_descriptor(),
        handle,
        transport,
        request_idempotency_key="idem-3",
        **_kw(governance_item="messages.send"),
    )
    assert outcome.error is not None and outcome.error["error_class"] == "input"
    assert outcome.write_outcome is None


def test_a_real_response_over_the_size_cap_is_refused(trust_loopback: Tuple[Path, Path]) -> None:
    certfile, keyfile = trust_loopback
    rec = _Recorder()
    oversize = b"x" * 4096
    handler = _handler_for(rec, reply=lambda: (200, {}, oversize))
    with _https_server(handler, certfile, keyfile) as port:
        # Under the cap: fine.
        ok = urllib_http_send(
            HttpRequest(method="GET", url=f"https://localhost:{port}/big", headers={}),
            timeout_seconds=10.0,
            max_response_bytes=4096,
        )
        assert len(ok.body) == 4096
        # One byte under what the server sends: refused, not truncated.
        with pytest.raises(ResponseTooLargeError) as caught:
            urllib_http_send(
                HttpRequest(method="GET", url=f"https://localhost:{port}/big", headers={}),
                timeout_seconds=10.0,
                max_response_bytes=4095,
            )
    assert "4095-byte cap" in str(caught.value)


def test_an_oversize_response_on_a_write_is_unknown(
    tmp_path: Path, real_vault: RecordingVault
) -> None:
    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        raise ResponseTooLargeError("too big")

    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to("https://graph.example.invalid/v1/me/sendMail", method="POST"),
        http_send=_send,
    )
    outcome = execute(
        _write_descriptor(),
        handle,
        transport,
        request_idempotency_key="idem-4",
        **_kw(governance_item="messages.send"),
    )
    assert outcome.write_outcome == ATTEMPT_UNKNOWN


def test_the_overall_deadline_cuts_off_a_real_read(trust_loopback: Tuple[Path, Path]) -> None:
    """The deadline is enforced against a real socket, on an injected clock.

    The clock is injected rather than slept through so the test is deterministic
    and fast; everything else -- the TLS connection, the response, the read loop
    -- is real.
    """

    certfile, keyfile = trust_loopback
    rec = _Recorder()
    handler = _handler_for(rec, reply=lambda: (200, {}, b"y" * 256))
    ticks = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])

    def _clock() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 10_000.0

    with _https_server(handler, certfile, keyfile) as port:
        with pytest.raises(TransportDeadlineExceededError) as caught:
            urllib_http_send(
                HttpRequest(method="GET", url=f"https://localhost:{port}/slow", headers={}),
                timeout_seconds=10.0,
                deadline_seconds=30.0,
                monotonic=_clock,
            )
    assert "deadline elapsed while reading" in str(caught.value)


def test_the_deadline_is_checked_before_the_request_is_even_opened() -> None:
    """An already-elapsed budget must not open a connection at all."""

    ticks = iter([0.0, 100.0])

    def _clock() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 100.0

    with pytest.raises(TransportDeadlineExceededError) as caught:
        urllib_http_send(
            HttpRequest(method="GET", url="https://127.0.0.1:1/never", headers={}),
            deadline_seconds=10.0,
            monotonic=_clock,
        )
    assert "before the request was opened" in str(caught.value)


def test_a_deadline_timeout_on_a_write_is_unknown(
    tmp_path: Path, real_vault: RecordingVault
) -> None:
    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        raise TransportDeadlineExceededError("out of budget")

    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=real_vault,
        locator=_locator_to("https://graph.example.invalid/v1/me/sendMail", method="POST"),
        http_send=_send,
    )
    outcome = execute(
        _write_descriptor(),
        handle,
        transport,
        request_idempotency_key="idem-5",
        **_kw(governance_item="messages.send"),
    )
    assert outcome.write_outcome == ATTEMPT_UNKNOWN


# =============================================================================
# the numbers and the schema versions the downstreams pin
# =============================================================================
def test_the_two_bounds_have_concrete_documented_numbers() -> None:
    assert DEFAULT_DEADLINE_SECONDS == 60.0
    assert DEFAULT_MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    # The deadline is the ceiling: it must exceed one socket operation's timeout,
    # or the per-op timeout could never be reached and would be decorative.
    from kiro_crew.connections.control_plane.production import DEFAULT_TIMEOUT_SECONDS

    assert DEFAULT_DEADLINE_SECONDS > DEFAULT_TIMEOUT_SECONDS


def test_both_schema_versions_were_bumped_for_these_shape_changes() -> None:
    # TransportResponse/ExecutionOutcome grew write_outcome and the Transport
    # contract grew trusted_view; the composition signature changed. Then the
    # success envelope grew a payload, which moved the executor to 3 (see
    # RESULT_SCHEMA_VERSION 2).
    #
    # All three then moved again, for two changes:
    #   * the cursor is SINGLE-SOURCED -- CollectionPayload.next_cursor is gone and
    #     result_with_payload takes an explicit next_cursor -- so RESULT went to 3,
    #     and the executor to 4 because a 3-era producer that set the cursor only on
    #     the collection now silently builds a one-page walk;
    #   * response metadata reaches a caller through an ALLOWLIST on
    #     TransportResponse.metadata / ExecutionOutcome.metadata, which is a new
    #     executor field (4) AND new behaviour in this module's transport on every
    #     reply branch, so PRODUCTION went to 3 as well. Unlike the payload -- which
    #     was a change in the L01 envelope this module merely returns -- the metadata
    #     is populated HERE, from this module's own allowlist.
    #
    # PRODUCTION moved once more, to 4: the send path resolves the credential
    # through L04's BindingStore.select_secret against the LIVE store, and the
    # slug-derived binding_secret_ref resolution is GONE from it (no fallback).
    # build_production_transport therefore takes `gate` + `store` where it took
    # `selector`, and every call is now fenced. EXECUTOR and RESULT are unchanged
    # by that: the Transport contract and the result envelope kept their shapes --
    # only how this module obtains the credential behind that contract changed.
    assert EXECUTOR_SCHEMA_VERSION == 4
    assert RESULT_SCHEMA_VERSION == 3
    assert PRODUCTION_SCHEMA_VERSION == 4


def test_the_production_symbols_stay_off_the_connections_top_level() -> None:
    import kiro_crew.connections as connections
    import kiro_crew.connections.control_plane as cp

    for name in (
        "BindingIdentityMismatchError",
        "BindingCustodyGate",
        "DEFAULT_DEADLINE_SECONDS",
        "DEFAULT_MAX_RESPONSE_BYTES",
        "Decoded2xx",
        "RedirectHop",
        "RedirectRefusedError",
        "ResponseTooLargeError",
        "TransportDeadlineExceededError",
        "is_non_idempotent_effect",
        "neutral_decode_detail",
    ):
        assert hasattr(cp, name), name
        assert name in cp.__all__, name
        assert name not in connections.__all__, f"{name} leaked into connections.__all__"

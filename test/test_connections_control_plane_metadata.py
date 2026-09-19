"""W01 · L09: response metadata reaches a caller through ONE ALLOWLIST.

The gap: the rate-limit family had nowhere to go. A caller that gets a 429 has to
know how long to wait, and the only thing carrying that was the transport's own
``retry_after_seconds`` -- one header, delta-seconds only, nothing for
``x-ratelimit-remaining`` / ``ratelimit-reset``. The obvious fix, handing the
response headers to the caller, is not available: a response header set is a
CREDENTIAL SURFACE. ``Set-Cookie`` mints a session, ``WWW-Authenticate`` /
``Proxy-Authenticate`` carry challenge material, and a proxy or a
misconfigured provider can echo ``Authorization`` back. A caller logs, serializes
and forwards its results, so handing it the set is the response-side twin of
forwarding a credential across a redirect hop -- the defect the redirect guard
exists to stop.

So the surface is a CLOSED ALLOWLIST
(:data:`~kiro_crew.connections.control_plane.production.RESPONSE_METADATA_ALLOWLIST`),
projected onto lowercase keys, and everything else is dropped. The direction of
failure is the whole point: an allowlist drops a header nobody has thought about
yet, a denylist forwards it.

What these tests assert is ABSENCE, which is the hard half: a test that only
checked that ``retry-after`` arrives would pass just as happily on a transport
that forwarded ``Set-Cookie`` beside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import Binding
from kiro_crew.connections.control_plane.executor import (
    EMPTY_RESPONSE_METADATA,
    ExecutionOutcome,
    TransportResponse,
    execute,
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
    _CREDENTIAL_RESPONSE_HEADERS as CREDENTIAL_RESPONSE_HEADERS,
)
from kiro_crew.connections.control_plane.production import (
    RESPONSE_METADATA_ALLOWLIST,
    BindingCustodyGate,
    HttpReply,
    HttpRequest,
    build_production_transport,
    response_metadata,
)
from kiro_crew.secrets import SecretVault

_T0 = 1_000_000.0
_GRANTED = ("mail.read", "mail.send")
_URL = "https://graph.example.invalid/v1.0/me/messages"

#: The credential/session headers a provider (or something answering as one) can
#: put on a RESPONSE. Every assertion below checks these are ABSENT.
_CREDENTIAL_RESPONSE_SAMPLE: Dict[str, str] = {
    "Set-Cookie": "session=SESSION-CANARY; Path=/; HttpOnly",
    "Set-Cookie2": "legacy=LEGACY-CANARY",
    "Authorization": "Bearer ECHOED-CANARY",
    "Proxy-Authorization": "Basic UFJPWFktQ0FOQVJZ",
    "WWW-Authenticate": 'Bearer realm="graph", error="invalid_token"',
    "Proxy-Authenticate": 'Basic realm="proxy"',
    "Cookie": "sent-back=COOKIE-CANARY",
    "Authentication-Info": "nextnonce=NONCE-CANARY",
}

#: Every canary VALUE, so an assertion can also prove no value leaked under some
#: other key (a provider that mirrored its cookie into ``X-Debug-Cookie``, say).
_CANARIES: Tuple[str, ...] = (
    "SESSION-CANARY",
    "LEGACY-CANARY",
    "ECHOED-CANARY",
    "PROXY-CANARY",
    "invalid_token",
    "COOKIE-CANARY",
    "NONCE-CANARY",
)


# =============================================================================
# the real control-plane composition
# =============================================================================
def _verifier(*, claimed_subject: str, claimed_tenant: str, service_id: str) -> Dict[str, str]:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _make_default_binding() -> Binding:
    from kiro_crew.connections.control_plane.binding import create_binding

    return create_binding(
        service_id="outlook",
        claimed_subject="alice",
        claimed_tenant="acme",
        credential_mode="oauth_user",
        verifier=_verifier,  # type: ignore[arg-type]
        slug="outlook",
    )


#: Minted ONCE by the real constructor so real_vault seeds the credential under
#: its OWN per-binding scoped secret_ref name (never a slug name).
_DEFAULT_BINDING: Binding = _make_default_binding()


def _bound() -> Tuple[Binding, DerivedHandle]:
    """A binding and a handle derived from it (the binding is needed to fence).

    Uses the shared ``_DEFAULT_BINDING`` so the real_vault fixture seeds the
    credential under its OWN per-binding scoped name -- the per-binding path.
    """

    binding = dict(_DEFAULT_BINDING)  # type: ignore[assignment]
    handle = derive_handle(
        binding,
        granted_scopes=_GRANTED,
        requested_scopes=("mail.read",),
        now=_T0,
        ttl_seconds=300.0,
    )
    return binding, handle


def _gate_for(binding: Binding, handle: DerivedHandle) -> BindingCustodyGate:
    view = ensure_usable(handle, now=_T0)
    return BindingCustodyGate(binding=binding, binding_fingerprint=view.binding_fingerprint)


def _live_store(root: Path, binding: Binding) -> BindingStore:
    """A REAL on-disk BindingStore holding ``binding``, for the live-store fence."""

    store = BindingStore(root / "connections" / "control_plane_bindings.json")
    store.insert(
        binding, deployment_id="deployment://test/graph", kiro_principal="kiro://test/owner"
    )
    return store


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
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    base.update(over)
    return base


@pytest.fixture
def real_vault(tmp_path: Path) -> SecretVault:
    """A REAL, isolated, AES-256-GCM vault on disk holding the binding secret."""

    vault = SecretVault(tmp_path / "crewhome")
    vault.set_sync(_DEFAULT_BINDING["secret_ref"]["name"], "outlook-live-token")
    assert (tmp_path / "crewhome" / ".vault" / "secrets.enc").is_file()
    return vault


class _Sender:
    def __init__(self, reply: HttpReply) -> None:
        self._reply = reply
        self.urls: List[str] = []

    def __call__(self, request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        self.urls.append(request.url)
        return self._reply

    @property
    def calls(self) -> int:
        return len(self.urls)


def _locator(*, request_args: Mapping[str, Any], **_: Any) -> HttpRequest:
    return HttpRequest(method="GET", url=_URL, headers={"Accept": "*/*"})


def _outcome_for(
    vault: SecretVault,
    reply: HttpReply,
    *,
    store_root: Path,
    sender_box: Optional[List[_Sender]] = None,
    **kw: Any,
) -> ExecutionOutcome:
    """Drive ONE call through the real composition and return the outcome."""

    binding, handle = _bound()
    sender = _Sender(reply)
    if sender_box is not None:
        sender_box.append(sender)
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(store_root, binding),
        vault=vault,
        locator=_locator,
        http_send=sender,
    )
    return execute(_descriptor(), handle, transport, **_kw(**kw))


def _assert_no_credential_material(metadata: Mapping[str, str]) -> None:
    """No credential KEY and no credential VALUE, under any spelling."""

    keys = {key.lower() for key in metadata}
    for name in _CREDENTIAL_RESPONSE_SAMPLE:
        assert name.lower() not in keys, f"{name} reached the caller's metadata"
        assert name not in metadata, f"{name} reached the caller's metadata"
    for banned in CREDENTIAL_RESPONSE_HEADERS:
        assert banned not in keys, f"{banned} reached the caller's metadata"
    blob = " ".join(f"{k}={v}" for k, v in metadata.items())
    for canary in _CANARIES:
        assert canary not in blob, f"credential material {canary!r} leaked into metadata"


# =============================================================================
# (1) the allowlist itself
# =============================================================================
def test_the_exact_allowlist() -> None:
    """The allowlist is pinned VALUE-BY-VALUE, so an addition is a visible diff."""

    assert RESPONSE_METADATA_ALLOWLIST == frozenset(
        {
            "retry-after",
            "ratelimit-limit",
            "ratelimit-remaining",
            "ratelimit-reset",
            "ratelimit-policy",
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
            "x-ratelimit-used",
            "x-ratelimit-resource",
            "x-rate-limit-limit",
            "x-rate-limit-remaining",
            "x-rate-limit-reset",
            "content-type",
            "etag",
            "last-modified",
        }
    )
    # Every entry is already lowercase: the projection lowercases the header it
    # reads, so an uppercase entry here would be unmatchable and silently dead.
    assert all(name == name.lower() for name in RESPONSE_METADATA_ALLOWLIST)


def test_the_allowlist_and_the_credential_headers_cannot_drift_into_overlap() -> None:
    """The gate cannot be relaxed quietly: the two sets are pinned DISJOINT.

    The allowlist being closed is what drops a credential header, so this is not
    the mechanism -- it is the guard on the mechanism. An edit that adds
    ``set-cookie`` or ``www-authenticate`` to the allowlist fails here.
    """

    assert RESPONSE_METADATA_ALLOWLIST & CREDENTIAL_RESPONSE_HEADERS == frozenset()
    for name in ("set-cookie", "set-cookie2", "authorization", "www-authenticate", "cookie"):
        assert name in CREDENTIAL_RESPONSE_HEADERS
        assert name not in RESPONSE_METADATA_ALLOWLIST


def test_the_projection_keeps_the_rate_family_and_drops_everything_else() -> None:
    """:func:`response_metadata` on a header set that mixes both."""

    metadata = response_metadata(
        {
            **_CREDENTIAL_RESPONSE_SAMPLE,
            "Retry-After": "120",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1700000000",
            "ETag": 'W/"etag-7"',
            # not on the allowlist, not a credential either: still dropped, because
            # the allowlist is what decides, not a judgment about harmfulness.
            "Server": "nginx/1.25",
            "X-Request-Id": "req-42",
        }
    )
    assert metadata["retry-after"] == "120"
    assert metadata["x-ratelimit-remaining"] == "0"
    assert metadata["x-ratelimit-reset"] == "1700000000"
    assert metadata["etag"] == 'W/"etag-7"'
    assert "server" not in metadata and "x-request-id" not in metadata
    _assert_no_credential_material(metadata)
    # Exactly the four allowlisted names that were present, and nothing else.
    assert set(metadata) == {"retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "etag"}


def test_the_projection_is_case_insensitive_and_lowercase_keyed() -> None:
    """A caller reads one spelling, whatever the provider sent."""

    for spelling in ("Retry-After", "retry-after", "RETRY-AFTER", "ReTrY-aFtEr"):
        metadata = response_metadata({spelling: "30"})
        assert metadata == {"retry-after": "30"}
    # And a credential header is dropped whatever its case, since the same
    # lowercasing decides membership.
    assert response_metadata({"SET-COOKIE": "s=1", "set-cookie": "s=2"}) == {}


def test_the_metadata_a_caller_holds_is_read_only() -> None:
    """One caller cannot edit another reader's view of the same call."""

    metadata = response_metadata({"Retry-After": "5"})
    with pytest.raises(TypeError):
        metadata["retry-after"] = "0"  # type: ignore[index]
    with pytest.raises(TypeError):
        metadata["set-cookie"] = "injected"  # type: ignore[index]
    assert dict(metadata) == {"retry-after": "5"}
    # The shared empty default is read-only too, which is what makes it safe to
    # share across every no-response outcome.
    with pytest.raises(TypeError):
        EMPTY_RESPONSE_METADATA["retry-after"] = "5"  # type: ignore[index]


# =============================================================================
# (2) THE pin: through the real composition, to what the caller actually holds
# =============================================================================
def test_a_throttled_reply_gives_the_caller_the_rate_headers_and_no_credentials(
    tmp_path: Path,
    real_vault: SecretVault,
) -> None:
    """ITEM 2's assertion, on the branch that matters: a 429 the caller backs off.

    The provider sends the rate family AND a full set of credential/session
    headers. The caller gets the first and none of the second.
    """

    outcome = _outcome_for(
        real_vault,
        HttpReply(
            status=429,
            headers={
                **_CREDENTIAL_RESPONSE_SAMPLE,
                "Retry-After": "42",
                "RateLimit-Limit": "5000",
                "RateLimit-Remaining": "0",
                "RateLimit-Reset": "60",
                "X-RateLimit-Used": "5000",
                "X-RateLimit-Resource": "core",
                "Content-Type": "application/json",
            },
            body=b'{"error":{"code":"tooManyRequests"}}',
        ),
        store_root=tmp_path,
    )

    assert outcome.error is not None and outcome.error["error_class"] == "throttle"
    # PRESENT: the whole rate family the provider sent.
    assert outcome.metadata["retry-after"] == "42"
    assert outcome.metadata["ratelimit-limit"] == "5000"
    assert outcome.metadata["ratelimit-remaining"] == "0"
    assert outcome.metadata["ratelimit-reset"] == "60"
    assert outcome.metadata["x-ratelimit-used"] == "5000"
    assert outcome.metadata["x-ratelimit-resource"] == "core"
    # ABSENT: every credential and session header, by key and by value.
    _assert_no_credential_material(outcome.metadata)
    # The existing typed advisory still works -- metadata is additive, not a
    # replacement, so nothing that already read retry_after_seconds broke.
    assert "retry_after=42.0s" in outcome.error["detail"]


def test_a_success_carries_metadata_and_still_no_credentials(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    """The 2xx branch: an ETag a readback re-derives against, and nothing else."""

    outcome = _outcome_for(
        real_vault,
        HttpReply(
            status=200,
            headers={
                **_CREDENTIAL_RESPONSE_SAMPLE,
                "Content-Type": "application/json",
                "ETag": 'W/"v9"',
                "Last-Modified": "Wed, 16 Sep 2026 10:00:00 GMT",
                "X-RateLimit-Remaining": "4999",
            },
            body=b'{"value":[]}',
        ),
        store_root=tmp_path,
    )

    assert outcome.ok
    assert outcome.metadata["etag"] == 'W/"v9"'
    assert outcome.metadata["last-modified"] == "Wed, 16 Sep 2026 10:00:00 GMT"
    assert outcome.metadata["x-ratelimit-remaining"] == "4999"
    assert outcome.metadata["content-type"] == "application/json"
    _assert_no_credential_material(outcome.metadata)


def test_a_412_carries_metadata_without_disturbing_the_structured_signal(
    tmp_path: Path,
    real_vault: SecretVault,
) -> None:
    """The 412 branch keeps its structured precondition signal AND gains metadata."""

    outcome = _outcome_for(
        real_vault,
        HttpReply(
            status=412,
            headers={**_CREDENTIAL_RESPONSE_SAMPLE, "ETag": 'W/"server-current"'},
            body=b"",
        ),
        store_root=tmp_path,
    )

    assert outcome.precondition is not None
    # unchanged invariant: no fabricated precondition, and the ETag still travels
    # on the structured signal rather than only in metadata.
    assert outcome.precondition.preconditions == ()
    assert outcome.precondition.server_etag == 'W/"server-current"'
    assert outcome.precondition.condition_unknown is False
    assert outcome.metadata["etag"] == 'W/"server-current"'
    _assert_no_credential_material(outcome.metadata)


def test_a_401_challenge_never_reaches_the_caller(tmp_path: Path, real_vault: SecretVault) -> None:
    """The header most likely to be mistaken for useful is still dropped.

    A ``WWW-Authenticate`` on a 401 looks like diagnostics, and that is exactly the
    argument that would put challenge material into a caller's logs. The typed
    ``auth`` error is what the caller gets.
    """

    outcome = _outcome_for(
        real_vault,
        HttpReply(
            status=401,
            headers={
                "WWW-Authenticate": 'Bearer realm="graph", error="invalid_token"',
                "Set-Cookie": "session=SESSION-CANARY",
                "Retry-After": "1",
            },
            body=b"",
        ),
        store_root=tmp_path,
    )

    assert outcome.error is not None and outcome.error["error_class"] == "auth"
    assert set(outcome.metadata) == {"retry-after"}
    _assert_no_credential_material(outcome.metadata)


def test_a_denied_gate_yields_empty_metadata_and_emits_nothing(
    tmp_path: Path, real_vault: SecretVault
) -> None:
    """No response, no metadata -- and the deny still reaches the sender not at all.

    The unchanged invariant (deny -> calls == 0) re-measured on the metadata path:
    an empty mapping here is the honest answer, not an omission.
    """

    box: List[_Sender] = []
    outcome = _outcome_for(
        real_vault,
        HttpReply(status=200, headers=dict(_CREDENTIAL_RESPONSE_SAMPLE), body=b"{}"),
        sender_box=box,
        governance_scope="definitely.not.a.catalog.scope",
        governance_item="x",
        store_root=tmp_path,
    )

    assert outcome.error is not None and not outcome.ok
    assert box[0].calls == 0
    assert dict(outcome.metadata) == {}
    assert len(outcome.metadata) == 0
    _assert_no_credential_material(outcome.metadata)


def test_a_transport_that_says_nothing_still_gives_a_caller_an_empty_mapping() -> None:
    """A default-constructed envelope reads as empty, never ``None``.

    A consumer doing ``outcome.metadata.get("retry-after")`` must not have to guard
    for ``None`` -- an optional mapping would put an ``AttributeError`` on the
    throttle path, which is the worst possible place for one.
    """

    assert dict(TransportResponse(http_status=204).metadata) == {}
    assert dict(ExecutionOutcome().metadata) == {}
    assert ExecutionOutcome().metadata.get("retry-after") is None
    assert TransportResponse(http_status=204).metadata is EMPTY_RESPONSE_METADATA


def test_an_allowlisted_header_whose_value_echoes_the_bearer_is_dropped() -> None:
    """The allowlist gates the NAME; a VALUE that echoes the outbound bearer drops.

    A provider that reflects the request's credential into an allowlisted header
    (here `etag`, whose name is allowed) would otherwise hand the token back to
    the caller through `.metadata`. When the outbound `sent_credential` is known,
    such a value is dropped; an unrelated allowlisted value on the same reply is
    kept, so the strip is by-value, not by-name.
    """

    bearer = "s3cr3t-token-value"
    metadata = response_metadata(
        {
            "ETag": bearer,  # provider echoed the token into an allowlisted header
            "Retry-After": "30",  # a genuine, unrelated allowlisted value
            "X-RateLimit-Remaining": f"tok={bearer}",  # embedded, not equal
        },
        sent_credential=bearer,
    )
    # The echoed value and the embedded-echo value are both gone.
    assert "etag" not in metadata
    assert "x-ratelimit-remaining" not in metadata
    # The unrelated allowlisted value survives.
    assert metadata["retry-after"] == "30"
    # And the token appears in no metadata value.
    assert all(bearer not in v for v in metadata.values())


def test_an_allowlisted_header_echoing_an_ENCODED_bearer_is_also_dropped() -> None:
    """F1: a reflected credential does not have to be RAW to be caught.

    A provider could reflect the bearer base64-encoded (standard or url-safe, with
    or without padding) or percent-encoded into an allowlisted header and still
    hand the token back through `.metadata`. Each encoded form is screened, while
    an unrelated allowlisted value on the same reply survives.
    """
    import base64
    import urllib.parse

    bearer = "s3cr3t-token-value"
    b64 = base64.b64encode(bearer.encode()).decode()
    b64url = base64.urlsafe_b64encode(bearer.encode()).decode()
    pct = urllib.parse.quote(bearer, safe="")
    for encoded in (b64, b64.rstrip("="), b64url, pct):
        metadata = response_metadata(
            {
                "ETag": encoded,  # provider echoed an ENCODED copy of the token
                "Retry-After": "30",  # genuine, unrelated
            },
            sent_credential=bearer,
        )
        assert "etag" not in metadata, f"encoded echo not dropped: {encoded!r}"
        assert metadata["retry-after"] == "30"


def test_without_a_sent_credential_the_projection_is_unchanged() -> None:
    """The value-strip is a no-op when no outbound credential is supplied.

    Callers that do not know the credential (there are none on this path, but the
    parameter is optional) get the pure name-allowlist behaviour.
    """

    metadata = response_metadata({"ETag": "abc123", "Retry-After": "30"})
    assert metadata["etag"] == "abc123"
    assert metadata["retry-after"] == "30"

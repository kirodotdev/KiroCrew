"""W01 · L09: the PRODUCTION transport composition for the executor.

:mod:`kiro_crew.connections.control_plane.executor` decides; it does not reach a
network. That is the right shape for the decision chain -- every gate is testable
against an in-memory fake, and the unit tests keep using that fake -- but a
dispatch seam that ONLY ever had a fake behind it is not an executor, it is a
decision table with an executor-shaped hole. This module fills the hole: it
composes a real :data:`~kiro_crew.connections.control_plane.executor.Transport`
out of the two things a real call needs and a fake supplies neither of:

1. **Real secret custody.** The credential comes from the EXISTING vault --
   :class:`kiro_crew.secrets.SecretVault` (``secrets/vault.py``), the same
   AES-256-GCM store the Secrets panel and ``oauth_clients`` already use -- read
   by NAME through the :class:`~kiro_crew.connections.control_plane.binding.SecretRef`
   on the binding record L04's trusted store holds, resolved by
   :meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.select_secret`
   so the name comes from the LIVE STORE per binding and never from a provider
   slug. No new vault, no new naming scheme, and no plaintext anywhere on the
   control plane's own types: the secret is resolved PER CALL, revealed once into
   an ``Authorization`` header, and never returned, stored on a closure, or placed
   in an error detail.
2. **A real HTTP client.** :func:`urllib_http_send` is the standard library's
   ``urllib.request`` -- the convention this repo already uses for outbound HTTP
   in non-async code (``apps/backend.py``, ``apps/official_catalog.py``,
   ``ops_mission_control/backend/providers/http.py``) -- so this adds no
   third-party dependency.

What this module deliberately does NOT do
-----------------------------------------
**No vendor semantics.** Turning an operation + its arguments into a concrete
method/URL/headers/body is the VENDOR owner's job (``vendors/microsoft/**``,
``vendors/github/**``): locator shape, ``@odata.nextLink`` vs ``page``/``perPage``
paging, readback and precondition derivation all differ per provider. So a
:data:`RequestLocator` is INJECTED, and the 2xx -> ``OperationResult`` decode is
injected too; the neutral default reports no continuation cursor rather than
guessing one provider's spelling, and hands the body on as RAW BYTES rather than
inventing a structure for it. This module owns custody + wire mechanics and
nothing above them.

**No network at import time.** Nothing here opens a socket, constructs a client,
or reads the vault while the module is being imported: every side effect happens
inside the transport closure, on a call. Importing this module is free.

**Not exercised by the executor's unit tests.** Those stay on the in-memory fake
(dependency injection is the correct design, not a workaround). The one test this
module owns proves the custody wiring -- that the transport asks the vault for the
binding's ``secret_ref`` name -- with an injected vault and an injected sender, so
it too performs no network.

**https only.** A ``Bearer`` credential over plain ``http`` is a credential sent
in clear, so :func:`urllib_http_send` refuses any scheme but ``https`` rather than
leaving it to the caller to remember.

**And https only PER HOP.** Checking the scheme of the URL a locator produced is
not the same as checking where the credential actually goes: ``urllib``'s default
opener FOLLOWS redirects, and its :class:`urllib.request.HTTPRedirectHandler`
copies every request header except ``Content-Length`` / ``Content-Type`` onto the
next hop -- so a provider (or anything that can answer as one) replying ``302
Location: http://elsewhere.invalid/`` receives the ``Authorization: Bearer``
header, at a different origin, in clear. That was observed on the wire, not
inferred. :func:`urllib_http_send` therefore installs its OWN opener with
:class:`_GuardedRedirectHandler` and follows NOTHING by default; when a caller
opts in with an explicit egress allowlist, every hop must independently be
``https``, be inside that allowlist, and -- for any hop leaving the original
origin -- travel with the credential headers REMOVED. See
:data:`DEFAULT_MAX_RESPONSE_BYTES` and :data:`DEFAULT_DEADLINE_SECONDS` for the
two other things an unbounded ``urlopen`` will happily do to you.
"""

from __future__ import annotations

import base64
import hmac
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

from kiro_crew.connections.control_plane.binding import (
    SECRET_BACKEND_VAULT,
    Binding,
    SecretRef,
)
from kiro_crew.connections.control_plane.executor import (
    ResponseMetadata,
    Transport,
    TransportResponse,
    is_non_idempotent_effect,
)
from kiro_crew.connections.control_plane.handle import (
    TrustedHandleView,
    _binding_fingerprint,
)
from kiro_crew.connections.control_plane.lifecycle import (
    BindingResolutionError,
    BindingRevokedError,
    BindingStore,
    BindingStoreCorruptError,
)
from kiro_crew.connections.control_plane.operation import (
    CredentialMode,
    OperationDescriptor,
)
from kiro_crew.connections.control_plane.result import (
    DEFAULT_MEDIA_TYPE,
    BytesPayload,
    OperationPayload,
    OperationResult,
    ResultStatus,
    result_with_payload,
)
from kiro_crew.connections.control_plane.writes import ATTEMPT_UNKNOWN
from kiro_crew.secrets import SecretValue

#: Bumped when this module's composition shape changes, mirroring the schema
#: version every sibling control-plane module carries.
#:
#: ``2``: :func:`build_production_transport` does not take a fixed
#: ``secret_ref``. It takes a per-call binding-identity selector, and the secret
#: is selected per call FROM the trusted binding identity the executor passes -- a
#: composition-signature change, so the number moves. :func:`urllib_http_send`
#: also grew the egress-guard parameters (``deadline_seconds``,
#: ``max_response_bytes``, ``allowed_redirect_hosts``, ``hop_log``); they are all
#: keyword-only with safe defaults, so an existing caller keeps working, but the
#: sender's observable behaviour on a redirect changed and that is a shape change
#: in every sense a downstream pin cares about.
#:
#: ``3``: the transport now populates
#: :attr:`~kiro_crew.connections.control_plane.executor.TransportResponse.metadata`
#: from :data:`RESPONSE_METADATA_ALLOWLIST` on every reply-bearing branch (2xx,
#: 412, other), and :func:`response_metadata` is a new public surface. A pin at
#: ``2`` sees no metadata at all, so a caller that should be reading
#: ``retry-after`` off the controlled surface goes looking for a header set this
#: composition deliberately does not hand out.
#:
#: ``4``: the send path resolves the credential through L04's
#: :meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.select_secret`
#: against a LIVE :class:`~kiro_crew.connections.control_plane.lifecycle.BindingStore`,
#: and the slug-derived ``binding_secret_ref`` resolution is GONE from the send
#: path -- there is no fallback branch that derives a vault name from a provider
#: slug. :func:`build_production_transport` therefore takes ``store`` +
#: ``gate`` (a :class:`BindingCustodyGate`) instead of the old
#: ``selector``, which is a composition-signature change; and every call is now
#: fenced against the live store (revoked / stale-generation / tampered refuses
#: before a socket exists), which a pin at ``3`` does not get.
PRODUCTION_SCHEMA_VERSION = 4

#: Default per-socket-operation HTTP timeout. A transport with no timeout can hang
#: a dispatch forever, which is a worse failure than a typed ``temporary`` error.
#: This bounds ONE socket operation (connect, or one read) -- not the call.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Default OVERALL wall-clock budget for one send: 60 seconds, from the moment
#: :func:`urllib_http_send` is entered until the body has been read.
#:
#: A per-operation timeout is not a bound on the call. A server that returns one
#: byte every 29 seconds satisfies a 30-second read timeout forever, and each
#: redirect hop gets its own fresh 30 seconds, so a redirect chain multiplies the
#: caller's expected wait by the number of hops. This is the number that actually
#: terminates the send, and the effective socket timeout is
#: ``min(timeout_seconds, time left on this deadline)`` so the deadline is always
#: the ceiling.
DEFAULT_DEADLINE_SECONDS = 60.0

#: Default cap on the response body this module will buffer: 8 MiB.
#:
#: ``response.read()`` with no argument reads until EOF into memory, so the size
#: of a control-plane process's heap becomes the remote end's choice. 8 MiB is
#: far above any control-plane JSON reply (a Graph page of messages is tens of
#: KiB) and far below anything that threatens a gateway; a body that exceeds it is
#: REFUSED with :class:`ResponseTooLargeError` rather than silently truncated,
#: because a truncated body handed to a vendor decode is the same silent-data-loss
#: shape that made a bare ``2xx -> complete ok`` wrong (see :func:`neutral_decode`).
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Read granularity while enforcing :data:`DEFAULT_MAX_RESPONSE_BYTES`. Small
#: enough that the cap is enforced before a huge body is resident, large enough
#: not to syscall per byte.
_BODY_CHUNK_BYTES = 64 * 1024

#: Headers that carry a credential and MUST NOT cross an origin boundary on a
#: redirect. Deliberately wider than the one header this module sets itself: a
#: vendor-owned locator may add its own key header, and the cost of stripping a
#: header a cross-origin hop did not need is nothing next to forwarding one it
#: should never have seen.
_CREDENTIAL_HEADERS: FrozenSet[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }
)

#: The RESPONSE headers a caller may see, as a CLOSED ALLOWLIST.
#:
#: This is the only path a response header takes to a caller (through
#: :attr:`~kiro_crew.connections.control_plane.executor.ExecutionOutcome.metadata`).
#: Its shape is an allowlist and not a denylist because the two fail in opposite
#: directions: an allowlist drops a header nobody has thought about yet, and a
#: denylist forwards it. A response header set is a CREDENTIAL SURFACE --
#: ``Set-Cookie`` mints a session, ``WWW-Authenticate`` / ``Proxy-Authenticate``
#: carry challenge material, an echoed ``Authorization`` is the bearer token
#: itself -- and a caller serializes, logs and forwards what it is handed, so
#: forwarding the set wholesale is the response-side twin of forwarding a
#: credential across a redirect hop (:data:`_CREDENTIAL_HEADERS`).
#:
#: Every name is lowercase, which is also the key a caller reads it back under.
#: The two groups, and why each is here:
#:
#: * **the rate-limit family** -- the reason this surface exists at all. A caller
#:   that must back off needs the numbers, and there are three live spellings in
#:   the providers this plane serves (the RFC draft's ``ratelimit-*``, GitHub's
#:   ``x-ratelimit-*`` including ``used`` / ``resource``, and the older
#:   ``x-rate-limit-*``), plus ``retry-after``, which is the one every provider
#:   agrees on;
#: * **three benign descriptors** -- ``content-type`` (what the body claims to be,
#:   on a non-2xx where no payload carries it), ``etag`` and ``last-modified``
#:   (what a caller re-derives an ``If-Match`` / ``If-Unmodified-Since``
#:   precondition against on a readback, which is exactly the loop a structured
#:   412 asks for).
#:
#: Nothing here carries a credential, a cookie, a challenge or a principal, and
#: ``test_connections_control_plane_metadata`` pins that this set and
#: :data:`_CREDENTIAL_RESPONSE_HEADERS` stay disjoint so a later edit cannot add
#: one quietly.
RESPONSE_METADATA_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        # rate-limit family
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
        # benign descriptors a caller genuinely needs
        "content-type",
        "etag",
        "last-modified",
    }
)

#: Response headers that carry credential, session or challenge material. NOT the
#: mechanism that drops them -- :data:`RESPONSE_METADATA_ALLOWLIST` being closed is
#: what drops them, and it would drop these even if this set were empty. This set
#: exists to be ASSERTED against: a test pins the two disjoint, so an edit that
#: adds ``set-cookie`` to the allowlist fails a check instead of shipping.
_CREDENTIAL_RESPONSE_HEADERS: FrozenSet[str] = frozenset(
    {
        "set-cookie",
        "set-cookie2",
        "cookie",
        "authorization",
        "proxy-authorization",
        "www-authenticate",
        "proxy-authenticate",
        "authentication-info",
        "proxy-authentication-info",
    }
)

#: Statuses that report a failure IN TRANSIT rather than an answer from the
#: application: no end-to-end commit verdict was reached, so for a non-idempotent
#: write whether the effect landed is UNKNOWN in exactly the way a socket timeout
#: is. A gateway that times out upstream (504) may well have delivered the
#: request; a 500 the origin raised carries no evidence of whether it fired
#: before or after it committed. 500 is INCLUDED here on purpose: the executor's
#: own contract (``ConnectorCall.write_outcome``) states that dropping the 5xx
#: ``unknown`` "would leave the caller inferring 'not applied' from a 5xx, which
#: is the precise inference L07 exists to refuse", so the DEFAULT for a 500 with
#: no proof of non-application must be ``unknown``, not ``None``. The asymmetry
#: this rests on is the one this module already states below: a wrong ``unknown``
#: costs one refused replay the caller can override, a wrong not-applied costs a
#: duplicated write. A vendor owner that KNOWS its API commits-first still
#: NARROWS this by setting ``write_outcome`` itself (see the NAMED GAP in
#: :func:`build_production_transport`); that path is unchanged and does not
#: conflict with the safe default.
_AMBIGUOUS_TRANSIT_STATUSES: FrozenSet[int] = frozenset({500, 502, 503, 504})

#: The request headers that ASSERT a precondition. On a 412 the provider often
#: does not say which one failed, so the transport reports the ones this request
#: actually sent -- never a name it did not send (the executor's
#: ``condition_unknown`` covers the "nothing is known" case).
_PRECONDITION_HEADERS: Tuple[str, ...] = (
    "If-Match",
    "If-None-Match",
    "If-Unmodified-Since",
    "If-Modified-Since",
)


class SecretStore(Protocol):
    """The READ side of the existing vault, as this module needs it.

    :class:`kiro_crew.secrets.SecretVault` satisfies this structurally -- this is
    NOT a second vault, it is the injection seam that lets a test hand in a stub
    instead of building an encrypted store on disk. Nothing here writes, deletes,
    or re-keys a secret; the transport only ever reads one by name.
    """

    def get(self, name: str) -> Optional[SecretValue]:  # pragma: no cover - Protocol
        ...


class SecretResolutionError(Exception):
    """Raised when a binding's :class:`SecretRef` cannot be resolved to a value.

    Carries no secret material and no vault contents -- only the entry NAME that
    was looked up, which is a public, deterministic string recorded on the
    binding's :class:`SecretRef`. The transport catches this and returns a
    typed ``auth`` failure rather than letting it escape, because the
    :data:`~kiro_crew.connections.control_plane.executor.Transport` contract is
    "return a structured envelope, do not raise".
    """


class BindingIdentityMismatchError(SecretResolutionError):
    """Raised when a call's trusted binding identity is not the composed one.

    A subclass of :class:`SecretResolutionError` because the honest description
    is the same -- no secret may be resolved -- and both end as a typed ``auth``
    refusal. It is a distinct class so the transport can say WHICH refusal it
    was without parsing a message.

    Its detail names only trusted, non-invertible material: a
    ``binding_fingerprint`` is a keyed HMAC under a per-process key (see
    ``handle._binding_fingerprint``), a ``service_id`` and a ``credential_mode``
    are L01 closed-set values. No vault entry name, no secret, and no
    ``binding_id`` appears -- and since this is raised BEFORE any vault read, no
    secret exists at the point it is raised.
    """


class RedirectRefusedError(Exception):
    """Raised when a redirect hop fails the per-hop egress check.

    Not a subclass of :class:`SecretResolutionError`: the credential resolved
    fine, and the refusal is about where the provider tried to send it. The
    transport maps it to a typed ``input`` failure -- deterministic, so a retry
    of the identical request is refused identically, and calling it ``temporary``
    would invite a retry loop that cannot succeed.

    The detail names the refused target URL and the reason. That URL came from the
    provider's ``Location`` header, so it is not caller-secret; the credential is
    never in it, and :func:`operation_error` redacts the detail again before it
    leaves the executor.
    """


class ResponseTooLargeError(Exception):
    """Raised when a response body exceeds the caller's ``max_response_bytes``.

    Refusing beats truncating: a truncated body handed to a vendor ``decode``
    parses as a short page and silently loses records, which is the same
    silent-truncation failure :func:`neutral_decode` was corrected for. The
    caller gets a typed ``input`` failure and the bytes already read are dropped.
    """


class TransportDeadlineExceededError(Exception):
    """Raised when the OVERALL send budget (:data:`DEFAULT_DEADLINE_SECONDS`) ran out.

    Distinct from a per-socket timeout so the transport can say the send was cut
    off by OUR clock rather than by the remote end -- but the two are treated
    IDENTICALLY where it matters: on a non-idempotent write both mean the request
    may already have been applied, so both record ``unknown``.
    """


class MalformedResponseBodyError(Exception):
    """Raised when a NON-EMPTY reply body cannot be read as a JSON object.

    A present-but-unparseable 2xx body (mid-transfer truncation, an HTML error
    page served with 200, a gzip fault, or a JSON array/scalar where an object
    was contracted) must NOT collapse to ``{}``: an empty mapping is
    indistinguishable from a genuinely empty body, so a caller reads a reported
    success carrying no data and has no signal to retry -- silent data loss on
    the success path. Refusing with a typed error gives the caller that signal.
    An EMPTY body is not this error; it is a real empty result and still yields
    ``{}``.
    """


def _frozen_binding(binding: Mapping[str, Any]) -> Binding:
    """A read-only, independently-owned snapshot of ``binding``.

    Copies every field into a fresh mapping (so no reference the caller kept can
    reach it) and wraps both the top level AND the nested ``secret_ref`` in
    :class:`~types.MappingProxyType`, so neither the binding's identity fields nor
    its secret reference can be mutated after the snapshot is taken. This is the
    carrier :class:`BindingCustodyGate` holds and hands out once its fingerprint
    check has passed, which is what makes "validate then someone mutates the dict"
    unrepresentable rather than merely re-checked. ``assert_live`` only subscripts
    the binding, so a read-only mapping satisfies every consumer downstream.
    """

    snapshot: dict[str, Any] = dict(binding)
    secret_ref = snapshot.get("secret_ref")
    if isinstance(secret_ref, Mapping):
        snapshot["secret_ref"] = MappingProxyType(dict(secret_ref))
    return MappingProxyType(snapshot)  # type: ignore[return-value]


@dataclass(frozen=True)
class BindingCustodyGate:
    """Answers WHICH binding a call may resolve a credential for. No secret, no ref.

    This exists because a composition that took a fixed ``secret_ref`` would let
    the transport closure resolve that same one on every call, whatever identity
    the call was actually authorized for. ``service_id`` and ``credential_mode``
    -- the two axes the executor passes -- do NOT identify a binding: two
    bindings for two different accounts in two different tenants can agree on
    both, so a transport that only saw those would have no way to tell whose credential
    it was reaching for, and would reach for the one it was built with. A
    transport composed for tenant A could therefore serve a call routed for
    tenant B under A's credential. That is the same "validate one thing, then use
    another" shape the executor's own step 1b closes one layer up.

    The gate is a function, not a constant: :meth:`trusted_binding_for` takes the
    :class:`~kiro_crew.connections.control_plane.handle.TrustedHandleView` the
    executor resolved and returns THE BINDING to fence only when that view's
    binding identity is the one this gate was composed for. On any disagreement --
    or on no view at all -- it raises :class:`BindingIdentityMismatchError` and
    NOTHING is resolved: neither the store nor the vault is asked, so there is no
    window in which the wrong binding's plaintext exists.

    **It does NOT select a secret, and it derives no vault name.** Returning a
    :class:`SecretRef` built by
    :func:`~kiro_crew.connections.control_plane.binding.binding_secret_ref` from a
    provider ``slug`` would collapse every binding of one provider onto ONE
    vault entry, so per-binding custody would be only ever apparent. That derivation is
    gone: the gate hands back a binding, and L04's
    :meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.select_secret`
    is what resolves a credential, taking the ``secret_ref`` from the LIVE STORE's
    own record. The gate holds no ``slug`` and no ``secret_backend`` field for that
    reason: there is nothing here left to derive a name from.

    ``binding`` -- the binding this transport holds custody for, as composed. It
    is a CLAIM, not the authority: what the transport actually uses is whatever
    :meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.assert_live`
    returns for it, so a stale composed copy cannot outvote the store.
    ``binding_fingerprint`` -- the trusted one-way digest of that binding, taken
    from the view of a handle derived for THAT binding. ``service_id`` /
    ``credential_mode`` are READ OFF ``binding`` rather than passed separately, so
    a gate cannot be composed to check the view against one identity while
    presenting another to the fence.

    NAMED GAP (not fixed here, and not claimed): one gate serves ONE binding.
    There is no trusted ``principal -> binding`` resolver on this path, so a
    single transport cannot yet serve many tenants -- it refuses every binding but
    its own. See :func:`build_production_transport`.
    """

    binding: Binding
    binding_fingerprint: str

    def __post_init__(self) -> None:
        """Bind the two fields to each other: the fingerprint MUST be this binding's.

        ``binding`` and ``binding_fingerprint`` arrive as two independent values.
        Without checking that the fingerprint is the one-way digest of THIS
        binding's id, a gate could be composed with binding B but A's fingerprint:
        a call carrying A's trusted view would then match the fingerprint, pass
        every axis when A and B share ``service_id`` / ``credential_mode``, and get
        B's credential selected and sent -- cross-binding credential exposure. So a
        gate whose fingerprint is not derived from its own binding's id is refused
        at construction; the two can never be paired mismatched. (The eighth
        recurrence of the trusted-source discipline: two values that must pin each
        other are worthless unless compared.)
        """

        expected = _binding_fingerprint(self.binding["binding_id"])
        if not hmac.compare_digest(self.binding_fingerprint or "", expected):
            raise BindingIdentityMismatchError(
                "this gate's binding_fingerprint is not the fingerprint of its own "
                "binding's id; the two must pin each other, refusing to compose a "
                "gate that could fence one binding while carrying another's credential"
            )
        # FREEZE the authorization carrier AT THE MOMENT OF VALIDATION. The root
        # cause this class keeps recurring through (ninth time: record ownership ->
        # returned view -> executor arg -> secret resolution -> candidate set ->
        # assert_live return -> fingerprint cross-check -> and here) is that the
        # authorization value has always travelled as a MUTABLE dict, while each
        # consumer separately decided whether to re-read it. A gate that validated
        # ``self.binding`` and then held the same mutable dict lets any holder of
        # the original reference mutate the id / credential_mode / secret_ref AFTER
        # the checks above have passed -- so a gate composed for binding A, its dict
        # then swapped to B, would fence and send B's credential under A's proof.
        # Adding one more field comparison would only move the seam. The fix is to
        # remove the mutable carrier itself: from this point the gate holds a
        # read-only snapshot (deep, so the nested ``secret_ref`` cannot be swapped
        # either), and :meth:`trusted_binding_for` hands out read-only mappings, so
        # NO consumer downstream has a mutable dict left to re-read. Mutating what
        # the caller passed in does not reach anything the gate uses.
        object.__setattr__(self, "binding", _frozen_binding(self.binding))

    @property
    def service_id(self) -> str:
        """The service this gate holds custody for, off its binding."""

        return self.binding["service_id"]

    @property
    def credential_mode(self) -> CredentialMode:
        """The credential mode this gate holds custody for, off its binding."""

        return self.binding["credential_mode"]

    def trusted_binding_for(self, trusted_view: Optional[TrustedHandleView]) -> Binding:
        """Return the binding to FENCE for this call, or refuse. Resolves nothing.

        Fails closed on a missing view: a transport reached without the trusted
        identity has no basis to pick a binding, and defaulting to "the one I
        was composed with" is precisely the defect. An empty composed fingerprint
        is refused too, so a gate built from an incomplete record cannot
        match everything by comparing blank to blank.

        The returned binding carries the VIEW's ``generation`` -- the generation
        the call actually presents -- not the one frozen into the composed copy, so
        a rotation or revoke that moved the store's live generation is judged
        against what this call claims and is refused by the fence downstream. The
        remaining fields are the composed ones; the store re-checks every one of
        them and hands back its own record, which is what gets used.
        """

        if trusted_view is None:
            raise BindingIdentityMismatchError(
                "no trusted binding identity accompanied this call, so which "
                "binding's credential to resolve is not established; refusing "
                "rather than falling back to the composed one"
            )
        if not self.binding_fingerprint:
            raise BindingIdentityMismatchError(
                "this transport was composed without a binding fingerprint, so "
                "it cannot establish that a call belongs to its binding"
            )
        if not hmac.compare_digest(
            trusted_view.binding_fingerprint or "", self.binding_fingerprint
        ):
            raise BindingIdentityMismatchError(
                "this call's trusted binding is not the binding this transport "
                "holds custody for; no credential is resolved"
            )
        if trusted_view.service_id != self.service_id:
            raise BindingIdentityMismatchError(
                f"this call is routed at service '{trusted_view.service_id}' but "
                f"this transport holds custody for '{self.service_id}'"
            )
        if trusted_view.credential_mode != self.credential_mode:
            raise BindingIdentityMismatchError(
                f"this call authenticates as '{trusted_view.credential_mode}' but "
                f"this transport holds custody for '{self.credential_mode}'"
            )
        # The carrier stays read-only end to end: build the presented binding as a
        # deep-frozen snapshot of the (already frozen) composed binding with only
        # the VIEW's generation substituted, so what leaves this method is a
        # read-only mapping the caller cannot mutate before or after select_secret
        # consumes it. Mutating what the caller passed in does not reach anything
        # the gate uses -- there is no mutable dict handed downstream.
        presented: Binding = _frozen_binding(
            {**self.binding, "generation": trusted_view.generation}  # type: ignore[misc]
        )
        return presented


@dataclass(frozen=True)
class HttpRequest:
    """One concrete outbound HTTP request, as a VENDOR locator shaped it.

    ``method`` -- the HTTP verb. ``url`` -- the absolute ``https`` URL.
    ``headers`` -- vendor headers WITHOUT the credential; the transport adds
    ``Authorization`` itself so a locator never touches a secret. ``body`` --
    the encoded request body, or ``None``.
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None


@dataclass(frozen=True)
class HttpReply:
    """One raw HTTP reply, before it is mapped to a :class:`TransportResponse`.

    ``status`` -- the HTTP status. ``headers`` -- the response headers.
    ``body`` -- the raw response body.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


#: Sends one :class:`HttpRequest` and returns its :class:`HttpReply`.
#: :func:`urllib_http_send` is the production implementation; a test injects its
#: own so no socket is opened.
HttpSend = Callable[..., HttpReply]

#: Shapes an operation + its arguments into one concrete :class:`HttpRequest`.
#: VENDOR-OWNED: it is injected, never implemented here (see the module
#: docstring).
RequestLocator = Callable[..., HttpRequest]

#: Maps a 2xx :class:`HttpReply` to the L01 success envelope -- INCLUDING its
#: ``payload``, which is where the caller's data comes from. VENDOR-OWNED for
#: anything structured: a decode that knows the provider builds a
#: :class:`~kiro_crew.connections.control_plane.result.CollectionPayload` (items)
#: or an
#: :class:`~kiro_crew.connections.control_plane.result.ObjectPayload`, and builds
#: it with
#: :func:`~kiro_crew.connections.control_plane.result.result_with_payload`, passing
#: that provider's cursor as the explicit ``next_cursor`` keyword -- the ONE place
#: a cursor lives.
#: :func:`neutral_decode` is the default and carries the body as raw bytes.
ResultDecode = Callable[..., OperationResult]


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup (HTTP header names are case-insensitive)."""

    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _credential_echo_forms(sent_credential: Optional[str]) -> Tuple[str, ...]:
    """Forms of the outbound credential to screen a response header value for.

    A provider that reflects the bearer into an allowlisted header does not have
    to reflect it RAW: a value could carry a base64 (standard or url-safe) or
    percent-encoded copy and still hand the token back through ``.metadata``. This
    returns the raw credential plus those encoded forms so
    :func:`response_metadata` drops a value containing ANY of them. Only non-empty,
    distinct forms are returned; an empty credential yields no forms (nothing to
    screen). This is a defensive superset of the raw-echo check -- the raw form is
    the realistic buggy-provider case, the encoded forms close the FLAGged
    edge-case escape without waving anything through.
    """

    if not sent_credential:
        return ()
    raw = sent_credential
    forms = {raw}
    raw_bytes = raw.encode("utf-8", "surrogatepass")
    # base64 (standard + url-safe), with and without padding stripped, since a
    # reflected token may drop '=' padding.
    for enc in (base64.b64encode(raw_bytes), base64.urlsafe_b64encode(raw_bytes)):
        s = enc.decode("ascii")
        forms.add(s)
        forms.add(s.rstrip("="))
    # percent-encoding (a URL-embedded reflection).
    forms.add(urllib.parse.quote(raw, safe=""))
    return tuple(f for f in forms if f)


def response_metadata(
    headers: Mapping[str, str], *, sent_credential: Optional[str] = None
) -> ResponseMetadata:
    """Project ``headers`` onto :data:`RESPONSE_METADATA_ALLOWLIST`.

    The ONE place a response header becomes something a caller can read. It walks
    the headers the provider sent, keeps a name only if its lowercase form is on
    the allowlist, and returns a READ-ONLY mapping keyed by that lowercase form.

    Four properties, each load-bearing:

    * **allowlist, not denylist.** A name that is not listed is dropped, so a
      provider's new session-cookie spelling is absent the day it appears rather
      than forwarded until somebody notices. Nothing here consults
      :data:`_CREDENTIAL_RESPONSE_HEADERS` -- it does not need to, and a
      belt-and-braces second filter would invite the next reader to believe the
      denylist is what protects them and relax the allowlist;
    * **the allowlist gates the NAME; a VALUE that echoes the outbound credential
      is still dropped.** An allowlist decides which header names a caller may
      read, but it cannot vouch for what a provider put in the value: a provider
      that reflects the request's ``Authorization`` (or the bearer itself) into an
      allowlisted header would hand the token back through
      :attr:`~kiro_crew.connections.control_plane.executor.TransportResponse.metadata`.
      So when the outbound ``sent_credential`` is known, any kept value that
      CONTAINS it -- raw OR in a base64 / percent-encoded form (see
      :func:`_credential_echo_forms`) -- is dropped rather than surfaced, the same
      discipline as stripping credential headers across a cross-origin redirect,
      applied to the value the allowlist would otherwise wave through;
    * **lowercase keys.** HTTP header names are case-insensitive, so a caller must
      not have to try ``Retry-After`` and then ``retry-after``;
    * **read-only.** A caller cannot mutate one call's metadata into something
      another reader of the same mapping will see.

    A repeated header keeps the FIRST value. ``Set-Cookie`` is the header that
    legitimately repeats, and it is not on the allowlist; for the rate-limit family
    a second value is a malformed response, and picking the first is at least
    deterministic.
    """

    kept: dict[str, str] = {}
    echo_forms = _credential_echo_forms(sent_credential)
    for name, value in headers.items():
        key = name.strip().lower()
        if key not in RESPONSE_METADATA_ALLOWLIST or key in kept:
            continue
        # The allowlist admitted the NAME; refuse the VALUE if it echoes the
        # outbound credential in ANY form. A bearer reflected into an allowlisted
        # header -- raw, or base64/url-encoded -- would otherwise reach the caller
        # through .metadata.
        if echo_forms and any(form in value for form in echo_forms):
            continue
        kept[key] = value
    return MappingProxyType(kept)


#: What a 2xx reply says about its own content, independently of any vendor's
#: paging spelling. This is the axis a naive ``neutral_decode`` would collapse.
#: ``no_content`` -- 204: the provider stated there is nothing, so ``ok`` +
#: ``next_cursor=None`` is a FACT. ``empty_complete`` -- a 2xx with an empty body
#: (a 201/202 acknowledgement): nothing was returned, so there is nothing left to
#: page. ``partial_content`` -- 206: the provider stated this is a fragment.
#: ``cursor_undetermined`` -- a 2xx carrying a body with no vendor decode
#: injected: there IS content, and whether more follows is unknown to this module.
_CONTENT_NO_CONTENT = "no_content"
_CONTENT_EMPTY_COMPLETE = "empty_complete"
_CONTENT_PARTIAL = "partial_content"
_CONTENT_CURSOR_UNDETERMINED = "cursor_undetermined"


@dataclass(frozen=True)
class Decoded2xx:
    """A 2xx reading that keeps what :class:`OperationResult` has no room for.

    :class:`~kiro_crew.connections.control_plane.result.OperationResult` carries
    ``status`` (in ``("ok", "partial")``), ``next_cursor`` and ``payload``. Two
    genuinely different 2xx replies can still land on the SAME ``status`` -- a 204
    that provably has no content, and a 200 whose body is present but whose
    continuation this module cannot know -- and the second must not be reported
    with the first's certainty. This wrapper is where that difference survives.

    ``result`` -- the L01 envelope to hand back (what the transport puts on a
    :class:`~kiro_crew.connections.control_plane.executor.TransportResponse`),
    including the :data:`~kiro_crew.connections.control_plane.result.OperationPayload`
    carrying the body. ``http_status`` -- the status it was read from.
    ``content_kind`` -- one of the four ``_CONTENT_*`` readings above. ``body`` --
    the raw bytes, PRESERVED. ``cursor_determined`` -- False when a vendor
    ``decode`` is required before any claim about completeness can be made; True
    when the status itself settled it.

    ``body`` and ``result["payload"].data`` hold the same bytes on the two
    body-bearing rows, and that redundancy is kept on purpose: ``body`` is this
    wrapper's own record of what came off the wire (a vendor ``decode`` replacing
    the payload with items must not erase it), while the payload is what a
    CONSUMER reads. They are written from the same ``reply.body`` in one place, so
    they cannot disagree.

    RESOLVED (this was a NAMED GAP): a caller that only saw ``result`` could not
    tell ``no_content`` from ``cursor_undetermined``, because
    ``OperationResult`` had no field expressing "content present, continuation
    unknown" -- and, worse, no field expressing the content at all. The envelope
    now carries a ``payload`` (``RESULT_SCHEMA_VERSION`` 2), so a ``no_content``
    row reads ``payload is None`` and a ``cursor_undetermined`` row reads a
    :class:`~kiro_crew.connections.control_plane.result.BytesPayload` holding the
    body. ``status`` is still downgraded to ``partial`` on the undetermined rows
    rather than asserting a completeness nobody established.
    """

    result: OperationResult
    http_status: int
    content_kind: str
    body: bytes = b""
    cursor_determined: bool = True


def _envelope(status: ResultStatus, payload: Optional[OperationPayload] = None) -> OperationResult:
    """Build an L01 result envelope through L01's own constructor.

    The one place :func:`neutral_decode_detail` builds an
    :class:`~kiro_crew.connections.control_plane.result.OperationResult`, so the
    ``status`` argument is typed to L01's closed
    :data:`~kiro_crew.connections.control_plane.result.ResultStatus` set -- a typo
    is a type error here rather than an invalid envelope handed downstream.

    ``next_cursor`` is never passed, so every envelope this neutral path builds
    reports ``None``: a cursor is the VENDOR's to name -- ``@odata.nextLink`` on
    one provider, ``page``/``perPage`` on another -- and this module never reads a
    body, so it has nothing to name one from. The uncertainty that creates is
    reported honestly through ``status`` and ``cursor_determined`` (see
    :func:`neutral_decode_detail`), never through a guessed cursor. It would also
    be REFUSED here for the two non-collection shapes:
    :func:`result_with_payload` raises on a cursor handed with an object or bytes.
    """

    return result_with_payload(payload, status=status)


def _raw_payload(reply: HttpReply) -> BytesPayload:
    """Carry a 2xx body out VERBATIM, as bytes, with the declared media type.

    The neutral half of the data channel. It does not parse, decode, sniff or
    inspect the body -- so an ``xlsx`` reaches the caller byte-identical, and so
    does a JSON body a vendor ``decode`` will structure later.

    Carrying every body as bytes, rather than branching on whether the reply
    "looks binary", is what makes the never-text-coerced property unconditional.
    A branch would need a rule for deciding, and every such rule has a wrong
    answer available: a ``Content-Type`` a provider mislabels, an absent header, an
    Office document served as ``application/octet-stream``. Bytes are the shape no
    reply loses information in, and the media type is REPORTED beside them so a
    consumer (or a vendor decode) can act on what the provider claimed.
    """

    declared = _header(reply.headers, "Content-Type")
    media_type = declared.strip() if declared and declared.strip() else DEFAULT_MEDIA_TYPE
    return BytesPayload(
        data=reply.body,
        media_type=media_type,
        filename=_content_disposition_filename(reply.headers),
    )


def _content_disposition_filename(headers: Mapping[str, str]) -> Optional[str]:
    """The plain ``filename="..."`` a provider offered, or ``None``.

    Only the unextended parameter is read. ``filename*`` (RFC 5987 percent-encoded
    charset form) needs a decode this module deliberately does not do, and a
    consumer that writes bytes to disk must treat any provider-supplied name as
    untrusted anyway -- so an unreadable name is reported as absent rather than
    half-decoded.
    """

    raw = _header(headers, "Content-Disposition")
    if not raw:
        return None
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "filename":
            return value.strip().strip('"') or None
    return None


def neutral_decode_detail(reply: HttpReply) -> Decoded2xx:
    """Read a 2xx honestly, with the body kept and the reading made explicit.

    The status-by-status table, and why each row is what it is:

    ==========================  ================= ========= ================== =============
    reply                       content_kind      status    cursor_determined  payload
    ==========================  ================= ========= ================== =============
    204 (any body)              no_content        ok        True               None
    2xx, empty body             empty_complete    ok        True               None
    206                         partial_content   partial   False              BytesPayload
    2xx, non-empty body         cursor_undeter'd  partial   False              BytesPayload
    ==========================  ================= ========= ================== =============

    * **204** is the one row where ``next_cursor=None`` is knowledge rather than
      assumption: the provider said there is no content, so there is nothing to
      continue. A 204 body is ignored because HTTP says there is not one.
    * **an empty body** on a 200/201/202 is an acknowledgement; nothing came back,
      so nothing is left to page.
    * **206** means the provider itself declared this a fragment, and L01 has the
      word for that on the success side: ``partial``. Reporting ``ok`` said the
      opposite of what the provider said.
    * **a body with no vendor decode** is the row most at risk of a silent
      truncation, in two separate ways. ``next_cursor`` stays ``None`` because
      guessing a provider's cursor spelling (``@odata.nextLink`` vs ``page`` vs
      ``queryMore``) is the vendor owner's call and inventing one here would be
      wrong for every other provider -- but the ``status`` is ``partial``, not
      ``ok``, because "complete" is a claim this module is not in a position to
      make. And the BODY is now carried, as a
      :class:`~kiro_crew.connections.control_plane.result.BytesPayload`: dropping
      it meant a consumer received a verdict and no data at all, which is a worse
      truncation than a missing cursor. A caller that needs items/objects/paging
      injects a ``decode``; ``cursor_determined=False`` is how it knows it must.

    The "no vendor cursor guessing" property is intact: no row READS the body. The
    payload carries it VERBATIM -- see :func:`_raw_payload` -- so nothing here
    parses JSON, decodes UTF-8, or infers structure. Turning those bytes into
    items, an object, or a cursor is the injected
    :data:`ResultDecode`'s job, i.e. the vendor owner's.
    """

    status = int(reply.status)
    if status == 204:
        return Decoded2xx(
            result=_envelope("ok"),
            http_status=status,
            content_kind=_CONTENT_NO_CONTENT,
            body=b"",
            cursor_determined=True,
        )
    if status == 206:
        return Decoded2xx(
            result=_envelope("partial", _raw_payload(reply)),
            http_status=status,
            content_kind=_CONTENT_PARTIAL,
            body=reply.body,
            cursor_determined=False,
        )
    if not reply.body:
        return Decoded2xx(
            result=_envelope("ok"),
            http_status=status,
            content_kind=_CONTENT_EMPTY_COMPLETE,
            body=b"",
            cursor_determined=True,
        )
    return Decoded2xx(
        result=_envelope("partial", _raw_payload(reply)),
        http_status=status,
        content_kind=_CONTENT_CURSOR_UNDETERMINED,
        body=reply.body,
        cursor_determined=False,
    )


def neutral_decode(reply: HttpReply) -> OperationResult:
    """The default 2xx -> :class:`OperationResult` mapping.

    Thin wrapper over :func:`neutral_decode_detail`, which carries the reasoning
    and the full table. It returns only the L01 envelope, because that is what a
    :data:`ResultDecode` is contracted to return -- and the envelope now includes
    the ``payload``, so a caller that only reads the envelope still receives the
    DATA. A caller that also wants ``content_kind`` or ``cursor_determined`` calls
    ``neutral_decode_detail`` directly.

    It NEVER guesses a continuation cursor -- ``next_cursor`` is always ``None``
    here, since the cursor lives at a different place in every provider's body and
    picking one would give every other provider a wrong answer. What it does NOT
    do is claim that ``next_cursor=None`` means COMPLETE for a reply it never
    read (only 204 and an empty body support that, and those are the only two rows
    that report ``ok``), and it does NOT DISCARD the body: a body-bearing 2xx
    comes back as a
    :class:`~kiro_crew.connections.control_plane.result.BytesPayload` holding the
    provider's bytes byte-for-byte, which is what makes an Office download survive
    this path with no vendor decode at all.
    """

    return neutral_decode_detail(reply).result


def resolve_binding_secret(secret_ref: SecretRef, *, vault: SecretStore) -> SecretValue:
    """Resolve L02's :class:`SecretRef` to a live value through the EXISTING vault.

    The one custody path. It reads the recorded entry NAME out of the ref and asks
    the vault for it -- :meth:`kiro_crew.secrets.SecretVault.get`, the same store
    the rest of the product uses -- and it refuses anything else:

    - a ref naming a backend other than :data:`SECRET_BACKEND_VAULT` is refused
      rather than assumed to mean the vault (the field exists precisely so a
      resolver does not have to guess);
    - a name the vault does not hold is refused, never substituted with an empty
      credential that would reach the provider as an anonymous call.

    Returns the opaque :class:`kiro_crew.secrets.SecretValue` (whose ``repr`` /
    ``str`` are ``****``), so the plaintext is only ever produced by an explicit
    ``.reveal()`` at the point of use. Raises :class:`SecretResolutionError`.

    **This is NOT the send path.** :func:`build_production_transport` does not call
    it: a send resolves through
    :meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.select_secret`,
    which FENCES against the live store first and takes the reference from the
    store's own record. This helper takes whatever ref it is handed and fences
    nothing, so it must not serve a call -- it is the plain
    ref -> value custody read, kept public because a ref-holding caller outside a
    dispatch (a diagnostic, a vault-presence check) legitimately needs it.
    """

    if secret_ref["backend"] != SECRET_BACKEND_VAULT:
        raise SecretResolutionError(
            f"secret ref names backend {secret_ref['backend']!r}, and this "
            f"composition resolves only {SECRET_BACKEND_VAULT!r}"
        )
    value = vault.get(secret_ref["name"])
    if value is None:
        raise SecretResolutionError(
            f"vault holds no entry named {secret_ref['name']!r}; the binding's "
            "secret must be stored before an operation can be dispatched"
        )
    return value


@dataclass(frozen=True)
class RedirectHop:
    """One redirect hop the egress guard ALLOWED, recorded for audit/test.

    ``from_url`` / ``to_url`` -- the hop. ``same_origin`` -- whether ``to_url``'s
    (scheme, host, port) equals the ORIGINAL request's. ``credential_forwarded``
    -- whether any :data:`_CREDENTIAL_HEADERS` header survived onto ``to_url``.
    That last field is the one worth asserting: it must be False on every hop
    where ``same_origin`` is False, and a test reads it off this record rather
    than trusting a docstring.
    """

    from_url: str
    to_url: str
    same_origin: bool
    credential_forwarded: bool


def _origin_of(url: str) -> Tuple[str, str, int]:
    """The (scheme, host, port) triple a same-origin check compares.

    The port is made EXPLICIT from the scheme when the URL omits it, so
    ``https://h/x`` and ``https://h:443/x`` are one origin rather than two, and
    ``https://h`` and ``http://h`` never collapse into one.
    """

    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    default_port = 443 if scheme == "https" else 80
    port = parsed.port if parsed.port is not None else default_port
    return (scheme, host, int(port))


def _host_key(origin: Tuple[str, str, int]) -> Tuple[str, str]:
    """The two allowlist spellings accepted for ``origin``: bare host and host:port."""

    _scheme, host, port = origin
    return (host, f"{host}:{port}")


def _strip_credentials(request: urllib.request.Request) -> None:
    """Remove every credential header from ``request``, in BOTH header stores.

    ``urllib`` keeps two: ``headers`` (what the caller set) and
    ``unredirected_hdrs`` (what handlers added, which is where ``AbstractHTTPHandler``
    puts things and where an ``add_unredirected_header`` credential would hide).
    Clearing only the first would leave a credential in the second, so both are
    swept. Keys are compared case-insensitively because ``Request.add_header``
    capitalizes them and HTTP header names are case-insensitive anyway.
    """

    for store in (request.headers, request.unredirected_hdrs):
        for name in [key for key in store if key.lower() in _CREDENTIAL_HEADERS]:
            del store[name]


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforces the four per-hop egress rules, or refuses the hop.

    Installed via :func:`urllib.request.build_opener`, which drops the DEFAULT
    :class:`urllib.request.HTTPRedirectHandler` when handed a subclass instance --
    so this fully replaces the follow-anything behaviour rather than layering on
    top of it.

    Per hop, in this order:

    1. **Follow nothing unless asked.** With no allowlist (the default) every
       redirect is refused. A 3xx then reaches the transport as a status, which is
       what a caller wants: a control-plane call whose provider redirected it is a
       locator that needs fixing, not a hop to take on faith.
    2. **Never downgrade.** A non-``https`` target is refused. This is the rule the
       original code could not enforce: it checked the scheme of the URL the
       locator produced, but the credential travels to wherever the hops END, and
       ``https -> http`` puts a ``Bearer`` token in clear.
    3. **Allowlist.** The target's host (or ``host:port``) must be in the injected
       allowlist. Default-deny: the absence of an entry is a refusal.
    4. **Strip credentials off-origin.** A hop leaving the original origin is
       followed WITHOUT any credential header. The credentialed request never
       leaves the origin it was authorized for, so an allowlisted-but-different
       origin gets an anonymous request or nothing at all.

    Rules 2-4 are independent on purpose. Rule 4 is not implied by rule 3: an
    allowlist entry is an operator saying "this host is a legitimate destination",
    never "this host may hold our credential".
    """

    def __init__(
        self,
        *,
        origin: Tuple[str, str, int],
        allowed_hosts: Optional[FrozenSet[str]],
        hop_log: Optional[List[RedirectHop]] = None,
    ) -> None:
        self._origin = origin
        self._allowed_hosts = allowed_hosts
        self._hop_log = hop_log

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        if self._allowed_hosts is None:
            raise RedirectRefusedError(
                f"refusing to follow the HTTP {code} redirect to {newurl!r}: this "
                "send follows no redirects (no egress allowlist was injected), so "
                "a credentialed request cannot be pointed elsewhere"
            )
        try:
            target = _origin_of(newurl)
        except ValueError as exc:
            # A malformed redirect target (e.g. a non-numeric port
            # ``https://host:invalid/``) makes ``urlsplit(...).port`` raise. That is
            # a redirect this send cannot even parse, let alone vouch for, so refuse
            # it as a redirect rather than letting the parse error escape the
            # transport uncaught.
            raise RedirectRefusedError(
                f"refusing the HTTP {code} redirect to {newurl!r}: its target could "
                f"not be parsed as a URL ({exc})"
            ) from exc
        if target[0] != "https":
            raise RedirectRefusedError(
                f"refusing the HTTP {code} redirect to {newurl!r}: it would leave "
                f"https for {target[0]!r}, putting the credential in clear"
            )
        if not any(key in self._allowed_hosts for key in _host_key(target)):
            raise RedirectRefusedError(
                f"refusing the HTTP {code} redirect to {newurl!r}: its host is not "
                "in this send's egress allowlist"
            )

        following = super().redirect_request(req, fp, code, msg, headers, newurl)
        if following is None:
            return None
        same_origin = target == self._origin
        if not same_origin:
            _strip_credentials(following)
        carried = any(
            name.lower() in _CREDENTIAL_HEADERS
            for store in (following.headers, following.unredirected_hdrs)
            for name in store
        )
        if self._hop_log is not None:
            self._hop_log.append(
                RedirectHop(
                    from_url=req.full_url,
                    to_url=newurl,
                    same_origin=same_origin,
                    credential_forwarded=carried,
                )
            )
        return following


def _read_capped(
    stream: Any,
    *,
    max_response_bytes: int,
    deadline_at: float,
    monotonic: Callable[[], float],
) -> bytes:
    """Read ``stream`` under BOTH bounds: the size cap and the wall-clock deadline.

    ``read()`` with no argument reads to EOF, so the remote end chooses this
    process's memory use and how long the read takes. Chunked reading enforces
    both: one byte past ``max_response_bytes`` raises
    :class:`ResponseTooLargeError` (the extra byte is what makes "at the cap" and
    "over the cap" distinguishable), and the deadline is re-checked between chunks
    so a drip-feeding server cannot outlast it while satisfying every individual
    read timeout.
    """

    chunks: List[bytes] = []
    total = 0
    while True:
        if monotonic() >= deadline_at:
            raise TransportDeadlineExceededError(
                "the overall send deadline elapsed while reading the response body"
            )
        want = min(_BODY_CHUNK_BYTES, max_response_bytes + 1 - total)
        chunk = stream.read(want)
        if not chunk:
            break
        total += len(chunk)
        if total > max_response_bytes:
            raise ResponseTooLargeError(
                f"response body exceeded the {max_response_bytes}-byte cap; "
                "refusing it rather than handing a truncated body to a decode"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def urllib_http_send(
    request: HttpRequest,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    allowed_redirect_hosts: Optional[FrozenSet[str]] = None,
    hop_log: Optional[List[RedirectHop]] = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> HttpReply:
    """Send ``request`` with the standard library's ``urllib.request``.

    The real HTTP client on the production path -- stdlib, so no new dependency.
    A non-2xx is a REPLY, not an exception: ``urllib`` raises
    :class:`urllib.error.HTTPError` for it, and that object carries the status,
    headers and body, so it is unwrapped back into an :class:`HttpReply` for the
    caller to classify. A genuine connectivity failure still raises
    :class:`urllib.error.URLError`, which the transport maps to a typed
    ``temporary`` -- and, for a non-idempotent write, to an ``unknown`` outcome.

    Refuses any scheme but ``https``: the caller is about to attach a ``Bearer``
    credential, and sending that over ``http`` would put it on the wire in clear.

    **It does not use the default opener.** The default one follows redirects and
    copies the ``Authorization`` header onto every hop, so the initial https check
    guarantees nothing about where the credential ends up. This builds its own
    opener around :class:`_GuardedRedirectHandler`; with the default
    ``allowed_redirect_hosts=None`` NOTHING is followed and a 3xx surfaces as a
    status. Pass an allowlist to opt in, and each hop is then checked
    independently for https, membership, and -- off-origin -- credential removal.

    Three bounds, all keyword-only with the module defaults: ``timeout_seconds``
    caps ONE socket operation, ``deadline_seconds`` caps the WHOLE send (the
    effective socket timeout is the smaller of "what is left of the deadline" and
    ``timeout_seconds``, so the deadline is the ceiling even across a redirect
    chain), and ``max_response_bytes`` caps the body.

    ``hop_log`` collects a :class:`RedirectHop` per followed hop, which is how a
    caller (or a test) can assert that no credential crossed an origin instead of
    taking this docstring's word for it. ``monotonic`` is the deadline's clock,
    injectable for a deterministic test.

    A composer binds the non-default egress policy at composition:
    ``http_send=functools.partial(urllib_http_send,
    allowed_redirect_hosts=frozenset({"graph.microsoft.com"}))``. The
    :data:`HttpSend` contract stays ``(request, *, timeout_seconds)``, so every
    existing injected fake is untouched.
    """

    if not request.url.lower().startswith("https://"):
        raise SecretResolutionError("refusing to send a credentialed request over a non-https URL")
    try:
        request_origin = _origin_of(request.url)
    except ValueError as exc:
        # A malformed request URL (e.g. a non-numeric port ``https://host:x/``)
        # makes ``urlsplit(...).port`` raise. Refuse before a socket exists rather
        # than letting the parse error escape untyped -- the same typed pre-send
        # refusal as the non-https guard above.
        raise SecretResolutionError(
            f"refusing to send: the request URL could not be parsed ({exc})"
        ) from exc
    deadline_at = monotonic() + deadline_seconds
    opener = urllib.request.build_opener(
        _GuardedRedirectHandler(
            origin=request_origin,
            allowed_hosts=allowed_redirect_hosts,
            hop_log=hop_log,
        )
    )
    req = urllib.request.Request(  # noqa: S310 - scheme is pinned to https above
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    remaining = deadline_at - monotonic()
    if remaining <= 0:
        raise TransportDeadlineExceededError(
            "the overall send deadline elapsed before the request was opened"
        )
    try:
        with opener.open(req, timeout=min(timeout_seconds, remaining)) as response:
            return HttpReply(
                status=int(response.status),
                headers={k: v for k, v in response.headers.items()},
                body=_read_capped(
                    response,
                    max_response_bytes=max_response_bytes,
                    deadline_at=deadline_at,
                    monotonic=monotonic,
                ),
            )
    except urllib.error.HTTPError as exc:
        # A status the provider chose to return, not a transport failure. Its body
        # is capped too: an error body is as attacker-controlled as a success one.
        return HttpReply(
            status=int(exc.code),
            headers={k: v for k, v in (exc.headers or {}).items()},
            body=_read_capped(
                exc,
                max_response_bytes=max_response_bytes,
                deadline_at=deadline_at,
                monotonic=monotonic,
            ),
        )


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    """The ``Retry-After`` advisory in seconds, or ``None``.

    Only the delta-seconds form is read. The HTTP-date form would need the
    server's clock to be trusted and subtracted from ours -- exactly the
    caller-clock mistake the executor's own clock source exists to avoid -- so it
    is reported as "no advisory" instead of being converted on a guess.
    """

    raw = _header(headers, "Retry-After")
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _asserted_preconditions(headers: Mapping[str, str]) -> Tuple[str, ...]:
    """The precondition headers THIS request actually sent, in canonical order."""

    return tuple(name for name in _PRECONDITION_HEADERS if _header(headers, name) is not None)


def build_production_transport(
    *,
    gate: BindingCustodyGate,
    store: BindingStore,
    vault: SecretStore,
    locator: RequestLocator,
    http_send: HttpSend = urllib_http_send,
    decode: ResultDecode = neutral_decode,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Transport:
    """Compose the real transport the executor dispatches through in production.

    Returns a :data:`~kiro_crew.connections.control_plane.executor.Transport`: it
    accepts the TRUSTED routing axes the executor passes (``service_id`` /
    ``credential_mode`` / ``trusted_view``, all off the handle view, never off the
    handle) and returns a :class:`TransportResponse`, so it drops straight into
    :func:`~kiro_crew.connections.control_plane.executor.execute` in place of the
    unit tests' fake.

    Per call, IN THIS ORDER:

    1. ``gate`` matches the call's trusted binding identity and yields THE BINDING
       to fence -- carrying the generation this call presents. A mismatch refuses
       here -- before the locator runs, before the store or the vault is asked,
       before anything is emitted -- so a transport can never serve a call it does
       not hold custody for, and a refusal leaves no plaintext anywhere.
    2. ``locator`` shapes the vendor request (no credential in it).
    3. ``store.select_secret(binding, reader=vault)`` -- L04's per-binding
       selector -- FENCES the call against the LIVE store and resolves the
       credential from the store's own record. See below.
    4. It is revealed once into an ``Authorization: Bearer`` header.
    5. ``http_send`` emits it; the reply is mapped to a :class:`TransportResponse`.

    **The credential comes from the LIVE STORE, per binding -- never from a slug.**
    Step 3 calls
    :meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.select_secret`,
    which re-reads the store's CURRENT record and (a) refuses a revoked binding, a
    stale or forged ``generation``, or a swapped identity / ``credential_mode`` /
    ``secret_ref`` (:class:`~kiro_crew.connections.control_plane.lifecycle.BindingRevokedError`),
    and (b) takes the ``secret_ref`` from the STORE's record -- never the composed
    binding's copy, and never a name derived from a provider slug. The previous
    composition derived the vault name with
    :func:`~kiro_crew.connections.control_plane.binding.binding_secret_ref` off a
    ``slug``, which collapses every binding of one provider onto ONE vault entry:
    two accounts in two tenants resolved the same credential, so per-binding
    custody was apparent rather than real. That derivation is REMOVED from this
    path and there is no fallback branch that reintroduces it -- ``slug`` is not a
    parameter of this composition, and this module does not import
    ``binding_secret_ref`` at all. A fence refusal and a resolution failure both
    return a typed refusal and emit ZERO calls.

    Because the fence is a fresh live-store read on EVERY call, a revoke or a
    rotation takes effect on the next call with no cache to invalidate -- including
    the next page of a
    :class:`~kiro_crew.connections.control_plane.executor.PageWalk`, since each
    page re-runs the whole chain through this closure rather than reusing page 1's
    answer.

    A 412 carries back the preconditions this request asserted plus the server's
    ``ETag`` -- and NOTHING when it asserted none, which is what lets the executor
    set ``condition_unknown`` instead of inventing an ``If-Match``.

    **Response metadata is allowlisted, on every reply branch.** A reply that came
    from the provider (2xx, 412, or any other status) carries
    :func:`response_metadata` of its headers on
    :attr:`~kiro_crew.connections.control_plane.executor.TransportResponse.metadata`
    -- the rate-limit family plus three benign descriptors, and NOTHING else. The
    raw header set never leaves this closure. That matters most on the failure
    branch, which is the one a caller reads ``retry-after`` off, and it is also the
    branch where a provider is most likely to send ``WWW-Authenticate`` on a 401 or
    ``Set-Cookie`` on a redirect-ish 3xx; those are dropped because they are not on
    the allowlist, not because anything here recognized them.

    Nothing is cached: the fence and the secret are re-resolved every call, so a
    revoked, rotated or deleted credential takes effect immediately and no
    plaintext outlives the call. Failures are returned, never raised -- because the
    transport contract is a structured envelope. Details name only the status, the
    service and the operation: no response body, no header values, no credential.

    **An ambiguous failure is reported as ambiguous.** A connectivity failure used
    to become a flat 503, which the executor classifies ``temporary`` -- and a
    caller recording that as L07's ``failed_not_applied`` would be asserting the
    write did not land. It has no way to know that. A socket timeout, a dropped
    connection, our own overall deadline, a refused redirect (the first request
    WAS sent and answered) and a discarded oversize body (the server answered, we
    could not read it) all leave the effect genuinely undetermined. So for an
    effect :func:`~kiro_crew.connections.control_plane.executor.is_non_idempotent_effect`
    calls non-idempotent, every one of those sets ``write_outcome`` to
    :data:`~kiro_crew.connections.control_plane.writes.ATTEMPT_UNKNOWN`, and L07
    then refuses to blind-replay unless the caller explicitly asserts idempotence.
    The asymmetry is deliberate: a wrong ``unknown`` costs a refused replay the
    caller can override, a wrong ``failed_not_applied`` costs a duplicated
    ``sendMail``. Determinate-and-nothing-sent is kept determinate -- the
    non-https refusal, the binding mismatch and the live-store fence refusal all
    happen before a socket exists, so none of them sets an outcome.
    :data:`_AMBIGUOUS_TRANSIT_STATUSES` (502/503/504) is treated the same way: a
    gateway failure is a failure in transit, so no end-to-end verdict was reached.

    NAMED GAPS (not fixed here, and not claimed as working):

    * **principal -> binding.** A :class:`BindingCustodyGate` is composed for
      ONE binding and refuses every other. There is no trusted resolver on this
      path from a caller/principal to the binding it may act through, so one
      transport cannot serve many tenants -- a caller composes one per binding.
      (L04's ``BindingStore.resolve`` is a trusted principal->binding resolver,
      but it is NOT wired into this composition; the gate is still the one binding
      this transport was built for.) This is a REFUSAL, not multi-identity
      support: nothing here should be read as "real multi-identity runtime
      complete".
    * **L03 (auth-code / PKCE).** NOT implemented anywhere in this stack. The
      credential the vault holds has to have been put there by an authorization
      flow that does not exist yet. This module reads custody; it never
      establishes it, and the store persists a reference, never a value.
    * **Refresh rotation is not driven from here.** L04's
      :meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.rotate`
      exists and is single-writer, but this transport never CALLS it: it does not
      react to a 401 by refreshing. What it does honour is the outcome -- once
      something else rotates or revokes, the next call through here is fenced.
    * **HTTP 500 defaults to ``unknown``, and a vendor owner may NARROW it.**
      500 is IN :data:`_AMBIGUOUS_TRANSIT_STATUSES`: a 500 on a non-idempotent
      write carries no evidence of whether the origin raised it before or after
      committing, and this module's own contract refuses to let a caller infer
      "not applied" from a 5xx. So with no proof of non-application the outcome is
      ``unknown`` and L07 refuses a blind replay (the caller can still override by
      asserting idempotence). "Unknown" IS the state of not knowing whether the
      effect landed, so it is the correct default -- the burden of proof runs the
      other way: a safe default does not wait for a vendor to prove commits-first,
      and the safety asymmetry stated above (a wrong ``unknown`` costs one
      overridable refused replay; a wrong not-applied costs a duplicated write)
      outweighs the convenience of keeping more writes replayable. A vendor owner
      that genuinely KNOWS its API commits-first still narrows this the sanctioned
      way, by setting ``write_outcome`` itself; that path is unchanged and does
      not conflict with the safe default.
    """

    def _transport(
        *,
        service_id: str,
        credential_mode: CredentialMode,
        descriptor: OperationDescriptor,
        request_args: Mapping[str, Any],
        request_idempotency_key: str = "",
        trusted_view: Optional[TrustedHandleView] = None,
        **_ignored: Any,
    ) -> TransportResponse:
        operation_id = descriptor["operation_id"]
        # An UNKNOWN outcome only needs saying for an effect whose replay L07
        # gates; a read has no effect to have half-landed.
        unknown = ATTEMPT_UNKNOWN if is_non_idempotent_effect(descriptor["effect"]) else None

        # (1) Custody is bound to the identity THIS call was authorized for. This
        # runs first so a mismatch emits nothing and resolves nothing.
        try:
            presented = gate.trusted_binding_for(trusted_view)
        except BindingIdentityMismatchError:
            # Determinate: no request existed. The exception's text is not
            # forwarded -- the caller only needs to know it is not authorized.
            return TransportResponse(
                http_status=401,
                detail=(
                    f"operation {operation_id} was not dispatched: this transport "
                    "does not hold credential custody for the calling binding"
                ),
            )

        request = locator(
            service_id=service_id,
            credential_mode=credential_mode,
            descriptor=descriptor,
            request_args=dict(request_args),
            request_idempotency_key=request_idempotency_key,
        )
        # (3) FENCE against the live store and resolve from ITS record. One call
        # does both, so there is no window between the fence and the read in which
        # the store could move -- and no branch that resolves without fencing.
        try:
            credential = store.select_secret(presented, reader=vault)
        except BindingRevokedError:
            # The live store refused this binding: revoked, or the presented
            # generation/identity/mode/secret_ref is not the live one. Determinate:
            # nothing was sent, and no secret was read. The exception's text names
            # a binding id, which the caller does not need in order to know it is
            # not authorized.
            return TransportResponse(
                http_status=401,
                detail=(
                    f"operation {operation_id} was not dispatched: the binding it "
                    "was authorized under is no longer live"
                ),
            )
        except BindingResolutionError:
            # The store's record names a vault entry the vault does not hold.
            # Typed as an auth failure; the entry NAME is not forwarded.
            return TransportResponse(
                http_status=401,
                detail=f"credential for operation {operation_id} is not available",
            )
        except BindingStoreCorruptError:
            # Fail CLOSED and say so honestly: an unreadable store is not an empty
            # one, and it is not a statement about this binding either. Temporary,
            # because a caller may legitimately retry once the store is readable.
            # Nothing was sent, so this is determinate -- no write_outcome.
            return TransportResponse(
                http_status=503,
                detail=(
                    f"operation {operation_id} was not dispatched: the binding "
                    "store could not be read, so no binding could be fenced"
                ),
            )
        # Everything used below comes from the STORE's answer, never from the
        # composed copy: this is the point of select_secret returning an envelope.
        secret = credential["secret"]
        if not isinstance(secret, SecretValue):  # pragma: no cover - defensive
            return TransportResponse(
                http_status=401,
                detail=f"credential for operation {operation_id} is not available",
            )
        if credential["secret_ref"]["backend"] != SECRET_BACKEND_VAULT:
            # The store's own record points at a backend this transport does not
            # read. Refuse rather than reveal a value it cannot vouch for.
            return TransportResponse(
                http_status=401,
                detail=f"credential for operation {operation_id} is not available",
            )

        headers = dict(request.headers)
        # The ONE place the plaintext exists, on a local that dies with the call.
        # All three L01 credential modes present as a bearer credential here; a
        # provider needing another header shape is a vendor-owned locator concern.
        sent_credential = secret.reveal()
        headers["Authorization"] = f"Bearer {sent_credential}"

        try:
            reply = http_send(
                HttpRequest(
                    method=request.method,
                    url=request.url,
                    headers=headers,
                    body=request.body,
                ),
                timeout_seconds=timeout_seconds,
            )
        except SecretResolutionError:
            # urllib_http_send's https refusal, raised BEFORE a socket exists.
            # Nothing was sent, so the outcome is determinate.
            return TransportResponse(
                http_status=400,
                detail=f"operation {operation_id} was not dispatched over https",
            )
        except (
            urllib.error.URLError,
            ConnectionError,
            http.client.HTTPException,
            TimeoutError,
            TransportDeadlineExceededError,
        ):
            # Ambiguous by construction: the transport cannot tell whether the
            # request bytes reached the server. See the docstring on why this is
            # `unknown` for a non-idempotent write rather than a determinate
            # not-applied. The tuple names each ambiguity family explicitly rather
            # than catching Exception (which would misclassify a programming error
            # as network ambiguity): `urllib.error.URLError` is what the default
            # opener wraps a connect/read failure in, but a raw
            # `http.client.RemoteDisconnected` -- a server-committed-then-dropped
            # reply -- is a `ConnectionResetError` (-> `ConnectionError`) AND an
            # `http.client.BadStatusLine` (-> `HTTPException`), and is NOT a
            # `URLError`, so it escaped this branch until both bases were named.
            # `ConnectionError` also covers ConnectionAborted / BrokenPipe;
            # `http.client.HTTPException` also covers IncompleteRead / BadStatusLine.
            return TransportResponse(
                http_status=503,
                detail=f"could not reach {service_id} for operation {operation_id}",
                write_outcome=unknown,
            )
        except RedirectRefusedError:
            # The first request WAS sent and the provider answered with a 3xx we
            # refuse to follow, so for a write the effect is undetermined.
            return TransportResponse(
                http_status=400,
                detail=(
                    f"operation {operation_id} was redirected to a target this "
                    "transport refuses to send a credential to"
                ),
                write_outcome=unknown,
            )
        except ResponseTooLargeError:
            # The server answered; we refused to buffer the answer. What it said
            # about the write is exactly what we do not know.
            return TransportResponse(
                http_status=400,
                detail=(
                    f"{service_id} returned a response for operation {operation_id} "
                    "larger than this transport will buffer"
                ),
                write_outcome=unknown,
            )

        # The provider answered, so there is response metadata to project. ONE
        # call, reused by all three reply branches below, so a branch cannot be
        # added later that forgets it -- or that reaches for reply.headers raw.
        # `sent_credential` is passed so an allowlisted header whose VALUE echoes
        # the outbound bearer is dropped, not handed to the caller.
        metadata = response_metadata(reply.headers, sent_credential=sent_credential)

        if 200 <= reply.status < 300:
            return TransportResponse(
                http_status=reply.status, result=decode(reply), metadata=metadata
            )
        if reply.status == 412:
            return TransportResponse(
                http_status=412,
                preconditions=_asserted_preconditions(headers),
                etag=_header(reply.headers, "ETag"),
                detail=f"precondition failed for operation {operation_id}",
                metadata=metadata,
            )
        return TransportResponse(
            http_status=reply.status,
            retry_after_seconds=_retry_after(reply.headers),
            detail=f"{service_id} returned HTTP {reply.status} for operation {operation_id}",
            write_outcome=unknown if reply.status in _AMBIGUOUS_TRANSIT_STATUSES else None,
            metadata=metadata,
        )

    return _transport


def decode_json_body(reply: HttpReply) -> Mapping[str, Any]:
    """Parse a JSON reply body into an object, refusing a malformed non-empty body.

    A helper for a vendor-owned ``decode``: it does the byte/JSON mechanics (this
    module's business) without deciding where a cursor lives (the vendor's).

    An EMPTY body is a genuine empty result and returns ``{}``. A NON-EMPTY body
    that is not valid UTF-8 JSON, or that parses to something other than a JSON
    object (an array, a scalar), is REFUSED with
    :class:`MalformedResponseBodyError` rather than collapsed to ``{}``: a silent
    ``{}`` on a 2xx is indistinguishable from a real empty body, so a caller reads
    a reported success carrying no data with no signal to retry -- silent data
    loss. The typed error is that signal.
    """

    if not reply.body:
        return {}
    try:
        parsed = json.loads(reply.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedResponseBodyError(
            "a non-empty 2xx body could not be read as JSON; refusing to report it "
            "as an empty success"
        ) from exc
    if not isinstance(parsed, dict):
        raise MalformedResponseBodyError(
            "a non-empty 2xx body parsed as JSON but not as an object; refusing to "
            "report it as an empty success"
        )
    return parsed


__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "PRODUCTION_SCHEMA_VERSION",
    "RESPONSE_METADATA_ALLOWLIST",
    "BindingCustodyGate",
    "BindingIdentityMismatchError",
    "Decoded2xx",
    "HttpReply",
    "HttpRequest",
    "HttpSend",
    "MalformedResponseBodyError",
    "RedirectHop",
    "RedirectRefusedError",
    "RequestLocator",
    "ResponseTooLargeError",
    "ResultDecode",
    "SecretResolutionError",
    "SecretStore",
    "TransportDeadlineExceededError",
    "build_production_transport",
    "decode_json_body",
    "neutral_decode",
    "neutral_decode_detail",
    "resolve_binding_secret",
    "response_metadata",
    "urllib_http_send",
]

"""GitHub dispatch: drive a real GitHub operation through W01's executor.

WHAT THIS OWNS
==============
This is the piece that makes a GitHub operation ACTUALLY INVOKED -- located,
dispatched through W01's real executor and transport, decoded, and paged -- with
no naked sender anywhere. ``locator.py`` shapes the request and ``decoder.py``
reads the reply; this module is the caller that wires them into
:func:`~kiro_crew.connections.control_plane.executor.execute` and
:class:`~kiro_crew.connections.control_plane.executor.PageWalk`, over the
production transport composed by
:func:`~kiro_crew.connections.control_plane.production.build_production_transport`.

It consumes, and RE-IMPLEMENTS NOTHING, of W01's judgment chain:

* auth / handle trust / credential-mode permit / five-layer governance /
  write-replay all run inside ``execute`` -- this module supplies the inputs and
  never re-decides any of them;
* custody is W01's ``BindingCustodyGate`` (a function of the trusted handle
  view) + L04's ``BindingStore.select_secret``, fenced per call to the trusted
  binding identity -- multi-binding therefore goes through W01's per-binding
  live-store selection (e8), never a local substitute;
* the transport is W01's ``build_production_transport`` with GitHub's own
  ``locator`` / ``decoder`` injected -- the ONLY sender. Nothing here touches
  ``urllib`` / ``requests`` / ``httpx``.

The one GitHub-specific decision this module makes is which decoder a given
operation's pagination contract needs (:func:`decode_for`), and how to translate
a rich :class:`~kiro_crew.connections.vendors.github.descriptors.GithubOperationDescriptor`
into the five-field control-plane
:class:`~kiro_crew.connections.control_plane.operation.OperationDescriptor` the
executor dispatches by (:func:`control_plane_descriptor`).

SCHEMA VERSIONS THIS BUILDS AGAINST
===================================
Pinned so a shape change in the seam this consumes is a visible break:

* ``EXECUTOR_SCHEMA_VERSION == 4`` -- the executor envelope now exposes
  ``ExecutionOutcome.payload`` (the neutral data channel) beside the
  transport callable and paging surface this drives.
* ``PRODUCTION_SCHEMA_VERSION == 4`` -- ``build_production_transport`` takes a
  ``BindingCustodyGate`` + an L04 ``BindingStore`` (per-call fence + live-store
  credential selection) and threads the payload through.
* ``RESULT_SCHEMA_VERSION == 3`` -- ``OperationResult`` carries a single
  authoritative ``next_cursor`` plus the ``payload`` union
  (collection/object/bytes); the cursor lives ONLY on the envelope.
* ``OPERATION_SCHEMA_VERSION == 2`` -- the ``OperationDescriptor`` TypedDict shape.

:func:`assert_schema_versions` checks these at composition so a mismatch is a
loud failure, not a silently wrong decode.

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No auth, no custody, no retry, no fencing, no error classification, no
pagination engine -- all W01's. No credential ever reaches this module: it hands
the transport a ``vault`` (a ``SecretStore``), a ``gate`` and the live
``store``; the secret is
resolved inside the transport, revealed once into a header there, and never seen
here. It opens no socket.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, cast

from kiro_crew.connections.control_plane.auth_modes import PermittedModes
from kiro_crew.connections.control_plane.executor import (
    EXECUTOR_SCHEMA_VERSION,
    Clock,
    ExecutionOutcome,
    PageWalk,
    Transport,
    advance_page,
    execute,
)
from kiro_crew.connections.control_plane.handle import DerivedHandle
from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.operation import (
    OPERATION_SCHEMA_VERSION,
    CredentialMode,
    OperationDescriptor,
    OperationKind,
    ServiceId,
)
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import (
    PRODUCTION_SCHEMA_VERSION,
    BindingCustodyGate,
    ResultDecode,
    SecretStore,
    build_production_transport,
)
from kiro_crew.connections.control_plane.result import RESULT_SCHEMA_VERSION
from kiro_crew.connections.control_plane.writes import AttemptRecord
from kiro_crew.connections.vendors.github.decoder import (
    decode_cursor_page,
    decode_rest_page,
    decode_single,
)
from kiro_crew.connections.vendors.github.descriptors import (
    GithubOperationDescriptor,
    Pagination,
    get_descriptor,
)
from kiro_crew.connections.vendors.github.locator import locate

#: The provider slug whose vault-secret family a GitHub binding's credential
#: belongs to (registry.json ``slug``: ``github``). ``create_binding(slug=...)``
#: sets the binding's own ``secret_ref`` from it; L04's ``select_secret`` then
#: reads that ref off the LIVE STORE record per binding -- the slug is not what
#: the transport resolves against, only how the binding's secret is seeded.
GITHUB_SLUG = "github"

#: The neutral service range GitHub operations route at (L01 closed set).
GITHUB_SERVICE_ID: ServiceId = "github"

#: The schema versions of the W01 seam this dispatch was built against. Named so
#: a downstream reader sees exactly what shapes it pins, and
#: :func:`assert_schema_versions` can fail loudly on a drift.
BUILT_AGAINST_EXECUTOR_SCHEMA = 4
BUILT_AGAINST_PRODUCTION_SCHEMA = 4
BUILT_AGAINST_RESULT_SCHEMA = 3
BUILT_AGAINST_OPERATION_SCHEMA = 2


class GithubDispatchError(RuntimeError):
    """A GitHub dispatch could not be composed against the W01 seam.

    Raised by :func:`assert_schema_versions` on a schema drift, and by
    :func:`control_plane_descriptor` for an unknown ``operation_id``. Not a
    vendor error (those are the executor's typed boundary) -- a composition /
    shaping fault this module refuses to proceed past.
    """


def assert_schema_versions() -> None:
    """Fail loudly if the consumed W01 seam is not the version this pins.

    A schema constant this module encodes against having moved means the
    envelope, the transport contract, or the descriptor shape changed under it,
    and a silently-wrong decode is worse than a refusal to compose. Called at the
    top of :func:`build_github_transport` so no dispatch runs against a drifted
    seam.
    """

    mismatches = []
    if EXECUTOR_SCHEMA_VERSION != BUILT_AGAINST_EXECUTOR_SCHEMA:
        mismatches.append(
            f"executor schema is {EXECUTOR_SCHEMA_VERSION}, built against "
            f"{BUILT_AGAINST_EXECUTOR_SCHEMA}"
        )
    if PRODUCTION_SCHEMA_VERSION != BUILT_AGAINST_PRODUCTION_SCHEMA:
        mismatches.append(
            f"production schema is {PRODUCTION_SCHEMA_VERSION}, built against "
            f"{BUILT_AGAINST_PRODUCTION_SCHEMA}"
        )
    if RESULT_SCHEMA_VERSION != BUILT_AGAINST_RESULT_SCHEMA:
        mismatches.append(
            f"result schema is {RESULT_SCHEMA_VERSION}, built against "
            f"{BUILT_AGAINST_RESULT_SCHEMA}"
        )
    if OPERATION_SCHEMA_VERSION != BUILT_AGAINST_OPERATION_SCHEMA:
        mismatches.append(
            f"operation schema is {OPERATION_SCHEMA_VERSION}, built against "
            f"{BUILT_AGAINST_OPERATION_SCHEMA}"
        )
    if mismatches:
        raise GithubDispatchError(
            "GitHub dispatch was built against a different W01 seam version: "
            + "; ".join(mismatches)
            + " -- re-verify the shapes before dispatching"
        )


def control_plane_descriptor(operation_id: str) -> OperationDescriptor:
    """Project a GitHub instance-data row into the executor's descriptor.

    The executor dispatches by the minimal five-field
    :class:`~kiro_crew.connections.control_plane.operation.OperationDescriptor`
    (``operation_id`` / ``service_id`` / ``operation_kind`` / ``effect`` /
    ``credential_modes``). The rich
    :class:`~kiro_crew.connections.vendors.github.descriptors.GithubOperationDescriptor`
    carries all of these facts already; this reads them off the existing table
    (via :func:`get_descriptor`) and never invents a value:

    * ``service_id`` is fixed to :data:`GITHUB_SERVICE_ID`.
    * ``operation_kind`` is derived from the GitHub pagination + effect facts:
      a paginated read is a ``list``, an unpaginated read a ``single_fetch``, a
      mutating effect a ``mutation`` -- the L01 closed-set values, chosen from
      the descriptor's own facts, not guessed from the operation name.
    * ``effect`` and ``credential_modes`` are the descriptor's own ``effect`` and
      ``auth_modes`` (already the shared vocabularies, pinned at GitHub import).

    An unknown ``operation_id`` raises :class:`GithubDispatchError`.
    """

    github = get_descriptor(operation_id)
    if github is None:
        raise GithubDispatchError(
            f"operation {operation_id!r} is not a known GitHub operation"
        )
    # ``auth_modes`` on the GitHub descriptor is typed ``Tuple[str, ...]`` but is
    # pinned at GitHub import to members of the shared ``CREDENTIAL_MODES`` closed
    # set, so casting to the shared type restates a checked fact rather than
    # widening one. The executor re-validates the mode against the descriptor's
    # declared set anyway (``permit_operation``), so a stray value could not slip
    # a call through even if the cast were wrong.
    credential_modes = cast("tuple[CredentialMode, ...]", tuple(github.auth_modes))
    return {
        "operation_id": github.operation_id,
        "service_id": GITHUB_SERVICE_ID,
        "operation_kind": _operation_kind(github),
        "effect": github.effect,
        "credential_modes": credential_modes,
    }


def _operation_kind(github: GithubOperationDescriptor) -> OperationKind:
    """The L01 ``operation_kind`` for a GitHub descriptor, from its own facts.

    A mutating effect is a ``mutation``; a read that paginates is a ``list``; an
    unpaginated read is a ``single_fetch``. A ``search``-endpoint read is still a
    ``list`` here -- the descriptor's endpoint distinguishes search from list,
    but both return a paginated collection, and ``operation_kind`` only needs the
    coarse list/single/mutation shape the executor and manifest read.
    """

    if github.effect != "read":
        return "mutation"
    if github.pagination in (Pagination.REST_PAGE, Pagination.CURSOR_AFTER, Pagination.MIXED):
        return "list"
    return "single_fetch"


def decode_for(operation_id: str) -> ResultDecode:
    """Pick the GitHub decoder a given operation's pagination contract needs.

    ``REST_PAGE`` -> :func:`decode_rest_page` (``Link`` header cursor);
    ``CURSOR_AFTER`` -> :func:`decode_cursor_page` (GraphQL ``after``);
    everything else (``NONE``) -> :func:`decode_single`. ``MIXED`` is refused: a
    single dispatch cannot pick one of a multi-method tool's contracts, so the
    caller must name the concrete method-scoped operation. An unknown operation
    raises :class:`GithubDispatchError`.
    """

    github = get_descriptor(operation_id)
    if github is None:
        raise GithubDispatchError(
            f"operation {operation_id!r} is not a known GitHub operation"
        )
    if github.pagination is Pagination.REST_PAGE:
        return decode_rest_page
    if github.pagination is Pagination.CURSOR_AFTER:
        return decode_cursor_page
    if github.pagination is Pagination.MIXED:
        raise GithubDispatchError(
            f"operation {operation_id!r} has MIXED pagination; dispatch needs a "
            "single contract -- name the method-scoped operation"
        )
    return decode_single


def build_github_transport(
    *,
    operation_id: str,
    gate: BindingCustodyGate,
    store: BindingStore,
    vault: SecretStore,
    http_send: Optional[Any] = None,
) -> Transport:
    """Compose the production transport for ONE GitHub operation.

    Asserts the W01 seam is the version this pins, then hands
    :func:`~kiro_crew.connections.control_plane.production.build_production_transport`
    GitHub's own ``locate`` locator and the operation's ``decode_for`` decoder.
    Custody is W01's: the ``gate`` (a function of the call's trusted handle view,
    composed for one binding identity), the live L04 ``store`` and the ``vault``
    go straight to the production module, which per call fences the binding via
    the gate, resolves the credential from the LIVE STORE's own record
    (``store.select_secret``), and reveals it into a header there -- no credential
    is seen here. The returned :class:`Transport` is the ONLY sender; this module
    never opens a socket.

    ``http_send`` is W01's own
    :data:`~kiro_crew.connections.control_plane.production.HttpSend` injection
    seam, forwarded unchanged: left unset it is the real ``urllib_http_send``
    (the production path); a test injects a controlled sender so no socket opens,
    exactly as the executor's unit tests inject a fake transport. Injecting a
    sender is NOT a naked sender -- the send still goes through W01's transport,
    which resolves custody and attaches the credential; the seam only replaces
    the socket at the bottom.

    A ``gate`` composed for the WRONG binding refuses every call inside the
    transport (W01's ``BindingIdentityMismatchError`` -> a typed ``auth``
    failure) BEFORE the store or vault is asked, so multi-binding is W01's
    per-binding fence + live-store selection, not a local one.
    """

    assert_schema_versions()
    decode = decode_for(operation_id)
    kwargs: dict[str, Any] = dict(
        gate=gate,
        store=store,
        vault=vault,
        locator=locate,
        decode=decode,
    )
    if http_send is not None:
        kwargs["http_send"] = http_send
    return build_production_transport(**kwargs)


def dispatch_operation(
    *,
    operation_id: str,
    handle: DerivedHandle,
    transport: Transport,
    offered_mode: CredentialMode,
    permitted: PermittedModes,
    layers: LayerCeilings,
    governance_scope: str,
    governance_item: str,
    request_args: Optional[Mapping[str, Any]] = None,
    request_idempotency_key: str = "",
    attempt_record: Optional[AttemptRecord] = None,
    clock: Optional[Clock] = None,
) -> ExecutionOutcome:
    """Invoke ONE GitHub operation through W01's executor -- a single call.

    A thin, faithful pass-through to
    :func:`~kiro_crew.connections.control_plane.executor.execute`: it builds the
    control-plane descriptor from the GitHub table and forwards every gate input
    unchanged, so the full judgment chain (handle trust -> caller/view agreement
    -> credential-mode permit -> five-layer governance -> write replay) runs
    inside the executor and the transport is reached only if every gate passes.
    Nothing is re-decided here.

    ``clock`` is forwarded only when supplied (a deterministic test injects one);
    left unset, the executor reads its default server clock fresh per call.
    """

    descriptor = control_plane_descriptor(operation_id)
    kwargs: dict[str, Any] = dict(
        offered_mode=offered_mode,
        permitted=permitted,
        layers=layers,
        governance_scope=governance_scope,
        governance_item=governance_item,
        request_args=request_args or {},
        request_idempotency_key=request_idempotency_key,
        attempt_record=attempt_record,
    )
    if clock is not None:
        kwargs["clock"] = clock
    return execute(descriptor, handle, transport, **kwargs)


def open_page_walk(
    *,
    operation_id: str,
    handle: DerivedHandle,
    transport: Transport,
    offered_mode: CredentialMode,
    permitted: PermittedModes,
    layers: LayerCeilings,
    governance_scope: str,
    governance_item: str,
    base_args: Optional[Mapping[str, Any]] = None,
    clock: Optional[Clock] = None,
) -> PageWalk:
    """Open a W01 :class:`PageWalk` for a paginated GitHub list operation.

    Constructs the executor's own paging driver with the GitHub operation's
    control-plane descriptor and the same gate inputs :func:`dispatch_operation`
    forwards. The walk re-runs the full gate chain on every page and carries
    ``base_args`` (the caller's filters) onto each one, so page 2+ is the same
    query as page 1 -- W01's guarantee, not re-implemented here.

    Drive it with :func:`walk_pages` (or W01's :func:`advance_page` directly).
    """

    descriptor = control_plane_descriptor(operation_id)
    walk_kwargs: dict[str, Any] = dict(
        descriptor=descriptor,
        handle=handle,
        transport=transport,
        offered_mode=offered_mode,
        permitted=permitted,
        layers=layers,
        governance_scope=governance_scope,
        governance_item=governance_item,
        base_args=dict(base_args or {}),
    )
    if clock is not None:
        walk_kwargs["clock"] = clock
    return PageWalk(**walk_kwargs)


def walk_pages(walk: PageWalk, *, runaway_ceiling: int = 1_000_000) -> List[ExecutionOutcome]:
    """Drive a :class:`PageWalk` to completion, returning each page's outcome.

    Calls W01's :func:`~kiro_crew.connections.control_plane.executor.advance_page`
    until the walk sets ``done`` (a terminal page with no ``next_cursor``, a
    denied gate, a transport error, a 412, OR W01's own repeated-cursor guard
    that terminates a non-advancing walk). It adds no paging logic of its own --
    W01's ``PageWalk`` decides cursor advance, repeated-cursor termination and
    per-page re-authorization; this only pumps it to the end.

    It does NOT cap the page count at a data-bearing limit: a legitimately large
    repository (a first full ``since=None`` sync can exceed any small ceiling)
    must complete rather than abort every round. W01's repeated-cursor guard is
    the real terminator; ``runaway_ceiling`` is only a last-resort backstop
    against a provider that both misbehaves AND defeats that guard, set so high
    that no real listing reaches it. Reaching it raises
    :class:`GithubDispatchError` rather than looping forever.
    """

    outcomes: List[ExecutionOutcome] = []
    pages = 0
    while not walk.done:
        outcomes.append(advance_page(walk))
        pages += 1
        if pages >= runaway_ceiling:
            raise GithubDispatchError(
                f"page walk exceeded the runaway ceiling ({runaway_ceiling} "
                "pages) without W01's repeated-cursor guard terminating it"
            )
    return outcomes


__all__ = [
    "BUILT_AGAINST_EXECUTOR_SCHEMA",
    "BUILT_AGAINST_OPERATION_SCHEMA",
    "BUILT_AGAINST_PRODUCTION_SCHEMA",
    "GITHUB_SERVICE_ID",
    "GITHUB_SLUG",
    "GithubDispatchError",
    "assert_schema_versions",
    "build_github_transport",
    "control_plane_descriptor",
    "decode_for",
    "dispatch_operation",
    "open_page_walk",
    "walk_pages",
]

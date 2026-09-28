"""W01 · L01: the typed descriptor one connector operation is dispatched by.

This is the SHARED single source every provider stream (W02..W14) references
its operations through, so the vocabulary a downstream manifest entry declares
in its ``operation_kind`` / ``effect`` / ``service_id`` / ``auth_modes`` fields
and the vocabulary a runtime dispatch switches on are one set of constants,
never two that drift. It is a pure descriptor: no IO, no client, no token --
:mod:`kiro_crew.connections.control_plane.context` carries the per-call
references and this module carries only what is true of the operation itself.

THREE of the enums -- ``operation_kind`` / ``effect`` / ``service_id`` -- are
COPIED VERBATIM from the closed sets fixed in
``docs/system-specs/modules/connector-capability-manifest.md`` (the manifest is
their owning spec, and this module neither invents a value nor drops one). The
fourth vocabulary, ``credential_mode``, is NOT a new axis the manifest lacks: it
IS the manifest's ``auth_modes`` axis (``oauth_user`` / ``fine_grained_pat`` /
``service_to_service``), named here as its own ``Literal`` so a dispatch reads
it directly. If a real operation needs a value none of the four hold, the fix
is a scoped revision of the owning manifest enum, never a value minted here
where a validator built against the manifest would never see it.

``credential_modes`` is a per-operation DECLARATION SET, plural
--------------------------------------------------------------
The manifest defines ``auth_modes`` as a per-operation ARRAY -- "every auth mode
this specific operation supports" -- and a real operation genuinely supports
more than one (W02's GitHub ``get_rate_limit`` declares OAuth + PAT +
service-to-service). So the descriptor carries ``credential_modes: tuple[...]``,
the *set of modes the operation permits*, aligned one-to-one with the manifest's
``auth_modes``. The SINGLE mode a given invocation actually authenticates with
is a property of the CALL, not of the operation, so it lives on the per-call
:class:`kiro_crew.connections.control_plane.context.OperationContext`
(``credential_mode``, singular), chosen from -- and never outside -- the
descriptor's declared set. A single-valued field on the descriptor could not
express a multi-mode operation on this shared seam: downstream would have to
duplicate a descriptor per mode or break the contract W02..W14 all consume.

**Descriptor declares, policy narrows.** The declared set is the OUTER bound: a
governance policy (e.g. L05's ``permit_operation`` / ``PermittedModes``) may
narrow which of the declared modes are permitted in a given context, but MUST
NOT permit a mode the descriptor did not declare. There is one source of truth
for "what modes exist for this operation" -- this descriptor -- and everything
else only subtracts from it.

Two orthogonal axes, same words in this repo today, kept apart on purpose
--------------------------------------------------------------------------
- **Axis A -- registration mode** answers "where did the OAuth *client* come
  from": ``dcr`` (dynamic client registration) vs ``preregistered`` (an app an
  operator registered in the vendor console). It lives in
  :class:`kiro_crew.connections.registry.AuthConfig` and is read through
  :func:`kiro_crew.connections.registry.auth_mode` /
  :func:`kiro_crew.connections.registry.is_preregistered`. This module does not
  restate it, redefine it, or import a value from it.
- **Axis B -- credential mode** answers "what credential does *this operation*
  authenticate its call with": ``oauth_user`` (the user's own OAuth grant),
  ``fine_grained_pat`` (a scoped personal access token), or
  ``service_to_service`` (an application/service credential). Its DECLARATION
  SET lives HERE (``credential_modes``); the SELECTED value lives on the call
  context.

The two combine freely and NEITHER is derived from the other. The manifest
already states the general form of this rule -- "an account type never stands
in for an auth mode" -- and registration mode standing in for credential mode
is the same category error: a ``preregistered`` client (Axis A) can still
authenticate a given operation as ``oauth_user`` OR ``service_to_service``
(Axis B), and a ``dcr`` client says nothing about which credential a particular
operation uses. Code that inferred one axis from the other would reintroduce
exactly the collapse the manifest's two-axis design exists to prevent.
"""

from __future__ import annotations

from typing import Literal, TypedDict

#: Bumped when this descriptor's shape changes, mirroring the module-level
#: schema-version constant ``l0_probe`` / ``l1_smoke`` / ``status`` each carry.
OPERATION_SCHEMA_VERSION = 2

# --- Axis-A note (NOT redefined here) --------------------------------------
# Registration mode (dcr | preregistered) is owned by registry.AuthConfig and
# is deliberately absent from this module's vocabulary. See the module
# docstring: the two axes are kept apart, and this file touches only Axis B.

# --- credential_mode: Axis B -- the manifest's own auth_modes axis ---------
#: The user's own OAuth grant authenticates the call.
CREDENTIAL_MODE_OAUTH_USER = "oauth_user"
#: A scoped, fine-grained personal access token authenticates the call.
CREDENTIAL_MODE_FINE_GRAINED_PAT = "fine_grained_pat"
#: An application / service credential authenticates the call.
CREDENTIAL_MODE_SERVICE_TO_SERVICE = "service_to_service"

#: ``credential_mode`` is a THREE-value closed set -- the manifest's own
#: ``auth_modes`` axis (``oauth_user`` / ``fine_grained_pat`` /
#: ``service_to_service``), named here so a dispatch reads it directly instead
#: of re-deriving it from a registration mode it must not. The operation
#: descriptor declares a SET of these (``credential_modes``); the call context
#: carries the single SELECTED one.
CredentialMode = Literal["oauth_user", "fine_grained_pat", "service_to_service"]

#: The tuple form, for a membership check or a total-coverage assertion that
#: wants the values without reaching into the ``Literal``.
CREDENTIAL_MODES: tuple[CredentialMode, ...] = (
    "oauth_user",
    "fine_grained_pat",
    "service_to_service",
)

# --- operation_kind: copied verbatim from the manifest's closed set --------
#: One of ``single_fetch`` / ``list`` / ``search`` / ``mutation`` / ``stream``.
#: Verbatim from ``connector-capability-manifest.md``; a validator reads this
#: rather than inferring list/search-ness from an operation's name or effect.
OperationKind = Literal["single_fetch", "list", "search", "mutation", "stream"]

#: Tuple form of :data:`OperationKind`'s closed set.
OPERATION_KINDS: tuple[OperationKind, ...] = (
    "single_fetch",
    "list",
    "search",
    "mutation",
    "stream",
)

# --- effect: copied verbatim from the manifest's closed set ----------------
#: One of ``read`` / ``write`` / ``delete`` / ``share`` / ``external_send`` /
#: ``admin`` / ``billable``. Verbatim from ``connector-capability-manifest.md``,
#: the closed vocabulary a governance policy hook matches on.
Effect = Literal["read", "write", "delete", "share", "external_send", "admin", "billable"]

#: Tuple form of :data:`Effect`'s closed set.
EFFECTS: tuple[Effect, ...] = (
    "read",
    "write",
    "delete",
    "share",
    "external_send",
    "admin",
    "billable",
)

# --- service_id: copied verbatim from the manifest's closed set ------------
#: The campaign's neutral service-range identifier. Verbatim from
#: ``connector-capability-manifest.md``: the 12 named service ranges plus the
#: two Office capability sets (``excel_shared_engine`` / ``office_documents``).
ServiceId = Literal[
    "github",
    "gmail",
    "google_drive",
    "sharepoint",
    "outlook",
    "onedrive",
    "onenote",
    "teams",
    "excel_shared_engine",
    "office_documents",
    "slack",
    "asana",
    "salesforce",
    "zoom",
]

#: Tuple form of :data:`ServiceId`'s closed set, in the manifest's own order.
SERVICE_IDS: tuple[ServiceId, ...] = (
    "github",
    "gmail",
    "google_drive",
    "sharepoint",
    "outlook",
    "onedrive",
    "onenote",
    "teams",
    "excel_shared_engine",
    "office_documents",
    "slack",
    "asana",
    "salesforce",
    "zoom",
)


class OperationDescriptor(TypedDict):
    """What is true of one connector operation, independent of any one call.

    A ``TypedDict`` in the shape ``l0_probe.ProbeResult`` /
    ``l1_smoke.SmokeResult`` / ``status.ConnectionStatus`` already use: every
    field present, no optional keys, so no consumer ever sees a partial record.

    ``operation_id`` is the stable identifier the manifest assigns and does not
    change across campaign rounds. ``service_id`` is the neutral service range;
    ``operation_kind`` and ``effect`` are the manifest's discriminators.
    ``credential_modes`` is Axis B -- the SET of credentials this operation
    permits, aligned one-to-one with the manifest's per-operation ``auth_modes``
    array. It is plural because a real operation supports more than one mode;
    the single mode a given invocation uses is chosen (from within this set) on
    the per-call :class:`~kiro_crew.connections.control_plane.context.OperationContext`,
    never fixed here. It is carried here rather than derived from the
    registration-mode axis (see the module docstring).
    """

    operation_id: str
    service_id: ServiceId
    operation_kind: OperationKind
    effect: Effect
    credential_modes: tuple[CredentialMode, ...]

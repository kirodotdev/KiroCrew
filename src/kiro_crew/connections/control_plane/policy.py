"""W01 · L06: the five-layer connector governance intersection + approval binding.

This module answers ONE question for a connector operation: *given every
governance layer that has an opinion, is this scoped item permitted, and does an
approval on record still apply to the exact parameters it was granted for?* It
is the point where the connector control plane composes governance narrower than
the two layers ``platform/governance.py`` already knows about, WITHOUT changing
that shared module.

Why it sits ON TOP of ``resolve()`` rather than extending it
------------------------------------------------------------
``platform.governance.Decision.layer`` is a two-layer vocabulary today —
``policy | profile | both | default`` — and ``platform.governance.resolve``
composes exactly those two (an enterprise ceiling ∩ a per-surface profile) under
Rule 2 ("an item is permitted only if BOTH levels permit it"). The connector
campaign needs FIVE layers:

    platform ∩ workspace/profile ∩ session/App/job ∩ connection ∩ vendor

Widening ``Decision.layer``'s closed set to name three more layers would edit
``platform/governance.py``, which is **outside this stream's ownership boundary**.
So this module does not touch it. It *consumes* the platform primitives read-only
— ``SCOPE_CATALOG`` (the ONE scope catalog; this module invents no second one),
``resolve``, ``GovernanceCeiling``, ``Profile``, ``Decision`` — and layers the
extra narrowing on top of them.

The composition rule, and why it can only narrow
------------------------------------------------
``resolve(ceiling, profile, scope, item)`` already yields ``platform ∩ profile``.
The remaining three layers are each an ordinary :class:`GovernanceCeiling`, and a
single-ceiling permit is obtained by reusing the SAME primitive with no profile:
``resolve(layer_ceiling, None, scope, item)`` returns that one layer's own
answer. The effective decision is the AND of all five layer permits: any layer
that denies makes the result deny, and no layer can turn another layer's deny
into a permit. Intersection only ever removes elements from the permitted set —
adding a layer can only shrink it, never grow it. That is the load-bearing
"cannot widen" property, and it is a property of AND, not a rule this module has
to police:

    permit(item) ⇔ platform.permits(item)
                 ∧ profile.permits(item)
                 ∧ session_app_job.permits(item)
                 ∧ connection.permits(item)
                 ∧ vendor.permits(item)

so the result is ⊆ every individual layer's permitted set, and ⊆
``resolve(platform, profile, …)`` over the range that call covers.

Ceilings are inputs, never hardcoded here
-----------------------------------------
Neither the connector-capability manifest nor ``connections.md`` defines a
per-layer *legal scope subset* — which scopes a "connection" layer or a "vendor"
layer may govern. This module therefore does NOT invent one: it takes each
layer's ceiling/profile as a parameter and intersects whatever the caller
supplies. A layer with nothing to say passes ``None`` (an ungoverned layer is
unrestricted, so it contributes a permit — exactly ``resolve``'s own
``None`` semantics), and the intersection is unaffected.

Deny-by-default
---------------
An unknown scope (not a live ``SCOPE_CATALOG`` member) and an unknown layer name
are REFUSED, not permitted. A caller that misspells a scope, or asks about a
layer this module does not compose, gets a typed denial rather than a silent
open door.

Approval-parameter binding
--------------------------
An approval is granted for a specific parameter set. Reusing it for a call whose
parameters differ is refused: :func:`approval_applies` binds an approval to a
canonical fingerprint of the parameters it was granted with, and a later call
must present the SAME parameters to reuse it. Different parameters → no reuse,
even for the same scope and item. This closes the "approve once, then swap the
arguments" replay.

Typed refusals
--------------
Every refusal is an L01 :class:`~kiro_crew.connections.control_plane.errors.OperationError`
built with :func:`~kiro_crew.connections.control_plane.errors.operation_error`,
which redacts its ``detail`` unconditionally. This module never hand-assembles an
error string that would bypass that redaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Mapping, Optional, Tuple

from kiro_crew.connections.control_plane.errors import (
    ErrorClass,
    OperationError,
    operation_error,
)

# Read-only consumption of the shared platform governance primitives. This
# module imports them; it neither redefines nor mutates them, and it registers
# no new SCOPE_CATALOG row.
from kiro_crew.platform.governance import (
    SCOPE_CATALOG,
    Decision,
    GovernanceCeiling,
    Profile,
    resolve,
)

#: Bumped when this module's shape changes, mirroring the L01 sibling modules
#: (``operation`` / ``context`` / ``result`` / ``errors`` each pin one).
POLICY_SCHEMA_VERSION = 1

# --- The five governance layers, named as a closed set ---------------------
#: The enterprise/platform ceiling — Level 1 in ``platform/governance.py``.
LAYER_PLATFORM = "platform"
#: The workspace / per-surface profile — Level 2 in ``platform/governance.py``.
LAYER_WORKSPACE = "workspace"
#: The per-session / per-App / per-job narrowing.
LAYER_SESSION = "session"
#: The per-connection narrowing.
LAYER_CONNECTION = "connection"
#: The per-vendor narrowing.
LAYER_VENDOR = "vendor"

#: The FIVE layers, in intersection order (broadest ceiling first). This is the
#: closed set of layer names this module composes; an unknown name is refused
#: (deny-by-default), never treated as an ungoverned pass.
LAYERS: Tuple[str, ...] = (
    LAYER_PLATFORM,
    LAYER_WORKSPACE,
    LAYER_SESSION,
    LAYER_CONNECTION,
    LAYER_VENDOR,
)

#: The five-layer name as a type, for a call site that wants the literal set.
LayerName = Literal["platform", "workspace", "session", "connection", "vendor"]


@dataclass(frozen=True)
class LayerCeilings:
    """The governance input for each of the five layers.

    Every field is OPTIONAL and defaults to ``None``. A ``None`` layer is
    ungoverned — unrestricted — and contributes a permit to the intersection,
    which is exactly ``platform.governance.resolve``'s own ``None`` semantics.
    Nothing per-layer is hardcoded: the caller supplies whatever ceiling applies
    at each layer, and this module intersects them.

    ``platform`` is the enterprise :class:`GovernanceCeiling` (Level 1).
    ``workspace`` is the per-surface :class:`Profile` (Level 2) — these two are
    the pair ``resolve(ceiling, profile, …)`` already composes. ``session``,
    ``connection`` and ``vendor`` are each an additional :class:`GovernanceCeiling`
    layered on top via the same primitive.
    """

    platform: Optional[GovernanceCeiling] = None
    workspace: Optional[Profile] = None
    session: Optional[GovernanceCeiling] = None
    connection: Optional[GovernanceCeiling] = None
    vendor: Optional[GovernanceCeiling] = None


def _known_scope(scope: str) -> bool:
    """True iff ``scope`` is a live ``SCOPE_CATALOG`` member (the one catalog)."""

    return scope in SCOPE_CATALOG


def resolve_layers(layers: LayerCeilings, scope: str, item: str) -> Decision:
    """Resolve ``item`` in ``scope`` across all five layers — the intersection.

    Delegates the platform ∩ workspace pair to ``platform.governance.resolve``
    verbatim, then intersects the three additional ceilings by reusing the SAME
    primitive with ``profile=None`` (a single-ceiling query). Returns a permit
    only if EVERY governing layer permits; the FIRST layer that denies is
    returned, so the ``Decision.layer`` / ``reason`` name the deciding layer.

    Precondition: ``scope`` must be a known ``SCOPE_CATALOG`` member. An unknown
    scope is deny-by-default and is reported by :func:`decide` (which callers use
    for the typed-refusal envelope); this internal resolver assumes the scope was
    already validated, and still refuses to widen — an unknown scope on any layer
    simply cannot match a catalog-governed control, so it can never turn a deny
    into a permit here.

    The permitted set is ⊆ every layer's own permitted set (AND), and ⊆
    ``resolve(platform, workspace, …)`` over the range that pair covers, because
    the three extra layers are further AND-ed on top and AND is monotone
    non-increasing.
    """

    # Layers 1+2: platform ∩ workspace — exactly what resolve() composes.
    dec = resolve(layers.platform, layers.workspace, scope, item)
    if not dec.permitted:
        return dec

    # Layers 3..5: each an additional ceiling, queried through the same
    # primitive with no profile so it contributes only its own answer. The
    # first deny wins and carries that layer's reason.
    for name, ceiling in (
        (LAYER_SESSION, layers.session),
        (LAYER_CONNECTION, layers.connection),
        (LAYER_VENDOR, layers.vendor),
    ):
        if ceiling is None:
            continue  # ungoverned layer = unrestricted = permit
        layer_dec = resolve(ceiling, None, scope, item)
        if not layer_dec.permitted:
            # Re-label so the audit record names the connector layer that
            # denied, not resolve()'s two-layer vocabulary.
            return Decision(
                False,
                f"{name} denies: {layer_dec.reason}",
                rule=layer_dec.rule,
                layer=name,
                item=item,
            )

    return Decision(True, "permitted by all governing layers", rule="l06-intersect", item=item)


def decide(layers: LayerCeilings, scope: str, item: str) -> Tuple[bool, Optional[OperationError]]:
    """Public entry: permit/deny + a typed refusal on deny (deny-by-default).

    Returns ``(True, None)`` when every governing layer permits ``item`` in
    ``scope``. Returns ``(False, OperationError)`` otherwise, with the refusal
    built by :func:`operation_error` so its ``detail`` is redacted
    unconditionally. Deny-by-default: an unknown ``scope`` (not a live
    ``SCOPE_CATALOG`` member) is refused before any layer is consulted.
    """

    if not _known_scope(scope):
        return False, operation_error(
            "forbidden",
            f"unknown scope {scope!r}: not a governed scope, denied by default",
        )

    dec = resolve_layers(layers, scope, item)
    if dec.permitted:
        return True, None
    return False, operation_error("forbidden", f"{scope}:{item} denied — {dec.reason}")


# --- Approval-parameter binding --------------------------------------------
#: The RUN-01 error class a mismatched-parameter approval reuse is refused with:
#: the approval on record does not permit THESE parameters.
_APPROVAL_MISMATCH_ERROR: ErrorClass = "forbidden"


def _fingerprint(params: Mapping[str, object]) -> str:
    """A canonical, order-independent fingerprint of an approval's parameters.

    ``json.dumps(..., sort_keys=True)`` makes two mappings with the same
    key/value pairs in any order compare equal, and any difference — an added
    key, a removed key, a changed value — produce a different fingerprint. This
    is what binds an approval to the EXACT parameter set it was granted for.
    """

    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class Approval:
    """An approval bound to the exact parameters it was granted with.

    An approval is NOT "scope+item may proceed forever": it is "scope+item may
    proceed WITH THESE parameters". Reusing it for a call whose parameters differ
    is a different grant and is refused (see :func:`approval_applies`). The
    fingerprint is computed once, at grant time, from the parameter mapping.
    """

    scope: str
    item: str
    param_fingerprint: str

    @staticmethod
    def grant(scope: str, item: str, params: Mapping[str, object]) -> "Approval":
        """Bind a fresh approval to ``params`` as granted for ``scope`` / ``item``."""

        return Approval(
            scope=scope,
            item=item,
            param_fingerprint=_fingerprint(params),
        )


def approval_applies(
    approval: Approval, scope: str, item: str, params: Mapping[str, object]
) -> Tuple[bool, Optional[OperationError]]:
    """Does ``approval`` cover a call for ``scope`` / ``item`` with ``params``?

    Returns ``(True, None)`` only when the scope, the item, AND the parameter
    fingerprint all match what the approval was granted with. A different
    parameter set — even one added or removed key — is refused with a typed,
    redacted :class:`OperationError`: the approval on record does not permit
    those parameters, and it is NOT reused. Deny-by-default: any mismatch denies.
    """

    if approval.scope != scope or approval.item != item:
        return False, operation_error(
            _APPROVAL_MISMATCH_ERROR,
            f"approval is for {approval.scope}:{approval.item}, not {scope}:{item}",
        )
    if approval.param_fingerprint != _fingerprint(params):
        return False, operation_error(
            _APPROVAL_MISMATCH_ERROR,
            f"approval for {scope}:{item} was granted for different parameters; "
            "it does not apply to this parameter set",
        )
    return True, None

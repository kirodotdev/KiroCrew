"""Which ACP backend a live session is talking to.

This is the ``agent.acp_backend`` axis, and it is NOT the axis
:mod:`kiro_crew.agent_sdk.provider_identity` owns. The two are independent and a
reader who conflates them will draw the wrong conclusion from either:

* ``agent.provider`` -- ``"acp"`` or ``"claude_code"``. Which *seam* serves the
  session. Answered by ``provider_identity.is_claude_code``.
* ``agent.acp_backend`` -- ``""`` (kiro-cli), ``"kas"``, or ``"claude"``. Which
  *harness* the ACP seam spawned. Answered here.

Both can say "claude" about the same session and neither implies the other: the
public build admits only ``provider == "acp"`` while still refusing
``acp_backend == "claude"``, so the pair is genuinely two-dimensional.

Why this module takes the backend NAME and not the provider
-----------------------------------------------------------
Every caller already has to reach the backend string through whatever shape its
provider happens to be in, and those shapes differ for reasons that are not this
module's business -- an ``AcpProvider`` holds it at ``client.backend``, a
runtime-backed ``AcpSessionProvider`` holds it at ``backend``, and the two
existing predicates gate on ``isinstance`` before reading either. Accepting the
provider here would mean either importing ``kiro_crew.providers`` (a forbidden
root, which would put this module on the wrong side of the boundary it exists to
serve) or dropping the ``isinstance`` gate and reading the attribute blind.

Reading it blind is the trap. ``ACP_BACKEND_KIRO`` is the empty string, so a
``getattr(provider, "backend", "")`` that misses -- wrong shape, unstarted
provider, plain mock -- returns the *kiro backend's own value* and the miss is
indistinguishable from a real answer. Comparing to ``ACP_BACKEND_CLAUDE`` is
safe under that failure; comparing to kiro is not. So the lookup stays at the
call site that knows the shape, and only the comparison is centralized.

That also preserves the string-vs-property property the codebase depends on. See
``providers.acp.provider_label``: a ``MagicMock(spec=...)`` constrains attribute
names but not their values, so a spec'd provider's ``is_claude_backend``
*property* reads truthy while its ``client.backend`` *string* does not match any
real backend. Call sites that read the string are mock-safe and call sites that
read the property are not, and that difference is deliberate at each one. This
module changes neither.

``AcpClient._is_claude`` is deliberately not routed here
-------------------------------------------------------
``acp.client.AcpClient`` runs its own ``backend == ACP_BACKEND_CLAUDE`` check and
keeps it. ``agent_sdk`` sits ABOVE ``kiro_crew.acp``, so having the client import
this module would invert that layering and pull the SDK package -- and the
backend-install registry its ``__init__`` builds -- into every client import.
A one-line comparison against a constant both modules already read from
``acp_backends`` is the cheaper duplicate.

Only ``claude`` is here
-----------------------
``providers.acp`` also exposes ``is_kas_backend`` and ``is_kiro_backend``, and
they are deliberately not consolidated: both have zero readers outside
``providers/acp.py``, so there is no second place for them to drift from. And a
bare ``is_kiro_backend_name`` helper would be an attractive nuisance for the
empty-string reason above -- it would answer True for every failed lookup. Add
either one when a real second reader appears, not before.
"""

from __future__ import annotations

from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE

__all__ = ["ACP_BACKEND_CLAUDE", "is_claude_backend_name"]


def is_claude_backend_name(backend: str | None) -> bool:
    """Whether *backend* names the claude-agent-acp harness.

    Takes the ``acp_backend`` string a caller already resolved from its own
    provider shape -- deliberately the same calling convention as
    ``provider_identity.is_claude_code``, so both provider axes are asked the
    same way.

    Named ``..._name`` rather than ``is_claude_backend`` because
    ``providers.acp`` already exports an ``is_claude_backend`` that takes a
    *provider*. Two functions with one name and different argument types, one
    ``isinstance``-gated and one not, is how a call site ends up passing the
    wrong thing to the wrong one.

    A missing or empty value is NOT the claude backend: empty is the kiro
    backend's own spelling, so this answers False rather than guessing.
    """
    return backend == ACP_BACKEND_CLAUDE

"""KAS's meta-capability hierarchy, for code outside the agent backend.

The policy language groups capabilities under ``all``, ``builtin`` and ``filesystem``.
KAS's own table of what each group holds is
:data:`kiro_crew.acp.kas_permissions.META_CAPABILITY_EXPANSION`; the enterprise
policy translator reads it here instead of keeping a second copy.
"""

from __future__ import annotations

__all__ = ["meta_capability_expansion"]


def meta_capability_expansion(capability: str) -> tuple[str, ...] | None:
    """The capabilities meta-capability *capability* holds, or ``None`` if it is not one."""
    # Function-local: the ACP layer is loaded only when a meta rule is read.
    from kiro_crew.acp.kas_permissions import META_CAPABILITY_EXPANSION

    return META_CAPABILITY_EXPANSION.get(capability)

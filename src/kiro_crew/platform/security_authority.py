"""The ADD-only PolicyAuthority — the deny-floor security invariant.

The un-weakenable deny floor is **ADD-only at the contract boundary**: a
companion or plugin may *add* deny patterns but can never remove or weaken it.
Under the user-configurable-denied-commands model the floor is composed of two
things, **neither** a static built-in pattern list:

* the companion's ADD-only :class:`SecurityOverlay` — structurally un-removable
  because :meth:`PolicyAuthority.is_denied` and
  :meth:`PolicyAuthority.effective_patterns` are ``@final`` and the overlay is
  union-only (there is no method that subtracts from or replaces it); and
* governance ``commands``-scope **pins** — loaded from ``security_policy.json``
  and applied tightest-wins in ``governance.py`` (not visible to this module).

The built-in ``security.BUILTIN_DENY_PATTERNS`` are **not** part of the
un-weakenable baseline: they are the DEFAULT-ON but USER-DISABLEABLE tier,
filtered per-call via the ``denied_regexes`` regex tier and enforced at
``hooks.py``'s PreToolUse gate.  ``BASELINE_DENY`` is therefore the empty
tuple — the static OSS floor carries no pattern.

:func:`assert_security_floor` (called at boot) consequently guarantees only the
ADD-only overlay contract: that the authority is a :class:`PolicyAuthority` and
has not overridden the ``@final`` decision methods.  It does not assert a
built-in superset or probe git-publish, because those rules are
user-disableable.

The actual deny evaluation (two-pass, git-publish verb anchoring, SEL audit)
still lives in ``security.py`` and is reused verbatim — the overlay patterns
flow through the existing ``extra_patterns`` parameter (never filtered by user
opt-out), and the disableable regex tier flows through ``denied_regexes``.

See ``docs/system-specs/modules/platform-context.md`` (Security floor section).
"""

from __future__ import annotations

from typing import Protocol, Tuple, final, runtime_checkable

from kiro_crew import security
from kiro_crew.platform.context import PlatformCompositionError

# The static, un-weakenable OSS deny floor — EMPTY.
#
# The built-in deny patterns live in the disableable tier
# (``security.BUILTIN_DENY_PATTERNS``, filtered per-call via ``denied_regexes``
# and enforced at ``hooks.py``'s PreToolUse gate).  The un-opt-out-able floor is
# supplied dynamically by (a) the companion :class:`SecurityOverlay` and
# (b) governance ``commands``-scope pins, so there is no static pattern list to
# assert here.  The name is kept because ``platform/__init__.py`` re-exports
# it and tests import it.
BASELINE_DENY: Tuple[str, ...] = ()


@runtime_checkable
class SecurityOverlay(Protocol):
    """The only override surface for the deny floor.

    An overlay can *add* deny patterns.  It cannot see, modify, or remove the
    baseline.  Returning an empty tuple is a valid no-op overlay.
    """

    def extra_deny_patterns(self) -> Tuple[str, ...]: ...


class _NullOverlay:
    """The public default overlay — adds nothing."""

    def extra_deny_patterns(self) -> Tuple[str, ...]:
        return ()


class PolicyAuthority:
    """Concrete deny-floor authority.  Subclassing is allowed; weakening is not.

    A companion does not subclass this to change the decision — it passes a
    :class:`SecurityOverlay` whose extra patterns are unioned onto the
    baseline.  The decision methods are ``@final``.
    """

    def __init__(self, overlay: "SecurityOverlay | None" = None) -> None:
        self._overlay: SecurityOverlay = overlay or _NullOverlay()

    @final
    def effective_patterns(self, extra: Tuple[str, ...] = ()) -> Tuple[str, ...]:
        """Return the un-weakenable floor patterns: overlay ∪ per-call extra.

        Since ``BASELINE_DENY`` is ``()`` this reports the companion
        overlay's ADD-only patterns unioned with any per-call extra — i.e. the
        floor patterns only.  It deliberately does NOT include the disableable
        built-in tier (those are applied inside ``security.is_denied`` and may be
        suppressed via the ``denied_regexes`` regex tier).  Union only: there is
        deliberately no code path that removes an overlay entry.
        """
        return BASELINE_DENY + tuple(self._overlay.extra_deny_patterns()) + tuple(extra)

    @final
    def is_denied(
        self,
        tool_name: str,
        extra_patterns: "list[str] | None" = None,
        *,
        denied_regexes: "list[str] | None" = None,
        reason_notes: "dict[str, str] | None" = None,
    ) -> "str | None":
        """Evaluate a command/tool against the effective deny set.

        The companion overlay patterns are ALWAYS applied: they are prepended to
        ``extra_patterns`` (the glob tier) and forwarded to ``security.is_denied``
        so the two-pass + SEL-audit evaluation is identical for overlay and
        caller-supplied patterns.  ``denied_regexes`` (the disableable built-in +
        user regex tier, resolved by the hooks gate as enabled-built-ins ∪
        user-added, with governance-pinned ids force-re-added) is forwarded
        opaquely.  ``@final`` — the decision cannot be overridden by a subclass.

        ``reason_notes`` maps a user pattern to the operator's own explanation and
        is forwarded opaquely too.  It is presentation-only: it cannot add,
        remove, or alter a match, so it cannot weaken the overlay.

        Critical ADD-only invariant: the overlay patterns travel via
        ``extra_patterns`` and are NEVER routed through ``denied_regexes``, so a
        user opt-out of a built-in rule can never weaken the companion overlay.
        """
        overlay_patterns = list(self._overlay.extra_deny_patterns())
        combined = overlay_patterns + list(extra_patterns or [])
        return security.is_denied(
            tool_name,
            extra_patterns=combined or None,
            denied_regexes=denied_regexes,
            reason_notes=reason_notes,
        )

    @final
    def is_denied_synthesized_target(
        self,
        target: str,
        patterns: "list[str] | None" = None,
        *,
        extra_patterns: "list[str] | None" = None,
        reason_notes: "dict[str, str] | None" = None,
    ) -> "str | None":
        """Evaluate a SYNTHESIZED target (see ``security``); overlay stays ADD-only.

        The overlay is evaluated FIRST and through :func:`security.is_denied` with an
        empty regex tier, so it keeps exactly the semantics it has for a real command.
        This is the ADD-only invariant: an overlay pattern is opaque enterprise policy,
        and one restricting a filesystem SCOPE is spelled as bare path text
        (``*forbidden-share*``), so any host-side narrowing of how it is matched could
        drop a denial its author meant.  It therefore never travels into the synthesized
        tier, whose participating patterns are supplied by the caller.

        ``@final`` -- the decision cannot be overridden by a subclass, and
        ``assert_security_floor`` enforces that at runtime for this method too.
        """
        overlay_patterns = list(self._overlay.extra_deny_patterns())
        if overlay_patterns:
            # Empty regex tier, NOT None: ``None`` fails closed to every built-in, which
            # would evaluate the whole shipped catalogue against a synthesized target and
            # reinstate the collision this tier exists to remove.
            overlay_reason = security.is_denied(
                target,
                overlay_patterns,
                denied_regexes=[],
                reason_notes=reason_notes,
            )
            if overlay_reason:
                return overlay_reason
        return security.is_denied_synthesized_target(
            target,
            patterns,
            extra_patterns=extra_patterns,
            reason_notes=reason_notes,
        )


def assert_security_floor(authority: object) -> None:
    """Boot-time guard: verify the ADD-only overlay contract is intact.

    Under the user-configurable-denied-commands model the built-in patterns are
    user-disableable, so this guard does not assert a built-in superset or
    probe git-publish.  It verifies only the structural ADD-only guarantee: the
    authority is a :class:`PolicyAuthority` and has not overridden its ``@final``
    decision methods at runtime.  This is now the PRIMARY (and only structural)
    protection of the overlay — it MUST stay verbatim, or a companion could
    override ``is_denied`` to always-allow and pass boot.

    The un-weakenable enterprise floor (governance ``commands``-scope pins) is
    enforced separately by ``governance.py``'s gate + ``assert_governance_floor``,
    not here; git-publish is enforced independently by the always-on
    ``_is_git_publish`` floor inside ``security.is_denied``.
    """
    if not isinstance(authority, PolicyAuthority):
        raise PlatformCompositionError(
            f"security authority must be a PolicyAuthority, got {type(authority).__name__}"
        )
    # ``@final`` is a type-checker-only hint with no runtime enforcement, so a
    # subclass could override ``is_denied`` to return ``None`` (allowing every
    # tool).  Verify at runtime that neither decision method was overridden —
    # closes the gap between the documented "no subclass can override the deny
    # decision" invariant and what static analysis enforces.  ``is_denied`` is not
    # the only decision path: ``is_denied_synthesized_target`` decides the same
    # question for a synthesized target, so leaving it out would let a subclass
    # always-allow every file-search call and still pass boot.
    for method_name in ("is_denied", "is_denied_synthesized_target", "effective_patterns"):
        if getattr(type(authority), method_name) is not getattr(PolicyAuthority, method_name):
            raise PlatformCompositionError(
                f"security authority overrides {method_name!r}; the deny decision "
                "is @final and may not be overridden (use a SecurityOverlay instead)."
            )
    # Superset check over the static floor.  ``BASELINE_DENY`` is () for the OSS
    # edition so this is vacuous today; it is retained so any future non-empty
    # static floor is auto-enforced without re-adding the check.
    effective = set(authority.effective_patterns())
    missing = set(BASELINE_DENY) - effective
    if missing:
        raise PlatformCompositionError(
            f"security floor violated: overlay dropped baseline patterns {sorted(missing)}"
        )

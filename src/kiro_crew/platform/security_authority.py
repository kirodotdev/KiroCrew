"""The ADD-only PolicyAuthority — the deny-floor security invariant.

The security floor is **ADD-only at the contract boundary**: a companion or
plugin may *add* deny patterns but can never remove or weaken the baseline.
This is enforced structurally, not by convention:

* :meth:`PolicyAuthority.is_denied` and
  :meth:`PolicyAuthority.effective_patterns` are ``@final`` — no subclass can
  override the deny decision or the way the effective set is built.
* The only override surface is the :class:`SecurityOverlay` protocol, whose
  ``extra_deny_patterns()`` returns patterns that are **concatenated** to the
  baseline.  There is no method anywhere that subtracts from or replaces it.
* :func:`assert_security_floor` (called at boot) rejects any authority whose
  effective set does not contain the full baseline.

The actual deny evaluation (two-pass, git-publish verb anchoring, SEL audit)
lives in ``security.py`` and is reused verbatim — the overlay patterns simply
flow through the existing ``extra_patterns`` parameter, so the audit and
exception machinery is identical for baseline and overlay rules.

See ``docs/system-specs/modules/platform-context.md`` (Security floor section).
"""

from __future__ import annotations

from typing import Protocol, Tuple, final, runtime_checkable

from kiro_crew import security
from kiro_crew.platform.context import PlatformCompositionError

# The immutable baseline — the public core's always-on deny *patterns*.  Snapshot
# as a tuple so it cannot be mutated in place.
#
# NOTE: this is the pattern-list portion of the floor only.  ``security.is_denied``
# ALSO enforces deny rules that are NOT expressed as patterns here — most notably
# the git-publish verb anchoring (``_is_git_publish``).  ``assert_security_floor``
# therefore verifies BOTH that the pattern set is a superset of this baseline AND
# that a representative git-publish command is still denied behaviorally, so the
# floor check does not silently under-cover the non-pattern rules.
BASELINE_DENY: Tuple[str, ...] = tuple(security.BUILTIN_DENY_PATTERNS)

# A command the baseline ``security.is_denied`` MUST block via its (non-pattern)
# git-publish verb anchoring.  Used as a behavioral floor probe so a weakened
# authority cannot pass the floor check by leaving the pattern set intact while
# defeating the git-publish rule.
_GIT_PUBLISH_PROBE = "git push origin main"


@runtime_checkable
class SecurityOverlay(Protocol):
    """The only override surface for the deny floor.

    An overlay can *add* deny patterns.  It cannot see, modify, or remove the
    baseline.  Returning an empty tuple is a valid no-op overlay.
    """

    def extra_deny_patterns(self) -> Tuple[str, ...]:
        ...


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
        """Return BASELINE ∪ overlay ∪ per-call extra.  Union only.

        There is deliberately no code path that removes a baseline entry.
        """
        return BASELINE_DENY + tuple(self._overlay.extra_deny_patterns()) + tuple(extra)

    @final
    def is_denied(self, tool_name: str, extra_patterns: "list[str] | None" = None) -> "str | None":
        """Evaluate a command/tool against the effective deny set.

        Delegates to ``security.is_denied`` with the overlay patterns appended
        to ``extra_patterns`` so the entire two-pass + SEL-audit evaluation is
        identical for baseline and overlay rules.  ``@final`` — the decision
        cannot be overridden by a subclass.
        """
        overlay_patterns = list(self._overlay.extra_deny_patterns())
        combined = overlay_patterns + list(extra_patterns or [])
        return security.is_denied(tool_name, extra_patterns=combined or None)


def assert_security_floor(authority: object) -> None:
    """Boot-time guard: reject anything that would weaken the deny floor.

    Verifies the authority is a :class:`PolicyAuthority`, that its ``@final``
    decision methods have not been overridden at runtime, and that its effective
    pattern set (with no per-call extra) is a superset of the baseline.  A
    companion that returns fewer than baseline patterns fails composition and
    boot aborts.
    """
    if not isinstance(authority, PolicyAuthority):
        raise PlatformCompositionError(
            f"security authority must be a PolicyAuthority, got {type(authority).__name__}"
        )
    # ``@final`` is a type-checker-only hint with no runtime enforcement, so a
    # subclass could override ``is_denied`` to return ``None`` (allowing every
    # tool) while leaving ``effective_patterns`` intact to pass the superset
    # check below.  Verify at runtime that neither decision method was
    # overridden — closes the gap between the documented "no subclass can
    # override the deny decision" invariant and what static analysis enforces.
    for method_name in ("is_denied", "effective_patterns"):
        if getattr(type(authority), method_name) is not getattr(PolicyAuthority, method_name):
            raise PlatformCompositionError(
                f"security authority overrides {method_name!r}; the deny decision "
                "is @final and may not be overridden (use a SecurityOverlay instead)."
            )
    effective = set(authority.effective_patterns())
    missing = set(BASELINE_DENY) - effective
    if missing:
        raise PlatformCompositionError(
            f"security floor violated: overlay dropped baseline patterns {sorted(missing)}"
        )
    # Behavioral floor probe: the pattern superset above does not cover the
    # non-pattern git-publish rule enforced inside ``security.is_denied``.  Verify
    # the authority still denies a representative git-publish command so the floor
    # check covers the full deny floor, not just the pattern list.
    if authority.is_denied(_GIT_PUBLISH_PROBE) is None:
        raise PlatformCompositionError(
            "security floor violated: authority does not deny git-publish "
            f"({_GIT_PUBLISH_PROBE!r}); the deny floor must block it."
        )

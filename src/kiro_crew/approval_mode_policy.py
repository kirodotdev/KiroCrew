"""Whether the policy lets a session hold ``trust`` or ``trust_reads``. From memory.

The ``approval_modes`` scope (KGL ``permission_mode``) may deny ``trust`` and
``trust_reads``; only ``normal`` is the floor. A deny has to hold where a grant is
HONOURED, not only where it is set: a slot that was trusted before the policy landed,
a session whose stored ``approval_policy`` is ``"auto"``, a messaging session in the
Trust set. Those reads happen per tool call and on the 5s status push, so the answer
is resolved once per ceiling install and read from a module flag, like the ``yolo``
verdict in ``safety_override_grant``.

Only the installed enterprise policy (the ceiling's ``approval_modes`` rule) narrows
the two modes; a governance profile does not, as on main. The verdict is therefore a
memory read of the ceiling, with no file I/O, resolved once per ceiling install.

Fail closed only where a rule could narrow the scope: while a new ceiling is being
installed and when the rule cannot be read, both modes
are denied (a person is asked instead) IF the installed ceiling has a rule refusing
one of them (:func:`_rule_may_narrow`). With no such rule, those same moments permit
both modes, as on main: nothing could deny them.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from kiro_crew.platform.context import (
    register_ceiling_install_hook,
    register_ceiling_invalidate_hook,
)

logger = logging.getLogger(__name__)

#: Approval modes a single session may hold. ``yolo`` is process-wide and has its
#: own pushed verdict; ``normal`` is the floor and never denied.
SESSION_TRUST_MODES = ("trust_reads", "trust")
#: Every approval-mode id Crew knows. Anything else is refused, not passed on.
KNOWN_APPROVAL_MODES = ("normal", "trust_reads", "trust", "yolo")

_denied: frozenset[str] = frozenset(SESSION_TRUST_MODES)
_resolved = False
#: Bumped by each ceiling invalidation. A resolve started under an older value was
#: reading a ceiling that is being replaced, so its answer is dropped.
_ceiling_gen = 0
#: True from a ceiling invalidation until its install hook resolves: reads deny.
_installing = False
#: Set while no install is in progress; ``ensure_resolved`` waits on it.
_install_done = threading.Event()
_install_done.set()
#: shortcut: an install that takes longer than this leaves ensure_resolved with a
#: deny; raise it if a slow governance read ever trips it in practice.
_INSTALL_WAIT_SECS = 30.0
#: Called with the denied modes after an install resolves a deny. Each one drops the
#: grants it holds, so a later policy that allows the mode again does not bring a
#: grant back that nobody re-made. The read paths mask on their own; these make the
#: mask and the state agree.
#: Keyed by owner, so a re-registration (a second ``start_dashboard`` in one
#: process) replaces the old callback instead of keeping its state alive.
_deny_hooks: dict[str, Callable[[frozenset[str]], None]] = {}


def register_deny_hook(owner: str, cb: Callable[[frozenset[str]], None]) -> None:
    """Call ``cb(denied)`` after each install that resolves a deny; one per *owner*."""
    _deny_hooks[owner] = cb


def _run_deny_hooks(denied: frozenset[str]) -> None:
    for cb in list(_deny_hooks.values()):
        try:
            cb(denied)
        except Exception:
            logger.warning("a trust-revoke hook raised; its grants are masked only", exc_info=True)


def _ceiling_control() -> object:
    """The installed ceiling's ``approval_modes`` control, or None. Pure memory.

    Only the installed enterprise policy can narrow ``trust`` and ``trust_reads``; a
    governance profile cannot (as on main, where neither is ever narrowed). The
    translator leaves the scope out when no ``permission_mode`` rule restricts it.
    """
    from kiro_crew.platform.context import installed_context
    from kiro_crew.safety_override_permits import _APPROVAL_MODES_SCOPE

    ctx = installed_context()
    ceiling = ctx.governance if ctx is not None else None
    return ceiling.get(_APPROVAL_MODES_SCOPE) if ceiling is not None else None


def _ceiling_denied() -> frozenset[str]:
    """The trust modes the installed ceiling's rule refuses. Pure memory, no file read.

    The LTS baseline control allows both, so it denies nothing. A control that
    cannot be read denies both (fail closed: a rule is there and cannot be asked).
    """
    from kiro_crew.platform.governance_parts.evaluator import _query_level
    from kiro_crew.safety_override_permits import _APPROVAL_MODES_SCOPE

    try:
        control = _ceiling_control()
        if control is None:
            return frozenset()
        return frozenset(
            mode
            for mode in SESSION_TRUST_MODES
            if not _query_level(control, _APPROVAL_MODES_SCOPE, mode).permitted
        )
    except Exception:
        logger.warning("could not read the approval_modes rule; denying trust", exc_info=True)
        return frozenset(SESSION_TRUST_MODES)


def _rule_may_narrow() -> bool:
    """Whether the installed ceiling has a rule that refuses ``trust`` or ``trust_reads``."""
    return bool(_ceiling_denied())


def _resolve() -> frozenset[str]:
    return _ceiling_denied()


def _on_ceiling_invalidating() -> None:
    """Deny both modes before a new ceiling becomes visible. Pure memory."""
    global _denied, _resolved, _ceiling_gen, _installing
    _ceiling_gen += 1
    _installing = True
    _install_done.clear()
    _denied = frozenset(SESSION_TRUST_MODES)
    _resolved = True


def _on_ceiling_installed(ctx: object) -> None:
    global _installing
    if ctx is None:
        reset_state()
        return
    gen = _ceiling_gen
    try:
        # The fence stays up through the read: until the new verdict is stored, the
        # deny-all placeholder must still read as pending, never as a final answer.
        _resolve_now(from_install=True)
    finally:
        if _installing and gen == _ceiling_gen:
            # The read failed or was dropped: lower the fence anyway (fail closed,
            # the placeholder stands). A newer install owns its own fence.
            _installing = False
            _install_done.set()


def _resolve_now(*, from_install: bool = False) -> None:
    """Resolve the verdict from the installed ceiling, store it, drop denied grants."""
    global _denied, _resolved, _installing
    if _installing and not from_install:
        # A ceiling install is in progress: the outgoing ceiling may still be the one
        # a read here would see. Its install hook resolves the new one.
        return
    gen = _ceiling_gen
    denied = _resolve()
    if gen != _ceiling_gen or (_installing and not from_install):
        # A ceiling install started while this resolved: its answer belongs to the
        # outgoing ceiling. Drop it; the install hook resolves the new one.
        return
    _denied, _resolved = denied, True
    if from_install:
        # The verdict is final: lower the fence before the deny hooks read it.
        _installing = False
        _install_done.set()
    if denied:
        _run_deny_hooks(denied)


def _bootstrap() -> None:
    """First read with no install pushed yet (a late import, a CLI, a test)."""
    global _denied, _resolved
    try:
        from kiro_crew.platform.context import current_context

        current_context()
    except Exception:
        # No installed ceiling, so no rule exists that could deny either mode (as main).
        logger.debug("no installed ceiling for the trust verdict", exc_info=True)
        _denied, _resolved = _ceiling_denied(), True
        return
    if not _resolved:
        _resolve_now()


def session_mode_permitted(mode: str) -> bool:
    """True when policy lets a session hold *mode*. ``normal`` always; unknown never."""
    if mode == "normal":
        return True
    if mode not in SESSION_TRUST_MODES:
        return False
    if _installing:
        # The placeholder deny holds only where a rule could narrow the mode.
        return not _rule_may_narrow()
    if not _resolved:
        _bootstrap()
    return mode not in _denied


def verdict_pending() -> bool:
    """True while a ceiling with a narrowing rule is being installed.

    A read then answers deny (fail closed). A caller that would COMMIT that deny as a
    lasting choice (a crewmate's one-shot seed) waits for a final answer instead.
    """
    return _installing and _rule_may_narrow()


def ensure_resolved() -> None:
    """Make the verdict final: wait out an install, then resolve one never resolved.

    MAY BLOCK (a wait): call it off the event loop. A caller
    that COMMITS a mode (a crewmate Profile apply) calls it first, so a passing deny
    is never stored as a choice.
    """
    if _installing:
        _install_done.wait(_INSTALL_WAIT_SECS)
    if not _installing and not _resolved:
        _resolve_now()


def clamp_session_mode(mode: str) -> str:
    """*mode* if the policy permits it, else the highest permitted mode below it.

    For a default nobody chose this session (a crewmate's unset Profile opens in
    ``trust``): it lands on what the policy allows rather than being refused.
    """
    order = ("normal", *SESSION_TRUST_MODES)
    if mode not in order:
        return "normal"
    for candidate in reversed(order[: order.index(mode) + 1]):
        if session_mode_permitted(candidate):
            return candidate
    return "normal"


def stored_auto_permitted() -> bool:
    """Whether an ``approval_policy="auto"`` grant may still be honoured. From memory.

    ``"auto"`` is written by a session Trust (slot trust, a messaging Trust press, a
    crewmate Profile, a spawn's ``approval_mode``) or by a session-wide YOLO grant, and
    its readers (the spawn gate, a subagent's inherited policy, a messaging session's
    own approval) trust it as is. With ``trust`` denied it holds only while a
    session-wide YOLO grant is live; YOLO revocation already clears what it wrote.
    A ``gateway --approval yolo`` grant (``is_cli_launch``) reaches dashboard chats
    only, so it keeps no ``"auto"`` here.
    shortcut: the readers pass no session, so a CLI launch keeps none, a dashboard
    one included; carry the session through if that ever needs to differ.
    """
    if session_mode_permitted("trust"):
        return True
    from kiro_crew.safety_override import safety_override

    so = safety_override()
    return bool(so.is_active()) and so.is_cli_launch is not True


def disabled_session_modes() -> list[str]:
    """The session modes the policy forbids, in picker order."""
    return [m for m in SESSION_TRUST_MODES if not session_mode_permitted(m)]


def reset_state() -> None:
    """Forget the verdict so the next read resolves again. Tests and ``reset_context``."""
    global _denied, _resolved, _installing
    _denied, _resolved, _installing = frozenset(SESSION_TRUST_MODES), False, False
    _install_done.set()


def register_hooks() -> None:
    """Register the mask and the push, AFTER the YOLO verdict's pair.

    Called by :mod:`kiro_crew.safety_override` right after it registers its own.
    The order is load-bearing: the YOLO suspend (``on_policy_suspend``) reads each
    slot's stored ``approval_policy`` through ``get_approval_policy``, which this
    module's mask also answers. Masked first, the suspend would read ``""``, skip a
    YOLO-inherited ``"auto"``, and this push would unmask it again before the YOLO
    verdict resolved. Registration is idempotent per callable.
    """
    register_ceiling_invalidate_hook(_on_ceiling_invalidating)
    register_ceiling_install_hook(_on_ceiling_installed)


# Importing the facade registers both pairs in order, whichever module loads first.
from kiro_crew import safety_override as _safety_override  # noqa: E402,F401

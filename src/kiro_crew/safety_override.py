"""Time-limited safety override — replaces permanent YOLO mode.

Provides a ``SafetyOverride`` class with two kinds of grant:

- **Ad-hoc** — YOLO toggled mid-session from Slack, the dashboard picker or the
  API. Bounded by ONE duration shared by every surface (``agent.yolo_duration``,
  default 6 h, hard ceiling 24 h) and automatically expires. A 5-minute grace
  window after expiry allows renew() to reactivate without a full
  re-activation flow.
- **Declared** — ``agent.dangerously_skip_permissions: true`` in operator-owned
  config (the camelCase and legacy ``yolo`` spellings are also read). A standing
  instruction, so it does NOT expire: it is re-established and re-audited on
  every startup (state is in-memory), cleared the moment the operator picks
  another approval mode, and deniable by the enterprise governance ceiling via
  the ``yolo_duration`` scope's ``permanent`` member — which downgrades it to the
  ad-hoc duration.

Per-surface TTLs (30 min Slack / 6 h dashboard / 24 h config) were removed: the
same operator re-enabling the same grant got a different lifetime depending on
where they clicked, which was unpredictable without buying any security.

All state changes are logged to the Security Event Log (SEL).
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew.platform.context import (
    register_ceiling_install_hook,
    register_ceiling_invalidate_hook,
)
from kiro_crew.safety_override_breadcrumb import (
    DroppedGrant,
)
from kiro_crew.safety_override_grant import (
    SafetyOverride,
    _on_ceiling_invalidating,
    _push_yolo_policy,
    reset_yolo_policy_state,
    safety_override,
    yolo_policy_permits,
)
from kiro_crew.safety_override_permits import (
    _YOLO_MODE,
    declared_grant_permitted,
    until_shutdown_permitted,
)
from kiro_crew.safety_override_records import (  # noqa: F401  (sel: bound so a mock.patch exit restores it in one write)
    ActivationResult,
    sel,
)

logger = logging.getLogger(__name__)


def describe_dropped_grant(dropped: DroppedGrant) -> str:
    """One channel-neutral line telling the operator what they lost."""
    return (
        f"Auto-approve (YOLO) is OFF: the grant from {dropped.source or 'an earlier session'} "
        f"had {fmt_grant_duration(dropped.remaining_secs)} left when Kiro Crew restarted. "
        "Grants live in memory only, so a restart ends them. Re-enable it if you still want it."
    )


def _on_ceiling_installed(ctx: object) -> None:
    """Re-resolve the YOLO verdict for a newly installed ceiling. The push.

    Registered with ``platform.context`` at import, so it runs on every install of
    the active context.

    ``ctx is None`` is :func:`platform.context.reset_context` -- there is no ceiling
    to derive from, so the flag goes back to unresolved rather than keeping an answer
    that belongs to a ceiling that is gone. The next read bootstraps.
    """
    if ctx is None:
        reset_yolo_policy_state()
        return
    _push_yolo_policy()


# Registered at import so no ceiling install is missed, and deliberately WITHOUT
# resolving here: this module is imported very early by the security and hook layers,
# and reading governance at import time would pull config + the governance stack onto
# that path. ``yolo_policy_permits`` covers the late-registration ordering instead.
register_ceiling_invalidate_hook(_on_ceiling_invalidating)
register_ceiling_install_hook(_on_ceiling_installed)


# ── The status field, derived from the SAME verdict the enforcement path reads ──
#
# ``approval_modes`` governs exactly one mode today: ``yolo``. ``normal`` is the
# interactive floor, and ``trust`` / ``trust_reads`` are non-deniable because their
# live consumption predicates are not gated -- a policy naming any of the three is
# refused at parse time (see the ``SCOPE_CATALOG`` entry).
#
# This helper exists so the dashboard's status field and the per-tool-call
# enforcement predicate cannot disagree. A second TTL cache of the same question
# in ``dashboard/state_parts/status_payload.py`` would drift from this one: one mechanism cannot
# drift from itself.


def cached_disabled_approval_modes() -> list[str]:
    """Modes the policy forbids. Reads the pushed verdict, so no filesystem access.

    Backs ``status_snapshot``, which is emitted on the 5s WS push, and
    ``/api/status``. Presentation only -- enforcement is ``api_chat_mode``, the
    slot-approve gate, and arming in ``safety_override_grant``.
    """
    return [] if yolo_policy_permits() else [_YOLO_MODE]


def resolve_configured_duration() -> tuple[int, bool]:
    """``(ttl_secs, until_shutdown)`` from live config, with the policy clamp.

    Read at every ad-hoc activation, so a duration saved from Settings takes
    effect on the next activation rather than only after a restart.
    ``until_shutdown`` is clamped back to the default TTL when policy forbids it.
    """
    from kiro_crew.config.loader import (
        YOLO_UNTIL_SHUTDOWN,
        KiroCrewConfig,
        yolo_duration_to_secs,
    )

    label = KiroCrewConfig.load().agent.yolo_duration
    if label == YOLO_UNTIL_SHUTDOWN:
        if until_shutdown_permitted():
            return SafetyOverride._ADHOC_TTL_DEFAULT, True
        logger.info(
            "Enterprise policy forbids the until_shutdown auto-approve duration; "
            "using the default timed duration"
        )
        return SafetyOverride._ADHOC_TTL_DEFAULT, False
    return yolo_duration_to_secs(label), False


def install_duration_resolver() -> None:
    """Make ad-hoc activations read their duration from live config.

    Called from every entrypoint that can hand out an ad-hoc grant, so Slack, the
    dashboard and the API all agree — and so a duration change applies without a
    restart. Idempotent.
    """
    safety_override().duration_resolver = resolve_configured_duration


def apply_config_duration() -> int:
    """Seed the ad-hoc duration once and return the TTL (0 for until_shutdown).

    Kept for the startup log and for callers that want the value up front; the
    resolver installed by :func:`install_duration_resolver` is what keeps it
    current afterwards.
    """
    so = safety_override()
    install_duration_resolver()
    try:
        ttl, until_shutdown = resolve_configured_duration()
    except Exception:
        logger.warning("could not read agent.yolo_duration; using the default", exc_info=True)
        so.adhoc_until_shutdown = False
        so.adhoc_ttl = SafetyOverride._ADHOC_TTL_DEFAULT
        return so.adhoc_ttl
    so.adhoc_until_shutdown = until_shutdown
    so.adhoc_ttl = ttl
    return 0 if until_shutdown else ttl


def grant_declared_yolo() -> ActivationResult:
    """Install the operator's declared ``dangerouslySkipPermissions`` grant.

    Permanent when policy permits, otherwise clamped to the ad-hoc duration so
    the admin ceiling wins. Shared by the dashboard and Slack startup paths so a
    headless ``--slack-only`` gateway behaves identically to a full one.
    """
    apply_config_duration()
    so = safety_override()
    if declared_grant_permitted():
        return so.activate_declared()
    logger.info(
        "Enterprise policy forbids a never-expiring auto-approve grant; "
        "the declared grant falls back to the ad-hoc duration"
    )
    return so.activate(SafetyOverride._DECLARED_SOURCE)


# ── User-facing grant-lifetime text (channel-neutral) ──

NO_EXPIRY_TEXT = "stays on until Kiro Crew restarts"


def fmt_grant_duration(secs: int) -> str:
    """Render an ad-hoc TTL for a user-facing message (e.g. "6h", "30min")."""
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    return f"{secs // 60}min"


def describe_grant_lifetime() -> str:
    """Describe the LIVE grant's lifetime truthfully.

    A grant can have no timed expiry at all, in which case ``remaining_secs()``
    is -1. Claiming such a grant "auto-expires" would tell the operator the
    skip-every-approval mode disarms itself when it never does.
    """
    so = safety_override()
    if not so.is_active():
        return "off"
    if so.is_permanent:
        return NO_EXPIRY_TEXT
    return f"{max(0, so.remaining_secs()) // 60}min remaining"


def describe_new_grant(result_ttl: int) -> str:
    """Describe the lifetime of a grant that was just created."""
    if result_ttl <= 0:
        return NO_EXPIRY_TEXT
    return f"auto-expires in {fmt_grant_duration(result_ttl)}"


# This module is the facade of the safety override's owners. It keeps the import-time
# registration of the ceiling hooks, the configured-duration and declared-grant entry
# points, the status field's mode list and the channel-neutral wording of a grant's
# lifetime; every other name is reached through it as ``kiro_crew.safety_override.<name>``:
# a read is served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every module holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. New work goes to the owner of its responsibility:
#
#   ``safety_override_records`` -- the SEL accessor, the revocation sentinel, the result records.
#   ``safety_override_permits`` -- what the governance ceiling permits a YOLO grant.
#   ``safety_override_breadcrumb`` -- the restart-drop record and its writer thread.
#   ``safety_override_grant`` -- ``SafetyOverride``, its singleton and the pushed YOLO verdict.
#
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.safety_override_records",
    "kiro_crew.safety_override_permits",
    "kiro_crew.safety_override_breadcrumb",
    "kiro_crew.safety_override_grant",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.safety_override_breadcrumb import (  # noqa: F401
        _BREADCRUMB_FILE,
        _BREADCRUMB_MAX_BYTES,
        _IMAGE_TOKEN,
        _breadcrumb_idle,
        _breadcrumb_io_lock,
        _breadcrumb_path,
        _breadcrumb_pending,
        _breadcrumb_pending_lock,
        _breadcrumb_pump,
        _breadcrumb_queue,
        _breadcrumb_worker,
        _breadcrumb_worker_lock,
        _clear_breadcrumb,
        _consume_breadcrumb,
        _enqueue_breadcrumb,
        _write_breadcrumb,
        flush_breadcrumb_writes,
        take_dropped_grant,
    )
    from kiro_crew.safety_override_grant import (  # noqa: F401
        _bootstrap_yolo_policy,
        _capped,
        _past_life_cap,
        _revoke_grants_for_policy_deny,
        _singleton,
        _singleton_lock,
        _suspended_trust_restore,
        _yolo_policy_permitted,
        _yolo_policy_permitted_now,
        _yolo_policy_resolved,
        reset_singleton,
    )
    from kiro_crew.safety_override_permits import (  # noqa: F401
        _APPROVAL_MODES_SCOPE,
        _GOVERNANCE_SCOPE,
        _PERMANENT_MEMBER,
        _UNTIL_SHUTDOWN_MEMBER,
        _duration_member_permitted,
        _non_deniable_approval_modes,
        approval_mode_permitted,
        yolo_ttl_cap,
    )
    from kiro_crew.safety_override_records import (  # noqa: F401
        POLICY_REVOKED_SOURCE,
        OverrideStatus,
        RenewResult,
    )

if TYPE_CHECKING:  # names it serves from outside its owners, from their homes
    from kiro_crew.atomic_write import (  # noqa: F401
        atomic_write,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        config_dir,
    )
    from kiro_crew.platform.context import (  # noqa: F401
        current_context,
    )
    from kiro_crew.sel import sel as _get_sel  # noqa: F401

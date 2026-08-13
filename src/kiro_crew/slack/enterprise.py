"""Slack Enterprise Grid workspace validation (default-open).

Optionally restricts the bot to specific Enterprise Grid workspaces when
an operator configures ``slack.allowed_enterprise_ids``.  With no
allowlist configured (the default), all workspaces are accepted — this
is an opt-in restriction, not a hardcoded one.

Two layers of defence:
1. ``validate_enterprise()`` at gateway startup — calls ``auth.test``,
   caches the validated ``team_id``, and (when an allowlist is
   configured) blocks workspaces outside the allowlist.
2. ``check_message_origin()`` on every incoming message — compares the
   event's ``team`` field against the cached value (zero-cost in-memory
   check, no API call).  Catches hot-swap of ``.env`` tokens while the
   gateway is running.  Allows everything when no allowlist is set.
"""

from __future__ import annotations

import logging

from kiro_crew.config.loader import (
    ConfigReadError,
    config_local_path,
    config_path,
    read_config_for_update,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Cached at startup by validate_enterprise().  Checked per-message by
# check_message_origin().  Module-level — safe because the gateway runs
# in a single asyncio event loop.
_validated_team_id: str = ""
_validated_enterprise_id: str = ""

# Set of team_ids accepted by check_message_origin().  Contains the
# validated team_id plus any workspace IDs explicitly listed in
# ``slack.allowed_enterprise_ids`` config — populated once during
# validate_enterprise() so per-message checks remain pure in-memory
# lookups.  See ``_load_allowed_team_ids``.
_allowed_team_ids: set[str] = set()

# True when the operator configured a non-empty
# ``slack.allowed_enterprise_ids`` allowlist.  When False (the default),
# both validate_enterprise() and check_message_origin() are default-open.
_allowlist_configured: bool = False


def _read_allowlist() -> tuple[set[str] | None, str]:
    """Read ``slack.allowed_enterprise_ids`` from config, or refuse the config.

    Returns ``(ids, "")`` on a usable config, or ``(None, reason)`` when the
    config cannot be honoured and the caller must fail CLOSED.

    **One reader.** The same validated read decides BOTH "is this config
    usable" and "what is the allowlist", so the two answers can never
    disagree.  Asking ``KiroCrewConfig.load()`` for the value while probing the
    file separately for health is what let malformed input reopen the
    allowlist: ``load()`` normalizes bad input away at several points and
    returns a defaults-shaped object, which is indistinguishable from "the
    operator configured nothing" -- and "configured nothing" means
    default-open.  Every shape below was a distinct door into that one room.

    A shape is refused when the operator clearly asked for a restriction we
    cannot honour, and accepted when it is genuinely absent:

    * unreadable / non-object file -> refuse (``ConfigReadError``)
    * a symlink whose target is missing -> refuse; the link is a configuration
      artifact, so config was meant to be here and is merely unavailable. This
      is deliberately NOT the same as the absent-file case below: no file at all
      means none was ever written, which is a fresh install.
    * absent file -> skip; an absent config is genuinely unconfigured
    * ``slack`` present but not an object -> refuse (``load()`` would coerce it
      to ``{}`` and drop the allowlist)
    * ``allowed_enterprise_ids`` absent -> skip, nothing configured here
    * present but not a list -> refuse (``load()`` iterates it, so a bare
      string yields per-character entries)
    * present and non-empty, but NO entry survives validation -> refuse; the
      operator asked for a restriction and none of it is usable, and silently
      collapsing to empty would mean default-open

    Mixed valid/invalid entries keep the valid ones, matching the loader: that
    narrows the allowlist rather than widening it, so it is not a widening
    door and dropping the operator's working ids would be a regression.

    ``config.local.json`` REPLACES the base list when it carries the key, which
    is what ``_deep_merge`` does to a list value -- so the overlay is applied
    last here too.
    """
    ids: set[str] = set()
    for path in (config_path(), config_local_path()):
        # A symlink whose target is GONE is not an absent config. The link is
        # itself a configuration artifact, so the operator meant config to live
        # here and it is currently unavailable -- which must not be read as
        # permission. ``exists()`` follows the link, so this is true ONLY when
        # the target is missing; an intact symlink pointing at a file that
        # happens to hold ``{}`` still reads as genuinely unconfigured below.
        if path.is_symlink() and not path.exists():
            return None, f"{path.name}: symlink target is missing"
        try:
            raw = read_config_for_update(path)
        except ConfigReadError as e:
            return None, str(e)
        if not raw:
            continue
        if "slack" not in raw:
            continue
        slack = raw["slack"]
        if not isinstance(slack, dict):
            return None, f"{path.name}: 'slack' is not a JSON object"
        if "allowed_enterprise_ids" not in slack:
            continue
        entries = slack["allowed_enterprise_ids"]
        if not isinstance(entries, list):
            return None, (
                f"{path.name}: slack.allowed_enterprise_ids is "
                f"{type(entries).__name__}, expected a list"
            )
        # Same per-entry filter the loader applies: Slack enterprise/team ids.
        usable = {
            e for e in entries
            if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
        }
        if entries and not usable:
            return None, (
                f"{path.name}: slack.allowed_enterprise_ids has "
                f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} but "
                f"none is a usable Slack id"
            )
        ids = usable
    return ids, ""


def _load_allowed_team_ids() -> bool:
    """Populate ``_allowed_team_ids`` from validated state + config.

    Called by ``validate_enterprise()`` after the validated team_id has
    been cached.  The result includes:
      - the validated team_id (from ``auth.test``)
      - every entry in ``slack.allowed_enterprise_ids`` config

    On Enterprise Grid, ``auth.test`` returns the org-level enterprise ID
    while per-message events carry child workspace team_ids.  Operators
    add child workspace IDs to ``slack.allowed_enterprise_ids`` to allow
    those events through ``check_message_origin``.

    Sets ``_allowlist_configured`` based on whether the operator supplied
    any ``slack.allowed_enterprise_ids`` entries.  When none are
    configured the module stays default-open.

    Fail-closed on a degraded read: the allowlist value and the "is this config
    usable" judgement both come from :func:`_read_allowlist`, which documents
    that invariant and why it matters. When that read refuses the config, this
    function keeps ``_allowlist_configured`` True and admits NOTHING, and
    SEL-audits it, rather than silently widening the allowlist. Not even the
    validated team_id: which authenticated workspace is allowed is the very
    question the unreadable allowlist would have answered.

    Returns True when the read was DEGRADED (refused). The caller must honour
    that: no other source of ids -- including ones the caller derived from its
    own ``KiroCrewConfig.load()`` -- may widen the allowlist afterwards, or the
    refusal made here is silently undone one level up.
    """
    global _allowed_team_ids, _allowlist_configured
    allowed: set[str] = set()
    if _validated_team_id:
        allowed.add(_validated_team_id)

    configured: set[str] | None
    try:
        configured, refusal = _read_allowlist()
    except Exception:
        configured, refusal = None, "unexpected error reading config"
        logger.exception(
            "Failed to read slack.allowed_enterprise_ids; failing closed "
            "with no origin admitted"
        )

    if configured is None:
        # The config cannot be honoured: unreadable, or a shape whose meaning
        # we cannot determine. An empty allowlist is indistinguishable from
        # "operator configured none", and "configured none" means default-open,
        # so guessing here is guessing in the WIDENING direction. Fail CLOSED:
        # keep the allowlist "configured" and admit NOTHING, and SEL-audit it.
        #
        # Deliberately NOT the validated team_id. Deciding WHICH authenticated
        # workspace is allowed is this allowlist's entire job, so answering
        # "whichever one just authenticated" is circular -- it trusts exactly
        # the thing being checked, and it reports success while doing so. On a
        # non-Grid workspace it is also permissive: the candidate checked below
        # is the bare team_id, which would be the one id admitted here, so a bot
        # token pointing at a FOREIGN workspace would validate against itself
        # and defeat the operator's restriction. The token lives in .env / the
        # environment while the allowlist lives in config.json, so those are
        # separate write surfaces -- an env-only token swap needs no file edit,
        # and the unreadable config can be an independent accident.
        #
        # Admitting nothing makes the caller's own candidate check refuse
        # startup, and makes check_message_origin() deny every origin.
        _allowlist_configured = True
        allowed = set()
        logger.error(
            "slack.allowed_enterprise_ids could not be read (%s); "
            "failing closed with no origin admitted",
            refusal,
        )
        sel().log_api_access(
            caller="gateway",
            operation="slack.allowed_team_ids_load",
            outcome="denied",
            source="startup",
            error="config_load_degraded_fail_closed",
        )
        _allowed_team_ids = allowed
        return True
    elif configured:
        # Every config file read cleanly and the operator configured an
        # allowlist.
        _allowlist_configured = True
        allowed.update(configured)
    else:
        # Genuinely unconfigured: no config file, or a clean file with no
        # allowlist entries.  Stay default-open exactly as before.
        _allowlist_configured = False

    _allowed_team_ids = allowed
    return False


def _governance_posture_permits_workspace(enterprise_id: str, team_id: str) -> bool:
    """Check the workspace against ``channels.posture.slack.allowed_enterprise_ids``.

    The governance ``channels`` ScopedMap may carry a policy-only ``posture`` for
    the ``slack`` member pinning ``allowed_enterprise_ids`` (and/or
    ``allowed_team_ids``) — an enterprise ceiling the agent cannot edit. We query
    it via ``governance_permits("channels", "slack/<leaf>:<value>")`` for each
    candidate id. Default-open (True) when no policy / no posture governs it, so a
    standalone host is unaffected. Fail-closed (deny) on ANY error — a
    PlatformCompositionError (a host that could not compose its companion) OR any
    other unexpected error → deny; a governance error must not
    silently permit a workspace the operator's posture would restrict.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # An empty session key resolves policy-only — exactly the ceiling we want:
        # the posture is policy-only (Rule 6 rejects a profile carrying it), so a
        # surface-bound profile must NOT additionally intersect here.  (The degrade
        # audit below uses the _host surface only for honest SEL attribution.)
        for leaf, value in (("allowed_enterprise_ids", enterprise_id), ("allowed_team_ids", team_id)):
            if not value:
                # An EMPTY id (Slack returns enterprise_id="" for every
                # non-Enterprise-Grid workspace, the common case) cannot satisfy
                # an explicitly-pinned allowlist, so it must fail CLOSED when the
                # leaf is pinned — otherwise an operator's un-weakenable
                # allowed_enterprise_ids ceiling is silently bypassed.  Probe the
                # posture with a sentinel that no real id can equal: if the leaf
                # is an allow-mode allowlist the sentinel is DENIED (pinned →
                # close); if the leaf is ungoverned / deny-mode / allow-any the
                # sentinel PERMITS (not pinned → the empty id is fine, skip).
                probe = governance_permits("channels", f"slack/{leaf}:\x00__unpinned_probe__")
                if not getattr(probe, "permitted", True):
                    return False
                continue
            decision = governance_permits("channels", f"slack/{leaf}:{value}")
            if not getattr(decision, "permitted", True):
                return False
        return True
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED: a governance evaluation error must DENY the
        # workspace, not silently permit it.  session
        # key=_host so the degrade SEL records the honest "host" surface (this
        # in-process admission check is not driven by a Slack session).
        try:
            from kiro_crew.platform.governance_profiles import (
                HOST_SESSION_KEY,
                audit_governance_degraded,
            )

            audit_governance_degraded(
                "slack_enterprise_posture",
                session_key=HOST_SESSION_KEY,
                scope="channels.posture",
                failed_closed=True,
            )
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return False


def validate_enterprise(
    bot_token: str,
    *,
    extra_ids: set[str] | None = None,
) -> bool:
    """Validate the configured workspace (default-open).

    Calls ``auth.test`` to cache ``team_id`` and ``enterprise_id`` so
    ``check_message_origin()`` can verify each incoming message without
    an API call.

    Default-open: returns True for any workspace unless the operator
    configured an allowlist via ``slack.allowed_enterprise_ids``, in which
    case the workspace's enterprise_id must appear in that allowlist.  Logs
    the result to SEL for audit.

    ``extra_ids`` does NOT contribute to the admitted set, on any path. Callers
    pass their own ``KiroCrewConfig.load()`` snapshot of
    ``slack.allowed_enterprise_ids`` -- the same key :func:`_read_allowlist`
    reads here, taken earlier, so this read is never older and the snapshot can
    only differ by holding ids the operator has since REMOVED. Honouring it
    would either re-admit those ids or, on the ``auth.test``-failure path,
    manufacture a restriction the file says does not exist. So the validated
    read is the sole source of the allowlist and the snapshot is ignored
    (logged when it disagrees). The parameter is kept because it is part of the
    :class:`~kiro_crew.platform.interfaces.SlackEnterpriseGate` protocol, which
    another edition implements against a different allowlist source.
    """
    global _validated_team_id, _validated_enterprise_id, _allowed_team_ids
    global _allowlist_configured

    # Clear stale state before re-validating.
    _validated_team_id = ""
    _validated_enterprise_id = ""
    _allowed_team_ids = set()
    _allowlist_configured = False

    extra = extra_ids or set()

    try:
        from slack_sdk.web import WebClient

        client = WebClient(token=bot_token)
        resp = client.auth_test()
    except Exception:
        # auth.test failed (missing slack_sdk or API error): the workspace
        # identity cannot be verified.  Whether we fail open or closed
        # depends on whether an allowlist is configured.
        #
        # An allowlist is configured if extra_ids was passed OR the
        # operator set slack.allowed_enterprise_ids in config.  Reading
        # config here cannot rely on auth.test having succeeded, so check
        # it directly -- through the SAME validated reader
        # ``_load_allowed_team_ids`` uses, so the two call sites cannot
        # disagree about whether a restriction exists.
        #
        # A config we cannot read counts as "a restriction may be in force".
        # Swallowing the error and leaving ``configured`` empty would make an
        # unreadable config indistinguishable from "no allowlist", and that
        # branch ACCEPTS an unverifiable workspace -- the same silent widening
        # this module exists to prevent, reached from the auth.test-failure
        # path instead of the startup path.
        # Guarded exactly like ``_load_allowed_team_ids``' call: an unexpected
        # exception from the reader (not just ConfigReadError -- e.g. a
        # RecursionError from pathologically nested JSON) must degrade to
        # "config unreadable" and fail CLOSED, not escape into
        # ``init_socket_mode()`` and take the gateway down. The pre-fix code
        # here wrapped its own read in ``except Exception``; keeping that
        # blanket guard at one call site and not the other would be an
        # asymmetry, and this branch is the one reached while Slack is already
        # failing.
        try:
            ids, refusal = _read_allowlist()
        except Exception:
            ids, refusal = None, "unexpected error reading config"
            logger.exception(
                "Failed to read slack.allowed_enterprise_ids while handling "
                "an auth.test failure; failing closed"
            )
        config_unreadable = ids is None
        if config_unreadable:
            logger.error(
                "slack.allowed_enterprise_ids could not be read (%s) while "
                "handling an auth.test failure; failing closed",
                refusal,
            )
        # ``extra`` is deliberately NOT unioned in. It is the caller's earlier
        # snapshot of this same key, so an id it holds that the read did not
        # return is one the operator REMOVED -- and using it here would
        # manufacture a restriction the file says does not exist, refusing
        # startup on a workspace nobody restricted. The file decides, including
        # when it decides to list nothing. An unreadable file is still refused
        # below via ``config_unreadable``: that is a config we cannot honour,
        # which is not the same as one that honestly lists no restriction.
        allowlist = ids or set()

        if allowlist or config_unreadable:
            # FAIL CLOSED: an operator restriction is in force but the
            # workspace identity could not be verified.  Accepting an
            # unverifiable workspace against an explicit allowlist would
            # silently bypass the restriction.  check_message_origin()
            # also denies because no validated team_id was cached.
            _allowlist_configured = True
            _allowed_team_ids = set(allowlist)
            logger.error(
                "Enterprise validation FAILED: auth.test unavailable and an "
                "allowlist is configured; cannot verify workspace identity."
            )
            sel().log_api_access(
                caller="gateway",
                operation="slack.enterprise_validation",
                outcome="denied",
                source="startup",
                error="auth_test_unavailable_with_allowlist",
            )
            return False

        # Default-open: no allowlist configured, so a missing slack_sdk or
        # auth.test failure must not block startup.  Without cached state,
        # check_message_origin() stays default-open too.
        logger.warning(
            "Enterprise validation: auth.test unavailable; "
            "continuing default-open"
        )
        sel().log_api_access(
            caller="gateway",
            operation="slack.enterprise_validation",
            outcome="allowed",
            source="startup",
            error="auth_test_unavailable",
        )
        return True

    enterprise_id = resp.get("enterprise_id", "")
    team_id = resp.get("team_id", "")
    team = resp.get("team", "")
    url = resp.get("url", "")

    # Cache for per-message checks (populates _allowlist_configured from
    # slack.allowed_enterprise_ids config).
    _validated_team_id = team_id
    _validated_enterprise_id = enterprise_id
    degraded = _load_allowed_team_ids()
    if extra and degraded:
        # The config read REFUSED, and ``extra`` is the caller's own
        # ``KiroCrewConfig.load()``-derived value -- which degrades a torn
        # overlay by DROPPING it, yielding the pre-overlay BASE list. Unioning
        # it here would re-admit exactly the origins the operator removed in
        # that overlay, undoing the refusal `_load_allowed_team_ids` just made:
        # the same two-reader widening, with the CALLER as the second reader.
        # The allowlist stays configured and admits nothing.
        logger.error(
            "slack.allowed_enterprise_ids could not be read; ignoring %d "
            "caller-supplied id(s) rather than widening a degraded allowlist",
            len(extra),
        )
        sel().log_api_access(
            caller="gateway",
            operation="slack.enterprise_validation",
            outcome="denied",
            source="startup",
            error="extra_ids_ignored_on_degraded_config",
        )
    elif extra:
        # The read SUCCEEDED, so it is authoritative and ``extra`` cannot add
        # anything legitimate to it: ``extra`` is the caller's EARLIER
        # ``KiroCrewConfig.load()`` snapshot of the same
        # ``slack.allowed_enterprise_ids`` key, and this read is never older.
        # So an id in ``extra`` that the validated read did not return is an id
        # REMOVED from the file since the snapshot was taken, and unioning it
        # would undo that removal -- the two-reader widening again, with the
        # CALLER as the second reader and a stale snapshot as the wider read.
        # The file decides, including when it decides to list nothing.
        # Logged, not SEL-audited: ignoring the snapshot grants and denies
        # nothing by itself, and the access decision is audited below once the
        # candidate has actually been checked. Auditing here would have to name
        # an outcome before one exists.
        stale = extra - _allowed_team_ids
        if stale:
            logger.warning(
                "Ignoring %d caller-supplied slack.allowed_enterprise_ids "
                "value(s) that the validated read did not return: the "
                "allowlist was narrowed after the caller's config snapshot",
                len(stale),
            )

    # Default-open unless the operator configured an allowlist.
    if _allowlist_configured:
        candidate = enterprise_id or team_id
        if candidate not in _allowed_team_ids:
            logger.error(
                "Enterprise validation FAILED: enterprise_id=%s (team=%s) "
                "is not in slack.allowed_enterprise_ids.",
                enterprise_id,
                team,
            )
            sel().log_api_access(
                caller="gateway",
                operation="slack.enterprise_validation",
                outcome="denied",
                source="startup",
                resources=f"enterprise_id={enterprise_id} team={team} url={url}",
                error="enterprise_id_not_allowed",
            )
            return False

    # Governance posture (un-weakenable): the enterprise security policy may pin
    # ``channels.posture.slack.allowed_enterprise_ids`` — an enterprise ceiling the
    # AGENT cannot edit (config.json's slack.allowed_enterprise_ids is operator-
    # editable; the posture is the policy-level, agent-unweakenable equivalent).
    # This composes as an ADDITIONAL ceiling: the workspace must satisfy the
    # governance posture too. Default-open when no posture is configured.
    if not _governance_posture_permits_workspace(enterprise_id, team_id):
        logger.error(
            "Enterprise validation FAILED: enterprise_id=%s (team=%s) is not "
            "permitted by the governance channels.posture allowlist.",
            enterprise_id,
            team,
        )
        sel().log_api_access(
            caller="gateway",
            operation="slack.enterprise_validation",
            outcome="denied",
            source="startup",
            resources=f"enterprise_id={enterprise_id} team={team} url={url}",
            error="enterprise_id_not_allowed_by_governance",
        )
        return False

    logger.info(
        "Enterprise validation OK: enterprise_id=%s team=%s team_id=%s",
        enterprise_id,
        team,
        team_id,
    )
    sel().log_api_access(
        caller="gateway",
        operation="slack.enterprise_validation",
        outcome="allowed",
        source="startup",
        resources=f"enterprise_id={enterprise_id} team={team} team_id={team_id}",
    )
    return True


def check_message_origin(event_team_id: str) -> bool:
    """Verify an incoming message's team_id is allowed (default-open).

    Zero-cost in-memory comparison — no API call, no config load.  The
    allowed set is populated once during ``validate_enterprise()``;
    re-validate to refresh.

    Default-open: returns True for any message unless the operator
    configured an allowlist via ``slack.allowed_enterprise_ids``, in
    which case the event's team_id must appear in that allowlist.

    Every permission decision (accept, deny) is audited via SEL per the
    ``security-controls`` guideline.

    Enterprise Grid: ``auth.test`` returns the org-level enterprise ID
    as ``team_id`` while per-message events carry child workspace
    team_ids.  Operators add child workspace IDs to
    ``slack.allowed_enterprise_ids`` config so legitimate events from
    those workspaces are accepted via the same allowed-set lookup.
    """
    if not _allowlist_configured:
        # No operator allowlist — accept all message origins.
        sel().log_api_access(
            caller="gateway",
            operation="slack.message_origin_check",
            outcome="allowed",
            source="message",
            resources=f"team_id={event_team_id}",
            error="no_allowlist_configured",
        )
        return True
    if not event_team_id:
        sel().log_api_access(
            caller="gateway",
            operation="slack.message_origin_check",
            outcome="denied",
            source="message",
            error="empty_team_id",
        )
        return False
    if event_team_id in _allowed_team_ids:
        sel().log_api_access(
            caller="gateway",
            operation="slack.message_origin_check",
            outcome="allowed",
            source="message",
            resources=f"team_id={event_team_id}",
        )
        return True
    sel().log_api_access(
        caller="gateway",
        operation="slack.message_origin_check",
        outcome="denied",
        source="message",
        resources=f"team_id={event_team_id}",
        error="not_in_allowlist",
    )
    return False

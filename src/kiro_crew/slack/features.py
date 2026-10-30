"""Slack Code: whether each Slack Code switch runs.

The switches are the ones registered in :data:`kiro_crew.slack_manifest.FEATURE_SCOPES`
(today ``agent_sessions``); everything here is keyed by that name, so registering
a switch there is all this module needs. Two layers decide whether one runs, and
both are read here so every caller answers "is it on?" the same way:

* the **toggle** -- ``slack.<feature>`` in ``config.json`` (Settings → Slack). It
  also decides what the generated app manifest asks Slack for
  (:func:`manifest_features`). Anything that is not a real ``True`` is off. Read
  live, so a flip applies on the next event.
* the **capability** -- whether the installed Slack app can actually use the
  feature: its granted bot scopes (``auth.test``'s ``x-oauth-scopes``, read when
  the Slack socket connects and on every reconnect) include the feature's
  registered scope, and Slack has not answered a call for it with
  ``feature_disabled``, ``missing_scope`` or ``not_allowed_token_type``. Such an
  answer LATCHES the feature off for the process (until the next reconnect
  re-probes): one dashboard notification, one log line, and the runtime falls
  back silently to today's Slack behaviour.

:func:`enabled` is toggle AND capability -- the runtimes call it (through
:func:`agent_sessions_enabled`). :func:`available` is the capability alone, for
the settings page. The config readers are pure; the capability record is the
one piece of process state, guarded by a lock.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, Mapping

from kiro_crew import slack_manifest

logger = logging.getLogger(__name__)

#: Feature name, the same string as ``slack_manifest.AGENT_SESSIONS``.
AGENT_SESSIONS = slack_manifest.AGENT_SESSIONS

#: The bot scope each registered feature cannot run without: the manifest's own
#: registry, so a switch registered there is known here with no second list.
REQUIRED_SCOPE: Mapping[str, str] = slack_manifest.FEATURE_SCOPES

#: Slack error codes that mean "this install cannot use the feature": they latch
#: it off rather than being retried on every message.
LATCH_ERRORS = frozenset({"feature_disabled", "missing_scope", "not_allowed_token_type"})

#: ``availability()`` reasons beyond the latch codes.
REASON_SCOPES_UNKNOWN = "scopes_unknown"


def _slack_section(cfg: Any) -> Any:
    """``cfg.slack`` for a full config, or *cfg* itself for a ``SlackConfig``."""
    return getattr(cfg, "slack", cfg)


def toggle(cfg: Any, feature: str) -> bool:
    """A registered switch's toggle (``slack.<feature>``), capability aside.

    An unregistered name, a config without the field, and anything but a real
    ``True`` all read as off.
    """
    if feature not in REQUIRED_SCOPE:
        return False
    return getattr(_slack_section(cfg), feature, False) is True


def enabled(cfg: Any, feature: str) -> bool:
    """A registered switch runs: its toggle is on AND the installed app can."""
    return toggle(cfg, feature) and available(feature)


def agent_sessions_enabled(cfg: Any) -> bool:
    """Run Slack threads as agent sessions: the toggle is on AND the app can."""
    return enabled(cfg, AGENT_SESSIONS)


# ── Capability: what the installed Slack app can actually use ──


class _Capabilities:
    """Granted bot scopes and latched refusals for this process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        #: ``None`` until the connect probe has read them (or when Slack did not say).
        self.granted: frozenset[str] | None = None
        #: feature -> the Slack error that latched it off.
        self.latched: dict[str, str] = {}
        #: features already notified about this process (one notification each).
        self.notified: set[str] = set()
        self.notifier: Callable[[str, str], None] | None = None


_CAPS = _Capabilities()


def set_capability_notifier(notifier: Callable[[str, str], None] | None) -> None:
    """Install ``notifier(feature, reason)``, called once per feature per process
    when it latches off (the gateway wires the dashboard notification here)."""
    with _CAPS.lock:
        _CAPS.notifier = notifier


def record_granted_scopes(scopes: Iterable[str] | None) -> None:
    """Record the bot scopes the install holds (``None`` = Slack did not say).

    A ``missing_scope`` latch whose scope is now granted is lifted: the user
    reinstalled with the updated manifest, so the feature may run again.
    """
    granted = frozenset(scopes) if scopes is not None else None
    with _CAPS.lock:
        _CAPS.granted = granted
        if granted is not None:
            for feature, error in list(_CAPS.latched.items()):
                if error == "missing_scope" and REQUIRED_SCOPE.get(feature, "") in granted:
                    del _CAPS.latched[feature]


def granted_scopes() -> frozenset[str] | None:
    """The bot scopes last recorded, or ``None`` when unknown."""
    with _CAPS.lock:
        return _CAPS.granted


def latch_unavailable(feature: str, error: str) -> bool:
    """Latch *feature* off after Slack answered a call for it with *error*.

    Only the codes in :data:`LATCH_ERRORS` latch; anything else (a transient
    failure, a bad argument) returns ``False`` and changes nothing. The first
    latch of a feature in this process logs one line and notifies once.
    """
    if feature not in REQUIRED_SCOPE or error not in LATCH_ERRORS:
        return False
    with _CAPS.lock:
        first = feature not in _CAPS.latched
        _CAPS.latched[feature] = error
        notify = first and feature not in _CAPS.notified
        if notify:
            _CAPS.notified.add(feature)
        notifier = _CAPS.notifier
    if first:
        logger.warning(
            "Slack %s unavailable (%s): falling back to plain Slack threads", feature, error
        )
    if notify and notifier is not None:
        try:
            notifier(feature, error)
        except Exception:
            logger.debug("Slack capability notification failed", exc_info=True)
    return True


def clear_latch(feature: str | None = None) -> None:
    """Lift one feature's latch, or every latch (a reconnect re-probes from scratch)."""
    with _CAPS.lock:
        if feature is None:
            _CAPS.latched.clear()
        else:
            _CAPS.latched.pop(feature, None)


def latched() -> dict[str, str]:
    """Feature -> the Slack error that latched it off, this process."""
    with _CAPS.lock:
        return dict(_CAPS.latched)


def reset_capabilities() -> None:
    """Forget everything (tests; a gateway that drops its Slack connection)."""
    with _CAPS.lock:
        _CAPS.granted = None
        _CAPS.latched.clear()
        _CAPS.notified.clear()


def availability(feature: str) -> tuple[bool, str]:
    """``(available, reason)`` for *feature*; reason is '' when available.

    Reasons: a latch code (``feature_disabled`` …), ``missing_scope`` when the
    granted scopes lack the feature's scope, ``scopes_unknown`` before the
    connect probe has read them.
    """
    with _CAPS.lock:
        error = _CAPS.latched.get(feature)
        granted = _CAPS.granted
    if error:
        return False, error
    if granted is None:
        return False, REASON_SCOPES_UNKNOWN
    if REQUIRED_SCOPE.get(feature, "\0") not in granted:
        return False, "missing_scope"
    return True, ""


def available(feature: str) -> bool:
    """The installed app can use *feature* (scope granted, not latched)."""
    return availability(feature)[0]


def manifest_features(cfg: Any) -> frozenset[str]:
    """The Slack Code switches *cfg* turns on, as ``slack_manifest`` feature names.

    The one bridge from config to the toggle-aware manifest: every emitter
    (``kirocrew manifest``, ``GET /api/slack/manifest``, the settings page's
    update panel) passes this to ``slack_manifest.render``.
    """
    # The TOGGLE, never the capability: the manifest is how an install gains the
    # capability, so it must ask for the scopes before they are granted.
    return frozenset(f for f in slack_manifest.FEATURES if toggle(cfg, f))

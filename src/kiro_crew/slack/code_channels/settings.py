"""Whether code channels run, and what a new one starts with.

The gate is :func:`kiro_crew.slack.features.code_channels_enabled`: the user's
``slack.code_channels`` setting is on AND the install can run code channels --
the bot was granted ``code_channels:manage`` and Slack has not answered
``feature_disabled`` / ``missing_scope`` / ``not_allowed_token_type``. The first
such answer latches the feature off for the rest of the process (one dashboard
notification, one log line, nothing posted in Slack); a reconnect re-probes.
While the gate is shut every seam in this package is a pass-through, which keeps
the gateway exactly as it is without code channels.

The setting, the scope probe, the latch and its notification are owned by
:mod:`kiro_crew.slack.features` (the Slack Code settings work). This module is
the ONE place this package reads them, and the one place it reports a latching
answer it received (:func:`report_slack_refusal`). Where that module does not
export the capability half yet, a local stand-in answers with the same meaning:
unavailable until granted scopes are noted, latched by the first refusal.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels.models import LATCH_ERRORS

logger = logging.getLogger(__name__)

#: The bot scope ``agents.conversations.*`` needs.
REQUIRED_SCOPE = "code_channels:manage"


@dataclass(frozen=True)
class CodeChannelDefaults:
    """What a new code channel's dashboard session starts with ("" = not set).

    ``folder`` is a folder id or a ``Parent/Child`` path, as the settings page
    stores it.
    """

    folder: str = ""
    agent: str = ""
    model: str = ""
    reasoning_effort: str = ""


def _features() -> Any | None:
    try:
        from kiro_crew.slack import features

        return features
    except Exception:
        return None


# ── Local stand-in for the capability probe and its latch ─────────────────

_lock = threading.Lock()
_granted_scopes: frozenset[str] | None = None
_latched_code: str = ""


def note_granted_scopes(scopes: Any) -> None:
    """Stand-in for the connect-time scope read (auth.test ``x-oauth-scopes``).

    A reconnect calls it again, which clears the latch: the app may have been
    reinstalled with the scope since.
    """
    global _granted_scopes, _latched_code
    if isinstance(scopes, str):
        scopes = scopes.split(",")
    with _lock:
        _granted_scopes = frozenset(s.strip() for s in scopes or () if s and s.strip())
        _latched_code = ""


def reset_capability() -> None:
    """Forget noted scopes and the latch (tests; a disconnect)."""
    global _granted_scopes, _latched_code
    with _lock:
        _granted_scopes = None
        _latched_code = ""


def _stand_in_available() -> bool:
    with _lock:
        return (
            not _latched_code and _granted_scopes is not None and REQUIRED_SCOPE in _granted_scopes
        )


def code_channels_on(cfg: Any | None) -> bool:
    """The gate: the setting is on AND the install can run code channels.

    Anything unreadable is OFF, so a broken config or a failing probe can never
    switch the feature on.
    """
    if cfg is None:
        return False
    features = _features()
    enabled = getattr(features, "code_channels_enabled", None)
    try:
        on = (
            enabled(cfg) is True
            if callable(enabled)
            else (getattr(getattr(cfg, "slack", None), "code_channels", False) is True)
        )
    except Exception:
        logger.debug("code_channels_enabled failed; treating as off", exc_info=True)
        return False
    if not on:
        return False
    if callable(getattr(features, "code_channels_available", None)):
        # code_channels_enabled already folds the capability in.
        return True
    return _stand_in_available()


def report_slack_refusal(code: str, *, method: str = "") -> None:
    """Hand a Slack answer to the latch; only the latching codes do anything."""
    global _latched_code
    if code not in LATCH_ERRORS:
        return
    report = getattr(_features(), "report_code_channels_refusal", None)
    if callable(report):
        try:
            report(code, method=method)
        except Exception:
            logger.debug("code channel refusal report failed", exc_info=True)
        return
    with _lock:
        if _latched_code:
            return
        _latched_code = code
    logger.warning(
        "Slack answered %s to %s: code channels are off until Slack reconnects",
        code,
        method or "a code-channel call",
    )


# ── Config and defaults ───────────────────────────────────────────────────


def live_config(orch: Any | None = None) -> Any | None:
    """The gateway's live config (reload-aware), or a fresh load as a fallback."""
    try:
        from kiro_crew.slack.handler import slack_cfg

        return slack_cfg(orch)
    except Exception:
        logger.debug("code channels: live config unavailable", exc_info=True)
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def code_channel_defaults(cfg: Any | None) -> CodeChannelDefaults:
    """``slack.code_channel_defaults`` (all empty when unset or unreadable)."""
    if cfg is None:
        return CodeChannelDefaults()
    raw: Any = None
    fn = getattr(_features(), "code_channel_defaults", None)
    if callable(fn):
        try:
            raw = fn(cfg)
        except Exception:
            logger.debug("code_channel_defaults failed", exc_info=True)
    if raw is None:
        raw = getattr(getattr(cfg, "slack", None), "code_channel_defaults", None)

    def _get(name: str) -> str:
        if isinstance(raw, dict):
            return _text(raw.get(name))
        return _text(getattr(raw, name, ""))

    return CodeChannelDefaults(
        folder=_get("folder"),
        agent=_get("agent"),
        model=_get("model"),
        reasoning_effort=_get("reasoning_effort"),
    )


def find_folder(folders: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """The folder *ref* names: by id, else by unique ``Parent/Child`` path."""
    if not ref:
        return None
    fn = getattr(_features(), "find_folder", None)
    if callable(fn):
        found = fn(folders, ref)
        return dict(found) if found is not None else None
    for folder in folders:
        if str(folder.get("id") or "") == ref:
            return folder
    return None

"""Slack Code settings: the registered switches and the install's drift report.

The read model, the validation and the manifest-drift report behind Settings →
Slack's "Slack Code" card. Kept out of :mod:`slack_settings` (whose functions
are composed onto the messaging facade's globals) so its helpers resolve their
own imports; ``slack_settings`` calls in here.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping

from kiro_crew import slack_manifest
from kiro_crew.slack import features as slack_features
from kiro_crew.slack import scope_probe

# The switches the PUT accepts and the GET reports are every switch registered
# in ``slack_manifest.FEATURE_SCOPES`` (read at call time, as
# ``slack_manifest.FEATURES``), each under its own ``slack.<name>`` key, so
# registering one there needs nothing here.

#: How long a manifest-drift probe answer is reused (seconds). The page polls on
#: open and on Refresh; Slack's auth.test is tier 4, but there is no reason to
#: call it on every render.
_STATUS_TTL = 60.0
_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class CodeSettingsError(ValueError):
    """A refused Slack Code settings field; the message is safe to show."""


def code_settings_view(cfg: Any) -> dict[str, Any]:
    """The GET body fragment: each switch as saved and whether the install can run it."""

    def _capability(feature: str) -> dict[str, object]:
        ok, reason = slack_features.availability(feature)
        return {
            "available": ok,
            "reason": reason,
            "scope": slack_features.REQUIRED_SCOPE[feature],
        }

    # Per registered switch: the toggle as saved (``<name>``) and whether the
    # install can run it (``<name>_capability``); toggle AND available is what
    # the runtime checks.
    switches: dict[str, Any] = {}
    for feature in slack_manifest.FEATURES:
        switches[feature] = slack_features.toggle(cfg, feature)
        switches[f"{feature}_capability"] = _capability(feature)
    return switches


def validate_code_settings(
    body: Mapping[str, Any], stored: Mapping[str, Any]
) -> tuple[dict[str, object], list[str]]:
    """Validate the Slack Code switches of a PUT body against the stored section.

    Returns ``(staged, applied)``: the ``slack.*`` keys to write and their names,
    staging only real changes. Raises :class:`CodeSettingsError` on the first
    refusal; nothing is staged in that case.
    """
    staged: dict[str, object] = {}
    applied: list[str] = []
    for key in slack_manifest.FEATURES:
        if key not in body:
            continue
        val = body.get(key)
        if not isinstance(val, bool):
            raise CodeSettingsError(f"{key} must be a boolean")
        if val != (stored.get(key) is True):
            staged[key] = val
            applied.append(key)
    return staged, applied


def feature_changes() -> dict[str, dict[str, list[str]]]:
    """Per switch, what turning it on adds to a manifest that has nothing else on."""
    base = slack_manifest.requirements(frozenset())
    return {
        f: slack_manifest.diff_requirements(
            base, slack_manifest.requirements(frozenset({f}))
        ).as_dict()
        for f in slack_manifest.FEATURES
    }


def _known_scopes() -> frozenset[str]:
    every = frozenset(slack_manifest.FEATURES)
    return slack_manifest.requirements(every).bot_scopes


async def manifest_status(cfg: Any, bot_token: str, *, refresh: bool = False) -> dict[str, Any]:
    """Compare the install behind *bot_token* with the manifest *cfg*'s switches need.

    The token is used only for the Slack calls and a cache key digest; it is never
    returned, logged or stored.
    """
    enabled = slack_features.manifest_features(cfg)
    required = slack_manifest.requirements(enabled)
    body: dict[str, Any] = {
        "features": [f for f in slack_manifest.FEATURES if f in enabled],
        "required_scopes": sorted(required.bot_scopes),
        "required_events": sorted(required.bot_events),
        "required_features": sorted(required.features),
        "feature_changes": feature_changes(),
    }
    if not bot_token:
        body["install"] = scope_probe.ManifestStatus(scope_probe.STATUS_NOT_CONFIGURED).as_dict()
        body["app_manifest_url"] = None
        return body

    key = hashlib.sha256((bot_token + "\0" + ",".join(sorted(enabled))).encode("utf-8")).hexdigest()
    now = time.monotonic()
    cached = _status_cache.get(key)
    if cached is not None and not refresh and now - cached[0] < _STATUS_TTL:
        install = cached[1]
    else:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=bot_token, timeout=10)
        if refresh:
            # Check again re-probes like a reconnect: a workspace an admin has
            # since enabled must read as such, so a runtime "feature_disabled"
            # refusal is lifted here. If Slack still refuses, the next call
            # latches it again, with no second notification.
            for feature, error in slack_features.latched().items():
                if error == "feature_disabled":
                    slack_features.clear_latch(feature)
        status = await scope_probe.probe_manifest_status(
            client, required.bot_scopes, known_scopes=_known_scopes()
        )
        install = status.as_dict()
        _status_cache.clear()  # one install per gateway: keep only the newest answer
        _status_cache[key] = (now, install)
    body["install"] = install
    body["app_manifest_url"] = slack_manifest.app_manifest_url(str(install.get("app_id") or ""))
    return body


def reset_status_cache() -> None:
    """Drop the cached drift answer (after a save that changes the switches)."""
    _status_cache.clear()

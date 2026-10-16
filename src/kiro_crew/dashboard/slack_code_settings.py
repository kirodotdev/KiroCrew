"""Slack Code settings: the agent-session / code-channel switches and defaults.

The read model, the validation and the manifest-drift report behind Settings →
Slack's "Slack Code" cards. Kept out of :mod:`slack_settings` (whose functions
are composed onto the messaging facade's globals) so its helpers resolve their
own imports; ``slack_settings`` calls in here.

Validation refuses what would make a saved default silently do nothing: a folder
that is not in the sidebar, an agent no scope declares, a model the account
cannot use, an effort off the ladder. A default that was valid when saved and
has since gone stale (the folder deleted, the agent removed) is not refused at
read time: the GET reports it in ``warnings`` and the runtime falls through to
the next source, exactly as the resolution order says.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Mapping

from aiohttp import web

from kiro_crew import slack_manifest
from kiro_crew.config.sections_slack import CODE_CHANNEL_DEFAULT_MAX
from kiro_crew.effort import is_valid_effort
from kiro_crew.slack import features as slack_features
from kiro_crew.slack import scope_probe

#: The switches the PUT accepts, in display order.
SWITCH_KEYS: tuple[str, ...] = ("agent_sessions", "code_channels")
#: The ``code_channel_defaults`` keys the PUT accepts.
DEFAULT_KEYS: tuple[str, ...] = ("folder", "agent", "model", "reasoning_effort")
#: The model picker's "pin nothing, inherit" spelling; stored as unset.
_INHERIT_MODEL = "auto"

#: How long a manifest-drift probe answer is reused (seconds). The page polls on
#: open and on Refresh; Slack's auth.test is tier 4, but there is no reason to
#: call it on every render.
_STATUS_TTL = 60.0
_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class CodeSettingsError(ValueError):
    """A refused Slack Code settings field; the message is safe to show."""


def _folders(state: Any) -> list[dict[str, Any]]:
    raw = getattr(state, "_folders", None) if state is not None else None
    return [f for f in (raw or []) if isinstance(f, dict)]


def _folder_view(folders: list[dict[str, Any]], folder: Mapping[str, Any] | None) -> dict | None:
    if folder is None:
        return None
    fid = str(folder.get("id") or "")
    return {
        "id": fid,
        "path": slack_features.folder_paths(folders).get(fid, str(folder.get("name") or "")),
        "project_dir": slack_features.folder_project_dir(folders, fid) or "",
        "default_agent": str(folder.get("default_agent") or ""),
    }


async def _agent_names(project_dir: str | None) -> set[str]:
    """Every agent a session in *project_dir* could run (user + project scope)."""
    from kiro_crew.agent_discovery import list_agents
    from kiro_crew.executors import discovery_executor

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        discovery_executor(),
        lambda: {a.name for a in list_agents(project_dir=project_dir or None)},
    )


def _slack_agent(cfg: Any) -> str:
    """The agent Slack already uses when nothing else names one."""
    return str(getattr(getattr(cfg, "agent", None), "default_agent", "") or "")


async def code_settings_view(cfg: Any, state: Any) -> dict[str, Any]:
    """The GET body fragment: switches, defaults, effective resolution, warnings."""
    defaults = slack_features.code_channel_defaults(cfg)
    folders = _folders(state)
    resolved = slack_features.resolve_code_channel_session(
        defaults, folders, slack_agent=_slack_agent(cfg) or None
    )
    folder = slack_features.find_folder(folders, defaults.folder)
    warnings: list[str] = []
    if resolved.folder_missing:
        warnings.append("folder_missing")
    if defaults.agent:
        try:
            known = await _agent_names(resolved.project_dir)
        except Exception:
            known = None
        if known is not None and defaults.agent not in known:
            warnings.append("agent_missing")

    def _capability(feature: str) -> dict[str, object]:
        ok, reason = slack_features.availability(feature)
        return {
            "available": ok,
            "reason": reason,
            "scope": slack_features.REQUIRED_SCOPE[feature],
        }

    return {
        # The toggles as saved; whether each one actually runs also needs the
        # capability below (toggle AND available is what the runtime checks).
        "agent_sessions": slack_features.agent_sessions_toggle(cfg),
        "code_channels": slack_features.code_channels_toggle(cfg),
        "agent_sessions_capability": _capability(slack_features.AGENT_SESSIONS),
        "code_channels_capability": _capability(slack_features.CODE_CHANNELS),
        "code_channel_defaults": {
            "folder": defaults.folder,
            "agent": defaults.agent,
            "model": defaults.model,
            "reasoning_effort": defaults.reasoning_effort,
        },
        "code_channel_effective": {
            "folder": _folder_view(folders, folder),
            "agent": resolved.agent,
            "agent_source": resolved.agent_source,
            "model": resolved.model,
            "reasoning_effort": resolved.reasoning_effort,
            "project_dir": resolved.project_dir,
            "project_source": resolved.project_source,
            "slack_agent": _slack_agent(cfg),
        },
        "code_channel_warnings": warnings,
    }


def _opt_text(key: str, raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CodeSettingsError(f"code_channel_defaults.{key} must be a string or null")
    value = raw.strip()
    if len(value) > CODE_CHANNEL_DEFAULT_MAX:
        raise CodeSettingsError(
            f"code_channel_defaults.{key} must be at most {CODE_CHANNEL_DEFAULT_MAX} characters"
        )
    return value or None


def _stored_default(prior: Mapping[str, Any], key: str) -> str | None:
    """A stored default as the builder reads it: trimmed text, else unset."""
    value = prior.get(key)
    if not isinstance(value, str) or len(value.strip()) > CODE_CHANNEL_DEFAULT_MAX:
        return None
    return value.strip() or None


async def validate_code_settings(
    body: Mapping[str, Any],
    stored: Mapping[str, Any],
    *,
    state: Any,
    request: web.Request,
) -> tuple[dict[str, object], list[str]]:
    """Validate the Slack Code fields of a PUT body against the stored section.

    Returns ``(staged, applied)``: the ``slack.*`` keys to write and their names,
    staging only real changes. Raises :class:`CodeSettingsError` on the first
    refusal; nothing is staged in that case.
    """
    staged: dict[str, object] = {}
    applied: list[str] = []
    for key in SWITCH_KEYS:
        if key not in body:
            continue
        val = body.get(key)
        if not isinstance(val, bool):
            raise CodeSettingsError(f"{key} must be a boolean")
        if val != (stored.get(key) is True):
            staged[key] = val
            applied.append(key)

    if "code_channel_defaults" not in body:
        return staged, applied
    raw = body.get("code_channel_defaults")
    if not isinstance(raw, dict):
        raise CodeSettingsError("code_channel_defaults must be an object")
    unknown = sorted(set(raw) - set(DEFAULT_KEYS))
    if unknown:
        raise CodeSettingsError(f"code_channel_defaults has unknown key(s): {', '.join(unknown)}")
    prior = stored.get("code_channel_defaults")
    prior = prior if isinstance(prior, dict) else {}
    stored_defaults = {k: _stored_default(prior, k) for k in DEFAULT_KEYS}
    # A key the body omits keeps its stored value; null or "" clears it.
    merged: dict[str, str | None] = {
        k: _opt_text(k, raw[k]) if k in raw else stored_defaults[k] for k in DEFAULT_KEYS
    }

    folders = _folders(state)
    project_dir: str | None = None
    if merged["folder"] is not None and "folder" in raw:
        folder = slack_features.find_folder(folders, merged["folder"])
        if folder is None:
            raise CodeSettingsError(
                "code_channel_defaults.folder does not name exactly one sidebar folder"
            )
        # Stored by id: a rename or a move in the sidebar keeps the default.
        merged["folder"] = str(folder.get("id"))
    if merged["folder"] is not None:
        project_dir = slack_features.folder_project_dir(folders, merged["folder"])

    if merged["agent"] is not None and ("agent" in raw or "folder" in raw):
        try:
            known = await _agent_names(project_dir)
        except Exception as exc:
            raise CodeSettingsError("could not list agents to check the default agent") from exc
        if merged["agent"] not in known:
            raise CodeSettingsError(f"code_channel_defaults.agent {merged['agent']!r} not found")

    if merged["model"] is not None and "model" in raw:
        if merged["model"] == _INHERIT_MODEL:
            merged["model"] = None
        else:
            from kiro_crew.dashboard.handlers.core import _validate_role_model

            reason = _validate_role_model(merged["model"], request)
            if reason:
                raise CodeSettingsError(f"code_channel_defaults.model: {reason}")

    effort = merged["reasoning_effort"]
    if effort is not None and not is_valid_effort(effort):
        raise CodeSettingsError(
            "code_channel_defaults.reasoning_effort must be one of low, medium, high, xhigh, max"
        )

    # Only the sub-keys this save changes: the write merges them into the object
    # as re-read inside the config lock (``merge_nested``), so a concurrent edit
    # to another default survives instead of being replaced by this snapshot.
    changed = {k: merged[k] for k in DEFAULT_KEYS if merged[k] != stored_defaults[k]}
    if changed:
        staged["code_channel_defaults"] = changed
        applied.append("code_channel_defaults")
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
        status = await scope_probe.probe_manifest_status(
            client,
            required.bot_scopes,
            known_scopes=_known_scopes(),
            probe_code_channels=slack_manifest.CODE_CHANNELS in enabled,
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

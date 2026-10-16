"""Slack Code: whether agent sessions and code channels run, and with what defaults.

Two layers decide whether a feature runs, and both are read here so every caller
answers "is it on?" the same way:

* the **toggle** -- ``slack.agent_sessions`` / ``slack.code_channels`` in
  ``config.json`` (Settings → Slack). It also decides what the generated app
  manifest asks Slack for (:func:`manifest_features`). Anything that is not a real
  ``True`` is off. Read live, so a flip applies on the next event.
* the **capability** -- whether the installed Slack app can actually use the
  feature: its granted bot scopes (``auth.test``'s ``x-oauth-scopes``, read when
  the Slack socket connects and on every reconnect) include the feature's scope
  (``assistant:write`` / ``code_channels:manage``), and Slack has not answered a
  call for it with ``feature_disabled``, ``missing_scope`` or
  ``not_allowed_token_type``. Such an answer LATCHES the feature off for the
  process (until the next reconnect re-probes): one dashboard notification, one
  log line, and the runtime falls back silently to today's Slack behaviour.

:func:`agent_sessions_enabled` / :func:`code_channels_enabled` are toggle AND
capability -- the runtimes call these. :func:`agent_sessions_available` /
:func:`code_channels_available` are the capability alone, for the settings page.

``code_channel_defaults`` is the folder / agent / model / effort a new
code-channel session starts with; :func:`resolve_code_channel_session` applies
the resolution order. The config readers and the resolver are pure; the
capability record is the one piece of process state, guarded by a lock.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from kiro_crew.effort import is_valid_effort

logger = logging.getLogger(__name__)

#: Feature names, matching ``slack_manifest.AGENT_SESSIONS`` / ``CODE_CHANNELS``.
AGENT_SESSIONS = "agent_sessions"
CODE_CHANNELS = "code_channels"

#: The bot scope each feature cannot run without.
REQUIRED_SCOPE: dict[str, str] = {
    AGENT_SESSIONS: "assistant:write",
    CODE_CHANNELS: "code_channels:manage",
}

#: Slack error codes that mean "this install cannot use the feature": they latch
#: it off rather than being retried on every message.
LATCH_ERRORS = frozenset({"feature_disabled", "missing_scope", "not_allowed_token_type"})

#: ``availability()`` reasons beyond the latch codes.
REASON_SCOPES_UNKNOWN = "scopes_unknown"

#: Sidebar folder ids are ``uuid4().hex[:12]`` (``chat_folders``); an id-shaped
#: reference is matched by id only, never as a folder name.
_FOLDER_ID_LEN = 12
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class CodeChannelDefaults:
    """What a new code-channel session starts with; ``None`` = not set here."""

    folder: str | None = None
    agent: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def is_empty(self) -> bool:
        """True when no default is set at all."""
        return not (self.folder or self.agent or self.model or self.reasoning_effort)


@dataclass(frozen=True)
class ResolvedCodeChannelSession:
    """The effective start state of a new code-channel session.

    ``*_source`` names where each value came from (``"default"``, ``"folder"``,
    ``"slack"``, ``"origin"`` or ``""`` for nothing), so the settings page can
    explain the outcome and a runtime can log it without re-deriving it.
    """

    folder_id: str | None
    agent: str | None
    agent_source: str
    model: str | None
    reasoning_effort: str | None
    project_dir: str | None
    project_source: str
    #: True when a folder default is configured but does not exist.
    folder_missing: bool = False


def _slack_section(cfg: Any) -> Any:
    """``cfg.slack`` for a full config, or *cfg* itself for a ``SlackConfig``."""
    return getattr(cfg, "slack", cfg)


def agent_sessions_toggle(cfg: Any) -> bool:
    """The Agent sessions toggle (``slack.agent_sessions``), capability aside."""
    return getattr(_slack_section(cfg), "agent_sessions", False) is True


def code_channels_toggle(cfg: Any) -> bool:
    """The Code channels toggle (``slack.code_channels``), capability aside."""
    return getattr(_slack_section(cfg), "code_channels", False) is True


def agent_sessions_enabled(cfg: Any) -> bool:
    """Run Slack threads as agent sessions: the toggle is on AND the app can."""
    return agent_sessions_toggle(cfg) and agent_sessions_available()


def code_channels_enabled(cfg: Any) -> bool:
    """Let the agent open code channels: the toggle is on AND the app can."""
    return code_channels_toggle(cfg) and code_channels_available()


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


def agent_sessions_available() -> bool:
    """The installed app can run agent sessions (scope granted, not latched)."""
    return availability(AGENT_SESSIONS)[0]


def code_channels_available() -> bool:
    """The installed app can open code channels (scope granted, not latched)."""
    return availability(CODE_CHANNELS)[0]


def manifest_features(cfg: Any) -> frozenset[str]:
    """The Slack Code switches *cfg* turns on, as ``slack_manifest`` feature names.

    The one bridge from config to the toggle-aware manifest: every emitter
    (``kirocrew manifest``, ``GET /api/slack/manifest``, the settings page's
    update panel) passes this to ``slack_manifest.render``.
    """
    from kiro_crew import slack_manifest

    # The TOGGLE, never the capability: the manifest is how an install gains the
    # capability, so it must ask for the scopes before they are granted.
    on: set[str] = set()
    if agent_sessions_toggle(cfg):
        on.add(slack_manifest.AGENT_SESSIONS)
    if code_channels_toggle(cfg):
        on.add(slack_manifest.CODE_CHANNELS)
    return frozenset(on)


def _opt_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def code_channel_defaults(cfg: Any) -> CodeChannelDefaults:
    """``slack.code_channel_defaults`` as a frozen value, invalid fields unset."""
    raw = getattr(_slack_section(cfg), "code_channel_defaults", None)
    effort = _opt_str(getattr(raw, "reasoning_effort", None))
    return CodeChannelDefaults(
        folder=_opt_str(getattr(raw, "folder", None)),
        agent=_opt_str(getattr(raw, "agent", None)),
        model=_opt_str(getattr(raw, "model", None)),
        reasoning_effort=effort if is_valid_effort(effort) else None,
    )


def looks_like_folder_id(ref: str) -> bool:
    """True when *ref* has the shape of a sidebar folder id."""
    return len(ref) == _FOLDER_ID_LEN and set(ref) <= _HEX


def folder_paths(folders: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Map each sidebar folder id to its ``Parent/Child`` path (cycle-safe)."""
    by_id = {str(f.get("id") or ""): f for f in folders if isinstance(f, Mapping) and f.get("id")}
    paths: dict[str, str] = {}
    for fid in by_id:
        segments: list[str] = []
        seen: set[str] = set()
        cur = fid
        while cur and cur in by_id and cur not in seen:
            seen.add(cur)
            segments.append(str(by_id[cur].get("name") or "?"))
            cur = str(by_id[cur].get("parent_id") or "")
        paths[fid] = "/".join(reversed(segments))
    return paths


def find_folder(folders: Iterable[Mapping[str, Any]], ref: str | None) -> Mapping[str, Any] | None:
    """The sidebar folder *ref* names -- by id, else by unique ``Parent/Child`` path.

    Path matching is case-insensitive and ignores spaces around ``/``. A path two
    folders share is ambiguous and matches nothing: filing into an arbitrary one of
    them would be a silent guess. Returns ``None`` for an unset or dangling ref.
    """
    if not ref:
        return None
    listed = [f for f in folders if isinstance(f, Mapping) and f.get("id")]
    for folder in listed:
        if str(folder.get("id")) == ref:
            return folder
    if looks_like_folder_id(ref):
        return None
    wanted = "/".join(seg.strip().lower() for seg in ref.split("/") if seg.strip())
    paths = folder_paths(listed)
    hits = [f for f in listed if paths.get(str(f.get("id")), "").lower() == wanted]
    return hits[0] if len(hits) == 1 else None


def folder_project_dir(folders: Iterable[Mapping[str, Any]], folder_id: str | None) -> str | None:
    """Nearest declared ``project_dir`` on *folder_id*'s ancestor chain (unvalidated)."""
    by_id = {str(f.get("id") or ""): f for f in folders if isinstance(f, Mapping)}
    seen: set[str] = set()
    cur = folder_id or ""
    while cur and cur not in seen:
        seen.add(cur)
        folder = by_id.get(cur)
        if folder is None:
            break
        project = _opt_str(folder.get("project_dir"))
        if project:
            return project
        cur = str(folder.get("parent_id") or "")
    return None


def resolve_code_channel_session(
    defaults: CodeChannelDefaults,
    folders: Iterable[Mapping[str, Any]],
    *,
    slack_agent: str | None = None,
    origin_project_dir: str | None = None,
) -> ResolvedCodeChannelSession:
    """Apply the code-channel resolution order to one new session.

    * agent: ``defaults.agent`` > the folder's ``default_agent`` > *slack_agent*
      (the agent Slack already uses for the originating conversation);
    * model / effort: ``defaults`` only -- unset means the agent's own;
    * project: the folder's ``project_dir`` (nearest ancestor that declares one)
      > *origin_project_dir* (the project the origin thread is bound to) > none.

    Pure: the caller supplies the folder list. Validation of the project path
    (sensitive paths, existence) stays with the session layer that opens it.
    """
    listed = [f for f in folders if isinstance(f, Mapping)]
    folder = find_folder(listed, defaults.folder)
    folder_id = str(folder.get("id")) if folder is not None else None
    folder_agent = _opt_str(folder.get("default_agent")) if folder is not None else None

    slack_choice = _opt_str(slack_agent)
    agent: str | None
    if defaults.agent:
        agent, agent_source = defaults.agent, "default"
    elif folder_agent:
        agent, agent_source = folder_agent, "folder"
    elif slack_choice:
        agent, agent_source = slack_choice, "slack"
    else:
        agent, agent_source = None, ""

    project = folder_project_dir(listed, folder_id)
    if project:
        project_source = "folder"
    elif _opt_str(origin_project_dir):
        project, project_source = _opt_str(origin_project_dir), "origin"
    else:
        project, project_source = None, ""

    return ResolvedCodeChannelSession(
        folder_id=folder_id,
        agent=agent,
        agent_source=agent_source,
        model=defaults.model,
        reasoning_effort=defaults.reasoning_effort,
        project_dir=project,
        project_source=project_source,
        folder_missing=bool(defaults.folder) and folder is None,
    )

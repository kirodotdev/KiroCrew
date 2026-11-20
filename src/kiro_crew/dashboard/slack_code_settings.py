"""Slack Code settings: the registered switches and the code-channel defaults.

The read model, the validation and the manifest-drift report behind Settings →
Messaging Channels → Slack's "Slack Code" cards. Kept out of
:mod:`slack_settings` (whose functions are composed onto the messaging
facade's globals) so its helpers resolve their own imports; ``slack_settings``
calls in here.

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
from kiro_crew.agent_files import HEARTBEAT_AGENT_FILENAME
from kiro_crew.config.sections_slack import (
    CODE_CHANNEL_APPROVAL_ASK,
    CODE_CHANNEL_APPROVAL_MODES,
    CODE_CHANNEL_DEFAULT_MAX,
)
from kiro_crew.effort import is_valid_effort
from kiro_crew.slack import features as slack_features
from kiro_crew.slack import scope_probe

# The switches the PUT accepts and the GET reports are every switch registered
# in ``slack_manifest.FEATURE_SCOPES`` (read at call time, as
# ``slack_manifest.FEATURES``), each under its own ``slack.<name>`` key, so
# registering one there needs nothing here.
#: The ``code_channel_defaults`` keys the PUT accepts.
DEFAULT_KEYS: tuple[str, ...] = ("folder", "agent", "model", "reasoning_effort", "approval_mode")
#: ``code_channel_defaults.conductor_mode`` ("Always use conductor mode"): a boolean, so it
#: is validated apart from the text keys above. Stored only when on.
CONDUCTOR_MODE_KEY = "conductor_mode"
#: ``slack.code_channel_dispatch``: the sub-toggle under Code channels ("Let code
#: channels start helper agents"). Off when unset; a save that newly turns Code
#: channels on also turns it on unless a value is already stored (see
#: :func:`dispatch_follows_enable`). Hot-applied like the switches, through the
#: live config watcher.
DISPATCH_KEY = "code_channel_dispatch"
#: The model picker's "pin nothing, inherit" spelling; stored as unset.
_INHERIT_MODEL = "auto"

#: How long a manifest-drift probe answer is reused (seconds). The page polls on
#: open and on Refresh; Slack's auth.test is tier 4, but there is no reason to
#: call it on every render.
_STATUS_TTL = 60.0
_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class CodeSettingsError(ValueError):
    """A refused Slack Code settings field; the message is safe to show."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _overlay_read(*keys: str) -> tuple[str, object]:
    """``(state, value)``: does ``config.local.json`` set the key at *keys*
    (``"slack", "code_channel_dispatch"`` for ``slack.code_channel_dispatch``)?

    *state* is ``"pinned"``, ``"absent"`` or ``"unreadable"``; *value* is what the
    overlay sets when pinned, else ``None``.

    Not ``overlay_pins``, which answers "not pinned" for a file it cannot read:
    a save must not take an unreadable overlay -- which may hold the value that
    wins -- for one that does not set the key.

    Read as the load reads it (``read_endorsed_config_text``), with no
    ``exists()`` check first, as ``overlay_entry`` does: where the sandbox seals
    the file, a pending hand-edit or delete still reads as the endorsed copy the
    loader keeps in force.
    """
    import json

    from kiro_crew.config.loader import config_local_path, read_endorsed_config_text

    path = config_local_path()
    try:
        node: object = json.loads(read_endorsed_config_text(path))
    except FileNotFoundError:
        # Neither a file nor an endorsed copy (a dangling symlink included): what
        # the loader also reads as absent.
        return "absent", None
    except (OSError, ValueError):
        return "unreadable", None
    for key in keys:
        if not isinstance(node, dict):
            return "unreadable", None
        if key not in node:
            return "absent", None
        node = node[key]
    return "pinned", node


def _overlay_state(*keys: str) -> str:
    """The *state* half of :func:`_overlay_read`."""
    return _overlay_read(*keys)[0]


def _dispatch_overlay_state() -> str:
    """:func:`_overlay_state` for ``slack.code_channel_dispatch``."""
    return _overlay_state("slack", DISPATCH_KEY)


def _conductor_overlay_read() -> tuple[str, object]:
    """:func:`_overlay_read` for ``slack.code_channel_defaults.conductor_mode``.

    A ``code_channel_defaults`` the overlay sets to a non-object (``null``, a
    string) replaces the base one whole in the merge (``_deep_merge``), and the
    reader takes it as all-unset: the overlay pins the switch off, so it reads
    ``("pinned", None)``, not unreadable.
    """
    return _conductor_overlay_from(*_overlay_read("slack", "code_channel_defaults"))


def _conductor_overlay_from(state: str, section: object) -> tuple[str, object]:
    """:func:`_conductor_overlay_read` from one read of ``slack.code_channel_defaults``."""
    if state != "pinned":
        return state, None
    if not isinstance(section, dict):
        return "pinned", None
    if CONDUCTOR_MODE_KEY not in section:
        return "absent", None
    return "pinned", section[CONDUCTOR_MODE_KEY]


def _conductor_overlay_state() -> str:
    """The *state* half of :func:`_conductor_overlay_read`."""
    return _conductor_overlay_read()[0]


def conductor_overlay_unreadable() -> bool:
    """Whether ``config.local.json`` cannot be read, so whether it sets "Always use
    conductor mode" is unknown.

    The one rule every reader of the switch applies (the bind, the settings GET,
    the save, and the card through ``code_channel_conductor_mode_overlay_state``):
    while the file is unreadable the switch counts as OFF, read from the file at
    the moment of the decision. Fail-closed, as the helper-agents switch is, and
    independent of whether the loader dropped the file or the live watcher kept
    an earlier copy. Filesystem read: run it through ``asyncio.to_thread``.
    """
    return _conductor_overlay_state() == "unreadable"


#: How :func:`_overlay_display_path` names the home directory: ``~`` on POSIX,
#: ``%USERPROFILE%`` on Windows, where Explorer, cmd and PowerShell all expand it
#: (Explorer and cmd do not expand ``~``).
HOME_PREFIXES: tuple[str, ...] = ("~/", "%USERPROFILE%\\")


def _overlay_display_path() -> str:
    """``config.local.json``'s path with the home directory shown as one of
    :data:`HOME_PREFIXES`, in the platform's own separators; else the full path. No I/O."""
    import os
    from pathlib import Path

    from kiro_crew.config.loader import config_local_path

    path = config_local_path()
    try:
        rel = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return HOME_PREFIXES[1] + str(rel) if os.name == "nt" else HOME_PREFIXES[0] + rel.as_posix()


def _dispatch_overlay_pins() -> bool:
    """Whether ``config.local.json`` may set ``slack.code_channel_dispatch`` (an
    unreadable overlay counts: what it holds is unknown)."""
    return _dispatch_overlay_state() != "absent"


def dispatch_follows_enable(stored: Mapping[str, Any]) -> bool:
    """Whether a save that turns Code channels on in *stored* (the ``slack``
    section of ``config.json``) also turns helper agents on.

    Only for a first enable: Code channels not saved on, and no dispatch value
    stored, so an explicit off (and any value an existing install saved) is
    kept. ``config.local.json`` is checked by the caller: a value it sets wins,
    and one it may set (unreadable) is unknown, so neither gets a base write.
    """
    return stored.get(slack_manifest.CODE_CHANNELS) is not True and DISPATCH_KEY not in stored


def _dispatch_on_enable() -> bool:
    """:func:`dispatch_follows_enable` on the files as they are now, for the
    card; ``False`` (helpers stay as shown) when either file cannot be read."""
    from kiro_crew.config.loader import config_path, read_config_for_update

    if _dispatch_overlay_state() != "absent":
        return False
    try:
        data = read_config_for_update(config_path())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    # A non-object ``slack`` section is rebuilt as ``{}`` by the save, as here.
    slack = data.get("slack")
    return dispatch_follows_enable(slack if isinstance(slack, dict) else {})


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


#: Templates the code-channel agent picker leaves out beyond what the chat
#: picker's catalog already withholds (``kirocrew-lite``, the title/compaction
#: and routing-classifier agent with no tools; ``kirocrew-guest``, a trust
#: boundary; the dashboard manager; crewmates' private copies). The heartbeat
#: agent runs unattended polling, never a conversation.
_BACKGROUND_AGENT_FILES = frozenset({HEARTBEAT_AGENT_FILENAME})


def _choosable_agent_names(project_dir: str | None) -> list[str]:
    """The agents a code channel may run, in the chat picker's order.

    The chat picker's own template choices (so ``kirocrew-conductor``,
    ``kirocrew-team-lead`` and every other shipped agent a person can run are
    offered), minus Kiro Crew's generated specs (the side-turn read-only spec,
    skill-view aliases) and the background-only agents above.
    """
    return list(_choosable_agents(project_dir))


def _choosable_agents(project_dir: str | None) -> dict[str, str]:
    """:func:`_choosable_agent_names`, each with its spec's ``description``, in order."""
    from kiro_crew.agent_discovery import is_internal_agent_spec, list_agents
    from kiro_crew.dashboard.handlers.agent_catalog import _choosable_templates

    return {
        a.name: str(a.description or "")
        for a in _choosable_templates(list_agents(project_dir=project_dir or None))
        if not is_internal_agent_spec(a)
        and not (a.kirocrew_owned and a.filename in _BACKGROUND_AGENT_FILES)
    }


async def _agent_names(project_dir: str | None) -> list[str]:
    """:func:`_choosable_agent_names`, off the event loop."""
    return list(await _agent_menu(project_dir))


async def _agent_menu(project_dir: str | None) -> dict[str, str]:
    """:func:`_choosable_agents`, off the event loop."""
    from kiro_crew.executors import discovery_executor

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(discovery_executor(), lambda: _choosable_agents(project_dir))


def _defaults_project_dir(folders: list[dict[str, Any]], folder_ref: str | None) -> str | None:
    """The project a default agent is looked up in, for default folder *folder_ref*.

    The folder it names (by id, or by path from a hand edit), else the auto
    folder for an unset one; ``None`` when neither resolves. The save's agent
    check and the picker's list both use this, so they cannot disagree.
    """
    folder = (
        slack_features.find_folder(folders, folder_ref)
        if folder_ref
        else slack_features.find_auto_folder(folders)
    )
    if folder is None:
        return None
    return slack_features.folder_project_dir(folders, str(folder.get("id")))


async def default_agent_available(agent: str | None, project_dir: str | None) -> bool | None:
    """Whether the saved default *agent* can still be chosen in *project_dir*.

    ``None`` when there is no saved agent or the list cannot be read (the
    default is then kept, as before). The settings view's ``agent_missing``
    warning and the runtime's resolution both use this, so they agree.
    """
    if not agent:
        return None
    try:
        return agent in await _agent_names(project_dir)
    except Exception:
        return None


async def code_channel_agents(state: Any, folder_ref: str | None) -> list[str] | None:
    """The agents a save accepts with default folder *folder_ref*; ``None`` when
    they cannot be listed. Lets the picker follow an unsaved folder pick."""
    try:
        return await _agent_names(_defaults_project_dir(_folders(state), folder_ref or None))
    except Exception:
        return None


#: The Connections registry slug of the card the Slack MCP switch is for.
SLACK_CONNECTIONS_SLUG = "slack"


def _slack_card_offered(cfg: Any) -> bool:
    """Connections shows a Slack card, by the Connections page's own two rules.

    The gallery is on (``connections_ui``, a real ``True`` as the page reads it;
    off, it offers no provider at all) and the registry shows the entry
    (``is_visible``). Whether that card can already *connect* (its OAuth app
    saved, the Crew-owned sign-in preview on) is the card's to say, not ours.
    """
    from kiro_crew.connections import registry

    if getattr(cfg, "connections_ui", False) is not True:
        return False
    provider = registry.get_provider(SLACK_CONNECTIONS_SLUG)
    return provider is not None and registry.is_visible(provider)


async def code_settings_view(cfg: Any, state: Any) -> dict[str, Any]:
    """The GET body fragment: switches, defaults, effective resolution, warnings."""
    from kiro_crew import crew_mode
    from kiro_crew.slack.code_channels.binding import resolved_default_agent

    defaults = slack_features.code_channel_defaults(cfg)
    conductor_agent = slack_features.code_channel_conductor_agent(cfg)
    unreadable = bool(conductor_agent) and await asyncio.to_thread(conductor_overlay_unreadable)
    if unreadable:
        # The rule the bind applies: unknown reads as off.
        conductor_agent = None
    # ``agent.default_agent`` as a new code channel resolves it (a removed spec
    # falls back to the managed default), so the preview names what will run.
    slack_agent = await asyncio.to_thread(resolved_default_agent, cfg)
    folders = _folders(state)
    resolved = slack_features.resolve_code_channel_session(
        defaults, folders, slack_agent=slack_agent or None, conductor_agent=conductor_agent
    )
    folder = (
        slack_features.find_folder(folders, defaults.folder)
        if defaults.folder
        else slack_features.find_auto_folder(folders)
    )
    warnings: list[str] = []
    if resolved.folder_missing:
        warnings.append("folder_missing")
    try:
        choices: list[str] | None = await _agent_names(resolved.project_dir)
    except Exception:
        choices = None
    # The same rule as ``default_agent_available``, on the list already read.
    # Reported while conductor mode decides the agent too: the card hides it while
    # its draft has the switch on, and shows it the moment the draft turns it off.
    if defaults.agent and choices is not None and defaults.agent not in choices:
        warnings.append("agent_missing")
        # What a new channel runs with instead, as the runtime resolves it.
        resolved = slack_features.resolve_code_channel_session(
            defaults,
            folders,
            slack_agent=slack_agent or None,
            default_agent_available=False,
            conductor_agent=conductor_agent,
        )

    def _capability(feature: str) -> dict[str, object]:
        ok, reason = slack_features.availability(feature)
        return {
            "available": ok,
            "reason": reason,
            "scope": slack_features.REQUIRED_SCOPE[feature],
        }

    # Per registered switch: the toggle as saved (``<name>``) and, for a switch
    # the gateway runs, whether the install can run it (``<name>_capability``);
    # toggle AND available is what the runtime checks. The Slack MCP switch has
    # no capability: nothing runs for it, and a bot token cannot read the user
    # scopes or app settings it adds.
    switches: dict[str, Any] = {}
    for feature in slack_manifest.FEATURES:
        switches[feature] = slack_features.toggle(cfg, feature)
        if feature not in slack_manifest.MANIFEST_ONLY_FEATURES:
            switches[f"{feature}_capability"] = _capability(feature)
    return {
        **switches,
        # The code-channel dispatch sub-toggle under Code channels, with no
        # manifest scope of its own, so it is not a registered switch.
        DISPATCH_KEY: getattr(getattr(cfg, "slack", None), DISPATCH_KEY, False) is True,
        # Turning Code channels on now would also turn helper agents on (nothing
        # stored, no overlay), so the card can show that before the save.
        f"{DISPATCH_KEY}_on_enable": await asyncio.to_thread(_dispatch_on_enable),
        # ``config.local.json`` sets it: a save writes the base file, which the
        # overlay wins over, so the card disables the toggle and the PUT refuses.
        f"{DISPATCH_KEY}_overlay": await asyncio.to_thread(_dispatch_overlay_pins),
        # Which: "pinned" (the value shown is the one it sets) or "unreadable"
        # (what it sets is unknown), so the card never states a value it cannot know.
        f"{DISPATCH_KEY}_overlay_state": await asyncio.to_thread(_dispatch_overlay_state),
        # Where that file is, home shown as ``~``, so the card can name it.
        f"{DISPATCH_KEY}_overlay_path": _overlay_display_path(),
        # Whether Connections offers the Slack card, which is the only way to
        # use what the Slack MCP switch adds; the page hides that switch
        # without it (unless it is already on, so it can be turned off).
        "slack_mcp_card": _slack_card_offered(cfg),
        "code_channel_defaults": {
            "folder": defaults.folder,
            "agent": defaults.agent,
            "model": defaults.model,
            "reasoning_effort": defaults.reasoning_effort,
            "approval_mode": defaults.approval_mode,
            "conductor_mode": defaults.conductor_mode,
        },
        # The agent "Always use conductor mode" runs (``agent.crew_mode_agent``), so
        # the switch can name it; shown only where conductor mode is offered.
        "code_channel_conductor_agent": crew_mode.crew_mode_agent(cfg),
        # ``config.local.json`` sets the switch ("pinned"), may (``"unreadable"``),
        # or does not (``"absent"``): the card disables it unless absent, and the
        # PUT refuses a change, as for the dispatch switch.
        "code_channel_conductor_mode_overlay_state": await asyncio.to_thread(
            _conductor_overlay_state
        ),
        "code_channel_effective": {
            "folder": _folder_view(folders, folder),
            "agent": resolved.agent,
            "agent_source": resolved.agent_source,
            "model": resolved.model,
            "reasoning_effort": resolved.reasoning_effort,
            "project_dir": resolved.project_dir,
            "project_source": resolved.project_source,
            "approval_mode": resolved.approval_mode,
            "slack_agent": slack_agent,
            "folder_path": resolved.folder_path,
            "folder_auto_create": resolved.folder_auto_create,
        },
        # What an unset default folder means, so the picker's default row and the
        # preview say it without a second copy of the rule: the folder's name,
        # the id when it already exists, whether two top-level folders share the
        # name (then neither is used), and the agent an earlier release created
        # it with, which reads as no folder agent.
        "code_channel_auto_folder": {
            "name": slack_features.AUTO_FOLDER_NAME,
            "legacy_agent": slack_features.LEGACY_AUTO_FOLDER_AGENT,
            "id": (
                str(auto["id"])
                if (auto := slack_features.find_auto_folder(folders)) is not None
                else None
            ),
            "ambiguous": len(slack_features.auto_folder_matches(folders)) > 1,
        },
        # The agents the default-agent picker offers; ``None`` when they could
        # not be listed (the picker then says so rather than showing none).
        "code_channel_agents": choices,
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
    """A stored default as the builder reads it: trimmed text, else unset.

    An effort off the ladder (``"High"`` from a hand edit) is unset too, as the
    builder reads it: kept, it would be merged into every later save and refuse
    it over a value the page shows as empty.
    """
    value = prior.get(key)
    if not isinstance(value, str) or len(value.strip()) > CODE_CHANNEL_DEFAULT_MAX:
        return None
    text = value.strip() or None
    if key == "reasoning_effort" and text is not None and not is_valid_effort(text):
        return None
    return text


async def validate_code_settings(
    body: Mapping[str, Any],
    stored: Mapping[str, Any],
    *,
    state: Any,
    request: web.Request | None,
) -> tuple[dict[str, object], list[str]]:
    """Validate the Slack Code fields of a PUT body against the stored section.

    Returns ``(staged, applied)``: the ``slack.*`` keys to write and their names,
    staging only real changes. Raises :class:`CodeSettingsError` on the first
    refusal; nothing is staged in that case. *request* is needed only to check a
    default model; a caller with none (the code channel's hand-off button) sends
    no defaults.
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
    # A first enable of Code channels turns helper agents on with it; a value
    # already stored (an explicit off included) is kept.
    enabling = staged.get(slack_manifest.CODE_CHANNELS) is True and dispatch_follows_enable(stored)
    if DISPATCH_KEY in body:
        val = body.get(DISPATCH_KEY)
        if not isinstance(val, bool):
            raise CodeSettingsError(f"{DISPATCH_KEY} must be a boolean")
        # Refused, not acknowledged: the save writes config.json and the overlay
        # deep-merges over it, so a withdrawal saved here would report success
        # while dispatch stayed granted.
        overlay = await asyncio.to_thread(_dispatch_overlay_state)
        if overlay == "pinned":
            raise CodeSettingsError(
                f"Helper agents are set in config.local.json (slack.{DISPATCH_KEY}), "
                "which overrides this setting; change it there",
                status=409,
            )
        if overlay == "unreadable":
            raise CodeSettingsError(
                "config.local.json cannot be read, so whether it turns helper agents on "
                f"(slack.{DISPATCH_KEY}) is unknown; fix the file and try again",
                status=409,
            )
        # On a first enable an off is stored too, so a later enable keeps it.
        if val != (stored.get(DISPATCH_KEY) is True) or enabling:
            staged[DISPATCH_KEY] = val
            applied.append(DISPATCH_KEY)
    elif enabling and await asyncio.to_thread(_dispatch_overlay_state) == "absent":
        # Only with no overlay: one that sets the key wins over this write, and
        # one that cannot be read may, so helpers are left as the files say.
        staged[DISPATCH_KEY] = True
        applied.append(DISPATCH_KEY)

    if "code_channel_defaults" not in body:
        return staged, applied
    raw = body.get("code_channel_defaults")
    if not isinstance(raw, dict):
        raise CodeSettingsError("code_channel_defaults must be an object")
    unknown = sorted(set(raw) - set(DEFAULT_KEYS) - {CONDUCTOR_MODE_KEY})
    if unknown:
        raise CodeSettingsError(f"code_channel_defaults has unknown key(s): {', '.join(unknown)}")
    prior = stored.get("code_channel_defaults")
    prior = prior if isinstance(prior, dict) else {}
    # config.local.json's ``slack.code_channel_defaults``, read ONCE for this save,
    # so the refusal, the switch state and the agent check below cannot disagree.
    overlay_defaults = await asyncio.to_thread(_overlay_read, "slack", "code_channel_defaults")
    overlay_state, overlay_value = _conductor_overlay_from(*overlay_defaults)
    conductor_changed: dict[str, object] = {}
    if CONDUCTOR_MODE_KEY in raw:
        conductor = raw[CONDUCTOR_MODE_KEY]
        if conductor is not None and not isinstance(conductor, bool):
            raise CodeSettingsError(f"code_channel_defaults.{CONDUCTOR_MODE_KEY} must be a boolean")
        # Refused, not acknowledged, as the dispatch switch is: the save writes
        # config.json and the overlay deep-merges over it, so a change saved here
        # would report success while the overlay's value stayed in force.
        if overlay_state == "pinned":
            raise CodeSettingsError(
                "Always use conductor mode is set in config.local.json "
                f"(slack.code_channel_defaults.{CONDUCTOR_MODE_KEY}), which overrides this "
                "setting; change it there",
                status=409,
            )
        if overlay_state == "unreadable":
            raise CodeSettingsError(
                "config.local.json cannot be read, so whether it sets Always use conductor "
                f"mode (slack.code_channel_defaults.{CONDUCTOR_MODE_KEY}) is unknown; fix the "
                "file and try again",
                status=409,
            )
        # Stored only when on; off is unset, the one spelling the builder reads.
        if (conductor is True) != (prior.get(CONDUCTOR_MODE_KEY) is True):
            conductor_changed[CONDUCTOR_MODE_KEY] = True if conductor is True else None
    # Conductor mode decides the agent after this save: the stored default agent is
    # dormant (kept for when it goes off), so a folder change alone does not judge
    # it against the new folder's project. An explicit agent edit still is checked.
    # Read with the overlay applied, as the runtime reads it: a pinned value wins
    # over config.json (and a change to it was refused above). While the overlay
    # is unreadable the switch counts as off (``conductor_overlay_unreadable``),
    # so the stored agent is judged as live.
    after = (
        overlay_value
        if overlay_state == "pinned"
        else (raw.get(CONDUCTOR_MODE_KEY, prior.get(CONDUCTOR_MODE_KEY)))
    )
    conductor_on = (
        overlay_state != "unreadable" and after is True and slack_features.conductor_mode_offered()
    )
    stored_defaults = {k: _stored_default(prior, k) for k in DEFAULT_KEYS}
    # A key the body omits keeps its stored value; null or "" clears it.
    merged: dict[str, str | None] = {
        k: _opt_text(k, raw[k]) if k in raw else stored_defaults[k] for k in DEFAULT_KEYS
    }

    folders = _folders(state)
    if merged["folder"] is not None and "folder" in raw:
        folder = slack_features.find_folder(folders, merged["folder"])
        if folder is None:
            raise CodeSettingsError(
                "code_channel_defaults.folder does not name exactly one sidebar folder"
            )
        # Stored by id: a rename or a move in the sidebar keeps the default.
        merged["folder"] = str(folder.get("id"))
    project_dir = _defaults_project_dir(folders, merged["folder"])

    # Judged when this save edits it, or when it is live after this save and the
    # save moved the folder or turned conductor mode off (the dormant agent runs
    # again, so a missing one is refused rather than saved silently). A request
    # that resends the switch unchanged turns nothing off.
    reawakened = ("folder" in raw or CONDUCTOR_MODE_KEY in conductor_changed) and not conductor_on
    judged_agent, judged_project = merged["agent"], project_dir
    if reawakened and "agent" not in raw and CONDUCTOR_MODE_KEY in conductor_changed:
        # This save turns the switch off and does not edit the agent: judge what the
        # runtime will run after it, the base values with config.local.json merged
        # over them (``_deep_merge``). A plain folder change judges the stored
        # agent against the new folder, as before this switch existed.
        # A section the overlay sets to a non-object is all-unset; an unreadable
        # overlay leaves the base values judged, as before.
        section_state, overlay_section = overlay_defaults
        if section_state == "pinned":
            replaced = not isinstance(overlay_section, dict)
            section: Mapping[str, Any] = (
                overlay_section if isinstance(overlay_section, dict) else {}
            )
            if replaced or "agent" in section:
                # config.local.json decides the agent (or, replacing the section,
                # leaves none): not this page's to change, so nothing to judge here.
                # The runtime skips a missing one and the GET warns about it.
                judged_agent = None
            if replaced or "folder" in section:
                judged_project = _defaults_project_dir(folders, _stored_default(section, "folder"))
    if judged_agent is not None and ("agent" in raw or reawakened):
        try:
            known = await _agent_names(judged_project)
        except Exception as exc:
            raise CodeSettingsError("could not list agents to check the default agent") from exc
        if judged_agent not in known:
            raise CodeSettingsError(f"code_channel_defaults.agent {judged_agent!r} not found")

    if merged["model"] is not None and "model" in raw:
        if merged["model"] == _INHERIT_MODEL:
            merged["model"] = None
        else:
            from kiro_crew.dashboard.handlers import core as core_handlers

            if request is None:
                raise CodeSettingsError("code_channel_defaults.model cannot be checked here")
            # A code channel's session runs on the default harness, so only that
            # harness's live catalog may judge the pin (as the role pins in
            # ``api_kirocrew_config_patch``): a newer member session on another
            # harness must not reject a model the default-harness picker offers.
            provider, pin_backend = await asyncio.to_thread(
                core_handlers._active_provider_and_pin_backend
            )
            reason = await core_handlers._revalidate_role_pin_evidence(
                merged["model"], request, backend=pin_backend
            ) or core_handlers._validate_role_model(
                merged["model"], request, provider, backend=pin_backend
            )
            if reason:
                raise CodeSettingsError(f"code_channel_defaults.model: {reason}")

    effort = merged["reasoning_effort"]
    if effort is not None and not is_valid_effort(effort):
        raise CodeSettingsError(
            "code_channel_defaults.reasoning_effort must be one of low, medium, high, xhigh, max"
        )

    approval = merged["approval_mode"]
    if approval is not None and "approval_mode" in raw:
        if approval not in CODE_CHANNEL_APPROVAL_MODES:
            raise CodeSettingsError(
                "code_channel_defaults.approval_mode must be one of "
                + ", ".join(CODE_CHANNEL_APPROVAL_MODES)
            )
        if approval == CODE_CHANNEL_APPROVAL_ASK:
            # The default: stored as unset, the one spelling the builder reads.
            merged["approval_mode"] = None

    # Only the sub-keys this save changes: the write merges them into the object
    # as re-read inside the config lock (``merge_nested``), so a concurrent edit
    # to another default survives instead of being replaced by this snapshot.
    changed: dict[str, object] = {
        k: merged[k] for k in DEFAULT_KEYS if merged[k] != stored_defaults[k]
    }
    changed.update(conductor_changed)
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
    # An optional scope missing from an older install is not drift (OPTIONAL_BOT_SCOPES).
    needed_scopes = required.bot_scopes - slack_manifest.OPTIONAL_BOT_SCOPES
    body: dict[str, Any] = {
        "features": [f for f in slack_manifest.FEATURES if f in enabled],
        "required_scopes": sorted(needed_scopes),
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
            # Code-channel status writes latch privately (they must not turn a
            # feature off): lift that latch too.
            slack_features.clear_private_latches()
        status = await scope_probe.probe_manifest_status(
            client,
            needed_scopes,
            # Nor a removable leftover: an install that holds it keeps add-by-email.
            known_scopes=_known_scopes() - slack_manifest.OPTIONAL_BOT_SCOPES,
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

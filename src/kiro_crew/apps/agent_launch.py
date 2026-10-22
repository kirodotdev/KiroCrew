"""The Crew-owned launch copy a palette-opened app agent runs under on kiro-cli.

kiro-cli takes its agent as a NAME (``--agent <name>``, ``session/set_mode``) and
resolves that name against the session's checkout before ``~/.kiro/agents``, by
declared ``name`` or by filename stem. The app's own spec is registered as
``~/.kiro/agents/<app>--<agent>.json`` but declares the bare ``name: <agent>``, so
handing kiro-cli the bare name lets a checkout's same-named spec win, including one
written after every check ran.

So a session a palette command opened for its app's own agent does not launch the
bare name on kiro-cli. Before each turn the app's registered file -- that one file,
read by its path, never looked up by name -- is copied to
``~/.kiro/agents/kirocrew-app-launch-<app tag>-<agent tag>.json`` with ``name`` set
to the same reserved name, and kiro-cli is asked for that name -- on ``--agent``
and in ``session/set_mode`` only (the Kiro runtime's ``launch_agent``). The session's
agent stays the bare name every gate, profile, audit row and hook reads. The
ownership proof (the registered file declares the agent) runs on the read that
produces the copy, so kiro-cli is only ever asked for a name Crew writes.

kiro-cli looks a name up in ``<process cwd>/.kiro/agents`` before
``~/.kiro/agents``, and it takes its project agents from its PROCESS working
directory, not the ACP session's. So the kiro-cli process that runs a launch copy
is started in :func:`launch_process_cwd`, an empty directory under the data home.
On Linux the sandbox seals it read-only and Crew's launcher enters it after the seals
(``acp.launch.process_directory``); on macOS and Windows it is as protected as
``~/.kiro/agents`` itself. No checkout's ``.kiro/agents`` is ever read for the name,
however late a writer acts; the session's own cwd is still the checkout, where its
tools, ``file://`` resources and ``AGENTS.md`` resolve. The paths kiro-cli reports
against its process directory are mapped back to the session's
(:func:`map_process_cwd_paths`), and Crew's own reads of the spec take the copy by its
path (:func:`read_launch_copy`). Agent-by-path in kiro-cli would retire the
directory. An app agent may not take the ``kirocrew-app-launch-*`` prefix
(``apps.manager``), so no app's own spec shares a copy's name. A file
already at the copy's path that does not carry :data:`OWNER_MARKER` is somebody
else's and is never overwritten, and a second user-level spec declaring the
reserved name refuses the turn.

The copy is the registered spec's native skill view, as every Kiro launch runs
(``acp.skill_projection.project_agent_view``, reached through
``agent_sdk.drivers.acp.native_skill_view``): ``skill://`` resources are dropped
and the agent's skills are served through ``kirocrew-core``'s ``skill_search``,
scoped to the registered file (:func:`registered_spec_path`). Past that only
``name`` and ``description`` change: every grant, tool, server, hook and prompt is
the app's own, so permission and trust behave exactly as they do for the
registered spec. Discovery leaves the copies out of every roster
(``agent_discovery.list_agents``). The app's deregistration, and an update that
drops an agent from its manifest, remove them (:func:`remove_app_agent_launches`).
The name is fixed per home, app and agent, and the file is rewritten only when its
content changed, so concurrent sessions of one app agent share one copy.

KAS takes no agent name, so it gets no copy. Its projection of a session bound to an
app's own agent (:func:`bind_session_app_agent`) reads the registered file by its path
instead of resolving the name (:func:`read_bound_app_spec`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
from collections.abc import Collection
from pathlib import Path
from typing import Any

from kiro_crew.agent_spec_format import APP_AGENT_LAUNCH_PREFIX

logger = logging.getLogger(__name__)

#: Opening of every launch copy's ``description``: how this module tells its own
#: file from a user's. ``description`` is a first-class kiro-cli spec field, so it
#: survives ``deny_unknown_fields`` where a custom key would not.
OWNER_MARKER = "Kiro Crew app agent launch copy"

#: Hex digits of the app tag, the prefix :func:`remove_app_agent_launches`
#: matches; it must not collide across the apps installed on one host.
_TAG_HEX = 8

#: Hex digits of the agent tag. A plain hash: the name need not be secret, because
#: kiro-cli is started where no checkout spec can sit (:func:`launch_process_cwd`).
_AGENT_TAG_HEX = 24

#: The data-home directory that holds :data:`_LAUNCH_CWD_NAME`. Read-only in the Linux
#: sandbox (``sandbox._CREW_READONLY_LEAVES``), precreated before every spawn so the seal
#: has a target, and write-protected from the file tools. Spelled here and in
#: ``sandbox_leaves``; ``test_app_agent_launch_copy`` pins the two equal.
_LAUNCH_DIR_LEAF = "app-agent-launch"
#: The empty directory a launch copy's kiro-cli process starts in.
_LAUNCH_CWD_NAME = "cwd"

#: Serializes a publication against a removal in this process.
_LAUNCH_FILE_LOCK = threading.Lock()

#: Guards :data:`_SEARCH_COPIES` and :data:`_LAUNCH_SOURCES` alone, and is never held
#: across file I/O, so the event loop may read them without waiting on a publication.
_LAUNCH_META_LOCK = threading.Lock()

#: Launch copies whose published view is a skill-search agent, by name: the
#: projection's own verdict (``project_agent_view``), kept so the spawn holds the
#: copy exactly as the projection would have. Read under :data:`_LAUNCH_META_LOCK`.
_SEARCH_COPIES: set[str] = set()

#: Launch copy name -> the registered spec's file stem (``<app>--<agent>``), recorded
#: on publication. The gateway's MCP overlay keeps that stem, so a copy's pooled stubs
#: are found under it rather than by the bare agent name, which several apps' agents
#: and a user's own spec can share. Read under :data:`_LAUNCH_META_LOCK`.
_LAUNCH_SOURCES: dict[str, str] = {}


class AppAgentLaunchError(RuntimeError):
    """The launch copy could not be produced; the app-owned turn must not run.

    ``code`` is stable and logged; ``detail`` is the sentence the chat shows.

    * ``not_installed`` -- the app's registered spec is absent or does not declare
      the agent.
    * ``unverifiable`` -- the registered entry, the file at the copy's path, or the
      launch directory could not be used safely (a link, a protected target, an
      unreadable file, a launch directory that is not empty).
    * ``foreign`` -- a file at the copy's path is not this module's.
    * ``shadowed`` -- another user-level spec declares the copy's name.
    * ``no_skill_view`` -- the registered spec cannot get a native skill view (its
      ``kirocrew-core`` or ``skill_search`` is restricted), as any Kiro spawn refuses.
    * ``write_failed`` -- the copy could not be written.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{detail} [{code}]")
        self.code = code
        self.detail = detail


def _tag(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_TAG_HEX]


def _app_prefix(app_name: str) -> str:
    return f"{APP_AGENT_LAUNCH_PREFIX}{_tag(app_name)}-"


def launch_process_cwd() -> Path:
    """The empty directory kiro-cli is started in when it runs a launch copy.

    kiro-cli finds project agents in its PROCESS working directory, not the ACP
    session's; its tools, ``file://`` resources and ``AGENTS.md`` resolve against the
    session's, while the MCP servers it starts itself begin here. So a process started
    here reads no checkout's ``.kiro/agents`` while its session still works in the
    checkout. Created owner-only on first use; refuses
    a link or a file at either name, and a directory that is not empty, so nothing
    placed here before the spawn can be read as a spec.
    """
    from kiro_crew import platform_compat
    from kiro_crew.config.paths import data_home

    leaf = data_home() / _LAUNCH_DIR_LEAF
    path = leaf / _LAUNCH_CWD_NAME
    try:
        for directory in (leaf, path):
            # Judged before the owner-only narrowing, which follows a link.
            if os.path.lexists(directory) and not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise OSError("not a directory")
            platform_compat.make_owner_only_dir(directory)
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise OSError("not a directory")
        entries = os.listdir(path)
    except OSError as exc:
        raise AppAgentLaunchError("unverifiable", f"could not use {path}: {exc}") from exc
    if entries:
        raise AppAgentLaunchError(
            "unverifiable", f"{path} must be empty; remove what is in it and retry."
        )
    return path


def launch_cwd_roots(process_cwd: str) -> tuple[str, ...]:
    """Both spellings of *process_cwd*, as given and resolved, for :func:`map_process_cwd_paths`.

    kiro-cli may report the directory as its own ``getcwd()`` does, which resolves a
    symlinked data home. Blocking (``realpath``): call it once, off the event loop.
    """
    given = os.path.normpath(process_cwd)
    return tuple(dict.fromkeys((given, os.path.normpath(os.path.realpath(process_cwd)))))


def map_process_cwd_paths(update: object, roots: tuple[str, ...], session_cwd: str) -> None:
    """Point the paths of one tool call at what the tool touched, in place.

    *roots* are the launch directory's spellings (:func:`launch_cwd_roots`). String
    work only (``$VAR`` and ``~`` expansion read the environment), so the reader loop can call it
    on every frame.

    kiro-cli runs the tool in the session's directory but reports the ``diff`` path
    resolved and normalised against its PROCESS directory: ``../sib/x`` comes back as
    ``<launch parent>/sib/x``, naming neither the file written nor anything the edit
    gates should judge. ``locations`` carries the spelling the tool was given, so:

    * every ``locations`` path is resolved as the tool resolved it: ``$VAR`` and ``~``
      expanded,
      a relative one joined to *session_cwd*;
    * a ``diff`` path takes the ONE spelling ``locations`` names, resolved the same
      way, whatever kiro-cli reported;
    * with no ``locations``, a ``diff`` path inside one of *roots* moves to the same
      place under *session_cwd*;
    * any other ``diff`` path -- several spellings, several diff blocks (the gates
      judge only the first), or no spelling and outside *roots* -- is
      made relative: the edit gates treat a relative diff path as unanchored and
      refuse the call rather than judge a file the tool may not have touched.
    """
    if not (roots and session_cwd) or not isinstance(update, dict):
        return

    def resolved(spelling: str) -> str:
        path = os.path.expanduser(os.path.expandvars(spelling))
        return os.path.normpath(path if os.path.isabs(path) else os.path.join(session_cwd, path))

    def under_root(path: str) -> str | None:
        for root in roots:
            if path == root:
                return session_cwd
            if path.startswith(root + os.sep):
                return os.path.join(session_cwd, os.path.relpath(path, root))
        return None

    locations = update.get("locations")
    spellings: list[str] = []
    if isinstance(locations, list):
        for item in locations:
            if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]:
                spellings.append(item["path"])
                item["path"] = resolved(item["path"])
    targets = {resolved(spelling) for spelling in spellings}
    content = update.get("content")
    if not isinstance(content, list):
        return
    diffs = [
        item
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "diff"
        and isinstance(item.get("path"), str)
        and item["path"]
    ]
    for item in diffs:
        value = item["path"]
        if len(diffs) == 1 and len(targets) == 1:
            item["path"] = next(iter(targets))
            continue
        mapped = None if targets or len(diffs) > 1 else under_root(os.path.normpath(value))
        if mapped is not None:
            item["path"] = mapped
        else:
            item["path"] = os.path.join("unanchored", os.path.basename(value) or "path")


def app_agent_launch_name(app_name: str, agent: str) -> str:
    """The reserved name *app_name*'s agent *agent* launches under on kiro-cli."""
    agent_tag = hashlib.sha256(f"{app_name}/{agent}".encode("utf-8")).hexdigest()
    return f"{_app_prefix(app_name)}{agent_tag[:_AGENT_TAG_HEX]}"


def is_owned_launch_spec(data: object) -> bool:
    """Whether a parsed spec carries this module's owner marker."""
    if not isinstance(data, dict):
        return False
    description = data.get("description")
    return isinstance(description, str) and description.startswith(OWNER_MARKER)


def registered_spec_path(app_name: str, agent: str, agents_dir: Path | None = None) -> Path:
    """Where *app_name*'s agent *agent* is registered: ``<agents dir>/<app>--<agent>.json``.

    The one file the launch copy and the copy's skill scope are read from, by path,
    never by a name lookup that could reach a checkout's same-named spec.
    """
    from kiro_crew.apps import bridges

    directory = agents_dir if agents_dir is not None else bridges._kiro_agents_dir()
    return directory / (bridges._safe_link_name(bridges._namespace(app_name, agent)) + ".json")


def _read_registered_spec(app_name: str, agent: str, agents_dir: Path) -> dict[str, Any]:
    """The app's registered ``<app>--<agent>.json``, proven to declare *agent*."""
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.apps.manager import _screened_spec_path

    path = registered_spec_path(app_name, agent, agents_dir)
    # Screened before the reader resolves it, so a link to a protected location or
    # an untrusted share is refused without being followed.
    screened = _screened_spec_path(path)
    if screened is None:
        raise AppAgentLaunchError(
            "unverifiable",
            f"the agent entry at {path} could not be verified as the app's own agent. "
            "Fix or remove that entry, then run the command again from the command palette.",
        )
    data = _read_agent_spec(screened, operation="app_agent_launch", source="unknown")
    if not (isinstance(data, dict) and data.get("name", path.stem) == agent):
        raise AppAgentLaunchError(
            "not_installed", f"the app's agent {agent!r} is not registered at {path}."
        )
    return data


def _read_current(target: Path) -> dict[str, Any] | None:
    """The copy now at *target*, ``None`` when absent; raises when it cannot be judged."""
    from kiro_crew.agent_discovery import _read_agent_spec

    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AppAgentLaunchError("unverifiable", f"could not inspect {target}: {exc}") from exc
    # This module only ever writes regular files; a link or anything else is not ours.
    if not stat.S_ISREG(info.st_mode):
        raise AppAgentLaunchError(
            "foreign", f"{target} is not a regular file; refusing to write through it."
        )
    data = _read_agent_spec(target, operation="app_agent_launch", source="unknown")
    if not isinstance(data, dict) or not is_owned_launch_spec(data):
        raise AppAgentLaunchError(
            "foreign",
            f"{target} exists and was not written by Kiro Crew; it is not overwritten. "
            "Remove it, then run the command again from the command palette.",
        )
    return data


def is_search_launch_copy(name: str) -> bool:
    """Whether the launch copy *name* was published as a skill-search agent's view."""
    with _LAUNCH_META_LOCK:
        return name in _SEARCH_COPIES


def _skill_view(
    app_name: str, agent: str, spec: dict[str, Any], source: Path
) -> tuple[dict[str, Any], bool]:
    """*spec* as the native skill view every Kiro launch runs (``acp.skill_projection``).

    ``skill://`` resources are dropped and the agent's skills are served through
    ``kirocrew-core``'s ``skill_search``, scoped to this registered file; every other
    field is the app's own. One copy serves every workspace, so the view is built as
    if the workspace lends inherited skill trees: the search tool is then present
    for an unmapped agent too, and a workspace that lends none leaves it with nothing
    to find. A spec the projection refuses a view for refuses the turn, with the
    projection's own sentence.
    """
    from kiro_crew.agent_sdk.drivers.acp import native_skill_view
    from kiro_crew.config.paths import data_home

    view, search, refusal = native_skill_view(
        spec, source, agent, crew_home_id=data_home().absolute().as_posix()
    )
    if view is None:
        raise AppAgentLaunchError(
            "no_skill_view",
            f"the app's agent {app_name}/{agent} cannot get a skill view: {refusal}",
        )
    return view, search


def publish_app_agent_launch(app_name: str, agent: str) -> str:
    """Copy *app_name*'s registered spec for *agent* to its launch name; return the name.

    Raises :class:`AppAgentLaunchError` on every failure and never returns a name
    whose file is not on disk. Writes only when the content changed. Blocking file
    I/O: call it off the event loop.
    """
    from kiro_crew.agent import _atomic_json_write
    from kiro_crew.agent_sdk import agents_dir_competing_claim
    from kiro_crew.apps import bridges

    name = app_agent_launch_name(app_name, agent)
    agents_dir = bridges._kiro_agents_dir()
    target = agents_dir / f"{name}.json"
    with _LAUNCH_FILE_LOCK:
        source = _read_registered_spec(app_name, agent, agents_dir)
        launch, search = _skill_view(
            app_name, agent, source, registered_spec_path(app_name, agent, agents_dir)
        )
        launch["name"] = name
        launch["description"] = (
            f"{OWNER_MARKER} of {app_name}/{agent}; written before each turn, do not edit."
        )
        current = _read_current(target)
        claim = agents_dir_competing_claim(name, agents_dir, target)
        if claim is not None:
            if claim.kind == "claimed":
                raise AppAgentLaunchError(
                    "shadowed",
                    f"{Path(claim.detail).name} in {agents_dir} also declares the name "
                    f"{name!r}, which Kiro Crew reserves for this app's agent. Remove it, "
                    "then run the command again from the command palette.",
                )
            raise AppAgentLaunchError("unverifiable", claim.detail)
        if current != launch:
            try:
                _atomic_json_write(target, launch)
            except Exception as exc:  # noqa: BLE001 -- every write failure refuses the turn
                raise AppAgentLaunchError(
                    "write_failed", f"could not write {target}: {exc}"
                ) from exc
            logger.info("App agent launch copy written: %s", target)
        stem = registered_spec_path(app_name, agent, agents_dir).stem
        with _LAUNCH_META_LOCK:
            if search:
                _SEARCH_COPIES.add(name)
            else:
                _SEARCH_COPIES.discard(name)
            _LAUNCH_SOURCES[name] = stem
    return name


def launch_source_stem(name: str) -> str | None:
    """The registered spec's file stem the launch copy *name* was published from.

    ``None`` for a name this process has not published.
    """
    with _LAUNCH_META_LOCK:
        return _LAUNCH_SOURCES.get(name)


def read_launch_copy(name: str, agents_dir: Path | None = None) -> dict[str, Any]:
    """The launch copy *name*, read by its user-level path, never by a name lookup.

    Raises :class:`AppAgentLaunchError` (``unverifiable``) when it is not this module's
    regular file: a session must not judge a spec other than the one kiro-cli runs.
    """
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.apps import bridges

    directory = agents_dir if agents_dir is not None else bridges._kiro_agents_dir()
    path = directory / f"{name}.json"
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError("not a regular file")
    except OSError as exc:
        raise AppAgentLaunchError("unverifiable", f"could not read {path}: {exc}") from exc
    data = _read_agent_spec(path, operation="app_agent_launch", source="unknown")
    if not isinstance(data, dict) or not is_owned_launch_spec(data):
        raise AppAgentLaunchError("unverifiable", f"{path} is not a launch copy.")
    return data


def remove_app_agent_launches(app_name: str, *, keep_agents: Collection[str] = ()) -> int:
    """Remove the launch copies *app_name*'s agents left in the agents directory.

    The copies of the agents named in *keep_agents* stay. Only regular files
    carrying :data:`OWNER_MARKER` are removed. Best-effort: an unreadable
    directory or file is skipped.
    """
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.apps import bridges

    agents_dir = bridges._kiro_agents_dir()
    prefix = _app_prefix(app_name)
    kept = {f"{app_agent_launch_name(app_name, agent)}.json" for agent in keep_agents}
    removed = 0
    with _LAUNCH_FILE_LOCK:
        try:
            entries = [p for p in agents_dir.iterdir() if p.name.startswith(prefix)]
        except OSError:
            return 0
        for path in entries:
            if path.suffix != ".json" or path.name in kept:
                continue
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                data = _read_agent_spec(path, operation="app_agent_launch", source="unknown")
                if not is_owned_launch_spec(data):
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
    if removed:
        logger.info("Removed %d app agent launch copy(ies) for app %s", removed, app_name)
    return removed


# ── KAS: the projection bound to the registered file ────────────────────────

#: Session key -> (app, agent) of the app's own agent the session runs as. The gateway
#: sets it before an admitted app-owned turn claims its session, and removes it before
#: any other turn of the key claims one (a slot switched to another agent, or a new
#: slot created under a deleted one's name), so only an app-owned turn's session is
#: ever bound. Read by the KAS projection (``acp.harness.kas``). Process-local, like
#: the sessions it binds. Read under :data:`_KAS_BINDING_LOCK`.
_KAS_BINDINGS: dict[str, tuple[str, str]] = {}
_KAS_BINDING_LOCK = threading.Lock()


def bind_session_app_agent(session_key: str, app_name: str, agent: str) -> None:
    """Bind *session_key*'s agent *agent* to *app_name*'s registered spec.

    KAS takes no agent name: Crew reads the spec it sends. A name the user level
    declares resolves there whatever the checkout holds, but a name the user level
    stops declaring -- the app switched off between the turn's check and the session
    start -- would fall back to the checkout. A bound session's projection reads the
    registered file by its path instead, and refuses when it is gone
    (:func:`read_bound_app_spec`).
    """
    if not (session_key and app_name and agent):
        return
    with _KAS_BINDING_LOCK:
        _KAS_BINDINGS[session_key] = (app_name, agent)


def unbind_session_app_agent(session_key: str) -> None:
    """Remove *session_key*'s binding, for a turn of the key not bound to an app's agent."""
    if not session_key:
        return
    with _KAS_BINDING_LOCK:
        _KAS_BINDINGS.pop(session_key, None)


def read_bound_app_spec(session_key: str, agent: str) -> dict[str, Any] | None:
    """The registered spec *session_key*'s *agent* is bound to, or ``None`` when unbound.

    Read by its path through the screened, hardened reader and proven to declare
    *agent*, never looked up by name. Raises :class:`AppAgentLaunchError` when the
    bound file is missing, unverifiable or declares another agent: the session must
    not start rather than run a checkout's same-named spec. Does filesystem I/O.
    """
    if not (session_key and agent):
        return None
    with _KAS_BINDING_LOCK:
        bound = _KAS_BINDINGS.get(session_key)
    if bound is None or bound[1] != agent:
        return None
    from kiro_crew.apps import bridges

    app_name, _ = bound
    return _read_registered_spec(app_name, agent, bridges._kiro_agents_dir())

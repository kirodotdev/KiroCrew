"""Reading installed apps: the app list, one app's row, enablement, and agent ownership.

``list_apps``, ``get_app``, ``app_enabled_state`` / ``is_app_enabled`` / ``app_disabled``,
the source and provenance setters, and which installed app owns an agent and whether a
session bound to it may run (``owning_app_for_agent``, ``app_command_agent_refusal``).

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import functools
import json
import logging
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kiro_crew.apps.execution import shipped_builtin_app_root
from kiro_crew.apps.manager_builtins import detect_orphaned_builtins
from kiro_crew.apps.manager_records import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    InstalledApp,
    _approved_grants_row,
    _read_installed,
    _write_installed,
    app_dir,
    apps_dir,
    hold_app_record_lock,
    holds_app_record_locks,
)
from kiro_crew.apps.manifest import AppManifest, contain_command_agents
from kiro_crew.platform_compat import is_link_or_junction

logger = logging.getLogger("kiro_crew.apps.manager")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _app_registered_agent(app_name: str, agent: str) -> bool:
    """Whether registration wrote *app_name*'s own agent *agent* into the agents dir.

    True only when :func:`_app_agent_registration` reports ``"ok"``.
    """
    return _app_agent_registration(app_name, agent)[0] == "ok"


def _app_agent_registration(app_name: str, agent: str) -> tuple[str, tuple[str, ...]]:
    """Whether *app_name*'s own agent *agent* is registered, and the clashing files.

    The status is ``"ok"``, ``"missing"``, ``"ambiguous"`` or ``"unverifiable"``.
    The second item names a competing file for ``"ambiguous"`` and carries the
    refusal reason for ``"unverifiable"``.

    Judged from the namespaced config ``_register_agents`` writes (``app--agent.json``)
    and the bare ``name`` it carries, so every skip reason registration has -- an
    unresolved template, an unsafe name, a malformed spec -- counts, including ones
    added later. Anything unreadable reads as not registered.

    Read through the hardened agent-spec reader, never a bare ``read_text``: the agents
    dir is user-writable, so this file may be a symlink to a sensitive path, and the
    reader refuses (and audits) that before any byte is read.

    The name must also be UNIQUE in the agents dir. Sessions select an agent by its
    bare declared ``name``, so if a host spec (or another app's) declares the same
    name, which file a session resolves to is not this app's to decide, and the
    app-authored prompt could run with that other spec's tools and auto-approvals.
    An ambiguous name is refused rather than guessed at.

    This listing checks only the USER-level agents dir. A checkout's spec of the
    same name never runs as the app's agent: the session the command opens carries
    ``app_agent_owner``, and its turns launch the registered file
    (:func:`app_command_agent_refusal`).
    """
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.agent_sdk import agents_dir_competing_claim
    from kiro_crew.apps import bridges  # bridges imports this module

    agents_dir = bridges._kiro_agents_dir()
    path = agents_dir / (bridges._safe_link_name(bridges._namespace(app_name, agent)) + ".json")
    # Screened before the reader resolves it: the reader's ``resolve()`` would follow
    # a link to wherever it points (on Windows, a UNC share is probed by the resolve
    # itself), so the link's own target is checked first and a refused one is reported
    # rather than followed.
    screened = _screened_spec_path(path)
    if screened is None:
        return "unverifiable", (
            f"the agent entry at {path} could not be verified, so Kiro Crew cannot "
            "confirm it is the app's agent. Fix or remove that entry, then run the "
            "command again from the command palette.",
        )
    data = _read_agent_spec(screened, operation="list_apps_command_agent", source="unknown")
    if not (isinstance(data, dict) and data.get("name", path.stem) == agent):
        return "missing", ()
    claim = agents_dir_competing_claim(agent, agents_dir, path)
    if claim is None:
        return "ok", ()
    if claim.kind == "claimed":
        return "ambiguous", (Path(claim.detail).name,)
    return "unverifiable", (claim.detail,)


def _app_display_name(app_name: str) -> str:
    """The ``displayName`` the dashboard shows for *app_name*, or the id when unreadable.

    ``app.json`` is app-writable, so it is read through the hardened
    ``_read_agent_spec`` reader: a symlink whose resolved target is sensitive is
    refused and audited before any byte is read, and a refusal falls back to the id.
    """
    from kiro_crew.agent_discovery import _read_agent_spec

    try:
        screened = _screened_spec_path(app_dir(app_name) / APP_MANIFEST_FILENAME)
        if screened is None:
            return app_name
        data = _read_agent_spec(screened, operation="app_command_agent_label", source="unknown")
    except Exception:  # noqa: BLE001 - a label must never fail the refusal it labels
        return app_name
    label = data.get("displayName") if isinstance(data, dict) else None
    return label if isinstance(label, str) and label.strip() else app_name


def _screened_spec_path(path: Path) -> Path | None:
    """*path* as the screened canonical path to read, or None when the screen refuses.

    :func:`kiro_crew.hooks.validate_file_path` reads a link's own target before
    anything resolves it, so a link into a protected location or an untrusted share
    is refused without being followed; the hardened reader then reads the canonical
    path the screen returned.
    """
    from kiro_crew import hooks

    try:
        screened = hooks.validate_file_path(str(path))
    except (OSError, ValueError):
        return None
    return None if screened is None else Path(screened)


def app_agent_backend_refusal(owner: str) -> str:
    """Why a session bound to app *owner*'s agent cannot run on its backend.

    Only kiro-cli (``--agent``) and KAS (the spec Crew reads and sends) launch an
    agent from its spec; every other backend would run the app-authored prompt
    without the app's spec, its tools or its approvals. App-owned sessions are
    therefore limited to those two, and a session on any other backend is refused
    rather than given a per-backend way to load the spec. Reads the app's manifest
    for its label; call it off the event loop.
    """
    app = _app_display_name(owner)
    return (
        f"{app} can't run here: its agent runs only on the KAS or Kiro backend, and "
        "this chat runs on another one. Set the agent backend (agent.acp_backend) to "
        "KAS or Kiro, then run the command again from the command palette."
    )


def app_agent_launch_refusal(owner: str, detail: str) -> str:
    """Why app *owner*'s agent cannot launch: its launch copy failed with *detail*.

    Reads the app's manifest for its label; call it off the event loop.
    """
    return f"{_app_display_name(owner)} can't run: {detail}"


def app_command_agent_refusal(owner: str, agent: str) -> str | None:
    """Why a session bound to app *owner*'s agent must not run as *agent* now, or None.

    A turn on a session a palette command opened for the app's own agent runs only
    while the app's materialized ``<app>--<agent>.json`` is installed and is the only
    user-level spec declaring *agent*, and *agent* is not a name Kiro Crew reserves.
    No checkout is consulted: neither allowed backend lets a checkout's spec of that
    name stand in for the app's. On kiro-cli the turn launches the app's launch copy
    from an empty directory (:mod:`kiro_crew.apps.agent_launch`); on KAS a name the
    user level declares resolves to the user-level spec whatever the checkout holds
    (``acp.harness.kas.resolve_projected_spec``). On both, the turn pins the
    session's skill scope and injected prompt to the registered file
    (:mod:`kiro_crew.skill_scope_pin`). Does filesystem I/O; call it off the event
    loop.
    """
    from kiro_crew.agent_spec_format import APP_AGENT_LAUNCH_PREFIX, NATIVE_SKILL_ALIAS_PREFIX

    app = _app_display_name(owner)
    # A skill-view name is never launched as itself: the spawn maps it to the host
    # agent its view was built from, even after the view file is pruned, so it can
    # never mean the app's own spec.
    # A launch-copy name is reserved the same way: it names a file Crew writes.
    if agent.startswith(NATIVE_SKILL_ALIAS_PREFIX) or agent.startswith(APP_AGENT_LAUNCH_PREFIX):
        return (
            f"{app} can't run: it has a setup problem its author needs to fix "
            f'(its agent "{agent}" uses a name Kiro Crew reserves). '
            f"Ask {app}'s author to rename that agent."
        )
    registration, others = _app_agent_registration(owner, agent)
    if registration == "unverifiable":
        return f"{app} can't run: {others[0]}"
    if registration == "ambiguous":
        where = ", ".join(others) if others else f'the other agent named "{agent}"'
        return (
            f'{app} can\'t run: another agent in ~/.kiro/agents is also named "{agent}", '
            f"so {app}'s agent can't be told apart from it. Rename or remove {where} "
            "in ~/.kiro/agents, then run the command again from the command palette."
        )
    if registration != "ok":
        return (
            f"{app} can't run: its agent \"{agent}\" isn't installed. Re-enable {app} "
            "in Apps \u203a Library, then run the command again from the command palette."
        )
    return None


@dataclass(frozen=True)
class AgentOwner:
    """The installed app whose manifest declares an agent, and whether it is switched on."""

    app: str
    label: str
    enabled: bool


def _owner_json(path: Path) -> dict[str, Any] | None:
    """*path* parsed as a JSON object for the owner lookup, or None when refused.

    Every file the lookup reads -- an app's ``installed.json``, its ``app.json``
    and its agent specs -- sits in a tree the app can write, so each is screened
    before anything follows a link (:func:`_screened_spec_path`) and read through
    the hardened, audited agent-spec reader. A link into a protected location, an
    oversized file and a document that is not an object all read as None.
    """
    from kiro_crew.agent_discovery import _read_agent_spec

    screened = _screened_spec_path(path)
    if screened is None:
        return None
    data = _read_agent_spec(screened, operation="app_agent_owner", source="unknown")
    return data if isinstance(data, dict) else None


def _app_agent_names(app_name: str, manifest: AppManifest, app_root: Path) -> set[str]:
    """Every name *app_name*'s own agents are selected by, for attributing a binding.

    Each agent's declared ``name`` (its file stem when it declares none), plus the
    ``<app>--<name>`` stem registration writes the spec under, so a binding that
    names either is recognised. Attribution only, never an authorization: a
    templated spec still names its agent here, unlike the command-agent check in
    :mod:`kiro_crew.apps.manifest`. A path escaping the app root, and a spec the
    screened read refuses (:func:`_owner_json`), contribute nothing.
    """
    from kiro_crew.apps import bridges  # bridges imports this module
    from kiro_crew.apps.manifest import _app_path_refusal, screen_app_path

    names: set[str] = set()
    screened_root = screen_app_path(app_root) if manifest.agents else None
    for rel in manifest.agents:
        rel = str(rel)
        if not rel or _app_path_refusal(rel, app_root, screened_root):
            continue
        data = _owner_json(app_root / rel)
        if data is None:
            continue
        name = data.get("name", Path(rel.replace("\\", "/")).stem)
        if isinstance(name, str) and name:
            names.add(name)
            names.add(bridges._safe_link_name(bridges._namespace(app_name, name)))
    return names


def owning_app_for_agent(agent: str) -> AgentOwner | None:
    """The installed app that declares *agent*, enabled or not, or None when none does.

    Read from each installed app's own manifest and agent files, never from the
    agents directory: disabling an app removes the specs it registered there, so
    that directory cannot say who owned a name absent from it. The manifest and the
    agent files come from where registration reads them: a shipped builtin's
    immutable package root, every other app's installed tree
    (``bridges._registration_source``). Apps are visited in name order and the
    first that declares the agent answers. Every read is screened
    (:func:`_owner_json`). Blocking file I/O; call it off the event loop. Never
    raises: an app that cannot be read is skipped.
    """
    if not agent:
        return None
    # No ``is_dir`` filter: it would follow an entry that is a link before the
    # screen sees it. Each entry is screened through its ``installed.json`` read
    # instead, which refuses a link to a protected location or an untrusted share
    # and reads nothing for an entry that is not an app directory.
    try:
        entries = sorted(apps_dir().iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            raw_meta = _owner_json(entry / INSTALLED_META_FILENAME)
            if raw_meta is None:
                continue
            root = shipped_builtin_app_root(entry.name) or entry
            raw_manifest = _owner_json(root / APP_MANIFEST_FILENAME)
            if raw_manifest is None:
                continue
            manifest = AppManifest.from_dict(raw_manifest)
            if not manifest.agents or agent not in _app_agent_names(entry.name, manifest, root):
                continue
            label = manifest.displayName.strip() or entry.name
            enabled = bool(InstalledApp.from_dict(raw_meta).enabled)
            return AgentOwner(entry.name, label, enabled)
        except Exception:  # noqa: BLE001 - one unreadable app must not hide the others
            logger.debug("agent owner lookup skipped app %s", entry.name, exc_info=True)
    return None


def missing_agent_reason(agent: str, *, member: str = "") -> str:
    """Why a session cannot start on *agent*, whose definition is gone, and the fix.

    *member* is the Crew Member the session belongs to, when there is one; the
    reason then names the member rather than the agent. Blocking file I/O through
    :func:`owning_app_for_agent`; call it off the event loop.
    """
    owner = owning_app_for_agent(agent)
    subject = f'Crew member "{member}"' if member else f'The agent "{agent}"'
    target = "this crew member" if member else "it"
    if owner is None:
        if member:
            return (
                f'{subject} can\'t start: its agent template "{agent}" is missing. '
                "Pick another template on its Agent Template tab."
            )
        return f"{subject} can't start: its agent definition is missing."
    if not owner.enabled:
        return (
            f"{subject} comes from the {owner.label} app, which is disabled. "
            f"Enable {owner.label} in Apps \u203a Library to use {target}."
        )
    return (
        f"{subject} comes from the {owner.label} app, but its agent definition "
        f'"{agent}" is missing. Disable and re-enable {owner.label} in Apps \u203a Library '
        f"to register it again."
    )


def list_apps() -> list[dict[str, Any]]:
    """Return metadata for all installed apps."""
    root = apps_dir()
    if not root.is_dir():
        return []
    orphaned_set = detect_orphaned_builtins()
    result: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        meta = _read_installed(entry.name)
        if not meta:
            continue
        # Also load manifest for full info
        manifest_path = entry / APP_MANIFEST_FILENAME
        manifest_data: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = AppManifest.from_json_file(manifest_path)
                # A command may only name one of this app's own agents. Re-checked here,
                # against the files, because a self-managed app's app.json never passes
                # through install-time validation.
                manifest_data = contain_command_agents(
                    manifest.to_dict(),
                    manifest.agents,
                    entry,
                    is_registered=functools.partial(_app_registered_agent, entry.name),
                )
                # For self-managed apps, the app may update its own
                # app.json without going through update_app().  Reflect
                # the manifest version in the RETURNED metadata only, so
                # the dashboard shows the real version. Deliberately no
                # write-back here: list_apps() must stay read-only —
                # callers run it concurrently from worker threads, and a
                # persisted read-modify-write of installed.json from a
                # listing would race real mutators (install/enable/
                # register) and silently overwrite their fields. get_app
                # is read-only for the same reason; the record's version is
                # rewritten by update_app / register_external_app.
                if (
                    meta.lifecycle == "app"
                    and manifest.version
                    and manifest.version != meta.version
                ):
                    meta.version = manifest.version
            except Exception:
                pass
        app_info: dict[str, Any] = {
            **meta.to_dict(),
            **_approved_grants_row(entry.name),
            "manifest": manifest_data,
        }
        # Include migratedTo if non-empty
        if meta.migratedTo:
            app_info["migratedTo"] = meta.migratedTo
        # Mark orphaned builtins
        if entry.name in orphaned_set:
            app_info["orphaned"] = True
        result.append(app_info)
    return result


def get_app(name: str) -> dict[str, Any] | None:
    """Return full metadata for a single installed app, or None."""
    meta = _read_installed(name)
    if not meta:
        return None
    manifest_path = app_dir(name) / APP_MANIFEST_FILENAME
    manifest_data: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = AppManifest.from_json_file(manifest_path)
            manifest_data = manifest.to_dict()
            # Reflect a self-managed app's own manifest version in the RETURNED
            # row only, as list_apps does. The record here was read before the
            # manifest, so writing it back would overwrite whatever a
            # registration landing between the two reads stored -- an approval,
            # a disable-for-consent -- with the stale snapshot.
            if meta.lifecycle == "app" and manifest.version and manifest.version != meta.version:
                meta.version = manifest.version
        except Exception:
            pass
    return {**meta.to_dict(), **_approved_grants_row(name), "manifest": manifest_data}


def _absence_is_genuine(meta_path: Path) -> bool:
    """Whether nothing at *meta_path* really means nothing is there.

    The nearest ancestor that exists has to be a DIRECTORY. If something else
    occupies part of the path, the file cannot exist for a reason that is NOT
    absence, and that must not read as "the app was uninstalled".

    Separated from the exception class deliberately: POSIX reports this as
    ``NotADirectoryError`` while Windows raises ``FileNotFoundError``, so the class
    identifies the platform rather than the condition.

    ``is_link_or_junction`` is checked for the same reason, one predicate over:
    ``is_symlink`` is False for a Windows directory junction, so a DANGLING junction
    would present as ``is_dir=False, exists=False, is_symlink=False`` and this walk
    would step over the thing occupying the path. Something IS at that component, so
    the answer is unknown, not absence.

    Walks upward because the non-directory component need not be the immediate
    parent. Terminates: the filesystem root exists and is a directory. An ancestor
    that cannot be inspected at all is treated as not-genuine, which is the same
    fail-to-unknown direction as the rest of this function.
    """
    for ancestor in meta_path.parents:
        try:
            if ancestor.is_dir():
                return True
            if ancestor.exists() or ancestor.is_symlink() or is_link_or_junction(ancestor):
                return False
        except OSError:
            return False
    return True


def app_enabled_state(name: str) -> bool | None:
    """Tri-state enablement: True, False, or None when the metadata cannot be READ.

    :func:`is_app_enabled` collapses "not installed" and "unreadable" into a single
    False, because :func:`_read_installed` returns None for both. That is the right
    answer for a caller deciding whether to ACT on an app, and the wrong one for a caller
    deciding whether to DELETE its files: a transient read fault (EMFILE, EIO, a Windows
    AV lock) would be indistinguishable from a deliberate disable, and the deletion is
    unrecoverable. This keeps the two apart.

    A missing metadata file is a definite False — the app is not installed — not a
    failure to read one, and NOTHING ELSE is. Leading with ``Path.is_file()`` broke
    that: it answers a silent False for five path shapes that are not absence, all
    verified against this interpreter — a dangling symlink, a directory in the file's
    place, a fifo in its place, a symlink loop (ELOOP), and a non-directory parent
    component (ENOTDIR). Only a genuine ``stat`` fault such as EACCES was reported
    correctly, because ``is_file`` re-raises that and the handler below turns it into
    None.

    Absence is decided from the path's SHAPE, never from the exception class, because
    one condition does not produce one class across platforms: a non-directory parent
    component raises ``NotADirectoryError`` (ENOTDIR) on POSIX but
    ``FileNotFoundError`` on Windows, which maps ERROR_PATH_NOT_FOUND to ENOENT — the
    same class a genuinely missing file raises. Keying "definitely not installed" on
    ``FileNotFoundError`` therefore told the truth on Linux and not on Windows, where
    a wrong-shape parent still read as a deliberate uninstall. See
    :func:`_absence_is_genuine`; ``_spawn_exec_shim`` records the same lesson for
    ``chdir`` ("the errno is not the thing to key on").

    The cost of the wrong answer is asymmetric, which is why the callers that already
    respect the tri-state are the ones that make this worth fixing. ``apps.backend``
    reads it before DELETING materialized resources -- ``_drop_disabled_app_resources``
    on a False, ``_undo_promotion_of_disabled_app`` likewise -- and its own comments
    say a None "must not be collapsed into disabled" and is retried instead. That
    contract was already written correctly; it was this function that did not honour
    it, so a dangling symlink or a directory in the metadata's place deleted an app's
    agent files.

    ``apps.hook_reconcile`` consumes it too, and only because this fix put it there.
    Its unattended 15s teardown decides "gone" from ``get_app`` -> ``_read_installed``,
    which has the same ``Path.is_file()`` collapse and additionally folds a corrupt
    JSON body into None -- so before this change every one of those shapes unloaded a
    healthy app's routes and modules on the next tick. That reader has 24 callers and
    ``get_app``/``list_apps`` 63, so it is not made tri-state here; the reconciler
    confirms absence through THIS function instead and defers on unknown.
    """
    meta_path = app_dir(name) / INSTALLED_META_FILENAME
    try:
        try:
            st = meta_path.stat()
        except FileNotFoundError:
            # A dangling link is a path that EXISTS and whose target cannot be
            # seen, which is not the same as nothing being there. Both predicates
            # are asked because is_symlink is False for a Windows junction, and
            # _absence_is_genuine below walks the PARENTS -- never meta_path itself.
            if meta_path.is_symlink() or is_link_or_junction(meta_path):
                logger.warning("Metadata path %s is a dangling link or junction", meta_path)
                return None
            # This class is reached for TWO different conditions depending on the
            # platform, so it cannot decide the verdict on its own.
            if not _absence_is_genuine(meta_path):
                logger.warning(
                    "Metadata path %s cannot exist: a component of it is not a " "directory",
                    meta_path,
                )
                return None
            return False
        if not stat.S_ISREG(st.st_mode):
            logger.warning("Metadata path %s is not a regular file", meta_path)
            return None
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return bool(InstalledApp.from_dict(data).enabled)
    # No `json.JSONDecodeError` member: it subclasses ValueError, so pairing the two is
    # redundant and the repo ratchets against it.
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Could not determine enabled state from %s: %s", meta_path, exc)
        return None


def is_app_enabled(name: str) -> bool:
    """Read-only enablement check: True only for an installed, enabled app.

    Unlike ``get_app`` this never writes (no version-sync side effect), so it
    is safe to call from worker threads (e.g. ``asyncio.to_thread``) without
    racing loop-side writers of ``installed.json``.
    """
    meta = _read_installed(name)
    return bool(meta and meta.enabled)


# The builtin that owns the Task Runner page. Its routes are host-owned, so the
# start paths read this app's switch themselves (see ``app_disabled``).
TASK_RUNNER_APP = "projects"

TASK_RUNNER_DISABLED_MESSAGE = "Task Runner is disabled. Enable it in Library to start tasks."


def app_disabled(name: str) -> bool:
    """True only when the app is installed and switched off.

    Not ``not is_app_enabled(name)``: that is also True when no record exists,
    and a host-owned surface (Task Runner on a headless host or in tests) runs
    without one. Only an explicit ``enabled: false`` reads as disabled here.
    Read-only, like :func:`is_app_enabled`.
    """
    meta = _read_installed(name)
    return meta is not None and not meta.enabled


@holds_app_record_locks
def set_app_source(name: str, source: str) -> bool:
    """Update the ``source`` field of an installed app's metadata.

    Returns True if the update succeeded, False if the app is not installed.
    Used by the registry module to mark apps as registry-installed after
    the temp clone directory is cleaned up.
    """
    hold_app_record_lock(name)
    meta = _read_installed(name)
    if not meta:
        return False
    meta.source = source
    _write_installed(name, meta)
    return True


@holds_app_record_locks
def set_app_provenance(
    name: str,
    *,
    source: str,
    url: str,
    registry: str = "",
    commit: str = "",
    signer: str = "",
) -> bool:
    """Record the full install provenance of a registry-installed app.

    Superset of :func:`set_app_source`: alongside the bare ``registry:<name>``
    marker it persists WHICH source the app actually came from (*url* plus the
    originating external *registry* id, empty for the bundled catalog), the
    *commit* resolved in that source clone, and the verified *signer* if the
    admission layer verified one.  Updates resolve from these fields instead of
    re-looking-up the bare name, so a same-named entry published by a different
    registry source cannot capture an installed app's updates.

    Uses ``dataclasses.replace`` so every other persisted field (``enabled``,
    ``dev``, ``origin``, ...) carries forward untouched.

    Returns True if the update succeeded, False if the app is not installed.
    """
    hold_app_record_lock(name)
    meta = _read_installed(name)
    if not meta:
        return False
    _write_installed(
        name,
        replace(
            meta,
            source=source,
            sourceUrl=url,
            sourceRegistry=registry,
            sourceCommit=commit,
            sourceSigner=signer,
        ),
    )
    return True

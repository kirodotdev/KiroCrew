"""``kirocrew workspace``, and the locked ``config.json`` write the CRUD commands share.

``_handle_workspace`` lists, creates, updates and deletes workspace entries, confining
``--dir`` to the data home (``_cli_validated_workspace_dst``). ``_locked_config_write``
runs one config delta under the loader's sidecar flock and refuses a stale precondition
with ``_CliConflict``; ``kirocrew agent delete`` (``cli_commands_agent``) writes
through it too.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import dataclasses
import shutil
import sys
import uuid
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config import config_dir
from kiro_crew.config.loader import (
    KiroCrewConfig,
    WorkspaceConfig,
    WorkspaceDirUnusable,
    coerce_dict_section,
    materialize_workspace_dir,
)
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.validation import WORKSPACE_NAME_RE

# Workspace dirs are confined to the data home: a workspace is agent-writable
# working state, so letting --dir escape would let it be pointed at ~/.ssh or the
# keystone policy files. The refusal is deliberate — say so, and say what to pass
# instead, rather than a bare "invalid directory path".
_WS_DIR_OUTSIDE_HOME = (
    "Error: --dir must resolve inside the KiroCrew data home ({home}); got {given!r}. "  # brand-ok -- moved verbatim; user-visible refusal text, respelling is a separate change
    "Pass a relative directory name (e.g. 'workspace-myproject')."
)


def _ws_dir_error(given: str) -> str:
    return _WS_DIR_OUTSIDE_HOME.format(home=config_dir(), given=given)


def _cli_validated_workspace_dst(ws_dir: str, *, operation: str, name: str) -> Path:
    """Refuse *ws_dir* unless it is contained in the data home; else return its path.

    The ONE place the CLI turns a workspace ``dir`` string into a directory. The
    containment check runs first and exits the command on refusal (SEL ``denied``
    event + the outside-home message); it fails closed on a ``~unknownuser``
    prefix, so ``expanduser()`` never escapes as a traceback. What it returns is
    the very ``Path`` object the check resolved and judged -- ``~`` expanded, an
    absolute dir taken as given, a relative dir joined onto the data home --
    resolved ONCE there and never again: the create pins this parent chain, so a
    component swapped for a link after that resolution is refused, not followed,
    and there is no second resolution for a swap to slip through. Composing or
    resolving separately at a call site is how earlier revisions crashed on a
    tilde spelling and re-resolved after validation, so no call site does either.
    """
    validated = _ws_dir_resolves_inside_home(ws_dir)
    if validated is None:
        sel().log_api_access(
            caller="cli",
            operation=operation,
            outcome="denied",
            source="cli",
            resources=name,
        )
        print(_ws_dir_error(ws_dir), file=sys.stderr)
        sys.exit(1)
    return validated


def _ws_dir_composed(ws_dir: str) -> Path:
    """*ws_dir* as a path: ``~`` expanded, a relative dir joined onto the data home.

    NOT resolved. The one composition the containment check resolves and the
    create hands to the link screen as the unresolved leaf.
    """
    expanded = Path(ws_dir).expanduser()
    return expanded if expanded.is_absolute() else config_dir() / expanded


def _ws_dir_resolves_inside_home(ws_dir: str) -> Path | None:
    """The resolved path when *ws_dir* is a STRICT descendant of the data home, else None.

    The path is resolved exactly ONCE and the resolved object itself is returned,
    so the caller materializes the very path these checks judged. Resolving a
    second time after the checks would follow a parent swapped for a link in the
    meantime, and the pinned create can only refuse a swap that happens AFTER the
    path it is handed was resolved.

    ``expanduser()`` FIRST is what makes this honest: ``config_dir() / "~/x"``
    silently yields ``<home>/~/x`` — contained, but it creates a literal ``~``
    directory the user never asked for and quietly ignores the tilde they wrote.
    Expanding first means a tilde path is judged as the absolute path the user
    meant, so it is refused with the same clear message as ``/tmp/x`` (matching
    how the dashboard handler reads the same field).

    The test is CONTAINMENT, not "is it absolute": an absolute path that lands
    inside the data home is accepted (it resolves to the same place the relative
    form would, so there is nothing to refuse). What is rejected is anything
    resolving OUTSIDE — which is the property the boundary actually protects.

    STRICT descendant, so the root itself is refused HERE, in the one place
    that expands ``~`` -- the earlier per-call-site root checks compared
    ``config_dir() / ws_dir`` unexpanded, so ``~/.kiro/crew`` slipped past them.
    A workspace pointed at the data-home root would put agent-writable
    memory/lessons on top of ``config.json`` / ``.env``.

    Inside the home is NOT automatically safe: the keystone paths live there too
    (``profiles/``, ``security_policy.json``, ``admission_policy.json``,
    ``denied_commands.json``, ``.env``, ``sel_hmac.key``…). ``--copy-from`` runs
    ``copytree(..., dirs_exist_ok=True)``, so a workspace dir of ``profiles``
    would OVERWRITE the governance ceiling the agent is specifically forbidden to
    write — the one mechanism that makes that ceiling un-disableable. So the
    resolved target must also clear ``is_sensitive_path()``, the shared gate used
    everywhere else for exactly this question.

    Fails CLOSED on any path we cannot resolve. ``expanduser()`` raises
    ``RuntimeError`` for a ``~unknownuser/...`` prefix (no such user, so no home
    to expand), and ``resolve()`` can raise ``OSError`` on a pathological path —
    both must return None and route into the normal refusal, never escape as a
    traceback. That is the whole point of this PR, so the guard cannot be the one
    thing that crashes.
    """
    try:
        candidate = _ws_dir_composed(ws_dir).resolve()
        root = config_dir().resolve()
        if candidate == root or not candidate.is_relative_to(root):
            return None
        return None if is_sensitive_path(str(candidate)) else candidate
    except (RuntimeError, OSError, ValueError):
        return None


class _CliConflict(Exception):
    """An in-lock precondition re-check failed for a CLI config CRUD write.

    The CLI pre-checks its inputs against a config snapshot for fast, friendly
    errors, but the snapshot can be stale by the time the write runs. The
    mutate callbacks below re-decide every state-dependent precondition on the
    document read INSIDE the sidecar flock and raise this to refuse.
    """


# Named by the facade module, the path its handlers are reached through:
# ``module.qualname`` is how a traceback names a raised class.
_CliConflict.__module__ = "kiro_crew.cli_commands"


def _locked_config_write(
    mutate, *, cleanup_conflict=None, cleanup_failure=None, after_write=None
) -> None:
    """Run one config delta under the sidecar flock; exit(1) on a conflict.

    A load -> mutate dataclass -> ``cfg.save()`` shape cannot be used here: its
    whole-document rename silently discards any change another process
    landed between the load and the save.

    Resolved through the loader MODULE, not this module's by-value imports,
    so it honors the same ``kiro_crew.config.loader.config_path`` patches the
    old ``cfg.save()`` path did (save() resolves its path inside loader).

    ``cleanup_conflict`` runs before the exit(1) on a refused precondition;
    ``cleanup_failure`` runs when the write itself fails -- the workspace
    create passes its staging-drop / install-rollback handlers here.
    ``after_write`` runs inside the lock once the write has committed (see
    ``update_config_locked``).
    """
    from kiro_crew.config import loader as _loader

    try:
        _loader.update_config_locked(_loader.config_path(), mutate=mutate, after_write=after_write)
    except _CliConflict as exc:
        if cleanup_conflict is not None:
            cleanup_conflict()
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except BaseException:
        if cleanup_failure is not None:
            cleanup_failure()
        raise


def _handle_workspace(args: argparse.Namespace) -> None:
    """Dispatch workspace subcommands: list, create, update, delete."""

    action = getattr(args, "workspace_action", None)
    cfg = KiroCrewConfig.load()

    if action == "list":
        default = cfg.default_workspace
        print(f"{'NAME':<20} {'DIR':<40}")
        for name, ws in cfg.workspaces.items():
            marker = " *" if name == default else ""
            print(f"{name + marker:<20} {ws.dir:<40}")

    elif action == "create":
        if not WORKSPACE_NAME_RE.match(args.name):
            # Read here, not at module level: the facade mirrors every module-level
            # import of its owners, and its import inventory is frozen.
            from kiro_crew.validation import AGENT_NAME_HINT

            print(f"Error: invalid workspace name ({AGENT_NAME_HINT})", file=sys.stderr)
            sys.exit(1)
        if args.name in cfg.workspaces:
            print(f"Error: workspace '{args.name}' already exists", file=sys.stderr)
            sys.exit(1)
        copy_from = getattr(args, "copy_from", None)
        staged_path: Path | None = None
        install_state = {"installed": False}
        if copy_from:
            if copy_from not in cfg.workspaces:
                print(
                    f"Error: source workspace '{copy_from}' not found",
                    file=sys.stderr,
                )
                sys.exit(1)

            ws_dir = args.dir if args.dir is not None else f"workspace-{args.name}"
            src_path = config_dir() / cfg.workspaces[copy_from].dir
            # Validated and composed in one step (refuses and exits on a dir
            # outside the data home): the install and the fallback mkdir below
            # both land on this path, the one the containment check judged.
            dst_path = _cli_validated_workspace_dst(
                ws_dir, operation="workspace.create", name=args.name
            )
            if not src_path.resolve().is_relative_to(config_dir().resolve()):
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.create",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print("Error: invalid source directory path", file=sys.stderr)
                sys.exit(1)
            # Reject the config root as a SOURCE (the destination root was refused above).
            cfg_root = config_dir().resolve()
            if src_path.resolve() == cfg_root:
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.create",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print("Error: cannot use config root as workspace directory", file=sys.stderr)
                sys.exit(1)
            # Check for directory collision BEFORE copying files
            existing_dirs = {ws.dir for ws in cfg.workspaces.values()}
            if ws_dir in existing_dirs:
                print(
                    f"Error: directory '{ws_dir}' is already used by another workspace",
                    file=sys.stderr,
                )
                sys.exit(1)
            if src_path.is_dir():

                def _ignore_sensitive(directory: str, entries: list[str]) -> set[str]:
                    skip: set[str] = set()
                    for entry in entries:
                        full = str(Path(directory, entry).resolve())
                        if is_sensitive_path(full):
                            skip.add(entry)
                    return skip

                # Copy to a STAGING sibling, not the destination: the copy
                # runs before the locked registration, so a losing same-name
                # race must leave the winner's directory untouched. The staged
                # tree is installed inside the locked mutate below, only after
                # the in-lock checks pass.
                staging = dst_path.parent / f".{dst_path.name}.staging-{uuid.uuid4().hex[:8]}"
                try:
                    shutil.copytree(
                        src_path,
                        staging,
                        symlinks=True,
                        ignore=_ignore_sensitive,
                    )
                except BaseException:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
                staged_path = staging
        else:
            ws_dir = args.dir if args.dir is not None else f"workspace-{args.name}"

            # Validated and composed in one step (refuses and exits on a dir
            # outside the data home). Defined for BOTH branches, so the
            # destination is never an undefined name in the rollback closure
            # below, and it is the directory the containment check judged --
            # not ``<home>/~/...``.
            dst_path = _cli_validated_workspace_dst(
                ws_dir, operation="workspace.create", name=args.name
            )
        # Check for directory collision with existing workspaces
        existing_dirs = {ws.dir for ws in cfg.workspaces.values()}
        if ws_dir in existing_dirs:
            print(
                f"Error: directory '{ws_dir}' is already used by another workspace",
                file=sys.stderr,
            )
            sys.exit(1)

        def _mutate_ws_create(doc: dict) -> dict:
            workspaces = coerce_dict_section(doc, "workspaces")
            if args.name in workspaces:
                raise _CliConflict(f"workspace '{args.name}' already exists")
            used = {str(ws.get("dir", "")) for ws in workspaces.values() if isinstance(ws, dict)}
            if ws_dir in used:
                raise _CliConflict(f"directory '{ws_dir}' is already used by another workspace")
            if staged_path is not None:
                # Same install invariant as the dashboard handler: the
                # destination must not exist AT ALL. publish_dir_noreplace,
                # not check-then-rename: POSIX os.rename silently replaces an
                # EMPTY destination, so a racer's directory created between
                # the check and the rename would be destroyed.
                if dst_path.exists():
                    raise _CliConflict(
                        f"destination directory '{ws_dir}' already exists; "
                        "choose another dir or remove it first"
                    )
                try:
                    platform_compat.publish_dir_noreplace(staged_path, dst_path)
                except (FileExistsError, OSError) as exc:
                    raise _CliConflict(
                        f"destination directory '{ws_dir}' already exists; "
                        "choose another dir or remove it first"
                    ) from exc
                install_state["installed"] = True
            # A create with no copy source still needs a usable provider cwd and
            # project document directory. Created through the pinned parent, adopting a
            # directory already there; deliberately NOT rolled back on a failed
            # write -- a concurrent create can already have adopted and registered it.
            else:
                try:
                    materialize_workspace_dir(
                        dst_path, leaf=_ws_dir_composed(ws_dir), display=ws_dir
                    )
                except WorkspaceDirUnusable as exc:
                    raise _CliConflict(str(exc)) from exc
            workspaces[args.name] = dataclasses.asdict(WorkspaceConfig(dir=ws_dir))
            return doc

        def _drop_staging() -> None:
            if staged_path is not None and not install_state["installed"]:
                shutil.rmtree(staged_path, ignore_errors=True)

        def _rollback_install() -> None:
            if install_state["installed"]:
                # Same rule as the dashboard handler and as the plain-create
                # directory: an installed tree is left in place. A concurrent create
                # can already have adopted and registered it (EEXIST is accepted),
                # so deleting it would leave that workspace declared with no
                # directory. Say where it is; a silent orphan reads as a leak.
                print(
                    f"Note: leaving '{dst_path}' in place; the workspace was not "
                    "registered and no entry names it.",
                    file=sys.stderr,
                )
            elif staged_path is not None:
                shutil.rmtree(staged_path, ignore_errors=True)

        _locked_config_write(
            _mutate_ws_create,
            cleanup_conflict=_drop_staging,
            cleanup_failure=_rollback_install,
        )
        sel().log_api_access(
            caller="cli",
            operation="workspace.create",
            outcome="success",
            source="cli",
            resources=args.name,
        )
        print(f"Created workspace: {args.name}")

    elif action == "update":
        if args.name not in cfg.workspaces:
            print(f"Error: workspace '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        if args.dir is not None:
            # Validated and composed in one step (refuses and exits on a dir
            # outside the data home); the locked mutate below checks this same
            # path, so the update judges the directory the check judged.
            update_dst = _cli_validated_workspace_dst(
                args.dir, operation="workspace.update", name=args.name
            )
            existing_dirs = {ws.dir for n, ws in cfg.workspaces.items() if n != args.name}
            if args.dir in existing_dirs:
                print(
                    f"Error: directory '{args.dir}' is already used by another workspace",
                    file=sys.stderr,
                )
                sys.exit(1)

        def _mutate_ws_update(doc: dict) -> dict:
            workspaces = coerce_dict_section(doc, "workspaces")
            entry = workspaces.get(args.name)
            if not isinstance(entry, dict):
                raise _CliConflict(f"workspace '{args.name}' not found")
            if args.dir is not None:
                used = {
                    str(ws.get("dir", ""))
                    for n, ws in workspaces.items()
                    if n != args.name and isinstance(ws, dict)
                }
                if args.dir in used:
                    raise _CliConflict(
                        f"directory '{args.dir}' is already used by another workspace"
                    )
                # Same materialize-or-refuse invariant the create path holds:
                # provider cwd and project documents need a usable directory.
                # An update names a destination
                # the owner already chose, so it refuses rather than creating one.
                if not update_dst.is_dir():
                    raise _CliConflict(
                        f"directory '{args.dir}' does not exist or is not a "
                        "directory; create it first"
                    )
                entry["dir"] = args.dir
            return doc

        _locked_config_write(_mutate_ws_update)
        sel().log_api_access(
            caller="cli",
            operation="workspace.update",
            outcome="success",
            source="cli",
            resources=args.name,
        )
        print(f"Updated workspace: {args.name}")

    elif action == "delete":
        if args.name not in cfg.workspaces:
            sel().log_api_access(
                caller="cli",
                operation="workspace.delete",
                outcome="denied",
                source="cli",
                resources=args.name,
            )
            print(f"Error: workspace '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        if args.name == cfg.default_workspace:
            sel().log_api_access(
                caller="cli",
                operation="workspace.delete",
                outcome="denied",
                source="cli",
                resources=args.name,
            )
            print(
                f"Error: cannot delete default workspace '{args.name}'",
                file=sys.stderr,
            )
            sys.exit(1)
        referencing = [a for a, ac in cfg.agents.items() if ac.workspace == args.name]
        if referencing:
            sel().log_api_access(
                caller="cli",
                operation="workspace.delete",
                outcome="denied",
                source="cli",
                resources=args.name,
            )
            print(
                f"Error: workspace '{args.name}' is referenced by agents: "
                f"{', '.join(referencing)}",
                file=sys.stderr,
            )
            sys.exit(1)

        def _mutate_ws_delete(doc: dict) -> dict:
            workspaces = coerce_dict_section(doc, "workspaces")
            if args.name not in workspaces:
                raise _CliConflict(f"workspace '{args.name}' not found")
            # Re-check the TOP-LEVEL default in-lock: a concurrent default
            # change between this command's snapshot and the lock would
            # otherwise leave config pointing at a deleted workspace.
            if doc.get("default_workspace") == args.name:
                raise _CliConflict(f"cannot delete default workspace '{args.name}'")
            agents = doc.get("agents")
            if isinstance(agents, dict):
                still_referencing = sorted(
                    a
                    for a, ac in agents.items()
                    if isinstance(ac, dict) and ac.get("workspace") == args.name
                )
                if still_referencing:
                    raise _CliConflict(
                        f"workspace '{args.name}' is referenced by agents: "
                        + ", ".join(still_referencing)
                    )
            del workspaces[args.name]
            return doc

        _locked_config_write(_mutate_ws_delete)
        sel().log_api_access(
            caller="cli",
            operation="workspace.delete",
            outcome="success",
            source="cli",
            resources=args.name,
        )
        print(f"Deleted workspace: {args.name}")

    else:
        print("Usage: kirocrew workspace {list|create|update|delete}")

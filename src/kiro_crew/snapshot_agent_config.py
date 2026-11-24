"""Restore's hold-back of the agent config an archive carries under ``workspace/``.

A bundle can come from someone else, and each harness reads its project config from the
folder a session starts in, so neither restore mode installs the archive's copy of those
names. :func:`_hold_back_agent_config` is the entry point; ``kiro_crew.snapshot`` re-exports
every name here for its existing callers.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.snapshot_components import _WHOLE_TREE_COMPONENTS, COMPONENTS, _facade, _want
from kiro_crew.snapshot_restore import _rmtree_or_raise

#: Names, at any depth under ``workspace/``, that steer an agent session started in that
#: folder (sessions start in ``workspace/<project>/`` folders): each harness's project
#: settings, hooks, skills, agents and MCP servers. Derived from the project config the
#: harness code itself reads or fences, so a harness added there is added here:
#: ``.claude`` (``acp/claude_settings_sources.py:171``, ``native_allow_stores.py:126``),
#: ``.mcp.json`` (Claude's project MCP file), ``.kiro`` (``acp/kas_settings_mcp.py:209``,
#: ``acp/session_mcp.py:1501``, ``acp/kas_skill_scope.py:128``), ``opencode.json``,
#: ``opencode.jsonc``, ``.opencode`` (``native_allow_stores.py:130-132``), ``.pi``
#: (``native_allow_stores.py:135``), ``mimocode.json``, ``mimocode.jsonc``, ``.mimocode``
#: (``native_allow_stores.py:141-143``). ``.codex``, ``.goose`` and ``.goosehints`` are those
#: tools' own project names, named by the security review; nothing in this tree reads them.
#: Plain notes are NOT here, see ``_NOTE_NAMES``.
_AGENT_CONFIG_NAMES = (
    ".claude",
    ".mcp.json",
    ".kiro",
    ".codex",
    ".goose",
    ".goosehints",
    ".opencode",
    "opencode.json",
    "opencode.jsonc",
    ".pi",
    ".mimocode",
    "mimocode.json",
    "mimocode.jsonc",
)
#: Notes an agent reads as instructions but that carry no permissions, hooks or servers.
#: They restore as usual and the summary names each one a restore installed.
_NOTE_NAMES = ("claude.md", "agents.md")


def _agent_config_paths(
    root: Path,
    *,
    names: tuple[str, ...] = _AGENT_CONFIG_NAMES,
) -> list[Path]:
    """Every entry under *root* named in *names*, at any depth, without following links.

    A matching directory is returned whole and not entered; a link is returned as the
    entry it is, so the caller refuses or unlinks it rather than walking through it.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        keep: list[str] = []
        for d in sorted(dirnames):
            if d.casefold() in names:
                found.append(here / d)
            else:
                keep.append(d)
        dirnames[:] = keep
        found.extend(here / f for f in sorted(filenames) if f.casefold() in names)
    return found


def _hold_back_agent_config(
    snap: Path, mc: Path, components: list[str] | None, mode: str, *, allow_unpinned: bool = False
) -> tuple[list[str], list[str]]:
    """Keep this install's agent-config files out of reach of the archive's copies.

    Done on the staged bundle, like :func:`_hold_back_executables`, over every
    ``workspace/`` tree the selected components install (:func:`_restored_workspace_trees`):
    the whole tree for ``workspace``, the memory and knowledge subtrees for ``memory``.
    The archive's copy is removed, so merge (which adds what is missing) installs nothing
    for these names. Replace swaps each such tree whole, so it also gets the LIVE copy
    staged in the
    archive's place; a live name with no copy ends up absent, never the archive's. Returns
    ``(held, notes)``: the workspace-relative paths the bundle carried and did not get to
    install, and the notes (``CLAUDE.md``, ``AGENTS.md``) the restore WILL install, which
    stay restorable but are named. Merge keeps a note the live tree already has.

    A live link or hard link is not handled here: Replace's own backup of the live
    ``workspace/`` (``_backup_tree_or_refuse``) refuses on one before this copy matters.

    Both walks enter every directory, ``node_modules``, ``.git`` and ``venv`` included: a
    skip on only the live side strips the archive's copy there while the live one is lost,
    and a skip on both sides would let the archive install config there. Walking everything
    is the one choice that can neither lose nor install a config.
    shortcut: the live walk reads the whole workspace tree, so a huge ``node_modules`` costs
    time on Replace; bound it only with a skip applied to BOTH walks.
    """
    held: list[str] = []
    notes: list[str] = []
    workspace = snap / "workspace"
    for tree in _restored_workspace_trees(components):
        staged = snap / tree
        live = mc / tree
        if not staged.is_dir():
            # A whole-tree component leaves its live tree alone when the archive lacks it.
            # Replace clears any other tree anyway (`_do_replace_mutations`), so its live
            # config is staged into an empty tree, which replace then installs.
            if (
                mode != "replace"
                or _owned_by_whole_tree_component(tree)
                or platform_compat.is_link_or_junction(live)
                or not _agent_config_paths(live)
            ):
                continue
            staged.mkdir(parents=True)
        h, n = _hold_back_agent_config_in(
            staged, live, mode, allow_unpinned=allow_unpinned, workspace=workspace
        )
        held.extend(h)
        notes.extend(n)
    return held, notes


def _owned_by_whole_tree_component(tree: str) -> bool:
    """Whether *tree* belongs to a component whose replace keeps a tree the archive lacks."""
    return any(tree in COMPONENTS[name].trees for name in _WHOLE_TREE_COMPONENTS)


def _restored_workspace_trees(components: list[str] | None) -> list[str]:
    """The ``workspace/`` trees a restore of *components* installs, outermost only.

    Read from the component table: ``workspace`` installs the whole tree, and ``memory``
    on its own installs ``workspace/memory`` and ``workspace/knowledge``. A tree inside a
    selected one is dropped, so no path is walked twice.
    """
    trees = {
        tree
        for name, spec in COMPONENTS.items()
        if _want(components, name)
        for tree in spec.trees
        if PurePosixPath(tree).parts[0] == "workspace"
    }
    return sorted(
        t for t in trees if not any(o != t and t.startswith(o.rstrip("/") + "/") for o in trees)
    )


def _hold_back_agent_config_in(
    staged: Path, live_root: Path, mode: str, *, allow_unpinned: bool, workspace: Path
) -> tuple[list[str], list[str]]:
    """:func:`_hold_back_agent_config` for one staged tree and its live counterpart.

    Paths it returns are relative to the staged ``workspace/``, so a subtree's entries
    read the same as the whole tree's.
    """
    facade = _facade()
    held: list[str] = []
    for entry in _agent_config_paths(staged):
        if platform_compat.is_link_or_junction(entry):
            platform_compat.unlink_link_or_junction(entry)
        elif entry.is_dir():
            _rmtree_or_raise(entry)
        else:
            entry.unlink()
        held.append(entry.relative_to(workspace).as_posix())
    notes = [
        p.relative_to(workspace).as_posix()
        for p in _agent_config_paths(staged, names=_NOTE_NAMES)
        if mode == "replace" or not os.path.lexists(live_root / p.relative_to(staged))
    ]
    if mode == "replace":
        for live in _agent_config_paths(live_root):
            rel = live.relative_to(live_root)
            dst = staged / rel
            what = f"keeping the live workspace/{dst.relative_to(workspace).as_posix()}"
            # A parent the archive does not carry is made; one that is a link is refused,
            # so the live copy is never written through it.
            for parent in dst.parents:
                if parent == staged:
                    break
                if platform_compat.is_link_or_junction(parent):
                    raise pinned_fs.PinnedPathRefusal(f"{what}: staged parent is a link")
            dst.parent.mkdir(parents=True, exist_ok=True)
            skip = pinned_fs.fatal_skip_reporter(what)
            if live.is_dir() and not platform_compat.is_link_or_junction(live):
                facade._copytree_safe(
                    live, dst, allow_unpinned=allow_unpinned, must_create=True, on_skip=skip
                )
            else:
                pinned_fs.copy_file_pinned(str(live), str(dst), on_skip=skip)
    return held, notes

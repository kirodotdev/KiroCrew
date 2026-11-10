"""Workspace steering and default-resource inheritance on a KAS session.

KAS loads every root's ``.kiro/steering`` tree itself (``.md`` files only) and
honours each file's ``inclusion`` (``manual`` and ``fileMatch`` wait). Its
``resources`` reader has no such filter: a ``file://.kiro/steering/**/*.md`` entry
injects every matched file on every turn, beside the native copy. Crew's default
agent ships that entry (``config/defaults.json``) for kiro-cli, which applies the
inclusion rules to it.

So on KAS a relative entry inside ``.kiro/steering`` that can match only ``.md``
files is dropped: KAS resolves it under the same workspace roots its native load
scans. It is kept, exactly as before, unless Crew can show nothing is lost:

* an ``inclusion: auto`` file is one KAS may never deliver (it skips one with no
  ``description``), while the entry delivers it. shortcut: any file that might be
  ``auto`` keeps the entry, read as text, not front matter; forward just those
  files once a reader matches KAS's front matter and walk exactly.
* a link in the tree, an unreadable folder or file, or a tree past KAS's walk
  bounds keeps the entry too; nothing here follows a link.

shortcut: the tree is checked once, when the session's agent is projected, while
KAS re-reads ``resources`` every turn. A file that may be ``auto``, or a link,
added after that loads from the next session; re-project on a steering change if
that delay matters.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

_FILE_SCHEME = "file://"
_STEERING_TREE = (".kiro", "steering")
_MD = ".md"
#: KAS reads no resource file bigger than this (``resource-resolver.ts``).
_MAX_FILE_BYTES = 1024 * 1024
#: The bounds of KAS's own steering walk (``steering-tree-walk.ts``).
_MAX_DIRECTORIES = 2_000
_MAX_DOCUMENTS = 2_000


def _workspace_steering_entry(entry: str) -> bool:
    """Whether *entry* names only ``.md`` files of the workspace steering tree.

    That is a relative ``file://`` path inside ``.kiro/steering`` whose last part
    ends in ``.md`` and uses only ``*``, ``?`` and ``**`` as wildcards.

    shortcut: ``~/.kiro/steering`` entries are kept, since KAS's home steering
    folder follows its own config dir; drop them too once Crew reads that dir.
    """
    if not entry.startswith(_FILE_SCHEME):
        return False
    raw = entry[len(_FILE_SCHEME) :]
    if raw.startswith(("/", "\\", "~", "$")) or Path(raw).anchor or set(raw) & set("[]{}!"):
        return False
    parts = PurePosixPath(raw.replace("\\", "/")).parts
    return (
        bool(parts)
        and ".." not in parts
        and tuple(parts[:2]) == _STEERING_TREE
        and parts[-1].endswith(_MD)
    )


def _is_link(path: Path) -> bool:
    """Whether *path* is a symlink or a Windows junction; a failed probe counts as one."""
    try:
        return path.is_symlink() or os.path.isjunction(path)
    except OSError:
        return True


def _may_be_auto(path: Path) -> bool:
    """Whether *path* could be ``inclusion: auto`` steering, or cannot be read."""
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_FILE_BYTES + 1)
    except OSError:
        return True
    text = data.decode("utf-8", errors="replace").casefold()
    return len(data) > _MAX_FILE_BYTES or ("inclusion" in text and "auto" in text)


def _native_load_covers(work_dir: str | os.PathLike[str]) -> bool:
    """Whether KAS's native load delivers every ``.md`` of *work_dir*'s steering tree."""
    tree = Path(work_dir, *_STEERING_TREE)
    if _is_link(tree.parent) or _is_link(tree):
        return False
    failed: list[OSError] = []
    dirs = docs = 0
    for directory, subdirs, files in os.walk(tree, onerror=failed.append):
        dirs += 1
        base = Path(directory)
        if any(_is_link(base / name) for name in (*subdirs, *files)):
            return False
        for name in files:
            # The entry's glob ignores case on macOS and Windows; KAS's walk does not.
            if name.casefold().endswith(_MD) and not name.endswith(_MD):
                return False
            if name.endswith(_MD):
                docs += 1
                if _may_be_auto(base / name):
                    return False
        if dirs > _MAX_DIRECTORIES or docs > _MAX_DOCUMENTS:
            return False
    return not failed


def kas_steering_resources(resources: object, work_dir: str | os.PathLike[str] | None) -> object:
    """*resources* as a KAS session should get them (module docstring).

    Anything that is not a list, and every entry native KAS does not cover,
    passes unchanged.
    """
    if not isinstance(resources, list) or not work_dir:
        return resources
    if not any(isinstance(e, str) and _workspace_steering_entry(e) for e in resources):
        return resources
    if not _native_load_covers(work_dir):
        return resources
    return [e for e in resources if not (isinstance(e, str) and _workspace_steering_entry(e))]


def with_kas_steering(
    spec: dict[str, Any], work_dir: str | os.PathLike[str] | None
) -> dict[str, Any]:
    """A copy of *spec* with :func:`kas_steering_resources` applied; *spec* is not changed."""
    if "resources" not in spec:
        return spec
    return {**spec, "resources": kas_steering_resources(spec["resources"], work_dir)}


def inheritance_off_notice(work_dir: str | Path | None) -> str:
    """The session-start line when Kiro's default resources are turned off, else ``""``.

    KAS has no switch for its native ``AGENTS.md`` and steering load. The line says
    nothing about skills: whether the default trees are held back depends on the
    session's skill scope (``acp/kas_skill_scope.py``), and a whole-catalog agent
    keeps them.
    """
    from kiro_crew.agent_sdk.drivers.acp import inherits_default_resources

    if inherits_default_resources(work_dir):
        return ""
    return (
        "ℹ Kiro's chat.disableInheritingDefaultResources is on, but KAS has no switch "
        "for its native load: AGENTS.md and .kiro/steering files still load."
    )

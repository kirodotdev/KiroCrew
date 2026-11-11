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
  ``description``), while the entry delivers it. So every file that might be
  ``auto``, read as text, not front matter, is named by its own path in the
  entry's place, and KAS reads just those on every turn. A dot-named one is left
  out: the entry's glob never matched it. The entry stays when KAS might read a
  path otherwise, or with less of its resource budget than the entry gave it: a
  ``*`` or ``?`` in a name (KAS reads that as a glob), another file entry before
  it or another steering entry, or files that might pass the budget.
* a link in the tree, an unreadable folder, or a tree past KAS's walk bounds
  keeps the entry too; nothing here follows a link.

shortcut: the tree is checked once, when the session's agent is projected, while
KAS re-reads ``resources`` every turn. A file that may be ``auto``, or a link,
added after that loads from the next session; re-project on a steering change if
that delay matters.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew.bounded_read import bounded_read

_FILE_SCHEME = "file://"
_STEERING_TREE = (".kiro", "steering")
#: The entry Crew's default agent ships, the only one named file by file.
_WHOLE_TREE = "file://.kiro/steering/**/*.md"
_MD = ".md"
#: KAS reads no resource file bigger than this (``resource-resolver.ts``).
_MAX_FILE_BYTES = 1024 * 1024
#: The bounds of KAS's own steering walk (``steering-tree-walk.ts``).
_MAX_DIRECTORIES = 2_000
_MAX_DOCUMENTS = 2_000
#: How many resource files, and characters of them, KAS reads for an agent
#: (``BoundedReader``: 32 files, 100,000 characters less 1,000 for its marker).
_MAX_RESOURCE_FILES = 32
_MAX_RESOURCE_CHARS = 99_000


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
            data = bounded_read(handle, _MAX_FILE_BYTES + 1)
    except OSError:
        return True
    text = data.decode("utf-8", errors="replace").casefold()
    # KAS reads YAML and JSON escapes, so `inclusion: "\x61uto"` is `auto` with no
    # literal word. Any backslash before KAS's first closing `\n---` counts as maybe.
    front = text.split("\n---", 1)[0]
    return len(data) > _MAX_FILE_BYTES or "\\" in front or ("inclusion" in text and "auto" in text)


def _maybe_auto_files(tree: Path) -> list[Path] | None:
    """The ``.md`` files of *tree* KAS's native load may skip as ``auto``.

    ``None`` when it may miss others too: a link, an unreadable folder, a name the
    entry's glob matches in another case, or a tree past KAS's walk bounds.
    """
    if _is_link(tree.parent) or _is_link(tree):
        return None
    failed: list[OSError] = []
    maybe_auto: list[Path] = []
    dirs = docs = 0
    for directory, subdirs, files in os.walk(tree, onerror=failed.append):
        dirs += 1
        base = Path(directory)
        if any(_is_link(base / name) for name in (*subdirs, *files)):
            return None
        for name in files:
            # The entry's glob ignores case on macOS and Windows; KAS's walk does not.
            if name.casefold().endswith(_MD) and not name.endswith(_MD):
                return None
            if name.endswith(_MD):
                docs += 1
                if _may_be_auto(base / name):
                    maybe_auto.append(base / name)
        if dirs > _MAX_DIRECTORIES or docs > _MAX_DOCUMENTS:
            return None
    return None if failed else maybe_auto


def _by_path(resources: list[Any], tree: Path, files: list[Path]) -> list[str] | None:
    """Entries naming each of *files*, for the whole-tree entry in *resources*.

    ``None`` when KAS might read one of them otherwise, or with less of its budget
    than the entry gave it (module docstring).
    """
    strings = [e for e in resources if isinstance(e, str)]
    if {e for e in strings if _workspace_steering_entry(e)} != {_WHOLE_TREE}:
        return None
    if any(e.startswith(_FILE_SCHEME) for e in strings[: strings.index(_WHOLE_TREE)]):
        return None
    # node-glob's `dot: false`: the entry never matched a dot-named folder or file.
    parts = [
        p
        for p in (f.relative_to(tree).parts for f in files)
        if not any(n.startswith(".") for n in p)
    ]
    if len(parts) > _MAX_RESOURCE_FILES or any(set("*?") & set("".join(p)) for p in parts):
        return None
    try:
        # A UTF-8 byte count is never below the character count KAS budgets.
        if sum(tree.joinpath(*p).stat().st_size for p in parts) > _MAX_RESOURCE_CHARS:
            return None
    except OSError:
        return None
    return [_FILE_SCHEME + str(PurePosixPath(*_STEERING_TREE, *p)) for p in sorted(parts)]


def kas_steering_resources(resources: object, work_dir: str | os.PathLike[str] | None) -> object:
    """*resources* as a KAS session should get them (module docstring).

    Anything that is not a list, and every entry native KAS does not cover,
    passes unchanged.
    """
    if not isinstance(resources, list) or not work_dir:
        return resources
    if not any(isinstance(e, str) and _workspace_steering_entry(e) for e in resources):
        return resources
    tree = Path(work_dir, *_STEERING_TREE)
    maybe_auto = _maybe_auto_files(tree)
    if maybe_auto is None:
        return resources
    by_path = _by_path(resources, tree, maybe_auto) if maybe_auto else []
    if by_path is None:
        return resources
    out: list[Any] = []
    for entry in resources:
        if entry == _WHOLE_TREE:
            out += by_path
            by_path = []
        elif not (isinstance(entry, str) and _workspace_steering_entry(entry)):
            out.append(entry)
    return out


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

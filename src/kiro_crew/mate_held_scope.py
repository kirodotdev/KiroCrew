"""What one held Bot limit covers: a whole capability, one tool, or some paths.

``mate_held_rules`` records each limit a moved Bot's worker cannot carry, and its
calls ask a person even under Trust. A limit is recorded as narrowly as Crew can
read it, so it holds the calls it speaks about and not the Bot's others:

- a hook whose matcher names, in any kiro-cli spelling (``useAws``, ``aws``), a
  built-in tool Crew has no capability for (:data:`_UNCAPPED_TOOLS`) is recorded
  as ``tool:<its kiro-cli name>`` and holds that tool's calls only;
- a ``permissions`` rule that denies (or asks for) reads or writes of named
  absolute paths, and a ``toolsSettings`` entry whose only limit is
  ``deniedPaths`` of absolute paths, keep those paths, and hold only the reads or
  writes whose target is at, below or above the folder each path starts in.

Every narrowing fails closed. A matcher that is a tag, a case variant, an unknown
name or more than one tool, a rule or setting that limits more than paths or names
a relative path or a variable, and a call whose target cannot be told (no path, a
variable, a relative path with no folder to read it in, a scan cut short, a link
that cannot be resolved) are held as before. Blocking file I/O (bounded
``realpath``); never raises for a call it cannot read, it holds it.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from collections.abc import Iterable, Mapping
from typing import Any

#: The capability prefix of a limit that covers one tool no capability names.
TOOL_PREFIX = "tool:"

#: kiro-cli built-in tools Crew places under no capability, by kiro-cli's name. A
#: hook on one of these is narrowed to it; any other name it cannot place stays a
#: limit on every call.
_UNCAPPED_TOOLS = frozenset({"use_aws"})

#: The capabilities a held limit may be narrowed to paths for.
_PATH_CAPABILITIES = frozenset({"fs_read", "fs_write"})

#: The effects of a ``permissions`` rule that limit a call.
_LIMITING_EFFECTS = frozenset({"deny", "ask"})


def named_tool(name: str) -> str:
    """``tool:<kiro-cli name>`` when *name* spells an :data:`_UNCAPPED_TOOLS` tool, else ``""``.

    *name* is folded through kiro-cli's own aliases first (``useAws`` and ``aws``
    are ``use_aws``), so every spelling of one tool records and matches as one.
    """
    from kiro_crew.platform.tool_names import canonical_kiro_cli_tool_name
    from kiro_crew.platform.tools_settings_aliases import _TOOLS_SETTINGS_CANONICAL

    if not isinstance(name, str) or not name:
        return ""
    folded = canonical_kiro_cli_tool_name(name)
    folded = _TOOLS_SETTINGS_CANONICAL.get(folded, folded)
    return TOOL_PREFIX + folded if folded in _UNCAPPED_TOOLS else ""


def rule_paths(capability: str, rule: Any) -> list[str] | None:
    """The paths a held ``permissions`` *rule* limits, ``None`` when it limits more."""
    if capability not in _PATH_CAPABILITIES or not isinstance(rule, dict):
        return None
    if set(rule) - {"capability", "effect", "match"} or rule.get("effect") not in _LIMITING_EFFECTS:
        return None
    return _absolute_strings(rule.get("match"))


def settings_paths(capability: str, entry: Any, grant_keys: Iterable[str]) -> list[str] | None:
    """The paths a held ``toolsSettings`` *entry* limits, ``None`` when it limits more.

    Its grant keys (*grant_keys*, such as ``allowedPaths``) only let calls run, so
    only an entry whose one limiting key is ``deniedPaths`` is read as a path limit.
    """
    if capability not in _PATH_CAPABILITIES or not isinstance(entry, dict):
        return None
    if set(entry) - set(grant_keys) != {"deniedPaths"}:
        return None
    return _absolute_strings(entry.get("deniedPaths"))


def _absolute_strings(value: Any) -> list[str] | None:
    """*value* when it is a list of patterns that are each absolute once ``~`` is read.

    A relative pattern is matched by its tool in folders Crew cannot list here, and
    a variable or a glob character Crew does not read names what it cannot tell:
    either keeps the limit on every call of its capability (``None``).
    """
    if not isinstance(value, list) or not value:
        return None
    return list(value) if all(_prefix(v) for v in value) else None


def _prefix(pattern: Any) -> str:
    """The folder *pattern* starts in, in glob form, ``""`` when that cannot be told.

    The path itself for a pattern with no wildcard; the folder before the first
    wildcard otherwise (``/p/**/x`` -> ``/p``).
    """
    from kiro_crew.platform import allowed_paths

    if not isinstance(pattern, str) or not pattern:
        return ""
    if os.path.expandvars(pattern) != pattern:
        return ""
    parsed = allowed_paths.parse(os.path.expanduser(pattern))
    if parsed is None or not allowed_paths.is_absolute(parsed):
        return ""
    return posixpath.normpath(allowed_paths.literal_prefix_dir(parsed))


def covers_tool(capability: str, names: set[str]) -> bool:
    """Whether a ``tool:<name>`` limit covers a call the harness named *names*."""
    return any(named_tool(n) == capability for n in names)


def covers_paths(paths: list[str], event: Any) -> bool:
    """Whether a call *event* may reach one of *paths*, or its targets cannot be told.

    It may when a target, in any resolved spelling, is the folder a pattern starts
    in (:func:`_prefix`, in any resolved spelling), lies below it, or is a folder
    above it. That holds more than the pattern matches, never less.
    """
    from kiro_crew.platform.tool_paths import edit_target_candidates

    prefixes: set[str] = set()
    for pattern in paths:
        forms = _forms(_prefix(pattern))
        if forms is None:
            return True
        prefixes |= forms
    raw = getattr(event, "raw_tool_params", None)
    if _names_unscanned_targets(raw):
        return True
    found = edit_target_candidates(
        raw if isinstance(raw, Mapping) else None,
        getattr(event, "diff_path", "") or "",
        tool_kind=getattr(event, "tool_kind", "") or "",
    )
    if found.truncated or found.unanchored or not found:
        return True
    cwd = str(getattr(event, "spec_write_cwd", "") or "")
    for target in found:
        forms = _forms(_anchored(target, cwd))
        if forms is None or any(_related(t, p) for t in forms for p in prefixes):
            return True
    return False


def _names_unscanned_targets(raw: object) -> bool:
    """Whether *raw* names files under a key ``edit_target_candidates`` does not read.

    An Image read lists its files under ``image_paths`` (``paths`` in kiro-cli's
    agent engine), and a batch read's entry may name no scanned path at all, so
    such a call's targets cannot be told from the scan.
    """
    from kiro_crew.platform.governance import _every_operation_names_a_path

    if not isinstance(raw, Mapping):
        return False
    if isinstance(raw.get("operations"), list):
        return not _every_operation_names_a_path(raw)
    return "image_paths" in raw or raw.get("mode") == "Image"


def _anchored(path: str, cwd: str) -> str:
    """*path* made absolute, ``""`` when what it names cannot be told."""
    if not isinstance(path, str) or os.path.expandvars(path) != path:
        return ""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        if not cwd or not os.path.isabs(cwd):
            return ""
        expanded = os.path.join(cwd, expanded)
    return expanded


def _forms(path: str) -> set[str] | None:
    """*path* as written and through every link, compared in one form; ``None`` when unknown.

    Unknown for a blank *path*, for a resolution that stalls or gives nothing, and on
    Windows for a path with no drive (``\\held``): it resolves against the gateway's
    own drive, which need not be the drive the call meant.
    """
    from kiro_crew.platform import allowed_paths
    from kiro_crew.security.paths_resolver import PathResolutionStalled, _resolved_forms_bounded

    if not path:
        return None
    if allowed_paths.ON_WINDOWS and not ntpath.splitdrive(path)[0]:
        return None
    try:
        resolved = _resolved_forms_bounded(path)
    except PathResolutionStalled:
        return None
    if not resolved:
        return None
    return {_fold(p) for p in (path, *resolved)}


def _fold(path: str) -> str:
    from kiro_crew.platform import allowed_paths

    path = posixpath.normpath(allowed_paths.to_glob_path(path))
    # macOS and Windows file systems ignore case by default: compare without it.
    return path.casefold() if sys.platform in ("darwin", "win32") else path


def _related(a: str, b: str) -> bool:
    """Whether *a* and *b* are one path, or one lies inside the other."""
    return a == b or _inside(a, b) or _inside(b, a)


def _inside(path: str, folder: str) -> bool:
    return path.startswith(folder.rstrip("/") + "/")

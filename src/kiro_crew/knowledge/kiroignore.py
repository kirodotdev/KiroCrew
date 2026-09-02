"""``.kiroignore`` -- per-project exclusion rules for knowledge folder scans.

A folder source otherwise has no way to honour the project's own notion of what
is generated: registering a directory holding several projects pulls whole
``cdk.out`` / coverage / lockfile trees into the Library at LLM cost, and the
only remedy is hand-editing that source's ``ignore_patterns`` JSON. A
``.kiroignore`` file at the SOURCE ROOT moves that decision into the project,
where it can be committed and reviewed.

Syntax is a deliberate SUBSET of gitignore. Supported:

* ``#`` comments and blank lines; a leading ``\\#`` or ``\\!`` escapes the marker
* trailing ``/`` -- matches directories only
* leading ``/`` -- anchors the pattern at the source root
* any other embedded ``/`` -- also anchors at the root (gitignore's rule); a
  pattern with no separator matches that basename at any depth
* ``*`` (any run of non-separator characters), ``?`` (one non-separator),
  ``**`` (crosses separators, including the ``**/`` prefix and ``/**`` suffix)
* ``!`` negation -- among the rules that match a given path, the LAST one wins
* trailing whitespace is stripped

Deliberately NOT supported, so a reader is never misled about the fidelity:

* ``[a-z]`` character classes -- the brackets are matched literally
* backslash escapes other than a leading ``\\#`` / ``\\!``
* nested ``.kiroignore`` files in subdirectories -- root only
* a rule file that is a symlink or hardlink resolving OUTSIDE the source root,
  including one pointing at a protected file. The read is gated (see
  ``_read_rule_file``), so sharing one rule file across roots by symlink is
  ignored rather than followed; copy the file into each root instead.
* re-including a path underneath an excluded DIRECTORY. As in git, once a
  directory is excluded nothing inside it can be negated back in; the walk
  prunes the directory and never descends it.

``.gitignore`` is deliberately NOT read as a fallback. A folder scan treats a
file that stops being discovered as DELETED and archives its indexed items, so
honouring ``.gitignore`` implicitly would, on the next sweep, drop already
indexed documents out of every existing folder source whose root happens to be a
repository -- silent data loss the user never asked for. Creating a
``.kiroignore`` is an explicit act, so the same removal is the intent.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

KIROIGNORE_FILENAME = ".kiroignore"

# A rule file is hand-written config, so these ceilings only exist to stop a
# pathological or accidentally-huge file (a committed log renamed by mistake)
# from costing memory and regex-compile time on every scan.
MAX_FILE_BYTES = 64 * 1024
MAX_RULES = 1000


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    dir_only: bool
    negated: bool


def _translate(pattern: str) -> str:
    """Convert one glob body (no leading/trailing ``/``) to a regex fragment."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            # Zero or more leading path segments.
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def _compile(line: str) -> _Rule | None:
    """Compile one raw ``.kiroignore`` line, or ``None`` if it carries no rule."""
    # Trailing whitespace is not significant; escaped trailing spaces are out of
    # scope (documented), so this strip is unconditional.
    line = line.rstrip()
    if not line or line.lstrip().startswith("#"):
        return None

    negated = False
    if line.startswith("!"):
        negated = True
        line = line[1:]
    elif line.startswith("\\#") or line.startswith("\\!"):
        line = line[1:]
    if not line:
        return None

    dir_only = line.endswith("/")
    body = line[:-1] if dir_only else line
    # gitignore anchors any pattern carrying a separator anywhere other than as
    # its trailing character (already removed above); everything else matches
    # that basename at any depth. A leading "/" only anchors -- it is not part
    # of the pattern to match.
    anchored = "/" in body
    body = body.lstrip("/")
    if not body:
        return None
    prefix = "" if anchored else "(?:[^/]+/)*"
    try:
        regex = re.compile(f"^{prefix}{_translate(body)}$")
    except re.error:
        # One unusable pattern must not cost the whole file.
        logger.debug("Ignoring unusable .kiroignore pattern: %r", line)
        return None
    return _Rule(regex=regex, dir_only=dir_only, negated=negated)


class KiroIgnore:
    """Compiled ``.kiroignore`` rule set, matched against root-relative paths."""

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules

    def __bool__(self) -> bool:
        return bool(self._rules)

    def is_ignored(self, rel_path: str, *, is_dir: bool) -> bool:
        """True when *rel_path* (``/``-separated, relative to the source root) is excluded.

        Every ancestor directory is tested too, so a rule that excludes a
        directory excludes its whole subtree even when the walk reaches a file
        inside it directly.
        """
        if not self._rules or not rel_path or rel_path == ".":
            return False
        parts = [p for p in rel_path.split("/") if p and p != "."]
        for depth in range(1, len(parts) + 1):
            candidate = "/".join(parts[:depth])
            # Every prefix shorter than the full path is a directory by construction.
            if self._match(candidate, is_dir=is_dir or depth < len(parts)):
                return True
        return False

    def _match(self, path: str, *, is_dir: bool) -> bool:
        """Last matching rule wins, which is what makes ``!`` negation work."""
        result = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(path):
                result = not rule.negated
        return result


def _read_rule_file(path: Path, root: str | os.PathLike[str]) -> bytes | None:
    """Read the rule file through the centralized guarded-read helper.

    A source root is chosen by whoever registers the folder, so ``.kiroignore``
    is an attacker-influenceable path and must not be read with a bare ``open``.
    ``hooks.safe_read_file_bytes_nolink`` is the repo's hardened read, and each
    of its three guarantees matters for a rule file:

    * the RESOLVED target is screened by ``is_sensitive_path``, so a
      ``.kiroignore`` that is a symlink to a protected file (``~/.aws/credentials``
      and friends) is refused instead of having its lines parsed -- and logged --
      as ignore patterns;
    * ``within_root`` pins the OPENED descriptor's real path inside the source
      root, so a symlink or hardlink pointing out of the tree is refused even
      when its target is not itself classified sensitive. A rule file is project
      config; it has no reason to resolve outside the project it describes;
    * the open precedes the validation (``O_NOFOLLOW``, then ``fstat`` on the
      descriptor), so the inode validated is the inode read. That leaves no
      check-then-open window, and it puts the size ceiling on the bytes actually
      read rather than on a ``stat`` of a path that may no longer be the same
      file.

    Returns the bytes, or ``None`` when the read is refused -- which the caller
    treats as "no exclusions", the same degradation as a malformed file.

    ``hooks`` is imported here rather than at module scope so the matcher itself
    stays stdlib-only, and the heavy module is pulled in only when a rule file
    actually exists.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        return safe_read_file_bytes_nolink(str(path), str(root), max_bytes=MAX_FILE_BYTES)
    except FileTooLargeError:
        logger.warning("Ignoring %s: larger than %d bytes", path, MAX_FILE_BYTES)
        return None


def load(root: str | os.PathLike[str]) -> KiroIgnore | None:
    """Load ``<root>/.kiroignore``, or ``None`` when there is nothing to apply.

    Never raises: the file is user-authored and read on every sweep, so an
    unreadable, oversized, undecodable, or guard-refused one degrades to "no
    extra exclusions" rather than failing the scan. A single unusable pattern is
    dropped and the rest of the file still applies.
    """
    try:
        path = Path(root) / KIROIGNORE_FILENAME
        # Existence fast path only, NOT the security gate: this runs on every
        # sweep and the common case is that no rule file exists. ``is_file()``
        # follows symlinks, so what may actually be read is decided by the
        # guarded read below.
        if not path.is_file():
            return None
        raw = _read_rule_file(path, root)
        if raw is None:
            return None
        # errors="replace" so a stray non-UTF-8 byte costs one pattern, not the file.
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        logger.debug("Could not read %s in %s", KIROIGNORE_FILENAME, root, exc_info=True)
        return None

    rules: list[_Rule] = []
    try:
        for line in text.splitlines():
            rule = _compile(line)
            if rule is not None:
                rules.append(rule)
            if len(rules) >= MAX_RULES:
                logger.warning(
                    "Truncating %s at %d rules", KIROIGNORE_FILENAME, MAX_RULES)
                break
    except Exception:
        logger.debug("Could not parse %s in %s", KIROIGNORE_FILENAME, root, exc_info=True)
        return None
    return KiroIgnore(rules) if rules else None

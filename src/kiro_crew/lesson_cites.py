"""Code provenance for lessons: which files a lesson describes, and whether they moved.

A lesson that names a file, symbol or behaviour of a repository is only as good as
that code. Nothing recorded what the lesson was derived from, so a rule about a file
that has since been rewritten kept arriving as a standing rule. This module is the
shared half of the fix. At WRITE time it records each cited file's content hash and
the commit the repository was at; at PROMPT-BUILD time it reads the same files again
and classifies the row:

* ``missing``: a cited path does not resolve. The row is withheld.
* ``changed``: the path resolves and its content differs. The row is injected with a
  marker, because a file that changed is usually still described correctly.
* ``current`` or no cites: the row renders exactly as it always did.

An existence check alone flags almost nothing: cited code mostly moves rather than
vanishes, so a content hash is what catches the common case.

Everything here is plain file I/O. Prompt assembly runs under the gateway's stall
budget, and a subprocess per row (``git diff``) is the cost this module
exists to avoid; the commit is read from ``.git`` directly for the same reason.
Every path goes through :func:`kiro_crew.project_scope.resolve_in_project`, so a
cite is held to the gate every other repository-scoped entry is held to: absolute
project only, bounded at the repository root, no symlink out, no sensitive target.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.platform import governance_profiles
from kiro_crew.project_scope import find_repo_root, resolve_in_project, scope_is_admissible

__all__ = [
    "CHANGED",
    "CITE_MAX",
    "CITE_PATH_MAX",
    "CURRENT",
    "FILES_PER_BUILD",
    "MISSING",
    "TEXT_SOURCE",
    "CapturedCites",
    "CiteReview",
    "capture_cites",
    "changed_marker",
    "cite_session",
    "fit_withheld",
    "governed_may_read",
    "normalize_cited_commit",
    "normalize_cites",
    "commit_in_repo",
    "read_head_commit",
    "reconcile_cites",
    "reconcile_commit",
    "run_for_session",
    "withheld_notice",
]

MISSING = "missing"
UNCHECKED = "unchecked"
CHANGED = "changed"
CURRENT = "current"

#: Marks a cite recorded because the rule or NOT-clause mentioned the file, not because
#: the writer named it. Such a cite only ever annotates: a prose mention is often an
#: example, so a rule is never withheld on its account.
TEXT_SOURCE = "text"

#: Most files one lesson may cite. A lesson is a short rule; a long list is a
#: sign the writer cited a directory's worth of files rather than the one it is about.
CITE_MAX = 8

#: Longest repo-relative path a cite may carry. Every structure that bounds a cite's
#: path (the stored value, the route schema, the tool schema) reads this one constant.
CITE_PATH_MAX = 256

#: Most distinct cited paths one lesson render examines. Every cited file is authorized,
#: probed and read on every build, under the gateway's stall budget, so the cost is
#: bounded per render rather than per lesson (a protected-context refit renders again and
#: starts a fresh count). A row citing a path past the budget is left
#: unchecked and renders as it always did.
FILES_PER_BUILD = 64

#: Largest file a cite may hash. Prompt assembly rehashes every cited file, so the
#: bound is what keeps one huge file from stalling it. A file past it cannot be cited.
_HASH_LIMIT = 1 << 20

# The characters a cited path may use. Stricter than the scope gate's segment rule,
# which only forbids separators: a path is rendered into the marker that follows a
# lesson in the prompt, so it must not carry brackets, quotes, whitespace or a
# newline that could close the lesson's frame or open another.
_CITE_PATH_RE = re.compile(r"^[\w.@+-]+(?:/[\w.@+-]+)*\Z")

# A path-looking token in free text: an optional directory prefix and a file extension,
# not part of a longer word, URL or absolute path. A token that does not resolve to a
# file in the project is prose and is ignored, so a bare ``version 1.2`` costs nothing.
# A trailing ``:123`` line suffix is left out of the match.
_TEXT_PATH_RE = re.compile(
    r"(?<![\w./~:@+-])(?:[\w.@+-]+/)*[\w.@+-]*\w\.[A-Za-z0-9]{1,8}(?![\w/@+-])"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

# Git metadata reads. Both files live inside the checkout, so their size is not
# ours to trust; a real HEAD or ref is one short line.
_HEAD_READ_LIMIT = 512
_PACKED_REFS_READ_LIMIT = 1 << 20

# Pack index files searched for a commit. A repository packs into a handful of
# files; the bound keeps a hostile ``objects/pack`` from turning one lookup into an
# unbounded directory scan.
_MAX_PACK_INDEXES = 64

# Entries read from ``objects/pack`` while looking for those indexes: a pack directory
# holds a few files per pack, so the bound is a ceiling on a hostile listing.
_MAX_PACK_ENTRIES = 1024
_IDX_V2_MAGIC = b"\xfftOc\x00\x00\x00\x02"
_IDX_FANOUT_END = 8 + 256 * 4


# The session whose prompt is being built, for the read authorization a recheck asks.
# A lesson renderer is called from several places and takes no session, so the context
# builder names it for the duration of the render rather than threading a parameter
# through every signature.
_CITE_SESSION: ContextVar[str] = ContextVar("lesson_cite_session", default="")
# The agent running that session, which a profile can bind to. Left as it was when a
# nested render names only the session.
_CITE_AGENT: ContextVar[str] = ContextVar("lesson_cite_agent", default="")


@contextmanager
def cite_session(session_key: str | None, agent: str | None = None) -> Iterator[None]:
    """Name *session_key* (and, when known, its *agent*) as who a lesson render is for."""
    session_token = _CITE_SESSION.set(session_key or "")
    agent_token = _CITE_AGENT.set(agent or "") if agent is not None else None
    try:
        yield
    finally:
        if agent_token is not None:
            _CITE_AGENT.reset(agent_token)
        _CITE_SESSION.reset(session_token)


def _deny_all(path: Path) -> bool:
    return False


def run_for_session(
    session_key: str | None,
    fn: Callable[..., Any],
    *args: Any,
    _cite_agent: str | None = None,
    **kwargs: Any,
) -> Any:
    """Call *fn* with *session_key* (and its agent) named as who a lesson render is for.

    For a caller that hands the render to a worker (an executor does not carry the
    caller's context), so the worker names the session itself.
    """
    with cite_session(session_key, _cite_agent):
        return fn(*args, **kwargs)


def governed_may_read(session_key: str, agent: str = "") -> Callable[[Path], bool]:
    """The ``filesystem.read`` authorization a cited file is held to.

    Hashing a cited file is a read the gateway makes on the agent's behalf, outside the
    tool gate that governs ``fs_read``, so it asks the same ceiling and profile, at
    write and again at every recheck. Each decision, grant or denial, lands in the SEL
    trail through ``vet_and_audit``, and an evaluation error denies: a wrong permit
    reads a file the operator excluded. A denied cite is not read; at write it is
    refused, and at a recheck its row is left unchecked.
    """

    def may_read(path: Path) -> bool:
        decision = governance_profiles.vet_and_audit(
            "filesystem.read",
            str(path),
            session_key=session_key,
            agent=agent,
            tool_name="learn_add",
            fail_closed=True,
            log_warning=False,
        )
        return bool(getattr(decision, "permitted", False))

    return may_read


def normalize_cites(raw: object) -> list[dict[str, str]] | None:
    """The valid cites in *raw*, or None when there are none.

    Each cite is ``{"path": <repo-relative path>, "sha256": <hex digest>}``, plus
    ``"source": "text"`` when the writer did not name the file and the rule's own text
    did (see :data:`TEXT_SOURCE`). An entry
    that is not that shape, names a path the scope gate would never accept, carries a
    character outside the marker-safe set, or repeats a path is dropped, and the list
    is cut at :data:`CITE_MAX`. This runs on the READ path, where a hand-edited or
    imported row must not raise while a prompt is being assembled, and on the write
    path, so a stored cite is always one a reader accepts.
    """
    if not isinstance(raw, list):
        return None
    cites: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            continue
        if not _valid_path(path) or _SHA256_RE.match(digest) is None or path in seen:
            continue
        seen.add(path)
        cite = {"path": path, "sha256": digest}
        if item.get("source") == TEXT_SOURCE:
            cite["source"] = TEXT_SOURCE
        cites.append(cite)
        if len(cites) == CITE_MAX:
            break
    return cites or None


def reconcile_cites(
    stored: list[dict[str, str]] | None, submitted: list[dict[str, str]] | None
) -> list[dict[str, str]] | None:
    """The cites a row carries after a re-submit of its rule.

    A submission that names a file explicitly states the cites outright, so it
    replaces the stored set (which is also how a cite is dropped or re-anchored). A
    submission made of text-derived cites alone states nothing: the writer only
    repeated a rule whose text happens to name a file, so it can add a file the row
    does not cite yet but never replaces, re-hashes or demotes a stored cite.
    """
    if submitted is None:
        return stored
    if not stored or any(cite.get("source") != TEXT_SOURCE for cite in submitted):
        return submitted
    known = {cite["path"] for cite in stored}
    merged = stored + [cite for cite in submitted if cite["path"] not in known]
    return merged[:CITE_MAX]


def reconcile_commit(
    stored: str | None, submitted: list[dict[str, str]] | None, commit: str | None
) -> str | None:
    """The commit that goes with :func:`reconcile_cites`' result.

    An explicit submission brings the commit its hashes were taken at. A text-only
    submission added cites to an existing set, so the stored commit stays.
    """
    if submitted is not None and any(c.get("source") != TEXT_SOURCE for c in submitted):
        return commit
    return stored or commit


def normalize_cited_commit(raw: object) -> str | None:
    """*raw* when it is a git object name, else None."""
    return raw if isinstance(raw, str) and _COMMIT_RE.match(raw) else None


def _valid_path(path: str) -> bool:
    return (
        len(path) <= CITE_PATH_MAX
        and _CITE_PATH_RE.match(path) is not None
        and scope_is_admissible(path)
    )


def _cite_base(project_dir: str | Path | None) -> Path | None:
    """The directory cites are resolved against, or None when there is no project.

    The repository root, not the session's working directory. A cite names a file
    relative to the repository, so a session started in a subdirectory must read the
    same file a session started at the root does; resolving from the working directory
    would let the same cite hash two different files. A project outside any repository
    resolves against itself.
    """
    if not project_dir:
        return None
    root = find_repo_root(project_dir)
    if root is not None:
        return root
    try:
        given = Path(project_dir)
        return given.resolve() if given.is_absolute() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _file_digest(path: Path) -> str | None:
    """SHA-256 of *path*, or None when it cannot be read or exceeds the bound.

    Read through ``hooks.safe_read_prefix``: the same sensitive-path refusal and
    no-follow open every other agent-influenced read uses, and a non-regular file
    (a FIFO, a device) is refused rather than blocked on.
    """
    from kiro_crew import hooks  # circular import: hooks -> validation -> this module

    data = hooks.safe_read_prefix(str(path), _HASH_LIMIT + 1)
    if data is None or len(data) > _HASH_LIMIT:
        return None
    return hashlib.sha256(data).hexdigest()


def _asked_once(may_read: Callable[[Path], bool] | None) -> Callable[[Path], bool] | None:
    """*may_read* answering each path once, so one lookup audits a file once."""
    if may_read is None:
        return None
    answers: dict[str, bool] = {}

    def ask(path: Path) -> bool:
        key = str(path)
        if key not in answers:
            answers[key] = bool(may_read(path))
        return answers[key]

    return ask


def _no_link_between(base: Path, path: Path) -> bool:
    """Whether no component of *path* below *base* is a symlink or junction.

    Authorization is asked about the name a metadata file is reached by, and the
    guarded reader then opens whatever that name resolves to, so a link under ``.git``
    would let the two differ. Git does not write links for its metadata, so a path with
    one is not read. *base* is a directory already known not to be linked.
    """
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return False
    current = base
    for part in parts:
        current = current / part
        if _is_linked(current):
            return False
    return True


def _read_git_text(
    path: Path,
    limit: int,
    may_read: Callable[[Path], bool] | None = None,
    below: Path | None = None,
) -> str | None:
    """At most *limit* bytes of a git metadata file as text, or None.

    Content that fills the bound counts as truncated and is refused, so a caller
    never acts on a value cut mid-token. With *may_read* the file is authorized before
    it is read, and a denial reads as an unreadable file.
    """
    if below is not None and not _no_link_between(below, path):
        return None
    if may_read is not None and not may_read(path):
        return None
    from kiro_crew import hooks  # circular import: hooks -> validation -> this module

    raw = hooks.safe_read_prefix(str(path), limit)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if len(text) >= limit else text


def _local_target(base: Path, raw: str) -> Path | None:
    """The path *raw* names under *base*, or None when it is not a plain local path.

    A pointer in ``.git`` is text a checkout supplies. On Windows a UNC or device path
    names a host, and resolving or stat-ing it opens an outbound SMB connection, so
    such a target is refused on the raw text, before any ``Path`` operation touches the
    filesystem.
    """
    from kiro_crew import hooks  # circular import: hooks -> validation -> this module

    text = raw.strip()
    if not text or hooks.is_unc_shape(text):
        return None
    target = Path(text)
    # Normalized as a string, never resolved: ``..`` segments are folded lexically, so
    # the link checks below see the path exactly as it will be opened.
    return Path(os.path.normpath(str(target if target.is_absolute() else base / target)))


def _is_linked(path: Path) -> bool:
    """Whether *path* is a symlink or, on Windows, a junction (``lstat`` only)."""
    try:
        return platform_compat.is_link_or_junction(path)
    except (OSError, ValueError):
        return True


def _unlinked(path: Path) -> bool:
    """Whether neither *path* nor any ancestor of it is a link or junction.

    Tested root-first by ``lstat``, so no probe traverses a link on the way to the
    next. On Windows a junction can name a UNC share, and the first ``is_dir()`` or
    ``resolve()`` through it opens an outbound SMB connection that authenticates as
    this process; a lexical UNC screen cannot see it, because the path is local-shaped.
    A ``.git`` location reached through a link is not read.
    """
    return platform_compat.first_linked_ancestor(path) is None and not _is_linked(path)


def _plain_dir(path: Path) -> bool:
    """Whether *path* is a real directory, never a link or junction."""
    try:
        return not _is_linked(path) and path.is_dir()
    except OSError:
        return False


def _git_dirs(
    project_dir: str | Path | None, may_read: Callable[[Path], bool] | None = None
) -> tuple[Path, Path] | None:
    """``(git directory, common directory)`` of *project_dir*'s repository, or None.

    A worktree's ``.git`` is a FILE pointing at its own git directory, whose
    ``commondir`` names the directory that holds the shared refs and objects. In an
    ordinary repository the two are the same directory.
    """
    root = find_repo_root(project_dir)
    if root is None:
        return None
    dotgit = root / ".git"
    try:
        if _is_linked(dotgit):
            return None
        if dotgit.is_dir():
            git_dir = dotgit
        else:
            pointer = _read_git_text(dotgit, _HEAD_READ_LIMIT, may_read)
            if pointer is None or not pointer.startswith("gitdir:"):
                return None
            target = _local_target(root, pointer[len("gitdir:") :])
            if target is None or not _unlinked(target) or not target.is_dir():
                return None
            git_dir = target
        common = git_dir
        common_pointer = _read_git_text(git_dir / "commondir", _HEAD_READ_LIMIT, may_read, git_dir)
        if common_pointer is not None and common_pointer.strip():
            candidate = _local_target(git_dir, common_pointer)
            if candidate is not None and _unlinked(candidate) and candidate.is_dir():
                common = candidate
    except (OSError, RuntimeError, ValueError):
        return None
    return git_dir, common


def _pack_index_has(
    idx: Path, wanted: bytes, may_read: Callable[[Path], bool] | None = None
) -> bool:
    """Whether the version-2 pack index *idx* lists the object name *wanted*.

    A binary search over the sorted name table, reading the fanout and a few
    names rather than the file: the index of a large repository is hundreds of
    megabytes. The file sits in the checkout, so every read goes through
    ``hooks.safe_read_range`` (sensitive-path refusal, non-blocking no-reparse open),
    and a read that comes back short, or a fanout that disagrees with itself,
    answers False.
    """
    from kiro_crew import hooks  # circular import: hooks -> validation -> this module

    if _is_linked(idx):
        return False
    if may_read is not None and not may_read(idx):
        return False
    path = str(idx)
    width = len(wanted)
    head = hooks.safe_read_range(path, 0, _IDX_FANOUT_END)
    if head is None or len(head) < _IDX_FANOUT_END or not head.startswith(_IDX_V2_MAGIC):
        return False
    try:
        fanout = struct.unpack(">256I", head[8:])
    except struct.error:
        return False
    first = wanted[0]
    low = fanout[first - 1] if first else 0
    high = fanout[first]
    if not low <= high <= fanout[255]:
        return False
    while low < high:
        mid = (low + high) // 2
        found = hooks.safe_read_range(path, _IDX_FANOUT_END + mid * width, width)
        if found is None or len(found) != width:
            return False
        if found == wanted:
            return True
        if found < wanted:
            low = mid + 1
        else:
            high = mid
    return False


def commit_in_repo(
    project_dir: str | Path | None,
    commit: str,
    may_read: Callable[[Path], bool] | None = None,
) -> bool:
    """Whether *commit* is an object of *project_dir*'s repository.

    A lesson records the commit its repository was at, so a repository that holds
    that commit is the repository the lesson was written in, and a repository that
    does not may be another checkout. Looks for a loose object, then in each pack
    index, by plain file reads; there is no subprocess. Alternate object stores are
    not followed, so a repository that borrows its history answers False and is
    treated as unestablished, which renders its lessons as they always did.

    With *may_read*, every metadata file is authorized before it is read, so a lookup
    reads no more of ``.git`` than the session may; a denied file reads as absent.
    """
    if _COMMIT_RE.match(commit) is None:
        return False
    ask = _asked_once(may_read)
    dirs = _git_dirs(project_dir, ask)
    if dirs is None:
        return False
    objects = dirs[1] / "objects"
    try:
        if not _plain_dir(objects):
            return False
        loose = objects / commit[:2] / commit[2:]
        if (
            _plain_dir(loose.parent)
            and not _is_linked(loose)
            and (ask is None or ask(loose))
            and loose.is_file()
        ):
            return True
        pack = objects / "pack"
        if not _plain_dir(pack):
            return False
        # The bounds are applied as the directory is read, so a hostile pack directory
        # costs at most _MAX_PACK_ENTRIES listings and _MAX_PACK_INDEXES retained
        # indexes, never a listing of all of it.
        entries = islice(pack.iterdir(), _MAX_PACK_ENTRIES)
        indexes = sorted(islice((p for p in entries if p.suffix == ".idx"), _MAX_PACK_INDEXES))
    except OSError:
        return False
    wanted = bytes.fromhex(commit)
    return any(_pack_index_has(idx, wanted, ask) for idx in indexes)


def read_head_commit(
    project_dir: str | Path | None, may_read: Callable[[Path], bool] | None = None
) -> str | None:
    """The commit HEAD names in *project_dir*'s repository, or None when unknown.

    Reads ``.git`` directly rather than spawning ``git rev-parse``; see the module
    docstring. A worktree's ``.git`` is a FILE pointing at its own git directory, whose
    ``commondir`` names the directory that holds the shared refs, and both forms are
    followed. Every failure answers None: the commit is provenance, never a reason to
    refuse a write. With *may_read* every metadata file is authorized before it is
    read, and a denied file reads as unreadable.
    """
    ask = _asked_once(may_read)
    dirs = _git_dirs(project_dir, ask)
    if dirs is None:
        return None
    git_dir, common = dirs
    head = _read_git_text(git_dir / "HEAD", _HEAD_READ_LIMIT, ask, git_dir)
    if head is None:
        return None
    head = head.strip()
    if not head.startswith("ref:"):
        return head if _COMMIT_RE.match(head) else None
    ref = head[len("ref:") :].strip()
    if not ref.startswith("refs/") or ".." in ref.split("/"):
        return None
    for base in (git_dir, common):
        loose = _read_git_text(base / ref, _HEAD_READ_LIMIT, ask, base)
        if loose is not None and _COMMIT_RE.match(loose.strip()):
            return loose.strip()
    packed = _read_git_text(common / "packed-refs", _PACKED_REFS_READ_LIMIT, ask, common)
    if packed is not None:
        for line in packed.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _COMMIT_RE.match(parts[0]):
                return parts[0]
    return None


@dataclass(frozen=True)
class CapturedCites:
    """What a lesson write records about the code it cites.

    ``cites`` and ``cited_commit`` are None when there is nothing to record, so a
    lesson with no cites is stored exactly as it was before this field existed.
    ``refused`` is ``(path, reason)`` for each explicit cite that was not recorded;
    the lesson is saved without it. ``from_text`` names the cites recorded because the
    rule or NOT-clause mentioned them, which the writer did not choose, so the write
    surfaces say so.
    """

    cites: list[dict[str, str]] | None = None
    cited_commit: str | None = None
    refused: tuple[tuple[str, str], ...] = ()
    from_text: tuple[str, ...] = ()


def capture_cites(
    explicit: Iterable[str],
    texts: Iterable[str],
    project_dir: str | Path | None,
    may_read: Callable[[Path], bool] | None = None,
) -> CapturedCites:
    """Record the files a lesson being written cites.

    *explicit* are the paths the writer named; each one that cannot be recorded is
    refused BY NAME with a reason. *texts* (the rule and its NOT-clause) are scanned
    for path-looking tokens, and a token that resolves to a readable file inside the
    project is recorded too; one that does not is prose and is ignored silently.

    Every refusal of an explicit cite carries one of three reasons, none of which says
    whether the path exists: a path that is missing, one that leaves the project
    through a symlink and one that names a sensitive file all read "does not resolve
    to a file inside the project", so the refusal is not an oracle for paths the
    writer may not read.
    """
    base = _cite_base(project_dir)
    cites: list[dict[str, str]] = []
    seen: set[str] = set()
    refused: list[tuple[str, str]] = []
    from_text: list[str] = []

    def record(path: str, source: str | None = None) -> str | None:
        """Add *path*, or return the reason it was not added."""
        if path in seen:
            return None
        if len(cites) >= CITE_MAX:
            return f"a lesson may cite at most {CITE_MAX} files"
        if not _valid_path(path):
            return "not a repo-relative file path"
        if base is None:
            return "the session has no project to resolve it against"
        # *may_read* is the caller's own read authorization (the gateway's filesystem
        # governance), asked BEFORE the path is probed. A denial reads exactly like a
        # missing path, so it neither reads the file nor says that it exists.
        if may_read and not may_read(base / path):
            return "does not resolve to a file inside the project"
        # A cite is a path inside the repository, not a way through a link or junction
        # (which on Windows can name a UNC share, and resolving it authenticates outbound).
        if not _no_link_between(base, base / path):
            return "does not resolve to a file inside the project"
        resolved = resolve_in_project(path, base)
        if resolved is None or not resolved.is_file():
            return "does not resolve to a file inside the project"
        # A link inside the project can name a target the session may not read, so
        # the file actually read is authorized too, not only the name it was reached by.
        if may_read and resolved != base / path and not may_read(resolved):
            return "does not resolve to a file inside the project"
        digest = _file_digest(resolved)
        if digest is None:
            return f"cannot be read, or is larger than {_HASH_LIMIT // 1024} KiB"
        seen.add(path)
        cite = {"path": path, "sha256": digest}
        if source:
            cite["source"] = source
        cites.append(cite)
        return None

    # One past the bound proves an overflow, so the caller's list is never walked past
    # it: what is not looked at is not resolved, hashed or held, and the overflow is
    # reported once rather than per item.
    items = list(islice(explicit, CITE_MAX + 1))
    if len(items) > CITE_MAX:
        refused_overflow = (
            "(further cites)",
            f"a lesson may cite at most {CITE_MAX} files; the rest were not looked at",
        )
        items = items[:CITE_MAX]
    else:
        refused_overflow = None
    for raw in items:
        shown = raw if isinstance(raw, str) else repr(raw)
        path = raw.strip().replace("\\", "/") if isinstance(raw, str) else ""
        reason = record(path) if path else "not a repo-relative file path"
        if reason is not None:
            refused.append((shown[:CITE_PATH_MAX], reason))
    if refused_overflow is not None:
        refused.append(refused_overflow)
    for text in texts:
        if not isinstance(text, str):
            continue
        for match in _TEXT_PATH_RE.finditer(text):
            if len(cites) >= CITE_MAX:
                break
            path = match.group(0)
            if path not in seen and record(path, TEXT_SOURCE) is None:
                from_text.append(path)
    if not cites:
        return CapturedCites(refused=tuple(refused))
    return CapturedCites(cites, read_head_commit(base, may_read), tuple(refused), tuple(from_text))


def withheld_notice(count: int) -> str:
    """The block that reports rows withheld because the code they cite is gone.

    Its own count and its own wording, never folded into a budget omission: those say
    a rule did not fit, and this says the code it was about is gone.
    """
    noun = "rule" if count == 1 else "rules"
    return (
        f"[Withheld {count} learned {noun}: the code {'it cites' if count == 1 else 'they cite'}"
        " no longer exists in this repository. This is NOT a budget limit; the stored "
        f"{noun} {'is' if count == 1 else 'are'} unchanged. Review with learn_list.]\n\n"
    )


def fit_withheld(notice: str, *budgets: int) -> tuple[str, tuple[int, ...]]:
    """*notice* and each budget reduced by its length, so the block stays in its cap.

    The withheld line is appended to the lesson block, so a renderer that spends its
    whole budget on lessons would return more than the ceiling it was handed. A
    budget of 0 means unbounded and stays 0. A notice that cannot fit under the
    smallest bounded budget is dropped rather than overrunning it.
    """
    bounded = [b for b in budgets if b]
    if notice and bounded and len(notice) >= min(bounded):
        return "", budgets
    return notice, tuple(max(1, b - len(notice)) if b else 0 for b in budgets)


def changed_marker(paths: list[str]) -> str:
    """The note that follows a lesson whose cited files changed.

    A leading space, so a caller appends it to the lesson text. At most two paths are
    named; the model needs to know WHICH code to recheck, not an inventory.
    """
    named = ", ".join(paths[:2]) + (", …" if len(paths) > 2 else "")
    return f" [cited code changed since learned: {named}]"


class CiteReview:
    """One prompt build's view of whether lessons' cited code still matches.

    Built once per context assembly and handed to every row, so a file two lessons
    cite is hashed once. ``withheld`` counts the rows :meth:`verdict` classified
    ``missing``, which a renderer reports separately from rows omitted for budget or
    scope: those answers mean "did not fit" and "not this repository", and this one
    means "the code it was about is gone".
    """

    def __init__(
        self,
        project_dir: str | Path | None,
        may_read: Callable[[Path], bool] | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._may_read = may_read
        # Resolved on the first row that cites anything: a store whose lessons cite
        # nothing never pays for the filesystem probes behind it.
        self._base: Path | None = None
        self._base_resolved = False
        self._state: dict[str, tuple[str, str | None]] = {}
        self._examined = 0
        self._commits: dict[str, bool] = {}
        self.withheld = 0

    def _resolve_base(self) -> Path | None:
        if not self._base_resolved:
            self._base = _cite_base(self._project_dir)
            self._base_resolved = True
        return self._base

    def _classify_path(self, cite: dict[str, str]) -> str:
        path = cite["path"]
        cached = self._state.get(path)
        if cached is None:
            # Each new path is charged before anything is asked or read, so a build
            # past the budget writes no further audit record and probes no further file.
            if self._examined >= FILES_PER_BUILD:
                return UNCHECKED
            self._examined += 1
            # Authorize BEFORE probing existence. A denied path answers "unchecked"
            # whether or not the file exists, so a denial is not a way to tell a
            # deleted file from one the session may not read.
            if self._base is not None and not self._read_allowed(self._base / path):
                cached = (UNCHECKED, None)
            elif self._base is not None and not _no_link_between(self._base, self._base / path):
                # Not read through a link, and not reported either way.
                cached = (UNCHECKED, None)
            else:
                resolved = resolve_in_project(path, self._base) if self._base else None
                if resolved is None or not resolved.is_file():
                    cached = (MISSING, None)
                elif (
                    self._base is not None
                    and resolved != self._base / path
                    and not self._read_allowed(resolved)
                ):
                    # A link inside the project reached a target the session may not
                    # read: the file actually read is authorized, not only its name.
                    cached = (UNCHECKED, None)
                else:
                    cached = (CURRENT, _file_digest(resolved))
            self._state[path] = cached
        state, digest = cached
        if state == UNCHECKED:
            return UNCHECKED
        if state == MISSING:
            return MISSING
        # Every stored hash was taken from a readable file within the bound, so a file
        # that now cannot be hashed (grown past the bound, unreadable) is not the one
        # the lesson was written against: it is flagged, never read as current.
        return CURRENT if digest == cite["sha256"] else CHANGED

    def _read_allowed(self, resolved: Path) -> bool:
        """Whether the session being rendered may read *resolved* under governance.

        A denied file is not hashed and its row is left unchecked, so a denial neither
        reads the file nor reports whether it changed.
        """
        if self._may_read is None:
            session = _CITE_SESSION.get()
            # No session named means no profile to ask. An empty key would resolve to
            # the policy ceiling alone and drop the caller's profile, so it denies.
            self._may_read = governed_may_read(session, _CITE_AGENT.get()) if session else _deny_all
        return self._may_read(resolved)

    def _commit_known(self, commit: str | None) -> bool:
        if not commit:
            return False
        if commit not in self._commits:
            self._commits[commit] = commit_in_repo(self._base, commit, self._read_allowed)
        return self._commits[commit]

    def _verdict(
        self,
        cites: list[dict[str, str]] | None,
        *,
        scope_satisfied: bool,
        cited_commit: str | None,
    ) -> tuple[str, list[str]] | None:
        """``(state, changed paths)`` for a row, or None when it is not checked.

        A row is checked only when its repository is established for THIS session:
        its ``repo_scope`` is satisfied, or the commit the lesson was written at is an
        object of this repository (with no commit on record, at least one cite
        resolves). Otherwise a
        session in a different checkout would read every anchored row as ``missing``
        and lose rules that were never about its code. A session with no project, and
        a row with no cites, are not checked either, so those render as they always did.
        """
        if not cites or self._resolve_base() is None:
            return None
        observed = [(cite, self._classify_path(cite)) for cite in cites]
        if any(state == UNCHECKED for _, state in observed):
            return None
        if not scope_satisfied:
            # A commit is the strongest sign the session is in the repository the
            # lesson was written in. A path that merely resolves is weaker: ``README.md``
            # exists in most repositories, so with a commit on record it is not
            # trusted, and only the commit establishes. A row with no commit falls back
            # to "at least one cite resolves".
            if cited_commit:
                established = self._commit_known(cited_commit)
            else:
                established = any(state != MISSING for _, state in observed)
            if not established:
                return None
        # A cite taken from the rule's text only ever annotates: a path in prose is
        # often an example, so its disappearance is reported as a change, never as
        # grounds to withhold the rule.
        states = [
            (
                cite["path"],
                CHANGED if state == MISSING and cite.get("source") == TEXT_SOURCE else state,
            )
            for cite, state in observed
        ]
        if any(state == MISSING for _, state in states):
            return MISSING, []
        changed = [path for path, state in states if state == CHANGED]
        return (CHANGED, changed) if changed else (CURRENT, [])

    def annotate(
        self,
        cites: list[dict[str, str]] | None,
        *,
        scope_satisfied: bool,
        cited_commit: str | None = None,
    ) -> tuple[bool, str]:
        """``(keep, note)`` for one row: whether to inject it, and what follows it.

        ``note`` is empty unless the cited code changed, so appending it leaves every
        other row byte-identical. A ``missing`` row is not kept and is counted in
        ``withheld``.
        """
        verdict = self._verdict(cites, scope_satisfied=scope_satisfied, cited_commit=cited_commit)
        if verdict is None:
            return True, ""
        state, changed = verdict
        if state == MISSING:
            self.withheld += 1
            return False, ""
        return True, changed_marker(changed) if state == CHANGED else ""

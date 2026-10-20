"""The skill catalog an agent's skill picker maps onto, and reading a skill's files.

``walk_skill_catalog`` enumerates the skills under the trusted roots once per settled
directory state; skill keys and URIs convert through it, an agent's skill views and
mapping are read and written against it, and ``list_skill_tree`` and
``read_skill_file`` serve a skill's files within their caps.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew.agent_discovery import (
    SKILL_URI_PREFIX,
    expand_skill_uri,
    skill_resource_uris,
    skill_uri_for_path,
)
from kiro_crew.dashboard.handlers._shared_skill_roots import (
    LEAF_SYMLINK_PREFIX,
    _canonical_skill_roots,
    _edition_package_roots,
    _leaf_is_contained,
    _path_at_or_under,
    _resolved_set,
)
from kiro_crew.dashboard.handlers._shared_workspace import active_project_dir
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.security import is_sensitive_path

# ── Agent-template skill mapping (skill:// resources <-> catalog keys) ──


# Upper bound on how many skills one agent template may map. Each mapped skill
# is a full SKILL.md that kiro-cli loads into the agent's context, so an
# unbounded list is a context-exhaustion footgun, not a feature.
MAX_AGENT_SKILLS = 100


# fnmatch metacharacters. A URI containing any of these matches a SET of skills
# ("every skill in this root"), which has no single catalog key — such entries
# are surfaced read-only and preserved verbatim across edits.
_GLOB_CHARS = ("*", "?", "[")


def _skill_key_roots(state: DashboardState, session_key: str = "") -> list[tuple[str, Path]]:
    """``(key_prefix, root)`` pairs for every location skills are keyed from.

    Mirrors :func:`_resolve_skill_root`'s roots, in the same precedence order,
    so an enumerated key names the same directory that function would resolve.
    Roots that cannot exist in this deployment (no active project dir, no
    edition roots) are omitted. *session_key* scopes the ``kiro-workspace/``
    root to the requesting chat slot's project, exactly as
    :func:`_resolve_skill_root` does — the two MUST agree or an enumerated key
    would not resolve.
    """
    out: list[tuple[str, Path]] = [("kiro-user/", Path.home() / ".kiro" / "skills")]
    proj = active_project_dir(state, session_key)
    if proj is not None:
        out.append(("kiro-workspace/", proj / ".kiro" / "skills"))
    out.extend(("", root) for root in _canonical_skill_roots()[1:])
    # ``package/`` covers only the edition roots the core does not already key
    # above — via the same helper the resolver uses, so enumeration and
    # resolution cannot drift apart. A key the catalog offers must be one the
    # resolver accepts, and vice versa.
    canonical = _resolved_set(root for _prefix, root in out)
    out.extend(("package/", root) for root in _edition_package_roots(canonical))
    return out


# Skills may sit under category directories (``utils/tiny-url``). Bound the
# enumeration walk so a deep or pathological tree cannot turn one PATCH into an
# unbounded filesystem crawl. Three levels covers every layout in use.
_SKILL_NEST_DEPTH = 3


def _collect_skills_under(
    directory: Path,
    root: Path,
    root_resolved: Path,
    prefix: str,
    out: dict[str, Path],
    depth: int,
    allow_leaf_symlink: bool = False,
    mtimes: dict[Path, int | None] | None = None,
) -> None:
    """Add every ``<dir>/SKILL.md`` at or under *directory* to *out*.

    Containment mirrors :func:`_resolve_skill_root` through the same
    :func:`_leaf_is_contained` predicate, because a key this walk offers must be
    one that function accepts: a candidate's *parent* must resolve at or under
    the trusted root, and so must the candidate itself unless the prefix is
    :data:`LEAF_SYMLINK_PREFIX` or the target is an app skill provider root.
    Without that agreement an escaping leaf would be enumerated as a phantom key
    the resolver refuses, and its ``skill://`` URI would name a file outside the
    root.
    Sensitive paths are rejected before and after symlink resolution.

    *mtimes*, when given, receives the modification time of every directory
    this walk reads -- *directory* itself and each skill directory whose
    ``SKILL.md`` it checks -- taken BEFORE the read, so a change that lands
    between the stat and the read still shows as a change afterwards. It is
    what :meth:`SkillCatalogSnapshot.changed` compares against.
    """
    if depth <= 0:
        return
    if mtimes is not None:
        mtimes[directory] = _dir_mtime_ns(directory)
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if is_sensitive_path(str(entry)):
            continue
        try:
            parent_resolved = entry.parent.resolve(strict=True)
            entry_resolved = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            # A symlink loop raises RuntimeError on some CPythons and OSError on
            # others, and this walk answers a request: an entry whose identity
            # cannot be established is skipped like any other uncontainable one,
            # never propagated as a 500 (matching :func:`_resolved_set`).
            continue
        if not _path_at_or_under(parent_resolved, root_resolved):
            continue
        if not _leaf_is_contained(entry_resolved, root_resolved, allow_leaf_symlink):
            continue
        if mtimes is not None:
            mtimes[entry] = _dir_mtime_ns(entry)
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            try:
                target = skill_md.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if is_sensitive_path(str(target)):
                continue
            # First root wins, matching _skill_key_roots precedence.
            out.setdefault(prefix + entry.relative_to(root).as_posix(), skill_md)
        else:
            _collect_skills_under(
                entry, root, root_resolved, prefix, out, depth - 1, allow_leaf_symlink, mtimes
            )


def _dir_mtime_ns(path: Path) -> int | None:
    """``st_mtime_ns`` of *path*, or ``None`` when it cannot be stat'ed (absent)."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


# A directory's mtime comes from the kernel's coarse clock (a jiffy on Linux; a
# whole second on some filesystems, two on FAT), so a change landing in the same
# tick as the one a walk recorded leaves the mtime equal. A directory modified
# this close to the walk is therefore treated as changed rather than trusted.
_CATALOG_SETTLE_NS = 2_000_000_000


class SkillCatalogSnapshot(NamedTuple):
    """One walk of the skill roots: its keys, and what it read to build them.

    ``entries`` is the catalog (see :func:`enumerate_skill_catalog`).
    ``dir_mtimes`` is the modification time of every directory the walk read
    (each root, absent ones as ``None``; each category directory it descended
    into; each skill directory whose ``SKILL.md`` it checked), and
    ``walked_at_ns`` the wall clock when the walk began.
    """

    entries: dict[str, Path]
    dir_mtimes: dict[Path, int | None]
    walked_at_ns: int

    def changed(self) -> bool:
        """Whether the skill roots may hold a different catalog now.

        One ``stat`` per directory the walk read and no walk: ``True`` when any
        of them has a different mtime (a skill directory created or removed, a
        ``SKILL.md`` added to or taken from one, a root that appeared), or when
        one was modified within :data:`_CATALOG_SETTLE_NS` of the walk, where an
        equal mtime cannot prove nothing landed. ``False`` means the snapshot's
        ``entries`` are still what a walk would return.
        """
        settled_before = self.walked_at_ns - _CATALOG_SETTLE_NS
        for path, recorded in self.dir_mtimes.items():
            current = _dir_mtime_ns(path)
            if current != recorded:
                return True
            if current is not None and current >= settled_before:
                return True
        return False


def walk_skill_catalog(state: DashboardState, session_key: str = "") -> SkillCatalogSnapshot:
    """Walk the skill roots once; return the catalog with its staleness stamp.

    The walk :func:`enumerate_skill_catalog` performs, plus the directory mtimes
    :meth:`SkillCatalogSnapshot.changed` needs. A caller that must answer off a
    catalog some time after it walked (the agent PATCH's receipt) keeps the
    snapshot and re-walks only when the check says the roots moved. The check is
    worth carrying because a walk is two orders of magnitude dearer than it: each
    entry passes the sensitive-path check on top of its ``resolve`` calls, so a
    200-skill tree walks in hundreds of milliseconds and re-checks in about one
    (``test/skill_catalog_walk_bench.py`` measures both).
    """
    catalog: dict[str, Path] = {}
    mtimes: dict[Path, int | None] = {}
    walked_at_ns = time.time_ns()
    for prefix, root in _skill_key_roots(state, session_key):
        if is_sensitive_path(str(root)):
            continue
        if not root.is_dir():
            # Recorded as absent so a root created later reads as a change.
            mtimes[root] = None
            continue
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            mtimes[root] = None
            continue
        _collect_skills_under(
            root,
            root,
            root_resolved,
            prefix,
            catalog,
            _SKILL_NEST_DEPTH,
            prefix == LEAF_SYMLINK_PREFIX,
            mtimes,
        )
    return SkillCatalogSnapshot(catalog, mtimes, walked_at_ns)


def enumerate_skill_catalog(state: DashboardState, session_key: str = "") -> dict[str, Path]:
    """Map every discoverable catalog key to its ``SKILL.md`` path.

    Built by **enumerating** the skill roots, never by joining a caller-supplied
    string onto one. That is the security property this function exists for: the
    only paths the agent-template editor can ever hand to the filesystem or
    write into an agent spec are paths this walk discovered, so a hostile or
    traversing key (``../../.ssh``, an absolute path, a ``~`` prefix) can do
    nothing but miss a dict lookup. Allowlist by enumeration rather than
    validate-then-join — it also removes the tainted-path dataflow that
    validate-then-join leaves for static analysis to flag.

    *session_key* only selects which project's ``kiro-workspace/`` root joins
    the walk (see :func:`_skill_key_roots`); it never widens the enumeration
    property above. Results are computed per call — nothing is cached — so a
    per-session root cannot leak into another session's catalog.

    It is additionally the single source of truth for BOTH directions of the
    key <-> URI mapping, so they cannot disagree: a mapping written against a
    symlinked skill directory inverts back to the same key it was written from.

    The walk itself is :func:`walk_skill_catalog`; this is its catalog alone.
    """
    return walk_skill_catalog(state, session_key).entries


def skill_key_for_uri(
    uri: str,
    agent_path: Path,
    state: DashboardState,
    catalog: dict[str, Path] | None = None,
    session_key: str = "",
) -> str | None:
    """Invert a ``skill://`` resource URI back to a catalog key, or ``None``.

    ``None`` means "not editable through the catalog" — a wildcard pattern, or a
    path that no enumerated skill accounts for (a hand-authored URI, or a skill
    that has since been deleted). Callers preserve those verbatim instead of
    rewriting or dropping them.

    Pass *catalog* (from :func:`enumerate_skill_catalog`) to reuse one walk
    across many URIs.
    """
    if any(c in uri for c in _GLOB_CHARS):
        return None
    expanded = expand_skill_uri(uri, agent_path)
    if not expanded:
        return None
    entries = catalog if catalog is not None else enumerate_skill_catalog(state, session_key)
    wanted = Path(expanded)
    for key, path in entries.items():
        if path == wanted:
            return key
    # Fall back to comparing resolved targets so a URI written against a
    # symlinked skill directory (or against its target) still inverts.
    try:
        target = wanted.resolve(strict=True)
    except OSError:
        return None
    for key, path in entries.items():
        try:
            if path.resolve(strict=True) == target:
                return key
        except OSError:
            continue
    return None


def skill_uri_for_key(
    key: str,
    state: DashboardState,
    catalog: dict[str, Path] | None = None,
    session_key: str = "",
) -> str | None:
    """Resolve a catalog key to the ``skill://`` URI for its ``SKILL.md``.

    A miss returns ``None`` — the key names no discoverable skill. Because the
    lookup goes through :func:`enumerate_skill_catalog` rather than joining
    *key* onto a root, an arbitrary caller-supplied key can never widen an
    agent's resources beyond the enumerated skill trees.

    Pass *catalog* to reuse one walk across many keys.
    """
    entries = catalog if catalog is not None else enumerate_skill_catalog(state, session_key)
    skill_md = entries.get(key)
    if skill_md is None:
        return None
    # The one renderer the builtin-agent installers call too, so the editor's
    # mappings and theirs cannot drift into two forms ``expand_skill_uri`` treats
    # differently.
    return skill_uri_for_path(skill_md)


def agent_skill_views(
    data: dict[str, Any],
    agent_path: Path,
    state: DashboardState,
    session_key: str = "",
    catalog: dict[str, Path] | None = None,
) -> tuple[list[str], list[str]]:
    """``(catalog_keys, unmanaged_uris)`` for *data*, from ONE catalog walk.

    The two views partition the agent's ``skill://`` resources: keys the editor
    owns and can rewrite, and URIs it cannot express (wildcards, or paths no
    enumerated skill accounts for) which are shown read-only and preserved on
    every write. Both are order-preserving; keys are de-duplicated.

    Pass *catalog* (from :func:`enumerate_skill_catalog`) to reuse a walk the
    caller already did instead of enumerating the roots again.

    Filesystem-heavy (it enumerates the skill roots) — callers on the asyncio
    event loop MUST run this off the loop.
    """
    entries = catalog if catalog is not None else enumerate_skill_catalog(state, session_key)
    keys: list[str] = []
    unmanaged: list[str] = []
    seen: set[str] = set()
    for uri in skill_resource_uris(data):
        key = skill_key_for_uri(uri, agent_path, state, entries)
        if key is None:
            unmanaged.append(uri)
        elif key not in seen:
            seen.add(key)
            keys.append(key)
    return keys, unmanaged


def agent_skill_keys(
    data: dict[str, Any],
    agent_path: Path,
    state: DashboardState,
    session_key: str = "",
    catalog: dict[str, Path] | None = None,
) -> list[str]:
    """Catalog keys for the skills *data* maps, de-duplicated, order-preserving.

    Only catalog-resolvable entries are returned — this is the set the Agent
    Templates editor owns and can rewrite. Wildcard / hand-authored URIs are
    excluded here and reported separately by :func:`agent_unmanaged_skill_uris`.
    Pass *catalog* to reuse one walk, as for :func:`agent_skill_views`.
    """
    return agent_skill_views(data, agent_path, state, session_key, catalog)[0]


def agent_unmanaged_skill_uris(
    data: dict[str, Any], agent_path: Path, state: DashboardState, session_key: str = ""
) -> list[str]:
    """``skill://`` URIs that the catalog editor cannot express, in order.

    Wildcards, and paths no enumerated skill accounts for. Surfaced read-only in
    the UI and preserved on every write so editing an agent through the dashboard
    never silently drops a hand-authored mapping.
    """
    return agent_skill_views(data, agent_path, state, session_key)[1]


def apply_skill_mapping(
    data: dict[str, Any],
    agent_path: Path,
    state: DashboardState,
    keys: list[str],
    session_key: str = "",
) -> tuple[list[str], list[str], list[str], SkillCatalogSnapshot]:
    """Rewrite *data*'s ``skill://`` resources to *keys*, in place.

    Returns ``(applied_keys, unknown_keys, applied_uris, snapshot)``. ``applied_uris[i]``
    is the ``skill://`` resource written for ``applied_keys[i]``, in request order --
    the one statement of which entries of the rewritten list are the managed ones,
    resolved against the same catalog walk that validated the keys, so a caller
    that must re-apply the request's order onto a later read of the spec never
    has to guess it from the list's shape. ``snapshot`` is that walk itself, with
    the stamp :meth:`SkillCatalogSnapshot.changed` reads, so the caller can resolve
    what it finally writes (:func:`agent_skill_keys` takes ``snapshot.entries``)
    against the snapshot the keys were validated with, and walk the roots a second
    time only when they moved in between. Nothing is written when *unknown_keys*
    is non-empty -- the caller rejects the whole request so a typo'd key can never
    partially apply.

    Invariants:

    * Non-``skill://`` resources (``file://`` steering globs) keep their
      original relative order and are never touched.
    * Unmanaged ``skill://`` URIs (wildcards, hand-authored paths) are preserved.
    * The managed set is fully replaced, so removing a key removes the mapping.
    """
    applied: list[str] = []
    unknown: list[str] = []
    uris: list[str] = []
    seen: set[str] = set()
    # One enumeration for the whole write: every key resolved and every existing
    # URI inverted against the SAME snapshot, so a concurrent skill add/remove
    # cannot make the two halves disagree mid-request.
    snapshot = walk_skill_catalog(state, session_key)
    catalog = snapshot.entries
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        uri = skill_uri_for_key(key, state, catalog)
        if uri is None:
            unknown.append(key)
            continue
        applied.append(key)
        uris.append(uri)
    if unknown:
        return applied, unknown, uris, snapshot

    resources = data.get("resources") or []
    if not isinstance(resources, list):
        resources = []
    kept = [
        r
        for r in resources
        if not (isinstance(r, str) and r.startswith(SKILL_URI_PREFIX))
        or skill_key_for_uri(r, agent_path, state, catalog) is None
    ]
    merged = kept + [u for u in uris if u not in kept]
    if merged:
        data["resources"] = merged
    else:
        # An empty list is meaningful to kiro-cli (it suppresses the shipped
        # steering defaults that _refresh_dynamic_fields only re-seeds when the
        # key is absent/empty), and an agent with nothing mapped should fall
        # back to those defaults — so drop the key instead of writing [].
        data.pop("resources", None)
    return applied, unknown, uris, snapshot


# ── Skill directory browser (tree + file content) ──


# Hard caps to keep the API responsive and bounded.
SKILL_TREE_MAX_ENTRIES = 500


SKILL_FILE_MAX_BYTES = 1_048_576  # 1 MiB


def list_skill_tree(skill_root: Path) -> list[dict[str, Any]]:
    """Return a flat list of files under *skill_root*, capped at SKILL_TREE_MAX_ENTRIES.

    Each entry: ``{path: relative-from-root, type: "file"|"dir", size: int}``.
    Sensitive paths are filtered out.  Symlinks are resolved; entries whose
    real path escapes *skill_root* are omitted.

    *skill_root* is the root :func:`_resolve_skill_root` admitted, and this walk
    addresses it by name — so a root REPLACED after that admission is enumerated
    as whatever its name then denotes. Holding the root's identity across the
    walk needs the traversal itself to run relative to a descriptor, which is a
    second mechanism with its own platform fallback (no ``O_DIRECTORY`` on
    Windows) and its own listing semantics. This function therefore carries the
    same exposure as the rest of the by-name filesystem surface, and the
    guarantee that bytes never leave the root lives where bytes are read:
    :func:`read_skill_file` opens through a descriptor and holds its admission
    root literally.
    """
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(skill_root, followlinks=False):
        # Stable order — reproducible across runs / tests.
        dirnames.sort()
        filenames.sort()
        for d in list(dirnames):
            full = Path(dirpath) / d
            if is_sensitive_path(str(full)):
                dirnames.remove(d)
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "dir", "size": 0})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
        for f in filenames:
            full = Path(dirpath) / f
            if is_sensitive_path(str(full)):
                continue
            try:
                if full.is_symlink():
                    real = full.resolve(strict=True)
                    real.relative_to(skill_root.resolve(strict=True))
                    if is_sensitive_path(str(real)):
                        continue
                stat = full.stat()
            except (OSError, ValueError):
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "file", "size": int(stat.st_size)})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
    return out


def read_skill_file(skill_root: Path, rel_path: str) -> tuple[str, str | None]:
    """Read ``skill_root/rel_path`` with safety + size guards.

    Returns ``(content, error)``.  ``error`` is non-empty when access is
    denied, the file is too big, or it doesn't exist.

    *skill_root* must be a root a caller already admitted —
    :func:`_resolve_skill_root` returns a resolved path, and that path is what
    bounds the read. Resolving the root again HERE cannot serve as the bound: a
    skill directory replaced between admission and this call resolves to the
    replacement's target, so the bound would be computed from the swap.

    The path checks below choose WHICH file to serve; the bytes then come from
    ``hooks.safe_read_file_bytes_nolink`` with ``within_root``, so containment
    is enforced on the descriptor actually opened rather than on a path resolved
    earlier. Re-opening by name instead leaves a check-to-use window — an
    ancestor directory swapped for a symlink after the check escapes the root —
    and no hardlink guard at all, since ``resolve()`` does not follow a
    hardlink, so a link to a file outside the root satisfies the containment
    check above. The helper opens without following the final component, then
    validates the opened inode: hardlinked (``st_nlink > 1``), non-regular, or
    escaping paths are refused.
    """
    if not rel_path or ".." in rel_path.split("/") or rel_path.startswith("/"):
        return "", "invalid path"
    target = skill_root / rel_path
    try:
        resolved = target.resolve(strict=True)
        skill_resolved = skill_root.resolve(strict=True)
        resolved.relative_to(skill_resolved)
    except (OSError, ValueError):
        return "", "not found"
    if is_sensitive_path(str(resolved)):
        return "", "access denied"
    if not resolved.is_file():
        return "", "not a file"
    try:
        size = resolved.stat().st_size
    except OSError:
        return "", "stat failed"
    if size > SKILL_FILE_MAX_BYTES:
        return "", f"file too large ({size} bytes; cap {SKILL_FILE_MAX_BYTES})"
    try:
        data = safe_read_file_bytes_nolink(
            str(target),
            # The ADMITTED root, and kept literally: neither this function nor
            # the helper may resolve it again. Both re-resolutions authorize a
            # replacement — ``skill_resolved`` is computed after the admission,
            # and the helper's own ``realpath`` runs later still — so an fd under
            # the swapped target would satisfy containment against a root
            # derived from the swap.
            within_root=str(skill_root),
            max_bytes=SKILL_FILE_MAX_BYTES,
            within_root_is_canonical=True,
        )
    except FileTooLargeError:
        # The file grew past the cap between the stat above and the read.
        return "", f"file too large (cap {SKILL_FILE_MAX_BYTES})"
    if data is None:
        # One message for every descriptor-level refusal (escape, hardlink,
        # non-regular, unreadable): the caller is a browse endpoint, and naming
        # which guard fired would describe the filesystem to the client.
        return "", "access denied"
    # Universal newlines, because the descriptor read is a BINARY one and the
    # contract this serves is text: a CRLF skill file has to render as the same
    # content on every platform, and the viewer receiving \r\n on Windows alone
    # is a difference nothing downstream asked for.
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n"), None

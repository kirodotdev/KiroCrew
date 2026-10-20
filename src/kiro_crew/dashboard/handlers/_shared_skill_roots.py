"""Where skills may live: the trusted skill roots and the containment of a skill path.

The edition's and the package's skill roots, the app-declared skill directories, and
``_resolve_skill_root``, which decides which root a requested skill path belongs to
and refuses one that escapes it.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared_workspace import active_project_dir
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import is_sensitive_path
from kiro_crew.skills import _trusted_skill_roots, skills_dir

logger = logging.getLogger("kiro_crew.dashboard.handlers._shared")


def _edition_skill_roots() -> list[Path]:
    """Return edition-contributed SKILL.md source roots (CPP seam).

    Reads ``McpToolingProvider.extra_skills()`` fail-closed through
    ``safe_context_call`` (public Default: ``[]``), so on a vanilla OSS install
    there are no roots to discover and the edition skill helpers below
    return "nothing found" rather than globbing a hardcoded home-dir tree.
    Deferred import (sel.py pattern) so this module never imports the platform
    package at module load.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    roots: list[Path] = safe_context_call(
        lambda: list(current_context().mcp_tooling.extra_skills()),
        fallback_factory=list,
        log_message="extra_skills lookup failed; using none",
    )
    return [Path(r) for r in roots]


def _canonical_skill_roots() -> list[Path]:
    """Skill roots the CORE owns and keys under its own prefixes.

    ``extra_skills()`` legitimately advertises some of these — the data home and
    ``~/.kiro/skills`` — so the loader indexes them. They must not ALSO be
    searched or keyed as ``package/``, or one file gets two identities and a
    ``package/<name>`` request can be answered with the user's own editable skill.

    State-free on purpose, so every consumer gets the exclusion by default;
    ``<project>/.kiro/skills`` needs a chat slot and is added by the caller that
    has one.
    """
    out: list[Path] = [Path.home() / ".kiro" / "skills", skills_dir()]
    try:
        # ``resolve()``, not just ``expanduser()``: a RELATIVE extra_paths entry
        # would otherwise key the catalog by a relative root, and the persisted
        # ``skill://`` URI would then resolve against whatever cwd the next
        # kiro-cli session starts in — silently loading a different skill, or
        # none. A skill root must be a stable absolute location.
        out.extend(Path(p).expanduser().resolve() for p in KiroCrewConfig.load().skills.extra_paths)
    except Exception:
        logger.debug("failed to load extra skill paths from config", exc_info=True)
    return out


def _resolved_set(paths: Iterable[Path]) -> set[Path]:
    """Resolved forms of *paths*, skipping any that cannot be resolved.

    ``Path.resolve()`` raises ``RuntimeError`` (not ``OSError``) on a symlink
    loop, so both are caught: an unresolvable root simply does not participate in
    identity comparisons.
    """
    out: set[Path] = set()
    for p in paths:
        try:
            out.add(p.resolve())
        except (OSError, RuntimeError):
            continue
    return out


def _edition_package_roots(canonical: set[Path] | None = None) -> list[Path]:
    """Edition roots that are genuinely ``package/`` territory.

    The single source of truth for "which advertised roots are package roots",
    shared by key enumeration and path resolution — if those two disagree, the
    catalog offers a key the resolver refuses, or worse resolves to a file the
    catalog never listed.

    An unresolvable root is KEPT: it cannot be compared for identity, and
    dropping it would silently remove a root that is otherwise served.
    """
    owned = set(canonical) if canonical is not None else _resolved_set(_canonical_skill_roots())
    out: list[Path] = []
    for root in _edition_skill_roots():
        try:
            resolved = root.resolve()
        except (OSError, RuntimeError):
            out.append(root)
            continue
        if resolved in owned:
            continue
        owned.add(resolved)
        out.append(root)
    return out


def _dedupe_resolved(paths: list[Path]) -> list[Path]:
    """Collapse paths that resolve to the same file, preserving order.

    One skill is routinely reachable through two roots — an edition may advertise
    both a directory and a symlink into it — and that is NOT an ambiguity. Only
    distinct FILES are.

    ``Path.resolve()`` raises ``RuntimeError`` (not ``OSError``) on a symlink
    loop, and a looping ``SKILL.md`` is yielded by ``glob`` because a literal
    pattern matches the dirent without following it. Catching only ``OSError``
    would turn that into a 500 on a browser-triggered request, so an
    unresolvable path is skipped instead: it cannot be read anyway.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        try:
            key = p.resolve()
        except (OSError, RuntimeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _resolve_package_skill_path(name: str, canonical: set[Path] | None = None) -> Path | None:
    """Find SKILL.md for an edition-contributed skill by its key remainder.

    Searched over the ``package/`` territory of the edition skill roots
    (:func:`_edition_package_roots`) — NOT every advertised root. A root the core
    already keys as ``kiro-user/`` or unprefixed is excluded, so a
    ``package/<name>`` request can never be answered with the user's own editable
    skill; *canonical* lets a caller that knows the active project add
    ``<project>/.kiro/skills`` to that exclusion.

    Two layouts are supported, in precedence order:

    1. ``<root>/<name>/SKILL.md`` — *name* is the path relative to the root, which
       is how a row keyed ``package/<rel>`` addresses its file.
    2. ``<root>/<pkg>/<name>/SKILL.md`` — *name* is a leaf under some package
       directory, for an edition that keys rows by leaf.

    An exact relative-path hit wins over a nested leaf hit. Within a tier, two
    DISTINCT files matching is a genuine ambiguity — the same relative path
    bundled by two packages, which this key grammar cannot tell apart — so it
    returns ``None`` and logs instead of picking one. Serving an arbitrary one of
    the two looks completely successful and shows the wrong skill's content,
    which is the failure mode worth being loud about.
    """
    exact: list[Path] = []
    nested: list[Path] = []
    for root in _edition_package_roots(canonical):
        exact.extend(root.glob(f"{name}/SKILL.md"))
        nested.extend(root.glob(f"*/{name}/SKILL.md"))
    for tier, label in ((exact, "relative path"), (nested, "leaf name")):
        candidates = _dedupe_resolved(tier)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            logger.warning(
                "edition skill %r matches %d distinct files by %s (%s); refusing "
                "to guess — the package/<path> key cannot address more than one",
                name,
                len(candidates),
                label,
                ", ".join(sorted(str(p) for p in candidates)),
            )
            return None
    return None


# The ONE key prefix whose leaf skill directory may be a symlink pointing
# anywhere. Editions install ``~/.kiro/skills/<name>`` as a link to their own
# tree (AIM ``--local`` and friends), so refusing it there would 404 a shape
# people actually run. Under every other prefix a leaf that resolves outside its
# root is admitted only when it lands in an app skill provider root (see
# :func:`_leaf_is_contained`), because an unconstrained leaf link is a
# whole-filesystem read primitive: ``<project>/.kiro/skills/x -> /etc`` would
# serve /etc through the tree and file endpoints. ``is_sensitive_path`` is no
# backstop for that — it is a $HOME-anchored credential denylist, not a
# containment check.
LEAF_SYMLINK_PREFIX = "kiro-user/"


def _path_at_or_under(path: Path, root: Path) -> bool:
    """Whether *path* IS *root* or sits under it. Both must be resolved."""
    return path == root or root in path.parents


def _declared_app_skill_dirs(resolved: Path) -> list[Path]:
    """The skill directories the app OWNING *resolved* declares, resolved.

    ``apps.bridges._register_skills`` links ``app_root / p`` for each ``p`` in
    that app's ``manifest.skills``, so this is the exact set of targets a skills
    link can legitimately have. Read through ``bridges._registration_source``,
    which for a shipped builtin is the immutable package copy rather than
    mutable installed metadata. Empty — admitting nothing — when *resolved* names
    no app, the app declares no skills, or its manifest cannot be read.
    """
    from kiro_crew.apps.execution import shipped_builtin_app_name_at

    # A shipped builtin is found by the package root it resolves into: its
    # directory name need not match its manifest name.
    app_name = shipped_builtin_app_name_at(resolved) or ""
    roots = [] if app_name else _trusted_skill_roots()
    for root in roots:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        # ``<data home>/apps/<app>/…`` and the package's
        # ``<kiro_crew>/apps/builtins/<app>/…``.
        if parts[:2] == ("apps", "builtins") and len(parts) > 2:
            app_name = parts[2]
        elif parts:
            app_name = parts[0]
        if app_name:
            break
    if not app_name:
        return []
    try:
        from kiro_crew.apps import bridges  # deferred: heavy import chain

        manifest, app_root = bridges._registration_source(app_name)
    except Exception:  # noqa: BLE001 — an unreadable app admits nothing
        logger.debug("app skill sources unavailable for %r", app_name, exc_info=True)
        return []
    if manifest is None:
        return []
    out: list[Path] = []
    for declared in getattr(manifest, "skills", []) or []:
        try:
            out.append((app_root / str(declared)).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    return out


def _leaf_is_contained(resolved: Path, root_resolved: Path, prefix_allows_link: bool) -> bool:
    """Whether a RESOLVED skill directory may be served under its root.

    Inside the root is always fine. Outside it, two sources are legitimate and
    nothing else is:

    * the prefix in :data:`LEAF_SYMLINK_PREFIX`, where an edition installs
      ``~/.kiro/skills/<name>`` as a link into its own tree;
    * a directory an app DECLARES as a skill — ``apps.bridges._register_skills``
      links each one into the kirocrew skills root (namespaced AND flat) with the
      target in the app's own tree, so those land outside the root by
      construction.

    The declared directories, NOT the app's root: an app tree also holds that
    app's data, tokens and rendered configs, and ``<root>/x -> <app>/data`` must
    not serve them. Taking the set from the manifest the bridge registers from is
    what keeps the browse side from admitting more than the bridge links.
    """
    if _path_at_or_under(resolved, root_resolved):
        return True
    if prefix_allows_link:
        return True
    return any(
        _path_at_or_under(resolved, declared) for declared in _declared_app_skill_dirs(resolved)
    )


def _resolve_skill_root(name: str, state: DashboardState, session_key: str = "") -> Path | None:
    """Return the absolute skill directory for *name*, or None.

    Accepts the same nested-name scheme used by the existing skill API:
    - ``foo`` → ``~/.kiro/crew/skills/foo``
    - ``utils/tiny-url`` → ``~/.kiro/crew/skills/utils/tiny-url``
    - ``package/<skill>`` → resolved via _resolve_package_skill_path() lookup
    - ``kiro-user/<skill>`` → ``~/.kiro/skills/<skill>``
    - ``kiro-workspace/<skill>`` → ``<project>/.kiro/skills/<skill>``

    *session_key* scopes ``kiro-workspace/`` to the requesting chat slot's
    project. Without it, resolution falls back to the single project shared by
    every slot — and fails closed to ``None`` when open slots disagree, since
    guessing could read the wrong checkout.

    The returned path is always under one of the allowed roots — paths
    that try to escape via ``..`` or symlinks are rejected. The single
    exception is a ``kiro-user/`` leaf symlink; see
    :data:`LEAF_SYMLINK_PREFIX`.
    """
    if not name or ".." in name or name.startswith("/"):
        return None
    # Only the edition-install prefix may resolve to a leaf outside its root.
    allow_leaf_symlink = name.startswith(LEAF_SYMLINK_PREFIX)
    if name.startswith("kiro-user/"):
        rel = name[len("kiro-user/") :]
        root = Path.home() / ".kiro" / "skills"
    elif name.startswith("kiro-workspace/"):
        rel = name[len("kiro-workspace/") :]
        # NOT trust-gated, deliberately: reading a SKILL.md is how the operator
        # decides whether to grant trust in the first place, so requiring the
        # grant to view the file would make the consent decision blind. The
        # boundary that matters -- an unconsented project skill never reaching the
        # agent's context -- is enforced in SkillsLoader. Uses the permissive
        # resolver so the documented keyless single-project fallback and the
        # two-project behaviour stay as they are.
        proj = active_project_dir(state, session_key)
        if proj is None:
            return None
        root = proj / ".kiro" / "skills"
    elif name.startswith("package/"):
        # Locate via existing helper (sync version). The active project's
        # ``.kiro/skills`` joins the canonical exclusion here because this caller
        # is the one that knows the chat slot.
        pkg_rel = name[len("package/") :]
        canonical = _resolved_set(_canonical_skill_roots())
        proj = active_project_dir(state, session_key)
        if proj is not None:
            canonical |= _resolved_set([proj / ".kiro" / "skills"])
        path = _resolve_package_skill_path(pkg_rel, canonical)
        if not path:
            return None
        candidate = path.parent
        if is_sensitive_path(str(candidate)):
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        # Re-check the *resolved* target — a symlink within the package path could
        # point at a sensitive location that the unresolved check missed
        # (consistent with the kirocrew/kiro branches below).
        if is_sensitive_path(str(resolved)):
            return None
        # Leaf containment, as the shared block below applies it to every other
        # prefix: the resolved skill directory must sit at or under one of the
        # package roots it was searched in. This branch returns early, so the
        # check belongs here too — an edition packager can plant
        # ``<edition-root>/x -> /outside`` exactly like any other root, and
        # ``package/`` carries no leaf-symlink allowance. An unresolvable root
        # contains nothing, so a leaf found only through one fails closed.
        package_roots = _resolved_set(_edition_package_roots(canonical))
        if not any(_path_at_or_under(resolved, r) for r in package_roots):
            return None
        return resolved
    else:
        # ``kirocrew`` skills live under the active config home, which honors
        # KIROCREW_HOME (e.g. isolated dev gateways).  Hardcoding
        # ``~/.kirocrew`` here would 404 every skill in a KIROCREW_HOME-isolated
        # deployment even though SkillsLoader (the GET /api/skills source)
        # resolves them correctly.
        rel = name
        # Reject empty, traversal, absolute, and home-expansion inputs before
        # any filesystem probing. pathlib collapses ``Path(root) / "/etc"`` to
        # ``/etc`` (absolute RHS overrides the base), so an un-rejected absolute
        # or ``~`` prefix would let _probe() run is_dir() on arbitrary paths
        # before the containment check.
        if not rel or ".." in rel or rel.startswith("/") or rel.startswith("~"):
            return None
        # Root precedence must match SkillsLoader.load_skill(): kirocrew ->
        # user extra_paths -> edition skill roots (lowest). Otherwise the tree
        # endpoint could display a different directory than load_skill() reads.
        roots = [skills_dir()]
        try:
            roots.extend(Path(p).expanduser() for p in KiroCrewConfig.load().skills.extra_paths)
        except Exception:
            logger.debug("failed to load extra skill paths from config", exc_info=True)
        roots.extend(_edition_skill_roots())

        def _probe(r: Path) -> bool:
            try:
                return (r / rel).is_dir()
            except OSError:
                return False

        root = next((r for r in roots if _probe(r)), skills_dir())
    candidate = root / rel
    if not candidate.is_dir():
        return None
    if is_sensitive_path(str(candidate)):
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        # ``RuntimeError`` is what a symlink loop raises on some CPythons, and a
        # standing loop is already filtered by ``is_dir()`` above — so what
        # reaches here is a leaf that BECAME one after that check. Either way the
        # skill is refused; this function answers a request and must not turn an
        # unresolvable name into a 500.
        return None
    # Containment + symlink policy.  Skills can be nested under category
    # directories (``utils/multi-badger`` → ``<root>/utils/multi-badger``), so
    # the candidate's *parent* must resolve to a location at or under the
    # trusted root — that is what rejects a symlinked *intermediate* directory
    # which would let ``a/b`` escape the tree.
    #
    # The LEAF is held to the same rule, with the two documented exceptions in
    # :func:`_leaf_is_contained` — the edition-install prefix, and an app skill
    # provider root. Otherwise a parent-only check is an escape, so the resolved
    # target itself must be inside the root. That target is finally checked
    # against the sensitive-path list, which narrows the exceptions but does not
    # bound them.
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not _path_at_or_under(parent_resolved, root_resolved):
        return None
    if not _leaf_is_contained(resolved, root_resolved, allow_leaf_symlink):
        return None
    if is_sensitive_path(str(resolved)):
        return None
    return resolved

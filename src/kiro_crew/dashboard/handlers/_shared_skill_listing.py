"""The installed-skills list the dashboard shows, with the agents that load each skill.

``collect_skills_blocking`` lists the Kiro and Kiro Crew skills, keeps one row per
skill file, annotates each row with the agents whose specs load it, and warns about
skills found outside the trusted roots.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew.agent_discovery import expand_skill_uri, parsed_agent_specs
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers._shared_skill_roots import _edition_skill_roots
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.security import is_sensitive_path
from kiro_crew.skill_trust import is_project_trusted as _is_project_trusted
from kiro_crew.skills import _matches_any, _with_canonical_globs

logger = logging.getLogger("kiro_crew.dashboard.handlers._shared")


# ── Kiro-cli native skills (~/.kiro/skills/, <project>/.kiro/skills/) ──


# Maximum SKILL.md content we'll read just to extract frontmatter description.
_KIRO_SKILL_FRONTMATTER_BYTES = 4096


def _kiro_skill_roots(project_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return ``(label, path)`` pairs for the open-standard skill locations.

    label is one of: ``kiro-user``, ``kiro-workspace``.  Used as the
    ``source`` field on listed skills so the UI can show provenance.
    """
    out: list[tuple[str, Path]] = []
    user_dir = Path.home() / ".kiro" / "skills"
    if user_dir.is_dir() and not is_sensitive_path(str(user_dir)):
        out.append(("kiro-user", user_dir))
    if project_dir:
        ws_dir = project_dir / ".kiro" / "skills"
        if ws_dir.is_dir() and not is_sensitive_path(str(ws_dir)):
            out.append(("kiro-workspace", ws_dir))
    return out


def _parse_skill_description(skill_md: Path) -> tuple[str, bool]:
    """Cheap frontmatter parse — return (description, always)."""
    # Gate on the resolved target before reading: a SKILL.md inside an
    # otherwise-trusted skills root may itself be a symlink to a sensitive
    # credential file (e.g. ~/.kiro/skills/evil/SKILL.md → ~/.aws/credentials).
    # Checking the root dir is not enough — individual files must be checked.
    try:
        resolved_md = skill_md.resolve(strict=True)
    except OSError:
        return "", False
    if is_sensitive_path(str(resolved_md)):
        return "", False
    try:
        with resolved_md.open("rb") as f:
            head = f.read(_KIRO_SKILL_FRONTMATTER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return "", False
    # The loader's own grammar: only a column-0 key is a field, so an indented
    # ``description:`` inside a nested block (``inputs[].description``) never
    # replaces the skill's own, and both readers agree on what a row says.
    meta = parse_frontmatter(head, SKILL_LOADER)
    return meta.get("description", ""), meta.get("always", "").strip().lower() == "true"


def list_kiro_skills(project_dir: Path | None = None) -> list[dict[str, Any]]:
    """List skills from kiro-cli's open-standard locations.

    Each entry has the same shape as a SkillsLoader entry plus a
    ``source`` of ``kiro-user`` or ``kiro-workspace``.  Read-only —
    edits are not routed back here (kiro-cli owns these directories).
    """
    out: list[dict[str, Any]] = []
    for source, root in _kiro_skill_roots(project_dir):
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            desc, always = _parse_skill_description(skill_md)
            out.append(
                {
                    "key": f"{source}/{entry.name}",
                    "name": entry.name,
                    "description": desc,
                    "path": str(skill_md),
                    "dir": str(entry),
                    "always": always,
                    "source": source,
                }
            )
    return out


# ── loaded_by_agents resolution ──


def _agent_dirs() -> list[Path]:
    """Return existing agent JSON directories (the user-level dir)."""
    out: list[Path] = []
    user = kiro_agents_dir()
    if user.is_dir():
        out.append(user)
    return out


def _expand_resource_uri(
    uri: str, agent_path: Path, *, project_dir: str | Path | None = None
) -> str | None:
    """Strip ``skill://`` and resolve ``~`` / workspace-relative paths.

    Thin alias for :func:`kiro_crew.agent_discovery.expand_skill_uri` — the
    single implementation, shared with the session-context skill filter so the
    dashboard's ``loaded_by_agents`` annotation and the runtime injection agree
    on what a given URI matches.

    A workspace-relative URI (``skill://.kiro/skills/*``) resolves against
    *project_dir* when the caller supplies one — the same cwd kiro-cli is
    launched in — so the picker's agent filter matches a relative glob the way
    the runtime's ``session_skill_globs`` does. With no project it falls back
    to the project root inferred from *agent_path*, which for a user-dir spec
    collapses the glob onto ``~`` and never matches a project skill.

    Returns a glob pattern usable with fnmatch, or None if not a skill URI.
    """
    return expand_skill_uri(uri, agent_path, project_dir=project_dir)


def _agent_loads_skill(agent_json: dict[str, Any], agent_path: Path, skill_md: Path) -> bool:
    """Return True if *agent_json*'s ``resources`` would load *skill_md*.

    One-off helper (single skill vs single agent). For annotating *many*
    skills against *many* agents, prefer :func:`_expand_agent_globs` +
    :func:`_agents_loading_skill` so each agent's globs are expanded once
    instead of once per skill.
    """
    resources = agent_json.get("resources") or []
    if not isinstance(resources, list):
        return False
    globs = [
        g
        for res in resources
        if isinstance(res, str)
        for g in (_expand_resource_uri(res, agent_path),)
        if g
    ]
    return bool(_agents_loading_skill(skill_md, [("", _with_canonical_globs(globs))]))


def _expand_agent_globs(
    parsed_agents: list[tuple[str, dict[str, Any], Path]],
    *,
    project_dir: str | Path | None = None,
) -> list[tuple[str, list[str]]]:
    """Pre-expand every agent's ``skill://`` resources into fnmatch globs ONCE.

    Returns ``(agent_name, [glob, ...])`` pairs. The glob for a resource
    depends only on ``(uri, agent_path, project_dir)`` — NOT on the skill being
    matched — so expanding here (O(agents × resources)) and reusing the result
    across all skills avoids re-running :func:`_expand_resource_uri` once per
    (skill, agent, resource), which on a large catalog is the dominant cost.
    Agents with no skill:// resources are dropped (they can match nothing).
    Each glob is paired with its canonical spelling, as the loader's scope
    match does, so a ``~`` mapping under a symlinked ``$HOME`` still matches
    the resolved catalog path the loader injects.

    *project_dir* anchors a workspace-relative glob, so the catalog's
    ``kiro-workspace`` rows match the agent that declares ``skill://.kiro/...``
    the same way the runtime does.
    """
    expanded: list[tuple[str, list[str]]] = []
    for name, data, agent_path in parsed_agents:
        resources = data.get("resources") or []
        if not isinstance(resources, list):
            continue
        globs = [
            g
            for res in resources
            if isinstance(res, str)
            for g in (_expand_resource_uri(res, agent_path, project_dir=project_dir),)
            if g
        ]
        if globs:
            expanded.append((name, _with_canonical_globs(globs, project_dir)))
    return expanded


def _agents_loading_skill(
    skill_md: Path, expanded_agents: list[tuple[str, list[str]]]
) -> list[str]:
    """Return names of agents whose pre-expanded globs match *skill_md*.

    Uses the runtime scope match, so a row listed under a symlinked ``$HOME``
    also matches a glob written against the resolved home path, and an app
    skill listed under its flat link matches its app's ``<app>/*/SKILL.md``.
    """
    # Imported here: every module-level name of this family is served by the
    # ``_shared`` facade, and these two are not part of that surface.
    from kiro_crew.skill_runtime.catalog import _alias_matches
    from kiro_crew.skills import skills_dir

    target = str(skill_md)
    return [
        name
        for name, globs in expanded_agents
        if _matches_any(target, globs) or _alias_matches(skill_md, globs, [skills_dir()])
    ]


def skill_rows_for_globs(
    rows: list[dict[str, Any]],
    globs: list[str],
    *,
    project_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Keep the catalog *rows* whose ``SKILL.md`` the agent's *globs* load.

    *globs* are the skill globs a caller already resolved for ONE agent via
    ``agent_skill_globs(agent, project_dir=…)``, whose winning spec
    :func:`kiro_crew.agent_discovery.list_agents` alone decides (project scope
    shadows user, declared name outranks filename, package rules). Whether a
    row is loaded is answered by :func:`_agents_loading_skill` -- the one
    matcher the ``loaded_by_agents`` annotation uses -- so the agent-scoped
    picker and that badge apply the same runtime rule: canonical globs, the
    resolved-symlink retry, and the flat-link alias fallback for app skills.
    *project_dir* is handed to the canonicalization as the annotation does, so a
    prefix inside the project is never resolved before consent. A row with no
    ``path`` is dropped.

    A row :func:`_one_row_per_skill_file` collapsed several spellings into
    also carries the dropped rows' paths as ``alias_paths``; the row is kept
    when ANY spelling matches, so a glob naming only the dropped spelling (a
    crew skill symlinked into ``~/.kiro/skills``) still finds the skill.
    """
    agent = [("", _with_canonical_globs(globs, project_dir))]
    return [
        row
        for row in rows
        if any(
            _agents_loading_skill(Path(p), agent)
            for p in (row.get("path"), *(row.get("alias_paths") or ()))
            if p
        )
    ]


# The parsed-agents snapshot lives in ``agent_discovery.parsed_agent_specs``
# — one cache, one signature, one invalidation point shared with the
# ``list_agents`` cache (the agent write paths clear both through
# ``clear_list_agents_cache``). Skill rows themselves stay uncached: the
# endpoint's documented freshness contract is about skills on disk, not
# agent specs.


def _load_parsed_agents() -> list[tuple[str, dict[str, Any], Path]]:
    """Read every agent JSON ONCE, returning ``(name, data, agent_path)``.

    Hoisted out of the per-skill loop so ``api_skills`` parses each agent
    file exactly once per request instead of once per skill — turning an
    O(skills × agents) read/parse blowup into O(agents). Resolved from the
    :func:`kiro_crew.agent_discovery.parsed_agent_specs` snapshot, so a warm
    request parses nothing and reads go through the one hardened reader
    (sidecars, symlink loops, sensitive targets, oversized files, and
    invalid JSON are skipped best-effort rather than 500ing the response).
    Rows are shared with that cache: treat them as read-only.
    """
    parsed: list[tuple[str, dict[str, Any], Path]] = []
    for agents_dir in _agent_dirs():
        for data, agent_path in parsed_agent_specs(
            agents_dir, operation="skills_loaded_by_agents", source="dashboard"
        ):
            name = data.get("name") or agent_path.stem
            parsed.append((str(name), data, agent_path))
    return parsed


def _resolve_loaded_by_agents(
    skill_md: Path,
    parsed_agents: list[tuple[str, dict[str, Any], Path]] | None = None,
) -> list[str]:
    """Return list of agent names whose ``resources`` glob matches *skill_md*.

    Pass *parsed_agents* (from :func:`_load_parsed_agents`) to reuse a single
    agent parse across many skills; omit it for a one-off lookup (parses
    agents inline). Empty list means no agent loads this skill via
    ``skill://`` URIs (it may still be loaded via Kiro Crew text-injection or
    an external MCP server).
    """
    agents = parsed_agents if parsed_agents is not None else _load_parsed_agents()
    out: list[str] = []
    for name, data, agent_path in agents:
        if _agent_loads_skill(data, agent_path, skill_md):
            out.append(name)
    return out


def annotate_skills_with_agents(
    skills: list[dict[str, Any]], *, project_dir: str | Path | None = None
) -> None:
    """Annotate each skill dict in-place with ``loaded_by_agents``.

    Parses the agent JSONs ONCE and pre-expands each agent's ``skill://``
    globs ONCE, then matches every skill against that in-memory set —
    O(agents × resources) expansion + O(skills × globs) matching, instead of
    re-expanding every agent glob per skill. Synchronous and filesystem-heavy
    (the parse walks ~/.kiro/agents) — callers on the asyncio event loop MUST
    run this off the loop. Per-skill failures isolate to an empty list (the
    documented default) rather than blanking the whole response.

    *project_dir* anchors workspace-relative ``skill://`` globs, so a
    ``kiro-workspace`` row is credited to the agent that declares the relative
    glob — the same project the runtime resolves it against. Without it such a
    row is never credited to a user-dir agent, so the agent-scoped listing
    would drop every project skill for that agent.
    """
    expanded = _expand_agent_globs(_load_parsed_agents(), project_dir=project_dir)
    for s in skills:
        path = s.get("path") or ""
        if not path:
            s["loaded_by_agents"] = []
            continue
        try:
            s["loaded_by_agents"] = _agents_loading_skill(Path(path), expanded)
        except Exception:
            s["loaded_by_agents"] = []


def collect_skills_blocking(
    skills_loader: Any,
    package_skills: list[dict[str, Any]],
    project_dir: Path | None,
) -> list[dict[str, Any]]:
    """Gather + annotate the full skill catalog. Runs ALL blocking FS work.

    This is the synchronous core behind ``GET /api/skills``. It performs
    every filesystem-heavy step in one call so the caller can offload the
    whole thing to a thread via ``run_in_executor``. ``list_skills()`` (os.walk +
    per-file frontmatter reads), ``list_kiro_skills()`` (per-skill resolve +
    read), and the confined project catalog are filesystem-heavy enough to
    stall the event loop past the loop-stall watchdog on large catalogs, so
    they run in the thread too rather than inline.

    Steps, in the same order the handler used inline:

    1. ``skills_loader.list_skills()`` — kirocrew skills (default source).
    2. ``package_skills`` — edition/package skills already fetched (structured
       rows) from ``CapabilityManager.list_skills()``; the manager owns their
       parsing, so nothing is parsed here.
    3. Global open-standard kiro-cli skills plus project rows from the loader's
       confined no-follow catalog.
    4. ``annotate_skills_with_agents(...)`` — ``loaded_by_agents`` per skill,
       with *project_dir* so a workspace-relative ``skill://`` glob is credited
       to the agent the way the runtime resolves it.

    The capability-manager fetch is intentionally NOT done here (it is async);
    the caller awaits it and hands us the structured rows.
    """
    result: list[dict[str, Any]] = skills_loader.list_skills()
    for s in result:
        s.setdefault("source", "kirocrew")
    _warn_skills_outside_roots(package_skills)
    result.extend(package_skills)
    # The legacy scanner is valid for the operator-owned global Kiro directory,
    # but it resolves and reads project link targets before containment can be
    # checked. Never pass the project to it: pre-consent project rows must come
    # from the loader's confined no-follow enumeration below.
    workspace_rows = list_kiro_skills()
    if project_dir is not None:
        # A workspace row is LISTABLE without consent but only USABLE with it:
        # $token expansion and context injection both resolve through
        # SkillsLoader, which gates the project root on the operator's grant.
        # Marking the row lets the picker offer that consent instead of handing
        # back a token that silently expands to nothing.
        trusted = _is_project_trusted(project_dir)

        # The loader's containment-only catalog IS the definition of what
        # consent could make loadable. It intentionally bypasses trust
        # enforcement so genuine untrusted rows remain visible, while its
        # confined no-follow read keeps linked targets untouched.
        try:
            project_rows = skills_loader.catalog_project_skills(project_dir)
        except Exception:  # noqa: BLE001 — a listing must not die on enumeration
            logger.warning("skills catalog: enumeration failed; listing no workspace rows")
            project_rows = []
        for row in project_rows:
            row["key"] = f"kiro-workspace/{row.get('key', '')}"
            row["source"] = "kiro-workspace"
            row["trusted"] = trusted
        workspace_rows.extend(project_rows)
    result.extend(workspace_rows)
    annotate_skills_with_agents(result, project_dir=project_dir)
    return _one_row_per_skill_file(result)


def _skill_file_identity(row: dict[str, Any]) -> tuple[int, int] | None:
    """The ``(st_dev, st_ino)`` of the SKILL.md a row lists, or ``None``.

    An inode, not a path: ``/home/u`` and ``/local/home/u`` can be one bind
    mount, so two spellings of the same file only compare equal this way.
    Workspace rows return ``None``: they come from the confined no-follow
    catalog and are gated on project consent, so they are never merged with
    an operator-owned row. A row whose path is a directory (an edition row may
    carry the skill dir) is compared by its ``SKILL.md``.
    """
    if row.get("source") == "kiro-workspace":
        return None
    raw = row.get("path") or ""
    if not raw:
        return None
    try:
        if os.path.isdir(raw):
            raw = os.path.join(raw, "SKILL.md")
        st = os.stat(raw)
    except (OSError, ValueError):
        return None
    if not st.st_ino:
        # Some filesystems (FAT/exFAT, certain network shares) report inode 0
        # for every file; that identity cannot tell two files apart.
        return None
    return (st.st_dev, st.st_ino)


def _row_is_editable(row: dict[str, Any]) -> bool:
    """Whether the dashboard's Edit/Delete can write this row's file.

    Only a Kiro Crew row under the skills dir the loader writes to: the write
    handlers join the key onto that dir, so a row reached through
    ``skills.extra_paths`` (``owned: False``) or any other source is read-only.
    """
    return row.get("source") == "kirocrew" and row.get("owned") is not False


def _row_rank(row: dict[str, Any]) -> int:
    """Which of two rows for one file to keep: lower wins, ties keep the first.

    The editable row first. Then a row from the source that owns the file's
    folder (``package``, ``kiro-user``): its key is the one the agent-skill
    catalog keys that folder by, so a mapping saved from it resolves. A
    ``kirocrew`` row reached through ``skills.extra_paths`` or an edition root
    (``owned: False``) is last: nothing can edit it and that catalog does not
    key it.
    """
    if _row_is_editable(row):
        return 0
    if row.get("source") == "kirocrew":
        return 2
    return 1


def _one_row_per_skill_file(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse rows that list the same SKILL.md into one row.

    Two sources can reach one file -- ``skills.extra_paths`` naming
    ``~/.kiro/skills`` lists it as a Kiro Crew row AND ``list_kiro_skills``
    lists it as ``kiro-user`` -- and the browser showed both. :func:`_row_rank`
    picks the row kept. ``loaded_by_agents`` is the union, so an agent-scoped listing
    still finds the skill through whichever spelling its glob matched, and the
    dropped keys ride along as ``aliases`` for readers that look a row up by key,
    and the dropped paths as ``alias_paths`` for :func:`skill_rows_for_globs`,
    which matches an agent's globs against every spelling.
    """
    out: list[dict[str, Any]] = []
    seen: dict[tuple[int, int], int] = {}
    for row in rows:
        ident = _skill_file_identity(row)
        if ident is None:
            out.append(row)
            continue
        idx = seen.get(ident)
        if idx is None:
            seen[ident] = len(out)
            out.append(row)
            continue
        kept = out[idx]
        if _row_rank(row) < _row_rank(kept):
            kept, row = row, kept
            out[idx] = kept
        agents = list(kept.get("loaded_by_agents") or [])
        for name in row.get("loaded_by_agents") or []:
            if name not in agents:
                agents.append(name)
        kept["loaded_by_agents"] = agents
        # The dropped row's key stays resolvable: an agent's skill:// mapping
        # inverts to whichever key the catalog walk meets first (``kiro-user/``
        # leads), which can be the key of the row dropped here.
        aliases = list(kept.get("aliases") or [])
        for key in [row.get("key"), *(row.get("aliases") or [])]:
            if key and key != kept.get("key") and key not in aliases:
                aliases.append(key)
        kept["aliases"] = aliases
        # The dropped row's path too: ``skill_rows_for_globs`` matches an
        # agent's globs against every spelling, as the ``loaded_by_agents``
        # union above does, so a glob naming only the dropped spelling still
        # keeps the skill in an agent-scoped listing.
        alias_paths = list(kept.get("alias_paths") or [])
        for p in [row.get("path"), *(row.get("alias_paths") or [])]:
            if p and p != kept.get("path") and p not in alias_paths:
                alias_paths.append(p)
        kept["alias_paths"] = alias_paths
        # Usage, size, creation time and the injection mode come only from the
        # loader's read, so a kept row from another source would otherwise show
        # the skill as never used, undated, sizeless and "on demand".
        for field in (
            "deliveries",
            "last_used_at",
            "created_at",
            "size_bytes",
            "inject_on_trigger",
        ):
            if kept.get(field) is None and row.get(field) is not None:
                kept[field] = row[field]
    return out


def _warn_skills_outside_roots(package_skills: list[dict[str, Any]]) -> None:
    """Log loudly for any ``CapabilityManager.list_skills()`` row whose path
    falls outside every ``McpToolingProvider.extra_skills()`` root.

    Enforces (at runtime, not just in the interface docstring) the containment
    invariant the two Protocols share: the skill browser
    (``/api/skills/package/<name>/tree`` + detail) resolves a skill's on-disk
    path by searching those roots, so a listed row outside them lists in
    ``/api/skills`` but 404s on tree/detail. An edition that satisfies both
    seams independently can violate this; a loud warning turns an otherwise
    silent, hard-to-diagnose 404 into an actionable log line. No-op in OSS
    (``list_skills()`` returns ``[]``, so ``package_skills`` is empty).
    """
    if not package_skills:
        return
    roots = _edition_skill_roots()
    if not roots:
        return
    resolved_roots = []
    for r in roots:
        try:
            resolved_roots.append(r.resolve())
        except OSError:
            continue
    for row in package_skills:
        raw = row.get("dir") or row.get("path")
        if not raw:
            continue
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if not any(p == root or root in p.parents for root in resolved_roots):
            logger.warning(
                "skill %r (path %s) is outside every extra_skills() root %s — it "
                "will list in /api/skills but 404 on tree/detail (CapabilityManager."
                "list_skills / McpToolingProvider.extra_skills containment invariant)",
                row.get("name") or row.get("key"),
                raw,
                [str(r) for r in resolved_roots],
            )

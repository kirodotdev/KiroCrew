"""Scoped search and explicit activation of skills.

Ranks a query over metadata and bodies (the term index first, bounded direct
reads for the rest), serves an exact key while a first walk is still running, and
resolves ``$skillname`` tokens. The available set itself
(``SkillsLoader.scoped_skills``) and exact reads (``read_scoped_skill``) stay in
the facade with the other ``repo_scope`` gate sites.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, NamedTuple

from kiro_crew.skill_runtime import catalog as _catalog
from kiro_crew.skill_runtime import listing as _listing

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

logger = logging.getLogger("kiro_crew.skills")


class SkillSearchReport(NamedTuple):
    """One search's answer, and whether that answer may be missing matches.

    ``incomplete`` belongs to the call that produced it. The loader and its term
    index are shared by every concurrent search in the gateway, so a flag kept on
    either would be overwritten by whichever search finished last.
    """

    matches: list[dict]
    incomplete: bool


def _body_term_hits(content: str, terms: Iterable[str]) -> int:
    """How many of *terms* the body carries, by the SAME rule the index uses.

    Tokenized, then prefix-matched per token, because the persisted index answers
    that way: it stores `recall_terms` output and matches a query term against
    stored terms it prefixes. A plain substring scan here would score the two paths
    differently, so whether a skill ranked at all would depend on which path
    answered for it -- and both can answer inside ONE search, since the index hands
    individual keys back for a direct read.

    Concretely, substring scoring let `rollback` match a body whose only occurrence
    is inside `scrollback`; the index calls that a near miss, and so does this.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    body_terms = sk.recall_terms(content)
    if not body_terms:
        return 0
    return sum(1 for term in terms if any(bt.startswith(term) for bt in body_terms))


def _body_hits(
    loader: SkillsLoader,
    skills: list[dict],
    terms: Iterable[str],
    live_keys: list[str],
    project_dir: str | Path | None,
) -> dict[str, int]:
    hits, _incomplete = loader._body_matches(skills, terms, live_keys, project_dir)
    return {key: len(matched) for key, matched in hits.items()}


def _body_matches(
    loader: SkillsLoader,
    skills: list[dict],
    terms: Iterable[str],
    live_keys: list[str],
    project_dir: str | Path | None,
) -> tuple[dict[str, set[str]], bool]:
    """Refresh once per query, with bounded work and explicit incomplete recall.

    Returns the hits and whether this call left bodies unread.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    terms = tuple(terms)
    hits: dict[str, set[str]] = {}
    incomplete = False
    unconfined = [s for s in skills if not s.get("confine_root")]
    fallback = [s for s in skills if s.get("confine_root")]
    indexed = None
    started = time.monotonic()
    if unconfined and loader._search_index is not None:
        rows = []
        for skill in unconfined:
            path = str(skill.get("path", ""))
            fingerprint = skill.get("fingerprint") or sk.body_fingerprint(path)
            if fingerprint:
                rows.append((str(skill["key"]), path, fingerprint))
        # A scoped query cannot evict the other agents' persistent entries.
        outcome = loader._search_index.sync(
            rows,
            budget_seconds=0.25,
            canonical_roots={
                str(s["key"]): str(s["mapping_root"]) for s in unconfined if s.get("mapping_root")
            },
        )
        if outcome is not None:
            deferred, pending = outcome
            answered = [str(s["key"]) for s in unconfined if str(s["key"]) not in deferred]
            indexed = loader._search_index.body_matches(answered, terms)
            if indexed is not None:
                # Pending bodies are unread only on this branch: without index
                # hits every unconfined body joins the direct read below.
                incomplete = bool(pending)
                fallback += [
                    s
                    for s in unconfined
                    if str(s["key"]) in deferred and str(s["key"]) not in pending
                ]
    logger.debug("skill body index refresh/query: %.2fms", (time.monotonic() - started) * 1000)
    if indexed is None:
        fallback += unconfined
    else:
        hits.update(indexed)
    deadline = time.monotonic() + 0.25
    for skill in fallback:
        if time.monotonic() >= deadline:
            incomplete = True
            break
        cap = sk.PROJECT_SKILL_BODY_CAP if skill.get("confine_root") else None
        if cap is not None and int(skill.get("size_bytes", 0)) > cap:
            continue
        if skill.get("mapping_root") is not None:
            body = loader._read_global_skill_text(
                Path(skill["path"]), cap, canonical_root=skill.get("mapping_root")
            )
        else:
            body = loader.load_skill(str(skill["key"]), project_dir, max_bytes=cap)
        content = (body or "").lower()
        matched = {term for term in terms if _body_term_hits(content, [term])}
        if matched:
            hits[str(skill["key"])] = matched
    return hits, incomplete


def _exact_read_while_building(
    loader: SkillsLoader,
    key: str,
    only: list[str] | None,
    project_dir: str | Path | None,
    max_bytes: int,
    refusal_reasons: list[str] | None = None,
) -> str | None:
    """Serve a COMPLETE key during an unfinished first walk, or ``None``.

    A cold catalog cannot answer "does this skill exist", so without this an
    exact read on a machine's first run reports a skill that is right there as
    absent. Only reachable while :meth:`catalog_status` says ``building``: once
    the enumeration exists it is the authority, and a second resolution path
    running alongside it is how the two drift apart.

    *key* is never treated as a path. ``_safe_name`` rejects anything that is not
    a plain catalog key, and the candidate is composed from a root this loader
    already owns, so ``../`` reaches nothing. The complete key is required and
    matched namespace-first — ``team-b/review`` composes only
    ``<root>/team-b/review/SKILL.md`` and can never resolve ``team-a/review``.

    Every live gate still applies: the mapping must admit the candidate, a
    disabled app's skill stays hidden, the caller re-checks ``repo_scope``, and
    the body itself is read through :meth:`load_skill`'s fenced readers. The one
    thing withheld is the confined project tier, whose containment is only
    knowable from the enumeration — a project skill therefore waits for the walk
    rather than being read through a path this method composed.
    """
    if loader.catalog_status(project_dir) != "building" or not loader._safe_name(key):
        return None
    disabled_apps = loader._get_disabled_app_names()
    for root in (loader._dir, *loader._extra_paths):
        candidate = root / key / "SKILL.md"
        if not candidate.is_file():
            continue
        # Precedence is the enumeration's: the first root holding the key wins,
        # so a refusal here is final rather than a reason to try a lower root.
        if only is not None and not _catalog._matches_any(
            str(candidate), _catalog._with_canonical_globs(only, project_dir)
        ):
            return None
        if disabled_apps and loader._owning_app(key, candidate) in disabled_apps:
            return None
        return loader.load_skill(
            key, project_dir, max_bytes=max_bytes, refusal_reasons=refusal_reasons
        )
    return None


def search_skills_report(
    loader: SkillsLoader,
    query: str,
    limit: int = 20,
    *,
    project_dir: str | Path | None = None,
    only: list[str] | None = None,
    offset: int = 0,
    browse: bool = False,
) -> SkillSearchReport:
    """Rank total query coverage before rarity, metadata preference and usage.

    Partial matches remain available, with stable full keys and pagination.
    An empty browse request lists the complete resolved scope in key order.
    ``incomplete`` says this answer may be missing matches: an unfinished first
    walk, or body work this call's budget left undone.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    rows = loader.scoped_skills(project_dir=project_dir, only=only)
    # An unfinished first walk is the other way this answer can be partial, and
    # the caller cannot tell it from "no match" without being told: `incomplete`
    # already travels to the MCP tool and the dashboard, so the existing signal
    # carries it rather than a second one.
    building = loader.catalog_status(project_dir) == "building"
    offset = max(0, offset)
    if browse:
        return SkillSearchReport(
            sorted(rows, key=lambda s: str(s["key"]))[offset : offset + limit], building
        )
    rows = _listing._dedupe_identical_skills(rows)
    terms = sorted(sk.recall_terms((query or "").strip().lower()))
    if not terms:
        return SkillSearchReport([], building)
    # Ask every term across both surfaces: a metadata hit must not suppress
    # the other query words found only in the procedure.
    matched: dict[str, set[str]] = {}
    meta_counts: dict[str, int] = {}
    frequencies: dict[str, int] = {}
    live = [str(s["key"]) for s in rows if not s.get("confine_root")]
    bodies, bodies_incomplete = loader._body_matches(rows, terms, live, project_dir)
    metadata = loader._search_index.metadata_matches(terms) if loader._search_index else None
    for row in rows:
        key = str(row["key"])
        if metadata is None or not row.get("metadata_indexed"):
            vocabulary = sk.recall_terms(f"{key} {row['name']} {row['description']}".lower())
            meta_hits = {t for t in terms if any(word.startswith(t) for word in vocabulary)}
        else:
            meta_hits = metadata.get(str(row["path"]), set())
        hits = meta_hits | bodies.get(key, set())
        if hits:
            matched[key] = hits
            meta_counts[key] = len(meta_hits)
            for term in hits:
                frequencies[term] = frequencies.get(term, 0) + 1

    def rank(row: dict) -> tuple:
        key = str(row["key"])
        hits = matched.get(key, set())
        rarity = sum(math.log1p(len(rows) / max(1, frequencies[t])) for t in hits)
        usage = loader._usage.score(key)[0] if loader._usage else 0.0
        return (-len(hits), -rarity, -meta_counts.get(key, 0), -usage, key)

    candidates = [s for s in rows if str(s["key"]) in matched]
    return SkillSearchReport(
        sorted(candidates, key=rank)[offset : offset + limit], building or bodies_incomplete
    )


def resolve_dollar_skills(
    loader: SkillsLoader,
    text: str,
    project_dir: str | Path | None = None,
    *,
    only: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Resolve ``$skillname`` tokens in *text* to loadable skills.

    Scans *text* for ``$token`` occurrences (anywhere, multiple allowed) and
    matches a qualified token against its complete key first. An unqualified
    token matches a unique last path segment of an enumerated skill key — so ``$oncall-handover`` resolves the skill whose key is
    ``WorkforceEmploymentKnowledgeBase/oncall-handover``. Matching is
    case-insensitive on the leaf.

    Security (per input-validation guidance): this is allowlist-only. The
    token is *matched against* the vetted, already-enumerated skill set from
    ``_iter()`` — no filesystem path is ever built from the raw token. A
    token like ``$../../etc/passwd`` simply matches nothing. Content is loaded
    through ``load_skill`` (which inherits ``_safe_name`` + ``validate_file_path``
    + sensitive-path gating) and frontmatter is stripped before return.

    Returns a list of ``(token, skill_name, stripped_body)`` tuples — one per
    distinct resolved skill, in first-appearance order, deduped, and capped at
    ``_MAX_DOLLAR_SKILLS``. Unknown tokens are silently skipped (left literal by
    the caller). Returns an empty list if *text* has no resolvable tokens.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not text or "$" not in text:
        return []

    names = [str(s["key"]) for s in loader.scoped_skills(project_dir=project_dir, only=only)]
    exact = {name: name for name in names}
    leaves: dict[str, list[str]] = {}
    for name in names:
        leaves.setdefault(name.rsplit("/", 1)[-1].casefold(), []).append(name)

    resolved: list[tuple[str, str, str]] = []
    seen_names: set[str] = set()
    for match in sk._DOLLAR_SKILL_PATTERN.finditer(text):
        token = match.group(1)
        matched = exact.get(token)
        if matched is None and "/" not in token:
            choices = leaves.get(token.casefold(), [])
            matched = choices[0] if len(choices) == 1 else None
        if matched is None and token not in exact and not leaves.get(token.casefold()):
            # A built-in that moved or was renamed keeps answering to its old
            # name (a saved hook's `$prepare-pr`, an AGENTS.md line), but only
            # when no installed skill holds that name -- an ambiguous leaf
            # stays unresolved rather than quietly picking the built-in.
            matched = exact.get(sk._RELOCATED_SKILLS.get(token, ""))
        if matched is None or matched in seen_names:
            continue
        content = loader.read_scoped_skill(matched, only=only, project_dir=project_dir)
        if content is None:
            continue
        seen_names.add(matched)
        resolved.append((token, matched, loader.strip_frontmatter(content)))
        loader._record_use(matched)
        if len(resolved) >= sk._MAX_DOLLAR_SKILLS:
            break
    return resolved


def has_dollar_candidate(text: str) -> bool:
    """True if *text* contains at least one ``$skill``-shaped token.

    Distinguishes a genuine (if unresolved) skill-invocation attempt from
    an incidental ``$`` (e.g. ``$5``, ``$42``, ``$PATH``, a bare ``$``). The
    caller uses this to decide whether an empty ``resolve_dollar_skills``
    result is worth a ``not_found`` audit event — keeps the regex the single
    source of truth instead of duplicating it in chat_runner.

    Note: the token charset is digit-led (so a skill like ``5whys`` works via
    ``$5whys``), which means a purely numeric ``$5`` *matches the regex*. A
    bare price is not a skill attempt, so we additionally require the matched
    token to contain at least one letter before counting it as a candidate.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not text or "$" not in text:
        return False
    return any(
        any(c.isalpha() for c in m.group(1)) for m in sk._DOLLAR_SKILL_PATTERN.finditer(text)
    )

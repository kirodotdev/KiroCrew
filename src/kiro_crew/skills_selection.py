"""Which skills a session is offered: the always-loaded set, trigger matches under the live
per-message cap, the ``repo_scope`` gate, the scoped available set, and the startup index's
user-first order and the bounds it is rendered under.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields read: ``_edition_extra_paths``, ``_max_triggered``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import time
from collections.abc import Collection
from pathlib import Path
from typing import Callable

from kiro_crew.config import live
from kiro_crew.deploy import _SKILLS_DIR as _DEPLOY_SKILLS_DIR
from kiro_crew.project_scope import project_scope_satisfied
from kiro_crew.sel import sel
from kiro_crew.skills_builtin_provenance import _PROVENANCE_MARKER
from kiro_crew.skills_discovery import (
    _BUILTIN_SKILLS_DIR,
    _iter_skill_files,
    _trusted_skill_roots,
    _within_any,
)
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


#: Re-exported from ``trigger_match``, which owns the value and the grammar
#: it belongs to. Kept as a module name because tests and call sites here
#: reference it.
_MIN_TRIGGER_OVERLAP = MIN_TRIGGER_OVERLAP


#: Labels on one family line. A bound rather than a budget trim, so the line
#: cannot grow long enough to push a named skill (which carries the description,
#: the only text saying what a skill does) out of a tight allowance.
_FAMILY_LINE_MAX_LABELS = 6


# Lazy-load ranking (Mesh skill lazy-load): the session-start skills block only
# affords a bounded slice of the context budget, so on-demand skills are ranked
# by usage and summarized top-down; the tail is discoverable via `skill_search`.
# Per-skill description is truncated to this many chars in the summary line so a
# few verbose descriptions can't dominate the block. Sized as a guardrail against
# a pathological description rather than a routine trim: the description is the
# only signal the model has for deciding whether to load a skill, so the cap sits
# above the typical length (~290 chars across the built-in set) and bites only the
# outliers. Descriptions also arrive from the public registry, where their length
# is not ours to control — hence a cap rather than hand-trimming.
_SHORT_DESC_CHARS = 300
# A skill whose file mtime is within this window gets a recency boost in the
# ranking so a freshly-added, never-used skill still surfaces instead of being
# starved by the rich-get-richer usage ordering.
_NEW_SKILL_BOOST_WINDOW_SECS = 7 * 24 * 60 * 60
# The startup pointer names this many skills; the ranked index's head is the
# same width so both variants agree on which rows are "the front".
_INDEX_HEAD_SLOTS = 8
# Of those slots, how many the user's own skills may claim on provenance alone.
# Fewer than all of them, so a shipped skill with real usage keeps a name even
# under a large user tree — the mirror of the empty-ledger case this guards.
_INDEX_USER_SLOTS = 6


# The "[Skills:]" opener and "[End of skills]" closer wrapping the whole block.
_MAPPED_BLOCK_OVERHEAD_BYTES = 64


@functools.lru_cache(maxsize=1)
def _packaged_skill_names() -> frozenset[str]:
    """Keys of the skills this package ships, walked once per process.

    Two packaged trees install into the skills dir: ``builtin_skills/`` (the
    builtin sync) and the deploy layer's own ``deploy/skills/`` copies. Both are
    immutable while the process runs, so the one walk is the whole cost; the
    startup index asks this per row to tell a shipped skill from one the user
    wrote.
    """
    return frozenset(
        name
        for root in (_BUILTIN_SKILLS_DIR, _DEPLOY_SKILLS_DIR)
        if root.is_dir()
        for name, _ in _iter_skill_files(root)
    )


def _max_triggered_now(self) -> int:
    """The per-message trigger cap, read live.

    Read from the watcher's snapshot rather than the boot copy, so
    ``kirocrew config set skills.max_triggered`` applies to the very next
    message from any writer. The snapshot is a plain attribute read, which is
    what keeps this off the disk on a path that runs once per message --
    loading here would put two stats and a deepcopy in front of every message.

    Falls back to the construction-time value when there is no snapshot: a
    loader built from an explicitly injected config (tests, and any caller that
    already holds one) must honour that config rather than resolve a cap the
    injected document never carried, and the absent-key default is 0, which
    would suppress every skill.
    """
    cfg = live.snapshot()
    if cfg is None:
        return self._max_triggered
    try:
        return int(cfg.skills.max_triggered)
    except (AttributeError, TypeError, ValueError):
        logger.debug("skills.max_triggered read failed; using boot value", exc_info=True)
        return self._max_triggered


def _repo_scope_satisfied(relpath: str, project_dir: str | Path | None) -> bool:
    """Mechanical gate for repo-scoped skills (``repo_scope:`` frontmatter).

    A skill carrying ``repo_scope: <relpath>`` is only eligible for
    injection when *project_dir* (or an ancestor of it) contains *relpath*
    — e.g. ``repo_scope: src/kiro_crew`` restricts a skill to sessions
    whose active project IS the Kiro Crew source tree. This is the
    loader-enforced counterpart to a prose "ignore this skill elsewhere"
    scope guard: prose depends on probabilistic LLM obedience, while this
    check runs before the skill ever reaches the context (destructive
    repo-dev instructions must be mechanically contained).

    *project_dir* is the SESSION's active project — the same value the
    ``[PROJECT]`` context block names. The process working directory is
    deliberately NOT consulted: this runs in the gateway while it assembles
    context, so ``Path.cwd()`` is the gateway's own working directory and
    says nothing about the repository the session is working on. Reading it
    made the gate answer by install shape rather than by work: a gateway
    started from inside a checkout of the scoped repo admitted the skill
    into EVERY session, while a packaged install whose cwd holds no marker
    suppressed it for every session, contributors included.

    Fails CLOSED — no project, an unusable one, or any error suppresses the
    skill, so an un-scoped surface never inherits repo-specific rules.

    The rule itself lives in ``kiro_crew.project_scope`` because lessons are
    scoped by the same key: both are instructions injected into a session, so
    both must agree on what "in scope" means.
    """
    return project_scope_satisfied(relpath, project_dir)


def get_always_skills(self, project_dir: str | Path | None = None) -> list[str]:
    """Return names of skills marked ``always: true`` in frontmatter.

    *project_dir* is the session's active project, used only by the
    ``repo_scope`` gate; omitting it suppresses every repo-scoped skill
    (see :meth:`_repo_scope_satisfied` for why the gate cannot fall back
    to the process working directory).
    """
    result: list[str] = []
    for name, skill_file, _within in self._iter_visible(project_dir):
        meta = self._cached_frontmatter(skill_file, within=_within)
        if meta.get("always", "").strip().lower() == "true":
            # Stripped so a whitespace-only value means "no scope" here exactly as it
            # does at the other two gate call sites. The guard below tests this
            # value's TRUTHINESS, and `repo_scope: |` over a blank line now resolves
            # to a break rather than to "" -- truthy, so the gate would be handed
            # whitespace and refuse it, suppressing a skill its author never scoped.
            # A trailing break on a real path is NOT the concern:
            # `project_scope_satisfied` strips its own fragment, so `src/x\n` was
            # always gated as `src/x`.
            scope = meta.get("repo_scope", "").strip()
            if scope and not self._repo_scope_satisfied(scope, project_dir):
                continue
            result.append(name)
    return result


def get_triggered_skills(
    self,
    text: str,
    project_dir: str | Path | None = None,
    *,
    select: Callable[[], list[str] | None] | None = None,
    unusable: Collection[str] = (),
) -> list[str]:
    """Return names of skills whose triggers match the given text.

    Uses word-overlap matching with multi-word trigger phrases and
    negative keywords.  Triggers are comma-separated phrases in the
    ``triggers`` frontmatter field.  A phrase prefixed with ``!`` is a
    negative trigger — if *any* negative trigger matches, the skill is
    excluded regardless of positive matches.

    *project_dir* is the session's active project, used only by the
    ``repo_scope`` gate; omitting it suppresses every repo-scoped skill.

    *unusable* names skills the turn cannot use (their feature is off for the
    session). They are skipped like a ``repo_scope`` mismatch, so one never
    takes a slot under the cap.

    *select*, when given, may replace the matched set before the audit row
    is written; it returns ``None`` to keep the match. It is a callable, not
    a list, so the caller pays for it only when the match is being audited.

    Returns at most ``max_triggered`` matcher results sorted by best overlap
    score; the cap does not apply to a list *select* returns, which replaces
    them as given. A cap of zero (the shipped default) is the matcher
    switched off: no skill is scanned or scored and no trigger audit row is
    written; only *select* can still name skills.
    """
    scored: list[tuple[str, float]] = []
    # Skills a negative trigger actively excluded — a permission DENY that
    # must still be audited (see the audit event below).
    negated_skills: list[str] = []
    # The cap is read BEFORE the scan. At zero every skill scored below would
    # be sliced away, so the walk -- a frontmatter read, a repo-scope fence
    # check and a trigger score per visible skill, on every message -- would
    # buy nothing, and a `!` veto it recorded would be a DENY for a grant that
    # could never have happened. Tokenizing the message feeds only that
    # scoring, so it waits for the cap too. `select` still gets its turn: a
    # selection point owns its own zero-cap refusal.
    cap = self._max_triggered_now()
    visible = self._iter_visible(project_dir) if cap > 0 else ()
    text_words: set[str] = words_of(text) if cap > 0 else set()
    for name, skill_file, _within in visible:
        if name in unusable:
            continue
        # A reader on the per-message path: one SKILL.md that is not UTF-8
        # costs its own match, never the turn (rationale on the helper).
        meta = self._readable_frontmatter(skill_file, within=_within)
        if meta is None:
            continue
        if meta.get("always", "").strip().lower() == "true":
            continue
        triggers = meta.get("triggers", "")
        if not triggers:
            continue
        # Repo-scoped skills are mechanically suppressed outside their
        # repo — word-overlap can fire on ordinary user phrasing, and a
        # prose scope guard alone is probabilistic. Stripped so a
        # whitespace-only value reads as "no scope" at every gate call site
        # (see the always-on lister for why the truthiness test needs it).
        scope = meta.get("repo_scope", "").strip()
        if scope and not self._repo_scope_satisfied(scope, project_dir):
            continue

        # Scored by the shared primitive, not here: crew routing scores the
        # same trigger grammar, and two implementations would agree on the
        # easy cases and diverge on the ones that matter. `negated` stays
        # separate from the score because the DENY audit below has to tell
        # "scored nothing" apart from "scored well and was vetoed".
        best_overlap, negated = trigger_score(triggers, text_words)

        # Only record a negation as a DENY when the skill would otherwise
        # have triggered (positive overlap met the threshold) — that's the
        # case where the negative trigger actually changed the outcome.
        if negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
            negated_skills.append(name)
        elif not negated and best_overlap >= _MIN_TRIGGER_OVERLAP:
            scored.append((name, best_overlap))

    scored.sort(key=lambda x: x[1], reverse=True)
    triggered = [name for name, _ in scored[:cap]]

    # An external *select* runs BEFORE the audit below so the one row records
    # what is actually injected. Its three readings: a list replaces the
    # trigger match, ``[]`` is a real "no skill applies" that empties it, and
    # ``None`` (off, unusable answer, failure, expired budget) keeps it.
    selected = None
    if select is not None:
        try:
            selected = select()
        except Exception as exc:
            logger.debug("skills.select: selection failed (%s)", type(exc).__name__)
        if selected is not None:
            triggered = list(selected)

    # Emit ONE audit event for the matched + denied sets rather than one per
    # skill. A SEL entry per skill (incl. every non-match) on every message
    # would be N synchronous writes that dominate the per-message cost.
    # The security-relevant signals are which
    # skills were injected (permission grant) and which were excluded by a
    # negative trigger (permission deny); both are captured here. Skipped
    # entirely only when nothing triggered or was denied and no selection
    # ran (the common case): a selection that emptied the match is a row.
    if triggered or negated_skills or selected is not None:
        metadata = {"text_hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
        if triggered or selected is not None:
            metadata["skills"] = ",".join(triggered)
            # Record HOW each match was delivered, not just that it matched.
            # A pointer is an offer the agent may decline, so an auditor
            # reconstructing "was this procedure actually in the prompt?"
            # needs the split — the skill list alone does not answer it.
            bodies, pointers = self.split_triggered(triggered, project_dir)
            metadata["bodies"] = ",".join(bodies)
            metadata["pointers"] = ",".join(pointers)
        if selected is not None:
            # Which mechanism chose: an auditor reading an empty or widened
            # set needs to know it was a selection, not a matcher change.
            metadata["selected"] = "true"
        if negated_skills:
            metadata["negated"] = ",".join(negated_skills)
        sel().log_tool_invocation(
            session_key="skills",
            tool_name="skill_trigger",
            tool_kind="permission",
            outcome="triggered" if triggered else "denied",
            metadata=metadata,
        )
    return triggered


def _is_user_authored(self, s: dict) -> bool:
    """Whether *s* is a skill the user wrote rather than one Kiro Crew shipped.

    Shipped means a packaged built-in (by key, or by the provenance marker
    the builtin sync writes into every copy it installs — which also covers
    a retired built-in still on disk), a skill an app registered (its file
    resolves into a provider root), or an edition-contributed root. Anything
    else — a skill the user created in the skills dir, a ``skills.extra_paths``
    root, a trusted project's ``.kiro/skills``, an agent's ``skill://``
    mapping — is the user's.

    A confined project row answers before any filesystem call: resolving its
    path would reintroduce the link probe the confined walker exists to
    prevent, and a project skill is never shipped anyway.
    """
    if s.get("confine_root"):
        return True
    if str(s["key"]) in _packaged_skill_names():
        return False
    path = Path(str(s["path"]))
    if any(path.is_relative_to(root) for root in self._edition_extra_paths):
        return False
    if os.path.lexists(path.parent / _PROVENANCE_MARKER):
        return False
    return not _within_any(os.path.realpath(path), _trusted_skill_roots())


def _user_first(self, ranked: list[dict]) -> list[dict]:
    """Reorder *ranked* so the user's own skills lead without owning the head.

    The first ``_INDEX_HEAD_SLOTS`` positions hold up to ``_INDEX_USER_SLOTS``
    user-authored rows — the highest-ranked ones — and whatever positions
    those leave are filled from the rank order of everything else, shipped or
    overflow user rows alike. After the head, user rows precede shipped rows,
    each group in rank order.

    A new install has no usage history, so rank alone would let the ~60
    shipped skills take every slot and a skill the user just wrote is never
    named; reserving the whole head for the user instead lets a large user
    tree evict a shipped skill the user genuinely relies on. The quota fixes
    the first without causing the second. Order within each group is the
    caller's rank order, so the existing key tie-break still decides ties.
    """
    user_ids = {id(s) for s in ranked if self._is_user_authored(s)}
    head = [s for s in ranked if id(s) in user_ids][:_INDEX_USER_SLOTS]
    head_ids = {id(s) for s in head}
    rest = [s for s in ranked if id(s) not in head_ids]
    fill = _INDEX_HEAD_SLOTS - len(head)
    head += rest[:fill]
    tail = rest[fill:]
    return (
        head + [s for s in tail if id(s) in user_ids] + [s for s in tail if id(s) not in user_ids]
    )


def scoped_skills(
    self,
    *,
    project_dir: str | Path | None = None,
    only: list[str] | None = None,
) -> list[dict]:
    """The same available set for directory, search, list and explicit reads."""
    started = time.monotonic()
    entries = self._scoped_entries(project_dir, only)
    logger.debug(
        "skill enumeration: %.2fms, %d entries",
        (time.monotonic() - started) * 1000,
        len(entries),
    )
    rows = self.list_skills(project_dir, _entries=entries)
    return [
        row
        for row in rows
        if not row.get("repo_scope")
        or self._repo_scope_satisfied(str(row["repo_scope"]), project_dir)
    ]

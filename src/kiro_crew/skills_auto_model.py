"""What an auto-generated skill is: its namespace and on-disk layout, its provenance record and
rendered ``SKILL.md``, the claim refusal record, and the pending-queue observer hooks.

``kiro_crew.skills`` re-exports every name here, and the facade mirror sends a patch of one
through ``kiro_crew.skills`` to this module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


# ── Auto skill creation ──

# Namespace for auto-generated skills — keeps them out of the way of
# hand-authored skills.  Final path: ``~/.kiro/crew/skills/auto/<name>/SKILL.md``.
AUTO_SKILL_NAMESPACE = "auto"

# Archive area for retired auto-skills. A dot-prefixed dir so it is pruned from
# skill discovery (``_iter_skill_files``) — archived skills never trigger, but
# stay on disk and are restorable. Layout: ``auto/.archive/<slug>/SKILL.md``.
AUTO_ARCHIVE_DIRNAME = ".archive"

# Staging area for unapproved skill candidates. Dot-prefixed so it is pruned
# from discovery — pending candidates never trigger. Layout:
# ``auto/.pending/<slug>/{SKILL.md, scripts/, .meta.json}``.
AUTO_PENDING_DIRNAME = ".pending"

# One lock file for the whole auto slug space, held across an availability test
# and the claim it authorizes. Dot-prefixed and a plain file, so discovery skips
# it (``_iter_skill_files`` skips dot-dirs and reads only ``<name>/SKILL.md``).
# It lives at the skills root rather than inside ``auto/`` so taking it does not
# create the auto namespace as a side effect of a refused claim.
AUTO_SLUG_CLAIM_LOCK_NAME = ".auto-slug-claim.lock"

# The critical section is one directory test plus a small write, so a holder that
# has not released within seconds is stuck rather than busy. This overrides
# ``file_lock``'s own default ceiling downward, because the default suits a caller
# whose work may legitimately run long and a claim path's does not: refusing early
# and letting the next consolidation pass retry beats waiting on a dead holder.
AUTO_SLUG_CLAIM_LOCK_TIMEOUT_SECS = 5.0


@dataclass
class ClaimRefusal:
    """Whether a claim path's ``None`` was the lock, and so is worth retrying.

    Both claim paths return ``None`` for several unrelated reasons, and only one of
    them is transient. An invalid slug, an over-long procedure and an exhausted
    sibling walk are properties of the CANDIDATE: the same input refused once is
    refused forever, so a retry is waste. An unacquired claim lock is a property of
    the MOMENT -- another process held it, or could not be coordinated with -- and
    the comment above, plus ``skill_runtime.auto_skills._auto_slug_claim_lock``,
    both promise that the next pass retries. That promise is only keepable by a
    caller that can tell the two apart, and a bare ``None`` cannot.

    Pass an instance to a claim path to learn which it was. It is deliberately a
    mutable out-parameter rather than a raise or a changed return type: the lock
    helper documents that it never raises, because an escaping error would abort a
    consolidation pass mid-way, and every existing caller that does not care about
    the distinction keeps reading the same ``str | None``.
    """

    #: True only when the refusal was an unacquired claim lock.
    retryable: bool = False


# Per-skill version history. A dot-prefixed dir *inside* a live auto-skill
# (``auto/<slug>/.versions/v<N>-SKILL.md``) so it is pruned from skill discovery
# (``_iter_skill_files`` skips dot-dirs) — historical snapshots never trigger and
# never surface in list_skills / list_auto_skills. Written by
# ``approve_pending_update_checked`` before each live overwrite.
VERSIONS_DIRNAME = ".versions"

# Cap on retained per-skill version snapshots; oldest are pruned past this.
MAX_SKILL_VERSIONS = 20

# ── Pending-staged observer hook ──────────────────────────────────────────────
# A candidate can be staged by ANY ``SkillsLoader`` instance (consolidation uses
# the ContextBuilder's loader; dashboard requests build their own), so the
# observer is registered at MODULE level rather than per instance — otherwise a
# gateway-wired instance callback would silently miss the consolidation path that
# produces most candidates. The gateway registers a hook that raises a bell-feed
# notification + broadcasts ``skills.pending_changed``; CLI processes register
# nothing and simply stage silently.
_PENDING_STAGED_HOOK: "Callable[[dict], None] | None" = None


def set_pending_staged_hook(fn: "Callable[[dict], None] | None") -> None:
    """Register (or clear, with ``None``) the pending-candidate observer.

    Called once at gateway boot. Idempotent — a later call replaces the hook, so
    a re-created dashboard state does not stack duplicate notifications.
    """
    global _PENDING_STAGED_HOOK
    _PENDING_STAGED_HOOK = fn


def _emit_pending_staged(payload: dict) -> None:
    """Invoke the pending-staged hook, swallowing every failure.

    Staging has already succeeded on disk by the time this runs; a broken or
    slow observer must never turn a successful stage into a failure.
    """
    fn = _PENDING_STAGED_HOOK
    if fn is None:
        return
    try:
        fn(payload)
    except Exception:  # pragma: no cover - defensive
        logger.debug("pending-staged hook failed", exc_info=True)


# Counterpart observer for candidates LEAVING the queue (approved, dismissed,
# or TTL-pruned). Module-level for the same reason as the staged hook: any
# loader instance can consume a candidate. The gateway registers a hook that
# retires the candidate's bell-feed notification — without it, the "awaiting
# review" row stays unread forever and its deep link lands on the
# no-longer-awaiting-review banner.
_PENDING_CONSUMED_HOOK: "Callable[[dict], None] | None" = None


def set_pending_consumed_hook(fn: "Callable[[dict], None] | None") -> None:
    """Register (or clear, with ``None``) the pending-candidate consumed observer.

    Called once at gateway boot. Idempotent — a later call replaces the hook.
    """
    global _PENDING_CONSUMED_HOOK
    _PENDING_CONSUMED_HOOK = fn


def _emit_pending_consumed(payload: dict) -> None:
    """Invoke the pending-consumed hook, swallowing every failure.

    Consumption has already succeeded on disk by the time this runs; a broken
    observer must never turn a successful approve/dismiss into a failure.
    """
    fn = _PENDING_CONSUMED_HOOK
    if fn is None:
        return
    try:
        fn(payload)
    except Exception:  # pragma: no cover - defensive
        logger.debug("pending-consumed hook failed", exc_info=True)


# Frontmatter field that marks a skill as auto-generated.  Absence means
# the skill carries no source field, i.e. is hand-authored.
AUTO_SKILL_SOURCE_VALUE = "auto"

# Cap synthesized procedure markdown at 10 KB.  Longer outputs indicate
# the aux LLM failed to stay on-task and should be rejected.
AUTO_SKILL_MAX_PROCEDURE_CHARS = 10_240

# Byte bound on a pending candidate's two DOCUMENT files, ``SKILL.md`` and
# ``.meta.json``, for the detail read that informs approval. Derived from the
# limit above rather than chosen: the procedure is bounded in characters and a
# UTF-8 character is at most four bytes, so the body alone may legitimately
# reach four times the limit; the frontmatter around it (name, the one-line
# description and trigger list, the provenance fields, the heading) gets one
# more limit's worth of headroom, many times what the generator asks of those
# fields (a description of at most 150 characters, three to eight triggers),
# and ``.meta.json``, which repeats the description and triggers beside at most
# ``_PENDING_SCRIPT_MAX_ENTRIES`` script names and carries no procedure, fits
# under the same number. Staging bounds only the procedure, so a candidate whose
# frontmatter or metadata runs past the headroom is refused by this cap like any
# other over-cap document. It is a separate cap from ``MAX_SCRIPT_BYTES``,
# which bounds a bundled EXECUTABLE script and is the per-file cap under
# ``scripts/``: a document read under that cap refuses every valid generated
# skill whose SKILL.md is over 4 KiB as unreadable, with Approve disabled, for a
# candidate approve would promote. A document over this cap is refused whole;
# the read is bounded and never serves a partial view.
_PENDING_DOCUMENT_MAX_BYTES = (
    AUTO_SKILL_MAX_PROCEDURE_CHARS * 4  # the procedure, at UTF-8's four bytes per character
    + AUTO_SKILL_MAX_PROCEDURE_CHARS  # frontmatter headroom
)

# Regex for auto-generated skill name segment validation.  Deliberately
# restrictive — we control the generator so we don't need to accept
# arbitrary unicode.  ``_safe_name`` already rejects ``..`` and ``\``;
# this is an additional sanitization layer specific to auto-gen.
_AUTO_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


@dataclass(frozen=True)
class AutoSkillProvenance:
    """Immutable provenance record for an auto-generated skill.

    Serialized into the SKILL.md YAML frontmatter (``source: auto``,
    ``session_key``, ``created_at``, ``refined_at``, ``reuse_count``) so
    operators can always see how a skill was produced and when it was
    last refined.  Absence of ``source: auto`` identifies the skill as
    hand-authored.
    """

    session_key: str
    created_at: str  # ISO 8601 UTC
    refined_at: str = ""  # ISO 8601 UTC; empty until first refinement
    reuse_count: int = 0
    pinned: bool = False  # user-pinned: exempt from lifecycle eviction

    @staticmethod
    def now_iso() -> str:
        """Return the current time as an ISO 8601 UTC string."""
        return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    def to_frontmatter_lines(self) -> list[str]:
        """Serialize to the YAML key/value lines used in SKILL.md frontmatter."""
        lines = [
            f"source: {AUTO_SKILL_SOURCE_VALUE}",
            f"session_key: {self.session_key}",
            f"created_at: {self.created_at}",
        ]
        if self.refined_at:
            lines.append(f"refined_at: {self.refined_at}")
        if self.reuse_count:
            lines.append(f"reuse_count: {self.reuse_count}")
        if self.pinned:
            lines.append("pinned: true")
        return lines


def _build_auto_skill_content(
    *,
    slug: str,
    description: str,
    triggers: str,
    procedure_md: str,
    provenance: AutoSkillProvenance,
) -> str:
    """Render a complete ``SKILL.md`` body for an auto-generated skill.

    Layout::

        ---
        name: auto/<slug>
        description: <description>
        triggers: <comma-separated triggers>
        source: auto
        session_key: <session>
        created_at: <iso8601>
        refined_at: <iso8601>      # omitted if empty
        reuse_count: <int>         # omitted if 0
        ---

        # <slug> (auto-generated)

        <procedure_md>

    The leading ``---`` keeps this compatible with existing frontmatter
    parsing in ``SkillsLoader._parse_frontmatter``.  YAML values are
    single-line and newline-stripped to stay within the parser's
    ``key: value`` line format.
    """
    name = f"{AUTO_SKILL_NAMESPACE}/{slug}"
    desc_safe = re.sub(r"\s+", " ", description or "").strip() or name
    triggers_safe = re.sub(r"\s+", " ", triggers or "").strip()
    header_lines = [
        "---",
        f"name: {name}",
        f"description: {desc_safe}",
    ]
    if triggers_safe:
        header_lines.append(f"triggers: {triggers_safe}")
    header_lines.extend(provenance.to_frontmatter_lines())
    header_lines.append("---")
    # Normalize line endings, strip leading/trailing blanks so diffs
    # between revisions stay readable.
    body = procedure_md.replace("\r\n", "\n").strip()
    return "\n".join(header_lines) + "\n\n" + body + "\n"

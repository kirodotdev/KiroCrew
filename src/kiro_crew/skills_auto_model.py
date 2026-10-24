"""What an auto-generated skill is: its namespace and on-disk layout, its provenance record and
rendered ``SKILL.md``, the claim refusal record, and the pending-queue observer hooks.

It also holds what the claim protocol shares: the one auto-skill lock order, the quarantine,
claim, evidence and lock directory names, the private authority's provenance record names,
the scan and snapshot bounds, the immutable records a claim captures, and the refusals a
dismissal or a live mutation reports while its candidate or skill is still there.

``kiro_crew.skills`` re-exports every name here, and the facade mirror sends a patch of one
through ``kiro_crew.skills`` to this module.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from kiro_crew.constants import AUTO_SKILL_AUTHORITY_PARENT_DIRNAME

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

#: The one acquisition order for every auto-skill lock. A path takes any
#: subsequence of it, always left to right, and never blocks on an earlier lock
#: while it holds a later one, so no two paths can wait on each other. The one
#: out-of-order acquisition is a try-acquire that never waits: staging, under the
#: ``namespace`` lock, try-acquires ``claim`` locks to tell an active claim from
#: retired evidence (``SkillsLoader._pending_slug_claimed``).
#:
#: 1. ``slug-claim`` -- :data:`AUTO_SLUG_CLAIM_LOCK_NAME` at the skills root: one
#:    test-and-allocate across the live and pending halves of the slug space.
#:    Staging, a live publish (``create_auto_skill``) and a restore take it first.
#: 2. ``claim`` -- ``locks/claims/<claim>.lock`` under the private authority, held
#:    for the life of one claim transaction (approve, dismiss, unattended apply).
#:    Restart recovery only ever try-acquires it.
#: 3. ``target`` -- ``locks/target-<slug>.lock``: one live auto-skill. Promotion
#:    and recovery take it while holding their claim; a live publish and a
#:    restore take it inside the slug-claim lock; every other live mutation
#:    (update, pin, inject, archive, delete) takes it alone. A live name that is
#:    an alias of a canonical slug takes that slug's lock; a name no promotion
#:    can target under any spelling takes none.
#: 4. ``namespace`` -- ``locks/pending.lock``: the pending directory. Staging takes
#:    it inside the slug-claim lock; a claim rename and a refusal restore take it
#:    inside their claim lock.
#:
#: Publication itself never needs the slug-claim lock: it moves a stage into
#: ``auto/<slug>`` with a no-replace rename, so an occupied name refuses
#: atomically instead of being tested first.
AUTO_SKILL_LOCK_ORDER: tuple[str, ...] = ("slug-claim", "claim", "target", "namespace")


@dataclass
class ClaimRefusal:
    """Whether a claim path's ``None`` is transient and worth retrying.

    Claim paths return ``None`` for several unrelated reasons. An invalid slug,
    an over-long procedure and an exhausted sibling walk are properties of the
    CANDIDATE: the same input refused once is refused forever, so a retry is
    waste. An unacquired claim lock and an indeterminate active-claim inspection
    are properties of the MOMENT. The former means another process held the
    coordination lock; the latter means I/O could not prove the slug unclaimed.
    Both leave the candidate unwritten and promise that the next pass retries.
    That promise is only keepable by a caller that can distinguish them from a
    final candidate verdict, and a bare ``None`` cannot.

    Pass an instance to a claim path to learn which it was. It is deliberately a
    mutable out-parameter rather than a raise or a changed return type: the lock
    helper documents that it never raises, because an escaping error would abort a
    consolidation pass mid-way, and every existing caller that does not care about
    the distinction keeps reading the same ``str | None``.
    """

    #: True only when the refusal is transient and the same candidate should retry.
    retryable: bool = False
    #: Stable reason for a retryable refusal, or ``None`` for a final candidate verdict.
    reason: str | None = None


# Per-skill version history. A dot-prefixed dir *inside* a live auto-skill
# (``auto/<slug>/.versions/v<N>-SKILL.md``) so it is pruned from skill discovery
# (``_iter_skill_files`` skips dot-dirs) — historical snapshots never trigger and
# never surface in list_skills / list_auto_skills. Written by
# ``approve_pending_update_checked`` before each live overwrite.
VERSIONS_DIRNAME = ".versions"

# Cap on retained per-skill version snapshots; oldest are pruned past this.
MAX_SKILL_VERSIONS = 20


# Public, hidden holding area for candidate inodes that may have retained writers.
# It stays outside the masked private tree so a descriptor opened before the claim
# never crosses beneath a trusted ancestor. Dot-prefix discovery pruning keeps these
# consumed candidates out of the pending and live skill catalogs.
AUTO_QUARANTINE_DIRNAME = ".quarantine"

# Public holding area for detached old-live generations. These inodes were
# reachable by agent processes before publication, so they must never cross
# beneath private authority: a retained directory descriptor follows a rename and
# its ``..`` would bypass the path mask on the private root. Candidate quarantine
# stays separate so one claim name has one unambiguous role per parent.
AUTO_LIVE_QUARANTINE_DIRNAME = ".live-quarantine"

AUTO_CLAIMS_DIRNAME = "claims"
AUTO_EVIDENCE_DIRNAME = "evidence"

# ``pending.lock`` serializes pending publish/claim/restore; target-specific
# files serialize live updates or first publication to one auto-skill.
AUTO_LOCKS_DIRNAME = "locks"
_PROMOTE_LOCK_TIMEOUT_S = 10.0
_PROMOTE_LOCK_POLL_S = 0.05
_CLAIM_LOCK_MAX_STATE_BYTES = 1_500_000

#: Maximum names read from any one authority-state directory while startup decides
#: whether stale claim state is idle. Certification stays on the gateway's
#: readiness path, before the orchestrator exists and the dashboard socket binds,
#: so no agent this gateway spawns can run before the stale-state checks. Its
#: work is bounded regardless of planted state: one provenance read of at most
#: ``_AUTHORITY_PROVENANCE_MAX_BYTES`` and its MAC; only when provenance does not
#: verify, at most six directory scans of ``_STALE_CLAIM_SCAN_LIMIT + 1`` entries
#: each (authority root, ``claims/``, ``evidence/``, ``locks/claims/`` and both
#: public quarantines) plus two renames; when provisioning, one ``mkdir``, one
#: record write with its ``fsync`` and two directory syncs; and one rename probe
#: (create, rename and unlink of one empty file). An overflow is indeterminate and
#: disables auto-skills instead of extending that path.
_STALE_CLAIM_SCAN_LIMIT = 1024

#: Maximum unconsumed names counted while staging or recovery determines the
#: active claim namespace. A public quarantine name already retired into private
#: evidence is history and is not counted. This separately names the runtime
#: bound while keeping it aligned with startup's stale-authority scan ceiling.
_ACTIVE_CLAIM_SCAN_LIMIT = _STALE_CLAIM_SCAN_LIMIT


class _StaleClaimScanOverflow(RuntimeError):
    """A stale-authority directory exceeded the bounded startup scan."""


class _ActiveClaimScanOverflow(RuntimeError):
    """The active claim namespace exceeded its bounded runtime scan."""


def _bounded_stale_claim_names(
    target: int | Path,
    *,
    label: str,
    scanner=os.scandir,
) -> set[str]:
    """Read at most ``_STALE_CLAIM_SCAN_LIMIT`` names from one pinned directory."""
    names: set[str] = set()
    with scanner(target) as entries:
        for entry in entries:
            if len(names) >= _STALE_CLAIM_SCAN_LIMIT:
                raise _StaleClaimScanOverflow(
                    f"{label} exceeded the {_STALE_CLAIM_SCAN_LIMIT}-entry "
                    "stale-claim scan limit"
                )
            names.add(entry.name)
    return names


# Whole-generation snapshots retain file bytes until publication completes. Generated
# skill bodies and scripts are capped far below these ceilings, while live trees may also
# carry version history. Bound every allocation axis so a planted candidate or live tree
# fails closed instead of exhausting the gateway while it is authenticated.
_SKILL_SNAPSHOT_MAX_FILE_BYTES = 1024 * 1024
_SKILL_SNAPSHOT_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_SKILL_SNAPSHOT_MAX_ENTRIES = 512
_SKILL_SNAPSHOT_MAX_DEPTH = 32

# The provenance record is deliberately outside the authority root it certifies.
# ``tag-grants`` is a pre-existing, precreated whole-directory sandbox mask and
# file-tool deny floor, so an older sandbox cannot plant this record before the
# new authority leaf exists. The MAC under ``token_signing.key`` additionally
# makes a copied or hand-written record inert.
_AUTHORITY_PROVENANCE_PARENT = AUTO_SKILL_AUTHORITY_PARENT_DIRNAME
_AUTHORITY_PROVENANCE_NAME = "auto-skill-private-authority.json"
_AUTHORITY_PROVENANCE_VERSION = 2
_AUTHORITY_PROVENANCE_MAX_BYTES = 4096
_AUTHORITY_PROVENANCE_DOMAIN = b"kiro-crew:auto-skill-private-authority:v2\x00"


_AUTHORITY_RETIRE_COMMAND = "kirocrew skills authority-retire"


#: The refusal for a host whose agents run in a sandbox Kiro Crew does not build.
#: Pending maintainer confirmation of the supported platform contract; this is the
#: fail-closed choice.
_DELEGATED_SANDBOX_REFUSAL = (
    "agents on this host run in a sandbox Kiro Crew does not build (native Windows, "
    "or macOS with the Kiro CLI internal sandbox), which cannot hide the auto-skill "
    "authority from them; auto-skill staging and promotion stay off on this host"
)

# Stable refusal codes: callers and audit rows must distinguish an operator who
# explicitly disabled the sandbox from a delegated or unavailable protecting mask.
_SANDBOX_OFF_REFUSAL = "sandbox_off"
_SANDBOX_MASK_UNAVAILABLE_REFUSAL = "sandbox_mask_unavailable"

#: The refusal for a data home whose filesystem has no atomic no-replace rename.
_NO_REPLACE_RENAME_REFUSAL = (
    "the data home's filesystem has no atomic no-replace rename "
    "(renameat2(RENAME_NOREPLACE) on Linux, renameatx_np(RENAME_EXCL) on macOS), which "
    "auto-skill claims and publication need; move KIROCREW_HOME to a local filesystem "
    "that supports it. Auto-skill staging and promotion stay off on this data home"
)

# Name prefix of a no-replace rename probe's scratch file. A crash can leave one
# behind; it is never a claim, so the stale-authority check, lifecycle and claim
# recovery all skip it, and recovery removes one old enough to be orphaned.
_RENAME_PROBE_PREFIX = ".rename-probe-"

#: Minimum age before restart recovery treats a ``claims/`` rename probe as
#: orphaned. A live probe exists for one create-rename-unlink sequence; another
#: process can be inside one while recovery scans, and removing its file would
#: fail that process's claim. Measured against the file's own modification time.
_ORPHANED_RENAME_PROBE_MIN_AGE_S = 300.0

#: Maximum orphaned rename probes one recovery pass removes, so cleanup adds a
#: bounded amount of work to a pass that is itself bounded by
#: ``_ACTIVE_CLAIM_SCAN_LIMIT``.
_ORPHANED_RENAME_PROBE_CLEANUP_LIMIT = 64


@dataclass(frozen=True)
class _TaggedFileIdentity:
    """A serializable identity from an already-open native handle."""

    kind: str
    volume: int
    object_id: int | bytes


@dataclass(frozen=True)
class _CertifiedAuthorityBinding:
    """Native identities certified by the external authority provenance record."""

    configured_home: Path
    canonical_home: Path
    home_identity: _TaggedFileIdentity
    root_path: Path
    root_identity: _TaggedFileIdentity
    provenance_path: Path


@dataclass(frozen=True)
class _SkillTreeSnapshot:
    """One immutable tree generation captured from authenticated opened inodes."""

    files: dict[Path, bytes]
    file_modes: dict[Path, int]
    dir_modes: dict[Path, int]
    generation_hash: str
    root_identity: _TaggedFileIdentity


@dataclass(frozen=True)
class _ValidatedCandidateSnapshot:
    """One authenticated candidate generation captured at the claim boundary."""

    source_files: dict[Path, bytes]
    files: dict[Path, bytes]
    modes: dict[Path, int]
    metadata: dict[str, object]
    generation_hash: str


@dataclass(frozen=True)
class _ClaimSnapshot:
    """Immutable facts captured immediately before a pending claim rename.

    ``tree`` is process-local snapshot authority. The durable lock record keeps
    only the generation witness and metadata needed by restart recovery; no
    recovered path may publish a candidate without a fresh pre-claim snapshot.
    """

    generation_hash: str | None
    metadata_bytes: bytes | None
    tree: _SkillTreeSnapshot | None = field(default=None, compare=False, repr=False)
    quarantine_identity: _TaggedFileIdentity | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class _PinnedSkillParent:
    """One opened skill-state parent and the identity captured from its handle."""

    path: Path
    fd: int
    identity: tuple[int, int]
    native_identity: _TaggedFileIdentity


@dataclass(frozen=True)
class _PinnedPrivateState:
    """The public/private authority hierarchy held from canonical roots."""

    data_home: _PinnedSkillParent
    auto: _PinnedSkillParent
    pending: _PinnedSkillParent
    quarantine: _PinnedSkillParent
    live_quarantine: _PinnedSkillParent
    private: _PinnedSkillParent
    claims: _PinnedSkillParent
    evidence: _PinnedSkillParent
    locks: _PinnedSkillParent
    claim_locks: _PinnedSkillParent


def canonical_skill_text_hash(content: str | bytes) -> str:
    """Hash UTF-8 skill text after canonicalizing CRLF/CR newlines to LF.

    Text-mode reads normalize newlines while descriptor snapshots retain raw
    bytes.  A base-content binding must describe the logical SKILL.md text, not
    which platform wrote it; candidate-generation hashes remain byte-exact.
    """
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


# Informational observer for prose-only updates promoted without review. This is
# separate from the staged hook because the candidate is already live.
_UPDATE_AUTO_APPLIED_HOOK: "Callable[[dict], None] | None" = None


def set_update_auto_applied_hook(fn: "Callable[[dict], None] | None") -> None:
    """Register (or clear) the unattended-update observer."""
    global _UPDATE_AUTO_APPLIED_HOOK
    _UPDATE_AUTO_APPLIED_HOOK = fn


def _emit_update_auto_applied(payload: dict) -> None:
    """Invoke the unattended-update observer without affecting promotion."""
    fn = _UPDATE_AUTO_APPLIED_HOOK
    if fn is None:
        return
    try:
        fn(payload)
    except Exception:  # pragma: no cover - defensive
        logger.debug("update-auto-applied hook failed", exc_info=True)


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


class PendingDismissalRefused(Exception):
    """A dismissal was refused while its pending candidate is still staged.

    ``reason`` is ``promotion_disabled``: the data home still has an auto-skill
    authority root, so a dismissal must claim the candidate under that
    authority, and this process holds no certificate for it. ``detail`` is
    :func:`auto_skill_promotion_disabled_reason`; ``hint`` names the operator
    recovery where ``kirocrew skills authority-retire`` applies to this host, and
    is ``None`` elsewhere. Raised only by ``dismiss_pending_skill_checked``; the
    unsuffixed ``dismiss_pending_skill`` keeps its ``False`` for every caller.
    """

    def __init__(self, reason: str, *, detail: str, hint: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.hint = hint


class LiveSkillMutationRefused(Exception):
    """A live auto-skill delete or pin was refused while the skill is still there.

    ``reason`` is ``promotion_disabled``: the data home still has an auto-skill
    authority root, so the mutation must take that authority's target lock, and
    this process holds no certificate for it. ``detail`` and ``hint`` carry the
    same meaning as on :class:`PendingDismissalRefused`. Raised only by
    ``delete_skill_checked`` and ``set_pinned_checked``; the unsuffixed methods
    keep their ``False`` for every caller.
    """

    def __init__(self, reason: str, *, detail: str, hint: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.hint = hint


# Named by the path callers import them from: a traceback renders a raised class as
# ``module.qualname``, so the owner module must not leak into it.
PendingDismissalRefused.__module__ = "kiro_crew.skills"
LiveSkillMutationRefused.__module__ = "kiro_crew.skills"
_StaleClaimScanOverflow.__module__ = "kiro_crew.skills"
_ActiveClaimScanOverflow.__module__ = "kiro_crew.skills"

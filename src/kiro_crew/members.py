"""Per-crew-member space: ``$KIROCREW_HOME/members/<slug>/``.

A crew member is the same agent running with different context, so its space
holds what belongs to that member alone rather than to the user as a whole. The
first occupant is ``activity.jsonl`` — pointers to the sessions the member took
part in, which is the signal trigger generation reads.

The directory name is a **slug**: stable, immutable, and path-safe. A member's
display name is editable independently, so a rename never has to move files.
This mirrors the artifact store's ``artifacts/<slug>/`` layout, and reuses its
:func:`~kiro_crew.artifacts.slugify` so both surfaces normalize names the same
way.

Activity entries are pointers by design: they carry the session key, not a copy
of what happened. Details are read back from the session itself, so the log
cannot drift from the transcript — and it survives session pruning, which is why
frequency counts taken from it stay stable.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.artifacts import slugify
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: Directory under the data home holding one subdirectory per crew member.
MEMBERS_DIR_NAME = "members"

#: Append-only pointer log inside a member's directory.
ACTIVITY_FILE_NAME = "activity.jsonl"

# Same shape the artifact store enforces for its slugs: lowercase letters,
# digits and hyphens, 1-80 chars, no leading or trailing hyphen. Kept here as a
# local constant rather than imported because it is a private name there; the
# artifact store remains the source of truth for the spelling.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

#: The ONLY mode whose sessions may be recorded. An allowlist, not a denylist of
#: no-trace modes: a mode that is missing, empty (metadata not yet flushed for a
#: brand-new session) or simply unrecognized would pass a denylist and durably
#: record a private session key in a log that outlives session pruning. Failing
#: closed costs at most a missing entry in an advisory log.
_TRACEABLE_MEMORY_MODES = frozenset({"persistent"})


class MemberSlugError(ValueError):
    """Raised when a member slug is unusable or cannot be allocated."""


def members_root() -> Path:
    """Root directory for member spaces.

    Uses :func:`data_home` rather than :func:`config_dir`: this is reached from
    request and chat paths, and ``config_dir`` re-runs start-of-process
    maintenance (including a destructive leftover sweep) on every call.
    """
    return data_home() / MEMBERS_DIR_NAME


def validate_slug(slug: str) -> str:
    """Return *slug* unchanged when it is well-formed, else raise.

    The pattern admits no ``/``, ``.`` or whitespace, so a validated slug cannot
    traverse out of :func:`members_root` on its own. :func:`member_dir` still
    re-checks containment, because validation and use are separated by a call
    boundary a future caller could bypass.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise MemberSlugError(f"invalid member slug {slug!r}: must match {_SLUG_RE.pattern}")
    return slug


def slug_for_name(name: str) -> str:
    """Derive a candidate slug from a free-form member name.

    Not guaranteed unique: slugification is lossy, so two distinct member names
    can map to one slug. :func:`record_activity` stores the exact name in each
    entry so attribution survives that. Falls back to
    ``"member"`` when the name has no slug-safe characters, so a name written
    entirely in punctuation still yields something addressable.
    """
    base = slugify(name)
    # slugify falls back to its own module's noun; ours should read as a member.
    if base == "artifact":
        base = "member"
    return validate_slug(base)


def member_dir(slug: str) -> Path:
    """Absolute path to one member's directory, containment-checked.

    Does NOT create the directory; :func:`record_activity` creates it on demand.
    """
    validate_slug(slug)
    root = members_root().resolve()
    target = (root / slug).resolve()
    # Defence in depth behind validate_slug: a symlinked root, or a future
    # caller that skipped validation, must not land outside the members root.
    if target != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


def record_activity(
    member: str,
    session_key: str,
    memory_mode: str,
    *,
    project: str = "",
    via: str = "",
    dedupe_session: bool = False,
) -> bool:
    """Append one pointer entry to a member's activity log.

    Takes the member's NAME and derives the slug internally, so callers need no
    try/except: every failure path — a name that yields no usable slug, a
    read-only home, a torn write — is handled here and reported ``False``. This
    is best-effort by contract; a logging failure must never break the turn that
    triggered it, and one call site (``mcp_core``) has no logger of its own.

    ``memory_mode`` is REQUIRED and positional, not an opt-in keyword: it gates
    whether the session may be recorded at all, and a caller that simply forgot
    it would durably log a private session. It is matched against an allowlist
    (:data:`_TRACEABLE_MEMORY_MODES`), so an absent, empty or unrecognized mode
    skips the write rather than passing through.

    ``dedupe_session`` suppresses a repeat entry for a member/session pair. The
    chat site needs it because its ``is_new`` flag tracks the PROVIDER session,
    not the conversation: a dead provider cold-starts the same conversation with
    ``is_new=True`` again, which would append the same pointer twice and inflate
    the counts this log exists to feed. Routing decisions are NOT deduped — each
    ``select_crew`` bind is a distinct event even for one session.

    ``via`` records HOW the member was chosen, because the two call sites mean
    different things and a mixed log cannot be read apart afterwards:

    * ``"chat"`` — the human picked this member for the session.
    * ``"select_crew"`` — the orchestrator judged this member fits the task.

    A ``select_crew`` entry records the routing *decision*, not an execution:
    binding a crew does not oblige the model to delegate to it. That is the
    useful signal for trigger generation (what the router believes belongs to
    whom), but it means these counts are intent, not runs.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    if not member or not session_key:
        return False
    if memory_mode.strip().lower() not in _TRACEABLE_MEMORY_MODES:
        return False
    # The exact member name travels IN the record rather than being implied by
    # the directory. Slugification is lossy, so two distinct member names can
    # map to one slug ("Review_Agent" and "review-agent") and share a log;
    # carrying the name keeps per-member attribution recoverable in that case,
    # which the frequency signal downstream depends on.
    #
    # The session pointer is named for what it MEANS, not just what it holds.
    # A routing decision is recorded in the session that made it — the parent —
    # while the member itself runs in a different (sub-agent) session that does
    # not exist yet at bind time. Filing both under one `session` key would let
    # a consumer counting "sessions this member took part in" count a session
    # the member never ran in. Distinct keys make that misread impossible
    # instead of leaving it to the consumer to notice `via`.
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "member": member,
    }
    if via == "select_crew":
        entry["decided_in"] = session_key
    else:
        entry["session"] = session_key
    if project:
        entry["project"] = project
    if via:
        entry["via"] = via
    try:
        slug = slug_for_name(member)
        path = member_dir(slug)
        if dedupe_session and any(
            # Matched on BOTH fields: a colliding slug means one file can hold
            # two members, so session alone would suppress the wrong entry.
            # Only participation entries carry `session`, which is also the only
            # kind deduped — routing decisions are distinct events.
            r.get("session") == session_key and r.get("member") == member
            for r in read_activity(slug)
        ):
            return False
        path.mkdir(parents=True, exist_ok=True)
        # Newline on BOTH sides. The trailing one is ordinary JSONL framing; the
        # LEADING one is what survives a torn write. A record appended straight
        # after an interrupted write would otherwise be glued to that fragment,
        # losing BOTH to one unparseable line — and a leading newline alone is
        # not enough either, because the newest record would then carry no
        # terminator and be absorbed by whatever came next. read_activity skips
        # the blank lines this produces.
        line = "\n" + json.dumps(entry, ensure_ascii=False) + "\n"
        # No fsync: this is an advisory pointer log, and a durability barrier is
        # a blocking kernel syscall that would stall the shared event loop for
        # every concurrent session. Losing the final entry to a crash is
        # acceptable; stalling the gateway is not.
        with open(path / ACTIVITY_FILE_NAME, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception:
        logger.debug("member activity log write failed for %r", member, exc_info=True)
        return False


def read_activity(slug: str, limit: int = 0) -> list[dict]:
    """Return a member's activity entries, oldest first.

    Malformed lines are skipped rather than raising: the log is append-only from
    multiple processes, and one torn line must not make the whole history
    unreadable. ``limit`` > 0 returns only the most recent N.
    """
    try:
        path = member_dir(slug) / ACTIVITY_FILE_NAME
    except MemberSlugError:
        return []
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        logger.debug("member activity log read failed for %r", slug, exc_info=True)
        return []
    return out[-limit:] if limit > 0 else out

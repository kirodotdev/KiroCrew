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
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.artifacts import slugify
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.jsonl_util import (
    RECORD_CAP,
    UnreadableRecord,
    rotate_jsonl_at,
    strict_records,
)

logger = logging.getLogger(__name__)

#: Directory under the data home holding one subdirectory per crew member.
MEMBERS_DIR_NAME = "members"

#: Append-only pointer log inside a member's directory.
ACTIVITY_FILE_NAME = "activity.jsonl"

# Rotate a member's activity log once it exceeds this size, keeping ONE
# previous generation (``.jsonl.1``) — the same 1 MiB cap / ~2 MiB total
# shape as ``mcp_gateway.stub._FALLBACK_LOG_MAX_BYTES``. Entries are ~150
# bytes, so the two generations together hold thousands of the most recent
# pointers for :func:`read_activity`'s consumers (today the
# ``dedupe_session`` probe inside :func:`record_activity`) — while an
# unbounded log would grow forever (one append per participation event,
# from multiple processes, and nothing ever pruned it). The dedupe probe
# also reads this whole file synchronously on every deduped call, so the
# cap bounds that read as well as the disk.
_ACTIVITY_LOG_MAX_BYTES = 1024 * 1024

# Longest single RECORD the reader will materialise. Distinct from the file-size
# cap above and not implied by it: rotation only fires when the writer next
# appends, so a crafted newline-free line lands whole before any rotation sees
# it, and `for line in handle` would then allocate all of it at once. This log is
# agent-writable and its read feeds an append/suppress decision, so an over-cap
# record aborts the read (see :func:`_read_activity_checked`) rather than being
# skipped. Named here so a test can move the dial; real entries are ~150 bytes,
# so the shared cap has enormous headroom over anything legitimate.
_RECORD_CAP = RECORD_CAP

#: Crew-slug -> DM-thread binding inside a member's directory.
DM_FILE_NAME = "dm.json"
# Bindings live under the keystone-gated ``trust/`` subtree, NOT inside the
# member's own directory. The binding IS the thread's identity authority (the
# resume/send/thread-open guards all defer to it precisely because transcript
# metadata is operator-editable), so it must not sit on a path the agent's
# file tools can write: a prompt-injected write that re-points ``member`` at a
# colliding crew would hand that crew the thread's entire transcript at the
# next restore. ``trust/`` is already in the sensitive-path floor as a whole
# directory (like the SEL HMAC key and Spec Builder's decision record), and
# keystone writers open paths there directly, so the gateway keeps working.
DM_BINDINGS_DIR_NAME = "member-bindings"

#: Slot ``mode`` tag for member DM threads. The frontend's single
#: chat-ownership predicate (``isChatPageSurface``) does not admit it, so a
#: slot born with this mode is excluded from the ordinary Sessions list on
#: every consumer with no filtering code of its own.
DM_SLOT_MODE = "member"

#: Slot-key prefix for member DM threads (``member-<slug>``), following the
#: existing ``<kind>-<id>`` key convention (``chat-<N>-<ts>``, ``cron-<id>``).
DM_SLOT_KEY_PREFIX = "member-"


def is_member_session_key(session_key: str | None) -> bool:
    """Whether *session_key* addresses a crew member's pinned DM session.

    Member slots are created only by the members thread endpoint with keys of
    the form ``member-<slug>``. That slot name travels under several prefixes
    depending on the layer: ``dashboard_member-<slug>`` (the chat session key)
    and ``dashboard:member-<slug>`` (the canonical session-map alias, which is
    what reaches the provider factory). Accepts all three spellings so the
    predicate works at every layer a key travels through.
    """
    if not session_key:
        return False
    key = session_key
    for prefix in ("dashboard_", "dashboard:"):
        if key.startswith(prefix):
            key = key[len(prefix) :]
            break
    return key.startswith(DM_SLOT_KEY_PREFIX)


def select_provider_backend(
    explicit: str | None,
    session_key: str | None,
    member_backend: str,
    configured_default: str,
) -> str:
    """The per-session half of the ONE backend-selection gate (H3/H13).

    Precedence: an explicit caller pick, then the member-DM auto-route, then
    the configured default. Both non-default arms go through
    :func:`resolve_selected_backend` — the same governance/selectability gate
    the persisted field crosses, so a denied or unknown value degrades to kiro
    and the member thread runs as plain chat.

    Lives here rather than inline in ``create_provider_factory`` so the
    factory body stays a single selection CALL with no branching of its own:
    harness-parity H3/H13 allow exactly one selection gate on the construction
    path, and this function is an input to that gate, not a second one.
    """
    from kiro_crew.acp_backends import resolve_selected_backend

    if explicit:
        return resolve_selected_backend(explicit)
    if is_member_session_key(session_key):
        backend = resolve_selected_backend(member_backend)
        logger.info(
            "member session %s: routing to acp_backend=%r " "(agent.member_acp_backend=%r)",
            session_key,
            backend,
            member_backend,
        )
        return backend
    return configured_default


#: MCP server mounted per session into member DM threads — the delivery vehicle
#: for the member operating model (dispatch work into worker sessions, patrol
#: them). Session-level, so the on-disk agent template is untouched and every
#: other session on the same template keeps its ordinary tool set.
MEMBER_DISPATCH_SERVER = "kirocrew-dashboard"


def member_dispatch_session_server(session_key: str) -> dict[str, object] | None:
    """ACP ``session/new`` ``mcpServers`` element mounting session control.

    The entry carries ``KIROCREW_SESSION_KEY`` so the server's strict identity
    resolution names this member session — the same per-process trust channel
    the Claude backend's ``AcpClient`` uses. It rides the session-level param,
    which the KAS projection's credential stripping never touches (that filter
    applies to the agent-declared ``mcpServers`` block, not to what the host
    itself injects per session).

    ``None`` when the server command cannot be resolved — the member thread
    then runs as plain chat and the caller logs the degradation.
    """
    # circular import: agent's module graph is heavy and imports config, which
    # sits below this module for the thread-endpoint path.
    from kiro_crew.agent import _kirocrew_mcp_invocation

    try:
        command, args = _kirocrew_mcp_invocation("mcp-dashboard")
    except Exception:  # pragma: no cover - defensive; resolver logs its own reason
        logger.warning("member dispatch: could not resolve the dashboard server command")
        return None
    if not command:
        return None
    return {
        "name": MEMBER_DISPATCH_SERVER,
        "command": command,
        "args": list(args),
        "env": [{"name": "KIROCREW_SESSION_KEY", "value": session_key}],
        "type": "stdio",
    }


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


def dm_binding_path(slug: str) -> Path:
    """Absolute path to one member's DM-thread binding, containment-checked.

    Lives under the keystone-gated ``trust/`` subtree (see the note on
    ``DM_BINDINGS_DIR_NAME``): agent file tools cannot reach it, the gateway
    opens it directly. One flat ``<slug>.json`` per member — the slug is
    already validated to a safe charset, so the filename cannot traverse.
    Does NOT create the directory; :func:`write_dm_binding` does on demand.
    """
    validate_slug(slug)
    root = (data_home() / "trust" / DM_BINDINGS_DIR_NAME).resolve()
    target = (root / f"{slug}.json").resolve()
    # Defence in depth behind validate_slug, mirroring member_dir: a symlinked
    # component must not land the binding outside its trust-rooted directory.
    if target.parent != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


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


def member_slot_key(slug: str) -> str:
    """Derived, stable chat-slot key for a member's pinned DM thread.

    One slot per member, reused forever. Purely a derivation — nothing is read
    or written. The dashboard's slot layer normalizes keys to a filename-safe
    charset, but a validated slug is already inside that charset, so the
    derived key survives ``_normalize_slot_key`` unchanged; callers must still
    use the slot layer's RETURNED key as the source of truth.
    """
    return DM_SLOT_KEY_PREFIX + validate_slug(slug)


def read_dm_binding(slug: str) -> dict | None:
    """Return a member's DM-thread binding, or ``None`` when absent/unusable.

    Total by contract, like :func:`read_activity`: a bad slug, a missing file,
    an unreadable file, or a malformed payload all read as "not bound" — the
    binding is idempotently re-creatable, so degrading to re-creation is
    always safe and the caller needs no try/except.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    try:
        path = dm_binding_path(slug)
    except (MemberSlugError, OSError, RuntimeError):
        # member_dir resolves (and may mkdir) real filesystem paths, so an
        # unreadable directory or a symlink loop surfaces here — the totality
        # contract above says every such state reads as "not bound", and the
        # restore paths rely on that to survive any on-disk state at boot.
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        # Invalid UTF-8 is the same totality case as an unreadable file: the
        # binding reads as absent, never as a 500 out of every member API.
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    # A binding is only usable if it names the thread's slot and the exact
    # member it belongs to. Slugification is lossy (two crew names can share
    # one slug and therefore one dm.json), so the member name inside the
    # payload — not the directory — is what attributes the thread.
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("slot_key"), str)
        or not data["slot_key"]
        or not isinstance(data.get("member"), str)
        or not data["member"]
    ):
        return None
    # The slot key is a pure derivation of the slug; any other value is a
    # malformed or tampered binding. Accepting it would let dm.json point the
    # roster (and the page, which trusts `bound` rows enough to skip the
    # create POST) at an arbitrary unrelated session. Treat non-canonical as
    # absent — the thread endpoint then repairs it to the derived key.
    if data["slot_key"] != member_slot_key(slug):
        return None
    # And the member must actually BELONG to this slug: a tampered dm.json in
    # slug A's directory naming crew B (a real, registered crew whose slug
    # differs) would otherwise pin A's thread — and A's restored transcript —
    # to B's identity. Colliding names are fine: every name that slugifies to
    # this slug passes; anything else reads as absent.
    if slug_for_name(data["member"]) != slug:
        return None
    return data


def write_dm_binding(slug: str, *, member: str, slot_key: str) -> dict:
    """Persist a member's DM-thread binding atomically; return the record.

    ``slot_key`` must be the slug's own derivation — the same canonicality
    invariant :func:`read_dm_binding` enforces on the way back. Writing any
    other value would produce a binding that always reads as absent, so the
    mismatch is a caller bug worth failing loudly on.

    Raises :class:`MemberSlugError` on a bad slug and lets ``OSError``
    propagate: unlike the advisory activity log, the caller (the thread
    get-or-create endpoint) must know the binding did not land so it can
    report failure instead of advertising a thread that will not be found
    again.

    No fsync, deliberately: the binding is re-derivable — the slot key is a
    pure function of the slug and the endpoint that writes it is idempotent —
    so losing it to a crash costs one re-create, while a durability barrier
    would stall the event-loop thread pool for every thread open. The write
    itself is atomic (unique temp file + rename), so a torn file is never
    observable. Not a secret (a slot key and a crew name), so no owner-only
    permission tightening.
    """
    path = dm_binding_path(slug)
    if slot_key != member_slot_key(slug):
        raise ValueError(
            f"non-canonical dm binding slot_key {slot_key!r} for slug {slug!r} "
            f"(expected {member_slot_key(slug)!r}); such a binding always reads back as absent"
        )
    binding = {
        "member": member,
        "slug": slug,
        "slot_key": slot_key,
        "created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # The trust subtree is owner-only everywhere else (sel.py creates it 0o700);
    # a parents=True mkdir would otherwise leave a default-mode directory chain.
    # Best-effort: the sensitive-path floor is the real fence, the mode is
    # defence in depth, and a chmod failure must not cost the thread its binding.
    for _dir in (path.parent, path.parent.parent):
        try:
            platform_compat.restrict_dir_to_owner(_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", _dir, exc_info=True)
    # fsync: the binding is the thread's durability anchor — the orphan-history
    # guard REFUSES to rebind a slug whose binding is gone while its transcript
    # survives, so a binding lost to power failure after a transcript flush
    # would strand that transcript behind member_binding_missing. The binding
    # must be at least as durable as the transcript it attributes.
    atomic_write(path, json.dumps(binding, ensure_ascii=False), fsync=True)
    return binding


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
        if dedupe_session:
            prior, complete = _read_activity_checked(slug)
            if not complete:
                # Fail closed. An over-cap record was refused, so `prior` is a
                # prefix of the log and the probe below cannot prove this pair
                # is absent from the part it could not read. Appending anyway
                # would risk a duplicate participation entry, which inflates
                # the counts that drive trigger generation and routing. Not
                # recording is the same outcome the blanket handler below
                # already produces for any other read failure, so no caller
                # learns a new failure mode from this.
                logger.warning(
                    "member activity log unreadable in full; not recording %r to avoid a "
                    "duplicate entry",
                    member,
                )
                return False
            if any(
                # Matched on BOTH fields: a colliding slug means one file can hold
                # two members, so session alone would suppress the wrong entry.
                # Only participation entries carry `session`, which is also the only
                # kind deduped — routing decisions are distinct events.
                r.get("session") == session_key and r.get("member") == member
                for r in prior
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
        # Bound the log before appending. The helper's rotation is
        # try-lock-guarded (the log is append-only from multiple processes, so
        # unserialized rotation would let two writers hitting the cap together
        # discard a generation), best-effort, and never raises; a lost
        # try-lock skips rotating rather than waiting, so this call cannot
        # stall the shared event loop any more than the append itself.
        # History survives rotation: read_activity spans both generations, so
        # the `dedupe_session` probe keeps seeing rotated-aside entries.
        rotate_jsonl_at(path / ACTIVITY_FILE_NAME, _ACTIVITY_LOG_MAX_BYTES)
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

    The degrading view of :func:`_read_activity_checked`: it drops the
    completeness flag. A generation stopped by an over-cap record still
    contributes the entries it read BEFORE that record, so the caller sees a
    prefix rather than nothing -- correct for a caller that only displays or
    counts entries. A caller whose output feeds a durable decision must use
    :func:`_read_activity_checked` and honour the flag, because a prefix is
    indistinguishable from the whole log without it -- see the
    ``dedupe_session`` probe in :func:`record_activity`.
    """
    rows, _complete = _read_activity_checked(slug, limit)
    return rows


def _read_activity_checked(slug: str, limit: int = 0) -> tuple[list[dict], bool]:
    """Return a member's activity entries oldest first, and whether they are ALL of them.

    Reads the one rotated generation (``.jsonl.1``, see
    :data:`_ACTIVITY_LOG_MAX_BYTES`) before the live file — the same
    two-generation read as the stub fallback log's aggregator — so a
    rotation does not hide history from consumers: in particular the
    ``dedupe_session`` probe keeps suppressing a session pair whose entry
    was rotated aside. Malformed lines are skipped rather than raising: the
    log is append-only from multiple processes, and one torn line must not
    make the whole history unreadable. A generation that cannot be read is
    likewise skipped rather than discarding what the other generation
    yielded. ``limit`` > 0 returns only the most recent N across both
    generations.

    The second element is False when a record exceeded
    :data:`_RECORD_CAP` and was therefore refused. That case cannot be
    treated like a malformed line: this log is agent-writable, so one
    crafted newline-free line would otherwise be materialised whole, and
    :func:`kiro_crew.jsonl_util.strict_records` stops the read instead of
    skipping it. Skipping would be worse than losing the entry — the
    ``dedupe_session`` probe reads absence as "no prior entry" and appends a
    duplicate, inflating the participation counts this log exists to feed.
    So the flag is returned rather than swallowed, and the one caller that
    writes based on this read fails closed on it.
    """
    try:
        live = member_dir(slug) / ACTIVITY_FILE_NAME
    except MemberSlugError:
        return [], True
    out: list[dict] = []
    complete = True

    # Hold a shared (non-blocking) lock on the rotation lock file while
    # reading both generations.  This prevents a concurrent writer from
    # rotating the live file into .1 between the two reads, which could
    # cause records to be missed and duplicate session entries appended.
    lock_fd: int = -1
    lock_path = live.with_name(live.name + ".lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if not platform_compat.try_acquire_lock(lock_fd, exclusive=False):
            # Could not acquire; close and proceed without the lock.
            os.close(lock_fd)
            lock_fd = -1
    except OSError:
        lock_fd = -1

    try:
        for path in (live.with_name(live.name + ".1"), live):
            if not path.is_file():
                continue
            try:
                with open(path, "rb") as fh:
                    for line in strict_records(fh, path, cap=_RECORD_CAP):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(row, dict):
                            out.append(row)
            except UnreadableRecord:
                # The generation is abandoned at the record it could not
                # deliver, so what it yielded so far is a prefix, not the whole
                # of it. Keep those rows (they are real entries the caller may
                # display) but report the read as incomplete.
                #
                # "unreadable", not "over-cap": UnreadableRecord also covers a
                # record that is not valid UTF-8, and naming only the cap here
                # would send a reader looking for a size problem that may not
                # exist.
                complete = False
                logger.warning(
                    "member activity log has an over-cap record; %r read as incomplete", slug
                )
                continue
            except OSError:
                logger.debug("member activity log read failed for %r", slug, exc_info=True)
                continue
    finally:
        if lock_fd != -1:
            platform_compat.release_lock(lock_fd)
            os.close(lock_fd)

    return (out[-limit:] if limit > 0 else out), complete

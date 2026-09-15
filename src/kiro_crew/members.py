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
import shutil
import stat
import stat as stat_mod
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.artifacts import slugify
from kiro_crew.atomic_write import atomic_write, fsync_dir, fsync_dir_fd, read_bytes_with_retry
from kiro_crew.config.paths import data_home
from kiro_crew.jsonl_util import (
    RECORD_CAP,
    UnreadableRecord,
    rotate_jsonl_at,
    strict_records,
)
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    PinnedTree,
    open_in_pinned_parent,
    remove_tree_pinned,
    supports_pinned_walk,
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

#: Per-member PERMANENT RULES (the user-owned layer of the member system
#: prompt: approval boundaries, forbidden actions, evidence requirements).
#: Lives under the keystone-gated ``trust/`` subtree for the same reason the
#: DM binding does: these rules are precisely what the member must not be able
#: to rewrite for itself, so they cannot sit on a path the agent's file tools
#: can write. Only the gateway (via the dashboard rules endpoint — a human
#: action) writes here.
RULES_DIR_NAME = "member-rules"

#: Hard cap on a member's permanent-rules text. Enforced on WRITE (the write
#: is a human dashboard action, so a too-long payload is refused loudly)
#: rather than truncated on read — silently dropping the tail of a rules
#: document would drop rules.
MEMBER_RULES_MAX_CHARS = 4000

#: Per-member self-maintained briefing (the member-owned layer of the member
#: system prompt: current priorities, pointers to its own scripts and notes).
#: Lives in the member's OWN directory — deliberately agent-writable, since
#: the whole point is that the member curates what its future self wakes up
#: knowing. Precedence is fixed by injection order, not trust: rules outrank
#: the briefing because they are injected above it and named as user-owned.
BRIEFING_FILE_NAME = "briefing.md"

#: Cap on how much of the briefing is INJECTED per turn (working memory, not
#: an archive). Enforced on read with a visible truncation marker so the
#: member learns its briefing overflowed instead of silently losing the tail.
MEMBER_BRIEFING_MAX_CHARS = 4000
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
    session_key: str | None,
    member_backend: str,
    configured_default: str,
) -> str:
    """The per-session half of the ONE backend-selection gate (H3/H13).

    Precedence: the member-DM auto-route, then the configured default. The
    member arm goes through :func:`resolve_selected_backend` — the same
    governance/selectability gate the persisted field crosses, so a denied or
    unknown value degrades to kiro and the member thread runs as plain chat.

    Lives here rather than inline in ``create_provider_factory`` so the
    factory body stays a single selection CALL with no branching of its own:
    harness-parity H3/H13 allow exactly one selection gate on the construction
    path, and this function is an input to that gate, not a second one.
    """
    from kiro_crew.acp_backends import resolve_selected_backend

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

    The env also carries ``KIROCREW_BOUND_PORT`` — the port this gateway is
    actually serving. Unlike a chat session's MCP child, which inherits the
    gateway's whole environment, this entry is built from scratch, so without
    the port the child's resolution chain falls through to the run marker; that
    check needs :func:`platform_compat.find_listening_pids` (``lsof``), which
    sees no listener from inside the sandbox's user namespace, and the child
    then dials the default port. On a gateway bound anywhere else that is a
    connection refused on every dispatch call. ``KIROCREW_BOUND_PORT`` rather
    than ``KIROCREW_PORT`` because the latter means "the port an operator
    asked for" and is persisted, while this is the transient fact of what got
    bound.

    It also carries the same home override every managed Crew server carries
    (``_managed_mcp_env``): the server resolves *which gateway* to call from
    its data home, so on an install where ``KIROCREW_HOME`` is set (a pod, a
    second profile) an entry without it would present this member's identity to
    the default home's gateway, which has no such slot and refuses every verb
    as ``caller_unidentified``. On a default install the helper returns nothing
    and the entry is unchanged.

    ``None`` when the server command cannot be resolved — the member thread
    then runs as plain chat and the caller logs the degradation.
    """
    # circular import: agent's module graph is heavy and imports config, which
    # sits below this module for the thread-endpoint path.
    from kiro_crew.agent import _kirocrew_mcp_invocation, _managed_mcp_env

    # circular import, same shape: port_resolution reaches config.loader, whose
    # provider-backend path imports this module.
    from kiro_crew.port_resolution import resolve_serving_port

    try:
        command, args = _kirocrew_mcp_invocation("mcp-dashboard")
    except Exception:  # pragma: no cover - defensive; resolver logs its own reason
        logger.warning("member dispatch: could not resolve the dashboard server command")
        return None
    if not command:
        return None
    # The SAME home override every managed Crew server carries
    # (``_managed_mcp_env``): the server resolves the gateway to call from its
    # data home, so on an install with ``KIROCREW_HOME`` set (a pod, a second
    # profile) an entry without it would authenticate as this member to the
    # DEFAULT home's gateway — where the member slot does not exist and every
    # verb is refused as ``caller_unidentified``. Empty on a default install.
    env: list[dict[str, str]] = [{"name": k, "value": v} for k, v in _managed_mcp_env().items()]
    # Then the identity key.
    env.append({"name": "KIROCREW_SESSION_KEY", "value": session_key})
    # resolve_serving_port() reads KIROCREW_BOUND_PORT first and only then falls
    # through the client order, so one call covers both "the gateway exported the
    # port it bound" and "derive it" — and a malformed export is ignored rather
    # than forwarded.
    env.append({"name": "KIROCREW_BOUND_PORT", "value": str(resolve_serving_port())})
    return {
        "name": MEMBER_DISPATCH_SERVER,
        "command": command,
        "args": list(args),
        "env": env,
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


def _trust_child(name: str) -> Path:
    """``<data home>/trust/<name>`` taken LITERALLY: a link at ``trust`` or at
    the child refuses rather than resolves.

    ``trust`` is sandbox-VISIBLE (its SEL key and log have in-sandbox readers),
    so a spawned shell can replace one of these directories with a symlink; a
    caller that ``resolve()``-d the child would then land its ``<slug>.json``
    wherever the link points -- ``config.json``, for a slug named ``config`` --
    and a later unlink or replace would destroy that target. The path is built
    from the resolved data home (gateway-owned ancestors) and the two literal
    components below it.
    """
    trust = data_home().resolve() / "trust"
    child = trust / name
    for p in (trust, child):
        if platform_compat.is_link_or_junction(p):
            raise MemberSlugError(f"{p} is a symbolic link; refusing to use it")
        if p.exists() and not p.is_dir():
            raise MemberSlugError(f"{p} is not a directory; refusing to use it")
    return child


def dm_binding_path(slug: str) -> Path:
    """Absolute path to one member's DM-thread binding, containment-checked.

    Lives under the keystone-gated ``trust/`` subtree (see the note on
    ``DM_BINDINGS_DIR_NAME``): agent file tools cannot reach it, the gateway
    opens it directly. One flat ``<slug>.json`` per member — the slug is
    already validated to a safe charset, so the filename cannot traverse.
    Does NOT create the directory; :func:`write_dm_binding` does on demand.
    The directory is taken literally (:func:`_trust_child`): a link planted
    at ``trust`` or at ``member-bindings`` refuses instead of resolving.
    """
    validate_slug(slug)
    root = _trust_child(DM_BINDINGS_DIR_NAME)
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


#: Where a fired member's lived state goes (``members/.retired/``). Outside the
#: slug grammar (a slug cannot start with ``.``), so no member can be named it.
RETIRED_DIR_NAME = ".retired"
#: The record a retirement leaves beside the archived space.
FIRED_RECORD_FILE = "fired.json"


def retired_root() -> Path:
    """``members/.retired/``, pinned: created if absent, never a link.

    The members root is agent-writable, so an agent can plant ``.retired`` as
    a symlink and have a fire follow it -- the archive of another member's
    lived state, and the protected rules beside it, would then land wherever
    the link points. The root is therefore taken as a plain directory only: a
    link (or anything else that is not a directory) at that name raises
    :class:`MemberSlugError` and the fire stops before it archives anything.
    """
    root = members_root().resolve()
    retired = root / RETIRED_DIR_NAME
    if platform_compat.is_link_or_junction(retired) or (retired.exists() and not retired.is_dir()):
        raise MemberSlugError(f"{retired} is not a plain directory; refusing to archive into it")
    retired.mkdir(parents=True, exist_ok=True)
    # Re-checked after the mkdir: a link planted between the check and the
    # create would have been followed by mkdir(exist_ok=True).
    if platform_compat.is_link_or_junction(retired) or retired.resolve().parent != root:
        raise MemberSlugError(f"{retired} is not a plain directory; refusing to archive into it")
    return retired


def retired_rules_root() -> Path:
    """Where archived rules go: under the protected ``trust/`` rules subtree,
    never the agent-writable members root -- a fired member's permanent rules
    stay as unreachable to agent file tools as they were while it lived."""
    root = _trust_child(RULES_DIR_NAME)
    retired = root / RETIRED_DIR_NAME
    if platform_compat.is_link_or_junction(retired) or (retired.exists() and not retired.is_dir()):
        raise MemberSlugError(f"{retired} is not a plain directory; refusing to archive into it")
    retired.mkdir(parents=True, exist_ok=True)
    if platform_compat.is_link_or_junction(retired) or retired.resolve().parent != root:
        raise MemberSlugError(f"{retired} is not a plain directory; refusing to archive into it")
    return retired


def _open_trust_child_pinned(name: str, *, create: bool = False) -> int:
    """Open ``trust/<name>`` by descriptor: the data home by name (gateway-owned
    ancestors), then ``trust`` and *name* as literal children, ``O_NOFOLLOW``
    at each. A link or a plain file at either raises :class:`MemberSlugError`;
    an absent child is created when *create* is set, else ``FileNotFoundError``.
    """
    home_fd = _open_dir_nofollow(data_home().resolve())
    try:
        try:
            trust_fd = _open_dir_nofollow("trust", dir_fd=home_fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir("trust", 0o700, dir_fd=home_fd)
            trust_fd = _open_dir_nofollow("trust", dir_fd=home_fd)
        except OSError as exc:
            raise MemberSlugError(
                f"{data_home() / 'trust'} is not a plain directory; refusing to use it"
            ) from exc
    finally:
        os.close(home_fd)
    try:
        try:
            return _open_dir_nofollow(name, dir_fd=trust_fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(name, 0o700, dir_fd=trust_fd)
            return _open_dir_nofollow(name, dir_fd=trust_fd)
        except OSError as exc:
            raise MemberSlugError(
                f"{data_home() / 'trust' / name} is not a plain directory; refusing to use it"
            ) from exc
    finally:
        os.close(trust_fd)


def _retire_rules_pinned(slug: str, name: str, purge: bool) -> bool:
    """The POSIX flow of the rules step of :func:`retire_member_space`: the
    rules directory pinned by descriptor, the file ``lstat``-ed relative to it,
    then unlinked or renamed into the pinned ``.retired`` child -- one lookup
    per step, nothing resolved by name between the check and the move. True
    when a rules file was retired.

    The same rule as the member directory's move: a LINK at the rules name is
    unlinked as a link (relative to the pinned directory; its target is never
    followed, read or moved) -- left in place it would survive the fire, and
    the next member on the slug would inherit whatever rules it resolves to;
    anything else that is not a regular file (a directory, a device) is
    refused, so the fire keeps its marker and says so rather than clearing it
    over the obstacle.
    """
    try:
        rules_fd = _open_trust_child_pinned(RULES_DIR_NAME)
    except FileNotFoundError:
        return False
    try:
        try:
            st = os.lstat(f"{slug}.json", dir_fd=rules_fd)
        except FileNotFoundError:
            # No rules at the live name: an earlier attempt of THIS fire may
            # already have moved them into ``.retired/<name>.json`` (the
            # archive name is fixed in the fire's intent marker, so a resume
            # looks for exactly that entry). A resume that archives counts it
            # as retired; one that purges removes it -- left there, a purge
            # would report the rules gone while they sit in the trust subtree.
            return _resumed_rules_pinned(rules_fd, name, purge)
        if stat_mod.S_ISLNK(st.st_mode):
            os.unlink(f"{slug}.json", dir_fd=rules_fd)
            fsync_dir_fd(rules_fd, "member rules")
            return False
        if not stat_mod.S_ISREG(st.st_mode):
            raise MemberSlugError(
                f"rules of {slug!r} are not a regular file; remove the entry and fire again"
            )
        if purge:
            os.unlink(f"{slug}.json", dir_fd=rules_fd)
            fsync_dir_fd(rules_fd, "member rules")
            return True
        try:
            os.mkdir(RETIRED_DIR_NAME, 0o700, dir_fd=rules_fd)
        except FileExistsError:
            pass
        try:
            retired_fd = _open_dir_nofollow(RETIRED_DIR_NAME, dir_fd=rules_fd)
        except OSError as exc:
            raise MemberSlugError(
                f"{data_home() / 'trust' / RULES_DIR_NAME / RETIRED_DIR_NAME} is not a plain "
                "directory; refusing to archive into it"
            ) from exc
        try:
            os.rename(f"{slug}.json", f"{name}.json", src_dir_fd=rules_fd, dst_dir_fd=retired_fd)
            # Both ends of the move, before the fire goes on to clear the
            # marker that would otherwise resume it (see retire_member_space).
            fsync_dir_fd(retired_fd, "retired member rules")
            fsync_dir_fd(rules_fd, "member rules")
        finally:
            os.close(retired_fd)
        return True
    finally:
        os.close(rules_fd)


def _resumed_rules_pinned(rules_fd: int, name: str, purge: bool) -> bool:
    """The rules step's answer when nothing sits at the live name: the entry an
    interrupted attempt already archived under *name*, if any (relative to the
    pinned rules directory, ``.retired`` opened ``O_NOFOLLOW``). Absent both
    -> False; archived -> True, unlinked first when this resume purges."""
    try:
        retired_fd = _open_dir_nofollow(RETIRED_DIR_NAME, dir_fd=rules_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MemberSlugError(
            f"{data_home() / 'trust' / RULES_DIR_NAME / RETIRED_DIR_NAME} is not a plain "
            "directory; refusing to use it"
        ) from exc
    try:
        try:
            st = os.lstat(f"{name}.json", dir_fd=retired_fd)
        except FileNotFoundError:
            return False
        if not stat_mod.S_ISREG(st.st_mode):
            raise MemberSlugError(
                f"archived rules {name!r} are not a regular file; remove the entry and fire again"
            )
        if purge:
            os.unlink(f"{name}.json", dir_fd=retired_fd)
        return True
    finally:
        os.close(retired_fd)


def new_archive_name(slug: str) -> str:
    """The archive entry a fire of *slug* will land in: ``<slug>--<stamp>``.

    Chosen by the caller BEFORE anything moves and recorded in the fire's intent
    marker, so an interrupted archive (the rename done, ``fired.json`` not yet
    written) resumes into the SAME entry instead of minting a second one and
    leaving the first unrecorded. Unique enough on its own -- a stamp to the
    second plus a short random tail -- that no listing of the archive root is
    needed to pick it.
    """
    validate_slug(slug)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{slug}--{stamp}-{os.urandom(2).hex()}"


def retire_member_space(
    slug: str,
    *,
    member: str,
    record: dict,
    purge: bool = False,
    archive_name: str | None = None,
) -> Path | None:
    """Archive (or, with *purge*, destroy) a fired member's lived state.

    The lived state is ``members/<slug>/`` -- activity log, briefing -- plus,
    under ``trust/``, the member's rules and its DM-thread binding. Archive
    moves the directory to ``members/.retired/<archive_name>/`` in one rename
    and writes ``fired.json`` there (*record*: member id, display name, the
    thread's slot and history keys, when, from which template), so the thread
    the History tab still lists can be tied back to the colleague it belonged
    to; the rules file moves to ``trust/<rules>/.retired/<archive_name>.json``
    -- the same protected subtree it lived in, never the agent-writable
    archive. Purge removes the directory and the rules instead. The binding
    goes only when it names *member*: a binding is only usable when it names
    the exact member, and one naming somebody else on the same slug is not
    this fire's to remove. Returns the archive path, or ``None`` when nothing
    was archived (a member that never wrote anything, or a purge).

    *archive_name* (:func:`new_archive_name`, recorded in the fire's intent
    marker by the caller) is the entry every attempt of one fire uses, which
    is what makes an interrupted archive **resumable**: a directory already
    under that name is the earlier attempt's rename, so the resume writes the
    missing ``fired.json`` into it rather than archiving twice; a resume asked
    to purge removes that exact entry. Every other step is idempotent too -- a
    directory, rules file or binding already gone is skipped, never an error.

    On POSIX every step is relative to pinned descriptors: the members root is
    opened as the literal ``members`` child of the data home with
    ``O_NOFOLLOW`` (a link planted AT the root is refused with
    :class:`MemberSlugError`, never followed -- otherwise a purge would delete,
    and an archive would land, wherever it points), the archive root as the
    literal ``.retired`` child of that descriptor, and the member's entry is
    ``lstat``-ed, unlinked or renamed relative to them. The path flow (Windows)
    keeps the ``lstat`` refusals a planted link needs no race to defeat.
    Blocking IO: call off the loop.
    """
    validate_slug(slug)
    name = archive_name or new_archive_name(slug)
    if not name.startswith(f"{slug}--") or "/" in name or "\\" in name or name in (".", ".."):
        raise MemberSlugError(f"archive name {name!r} is not one this member's fire may use")
    # The rules LEAF, literal and unresolved: ``member_rules_path`` resolves the
    # name and refuses a link that escapes the subtree, but a retire must see
    # the entry as it is -- a link at the name is removed as a link, and one
    # resolving to another member's rules must never be what gets moved.
    rules = _trust_child(RULES_DIR_NAME) / f"{slug}.json"
    archived: Path | None = None
    text = json.dumps(dict(record, member=member, slug=slug), indent=2, ensure_ascii=False)

    if _DIR_FD_SUPPORTED:
        archived = _retire_dir_pinned(slug, name, purge, text)
    else:
        root = members_root()
        if platform_compat.is_link_or_junction(root) or (root.exists() and not root.is_dir()):
            raise MemberSlugError(f"{root} is not a plain directory; refusing to retire under it")
        root = root.resolve()
        target = root / slug
        if platform_compat.is_link_or_junction(target):
            # A symlink OR a Windows junction (``is_symlink`` is False for a
            # junction, and ``rmtree`` through one destroys the target): the
            # link itself goes, its target untouched.
            platform_compat.unlink_link_or_junction(target)
        elif target.exists() and not target.is_dir():
            # Same refusal as the pinned flow: an obstacle at the member's name
            # is not a space to retire, and the next member could not create
            # its directory behind it.
            raise MemberSlugError(
                f"member space {slug!r} is not a directory; remove it and fire again"
            )
        elif target.is_dir():
            # Containment, as member_dir checks it: a resolved path outside the
            # root is not this member's space.
            if root not in target.resolve().parents:
                raise MemberSlugError(f"member slug {slug!r} escapes {root}")
            if purge:
                shutil.rmtree(target)
            else:
                dest = retired_root() / name
                if dest.exists():
                    raise MemberSlugError(
                        f"member space {slug!r} exists again beside its archive {name!r}; "
                        "remove the recreated space and fire again"
                    )
                os.rename(target, dest)
                fsync_dir(dest.parent)
                fsync_dir(root)
                archived = dest
        if not purge:
            earlier = root / RETIRED_DIR_NAME / name
            if earlier.is_dir() and not platform_compat.is_link_or_junction(earlier):
                archived = earlier
        elif (root / RETIRED_DIR_NAME / name).is_dir():
            if platform_compat.is_link_or_junction(root / RETIRED_DIR_NAME / name):
                raise MemberSlugError(
                    f"archive entry {name!r} is a link; refusing to remove through it"
                )
            shutil.rmtree(root / RETIRED_DIR_NAME / name)
        if archived is not None:
            _write_fired_record(archived, text)
    if _DIR_FD_SUPPORTED:
        # Pinned: ``trust`` and ``member-rules`` opened as literal children,
        # the file moved relative to them -- a directory link an agent planted
        # at either name is refused, never followed into ``os.replace``.
        rules_retired = _retire_rules_pinned(slug, name, purge)
    else:
        # The path flow: ``member_rules_path`` has already refused a link at
        # ``trust`` or ``member-rules`` (``_trust_child``); the leaf is checked
        # by ``lstat`` -- a link is removed as a link, anything but a regular
        # file is refused, as in the pinned flow.
        rules_retired = False
        try:
            leaf = os.lstat(rules)
        except FileNotFoundError:
            leaf = None
        if leaf is not None and stat_mod.S_ISLNK(leaf.st_mode):
            rules.unlink()
        elif leaf is not None and not stat_mod.S_ISREG(leaf.st_mode):
            raise MemberSlugError(
                f"rules of {slug!r} are not a regular file; remove the entry and fire again"
            )
        elif leaf is not None:
            if purge:
                rules.unlink()
            else:
                dest = retired_rules_root() / f"{name}.json"
                os.replace(rules, dest)
                fsync_dir(dest.parent)
            fsync_dir(rules.parent)
            rules_retired = True
        else:
            # Nothing at the live name: the entry an interrupted attempt of
            # this fire already archived under *name* counts as retired (and
            # goes when this resume purges) -- as in the pinned flow.
            archived_rules = _trust_child(RULES_DIR_NAME) / RETIRED_DIR_NAME / f"{name}.json"
            try:
                arch = os.lstat(archived_rules)
            except FileNotFoundError:
                arch = None
            if arch is not None:
                if not stat_mod.S_ISREG(arch.st_mode):
                    raise MemberSlugError(
                        f"archived rules {name!r} are not a regular file; remove the entry "
                        "and fire again"
                    )
                if purge:
                    archived_rules.unlink()
                rules_retired = True
    if rules_retired and not purge and archived is None:
        if _DIR_FD_SUPPORTED:
            archived = _retire_dir_pinned(slug, name, False, text, create=True)
        else:
            archived = retired_root() / name
            _write_fired_record(archived, text)
    remove_dm_binding_of(slug, member)
    return archived


def _retire_dir_pinned(
    slug: str, name: str, purge: bool, text: str, *, create: bool = False
) -> Path | None:
    """The POSIX flow of :func:`retire_member_space` for the member directory:
    root and archive root pinned by descriptor, one lookup per step. Returns
    the archive path when an archive entry exists afterwards (renamed now, or
    an earlier attempt's) with ``fired.json`` written into it; ``None`` when
    nothing was archived. *create* makes the archive entry even when no
    directory lived (a rules-only archive still gets its ``fired.json``).
    """
    home = data_home().resolve()  # trust-rooted: the one path resolved by name
    try:
        home_fd = _open_dir_nofollow(home)
    except FileNotFoundError:
        return None
    try:
        try:
            root_fd = _open_dir_nofollow(MEMBERS_DIR_NAME, dir_fd=home_fd)
        except FileNotFoundError:
            if not create:
                return None  # no members root yet: nothing lived
            os.mkdir(MEMBERS_DIR_NAME, dir_fd=home_fd)
            root_fd = _open_dir_nofollow(MEMBERS_DIR_NAME, dir_fd=home_fd)
        except OSError as exc:  # ELOOP (a planted link), ENOTDIR (a file)
            raise MemberSlugError(
                f"{home / MEMBERS_DIR_NAME} is not a plain directory; refusing to retire under it"
            ) from exc
    finally:
        os.close(home_fd)
    try:
        _move_member_dir_pinned(root_fd, slug, purge, name)
        try:
            os.mkdir(RETIRED_DIR_NAME, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            retired_fd = _open_dir_nofollow(RETIRED_DIR_NAME, dir_fd=root_fd)
        except OSError as exc:
            raise MemberSlugError(
                f"{members_root() / RETIRED_DIR_NAME} is not a plain directory; "
                "refusing to archive into it"
            ) from exc
        try:
            try:
                st = os.lstat(name, dir_fd=retired_fd)
            except FileNotFoundError:
                st = None
            if purge:
                if st is not None and stat_mod.S_ISDIR(st.st_mode):
                    # An earlier attempt archived it; this request says purge.
                    _remove_member_tree(
                        members_root() / RETIRED_DIR_NAME / name,
                        (st.st_dev, st.st_ino),
                        what="archived member space",
                    )
                    fsync_dir_fd(retired_fd, "retired members")
                return None
            if st is None:
                if not create:
                    return None
                os.mkdir(name, dir_fd=retired_fd)
                fsync_dir_fd(retired_fd, "retired members")
            elif not stat_mod.S_ISDIR(st.st_mode):
                raise MemberSlugError(f"archive entry {name!r} is not a plain directory")
            _write_fired_record_at(retired_fd, name, text)
            return members_root() / RETIRED_DIR_NAME / name
        finally:
            os.close(retired_fd)
    finally:
        os.close(root_fd)


#: ``dir_fd``-relative rename/open/lstat are what pin a directory by descriptor
#: (POSIX). Windows has none of them; the path-based flow with its lstat checks
#: is what runs there (symlinks need a privilege on Windows in the first place).
_DIR_FD_SUPPORTED = (
    os.rename in os.supports_dir_fd
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


def _open_dir_nofollow(path: str | Path, *, dir_fd: int | None = None) -> int:
    """Open *path* as a directory without following a link at its final
    component; ``NotADirectoryError``/``OSError`` when it is not a plain dir."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _move_member_dir_pinned(root_fd: int, slug: str, purge: bool, name: str) -> None:
    """Retire ``members/<slug>/`` with every step relative to pinned descriptors.

    A path-based ``is_dir()`` then ``os.rename(path)`` is two lookups of an
    agent-writable name: between them the agent can swap the directory for a
    symlink, and the rename (or the writes that follow) then acts on whatever
    the link points at. Here the members root arrives already open (the literal
    ``members`` child of the data home, ``O_NOFOLLOW``), the archive root is
    opened relative to it the same way, the entry is ``lstat``-ed relative to
    the root descriptor, a link is unlinked relative to it, a directory is
    renamed relative to both descriptors -- one lookup each, nothing between a
    check and its use resolves the name again. A directory already under the
    archive *name* (an earlier attempt's rename) is left for the caller to
    record; the member directory, if one lived again, stays.

    The name is still the agent's to re-point between the ``lstat`` and the
    step that acts on it (a rename of a COLLEAGUE's directory onto this slug
    is one ``mv`` in an agent-writable tree), so every step that acts checks
    it acted on the very directory the ``lstat`` saw -- ``(st_dev, st_ino)``
    -- and stops when it did not: the purge compares the opened descriptor
    before a byte is removed; the archive compares the entry at its new name
    and renames it BACK before refusing, so a swapped-in directory is neither
    purged nor archived, and the fire keeps its marker and says so.
    """
    try:
        st = os.lstat(slug, dir_fd=root_fd)
    except FileNotFoundError:
        return
    expected = (st.st_dev, st.st_ino)
    if stat_mod.S_ISLNK(st.st_mode):
        os.unlink(slug, dir_fd=root_fd)
        return
    if not stat_mod.S_ISDIR(st.st_mode):
        # A plain file (or a device, a socket) at the member's name is not a
        # space this fire can archive or purge -- and left there, the next
        # member on the slug cannot create its directory, so its activity
        # writes fail silently. Refuse, so the fire keeps its marker and says
        # so, rather than clearing it over the obstacle.
        raise MemberSlugError(f"member space {slug!r} is not a directory; remove it and fire again")
    if purge:
        # The whole-tree removal is the shared pinned one: the parent chain is
        # re-pinned one ``openat`` per component (a link planted at ``members``
        # in between is refused, never followed), the entry is opened
        # ``O_NOFOLLOW`` through it, and ``approve`` confirms -- through that
        # descriptor -- that it IS the directory the lstat above saw, before a
        # byte is removed.
        _remove_member_tree(members_root() / slug, expected, what="member space")
        fsync_dir_fd(root_fd, "members root")
        return
    try:
        os.mkdir(RETIRED_DIR_NAME, dir_fd=root_fd)
    except FileExistsError:
        pass
    try:
        retired_fd = _open_dir_nofollow(RETIRED_DIR_NAME, dir_fd=root_fd)
    except OSError as exc:
        raise MemberSlugError(
            f"{members_root() / RETIRED_DIR_NAME} is not a plain directory; "
            "refusing to archive into it"
        ) from exc
    try:
        try:
            os.lstat(name, dir_fd=retired_fd)
        except FileNotFoundError:
            os.rename(slug, name, src_dir_fd=root_fd, dst_dir_fd=retired_fd)
            moved = os.lstat(name, dir_fd=retired_fd)
            if (moved.st_dev, moved.st_ino) != expected:
                # Not the directory the lstat saw: something re-pointed the
                # name in between. Put it back where it was and refuse; the
                # marker stays, so the fire is resumable once the tree is
                # what it claims to be.
                os.rename(name, slug, src_dir_fd=retired_fd, dst_dir_fd=root_fd)
                raise MemberSlugError(
                    f"member space {slug!r} changed while it was being archived; fire again"
                )
            # DURABLE before the caller may clear the fire's marker: the
            # rename is metadata in two directories, and neither is on disk
            # until its directory is synced. ``fired.json``'s own fsync covers
            # its bytes only; a power loss after the marker's unlink reached
            # disk but before these did would leave the member's row gone, its
            # space back at the live name and no marker to resume from.
            fsync_dir_fd(retired_fd, "retired members")
            fsync_dir_fd(root_fd, "members root")
            return
        # Both exist: the earlier attempt renamed the space away, and something
        # -- an agent, most likely -- has since recreated ``members/<slug>``. A
        # resume that skipped the source would clear the marker with live state
        # still on disk for a same-name colleague to inherit; refusing keeps the
        # marker, so the fire stays resumable once the recreated space is gone.
        raise MemberSlugError(
            f"member space {slug!r} exists again beside its archive {name!r}; "
            "remove the recreated space and fire again"
        )
    finally:
        os.close(retired_fd)


def _remove_member_tree(path: Path, expected: tuple[int, int], *, what: str) -> None:
    """Purge a member's directory (live or archived) through
    :func:`kiro_crew.pinned_fs.remove_tree_pinned`, the one descriptor-pinned
    whole-tree removal, with the fire's own semantics on top: *expected* is the
    ``(st_dev, st_ino)`` the caller's ``lstat`` saw, re-checked through the
    opened descriptor before anything is removed, and a tree that would not go
    -- refused by that check, or with survivors -- RAISES ``MemberSlugError`` so
    the fire keeps its marker and says so, where the helper returns.
    """

    def _same_directory(root_fd: int, _tree: PinnedTree) -> str | None:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != expected:
            return f"{what} {path.name!r} changed while it was being purged; fire again"
        return None

    outcome = remove_tree_pinned(
        str(path.resolve()), what=what, approve=_same_directory, refusal=MemberSlugError
    )
    if not outcome.removed:
        raise MemberSlugError(
            outcome.reason
            or f"{what} {path.name!r} could not be removed completely "
            f"({outcome.survivors} entries left); fire again"
        )


def _write_fired_record(archived: Path, text: str) -> None:
    """The path flow of the archive record (no ``dir_fd`` support): create the
    archive entry if the rename left none and publish ``fired.json`` by name.

    An existing leaf is NEVER opened. ``O_NOFOLLOW`` is 0 where this flow runs
    (Windows), so an open by name would follow a symlink planted at
    ``fired.json`` and write the record into whatever file the link names.
    Instead the record goes to a temp file beside the leaf and is published
    with ``os.replace``, which swaps the NAME: a planted link is replaced, not
    followed, and a hard link planted there is unlinked with its target
    untouched. What sits at the name is still looked at first with ``lstat``
    and anything but absence or a plain single-link file refused, so the
    path flow refuses exactly what the pinned flow refuses.
    """
    if platform_compat.is_link_or_junction(archived) or (
        archived.exists() and not archived.is_dir()
    ):
        raise MemberSlugError(
            f"archive entry {archived.name!r} is not a plain directory; refusing to write the record"
        )
    archived.mkdir(parents=True, exist_ok=True)
    leaf = archived / FIRED_RECORD_FILE
    try:
        st = os.lstat(leaf)
    except FileNotFoundError:
        pass
    else:
        if not stat_mod.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise MemberSlugError(
                f"{FIRED_RECORD_FILE} in the archive is not a plain single-link file; "
                "refusing to write the record over it"
            )
    fd, tmp_name = tempfile.mkstemp(dir=archived, prefix=".fired-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, leaf)
        fsync_dir(archived)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_fired_record_at(retired_fd: int, name: str, text: str) -> None:
    """Write ``fired.json`` into the archive entry *name* relative to the pinned
    archive root, without following a link planted at the entry's name after
    the rename (the archive root is itself agent-writable ground)."""
    arch_fd = _open_dir_nofollow(name, dir_fd=retired_fd)
    try:
        fd = os.open(
            FIRED_RECORD_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=arch_fd,
        )
        _write_record_fd(fd, text)
        fsync_dir_fd(arch_fd, "archive entry")
    finally:
        os.close(arch_fd)


def _write_record_fd(fd: int, text: str) -> None:
    """Write *text* over an opened record descriptor, truncating only AFTER the
    open target proves to be a plain, single-link regular file.

    ``O_TRUNC`` on the open would truncate whatever the name is by then --
    ``O_NOFOLLOW`` refuses a symlink, but a HARD link planted at ``fired.json``
    is the target file itself, and truncating it destroys another member's
    file. So: open without truncation, ``fstat`` the descriptor, refuse anything
    but a regular file with exactly one link, then truncate and write.
    """
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise MemberSlugError(
                f"{FIRED_RECORD_FILE} in the archive is not a plain single-link file; "
                "refusing to write the record over it"
            )
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            # The record is what a resume and the History fallback read; its
            # bytes are forced out here, its NAME by the caller's directory sync.
            os.fsync(fh.fileno())
    finally:
        if fd != -1:
            os.close(fd)


def remove_dm_binding_of(slug: str, member: str) -> bool:
    """Remove the slug's DM binding if it names *member* (or is unusable).

    Slugification is lossy: a binding under this slug may attribute the thread
    to another member of the same slug, and that one is not *member*'s to
    remove. A binding that does not read (malformed, tampered) is removed --
    it attributes nothing. Returns whether a file was removed.

    Only ABSENCE answers ``False`` quietly. An obstacle -- a link or a plain
    file at ``trust`` or ``member-bindings``, a directory or a device at the
    binding's name -- raises :class:`MemberSlugError`: the caller is a fire
    deciding whether its cleanup is done, and a fire that clears its marker
    over an obstacle leaves the next member on the slug unable to bind its
    thread (``write_dm_binding`` renames onto the obstacle and fails on every
    attempt). A LINK at the binding's name is removed as a link, its target
    untouched.
    """
    validate_slug(slug)
    if _DIR_FD_SUPPORTED:
        # Everything relative to the pinned ``trust/member-bindings`` directory
        # and by the LITERAL name ``<slug>.json``: a directory link planted at
        # either ancestor is refused (``_open_trust_child_pinned``), and a
        # file link planted at the binding's name is looked at with ``lstat``
        # and removed as the link it is -- never followed to the file it names.
        # ``dm_binding_path`` resolves, so its ``.name`` after a planted
        # ``<slug>.json -> <victim>.json`` would be the VICTIM's binding; the
        # name unlinked here is the slug's own, always.
        literal = f"{slug}.json"
        try:
            bindings_fd = _open_trust_child_pinned(DM_BINDINGS_DIR_NAME)
        except FileNotFoundError:
            return False
        try:
            try:
                st = os.lstat(literal, dir_fd=bindings_fd)
            except FileNotFoundError:
                return False
            if stat_mod.S_ISLNK(st.st_mode):
                # Not a binding -- a link somebody planted in a gateway-only
                # directory. It attributes nothing, so it goes; its target is
                # not touched.
                logger.warning(
                    "member %r: the DM binding at %s is a symlink; removing the link, not its target",
                    member,
                    literal,
                )
                os.unlink(literal, dir_fd=bindings_fd)
                return True
            if not stat_mod.S_ISREG(st.st_mode):
                raise MemberSlugError(
                    f"the DM binding of {slug!r} is not a regular file; remove the entry and "
                    "fire again"
                )
            # Read the literal file (O_NOFOLLOW, relative to the pinned
            # directory) to decide whose binding it is: slugification is lossy,
            # and another member's binding under this slug is not ours.
            current = _read_dm_binding_literal(bindings_fd, literal)
            if current is not None and current.get("member") != member:
                return False
            try:
                os.unlink(literal, dir_fd=bindings_fd)
            except FileNotFoundError:
                return False
            fsync_dir_fd(bindings_fd, "member bindings")
        finally:
            os.close(bindings_fd)
        return True
    # The path flow (Windows): the directory taken literally (``_trust_child``
    # raises on a link or a plain file at ``trust`` / ``member-bindings``), the
    # leaf ``lstat``-ed by its literal name -- never the resolved path.
    leaf = _trust_child(DM_BINDINGS_DIR_NAME) / f"{slug}.json"
    try:
        st = os.lstat(leaf)
    except FileNotFoundError:
        return False
    if stat_mod.S_ISLNK(st.st_mode):
        logger.warning(
            "member %r: the DM binding at %s is a symlink; removing the link, not its target",
            member,
            leaf.name,
        )
        leaf.unlink()
        return True
    if not stat_mod.S_ISREG(st.st_mode):
        raise MemberSlugError(
            f"the DM binding of {slug!r} is not a regular file; remove the entry and fire again"
        )
    current = read_dm_binding(slug)
    if current is not None and current.get("member") != member:
        return False
    leaf.unlink(missing_ok=True)
    return True


def _read_dm_binding_literal(dir_fd: int, name: str) -> dict | None:
    """The binding at *name* inside the pinned bindings directory, read without
    following a link at the name; ``None`` when absent, unreadable or malformed
    (the same totality as :func:`read_dm_binding`)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("member"), str):
        return None
    return data


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


def member_slot_key(slug: str, memory_store: str = "") -> str:
    """Derived, stable chat-slot key for a member's pinned DM thread.

    V1 uses the slug; a V2 opt-in uses its private store generation. Nothing is
    read or written. The dashboard's slot layer normalizes keys to a filename-safe
    charset, but a validated slug is already inside that charset, so the
    derived key survives ``_normalize_slot_key`` unchanged; callers must still
    use the slot layer's RETURNED key as the source of truth.
    """
    key = DM_SLOT_KEY_PREFIX + validate_slug(slug)
    if memory_store:
        from kiro_crew.memory_stores import validate_memory_store_name

        validate_memory_store_name(memory_store)
        if memory_store == "default":
            raise ValueError("Global memory has no private conversation generation")
        # The complete store name is already a unique, bounded generation ID.
        key += ".memory-" + memory_store
    return key


def member_thread_session_alias(slug: str, memory_store: str = "") -> str:
    """Canonical session-map alias for a member's pinned DM thread.

    ``dashboard:<slot key>`` — the spelling the session manager and the
    conversation log key a member thread under (see
    :func:`is_member_session_key` for the full set of prefixes a member key
    travels through). This is the ONE derivation every out-of-turn touch of a
    member session goes through — flagging the warm session for re-injection
    after a rules write, probing the thread's on-disk history — so the key
    format lives here rather than being hand-built at each site, where one
    divergent spelling would silently orphan the invariant it serves.
    """
    return f"dashboard:{member_slot_key(slug, memory_store)}"


class MemberLifecycle(str, Enum):
    """Session-lifecycle states a turn can arrive in, as the member-context
    chokepoint distinguishes them.

    Derived by :func:`member_lifecycle` from the same inputs
    ``build_message`` already branches on, so the two can never disagree on
    what state a turn is in:

    * ``FRESH`` — brand-new session; the full session context is built and
      injected.
    * ``SLIM_RESUME`` — the provider restored the native transcript
      (``session/load``); only a minimal header is injected, but the restored
      member section may be stale.
    * ``WARM_REINJECTION`` — follow-up turn whose session-start context was
      compacted away (or deliberately invalidated, e.g. by a rules write);
      the one-shot re-injection flag was consumed for this turn.
    * ``WARM`` — ordinary follow-up turn; the delivered section is still live
      in the provider conversation.
    * ``MINIMAL`` — minimal-context turn (cron); never a member thread by
      contract (``member`` is ``""`` on every such call).
    """

    FRESH = "fresh"
    SLIM_RESUME = "slim_resume"
    WARM_REINJECTION = "warm_reinjection"
    WARM = "warm"
    MINIMAL = "minimal"

    @property
    def delivers_section(self) -> bool:
        """Whether a member turn in this state injects the CURRENT section.

        The lifecycle half of the chokepoint's verdict, exposed on the enum so
        a caller that knows a turn is member-shaped without holding the member
        NAME (the chat runner records delivery-at-stake the moment the session
        client exists, before the context build resolves the name) reads the
        same single source of truth :func:`member_turn_context` does.
        """
        return self in (
            MemberLifecycle.FRESH,
            MemberLifecycle.SLIM_RESUME,
            MemberLifecycle.WARM_REINJECTION,
        )


@dataclass(frozen=True)
class MemberTurnContext:
    """What the member layer must do on ONE turn — the chokepoint's verdict.

    Exactly one of the two flags is set for a member turn (both are ``False``
    only when the turn carries no member, or on ``MINIMAL`` turns, which
    carry no member by contract):

    * ``deliver_section`` — inject the CURRENT four-layer member section this
      turn. Delivery itself enforces the rules gate: the section builder
      reads the user's [PERMANENT RULES] fresh, and an existing-but-unreadable
      rules file aborts the turn (fail closed) instead of running the member
      unbounded.
    * ``enforce_rules_gate`` — the section is already live in the provider
      conversation, so nothing is injected, but the fail-closed rules read
      still runs: a first-turn abort leaves a warm session, and without this
      per-turn check the member would keep running after its rules file went
      unreadable.
    """

    member: str
    lifecycle: MemberLifecycle
    deliver_section: bool
    enforce_rules_gate: bool


def member_lifecycle(
    *,
    is_new_session: bool,
    resumed: bool,
    minimal_context: bool,
    needs_reinjection: bool,
) -> MemberLifecycle:
    """Map ``build_message``'s branch inputs to one lifecycle state.

    Mirrors the branch structure of ``build_message`` exactly — including the
    precedence quirks a hand-written table would have to document:
    ``minimal_context`` beats ``resumed`` (a minimal resumed build early-returns
    before any member handling), and ``needs_reinjection`` only matters on warm
    turns (on a new session the full/slim injection path already delivers).
    """
    if is_new_session:
        if minimal_context:
            return MemberLifecycle.MINIMAL
        if resumed:
            return MemberLifecycle.SLIM_RESUME
        return MemberLifecycle.FRESH
    if needs_reinjection:
        return MemberLifecycle.WARM_REINJECTION
    return MemberLifecycle.WARM


def member_turn_context(member: str, lifecycle: MemberLifecycle) -> MemberTurnContext:
    """THE decision point for the rules-currency invariant.

    Invariant: **every member turn runs under the user's CURRENT rules.**
    Every lifecycle state satisfies it one of two ways — deliver the current
    section (which reads the rules, fail closed), or run the standalone
    fail-closed rules read against the section already live in the provider
    conversation. All delivery branches call this function instead of
    branching by hand, so a future lifecycle state added to ``build_message``
    cannot silently skip both the member section and the rules gate: it has
    to be given a verdict here first, where the mapping is pinned by tests.

    ``MINIMAL`` is the one deliberate exception — such turns are never member
    threads (``member`` is ``""`` by contract at every call site), and a
    member name arriving anyway gets neither delivery nor gate, exactly as
    the branch structure disposes of it (the minimal build early-returns
    before any member handling).

    An empty *member* means the turn has no member layer at all: nothing is
    delivered and nothing is gated, whatever the lifecycle.
    """
    # Deny by default. Both verdict predicates are identity/membership tests
    # that answer False for anything that is not a genuine enum member — and
    # the str mixin makes a bare "warm" compare EQUAL to the member while
    # failing both — so an unrecognized lifecycle would otherwise yield the
    # one combination the invariant forbids (no delivery, no gate) silently,
    # at the single decision point the invariant rests on. Refuse loudly
    # instead.
    if not isinstance(lifecycle, MemberLifecycle):
        raise TypeError(f"lifecycle must be MemberLifecycle, got {lifecycle!r}")
    if not member:
        return MemberTurnContext(
            member="", lifecycle=lifecycle, deliver_section=False, enforce_rules_gate=False
        )
    gate = lifecycle is MemberLifecycle.WARM
    return MemberTurnContext(
        member=member,
        lifecycle=lifecycle,
        deliver_section=lifecycle.delivers_section,
        enforce_rules_gate=gate,
    )


def read_dm_binding(slug: str) -> dict | None:
    """Return a member's DM-thread binding, or ``None`` when absent/unusable.

    Total by contract, like :func:`read_activity`: a bad slug, a missing file,
    an unreadable file, or a malformed payload all read as "not bound" — the
    binding is idempotently re-creatable, so degrading to re-creation is
    always safe and the caller needs no try/except.

    Blocking file IO: call via ``asyncio.to_thread`` from async code. The read
    goes through :func:`read_bytes_with_retry`, which retries a transient
    Windows sharing violation (an AV/indexer handle on the file this
    function's write-side twin, :func:`write_dm_binding`, just atomically
    replaced) — off-loop only, matching this function's own calling contract.
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
        raw = read_bytes_with_retry(path).decode("utf-8")
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
    generation = data.get("memory_store", "")
    if not isinstance(generation, str):
        return None
    try:
        canonical = member_slot_key(slug, generation)
    except ValueError:
        return None
    if data["slot_key"] != canonical:
        return None
    # And the member must actually BELONG to this slug: a tampered dm.json in
    # slug A's directory naming crew B (a real, registered crew whose slug
    # differs) would otherwise pin A's thread — and A's restored transcript —
    # to B's identity. Colliding names are fine: every name that slugifies to
    # this slug passes; anything else reads as absent.
    if slug_for_name(data["member"]) != slug:
        return None
    return data


def write_dm_binding(slug: str, *, member: str, slot_key: str, memory_store: str = "") -> dict:
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
    if slot_key != member_slot_key(slug, memory_store):
        raise ValueError(
            f"non-canonical dm binding slot_key {slot_key!r} for slug {slug!r} "
            f"(expected {member_slot_key(slug, memory_store)!r}); such a binding always reads back as absent"
        )
    binding = {
        "member": member,
        "slug": slug,
        "slot_key": slot_key,
        "created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if memory_store:
        binding["memory_store"] = memory_store
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


def read_dm_binding_for_slot(slot_key: str) -> dict | None:
    """Resolve a member slot without letting a newer generation adopt its history."""
    if not slot_key.startswith(DM_SLOT_KEY_PREFIX):
        return None
    slug = slot_key[len(DM_SLOT_KEY_PREFIX) :].split(".memory-", 1)[0]
    binding = read_dm_binding(slug)
    return binding if binding is not None and binding["slot_key"] == slot_key else None


def member_rules_path(slug: str) -> Path:
    """Absolute path to one member's permanent-rules file, containment-checked.

    Mirrors :func:`dm_binding_path`: keystone-gated ``trust/`` subtree, one
    flat ``<slug>.json`` per member, symlink containment re-checked behind
    ``validate_slug``. JSON rather than bare text because the payload records
    the EXACT member name (slugification is lossy, and a safety-rules file
    shared between two colliding crew names must be attributable — the same
    reason ``dm.json`` records the name). Does NOT create the directory;
    :func:`write_member_rules` does on demand.
    """
    validate_slug(slug)
    root = _trust_child(RULES_DIR_NAME)
    target = (root / f"{slug}.json").resolve()
    if target.parent != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


class MemberRulesUnreadable(RuntimeError):
    """A rules file EXISTS but cannot be read or parsed.

    Deliberately distinct from "absent": the rules layer is the user's safety
    boundary, so an unreadable file must NOT silently read as "never set" —
    injecting identity + protocol + briefing with the user's rules quietly
    missing would be indistinguishable from a member the user never bounded.
    Callers degrade the WHOLE member section (or answer 500), never just the
    rules layer.
    """


def read_member_rules(slug: str, member: str) -> str:
    """Return *member*'s permanent rules text, or ``""`` when never set.

    Name-scoped, like every read of a lossy-slug file: the payload's recorded
    ``member`` must equal the requested name, so a colliding crew name reads
    the shared file as "no rules for me" instead of inheriting another
    member's safety boundary.

    Missing file reads as ``""`` (the normal state). An EXISTING file that
    cannot be read or parsed raises :class:`MemberRulesUnreadable` — see its
    docstring for why that must not degrade to ``""``.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    path = member_rules_path(slug)
    try:
        # Same read-side twin as read_dm_binding: write_member_rules replaces
        # this file atomically, and on Windows an AV/indexer handle on the
        # just-replaced file is a transient sharing violation, not a corrupt
        # rules file -- so it must not surface as MemberRulesUnreadable.
        raw = read_bytes_with_retry(path).decode("utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise MemberRulesUnreadable(
            f"member rules for {slug!r} exist at {path} but cannot be read; "
            f"the member will not run until the file is repaired — rewrite or "
            f"clear the rules via PUT /api/members/{slug}/rules"
        ) from exc
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise MemberRulesUnreadable(
            f"member rules for {slug!r} at {path} are malformed (not valid "
            f"JSON); the member will not run until the file is repaired — "
            f"rewrite or clear the rules via PUT /api/members/{slug}/rules"
        ) from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("member"), str)
        or not isinstance(data.get("rules"), str)
    ):
        raise MemberRulesUnreadable(
            f"member rules for {slug!r} at {path} are malformed (unexpected "
            f"shape); the member will not run until the file is repaired — "
            f"rewrite or clear the rules via PUT /api/members/{slug}/rules"
        )
    if data["member"] != member:
        # A colliding slug's file holds another exact name's rules; for THIS
        # member that is "never set", not an error.
        return ""
    return data["rules"].strip()


def write_member_rules(slug: str, *, member: str, text: str) -> None:
    """Persist a member's permanent rules atomically (human write path only).

    Records the EXACT member name in the payload so the name-scoped read can
    attribute the file across lossy-slug collisions. Raises rather than
    degrading: the caller is the dashboard rules endpoint — a human action —
    and the human must know their rules did not land. ``ValueError`` on an
    over-cap payload (refused loudly, never truncated: silently dropping the
    tail of a rules document would drop rules), and ``OSError`` propagates
    like :func:`write_dm_binding`.

    An empty/whitespace *text* deletes the rules file: "no rules" is the
    documented absent state, so clearing the editor clears the state instead
    of leaving a zero-byte file that reads back differently from "never set".

    fsync, like the DM binding: rules are the user's safety boundary for this
    member, so they must not silently vanish to a crash after the dashboard
    confirmed the save.
    """
    path = member_rules_path(slug)
    if len(text) > MEMBER_RULES_MAX_CHARS:
        raise ValueError(
            f"member rules for {slug!r} exceed {MEMBER_RULES_MAX_CHARS} chars ({len(text)})"
        )
    # JSON permits escaped lone surrogates ("\ud800"), so request-parsed text
    # can hold code points UTF-8 cannot encode. Reject them HERE, before any
    # state changes: letting the write raise mid-flight (atomic_write encodes
    # to UTF-8) turns a bad payload into a 500, and escaping them into the
    # file (ensure_ascii=True) only defers the same crash to prompt-encode
    # time — inside the member's turn instead of at the save.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"member rules for {slug!r} contain characters UTF-8 cannot encode"
        ) from exc
    if not text.strip():
        try:
            path.unlink()
        except FileNotFoundError:
            return
        # The unlink changed a directory ENTRY, which atomic_write's file-level
        # fsync never covers: without syncing the parent, a power-off after the
        # 200 can bring the cleared rules back. best_effort: the clear is
        # already committed — see fsync_dir's contract for why a raise here
        # would report completed work as failed.
        fsync_dir(path.parent, best_effort=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same defence-in-depth mode tightening as the DM binding: the trust
    # subtree is owner-only everywhere else, and a chmod failure must not
    # cost the save (the sensitive-path floor is the real fence).
    for _dir in (path.parent, path.parent.parent):
        try:
            platform_compat.restrict_dir_to_owner(_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", _dir, exc_info=True)
    payload = {"member": member, "slug": slug, "rules": text}
    atomic_write(path, json.dumps(payload, ensure_ascii=False), fsync=True)
    # atomic_write's fsync=True forces the file DATA; the rename that
    # publishes it — and, on first save, the just-created member-rules
    # directory's own entry in ITS parent — live in directory metadata a
    # power-off can still lose, returning the dashboard's confirmed save to
    # the old (or no) rules. Sync both levels, raising like the write itself:
    # the caller is the rules PUT endpoint and the human must know their
    # safety boundary did not land.
    fsync_dir(path.parent)
    fsync_dir(path.parent.parent)


def member_briefing_path(slug: str) -> Path:
    """Absolute path to one member's self-maintained briefing file.

    Inside :func:`member_dir` — deliberately agent-writable (see
    ``BRIEFING_FILE_NAME``). Does NOT create the directory; the member's own
    file tools do when it first writes its briefing.
    """
    return member_dir(slug) / BRIEFING_FILE_NAME


def member_briefing_supported() -> bool:
    """Whether this platform can read a member briefing race-free (layer 4).

    Two requirements, both open-time controls (no check-then-open race):
    a truthy ``O_NOFOLLOW`` to refuse a symlink at the final name, and the
    descriptor-relative pinned walk (:func:`~kiro_crew.pinned_fs
    .supports_pinned_walk`) to refuse an ancestor swapped for a link. Where
    either is missing (Windows), briefing reads FAIL CLOSED and the section
    builder renders the layer as unavailable rather than inviting the member
    into a futile write-then-never-injected loop.
    """
    return bool(getattr(os, "O_NOFOLLOW", 0)) and supports_pinned_walk()


def read_member_briefing(slug: str) -> str:
    """Return a member's briefing text capped for injection, or ``""``.

    Total by contract: every failure reads as "no briefing yet", which is the
    normal state of a fresh member. Content past
    :data:`MEMBER_BRIEFING_MAX_CHARS` is cut at the cap with a visible marker,
    so the member SEES that its briefing overflowed (and can prune it) rather
    than silently losing the tail.

    The file is AGENT-WRITTEN, so two properties are enforced at the open,
    not after it:

    * **No symlink following anywhere on the path, no non-regular files, no
      blocking open.** The gateway reads this file with its own privileges
      while building context, so a briefing replaced by a symlink would pull
      any gateway-readable file — including the keystone-gated ``trust/``
      payloads the agent's tools cannot reach — into the prompt, and a
      briefing replaced by a FIFO would make a plain ``open`` block forever,
      hanging the member's turn and exhausting the embed workers. The leaf is
      opened through :func:`~kiro_crew.pinned_fs.open_in_pinned_parent`, which
      walks the ancestor chain one descriptor-relative ``openat`` at a time,
      each carrying ``O_NOFOLLOW`` — an ``O_NOFOLLOW`` on the final component
      alone is not enough, because the member's own directory
      (``members/<slug>/``) is agent-writable too, and swapping IT for a link
      redirects the whole traversal while the leaf open still finds an
      ordinary file (the same ancestor-swap shape the pinned walk exists to close).
      ``O_NONBLOCK`` makes a FIFO open return immediately instead of waiting
      for a writer (both at open time — no check-then-open race); ``fstat``
      then rejects anything that is not a regular file. Where the pinned walk
      or ``O_NOFOLLOW`` is unavailable (Windows —
      :func:`member_briefing_supported`) the read FAILS CLOSED to "no
      briefing": a check-then-open probe is exactly the TOCTOU an
      agent-writable path invites (swap a symlink in after the check), and
      the repo's posture on hosts lacking an OS-level control is to refuse,
      not to run the racy approximation.
    * **Bounded read.** At most ``(cap + 2) * 4`` bytes are read (4 = max
      UTF-8 bytes per char), so an arbitrarily large briefing costs a bounded
      allocation, never gateway memory.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    try:
        path = member_briefing_path(slug)
    except (MemberSlugError, OSError, RuntimeError):
        return ""
    byte_cap = (MEMBER_BRIEFING_MAX_CHARS + 2) * 4
    if not member_briefing_supported():
        # Fail closed (see the docstring): without O_NOFOLLOW and the pinned
        # ancestor walk there is no race-free way to refuse a symlink on an
        # agent-writable path.
        return ""
    try:
        # ``path.parent`` comes from :func:`member_dir`, which resolves and
        # containment-checks it — the "caller resolves once" contract of the
        # pinned walk. The walk then refuses any component swapped for a link
        # after that resolution, ``members/<slug>/`` included.
        fd = open_in_pinned_parent(
            str(path.parent),
            path.name,
            flags=os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            mode=0o600,
            what="member briefing",
        )
    except (PinnedPathRefusal, OSError):
        # Missing file/dir, a symlink refused anywhere on the pinned walk
        # (ancestor or leaf), or any unreadable state — all read as "no
        # briefing yet".
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            # A FIFO, device or socket is never a briefing; reading one can
            # block or misbehave, so it reads as "no briefing yet".
            return ""
        data = os.read(fd, byte_cap + 1)
    except OSError:
        return ""
    finally:
        os.close(fd)
    truncated_bytes = len(data) > byte_cap
    if truncated_bytes:
        # The cut can split a multi-byte character; the tail is being
        # truncated anyway, so drop the partial character rather than failing
        # the whole read over it.
        text = data[:byte_cap].decode("utf-8", errors="ignore").strip()
    else:
        try:
            text = data.decode("utf-8").strip()
        except UnicodeError:
            return ""
    if truncated_bytes or len(text) > MEMBER_BRIEFING_MAX_CHARS:
        return text[:MEMBER_BRIEFING_MAX_CHARS] + "\n[... briefing truncated at cap — prune it]"
    return text


FIRE_MARKERS_DIR_NAME = "member-fires"
"""Under ``data_home()``: one ``<member id>.json`` per fire that has started and
not finished (written before the row goes, removed last -- the dashboard's fire
handler owns the writes). Read here so a member-space writer can tell that the
space it is about to create belongs to a member being fired."""


def fire_pending_for_slug(slug: str) -> bool:
    """Whether a fire marker names a member on *slug*.

    A fire archives (or purges) ``members/<slug>`` and only then clears its
    marker; a writer that recreated the directory in between would leave live
    state for the next member hired onto the slug to inherit, with the fire
    reporting success over it. The markers are keyed by member id, which the
    slug is derived from, so one directory listing answers. Fails CLOSED on a
    directory that cannot be listed (a pending fire may be hidden behind the
    error); a missing directory means no fire has ever been recorded.
    """
    root = data_home().resolve() / FIRE_MARKERS_DIR_NAME
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            if slug_for_name(name[: -len(".json")]) == slug:
                return True
        except MemberSlugError:
            continue
    return False


def member_space_exists(slug: str) -> bool:
    """Whether ``members/<slug>`` is present -- ANY kind of entry at the literal
    name, a link included, looked at without following anything: the fire's
    last check before it clears its marker. A space that reappeared after the
    archive is live state the fire has not retired; a link planted at the name
    is an entry the next hire would write its state THROUGH, so it counts as
    present too (the resumed fire's retire unlinks it as a link).

    Never through :func:`member_dir`: that resolves the path, so a dangling
    link at ``members/<slug>`` resolved to its absent target and read as
    "gone" -- the marker cleared while the link stayed for a rehire to write
    through. On POSIX the entry is ``lstat``-ed relative to a descriptor of the
    members root opened as the literal ``members`` child of the data home with
    ``O_NOFOLLOW`` (a root that is not a plain directory reads as present:
    fail closed). The path flow (Windows) ``lstat``-s the literal, unresolved
    ``members/<slug>`` and refuses a root that is a link.
    """
    validate_slug(slug)
    if _DIR_FD_SUPPORTED:
        home = data_home().resolve()  # trust-rooted: the one path resolved by name
        try:
            home_fd = _open_dir_nofollow(home)
        except FileNotFoundError:
            return False
        try:
            try:
                root_fd = _open_dir_nofollow(MEMBERS_DIR_NAME, dir_fd=home_fd)
            except FileNotFoundError:
                return False
            except OSError:
                return True  # a link or a file at the root: not a retired state
        finally:
            os.close(home_fd)
        try:
            os.lstat(slug, dir_fd=root_fd)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        finally:
            os.close(root_fd)
        return True
    root = members_root()
    if platform_compat.is_link_or_junction(root):
        return True
    try:
        os.lstat(root / slug)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


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
        if not path.exists() and fire_pending_for_slug(slug):
            # The space is gone because a fire is retiring this member (its
            # marker is up). Recreating it here -- a routing decision landing
            # a beat after the archive -- would hand the next member hired onto
            # the slug a directory with a stranger's entry, and the fire would
            # clear its marker over it. Not recorded; the fire re-checks the
            # space before it finishes, so a write that slipped through is
            # caught there.
            logger.debug("member activity not recorded for %r: a fire is pending", member)
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

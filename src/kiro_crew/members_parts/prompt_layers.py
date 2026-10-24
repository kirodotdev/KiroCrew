"""The two file layers of a member's system prompt: its rules and its briefing.

The user-owned permanent rules under ``member-records/`` (path, read, the human write path) and
the member-owned briefing in its own directory (path, the race-free bounded read, the
injection cap with its visible marker).

:mod:`kiro_crew.members` stays the import path and the patch surface; see
:mod:`kiro_crew.members_parts`.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path

from kiro_crew import context_omissions, member_records, platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.members_parts.member_space import (
    BRIEFING_FILE_NAME,
    MEMBER_BRIEFING_MAX_CHARS,
    MEMBER_RULES_MAX_CHARS,
    RULES_DIR_NAME,
    MemberSlugError,
    _stable_member_slug,
    member_dir,
    members_root,
    validate_slug,
)
from kiro_crew.pinned_fs import PinnedPathRefusal, open_in_pinned_parent, supports_pinned_walk

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.members")


def member_rules_path(slug: str) -> Path:
    """Absolute path to one member's permanent-rules file, containment-checked.

    Mirrors :func:`dm_binding_path`: the gateway-only ``member-records/``
    leaf (:mod:`kiro_crew.member_records`), one flat ``<slug>.json`` per
    member, symlink containment re-checked behind ``validate_slug``. JSON
    rather than bare text because the payload records the EXACT member name
    (slugification is lossy, and a safety-rules file shared between two
    colliding crew names must be attributable — the same reason ``dm.json``
    records the name). Does NOT create the directory;
    :func:`write_member_rules` does on demand.
    """
    validate_slug(slug)
    root = member_records.records_dir(RULES_DIR_NAME).resolve()
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


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
MemberRulesUnreadable.__module__ = "kiro_crew.members"


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
    try:
        path = member_rules_path(slug)
    except member_records.RecordsNotMoved as exc:
        # The rules may still sit in the old layout: reading the new one would turn
        # them into "never set", which is exactly what this error exists to refuse.
        raise MemberRulesUnreadable(
            f"member rules for {slug!r} have not been moved to the records directory "
            f"yet; the member will not run until the move finishes ({exc})"
        ) from exc
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
    if data.get("member_id") != slug and data["member"] != member:
        # A colliding slug's file holds another exact name's rules; for THIS
        # member that is "never set", not an error.
        return ""
    return data["rules"].strip()


#: Every rules write in this process holds it, so a compare-and-write (a card
#: Undo clearing the rules it wrote) cannot interleave with an ordinary save.
_RULES_WRITE_LOCK = threading.Lock()


class MemberRulesChanged(RuntimeError):
    """The rules read differently from what the caller expected; nothing was written."""


MemberRulesChanged.__module__ = "kiro_crew.members"


def write_member_rules(
    slug: str, *, member: str, text: str, expected_sha256: str | None = None
) -> None:
    """Persist a member's permanent rules atomically (human write path only).

    With *expected_sha256*, write only while the current rules (as
    :func:`read_member_rules` returns them) still hash to it, compared under the
    same lock every rules write holds; otherwise raise :class:`MemberRulesChanged`.
    A card Undo uses it so rules another tab saved after its check are kept.

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
    import hashlib

    with _RULES_WRITE_LOCK:
        if expected_sha256 is not None:
            current = read_member_rules(slug, member)
            if hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_sha256:
                raise MemberRulesChanged(f"member rules for {slug!r} changed since the check")
        _write_member_rules(slug, member=member, text=text)


def _write_member_rules(slug: str, *, member: str, text: str) -> None:
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
    # Same defence-in-depth mode tightening as the DM binding: the records
    # leaf is owner-only, and a chmod failure must not cost the save (the
    # sandbox mask and the sensitive-path floor are the real fences).
    for _dir in (path.parent, path.parent.parent):
        try:
            platform_compat.restrict_dir_to_owner(_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", _dir, exc_info=True)
    payload = {
        "member": member,
        "member_id": slug if _stable_member_slug(slug, member) else "",
        "slug": slug,
        "rules": text,
    }
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
    ``BRIEFING_FILE_NAME``). Does NOT create the directory;
    ``member_notes.write_member_notes`` (``save_notes``) does on the first save.
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


def _briefing_pinned_target(slug: str) -> tuple[str, str]:
    """``(resolved members root / slug, BRIEFING_FILE_NAME)`` for the pinned open.

    The ROOT is resolved once (the pinned walk's "caller resolves once"
    contract) and the member's own directory component is appended LEXICALLY,
    never resolved: ``member_dir`` resolves ``members/<slug>`` too, and a
    ``members/<slug>`` swapped for a symlink to a peer's directory would then
    be followed *before* the walk begins -- the walk pins whatever the link
    points at and reads the peer's briefing as this member's. Left lexical,
    that component is opened with ``O_NOFOLLOW`` like every other and a link
    there is refused, which is the property the agent-writable
    ``members/<slug>/`` directory needs.
    """
    validate_slug(slug)
    root = members_root().resolve()
    return str(root / slug), BRIEFING_FILE_NAME


MEMBER_BRIEFING_TRUNCATION_MARKER = "\n[... briefing truncated at cap — prune it]"


def read_member_briefing_bounded(slug: str) -> tuple[str, float | None, bool]:
    """The bounded, UNCAPPED briefing buffer, its mtime, and whether the read hit its bound.

    The read half of :func:`read_member_briefing`, for a caller that must
    transform the text BEFORE cutting it at :data:`MEMBER_BRIEFING_MAX_CHARS`
    -- the dashboard's briefing endpoint redacts credentials, and a redaction
    run over already-capped text cannot match a token the cap split in two:
    the plaintext prefix would cross the boundary unmatched. The buffer is
    still bounded by the read itself (``(cap + 2) * 4`` bytes; see
    :func:`read_member_briefing`), so a token that straddles THAT edge is
    possible too, which is why :func:`cap_member_briefing` can drop the split
    tail. The third value is ``True`` when the file ran past the READ bound
    (the buffer lacks the file's tail); whether the text also runs past the
    character cap is :func:`cap_member_briefing`'s call, made on the text it
    is given -- after any transform -- not on the raw length. The mtime is
    from the same open as the text and ``None`` whenever the text reads as no
    briefing. Blocking file IO.
    """
    try:
        parent, name = _briefing_pinned_target(slug)
    except (MemberSlugError, OSError, RuntimeError):
        return "", None, False
    byte_cap = (MEMBER_BRIEFING_MAX_CHARS + 2) * 4
    if not member_briefing_supported():
        # Fail closed (see the docstring): without O_NOFOLLOW and the pinned
        # ancestor walk there is no race-free way to refuse a symlink on an
        # agent-writable path.
        return "", None, False
    try:
        fd = open_in_pinned_parent(
            parent,
            name,
            flags=os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            mode=0o600,
            what="member briefing",
        )
    except (PinnedPathRefusal, OSError):
        # Missing file/dir, a symlink refused anywhere on the pinned walk
        # (``members/<slug>`` itself included) or the leaf, or any unreadable
        # state — all read as "no briefing yet".
        return "", None, False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            # A FIFO, device or socket is never a briefing; reading one can
            # block or misbehave, so it reads as "no briefing yet".
            return "", None, False
        data = os.read(fd, byte_cap + 1)
    except OSError:
        return "", None, False
    finally:
        os.close(fd)
    mtime = float(st.st_mtime)
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
            return "", None, False
    return text, mtime, truncated_bytes


def cap_member_briefing(
    text: str, read_bounded: bool, *, drop_split_tail: bool = False
) -> tuple[str, bool]:
    """Cut ``text`` at :data:`MEMBER_BRIEFING_MAX_CHARS` with the visible marker.

    Returns the text and whether it was cut. ``read_bounded`` is the third
    value of :func:`read_member_briefing_bounded`: when the read hit its byte
    bound the buffer lacks the file's tail, so the marker is owed even when
    what remains fits the cap. Otherwise the cut happens only when the text
    GIVEN runs past the cap -- measured here, on the text as it is now, so a
    caller that redacted the buffer first (the dashboard endpoint) is judged
    on the redacted length: a briefing that only overflowed before its
    placeholders shrank it is shown whole, with no marker and no word lost.

    With ``drop_split_tail`` the cut also removes the trailing run of
    non-whitespace characters, so the shown text never ends in the FIRST HALF
    of a word the cap split: every credential the redaction chain knows is
    such a run, and a token that straddles the cut (either the character cap
    or the bounded read's own edge) would otherwise cross the wire as an
    unmatched plaintext prefix. At most one word of the shown tail is lost to
    it; a briefing with no whitespace at all in its first cap's worth of
    characters reads as the marker alone, which is the fail-closed answer.
    The prompt path keeps the plain cut: the member reads its own file, and
    the marker is what tells it to prune.
    """
    text = text.strip()
    if not read_bounded and len(text) <= MEMBER_BRIEFING_MAX_CHARS:
        return text, False
    head = text[:MEMBER_BRIEFING_MAX_CHARS]
    if drop_split_tail:
        stripped = head.rstrip()
        cut = len(stripped)
        while cut > 0 and not stripped[cut - 1].isspace():
            cut -= 1
        head = stripped[:cut].rstrip()
    return head + MEMBER_BRIEFING_TRUNCATION_MARKER, True


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
      any gateway-readable file — including the ``trust/`` and
      ``member-records/`` payloads the agent's tools cannot reach — into the prompt, and a
      briefing replaced by a FIFO would make a plain ``open`` block forever,
      hanging the member's turn and exhausting the embed workers. The leaf is
      opened through :func:`~kiro_crew.pinned_fs.open_in_pinned_parent`, which
      walks the ancestor chain one descriptor-relative ``openat`` at a time,
      each carrying ``O_NOFOLLOW`` — an ``O_NOFOLLOW`` on the final component
      alone is not enough, because the member's own directory
      (``members/<slug>/``) is agent-writable too, and swapping IT for a link
      redirects the whole traversal while the leaf open still finds an
      ordinary file. That is why the walk starts from the resolved members
      ROOT with ``<slug>`` appended lexically (:func:`_briefing_pinned_target`)
      rather than from :func:`member_dir`, whose own ``resolve()`` would follow
      such a link before the walk could refuse it.
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
    text, _mtime, read_bounded = read_member_briefing_bounded(slug)
    capped, truncated = cap_member_briefing(text, read_bounded)
    if truncated:
        context_omissions.record(
            context_omissions.MEMBER_BRIEFING, "cap", budget_chars=MEMBER_BRIEFING_MAX_CHARS
        )
    return capped

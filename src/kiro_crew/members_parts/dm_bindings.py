"""The crew-to-thread binding of a member's DM thread, kept under ``member-records/``.

Its containment-checked path, the read and the atomic write, the slug a DM slot key
names, the resolution of a slot without letting a newer generation adopt its history,
and the crewmate identity a session or a request may claim through it.

:mod:`kiro_crew.members` stays the import path and the patch surface; see
:mod:`kiro_crew.members_parts`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import member_records, platform_compat
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.members_parts.member_sessions import is_member_session_key, member_slot_key
from kiro_crew.members_parts.member_space import (
    DM_BINDINGS_DIR_NAME,
    DM_SLOT_KEY_PREFIX,
    MEMBER_NAME_MAX_CHARS,
    MEMORY_STORE_SLOT_SUFFIX,
    MemberSlugError,
    _stable_member_slug,
    slug_for_name,
    validate_slug,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.members")


def dm_binding_path(slug: str) -> Path:
    """Absolute path to one member's DM-thread binding, containment-checked.

    Lives under the gateway-only ``member-records/`` leaf (see the note on
    ``DM_BINDINGS_DIR_NAME``): the OS sandbox masks it and agent file tools
    refuse it; the gateway opens it directly. One flat ``<slug>.json`` per
    member — the slug is already validated to a safe charset, so the filename
    cannot traverse. Does NOT create the directory; :func:`write_dm_binding`
    does on demand.
    """
    validate_slug(slug)
    root = member_records.records_dir(DM_BINDINGS_DIR_NAME).resolve()
    target = (root / f"{slug}.json").resolve()
    # Defence in depth behind validate_slug, mirroring member_dir: a symlinked
    # component must not land the binding outside its records directory.
    if target.parent != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


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
    if data.get("member_id") == slug:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.execution_context import member_config_for_id

        try:
            alias, _ = member_config_for_id(KiroCrewConfig.load(), slug)
        except ValueError:
            return None
        data["member"] = alias
    elif slug_for_name(data["member"]) != slug:
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
        "member_id": slug if _stable_member_slug(slug, member) else "",
        "member": member,
        "slug": slug,
        "slot_key": slot_key,
        "created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if memory_store:
        binding["memory_store"] = memory_store
    path.parent.mkdir(parents=True, exist_ok=True)
    # The records leaf is owner-only (the sandbox precreates it 0o700); a
    # parents=True mkdir would otherwise leave a default-mode directory chain.
    # Best-effort: the sandbox mask and the sensitive-path floor are the real fences, the mode is
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


def slug_from_dm_slot_key(slot_key: str) -> str | None:
    """The member slug a DM slot key names, or ``None`` if it names no member.

    A V2 member opts into a memory store, and ``dm_slot_key`` then appends
    ``.memory-<store>`` to the key. That suffix is part of the SLOT's identity,
    never of the slug, so every reader of a slot key has to drop it -- and a
    reader that forgets sees a slug carrying a ``.``, which ``validate_slug``
    rejects. One spelling here so a third reader cannot drift from the other
    two.
    """
    if not slot_key.startswith(DM_SLOT_KEY_PREFIX):
        return None
    return slot_key[len(DM_SLOT_KEY_PREFIX) :].split(MEMORY_STORE_SLOT_SUFFIX, 1)[0]


def read_dm_binding_for_slot(slot_key: str) -> dict | None:
    """Resolve a member slot without letting a newer generation adopt its history."""
    slug = slug_from_dm_slot_key(slot_key)
    if slug is None:
        return None
    binding = read_dm_binding(slug)
    return binding if binding is not None and binding["slot_key"] == slot_key else None


def member_identity_for_session(session_key: str | None) -> dict[str, str] | None:
    """``{"slug", "name"}`` of the crewmate whose pinned DM session this is, else ``None``.

    Accepts every spelling :func:`is_member_session_key` does. The name comes
    from the slot's live ``dm.json`` binding, never from the key: slugification
    is lossy, and a key whose binding is gone or belongs to another generation
    names no crewmate. Total, and blocking file IO like :func:`read_dm_binding`.
    """
    if not session_key or not is_member_session_key(session_key):
        return None
    key = session_key
    for prefix in ("dashboard_", "dashboard:"):
        if key.startswith(prefix):
            key = key[len(prefix) :]
            break
    binding = read_dm_binding_for_slot(key)
    slug = slug_from_dm_slot_key(key)
    if binding is None or not slug:
        return None
    name = binding["member"]
    # The payload refuses a member past this bound, and a refused payload drops
    # the whole note; an unattributable note must still be delivered.
    if len(name) > MEMBER_NAME_MAX_CHARS:
        return None
    return {"slug": slug, "name": name}


def attributable_session_key(request: Any) -> str:
    """The request's ``X-Session-Key`` when it may name a note's crewmate, else ``""``.

    Over TCP loopback the header alone is the caller's word, so an app token
    holding a send grant could declare a crewmate's DM key and put that
    crewmate's face on its own note. The key counts only once
    :func:`kiro_crew.member_memory_auth.session_key_is_attested` binds it to
    the caller (the Unix-socket peer attestation, or a signed per-session
    token that verifies to it), and never for an app caller
    (``request["app"]``), whose notes are the app's own. Blocking file IO on
    the token branch; callers on an event loop must offload it.
    """
    from kiro_crew.member_memory_auth import session_key_is_attested

    if request.get("app"):
        return ""
    key = request.headers.get("X-Session-Key", "")
    if not isinstance(key, str) or not key:
        return ""
    return key if session_key_is_attested(request, key) else ""

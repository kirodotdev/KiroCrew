"""The write gate for dashboard packages (``kind="dashboard"``).

What create and update consult before a package write: the binding refusals (a crewmate
or a session at the other end of ``bound_to``), the promote verdict and authoritative
content, the package write refusal, and the classification of the stored snapshot.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import hooks
from kiro_crew.artifact_source import LINK, classify_source
from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    ArtifactNotFoundError,
    ArtifactValidationError,
    get_default_store,
)
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import _redact_text
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import _run_off_loop, logger
from kiro_crew.dashboard.state import _normalize_slot_key


async def _authoritative_promote_content(path: str, root: str) -> tuple[str | None, str | None]:
    """Read the bytes to persist for a promotion from the SOURCE, server-side.

    The dashboard obtains its preview through ``/api/file-read``, which redacts
    credential-shaped spans and can truncate. Persisting that response would
    bake the placeholders into the artifact permanently -- the promoted copy
    would differ from the file it claims to be, silently and irreversibly, and
    for a copy there is no source pointer left to recover the real bytes from.

    So the server re-reads the file it just validated and uses those bytes.
    ``allow_truncate=False``: a file too large to store whole is REFUSED rather
    than promoted partial, matching the client-side truncation guard.

    Returns ``(content, None)`` on success or ``(None, message)`` on refusal.
    """

    def _read() -> tuple[str | None, str | None]:
        try:
            # NEVER within_root=None. A COPY (temp dir, Downloads) has no
            # project root, and passing None skips the descriptor containment
            # and sensitivity checks -- an ancestor swapped between validation
            # and open could then redirect the read into a secrets directory and
            # bake its contents into the artifact. With no project root, the
            # file's own directory is the boundary: it still pins the opened
            # descriptor to a known tree and keeps the sensitivity check on.
            data = hooks.safe_read_file_bytes_nolink(
                path,
                within_root=root or os.path.dirname(path),
                max_bytes=MAX_CONTENT_BYTES,
                allow_truncate=False,
            )
        except (ValueError, OSError, hooks.FileTooLargeError) as exc:
            # FileTooLargeError is NOT an OSError subclass, so without naming it
            # here a source over the cap escaped as an unhandled exception and
            # surfaced as a 500 instead of a refusal the caller can show.
            return None, f"cannot read source file: {exc}"
        if data is None:
            return None, "source file is unreadable, too large, or not permitted"
        try:
            return data.decode("utf-8"), None
        except UnicodeDecodeError:
            return None, "source file is not valid UTF-8 text"

    return await _run_off_loop(_read)


async def _promote_verdict(
    request: web.Request, raw_source_path: str
) -> tuple[str, str, bool, str | None]:
    """Decide whether a save with ``source_path`` links to the file or copies it.

    Returns ``(source_path, source_root, copy_only, error)``. A non-None *error*
    means the request named a file this endpoint will not accept, and the caller
    must refuse it rather than fall back to the client's body.

    ``source_path`` is kept on BOTH verdicts, because it is the only key
    :meth:`ArtifactStore.find_by_source_path` dedups on — dropping it on a copy
    made a second promotion of the same disposable file mint a duplicate
    artifact. Liveness is carried by ``source_copy_only`` instead: a copy
    records the path as provenance only, and the store never reads or writes
    through it, so the dead-pointer failure this guards against cannot occur.

    The verdict is derived from the path alone. Nothing the caller (or the agent)
    can write nominates an authorizing root: a LINK is granted only by an
    observable git repository root, which is re-verified on every read.
    """
    if not isinstance(raw_source_path, str) or not raw_source_path:
        return "", "", False, None

    # VALIDATE BEFORE PROBING. An existence probe on the raw path is itself an
    # information leak: a copy verdict retains source_path, so the response
    # distinguishes "this sensitive file exists" from "it does not". Run the
    # sensitive-path gate first and probe only its canonical result, so a
    # rejected path is indistinguishable from a missing one.
    def _resolve() -> str:
        try:
            canonical = hooks.validate_file_path(raw_source_path)
        except (ValueError, OSError):
            # An embedded NUL byte makes realpath raise ValueError; a hostile or
            # simply malformed path must be refused, never a 500.
            return ""
        if not canonical or not os.path.isabs(canonical):
            return ""
        # Keep a provenance path only for a file that actually exists: a path
        # that never resolved is useless as dedup identity and actively
        # misleading as provenance, so it is dropped rather than recorded.
        return canonical if os.path.isfile(canonical) else ""

    canonical_path = await _run_off_loop(_resolve)
    if not canonical_path:
        return "", "", False, None
    verdict, root = await _run_off_loop(lambda: classify_source(canonical_path))
    if verdict == LINK:
        # A LINK already names a re-verifiable project root, which IS the
        # deliberate widening this feature exists for (project files outside
        # $HOME). Its reads are confined to that root.
        return canonical_path, root, False, None

    # COPY has no project root, so a retained path would leave the server-side
    # promotion read confined only to the file's OWN directory -- a tautology.
    # That would make `POST /api/artifacts {"source_path": "/etc/passwd"}` an
    # arbitrary-local-file read whose bytes land in the library and come back in
    # the 201 body, bypassing the fixed-root barrier every other store read
    # honours. So a copy keeps its provenance pointer ONLY inside the store's
    # allowed roots (home, data home, publish.relocate_roots); outside them the
    # path is dropped and the save proceeds as ordinary content with no pointer.
    def _within_allowed() -> bool:
        p = Path(canonical_path)
        for r in get_default_store().allowed_source_roots():
            # Comparison kept INLINE (not factored into a helper): CodeQL's taint
            # tracker only recognises the containment guard in this shape.
            if p == r or p.is_relative_to(r):
                return True
        return False

    if not await _run_off_loop(_within_allowed):
        # REFUSE, rather than dropping the pointer and keeping the client's body.
        # The dashboard's copy of the file came from /api/file-read, which redacts
        # credential-shaped spans -- so falling back to it would persist
        # placeholders AS the artifact's authoritative content, and for a copy
        # there is no pointer left to ever recover the real bytes. Since the
        # server will not read this path, the only honest answer is to decline.
        return (
            "",
            "",
            False,
            (
                "source_path is outside the locations this instance may read "
                "(home, data home, or a configured relocate root)"
            ),
        )
    return canonical_path, "", True, None


# ── Dashboard package bindings ──────────────────────────────────────────────
#
# ``bound_to`` says WHOSE dashboard a ``kind="dashboard"`` package is. The store's
# validator (``artifact_store.dashboard_package.validate_bound_to``) checks its SHAPE
# and can check nothing else: it runs inside :class:`ArtifactStore`, a library the CLI
# and cron use with no roster loaded and no gateway running. So "is there anything at
# the other end of this binding" is asked HERE, on the one write path both doors reach
# -- the browser's PATCH and MCP's ``artifact_save`` / ``artifact_update`` all arrive at
# :func:`api_artifacts_create` / :func:`api_artifact_update`.
#
# ASKED ONLY WHEN THE WRITE CHANGES THE BINDING, and that single rule is the whole
# answer for a target that dies later. A session closes, a crewmate is renamed, and the
# package left behind stays exactly as servable as it was: the serve path refuses it
# (``member_dashboard._resolve``'s ``member_not_found`` / ``dashboard_slug_ambiguous``,
# ``api_session_dashboard``'s ``not_root_session``), and that is where the refusal
# belongs, because the fact expires and a stored record cannot be re-asked. What a
# write-time check CAN do is catch the author at the one moment the binding is still a
# typo rather than history. So an edit carrying the binding it already had is never
# re-litigated: a package whose target is gone can still be re-themed, re-versioned and
# REBOUND to a live target, and no record written before this check can be stranded by
# it.
#
# Neither half replaces the other, and the eligibility questions are the serve path's
# own: ``_member_names_for_slug`` for a crewmate, ``is_root_session`` for a session. A
# binding is accepted here exactly when the serve path would serve it.


def _package_binding(content: Any) -> str | None:
    """The ``bound_to`` a dashboard write would store, or ``None`` when it names none.

    A non-string ``bound_to`` reads as none: its shape is the store validator's
    question, and answering it here would give the caller two different 400s for one
    malformed value.

    Asked of EVERY update carrying content, before the record's kind is known, so it
    must answer "not a binding" for arbitrary text rather than raise.

    Decoded through the package module's OWN ``_decode_package`` rather than a
    ``json.loads`` here, which is what makes that true. That function is the half of
    ``parse_package`` that runs before any validator, and it already carries the three
    guards a decoder of submitted text needs: the surrogate scan, the byte cap, and a
    ``RecursionError`` branch for the case the byte cap does not cover (depth and size
    come apart -- a quarter-megabyte of ``[`` is small and still exhausts the stack).
    A second decoder spelled here would have to re-earn all three, and the first
    revision of this function proved it: it caught only ``ValueError``, so content that
    was valid JSON nested past the interpreter's limit turned a perfectly good
    opaque-text write into a 500.

    Every refusal it raises reads as "no binding here", which is the right answer for
    all of them: arbitrary text, a package over the cap, and one carrying a lone
    surrogate are each content the store refuses on its own terms a moment later.
    """
    from kiro_crew.artifact_store.dashboard_package import _decode_package

    try:
        parsed = _decode_package(content)
    except ArtifactValidationError:
        return None
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("bound_to")
    return raw if isinstance(raw, str) else None


def _refuse_binding(reason: str) -> str:
    """One refusal sentence, with the caller's own text redacted.

    EVERY binding refusal is built through here, which is the point of it being a
    function rather than a wrap at the two returns. A refusal names the target the
    caller submitted, so that it can say which binding was wrong -- and an agent can
    put anything in a `bound_to` slug, including a credential-shaped string. Redacting
    where the sentence is BUILT means no route, and no scope branch added later, can
    return one that was never redacted, and it is the same text ``_audit`` records
    rather than a second copy that happens to agree.

    An ordinary target survives this unchanged: ``redact_credentials`` rewrites
    credential-shaped spans, not identifiers, so a slug (``someone-else``) and a slot
    key (``slack_1785370133.085469``) read back verbatim and the author can still see
    their typo. Only an `AKIA...`-shaped target becomes ``[REDACTED: credential]``.
    """
    return _redact_text(reason)


def _crewmate_binding_refusal(cfg: Any, slug: str) -> str | None:
    """Why no crewmate can resolve to *slug*, or ``None`` when EXACTLY ONE does.

    Takes the config the caller loaded, so the roster this answers from is the same one
    a sibling check in the same request read.

    EXACTLY one, which is the same count ``member_dashboard._resolve`` requires of this
    same helper (``!= [member]``). Slugification is lossy, so a hand-edited roster
    holding both ``Atlas`` and ``atlas`` derives one slug from two names: accepting that
    binding because SOMETHING matched would store a page every read then refuses with
    409 ``dashboard_slug_ambiguous``. Ambiguity refuses and never picks -- one of the
    two crewmates would otherwise be handed the other's page -- and the remedy is a
    person renaming one of them, which is what the serve-time refusal already says.

    The RAW slug does the lookup and only the message is redacted: redacting first
    would send ``[REDACTED: credential]`` to the roster and answer a different question
    than the caller asked.
    """
    from kiro_crew.dashboard.handlers.members import _member_names_for_slug

    owners = _member_names_for_slug(cfg, slug)
    if not owners:
        return _refuse_binding(
            f"bound_to 'crewmate:{slug}' names no crew member, so nothing can ever "
            "render this package: a crewmate's dashboard is resolved from the roster "
            "by that slug. Create the crewmate first, or bind to an existing one"
        )
    if len(owners) > 1:
        return _refuse_binding(
            f"bound_to 'crewmate:{slug}' is ambiguous: {len(owners)} crew members "
            "resolve to that slug, so this page would belong to none of them and every "
            "read of it is refused 'dashboard_slug_ambiguous'. Ask the human to rename "
            "one of them"
        )
    return None


def _session_binding_refusal(state: Any, key: str) -> str | None:
    """Why no session can be served *key*'s package, or ``None`` when one can.

    ``is_root_session`` is the test :func:`api_session_dashboard` applies on every
    read, and it is asked here for that reason rather than re-derived: a dispatched or
    adopted session has no dashboard at all, by the product rule that a worker's
    records belong to the board of whoever dispatched it.
    """
    from kiro_crew.dashboard.card_lifecycle import is_root_session

    slot = state.get_slot(_normalize_slot_key(key))
    if slot is None:
        return _refuse_binding(
            f"bound_to 'session:{key}' names no live session, so nothing can render "
            "this package: a session's dashboard reads the folds of that session's own "
            "slot. Bind to a session this gateway knows"
        )
    if not is_root_session(slot):
        return _refuse_binding(
            f"bound_to 'session:{key}' is a dispatched or adopted session, which has "
            "no dashboard of its own -- its records belong to the board of whoever "
            "dispatched it, and this page would be refused 'not_root_session' on every "
            "read. Bind to the root session instead"
        )
    return None


async def _binding_refusal(state: Any, bound_to: str) -> str | None:
    """Why *bound_to* can never be served, or ``None`` when it can.

    Takes a binding the store validator has NOT seen yet, so an unknown scope reads as
    "not this function's question" rather than as an accepted one: the shape check
    behind it admits exactly two scopes and refuses everything else, and a scope this
    rejected would be answered twice.

    The roster read is offloaded and the slot read is not, matching the two serve-time
    checks this mirrors: ``member_dashboard._resolve`` loads the config in a thread,
    ``api_session_dashboard`` asks the live state inline.
    """
    scope, _, target = bound_to.partition(":")
    if scope == "crewmate":
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        return _crewmate_binding_refusal(cfg, target)
    if scope == "session":
        return _session_binding_refusal(state, target)
    return None


def _package_write_refusal(request: web.Request, content: Any, stored: Any = None) -> str:
    """The dashboard binding gate, imported HERE rather than at module scope.

    ``dashboard.server`` imports this module at boot, and the controller behind this
    gate reaches the template manifest and the projection registry. Importing it up
    there would execute that graph before the socket binds, for every gateway, whether
    or not a package dashboard is ever opened -- which is what the boot-path rule
    forbids and what every other optional dashboard subsystem defers.
    """
    from kiro_crew.dashboard.handlers.member_dashboard_fields import package_write_refusal

    return package_write_refusal(request, content, stored)


def _is_dashboard_package(*texts: Any) -> bool:
    """True when any of *texts* is a dashboard package.

    The same classifier the gate uses, deferred for the same boot-path reason. Asked
    here to decide whether a write needs the stale-write check below, so that a
    judgement the gate made on a snapshot cannot be committed against a later one.

    THE STORED SIDE IS CLASSIFIED THE WAY THE GATE CLASSIFIES IT -- off the envelope,
    not by parsing. The two have to agree: the gate enforces a stored binding whatever
    the layout does, so classifying the same record as "not a package" HERE authorizes
    that write and then forwards no token to fence it. A legacy package whose field name
    the current validator rejects is exactly that record, and its former owner's
    kind-only PATCH would commit against a rebind that landed in between. Ownership and
    the fence are halves of one answer and are read the same way.
    """
    from kiro_crew.dashboard.handlers.member_dashboard_fields import (
        dashboard_package_of,
        stored_package_binding,
    )

    return any(
        dashboard_package_of(text) is not None or stored_package_binding(text) is not None
        for text in texts
    )


#: A stored record was read: its content and token are the snapshot to authorize.
_SNAPSHOT_FOUND = "found"
#: The slug holds nothing. A FACT, not a failure -- there is no binding to authorize.
_SNAPSHOT_ABSENT = "absent"
#: The slug may hold anything; this read could not say. Never authorized against.
_SNAPSHOT_UNREADABLE = "unreadable"


def _gate_must_classify(slug: str, submitted: Any) -> bool:
    """True when the dashboard binding gate is the rule that decides this write.

    Asked only when the stored snapshot could not be READ, to decide whether that
    failure costs the write. Two ways the gate owns it: the content being submitted is
    a package, or the record already is one. The second is answered from the library
    LISTING, which is metadata and so still readable when the record's own bytes are
    not -- the kind is the one thing needed and it is not in those bytes.

    Fails closed if even the listing cannot be read: a record that may be a crewmate's
    dashboard is not written over on the strength of a failed lookup.
    """
    from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND

    if _is_dashboard_package(submitted):
        return True
    try:
        return any(
            str(getattr(record, "slug", "")) == slug
            for record in get_default_store().list(kind=DASHBOARD_KIND)
        )
    except Exception:
        logger.warning("artifacts: the dashboard listing could not be read for %r", slug)
        return True


def _stored_snapshot_for(slug: str) -> tuple[str | None, str | None, str]:
    """What *slug* holds now, the token that says so, and whether the read could tell.

    For the binding gate, which is why the three outcomes are kept apart instead of
    collapsing into "no content". "Nothing stored" and "I could not read what is
    stored" authorize very differently: the first has no binding to check, while the
    second is a record that may be somebody's dashboard, and treating it as empty
    checks only the binding the CALLER submitted and carries no token forward -- so a
    transient read error becomes a worker's write over an owner's page.

    The token travels with the content because the gate authorizes a SNAPSHOT: it reads
    the stored binding here and the write lands later, so without carrying the token
    forward an owner's rebind in between would be overwritten by a write authorized
    against the binding it replaced. Blocking file IO, so callers run it off the event
    loop.

    """
    try:
        # ``assign_token=True`` because the token is the POINT of this read: a record
        # that has never been written through the guarded path holds none, and without
        # one minted here the write's compare-and-set would have nothing to compare and
        # the write would silently fall back to last-write-wins.
        art = get_default_store().get(slug, assign_token=True)
    except ArtifactNotFoundError:
        return None, None, _SNAPSHOT_ABSENT
    except Exception:
        logger.warning("artifacts: %r could not be read for the dashboard binding gate", slug)
        return None, None, _SNAPSHOT_UNREADABLE
    return art.content, getattr(art, "content_token", None) or None, _SNAPSHOT_FOUND

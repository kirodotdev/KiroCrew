"""Session export — download one session as a single file.

``GET /api/chat/slots/{slot}/export`` streams the bundle ``session_transfer``
already builds for the Instances tunnel, plus that module's ``source``
provenance record, gzipped as ``<title-slug>-<stamp>.kcsession.json.gz``.

**Why a file hop at all.** The tunnel is a request/response between two live
gateways, so it needs both machines up at the same moment, reachable from one
account, with a working tunnel between them. A laptop that is asleep can receive
nothing, and two machines that never see each other have no path at all. A file
needs none of that: it can sit in a download, a bucket or on a USB stick until
somebody opens it.

**Two renderings, one bundle.** ``?format=md`` answers
``<title-slug>-<stamp>.kcsession.md`` instead — the same assembled bundle written
as a human-readable Markdown transcript by
:mod:`kiro_crew.dashboard.session_markdown`, for reading, sharing and archiving
rather than for installing. Every guard in the handler is shared, because the
format decides only how the bundle is written out; the Markdown rendering never
carries Layer B, whatever the operator has opted into.

**It adds no WIRE format, and it writes no file server-side.** The transfer
document is the tunnel's own bundle at ``bundle_version`` 2 — no version bump,
because
``session_transfer._validate_bundle`` refuses an unrecognised version outright
while dropping unknown keys silently, so bumping would break sending to an
instance that has not updated while a new optional key costs it nothing. The
bytes are streamed to the caller and nothing is stored here, so a repeat click
costs the source nothing and needs no confirm step.

Pending session state MAY be flushed, though, so this is not a pure read of the
disk. Like the tunnel's send, it flushes a dirty slot first
(``build_transfer_bundle_async`` calls ``save_slot_off_loop`` with
``best_effort=False``), because the alternative is serialising a stale
transcript: an in-place edit below the persisted boundary would otherwise ship
the superseded turn. So an export CAN persist pending session state, and it fails
rather than exporting when that write fails. What it never does is change the
conversation -- a flush writes what is already in memory.

**Layer B travels only on an explicit operator opt-in, and is withheld by
default.** The bundle CAN carry *Layer B* -- the kiro-cli context window --
byte-exact and unredacted, so a session installed from the file RESUMES through
``session/load`` rather than replaying its transcript as a lossy prefix. Byte-exact
is forced rather than chosen: the thinking-block signatures inside Layer B are
validated when the conversation is replayed, so redacting it and transplanting it
cannot both hold -- there is no redacted variant. Because an export can be shared
with another person, unredacted context must not ride along unless the operator
asks for it: the RFC (``rfc-s3-backup.md`` O1) assigns that risk to the operator,
not the exporter, and its minimum bar for putting a sensitive payload into a
bundle is conjunctive (``rfc-s3-backup.md``:317-319) -- a separate config key OFF
BY DEFAULT and an explicit per-invocation flag. So Layer B travels only when the
caller is the dashboard operator, ``dashboard.export_include_layer_b`` is enabled
(standing permission, default false), AND the request carries
``?include_layer_b=true`` (this export asked). Every non-operator export and every
export by an operator who never chose stays Layer A only, sets
``layer_b_skipped``, and tells the reader the copy resumes from its transcript.
Layer B is also withheld, with the same flag, for the cases the builder gate
already handles: a mid-turn snapshot whose context would lag the visible
transcript. A session that never opened a kiro-cli context sets neither key,
because there is no context to lose.

**Nothing here installs.** Reading such a file back is a separate, later piece of
work; this module only produces one.

**Separate module, deliberately.** ``session_transfer`` may not answer 404 or
405: ``SshTunnelManager.send_session_bundle`` reads those two codes from a peer
as "that instance has no importer, tell the user to update it", and
``test_session_transfer.test_importer_never_answers_404_or_405`` pins the premise
by scanning that module's source. This endpoint needs a real 404 for an unknown
slot, so it lives here instead of weakening the guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, BinaryIO

from aiohttp import hdrs, web

from kiro_crew.config.loader import _raw_config
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.session_markdown import (
    MARKDOWN_FILE_SUFFIX,
    write_markdown_file,
)
from kiro_crew.dashboard.session_transfer import (
    _CHUNK_BYTES,
    SnapshotUnstable,
    TranscriptBusy,
    TranscriptWithheld,
    _rm_import_temps,
    build_transfer_bundle_async,
    release_bundle_files,
    write_bundle_file,
)
from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Extension of an exported session file. Double-barrelled on purpose: the outer
#: ``.gz`` tells a browser and an operator it is compressed, and the
#: ``.kcsession.json`` inside says what decompressing it yields, so a file that
#: outlived the tab it came from is still identifiable by name alone.
EXPORT_FILE_SUFFIX = ".kcsession.json.gz"

#: Cap on the title slug in an export filename. A session title runs to 500
#: characters, which would produce a name some filesystems refuse; the slug is a
#: human hint and not an identifier, so truncating it loses nothing.
_MAX_SLUG_CHARS = 60

#: Fallback slug for a session whose redacted title has no usable characters —
#: an untitled tab, or a title that was entirely non-ASCII or entirely redacted.
_DEFAULT_SLUG = "session"

#: The two renderings this endpoint can answer with. ``json`` is the default and
#: the transfer document; a caller that names nothing, or names something
#: unrecognised, gets it. ``md`` is the ONLY spelling that selects Markdown — it
#: is the extension a person types, and one spelling means there is no second
#: branch for a caller to exercise unnoticed. These two names are the values the
#: handler and the audit entry carry, not the query strings it accepts.
#:
#: A query parameter rather than a second route: the two formats are the SAME
#: assembled bundle rendered twice, and every guard in this handler — the
#: app-scope ownership checks, the restricted-session refusal, the transcript
#: locking, the publication hold — has to hold identically for both. A second
#: route is a second place for one of them to be forgotten.
_FORMAT_MARKDOWN = "markdown"
_FORMAT_JSON = "json"


def _export_format(request: web.Request) -> str:
    """Which rendering this request asked for: :data:`_FORMAT_JSON` or Markdown.

    ``?format=md`` selects Markdown after surrounding whitespace is stripped and
    ASCII case is normalised; everything else — absent, empty, or unrecognised —
    selects JSON, the document every existing caller already receives.

    An unrecognised value falls back rather than erroring. The format is a
    presentation choice and both renderings pass the same guards, but Markdown
    is always egress-redacted and excludes Layer B while JSON may include
    unredacted Layer B after the operator's explicit opt-in, so a typo costs the
    caller a re-click without weakening either format's privacy contract.
    """
    raw = request.query.get("format", "").strip().lower()
    return _FORMAT_MARKDOWN if raw == "md" else _FORMAT_JSON


def export_stamp() -> str:
    """A compact UTC stamp for an export filename, e.g. ``20260909T083244Z``.

    Colon-free and separator-free because it lands in a filename on whatever
    filesystem the browser saves to, and ``:`` is illegal on Windows.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify_title(title: str) -> str:
    """Turn an ALREADY-REDACTED session title into a filename-safe slug.

    **Caller contract: pass the redacted title, never the raw one.** A filename
    is read by whoever receives the file and listed by whatever holds it — a
    share, a bucket listing, a chat attachment — so it is an egress surface in
    its own right, not just decoration on one. ``session_transfer`` redacts the
    title as it assembles the bundle, which is why the handler below slugifies
    ``bundle["title"]`` and never ``slot.title``.

    Script-preserving: a CJK, Cyrillic or accented title keeps its characters, so
    the name hint survives for the readers most likely to need it. ``\\w`` under
    Unicode is what admits those letters while still dropping every path
    separator, control character and shell metacharacter, and the result is
    percent-encoded into the header by :func:`export_filename`.
    """
    slug = re.sub(r"[^\w.-]+", "-", title.lower(), flags=re.UNICODE)
    slug = slug.strip("-.")[:_MAX_SLUG_CHARS].strip("-.")
    return slug or _DEFAULT_SLUG


def export_filename(redacted_title: str, stamp: str = "", suffix: str = EXPORT_FILE_SUFFIX) -> str:
    """``<title-slug>-<stamp><suffix>`` for an already-redacted title.

    The slug leads, so a directory of exports sorts by conversation; the drive
    key in a later phase reverses that for a different reason (a listing wants
    newest first), which is why the two are built separately rather than shared.

    *suffix* selects the format's extension — :data:`EXPORT_FILE_SUFFIX` for the
    JSON bundle, ``session_markdown.MARKDOWN_FILE_SUFFIX`` for the Markdown
    rendering. Both keep the ``.kcsession`` middle so the two sort together in a
    directory of exports from the same conversation.
    """
    return f"{slugify_title(redacted_title)}-{stamp or export_stamp()}{suffix}"


def content_disposition(filename: str) -> str:
    """The ``Content-Disposition`` value for *filename*.

    ``filename*=UTF-8''`` with the name percent-encoded, which is the spelling
    every other download handler in the dashboard already ships
    (``handlers/files.py``, ``handlers/diagnostics.py``, ``handlers/wakatime.py``).
    Reusing it rather than reducing the name to ASCII is what lets a
    non-Latin-titled session keep its name hint.

    ``quote(..., safe="")`` encodes EVERYTHING outside the unreserved set, which
    is also what makes the header injection-proof: a title cannot contribute a
    quote, a semicolon, a CR or an LF, because none of them survive encoding.
    """
    return f"attachment; filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"


def _stage_export(bundle: dict[str, Any]) -> Path:
    """The export body, built compressed in a temp file. **Blocking.**

    Offloaded by the caller: a bundle can carry a whole long session, and both
    the JSON encode and the deflate over that much text are far too much CPU to
    hold the event loop with. Neither the document nor its compressed form is
    ever resident: the file is what :class:`_StagedExport` sends.
    """
    return write_bundle_file(bundle, compress=True)


#: Releases still running after their send, or the refused commit that never
#: handed the file to one, was cancelled. A shielded task needs a strong
#: reference or it can be collected before it finishes.
_PENDING_RELEASES: set[asyncio.Task[None]] = set()


async def _shielded_release(release: Awaitable[None]) -> None:
    """Await a staged file's release so a cancellation cannot withdraw it.

    Cancelling an ``asyncio.to_thread`` that no worker has started withdraws the
    job, so an unshielded release is skipped whenever the cancellation wins the
    race for a worker. The task is held in :data:`_PENDING_RELEASES` until done.
    """
    task = asyncio.ensure_future(release)
    _PENDING_RELEASES.add(task)
    task.add_done_callback(_PENDING_RELEASES.discard)
    await asyncio.shield(task)


def _open_staged(path: Path) -> tuple[BinaryIO, int]:
    """Open a staged export and read its size. **Blocking.**"""
    fobj = path.open("rb")
    try:
        return fobj, os.fstat(fobj.fileno()).st_size
    except BaseException:
        fobj.close()
        raise


def _close_and_remove(fobj: BinaryIO | None, path: Path) -> None:
    """Close the send's handle, then remove its file. **Blocking.**

    The close comes first because Windows refuses to delete a file while any
    handle on it is open.
    """
    if fobj is not None:
        with contextlib.suppress(OSError):
            fobj.close()
    _rm_import_temps(path)


async def _release_staged(opening: asyncio.Future[tuple[BinaryIO, int]], path: Path) -> None:
    """Remove a staged export once the open it may still be waiting on settles."""
    fobj: BinaryIO | None = None
    with contextlib.suppress(Exception):
        fobj, _size = await opening
    await asyncio.to_thread(_close_and_remove, fobj, path)


class _StagedExport(web.StreamResponse):
    """An export served from its staged file, which is removed once sent.

    The file is streamed to the socket a chunk at a time, so a session of any
    size leaves the gateway without its compressed body ever being held in
    memory. It is removed when the send ends, whether it completed or the
    client went away; a response that is never sent leaves its file under the
    egress staging directory.

    The response owns its file handle and closes it before the removal, since
    a handle still open makes the delete fail on Windows. The removal is
    shielded: a client that goes away cancels the send, and cancelling an
    ``asyncio.to_thread`` that no worker has started withdraws the job.
    """

    def __init__(self, path: Path, *, headers: dict[str, str]) -> None:
        super().__init__(headers=headers)
        self._path = path

    async def prepare(self, request: web.BaseRequest) -> Any:
        loop = asyncio.get_running_loop()
        opening = loop.run_in_executor(None, _open_staged, self._path)
        try:
            fobj, size = await asyncio.shield(opening)
            self.content_length = size
            writer = await super().prepare(request)
            if request.method != hdrs.METH_HEAD:
                while chunk := await loop.run_in_executor(None, fobj.read, _CHUNK_BYTES):
                    await self.write(chunk)
            await self.write_eof()
            return writer
        finally:
            await _shielded_release(_release_staged(opening, self._path))


#: Whether the operator has granted STANDING PERMISSION for the file export to
#: carry Layer B -- the byte-exact, unredacted model context window. This is one
#: of the TWO operator opt-ins; see :func:`_export_layer_b_requested` for the other.
#: A verified dashboard-operator request is also required. The RFC's minimum bar for putting a sensitive payload into a
#: bundle is conjunctive (rfc-s3-backup.md:317-319): a separate config key OFF BY
#: DEFAULT, and an explicit per-invocation flag. Only a dashboard-operator request
#: can carry Layer B, and only when BOTH opt-ins hold, so the operator opts in once
#: at the config layer and again per export; every other export stays Layer A only.
#: rfc O1
#: assigns this to the operator ("the operator's risk decision, not the
#: implementer's"), and a default-on would ship the implementer's decision to
#: everyone who never chooses -- the opposite of what O1 assigns -- so the default
#: is OFF.
#:
#: A backend config key rather than a Settings row on purpose: a key needs no
#: i18n, whereas a UI row would need ``en.json`` plus nine locales and the
#: ``app-manifest-sync``-class catalog gate. It is read through the same
#: ``_raw_config`` route ``dashboard.max_background_turns`` uses, and an
#: unreadable or non-boolean value falls back to WITHHOLDING (the safe default)
#: rather than failing an export.
def _export_layer_b_permitted() -> bool:
    """Whether the operator has enabled the standing permission to carry Layer B.
    Default False; enable by setting ``config.json -> dashboard.export_include_layer_b``
    to ``true``. This alone does not carry Layer B -- the request must also ask for
    it (see :func:`_export_layer_b_requested`)."""
    try:
        raw = (_raw_config().get("dashboard") or {}).get("export_include_layer_b", False)
    except Exception:
        logger.debug(
            "export_include_layer_b config unavailable; withholding Layer B", exc_info=True
        )
        return False
    return raw if isinstance(raw, bool) else False


#: The explicit per-invocation half of the RFC bar (rfc-s3-backup.md:317-319). A
#: standing config permission is not enough on its own: THIS export must also ask
#: to carry Layer B, via ``?include_layer_b=true`` (or ``1``/``yes``/``on``) on the
#: request. Absent, malformed, or any other value reads as "did not ask", so the
#: export withholds. Keeping the two conditions separate is deliberate -- the RFC's
#: bar is a conjunctive list and must not collapse to one lever.
def _export_layer_b_requested(request: web.Request) -> bool:
    """Whether THIS export invocation explicitly asked to carry Layer B via the
    ``include_layer_b`` query flag. Default False (absent == did not ask)."""
    raw = request.query.get("include_layer_b", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def api_chat_slot_export(request: web.Request) -> web.StreamResponse:
    """GET /api/chat/slots/{slot}/export — download one session as a file.

    ``?format=md`` renders the Markdown document instead of the gzipped JSON
    bundle (see :mod:`kiro_crew.dashboard.session_markdown`). Every guard below is
    shared by both formats, deliberately: the format decides only how the
    assembled bundle is written out.
    """
    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    caller = request_app or "dashboard"
    slot_key = request.match_info.get("slot", "")
    export_format = _export_format(request)

    def _audit(outcome: str, resources: str = "", error: str = "") -> None:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_export",
            outcome=outcome,
            source="dashboard",
            # The format is on every line, including the refusals: an operator
            # auditing what left the host needs to know WHICH document was
            # produced, because only one of the two can resume a session.
            resources=resources or f"slot={slot_key},format={export_format}",
            error=error,
        )

    slot = state._slots.get(slot_key)
    if slot is None:
        _audit("denied", error="slot not found")
        if request_app:
            # The per-slot checkpoint's body: an app gets one 404 on this route.
            return slot_not_found()
        return web.json_response(
            {"error": "session not found", "code": "export_slot_not_found"}, status=404
        )
    # App-scope ownership, mirroring chat_fork and the tunnel's send. An app token
    # sets request["user"] so it clears the dashboard guard, and an app whose
    # manifest declares this surface reaches here — without this check it could
    # name ANOTHER slot's key and download that transcript, which is an
    # exfiltration path straight out of the app sandbox. 404 rather than 403: a
    # slot owned by another app has to be indistinguishable from one that does not
    # exist, or the status code itself enumerates slots across the isolation
    # boundary (CWE-204). The real reason is recorded server-side instead. The
    # body is the per-slot checkpoint's, which has already refused such a caller
    # before this handler runs; this catches the handler mounted outside the chain.
    denied = deny_app_slot_access(request_app, slot, slot_key, "chat.slot_export")
    if denied is not None:
        return denied
    # Owning the SLOT is not owning the TRANSCRIPT. A channel-linked slot displays
    # a conversation that lives on the channel's own session, and
    # ``get_or_create_slot`` auto-binds that link from a channel-shaped NAME --
    # which the creating caller supplies. So an app could hold a slot it legitimately
    # owns whose transcript is a channel's, and the ownership check above would pass
    # it. The app boundary refuses instead of reasoning about the binding: fail
    # closed, because the cost of being wrong is a foreign conversation leaving the
    # sandbox. The dashboard owner is unaffected -- they are entitled to both.
    #
    # The checkpoint's 404, for the same reason: a distinguishable body would let
    # an app learn which of its slots carry a channel link.
    if request_app and getattr(slot, "linked_session_key", ""):
        _audit("denied", error=f"app {request_app!r} may not export a channel-linked slot")
        return slot_not_found()

    def _refuse_restricted(reason: str) -> web.Response:
        # An incognito or temporary transcript is kept for the user's own History
        # and nothing is produced FROM it -- no lesson, no summary, no snapshot.
        # A bundle written into a file the user then stores somewhere is such a
        # product, so this is a refusal rather than a best-effort export of
        # whatever happens to be resident.
        _audit("denied", error=reason)
        return web.json_response(
            {
                "error": "cannot export an incognito or temporary session",
                "code": "export_slot_not_persistent",
            },
            status=400,
        )

    if slot.memory_mode != "persistent":
        return _refuse_restricted(f"memory_mode={slot.memory_mode}")

    try:
        bundle = await build_transfer_bundle_async(
            state,
            slot,
            # A downloaded file can be shared with anyone, so it must not stamp
            # this host's identity. ``local_instance_label()`` is the machine's
            # hostname, which on a Linux dev host can embed the operator's login
            # and in any case names a machine the
            # recipient cannot act on. The tunnel path keeps the real label
            # (it reaches the operator's own trusted peer instance); the file
            # path carries none, so on import the "(from ...)" suffix is simply
            # absent rather than disclosing where the file came from.
            origin="",
            with_source=True,
            # Layer B -- the byte-exact, unredacted model context window -- rides
            # along ONLY when all three conditions hold: the caller is the
            # dashboard operator, standing config permission
            # (``dashboard.export_include_layer_b``) is ON, and this request asks
            # for it (``?include_layer_b=true``). The latter two are the operator's
            # twofold opt-in and the RFC's conjunctive minimum bar for a sensitive
            # payload in a bundle (rfc-s3-backup.md:317-319). Layer B cannot be
            # redacted -- the thinking-block signatures inside it are validated on
            # replay, so redacting and transplanting cannot both hold, and there is
            # no redacted variant -- so it ships byte-exact or not at all. Carrying
            # it lets an installed file resume through ``session/load`` rather than
            # replaying a lossy prefix; that full-fidelity resume is why an operator
            # would ask for it.
            #
            # The default is WITHHOLD for everyone who never chooses, because
            # whether unredacted context leaves in a downloaded file is the
            # operator's risk decision, not the exporter's (rfc-s3-backup.md O1),
            # and a default-on would ship the implementer's decision to everyone
            # who never chose -- the opposite of what O1 assigns. A withheld export
            # degrades to Layer A and sets ``layer_b_skipped`` so the lost resume
            # fidelity is stated, not inferred from an absent key. The builder seam
            # ``include_layer_b`` also withholds on its own for a mid-turn snapshot,
            # a v1 sender, or a session that never opened a context; this caller
            # only supplies the operator's opt-in on top of that.
            include_layer_b=(
                export_format == _FORMAT_JSON
                and _export_layer_b_permitted()
                and _export_layer_b_requested(request)
                and is_owner_dashboard_request(request)
            ),
        )
    except TranscriptBusy:
        # The seam could not take the transcript lock in time: a holder (this
        # session's own save, a cron append, a second gateway) outlasted the
        # acquire ceiling. Nothing about the session is wrong and nothing was
        # written, so this is the same retryable answer as an unstable snapshot.
        _audit("failure", error="transcript busy")
        return web.json_response(
            {
                "error": "the session is still being saved -- try again in a moment",
                "code": "export_snapshot_unstable",
            },
            status=503,
        )
    except TranscriptWithheld as exc:
        # The bundle is built from the transcript on DISK, and the file's own
        # privacy contract gates it, not only the live slot's mode above: another
        # writer (a second gateway on this data home, a same-key hand-over, a
        # subagent or cron appending) may have tightened the line while this slot
        # still reads persistent in memory. The builder checks the line before
        # and after its read and raises; nothing was built or written.
        return _refuse_restricted(f"on-disk line: {exc}")
    except SnapshotUnstable:
        # No consistent view of the source: a flush landed inside every retry, or
        # a rewind/regenerate rewrite is still owed so disk is stale. Retryable,
        # and the session is untouched, so failing costs the user nothing but a
        # second click. The message names what the user can act on -- waiting --
        # rather than the snapshot mechanism behind it.
        _audit("failure", error="snapshot unstable")
        return web.json_response(
            {
                "error": "the session is still being saved -- try again in a moment",
                "code": "export_snapshot_unstable",
            },
            status=503,
        )
    except Exception:
        # Anything else — an unreadable transcript, a disk error inside the
        # threaded read. Caught so the SEL trail stays complete: every other exit
        # from this handler records an outcome, and an unhandled exception would
        # leave the one case an operator most wants to find as the only export
        # with no audit line at all. Nothing was written, so this is safe to
        # answer and safe to retry.
        logger.warning(
            "session_export: could not build the bundle for slot=%s", slot_key, exc_info=True
        )
        _audit("error", error="bundle assembly failed")
        return web.json_response(
            {"error": "the session could not be exported", "code": "export_failed"},
            status=500,
        )

    if not bundle.get("messages"):
        # Refused here rather than handed over, because the importer's floor
        # requires a non-empty ``messages`` array: an empty file would download
        # cleanly and then be rejected wherever it was taken, which is a worse
        # answer than saying so now. Only measurable after the build — the
        # visible transcript lives on disk, not in the resident window.
        _audit("denied", error="no visible messages")
        release_bundle_files(bundle)
        return web.json_response(
            {"error": "this session has no messages to export", "code": "export_bundle_empty"},
            status=400,
        )

    # Serialised to a temp file a message at a time, with a Layer B log streamed
    # from its snapshot, and sent from that file, so neither the document nor
    # its compressed form is ever resident. The Markdown writer streams the same
    # way, for the same reason.
    markdown = export_format == _FORMAT_MARKDOWN
    try:
        staged = await asyncio.to_thread(write_markdown_file if markdown else _stage_export, bundle)
    except Exception:
        # A full staging volume, most often. The same audited, coded failure as
        # a build that could not complete; nothing reached the client.
        logger.warning(
            "session_export: could not serialise the bundle for slot=%s", slot_key, exc_info=True
        )
        _audit("error", error="bundle serialisation failed")
        return web.json_response(
            {"error": "the session could not be exported", "code": "export_failed"},
            status=500,
        )
    finally:
        release_bundle_files(bundle)
    filename = export_filename(
        bundle.get("title") or "",
        suffix=MARKDOWN_FILE_SUFFIX if markdown else EXPORT_FILE_SUFFIX,
    )

    publication_key = slot_history_key(slot)

    def _commit_response() -> tuple[web.StreamResponse, int]:
        size = staged.stat().st_size
        headers = {
            # ``charset`` is spelled out for the Markdown document and absent for
            # the gzip: one is text whose encoding a reader must be told, the
            # other is an opaque stream. Both are still served ``nosniff`` below.
            "Content-Type": "text/markdown; charset=utf-8" if markdown else "application/gzip",
            "Content-Disposition": content_disposition(filename),
            # The body is a session's own text coming back out of the gateway on
            # the dashboard's own origin. Without this a browser is free to sniff
            # it and render it as something executable instead of saving it. It
            # matters MORE for the Markdown format, not less: that body is text
            # the session itself produced, so a sniffing browser could be talked
            # into treating a transcript as HTML on this origin.
            "X-Content-Type-Options": "nosniff",
        }
        log = state.conversation_log
        if log is None:
            return _StagedExport(staged, headers=headers), size
        expected_keys = getattr(bundle, "publication_keys", (publication_key,))
        with log.publication_hold(publication_key, expected_keys=expected_keys):
            return _StagedExport(staged, headers=headers), size

    # Response construction is synchronous, so the publication lock covers the
    # commit without crossing an await. The socket write happens after the handler
    # returns and is the unavoidable residual transmit window. Until the response
    # owns the staged file, every exit here removes it, shielded like the send's.
    handed_off = False
    try:
        response, size = await asyncio.to_thread(_commit_response)
        handed_off = True
    except TranscriptBusy:
        _audit("failure", error="transcript busy at response commit")
        return web.json_response(
            {
                "error": "the session is still being saved -- try again in a moment",
                "code": "export_snapshot_unstable",
            },
            status=503,
        )
    except TranscriptWithheld as exc:
        return _refuse_restricted(f"on-disk line at response commit: {exc}")
    finally:
        if not handed_off:
            await _shielded_release(asyncio.to_thread(_rm_import_temps, staged))
    # Recorded only once the commit has taken the response: an ``allowed`` written
    # before the revalidation above would name a byte count that was never
    # transmitted whenever the line tightened during the build, and sit right
    # next to the ``denied`` that says so.
    _audit(
        "allowed",
        resources=(
            f"slot={slot_key},format={export_format},messages={len(bundle['messages'])},"
            f"bytes={size},layer_b={'yes' if bundle.get('layer_b') else 'no'}"
        ),
    )
    return response

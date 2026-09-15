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

**It adds no format, and it writes no file server-side.** The document is the
tunnel's own bundle at ``bundle_version`` 2 — no version bump, because
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

**Layer A only.** The tunnel's bundle can carry *Layer B* -- the kiro-cli context
window -- byte-exact and unredacted, and that is forced rather than chosen: the
thinking-block signatures inside it are validated when the conversation is
replayed, so redacting it and transplanting it cannot both hold. What makes
byte-exact acceptable is therefore the DESTINATION: a send goes to the operator's
own authenticated peer, which stores it 0600. A file has no destination -- the
whole point of it is that it can sit in a download, a bucket or on a USB stick --
so that justification does not carry over and an export ships the transcript
alone, with ``layer_b_skipped`` set so the loss is stated rather than inferred.
The cost is real and accepted: a session installed from a file resumes from its
transcript rather than through ``session/load``.

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
import gzip
import json
import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.session_transfer import (
    SnapshotUnstable,
    build_transfer_bundle_async,
    bundle_rejection_reason,
    local_instance_label,
)
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


def export_filename(redacted_title: str, stamp: str = "") -> str:
    """``<title-slug>-<stamp>.kcsession.json.gz`` for an already-redacted title.

    The slug leads, so a directory of exports sorts by conversation; the drive
    key in a later phase reverses that for a different reason (a listing wants
    newest first), which is why the two are built separately rather than shared.
    """
    return f"{slugify_title(redacted_title)}-{stamp or export_stamp()}{EXPORT_FILE_SUFFIX}"


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


def gzip_bundle(bundle: dict[str, Any]) -> bytes:
    """Serialise *bundle* as gzipped JSON. **Blocking CPU, thread-safe.**

    Offloaded by the caller: a bundle runs to ``session_transfer``'s 20M-char
    content cap, and both the JSON encode and the deflate over that much text are
    far too much CPU to hold the event loop with — the same starvation that stops
    the liveness heartbeat and lets the watchdog exit the gateway.

    ``ensure_ascii`` is left at its DEFAULT, and that is a correctness choice
    rather than a stylistic one. A transcript can legitimately contain a LONE
    SURROGATE: ``_validate_bundle`` accepts any ``str`` content, and
    ``json.loads('"\\ud800"')`` yields exactly that, so an imported conversation
    can persist one. With ``ensure_ascii=False`` the following ``.encode("utf-8")``
    raises ``UnicodeEncodeError`` on it and the export answers 500 for a session
    the user can otherwise read. ASCII escaping represents the same character as
    ``\\ud800`` and round-trips through ``json.loads`` unchanged. The size cost is
    paid back by the gzip immediately below.

    ``mtime=0`` because the export instant is already inside the document as
    ``source.exported_at``. A second copy of it in the gzip header would add
    nothing and would make two otherwise-identical exports differ in their bytes.
    """
    raw = json.dumps(bundle, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, mtime=0)


async def api_chat_slot_export(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/export — download one session as a file."""
    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    caller = request_app or "dashboard"
    slot_key = request.match_info.get("slot", "")

    def _audit(outcome: str, resources: str = "", error: str = "") -> None:
        sel().log_api_access(
            caller=caller,
            operation="chat.slot_export",
            outcome=outcome,
            source="dashboard",
            resources=resources or f"slot={slot_key}",
            error=error,
        )

    slot = state._slots.get(slot_key)
    if slot is None:
        _audit("denied", error="slot not found")
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
    # boundary (CWE-204). The real reason is recorded server-side instead.
    if request_app and (not getattr(slot, "_app", "") or slot._app != request_app):
        _audit("denied", error=f"app {request_app!r} does not own this slot")
        return web.json_response(
            {"error": "session not found", "code": "export_slot_not_found"}, status=404
        )
    # Owning the SLOT is not owning the TRANSCRIPT. A channel-linked slot displays
    # a conversation that lives on the channel's own session, and
    # ``get_or_create_slot`` auto-binds that link from a channel-shaped NAME --
    # which the creating caller supplies. So an app could hold a slot it legitimately
    # owns whose transcript is a channel's, and the ownership check above would pass
    # it. The app boundary refuses instead of reasoning about the binding: fail
    # closed, because the cost of being wrong is a foreign conversation leaving the
    # sandbox. The dashboard owner is unaffected -- they are entitled to both.
    #
    # Same 404 as above, for the same reason: a distinguishable code would let an
    # app learn which of its slots carry a channel link.
    if request_app and getattr(slot, "linked_session_key", ""):
        _audit("denied", error=f"app {request_app!r} may not export a channel-linked slot")
        return web.json_response(
            {"error": "session not found", "code": "export_slot_not_found"}, status=404
        )
    if slot.memory_mode != "persistent":
        # An incognito or temporary transcript exists under a promise that nothing
        # is kept. Writing one into a file the user then stores somewhere is the
        # precise opposite of the mode they chose, so this is a refusal rather
        # than a best-effort export of whatever happens to be resident.
        _audit("denied", error=f"memory_mode={slot.memory_mode}")
        return web.json_response(
            {
                "error": "cannot export an incognito or temporary session",
                "code": "export_slot_not_persistent",
            },
            status=400,
        )

    try:
        bundle = await build_transfer_bundle_async(
            state,
            slot,
            origin=local_instance_label(),
            with_source=True,
            # Layer A ONLY. Layer B -- the model's context window -- travels
            # byte-exact and unredacted over a tunnel, and what makes that
            # acceptable is the destination: the operator's own authenticated
            # peer, stored 0600, never outside their trust boundary. A file has no
            # destination at all; the whole point of it is that it can sit in a
            # download, a bucket or on a USB stick. So the justification does not
            # carry over and the context stays behind. The bundle says so via
            # ``layer_b_skipped``, and the cost is honest: a session installed
            # from a file resumes from its transcript rather than through
            # ``session/load``.
            include_layer_b=False,
        )
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
        return web.json_response(
            {"error": "this session has no messages to export", "code": "export_bundle_empty"},
            status=400,
        )

    # A file must not be handed over unless this instance's OWN importer would
    # accept it. The bounds are the importer's (5 000 messages, 1 MB per message,
    # 20 MB of content total) and they are consulted through the validator itself
    # rather than restated here, so the two cannot drift apart.
    #
    # Without this, a session past those bounds exports 200 and is then refused
    # wherever it is taken: a file that looked complete, cost a download, and can
    # never be installed. Refusing at the producer puts the failure next to the
    # only party who can act on it.
    reason, importer_code = bundle_rejection_reason(bundle)
    if reason:
        # The importer's own code goes in the AUDIT, not the response body. Nothing
        # reads it off the wire, and the response already says which bound was hit
        # in prose -- a machine-readable third copy of the same fact is a field this
        # code invents and no caller consumes.
        _audit("denied", error=f"the importer would reject this bundle: {importer_code}")
        return web.json_response(
            {
                "error": f"this session is too large to export: {reason}",
                "code": "export_bundle_rejected",
            },
            status=400,
        )

    body = await asyncio.to_thread(gzip_bundle, bundle)
    filename = export_filename(bundle.get("title") or "")
    _audit(
        "allowed",
        resources=(
            f"slot={slot_key},messages={len(bundle['messages'])},"
            f"bytes={len(body)},layer_b={'yes' if bundle.get('layer_b') else 'no'}"
        ),
    )
    return web.Response(
        body=body,
        content_type="application/gzip",
        headers={
            "Content-Disposition": content_disposition(filename),
            "Content-Length": str(len(body)),
            # The body is a session's own text coming back out of the gateway on
            # the dashboard's own origin. Without this a browser is free to sniff
            # it and render it as something executable instead of saving it.
            "X-Content-Type-Options": "nosniff",
        },
    )

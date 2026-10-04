"""``GET /api/file-office-preview``: extracted Office text, blocks and slides, redacted before they are capped."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _BLOCK_STRUCTURAL_KEYS,
        _MAX_UPLOAD_BYTES,
        _OFFICE_PREVIEW_CAP,
        _OFFICE_PREVIEWABLE_EXT,
        FILE_READ_SCHEMA,
        ValidationError,
        _open_checked_file,
        _OpenDenied,
        _PathProbeBusy,
        _probe_busy_response,
        _resolve_project_relative,
        _run_path_probe,
        _sel,
        extract_blocks,
        extract_slides,
        extract_text,
        join_slides,
        logger,
        redact,
        require_owner_dashboard_request,
        validate_tool_args,
    )


def _redact_value(value: object) -> object:
    """Redact every string reachable from a block value, walking containers.

    The default is REDACT, not pass-through. Enumerating the keys that hold text
    means a block shape added later carries its text out unredacted until someone
    remembers to extend the list, and nothing fails while they have not: the miss
    is silent and its consequence is exposure. Inverting the default costs a
    no-op ``redact`` call on strings that never held a secret.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return _redact_block(value)
    return value


def _redact_block(block: object) -> object:
    """Redact one block, every string by default, with one carve-out.

    **A paragraph is redacted on its JOINED runs**, never run by run. Word splits
    a sentence at every formatting change, so a credential straddling a bold
    boundary arrives as two fragments that match nothing on their own, while text
    mode -- which redacts the whole joined extraction -- masks it. Joining first
    is what makes the two modes mask the same things, so choosing a format cannot
    weaken the control. Walking runs individually would re-open exactly that gap,
    which is why this case is spelled out rather than left to the generic walk.
    When redaction changes a paragraph its runs collapse into one: the redacted
    string carries no run boundaries to map back onto, and losing bold on a
    paragraph that contained a secret is much the cheaper loss.
    """
    if not isinstance(block, dict):
        return block
    if block.get("type") == "paragraph" and isinstance(block.get("runs"), list):
        runs = [r for r in block["runs"] if isinstance(r, dict)]
        joined = "".join(str(r.get("text", "")) for r in runs)
        cleaned = redact(joined)
        if cleaned == joined:
            return block
        return {
            "type": "paragraph",
            "runs": [{"text": cleaned, "bold": False, "italic": False}],
        }
    out: dict[str, object] = {}
    for key, value in block.items():
        if key in _BLOCK_STRUCTURAL_KEYS:
            out[key] = value
        else:
            out[key] = _redact_value(value)
    return out


def _redact_blocks(blocks: object) -> object:
    """Redact every block in a payload list. See :func:`_redact_block`."""
    if not isinstance(blocks, list):
        return blocks
    return [_redact_block(block) for block in blocks]


class _PreviewUnsupported(Exception):
    """The validated path's extension is outside :data:`_OFFICE_PREVIEWABLE_EXT`.

    Endpoint-local, mirroring :class:`_SheetRefusal`: ``_OpenDenied``'s codes
    are the SHARED file-serving boundary's vocabulary, and this is this
    endpoint's own FORMAT policy rather than a security refusal, so it does
    not belong in that enum. Raised from inside the worker callback so the
    checked file object is closed by its ``with`` block on the same thread.
    """


def _cap_slides(slides: list[tuple[int, str]], cap: int) -> list[dict[str, object]]:
    """Redact each slide's text and bound the slides' TOTAL text to *cap*.

    The same two rules the flat ``text`` field follows, applied per slide so
    the structured form never carries more than the flat one would: redact
    first (a credential must not be cut in half by the cap and slip past the
    redactor), then spend one budget across the deck in slide order -- a
    slide that does not fit is cut to the remaining budget and the slides
    after it are dropped. Slide numbers are the deck's own (``slideN.xml``),
    so a gap tells the reader a slide carried no text rather than that one
    went missing.
    """
    out: list[dict[str, object]] = []
    budget = cap
    for index, raw in slides:
        if budget <= 0:
            break
        text = redact(raw)
        if len(text) > budget:
            text = text[:budget]
        budget -= len(text)
        out.append({"index": index, "text": text})
    return out


async def api_file_office_preview(request: web.Request) -> web.Response:
    """GET /api/file-office-preview?path=...[&format=blocks] — inline preview of a .docx/.pptx.

    Sibling of /api/file-download. file-download streams original bytes for
    saving to disk; this endpoint returns plaintext extracted from the
    OOXML XML inside so the dashboard can render a scrollable preview of
    the document contents in place of the "can't view a binary" download
    card — a common ask for anyone browsing shared reports in the file
    tree without wanting to save each one.

    ``format`` selects the shape. The default ``text`` uses
    ``kiro_crew.doc_parser.extract_text``, which parses the .docx / .pptx
    ZIP+XML with hardened defusedxml (XXE-safe) and returns "" on any
    failure. ``blocks`` uses ``kiro_crew.doc_blocks.extract_blocks`` for a
    structured block list of a .docx — headings, formatted paragraph runs,
    lists and tables; any other extension answers an empty list. Text stays
    the default so every existing caller's response is byte-identical, and the
    frontend falls back to it whenever blocks comes back empty. python-docx /
    python-pptx are not required by either path.

    For a .pptx the ``text`` response also carries
    ``"slides": [{"index", "text"}, ...]``. The panel renders a deck slide
    by slide from ``slides`` (a deck flattened into one string reads as a
    parse failure, not a preview); ``text`` stays the flat form for the
    .docx path and for any consumer that predates ``slides``. Both fields
    come from ONE slide walk (``kiro_crew.doc_parser.extract_slides`` /
    ``join_slides``), so they cannot disagree. This endpoint returns only a
    document's text; a deck's rendered slide IMAGES (layout, charts,
    positions) come from the separate ``/api/file-office-slides`` route,
    which needs an office suite to rasterize the deck.

    Not supported (fall through to download): .doc, .ppt, .xls, .xlsx,
    .odt, .ods, .odp. The frontend keeps the download card for these.

    Security: the open-and-check prefix is the SHARED
    :func:`_open_checked_file` (dashboard path validation, sensitive-path
    block, is-file, symlink-refusing ``_open_rb_nofollow`` — atomic
    O_NOFOLLOW on POSIX, lstat guard on Windows — then fstat), never a
    hand-rolled second spelling of it, so a future hardening change to that
    boundary lands here too. This endpoint's own POLICY on top is the 50 MB
    ``fstat_cap``, the ``.docx``/``.pptx`` format gate, the aggregate
    extraction budget, and credential redaction before the preview cap is
    applied. All of it — validation, open, fstat, ZIP+XML parsing,
    redaction — runs in ONE worker-thread hop, like ``api_file_sheet``.
    """
    owner_denied = await require_owner_dashboard_request(request, "file_office_preview")
    if owner_denied is not None:
        return owner_denied
    raw_path = request.query.get("path", "")

    def _log(outcome: str, res: str, error: str = "") -> None:
        kw = {"error": error} if error else {}
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_office_preview",
            outcome=outcome,
            resources=res,
            **kw,
        )

    # Resolve relative paths against project dir when resolve=1. Uses the
    # shared helper (same as api_file_read / api_file_download / file-raw):
    # it passes Windows-absolute/UNC shapes through to the validator, whose
    # network-path gate runs BEFORE realpath — never re-implement this inline.
    # Off-loop: the resolution is a pair of realpath calls.
    if request.query.get("resolve") == "1":
        try:
            raw_path, _resolve_err = await _run_path_probe(_resolve_project_relative, raw_path)
        except _PathProbeBusy:
            return _probe_busy_response(resource=raw_path, tool_name="file_office_preview")
        if _resolve_err == "cannot_resolve":
            _log("denied", request.query.get("path", ""), "cannot_resolve")
            return web.json_response(
                {"error": "cannot resolve: no project dir configured", "code": "no_project_dir"},
                status=400,
            )
        if _resolve_err == "outside_project":
            _log("denied", request.query.get("path", ""), "outside_project")
            return web.json_response(
                {"error": "path outside project directory", "code": "path_outside_project"},
                status=400,
            )

    try:
        validate_tool_args({"path": raw_path}, FILE_READ_SCHEMA)
    except ValidationError:
        _log("denied", raw_path)
        return web.json_response({"error": "invalid input", "code": "invalid_input"}, status=400)

    # An unrecognized format is a 400, never a silent fall back to text: a
    # caller asking for a shape this build does not serve must learn that
    # rather than render a plaintext blob as though it were structure.
    fmt = request.query.get("format", "text")
    if fmt not in ("text", "blocks"):
        _log("denied", raw_path, "invalid_format")
        return web.json_response(
            {"error": "unknown preview format", "code": "invalid_format"},
            status=400,
        )

    # The validated path once the shared prefix produces one -- exported by
    # the worker callback so the exception handlers log the same SEL resource
    # the success path does.
    res_path = raw_path

    def _open_and_extract() -> dict[str, object] | _OpenDenied:
        """Open-and-check plus extract, in ONE worker-thread hop.

        Everything here is blocking I/O or CPU-bound — realpath validation,
        the sensitive-path screen, the open, the fstat, ZIP decompression,
        XML parsing, redaction — so none of it may run on the event loop: an
        NFS/FUSE-backed document makes even the validate/open envelope block
        for seconds, stalling every session's streaming and the liveness
        heartbeat.

        The checked open file object never crosses back to the event loop:
        every path that opens it also closes it on THIS thread (refusals
        close inside the prefix; the ``with`` block below covers the rest,
        the format refusal included). A cancellation of the awaiting task
        therefore cannot strand an open file in a discarded future or
        finalize one on the loop — the future's result is only ever a
        payload dict or a typed refusal.
        """
        nonlocal res_path
        # fstat_cap is this endpoint's size gate, enforced on the fd BEFORE
        # any ZIP parsing: zipfile.ZipFile materializes the archive's central
        # directory in memory, bounded only by the file itself, so a crafted
        # archive could otherwise exhaust memory before doc_parser's
        # per-entry and aggregate budgets ever apply. Same 50 MB ceiling as
        # file uploads. log_open_failure=False: this endpoint answers a coded
        # refusal, so a request loop against a known-unreadable path cannot
        # amplify into the log.
        checked = _open_checked_file(
            raw_path,
            tool_name="file_office_preview",
            fstat_cap=_MAX_UPLOAD_BYTES,
            log_open_failure=False,
        )
        if isinstance(checked, _OpenDenied):
            return checked
        res_path = checked.path
        with checked.file as fobj:
            ext = os.path.splitext(checked.path)[1].lower()
            if ext not in _OFFICE_PREVIEWABLE_EXT:
                raise _PreviewUnsupported(checked.path)
            if fmt == "blocks":
                # Same handle, same one-hop discipline as the text branch:
                # extract_blocks reads through the fd the prefix opened and
                # fstat-ed, so the bytes parsed are the bytes measured. Its own
                # block-count and character budgets bound what one document can
                # become; `truncated` says a budget stopped it, and an empty
                # list means "no structured preview" (malformed container, or a
                # document with nothing extractable) for the frontend to answer
                # by falling back to text.
                blocks, blocks_truncated = extract_blocks(
                    checked.path,
                    filename=os.path.basename(checked.path),
                    fileobj=fobj,
                )
                return {
                    "blocks": _redact_blocks(blocks),
                    "truncated": blocks_truncated,
                }
            # The extractors parse through the SAME handle the prefix opened
            # and fstat-ed (their opt-in fileobj parameter), so the bytes
            # parsed are exactly the bytes measured — no stat→open TOCTOU
            # window. max_chars bounds AGGREGATE extraction (cap + 1 keeps
            # the truncation flag detectable): a deck with thousands of
            # slides stops parsing at the budget instead of accumulating
            # unbounded text. Neither raises — an empty result on any failure.
            slides: list[tuple[int, str]] = []
            if ext == ".pptx":
                # One walk of the deck feeds both fields: `text` is the flat
                # join of the same slides, never a second extraction that
                # could read a different budget or a different byte range.
                slides = extract_slides(
                    checked.path,
                    filename=os.path.basename(checked.path),
                    max_chars=_OFFICE_PREVIEW_CAP + 1,
                    fileobj=fobj,
                )
                text = join_slides(slides)
            else:
                text = extract_text(
                    checked.path,
                    filename=os.path.basename(checked.path),
                    max_chars=_OFFICE_PREVIEW_CAP + 1,
                    fileobj=fobj,
                )
        truncated = len(text) > _OFFICE_PREVIEW_CAP
        # Redact BEFORE truncating: slicing first could cut a credential
        # across the cap boundary, leaving an unmatched prefix the redactor
        # does not recognize. Redaction may change the length, so the
        # truncation flag is computed from the raw extraction above.
        text = redact(text)
        if truncated:
            text = text[:_OFFICE_PREVIEW_CAP]
        payload: dict[str, object] = {
            "text": text,
            "truncated": truncated,
            # No `empty` field: doc_parser returns "" for both a genuinely
            # blank document and a parse failure, so the two are
            # indistinguishable here. The frontend treats empty `text` as
            # "no preview available" and falls back to the download card.
        }
        if slides:
            payload["slides"] = _cap_slides(slides, _OFFICE_PREVIEW_CAP)
        return payload

    try:
        result = await _run_path_probe(_open_and_extract, transfer=True)
    except asyncio.CancelledError:
        # Gateway shutdown / client disconnect while the worker thread is
        # parsing: the access attempt already happened, so record it before
        # propagating — CancelledError is a BaseException and would bypass
        # the Exception handler below, leaving the access unaudited. No
        # resource handling here: the worker callback owns the file's whole
        # lifetime.
        _log("cancelled", res_path)
        raise
    except _PathProbeBusy:
        return _probe_busy_response(resource=res_path, tool_name="file_office_preview")
    except _PreviewUnsupported:
        # 415 (not 400) so the frontend can distinguish "unsupported format,
        # keep showing the download card" from "invalid input, something's
        # actually wrong". The frontend short-circuits known-unsupported
        # extensions client-side, so this branch is the safety net (direct
        # API calls, frontend/backend list drift).
        _log("denied", res_path, "unsupported_preview_format")
        return web.json_response(
            {
                "error": "unsupported format for inline preview",
                "code": "unsupported_preview_format",
            },
            status=415,
        )
    except Exception:  # noqa: BLE001  # last-resort guard; doc_parser already logs
        logger.exception("file_office_preview extract_text failed for %s", res_path)
        _log("failure", res_path)
        return web.json_response(
            {"error": "failed to extract preview", "code": "preview_extraction_failed"},
            status=500,
        )
    if isinstance(result, _OpenDenied):
        # The shared prefix's typed refusals, mapped onto this endpoint's SEL
        # outcomes and response vocabulary — the part that legitimately
        # differs per endpoint.
        code, res = result.code, result.path
        if code == "invalid_path":
            _log("denied", res)
            return web.json_response(
                {"error": "invalid or forbidden path", "code": "forbidden_path"},
                status=400,
            )
        if code == "sensitive_path":
            _log("denied", res, "sensitive_path")
            return web.json_response(
                {"error": "sensitive path blocked", "code": "sensitive_path"},
                status=403,
            )
        if code == "not_found":
            _log("not_found", res)
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        if code == "symlink_refused":
            _log("denied", res, "symlink_rejected")
            return web.json_response(
                {"error": "symlinks not allowed", "code": "symlink_rejected"},
                status=403,
            )
        if code == "file_too_large":
            _log("denied", res, "file_too_large")
            return web.json_response(
                {
                    "error": (
                        "file too large for preview " f"(max {_MAX_UPLOAD_BYTES // 1024 // 1024}MB)"
                    ),
                    "code": "file_too_large",
                },
                status=413,
            )
        # read_failed: the residual code.
        _log("failure", res)
        return web.json_response(
            {"error": "cannot read file", "code": "file_read_failed"},
            status=500,
        )
    _log("success", res_path)
    return web.json_response(result)

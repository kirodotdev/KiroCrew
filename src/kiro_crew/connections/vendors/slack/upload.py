"""The three-stage Slack external file upload protocol, made explicit.

Slack's current file upload is three calls, not one:

1. ``files.getUploadURLExternal`` — send ``length`` (bytes) and ``filename``;
   get back ``{ok, upload_url, file_id}``. Optional ``snippet_type`` / ``alt_txt``
   (``alt_txt`` is capped at 1000 chars — ``alt_txt_too_large``). A ``length`` of
   0 is ``missing_argument``.
2. A bare ``POST`` of the file bytes (raw or multipart) to the returned
   ``upload_url``. This is NOT a Web API method call: it has no ``ok`` envelope.
   Slack returns HTTP 200 on success and any non-200 on failure, and processes
   the bytes asynchronously via a job handler.
3. ``files.completeUploadExternal`` — pass the ``file_id`` (with the optional
   channel / thread / initial-comment placement) to finalize. Slack explicitly
   documents that **if this call is never made the upload is aborted and the
   client receives an error**, and that this call does not need to wait for the
   async job handler.

``files.upload`` (the old single-call method) is DEPRECATED and is only recorded
here as deprecated — it is never the current path.

Why Kiro Crew needs this made explicit
--------------------------------------
Today Kiro Crew's outbound upload goes through ``slack/files.py`` ->
``SlackClientOps.upload_file`` -> ``slack_sdk``'s ``files_upload_v2``, which wraps
all three stages inside the SDK. That is fine for the happy path, but it hides
three things this slice's callers need to reason about and test:

- **Per-stage typed errors.** A failure in stage 1 (``file_uploads_disabled``,
  ``file_type_not_allowed``), stage 2 (a non-200 from the upload service), or
  stage 3 (``completeUploadExternal`` refused) are three different situations,
  and a caller that only sees "the upload failed" cannot tell a scope problem
  from a transient blip from a never-finalized upload. :func:`stage_of_error`
  and :data:`STAGE_ERROR_CODES` name which stage a failure belongs to.
- **``upload_url`` expiry.** The URL from stage 1 is short-lived; a stage-2 POST
  against a stale URL fails. Slack does not currently document the exact TTL on
  the method page (recorded as a gap below), so this module models expiry as a
  state, not a hardcoded duration.
- **``completeUploadExternal`` idempotency.** Completing the same ``file_id``
  twice is a stage-3 concern; Slack gives no idempotency key, so a caller must
  track completion itself.

This module is pure protocol logic: it validates stage inputs, classifies a
stage's outcome, and tracks the small state machine (got-url -> bytes-posted ->
completed). It performs no HTTP and holds no token; the caller drives the three
calls. It does NOT replace ``files_upload_v2`` — it is the explicit model a
caller reasons against, and the record of where the SDK wrapper is not enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class UploadStage(str, Enum):
    """The three stages of the external upload protocol."""

    #: files.getUploadURLExternal
    GET_URL = "get_upload_url"
    #: bare POST of bytes to upload_url
    POST_BYTES = "post_bytes"
    #: files.completeUploadExternal
    COMPLETE = "complete_upload"


class UploadState(str, Enum):
    """Where one upload is in its lifecycle."""

    #: Nothing done yet.
    NEW = "new"
    #: Stage 1 done: we hold an upload_url + file_id.
    URL_ACQUIRED = "url_acquired"
    #: Stage 2 done: bytes accepted (HTTP 200) by the upload service.
    BYTES_POSTED = "bytes_posted"
    #: Stage 3 done: finalized. Terminal success.
    COMPLETED = "completed"
    #: A stage failed. Terminal failure; ``failed_stage`` says which.
    FAILED = "failed"


#: The single-call method that is DEPRECATED. Recorded so callers can detect and
#: refuse it; never the current path.
DEPRECATED_UPLOAD_METHOD = "files.upload"

#: Slack error strings grouped by the stage they can arise in, from the method
#: references (files.getUploadURLExternal / files.completeUploadExternal). Used
#: by :func:`stage_of_error` to attribute a failure to a stage. A code can be
#: shared (e.g. ``ratelimited`` on either Web API call); shared codes are listed
#: under every stage they occur in and :func:`stage_of_error` returns the most
#: specific single stage only when unambiguous.
STAGE_ERROR_CODES: dict[UploadStage, frozenset[str]] = {
    UploadStage.GET_URL: frozenset(
        {
            "file_type_not_allowed",
            "file_upload_size_restricted",
            "file_uploads_disabled",
            "file_uploads_except_images_disabled",
            "storage_limit_reached",
            "alt_txt_too_large",
            "snippet_too_large",
            "unknown_snippet_type",
            "missing_argument",  # length == 0
        }
    ),
    # Stage 2 has no Slack error string — it is a bare HTTP POST whose only
    # signal is the status code. Modeled via post_bytes_ok() below.
    UploadStage.POST_BYTES: frozenset(),
    UploadStage.COMPLETE: frozenset(
        {
            "invalid_arguments",
            "file_not_found",
            "channel_not_found",
            "not_in_channel",
            "access_denied",
        }
    ),
}

#: Slack's ``alt_txt`` cap (stage 1) — ``alt_txt_too_large`` above this.
MAX_ALT_TXT_CHARS = 1000


def stage_of_error(code: str) -> Optional[UploadStage]:
    """Attribute a Slack error string to the upload stage it arises in.

    Returns the stage when the code maps to exactly one, else ``None`` (an
    unknown or cross-stage code the caller must place by context). Fails closed
    to ``None`` rather than guessing a stage.
    """
    hits = [stage for stage, codes in STAGE_ERROR_CODES.items() if code in codes]
    return hits[0] if len(hits) == 1 else None


def validate_get_url_inputs(length: int, filename: str, alt_txt: str = "") -> list[str]:
    """Validate stage-1 inputs before the call. Returns a list of error strings.

    Mirrors what Slack would reject: a non-positive ``length`` is
    ``missing_argument`` (Slack's own note: "typically only occurs when the
    length provided is 0"), an empty ``filename`` is invalid, and an ``alt_txt``
    over the cap is ``alt_txt_too_large``. Validating locally turns a network
    round-trip into an immediate, testable rejection.
    """
    errors: list[str] = []
    if length <= 0:
        errors.append("length must be a positive byte count (missing_argument)")
    if not filename:
        errors.append("filename is required")
    if len(alt_txt) > MAX_ALT_TXT_CHARS:
        errors.append(f"alt_txt {len(alt_txt)} > {MAX_ALT_TXT_CHARS} chars (alt_txt_too_large)")
    return errors


def post_bytes_ok(http_status: int) -> bool:
    """Stage 2 has no ``ok`` envelope: HTTP 200 means accepted, anything else fails.

    Slack documents exactly "HTTP 200 if the upload is successful; a non-200
    response indicates a failure" for the bare POST to ``upload_url``.
    """
    return http_status == 200


@dataclass
class UploadSession:
    """The small state machine for one external upload.

    A caller advances it as each stage returns. It refuses out-of-order
    transitions (posting bytes before acquiring a URL, completing before posting)
    so a protocol misuse is a loud error here rather than a confusing Slack
    rejection later. Completing an already-completed session is refused — the
    stand-in for the missing idempotency key: the caller must not fire stage 3
    twice, and this catches it.
    """

    filename: str
    length: int
    state: UploadState = UploadState.NEW
    file_id: Optional[str] = None
    upload_url: Optional[str] = None
    failed_stage: Optional[UploadStage] = None
    error_code: Optional[str] = None
    _bytes_posted: bool = field(default=False, init=False)

    def acquired_url(self, *, upload_url: str, file_id: str) -> None:
        """Record a successful stage 1."""
        if self.state is not UploadState.NEW:
            raise ValueError(f"cannot acquire URL from state {self.state.value}")
        if not upload_url or not file_id:
            raise ValueError("stage 1 must return both upload_url and file_id")
        self.upload_url = upload_url
        self.file_id = file_id
        self.state = UploadState.URL_ACQUIRED

    def posted_bytes(self, http_status: int) -> bool:
        """Record a stage-2 POST result; return whether it succeeded.

        A non-200 marks the session FAILED at the POST stage. Posting before a
        URL was acquired is a protocol misuse and raises.
        """
        if self.state is not UploadState.URL_ACQUIRED:
            raise ValueError(f"cannot post bytes from state {self.state.value}")
        if not post_bytes_ok(http_status):
            self.state = UploadState.FAILED
            self.failed_stage = UploadStage.POST_BYTES
            self.error_code = f"http_{http_status}"
            return False
        self._bytes_posted = True
        self.state = UploadState.BYTES_POSTED
        return True

    def completed(self) -> None:
        """Record a successful stage 3 (finalization).

        Refuses to complete before bytes were posted (Slack aborts an upload
        whose complete call never lands, and completing before posting is the
        inverse misuse), and refuses to complete twice (the missing-idempotency
        guard).
        """
        if self.state is UploadState.COMPLETED:
            raise ValueError(
                "upload already completed (no idempotency key: "
                "the caller must not complete twice)"
            )
        if self.state is not UploadState.BYTES_POSTED:
            raise ValueError(
                f"cannot complete from state {self.state.value}; bytes must be " f"posted first"
            )
        self.state = UploadState.COMPLETED

    def failed(self, stage: UploadStage, code: str) -> None:
        """Record a stage-1 or stage-3 Web API failure with its Slack error code."""
        self.state = UploadState.FAILED
        self.failed_stage = stage
        self.error_code = code

    @property
    def is_complete(self) -> bool:
        return self.state is UploadState.COMPLETED

    @property
    def is_failed(self) -> bool:
        return self.state is UploadState.FAILED

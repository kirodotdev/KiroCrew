"""Delivery intent — the durable outbox record behind one review-posting operation.

These helpers mint and check operation markers, digest outbound payloads,
persist intent transitions, and phrase the poster's predecessor-deletion step.
``review_driver`` re-exports them. Remote read-back lives in ``posting_recovery``
and uses the driver's canonical diff and host helpers.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path

from sage_lib import adapters, results

_OPERATION_MARKER_PREFIX = "<!-- code-review-sage-operation:"
_OPERATION_MARKER_SUFFIX = " -->"
_OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}")


class DeliveryProbe(str, Enum):
    """The remote evidence states that govern whether a delivery may proceed."""

    FOUND = "FOUND"
    ABSENT = "ABSENT"
    PREDECESSOR_SUBMITTED = "PREDECESSOR_SUBMITTED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


def _operation_marker(operation_id: str) -> str:
    """Return the non-preview marker that identifies one outbound operation."""
    if operation_id == "":
        return f"{_OPERATION_MARKER_PREFIX}{_OPERATION_MARKER_SUFFIX}"
    if not isinstance(operation_id, str) or not _OPERATION_ID_RE.fullmatch(operation_id):
        raise ValueError("delivery operation ID must be 32 lowercase hexadecimal characters")
    return f"{_OPERATION_MARKER_PREFIX}{operation_id}{_OPERATION_MARKER_SUFFIX}"


def _marker_free_body(value: object) -> str:
    """Drop operation-marker lines from a body before it enters a digest.

    The poster LLM appends the marker to the outbound body with a separator of
    its own choosing, so the driver cannot reconstruct the exact wire bytes —
    a digest over them would read a different separator as a different payload.
    The marker's job is matching an operation to a review, a plain substring
    check, not identity: the digest is therefore computed over the marker-free
    payload on both sides.

    Only a line that IS exactly a marker is dropped: anything else sharing a
    marker line (a body whose last line got text appended after the marker) is
    a real payload difference and must fail the comparison.
    """

    def _is_marker_line(line: str) -> bool:
        stripped = line.strip()
        if not (
            stripped.startswith(_OPERATION_MARKER_PREFIX)
            and stripped.endswith(_OPERATION_MARKER_SUFFIX)
        ):
            return False
        inner = stripped[len(_OPERATION_MARKER_PREFIX) : -len(_OPERATION_MARKER_SUFFIX)]
        return inner == "" or bool(_OPERATION_ID_RE.fullmatch(inner))

    return "\n".join(line for line in str(value or "").split("\n") if not _is_marker_line(line))


def _outbound_payload(payload: dict, operation_id: str) -> dict:
    """Make the wire-only payload without contaminating the visible draft payload."""
    wire = dict(payload)
    body = _marker_free_body(wire.get("body")).rstrip()
    wire["body"] = f"{body}\n\n{_operation_marker(operation_id)}"
    return wire


def _canonical_payload(payload: dict) -> dict:
    """Normalize exactly the GitHub fields whose remote echo proves a delivery."""
    comments = []
    for comment in payload.get("comments") or []:
        comments.append(
            {
                "path": str(comment.get("path") or ""),
                "line": int(comment.get("line") or 0),
                "side": str(comment.get("side") or "RIGHT").upper(),
                "body": _confirm_text(comment.get("body")),
            }
        )
    comments.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return {
        "body": _confirm_text(_marker_free_body(payload.get("body"))),
        "comments": comments,
        "commit_id": str(payload.get("commit_id") or ""),
    }


def _payload_digest(payload: dict) -> str:
    """Hash the exact canonical payload that is sent to GitHub."""
    wire = json.dumps(_canonical_payload(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _canonical_target(link: str) -> str:
    """Canonicalize a GitHub pull-request URL before it enters durable intent."""
    host, owner, repo, number = adapters.github_pr_ref(link)
    return f"https://{host}/{owner}/{repo}/pull/{number}"


# A retained error is a diagnostic, not evidence: its text can carry a whole `gh`
# stderr. Kept short so a large one cannot push the record past the size every
# cross-boundary reader refuses (results._RECORD_MAX_BYTES).
_INTENT_ERROR_MAX_CHARS = 2000


def _write_intent(
    record: dict,
    intent: dict,
    state: str,
    *,
    root: Path | None,
    run_id: str | None,
    error: str = "",
    review_id: str = "",
) -> None:
    """Persist a state transition before any caller can observe or act on it."""
    intent["state"] = state
    if error:
        intent["error"] = (
            error
            if len(error) <= _INTENT_ERROR_MAX_CHARS
            else error[: _INTENT_ERROR_MAX_CHARS - 1] + "\u2026"
        )
    else:
        intent.pop("error", None)
    if review_id:
        intent["review_id"] = review_id
    record["delivery_intent"] = intent
    results.write_result(record, root, run_id)


def _intent_payload(record: dict, intent: dict) -> tuple[dict | None, str]:
    """Rebuild and verify the immutable wire payload held by an intent digest."""
    payload = record.get("github_review_payload")
    operation_id = intent.get("operation_id")
    if not isinstance(operation_id, str) or not _OPERATION_ID_RE.fullmatch(operation_id):
        return None, "delivery intent has invalid operation ID"
    if not isinstance(payload, dict):
        return None, "delivery intent has no reconstructable outbound payload"
    wire = _outbound_payload(payload, operation_id)
    if _payload_digest(wire) != str(intent.get("payload_digest") or ""):
        return None, "delivery intent payload no longer matches its immutable digest"
    return wire, ""


def _predecessor_instruction(predecessor: dict | None, *, recovering: bool = False) -> str:
    """Tell the poster the only review it may replace after driver reconciliation."""
    if not predecessor:
        if recovering:
            return (
                "  2. This is an interrupted saved operation. Do NOT delete any existing "
                "pending review, even one with the Sage watermark. Immediately before "
                "POST, GET repos/<owner>/<repo>/pulls/<number>/reviews. If an existing "
                "pending review blocks this account's POST, stop without DELETE or POST "
                "and report it. If a pending review appears after this check and GitHub "
                "refuses the POST, stop without deleting it.\n"
            )
        return (
            "  2. FIRST clear any stale sage draft: GitHub allows only ONE pending "
            "review per PR per user, so a leftover one would make step 3 fail with 422. "
            "GET repos/<owner>/<repo>/pulls/<number>/reviews and, if a review with "
            'state=="PENDING" exists WHOSE BODY CONTAINS the exact marker '
            "`[code-review-sage]`, DELETE just that one (DELETE "
            "repos/<owner>/<repo>/pulls/<number>/reviews/<review_id>) — it is a stale "
            "sage draft. NEVER delete a non-PENDING review or a PENDING review lacking "
            "that marker (it may be a human's in-progress draft).\n"
        )
    review_id = str(predecessor.get("review_id") or "")
    operation_id = str(predecessor.get("operation_id") or "")
    if not review_id or not operation_id:
        return "  2. Do NOT delete any existing pending review. If GitHub refuses the POST, stop.\n"
    marker = _operation_marker(operation_id)
    expected = predecessor.get("payload")
    if not isinstance(expected, dict):
        # No verified content to compare against: an edit made since the driver
        # last read the draft could not be told apart, so it is never deleted.
        return "  2. Do NOT delete any existing pending review. If GitHub refuses the POST, stop.\n"
    return (
        "  2. The driver reconciled exactly one locally known predecessor review: "
        f"id `{review_id}` with hidden marker `{marker}`. Immediately before deleting it, "
        "re-read it and compare it EXACTLY with what the driver verified, because a "
        "human may have edited the draft since:\n"
        f"     a. gh api repos/<owner>/<repo>/pulls/<number>/reviews/{review_id} "
        "--jq '{state, body}' and confirm `state` is PENDING and `body` contains the "
        "marker and the exact Sage watermark `[code-review-sage]`.\n"
        "     b. Copy the verified payload without printing or interpreting its text: "
        "jq '.delivery_intent.predecessor.payload' data/results/<id>.json > expected.json\n"
        f'     c. Build `actual.json` with: jq -n --argjson r "$(gh api '
        f'repos/<owner>/<repo>/pulls/<number>/reviews/{review_id})" --argjson c '
        f'"$(gh api --paginate --slurp repos/<owner>/<repo>/pulls/<number>/reviews/'
        f'{review_id}/comments)" '
        "'{body: $r.body, comments: [$c[][] | {path, line, side, position, body}]}' "
        "> actual.json\n"
        "     d. Run: diff <(jq -S . expected.json) <(jq -S . actual.json)\n"
        "     Only if the state is PENDING, both markers are present AND the diff is empty, "
        f"DELETE that exact id (DELETE repos/<owner>/<repo>/pulls/<number>/reviews/{review_id}). "
        "If any check differs, stop without DELETE or POST and report it. Never delete "
        "a review discovered only by the generic Sage marker or a human draft.\n"
    )


def _confirm_text(value: object) -> str:
    """Normalize a body for comparison: newline form, and trailing space per line.

    GitHub echoes a review body back verbatim except for line-ending form, so this
    is the smallest normalization that keeps the comparison an IDENTITY check rather
    than a fuzzy match. Anything beyond CRLF and trailing blanks is a real
    difference and must fail the comparison.
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()

#!/usr/bin/env python3
"""Worker response envelope -- bind a review record to the dispatch that asked for it.

Each review and coverage-follow-up worker returns a bounded JSON envelope that
carries a driver-issued capability and the exact change id. ``review_driver``
issues the capability, then hands the worker's turn text here: this module finds
the matching envelope, validates the record, and persists it to the run's result
scope. The caller consumes the record returned here and never re-reads the
run-scoped path, which stays writable by every dispatched worker.

``review_driver`` re-exports these names, so ``review_driver._persist_worker_response``
and friends remain the import path callers and tests use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

# Module scope, per the repo's top-level-imports rule. No cycle: `results` and
# `store` do not import this module.
from sage_lib import results, store

_RESPONSE_SCHEMA = "code-review-sage-response"
_RESPONSE_VERSION = 1
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_ENVELOPE_CANDIDATES = 64


def _record_digest(record: dict) -> str:
    """Stable digest binding a follow-up response to the first-pass record."""
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finding_counts(findings: list[dict]) -> dict[str, int]:
    """Return the result-contract severity tallies for accepted findings."""
    return {
        "red": sum(finding.get("severity") == "red" for finding in findings),
        "yellow": sum(finding.get("severity") == "yellow" for finding in findings),
    }


def _decode_worker_envelope(response: str, change_id: str, capability: str) -> dict | None:
    """Find a dispatch-bound envelope within bounded candidate parsing."""
    decoder = json.JSONDecoder()
    depth = 0
    in_string = False
    end = len(response)
    candidates = 0
    fallback = None
    for start in range(len(response) - 1, -1, -1):
        if candidates >= _MAX_ENVELOPE_CANDIDATES:
            break
        char = response[start]
        if depth == 0:
            if char == "}":
                end = start + 1
                depth = 1
            continue
        if char == '"':
            backslash = start - 1
            while backslash >= 0 and response[backslash] == "\\":
                backslash -= 1
            if (start - backslash - 1) % 2 == 0:
                in_string = not in_string
        if in_string:
            continue
        if char == "}":
            depth += 1
        elif char == "{":
            depth -= 1
        if depth:
            continue
        candidates += 1
        if end - start > _MAX_RESPONSE_BYTES:
            continue
        candidate = response[start:end]
        if len(candidate.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            continue
        try:
            envelope, decoded_end = decoder.raw_decode(candidate)
        except (ValueError, RecursionError):
            continue
        if decoded_end != len(candidate):
            continue
        if response[end:].lstrip().startswith(("}", "]", ",")):
            continue
        if not isinstance(envelope, dict):
            continue
        fallback = envelope
        received = envelope.get("capability")
        if (
            envelope.get("schema") == _RESPONSE_SCHEMA
            and envelope.get("version") == _RESPONSE_VERSION
            and envelope.get("change_id") == change_id
            and isinstance(received, str)
            and received.isascii()
            and hmac.compare_digest(received, capability)
        ):
            return envelope
    return fallback


def _persist_worker_response(
    response: object,
    *,
    change_id: str,
    capability: str,
    root: Path | None,
    run_id: str | None,
    base_record: dict | None = None,
) -> tuple[dict | None, str]:
    """Validate a bound worker envelope, publish its record, and return it.

    The accepted record is returned rather than left for the caller to read
    back: the run-scoped path stays writable by every dispatched worker, so a
    re-read is an unauthenticated second chance for a sibling to substitute
    its own file for the one this envelope authorised.
    """
    if not isinstance(response, str):
        return None, "worker response is missing"
    try:
        envelope = _decode_worker_envelope(response, change_id, capability)
    except UnicodeEncodeError:
        return None, "worker response is not valid UTF-8"
    except RecursionError:
        # A payload nested past the JSON parser's depth fails THIS change
        # alone; it must never abort the whole batch through `pool.map`.
        return None, "worker response is not valid JSON"
    if envelope is None:
        return None, "worker response is not valid JSON"
    if envelope.get("schema") != _RESPONSE_SCHEMA or envelope.get("version") != _RESPONSE_VERSION:
        return None, "worker response has an unsupported envelope schema"
    received_capability = envelope.get("capability")
    if not isinstance(received_capability, str):
        return None, "worker response capability is missing or invalid"
    try:
        capabilities_match = hmac.compare_digest(
            received_capability.encode("ascii"), capability.encode("ascii")
        )
    except UnicodeEncodeError:
        return None, "worker response capability is missing or invalid"
    if not capabilities_match:
        return None, "worker response capability is missing or invalid"
    if envelope.get("change_id") != change_id:
        return None, "worker response names a different change"
    record = envelope.get("record")
    if not isinstance(record, dict):
        return None, "worker response record must be an object"
    if record.get("change_id") != change_id:
        return None, "worker response record names a different change"
    errors = results.validate_result(record)
    if errors:
        # The validator interpolates the record's own key names (phase1.<k>,
        # findings[i].<k>, counts.<band>), and those keys are worker-written.
        # This string is surfaced and logged, so it goes through the same
        # scrub every other boundary carrying model-written text uses.
        return None, store.redact_text("worker response record is invalid: " + "; ".join(errors))
    if base_record is not None:
        if envelope.get("base_digest") != _record_digest(base_record):
            return None, "worker follow-up is not bound to the first-pass record"
        base_findings = base_record.get("findings", [])
        candidate_findings = record.get("findings", [])
        if not isinstance(base_findings, list) or not isinstance(candidate_findings, list):
            return None, "worker response record is invalid: findings must be a list"
        if candidate_findings[: len(base_findings)] != base_findings:
            return None, "worker follow-up removed or changed an existing finding"
        if record.get("phase1") != base_record.get("phase1"):
            return None, "worker follow-up changed the first-pass design assessment"
        # A coverage pass can extend findings and coverage, but it has no authority
        # over the first pass's change identity or review metadata. Reusing its
        # complete record let it replace the revision that anchors comment payloads.
        covered = record.get("files_covered", base_record.get("files_covered", []))
        if not isinstance(covered, list):
            return None, "worker response record is invalid: files_covered must be a list"
        accepted_record = json.loads(json.dumps(base_record))
        accepted_record["findings"] = candidate_findings
        accepted_record["counts"] = _finding_counts(candidate_findings)
        # A coverage pass reports the files IT read. Taking that answer whole
        # drops every path the first pass already covered, so the record would
        # claim less coverage than was reviewed -- and `coverage_complete` would
        # assert completeness over the smaller list.
        base_covered = base_record.get("files_covered", [])
        merged = list(base_covered) if isinstance(base_covered, list) else []
        merged += [name for name in covered if name not in merged]
        accepted_record["files_covered"] = merged
        for key in ("coverage_complete", "ship_summary"):
            if key in record:
                accepted_record[key] = record[key]
        record = accepted_record
    try:
        results.write_result(record, root, run_id)
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError: a worker record nested past the JSON encoder's depth
        # (a hostile or corrupted `record.extra`) fails THIS change alone; it
        # must never abort the whole batch through `pool.map`.
        return None, store.redact_text(f"could not persist worker response: {exc}")
    return record, ""

#!/usr/bin/env python3
"""check_connector_discovery.py -- validate the provider-capabilities discovery shapes.

The provider-capabilities discovery protocol -- a live probe that is a SEPARATE
protocol from the capability manifest -- is specified in
``docs/system-specs/modules/connector-discovery.md``. That doc is the single
home for what this gate validates, what it deliberately does NOT validate (four
manifest/run/receipt fields whose sole enforcer is the connector manifest
validator), and why ``source_status`` and ``evidence_tier`` are distinct fields
that must never be aliased. This docstring does not restate those contracts --
restating them would put one contract in two homes, the same drift this module's
own single-home rule forbids; read the doc for the normative statement.

In brief, for a reader at the call site: this checker validates the STRUCTURE of
``discovery_request`` / ``discovery_response`` (see ``validate_document``), and
nothing about the manifest, run, or receipt schemas.

## Usage

    # validate one or more discovery JSON documents
    python3 scripts/check_connector_discovery.py path/to/discovery.json ...

    # self-test: one probe per rule, each asserted
    python3 scripts/check_connector_discovery.py --test

Exit status is 0 when every input validates (or the self-test passes), 1 when a
document is malformed, and 2 on a usage error (unreadable or non-JSON input).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    """The outcome of validating one discovery document."""

    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _reject_json_constant(constant: str) -> Any:
    """Reject the non-standard JSON constants NaN / Infinity / -Infinity.

    CPython's json.load defaults to allow_nan=True, which accepts these three
    tokens even though RFC 8259 JSON does not permit them. Passed as
    ``parse_constant``, this raises ValueError so json.load propagates it into
    the read path's ValueError handler and the document is reported as a usage
    error rather than validating and falsely certifying invalid JSON.
    """

    raise ValueError(f"{constant} is not valid JSON")


def _validate_request(req: Any, result: Result, where: str) -> None:
    if not isinstance(req, dict):
        result.fail(where, "discovery_request must be an object")
        return
    if not _is_nonempty_str(req.get("provider")):
        result.fail(f"{where}.provider", "must be a non-empty string")
    if not _is_nonempty_str(req.get("account_binding")):
        result.fail(f"{where}.account_binding", "must be a non-empty string")
    if "requested_scope_hint" in req and not isinstance(req["requested_scope_hint"], list):
        result.fail(f"{where}.requested_scope_hint", "when present, must be an array")


def _validate_capability_row(row: Any, result: Result, where: str) -> None:
    if not isinstance(row, dict):
        result.fail(where, "capability_rows entry must be an object")
        return
    # operation_id is nullable: a string names a manifest operation, null records
    # a vendor capability the manifest does not yet cover. Absence is not null --
    # the key must be present so a reader cannot mistake "not probed" for "no match".
    if "operation_id" not in row:
        result.fail(f"{where}.operation_id", "key is required (use null when unmatched)")
    else:
        op = row["operation_id"]
        if op is not None and not _is_nonempty_str(op):
            result.fail(
                f"{where}.operation_id",
                "must be a non-empty string or null",
            )
    if not _is_nonempty_str(row.get("raw_capability_signature")):
        result.fail(f"{where}.raw_capability_signature", "must be a non-empty string")
    # A real bool, not a truthy string or 0/1: matches_manifest gates whether a
    # row is a gap, so an ambiguous type here would let a gap read as a match.
    if not isinstance(row.get("matches_manifest"), bool):
        result.fail(f"{where}.matches_manifest", "must be a boolean")
    # Cross-field: a match must name the operation it matched. operation_id=null
    # means "no manifest operation matched", so matches_manifest=true beside it
    # is a self-contradiction -- accepting it would exit 0 and falsely certify a
    # manifest match. A named operation with matches_manifest=false is fine
    # (probed, did not match); only the null+true combination is contradictory.
    if row.get("matches_manifest") is True and row.get("operation_id") is None:
        result.fail(
            f"{where}.matches_manifest",
            "cannot be true when operation_id is null (a match must name its operation)",
        )


def _validate_response(resp: Any, result: Result, where: str) -> None:
    if not isinstance(resp, dict):
        result.fail(where, "discovery_response must be an object")
        return
    if not _is_nonempty_str(resp.get("provider")):
        result.fail(f"{where}.provider", "must be a non-empty string")
    if not _is_nonempty_str(resp.get("observed_at")):
        result.fail(f"{where}.observed_at", "must be a non-empty timestamp string")
    if not _is_nonempty_str(resp.get("version_snapshot")):
        result.fail(f"{where}.version_snapshot", "must be a non-empty string")
    if not isinstance(resp.get("scope_snapshot"), list):
        result.fail(f"{where}.scope_snapshot", "must be an array")
    rows = resp.get("capability_rows")
    if not isinstance(rows, list):
        result.fail(f"{where}.capability_rows", "must be an array")
    else:
        for i, row in enumerate(rows):
            _validate_capability_row(row, result, f"{where}.capability_rows[{i}]")


def validate_document(doc: Any) -> Result:
    """Validate a discovery document holding a request, a response, or both.

    A document may carry ``discovery_request``, ``discovery_response``, or both.
    At least one must be present -- a document with neither has nothing this
    protocol recognises and is malformed.
    """

    result = Result()
    if not isinstance(doc, dict):
        result.fail("<root>", "document must be a JSON object")
        return result
    has_request = "discovery_request" in doc
    has_response = "discovery_response" in doc
    if not has_request and not has_response:
        result.fail(
            "<root>",
            "must contain discovery_request, discovery_response, or both",
        )
        return result
    if has_request:
        _validate_request(doc["discovery_request"], result, "discovery_request")
    if has_response:
        _validate_response(doc["discovery_response"], result, "discovery_response")
    return result


def _run_files(paths: list[str]) -> int:
    exit_code = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle, parse_constant=_reject_json_constant)
        except OSError as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 2
        except RecursionError as exc:
            # RecursionError is a RuntimeError subclass -- outside OSError,
            # json.JSONDecodeError and ValueError -- so a document nested deep
            # enough to exhaust the C recursion limit during json.load would
            # otherwise escape as a traceback. Same family as the
            # UnicodeDecodeError and embedded-NUL cases below: an exception
            # class the other handlers do not name. The input cannot be read as
            # a document, so it is a usage error, not a crash.
            print(f"error: {path} is nested too deeply to parse: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            # ValueError subsumes three read-path families that escape both
            # OSError and JSONDecodeError: UnicodeDecodeError (a ValueError
            # subclass) when the bytes are not valid UTF-8; the bare
            # ValueError("embedded null byte") that open() raises when the path
            # argument itself contains a NUL; and the ValueError raised by
            # _reject_json_constant when json.load meets a non-standard
            # NaN/Infinity/-Infinity token. All mean the input cannot be read as
            # a valid-JSON document, so all are usage errors, not crashes.
            print(f"error: {path} cannot be read as a document: {exc}", file=sys.stderr)
            return 2
        result = validate_document(doc)
        if result.ok:
            print(f"ok: {path}")
        else:
            exit_code = 1
            print(f"FAIL: {path}")
            for err in result.errors:
                print(f"  - {err}")
    return exit_code


def _selftest() -> int:
    """One probe per rule, each asserted -- the gate's own regression guard."""

    good = {
        "discovery_request": {
            "provider": "example",
            "account_binding": "binding-1",
            "requested_scope_hint": ["read"],
        },
        "discovery_response": {
            "provider": "example",
            "observed_at": "2026-01-01T00:00:00Z",
            "scope_snapshot": ["read", "write"],
            "capability_rows": [
                {
                    "operation_id": "example.read",
                    "raw_capability_signature": "read",
                    "matches_manifest": True,
                },
                {
                    "operation_id": None,
                    "raw_capability_signature": "vendor.extra",
                    "matches_manifest": False,
                },
            ],
            "version_snapshot": "v3",
        },
    }
    assert validate_document(good).ok, "a well-formed document must pass"

    # request: empty provider / binding, non-array hint
    assert not validate_document(
        {"discovery_request": {"provider": "", "account_binding": "b"}}
    ).ok, "empty provider must fail"
    assert not validate_document(
        {"discovery_request": {"provider": "p", "account_binding": ""}}
    ).ok, "empty account_binding must fail"
    assert not validate_document(
        {
            "discovery_request": {
                "provider": "p",
                "account_binding": "b",
                "requested_scope_hint": "read",
            }
        }
    ).ok, "non-array requested_scope_hint must fail"

    # response: missing fields
    assert not validate_document(
        {"discovery_response": {"provider": "p"}}
    ).ok, "response missing observed_at/scope_snapshot/rows/version must fail"

    # capability row: operation_id present-as-null passes; absent fails
    row_ok = {
        "discovery_response": {
            "provider": "p",
            "observed_at": "t",
            "scope_snapshot": [],
            "version_snapshot": "v",
            "capability_rows": [
                {
                    "operation_id": None,
                    "raw_capability_signature": "sig",
                    "matches_manifest": False,
                }
            ],
        }
    }
    assert validate_document(row_ok).ok, "operation_id=null must pass"

    row_missing_op = json.loads(json.dumps(row_ok))
    del row_missing_op["discovery_response"]["capability_rows"][0]["operation_id"]
    assert not validate_document(
        row_missing_op
    ).ok, "absent operation_id key must fail (null is not absence)"

    # matches_manifest must be a real bool, not a truthy string or int
    for bad_bool in ("true", 1, 0, None):
        doc = json.loads(json.dumps(row_ok))
        doc["discovery_response"]["capability_rows"][0]["matches_manifest"] = bad_bool
        assert not validate_document(doc).ok, f"matches_manifest={bad_bool!r} must fail"

    # empty raw_capability_signature
    doc = json.loads(json.dumps(row_ok))
    doc["discovery_response"]["capability_rows"][0]["raw_capability_signature"] = ""
    assert not validate_document(doc).ok, "empty raw_capability_signature must fail"

    # cross-field: matches_manifest=true with operation_id=null is contradictory
    doc = json.loads(json.dumps(row_ok))
    doc["discovery_response"]["capability_rows"][0]["operation_id"] = None
    doc["discovery_response"]["capability_rows"][0]["matches_manifest"] = True
    assert not validate_document(
        doc
    ).ok, "matches_manifest=true with operation_id=null must fail (a match must name its operation)"

    # a document with neither request nor response
    assert not validate_document({}).ok, "empty document must fail"
    assert not validate_document([]).ok, "non-object document must fail"

    print("check_connector_discovery self-test: all probes passed")
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if "--test" in args:
        return _selftest()
    if not args:
        print(
            "usage: check_connector_discovery.py [--test] <discovery.json> ...",
            file=sys.stderr,
        )
        return 2
    return _run_files(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""scope_redact — the security-scope review's ONE outbound scrub.

Every surface the scope-review lanes write is world-readable on a public
repository: the job log, the step summary, the uploaded artifact, and the pull
request comment. The text they publish is not text they authored -- a candidate
operation and a refusal reason are model-written, out of a diff the model read --
so any of it can quote an access-key id, an ARN, an account id or a
credential-bearing assignment. Redacting before publication is therefore a
property of the lane, not a nicety.

This script is that redaction, in ONE place. A regex copied per call site is a
defect generator: the copy that is wrong is wrong on every surface it guards, and
nothing can test it, because a program embedded in a ``run:`` body has no seam to
call. Here the vocabulary is testable, and a fix lands once.

Two modes, because the two file shapes cannot share one mechanism.

``--mode json``
    Parse, redact inside STRING VALUES only, re-serialize. The output is always
    valid JSON, which is the property the report's consumer depends on: the
    verdict folder reads a leg's report with ``json.loads`` and a report that
    does not parse is exit 2 -- a hard block that names no rows. A substitution
    performed on the raw bytes cannot hold that property, because a replacement
    whose match ran past the closing quote deletes the quote. A non-string scalar
    is never rewritten either, so a 12-digit JSON *number* stays a number instead
    of becoming a bare token where a value belongs. Input that does not parse as
    JSON is an ERROR the caller must see, never a silent fall back to text mode:
    the caller asked for the guarantee, and only a parse can give it.

``--mode text``
    Line-oriented redaction for logs, stderr and prose, where there is no
    structure to preserve.

Callers branch on whether anything CHANGED -- a redacted row names a placeholder
instead of the operation that was refused, so the lane withholds it rather than
passing that off as a finding -- so the answer is reported two ways: a
``changed=`` line per file on stdout, and ``--fail-if-changed`` for a caller that
wants the branch as an exit code.

Exit codes: ``0`` every file was rewritten (whether or not anything changed),
``1`` a file could not be redacted, ``10`` with ``--fail-if-changed`` and at
least one file changed. ``1`` and ``10`` are distinct because they demand
opposite things of the caller: ``1`` means the text is UNSCRUBBED and must not be
published at all, and ``10`` means it was scrubbed successfully and the caller
must decide whether a redacted artifact is still worth publishing.

The ``[REDACTED-...]`` marker spellings are a CONTRACT, not cosmetics. Both
lanes' seed paths grep for ``[REDACTED-`` to refuse a redacted row on the way
back in, so a renamed marker silently stops matching and a row nobody can read
travels into the next run's candidate set.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: An AWS access-key id. The prefix set and the 20-character total length are
#: fixed by AWS, and the body is base32, so this shape has no false-positive
#: reading in prose.
_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z2-7]{16}\b")

#: An ARN, bounded by the characters an ARN can actually contain rather than by
#: "up to the next whitespace". A whitespace-terminated match eats the closing
#: quote and the comma that follow an ARN at the end of a JSON string value, and
#: in prose it eats the sentence's own punctuation -- so the bound is the fix,
#: independent of which mode is running. Everything an ARN legally holds is here:
#: the partition and service names, the region, the account id, and a resource
#: path with its separators and wildcards.
_ARN_RE = re.compile(r"arn:aws[A-Za-z0-9:_./+=@*-]*")

#: A 12-digit AWS account id. Applied to STRING content only in JSON mode: a
#: 12-digit JSON number is a count, not an account, and rewriting it to a bare
#: token where a value belongs is what makes the whole document unparseable.
_ACCOUNT_RE = re.compile(r"\b[0-9]{12}\b")

#: A credential-bearing assignment, in either an ``=`` or a ``:`` spelling, with
#: an optional quote on each side so a JSON-shaped assignment inside a string
#: still matches. The value is bounded by the delimiters that END a value, so a
#: match cannot swallow the punctuation that follows it.
_SECRET_ASSIGN_RE = re.compile(
    r"(aws_secret_access_key|aws_session_token|x-amz-security-token)"
    r"[\"']?\s*[:=]\s*[\"']?"
    r"[^\s\"',;)\]}]+",
    re.IGNORECASE,
)

#: The same names as a whole JSON KEY. In JSON mode a key and its value are two
#: separate strings, so the assignment pattern above -- which needs both on one
#: side of the separator -- cannot see the pair; without this rule
#: ``{"aws_session_token": "<secret>"}`` would publish the secret. Anchored, so a
#: field merely MENTIONING a token name in prose is not treated as holding one.
_SECRET_KEY_RE = re.compile(
    r"^\s*[\"']?(aws_secret_access_key|aws_session_token|x-amz-security-token)[\"']?\s*$",
    re.IGNORECASE,
)

#: The marker a redacted secret VALUE carries. Deliberately the same spelling the
#: assignment rule produces, so the two rules cannot be told apart downstream by
#: a reader who only has the published text.
_SECRET_MARKER = "[REDACTED]"


class RedactError(Exception):
    """A file could not be redacted, so its text must not be published."""


def redact_text(text: str) -> str:
    """Every rule, in the order that keeps the earlier ones' output intact.

    The ARN rule runs before the account rule because an ARN CONTAINS an account
    id: reversing them would rewrite the account id first and leave an ARN the
    ARN rule cannot recognise, publishing the partition, service, region and
    resource path around a redacted middle.
    """
    text = _KEY_ID_RE.sub("[REDACTED-AWS-KEY-ID]", text)
    text = _ARN_RE.sub("[REDACTED-ARN]", text)
    text = _ACCOUNT_RE.sub("[REDACTED-ACCT]", text)
    return _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}={_SECRET_MARKER}", text)


def _redact_json_value(value: Any, *, key: str | None = None) -> tuple[Any, bool]:
    """Walk the document, rewriting string content and nothing else.

    Returns the value and whether this subtree changed. The traversal is
    exhaustive -- a credential in a row nested three levels down is the ordinary
    case here, since a report's findings live under a list under a dict.
    """
    if isinstance(value, str):
        if key is not None and _SECRET_KEY_RE.match(key):
            return _SECRET_MARKER, value != _SECRET_MARKER
        redacted = redact_text(value)
        return redacted, redacted != value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        changed = False
        for name, child in value.items():
            # A KEY names a field the consumer reads by name, so rewriting one
            # changes the schema rather than the content -- and two keys that
            # redact to the same text would collapse into one, dropping a field
            # with nothing to show for it. A credential shape in a key is
            # therefore refused, not redacted: these documents' keys are the
            # harness's own field names, so a match means the document is not
            # the shape this scrub was pointed at.
            if isinstance(name, str) and redact_text(name) != name:
                raise RedactError(
                    f"the object key {name!r} carries a credential shape. "
                    "A key names a field the consumer reads, so redacting it would "
                    "change the document's schema. Refusing to rewrite it."
                )
            out[name], child_changed = _redact_json_value(child, key=name)
            changed = changed or child_changed
        return out, changed
    if isinstance(value, list):
        results = [_redact_json_value(item) for item in value]
        return [item for item, _ in results], any(flag for _, flag in results)
    # int, float, bool and None carry no text to redact, and a number that merely
    # LOOKS like an account id is still a number: rewriting it to a bare token is
    # how the document stops parsing.
    return value, False


def redact_json_text(text: str) -> tuple[str, bool]:
    """Redact a JSON document, guaranteeing the result is still JSON.

    The guarantee comes from the round trip: the redaction happens on decoded
    Python strings and the output is produced by ``json.dumps``, so every quote,
    comma and control character in the redacted text is re-escaped rather than
    left to collide with the document's own syntax.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RedactError(f"the file is not valid JSON, so it cannot be redacted as JSON: {exc}")
    redacted, changed = _redact_json_value(document)
    return json.dumps(redacted, indent=2, ensure_ascii=False) + "\n", changed


def redact_file(path: Path, mode: str) -> bool:
    """Rewrite one file in place. Returns whether anything changed."""
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RedactError(f"{path} could not be read: {exc}")
    except UnicodeDecodeError as exc:
        raise RedactError(f"{path} is not UTF-8 text, so it cannot be redacted: {exc}")
    if mode == "json":
        rewritten, changed = redact_json_text(original)
    else:
        rewritten = redact_text(original)
        changed = rewritten != original
    if rewritten != original:
        try:
            path.write_text(rewritten, encoding="utf-8")
        except OSError as exc:
            raise RedactError(f"{path} could not be rewritten: {exc}")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scope_redact",
        description="Redact credential shapes from a file before it is published.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("json", "text"),
        help="json: parse and redact string values only, output stays valid JSON. "
        "text: line-oriented redaction for logs and prose.",
    )
    parser.add_argument(
        "--fail-if-changed",
        action="store_true",
        help="exit 10 when at least one file was redacted, for a caller that "
        "withholds a redacted file rather than publishing it.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="files to rewrite in place")
    parsed = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    any_changed = False
    for path in parsed.paths:
        try:
            changed = redact_file(path, parsed.mode)
        except RedactError as exc:
            # An unredacted file is the one outcome that must never read as
            # success: the caller's next step publishes it.
            print(f"scope_redact: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 -- the breadth IS the rule, see below
            # A defect in this script leaves the file unscrubbed exactly as a
            # read error does, and the caller can act on only one of those two
            # answers. So every unexpected failure ends as exit 1 rather than as
            # a traceback whose exit code a caller could read as "nothing to do".
            print(
                f"scope_redact: internal error, {path} was not redacted: {exc!r}",
                file=sys.stderr,
            )
            return 1
        any_changed = any_changed or changed
        print(f"scope_redact: {path} mode={parsed.mode} changed={'yes' if changed else 'no'}")
    print(f"scope_redact: changed={'yes' if any_changed else 'no'}")
    if parsed.fail_if_changed and any_changed:
        return 10
    return 0


if __name__ == "__main__":
    sys.exit(main())

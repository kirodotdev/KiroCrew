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

``--drop-changed-fields`` (a modifier on ``--mode json``, not a third mode)
    For a golden-paths corpus -- the candidate set the scope lane classifies --
    redaction is not available on the fields the classifier reads. ``deny_diff``
    selects rows by ``kind`` and ``platform`` and classifies ``command_or_flow``
    VERBATIM at the base ref and at the head ref, so a placeholder standing where a
    command belongs is a command nobody ever refused, which classifies as allowed.
    That is the false green in its least visible shape.

    So this flag splits the corpus by what the classifier reads:

    *A CLASSIFIED field carrying a credential shape is REFUSED*, with exit 11. It
    cannot be rewritten without changing what was measured, and it cannot be dropped
    either -- dropping the row would silently remove the boundary that row probes,
    and the rows are model-authored out of the diff under review, so a dropped row is
    a coverage hole an author can steer. Refusing is the only answer that neither
    measures the wrong thing nor quietly measures less.

    *An UNCLASSIFIED field carrying one is REMOVED*, field and all. ``reason`` is
    prose explaining who runs the operation and when; the classifier never reads it
    (``deny_diff`` takes it with ``entry.get("reason", "")`` and every renderer
    guards on it), so removing it costs that row's EXPLANATION and nothing else. The
    row is still classified, at both refs, on the bytes the reviewer wrote. No
    coverage is lost, so there is nothing here to steer.

    REMOVED rather than redacted, deliberately. A ``[REDACTED-`` marker written into
    a corpus would be read by the publish path's own grep, which withholds a
    confirmed row on finding one -- so rewriting the prose would reintroduce a block
    through a different door, on exactly the runs where the lane has a real finding.

    A change OUTSIDE the rows is refused too: a corpus file's content is its rows, so
    a credential shape anywhere else means the flag was pointed at a document it does
    not describe, and guessing which of those bytes are load-bearing is not something
    a scrub may do. The flag is refused outright with ``--mode text``, which has no
    rows to reason about.

Callers branch on whether anything CHANGED -- a redacted row names a placeholder
instead of the operation that was refused, so the lane withholds it rather than
passing that off as a finding -- so the answer is reported two ways: a
``changed=`` line per file on stdout, and ``--fail-if-changed`` for a caller that
wants the branch as an exit code.

Exit codes: ``0`` every file was rewritten (whether or not anything changed),
``1`` a file could not be redacted, ``10`` with ``--fail-if-changed`` and at
least one file changed, ``11`` with ``--drop-changed-fields`` and a field the
classifier READS carries a credential shape. ``1`` and ``10`` are distinct because
they demand opposite things of the caller: ``1`` means the text is UNSCRUBBED and
must not be published at all, and ``10`` means it was scrubbed successfully and the
caller must decide whether a redacted artifact is still worth publishing. ``11`` is
distinct from both because the file is UNCHANGED and unpublishable for a reason no
rewrite can fix, which is a fail-closed answer with its own sentence to say.

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


class ClassifiedFieldShaped(RedactError):
    """A field the classifier READS carries a credential shape.

    A subclass of :class:`RedactError` so a caller that only knows the base class
    still fails closed on it, and a distinct type so the caller that DOES know it
    can say the true thing: the file is unchanged and no rewrite can make it
    publishable, because the bytes at fault are the bytes being measured.
    """


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


#: The row fields whose BYTES the classifier reads. ``deny_diff`` selects rows by
#: ``kind`` and ``platform`` and hands ``command_or_flow`` to the deny composite
#: verbatim at both refs, so a placeholder in any of the three changes what was
#: measured rather than merely how it reads. Anything not in this tuple is
#: explanation: the classifier takes ``reason`` with ``entry.get("reason", "")`` and
#: never classifies it, and an extra field the reviewer invents reaches nothing at
#: all.
_CLASSIFIED_ROW_FIELDS = ("kind", "command_or_flow", "platform")

#: Every row field ``deny_diff._row`` reads, classified or not. Held here so the
#: split above can be PINNED against that loader instead of agreeing with it by
#: hand: ``test/test_scope_redact_classified_pin.py`` walks the loader's source and
#: reddens when it reads a field neither tuple names. ``reason`` is read but only
#: carried into ``Row`` for rendering, so no verdict depends on it.
#:
#: Why a pin and not a fail-closed branch: an unrecognized field carrying a live
#: secret MUST still be scrubbed from a world-readable artifact, and refusing the
#: run over it is the bug this change fixes. So drift cannot be caught at runtime
#: here -- it is caught in CI, before a fourth classified field ever ships.
_DENY_DIFF_ROW_FIELDS = ("kind", "command_or_flow", "platform", "reason")


def _would_change(value: Any) -> bool:
    """Whether the redaction above would change anything in *value*.

    Asked by running the ordinary JSON walk and reading only WHETHER it changed;
    the rewritten value is thrown away. Reusing the walk rather than re-testing the
    regexes is the point: the vocabulary stays in one place, and a rule added to
    :func:`redact_text` reaches this decision with no second edit.

    A credential shape in a KEY raises out of the walk, and for a field that is the
    same answer as a shaped value: this object is not the shape the scrub
    describes, so the field goes.
    """
    try:
        _, changed = _redact_json_value(value)
    except RedactError:
        return True
    return changed


def drop_changed_fields(text: str) -> tuple[str, int]:
    """Remove the UNCLASSIFIED corpus fields a redaction would change.

    Returns the document and how many fields were removed. A classified field
    carrying a shape raises :class:`ClassifiedFieldShaped` BEFORE anything is
    written, so the caller inherits the file exactly as it arrived.

    Accepts the wrapper object and a bare list, matching ``deny_diff.load_corpus``
    so a candidate file and the committed corpus are the same shape here too.

    No ROW is ever removed. A row is the boundary probe the lane exists to
    classify, and the rows are model-authored out of the diff under review -- so
    removing one silently narrows what the lane measured, on input the author of
    that diff influences. Every row that arrives is classified.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RedactError(f"the file is not valid JSON, so its rows cannot be read: {exc}")

    if isinstance(document, dict):
        if "golden_paths" not in document:
            raise RedactError(
                "the file is an object without a 'golden_paths' key, so it is not the "
                "corpus shape --drop-changed-fields describes. Refusing to guess which "
                "of its bytes the classifier reads."
            )
        rows = document["golden_paths"]
        beside = {name: value for name, value in document.items() if name != "golden_paths"}
    elif isinstance(document, list):
        rows = document
        beside = {}
    else:
        raise RedactError(
            f"a corpus must be a list of rows or an object wrapping one, got "
            f"{type(document).__name__}"
        )

    if not isinstance(rows, list):
        raise RedactError(f"'golden_paths' must hold a list of rows, got {type(rows).__name__}")

    # Anything beside the rows is refused, not removed and not rewritten. It belongs
    # to no row, so nothing here says whether the classifier reads it, and rewriting
    # it would put a marker into a file whose whole contract is that the measured
    # bytes are untouched. `_redact_json_value` also raises on a credential shape in
    # a KEY, which is the same answer for the same reason.
    if beside and _would_change(beside):
        raise RedactError(
            "a credential shape sits outside the corpus rows. A corpus's content is "
            "its rows, so nothing here can say whether the classifier reads this text. "
            "Refusing it."
        )

    kept_rows: list[Any] = []
    removed = 0
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            # Not a row shape, so it has no classified field to protect and no field
            # to remove. Left exactly as it is: the validator rejects it by position
            # with a message a reader can act on, and inventing a second refusal here
            # would only disagree with that one.
            kept_rows.append(row)
            continue
        for field in _CLASSIFIED_ROW_FIELDS:
            if field in row and _would_change(row[field]):
                raise ClassifiedFieldShaped(
                    f"row {position} carries a credential shape in {field!r}, which the "
                    "classifier reads verbatim at both refs. It cannot be rewritten "
                    "without measuring an operation nobody proposed, and it cannot be "
                    "removed without silently dropping the boundary this row probes."
                )
        pruned = {}
        for name, value in row.items():
            if name in _CLASSIFIED_ROW_FIELDS or not _would_change({name: value}):
                pruned[name] = value
            else:
                removed += 1
        kept_rows.append(pruned)

    if isinstance(document, dict):
        out: Any = dict(document)
        out["golden_paths"] = kept_rows
    else:
        out = kept_rows
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n", removed


def redact_file(path: Path, mode: str, *, drop_changed_fields_only: bool = False) -> bool:
    """Rewrite one file in place. Returns whether anything changed."""
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RedactError(f"{path} could not be read: {exc}")
    except UnicodeDecodeError as exc:
        raise RedactError(f"{path} is not UTF-8 text, so it cannot be redacted: {exc}")
    removed = 0
    if drop_changed_fields_only:
        rewritten, removed = drop_changed_fields(original)
        # A re-serialization that only moved whitespace is NOT a change: every
        # remaining field holds the bytes it arrived with, and the consumer parses
        # JSON. Only a removed field changes what anybody reads.
        changed = removed > 0
    elif mode == "json":
        rewritten, changed = redact_json_text(original)
    else:
        rewritten = redact_text(original)
        changed = rewritten != original
    if rewritten != original:
        try:
            path.write_text(rewritten, encoding="utf-8")
        except OSError as exc:
            raise RedactError(f"{path} could not be rewritten: {exc}")
    if drop_changed_fields_only:
        print(
            f"scope_redact: {path} removed={removed} unclassified field(s) "
            "carrying a credential shape"
        )
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
        "--drop-changed-fields",
        action="store_true",
        help="treat the document as a golden-paths corpus: REMOVE any unclassified "
        "field a redaction would change, keep every row, and exit 11 when a field "
        "the classifier reads carries a credential shape. Requires --mode json.",
    )
    parser.add_argument(
        "--fail-if-changed",
        action="store_true",
        help="exit 10 when at least one file was redacted, for a caller that "
        "withholds a redacted file rather than publishing it.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="files to rewrite in place")
    parsed = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if parsed.drop_changed_fields and parsed.mode != "json":
        # Argparse's own exit 2, not a RedactError: nothing was read, so no file is
        # left unscrubbed and there is nothing to report per path. A corpus is a
        # parsed document by definition -- `--mode text` has no fields to tell apart,
        # and accepting the pair would line-redact the very commands the flag exists
        # to keep byte-faithful.
        parser.error("--drop-changed-fields needs --mode json: a corpus is read as JSON rows.")

    any_changed = False
    for path in parsed.paths:
        try:
            changed = redact_file(
                path, parsed.mode, drop_changed_fields_only=parsed.drop_changed_fields
            )
        except ClassifiedFieldShaped as exc:
            # Its own code, BEFORE the base class: the file is UNCHANGED and no
            # rewrite can make it publishable. Reported as a distinct answer because a
            # caller that reads it as "unscrubbed" sends a reader looking for a leak in
            # this program, and one that reads it as success classifies a corpus whose
            # measured bytes nobody could publish.
            print(f"scope_redact: {exc}", file=sys.stderr)
            return 11
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

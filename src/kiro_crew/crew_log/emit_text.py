"""What an entry may carry: redacted text, clipped fields and bodies that fit a line.

:func:`_safe_text` and :func:`safe_work_fields` redact what an agent or a board wrote before it
reaches a line, and :func:`_clip` bounds a short field. :func:`_fits_one_line` and
:func:`_entry_line_fits` measure an entry against the store's one-line ceiling,
:func:`_text_slices` cuts a body into pieces that fit, and :func:`_append_body_entry` writes a
body as one entry or as ``message/chunk`` slices. :func:`_payload_digest` names a tool payload
by its hash and size.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log.emit_runtime import _report
from kiro_crew.crew_log.emit_state import _SRC_ACP, _crew_log
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover -- typing only; the runtime imports stay gated
    # Type-only, so the boot-path import gate is untouched: this name exists for the
    # checker and for test_crew_log_exc_info_sites.py, which reads annotations to decide
    # whether a frame can hold a handle. A handle passed in as ``Any`` is invisible to it.
    from kiro_crew.crew_log.schema import Entry
    from kiro_crew.crew_log.store import CrewLog


#: Where a ``message/chunk`` slice STARTS when a body is too big for a single
#: line. Not a guarantee: :func:`_text_slices` measures each piece and halves it
#: until it fits, because ``ensure_ascii`` escapes per code UNIT -- six bytes for a
#: BMP character, twelve for a surrogate pair -- so no character count can be
#: turned into a byte budget in advance.
_CHUNK_TEXT_CHARS = 8 * 1024

#: Bytes reserved on a line for everything that is not the body: the envelope,
#: the turn and step ordinals, a cited chunk list. Generous on purpose -- an
#: append refused for being one byte over is a body silently missing from the
#: log, and the cost of over-reserving is one extra chunk.
_ENVELOPE_HEADROOM = 4 * 1024


#: Ceiling for a SHORT field -- an approval's shown reason, a plan item's text, a
#: child's failure reason. These are not bodies: a body goes through
#: :func:`_append_body_entry`, which slices an oversize one into ``message/chunk``
#: entries so nothing is lost. A short field has no such path, so the choice is
#: between clipping it and letting one long value push the whole entry past
#: ``MAX_ENTRY_BYTES`` -- where the append is REFUSED and the fact disappears with
#: it. Clipping loses a tail; refusing loses the record.
_MAX_SHORT_TEXT = 512

#: Ceiling for an identifier the agent chose (a plan item's id). Short because a
#: value longer than this is not an identifier.
_MAX_ID_TEXT = 64

#: How many plan items one ``plan/updated`` entry carries. The agent re-sends its
#: whole list on every change, so a long plan is re-serialized on each update; the
#: entry keeps the real count in ``total`` when it clips.
_MAX_PLAN_ITEMS = 100


def _safe_text(text: Any) -> str:
    """*text* with secrets removed, or ``""`` when redaction itself failed.

    The same two helpers the transcript store applies before it persists a
    message, in the same order -- exfiltration URLs, then credentials -- so the log
    and the transcript cannot disagree about what a body is allowed to contain.

    Redaction happens HERE rather than being trusted from the call site. Some
    sites hand over text that is already clean (the assistant flush, a streamed
    delta) and some hand over raw input a person just typed; a rule enforced at
    the boundary cannot be forgotten by the next site that is added.

    A redaction failure yields the empty string, never the input. Failing closed
    is the only safe direction: this module's promise is that ``data`` carries no
    sensitive text, whether it came from a message body or a work record.
    """
    if not isinstance(text, str) or not text:
        return ""
    try:
        cleaned, _ = redact_exfiltration_urls(text)
        cleaned, _ = redact_credentials(cleaned)
        return cleaned
    except Exception as exc:
        _report("redacting a body", exc, op="redact-body")
        return ""


_WORK_PLAIN_TEXT_FIELDS = frozenset({"title", "goal", "decision", "summary", "event"})

_WORK_NESTED_TEXT_FIELDS = frozenset({"acceptance", "artifacts"})


class WorkFieldError(ValueError):
    """A work mapping cannot be redacted safely. Answered as a validation refusal."""


class WorkFieldCollisionError(WorkFieldError):
    """A nested work mapping cannot be redacted without losing a key."""


class WorkFieldTooDeepError(WorkFieldError):
    """A nested work mapping is deeper than the redaction walk follows."""


# A refusal and its traceback name the facade, ``kiro_crew.crew_log.emit``, as
# these classes' module: that is the import path callers catch them by.
WorkFieldError.__module__ = "kiro_crew.crew_log.emit"
WorkFieldCollisionError.__module__ = "kiro_crew.crew_log.emit"
WorkFieldTooDeepError.__module__ = "kiro_crew.crew_log.emit"


# Far above anything a board writes: the deepest shape these fields take is a
# mapping of mappings, so this refuses no real acceptance or artifact map.
_WORK_MAX_NESTING = 32


def _safe_work_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact string keys and values while preserving field shape.

    The walk recurses once per nesting level and the caller's own JSON decides how
    many there are, so the depth is bounded: unbounded, a deeply nested body
    exhausts the stack and the ``RecursionError`` escapes the route as a 500.
    Refusing at this boundary makes it the same 400 every other unusable field
    gets, and it happens before any lock is taken or byte committed.
    """
    if depth > _WORK_MAX_NESTING:
        raise WorkFieldTooDeepError(
            f"work field nesting is deeper than {_WORK_MAX_NESTING} levels; flatten it"
        )
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [_safe_work_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = _safe_text(key) if isinstance(key, str) else key
            if safe_key in cleaned:
                raise WorkFieldCollisionError("work field keys collide after redaction")
            cleaned[safe_key] = _safe_work_value(item, depth + 1)
        return cleaned
    return value


def safe_work_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Copy work-ledger input with every caller-controlled text value redacted.

    The routes apply this copy before either their fit probe or cache commit, so
    the cache and its ``work/recorded`` entry receive byte-identical values.
    ``title``, ``goal``, ``decision`` and ``summary`` are direct prose. Event text
    is derived from those cleaned fields (or a fixed vocabulary), but is included
    so a future direct producer cannot bypass the boundary. ``acceptance`` and
    artifact mappings may contain nested prose, paths or URLs in both their keys
    and values, so every string at that boundary is walked. A post-redaction key
    collision is refused rather than silently discarding one pointer. Identity
    fields and constrained vocabularies are deliberately unchanged.
    """
    cleaned = dict(fields)
    for name in _WORK_PLAIN_TEXT_FIELDS:
        if name in cleaned:
            cleaned[name] = _safe_text(cleaned[name])
    for name in _WORK_NESTED_TEXT_FIELDS:
        if name in cleaned:
            cleaned[name] = _safe_work_value(cleaned[name])
    return cleaned


def _clip(text: str, limit: int) -> str:
    """*text* bounded to *limit* characters, marked when it was cut.

    For SHORT fields only -- see :data:`_MAX_SHORT_TEXT` for why they are clipped
    rather than sliced. The ellipsis is part of the value on purpose: a reader must
    be able to tell a value that ends here from one that was cut, because the two
    support different conclusions and nothing else in the entry says which it is.

    Characters, not bytes. The byte ceiling is enforced by the append itself; this
    bound exists to keep one field from dominating a line, and a character count is
    what a call site can reason about.
    """
    if not text or limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "\u2026"


def _text_slices(text: str) -> list[str]:
    """*text* cut into pieces that each MEASURE small enough for one crew log line.

    Measured, not assumed. A character-count budget cannot be derived from the
    byte cap, because ``ensure_ascii`` escapes by code UNIT: a BMP character costs
    six bytes as ``\\uXXXX``, and one outside the BMP costs twelve as a surrogate
    pair. A slice sized for the six-byte case is refused for a body of emoji, and
    a refused chunk aborts the whole split -- so the body is lost entirely, not
    merely cut badly. So each piece starts at :data:`_CHUNK_TEXT_CHARS` and halves
    until it actually fits, which terminates because a single character always
    does.
    """
    slices: list[str] = []
    at = 0
    total = len(text)
    while at < total:
        take = min(_CHUNK_TEXT_CHARS, total - at)
        while take > 1 and not _fits_one_line(text[at : at + take]):
            take //= 2
        slices.append(text[at : at + take])
        at += take
    return slices


def _fits_one_line(text: str, extra: "dict[str, Any] | None" = None) -> bool:
    """Whether *text* can ride on a single entry, envelope included.

    Measured on the ESCAPED form, because that is what goes on the line: the
    crew log serializes with ``ensure_ascii``, so a non-ASCII character costs six
    bytes and a check against the raw length would pass a body the append then
    refuses. :data:`_ENVELOPE_HEADROOM` is left for the rest of the entry.

    *extra* is measured too, because it rides on the SAME line. The headroom is a
    fixed allowance for the envelope's own keys, not a slack fund for caller
    fields: a message carrying many attachment ids can exceed it on its own, and
    then a body that this said would fit is refused by the append and the whole
    message is dropped and counted -- the one outcome chunking exists to avoid.
    Measuring it here moves that message onto the chunked path instead.
    """
    escaped = len(json.dumps(text, ensure_ascii=True).encode("utf-8"))
    if extra:
        escaped += len(json.dumps(extra, ensure_ascii=True, default=str).encode("utf-8"))
    return escaped + _ENVELOPE_HEADROOM <= _crew_log().MAX_ENTRY_BYTES


#: How a message body is represented in ``data``. ``text`` puts the redacted body
#: IN the log, which is the decided behaviour: FR-7 was amended in this change to
#: include bodies, because they are redacted before they are written. ``ref`` names
#: the migration shape -- a pointer at a transcript position -- which the dual-write
#: bridge adds alongside the body, NOT instead of it. The mode stays a single name
#: read in one place (:func:`_append_body_entry`) so the bridge is a change there
#: and nowhere else.
BODY_MODE_TEXT = "text"

BODY_MODE_REF = "ref"

BODY_MODE = BODY_MODE_TEXT


def _entry_line_fits(entry_type: str, data: dict[str, Any], *, src: str) -> bool:
    """Whether *data* fits its complete serialized crew log entry.

    Measured through the STORE's own serializer rather than a local ``json.dumps``
    with the same options: this decides whether an entry is written whole or split,
    and the writer refuses the line by its serialized length, so a second spelling
    of the encoding is a second answer waiting to disagree with the first. Only the
    two counters are guessed, at their widest, because the writer assigns them under
    its lock after this decision is made -- a fit that depends on a small seq would
    stop fitting on a long-lived log.
    """
    counter_ceiling = (2**63) - 1
    envelope = {
        "type": entry_type,
        "seq": counter_ceiling,
        "time": counter_ceiling,
        "src": src,
        "data": data,
    }
    try:
        # The schema module by its own import, and deliberately NOT at module
        # scope: the package front exports names, not submodules, so reaching it
        # as an attribute worked only after some earlier import had loaded it --
        # and a module-scope import here would load the schema on every flag-off
        # launch, which
        # ``test_crew_log_emit_redaction_and_flag.py::test_a_flag_off_launch_does_not_load_the_storage_subsystem``
        # pins against ("the store, schema and lease stay unloaded until a call
        # reaches storage"). The ``top-level-imports`` convention is advisory;
        # that boot-path invariant is enforced, so the invariant wins.
        from kiro_crew.crew_log import schema as crew_log_schema

        line = crew_log_schema.serialize(envelope)
    except _crew_log().CrewLogError:
        # Not serializable at all. The append will refuse it for the same reason,
        # with the code that names it, so this reports "does not fit" rather than
        # deciding the outcome here.
        return False
    return len(line.encode("utf-8")) <= _crew_log().MAX_ENTRY_BYTES


def _bounded_attachment_data(
    entry_type: str,
    data: dict[str, Any],
    *,
    src: str,
    inline_text: str | None = None,
) -> dict[str, Any]:
    """Keep the longest attachment prefix that fits the entry."""
    attachments = data.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return data

    def _candidate(kept: int) -> dict[str, Any]:
        candidate = dict(data)
        if kept:
            candidate["attachments"] = attachments[:kept]
        else:
            candidate.pop("attachments", None)
        omitted = len(attachments) - kept
        if omitted:
            candidate["attachments_omitted"] = omitted
        else:
            candidate.pop("attachments_omitted", None)
        return candidate

    def _fits(candidate: dict[str, Any]) -> bool:
        if not _entry_line_fits(entry_type, candidate, src=src):
            return False
        if inline_text is None:
            return True
        extra = {
            key: value for key, value in candidate.items() if key not in {"turn", "step", "text"}
        }
        return _fits_one_line(inline_text, extra)

    if _fits(data):
        return data
    low, high = 0, len(attachments) - 1
    best = _candidate(0)
    while low <= high:
        middle = (low + high) // 2
        candidate = _candidate(middle)
        if _fits(candidate):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _append_body_entry(
    log: CrewLog,
    entry_type: str,
    turn: int,
    *,
    step: int = 0,
    text: str,
    extra: "dict[str, Any] | None" = None,
    src: str = _SRC_ACP,
) -> "Entry | None":
    """Append one body-bearing entry. THE single place a body becomes fields.

    Returns the body-bearing entry it wrote -- the citing one when the body was split
    into chunks -- so the caller can wake the eager folder for it, or ``None``.

    Every family that carries a message body goes through here, so the shape of a
    body is decided once. Under :data:`BODY_MODE_TEXT` the redacted text rides on
    the entry, split across ``message/chunk`` entries the citing entry names in
    ``chunks`` when it cannot fit one line. Under :data:`BODY_MODE_REF` the entry would
    ALSO carry a pointer at the transcript position, for the bridge period in which
    both records exist and have to be reconcilable; the body itself stays either
    way.

    Runs on the writer thread with the crew log handle in hand, because the chunks
    must be on disk before the entry citing them can name their seqs.
    """
    data: dict[str, Any] = {"turn": int(turn)}
    if step:
        data["step"] = int(step)
    if extra:
        data.update(extra)
    inline_data = _bounded_attachment_data(
        entry_type, {**data, "text": text}, src=src, inline_text=text
    )
    inline_extra = {
        key: value for key, value in inline_data.items() if key not in {"turn", "step", "text"}
    }
    if _fits_one_line(text, inline_extra or None):
        return log.append(entry_type, inline_data, src=src)
    # A chunk group is written as ONE batch, not entry by entry. The chunks are
    # meaningless without the entry that cites their seqs: appending them
    # separately leaves a window in which a hard kill puts the body on disk with
    # nothing pointing at it -- stored and unreachable, and the entry that would
    # have explained it never written. One write leaves either the whole group or a
    # torn tail, and the tail is what the next append truncates.
    slices = _text_slices(text)
    group: list[dict[str, Any]] = [
        {
            "type": "message/chunk",
            "data": {"turn": int(turn), **({"step": int(step)} if step else {}), "delta": piece},
            "ignorable": True,
        }
        for piece in slices
    ]

    def _cite(seqs: list[int]) -> dict[str, Any]:
        """Build the citing entry from the seqs the group was actually allocated.

        Called inside the store's lock, which is what makes the citation unable to
        disagree with the allocation. Reading the tail here and citing the result
        would not: the lock is cross-process, so another handle can append between
        that read and the write, shifting the run so the ``chunks`` name entries
        belonging to the intruder -- seqs that exist and parse, so nothing later
        detects it.
        """
        citing_data = _bounded_attachment_data(
            entry_type,
            {**data, "chunks": list(seqs), "chars": len(text)},
            src=src,
        )
        return {"type": entry_type, "data": citing_data}

    written = log.append_many(group, src=src, cite=_cite)
    # The citing entry is the group's last member.
    return written[-1] if written else None


def _payload_digest(payload: str) -> tuple[str, int]:
    """``(sha256, byte length)`` for *payload*, or ``("", -1)`` when there is none.

    A digest and a size, never the bytes. That is the whole point: the log can say
    two calls had the SAME arguments, or that a result was enormous, without the
    log becoming a place secrets and file contents accumulate. ``-1`` distinguishes
    "not recorded" from a genuinely empty payload, which is 0.
    """
    if not payload:
        return "", -1
    raw = payload.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest(), len(raw)

"""The agent's half of a dynamic dashboard: what it may write, and what it is told.

A dashboard field comes from one of two places, and the template's manifest says
which. A ``{"fold", "path"}`` field is read out of the crew log and no agent can
touch it. An ``{"agentic": true}`` field is one the crewmate writes itself, and
this module is the whole of that path: it checks a write against the live
manifest, records the accepted ones as crew-log entries, and records the REFUSED
ones too.

Why a refusal is recorded
-------------------------
A good error teaches exactly once. An agent that reaches a field name the manifest
does not declare is told so, with the names it could have used -- and on its next
cycle, from a fresh context, it reaches the same wrong name again and pays the same
round trip, because nothing on the gateway remembers that the guess was already
answered. So every refusal lands as a ``dashboard/agentic_refused`` entry, the
``mistakes`` fold groups them by ``(code, field)``, and :func:`fields_for_agent`
hands that book back BEFORE the next write. An accepted write that used a name an
earlier refusal had reached wrongly appends the correction, so the book has answers
in it rather than only errors.

Why the type check is here and not in the fold
----------------------------------------------
It is in BOTH, and the two are answering different questions. Here the question is
"may this write land", asked while the caller is present and can fix it, against
the manifest the instance is actually running. In the fold the question is "can
these bytes be trusted", asked on read against a line off disk that no writer
controls. A check only here would let a planted line put a string in a cell a chart
reads as a number; a check only in the fold would silently drop a write the agent
was told had succeeded.

What this module does NOT do
----------------------------
It computes no dashboard value. That is part 1's rule -- "no provider: no host
Python computes values" -- and it is what keeps a user-supplied template safe to
load at run time: the gateway never runs agent-authored fold code and never
evaluates an agent-authored expression. Every number on a page is either folded
from the log or written through here as a literal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence, cast

from kiro_crew.crew_log.entry_types import (
    DASHBOARD_AGENTIC_ENTRY_TYPE,
    DASHBOARD_REFUSED_ENTRY_TYPE,
    DASHBOARD_VALUE_BYTES,
    MISTAKE_CORRECTED_CODE,
    MISTAKES_FOLD_NAME,
)
from kiro_crew.dashboard_templates.manifest import FIELD_TYPES, FieldSpec, Shape, TemplateManifest
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: How many times an agent should retry a refused write before asking a human.
#:
#: ONE CONSTANT, which is what the contract asks for, and it lives here because
#: this is the module that produces the refusal the retry answers. The VALUE is
#: not decided: 3 is a placeholder with a reason rather than a ruling -- a
#: refusal names the valid fields, so the first retry is usually the last, and a
#: second and third cover a wrong type and a wrong shape found in turn. It is
#: quoted into the refusal text and the tool description from here, so changing it
#: is one edit.
AGENTIC_RETRY_BUDGET: Final[int] = 3

#: Recent mistake groups handed to an agent before it writes.
#:
#: Short on purpose. The reader is an agent with a budget, and a long list is one it
#: skims: the fold already orders by count, so the first few ARE the mistakes most
#: worth not repeating. The rest stay readable through the fold itself.
MISTAKES_SHOWN: Final[int] = 5

#: History rows handed to an agent with its fields, newest last.
#:
#: Short for the reason the mistake list is, and short ENOUGH that the version history
#: rides along on the read an agent already makes before it writes -- which is why
#: there is no second tool for it. Ten rows reach back past the last few changes a
#: person is likely to mean by "go back", and the versions a rollback can actually
#: reach are listed separately because the store retains fewer payloads than the fold
#: retains rows.
HISTORY_SHOWN: Final[int] = 10

#: How many levels of nested array or object one agentic value may carry.
#:
#: A dashboard field is a cell -- a count, a phrase, a short series a chart draws
#: -- so eight levels is deeper than a row, a table of rows, or a list of grouped
#: rows needs, and it is the same bound the agent panel puts on its own data. It
#: is also two orders of magnitude under Python's recursion limit, which is what
#: the bound is for: the byte cap does not bound depth, because an opening bracket
#: costs one byte and a value inside the cap can nest thousands deep. Every walk
#: of a stored value -- the fold's deepcopy, the redaction pass below -- recurses
#: once per level, so an unbounded depth is an unbounded stack.
AGENTIC_VALUE_DEPTH: Final[int] = 8


class WriteRefused(ValueError):
    """An agentic write the host refused.

    ``code`` is the machine-readable reason the mistake book groups on, ``field``
    is what the write named, and the message is the sentence the agent reads. All
    three travel together because all three are recorded: a refusal whose code and
    sentence could drift apart would group one mistake under two names.
    """

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True)
class Instance:
    """The crewmate's own dashboard, as this module needs to see it.

    A NARROW view of worker C's instance: the manifest to check against, the
    version to stamp a write with, and the template's identity for the message. It
    is a dataclass rather than the raw route payload so the type checker can see
    what this module depends on -- and so the parts of C's payload this module must
    NOT reach (the html, the state) are absent by construction rather than by
    nobody having used them.
    """

    manifest: TemplateManifest
    instance_version: int


def agentic_fields(manifest: TemplateManifest) -> dict[str, FieldSpec]:
    """The fields the crewmate may write, by name."""
    return {name: spec for name, spec in manifest.fields.items() if spec.agentic}


def type_holds(declared: str, value: Any) -> bool:
    """Whether *value* is of the manifest type *declared*.

    THE SAME RULE as ``projection._dashboard_type_holds``, deliberately duplicated:
    the fold cannot import this module and this module cannot live in
    ``projection`` (the manifest imports the fold names, so the cycle closes the
    other way). ``test_dashboard_agentic`` pins the two against one table of cases.

    A bool is NOT a number. Python says ``isinstance(True, int)``, so without the
    exclusion a crewmate writing ``true`` into a ``number`` field would pass and the
    page would render ``True`` where a count belongs.
    """
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    return False


#: How many shape problems one refusal names. The first few are enough to fix the
#: write; a list of every bad row in a long array is one the reader skims.
SHAPE_PROBLEMS_SHOWN: Final[int] = 6


def _shape_line(shape: Shape) -> str:
    """One shape in a few words: ``string (decide|approve|do)``, ``array of string``."""
    if shape.enum:
        return f"{shape.type} ({'|'.join(str(v) for v in shape.enum)})"
    if shape.type == "array" and shape.items is not None:
        return f"array of {_shape_line(shape.items)}"
    if shape.type == "object" and shape.properties is not None:
        return "object {" + ", ".join(sorted(shape.properties)) + "}"
    return shape.type


def _valid_keys(shape: Shape) -> str:
    """The keys an object may carry, each with its shape, required ones marked."""
    props = shape.properties or {}
    return ", ".join(
        f"{key}{' (required)' if key in shape.required else ''}: {_shape_line(sub)}"
        for key, sub in sorted(props.items())
    )


def shape_problems(shape: Shape, value: Any, where: str, out: list[str]) -> None:
    """Append to *out* every way *value* breaks *shape*, up to the shown cap.

    Each problem names WHERE (``for_you[0]``) and what is valid there, so the
    sentence is the fix: a refused key comes with the keys the page reads.
    """
    if len(out) >= SHAPE_PROBLEMS_SHOWN:
        return
    if not type_holds(shape.type, value):
        out.append(f"{where} wants {_shape_line(shape)}, and this is {_describe(value)}")
        return
    if shape.enum and value not in shape.enum:
        out.append(f"{where} is {value!r}, not one of " + "|".join(str(v) for v in shape.enum))
        return
    if shape.type == "array" and shape.items is not None:
        for index, item in enumerate(value):
            shape_problems(shape.items, item, f"{where}[{index}]", out)
            if len(out) >= SHAPE_PROBLEMS_SHOWN:
                return
        return
    if shape.type != "object":
        return
    if shape.properties is not None:
        unknown = sorted(str(k) for k in value if k not in shape.properties)
        missing = [k for k in shape.required if k not in value]
        if unknown or missing:
            said = []
            if unknown:
                said.append("unknown key(s) " + ", ".join(repr(k) for k in unknown))
            if missing:
                said.append("missing required key(s) " + ", ".join(repr(k) for k in missing))
            out.append(f"{where} has " + " and ".join(said) + f"; valid keys: {_valid_keys(shape)}")
        for key, sub in shape.properties.items():
            if key in value:
                shape_problems(sub, value[key], f"{where}.{key}", out)
                if len(out) >= SHAPE_PROBLEMS_SHOWN:
                    return
        return
    if shape.values is not None:
        for key, item in value.items():
            shape_problems(shape.values, item, f"{where}[{key!r}]", out)
            if len(out) >= SHAPE_PROBLEMS_SHOWN:
                return


def _describe(value: Any) -> str:
    """What the agent actually sent, in the manifest's own vocabulary.

    Named in the SAME words the manifest uses, so the sentence "field wants number,
    you sent string" can be acted on without the agent translating from Python's
    type names. A value of no manifest type at all (``None``) is named for what it
    is rather than mapped to the nearest one.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def _value_bytes(value: Any) -> int:
    """The value's serialized size, or ``-1`` when it cannot be serialized."""
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return -1


def _deeper_than(value: Any, limit: int) -> bool:
    """Whether *value* nests more than *limit* levels of array or object.

    It stops at the limit instead of measuring the whole value, so it recurses at
    most ``limit + 1`` frames however deep the value goes. A check that walked to
    the bottom to report the real depth would be the thing it is there to prevent.
    """
    if not isinstance(value, (list, dict)):
        return False
    if limit <= 0:
        return True
    items = value.values() if isinstance(value, dict) else value
    return any(_deeper_than(item, limit - 1) for item in items)


def _redacted(value: Any) -> Any:
    """*value* with every string in it scrubbed of credentials and exfiltration URLs.

    An agentic value is drawn on a page as the agent wrote it, so an access key or
    a credential-bearing address in one is displayed verbatim. Strings are nested
    inside arrays and objects as often as they stand alone, so the scrub walks the
    whole value rather than only a top-level string.

    The two helpers run in the order every other caller uses them, URLs first: a
    credential inside a suspicious address goes with the address, so the second
    pass has nothing left to find there.

    An object's KEY is scrubbed beside its value, because a page draws a key as a
    label in the same characters it draws a value. Two keys that scrub to ONE
    placeholder are then suffixed rather than collapsed -- ``<redacted> (2)``, the
    same shape ``member_dashboard._page_safe`` gives a colliding fold key. The
    collapse was recorded here as an acceptable trade, and it is not: the keys may
    be unfit to show, but each one carries a VALUE the agent wrote and meant to
    store, and a two-row write that lands as one row loses a row with nothing
    raised and nothing in the mistake book. The label is already unreadable either
    way; the data does not have to be lost with it.

    The depth check runs before this, so the recursion here is bounded by
    :data:`AGENTIC_VALUE_DEPTH`.
    """
    if isinstance(value, str):
        cleaned, _ = redact_exfiltration_urls(value)
        cleaned, _ = redact_credentials(cleaned)
        return cleaned
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = _redacted(key)
            if safe_key in out:
                # SUFFIXED, not overwritten. Only reachable when two distinct keys
                # scrub to the same placeholder, which needs both to carry
                # credential or exfiltration-URL text -- rare, and exactly the case
                # where silently dropping one of the agent's rows is least
                # recoverable, because the label that would have identified the
                # loss is the part that was redacted.
                suffix = 2
                while f"{safe_key} ({suffix})" in out:
                    suffix += 1
                safe_key = f"{safe_key} ({suffix})"
            out[safe_key] = _redacted(item)
        return out
    return value


def check_write(
    instance: Instance | None,
    field: str,
    value: Any,
    mistakes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check one agentic write; return the entry payload or raise :class:`WriteRefused`.

    Every refusal names what to do instead, and names it from the MANIFEST rather
    than from prose: the valid field list is the instance's own, so a refusal cannot
    advise a field that does not exist. *mistakes* is the folded mistake book, and
    when a refusal matches a group already in it the sentence says so and quotes the
    correction that worked -- an agent repeating a mistake is told it is repeating
    one, which is the difference between an error and a lesson.

    The order of the checks is the order of the questions: is there a dashboard at
    all, does it declare this field, is the field the agent's to write, is the value
    the declared type, is it shallow enough to walk, and is it small enough to keep.
    Each answers before the next can be asked, so no refusal rests on a fact an
    earlier one has not established -- and the depth question comes before anything
    walks the value, because the walk is what an unbounded depth breaks.

    What lands is the REDACTED value. A credential or a suspicious address in
    agent-written text would otherwise reach the page as written, so the scrub runs
    on the way in and the type and size questions are asked again about its result.
    """
    if instance is None:
        raise WriteRefused(
            "no_instance",
            field,
            "this crewmate has no dashboard yet, so it has no field to write: adopt a "
            "template first, then write its agentic fields",
        )
    writable = agentic_fields(instance.manifest)
    if field not in instance.manifest.fields:
        raise WriteRefused(
            "unknown_field",
            field,
            f"{field!r} is not a field of template {instance.manifest.id!r} "
            f"(version {instance.manifest.version}). " + _advice(writable, mistakes, field),
        )
    spec = instance.manifest.fields[field]
    if not spec.agentic:
        raise WriteRefused(
            "field_not_agentic",
            field,
            f"{field!r} is read from the {spec.fold!r} fold at {spec.path!r}, so the host "
            "fills it and a write would be overwritten on the next fold. "
            + _advice(writable, mistakes, field),
        )
    if not type_holds(spec.type, value):
        raise WriteRefused(
            "wrong_type",
            field,
            f"{field!r} wants {spec.type}, and this value is {_describe(value)}. "
            + _repeat_note(mistakes, "wrong_type", field),
        )
    if _deeper_than(value, AGENTIC_VALUE_DEPTH):
        raise WriteRefused(
            "value_too_deep",
            field,
            f"this value for {field!r} nests arrays or objects more than "
            f"{AGENTIC_VALUE_DEPTH} levels deep -- flatten it to rows a page can draw, "
            "or send the one level the field shows and write the rest as its own field",
        )
    # AFTER the depth check, because this walks the value and the depth bound is
    # what keeps the walk's recursion bounded.
    if spec.shape is not None:
        problems: list[str] = []
        shape_problems(spec.shape, value, field, problems)
        if problems:
            raise WriteRefused(
                "wrong_shape",
                field,
                (
                    f"{field!r} does not match the shape template "
                    f"{instance.manifest.id!r} draws: "
                    + "; ".join(problems)
                    + ". "
                    + _repeat_note(mistakes, "wrong_shape", field)
                ).strip(),
            )
    size = _value_bytes(value)
    if size < 0:
        raise WriteRefused(
            "value_not_serializable",
            field,
            f"this value for {field!r} is not JSON (a NaN, an infinity, or an object "
            "with no JSON form), so nothing could store or draw it",
        )
    if size > DASHBOARD_VALUE_BYTES:
        raise WriteRefused(
            "value_too_large",
            field,
            f"this value for {field!r} is {size} bytes, over the "
            f"{DASHBOARD_VALUE_BYTES}-byte limit for one field -- send fewer rows, "
            "coarser buckets or fewer decimals",
        )
    # The STORED value is the scrubbed one, so the type and size checks are asked
    # again about it: a placeholder is longer than the secret it replaces, so a
    # value that fits before the scrub can be over the cap after it, and the cap
    # has to hold for the bytes that actually land in the log.
    cleaned = _redacted(value)
    cleaned_size = _value_bytes(cleaned)
    if not type_holds(spec.type, cleaned) or cleaned_size < 0:
        raise WriteRefused(
            "redacted_value_invalid",
            field,
            f"redacting credentials from this value for {field!r} leaves something that "
            f"is not a storable {spec.type}, so nothing could store or draw it -- send a "
            "value with no credential-like text in it",
        )
    if cleaned_size > DASHBOARD_VALUE_BYTES:
        raise WriteRefused(
            "redacted_value_too_large",
            field,
            f"this value for {field!r} carries credential-like text, and each piece is "
            f"replaced by a longer placeholder, which puts it at {cleaned_size} bytes "
            f"over the {DASHBOARD_VALUE_BYTES}-byte limit for one field -- send the "
            "value without the keys, tokens or signed URLs in it",
        )
    return {
        "field": field,
        "type": spec.type,
        # WRAPPED, because the crew-log entry declares ``value`` as an object: the
        # registry has no any-type and an undeclared field is refused by the append
        # validator, so an object whose members are unchecked is the only declared
        # shape that carries all five manifest types. See the entry type's note.
        "value": {"v": cleaned},
        "instance_version": instance.instance_version,
    }


def _advice(
    writable: Mapping[str, FieldSpec],
    mistakes: Mapping[str, Any] | None,
    field: str,
) -> str:
    """The "write this instead" half of a refusal.

    The field list comes from the manifest, so it cannot name a field that does not
    exist -- which is the failure a hand-written list produces, and the reason the
    contract says to take the valid names from the folds and the manifest rather
    than from any document.

    An EMPTY list is its own sentence. A template with no agentic field at all is
    not a mistake the agent can fix by picking a better name, and telling it to
    choose from nothing would send it round the retry budget for no reason.
    """
    repeat = _repeat_note(mistakes, "", field)
    if not writable:
        return (
            "This template has no agentic field, so no value here is the agent's to "
            "write; every field is read from a fold. " + repeat
        ).strip()
    names = ", ".join(f"{name} ({spec.type})" for name, spec in sorted(writable.items()))
    return (f"Agentic fields: {names}. " + repeat).strip()


def _repeat_note(mistakes: Mapping[str, Any] | None, code: str, field: str) -> str:
    """ "You have done this before" -- and what worked, when anything did.

    Quoted from the fold rather than recomputed, so the count a refusal reports is
    the count the mistake book holds: two numbers for one fact is how a reader stops
    trusting either. Silent when the book has nothing for this group, which is the
    first time a mistake is made and the one time the plain refusal is enough.
    """
    group = _mistake_group(mistakes, code, field)
    if group is None:
        return ""
    count = int(group.get("count") or 0)
    if count < 1:
        return ""
    times = "once" if count == 1 else f"{count} times"
    fixed = [str(name) for name in (group.get("corrected_to") or []) if name]
    if fixed:
        # The ANSWER, not just the count. A book that only says "you were wrong
        # again" is a scoreboard; the correction is what makes it a mistake book.
        return f"You have made this mistake {times} before; {fixed[-1]!r} worked."
    return (
        f"You have made this mistake {times} before, and nothing has worked yet -- "
        f"read the field list above rather than guessing again ({AGENTIC_RETRY_BUDGET} "
        "tries, then ask the human)."
    )


def _mistake_group(
    mistakes: Mapping[str, Any] | None, code: str, field: str
) -> Mapping[str, Any] | None:
    """The folded group for ``(code, field)``, or for *field* alone when *code* is empty.

    An empty *code* matches on the FIELD only, which is what a refusal about a name
    wants: the agent reached ``credits_total`` and it does not matter whether last
    time that was an unknown field or a non-agentic one -- the lesson is the name.
    """
    if not isinstance(mistakes, Mapping):
        return None
    groups = mistakes.get("groups")
    if not isinstance(groups, list):
        return None
    best: Mapping[str, Any] | None = None
    for group in groups:
        if not isinstance(group, Mapping) or str(group.get("field") or "") != field:
            continue
        if code and str(group.get("code") or "") != code:
            continue
        # The fold orders by count, so the FIRST match is the one worth quoting.
        if best is None:
            best = group
    return best


#: Refusal codes that are about the field NAME. A group under one of these is
#: answered by an accepted write to a DIFFERENT field: the lesson is which name to
#: use, so a write to the same name cannot be its answer.
_NAME_CODES: Final[frozenset[str]] = frozenset({"unknown_field", "field_not_agentic"})

#: Refusal codes that are about the VALUE. A group under one of these is answered
#: by an accepted write to the SAME field, because the name was right and the value
#: was not -- and this is the harder lesson of the two: a wrong name is visible in
#: the field list, while a wrong type is only visible in the refusal.
#:
#: Split from the names rather than collapsed with them because the two need
#: OPPOSITE comparisons, and treating every group alike left a ``wrong_type`` group
#: permanently unanswered: the rule "a write never corrects itself" is exactly
#: right for a name and exactly wrong for a type.
_VALUE_CODES: Final[frozenset[str]] = frozenset(
    {
        "wrong_type",
        "wrong_shape",
        "value_too_large",
        "value_not_serializable",
        "value_too_deep",
        "redacted_value_invalid",
        "redacted_value_too_large",
    }
)


def correction_entry(
    field: str, mistakes: Mapping[str, Any] | None, crew_key: str = ""
) -> dict[str, Any] | None:
    """The correction an ACCEPTED write records, or ``None`` when it corrects nothing.

    Called after a write lands. It names every outstanding group this write answers,
    and WHICH groups those are depends on what each group's refusal was about:

    * a NAME group (``unknown_field``, ``field_not_agentic``) is answered by a write
      to a DIFFERENT field -- the lesson is which name to use;
    * a VALUE group (``wrong_type`` and its siblings) is answered by a write to the
      SAME field -- the name was right and the value was not.

    ``None`` when the book holds no such group, so an ordinary write appends no
    second entry.

    The comparison is on the FIELD NAME either way, which is what bounds this: a
    correction can only ever be claimed for a name already in the book, so a
    crewmate cannot grow the log by writing fields nobody ever refused.
    """
    if not isinstance(mistakes, Mapping):
        return None
    groups = mistakes.get("groups")
    if not isinstance(groups, list):
        return None
    wrong: set[str] = set()
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        named = str(group.get("field") or "")
        code = str(group.get("code") or "")
        if not named or (group.get("corrected_to") or []):
            continue
        if code in _VALUE_CODES and named == field:
            wrong.add(named)
        elif code in _NAME_CODES and named != field:
            wrong.add(named)
    if not wrong:
        return None
    # CODED, because ``code`` is required on the entry type and the fold decides
    # which of the type's two shapes a line is from exactly this key.
    entry: dict[str, Any] = {
        "code": MISTAKE_CORRECTED_CODE,
        "field": field,
        "corrects": sorted(wrong),
    }
    if crew_key:
        entry["crew_key"] = crew_key
    return entry


def fields_for_agent(
    instance: Instance | None,
    mistakes: Mapping[str, Any] | None = None,
    *,
    history: Sequence[Mapping[str, Any]] | None = None,
    rollback_versions: Sequence[int] | None = None,
    values: Mapping[str, Any] | None = None,
    written_at: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """What an agent is told BEFORE it writes: the fields, their sources, its mistakes.

    *values* is the page's current read, by field name, and *written_at* the host's
    stamp for each agentic cell. Each agentic row then carries its current value and
    each fold row a one-line summary; ``None`` (the read failed) leaves both out
    rather than reporting every cell empty.

    This is the read that makes the first try usually work. Without it an agent
    guesses a field name, is refused, and spends a cycle learning what one call
    could have told it -- and the mistake book exists precisely because that cycle
    was being spent repeatedly.

    Every field is listed, not only the writable ones, and each says where its value
    comes from. A fold-sourced field is NOT noise here: an agent that cannot see it
    has no way to know the number is already recorded, and will either try to write
    it (refused as ``field_not_agentic``) or, worse, duplicate it under an agentic
    name so the page shows the same quantity twice with two values.
    """
    if instance is None:
        return {
            "template": None,
            "instance_version": 0,
            "fields": [],
            "agentic": [],
            "mistakes": _recent_mistakes(mistakes),
            "retry_budget": AGENTIC_RETRY_BUDGET,
            "history": _recent_history(history),
            "rollback_versions": sorted(rollback_versions or ()),
        }
    listed: list[dict[str, Any]] = []
    current = values if isinstance(values, Mapping) else None
    stamps = written_at if isinstance(written_at, Mapping) else {}
    for name, spec in sorted(instance.manifest.fields.items()):
        row: dict[str, Any] = {"field": name, "type": spec.type}
        # The WHOLE shape, so the first write can be the right one: a type alone
        # says "array" and not which keys each row of it must carry.
        row["schema"] = spec.shape.describe() if spec.shape is not None else {"type": spec.type}
        if spec.agentic:
            row["source"] = "agentic"
            if current is not None:
                # What is on the page NOW, so a write that edits one card of a list
                # starts from the list that is there rather than from a guess.
                row["written"] = name in current
                if name in current:
                    row["value"] = current[name]
                    if stamps.get(name):
                        row["written_at"] = str(stamps[name])
        else:
            row["source"] = "fold"
            row["fold"] = spec.fold
            row["path"] = spec.path
            if current is not None:
                # One line, never the data: a fold value can be the whole work
                # board, and the agent cannot write it anyway.
                row["summary"] = fold_summary(current[name]) if name in current else "no value"
        listed.append(row)
    return {
        "template": {"id": instance.manifest.id, "version": instance.manifest.version},
        "instance_version": instance.instance_version,
        "fields": listed,
        "agentic": sorted(agentic_fields(instance.manifest)),
        "mistakes": _recent_mistakes(mistakes),
        "retry_budget": AGENTIC_RETRY_BUDGET,
        "history": _recent_history(history),
        "rollback_versions": sorted(rollback_versions or ()),
    }


#: Keys whose string value is a stamp, newest-first comparable as ISO text.
_STAMP_KEYS: Final[tuple[str, ...]] = ("at", "ts", "updated_at", "last_report_at", "last_entry_at")


def fold_summary(value: Any) -> str:
    """One line about a fold value: how many rows, and the newest stamp among them."""
    if isinstance(value, list):
        rows = [row for row in value if isinstance(row, Mapping)]
        stamps = [
            str(row[key])
            for row in rows
            for key in _STAMP_KEYS
            if isinstance(row.get(key), str) and row.get(key)
        ]
        line = f"{len(value)} item{'' if len(value) == 1 else 's'}"
        return line + (f", latest {max(stamps)}" if stamps else "")
    if isinstance(value, Mapping):
        return f"{len(value)} key{'' if len(value) == 1 else 's'}"
    if isinstance(value, str):
        return value if len(value) <= 80 else f"string, {len(value)} chars"
    if value is None:
        return "no value"
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return type(value).__name__


def turn_block(slug: str) -> str:
    """The ``[DASHBOARD]`` lines for a crewmate's turn, or ``""`` when it has none.

    A few lines on purpose: which page is in force and which fields are the
    crewmate's to write. The shapes and the mistake book stay behind
    ``dashboard_fields``, because a block on every turn is paid for on every turn.
    TOTAL: a dashboard that cannot be read costs the turn this block, nothing else.
    """
    try:
        from kiro_crew.dashboard_templates import instance as instance_store
        from kiro_crew.dashboard_templates.manifest import parse_manifest

        record = instance_store.read(slug)
        if record.state == instance_store.STATE_EMPTY:
            fallback = instance_store.default_instance(slug)
            if fallback is None:
                return ""
            record = fallback
        elif record.state not in (instance_store.STATE_LIVE, instance_store.STATE_STALE):
            return ""
        manifest = parse_manifest(dict(record.manifest))
    except Exception:
        logger.debug("no [DASHBOARD] block for %r", slug, exc_info=True)
        return ""
    names = sorted(agentic_fields(manifest))
    writes = ", ".join(names) if names else "none (every field is folded)"
    return (
        "[DASHBOARD]\n"
        f"Your dynamic dashboard is template {manifest.id} v{manifest.version}; the "
        "Dashboard tab shows it only with the 'Dynamic Dashboard' Feature Preview on.\n"
        f"Fields you write: {writes}. Call dashboard_fields for their shapes and "
        "current values before dashboard_write."
    )


def _recent_history(history: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """The last few changes to this dashboard, newest last, four columns wide.

    Four columns and not the fold's eight: an agent reading this is answering "go
    back" or "what is this page", and ``fields``, ``html_bytes`` and ``from_version``
    answer neither. Taken from the END of the fold's own order, because the fold keeps
    newest last and the rows worth showing are the recent ones.

    The ROW ORDER is preserved rather than reversed. The fold, the history savepoint
    and the instance record all read newest-last, and handing one reader the opposite
    order is how a caller ends up rolling back to the version it meant to keep.
    """
    if not history:
        return []
    rows: list[dict[str, Any]] = []
    for row in list(history)[-HISTORY_SHOWN:]:
        if not isinstance(row, Mapping):
            continue
        rows.append(
            {
                "instance_version": int(row.get("instance_version") or 0),
                "action": str(row.get("action") or ""),
                "template_id": str(row.get("template_id") or ""),
                "at_ms": int(row.get("at_ms") or 0),
            }
        )
    return rows


def _recent_mistakes(mistakes: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The mistake book's worst few groups, as the agent reads them.

    Taken in the fold's own order, which is by count: a mistake made five times is
    the one most worth not repeating. Trimmed to :data:`MISTAKES_SHOWN` because the
    reader is an agent with a budget and a long list is one it skims.
    """
    if not isinstance(mistakes, Mapping):
        return []
    groups = mistakes.get("groups")
    if not isinstance(groups, list):
        return []
    out: list[dict[str, Any]] = []
    for group in groups[:MISTAKES_SHOWN]:
        if not isinstance(group, Mapping):
            continue
        row: dict[str, Any] = {
            "code": str(group.get("code") or ""),
            "field": str(group.get("field") or ""),
            "count": int(group.get("count") or 0),
            "reason": str(group.get("reason") or ""),
        }
        fixed = [str(name) for name in (group.get("corrected_to") or []) if name]
        if fixed:
            row["use_instead"] = fixed[-1]
        out.append(row)
    return out


def refusal_sentence(refused: WriteRefused) -> str:
    """The refusal as the agent reads it: scrubbed, and WHOLE.

    One scrub and one sentence, which is what keeps the 400 body and the stored
    mistake row from disagreeing about what the refusal said. The body is not
    truncated, because the end of the sentence is where the remedy is: the valid
    field names come first, and the repeat note that quotes what worked last. An
    agent handed the head of that sentence is told what it got wrong and not what
    to do instead, which is the cycle the mistake book exists to end.
    """
    scrubbed = cast("dict[str, Any]", _redacted({"reason": str(refused)}))
    return str(scrubbed.get("reason") or "")


def refusal_entry(refused: WriteRefused, crew_key: str = "") -> dict[str, Any]:
    """The ``dashboard/agentic_refused`` payload for one refusal.

    The sentence travels with the code, so the mistake book hands back the SAME
    correction the agent was given rather than a second wording of it -- two
    wordings of one rule is how an agent ends up unsure which it broke.

    SCRUBBED, for the same reason an accepted value is. Both halves of this payload
    are agent-written: the `field` is the name the agent asked for, and the `reason`
    quotes it back inside the sentence. A page binding the mistake book draws them
    as text, so a credential-shaped field name within the length cap would be
    rendered verbatim on the dashboard. The scrub runs in
    :func:`refusal_sentence`, which this builds on, so the stored entry and the
    caller's own 400 body cannot differ about what the refusal SAID.

    The stored ``reason`` is a PREFIX of that sentence and not a second wording of
    it. The cap bounds the crew-log entry; the 400 body carries the sentence whole,
    because the tail is the part that says "you have made this mistake N times
    before, and 'x' worked" -- the correction the mistake book exists to hand back,
    and the first thing a 240-character cut drops once a manifest has about five
    fields to list.
    """
    entry: dict[str, Any] = {
        "code": refused.code,
        "reason": refusal_sentence(refused)[:240],
    }
    if refused.field:
        entry["field"] = refused.field
    if crew_key:
        entry["crew_key"] = crew_key
    #: `code` is a closed vocabulary this module writes, and `crew_key` is a host
    #: identifier, so neither can carry agent text; both pass through the walk
    #: unchanged, and scrubbing the whole dict keeps a field added later covered.
    return cast("dict[str, Any]", _redacted(entry))


#: The fold a crewmate's mistake book is read from, re-exported so a caller does
#: not have to know it is a crew-log fold name.
MISTAKES_FOLD = MISTAKES_FOLD_NAME

#: The entry type a refusal lands as, re-exported for the same reason.
REFUSED_ENTRY_TYPE = DASHBOARD_REFUSED_ENTRY_TYPE

#: The entry type an accepted value lands as, re-exported beside its sibling so a
#: caller asking "does this fit one entry" names the type rather than deriving it.
VALUE_ENTRY_TYPE = DASHBOARD_AGENTIC_ENTRY_TYPE

#: Every manifest type this module checks. Pinned against the manifest's own set by
#: test, so a type added there without a branch in :func:`type_holds` reddens rather
#: than refusing every write to a field of the new type.
CHECKED_TYPES: Final[frozenset[str]] = FIELD_TYPES

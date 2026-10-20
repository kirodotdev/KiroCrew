"""Scrubbing the text a handler returns: memory fields, JSON transports, search rows.

A memory record's text and a JSON document carried inside one are put through the
exfiltration-URL and credential redactors before egress, field by field, and
``SESSION_SEARCH_TEXT_FIELDS`` names the session-search fields every pass returning
such a row must redact.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import json
from typing import overload

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.terminal_safe import normalize_for_scanning, strip_control_characters


def _scrub_text(val: str) -> str:
    """Redact ``val``, dropping control characters but keeping the content around them.

    Invisible characters split a token, and both redactors decide by matching a pattern,
    so a token split by one matches nothing and the field egresses carrying it. They do
    not all deserve the same treatment on the way out, though, and that is the whole of
    this function's shape.

    A CONTROL character other than tab, newline and carriage return is terminal-escape
    material rather than text a user wrote, so it always leaves: it drives a terminal that
    renders the field verbatim, and no consumer is worse off without it. Those three are
    content and stay, which is why a token split by one of them is still split afterwards.
    Removing a control character can JOIN a token back together, which is why the redactors
    run again afterwards rather than trusting the first verdict.

    A FORMAT character is usually content. A soft hyphen inside a word, a joiner holding an
    emoji together, a mark ordering a Latin digit before Arabic -- deleting those rewrites
    what the user stored, on a path that runs for every row of every listing, and the
    caller that compares this function's output with its input to decide whether a
    document is editable then refuses every write to it. So a copy with the format
    characters removed is scanned as EVIDENCE, and handed back only when it reveals a
    credential the output still hides: that field holds a credential, so its exact bytes
    are the thing that must not egress, and its own format characters go with them.

    Scanning the text as stored first is not redundant with scanning that copy. Removing
    an invisible character can destroy a boundary a pattern requires, so a token the
    stored text matches can stop matching once the copy is joined up.
    """
    out, _ = redact_exfiltration_urls(val)
    out, _ = redact_credentials(out)
    stripped = strip_control_characters(out)
    if stripped != out:
        out, _ = redact_exfiltration_urls(stripped)
        out, _ = redact_credentials(out)
    normalised = normalize_for_scanning(out)
    if normalised == out:
        return out
    scanned, _ = redact_exfiltration_urls(normalised)
    scanned, _ = redact_credentials(scanned)
    if scanned == normalised:
        return out
    return scanned


#: Deepest JSON nesting this scrub walks. A stored memory payload is a handful of levels
#: deep, so the cap costs real data nothing; what it buys is a bound on the descent below,
#: which would otherwise recurse as deep as a hostile document asks it to.
_MAX_JSON_SCRUB_DEPTH = 64


#: Stands in for a JSON payload this chain cannot scan to the bottom. Serialised as a JSON
#: string so the field a consumer parses still parses.
_UNSCANNABLE_JSON = "[REDACTED: unscannable JSON payload]"


class _UnscannableJSON(Exception):
    """A JSON payload that cannot be scanned to the bottom, so it must not egress."""


def _decode_unique(val: str) -> object:
    """Decode a JSON document, refusing one whose object names are not unique.

    ``json.loads`` keeps the LAST value for a repeated name and discards the rest, so a
    credential sitting in an earlier duplicate is gone before the scan below ever sees it.
    What survives decoding then looks exactly like a clean document, the comparison
    downstream finds nothing changed, and the raw text egresses still carrying it. Reading
    all of a document is what certifies it, so a repeated name makes it unscannable.
    """

    def unique(items: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in items:
            if key in out:
                raise _UnscannableJSON
            out[key] = value
        return out

    return json.loads(val, object_pairs_hook=unique)


def _scrub_decoded(val: object, depth: int = 0) -> object:
    """Scrub every string inside an already-decoded JSON value, shape preserved.

    An object's NAMES are scrubbed alongside its values, by the same path. A name is text
    the document carries and the field egresses, so a credential sitting in one leaves
    untouched if only values are walked, and the name is the easier place to put it:
    whoever writes structured memory chooses both halves of a pair. A name can also be a
    document in its own right, so it takes the descent below rather than a text scrub
    alone, and the two halves of a pair are defended identically.

    A string is handed back to the transport scrub, because one decode does not reach the
    bottom of every field. A memory record keeps its own value as a JSON document, so a
    revision snapshot of that record is a document holding a document: the outer decode
    yields a string whose escapes are still printable text, where no control character
    exists to remove and no split credential matches. Descending again is what reaches it.

    Scrubbing two different names can produce the same name, and a dict holds one value
    per name. Keeping either silently drops the other's value, so this raises instead and
    lets the caller withhold the whole document. Depth is bounded for the same reason it
    is bounded anywhere: the walk below is recursive and the document is untrusted. The
    count spans encoding levels as well as structural ones, so the descent above cannot
    restart it and escape the bound.
    """
    if depth > _MAX_JSON_SCRUB_DEPTH:
        raise _UnscannableJSON
    if isinstance(val, str):
        return _scrub_json_transport(_scrub_text(val), depth + 1, original=val)
    if isinstance(val, list):
        return [_scrub_decoded(item, depth + 1) for item in val]
    if isinstance(val, dict):
        cleaned: dict[object, object] = {}
        for key, item in val.items():
            name = (
                _scrub_json_transport(_scrub_text(key), depth + 1, original=key)
                if isinstance(key, str)
                else key
            )
            if name in cleaned:
                raise _UnscannableJSON
            cleaned[name] = _scrub_decoded(item, depth + 1)
        return cleaned
    return val


def _is_json_document(val: str) -> bool:
    """Whether the text is a JSON document, judged before any scrub has touched it.

    The scan below runs on scrubbed text, and a redactor's replacement can span JSON
    structure: the credential-assignment patterns match across a name, its colon and its
    value, so splicing one out leaves text that does not parse. Judging JSON-ness on that
    text would call a real document prose and hand it back unscanned. This answers for the
    stored bytes instead, so the two questions stay separate.

    A decode that fails for any reason OTHER than malformed syntax still means the text is
    JSON -- decoding merely could not finish -- so those count as a document here.
    """
    if val.lstrip()[:1] not in ("{", "[", '"'):
        return False
    try:
        json.loads(val)
    except json.JSONDecodeError:
        return False
    except (ValueError, RecursionError):
        return True
    return True


def _scrubbed_document_or(val: str, original: str | None) -> str:
    """Hand back scrubbed text that is not a document, or withhold one the scrub broke.

    Reaching here means the text in hand does not parse. That is the safe case only when the
    stored bytes did not parse either: then no JSON payload ever existed and the text scan
    covered everything. When the stored bytes DID parse, the scrub's own replacement broke
    the document, so its payload was never walked and handing the text back ships an
    uncertified payload -- the hole this chain closes.
    """
    if original is not None and original != val and _is_json_document(original):
        return json.dumps(_UNSCANNABLE_JSON)
    return val


def _scrub_json_transport(val: str, depth: int = 0, *, original: str | None = None) -> str:
    """Scrub the PAYLOAD of a field that carries a JSON document, not just its text.

    A JSON document is a transport encoding, and encoding hides the very characters the
    scan looks for: a control character inside the payload is written as the six printable
    characters ``\\u0001``, so the control pattern finds nothing to remove and the token it
    splits stays split for both redactor passes. The field then egresses carrying the
    credential, and whatever decodes the document -- a browser calling ``JSON.parse`` --
    gets the control character back, where it renders as nothing and the credential reads
    as whole. A memory row carries the same value twice, as text and as JSON, so scanning
    only the text redacts one copy of a credential and ships the other.

    Scrubbing therefore descends into the decoded value. The field is replaced only when
    that changed something, so a document holding nothing sensitive is passed through
    byte for byte rather than re-serialised into a different spelling of itself.

    Only a document decoding to a string, list or dict is considered: a bare JSON number
    or boolean carries no text to scan, and a plain prose field does not parse at all.

    Two ways of failing are kept apart, because only one of them leaves a payload behind.
    The dividing line is the PARSER's own verdict on the STORED bytes, not on the text in
    hand: the scan runs on scrubbed text, and a credential-assignment pattern spans a name,
    its colon and its value, so splicing one out can leave text that fails to parse even
    though the stored document parses fine. Text the parser rejects AND that was never a
    document
    holds no JSON payload at all, so the text scrub already covered everything there was to
    cover and the field passes through. Every other failure means a payload existed and
    decoding could not finish -- nested past the cap, nested deeply enough to exhaust the
    parser's own recursion, repeating an object name so decoding discards a member, holding
    names that collide once scrubbed, tripping a limit of this interpreter that a consumer's
    parser does not share such as the cap on converting a very long integer, or broken by
    the scrub's own replacement. Such a document has a payload this chain cannot certify,
    and shipping an uncertified payload is the hole being closed here, so the field is
    withheld and a marker goes out instead.
    """
    if val.lstrip()[:1] not in ("{", "[", '"'):
        return _scrubbed_document_or(val, original)
    try:
        decoded = _decode_unique(val)
    except json.JSONDecodeError:
        return _scrubbed_document_or(val, original)
    except (_UnscannableJSON, ValueError, RecursionError):
        return json.dumps(_UNSCANNABLE_JSON)
    if not isinstance(decoded, (str, list, dict)):
        return val
    try:
        cleaned = _scrub_decoded(decoded, depth)
    except (_UnscannableJSON, RecursionError):
        return json.dumps(_UNSCANNABLE_JSON)
    if cleaned == decoded:
        return val
    return json.dumps(cleaned)


@overload
def _redact_memory_field(val: dict) -> dict: ...


@overload
def _redact_memory_field(val: list) -> list: ...


@overload
def _redact_memory_field(val: str) -> str: ...


@overload
def _redact_memory_field(val: object) -> object: ...


def _redact_memory_field(val: object) -> object:
    """Redact credentials and exfiltration URLs from a memory field.

    Lives here (not in ``memory.py``) so handlers that ``memory.py`` itself
    imports from -- e.g. ``cron.py`` -- can share the chain without an import
    cycle.

    SHAPE-PRESERVING for a container, and the overloads above say so rather than
    flattening every result to ``object``: a caller that hands this a dict and then
    bounds or indexes the result would otherwise need a cast, which asserts the shape
    instead of reading it off the function. A dict comes back a dict, a list a list
    and a string a string.

    NOT shape-preserving for bytes, which is why one type variable would be the wrong
    tool here: binary is dropped to ``None`` rather than redacted, since it is not
    text this chain can scan and returning it unread would put an unscanned blob on an
    egress path. That case falls to the ``object`` overload.

    The redactors run TWICE, once on the text as stored and once after invisible
    characters are removed, because each pass catches what the other cannot.

    Both redactors decide by matching a pattern. An invisible character embedded
    mid-token splits the token so no pattern matches it, and the field would leave on
    an egress path carrying credential material any consumer that drops those
    characters can reassemble. Removing them first rejoins the token, which is what the
    second pass sees.

    The first pass is not redundant, because removing a character can also DESTROY a
    match. A pattern guarded by a negative lookbehind for a non-word character is
    satisfied by the invisible character itself, so joining a word character onto the
    token defeats it -- a credential the text as stored would have given up survives
    normalisation. Scanning the original first keeps that verdict.

    Normalising between the two passes rather than after both is what makes the order
    safe. Normalising after the last pass would reassemble the very token that pass had
    just failed to match, and the field would egress the whole secret.

    Tab, newline and carriage return are content and survive, so a token split by one of
    those three stays split; see
    :func:`kiro_crew.terminal_safe.normalize_for_scanning` for what is removed and why
    no visible content is lost.
    """
    if isinstance(val, (bytes, memoryview)):
        return None
    if isinstance(val, str):
        return _scrub_json_transport(_scrub_text(val), original=val)
    if isinstance(val, list):
        return [_redact_memory_field(item) for item in val]
    if isinstance(val, dict):
        return {k: _redact_memory_field(v) for k, v in val.items()}
    return val


#: The session-search row fields carrying LLM-authored or peer-supplied prose,
#: which every pass returning such a row must put through
#: :func:`kiro_crew.security.redact` before egress.
#:
#: Three passes return these rows -- ``api_sessions_search``, and
#: ``api_instances_search_sessions``' local-row and peer-row passes -- and each
#: would otherwise hand-copy both the redaction chain AND this field list. The
#: chain already has an owner (``security.redact`` composes the exfiltration-URL
#: and credential passes in that order); this tuple gives the field list one too,
#: so a caller cannot redact ``title`` and quietly forget ``snippet``. A missing
#: field reads as correct at the call site, which is why the list is shared
#: rather than restated.
#:
#: Order is irrelevant; membership is the contract.
SESSION_SEARCH_TEXT_FIELDS: tuple[str, ...] = ("title", "snippet")

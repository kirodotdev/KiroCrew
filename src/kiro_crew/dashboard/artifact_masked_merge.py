"""Keep the text a canvas read masked when its owner saves over the masked copy.

The detail read masks every credential value in a canvas (``_serialize``), so the
page holds placeholders where the stored document holds the values, and an
autosave sends the whole document back. :func:`restore_masked_values` puts each
hidden value back where the save still carries its placeholder, so the owner can
edit, move and delete around masked text, and delete a masked value by deleting
its placeholder, without the browser ever receiving the value.

A value goes back only where the pairing of placeholder and value is certain.
Identical placeholders carry no identity, so a text that still carries
placeholders must carry exactly as many as it did, with the text between each
two of them unchanged; only the text before the first and after the last may
change. Anything else (one of several deleted, a placeholder moved, copied or
pasted into another shape, an undo of a deletion an earlier save already stored)
is refused rather than guessed. Plain placeholder text the store already held
stays as it is, and a text with no placeholder left deletes its values. The
merged document must mask to exactly what the save showed, or nothing is
written: a restored value can never become visible to a reader the filter masks
it for.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

#: Every placeholder the credential pass writes. Longest first, so the encoded tag
#: is never read as a plain one.
_TAG_RE = re.compile(
    "|".join(re.escape(t) for t in sorted(CREDENTIAL_REDACTION_TAGS, key=len, reverse=True))
)

#: A JSON path: dict keys, list indexes, and ``("id", <id>)`` for a list item that
#: carries a unique string ``id`` (a canvas node or edge), so a shape keeps its
#: path when others are added, removed or restacked.
_Path = tuple[Any, ...]


_UNBACKED = (
    "this save changes hidden-text placeholders or the text between them (a copy, a move, "
    "one of several removed, or an undo of a deletion that was already saved)"
)


class MaskedMergeError(ValueError):
    """The save cannot keep the masked values without showing one or losing one."""


def carries_placeholder(content: str) -> bool:
    """Whether *content* holds a credential placeholder, the only case a merge acts on."""
    return bool(_TAG_RE.search(content))


def _child_keys(items: list[Any]) -> list[Any]:
    ids = [it.get("id") if isinstance(it, dict) else None for it in items]
    counts = Counter(i for i in ids if isinstance(i, str))
    keys: list[Any] = []
    for i, item_id in enumerate(ids):
        unique = isinstance(item_id, str) and counts[item_id] == 1
        keys.append(("id", item_id) if unique else i)
    return keys


def _leaves(doc: Any, path: _Path = ()) -> dict[_Path, str]:
    """Every string leaf of *doc* by path."""
    out: dict[_Path, str] = {}
    if isinstance(doc, str):
        out[path] = doc
    elif isinstance(doc, dict):
        for key, value in doc.items():
            out.update(_leaves(value, path + (key,)))
    elif isinstance(doc, list):
        for key, value in zip(_child_keys(doc), doc):
            out.update(_leaves(value, path + (key,)))
    return out


def _hidden_values(masked: str, original: str) -> list[tuple[int, str, str]] | None:
    """``(offset in masked, tag, value)`` for each placeholder in *masked*, or None.

    The masked leaf is the original with each credential value replaced by its
    tag, so the literal runs between tags pin each value. shortcut: a literal run
    that also occurs inside a value can move the split by that run, so the value's
    edges can be misattributed when the page edits text next to a placeholder;
    upgrade to the redaction plan's own spans if that shows up.
    """
    tags = list(_TAG_RE.finditer(masked))
    literals = _TAG_RE.split(masked)
    pattern = "(.*?)".join(re.escape(lit) for lit in literals)
    found = re.fullmatch(pattern, original, re.DOTALL)
    if found is None:
        return None
    return [(m.start(), m.group(), v) for m, v in zip(tags, found.groups())]


def _restore_leaf(masked: str, original: str, incoming: str) -> str:
    """Put *original*'s hidden values back into *incoming*, the page's edit of *masked*.

    The runs of text between *masked*'s placeholders pair them with their values;
    *incoming* must have the same number of placeholders and the same inner runs,
    so each placeholder stands where its value was. Otherwise
    :class:`MaskedMergeError`.
    """
    if incoming == masked:
        return original
    hidden = _hidden_values(masked, original)
    if hidden is None:
        raise MaskedMergeError("the stored text does not match its masked copy")
    runs, now = _TAG_RE.split(masked), _TAG_RE.split(incoming)
    if len(now) != len(runs) or now[1:-1] != runs[1:-1]:
        raise MaskedMergeError(_UNBACKED)
    out = [now[0]]
    for (_offset, _tag, value), run in zip(hidden, now[1:]):
        out.extend((value, run))
    return "".join(out)


def _set_leaf(doc: Any, path: _Path, value: str) -> Any:
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(doc, dict):
        doc[head] = _set_leaf(doc[head], rest, value)
    else:
        keys = _child_keys(doc)
        i = keys.index(head)
        doc[i] = _set_leaf(doc[i], rest, value)
    return doc


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _dump(doc: Any) -> str:
    # The canvas editor's own spelling (``JSON.stringify(doc, null, 1)``): other
    # characters as written, a lone surrogate escaped as it is on the wire.
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    return _LONE_SURROGATE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def restore_masked_values(stored: str, incoming: str, read_filter: Callable[[str], str]) -> str:
    """*incoming* with each value the masked read hid put back under its placeholder.

    *stored* is the canvas the save was based on (its ``expected_token`` names
    it), *incoming* the document the page sends, built from the masked read, and
    *read_filter* the read filter that produced that read (the caller's, never a copy).
    Returns *incoming* unchanged when it carries no placeholder.
    Raises :class:`MaskedMergeError` when the result would not mask to what the
    page showed: then a value would leak, or a placeholder is out of line with it.
    """
    if not carries_placeholder(incoming):
        return incoming
    masked = read_filter(stored)
    try:
        stored_doc, masked_doc, incoming_doc = (json.loads(t) for t in (stored, masked, incoming))
    except ValueError:
        return incoming
    stored_leaves, masked_leaves = _leaves(stored_doc), _leaves(masked_doc)
    if stored_leaves.keys() != masked_leaves.keys():
        raise MaskedMergeError("the masked copy does not have the stored canvas's shape")
    hidden = {p: (masked_leaves[p], v) for p, v in stored_leaves.items() if masked_leaves[p] != v}
    merged = json.loads(_dump(incoming_doc))
    restored: dict[_Path, str] = {}
    for path, text in _leaves(incoming_doc).items():
        if not carries_placeholder(text):
            continue
        if path in hidden:
            restored[path] = _restore_leaf(*hidden[path], text)
        elif path in stored_leaves:
            # Plain text the store already held, placeholder look-alike included.
            if len(_TAG_RE.findall(text)) > len(_TAG_RE.findall(stored_leaves[path])):
                raise MaskedMergeError(_UNBACKED)
        else:
            raise MaskedMergeError(_UNBACKED)
    for path, text in restored.items():
        merged = _set_leaf(merged, path, text)
    out = _dump(merged)
    if read_filter(out) != read_filter(_dump(incoming_doc)):
        raise MaskedMergeError(
            "this save would show hidden text, or no longer lines up with it; "
            "delete the hidden text whole, or reload the canvas"
        )
    return out

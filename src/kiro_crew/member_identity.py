"""Crew-member identity: the split between a member's ``id`` and its ``display_name``.

A crew member is a Kiro custom agent plus a Kiro Crew wrapper (the ``agents`` row
in ``config.json`` and ``members/<id>/``). Before this module the row's KEY did
two jobs at once — it was the primary key every subsystem references (member
dir, DM binding, slot ``agent``, cron ``agent``, governance identity) AND the
label the user typed and reads. The two roles have incompatible rules: the
create flow accepted ``case competition`` (a space), and the Crew Members roster
silently filtered the row out because a key with a space cannot address anything.

The wrapper now separates them:

* ``id`` — the ``agents`` map key. System-minted, immutable, matches
  :data:`MEMBER_ID_RE` (the shared agent-name grammar). Also the member's Kiro
  custom-agent ``name``, so Kiro and Kiro Crew agree on identity.
* ``display_name`` — what the user calls the member. Free text, free rename,
  zero blast radius. Stored on the row; empty means "same as the id".

Every consumer that keyed on the name keeps working, because for a
well-formed name the minted id IS the name. Only a name outside the grammar
gets a sanitized id, with the original string preserved as its display name —
the split the user intended when typing it.

Leaf module on purpose: the config loader imports it during ``load()`` (the
migration), so it must not import from ``kiro_crew.config`` or anything that
does. The grammar is spelled here and pinned equal to
``kiro_crew.validation._AGENT_NAME_RE`` by a test rather than imported, because
``validation`` imports ``config.sections`` and would close a cycle.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Collection, Mapping

#: The member-id grammar. Byte-identical to ``validation._AGENT_NAME_RE`` (pinned
#: by ``test_member_identity.py``): ASCII letters, digits, ``_`` and ``-``, 1..64
#: chars, no leading or trailing separator. A value in this grammar can name a
#: Kiro agent, a member directory, a slot key and a cron ``agent`` field.
MEMBER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")

#: Longest id the grammar admits (63 body chars + 1 terminal).
MEMBER_ID_MAX_LEN = 64

#: Longest display name the wrapper stores. Presentation-only, so the bound is
#: about UI legibility and a hand-editable config not carrying a novel.
DISPLAY_NAME_MAX_LEN = 80

#: What an id collapses to when the display name has no id-safe characters at
#: all (a name written entirely in punctuation or in a non-Latin script).
_FALLBACK_ID = "member"

_NON_ID_RUN_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_SEPARATOR_RUN_RE = re.compile(r"[-_]{2,}")


def is_valid_member_id(value: object) -> bool:
    """True when *value* is a string inside the member-id grammar."""
    return isinstance(value, str) and MEMBER_ID_RE.match(value) is not None


def sanitize_member_id(display_name: str) -> str:
    """Derive an id CANDIDATE from a free-form display name.

    Lossy and not unique on its own — pass the result through
    :func:`mint_member_id` to resolve collisions. A name already inside the
    grammar comes back unchanged, which is what keeps every pre-split consumer
    working: for the common case the id IS the typed name.

    Case is preserved (the grammar admits it and existing ids like
    ``Docs_Writer`` depend on it); accents are folded to ASCII; every run of
    characters outside the grammar becomes one hyphen; separator runs collapse;
    leading/trailing separators are stripped; the result is capped at
    :data:`MEMBER_ID_MAX_LEN` without a trailing separator.
    """
    if is_valid_member_id(display_name):
        return display_name
    text = unicodedata.normalize("NFKD", display_name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NON_ID_RUN_RE.sub("-", text.strip())
    text = _SEPARATOR_RUN_RE.sub("-", text)
    text = text.strip("-_")
    if not text:
        return _FALLBACK_ID
    text = text[:MEMBER_ID_MAX_LEN].rstrip("-_")
    return text or _FALLBACK_ID


def opaque_member_id(seed: str) -> str:
    """An id that carries NOTHING of *seed*: ``member-<10 hex of sha256(seed)>``.

    For a name the redactors would alter (a credential-shaped legacy key, an
    exfiltration URL): the sanitized candidate would keep the secret inside the
    id grammar, and an id is rendered verbatim everywhere -- the roster's
    ``name``, the slug, the slot key, the URL. Deterministic, so a retried
    migration mints the same id from the same key; always inside the grammar.
    """
    digest = hashlib.sha256((seed or "").encode("utf-8", "surrogatepass")).hexdigest()
    return f"member-{digest[:10]}"


def mint_member_id(display_name: str, taken: Collection[str]) -> str:
    """Mint a member id from *display_name* that is not in *taken*.

    The sanitized candidate is used verbatim when free; otherwise a ``-2``,
    ``-3``, … suffix is appended (trimming the stem so the result stays inside
    the length cap). Deterministic given the same inputs, so a retried create
    lands on the same id.
    """
    return mint_member_id_from_base(sanitize_member_id(display_name), taken)


def mint_member_id_from_base(base: str, taken: Collection[str]) -> str:
    """:func:`mint_member_id` for a candidate already inside the grammar
    (a sanitized name, or :func:`opaque_member_id`'s output)."""
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        stem = base[: MEMBER_ID_MAX_LEN - len(suffix)].rstrip("-_") or _FALLBACK_ID
        candidate = f"{stem}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1


def collapse_display_name(raw: object) -> str:
    """Whitespace-normalize *raw* without deciding whether the wrapper keeps it.

    Non-strings read as ""; inner whitespace runs collapse to one space. This is
    the WHOLE submitted value, which is what a write route must hand the
    credential scanner: a check run on a shortened copy can miss a secret whose
    tail was cut, and the shortened copy then carries most of that secret.
    """
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())


def display_name_too_long(raw: object) -> bool:
    """True when *raw*, whitespace-normalized, exceeds :data:`DISPLAY_NAME_MAX_LEN`.

    The write routes refuse such a value (400) instead of storing a prefix of
    it, and :func:`normalize_display_name` reads a stored one as "".
    """
    return len(collapse_display_name(raw)) > DISPLAY_NAME_MAX_LEN


def normalize_display_name(raw: object) -> str:
    """Coerce a stored or submitted display name to what the wrapper keeps.

    Non-strings and whitespace-only values read as "" (= use the id); inner
    whitespace runs collapse to one space. A value longer than
    :data:`DISPLAY_NAME_MAX_LEN` ALSO reads as "": it is never truncated,
    because a truncated label is a value no scanner saw whole — a label that
    ends in a credential, cut one byte short, is a label that carries nineteen
    twentieths of that credential past every check keyed on the full pattern.
    The write routes refuse the over-long value outright; a hand-edited one in
    the config falls back to the id. Presentation-only otherwise, so no charset
    is refused here — the credential-shaped check the create route applies to
    labels is the caller's job, because it needs the roster mask this leaf
    module cannot see.
    """
    text = collapse_display_name(raw)
    if len(text) > DISPLAY_NAME_MAX_LEN:
        return ""
    return text


def effective_display_name(member_id: str, display_name: object) -> str:
    """The label a surface renders for a member: its display name, else its id."""
    return normalize_display_name(display_name) or member_id


def members_named(handle: str, display_names: Mapping[str, object]) -> list[str]:
    """Ids of the members whose *effective* display name equals *handle*.

    *display_names* maps member id → stored display name (``""`` meaning "same
    as the id"). The handle is normalized the way display names are stored, so
    the free text a caller typed compares against what the wrapper kept. Exact,
    case-sensitive match: this is the fallback for a caller holding an OLD
    handle (the free-text name that was the row key before the split minted an
    id from it), not a search. Two members may legitimately carry the same
    display name, so the result is a list — the caller decides what an
    ambiguous handle means.
    """
    wanted = normalize_display_name(handle)
    if not wanted:
        return []
    return [
        member_id
        for member_id, stored in display_names.items()
        if effective_display_name(member_id, stored) == wanted
    ]

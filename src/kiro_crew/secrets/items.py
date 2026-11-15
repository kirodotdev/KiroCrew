"""Typed vault items: what one encrypted entry holds, and how a reference picks a field.

A vault entry is one ciphertext under one name. This module decides what the
PLAINTEXT inside it looks like, per item type, and nothing about how it is
stored or who may read it (that is :mod:`kiro_crew.secrets.vault` and the
sandbox).

Three types:

``secret``
    The plaintext IS the value, with no wrapper. Every entry stored before item
    types existed is this type, byte for byte, so nothing migrates.
``login``
    A website credential: ``username``, ``password``, ``urls`` (the sites
    it belongs to, as plain http(s) addresses) and ``notes``. Stored as one JSON
    object. The URL and username sit INSIDE the ciphertext: a store
    that leaked would say which sites a person has accounts on otherwise.
``note``
    Free text under ``text``.

A reference is ``secret://NAME`` or ``secret://NAME#FIELD``. With no field it
resolves to the type's DEFAULT field (``password`` for a login, ``text`` for a
note, the value for a secret), so a consumer written for a plain secret keeps
working when the entry becomes a login. An unknown field fails closed: a reference never degrades to a literal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import urlsplit

ITEM_TYPE_SECRET = "secret"
ITEM_TYPE_LOGIN = "login"
ITEM_TYPE_NOTE = "note"
ITEM_TYPES: frozenset[str] = frozenset({ITEM_TYPE_SECRET, ITEM_TYPE_LOGIN, ITEM_TYPE_NOTE})

#: Separates the item name from the field in a reference.
REFERENCE_FIELD_SEPARATOR = "#"

FIELD_VALUE = "value"
FIELD_USERNAME = "username"
FIELD_PASSWORD = "password"
FIELD_NOTES = "notes"
FIELD_URL = "url"
FIELD_TEXT = "text"

#: The fields a reference may name, per type, and the one a bare reference means.
REFERENCE_FIELDS: dict[str, frozenset[str]] = {
    ITEM_TYPE_SECRET: frozenset({FIELD_VALUE}),
    ITEM_TYPE_LOGIN: frozenset({FIELD_USERNAME, FIELD_PASSWORD, FIELD_NOTES, FIELD_URL}),
    ITEM_TYPE_NOTE: frozenset({FIELD_TEXT}),
}
DEFAULT_FIELD: dict[str, str] = {
    ITEM_TYPE_SECRET: FIELD_VALUE,
    ITEM_TYPE_LOGIN: FIELD_PASSWORD,
    ITEM_TYPE_NOTE: FIELD_TEXT,
}

#: Bounds on what one item may hold. A vault entry is a credential, not a
#: document, and the whole store is read into memory on every lookup.
MAX_FIELD_CHARS = 16_384
MAX_LOGIN_URLS = 16


class ItemError(ValueError):
    """An item that cannot be stored or a field that cannot be resolved.

    ``code`` is a stable machine-readable reason for an API response; the
    message is for a person. Neither ever carries a field VALUE.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── References ──


def parse_reference(ref: str) -> tuple[str, Optional[str]]:
    """Split the part after ``secret://`` into ``(name, field)``.

    The field is what follows the FIRST ``#``; ``None`` when there is none. The
    name is returned as written: whether it is storable is the caller's check,
    the same one a plain reference gets.
    """
    name, sep, field = ref.partition(REFERENCE_FIELD_SEPARATOR)
    return (name, field) if sep else (ref, None)


def resolve_field(item_type: str, plaintext: str, field: Optional[str]) -> str:
    """The value a reference to *field* of this item means.

    *plaintext* is the decrypted entry. A ``None`` field is the type's default.
    Raises :class:`ItemError` for a field the type does not have, so a reference
    fails closed rather than resolving to something else.
    """
    _check_type(item_type)
    wanted = DEFAULT_FIELD[item_type] if field is None else field
    if wanted not in REFERENCE_FIELDS[item_type]:
        raise ItemError(
            "unknown_field",
            f"a {item_type} item has no field {wanted!r}; "
            f"one of {sorted(REFERENCE_FIELDS[item_type])}",
        )
    if item_type == ITEM_TYPE_SECRET:
        return plaintext
    fields = decode_item(item_type, plaintext)
    if item_type == ITEM_TYPE_NOTE:
        return str(fields.get(FIELD_TEXT, ""))
    if wanted == FIELD_URL:
        urls = fields.get("urls") or []
        return str(urls[0]) if urls else ""
    return str(fields.get(wanted, ""))


# ── Encoding ──


def _check_type(item_type: str) -> None:
    if item_type not in ITEM_TYPES:
        raise ItemError(
            "unknown_type", f"unknown item type {item_type!r}; one of {sorted(ITEM_TYPES)}"
        )


def _text(
    fields: Mapping[str, Any], key: str, *, required: bool = False, bounded: bool = True
) -> str:
    value = fields.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ItemError("invalid_field_type", f"field {key!r} must be a string")
    if required and not value.strip():
        raise ItemError("missing_field", f"field {key!r} is required")
    if bounded and len(value) > MAX_FIELD_CHARS:
        raise ItemError(
            "field_too_long", f"field {key!r} is longer than {MAX_FIELD_CHARS} characters"
        )
    return value


def _urls(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ItemError("invalid_field_type", "field 'urls' must be a list")
    if len(raw) > MAX_LOGIN_URLS:
        raise ItemError("too_many_urls", f"a login may carry at most {MAX_LOGIN_URLS} URLs")
    out: list[str] = []
    for entry in raw:
        href = _text({"href": entry}, "href", required=True).strip()
        try:
            parts = urlsplit(href)
            hostname = parts.hostname
        except ValueError:
            # urlsplit refuses an unbalanced IPv6 bracket ("https://[::1").
            raise ItemError("invalid_url", "each URL must be an http(s) URL with a host") from None
        if parts.scheme not in ("http", "https") or not hostname:
            raise ItemError("invalid_url", "each URL must be an http(s) URL with a host")
        # A listing shows a login's URLs, so one may not carry a credential.
        if _has_userinfo(href):
            raise ItemError("invalid_url", "a URL must not carry a username or password")
        out.append(href)
    return out


def _has_userinfo(href: str) -> bool:
    """Whether *href* carries a username or password; an unparsable URL counts."""
    try:
        parts = urlsplit(href)
        return parts.username is not None or parts.password is not None
    except ValueError:
        return True


#: Per type, the fields a listing never shows. On an update of an item of the
#: same type, one of these left OUT of the request keeps its stored value, so
#: an edit form that cannot show them does not erase them. Sending ``notes``
#: as "" clears it; ``password`` and a note's ``text`` are required, so ""
#: for either is refused (``missing_field``).
KEPT_WHEN_ABSENT: dict[str, tuple[str, ...]] = {
    ITEM_TYPE_SECRET: (),
    ITEM_TYPE_LOGIN: (FIELD_PASSWORD, FIELD_NOTES),
    ITEM_TYPE_NOTE: (FIELD_TEXT,),
}


def keep_absent_fields(
    item_type: str, fields: Mapping[str, Any], previous: Mapping[str, Any]
) -> dict[str, Any]:
    """*fields* with each :data:`KEPT_WHEN_ABSENT` key it lacks taken from *previous*.

    *previous* is the stored item of the SAME type (:func:`decode_item`).
    """
    merged = dict(fields)
    for key in KEPT_WHEN_ABSENT[item_type]:
        if key not in merged and key in previous:
            merged[key] = previous[key]
    return merged


def validate_item(item_type: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical field dict for *item_type*, or :class:`ItemError`.

    Unknown keys are dropped and every kept value is a string of bounded
    length (a plain secret's value excepted). A ``secret`` needs a non-blank ``value``;
    a ``login`` needs a non-blank ``password``; a ``note`` needs ``text``.
    Values are kept as given (a credential may carry meaningful whitespace):
    only the emptiness check strips.
    """
    _check_type(item_type)
    if not isinstance(fields, Mapping):
        raise ItemError("invalid_fields", "fields must be an object")
    if item_type == ITEM_TYPE_SECRET:
        # Unbounded: a plain secret takes any length it always took.
        return {FIELD_VALUE: _text(fields, FIELD_VALUE, required=True, bounded=False)}
    if item_type == ITEM_TYPE_NOTE:
        return {FIELD_TEXT: _text(fields, FIELD_TEXT, required=True)}
    return {
        FIELD_USERNAME: _text(fields, FIELD_USERNAME),
        FIELD_PASSWORD: _text(fields, FIELD_PASSWORD, required=True),
        "urls": _urls(fields.get("urls")),
        FIELD_NOTES: _text(fields, FIELD_NOTES),
    }


def encode_item(item_type: str, fields: Mapping[str, Any]) -> str:
    """The plaintext to encrypt for a validated item (see :func:`validate_item`)."""
    canonical = validate_item(item_type, fields)
    if item_type == ITEM_TYPE_SECRET:
        return canonical[FIELD_VALUE]
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode_item(item_type: str, plaintext: str) -> dict[str, Any]:
    """The field dict a decrypted entry of *item_type* holds.

    A ``secret`` answers ``{"value": plaintext}``. A typed entry whose plaintext
    is not a JSON object is corrupt and raises :class:`ItemError`: the type is
    authenticated with the ciphertext, so this means the store was damaged,
    not that an older format is in use.
    """
    _check_type(item_type)
    if item_type == ITEM_TYPE_SECRET:
        return {FIELD_VALUE: plaintext}
    try:
        fields = json.loads(plaintext)
    except ValueError:
        fields = None
    if not isinstance(fields, dict):
        raise ItemError("corrupt_item", f"a {item_type} item's content is not an object")
    return fields


def public_view(item_type: str, plaintext: str) -> dict[str, Any]:
    """The part of an item a listing may show without revealing a credential.

    A login shows its username and URLs; a secret or a note shows nothing (the whole content is the credential).
    """
    if item_type != ITEM_TYPE_LOGIN:
        return {}
    fields = decode_item(item_type, plaintext)
    return {
        FIELD_USERNAME: str(fields.get(FIELD_USERNAME, "")),
        "urls": [
            u for u in fields.get("urls") or [] if isinstance(u, str) and not _has_userinfo(u)
        ],
    }

"""Conditional GET (``ETag`` / ``If-None-Match`` / 304) for dashboard media routes.

Four routes serve bytes the browser re-fetches on every dashboard load --
appearance-pack media, theme assets, app art, crew avatars -- and each one had
grown its own copy of the same three steps: mint a validator, compare it with
what the client sent, answer 304 or 200 with the same headers on both. The
copies drifted: some compared the raw ``If-None-Match`` string with ``==``,
which misses the weak ``W/"..."`` form a proxy may add and the list form a
browser sends when it holds several representations, so an unchanged body was
re-downloaded whenever the header was not byte-identical.

This module is the one implementation. Callers still decide the two things
that legitimately differ per route -- how the validator is derived (a content
digest, or ``inode-size-mtime`` for a streamed file) and how long the browser
may hold the bytes (``Cache-Control``) -- and pass them in.

The compare follows RFC 9110 section 13.1.2: ``If-None-Match`` uses the WEAK
comparison, so ``"x"`` and ``W/"x"`` match each other, ``*`` matches any
current representation, and a list matches when any member does. aiohttp's
parsed ``request.if_none_match`` already splits the list and strips the weak
marker, so the compare works on bare values.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from aiohttp import web

__all__ = [
    "bare_etag_value",
    "conditional_response",
    "is_not_modified",
    "strong_content_etag",
    "weak_content_etag",
]


def strong_content_etag(body: bytes) -> str:
    """A strong ``ETag`` header value for *body*: ``"<32 hex of sha256>"``.

    Strong means byte-for-byte: any change to the bytes changes the tag.
    """
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def weak_content_etag(body: bytes) -> str:
    """A weak ``ETag`` header value for *body*: ``W/"<16 hex of blake2b-8>"``.

    Weak is the honest label for a short digest of bytes that are replaced in
    place under the same URL (an installed theme pack): it says "equivalent
    representation", and the compare below treats weak and strong alike.
    """
    return f'W/"{hashlib.blake2b(body, digest_size=8).hexdigest()}"'


def bare_etag_value(etag: str) -> str:
    """The opaque value inside an ``ETag`` header: no ``W/`` marker, no quotes.

    This is the form aiohttp's parsed ``If-None-Match`` entries carry in
    ``.value``, so it is the form both sides of the compare use.
    """
    value = etag.strip()
    if value.startswith("W/"):
        value = value[2:]
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value


def is_not_modified(
    request: web.Request,
    etag: str,
    *,
    last_modified: float | None = None,
) -> bool:
    """True when the client already holds the representation *etag* names.

    ``If-None-Match`` decides whenever the header field is present (weak
    compare: ``*``, lists and ``W/`` forms all count). Empty and malformed
    fields do not match, but still suppress ``If-Modified-Since``. Only when
    the field is absent does ``If-Modified-Since`` apply (RFC 9110 section
    13.1.3), and only when the caller supplies the resource's *last_modified*
    as a POSIX timestamp. Both sides of that date compare are second-granular:
    HTTP dates carry no sub-second part, so the stored mtime is truncated
    before the compare rather than rounded.
    """
    if "If-None-Match" in request.headers:
        if_none_match = request.if_none_match
        if not if_none_match:
            return False
        wanted = bare_etag_value(etag)
        if len(if_none_match) == 1 and if_none_match[0].value == "*":
            return True
        return any(tag.value == wanted for tag in if_none_match)
    if last_modified is None:
        return False
    since = request.if_modified_since
    if since is None:
        return False
    return int(last_modified) <= since.timestamp()


def conditional_response(
    request: web.Request,
    body: bytes,
    content_type: str,
    *,
    etag: str,
    cache_control: str,
    extra_headers: Mapping[str, str] | None = None,
    charset: str | None = None,
) -> web.Response:
    """A 200 carrying *body*, or a body-less 304 when the client already has it.

    Both answers carry the same ``ETag``, ``Cache-Control``,
    ``X-Content-Type-Options: nosniff`` and every entry of *extra_headers*, so
    a 304 never relaxes what the 200 promised (a route's CSP rides on both).
    ``nosniff`` is unconditional because every caller's ``content_type`` is
    one of a fixed allowlist -- picked by the file's extension, or (appearance
    sounds: ``appearance_packs.sounds.read_sound``) by sniffing the bytes'
    magic against that same allowlist -- so a manifest-supplied name never
    chooses the type, and the browser must not second-guess the choice.
    """
    headers = {
        "ETag": etag,
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
    }
    if extra_headers:
        headers.update(extra_headers)
    if is_not_modified(request, etag):
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type=content_type, charset=charset, headers=headers)

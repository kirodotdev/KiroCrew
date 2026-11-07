"""The vendored Tailwind v4 browser runtime, carried inside a document.

Widget and artifact HTML is styled by the ``@tailwindcss/browser`` runtime that the
frontend build stages at ``static/dist/vendor/tailwindcss-browser.js``
(``website/vite.config.ts`` emits it, ``website/src/lib/vendorPaths.ts`` names its
URL path). A document normally links it. Two places in the gateway put it inside
the document instead:

* ``publish_sync.wrap_widget_html``: a published widget is viewed away from the
  dashboard, where a link to the dashboard's origin resolves nowhere.
* ``dashboard.handlers.sandbox_doc``: a widget or artifact frame is sandboxed, so
  its document has an opaque origin and its own request for the runtime carries no
  cookie. A dashboard reached through a proxy that authenticates every request by
  cookie (a single-sign-on tunnel) refuses that request, and every Tailwind class in
  the frame does nothing. The frame's navigation to its document does carry the
  cookie, so a runtime that arrives inside the document does not depend on the
  proxy at all.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: The staged runtime. ``publish_sync`` reads the same file.
TAILWIND_RUNTIME_FILE = (
    Path(__file__).resolve().parent / "static" / "dist" / "vendor" / "tailwindcss-browser.js"
)

#: Matches a raw-text script terminator in any ASCII casing, capturing the tag
#: name so a substitution can preserve it. HTML tokenization compares
#: ``</script`` case-insensitively, so a lowercase-only escape lets ``</SCRIPT>``
#: close an inlined script early; preserving the matched casing keeps the
#: neutralised text byte-identical to the source once the browser reads ``<\/``
#: back as ``/``. ``re.ASCII`` bounds the folding to ASCII: Python otherwise
#: folds U+017F onto ``s`` and U+0131/U+0130 onto ``i``, so ``</ſcript>`` — which
#: the HTML tokenizer does NOT treat as a terminator — would take a stray
#: backslash and could corrupt a raw string literal in the bundle.
SCRIPT_CLOSE_RE = re.compile(r"</(script)", re.IGNORECASE | re.ASCII)

#: The end of a document's head. Only the head is searched for the runtime tag:
#: ``buildSrcdoc`` puts the tag there and puts the model's HTML in the body.
_HEAD_END_RE = re.compile(rb"</head\s*>", re.IGNORECASE)

#: The runtime tag ``buildSrcdoc`` (``website/src/lib/widgetSrcdoc.ts``) emits:
#: ``<script src="<origin><base>/vendor/tailwindcss-browser.js"
#: crossorigin="anonymous" onerror="..."></script>``, or with ``nonce="..."`` in
#: place of ``onerror`` when the page's own scripts are blocked. The origin, a
#: base path and the other attributes may vary.
#: ``test/test_tailwind_runtime.py`` renders the tag from the TypeScript source,
#: so a change to its shape on either side fails a test instead of quietly
#: linking the runtime again. Opening tags are matched as disjoint candidates,
#: then each candidate's first ``src`` and adjacent closing tag are checked once;
#: no candidate can make a later attribute rescan the rest of the tag.
_SCRIPT_OPEN_TAG_RE = re.compile(rb"<script\b[^<>]*>", re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(rb'\ssrc="([^"]*)"', re.IGNORECASE)
_SCRIPT_END_TAG_RE = re.compile(rb"\s*</script\s*>", re.IGNORECASE)
_RUNTIME_SRC_SUFFIX = b"/vendor/" + TAILWIND_RUNTIME_FILE.name.encode("ascii")

#: A ``nonce`` attribute on the matched tag. When the document's CSP admits
#: scripts by nonce instead of ``'unsafe-inline'``, the inline copy must carry the
#: same nonce or the browser refuses it. The value ``buildSrcdoc`` mints is hex;
#: anything outside the base64 alphabet a CSP nonce may use is not copied.
_NONCE_ATTR_RE = re.compile(rb'\snonce="([^"]*)"', re.IGNORECASE)
_NONCE_VALUE_RE = re.compile(rb"[A-Za-z0-9+/_=-]{1,256}")

_lock = threading.Lock()
#: The last bundle read, as ``(identity, script body)``, so serving a frame costs
#: a stat rather than a 260 KB read. Keyed on the file's identity, not cached for
#: the life of the process: a frontend rebuild restages the bundle while the
#: gateway runs, and a cache filled before the first build would pin the missing
#: state.
_cached: tuple[tuple[str, int, int, int], bytes | None] | None = None


def script_text(js: str) -> str | None:
    """``js`` made safe to put between ``<script>`` and ``</script>``, or None.

    Every casing of ``</script`` is neutralised (see :data:`SCRIPT_CLOSE_RE`).
    ``<!--`` is refused rather than escaped: after it, a later ``<script`` puts
    the HTML tokenizer in its double-escaped state, where the closing
    ``</script>`` does not end the element and the rest of the document is
    read as script. No rewrite of it is valid in every JavaScript context
    (``<\\!--`` is a syntax error inside a ``u``-flag regular expression), and
    the bundle contains none today; refusing turns a dependency bump that adds
    one into a linked runtime instead of a broken document.
    """
    if "<!--" in js:
        logger.warning("Tailwind runtime contains '<!--'; it will not be inlined")
        return None
    return SCRIPT_CLOSE_RE.sub(lambda m: "<\\/" + m.group(1), js)


def _script_body() -> bytes | None:
    """The staged runtime made safe for a ``<script>`` element, UTF-8 encoded."""
    global _cached
    path = TAILWIND_RUNTIME_FILE
    try:
        st = path.stat()
    except OSError:
        return None
    identity = (str(path), st.st_ino, st.st_mtime_ns, st.st_size)
    with _lock:
        if _cached is not None and _cached[0] == identity:
            return _cached[1]
    body: bytes | None = None
    try:
        text = script_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Tailwind runtime %s unreadable (%s); it will not be inlined", path, exc)
    else:
        if text:
            body = text.encode("utf-8")
    with _lock:
        _cached = (identity, body)
    return body


def runtime_script_tag(nonce: bytes | None = None) -> bytes | None:
    """The staged runtime as one inline ``<script>`` element, UTF-8 encoded,
    carrying ``nonce`` when one is given.

    None when the bundle is missing (an unbuilt checkout), unreadable, empty, or
    cannot sit inside a ``<script>`` element (see :func:`script_text`).
    """
    body = _script_body()
    if body is None:
        return None
    open_tag = b'<script nonce="' + nonce + b'">' if nonce else b"<script>"
    return open_tag + body + b"</script>"


def _runtime_tag_span(doc: bytes, head_end: int) -> tuple[int, int, bytes] | None:
    """Return the runtime tag's span and opening tag within ``doc``'s head."""
    for opening in _SCRIPT_OPEN_TAG_RE.finditer(doc, 0, head_end):
        open_tag = opening.group(0)
        src_attr = _SCRIPT_SRC_RE.search(open_tag)
        if src_attr is None:
            continue
        src = src_attr.group(1)
        if any(char in src for char in (b'"', b"<", b">")):
            continue
        if not src.endswith(_RUNTIME_SRC_SUFFIX):
            continue
        closing = _SCRIPT_END_TAG_RE.match(doc, opening.end(), head_end)
        if closing is not None:
            return opening.start(), closing.end(), open_tag
    return None


def inline_into_document(doc: bytes) -> bytes:
    """``doc`` with the runtime its head links replaced by an inline copy.

    The copy keeps the linked tag's ``nonce``, so a document whose CSP admits
    only nonce-bearing scripts still runs it. Returned unchanged when the head
    links no runtime, when that tag's nonce is not a plain CSP nonce, or when
    the staged bundle cannot be inlined. The link then loads as it always did,
    which works wherever the frame's request needs no cookie.
    """
    head_end = _HEAD_END_RE.search(doc)
    if head_end is None:
        return doc
    match = _runtime_tag_span(doc, head_end.start())
    if match is None:
        return doc
    start, end, open_tag = match
    nonce: bytes | None = None
    nonce_attr = _NONCE_ATTR_RE.search(open_tag)
    if nonce_attr is not None:
        if _NONCE_VALUE_RE.fullmatch(nonce_attr.group(1)) is None:
            return doc
        nonce = nonce_attr.group(1)
    tag = runtime_script_tag(nonce)
    if tag is None:
        return doc
    return doc[:start] + tag + doc[end:]

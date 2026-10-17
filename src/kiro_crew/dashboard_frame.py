"""The dynamic dashboard's frame: the host owns the document, the template owns the page.

A template's ``template.html`` is a body fragment and it may run JS. That is a
decided point of contract v3 and it is what makes a chart possible -- the host
cannot draw one for a template it has never seen, and a page that can only print
text cannot be a dashboard. So the fragment is treated the way dsh-visualize
treats a visualization: the page author writes the body, and the host writes
everything around it.

Three ways a value reaches the page, and the page chooses per element
--------------------------------------------------------------------
1. ``data-dashboard-field="<name>"`` on an element: the host sets that element's
   TEXT to the field's value. No markup, no attributes, no HTML -- a text node, so
   a value can never become an element. This is the path for every cell a reader
   reads.
2. ``window.kirocrew.fields`` in a script: the same values as data, for a chart to
   draw from. :data:`WINDOW_GLOBAL` also carries ``agentic`` (the names the
   crewmate wrote rather than the gateway folded), ``seq`` (the fold watermark) and
   ``stale``.
3. ``window.kirocrew.subscribe(name, cb)``: ``cb(value, name)`` on first load and
   again on every push that carries that field, returning a disposer. For a page
   that DRAWS -- a tree, a chart, a scene -- where (2) is a value read once and the
   generic :data:`PAGE_EVENT` only says that something landed, leaving the page to
   diff a whole read to find out what.

The host fills (1) itself, from the same object it hands to (2) and (3), so a page
cannot show one number in a cell and a different one in its chart.

What the host refuses to let the page do
----------------------------------------
:data:`FRAME_CSP` closes egress (``connect-src 'none'``) and grants no dynamic-exec
primitive, over a frame the dashboard mounts with ``allow-scripts`` and nothing
else -- an opaque origin with no access to the dashboard's cookies, storage or DOM.
A fixed two-origin CDN allowlist carries Chart.js and D3 and can be used for
nothing else, because nothing can be sent to it.

Egress is closed harder here than a chat card needs it closed, and the reason is
what this page HOLDS: real numbers out of the operator's own crew log, including a
work ledger and a crewmate's mistake book. A page that can both read those and open
a channel is an exfiltration path with a data source attached.

Staleness and the agentic marker
--------------------------------
A field whose source did not resolve is reported, not guessed at. The page keeps
the last values that did resolve and the host shows a band saying the data is
stale -- a half-filled page is the one state a status surface must never reach,
because a reader cannot tell it from a current one. Agentic values are marked for
the same reason at a smaller scale: a number the crewmate asserted and a number the
log recorded are different kinds of claim, and a reader deciding whether to act on
one is entitled to know which it is looking at.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Final, Mapping

logger = logging.getLogger(__name__)

#: The size ceiling for one template page, in bytes of UTF-8.
#:
#: 3 MiB, because a hand-written page that inlines a client-side library -- a 3D
#: renderer, a diagram engine -- together with its own scene code runs to a few MB of
#: layout with no data in it. Measured in BYTES, not characters, so a multi-byte page
#: cannot pass at several times the ceiling.
#:
#: Measured on the PAGE, before :func:`compose` wraps it. What composing adds -- about
#: 17 KB of host markup and bootstrap, plus the data island -- is held where the whole
#: document is: the browser wraps the composed body in its own head (``buildSrcdoc`` in
#: ``website/src/lib/widgetSrcdoc.ts``, a few KB more) and mints it through
#: :mod:`kiro_crew.dashboard.handlers.sandbox_doc`, which holds up to 8 MiB across all
#: the documents in flight at once. That is room for a page at this ceiling, what
#: composing and the browser add around it, and a data island of 4 MiB
#: (``MAX_MODEL_FIELDS`` fields of ``MAX_ENTRY_BYTES`` each), with about 1 MiB over;
#: ``test_a_dashboard_page_at_the_frames_ceiling_fits_one_document`` pins it. An island
#: has no bound of its own, so a composed document over 8 MiB is refused at the mint.
MAX_PAGE_BYTES: Final[int] = 3 * 1024 * 1024

#: Origins a page may load static resources from.
#:
#: FIXED, and not configuration. An operator-editable allowlist would make the
#: containment story depend on a setting -- and on the reader finding that setting
#: before they can trust the frame. Both entries carry the libraries a dashboard
#: actually needs (Chart.js, D3) and neither can be reached for egress:
#: ``connect-src`` is closed, so these origins serve scripts and nothing sends.
#:
#: PINNED EQUAL to the browser wrapper's own ``script-src`` list in
#: ``website/src/lib/widgetSrcdoc.ts``. The two policies intersect, so an origin
#: named only here is still blocked and the page fails to load its chart library
#: with no clue why -- ``test_the_cdn_allowlist_matches_the_browser_wrapper`` is
#: what keeps them one list in two files.
RESOURCE_ORIGINS: Final[tuple[str, ...]] = (
    "https://cdn.jsdelivr.net",
    "https://cdnjs.cloudflare.com",
)

_RESOURCE_SOURCES = " ".join(RESOURCE_ORIGINS)

#: The composed document's own Content-Security-Policy.
#:
#: Shaped on dsh-visualize's ``FRAME_CSP`` and tightened in two places this surface
#: differs from a chat card:
#:
#: * ``connect-src 'none'`` rather than ``blob: data:`` -- see the module docstring.
#: * no ``'unsafe-eval'`` and no ``'wasm-unsafe-eval'``. Chart.js and D3 need
#:   neither (the browser wrapper's own policy already withholds them and says so),
#:   and withholding them denies a page any dynamic-exec primitive.
#:
#: ``'unsafe-inline'`` stays on script and style: inline markup and inline script
#: ARE the format, and a nonce cannot be given to a file the host did not write.
#:
#: Injected INSIDE the document as well as by the browser wrapper outside it. Not
#: redundancy by accident: policies combine by intersection, so the stricter of the
#: two wins on every directive and this one survives a future caller that renders a
#: composed document through a laxer wrapper.
FRAME_CSP: Final[str] = "; ".join(
    (
        "default-src 'none'",
        f"script-src 'unsafe-inline' {_RESOURCE_SOURCES}",
        f"style-src 'unsafe-inline' {_RESOURCE_SOURCES}",
        "img-src data: blob:",
        "font-src data:",
        "connect-src 'none'",
        "frame-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    )
)

#: The global a page reads its values from.
WINDOW_GLOBAL: Final[str] = "kirocrew"

#: The binding attribute, MIRRORED from ``dashboard_templates.parity._BINDING``.
#:
#: Mirrored rather than imported because this module is reachable from the panel
#: read path and ``parity`` pulls in the html parser and the contract machinery for
#: a dev-time gate. ``test_the_binding_attribute_matches_parity`` pins the two
#: equal, so the gate that checks a template's bindings and the frame that fills
#: them cannot disagree about what a binding is.
BINDING_ATTRIBUTE: Final[str] = "data-dashboard-field"

_DATA_ELEMENT_ID = "kirocrew-dashboard-data"

#: Document-skeleton tags a page must not carry.
#:
#: Taken from dsh-visualize's ``SKELETON_TAG`` regex. The host supplies the
#: skeleton, so a page that ships one would nest documents -- and could carry its
#: own CSP ``<meta>``. That is not a bypass (a policy cannot be loosened by a second
#: meta tag) but it is still a page that renders wrong, and refusing it loudly while
#: the author is present is the whole point.
_SKELETON_TAG_RE = re.compile(r"<!doctype\b|<\s*(?:html|head|body)\b", re.IGNORECASE)


class PageError(ValueError):
    """A template page the host refuses. ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_page(html: Any) -> int:
    """Check one template page against the inline contract; return its UTF-8 size.

    Every refusal says how to act, because the party that reads it is the one that
    can fix it -- a template author at review time, or an agent that will get one
    more try.
    """
    if not isinstance(html, str):
        raise PageError("page_not_a_string", "the page must be a string of HTML")
    if not html.strip():
        raise PageError(
            "page_empty",
            "the page is empty: send the inline HTML, with one "
            f"{BINDING_ATTRIBUTE} element per field",
        )
    size = len(html.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise PageError(
            "page_too_large",
            f"the page is {size} bytes, over the {MAX_PAGE_BYTES}-byte limit -- "
            "shrink the inlined markup, or read the data from "
            f"window.{WINDOW_GLOBAL}.fields instead of inlining it",
        )
    # A document tag inside a <script> or <style> element's text is not markup --
    # minified ECharts carries "<body" in a string literal -- so look for the
    # skeleton tag only in what falls OUTSIDE the code spans code_spans() reports.
    # Where that scan stops early (an unterminated element) the remainder has no
    # span and is scanned, so a real document tag is refused, never under-refused.
    spans = code_spans(html)
    skeleton = next(
        (
            m.group(0)
            for m in _SKELETON_TAG_RE.finditer(html)
            if not any(start <= m.start() < end for start, end in spans)
        ),
        None,
    )
    if skeleton is not None:
        raise PageError(
            "page_has_skeleton",
            f"the page contains the document tag {skeleton!r} -- write only "
            "the inline body; the host supplies <!doctype>, <html>, <head> and "
            "<body>, and owns the page's security policy",
        )
    return size


#: HTML white space. Not ``\s``: the tokenizer splits on these five characters only, so
#: ``<script`` followed by U+00A0 is another element, and reading it as a script would hand
#: the text after it to a relaxed rule.
_WS = r"\t\n\f\r "

#: ASCII case folding only, as the tokenizer does. Python's Unicode folding reads U+017F as
#: an ``s``, so ``<\u017fcript>`` would be a script to the scan and plain text to a browser.
_ASCII_FOLD = re.IGNORECASE | re.ASCII
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

#: Elements whose content the tokenizer reads as raw text, or as character data for
#: ``textarea`` and ``title``, up to their own end tag. A ``<script`` inside one is text.
#: ``plaintext`` has no end tag, and ``noscript`` is raw text only while scripting is on,
#: which a frame without ``allow-scripts`` is not, so the scan stops at both.
_RAW_TEXT_NAMES = (
    "script",
    "style",
    "textarea",
    "title",
    "xmp",
    "iframe",
    "noembed",
    "noframes",
    "noscript",
    "plaintext",
)
_RAW_END = {name: re.compile(rf"</{name}(?=[{_WS}/>])", _ASCII_FOLD) for name in _RAW_TEXT_NAMES}
_CODE_ELEMENT_START = re.compile(rf"<(?:script|style)(?=[{_WS}/>])", _ASCII_FOLD)
_COMMENT_END = re.compile(r"--!?>")

#: What can change where a script ends: ``<!--`` escapes the text after it, ``<script``
#: inside an escape nests, ``-->`` leaves the escape, and ``</script`` ends the element or,
#: nested, only the nesting.
_SCRIPT_MARKER = re.compile(rf"<!--|-->|<script(?=[{_WS}/>])|</script(?=[{_WS}/>])", _ASCII_FOLD)

#: Elements inside which the tree builder, not the tokenizer, decides how a tag reads:
#: ``<svg>`` and ``<math>`` switch to foreign content, where ``<script>``, ``<style>`` and
#: ``<title>`` are ordinary elements, ``<select>`` ignores some start tags in some browsers,
#: and ``<template>`` has insertion modes of its own, one of which (a column group inside it)
#: ignores ``<style>`` and ``<script>``. A simple ``<svg>`` or ``<select>`` is skipped whole;
#: any other, and every ``<math>`` and ``<template>``, ends the scan.
_MODAL_NAMES = ("svg", "math", "select", "template")

#: More that a simple element may not hold: ``<desc>`` and ``<foreignObject>`` switch the
#: mode back inside ``<svg>``.
_NESTED_STOPPERS = ("desc", "foreignobject")

_TAG_NAME = rf"[A-Za-z][^{_WS}/>]*+"


def _attributes(value: str) -> str:
    """The attributes of a tag as the tokenizer reads them, each value matching *value*.

    Possessive throughout, so a tag that does not fit fails in time linear in its length.
    """
    gap = rf"[{_WS}]*+"
    separator = rf"[{_WS}/]++"
    attribute = rf"[^{_WS}/>=]++(?:{gap}={gap}(?:{value}))?+"
    return rf"(?:{separator}|{attribute})*+"


#: A value as the tokenizer ends it: quoted, which may hold anything, or bare, which runs
#: to white space or ``>`` and may not start with a quote.
_ANY_VALUE = rf"\"[^\"]*+\"|'[^']*+'|(?![\"'])[^{_WS}>]*+"

#: Any start or end tag.
_TAG = re.compile(rf"</?+(?P<name>{_TAG_NAME}){_attributes(_ANY_VALUE)}>", _ASCII_FOLD)

#: What the bulk run below may take as a value: one the tokenizer ends at the same place. A
#: quoted value without ``<``, or a bare one that is followed by white space or ``>``. A bare
#: value runs on through quotes and ``=``, so one cut short by either would leave the rest to
#: be read as more attributes, and the tag to end somewhere the tokenizer does not end it.
_PLAIN_VALUE = rf"\"[^\"<]*+\"|'[^'<]*+'|[^{_WS}\"'<>=`]++(?=[{_WS}>])"


def _plain_tag(names: tuple[str, ...]) -> str:
    """An ordinary tag: a name that is none of *names*, and only values the bulk run may take."""
    guard = rf"(?!(?:{'|'.join(names)})(?![^{_WS}/>]))"
    return rf"</?+{guard}{_TAG_NAME}{_attributes(_PLAIN_VALUE)}>"


_ORDINARY_HERE = _plain_tag(_RAW_TEXT_NAMES + _MODAL_NAMES)
_ORDINARY_INSIDE = _plain_tag(_RAW_TEXT_NAMES + _MODAL_NAMES + _NESTED_STOPPERS)

#: A run of text and ordinary tags, which the scan skips in one match. Whatever it cannot
#: take (a comment, a special element, an odd tag) the scan reads one construct at a time,
#: so this only makes the common page fast and never changes what the scan finds.
_PLAIN_RUN = re.compile(rf"(?:[^<]++|{_ORDINARY_HERE})++", _ASCII_FOLD)


def _region(name: str) -> "re.Pattern[str]":
    """The rest of a simple ``<name>`` element: text and ordinary tags up to its end tag.

    With no special element inside, the tokenizer stays in its data state throughout and
    the first end tag closes the element. Anything else makes the pattern fail, and the
    scan stops.
    """
    end = rf"</{name}(?![^{_WS}/>]){_attributes(_PLAIN_VALUE)}>"
    return re.compile(rf"(?:[^<]++|{_ORDINARY_INSIDE})*+{end}", _ASCII_FOLD)


_SIMPLE_REGION = {"svg": _region("svg"), "select": _region("select")}

#: Constructs the scan reads before it gives up. Comments, special elements and odd tags
#: each cost one; ordinary markup costs nothing, so a real page uses a few hundred.
_MAX_SCAN_STEPS: Final[int] = 50_000


def _script_end(html: str, start: int) -> int | None:
    """Where the script data that begins at *start* ends, as the tokenizer's escape states place it.

    ``<!--`` escapes the text that follows and ``-->`` ends the escape. Inside an escape
    ``<script`` nests, and its ``</script`` ends the nesting rather than the element. The
    element ends at the first ``</script`` outside a nesting, or never (``None``). A scan
    that stopped at the first ``</script`` would resume reading markup inside script data.
    """
    state = 0  # 0 script data, 1 escaped, 2 double escaped
    pos = start
    while (marker := _SCRIPT_MARKER.search(html, pos)) is not None:
        text = marker.group()
        pos = marker.end()
        if text == "-->":
            state = 0
        elif text == "<!--":
            state = state or 1
            pos -= 2  # its two dashes also begin a ``-->``
        elif text[1] == "/":
            if state != 2:
                return marker.start()
            state = 1
        elif state == 1:
            state = 2
    return None


def _read_construct(html: str, pos: int) -> tuple[int, tuple[int, int] | None] | None:
    """Read the markup that starts at the ``<`` at *pos*.

    ``(next_pos, span)``, *span* being the text of the script or style element this tag
    opens. ``None`` when the scan can go no further: a comment or tag that never ends, an
    element that is never closed, or a form whose reading depends on the document around
    it (``<script/>`` is empty inside ``<svg>`` and a script in HTML; ``<plaintext>``
    swallows the rest of the page; ``<noscript>`` is raw text with scripting on and markup
    with it off; ``<math>``, ``<template>`` and a complex ``<svg>`` or ``<select>`` read
    their children by the tree builder's rules).
    """
    if html.startswith("<!--", pos):
        body = pos + 4
        if html.startswith(">", body):
            return body + 1, None
        if html.startswith("->", body):
            return body + 2, None
        ended = _COMMENT_END.search(html, body)
        return (ended.end(), None) if ended is not None else None
    after = html[pos + 1 : pos + 2]
    letter = html[pos + 2 : pos + 3] if after == "/" else after
    if not (letter.isascii() and letter.isalpha()):
        if after in ("!", "?", "/"):
            # A doctype, a CDATA section, a processing instruction or a stray ``</``: a
            # comment up to the first ``>``.
            close = html.find(">", pos + 2)
            return (close + 1, None) if close != -1 else None
        return pos + 1, None
    tag = _TAG.match(html, pos)
    if tag is None:
        return None
    end = tag.end()
    if after == "/":
        return end, None
    name = tag.group("name").translate(_ASCII_LOWER)
    if name in _MODAL_NAMES:
        region = _SIMPLE_REGION.get(name)
        skipped = region.match(html, end) if region is not None else None
        return (skipped.end(), None) if skipped is not None else None
    if name not in _RAW_TEXT_NAMES:
        return end, None
    if name in ("plaintext", "noscript") or html.startswith("/>", end - 2):
        return None
    if name == "script":
        stop = _script_end(html, end)
    else:
        closing = _RAW_END[name].search(html, end)
        stop = closing.start() if closing is not None else None
    if stop is None:
        return None
    is_code = name in ("script", "style") and stop > end
    return stop, ((end, stop) if is_code else None)


def code_spans(html: str, limit: int | None = None) -> list[tuple[int, int]]:
    """``(start, end)`` of the text inside each ``<script>`` and ``<style>`` element of *html*.

    The text only: the start tag with its attributes and the end tag are markup. Spans are
    ordered, disjoint and never empty; *limit* stops the scan after that many.

    What a span licenses is a weaker rule over that text, so the scan reads the page as the
    HTML tokenizer does where that decides what is code: a ``<script`` inside a comment, a
    quoted attribute value or a ``<textarea>`` opens nothing, a ``>`` inside a quoted value
    does not end its tag, and a script ends where its escape states end it. Where the tree
    builder decides instead (``<svg>``, ``<math>``, ``<select>``, ``<template>``) it reads a
    simple ``<svg>`` or ``<select>`` whole and stops at any other, and at every ``<math>`` and
    ``<template>``. A page is read the same with scripting on and off, so it
    stops at ``<noscript>``, whose content is text in one and markup in the other. It stops,
    rather than guess, at anything it cannot read, and after ``_MAX_SCAN_STEPS`` constructs.
    Text past that point has no span, so a doubtful page gets more rules, never fewer.
    """
    spans: list[tuple[int, int]] = []
    if (limit is not None and limit <= 0) or _CODE_ELEMENT_START.search(html) is None:
        return spans
    size = len(html)
    pos = 0
    for _ in range(_MAX_SCAN_STEPS):
        run = _PLAIN_RUN.match(html, pos)
        if run is not None:
            pos = run.end()
        if pos >= size:
            break
        read = _read_construct(html, pos)
        if read is None:
            break
        pos, span = read
        if span is not None:
            spans.append(span)
            if limit is not None and len(spans) >= limit:
                break
    return spans


#: How many script and style texts :func:`map_page_text` leaves out of the markup function.
#: Each stretch between them is a separate call, so a page of thousands of tiny elements
#: would cost far more than one call over the whole page. Past this many, the rest of the
#: page goes to the markup function, which is the stricter of the two.
MAX_CODE_PIECES: Final[int] = 256


def map_page_text(html: str, markup: Callable[[str], str], code: Callable[[str], str]) -> str:
    """What *markup* makes of *html*, except that the text of its script and style elements is *code*'s.

    *markup* is the baseline: the answer for the whole page is its answer, and the one change
    allowed is to put *code*'s result for an element's text where *markup*'s was. So the page
    differs from the baseline only inside script and style text, and only by what *code* leaves
    out. A page with no such text, and a page *markup* does not change, never reaches *code*.

    The baseline is cut at the script and style text :func:`code_spans` finds in *html*, and
    the pieces are scrubbed one by one. A rule that reaches across a cut (a PEM block, a
    labelled value) makes the pieces differ from the baseline, and then the baseline is the
    answer. The browser reads the page this returns, so the result is scanned again, and unless
    the kept text is still exactly its script and style text the baseline is the answer as well.
    """
    baseline = markup(html)
    spans = code_spans(html, MAX_CODE_PIECES)
    if not spans or baseline == html:
        return baseline
    pieces: list[str] = []
    swapped: list[str] = []
    kept: list[tuple[int, int]] = []  # where each kept text sits in the joined pieces
    at = served_len = 0
    for (start, end), is_text in zip(
        (*spans, (len(html), len(html))), (*([True] * len(spans)), False)
    ):
        outside = html[at:start]
        if outside:
            scrubbed = markup(outside)
            pieces.append(scrubbed)
            swapped.append(scrubbed)
            served_len += len(scrubbed)
        if is_text:
            text = html[start:end]
            pieces.append(markup(text))
            kept_text = code(text)
            swapped.append(kept_text)
            kept.append((served_len, served_len + len(kept_text)))
            served_len += len(kept_text)
        at = end
    if "".join(pieces) != baseline:
        return baseline
    served = "".join(swapped)
    if code_spans(served, MAX_CODE_PIECES) != kept:
        return baseline
    return served


#: Escapes applied inside the data island's JSON string literals.
#:
#: Mirrors ``agent_panel._JSON_HTML_ESCAPES``, pinned equal by test. ``<`` is the
#: one that matters: a value holding ``</script>`` would end the island early and
#: everything after it would parse as markup -- inside a document whose body is
#: already template-authored, which is exactly where a second injection point must
#: not exist.
_JSON_HTML_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}


def escape_json_for_html(blob: str) -> str:
    """Make a JSON document safe to embed in an HTML script element.

    The output is still the SAME JSON: every replacement is one of JSON's own
    ``\\u`` escapes inside a string literal, so ``JSON.parse`` returns the original
    values and only the HTML parser's view changes.
    """
    for raw, escaped in _JSON_HTML_ESCAPES.items():
        blob = blob.replace(raw, escaped)
    return blob


def _escape_html_text(text: str) -> str:
    """Escape text going into element content or a ``<title>``."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#: The host's bootstrap: parse the island, freeze it onto ``window``, FILL the
#: bindings, mark the agentic cells, and raise the stale band.
#:
#: It runs BEFORE the page, and the order is load-bearing: a page's own inline
#: script runs during parse, so a bootstrap placed after it would leave the first
#: script reading ``undefined`` -- and a chart that cannot see its data on first
#: paint is a chart that draws nothing or, worse, invents a placeholder.
#:
#: The FILL therefore runs on ``DOMContentLoaded`` rather than immediately: the
#: elements it binds are parsed after this script. Both halves are here rather than
#: split so there is one place that knows the shape of the object.
#:
#: Values are written with ``textContent``. Never ``innerHTML``, and this is the
#: single line of this file that is a containment boundary rather than a rendering
#: choice: a fold value can hold any text the log recorded -- an issue title, a
#: review comment, a command's output -- and ``textContent`` is what keeps such a
#: string a string.
#:
#: FROZEN deeply, so the global is read-only in the sense the surface promises. The
#: freeze is cosmetic against a hostile page (it could draw anything regardless) and
#: load-bearing against a careless one, which is the realistic case: a chart library
#: handed a live array sorts it in place, and the next widget on the page would then
#: read an order the log never recorded.
#:
#: A STATIC string. Nothing template-authored and nothing fold-derived is
#: interpolated into it: the only dynamic bytes in this document are the island
#: (escaped above) and the body.
#: The flag the beacon reads. A page CAN overwrite it, which is fine: the beacon is a
#: health signal, not a security boundary -- a page that wanted to lie about loading
#: could post the ready message itself.
_INIT_FAILED_FLAG: Final[str] = "__kirocrewInitFailed"

#: Initialization-error tracking, installed BEFORE the page's own scripts.
#:
#: Separate ``<script>`` elements are independent: a top-level throw aborts the script
#: it happened in and the next one still runs. So the beacon cannot infer a healthy
#: page from the fact that it was reached, and a page whose chart library is missing
#: beacons exactly like a working one -- which promotes a blank page over the one on
#: screen, without the band that exists to say so.
#:
#: Only SCRIPT errors count. A resource error (a missing image) reaches window with
#: ``target`` set to the element, and blanking the page over a broken image would be a
#: worse answer than drawing it. ``unhandledrejection`` is included because a template
#: that initializes in a promise fails there rather than at top level.
_INIT_GUARD_JS: Final[str] = f"""
(function () {{
  window[{_INIT_FAILED_FLAG!r}] = false;
  addEventListener('error', function (event) {{
    if (event && event.target && event.target !== window) return;
    window[{_INIT_FAILED_FLAG!r}] = true;
  }}, true);
  addEventListener('unhandledrejection', function () {{
    window[{_INIT_FAILED_FLAG!r}] = true;
  }});
}})();
"""

_BOOTSTRAP_JS: Final[str] = f"""
(function () {{
  var el = document.getElementById({_DATA_ELEMENT_ID!r});
  var read = {{ fields: {{}}, agentic: [], seq: 0, stale: false, missing: [] }};
  try {{
    read = JSON.parse(el.textContent);
  }} catch (err) {{
    read.stale = true;
  }}
  function freeze(value) {{
    if (value === null || typeof value !== 'object') return value;
    for (var key of Object.keys(value)) freeze(value[key]);
    return Object.freeze(value);
  }}
  // ONE REGISTRY for the whole document's lifetime, held in this closure rather
  // than on the global the refill replaces. A page subscribes once, during parse,
  // and keeps drawing across every push: a registry living on the published object
  // would be discarded with it and every callback would silently stop.
  var subscribers = [];
  function deliver(entry, source) {{
    if (!entry.live) return;
    var fields = source && source.fields;
    if (!fields || typeof fields !== 'object') return;
    // PRESENCE, not truthiness. A field is ABSENT when its fold path did not
    // resolve, and `0`, `''` and `false` are values a fold legitimately holds --
    // reading them as absent would stop a counter's subscriber at exactly the
    // moment the counter reached zero.
    if (!Object.prototype.hasOwnProperty.call(fields, entry.name)) return;
    try {{
      entry.cb(fields[entry.name], entry.name);
    }} catch (err) {{
      // ONE SUBSCRIBER'S THROW COSTS ONLY ITSELF. Two blocks on one page redraw
      // from one push, and letting the first one's error abort the loop leaves the
      // second showing the value from before it -- a page of fields at two
      // different ages with nothing on it saying so.
    }}
  }}
  function notify(source) {{
    // A COPY of the list, because a callback may subscribe or dispose during
    // delivery and mutating the array under the loop would skip its neighbour.
    var current = subscribers.slice();
    for (var i = 0; i < current.length; i++) deliver(current[i], source);
  }}
  function subscribe(name, cb) {{
    if (typeof name !== 'string' || typeof cb !== 'function') return function () {{}};
    var entry = {{ name: name, cb: cb, live: true }};
    subscribers.push(entry);
    // FIRES NOW, against the read the page already holds. A page's inline script
    // runs during parse, before a single binding is filled, so a callback that only
    // fired on a push would draw nothing on first paint -- over a log that has the
    // numbers in it. This is what makes the callback a page's one entry point
    // rather than something it needs a binding beside.
    deliver(entry, read);
    return function () {{ entry.live = false; }};
  }}
  // The push version the page HOLDS, taken from the ISLAND rather than started at
  // zero: the document is composed from a full read that already carries one, so a
  // page that began at zero would accept the first patch whatever its version and
  // apply a frame computed against a read it never saw.
  var held = typeof read.version === 'number' ? read.version : 0;
  // WHOSE COUNTER `held` BELONGS TO. Versions are only comparable inside one armed
  // page: a page the host re-arms starts its counter again, so a lower number can
  // belong to a newer read. The generation says which counter a number came from, and
  // the fence below compares numbers only when both carry the same one.
  var generation = typeof read.generation === 'string' ? read.generation : '';
  function mergePatch(base, patch) {{
    // A PATCH CARRIES WHAT MOVED and says nothing about what did not, so the fields
    // it omits are kept. Without this a push of one field would leave the global
    // holding that field alone -- a chart reading a second one would find
    // `undefined` and the host's own fill would mark every other cell missing, so
    // the page would go blank one field at a time with no state saying why.
    var fields = {{}};
    var key;
    var from = (base && base.fields) || {{}};
    for (key of Object.keys(from)) fields[key] = from[key];
    var moved = patch.fields || {{}};
    for (key of Object.keys(moved)) fields[key] = moved[key];
    return {{
      fields: fields,
      // The REST of the read is the patch's, not merged: `stale`, `missing` and the
      // agentic marks are statements about the read as a whole, and a merge of two
      // of them would describe neither. The host sends them on every patch for that
      // reason.
      agentic: patch.agentic || [],
      seq: typeof patch.seq === 'number' ? patch.seq : 0,
      stale: patch.stale === true,
      missing: patch.missing || [],
      written_at: patch.written_at || {{}},
      locale: typeof patch.locale === 'string' ? patch.locale : (base || {{}}).locale,
      version: typeof patch.version === 'number' ? patch.version : 0,
      generation:
        typeof patch.generation === 'string' ? patch.generation : (base || {{}}).generation,
    }};
  }}
  function publish(value) {{
    // `subscribe` travels ON the published object, because the object is replaced
    // wholesale by every refill: a page that read `window.kirocrew.subscribe` on
    // first load and called it after a push would otherwise meet `undefined`. The
    // function is the same one every time, so two blocks subscribing from either
    // side of a push land in one registry.
    value.subscribe = subscribe;
    // CONFIGURABLE, which looks like a weakening and is not. The REFILL below has
    // to redefine this property, so a non-configurable definition here makes the
    // first refill throw -- and the same throw hits any host that puts a second
    // document in one window, where the first document's definition is still
    // standing. What protects the values is the deep freeze, not the descriptor: a
    // page that wanted to lie about its numbers could draw anything it liked
    // regardless, so an attribute that only breaks the refill buys nothing.
    Object.defineProperty(window, {WINDOW_GLOBAL!r}, {{
      value: freeze(value),
      writable: false,
      configurable: true,
      enumerable: true,
    }});
  }}
  publish(read);
  function text(value) {{
    if (value === null || value === undefined) return '\\u2014';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }}
  function fill() {{
    var agentic = {{}};
    for (var i = 0; i < read.agentic.length; i++) agentic[read.agentic[i]] = true;
    var nodes = document.querySelectorAll('[{BINDING_ATTRIBUTE}]');
    for (var n = 0; n < nodes.length; n++) {{
      var node = nodes[n];
      var name = node.getAttribute({BINDING_ATTRIBUTE!r});
      if (!Object.prototype.hasOwnProperty.call(read.fields, name)) {{
        // NOT CLEARED. A field whose source did not resolve keeps whatever the
        // page last showed, which on a refill is the last value that DID resolve.
        // Blanking it would turn one unresolved path into a page of em dashes and
        // make a stale dashboard look like an empty one.
        node.setAttribute('data-dashboard-missing', 'true');
        continue;
      }}
      node.removeAttribute('data-dashboard-missing');
      // Text, never markup. See _BOOTSTRAP_JS's own note in dashboard_frame.py.
      node.textContent = text(read.fields[name]);
      if (agentic[name]) node.setAttribute('data-dashboard-agentic', 'true');
      else node.removeAttribute('data-dashboard-agentic');
    }}
    document.documentElement.setAttribute(
      'data-dashboard-stale', read.stale ? 'true' : 'false'
    );
    // A host that wraps compose_body writes its own root element, so the reader's
    // language is set here too, from the same read the page picks its words by.
    if (typeof read.locale === 'string' && read.locale) {{
      document.documentElement.lang = read.locale;
    }}
  }}
  // THE REFILL, which is what makes this a live dashboard rather than a snapshot.
  // The host posts a new read when a fold advances; the page is not reloaded, so a
  // chart keeps its state and its animation instead of flashing back to empty.
  addEventListener('message', function (event) {{
    // THE SENDER IS CHECKED FIRST, before the type. The only sender entitled to
    // refill this page is the window that minted it: this frame runs with
    // `allow-scripts`, and so does every other frame the dashboard embeds, so a
    // sibling that runs script of its own can reach `parent.frames[i]` and post
    // here. Without this test any such frame could replace every recorded cell on
    // the page with values it chose, and the page would present them exactly as it
    // presents the host's -- which is the one property the fold-sourced fields
    // exist to guarantee. `parent` is the opaque cross-origin reference a sandboxed
    // frame still gets, and identity comparison on it is valid.
    if (event.source !== parent) return;
    var data = event.data;
    if (!data || data.type !== {'"kirocrew-dashboard:data"'}) return;
    if (!data.read || typeof data.read !== 'object') return;
    var arrived = data.read;
    var version = typeof arrived.version === 'number' ? arrived.version : 0;
    var arrivedGeneration = typeof arrived.generation === 'string' ? arrived.generation : '';
    // A SUPERSEDED FULL READ, dropped. Two gaps can be in flight at once, their
    // answers can arrive in either order, and a full read REPLACES -- so the older
    // answer puts back the values the newer one already moved past, and reports
    // `stale: false` while doing it, which is the one case where nothing on the page
    // says the number is behind. Only the full read is fenced: a patch out of order is
    // the gap check's business one line below, and it asks for a fresh read.
    //
    // Same generation only. A re-armed page restarts the counter, so its first read is
    // newer than this one despite a smaller number -- fencing across generations would
    // leave that tab unable to ever accept its own recovery.
    if (arrived.patch !== true && arrivedGeneration === generation && version < held) return;
    if (arrived.patch === true) {{
      // A GAP, and the one condition this frame cannot recover from on its own: the
      // patch is not the next one, so a frame was dropped and nothing on the page
      // would show it. NO EXEMPTION for a page holding zero: a gateway restart
      // restarts the counter, and the refetch this asks for answers with a full read
      // that replaces -- one extra round trip, where the exemption instead applied a
      // patch computed against a read this page never held.
      // A DIFFERENT GENERATION IS A GAP TOO, whatever the number says. The host can
      // replace the live page under an open tab -- a second tab's load disarms the old
      // one and arms a new one -- and the replacement's counter starts again, so its
      // patch 2 can look like the next one to a tab holding 1. Merging it would mix
      // fields computed from two different reads, which is the state the gap check
      // exists to refuse, so the same refetch answers it.
      if (version !== held + 1 || arrivedGeneration !== generation) {{
        // The literal, like its two siblings in this script: the named constants are
        // declared below it. `test_a_patch_that_skips_a_version_asks_for_a_full_read`
        // reads the posted message and compares it to REFETCH_MESSAGE_TYPE, so a
        // drift between the two fails rather than going quiet.
        parent.postMessage(
          {{ type: {'"kirocrew-dashboard:refetch"'}, held: held }}, '*'
        );
        // NOT APPLIED. Asking for the whole read and applying the patch anyway
        // would reach the same wrong page one round trip later.
        return;
      }}
      arrived = mergePatch(read, arrived);
    }}
    held = version;
    generation = arrivedGeneration;
    // The FROZEN global is replaced wholesale rather than mutated, for the reason
    // it is frozen: a page holding a reference to the old object keeps reading
    // consistent numbers from it, and nothing half-updates.
    read = arrived;
    publish(read);
    fill();
    // AFTER the fill, so a callback that reads its own bound element finds the text
    // the host wrote for this push rather than the previous one's. Before the
    // generic event, so a page using both hears the specific signal first.
    //
    // Notified with WHAT ARRIVED, never with the merged read: `deliver` fires on the
    // presence of a field, so handing it the merge would fire every subscriber on
    // every push and make the callback another spelling of the generic event.
    notify(data.read);
    window.dispatchEvent(new Event({'"kirocrew-dashboard"'}));
  }});
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', fill);
  else fill();
}})();
"""

#: The flag the beacon reports as ``refill``: did the PAGE register a listener that
#: a refill reaches?
_REFILL_FLAG: Final[str] = "__kirocrewPageRefills"

#: Whether the page's own script will redraw on a refill, recorded as it registers.
#:
#: The bootstrap's refill fills the bound cells of ANY page, but a page that draws
#: from ``window.kirocrew`` in its own script only shows the new values if it draws
#: again when one lands. The host cannot read that off the page, so it asks the page
#: by watching what it does: a page that listens for ``message`` or for the refill
#: event on ``window`` is one a refill reaches. Every builtin template does. A page
#: that does not is re-minted on a new read, which is what every page got before, so
#: a wrong NO costs a reload and never a stale value.
#:
#: Installed AFTER the bootstrap, so the bootstrap's own refill listener is not
#: counted, and BEFORE the page. The band script after the page registers through
#: the prototype, so it is not counted either. Not a security boundary: a page that
#: claims refills it does not handle only keeps its own numbers stale.
_REFILL_PROBE_JS: Final[str] = f"""
(function () {{
  window[{_REFILL_FLAG!r}] = false;
  var add = window.addEventListener;
  window.addEventListener = function (type) {{
    if ((this === window || this == null) &&
        (type === 'message' || type === {'"kirocrew-dashboard"'})) {{
      window[{_REFILL_FLAG!r}] = true;
    }}
    return add.apply(this == null ? window : this, arguments);
  }};
}})();
"""

#: The wire type of the host's refill message.
DATA_MESSAGE_TYPE: Final[str] = "kirocrew-dashboard:data"

#: The event the page may listen for to know a refill landed. A page drawing a chart
#: redraws on this rather than polling.
PAGE_EVENT: Final[str] = "kirocrew-dashboard"

#: What the page posts when it cannot place a patch in order: send the whole read.
#:
#: The ONLY thing that detects a dropped frame. A patch says what moved and nothing
#: about what did not, so a page that applied one out of order shows one field from
#: after a change and the rest from before it -- and no band, no marker and no seq says
#: so, because every value on the page did arrive from the host. The message carries
#: ``held``, the version the page has, so the host can answer with a full read rather
#: than re-deriving which patches to resend.
REFETCH_MESSAGE_TYPE: Final[str] = "kirocrew-dashboard:refetch"

#: The readiness beacon, which runs AFTER the page.
#:
#: Posted only when nothing in the document raised while it initialized, which is
#: exactly the condition worth reporting: a parse error, a missing CDN library or a
#: rejected initialization promise leaves the page blank with no error of its own,
#: and a blank page is indistinguishable from a healthy one from outside the frame.
#: Reaching this script says nothing on its own, because a throw aborts only the
#: script it happened in, so the condition is read from ``_INIT_GUARD_JS``'s flag --
#: installed before the page, so an error in the page's FIRST script is already
#: counted. Withholding the beacon is what makes the host keep the last page that
#: loaded and raise the band that says it did.
READY_MESSAGE_TYPE: Final[str] = "kirocrew-dashboard:ready"

_BEACON_JS: Final[str] = f"""
(function () {{
  var post = function () {{
    if (window[{_INIT_FAILED_FLAG!r}]) return;
    // `refill` tells the host it may post new values into this document instead of
    // loading a new one. See _REFILL_PROBE_JS.
    parent.postMessage({{
      type: {READY_MESSAGE_TYPE!r}, refill: window[{_REFILL_FLAG!r}] === true
    }}, '*');
  }};
  // One turn of the event loop after load, so an error thrown from a load handler
  // the page registered before this one is already recorded when the flag is read.
  var ready = function () {{ setTimeout(post, 0); }};
  if (document.readyState === 'complete') ready();
  else addEventListener('load', ready);
}})();
"""

#: The band the host draws when the data is stale.
#:
#: HOST MARKUP, above the page and never over it. Over the page it would cover
#: whatever the last good read had painted -- including the numbers the reader is
#: trying to judge -- so the honest shape is a band between the frame's top and the
#: template's own first element. A template cannot suppress it: it is not in the
#: fragment, and the page has no way to remove an element it did not create before
#: the reader sees it.
_STALE_BAND = (
    '<div id="kirocrew-stale-band" role="status" hidden>'
    #: The label says what the reader is looking at, not a property of the data: a
    #: word for the data's state names a condition the reader has no act for. The
    #: detail span carries which values are old and that the rest is current, so the
    #: label's whole job is that some of what is on screen is older than the rest.
    "<strong>Some numbers are older.</strong> "
    "<span data-stale-detail></span>"
    "</div>"
)

_HOST_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 12px; font: 13px/1.5 -apple-system, BlinkMacSystemFont,
  'Segoe UI', sans-serif; color: var(--text, #e6e6e6); background: var(--bg, #12141a); }
#kirocrew-stale-band { margin: 0 0 10px; padding: 6px 9px; border-radius: 6px;
  font-size: 11px; color: var(--warn, #ffd230);
  background: var(--warn-subtle, rgba(255, 210, 48, 0.1));
  border: 1px solid var(--warn, #ffd230); }
html[data-dashboard-stale='true'] #kirocrew-stale-band { display: block; }
[data-dashboard-agentic='true'] { border-bottom: 1px dotted var(--muted, #8b8b8b); }
[data-dashboard-missing='true'] { opacity: 0.55; }
"""

#: The band's own script, which reveals it and names what did not resolve.
#:
#: Separate from the bootstrap so the band is filled on every refill too: a read
#: that goes from stale to current must take the band away, and one that goes the
#: other way must raise it, or the band becomes a thing a reader learns to ignore.
_BAND_JS: Final[str] = f"""
(function () {{
  var band = document.getElementById('kirocrew-stale-band');
  if (!band) return;
  var detail = band.querySelector('[data-stale-detail]');
  function paint() {{
    var read = window[{WINDOW_GLOBAL!r}] || {{}};
    var missing = read.missing || [];
    band.hidden = !read.stale;
    if (!detail) return;
    //: NAMED, not just counted. "2 values are from an earlier read" leaves the reader
    //: scanning the page for which two, and the cells are already marked -- so the band
    //: says the names and the marks confirm them. Capped at three with the rest
    //: counted, because a band that grows with the failure stops being a band.
    var shown = missing.slice(0, 3).join(', ');
    var rest = missing.length - 3;
    if (rest > 0) shown += ' and ' + rest + ' more';
    detail.textContent = missing.length
      ? shown + (missing.length === 1 ? ' is' : ' are') +
        ' from an earlier read. Everything else on this page is current.'
      : 'Some values are from an earlier read. Everything else on this page is current.';
  }}
  // Through the prototype, so the refill probe does not count the host's own
  // listener as the page's.
  EventTarget.prototype.addEventListener.call(window, {PAGE_EVENT!r}, paint);
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', paint);
  else paint();
}})();
"""


#: The language a page renders its own words in when the host names none it ships.
DEFAULT_LOCALE: Final[str] = "en"


def page_locale(value: object) -> str:
    """The UI language a page may render in: a shipped catalog's tag, else English.

    The same gate the gateway uses for the persisted UI language, so a tag the app
    has no catalog for cannot reach a page as a language the chrome around it does
    not speak. Imported lazily: ``context`` is heavy and this module is on the
    panel read path.
    """
    from kiro_crew.context import normalize_ui_language_tag

    return normalize_ui_language_tag(value, source="dashboard locale") or DEFAULT_LOCALE


def read_payload(
    fields: Mapping[str, Any],
    agentic: list[str] | tuple[str, ...] = (),
    seq: int = 0,
    stale: bool = False,
    missing: list[str] | tuple[str, ...] = (),
    written_at: Mapping[str, str] | None = None,
    locale: object = DEFAULT_LOCALE,
    subject: str = "crewmate",
    last_user_at: object = "",
) -> dict[str, Any]:
    """The object the page sees as ``window.kirocrew``.

    ``subject`` says whose page this is -- ``crewmate`` or ``session`` -- so a template
    shared by both can word its own heading for the reader it is drawn for. Any other
    value is read as ``crewmate``.

    ONE builder for the initial document and for every refill, so the two cannot
    describe the same read differently -- which is the bug a reader cannot detect,
    because a page filled once and refilled later looks the same either way.

    ``missing`` is NAMED and not merely counted, because the band names the cells
    that kept their last value; a count could not say which, and a reader told
    "something is stale" on a page of twenty numbers has been told nothing.

    ``written_at`` is when each agentic cell was written, from the record. A page
    that draws a crewmate's JUDGMENT needs it: a judgment is only current if nothing
    has happened since, and a time inside the value would be the writer's own claim
    about its own freshness. Empty for a page with no agentic fields, and missing a
    name whose cell has never been written.

    ``locale`` is the reader's UI language, checked by :func:`page_locale`. A page
    picks its own words by it; the values in ``fields`` are never translated.

    ``last_user_at`` is when a PERSON last spoke in the chat this page belongs to --
    a typed message, an option clicked in the chat, a steer into a running turn --
    from the host's own record of that chat, as an aware UTC stamp in whole seconds,
    the precision ``written_at`` is spelled in. A page compares
    it with ``written_at`` to tell that the person wrote in the chat after a card was
    written, which nothing else on the page can see: those messages never pass
    through the page. ``""`` when the host has no stamp to give,
    for the reason ``written_at`` is an empty map rather than an absent key.
    """
    return {
        "fields": dict(fields),
        "agentic": sorted(agentic),
        "seq": int(seq),
        "stale": bool(stale),
        "missing": sorted(missing),
        "written_at": dict(written_at or {}),
        "locale": page_locale(locale),
        "subject": "session" if subject == "session" else "crewmate",
        "last_user_at": last_user_at if isinstance(last_user_at, str) else "",
    }


def compose(html: str, read: Mapping[str, Any], title: str = "") -> str:
    """Frame one template page as a complete, contained document.

    The assembly order IS the contract and each position is load-bearing:

    1. the host's ``<head>`` -- charset, viewport, no-referrer, :data:`FRAME_CSP`,
       an escaped title and the host stylesheet;
    2. the stale band, which is host markup above the page;
    3. the inert data island, then the bootstrap that freezes it onto ``window``
       and binds the fields -- both before the page, so the page's first inline
       script already sees its values;
    4. the page, verbatim, inside ``<body>``;
    5. the band's script and the readiness beacon, which only a page whose own
       scripts survived reaches.

    *html* is assumed VALIDATED. :func:`validate_page` runs where the author or the
    agent is still present to fix a refusal; re-running it on every read would
    refuse a stored page the operator cannot do anything about.

    A read that will not serialize composes an EMPTY one marked stale, rather than
    failing. The page is the template's work and the data is the host's half of the
    bargain -- a value this module could not encode must not blank somebody's
    dashboard, and a stale band over the template's own empty state is the honest
    rendering of a read that could not be sent.
    """
    try:
        blob = json.dumps(read, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        logger.warning("the dashboard read could not be serialized", exc_info=True)
        blob = json.dumps(read_payload({}, stale=True))
    # Re-checked here rather than trusted from the read: a caller that built its own
    # mapping instead of using read_payload must not put an unchecked tag in markup.
    lang = page_locale(read.get("locale") if isinstance(read, Mapping) else None)
    island = (
        f'<script type="application/json" id="{_DATA_ELEMENT_ID}">'
        f"{escape_json_for_html(blob)}</script>"
    )
    return (
        "<!doctype html>\n"
        f'<html lang="{lang}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{FRAME_CSP}">\n'
        f"<title>{_escape_html_text(title or 'Dashboard')}</title>\n"
        f"<style>{_HOST_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{_STALE_BAND}\n"
        f"{island}\n"
        # BEFORE the page, so an error in the page's first script is counted.
        f"<script>{_INIT_GUARD_JS}</script>\n"
        f"<script>{_BOOTSTRAP_JS}</script>\n"
        f"<script>{_REFILL_PROBE_JS}</script>\n"
        f"{html}\n"
        f"<script>{_BAND_JS}</script>\n"
        f"<script>{_BEACON_JS}</script>\n"
        "</body>\n"
        "</html>\n"
    )


#: A value as this reader ends it, the way an HTML tokenizer does: a quoted value is a
#: unit that may hold anything but ``<`` (so a ``>`` inside the quotes is part of the
#: value, not the end of the tag), and a bare value runs to white space, ``>`` or ``<``
#: and may not start with a quote. Stopping a quoted value at ``<`` as well keeps the
#: run from scanning past the next tag into the page, the same guard the two runs below
#: carry; a start tag never legitimately holds a bare ``<`` inside a value.
_CELL_VALUE = rf"\"[^\"<]*+\"|'[^'<]*+'|(?![\"'])[^{_WS}<>]*+"


def _cell_attributes(value: str, guard: str = "") -> str:
    """Attributes of a start tag as this reader skips them, each value matching *value*.

    The same shape as :func:`_attributes`, possessive throughout so a run that does not
    fit fails in time linear in its length, and reading every value as a whole unit so a
    quoted ``>`` inside one is consumed rather than mistaken for the end of the tag.

    *guard* is an optional negative lookahead placed before each attribute, so the run
    that precedes the binding attribute does not itself consume it: being possessive, a
    run that swallowed ``data-dashboard-field=...`` would never give it back for the
    capture, so the whole cell would fail to match. Guarding only the attribute (not the
    white-space separator) lets another attribute that merely starts with the same letters
    (``data-dashboard-fieldset``) still be read before the binding one.
    """
    gap = rf"[{_WS}]*+"
    separator = rf"[{_WS}/]++"
    attribute = rf"{guard}[^{_WS}/<>=]++(?:{gap}={gap}(?:{value}))?+"
    return rf"(?:{separator}|{attribute})*+"


#: Refuses the binding attribute at the start of an attribute, so the run before it leaves
#: the first one for the capture. ``\s*=`` is required so a boolean ``data-dashboard-field``
#: with no value -- which the capture would not take anyway -- is still skipped as an
#: ordinary attribute rather than ending the run.
_NOT_BINDING = rf"(?!{BINDING_ATTRIBUTE}\s*=)"


#: A text binding on an element with NO nested markup, which is every valid one: the
#: bootstrap fills a binding by setting ``textContent``, so an element holding tags is
#: not a text cell and is left alone rather than rewritten.
#:
#: The opening tag is read the way an HTML tokenizer reads it, not with a ``[^>]`` run:
#: each attribute value is a whole unit (see :data:`_CELL_VALUE`), so a ``>`` inside a
#: quoted value is part of the value and does not end the tag early -- which, before this,
#: corrupted the page when the quoted ``>`` came after the binding attribute and silently
#: left the cell unfilled when it came before. The tag name and both attribute runs are
#: possessive, and the first binding attribute is committed in an atomic group, so a start
#: tag that repeats the attribute or never closes is read once rather than researched; and
#: every run stops at ``<`` as well as ``>``, so the search never scans past the next tag.
_SIMPLE_BINDING_CELL = re.compile(
    r"(<(?P<tag>[a-zA-Z][\w-]*+)(?>"
    + _cell_attributes(_CELL_VALUE, _NOT_BINDING)
    + BINDING_ATTRIBUTE
    + r"\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s\"'>=`]+)))"
    + _cell_attributes(_CELL_VALUE)
    + r">)(?P<body>[^<]*)(?P<close></(?P=tag)\s*>)"
)


def _as_text(value: Any) -> str:
    """A value as the page would show it, matching the bootstrap's own ``text()``.

    Spelled twice because the two run in different languages on the same contract; a
    test pins them to the same three rules so a page filled here and a page filled by
    the script cannot disagree about one value.
    """
    if value is None:
        return "\u2014"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def fill_bindings_as_text(html: str, read: Mapping[str, Any]) -> str:
    """Fill each text binding in *html* with its value, HTML-ESCAPED.

    For the page that is served with no script. The bootstrap is what normally fills a
    binding, so a page mounted without it drew every bound cell EMPTY while its stale
    band stayed hidden -- an empty page that reads as a current one, which is the one
    thing this surface must not do.

    The value is escaped before it is placed, so this path keeps the frame's own
    guarantee that a value can never become markup: it is the same promise the
    ``textContent`` assignment makes, made here instead.

    Only a cell with NO nested markup is filled. A binding is a text cell by
    definition, so an element holding tags was never one, and rewriting it would be
    this function guessing at a structure the contract does not describe.
    """
    fields = read.get("fields") if isinstance(read, Mapping) else None
    values = fields if isinstance(fields, Mapping) else {}

    def _fill(m: "re.Match[str]") -> str:
        name = m.group("dq") or m.group("sq") or m.group("bare") or ""
        if name not in values:
            return m.group(0)
        return m.group(1) + _escape_html_text(_as_text(values[name])) + m.group("close")

    return _SIMPLE_BINDING_CELL.sub(_fill, html)


def compose_body(html: str, read: Mapping[str, Any]) -> str:
    """The body half of :func:`compose`: band, data island, bootstrap, page, band and beacon scripts.

    For a host that wraps the page in its OWN document -- the dashboard's sandbox
    frame builds the head (theme variables, its CSP, the height reporter) itself, so
    what it needs from here is everything :func:`compose` puts inside ``<body>``, in
    the same order and from the same builder, so the two cannot describe one read
    differently.
    """
    document = compose(html, read)
    start = document.index("<body>\n") + len("<body>\n")
    end = document.rindex("</body>")
    # The host builds the HEAD, so everything this module puts there is dropped by the
    # slice -- including its own stylesheet, which is not decoration: it is what dims a
    # missing cell, underlines an agentic one and makes the stale band a band rather
    # than a line of plain text. Prepended rather than handed to the host separately,
    # so the rules travel with the markup that depends on them and a host cannot
    # compose one without the other. A `<style>` in the body is valid and applies to
    # the whole document.
    return f"<style>{_HOST_CSS}</style>\n" + document[start:end]

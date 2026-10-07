"""The dynamic dashboard's frame: the host owns the document, the template owns the page.

A template's ``template.html`` is a body fragment and it may run JS. That is a
decided point of contract v3 and it is what makes a chart possible -- the host
cannot draw one for a template it has never seen, and a page that can only print
text cannot be a dashboard. So the fragment is treated the way dsh-visualize
treats a visualization: the page author writes the body, and the host writes
everything around it.

Two ways a value reaches the page, and the page chooses per element
------------------------------------------------------------------
1. ``data-dashboard-field="<name>"`` on an element: the host sets that element's
   TEXT to the field's value. No markup, no attributes, no HTML -- a text node, so
   a value can never become an element. This is the path for every cell a reader
   reads.
2. ``window.kirocrew.fields`` in a script: the same values as data, for a chart to
   draw from. :data:`WINDOW_GLOBAL` also carries ``agentic`` (the names the
   crewmate wrote rather than the gateway folded), ``seq`` (the fold watermark) and
   ``stale``.

The host fills (1) itself, from the same object it hands to (2), so a page cannot
show one number in a cell and a different one in its chart.

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
from typing import Any, Final, Mapping

logger = logging.getLogger(__name__)

#: The size ceiling for one template page, in bytes of UTF-8.
#:
#: dsh-visualize's own ``maxFragmentBytes`` default, taken rather than invented:
#: the brief asks for its default and the number is a considered one -- a page with
#: an inlined chart library's worth of markup fits, while a runaway generation does
#: not. Measured in BYTES, not characters, so a multi-byte page cannot pass at
#: several times the ceiling the document is assembled under.
MAX_PAGE_BYTES: Final[int] = 1_000_000

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
    skeleton = _SKELETON_TAG_RE.search(html)
    if skeleton is not None:
        raise PageError(
            "page_has_skeleton",
            f"the page contains the document tag {skeleton.group(0)!r} -- write only "
            "the inline body; the host supplies <!doctype>, <html>, <head> and "
            "<body>, and owns the page's security policy",
        )
    return size


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
  // CONFIGURABLE, which looks like a weakening and is not. The REFILL below has
  // to redefine this property, so a non-configurable definition here makes the
  // first refill throw -- and the same throw hits any host that puts a second
  // document in one window, where the first document's definition is still
  // standing. What protects the values is the deep freeze, not the descriptor: a
  // page that wanted to lie about its numbers could draw anything it liked
  // regardless, so an attribute that only breaks the refill buys nothing.
  Object.defineProperty(window, {WINDOW_GLOBAL!r}, {{
    value: freeze(read),
    writable: false,
    configurable: true,
    enumerable: true,
  }});
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
    // The FROZEN global is replaced wholesale rather than mutated, for the reason
    // it is frozen: a page holding a reference to the old object keeps reading
    // consistent numbers from it, and nothing half-updates.
    read = freeze(data.read);
    Object.defineProperty(window, {WINDOW_GLOBAL!r}, {{
      value: read, writable: false, configurable: true, enumerable: true,
    }});
    fill();
    window.dispatchEvent(new Event({'"kirocrew-dashboard"'}));
  }});
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', fill);
  else fill();
}})();
"""

#: The wire type of the host's refill message.
DATA_MESSAGE_TYPE: Final[str] = "kirocrew-dashboard:data"

#: The event the page may listen for to know a refill landed. A page drawing a chart
#: redraws on this rather than polling.
PAGE_EVENT: Final[str] = "kirocrew-dashboard"

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
    parent.postMessage({{ type: {READY_MESSAGE_TYPE!r} }}, '*');
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
  addEventListener({PAGE_EVENT!r}, paint);
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', paint);
  else paint();
}})();
"""


def read_payload(
    fields: Mapping[str, Any],
    agentic: list[str] | tuple[str, ...] = (),
    seq: int = 0,
    stale: bool = False,
    missing: list[str] | tuple[str, ...] = (),
    written_at: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The object the page sees as ``window.kirocrew``.

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
    """
    return {
        "fields": dict(fields),
        "agentic": sorted(agentic),
        "seq": int(seq),
        "stale": bool(stale),
        "missing": sorted(missing),
        "written_at": dict(written_at or {}),
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
    island = (
        f'<script type="application/json" id="{_DATA_ELEMENT_ID}">'
        f"{escape_json_for_html(blob)}</script>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
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
        f"{html}\n"
        f"<script>{_BAND_JS}</script>\n"
        f"<script>{_BEACON_JS}</script>\n"
        "</body>\n"
        "</html>\n"
    )


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

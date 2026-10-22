#!/usr/bin/env python3
"""harness.py -- write a preview page for a Living UI widget or Dashboard page.

    python3 harness.py WIDGET.html [-o PREVIEW.html]
    python3 harness.py VIEW.html --fields FIELDS.json [--push NEXT.json ...] [-o PREVIEW.html]

WIDGET.html holds a widget body: the HTML that goes inside an ``<mcwidget>``
tag, or the whole tag. The page this writes frames that body the way the chat
frames a widget:

* the same Content-Security-Policy, so a network call, a remote image or a
  script from an origin the frame does not allow fails here as it would there;
* the same sandbox: scripts run, but without ``allow-same-origin`` the frame
  has an opaque origin, with no storage, no cookies and no reach into the page;
* the same link handling: the frame opens no window, so a web link opens in a
  new tab through the page, at once after a click tied to the frame, or from an
  "Open <host>" button in the bar when the click may have been the page's own;
* the theme variables of the default palette, in its dark set and its light set.

With ``--fields`` the file is a Dashboard page's ``view.html`` instead, and
FIELDS.json holds one JSON object of sample field values, by field name. The
page then frames the view the way the Dashboard tab does:

* that frame's CSP, which allows inline script only: a widget may load a
  script from two CDNs, a Dashboard page from none;
* its sandbox, ``allow-scripts`` and nothing else;
* the same theme variables, and around the view what the host's own
  ``compose_body`` writes there, over a read from its own ``read_payload``,
  so the bound cells are filled and ``window.kirocrew.fields`` and
  ``window.kirocrew.subscribe`` behave as they do in the tab.

Each ``--push`` file holds more field values. A button above the frame sends
them into it as the next refill, the message the tab posts when a fold moves,
so a page's motion on a change can be seen, and slowed down.

Open the written file in a browser and pick a variant with query flags:

* ``?theme=dark`` or ``?theme=light``; dark when absent.
* ``?reduced=1`` answers every ``prefers-reduced-motion`` query as ``reduce``,
  in script (``matchMedia``, so a Living UI ``loop`` runs with ``calm`` true)
  and in the CSS rules the widget has when the document finishes parsing.
* ``?slow=0.05`` runs ``requestAnimationFrame`` timestamps and
  ``performance.now`` at 0.05 of real time, so a 100 ms envelope lands on
  several frames. Any speed above 0 and up to 1 works; ``?slow`` alone is 0.05.
  Timers and CSS animations keep real time.

A script error is listed above the frame with its line in WIDGET.html or
VIEW.html, a load the CSP blocks with its address, and both are logged to the
console.

The page is one self-contained file: it needs no server, and this script opens
no socket. The frame cannot load the Tailwind runtime the gateway serves to the
real frame, so Tailwind classes stay unstyled here. Theme variables, inline
``style`` and ``<style>`` work as they do in the chat.

The CSP, the sandbox, the base body style and the theme values below mirror
the web app's widget frame (``cspFor``, ``BASE_BODY_CSS`` and
``THEME_VAR_NAMES`` in ``widgetSrcdoc.ts``, the sandbox in ``WidgetFrame.tsx``,
and the default palette in ``theme-palettes.css``). A test in the Kiro Crew
repository reads those files and fails when this copy drifts from them.

The Dashboard mode's CSP mirrors ``cspFor`` with ``offlineScripts`` and its
sandbox ``CREW_DASHBOARD_SANDBOX`` in ``CrewDynamicDashboard.tsx``, pinned by
the same test. Its markup and scripts are not copied: the mode imports
``kiro_crew.dashboard_frame``. A plain ``python3`` cannot, so there the script
runs itself again under the interpreter a crewmate's shell names in
``KIROCREW_RUNTIME_PYTHON``, and refuses when there is none. The widget mode
needs neither.

Exit codes: 0 written, 2 an input could not be read, the page not written, or
the Dashboard mode has no Python that can import Kiro Crew.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

#: The chat widget frame's policy, as ``cspFor`` writes it for a widget, less
#: the one source a file cannot use: the gateway's copy of the Tailwind runtime.
FRAME_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none';"
)

#: The chat widget frame's sandbox grants (``AGENT_PAGE_SANDBOX`` in
#: ``website/src/lib/widgetSrcdoc.ts``). No ``allow-same-origin``, and no popup
#: grant: in the dashboard a link opens through the host's link bridge.
FRAME_SANDBOX = "allow-scripts"

#: The frame's own body style, put ahead of the theme rules.
BASE_BODY_CSS = (
    "body { margin: 0; padding: 16px; font-family: -apple-system, "
    "BlinkMacSystemFont, 'Segoe UI', sans-serif; }"
)

#: The theme variables the frame receives, with the default palette's values.
THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "--bg": "#19161d",
        "--bg-elevated": "#211d25",
        "--bg-hover": "#28242e",
        "--card": "#211d25",
        "--card-fg": "#f2f1f4",
        "--text": "#dcdadf",
        "--text-strong": "#f2f1f4",
        "--muted": "#938f9b",
        "--muted-strong": "#5e5966",
        "--border": "#352f3d",
        "--border-strong": "#4a464f",
        "--accent": "#8e48ff",
        "--accent-hover": "#9f63ff",
        "--accent-subtle": "rgba(178,127,255,.16)",
        "--ok": "#008543",
        "--ok-subtle": "rgba(0,133,67,.15)",
        "--warn": "#e0b94d",
        "--warn-subtle": "rgba(224,185,77,.12)",
        "--danger": "#f94359",
        "--danger-subtle": "rgba(249,67,89,.12)",
        "--info": "#6f9ae0",
    },
    "light": {
        "--bg": "#ffffff",
        "--bg-elevated": "#ffffff",
        "--bg-hover": "#f0f0f0",
        "--card": "#ffffff",
        "--card-fg": "#19161d",
        "--text": "#4a464f",
        "--text-strong": "#19161d",
        "--muted": "#5e5966",
        "--muted-strong": "#4a464f",
        "--border": "#e4e4e7",
        "--border-strong": "#d4d4d8",
        "--accent": "#8e48ff",
        "--accent-hover": "#7a35e0",
        "--accent-subtle": "rgba(142,72,255,.12)",
        "--ok": "#008543",
        "--ok-subtle": "rgba(0,133,67,.12)",
        "--warn": "#a38a00",
        "--warn-subtle": "rgba(163,138,0,.1)",
        "--danger": "#d92544",
        "--danger-subtle": "rgba(217,37,68,.1)",
        "--info": "#285092",
    },
}

#: Runs first in the frame under ``?reduced``. A ``prefers-reduced-motion`` test
#: becomes a width test that always holds (``reduce``) or never holds
#: (``no-preference``), so the rest of a compound query still counts.
REDUCED_SHIM = r"""(function () {
  var feature = /\(\s*prefers-reduced-motion\s*(?::\s*([a-z-]+)\s*)?\)/gi;
  function force(text) {
    return String(text).replace(feature, function (_, value) {
      return value && value.toLowerCase() === 'no-preference' ? '(max-width: 0px)' : '(min-width: 0px)';
    });
  }
  var match = window.matchMedia.bind(window);
  window.matchMedia = function (query) { return match(force(query)); };
  function walk(rules) {
    for (var i = 0; i < rules.length; i++) {
      if (rules[i].media) rules[i].media.mediaText = force(rules[i].media.mediaText);
      if (rules[i].cssRules) walk(rules[i].cssRules);
    }
  }
  document.addEventListener('DOMContentLoaded', function () {
    for (var i = 0; i < document.styleSheets.length; i++) {
      var sheet = document.styleSheets[i];
      sheet.media.mediaText = force(sheet.media.mediaText);
      walk(sheet.cssRules);
    }
  });
})();"""

#: Runs first in the frame under ``?slow``; the page puts the speed in place
#: of ``__SPEED__``. Both clocks start from the same instant.
SLOW_SHIM = r"""(function () {
  var speed = __SPEED__;
  var now = performance.now.bind(performance);
  var start = now();
  function slowed(t) { return start + (t - start) * speed; }
  performance.now = function () { return slowed(now()); };
  var frame = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = function (callback) {
    return frame(function (t) { callback(slowed(t)); });
  };
})();"""

#: Always runs in the frame: it sizes the frame to its content, as the chat
#: frame does, and sends script errors and CSP refusals to the page.
REPORT_SHIM = r"""(function () {
  function post(message) { parent.postMessage(message, '*'); }
  function size() {
    post({ type: 'lui-preview-height', height: Math.ceil(document.body.getBoundingClientRect().bottom + window.scrollY) });
  }
  document.addEventListener('DOMContentLoaded', function () {
    new ResizeObserver(size).observe(document.body);
  });
  window.addEventListener('error', function (e) {
    post({ type: 'lui-preview-error', text: String(e.message), file: e.filename, line: e.lineno });
  });
  // No line for a refusal: for a load from markup the browser names wherever
  // the parser was, not the element. The blocked address finds it instead.
  document.addEventListener('securitypolicyviolation', function (e) {
    post({ type: 'lui-preview-error', file: e.sourceFile,
      text: 'the frame CSP blocked ' + (e.blockedURI || 'inline code') + ' (' + e.effectiveDirective + ')' });
  });
})();"""

#: Runs first in a widget preview's frame, as the chat frame's link bridge
#: (``LINK_BRIDGE_SHIM_BODY`` in ``website/src/lib/widgetSrcdoc.ts``) does there:
#: the sandbox opens no window, so a web link's click is handed to the page,
#: which opens it right after a click in the frame. Decided a task after the
#: click, so a page that cancels it keeps that. A plain click is left to the
#: sandbox, which refuses its window (a bare link is made window-opening, so the
#: frame does not navigate away); a middle or modified click, which opens a tab
#: even in the sandbox, is cancelled by a window listener added during its
#: capture, so after the page's own window listeners, and only if the page has not
#: cancelled it in any form (preventDefault, return false, returnValue).
LINK_SHIM = r"""(function () {
  // A protocol-relative link takes the page's scheme (a local preview file has none
  // of its own, so https); an SVG link may carry xlink:href instead of href.
  var XLINK = 'http://www.w3.org/1999/xlink';
  function web(raw) {
    var s = String(raw).trim();
    if (s.slice(0, 2) === '//') s = (/^http:/.test(document.baseURI) ? 'http:' : 'https:') + s;
    try { var u = new URL(s); } catch (_) { return null; }
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  }
  function hrefOf(n) {
    return n.getAttribute('href') || (n.getAttributeNS ? n.getAttributeNS(XLINK, 'href') : '') || '';
  }
  function decide(e, middle) {
    var path = typeof e.composedPath === 'function' ? e.composedPath() : [e.target], a = null;
    for (var i = 0; i < path.length && !a; i++) {
      var n = path[i];
      if (n && n.nodeType === 1 && (n.localName === 'a' || n.localName === 'area')
        && (n.hasAttribute('href') || (n.hasAttributeNS && n.hasAttributeNS(XLINK, 'href')))) a = n;
    }
    if (!a || a.closest('[data-action]')) return;
    var target = (a.getAttribute('target') || '').trim().toLowerCase();
    var url = web(hrefOf(a));
    if (!url || (!middle && target === '_self')) return;
    if (!middle && !target) a.setAttribute('target', '_blank');
    var post = function () { parent.postMessage({ type: 'lui-preview-open', url: url }, '*'); };
    if (!(middle || e.ctrlKey || e.metaKey || e.shiftKey)) {
      setTimeout(function () { if (!e.defaultPrevented) post(); }, 0);
      return;
    }
    // Added now, during capture, so it runs after every window listener the page
    // registered, and reads a cancel in any form before the tab would open.
    window.addEventListener(e.type, function (ev) {
      if (ev !== e || e.defaultPrevented) return;
      e.preventDefault();
      post();
    }, { once: true });
  }
  window.addEventListener('click', function (e) { decide(e, false); }, true);
  window.addEventListener('auxclick', function (e) { if (e.button === 1) decide(e, true); }, true);
  // window.open goes the same way and returns null, as the chat frame's bridge does.
  // A plain assignment, so a page that declares its own open still runs; the page
  // opens only a web URL, right after a click in the frame.
  try {
    window.open = function (raw) {
      var url = web(raw);
      if (url) parent.postMessage({ type: 'lui-preview-open', url: url }, '*');
      return null;
    };
  } catch (_) {}
})();"""

#: The page script. It reads the flags, builds the frame's document the way the
#: chat frame's builder does, and shows what the frame reports.
_PAGE_SCRIPT = r"""(() => {
  const query = new URLSearchParams(location.search);
  const mode = query.get('theme') === 'light' ? 'light' : 'dark';
  const reduced = query.has('reduced') && query.get('reduced') !== '0';
  const asked = Number(query.get('slow'));
  const slow = !query.has('slow') ? 1 : asked > 0 && asked <= 1 ? asked : 0.05;
  const vars = CONFIG.themes[mode];
  document.documentElement.style.colorScheme = mode;
  document.body.style.background = vars['--bg'];
  document.body.style.color = vars['--muted'];
  document.getElementById('lui-bar').textContent = ['Living UI preview', mode,
    reduced && 'reduced motion', slow < 1 && `slow x${slow}`].filter(Boolean).join(' · ');

  const shims = [];
  if (reduced) shims.push(CONFIG.shims.reduced);
  if (slow < 1) shims.push(CONFIG.shims.slow.replace('__SPEED__', String(slow)));
  if (CONFIG.shims.link) shims.push(CONFIG.shims.link);
  shims.push(CONFIG.shims.report);
  const rootBody = Object.entries(vars).map(([name, value]) => `${name}:${value}`).join(';');
  const theme = `:root{${rootBody};color-scheme:${mode}}` +
    `html{background:var(--bg)}` +
    `body{background:var(--bg);color:var(--text)}`;
  const head = '<!DOCTYPE html>\n<html><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    `<meta http-equiv="Content-Security-Policy" content="${CONFIG.csp}">` +
    `<style>${CONFIG.baseCss} ${theme}</style>` +
    shims.map((code) => `<script>${code}</` + 'script>').join('') +
    `</head><body class="${mode}">\n`;
  // Lines before the widget's first line, to turn a frame line into a file line.
  const offset = head.split('\n').length - 1;

  const frame = document.getElementById('lui-frame');
  const errors = document.getElementById('lui-errors');
  // One line per distinct error with its count, so an error thrown on every
  // frame stays one line instead of growing the page without end; reports past the cap are counted.
  const seen = new Map();
  let dropped = 0;
  // Whose click it is, as the dashboard's bridge decides: the page's own clicks and
  // keys (the bar, a push button) give it a live activation too, and the frame's
  // script can focus the frame. So a link opens at once only when focus came into
  // the frame from this page and no click or key on the page is still live (5 s);
  // otherwise an "Open <host>" button in the bar opens it on the person's own click.
  let lastInput = -Infinity;
  let entered = false;
  let offer = null;
  ['keydown', 'mousedown', 'pointerdown', 'pointerup', 'touchend'].forEach((type) => {
    addEventListener(type, (e) => {
      lastInput = performance.now();
      if (offer && (type === 'keydown' || type === 'pointerdown') && !offer.contains(e.target)) dropOffer();
    }, true);
  });
  addEventListener('blur', () => { entered = document.activeElement === frame; });
  addEventListener('focus', () => { entered = false; });
  function dropOffer() { if (offer) { offer.remove(); offer = null; } }
  function offerOpen(url) {
    dropOffer();
    const button = document.createElement('button');
    button.type = 'button';
    button.style.marginLeft = '12px';
    button.textContent = `Open ${new URL(url).host}`;
    button.title = url;
    button.dataset.luiOpen = '';
    button.addEventListener('click', () => { dropOffer(); window.open(url, '_blank', 'noopener,noreferrer'); });
    document.getElementById('lui-bar').append(button);
    offer = button;
    button.focus();
  }
  // The dashboard bridge's URL rule (safeHttpUrl): an absolute http(s) URL with no
  // user name or password.
  function webUrl(raw) {
    let u;
    try { u = new URL(raw); } catch (_) { return null; }
    if ((u.protocol !== 'http:' && u.protocol !== 'https:') || u.username || u.password) return null;
    return u.href;
  }
  addEventListener('message', (event) => {
    const data = event.data;
    if (event.source !== frame.contentWindow || !data) return;
    const url = data.type === 'lui-preview-open' && typeof data.url === 'string' ? webUrl(data.url) : null;
    if (url && CONFIG.shims.link && navigator.userActivation && navigator.userActivation.isActive
      && document.activeElement === frame) {
      if (entered && performance.now() - lastInput >= 5000) {
        frame.blur();
        window.open(url, '_blank', 'noopener,noreferrer');
      } else {
        offerOpen(url);
      }
    }
    if (data.type === 'lui-preview-height' && Number.isFinite(data.height)) {
      frame.style.height = `${Math.min(Math.max(data.height, 0), 20000)}px`;
    }
    if (data.type === 'lui-preview-error') {
      // The frame's own document reports itself as about:srcdoc, or about.
      const own = !data.file || /^about(:srcdoc)?$/.test(data.file);
      const line = own && data.line > offset ? ` line ${data.line - offset + CONFIG.above}` : '';
      const where = own ? `${CONFIG.name}${line}` : String(data.file);
      const text = `${where}: ${String(data.text)}`.slice(0, 500);
      if (!seen.has(text) && seen.size >= 50) {
        dropped += 1;
      } else {
        seen.set(text, (seen.get(text) || 0) + 1);
        if (seen.get(text) === 1) console.error(`[living-ui preview] ${text}`);
      }
      errors.style.color = vars['--danger'];
      errors.hidden = false;
      const lines = [...seen].map(([t, n]) => (n > 1 ? `${t} (x${n})` : t));
      if (dropped) lines.push(`${dropped} more error report(s) past the first 50, not listed`);
      errors.textContent = lines.join('\n');
    }
  });
  frame.setAttribute('sandbox', CONFIG.sandbox);
  frame.srcdoc = head + CONFIG.body + '\n</body></html>';
})();"""

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Living UI preview: {title}</title>
<style>
html, body {{ margin: 0; }}
#lui-bar, #lui-errors {{ margin: 0; padding: 6px 16px; font: 12px/1.5 ui-monospace, monospace; }}
#lui-errors {{ white-space: pre-wrap; }}
#lui-frame {{ display: block; width: 100%; height: 0; border: 0; }}
</style>
</head>
<body>
<div id="lui-preview">
<p id="lui-bar"></p>
<pre id="lui-errors" hidden></pre>
<iframe id="lui-frame" title="{title}"></iframe>
</div>
<script>
const CONFIG = {config};
{script}
</script>
</body>
</html>
"""

_MCWIDGET = re.compile(r"\A\s*<mcwidget\b[^>]*>(.*)</mcwidget>\s*\Z", re.I | re.S)

# --------------------------------------------------------------------------- #
# The Dashboard tab's frame
# --------------------------------------------------------------------------- #

#: The Dashboard tab's policy: ``cspFor`` with ``offlineScripts``, which drops
#: both CDN origins, less the gateway's copy of the Tailwind runtime. Inline
#: script only, which is also what ``FRAME_CSP`` in ``dashboard_frame.py`` leaves
#: when the two policies meet.
DASHBOARD_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none';"
)

#: The Dashboard tab's sandbox grants: scripts, and nothing else.
DASHBOARD_SANDBOX = "allow-scripts"

#: Whose counter a read's version belongs to, and the version the page loads at.
#: Every read the preview sends carries this generation, so each push is the next
#: version of the same armed page.
GENERATION = "preview"
FIRST_VERSION = 1

#: Set on the run this script starts under ``KIROCREW_RUNTIME_PYTHON``, so a
#: second Python that cannot import Kiro Crew either refuses instead of re-running.
REEXEC_ENV = "LIVING_UI_HARNESS_REEXEC"

#: The page script's addition in the Dashboard mode, run after the shared script
#: has built the frame. Each push file gets a button in the bar above the frame,
#: enabled once the frame has loaded and its bootstrap listens. A press posts the
#: file's fields into the frame as the next patch, one version past the last one
#: this document took, the message the Dashboard tab posts when a fold moves. The
#: target origin is ``*``
#: because a frame sandboxed without ``allow-same-origin`` has an opaque origin,
#: which no other value names; the message goes to that one frame.
_DASHBOARD_SCRIPT = r"""(() => {
  const frame = document.getElementById('lui-frame');
  const bar = document.getElementById('lui-bar');
  let version = CONFIG.version;
  const buttons = CONFIG.pushes.map((push) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.disabled = true;
    button.style.marginLeft = '12px';
    button.textContent = `Push ${push.name}`;
    button.addEventListener('click', () => {
      version += 1;
      frame.contentWindow.postMessage({ type: CONFIG.dataType, read: { ...push.read, version } }, '*');
    });
    bar.append(button);
    return button;
  });
  // Each load is a new document, whose bootstrap holds the island's version again.
  frame.addEventListener('load', () => {
    version = CONFIG.version;
    buttons.forEach((button) => { button.disabled = false; });
  });
})();"""


def widget_body(text: str) -> tuple[str, int]:
    """The widget body in *text*, and how many lines of *text* sit above its first line.

    The body is what one ``<mcwidget>`` tag wraps, else all of *text*. The line
    count turns a line in the frame back into a line in the file.
    """
    wrapped = _MCWIDGET.match(text)
    if not wrapped:
        return text, 0
    return wrapped.group(1), text[: wrapped.start(1)].count("\n")


def _script_literal(value: object) -> str:
    """*value* as a JavaScript literal that cannot end the ``<script>`` it sits in.

    ``ensure_ascii`` escapes every non-ASCII character, U+2028 included, and the
    ``<`` escape keeps ``</script>`` and ``<!--`` in a widget body from being
    read as markup by the page's HTML parser.
    """
    return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c")


def page(text: str, name: str = "widget") -> str:
    """The preview page for the widget in *text*; *name* labels its errors."""
    body, above = widget_body(text)
    config = {
        "name": name,
        "body": body,
        "above": above,
        "csp": FRAME_CSP,
        "sandbox": FRAME_SANDBOX,
        "baseCss": BASE_BODY_CSS,
        "themes": THEMES,
        "shims": {
            "reduced": REDUCED_SHIM,
            "slow": SLOW_SHIM,
            "link": LINK_SHIM,
            "report": REPORT_SHIM,
        },
    }
    return _PAGE.format(
        title=html.escape(name), config=_script_literal(config), script=_PAGE_SCRIPT
    )


def _host_frame() -> Any:
    """Kiro Crew's own Dashboard frame module, or ``None`` where this Python cannot import it."""
    try:
        from kiro_crew import dashboard_frame
    except ImportError:
        return None
    return dashboard_frame


def dashboard_page(
    view: str,
    fields: dict[str, object],
    pushes: tuple[tuple[str, dict[str, object]], ...] = (),
    name: str = "view.html",
    frame: Any = None,
) -> str:
    """The preview page for the Dashboard *view* over *fields*; *name* labels its errors.

    Each of *pushes*, a label and the fields to send, gets a button above the frame.
    *frame* is the host's ``dashboard_frame`` module, imported here when not given.
    """
    frame = frame or _host_frame()
    if frame is None:
        raise RuntimeError("the Dashboard mode needs Kiro Crew's dashboard_frame module")
    read = {**frame.read_payload(fields), "version": FIRST_VERSION, "generation": GENERATION}
    body: str = frame.compose_body(view, read)
    # The host's own lines open the body; a unique view marks where the view starts.
    marker = "living-ui-preview-view-starts-here-0b1c9e"
    probe: str = frame.compose_body(marker, read)
    above = probe[: probe.index(marker)].count("\n")
    config = {
        "name": name,
        "body": body,
        # Below zero: the host's own lines open the body, above the view's first line.
        "above": -above,
        "csp": DASHBOARD_CSP,
        "sandbox": DASHBOARD_SANDBOX,
        "baseCss": BASE_BODY_CSS,
        "themes": THEMES,
        "shims": {"reduced": REDUCED_SHIM, "slow": SLOW_SHIM, "report": REPORT_SHIM},
        "dataType": frame.DATA_MESSAGE_TYPE,
        "version": FIRST_VERSION,
        "pushes": [
            {
                "name": label,
                "read": {**frame.read_payload(more), "generation": GENERATION, "patch": True},
            }
            for label, more in pushes
        ],
    }
    return _PAGE.format(
        title=html.escape(name),
        config=_script_literal(config),
        script=f"{_PAGE_SCRIPT}\n{_DASHBOARD_SCRIPT}",
    )


def read_fields(path: Path) -> dict[str, object]:
    """The JSON object of field values in *path*; a ``ValueError`` says why it is not one."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        # Python reads NaN, Infinity and a number past a float's range, such as 1e400;
        # JSON holds none of them, and the island could not carry one.
        json.dumps(value, allow_nan=False)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{path} must hold one JSON object of field values, by field name")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a preview page that frames a widget body like the chat does, "
        "or a Dashboard view.html like the Dashboard tab does."
    )
    parser.add_argument(
        "widget",
        type=Path,
        help="the widget body, or one <mcwidget> tag; with --fields, a Dashboard view.html",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        help="the page to write (default: <widget name>.preview.html here)",
    )
    parser.add_argument(
        "--fields",
        type=Path,
        help="sample field values as one JSON object: preview a Dashboard page",
    )
    parser.add_argument(
        "--push",
        type=Path,
        action="append",
        default=[],
        help="more field values, sent by a button above the frame as the next refill; "
        "repeat for more",
    )
    args = parser.parse_args(argv)
    if args.push and args.fields is None:
        parser.error("--push needs --fields")
    frame = None
    if args.fields is not None:
        frame = _host_frame()
        if frame is None:
            runtime = os.environ.get("KIROCREW_RUNTIME_PYTHON", "").strip()
            if os.environ.get(REEXEC_ENV) or not runtime or not os.path.isfile(runtime):
                print(
                    "harness: the Dashboard mode frames the page with Kiro Crew's own code, "
                    "which this Python cannot import; a crewmate's shell names that "
                    "interpreter in KIROCREW_RUNTIME_PYTHON",
                    file=sys.stderr,
                )
                return 2
            again = sys.argv[1:] if argv is None else argv
            return subprocess.call(
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- no shell; the interpreter is the one the gateway names in the agent's environment after its scrub, and the arguments are this script's own
                [runtime, "-I", "-B", os.path.abspath(__file__), *again],
                env={**os.environ, REEXEC_ENV: "1"},
            )
    widget: Path = args.widget
    out: Path = args.out or Path.cwd() / f"{widget.stem}.preview.html"
    try:
        text = widget.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"harness: cannot read {widget}: {exc}", file=sys.stderr)
        return 2
    inputs: list[Path] = [widget]
    fields: dict[str, object] = {}
    pushes: tuple[tuple[str, dict[str, object]], ...] = ()
    if args.fields is not None:
        inputs += [args.fields, *args.push]
        try:
            fields = read_fields(args.fields)
            pushes = tuple((push.name, read_fields(push)) for push in args.push)
        except ValueError as exc:
            print(f"harness: {exc}", file=sys.stderr)
            return 2
    # samefile, not a path compare: a hard link or a case-folded name is the same file.
    for source in inputs:
        if out.exists() and out.samefile(source):
            print(f"harness: {out} is {source} itself; pick another --out", file=sys.stderr)
            return 2
    if args.fields is None:
        document = page(text, widget.name)
    else:
        document = dashboard_page(text, fields, pushes, widget.name, frame)
    try:
        out.write_text(document, encoding="utf-8")
    except OSError as exc:
        print(f"harness: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    url = out.resolve().as_uri()
    print(f"wrote {out}")
    for theme in ("dark", "light"):
        for flags in ("", "&reduced=1", "&slow=0.05"):
            print(f"  {url}?theme={theme}{flags}")
    for push in args.push:
        print(f"  a button above the frame pushes {push.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

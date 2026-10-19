#!/usr/bin/env python3
"""harness.py -- write a preview page for a Living UI widget.

    python3 harness.py WIDGET.html [-o PREVIEW.html]

WIDGET.html holds a widget body: the HTML that goes inside an ``<mcwidget>``
tag, or the whole tag. The page this writes frames that body the way the chat
frames a widget:

* the same Content-Security-Policy, so a network call, a remote image or a
  script from an origin the frame does not allow fails here as it would there;
* the same sandbox: scripts run, but without ``allow-same-origin`` the frame
  has an opaque origin, with no storage, no cookies and no reach into the page;
* the theme variables of the default palette, in its dark set and its light set.

Open the written file in a browser and pick a variant with query flags:

* ``?theme=dark`` or ``?theme=light``; dark when absent.
* ``?reduced=1`` answers every ``prefers-reduced-motion`` query as ``reduce``,
  in script (``matchMedia``, so a Living UI ``loop`` runs with ``calm`` true)
  and in the CSS rules the widget has when the document finishes parsing.
* ``?slow=0.05`` runs ``requestAnimationFrame`` timestamps and
  ``performance.now`` at 0.05 of real time, so a 100 ms envelope lands on
  several frames. Any speed above 0 and up to 1 works; ``?slow`` alone is 0.05.
  Timers and CSS animations keep real time.

A script error is listed above the frame with its line in WIDGET.html, a load
the CSP blocks with its address, and both are logged to the console.

The page is one self-contained file: it needs no server, and this script opens
no socket. The frame cannot load the Tailwind runtime the gateway serves to the
real frame, so Tailwind classes stay unstyled here. Theme variables, inline
``style`` and ``<style>`` work as they do in the chat.

The CSP, the sandbox, the base body style and the theme values below mirror
the web app's widget frame (``cspFor``, ``BASE_BODY_CSS`` and
``THEME_VAR_NAMES`` in ``widgetSrcdoc.ts``, the sandbox in ``WidgetFrame.tsx``,
and the default palette in ``theme-palettes.css``). A test in the Kiro Crew
repository reads those files and fails when this copy drifts from them.

Exit codes: 0 written, 2 the widget could not be read or the page not written.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

#: The chat widget frame's policy, as ``cspFor`` writes it for a widget, less
#: the one source a file cannot use: the gateway's copy of the Tailwind runtime.
FRAME_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none';"
)

#: The chat widget frame's sandbox grants. No ``allow-same-origin``.
FRAME_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox"

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
  addEventListener('message', (event) => {
    const data = event.data;
    if (event.source !== frame.contentWindow || !data) return;
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
        "shims": {"reduced": REDUCED_SHIM, "slow": SLOW_SHIM, "report": REPORT_SHIM},
    }
    return _PAGE.format(
        title=html.escape(name), config=_script_literal(config), script=_PAGE_SCRIPT
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a preview page that frames a widget body like the chat does."
    )
    parser.add_argument("widget", type=Path, help="the widget body, or one <mcwidget> tag")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        help="the page to write (default: <widget name>.preview.html here)",
    )
    args = parser.parse_args(argv)
    widget: Path = args.widget
    out: Path = args.out or Path.cwd() / f"{widget.stem}.preview.html"
    try:
        text = widget.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"harness: cannot read {widget}: {exc}", file=sys.stderr)
        return 2
    # samefile, not a path compare: a hard link or a case-folded name is the same file.
    if out.exists() and out.samefile(widget):
        print(f"harness: {out} is the widget itself; pick another --out", file=sys.stderr)
        return 2
    try:
        out.write_text(page(text, widget.name), encoding="utf-8")
    except OSError as exc:
        print(f"harness: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    url = out.resolve().as_uri()
    print(f"wrote {out}")
    for theme in ("dark", "light"):
        for flags in ("", "&reduced=1", "&slow=0.05"):
            print(f"  {url}?theme={theme}{flags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

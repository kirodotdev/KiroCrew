#!/usr/bin/env python3
"""Render one built-in template in a plain HTML page and screenshot it.

The harness is the FRAME's two jobs and nothing else: it sets
``window.kirocrew = {fields, agentic, seq, stale}`` from the captured fold values, and
it fills every ``data-dashboard-field`` element by ``textContent`` -- the same binding
``website/src/pages/chat/command-center/dashboardDocument.ts`` performs. So what the
screenshot shows is what the page draws from the record, with no SPA, no gateway and no
React in the picture.

Fields are resolved exactly as the host must: a dotted path walked against the fold's
rendered value, with "absent" distinguished from "null". An absent path is left out of
the bag entirely rather than set to null, because those are different facts and the
page's own helpers answer for them differently.

Usage:
  python render_builtin.py <template-dir> <fixture.json> <out.png>
                           [--width 760] [--theme light|dark]

``--width`` and ``--theme`` default to the side panel's 430 px and the dark theme, so
a command line written without them renders what it always did. A template has to work
at the panel's real width AND wide, in both themes, which is four renders of one page.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

MISSING = object()

#: The frame injects every name in ``website/src/lib/widgetSrcdoc.ts`` THEME_VAR_NAMES,
#: so the harness does too: a page that reads one this dict omits would fall back to
#: its own hard-coded hex and screenshot a colour the product never serves. Values are
#: the product's own -- the dark set from ``website/src/index.css`` ``:root``, the light
#: set from ``LIGHT_CANVAS_FALLBACK_VARS``.
THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "--bg": "#12141a",
        "--bg-accent": "#14161d",
        "--bg-elevated": "#1a1d25",
        "--bg-hover": "#262a35",
        "--card": "#181b22",
        "--card-fg": "#f4f4f5",
        "--text": "#e4e4e7",
        "--text-strong": "#fafafa",
        "--muted": "#7f7f88",
        "--muted-strong": "#52525b",
        "--border": "#27272a",
        "--border-strong": "#3f3f46",
        "--accent": "#00d492",
        "--accent-hover": "#34d399",
        "--accent-subtle": "rgba(4,117,88,.2)",
        "--ok": "#22c55e",
        "--ok-subtle": "rgba(34,197,94,.12)",
        "--warn": "#eab308",
        "--warn-subtle": "rgba(234,179,8,.12)",
        "--danger": "#ef4444",
        "--danger-subtle": "rgba(239,68,68,.12)",
        "--info": "#0891b2",
    },
    "light": {
        "--bg": "#f3f4f6",
        "--bg-accent": "#ffffff",
        "--bg-elevated": "#ffffff",
        "--bg-hover": "#e5e7eb",
        "--card": "#ffffff",
        "--card-fg": "#111827",
        "--text": "#111827",
        "--text-strong": "#030712",
        "--muted": "#6b7280",
        "--muted-strong": "#4b5563",
        "--border": "#e5e7eb",
        "--border-strong": "#d1d5db",
        "--accent": "#4f46e5",
        "--accent-hover": "#4338ca",
        "--accent-subtle": "#eef2ff",
        "--ok": "#15803d",
        "--ok-subtle": "#ecfdf5",
        "--warn": "#b45309",
        "--warn-subtle": "#fffbeb",
        "--danger": "#b91c1c",
        "--danger-subtle": "#fef2f2",
        "--info": "#1d4ed8",
    },
}

#: The side panel's real width. The default stays what it was, so an existing command
#: line renders the same picture it did before ``--width`` existed.
DEFAULT_WIDTH = 430
DEFAULT_THEME = "dark"


def walk(value: object, path: str) -> object:
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return MISSING
        current = current[key]
    return current


def build_page(
    directory: Path,
    fixture: Path,
    width: int = DEFAULT_WIDTH,
    theme: str = DEFAULT_THEME,
) -> tuple[str, list[str], list[str]]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    fragment = (directory / "template.html").read_text(encoding="utf-8")
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    folds = {name: entry["value"] for name, entry in doc["folds"].items()}
    # What the crewmate wrote with ``dashboard_write``, served to the page from the
    # fixture's own ``agentic`` map. A fixture that omits a name falls back to the
    # stand-in sentence below, so the fixtures written before this key keep rendering.
    written = doc.get("agentic")
    written = written if isinstance(written, dict) else {}

    fields: dict[str, object] = {}
    agentic: list[str] = []
    unresolved: list[str] = []
    for name, spec in manifest["fields"].items():
        source = spec["source"]
        if source.get("agentic") is True:
            agentic.append(name)
            # An agentic value has no fold to resolve against, so it comes from the
            # fixture or, absent that, stands in as one sentence. Either way the page
            # marks it as agent-written, which is the behaviour being screenshotted.
            fields[name] = written.get(
                name, "Three built-in templates written; every fold path resolves."
            )
            continue
        got = walk(folds.get(source["fold"]), source["path"])
        if got is MISSING:
            unresolved.append(f"{name} -> {source['fold']}.{source['path']}")
            continue
        fields[name] = got

    seq = max((entry.get("seq") or 0) for entry in doc["folds"].values())
    api = {"fields": fields, "agentic": agentic, "seq": seq, "stale": False}
    variables = "\n    ".join(f"{name}: {value};" for name, value in THEMES[theme].items())
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{manifest['id']}</title>
<style>
  :root {{
    {variables}
  }}
  html, body {{ margin: 0; background: var(--bg); color: var(--text); }}
  body {{ width: {width}px; }}
  .harness-head {{ font: 600 11px system-ui, sans-serif; color: var(--muted);
    padding: 7px 10px; border-bottom: 1px solid var(--border); }}
</style></head>
<body>
<div class="harness-head">{manifest['title']} &middot; template {manifest['id']} v{manifest['version']}
  &middot; fold seq {seq} &middot; {theme} {width}px</div>
{fragment}
<script>
  window.kirocrew = {json.dumps(api)};
  // The frame's own binding: textContent on each bound element, body descendants only.
  (function () {{
    var nodes = document.body.querySelectorAll('[data-dashboard-field]');
    for (var i = 0; i < nodes.length; i++) {{
      var name = nodes[i].getAttribute('data-dashboard-field');
      if (!Object.prototype.hasOwnProperty.call(window.kirocrew.fields, name)) continue;
      var v = window.kirocrew.fields[name];
      nodes[i].textContent = (v === null || v === undefined)
        ? '\\u2014'
        : (typeof v === 'object' ? JSON.stringify(v) : String(v));
    }}
    // Then one push, so the page's own message handler runs the way it will in the frame.
    window.postMessage({{ type: 'kirocrew:fields' }}, '*');
  }}());
</script>
</body></html>
"""
    return page, agentic, unresolved


def main() -> int:
    args = sys.argv[1:]
    width, theme = DEFAULT_WIDTH, DEFAULT_THEME
    rest: list[str] = []
    # Parsed by hand rather than with argparse so the three positionals keep working
    # exactly as they did: a command line written before these flags existed renders
    # the same picture it rendered then.
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--width" and index + 1 < len(args):
            width, index = int(args[index + 1]), index + 2
            continue
        if token == "--theme" and index + 1 < len(args):
            theme, index = args[index + 1], index + 2
            continue
        rest.append(token)
        index += 1
    if len(rest) != 3:
        print(__doc__)
        return 2
    if theme not in THEMES:
        print(f"REFUSED: theme {theme!r} is not one of {sorted(THEMES)}")
        return 2
    directory, fixture, out = Path(rest[0]), Path(rest[1]), Path(rest[2])
    page, agentic, unresolved = build_page(directory, fixture, width, theme)
    if unresolved:
        print(f"REFUSED: {len(unresolved)} field(s) do not resolve: {unresolved}")
        return 2
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(page)
        html_path = fh.name
    print(f"{directory.name}: agentic={agentic} theme={theme} width={width} page={html_path}")
    shot = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from playwright.sync_api import sync_playwright\n"
            "src, dest, w = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
            "with sync_playwright() as p:\n"
            "    b = p.chromium.launch()\n"
            "    pg = b.new_page(viewport={'width': w, 'height': 900},"
            " device_scale_factor=2)\n"
            "    pg.goto('file://' + src)\n"
            "    pg.wait_for_timeout(700)\n"
            "    pg.screenshot(path=dest, full_page=True)\n"
            "    b.close()\n",
            html_path,
            str(out),
            str(width),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if shot.returncode != 0:
        print(shot.stdout[-2000:])
        print(shot.stderr[-2000:])
        return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

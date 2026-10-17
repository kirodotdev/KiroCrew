#!/usr/bin/env python3
"""Render one built-in template in a plain HTML page and screenshot it.

The harness is the FRAME's two jobs and nothing else: it sets
``window.kirocrew = {fields, agentic, seq, stale, written_at}`` from the captured fold
values, and
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

``--view <text>`` presses the pill whose label contains *text* before shooting,
``--open <n>`` clicks the nth VISIBLE task row (which opens its drawer), and
``--crop <selector>`` shoots that one element instead of the page. Both exist so every
committed screenshot can be reproduced from a checkout: the views this page reaches by
being clicked, and the cards a reviewer is shown on their own, used to need a harness
that was never in the tree, which made the evidence unreproducible and therefore
unauditable.

``--no-head`` drops the harness's own caption line (template, fold seq, theme, width).
It is on by default because a screenshot that cannot say what produced it is not
evidence; it comes off for a shot whose subject is how the PAGE looks -- a comparison
between two templates, where the caption is the one band neither of them drew.

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
    head: bool = True,
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
    # WHEN each of those was written, from the fixture's own map rather than from
    # inside a value. The page compares a judgment against the fold's last advance to
    # decide whether it is still current, so a fixture that wants the live reading has
    # to say its verdict is newer than the log -- which is a property of the capture
    # and belongs in the capture, not in the page's guesswork.
    written_at = doc.get("agentic_written_at")
    written_at = written_at if isinstance(written_at, dict) else {}
    # THE INSTANT THE CAPTURE DESCRIBES. The page draws elapsed figures -- how long a
    # row has been quiet, how long since the log advanced -- against the clock, so
    # without this a fixture of fixed stamps renders a different page every day and
    # the committed evidence drifts away from what the fixture meant. Pinned below by
    # overriding ``Date.now`` for the page, which is the smallest lie that makes the
    # render reproducible: every other clock read on the page is a difference from it.
    frozen = doc.get("now")
    frozen = frozen if isinstance(frozen, str) and frozen else ""

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
    api = {
        "fields": fields,
        "agentic": agentic,
        "seq": seq,
        "stale": False,
        "written_at": written_at,
    }
    variables = "\n    ".join(f"{name}: {value};" for name, value in THEMES[theme].items())
    # BEFORE the fragment, because the template's own script runs on insertion and may
    # read the clock on that first pass. Written as a wrapper over the real `Date` so
    # `Date.parse` and `new Date(...)` keep working -- only "what time is it now" is
    # answered from the capture.
    clock = (
        ""
        if not frozen
        else f"""<script>
  (function () {{
    var pinned = Date.parse({json.dumps(frozen)});
    if (!isFinite(pinned)) return;
    Date.now = function () {{ return pinned; }};
  }}());
</script>"""
    )
    # The harness's own caption line, which names the template, the fold seq, the theme
    # and the width a shot was taken at. On by default, because a screenshot that cannot
    # say what produced it is not evidence. Turned off by ``--no-head`` for a shot whose
    # whole point is how the PAGE looks -- a comparison between two templates, say, where
    # the caption is the one band on the picture neither template drew.
    caption = (
        f"<div class=\"harness-head\">{manifest['title']} &middot; template "
        f"{manifest['id']} v{manifest['version']} &middot; fold seq {seq} &middot; "
        f"{theme} {width}px{' &middot; at ' + frozen if frozen else ''}</div>\n"
        if head
        else ""
    )
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
</style>
{clock}</head>
<body>
{caption}{fragment}
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
    pill, crop, open_row = "", "", -1
    head = True
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
        if token == "--view" and index + 1 < len(args):
            pill, index = args[index + 1], index + 2
            continue
        if token == "--crop" and index + 1 < len(args):
            crop, index = args[index + 1], index + 2
            continue
        if token == "--open" and index + 1 < len(args):
            open_row, index = int(args[index + 1]), index + 2
            continue
        if token == "--no-head":
            head, index = False, index + 1
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
    page, agentic, unresolved = build_page(directory, fixture, width, theme, head)
    if unresolved:
        print(f"REFUSED: {len(unresolved)} field(s) do not resolve: {unresolved}")
        return 2
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(page)
        html_path = fh.name
    print(
        f"{directory.name}: agentic={agentic} theme={theme} width={width} "
        f"view={pill or 'all'} crop={crop or 'page'} "
        f"open={open_row if open_row >= 0 else 'none'} "
        f"head={'on' if head else 'off'} page={html_path}"
    )
    shot = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from playwright.sync_api import sync_playwright\n"
            "src, dest, w = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
            "pill, crop, row = sys.argv[4], sys.argv[5], int(sys.argv[6])\n"
            "with sync_playwright() as p:\n"
            "    b = p.chromium.launch()\n"
            "    pg = b.new_page(viewport={'width': w, 'height': 900},"
            " device_scale_factor=2)\n"
            "    pg.goto('file://' + src)\n"
            "    pg.wait_for_timeout(700)\n"
            # A VIEW the page reaches only by being clicked. The pill row is the page's
            # own switch between every workstream and one of them, so a shot of the
            # one-workstream view has to press it -- which is why this used to need a
            # harness outside the tree, and why the committed evidence could not be
            # reproduced from a checkout.
            "    if pill:\n"
            "        hit = pg.get_by_text(pill, exact=False).first\n"
            "        hit.click(timeout=5000)\n"
            "        pg.wait_for_timeout(600)\n"
            # A TASK ROW BY INDEX, not by its text: a task's title also appears in
            # the Needs-you card, so a text match clicks whichever the page drew
            # first and the shot is of the wrong thing. The index is over the
            # VISIBLE task rows, which is deterministic for a given fixture.
            "    if row >= 0:\n"
            "        rows = pg.locator('.pr-n-task > .pr-n-h')\n"
            "        seen = -1\n"
            "        for i in range(rows.count()):\n"
            "            if not rows.nth(i).is_visible():\n"
            "                continue\n"
            "            seen += 1\n"
            "            if seen == row:\n"
            "                rows.nth(i).click(timeout=5000)\n"
            "                pg.wait_for_timeout(500)\n"
            "                break\n"
            "        else:\n"
            "            raise SystemExit('no visible task row at index %d' % row)\n"
            "    if crop:\n"
            "        node = pg.locator(crop).first\n"
            "        node.scroll_into_view_if_needed(timeout=5000)\n"
            "        pg.wait_for_timeout(250)\n"
            "        node.screenshot(path=dest)\n"
            "    else:\n"
            "        pg.screenshot(path=dest, full_page=True)\n"
            "    b.close()\n",
            html_path,
            str(out),
            str(width),
            pill,
            crop,
            str(open_row),
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

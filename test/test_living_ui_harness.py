"""Pin the living-ui preview harness to the frames it imitates.

``scripts/harness.py`` in the ``living-ui`` built-in skill writes a page that
frames a widget body the way the chat frames an ``<mcwidget>``, or a Dashboard
``view.html`` the way the Dashboard tab frames it. It ships with the skill,
where the web app's source is absent, so it carries its own copy of each
frame's policy, sandbox, base style and default theme values. Each check here
reads that copy and the file that owns the fact, so a change on either side that
would make the preview differ from the real frame fails here. The markup and
scripts around a Dashboard page are the host's own, from ``dashboard_frame``.

These checks pin what the page carries. Only the host's bootstrap is run, in
Node, to show the reads the page sends are the ones it accepts.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import dashboard_frame

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "src" / "kiro_crew" / "builtin_skills" / "living-ui" / "scripts" / "harness.py"
WEB = ROOT / "website" / "src"
WIDGET_SRCDOC = WEB / "lib" / "widgetSrcdoc.ts"
WIDGET_FRAME = WEB / "components" / "WidgetFrame.tsx"
DASHBOARD_TAB = WEB / "pages" / "members" / "CrewDynamicDashboard.tsx"
USE_THEME = WEB / "hooks" / "useTheme.tsx"
# In the order index.css imports them, which is the cascade order.
PALETTE_SHEETS = (WEB / "styles" / "design-tokens.css", WEB / "styles" / "theme-palettes.css")
# The one source of the real frame's policy a file cannot use.
GATEWAY_RUNTIME = "${scriptOrigin}${TAILWIND_RUNTIME_PATH}"


@pytest.fixture(scope="module")
def harness() -> Any:
    return load_skill_script("living_ui_harness_under_test", HARNESS)


def _config(page: str) -> dict[str, Any]:
    found = re.search(r"^const CONFIG = (\{.*\});$", page, re.M)
    assert found, "the page carries no CONFIG line; update this test"
    config: dict[str, Any] = json.loads(found.group(1))
    return config


def _directives(policy: str) -> dict[str, list[str]]:
    return {words[0]: words[1:] for words in (p.split() for p in policy.split(";")) if words}


def _csp_for(offline_scripts: bool = False) -> dict[str, list[str]]:
    """What ``cspFor`` returns, as directives: for a widget, or with ``offlineScripts``
    for the Dashboard tab."""
    source = WIDGET_SRCDOC.read_text(encoding="utf-8")
    body = source.split("const cspFor")[1].split("const BASE_BODY_CSS")[0]
    branch = r"\1" if offline_scripts else r"\2"
    body, picked = re.subn(r"\(offlineScripts \? ('[^']*') : ('[^']*')\)", branch, body)
    assert picked == 1, "cspFor no longer picks a widget's sources in one ternary; update this test"
    policy = "".join(m.group(2) for m in re.finditer(r"([\"'`])(.*?)\1", body))
    return _directives(policy)


#: The fetch directives a policy that omits them takes from ``default-src``.
_FETCH = (
    "script-src",
    "style-src",
    "img-src",
    "font-src",
    "connect-src",
    "frame-src",
    "object-src",
)


def _allowed(policy: dict[str, list[str]], name: str) -> frozenset[str] | None:
    """The sources *policy* allows for directive *name*, or None where nothing limits it."""
    sources = policy.get(name)
    if sources is None and name in _FETCH:
        sources = policy.get("default-src")
    return None if sources is None else frozenset(s for s in sources if s != "'none'")


def _theme_var_names() -> list[str]:
    source = WIDGET_SRCDOC.read_text(encoding="utf-8")
    block = re.search(r"export const THEME_VAR_NAMES = \[(.*?)\] as const", source, re.S)
    assert block, "THEME_VAR_NAMES not found in widgetSrcdoc.ts; update this test"
    return re.findall(r"'(--[a-z-]+)'", block.group(1))


def _default_palette(mode: str) -> dict[str, str]:
    """Every custom property the default palette's *mode* root resolves to.

    Reads blocks whose selector list names ``:root`` or the palette's
    ``data-theme`` value, in source order, so a later declaration wins as it does
    in the cascade (both selectors weigh the same).
    """
    hooks = USE_THEME.read_text(encoding="utf-8")
    default = re.search(r"export const DEFAULT_COLOR_THEME: ColorTheme = '([a-z0-9-]+)'", hooks)
    assert default, "DEFAULT_COLOR_THEME not found in useTheme.tsx; update this test"
    # themeDataAttribute spells the emerald palette as the bare mode.
    slug = default.group(1)
    attribute = mode if slug == "emerald" else f"{slug}-{mode}"
    wanted = {":root", f'[data-theme="{attribute}"]'}
    declared: dict[str, str] = {}
    for sheet in PALETTE_SHEETS:
        css = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if wanted & {s.strip() for s in selectors.split(",")}:
                for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+)", body):
                    declared[name] = value.strip()
    return declared


def test_the_page_carries_the_widget_frames_csp(harness: Any) -> None:
    frame = _csp_for()
    assert GATEWAY_RUNTIME in frame.get(
        "script-src", []
    ), "the widget frame no longer loads the gateway's Tailwind runtime; update this test"
    frame["script-src"].remove(GATEWAY_RUNTIME)
    assert _directives(harness.FRAME_CSP) == frame
    page = harness.page("<p>x</p>")
    assert _config(page)["csp"] == harness.FRAME_CSP
    assert '<meta http-equiv="Content-Security-Policy" content="${CONFIG.csp}">' in page


def test_the_page_carries_the_default_palette_in_both_sets(harness: Any) -> None:
    names = _theme_var_names()
    assert names, "THEME_VAR_NAMES is empty; update this test"
    themes = _config(harness.page("<p>x</p>"))["themes"]
    assert sorted(themes) == ["dark", "light"]
    for mode in ("dark", "light"):
        palette = _default_palette(mode)
        missing = [name for name in names if name not in palette]
        assert not missing, f"the default {mode} palette sets no {missing}"
        expected = {name: palette[name] for name in names}
        assert harness.THEMES[mode] == expected, f"harness.py THEMES[{mode!r}] drifted"
        assert themes[mode] == expected


def test_the_frame_is_sandboxed_like_the_chat_widget_frame(harness: Any) -> None:
    source = WIDGET_FRAME.read_text(encoding="utf-8")
    # The widget frame names its grant once, as AGENT_PAGE_SANDBOX, on the inline
    # frame and on the popped-out one; a literal grant beside it would be a second
    # policy the harness does not mirror.
    assert not re.findall(r'sandbox="([^"]*)"', source)
    assert not re.findall(r"setAttribute\('sandbox', '([^']*)'\)", source)
    assert source.count("sandbox={AGENT_PAGE_SANDBOX}") == 1
    assert source.count("setAttribute('sandbox', AGENT_PAGE_SANDBOX)") == 1
    declared = re.findall(
        r"export const AGENT_PAGE_SANDBOX = '([^']*)'", WIDGET_SRCDOC.read_text(encoding="utf-8")
    )
    assert declared == [harness.FRAME_SANDBOX]
    # Without it the frame has an opaque origin: no storage, no cookies, no page.
    assert "allow-same-origin" not in harness.FRAME_SANDBOX.split()
    page = harness.page("<p>x</p>")
    assert _config(page)["sandbox"] == harness.FRAME_SANDBOX
    assert "frame.setAttribute('sandbox', CONFIG.sandbox);" in page


_LINK_SCENARIO = r"""
const posted = [];
const handlers = {};
// A listener added while a click is dispatched (the shim's last word on a modified
// click) runs after the page's, as it does on a real window.
let late = [];
let dispatching = false;
const window = {
  addEventListener: (type, fn) => { if (dispatching) late.push(fn); else handlers[type] = fn; },
};
const parent = { postMessage: (message) => { posted.push(message.url); } };
const document = { baseURI: 'file:///tmp/preview.html' };
function link(attrs) {
  return {
    nodeType: 1, localName: 'a', attrs,
    hasAttribute(k) { return k in this.attrs; },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = v; },
    hasAttributeNS(ns, k) { return ns === 'http://www.w3.org/1999/xlink' && ('xlink:' + k) in this.attrs; },
    getAttributeNS(ns, k) { return this.hasAttributeNS(ns, k) ? this.attrs['xlink:' + k] : null; },
    closest() { return null; },
  };
}
function fire(type, a, extra = {}, page = null) {
  const e = { defaultPrevented: false, composedPath: () => [a], ...extra };
  e.preventDefault = function () { e.defaultPrevented = true; };
  dispatching = true;
  handlers[type](e);
  dispatching = false;
  if (page) page(e);
  late.splice(0).forEach((fn) => fn(e));
  return e.defaultPrevented;
}
__SHIM__
const bare = link({ href: 'https://example.test/bare' });
const seen = {
  blank: fire('click', link({ href: 'https://example.test/blank', target: '_blank' })),
  bare: fire('click', bare),
  bareTarget: bare.attrs.target,
  cancelled: fire('click', link({ href: 'https://example.test/panel', target: '_blank' }), {}, (e) => e.preventDefault()),
  ctrl: fire('click', link({ href: 'https://example.test/ctrl' }), { ctrlKey: true }),
  ctrlCancelled: fire('click', link({ href: 'https://example.test/ctrl-panel' }), { ctrlKey: true }, (e) => e.preventDefault()),
  middle: fire('auxclick', link({ href: 'https://example.test/middle', target: '_self' }), { button: 1 }),
  self: fire('click', link({ href: 'https://example.test/self', target: '_self' })),
  mail: fire('click', link({ href: 'mailto:a@example.test', target: '_blank' })),
  relative: fire('click', link({ href: '//example.test/rel', target: '_blank' })),
  svg: fire('click', link({ 'xlink:href': 'https://example.test/svg', target: '_blank' })),
};
// window.open relays a web URL and returns null; any other scheme posts nothing.
seen.openReturned = window.open('https://example.test/scripted');
window.open('javascript:alert(1)');
setTimeout(() => { console.log(JSON.stringify({ seen, posted })); }, 20);
"""


def test_a_widget_preview_hands_web_links_to_the_page(harness: Any) -> None:
    """The frame's link relay, run in Node over stand-in links and events."""
    node = shutil.which("node")
    assert node is not None, "Node is needed on PATH to run the frame's link relay"
    done = subprocess.run(
        [node, "-"],
        check=False,
        input=_LINK_SCENARIO.replace("__SHIM__", harness.LINK_SHIM),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    # A plain click is left to the sandbox; a middle or modified one is cancelled now.
    assert out["seen"] == {
        "blank": False,
        "bare": False,
        "bareTarget": "_blank",
        "cancelled": True,
        "ctrl": True,
        "ctrlCancelled": True,
        "middle": True,
        "self": False,
        "mail": False,
        "relative": False,
        "svg": False,
        "openReturned": None,
    }
    # Posted, except where the page cancelled the click: a plain click a task later, a
    # middle or modified one from the shim's last listener, so the order differs.
    assert sorted(out["posted"]) == [
        "https://example.test/bare",
        "https://example.test/blank",
        "https://example.test/ctrl",
        "https://example.test/middle",
        "https://example.test/rel",
        "https://example.test/scripted",
        "https://example.test/svg",
    ]


_PAGE_OPEN_SCENARIO = r"""
const listeners = {};
const opened = [];
function element(id) {
  return {
    id, style: {}, dataset: {}, children: [], removed: false, title: '', textContent: '',
    setAttribute() {}, append(child) { this.children.push(child); child.parent = this; },
    focus() { doc.activeElement = this; }, blur() { if (doc.activeElement === this) doc.activeElement = null; },
    remove() { this.removed = true; }, contains(t) { return t === this; },
    addEventListener(type, fn) { this['on' + type] = fn; },
  };
}
const frame = element('lui-frame');
frame.contentWindow = {};
const nodes = { 'lui-bar': element('lui-bar'), 'lui-frame': frame, 'lui-errors': element('lui-errors') };
const doc = {
  activeElement: null, documentElement: { style: {} }, body: { style: {} },
  getElementById: (id) => nodes[id], createElement: () => element('button'),
};
const nav = { userActivation: { isActive: true } };
const win = { open: (url) => { opened.push(url); } };
const on = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
const fire = (type, e = {}) => (listeners[type] || []).forEach((fn) => fn(e));
let clock = 100000;
performance.now = () => clock;
new Function('CONFIG', 'document', 'navigator', 'location', 'addEventListener', 'window', SCRIPT)(
  CONFIG, doc, nav, { search: '' }, on, win);
const ask = (url) => fire('message', { source: frame.contentWindow, data: { type: 'lui-preview-open', url } });
const offers = () => nodes['lui-bar'].children.filter((c) => !c.removed);
const out = {};
// Focus came in from the page, no page input for a while: opens at once.
frame.focus(); fire('blur');
ask('https://example.test/a');
out.tied = { opened: opened.splice(0), offers: offers().length };
// A click on the page's bar, then the frame takes focus back: the button instead.
fire('focus'); fire('pointerdown', { target: nodes['lui-bar'] }); frame.focus();
ask('https://example.test/b');
const button = offers()[0];
out.borrowed = { opened: opened.splice(0), offers: offers().length, label: button && button.textContent, title: button && button.title };
// The person's own click on the button opens it.
button.onclick();
out.clicked = { opened: opened.splice(0), offers: offers().length };
// Long after any page input, but focus reached the frame without coming in from
// the page: still the button.
clock += 6000;
frame.focus();
ask('https://example.test/c');
out.unentered = { opened: opened.splice(0), offers: offers().length };
offers().forEach((c) => c.remove());
// Focus came in from the page, but a key on the page is still live: the button.
fire('focus'); frame.focus(); fire('blur'); fire('keydown', { target: nodes['lui-bar'] });
ask('https://example.test/d');
out.recent = { opened: opened.splice(0), offers: offers().length };
offers().forEach((c) => c.remove());
// The dashboard bridge's URL rule: a user name or password, or another scheme, is refused.
clock += 6000;
fire('focus'); frame.focus(); fire('blur');
ask('https://user:pass@example.test/e');
ask('javascript:alert(1)');
out.refused = { opened: opened.splice(0), offers: offers().length };
console.log(JSON.stringify(out));
"""


def test_a_preview_opens_only_a_click_tied_to_its_frame_and_offers_a_button_otherwise(
    harness: Any,
) -> None:
    """The page script's open path, run in Node over stand-ins for the page."""
    node = shutil.which("node")
    assert node is not None, "Node is needed on PATH to run the page script"
    config = _config(harness.page("<p>x</p>"))
    program = "\n".join(
        (
            f"const CONFIG = {json.dumps(config)};",
            f"const SCRIPT = {json.dumps(harness._PAGE_SCRIPT)};",
            _PAGE_OPEN_SCENARIO,
        )
    )
    done = subprocess.run(
        [node, "-"],
        check=False,
        input=program,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["tied"] == {"opened": ["https://example.test/a"], "offers": 0}
    assert out["borrowed"] == {
        "opened": [],
        "offers": 1,
        "label": "Open example.test",
        "title": "https://example.test/b",
    }
    assert out["clicked"] == {"opened": ["https://example.test/b"], "offers": 0}
    assert out["unentered"] == {"opened": [], "offers": 1}
    assert out["recent"] == {"opened": [], "offers": 1}
    assert out["refused"] == {"opened": [], "offers": 0}


def test_the_page_opens_a_link_only_right_after_a_click_in_the_frame(harness: Any) -> None:
    page = harness.page("<p>x</p>")
    assert _config(page)["shims"]["link"] == harness.LINK_SHIM
    assert "navigator.userActivation.isActive" in page
    assert "document.activeElement === frame" in page
    assert "window.open(url, '_blank', 'noopener,noreferrer')" in page
    # The Dashboard tab has no link bridge, and neither does its preview.
    dashboard = _config(harness.dashboard_page("<p>x</p>", {"n": 1}))
    assert "link" not in dashboard["shims"]


def test_the_frame_body_starts_from_the_same_style(harness: Any) -> None:
    source = WIDGET_SRCDOC.read_text(encoding="utf-8")
    block = re.search(r"const BASE_BODY_CSS =\s*((?:\"[^\"]*\"\s*\+?\s*)+)", source)
    assert block, "BASE_BODY_CSS not found in widgetSrcdoc.ts; update this test"
    assert harness.BASE_BODY_CSS == "".join(re.findall(r'"([^"]*)"', block.group(1)))
    page = harness.page("<p>x</p>")
    assert _config(page)["baseCss"] == harness.BASE_BODY_CSS
    # The theme rules buildThemeCss writes after the base style.
    for rule in (
        "`:root{${rootBody};color-scheme:${mode}}`",
        "`html{background:var(--bg)}`",
        "`body{background:var(--bg);color:var(--text)}`",
    ):
        assert rule in source, f"buildThemeCss no longer writes {rule}; update this test"
        assert rule in page


def test_the_page_reads_the_theme_reduced_and_slow_flags(harness: Any) -> None:
    page = harness.page("<p>x</p>")
    for read in ("query.get('theme')", "query.has('reduced')", "query.get('slow')"):
        assert read in page
    shims = _config(page)["shims"]
    assert "if (reduced) shims.push(CONFIG.shims.reduced);" in page
    assert "window.matchMedia = function" in shims["reduced"]
    assert "mediaText = force(" in shims["reduced"]
    assert "CONFIG.shims.slow.replace('__SPEED__', String(slow))" in page
    assert "performance.now = function" in shims["slow"]
    assert "window.requestAnimationFrame = function" in shims["slow"]


def test_the_page_bounds_the_complete_error_line(harness: Any) -> None:
    page = harness.page("<p>x</p>")
    # The filename must share the message's bound before the line becomes a seen key.
    bounded = "const text = `${where}: ${String(data.text)}`.slice(0, 500);"
    assert bounded in page
    assert "String(data.file)" not in page.split(bounded, 1)[1]


def test_the_page_counts_error_reports_past_the_cap(harness: Any) -> None:
    script = harness._PAGE_SCRIPT
    capped = script.index("seen.size >= 50")
    # Past the cap a report is counted and shown, never dropped in silence.
    assert "dropped += 1" in script[capped : capped + 80]
    assert "seen.size >= 50) return" not in script
    assert "more error report(s) past the first 50" in script


def test_a_body_cannot_end_the_page_script(harness: Any) -> None:
    body = "<script>let a = '</script><!-- \u2028 é';</script>"
    page = harness.page(body, "a.html")
    code, after = page.split("<script>\n", 1)[1].split("</script>", 1)
    assert after.startswith("\n</body>"), "the page's script ended inside the widget body"
    assert "<!--" not in code
    assert _config(page)["body"] == body


def test_reads_the_body_out_of_one_mcwidget_tag(harness: Any) -> None:
    tag = '<mcwidget title="Q">\n<p>x</p>\n</mcwidget>\n'
    assert harness.widget_body(tag) == ("\n<p>x</p>\n", 0)
    # Lines above the tag still count, so an error line maps back to the file.
    assert harness.widget_body("\n\n" + tag) == ("\n<p>x</p>\n", 2)
    assert harness.widget_body("<p>x</p>") == ("<p>x</p>", 0)
    assert _config(harness.page("\n\n" + tag))["above"] == 2
    assert "data.line - offset + CONFIG.above" in harness.page(tag)


def test_imports_nothing_that_could_open_a_socket() -> None:
    """The page is a file and nothing serves it, so a new import is a choice made here.

    ``kiro_crew`` is imported for the host's own Dashboard frame code, which builds
    markup and opens nothing. ``subprocess`` only re-runs this script under the
    interpreter ``KIROCREW_RUNTIME_PYTHON`` names; ``os`` reads that variable and
    the script's own path; ``typing`` holds an annotation.
    """
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    imported |= {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert imported == {
        "__future__",
        "argparse",
        "html",
        "json",
        "kiro_crew",
        "os",
        "pathlib",
        "re",
        "subprocess",
        "sys",
        "typing",
    }


def test_writes_one_file_and_prints_each_variant(
    harness: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    widget = tmp_path / "queue.html"
    widget.write_text("<p>hi</p>", encoding="utf-8")
    assert harness.main([str(widget)]) == 0
    written = tmp_path / "queue.preview.html"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["queue.html", "queue.preview.html"]
    assert _config(written.read_text(encoding="utf-8"))["body"] == "<p>hi</p>"
    printed = capsys.readouterr().out
    url = written.resolve().as_uri()
    for theme in ("dark", "light"):
        for flags in ("", "&reduced=1", "&slow=0.05"):
            assert f"{url}?theme={theme}{flags}\n" in printed


def test_refuses_a_missing_widget_and_writing_over_it(harness: Any, tmp_path: Path) -> None:
    widget = tmp_path / "w.html"
    assert harness.main([str(widget), "-o", str(tmp_path / "p.html")]) == 2
    widget.write_text("<p>x</p>", encoding="utf-8")
    alias = tmp_path / "alias.html"
    os.link(widget, alias)
    for out in (widget, alias):
        assert harness.main([str(widget), "-o", str(out)]) == 2
    assert widget.read_text(encoding="utf-8") == "<p>x</p>"


# --------------------------------------------------------------------------- #
# The Dashboard mode
# --------------------------------------------------------------------------- #


def _host_read(harness: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """The read the page loads at: the host's own payload, in the preview's generation."""
    return {
        **dashboard_frame.read_payload(fields),
        "version": harness.FIRST_VERSION,
        "generation": harness.GENERATION,
    }


def _push_read(harness: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """A push's read: the host's own payload as a patch of the same generation."""
    return {**dashboard_frame.read_payload(fields), "generation": harness.GENERATION, "patch": True}


def test_the_dashboard_page_carries_the_dashboard_tabs_csp(harness: Any) -> None:
    tab = _csp_for(offline_scripts=True)
    assert GATEWAY_RUNTIME in tab.get(
        "script-src", []
    ), "the Dashboard tab no longer loads the gateway's Tailwind runtime; update this test"
    # The tab's policy and the composed page's own meet on every directive.
    composed = _directives(dashboard_frame.FRAME_CSP)
    page_policy = _directives(harness.DASHBOARD_CSP)
    for name in set(tab) | set(composed) | set(_FETCH):
        mine, theirs = _allowed(tab, name), _allowed(composed, name)
        met = theirs if mine is None else mine if theirs is None else mine & theirs
        assert (
            _allowed(page_policy, name) == met
        ), f"{name} differs from where the two policies meet"
    assert page_policy["script-src"] == ["'unsafe-inline'"]
    tab["script-src"].remove(GATEWAY_RUNTIME)
    assert page_policy == tab
    # A widget may load a script from two CDNs; a Dashboard page from none.
    for origin in dashboard_frame.RESOURCE_ORIGINS:
        assert origin in _directives(harness.FRAME_CSP)["script-src"]
        assert origin not in harness.DASHBOARD_CSP
    page = harness.dashboard_page("<p>x</p>", {})
    assert _config(page)["csp"] == harness.DASHBOARD_CSP


def test_the_dashboard_frame_is_sandboxed_like_the_dashboard_tab(harness: Any) -> None:
    # The Dashboard tab imports its grant from the one definition in lib/.
    grant_file = WEB / "lib" / "crewDashboardSandbox.ts"
    assert "from '../../lib/crewDashboardSandbox'" in DASHBOARD_TAB.read_text(encoding="utf-8")
    grants = re.search(
        r"export const CREW_DASHBOARD_SANDBOX = '([^']*)'",
        grant_file.read_text(encoding="utf-8"),
    )
    assert (
        grants
    ), "CREW_DASHBOARD_SANDBOX not found in lib/crewDashboardSandbox.ts; update this test"
    assert harness.DASHBOARD_SANDBOX == grants.group(1)
    assert "allow-same-origin" not in harness.DASHBOARD_SANDBOX.split()
    page = harness.dashboard_page("<p>x</p>", {})
    assert _config(page)["sandbox"] == harness.DASHBOARD_SANDBOX
    assert "frame.setAttribute('sandbox', CONFIG.sandbox);" in page


def test_the_dashboard_body_is_the_one_the_host_composes(harness: Any) -> None:
    """The body, the reads and the message type are the host's own, not a copy."""
    cases = (
        ('<p data-dashboard-field="n">-</p>', {"n": 3}),
        ("<script>let s = '</script>';</script>\n", {"s": "</script><!-- & > \u2028\u2029 é"}),
        ("", {}),
        ("<ol>\n\n</ol>", {"none": None, "list": [], "deep": {"k": [1, {"z": True}]}}),
    )
    for view, fields in cases:
        pushes = (("next.json", {"n": 4}), ("later.json", fields))
        config = _config(harness.dashboard_page(view, fields, pushes))
        body = config["body"]
        assert body == dashboard_frame.compose_body(view, _host_read(harness, fields))
        # The host's own lines open the body, so the view starts `-above` lines in.
        assert config["above"] < 0
        start = -config["above"]
        lines = body.split("\n")
        assert lines[start].startswith(view.split("\n")[0])
        assert lines[start : start + view.count("\n") + 1] == view.split("\n")
        assert [push["read"] for push in config["pushes"]] == [
            _push_read(harness, more) for _, more in pushes
        ]
        assert config["dataType"] == dashboard_frame.DATA_MESSAGE_TYPE


def test_a_dashboard_page_reads_the_flags_and_maps_a_line_to_the_view(harness: Any) -> None:
    view = "<p>a</p>\n<p>b</p>\n"
    page = harness.dashboard_page(view, {"n": 1}, name="view.html")
    config = _config(page)
    # The widget mode's own script builds the frame, so ?theme, ?reduced and ?slow work here.
    assert f"{harness._PAGE_SCRIPT}\n{harness._DASHBOARD_SCRIPT}" in page
    assert config["themes"] == harness.THEMES and config["baseCss"] == harness.BASE_BODY_CSS
    assert set(config["shims"]) == {"reduced", "slow", "report"}
    # A frame line maps to a file line as `line - offset + above`, and the host's own
    # lines open the body, so the view's first line is body line 1 - above.
    assert config["body"] == dashboard_frame.compose_body(view, _host_read(harness, {"n": 1}))
    assert config["body"].split("\n")[-config["above"]] == "<p>a</p>"
    assert config["name"] == "view.html"


def test_a_view_cannot_end_the_dashboard_page_script(harness: Any) -> None:
    view = "<script>let a = '</script><!-- \u2028';</script>"
    page = harness.dashboard_page(view, {"s": "</script><!--"}, (("next.json", {"s": "</b>"}),))
    code, after = page.split("<script>\n", 1)[1].split("</script>", 1)
    assert after.startswith("\n</body>"), "the page's script ended inside the view"
    assert "<!--" not in code


def test_each_push_is_the_next_patch_of_the_same_read(harness: Any) -> None:
    pushes = (("next.json", {"n": 2}), ("later.json", {"m": "x"}))
    config = _config(harness.dashboard_page("<p>x</p>", {"n": 1}, pushes))
    island = re.search(
        r'<script type="application/json" id="kirocrew-dashboard-data">(.*?)</script>',
        config["body"],
    )
    assert island, "the frame body carries no data island"
    loaded = json.loads(island.group(1))
    assert loaded["version"] == config["version"] == harness.FIRST_VERSION
    assert [push["name"] for push in config["pushes"]] == ["next.json", "later.json"]
    for push, (_, fields) in zip(config["pushes"], pushes):
        assert push["read"] == _push_read(harness, fields)
        assert push["read"]["generation"] == loaded["generation"]
    script = harness._DASHBOARD_SCRIPT
    assert "version += 1;" in script
    assert "{ type: CONFIG.dataType, read: { ...push.read, version } }" in script
    # Pressed before the frame's bootstrap listens, a push would be lost and the next
    # one would arrive as a gap.
    assert "button.disabled = true;" in script
    # A view that reloads its frame starts a new bootstrap at the island's version.
    loaded = script.split("frame.addEventListener('load'", 1)[1]
    assert "version = CONFIG.version;" in loaded


#: A DOM just big enough for the host's bootstrap: one bound cell, the data island,
#: window listeners, and a parent that records what the frame posts to it.
_FRAME_SHIM = r"""
const listeners = {};
globalThis.window = globalThis;
globalThis.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
globalThis.dispatchEvent = (event) => { for (const fn of listeners[event.type] || []) fn(event); return true; };
globalThis.Event = class { constructor(type) { this.type = type; } };
const posted = [];
globalThis.parent = { postMessage: (message) => { posted.push(message); } };
const cell = {
  attrs: { 'data-dashboard-field': 'n' },
  textContent: 'as written',
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
  setAttribute(name, value) { this.attrs[name] = String(value); },
  removeAttribute(name) { delete this.attrs[name]; },
};
const island = { textContent: ISLAND };
globalThis.document = {
  readyState: 'loading',
  documentElement: { setAttribute() {}, lang: '' },
  getElementById: (id) => (id === 'kirocrew-dashboard-data' ? island : null),
  querySelectorAll: (selector) => (selector === '[data-dashboard-field]' ? [cell] : []),
};
"""

#: A page that subscribes to one field and listens for the refill event, then the
#: preview's two pushes, sent from the parent the way its page script sends them.
_FRAME_SCENARIO = r"""
const heard = [];
window.kirocrew.subscribe('n', (value, name) => heard.push([name, value]));
addEventListener('kirocrew-dashboard', () => heard.push(['refill', window.kirocrew.fields.n]));
const look = () => ({
  fields: { ...window.kirocrew.fields }, cell: cell.textContent, heard: heard.splice(0),
  posted: posted.splice(0),
});
const out = [look()];
document.readyState = 'interactive';
for (const fn of listeners.DOMContentLoaded || []) fn();
out.push(look());
let version = CONFIG.version;
for (const push of CONFIG.pushes) {
  version += 1;
  const data = { type: CONFIG.dataType, read: { ...push.read, version } };
  for (const fn of listeners.message || []) fn({ source: parent, data });
  out.push(look());
}
console.log(JSON.stringify(out));
"""


def test_the_frame_answers_fields_and_subscribe_as_the_host_does(harness: Any) -> None:
    """Run the host's own bootstrap over the island and pushes the page carries."""
    node = shutil.which("node")
    assert node is not None, "Node is needed on PATH to run the frame's bootstrap"
    pushes = (("next.json", {"n": 5}), ("later.json", {"m": "x"}))
    config = _config(harness.dashboard_page("<p>x</p>", {"n": 2, "m": "a"}, pushes))
    island = config["body"].split('id="kirocrew-dashboard-data">', 1)[1].split("</script>", 1)[0]
    program = "\n".join(
        (
            f"const ISLAND = {json.dumps(island)};",
            f"const CONFIG = {json.dumps(config)};",
            _FRAME_SHIM,
            dashboard_frame._BOOTSTRAP_JS,
            _FRAME_SCENARIO,
        )
    )
    # On stdin: the program is longer than a Windows command line may be.
    done = subprocess.run(
        [node, "-"],
        input=program,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    loaded, filled, first, second = json.loads(done.stdout.strip().splitlines()[-1])
    # A subscriber fires at once, on the sample, before any cell is filled.
    assert loaded == {
        "fields": {"n": 2, "m": "a"},
        "cell": "as written",
        "heard": [["n", 2]],
        "posted": [],
    }
    assert filled["cell"] == "2" and filled["heard"] == []
    # Each push is applied: merged into the fields, the cell refilled, the field's
    # subscriber called, then the refill event. No push is read as a gap.
    assert first == {
        "fields": {"n": 5, "m": "a"},
        "cell": "5",
        "heard": [["n", 5], ["refill", 5]],
        "posted": [],
    }
    assert second == {
        "fields": {"n": 5, "m": "x"},
        "cell": "5",
        "heard": [["refill", 5]],
        "posted": [],
    }


def test_writes_a_dashboard_preview_and_names_each_push(
    harness: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    view = tmp_path / "view.html"
    view.write_text(
        "<b data-dashboard-field=\"n\">-</b><i data-dashboard-field='gone'></i>",
        "utf-8",
    )
    (tmp_path / "now.json").write_text('{"n": 2}', encoding="utf-8")
    (tmp_path / "next.json").write_text('{"n": 3}', encoding="utf-8")
    argv = [str(view), "--fields", "now.json", "--push", "next.json"]
    assert harness.main(argv) == 0
    written = tmp_path / "view.preview.html"
    config = _config(written.read_text(encoding="utf-8"))
    assert config["csp"] == harness.DASHBOARD_CSP
    assert '"fields": {"n": 2}' in config["body"]
    assert config["pushes"] == [{"name": "next.json", "read": _push_read(harness, {"n": 3})}]
    captured = capsys.readouterr()
    url = written.resolve().as_uri()
    for theme in ("dark", "light"):
        for flags in ("", "&reduced=1", "&slow=0.05"):
            assert f"{url}?theme={theme}{flags}\n" in captured.out
    assert "a button above the frame pushes next.json" in captured.out
    assert captured.err == ""


def test_the_dashboard_mode_reruns_under_the_runtime_python(
    harness: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Python that cannot import Kiro Crew hands the run to the one that can, once."""
    monkeypatch.setattr(harness, "_host_frame", lambda: None)
    view = tmp_path / "view.html"
    view.write_text('<b data-dashboard-field="n">-</b>', encoding="utf-8")
    fields = tmp_path / "now.json"
    fields.write_text('{"n": 2}', encoding="utf-8")
    out = tmp_path / "view.preview.html"
    argv = [str(view.resolve()), "--fields", str(fields.resolve()), "-o", str(out.resolve())]
    # The child is real, and bounded like the Node test's, so a hang fails the test.
    started: list[tuple[list[str], str | None]] = []
    real_call = subprocess.call

    def bounded(command: list[str], **kwargs: Any) -> int:
        started.append((command, kwargs["env"].get("LIVING_UI_HARNESS_REEXEC")))
        return real_call(command, timeout=60, cwd=str(tmp_path), **kwargs)

    monkeypatch.setattr(harness.subprocess, "call", bounded)
    monkeypatch.delenv("LIVING_UI_HARNESS_REEXEC", raising=False)
    monkeypatch.setenv("KIROCREW_RUNTIME_PYTHON", sys.executable)
    assert harness.main(argv) == 0
    assert started == [([sys.executable, "-I", "-B", str(HARNESS), *argv], "1")]
    config = _config(out.read_text(encoding="utf-8"))
    assert config["csp"] == harness.DASHBOARD_CSP
    assert config["body"] == dashboard_frame.compose_body(
        view.read_text(encoding="utf-8"), _host_read(harness, {"n": 2})
    )
    out.unlink()
    capsys.readouterr()
    refusal = "frames the page with Kiro Crew's own code"
    monkeypatch.delenv("KIROCREW_RUNTIME_PYTHON")
    assert harness.main(argv) == 2
    assert refusal in capsys.readouterr().err
    # The run already started under the runtime Python does not start another.
    monkeypatch.setenv("KIROCREW_RUNTIME_PYTHON", sys.executable)
    monkeypatch.setenv("LIVING_UI_HARNESS_REEXEC", "1")
    assert harness.main(argv) == 2
    assert refusal in capsys.readouterr().err
    assert not out.exists()


def test_refuses_fields_that_are_not_one_json_object(
    harness: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    view = tmp_path / "view.html"
    view.write_text("<p>x</p>", encoding="utf-8")
    out = tmp_path / "p.html"
    for name, text in (
        ("broken.json", '{"n": }'),
        ("list.json", "[1, 2]"),
        ("nan.json", '{"n": NaN}'),
        ("huge.json", '{"n": [1e400]}'),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8")
        assert harness.main([str(view), "--fields", str(tmp_path / name), "-o", str(out)]) == 2
        assert name in capsys.readouterr().err
    good = tmp_path / "now.json"
    good.write_text('{"n": 1}', encoding="utf-8")
    bad_push = str(tmp_path / "nan.json")
    assert harness.main([str(view), "--fields", str(good), "--push", bad_push, "-o", str(out)]) == 2
    assert "nan.json" in capsys.readouterr().err
    assert not out.exists()
    # The page may not be written over any input.
    assert harness.main([str(view), "--fields", str(good), "-o", str(good)]) == 2
    assert good.read_text(encoding="utf-8") == '{"n": 1}'
    with pytest.raises(SystemExit) as refused:
        harness.main([str(view), "--push", str(good)])
    assert refused.value.code == 2

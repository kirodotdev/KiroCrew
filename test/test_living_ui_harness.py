"""Pin the living-ui preview harness to the chat widget frame it imitates.

``scripts/harness.py`` in the ``living-ui`` built-in skill writes a page that
frames a widget body the way the chat frames an ``<mcwidget>``. It ships with
the skill, where the web app's source is absent, so it carries its own copy of
the frame's policy, sandbox, base style and default theme values. Each check
here reads that copy and the web app file that owns the fact, so a change on
either side that would make the preview differ from the real frame fails here.

These checks pin what the page carries. They do not run its script.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "src" / "kiro_crew" / "builtin_skills" / "living-ui" / "scripts" / "harness.py"
WEB = ROOT / "website" / "src"
WIDGET_SRCDOC = WEB / "lib" / "widgetSrcdoc.ts"
WIDGET_FRAME = WEB / "components" / "WidgetFrame.tsx"
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


def _widget_frame_csp() -> dict[str, list[str]]:
    """What ``cspFor`` returns for a widget (``offlineScripts`` false), as directives."""
    source = WIDGET_SRCDOC.read_text(encoding="utf-8")
    body = source.split("const cspFor")[1].split("const BASE_BODY_CSS")[0]
    body, picked = re.subn(r"\(offlineScripts \? ('[^']*') : ('[^']*')\)", r"\2", body)
    assert picked == 1, "cspFor no longer picks a widget's sources in one ternary; update this test"
    policy = "".join(m.group(2) for m in re.finditer(r"([\"'`])(.*?)\1", body))
    return _directives(policy)


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
    frame = _widget_frame_csp()
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
    grants = set(re.findall(r'sandbox="([^"]*)"', source))
    grants |= set(re.findall(r"setAttribute\('sandbox', '([^']*)'\)", source))
    assert grants == {harness.FRAME_SANDBOX}
    # Without it the frame has an opaque origin: no storage, no cookies, no page.
    assert "allow-same-origin" not in harness.FRAME_SANDBOX.split()
    page = harness.page("<p>x</p>")
    assert _config(page)["sandbox"] == harness.FRAME_SANDBOX
    assert "frame.setAttribute('sandbox', CONFIG.sandbox);" in page


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
    """The page is a file and nothing serves it, so a new import is a choice made here."""
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    imported |= {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert imported == {"__future__", "argparse", "html", "json", "pathlib", "re", "sys"}


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

"""No agent-facing text may point at the deleted Playwright MCP surface.

The Playwright MCP proxy is gone: the ``browser_*`` tools, ``browse_outline`` /
``browse_search``, the managed ``playwright-mcp`` server entry and the Browser
Mode toggle were all removed when browsing moved to ``playwright-cli`` shell
commands. Prose and agent specs did not move with it, and a stale reference here
fails in the one way nothing else catches: the agent is *told* to call a tool
that no longer exists, so it either does nothing or relays a remedy pointing at
a setting the user cannot find. Neither shows up as an error.

So this file scans every surface an agent actually reads -- skills, app agent
specs, the system prompt -- and fails on the removed names. It is a ratchet, not
a description: it does not care WHY a reference appears, only that none does.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "kiro_crew"

# Names the migration deleted. ``browser_[a-z]`` covers the whole 22-tool
# surface (browser_navigate, browser_snapshot, browser_take_screenshot, ...)
# without pinning a list that would rot, and the ``*`` alternative catches the
# ``browser_*`` glob that prose used to refer to the set as a whole -- the exact
# spelling two of the stale files used. ``browser_cli`` is ours and is the one
# spelling that must stay legal.
REMOVED = re.compile(
    r"\bbrowser_(?!cli\b)([a-z]|\*)|\bbrowse_outline\b|\bbrowse_search\b|@playwright-mcp"
)


def _agent_facing_files() -> list[Path]:
    """Every file whose text or config reaches a live agent."""
    files: list[Path] = [SRC / "config" / "prompt.md"]
    files += sorted((SRC / "builtin_skills").rglob("SKILL.md"))
    files += sorted((SRC / "apps" / "builtins").glob("*/skills/**/SKILL.md"))
    files += sorted((SRC / "apps" / "builtins").glob("*/agents/*.json"))
    files += sorted((ROOT / "skills").rglob("SKILL.md"))
    return files


def test_scan_covers_the_surfaces_it_claims_to() -> None:
    """A glob that silently matches nothing would make every case below vacuous."""
    files = _agent_facing_files()
    assert len(files) > 20, f"expected the full skill/agent corpus, got {len(files)}"
    names = {p.name for p in files}
    assert "prompt.md" in names
    assert "advisor.json" in names, "app agent specs must be in scope"
    # The two app skill trees the migration left behind live here.
    assert any("personal-shopper" in str(p) for p in files)
    assert any("feature-demo-recording" in str(p) for p in files)


def test_no_agent_facing_file_names_a_removed_browser_tool() -> None:
    offenders: list[str] = []
    for path in _agent_facing_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if REMOVED.search(line):
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert not offenders, (
        "these files still point agents at the deleted Playwright MCP surface; "
        "browsing is `playwright-cli` shell commands now:\n  " + "\n  ".join(offenders)
    )


def test_pattern_catches_the_references_this_change_removed() -> None:
    """The regex is the whole test -- prove it fires on the real prior text."""
    assert REMOVED.search("just use `browser_take_screenshot`")
    assert REMOVED.search("If the browser_* tools are NOT in your tool list")
    assert REMOVED.search('    "@playwright-mcp",')
    assert REMOVED.search("use browse_outline to compress the snapshot")
    # ...and not on our own package, or the CLI it wraps.
    assert not REMOVED.search("from kiro_crew.browser_cli import install")
    assert not REMOVED.search("playwright-cli open https://example.com")

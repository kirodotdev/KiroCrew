"""The front-end self-verification loop must stay wired end to end.

Three files have to agree for an agent to actually look at its own UI change:
the ``web-verify`` skill (the how), ``config/prompt.md`` (the permission — a
blanket screenshot prohibition here silently disables the skill), and the
sibling skills that route work to it. Each assertion below locks one of those
joints; drop any of them and the loop goes quiet without a test failing.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "src" / "kiro_crew" / "builtin_skills"
WEB_VERIFY = SKILLS / "web-verify" / "SKILL.md"
WEB_BROWSE = SKILLS / "web-browse" / "SKILL.md"
WEB_PREVIEW = SKILLS / "web-preview" / "SKILL.md"
PREPARE_PR = SKILLS / "kirocrew-dev" / "prepare-pr" / "SKILL.md"
PROMPT = ROOT / "src" / "kiro_crew" / "config" / "prompt.md"


def test_web_verify_skill_exists_with_frontmatter() -> None:
    body = WEB_VERIFY.read_text(encoding="utf-8")
    assert body.startswith("---\n"), "skill needs YAML frontmatter to be discoverable"
    assert "name: web-verify" in body
    assert "triggers:" in body
    # The loop is worthless if the agent never looks at the frame it captured.
    assert "Read" in body and "browser_take_screenshot" in body


def test_web_verify_keeps_the_playwright_guard() -> None:
    """No silent pretending when no browser backend is installed."""
    body = WEB_VERIFY.read_text(encoding="utf-8")
    assert "browser_*" in body
    assert "kirocrew browse setup" in body
    assert "npm install -g agent-browser" in body


def test_web_verify_names_all_three_capture_backends() -> None:
    """A missing Playwright MCP browser must not read as 'verification impossible'."""
    body = WEB_VERIFY.read_text(encoding="utf-8")
    assert "Playwright MCP" in body
    assert "agent-browser open" in body and "agent-browser screenshot" in body
    assert "pod-e2e" in body
    # The panel stream is specific to the MCP path — don't let the CLI imply it.
    assert "not in the Browser panel" in body


def test_web_verify_bounds_the_frame_count() -> None:
    """The cap left the prompt; it has to be enforced somewhere."""
    body = WEB_VERIFY.read_text(encoding="utf-8")
    assert "One or two frames" in body


def test_prompt_drops_the_screenshot_prohibition() -> None:
    """The old blanket rule made front-end self-verification a policy violation."""
    prompt = PROMPT.read_text(encoding="utf-8")
    assert "**DO NOT use `browser_take_screenshot`**" not in prompt


def test_prompt_authorizes_view_only_self_verification() -> None:
    # Browsing is gated by tool availability and the agent decides when to use
    # it; the prompt names visual verification as a reason to reach for the
    # browser tools and points at web-verify, so front-end self-verification is
    # permitted rather than a policy violation.
    prompt = PROMPT.read_text(encoding="utf-8")
    assert "visual verification" in prompt
    assert "web-verify" in prompt, "prompt.md must name the skill for it to be reachable"


def test_prompt_leaves_the_how_to_in_the_skill() -> None:
    """Deliberate: capture mechanics belong in skills, not the system prompt.

    The prompt grants permission and names the skill; everything about which
    backend to use, how many frames, and how to caption them lives in
    web-verify. Growing the prompt with per-skill procedure is the thing this
    design decision rejects.
    """
    prompt = PROMPT.read_text(encoding="utf-8")
    assert "agent-browser" not in prompt
    assert "pod-e2e" not in prompt
    body = WEB_VERIFY.read_text(encoding="utf-8")
    assert "agent-browser" in body and "pod-e2e" in body


def test_prompt_shows_screenshots_in_chat() -> None:
    prompt = PROMPT.read_text(encoding="utf-8")
    assert "![" in prompt and "/absolute/path.png" in prompt


def test_siblings_route_to_web_verify() -> None:
    assert "web-verify" in WEB_BROWSE.read_text(encoding="utf-8")
    assert "web-verify" in WEB_PREVIEW.read_text(encoding="utf-8")


def test_prepare_pr_sources_screenshots_from_verification() -> None:
    body = PREPARE_PR.read_text(encoding="utf-8")
    assert "web-verify" in body

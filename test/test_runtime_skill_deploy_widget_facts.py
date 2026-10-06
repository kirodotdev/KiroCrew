"""Pin runtime skill text to the code facts it describes.

Each check reads the code that owns a fact and the skill that states it, so a
change to either side that makes the skill wrong fails here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SKILL = ROOT / "src/kiro_crew/deploy/skills/artifact-deploy/SKILL.md"
DEPLOY_WEB_SKILL = ROOT / "src/kiro_crew/deploy/skills/deploy-web/SKILL.md"
DETACH = ROOT / "src/kiro_crew/deploy/skills/artifact-deploy/scripts/detach_backend.py"
HANDLERS = ROOT / "src/kiro_crew/deploy/handlers.py"
WIDGETS_SKILL = ROOT / "src/kiro_crew/builtin_skills/widgets/SKILL.md"
WIDGET_SRCDOC = ROOT / "website/src/lib/widgetSrcdoc.ts"


def _required_flags(path: Path) -> set[str]:
    """Flags passed to ``add_argument`` with ``required=True``."""
    flags: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"):
            continue
        if any(
            k.arg == "required" and getattr(k.value, "value", False) is True for k in node.keywords
        ):
            flags.update(a.value for a in node.args if isinstance(a, ast.Constant))
    return flags


def test_detach_usage_names_every_required_flag():
    required = _required_flags(DETACH)
    assert required, "detach_backend.py has no required flags; update this test"
    line = next(
        ln
        for ln in DEPLOY_SKILL.read_text(encoding="utf-8").splitlines()
        if ln.startswith("- **Lifecycle**")
    )
    usage = line[line.index("detach_backend.py") :]
    for flag in required:
        assert flag in usage, f"Lifecycle usage omits required {flag}"


def test_verify_is_denied_to_agents_and_skill_does_not_tell_them_to_call_it():
    src = HANDLERS.read_text(encoding="utf-8")
    assert re.search(r"@_internal_denied\s*\nasync def _handle_verify\b", src)
    skill = DEPLOY_SKILL.read_text(encoding="utf-8")
    assert "Verify access" in skill
    assert not re.search(r"confirm reachability via the core verify endpoint", skill, re.I)


def test_webapp_slug_refusal_code_matches_both_skills():
    assert '"webapp_root_unavailable"' in HANDLERS.read_text(encoding="utf-8")
    for skill in (DEPLOY_SKILL, DEPLOY_WEB_SKILL):
        text = skill.read_text(encoding="utf-8")
        assert "webapp_root_unavailable" in text, skill
        assert '`kind="webapp"` is rejected' not in text, skill


def test_widget_action_prefills_and_form_keys_match_collector():
    skill = WIDGETS_SKILL.read_text(encoding="utf-8")
    assert "auto-submits" not in skill
    assert "PRE-FILLS" in skill
    collector = WIDGET_SRCDOC.read_text(encoding="utf-8")
    assert "inp.name || inp.id || inp.getAttribute('data-field')" in collector
    for key in ("`name`", "`id`", "`data-field`"):
        assert key in skill, f"widgets skill omits form key {key}"

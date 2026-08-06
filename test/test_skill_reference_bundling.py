"""Guard: a shipped skill must not point an installed user at a skill that isn't
shipped.

Only ``src/kiro_crew/builtin_skills/`` (and an enabled app's ``skills/``) reach a
pip/PyPI user's ``~/.kiro/crew/skills/``; the top-level ``skills/`` tree is
repo-checkout-only. So a *packaged* skill that references an *unpackaged* one
resolves to nothing on an installed machine -- silently (AGENTS.md, "LLM-facing
capabilities": a referenced skill MUST live in ``builtin_skills/``).

``babysit`` -- the same-session ``monitor_start`` loop, a shipped feature the
agent prompt drives every user to -- is referenced by the packaged ``prepare-pr``
skill, so it must ship. ``kirocrew-worktree-dev`` is the opposite case: it is
scoped to the Kiro Crew repo itself and is deliberately kept repo-checkout-only
(dev-fleet.md), so no packaged skill may reference it.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_SKILLS = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills"
APP_SKILL_ROOTS = sorted((REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins").glob("*/skills"))
TOPLEVEL_SKILLS = REPO_ROOT / "skills"


def _skill_names(*roots: Path) -> set[str]:
    """Leaf directory name of every ``SKILL.md`` under *roots* -- the identity a
    skill is referenced by (e.g. ``babysit``, ``kirocrew-worktree-dev``)."""
    names: set[str] = set()
    for root in roots:
        if root.is_dir():
            names |= {p.parent.name for p in root.rglob("SKILL.md")}
    return names


def _packaged_skill_files() -> list[Path]:
    files: list[Path] = []
    for root in (BUILTIN_SKILLS, *APP_SKILL_ROOTS):
        if root.is_dir():
            files += sorted(root.rglob("SKILL.md"))
    return files


def test_babysit_reaches_an_installed_users_skills_home(tmp_path, monkeypatch):
    """The real sync (`_ensure_builtin_skills`) must land babysit in the user's
    skills home, so the packaged prepare-pr's "see the `babysit` skill" resolves."""
    from kiro_crew.skills import _ensure_builtin_skills

    # Simulate a pip install: no repo checkout, so the top-level skills/ tree is
    # unreachable and only builtin_skills/ can supply the skill.
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    _ensure_builtin_skills(tmp_path)

    assert (tmp_path / "kirocrew-dev" / "babysit" / "SKILL.md").is_file()


def test_worktree_dev_stays_repo_only_for_installed_users(tmp_path, monkeypatch):
    """kirocrew-worktree-dev is deliberately NOT bundled; a plain install must not
    receive it (guards against accidentally promoting it)."""
    from kiro_crew.skills import _ensure_builtin_skills

    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    _ensure_builtin_skills(tmp_path)

    assert not (tmp_path / "kirocrew-dev" / "kirocrew-worktree-dev").exists()


def test_no_packaged_skill_references_a_repo_only_skill():
    """Every skill named by a packaged skill must itself be packaged."""
    packaged = _skill_names(BUILTIN_SKILLS, *APP_SKILL_ROOTS)
    repo_only = _skill_names(TOPLEVEL_SKILLS) - packaged
    assert repo_only, "expected at least one repo-only skill (fixture drift?)"

    offenders: list[str] = []
    for skill_md in _packaged_skill_files():
        text = skill_md.read_text(encoding="utf-8")
        for name in repo_only:
            if name in text:
                rel = skill_md.relative_to(REPO_ROOT)
                offenders.append(f"{rel} references repo-only skill '{name}'")

    assert not offenders, (
        "a packaged skill references a skill that never reaches an installed user; "
        "bundle the target into builtin_skills/ or drop the reference:\n  " + "\n  ".join(offenders)
    )

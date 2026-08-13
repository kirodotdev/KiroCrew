"""Coverage for ``kiro_crew.conductor_skill`` — the always-loaded delegation guide.

The generated SKILL.md is injected into every default-agent turn, so the
contract worth pinning is: it lands at ``<loader._dir>/conductor/SKILL.md``
(creating the directory), it is marked ``always: true``, it points the model at
``select_crew`` rather than inlining a roster, and regenerating overwrites in
place instead of appending.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kiro_crew.conductor_skill import generate_conductor_skill


def _loader(root: Path) -> SimpleNamespace:
    return SimpleNamespace(_dir=root / "skills")


def test_writes_skill_under_conductor_dir(tmp_path: Path) -> None:
    out = generate_conductor_skill(_loader(tmp_path))

    assert out == tmp_path / "skills" / "conductor" / "SKILL.md"
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    assert body.startswith("---\nalways: true\n---")


def test_roster_is_resolved_via_select_crew_not_inlined(tmp_path: Path) -> None:
    """The roster must come from the tool at decision time — an inlined roster
    would go stale whenever crews change and bloat an always-on skill."""
    body = generate_conductor_skill(_loader(tmp_path)).read_text(encoding="utf-8")

    assert 'select_crew(crew="<name>")' in body
    assert 'spawn_run(agent="<name>"' in body


def test_regeneration_overwrites_in_place(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    first = generate_conductor_skill(loader)
    first.write_text("stale-nonsense", encoding="utf-8")

    second = generate_conductor_skill(loader)

    assert second == first
    assert second.read_text(encoding="utf-8").startswith("---")
    assert "stale-nonsense" not in second.read_text(encoding="utf-8")

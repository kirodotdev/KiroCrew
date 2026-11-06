"""A steering folder past the document cap loads up to the cap and names the rest.

Global steering is read because it exists, and a Bot inherits it by default, so a
user with more than ``_MAX_DOCUMENTS`` steering files must not have every Bot turn
refused. Steering that loads is kept in sorted order up to the cap (a conditional
file takes no slot), the rest is left out
and named in-band, and the member's own sources (persona, ``SOUL.md``, declared
resources) are never the ones dropped.

The shared ``env`` fixture is in ``member_essential_context_helpers``.
"""

import os
import sys
from pathlib import Path

import member_essential_context_helpers
import pytest

from kiro_crew import member_essential_context as _mec

_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env

_CAP = _mec._MAX_DOCUMENTS
_EXTRA = 19  # the reported 83 steering files


def _many_global_steering() -> list[Path]:
    steering = Path.home() / ".kiro" / "steering" / "synced"
    steering.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(_CAP + _EXTRA):
        path = steering / f"rule-{index:03d}.md"
        path.write_text(f"GLOBAL-RULE-{index:03d}", encoding="utf-8")
        paths.append(path)
    return paths


def test_steering_past_the_cap_is_named_and_the_members_own_sources_stay(env, caplog):
    paths = _many_global_steering()
    with caplog.at_level("WARNING"):
        documents = _mec.documents_for_member("writer-template", str(env.project))
    sources = [source for source, _ in documents]
    bodies = "\n".join(body for _, body in documents)

    # The member's own sources are all present.
    assert "Bound Soul: preserve the user's voice." in bodies
    assert "Project Soul: write with empathy." in bodies
    assert "Declared guide: examples must be reproducible." in bodies
    # Steering is kept in sorted order up to the cap; nothing past it is read.
    kept = [path for path in paths if str(path) in sources]
    assert kept == paths[: len(kept)]
    assert len(kept) >= _CAP - 8
    assert f"GLOBAL-RULE-{_CAP + _EXTRA - 1:03d}" not in bodies
    loaded = [s for s in sources if not s.startswith("essential-context#")]
    assert len(loaded) <= _CAP

    note = dict(documents)[f"{_mec.ESSENTIAL_OVER_CAP_SKIP_SOURCE}:writer-template"]
    # Every dropped global file is counted (project steering past the cap too).
    count = int(note.split("SNAPSHOT. ", 1)[1].split(" ", 1)[0])
    assert count >= len(paths) - len(kept)
    assert "steering files were left out" in note
    assert f"at most {_CAP} documents" in note
    assert str(paths[len(kept)]) in note
    assert any("document cap" in r.getMessage() for r in caplog.records)


def test_native_and_projected_steering_lists_do_not_refuse_past_the_cap(env):
    paths = _many_global_steering()
    sources = dict(_mec.kiro_launch_documents("writer-template", str(env.project)))
    assert str(paths[0]) in sources and str(paths[-1]) not in sources

    project_steering = env.project / ".kiro" / "steering"
    for index in range(_CAP + 1):
        (project_steering / f"p-{index:03d}.md").write_text(f"P-{index}", encoding="utf-8")
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    assert 0 < len(documents) <= _CAP
    assert "P-0" in documents.values()


def test_over_cap_note_names_each_file_once():
    first, second = Path("a.md"), Path("b.md")
    assert _mec._skipped_over_cap_note([], "t") is None
    source, body = _mec._skipped_over_cap_note([first, second, first], "t")
    assert source == f"{_mec.ESSENTIAL_OVER_CAP_SKIP_SOURCE}:t"
    assert body.count(str(first)) == 1 and "2 steering files" in body


@pytest.mark.parametrize("spelling", ["canonical", "dotdot"])
def test_a_steering_file_reused_as_the_persona_is_never_evicted(env, spelling):
    import json

    # One global file, AGENTS.md, SOUL.md and always.md plus these rule files
    # fill the cap exactly; the project glob matches exactly the cap (the three
    # conditional files count there but are not loaded). The persona is the last
    # steering file kept, the one a later source would evict.
    (Path.home() / ".kiro" / "steering").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".kiro" / "steering" / "g.md").write_text("GLOBAL", encoding="utf-8")
    steering = env.project / ".kiro" / "steering"
    paths = []
    for index in range(_CAP - 4):
        path = steering / f"rule-{index:03d}.md"
        path.write_text(f"PROJECT-RULE-{index:03d}", encoding="utf-8")
        paths.append(path)
    persona = paths[-1].resolve()  # the spelling the steering scan returns
    # The same file, spelled through ``..``; deduplication normalizes it.
    declared = (
        persona if spelling == "canonical" else persona.parent / ".." / "steering" / persona.name
    )
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "prompt": f"file://{declared}", "resources": []}),
        encoding="utf-8",
    )
    core: set[str] = set()
    documents = _mec.documents_for_member(
        "writer-template", str(env.project), context_settings=True, core_sources_out=core
    )
    assert dict(documents).get(str(persona)) == f"PROJECT-RULE-{_CAP - 5:03d}"
    assert str(persona) in core
    if spelling != "dotdot":
        return

    # Two files sorted ahead push the persona past the scan's cap, so the scan
    # leaves it out and the persona read delivers it under its ``..`` label.
    # The note must not name it, and its count is the always-steering files
    # actually missing from the snapshot.
    early = [steering / "a-0.md", steering / "a-1.md"]
    for path in early:
        path.write_text("EARLY", encoding="utf-8")
    documents = _mec.documents_for_member(
        "writer-template", str(env.project), context_settings=True
    )
    by_path = {os.path.abspath(source): body for source, body in documents}
    assert by_path.get(str(persona)) == f"PROJECT-RULE-{_CAP - 5:03d}"
    note = dict(documents)[f"{_mec.ESSENTIAL_OVER_CAP_SKIP_SOURCE}:writer-template"]
    assert str(persona) not in note
    always = [Path.home() / ".kiro" / "steering" / "g.md", *early, steering / "always.md", *paths]
    missing = sum(os.path.abspath(p.resolve()) not in by_path for p in always)
    assert missing >= 1
    assert f"SNAPSHOT. {missing} steering file" in note


def test_conditional_steering_sorted_first_takes_no_slot(env):
    steering = Path.home() / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    conditional = []
    for index, inclusion in enumerate(["manual", "fileMatch", "auto"] * 4):
        path = steering / f"a-{index:02d}.md"
        path.write_text(f"---\ninclusion: {inclusion}\n---\nCOND-{index}", encoding="utf-8")
        conditional.append(path)
    always = []
    for index in range(_CAP + 6):
        path = steering / f"b-{index:03d}.md"
        path.write_text(f"ALWAYS-{index:03d}", encoding="utf-8")
        always.append(path)

    documents = _mec.documents_for_member("writer-template", str(env.project))
    sources = {source for source, _ in documents}
    loaded = [s for s in sources if not s.startswith("essential-context#")]
    # Conditional files take no slot, so always files fill the snapshot to the cap.
    assert len(loaded) == _CAP
    assert str(always[0]) in sources
    note = dict(documents)[f"{_mec.ESSENTIAL_OVER_CAP_SKIP_SOURCE}:writer-template"]
    assert not any(str(path) in note for path in conditional)
    project_always = env.project / ".kiro" / "steering" / "always.md"
    missing = sum(str(p) not in sources for p in [*always, project_always.resolve()])
    assert f"SNAPSHOT. {missing} steering files were left out" in note

    launch = dict(_mec.kiro_launch_documents("writer-template", str(env.project)))
    assert sum(str(path) in launch for path in always) == _CAP


def test_left_out_and_core_comparisons_fold_case(env, monkeypatch):
    """Both sides of each identity comparison go through ``normcase``, as ``Path``
    equality does on a case-insensitive host."""
    folded: list[str] = []

    def lower(path):
        # Only this module's own comparisons count, not normcase calls elsewhere.
        if sys._getframe(1).f_code.co_filename == _mec.__file__:
            folded.append(str(path))
        return str(path).lower()

    monkeypatch.setattr(_mec.os.path, "normcase", lower)
    _many_global_steering()
    documents = _mec.documents_for_member(
        "writer-template", str(env.project), context_settings=True
    )
    assert f"{_mec.ESSENTIAL_OVER_CAP_SKIP_SOURCE}:writer-template" in dict(documents)
    # Path components, so the check holds for either separator.
    parts = [Path(item).parts for item in folded]
    assert any(".kiro" in p and "agents" in p for p in parts)
    assert any(".kiro" in p and "steering" in p for p in parts)

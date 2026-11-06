"""A steering folder past the document cap loads up to the cap and names the rest.

Global steering is read because it exists, and a Bot inherits it by default, so a
user with more than ``_MAX_DOCUMENTS`` steering files must not have every Bot turn
refused. The first files in sorted order are kept, the rest are left out unread
and named in-band, and the member's own sources (persona, ``SOUL.md``, declared
resources) are never the ones dropped.

The shared ``env`` fixture is in ``member_essential_context_helpers``.
"""

from pathlib import Path

import member_essential_context_helpers

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

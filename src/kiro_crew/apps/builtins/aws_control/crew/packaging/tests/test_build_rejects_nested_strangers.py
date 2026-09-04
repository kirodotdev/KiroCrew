"""``build`` replaces ``--out`` recursively, so it must know the whole directory.

The first version of that guard listed owned TOP-LEVEL names. ``skills`` is one of
them, so a directory holding only ``skills/notes.txt`` passed the check and then had
notes.txt deleted by the recursive replace -- the check examined the container while
the delete reached the contents. These tests pin the nested case, and pin that
closing it did not break the documented plan/sign/build flow, which legitimately
re-uses the same ``--out``.
"""

from __future__ import annotations

import json

import pytest

from .test_producer import load_build, make_crew


def _build(mod, crew, out):
    """One real build into ``out``, using the suite's deny-all (no plan) shape."""
    spec = mod.read_agent_spec(crew)
    return mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


def _built_bundle(tmp_path):
    """Run one real build and return its output directory."""
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    crew = mod.resolve_crew("frontdesk", src)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    return mod, crew, out


def test_a_nested_stranger_is_refused(tmp_path):
    """A file the build never wrote, nested under an owned directory name."""
    mod, crew, out = _built_bundle(tmp_path)
    stray = out / "skills" / "notes.txt"
    stray.write_text("the owner's own notes\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as excinfo:
        _build(mod, crew, out)

    assert "did not write" in str(excinfo.value) or "does not match" in str(excinfo.value)
    assert stray.is_file(), "the refusal must happen BEFORE the delete, not after"
    assert stray.read_text(encoding="utf-8") == "the owner's own notes\n"


def test_a_bundle_shaped_directory_without_a_manifest_is_refused(tmp_path):
    """No manifest means the build cannot prove it produced what it is deleting."""
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    crew = mod.resolve_crew("frontdesk", src)
    out = tmp_path / "handmade"
    (out / "skills").mkdir(parents=True)
    (out / "skills" / "notes.txt").write_text("mine\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused, match="no manifest.json"):
        _build(mod, crew, out)
    assert (out / "skills" / "notes.txt").is_file()


def test_rebuilding_a_clean_previous_bundle_still_works(tmp_path):
    """The ordinary case must not become a refusal."""
    mod, crew, out = _built_bundle(tmp_path)
    _build(mod, crew, out)  # must not raise
    assert (out / "manifest.json").is_file()


def test_the_plan_flow_still_works(tmp_path):
    """plan, then build with the same --out: the plan survives and is not a stranger.

    The plan is written into staging AFTER the manifest digest is taken, so it is
    absent from the recorded digest. The verification skips it for exactly that
    reason; if it stopped skipping it, this test fails rather than the flow silently
    breaking again.
    """
    mod, crew, out = _built_bundle(tmp_path)
    plan_path = out / mod.PLAN_FILENAME
    plan_path.write_text(json.dumps({"select": []}) + "\n", encoding="utf-8")

    _build(mod, crew, out)  # must not raise

    assert plan_path.is_file(), "the plan must be carried across the swap"
    assert json.loads(plan_path.read_text(encoding="utf-8")) == {"select": []}

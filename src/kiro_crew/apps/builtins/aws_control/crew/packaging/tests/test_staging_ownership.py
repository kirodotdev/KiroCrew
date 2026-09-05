"""The staging path is derived from ``--out``, so it can already be the owner's.

``build_bundle`` writes into ``<out>.staging`` and starts by removing whatever is
there. That path is not chosen by the build -- it is ``--out`` with a suffix -- so an
owner can have a directory at exactly that name, either their own or one a killed
build left behind. Deleting it unconditionally is the same destructive hole the
``--out`` ownership check closes, and it sat one line above that check.
"""

from __future__ import annotations

import pytest

from .test_producer import load_build, make_crew


def _crew(mod, tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    return mod.resolve_crew("frontdesk", src)


def _build(mod, crew, out):
    spec = mod.read_agent_spec(crew)
    return mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


def test_a_stranger_in_the_staging_path_is_refused(tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    staging = tmp_path / "bundle.staging"
    (staging / "notes").mkdir(parents=True)
    keep = staging / "notes" / "mine.txt"
    keep.write_text("the owner's own work\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused, match="did not write"):
        _build(mod, crew, out)

    assert keep.is_file(), "the refusal must come BEFORE the delete"
    assert keep.read_text(encoding="utf-8") == "the owner's own work\n"


def test_a_leftover_from_a_killed_build_is_still_cleaned(tmp_path):
    """The refusal must not turn an interrupted build into a permanent block.

    Everything a build leaves in staging is a name the build writes, so that case is
    recognisable and is cleaned rather than refused.
    """
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    staging = tmp_path / "bundle.staging"
    (staging / "skills" / "faq").mkdir(parents=True)
    (staging / "skills" / "faq" / "SKILL.md").write_text("half-written\n", encoding="utf-8")
    (staging / "agent.json").write_text("{}\n", encoding="utf-8")

    _build(mod, crew, out)  # must NOT raise

    assert (out / "manifest.json").is_file()
    assert not staging.exists(), "staging is removed after a successful swap"


def test_an_empty_staging_path_is_fine(tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    (tmp_path / "bundle.staging").mkdir(parents=True)
    _build(mod, crew, tmp_path / "bundle")  # must NOT raise


def test_both_deletes_share_one_vocabulary(tmp_path):
    """The out_dir check and the staging check must not drift apart.

    They are two recursive deletes governed by one idea of what the build owns. Two
    copies of that list would let one accept a name the other refuses, so the module
    is asserted to hold exactly one.
    """
    mod = load_build()
    assert mod._STAGING_OWNED_TOP_LEVEL == frozenset(
        {"agent.json", "mcp.json", "manifest.json", "skills", mod.PLAN_FILENAME}
    )
    import inspect

    src = inspect.getsource(mod.build_bundle)
    assert 'owned = {"agent.json"' not in src, (
        "build_bundle spells the owned-name set out again instead of using "
        "_STAGING_OWNED_TOP_LEVEL"
    )

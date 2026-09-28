"""A plain FILE at ``--out`` or at the staging path must be refused, not crashed on.

``Path.exists()`` is true for a file, so the two residue scans that follow it --
``staging.rglob("*")`` and ``out_dir.iterdir()`` -- raised an uncaught
``NotADirectoryError``. Reproduced for each before this suite existed, and in both cases the
staging directory was left on disk by the crash, so a retry then met leftovers it had to
reason about.

The refusal is the same answer the residue scans already give for content the build does not
own; it just has to arrive BEFORE anything is created. Both halves are asserted on the
outcome the caller sees -- a clean ``ExportRefused`` -- and on the disk being left alone.
"""

from __future__ import annotations

import os

import pytest

from .test_producer import load_build, make_crew

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _crew(mod, tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    return mod.resolve_crew("frontdesk", src)


def _build(mod, crew, out):
    spec = mod.read_agent_spec(crew)
    return mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


def _staging_of(out):
    return out.parent / (out.name + ".staging")


def test_a_file_at_out_is_refused_not_crashed_on(tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    out.write_text("not a bundle\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, crew, out)
    if os.name == "posix":
        assert "not a directory" in str(caught.value)
    else:
        assert "POSIX-only" in str(caught.value)

    assert out.is_file(), "the refusal must leave the owner's file alone"
    assert out.read_text(encoding="utf-8") == "not a bundle\n"


def test_a_file_at_out_leaves_no_staging_residue(tmp_path):
    """The crash left a staging directory behind, which a retry then had to explain."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    out.write_text("not a bundle\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, out)

    assert not _staging_of(out).exists(), "the refusal created staging and left it"


def test_a_file_at_the_staging_path_is_refused(tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    stray = _staging_of(out)
    stray.write_text("someone else's file\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, crew, out)
    if os.name == "posix":
        assert "not a directory" in str(caught.value)
    else:
        assert "POSIX-only" in str(caught.value)

    assert stray.is_file(), "the owner's file at the staging path was destroyed"
    assert stray.read_text(encoding="utf-8") == "someone else's file\n"
    assert not out.exists(), "nothing should have been written to --out"


@_posix_only
def test_a_fresh_out_dir_still_builds(tmp_path):
    """The guards must not refuse the ordinary case."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    assert (out / "manifest.json").is_file()
    assert not _staging_of(out).exists(), "staging should not survive a successful build"


@_posix_only
def test_rebuilding_over_a_previous_bundle_still_works(tmp_path):
    """A previous bundle is a directory, so the new guards must not see it as a stranger."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    _build(mod, crew, out)
    assert (out / "manifest.json").is_file()

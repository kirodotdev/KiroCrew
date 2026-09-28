"""The third and last recursive-delete site: ``<out>.staging``.

Three paths in ``build_bundle`` delete a directory recursively, and each carries a
different subset of the same rule. ``--out`` and ``<out>.previous`` share one function;
staging cannot use it, because staging is filled in incrementally and its manifest is
written near the end, so a directory this build abandoned legitimately has no digest to
verify.

What staging has instead is that this build CREATES it. So it leaves a marker beside it, and
a directory without one was made by someone else whatever it contains. Before that, the name
and shape rules were satisfied by an operator's own directory: ``skills`` is a name the build
writes, so ``<out>.staging/skills/notes.txt`` passed the top-level check and the recursive
delete removed notes.txt.

The marker sits BESIDE staging rather than inside it because ``bundle_digest(staging)`` is a
frozen contract value computed over everything in there -- a file inside would either change
that digest or ship inside the bundle.
"""

from __future__ import annotations

import os
import pathlib

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


def _staging(out: pathlib.Path) -> pathlib.Path:
    return out.parent / (out.name + ".staging")


def _marker(out: pathlib.Path) -> pathlib.Path:
    return out.parent / (out.name + ".staging.owned")


def test_an_unmarked_staging_directory_is_refused(tmp_path):
    """Even when everything in it uses names the build writes."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    theirs = _staging(out)
    (theirs / "skills").mkdir(parents=True)
    (theirs / "skills" / "notes.txt").write_text("my own notes\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, crew, out)
    if os.name == "posix":
        assert "did not create it" in str(caught.value)
    else:
        assert "POSIX-only" in str(caught.value)

    assert (theirs / "skills" / "notes.txt").read_text(encoding="utf-8") == "my own notes\n"


def test_an_unmarked_but_perfectly_bundle_shaped_staging_is_refused(tmp_path):
    """The old name+shape scan passed this: every name is one the build writes."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    theirs = _staging(out)
    theirs.mkdir(parents=True)
    (theirs / "manifest.json").write_text('{"mine": true}\n', encoding="utf-8")
    (theirs / "agent.json").write_text("{}\n", encoding="utf-8")
    (theirs / "skills").mkdir()

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, crew, out)
    if os.name == "posix":
        assert "did not create it" in str(caught.value)
    else:
        assert "POSIX-only" in str(caught.value)

    assert (theirs / "manifest.json").read_text(encoding="utf-8") == '{"mine": true}\n'


@_posix_only
def test_a_marked_staging_directory_is_cleaned_and_the_build_proceeds(tmp_path):
    """What a killed build leaves: the directory AND the marker.

    The marker is written with the module's own body rather than arbitrary text, because
    the ownership check now requires this builder's token. Before it did, any plain file
    satisfied the check -- which is what made an operator's own note beside their own
    ``<name>.staging`` directory authorise a recursive delete of it. That forged case is
    pinned separately in ``test_review_findings_security.py``.
    """
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    abandoned = _staging(out)
    (abandoned / "skills").mkdir(parents=True)
    (abandoned / "agent.json").write_text("{}\n", encoding="utf-8")
    _marker(out).write_text(mod._STAGING_MARKER_BODY, encoding="utf-8", newline="")

    _build(mod, crew, out)
    assert (out / "manifest.json").is_file()


@_posix_only
def test_a_successful_build_leaves_no_marker(tmp_path):
    """A marker left behind is a licence for the next run to delete whatever is there."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    assert not _marker(out).exists()
    assert not _staging(out).exists()


def test_a_failed_build_leaves_no_marker(tmp_path):
    """Otherwise the failure hands the next run permission it should not have."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "quarterly-report.xlsx").write_bytes(b"not mine to delete")

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, out)

    assert not _marker(out).exists(), "the refusal left a marker behind"
    assert not _staging(out).exists()
    assert (out / "quarterly-report.xlsx").read_bytes() == b"not mine to delete"


@_posix_only
def test_the_marker_never_ships_inside_the_bundle(tmp_path):
    """It sits beside staging so the frozen bundle digest is unchanged."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    names = {p.name for p in out.rglob("*")}
    assert not any("staging" in n for n in names), f"a staging artefact shipped: {names}"


@_posix_only
def test_the_bundle_digest_still_covers_what_it_claims(tmp_path):
    """The manifest's recorded digest must still re-derive from the shipped bundle."""
    import json

    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    recorded = json.loads((out / "manifest.json").read_text(encoding="utf-8"))["digest"]
    assert recorded == mod.bundle_digest(out)

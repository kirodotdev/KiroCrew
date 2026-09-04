"""Neither recursive delete may remove an entry it did not write.

``build_bundle`` replaces two directories wholesale -- the ``.staging`` path derived from
``--out``, and ``--out`` itself -- and each is guarded by a scan that refuses content the
build did not produce. Both scans decided that with ``p.is_file()``, which is False for an
empty directory, a FIFO, a socket, a device node and a link to a directory. Every one of
those passed the guard and was then deleted by ``shutil.rmtree``.

Measured before the fix: with the old predicate a FIFO and an empty directory were both
invisible to the scan, while the legitimate ``manifest.json`` and ``skills/`` were
correctly ignored. The subtle case is a link carrying an OWNED name, which the old
name-based test could never see because ``is_file()`` follows the link.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from ..build import _STAGING_OWNED_TOP_LEVEL as OWNED
from ..build import _is_shape_this_build_never_writes as odd
from .test_producer import load_build, make_crew

_needs_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="FIFOs are a POSIX shape; the predicate is total anyway"
)


def _crew(mod, tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    return mod.resolve_crew("frontdesk", src)


def _build(mod, crew, out):
    spec = mod.read_agent_spec(crew)
    return mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


# --- the real entry point, so the CALL SITES are covered and not only the predicate ---


@_needs_fifo
def test_a_fifo_in_the_staging_path_survives_the_build(tmp_path):
    """The property is on disk: the FIFO must still be there after the refusal."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    staging = tmp_path / "bundle.staging"
    staging.mkdir(parents=True)
    fifo = staging / "a_fifo"
    os.mkfifo(fifo)

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, out)

    assert fifo.exists(), "the FIFO was deleted; the scan still cannot see it"


def test_an_owners_empty_directory_in_the_staging_path_survives(tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    staging = tmp_path / "bundle.staging"
    mine = staging / "someones_own_dir"
    mine.mkdir(parents=True)

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, out)

    assert mine.is_dir(), "the empty directory was deleted; the scan still cannot see it"


def test_an_owners_empty_directory_in_out_dir_survives(tmp_path):
    """Regression pin, NOT a proof of the shape guard.

    Measured: the pre-existing ``strangers`` name check already refuses this, because
    ``someones_own_dir`` is not an owned name. Kept so the behaviour cannot regress, and
    labelled so the next reader does not credit it to the shape rule.
    """
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    mine = out / "someones_own_dir"
    mine.mkdir(parents=True)

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, out)

    assert mine.is_dir(), "the empty directory in --out was deleted"


@_needs_fifo
def test_a_fifo_in_out_dir_survives(tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    out.mkdir(parents=True)
    fifo = out / "manifest.json"  # an OWNED name, so only the shape check can catch it
    os.mkfifo(fifo)

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, out)

    assert fifo.exists(), "the FIFO wearing an owned name was deleted"


@_needs_fifo
def test_a_fifo_below_an_owned_directory_survives(tmp_path):
    """The case ONLY the descendant shape scan can reach.

    ``strangers`` uses ``iterdir()``, so it sees ``skills`` -- an owned name -- and never
    looks inside. The digest check cannot see a FIFO either, because that walk keeps only
    ``is_file()`` entries. Without the shape scan this was deleted.
    """
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    (out / "skills").mkdir(parents=True)
    fifo = out / "skills" / "a_fifo"
    os.mkfifo(fifo)

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, out)

    assert fifo.exists(), "a FIFO one level below an owned name was deleted"


def test_a_fresh_out_dir_still_builds(tmp_path):
    """The guards must not refuse the ordinary case."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    report = _build(mod, crew, tmp_path / "bundle")
    assert (tmp_path / "bundle" / "manifest.json").is_file(), report


# --- the predicate itself, case by case ------------------------------------------


def _old_predicate(p: pathlib.Path, root: pathlib.Path) -> bool:
    """The pre-fix scan, kept here so each case states what actually changed."""
    return p.is_file() and p.relative_to(root).parts[0] not in OWNED


def _new_predicate(p: pathlib.Path, root: pathlib.Path) -> bool:
    rel = p.relative_to(root)
    return rel.parts[0] not in OWNED or odd(p)


def test_a_plain_file_and_a_plain_directory_are_shapes_the_build_writes(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "skills").mkdir()
    assert odd(tmp_path / "manifest.json") is False
    assert odd(tmp_path / "skills") is False


def test_an_empty_directory_was_invisible_and_now_is_not(tmp_path):
    d = tmp_path / "someones_own_dir"
    d.mkdir()
    assert _old_predicate(d, tmp_path) is False, "the old scan is supposed to miss this"
    assert _new_predicate(d, tmp_path) is True


@_needs_fifo
def test_a_fifo_was_invisible_and_now_is_not(tmp_path):
    f = tmp_path / "a_fifo"
    os.mkfifo(f)
    assert _old_predicate(f, tmp_path) is False, "the old scan is supposed to miss this"
    assert _new_predicate(f, tmp_path) is True


@_needs_fifo
def test_a_fifo_with_an_owned_name_is_still_refused(tmp_path):
    """Name-based ownership cannot see this one: the shape check is what catches it."""
    f = tmp_path / "manifest.json"
    os.mkfifo(f)
    assert _old_predicate(f, tmp_path) is False
    assert _new_predicate(f, tmp_path) is True


def test_a_link_with_an_owned_name_is_refused(tmp_path):
    """``is_file()`` follows links, so the old scan saw a legitimate manifest here."""
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    link = tmp_path / "manifest.json"
    os.symlink(target, link)
    assert link.is_file() is True, "the link resolves, which is why this was missed"
    assert _old_predicate(link, tmp_path) is False
    assert _new_predicate(link, tmp_path) is True


def test_a_link_to_a_directory_with_an_owned_name_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    link = tmp_path / "skills"
    os.symlink(tmp_path / "real", link)
    assert _old_predicate(link, tmp_path) is False
    assert _new_predicate(link, tmp_path) is True


def test_the_predicate_judges_the_link_not_its_target(tmp_path):
    """A dangling link must be refused too, rather than raising or being ignored."""
    link = tmp_path / "manifest.json"
    os.symlink(tmp_path / "does_not_exist", link)
    assert odd(link) is True

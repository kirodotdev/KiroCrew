"""A swapped PARENT directory must not be traversed on the way to a prompt.

``O_NOFOLLOW`` on a single open refuses a FINAL-component link only. The agents
directory is writable, so an agent can leave the leaf name alone and swap a parent for
a link to ``~/.ssh`` instead: the final component is then a real file, the single-open
check passes, and the prompt that ships inside ``agent.json`` carries the target's
bytes. Measured before the fix -- a parent swap read private key material straight
through.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from ..build import ExportRefused, _read_text_nofollow

pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"),
    reason="the per-component opener needs dir_fd; the single-open fallback is a "
    "documented narrowing on platforms without it",
)


def _tree(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    root = tmp_path / "agents"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "prompt.md").write_text("PRIVATE-KEY-MATERIAL\n")
    (root / "sub" / "prompt.md").write_text("a harmless prompt\n")
    return root, root / "sub" / "prompt.md"


def test_a_legitimate_prompt_still_reads(tmp_path):
    root, p = _tree(tmp_path)
    assert _read_text_nofollow(p, root) == "a harmless prompt\n"


def test_a_swapped_parent_is_refused(tmp_path):
    root, p = _tree(tmp_path)
    os.rename(root / "sub", root / "sub.real")
    os.symlink(tmp_path / "secrets", root / "sub")

    # relative_to() is pure string arithmetic and resolves either way, so a refusal
    # here can only come from the O_NOFOLLOW on the parent component -- not from the
    # path check. Asserted so the test cannot pass for the wrong reason.
    assert p.relative_to(root).parts == ("sub", "prompt.md")

    with pytest.raises(ExportRefused):
        _read_text_nofollow(p, root)


def test_the_secret_bytes_never_come_back(tmp_path):
    """The property that matters is the CONTENT, not which exception was raised."""
    root, p = _tree(tmp_path)
    os.rename(root / "sub", root / "sub.real")
    os.symlink(tmp_path / "secrets", root / "sub")
    try:
        got = _read_text_nofollow(p, root)
    except ExportRefused:
        return
    assert got is None or "PRIVATE-KEY" not in got, "the swap leaked the target's bytes"


def test_a_final_component_link_is_still_refused(tmp_path):
    """The narrower case the single open already covered must not regress."""
    root, p = _tree(tmp_path)
    p.unlink()
    os.symlink(tmp_path / "secrets" / "prompt.md", p)
    try:
        got = _read_text_nofollow(p, root)
    except ExportRefused:
        return
    assert got is None or "PRIVATE-KEY" not in got


def test_a_path_outside_the_root_is_refused(tmp_path):
    root, _ = _tree(tmp_path)
    with pytest.raises(ExportRefused):
        _read_text_nofollow(tmp_path / "secrets" / "prompt.md", root)


def test_the_opener_matches_the_sidecar_copy():
    """The two copies exist because gateway code must not import the container tree.

    Compared as SOURCE TEXT, without importing ``crew/runtime/**``: that directory is
    Linux container image source, exempt from the spawn audit on the premise nothing
    imports it, and importing it here would break that premise.
    """
    here = pathlib.Path(__file__).resolve()
    crew = here.parents[2]
    sidecar = (crew / "runtime" / "container" / "backup" / "sidecar.py").read_text()
    ours = (crew / "packaging" / "build.py").read_text()

    # The load-bearing line: each parent opened relative to the previous descriptor
    # with O_NOFOLLOW set.
    line = "os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)"
    assert line in sidecar, "the sidecar's per-component open moved; re-pin this"
    assert line in ours, "build.py no longer opens each parent component the same way"

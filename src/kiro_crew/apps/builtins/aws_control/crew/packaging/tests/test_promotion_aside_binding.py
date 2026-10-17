"""The tree kept as the rollback copy must be the tree that was verified.

``out_dir`` is checked as one this build wrote roughly two hundred lines before it is renamed
aside, and ``rename`` acts on whatever the name IS at that instant. A tree swapped in between
was moved to ``<out>.previous`` unverified, the new bundle was promoted over the original path,
and the ownership check that would have objected ran afterwards -- when the operator's data was
already somewhere they did not put it. Measured before the binding.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from .test_producer import load_build, make_crew

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="swaps a directory for another tree mid-promotion"
)


def _build(mod, home, out):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    return mod.build_bundle(crew, spec, {}, None, out)


def test_a_tree_swapped_in_before_the_aside_rename_is_not_kept_as_the_rollback_copy(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    mod = load_build()
    home = make_crew(tmp_path / "home")
    out = tmp_path / "bundle"

    _build(mod, home, out)
    assert (out / "agent.json").exists(), "the first build must land for there to be a previous"

    operator = tmp_path / "operator-data"
    operator.mkdir()
    (operator / "their-notes.txt").write_text("IRREPLACEABLE\n", encoding="utf-8")

    real = mod._refuse_unless_this_build_wrote_it
    state = {"swapped": False}

    def _swap_then_check(d, flag, crew_name):
        # Swap AFTER --out has been cleared, which is the window the binding closes.
        real(d, flag, crew_name)
        if flag == "--out" and not state["swapped"]:
            state["swapped"] = True
            os.rename(out, tmp_path / "ours.real")
            os.rename(operator, out)

    monkeypatch.setattr(mod, "_refuse_unless_this_build_wrote_it", _swap_then_check)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out)

    assert state["swapped"], "the swap never happened, so this proves nothing"
    notes = out / "their-notes.txt"
    assert notes.exists(), f"the operator's tree was not returned to {out}: {caught.value}"
    assert notes.read_text(encoding="utf-8") == "IRREPLACEABLE\n"
    assert not (out / "agent.json").exists(), "a bundle was promoted over the operator's tree"

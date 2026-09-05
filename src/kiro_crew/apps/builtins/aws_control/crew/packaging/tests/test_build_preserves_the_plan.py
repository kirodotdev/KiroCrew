"""Building must not delete the signed plan it just read.

`build` stages the bundle and then replaces `--out` wholesale, which is what makes
a failed build leave nothing half-written. But `plan` writes its review template
into that same `--out`, so the documented flow -- plan, sign, build with the same
`--out` -- had the build delete the signed plan, silently. Reproduced end to end
before it was fixed: after the build, `curation-plan.json` was simply gone, and the
owner had to regenerate and re-sign with nothing telling them why.

Two rules keep the atomic swap without eating anything: the plan is carried through
staging so it lands back in the new directory, and a directory holding files the
build does not own is REFUSED by name rather than absorbed. The refusal matters
more than it looks: pointing `--out` at a directory of unrelated files is exactly
the case where a silent recursive delete does the most damage.
"""

from __future__ import annotations

import json

import pytest

from .test_producer import load_build, make_crew


def _signed_plan(mod, home, out):
    """Run the plan command, then sign what it wrote, as an owner would."""
    mod._cmd_plan("frontdesk", out, [], home)
    p = out / mod.PLAN_FILENAME
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["reviewed_by"] = "an owner"
    doc["reviewed_at"] = "2026-09-04"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_the_signed_plan_survives_the_build(tmp_path):
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "out"
    out.mkdir()
    plan = _signed_plan(mod, home, out)

    mod._cmd_build("frontdesk", out, [plan], home)

    assert plan.is_file(), "the build deleted the signed plan it had just read"
    doc = json.loads(plan.read_text(encoding="utf-8"))
    assert doc["reviewed_by"] == "an owner", "the plan survived but lost its signature"


def test_the_bundle_is_still_written(tmp_path):
    """Carrying the plan must not have broken what the build is for."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "out"
    out.mkdir()
    plan = _signed_plan(mod, home, out)

    mod._cmd_build("frontdesk", out, [plan], home)

    for entry in ("agent.json", "mcp.json", "manifest.json", "skills"):
        assert (out / entry).exists(), f"{entry} missing from the bundle"


def test_an_unrelated_file_is_refused_not_deleted(tmp_path):
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "out"
    out.mkdir()
    plan = _signed_plan(mod, home, out)
    stranger = out / "my-notes.txt"
    stranger.write_text("something the owner cares about", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as exc:
        mod._cmd_build("frontdesk", out, [plan], home)

    # Named, so the owner knows which file stopped the build.
    assert "my-notes.txt" in str(exc.value)
    assert stranger.is_file(), "the build deleted a file it had refused to delete"
    assert stranger.read_text(encoding="utf-8") == "something the owner cares about"


def test_rebuilding_over_a_previous_bundle_still_works(tmp_path):
    """A previous bundle IS owned, so a rebuild must not be refused."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "out"
    out.mkdir()
    plan = _signed_plan(mod, home, out)

    mod._cmd_build("frontdesk", out, [plan], home)
    mod._cmd_build("frontdesk", out, [plan], home)  # must not raise

    assert (out / "manifest.json").is_file()
    assert plan.is_file()


def test_MUTATION_the_plan_is_not_carried(tmp_path):
    """Drop the carry and the signed plan disappears again."""
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "out"
    out.mkdir()

    bad = load_build(
        mutate=(
            "            (staging / PLAN_FILENAME).write_bytes(carried_plan)",
            "            pass",
        )
    )
    plan = _signed_plan(bad, home, out)
    bad._cmd_build("frontdesk", out, [plan], home)

    assert not plan.exists(), "mutation did not take effect; this test proves nothing"

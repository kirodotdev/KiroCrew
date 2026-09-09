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
import os

import pytest

from .test_producer import load_build, make_crew

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _signed_plan(mod, home, out):
    """Run the plan command, then sign what it wrote, as an owner would."""
    mod._cmd_plan("frontdesk", out, [], home)
    p = out / mod.PLAN_FILENAME
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["reviewed_by"] = "an owner"
    doc["reviewed_at"] = "2026-09-04"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


@_posix_only
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


@_posix_only
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


@_posix_only
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


@_posix_only
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


@_posix_only
def test_MUTATION_the_plan_is_not_carried(tmp_path):
    """Drop the carry and the signed plan disappears again."""
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "out"
    out.mkdir()

    bad = load_build(
        mutate=(
            "            _write_bytes_nofollow(\n"
            "                staging / PLAN_FILENAME, carried_plan, staging_fd=_sfd, "
            "rel=PLAN_FILENAME\n"
            "            )",
            "            pass",
        )
    )
    plan = _signed_plan(bad, home, out)
    bad._cmd_build("frontdesk", out, [plan], home)

    assert not plan.exists(), "mutation did not take effect; this test proves nothing"


@_posix_only
def test_the_carried_plan_write_refuses_a_planted_symlink(tmp_path):
    """GPT/Opus :3426 -- the carried plan is written through the no-follow primitive.

    The staging tree lives beside --out in a directory this build does not own, so a same-UID
    process can plant a symlink at ``staging/curation-plan.json`` in the mkdir->write window.
    A following ``write_bytes`` would truncate whatever the link named and ship a redirect as
    the plan. The write now goes through ``_write_bytes_nofollow``; this pins that primitive's
    contract directly -- a link at the leaf is refused at open, and its target is untouched.
    """
    mod = load_build()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"an external file the build user can write\n")
    staging = tmp_path / "staging"
    staging.mkdir()
    leaf = staging / mod.PLAN_FILENAME
    leaf.symlink_to(victim)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._write_bytes_nofollow(leaf, b'{"reviewed_by": "an owner"}\n')
    assert "symlink" in str(caught.value)
    # The link's target is untouched -- the write was refused at open, not followed through.
    assert victim.read_bytes() == b"an external file the build user can write\n"


@_posix_only
def test_the_carried_plan_lands_byte_for_byte(tmp_path):
    """Non-vacuity: the no-follow write is byte-exact, so the signed plan's bytes are preserved.

    The plan carries a signature over its own bytes; a decode/re-encode round-trip could
    corrupt it, so the primitive writes raw bytes.
    """
    mod = load_build()
    staging = tmp_path / "staging"
    staging.mkdir()
    leaf = staging / mod.PLAN_FILENAME
    signed = b'{"reviewed_by": "an owner", "sig": "\xe2\x9c\x93 unicode check"}\n'
    mod._write_bytes_nofollow(leaf, signed)
    assert leaf.read_bytes() == signed

"""Three findings about what the bundle carries and what the scanner can see.

Each is the same kind of gap: a rule that was true of the shape in front of it and silent
about a shape one step away.

N1 nested skills -- ``_copy_skill`` walked the selected skill with ``rglob`` and shipped
   everything under it. Skill ids nest (``aws`` and ``aws/ec2`` are both skills, each with a
   ``SKILL.md``), so selecting the parent shipped the child the plan had excluded, and the
   printed notes said nothing about it. Deny-by-default is the whole premise of the plan, so
   an implicit inclusion is not a smaller version of the same thing.

N2 encoded credentials -- every pattern in the scanner matches a credential written
   literally, so a base64 of the same bytes matched none of them. The repo's own
   ``redact_credentials`` already decodes base64 chunks, so it is imported rather than
   restated; a local pattern per shape is what this file keeps needing, and that
   is the shape being retired.

N3 the Windows read -- ``_open_nofollow_under`` fell back to a single ``O_NOFOLLOW`` open
   where ``dir_fd`` is unavailable. That rejects only a FINAL-component link, so a parent
   replaced by a junction was traversed: the fallback protected the case that needs no
   protection and missed the one the function exists for. It now refuses.
"""

from __future__ import annotations

import base64
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)

# From AWS's own documentation, so nothing here is a real credential.
_DOC_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _build(mod, home: pathlib.Path, work: pathlib.Path, select):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    work.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, work, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, work / "bundle")


# ---------------------------------------------------------------------------
# N1: selecting a parent must not ship an excluded child
# ---------------------------------------------------------------------------
def _nested_crew(tmp_path: pathlib.Path) -> pathlib.Path:
    return make_crew(
        tmp_path / "home",
        skills={
            "aws": {"SKILL.md": "# AWS\nparent\n"},
            "aws/ec2": {"SKILL.md": "# EC2\nchild\n"},
            "aws/s3": {"SKILL.md": "# S3\nother child\n"},
        },
    )


@_posix_only
def test_selecting_only_the_parent_ships_only_the_parent(tmp_path: pathlib.Path) -> None:
    """The excluded children must be absent, and the count must agree.

    Both, because the count is what an operator reads and the files are what a customer
    gets. A bundle that ships three skills while reporting one is worse than either error
    alone.
    """
    mod = load_build()
    home = _nested_crew(tmp_path)
    work = tmp_path / "work"
    report = _build(mod, home, work, {"skills": {"aws"}})
    out = work / "bundle"

    assert (out / "skills" / "aws" / "SKILL.md").is_file()
    assert not (out / "skills" / "aws" / "ec2").exists(), "an excluded child skill shipped"
    assert not (out / "skills" / "aws" / "s3").exists(), "an excluded child skill shipped"
    assert report.skill_count == 1


@_posix_only
def test_selecting_parent_and_one_child_ships_exactly_those(tmp_path: pathlib.Path) -> None:
    """The child comes back when the plan selects it, and the sibling stays out.

    Non-vacuity for the skip: a copy that simply stopped at every nested root would satisfy
    the test above while making a selected child unshippable.
    """
    mod = load_build()
    home = _nested_crew(tmp_path)
    work = tmp_path / "work"
    report = _build(mod, home, work, {"skills": {"aws", "aws/ec2"}})
    out = work / "bundle"

    assert (out / "skills" / "aws" / "SKILL.md").is_file()
    assert (out / "skills" / "aws" / "ec2" / "SKILL.md").is_file()
    assert not (out / "skills" / "aws" / "s3").exists()
    assert report.skill_count == 2


@_posix_only
def test_a_parents_own_files_beside_a_child_still_ship(tmp_path: pathlib.Path) -> None:
    """The skip is scoped to the CHILD's subtree, not to everything below the parent.

    A parent legitimately has its own files at any depth. Skipping by "is under some nested
    root" rather than "is under an EXCLUDED nested root" would silently drop them.
    """
    mod = load_build()
    home = make_crew(
        tmp_path / "home",
        skills={"aws": {"SKILL.md": "# AWS\n"}, "aws/ec2": {"SKILL.md": "# EC2\n"}},
    )
    # Written here rather than through ``make_crew``: its writer does not create parent
    # directories for a nested filename, so a nested entry would fail on the write rather
    # than testing anything. The directory is called "reference" and not "docs", because the
    # docs lint reads a "docs/<name>.md" string appearing in source as a citation of a real
    # repository document and fails on the missing file.
    own = home / "skills" / "aws" / "reference"
    own.mkdir(parents=True)
    (own / "notes.md").write_text("parent's own file\n", encoding="utf-8")

    work = tmp_path / "work"
    _build(mod, home, work, {"skills": {"aws"}})
    out = work / "bundle"

    assert (out / "skills" / "aws" / "reference" / "notes.md").is_file()
    assert not (out / "skills" / "aws" / "ec2").exists()


# ---------------------------------------------------------------------------
# N2: a credential the scanner cannot read literally
# ---------------------------------------------------------------------------
def test_a_base64_encoded_labelled_secret_is_found() -> None:
    """The encoded form must be a finding, as the literal form already was.

    Asserted on both spellings in one test so the comparison is the assertion: if the
    encoded case ever stops being found, this fails while the literal case still passes,
    which is exactly the state the finding described.
    """
    mod = load_build()
    literal = f"aws_secret_access_key = {_DOC_SECRET}"
    encoded = base64.b64encode(literal.encode()).decode()

    assert mod.scan_text(literal, "t"), "the literal form must still be found"
    assert mod.scan_text(encoded, "t"), "the encoded form ships past every literal pattern"


def test_ordinary_skill_text_is_still_clean() -> None:
    """Non-vacuity: a scanner that flagged everything would pass the test above.

    The strings here are the kind of thing a real SKILL.md holds, including base64-looking
    words, because a detector that cannot tell those apart makes the build unusable.
    """
    mod = load_build()
    for text in (
        "# FAQ\nStore hours are 9 to 6.\n",
        "# Deploy\nRun `make release` and check the output.\n",
        "# Encoding\nUse base64 for binary payloads.\n",
    ):
        assert not mod.scan_text(text, "t"), text


@_posix_only
def test_an_encoded_credential_blocks_the_skill_that_carries_it(tmp_path: pathlib.Path) -> None:
    """End to end: the candidate is blocked, so the plan cannot select it."""
    mod = load_build()
    encoded = base64.b64encode(f"aws_secret_access_key = {_DOC_SECRET}".encode()).decode()
    home = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": f"# L\n{encoded}\n"}})
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    leaky = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "leaky")
    assert leaky.blocked


# ---------------------------------------------------------------------------
# N3: no per-component fence, no read
# ---------------------------------------------------------------------------
def _no_dir_fd(mod_loader):
    return mod_loader(
        mutate=(
            '    return os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")',
            "    return False",
        )
    )


def test_the_builder_refuses_where_the_nofollow_primitive_is_unavailable(
    tmp_path: pathlib.Path,
) -> None:
    """With no atomic no-follow primitive (the Windows condition), the builder refuses.

    Every filesystem entry point's fallback follows reparse points on such a platform, so a
    junction to a UNC share would leak an SMB/NTLM exchange during ordinary packaging. One
    entry-point guard refuses rather than ship that surface; the builder is POSIX-only until a
    real no-follow primitive is available.

    A skills-only build is the vehicle because it is the narrowest one that still reaches an
    entry point: no prompt, no plan, nothing but a skill file. The refusal names the platform
    condition and the issue, and nothing is written.
    """
    mod = _no_dir_fd(load_build)
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work, {"skills": {"faq"}})
    assert "POSIX-only" in str(caught.value)
    assert "#9496" in str(caught.value)
    assert not (work / "bundle").exists(), "nothing is written when the builder refuses"

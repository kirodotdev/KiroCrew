"""Entry shapes at derived paths, and preflight ordering.

T1 the report refusal ran AFTER build_bundle -- so it refused a foreign report only once
   staging had been built, the previous bundle moved aside, staging renamed into place and the
   aside copy deleted. A preflight that runs after the thing it guards is a message.

T2 the aside path had no redirect check -- ``staging`` and ``out_dir`` got one an earlier round
   and ``<out>.previous`` did not, so a redirect there aimed the ownership check AND the
   rmtree below it at somewhere else, and the check passed because it examined the target.

T3 the report check's own preflight used ``is_file()`` first, which follows a link -- and on
   Windows follows a reparse point naming a share, which is the outbound SMB probe, from a
   path derived from --out.

T4 the agent spec was read with no sensitive-path check, while the prompt reference beside it
   had one. The spec's bytes SHIP, as ``agent.json``, so the read reaches the customer just as
   directly as an inlined prompt.

A fifth finding is REJECTED with evidence; see
``test_the_carried_plan_is_deliberately_outside_the_digest``.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


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
# T1
# ---------------------------------------------------------------------------
@_posix_only
def test_a_foreign_report_is_refused_before_the_bundle_is_touched(tmp_path: pathlib.Path) -> None:
    """The bundle directory must be UNCHANGED when the refusal fires.

    That is the whole finding: the check existed and ran too late, so asserting the refusal
    alone would have passed before the fix. What distinguishes the two is the state of the
    output directory afterwards.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    work.mkdir()
    _build(mod, home, work, {"skills": {"faq"}})
    before = sorted(p.name for p in (work / "bundle").iterdir())
    digest_before = json.loads((work / "bundle" / "manifest.json").read_text(encoding="utf-8"))[
        "digest"
    ]

    foreign = work / "bundle.smc-bundle.json"
    foreign.write_text('{"something": "the operator wrote this"}', encoding="utf-8")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, tmp_path / "w2", select={"skills": {"faq"}})
    code = mod.main(
        [
            "build",
            "--crew",
            "frontdesk",
            "--source",
            str(home),
            "--allow",
            str(plan_path),
            "--out",
            str(work / "bundle"),
        ]
    )

    assert code != 0, "the build did not refuse"
    assert "operator wrote this" in foreign.read_text(encoding="utf-8")
    assert sorted(p.name for p in (work / "bundle").iterdir()) == before
    assert (
        json.loads((work / "bundle" / "manifest.json").read_text(encoding="utf-8"))["digest"]
        == digest_before
    ), "the bundle was rebuilt before the refusal"
    assert not (work / "bundle.previous").exists(), "the previous bundle was moved aside"
    assert not (work / "bundle.staging").exists(), "staging was left behind"


# ---------------------------------------------------------------------------
# T2
# ---------------------------------------------------------------------------
@_posix_only
def test_a_redirect_at_the_aside_path_is_refused(tmp_path: pathlib.Path) -> None:
    """The rmtree that follows would run inside the link's target."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    work.mkdir()
    _build(mod, home, work, {"skills": {"faq"}})

    elsewhere = tmp_path / "operators-dir"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_bytes(b"do not delete me\n")
    (work / "bundle.previous").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work, {"skills": {"faq"}})
    assert "aside path" in str(caught.value)
    assert "link or junction" in str(caught.value)
    assert (elsewhere / "keep.txt").is_file(), "the target's contents were deleted"


@_posix_only
def test_an_ordinary_rebuild_still_uses_the_aside_path(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the refusal must not break the promotion it guards."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    for _ in range(3):
        report = _build(mod, home, work, {"skills": {"faq"}})
    assert report.digest.startswith("sha256:")
    assert not (work / "bundle.previous").exists(), "the aside copy was left behind"


# ---------------------------------------------------------------------------
# T3
# ---------------------------------------------------------------------------
def test_the_report_preflight_judges_the_entry_before_reading_it(tmp_path: pathlib.Path) -> None:
    """A link at the report path must be decided WITHOUT reading through it.

    Asserting "does not raise" cannot tell the two orderings apart: with ``is_file()`` first
    the link is followed, the target reads as a valid report, and the check also returns
    quietly. So the target is made a NON-report -- then the two orderings disagree. Judging
    the entry returns (the shape decision belongs to ``_write_nofollow``); following the link
    reads the target, finds no ``report_version``, and refuses with the wrong reason about the
    wrong file.

    The ordering matters beyond the message: on Windows, following a reparse point that names
    a share is the outbound SMB probe, from a path derived from --out.
    """
    mod = load_build()
    target = tmp_path / "target.json"
    target.write_text('{"not": "a report at all"}', encoding="utf-8")
    link = tmp_path / "bundle.smc-bundle.json"
    link.symlink_to(target)

    mod._refuse_unless_our_report(link, tmp_path / "bundle")  # a link: the writer judges it

    with pytest.raises(mod.ExportRefused) as caught:
        mod._write_nofollow(link, "{}\n")
    assert "symlink" in str(caught.value)
    assert target.read_text(encoding="utf-8") == '{"not": "a report at all"}'


# ---------------------------------------------------------------------------
# T4
# ---------------------------------------------------------------------------
@_posix_only
def test_a_symlinked_agent_spec_is_refused(tmp_path: pathlib.Path) -> None:
    """The spec's bytes ship, so the read must not follow a redirect."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"prompt": "borrowed"}), encoding="utf-8")
    spec_path = home / "agents" / "frontdesk.json"
    spec_path.unlink()
    spec_path.symlink_to(real)

    crew = mod.resolve_crew("frontdesk", home)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_agent_spec(crew)
    assert "link or junction" in str(caught.value)


def test_a_sensitive_agent_spec_path_is_refused(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The same fence the prompt reference gets, asked of the spec path too."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    crew = mod.resolve_crew("frontdesk", home)
    # The fence is asked about this build's own path, so the test makes the fence say yes
    # rather than moving the crew into a real credential directory.
    import kiro_crew.security as sec

    monkeypatch.setattr(sec, "is_sensitive_path", lambda posix: "agents" in posix)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_agent_spec(crew)
    msg = str(caught.value)
    if os.name == "posix":
        assert "sensitive" in msg
    else:
        assert "POSIX-only" in msg


@_posix_only
def test_an_ordinary_agent_spec_still_reads(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the fence must not refuse the ordinary crew directory."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    crew = mod.resolve_crew("frontdesk", home)
    assert isinstance(mod.read_agent_spec(crew), dict)


# ---------------------------------------------------------------------------
# The rejected finding
# ---------------------------------------------------------------------------
@_posix_only
def test_the_carried_plan_is_deliberately_outside_the_digest(tmp_path: pathlib.Path) -> None:
    """The review asked for the plan to be hashed. That is rejected, and here is why.

    The plan is the OPERATOR's file: ``_cmd_plan`` writes it into --out, they edit and sign it,
    and the next build carries it forward. So it is EXPECTED to differ between builds, which is
    what this test does. Hashing it makes every such edit break the rebuild preflight --
    measured: implementing the requested change reddened this flow and one more test.

    It also protects nothing. The container consumes four entries -- manifest.json, agent.json,
    mcp.json and skills/ -- and the whole container tree contains no reference to the plan
    filename, so a swapped plan changes no deployed behaviour. What ships was decided at build
    time and IS covered by the digest.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    _build(mod, home, work, {"skills": {"faq"}})

    plan_in_bundle = work / "bundle" / mod.PLAN_FILENAME
    plan_in_bundle.write_text(json.dumps({"edited": True}) + "\n", encoding="utf-8")

    _build(mod, home, work, {"skills": {"faq"}})  # must not raise
    assert json.loads(plan_in_bundle.read_text(encoding="utf-8")) == {"edited": True}


# ---------------------------------------------------------------------------
# GPT 5.6: a staged bundle leaf is written through the no-follow primitive, so a
# symlink planted at the leaf in the mkdir->write window is refused, not followed
# and its target truncated. The staging tree lives beside --out in a directory the
# build does not own, which is the module's stated adversarial-writer threat model.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    os.name != "posix",
    reason="needs O_NOFOLLOW symlink semantics; the builder is POSIX-only per the primitive guard",
)
def test_a_staged_leaf_write_refuses_a_planted_symlink_and_spares_its_target(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious operator file\n", encoding="utf-8")
    leaf = tmp_path / "staging" / "SKILL.md"
    leaf.parent.mkdir(parents=True)
    leaf.symlink_to(victim)  # a link planted where a staged leaf will be written
    with pytest.raises(mod.ExportRefused) as caught:
        mod._write_guarded(leaf, "# fresh skill body\n", "skills/faq/SKILL.md")
    assert "symlink" in str(caught.value)
    # The link's target is untouched: the write was refused, not followed through.
    assert victim.read_text(encoding="utf-8") == "precious operator file\n"

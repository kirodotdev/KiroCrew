"""Two concurrent builds, and what the staging marker may claim.

U1 the agent spec's redirect check judged only the FINAL component, so a junction at
   ``<source>/agents`` was traversed by the ``is_file()`` below it. Same mistake the prompt
   fence made in its first version, and the same function fixes it -- which is the point:
   there was already a whole-chain walker, and this call site used the single-entry predicate.

U2 the staging marker said "a kiro-crew build made this" and nothing more, so two concurrent
   builds against one --out each read the OTHER's marker as their own and deleted the other's
   staging tree with the recursive delete the marker authorises.
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
# U1
# ---------------------------------------------------------------------------
@_posix_only
def test_a_junction_at_the_agents_directory_is_refused(tmp_path: pathlib.Path) -> None:
    """The PARENT, not the spec file. A final-component check cannot see this."""
    mod = load_build()
    real = make_crew(tmp_path / "real", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    home = tmp_path / "home"
    home.mkdir()
    (home / "agents").symlink_to(real / "agents", target_is_directory=True)
    (home / "skills").mkdir()

    crew = mod.resolve_crew("frontdesk", home)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_agent_spec(crew)
    assert "link or junction" in str(caught.value)
    assert "agent spec" in str(caught.value), "the message still says prompt file"


def test_a_link_at_the_spec_itself_is_still_refused(tmp_path: pathlib.Path) -> None:
    """The narrower case must keep working; the walk covers both."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"prompt": "borrowed"}), encoding="utf-8")
    spec_path = home / "agents" / "frontdesk.json"
    spec_path.unlink()
    spec_path.symlink_to(real)

    crew = mod.resolve_crew("frontdesk", home)
    with pytest.raises(mod.ExportRefused):
        mod.read_agent_spec(crew)


@_posix_only
def test_an_ordinary_crew_directory_still_reads(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the walk must not refuse a plain crew home."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    crew = mod.resolve_crew("frontdesk", home)
    assert isinstance(mod.read_agent_spec(crew), dict)
    _build(mod, home, tmp_path / "work", {"skills": {"faq"}})


# ---------------------------------------------------------------------------
# U2
# ---------------------------------------------------------------------------
def test_another_runs_marker_does_not_authorise_the_delete(tmp_path: pathlib.Path) -> None:
    """A marker with this builder's token but a different run id is NOT ours.

    Written by hand rather than by racing two real builds: what the fix changes is which
    markers authorise the recursive delete, and a hand-written marker states that directly
    where a race would only sometimes reproduce it.
    """
    mod = load_build()
    marker = tmp_path / "bundle.staging.owned"
    marker.write_text(
        mod._STAGING_MARKER_TOKEN + "\n" + "99999-cafebabecafebabe" + "\n",
        encoding="utf-8",
    )
    assert not mod._marker_is_ours(marker), "another run's marker authorised the delete"


def test_this_runs_marker_is_recognised(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the run that wrote it must still be able to clean up after itself."""
    mod = load_build()
    marker = tmp_path / "bundle.staging.owned"
    mod._write_nofollow(marker, mod._STAGING_MARKER_BODY, exclusive=True)
    assert mod._marker_is_ours(marker)


def test_a_token_only_marker_is_not_ours(tmp_path: pathlib.Path) -> None:
    """The old marker shape -- token and no run id -- must not pass either.

    That is the concurrency window as it stood: every build wrote this and every build
    accepted it.
    """
    mod = load_build()
    marker = tmp_path / "bundle.staging.owned"
    marker.write_text(mod._STAGING_MARKER_TOKEN + "\n", encoding="utf-8")
    assert not mod._marker_is_ours(marker)


@_posix_only
def test_a_concurrent_build_refuses_instead_of_deleting(tmp_path: pathlib.Path) -> None:
    """End to end: a staging tree with another run's marker beside it is refused.

    The tree is left in place, which is the property that matters -- the finding was about a
    build deleting a tree another build was still writing into.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    work.mkdir()
    staging = work / "bundle.staging"
    (staging / "skills").mkdir(parents=True)
    (staging / "skills" / "in-flight.md").write_bytes(b"another build is writing this\n")
    (work / "bundle.staging.owned").write_text(
        mod._STAGING_MARKER_TOKEN + "\n" + "99999-cafebabecafebabe" + "\n",
        encoding="utf-8",
    )

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work, {"skills": {"faq"}})
    assert "did not create it" in str(caught.value)
    assert (staging / "skills" / "in-flight.md").is_file(), "the other build's tree was deleted"


def test_the_run_id_carries_more_than_the_pid() -> None:
    """A pid alone repeats, so the id must have a component a pid cannot supply.

    Checked in-process by shape rather than by spawning two builders: a second process would
    prove the ids differ, and it would also need a fourth entry in the spawn audit's benign
    list to justify a subprocess in a test. The property that makes two runs distinguishable is
    that the id is not a function of the pid, and that is visible here.
    """
    mod = load_build()
    run_id = mod._RUN_ID
    pid_part, _, random_part = run_id.partition("-")
    assert pid_part == str(os.getpid()), run_id
    assert len(random_part) >= 16, f"no random component to tell two runs apart: {run_id}"
    assert random_part != pid_part


def test_the_marker_body_contains_the_run_id() -> None:
    """The reader compares the second line, so the writer has to put it there."""
    mod = load_build()
    lines = mod._STAGING_MARKER_BODY.splitlines()
    assert lines[0] == mod._STAGING_MARKER_TOKEN
    assert lines[1] == mod._RUN_ID

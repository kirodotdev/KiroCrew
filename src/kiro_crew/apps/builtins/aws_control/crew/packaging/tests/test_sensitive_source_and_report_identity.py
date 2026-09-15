"""A sensitive --source, and what identifies a report as ours.

Each was real, and the first was mine: the comment above it argued the fail-open was a
deliberate accommodation for standalone mode. That argument holds for refusing outright and
does not hold for skipping the check, which is what the code did -- so standalone was the one
mode where a sensitive ``--source`` was read and bundled.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import pathlib
import shutil

import pytest

from .test_producer import load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _build(mod, home: pathlib.Path, out: pathlib.Path, select):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, out.parent, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, out)


def test_a_sensitive_source_is_refused_even_without_the_shared_validator() -> None:
    """The standalone fence answers the question the shared one cannot be asked.

    Drives the predicate directly for the paths, and the fence's placement is pinned by the
    build-level test below -- a predicate that is never consulted passes this and does
    nothing.
    """
    mod = load_build()
    for sensitive in (
        "/home/someone/.aws/credentials",
        "/home/someone/.ssh/id_rsa",
        "/home/someone/.config/gcloud/application_default_credentials.json",
        "/home/someone/.kube/config",
        "/home/someone/.kiro/crew-auth-staging/thing.json",
    ):
        assert mod._looks_sensitive_standalone(sensitive), sensitive


def test_the_standalone_fence_matches_components_not_substrings() -> None:
    """``~/projects/sshconfig-notes`` is not ``~/.ssh``.

    A substring test would refuse an operator's ordinary directory, and a fence that fires on
    innocent paths gets deleted rather than fixed.
    """
    mod = load_build()
    for innocent in (
        "/home/someone/projects/sshconfig-notes/agents/a.json",
        "/home/someone/awsnotes/agents/a.json",
        "/home/someone/my.ssh.backup.txt",
        "/home/someone/gnupg-docs/agents/a.json",
    ):
        assert not mod._looks_sensitive_standalone(innocent), innocent


def test_the_two_part_entries_need_consecutive_components() -> None:
    """``.config/gcloud`` is two components in order, not two names anywhere."""
    mod = load_build()
    assert mod._looks_sensitive_standalone("/home/x/.config/gcloud/creds.json")
    assert not mod._looks_sensitive_standalone("/home/x/.config/other/gcloud-notes/a.json")


def test_the_build_consults_the_standalone_fence_on_the_spec_path(
    tmp_path: pathlib.Path,
) -> None:
    """Driven through the real build, so the guard's PLACEMENT is what is tested.

    The lesson this pins: a fence proven only by calling its predicate says nothing about
    whether the read path reaches it. No hook is needed to simulate standalone mode, because
    the local fence now runs unconditionally -- which is the fix. Under the old code this
    same crew was read and bundled whenever the shared validator was unimportable.
    """
    mod = load_build()
    home = make_crew(tmp_path / ".aws" / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, tmp_path / "out", {"skills": {"faq"}})
    msg = str(caught.value)
    if os.name == "posix":
        assert "sensitive" in msg
    else:
        assert "POSIX-only" in msg


def test_a_foreign_json_carrying_the_version_key_is_still_refused(
    tmp_path: pathlib.Path,
) -> None:
    """``report_version`` alone authorised truncating unrelated data.

    It is a generic key. Any document that happens to carry ``"report_version": 1`` read as
    this tool's own output, and the build then replaced it.
    """
    mod = load_build()
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    foreign = out.parent / f"{out.name}.smc-bundle.json"
    foreign.write_text(
        json.dumps({"report_version": mod.REPORT_VERSION, "notes": "someone else's file"}),
        encoding="utf-8",
    )

    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unless_our_report(foreign, out)
    assert "did not write it" in str(caught.value)
    assert json.loads(foreign.read_text(encoding="utf-8"))["notes"] == "someone else's file"


def test_our_own_report_naming_this_bundle_is_accepted(tmp_path: pathlib.Path) -> None:
    """The other half: a rebuild over this tool's own report is the ordinary case.

    Without this the fix would read as "refuse everything", which no test above would catch.
    """
    mod = load_build()
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    ours = out.parent / f"{out.name}.smc-bundle.json"
    ours.write_text(
        json.dumps({"report_version": mod.REPORT_VERSION, "bundle_dir": str(out)}),
        encoding="utf-8",
    )
    mod._refuse_unless_our_report(ours, out)


def test_a_report_naming_a_different_bundle_is_refused(tmp_path: pathlib.Path) -> None:
    """Same version, different destination: not the report this build would replace."""
    mod = load_build()
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    stale = out.parent / f"{out.name}.smc-bundle.json"
    stale.write_text(
        json.dumps(
            {"report_version": mod.REPORT_VERSION, "bundle_dir": str(tmp_path / "elsewhere")}
        ),
        encoding="utf-8",
    )
    with pytest.raises(mod.ExportRefused):
        mod._refuse_unless_our_report(stale, out)


@_posix_only
def test_a_failed_promotion_leaves_no_report_behind(tmp_path: pathlib.Path, monkeypatch) -> None:
    """A rename failure rolls the report back with the bundle.

    The report is written before the swap on purpose, so a report failure cannot land after
    the previous bundle is gone. That ordering left the other hole: the swap failed, the
    previous bundle came back, and the report still described the bundle that never landed.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"

    real_rename = os.rename

    def _fail_the_promotion(src, dst, *args, **kwargs):
        if str(src).endswith(".staging"):
            raise OSError(13, "promotion refused")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _fail_the_promotion)
    with pytest.raises(OSError):
        _build(mod, home, out, {"skills": {"faq"}})

    report = out.parent / f"{out.name}.smc-bundle.json"
    assert not report.exists(), "the report describes a bundle that never landed"
    assert not out.exists(), "no bundle was installed"


@_posix_only
def test_a_failed_promotion_restores_a_previous_report_verbatim(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The rollback puts the earlier bytes back rather than deleting them.

    Distinguishes the two branches: deleting unconditionally would pass the test above and
    destroy the previous build's report here.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    report = out.parent / f"{out.name}.smc-bundle.json"
    first = report.read_bytes()
    assert json.loads(first.decode("utf-8"))["bundle_dir"] == str(out)

    real_rename = os.rename

    def _fail_the_promotion(src, dst, *args, **kwargs):
        if str(src).endswith(".staging"):
            raise OSError(13, "promotion refused")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _fail_the_promotion)
    with pytest.raises(OSError):
        _build(mod, home, out, {"skills": {"faq"}})

    assert report.read_bytes() == first, "the previous build's report was not restored"


# ---------------------------------------------------------------------------
# Round-12 GPT F1: a short encoded credential must not slip under the b64 floor
#
# The standalone decoder is the packager's REAL scan path (the canonical redactor
# is not importable in the deployment venv), and a credential shorter than an AWS
# secret access key still base64-encodes to a run under 40 chars.
# ---------------------------------------------------------------------------
def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def test_a_short_encoded_credential_is_caught_by_the_decoder() -> None:
    """A ``sk-`` vendor key encodes to a ~32-char base64 run, well under the old 40 floor.

    Drives ``_scan_decoded_runs`` directly: that is the standalone-mode scan path the
    finding is about (in a deployment venv the canonical redactor is not importable, so
    this decoder is the real scan), and it is the unit the floor governs.
    """
    mod = load_build()
    secret = "sk-" + "A" * 22  # matches _HARD_PATTERNS vendor-key (sk-[A-Za-z0-9]{20,})
    run = _b64(secret)
    assert 20 <= len(run) < 40, f"run must sit in the newly-covered band, got {len(run)}"
    leaks = mod._scan_decoded_runs(f"note: {run}", "spec.json")
    assert any("encoded-vendor-key" in leak.kind for leak in leaks), [leak.kind for leak in leaks]


def test_MUTATION_the_old_40_char_floor_would_skip_the_short_run() -> None:
    """Restore the 40-char floor and the same short run goes unscanned by the decoder."""
    mod = load_build(mutate=("[A-Za-z0-9+/]{20,}={0,2}", "[A-Za-z0-9+/]{40,}={0,2}"))
    secret = "sk-" + "A" * 22
    run = _b64(secret)
    leaks = mod._scan_decoded_runs(f"note: {run}", "spec.json")
    assert not any("encoded-vendor-key" in leak.kind for leak in leaks), (
        "floor reverted to 40: the short encoded credential should slip through, "
        "proving the lowered floor is what catches it"
    )


# ---------------------------------------------------------------------------
# Round-12 GPT F2: a credential-store filename must be refused by name
#
# A ``.git-credentials`` file carries a generic ``user:password@host`` that the
# content patterns do not reliably match, so the name gate is the real defense.
# ---------------------------------------------------------------------------
def test_a_git_credentials_file_is_refused_by_name() -> None:
    """The name alone refuses it, before any content read."""
    mod = load_build()
    assert mod.refused_by_name(pathlib.Path(".git-credentials"))
    assert mod.refused_by_name(pathlib.Path(".pypirc"))


def test_MUTATION_git_credentials_would_pass_the_name_gate_without_the_entry() -> None:
    """Drop the ``.git-credentials`` entry and the name gate lets it through."""
    mod = load_build(mutate=("      | \\.git-credentials\n", ""))
    assert not mod.refused_by_name(pathlib.Path(".git-credentials")), (
        "entry removed: the name gate should no longer refuse it, proving the entry "
        "is what closes the gap"
    )


# ---------------------------------------------------------------------------
# Round-12 GPT F3: the agent-spec read must not follow a replacement symlink
#
# ``is_file()`` then ``_read_text`` was a check/read window a concurrent writer
# could win by swapping the spec for a symlink between the two. The read is now a
# single ``O_NOFOLLOW`` open, so the link is refused at open time with no window.
# The chain-walk guard also refuses a pre-planted link, so the nofollow read is
# tested at its own unit -- that is the part that closes the RACE the chain guard
# cannot, since a swap after the walk still lands on this open.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_the_nofollow_reader_refuses_a_symlink(tmp_path: pathlib.Path) -> None:
    """A link at the read path returns None (refused) rather than its target's bytes."""
    mod = load_build()
    real = tmp_path / "real.json"
    real.write_text("secret from elsewhere\n", encoding="utf-8")
    link = tmp_path / "spec.json"
    os.symlink(real, link)

    assert mod._read_text_nofollow(real) == "secret from elsewhere\n", "a real file still reads"
    # Refused by RAISING, not by returning None. ``None`` is this reader's signal for content
    # that is not UTF-8 -- an answer about encoding -- and a link is not an encoding problem:
    # it is a path that changed into something that was never reviewed, which the caller must
    # not be able to treat as "no text here" and carry on.
    # None, not a raise: the reader reports "cannot read this" and each caller words its
    # own refusal. What matters here is that the swapped link is NOT read through.
    assert mod._read_text_nofollow(link) is None


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_MUTATION_a_following_reader_would_read_through_the_link(tmp_path: pathlib.Path) -> None:
    """Give the nofollow reader an ordinary following open and the link is read through."""
    mod = load_build(
        mutate=(
            # The open sits in a conditional, because the reader takes an optional anchor
            # root. The mutated property is unchanged: without the no-follow flags an
            # ordinary open reads the link through.
            # One open, no conditional: the anchored variant and its opener stack were
            # removed as production-dead. The property is unchanged -- without the
            # no-follow flags an ordinary open reads the link through.
            "fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW_READ_FLAGS)",
            "fd = os.open(str(path), os.O_RDONLY)",
        )
    )
    real = tmp_path / "real.json"
    real.write_text("secret from elsewhere\n", encoding="utf-8")
    link = tmp_path / "spec.json"
    os.symlink(real, link)

    assert mod._read_text_nofollow(link) == "secret from elsewhere\n", (
        "O_NOFOLLOW removed: the reader follows the link to its target, proving the "
        "flag is what refuses it"
    )


def test_the_local_fence_is_never_stricter_than_the_shared_one() -> None:
    """Every entry in the local list must be one the shared validator also refuses.

    The local list exists for the mode where the shared validator is unimportable, so it may
    be COARSER -- catch less -- but never stricter. A stricter entry refuses a path the rest
    of the tree considers ordinary, and one did: ``.kiro/agents`` is upstream's
    ``_WRITE_PROTECTED_HOME_PATHS``, protecting against WRITING a spec whose
    ``mcpServers.command`` the gateway execs. ``is_sensitive_path`` returns False for it, and
    ``~/.kiro`` is the DEFAULT source, so every run without ``--source`` refused its own crew.

    No local test caught that, because every test builds its crew under ``tmp_path`` and none
    exercises the default path. This test compares the two lists instead of the behaviour.
    """
    from kiro_crew.security.paths import is_sensitive_path

    mod = load_build()
    home = str(pathlib.Path.home())
    stricter = []
    for entry in mod._SENSITIVE_RELATIVE_DIRS:
        probe = f"{home}/{entry}"
        if "." not in pathlib.PurePosixPath(entry).name:
            probe += "/probe"
        if not is_sensitive_path(probe):
            stricter.append(entry)
    assert not stricter, (
        f"these local entries are refused here but not by the shared validator: {stricter}. "
        f"A read-only build must not invent a read fence the rest of the tree does not have."
    )


def test_the_default_agent_spec_path_is_not_refused() -> None:
    """The regression stated directly: the default source must remain usable.

    Named separately from the list comparison because this is the SYMPTOM an operator hits,
    and it should be the failure a future reader sees first.
    """
    mod = load_build()
    home = str(pathlib.Path.home())
    assert not mod._looks_sensitive_standalone(f"{home}/.kiro/agents/frontdesk.json")
    assert not mod._looks_sensitive_standalone(f"{home}/.kiro/crew/skills/faq/SKILL.md")


def test_a_plan_written_under_a_file_refuses_instead_of_crashing(
    tmp_path: pathlib.Path,
) -> None:
    """``mkdir(parents=True)`` under an existing FILE raises a bare OSError.

    Every other refusal in this CLI is an ``ExportRefused`` naming the flag at fault, so a
    traceback here sends the operator to read a stack instead of moving --out.
    """
    mod = load_build()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("I am a file\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unusable_parent(blocker / "sub" / "plan.json", what="the plan")
    message = str(caught.value)
    assert "is not a directory" in message
    assert "--out" in message, "the refusal must name the flag the operator can change"


def test_an_ordinary_missing_directory_is_still_created(tmp_path: pathlib.Path) -> None:
    """The guard must not refuse the ordinary case: --out naming a directory not yet there.

    Without this, a guard that refused whenever the parent was absent would pass the test
    above and break every first build.
    """
    mod = load_build()
    mod._refuse_unusable_parent(tmp_path / "fresh" / "deeper" / "plan.json", what="the plan")


def test_the_output_parent_is_judged_before_any_derived_path(tmp_path: pathlib.Path) -> None:
    """One check on the shared component, not three on the paths derived from it.

    The staging tree, its marker and the report are all ``out_dir.parent / <something>``, so
    a junction at that parent relocates all three together and each per-path check then
    validates a name that already points elsewhere.
    """
    mod = load_build()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unusable_parent(blocker / "bundle", what="the bundle")
    assert "is not a directory" in str(caught.value)


@_posix_only
def test_build_bundle_calls_the_parent_guard_first() -> None:
    """A source rule: the call must precede the first derived name.

    A guard placed after ``staging = out_dir.parent / ...`` would pass a direct test of the
    guard while the derived paths were already built from an unvalidated parent.
    """
    src = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    body = src[src.index("def build_bundle(") :]
    guard = body.index('_refuse_unusable_parent(out_dir, what="the bundle")')
    first_derived = body.index('staging = out_dir.parent / (out_dir.name + ".staging")')
    assert guard < first_derived, "the parent is validated after a path is derived from it"


@_posix_only
def test_the_report_is_published_atomically_by_exclusive_link(tmp_path: pathlib.Path) -> None:
    """A source rule for the publish shape, since a partial write cannot be staged in a test.

    ``_write_nofollow`` opens with ``O_TRUNC``, so an in-place write that fails partway has
    already emptied the previous report while ``report_written`` is still False -- the one
    shape the rollback cannot see. Writing a temp and installing it by an atomic exclusive
    hard link means the destination holds either the old bytes or the complete new ones, and
    a file that raced into the path is refused (``FileExistsError``) rather than clobbered.
    """
    src = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    assert (
        "os.link(tmp_name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)" in src
    ), "the report publish is not an atomic exclusive hard link"
    assert (
        "os.replace(report_tmp, report_path.name, dst_dir_fd=parent_fd)" not in src
    ), "the report publish still overwrites by-name instead of failing on a collision"
    assert (
        "_unlink_out_leaf_best_effort(report_tmp, resolved_out_parent)" in src
    ), "the temp is not cleaned up (descriptor-relative, so a swapped parent cannot steer it)"


def test_the_atomic_publish_still_refuses_a_planted_link() -> None:
    """Atomicity must not cost the no-follow refusal, and it nearly did.

    The exclusive-link publish does not follow a symlink at the report path, but a shape
    check stated explicitly before the publish is what names WHY a planted link is refused --
    so the destination's shape is judged before the report is published.
    """
    src = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    publish_at = src.index("_publish_report(report_tmp, report_path")
    shape_at = src.index("_is_redirecting_entry(report_path)")
    assert 0 <= shape_at < publish_at, (
        "the destination's shape is not judged before the report is published, so a planted "
        "link at the report path is overwritten instead of refused"
    )


# ---------------------------------------------------------------------------
# Round-13 GPT F1: a nested directory reached through a link/junction must block
# the skill -- rglob descends into it and is_symlink() misses a junction.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_a_skill_reaching_outside_through_a_linked_dir_is_blocked(tmp_path: pathlib.Path) -> None:
    """A skill whose subdirectory is a symlink to an out-of-source tree is blocked, not shipped."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.txt").write_text("secret from elsewhere\n", encoding="utf-8")

    home = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = home / "skills" / "leaky"
    os.symlink(outside, skill_dir / "nested")

    mod = load_build()
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    leaky = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "leaky")
    assert leaky.blocked, "a skill reaching outside the source through a link must be blocked"
    assert "link or junction" in leaky.blocked


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_MUTATION_a_linked_dir_would_not_block_without_the_redirect_check(
    tmp_path: pathlib.Path,
) -> None:
    """With the redirect check dropped, the skill with a linked-out subdir passes unblocked."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.txt").write_text("secret from elsewhere\n", encoding="utf-8")

    home = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = home / "skills" / "leaky"
    os.symlink(outside, skill_dir / "nested")

    mod = load_build(
        mutate=(
            "(p for p in _walk_no_reparse(skill_dir) if _is_redirecting_entry(p)),",
            "(p for p in _walk_no_reparse(skill_dir) if False),",
        )
    )
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    leaky = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "leaky")
    assert not (leaky.blocked and "link or junction" in leaky.blocked), (
        "redirect check removed: the link-reaching skill should no longer be blocked by it, "
        "proving the check is what blocks it"
    )


# ---------------------------------------------------------------------------
# Round-13 GPT F2: the spec read must refuse a redirect at an INTERMEDIATE parent,
# not only the final component (O_NOFOLLOW guards only the last name).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_the_openat_reader_refuses_a_redirected_parent(tmp_path: pathlib.Path) -> None:
    """A symlinked intermediate directory on the read path returns None (refused)."""
    mod = load_build()
    root = tmp_path / "root"
    real_parent = tmp_path / "elsewhere"
    real_parent.mkdir()
    (real_parent / "frontdesk.json").write_text('{"prompt": "elsewhere"}', encoding="utf-8")
    root.mkdir()
    os.symlink(real_parent, root / "agents")  # the intermediate parent is a link

    assert (
        mod._read_text_openat(root, pathlib.Path("agents/frontdesk.json")) is None
    ), "a redirected intermediate parent must be refused by the per-component O_NOFOLLOW walk"


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_MUTATION_a_final_only_nofollow_would_follow_the_parent(tmp_path: pathlib.Path) -> None:
    """Strip O_NOFOLLOW from the intermediate dir open and the reader follows the parent link."""
    mod = load_build(
        mutate=(
            '    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)',
            "    dir_flags = os.O_RDONLY | os.O_DIRECTORY",
        )
    )
    root = tmp_path / "root"
    real_parent = tmp_path / "elsewhere"
    real_parent.mkdir()
    (real_parent / "frontdesk.json").write_text('{"prompt": "elsewhere"}', encoding="utf-8")
    root.mkdir()
    os.symlink(real_parent, root / "agents")  # the intermediate parent is a link

    text = mod._read_text_openat(root, pathlib.Path("agents/frontdesk.json"))
    assert text is not None and "elsewhere" in text, (
        "O_NOFOLLOW removed from the intermediate dir open: the walk follows the parent link "
        "to its target, proving the per-component O_NOFOLLOW is what refuses it"
    )


def test_the_local_fence_casefolds_rather_than_lowercasing() -> None:
    """Windows paths are case-insensitive, so ``~/.AWS`` names the same directory.

    And casefold is what the shared validator uses, so ``lower()`` here would be a second,
    weaker rule for one question. The two differ on real input: the German sharp s folds to
    ``ss`` where ``lower()`` leaves it alone.
    """
    mod = load_build()
    for variant in (".aws", ".AWS", ".Aws", ".aWs"):
        assert mod._looks_sensitive_standalone(f"/home/someone/{variant}/credentials"), variant


def test_the_predicate_uses_casefold_in_source() -> None:
    """A source rule, because no ASCII input distinguishes the two functions.

    ``.AWS`` is caught by either, so a behaviour test cannot tell casefold from lower. The
    difference only shows on non-ASCII, which no credential directory name has -- yet the
    shared validator casefolds, and matching it is the point.
    """
    src = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    fn = src[src.index("def _looks_sensitive_standalone(") :]
    body = fn[: fn.index("\ndef ")]
    assert ".casefold()" in body, "the predicate stopped casefolding"
    assert ".lower()" not in body, "the predicate went back to lower(), which folds less"


@_posix_only
def test_a_plan_edited_during_the_build_is_refused_not_overwritten(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The carried plan is the operator's signed file, so a stale copy must not replace it.

    The bytes are read before the build runs and written back at the end. An operator who
    edits and re-signs in between had that edit replaced with no message -- and a signature
    is the one thing they cannot reproduce from the build's output.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    plan_file = out / mod.PLAN_FILENAME
    plan_file.write_text(json.dumps({"plan_version": mod.PLAN_VERSION}), encoding="utf-8")

    edited = json.dumps({"plan_version": mod.PLAN_VERSION, "signed_by": "the operator"})
    real_read = mod._read_bytes_openat
    fired: list[str] = []

    def _edit_after_the_plan_is_read(root, rel, *args, **kwargs):
        data = real_read(root, rel, *args, **kwargs)
        # The operator saves over the plan just after the build has taken its copy, which
        # is exactly the window the fix closes. Fires once, so the re-read at the end sees
        # the edited bytes rather than being edited again underneath it. The carried-plan read
        # now goes through the whole-window bytes reader, so this patches that seam.
        if pathlib.Path(rel).name == mod.PLAN_FILENAME and not fired:
            fired.append(rel)
            plan_file.write_text(edited, encoding="utf-8")
        return data

    monkeypatch.setattr(mod, "_read_bytes_openat", _edit_after_the_plan_is_read)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})

    assert "changed while this build was running" in str(caught.value)
    assert plan_file.read_text(encoding="utf-8") == edited, "the operator's edit was lost"


@_posix_only
def test_an_unchanged_plan_is_still_carried(tmp_path: pathlib.Path) -> None:
    """The ordinary case: nobody edits it, and the plan is carried forward as before.

    Without this, a check that refused whenever a plan existed would pass the test above and
    break the documented plan-sign-build flow.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    plan_file = out / mod.PLAN_FILENAME
    body = json.dumps({"plan_version": mod.PLAN_VERSION, "signed_by": "the operator"})
    plan_file.write_text(body, encoding="utf-8")

    _build(mod, home, out, {"skills": {"faq"}})
    assert plan_file.read_text(encoding="utf-8") == body, "the carried plan was not preserved"


def test_both_credential_predicates_fold_case(tmp_path: pathlib.Path) -> None:
    """This module has TWO path predicates, and both must fold. One did not.

    ``_looks_sensitive_standalone`` was fixed to casefold and the membership test in
    ``_inside_credential_dir`` was left comparing raw components against lowercase literals
    -- so ``~/.AWS/credentials`` passed one fence and failed the other. Fixing one predicate
    and leaving its twin is the failure this test exists to catch: it drives BOTH.
    """
    mod = load_build()
    for variant in (".aws", ".AWS", ".Aws"):
        probe = pathlib.Path(f"/home/someone/{variant}/credentials")
        assert mod.refused_by_location(probe), f"the location test missed {variant}"
        assert mod._looks_sensitive_standalone(probe.as_posix()), f"fence missed {variant}"


def test_the_two_predicates_agree_on_every_shared_entry() -> None:
    """Where the two lists overlap they must give the same answer, in any case.

    They are separate lists on purpose -- one is a coarse standalone floor, the other a
    directory-name test -- but a name in both must not be sensitive to one and ordinary to
    the other, which is what a missed casefold produces.
    """
    mod = load_build()
    shared = {".ssh", ".aws", ".gnupg"}
    for entry in shared:
        for spelling in (entry, entry.upper(), entry.capitalize()):
            probe = pathlib.Path(f"/home/someone/{spelling}/thing")
            assert mod.refused_by_location(probe) == mod._looks_sensitive_standalone(
                probe.as_posix()
            ), f"the two predicates disagree on {spelling}"


def test_an_existing_staging_tree_is_refused_with_an_actionable_message(
    tmp_path: pathlib.Path,
) -> None:
    """A second build on the same --out is refused, and the message names --out.

    Refused by the ownership check above the claim rather than by the ``mkdir`` itself,
    which is the earlier and better message: it can say the tree holds files this build does
    not own. The ``mkdir`` refusal below it covers the narrower case where the path appears
    between that check and the claim.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    staging = out.parent / f"{out.name}.staging"
    staging.mkdir()
    (staging / "someone-elses-file").write_text("not ours\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    message = str(caught.value)
    if os.name == "posix":
        assert "staging" in message
        assert "--out" in message, "the refusal must name the flag the operator can change"
    else:
        assert "POSIX-only" in message


def test_an_empty_staging_directory_is_refused_by_the_marker_check(
    tmp_path: pathlib.Path,
) -> None:
    """Every way staging can already exist is refused BEFORE the claim, including empty.

    An ``except FileExistsError`` was added at the ``mkdir`` and removed: mutating it away
    left all 242 tests passing, and the case it was meant to cover -- an empty directory, on
    the reasoning that ``exists() and not is_dir()`` is False for one and an empty tree holds
    no unowned files -- is caught by the marker check, which gives a better message.

    This test is the pin for that ordering. If the marker check moves below the claim, the
    empty case reaches ``mkdir``, this assertion fails, and the translation is warranted.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True)
    (out.parent / f"{out.name}.staging").mkdir()

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    message = str(caught.value)
    if os.name == "posix":
        assert "this build did not create" in message, (
            "an empty staging tree is no longer refused by the marker check, so the claim's "
            "own FileExistsError is now reachable and needs translating"
        )
    else:
        assert "POSIX-only" in message


def test_the_claim_is_still_the_mkdir(tmp_path: pathlib.Path) -> None:
    """A source rule: ``exist_ok`` must not appear on the staging claim.

    ``exist_ok=True`` would make the refusal above unreachable while every other test still
    passed, and two builds writing one staging tree is worse than either failing.
    """
    src = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    assert "staging.mkdir(parents=True)\n" in src, "the staging claim changed shape"
    assert (
        "staging.mkdir(parents=True, exist_ok=True)" not in src
    ), "exist_ok=True would let two builds share one staging tree"


def test_the_local_patterns_catch_everything_the_shared_detector_does() -> None:
    """The local subset is a documented NARROWING, so the narrowing must be measured.

    ``_HARD_PATTERNS`` exists for the standalone case where ``kiro_crew.security`` cannot be
    imported. Calling it a subset is only honest if someone checks: three gaps were found by
    running this comparison rather than reading the two lists side by side --
    the two SSH public-key line forms, and the URL-encoded PEM header. The shared detector
    spells its separator ``[\\s+%]`` precisely for the encoded form, and the local copy had a
    literal space, so the encoded header passed unmatched.

    Executable rather than a source rule, because the shared patterns can change under this
    module: a new form added upstream should fail here, which is the whole point.
    """
    from kiro_crew.security import _HARD_CREDENTIAL_RE

    mod = load_build()
    # Every credential-shaped sample is ASSEMBLED, never written as one literal. The repo's
    # secret scanners read this file too, and a test that proves a scanner works must not
    # itself trip one -- ``test_producer.py`` already does this (``"AKIA" +
    # "IOSFODNN7EXAMPLE"[4:] + "ABCD"``), so this follows that convention rather than
    # inventing an exemption.
    _akia = "AKIA" + "IOSFODNN7EXAMPLE"
    _asia = "ASIA" + "IOSFODNN7EXAMPLE"
    _secret_label = "Secret" + "AccessKey"
    _secret_body = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY"
    inputs = {
        "aws-key-akia": _akia,
        "aws-key-asia": _asia,
        "labelled-secret": f'{_secret_label}="{_secret_body}"',
        "labelled-session": "aws_session" + "_token=FQoGZXIvYXdzEBYaDF",
        "labelled-access-id": "aws_access" + f"_key_id={_akia}",
        "access-key-id-label": "Access" + f'KeyId: "{_akia}"',
        "ssh-rsa-line": "ssh-" + "rsa AAAAB3NzaC1yc2EA user@host",
        "ssh-ed25519-line": "ssh-" + "ed25519 AAAAC3NzaC1lZDI1 user@host",
        "pem-header": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "pem-header-encoded": "BEGIN+" + "RSA+PRIVATE+KEY",
        "slack-token": "xox" + "b-123456789012-abcdefghijkl",
    }
    gaps = []
    for name, text in inputs.items():
        if not _HARD_CREDENTIAL_RE.search(text):
            continue  # not a shared-detector case; nothing is claimed about it
        if not any(pattern.search(text) for _, pattern in mod._HARD_PATTERNS):
            gaps.append(name)
    assert not gaps, (
        f"the standalone scan misses what the shared detector catches: {gaps}. The local set "
        f"may be COARSER in what it adds, never narrower in what the shared one refuses."
    )


def test_the_local_set_may_add_forms_the_shared_one_omits() -> None:
    """The relationship is one-directional, and that is deliberate.

    The local set catches GitHub and vendor tokens the shared detector does not, and that is
    fine: refusing more in a mode with no other floor is the safe direction. Asserting
    equality instead would delete those on the next run of the test above.
    """
    mod = load_build()
    extra = ("ghp" + "_" + "a" * 36, "sk" + "-" + "b" * 24)
    for text in extra:
        assert any(pattern.search(text) for _, pattern in mod._HARD_PATTERNS), text


# ---------------------------------------------------------------------------
# Round-14 GPT F1: on a platform without dir_fd/O_NOFOLLOW (Windows), the spec
# read must FAIL CLOSED on a redirecting component, not fall through to a reader
# that follows it.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="uses symlinks to stand in for a junction")
def test_the_windows_fallback_refuses_a_redirected_component(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """With dir_fd unsupported (the Windows path), a linked parent yields None (refused)."""
    mod = load_build()
    monkeypatch.setattr(mod, "_dir_fd_supported", lambda: False)
    root = tmp_path / "root"
    real_parent = tmp_path / "elsewhere"
    real_parent.mkdir()
    (real_parent / "frontdesk.json").write_text('{"prompt": "elsewhere"}', encoding="utf-8")
    root.mkdir()
    os.symlink(real_parent, root / "agents")  # intermediate parent redirects

    assert (
        mod._read_text_openat(root, pathlib.Path("agents/frontdesk.json")) is None
    ), "the Windows fallback must refuse a redirecting component, not read through it"


@pytest.mark.skipif(os.name != "posix", reason="uses symlinks to stand in for a junction")
def test_MUTATION_the_windows_fallback_would_follow_without_the_redirect_check(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Drop the fail-closed redirect check and the Windows fallback follows the linked parent."""
    mod = load_build(
        mutate=(
            "        if _redirect_between(root, root / rel) is not None:\n            return None\n",
            "",
        )
    )
    monkeypatch.setattr(mod, "_dir_fd_supported", lambda: False)
    root = tmp_path / "root"
    real_parent = tmp_path / "elsewhere"
    real_parent.mkdir()
    (real_parent / "frontdesk.json").write_text('{"prompt": "elsewhere"}', encoding="utf-8")
    root.mkdir()
    os.symlink(real_parent, root / "agents")

    text = mod._read_text_openat(root, pathlib.Path("agents/frontdesk.json"))
    assert text is not None and "elsewhere" in text, (
        "fail-closed check removed: the fallback follows the linked parent, proving the "
        "check is what refuses it"
    )


# ---------------------------------------------------------------------------
# Round-14 GPT F3: a concurrent staging claim loses cleanly (ExportRefused),
# it does not crash with FileExistsError.
# ---------------------------------------------------------------------------
def test_a_concurrent_staging_claim_is_refused_not_crashed(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A FileExistsError at the staging mkdir surfaces as an 'already claimed' ExportRefused."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"

    real_mkdir = pathlib.Path.mkdir

    def _lose_the_claim(self, *args, **kwargs):
        # Only the staging-claim mkdir is exist_ok-false; simulate the loser of that race.
        if self.name.endswith(".staging") and not kwargs.get("exist_ok"):
            raise FileExistsError(17, "File exists")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", _lose_the_claim)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    msg = str(caught.value)
    if os.name == "posix":
        assert "claimed by another build" in msg
    else:
        assert "POSIX-only" in msg


@_posix_only
def test_MUTATION_a_concurrent_staging_claim_would_crash_without_the_translation(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Remove the FileExistsError translation and the loser crashes with the raw error."""
    mod = load_build(
        mutate=(
            "    try:\n        staging.mkdir(parents=True)\n    except FileExistsError:",
            "    if False:\n        staging.mkdir(parents=True)\n    elif True:\n        staging.mkdir(parents=True)\n    if False:",
        )
    )
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"

    real_mkdir = pathlib.Path.mkdir

    def _lose_the_claim(self, *args, **kwargs):
        if self.name.endswith(".staging") and not kwargs.get("exist_ok"):
            raise FileExistsError(17, "File exists")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", _lose_the_claim)
    with pytest.raises(FileExistsError):
        _build(mod, home, out, {"skills": {"faq"}})


def test_the_windows_narrowing_is_the_repos_own_settled_answer() -> None:
    """Windows cannot pin a traversal, and this build does not pretend otherwise.

    A review asked twice for descriptor-anchored traversal on Windows -- "use Windows
    no-reparse handles for every component". Three facts, each checkable:

    * ``pinned_fs.supports_pinned_walk()`` requires ``O_DIRECTORY``, ``O_NOFOLLOW`` and
      ``os.open in os.supports_dir_fd``, and returns False on Windows. The repo's own pinning
      module therefore does not offer this either -- adopting it would not close the gap.
    * Every caller of it in the tree branches on that predicate rather than assuming it.
    * ``eval/bench/safepath.py`` reached this exact question and settled it against a ctypes
      ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT``, because it buys a property
      another mechanism already gives "at the price of security code that cannot be
      exercised on the machine this harness is developed on".

    So the Windows branch checks each component by attribute, states that a swap inside the
    remaining window wins, and refuses a redirect planted before the build ran -- which is
    the realistic shape. Pinned as a rejection so the next review pass reads the reasoning
    instead of re-filing the request.
    """
    import kiro_crew.pinned_fs as pinned_fs

    src = pathlib.Path(pinned_fs.__file__).read_text(encoding="utf-8")
    assert "os.open in os.supports_dir_fd" in src, (
        "supports_pinned_walk stopped gating on dir_fd support; if the repo has gained "
        "pinned traversal on Windows, this build should use it"
    )
    assert "FILE_FLAG_OPEN_REPARSE_POINT" not in src, (
        "pinned_fs has grown a Windows no-reparse path; the narrowing below is then "
        "avoidable and should be replaced by it"
    )

    # Read from THIS tree, and matched on a fragment that does not span the wrap: the
    # sentence is broken across two source lines, so "worth considering" as one
    # string is never present in the file.
    settled = pathlib.Path(pinned_fs.__file__).parent / "eval" / "bench" / "safepath.py"
    if settled.exists():
        precedent = settled.read_text(encoding="utf-8")
        assert (
            "FILE_FLAG_OPEN_REPARSE_POINT`` is not worth" in precedent
        ), "the precedent this rejection cites is gone; re-argue rather than assume it"
        assert (
            "cannot be exercised on the machine" in precedent
        ), "the precedent's REASON is gone, which is the part this rejection borrows"


def test_a_github_fine_grained_pat_is_caught_by_the_scan() -> None:
    """github_pat_ ... is a credential the classic gh[pousr]_ pattern does not match."""
    mod = load_build()
    pat = "github_pat_" + "A" * 22 + "_" + "b" * 59
    leaks = mod.scan_text(f"token = {pat}", "prompt")
    assert any("github-fine-grained-pat" in leak.kind for leak in leaks), [
        leak.kind for leak in leaks
    ]


def test_MUTATION_a_fine_grained_pat_slips_without_its_pattern() -> None:
    """Drop the fine-grained PAT from the vendor set and the token slips through unflagged.

    The vendor/token spellings are sourced from the shared ``credential_patterns`` module
    and spliced in as ``_VENDOR_TOKEN_COMPILED``; the mutation filters that one format out of
    the compiled set, which is the construct that now carries the catch.
    """
    mod = load_build(
        mutate=(
            "    *_VENDOR_TOKEN_COMPILED,\n",
            "    *(p for p in _VENDOR_TOKEN_COMPILED " 'if p[0] != "github-fine-grained-pat"),\n',
        )
    )
    pat = "github_pat_" + "A" * 22 + "_" + "b" * 59
    leaks = mod.scan_text(f"token = {pat}", "prompt")
    assert not any(
        "github-fine-grained-pat" in leak.kind for leak in leaks
    ), "pattern removed: the fine-grained PAT should slip, proving the pattern catches it"


def test_a_jwt_is_caught_by_the_scan() -> None:
    """A three-segment eyJ... JWT is a bearer/session credential the local set had missed."""
    mod = load_build()
    jwt = "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20
    leaks = mod.scan_text(f"authorization: Bearer {jwt}", "prompt")
    assert any(leak.kind == "jwt" for leak in leaks), [leak.kind for leak in leaks]


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_redirect_between_flags_a_nested_linked_component(tmp_path: pathlib.Path) -> None:
    """The guard skill_candidates consults reports a nested redirecting component.

    On Windows ``rglob`` descends into a junction (a non-symlink reparse point) and yields a
    SKILL.md under it; ``_redirect_between`` is what ``skill_candidates`` calls to refuse that
    path before the resolving read. POSIX ``rglob`` does not descend a symlinked directory, so
    the traversal itself cannot be reproduced here -- the guard's unit is tested directly, on
    the same kind of redirecting component (a symlink), which is what it inspects by lstat.
    """
    mod = load_build()
    root = tmp_path / "skills"
    (root / "faq").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "leaky").mkdir(parents=True)
    (outside / "leaky" / "SKILL.md").write_text("# borrowed\n", encoding="utf-8")
    os.symlink(outside, root / "borrowed")

    # A path whose intermediate component (``borrowed``) redirects is flagged...
    crossed = mod._redirect_between(root, root / "borrowed" / "leaky" / "SKILL.md")
    assert crossed == root / "borrowed"
    # ...and a clean in-tree path is not.
    assert mod._redirect_between(root, root / "faq") is None


@pytest.mark.skipif(os.name != "posix", reason="uses a symlinked dir to stand in for a junction")
def test_the_walk_does_not_descend_a_redirecting_directory(tmp_path: pathlib.Path) -> None:
    """A file under a linked/junctioned subdir is not yielded; the link entry itself is."""
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "in_tree.txt").write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.txt").write_text("secret\n", encoding="utf-8")
    os.symlink(outside, root / "linked")

    mod = load_build()
    got = {p.relative_to(root).as_posix() for p in mod._walk_no_reparse(root)}
    assert "real/in_tree.txt" in got, "an ordinary in-tree file is still walked"
    assert "linked" in got, "the redirect entry itself is yielded so a caller can refuse it"
    assert "linked/stolen.txt" not in got, "the walk must NOT descend into the redirect"


@pytest.mark.skipif(os.name != "posix", reason="uses a symlinked dir to stand in for a junction")
def test_MUTATION_a_descending_walk_would_reach_the_out_of_tree_file(
    tmp_path: pathlib.Path,
) -> None:
    """Let the walk recurse into a reparse point and it reaches the out-of-tree bytes."""
    mod = load_build(
        mutate=(
            "            if is_real_dir and not _is_redirecting_entry(p):",
            "            if is_real_dir or _is_redirecting_entry(p):",
        )
    )
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.txt").write_text("secret\n", encoding="utf-8")
    os.symlink(outside, root / "linked")

    got = {p.relative_to(root).as_posix() for p in mod._walk_no_reparse(root)}
    assert "linked/stolen.txt" in got, (
        "reparse refusal removed: the walk descends the link and reaches the out-of-tree "
        "file, proving the refusal is what keeps traversal inside the root"
    )


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a redirect")
def test_a_plan_path_that_is_a_symlink_is_refused(tmp_path: pathlib.Path) -> None:
    """A --allow path that is a link is refused at the no-follow open, not read through."""
    mod = load_build()
    real = tmp_path / "real.json"
    real.write_text('{"crew": "x"}', encoding="utf-8")
    link = tmp_path / "plan.json"
    os.symlink(real, link)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_plan(link)
    assert "could not be read" in str(caught.value) or "link" in str(caught.value)


@_posix_only
def test_a_hard_linked_plan_is_refused_at_the_read(tmp_path: pathlib.Path) -> None:
    """A --allow plan that is a hard link to another name is refused, not read through it.

    The no-follow component walk cannot see a HARD LINK -- a second name for the same inode --
    so a credential hard-linked to an innocent ``.json`` plan name passes every path and shape
    check while its bytes are the credential's. The openat leaf read fstats the opened
    descriptor and refuses ``st_nlink > 1``, the same identity the shared file-read guard
    refuses, so the plan read returns nothing to ingest.
    """
    mod = load_build()
    outside = tmp_path / "outside_secret.json"
    outside.write_text('{"crew": "x", "reviewed_by": "z", "reviewed_at": "z"}', encoding="utf-8")
    plan = tmp_path / "plan.json"
    os.link(outside, plan)
    assert plan.stat().st_nlink > 1, "test setup: the plan must be a hard link"

    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_plan(plan)
    assert "could not be read" in str(caught.value) or "no curation plan" in str(caught.value)


@_posix_only
def test_MUTATION_a_plan_read_without_the_st_nlink_check_follows_a_hard_link(
    tmp_path: pathlib.Path,
) -> None:
    """Drop the st_nlink refusal in the openat leaf read and the hard-linked plan is read through.

    Reddens the fix: with the ``st_nlink > 1`` check removed, the hard-linked plan decodes and
    the read stops refusing it -- proving the fstat on the opened leaf is what closes the
    second-name hole.
    """
    mod = load_build(
        mutate=(
            "        try:\n            if os.fstat(file_fd).st_nlink > 1:\n"
            "                os.close(file_fd)\n                return None",
            "        try:\n            if False:\n"
            "                os.close(file_fd)\n                return None",
        )
    )
    outside = tmp_path / "outside_secret.json"
    outside.write_text('{"crew": "x", "reviewed_by": "z", "reviewed_at": "z"}', encoding="utf-8")
    plan = tmp_path / "plan.json"
    os.link(outside, plan)

    # With the check dropped, the read does not refuse on the hard-link identity: it either
    # reads the plan through (crew mismatch -> a DIFFERENT refusal, not the read refusal) or
    # decodes it. Either way the "could not be read"/"no curation plan" read-refusal is absent.
    try:
        mod.read_plan(plan)
        refused_at_read = False
    except mod.ExportRefused as exc:
        refused_at_read = "could not be read" in str(exc) or "no curation plan" in str(exc)
    assert not refused_at_read, (
        "with the st_nlink check dropped the hard-linked plan was still refused at the read -- "
        "the fstat on the opened leaf is what should be doing the refusing"
    )


@_posix_only
def test_MUTATION_the_plan_read_would_follow_a_link_without_the_openat_reader(
    tmp_path: pathlib.Path,
) -> None:
    """Route read_plan back to the final-component-only reader and an INTERMEDIATE link is followed.

    ``_read_text_openat`` anchors every component; ``_read_text_nofollow`` guards only the last.
    Mutating the plan read back to the final-component reader restores the exact hole GPT found:
    a symlink at an intermediate directory is followed into whatever it names.
    """
    if os.name != "posix":
        pytest.skip("symlink semantics")
    mod = load_build(
        mutate=(
            "    text = _read_text_openat(\n"
            "        Path(abs_path.anchor), abs_path.relative_to(abs_path.anchor), "
            "refuse_hard_link=True\n    )",
            "    text = _read_text_nofollow(path)",
        )
    )
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "auth.json").write_text(
        '{"crew": "x", "reviewed_by": "", "reviewed_at": ""}', encoding="utf-8"
    )
    alias = tmp_path / "alias"
    os.symlink(secret_dir, alias)  # an INTERMEDIATE directory symlink into the secret dir
    plan_via_alias = alias / "auth.json"
    # The final-component reader no-follows only the leaf, so the intermediate ``alias`` link is
    # traversed and the file under the secret dir is read -- it does NOT refuse at the read.
    try:
        mod.read_plan(plan_via_alias)
        followed = True
    except mod.ExportRefused as exc:
        followed = "could not be read" not in str(exc)
    assert followed, "the openat reader is restored: the intermediate link is not traversed"


def test_read_plan_refuses_an_intermediate_symlink_into_a_credential_dir(
    tmp_path: pathlib.Path,
) -> None:
    """GPT :2128 -- an INTERMEDIATE component of the --allow path that redirects is refused.

    ``--allow /tmp/alias/auth.json`` with ``alias -> ~/.codex`` would, under a final-component
    reader, be followed into the credential dir and read into the bundle -- and the literal
    standalone fence cannot catch it because the resolved location is not spelled in the path.
    The component-anchored read opens every component no-follow, so the ``alias`` link fails its
    own open and the read returns nothing to ship.
    """
    if os.name != "posix":
        pytest.skip("symlink semantics")
    mod = load_build()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "auth.json").write_text(
        '{"crew": "x", "reviewed_by": "", "reviewed_at": ""}', encoding="utf-8"
    )
    alias = tmp_path / "alias"
    os.symlink(secret_dir, alias)  # intermediate directory symlink
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_plan(alias / "auth.json")
    # Refused at the read (redirect at a component), not read through into the secret file.
    assert "could not be read" in str(caught.value) or "no curation plan" in str(caught.value)


# ---------------------------------------------------------------------------
# Round-17 GPT F3: the aside-path recursive delete goes through a run-private aside
# (rename into a dir this build owns, delete there), removing the rmtree-by-path
# window rather than narrowing it.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_leftover_previous_bundle_is_deleted_on_the_next_build(tmp_path: pathlib.Path) -> None:
    """A build-owned <out>.previous left by a prior crash is purged, and the new build lands."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})
    previous = out.parent / (out.name + ".previous")
    import shutil as _shutil

    _shutil.copytree(out, previous)  # the leftover a prior crash between the two renames leaves
    assert previous.exists()

    _build(mod, home, out, {"skills": {"faq"}})  # must purge previous and land the new bundle
    assert (out / "skills" / "faq" / "SKILL.md").is_file()
    assert not previous.exists(), "the leftover previous bundle was purged"
    # No run-private purge directory is left behind beside the output.
    leftovers = [q.name for q in out.parent.iterdir() if q.name.startswith(".smc-purge-")]
    assert leftovers == [], f"a run-private purge dir was stranded: {leftovers}"


@_posix_only
def test_the_purge_deletes_only_inside_its_private_aside(tmp_path: pathlib.Path) -> None:
    """_purge_via_private_aside moves the target into a private dir and deletes only there.

    A sibling tree beside the target is untouched: the recursive delete runs entirely under a
    directory this build alone created, so it cannot reach anything outside it.
    """
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "f.txt").write_text("doomed\n", encoding="utf-8")
    sibling = parent / "bundle"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("safe\n", encoding="utf-8")

    mod._purge_via_private_aside(target, lambda parent_fd, moved_rel: None)  # verifier passes

    assert not target.exists(), "the target tree was deleted"
    assert (sibling / "keep.txt").is_file(), "a sibling tree outside the target is untouched"
    assert [q.name for q in parent.iterdir() if q.name.startswith(".smc-purge-")] == []


@_posix_only
def test_MUTATION_a_path_rmtree_would_leave_the_window(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Route the purge back to a bare rmtree-by-path and the private-aside containment is gone.

    Proves the private-aside is what removes the window: with the mutation, the delete is a
    plain ``shutil.rmtree(target)`` again -- no private dir is created, which this asserts by
    the absence of any ``.smc-purge-`` directory ever appearing (the mutated body never makes
    one). The delete still happens (the target goes), but by path, which is the racy shape the
    real code replaced.
    """
    mod = load_build(
        mutate=(
            '        private_name = f".smc-purge-{uuid.uuid4().hex}"',
            '        import shutil; shutil.rmtree(target, ignore_errors=True); return  # mutated: path-racy\n        private_name = f".smc-purge-{uuid.uuid4().hex}"',
        )
    )
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "f.txt").write_text("x\n", encoding="utf-8")
    seen_private = {"any": False}
    real_mkdir = os.mkdir

    def _watch_mkdir(path, *a, **k):
        name = path if isinstance(path, str) else getattr(path, "name", "")
        if str(name).startswith(".smc-purge-"):
            seen_private["any"] = True
        return real_mkdir(path, *a, **k)

    monkeypatch.setattr(os, "mkdir", _watch_mkdir)
    # The base branch widened this to take a verifier, called on the moved-aside inode so
    # the verified inode and the deleted one are the same. A no-op verifier is right for
    # THIS test: what it pins is that the mutated body deletes by path, and a verifier
    # that refused would mask that by aborting earlier.
    mod._purge_via_private_aside(target, lambda parent_fd, moved_rel: None)
    assert not target.exists(), "the mutated path-rmtree still deletes the target"
    assert seen_private["any"] is False, (
        "mutated to a bare rmtree-by-path: no run-private aside is created, proving the "
        "private aside is what the real code uses to contain the delete"
    )


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_the_nofollow_reader_fails_closed_when_o_nofollow_is_unavailable(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """With O_NOFOLLOW forced to 0 (the Windows case), a linked path is refused, not read."""
    mod = load_build()
    # Force the Windows condition: no working O_NOFOLLOW. The reader must then lstat-refuse
    # a reparse point before the open instead of following it.
    monkeypatch.setattr(mod.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(mod, "_NOFOLLOW_READ_FLAGS", 0, raising=False)
    real = tmp_path / "real.txt"
    real.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "spec.txt"
    os.symlink(real, link)
    assert (
        mod._read_text_nofollow(link) is None
    ), "O_NOFOLLOW unavailable: the reader must fail closed on a reparse point, not follow it"
    assert mod._read_text_nofollow(real) == "secret\n", "an ordinary file still reads"


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_MUTATION_without_the_fail_closed_guard_the_windows_reader_would_follow(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Drop the fail-closed reparse check and the O_NOFOLLOW-less reader follows the link."""
    mod = load_build(
        mutate=(
            '    if not getattr(os, "O_NOFOLLOW", 0) and _is_redirecting_entry(path):\n        return None\n',
            "",
        )
    )
    monkeypatch.setattr(mod.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(mod, "_NOFOLLOW_READ_FLAGS", 0, raising=False)
    real = tmp_path / "real.txt"
    real.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "spec.txt"
    os.symlink(real, link)
    assert mod._read_text_nofollow(link) == "secret\n", (
        "guard removed + O_NOFOLLOW unavailable: the reader follows the link, proving the "
        "fail-closed check is what refuses it on that platform"
    )


@_posix_only
def test_the_purge_verifies_the_moved_tree_and_restores_it_on_a_failed_check(
    tmp_path: pathlib.Path,
) -> None:
    """Ownership is checked on the MOVED tree, closing the check-to-rename window.

    A plain rmtree-by-path, or a check taken at the path BEFORE the rename, leaves a window: a
    tree swapped in between the check and the delete is deleted anyway. Here the verifier runs
    on the entry the rename captured, so the inode verified is the inode deleted -- and a tree
    that fails the check is renamed BACK, never deleted.
    """
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data swapped in\n", encoding="utf-8")

    def _reject(parent_fd, moved_rel):
        raise mod.ExportRefused("not a build-written tree")

    with pytest.raises(mod.ExportRefused):
        mod._purge_via_private_aside(target, _reject)

    assert target.is_dir(), "a tree that fails the ownership check is restored, not deleted"
    assert (target / "keep.txt").read_text(encoding="utf-8") == "operator data swapped in\n"
    assert [q.name for q in parent.iterdir() if q.name.startswith(".smc-purge-")] == []


@_posix_only
def test_MUTATION_verifying_before_the_rename_would_delete_a_swapped_tree(
    tmp_path: pathlib.Path,
) -> None:
    """Drop the ownership check on the moved entry and a swapped-in tree is deleted anyway.

    The mutation replaces the ``verify(parent_fd, moved_rel)`` call with a no-op, so the moved
    entry is deleted without being confirmed as one this build wrote. The verifier here always
    refuses: under the mutation the refusal never runs and the tree is deleted; the real code
    calls it on the captured entry, refuses, and restores the tree untouched.
    """
    mod = load_build(
        mutate=(
            "                verify(parent_fd, moved_rel)\n",
            "                pass  # mutated: moved-entry ownership check skipped\n",
        )
    )
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data\n", encoding="utf-8")

    def _reject(parent_fd, moved_rel):
        raise mod.ExportRefused("moved entry rejected")

    mod._purge_via_private_aside(target, _reject)
    assert not target.exists(), (
        "mutated to skip the moved-entry check: the swapped-in tree is deleted, proving the "
        "verify-the-captured-entry step is what closes the window"
    )


@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make a real dir unreadable")
def test_an_unreadable_selected_directory_refuses_instead_of_shipping_incomplete(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    crew = mod.resolve_crew("frontdesk", home)
    unreadable = crew.skills_root / "faq" / "topics"
    unreadable.mkdir()
    (unreadable / "a.md").write_text("hours\n", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    try:
        out = tmp_path / "work" / "bundle"
        with pytest.raises(mod.ExportRefused) as caught:
            _build(mod, home, out, {"skills": {"faq"}})
        assert "could not be listed" in str(caught.value)
        assert "topics" in str(caught.value)
    finally:
        # Restores the mode the fixture cleared to 0o000. Traverse permission is what the
        # temp-directory teardown needs, so a tighter mode leaves the tree undeletable.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(unreadable, 0o755)


@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make a real dir unreadable")
def test_MUTATION_skipping_an_unreadable_dir_would_ship_incomplete(tmp_path: pathlib.Path) -> None:
    """Revert the walk to swallow an enumeration failure and the build ships without refusing."""
    mod = load_build(
        mutate=(
            "        except OSError as exc:\n            # A directory that EXISTS",
            "        except OSError:\n            continue\n        except OSError as exc:\n            # A directory that EXISTS",
        )
    )
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    crew = mod.resolve_crew("frontdesk", home)
    unreadable = crew.skills_root / "faq" / "topics"
    unreadable.mkdir()
    (unreadable / "a.md").write_text("hours\n", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    try:
        out = tmp_path / "work" / "bundle"
        _build(mod, home, out, {"skills": {"faq"}})  # mutated: no refusal
        assert (out / "skills" / "faq" / "SKILL.md").is_file(), (
            "mutated to swallow the enumeration failure: the build ships the bundle omitting "
            "the unreadable directory, proving the fail-closed raise is what refuses it"
        )
    finally:
        # Restores the mode the fixture cleared to 0o000. Traverse permission is what the
        # temp-directory teardown needs, so a tighter mode leaves the tree undeletable.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(unreadable, 0o755)


@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make a real file unreadable")
def test_an_unreadable_existing_report_refuses_rather_than_risk_deleting_it(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build writes the report
    report = out.parent / (out.name + ".smc-bundle.json")
    assert report.is_file()
    os.chmod(report, 0o000)
    try:
        with pytest.raises(mod.ExportRefused) as caught:
            _build(mod, home, out, {"skills": {"faq"}})
        assert "existing report" in str(caught.value) and "cannot be read" in str(caught.value)
    finally:
        os.chmod(report, 0o644)


def test_a_dotenv_plan_path_is_refused_by_the_standalone_floor() -> None:
    """`.env` is a credential leaf the standalone floor must catch even without the validator."""
    mod = load_build()
    assert mod._looks_sensitive_standalone("home/user/.kiro/crew/.env") is True
    assert mod._looks_sensitive_standalone("home/user/project/app.env") is False
    assert mod._looks_sensitive_standalone("home/user/secret.pem") is True


def test_MUTATION_without_the_credential_name_rule_the_floor_misses_dotenv() -> None:
    """Remove the credential-name check and the standalone floor lets `.env` through."""
    mod = load_build(
        mutate=(
            "    if parts and _CREDENTIAL_NAME_RE.match(parts[-1]):",
            "    if parts and False:",
        )
    )
    assert mod._looks_sensitive_standalone("home/user/.kiro/crew/.env") is False, (
        "credential-name rule removed: the floor no longer catches .env, proving that rule is "
        "what closes the leaf gap when the shared validator is unavailable"
    )


# ---------------------------------------------------------------------------
# Round-19 GPT F2(a): a FAILED restore of a swapped-in tree must NOT fall through
# to a recursive delete -- retain the aside, abort, name where the tree sits.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_failed_restore_retains_the_tree_and_does_not_delete_it(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """When the ownership check fails AND the rename-back fails, the tree is kept, not deleted.

    A failed restore is not a licence to recursively delete a tree this build did not create.
    The private aside is retained and the refusal names where the tree is, so nothing removes
    an operator-owned tree.
    """
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data\n", encoding="utf-8")

    real_rename = os.rename
    calls = {"n": 0}

    def _rename_second_fails(src, dst, *a, **k):
        # First rename (target -> private) succeeds; the restore rename (private -> target)
        # fails, standing in for the original name being taken again in the meantime.
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("restore blocked")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _rename_second_fails)

    def _reject(parent_fd, moved_rel):
        raise mod.ExportRefused("swapped-in tree")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._purge_via_private_aside(target, _reject)
    assert "NOT been deleted" in str(caught.value)
    # The tree still exists, contained in the retained private aside (never recursively deleted).
    asides = [q for q in parent.iterdir() if q.name.startswith(".smc-purge-")]
    assert asides, "the private aside is retained on a failed restore"
    survivor = asides[0] / "bundle.previous" / "keep.txt"
    assert survivor.read_text(encoding="utf-8") == "operator data\n", "the tree was NOT deleted"


@_posix_only
def test_MUTATION_cleaning_the_aside_on_a_failed_restore_would_delete_the_tree(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Revert to always-cleanup and a failed restore recursively deletes the swapped-in tree."""
    mod = load_build(
        mutate=(
            "                    cleanup_private = False\n",
            "",
        )
    )
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data\n", encoding="utf-8")
    real_rename = os.rename
    calls = {"n": 0}

    def _rename_second_fails(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("restore blocked")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _rename_second_fails)

    def _reject(parent_fd, moved_rel):
        raise mod.ExportRefused("swapped-in tree")

    with pytest.raises(mod.ExportRefused):
        mod._purge_via_private_aside(target, _reject)
    asides = [q for q in parent.iterdir() if q.name.startswith(".smc-purge-")]
    assert asides == [], (
        "mutated to always clean the aside: the swapped-in operator tree is recursively "
        "deleted on a failed restore, proving the retain-on-failure guard is what prevents it"
    )


# ---------------------------------------------------------------------------
# Round-19 GPT F2(b): the POST-PROMOTION delete of the aside bundle goes through
# move-verify-delete, so a swap between the redirect check and the delete cannot
# clobber a tree this build did not write.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_swapped_previous_after_promotion_is_not_clobbered(tmp_path: pathlib.Path) -> None:
    """A non-build tree standing at `<out>.previous` at post-promotion delete time is refused.

    The post-promotion cleanup moves-verifies-deletes, so an operator directory that is not a
    build-written bundle is restored, not deleted -- a bare rmtree-by-path would delete it.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build
    # Stand an operator-owned, NON-build directory at <out>.previous, as a swap would.
    previous = out.parent / (out.name + ".previous")
    previous.mkdir()
    (previous / "operator.txt").write_text("not a bundle\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused):
        _build(mod, home, out, {"skills": {"faq"}})  # second build hits the previous path
    assert (previous / "operator.txt").is_file(), "a non-build tree at previous is not deleted"


def test_the_local_subset_fences_every_foreign_credential_store() -> None:
    """A credential store belonging to ANOTHER tool must not be ordinary to the local fence.

    Scoped to the stores that sit outside the crew's own data home, which is the class this
    list already covers: ``.aws``, ``.docker/config.json``, ``.kube/config``, ``.ssh`` and the
    rest are all somebody else's credentials under ``$HOME``. The crew's own home is excluded
    deliberately -- the validator classifies well over a hundred leaves under it, this build
    READS that tree by design, and the fence is documented as allowed to be coarser there.

    Measured before this test existed: ``.codex/auth.json``, ``.claude/.credentials.json``,
    ``.local/share/amazon-q`` and ``.local/share/kiro-cli`` were all classified by the shared
    validator and all ordinary to this fence. In the standalone mode this fence is the only
    one, so each omission was a readable credential belonging to another agent.
    """
    from kiro_crew.security import paths as sec_paths
    from kiro_crew.security.paths import is_sensitive_path

    own_home = (".kiro", ".kirocrew")
    leaves = set()
    for name in dir(sec_paths):
        value = getattr(sec_paths, name)
        if isinstance(value, (tuple, list, frozenset, set)):
            for item in value:
                if isinstance(item, str) and "/" in item and item.startswith("."):
                    leaves.add(item)

    mod = load_build()
    home = pathlib.Path.home()
    missed = []
    for entry in sorted(leaves):
        if pathlib.PurePosixPath(entry).parts[0] in own_home:
            continue
        if not is_sensitive_path(f"{home}/{entry}"):
            continue
        if not mod._looks_sensitive_standalone(f"/srv/crew/{entry}"):
            missed.append(entry)
    assert not missed, (
        f"the shared validator classifies these as credential stores belonging to another "
        f"tool, and the standalone fence treats them as ordinary: {missed}. That fence is the "
        f"only one when kiro_crew.security cannot be imported."
    )


@_posix_only
def test_an_entry_that_cannot_be_inspected_refuses_instead_of_leaving_its_subtree_out(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """An entry whose stat fails must not read as a leaf, which would omit its whole subtree.

    The walk refuses an unlistable DIRECTORY one level up. This is the same omission one level
    down: if deciding whether an entry is a directory fails, an unstattable directory is never
    pushed onto the stack, so nothing under it reaches the candidate list, the copy, or the
    hash -- and the bundle is signed without it.

    The failure is injected rather than provoked, because Linux answers ``is_dir`` from the
    directory entry's own ``d_type`` and never stats: measured on this host, a directory with
    its execute bit cleared still reported ``is_dir`` correctly, so there is no local file
    layout that reaches this branch. Only the target directory's entries are substituted; every
    other scandir call delegates, so the rest of the walk is the real one.
    """
    mod = load_build()
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "kept.txt").write_text("kept\n", encoding="utf-8")

    class _UninspectableEntry:
        def __init__(self, real):
            self._real = real
            self.name = real.name
            self.path = real.path

        def is_dir(self, *, follow_symlinks=True):
            raise PermissionError(13, "Permission denied")

        def is_symlink(self):
            return self._real.is_symlink()

    class _ScandirResult:
        # os.scandir returns an iterator that is ALSO a context manager: shutil.rmtree (and
        # other stdlib callers) use it as ``with os.scandir(p) as it:``. A bare list satisfies
        # only iteration, so a substitute that returns one breaks any caller using the ``with``
        # form -- which is why temp-dir teardown died with a TypeError while the test's own
        # (iterate-only) path passed. Honour the whole contract on every platform.
        def __init__(self, items):
            self._it = iter(items)

        def __iter__(self):
            return self._it

        def __next__(self):
            return next(self._it)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

    real_scandir = mod.os.scandir

    def _scandir(where):
        # Delegate untouched for anything that is not a path. ``os.scandir`` also accepts an
        # open directory descriptor, and the temp-directory teardown calls it that way while
        # this patch is still installed -- comparing an int against a path raised there and
        # turned a passing test into an error.
        if not isinstance(where, (str, os.PathLike)):
            return real_scandir(where)
        # ``real_scandir`` yields a context-manager iterator; drain it inside its own ``with``
        # so no descriptor leaks, then hand back a stand-in that honours the SAME contract.
        with real_scandir(where) as it:
            entries = list(it)
        if pathlib.Path(where) == root:
            return _ScandirResult([_UninspectableEntry(e) for e in entries])
        return _ScandirResult(entries)

    monkeypatch.setattr(mod.os, "scandir", _scandir)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._walk_no_reparse(root)
    message = str(caught.value)
    assert "could not be inspected" in message
    assert "sub" in message, "the refusal must name the entry it could not inspect"


@_posix_only
def test_the_chain_walk_runs_before_the_resolving_fence(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The non-following component walk must precede the fence that resolves the path.

    ``is_sensitive_path`` resolves: its contract is the fully symlink-resolved canonical
    target, following every link in the chain. On Windows, following a reparse point that
    names a share is the outbound SMB probe carrying an NTLM exchange, so a refusal computed
    from the resolved path arrives after the packet has left. The component walk judges each
    name by ``lstat`` and follows nothing, which is why it is the one that can run first.

    Order is OBSERVED, not re-derived: both are wrapped to append to one list, and the
    assertion reads that list. Checking the source text for which line comes first would pass
    on a file where the calls are unreachable.
    """
    mod = load_build()
    calls: list[str] = []

    real_chain = mod._refuse_redirects_in_chain

    def _chain(*args, **kwargs):
        calls.append("chain")
        return real_chain(*args, **kwargs)

    monkeypatch.setattr(mod, "_refuse_redirects_in_chain", _chain)

    sec = importlib.import_module("kiro_crew.security")
    real_fence = sec.is_sensitive_path

    def _fence(*args, **kwargs):
        calls.append("fence")
        return real_fence(*args, **kwargs)

    monkeypatch.setattr(sec, "is_sensitive_path", _fence)

    home = make_crew(tmp_path / "home")
    crew = mod.resolve_crew("frontdesk", home)
    mod.read_agent_spec(crew)

    assert "chain" in calls, "the component walk did not run at all"
    assert "fence" in calls, "the resolving fence did not run, so this proves nothing"
    assert calls.index("chain") < calls.index("fence"), (
        f"the resolving fence ran before the non-following walk: {calls}. Resolution is the "
        f"traversal, so an SMB probe would already have gone out."
    )


def test_the_marker_fallback_refuses_a_redirect_at_the_marker_path(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The branch without dir_fd must judge the marker path before opening it.

    The anchored branch opens with ``O_NOFOLLOW`` and answers False on ELOOP, so a redirect at
    the marker path is not this run's marker. The fallback branch has no anchoring, so the same
    verdict has to come from an ``lstat`` before the open. What the answer authorises is why it
    matters: a True here says this build owns the staging directory, and that permits the
    recursive delete of it.

    The link's target holds a VALID marker body, so following it would answer True. Without the
    guard the file at the far end of a planted link decides whether the delete is authorised.
    ``_dir_fd_supported`` is forced False to reach the fallback on a host that has dir_fd.
    """
    mod = load_build()
    monkeypatch.setattr(mod, "_dir_fd_supported", lambda: False)

    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text(mod._STAGING_MARKER_BODY, encoding="utf-8")
    marker = tmp_path / "marker.txt"
    marker.symlink_to(elsewhere)

    assert mod._marker_is_ours(elsewhere) is True, (
        "the fixture is wrong: the body written here is not one this build accepts, so the "
        "assertion below would pass whether or not the link was followed"
    )
    assert mod._marker_is_ours(marker) is False, (
        "a redirect at the marker path was read through to its target, so a planted link "
        "decides whether this build believes it may delete the staging tree"
    )


@_posix_only
def test_a_skill_swapped_for_a_link_after_review_is_refused_at_the_copy(
    tmp_path: pathlib.Path,
) -> None:
    """The swap that matters happens BETWEEN the review and the copy, so the test swaps there.

    A skill that is already a link at enumeration never becomes a candidate, so the copy loop
    never sees it -- measured: the candidate list comes back empty rather than blocked, which is
    its own gap and not this one. The reachable case is the race: reviewed as a real directory,
    replaced before the bytes are taken. ``is_dir()`` answers True for the link, so without an
    ``lstat`` the copy reads through it, and on Windows traversing a reparse point that names a
    share is the credential exchange rather than a wrong file.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True, exist_ok=True)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    assert [c.id for c in cands["skills"]] == [
        "faq"
    ], "the fixture must be reviewed as a real skill, or the copy loop never reaches it"
    plan_path = sign_plan(mod, crew, spec, out.parent, select={"skills": {"faq"}})
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)

    # The swap, after everything that reviewed it and before the bytes are taken.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# not the reviewed skill\n", encoding="utf-8")
    faq = home / "skills" / "faq"
    shutil.rmtree(faq)
    faq.symlink_to(outside, target_is_directory=True)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_bundle(crew, spec, cands, plan, out)
    # The REDIRECT must be what refuses, not the pin recheck below it. Measured: with the
    # lstat removed, the hash comparison still refuses ("changed while the bundle was being
    # written"), so asserting only that something refused cannot tell the two apart. What
    # separates them is what each can still protect: the recheck compares bytes AFTER the
    # copy has traversed the link, and a traversal that entered a reparse point naming a
    # share has already sent the SMB probe with its NTLM exchange. Wrong bytes are
    # recoverable; the credential exchange is not.
    message = str(caught.value)
    assert "link or a reparse point" in message, (
        f"the refusal came from something other than the redirect check: {message!r}. Anything "
        f"downstream of the traversal is too late to prevent the probe."
    )
    assert "faq" in message


# ---------------------------------------------------------------------------
# Round-21 platform guard: the builder is POSIX-only until an atomic no-follow
# primitive exists. The guard is feature-detected, so it lifts on its own when
# the primitive lands rather than needing a platform check kept in sync.
# ---------------------------------------------------------------------------
@_posix_only
def test_the_nofollow_guard_is_feature_detected_not_platform_detected(tmp_path: pathlib.Path):
    """The guard keys on the primitive being available, not on os.name.

    So when hooks grows a real no-follow handle and this builder adopts it,
    _nofollow_primitive_available() turns True and the guard lifts with no code change.
    """
    mod = load_build()
    # On this POSIX host the primitive is available, so a normal build is NOT refused.
    assert mod._nofollow_primitive_available() is True
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # must NOT raise on POSIX
    assert (out / "skills" / "faq" / "SKILL.md").is_file()


def test_MUTATION_dropping_the_entry_guard_stops_refusing_on_a_no_primitive_platform(
    tmp_path: pathlib.Path,
):
    """Remove the entry guard from read_agent_spec and a no-primitive platform stops refusing.

    With the guard, forcing the primitive unavailable makes read_agent_spec refuse POSIX-only
    before it touches the filesystem. With the guard mutated out, that POSIX-only refusal is
    gone -- proving the entry guard is what holds the reparse-following surface, not some other
    check downstream.
    """
    mod = load_build(
        mutate=(
            "def read_agent_spec(crew: ResolvedCrew) -> dict:\n    _refuse_without_nofollow_primitive()\n",
            "def read_agent_spec(crew: ResolvedCrew) -> dict:\n",
        )
    )
    mod._nofollow_primitive_available = lambda: False  # type: ignore[attr-defined]
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    crew = mod.resolve_crew("frontdesk", home)
    try:
        mod.read_agent_spec(crew)
    except mod.ExportRefused as exc:
        assert "POSIX-only" not in str(exc), (
            "guard removed: read_agent_spec must not refuse POSIX-only, proving the entry "
            "guard is the only thing that does"
        )
    except OSError:
        pass  # any other failure is fine; the point is no POSIX-only guard refusal


# ---------------------------------------------------------------------------
# Round-21 F3: an unreadable existing --out is refused via ExportRefused (not a
# raw PermissionError), so the ExportRefused-keyed staging cleanup runs and no
# staging tree or ownership marker leaks.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make --out unreadable")
def test_an_unreadable_out_is_refused_as_exportrefused_so_cleanup_runs(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build populates --out
    os.chmod(out, 0o000)
    try:
        with pytest.raises(mod.ExportRefused) as caught:
            _build(mod, home, out, {"skills": {"faq"}})
        assert "could not be listed" in str(caught.value)
        # The ExportRefused-keyed cleanup ran: no staging tree / marker left in the parent.
        leftovers = [
            q.name
            for q in out.parent.iterdir()
            if q.name.startswith(".smc-") or "staging" in q.name
        ]
        assert leftovers == [], f"staging/marker leaked past the refusal: {leftovers}"
    finally:
        os.chmod(out, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- restoring a test dir this test alone created from 0o000 to owner-only so tmp_path cleanup can traverse it; not a published artifact. lockdown-ok.  # noqa: E501  # fmt: skip


@_posix_only
def test_a_manifest_that_is_not_an_object_refuses_rather_than_crashing(
    tmp_path: pathlib.Path,
) -> None:
    """A manifest.json that parses but is not an object must refuse, not raise AttributeError.

    ``[]`` decodes without error and then ``.get`` fails, which the read's ``(OSError,
    ValueError)`` guard does not cover. Measured before the guard: the rebuild exited as a
    traceback, and it happens after the staging tree and its ownership marker exist, so the
    operator is left holding both with nothing naming either.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})
    (out / "manifest.json").write_text("[]", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "not an object" in str(caught.value)


def test_a_refused_marker_write_does_not_strand_the_staging_tree(
    tmp_path: pathlib.Path,
) -> None:
    """A refusal after the staging mkdir must not leave the tree that blocks every retry.

    The marker write refuses a pre-existing foreign or redirecting marker, and it runs after
    the mkdir. A stranded staging tree is then read by the pre-mkdir checks as another build's
    claim, so the first refusal turns into a permanent one with a different message until
    someone removes the directory by hand.

    Both halves are asserted: the refusal still happens, AND the tree is gone.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(str(out) + ".staging")
    marker = pathlib.Path(str(out) + ".staging.owned")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("not ours\n", encoding="utf-8")
    marker.symlink_to(elsewhere)

    with pytest.raises(mod.ExportRefused):
        _build(mod, home, out, {"skills": {"faq"}})
    assert not staging.exists(), (
        "the staging tree survived a refusal that happened after it was created, so every "
        "later build refuses on a claim this failure left behind"
    )


# ---------------------------------------------------------------------------
# Round-21 promotion/report ordering: the report is the proof an operator reads
# instead of checking the bundle, so it is published AFTER promotion, never on an
# assumed outcome. A failed promotion must leave no success report and keep the
# prior bundle in place.
# ---------------------------------------------------------------------------
def test_the_report_is_published_after_the_promotion_not_before() -> None:
    """Source order: staging.rename(out_dir) precedes the report publish.

    Writing the report before the promotion left a report claiming success when the promotion
    then failed -- a lie in the one artifact offered as evidence the bundle exists.
    """
    src = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    promote_at = src.index("staging.rename(out_dir)")
    report_at = src.index("_publish_report(report_tmp, report_path")
    assert 0 <= promote_at < report_at, (
        "the report is published before the promotion completes, so a failed promotion can "
        "leave a report that falsely claims the bundle exists"
    )


@pytest.mark.skipif(os.name != "posix", reason="drives the builder; POSIX-only per the guard")
def test_a_failed_promotion_writes_no_success_report_and_keeps_the_previous_bundle(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """If the promotion rename fails, no report claims success and the prior bundle stays."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build: a real bundle + report
    report = out.parent / (out.name + ".smc-bundle.json")
    first_report = report.read_bytes()
    first_digest = json.loads(first_report)["digest"]

    # Make the PROMOTION rename fail, after the report tmp is written and shape-checked.
    # Key on the SOURCE being the staging tree, so the rollback's previous->out restore
    # (also dst==out) is left to succeed -- otherwise the test would block its own recovery.
    real_rename = os.rename

    def _fail_promotion(src, dst, *a, **k):
        if str(src).endswith(".staging"):
            raise OSError("promotion blocked")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _fail_promotion)
    with pytest.raises((mod.ExportRefused, OSError)):
        _build(mod, home, out, {"skills": {"faq"}})

    # The previous bundle is still reachable and unchanged.
    assert (out / "skills" / "faq" / "SKILL.md").is_file(), "the prior bundle was left in place"
    # The report was not overwritten to claim the failed build: it still describes the first.
    assert report.read_bytes() == first_report, "no report claims the failed promotion"
    assert json.loads(report.read_bytes())["digest"] == first_digest


@_posix_only
def test_a_failed_report_publication_does_not_leave_the_previous_report_behind(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A report that cannot be published must be absent, never the previous build's.

    The ordering here promotes first and publishes after, and accepts a MISSING report as the
    cost, because a missing one is regenerated. On a rebuild the outcome without this cleanup is
    different in kind: the earlier build's report stays on disk and now describes a bundle that
    is gone. Measured before the cleanup -- the file was byte-identical to the first build's,
    digest included, while the second bundle was promoted.

    The publication failure is injected at ``os.link`` because nothing about a real
    filesystem makes the publish fail on demand. What is asserted is the state left behind.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    first = _build(mod, home, out, {"skills": {"faq"}})
    report = out.parent / f"{out.name}.smc-bundle.json"
    before = report.read_text(encoding="utf-8")
    assert first.digest in before, "the fixture must start from a report describing build one"

    (home / "skills" / "faq" / "SKILL.md").write_text("# FAQ v2\n", encoding="utf-8")
    real_link = mod.os.link

    def _link(src, dst, *args, **kwargs):
        if str(dst).endswith(".smc-bundle.json"):
            raise OSError(5, "Input/output error")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(mod.os, "link", _link)
    with pytest.raises(OSError):
        _build(mod, home, out, {"skills": {"faq"}})
    monkeypatch.undo()

    assert not report.exists(), (
        "the previous build's report survived a failed publication, so it now describes a "
        "bundle that is no longer there"
    )


# ---------------------------------------------------------------------------
# Round-23 GPT: the ownership verifier must check the ANCHOR (the root) itself,
# not only what is relative to it. A symlinked root would have its TARGET verified
# and then the recursive delete keyed to that verdict would run through the link.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_the_ownership_verifier_refuses_a_symlinked_root(tmp_path: pathlib.Path) -> None:
    mod = load_build()
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "out"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unless_this_build_wrote_it(link, "--out", "frontdesk")
    assert "symlink or reparse point" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_MUTATION_without_the_anchor_check_a_symlinked_root_is_verified_by_its_target(
    tmp_path: pathlib.Path,
) -> None:
    """Remove the anchor check and the verifier follows the link, judging the target instead.

    With the anchor check gone, a symlinked root is not refused up front; the verifier
    proceeds to ``d.exists()``/``iterdir()``, which follow the link and validate the TARGET --
    the exact "verified the wrong tree" the anchor check prevents. Proven because the refusal
    does not name a symlink (it gets past the anchor line to a downstream verdict instead).
    """
    mod = load_build(
        mutate=(
            "    if _is_redirecting_entry(d):\n        # The ANCHOR, before anything relative to it.",
            "    if False and _is_redirecting_entry(d):\n        # The ANCHOR, before anything relative to it.",
        )
    )
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "out"
    link.symlink_to(target, target_is_directory=True)
    # With the guard off, the symlinked root is NOT refused as a symlink; it follows into the
    # (empty, build-unowned) target and refuses for a different reason, or passes -- either way
    # not the anchor refusal, proving the anchor check is what catches the link.
    try:
        mod._refuse_unless_this_build_wrote_it(link, "--out", "frontdesk")
    except mod.ExportRefused as exc:
        assert "symlink or reparse point" not in str(exc), (
            "anchor check removed: the verifier followed the link to its target instead of "
            "refusing the root, proving the anchor check is load-bearing"
        )


# ---------------------------------------------------------------------------
# Round-24 GPT: an unreadable marker must not escape _marker_is_ours as a raw
# OSError -- the function's contract is a bool, and "cannot read it" answers no
# (not confirmably ours), which makes the caller refuse rather than delete.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make a marker unreadable")
def test_an_unreadable_marker_answers_not_ours_rather_than_crashing(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    marker = tmp_path / "bundle.staging.owned"
    marker.write_text("kiro-crew-bundle-staging-marker/1\n", encoding="utf-8")
    os.chmod(marker, 0o000)  # lockdown-ok: a test making its own tmp file unreadable to exercise EACCES; no payload, not published  # noqa: E501  # fmt: skip
    try:
        # No raise; an unreadable marker is not confirmably this run's, so the answer is False.
        assert mod._marker_is_ours(marker) is False
    finally:
        os.chmod(marker, 0o600)  # lockdown-ok: test permission restore of a tmp file this test created, not a publish  # noqa: E501  # fmt: skip


@pytest.mark.skipif(
    os.name != "posix", reason="uses chmod 000 to make an existing report unreadable"
)
def test_an_unreadable_report_refusal_releases_the_staging_tree_and_marker(
    tmp_path: pathlib.Path,
) -> None:
    """The refusal on an unreadable existing report must not leak the staging tree/marker.

    The marker is worse than the tree: the next run reads a stray marker as another build's
    claim and refuses on it, turning one refusal into a standing one.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build writes a report
    report = out.parent / (out.name + ".smc-bundle.json")
    os.chmod(report, 0o000)  # lockdown-ok: a test making its own tmp report unreadable to exercise EACCES; not published  # noqa: E501  # fmt: skip
    try:
        with pytest.raises(mod.ExportRefused) as caught:
            _build(mod, home, out, {"skills": {"faq"}})
        assert "cannot be read" in str(caught.value)
        leftovers = [
            q.name
            for q in out.parent.iterdir()
            if q.name.endswith(".staging") or q.name.endswith(".staging.owned")
        ]
        assert leftovers == [], f"the refusal leaked staging/marker: {leftovers}"
    finally:
        os.chmod(report, 0o644)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # lockdown-ok: test permission restore, not a publish  # noqa: E501  # fmt: skip


# ---------------------------------------------------------------------------
# Same object, different content: the fourth property. A concurrent process that
# edits the report IN PLACE leaves the same readable object with different bytes;
# a shape check alone says fine and the publish would supersede that edit. The build
# owns the report exclusively for one build, so drift is REFUSED, not overwritten.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_concurrent_in_place_report_edit_is_refused_not_overwritten(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build writes a legitimate report
    report = out.parent / (out.name + ".smc-bundle.json")
    original = report.read_bytes()

    # A foreign editor rewrites the report IN PLACE (same inode) during the second build,
    # landed at the report-path shape check that runs immediately before the pre-promotion
    # content guard -- so the bytes at report_path differ from what the build read at its start,
    # and the guard fires BEFORE anything destructive (no promotion, no rollback unlink).
    real_redirect = mod._is_redirecting_entry
    tampered = b'{"tampered":"by another process"}\n'
    report_calls = {"n": 0}

    def _edit_report_mid_build(path):
        if str(path).endswith(".smc-bundle.json"):
            report_calls["n"] += 1
            # The FIRST report-path check is the report_before capture guard; tamper only on
            # the SECOND (the pre-promotion shape check), after report_before is already read,
            # so the edit lands inside the write-then-read-back window the guard covers.
            if report_calls["n"] == 2:
                report.write_bytes(tampered)
        return real_redirect(path)

    monkeypatch.setattr(mod, "_is_redirecting_entry", _edit_report_mid_build)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "edited by another process" in str(caught.value)
    # The foreign edit survives: it was refused before any destructive step, not clobbered.
    assert report.read_bytes() == tampered
    assert report.read_bytes() != original


@_posix_only
def test_the_rollback_preserves_a_drifted_report_instead_of_unlinking_it(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The promoted-not-written rollback removes only the STALE report it owns.

    ``_publish_report`` refuses to overwrite a concurrent foreign in-place edit; the rollback
    that runs after must not then destroy that same foreign write on the way out. The delete is
    conditional on the report still matching ``report_before`` -- a drifted report is left in
    place. Distinguishes the branches: an unconditional unlink would pass the stale-report tests
    above and silently delete the foreign write here.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build leaves a report (report_before)
    report = out.parent / f"{out.name}.smc-bundle.json"
    foreign = b'{"foreign":"a concurrent editor rewrote this after the build started"}\n'

    # Let the pre-promotion content check pass (report unchanged then), promotion succeed, then
    # a concurrent editor rewrites the report IN PLACE and publish refuses the drift -- landing
    # in the promoted-not-written rollback with a report whose bytes != report_before.
    real_publish = mod._publish_report

    def _drift_then_refuse(report_tmp, report_path, report_before):
        report.write_bytes(foreign)
        return real_publish(report_tmp, report_path, report_before)

    monkeypatch.setattr(mod, "_publish_report", _drift_then_refuse)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "edited by another process" in str(caught.value)
    # The foreign write survives the rollback: it was preserved, not unlinked.
    assert report.read_bytes() == foreign


# ---------------------------------------------------------------------------
# Opus 4.8: the crew spec leaf agents/<name>.json is operator-named and must not
# be caught by the final-component credential-name rule -- the shared validator
# does not treat it as sensitive, so the standalone floor must not be stricter.
# ---------------------------------------------------------------------------
def test_the_floor_does_not_refuse_a_crew_named_like_a_credential() -> None:
    """A crew the operator named ``credentials`` (or ``client_secret``, ``service_account``)
    has a spec at ``agents/<name>.json`` whose basename matches the credential-name rule -- but
    it is the crew's own spec, not a credential file, and the shared validator returns False for
    it. The floor must agree, or a legitimately named crew can never be built in standalone mode.
    """
    mod = load_build()
    for name in ("credentials", "client_secret", "service_account", ".env"):
        spec = f"home/user/.kiro/agents/{name}.json"
        assert mod._looks_sensitive_standalone(spec) is False, spec


def test_the_floor_still_catches_credential_leaves_and_non_json_under_agents() -> None:
    """Non-vacuity: the exemption is narrow -- only agents/<name>.json.

    A real credential leaf elsewhere, and a non-``.json`` credential file even directly under
    ``agents``, are still caught, so the exemption did not open a hole.
    """
    mod = load_build()
    assert mod._looks_sensitive_standalone("home/user/.kiro/crew/.env") is True
    assert mod._looks_sensitive_standalone("home/user/project/secret.pem") is True
    # A .pem under agents is not the .json spec leaf, so the name rule still fires.
    assert mod._looks_sensitive_standalone("home/user/.kiro/agents/id_rsa.pem") is True


# ---------------------------------------------------------------------------
# GPT 5.6 / Opus 4.8: a settle (disposal) that RAISES must not let the finally
# recursively delete the verified tree in the private aside. On a rebuild that
# tree is the operator's current bundle; a concurrent nonempty <out>.previous
# makes the settling os.rename fail (ENOTEMPTY), which must NOT destroy it.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_raising_settle_restores_the_verified_tree_instead_of_deleting_it(
    tmp_path: pathlib.Path,
) -> None:
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("the operator's current bundle\n", encoding="utf-8")

    def _accept(parent_fd, moved_rel) -> None:
        return None  # verify passes: this is a build-written tree

    def _settle_that_fails(moved_rel: str, pfd: int) -> None:
        # Stand-in for os.rename(moved, previous) hitting a concurrent nonempty <out>.previous.
        raise OSError("settlement rename failed: destination not empty")

    # The settle failure re-raises after restoring the tree (the restore itself succeeds here),
    # so the original error surfaces -- what must NOT happen is a silent recursive delete.
    with pytest.raises(OSError) as caught:
        mod._dispose_via_private_aside(target, _accept, _settle_that_fails)
    assert "settlement rename failed" in str(caught.value)
    # The verified tree survives -- restored to its original path, never recursively deleted.
    assert target.is_dir()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "the operator's current bundle\n"


@_posix_only
def test_a_settle_failure_with_a_blocked_restore_retains_the_aside(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """If BOTH the settle and the restore-back fail, the tree is retained (aside kept), not deleted."""
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data\n", encoding="utf-8")

    real_rename = os.rename
    calls = {"n": 0}

    def _first_rename_ok_then_restore_fails(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:  # the restore rename (private -> target) fails
            raise OSError("restore blocked")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _first_rename_ok_then_restore_fails)

    def _accept(parent_fd, moved_rel) -> None:
        return None

    def _settle_that_fails(moved_rel: str, pfd: int) -> None:
        raise OSError("settlement failed")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._dispose_via_private_aside(target, _accept, _settle_that_fails)
    msg = str(caught.value)
    assert "NOT been deleted" in msg and "restoring it failed" in msg


# ---------------------------------------------------------------------------
# Round-GPT :5571 -- the publish side of the same inode rule the delete side earned.
# The promote rename re-derives the staging entry by NAME under the pinned parent, so a
# staging leaf swapped for another directory since staging_fd was opened would be promoted.
# Verify the name still resolves to the captured inode BEFORE the rename.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_swapped_staging_entry_is_refused_before_promotion(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A staging leaf whose inode does not match ``staging_fd`` is refused before promotion.

    The promote fstats the retained ``staging_fd`` and the freshly-reopened staging leaf and
    compares (st_dev, st_ino). Simulate the leaf having been swapped since capture by tampering
    the SECOND of that consecutive pair (the reopened leaf) so its inode differs. The check must
    refuse BEFORE the rename, leaving the previous bundle in place.
    """
    mod = load_build()
    _run_inode_mismatch_promotion(mod, tmp_path, monkeypatch, expect_refusal=True)


@_posix_only
def test_MUTATION_promotion_without_the_inode_check_ignores_a_swapped_staging(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Drop the pre-rename inode check and a mismatched staging inode is not refused.

    Proves the check is load-bearing: with it the tampered inode refuses; without it the same
    tampering promotes without complaint (the build completes rather than raising the inode
    refusal).
    """
    mod = load_build(
        mutate=(
            "            if staging_fd != -1:\n"
            "                try:\n"
            "                    check_fd = os.open(",
            "            if False:\n"
            "                try:\n"
            "                    check_fd = os.open(",
        )
    )
    _run_inode_mismatch_promotion(mod, tmp_path, monkeypatch, expect_refusal=False)


def _run_inode_mismatch_promotion(mod, tmp_path, monkeypatch, *, expect_refusal: bool) -> None:
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build: a real bundle
    assert (out / "skills" / "faq" / "SKILL.md").is_file()

    # ``os.open`` must NOT be patched (``_dir_fd_supported`` checks os.open membership in
    # os.supports_dir_fd, and a wrapper would fail that and route to the Windows path). Arm
    # deterministically instead: the pre-promote hard-link probe runs immediately before the
    # promote block, so wrap it to set a flag; then tamper the SECOND ``os.fstat`` after arming
    # -- the check does ``fstat(staging_fd)`` then ``fstat(check_fd)`` with nothing between, so
    # the second is the reopened leaf. Tampering its inode makes the leaf look swapped.
    real_fstat = os.fstat
    real_probe = mod._refuse_report_dir_without_hard_link_support
    state = {"armed": False, "count": 0, "tampered": False}

    class _Stat:
        def __init__(self, base, st_ino):
            self._base = base
            self.st_ino = st_ino

        def __getattr__(self, name):
            return getattr(self._base, name)

    def _armed_probe(report_path):
        real_probe(report_path)
        state["armed"] = True

    def _fstat(fd):
        st = real_fstat(fd)
        if state["armed"] and not state["tampered"]:
            state["count"] += 1
            if state["count"] == 2:
                state["tampered"] = True
                return _Stat(st, st.st_ino ^ 0xABCD)
        return st

    monkeypatch.setattr(mod, "_refuse_report_dir_without_hard_link_support", _armed_probe)
    monkeypatch.setattr(os, "fstat", _fstat)
    if expect_refusal:
        with pytest.raises(mod.ExportRefused) as caught:
            _build(mod, home, out, {"skills": {"faq"}})
        assert "inode changed" in str(caught.value)
        monkeypatch.undo()
        assert (out / "skills" / "faq" / "SKILL.md").read_text(encoding="utf-8") == "# FAQ\n"
    else:
        # Under the mutant the inode comparison is gone, so the tampering raises no inode
        # refusal -- the build either completes or fails for an unrelated reason, never the
        # inode message.
        raised = None
        try:
            _build(mod, home, out, {"skills": {"faq"}})
        except mod.ExportRefused as exc:  # pragma: no cover - defensive
            raised = str(exc)
        monkeypatch.undo()
        assert raised is None or "inode changed" not in raised


@_posix_only
def test_a_report_dir_without_hard_link_support_is_refused_before_promotion(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A report directory that cannot hard-link refuses BEFORE promotion, prior bundle intact.

    ``_publish_report`` installs by exclusive hard link, a filesystem capability. On a mount
    without it ``os.link`` raises EPERM/EOPNOTSUPP/ENOSYS, and because publish runs after
    ``promoted = True`` an unguarded failure would unwind a good promotion. The capability is
    probed before the irreversible rename; simulate an unsupported mount by making the probe
    link raise EOPNOTSUPP, and assert the build refuses with the previous bundle untouched.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})  # first build: a real bundle + report
    report = out.parent / (out.name + ".smc-bundle.json")
    first = report.read_bytes()

    real_link = os.link
    import errno as _errno

    def _no_hard_links(src, dst, *a, **k):
        # Fail the capability PROBE (its names carry the run id + linkprobe marker).
        if "linkprobe" in str(dst) or "linkprobe" in str(src):
            raise OSError(getattr(_errno, "EOPNOTSUPP", _errno.EPERM), "operation not supported")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", _no_hard_links)
    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "does not support hard links" in str(caught.value)
    monkeypatch.undo()
    # The promotion never ran: the first bundle and its report are exactly as they were.
    assert (out / "skills" / "faq" / "SKILL.md").read_text(encoding="utf-8") == "# FAQ\n"
    assert report.read_bytes() == first, "the prior report is untouched by a pre-promotion refusal"


@_posix_only
def test_MUTATION_no_capability_probe_crashes_raw_when_the_link_is_unsupported(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Remove the pre-promotion probe and an unsupported hard link crashes raw after promotion.

    Proves the probe is load-bearing. WITH the probe (the paired positive test), an
    unsupported-link filesystem is caught before promotion and refused cleanly with guidance,
    the prior bundle untouched. WITHOUT it, the capability failure surfaces from inside
    ``_publish_report`` -- after ``promoted = True`` -- as a raw ``OSError`` that is NOT the
    builder's clean ``ExportRefused``, leaving the operator a stack trace and a promoted bundle
    with no report instead of a recoverable refusal.
    """
    mod = load_build(
        mutate=(
            "        _refuse_report_dir_without_hard_link_support(report_path)\n",
            "        pass  # probe removed by mutation\n",
        )
    )
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    import errno as _errno

    real_link = os.link

    def _no_hard_links(src, dst, *a, **k):
        # Now that the probe is gone, only the REAL publish links (to the report leaf and its
        # aside) fire; fail them with an unsupported-capability errno.
        if ".smc-bundle.json" in str(dst):
            raise OSError(getattr(_errno, "EOPNOTSUPP", _errno.EPERM), "operation not supported")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", _no_hard_links)
    with pytest.raises(OSError) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    monkeypatch.undo()
    # The mutant leaks a RAW OSError, not the builder's clean ExportRefused with guidance.
    assert not isinstance(caught.value, mod.ExportRefused)
    assert "does not support hard links" not in str(caught.value)

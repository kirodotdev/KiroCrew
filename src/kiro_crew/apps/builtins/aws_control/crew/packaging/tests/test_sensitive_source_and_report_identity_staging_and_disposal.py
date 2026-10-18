"""The --out parent, the staging claim, the carried plan, and disposal through a private aside.

Covers the guard that judges the output parent before any path is derived from it, the staging
claim and the cleanup that runs when a build refuses after claiming it, the signed plan carried
into the new bundle, the report's atomic publish shape, and the private aside through which a
previous tree is verified and deleted -- or restored when the check fails.

Part of the sensitive-source suite whose first module is
``test_sensitive_source_and_report_identity.py``; ``_build`` and ``_posix_only`` come from
``sensitive_source_and_report_identity_helpers.py``.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from .sensitive_source_and_report_identity_helpers import _build, _posix_only
from .test_producer import builder_source_text, load_build, make_crew, transaction_source


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
    src = transaction_source()
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
    src = builder_source_text()
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
    src = transaction_source()
    publish_at = src.index("_publish_report(report_tmp, report_path")
    shape_at = src.index("_is_redirecting_entry(report_path)")
    assert 0 <= shape_at < publish_at, (
        "the destination's shape is not judged before the report is published, so a planted "
        "link at the report path is overwritten instead of refused"
    )


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
    src = builder_source_text()
    assert "staging.mkdir(parents=True)\n" in src, "the staging claim changed shape"
    assert (
        "staging.mkdir(parents=True, exist_ok=True)" not in src
    ), "exist_ok=True would let two builds share one staging tree"


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

"""Two authority-boundary properties of the builder.

``_tree_hash`` takes the content pin over the bytes that SHIP, so it must read each file
through the same authority the copy reads it through -- ``hooks.safe_read_file_bytes_nolink``,
which refuses a hard link (``st_nlink > 1``) the name checks cannot see. A ``read_bytes`` here
would pin the bytes of a hard-linked credential swapped in after the scan cleared the file.

The promotion renames staging onto ``out_dir`` relative to the parent pinned by descriptor,
not ``staging.rename(out_dir)``. A bare rename re-resolves both path strings, so a parent
component swapped for a link after ``--out`` was validated would land the promotion wherever
the link points; the descriptor-relative rename refuses a component swapped since.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from .test_producer import load_build, make_crew

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


# ---------------------------------------------------------------------------
# _tree_hash reads through the shared file-read guard, so a hard-linked file is
# refused at hashing rather than pinned through its second name.
# ---------------------------------------------------------------------------
@_posix_only
def test_tree_hash_refuses_a_hard_linked_file_and_names_it(tmp_path: pathlib.Path) -> None:
    """A skill member hard-linked to a file outside the skill is refused at hashing.

    The outside file's content is benign, so the refusal is the hard-link identity
    (``st_nlink > 1``) on the opened descriptor, not the credential scan. The refusal names
    the file so the pin cannot silently certify content the copy then refuses.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = src / "skills" / "leaky"
    outside = tmp_path / "outside_secret"
    outside.write_text("shared bytes that live outside the skill\n", encoding="utf-8")
    os.link(outside, skill_dir / "notes.md")
    assert (skill_dir / "notes.md").stat().st_nlink > 1, "test setup: member must be a hard link"

    with pytest.raises(mod.ExportRefused) as caught:
        mod._tree_hash(skill_dir)
    assert "notes.md" in str(caught.value), "the refusal must name the offending file"


@_posix_only
def test_tree_hash_hashes_an_ordinary_tree(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a tree of ordinary single-name regular files still hashes.

    The guard must not have become a blanket refusal -- a plain skill is read and pinned, so
    the hard-link refusal above is the hard link and not the read.
    """
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        skills={"faq": {"SKILL.md": "# faq\nhours 9 to 5\n", "extra.md": "no secrets\n"}},
    )
    digest = mod._tree_hash(src / "skills" / "faq")
    assert isinstance(digest, str) and len(digest) == 64, "an all-regular-file tree must hash"


@_posix_only
def test_MUTATION_a_by_name_read_pins_a_hard_linked_file_through(tmp_path: pathlib.Path) -> None:
    """Revert the guarded read to ``read_bytes`` and the hard-linked file is pinned, not refused.

    Reddens the fix: ``read_bytes`` never fstats for ``st_nlink``, so a hard link passes and
    the pin is taken over its bytes instead of refusing. The mutation anchor is the guarded
    read, unique to ``_tree_hash`` by its ``str(root)`` argument.
    """
    mod = load_build(
        mutate=(
            "safe_read_file_bytes_nolink(str(p), str(root), max_bytes=_MAX_PROMPT_BYTES)",
            "p.read_bytes()",
        )
    )
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = src / "skills" / "leaky"
    outside = tmp_path / "outside_secret"
    outside.write_text("shared bytes that live outside the skill\n", encoding="utf-8")
    os.link(outside, skill_dir / "notes.md")

    digest = mod._tree_hash(skill_dir)
    assert isinstance(digest, str) and len(digest) == 64, (
        "mutated: a by-name read with no st_nlink check pins the hard-linked file instead of "
        "refusing it, proving the guard's fstat is what refuses it"
    )


# ---------------------------------------------------------------------------
# The promotion renames staging onto out_dir relative to a pinned parent
# descriptor, and refuses a parent swapped for a link after validation.
# ---------------------------------------------------------------------------
def _build_at(mod, home: pathlib.Path, out: pathlib.Path):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    return mod.build_bundle(crew, spec, cands, None, out)


@_posix_only
def test_promotion_renames_staging_relative_to_a_pinned_parent_fd(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A clean build promotes via ``os.rename`` of the bare leaf names under a pinned fd.

    Non-vacuity for the pinned parent: the promotion of ``<name>.staging`` -> ``<name>``
    passes bare leaf names and ``src_dir_fd`` / ``dst_dir_fd``, which is only possible when
    the parent is opened as a descriptor first. A bare ``staging.rename(out_dir)`` would
    re-resolve full path strings instead and carries no descriptor.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    out = tmp_path / "work" / "bundle"

    calls: list[tuple] = []
    real_rename = os.rename

    def spy(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        calls.append((str(src), str(dst), src_dir_fd, dst_dir_fd))
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", spy)
    _build_at(mod, home, out)
    assert (out / "agent.json").is_file(), "the clean build must land the bundle"

    promote = [
        c
        for c in calls
        if c[0] == "bundle.staging" and c[1] == "bundle" and c[2] is not None and c[3] is not None
    ]
    assert promote, "the promotion did not rename staging->out_dir relative to a pinned parent fd"


@_posix_only
def test_MUTATION_a_bare_rename_promotion_is_not_descriptor_relative(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Revert to ``staging.rename(out_dir)`` and no descriptor-relative promotion happens.

    ``Path.rename`` re-resolves the full path strings, so the bare-leaf, ``dir_fd``-anchored
    promotion the fix records never appears -- proving the pinned ``os.rename`` is what makes
    the promotion descriptor-relative.
    """
    mod = load_build(
        mutate=(
            "            os.rename(\n"
            "                staging.name,\n"
            "                out_dir.name,\n"
            "                src_dir_fd=promote_parent_fd,\n"
            "                dst_dir_fd=promote_parent_fd,\n"
            "            )",
            "            staging.rename(out_dir)",
        )
    )
    home = make_crew(tmp_path / "home")
    out = tmp_path / "work" / "bundle"

    calls: list[tuple] = []
    real_rename = os.rename

    def spy(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        calls.append((str(src), str(dst), src_dir_fd, dst_dir_fd))
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", spy)
    _build_at(mod, home, out)
    assert (out / "agent.json").is_file(), "the mutated build still promotes (via Path.rename)"

    promote = [
        c
        for c in calls
        if c[0] == "bundle.staging" and c[1] == "bundle" and c[2] is not None and c[3] is not None
    ]
    assert not promote, (
        "mutated: a bare Path.rename promotion produced a descriptor-relative rename, which it "
        "cannot -- the pinned os.rename is what the fix adds"
    )


@_posix_only
def test_promotion_refuses_a_parent_swapped_after_validation(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A parent swapped for a link between validation and the rename is refused, not followed.

    The shared parent is swapped for a symlink to an attacker directory right before the
    promotion (at the report-path shape check, the last step before the rename). The parent
    was resolved once at validation, and the descriptor-relative promotion walks that value
    ``O_NOFOLLOW``, so the swapped component fails its own open and the build refuses. Nothing
    is promoted into the attacker directory.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    parent = tmp_path / "work"
    parent.mkdir()
    out = parent / "bundle"

    victim = tmp_path / "victim"
    victim.mkdir()
    # A decoy staging tree in the victim, so that if the promotion followed the swapped parent
    # (the bug) a bare rename would find a source and land ``victim/bundle``.
    (victim / "bundle.staging").mkdir()

    real_ire = mod._is_redirecting_entry
    state = {"swapped": False}

    def swap_before_promote(p):
        if str(p).endswith(".smc-bundle.json") and not state["swapped"]:
            state["swapped"] = True
            os.rename(parent, tmp_path / "real-work")
            parent.symlink_to(victim, target_is_directory=True)
        return real_ire(p)

    monkeypatch.setattr(mod, "_is_redirecting_entry", swap_before_promote)

    with pytest.raises(mod.ExportRefused):
        _build_at(mod, home, out)
    assert state["swapped"], "the swap never happened, so this proves nothing"
    assert not (victim / "bundle").exists(), "the promotion followed the swapped parent"


@_posix_only
def test_a_clean_parent_still_promotes(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: an untouched parent promotes the bundle, so the refusal above is the swap."""
    mod = load_build()
    home = make_crew(tmp_path / "home")
    out = tmp_path / "work" / "bundle"
    report = _build_at(mod, home, out)
    assert (out / "agent.json").is_file()
    assert (out / "manifest.json").is_file()
    assert report.bundle_dir == out


# ---------------------------------------------------------------------------
# The disposal path pins its parent by descriptor, so a parent swapped BETWEEN
# two disposal mutation points is refused by the pin, not followed onto an
# external tree by a re-resolved recursive delete.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_parent_swapped_between_two_disposal_points_refuses_by_the_pin(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Swap the shared parent between the leftover purge and the out_dir dispose; the pin refuses.

    A rebuild runs two disposal mutations in a row: it purges a leftover ``<out>.previous`` and
    then moves ``out_dir`` aside. The swap fires inside the FIRST one's ownership check, so its
    own held descriptor finishes safely; the SECOND opens a fresh pinned descriptor on the
    parent resolved at validation, and the swapped component fails its own ``O_NOFOLLOW`` open.
    The refusal is asserted BY THE DISPOSAL PIN'S OWN WORDING ("cannot dispose of"), not merely
    that something refused -- a downstream guard masking a late pin would use different words.
    Nothing is deleted inside the attacker directory.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    parent = tmp_path / "work"
    parent.mkdir()
    out = parent / "bundle"

    # A valid bundle at --out, and a build-owned leftover aside so the leftover purge runs.
    _build_at(mod, home, out)
    previous = parent / "bundle.previous"
    shutil.copytree(out, previous)

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "sentinel.txt").write_text("operator data outside --out\n", encoding="utf-8")

    real_check = mod._verify_build_wrote_captured_fd
    state = {"swapped": False}

    def swapping_check(parent_fd, moved_rel, flag, crew_name, *, label):
        real_check(parent_fd, moved_rel, flag, crew_name, label=label)
        # After the leftover aside clears its ownership check, swap the shared parent for a
        # link to the attacker directory -- i.e. between the two disposal mutation points.
        if flag == "the aside path" and not state["swapped"]:
            state["swapped"] = True
            os.rename(parent, tmp_path / "real-work")
            parent.symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(mod, "_verify_build_wrote_captured_fd", swapping_check)

    with pytest.raises(mod.ExportRefused) as caught:
        _build_at(mod, home, out)
    assert state["swapped"], "the swap never happened, so this proves nothing"
    assert "cannot dispose of" in str(caught.value), (
        "the refusal must come from the disposal pin's own message, not a downstream guard "
        f"that masks a pin landing too late: {caught.value}"
    )
    assert (victim / "sentinel.txt").read_text(encoding="utf-8") == (
        "operator data outside --out\n"
    ), "the disposal followed the swapped parent into the attacker directory"


@_posix_only
def test_MUTATION_bypassing_the_disposal_pin_lands_the_delete_on_a_swapped_parent(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Bypass the O_NOFOLLOW pin and a parent swap lands the recursive delete on the wrong tree.

    The pin is replaced by a plain open-by-name that follows symlinks (the pre-fix shape). With
    the parent swapped for a link to a victim directory after the resolve, the private-aside
    mkdir/rename/sweep all run inside the victim, and the recursive delete removes the victim's
    tree -- proving the descriptor pin is load-bearing. The ownership verifier is a no-op here
    so the pin is the only guard under test.
    """
    mod = load_build()
    parent = tmp_path / "work"
    parent.mkdir()
    target = parent / "bundle.previous"
    target.mkdir()
    (target / "keep.txt").write_text("real\n", encoding="utf-8")
    resolved_parent = parent.resolve()  # captured BEFORE the swap, as validation would

    victim = tmp_path / "victim"
    victim.mkdir()
    decoy = victim / "bundle.previous"
    decoy.mkdir()
    (decoy / "sentinel.txt").write_text("victim data\n", encoding="utf-8")

    def _unpinned_open(dir_path, *, already_resolved=False):
        # The pre-fix shape: open by NAME, following any link at a parent component.
        return os.open(str(dir_path), os.O_RDONLY | os.O_DIRECTORY)

    monkeypatch.setattr(mod, "_open_dir_nofollow_pinned", _unpinned_open)

    os.rename(parent, tmp_path / "real-work")
    parent.symlink_to(victim, target_is_directory=True)

    mod._purge_via_private_aside(
        target, lambda parent_fd, moved_rel: None, resolved_parent=resolved_parent
    )

    assert not (decoy / "sentinel.txt").exists(), (
        "with the pin bypassed the open followed the swapped parent to the victim and the "
        "recursive delete removed its tree -- proving the O_NOFOLLOW pin is what keeps the "
        "delete inside --out"
    )


# ---------------------------------------------------------------------------
# The transaction's recursive deletes are a CLOSED set, each bound to a pinned
# descriptor. Enumeration, not discovery: a name-based recursive delete of a
# tree derived from --out (staging / previous / the private aside) re-resolves
# its target and can be steered outside --out by a swap. Every such delete goes
# through _dispose_via_private_aside / _purge_via_private_aside / _rmtree_pinned,
# which reach the target relative to a held O_NOFOLLOW parent descriptor. This
# test fails if a NEW bare shutil.rmtree of an --out-derived tree is added.
# ---------------------------------------------------------------------------
def test_every_out_derived_recursive_delete_goes_through_the_pin() -> None:
    """No bare ``shutil.rmtree`` of staging / previous / the aside survives in ``build.py``.

    Every recursive delete of an --out-derived tree is reached through a held descriptor: the
    previous-bundle and aside disposals go through ``_dispose_via_private_aside`` /
    ``_purge_via_private_aside``, and the failure-path staging cleanup goes through
    ``_purge_staging_best_effort``, which verifies ownership and deletes through the pinned
    parent. The ONE ``shutil.rmtree`` this permits is the delete inside ``_rmtree_pinned``
    itself, the descriptor-relative primitive the pinned helpers call. Any OTHER bare
    ``shutil.rmtree`` -- even ``ignore_errors=True`` on staging or previous -- re-resolves its
    target by name, which a swap can steer outside --out, so it fails here and the closed set
    cannot silently grow.
    """
    import ast

    build_py = pathlib.Path(__file__).resolve().parents[1] / "build.py"
    tree = ast.parse(build_py.read_text(encoding="utf-8"), str(build_py))

    def _in_rmtree_pinned(node: ast.AST) -> bool:
        for fn in ast.walk(tree):
            if (
                isinstance(fn, ast.FunctionDef)
                and fn.name == "_rmtree_pinned"
                and any(n is node for n in ast.walk(fn))
            ):
                return True
        return False

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "rmtree":
            continue
        if _in_rmtree_pinned(node):
            continue  # the descriptor-relative primitive itself
        offenders.append(node.lineno)

    assert not offenders, (
        "these bare shutil.rmtree calls delete an --out-derived tree by a re-resolved name, "
        f"which a swap can steer outside --out (lines {offenders}); route each through the "
        "pinned aside (_purge_via_private_aside / _dispose_via_private_aside / "
        "_purge_staging_best_effort) so the target is reached relative to a held O_NOFOLLOW "
        "descriptor"
    )


def test_the_pin_rule_is_scanning_the_real_disposal_helpers() -> None:
    """Non-vacuity: the pinned helpers exist and the primitive is named as expected.

    A rule that scanned an empty set, or that named a helper absent from the module, would pass
    while the swap window it guards reopened. Assert the three names the rule relies on are
    real functions in the module.
    """
    import ast

    build_py = pathlib.Path(__file__).resolve().parents[1] / "build.py"
    tree = ast.parse(build_py.read_text(encoding="utf-8"), str(build_py))
    defined = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)}
    for name in ("_rmtree_pinned", "_dispose_via_private_aside", "_purge_via_private_aside"):
        assert name in defined, f"{name} is the pin the closed-set rule relies on; it is gone"


@_posix_only
def test_a_preexisting_staging_swapped_before_cleanup_is_not_deleted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staging cleanup is the third mutation point; a swap between check and delete refuses.

    A pre-existing staging tree clears the ownership check BY NAME, then the cleanup deletes it.
    With the pin bypassed, a parent swapped for a link between the two steps steers the delete
    onto an external tree. The pinned aside opens the parent ``O_NOFOLLOW`` and reaches staging
    relative to that held descriptor, so the swapped parent fails its own no-follow open and the
    external tree is NOT deleted.
    """
    mod = load_build()
    parent = tmp_path / "work"
    parent.mkdir()
    staging = parent / "b.staging"
    staging.mkdir()
    resolved_parent = parent.resolve()

    victim = tmp_path / "victim"
    victim.mkdir()
    decoy = victim / "b.staging"
    decoy.mkdir()
    (decoy / "sentinel.txt").write_text("victim data\n", encoding="utf-8")

    def _unpinned_open(dir_path, *, already_resolved=False):
        return os.open(str(dir_path), os.O_RDONLY | os.O_DIRECTORY)

    monkeypatch.setattr(mod, "_open_dir_nofollow_pinned", _unpinned_open)
    os.rename(parent, tmp_path / "real-work")
    parent.symlink_to(victim, target_is_directory=True)

    mod._purge_via_private_aside(
        staging, lambda parent_fd, moved_rel: None, resolved_parent=resolved_parent
    )

    assert not (decoy / "sentinel.txt").exists(), (
        "with the pin bypassed the staging cleanup opened the swapped parent (the victim) by "
        "name, moved the victim's own b.staging into the aside and swept it -- the delete "
        "landed OUTSIDE --out. This is the same defect as the previous-bundle and aside "
        "disposals, proving the staging delete must reach its target through the held "
        "O_NOFOLLOW parent descriptor, which refuses the swapped parent"
    )


# ---------------------------------------------------------------------------
# Finding 1: the moved-entry ownership verify reads the captured inode through
# the pinned parent, so a parent swapped in the capture-to-verify window cannot
# make it inspect a decoy while the sweep deletes the captured tree.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_parent_swap_between_capture_and_verify_judges_the_captured_inode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verify reads the captured inode through the pinned parent, not a re-resolved decoy.

    A parent component swapped for a link AFTER the rename captures the tree -- the window
    between capture and verify -- must not steer the ownership check to whatever the name
    resolves to now. The check runs through the descriptor opened before the swap, so it judges
    the operator tree the rename actually captured (refusing it by the pin's own wording, naming
    the stray file it holds) and restores it, and the tree outside --out the swapped link points
    at is never read or deleted.
    """
    mod = load_build()
    parent = tmp_path / "work"
    parent.mkdir()
    target = parent / "bundle.previous"
    target.mkdir()
    (target / "their-notes.txt").write_text("OPERATOR DATA\n", encoding="utf-8")
    resolved_parent = parent.resolve()

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "sentinel.txt").write_text("outside --out\n", encoding="utf-8")

    real_rename = os.rename
    state = {"swapped": False}

    def _swap_after_capture(src, dst, *a, **k):
        result = real_rename(src, dst, *a, **k)
        # The capture rename moves target into '<private>/bundle.previous'. Right after it, swap
        # the shared parent NAME for a link to the victim -- the capture-to-verify window.
        if not state["swapped"] and isinstance(dst, str) and dst.startswith(".smc-purge-"):
            state["swapped"] = True
            real_rename(parent, tmp_path / "real-work")
            parent.symlink_to(victim, target_is_directory=True)
        return result

    monkeypatch.setattr(os, "rename", _swap_after_capture)

    def _verify(parent_fd, moved_rel):
        mod._verify_build_wrote_captured_fd(
            parent_fd, moved_rel, "the aside path", "frontdesk", label=target
        )

    with pytest.raises(mod.ExportRefused) as caught:
        mod._purge_via_private_aside(target, _verify, resolved_parent=resolved_parent)

    assert state["swapped"], "the swap never happened, so this proves nothing"
    assert "their-notes.txt" in str(caught.value), (
        "the verify must judge the captured inode (which holds their-notes.txt), not the tree "
        f"the swapped parent link points at: {caught.value}"
    )
    assert (victim / "sentinel.txt").read_text(
        encoding="utf-8"
    ) == "outside --out\n", "the verify followed the swapped parent into the victim tree"


# ---------------------------------------------------------------------------
# Finding 2: the failure-path staging cleanup reaches staging through a pinned
# parent, so a swap leaves an external tree alone; bypassing the pin proves the
# descriptor is what contains the delete.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_parent_swap_before_the_failure_path_staging_cleanup_leaves_the_external_tree(
    tmp_path: pathlib.Path,
) -> None:
    """``_purge_staging_best_effort`` reaches staging through a pinned parent, so a swap is a no-op.

    The helper opens the parent ``O_NOFOLLOW`` at every component from the value resolved at
    validation. A parent swapped for a link since then fails that open, the best-effort cleanup
    swallows it, and the owned-looking tree the link points at is NOT deleted.
    """
    mod = load_build()
    parent = tmp_path / "work"
    parent.mkdir()
    staging = parent / "bundle.staging"
    staging.mkdir()
    (staging / "agent.json").write_text("{}\n", encoding="utf-8")
    resolved_parent = parent.resolve()

    victim = tmp_path / "victim"
    victim.mkdir()
    decoy = victim / "bundle.staging"
    decoy.mkdir()
    # Owned-looking, so ONLY the pin -- not the ownership check -- stands between the swap and a
    # delete of this external tree.
    (decoy / "agent.json").write_text("{}\n", encoding="utf-8")

    os.rename(parent, tmp_path / "real-work")
    parent.symlink_to(victim, target_is_directory=True)

    mod._purge_staging_best_effort(staging, resolved_parent)

    assert (decoy / "agent.json").exists(), (
        "the failure-path staging cleanup followed the swapped parent and deleted an external "
        "owned-looking tree"
    )


@_posix_only
def test_MUTATION_bypassing_the_pin_lets_the_staging_cleanup_delete_a_swapped_tree(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bypass the O_NOFOLLOW pin and the failure-path staging cleanup deletes the swapped tree.

    The pin is replaced by a plain open-by-name that follows a link at a parent component (the
    pre-fix shape). With the parent swapped for a link to an owned-looking external tree, the
    ownership check passes on it and the cleanup deletes it -- proving the descriptor pin is what
    keeps ``_purge_staging_best_effort`` inside --out and that the delete is load-bearing on it.
    """
    mod = load_build()
    parent = tmp_path / "work"
    parent.mkdir()
    staging = parent / "bundle.staging"
    staging.mkdir()
    (staging / "agent.json").write_text("{}\n", encoding="utf-8")
    resolved_parent = parent.resolve()

    victim = tmp_path / "victim"
    victim.mkdir()
    decoy = victim / "bundle.staging"
    decoy.mkdir()
    (decoy / "agent.json").write_text("{}\n", encoding="utf-8")

    def _unpinned_open(dir_path, *, already_resolved=False):
        return os.open(str(dir_path), os.O_RDONLY | os.O_DIRECTORY)

    monkeypatch.setattr(mod, "_open_dir_nofollow_pinned", _unpinned_open)
    os.rename(parent, tmp_path / "real-work")
    parent.symlink_to(victim, target_is_directory=True)

    mod._purge_staging_best_effort(staging, resolved_parent)

    assert not (decoy / "agent.json").exists(), (
        "with the pin bypassed the cleanup followed the swapped parent and deleted the external "
        "owned-looking tree, proving the O_NOFOLLOW pin is load-bearing on the staging cleanup"
    )


@_posix_only
def test_a_clean_rebuild_verifies_promotes_and_leaves_no_scratch(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a clean rebuild over an existing bundle still verifies, promotes and cleans.

    The rebuild path drives the fd ownership verify (its digest is re-derived through the pinned
    parent and must equal the bundle's recorded manifest digest), the previous-bundle purge and
    the staging cleanup on the happy path. It must succeed, leave the new bundle at --out, and
    leave no staging, previous or private-aside directory behind.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    out = tmp_path / "bundle"

    _build_at(mod, home, out)
    assert (out / "manifest.json").is_file()

    # Rebuild over the same --out: out_dir is verified as build-written through the descriptor
    # (a digest match against its manifest), moved aside, and the new bundle promoted.
    _build_at(mod, home, out)
    assert (out / "agent.json").is_file() and (out / "manifest.json").is_file()

    leftovers = sorted(
        q.name
        for q in out.parent.iterdir()
        if q.name.startswith("bundle.staging")
        or q.name == "bundle.previous"
        or q.name.startswith(".smc-purge-")
    )
    assert leftovers == [], f"a clean rebuild left scratch behind: {leftovers}"


# ---------------------------------------------------------------------------
# The failure-path ROLLBACK is the sixth step that names the directory. When a
# promotion fails after the previous bundle was moved to <out>.previous, the
# restore puts it back -- and a bare previous.rename(out_dir) re-resolves both
# path strings, so a parent swapped since validation would steer the restore.
# The restore runs descriptor-relative through the pinned out-parent fd.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_failed_promotion_restores_the_previous_bundle_descriptor_relative(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A promotion that fails after the aside is made restores the previous bundle by descriptor.

    Drive a first build (so a bundle exists), then a rebuild whose promotion ``os.rename`` is
    forced to fail: the previous bundle has already been moved to ``<out>.previous`` and
    ``promoted`` is still False, so the rollback runs. Assert the restoring rename is
    descriptor-relative (bare leaf names under ``src_dir_fd``/``dst_dir_fd``), not a re-resolved
    ``previous.rename(out_dir)``, and that the previous bundle is back at ``out_dir``.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    out = tmp_path / "work" / "bundle"
    _build_at(mod, home, out)
    assert (out / "agent.json").is_file(), "first build must land for there to be a previous"

    real_rename = os.rename
    calls: list[tuple] = []

    def spy(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        calls.append((str(src), str(dst), src_dir_fd, dst_dir_fd))
        # Fail the promotion itself (staging -> out_dir leaf) so the rollback runs, but let
        # every other rename (the aside move, and the restore we are testing) proceed.
        if str(src) == "bundle.staging" and str(dst) == "bundle":
            raise OSError("promotion forced to fail")
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", spy)
    with pytest.raises(Exception):
        _build_at(mod, home, out)

    assert (
        out / "agent.json"
    ).is_file(), "the previous bundle must be restored to out_dir after a failed promotion"
    restore = [
        c
        for c in calls
        if c[0] == "bundle.previous" and c[1] == "bundle" and c[2] is not None and c[3] is not None
    ]
    assert restore, (
        "the rollback restored the previous bundle by a re-resolved path rename, not a "
        "descriptor-relative one -- a swap could steer it outside --out"
    )


@_posix_only
def test_MUTATION_a_bare_rename_rollback_is_not_descriptor_relative(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Revert the rollback to ``previous.rename(out_dir)`` and no descriptor-relative restore runs.

    Reddens the fix: with the mutation the restore is a re-resolved ``Path.rename``, so the
    bare-leaf ``dir_fd``-anchored restore the fix records never appears, proving the pinned
    ``os.rename`` is what makes the rollback descriptor-relative.
    """
    mod = load_build(
        mutate=(
            "                            os.rename(\n"
            "                                previous.name,\n"
            "                                out_dir.name,\n"
            "                                src_dir_fd=restore_parent_fd,\n"
            "                                dst_dir_fd=restore_parent_fd,\n"
            "                            )",
            "                            previous.rename(out_dir)",
        )
    )
    home = make_crew(tmp_path / "home")
    out = tmp_path / "work" / "bundle"
    _build_at(mod, home, out)

    real_rename = os.rename
    calls: list[tuple] = []

    def spy(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        calls.append((str(src), str(dst), src_dir_fd, dst_dir_fd))
        if str(src) == "bundle.staging" and str(dst) == "bundle":
            raise OSError("promotion forced to fail")
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", spy)
    with pytest.raises(Exception):
        _build_at(mod, home, out)

    restore = [
        c
        for c in calls
        if c[0] == "bundle.previous" and c[1] == "bundle" and c[2] is not None and c[3] is not None
    ]
    assert not restore, (
        "mutated: a bare previous.rename(out_dir) produced a descriptor-relative restore, "
        "which it cannot -- the pinned os.rename is what the fix adds"
    )


# ---------------------------------------------------------------------------
# The staging WRITE is the seventh step that names the directory. A write to
# ``staging / "leaf"`` re-walks ``staging`` from its path string, so ``staging``
# swapped for another real directory between mkdir and the write lands the write
# there. The write goes through a descriptor RETAINED on the staging inode at
# creation, so the swap is defeated: the fd names the inode mkdir made.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_staging_write_through_the_retained_fd_ignores_a_swapped_staging(
    tmp_path: pathlib.Path,
) -> None:
    """A write via the staging descriptor lands in the created inode, not a decoy at the name.

    Create staging, open the retained no-follow descriptor on it, then rename staging away and
    put a decoy real directory at the same path. A write through the descriptor must reach the
    original inode (now renamed), NOT the decoy -- proving the write follows the pinned inode,
    not the re-resolved name. A symlink swap is refused by the by-name walk already; a real
    directory swapped in is the case only the retained descriptor closes.
    """
    mod = load_build()
    parent = tmp_path / "work"
    parent.mkdir()
    staging = parent / "bundle.staging"
    staging.mkdir()
    staging_fd = mod._open_dir_nofollow_pinned(staging)
    try:
        real = tmp_path / "real-staging"
        os.rename(staging, real)  # the inode the fd holds is now at ``real``
        decoy = parent / "bundle.staging"
        decoy.mkdir()  # a NEW real directory now sits at the staging path

        mod._write_bytes_nofollow(
            staging / "agent.json", b"{}\n", staging_fd=staging_fd, rel="agent.json"
        )
        assert (
            real / "agent.json"
        ).read_bytes() == b"{}\n", (
            "the write did not follow the retained descriptor to the original inode"
        )
        assert not (decoy / "agent.json").exists(), (
            "the write landed in the decoy swapped in at the staging name -- the retained "
            "descriptor is not being used"
        )
    finally:
        os.close(staging_fd)


@_posix_only
def test_MUTATION_a_by_name_staging_write_lands_in_the_swapped_decoy(
    tmp_path: pathlib.Path,
) -> None:
    """Bypass the retained fd (write by name) and the swapped decoy receives the write.

    Reddens the fix: with the descriptor ignored and the leaf re-resolved from ``staging``'s
    path, the decoy swapped in at that path receives the write -- proving the retained
    descriptor is what keeps a staging write on the inode this build created.
    """
    mod = load_build()
    parent = tmp_path / "work"
    parent.mkdir()
    staging = parent / "bundle.staging"
    staging.mkdir()
    staging_fd = mod._open_dir_nofollow_pinned(staging)
    try:
        real = tmp_path / "real-staging"
        os.rename(staging, real)
        decoy = parent / "bundle.staging"
        decoy.mkdir()

        # Bypass the retained-descriptor branch: write by name, which re-resolves ``staging``.
        mod._write_bytes_nofollow(staging / "agent.json", b"{}\n")
        assert (decoy / "agent.json").exists(), (
            "a by-name staging write did not land in the decoy -- the retained-descriptor "
            "branch is what the fix uses to avoid exactly this"
        )
    finally:
        os.close(staging_fd)


# ---------------------------------------------------------------------------
# The :2917 shape at the verification step. verify() now inspects the captured
# tree through the pinned descriptor, so it can raise an OSError from the walk,
# and a KeyboardInterrupt/SystemExit can arrive during it -- NOT only
# ExportRefused. ANY exception out of verify must restore the captured tree
# before it propagates, or the finally sweep takes the operator's bundle.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_non_export_refused_error_from_verify_restores_the_captured_tree(
    tmp_path: pathlib.Path,
) -> None:
    """An ``OSError`` (not ``ExportRefused``) out of verify leaves the tree restored, not deleted.

    The verify handler is ``except BaseException`` so a walk error, a cancellation, or any
    other exception restores the moved tree to where it came from before re-raising. Assert the
    target survives at its original path and the exception propagates.
    """
    mod = load_build()
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    (target / "keep.txt").parent.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data\n", encoding="utf-8")

    def _verify_raises_oserror(parent_fd: int, moved_rel: str) -> None:
        raise OSError("a walk error during verification, not an ExportRefused")

    with pytest.raises(OSError):
        mod._purge_via_private_aside(target, _verify_raises_oserror)

    assert (target / "keep.txt").read_text(encoding="utf-8") == "operator data\n", (
        "a non-ExportRefused exception out of verify deleted the captured tree -- the handler "
        "must be BaseException-broad and restore before re-raising (the :2917 shape)"
    )


@_posix_only
def test_MUTATION_a_narrow_verify_handler_deletes_the_captured_tree(
    tmp_path: pathlib.Path,
) -> None:
    """Narrow the verify handler back to ``except ExportRefused`` and an OSError deletes the tree.

    Reddens the fix: with the handler narrowed, an ``OSError`` out of verify skips the restore,
    the finally sweeps the private aside, and the captured tree is gone -- exactly the :2917
    destruction the BaseException handler prevents.
    """
    mod = load_build(
        mutate=(
            "            try:\n                verify(parent_fd, moved_rel)\n"
            "            except BaseException:",
            "            try:\n                verify(parent_fd, moved_rel)\n"
            "            except ExportRefused:",
        )
    )
    parent = tmp_path / "parent"
    target = parent / "bundle.previous"
    (target / "keep.txt").parent.mkdir(parents=True)
    (target / "keep.txt").write_text("operator data\n", encoding="utf-8")

    def _verify_raises_oserror(parent_fd: int, moved_rel: str) -> None:
        raise OSError("a walk error during verification")

    with pytest.raises(OSError):
        mod._purge_via_private_aside(target, _verify_raises_oserror)

    assert not (target / "keep.txt").exists(), (
        "with the narrow handler the OSError skipped the restore and the captured tree was "
        "swept -- proving the BaseException handler is what keeps the bundle recoverable"
    )


@_posix_only
def test_an_out_leaf_unlink_leaves_residue_when_the_parent_cannot_be_pinned(
    tmp_path: pathlib.Path,
) -> None:
    """Cleanup unlink of an --out-derived leaf leaves the file when the parent is not pinnable.

    Deny-by-default: when the validated parent cannot be opened ``O_NOFOLLOW`` (a component is a
    link), the unlink must NOT guess where the leaf now is and delete it -- it leaves the file.
    Simulate an unpinnable parent by pointing ``resolved_parent`` at a path whose parent is a
    symlink, and assert the marker survives.
    """
    mod = load_build()
    real = tmp_path / "real"
    real.mkdir()
    marker = real / "bundle.staging.owned"
    marker.write_text("ours\n", encoding="utf-8")
    # An unpinnable parent: a symlink stands in for ``real``, so the no-follow walk refuses it.
    link_parent = tmp_path / "via-link"
    link_parent.symlink_to(real, target_is_directory=True)
    unpinnable = link_parent  # resolved_parent whose own open O_NOFOLLOW fails at the link

    mod._unlink_out_leaf_best_effort(link_parent / "bundle.staging.owned", unpinnable)
    assert marker.exists(), (
        "the unlink deleted through an unpinnable (symlinked) parent -- it must leave residue "
        "rather than guess where the leaf is"
    )

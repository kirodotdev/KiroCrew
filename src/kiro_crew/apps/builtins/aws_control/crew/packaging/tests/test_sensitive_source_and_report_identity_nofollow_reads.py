"""Every read the build takes refuses a link, a redirect or a hard link instead of following it.

Covers the agent-spec and plan reads, the skill walk and the copy that follows review, the
anchored leaf opener and the descriptors it hands back, the Windows fallback that has no
``dir_fd``, and the POSIX-only entry guard that refuses a platform without an atomic no-follow
primitive.

Part of the sensitive-source suite whose first module is
``test_sensitive_source_and_report_identity.py``; ``_build`` and ``_posix_only`` come from
``sensitive_source_and_report_identity_helpers.py``.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import shutil

import pytest

from .sensitive_source_and_report_identity_helpers import _build, _posix_only
from .test_producer import load_build, make_crew, patch_builder_global, sign_plan


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
            # The reader borrows its descriptor from the shared no-reparse opener, so the
            # mutation swaps that borrow for a plain following open. The mutated property is
            # unchanged: without a no-follow open the link is read through to its target.
            "        fd = open_file_no_reparse(path, nonblocking=True)",
            "        fd = os.open(path, os.O_RDONLY)",
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
            "(p for p in _pinned._walk_no_reparse(skill_dir) if _pinned._is_redirecting_entry(p)),",
            "(p for p in _pinned._walk_no_reparse(skill_dir) if False),",
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

    # pinned_fs is a facade over owner modules; its text is the facade's and theirs.
    home = pathlib.Path(pinned_fs.__file__).parent
    src = "\n".join(
        (home / f"{name.rpartition('.')[2]}.py").read_text(encoding="utf-8")
        for name in ("kiro_crew.pinned_fs", *pinned_fs._OWNERS)
    )
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
            "    text = _pinned._read_text_openat(\n"
            "        Path(abs_path.anchor), abs_path.relative_to(abs_path.anchor), "
            "refuse_hard_link=True\n    )",
            "    text = _pinned._read_text_nofollow(path)",
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


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_the_nofollow_reader_refuses_a_redirect_without_a_path_check(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The redirect refusal is the OPEN's, so no by-name verdict is taken before the read.

    A verdict taken on the path and then acted on by a separate open is a window a concurrent
    writer wins, and on a platform whose junction can name a UNC share the read at the far end
    of that window is an outbound SMB/NTLM exchange. The reader borrows
    ``platform_compat.open_file_no_reparse``, which refuses the redirect in the operation that
    opens the name, so the path predicate must not be consulted at all -- forced to raise here,
    which turns any surviving pre-check into a failure rather than a silent pass.
    """
    mod = load_build()

    def _must_not_run(_probe):
        raise AssertionError("the reader must not judge the path before opening it")

    monkeypatch.setattr(mod, "_is_redirecting_entry", _must_not_run)
    real = tmp_path / "real.txt"
    real.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "spec.txt"
    os.symlink(real, link)
    assert (
        mod._read_text_nofollow(link) is None
    ), "a redirect at the read path must be refused by the open, not read through"
    assert mod._read_text_nofollow(real) == "secret\n", "an ordinary file still reads"


@pytest.mark.skipif(os.name != "posix", reason="uses a symlink to stand in for a junction")
def test_MUTATION_without_the_shared_opener_the_reader_stops_rather_than_approximating(
    tmp_path: pathlib.Path,
) -> None:
    """Make the shared module unimportable and the reader refuses instead of reading.

    Fail-closed is the direction that matters: a local approximation of the refusal is the
    check-then-open window itself, so an environment without the shared opener gets no read.
    """
    mod = load_build(
        mutate=(
            "        from kiro_crew.platform_compat import open_file_no_reparse",
            "        raise ImportError('simulated standalone environment')",
        )
    )
    real = tmp_path / "real.txt"
    real.write_text("secret\n", encoding="utf-8")
    assert mod._read_text_nofollow(real) is None, (
        "without the shared opener the reader must refuse; reading anyway would mean it fell "
        "back to an open with no no-follow guarantee"
    )


@pytest.mark.skipif(os.name != "posix", reason="a POSIX O_RDONLY open of a directory succeeds")
def test_the_leaf_opener_hands_back_no_directory_descriptor(tmp_path: pathlib.Path) -> None:
    """A directory at a read path yields None, not a descriptor its callers cannot wrap.

    The two platforms disagree about where a directory surfaces. The shared opener's Windows
    branch reads the directory attribute off the handle and raises; a POSIX ``O_RDONLY`` open
    of a directory succeeds and yields a usable descriptor. Deciding it in the opener is what
    makes the two answers the same, and what keeps the leak below out of reach.
    """
    mod = load_build()
    a_dir = tmp_path / "a_dir"
    a_dir.mkdir()
    assert (
        mod._open_leaf_no_reparse(a_dir) is None
    ), "a directory is not a leaf read; the opener must refuse it rather than pass the fd on"

    a_file = tmp_path / "a_file.txt"
    a_file.write_text("body\n", encoding="utf-8")
    fd = mod._open_leaf_no_reparse(a_file)
    assert fd is not None, "CONTROL: an ordinary file must still open, or this pins nothing"
    os.close(fd)


def _descriptor_ledger(mod, monkeypatch):
    """Track every leaf descriptor the build module opens, without moving it to another branch.

    All three leaf opens hand their descriptor to ``_dir_fd_closed`` before any reader touches
    it, so wrapping that one authority sees all three; the wrapper calls straight through, so
    the verdict stays the module's own. ``os.close`` and ``os.fdopen`` are wrapped beside it
    because a descriptor is accounted for either by a close or by a successful ``fdopen``,
    which TAKES OWNERSHIP and closes through the file object instead of through ``os.close``.

    ``os.open`` is deliberately NOT wrapped. ``_dir_fd_supported`` asks
    ``os.open in os.supports_dir_fd``, and a wrapper is not a member of that set, so wrapping
    it answers False and routes every reader down the by-name fallback -- a spy that picks the
    branch it is supposed to be watching, and whose leaf open then happens inside
    ``platform_compat`` where this ledger cannot see it at all.

    A close of a descriptor the ledger is not holding (a parent directory fd from the
    anchoring walk) is ignored rather than subtracted, so a reissued number cannot cancel a
    real strand recorded under the same number.
    """
    real_os = mod.os
    real_verdict = mod._dir_fd_closed
    outstanding: dict[int, int] = {}

    def release(fd: int) -> None:
        if outstanding.get(fd):
            outstanding[fd] -= 1
            if not outstanding[fd]:
                del outstanding[fd]

    class _LedgerOs:
        def close(self, fd):
            release(fd)
            return real_os.close(fd)

        def fdopen(self, fd, *args, **kwargs):
            fh = real_os.fdopen(fd, *args, **kwargs)
            if kwargs.get("closefd", True):
                release(fd)
            return fh

        def __getattr__(self, name):
            return getattr(real_os, name)

    def ledger_verdict(fd: int) -> bool:
        outstanding[fd] = outstanding.get(fd, 0) + 1
        return real_verdict(fd)

    patch_builder_global(monkeypatch, mod, "os", _LedgerOs())
    monkeypatch.setattr(mod, "_dir_fd_closed", ledger_verdict)
    return outstanding


def _stranded(outstanding: dict) -> int:
    """Leaf descriptors still held: opened, then neither closed nor handed to an owner."""
    return sum(outstanding.values())


def _release_held(outstanding: dict) -> None:
    """Close whatever the ledger is still holding, so a FAILING assertion strands nothing.

    Belongs in a ``finally``: the descriptors exist by the time the reads are driven, and an
    assertion that fires before the closing loop would leave them open for the remainder of
    the worker process -- the same defect these tests are about, committed by the test.
    """
    for fd, count in list(outstanding.items()):
        for _ in range(count):
            try:
                os.close(fd)
            except OSError:
                break
        outstanding.pop(fd, None)


@_posix_only
def test_a_directory_at_a_read_path_strands_no_descriptor(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Reading a directory path leaves every descriptor the module opened accounted for.

    ``os.fdopen`` raises ``IsADirectoryError`` BEFORE the wrapper it would return owns the
    fd, so a reader written as ``with os.fdopen(fd, ...)`` never reaches a close for it. Both
    opener paths are exercised: the anchored walk, which is the one that runs on a platform
    with ``dir_fd``, and the by-name fallback the Windows branch would take.
    """
    root = tmp_path / "root"
    (root / "a_dir").mkdir(parents=True)
    marker_dir = tmp_path / "marker_dir"
    marker_dir.mkdir()
    rounds = 8

    mod = load_build()
    held = _descriptor_ledger(mod, monkeypatch)
    try:
        for _ in range(rounds):
            assert (
                mod._read_bytes_openat(root, pathlib.Path("a_dir")) is None
            ), "a directory is not a readable member; the anchored reader must answer None"
            assert (
                mod._marker_is_ours(marker_dir) is False
            ), "a directory at the marker path is not a marker this run wrote"
        if mod._dir_fd_supported():
            # The fourth leaf open: a read relative to a descriptor the caller already holds.
            # The held fd pins the directory, not the name inside it, so this is the same
            # window.
            dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for _ in range(rounds):
                    assert (
                        mod._read_regular_leaf_fd(dir_fd, "a_dir") is None
                    ), "a directory under a held descriptor is not a regular leaf to read"
            finally:
                os.close(dir_fd)
        anchored = _stranded(held)
        assert anchored == 0, (
            f"the anchored readers stranded {anchored} descriptor(s) over {rounds} rounds: "
            "the fd os.fdopen refused to wrap reaches no close"
        )
    finally:
        _release_held(held)

    mod = load_build()
    monkeypatch.setattr(mod, "_dir_fd_supported", lambda: False)
    held = _descriptor_ledger(mod, monkeypatch)
    try:
        for _ in range(rounds):
            assert (
                mod._read_bytes_openat(root, pathlib.Path("a_dir")) is None
            ), "the by-name fallback must answer None for a directory too"
            assert (
                mod._marker_is_ours(marker_dir) is False
            ), "the marker fallback must answer False for a directory too"
        fallback = _stranded(held)
        assert (
            fallback == 0
        ), f"the fallback readers stranded {fallback} descriptor(s) over {rounds} rounds"
    finally:
        _release_held(held)


@_posix_only
def test_MUTATION_without_the_directory_verdict_each_read_strands_a_descriptor(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Report every descriptor as a file and the strand count rises by one per read.

    This is what proves the test above discriminates: with the verdict reversed every
    reader's ANSWER is still correct, and the descriptor that reaches no close is the only
    observable difference. One anchor covers all three leaf opens because they share one
    authority -- which is the reason the verdict was factored into one.
    """
    mod = load_build(
        mutate=("        if not stat.S_ISDIR(os.fstat(fd).st_mode):", "        if True:")
    )
    root = tmp_path / "root"
    (root / "a_dir").mkdir(parents=True)
    rounds = 8

    held = _descriptor_ledger(mod, monkeypatch)
    try:
        for _ in range(rounds):
            assert mod._read_bytes_openat(root, pathlib.Path("a_dir")) is None, (
                "the mutation leaves the verdict alone -- which is the point: the answer looks "
                "right while the descriptor is stranded"
            )
        stranded = _stranded(held)
        assert stranded == rounds, (
            f"expected one stranded descriptor per read ({rounds}), counted {stranded}; a 0 "
            "here means the mutation changed nothing and the test above is proving nothing"
        )
    finally:
        _release_held(held)


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
    """The branch without dir_fd must let the OPEN decide the marker verdict.

    The anchored branch opens with ``O_NOFOLLOW`` and answers False on ELOOP, so a redirect at
    the marker path is not this run's marker. The fallback branch has no anchoring, so the same
    verdict has to come from the open itself. What the answer authorises is why it
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
            "def read_agent_spec(crew: ResolvedCrew) -> dict:\n    _pinned._refuse_without_nofollow_primitive()\n",
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

"""Release-channel resolution for Dev Fleet's per-lane worktrees.

The defect these tests exist to prevent is a SILENT one: a lane that resolves to
the wrong ref still produces a worktree that builds, boots as a pod and serves a
dashboard, so "stable" showing a prerelease looks exactly like success. Every
assertion below is therefore about the resolver's *answer*, not about whether it
ran.

Tag fixtures use this repository's real tag vocabulary (``v0.5.0``,
``v0.6.0-insider.6``) so a rename of the release workflow's tag shape shows up
here rather than in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import release_channel_pin as rcp
from kiro_crew.apps.builtins.dev_fleet import repository, runtime

# Newest first, which is what `--sort=-creatordate` gives the resolver. The
# interleaving is the point: insider's tip is NEWER than stable's tip, so a
# resolver that ignored the lane filter and simply took the first line would
# return an insider tag for stable — and that is the real shape of this repo's
# tag history, not a contrived case.
_TAGS_NEWEST_FIRST = [
    "v0.6.0-insider.6",
    "v0.6.0-insider.5",
    "v0.5.0",
    "v0.5.0-insider.11",
    "v0.4.1",
]


def _fake_git(tags: list[str] | None = None, *, oids: dict[str, str] | None = None):
    """A git stand-in answering only what the resolver asks."""
    tags = _TAGS_NEWEST_FIRST if tags is None else tags
    oids = oids or {}

    async def fake_run(cmd, **kw):
        if "tag" in cmd and "--list" in cmd:
            return 0, "\n".join(tags) + "\n", ""
        if "rev-parse" in cmd:
            target = cmd[-1]
            if target in oids:
                return 0, oids[target] + "\n", ""
            # Deterministic stand-in oid derived from the ref, so assertions can
            # tie a returned oid back to the ref it was resolved from.
            return 0, f"oid-{target}\n", ""
        return 1, "", f"unexpected argv: {cmd}"

    return fake_run


@pytest.fixture(autouse=True)
def _pinned_repo(monkeypatch):
    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------
def test_the_worktree_name_carries_the_channel_under_the_shared_prefix():
    """One naming rule, on the backend only.

    The fleet payload publishes this string, so the frontend never rebuilds it —
    a second copy of the prefix rule is what would let a change to
    ``WORKTREE_PREFIX`` desync a row's label from the directory it names.
    """
    assert rcp.WORKTREE_NAME == f"{rcp.WORKTREE_PREFIX}{rcp.CHANNEL}"
    assert rcp.WORKTREE_NAME.endswith(rcp.CHANNEL)


def test_worktree_name_is_a_valid_pod_identity():
    """The basename becomes ``kirocrew-pod@<name>.service``.

    A name that fails the pod name rule would surface as a pod that cannot be
    brought up — long after the worktree was created and built.
    """
    from kiro_crew.pod.runtime import _NAME_RE

    assert _NAME_RE.match(rcp.WORKTREE_NAME), rcp.WORKTREE_NAME


def test_worktree_path_is_a_sibling_of_the_primary_checkout():
    # Compared as PATHS, not as strings. ``worktree_path`` returns a native path,
    # so a POSIX string literal here asserted the separator rather than the
    # placement and failed on Windows for a correct return value. What the name
    # of this test actually claims is sibling-ness, so pin that instead.
    repo = Path("/Users/me/Projects/KiroCrew")
    got = Path(rcp.worktree_path(str(repo)))
    assert got == repo.parent / "release-channel-stable"
    assert got.parent == repo.parent
    assert got.name == rcp.WORKTREE_NAME


# --------------------------------------------------------------------------
# tag classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tag", ["v0.5.0", "v0.4.9", "v1.0.0", "v0.10.0", "v12.3.45"])
def test_a_release_tag_is_decided_by_shape_and_never_disagrees_with_the_shared_rule(tag):
    """The regex IS the classification here, and it answers what the repo answers.

    ``is_release_tag`` does not call ``release_channel.channel``: a bare ``vX.Y.Z``
    has no suffix left to read, so the call could only agree. What still has to
    hold is that the two never DIVERGE — a release-shaped tag this module admits
    must be one the shared rule also calls stable, or the worktree would pin to a
    build the rest of the product does not treat as a release.
    """
    from kiro_crew import release_channel

    assert rcp.is_release_tag(tag) is True
    assert release_channel.channel(tag[1:]) == rcp.CHANNEL


@pytest.mark.parametrize(
    "tag",
    ["main", "v1.2", "release-0.5.0", "v0.5.0.1", "", "v0.6.0-insider.6", "v0.6.0-rc.2"],
)
def test_a_non_release_tag_is_rejected_at_the_shape_check(tag):
    """A prerelease tag is not a release tag here, because no channel answers as one.

    ``stable`` is the only channel, so a prerelease could only ever be classified
    and then discarded. Rejecting it at the shape check keeps one path instead of
    two that agree.
    """
    assert rcp.is_release_tag(tag) is False


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_stable_skips_newer_prerelease_tags(monkeypatch):
    """The core discriminator: a prerelease tip is NEWER, stable must not take it.

    The prerelease tags stay in the fixture even though no lane resolves to them —
    they are in this repo's real tag history, so "the newest tag" and "the newest
    stable tag" are genuinely different commits and a resolver that dropped the
    lane filter would look successful while pinning a prerelease.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve()
    assert got["ok"] is True
    assert got["tag"] == "v0.5.0"
    assert got["ref"] == "refs/tags/v0.5.0"
    assert got["version"] == "0.5.0"


@pytest.mark.asyncio
async def test_a_prerelease_tag_never_becomes_the_tip_even_when_it_is_newest(monkeypatch):
    """An ``insider`` tag classifies as a channel elsewhere and still cannot win here.

    ``release_channel.channel`` calls ``-insider.N`` tags insider, and this module
    does not get a second opinion — it simply does not admit them, because ranking
    ``-insider.N`` against ``-rc.N`` needs a precedence nothing in this repo states.
    With no channel parameter there is nothing to refuse: the shape check is the
    whole gate, so a newer prerelease is passed over rather than rejected.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.6.0-insider.6", "v0.5.0"]))
    got = await rcp.resolve()
    assert got["ok"] is True
    assert got["tag"] == "v0.5.0"


@pytest.mark.asyncio
async def test_resolve_stable_orders_by_version_not_by_tag_date(monkeypatch):
    """A backport cut AFTER a newer line must not become the stable tip.

    ``v0.4.2`` tagged after ``v0.5.0`` is ordinary release practice (a patch on an
    older line), and it is what breaks date ordering: it is the newest tag by date
    and an older release by version. A re-pushed or re-created tag does the same
    thing — it carries today's date for last year's release. Stable users are on
    ``v0.5.0``, so that is what the stable row must pin.
    """
    monkeypatch.setattr(
        runtime,
        "_run_cmd",
        _fake_git(["v0.4.2", "v0.5.0", "v0.4.1"]),  # newest-first BY DATE
    )
    got = await rcp.resolve()
    assert got["tag"] == "v0.5.0"


@pytest.mark.asyncio
async def test_resolve_stable_compares_version_parts_numerically(monkeypatch):
    """``v0.10.0`` beats ``v0.9.0`` — a string sort would get this backwards."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.9.0", "v0.10.0"]))
    got = await rcp.resolve()
    assert got["tag"] == "v0.10.0"


def test_release_candidates_is_deterministic_under_a_reordered_listing():
    """Same tags, different listing order → same stable answer.

    The version sort has to be total for the row to stop flickering between two
    tags as git's date ordering shifts under a re-fetch.
    """
    tags = ["v0.4.2", "v0.5.0", "v0.4.1"]
    first = rcp._release_candidates(tags)
    assert first == rcp._release_candidates(list(reversed(tags)))
    assert first[0] == "v0.5.0"


def test_the_channel_is_a_real_release_channel_and_the_others_are_out():
    """The channel must name a PUBLISHED build whose order among its tags is a fact.

    ``nightly.yml`` builds from ``main`` HEAD and tags nothing, so the newest ref a
    nightly row could name is ``<remote>/main`` — main *now*, not what nightly
    shipped, and where the primary checkout already sits after Sync. A row
    promising the former while showing the latter is worse than no row.

    ``insider`` fails a different half: its tags resolve, but ranking
    ``-insider.N`` against ``-rc.N`` needs a precedence nothing in this repo
    states. Both are excluded, for reasons that are not the same reason, and
    neither is reachable — there is no channel parameter to pass one through.
    """
    from kiro_crew.platform.update_layout import RELEASE_CHANNELS

    assert {"nightly", "insider"} <= set(RELEASE_CHANNELS)
    assert rcp.CHANNEL in RELEASE_CHANNELS
    assert rcp.CHANNEL == "stable"


@pytest.mark.asyncio
async def test_resolve_reports_oid_of_the_tagged_commit(monkeypatch):
    """Resolution must peel to a commit.

    An annotated tag's own object is not a commit, and handing a tag object to
    ``git worktree add`` / ``checkout --detach`` puts the worktree somewhere the
    behind-count cannot be computed from.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve()
    assert got["oid"] == "oid-refs/tags/v0.5.0^{commit}"


@pytest.mark.asyncio
async def test_resolve_reports_an_empty_channel_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.6.0-insider.6"]))
    got = await rcp.resolve()
    assert got["ok"] is False
    assert "no stable release tag" in got["error"]


@pytest.mark.asyncio
async def test_resolve_skips_a_tag_that_will_not_resolve(monkeypatch):
    """One broken local ref must not report the whole lane as unpublished."""

    async def fake_run(cmd, **kw):
        if "tag" in cmd and "--list" in cmd:
            return 0, "v0.6.0\nv0.5.0\n", ""
        if "rev-parse" in cmd:
            if "v0.6.0^{commit}" in cmd[-1]:
                return 1, "", "bad object"
            return 0, f"oid-{cmd[-1]}\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve()
    assert got["ok"] is True
    assert got["tag"] == "v0.5.0"


@pytest.mark.asyncio
async def test_resolve_does_not_re_decide_the_tag_it_already_picked(monkeypatch):
    """There is no second classification to re-check, and that is deliberate.

    A ``lane_check`` field claiming the resolver verified its own answer cannot do
    that: selection already filtered on ``is_release_tag(tag)``, and ``version`` is
    that same ``tag[1:]`` — so the comparison is a tautology that only ever agrees,
    and a test which "proves" it fires has to stub BOTH sides to produce a
    disagreement. A guard asserted against a stub of itself is not a guard.

    What holds instead: the decision is made exactly once, by the tag's shape, and
    the result carries no field re-stating it.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.5.0"]))
    got = await rcp.resolve()
    assert got["ok"] is True
    assert "lane_check" not in got
    assert got["version"] == "0.5.0"
    assert rcp.is_release_tag("v0.5.0") is True


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_refs_is_additive_and_never_prunes_tags(monkeypatch):
    """The fetch must not be able to delete a tag the operator authored.

    Measured on git 2.54: the only fetch forms that drop a remotely-deleted tag
    (``--prune --prune-tags`` with no ``--tags``, or an explicit
    ``+refs/tags/*:refs/tags/*`` under ``--prune``) delete every local-only tag
    with it, and a pruned tag ref is in no reflog. So this asserts the ABSENCE of
    both pruning flags: the cost of retraction coverage by this route is the
    operator's own tags, which is the worse trade.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs("/fake/repo") is None
    assert seen and "--tags" in seen[0]
    assert "--prune-tags" not in seen[0]
    assert "--prune" not in seen[0]


@pytest.mark.asyncio
async def test_fetch_refs_returns_a_redacted_error(monkeypatch):
    async def fake_run(cmd, **kw):
        return 1, "", "fatal: could not read Username for 'https://github.com'"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    err = await rcp.fetch_refs("/fake/repo")
    assert err and "fatal" in err


# --------------------------------------------------------------------------
# worktree position relative to the lane tip
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_state_counts_behind_the_channel_tip_not_main(monkeypatch):
    """``behind`` on a channel row means distance from the lane tip.

    The fleet's usual behind-count is against ``BASE_BRANCH``; on a release
    worktree that number is large and meaningless, because the worktree is not
    trying to track main.

    Also pins the defect that made the row's badge dishonest: ``version`` is the
    release the tree IS on (``v0.5.0``), never the lane's resolved tip
    (``v0.6.0``). A row fed the resolved version renames itself to every new
    release as it ships while the checkout stays put.
    """
    calls: list[list[str]] = []

    async def fake_run(cmd, **kw):
        calls.append(cmd)
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "3\n", ""
        if "tag" in cmd:
            return 0, "v0.5.0\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid", "lane": "stable", "version": "0.6.0"})
    assert got["behind"] == 3
    assert got["at_tip"] is False
    assert got["detached"] is True
    assert got["version"] == "0.5.0"
    ranges = [c[-1] for c in calls if "rev-list" in c]
    assert ranges == ["head-oid..tip-oid"]


@pytest.mark.asyncio
async def test_behind_worktree_on_a_foreign_tag_reports_no_release(monkeypatch):
    """A tag from ANOTHER lane at the same commit must not rename the row.

    A commit can carry several tags. Taking the first would let an insider tag
    that happens to share the commit label the stable row, so the lookup filters
    on the lane — and when nothing matches, ``None`` is the honest answer rather
    than falling back to the tip the tree does not contain.
    """

    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", ""
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "2\n", ""
        if "tag" in cmd:
            return 0, "v0.6.0-insider.4\nsome-local-marker\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid", "lane": "stable", "version": "0.6.0"})
    assert got["behind"] == 2
    assert got["version"] is None


@pytest.mark.asyncio
async def test_worktree_state_reports_at_tip_without_counting(monkeypatch):
    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", ""
        if "rev-parse" in cmd:
            return 0, "same-oid\n", ""
        raise AssertionError(f"should not have run: {cmd}")

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "same-oid", "lane": "stable", "version": "0.6.0"})
    assert got["at_tip"] is True
    assert got["behind"] == 0
    # At the tip the tree is ON the tip's release by definition, so the version
    # comes from the already-resolved answer. The `raise` above is the assertion
    # that matters: no `git tag` call is spent re-deriving what we know.
    assert got["version"] == "0.6.0"


@pytest.mark.asyncio
async def test_an_unreadable_head_is_neither_detached_nor_on_a_branch(monkeypatch):
    """``detached`` carries THREE states, or the caller's three-state code is decoration.

    ``symbolic-ref --quiet HEAD`` exits non-zero for a detached HEAD AND for a read
    that failed outright -- a directory removed while still registered in ``git
    worktree list``, an unreadable repo. Deriving ``detached`` from that alone
    published such a tree as a confirmed lane pin, so the row offered Advance on a
    tree nobody had read. ``rev-parse HEAD`` is the discriminator.
    """

    async def unreadable(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", "fatal: not a git repository"
        if "rev-parse" in cmd:
            return 1, "", "fatal: bad revision"
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", unreadable)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid", "lane": "stable"})
    assert got["detached"] is None
    assert got["at_tip"] is False
    assert got["version"] is None


@pytest.mark.asyncio
async def test_worktree_state_reports_an_attached_head_as_not_detached(monkeypatch):
    """The guard against adopting a coincidentally-named worktree.

    A user's own ``release-channel-stable`` branch checkout must not gain lane
    controls on the strength of its name; only a detached checkout at a resolved
    ref is a channel worktree.
    """

    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "1\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid"})
    assert got["detached"] is False

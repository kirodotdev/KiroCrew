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

# Every resolver test fakes `runtime._run_cmd`, so no argv reaches git and the
# checkout path is inert — any string does. It is passed explicitly rather than
# left to a default because `resolve(repo=...)` is required: a test that omitted
# it would be resolving against whatever checkout the ambient Dev Fleet
# configuration happens to name, which is a dependency these tests do not want.
_FAKE_REPO = "/fake/checkout"


def _fake_git(tags: list[str] | None = None, *, oids: dict[str, str] | None = None):
    """A git stand-in answering only what the resolver asks.

    ``tags`` stands for the contents of the MIRROR (:data:`rcp._TAG_NS`), not of
    ``refs/tags/``. The resolver reads only the mirror, so a fixture that seeded
    local tags would be describing a namespace it never queries.
    """
    tags = _TAGS_NEWEST_FIRST if tags is None else tags
    oids = oids or {}

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd and any(rcp._TAG_NS in part for part in cmd):
            # `--points-at HEAD` narrows the same namespace to the checked-out
            # commit; the resolver's listing does not pass it. Fixtures that need
            # the narrowed answer override this stand-in.
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
def test_the_worktree_name_carries_the_channel_and_is_spelled_once():
    """One naming rule, on the backend only.

    The fleet payload publishes this string, so the frontend never rebuilds it — a
    second copy of the rule is what would let a change here desync a row's label from
    the directory it names.

    The name is a literal rather than a prefix composed with :data:`rcp.CHANNEL`, so
    what is asserted is the PROPERTY that matters: the basename still names its
    channel. An equality against a composition of two constants in this same module
    would be a tautology — it can only ever agree with itself.
    """
    assert rcp.WORKTREE_NAME == "release-channel-stable"
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
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["ref"] == "refs/tags/v0.5.0"
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
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["ref"] == "refs/tags/v0.5.0"


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
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ref"] == "refs/tags/v0.5.0"


@pytest.mark.asyncio
async def test_resolve_stable_compares_version_parts_numerically(monkeypatch):
    """``v0.10.0`` beats ``v0.9.0`` — a string sort would get this backwards."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.9.0", "v0.10.0"]))
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ref"] == "refs/tags/v0.10.0"


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

    Peeled from the MIRROR ref, which is also what the oid proves: resolution never
    reads ``refs/tags/``, so the commit handed to Create is one the remote published.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["oid"] == f"oid-{rcp._TAG_NS}/v0.5.0^{{commit}}"


@pytest.mark.asyncio
async def test_resolve_reports_an_empty_channel_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.6.0-insider.6"]))
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is False
    assert "no stable release tag" in got["error"]
    # The discriminator: an empty channel is benign, so it is flagged apart from a
    # git failure. The fleet row reads this to render information rather than an
    # error, so the flag must be present on this path and true.
    assert got.get("unpublished") is True


@pytest.mark.asyncio
async def test_resolve_does_not_flag_a_git_failure_as_unpublished(monkeypatch):
    """A git failure is NOT the benign empty-channel state and must not be flagged.

    ``list_release_tags`` returns ``None`` when the tag listing itself fails, which
    is a genuine failure the operator is shown — distinct from an empty mirror. The
    two share ``ok: False`` and are told apart only by ``unpublished``, so the flag
    must be absent (falsy) here.
    """

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd:
            return 1, "", "fatal: not a git repository"
        return 1, "", f"unexpected argv: {cmd}"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is False
    assert "cannot list tags" in got["error"]
    assert not got.get("unpublished")


@pytest.mark.asyncio
async def test_resolve_skips_a_tag_that_will_not_resolve(monkeypatch):
    """One broken local ref must not report the whole lane as unpublished."""

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd:
            return 0, "v0.6.0\nv0.5.0\n", ""
        if "rev-parse" in cmd:
            if "v0.6.0^{commit}" in cmd[-1]:
                return 1, "", "bad object"
            return 0, f"oid-{cmd[-1]}\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["ref"] == "refs/tags/v0.5.0"


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
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert "lane_check" not in got
    assert got["version"] == "0.5.0"
    assert rcp.is_release_tag("v0.5.0") is True


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_local_only_tag_is_never_a_channel_candidate(monkeypatch):
    """The masquerade this namespace exists to stop.

    ``v99.0.0`` on an operator's machine -- a locally cut release candidate, a bisect
    marker, a tag someone made to test an upgrade path -- matches the stable shape,
    sorts newest, and under ``git tag --list`` became the channel tip. Create would
    then check out an unpublished commit and the badge would call it a release.

    The assertion is on WHICH namespace is read, because that is the whole mechanism:
    a local tag is indistinguishable from a fetched one after the fact, so the only
    place the two can be separated is where the ref is written. The mirror here holds
    the published releases; the fake answers the query only for the mirror path, so a
    resolver that reached for ``refs/tags/`` gets nothing rather than the fake tip.
    """
    queried: list[list[str]] = []

    async def fake_run(cmd, **kw):
        queried.append(list(cmd))
        if "for-each-ref" in cmd and any(rcp._TAG_NS in part for part in cmd):
            return 0, "v0.5.0\nv0.4.1\n", ""
        if "tag" in cmd:
            # What refs/tags/ would have answered, fake tip included. Reaching this
            # branch at all is the defect.
            return 0, "v99.0.0\nv0.5.0\nv0.4.1\n", ""
        if "rev-parse" in cmd:
            return 0, f"oid-{cmd[-1]}\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["version"] == "0.5.0"
    assert "99" not in got["ref"]
    assert not [c for c in queried if "tag" in c and "--list" in c]


@pytest.mark.asyncio
async def test_the_version_probe_ignores_a_local_tag_on_the_checked_out_commit(monkeypatch):
    """The same masquerade one row down.

    ``git tag --points-at HEAD`` would let a tag the operator wrote rename the row,
    so a local ``v99.0.0`` sitting on the checked-out commit would badge it as a
    shipped release -- a claim about publication made from a purely local fact.

    The row is deliberately BEHIND the tip: at the tip the release is known from the
    resolved answer and no probe runs, so the at-tip row could not exercise this.
    """

    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "4\n", ""
        if "for-each-ref" in cmd and "--points-at" in cmd:
            # The mirror holds no ref at this commit: the local tag is not published.
            return 0, "", ""
        if "tag" in cmd and "--points-at" in cmd:
            return 0, "v99.0.0\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid", "lane": "stable", "version": "0.5.0"})
    assert got["behind"] == 4
    assert got["version"] is None


@pytest.mark.asyncio
async def test_an_unfetched_mirror_reads_as_unpublished_not_as_an_error(monkeypatch):
    """An empty mirror is a state, not a failure, and the two must stay apart.

    A checkout that has not fetched yet genuinely does not know what is published,
    so reporting "no release found" is honest and the next refresher cycle fixes it.
    Reporting a git error instead would put a red row on an ordinary cold start --
    and, worse, a resolver that treated empty as a reason to fall back to
    ``refs/tags/`` would reintroduce the masquerade on exactly the path where the
    mirror is least trustworthy.
    """

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd:
            return 0, "", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is False
    assert "no stable release tag" in got["error"]
    assert "failed" not in got["error"]


@pytest.mark.asyncio
async def test_a_failed_mirror_read_is_reported_as_an_error(monkeypatch):
    """git itself failing is a different row from an empty mirror.

    ``None`` from the listing must stay distinguishable from ``[]``: one is an
    operator-visible error, the other is a cold start.
    """

    async def fake_run(cmd, **kw):
        return 1, "", "fatal: not a git repository"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ok"] is False
    assert "cannot list tags" in got["error"]


@pytest.mark.asyncio
async def test_fetch_refs_populates_the_mirror_it_resolves_from(monkeypatch):
    """The fetch and the read must name the same namespace.

    Two callers fetch for this channel -- Create and the background refresher -- and
    a refspec spelled separately in each is one that drifts. The drifted copy fails
    silently: the channel resolves against a mirror nothing updates, which looks
    exactly like a repo that has published nothing new. So this asserts the fetch
    writes the namespace the resolver reads, derived from the same constant rather
    than from a literal repeated in the test.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs("/fake/repo") is None
    assert rcp._TAG_REFSPEC in seen[0]
    assert rcp._TAG_REFSPEC.endswith(rcp._TAG_NS + "/v*")
    # Forced: a re-pushed tag must overwrite the mirror rather than be skipped as a
    # non-fast-forward, because the mirror is a cache of the remote's answer.
    assert rcp._TAG_REFSPEC.startswith("+")


@pytest.mark.asyncio
async def test_fetch_refs_still_refreshes_the_operators_own_tag_namespace(monkeypatch):
    """``--tags`` is kept BESIDE the mirror refspec, not replaced by it.

    The reported ``ref`` is a real ``refs/tags/vX.Y.Z`` the operator can check out,
    and every ordinary git command they run in this repo reads that namespace. The
    channel reads only the mirror, so keeping ``refs/tags/`` current cannot put a
    local tag back in the running.

    Beside, not inside: the two run as separate fetches, so this looks for ``--tags``
    across the invocations rather than in the one that carries the refspec.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.fetch_refs("/fake/repo")
    assert any("--tags" in argv for argv in seen)


@pytest.mark.asyncio
async def test_the_reported_ref_is_the_tag_the_operator_can_act_on(monkeypatch):
    """Resolution reads the mirror; the payload names ``refs/tags/``.

    The mirror is how "published" is decided, not something to put in front of a
    human: ``git checkout refs/dev-fleet/release-tags/v0.5.0`` is not a thing anyone
    types. The oid still comes from the mirror ref, which is what makes the answer a
    published one.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.5.0"]))
    got = await rcp.resolve(repo=_FAKE_REPO)
    assert got["ref"] == "refs/tags/v0.5.0"
    assert rcp._TAG_NS not in got["ref"]
    assert got["oid"] == f"oid-{rcp._TAG_NS}/v0.5.0^{{commit}}"


@pytest.mark.asyncio
async def test_fetch_refs_prunes_the_mirror_but_never_the_operators_tags(monkeypatch):
    """``--prune`` yes, ``--prune-tags`` never -- and the distinction is the whole point.

    ``--prune`` is what makes the mirror a statement about what the remote advertised.
    Without it the namespace is merely a place the operator has no REASON to write to,
    which is habit rather than a boundary: a ref store is writable by anything with
    access to this repo, so a hand-written mirror ref outranks every real release by
    semver, survives every additive fetch, and is checked out and badged as stable.

    ``--prune-tags`` is a different flag and stays refused. It contributes an implicit
    ``+refs/tags/*:refs/tags/*``, which is what would delete a tag the operator
    authored -- and a pruned tag ref is in no reflog.

    The split makes the scoping structural instead of argued. Plain ``--prune`` prunes
    only the destinations of the refspecs actually given, which ``git-fetch(1)`` states
    directly, and the pruning invocation names the mirror refspec and the base branch
    and no tags at all -- so ``refs/tags/`` is not among its destinations for an
    operator's local tag to be reachable by. The courtesy ``--tags`` fetch carries no
    pruning flag.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs("/fake/repo") is None
    pruning = [argv for argv in seen if "--prune" in argv]
    assert len(pruning) == 1, "exactly one invocation prunes"
    assert rcp._TAG_REFSPEC in pruning[0], "the pruning fetch is the one writing the mirror"
    # The load-bearing assertion: the pruning fetch does not name the operator's tag
    # namespace, in either spelling.
    assert "--tags" not in pruning[0]
    assert "--prune-tags" not in pruning[0]
    for argv in seen:
        assert "--prune-tags" not in argv
    # ...and the courtesy fetch prunes nothing.
    courtesy = [argv for argv in seen if "--tags" in argv]
    assert courtesy, "the operator's tag namespace is still refreshed"
    for argv in courtesy:
        assert "--prune" not in argv


@pytest.mark.asyncio
async def test_a_diverged_local_tag_does_not_block_the_channel(monkeypatch):
    """A local tag the operator cut is theirs to keep, not a reason to refuse Create.

    ``--tags`` is unforced, so when a local ``vX.Y.Z`` has diverged from the remote's
    tag of the same name git reports ``! [rejected] ... (would clobber existing tag)``
    and exits non-zero. While both jobs shared one invocation that status was the
    status of the whole refresh, so an operator who had ever cut a release-shaped tag
    could not create the channel worktree at all -- even though the forced mirror
    refspec, the only thing the channel resolves against, had succeeded.

    Pinned at this level because the failure is a benign LOCAL state that no fixture
    of the remote can produce: only the courtesy fetch fails, and ``fetch_refs`` must
    still report success.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        if "--tags" in cmd:
            return (
                1,
                "",
                "! [rejected] v1.0.0 -> v1.0.0 (would clobber existing tag)",
            )
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs("/fake/repo") is None
    assert any(rcp._TAG_REFSPEC in argv for argv in seen), "the mirror was still refreshed"


@pytest.mark.asyncio
async def test_a_failed_mirror_fetch_still_fails_the_refresh(monkeypatch):
    """The other half of the split: only the courtesy fetch is best-effort.

    Mutation-guard for the test above -- if tolerating a non-zero rc leaked to the
    mirror fetch, a Create would resolve against a stale mirror and silently check out
    a superseded release.
    """

    async def fake_run(cmd, **kw):
        if rcp._TAG_REFSPEC in cmd:
            return 1, "", "fatal: couldn't find remote ref"
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    err = await rcp.fetch_refs("/fake/repo")
    assert err and "couldn't find remote ref" in err


@pytest.mark.asyncio
async def test_a_forged_mirror_ref_cannot_outlive_the_fetch(monkeypatch):
    """The integrity guard: resolution is only as trustworthy as the mirror is pruned.

    A mirror ref nothing on the remote advertises is a forgery -- one ``git update-ref``
    away for any agent with git in this repo -- and semver ordering hands it the tip.
    The defence is not the namespace's obscurity, it is that every fetch preceding a
    mutation removes what the remote does not advertise, so a forged ref cannot be the
    thing Create checks out.

    Asserted at the argv level because that is where the property lives: the fetch that
    populates the mirror must also prune it, in the SAME invocation, or there is a
    window where the mirror holds a ref the remote never published.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.fetch_refs("/fake/repo")
    populating = [c for c in seen if rcp._TAG_REFSPEC in c]
    assert populating, "the fetch must carry the mirror refspec"
    for argv in populating:
        assert "--prune" in argv, "the fetch that writes the mirror must also prune it"


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
        if "for-each-ref" in cmd:
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
        if "for-each-ref" in cmd:
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
    published such a tree as a confirmed lane pin, so the row adopted a tree nobody
    had read. ``rev-parse HEAD`` is the discriminator.
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


# --------------------------------------------------------------------------
# offline resolution
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_answers_from_the_mirror_with_the_remote_unreachable(monkeypatch):
    """The mirror's unique job: a published version on the row with no network.

    This is the property that decides whether the mirror may be replaced by the
    ``ls-remote`` answer Create verifies against. It cannot: the fleet is
    deliberately offline-tolerant -- PR lookups degrade to ``None`` on any git or
    ``gh`` failure so the rows still render from local state -- and the release row
    participates in that render only because resolution reads ``.git``. Sourcing it
    from an advertisement would make this the one row that blanks offline, and the
    in-memory fleet cache cannot cover the gap because a cold process starts empty.

    The fake fails every ``ls-remote`` and every ``fetch``, so a resolver that
    reached the network would either be caught by the argv assertion or fail
    outright -- which is what makes this test fail on the substitution rather than
    merely pass alongside it.
    """
    recorded: list[list[str]] = []
    mirror = _fake_git()

    async def offline(cmd, **kw):
        recorded.append(list(cmd))
        if "ls-remote" in cmd or "fetch" in cmd:
            return 128, "", "fatal: unable to access remote: Could not resolve host"
        return await mirror(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", offline)

    got = await rcp.resolve(repo=_FAKE_REPO)

    assert got["ok"] is True, "the mirror must resolve without reaching the remote"
    assert got["ref"] == "refs/tags/v0.5.0"
    assert got["version"] == "0.5.0"
    assert got["oid"]
    assert not any(
        "ls-remote" in argv or "fetch" in argv for argv in recorded
    ), f"resolve() must read .git only; it issued {recorded}"

"""The release channel resolves to the ref stable actually published.

The defect these tests exist to prevent is a SILENT one: a lane that resolves to the
wrong ref still produces a worktree that builds, boots as a pod and serves a
dashboard, so "stable" showing a prerelease looks exactly like success. Every
assertion below is therefore about the resolver's *answer*, not about whether it ran.

Tag fixtures use this repository's real tag vocabulary (``v0.5.0``,
``v0.6.0-insider.6``) so a rename of the release workflow's tag shape shows up here
rather than in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import release_channel_pin as rcp
from kiro_crew.apps.builtins.dev_fleet import repository, runtime

# Newest first by DATE, which is the order a `--sort=-creatordate` listing would give.
# The interleaving is the point: insider's tip is NEWER than stable's tip, so a
# resolver that ignored the shape filter and simply took the first line would return an
# insider tag for stable -- and that is the real shape of this repo's tag history, not a
# contrived case.
_TAGS_NEWEST_FIRST = [
    "v0.6.0-insider.6",
    "v0.6.0-insider.5",
    "v0.5.0",
    "v0.5.0-insider.11",
    "v0.4.1",
]

# Every resolver test fakes `runtime._run_cmd`, so no argv reaches git and the checkout
# path is inert -- any string does. It is passed explicitly rather than left to a
# default because `resolve(repo=...)` is required: a test that omitted it would be
# resolving against whatever checkout the ambient Dev Fleet configuration happens to
# name, which is a dependency these tests do not want.
_FAKE_REPO = "/fake/checkout"


def _fake_git(tags: list[str] | None = None, *, oids: dict[str, str] | None = None):
    """A git stand-in answering only what the resolver asks.

    ``tags`` stands for the contents of the MIRROR (``rcp._TAG_NS``), not of
    ``refs/tags/``. The resolver reads only the mirror, so a fixture that seeded local
    tags would be describing a namespace it never queries.
    """
    tags = _TAGS_NEWEST_FIRST if tags is None else tags
    oids = oids or {}

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd and any(rcp._TAG_NS in part for part in cmd):
            # `--points-at HEAD` narrows the same namespace to the checked-out commit;
            # the resolver's listing does not pass it. Fixtures that need the narrowed
            # answer override this stand-in.
            return 0, "\n".join(tags) + "\n", ""
        if "rev-parse" in cmd:
            target = cmd[-1]
            if target in oids:
                return 0, oids[target] + "\n", ""
            # The operator's `refs/tags/<tag>` AGREES with the mirror unless a fixture
            # says otherwise through `oids`: that is the ordinary state after the
            # courtesy `--tags` fetch, and it is what lets the resolver publish the
            # tag ref the operator can type. A fixture models divergence by pinning
            # the `refs/tags/...` key to another oid, and absence by pinning it to "".
            if target.startswith("refs/tags/"):
                target = f"{rcp._TAG_NS}/{target[len('refs/tags/'):]}"
                if target in oids:
                    return 0, oids[target] + "\n", ""
            # Deterministic stand-in oid derived from the ref, so assertions can tie a
            # returned oid back to the ref it was resolved from.
            return 0, f"oid-{target}\n", ""
        return 1, "", f"unexpected argv: {cmd}"

    return fake_run


@pytest.fixture(autouse=True)
def _pinned_remote(_floor_monkeypatch):
    """Pin the remote so no test resolves it from an ambient checkout's git config.

    Through ``_floor_monkeypatch``, not the test's own ``monkeypatch``: an autouse
    patch on the shared stack is lifted by any test that calls ``monkeypatch.undo()``,
    while the floor's stack is undone independently (testing-conventions D11).
    """
    _floor_monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    _floor_monkeypatch.setattr(repository, "BASE_BRANCH", "main")


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------
def test_the_worktree_name_carries_the_channel_and_is_spelled_once():
    """One naming rule, on the backend only.

    The fleet payload publishes this string, so the frontend never rebuilds it -- a
    second copy of the rule is what would let a change here desync a row's label from
    the directory it names.

    The name is a literal rather than a prefix composed with ``rcp.CHANNEL``, so what is
    asserted is the PROPERTY that matters: the basename still names its channel. An
    equality against a composition of two constants in this same module would be a
    tautology.
    """
    assert rcp.WORKTREE_NAME == "release-channel-stable"
    assert rcp.WORKTREE_NAME.endswith(rcp.CHANNEL)


def test_worktree_name_is_a_valid_pod_identity():
    """The basename becomes ``kirocrew-pod@<name>.service``.

    A name that fails the pod name rule would surface as a pod that cannot be brought
    up -- long after the worktree was created and built.
    """
    from kiro_crew.pod.runtime import _NAME_RE

    assert _NAME_RE.match(rcp.WORKTREE_NAME), rcp.WORKTREE_NAME


def test_worktree_path_is_a_sibling_of_the_primary_checkout():
    # Compared as PATHS, not as strings: `worktree_path` returns a native path, so a
    # POSIX string literal would assert the separator rather than the placement.
    repo = Path("/Users/me/Projects/KiroCrew")  # brand-ok: directory name, not prose
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

    ``is_release_tag`` does not call ``release_channel.channel``: a bare ``vX.Y.Z`` has
    no suffix left to read, so the call could only agree. What still has to hold is that
    the two never DIVERGE -- a release-shaped tag this module admits must be one the
    shared rule also calls stable, or the worktree would pin to a build the rest of the
    product does not treat as a release.
    """
    from kiro_crew import release_channel

    assert rcp.is_release_tag(tag) is True
    assert release_channel.channel(tag[1:]) == rcp.CHANNEL


@pytest.mark.parametrize(
    "tag",
    [
        "v0.6.0-insider.6",
        "v0.6.0-rc.1",
        "0.5.0",
        "v0.5",
        "v0.5.0.1",
        "release-v0.5.0",
        "v0.5.0-",
        "",
    ],
)
def test_a_non_release_tag_is_rejected_at_the_shape_check(tag):
    assert rcp.is_release_tag(tag) is False


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_skips_newer_prerelease_tags(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["ref"] == "refs/tags/v0.5.0"
    assert got["version"] == "0.5.0"
    assert got["lane"] == rcp.CHANNEL


@pytest.mark.asyncio
async def test_a_prerelease_tag_never_becomes_the_tip_even_when_it_is_the_only_newer_one(
    monkeypatch,
):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.9.0-insider.1", "v0.4.1"]))
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["version"] == "0.4.1"


@pytest.mark.asyncio
async def test_resolve_orders_by_version_not_by_listing_order(monkeypatch):
    """A backport cut after a newer line is the newest tag by DATE and an older release.

    ``v0.4.2`` first in the listing is exactly what ``--sort=-creatordate`` would hand
    over for that history, and pinning it would put the row on a release stable users
    are not on.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.4.2", "v0.5.0"]))
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["version"] == "0.5.0"


@pytest.mark.asyncio
async def test_resolve_compares_version_parts_numerically(monkeypatch):
    """``v0.10.0`` beats ``v0.9.0``; a lexicographic sort says the opposite."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.9.0", "v0.10.0"]))
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["version"] == "0.10.0"


def test_release_candidates_is_deterministic_under_a_reordered_listing():
    """Two orderings of one tag set resolve to the same sequence, so a repeat agrees."""
    forward = rcp._release_candidates(["v0.4.1", "v0.5.0", "v1.0.0"])
    backward = rcp._release_candidates(["v1.0.0", "v0.5.0", "v0.4.1"])
    assert forward == backward == ["v1.0.0", "v0.5.0", "v0.4.1"]


def test_the_channel_is_a_real_release_channel():
    """A lane name the rest of the product does not know is a row nobody can read."""
    from kiro_crew import release_channel

    assert release_channel.channel("1.2.3") == rcp.CHANNEL


@pytest.mark.asyncio
async def test_resolve_reports_the_oid_of_the_tagged_commit(monkeypatch):
    """The oid is the one the MIRROR ref resolves to, peeled to a commit."""
    monkeypatch.setattr(
        runtime,
        "_run_cmd",
        _fake_git(["v0.5.0"], oids={f"{rcp._TAG_NS}/v0.5.0^{{commit}}": "a" * 40}),
    )
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["oid"] == "a" * 40


@pytest.mark.asyncio
async def test_an_unfetched_mirror_reads_as_unpublished_not_as_an_error(monkeypatch):
    """The ordinary state of a checkout that has not fetched yet is not an incident."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git([]))
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ok"] is False
    assert got["unpublished"] is True
    assert rcp.CHANNEL in got["error"]


@pytest.mark.asyncio
async def test_resolve_does_not_flag_a_git_failure_as_unpublished(monkeypatch):
    """A failed listing is an error the operator is shown, a different row entirely."""

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd:
            return 128, "", "fatal: not a git repository"
        return 1, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ok"] is False
    assert "unpublished" not in got


@pytest.mark.asyncio
async def test_resolve_skips_a_mirror_ref_that_will_not_resolve(monkeypatch):
    """One broken ref must not hide the real release behind an empty channel."""

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd:
            return 0, "v0.9.0\nv0.5.0\n", ""
        if "rev-parse" in cmd and cmd[-1].endswith("v0.9.0^{commit}"):
            return 128, "", "fatal: bad revision"
        if "rev-parse" in cmd:
            return 0, "b" * 40 + "\n", ""
        return 1, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["version"] == "0.5.0"


@pytest.mark.asyncio
async def test_a_local_only_tag_is_never_a_channel_candidate(monkeypatch):
    """``refs/tags/`` is a namespace the operator writes to; the mirror is not.

    A hand-cut ``v99.0.0`` sorts newest and would badge an unpublished commit as the
    shipped release. The listing reads the mirror, so the local tag is invisible.
    """
    read: list[list[str]] = []

    async def fake_run(cmd, **kw):
        read.append(list(cmd))
        if "for-each-ref" in cmd:
            return 0, "v0.5.0\n", ""
        if "rev-parse" in cmd:
            return 0, "c" * 40 + "\n", ""
        return 1, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["version"] == "0.5.0"
    assert not any("tag" in cmd for cmd in read), read
    listing = next(cmd for cmd in read if "for-each-ref" in cmd)
    assert f"{rcp._TAG_NS}/" in listing


@pytest.mark.asyncio
async def test_the_reported_ref_is_the_tag_the_operator_can_act_on(monkeypatch):
    """The mirror is how "published" is decided; it is not a ref anyone types."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(["v0.5.0"]))
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ref"] == "refs/tags/v0.5.0"
    assert rcp._TAG_NS not in got["ref"]


@pytest.mark.asyncio
async def test_a_diverged_local_tag_is_not_published_as_the_row_ref(monkeypatch):
    """``refs/tags/<tag>`` is named only while it IS the commit the row is about.

    The courtesy ``--tags`` fetch is unforced, so an operator's own ``v0.5.0`` -- or a
    stale one from before upstream re-pushed the release -- keeps pointing elsewhere.
    Naming it would send whoever acts on the displayed ref to the wrong tree, so the
    row names the ref that actually resolved: the mirror's.
    """
    monkeypatch.setattr(
        runtime,
        "_run_cmd",
        _fake_git(["v0.5.0"], oids={"refs/tags/v0.5.0^{commit}": "d" * 40}),
    )
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["oid"] == f"oid-{rcp._TAG_NS}/v0.5.0^{{commit}}"
    assert got["ref"] == f"{rcp._TAG_NS}/v0.5.0"


@pytest.mark.asyncio
async def test_an_absent_local_tag_is_not_published_as_the_row_ref(monkeypatch):
    """No local ``refs/tags/v0.5.0`` at all (the courtesy fetch failed): same answer."""

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd:
            return 0, "v0.5.0\n", ""
        if "rev-parse" in cmd and cmd[-1].startswith("refs/tags/"):
            return 1, "", ""  # --verify --quiet: absent
        if "rev-parse" in cmd:
            return 0, "e" * 40 + "\n", ""
        return 1, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ok"] is True
    assert got["ref"] == f"{rcp._TAG_NS}/v0.5.0"


@pytest.mark.asyncio
async def test_the_local_tag_check_is_one_verified_quiet_rev_parse(monkeypatch):
    """The comparison reads the tag's PEELED commit and tolerates its absence quietly."""
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        if "for-each-ref" in cmd:
            return 0, "v0.5.0\n", ""
        if "rev-parse" in cmd:
            return 0, "f" * 40 + "\n", ""
        return 1, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.resolve_tip(repo=_FAKE_REPO)
    local = [c for c in seen if "rev-parse" in c and c[-1].startswith("refs/tags/")]
    assert len(local) == 1
    assert local[0][-3:] == ["--verify", "--quiet", "refs/tags/v0.5.0^{commit}"]


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def _fetch_recorder(*, mirror=(0, "", ""), courtesy=(0, "", "")):
    calls: list[list[str]] = []

    async def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return courtesy if "--tags" in cmd else mirror

    return calls, fake_run


@pytest.mark.asyncio
async def test_fetch_refs_populates_the_mirror_it_resolves_from(monkeypatch):
    calls, fake_run = _fetch_recorder()
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs(_FAKE_REPO) is None
    mirror = next(cmd for cmd in calls if rcp._TAG_REFSPEC in cmd)
    assert rcp._TAG_REFSPEC == f"+refs/tags/v*:{rcp._TAG_NS}/v*"
    assert "origin" in mirror
    assert repository.BASE_BRANCH in mirror


@pytest.mark.asyncio
async def test_fetch_refs_prunes_the_mirror_and_never_asks_for_prune_tags(monkeypatch):
    """``--prune`` is scoped to the destinations of the refspecs given.

    Without it a hand-written mirror ref survives every additive fetch and outranks
    every real release. ``--prune-tags`` would add an implicit
    ``+refs/tags/*:refs/tags/*`` and destroy the operator's own tags, so its absence is
    part of the contract, not an omission.
    """
    calls, fake_run = _fetch_recorder()
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.fetch_refs(_FAKE_REPO)
    mirror = next(cmd for cmd in calls if rcp._TAG_REFSPEC in cmd)
    assert "--prune" in mirror
    assert all("--prune-tags" not in cmd for cmd in calls), calls
    assert "--tags" not in mirror, "the pruning fetch must not name tags as a destination"


@pytest.mark.asyncio
async def test_the_pruning_fetch_pins_no_prune_tags_against_repo_config(monkeypatch):
    """``fetch.pruneTags`` in the repository's own ``.git/config`` would make ``--prune``
    with explicit refspecs also prune ``refs/tags/`` -- the operator's local-only tags
    gone on every refresher cycle, from a file an agent can write. The flag is said
    explicitly so the scoping the docstring promises holds whatever the config says."""
    calls, fake_run = _fetch_recorder()
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.fetch_refs(_FAKE_REPO)
    mirror = next(cmd for cmd in calls if rcp._TAG_REFSPEC in cmd)
    assert "--no-prune-tags" in mirror
    assert mirror.index("--no-prune-tags") < mirror.index(rcp._TAG_REFSPEC)


@pytest.mark.asyncio
async def test_two_fetches_in_one_loop_run_one_at_a_time(monkeypatch):
    """The refresher and a Create fetch the same refs; concurrent fetches contend for
    the same ref locks and the loser's ``cannot lock ref`` would refuse a Create that a
    second later succeeds. Serialized per event loop."""
    inside = 0
    overlapped = False

    async def fake_run(cmd, **kw):
        nonlocal inside, overlapped
        if "fetch" in cmd:
            inside += 1
            overlapped = overlapped or inside > 1
            await asyncio.sleep(0)
            inside -= 1
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    results = await asyncio.gather(rcp.fetch_refs(_FAKE_REPO), rcp.fetch_refs(_FAKE_REPO))
    assert results == [None, None]
    assert overlapped is False


@pytest.mark.asyncio
async def test_fetch_refs_still_refreshes_the_operators_own_tag_namespace(monkeypatch):
    """The reported ``refs/tags/vX.Y.Z`` has to be real in this checkout."""
    calls, fake_run = _fetch_recorder()
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.fetch_refs(_FAKE_REPO)
    assert any("--tags" in cmd for cmd in calls), calls


@pytest.mark.asyncio
async def test_the_courtesy_fetch_can_never_prune_the_operators_tags(monkeypatch):
    """It names ``refs/tags/`` as a destination, so ``fetch.prune`` + ``fetch.pruneTags``
    in the repository's own config would make it delete local-only tags. Both flags are
    said on this invocation too; the first fetch's flags protect only the first fetch."""
    calls, fake_run = _fetch_recorder()
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.fetch_refs(_FAKE_REPO)
    courtesy = next(cmd for cmd in calls if "--tags" in cmd)
    assert "--no-prune" in courtesy and "--no-prune-tags" in courtesy, courtesy
    assert "--prune" not in courtesy


@pytest.mark.asyncio
async def test_a_diverged_local_tag_does_not_block_the_channel(monkeypatch):
    """The courtesy fetch is best-effort; only the mirror fetch decides.

    An operator who ever cut a release-shaped tag of their own gets
    ``! [rejected] ... would clobber existing tag`` and a non-zero exit from the
    unforced ``--tags`` fetch. Reported as one status, that would refuse every Create
    for them.
    """
    _calls, fake_run = _fetch_recorder(
        courtesy=(1, "", "! [rejected] v0.5.0 -> v0.5.0 (would clobber existing tag)")
    )
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs(_FAKE_REPO) is None


@pytest.mark.asyncio
async def test_a_failed_mirror_fetch_fails_the_refresh(monkeypatch):
    _calls, fake_run = _fetch_recorder(mirror=(128, "", "fatal: could not read from remote"))
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.fetch_refs(_FAKE_REPO)
    assert got is not None
    assert "could not read from remote" in got


@pytest.mark.asyncio
async def test_fetch_refs_redacts_the_error_it_returns(monkeypatch):
    """The string becomes the lane's ``error`` on the page, so it is scrubbed first."""
    secret = "https://x-access-token:ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA@github.com/o/r"
    _calls, fake_run = _fetch_recorder(mirror=(128, "", f"fatal: unable to access {secret}"))
    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.fetch_refs(_FAKE_REPO)
    assert got is not None
    assert "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in got


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_advertised_release_commit_reads_the_peel_line_of_an_annotated_tag(monkeypatch):
    """An annotated tag's plain entry is the TAG OBJECT, not the commit.

    A resolver holding the commit compared against the plain entry would refuse every
    annotated release, which is how releases are normally cut.
    """
    tag_object, commit = "d" * 40, "e" * 40

    async def fake_run(cmd, **kw):
        assert "ls-remote" in cmd
        return (
            0,
            f"{tag_object}\trefs/tags/v0.5.0\n{commit}\trefs/tags/v0.5.0^{{}}\n",
            "",
        )

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got, err = await rcp.advertised_release_commit(_FAKE_REPO, "v0.5.0")
    assert (got, err) == (commit, None)


@pytest.mark.asyncio
async def test_advertised_release_commit_accepts_a_lightweight_tag(monkeypatch):
    """A lightweight tag advertises its commit on the plain entry and has no peel."""

    async def fake_run(cmd, **kw):
        return 0, f"{'f' * 40}\trefs/tags/v0.5.0\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got, err = await rcp.advertised_release_commit(_FAKE_REPO, "v0.5.0")
    assert (got, err) == ("f" * 40, None)


@pytest.mark.asyncio
async def test_an_unadvertised_tag_is_absence_not_an_error(monkeypatch):
    """``ls-remote`` exits 0 and prints nothing for a tag the remote does not carry."""

    async def fake_run(cmd, **kw):
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.advertised_release_commit(_FAKE_REPO, "v0.5.0") == (None, None)


@pytest.mark.asyncio
async def test_a_failed_provenance_query_fails_closed(monkeypatch):
    async def fake_run(cmd, **kw):
        return 128, "", "fatal: could not read from remote repository"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got, err = await rcp.advertised_release_commit(_FAKE_REPO, "v0.5.0")
    assert got is None
    assert err and "could not read from remote" in err


@pytest.mark.asyncio
async def test_the_remote_is_asked_for_only_the_resolved_tag_and_its_peel(monkeypatch):
    """Provenance does not need to enumerate every release tag the remote carries."""
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return 0, f"{'1' * 40}\trefs/tags/v0.5.0\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.advertised_release_commit(_FAKE_REPO, "v0.5.0")
    assert seen[0][-2:] == ["refs/tags/v0.5.0", "refs/tags/v0.5.0^{}"]


@pytest.mark.asyncio
async def test_provenance_retains_only_the_requested_tags_oids(monkeypatch):
    """A remote answering with unrelated refs cannot substitute one of its oids."""

    async def fake_run(cmd, **kw):
        return (
            0,
            f"{'9' * 40}\trefs/tags/v9.9.9\n{'2' * 40}\trefs/tags/v0.5.0\nnot-an-oid\trefs/tags/x\n",
            "",
        )

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got, err = await rcp.advertised_release_commit(_FAKE_REPO, "v0.5.0")
    assert (got, err) == ("2" * 40, None)


@pytest.mark.asyncio
async def test_provenance_refuses_a_non_release_tag_before_any_network_call(monkeypatch):
    """The tag reaches an argv, so its shape is checked before it is interpolated."""
    called: list[list[str]] = []

    async def fake_run(cmd, **kw):
        called.append(list(cmd))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    with pytest.raises(ValueError):
        await rcp.advertised_release_commit(_FAKE_REPO, "--upload-pack=evil")
    assert called == []


# --------------------------------------------------------------------------
# worktree state
# --------------------------------------------------------------------------
def _tree_git(*, head: str, symref_rc: int = 1, points_at: str = ""):
    """A git stand-in for the three reads ``worktree_state`` makes."""

    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return (0, "refs/heads/main\n", "") if symref_rc == 0 else (1, "", "not a symbolic ref")
        if "for-each-ref" in cmd:
            return 0, (points_at + "\n" if points_at else ""), ""
        if "rev-parse" in cmd:
            return (0, head + "\n", "") if head else (128, "", "fatal: bad revision")
        return 1, "", ""

    return fake_run


@pytest.mark.asyncio
async def test_a_tree_at_the_tip_reports_at_tip_and_the_tips_version(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head="t" * 40))
    got = await rcp.worktree_state(
        "/wt", {"ok": True, "oid": "t" * 40, "version": "0.5.0", "ref": "refs/tags/v0.5.0"}
    )
    assert got == {"at_tip": True, "detached": True, "version": "0.5.0"}


@pytest.mark.asyncio
async def test_a_stale_tree_reports_its_own_release_not_the_tips(monkeypatch):
    """The moment a newer release ships the tip moves and the tree does not.

    A row showing the resolved version would rename the operator's checkout to a build
    it does not contain.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head="o" * 40, points_at="v0.4.1"))
    got = await rcp.worktree_state("/wt", {"ok": True, "oid": "t" * 40, "version": "0.5.0"})
    assert got == {"at_tip": False, "detached": True, "version": "0.4.1"}


@pytest.mark.asyncio
async def test_a_tree_on_no_release_tag_reports_no_version(monkeypatch):
    """Adoption is for being DETACHED, not for being at a release."""
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head="o" * 40, points_at=""))
    got = await rcp.worktree_state("/wt", {"ok": True, "oid": "t" * 40, "version": "0.5.0"})
    assert got["version"] is None
    assert got["detached"] is True


@pytest.mark.asyncio
async def test_a_tree_on_a_local_only_tag_reports_no_release(monkeypatch):
    """``--points-at`` reads the mirror, so an operator's own tag cannot rename the row."""
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head="o" * 40, points_at="v99.0.0-local"))
    got = await rcp.worktree_state("/wt", {"ok": True, "oid": "t" * 40, "version": "0.5.0"})
    assert got["version"] is None


@pytest.mark.asyncio
async def test_a_trees_own_release_survives_a_failed_tip_lookup(monkeypatch):
    """Answered from the local mirror, so a failed resolve is no reason to withhold it."""
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head="o" * 40, points_at="v0.4.1"))
    got = await rcp.worktree_state("/wt", {})
    assert got == {"at_tip": False, "detached": True, "version": "0.4.1"}


@pytest.mark.asyncio
async def test_an_attached_head_is_reported_as_not_detached(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head="o" * 40, symref_rc=0))
    got = await rcp.worktree_state("/wt", {"ok": True, "oid": "t" * 40, "version": "0.5.0"})
    assert got["detached"] is False
    assert got["version"] is None


@pytest.mark.asyncio
async def test_an_unreadable_head_is_neither_detached_nor_on_a_branch(monkeypatch):
    """``symbolic-ref`` exits non-zero for a detached HEAD AND for a failed read.

    Reading the first as "detached" would publish a checkout of unknown shape as a
    confirmed channel pin, which is the one thing the caller must never be told.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head=""))
    got = await rcp.worktree_state("/wt", {"ok": True, "oid": "t" * 40, "version": "0.5.0"})
    assert got == {"at_tip": False, "detached": None, "version": None}


# --------------------------------------------------------------------------
# mirror bounds
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_mirror_listing_is_bounded_by_git_before_it_reaches_this_process(monkeypatch):
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return 0, "v0.5.0\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    await rcp.list_release_tags(_FAKE_REPO)
    assert f"--count={rcp._MAX_RELEASE_REFS + 1}" in seen[0]


def test_an_overflowing_listing_refuses_instead_of_sorting_a_truncated_set():
    """A truncated tail would sort and could name a release that is not the tip."""
    rows = "\n".join(f"v0.0.{n}" for n in range(rcp._MAX_RELEASE_REFS + 1))
    with pytest.raises(rcp.MirrorOverflow):
        rcp._bounded_mirror_rows(rows)


def test_an_overlong_ref_name_is_overflow_not_a_silent_drop():
    """A name longer than a release tag is something else wearing the namespace."""
    with pytest.raises(rcp.MirrorOverflow):
        rcp._bounded_mirror_rows("v0.5.0\n" + "v" + "0" * (rcp._MAX_REF_NAME_LEN + 1))


def test_overflow_is_decided_on_the_raw_rows_before_any_name_is_filtered():
    """Filtering first would let an overlength row pass as an absent one."""
    rows = "\n".join(["not-a-release-tag"] * (rcp._MAX_RELEASE_REFS + 1))
    with pytest.raises(rcp.MirrorOverflow):
        rcp._bounded_mirror_rows(rows)


def test_a_listing_at_the_cap_is_not_overflow():
    rows = "\n".join(f"v0.0.{n}" for n in range(rcp._MAX_RELEASE_REFS))
    assert len(rcp._bounded_mirror_rows(rows)) == rcp._MAX_RELEASE_REFS


@pytest.mark.asyncio
async def test_resolve_reports_an_overflowing_mirror_as_an_error(monkeypatch):
    rows = "\n".join(f"v0.0.{n}" for n in range(rcp._MAX_RELEASE_REFS + 1))

    async def fake_run(cmd, **kw):
        if "for-each-ref" in cmd:
            return 0, rows + "\n", ""
        return 1, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve_tip(repo=_FAKE_REPO)
    assert got["ok"] is False
    assert "unpublished" not in got
    assert "refusing to resolve" in got["error"]


@pytest.mark.asyncio
async def test_worktree_state_refuses_an_overflowing_points_at_listing(monkeypatch):
    """The caller turns this into the row's error rather than naming a release."""
    rows = "\n".join(f"v0.0.{n}" for n in range(rcp._MAX_RELEASE_REFS + 1))
    monkeypatch.setattr(runtime, "_run_cmd", _tree_git(head="o" * 40, points_at=rows))
    with pytest.raises(rcp.MirrorOverflow):
        await rcp.worktree_state("/wt", {"ok": True, "oid": "t" * 40, "version": "0.5.0"})

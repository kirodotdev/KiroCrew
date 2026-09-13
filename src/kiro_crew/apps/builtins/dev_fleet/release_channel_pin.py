"""Release-channel worktree: one detached checkout pinned to the stable release.

Dev Fleet manages git checkouts, and a git checkout has no release channel —
``platform/update_capability.py`` says so outright: *"Only the wheel command
carries a channel. A git checkout follows its remote."* That is fine for the
install the user runs, and useless for the question this module answers: **what
did stable actually ship, and can I click through it right now?**

WHY A WORKTREE, AND NOT A PIN ON THE PRIMARY CHECKOUT. Sync
fast-forwards the primary checkout (``git merge --ff-only``) and refuses to run
unless HEAD is literally :data:`repository.BASE_BRANCH`. A stable tag is
normally BEHIND main, so pinning that checkout to a lane could only work by
detaching its HEAD (which the sync guard rejects, and every ``origin/main``
comparison on the fleet row is then measuring against a ref the user did not
choose) or by resetting ``main`` backwards, which destroys work. So the lane
gets its own detached worktree instead: additive, non-destructive, and it lands
in the fleet as an ordinary row that pods and Make Live already know how to
drive.

WHAT THIS MODULE IS NOT. It never reads or writes ``$KIROCREW_HOME/channel``.
That file says which lane the user's real install FOLLOWS for updates; a pin
here says which git ref a worktree SITS ON. Coupling them would mean
materializing a stable worktree silently changed what the user's live install
downloads next — a blast radius nobody asked for. The only thing borrowed from
the update stack is vocabulary and validation.

Resolution is deliberately split from mutation, and the line is the WORKTREE:
nothing here moves a checkout, so the fleet snapshot can resolve the channel on
its refresh path without that risk. It is not ref-free -- :func:`fetch_refs`
writes remote-tracking refs and tags, which is exactly what makes a resolve
current -- but a ref update cannot strand a commit or change what a pod is
serving. The create/advance mutations live in ``worktree_ops``
beside the other worktree writers, because they take the same ``.git`` admin
lock those do.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.apps.builtins.dev_fleet import repository, runtime
from kiro_crew.apps.version import parse_version

#: Basename prefix of a release-channel worktree. The basename becomes the fleet
#: row label (``fleet_state`` uses ``Path(path).name`` verbatim) AND the pod
#: identity (``kirocrew-pod@<name>.service``), so it is spelled in full rather
#: than abbreviated: ``release-channel-stable`` reads as what it is next to a
#: ``kirocrew-wt-<slug>`` feature worktree, and the missing ``kirocrew-wt-``
#: prefix is what visually separates the two groups with no extra chrome.
#:
#: Deliberately NOT ``channel-`` — bare "channel" already means four unrelated
#: things in this codebase (agent channels in ``kiro_crew/channel.py``, messaging
#: channels, notification channels, upload document channels).
WORKTREE_PREFIX = "release-channel-"

#: The one release channel Dev Fleet materializes: the channel whose tip a git tag
#: actually names, and whose order among those tags is a fact rather than a rule
#: this feature would have to invent.
#:
#: ``nightly`` could not be it: ``nightly.yml`` builds from ``main`` HEAD on a
#: schedule and tags NOTHING, so the newest ref it could name is ``<remote>/main``
#: — which is main *now*, not the commit the last nightly published, and is where
#: the primary checkout already sits after Sync. A row promising "what nightly
#: shipped" that shows neither is worse than no row.
#:
#: ``insider`` is out by product decision, not by a missing fact — its
#: ``-insider.N`` tags do resolve. Ranking them is what has no answer: ordering
#: ``-insider.N`` against ``-rc.N`` needs a precedence between two prerelease
#: spellings that nothing in this repo states, so a prerelease channel's "tip"
#: would rest on a rule this feature invented.
#:
#: Singular on purpose. A tuple of channels, a channel parameter on every function
#: and a channel key in every request would all be shapes with one possible value,
#: and ``test_release_channel_is_a_real_channel`` pins that this name is still a
#: channel stack knows. A second channel is a change to this module's shape, made
#: when the ordering rule for prerelease tags exists to justify it.
CHANNEL = "stable"

#: A tag naming a stable release: ``v1.2.3`` and nothing after it.
_STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

#: The basename of the release-channel worktree.
WORKTREE_NAME = f"{WORKTREE_PREFIX}{CHANNEL}"


def is_release_tag(tag: str) -> bool:
    """Whether *tag* names a release this channel publishes.

    The shape check IS the classification. ``_STABLE_TAG_RE`` admits only a bare
    ``vX.Y.Z``, and every such string is stable by definition -- there is no
    prerelease suffix left for ``release_channel.channel`` to read, so deferring to
    it could only ever return the answer this already knows.
    """
    return bool(_STABLE_TAG_RE.match(tag))


def worktree_path(repo: str) -> str:
    """Where the release-channel worktree lives: a sibling of the primary checkout.

    Matches where the existing fleet already is — every ``kirocrew-wt-<slug>``
    worktree is a sibling of the primary checkout — so the new trees land in the
    directory the operator already associates with this repo instead of a second
    root they have to learn.
    """
    return str(Path(repo).parent / WORKTREE_NAME)


async def fetch_refs(repo: str, *, timeout: int = 120) -> str | None:
    """Refresh the remote-tracking ref AND the tags the channel resolves against.

    Returns ``None`` on success, else the error to report. Called before every
    resolve that must be current, so a mutation acts on the channel's real tip
    rather than on whatever tags happened to be local. The background refresher
    also fetches ``--tags``, which is what keeps the fleet ROWS honest between
    mutations; this call is what makes a Create or an Advance honest at the moment
    it runs.

    ADDITIVE, and deliberately so: a tag deleted upstream (a retracted release)
    is NOT removed locally, so it stays resolvable as a channel tip until someone
    deletes it by hand. Pruning it is not available at this cost — measured on git
    2.54, the only fetch forms that drop a remotely-deleted tag
    (``--prune --prune-tags`` with no ``--tags``, or an explicit
    ``+refs/tags/*:refs/tags/*`` under ``--prune``) delete every local-only tag
    with it, including ones the operator authored, and a pruned tag ref is not in
    any reflog. Trading an operator's own tags for retraction coverage is the
    worse bargain. Buying it properly means fetching release tags into a private
    namespace and resolving the channel there, which is a larger change than this.
    """
    remote = await repository._upstream_remote()
    rc, _out, err = await runtime._run_cmd(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "--tags",
            remote,
            repository.BASE_BRANCH,
        ],
        timeout=timeout,
    )
    if rc != 0:
        return runtime._redact((err or "").strip())[:200] or f"git fetch {remote} --tags failed"
    return None


async def resolve(*, repo: str | None = None) -> dict:
    """Resolve the channel to the ref it most recently published.

    Read-only: never fetches (call :func:`fetch_refs` first when freshness
    matters) and never touches a worktree.

    Returns ``{"ok": True, lane, ref, tag, oid, version}`` or
    ``{"ok": False, "error": ...}``.

    Resolution is to a TAG, which is why :data:`CHANNEL` is the channel it is: an
    untagged channel has no ref naming a specific published build.
    """
    if repo is None:
        repo = repository._repo()

    listed = await list_release_tags(repo)
    if listed is None:
        return {"ok": False, "error": "cannot list tags (git tag failed)"}
    return await _resolve_tagged(repo, listed)


async def list_release_tags(repo: str) -> list[str] | None:
    """Candidate release tags, newest published first. ``None`` if git failed.

    Creation order is what git is asked for because it is the order git can give
    cheaply and correctly. Re-ordering into the one that decides a tip is
    :func:`_release_candidates`' job, not this one's.
    """
    listed = await repository._git(repo, "tag", "--list", "v*", "--sort=-creatordate", timeout=20)
    if listed is None:
        return None
    return [ln.strip() for ln in listed.splitlines() if ln.strip()]


def _release_candidates(listed: list[str]) -> list[str]:
    """The channel's tags out of *listed*, best-first — the order its tip means.

    Ordered by SEMVER, descending, and creation date is NOT the same order. A
    backport cut after a newer line (``v0.4.1`` tagged after ``v0.5.0``, which is
    ordinary release practice) is the NEWEST tag by date and an OLDER release by
    version, and a re-pushed or re-created tag carries today's date for last
    year's release. Either would pin the row to a release stable users are not on.

    Stable under ties, so a repeat call resolves the same tag.
    """
    tags = [t for t in (tag.strip() for tag in listed) if t and is_release_tag(t)]
    # `parse_version` is the repo's one version parser; a second hand-rolled key
    # here would be a second place for `v0.10.0` vs `v0.9.0` to disagree. Every
    # tag reaching this line matched ``_STABLE_TAG_RE``, so it is a bare
    # ``vX.Y.Z`` and the parse cannot raise.
    return sorted(tags, key=lambda t: parse_version(t[1:]), reverse=True)


async def _resolve_tagged(repo: str, listed: list[str]) -> dict:
    """Pick the channel's tip out of an already-listed tag set."""
    for tag in _release_candidates(listed):
        oid = await repository._git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        if not oid:
            # A listed tag that will not resolve is a broken local ref, not an
            # empty channel. Keep scanning rather than reporting the channel as
            # unpublished, which would hide a real release behind one bad ref.
            continue
        # No second classification of `version` here. `is_release_tag(tag)` already
        # decided from the tag's shape, and `version` is that same `tag[1:]` — so
        # re-asking would be a tautology that can only ever agree. One decision,
        # made once, at the point that picks the tag.
        return {
            "ok": True,
            # The channel's name, for the row and the strings that name it on
            # screen. Display data, not a parameter: no request carries it back.
            "lane": CHANNEL,
            "ref": f"refs/tags/{tag}",
            "tag": tag,
            "oid": oid,
            "version": tag[1:],
        }
    return {"ok": False, "error": f"no {CHANNEL} release tag found in this checkout"}


async def worktree_state(path: str, resolved: dict) -> dict:
    """Where the worktree at *path* sits relative to the resolved channel tip.

    ``at_tip`` / ``behind`` describe distance from the CHANNEL TIP, not from
    ``BASE_BRANCH`` — a release worktree is not trying to track main, so the
    fleet's usual behind-main count would be a large number that means nothing
    on this row.

    ``version`` is the release the tree is ACTUALLY on, which is not the lane's
    resolved version: the moment a newer release ships, the resolved tip moves
    and the tree does not. A row that showed the resolved version would rename
    the operator's checkout to a build it does not contain.
    """
    # Four keys, all read by `fleet_state._release_channel`. The worktree's own
    # HEAD *oid* is still not among them — no surface shows a bare sha — but the
    # release that oid corresponds to is exactly what the row's badge claims to
    # display, so it is resolved here rather than inferred from the lane tip.
    out: dict = {"at_tip": False, "behind": None, "detached": None, "version": None}
    head = await repository._git(path, "rev-parse", "HEAD")
    # THREE states, and the primitive underneath has to carry all three or the
    # caller's three-state handling is decoration. `symbolic-ref --quiet HEAD`
    # exits non-zero for a detached HEAD, which `_git` reports as None -- but it
    # ALSO exits non-zero when the read fails outright (the directory was removed
    # while still registered in `git worktree list`, the repo is unreadable). So
    # `is None` alone published an unreadable checkout as a confirmed detached
    # lane, which is the one thing the caller must never be told: it would adopt
    # a tree of unknown shape and offer Advance on it. `rev-parse HEAD` is the
    # discriminator -- a tree whose HEAD cannot be read is not known to be
    # anything, so `detached` stays None and the caller says so.
    symref = await repository._git(path, "symbolic-ref", "--quiet", "HEAD")
    if symref is not None:
        out["detached"] = False
    elif head:
        out["detached"] = True
    tip = resolved.get("oid")
    if not head or not tip:
        return out
    if head == tip:
        out["at_tip"] = True
        out["behind"] = 0
        # Same commit as the tip, so the same release by definition: no second
        # git call to learn what this already tells us.
        out["version"] = resolved.get("version")
        return out
    count = await repository._git(path, "rev-list", "--count", f"{head}..{tip}", timeout=12)
    if count and count.isdigit():
        out["behind"] = int(count)
    out["version"] = await _release_at_head(path)
    return out


async def _release_at_head(path: str) -> str | None:
    """The version of the release tag *path*'s HEAD sits on, if it is on one.

    Filtered to release tags: a commit can carry several tags, and picking the
    first would let an unrelated tag that happens to share the commit rename the
    row. ``None`` is a real answer — the worktree is adopted for being DETACHED,
    not for being at a release, so an operator who checked out an arbitrary commit
    in it is on no release and the row must not invent one.
    """
    listed = await repository._git(path, "tag", "--points-at", "HEAD", timeout=12)
    if not listed:
        return None
    for tag in (t.strip() for t in listed.splitlines()):
        if tag and is_release_tag(tag):
            return tag[1:]
    return None


#: What OTHER Dev Fleet components read through the ``server`` facade, and
#: nothing else. Each name below has a caller outside this module; a name with
#: none is reachable as an attribute anyway (tests import the module directly),
#: so exporting it would only widen the facade's surface without widening its
#: use. ``is_release_tag`` and ``list_release_tags`` are internal for that reason.
__all__ = [
    "CHANNEL",
    "WORKTREE_NAME",
    "fetch_refs",
    "resolve",
    "worktree_path",
    "worktree_state",
]

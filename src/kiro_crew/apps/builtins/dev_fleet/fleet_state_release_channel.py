"""The release channel's row in the fleet snapshot: its resolved tip and its tree's state.

Resolution is :mod:`release_channel_pin`'s (a pure read); the row shape the dashboard
renders is decided here, apart from the rest of the fleet projection, so the channel's
three-state adoption rule (detached pin / branch checkout holding the name / unreadable)
reads as one unit and the fleet builder only asks for the finished row.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.dev_fleet import release_channel_pin, repository, runtime


async def _release_channel(worktrees: list[dict]) -> dict | None:
    """The release channel's resolved tip, and where its tree sits. ``None`` if unknown.

    Published as its OWN key rather than as an extra ``worktrees`` row. With no
    worktree yet there is no path, and every existing consumer of a ``worktrees`` entry
    -- disk measurement, pod matching, prune candidacy -- assumes one. A pathless row
    injected there would be a fabricated worktree, so the placeholder stays here and
    the frontend renders it as a ghost row instead.

    Never raises: this rides on the cached fleet snapshot, and a channel that cannot be
    resolved must not take the whole fleet view down with it. ``None`` means "nothing
    can be said about it" -- no repo, or the resolve itself blew up -- which is
    different from a resolved-but-failed channel, whose row carries the error and still
    renders.
    """
    try:
        repo = repository._repo()
    except repository.RepoUnavailable:
        return None
    name = release_channel_pin.WORKTREE_NAME
    # EVERY registered checkout carrying the reserved basename, not the last one a
    # dict comprehension happened to keep. The channel can drive exactly one tree, and
    # the page binds channel treatment to rows by name (`channelForRow`), so adopting
    # one of two same-named checkouts would dress the other -- a branch checkout, say
    # -- in the adopted tree's version badge and take its Rebase away. The same
    # invariant Create enforces (`_find_worktree` refuses an ambiguous basename, so no
    # second tree is ever created by this app) holds here for trees created by hand:
    # with more than one, nothing is adopted and the row says why.
    holders = [
        w
        for w in worktrees
        if not w.get("is_main") and w.get("path") and Path(w["path"]).name == name
    ]
    try:
        res = await release_channel_pin.resolve_tip(repo=repo)
    except Exception:  # noqa: BLE001
        runtime.logger.exception("dev-fleet: release-channel resolution failed")
        return None
    wt = holders[0] if len(holders) == 1 else None
    row: dict = {
        "lane": release_channel_pin.CHANNEL,
        # The basename this channel's worktree has or would have. Published even when
        # nothing exists yet, because the frontend labels the not-yet-created row with
        # it -- deriving the name there instead would put the naming rule on both sides
        # of the boundary, where a change to WORKTREE_NAME silently desyncs the label
        # from the real directory.
        "name": name,
        # Set only for a tree this app is willing to DRIVE as a channel pin.
        "worktree": None,
        # `ref` and `version` are what the row RENDERS. The resolved commit id and the
        # worktree's own HEAD sha are deliberately absent: nothing reads them, and a
        # field carried "for diagnostics" that no surface shows is a claim about the
        # payload's contract that no consumer keeps.
        #
        # `version` is the release THIS ROW IS ON, which differs by row kind: on a
        # placeholder it is the release Create would check out (the channel tip), and on
        # an adopted row it is the release the tree actually holds -- overwritten below.
        # `tip_version` is always the channel tip, so a stale row can name both without
        # either being ambiguous.
        "ref": res.get("ref"),
        "version": res.get("version"),
        "tip_version": res.get("version"),
        # A benign empty channel is NOT an error. The resolver marks that state
        # `unpublished`, and the row carries it apart from `error` so the frontend
        # renders it as ordinary information and leaves Create enabled. `error` is
        # reserved for a genuine git failure, which is what reaches the shared error
        # surface. A resolved channel sets neither.
        "unpublished": bool(res.get("unpublished")),
        "error": None if res.get("ok") or res.get("unpublished") else res.get("error"),
        "at_tip": None,
        # On an adopted row, `version: null` means two different things, and the badge
        # must not say the stronger one when only the weaker is known: `False` here
        # and the tree IS on no published release (the real answer, "not on a
        # release"); `True` and the release could not be READ (a timed-out or
        # unreadable `--points-at` listing), in which case the row carries the error
        # and the badge says the version is unknown. Never set on a placeholder.
        "version_unknown": False,
        # A worktree holds the reserved name but is NOT a channel pin, because it is
        # POSITIVELY known to be on a branch. Distinct from "no worktree": Create would
        # fail on the occupied path, so the UI must say what is in the way rather than
        # offering an action that cannot work.
        "name_taken_by_branch": False,
    }
    if len(holders) > 1:
        # The ambiguity is the row's state, whatever the tip resolved to: it rides on
        # `error` because that is the one field with a surface on an UNADOPTED row that
        # exists in the fleet (`channelErrorFor` -> the shared ErrorNotice), and the
        # fleet carrying the name is what suppresses the Create placeholder -- so the
        # only control on offer is the one that resolves it. Count, not paths: the
        # rows themselves show where each checkout sits.
        row["error"] = (
            f"{len(holders)} registered worktrees are named {name}; the "
            f"{release_channel_pin.CHANNEL} channel can drive only one. Rename or remove the "
            "extras (`git worktree move` / `git worktree remove` in a shell) so one remains"
        )
    if wt is not None:
        st = await _release_channel_tree_state(name, wt, res, row)
        _adopt_release_channel_row(row, st, tip_resolved=bool(res.get("ok")))
    return row


async def _release_channel_tree_state(name: str, wt: dict, res: dict, row: dict) -> dict:
    """``worktree_state`` for the tree holding the reserved name, failures absorbed."""
    try:
        return await release_channel_pin.worktree_state(wt["path"], res if res.get("ok") else {})
    except (release_channel_pin.MirrorOverflow, release_channel_pin.ReleaseLookupFailed) as exc:
        # Both can only come from the points-at probe after HEAD was readable and the
        # worktree was positively identified as detached: the tree IS the pin, its
        # release is unknown, and the row says so instead of "not on a release".
        if not row["error"]:
            row["error"] = f"{name}: {exc}; refusing to name the release this tree holds"
        return {"at_tip": False, "detached": True, "version": None, "version_unknown": True}
    except Exception:  # noqa: BLE001
        runtime.logger.exception("dev-fleet: release-channel worktree probe failed")
        return {"at_tip": False, "detached": None, "version": None}


def _adopt_release_channel_row(row: dict, st: dict, *, tip_resolved: bool) -> None:
    """Fold the tree's own state into *row*, in place.

    THREE states, not two. ``detached is None`` means the probe could not read HEAD at
    all, and neither of the other branches may claim it: adopting it would drive a tree
    of unknown shape, and reporting ``name_taken_by_branch`` would assert "this is on a
    branch" about a tree nobody read -- which also hides the row entirely behind a
    fabricated explanation. Say the truth instead: the path exists and its state is
    unknown.
    """
    detached = st.get("detached")
    if detached is True:
        row["worktree"] = row["name"]
        # The tree's OWN release, not the channel's -- and published whether or not the
        # tip resolved. It is answered from the local mirror, so a failed tip lookup is
        # no reason to withhold it; leaving it unset here labelled a checkout sitting on
        # a published release "not on a release" every time the resolver failed, which
        # is the badge saying the opposite of the truth beside an error notice. Leaving
        # the RESOLVED version here instead would rename the row to a build it does not
        # contain the moment a newer release ships. `None` when the tree is detached at
        # no release tag at all, which the row says plainly rather than papering over
        # with the tip.
        row["version"] = st.get("version")
        # Only the probe's own failure sets this; a `None` version with it unset is the
        # positive answer "detached at no release tag".
        row["version_unknown"] = bool(st.get("version_unknown"))
        if tip_resolved:
            row["at_tip"] = st.get("at_tip")
        # else: `at_tip` stays None -- with no tip there is no answer, and the row's
        # tooltip says the tip could not be resolved.
    elif detached is False:
        row["name_taken_by_branch"] = True
    elif not row["error"]:
        row["error"] = f"{row['name']} exists but its git state could not be read"


__all__ = (
    "_adopt_release_channel_row",
    "_release_channel",
    "_release_channel_tree_state",
)

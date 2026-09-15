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
serving. The create mutation lives in ``worktree_ops``
beside the other worktree writers, because they take the same ``.git`` admin
lock those do.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from kiro_crew.apps.builtins.dev_fleet import repository, runtime
from kiro_crew.apps.version import parse_version
from kiro_crew.executors import subprocess_executor

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

#: Where the remote's release tags are mirrored, and the ONLY place the channel
#: resolves from.
#:
#: ``refs/tags/`` cannot be that place. It is a shared namespace an operator writes
#: to as well: a locally cut release candidate, a bisect marker, a ``v99.0.0``
#: someone tagged to test an upgrade path. Every one of those matches
#: :data:`_STABLE_TAG_RE`, sorts newest, and would become the channel "tip" —
#: so the row would badge an unpublished commit as a shipped release and Create
#: would check it out. Nothing about a local tag distinguishes it from a fetched
#: one after the fact, so the separation has to happen when the ref is written.
#:
#: A mirror the operator has no reason to write to makes "published" a property of
#: WHERE the ref is rather than a claim about how it got there -- but only because the
#: fetch PRUNES it (see :func:`fetch_refs`). Location alone would be a statement about
#: habit, not an enforced boundary: a ref namespace is writable by anything with access
#: to this repo, so an unpruned mirror can be forged with one ``git update-ref`` and the
#: forgery outranks every real release. Pruning is what makes the namespace hold what
#: the remote advertised and nothing else.
#:
#: Because the mirror is pruned, a release retracted upstream also stops resolving on
#: the next fetch, while ``refs/tags/`` stays additive for the operator's own use.
_TAG_NS = "refs/dev-fleet/release-tags"

#: The refspec that populates :data:`_TAG_NS`. Forced, because a re-pushed tag must
#: overwrite the mirror rather than being skipped as a non-fast-forward: the mirror
#: is a cache of the remote's answer, not history anyone builds on.
#:
#: Exported because two callers fetch for this channel — :func:`fetch_refs` before a
#: mutation, and the background refresher every cycle — and a refspec spelled twice
#: is a refspec that drifts. The one that drifted would fail silently: the channel
#: would resolve against a mirror that stopped being updated, which reads exactly
#: like a repo that has published nothing new.
_TAG_REFSPEC = f"+refs/tags/v*:{_TAG_NS}/v*"

#: The basename of the release-channel worktree.
#:
#: The basename becomes the fleet row label (``fleet_state`` uses ``Path(path).name``
#: verbatim) AND the pod identity (``kirocrew-pod@<name>.service``), so it is spelled
#: in full rather than abbreviated: ``release-channel-stable`` reads as what it is
#: next to a ``kirocrew-wt-<slug>`` feature worktree, and the missing
#: ``kirocrew-wt-`` prefix is what visually separates the two groups with no extra
#: chrome.
#:
#: Deliberately NOT ``channel-`` — bare "channel" already means four unrelated
#: things in this codebase (agent channels in ``kiro_crew/channel.py``, messaging
#: channels, notification channels, upload document channels).
#:
#: One literal, not a prefix composed with :data:`CHANNEL`. That composition had
#: exactly one consumer -- this line -- so it was generality for a second channel
#: that is not shipping, and it made the name that every other surface matches on
#: (the row, the pod identity, the reserved-name guard) something a reader had to
#: assemble.
WORKTREE_NAME = "release-channel-stable"


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
    """Refresh the remote-tracking ref AND the tag mirror the channel resolves against.

    Returns ``None`` on success, else the error to report. Called before every
    resolve that must be current, so a mutation acts on the channel's real tip
    rather than on whatever the mirror held a cycle ago. The background refresher
    fetches the same things every cycle, which is what keeps the fleet ROWS
    honest between mutations; this call is what makes a Create honest at the moment
    it runs.

    ``--tags`` is kept beside :data:`_TAG_REFSPEC` rather than replaced by it, but the
    two run as SEPARATE fetches because only one of them is load-bearing. The refspec
    populates the private mirror the channel resolves from and its failure is fatal;
    ``--tags`` keeps ``refs/tags/`` current for the operator's own git — the reported
    ``ref`` is a real ``refs/tags/vX.Y.Z`` they can check out, and dropping it would
    leave newly published tags invisible to every ordinary command they run in this
    repo. The channel reads only the mirror, so a tag that reaches ``refs/tags/`` by
    any other route cannot become a tip.

    Splitting them is what keeps a benign local state from blocking the feature. The
    refspec is forced; ``--tags`` is not, so a local tag that diverged from the
    remote's tag of the same name makes git report
    ``! [rejected] vX.Y.Z -> vX.Y.Z (would clobber existing tag)`` and exit non-zero
    even when the mirror refspec succeeded. Reported as one status that would refuse
    every Create for an operator who ever cut a release-shaped tag of their own, so the
    courtesy fetch is best-effort and only the mirror fetch decides.

    **``--prune`` is what makes the mirror a fact rather than a habit, and it is
    load-bearing for two distinct jobs -- neither of which is Create's integrity.** A ref
    namespace is writable by anything with access to this repo -- an agent with git is
    exactly that -- so without pruning, a hand-written
    ``refs/dev-fleet/release-tags/v999.0.0`` outranks every real release by semver and
    survives every additive fetch. What that costs is, first, the FLEET ROWS: the display
    path resolves out of this same mirror and has no provenance backstop, so a forged ref
    is badged as the stable release there. Second, it costs Create's AVAILABILITY: the
    forgery is picked by every resolve and refused by every provenance check, so Create
    stays refused until someone hand-runs ``git update-ref -d``. Create's INTEGRITY is
    owned by :func:`advertised_release_commits`, which asks the configured upstream what it
    publishes and refuses any other oid, so a forged mirror ref cannot reach a checkout
    whether or not the fetch pruned. Pruning deletes any mirror ref the remote does not
    advertise, so the namespace holds what the remote published and nothing else.

    ``--prune`` is NOT ``--prune-tags``, and conflating the two is what left this open.
    Pruning is scoped to the DESTINATIONS of the refspecs actually given, which
    ``git-fetch(1)`` states directly: a tag fetched only via ``--tags`` is not subject to
    pruning, while the destination of an explicit refspec is. The split makes that
    scoping structural rather than argued — the pruning fetch names the mirror refspec
    and the base branch and never mentions tags at all, so it deletes a forged mirror
    ref and a tag the remote retracted, and ``refs/tags/`` is not among its destinations
    for a LOCAL-ONLY tag the operator authored to be reachable by. It is ``--prune-tags``
    that contributes an implicit ``+refs/tags/*:refs/tags/*`` and destroys the operator's
    own tags -- that flag is still refused, and its absence is asserted by a test. A stale
    remote-tracking branch is likewise left alone. This follows from the documented
    scoping rule rather than from one version's behaviour, and ``--prune-tags`` is the
    opt-in shorthand git 2.17 ADDED, so no older git prunes ``refs/tags/`` more eagerly
    than this.

    So retraction is handled here rather than deferred: the channel stops resolving a
    yanked release on the next fetch, while ``refs/tags/`` stays additive so the
    operator can still see and check out a tag locally after upstream drops it.
    """
    remote = await repository._upstream_remote()
    rc, _out, err = await runtime._run_cmd(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "--prune",
            remote,
            repository.BASE_BRANCH,
            _TAG_REFSPEC,
        ],
        timeout=timeout,
    )
    if rc != 0:
        detail = runtime._redact((err or "").strip())[:200]
        return detail or f"git fetch {remote} {_TAG_REFSPEC} failed"
    # Courtesy only: keeps refs/tags/ current for the operator's own git. Deliberately
    # unchecked -- an unforced tag update is rejected when a local tag of the same name
    # diverged, and that is the operator's tag to keep, not a reason to refuse a Create
    # whose mirror already resolved.
    await runtime._run_cmd(
        ["git", "-C", repo, "fetch", "--tags", remote],
        timeout=timeout,
    )
    return None


_FILTER_KEY_RE = re.compile(r"^filter\.(?P<name>.+)\.(process|smudge|clean)$", re.IGNORECASE)

# Stands in for a key name when a scope will not answer. Treated as "refuse": a
# scope that cannot be read cannot be proven filter-free.
_FILTER_PROBE_FAILED = "an unreadable git config"


async def repo_supplied_filter(repo: str) -> str:
    """Name of a repo-supplied content filter a checkout would run, else ``""``.

    Checking a tag out runs whatever ``filter.<name>.process``/``.smudge``/``.clean``
    driver applies to the files it writes, and that driver is an arbitrary command.
    The key space is unbounded -- ``<name>`` is attacker-chosen -- so
    ``_GIT_ENV_NEUTRALIZERS`` cannot cover it the way it covers the fixed
    ``core.hooksPath``/``core.fsmonitor``/``credential.helper`` keys. Hence a
    refusal rather than a neutralizer, following
    ``dashboard/handlers/worktree.py::_checkout_filter``.

    WHAT THIS CAN AND CANNOT COME FROM. A driver is only ever defined in a config
    FILE. ``.gitattributes`` can *name* a filter for a path but cannot say what it
    runs, and a fetch carries refs and objects, never config -- so the release being
    checked out cannot introduce one, and a tag that names a filter with no driver
    defined is inert. What this probe therefore refuses is a declaration already
    present in THIS checkout's own config, which is the case worth refusing: that
    config is writable by anything with the agent's filesystem access.

    SCOPES. ``--local`` (``.git/config``), plus ``--worktree``
    (``config.worktree``) when ``extensions.worktreeConfig`` makes that scope live.
    ``--includes`` is mandatory on both: for a specific-scope query git defaults
    include-following OFF, so a driver reached through ``include.path`` resolves at
    checkout time while staying invisible to a probe without it. Global and system
    config are deliberately NOT probed -- that is the operator's own machine
    (``git lfs install`` writes ``filter.lfs.*`` there), not something a repository
    supplies, and refusing it would break every host with git-lfs installed.

    FILTERS ONLY, not merge drivers. This module's one mutation checks a tag out and
    never merges, so a ``merge.<name>.driver`` has no path to execution here; the
    sibling that does merge (``md_notebook/git_ops.py::repo_supplied_driver``) covers
    both because both apply to it.

    Fails CLOSED: a probe that errors returns a refusal, because an unread scope is
    not a clean one.

    Every read here runs at the ``strict`` tier, which is load-bearing rather than
    tidy. ``--includes`` exists precisely so the probe FOLLOWS repository-controlled
    ``include.path``, so these are the calls most exposed to config this checkout does
    not author -- and they need no network, so they need no credentials. The standard
    tier would hand them the gateway's trusted credential helpers for nothing.
    :func:`fetch_refs` and :func:`advertised_release_commits` stay standard because
    they talk to the remote and the helper is how they authenticate.
    """
    scopes = ["--local"]
    # `--bool` is mandatory, not cosmetic. Git accepts `yes`, `on`, `1` and a
    # VALUELESS key as true, and a raw `--get` returns each of those verbatim (the
    # valueless form returns an empty string), so comparing the raw text against
    # "true" skips the worktree scope for four spellings that all enable it --
    # measured against real git. `--bool` normalizes every one to `true`/`false`.
    rc, out, _err = await runtime._run_cmd(
        [
            "git",
            "-C",
            repo,
            "config",
            "--local",
            "--includes",
            "--bool",
            "--get",
            "extensions.worktreeConfig",
        ],
        timeout=20,
        mode="strict",
    )
    if rc == 0 and out.strip() == "true":
        # The extension makes git READ `$GIT_DIR/config.worktree`; it does not make
        # the file exist. Probing a scope whose file is absent is `fatal: unable to
        # read config file` (exit 128, stderr set), which the fail-closed branch
        # below would read as a broken probe and refuse every create on a repo that
        # merely enables the extension. Treating 128 as benign instead would fail
        # OPEN, because a malformed config is also 128 -- so the file's existence is
        # what decides whether the scope is probed at all. `--absolute-git-dir`
        # resolves the per-worktree `$GIT_DIR`, which is where a linked worktree
        # keeps its own copy.
        rc_dir, gitdir, _e = await runtime._run_cmd(
            ["git", "-C", repo, "rev-parse", "--absolute-git-dir"], timeout=20, mode="strict"
        )
        if rc_dir != 0:
            return _FILTER_PROBE_FAILED
        worktree_config_file = Path(gitdir.strip()) / "config.worktree"
        if await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), worktree_config_file.exists
        ):
            scopes.append("--worktree")
    for scope in scopes:
        rc, out, err = await runtime._run_cmd(
            ["git", "-C", repo, "config", scope, "--includes", "--name-only", "--list"],
            timeout=20,
            mode="strict",
        )
        if rc != 0:
            # A scope holding no keys exits non-zero and says nothing at all. That
            # is an empty scope, not an unreadable one, and a fresh
            # ``config.worktree`` is routinely empty -- so only a scope that also
            # produced output or an error message counts as a failed probe.
            if not out.strip() and not err.strip():
                continue
            return _FILTER_PROBE_FAILED
        for key in out.splitlines():
            candidate = key.strip()
            if _FILTER_KEY_RE.match(candidate):
                return candidate[:120]
    return ""


async def advertised_release_commits(repo: str) -> tuple[dict[str, str] | None, str | None]:
    """``({tag: commit oid}, None)`` as the CONFIGURED remote advertises it, or ``(None, error)``.

    The mirror namespace is writable by anything with access to this checkout, and
    ``--prune`` on the fetch only deletes refs the remote does not advertise AT FETCH
    TIME -- it cannot touch a ref written after that subprocess exits. So a ref planted
    between the fetch and the read that resolves the tip is pruned by no fetch that has
    already run, and ranks first if its version is high enough. This asks the upstream
    remote what it publishes, so "published" is a verified fact about that remote rather
    than an inference from where a local ref sits.

    The remote is the one named in this repository's own git config, and its URL is read
    from that config by git at query time. So the guarantee is scoped to a writer who can
    plant a local REF: it binds the checkout to what the configured upstream advertises,
    and it is not a defence against a writer who can rewrite ``remote.<name>.url``. Such a
    writer already redirects every fetch this app performs -- including the sync that
    rebases a worktree onto ``<remote>/main`` -- so a URL trust anchor would have to be
    operator-owned state shared by all of those call sites, which this app does not have.

    Queried with the ``refs/tags/v*`` GLOB, deliberately, not one exact ref per tag: for
    an EXACT pattern git prints only the ref's own object, while the glob also prints the
    ``^{}`` peel line. An annotated tag -- how a release is normally cut -- has a tag
    OBJECT at ``refs/tags/<t>`` and its commit only on that peel line, so an exact query
    would compare a tag object against a commit and reject every annotated release.

    A tag the remote does not carry is NOT an error from git: ``ls-remote`` exits 0 and
    prints nothing. Absence therefore has to be read from the map by the caller, and a
    non-zero exit is reserved for the query itself failing, which fails CLOSED.
    """
    remote = await repository._upstream_remote()
    rc, out, err = await runtime._run_cmd(
        ["git", "-C", repo, "ls-remote", "--tags", remote, "refs/tags/v*"],
        timeout=60,
    )
    if rc != 0:
        detail = runtime._redact((err or "").strip())[:200]
        return None, detail or f"git ls-remote {remote} failed"

    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        oid, ref = parts
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/") :]
        if name.endswith("^{}"):
            peeled[name[:-3]] = oid
        else:
            direct[name] = oid
    # The peel wins wherever it exists: that is the commit an annotated tag names,
    # and the plain entry there is the tag object, which no checkout resolves to.
    return {tag: peeled.get(tag, oid) for tag, oid in direct.items()}, None


async def resolve(*, repo: str) -> dict:
    """Resolve the channel to the ref it most recently published.

    Read-only: never fetches (call :func:`fetch_refs` first when freshness
    matters) and never touches a worktree.

    Returns ``{"ok": True, lane, ref, oid, version}`` on success. A failure is
    ``{"ok": False, "error": ...}``, and the two failure kinds are told apart by an
    ``unpublished`` flag: a benign empty mirror -- a checkout that has fetched no
    release tag -- carries ``"unpublished": True``, while a git failure carries no
    such flag. A reader that branches only on ``ok`` sees no change.

    Resolution is to a TAG, which is why :data:`CHANNEL` is the channel it is: an
    untagged channel has no ref naming a specific published build.
    """
    listed = await list_release_tags(repo)
    if listed is None:
        return {"ok": False, "error": "cannot list tags (git tag failed)"}
    return await _resolve_tagged(repo, listed)


async def list_release_tags(repo: str) -> list[str] | None:
    """Candidate release tags from the mirror, unordered. ``None`` if git failed.

    Reads :data:`_TAG_NS`, never ``refs/tags/``. That is the whole publication check:
    a tag is a channel candidate because the mirror holds it, and the mirror holds
    only what a fetch brought from the remote. ``git tag --list`` was the same query
    against a namespace the operator also writes to, so a locally cut ``v99.0.0``
    outranked every real release and became the tip.

    Deliberately UNORDERED. :func:`_release_candidates` re-sorts by semver, and a
    tag order asked for here would have no consumer -- the previous
    ``--sort=-creatordate`` was exactly that, and worse than inert: it read like
    the order the tip is chosen by, which is the bug this module already fixed once
    (a backport cut after a newer line is newer by DATE and is not the tip).

    An EMPTY mirror is not an error and must not be reported as one. It is the state
    of a checkout that has not fetched yet, and the caller renders it the same way as
    a repo that has published nothing -- which is honest, because an unfetched mirror
    is precisely a checkout that does not know what is published. The next refresher
    cycle fills it. ``None`` is reserved for git itself failing, which is a different
    row (an error the operator is shown) and must stay distinguishable.
    """
    listed = await repository._git(
        repo, "for-each-ref", "--format=%(refname:strip=3)", f"{_TAG_NS}/", timeout=20
    )
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
        oid = await repository._git(repo, "rev-parse", f"{_TAG_NS}/{tag}^{{commit}}")
        if not oid:
            # A listed tag that will not resolve is a broken mirror ref, not an
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
            # No `tag` key beside these. It is `ref` without its `refs/tags/`
            # prefix and `version` with a `v`, so a third spelling of one fact
            # would be a payload contract no consumer keeps -- the same reason the
            # fleet row omits the resolved commit id.
            #
            # `refs/tags/`, NOT the mirror this resolved from. The mirror is an
            # implementation detail of deciding what is published; what the operator
            # is shown has to be the ref they can act on, and `git checkout
            # refs/dev-fleet/release-tags/v1.2.3` is not a thing anyone types.
            # `--tags` keeps that ref real locally, so the payload is not a promise
            # about a ref that only exists here.
            "ref": f"refs/tags/{tag}",
            "oid": oid,
            "version": tag[1:],
        }
    # An empty channel is benign, and the flag says so: this marks the state apart
    # from a git failure so the fleet row can render it as ordinary information
    # rather than an incident. A checkout with an empty mirror has fetched no
    # release tag, and a Create fetches before it resolves, so the state is
    # actionable rather than broken.
    return {
        "ok": False,
        "unpublished": True,
        "error": f"no {CHANNEL} release tag found in this checkout",
    }


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
    # a tree of unknown shape as a channel pin. `rev-parse HEAD` is the
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

    Reads the mirror, for the same reason resolution does: ``git tag --points-at``
    would let a tag the operator wrote rename the row, so a local ``v99.0.0`` on the
    checked-out commit would badge it as a shipped release. The mirror answers
    "published", and that is the claim the badge makes.

    The mirror lives in the shared ref store, so it is visible from a linked worktree
    -- only HEAD and a few ``refs/worktree`` paths are per-worktree -- which is why
    this can run against *path* rather than having to be handed the primary checkout.

    Filtered to release tags: a commit can carry several tags, and picking the
    first would let an unrelated tag that happens to share the commit rename the
    row. ``None`` is a real answer — the worktree is adopted for being DETACHED,
    not for being at a release, so an operator who checked out an arbitrary commit
    in it is on no release and the row must not invent one.
    """
    listed = await repository._git(
        path,
        "for-each-ref",
        "--points-at",
        "HEAD",
        "--format=%(refname:strip=3)",
        f"{_TAG_NS}/",
        timeout=12,
    )
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
    "advertised_release_commits",
    "fetch_refs",
    "repo_supplied_filter",
    "resolve",
    "worktree_path",
    "worktree_state",
]

"""Find the aliases of a masked crew-home leaf: hardlinks and linked components.

A mask hides one path; a hardlink or a linked parent reaches the same inode by another
name. This module holds which protected leaves tolerate, degrade on or refuse such an
alias, walks the data home for them, and refuses a launch that would leave a masked
credential reachable. One of the owners ``kiro_crew.sandbox`` is carved
into: every name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
from pathlib import Path
from typing import NamedTuple

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES
from kiro_crew.sandbox_leaves import (
    _CREW_HIDDEN_LEAVES,
    _CREW_HOME_PREFIXES,
    _MD_NOTEBOOK_RETIRED_STATE_LEAVES,
    _MD_NOTEBOOK_STATE_LEAVES,
    MD_NOTEBOOK_APP_NAME,
)
from kiro_crew.sandbox_seals import SandboxCeilingUnsealable, _symlink_target_display
from kiro_crew.terminal_safe import safe_terminal_line

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


#: Masked leaves where a SYMLINK is tolerated, and why. Every other entry in
#: :data:`_CREW_HIDDEN_LEAVES` refuses one through :func:`_refuse_aliased_masked_leaves`,
#: so this set is the whole exception list and each member states its own reason.
#:
#: Tolerated means NOT REFUSED, never NOT LOOKED AT. The pass visits these leaves and logs a
#: warning, because the alias really is outside the mask: excluding them from the walk would
#: reproduce, on the credential leaf, exactly the silence the pass exists to end.
#:
#: * ``.env`` -- the operator's OWN channel-credential file, authored by hand and
#:   documented as such (``docs/architecture/overview.md`` lists it as "channel tokens,
#:   owner id"). It is the clearest member of the class ``_warn_if_alias_backed`` exists
#:   for: a dotfile manager (chezmoi, stow) symlinks exactly this file, so refusing it
#:   would turn an ordinary setup into a spawn failure for every agent on the host.
#:
#: Deliberately NOT here, having been checked for a supported second name and found to
#: resolve to one managed path on the platforms this mask runs, so a link is not a
#: relocation the product offers here:
#:
#: * ``scratch`` -- ``agent_scratch.scratch_root()`` honours a ``KIROCREW_SCRATCH_ROOT``
#:   override, but only on Windows, where no Crew OS sandbox (and so no mask) runs; on
#:   Linux and macOS the override is ignored and ``scratch_root()`` is always
#:   ``config_dir() / "scratch"``, so the mask's scratch root is always the default;
#: * ``work`` -- ``work_root.work_root()`` is ``config_dir() / "work"``, and the module
#:   refuses a linked root at allocation and sweeps nothing through one;
#: * ``backup`` -- no resolver in the tree reads an override for it either.
#:
#: The HARDLINK shape is tolerated for every leaf, which is why it is a property of the
#: pass rather than an entry here: see :func:`_refuse_aliased_masked_leaves`.
_CREW_ALIAS_TOLERATED_LEAVES: frozenset[str] = frozenset({".env"})

#: Masked leaves where a planted link at an INTERMEDIATE component DEGRADES instead of
#: refusing, because a sibling control already answers that case.
#:
#: The Notes builtin's retired bare state names under ``workspace/md-notebook``, the only
#: multi-component md-notebook leaves left (the live state directory is a top-level name,
#: so a link AT it refuses like any other). Nothing writes these names any more, and when
#: their chain holds a planted link :func:`_md_notebook_degraded_mask_dirs` masks the whole
#: ``workspace/md-notebook`` directory and the Notes backend does not start, so a stale copy
#: under such a chain is hidden either way. Refusing
#: the spawn instead would let one optional app's on-disk layout take every sandboxed
#: process on the host down with it -- an operator who symlinks ``workspace/`` to another
#: disk would find no agent could start, over a file they may never have created.
#:
#: Every OTHER multi-component masked leaf refuses, because no such compensating control
#: exists for it: nothing withholds anything when ``apps/aws-control`` is a link, so the
#: mask binds the referent while the writable alias name persists.
_CREW_ALIAS_CHAIN_DEGRADE_LEAVES: frozenset[str] = frozenset(_MD_NOTEBOOK_RETIRED_STATE_LEAVES)

#: Masked leaves where an extra HARD LINK refuses the spawn rather than warning.
#:
#: The mask binds a PATH, so it does not follow the inode: a second name on the same inode
#: is an unmasked way to the same bytes, for reading AND for writing, and no check on the
#: masked name can see it. That is the same reasoning
#: :func:`_refuse_unless_sole_regular_link` already applies to the live-target pointer,
#: where ``st_nlink != 1`` refuses; this set carries it to the leaves whose bytes are
#: themselves a usable secret.
#:
#: MEMBERSHIP RULE, so a leaf added later inherits a decision instead of silence: the
#: leaf's bytes are usable off this host on their own -- a signing key, a bearer token, a
#: session cookie, a password. A leaf masked for INTEGRITY instead, where the harm is an
#: agent WRITING it, stays a warning: the write alias is real, but those leaves each have a
#: reader that re-validates the content, and refusing on them would widen the spawn-failure
#: surface past the bytes an attacker can simply walk away with.
#:
#: Only a REGULAR FILE can carry a second hard link -- ``link(2)`` refuses a directory --
#: so every directory leaf is outside this decision by shape rather than by judgement, and
#: no entry here needs to name one. A file INSIDE a masked directory can be named, though:
#: the mask hides the name, not the inode.
#:
#: * ``token_signing.key`` -- signs dashboard access and refresh tokens. The bytes forge
#:   any session cookie, so a read is a full authentication bypass.
#: * ``refresh_chains.json`` -- the refresh-token chain state that decides which refresh
#:   presentations are still live; a read continues an operator's session.
#: * the auth SQLite store and its WAL, SHM and journal sidecars -- its own entry in
#:   :data:`_CREW_HIDDEN_LEAVES` states the reason this set needs: the bytes ARE a live
#:   bearer token, and the sidecars hold the same bytes mid-transaction.
#: * ``.env`` -- the operator's channel credentials, the live Slack, Discord and Telegram
#:   tokens. It is TOLERATED for a symlink in :data:`_CREW_ALIAS_TOLERATED_LEAVES` and
#:   deliberately NOT tolerated here: that tolerance exists for the layout a dotfile
#:   manager produces, and chezmoi and stow produce a SYMLINK or a copy. A hard link on
#:   this file is not that supported layout, and it is the highest-value secret in the home.
#: * the Notes personal access token -- a live bearer credential for the operator's
#:   repositories. Reachable from inside the sandbox rather than only by pre-planting: the
#:   Notes backend is itself a sandboxed spawn holding a legitimate window on this leaf, so
#:   one agent can create the second name and an ordinary session then reads the token.
#: * ``ops_mission_control_secrets.json`` -- the app's credential store by name.
#: * ``browser-cookies.txt``, ``playwright-storage-state.json`` and
#:   ``playwright-extension-token`` -- browser session material: site cookies, the storage
#:   state that carries them plus localStorage tokens, and the extension's bearer token.
#:   Retired leaves with no reader left in the tree, kept here for the reason
#:   ``.kiro_cli_binary_trust.json`` gives for being masked at all -- a backup restore can
#:   resurrect one, and a restored cookie jar is a live credential again.
#:
#: The cost is stated rather than hidden: an extra hard link refuses EVERY agent spawn on
#: the host, including when the second name sits outside the sandbox where it is harmless,
#: because ``st_nlink`` reports that a second name exists and not where it is.
#: :func:`masked_credential_leaf_aliases` is what keeps that from arriving as an
#: unexplained outage -- ``kirocrew doctor`` reports the condition before a spawn refuses
#: on it, the same answer the live-target pointer's own read already gives for the same
#: shape, and the refusal names the ``find -samefile`` command that locates the other name.
_CREW_HARDLINK_REFUSED_LEAVES: frozenset[str] = frozenset(
    {
        "token_signing.key",
        "refresh_chains.json",
        AUTH_SQLITE_DB,
        *(f"{AUTH_SQLITE_DB}{suffix}" for suffix in AUTH_SQLITE_SIDECAR_SUFFIXES),
        ".env",
        # The live PAT inside the wholly-masked state directory: a hard-linking backup
        # of the data home makes a second, unmasked name for it all the same. And its old
        # bare spelling, which may still hold a token on a host not yet moved.
        _MD_NOTEBOOK_STATE_LEAVES[0],
        f"workspace/{MD_NOTEBOOK_APP_NAME}/pat",
        "ops_mission_control_secrets.json",
        "browser-cookies.txt",
        "playwright-storage-state.json",
        "playwright-extension-token",
    }
)


#: The tolerated leaves must BE masked leaves -- an entry naming something outside
#: :data:`_CREW_HIDDEN_LEAVES` would be an exception to nothing, and would read as a
#: permission the pass never actually grants. Pinned by
#: ``test_the_tolerated_set_names_only_masked_leaves`` rather than a module-level
#: ``assert``, which ``python -O`` strips and which would make the invariant hold only
#: in the builds that happen not to be optimised.


def _first_linked_component_below(root: str, leaf: str) -> str | None:
    """The first component of *leaf* under *root* that is a link, or ``None``.

    ROOT-FIRST, stopping at the first hit, which is the safety property rather than a
    detail: each test runs only after every component above it is known not to be a link, so
    the probe itself never traverses one. The same order and reason as
    :func:`platform_compat.first_linked_ancestor`, which cannot be used directly here
    because it walks EVERY ancestor, including the data home and its parents --
    ``config_dir()`` documents that a symlinked data HOME is supported, so refusing that
    would break a layout the product allows.

    The leaf's own final component is excluded: the caller judges that with ``lstat``, and a
    link there is the leaf-alias case rather than the ancestor case.

    **Fails CLOSED on a component it cannot read**, raising rather than answering "not a
    link". ``os.path.islink`` (and so ``is_link_or_junction``) answers False when the stat
    fails, which makes "cannot tell" indistinguishable from "safe" -- and the data home is
    agent-writable, so an agent can strip search permission from a directory it owns and turn
    the whole walk into a silent pass. ABSENT is the one benign failure: a component that is
    not there means nothing below it exists, so there is no alias to find.
    """
    parts = leaf.replace(os.sep, "/").split("/")[:-1]
    walked = root
    for part in parts:
        if not part or part == ".":
            continue
        walked = os.path.join(walked, part)
        try:
            mode = os.lstat(walked).st_mode
        except FileNotFoundError:
            # Nothing below an absent component can exist, so nothing is aliased.
            return None
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot stat {safe_terminal_line(walked)} to check whether a masked path "
                f"passes through a link: {safe_terminal_line(str(exc))}. Refusing rather "
                "than assuming it is a real directory, because the mask would bind whatever "
                "this component resolves to."
            ) from exc
        if stat.S_ISLNK(mode) or platform_compat.is_link_or_junction(walked):
            return walked
    return None


def _masked_crew_home_roots() -> list[str]:
    """Every crew data home the masks cover, de-duplicated, live one first.

    The mask lists are NOT scoped to the live home, and that is the whole reason this
    helper exists. :func:`_crew_home_entries` expands each hidden leaf across both
    :data:`_CREW_HOME_PREFIXES`, so ``~/.kiro/crew/.env`` and ``~/.kirocrew/.env`` are
    both masked whichever one ``config_dir()`` resolves to, and
    :func:`_relocated_crew_targets` adds the resolved home on top when ``KIROCREW_HOME``
    moves it out from under ``$HOME``. A check that resolves ``config_dir()`` alone
    therefore judges one of the homes the launcher masks and none of the others.

    Ordered live-home-first so a refusal names the home the operator is most likely
    looking at, and de-duplicated so a relocation that happens to coincide with a prefix
    is visited once. Live-first is also what keeps the occupant pass and the launcher's
    lists in step: both spell a symlinked home's leaves the resolved way, the pass by
    walking this root and the builder by folding the ``$HOME`` spellings onto it.

    De-duplicated by DIRECTORY INODE, not by path string. A host part-way through a migration
    legitimately points the legacy spelling AT the live home -- this module's own alias pass
    documents that layout as one that must keep spawning -- and two strings for one directory
    make every walk over them report each entry twice. The alias accounting compares a count
    of located names against ``st_nlink``, so a doubled entry overshoots it and refuses a spawn
    over a single link. A root that cannot be stat'ed keeps its place: absence is a state every
    caller here already handles, and dropping it would silently narrow the set.

    Never raises. A home that cannot be resolved contributes nothing and the remaining
    roots still apply, because every caller here is a read that reports what it finds --
    a probe that cannot resolve a path must not turn that into a verdict about the host.
    """
    roots: list[str] = []
    try:
        roots.append(str(config_dir()))
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for the masked-leaf pass", exc_info=True)
    try:
        home = Path.home()
        roots.extend(str(home / Path(prefix)) for prefix in _CREW_HOME_PREFIXES)
    except Exception:  # pragma: no cover - defensive
        logger.debug("could not resolve $HOME for the masked-leaf pass", exc_info=True)
    unique: list[str] = []
    seen_ids: set[tuple[int, int]] = set()
    for root in dict.fromkeys(roots):
        try:
            info = os.stat(root)
        except OSError:
            # Absent, or unreadable to this process. Keep it: every caller treats an absent
            # home as contributing nothing, and a stat failure is not grounds to narrow the
            # set a refusal is measured against.
            unique.append(root)
            continue
        key = (info.st_dev, info.st_ino)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        unique.append(root)
    return unique


#: How many directory entries the alias walk may visit under ONE home before giving up.
#: The walk exists to name the OTHER path to a credential's bytes, and it runs only once a
#: masked leaf already carries a second hard link -- a state a healthy host never reaches --
#: so the cap is not a throughput budget. It is a refusal to be walked into an unbounded tree
#: by whoever controls the home's contents: the data home holds agent-writable subtrees, so
#: without a cap a planted directory of a million entries turns every spawn into a stall.
#: Exhausting it returns what was found so far, because a partial answer still names real
#: aliases and the caller's fallback covers the rest.
_ALIAS_WALK_MAX_ENTRIES = 20000


class _AliasMask(NamedTuple):
    """One discovered credential alias, with the inode it was when it was discovered.

    The path alone is not enough for the launcher to act on. Discovery runs in the parent and
    the bind runs later in the child, so a same-uid process can rename the alias and leave an
    unrelated file at that path in between -- and a decoy is one ``ln`` away from carrying two
    links, so neither the mode nor the link count tells it apart. Comparing the device and
    inode numbers does, because the identity is what discovery actually established.
    """

    path: str
    dev: int
    ino: int


class _InodeAliases(NamedTuple):
    """Other names for one inode under one crew home, split by whether a mask covers them.

    The split is the whole point. ``unmasked`` is what a caller must hide to close the hole;
    ``masked`` is what is already unreachable. Both count as LOCATED, so a caller deciding
    whether every link is accounted for must add them together -- reporting only ``unmasked``
    is what would make an all-masked leaf look short a name and refuse a spawn that is
    already safe.
    """

    unmasked: list[str]
    masked: list[str]


def _inode_aliases_under(root: str, target: str, info: os.stat_result) -> _InodeAliases:
    """Other names under *root* for *target*'s inode, split into unmasked and masked.

    The credential masks bind PATHS, so a second hard link to a masked leaf is a second way
    to the same bytes and the mask says nothing about it. Naming that path is what lets a
    caller close the hole by masking it too, instead of choosing between refusing every spawn
    and leaving the bytes readable.

    Only called when *target* already has ``st_nlink > 1``: the link count IS the trigger, and
    the caller tests it explicitly rather than relying on the walk to come back empty.

    Bounded and fail-soft by construction, because the tree is not this process's to trust:

    * ``os.walk`` with ``followlinks=False``, so a planted directory symlink cannot redirect
      the walk out of the home;
    * every entry judged with ``lstat``, so a symlink is never followed to its target's inode;
    * a cap of :data:`_ALIAS_WALK_MAX_ENTRIES` entries, returning what was found so far;
    * any entry that cannot be stat'ed is skipped -- a walk is a read, and a path this
      process cannot classify must not become a verdict about the host.

    A path a mask already covers goes in ``masked``, not ``unmasked``: it is another masked
    leaf, or it sits inside a whole-directory mask, so the bytes are unreachable under it and
    hiding it again buys nothing. It is still REPORTED, because a caller that never heard of
    it cannot tell an accounted-for leaf from one whose other name is somewhere unreachable.
    The auth-store staging link this module deliberately keeps when ``fsync_dir`` raises is
    exactly such a name, so dropping it silently refuses the spawn it was kept to rescue.
    """
    want = (info.st_dev, info.st_ino)
    masked_leaves = {os.path.join(root, leaf) for leaf in _CREW_HIDDEN_LEAVES}
    masked_dirs = tuple(
        os.path.join(root, leaf) + os.sep for leaf in _CREW_HIDDEN_LEAVES if "/" not in leaf
    )
    found = _InodeAliases(unmasked=[], masked=[])
    visited = 0

    def _capped() -> bool:
        """True once the cap is spent, saying so exactly once from ONE place.

        Two call sites reach the cap -- the file loop and the directory count -- and an
        earlier shape logged from only one of them, so a tree that hit the cap on
        directories returned a partial answer that read as a complete one.
        """
        if visited <= _ALIAS_WALK_MAX_ENTRIES:
            return False
        logger.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the word "credential" names what is being searched FOR; the arguments are a sanitised path and an integer cap, and no file is opened.  # noqa: E501  # fmt: skip
            "sandbox: stopped the credential alias walk under %s after %d entries; "
            "any alias beyond that point is not named here.",
            safe_terminal_line(root),
            _ALIAS_WALK_MAX_ENTRIES,
        )
        return True

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Directories count too. Counting only files let a subtree that is mostly
        # directories -- a deep agent-writable tree with few leaves in it -- pass the
        # cap untouched and enumerate in full, once per multilinked credential leaf per
        # crew home, on the spawn path this cap exists to bound.
        visited += len(dirnames)
        for name in filenames:
            visited += 1
            if _capped():
                return found
            candidate = os.path.join(dirpath, name)
            if candidate == target:
                continue
            try:
                other = os.lstat(candidate)
            except OSError:
                continue
            if not stat.S_ISREG(other.st_mode):
                continue
            if (other.st_dev, other.st_ino) != want:
                continue
            if candidate in masked_leaves or candidate.startswith(masked_dirs):
                found.masked.append(candidate)
            else:
                found.unmasked.append(candidate)
        if _capped():
            # Clearing dirnames stops the descent as well: os.walk has already been handed
            # this directory's children, so returning alone would end THIS walk while a
            # generator resumed elsewhere would keep going.
            dirnames[:] = []
            return found
    return found


def _refuse_multilinked_credential_leaves(
    *, masks_are_path_only: bool = False
) -> tuple[_AliasMask, ...]:
    """Close a second hard link on a masked CREDENTIAL leaf, and return what to mask.

    The masks bind PATHS, so a second name to a masked leaf is a second way to the same
    bytes that the mask says nothing about. Three outcomes, in the order they are preferred,
    because only the first one costs nothing:

    * EVERY other name is FOUND under a data home -- returned, for the caller to add to this
      spawn's hidden set. The bytes become unreachable in every namespace, which is what a
      refusal was standing in for, and no spawn is refused. This is the outcome that resolves
      the tension the other two trade against. "Every" is the load-bearing word and is
      COUNTED, not assumed: the leaf's own name plus the located ones must equal
      ``st_nlink``. Masking one name out of two remaining leaves the other readable while
      reporting the hole closed, and a walk that stopped at its cap reaches this same test
      with a short count.
    * a name is NOT under any data home, or not every name could be located -- the snapshot
      and backup case (``cp -al``, rsnapshot, ``rsync --link-dest``), which cannot be masked
      because it cannot be located without walking the filesystem. REFUSED in the live home,
      which is the cost this control has always accepted there, and reported elsewhere.
      Whatever WAS located is masked on the way, since a located name left readable would be
      strictly worse, but it does not buy the first outcome.
    * the leaf is absent, is a link, or is not a regular file -- skipped. Only a regular file
      can carry a second hard link, and ``lstat`` never follows a link, so the symlink case
      belongs to the pass that has a sentence for it.

    Every masked home is INSPECTED, not only the live one, because the masks cover every home
    (see :func:`_masked_crew_home_roots`) while ``config_dir()`` names one: a ``.env`` under
    an un-migrated ``~/.kirocrew``, or one left in the default home by a ``KIROCREW_HOME``
    relocation, holds live channel tokens and is masked.

    Only the live home REFUSES, and only in the unlocatable case. A mask target is bound only
    when it EXISTS -- the loop that seals them is gated on ``os.path.isdir``, whose own
    comment records that an absent directory left unsealed "would be creatable from inside
    the sandbox" -- so a home the install does not use is masked by nothing while it is
    absent, and a sandboxed process can create it and write what it likes inside. A refusal
    there would be reachable by the governed process itself: one ``mkdir`` plus one ``ln``,
    and every later spawn refuses until an operator finds a dotfile in a directory they have
    never opened. The live home carries the opposite properties: it exists, so it IS masked,
    so nothing inside it can be planted from a sandbox.

    The symlink and linked-component refusals in :func:`_refuse_aliased_masked_leaves` stay
    live-home-only deliberately: a host part-way through a migration legitimately points the
    legacy home AT the live one, and refusing every spawn for that layout would break working
    hosts while closing nothing -- the two names then share a single inode, which the live
    home's own pass already judges.

    A leaf reached through a linked INTERMEDIATE component is skipped with a warning, not
    refused. ``lstat`` leaves only the final component un-followed, so for a multi-component
    leaf (the two Notes PAT spellings are the only such entries here) a planted link above it
    makes this read report the link count of a file somewhere else entirely -- refusing on
    that would fail every spawn over a number belonging to a file outside the data home.
    The chain case has its own control: :data:`_CREW_ALIAS_CHAIN_DEGRADE_LEAVES` records
    why those leaves degrade rather than refuse, and :func:`_refuse_aliased_masked_leaves`
    applies it. Escalating here would invert that decision.
    """
    to_hide: list[_AliasMask] = []
    try:
        live_home = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for the credential pass", exc_info=True)
        live_home = ""
    # Read ONCE, so the loop below and the per-leaf alias search cover exactly the same set.
    # A leaf's second name can sit under a DIFFERENT crew home than the leaf itself, and a
    # search narrower than this loop would miss it, come up short on the count, and refuse a
    # spawn over a name it could have masked.
    search_roots = list(_masked_crew_home_roots())
    for root in search_roots:
        for leaf in sorted(_CREW_HARDLINK_REFUSED_LEAVES):
            target = os.path.join(root, leaf)
            try:
                linked = _first_linked_component_below(root, leaf)
            except SandboxCeilingUnsealable:
                # Same policy as the lstat below, and the same reason: a component this
                # pass cannot classify is left to the live home's own fail-closed pass.
                continue
            if linked is not None:
                logger.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the word "credential" names the leaf CLASS; the arguments are two paths and no file is read.  # noqa: E501  # fmt: skip
                    "sandbox: not reading the link count of the masked credential leaf %s. "
                    "It is reached through a component that is a link (%s), so the count "
                    "would belong to a file outside the data home. Replace the link with a "
                    "real directory to close the alias.",
                    target,
                    linked,
                )
                continue
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                continue
            except OSError:
                # A path this process cannot classify is judged by the live home's pass,
                # which has the fail-closed sentence for it. Escalating here would refuse
                # every spawn for an unreadable path in a home nothing is using.
                continue
            if not (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                continue
            # The link count is the trigger, tested here rather than left to the walk coming
            # back empty: the walk reads a tree this process does not control, so it runs
            # only once there is a second name to find.
            # Search EVERY masked home, not just the one holding the leaf: a second name can
            # sit under another crew home, and a per-root search would miss it, leave the
            # count short, and refuse a spawn over a name that was maskable all along.
            walked = [
                _inode_aliases_under(search_root, target, info) for search_root in search_roots
            ]
            found = sorted({alias for names in walked for alias in names.unmasked})
            covered = sorted({alias for names in walked for alias in names.masked})
            # EVERY link must be accounted for, not merely one. The leaf's own name is one, so
            # the walk has to produce the other ``st_nlink - 1``. A leaf with three links where
            # only one other name sits under a home leaves the third readable, and masking the
            # one that was found would report the hole closed while it is open. A walk that
            # stopped at its cap lands here too, and needs no separate flag: truncation only
            # matters when it hid a link, and then this count is short.
            # A name a mask ALREADY covers counts here as well. It is not added to ``to_hide``
            # -- hiding it twice buys nothing -- but it is located and its bytes are
            # unreachable, so leaving it out of the count would refuse a spawn whose every
            # name is already safe. This module's own durability fallback produces exactly
            # that layout: ``token_secret`` keeps the staging hard link when ``fsync_dir``
            # raises, and the staging directory is masked whole.
            accounted = len(found) + len(covered) + 1 == info.st_nlink
            # Mask what WAS found either way. In the live home an unaccounted leaf refuses
            # below and the masks are moot; elsewhere the spawn proceeds, and a located name
            # left readable would be strictly worse than hiding it.
            # The inode is carried with the path: the launcher binds later, and by then a
            # rename plus a two-link decoy at the same name would satisfy every check that
            # does not compare identity.
            to_hide.extend(_AliasMask(alias, info.st_dev, info.st_ino) for alias in found)
            if accounted:
                if masks_are_path_only and root == live_home:
                    # The caller hides by PATH RULE, not by an inode-verified bind. Nothing
                    # denies a write to this alias's parent or its ancestors while the data
                    # home root is writable in-sandbox, so the governed process renames a
                    # parent and the rule names a path that does not hold the bytes -- no
                    # race timing required. This module already carries that lesson: the
                    # voice-runtime seal exists because "a same-UID agent [can] rename a
                    # parent around the path-based subtree deny". Masking cannot stand in for
                    # a refusal on such a platform, so the live home refuses here as it does
                    # for an unlocatable name below.
                    raise SandboxCeilingUnsealable(
                        _masked_leaf_pathonly_alias_detail(target, found)
                    )
                # Every other name is known, so the hole can be CLOSED instead of traded
                # against. Hiding them makes the bytes unreachable in every namespace, which
                # is what a refusal was standing in for, and it costs no spawn -- so neither
                # a real leaf left in an unused home stays readable, nor can a file the
                # governed process wrote itself stop every launch on the host.
                for alias in found:
                    logger.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the word "credential" names the leaf CLASS; the arguments are two sanitised paths and no file is read.  # noqa: E501  # fmt: skip
                        "sandbox: masking %s for this spawn as well. It is a second name for "
                        "the masked credential leaf %s, so the mask over that leaf alone "
                        "would leave these bytes readable. Remove the extra link to stop "
                        "this recurring.",
                        safe_terminal_line(alias),
                        safe_terminal_line(target),
                    )
                continue
            if found or covered:
                # Some names are located and at least one is NOT. Say so, and count the ones
                # a mask already covered among the located: a message that named only what
                # THIS pass masked would understate what is known and overstate the hole.
                logger.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the word "credential" names the leaf CLASS; the arguments are sanitised paths and counts, and no file is read.  # noqa: E501  # fmt: skip
                    "sandbox: the masked credential leaf %s has %d hard links and only %d "
                    "other name(s) could be located, so at least one name for these bytes is "
                    "somewhere this pass cannot reach. Masking the located ones does not "
                    "close the hole.",
                    safe_terminal_line(target),
                    info.st_nlink,
                    len(found) + len(covered),
                )
            # Not every name is accounted for, so the remainder is outside the homes this
            # pass can read -- the snapshot and backup case (``cp -al``, rsnapshot,
            # ``rsync --link-dest``), which this control has always accepted as a cost in the
            # live home rather than tried to locate across the filesystem.
            if root == live_home:
                raise SandboxCeilingUnsealable(_masked_leaf_multilink_detail(target, info.st_nlink))
            logger.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the word "credential" names the leaf CLASS; the argument is a path and a link count, and no file is read.  # noqa: E501  # fmt: skip
                "sandbox: the masked credential leaf %s has %d hard links and the other name "
                "is not under any data home, so it cannot be masked for this spawn. It is not "
                "in the live data home either, so the spawn proceeds and this is reported "
                "rather than refused: a home the install does not use is not masked while it "
                "is absent, so anything inside it can have been created from inside a "
                "sandbox, and refusing would let that stop every launch on the host. Remove "
                "the extra name, or remove the unused home. kirocrew doctor lists this.",
                safe_terminal_line(target),
                info.st_nlink,
            )
    return tuple(to_hide)


def _referent_identity(target: str, info: os.stat_result) -> tuple[int, int, int]:
    """What *target* REACHES, as the launcher's pin will see it: ``(kind, dev, ino)``.

    The occupant identity ``(dev, ino, is_link)`` names the entry at the name. The
    pin needs more to judge what it will MOUNT over: for a link, the object the
    mask lands on is the referent, and the link's own identity says nothing about
    it. A referent swapped for another object of the same kind leaves the link
    untouched, so only the referent's own device and inode can refuse that swap.
    The kind settles a separate case: a directory loop and a file loop are both
    offered every path, and after the directory loop has masked a directory, the
    file loop reaches the stand-in -- a different object of the wrong kind, and
    not a substitution. Only a wrong-kind object at a name the pass saw holding
    THIS loop's kind is one. A link is followed once here, as the pin follows it.

    Kind ``0`` absent (a dangling link), ``1`` directory, ``2`` regular file, ``3``
    any other kind. For a name that is not a link, the referent is the entry
    itself. For a link with no referent, device and inode are ``0``.
    """
    st = info
    if stat.S_ISLNK(info.st_mode):
        try:
            st = os.stat(target)
        except FileNotFoundError:
            return (0, 0, 0)
        except OSError:
            return (3, 0, 0)
    mode = st.st_mode
    if stat.S_ISDIR(mode):
        kind = 1
    elif stat.S_ISREG(mode):
        kind = 2
    else:
        kind = 3
    return (kind, st.st_dev, st.st_ino)


def _refuse_aliased_masked_leaves(
    observed: dict[str, tuple[int, ...]] | None = None,
) -> tuple[_AliasMask, ...]:
    """Refuse the spawn when a MASKED leaf is reachable under a second name.

    The mask is a bind mount, so it attaches to the path the leaf RESOLVES to while the
    leaf's own name stays in the writable data home. A symlinked leaf therefore reads as
    masked and is not: a sandboxed process unlinks the name, drops its own directory or
    file there, and every later read goes to bytes it controls -- past whatever ownership
    check or redactor the gateway applies to the masked path. Warning and continuing is
    what made that silent, which is the whole reason this pass refuses.

    **Both the leaf and its components below the data home.** ``lstat`` leaves only the
    FINAL component un-followed, so a leaf checked that way alone still resolves through a
    planted link at an intermediate component, and multi-component masked leaves genuinely
    exist (``apps/aws-control/data``, ``apps/meetings/data/edits``, the md-notebook state
    leaves). Those intermediates sit inside the agent-writable data home, so a link at one
    of them lands the mask on an attacker-chosen tree while the lexical name stays
    replaceable -- the same hole one level up. Components are walked root-first by
    :func:`_first_linked_component_below`; the data home itself and its parents are NOT
    walked, because ``config_dir()`` documents that a symlinked data HOME is supported.

    Creates NOTHING. An ABSENT leaf is skipped, and that is what makes one pass safe over
    EVERY masked leaf rather than only the materialised ones: a store that has not been
    used yet offers no name to alias, and the retired ``ledgers`` root must not be
    re-materialised on every machine (see its entry in :data:`_CREW_HIDDEN_LEAVES`, which
    says so). Giving the leaves that need a mount target one is a different job, and
    :func:`_materialize_maskable_dirs` does it for the leaves in
    :data:`_CREW_PRECREATE_HIDDEN_DIR_LEAVES`.

    Runs LAST on the spawn path, after every materialiser, so a leaf with its own tailored
    refusal answers first and keeps its own sentence: ``live_target.json`` shares its
    wording with ``kirocrew doctor`` and the md-notebook leaves name their own documents,
    and a generic message arriving first would replace both.

    SYMLINKS refuse. An extra HARDLINK refuses for the leaves whose bytes are themselves a
    usable secret and is WARNED for the rest -- :data:`_CREW_HARDLINK_REFUSED_LEAVES` is
    that set and argues each entry. The split is where the cost sits: ``st_nlink`` says a
    second name EXISTS and not where it is, so refusing also refuses a link that
    ``rsync --link-dest`` or a snapshot tool left outside the sandbox, where it is
    harmless. For a credential leaf that is the right trade and the same one
    :func:`_refuse_unless_sole_regular_link` already makes for the live-target pointer; for
    a leaf masked only so an agent cannot WRITE it, the reader re-validates the content and
    a spawn-wide outage is not proportionate. Either way the warning is emitted HERE rather
    than left to :func:`_warn_if_alias_backed`, which never runs over these leaves.

    The credential refusal itself is issued by :func:`_refuse_multilinked_credential_leaves`,
    called at the end of this pass, because it has to cover EVERY masked home while this
    loop covers ``config_dir()``. The symlink and linked-component refusals stay scoped to
    this one home on purpose; that function's docstring gives the reason.

    A TOLERATED leaf is VISITED and WARNED, never skipped. Excluding it from the walk is the
    same silence one level along: a symlinked ``.env`` carries live channel tokens and is
    replaceable by the very mechanism described above, so saying nothing about it would
    reproduce the property this pass ends while claiming to end it. Tolerating the layout is
    the decision; tolerating it silently is not part of that decision.

    Per spawn rather than once per process, matching :func:`_warn_if_alias_backed`'s own
    stated reason: a host where this keeps happening has a real problem, and de-duplicating
    would hide how often the control cannot be established.
    """
    try:
        root = str(config_dir())
    except Exception as exc:
        # Fail CLOSED, like every other reason on this path: a spawn that skipped the
        # check would run the agent against leaves whose masks may be attached to
        # somewhere else entirely.
        raise SandboxCeilingUnsealable(
            f"cannot resolve the crew data home to check the masked leaves for an alias: {exc}"
        ) from exc
    for leaf in _CREW_HIDDEN_LEAVES:
        # TOLERATED leaves are VISITED, not excluded. Excluding them from the walk is what
        # made the exception silent: a symlinked ``.env`` -- live channel tokens, under the
        # same unlink-and-replace mechanism this pass exists to refuse -- got no refusal and
        # no log line either, which is the property being ended rather than an instance of
        # it. Tolerating the dotfile-manager layout is right; tolerating it silently is not.
        tolerated = leaf in _CREW_ALIAS_TOLERATED_LEAVES
        target = os.path.join(root, leaf)
        linked = _first_linked_component_below(root, leaf)
        if linked is not None:
            if leaf in _CREW_ALIAS_CHAIN_DEGRADE_LEAVES or tolerated:
                # A sibling control already answers the first case; see
                # ``_CREW_ALIAS_CHAIN_DEGRADE_LEAVES`` for why degrading beats refusing
                # for exactly those leaves. A tolerated leaf is never refused at all.
                logger.warning(
                    "sandbox: the masked path %s passes through a component that is a link "
                    "(%s), so the mask binds what that component resolves to. Not refused "
                    "for this leaf -- replace the link with a real directory to close it.",
                    target,
                    linked,
                )
                continue
            raise SandboxCeilingUnsealable(
                f"the masked path {safe_terminal_line(target)} passes through a component "
                f"that is a LINK ({safe_terminal_line(linked)}). The mask binds whatever "
                "that component resolves to, so the name would stay writable inside the "
                "sandbox while reads and writes reached an unmasked tree. Replace the link "
                "with a real directory."
            )
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            # Genuinely absent. Nothing is aliased, and nothing is created.
            continue
        except OSError as exc:
            # Every OTHER errno means this process could not judge the path, which is not
            # the same as the path being safe. Treating them alike failed OPEN: the data
            # home is agent-writable, so an agent that strips search permission from a
            # directory it owns (``apps/``) turns EACCES into a silent skip, and the
            # launcher's own ``isdir`` guard skips the mask for the same reason -- so the
            # child can restore the permission and reach the protected state unmasked.
            # Same split the sibling ``_refuse_if_symlink_leaf`` already makes.
            raise SandboxCeilingUnsealable(
                f"cannot stat the masked path {safe_terminal_line(target)} to check for an "
                f"alias: {safe_terminal_line(str(exc))}. Refusing rather than treating an "
                "unreadable path as absent, because the mask is skipped for a path that "
                "cannot be classified."
            ) from exc
        if observed is not None:
            # Record the identity this pass SAW as soon as the leaf is stat'd, BEFORE any
            # classification branch below can ``continue`` past it. A tolerated symlink or
            # a hard-linked credential leaf is left in place for this spawn (the mask binds
            # what it resolves to), but the launcher must still hold the identity the pass
            # observed: without it ``_carried_occupant`` returns nothing, the pin's
            # substitution check never fires, and an unlink of that name before the child
            # pins it lets the pin skip the now-absent path and expose the credential
            # referent. Taken from the ``lstat`` above, so it costs no extra syscall.
            observed[target] = (
                info.st_dev,
                info.st_ino,
                int(stat.S_ISLNK(info.st_mode)),
                *_referent_identity(target, info),
            )
        if stat.S_ISLNK(info.st_mode):
            pointed_at = _symlink_target_display(target)
            if tolerated:
                logger.warning(
                    "sandbox: the masked path %s is a SYMLINK -> %s. The mask binds what "
                    "the link resolves to, so this NAME stays writable inside the sandbox. "
                    "Not refused, because this leaf is the operator's own file and a dotfile "
                    "manager legitimately links it -- make it a regular file to close it.",
                    target,
                    pointed_at,
                )
                continue
            raise SandboxCeilingUnsealable(
                f"the masked path {safe_terminal_line(target)} is a SYMLINK -> "
                f"{pointed_at}. The mask binds whatever the link "
                "resolves to, so this NAME would stay writable inside the sandbox while "
                "reads and writes reached an unmasked target. Remove the link and keep a "
                "real directory or file under this name."
            )
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            if leaf in _CREW_HARDLINK_REFUSED_LEAVES:
                # Refused by _refuse_multilinked_credential_leaves, which owns this
                # condition across EVERY masked home rather than this loop's single one.
                continue
            logger.warning(
                "sandbox: the masked path %s has %d hardlinks. The mask covers this path "
                "only, so a read or write through another name reaches the same inode. "
                "Not refused, because this leaf is masked so an agent cannot WRITE it and "
                "its reader re-validates the content, while a hardlinking snapshot tool "
                "leaves a link on an ordinary host -- remove the extra link to close it.",
                target,
                info.st_nlink,
            )
    # Every masked home, not just this loop's. The credential leaves are masked under each
    # crew-home spelling and under a relocated home, so the pass has to cover the same set
    # the masks do. Its return value is the paths this spawn must ALSO hide: a second name to
    # a masked leaf that the mask does not already cover.
    return _refuse_multilinked_credential_leaves()


def _masked_leaf_pathonly_alias_detail(target: str, aliases: list[str]) -> str:
    """The refusal sentence when the platform can only hide an alias BY PATH.

    Separate from :func:`_masked_leaf_multilink_detail` because the condition is the
    opposite one: there every other name is UNLOCATABLE, so nothing can be masked; here
    every name IS located and masking them would ordinarily close the hole -- it is the
    platform's masking that cannot be relied on, since a path rule stops naming the bytes
    as soon as the governed process renames a parent it is free to write. Saying "another
    path is readable" would misdescribe that, and the operator's remedy is the same
    ``find`` command either way.
    """
    listed = ", ".join(safe_terminal_line(a) for a in aliases[:3])
    more = "" if len(aliases) <= 3 else f" and {len(aliases) - 3} more"
    return (
        f"cannot mask {safe_terminal_line(target)}: this leaf holds a credential and is also "
        f"reachable as {listed}{more}. On this platform a mask is a path rule rather than a "
        "bind over the inode, and nothing stops the sandboxed process renaming a parent "
        "directory so the rule no longer names these bytes, so hiding the other names cannot "
        f"stand in for removing them. {_masked_leaf_alias_search_hint(target)}"
    )


def _masked_leaf_multilink_detail(target: str, links: int) -> str:
    """The refusal sentence for a credential leaf reachable under more than one name.

    Names the ``find`` invocation rather than only the condition, for the reason
    :func:`_live_target_multilink_detail` gives: an ordinary snapshot run leaves a link, so
    the operator who meets this has no reason to know which OTHER path shares the inode,
    and without the command the remedy "remove the extra link" names no file to remove.

    Its own sentence rather than the pointer's, which says "the live-target pointer" and
    would name the wrong file here, and a module-level formatter rather than an inline
    string so ``kirocrew doctor`` reports the condition in the same words BEFORE a spawn
    refuses on it.
    """
    return (
        f"cannot mask {safe_terminal_line(target)}: this leaf holds a credential and has "
        f"{links} hard links, so a mask over this name would leave another path to the "
        f"same bytes readable and writable. {_masked_leaf_alias_search_hint(target)}"
    )


def _masked_leaf_alias_search_hint(target: str) -> str:
    """How to find the other names for *target*, without claiming anything is unmasked.

    Its own function because the two readers need the same command and disagree about the
    sentence before it: the launcher's refusal says the leaf cannot be masked, while
    ``kirocrew doctor`` reports leaves whose every other name WAS located and is masked for
    each spawn. Printing the refusal sentence for those said "masked for each spawn" and
    "cannot mask" one line apart, so the operator could not tell which had happened.

    :func:`_masked_leaf_multilink_detail` prepends the refusal sentence and is what a spawn
    raises with; doctor uses this alone for a leaf that is accounted for.
    """
    # ``shlex.quote`` per path for the reason the pointer's formatter states: a data home
    # holding a space otherwise turns the remedy into a two-directory search that answers a
    # different question without erroring, so it has to survive being pasted.
    # The search directory is the DATA HOME, not the leaf's own parent. Every entry in
    # _CREW_HARDLINK_REFUSED_LEAVES but the two Notes PAT spellings is a root-level name, for
    # which the two coincide; ``workspace/<md-notebook>/pat`` and ``md-notebook-staging/pat``
    # are the exceptions, and using their parent there
    # would search one app's state directory while the sentence below promises the data home,
    # so a second name anywhere else under the home would report as absent. Strip whichever
    # leaf this target ends with to recover the home the caller was iterating.
    search_dir = os.path.dirname(target)
    for leaf in _CREW_HARDLINK_REFUSED_LEAVES:
        suffix = os.sep + leaf.replace("/", os.sep)
        if target.endswith(suffix):
            search_dir = target[: -len(suffix)]
            break
    return (
        "List the names under the data home with the command find "
        f"{shlex.quote(safe_terminal_line(search_dir))} "
        f"-samefile {shlex.quote(safe_terminal_line(target))} "
        "-- that searches the data home only, and the tools that leave a link here "
        "(snapshot and backup runs) usually keep theirs somewhere else, so if it reports "
        "just this leaf, run it again from the mount point holding it with -xdev added: a "
        "hard link cannot cross a filesystem, but it can sit anywhere on this one. Then "
        "remove the extra link(s) and restart."
    )


def masked_credential_leaf_aliases() -> list[tuple[str, int, str, bool]]:
    """Every masked CREDENTIAL leaf that is reachable under more than one name, for doctor.

    The pre-spawn read that makes the refusal in :func:`_refuse_aliased_masked_leaves`
    defensible, and the counterpart of :func:`live_target_pointer_unfitness` for the same
    shape on the other leaves. The refusal is otherwise the operator's only notice, and it
    arrives too late and in the wrong place: a hard link on a file in the home is ordinary
    operation for a snapshot tool, so the condition appears without anybody doing anything
    wrong, and the first symptom is that agents stop starting.

    Read-only and total. It creates nothing, follows nothing, opens no file, and a data home
    it cannot resolve or a leaf it cannot stat is reported as nothing rather than as a
    fault, because doctor must not turn its own probe failure into a verdict about the host.
    An absent leaf has no second name by construction.

    Covers EVERY masked home (:func:`_masked_crew_home_roots`), the same set
    :func:`_refuse_multilinked_credential_leaves` judges. A probe narrower than the refusal
    is worse than no probe: it reports a clean host and the next spawn refuses anyway, which
    is the failure this read exists to prevent.

    Returns the leaf path, its link count, the home it was found under, and whether every
    other name was LOCATED. The caller renders the same sentence the spawn would use instead
    of paraphrasing it, and needs the other two to say what that spawn actually does: only
    the live home refuses, and only when a name could not be located -- a leaf whose every
    name is located is MASKED and the spawn proceeds. A reader told "REFUSED" for either of
    the other two would act on a failure that is not coming.
    """
    found: list[tuple[str, int, str, bool]] = []
    search_roots = list(_masked_crew_home_roots())
    for root in search_roots:
        for leaf in sorted(_CREW_HARDLINK_REFUSED_LEAVES):
            target = os.path.join(root, leaf)
            try:
                if _first_linked_component_below(root, leaf) is not None:
                    # Reached through a linked component, so the count would belong to a
                    # file outside the data home. The spawn path skips this case for that
                    # reason, and a probe that reports what the spawn does NOT refuse on
                    # would fail doctor's exit code over a foreign path.
                    continue
            except SandboxCeilingUnsealable:
                continue
            try:
                info = os.lstat(target)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                # The SAME accounting the refusal performs, over the SAME set of homes, so the
                # two cannot disagree: the launcher masks a leaf whose every other name is
                # located and refuses one where a name is not, and a probe reporting only the
                # link count cannot tell an operator which of those is coming. A name a mask
                # already covers counts as located on both sides, for the same reason.
                walked = [
                    _inode_aliases_under(search_root, target, info) for search_root in search_roots
                ]
                located = {alias for names in walked for alias in names.unmasked} | {
                    alias for names in walked for alias in names.masked
                }
                found.append((target, info.st_nlink, root, len(located) + 1 == info.st_nlink))
    return found

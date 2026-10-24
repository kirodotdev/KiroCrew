"""The home-directory target sets the path gates compare a resolved path against.

Every sensitive declaration is resolved under ``$HOME`` and under the roots the
environment overrides (``_OVERRIDE_ROOT_ENVS``, plus each adapter's declared credential
home) through the bounded resolver, built once per root key and cached for a time scaled
to what the build cost. The gates' matchers in ``kiro_crew.security.paths`` ask
``_home_dir_targets`` for a set and the candidate spellings of a path, and a stalled
rebuild raises rather than serving targets anchored on stale roots. A change to how
declared paths become concrete targets goes here.

``kiro_crew.security.paths`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the declarations
in ``paths_lists`` and the resolver in ``paths_resolver``, both below it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import NamedTuple

from kiro_crew.agent_sdk import host_auth, native_allow_stores

from .paths_lists import (
    _CREW_HOME_PREFIXES,
    _KEYSTONE_ARTIFACT_PARENTS,
    _KIRO_CLI_WRITE_TIER_LEAVES,
    _SENSITIVE_HOME_DIRS,
    _WRITE_PROTECTED_HOME_PATHS,
    _leaf_segments,
)
from .paths_resolver import (
    _PATH_RESOLVE_REBUILD_TIMEOUT_SECS,
    PathResolutionStalled,
    _fold_windows_alias,
    _realpath_or_none,
    _realpaths_or_none,
    _resolved_forms_bounded,
    _run_resolution_bounded,
    _stall_prefix,
)

logger = logging.getLogger("kiro_crew.security.paths")


def _candidate_forms(
    path_str: str, base_dir: str | None = None, *, pre_resolved: bool = False
) -> set[str]:
    """Expand *path_str* into every candidate form the sensitive-path gates match.

    Symlink-resolved forms defeat a link bypass; the lexical forms are the
    fail-safe fallback when resolution cannot complete (over-matching a
    sensitive-looking path is the safe direction). ``base_dir`` anchors a
    relative input against the caller's known working directory. Shared by
    :func:`_path_in_home_dirs` (is the path INSIDE a protected location?) and
    :func:`path_contains_sensitive` (does the path CONTAIN one?) so the
    symlink/anchoring hardening cannot drift between the two directions.

    *pre_resolved* says the caller ALREADY holds the canonical spelling -- the
    output of ``os.path.realpath`` computed on its own thread -- so no
    resolution is submitted to the ``mc-pathres`` pool: the candidates are the
    input and its ``normpath``, which is exactly what :func:`_resolved_spellings`
    returns for a path that has no link left to follow. Reserved for
    :func:`is_sensitive_resolved_path`; see there for why a caller may claim it.
    """
    # Expand ~ and $HOME
    raw = os.path.expanduser(os.path.expandvars(path_str))
    # Fold before resolving (a ``\\?\`` spelling would skip resolution as UNC-shaped);
    # the raw spelling stays a lexical candidate, so folding only adds candidates.
    expanded = _fold_windows_alias(raw)

    # Anchor a relative input against the supplied workspace dir so it resolves
    # to the real file rather than the gateway's CWD.  Absolutize base_dir
    # itself first — if a caller passes a relative base_dir, os.path.join would
    # re-anchor against the process CWD (the very thing the parameter exists to
    # avoid), giving zero protection when CWD is unrelated to the workspace.
    if base_dir and not os.path.isabs(expanded):
        expanded = os.path.join(os.path.abspath(base_dir), expanded)

    # Build the candidate forms.  Symlink-resolved forms defeat a link bypass;
    # the lexical forms are the fail-safe fallback when resolution FAILS
    # (over-matching a sensitive-looking path is the safe direction).
    # Resolution is BOUNDED -- see _resolved_forms_bounded: an unbounded lstat on
    # a stalled automount would wedge the event loop from inside on_tool_call.
    # A resolution that does not COMPLETE raises PathResolutionStalled through
    # here, and every gate turns that into a refusal: no lexical-only matching
    # of a path whose canonical form is unknown.
    candidates: set[str] = set() if pre_resolved else _resolved_forms_bounded(expanded)
    candidates.add(os.path.normpath(expanded))
    candidates.add(expanded)
    if raw != expanded:
        candidates.update((os.path.normpath(raw), raw))
    return candidates


class _BuiltTargets(set[str]):
    """The target set, plus whether building it traversed a symlink.

    A plain ``set`` wherever it is consumed -- membership, iteration, equality
    and the casefold contract are all unchanged -- carrying one extra fact that
    only the CACHE needs and no matcher does: did any path this build actually
    RESOLVED come back spelled differently from the path it was asked about.

    WHICH paths those are is the entire scope of the flag, so they are named here
    rather than left to be read as "every target". The build resolves six classes
    of path and no others: ``$HOME``, ``KIROCREW_OS_HOME``, every entry of the
    CALLER'S ``home_dirs`` list anchored under ``$HOME``, each such entry that
    carries a crew prefix, re-anchored under ``KIROCREW_HOME``, each path in
    ``_KIRO_CLI_WRITE_TIER_LEAVES`` -- the agents dir, the MCP registry leaf and the
    crewmate folder -- re-anchored under ``KIRO_HOME``, and each harness credential leaf
    re-anchored under its own home override. Every other member of the set -- the
    copies re-anchored under the logical home, the home's realpath and
    ``KIROCREW_OS_HOME`` -- comes from ``_anchor`` or ``_anchor_both_separators``,
    neither of which touches the filesystem, so those copies cannot report a
    traversal at all.

    A symlinked dotfile under ``$HOME`` sets this flag whenever it is a protected
    entry or sits on the path to one, so a dotfile-managed home is a case to look
    for, and the commonest one. The trigger is the union this build resolves: a
    ``home_dirs`` entry under ``$HOME`` (a linked ``~/.ssh``, ``~/.aws`` or
    ``~/.kiro`` pins every tier whose list carries an entry under it), a
    crew-prefixed entry of the caller's ``home_dirs`` under ``KIROCREW_HOME``, a
    kiro-cli path under ``KIRO_HOME``, or a DECLARED credential leaf under whichever
    harness variable relocates it. So a symlinked ``sessions`` or ``models`` leaf
    under a ``KIROCREW_HOME`` that sits below ``$HOME`` pins the write tier, and a
    symlinked ``goose`` directory pins the tier handed that leaf whether it sits
    under ``~/.config`` or under whatever ``XDG_CONFIG_HOME`` resolves to, because
    the leaf is resolved under each root. What CANNOT set it is a dotfile that is
    neither a protected entry nor on the path to one: no build resolves it. The
    roots themselves cannot either: ``_resolve_root_anchors`` canonicalises every
    root first, so the symlink has to sit BELOW the resolved root -- which is why
    relocating the exported config root alone does not trip it while a symlinked
    ``goose`` directory under it does.

    The LEAF-BEARING override roots are ``KIROCREW_HOME``, ``KIRO_HOME``, and every
    variable in ``host_auth.home_override_env_vars()``. That table is the
    enumeration; naming the variables or their count here would be a second place
    to forget the next harness, which is the documentation defect this docstring
    exists to remove. ``_OVERRIDE_ROOT_ENVS`` carries one more root,
    ``KIROCREW_OS_HOME``, which is resolved as a root but bears no leaf class of
    its own -- its entries are joined lexically by ``_anchor_both_separators``.

    The crew-prefix class is the caller's list, not a fixed set of leaves. The
    crew-prefix arm loops over the ``home_dirs`` it was handed and resolves every
    entry carrying a prefix, and the three callers hand it three different lists:
    the read gate passes ``_SENSITIVE_HOME_DIRS``, ``is_sensitive_write_path``
    passes ``_SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS``, and
    ``_is_keystone_publish_artifact`` passes ``_KEYSTONE_ARTIFACT_PARENTS``. So no
    one class of leaf describes the resolved population: on the write tier a
    symlinked write-protected crew leaf that holds no secret sets the flag too,
    and the three builds can disagree about it on one host.

    That fact is what bounds the deeper-leaf staleness the target cache always
    carried. A resolved path that comes back different came through a symlink at
    or below a resolved root, and THAT is the entry a repoint can move out from
    under a cached set. When nothing resolved differently there is no
    resolution-derived entry to go stale, so the long adaptive expiry is safe;
    when one did, the expiry is pinned to the floor. See
    :func:`_home_targets_ttl`.

    The roots cannot trip it. Every root reaching the builder is already
    canonical (:func:`_resolve_root_anchors` resolved them), so re-resolving one
    returns the same string -- which matters on a host where ``$HOME`` is itself
    a symlink, because flagging that would pin the floor on every cloud desktop
    and the adaptive expiry would never apply anywhere.

    Carried as an ATTRIBUTE rather than a second return value because
    ``_home_dir_targets_uncached`` has several callers and three test doubles. A
    widened signature would break each of them, whereas a double that returns a
    plain ``set`` simply lacks the attribute and reads as the class default
    below -- ``True``, the fail-safe answer, which selects the floor.
    """

    #: Fail-safe: an unknown build is treated as having traversed a symlink, so a
    #: caller that cannot prove otherwise gets the short expiry.
    resolution_differed: bool = True


def _home_dir_targets_uncached(
    home_dirs: list[str],
    roots: _ResolvedRoots | None = None,
) -> set[str]:
    """Anchor the ``$HOME``-relative *home_dirs* entries into absolute, casefolded
    on-disk targets.

    Every per-anchor resolved form comes from :func:`_realpaths_or_none`, looked
    up at call time so a test can stand in a recording or wedged resolver at
    module level.  It touches the filesystem, so in production this function
    runs under the bounded core via :func:`_home_dir_targets` (see there
    for why); only direct callers and tests run it inline.

    Two passes over the same anchoring logic (:func:`_anchor_targets`) so the
    per-leaf ``realpath`` calls -- one per entry plus the override re-anchors, a
    few hundred -- travel as ONE child round trip instead of one
    each: the first pass runs with no answers and only records which paths it asks
    for (every anchor derives from *roots* and *home_dirs*, never from an earlier
    answer, so the set is complete), then the batch is resolved and the second pass
    builds the real set from it.

    *roots* optionally supplies the already-resolved :class:`_ResolvedRoots`
    already resolved by the caller. The TTL cache in :func:`_home_dir_targets` MUST pass
    it: resolving the roots here as well would read the filesystem a second
    time, and a root symlink repointed between the two reads would file this
    set under a key naming the OTHER root — caching one root's targets against
    another root's key, which fails OPEN. ``None`` (direct callers and tests)
    resolves them here as before.

    Anchors against BOTH the logical home and its realpath.  On macOS the
    per-user temp/home prefix can itself be reached via OS symlinks (``/var`` →
    ``/private/var``); folding both roots in means a resolved candidate under
    either spelling is still matched.

    ``home_dirs`` entries are authored with POSIX "/" separators, and some are
    multi-segment now (e.g. ".kiro/crew/security_policy.json"). Split on "/"
    and re-join with ``os.path.join`` so the target uses the running OS's
    separator — otherwise on Windows the target keeps a literal "/" in the
    leaf while the candidate forms (realpath/normpath) are all-backslash, they
    never compare equal, and the keystone would silently stop gating its own
    secrets. On POSIX a single-segment entry splits to a 1-element list, so
    this is a no-op there.
    """
    resolved = roots if roots is not None else _resolved_root_key()
    wanted: dict[str, str | None] = {}
    _anchor_targets(home_dirs, resolved, wanted)
    answers = dict(zip(wanted, _realpaths_or_none(list(wanted))))
    return _anchor_targets(home_dirs, resolved, answers)


def _anchor_targets(
    home_dirs: list[str], resolved: _ResolvedRoots, resolved_paths: dict[str, str | None]
) -> set[str]:
    """One pass of :func:`_home_dir_targets_uncached`, reading answers from *resolved_paths*.

    A path not in the dict is recorded there as unresolved, which is how the first
    pass enumerates what the second pass needs resolved.
    """

    # Both supported Crew home prefixes map to the same override leaves.
    # Resolve each identical spelling once within this build; never carry these
    # answers across builds or cache keys, so root and leaf freshness is unchanged.
    def resolve_target(path: str) -> str | None:
        return resolved_paths.setdefault(path, None)

    home = resolved.home
    crew_home = resolved.crew_home
    kiro_home_override = resolved.kiro_home
    logical_home = resolved.logical_home
    os_home = resolved.os_home

    def _anchor(root: str, d: str) -> str:
        return os.path.join(root, *_leaf_segments(d)).casefold()

    def _anchor_both_separators(root: str, d: str) -> set[str]:
        """*d* under *root*, spelled with BOTH separators.

        ``_anchor`` joins with the RUNNING OS's separator, which is right for a
        candidate that reached the matcher as a native path. It is not enough for
        a pod root: this anchors a root that arrives from an ENV VARIABLE rather
        than from ``Path.home()``, so the operator's own spelling reaches the set
        and a Windows pod home would otherwise be all-backslash while a candidate
        normalised to forward slashes never compared equal -- the gate silently
        stops covering its own targets on that platform.

        Emitting both spellings is strictly WIDENING -- no target is removed, and a
        path is fenced under either spelling on either platform -- which is the
        right direction for a gate whose documented stance is that a *maybe*
        answers yes. Cheaper and more honest than teaching every candidate path to
        re-derive the separator it should have used.
        """
        parts = _leaf_segments(d)
        return {
            os.path.join(root, *parts).casefold(),
            "/".join([root.rstrip("/\\"), *parts]).casefold(),
            "\\".join([root.rstrip("/\\"), *parts]).casefold(),
        }

    sensitive_targets: set[str] = {_anchor(home, d) for d in home_dirs}
    # ``KIROCREW_OS_HOME`` is an ALTERNATE ``$HOME`` (see _resolved_root_key):
    # a pod-spawned kiro-cli child runs with it as its literal HOME, so its
    # credential store and the pod-minted OAuth grants live under this root. Every
    # entry re-anchors here -- the variable relocates the whole home, not just
    # ``.aws`` -- so a secret cannot be moved out from under its own gate.
    #
    # Resolved through ``_realpath_or_none`` for the same reason ``home`` is: it
    # touches the filesystem (and opens the directory on Windows), which is why the
    # whole rebuild runs off the loop, and ``None`` degrades to the lexical anchors
    # already added above rather than raising.
    if os_home:
        for d in home_dirs:
            sensitive_targets |= _anchor_both_separators(os_home, d)
        os_home_real = resolve_target(os_home) or os_home
        if os_home_real.casefold() != os_home.casefold():
            for d in home_dirs:
                sensitive_targets |= _anchor_both_separators(os_home_real, d)
    # ``home`` arrives RESOLVED from the cache key, so this is normally a no-op;
    # it still opens the directory on Windows, which is why the whole rebuild
    # runs off the loop.  None degrades to the lexical anchors already in the set.
    home_real = resolve_target(home) or home
    if home_real.casefold() != home.casefold():
        sensitive_targets |= {_anchor(home_real, d) for d in home_dirs}
    # ``home`` arrives RESOLVED (the cache is keyed on the resolved roots), so
    # the realpath above is normally a no-op and the LOGICAL spelling of a
    # symlinked ``$HOME`` was never anchored -- a gap masked as long as every
    # candidate was itself resolved.  Candidate resolution is now bounded and
    # degrades to the lexical spelling, so anchor the logical home explicitly:
    # ``~/.ssh/id_rsa`` spelled through ``/home/x`` must match even when
    # ``/home/x -> /local/home/x`` could not be followed in time.
    if logical_home.casefold() != home.casefold():
        sensitive_targets |= {_anchor(logical_home, d) for d in home_dirs}
    # When KIROCREW_HOME points to a non-default path, the keystone secrets
    # (token_signing.key, refresh_chains.json, .local_secret, sel_hmac.key,
    # security_policy.json etc.) live directly under it — NOT under either of
    # the default crew home prefixes (~/.kiro/crew, ~/.kirocrew). Without this
    # expansion any "<crew-prefix>/X" entry in the home_dirs list would miss
    # the real file location, letting the agent read/write its own signing key
    # or governance ceiling via the custom KIROCREW_HOME. Strip whichever crew
    # prefix an entry carries and re-anchor the leaf under the env-override
    # root ADDITIONALLY (the ~/-rooted default forms stay, so every location is
    # always covered).
    if crew_home:
        kiro_home = crew_home
        for d in home_dirs:
            for _prefix in _CREW_HOME_PREFIXES:
                # Compare with POSIX separators (home_dirs entries are authored
                # that way) so this matches regardless of the running os.sep.
                if d == _prefix or d.startswith(_prefix + "/"):
                    leaf = d[len(_prefix) :].lstrip("/")
                    full = os.path.join(kiro_home, *_leaf_segments(leaf)) if leaf else kiro_home
                    sensitive_targets.add(full.casefold())
                    # Also add the resolved form in case the env value itself has
                    # symlinks (matches the home/home_real duality above).
                    full_real = resolve_target(full)
                    if full_real is not None:
                        sensitive_targets.add(full_real.casefold())
                    break
    # Every entry is RESOLVED under the default home as well, not only anchored
    # lexically. ``home`` arrives already resolved and ``home_real`` covers a
    # symlinked ``$HOME`` ITSELF, but neither follows a symlink further down the
    # path. A protected entry that is itself a link, or sits below one -- a
    # dotfile-managed ``~/.aws``, ``~/.ssh`` or ``~/.kiro``, the case
    # :class:`_BuiltTargets` names as one to look for -- leaves the store it points
    # at outside every lexical anchor. The ``~``-spelled path stays protected, but a
    # caller that checks the ``realpath`` of a path inside it, as most do, would
    # match nothing, and the store would be read or written through its real path.
    #
    # Same shape as the crew-prefix arm above: the lexical form is already in the
    # set, and its resolved spelling joins it. Driven from the caller's own
    # *home_dirs*, so a write-tier leaf never reaches the read gate's set.
    for d in home_dirs:
        full_real = resolve_target(os.path.join(home, *_leaf_segments(d)))
        if full_real is not None:
            sensitive_targets.add(full_real.casefold())
    # Both kiro-cli leaves follow ``KIRO_HOME`` — kiro-cli's own home override,
    # honoured by ``kiro_agents_dir()`` and by the registry's own resolver. When it
    # is set, the specs the gateway execs live at ``<KIRO_HOME>/agents`` and the
    # registry whose ``autoApprove`` skips the tool gate at
    # ``<KIRO_HOME>/settings/mcp.json``, NOT under the real home, so the
    # ``$HOME``-anchored entries above miss them and an agent write there would
    # bypass the gate. Re-anchor each leaf's tail under the override, mirroring the
    # ``KIROCREW_HOME`` expansion directly above (the ~/-rooted default forms stay,
    # so every location is always covered).
    #
    # Driven from the tuple rather than one arm per leaf, so a third kiro-cli leaf is
    # one tuple entry and not a third arm someone has to remember to add; its
    # ``$HOME``-rooted half is the every-entry loop above. The tail comes off the
    # leaf spec (``_leaf_segments(leaf)[1:]`` drops the ``.kiro`` the override
    # replaces), so no leaf's path is spelled twice and the two spellings cannot
    # drift apart.
    #
    # Only added when the leaf is actually in *home_dirs* — both are on the
    # write-only tier (``_WRITE_PROTECTED_HOME_PATHS``) and NOT in
    # ``_SENSITIVE_HOME_DIRS``, so this must not leak a write-tier target into the
    # read gate. No validity check on the override: an unsafe ``KIRO_HOME`` falls
    # back to ``~/.kiro`` in ``kiro_home()`` (already covered by the default form),
    # so an extra target under a bogus value is harmless and fail-safe.
    if kiro_home_override:
        for _cli_leaf in _KIRO_CLI_WRITE_TIER_LEAVES:
            if _cli_leaf not in home_dirs:
                continue
            _under = _leaf_segments(_cli_leaf)[1:]
            _over_full = os.path.join(kiro_home_override, *_under) if _under else kiro_home_override
            sensitive_targets.add(_over_full.casefold())
            _over_real = resolve_target(_over_full)
            if _over_real is not None:
                sensitive_targets.add(_over_real.casefold())
    # An ACP adapter's OAuth token follows that adapter's own home override, so
    # the ``$HOME``-rooted entry anchored above covers only the documented
    # default. Re-anchor the token leaf under each override the adapter honours
    # (the default form stays, so every location is always covered). Guarded on
    # membership in *home_dirs* for the same reason as the agents dir above: a
    # write-tier build must not gain a read-tier target.
    _adapter_roots = dict(resolved.adapter_roots)
    for _leaf, _root_envs, _under_root in _OVERRIDE_ANCHORED_LEAVES:
        if _leaf not in home_dirs:
            continue
        for _env in _root_envs:
            _root = _adapter_roots.get(_env)
            if not _root:
                continue
            _full = os.path.join(_root, *_leaf_segments(_under_root))
            sensitive_targets.add(_full.casefold())
            _full_real = resolve_target(_full)
            if _full_real is not None:
                sensitive_targets.add(_full_real.casefold())
    # Did any path this build resolved come through a symlink? Read off the memo
    # this build already filled -- the six classes named on ``_BuiltTargets``, so
    # ``$HOME`` plus one entry per *home_dirs* entry on a host with no home override
    # set, and about eighty more with every one set -- which costs one pass over
    # that dict and no extra filesystem work: the resolutions themselves are the
    # expense and they have already happened. The copies re-anchored under the
    # logical home, the home's realpath and ``KIROCREW_OS_HOME`` never appear here,
    # because ``_anchor`` and ``_anchor_both_separators`` build them without
    # resolving anything. Normalised on both sides so a separator or case
    # difference cannot read as a symlink; a false positive here is merely the
    # short expiry, a false negative would be the stale window this bounds.
    differed = any(
        value is not None
        and os.path.normcase(os.path.normpath(value)) != os.path.normcase(os.path.normpath(key))
        for key, value in resolved_paths.items()
    )
    built = _BuiltTargets(sensitive_targets)
    built.resolution_differed = differed
    return built


# How long a built target set stays reusable. ``_home_dir_targets_uncached``
# rebuilds a ~75-entry set on EVERY ``is_sensitive_path`` call and measured at
# 1.14ms of that call's 1.25ms (91%) on a dev desktop, because it realpath()s
# ``$HOME`` and each KIROCREW_HOME-anchored leaf. Callers hit it per FILE — one
# skills-tree walk made thousands of identical calls and took 4.2s, of which
# 3.5s was this rebuild.
#
# Deliberately TTL-bounded rather than a plain ``lru_cache``: part of the set is
# derived from FILESYSTEM state, so an unbounded cache would keep matching a
# stale target if a symlink were repointed after the cache warmed — a gate that
# fails OPEN. ``_HOME_TARGETS_TTL_MAX_SECS`` bounds that window.
#
# The key is built from the RESOLVED roots (``Path.home().resolve()`` and the
# resolved ``KIROCREW_HOME``), NOT from the raw env vars, because those two
# values are exactly what the builder anchors its targets on. Keying on the raw
# ``$HOME`` string is wrong twice over:
#   1. Repointing a symlink AT ``$HOME`` leaves ``$HOME`` unchanged while every
#      target moves, so the gate returns False for a credential path the
#      uncached code blocks (a real, reproduced bypass — see the regression
#      test ``test_repointed_home_symlink_is_not_served_from_cache``).
#   2. ``Path.home()`` reads ``USERPROFILE`` on Windows and never ``HOME``, so on
#      that platform the key omits the one variable that decides the anchor.
# Resolving the roots costs ~2 realpath calls (~0.06ms) against the ~1.14ms
# rebuild it replaces, so the win survives. Those calls -- and the rebuild
# itself -- run on the ``mc-pathres`` pool under the resolve budget, one thread
# hop each (``_resolved_root_key`` resolves all six roots in one job,
# ``_rebuild_targets_bounded`` resolves every leaf in one job), because
# an inline ``realpath($HOME)`` on a loaded Windows desktop blocked past the
# loop-stall watchdog; see ``_rebuild_targets_bounded``.
#
# Residual, accepted: a symlink swapped DEEPER inside the crew home (an
# individual keystone leaf, or an intermediate directory on the way to one) can
# still be served stale for up to the TTL. Detecting that needs the per-leaf
# realpath calls that ARE the expense — measured 45 realpath calls per build,
# 94% of its 1.39ms — so there is no cheap way to keep the cache and revalidate
# them.
#
# The TTL is therefore sized as small as it can be while still doing its job.
# The FLOOR is 0.1s, NOT a "few seconds", because a skills walk issues thousands
# of calls in a burst and one build serves the whole burst either way. Measured
# cold-walk cost against a FIXED constant, on an idle interpreter:
#     5.0s -> 0.95s    1.0s -> 0.93s    0.1s -> 0.95s    0.0s -> 4.66s
# So 0.1s keeps the entire win while cutting the stale window 50x versus 5.0s.
# Only 0.0 (no cache) closes the window completely, and that reverts to the 4.7s
# scan whose GIL-held cost wedges the event loop -- the defect this exists to fix.
_HOME_TARGETS_TTL_SECS = 0.1


# ---------------------------------------------------------------------------
# Why the expiry is not that floor alone.
#
# 0.1s was chosen against an IDLE interpreter, where one rebuild costs ~2ms, so
# the cache spends ~2% of the wall clock rebuilding. But the rebuild is ~130
# ``os.path.realpath`` calls and each syscall releases and re-acquires the GIL,
# so its cost is set by CONTENTION, not by the disk. Measured on this repo's own
# anchors -- 64-core Linux, local xfs, one mount, ``sys.getswitchinterval()``
# 5ms -- with N sibling threads running pure Python:
#     0 -> 2ms    1 -> 581ms    2 -> 399ms    4 -> 3859ms    8 -> 7456ms
# A fixed 0.1s expiry does not move with that, so the share of the wall clock
# spent rebuilding rises with load until the rebuild misses
# ``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS`` and the gate refuses ordinary project
# files. That is the reported defect, and the filesystem is healthy throughout.
#
# So the expiry tracks the MEASURED cost of the build it is expiring:
#
#     ttl = clamp(last rebuild seconds * _HOME_TARGETS_TTL_COST_RATIO,
#                 _HOME_TARGETS_TTL_SECS, _HOME_TARGETS_TTL_MAX_SECS)
#
# The ratio is the reciprocal of the share of the wall clock the gate may spend
# rebuilding, so that share is held at ``1 / ratio`` at EVERY load level rather
# than only at the one the constant was picked on. The rebuild's own duration is
# the load signal, so no separate load metric is read and nothing has to be
# configured per host.
#
# WHAT THIS DOES NOT FIX, stated rather than implied: an expiry controls how
# OFTEN the rebuild is paid, never what ONE costs. Past roughly four contending
# threads a single COLD rebuild already exceeds its own budget, and there every
# expiry refuses alike -- measured, see ``scripts/measure_path_gate_ttl.py``.
# Only a resolver outside this interpreter's GIL closes that case. This law's
# job is narrower: it stops re-paying the contended cost every 100ms.
#
# THE TRADE, stated plainly: a longer expiry lengthens the window in which a
# symlink swapped DEEPER inside the crew home (a keystone leaf, or an
# intermediate directory on the way to one) is still answered from the stale
# set -- the residual named above, now bounded by ``_HOME_TARGETS_TTL_MAX_SECS``
# rather than by 0.1s. It does NOT widen the two reproduced bypasses, because
# both repoint an ANCHOR and every anchor is part of the cache KEY: a repointed
# ``$HOME`` or ``KIROCREW_HOME`` re-keys and misses the cache at any expiry,
# which ``test_repointed_home_symlink_is_not_served_from_cache_at_the_longest_expiry``
# pins at the maximum expiry as well as at the floor.
#
# Both inputs are NAMED constants, each pinned by a test, and each an OPERATOR
# KNOB read once at import and fail-soft to its reviewed default:
# ``KIROCREW_PATH_GATE_TTL_COST_RATIO`` and ``KIROCREW_PATH_GATE_TTL_MAX_SECS``.
# So a host can trade the two the other way, or revert to one fixed 0.1s expiry
# with a ratio of 0, without a release and without patching this file.
# ---------------------------------------------------------------------------

#: Environment overrides for the two inputs below, and the bounds each accepts.
#: Read ONCE at import, like the resolver pool size: these size a cache that is
#: already live by the first gate call, so a mid-run change would leave entries
#: written under one policy being judged by another.
_TTL_COST_RATIO_ENV = "KIROCREW_PATH_GATE_TTL_COST_RATIO"


_TTL_COST_RATIO_DEFAULT = 50.0


#: 0 is the REVERT: zero times any cost clamps to the floor for every input, so
#: ``KIROCREW_PATH_GATE_TTL_COST_RATIO=0`` pins one fixed 0.1s expiry at every
#: load level. That is deliberately the only spelling of the revert -- a separate
#: private boolean said the same thing while being unreachable from outside the
#: package, which made "operator lever" untrue. Above 1000 the clamp makes every
#: load level select the ceiling anyway, so a larger value expresses nothing this
#: cannot.
_TTL_COST_RATIO_MIN = 0.0


_TTL_COST_RATIO_MAX = 1000.0


_TTL_MAX_SECS_ENV = "KIROCREW_PATH_GATE_TTL_MAX_SECS"


_TTL_MAX_SECS_DEFAULT = 30.0


#: Floor on the ceiling: a ceiling below the expiry floor would invert the clamp.
_TTL_MAX_SECS_MIN = 0.1


_TTL_MAX_SECS_MAX = 60.0


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a float operator knob, falling back to *default*.

    Fail-soft TO THE DEFAULT in every failure mode -- absent, unparseable, out of
    range -- which is the conservative direction for both callers: the shipped
    ratio and the shipped ceiling are the reviewed values, so a bad override can
    only leave the reviewed behaviour in place and never widen the stale window
    the ceiling bounds.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using the default of %s", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "%s=%s is outside %s..%s; using the default of %s",
            name,
            value,
            minimum,
            maximum,
            default,
        )
        return default
    return value


#: Reciprocal of the share of the wall clock the gate may spend rebuilding
#: anchors. 50 holds that share at 2%, and it is the value at which an idle
#: interpreter selects the same 0.1s EXPIRY it selects today (2ms * 50 = the floor).
#: Only that duration is unchanged: an entry's total age is the expiry plus the build
#: that measured it, which for a 2ms build is the same to within those 2ms. So the
#: default is observationally identical on an unloaded host, by arithmetic rather
#: than by measurement.
#: Chosen as the SMALLEST ratio that refused nothing and paid the fewest
#: rebuilds at every load level the gate can still serve -- the same rule that
#: chose the floor, applied to the ratio. Larger ratios paid no fewer rebuilds
#: and only lengthened the stale window.
_HOME_TARGETS_TTL_COST_RATIO = _env_float(
    _TTL_COST_RATIO_ENV, _TTL_COST_RATIO_DEFAULT, _TTL_COST_RATIO_MIN, _TTL_COST_RATIO_MAX
)


#: Hard ceiling on the EXPIRY, bounding the deeper-leaf residual stale window
#: described above. The worst-case data age served is this ceiling PLUS the
#: rebuild that measured the entry (expiry starts at the clock read AFTER the
#: build), not the ceiling alone. A BOUNDED rebuild (pooled ``mc-pathres``) can
#: run its full 12s deadline -- the 8s ``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS`` budget
#: plus its ``min(budget*1.5, 4.0)`` = 4s grace -- so the worst case is ~42s for
#: the shipped default, not 30s; an INLINE rebuild
#: (``_cached_home_dir_targets(inline=True)`` off the loop) has no such deadline,
#: so a slow one adds its own unbounded duration -- the ceiling bounds the expiry,
#: not that build's wall time. The two ceilings are separate: DEFAULT 30s
#: (``_TTL_MAX_SECS_DEFAULT``), operator max 60s (``_TTL_MAX_SECS_MAX``); NOT
#: derived from the ratio, which asks for minutes (375s for a 7.5s rebuild).
_HOME_TARGETS_TTL_MAX_SECS = _env_float(
    _TTL_MAX_SECS_ENV, _TTL_MAX_SECS_DEFAULT, _TTL_MAX_SECS_MIN, _TTL_MAX_SECS_MAX
)


def _home_targets_ttl(rebuild_secs: float, *, resolution_differed: bool = True) -> float:
    """How long a target set that took *rebuild_secs* to build stays reusable.

    See the block above the constants for why this is a function of the build's
    own cost rather than a constant.  *rebuild_secs* is the WALL time the cache
    fill took, pool queue wait included: the caller blocked for all of it, and a
    saturated pool is exactly a state in which refreshing less often is correct,
    so the queue is part of the cost being amortised rather than noise to
    subtract.

    *resolution_differed* is what keeps the long expiry off the one class of
    install it could hurt.  True means one of the paths the build RESOLVED came
    back spelled differently -- in practice a protected entry under ``$HOME`` that
    is, or sits below, a symlink, or a symlinked leaf under a home-override root,
    the population :class:`_BuiltTargets` enumerates -- and that
    resolution-derived entry is exactly what a repoint can move out from under a
    cached set, the deeper-leaf residual.  There the expiry is pinned to the
    floor, so that window stays at 0.1s and does not grow.  False means nothing
    resolved differently, so the set holds nothing a repoint can stale WITHOUT
    first creating a symlink at a protected entry or inside the crew home, which
    is a write the write gate refuses; the adaptive expiry applies.  It defaults
    to True so every caller that cannot prove otherwise gets the short expiry.

    A STALLED rebuild never reaches here -- it raises and no entry is written --
    so a dead mount cannot inflate the expiry.  A zero or negative duration (a
    coarse clock, or a frozen one in tests) yields the floor, and so does a
    ratio of zero, which is how an operator pins one fixed 0.1s expiry at every
    load level.
    """
    if resolution_differed:
        return _HOME_TARGETS_TTL_SECS
    scaled = rebuild_secs * _HOME_TARGETS_TTL_COST_RATIO
    return min(max(scaled, _HOME_TARGETS_TTL_SECS), _HOME_TARGETS_TTL_MAX_SECS)


def _home_targets_tier(home_dirs: list[str]) -> str:
    """Name the gate whose ``home_dirs`` list this is, for the expiry-pin line.

    A label for a reader, never a decision: :func:`_report_expiry_pin` keys its
    state on the list itself, so an unrecognised list still gets its own slot and
    the right dedup while being named generically here. That is what keeps this
    from being a second place a new gate must be registered for correctness --
    ``test_every_gate_list_handed_to_the_builder_has_a_tier_name`` scans the call
    sites and fails when one arrives without a name, so the generic answer is a
    floor rather than somewhere to stop.

    Compared against the live lists rather than a table frozen at import, because
    the lists are assembled by ``+=`` across ``paths_lists`` and a test may extend one.
    """
    if home_dirs == _SENSITIVE_HOME_DIRS:
        return "read"
    if home_dirs == _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS:
        return "write"
    if home_dirs == _KEYSTONE_ARTIFACT_PARENTS:
        return "keystone-artifact"
    return f"unnamed {len(home_dirs)}-entry"


#: Last state :func:`_report_expiry_pin` reported FOR EACH ``home_dirs`` list, so the
#: line is emitted once per tier per TRANSITION rather than once per rebuild. Keyed on
#: the list itself rather than on one shared slot because the gates hand the builder
#: different lists whose answers can disagree on one host -- see
#: :func:`_report_expiry_pin`. The roots are deliberately NOT part of this key: they key
#: the target CACHE, but a tier reporting the same answer under new roots has not
#: transitioned and must not re-log. Bounded by the number of gate lists, a handful of
#: module constants, so it needs no eviction. A dict rather than a module global because
#: it is written from inside a function.
_home_targets_pin_state: dict[tuple[str, ...], bool] = {}


def _report_expiry_pin(home_dirs: list[str], pinned: bool) -> None:
    """Say once per tier, on each transition, which expiry that build selects.

    Without this the availability half self-disables in silence. On an install
    where a path the build RESOLVED came through a symlink -- a ``home_dirs`` entry
    under ``$HOME``, a crew-prefixed entry of the caller's ``home_dirs`` under
    ``KIROCREW_HOME``, a kiro-cli path under ``KIRO_HOME``, or a declared harness
    credential leaf under any variable in
    ``host_auth.home_override_env_vars()`` --
    the build that was handed that leaf reports a traversal, ITS expiry is
    therefore the floor, and both knobs read as no-ops for that tier to whoever
    tunes them. The refusals then come back with nothing in the log to say why the
    fix did not apply to this host.

    A dotfile-managed home IS a case to look for, and the commonest one. The
    trigger is narrower than "any symlinked dotfile" but not exotic: the symlinked
    path has to be a protected entry or sit on the path to one -- a ``home_dirs``
    entry under ``$HOME`` such as a linked ``~/.ssh`` or ``~/.aws``, a
    crew-prefixed entry under ``KIROCREW_HOME``, a kiro-cli path under
    ``KIRO_HOME``, or a declared harness credential leaf under its override -- and
    it has to sit BELOW the resolved root, since ``_resolve_root_anchors``
    canonicalises each root first. A symlinked ``sessions`` leaf under a
    ``KIROCREW_HOME`` below ``$HOME`` is that shape, and so is a ``goose`` directory
    symlinked into a store under ``~/.config`` or under whatever
    ``XDG_CONFIG_HOME`` resolves to. A dotfile that is neither a protected entry
    nor on the path to one cannot report a traversal, because no build resolves
    it.

    The dedup is per ``home_dirs`` list, because that list is what decides the
    answer. The gates hand the builder three different ones -- the read gate passes
    ``_SENSITIVE_HOME_DIRS``, :func:`is_sensitive_write_path` passes that plus
    ``_WRITE_PROTECTED_HOME_PATHS``, and :func:`_is_keystone_publish_artifact`
    passes ``_KEYSTONE_ARTIFACT_PARENTS`` -- while a symlinked write-protected crew
    leaf that holds no secret (``models``, ``sessions``, ``app-sources``) sits in
    the write gate's list alone. So one host can have the write build resolve that
    leaf and pin the floor while the keystone-artifact build reports no traversal
    and selects the cost-tracking expiry. Under one shared slot each of those builds
    flips the key the other just set, so the line alternates between two opposite
    messages for as long as the disagreement lasts and neither survives long enough
    to read as a state. A per-list slot lets each build transition its OWN state: a
    steady host says it once per tier rather than once per rebuild, and a tier that
    flips says it again.

    Both messages name the tier for the same reason. A reader diagnosing a refusal
    needs to know WHICH gate is pinned, and on a host whose builds disagree a line
    naming none of them cannot answer that -- the "cost-tracking expiry in force"
    half would read as the state of the whole gate while another tier is pinned to
    the floor, which is the opposite of the truth for whoever is tuning the knobs.
    """
    key = tuple(home_dirs)
    if _home_targets_pin_state.get(key) is pinned:
        return
    _home_targets_pin_state[key] = pinned
    tier = _home_targets_tier(home_dirs)
    if pinned:
        logger.info(
            "sensitive-path anchor cache (%s tier): expiry pinned to the %.1fs floor "
            "because a protected path resolved through a symlink, so "
            "the cost-tracking expiry and both of its knobs do not apply to this tier "
            "while that holds",
            tier,
            _HOME_TARGETS_TTL_SECS,
        )
    else:
        logger.info(
            "sensitive-path anchor cache (%s tier): cost-tracking expiry in force "
            "(ratio %s, cap %.1fs)",
            tier,
            _HOME_TARGETS_TTL_COST_RATIO,
            _HOME_TARGETS_TTL_MAX_SECS,
        )


# key -> (expiry_monotonic, targets)
_home_targets_cache: dict[tuple[object, ...], tuple[float, set[str]]] = {}


# Only off-loop bulk readers acquire this lock. Event-loop gates retain their
# bounded resolver and must never wait for an inline filesystem rebuild.
_home_targets_inline_lock = threading.Lock()


class _ResolvedRoots(NamedTuple):
    """The roots the sensitive-target set is anchored on, AND its cache key.

    Those two jobs are the same object on purpose: every field is part of the
    key, so an override that would move a target invalidates the cached set
    instead of serving targets anchored on the previous value. Keying on fewer
    fields than the builder anchors on is the fail-OPEN shape the resolved-home
    key already exists to prevent.

    A new adapter with its own credential home adds NOTHING here: its override
    variables arrive in ``adapter_roots``, projected from its own declaration in
    ``agent_sdk.host_auth``. That is what keeps this tuple fixed-width as harnesses
    are added, and what keeps the anchors and the key derived from one table rather
    than from a per-adapter field each caller has to remember to pair up.
    """

    home: str
    crew_home: str | None
    kiro_home: str | None
    #: Each declared ``$HOME``-override variable and the root it resolves to, in
    #: declaration order, ``None`` when the variable is unset.
    #:
    #: A TUPLE of pairs rather than a dict because this NamedTuple is also the
    #: cache key: it has to hash, and it has to change exactly when an override
    #: that would move a target changes. A dict would make the key unhashable,
    #: and the fallback -- keying on fewer fields than the builder anchors on --
    #: is the fail-OPEN shape the resolved-home key exists to prevent.
    adapter_roots: tuple[tuple[str, str | None], ...]
    logical_home: str
    # ``KIROCREW_OS_HOME`` is an ALTERNATE WHOLE ``$HOME``, not one adapter's
    # credential leaf: ``pod.runtime.build_pod_env`` sets it and
    # ``acp.client._apply_pod_home_remap`` makes it the literal ``HOME`` of a
    # pod-spawned kiro-cli child, so that child's whole credential store -- the
    # runtime identity store ``pod.runtime._seed_pod_os_home`` snapshots in, and
    # the MCP OAuth grants that child MINTS under ``.aws/sso/cache`` -- lives
    # under this root. No host SSO cache contents are copied in; that staging was
    # removed. It is therefore anchored by re-anchoring EVERY ``home_dirs`` entry
    # in ``_home_dir_targets_uncached``, rather than through
    # ``_OVERRIDE_ANCHORED_LEAVES``, which maps one leaf to the roots that move
    # it. Without it the relocated tree sits at a path no matcher
    # covers, so an agent inside a pod could read the operator's identity token
    # at the pod-path spelling while the identical bytes at ``~/.aws`` are
    # refused.
    os_home: str | None


#: Sensitive leaf -> the ``$HOME``-override VARIABLES that move it, and the
#: spelling it takes under each of them.
#:
#: PROJECTED from the harness declarations, not enumerated: the pairing has to
#: name the same leaf the list above fences and the same variable the resolver
#: below reads, and it was a third hand-maintained copy of both. A leaf absent
#: from the *home_dirs* list being built is skipped, so a write-tier build never
#: leaks a read-tier target.
#:
#: Read once at import, like the leaf list itself: the declarations are static
#: data, and re-projecting per gate call would put a table walk on the hot path.
_OVERRIDE_ANCHORED_LEAVES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    host_auth.override_anchored_leaves() + native_allow_stores.override_anchored_leaves()
)
#: Every variable an anchored leaf names; each root is resolved and keys the cache.
_ADAPTER_ROOT_ENVS: tuple[str, ...] = tuple(
    dict.fromkeys(host_auth.home_override_env_vars() + native_allow_stores.override_env_vars())
)


def _resolved_env_root(name: str) -> str | None:
    """Resolve an environment home override, or ``None`` when it is unset.

    Falls back to the unresolved absolute form on OSError/ValueError the same way
    the builder does. No validity check: an unsafe override falls back to its
    default inside the owning helper, and that default is already covered by the
    ``$HOME``-rooted entry, so an extra target under a bogus value is harmless
    and fail-safe.

    The value is read VERBATIM -- deliberately not stripped. Whitespace is a legal
    POSIX path character, and the owning resolvers take the variable raw
    (``_valid_override_home`` does ``Path(os.environ.get("KIROCREW_HOME"))``, and
    ``config_dir`` then ``mkdir``s whatever that names). Stripping here would
    anchor the target set on ``<root>`` while the process actually runs out of
    ``"<root> "``, leaving the real ``.env``, signing keys and governance files
    outside the floor this set defines. Emptiness is the only test, so an unset
    or empty override still resolves to ``None``.
    """
    expanded = _expanded_env_root(name)
    if expanded is None:
        return None
    # One child round trip; the gate itself batches every root through
    # _resolve_root_anchors instead, so go through _resolved_root_key from the
    # event loop.  A failure keeps the lexical form, exactly as the OSError arm
    # did.  ``Path.resolve()`` is ``os.path.realpath`` underneath, so the
    # resolved spelling is unchanged.
    return _realpath_or_none(expanded) or _lexical_root(expanded)


def _expanded_env_root(name: str) -> str | None:
    """The ``~``-expanded value of an environment home override, or ``None``."""
    raw = os.environ.get(name, "")
    if not raw:
        return None
    return os.path.expanduser(raw)


def _lexical_root(expanded: str) -> str:
    """Absolute, normalized spelling of *expanded* WITHOUT touching the filesystem.

    Deliberately not ``os.path.abspath``: on Windows that calls
    ``GetFullPathName``, which strips a trailing space or dot -- and the
    verbatim contract above says the anchor must keep it, because the owning
    resolvers run out of exactly the spelling the variable carries.
    """
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    return os.path.normpath(expanded)


#: The HOST's own override roots :func:`_resolve_root_anchors` resolves, in field
#: order. Each relocates a whole tree this core owns or honours itself.
#:
#: A harness's own credential home is NOT here: those arrive from
#: :func:`host_auth.home_override_env_vars` into ``adapter_roots``, so adding a
#: harness edits neither this tuple, nor ``_ResolvedRoots``, nor either of the two
#: loops that anchor on them.
_OVERRIDE_ROOT_ENVS: tuple[tuple[str, str], ...] = (
    ("crew_home", "KIROCREW_HOME"),
    ("kiro_home", "KIRO_HOME"),
    ("os_home", "KIROCREW_OS_HOME"),
)


def _resolve_root_anchors(logical_home: str) -> _ResolvedRoots:
    """Resolve every root the target set anchors on, in ONE child round trip.

    ``$HOME``, the override roots and every declared harness home travel in one
    request (:func:`_realpaths_or_none`), so :func:`_resolved_root_key` -- which
    runs once per ``is_sensitive_path`` call, on the event loop -- pays a single
    round trip rather than seven.  Each failed root keeps its lexical form, exactly
    as :func:`_resolved_env_root` does.  The stall bookkeeping is charged to the
    logical home's prefix: that is the mount every root ordinarily lives under,
    and it is the one the crash dumps named.
    """
    overrides = {field: _expanded_env_root(env) for field, env in _OVERRIDE_ROOT_ENVS}
    adapters = tuple((env, _expanded_env_root(env)) for env in _ADAPTER_ROOT_ENVS)
    wanted = [logical_home, *overrides.values(), *(root for _env, root in adapters)]
    distinct = list(dict.fromkeys(p for p in wanted if p is not None))
    answers = dict(zip(distinct, _realpaths_or_none(distinct)))

    def _root(expanded: str | None) -> str | None:
        if expanded is None:
            return None
        return answers[expanded] or _lexical_root(expanded)

    return _ResolvedRoots(
        home=answers[logical_home] or logical_home,
        logical_home=logical_home,
        adapter_roots=tuple((env, _root(root)) for env, root in adapters),
        **{field: _root(root) for field, root in overrides.items()},
    )


def _resolved_root_key() -> _ResolvedRoots:
    """Return the roots the target set is anchored on.

    Mirrors how :func:`_home_dir_targets_uncached` derives its anchors, so the
    cache key changes exactly when the anchors would. Falls back to the
    unresolved form on OSError/ValueError the same way the builder does.

    ``kiro_home`` is the resolved ``KIRO_HOME`` override (kiro-cli's own home
    override, honoured by ``kiro_agents_dir()``), or ``None`` when unset — it
    re-anchors the ``~/.kiro/agents`` write-protection, so a changed ``KIRO_HOME``
    must invalidate the cache. No validity check here (an unsafe value falls back
    to ``~/.kiro`` in ``kiro_home()``, already covered by the default form); it is
    resolved only so a symlinked override keys and anchors identically.

    ``adapter_roots`` does the same for every declared harness credential home in
    ``_OVERRIDE_ANCHORED_LEAVES``.

    ``logical_home`` is ``Path.home()`` UNRESOLVED.  It is a separate anchor, not
    a duplicate: on a host where ``$HOME`` is itself a symlink (``/home/x`` ->
    ``/local/home/x`` on cloud desktops) the resolved home spells every target
    one way while an agent-supplied ``~/.ssh/id_rsa`` spells it the other.  The
    resolved CANDIDATE normally bridges that -- but candidate resolution is
    bounded (:func:`_resolved_forms_bounded`) and degrades to the lexical
    spelling, which must still hit a target or the gate fails OPEN on exactly
    the hosts where ``$HOME`` is a link.  Keyed here so an env change that
    moves the logical spelling invalidates the cache like any other anchor.

    ``os_home`` is the resolved ``KIROCREW_OS_HOME`` override, or ``None`` when
    unset. It is an ALTERNATE ``$HOME``: ``pod.runtime.build_pod_env`` sets it
    and ``acp.client._apply_pod_home_remap`` makes it the literal ``HOME`` of a
    pod-spawned kiro-cli child, so kiro-cli's own ``$HOME``-derived credential
    store — the runtime identity store ``pod.runtime._seed_pod_os_home`` mirrors
    in, and the MCP OAuth grants that pod's own child MINTS under
    ``.aws/sso/cache`` — lives under this root rather than under the real home.
    No host SSO cache contents are copied in; that staging was removed. Anchoring
    it here is what fences the relocated tree, and it is the ONLY layer that can:
    the pod child's mount namespace must keep that tree readable AND writable
    because kiro-cli writes its grants there and no env lever relocates them. So
    the two audiences are split — the harness process reaches the tree, while an
    agent TOOL call naming any path under it is refused in-band here. Every
    ``home_dirs`` entry is re-anchored under it, not merely ``.aws``, because
    the variable relocates the whole home: the crew-home leaves, ``.ssh`` and
    every other fenced entry move with it. Same reasoning as the
    ``KIROCREW_HOME`` expansion below — an override must not move a secret out
    from under its own gate.
    """
    logical_home = str(Path.home())
    # Bounded (see _rebuild_targets_bounded): this runs on the event loop once per
    # is_sensitive_path call, and an inline resolve of a slow-to-stat $HOME is
    # exactly the stall the watchdog dumps caught.  All seven roots resolve in
    # ONE pool hop (_resolve_root_anchors).
    #
    # INVARIANT: the gate only ever compares against anchors resolved FRESH,
    # canonically, within the budget.  Anything else -- a stall, an open
    # cooldown, a pinned or faulted pool -- raises, and every gate turns that
    # into a refusal, exactly as it does for a stalled candidate.  Three
    # weaker fallbacks were each found open in review: lexical spellings (a
    # symlinked override root on another mount loses its canonical target), a
    # UNC skip (same, via a junction in a UNC home), and serving the previous
    # canonical resolution (a symlink repointed during the stall moves the
    # credential out from under the stale anchor).  Refusing for the cooldown
    # is the one outcome none of those reach.
    try:
        roots = _run_resolution_bounded(logical_home, _resolve_root_anchors)
    except PathResolutionStalled:
        roots = None
    if roots is None:
        raise PathResolutionStalled(logical_home, _stall_prefix(logical_home))
    return roots


def _home_dir_targets(home_dirs: list[str], *, inline: bool = False) -> set[str]:
    """TTL-cached :func:`_home_dir_targets_uncached`.

    Keyed on the *home_dirs* list plus the RESOLVED home and crew-home roots
    (see the note above the constant for why the raw env vars are not enough).

    *inline* resolves the anchors and rebuilds the set on the CALLING thread
    instead of through the bounded ``mc-pathres`` hop -- the same stance
    :func:`sandbox_credential_targets` takes, and for the same reason: the
    bound exists to keep the EVENT LOOP responsive, and a caller that is
    already on a worker thread gains nothing from it while its submissions
    queue ahead of the loop's own. Reserved for :func:`is_sensitive_resolved_path`,
    whose contract is exactly such a caller. The freshness invariant is kept:
    the roots are still resolved on every call and key the cache, so a repointed
    root still invalidates; what changes is only WHERE the ``realpath`` runs. A
    wedged mount blocks the calling thread here rather than raising a stall --
    which is what the same thread's own ``os.walk`` on that mount does anyway.

    ponytail: the returned set is the cached instance, not a copy — both
    callers only iterate it. A future caller that MUTATES the result would
    poison the cache for every other caller; copy here if that ever happens.
    """
    # Resolve the roots ONCE and use the same tuple for both the key and the
    # build. Resolving separately lets a root symlink repointed between the two
    # reads file one root's targets under the other root's key — a fail-OPEN
    # TOCTOU, pinned by the regression test
    # test_roots_are_resolved_once_for_key_and_build.
    if inline:
        roots = _resolve_root_anchors(str(Path.home()))
        cached = _home_targets_cache.get((tuple(home_dirs),) + roots)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        with _home_targets_inline_lock:
            # Resolve after acquiring: a queued worker must not reuse anchors
            # captured before another worker's potentially slow rebuild.
            roots = _resolve_root_anchors(str(Path.home()))
            return _cached_home_dir_targets(home_dirs, roots, inline=True)
    return _cached_home_dir_targets(home_dirs, _resolved_root_key(), inline=False)


def _cached_home_dir_targets(
    home_dirs: list[str], roots: _ResolvedRoots, *, inline: bool
) -> set[str]:
    """Share the target cache while coalescing inline builders at the caller."""
    key = (tuple(home_dirs),) + roots
    now = time.monotonic()
    cached = _home_targets_cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    if inline:
        targets = _home_dir_targets_uncached(home_dirs, roots)
    else:
        targets = _rebuild_targets_bounded(home_dirs, roots)
    # Read the clock AGAIN, and start the expiry here rather than at ``now``.
    # Two reasons, neither of them visible while a rebuild costs 2ms. The interval
    # between the two reads is the measured cost the expiry is a multiple of.
    # And an expiry that started BEFORE the build would already have elapsed by
    # the time a contended build returned -- a 2s build under a 0.1s expiry
    # hands back an entry that is expired on arrival, so the next call rebuilds
    # again and the cache stops being a cache exactly when it matters most.
    built_at = time.monotonic()
    # Read the flag off the built set, defaulting to the fail-safe answer: a test
    # double or any future builder that returns a plain ``set`` carries no
    # attribute and gets the floor rather than the long expiry.
    differed = bool(getattr(targets, "resolution_differed", True))
    _report_expiry_pin(home_dirs, differed)
    ttl = _home_targets_ttl(max(0.0, built_at - now), resolution_differed=differed)
    # Bound the dict: the key space is tiny (two constant home_dirs lists ×
    # roots), but a test or embedder that churns KIROCREW_HOME must not grow it
    # without limit.
    if len(_home_targets_cache) > 32:
        _home_targets_cache.clear()
    _home_targets_cache[key] = (built_at + ttl, targets)
    return targets


def _rebuild_targets_bounded(home_dirs: list[str], roots: _ResolvedRoots) -> set[str]:
    """Rebuild the target set on the ``mc-pathres`` pool.

    The anchors -- ``$HOME``, the ``KIROCREW_HOME`` / ``KIRO_HOME`` / adapter
    override roots and the ~40 keystone leaves under them -- are the paths the
    sensitive-target set is built FROM, as opposed to the agent-supplied
    candidate checked AGAINST it.  They are deliberately NOT ``realpath``'d
    inline on the event loop every time the target cache expires: on a Windows
    desktop under heavy disk load (a full test run plus several subagents, all
    being scanned by real-time antivirus) ``realpath($HOME)`` blocks past the
    25s loop-stall watchdog from inside ``on_tool_call``, and the gateway exits
    with every in-flight turn -- the same crash the bounded candidate
    resolution already prevents for the OTHER half of the check.

    The whole rebuild is ONE pool job, not one per leaf: a single bash command
    can drive ~200 rebuilds (``test_chained_cd_expansions_do_not_blow_up_the_gate``),
    and 40 thread hops per rebuild is what turns a 9s gate into a 15s one.  The
    stall bookkeeping is charged to ``roots.home``'s prefix, the mount every
    anchor ordinarily lives under and the one the crash dumps named.

    It carries its OWN budget (``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS``) because that
    single job does a few hundred ``realpath`` calls where a candidate resolution does one or
    two: sharing the candidate's budget sized the wait to the wrong work and let an
    ordinarily-slow rebuild on a loaded host read as a stalled mount.

    A rebuild that does not complete canonically within the budget RAISES, and
    every gate turns that into a refusal -- the same invariant as
    :func:`_resolved_root_key` (see the comment there for the three weaker
    fallbacks review found open: lexical spellings, a UNC skip, and serving the
    previous canonical set).  A pool fault is treated exactly like a stall, and
    a UNC home is probed here too (bounded): the candidate-side UNC shortcut is
    about tokens, not about the fence.  The stall is recorded, so the rebuild
    does not re-probe the wedged mount on every expiry -- it refuses without touching
    the filesystem until the cooldown lapses.
    """
    try:
        targets = _run_resolution_bounded(
            roots.home,
            lambda _home: _home_dir_targets_uncached(home_dirs, roots),
            budget=_PATH_RESOLVE_REBUILD_TIMEOUT_SECS,
        )
    except PathResolutionStalled:
        targets = None
    if targets is None:
        raise PathResolutionStalled(roots.home, _stall_prefix(roots.home))
    return targets

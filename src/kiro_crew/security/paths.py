"""Keystone: the sensitive-path lists, the resolver, the gates.

This is the always-on floor. Every other tier can be narrowed on the argument
that the OS sandbox sees the same thing; these declarations are the record of
what must stay unreadable and unwritable to the agent no matter which sandbox is
in force, and the gates below are what roughly forty modules import by name.

The comments on the declarations (``paths_lists``) carry more weight than the
code does: each one
records why an entry sits on the read-plus-write floor rather than the write-only
tier, which is not recoverable from the list itself. Read them before adding,
moving or removing an entry.

Layered internally. Layer one is declarations, predicates, the bounded resolver
and the public path gates, and it reads nothing else in this package. Layer two is
the command-line orchestrator, :func:`is_sensitive_bash_command`: it composes the
egress tier and the rules catalog, both of which load-import layer one, so it
reaches them through call-time imports in the one body that needs them. The fence
between the two layers is a comment and this module's load-time import list, not
a second file.

Layer one lives in owner modules beside this one, and this module is the import
path of every name they define: ``paths_lists`` holds the declarations,
``paths_resolver`` the bounded resolver, and ``paths_targets`` the home-directory
target sets built from both. The public path gates, their matchers, the scan-size
ceiling and layer two stay here. A read through this module answers with the
owner's object, and a write reaches every owner that binds the name
(:mod:`kiro_crew._facade_mirror`).

The command-line gate does not match paths. Sensitive paths are enforced by the OS
sandbox on the agent's process tree and by :func:`is_sensitive_path` on every
resolved path the file tools open; a regex over the text of a shell command added
no protection on top of those and denied ordinary read-only commands, so the
orchestrator carries only the size ceiling, the IMDS detector and the
environment-credential detector.

The resolver is bounded on purpose. A path check runs synchronously on the event
loop against an agent-supplied token, so a stalled automount under that token
would wedge the gateway. Resolution is therefore off-loaded to a small dedicated
pool with a deadline, and a stall fails CLOSED for every path under the stalled
prefix until the mount answers again.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import math
import os
import sys as _sys
from pathlib import Path
from typing import TYPE_CHECKING

from .diagnostics import annotate_refusal, refusal_diagnostic

logger = logging.getLogger(__name__)


#: Longest command ``is_sensitive_bash_command`` will scan. Longer input is
#: REFUSED, not skipped and not scanned: both detectors the gate runs are linear
#: in the subject, so this bound is what turns "linear" into a hard wall-clock
#: ceiling for a gate that runs synchronously on the event loop under a 25 s
#: watchdog (``dashboard.loop_stall_exit_after_secs``). The same number bounds
#: each tool_input string in ``llm_helpers``; a legitimate command this long is a
#: heredoc writing a file, and the tool-input tier already refuses it, so the
#: tiers agree.
MAX_SCANNABLE_COMMAND_CHARS = 20 * 1024

#: Ceiling on a cron SCRIPT BODY the source-body detectors in ``mcp_cron`` will scan.
#: It is a different number from the command ceiling because the two subjects have
#: different legitimate sizes: 20 KiB of shell on one ``Bash`` call is a heredoc, while
#: 20 KiB of cron script is an ordinary script, and a size-keyed refusal there is
#: permanent (re-fired on every tick until someone edits the file). Still a hard
#: ceiling: the full-text detectors are linear, so this bounds their wall time on the
#: event loop. ``mcp_cron._MAX_SCRIPT_SCAN_BYTES`` aliases it so the reader admits
#: exactly what the detectors will scan.
MAX_SCANNABLE_SOURCE_BODY_CHARS = 256 * 1024


def _oversize_refusal(length: int, limit: int) -> str:
    """The pass-0 refusal, in ONE spelling.

    Both entry points refuse above their own ceiling and both must say so the same
    way, because an operator reading the reason is being told which number to compare
    their input against.
    """
    return (
        "Blocked: input is too large to security-scan "
        f"({length} chars > {limit} limit); refused rather than left unscanned"
    )


def _path_in_home_dirs(
    path_str: str,
    home_dirs: list[str],
    base_dir: str | None = None,
    *,
    strict: bool = False,
    pre_resolved: bool = False,
) -> bool:
    """Return True if *path_str* resolves under any of *home_dirs* (``$HOME``-relative).

    Shared matching core for :func:`is_sensitive_path` (read+write gate,
    ``_SENSITIVE_HOME_DIRS``) and :func:`is_sensitive_write_path` (write-only
    gate, the read+write set PLUS ``_WRITE_PROTECTED_HOME_PATHS``). Keeping one
    implementation means the symlink/casefold hardening below cannot drift
    between the two gates.

    ── Symlink robustness ──
    A workspace symlink pointing at ``~/.aws/credentials`` (absolute OR relative
    ``../../.aws/credentials`` traversal) must NOT be readable through the link.
    We therefore check MULTIPLE candidate forms of the input and return True if
    ANY of them lands in a matched location:

      1. the fully symlink-RESOLVED canonical target (``realpath`` /
         ``Path.resolve`` — follows every symlink in the chain, including
         intermediate directories and the final component).  This is what
         defeats the symlink bypass: the resolved target of the link is
         ``~/.aws/credentials`` even though the link's own name is benign.
      2. the LEXICALLY-normalized path (no symlink following) and the raw
         expanded string — so a path that *textually* names a matched dir is
         still caught when resolution fails (dangling link, permission error).

    ``base_dir`` anchors a *relative* input against the caller's known working
    directory (e.g. the agent's workspace cwd) so a relative title like
    ``sub/cfg.ini`` resolves against the real directory rather than whatever CWD
    the gateway process happens to have.  Absolute inputs are unaffected;
    ``base_dir=None`` preserves the historical CWD-relative behavior.
    ``pre_resolved`` is :func:`_candidate_forms`'s flag of the same name, and
    such a caller is by contract on its own worker thread, so the anchors are
    resolved inline as well (``_home_dir_targets(inline=True)``): the whole
    check then performs no ``mc-pathres`` submission.
    """
    if not path_str:
        return False

    try:
        candidates = _candidate_forms(path_str, base_dir, pre_resolved=pre_resolved)
        # The anchors are bounded the same way (see _rebuild_targets_bounded):
        # a stall with no prior canonical resolution to serve refuses too.
        sensitive_targets = _home_dir_targets(home_dirs, inline=pre_resolved)
    except PathResolutionStalled:
        # Canonical form unavailable (wedged mount under the path): refuse.  A
        # lexical-only match here would pass a workspace symlink into a
        # credential store for the length of the stall.  A *strict* caller
        # (``sensitive_path_refusal``) wants to REPORT that as what it is rather
        # than as a match, so it gets the exception; the refusal is the same.
        if strict:
            raise
        return True

    # Case-fold both sides for the membership test.  On a case-insensitive
    # filesystem (macOS APFS/HFS+ default — a supported platform) the OS opens
    # ``~/.kirocrew/Security_Policy.json`` and ``~/.kirocrew/security_policy.json``
    # as the SAME file, so a byte-exact comparison would let the agent write its
    # own governance ceiling via an alternate-case path. Folding is strictly more
    # protective (it can only ever over-match an alternate-case variant of an
    # already-sensitive path, which is itself suspicious), so it is safe on
    # case-sensitive Linux too — matching the IGNORECASE bash-read matcher.
    for cand in candidates:
        cand_cf = cand.casefold()
        for sensitive_path in sensitive_targets:
            if cand_cf == sensitive_path or cand_cf.startswith(sensitive_path + os.sep):
                return True
    return False


def _is_keystone_publish_artifact(
    path_str: str,
    base_dir: str | None = None,
    *,
    strict: bool = False,
    pre_resolved: bool = False,
) -> bool:
    """Return True if *path_str* is the atomic-write temp or lock beside a keystone leaf.

    Closes the gap between a keystone leaf's FINAL name, which
    :data:`_SENSITIVE_HOME_DIRS` fences, and the intermediate inodes its publish
    actually goes through -- see :data:`_KEYSTONE_ARTIFACT_PARENTS` for why the rule is
    derived from the leaf list instead of restated per leaf.

    Two properties are load-bearing:

    - It reuses :func:`_candidate_forms` and :func:`_home_dir_targets`, so the
      symlink-resolution, casefolding and ``KIROCREW_HOME`` re-anchoring cannot drift
      from the main gate. A relocated crew home is covered because a
      ``<crew-prefix>``-rooted entry hits the prefix-stripping arm in
      :func:`_home_dir_targets_uncached`; a symlink aimed at a live temp is covered
      because the resolved form is one of the candidates.
    - The parent is compared for EQUALITY, not by prefix. An artifact is a direct child
      of the leaf's own directory, and a prefix test would sweep every descendant of the
      crew home whose name happens to end in ``.tmp`` -- far wider than this needs, in a
      directory that must stay readable.
    """
    if not path_str:
        return False
    try:
        artifact_parents = _home_dir_targets(_KEYSTONE_ARTIFACT_PARENTS, inline=pre_resolved)
        candidates = _candidate_forms(path_str, base_dir, pre_resolved=pre_resolved)
    except PathResolutionStalled:
        if strict:
            raise
        return True  # fail closed: see _path_in_home_dirs
    for cand in candidates:
        cand_cf = cand.casefold()
        # Suffixes are authored lowercase and the candidate is casefolded, so this is
        # the same case-insensitive comparison the rest of the gate makes -- on
        # macOS/Windows ``FOO.TMP`` and ``foo.tmp`` are the same file.
        if not cand_cf.endswith(_KEYSTONE_ARTIFACT_SUFFIXES):
            continue
        if os.path.dirname(cand_cf) in artifact_parents:
            return True
    return False


# Credential dot-dirs denied as a path COMPONENT anywhere in an app-picked local
# folder. This broadens the `is_sensitive_path()` floor below, which resolves its
# entries relative to $HOME and pins `.kube`/`.docker` to single leaf files
# (`config`, `config.json`): membership here denies these directory names at any
# depth and covers those two dirs whole. `path_contains_sensitive()` supplies the
# complementary ancestor/root protection. Owned here so every consumer
# (design_critique's local-target guard, design_tweak's project-folder guard)
# screens against the same set — a credential directory added for one app is
# automatically denied by the others.
DENIED_ROOT_PARTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})


def is_sensitive_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path points to a read+write-sensitive location.

    Used across every file-access surface (hooks.on_tool_call, validate_file_path,
    artifacts, dashboard file I/O, knowledge indexing) to block BOTH reads and
    writes of credential files and the governance trust-root
    (:data:`_SENSITIVE_HOME_DIRS`). See :func:`_path_in_home_dirs` for the
    symlink/casefold matching contract.

    Also covers a protected leaf's publish artifacts
    (:func:`_is_keystone_publish_artifact`): the temp an ``atomic_write`` renames over
    the leaf holds the leaf's full payload, so READ is blocked alongside write -- a
    write-only fence there would still disclose ``.env`` or ``token_signing.key`` to a
    reader that wins the race.

    The decision is :func:`sensitive_path_refusal`'s -- this is its boolean
    spelling, so the two cannot diverge. A stall is refused there (a string) and is
    therefore ``True`` here: the callers that only hold this boolean keep refusing
    fail-closed; what they lose is the distinct WORDING, which is the gate
    consumers' business.
    """
    return sensitive_path_refusal(path_str, base_dir) is not None


def is_sensitive_resolved_path(resolved: str) -> bool:
    """:func:`is_sensitive_path` for a path the caller has ALREADY canonicalised.

    Same decision and same targets, with NO ``mc-pathres`` submission on either
    half: the candidate is matched lexically, and the anchors (``$HOME``, the
    override roots, the keystone leaves) are resolved inline on the calling
    thread (:func:`_home_dir_targets` with ``inline=True``), fresh on every call
    and keying the same TTL cache the bounded path uses. *resolved* MUST be the
    output of ``os.path.realpath`` (or ``Path.resolve``) that the caller computed
    on its OWN worker thread: the only thing the bounded resolution would add for
    such an input is the same string back, since a canonical path has no link
    left to follow. Handing this an unresolved spelling is a link bypass, and
    calling it from the event loop forfeits the bound the pool exists to give
    that loop -- so it is for exactly one shape of caller: a bulk WALK on a
    worker thread that already resolves every entry to detect symlink loops and
    prove containment, and only then asks whether the entry is fenced.

    Why a separate entry point rather than "just call the pool anyway": the pool
    is sized for the event loop (two workers by default --
    ``executors._MAX_PATH_RESOLVE_WORKERS`` -- so a wedged mount can pin at most
    that many threads), and it is FIFO. A walk over a thousand skill directories, each
    submitting a resolution the walk had already performed plus an anchor
    resolution per call, fills that queue from worker threads while the loop's
    own latency-critical resolutions wait behind it -- not for a slow disk, for
    the queue -- and the accumulated waits cross the loop-stall watchdog. The
    scanner's realpath is unbounded either way (it runs off the loop, and
    ``os.walk`` on the same mount is unbounded too), so the pool bought that
    caller nothing and cost the loop its budget.

    A wedged mount therefore does not surface here as a refusal: it blocks the
    calling thread inside ``realpath``, exactly as that thread's own walk of the
    same mount would. Nothing is admitted while it blocks.
    """
    return _path_in_home_dirs(resolved, _SENSITIVE_HOME_DIRS, pre_resolved=True) or (
        _fold_windows_alias(resolved).casefold().endswith(_KEYSTONE_ARTIFACT_SUFFIXES)
        and _is_keystone_publish_artifact(resolved, pre_resolved=True)
    )


def is_sensitive_canonical_path(resolved: str) -> bool:
    """The sensitive-path verdict for a path the CALLER already canonicalised.

    The one entry point for a reader that computed ``os.path.realpath`` or
    ``Path.resolve`` itself and would otherwise hand the result to
    :func:`is_sensitive_path`, which resolves it again on the ``mc-pathres``
    pool and fails closed when the pool misses its budget. Which gate answers
    depends on the calling thread, and that is decided here rather than by the
    caller's say-so:

    * Off the event loop, :func:`is_sensitive_resolved_path` answers inline: no
      pool submission, so a saturated pool cannot refuse a healthy file.
    * On the event loop, :func:`is_sensitive_path` answers, bounded: the inline
      anchor ``realpath`` the pre-resolved gate performs is the blocking call
      the pool exists to keep off the loop, and the anchors (the override
      roots) need not share the caller's mount.

    The decision is the same gate list either way; only the submission path
    differs. The canonical-spelling half of the contract stays the caller's:
    hand this the resolved spelling, never the raw one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return is_sensitive_resolved_path(resolved)
    return is_sensitive_path(resolved)


def canonical_path_refusal(resolved: str) -> str | None:
    """:func:`is_sensitive_canonical_path` as reason-or-``None``: same gate per thread.

    Only the on-loop (bounded) gate can stall, so only it can return the stall wording.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if is_sensitive_canonical_path(resolved):
            return f"Blocked: access to sensitive path: {resolved}"
        return None
    return sensitive_path_refusal(resolved)


#: The fixed opening of an unverifiable-path refusal. Consumers tell a stall from a
#: match with :func:`is_unverifiable_path_refusal`, a prefix test, and never by
#: searching the text: both refusals embed the caller-chosen path, and a prefix is
#: the one place that path cannot reach -- a substring test would let a path
#: spelled to contain the phrase pass itself off as a stall.
UNVERIFIABLE_PATH_PREFIX = (
    "Blocked: the path could not be verified against the sensitive-path list within "
    "the resolver budget"
)


def is_unverifiable_path_refusal(reason: str) -> bool:
    """True when *reason* is the stall refusal :func:`sensitive_path_refusal` produces.

    Structural, by the fixed prefix that precedes any caller-influenced text. The
    match refusal opens ``Blocked: access to sensitive path:`` instead, so no path
    spelling can move one refusal into the other's class.
    """
    return reason.startswith(UNVERIFIABLE_PATH_PREFIX)


def sensitive_path_refusal(path_str: str, base_dir: str | None = None) -> str | None:
    """The path tier of the tool gate: the refusal for *path_str*, or ``None``.

    Reason-or-``None`` like the other tiers (``is_sensitive_bash_command``,
    ``audit_bash_exfiltration``, ``is_denied``), so ``hooks.on_tool_call`` applies
    it the same way.

    The ONE decision: :func:`is_sensitive_path` is ``refusal is not None``. A path
    whose canonical form (or whose anchors) could not be established within the
    resolve budget is refused exactly as a match is, fail-closed, and this function
    is never a way to let one through -- but the two refusals get different WORDS.
    A match is ``Blocked: access to sensitive path: <path>``. A stall opens with
    :data:`UNVERIFIABLE_PATH_PREFIX`, says the path is NOT a match, and quotes the
    path LAST: a stall reported as a match leads the agent reading it to conclude,
    reasonably and wrongly, that an ordinary project file holds a credential, that
    the session has been locked down, or that a different spelling might pass, and
    each of those costs a wasted round where "could not verify within budget, retry
    shortly" costs one wait.
    """
    try:
        matched = _path_in_home_dirs(
            path_str, _SENSITIVE_HOME_DIRS, base_dir, strict=True
        ) or _is_keystone_publish_artifact(path_str, base_dir, strict=True)
    except PathResolutionStalled as exc:
        return _stall_refusal(exc, path_str)
    return f"Blocked: access to sensitive path: {path_str}" if matched else None


def sensitive_write_path_refusal(path_str: str, base_dir: str | None = None) -> str | None:
    """:func:`is_sensitive_write_path` as reason-or-``None``: same decision, a stall
    still refuses, worded as the stall :func:`sensitive_path_refusal` words it."""
    try:
        matched = _path_in_home_dirs(
            path_str, _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS, base_dir, strict=True
        ) or _is_keystone_publish_artifact(path_str, base_dir, strict=True)
        matched = matched or _native_store_write(path_str, base_dir)
    except PathResolutionStalled as exc:
        return _stall_refusal(exc, path_str)
    return f"Blocked: write to protected path: {path_str}" if matched else None


def _stall_refusal(exc: PathResolutionStalled, path_str: str) -> str:
    # Caller text follows the fixed prefix. The stalled prefix is named only while its
    # cooldown is open: most raises (wait allowance spent, pool saturated, load, clamped
    # grace) never charge it, so naming it there would blame the wrong cause. A stalled
    # anchor rebuild is charged to the home prefix and refuses paths outside it too, so
    # the wait names the checks that touch the prefix, not only paths beneath it.
    now = _path_resolve_clock()
    with _path_resolve_lock:
        until = _path_resolve_degraded.get(exc.prefix, (0.0, 0))[0]
    if until > now:
        cause = f": the check timed out under {exc.prefix!r}"
        wait = (
            f"Path checks that touch that prefix are refused for about "
            f"{max(1, math.ceil(until - now))} s more; retry the same call after that wait"
        )
    else:
        cause = ""
        wait = "Retry the same call after a short wait"
    return (
        f"{UNVERIFIABLE_PATH_PREFIX} (symlink resolution did not complete in time"
        f"{cause}), so it is refused fail-closed. "
        f"This is NOT a match: the path is not known to be sensitive. {wait}; do not "
        f"re-spell it. Path: {path_str!r}"
    )


def path_contains_sensitive(
    dir_str: str, base_dir: str | None = None, *, pre_resolved: bool = False
) -> bool:
    """Return True if a read+write-sensitive location lies UNDER *dir_str*.

    The REVERSE direction of :func:`is_sensitive_path`: that gate answers "is
    this path inside a protected location?", this one answers "does this
    directory CONTAIN one?". A bulk operation rooted at *dir_str* — e.g. the
    Notes builtin's ``git add -A`` over an attached vault — sweeps every file
    below the root, so a root that is an ANCESTOR of a credential store (the
    home directory itself, or a parent of ``~/.ssh``) would stage and push the
    credentials wholesale even though the root is not itself a sensitive path.

    List-based, no filesystem walk: the known sensitive roots
    (:data:`_SENSITIVE_HOME_DIRS`, including the crew data-home secret leaves
    and any ``KIROCREW_HOME`` re-anchoring) are prefix-compared against the
    directory's candidate forms, so the check is O(sensitive entries) even when
    *dir_str* is a huge tree. Shares :func:`_candidate_forms` and
    :func:`_home_dir_targets` with :func:`_path_in_home_dirs` so the
    symlink/casefold hardening cannot drift between the two directions.

    ``pre_resolved`` is :func:`_candidate_forms`'s flag of the same name, paired
    with inline anchors exactly as :func:`_path_in_home_dirs` pairs them, and it
    carries :func:`is_sensitive_resolved_path`'s preconditions verbatim: no
    ``mc-pathres`` submission on either half, *dir_str* MUST be the
    ``os.path.realpath`` the caller computed on its OWN worker thread, and a
    caller on the event loop forfeits the bound the pool exists to give it. See
    there for why a bulk walk may claim it. One question per directory, so the
    walk that asks it would pay a pool hop per directory for nothing.
    """
    if not dir_str:
        return False
    try:
        sensitive_targets = _home_dir_targets(_SENSITIVE_HOME_DIRS, inline=pre_resolved)
        candidates = _candidate_forms(dir_str, base_dir, pre_resolved=pre_resolved)
    except PathResolutionStalled:
        return True  # fail closed: see _path_in_home_dirs
    for cand in candidates:
        # Normalize away a trailing separator so `/home/u/` and `/home/u`
        # produce the same prefix (a bare `/` or `C:\` root rstrips to ""/"C:",
        # whose prefix form still matches everything under it — correct: every
        # sensitive path is inside the filesystem root).
        cand_cf = cand.casefold().rstrip(os.sep)
        prefix = cand_cf + os.sep
        for target in sensitive_targets:
            # Equality (the dir IS the sensitive path) is is_sensitive_path's
            # job, but including it here fails safe for callers using only this
            # gate.
            if target == cand_cf or target.startswith(prefix):
                return True
    return False


def is_sensitive_write_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path must not be MODIFIED by an agent tool.

    Superset of :func:`is_sensitive_path`: everything that is read+write blocked
    PLUS the write-only-protected runtime config files
    (:data:`_WRITE_PROTECTED_HOME_PATHS`), which stay readable but must not be
    written by the agent. Enforced at the file-edit tool gate
    (``hooks.on_tool_call`` on the ACP ``edit`` kind) — see
    :data:`_WRITE_PROTECTED_HOME_PATHS` for the rationale.

    The publish-artifact clause is repeated from :func:`is_sensitive_path` rather than
    left to be inherited, because this gate is documented as a SUPERSET of it: omitting
    it here would leave a keystone temp writable through the edit gate while the
    read+write gate refused it, the same one-path-only hole the pairing notes above warn
    about.
    """
    return sensitive_write_path_refusal(path_str, base_dir) is not None


def sensitive_home_dirs() -> tuple[str, ...]:
    """Public, read-only view of the read+write-blocked home-relative paths.

    Lets the security-posture surface (``security_posture_controls.py``) enumerate what
    :func:`is_sensitive_path` actually blocks without coupling to the private
    ``_SENSITIVE_HOME_DIRS`` name — the same rationale as
    :func:`get_credential_patterns`. Returned as a tuple so a caller cannot
    mutate the live blocklist.
    """
    return tuple(_SENSITIVE_HOME_DIRS)


def write_protected_home_paths() -> tuple[str, ...]:
    """Public, read-only view of the write-only-protected home-relative paths.

    Companion to :func:`sensitive_home_dirs` — these stay readable but must not
    be written by an agent tool.
    """
    return tuple(_WRITE_PROTECTED_HOME_PATHS)


def crew_home_prefixes() -> tuple[str, ...]:
    """Public view of the known crew data-home prefixes.

    Used to classify a sensitive path as a Kiro Crew trust root vs. a third-party
    credential store when describing the posture.
    """
    return tuple(_CREW_HOME_PREFIXES)


def sandbox_credential_targets(exclude_leaves: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Absolute, on-disk-case credential targets for an OS sandbox deny list.

    Applies the SAME anchoring as :func:`_home_dir_targets_uncached` -- the
    ``$HOME`` projection of every :data:`_SENSITIVE_HOME_DIRS` leaf, plus each
    env-override re-anchor -- so a caller building a sandbox mask inherits the
    read gate's anchor rules instead of re-deriving them. That is the whole point
    of this living here: a mask that projected leaves under ``Path.home()`` only
    would silently miss a credential the operator relocated with
    ``KIROCREW_HOME``, ``CLAUDE_CONFIG_DIR`` or ``CLAUDE_HOME``, which is exactly
    the drift a hand-maintained list already produced once.

    Unlike the read gate's target set the paths are NOT casefolded: that set
    exists to COMPARE against candidate paths, while these are handed to a
    sandbox backend to deny on disk, and a casefolded path denies nothing on a
    case-sensitive filesystem.

    *exclude_leaves* drops a ``$HOME``-relative leaf (and its override
    re-anchors) from the result -- for an adapter whose own OAuth token it must
    still be able to read in order to authenticate. Excluding a leaf here only
    removes it from THIS mask; the read gate still fences it for the agent's own
    file tools, so the two controls keep covering different readers.

    Returns logical paths. The launcher ``os.path.abspath``es what it is handed,
    and the macOS profile emits both a ``subpath`` and a ``literal`` deny for each
    entry, so both a directory and a plain file leaf are valid entries.
    """
    excluded = set(exclude_leaves)
    leaves = [d for d in _SENSITIVE_HOME_DIRS if d not in excluded]
    # Resolved INLINE, not through _resolved_root_key: that one is bounded for
    # the event loop and RAISES on a stall, and this runs off the loop already
    # (``_sandbox_preflight`` wraps it in ``asyncio.to_thread``). A sandbox mask
    # must be canonical whatever the disk is doing, so the worker that resolves
    # the roots for the gate is called here directly and waits -- in THIS
    # interpreter, since no bounded-call deadline is armed (``_outside_bounded_call``),
    # so it neither holds a resolver child nor queues ahead of the loop; the spawn
    # side bounds that wait (``_run_preflight_bounded``, 60 s) and refuses the
    # adapter on expiry rather than starting it unmasked (found in review).
    resolved = _resolve_root_anchors(str(Path.home()))
    # BOTH home spellings, reusing the two anchors the read gate already keys on.
    # On a host whose home is itself a symlink (``/home/u`` -> ``/local/home/u``) the
    # resolved and logical spellings differ, and the read gate can absorb that because
    # it realpaths a candidate BEFORE comparing. A sandbox deny list gets no such
    # normalisation -- it denies the paths it is handed -- so denying only the resolved
    # form would leave every credential reachable through the symlinked one.
    home_anchors = {resolved.home, resolved.logical_home}
    targets: set[str] = {
        os.path.join(anchor, *_leaf_segments(d)) for anchor in home_anchors for d in leaves
    }
    # KIROCREW_HOME: the crew secrets (signing keys, governance ceiling, .env)
    # live directly under the override, not under either default crew prefix.
    if resolved.crew_home:
        for d in leaves:
            for prefix in _CREW_HOME_PREFIXES:
                if d == prefix or d.startswith(prefix + "/"):
                    leaf = d[len(prefix) :].lstrip("/")
                    targets.add(
                        os.path.join(resolved.crew_home, *_leaf_segments(leaf))
                        if leaf
                        else resolved.crew_home
                    )
                    break
    # An adapter's credential store follows that adapter's own home override.
    adapter_roots = dict(resolved.adapter_roots)
    for leaf, root_envs, under_root in _OVERRIDE_ANCHORED_LEAVES:
        if leaf in excluded or leaf not in _SENSITIVE_HOME_DIRS:
            continue
        for env in root_envs:
            root = adapter_roots.get(env)
            if root:
                targets.add(os.path.join(root, *_leaf_segments(under_root)))
    return tuple(sorted(targets))


# ---------------------------------------------------------------------------
# Layer two: the command-line orchestrator
# ---------------------------------------------------------------------------
# Everything above is layer one, with the declarations, the bounded resolver and the
# target sets it reads from the owners imported at this module's foot (``paths_lists``,
# ``paths_resolver``, ``paths_targets``), reading nothing in this package but the shell
# reader one layer below. The gate below composes the tiers that sit ABOVE this module -- the
# egress tier and the rules catalog -- each of which load-imports layer one. That is
# why the references to them are call-time imports inside the body that needs them
# and never module-level: a module-level import of either would close a load-time
# cycle, and the alternative, handing them in as parameters, would put the tier
# list in a public signature.


def is_sensitive_bash_command(
    command: str,
    *,
    enabled_ids: "frozenset[str] | None" = None,
) -> str | None:
    """Refuse a bash command that reaches IMDS or leaks environment credentials.

    The subject is a SHELL COMMAND LINE. The two detectors read it with shell grammar
    -- separator runs are redundant, newlines and ``|`` split pipeline stages, an
    ``env | grep`` pipeline is one command -- and none of that holds for a Python
    source file. A caller with a source body in hand must not route it here: every
    shell pass over a source body produces a class of false denial on ordinary
    scripts. The cron script gate (``mcp_cron._vet_script_contents``)
    runs only full-text detectors that are meaningful on source, and the sandbox is the
    runtime control for what a script may open.

    This gate does NOT match PATHS in command text. Sensitive paths are enforced where
    the spelling of a command cannot talk around them: the OS sandbox hides the
    credential stores (``~/.aws``, ``~/.gnupg``, SSH keys) from the agent's process
    tree and mounts the governance keystone (``security_policy.json``, ``profiles/``,
    ``admission_policy.json``, ``computer_use.json``) read-only in every mode, and
    :func:`is_sensitive_path` refuses every resolved path the file tools open. A text
    matcher over ``cat ~/.aws/credentials`` adds no protection on top of that and
    denies ordinary read-only commands whenever a fenced spelling appears as data
    (a grep pattern, a commit message, a note), so no such matcher runs here; a
    keystone READ through the shell is permitted by design.

    Three checks, in order:

    * **Pass 0, size ceiling.** A subject longer than
      :data:`MAX_SCANNABLE_COMMAND_CHARS` is refused unscanned.
    * **IMDS access** (``exfil._check_imds_access``) via any IP encoding.
    * **Environment credential exfiltration** (``denied_rules._check_env_credential_access``):
      ``declare -p``, ``env | grep``, ``printenv`` and their kin.

    Returns denial reason string, or None if clean.
    """
    # Call-time imports, layer two. Both tiers load-import this module for the keystone
    # declarations, so a module-level import here would close a load-time cycle; handing
    # them in as parameters would put the tier list in a public signature instead.
    # Resolving them per call also keeps a patch applied to the package attribute
    # observable here, which a name bound at import time would not be.
    from .denied_rules import _check_env_credential_access
    from .exfil import _check_imds_access

    # ── Pass 0: size ceiling ──
    # Both detectors below are linear in the subject, and this bound is what makes
    # that a wall-clock ceiling: the gate runs synchronously on the event loop,
    # so its worst case IS the loop's worst case. An oversized subject is
    # refused, never scanned partially and never let through unscanned -- a
    # denied long command is recoverable by the operator, a stalled gateway and
    # an unscanned command are not.
    #
    # The diagnostic is built HERE rather than inside the refusal producer, which
    # both entry points share and which is handed two numbers rather than the
    # subject. Its span is the whole subject because nothing matched: this refusal
    # is "not scanned", and pointing at a region would name a match that was never
    # attempted. The census behind it is bounded for the same reason the scan is.
    if len(command) > MAX_SCANNABLE_COMMAND_CHARS:
        return annotate_refusal(
            _oversize_refusal(len(command), MAX_SCANNABLE_COMMAND_CHARS),
            refusal_diagnostic("keystone-scan-ceiling", "size-ceiling", command),
        )

    # IMDS access via any IP encoding (decimal, hex, octal, IPv6-mapped)
    imds_result = _check_imds_access(command, enabled_ids=enabled_ids)
    if imds_result:
        return imds_result

    # Environment credential exfiltration (declare -p, env|grep, printenv, etc.)
    env_result = _check_env_credential_access(command)
    if env_result:
        return env_result
    return None


# This module is the import path of every name the path floor offers. The public path
# gates, their matchers and the command-line orchestrator above stay here; layer one's
# declarations, resolver and target sets live in the owner modules below, and a write
# through this module (a patch) reaches every module holding the name
# (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. New work goes to the owner of its responsibility:
#   ``paths_lists`` -- the sensitive and write-protected declarations
#   ``paths_resolver`` -- the bounded resolver and its stall accounting
#   ``paths_targets`` -- the home-directory target sets and their cache, and the candidate
#   spellings of a path the matchers compare (``_candidate_forms``)
from kiro_crew import _facade_mirror  # noqa: E402

from .paths_lists import (  # noqa: E402
    _CREW_HOME_PREFIXES,
    _KEYSTONE_ARTIFACT_PARENTS,
    _KEYSTONE_ARTIFACT_SUFFIXES,
    _SENSITIVE_HOME_DIRS,
    _WRITE_PROTECTED_HOME_PATHS,
    _leaf_segments,
    _native_store_write,
)
from .paths_resolver import (  # noqa: E402
    PathResolutionStalled,
    _fold_windows_alias,
    _path_resolve_clock,
    _path_resolve_degraded,
    _path_resolve_lock,
)
from .paths_targets import (  # noqa: E402
    _OVERRIDE_ANCHORED_LEAVES,
    _candidate_forms,
    _home_dir_targets,
    _resolve_root_anchors,
)

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.security.paths_lists",
    "kiro_crew.security.paths_resolver",
    "kiro_crew.security.paths_targets",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.security.paths_lists import (  # noqa: F401
        _CREW_SECRET_LEAVES,
        _KIRO_AGENTS_DIR,
        _KIRO_CLI_WRITE_TIER_LEAVES,
        _KIRO_MATE_AGENTS_DIR,
        _KIRO_SETTINGS_MCP_JSON,
        _LEAF_SEPARATOR,
        _NATIVE_ALLOW_STORE_LEAVES,
    )
    from kiro_crew.security.paths_resolver import (  # noqa: F401
        _CHILD_FAULT_WARN_STREAK,
        _FS_BLOCKING_SYSCALLS,
        _FS_BLOCKING_SYSCALLS_BY_ARCH,
        _ON_WINDOWS,
        _PATH_RESOLVE_COOLDOWN_MAX_SECS,
        _PATH_RESOLVE_COOLDOWN_SECS,
        _PATH_RESOLVE_GRACE_FACTOR,
        _PATH_RESOLVE_GRACE_MAX_SECS,
        _PATH_RESOLVE_LOAD_MAX_PROBES,
        _PATH_RESOLVE_LOAD_WINDOW_SECS,
        _PATH_RESOLVE_REBUILD_TIMEOUT_SECS,
        _PATH_RESOLVE_TIMEOUT_SECS,
        _PATH_RESOLVE_WAIT_CAP_SECS,
        _PATH_RESOLVE_WAIT_FLOOR_SECS,
        _PATH_RESOLVE_WAIT_WINDOW_SECS,
        _UNC_PREFIX_RE,
        _WIN_DEFAULT_STREAM_RE,
        _WIN_LOCAL_NS_RE,
        _absolutized,
        _child_blocked_in_filesystem,
        _child_budget,
        _child_fallback_warned,
        _child_fault_streak,
        _child_fault_warned,
        _child_request,
        _extend_cooldown,
        _inline_bounded,
        _is_unc_path,
        _load_arm_budget_spent,
        _mark_stalled,
        _outside_bounded_call,
        _path_resolve_load_probes,
        _path_resolve_thread_waits,
        _pool_down_to_last_child,
        _realpath_inline,
        _realpath_or_none,
        _realpaths_or_none,
        _resolve_on_calling_thread,
        _resolved_forms_bounded,
        _resolved_spellings,
        _resolved_spellings_inline,
        _ResolvedT,
        _run_resolution_bounded,
        _stall_prefix,
    )
    from kiro_crew.security.paths_targets import (  # noqa: F401
        _ADAPTER_ROOT_ENVS,
        _HOME_TARGETS_TTL_COST_RATIO,
        _HOME_TARGETS_TTL_MAX_SECS,
        _HOME_TARGETS_TTL_SECS,
        _OVERRIDE_ROOT_ENVS,
        _TTL_COST_RATIO_DEFAULT,
        _TTL_COST_RATIO_ENV,
        _TTL_COST_RATIO_MAX,
        _TTL_COST_RATIO_MIN,
        _TTL_MAX_SECS_DEFAULT,
        _TTL_MAX_SECS_ENV,
        _TTL_MAX_SECS_MAX,
        _TTL_MAX_SECS_MIN,
        _anchor_targets,
        _BuiltTargets,
        _cached_home_dir_targets,
        _env_float,
        _expanded_env_root,
        _home_dir_targets_uncached,
        _home_targets_cache,
        _home_targets_inline_lock,
        _home_targets_pin_state,
        _home_targets_tier,
        _home_targets_ttl,
        _lexical_root,
        _rebuild_targets_bounded,
        _report_expiry_pin,
        _resolved_env_root,
        _resolved_root_key,
        _ResolvedRoots,
    )
if TYPE_CHECKING:  # names it serves from outside its owners, from their homes
    from kiro_crew.agent_sdk import (  # noqa: F401
        host_auth,
        native_allow_stores,
    )
    from kiro_crew.executors import (  # noqa: F401
        path_resolve_executor,
    )
    from kiro_crew.identity_stores import (  # noqa: F401
        AUTH_SQLITE_DB,
        AUTH_SQLITE_SIDECAR_SUFFIXES,
        fenced_home_dirs,
    )
    from kiro_crew.memory_stores import (  # noqa: F401
        MEMORY_STORES_DIR_NAME,
    )
    from kiro_crew.subprocess_pool import (  # noqa: F401
        OP_REALPATH_MANY,
        OP_REALPATH_SPELLINGS,
        SubprocessPoolTimeout,
        SubprocessPoolUnavailable,
        pack_strings,
        unpack_strings,
    )
